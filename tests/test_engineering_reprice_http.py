"""Source-bound engineering repricing over real HTTP, using scalar fixtures only.

Run on ZeroClave. No model, CAD engine or frontend build is involved.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from prep_record import atomic_write, digest
from workbench_store import Workbench
from workbench_server import make_server
import test_batch_quote_http as http
from test_batch_material_prices import adopted_row, market_settings


def geometry(row, content):
    return {'schema_version': 1, 'output_unit': 'mm', 'source_sha256': row['sha256'],
            'solids': [{'id': 'S000' + str(index), 'name': name, 'definition_id': name,
                        'occurrence_path': ['root', name], 'volume_mm3': 100000 + index,
                        'bounds_mm': {'min': [0, 0, 0], 'max': [100, 100, 10], 'size': [100, 100, 10]},
                        'topology': {'valid': True, 'closed': True},
                        'material': {'status': 'not_found', 'value': None}}
                       for index, name in enumerate(('Pipe', 'Mesh', 'Zinc'), 1)]}


def observed(field, value, quote):
    return {'values': {field: value}, 'field_states': {field: 'candidate'},
            'evidence': {field: [{'reference': 'F0002', 'page': 1, 'quote': quote,
                                 'region': '合成工程备注', 'basis': 'ai_document', 'status': 'pending_review'}]},
            'notes': []}


class EngineeringRepriceHTTPTests(unittest.TestCase):
    request = http.BatchQuoteHTTPTests.request
    upload = http.BatchQuoteHTTPTests.upload

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyl-engineering-pricing-')
        self.app = Workbench(Path(self.temporary.name) / 'tasks')
        self.server = make_server(self.app, 0)
        self.service = self.server.batch_quotes
        self.native_calls = []
        def processor(row, content):
            self.native_calls.append(row['id'])
            return geometry(row, content)
        self.service.processor = processor
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.api = '/workbench/api/quote-batches'
        status, state, _ = self.upload()
        self.assertEqual(status, 201, state)
        self.batch_id = state['id']
        settings = market_settings({'carbon_steel': adopted_row(),
                                    'ss316': adopted_row('29', profile='ss316')}, order_sets=10)
        self.service.start(self.batch_id, {'request_id': 'start-fixture',
                                         'expected_revision': state['revision'], 'settings': settings})
        self.assertTrue(self.service.process_pending_once())
        self.state = self.service.get(self.batch_id)
        self.run_id = self.state['result']['run_id']
        self.prefix = self.api + '/' + self.batch_id + '/runs/' + self.run_id
        self.folder = self.service._folder(self.batch_id)
        self.original = json.loads((self.folder / 'runs' / (self.run_id + '.json')).read_text())
        record = {'id': 'ENG-' + 'a' * 32, 'run_id': self.run_id, 'source_digest': digest(self.original),
                  'status': 'completed', 'stage': 'completed', 'updated_at': '2026-11-09T00:00:00Z',
                  'native': self.original.get('engineering_native'), 'partial': False,
                  'ai': {'lines': {
                      'L0001': observed('material_grade', '316不锈钢', '此圆管焊接固定/不能漏水漏油/316不锈钢'),
                      'L0002': observed('specification', '8目不锈钢纱网', '焊接8目不锈钢纱网'),
                      'L0003': observed('surface_treatment', '热侵锌处理', '热侵锌处理')},
                      'provenance': {'kind': 'fixture', 'status': 'completed'}}, '_events': ['old-recognition']}
        atomic_write(self.folder / 'runs' / (self.run_id + '-engineering-recognition.json'), json.dumps(record))
        # Annotations are copied as pending metadata, not as approval history.
        self.service.save_engineering(self.batch_id, self.run_id, {'event_id': 'header-one',
            'expected_revision': 0, 'review': {'header': {'product_name': '工程核价合成订单'}}})
        self.service.save_engineering(self.batch_id, self.run_id, {'event_id': 'header-two',
            'expected_revision': 1, 'review': {'header': {'basis_notes': '10 套；成本加成20%；模拟价'}}})

    def tearDown(self):
        self.service.close()
        self.server.orders.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def plan(self):
        status, response, _ = self.request(self.prefix + '/engineering-pricing')
        self.assertEqual(status, 200, response)
        return response

    def payload(self, event='reprice-one'):
        plan = self.plan()
        return {'event_id': event, 'expected_revision': plan['expected_revision'],
                'policy': copy.deepcopy(plan['plan']['policy'])}

    def tree(self):
        return {str(path.relative_to(self.folder)): path.read_bytes()
                for path in self.folder.rglob('*') if path.is_file()}

    def post(self, payload, **kwargs):
        return self.request(self.prefix + '/engineering-pricing', payload, **kwargs)

    def test_plan_is_read_only_and_contains_exact_five_bound_requirements(self):
        before = self.tree()
        with (patch.object(self.service, '_native', side_effect=AssertionError('CAD must not run')),
              patch.object(self.service.engineering_recognition, 'enqueue', side_effect=AssertionError('AI must not run'))):
            plan = self.plan()
        self.assertEqual(self.tree(), before)
        self.assertEqual(plan['expected_revision'], self.state['revision'])
        self.assertEqual(plan['review_revision'], 2)
        self.assertEqual(plan['source_digest'], digest(self.original))
        self.assertEqual(plan['recognition_id'], 'ENG-' + 'a' * 32)
        self.assertEqual(len(plan['plan']['policy']['entries']), 5)
        self.assertEqual(plan['plan']['policy']['source_binding'], plan['source_binding'])
        self.assertTrue(plan['can_reprice'])
        self.assertEqual(self.native_calls, ['F0001'])

    def test_new_run_prices_requirements_rebases_metadata_and_preserves_original_exports(self):
        filenames = ('quote.json', 'bom.csv', 'cost.csv', 'quote-print.html')
        before_files = {name: content for name, content in self.tree().items() if name != 'state.json'}
        exports = {name: self.service.artifact(self.batch_id, self.run_id, name)[0] for name in filenames}
        payload = self.payload()
        with (patch.object(self.service, '_native', side_effect=AssertionError('CAD must not run')),
              patch.object(self.service.engineering_recognition, 'enqueue', side_effect=AssertionError('AI must not run'))):
            status, response, _ = self.post(payload)
        self.assertEqual(status, 201, response)
        new = response['batch']['result']
        self.assertNotEqual(response['run_id'], self.run_id)
        self.assertEqual(new['run_id'], response['run_id'])
        self.assertEqual(new['settings'], self.original['settings'])
        self.assertEqual(new['source_files'], self.original['source_files'])
        identity_fields = ('id', 'file_id', 'native_definition', 'native_solid_count',
                           'quantity_per_set', 'quantity_total', 'volume_mm3')
        self.assertEqual([{key: line.get(key) for key in identity_fields} for line in new['lines']],
                         [{key: line.get(key) for key in identity_fields} for line in self.original['lines']])
        self.assertEqual(new['lines'][0]['material_profile'], 'ss316')
        self.assertEqual(new['lines'][1]['material_profile'], 'stainless_mesh')
        self.assertEqual(new['lines'][1]['mode'], 'buy')
        native = geometry(self.original['source_files'][0], b'')
        for old_line, new_line in zip(self.original['lines'], new['lines']):
            self.assertEqual(self.service._preview_match(old_line, native, self.original['lines']),
                             self.service._preview_match(new_line, native, new['lines']))
        self.assertNotEqual(new['totals']['total_price'], self.original['totals']['total_price'])
        self.assertEqual(new['status'], 'pending_review')
        self.assertTrue(new['simulated'])
        self.assertEqual(len(response['batch']['runs']), 2)
        self.assertEqual(response['batch']['revision'], self.state['revision'] + 1)
        self.assertEqual(new['engineering_repricing']['source_quote_digest'], digest(self.original))
        for name, content in before_files.items():
            self.assertEqual((self.folder / name).read_bytes(), content, name)
        for name, content in exports.items():
            self.assertEqual(self.service.artifact(self.batch_id, self.run_id, name)[0], content, name)
        saved = json.loads((self.folder / 'runs' / (response['run_id'] + '.json')).read_text())
        review = json.loads((self.folder / 'runs' / (response['run_id'] + '-engineering.json')).read_text())
        recognized = json.loads((self.folder / 'runs' / (response['run_id'] + '-engineering-recognition.json')).read_text())
        self.assertEqual(review['source_digest'], digest(saved))
        self.assertEqual(recognized['source_digest'], digest(saved))
        self.assertEqual(review['header']['product_name'], '工程核价合成订单')
        self.assertEqual((review['revision'], review['history'], review['_events']), (0, [], {}))
        self.assertEqual(recognized['_events'], [])
        self.assertEqual(recognized['parent']['source_run_id'], self.run_id)
        self.assertEqual(recognized['ai']['provenance']['kind'], 'fixture')
        new_prefix = self.api + '/' + self.batch_id + '/runs/' + response['run_id']
        self.assertEqual(self.request(new_prefix + '/engineering')[0], 200)
        replay_status, replay, _ = self.post(payload)
        self.assertEqual(replay_status, 201, replay)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['run_id'], response['run_id'])
        self.assertEqual(len(replay['batch']['runs']), 2)
        self.assertEqual(replay['batch']['revision'], response['batch']['revision'])
        self.assertEqual(self.native_calls, ['F0001'])

    def test_conflicts_origin_and_numeric_validation_never_write_a_run(self):
        payload = self.payload()
        self.assertEqual(self.post(payload, marker=False)[0], 403)
        self.assertEqual(self.post(payload, headers={'Origin': 'https://other.invalid'})[0], 403)
        invalid = copy.deepcopy(payload)
        invalid['policy']['entries'][-1]['rate'] = 'NaN'
        self.assertEqual(self.post(invalid)[0], 400)
        wrong_revision = copy.deepcopy(payload)
        wrong_revision['expected_revision'] -= 1
        self.assertEqual(self.post(wrong_revision)[0], 409)
        self.service.save_engineering(self.batch_id, self.run_id, {'event_id': 'newer-review',
            'expected_revision': 2, 'review': {'header': {'product_name': '更新后'}}})
        self.assertEqual(self.post(payload)[0], 409)
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 1)
        current = self.payload()
        status, done, _ = self.post(current)
        self.assertEqual(status, 201, done)
        altered = copy.deepcopy(current)
        altered['policy']['entries'][-1]['rate'] = '4.00'
        self.assertEqual(self.post(altered)[0], 409)
        other_event = copy.deepcopy(current)
        other_event.update(event_id='other-stale-event', expected_revision=done['batch']['revision'])
        self.assertEqual(self.post(other_event)[0], 409)
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 2)

    def test_recognition_update_and_active_work_are_fenced(self):
        payload = self.payload()
        path = self.folder / 'runs' / (self.run_id + '-engineering-recognition.json')
        record = json.loads(path.read_text())
        record['id'] = 'ENG-' + 'b' * 32
        atomic_write(path, json.dumps(record))
        self.assertEqual(self.post(payload)[0], 409)
        current = self.payload()
        record['status'] = 'running'
        atomic_write(path, json.dumps(record))
        self.assertEqual(self.post(current)[0], 409)
        record['status'] = 'completed'
        atomic_write(path, json.dumps(record))
        current = self.payload()
        with self.service._lock(self.batch_id):
            state = self.service._read(self.batch_id)
            state['status'] = 'queued'
            self.service._save(state)
        self.assertEqual(self.post(current)[0], 409)
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 1)

    def test_prepared_run_recovers_once_after_state_commit_interruption(self):
        payload = self.payload()
        real_save = self.service._save
        def interrupted(state):
            if len(state['runs']) == 2:
                raise OSError('simulated interrupted state commit')
            return real_save(state)
        with patch.object(self.service, '_save', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.service.price_engineering(self.batch_id, self.run_id, payload)
        current = self.service.get(self.batch_id)
        self.assertEqual(len(current['runs']), 1)
        directory = self.folder / 'engineering-pricing'
        journal = next(directory.iterdir())
        prepared = json.loads(journal.read_text())
        self.assertEqual(prepared['status'], 'prepared')
        allocated = prepared['run_id']
        self.assertTrue((self.folder / 'runs' / (allocated + '.json')).exists())
        # Restart recovery finishes the already allocated run without AI/CAD.
        with (patch.object(self.service, '_native', side_effect=AssertionError('CAD must not run')),
              patch.object(self.service.engineering_recognition, 'enqueue', side_effect=AssertionError('AI must not run'))):
            self.service._recover()
        self.assertEqual(json.loads(journal.read_text())['status'], 'committed')
        status, recovered, _ = self.post(payload)
        self.assertEqual(status, 201, recovered)
        self.assertEqual(recovered['run_id'], allocated)
        self.assertEqual(len(recovered['batch']['runs']), 2)
        self.assertEqual(self.native_calls, ['F0001'])

    def test_direct_retry_completes_partial_snapshot_and_rejects_changed_event_body(self):
        payload = self.payload()
        original_writer = self.service._write_pricing_snapshot
        def interrupted(path, value):
            if path.name.endswith('-engineering.json'):
                raise OSError('simulated interrupted metadata write')
            return original_writer(path, value)
        with patch.object(self.service, '_write_pricing_snapshot', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.service.price_engineering(self.batch_id, self.run_id, payload)
        journal = next((self.folder / 'engineering-pricing').iterdir())
        allocated = json.loads(journal.read_text())['run_id']
        altered = copy.deepcopy(payload)
        altered['policy']['entries'][-1]['rate'] = '9.00'
        self.assertEqual(self.post(altered)[0], 409)
        self.assertEqual(self.post({**payload, 'event_id': 'new-while-pending'})[0], 409)
        status, recovered, _ = self.post(payload)
        self.assertEqual(status, 201, recovered)
        self.assertTrue(recovered['replayed'])
        self.assertEqual(recovered['run_id'], allocated)
        self.assertEqual(len(recovered['batch']['runs']), 2)

    def test_lost_receipt_after_committed_pointer_does_not_increment_revision_twice(self):
        payload = self.payload()
        def interrupted(path, content):
            if path.parent.name == 'engineering-pricing' and json.loads(content)['status'] == 'committed':
                raise OSError('simulated lost committed receipt')
            return atomic_write(path, content)
        with patch('batch_quote_service.atomic_write', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.service.price_engineering(self.batch_id, self.run_id, payload)
        committed = self.service.get(self.batch_id)
        self.assertEqual(len(committed['runs']), 2)
        status, replayed, _ = self.post(payload)
        self.assertEqual(status, 201, replayed)
        self.assertEqual(replayed['run_id'], committed['result']['run_id'])
        self.assertEqual(replayed['batch']['revision'], committed['revision'])
        self.assertEqual(len(replayed['batch']['runs']), 2)

    def test_current_pointer_must_match_immutable_source_snapshot(self):
        payload = self.payload()
        with self.service._lock(self.batch_id):
            state = self.service._read(self.batch_id)
            state['result']['totals']['total_price'] = '0.01'
            self.service._save(state)
        status, response, _ = self.post(payload)
        self.assertEqual(status, 409, response)
        self.assertEqual(response['error']['code'], 'source_conflict')
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 1)
        self.assertEqual(json.loads((self.folder / 'runs' / (self.run_id + '.json')).read_text()), self.original)


if __name__ == '__main__':
    unittest.main()
