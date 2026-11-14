"""Heavy preview request admission; synthetic HTTP fixtures, no CAD/model calls.

Execute on the remote test server only. No production stores or quota are used.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from workbench_server import WorkbenchHandler, make_server
from workbench_store import Workbench, WorkbenchError

BATCH = 'BATCH-' + 'a' * 32
RUN = 'RUN-' + 'b' * 32
STAFF = '/workbench/api/quote-batches/' + BATCH + '/runs/' + RUN
ADMIN = '/ortho2cad-test/customer-upload-admin/api/quote-batches/' + BATCH + '/runs/' + RUN
CUSTOMER = '/workbench/customer/api/orders/' + BATCH + '/runs/' + RUN


class GeometryRequestAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='geometry-admission-')
        self.server = make_server(Workbench(Path(self.temp.name) / 'tasks'), 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for name in ('customer_batches', 'batch_quotes', 'case_quotes', 'orders'):
            getattr(self.server, name).close()
        self.temp.cleanup()

    def get(self, path):
        request = Request(self.base + path, headers={'X-Workbench-Request': '1'})
        try:
            response = urlopen(request, timeout=8)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            body = json.loads(raw) if response.headers.get_content_type() == 'application/json' else raw
            return response.status, body, dict(response.headers)

    def test_busy_geometry_does_not_block_status_quota_or_thumbnail(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(*_):
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError('Fixture was not released')
            return {'synthetic': True}
        portal = self.server.customer_upload
        with patch.object(self.server.batch_quotes, 'part_geometry', side_effect=blocked) as source, \
             patch.object(self.server.batch_quotes, 'get', return_value={'id': BATCH}), \
             patch.object(self.server.batch_quotes, 'part_thumbnail', return_value=(b'png-fixture', 'image/png', 'L0001.png')), \
             patch.object(portal, 'session', return_value='synthetic-owner'), \
             patch.object(portal, '_owned', return_value={}), \
             patch.object(portal, 'quota', return_value={'remaining': 10}), \
             patch.object(portal, 'customer_part_geometry', side_effect=AssertionError('Busy request must not produce geometry')):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(self.get, STAFF + '/parts/L0001')
                try:
                    self.assertTrue(entered.wait(timeout=5))
                    status, body, headers = self.get(CUSTOMER + '/customer-parts/L0001')
                    self.assertEqual(status, 503)
                    self.assertEqual(body['error']['code'], 'geometry_busy')
                    self.assertEqual(headers.get('Retry-After'), '2')
                    self.assertEqual(self.get('/workbench/api/quote-batches/' + BATCH)[0], 200)
                    self.assertEqual(self.get('/workbench/customer/api/quota')[0], 200)
                    self.assertEqual(self.get(STAFF + '/parts/L0001/thumbnail')[:2], (200, b'png-fixture'))
                    self.assertEqual(source.call_count, 1)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=8)[0], 200)

    def test_all_heavy_get_namespaces_share_one_gate(self):
        # These producers cannot execute while occupied, even across customer,
        # staff and native-document routes. Ordinary authentication still runs.
        portal = self.server.customer_upload
        with patch.object(portal, 'session', return_value='synthetic-owner'), \
             patch.object(portal, '_owned', return_value={}):
            gate = self.server.geometry_request_gate
            self.assertTrue(gate.acquire(blocking=False))
            try:
                for path in (
                    STAFF + '/parts', STAFF + '/parts/L0001', STAFF + '/customer-parts/L0001',
                    STAFF + '/process-pricing', ADMIN + '/manufacturing',
                    ADMIN + '/customer-parts/L0001/step', ADMIN + '/manufacturing/L0001/laser.svg',
                    CUSTOMER + '/manufacturing', CUSTOMER + '/customer-parts/L0001',
                    CUSTOMER + '/process-pricing', CUSTOMER + '/customer-parts/L0001/step',
                    CUSTOMER + '/manufacturing/L0001/laser.dxf',
                    '/workbench/api/cad/documents/CAD-' + 'c' * 32 + '/manufacturing/geometry',
                    '/workbench/api/cad/documents/CAD-' + 'c' * 32 + '/manufacturing/bom.csv',
                    '/workbench/api/cad/documents/CAD-' + 'c' * 32 + '/manufacturing/files/S0001.step',
                ):
                    with self.subTest(path=path):
                        status, body, headers = self.get(path)
                        self.assertEqual(status, 503)
                        self.assertEqual(body['error']['code'], 'geometry_busy')
                        self.assertEqual(headers.get('Retry-After'), '2')
            finally:
                gate.release()

    def test_customer_ownership_is_checked_before_busy_preview_response(self):
        portal = self.server.customer_upload
        with patch.object(portal, 'session', return_value='other-browser'), \
             patch.object(portal, '_owned', side_effect=WorkbenchError('订单不存在。', 'not_found', 404)), \
             patch.object(portal, 'customer_part_geometry', side_effect=AssertionError('Foreign owner reached producer')):
            self.assertTrue(self.server.geometry_request_gate.acquire(blocking=False))
            try:
                status, body, headers = self.get(CUSTOMER + '/customer-parts/L0001')
                self.assertEqual(status, 404)
                self.assertEqual(body['error']['code'], 'not_found')
                self.assertNotIn('Retry-After', headers)
            finally:
                self.server.geometry_request_gate.release()

    def test_quote_and_print_rendering_share_geometry_gate_but_static_json_does_not(self):
        portal = self.server.customer_upload
        with patch.object(portal, 'session', return_value='synthetic-owner'), \
             patch.object(portal, '_owned', return_value={}) as owned, \
             patch.object(portal, 'artifact', side_effect=AssertionError('Busy customer quote must not render')), \
             patch.object(portal, 'staff_artifact', side_effect=AssertionError('Busy staff quote must not render')), \
             patch.object(self.server.batch_quotes, 'artifact',
                          return_value=(b'{"synthetic":true}', 'application/json', 'quote.json')) as artifact:
            gate = self.server.geometry_request_gate
            self.assertTrue(gate.acquire(blocking=False))
            try:
                for prefix, names in (
                    (CUSTOMER, ('customer-quote.html', 'customer-print.html')),
                    (ADMIN, ('customer-quote.html', 'customer-print.html')),
                    (STAFF, ('customer-quote.html', 'customer-print.html', 'quote.html', 'quote-print.html')),
                ):
                    for name in names:
                        with self.subTest(path=prefix + '/' + name):
                            status, body, headers = self.get(prefix + '/' + name)
                            self.assertEqual(status, 503)
                            self.assertEqual(body['error']['code'], 'geometry_busy')
                            self.assertEqual(headers.get('Retry-After'), '2')
                self.assertEqual(owned.call_count, 2, 'Customer quote ownership must run before admission')
                self.assertEqual(artifact.call_count, 0, 'Busy quote must not expand cached meshes')
                status, body, _ = self.get(STAFF + '/quote.json')
                self.assertEqual((status, body), (200, {'synthetic': True}))
                self.assertEqual(artifact.call_count, 1)
            finally:
                gate.release()


class GeometryGateLifetimeTests(unittest.TestCase):
    def handler(self):
        handler = WorkbenchHandler.__new__(WorkbenchHandler)
        handler.server = SimpleNamespace(geometry_request_gate=threading.BoundedSemaphore(1))
        return handler

    def test_gate_covers_serialization_and_releases_on_write_disconnect(self):
        handler = self.handler()
        gate = handler.server.geometry_request_gate
        def disconnected(_):
            self.assertFalse(gate.acquire(blocking=False), 'Gate was released before serialization/write')
            raise BrokenPipeError('synthetic client disconnected')
        handler._json = disconnected
        with self.assertRaises(BrokenPipeError):
            handler._geometry_json(lambda: {'synthetic': True})
        self.assertTrue(gate.acquire(blocking=False))
        gate.release()

    def test_producer_failure_releases_gate_without_sending_geometry(self):
        handler = self.handler()
        handler._json = Mock(side_effect=AssertionError('Failed producer must not send data'))
        def failed():
            raise ValueError('synthetic validation failure')
        with self.assertRaises(ValueError):
            handler._geometry_json(failed)
        self.assertTrue(handler.server.geometry_request_gate.acquire(blocking=False))
        handler.server.geometry_request_gate.release()

    def test_file_write_retains_gate_and_preserves_download_options(self):
        handler = self.handler()
        def send(status, content, mime, **options):
            self.assertFalse(handler.server.geometry_request_gate.acquire(blocking=False))
            self.assertEqual((status, content, mime), (200, b'synthetic', 'application/octet-stream'))
            self.assertEqual(options, {'filename': 'part.step', 'attachment': True, 'filename_fallback': 'part.step'})
        handler._send = send
        handler._geometry_file(lambda: (b'synthetic', 'application/octet-stream', 'part.step'),
                               attachment=True, ascii_filename=True)
        self.assertTrue(handler.server.geometry_request_gate.acquire(blocking=False))
        handler.server.geometry_request_gate.release()


if __name__ == '__main__':
    unittest.main()
