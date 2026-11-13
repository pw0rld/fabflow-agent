"""Source-bound CAD snapshots survive restarts without touching quotations."""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from cad_documents import CadDocumentStore, MAX_STEP_BYTES
from prep_record import RecordError
from workbench_store import Workbench, WorkbenchError
from workbench_server import make_server

STEP = b'ISO-10303-21;\nHEADER;ENDSEC;DATA;ENDSEC;END-ISO-10303-21;'


def snapshot(sha256):
    return {'schema_version': 1, 'source_sha256': sha256,
            'importer': {'name': 'occt-import-js', 'version': '0.0.23', 'output_unit': 'mm'},
            'root_id': 'n-0', 'hierarchy_origin': 'source', 'warnings': [],
            'nodes': [{'id': 'n-0', 'parent_id': None, 'name': 'assembly', 'mesh_indices': [],
                       'children': ['n-0-0', 'n-0-1']},
                      {'id': 'n-0-0', 'parent_id': 'n-0', 'name': 'lid', 'mesh_indices': [0], 'children': []},
                      {'id': 'n-0-1', 'parent_id': 'n-0', 'name': 'body', 'mesh_indices': [1], 'children': []}],
            'meshes': [{'id': 'm-0', 'index': 0, 'name': 'lid', 'vertex_count': 8, 'triangle_count': 12},
                       {'id': 'm-1', 'index': 1, 'name': 'body', 'vertex_count': 16, 'triangle_count': 24}],
            'stats': {'mesh_count': 2, 'vertex_count': 24, 'triangle_count': 36}}


class CadDocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'cad'
        self.store = CadDocumentStore(self.root)
        self.document = self.store.create('test.step', STEP)

    def tearDown(self):
        self.tmp.cleanup()

    def save_snapshot(self):
        self.document = self.store.update(self.document['id'], 'snapshot', {
            'expected_revision': self.document['revision'], 'snapshot': snapshot(self.document['sha256'])})
        return self.document

    def test_restart_preserves_source_snapshot_and_view(self):
        doc = self.save_snapshot()
        view = {'selected_node_id': 'n-0-0', 'isolated_node_id': 'n-0-0', 'hidden_node_ids': ['n-0-1']}
        doc = self.store.update(doc['id'], 'view', {'expected_revision': doc['revision'], 'view': view})
        restarted = CadDocumentStore(self.root)
        self.assertEqual(restarted.get(doc['id']), doc)
        self.assertEqual(restarted.file(doc['id']), (STEP, 'test.step'))
        self.assertEqual(doc['engineering_status'], 'pending_review')
        self.assertEqual(doc['snapshot_kind'], 'browser_derived')
        self.assertEqual(doc['processing_status'], 'parsed')
        self.assertEqual(restarted.list()[0]['node_count'], 3)
        self.assertNotIn('snapshot', restarted.list()[0])

    def test_reupload_is_idempotent_and_keeps_saved_view(self):
        doc = self.save_snapshot()
        doc = self.store.update(doc['id'], 'view', {'expected_revision': doc['revision'],
            'view': {'selected_node_id': 'n-0-1', 'isolated_node_id': None, 'hidden_node_ids': []}})
        self.assertEqual(self.store.create('renamed.stp', STEP), doc)
        self.assertEqual(len(self.store.list()), 1)
        same = self.store.update(doc['id'], 'snapshot', {'expected_revision': doc['revision'], 'snapshot': doc['snapshot']})
        self.assertEqual(same['revision'], doc['revision'])

    def test_stale_revision_and_invalid_view_do_not_mutate(self):
        doc = self.save_snapshot()
        for payload in (
            {'expected_revision': 0, 'view': doc['view']},
            {'expected_revision': doc['revision'], 'view': dict(doc['view'], selected_node_id='n-0-9')},
            {'expected_revision': doc['revision'], 'view': dict(doc['view'], hidden_node_ids=['n-0-0', 'n-0-0'])},
        ):
            with self.assertRaises(RecordError):
                self.store.update(doc['id'], 'view', payload)
            self.assertEqual(self.store.get(doc['id']), doc)

    def test_snapshot_rejects_wrong_source_or_graph_and_preserves_original(self):
        base = snapshot(self.document['sha256'])
        invalid = []
        wrong_sha = copy.deepcopy(base); wrong_sha['source_sha256'] = '0' * 64; invalid.append(wrong_sha)
        wrong_parent = copy.deepcopy(base); wrong_parent['nodes'][1]['parent_id'] = 'n-0-1'; invalid.append(wrong_parent)
        bad_mesh = copy.deepcopy(base); bad_mesh['nodes'][1]['mesh_indices'] = [2]; invalid.append(bad_mesh)
        unassigned = copy.deepcopy(base); unassigned['nodes'][1]['mesh_indices'] = []; invalid.append(unassigned)
        unreachable = copy.deepcopy(base); unreachable['nodes'][0]['children'] = ['n-0-0']; invalid.append(unreachable)
        bad_stats = copy.deepcopy(base); bad_stats['stats']['triangle_count'] = 100; invalid.append(bad_stats)
        bool_count = copy.deepcopy(base); bool_count['meshes'][0]['vertex_count'] = True; invalid.append(bool_count)
        unexpected = copy.deepcopy(base); unexpected['material'] = 'SS304'; invalid.append(unexpected)
        for item in invalid:
            with self.subTest(item=item):
                with self.assertRaises(RecordError):
                    self.store.update(self.document['id'], 'snapshot', {'expected_revision': 0, 'snapshot': item})
                self.assertIsNone(self.store.get(self.document['id'])['snapshot'])
                self.assertEqual(self.store.file(self.document['id'])[0], STEP)

    def test_existing_snapshot_cannot_be_silently_replaced(self):
        doc = self.save_snapshot()
        changed = copy.deepcopy(doc['snapshot'])
        changed['nodes'][1]['name'] = 'unexpected replacement'
        with self.assertRaises(RecordError):
            self.store.update(doc['id'], 'snapshot', {'expected_revision': doc['revision'], 'snapshot': changed})
        self.assertEqual(self.store.get(doc['id']), doc)

    def test_bad_uploads_and_paths_are_rejected(self):
        for name, content in [('../a.step', STEP), ('a\\b.step', STEP), ('a.pdf', STEP),
                              ('a.step', b'not step'), ('a.step', b''),
                              ('a.step', b'x' * (MAX_STEP_BYTES + 1))]:
            with self.assertRaises(RecordError):
                self.store.create(name, content)
        for document_id in ['../elsewhere', 'CAD-' + 'g' * 32, self.document['id'] + '/x']:
            with self.assertRaises(RecordError):
                self.store.get(document_id)
        self.assertEqual(len(self.store.list()), 1)

    def test_tampering_or_symlink_does_not_return_original(self):
        path = self.root / self.document['id'] / 'original.step'
        path.write_bytes(STEP + b'tampered')
        with self.assertRaises(RecordError):
            self.store.file(self.document['id'])
        path.unlink()
        target = self.root / 'elsewhere'
        target.write_bytes(STEP)
        path.symlink_to(target)
        with self.assertRaises(RecordError):
            self.store.get(self.document['id'])


