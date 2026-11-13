"""Incremental bend evidence must not rewrite prior manufacturing artifacts."""
import copy
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cad_documents import CadDocumentStore
from cad_manufacturing import CadManufacturingService, fallback_groups
from prep_record import RecordError
from workbench_store import Workbench, WorkbenchError
from workbench_server import make_server
from test_cad_bend_contract import BOUNDS, bend_profile, no_bends
from test_cad_bom import material
from test_cad_laser_service import DXF, native_laser_fixture
from test_cad_manufacturing import STEP, ready


def native_before_bends(source, output):
    native_laser_fixture(source, output)
    path = output / 'geometry.json'; data = json.loads(path.read_text())
    second = data['solids'][1]
    second.update(bounds_mm=copy.deepcopy(BOUNDS), area_mm2=24, volume_mm3=10)
    second['topology']['faces'] = 8
    path.write_text(json.dumps(data))


def native_bends_fixture(source, output):
    native_before_bends(source, output)
    path = output / 'geometry.json'; data = json.loads(path.read_text())
    data['solids'][0]['bend_features'] = no_bends()
    data['solids'][1]['bend_features'] = bend_profile()
    path.write_text(json.dumps(data))


class BendServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = CadDocumentStore(Path(self.temp.name) / 'cad')
        self.document = self.store.create('test.step', STEP)
        self.service = CadManufacturingService(self.store, native_runner=native_before_bends)

    def analyze(self, runner=None):
        if runner: self.service.native_runner = runner
        self.service.start(self.document['id'], {'expected_revision': 0})
        state = ready(self.service, self.document['id'])
        self.assertEqual(state['status'], 'ready', state)
        return state

    def generate(self, state, runner=native_bends_fixture):
        self.service.native_runner = runner
        self.service.identify_bends(self.document['id'], {'expected_revision': state['revision']})
        return ready(self.service, self.document['id'])

    def test_legacy_state_has_no_implied_bend_and_initial_analysis_can_include_candidates(self):
        state = self.analyze()
        self.assertEqual(state['bend_profiles'], {})
        self.assertEqual(self.service.get(self.document['id'])['bend_profiles'], {})
        other = self.store.create('second.step', STEP + b'\n')
        self.service.native_runner = native_bends_fixture
        self.service.start(other['id'], {'expected_revision': 0})
        current = ready(self.service, other['id'])
        self.assertEqual(current['status'], 'ready', current)
        self.assertEqual(current['bend_profiles']['S0002'], bend_profile())
        self.assertTrue(all(row['thickness_mm'] is None for row in current['groups']))

    def test_incremental_preserves_adopted_values_ai_bom_and_all_old_native_fields_and_bytes(self):
        calls = []
        def analyzer(_output, geometry):
            calls.append('model')
            return {'groups': fallback_groups(geometry), 'warnings': [],
                    'provenance': {'kind': 'fixture', 'status': 'completed', 'session_id': 'original'}}
        self.service.analyzer = analyzer
        state = self.analyze()
        groups = copy.deepcopy(state['groups'])
        groups[0].update(status='confirmed', name='人工核对零件', classification='make',
                         material='Q235B', thickness_mm=2, quantity_per_set=3, notes='人工保留说明')
        state = self.service.review(self.document['id'], {'expected_revision': state['revision'],
                   'groups': groups, 'bom_settings': {'order_sets': 12}})
        old_geometry = self.service.geometry(self.document['id'])
        old_state = self.service._read(self.document['id'], self.document['sha256'])
        old_files = {name: self.service._artifact_for_state(old_state, name)
                     for name in old_state['geometry']['files'] if name != 'geometry.json'}
        def changed_worker(source, output):
            native_bends_fixture(source, output)
            path = output / 'geometry.json'; data = json.loads(path.read_text())
            data['solids'][0]['name'] = '新引擎名称不采用'
            data['solids'][0]['material'] = material('Aluminium')
            data['solids'][1].pop('thickness')
            data['engine'] = {'name': 'new engine'}
            data['warnings'] = ['new warnings not replacing prior geometry']
            for name in old_files:
                (output / name).write_bytes(old_files[name] + b'new worker timestamp or GUID')
            (output / 'unregistered.txt').write_bytes(b'not a published artifact')
            path.write_text(json.dumps(data))
        after = self.generate(state, changed_worker)
        self.assertEqual(after['status'], 'ready', after)
        for key in ('groups', 'ai', 'bom_settings', 'laser_profiles', 'thickness_measurements',
                    'material_measurements', 'geometry_summary', 'quantity_candidates'):
            self.assertEqual(after[key], state[key], key)
        self.assertEqual(calls, ['model'])
        current_geometry = self.service.geometry(self.document['id'])
        for solid in current_geometry['solids']: solid.pop('bend_features')
        self.assertEqual(current_geometry, old_geometry)
        for name, content in old_files.items():
            self.assertEqual(self.service.file(self.document['id'], name)[0], content, name)
        folder = self.service._folder(self.document['id']) / 'runs' / after['geometry_run_id']
        self.assertEqual({path.name for path in folder.iterdir()}, set(old_files) | {'geometry.json'})
        restored = CadManufacturingService(self.store).get(self.document['id'])
        self.assertEqual(restored['bend_profiles'], after['bend_profiles'])
        self.assertEqual(self.store.file(self.document['id'])[0], STEP)

    def test_second_bend_run_replaces_only_bend_features_and_keeps_original_dxf(self):
        first = self.generate(self.analyze())
        def unresolved_worker(source, output):
            native_bends_fixture(source, output)
            path = output / 'geometry.json'; data = json.loads(path.read_text())
            data['solids'][1]['bend_features'] = no_bends('unresolved')
            (output / 'S0001-cut.dxf').write_bytes(DXF + b'new UUID')
            path.write_text(json.dumps(data))
        second = self.generate(first, unresolved_worker)
        self.assertEqual(second['status'], 'ready', second)
        self.assertEqual(second['bend_profiles']['S0002']['status'], 'unresolved')
        self.assertEqual(self.service.file(self.document['id'], 'S0001-cut.dxf')[0], DXF)
        self.assertEqual(second['laser_profiles'], first['laser_profiles'])

    def test_legacy_geometry_is_not_backfilled_with_other_new_worker_properties(self):
        def legacy(source, output):
            native_before_bends(source, output)
            path = output / 'geometry.json'; data = json.loads(path.read_text())
            for solid in data['solids']:
                for key in ('thickness', 'material', 'laser_cut'): solid.pop(key)
            path.write_text(json.dumps(data))
        before = self.analyze(legacy)
        old_geometry = self.service.geometry(self.document['id'])
        after = self.generate(before)
        self.assertEqual(after['status'], 'ready', after)
        for key in ('laser_profiles', 'thickness_measurements', 'material_measurements'):
            self.assertEqual(after[key], {})
        geometry = self.service.geometry(self.document['id'])
        for solid in geometry['solids']: solid.pop('bend_features')
        self.assertEqual(geometry, old_geometry)
        with self.assertRaises(RecordError): self.service.file(self.document['id'], 'S0001-cut.dxf')

    def test_unregistered_old_input_cannot_be_copied_to_fused_run(self):
        state = self.analyze()
        with self.store._lock(self.document['id']):
            raw = self.service._read(self.document['id'], self.document['sha256'])
            # A registry entry still cannot expand the geometry-derived allowlist.
            import hashlib
            raw['geometry']['files']['input.step'] = hashlib.sha256(STEP).hexdigest()
            self.service._save(raw)
        after = self.generate(state)
        self.assertEqual(after['status'], 'failed', after)
        self.assertIn('注册', after['error'])
        self.assertEqual(after['geometry_run_id'], state['geometry_run_id'])

    def test_missing_partial_profiles_and_false_confirmation_preserve_previous_results(self):
        baseline = self.generate(self.analyze())
        for kind in ('missing-all', 'missing-one', 'confirmed', 'foreign-face', 'bad-mesh'):
            def runner(source, output):
                native_bends_fixture(source, output)
                path = output / 'geometry.json'; data = json.loads(path.read_text())
                if kind == 'missing-all':
                    for solid in data['solids']: solid.pop('bend_features')
                if kind == 'missing-one': data['solids'][0].pop('bend_features')
                if kind == 'confirmed': data['solids'][1]['bend_features']['bends'][0]['status'] = 'confirmed'
                if kind == 'foreign-face': data['solids'][1]['bend_features']['bends'][0]['inner_face_ids'] = ['F9999']
                if kind == 'bad-mesh': data['solids'][1]['bend_features']['bends'][0]['mesh']['indices'][0] = 999
                path.write_text(json.dumps(data))
            after = self.generate(self.service.get(self.document['id']), runner)
            with self.subTest(kind=kind):
                self.assertEqual(after['status'], 'failed', after)
                for key in ('groups', 'ai', 'bom_settings', 'geometry_run_id', 'bend_profiles', 'laser_profiles'):
                    self.assertEqual(after[key], baseline[key], key)

    def test_source_identity_topology_bounds_and_volume_changes_cannot_merge_new_faces(self):
        baseline = self.analyze()
        for kind in ('source', 'identity', 'topology', 'bounds', 'volume', 'area'):
            def runner(source, output):
                native_bends_fixture(source, output)
                path = output / 'geometry.json'; data = json.loads(path.read_text())
                solid = data['solids'][1]
                if kind == 'source': data['source_sha256'] = '0'*64
                if kind == 'identity': solid['definition_id'] = 'different'
                if kind == 'topology': solid['topology']['faces'] += 1
                if kind == 'bounds':
                    solid['bounds_mm']['max'][0] += 1; solid['bounds_mm']['size'][0] += 1
                if kind == 'volume': solid['volume_mm3'] += 1
                if kind == 'area': solid['area_mm2'] += 1
                path.write_text(json.dumps(data))
            after = self.generate(self.service.get(self.document['id']), runner)
            with self.subTest(kind=kind):
                self.assertEqual(after['status'], 'failed', after)
                self.assertEqual(after['geometry_run_id'], baseline['geometry_run_id'])
                self.assertEqual(after['bend_profiles'], {})
                self.assertEqual(after['laser_profiles'], baseline['laser_profiles'])

    def test_old_artifact_hash_tamper_fails_without_publishing_new_result(self):
        state = self.analyze()
        old_path = self.service._folder(self.document['id']) / 'runs' / state['geometry_run_id'] / 'S0001-cut.dxf'
        def runner(source, output):
            native_bends_fixture(source, output)
            old_path.write_bytes(DXF + b'tampered before merge')
        after = self.generate(state, runner)
        self.assertEqual(after['status'], 'failed', after)
        self.assertEqual(after['geometry_run_id'], state['geometry_run_id'])
        self.assertEqual(after['bend_profiles'], {})
        with self.assertRaises(RecordError): self.service.file(self.document['id'], 'S0001-cut.dxf')

    def test_bends_require_existing_geometry_and_strict_current_revision(self):
        with self.assertRaises(RecordError): self.service.identify_bends(self.document['id'], {'expected_revision': 0})
        state = self.analyze()
        for payload in ({}, {'expected_revision': None}, {'expected_revision': True},
                        {'expected_revision': state['revision'], 'confirmed': True}):
            with self.subTest(payload=payload), self.assertRaises(RecordError):
                self.service.identify_bends(self.document['id'], payload)
        with self.assertRaises(WorkbenchError):
            self.service.identify_bends(self.document['id'], {'expected_revision': 0})

    def test_current_revision_reuses_active_job_and_keeps_previous_views(self):
        state = self.analyze(); entered, released = threading.Event(), threading.Event()
        self.addCleanup(released.set); calls = []
        def runner(source, output):
            calls.append(1); entered.set(); released.wait(3); native_bends_fixture(source, output)
        self.service.native_runner = runner
        first = self.service.identify_bends(self.document['id'], {'expected_revision': state['revision']})
        self.assertTrue(entered.wait(1))
        current = self.service.get(self.document['id'])
        self.assertEqual(current['stage'], 'bends')
        self.assertEqual(current['geometry_run_id'], state['geometry_run_id'])
        self.assertEqual(self.service.file(self.document['id'], 'S0001-cut.dxf')[0], DXF)
        second = self.service.identify_bends(self.document['id'], {'expected_revision': current['revision']})
        self.assertEqual(first['job_id'], second['job_id'])
        with self.assertRaises(WorkbenchError):
            self.service.identify_bends(self.document['id'], {'expected_revision': state['revision']})
        released.set()
        self.assertEqual(ready(self.service, self.document['id'])['status'], 'ready')
        self.assertEqual(calls, [1])


