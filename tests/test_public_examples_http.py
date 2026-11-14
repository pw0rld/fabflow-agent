"""Public sample allowlist and HTTP isolation; synthetic files only.

Run on the remote build server. These tests never invoke CAD, AI or SMS, create
customer sessions, admit orders, or use production upload allowance.
"""
from contextlib import ExitStack
import copy
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
import workbench_server
from public_examples import PREFIX, MAX_GEOMETRY_BYTES, MAX_STEP_BYTES
from workbench_store import Workbench

SLUG = 'motor-bracket'
STEP = b'ISO-10303-21;\nHEADER;ENDSEC;DATA;ENDSEC;END-ISO-10303-21;\n'
GEOMETRY = b'{"output_unit":"mm","solids":[]}'


def catalog():
    return {'schema_version': 1, 'examples': [{
        'id': SLUG, 'title': 'Synthetic public bracket', 'category': 'Bracket',
        'description': 'Synthetic registry fixture, never a customer model.',
        'filename': 'fixture.step', 'source_label': 'Synthetic public source',
        'license': 'CC0-1.0', 'part_count': 1, 'occurrence_count': 1,
        'detail_url': PREFIX + SLUG + '/example.json',
        'geometry_url': PREFIX + SLUG + '/geometry.json',
        'download_url': PREFIX + SLUG + '/source.step'}]}


class PublicExampleHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='public-cad-fixture-')
        self.root = Path(self.temp.name)
        self.app_root = self.root / 'app'
        self.public = self.app_root / 'web/public-examples'
        self.sample = self.public / SLUG
        self.sample.mkdir(parents=True)
        (self.public / 'catalog.json').write_text(json.dumps(catalog()))
        (self.sample / 'example.json').write_text(json.dumps({'id': SLUG, 'title': 'Synthetic bracket'}))
        (self.sample / 'geometry.json').write_bytes(GEOMETRY)
        (self.sample / 'source.step').write_bytes(STEP)
        (self.sample / 'LICENSE.txt').write_text('Synthetic CC0 license fixture')
        (self.sample / 'README.txt').write_text('Synthetic public attribution fixture')
        self.private = self.root / 'private-order.step'
        self.private.write_bytes(b'PRIVATE-CUSTOMER-CONTENT')
        self.paths = patch.object(workbench_server, 'APP_ROOT', self.app_root)
        self.paths.start()
        self.server = workbench_server.make_server(Workbench(self.root / 'tasks'), 0,
                                                    trial_flow_enabled=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        for name in ('customer_batches', 'batch_quotes', 'case_quotes', 'orders'):
            getattr(self.server, name).close()
        self.paths.stop()
        self.temp.cleanup()

    def request(self, path, *, method='GET', headers=None):
        # Send exact raw paths, including encoded/traversal/fragment attempts.
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=b'{}' if method == 'POST' else None,
                               headers={'X-Workbench-Request': '1', 'Content-Type': 'application/json',
                                        **(headers or {})})
            with connection.getresponse() as reply:
                return reply.status, reply.read(), dict(reply.getheaders())
        finally:
            connection.close()

    def test_public_catalog_works_without_cookie_or_private_services(self):
        with ExitStack() as stack:
            for method in ('session', 'new_session', 'quota', 'list', 'upload', 'start', 'get'):
                stack.enter_context(patch.object(self.server.customer_upload, method,
                    side_effect=AssertionError('Public sample reached private customer service')))
            status, body, headers = self.request(PREFIX + 'catalog.json')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), catalog())
        self.assertNotIn('Set-Cookie', headers)
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.server.trial_leads.staff_list()['total'], 0)

    def test_exact_bundled_files_and_safe_original_download(self):
        for leaf in ('example.json', 'geometry.json', 'source.step', 'LICENSE.txt', 'README.txt'):
            with self.subTest(leaf=leaf):
                status, body, headers = self.request(PREFIX + SLUG + '/' + leaf)
                self.assertEqual(status, 200)
                self.assertNotIn(b'PRIVATE-CUSTOMER-CONTENT', body)
                self.assertNotIn('Set-Cookie', headers)
                if leaf == 'source.step':
                    self.assertEqual(body, STEP)
                    self.assertEqual(headers['Content-Type'], 'application/step')
                    self.assertEqual(headers['Content-Disposition'],
                        "attachment; filename*=UTF-8''motor-bracket.step")
                if leaf == 'geometry.json':
                    self.assertEqual(body, GEOMETRY)
        status, body, headers = self.request(PREFIX + SLUG + '/source.step', method='HEAD')
        self.assertEqual((status, body, int(headers['Content-Length'])), (200, b'', len(STEP)))

    def test_unknown_queries_encoded_and_traversal_paths_are_closed(self):
        for suffix in ('', '../private-order.step', 'unknown/source.step', SLUG + '/..',
                       SLUG + '/../../private-order.step', SLUG + '/source.step/extra',
                       SLUG + '//source.step', SLUG + '/quote.json', SLUG + '/SOURCE.STEP',
                       SLUG + '/%73ource.step', '%6dotor-bracket/source.step',
                       SLUG + '/source.step?path=/private-order.step',
                       SLUG + '/source.step?', 'catalog.json?root=/private',
                       SLUG + '/source.step%3f', SLUG + '/source.step#ignored'):
            with self.subTest(suffix=suffix):
                status, body, _ = self.request(PREFIX + suffix)
                self.assertEqual(status, 404)
                self.assertNotIn(b'PRIVATE-CUSTOMER-CONTENT', body)

    def test_post_never_admits_work_and_private_customer_paths_still_need_owner(self):
        with patch.object(self.server.customer_upload, 'upload', side_effect=AssertionError('Upload admitted')):
            self.assertEqual(self.request(PREFIX + SLUG + '/source.step', method='POST')[0], 404)
        self.assertEqual(self.request('/workbench/customer/api/orders', headers={
            'X-Workbench-Role': 'staff', 'X-FabFlow-Staff-User': 'root'})[0], 401)
        self.assertEqual(self.request('/workbench/customer/api/orders/BATCH-' + 'a' * 32)[0], 401)

    def test_leaf_directory_and_root_symlinks_cannot_expose_private_files(self):
        original = self.sample / 'source.step'
        original.unlink(); original.symlink_to(self.private)
        self.assertEqual(self.request(PREFIX + SLUG + '/source.step')[0], 404)
        original.unlink(); original.write_bytes(STEP)
        kept = self.public / 'kept-bracket'
        self.sample.rename(kept); self.sample.symlink_to(kept, target_is_directory=True)
        self.assertEqual(self.request(PREFIX + SLUG + '/source.step')[0], 404)
        self.sample.unlink(); kept.rename(self.sample)
        public_kept = self.public.with_name('kept-public')
        self.public.rename(public_kept); self.public.symlink_to(public_kept, target_is_directory=True)
        self.assertEqual(self.request(PREFIX + 'catalog.json')[0], 404)
        self.public.unlink(); public_kept.rename(self.public)
        web = self.public.parent
        web_kept = web.with_name('kept-web')
        web.rename(web_kept); web.symlink_to(web_kept, target_is_directory=True)
        self.assertEqual(self.request(PREFIX + SLUG + '/source.step')[0], 404)

    def test_oversized_assets_and_nonregular_files_are_not_read(self):
        for name, limit in (('geometry.json', MAX_GEOMETRY_BYTES), ('source.step', MAX_STEP_BYTES)):
            with self.subTest(name=name):
                with (self.sample / name).open('wb') as stream:
                    stream.seek(limit); stream.write(b'x')
                self.assertEqual(self.request(PREFIX + SLUG + '/' + name)[0], 404)
        if hasattr(os, 'mkfifo'):
            target = self.sample / 'LICENSE.txt'
            target.unlink(); os.mkfifo(target)
            self.assertEqual(self.request(PREFIX + SLUG + '/LICENSE.txt')[0], 404)

    def test_catalog_cannot_redirect_to_private_or_remote_assets(self):
        variants = []
        for field, value in (('detail_url', '/workbench/customer/api/orders/private'),
                             ('geometry_url', 'https://outside.invalid/model.json'),
                             ('download_url', PREFIX + 'unknown/source.step'), ('part_count', True)):
            changed = copy.deepcopy(catalog()); changed['examples'][0][field] = value
            variants.append(changed)
        variants.append({'schema_version': 1, 'examples': ['not-an-entry']})
        injected = copy.deepcopy(catalog()); injected['examples'][0]['customer_owner'] = 'private'
        variants.append(injected)
        for value in variants:
            with self.subTest(value=value):
                (self.public / 'catalog.json').write_text(json.dumps(value))
                self.assertEqual(self.request(PREFIX + 'catalog.json')[0], 404)

    def test_missing_and_misbound_metadata_fail_without_private_fallback(self):
        detail = self.sample / 'example.json'
        detail.write_text('{"id":"sheet-metal-enclosure"}')
        self.assertEqual(self.request(PREFIX + SLUG + '/example.json')[0], 404)
        detail.write_bytes(b'not JSON')
        self.assertEqual(self.request(PREFIX + SLUG + '/example.json')[0], 404)
        detail.unlink()
        self.assertEqual(self.request(PREFIX + SLUG + '/example.json')[0], 404)


if __name__ == '__main__':
    unittest.main()
