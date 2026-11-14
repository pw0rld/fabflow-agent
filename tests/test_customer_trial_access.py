"""Exact trial routing and cookie boundaries; run on the remote build server.

Nginx Basic protection remains a deployment boundary and is verified against
the live reverse proxy separately. No model, CAD parse or upload is submitted.
"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from deploy.trial_access_config import append_trial_access, trial_access_fragment
from customer_upload_service import CustomerUploadService
from workbench_store import Workbench
import workbench_server


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


class TrialFragmentTests(unittest.TestCase):
    def test_only_exact_public_entry_is_proxied_and_unknown_suffixes_are_closed(self):
        fragment = trial_access_fragment()
        self.assertIn('location = /try {', fragment)
        self.assertIn('return 308 /try/;', fragment)
        self.assertIn('location = /try/ {', fragment)
        self.assertIn('proxy_pass http://127.0.0.1:3092/workbench/experience/;', fragment)
        self.assertIn('limit_except GET HEAD { deny all; }', fragment)
        self.assertIn('location ^~ /try/ {', fragment)
        self.assertIn('return 404;', fragment)
        self.assertEqual(fragment.count('proxy_pass '), 1)
        for namespace in ('/workbench/api/', '/ortho2cad-test/', '/workbench/customer/api', 'proxy_cookie_path'):
            self.assertNotIn(namespace, fragment)
        self.assertIn('proxy_set_header Authorization "";', fragment)
        self.assertIn('proxy_set_header Cookie "";', fragment)

    def test_append_is_idempotent_and_preserves_all_existing_access_rules(self):
        previous = 'location ^~ /workbench/api/ { auth_basic "staff"; return 401; }\n'
        result = append_trial_access(previous)
        self.assertTrue(result.startswith(previous.rstrip()))
        self.assertEqual(append_trial_access(result), result)
        for conflicting in ('location = /try { return 301 /other; }',
                            'location /try/ { proxy_pass http://127.0.0.1:1; }',
                            'location ^~ /try/ { return 403; }',
                            'location = "/try/" { return 403; }'):
            with self.subTest(conflicting=conflicting), self.assertRaises(ValueError):
                append_trial_access(conflicting)
        with self.assertRaises(ValueError):
            append_trial_access(result, 'http://127.0.0.1:3093')

    def test_upstream_cannot_add_remote_hosts_paths_or_nginx_directives(self):
        for value in ('https://127.0.0.1:3092', 'http://example.invalid:3092',
                      'http://user:pass@127.0.0.1:3092', 'http://127.0.0.1:3092/api',
                      'http://127.0.0.1:3092?token=x', 'http://127.0.0.1:3092#fragment',
                      'http://127.0.0.1:3092;\nreturn 200;', 'http://127.0.0.1',
                      'http://127.0.0.1:0', 'http://127.0.0.1:65536',
                      'http://127.0.0.1:3092\n', 'http://127.0.0.1:3092\r',
                      'http://127.0.0.1:3092\x00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                trial_access_fragment(value)


class TrialHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='fabflow-trial-http-')
        self.root = Path(self.temporary.name)
        exported = self.root / 'web/demo/experience/index.html'
        exported.parent.mkdir(parents=True)
        exported.write_text('<!doctype html><html><body>Isolated customer trial shell<script>window.trialFixture=true</script></body></html>')
        self.asset = self.root / 'web/demo/_next/static/trial.js'
        self.asset.parent.mkdir(parents=True)
        self.asset.write_text('window.trialAsset=true')
        self.paths = patch.object(workbench_server, 'APP_ROOT', self.root)
        self.routes = patch.dict(workbench_server.STATIC, {
            '/workbench/experience/': ('demo/experience/index.html', 'text/html; charset=utf-8'),
            '/workbench/_next/static/trial.js': ('demo/_next/static/trial.js', 'text/javascript; charset=utf-8')}, clear=True)
        self.paths.start(); self.routes.start()
        self.addCleanup(self.paths.stop); self.addCleanup(self.routes.stop)
        self.server = workbench_server.make_server(Workbench(self.root / 'tasks'), 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def tearDown(self):
        for name in ('customer_batches', 'batch_quotes', 'case_quotes', 'orders'):
            getattr(self.server, name).close()
        self.server.shutdown(); self.server.server_close(); self.thread.join(5)
        self.temporary.cleanup()

    def request(self, path, *, method='GET', data=None):
        headers = {'Origin': self.base, 'X-Workbench-Request': '1'}
        if data is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(data).encode()
        try:
            response = self.opener.open(Request(self.base + path, method=method, data=data, headers=headers), timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.read(), dict(response.headers)

    def test_canonical_trial_shell_has_own_route_and_bundled_hydration_policy(self):
        status, _, headers = self.request('/workbench/experience?resume=1')
        self.assertEqual(status, 308)
        self.assertEqual(headers['Location'], '/workbench/experience/?resume=1')
        status, body, headers = self.request('/workbench/experience/')
        self.assertEqual(status, 200)
        self.assertIn(b'Isolated customer trial shell', body)
        self.assertNotIn('Set-Cookie', headers)
        self.assertIn('sha256-', headers['Content-Security-Policy'])
        self.assertNotIn("'unsafe-eval'", headers['Content-Security-Policy'])
        self.assertNotIn("script-src 'self' 'unsafe-inline'", headers['Content-Security-Policy'])

    def test_trial_shell_does_not_create_upload_or_let_request_paths_select_files(self):
        before = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob('*') if path.is_file())
        self.assertEqual(self.request('/workbench/experience/')[0], 200)
        self.assertEqual(self.request('/workbench/_next/static/trial.js')[0], 200)
        for path in ('/workbench/experience/private.json', '/workbench/experience/api/orders',
                     '/workbench/experience/../../tasks/data.json', '/try/api/orders'):
            self.assertEqual(self.request(path)[0], 404)
        after = sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob('*') if path.is_file())
        self.assertEqual(before, after)

    def test_customer_cookie_scope_and_api_session_requirement_are_unchanged(self):
        self.assertEqual(self.request('/workbench/customer/api/orders')[0], 401)
        status, body, headers = self.request('/workbench/customer/api/session', method='POST', data={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'ready': True})
        cookie = headers['Set-Cookie']
        self.assertIn('Path=/workbench/customer/', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('Secure', cookie)
        self.assertIn('SameSite=Lax', cookie)
        self.assertNotIn('Path=/try/', cookie)
        self.assertEqual(self.request('/workbench/customer/api/orders')[0], 401)


if __name__ == '__main__':
    unittest.main()
