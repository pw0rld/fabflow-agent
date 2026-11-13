"""Visual projections must retain exact source bytes and never decode on GET."""
import copy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from part_visuals import project_part, validate_view
from reading_inputs import BUNDLE, MANIFEST
from workbench_store import Workbench
from workbench_server import make_server


def png():
    def chunk(kind, content):
        return struct.pack('>I', len(content)) + kind + content + struct.pack('>I', zlib.crc32(kind + content) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 10, 8, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\x00' + b'\xff' * 30) * 8)) + chunk(b'IEND', b''))


def view(reference='FILE-one', page=1):
    return {'label': '侧视图', 'reference': reference, 'page': page, 'bbox': [100, 200, 300, 400],
            'hotspots': [{'label': '孔', 'polygon': [[150, 250], [200, 250], [200, 300], [150, 300]]}]}


class VisualProjectionTests(unittest.TestCase):
    def setUp(self):
        self.raw = 'EXPLICIT DRAWING ID. TEST-01 REV04'
        self.digest = hashlib.sha256(b'original PDF bytes').hexdigest()
        self.part = {'evidence': [{'reference': 'FILE-one', 'page': 1, 'region': '本件'}],
                     'observations': [{'raw_text': self.raw, 'evidence': {'reference': 'FILE-one', 'page': 1}}]}
        self.sources = {('FILE-one', 1): {'source_sha256': self.digest, 'image_url': '/safe/page.png'}}
        prepared = view()
        prepared.pop('reference')
        self.config = [{'source_sha256': self.digest,
                        'observation_sha256': hashlib.sha256(self.raw.encode()).hexdigest(),
                        'views': [prepared],
                        'schematic': {'kind': 'prepared-example', 'template': 'sealed-box-v1'}}]

    def test_old_part_shows_original_page_without_inventing_geometry(self):
        output = project_part(self.part, self.sources)
        self.assertEqual(output['status'], 'source')
        self.assertEqual(output['views'][0]['bbox'], [0, 0, 1000, 1000])
        self.assertEqual(output['views'][0]['provenance'], 'source')
        self.assertEqual(output['views'][0]['hotspots'], [])
        self.assertNotIn('schematic', output)

    def test_ai_coordinates_use_only_available_exact_part_pages(self):
        self.part['views'] = [view(), view('FILE-other'), view(page=2)]
        output = project_part(self.part, self.sources)
        self.assertEqual(len(output['views']), 1)
        self.assertEqual(output['views'][0]['provenance'], 'ai')
        self.assertEqual(output['status'], 'located')
        self.assertNotIn('schematic', output)

    def test_prepared_schematic_requires_both_original_hash_and_same_source_observation(self):
        output = project_part(self.part, self.sources, self.config)
        self.assertEqual(output['views'][0]['provenance'], 'prepared')
        self.assertEqual(output['schematic']['template'], 'sealed-box-v1')
        for changed in ('source_hash', 'observation_text', 'observation_source', 'part_source'):
            part, sources = copy.deepcopy(self.part), copy.deepcopy(self.sources)
            if changed == 'source_hash':
                sources[('FILE-one', 1)]['source_sha256'] = '0' * 64
            elif changed == 'observation_text':
                part['observations'][0]['raw_text'] += ' different'
            elif changed == 'observation_source':
                part['observations'][0]['evidence']['reference'] = 'FILE-other'
            else:
                part['evidence'][0]['reference'] = 'FILE-other'
            with self.subTest(changed=changed):
                result = project_part(part, sources, self.config)
                self.assertNotIn('schematic', result)
                self.assertFalse(any(row['provenance'] == 'prepared' for row in result['views']))

    def test_invalid_bounds_nonfinite_booleans_urls_and_degenerate_polygons_are_rejected(self):
        changes = [{'bbox': [900, 0, 200, 100]}, {'bbox': [False, 0, 300, 300]},
                   {'bbox': [0, 0, 10 ** 10000, 100]}, {'bbox': [0, 0, float('nan'), 100]},
                   {'image_url': 'https://evil.example'}, {'reference': 'FILE-other'},
                   {'hotspots': [{'label': 'invalid', 'polygon': [[0, 0], [1, 1], [2, 2]]}]},
                   {'hotspots': [{'label': 'outside', 'polygon': [[0, 0], [100, 0], [100, 100]]}]}]
        for change in changes:
            with self.subTest(change=next(iter(change))):
                with self.assertRaises(ValueError):
                    validate_view({**view(), **change}, references={'FILE-one'})

    def test_missing_cache_leaves_a_clear_original_file_fallback(self):
        output = project_part(self.part, {})
        self.assertEqual(output['views'], [])
        self.assertIn('reason', output)
        self.assertNotIn('schematic', output)


class ReadingPageHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Workbench(Path(self.tmp.name) / 'tasks')
        task = self.app.create({'event_id': 'visual-create', 'inquiry': '本地只读可视化回归', 'simulated': True})
        task = self.app.upload(task['id'], 'one', task['revision'], 'drawing.pdf', b'%PDF-1.4\nfixture: GET must never decode')
        self.task_id, self.reference = task['id'], task['attachments'][0]['id']
        record = self.app.records.load(self.task_id)
        source = record['sources'][self.reference]
        self.workspace = self.app.reader_root / self.task_id / 'run-one'
        (self.workspace / 'attachments').mkdir(parents=True)
        copied = self.workspace / 'attachments' / (self.reference + '.pdf')
        copied.write_bytes((self.app.records.task_path(self.task_id) / source['path']).read_bytes())
        files = [{'id': self.reference, 'path': str(copied), 'original_name': source['original_name'], 'sha256': source['sha256']}]
        alias = 'PDFPAGE-' + hashlib.sha256(self.reference.encode()).hexdigest()[:16] + '-1'
        self.page_file = self.workspace / BUNDLE / (alias + '.png')
        self.page_file.parent.mkdir()
        self.page_bytes = png()
        self.page_file.write_bytes(self.page_bytes)
        cache = {'schema_version': 1, 'converter': 'order-views-1',
                 'sources': [{key: files[0][key] for key in ('id', 'sha256', 'original_name')}],
                 'page_counts': {self.reference: 1},
                 'views': [{'id': alias, 'origin_id': self.reference, 'page': 1, 'path': BUNDLE + '/' + alias + '.png',
                            'sha256': hashlib.sha256(self.page_bytes).hexdigest(), 'original_sha256': source['sha256'],
                            'original_name': source['original_name'] + ' 第1页.png', 'width': 10, 'height': 8}]}
        (self.workspace / BUNDLE / MANIFEST).write_text(json.dumps(cache))
        self.manifest_file = self.workspace / '.dsh-bridge-run.json'
        self.manifest = {'task_id': self.task_id, 'session_id': 'session-one', 'files': files, 'pdf_views': cache}
        self.manifest_file.write_text(json.dumps(self.manifest))
        state = self.app._state(self.task_id)
        state['run'] = {'id': 'run-one', 'status': 'completed', 'session_id': 'session-one',
                        'source_ids': [self.reference], 'observations': []}
        self.app._save_state(self.task_id, state)
        self.server = make_server(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.opener = build_opener(ProxyHandler({}))
        self.url = '/workbench/api/tasks/' + self.task_id + '/reading-pages/' + self.reference + '/1.png?run_id=run-one'

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def get(self, path=None):
        return self.opener.open(self.base + (path or self.url), timeout=5)

    def snapshot(self):
        return {str(path.relative_to(self.tmp.name)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in Path(self.tmp.name).rglob('*') if path.is_file() and not path.is_symlink()}

    def test_existing_page_get_is_readonly_and_cannot_call_decoder_or_model(self):
        before = self.snapshot()
        with patch('reading_inputs.subprocess.run', side_effect=AssertionError('GET decoder prohibited')):
            for _ in range(2):
                with self.get() as response:
                    self.assertEqual(response.read(), self.page_bytes)
                    self.assertEqual(response.headers.get_content_type(), 'image/png')
                    self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertEqual(self.snapshot(), before)

    def test_workbench_serves_the_visual_module_and_style_referenced_by_index(self):
        with self.get('/workbench/legacy/') as response:
            html = response.read().decode()
        for name, content_type in (('part-visuals.js', 'text/javascript'),
                                   ('part-visuals.css', 'text/css')):
            self.assertIn('/workbench/' + name, html)
            with self.get('/workbench/' + name) as response:
                self.assertEqual(response.headers.get_content_type(), content_type)
                self.assertEqual(response.read(), (ROOT / 'web' / name).read_bytes())

    def test_task_projection_keeps_explicit_views_and_readonly_source_binding(self):
        state = self.app._state(self.task_id)
        observation = {'name': 'engineering_open_items', 'raw_text': '本件侧视图', 'raw_unit': None,
                       'status': 'observed', 'reason': None,
                       'evidence': {'reference': self.reference, 'page': 1, 'region': '侧视图'}}
        part = {'key': 'p01', 'name': 'Test part', 'part_number': 'TEST-01', 'version': '04',
                'status': 'pending_review', 'evidence': [observation['evidence']],
                'observations': [observation], 'views': [view(self.reference)]}
        state['run']['source_page_counts'] = {self.reference: 1}
        state['run']['recognition_result'] = {'status': 'completed', 'candidates': [], 'parts': [part]}
        self.app._save_state(self.task_id, state)
        before = self.snapshot()
        with patch('reading_inputs.subprocess.run', side_effect=AssertionError('GET decoder prohibited')):
            task = self.app.task(self.task_id)
        visual = task['recognition']['parts'][0]['visualization']
        self.assertEqual(visual['status'], 'located')
        self.assertEqual(visual['views'][0]['provenance'], 'ai')
        self.assertEqual(visual['views'][0]['image_url'], self.url)
        self.assertNotIn('schematic', visual)
        self.assertEqual(self.snapshot(), before)

    def test_missing_run_wrong_reference_wrong_page_and_query_are_not_exposed(self):
        for path, expected in ((self.url.replace('run-one', 'other-run'), 404),
                               (self.url.replace(self.reference, 'FILE-other'), 404),
                               (self.url.replace('/1.png', '/2.png'), 404),
                               (self.url.split('?')[0], 400),
                               (self.url + '&run_id=other', 400),
                               (self.url + '&path=/etc/passwd', 400)):
            with self.subTest(path=path):
                with self.assertRaises(HTTPError) as caught:
                    self.get(path)
                self.assertEqual(caught.exception.code, expected)

    def test_changed_png_original_or_bridge_source_binding_is_rejected_without_repair(self):
        original = self.page_file.read_bytes()
        self.page_file.write_bytes(original + b'changed')
        with self.assertRaises(HTTPError) as caught:
            self.get()
        self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.page_file.read_bytes(), original + b'changed')
        self.page_file.write_bytes(original)
        self.manifest['files'][0]['sha256'] = '0' * 64
        self.manifest_file.write_text(json.dumps(self.manifest))
        with self.assertRaises(HTTPError) as caught:
            self.get()
        self.assertEqual(caught.exception.code, 400)

    def test_missing_cache_never_renders_and_does_not_mutate_task(self):
        self.page_file.unlink()
        before = self.snapshot()
        with patch('reading_inputs.subprocess.run', side_effect=AssertionError('GET decoder prohibited')):
            with self.assertRaises(HTTPError):
                self.get()
        self.assertEqual(self.snapshot(), before)

    def test_bridge_cache_and_symlink_cannot_point_to_another_task(self):
        saved = self.manifest_file.read_bytes()
        self.manifest['task_id'] = 'INQ-another'
        self.manifest_file.write_text(json.dumps(self.manifest))
        with self.assertRaises(HTTPError):
            self.get()
        self.manifest_file.write_bytes(saved)
        alternate = Path(self.tmp.name) / 'outside.png'
        alternate.write_bytes(self.page_bytes)
        self.page_file.unlink()
        self.page_file.symlink_to(alternate)
        with self.assertRaises(HTTPError):
            self.get()


if __name__ == '__main__':
    unittest.main()
