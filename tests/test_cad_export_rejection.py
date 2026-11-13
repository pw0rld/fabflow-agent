"""Derivative export rejection must not discard successfully read source geometry.

Run only on ZeroClave. The small protocol checks do not require OCCT; the native
check deliberately corrupts one real derivative and verifies the intact source.
"""
from contextlib import nullcontext
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import cad_geometry as geometry


class ExportRejectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyl-export-rejection-')
        self.output = Path(self.temporary.name)
        self.item = {
            'id': 'S0001', 'name': 'original solid', 'step_file': 'S0001.step',
            'volume_mm3': 100.0,
            'bounds_mm': {'min': [0, 0, 0], 'max': [10, 10, 1], 'size': [10, 10, 1]},
            'mesh': {'positions': [0, 0, 0, 10, 0, 0, 0, 10, 1], 'indices': [0, 1, 2]},
            'thickness': {'status': 'candidate', 'value_mm': 1},
            'material': {'status': 'unresolved', 'value': None},
        }

    def tearDown(self):
        self.temporary.cleanup()

    def deps(self, *, volume=100.0, bounds=None, write_error=None, count=1, valid=True, closed=True):
        bounds = bounds or [0, 0, 0, 10, 10, 1]

        class Writer:
            def Transfer(self, shape, mode):
                return 'done'

            def Write(self, filename):
                if isinstance(write_error, OSError):
                    raise write_error
                Path(filename).write_bytes(b'ISO-10303-21;\nHEADER;\nENDSEC;')
                if write_error is not None:
                    raise write_error
                return 'done'

        class Reader:
            def ReadFile(self, filename):
                return 'done'

            def SetSystemLengthUnit(self, unit):
                pass

            def TransferRoots(self):
                return 1

            def OneShape(self):
                return 'derived shape'

        class Box:
            def GetXMin(self): return bounds[0]
            def GetYMin(self): return bounds[1]
            def GetZMin(self): return bounds[2]
            def GetXMax(self): return bounds[3]
            def GetYMax(self): return bounds[4]
            def GetZMax(self): return bounds[5]

        deps = {
            'STEPControl_Writer': Writer, 'STEPControl_Reader': Reader,
            'STEPControl_AsIs': 'as-is', 'IFSelect_RetDone': 'done',
            'TopAbs': SimpleNamespace(TopAbs_SOLID='solid', TopAbs_SHELL='shell'),
            'BRepCheck_Analyzer': lambda shape: SimpleNamespace(IsValid=lambda: valid),
            'BRep_Tool': SimpleNamespace(IsClosed_s=lambda shell: closed),
            'GProp_GProps': lambda: SimpleNamespace(Mass=lambda: volume),
            'BRepGProp': SimpleNamespace(VolumeProperties_s=lambda *args: None),
            'Bnd_Box': Box, 'BRepBndLib': SimpleNamespace(AddOptimal_s=lambda *args: None),
        }
        return deps, lambda shape, kind: SimpleNamespace(Extent=lambda: count if kind == 'solid' else 1,
                                                        FindKey=lambda index: 'fixture shell')

    def export(self, **options):
        deps, shape_map = self.deps(**options)
        with patch.object(geometry, 'quiet_native', nullcontext):
            return geometry._export_solid_step('source shape', self.item, self.output, deps, shape_map)

    def test_volume_over_original_limit_rejects_file_and_preserves_source_fields(self):
        before = copy.deepcopy(self.item)
        result = self.export(volume=100.00110605035)
        self.assertEqual(result['status'], 'rejected')
        self.assertGreater(result['volume_relative_delta'], 1e-5)
        self.assertEqual(result['volume_relative_limit'], 1e-5)
        self.assertEqual(result['max_bound_delta_limit_mm'], 1e-5)
        self.assertEqual(result['max_bound_delta_mm'], 0)
        self.assertIn('几何偏差', result['reason'])
        self.assertIsNone(self.item['step_file'])
        self.assertFalse((self.output / 'S0001.step').exists())
        for key, value in before.items():
            if key != 'step_file':
                self.assertEqual(self.item[key], value)

    def test_bound_over_original_limit_rejects_even_when_volume_matches(self):
        result = self.export(bounds=[0, 0, 0, 10.00002, 10, 1])
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['volume_relative_delta'], 0)
        self.assertGreater(result['max_bound_delta_mm'], 1e-5)
        self.assertIsNone(self.item['step_file'])
        self.assertFalse((self.output / 'S0001.step').exists())

    def test_native_export_exception_deletes_partial_derivative_with_bounded_reason(self):
        result = self.export(write_error=RuntimeError('native failure\n' + 'x' * 1000))
        self.assertEqual(result['status'], 'rejected')
        self.assertLessEqual(len(result['reason']), 600)
        self.assertNotIn('\n', result['reason'])
        self.assertIsNone(result['solid_count'])
        self.assertIsNone(result['volume_relative_delta'])
        self.assertIsNone(self.item['step_file'])
        self.assertFalse((self.output / 'S0001.step').exists())

    def test_filesystem_error_is_not_mislabeled_as_local_export_rejection(self):
        with self.assertRaises(PermissionError):
            self.export(write_error=PermissionError('test output is not writable'))
        self.assertNotIn('export_validation', self.item)
        self.assertFalse((self.output / 'S0001.step').exists())

    def test_nonfinite_measurement_is_rejected_without_nonfinite_json(self):
        for options in ({'volume': float('nan')}, {'bounds': [0, 0, 0, float('inf'), 10, 1]}):
            with self.subTest(options=options):
                result = self.export(**options)
                self.assertEqual(result['status'], 'rejected')
                json.dumps(result, allow_nan=False)
                self.assertIsNone(self.item['step_file'])
                self.assertFalse((self.output / 'S0001.step').exists())

    def test_non_single_derivative_does_not_discard_original(self):
        result = self.export(count=2)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['solid_count'], 2)
        self.assertIsNone(result['volume_relative_delta'])
        self.assertIn('单一实体', result['reason'])
        self.assertIsNone(self.item['step_file'])

    def test_invalid_or_open_derivative_rejects_even_with_matching_scalars(self):
        for options in ({'valid': False}, {'closed': False}):
            with self.subTest(options=options):
                result = self.export(**options)
                self.assertEqual(result['status'], 'rejected')
                self.assertIsNone(self.item['step_file'])
                self.assertFalse((self.output / 'S0001.step').exists())

    def test_accepted_derivative_keeps_existing_success_contract(self):
        result = self.export(volume=100.0005)
        self.assertEqual(result['status'], 'engineering_draft')
        self.assertEqual(set(result), {'status', 'solid_count', 'volume_relative_delta',
                                     'max_bound_delta_mm', 'volume_relative_limit',
                                     'max_bound_delta_limit_mm'})
        self.assertLess(result['volume_relative_delta'], 1e-5)
        self.assertEqual(self.item['step_file'], 'S0001.step')
        self.assertTrue((self.output / self.item['step_file']).is_file())


