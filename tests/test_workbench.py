"""Integration checks against real records/calculator; no model/server calls."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from prep_record import RecordError, atomic_write
from workbench_store import Workbench, WorkbenchError
from workbench_server import make_server
from price_catalog import PriceCatalog


REQUIREMENTS = {'product_name': '测试支架', 'quantity': 10, 'material_family': '碳钢',
                'material_grade': 'Q235B', 'thickness': '3', 'surface_treatment': '镀锌'}
TECHNICAL = {'material_usage_per_piece': '0.4', 'material_rate': '20',
             'process_route': ['切割', '折弯', '镀锌', '包装'],
             'operation_rates': [{'operation': name, 'value': value, 'unit': 'CNY/piece'}
                                 for name, value in [('切割', '12'), ('折弯', '18'), ('镀锌', '6'), ('包装', '4')]],
             'setup_fee': '200', 'fee_inclusions': ['工序费用已含人工设备耗材；准备费独立收取。']}
POLICY = {'pricing_method': 'cost_plus', 'rate_percent': '20', 'tax_excluded': True, 'freight_excluded': True}


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'tasks'
        self.app = Workbench(self.root)
        self.task = self.app.create({'event_id': 'create-test', 'inquiry': '十件测试支架，仅本地模拟。', 'simulated': True})
        self.id = self.task['id']

    def tearDown(self):
        self.tmp.cleanup()

    def save(self, stage, values, event_id):
        self.task = self.app.save_fields(self.id, stage, {'event_id': event_id,
                'expected_revision': self.task['revision'], 'values': copy.deepcopy(values)})
        return self.task

    def ready(self):
        self.save('requirements', REQUIREMENTS, 'requirements')
        self.save('technical', TECHNICAL, 'technical')
        self.assertTrue(self.task['validation']['ready'])

    def issue(self, event='quote', policy=None):
        return self.app.issue(self.id, {'event_id': event, 'expected_revision': self.task['revision'],
                                      'policy': copy.deepcopy(policy or POLICY)})

    def test_new_inquiry_has_no_values_and_create_is_idempotent(self):
        self.assertTrue(all(x['value'] is None for x in self.task['fields'].values()))
        same = self.app.create({'event_id': 'create-test', 'inquiry': '十件测试支架，仅本地模拟。', 'simulated': True})
        self.assertEqual(same['id'], self.id)
        self.assertNotIn('last_policy', self.app._state(self.id))

    def test_real_calculator_cost_plus_and_gross_margin(self):
        self.ready()
        first = self.issue()
        self.assertEqual(first['amounts']['unit_price'], '81.60')
        self.assertEqual(first['amounts']['total_price'], '816.00')
        margin = dict(POLICY, pricing_method='gross_margin')
        second = self.issue('margin', margin)
        self.assertEqual(second['amounts']['unit_price'], '85.00')
        self.assertEqual(second['amounts']['total_price'], '850.00')
        self.assertTrue(first['links']['quote_url'].startswith('/quote-preview/'))

    def test_missing_policy_unknown_fees_and_unsupported_unit_never_become_zero(self):
        self.save('requirements', REQUIREMENTS, 'requirements')
        self.save('technical', {'process_route': ['折弯']}, 'partial')
        self.assertIsNone(self.task['fields']['material_rate']['value'])
        with self.assertRaises(RecordError):
            self.issue()
        self.save('technical', TECHNICAL, 'complete')
        with self.assertRaises(RecordError):
            self.issue('blank', dict(POLICY, rate_percent=''))
        unsupported = copy.deepcopy(TECHNICAL['operation_rates'])
        unsupported[0]['unit'] = 'CNY/hour'
        self.save('technical', {'operation_rates': unsupported}, 'hour-rate')
        with self.assertRaises(RecordError):
            self.issue('hour-quote')
        self.assertFalse((self.root / self.id / 'quotes' / 'Q-hour-quote').exists())

    def test_save_and_quote_retry_idempotency_and_stale_revisions(self):
        payload = {'event_id': 'requirements', 'expected_revision': 0, 'values': REQUIREMENTS}
        first = self.app.save_fields(self.id, 'requirements', payload)
        second = self.app.save_fields(self.id, 'requirements', payload)
        self.assertEqual(first['revision'], second['revision'])
        with self.assertRaises(WorkbenchError):
            self.app.save_fields(self.id, 'requirements', dict(payload, values={'quantity': 20}))
        with self.assertRaises(WorkbenchError):
            self.app.save_fields(self.id, 'requirements', dict(payload, event_id='stale'))
        self.task = second
        self.save('technical', TECHNICAL, 'technical')
        quote_payload = {'event_id': 'q-once', 'expected_revision': self.task['revision'], 'policy': POLICY}
        q1 = self.app.issue(self.id, quote_payload)
        self.save('requirements', {'quantity': 20}, 'new-quantity')
        q2 = self.app.issue(self.id, quote_payload)
        self.assertEqual(q1['quote_id'], q2['quote_id'])
        self.assertEqual(q2['amounts']['total_price'], '816.00')
        self.assertFalse(q2['source_current'])
        self.assertEqual(len(self.app.task(self.id)['quotes']), 1)

    def test_original_submission_and_immutable_quote_survive_parameter_change(self):
        self.ready()
        quote = self.issue()
        path = self.root / self.id / 'quotes' / quote['quote_id']
        before = {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in path.iterdir()}
        self.save('requirements', {'quantity': 12}, 'quantity-change')
        current = self.app._quote_result(self.id, quote['quote_id'])
        self.assertFalse(current['source_current'])
        self.assertEqual(current['quantity'], 10)
        self.assertEqual(before, {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in path.iterdir()})
        source = json.loads((self.root / self.id / 'form-requirements.json').read_text())
        self.assertEqual(source['submission']['values'], REQUIREMENTS)

    def test_retry_recovers_committed_record_before_ui_receipt(self):
        payload = {'event_id': 'interrupted', 'expected_revision': 0, 'values': {'quantity': 10}}
        self.task = self.app.save_fields(self.id, 'requirements', payload)
        state = self.app._state(self.id)
        state['events']['interrupted']['status'] = 'pending'
        self.app._save_state(self.id, state)
        result = self.app.save_fields(self.id, 'requirements', payload)
        self.assertEqual(result['revision'], 1)
        self.assertEqual(len(self.app.records.load(self.id)['events']), 1)

    def test_multiple_images_do_not_determine_quantity_and_upload_retry(self):
        content = b'\x89PNG\r\n\x1a\nmock-image'
        self.task = self.app.upload(self.id, 'file-one', 0, '图纸.png', content)
        retry = self.app.upload(self.id, 'file-one', 0, '图纸.png', content)
        self.assertEqual(retry['revision'], 1)
        self.task = self.app.upload(self.id, 'file-two', 1, '图纸2.png', content)
        self.assertEqual(len(self.task['attachments']), 2)
        self.assertIsNone(self.task['fields']['quantity']['value'])
        with self.assertRaises(RecordError):
            self.app.upload(self.id, 'escape', 2, '../evil.png', content)
        with self.assertRaises(RecordError):
            self.app.upload(self.id, 'html', 2, 'html.png', b'<html>')

    def test_reading_candidates_remain_unverified_and_cannot_overwrite_edits(self):
        self.task = self.app.upload(self.id, 'file-one', 0, 'image.png', b'\x89PNG\r\n\x1a\nmock')
        source_id = self.task['attachments'][0]['id']
        result = {'candidates': [{'name': 'quantity', 'value': 10, 'raw_value': 10,
                                  'unit': 'piece', 'raw_unit': '件',
                                  'evidence': {'reference': source_id, 'page': 1, 'region': '标题栏'}}]}
        run = {'id': 'run-one', 'base_revision': 1}
        self.app._apply_reading(self.id, run, result)
        task = self.app.task(self.id)
        self.assertEqual(task['fields']['quantity']['status'], 'pending_review')
        self.assertFalse(task['validation']['ready'])
        self.task = task
        self.save('requirements', {'quantity': 20}, 'human-edit')
        another = {'id': 'old-run', 'base_revision': 1}
        self.app._apply_reading(self.id, another, result)
        self.assertIn('未覆盖', another['warnings'][0])
        self.assertEqual(self.app.task(self.id)['fields']['quantity']['value'], 20)

    def test_matched_price_requires_actual_adoption_and_snapshot_survives_catalog_change(self):
        self.app.catalog = PriceCatalog(Path(self.tmp.name) / 'prices.sqlite3')
        price = {'kind': 'material', 'name': '本地模拟材料价', 'rate': '20', 'unit': 'CNY/kg',
                 'material_grade': 'Q235B', 'material_form': '板材', 'thickness_min': '2', 'thickness_max': '4',
                 'valid_from': '2020-01-01', 'valid_to': '2099-12-31', 'currency': 'CNY',
                 'tax_included': False, 'freight_included': False, 'source': '离线集成测试明确提供的模拟费率',
                 'simulated': True, 'inclusions': ['材料价未计税运费'], 'qty_min': 1, 'qty_max': 99}
        draft = self.app.catalog.create(price, request_id='new-price')
        published = self.app.catalog.publish(draft['entry_id'], draft['version'], request_id='publish-price')
        self.save('requirements', REQUIREMENTS, 'requirements')
        self.task = self.app.save_fields(self.id, 'technical', {
            'event_id': 'context', 'expected_revision': self.task['revision'], 'values': {'process_route': TECHNICAL['process_route']},
            'pricing_context': {'material_form': '板材', 'product_family': '测试支架'}})
        matches = self.app.match_prices(self.id)
        material = next(x for x in matches['results'] if x['kind'] == 'material')
        self.assertEqual(material['status'], 'unique')
        self.assertIsNone(self.app.task(self.id)['fields']['material_rate']['value'])
        self.task = self.app.save_fields(self.id, 'technical', {
            'event_id': 'adopt-price', 'expected_revision': self.task['revision'], 'values': TECHNICAL,
            'price_selections': {'material_rate': {'entry_id': published['entry_id'], 'version': published['version']}}})
        first = self.issue()
        source = json.loads((self.root / self.id / 'form-adopt-price.json').read_text())
        self.assertEqual(source['adopted_prices']['material_rate']['rate'], '20')
        newer = self.app.catalog.new_version(published['entry_id'], dict(price, rate='40'), request_id='new-version')
        self.app.catalog.publish(newer['entry_id'], newer['version'], request_id='publish-new')
        after = self.app._quote_result(self.id, first['quote_id'])
        self.assertEqual(after['amounts']['total_price'], '816.00')
        self.assertEqual(source['adopted_prices']['material_rate']['version'], 1)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Workbench(Path(self.tmp.name) / 'tasks')
        self.server = make_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, data=None, headers=None):
        headers = dict(headers or {})
        if data is not None:
            data = json.dumps(data).encode()
            headers.setdefault('Content-Type', 'application/json')
        try:
            response = urlopen(Request(self.base + path, data=data, headers=headers), timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_csrf_boundary_and_structured_errors(self):
        body = {'event_id': 'request', 'inquiry': 'local only', 'simulated': True}
        status, _ = self.request('/workbench/api/tasks', body)
        self.assertEqual(status, 403)
        status, response = self.request('/workbench/api/tasks', body, {'X-Workbench-Request': '1'})
        self.assertEqual(status, 201)
        self.assertEqual(response['revision'], 0)
        status, _ = self.request('/workbench/api/tasks', body,
                                 {'X-Workbench-Request': '1', 'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)
        status, response = self.request('/workbench/api/tasks/../../etc/passwd')
        self.assertIn(status, {400, 404})
        self.assertIn('message', response['error'])

    def test_authenticated_same_origin_embedding_has_its_own_readonly_route(self):
        task = self.app.create({'event_id': 'embed-create', 'inquiry': 'local embed test', 'simulated': True})
        task = self.app.save_fields(task['id'], 'requirements', {'event_id': 'embed-req', 'expected_revision': 0, 'values': REQUIREMENTS})
        task = self.app.save_fields(task['id'], 'technical', {'event_id': 'embed-tech', 'expected_revision': task['revision'], 'values': TECHNICAL})
        result = self.app.issue(task['id'], {'event_id': 'embed-quote', 'expected_revision': task['revision'], 'policy': POLICY})
        with urlopen(self.base + result['links']['quote_embed_url'], timeout=5) as response:
            self.assertEqual(response.headers['X-Frame-Options'], 'SAMEORIGIN')
            self.assertIn("frame-ancestors 'self'", response.headers['Content-Security-Policy'])
            body = response.read().decode()
            self.assertIn('816.00', body)
            self.assertIn('target="_blank"', body)
        self.assertTrue(result['links']['quote_url'].startswith('/quote-preview/'))


if __name__ == '__main__':
    unittest.main()
