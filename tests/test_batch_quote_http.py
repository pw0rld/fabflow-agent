"""Real HTTP tests for uploaded ZIP quotations; all geometry stays synthetic."""
import copy
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from workbench_store import Workbench
from workbench_server import make_server

SETTINGS = {'order_sets': 10, 'pricing_method': 'cost_plus', 'rate_percent': '20',
            'material_profile': 'carbon_steel', 'rate_profile': 'demo-v1',
            'demo_confirmed': True, 'scope_confirmed': True}


def package(rows=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in (rows or [('parts/plate.stp', b'ISO-10303-21;\nfixture plate'),
                                      ('refs/notes.png', b'\x89PNG\r\n\x1a\nfixture')]):
            archive.writestr(name, content)
    return output.getvalue()


def geometry(row, content):
    return {'schema_version': 1, 'output_unit': 'mm', 'source_sha256': row['sha256'], 'solids': [
        {'id': 'S0001', 'name': 'HTTP测试板', 'definition_id': 'plate', 'occurrence_path': ['root', 'a'],
         'volume_mm3': 1000000, 'topology': {'valid': True, 'closed': True},
         'material': {'status': 'not_found', 'value': None}},
    ]}


class BatchQuoteHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyl-batch-http-')
        self.app = Workbench(Path(self.temporary.name) / 'tasks')
        self.server = make_server(self.app, 0)
        self.calls = []
        self.fail_processing = False

        def processor(row, content):
            self.calls.append(row['id'])
            if self.fail_processing:
                raise ValueError('本文件本轮无法解析')
            return geometry(row, content)

        self.server.batch_quotes.processor = processor
        self.server.batch_quotes.start_worker()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.api = '/workbench/api/quote-batches'

    def tearDown(self):
        self.server.batch_quotes.close()
        self.server.orders.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(self, path, data=None, *, marker=True, headers=None):
        supplied = {'Origin': self.base}
        if marker:
            supplied['X-Workbench-Request'] = '1'
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            supplied['Content-Type'] = 'application/json'
        supplied.update(headers or {})
        try:
            response = urlopen(Request(self.base + path, data=data, headers=supplied), timeout=10)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            parsed = json.loads(body) if response.headers.get('Content-Type', '').startswith('application/json') else body
            return response.status, parsed, dict(response.headers)

    def upload(self, event='http-upload', content=None, name='订单资料.zip'):
        return self.request(self.api + '?name=' + quote(name), content or package(),
            headers={'Content-Type': 'application/octet-stream', 'X-Event-Id': event})

    def finish(self, batch_id):
        until = time.monotonic() + 10
        while time.monotonic() < until:
            status, batch, _ = self.request(self.api + '/' + batch_id)
            self.assertEqual(status, 200)
            if batch['status'] not in {'queued', 'running'}:
                return batch
            time.sleep(.05)
        self.fail('Batch worker did not finish its bounded synthetic run')

    def test_upload_inventory_is_idle_replay_and_origin_checks(self):
        status, batch, _ = self.upload()
        self.assertEqual(status, 201, batch)
        self.assertEqual(batch['status'], 'uploaded')
        self.assertEqual(len(batch['files']), 2)
        self.assertEqual(self.calls, [])
        self.assertIsNone(batch['result'])
        repeated = self.upload()[1]
        self.assertEqual(repeated['id'], batch['id'])
        listing = self.request(self.api)[1]['batches']
        self.assertEqual([row['id'] for row in listing], [batch['id']])
        _, changed, _ = self.upload(content=package([('other.stp', b'ISO-10303-21; other')]))
        self.assertIn('error', changed)
        forbidden = self.request(self.api + '?name=bad.zip', package(), marker=False,
            headers={'Content-Type': 'application/octet-stream', 'X-Event-Id': 'forbidden'})
        self.assertEqual(forbidden[0], 403)
        self.assertEqual(self.request(self.api + '?name=bad.zip', package(),
            headers={'Content-Type': 'application/octet-stream', 'X-Event-Id': 'forbidden',
                     'Origin': 'https://other.invalid'})[0], 403)

    def test_settings_validation_precedes_geometry_and_full_artifact_flow(self):
        batch = self.upload()[1]
        start = self.api + '/' + batch['id'] + '/start'
        invalid = copy.deepcopy(SETTINGS)
        invalid['demo_confirmed'] = False
        status, _, _ = self.request(start, {'expected_revision': batch['revision'], 'request_id': 'invalid', 'settings': invalid})
        self.assertEqual(status, 400)
        self.assertEqual(self.calls, [])
        payload = {'expected_revision': batch['revision'], 'request_id': 'http-start', 'settings': SETTINGS}
        status, started, _ = self.request(start, payload)
        self.assertEqual(status, 202, started)
        finished = self.finish(batch['id'])
        self.assertEqual(finished['status'], 'completed', finished)
        result = finished['result']
        self.assertEqual(result['settings']['order_sets'], 10)
        self.assertEqual(result['weight_summary']['estimated_total_net_weight_kg'], '78.5')
        self.assertEqual(result['status'], 'pending_review')
        self.assertTrue(result['simulated'])
        self.assertEqual(len(self.calls), 1)
        # A replay after completion cannot start another CAD operation.
        status, _, _ = self.request(start, payload)
        self.assertEqual(status, 202)
        self.assertEqual(len(self.calls), 1)
        for key, filename in [('quote_url', 'customer-quote.html'), ('bom_url', 'bom.csv'),
                              ('cost_url', 'cost.csv'), ('json_url', 'quote.json')]:
            url = result['links'][key]
            self.assertTrue(url.startswith(self.api + '/' + batch['id'] + '/runs/'))
            status, body, headers = self.request(url)
            self.assertEqual(status, 200, body)
            self.assertGreater(len(json.dumps(body)) if isinstance(body, dict) else len(body), 20)
            if filename == 'customer-quote.html':
                self.assertIn('data-customer-quote', body.decode())
                self.assertNotIn('成本汇总', body.decode())
                self.assertTrue(headers['Content-Disposition'].startswith('inline;'))
            else:
                self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
        ref = next(row for row in finished['files'] if row['kind'] == 'image')
        status, body, headers = self.request(self.api + '/' + batch['id'] + '/files/' + ref['id'])
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b'\x89PNG'))
        self.assertNotIn('/srv/', json.dumps(finished))
        self.assertNotIn('geometry', result['source_files'][0])

    def test_public_pricing_catalog_does_not_start_a_job(self):
        status, catalog, _ = self.request(self.api + '/pricing')
        self.assertEqual(status, 200, catalog)
        self.assertEqual(catalog['version'], 'foshan-reference-20261106')
        references = {row['id']: row for row in catalog['references']}
        self.assertEqual(references['FS-Q235B-HRC-20261103']['price_cny_per_kg'], '3.18')
        self.assertIs(references['FS-Q235B-HRC-20261103']['tax_included'], True)
        self.assertIsNone(references['FS-304-2B-20261105']['tax_included'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.request(self.api)[1]['batches'], [])

    def test_market_and_supplier_repricing_preserves_inputs_and_geometry(self):
        batch = self.upload()[1]
        api = self.api + '/' + batch['id']
        settings = copy.deepcopy(SETTINGS)
        settings['rate_profile'] = 'foshan-reference-20261106'
        status, _, _ = self.request(api + '/start', {'expected_revision': batch['revision'],
            'request_id': 'market-without-confirmation', 'settings': settings})
        self.assertEqual(status, 400)
        self.assertEqual(self.calls, [])
        settings['material_pricing'] = {'vat_rate_percent': '13', 'confirmed': True, 'rows': {
            'carbon_steel': {'rate': '3.18', 'tax_included': True, 'source_kind': 'market_reference',
                'reference_id': 'FS-Q235B-HRC-20261103', 'source_text': '', 'quote_date': '2026-11-03'}}}
        status, state, _ = self.request(api + '/start', {'expected_revision': batch['revision'],
            'request_id': 'market-confirmed', 'settings': settings})
        self.assertEqual(status, 202, state)
        state = self.finish(batch['id'])
        self.assertEqual(state['status'], 'completed', state)
        self.assertEqual(Decimal(state['result']['lines'][0]['material_rate']).quantize(Decimal('.000001')), Decimal('2.814159'))
        snapshot = state['result']['lines'][0]['material_price_snapshot']
        self.assertIn('FS-Q235B-HRC-20261103', json.dumps(snapshot))
        self.assertIn('2026-11-03', json.dumps(snapshot))
        old_url = state['result']['links']['json_url']
        old_result = self.request(old_url)[1]
        calls = list(self.calls)
        supplier = copy.deepcopy(settings)
        supplier['material_pricing']['rows']['carbon_steel'] = {
            'rate': '5', 'tax_included': False, 'source_kind': 'supplier_quote',
            'reference_id': None, 'source_text': '独立HTTP测试供应商价，并非工厂实价', 'quote_date': '2026-11-06'}
        status, state, _ = self.request(api + '/start', {'expected_revision': state['revision'],
            'request_id': 'supplier-reprice', 'settings': supplier})
        self.assertEqual(status, 202, state)
        state = self.finish(batch['id'])
        self.assertEqual(state['status'], 'completed', state)
        self.assertEqual(state['result']['lines'][0]['material_rate'], '5')
        self.assertNotEqual(state['result']['links']['json_url'], old_url)
        self.assertEqual(self.calls, calls, 'A price-only change must reuse the completed CAD geometry.')
        self.assertEqual(self.request(old_url)[1], old_result)
        self.assertEqual(len(state['runs']), 2)

    def test_failed_file_retry_retains_previous_run(self):
        batch = self.upload()[1]
        self.fail_processing = True
        status, started, _ = self.request(self.api + '/' + batch['id'] + '/start', {
            'expected_revision': batch['revision'], 'request_id': 'fail-start', 'settings': SETTINGS})
        self.assertEqual(status, 202)
        failed = self.finish(batch['id'])
        self.assertIsNone(failed['result']['totals']['total_price'])
        old_url = failed['result']['links']['json_url']
        old_json = self.request(old_url)[1]
        self.fail_processing = False
        file_id = next(row['id'] for row in failed['files'] if row['status'] == 'failed')
        status, state, _ = self.request(self.api + '/' + batch['id'] + '/retry', {
            'expected_revision': failed['revision'], 'request_id': 'retry-failed', 'file_ids': [file_id]})
        self.assertEqual(status, 202, state)
        ready = self.finish(batch['id'])
        self.assertEqual(ready['status'], 'completed', ready)
        self.assertNotEqual(ready['result']['links']['json_url'], old_url)
        self.assertEqual(self.request(old_url)[1], old_json)
        self.assertEqual(len(self.calls), 2)


if __name__ == '__main__':
    unittest.main()
