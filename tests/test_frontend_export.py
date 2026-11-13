"""The exported template loads under the existing same-origin HTTP boundary.

Only a new temporary store and bundled public assets are read. No model, DSH,
existing records or external URL is used.
"""
import base64
import hashlib
from html.parser import HTMLParser
import http.client
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from workbench_server import make_server
from workbench_store import Workbench


class ExportHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self.stylesheets = []
        self.inline = []
        self._current = None

    def handle_starttag(self, tag, attributes):
        values = dict(attributes)
        if tag == 'script':
            self._current = []
            if values.get('src'):
                self.scripts.append(values['src'])
        elif tag == 'link' and values.get('rel') == 'stylesheet':
            self.stylesheets.append(values['href'])

    def handle_data(self, text):
        if self._current is not None:
            self._current.append(text)

    def handle_endtag(self, tag):
        if tag == 'script' and self._current is not None:
            text = ''.join(self._current)
            if text:
                self.inline.append(text)
            self._current = None


class FrontendExportHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT / 'web/demo/index.html').is_file():
            raise unittest.SkipTest('Build frontend export before HTTP verification.')
        cls.temporary = tempfile.TemporaryDirectory(prefix='hyl-frontend-export-')
        cls.bridge_calls = []

        def never_start(workspace):
            cls.bridge_calls.append(str(workspace))
            raise AssertionError('Static frontend requests must never create a model bridge.')

        cls.app = Workbench(Path(cls.temporary.name) / 'tasks', bridge_factory=never_start)
        cls.server = make_server(cls.app, 0)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()
        cls.temporary.cleanup()

    def request(self, route, method='GET'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, route)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def page(self):
        status, headers, body = self.request('/workbench/')
        self.assertEqual(status, 200)
        parser = ExportHTML()
        parser.feed(body.decode('utf-8'))
        return headers, body, parser

    def test_root_serves_new_template_and_normalizes_missing_slash(self):
        headers, body, page = self.page()
        self.assertEqual(body, (ROOT / 'web/demo/index.html').read_bytes())
        self.assertIn('图智工坊'.encode(), body)
        self.assertIn('text/html', headers['Content-Type'])
        self.assertTrue(page.scripts)
        self.assertTrue(page.stylesheets)
        status, redirect, body = self.request('/workbench')
        self.assertEqual(status, 308)
        self.assertEqual(redirect['Location'], '/workbench/')
        self.assertEqual(body, b'')

    def test_referenced_scripts_and_styles_load_with_correct_types_under_base_path(self):
        _, _, page = self.page()
        for route, expected in [(url, 'text/javascript') for url in page.scripts] + [
                (url, 'text/css') for url in page.stylesheets]:
            with self.subTest(route=route):
                self.assertTrue(route.startswith('/workbench/_next/static/'), route)
                self.assertNotIn('://', route)
                status, headers, body = self.request(route)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertIn(expected, headers['Content-Type'])
                self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_hydration_csp_allows_only_the_bundled_inline_script_hashes(self):
        headers, _, page = self.page()
        csp = headers['Content-Security-Policy']
        directives = {item.strip().split()[0]: item.strip().split()[1:]
                      for item in csp.split(';') if item.strip()}
        self.assertEqual(directives['connect-src'], ["'self'"])
        self.assertEqual(directives['frame-src'], ["'self'"])
        self.assertNotIn("'unsafe-inline'", directives['script-src'])
        self.assertNotIn("'unsafe-eval'", directives['script-src'])
        self.assertNotIn('*', directives['script-src'])
        self.assertTrue(page.inline)
        expected = {"'sha256-" + base64.b64encode(hashlib.sha256(
            text.encode('utf-8')).digest()).decode('ascii') + "'" for text in page.inline}
        actual = {value for value in directives['script-src'] if value.startswith("'sha256-")}
        self.assertEqual(actual, expected)
        self.assertEqual(headers['X-Frame-Options'], 'SAMEORIGIN')
        self.assertIn("base-uri 'none'", csp)
        self.assertIn("object-src 'none'", csp)

    def test_legacy_page_is_preserved_without_template_script_permissions(self):
        status, headers, body = self.request('/workbench/legacy/')
        self.assertEqual(status, 200)
        self.assertEqual(body, (ROOT / 'web/index.html').read_bytes())
        self.assertIn(b'recognition.js', body)
        self.assertNotIn('sha256-', headers['Content-Security-Policy'])
        self.assertNotIn('unsafe-eval', headers['Content-Security-Policy'])
        status, headers, _ = self.request('/workbench/legacy')
        self.assertEqual(status, 308)
        self.assertEqual(headers['Location'], '/workbench/legacy/')

    def test_recognition_template_remains_on_its_explicit_route(self):
        status, headers, body = self.request('/workbench/recognition/')
        self.assertEqual(status, 200)
        self.assertEqual(body, (ROOT / 'web/demo/recognition/index.html').read_bytes())
        self.assertIn('上传图纸'.encode(), body)
        self.assertIn('sha256-', headers['Content-Security-Policy'])
        status, headers, body = self.request('/workbench/recognition')
        self.assertEqual(status, 308)
        self.assertEqual(headers['Location'], '/workbench/recognition/')

    def test_sources_and_non_exported_files_are_not_public_static_routes(self):
        for route in ['/workbench/frontend/package.json', '/workbench/frontend/components/drawing-workbench.tsx',
                      '/workbench/src/workbench_server.py', '/workbench/.env', '/workbench/AGENTS.md',
                      '/workbench/pnpm-lock.yaml', '/workbench/../../AGENTS.md',
                      '/workbench/%2e%2e/src/workbench_server.py', '/workbench/_next/static/not-bundled.js']:
            with self.subTest(route=route):
                status, _, _ = self.request(route)
                self.assertEqual(status, 404)

    def test_readonly_frontend_requests_never_create_records_or_model_sessions(self):
        status, head, body = self.request('/workbench/', 'HEAD')
        self.assertEqual(status, 200)
        self.assertGreater(int(head['Content-Length']), 0)
        self.assertEqual(body, b'')
        self.assertEqual(self.app.tasks(), [])
        self.assertEqual(self.bridge_calls, [])


if __name__ == '__main__':
    unittest.main()
