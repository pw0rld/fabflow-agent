"""Engineering recognition recovery and provenance without CAD or model calls."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_engineering_bom import build_engineering_bom
from batch_engineering_recognition import recognition_fields
from batch_quote_costing import aggregate_display_groups
from batch_quote_service import BatchQuoteService
from workbench_store import WorkbenchError
from test_batch_engineering_native import body
from test_batch_quote_service import SETTINGS, STEP, zipped
import test_batch_quote_http as http


def native_record():
    return {'lines': {'L0001': {'values': {'thickness_mm': '1', 'material_grade': 'SS304',
            'specification': '约 100 × 50 × 1 mm（成品包络；非展开）'},
            'field_status': {'thickness_mm': 'candidate', 'material_grade': 'candidate', 'specification': 'candidate'},
            'evidence': {'thickness_mm': {'basis': 'native_geometry', 'reason': '成对平面'},
                         'material_grade': {'basis': 'cad_property', 'reason': '明确赋值'}}}}}


def ai_record(field, value=None, state='candidate'):
    return {'lines': {'L0001': {'values': {field: value} if value is not None else {},
            'field_states': {field: state}, 'evidence': {field: [{'reference': 'F0002', 'page': 1,
            'quote': str(value), 'region': '合成图纸备注', 'basis': 'ai_document', 'status': 'pending_review'}]},
            'notes': []}}}


def fixture_geometry(row, content):
    return {'schema_version': 1, 'output_unit': 'mm',
            'source_sha256': hashlib.sha256(content).hexdigest(), 'solids': [body(1)]}


def fake_ai(folder, lines, sources, native, rpc, *, on_progress=None):
    if on_progress:
        on_progress('合成 AI 读取中')
    return {'lines': {row['id']: {'values': {'surface_treatment': '喷粉'},
            'field_states': {'surface_treatment': 'candidate'}, 'evidence': {'surface_treatment': [{
                'reference': sources[0]['id'], 'page': 1, 'quote': '喷粉', 'region': '合成备注',
                'basis': 'ai_document', 'status': 'pending_review'}]}, 'notes': []} for row in lines},
            'coverage': [{'reference': row['id'], 'page': 1, 'status': 'read', 'note': '合成已读'} for row in sources],
            'warnings': [], 'provenance': {'kind': 'fixture', 'status': 'completed'}}


class RecognitionMergeTests(unittest.TestCase):
    def test_missing_drawing_note_does_not_hide_incomplete_native_analysis(self):
        native = native_record()
        row = native['lines']['L0001']
        row['values']['thickness_mm'] = None
        row['field_status']['thickness_mm'] = 'unresolved'
        row['evidence']['thickness_mm']['reason'] = '板厚几何分析已达到本轮预算'
        field = recognition_fields(self.result, {'native': native, 'ai': ai_record('thickness_mm', state='not_stated')})['L0001']['fields']['thickness_mm']
        self.assertEqual(field['status'], 'unresolved')
        self.assertEqual(field['source'], 'native_geometry')
        self.assertEqual(field['label'], '板厚分析未完成')
        self.assertIn('预算', field['reason'])

    def setUp(self):
        self.result = {'lines': [{'id': 'L0001', 'part_name': 'Panel', 'quantity_per_set': 1, 'quantity_total': 10}],
                       'settings': {'order_sets': 10}, 'totals': {'total_price': '123.45'}}

    def test_drawing_specification_does_not_remove_native_finished_envelope(self):
        record = {'native': native_record(), 'ai': ai_record('specification', 'R 1-1/2 螺纹')}
        field = recognition_fields(self.result, record)['L0001']['fields']['specification']
        self.assertIn('100 × 50 × 1', field['value'])
        self.assertIn('R 1-1/2', field['value'])
        self.assertEqual(field['source'], 'native_and_drawing')
        self.assertIn('native', field['evidence'])
        self.assertIn('drawing', field['evidence'])

    def test_conflicting_material_or_thickness_does_not_choose_a_source(self):
        saved = copy.deepcopy(self.result)
        for field, value in [('material_grade', 'SS316'), ('thickness_mm', '2')]:
            with self.subTest(field=field):
                record = {'native': native_record(), 'ai': ai_record(field, value)}
                original = copy.deepcopy(record)
                actual = recognition_fields(self.result, record)['L0001']['fields'][field]
                self.assertIsNone(actual['value'])
                self.assertEqual(actual['status'], 'conflict')
                self.assertEqual(actual['source'], 'multiple_sources')
                self.assertIn('native', actual['evidence'])
                self.assertIn('drawing', actual['evidence'])
                self.assertEqual(record, original)
        self.assertEqual(self.result, saved)

    def test_equivalent_thickness_and_unstated_ai_preserve_real_native_candidates(self):
        record = {'native': native_record(), 'ai': ai_record('thickness_mm', '1.000')}
        actual = recognition_fields(self.result, record)['L0001']['fields']
        self.assertEqual(actual['thickness_mm']['value'], '1.000')
        self.assertEqual(actual['thickness_mm']['status'], 'candidate')
        self.assertEqual(actual['thickness_mm']['source'], 'ai_document')
        record['ai'] = ai_record('thickness_mm', state='not_stated')
        actual = recognition_fields(self.result, record)['L0001']['fields']
        self.assertEqual(actual['thickness_mm']['value'], '1')
        self.assertEqual(actual['thickness_mm']['source'], 'native_geometry')

    def test_absent_and_conflicting_document_fields_remain_distinct_states(self):
        for state, label in [('not_stated', '原文件未标注'), ('unreadable', '标注不清'), ('conflict', '信息冲突')]:
            with self.subTest(state=state):
                record = {'native': native_record(), 'ai': ai_record('surface_treatment', state=state)}
                actual = recognition_fields(self.result, record)['L0001']['fields']['surface_treatment']
                self.assertIsNone(actual['value'])
                self.assertEqual(actual['status'], state)
                self.assertEqual(actual['label'], label)

    def test_manual_clear_and_manual_value_override_recognition_without_repricing(self):
        record = {'native': native_record(), 'ai': ai_record('surface_treatment', '喷粉')}
        saved = copy.deepcopy(self.result)
        group = {**self.result['lines'][0], 'member_ids': ['L0001']}
        for value in (None, '镀锌'):
            with self.subTest(value=value):
                review = {'lines': {'L0001': {'surface_treatment': value, 'thickness_mm': None}}}
                actual = build_engineering_bom(self.result, [group], review=review, recognition=record)['lines'][0]
                self.assertEqual(actual['surface_treatment'], value)
                self.assertIsNone(actual['thickness_mm'])
                self.assertEqual(actual['field_sources']['surface_treatment'], 'manual')
                self.assertEqual(actual['field_sources']['thickness_mm'], 'manual')
                self.assertEqual(actual['quantity_total'], 10)
                self.assertEqual(actual['status'], 'pending_review')
        self.assertEqual(self.result, saved)


class RecognitionJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyl-engineering-recognition-')
        self.root = Path(self.temporary.name) / 'batches'
        self.geometries, self.native_calls, self.services = {}, [], []

        def processor(row, content):
            self.native_calls.append(row['id'])
            geometry = fixture_geometry(row, content)
            self.geometries[row['id']] = copy.deepcopy(geometry)
            return geometry

        self.processor = processor
        self.service = self.new_service()

    def new_service(self):
        service = BatchQuoteService(self.root, None, None, processor=self.processor)
        service.engineering_recognition.geometry_loader = lambda bid, state, result: copy.deepcopy(self.geometries)
        service.engineering_recognition.ai_reader = fake_ai
        self.services.append(service)
        return service

    def tearDown(self):
        for service in self.services:
            service.close()
        self.temporary.cleanup()

    def ready(self, images=True):
        files = [('panel.stp', STEP)]
        if images:
            files.append(('process.png', b'\x89PNG\r\n\x1a\nsynthetic engineering note'))
        state = self.service.create('engineering.zip', zipped(files), 'engineering-recognition-upload')
        self.service.start(state['id'], {'expected_revision': state['revision'], 'request_id': 'engineering-quote-start',
                                        'settings': copy.deepcopy(SETTINGS)})
        self.assertTrue(self.service.process_pending_once())
        state = self.service.get(state['id'])
        self.assertIn(state['status'], {'completed', 'completed_with_issues'})
        self.bid, self.rid = state['id'], state['result']['run_id']
        return state

    def original_bytes(self):
        folder = self.service._folder(self.bid)
        return {'state': (folder / 'state.json').read_bytes(),
                'snapshot': (folder / 'runs' / (self.rid + '.json')).read_bytes(),
                **{name: self.service.artifact(self.bid, self.rid, name)[0] for name in ('quote.json', 'bom.csv', 'cost.csv')}}

    def test_new_quote_preserves_native_thickness_bounds_and_inline_candidates(self):
        state = self.ready()
        folder = self.service._folder(self.bid)
        processed = json.loads((folder / 'processed/F0001.json').read_text())['geometry']['solids'][0]
        self.assertEqual(processed['thickness']['value_mm'], 1)
        self.assertEqual(processed['bounds_mm']['size'], [100, 50, 1])
        native = state['result']['engineering_native']['lines']['L0001']
        self.assertEqual(native['values']['thickness_mm'], '1')
        self.assertEqual(native['values']['material_grade'], 'SS304')
        self.assertIn('非展开尺寸', native['values']['specification'])
        self.assertEqual(self.service.engineering(self.bid, self.rid)['manifest']['lines'][0]['thickness_mm'], '1')

    def test_queued_job_replays_same_event_and_survives_service_reconstruction(self):
        self.ready()
        first = self.service.recognize_engineering(self.bid, self.rid, {'event_id': 'recognition-once'})
        self.assertEqual(first['recognition']['status'], 'queued')
        second = self.service.recognize_engineering(self.bid, self.rid, {'event_id': 'recognition-once'})
        third = self.service.recognize_engineering(self.bid, self.rid, {'event_id': 'active-repeat'})
        self.assertEqual(first['recognition']['id'], second['recognition']['id'])
        self.assertEqual(first['recognition']['id'], third['recognition']['id'])
        resumed = self.new_service()
        self.assertTrue(resumed.engineering_recognition.process_pending_once())
        completed = resumed.engineering(self.bid, self.rid)
        self.assertEqual(completed['recognition']['id'], first['recognition']['id'])
        self.assertEqual(completed['recognition']['status'], 'completed')
        self.assertEqual(completed['manifest']['lines'][0]['surface_treatment'], '喷粉')
        self.assertFalse(resumed.engineering_recognition.process_pending_once())
        replay = resumed.recognize_engineering(self.bid, self.rid, {'event_id': 'recognition-once'})
        self.assertEqual(replay['recognition']['status'], 'completed')
        self.assertEqual(replay['recognition']['id'], first['recognition']['id'])

    def test_running_job_recovers_same_job_and_existing_workspace(self):
        self.ready()
        queued = self.service.recognize_engineering(self.bid, self.rid, {'event_id': 'recognition-recover'})
        identity = queued['recognition']['id']
        record_path = self.service.engineering_recognition._path(self.bid, self.rid)
        record = json.loads(record_path.read_text())
        record.update(status='running', stage='reading', native=native_record())
        record_path.write_text(json.dumps(record))
        resumed = self.new_service()
        folders = []

        def resumed_reader(folder, *args, **kwargs):
            folders.append(Path(folder))
            return fake_ai(folder, *args, **kwargs)

        resumed.engineering_recognition.ai_reader = resumed_reader
        self.assertTrue(resumed.engineering_recognition.process_pending_once())
        completed = resumed.engineering(self.bid, self.rid)
        self.assertEqual(completed['recognition']['id'], identity)
        self.assertEqual(completed['recognition']['status'], 'completed')
        self.assertEqual(folders[0].name, identity)
        self.assertEqual(len(folders), 1)

    def test_native_is_saved_before_ai_and_success_preserves_financial_sources(self):
        self.ready()
        before, native_calls, observed = self.original_bytes(), list(self.native_calls), []

        def observe_reader(folder, lines, sources, native, rpc, **kwargs):
            current = self.service.engineering(self.bid, self.rid)
            observed.append(current['recognition']['status'])
            self.assertEqual(current['manifest']['lines'][0]['thickness_mm'], '1')
            self.assertEqual(current['recognition']['status'], 'running')
            self.assertTrue(Path(sources[0]['path']).is_file())
            self.assertEqual(hashlib.sha256(Path(sources[0]['path']).read_bytes()).hexdigest(), sources[0]['sha256'])
            return fake_ai(folder, lines, sources, native, rpc, **kwargs)

        self.service.engineering_recognition.ai_reader = observe_reader
        self.service.recognize_engineering(self.bid, self.rid, {'event_id': 'recognition-success'})
        self.assertTrue(self.service.engineering_recognition.process_pending_once())
        completed = self.service.engineering(self.bid, self.rid)
        self.assertEqual(observed, ['running'])
        self.assertEqual(completed['recognition']['status'], 'completed')
        self.assertEqual(completed['manifest']['lines'][0]['surface_treatment'], '喷粉')
        self.assertEqual(completed['manifest']['lines'][0]['field_sources']['surface_treatment'], 'ai_document')
        self.assertEqual(completed['manifest']['lines'][0]['status'], 'pending_review')
        self.assertEqual(self.original_bytes(), before)
        self.assertEqual(self.native_calls, native_calls)

    def test_ai_failure_keeps_native_candidates_and_every_financial_export(self):
        self.ready()
        before, native_calls = self.original_bytes(), list(self.native_calls)

        def failed_reader(*args, **kwargs):
            current = self.service.engineering(self.bid, self.rid)
            self.assertEqual(current['manifest']['lines'][0]['thickness_mm'], '1')
            raise ValueError('/private/model-workspace/provider-token: 合成图纸暂时无法辨认')

        self.service.engineering_recognition.ai_reader = failed_reader
        self.service.recognize_engineering(self.bid, self.rid, {'event_id': 'recognition-failure'})
        self.assertTrue(self.service.engineering_recognition.process_pending_once())
        failed = self.service.engineering(self.bid, self.rid)
        self.assertEqual(failed['recognition']['status'], 'failed')
        self.assertTrue(failed['recognition']['partial'])
        self.assertEqual(failed['manifest']['lines'][0]['thickness_mm'], '1')
        self.assertIsNone(failed['manifest']['lines'][0]['surface_treatment'])
        self.assertNotIn('_diagnostic', failed['recognition'])
        self.assertNotIn('/private/model-workspace', json.dumps(failed, ensure_ascii=False))
        self.assertNotIn('provider-token', json.dumps(failed, ensure_ascii=False))
        self.assertEqual(self.original_bytes(), before)
        self.assertEqual(self.native_calls, native_calls)

    def test_no_reference_images_complete_native_only_without_ai_call(self):
        self.ready(images=False)
        self.service.engineering_recognition.ai_reader = lambda *args, **kwargs: self.fail('AI must not run without reference images')
        self.service.recognize_engineering(self.bid, self.rid, {'event_id': 'native-only'})
        self.assertTrue(self.service.engineering_recognition.process_pending_once())
        completed = self.service.engineering(self.bid, self.rid)
        self.assertEqual(completed['recognition']['status'], 'completed')
        self.assertTrue(completed['recognition']['partial'])
        self.assertEqual(completed['manifest']['lines'][0]['thickness_mm'], '1')
        self.assertIsNone(completed['manifest']['lines'][0]['surface_treatment'])

    def test_recognition_record_cannot_attach_to_changed_quote_snapshot(self):
        self.ready()
        self.service.recognize_engineering(self.bid, self.rid, {'event_id': 'source-bound'})
        record_path = self.service.engineering_recognition._path(self.bid, self.rid)
        record = json.loads(record_path.read_text())
        record['source_digest'] = 'b' * 64
        record_path.write_text(json.dumps(record))
        with self.assertRaises(WorkbenchError) as error:
            self.service.engineering(self.bid, self.rid)
        self.assertEqual(error.exception.status, 409)


class RecognitionHTTPTests(unittest.TestCase):
    setUp = http.BatchQuoteHTTPTests.setUp
    tearDown = http.BatchQuoteHTTPTests.tearDown
    request = http.BatchQuoteHTTPTests.request
    upload = http.BatchQuoteHTTPTests.upload
    finish = http.BatchQuoteHTTPTests.finish

    def test_actual_http_returns_202_enforces_marker_and_exposes_durable_progress(self):
        self.server.batch_quotes.processor = fixture_geometry
        self.server.batch_quotes.engineering_recognition.ai_reader = fake_ai
        batch = self.upload()[1]
        self.request(self.api + '/' + batch['id'] + '/start', {'expected_revision': batch['revision'],
            'request_id': 'recognition-http-quote', 'settings': http.SETTINGS})
        ready = self.finish(batch['id'])
        prefix = self.api + '/' + batch['id'] + '/runs/' + ready['result']['run_id']
        forbidden = self.request(prefix + '/engineering-recognize', {'event_id': 'http-no-marker'}, marker=False)
        self.assertEqual(forbidden[0], 403)
        code, queued, _ = self.request(prefix + '/engineering-recognize', {'event_id': 'http-recognize'})
        self.assertEqual(code, 202, queued)
        identity = queued['recognition']['id']
        self.assertTrue(identity.startswith('ENG-'))
        until = time.monotonic() + 8
        current = queued
        while current['recognition']['status'] in {'queued', 'running'} and time.monotonic() < until:
            time.sleep(.03)
            status, current, _ = self.request(prefix + '/engineering')
            self.assertEqual(status, 200)
        self.assertEqual(current['recognition']['status'], 'completed', current)
        self.assertEqual(current['recognition']['id'], identity)
        self.assertEqual(current['manifest']['lines'][0]['thickness_mm'], '1')
        self.assertEqual(current['manifest']['lines'][0]['surface_treatment'], '喷粉')
        self.assertEqual(self.request(prefix + '/engineering-recognize', {'event_id': 'http-recognize'})[1]['recognition']['id'], identity)
        self.assertEqual(self.request(self.api + '/' + batch['id'])[1]['result']['totals'], ready['result']['totals'])


if __name__ == '__main__':
    unittest.main()
