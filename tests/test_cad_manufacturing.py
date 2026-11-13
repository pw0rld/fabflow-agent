"""Isolated protocol/storage checks; synthetic geometry is not an accuracy claim."""
import base64
import copy
import csv
import hashlib
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cad_documents import CadDocumentStore
from cad_manufacturing import (CadManufacturingService, fallback_groups, group_thickness_summary,
                               validate_geometry, validate_thickness)
from prep_record import RecordError
from workbench_store import Workbench, WorkbenchError
from workbench_server import make_server
from test_cad_bom import material

STEP = b'ISO-10303-21;\nHEADER;ENDSEC;DATA;ENDSEC;END-ISO-10303-21;'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jL1kAAAAASUVORK5CYII=')


def fixture_geometry(sha):
    solids = []
    for index in (1, 2):
        sid = f'S{index:04d}'
        solids.append({'id': sid, 'name': '测试实体 ' + str(index), 'definition_id': None,
            'occurrence_path': ['fixture', str(index)],
            'topology': {'faces': 4, 'edges': 6, 'vertices': 4, 'valid': True, 'closed': True},
            'bounds_mm': {'min': [0, 0, 0], 'max': [1, 1, 1], 'size': [1, 1, 1]},
            'area_mm2': 2.4, 'volume_mm3': 1 / 6,
            'mesh': {'positions': [0,0,0, 1,0,0, 0,1,0, 0,0,1],
                     'indices': [0,1,2, 0,1,3, 0,2,3, 1,2,3]},
            'thumbnail': sid + '.png', 'step_file': sid + '.step'})
    return {'schema_version': 1, 'source_sha256': sha, 'engine': {'name': 'fixture'},
            'output_unit': 'mm', 'source_units': {'fixture': True}, 'solids': solids,
            'warnings': [], 'contact_sheets': []}


def native_fixture(source, output):
    data = fixture_geometry(hashlib.sha256(source.read_bytes()).hexdigest())
    (output / 'geometry.json').write_text(json.dumps(data))
    for solid in data['solids']:
        (output / solid['thumbnail']).write_bytes(PNG)
        (output / solid['step_file']).write_bytes(STEP)


def thickness_fixture(value=1.0):
    return {'status': 'candidate', 'value_mm': value, 'method': 'parallel_faces', 'confidence': 'high',
            'reason': '两个对应平面之间的材料厚度，仍待核对。', 'evidence': [
                {'kind': 'parallel_faces', 'face_ids': ['F0001', 'F0002'],
                 'thickness_mm': value, 'support_area_mm2': 1.0}], 'warnings': []}


def native_thickness_fixture(source, output):
    native_fixture(source, output)
    path = output / 'geometry.json'
    data = json.loads(path.read_text())
    for solid in data['solids']:
        solid['thickness'] = thickness_fixture()
    path.write_text(json.dumps(data))


def native_properties_fixture(source, output):
    native_thickness_fixture(source, output)
    path = output / 'geometry.json'
    data = json.loads(path.read_text())
    for solid in data['solids']:
        solid['material'] = material()
        solid['definition_id'] = '0:1:1:2/solid-1'
    path.write_text(json.dumps(data))


def native_identity_fixture(source, output):
    native_fixture(source, output)
    path = output / 'geometry.json'
    data = json.loads(path.read_text())
    for solid in data['solids']:
        solid['definition_id'] = '0:1:1:2/solid-1'
    path.write_text(json.dumps(data))


def ready(service, document_id):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        state = service.get(document_id)
        if state['status'] in {'ready', 'failed'}:
            return state
        time.sleep(.01)
    raise AssertionError('Worker did not finish')


class ManufacturingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CadDocumentStore(Path(self.temp.name) / 'cad')
        self.doc = self.store.create('fixture.step', STEP)
        self.service = CadManufacturingService(self.store, native_runner=native_fixture)

    def tearDown(self):
        self.temp.cleanup()

    def analyze(self):
        first = self.service.start(self.doc['id'], {'expected_revision': 0})
        self.assertIn(first['status'], {'queued', 'running'})
        result = ready(self.service, self.doc['id'])
        self.assertEqual(result['status'], 'ready', result)
        return result

    def test_raw_geometry_is_independent_of_preview_and_no_ai_is_claimed(self):
        state = self.analyze()
        self.assertEqual(len(state['groups']), 2)
        self.assertEqual(state['ai']['status'], 'not_requested')
        self.assertTrue(state['warnings'])
        self.assertTrue(all(g['quantity_per_set'] is None and g['material'] is None for g in state['groups']))
        self.assertEqual(self.service.geometry(self.doc['id'])['source_sha256'], self.doc['sha256'])
        self.assertIsNone(self.store.get(self.doc['id'])['snapshot'])
        self.assertEqual(self.store.file(self.doc['id'])[0], STEP)

    def test_review_merge_split_persists_but_does_not_change_geometry(self):
        state = self.analyze()
        before = self.service.geometry(self.doc['id'])
        merged = copy.deepcopy(state['groups'][0])
        merged.update(id='G-merged', name='人工分组', solid_ids=['S0001', 'S0002'],
                      classification='assembly', status='confirmed', quantity_per_set=1)
        updated = self.service.review(self.doc['id'], {'expected_revision': state['revision'], 'groups': [merged]})
        self.assertEqual(len(updated['groups']), 1)
        self.assertEqual(self.service.geometry(self.doc['id']), before)
        again = CadManufacturingService(self.store, native_runner=native_fixture).get(self.doc['id'])
        self.assertEqual(again['groups'], updated['groups'])
        self.assertTrue(list(self.service._folder(self.doc['id']).glob('review-*.json')))
        with self.assertRaises(RecordError):
            self.service.start(self.doc['id'], {'retry': True})

    def test_invalid_groups_and_stale_revision_leave_previous_records(self):
        state = self.analyze()
        for mutation in ('duplicate', 'unknown', 'missing', 'zero', 'nan'):
            rows = copy.deepcopy(state['groups'])
            if mutation == 'duplicate': rows[1]['solid_ids'] = ['S0001']
            if mutation == 'unknown': rows[1]['solid_ids'] = ['S9999']
            if mutation == 'missing': rows.pop()
            if mutation == 'zero': rows[0]['quantity_per_set'] = 0
            if mutation == 'nan': rows[0]['thickness_mm'] = float('nan')
            with self.subTest(mutation=mutation), self.assertRaises(RecordError):
                self.service.review(self.doc['id'], {'expected_revision': state['revision'], 'groups': rows})
        with self.assertRaises(WorkbenchError):
            self.service.review(self.doc['id'], {'expected_revision': 0, 'groups': state['groups']})
        with self.assertRaises(RecordError):
            self.service.review(self.doc['id'], {'expected_revision': None, 'groups': state['groups']})
        self.assertEqual(self.service.get(self.doc['id'])['groups'], state['groups'])

    def test_model_failure_retains_native_candidates(self):
        self.service.analyzer = lambda *_: (_ for _ in ()).throw(RuntimeError('offline model'))
        state = self.analyze()
        self.assertEqual(state['ai']['status'], 'unavailable')
        self.assertEqual(state['geometry_summary']['solid_count'], 2)
        self.assertEqual(len(state['groups']), 2)

    def test_model_cannot_claim_confirmed_measurements_or_unknown_solids(self):
        def analyzer(_output, geometry):
            groups = [{'id': 'AI01', 'name': '模型建议', 'solid_ids': ['S0001', 'S0002'],
                       'classification': 'assembly', 'status': 'confirmed', 'quantity_per_set': 9,
                       'material': 'guessed steel', 'thickness_mm': 5, 'notes': '测试模型建议'}]
            return {'groups': groups, 'warnings': [], 'provenance': {'kind': 'fixture', 'status': 'completed'}}
        self.service.analyzer = analyzer
        state = self.analyze()
        self.assertEqual(state['ai']['status'], 'completed')
        row = state['groups'][0]
        self.assertEqual(row['status'], 'pending_review')
        self.assertIsNone(row['quantity_per_set'])
        self.assertIsNone(row['material'])
        self.assertIsNone(row['thickness_mm'])

    def test_native_failure_is_not_ready_and_recovery_is_explicit(self):
        self.service.native_runner = lambda *_: (_ for _ in ()).throw(RuntimeError('native error'))
        self.service.start(self.doc['id'], {})
        state = ready(self.service, self.doc['id'])
        self.assertEqual(state['status'], 'failed')
        self.assertFalse(state['groups'])
        with self.store._lock(self.doc['id']):
            raw = self.service._read(self.doc['id'], self.doc['sha256'])
            raw.update(status='running', stage='geometry')
            self.service._save(raw)
        recovered = CadManufacturingService(self.store).get(self.doc['id'])
        self.assertEqual(recovered['status'], 'failed')
        self.assertIn('服务中断', recovered['error'])

    def test_registered_files_and_draft_csv(self):
        state = self.analyze()
        data, mime = self.service.file(self.doc['id'], 'S0001.png')
        self.assertEqual((data, mime), (PNG, 'image/png'))
        for name in ('input.step', '../input.step', 'geometry.json', 'S9999.step'):
            with self.assertRaises(RecordError): self.service.file(self.doc['id'], name)
        rows = copy.deepcopy(state['groups'])
        rows[0]['name'] = '=dangerous-cell'
        rows[1].update(name='EXCLUDED-CANDIDATE', classification='exclude')
        self.service.review(self.doc['id'], {'expected_revision': state['revision'], 'groups': rows})
        csv = self.service.bom(self.doc['id']).decode('utf-8-sig')
        self.assertIn('物料图表草稿', csv)
        self.assertIn("'=dangerous-cell", csv)
        self.assertIn('待确认', csv)
        self.assertNotIn('EXCLUDED-CANDIDATE', csv)

    def test_single_active_job(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def runner(source, output):
            calls.append(1); entered.set(); release.wait(3); native_fixture(source, output)
        self.service.native_runner = runner
        first = self.service.start(self.doc['id'], {})
        entered.wait(1)
        second = self.service.start(self.doc['id'], {})
        self.assertEqual(first['job_id'], second['job_id'])
        release.set(); self.assertEqual(ready(self.service, self.doc['id'])['status'], 'ready')
        self.assertEqual(len(calls), 1)

    def test_native_kernel_is_serialized_across_documents(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def runner(source, output):
            calls.append(str(source))
            if len(calls) == 1:
                entered.set(); release.wait(3)
            native_fixture(source, output)
        self.service.native_runner = runner
        other = self.store.create('second.step', STEP + b'\n/* second source */')
        self.service.start(self.doc['id'], {})
        self.assertTrue(entered.wait(1))
        self.service.start(other['id'], {})
        time.sleep(.04)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.service.get(other['id'])['status'], 'queued')
        release.set()
        self.assertEqual(ready(self.service, self.doc['id'])['status'], 'ready')
        self.assertEqual(ready(self.service, other['id'])['status'], 'ready')
        self.assertEqual(len(calls), 2)

    def test_initial_thickness_is_evidence_not_adopted_parameter(self):
        self.service.native_runner = native_thickness_fixture
        state = self.analyze()
        self.assertEqual(set(state['thickness_measurements']), {'S0001', 'S0002'})
        self.assertEqual(state['thickness_measurements']['S0001']['value_mm'], 1)
        self.assertTrue(all(group['thickness_mm'] is None for group in state['groups']))
        self.assertEqual(self.service.geometry(self.doc['id'])['solids'][0]['thickness'], thickness_fixture())
        restored = CadManufacturingService(self.store).get(self.doc['id'])
        self.assertEqual(restored['thickness_measurements'], state['thickness_measurements'])
        rows = list(csv.reader(io.StringIO(self.service.bom(self.doc['id']).decode('utf-8-sig'))))
        self.assertEqual(rows[0][13:16], ['测厚候选(mm)', '测厚状态', '测量依据'])
        self.assertEqual(rows[1][10], '')
        self.assertEqual(rows[1][13:15], ['1.0', '候选待核对'])

    def test_legacy_geometry_has_no_implied_thickness(self):
        state = self.analyze()
        self.assertEqual(state['thickness_measurements'], {})
        with self.store._lock(self.doc['id']):
            raw = self.service._read(self.doc['id'], self.doc['sha256'])
            raw['geometry'].pop('thickness_measurements')
            self.service._save(raw)
        self.assertEqual(self.service.get(self.doc['id'])['thickness_measurements'], {})

    def test_incremental_thickness_preserves_confirmed_rows_and_ai_without_model_call(self):
        calls = []
        def analyzer(_output, geometry):
            calls.append(1)
            return {'groups': fallback_groups(geometry), 'warnings': [],
                    'provenance': {'kind': 'fixture', 'status': 'completed', 'session_id': 'original'}}
        self.service.analyzer = analyzer
        state = self.analyze()
        groups = copy.deepcopy(state['groups'])
        groups[0].update(status='confirmed', name='人工已核对件', classification='make',
                         thickness_mm=1.5, quantity_per_set=7, material='Q235B', notes='保留原有人工说明')
        before = self.service.review(self.doc['id'], {'expected_revision': state['revision'], 'groups': groups})
        old_run = before['geometry_run_id']
        self.service.native_runner = native_thickness_fixture
        started = self.service.measure_thickness(self.doc['id'], {'expected_revision': before['revision']})
        self.assertEqual(started['groups'], before['groups'])
        self.assertEqual(started['ai'], before['ai'])
        after = ready(self.service, self.doc['id'])
        self.assertEqual(after['status'], 'ready', after)
        self.assertEqual(after['groups'], before['groups'])
        self.assertEqual(after['ai'], before['ai'])
        self.assertEqual(calls, [1])
        self.assertNotEqual(after['geometry_run_id'], old_run)
        self.assertTrue((self.service._folder(self.doc['id']) / 'runs' / old_run / 'geometry.json').is_file())
        self.assertEqual(after['thickness_measurements']['S0001']['value_mm'], 1)
        rows = list(csv.reader(io.StringIO(self.service.bom(self.doc['id']).decode('utf-8-sig'))))
        self.assertEqual(rows[1][10], '1.5')
        self.assertEqual(rows[1][13], '1.0')

    def test_incremental_failure_preserves_old_evidence_and_group_state(self):
        self.service.native_runner = native_thickness_fixture
        before = self.analyze()
        self.service.native_runner = lambda *_: (_ for _ in ()).throw(ValueError('量测失败'))
        self.service.measure_thickness(self.doc['id'], {'expected_revision': before['revision']})
        after = ready(self.service, self.doc['id'])
        self.assertEqual(after['status'], 'failed')
        self.assertEqual(after['geometry_run_id'], before['geometry_run_id'])
        self.assertEqual(after['groups'], before['groups'])
        self.assertEqual(after['ai'], before['ai'])
        self.assertEqual(after['thickness_measurements'], before['thickness_measurements'])
        self.assertEqual(self.service.geometry(self.doc['id'])['solids'][0]['thickness'], thickness_fixture())

    def test_incremental_identity_change_is_rejected_without_replacing_geometry(self):
        self.service.native_runner = native_thickness_fixture
        before = self.analyze()
        for field in ('id', 'definition_id', 'occurrence_path'):
            def changed(source, output):
                native_thickness_fixture(source, output)
                path = output / 'geometry.json'
                data = json.loads(path.read_text())
                if field == 'id':
                    data['solids'].pop()
                elif field == 'definition_id':
                    data['solids'][0]['definition_id'] = 'different-definition'
                else:
                    data['solids'][0]['occurrence_path'] = ['different-instance']
                path.write_text(json.dumps(data))
            self.service.native_runner = changed
            current = self.service.get(self.doc['id'])
            with self.subTest(field=field):
                self.service.measure_thickness(self.doc['id'], {'expected_revision': current['revision']})
                after = ready(self.service, self.doc['id'])
                self.assertEqual(after['status'], 'failed')
                self.assertIn('对应关系', after['error'])
                self.assertEqual(after['geometry_run_id'], before['geometry_run_id'])
                self.assertEqual(after['groups'], before['groups'])
                self.assertEqual(after['thickness_measurements'], before['thickness_measurements'])

    def test_incremental_requires_existing_geometry_and_current_revision(self):
        with self.assertRaises(RecordError):
            self.service.measure_thickness(self.doc['id'], {'expected_revision': 0})
        state = self.analyze()
        for payload in ({}, {'expected_revision': True}, {'expected_revision': None},
                        {'expected_revision': state['revision'], 'groups': []}):
            with self.subTest(payload=payload), self.assertRaises(RecordError):
                self.service.measure_thickness(self.doc['id'], payload)
        with self.assertRaises(WorkbenchError):
            self.service.measure_thickness(self.doc['id'], {'expected_revision': 0})
        self.assertEqual(self.service.get(self.doc['id'])['geometry_run_id'], state['geometry_run_id'])

    def test_incremental_has_single_worker_and_keeps_previous_geometry_while_running(self):
        before = self.analyze()
        entered, release = threading.Event(), threading.Event()
        calls = []
        def runner(source, output):
            calls.append(1); entered.set(); release.wait(3); native_thickness_fixture(source, output)
        self.service.native_runner = runner
        first = self.service.measure_thickness(self.doc['id'], {'expected_revision': before['revision']})
        self.assertTrue(entered.wait(1))
        current = self.service.get(self.doc['id'])
        self.assertEqual(current['stage'], 'thickness')
        self.assertEqual(current['geometry_run_id'], before['geometry_run_id'])
        second = self.service.measure_thickness(self.doc['id'], {'expected_revision': current['revision']})
        self.assertEqual(first['job_id'], second['job_id'])
        release.set()
        self.assertEqual(ready(self.service, self.doc['id'])['status'], 'ready')
        self.assertEqual(calls, [1])

    def test_incremental_does_not_accept_legacy_worker_without_measurement_status(self):
        before = self.analyze()
        self.service.measure_thickness(self.doc['id'], {'expected_revision': before['revision']})
        after = ready(self.service, self.doc['id'])
        self.assertEqual(after['status'], 'failed')
        self.assertIn('完整板厚量测状态', after['error'])
        self.assertEqual(after['geometry_run_id'], before['geometry_run_id'])

    def test_material_and_quantity_candidates_never_adopt_themselves(self):
        self.service.native_runner = native_properties_fixture
        state = self.analyze()
        self.assertEqual(state['material_measurements']['S0001']['value'], 'Steel')
        self.assertEqual(state['bom_settings'], {'order_sets': None})
        self.assertTrue(all(row['material'] is None and row['quantity_per_set'] is None
                            for row in state['groups']))
        self.assertEqual(state['quantity_candidates']['P0001']['status'], 'unresolved')
        groups = copy.deepcopy(state['groups'])
        groups[0].update(classification='make', solid_ids=['S0001', 'S0002'])
        state = self.service.review(self.doc['id'], {'expected_revision': state['revision'], 'groups': groups[:1]})
        self.assertEqual(state['quantity_candidates']['P0001']['value'], 2)
        self.assertIsNone(state['groups'][0]['quantity_per_set'])
        self.assertEqual(CadManufacturingService(self.store).get(self.doc['id'])['quantity_candidates'],
                         state['quantity_candidates'])

    def test_properties_enrichment_preserves_all_human_fields_settings_and_ai(self):
        calls = []
        def analyzer(_output, geometry):
            calls.append(1)
            return {'groups': fallback_groups(geometry), 'warnings': [],
                    'provenance': {'kind': 'fixture', 'status': 'completed', 'session_id': 'original'}}
        self.service.native_runner = native_identity_fixture
        self.service.analyzer = analyzer
        state = self.analyze()
        groups = copy.deepcopy(state['groups'])
        groups[0].update(status='confirmed', classification='make', name='人工零件',
                         material='Q235B', quantity_per_set=7, thickness_mm=2.5, notes='人工说明')
        before = self.service.review(self.doc['id'], {'expected_revision': state['revision'], 'groups': groups,
                                                    'bom_settings': {'order_sets': 9}})
        self.service.native_runner = native_properties_fixture
        self.service.read_properties(self.doc['id'], {'expected_revision': before['revision']})
        after = ready(self.service, self.doc['id'])
        self.assertEqual(after['status'], 'ready', after)
        for field in ('groups', 'ai', 'bom_settings'):
            self.assertEqual(after[field], before[field])
        self.assertEqual(calls, [1])
        self.assertEqual(after['material_measurements']['S0001']['value'], 'Steel')
        self.assertEqual(after['quantity_candidates']['P0001']['value'], 1)
        self.assertNotEqual(after['geometry_run_id'], before['geometry_run_id'])

    def test_properties_require_complete_source_material_and_preserve_old_on_failure(self):
        self.service.native_runner = native_properties_fixture
        before = self.analyze()
        def missing_material(source, output):
            native_properties_fixture(source, output)
            path = output / 'geometry.json'; data = json.loads(path.read_text())
            data['solids'][1].pop('material'); path.write_text(json.dumps(data))
        self.service.native_runner = missing_material
        self.service.read_properties(self.doc['id'], {'expected_revision': before['revision']})
        after = ready(self.service, self.doc['id'])
        self.assertEqual(after['status'], 'failed')
        self.assertIn('完整材料', after['error'])
        for field in ('groups', 'ai', 'bom_settings', 'geometry_run_id', 'material_measurements', 'quantity_candidates'):
            self.assertEqual(after[field], before[field])

    def test_bom_settings_are_atomic_audited_and_legacy_review_preserves_them(self):
        before = self.analyze()
        state = self.service.review(self.doc['id'], {'expected_revision': before['revision'],
            'groups': before['groups'], 'bom_settings': {'order_sets': 3}})
        self.assertEqual(state['revision'], before['revision'] + 1)
        self.assertEqual(state['groups'], before['groups'])
        audit = json.loads(next(self.service._folder(self.doc['id']).glob('review-*.json')).read_text())
        self.assertEqual(audit['before_bom_settings'], {'order_sets': None})
        self.assertEqual(audit['after_bom_settings'], {'order_sets': 3})
        rows = copy.deepcopy(state['groups']); rows[0]['name'] = '改名'
        latest = self.service.review(self.doc['id'], {'expected_revision': state['revision'], 'groups': rows})
        self.assertEqual(latest['bom_settings'], {'order_sets': 3})
        with self.assertRaises(WorkbenchError):
            self.service.review(self.doc['id'], {'expected_revision': before['revision'],
                'groups': rows, 'bom_settings': {'order_sets': 5}})
        for bad in (0, -1, True, 1.5, '2', 1000001):
            with self.subTest(bad=bad), self.assertRaises(RecordError):
                self.service.review(self.doc['id'], {'expected_revision': latest['revision'],
                    'groups': rows, 'bom_settings': {'order_sets': bad}})
        self.assertEqual(self.service.get(self.doc['id']), latest)
        cleared = self.service.review(self.doc['id'], {'expected_revision': latest['revision'],
            'groups': rows, 'bom_settings': {'order_sets': None}})
        self.assertEqual(cleared['bom_settings'], {'order_sets': None})

    def test_csv_separates_source_candidates_adopted_values_and_order_totals(self):
        self.service.native_runner = native_properties_fixture
        state = self.analyze()
        groups = copy.deepcopy(state['groups'])
        groups[0].update(classification='make', quantity_per_set=7, material='Q235B')
        groups[1].update(classification='buy')
        state = self.service.review(self.doc['id'], {'expected_revision': state['revision'], 'groups': groups})
        table = list(csv.reader(io.StringIO(self.service.bom(self.doc['id']).decode('utf-8-sig'))))
        self.assertEqual(len(table[0]), 25)
        self.assertEqual(table[1][9], 'Q235B')
        self.assertEqual(table[1][16], 'Steel')
        self.assertEqual(table[1][19], '1')
        self.assertEqual(table[1][22:25], ['', '', ''])
        self.service.review(self.doc['id'], {'expected_revision': state['revision'], 'groups': groups,
                                             'bom_settings': {'order_sets': 3}})
        table = list(csv.reader(io.StringIO(self.service.bom(self.doc['id']).decode('utf-8-sig'))))
        self.assertEqual(table[1][22:25], ['3', '21', '3'])
        self.assertEqual(table[2][22:25], ['3', '', '3'])

    def test_old_saved_state_gets_empty_properties_and_unknown_sets_without_rewrite(self):
        before = self.analyze()
        with self.store._lock(self.doc['id']):
            raw = self.service._read(self.doc['id'], self.doc['sha256'])
            raw.pop('bom_settings')
            for key in ('material_measurements', 'solid_identities'):
                raw['geometry'].pop(key, None)
            self.service._save(raw)
        state = self.service.get(self.doc['id'])
        self.assertEqual(state['bom_settings'], {'order_sets': None})
        self.assertEqual(state['material_measurements'], {})
        self.assertTrue(all(row['value'] is None for row in state['quantity_candidates'].values()))
        self.assertEqual(state['groups'], before['groups'])


class ThicknessProtocolTests(unittest.TestCase):
    def test_rejects_nonfinite_false_evidence_and_foreign_faces(self):
        changes = [
            {'value_mm': float('nan')}, {'value_mm': 0}, {'value_mm': True},
            {'value_mm': float('inf')}, {'evidence': []}, {'status': []}, {'method': []},
            {'confidence': []}, {'reason': ''}, {'method': 'coaxial_cylinders'},
            {'method': 'combined'}, {'method': 'none'}, {'confidence': 'none'},
            {'status': 'unresolved'}, {'extra': True},
        ]
        evidence_changes = [
            {'face_ids': ['F0000', 'F0002']}, {'face_ids': ['F0001', 'F0005']},
            {'face_ids': ['F0001', 'F0001']}, {'face_ids': [['F0001'], 'F0002']},
            {'kind': []}, {'kind': 'coaxial_cylinders'}, {'thickness_mm': 0},
            {'thickness_mm': float('nan')}, {'thickness_mm': 2},
            {'support_area_mm2': 0}, {'support_area_mm2': float('inf')},
        ]
        for change in changes:
            value = thickness_fixture(); value.update(change)
            with self.subTest(change=change), self.assertRaises(RecordError):
                validate_thickness(value, 4)
        for change in evidence_changes:
            value = thickness_fixture(); value['evidence'][0].update(change)
            with self.subTest(evidence_change=change), self.assertRaises(RecordError):
                validate_thickness(value, 4)

    def test_large_valid_face_ids_and_unresolved_conflicting_evidence(self):
        value = thickness_fixture()
        value['evidence'][0]['face_ids'] = ['F10000', 'F10001']
        self.assertEqual(validate_thickness(value, 10001), value)
        value.update(status='unresolved', value_mm=None, confidence='none')
        other = copy.deepcopy(value['evidence'][0]); other['thickness_mm'] = 2
        value['evidence'].append(other)
        self.assertEqual(validate_thickness(value, 10001), value)

    def test_geometry_validates_optional_measurements(self):
        geometry = fixture_geometry('a' * 64)
        self.assertEqual(validate_geometry(geometry, 'a' * 64), geometry)
        geometry['solids'][0]['thickness'] = thickness_fixture()
        self.assertEqual(validate_geometry(geometry, 'a' * 64), geometry)
        geometry['solids'][0]['thickness']['evidence'][0]['face_ids'][1] = 'F0005'
        with self.assertRaises(RecordError):
            validate_geometry(geometry, 'a' * 64)

    def test_group_summary_requires_all_solid_evidence_and_consistency(self):
        group = {'solid_ids': ['S0001', 'S0002']}
        measures = {'S0001': thickness_fixture(1), 'S0002': thickness_fixture(1.0005)}
        result = group_thickness_summary(group, measures)
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual(result['value_mm'], 1.00025)
        measures['S0002'] = thickness_fixture(2)
        self.assertEqual(group_thickness_summary(group, measures)['status'], 'conflict')
        self.assertIsNone(group_thickness_summary(group, measures)['value_mm'])
        measures.pop('S0002')
        self.assertEqual(group_thickness_summary(group, measures)['status'], 'unresolved')
        measures['S0002'] = thickness_fixture(); measures['S0002'].update(status='unresolved', value_mm=None, confidence='none')
        self.assertEqual(group_thickness_summary(group, measures)['status'], 'unresolved')

    def test_group_summary_keeps_conflicts_visible_despite_missing_or_unresolved_members(self):
        group = {'solid_ids': ['S0001', 'S0002', 'S0003']}
        measures = {'S0001': thickness_fixture(1), 'S0002': thickness_fixture(2)}
        self.assertEqual(group_thickness_summary(group, measures)['status'], 'conflict')
        measures['S0003'] = thickness_fixture()
        measures['S0003'].update(status='unresolved', value_mm=None, confidence='none')
        self.assertEqual(group_thickness_summary(group, measures)['status'], 'conflict')
        measures['S0002'] = thickness_fixture(1)
        self.assertEqual(group_thickness_summary(group, measures)['status'], 'unresolved')
        self.assertEqual(group_thickness_summary(group, {})['status'], 'not_measured')
        not_applicable = {'status': 'not_applicable', 'value_mm': None, 'method': 'none',
                          'confidence': 'none', 'reason': '未发现支持的薄壁特征', 'evidence': [], 'warnings': []}
        measures = {sid: copy.deepcopy(not_applicable) for sid in group['solid_ids']}
        self.assertEqual(group_thickness_summary(group, measures)['status'], 'not_applicable')

    def test_non_candidates_cannot_claim_confidence(self):
        for status in ('unresolved', 'not_applicable'):
            for confidence in ('high', 'medium'):
                value = thickness_fixture()
                value.update(status=status, value_mm=None, confidence=confidence)
                with self.subTest(status=status, confidence=confidence), self.assertRaises(RecordError):
                    validate_thickness(value, 4)


class ManufacturingHTTPTests(unittest.TestCase):
    def test_routes_roundtrip_do_not_create_quotation(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Workbench(Path(directory) / 'tasks')
            server = make_server(app, 0)
            server.manufacturing.native_runner = native_fixture
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            def request(path, method='GET', payload=None):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                body = json.dumps(payload) if payload is not None else None
                conn.request(method, '/workbench/api/cad/' + path, body,
                             {'X-Workbench-Request': '1', 'Content-Type': 'application/json'})
                response = conn.getresponse(); data = response.read(); status = response.status; conn.close()
                return status, data
            try:
                doc = server.cad.create('fixture.step', STEP)
                base = 'documents/' + doc['id'] + '/manufacturing'
                self.assertEqual(request(base)[0], 200)
                self.assertEqual(request(base + '/thickness', 'POST', {'expected_revision': 0})[0], 400)
                self.assertEqual(request(base + '/properties', 'POST', {'expected_revision': 0})[0], 400)
                self.assertEqual(request(base + '/analyze', 'POST', {})[0], 202)
                state = ready(server.manufacturing, doc['id'])
                self.assertEqual(request(base + '/geometry')[0], 200)
                self.assertEqual(request(base + '/files/S0001.step'), (200, STEP))
                server.manufacturing.native_runner = native_thickness_fixture
                self.assertEqual(request(base + '/thickness', 'POST', {'expected_revision': state['revision']})[0], 202)
                state = ready(server.manufacturing, doc['id'])
                self.assertEqual(state['status'], 'ready')
                self.assertEqual(state['thickness_measurements']['S0001']['value_mm'], 1)
                self.assertEqual(request(base + '/thickness', 'POST', {'expected_revision': 0})[0], 409)
                def material_fixture(source, output):
                    native_thickness_fixture(source, output)
                    path = output / 'geometry.json'; data = json.loads(path.read_text())
                    for solid in data['solids']:
                        solid['material'] = material()
                    path.write_text(json.dumps(data))
                server.manufacturing.native_runner = material_fixture
                self.assertEqual(request(base + '/properties', 'POST', {'expected_revision': state['revision']})[0], 202)
                state = ready(server.manufacturing, doc['id'])
                self.assertEqual(state['status'], 'ready')
                self.assertEqual(state['material_measurements']['S0001']['value'], 'Steel')
                self.assertEqual(request(base + '/properties', 'POST', {'expected_revision': 0})[0], 409)
                self.assertEqual(request(base + '/review', 'POST', {'expected_revision': state['revision'], 'groups': state['groups']})[0], 200)
                self.assertEqual(request(base + '/review', 'POST', {'expected_revision': state['revision'],
                    'groups': state['groups'], 'bom_settings': {'order_sets': 2}})[0], 200)
                self.assertEqual(request(base + '/bom.csv')[0], 200)
                self.assertEqual(app.tasks(), [])
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__': unittest.main()
