"""Customer upload authorization and complete HTTP flow; synthetic CAD only.

Execute on ZeroClave in an isolated build. These tests create temporary stores,
never use a production order, CAD engine, external model or supplier endpoint.
"""
import copy
import hashlib
from http.cookies import SimpleCookie
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workbench_store import Workbench
from workbench_server import make_server
from test_batch_quote_service import STEP, fixture, zipped
from test_batch_quote_archive import stored_rar4, stored_rar5


API = '/workbench/customer/api'
COOKIE = 'HYL_CUSTOMER_SESSION'
PRIVATE_KEYS = {'settings', 'result', 'runs', 'cost_sheet', 'cost_components',
                'total_cost', 'unit_cost', 'material_cost', 'process_cost',
                'purchase_cost', 'setup_cost', 'margin', 'markup', 'rate_percent',
                'material_rate', 'material_pricing', 'pricing_method',
                'source_sha256', '_customer_owner', '_events', '_job'}
PRIVATE_URLS = ('/workbench/api/quote-batches', '/ortho2cad-test/',
                'internal-cost', 'cost-sheet', 'quote.json', 'cost.csv', 'bom.csv')


def customer_geometry(row, content):
    """A source-bound analytic flat-part protocol fixture, not native proof."""
    from test_customer_process_http import process_geometry
    return process_geometry(hashlib.sha256(content).hexdigest())


class CustomerUploadHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyl-customer-http-')
        self.app = Workbench(Path(self.temporary.name) / 'tasks')
        self.server = make_server(self.app, 0)
        self.calls = []
        self.fail_processing = False

        def processor(row, content):
            self.calls.append(row['id'])
            if self.fail_processing:
                raise ValueError('/srv/private/customer-token internal CAD failure')
            return customer_geometry(row, content)

        self.server.customer_batches.processor = processor
        self.server.customer_batches.start_worker()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        for name in ('customer_batches', 'batch_quotes', 'case_quotes', 'orders'):
            getattr(self.server, name).close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(self, path, data=None, *, cookie=None, marker=True, headers=None):
        supplied = {'Origin': self.base}
        if marker:
            supplied['X-Workbench-Request'] = '1'
        if cookie:
            supplied['Cookie'] = cookie
        if isinstance(data, dict):
            supplied['Content-Type'] = 'application/json'
            data = json.dumps(data).encode()
        supplied.update(headers or {})
        try:
            response = urlopen(Request(self.base + path, data=data, headers=supplied), timeout=15)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            parsed = json.loads(raw) if response.headers.get('Content-Type', '').startswith('application/json') else raw
            return response.status, parsed, dict(response.headers)

    def session(self, existing=None):
        status, body, headers = self.request(API + '/session', {}, cookie=existing)
        self.assertEqual(status, 200, body)
        if 'Set-Cookie' not in headers:
            self.assertIsNotNone(existing, 'A new visitor must receive a customer session.')
            return existing, headers
        parsed = SimpleCookie()
        parsed.load(headers['Set-Cookie'])
        self.assertIn(COOKIE, parsed)
        return COOKIE + '=' + parsed[COOKIE].value, headers

    def upload(self, cookie, *, name='客户资料.step', content=None, event='upload-one'):
        return self.request(API + '/orders?name=' + quote(name),
            content if content is not None else STEP,
            cookie=cookie, headers={'Content-Type': 'application/octet-stream', 'X-Event-Id': event})

    def start(self, cookie, order, *, event='start-one', sets=10, extra=None):
        payload = {'event_id': event, 'expected_revision': order['revision'], 'order_sets': sets}
        payload.update(extra or {})
        return self.request(API + '/orders/' + order['id'] + '/start', payload, cookie=cookie)

    def finish(self, cookie, order_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status, order, _ = self.request(API + '/orders/' + order_id, cookie=cookie)
            self.assertEqual(status, 200, order)
            if order['status'] not in {'queued', 'running'}:
                return order
            time.sleep(.04)
        self.fail('Isolated synthetic customer worker did not complete.')

    def assert_projection(self, data):
        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertNotIn(key, PRIVATE_KEYS, key)
                    self.assertFalse(key.startswith('_'), key)
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        visit(data)
        serialized = json.dumps(data, ensure_ascii=False)
        for text in PRIVATE_URLS + ('/srv/', 'internal CAD failure', 'customer-token'):
            self.assertNotIn(text, serialized)

    def test_session_cookie_is_private_and_reused_without_losing_orders(self):
        cookie, headers = self.session()
        parsed = SimpleCookie()
        parsed.load(headers['Set-Cookie'])
        morsel = parsed[COOKIE]
        self.assertTrue(morsel['httponly'])
        self.assertTrue(morsel['secure'])
        self.assertEqual(morsel['samesite'].lower(), 'lax')
        self.assertEqual(morsel['path'], '/workbench/customer/')
        self.assertEqual(int(morsel['max-age']), 30 * 24 * 60 * 60)
        order = self.upload(cookie)[1]
        reused, _ = self.session(cookie)
        self.assertEqual(reused, cookie)
        listing = self.request(API + '/orders', cookie=reused)
        self.assertEqual(listing[0], 200)
        self.assertEqual([row['id'] for row in listing[1]['orders']], [order['id']])
        self.assert_projection(listing[1])

    def test_no_cookie_or_forged_cookie_cannot_read_or_create_orders(self):
        for cookie in (None, COOKIE + '=' + '0' * 64, COOKIE + '=bad'):
            with self.subTest(cookie=cookie):
                self.assertEqual(self.request(API + '/orders', cookie=cookie)[0], 401)
                self.assertEqual(self.upload(cookie)[0], 401)
        self.assertEqual(self.server.customer_batches.list(), [])
        self.assertEqual(self.calls, [])

    def test_session_expiry_is_enforced_by_server_not_only_browser_cookie(self):
        cookie, _ = self.session()
        order = self.upload(cookie)[1]
        token = cookie.split('=', 1)[1]
        identity = self.server.customer_upload.root / (hashlib.sha256(token.encode()).hexdigest() + '.json')
        record = json.loads(identity.read_text())
        record['expires_at'] = 1
        identity.write_text(json.dumps(record))
        self.assertEqual(self.request(API + '/orders', cookie=cookie)[0], 401)
        self.assertEqual(self.request(API + '/orders/' + order['id'], cookie=cookie)[0], 401)
        renewed, _ = self.session(cookie)
        self.assertNotEqual(renewed, cookie)
        self.assertEqual(self.request(API + '/orders', cookie=renewed)[1]['orders'], [])

    def test_session_and_order_ownership_survive_server_restart(self):
        cookie, _ = self.session()
        outsider, _ = self.session()
        order = self.upload(cookie)[1]
        for name in ('customer_batches', 'batch_quotes', 'case_quotes', 'orders'):
            getattr(self.server, name).close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.server = make_server(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        reused, _ = self.session(cookie)
        self.assertEqual(reused, cookie)
        self.assertEqual(self.request(API + '/orders', cookie=cookie)[1]['orders'][0]['id'], order['id'])
        self.assertEqual(self.request(API + '/orders/' + order['id'], cookie=outsider)[0], 404)
        self.assertEqual(self.request(API + '/orders/' + order['id'], cookie=cookie)[0], 200)

    def test_two_sessions_have_distinct_orders_even_with_identical_upload_events(self):
        first, _ = self.session()
        second, _ = self.session()
        self.assertNotEqual(first, second)
        a = self.upload(first)[1]
        b = self.upload(second)[1]
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(self.upload(first)[1]['id'], a['id'])
        self.assertEqual(self.upload(second)[1]['id'], b['id'])
        for owner, expected in ((first, a), (second, b)):
            listing = self.request(API + '/orders', cookie=owner)[1]['orders']
            self.assertEqual([row['id'] for row in listing], [expected['id']])
        self.assertEqual(self.request(API + '/orders/' + a['id'], cookie=second)[0], 404)
        self.assertEqual(self.start(second, a)[0], 404)
        self.assertEqual(self.request(API + '/orders/' + a['id'] + '/retry',
            {'event_id': 'other-retry', 'expected_revision': a['revision']}, cookie=second)[0], 404)
        self.assertEqual(self.calls, [])

    def test_customer_orders_are_absent_from_public_operator_store_and_api(self):
        cookie, _ = self.session()
        order = self.upload(cookie)[1]
        self.assertEqual(self.server.batch_quotes.list(), [])
        self.assertEqual(self.request('/workbench/api/quote-batches')[1]['batches'], [])
        for suffix in ('', '/runs/RUN-' + '0' * 32 + '/quote.json'):
            self.assertEqual(self.request('/workbench/api/quote-batches/' + order['id'] + suffix)[0], 404)
        internal = self.server.customer_batches.get(order['id'])
        self.assertEqual(internal['name'], '客户资料.step')
        self.assertNotEqual(self.server.customer_batches.root, self.server.batch_quotes.root)

    def test_direct_step_to_customer_quote_print_and_download_boundary(self):
        owner, _ = self.session()
        outsider, _ = self.session()
        status, order, _ = self.upload(owner, name='机箱.STEP', content=STEP)
        self.assertEqual(status, 201, order)
        self.assertEqual(order['status'], 'uploaded')
        self.assertEqual(order['files'][0]['name'], '机箱.STEP')
        self.assertIsNone(order['quote'])
        self.assertEqual(self.calls, [])
        status, started, _ = self.start(owner, order)
        self.assertEqual(status, 202, started)
        finished = self.finish(owner, order['id'])
        self.assertEqual(finished['status'], 'completed', finished)
        self.assertEqual(finished['order_sets'], 10)
        self.assertEqual(finished['quote']['status'], 'pending_review')
        self.assertTrue(finished['quote']['simulated'])
        self.assertEqual(finished['quote']['unpriced_lines'], 0)
        self.assertEqual(len(self.calls), 1)
        self.assert_projection(finished)
        internal = self.server.customer_batches.get(order['id'])
        self.assertEqual(finished['quote']['total_price'], internal['result']['totals']['total_price'])
        self.assertIn('total_cost', internal['result']['totals'])
        for key in ('quote_url', 'print_url'):
            path = finished['quote'][key]
            self.assertTrue(path.startswith(API + '/orders/' + order['id'] + '/runs/'), path)
            status, body, headers = self.request(path, cookie=owner)
            self.assertEqual(status, 200, body)
            text = body.decode()
            self.assertIn('data-customer-quote', text)
            self.assertIn('模拟报价', text)
            for forbidden in ('成本汇总', '加成', '利润', '内部核价', '成本明细') + PRIVATE_URLS:
                self.assertNotIn(forbidden, text)
            self.assertIn('no-store', headers['Cache-Control'])
            self.assertEqual(self.request(path, cookie=outsider)[0], 404)
            self.assertEqual(self.request(path)[0], 401)
        prefix = API + '/orders/' + order['id'] + '/runs/' + finished['quote']['run_id'] + '/'
        for filename in ('quote.json', 'cost.csv', 'bom.csv', 'bom-summary.csv',
                         'internal-cost.html', 'internal-cost.csv', 'quote.html',
                         'preview-record.html', 'cost-sheet', 'engineering',
                         'parts/L0001', 'assistant/proposals/anything'):
            with self.subTest(filename=filename):
                self.assertEqual(self.request(prefix + filename, cookie=owner)[0], 404)
        status, _, _ = self.start(owner, order)
        self.assertEqual(status, 202)
        self.assertEqual(len(self.calls), 1, 'Replayed start cannot duplicate CAD work.')

    def test_technical_part_and_thumbnail_enforce_owner_without_financial_fields(self):
        # Quote measurement injection deliberately does not register a native
        # display document. Register the same source-bound display fixture used
        # by the existing preview tests, without calling a real CAD engine.
        from test_batch_part_preview import ExistingCad, ExistingManufacturing, display_geometry, PNG
        cad, manufacturing = ExistingCad(), ExistingManufacturing()

        def preview_processor(row, content):
            self.calls.append(row['id'])
            sha = hashlib.sha256(content).hexdigest()
            native_id = 'CAD-' + sha[:32]
            geometry = display_geometry(sha)
            geometry['solids'] = geometry['solids'][:1]
            cad.documents[native_id] = {'id': native_id, 'sha256': sha}
            manufacturing.geometries[native_id] = geometry
            manufacturing.files[(native_id, 'S0001.png')] = PNG
            return geometry, native_id

        self.server.customer_batches.cad = cad
        self.server.customer_batches.manufacturing = manufacturing
        self.server.customer_batches.processor = preview_processor
        owner, _ = self.session()
        outsider, _ = self.session()
        order = self.upload(owner, name='part.stp', content=STEP)[1]
        self.assertEqual(self.start(owner, order)[0], 202)
        finished = self.finish(owner, order['id'])
        prefix = API + '/orders/' + order['id'] + '/runs/' + finished['quote']['run_id'] + '/customer-parts/L0001'
        status, part, _ = self.request(prefix, cookie=owner)
        self.assertEqual(status, 200, part)
        self.assertEqual(part['geometry']['output_unit'], 'mm')
        self.assertTrue(part['geometry']['solids'][0]['mesh']['indices'])
        self.assertNotIn('settings', part)
        self.assertNotIn('unit_cost', part.get('line_snapshot', {}))
        self.assertNotIn('material_rate', part.get('line_snapshot', {}))
        self.assertNotIn('total_cost', part.get('line_snapshot', {}))
        for path in (prefix, prefix + '/thumbnail'):
            status, body, headers = self.request(path, cookie=owner)
            self.assertEqual(status, 200, body)
            if path.endswith('/thumbnail'):
                self.assertEqual(headers['Content-Type'], 'image/png')
                self.assertTrue(body.startswith(b'\x89PNG'))
            self.assertEqual(self.request(path, cookie=outsider)[0], 404)
            self.assertEqual(self.request(path)[0], 401)

    def test_start_rejects_customer_pricing_injection_and_invalid_quantities(self):
        cookie, _ = self.session()
        order = self.upload(cookie)[1]
        for number, sets in enumerate((0, -1, 1.5, True, '10', None)):
            with self.subTest(sets=sets):
                status, _, _ = self.start(cookie, order, event='invalid-' + str(number), sets=sets)
                self.assertEqual(status, 400)
        for number, extra in enumerate(({'settings': {'rate_percent': '0'}}, {'material_rate': '0'},
                                        {'pricing_method': 'gross_margin'}, {'rate_percent': '0'})):
            with self.subTest(extra=extra):
                self.assertEqual(self.start(cookie, order, event='override-' + str(number), extra=extra)[0], 400)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.server.customer_batches.get(order['id'])['status'], 'uploaded')

    def test_same_upload_event_conflict_preserves_original_customer_order(self):
        cookie, _ = self.session()
        order = self.upload(cookie)[1]
        status, _, _ = self.upload(cookie, content=STEP.replace(b'DATA;', b'DATA;\n/* changed */'))
        self.assertEqual(status, 409)
        listing = self.request(API + '/orders', cookie=cookie)[1]['orders']
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]['id'], order['id'])
        self.assertEqual(self.calls, [])

    def test_csrf_cross_origin_and_ambiguous_paths_are_rejected(self):
        owner, _ = self.session()
        for supplied in ({}, {'Origin': 'https://other.invalid'}, {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(supplied=supplied):
                status, _, _ = self.request(API + '/session', {}, cookie=owner,
                    marker=bool(supplied), headers=supplied)
                self.assertEqual(status, 403)
        for path in (API + '/orders?scope=poland', API + '/%6frders',
                     API + '/orders//', API + '/orders/../orders'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, cookie=owner)[0], 404)
        self.assertEqual(self.calls, [])

    def test_new_customer_uploads_reject_zip_and_both_rar_versions(self):
        cookie, _ = self.session()
        for name, content in (('批量.zip', zipped([('cad/panel.stp', STEP)])),
                              ('批量4.rar', stored_rar4([('cad/panel.stp', STEP)])),
                              ('批量5.rar', stored_rar5([('cad/panel.stp', STEP)]))):
            with self.subTest(name=name):
                status, order, _ = self.upload(cookie, name=name, content=content, event='upload-' + str(len(self.calls)))
                self.assertEqual(status, 415, order)
        self.assertEqual(self.request(API + '/orders', cookie=cookie)[1]['orders'], [])
        self.assertEqual(self.calls, [])

    def test_failed_parse_preserves_upload_and_exposes_safe_retry(self):
        cookie, _ = self.session()
        self.fail_processing = True
        order = self.upload(cookie)[1]
        self.assertEqual(self.start(cookie, order)[0], 202)
        failed = self.finish(cookie, order['id'])
        self.assertIn(failed['status'], ('failed', 'completed_with_issues'))
        self.assert_projection(failed)
        before = copy.deepcopy(self.server.customer_batches._read(order['id'])['files'])
        self.fail_processing = False
        status, _, _ = self.request(API + '/orders/' + order['id'] + '/retry',
            {'event_id': 'retry-one', 'expected_revision': failed['revision']}, cookie=cookie)
        self.assertEqual(status, 202)
        ready = self.finish(cookie, order['id'])
        self.assertEqual(ready['status'], 'completed', ready)
        after = self.server.customer_batches._read(order['id'])['files']
        self.assertEqual([(row['id'], row['sha256'], row['name']) for row in before],
                         [(row['id'], row['sha256'], row['name']) for row in after])
        self.assert_projection(ready)


if __name__ == '__main__':
    unittest.main()
