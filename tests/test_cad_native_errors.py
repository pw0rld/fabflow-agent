"""Native failure receipts and STEP export gates; no CAD or model calls."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cad_manufacturing import CadManufacturingService, NATIVE_DIAGNOSTIC_CHARS, validate_geometry
from prep_record import RecordError
from test_cad_manufacturing import fixture_geometry
from workbench_store import WorkbenchError


class NativeErrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / 'input.step'
        self.source.write_bytes(b'PRIVATE-SOURCE-CONTENT-NOT-A-LOG')
        self.service = CadManufacturingService.__new__(CadManufacturingService)
        self.service.native_python = '/isolated/native/python'

    def run_result(self, code=1, stdout='', stderr=''):
        with patch('cad_manufacturing.subprocess.run', return_value=
                   subprocess.CompletedProcess([], code, stdout, stderr)) as run:
            self.service._extract(self.source, self.folder)
        return run

    def receipt(self):
        return json.loads((self.folder / 'native-diagnostic.json').read_text())

    def test_stderr_structured_error_precedes_old_stdout_and_ocp_word(self):
        stderr = 'OCP transfer notice\n' + json.dumps({'ok': False, 'error': '导出 STEP 的几何偏差超出容许范围。'})
        with self.assertRaisesRegex(WorkbenchError, '几何偏差超出容许范围'):
            self.run_result(stdout=json.dumps({'error': 'old stdout detail'}), stderr=stderr)
        receipt = self.receipt()
        self.assertEqual(receipt['returncode'], 1)
        self.assertFalse(receipt['timed_out'])
        self.assertEqual(receipt['stderr'], stderr)
        self.assertNotIn(self.source.read_text(), json.dumps(receipt))

    def test_old_stdout_error_remains_supported(self):
        with self.assertRaisesRegex(WorkbenchError, '原始单位缺失'):
            self.run_result(stdout='progress\n[1, 2]\n' +
                            json.dumps({'error': '原始单位缺失'}), stderr='OCP native warning')

    def test_structured_error_wins_over_import_trace_and_empty_error_does_not_mask_it(self):
        stderr = 'ModuleNotFoundError: No module named OCP\n' + json.dumps({'error': '具体几何错误'})
        with self.assertRaisesRegex(WorkbenchError, '具体几何错误'):
            self.run_result(stderr=stderr)
        with self.assertRaisesRegex(WorkbenchError, 'stdout detail'):
            self.run_result(stdout=json.dumps({'error': 'stdout detail'}),
                            stderr=json.dumps({'error': '  \n '}) + '\nnull\n[1]')

    def test_only_actual_import_exception_identifies_missing_dependency(self):
        for stderr in ("ModuleNotFoundError: No module named 'OCP'",
                       "ImportError: libnative.so: cannot open shared object file"):
            with self.subTest(stderr=stderr), self.assertRaisesRegex(WorkbenchError, '运行时依赖不可用'):
                self.run_result(stderr=stderr)
        for stderr in ('OCP triangulation failed',
                       'source annotation: No module named OCP',
                       'CAD note: ImportError: text in a drawing'):
            with self.subTest(stderr=stderr), self.assertRaisesRegex(WorkbenchError, '退出码 1'):
                self.run_result(stderr=stderr)

    def test_signal_exit_is_not_claimed_to_be_oom_or_missing_engine(self):
        with self.assertRaises(WorkbenchError) as caught:
            self.run_result(code=-9)
        self.assertIn('SIGKILL', str(caught.exception))
        self.assertIn('信号 9', str(caught.exception))
        self.assertNotIn('内存', str(caught.exception))
        self.assertEqual(self.receipt()['returncode'], -9)

    def test_timeout_retains_partial_byte_streams_and_original_timeout_message(self):
        failure = subprocess.TimeoutExpired(['native'], 180,
                                            output=b'partial stdout', stderr='局部错误'.encode())
        with patch('cad_manufacturing.subprocess.run', side_effect=failure), \
                self.assertRaisesRegex(WorkbenchError, '读取超时'):
            self.service._extract(self.source, self.folder)
        receipt = self.receipt()
        self.assertTrue(receipt['timed_out'])
        self.assertIsNone(receipt['returncode'])
        self.assertEqual(receipt['stdout'], 'partial stdout')
        self.assertEqual(receipt['stderr'], '局部错误')

    def test_launch_error_has_distinct_diagnostic(self):
        with patch('cad_manufacturing.subprocess.run', side_effect=FileNotFoundError('native not found')), \
                self.assertRaisesRegex(WorkbenchError, '引擎无法启动'):
            self.service._extract(self.source, self.folder)
        self.assertTrue(self.receipt()['launch_failed'])

    def test_success_receipt_is_bounded_and_not_a_downloadable_artifact(self):
        stream = 'x' * (NATIVE_DIAGNOSTIC_CHARS + 100)
        self.run_result(code=0, stdout=stream, stderr='')
        receipt = self.receipt()
        self.assertEqual(len(receipt['stdout']), NATIVE_DIAGNOSTIC_CHARS)
        self.assertTrue(receipt['truncated']['stdout'])
        self.assertFalse(receipt['truncated']['stderr'])
        geometry = fixture_geometry('a' * 64)
        (self.folder / 'geometry.json').write_text(json.dumps(geometry))
        for solid in geometry['solids']:
            for key in ('thumbnail', 'step_file'):
                (self.folder / solid[key]).write_bytes(b'artifact')
        registered = self.service._registered(self.folder, geometry)
        self.assertNotIn('native-diagnostic.json', registered)

    def test_diagnostic_storage_failure_cannot_mask_native_error(self):
        with patch('cad_manufacturing.atomic_write', side_effect=OSError('disk unavailable')), \
                self.assertRaisesRegex(WorkbenchError, '源几何错误'):
            self.run_result(stderr=json.dumps({'error': '源几何错误'}))
        self.assertFalse((self.folder / 'native-diagnostic.json').exists())


class ExportValidationTests(unittest.TestCase):
    SHA = 'b' * 64

    def setUp(self):
        self.geometry = fixture_geometry(self.SHA)
        self.solid = self.geometry['solids'][0]
        self.receipt = {'solid_count': 1, 'volume_relative_delta': 1e-5,
                        'max_bound_delta_mm': 1e-5, 'volume_relative_limit': 1e-5,
                        'max_bound_delta_limit_mm': 1e-5, 'status': 'engineering_draft'}
        self.solid['export_validation'] = copy.deepcopy(self.receipt)

    def reject(self):
        self.solid['step_file'] = None
        self.solid['export_validation'] = {**self.receipt, 'status': 'rejected',
                                           'reason': '导出几何偏差超出范围。'}

    def test_legacy_geometry_without_export_receipt_remains_compatible(self):
        value = fixture_geometry(self.SHA)
        self.assertIs(validate_geometry(value, self.SHA), value)

    def test_success_at_fixed_threshold_and_rejection_with_unmeasured_values_are_valid(self):
        self.assertIs(validate_geometry(self.geometry, self.SHA), self.geometry)
        self.reject()
        self.solid['export_validation'].update(solid_count=None, volume_relative_delta=None,
                                               max_bound_delta_mm=None)
        validate_geometry(self.geometry, self.SHA)
        self.solid['export_validation'].update(solid_count=2, volume_relative_delta=.01,
                                               max_bound_delta_mm=.2)
        validate_geometry(self.geometry, self.SHA)

    def test_success_cannot_change_threshold_exceed_it_or_omit_measurement(self):
        for key, value in (('volume_relative_limit', .01), ('max_bound_delta_limit_mm', .01),
                           ('volume_relative_delta', 1.0001e-5), ('max_bound_delta_mm', 1.0001e-5),
                           ('volume_relative_delta', None), ('max_bound_delta_mm', None),
                           ('solid_count', 2), ('solid_count', True), ('solid_count', None)):
            with self.subTest(key=key, value=value):
                self.solid['export_validation'] = {**self.receipt, key: value}
                with self.assertRaises(RecordError):
                    validate_geometry(self.geometry, self.SHA)

    def test_receipt_and_download_file_must_agree(self):
        self.solid['step_file'] = None
        with self.assertRaises(RecordError):
            validate_geometry(self.geometry, self.SHA)
        self.reject()
        self.solid['step_file'] = 'S0001.step'
        with self.assertRaises(RecordError):
            validate_geometry(self.geometry, self.SHA)

    def test_rejected_requires_reason_and_finite_nonnegative_optional_measurements(self):
        self.reject()
        rejected = copy.deepcopy(self.solid['export_validation'])
        for key, value in (('reason', ''), ('reason', ' '), ('reason', None),
                           ('volume_relative_delta', float('nan')),
                           ('max_bound_delta_mm', float('inf')),
                           ('volume_relative_delta', -.01), ('max_bound_delta_mm', True),
                           ('solid_count', -1), ('solid_count', 1.0)):
            with self.subTest(key=key, value=value):
                self.solid['export_validation'] = {**rejected, key: value}
                with self.assertRaises(RecordError):
                    validate_geometry(self.geometry, self.SHA)
        self.solid['export_validation'] = {**rejected, 'max_bound_delta_limit_mm': .1}
        with self.assertRaises(RecordError):
            validate_geometry(self.geometry, self.SHA)


if __name__ == '__main__':
    unittest.main()