class CadDocumentHTTPTests(unittest.TestCase):
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

    def request(self, path, body=None, headers=None):
        request = Request(self.base + path, data=body, headers=headers or {})
        return urlopen(request, timeout=5)

    def test_upload_source_snapshot_view_roundtrip_without_quote_tasks(self):
        headers = {'X-Workbench-Request': '1', 'Content-Type': 'application/octet-stream'}
        with self.request('/workbench/api/cad/documents?name=' + quote('测试机箱.step'), STEP, headers) as response:
            self.assertEqual(response.status, 201)
            doc = json.load(response)
        self.assertEqual(doc['sha256'], hashlib.sha256(STEP).hexdigest())
        headers['Content-Type'] = 'application/json'
        payload = {'expected_revision': 0, 'snapshot': snapshot(doc['sha256'])}
        with self.request('/workbench/api/cad/documents/' + doc['id'] + '/snapshot', json.dumps(payload).encode(), headers) as response:
            doc = json.load(response)
        payload = {'expected_revision': doc['revision'], 'view': {
            'selected_node_id': 'n-0-0', 'isolated_node_id': 'n-0-0', 'hidden_node_ids': []}}
        with self.request('/workbench/api/cad/documents/' + doc['id'] + '/view', json.dumps(payload).encode(), headers) as response:
            doc = json.load(response)
        with self.request('/workbench/api/cad/documents/' + doc['id']) as response:
            self.assertEqual(json.load(response), doc)
        with self.request(doc['file_url']) as response:
            self.assertEqual(response.read(), STEP)
            self.assertIn('attachment', response.headers['Content-Disposition'])
        with self.request('/workbench/api/cad/documents') as response:
            self.assertEqual(len(json.load(response)['documents']), 1)
        self.assertEqual(self.app.tasks(), [])
        with self.assertRaises(HTTPError) as caught:
            self.request('/workbench/api/cad/documents/' + doc['id'] + '/view', json.dumps(payload).encode(), headers)
        self.assertEqual(caught.exception.code, 409)

    def test_mutations_reject_cross_site_or_missing_marker(self):
        for headers in ({'Content-Type': 'application/octet-stream'},
                        {'Content-Type': 'application/octet-stream', 'X-Workbench-Request': '1',
                         'Origin': 'https://unrelated.example'}):
            with self.assertRaises(HTTPError) as caught:
                self.request('/workbench/api/cad/documents?name=test.step', STEP, headers)
            self.assertEqual(caught.exception.code, 403)
        self.assertEqual(self.server.cad.list(), [])

    def test_fixed_public_sample_is_exact_original(self):
        sample = ROOT / 'samples/public-enclosure-20261026/input/Enclosure-v3-v9.step'
        with self.request('/workbench/api/cad/samples/enclosure/file') as response:
            self.assertEqual(hashlib.sha256(response.read()).digest(), hashlib.sha256(sample.read_bytes()).digest())


if __name__ == '__main__':
    unittest.main()