class BendHTTPTests(unittest.TestCase):
    def test_bend_route_state_and_old_download_without_quotation(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Workbench(Path(directory) / 'tasks'); server = make_server(app, 0)
            server.manufacturing.native_runner = native_before_bends
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            def request(path, method='GET', payload=None):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                conn.request(method, '/workbench/api/cad/' + path,
                    json.dumps(payload) if payload is not None else None,
                    {'X-Workbench-Request': '1', 'Content-Type': 'application/json'})
                response = conn.getresponse(); result = response.status, response.read(), dict(response.getheaders())
                conn.close(); return result
            try:
                document = server.cad.create('fixture.step', STEP)
                base = 'documents/' + document['id'] + '/manufacturing'
                self.assertEqual(request(base + '/bends', 'POST', {'expected_revision': 0})[0], 400)
                self.assertEqual(request(base + '/analyze', 'POST', {})[0], 202)
                state = ready(server.manufacturing, document['id'])
                server.manufacturing.native_runner = native_bends_fixture
                self.assertEqual(request(base + '/bends', 'POST', {'expected_revision': state['revision']})[0], 202)
                state = ready(server.manufacturing, document['id'])
                self.assertEqual(state['status'], 'ready', state)
                status, payload, _ = request(base)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(payload)['bend_profiles']['S0002'], bend_profile())
                self.assertEqual(request(base + '/files/S0001-cut.dxf')[1], DXF)
                self.assertEqual(request(base + '/bends', 'POST', {'expected_revision': 0})[0], 409)
                self.assertEqual(request(base + '/bends', 'POST', {})[0], 400)
                self.assertEqual(app.tasks(), [])
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