@unittest.skipUnless(importlib.util.find_spec('OCP') is not None, 'Requires server native CAD runtime')
class NativeExportIsolationTests(unittest.TestCase):
    def test_one_corrupt_real_derivative_retains_all_original_solids_and_later_exports(self):
        self._check_corruption(corrupt_subset=True)

    def test_corrupt_rewrite_recovers_only_strictly_verified_original_entity_subset(self):
        self._check_corruption(corrupt_subset=False)

    def _check_corruption(self, *, corrupt_subset):
        from OCP.BRep import BRep_Builder
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.TopoDS import TopoDS_Compound
        from OCP.gp import gp_Pnt

        with tempfile.TemporaryDirectory(prefix='hyl-native-export-isolation-') as temporary:
            folder = Path(temporary)
            source = folder / 'two-plates.step'
            deps = geometry._imports()
            def geometry_map(shape,kind):
                result=deps['IndexedMap_TopoDS_Shape']()
                deps['TopExp'].MapShapes_s(shape,kind,result)
                return result
            compound = TopoDS_Compound()
            builder = BRep_Builder()
            builder.MakeCompound(compound)
            builder.Add(compound, BRepPrimAPI_MakeBox(10, 20, 1).Shape())
            builder.Add(compound, BRepPrimAPI_MakeBox(gp_Pnt(30, 0, 0), 4, 6, 1).Shape())
            writer = deps['STEPControl_Writer']()
            with geometry.quiet_native():
                self.assertEqual(writer.Transfer(compound, deps['STEPControl_AsIs']), deps['IFSelect_RetDone'])
                self.assertEqual(writer.Write(str(source)), deps['IFSelect_RetDone'])
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            baseline = geometry.extract_geometry(source, folder / 'baseline')
            self.assertEqual(len(baseline['solids']), 2)

            real_writer = deps['STEPControl_Writer']
            writer_count = []

            class CorruptFirstWriter:
                def __init__(self):
                    self.writer = real_writer()
                    writer_count.append(1)
                    self.corrupt = len(writer_count) == 1

                def Transfer(self, shape, mode):
                    # Write a genuinely different BRep only to the first
                    # derivative. Source parsing and its measurements stay real.
                    if self.corrupt:
                        shape = BRepPrimAPI_MakeBox(100, 100, 100).Shape()
                    return self.writer.Transfer(shape, mode)

                def Write(self, filename):
                    return self.writer.Write(filename)

            isolated_deps = {**deps, 'STEPControl_Writer': CorruptFirstWriter}
            output = folder / 'isolated'
            original_subset=geometry.SourceStepSubset.write_matching
            def corrupt_fallback(instance, filename, validate):
                def validate_corrupted(candidate):
                    # Both export strategies are deliberately damaged in the
                    # rejection case; a correct original subset is a legitimate
                    # recovery and has its own strict positive case below.
                    bad=real_writer()
                    with geometry.quiet_native():
                        self.assertEqual(bad.Transfer(BRepPrimAPI_MakeBox(100,100,100).Shape(),deps['STEPControl_AsIs']),deps['IFSelect_RetDone'])
                        self.assertEqual(bad.Write(str(candidate)),deps['IFSelect_RetDone'])
                    return validate(candidate)
                return original_subset(instance,filename,validate_corrupted)
            subset_patch=(patch.object(geometry.SourceStepSubset,'write_matching',corrupt_fallback)
                          if corrupt_subset else nullcontext())
            with patch.object(geometry, '_imports', return_value=isolated_deps), \
                    patch.object(geometry, 'export_laser_cut', wraps=geometry.export_laser_cut) as laser, \
                    patch.object(geometry, 'recognize_bends', wraps=geometry.recognize_bends) as bends, subset_patch:
                result = geometry.extract_geometry(source, output)

            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_hash)
            self.assertEqual(result['source_sha256'], baseline['source_sha256'])
            self.assertEqual(len(result['solids']), 2)
            self.assertEqual(len(writer_count), 2)
            self.assertEqual(laser.call_count, 2)
            self.assertEqual(bends.call_count, 2)
            first, second = result['solids']
            if corrupt_subset:
                self.assertEqual(first['export_validation']['status'], 'rejected')
                self.assertGreater(first['export_validation']['volume_relative_delta'], 1e-5)
                self.assertIsNone(first['step_file'])
                self.assertFalse((output / (first['id'] + '.step')).exists())
                self.assertTrue(any(first['id'] in warning and '禁止下载' in warning
                                    for warning in result['warnings']))
            else:
                self.assertEqual(first['export_validation']['status'],'engineering_draft')
                self.assertEqual(first['export_validation']['extra_provenance']['method'],'source_step_entity_subset')
                self.assertFalse(first['export_validation']['extra_provenance']['geometry_rebuilt'])
                self.assertLessEqual(first['export_validation']['volume_relative_delta'],1e-5)
                self.assertLessEqual(first['export_validation']['max_bound_delta_mm'],1e-5)
                verified={}
                geometry._verify_solid_step(output/first['step_file'],first,deps,geometry_map,verified,strict=True,
                    source_fingerprints=geometry._solid_face_fingerprints(laser.call_args_list[0].args[0],deps,geometry_map))
            self.assertEqual(second['export_validation']['status'], 'engineering_draft')
            self.assertTrue((output / second['step_file']).is_file())
            for actual, original, laser_call, bend_call in zip(
                    result['solids'], baseline['solids'], laser.call_args_list, bends.call_args_list):
                for key in ('id', 'name', 'definition_id', 'occurrence_path', 'topology',
                            'bounds_mm', 'area_mm2', 'volume_mm3', 'mesh', 'thickness', 'material'):
                    self.assertEqual(actual[key], original[key], key)
                self.assertEqual(actual['laser_cut']['status'], original['laser_cut']['status'])
                self.assertEqual(actual['bend_features']['status'], original['bend_features']['status'])
                for native_shape in (laser_call.args[0], bend_call.args[0]):
                    props = deps['GProp_GProps']()
                    deps['BRepGProp'].VolumeProperties_s(native_shape, props, True, False, False)
                    self.assertAlmostEqual(abs(props.Mass()), original['volume_mm3'], places=6)
                self.assertTrue((output / actual['thumbnail']).is_file())
            self.assertEqual(json.loads((output / 'geometry.json').read_text()), result)


if __name__ == '__main__':
    unittest.main()
