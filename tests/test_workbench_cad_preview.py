"""CAD assets stay read-only and under the existing attachment/auth boundary."""
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from workbench_store import Workbench
from workbench_server import make_server


class CadPreviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Workbench(Path(self.tmp.name) / 'tasks')
        self.server = make_server(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def get(self, path):
        return urlopen(self.base + path, timeout=5)

    def test_worker_permissions_do_not_relax_business_or_viewer_pages(self):
        with self.get('/workbench/') as response:
            self.assertNotIn('unsafe-eval', response.headers['Content-Security-Policy'])
        with self.get('/workbench/cad-preview/index.html') as response:
            self.assertEqual(response.status, 200)
            csp = response.headers['Content-Security-Policy']
            self.assertIn("worker-src 'self'", csp)
            self.assertNotIn('unsafe-eval', csp)
        with self.get('/workbench/cad-preview/step-worker.js') as response:
            self.assertIn("script-src 'self' 'unsafe-eval'", response.headers['Content-Security-Policy'])
        with self.get('/workbench/cad-preview/vendor/step/occt-import-js.wasm') as response:
            self.assertEqual(response.headers.get_content_type(), 'application/wasm')
            self.assertEqual(response.read()[:4], b'\x00asm')

    def test_preview_does_not_mutate_uploaded_cad_or_revision(self):
        task = self.app.create({'event_id': 'cad-preview', 'inquiry': '本地只读预览', 'simulated': True})
        content = (ROOT / 'samples/cad-test-20261019/step/Nema-17_MountBracket.step').read_bytes()
        task = self.app.upload(task['id'], 'upload-cad', task['revision'], 'part.step', content)
        source = task['attachments'][0]
        before = self.app.records.load(task['id'])
        for path in ('/workbench/cad-preview/index.html?task=' + task['id'] + '&source=' + source['id'],
                     '/workbench/api/tasks/' + task['id']):
            with self.get(path) as response:
                self.assertEqual(response.status, 200)
        with self.get(source['url']) as response:
            self.assertEqual(hashlib.sha256(response.read()).hexdigest(), hashlib.sha256(content).hexdigest())
            self.assertIn('attachment', response.headers['Content-Disposition'])
        self.assertEqual(self.app.records.load(task['id']), before)
        self.assertIsNone(self.app.task(task['id']).get('run'))

    def test_only_bundled_public_assets_are_exposed(self):
        for path in ('/workbench/cad-preview/../../src/workbench_server.py',
                     '/workbench/cad-preview/%2e%2e/app.js',
                     '/workbench/cad-preview/vendor/step/not-bundled.js'):
            with self.assertRaises(HTTPError) as caught:
                self.get(path)
            self.assertEqual(caught.exception.code, 404)
