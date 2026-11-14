"""Source-bound small preview indexes; run only on the remote test server.

Synthetic protocol fixtures are not manufacturing accuracy evidence. These
tests call neither a CAD engine nor a model service.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_service import BatchQuoteService
from cad_documents import CadDocumentStore
from cad_manufacturing import CadManufacturingService, validate_geometry
from prep_record import RecordError
from workbench_store import WorkbenchError
from test_batch_part_preview import display_geometry, PNG
from test_batch_quote_service import STEP, SETTINGS, zipped


class PreviewGeometryIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='preview-index-')
        self.root = Path(self.temp.name)
        self.cad = CadDocumentStore(self.root / 'cad')
        self.document = self.cad.create('synthetic.step', STEP)
        self.native = CadManufacturingService(self.cad)
        self.geometry_path = self.publish('R' + '1' * 32)
        self.batch = None

    def tearDown(self):
        if self.batch is not None:
            self.batch.close()
        self.temp.cleanup()

    def publish(self, run_id, geometry=None):
        """Register a synthetic artifact run without starting any worker."""
        value = copy.deepcopy(geometry if geometry is not None else display_geometry(self.document['sha256']))
        folder = self.native._folder(self.document['id']) / 'runs' / run_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'geometry.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        files = {'geometry.json': hashlib.sha256(path.read_bytes()).hexdigest()}
        for solid in value['solids']:
            for filename, content in ((solid['thumbnail'], PNG), (solid['step_file'], STEP)):
                (folder / filename).write_bytes(content)
                files[filename] = hashlib.sha256(content).hexdigest()
        state = self.native._default(self.document['id'], self.document['sha256'])
        state.update(status='ready', stage='ready', geometry={'run_id': run_id, 'files': files})
        self.native._save(state)
        return path

    def quote(self):
        def processor(row, content):
            self.assertEqual(hashlib.sha256(content).hexdigest(), self.document['sha256'])
            return self.native.geometry(self.document['id']), self.document['id']
        self.batch = BatchQuoteService(self.root / 'batches', self.cad, self.native, processor=processor)
        state = self.batch.create('synthetic.zip', zipped([('synthetic.stp', STEP)]), 'index-upload')
        self.batch.start(state['id'], {'expected_revision': state['revision'], 'request_id': 'index-start',
                                       'settings': copy.deepcopy(SETTINGS)})
        with patch.object(self.batch, '_enqueue_engineering'):
            self.batch.process_pending_once()
        state = self.batch.get(state['id'])
        self.assertEqual(state['status'], 'completed', state.get('error'))
        return state, state['result']['run_id']

    def test_repeated_index_reads_parse_once_and_never_cache_meshes(self):
        state_path = self.native._folder(self.document['id']) / 'state.json'
        before = (state_path.read_bytes(), self.geometry_path.read_bytes())
        with patch('cad_manufacturing.validate_geometry', wraps=validate_geometry) as validate:
            first = self.native.preview_index(self.document['id'])
            first['solids'][0]['name'] = 'caller-local change'
            with patch.object(self.native, '_artifact_for_state', side_effect=AssertionError('hit reread full bytes')):
                second = self.native.preview_index(self.document['id'])
            self.assertEqual(validate.call_count, 1)
        self.assertEqual(second['solids'][0]['name'], 'Panel')
        self.assertNotIn('mesh', json.dumps(second))
        self.assertNotIn('topology', json.dumps(list(self.native._preview_indexes.values())))
        self.assertNotIn('private_internal', json.dumps(second))
        self.assertEqual((state_path.read_bytes(), self.geometry_path.read_bytes()), before)

    def test_full_geometry_read_warms_only_the_small_index(self):
        with patch('cad_manufacturing.validate_geometry', wraps=validate_geometry) as validate:
            full = self.native.geometry(self.document['id'])
            self.assertIn('mesh', full['solids'][0])
            index = self.native.preview_index(self.document['id'])
            self.assertEqual(validate.call_count, 1)
        self.assertNotIn('mesh', index['solids'][0])

    def test_concurrent_cold_reads_have_one_parse(self):
        barrier = threading.Barrier(6)
        def read(_):
            barrier.wait(timeout=10)
            return self.native.preview_index(self.document['id'])
        with patch('cad_manufacturing.validate_geometry', wraps=validate_geometry) as validate:
            with ThreadPoolExecutor(max_workers=6) as pool:
                values = list(pool.map(read, range(6)))
            self.assertEqual(validate.call_count, 1)
        self.assertTrue(all(value == values[0] for value in values))

    def test_cached_index_does_not_hide_disk_geometry_or_original_tampering(self):
        self.native.preview_index(self.document['id'])
        original_geometry = self.geometry_path.read_bytes()
        self.geometry_path.write_bytes(original_geometry.replace(b'Panel', b'Badly', 1))
        with self.assertRaises(RecordError):
            self.native.preview_index(self.document['id'])
        self.geometry_path.write_bytes(original_geometry)
        (self.cad._folder(self.document['id']) / 'original.step').write_bytes(b'x' * len(STEP))
        with self.assertRaises(RecordError):
            self.native.preview_index(self.document['id'])

    def test_new_run_and_new_registered_digest_invalidate_old_index(self):
        with patch('cad_manufacturing.validate_geometry', wraps=validate_geometry) as validate:
            first = self.native.preview_index(self.document['id'])
            self.publish('R' + '2' * 32)
            second = self.native.preview_index(self.document['id'])
            changed = display_geometry(self.document['sha256'])
            changed['solids'][0]['name'] = 'new registered artifact'
            self.publish('R' + '2' * 32, changed)
            third = self.native.preview_index(self.document['id'])
            self.assertEqual(validate.call_count, 3)
        self.assertNotEqual(first['preview_binding'], second['preview_binding'])
        self.assertNotEqual(second['preview_binding'], third['preview_binding'])
        self.assertEqual(third['solids'][0]['name'], 'new registered artifact')
        with self.assertRaises(RecordError):
            self.native.file(self.document['id'], 'S0001.png', expected_geometry=first['preview_binding'])

    def test_new_registered_artifact_still_requires_valid_mesh_and_source(self):
        self.native.preview_index(self.document['id'])
        for mutation in ('source', 'mesh'):
            value = display_geometry(self.document['sha256'])
            if mutation == 'source':
                value['source_sha256'] = '0' * 64
            else:
                value['solids'][0]['mesh']['indices'][0] = 999
            self.publish('R' + '2' * 32, value)
            with self.subTest(mutation=mutation), self.assertRaises(RecordError):
                self.native.preview_index(self.document['id'])

    def test_cache_is_bounded_and_evicts_the_oldest_source_run(self):
        with patch('cad_manufacturing.PREVIEW_INDEX_CACHE_SIZE', 2):
            for index in range(1, 4):
                self.publish('R' + str(index) * 32)
                self.native.preview_index(self.document['id'])
            self.assertEqual(len(self.native._preview_indexes), 2)
            self.assertEqual({key[2] for key in self.native._preview_indexes},
                             {'R' + '2' * 32, 'R' + '3' * 32})

    def test_batch_thumbnail_uses_index_and_checks_registered_image_hash(self):
        state, run_id = self.quote()
        with patch.object(self.native, 'geometry', side_effect=AssertionError('thumbnail parsed full mesh')), \
                patch('cad_manufacturing.validate_geometry', side_effect=AssertionError('warm thumbnail revalidated mesh')):
            image, mime, name = self.batch.part_thumbnail(state['id'], run_id, 'L0001')
            self.assertEqual((image, mime, name), (PNG, 'image/png', 'L0001.png'))
            thumbnail = self.geometry_path.parent / 'S0001.png'
            thumbnail.write_bytes(PNG + b'tampered')
            with self.assertRaises(WorkbenchError):
                self.batch.part_thumbnail(state['id'], run_id, 'L0001')

    def test_batch_thumbnail_still_checks_original_member_identity(self):
        state, run_id = self.quote()
        member = self.batch.root / state['id'] / 'files' / 'F0001.bin'
        member.write_bytes(b'x' * member.stat().st_size)
        with self.assertRaises(WorkbenchError):
            self.batch.part_thumbnail(state['id'], run_id, 'L0001')


if __name__ == '__main__':
    unittest.main()
