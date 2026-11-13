"""Real loopback HTTP contract for order endpoints; no remote/model traffic."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from prep_record import digest
from workbench_server import make_server
from workbench_store import Workbench

POLICY = {'pricing_method': 'cost_plus', 'rate_percent': '20', 'tax_excluded': True, 'freight_excluded': True}
DXF = b'0\nSECTION\n2\nENTITIES\n0\nLINE\n10\n0\n20\n0\n11\n10\n21\n10\n0\nENDSEC\n0\nEOF\n'


def part(key, mode='buy', **extra):
    return {'id': key, 'name': key, 'part_number': key, 'version': 'A', 'mode': mode,
            'document_ids': [], 'purchase_price': '3', 'cost_confirmed': True,
            'cost_inclusions': '本地测试采购报价，未税不含运费。', **extra}


class OrderHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='order-http-offline-')
        self.app = Workbench(Path(self.tmp.name) / 'tasks')
        self.server = make_server(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.api = '/workbench/api/orders'

    def tearDown(self):
        self.server.orders.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, path, data=None, *, headers=None, marker=True):
        request_headers = {'Origin': self.base}
        if marker:
            request_headers['X-Workbench-Request'] = '1'
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False).encode()
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        try:
            response = urlopen(Request(self.base + path, data=data, headers=request_headers), timeout=5)
        except HTTPError as error:
            response = error
        with response:
            content = response.read()
            response_headers = dict(response.headers)
            parsed = json.loads(content) if 'application/json' in response.headers.get('Content-Type', '') else content
            return response.status, parsed, response_headers

    def create(self, event='create'):
        status, body, _ = self.request(self.api, {'event_id': event, 'name': 'HTTP 离线订单', 'inquiry': '', 'simulated': True})
        self.assertEqual(status, 201)
        return body

    def structure(self, order, **values):
        payload = {'event_id': 'structure-' + str(order['revision']), 'expected_revision': order['revision'], **values}
        status, body, _ = self.request(self.api + '/' + order['id'] + '/structure', payload)
        self.assertEqual(status, 200, body)
        return body

    def test_create_list_get_direct_shape_and_same_event(self):
        order = self.create()
        self.assertTrue(order['id'].startswith('ORD-'))
        self.assertEqual(order['revision'], 1)
        self.assertNotIn('order', order)
        self.assertEqual(order['documents'], [])
        self.assertEqual(order['parts'], [])
        self.assertEqual(order['quotes'], [])
        self.assertEqual(self.create()['id'], order['id'])
        status, listing, _ = self.request(self.api)
        self.assertEqual(status, 200)
        self.assertEqual([row['id'] for row in listing['orders']], [order['id']])
        status, current, headers = self.request(self.api + '/' + order['id'])
        self.assertEqual(status, 200)
        self.assertEqual(current, order)
        self.assertEqual(headers['Cache-Control'], 'private, no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_post_requires_marker_and_matching_origin_for_json_and_raw(self):
        payload = {'event_id': 'forbidden', 'name': '不应创建', 'inquiry': '', 'simulated': True}
        for headers, marker in [({}, False), ({'Origin': 'https://elsewhere.invalid'}, True),
                                 ({'Sec-Fetch-Site': 'cross-site'}, True)]:
            status, body, _ = self.request(self.api, payload, headers=headers, marker=marker)
            self.assertEqual(status, 403)
            self.assertEqual(body['error']['code'], 'invalid_origin')
        self.assertEqual(self.server.orders.store.list(), [])
        order = self.create()
        path = self.api + '/' + order['id'] + '/documents?name=part.dxf'
        status, _, _ = self.request(path, DXF, marker=False, headers={'Content-Type': 'application/octet-stream',
                                      'X-Expected-Revision': '1', 'X-Event-Id': 'no-marker'})
        self.assertEqual(status, 403)
        self.assertEqual(self.server.orders.store.get(order['id'])['documents'], [])

    def test_raw_document_roundtrip_hash_replay_and_cross_order_isolation(self):
        order, other = self.create(), self.create('other')
        path = self.api + '/' + order['id'] + '/documents?name=' + quote('支架.dxf')
        headers = {'Content-Type': 'application/octet-stream', 'X-Expected-Revision': '1', 'X-Event-Id': 'upload'}
        status, uploaded, _ = self.request(path, DXF, headers=headers)
        self.assertEqual(status, 200)
        document = uploaded['documents'][0]
        self.assertEqual(document['sha256'], hashlib.sha256(DXF).hexdigest())
        self.assertNotIn('path', document)
        status, repeated, _ = self.request(path, DXF, headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(repeated['revision'], uploaded['revision'])
        self.assertEqual(len(repeated['documents']), 1)
        status, content, response_headers = self.request(document['url'])
        self.assertEqual(status, 200)
        self.assertEqual(content, DXF)
        self.assertEqual(response_headers['Content-Type'], 'application/octet-stream')
        self.assertTrue(response_headers['Content-Disposition'].startswith('attachment;'))
        self.assertIn("filename*=UTF-8''", response_headers['Content-Disposition'])
        status, _, _ = self.request(self.api + '/' + other['id'] + '/documents/' + document['id'])
        self.assertEqual(status, 404)
        _, original = self.server.orders._file(order['id'], document['id'])
        original.write_bytes(DXF + b'changed')
        status, error, _ = self.request(document['url'])
        self.assertEqual(status, 400)
        self.assertIn('校验不一致', error['error']['message'])

    def test_structure_cycle_returns_400_stale_revision_409_and_retry_safe(self):
        order = self.create()
        invalid = {'event_id': 'cycle', 'expected_revision': 1,
                   'parts': [part('A', 'assembly'), part('B', 'assembly')],
                   'bom': [{'id': 'E1', 'parent_id': 'A', 'part_id': 'B', 'quantity': '1', 'unit': 'piece'},
                           {'id': 'E2', 'parent_id': 'B', 'part_id': 'A', 'quantity': '1', 'unit': 'piece'}]}
        status, error, _ = self.request(self.api + '/' + order['id'] + '/structure', invalid)
        self.assertEqual(status, 400)
        self.assertIn('循环', error['error']['message'])
        self.assertEqual(self.server.orders.store.get(order['id'])['revision'], 1)
        current = self.structure(order, parts=[part('BUY')], lines=[{'id': 'L', 'part_id': 'BUY', 'quantity': 10, 'unit': 'piece'}])
        stale = {'event_id': 'stale', 'expected_revision': 1, 'lines': []}
        status, error, _ = self.request(self.api + '/' + order['id'] + '/structure', stale)
        self.assertEqual(status, 409)
        self.assertEqual(error['error']['code'], 'revision_conflict')
        replay = {'event_id': 'structure-1', 'expected_revision': 1, 'parts': [part('BUY')],
                  'lines': [{'id': 'L', 'part_id': 'BUY', 'quantity': 10, 'unit': 'piece'}]}
        status, again, _ = self.request(self.api + '/' + order['id'] + '/structure', replay)
        self.assertEqual(status, 200)
        self.assertEqual(again['revision'], current['revision'])

    def test_jobs_response_does_not_expose_leases_sessions_or_payload_paths(self):
        order = self.create()
        payload = {'path': '/private/test-secret-workspace', 'credentials': 'NOT-A-REAL-KEY'}
        job = self.server.orders.queue.enqueue(order['id'], 'discover', digest(payload), payload, 'job')
        claimed = self.server.orders.queue.claim('offline-worker', lease_seconds=120)
        self.server.orders.queue.heartbeat(job['id'], claimed['lease_token'], 120,
                                           session_id='session-private', progress={'stage': 'testing', 'label': '离线测试'})
        status, body, _ = self.request(self.api + '/' + order['id'] + '/jobs')
        self.assertEqual(status, 200)
        self.assertEqual(body['jobs'][0]['id'], job['id'])
        self.assertEqual(body['jobs'][0]['status'], 'running')
        for key in ('payload', 'lease_token', 'lease_until', 'session_id', 'worker_id'):
            self.assertNotIn(key, body['jobs'][0])
        text = json.dumps(body)
        self.assertNotIn('test-secret-workspace', text)
        self.assertNotIn('session-private', text)
        other = self.create('other')
        status, _, _ = self.request(self.api + '/' + other['id'] + '/jobs/' + job['id'] + '/stop', {})
        self.assertEqual(status, 400)
        status, stopped, _ = self.request(self.api + '/' + order['id'] + '/jobs/' + job['id'] + '/stop', {})
        self.assertEqual(status, 202)
        self.assertTrue(stopped['jobs'][0]['stop_requested'])

    def test_buy_quote_and_real_html_markdown_csv_responses_and_readonly_gets(self):
        order = self.structure(self.create(), parts=[part('BUY')],
                               lines=[{'id': 'L', 'part_id': 'BUY', 'quantity': 10, 'unit': 'piece'}])
        before = copy.deepcopy(self.server.orders.store.get(order['id']))
        for suffix in ['', '/jobs', '/quotes', '/exports/bom.csv']:
            self.request(self.api + '/' + order['id'] + suffix)
        self.assertEqual(self.server.orders.store.get(order['id']), before)
        self.assertEqual(before['quotes'], [])
        payload = {'event_id': 'quote-once', 'expected_revision': order['revision'], 'policy': POLICY,
                   'shared_fees': [{'id': 'F', 'name': '共享准备', 'value': '5', 'inclusions': '本订单计一次。'}]}
        quote_path = self.api + '/' + order['id'] + '/quotes'
        status, issued, _ = self.request(quote_path, payload)
        self.assertEqual(status, 201, issued)
        self.assertEqual(issued['amounts']['total_cost'], '35.00')
        self.assertEqual(issued['amounts']['total_price'], '42.00')
        self.assertTrue(issued['source_current'])
        status, retried, _ = self.request(quote_path, payload)
        self.assertEqual(status, 201)
        self.assertEqual(retried['quote_id'], issued['quote_id'])
        frozen = copy.deepcopy(self.server.orders.store.get(order['id']))
        base = quote_path + '/' + issued['quote_id']
        for filename, mime, disposition in [('quote.html', 'text/html', 'inline'), ('cost.html', 'text/html', 'inline'),
                                              ('quote.md', 'text/markdown', 'attachment'), ('cost.md', 'text/markdown', 'attachment'),
                                              ('bom.csv', 'text/csv', 'attachment'), ('routes.csv', 'text/csv', 'attachment')]:
            with self.subTest(filename=filename):
                status, content, headers = self.request(base + '/' + filename)
                self.assertEqual(status, 200)
                self.assertTrue(headers['Content-Type'].startswith(mime))
                self.assertTrue(headers['Content-Disposition'].startswith(disposition + ';'))
                self.assertIn("object-src 'none'", headers['Content-Security-Policy'])
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
                self.assertTrue(content)
                if filename.startswith('quote'):
                    self.assertIn('42.00', content.decode())
                    self.assertNotIn('内部成本明细', content.decode())
        status, quote_view, _ = self.request(base)
        self.assertEqual(status, 200)
        self.assertEqual(quote_view['quote_id'], issued['quote_id'])
        self.assertNotIn('record_snapshots', quote_view)
        self.assertEqual(self.server.orders.store.get(order['id']), frozen)
        other = self.create('other')
        status, _, _ = self.request(self.api + '/' + other['id'] + '/quotes/' + issued['quote_id'])
        self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
