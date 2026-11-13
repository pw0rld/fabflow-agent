"""Protected saved-case fixtures and source checks; run only on ZeroClave.

All meshes and engineering evidence in this module are labelled test data.
No CAD executable, network or model is used.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from prep_record import atomic_write, digest
from workbench_store import WorkbenchError
from batch_quote_cases import API_PREFIX, ProtectedCaseQuoteService, case_links, make_case_manifest
from batch_quote_costing import calculate_batch

RAW_CAD_NOTE = 'CAD 原文保持 /workbench/api/quote-batches/example ，不能做字符串替换。'
BATCH = 'BATCH-' + 'b' * 32
RUN = 'RUN-' + 'c' * 32
PNG = b'\x89PNG\r\n\x1a\nexplicit-test-fixture'


def write_json(path, value):
    atomic_write(Path(path), json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n')


def seed_case(service):
    """Create one independent imported case without using an upload/CAD worker."""
    folder = service._folder(BATCH)
    (folder / 'runs').mkdir(parents=True)
    (folder / 'files').mkdir()
    native_bytes, drawing_bytes = b'ISO-10303-21; explicitly synthetic', PNG
    archive_sha = hashlib.sha256(b'explicit synthetic archive').hexdigest()
    native_sha = hashlib.sha256(native_bytes).hexdigest()
    source = {'id': 'F0001', 'name': 'fixture.stp', 'relative_path': 'fixture.stp', 'kind': 'step',
              'sha256': native_sha, 'size': len(native_bytes), 'status': 'ready', 'error': None}
    reference = {'id': 'F0002', 'name': 'fixture.png', 'relative_path': 'fixture.png', 'kind': 'image',
                 'sha256': hashlib.sha256(drawing_bytes).hexdigest(), 'size': len(drawing_bytes),
                 'status': 'reference', 'error': None}
    solids = []
    for index in range(1, 4):
        solids.append({'id': 'S' + str(index).zfill(4), 'name': 'Plate' if index < 3 else 'M4x20',
            'definition_id': 'plate/solid-1' if index < 3 else 'screw/solid-1',
            'occurrence_path': ['root', str(index)], 'volume_mm3': 1000000 if index < 3 else 1000,
            'area_mm2': 10000, 'bounds_mm': {'min': [0, 0, 0], 'max': [100, 100, 10], 'size': [100, 100, 10]},
            'topology': {'faces': 6, 'edges': 12, 'vertices': 8, 'valid': True, 'closed': True},
            'mesh': {'positions': [0, 0, 0, 100, 0, 0, 0, 100, 0], 'indices': [0, 1, 2]}})
    geometry = {'schema_version': 1, 'output_unit': 'mm', 'source_sha256': native_sha, 'solids': solids}
    settings = {'order_sets': 10, 'pricing_method': 'cost_plus', 'rate_percent': '20',
                'material_profile': 'carbon_steel', 'rate_profile': 'demo-v1',
                'demo_confirmed': True, 'scope_confirmed': True}
    original = calculate_batch([{**source, 'geometry': geometry}, reference], settings)
    original.update(batch_id=BATCH, run_id=RUN, source_sha256=archive_sha,
                    created_at='2026-11-09T00:00:00Z', links=case_links(BATCH, RUN))
    original['lines'][0]['notes'].append(RAW_CAD_NOTE)
    state = {'id': BATCH, 'name': 'Explicit fixture.zip', 'source_sha256': archive_sha,
             'status': 'completed', 'revision': 1, 'created_at': original['created_at'],
             'updated_at': original['created_at'], 'settings': copy.deepcopy(original['settings']),
             'files': [copy.deepcopy(source), copy.deepcopy(reference)], 'result': copy.deepcopy(original),
             'runs': [{'id': RUN, 'created_at': original['created_at'], 'links': copy.deepcopy(original['links']),
                       'total_price': original['totals']['total_price'], 'status': 'pending_review'}],
             'progress': {'total': 1, 'done': 1, 'current': None, 'message': '显式测试夹具'}, '_job': None}
    write_json(folder / 'runs' / (RUN + '.json'), original)
    write_json(folder / 'state.json', state)
    (folder / 'files' / 'F0001.bin').write_bytes(native_bytes)
    (folder / 'files' / 'F0002.bin').write_bytes(drawing_bytes)
    cache = folder / '_case-preview'
    (cache / 'geometry').mkdir(parents=True)
    (cache / 'parts').mkdir()
    write_json(cache / 'geometry/F0001.json', geometry)
    matches = {line['id']: service._preview_match(line, geometry, original['lines']) for line in original['lines']}
    groups = [{'id': line['id'], 'name': line['part_name'], 'solid_ids': matches[line['id']]['solid_ids']}
              for line in original['lines']]
    previews = []
    for line in original['lines']:
        match = matches[line['id']]
        data = {'batch_id': BATCH, 'run_id': RUN, 'line_id': line['id'], 'source_sha256': native_sha,
                'part_name': line['part_name'], 'groups': copy.deepcopy(groups), **copy.deepcopy(match),
                'selected_id': line['id'], 'line_snapshot': copy.deepcopy(line),
                'settings': copy.deepcopy(settings), 'order_sets': 10,
                'shared_geometry_url': '/old-static/geometry/F0001.json'}
        write_json(cache / 'parts' / (line['id'] + '.json'), data)
        (cache / 'parts' / line['id']).mkdir()
        (cache / 'parts' / line['id'] / 'thumbnail.png').write_bytes(PNG)
        previews.append({'line_id': line['id'], 'file_id': 'F0001', 'status': 'ready', **copy.deepcopy(match),
                         'geometry_url': '/old-static/parts/' + line['id'] + '.json',
                         'viewer_url': '/old-static/part.html', 'thumbnail_url': '/old-static/thumb.png'})
    write_json(cache / 'part-previews.json', {'batch_id': BATCH, 'run_id': RUN, 'lines': previews})
    manifest = make_case_manifest(original, cache, original_source_digest=digest(original), original_run_id=RUN)
    write_json(cache / 'manifest.json', manifest)
    # Candidate evidence must remain pending after the quote is modified.
    recognition = {'id': 'ENG-' + 'd' * 32, 'run_id': RUN, 'source_digest': digest(original),
        'status': 'completed', 'stage': 'completed', 'updated_at': original['created_at'], 'native': None,
        'partial': False, '_events': [], 'ai': {'lines': {'L0001': {
            'values': {'material_grade': 'Q235B'}, 'field_states': {'material_grade': 'candidate'},
            'evidence': {'material_grade': [{'reference': 'F0002', 'page': 1, 'quote': '测试 Q235B 原文',
                'region': '显式合成说明', 'basis': 'ai_document', 'status': 'pending_review'}]}, 'notes': []}},
            'provenance': {'kind': 'explicit_test_fixture', 'status': 'completed'}}}
    write_json(folder / 'runs' / (RUN + '-engineering-recognition.json'), recognition)
    return original, cache, manifest


class ProtectedCaseServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyl-protected-case-')
        self.service = ProtectedCaseQuoteService(Path(self.temporary.name) / 'private')
        self.original, self.cache, self.manifest = seed_case(self.service)

    def tearDown(self):
        self.service.close()
        self.temporary.cleanup()

    def test_manifest_helper_is_read_only_and_records_original_provenance(self):
        before = {str(path.relative_to(self.cache)): path.read_bytes()
                  for path in self.cache.rglob('*') if path.is_file()}
        generated = make_case_manifest(self.original, self.cache, original_source_digest=digest(self.original), original_run_id=RUN)
        self.assertEqual(generated, self.manifest)
        self.assertEqual(len(generated['asset_sha256']), 6)
        self.assertEqual({str(path.relative_to(self.cache)): path.read_bytes()
                          for path in self.cache.rglob('*') if path.is_file()}, before)
        self.assertEqual(self.service.metadata()['entry_batch_id'], BATCH)
        self.assertEqual(self.service.metadata()['api_prefix'], API_PREFIX)

    def test_cached_geometry_is_inline_bound_and_current_financial_snapshot_is_preserved(self):
        response = self.service.part_geometry(BATCH, RUN, 'L0001')
        self.assertEqual(response['line_snapshot'], self.original['lines'][0])
        self.assertEqual(response['order_sets'], 10)
        self.assertEqual(response['solid_ids'], ['S0001', 'S0002'])
        self.assertNotIn('shared_geometry_url', response)
        self.assertEqual(response['geometry']['source_sha256'], self.original['source_files'][0]['sha256'])
        self.assertEqual(self.service.part_thumbnail(BATCH, RUN, 'L0001')[0], PNG)
        row = self.service.parts_manifest(BATCH, RUN)['lines'][0]
        self.assertTrue(row['geometry_url'].startswith(API_PREFIX + '/'))
        self.assertEqual(row['thumbnail_url'], row['geometry_url'] + '/thumbnail')
        self.assertNotIn('/old-static/', json.dumps(row))
        self.assertIsNone(self.service.cad)
        self.assertIsNone(self.service.manufacturing)

    def test_asset_hash_changes_are_rejected_without_repair_or_cad_fallback(self):
        path = self.cache / 'parts/L0001.json'
        path.write_text(path.read_text() + ' ')
        with self.assertRaises(WorkbenchError):
            self.service.part_geometry(BATCH, RUN, 'L0001')
        self.assertTrue(path.read_text().endswith(' '))

    def test_symlink_cache_and_source_mismatch_are_rejected(self):
        path = self.cache / 'parts/L0001/thumbnail.png'
        copy_path = self.cache / 'image-copy.png'
        copy_path.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy_path)
        with self.assertRaises(WorkbenchError):
            self.service.part_thumbnail(BATCH, RUN, 'L0001')
        changed = copy.deepcopy(self.manifest)
        changed['line_identities']['L0001']['native_definition'] = 'other-part'
        write_json(self.cache / 'manifest.json', changed)
        with self.assertRaises(WorkbenchError):
            self.service.get(BATCH)

    def test_cross_case_asset_identity_is_rejected_even_with_a_valid_asset_hash(self):
        path = self.cache / 'parts/L0001.json'
        data = json.loads(path.read_text())
        data['batch_id'] = 'BATCH-' + 'e' * 32
        write_json(path, data)
        manifest = copy.deepcopy(self.manifest)
        manifest['asset_sha256']['parts/L0001.json'] = hashlib.sha256(path.read_bytes()).hexdigest()
        write_json(self.cache / 'manifest.json', manifest)
        with self.assertRaises(WorkbenchError):
            self.service.part_geometry(BATCH, RUN, 'L0001')

    def test_non_private_generated_links_are_rejected_but_cad_original_text_is_unchanged(self):
        result = self.service.get(BATCH)['result']
        self.assertIn(RAW_CAD_NOTE, result['lines'][0]['notes'])
        self.assertTrue(all(link.startswith(API_PREFIX + '/') for link in result['links'].values()))
        base = copy.deepcopy(self.original)
        base['links']['quote_url'] = '/workbench/api/quote-batches/private-leak'
        write_json(self.service._folder(BATCH) / 'runs' / (RUN + '.json'), base)
        with self.assertRaises(WorkbenchError):
            self.service.artifact(BATCH, RUN, 'quote.json')

    def test_case_service_never_starts_upload_native_or_recognition_work(self):
        for name in ('create', 'start', 'retry', 'recognize_engineering', 'price_engineering'):
            with self.subTest(name=name), self.assertRaises(WorkbenchError) as error:
                getattr(self.service, name)(BATCH, {})
            self.assertEqual(error.exception.status, 405)
        self.service.start_worker()
        self.assertIsNone(self.service._thread)
        self.assertIsNone(self.service.engineering_recognition._thread)


if __name__ == '__main__':
    unittest.main()
