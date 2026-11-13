"""Read-only quote-to-native previews: identity, historical runs and honest gaps."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_service import BatchQuoteService
from workbench_store import WorkbenchError
from test_batch_quote_service import STEP, SETTINGS, zipped

PNG = b'\x89PNG\r\n\x1a\nfixture-registered-native-thumbnail'


def display_geometry(sha, offset=0):
    solids = []
    for index in range(1, 4):
        sid, x = f'S{index:04d}', offset + index * 10
        solids.append({'id': sid, 'name': 'Panel' if index < 3 else 'Bracket',
            'definition_id': 'PANEL/solid-1' if index < 3 else 'BRACKET/solid-1',
            'occurrence_path': ['assembly', str(index)], 'volume_mm3': 1000000,
            'area_mm2': 2.4, 'bounds_mm': {'min': [x, 0, 0], 'max': [x + 1, 1, 1], 'size': [1, 1, 1]},
            'topology': {'faces': 4, 'edges': 6, 'vertices': 4, 'valid': True, 'closed': True},
            'mesh': {'positions': [x, 0, 0, x+1, 0, 0, x, 1, 0, x, 0, 1],
                     'indices': [0, 1, 2, 0, 1, 3, 0, 2, 3, 1, 2, 3]},
            'thumbnail': sid + '.png', 'step_file': sid + '.step',
            'private_internal': '/private/never-export'})
    return {'schema_version': 1, 'source_sha256': sha, 'output_unit': 'mm', 'solids': solids}


class ExistingCad:
    def __init__(self):
        self.documents = {}

    def get(self, native_id):
        if native_id not in self.documents:
            raise FileNotFoundError(native_id)
        return copy.deepcopy(self.documents[native_id])

    def create(self, *args):
        raise AssertionError('Read-only preview must never create CAD documents')


class ExistingManufacturing:
    analyzer = None

    def __init__(self):
        self.geometries, self.files, self.geometry_calls = {}, {}, []

    def geometry(self, native_id):
        self.geometry_calls.append(native_id)
        return copy.deepcopy(self.geometries[native_id])

    def file(self, native_id, filename):
        return self.files[(native_id, filename)], 'image/png'

    def start(self, *args):
        raise AssertionError('Read-only preview must never start native work')


class BatchPartPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'batches'
        self.cad, self.manufacturing = ExistingCad(), ExistingManufacturing()
        self.processing_calls = 0

        def processor(row, content):
            self.processing_calls += 1
            sha = hashlib.sha256(content).hexdigest()
            native_id = 'CAD-' + sha[:32]
            geometry = display_geometry(sha, 100 if content.endswith(b'second') else 0)
            self.cad.documents[native_id] = {'id': native_id, 'sha256': sha}
            self.manufacturing.geometries[native_id] = geometry
            for solid in geometry['solids']:
                self.manufacturing.files[(native_id, solid['thumbnail'])] = PNG
            return geometry, native_id

        self.processor = processor
        self.service = BatchQuoteService(self.root, self.cad, self.manufacturing, processor=processor)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def quote(self, files=None, event='preview-upload'):
        state = self.service.create('order.zip', zipped(files or [('panel.stp', STEP)]), event)
        self.service.start(state['id'], {'expected_revision': state['revision'], 'request_id': event + '-start',
                                        'settings': copy.deepcopy(SETTINGS)})
        self.service.process_pending_once()
        state = self.service.get(state['id'])
        self.assertEqual(state['status'], 'completed')
        return state, state['result']['run_id']

    def saved(self, state, run_id):
        return self.root / state['id'] / 'runs' / (run_id + '.json')

    def receipt(self, state, file_id='F0001'):
        return self.root / state['id'] / 'processed' / (file_id + '.json')

    def native_id(self, state, file_id='F0001'):
        return json.loads(self.receipt(state, file_id).read_text())['native_id']

    def test_manifest_uses_registered_native_thumbnail_and_exact_source_group(self):
        state, run_id = self.quote()
        before = self.saved(state, run_id).read_bytes()
        manifest = self.service.parts_manifest(state['id'], run_id)
        self.assertEqual(manifest['run_id'], run_id)
        self.assertEqual([item['status'] for item in manifest['lines']], ['ready', 'ready'])
        panel = manifest['lines'][0]
        self.assertEqual(panel['solid_ids'], ['S0001', 'S0002'])
        self.assertEqual(panel['representative_solid_ids'], ['S0001'])
        self.assertEqual(panel['bounds_mm']['min'], [10, 0, 0])
        self.assertTrue(panel['thumbnail_url'].endswith('/parts/L0001/thumbnail'))
        self.assertTrue(panel['viewer_url'].startswith('/workbench/quotation/part/?preview=%2Fworkbench'))
        image, mime, name = self.service.part_thumbnail(state['id'], run_id, 'L0001')
        self.assertEqual((image, mime, name), (PNG, 'image/png', 'L0001.png'))
        self.assertEqual(self.saved(state, run_id).read_bytes(), before)
        self.assertEqual(self.processing_calls, 1)

    def test_detail_keeps_source_assembly_groups_and_original_quote_scalars(self):
        state, run_id = self.quote()
        geometry = self.manufacturing.geometries[self.native_id(state)]
        geometry['solids'][0]['mesh']['private_path'] = '/private/never-export'
        geometry['solids'][0]['topology']['private_path'] = '/private/never-export'
        geometry['solids'][0]['bounds_mm']['private_path'] = '/private/never-export'
        detail = self.service.part_geometry(state['id'], run_id, 'L0001')
        self.assertEqual(detail['selected_id'], 'L0001')
        self.assertEqual(detail['line_snapshot']['quantity_total'], 20)
        self.assertEqual(detail['line_snapshot']['total_price'], state['result']['lines'][0]['total_price'])
        self.assertEqual(detail['order_sets'], 10)
        self.assertEqual([row['id'] for row in detail['groups']], ['L0001', 'L0002'])
        self.assertEqual(len(detail['geometry']['solids']), 3)
        self.assertEqual(detail['occurrences'], [{'id': 'O0001', 'solid_ids': ['S0001']},
                                                {'id': 'O0002', 'solid_ids': ['S0002']}])
        encoded = json.dumps(detail)
        self.assertNotIn('private_internal', encoded)
        self.assertNotIn('never-export', encoded)
        self.assertNotIn('step_file', encoded)
        self.assertNotIn('material_pricing', detail['settings'])

    def test_representative_occurrence_keeps_all_solids_in_a_composite_part(self):
        def composite(row, content):
            geometry, native_id = self.processor(row, content)
            for index, original in enumerate(geometry['solids'][:2], 4):
                additional = copy.deepcopy(original)
                additional.update(id=f'S{index:04d}', definition_id='PANEL/solid-2', volume_mm3=500000,
                                  thumbnail=f'S{index:04d}.png', step_file=f'S{index:04d}.step')
                geometry['solids'].append(additional)
                self.manufacturing.files[(native_id, additional['thumbnail'])] = PNG
            return geometry, native_id
        self.service.processor = composite
        state, run_id = self.quote()
        detail = self.service.part_geometry(state['id'], run_id, 'L0001')
        self.assertEqual(detail['representative_solid_ids'], ['S0001', 'S0004'])
        self.assertEqual(detail['occurrences'][1]['solid_ids'], ['S0002', 'S0005'])
        self.assertEqual(detail['groups'][0]['solid_ids'], ['S0001', 'S0002', 'S0004', 'S0005'])
        self.assertEqual(detail['line_snapshot']['quantity_per_set'], 2)

    def test_scalar_synthetic_processor_without_a_native_document_is_unavailable(self):
        def scalar_only(row, content):
            return BatchQuoteService._measurements(display_geometry(hashlib.sha256(content).hexdigest()))
        self.service.processor = scalar_only
        state, run_id = self.quote()
        manifest = self.service.parts_manifest(state['id'], run_id)
        self.assertTrue(all(row['status'] == 'unavailable' for row in manifest['lines']))
        self.assertTrue(all(row['thumbnail_url'] is None and row['geometry_url'] is None for row in manifest['lines']))
        self.assertEqual(self.manufacturing.geometry_calls, [])

    def test_original_member_tampering_cannot_reuse_a_valid_native_cache(self):
        state, run_id = self.quote()
        member = self.root / state['id'] / 'files' / 'F0001.bin'
        member.write_bytes(b'x' * member.stat().st_size)
        self.assertEqual(self.service.parts_manifest(state['id'], run_id)['lines'][0]['status'], 'unavailable')

    def test_same_filename_and_definition_in_separate_files_never_cross_link(self):
        state, run_id = self.quote([('first/panel.stp', STEP), ('second/panel.stp', STEP + b'second')])
        first = self.service.part_geometry(state['id'], run_id, 'L0001')
        second = self.service.part_geometry(state['id'], run_id, 'L0003')
        self.assertNotEqual(first['source_sha256'], second['source_sha256'])
        self.assertEqual(first['geometry']['solids'][0]['bounds_mm']['min'][0], 10)
        self.assertEqual(second['geometry']['solids'][0]['bounds_mm']['min'][0], 110)
        self.assertEqual([row['id'] for row in second['groups']], ['L0003', 'L0004'])
        self.manufacturing.geometry_calls.clear()
        self.service.part_geometry(state['id'], run_id, 'L0003')
        self.assertEqual(set(self.manufacturing.geometry_calls), {self.native_id(state, 'F0002')})

    def test_missing_thumbnail_keeps_real_3d_available_without_a_fake_image(self):
        state, run_id = self.quote()
        self.manufacturing.files.pop((self.native_id(state), 'S0001.png'))
        panel = self.service.parts_manifest(state['id'], run_id)['lines'][0]
        self.assertEqual(panel['status'], 'ready')
        self.assertIsNone(panel['thumbnail_url'])
        self.assertIsNotNone(panel['geometry_url'])
        with self.assertRaises(WorkbenchError) as rejected:
            self.service.part_thumbnail(state['id'], run_id, 'L0001')
        self.assertEqual(rejected.exception.status, 404)

    def test_old_scalar_cache_can_only_reuse_an_existing_sha_identical_document(self):
        state, run_id = self.quote()
        path = self.receipt(state)
        receipt = json.loads(path.read_text())
        receipt.pop('native_id')
        receipt['geometry'] = BatchQuoteService._measurements(self.manufacturing.geometries[self.native_id(state)])
        path.write_text(json.dumps(receipt))
        self.assertEqual(self.service.parts_manifest(state['id'], run_id)['lines'][0]['status'], 'ready')
        self.cad.documents.clear()
        panel = self.service.parts_manifest(state['id'], run_id)['lines'][0]
        self.assertEqual(panel['status'], 'unavailable')
        self.assertIsNone(panel['geometry_url'])
        self.assertEqual(self.processing_calls, 1)

    def test_mismatched_receipt_source_document_or_mesh_is_unavailable(self):
        state, run_id = self.quote()
        native_id = self.native_id(state)
        original_receipt = self.receipt(state).read_text()
        original_geometry = copy.deepcopy(self.manufacturing.geometries[native_id])
        for mutation in ('receipt_sha', 'native_id', 'geometry_sha', 'bad_mesh'):
            self.receipt(state).write_text(original_receipt)
            self.manufacturing.geometries[native_id] = copy.deepcopy(original_geometry)
            if mutation in {'receipt_sha', 'native_id'}:
                value = json.loads(original_receipt)
                value['sha256' if mutation == 'receipt_sha' else 'native_id'] = 'b' * 64 if mutation == 'receipt_sha' else '../outside'
                self.receipt(state).write_text(json.dumps(value))
            elif mutation == 'geometry_sha':
                self.manufacturing.geometries[native_id]['source_sha256'] = 'b' * 64
            else:
                self.manufacturing.geometries[native_id]['solids'][0]['mesh']['indices'] = [999, 0, 1]
            with self.subTest(mutation=mutation):
                panel = self.service.parts_manifest(state['id'], run_id)['lines'][0]
                self.assertEqual(panel['status'], 'unavailable')
                with self.assertRaises(WorkbenchError):
                    self.service.part_geometry(state['id'], run_id, 'L0001')

    def test_changed_instance_group_counts_and_ambiguous_quote_definition_are_rejected(self):
        state, run_id = self.quote()
        path = self.saved(state, run_id)
        original = json.loads(path.read_text())
        for key, value in [('native_solid_count', 1), ('quantity_per_set', 1), ('volume_mm3', '999')]:
            changed = copy.deepcopy(original)
            changed['lines'][0][key] = value
            path.write_text(json.dumps(changed))
            with self.subTest(key=key):
                self.assertEqual(self.service.parts_manifest(state['id'], run_id)['lines'][0]['status'], 'unavailable')
        changed = copy.deepcopy(original)
        changed['lines'][1]['native_definition'] = changed['lines'][0]['native_definition']
        path.write_text(json.dumps(changed))
        self.assertTrue(all(row['status'] == 'unavailable' for row in self.service.parts_manifest(state['id'], run_id)['lines']))

    def test_history_uses_requested_run_without_replacing_its_json_or_amounts(self):
        state, old_run = self.quote()
        old_json = self.service.artifact(state['id'], old_run, 'quote.json')[0]
        updated = copy.deepcopy(SETTINGS)
        updated['order_sets'] = 20
        self.service.start(state['id'], {'expected_revision': state['revision'], 'request_id': 'second-run', 'settings': updated})
        self.service.process_pending_once()
        newer = self.service.get(state['id'])
        new_run = newer['result']['run_id']
        self.assertNotEqual(old_run, new_run)
        self.assertEqual(self.service.part_geometry(state['id'], old_run, 'L0001')['order_sets'], 10)
        self.assertEqual(self.service.part_geometry(state['id'], new_run, 'L0001')['order_sets'], 20)
        self.assertEqual(old_json, self.service.artifact(state['id'], old_run, 'quote.json')[0])
        self.assertEqual(self.processing_calls, 1)

    def test_path_unknown_run_cross_batch_and_symlinks_are_rejected(self):
        state, run_id = self.quote()
        for batch, run, line in [('../outside', run_id, 'L0001'), (state['id'], '../outside', 'L0001'),
                                  (state['id'], run_id, '../outside'), (state['id'], 'RUN-' + 'b'*32, 'L0001'),
                                  (state['id'], run_id, 'L9999')]:
            with self.subTest(batch=batch, run=run, line=line), self.assertRaises(WorkbenchError):
                self.service.part_geometry(batch, run, line)
        other, _ = self.quote(event='other-batch')
        with self.assertRaises(WorkbenchError):
            self.service.parts_manifest(other['id'], run_id)
        receipt = self.receipt(state)
        outside = Path(self.temp.name) / 'outside.json'
        outside.write_bytes(receipt.read_bytes())
        receipt.unlink()
        receipt.symlink_to(outside)
        self.assertEqual(self.service.parts_manifest(state['id'], run_id)['lines'][0]['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
