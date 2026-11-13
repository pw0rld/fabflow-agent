"""Isolated laser-profile job, persistence and download protocol checks."""
import copy
import hashlib
import http.client
import json
import math
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
from test_cad_laser_contract import laser_profile, unsupported_profile
from test_cad_manufacturing import (STEP, native_identity_fixture, native_properties_fixture,
                                    ready)

DXF = (b'0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n4\n0\nENDSEC\n'
       b'0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n')


def native_laser_fixture(source, output):
    native_properties_fixture(source, output)
    path = output / 'geometry.json'
    data = json.loads(path.read_text())
    solid = data['solids'][0]
    solid.update(bounds_mm={'min': [0, 0, 0], 'max': [10, 8, 1.5], 'size': [10, 8, 1.5]},
                 volume_mm3=(80-math.pi*.5**2)*1.5)
    solid['laser_cut'] = laser_profile()
    data['solids'][1]['laser_cut'] = unsupported_profile()
    (output / solid['laser_cut']['dxf_file']).write_bytes(DXF)
    path.write_text(json.dumps(data))


class LaserServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CadDocumentStore(Path(self.temp.name) / 'cad')
        self.document = self.store.create('test.step', STEP)
        self.service = CadManufacturingService(self.store, native_runner=native_identity_fixture)

    def analyze(self, runner=None):
        if runner:
            self.service.native_runner = runner
        self.service.start(self.document['id'], {'expected_revision': 0})
        state = ready(self.service, self.document['id'])
        self.assertEqual(state['status'], 'ready', state)
        return state

    def generate(self, state):
        self.service.native_runner = native_laser_fixture
        self.service.generate_laser(self.document['id'], {'expected_revision': state['revision']})
        return ready(self.service, self.document['id'])

    def test_existing_cache_does_not_imply_projected_cutting_paths(self):
        state = self.analyze()
        self.assertEqual(state['laser_profiles'], {})
        self.assertEqual(self.service.get(self.document['id'])['laser_profiles'], {})
        with self.assertRaises(RecordError):
            self.service.file(self.document['id'], 'S0001-cut.dxf')

    def test_initial_read_can_include_native_profiles_without_adoption(self):
        state = self.analyze(native_laser_fixture)
        self.assertEqual(state['laser_profiles']['S0001'], laser_profile())
        self.assertEqual(state['laser_profiles']['S0002']['status'], 'unsupported')
        self.assertTrue(all(group['thickness_mm'] is None for group in state['groups']))
        self.assertEqual(state['ai']['status'], 'not_requested')

    def test_incremental_laser_preserves_confirmed_group_all_parameters_and_ai(self):
        calls = []
        def analyzer(_output, geometry):
            calls.append('model')
            return {'groups': fallback_groups(geometry), 'warnings': [],
                    'provenance': {'kind': 'fixture', 'status': 'completed', 'session_id': 'original'}}
        self.service.analyzer = analyzer
        state = self.analyze()
        groups = copy.deepcopy(state['groups'])
        groups[0].update(status='confirmed', name='人工已核对', material='Q235B', thickness_mm=2,
                         quantity_per_set=9, notes='人工制造说明', classification='make')
        before = self.service.review(self.document['id'], {'expected_revision': state['revision'],
            'groups': groups, 'bom_settings': {'order_sets': 12}})
        after = self.generate(before)
        self.assertEqual(after['status'], 'ready', after)
        for key in ('groups', 'bom_settings', 'ai'):
            self.assertEqual(after[key], before[key])
        self.assertEqual(calls, ['model'])
        self.assertNotEqual(after['geometry_run_id'], before['geometry_run_id'])
        restored = CadManufacturingService(self.store).get(self.document['id'])
        self.assertEqual(restored['laser_profiles'], after['laser_profiles'])
        self.assertEqual(self.store.file(self.document['id'])[0], STEP)

    def test_partial_unsupported_is_explicit_and_only_registered_dxf_downloads(self):
        state = self.analyze()
        after = self.generate(state)
        self.assertEqual(after['status'], 'ready', after)
        self.assertEqual(self.service.file(self.document['id'], 'S0001-cut.dxf'), (DXF, 'application/dxf'))
        for filename in ('S0002-cut.dxf', 'S9999-cut.dxf', '../S0001-cut.dxf', 'input.step', 'geometry.json'):
            with self.subTest(filename=filename), self.assertRaises(RecordError):
                self.service.file(self.document['id'], filename)
        path = self.service._folder(self.document['id']) / 'runs' / after['geometry_run_id'] / 'S0001-cut.dxf'
        path.write_bytes(DXF + b'changed')
        with self.assertRaises(RecordError): self.service.file(self.document['id'], 'S0001-cut.dxf')

    def test_worker_without_complete_profiles_cannot_replace_previous_result(self):
        before = self.analyze(native_laser_fixture)
        self.service.native_runner = native_properties_fixture
        self.service.generate_laser(self.document['id'], {'expected_revision': before['revision']})
        after = ready(self.service, self.document['id'])
        self.assertEqual(after['status'], 'failed')
        self.assertIn('完整平板切割图', after['error'])
        for key in ('groups', 'ai', 'bom_settings', 'geometry_run_id', 'laser_profiles'):
            self.assertEqual(after[key], before[key])

    def test_identity_changes_malformed_profile_and_missing_dxf_preserve_old_geometry(self):
        baseline = self.analyze(native_laser_fixture)
        for mutation in ('identity', 'foreign-file', 'open-loop', 'missing-file', 'volume', 'plane'):
            def runner(source, output):
                native_laser_fixture(source, output)
                path = output / 'geometry.json'; data = json.loads(path.read_text())
                solid = data['solids'][0]
                if mutation == 'identity': solid['definition_id'] = 'other'
                if mutation == 'foreign-file': solid['laser_cut']['dxf_file'] = 'S0002-cut.dxf'
                if mutation == 'open-loop': solid['laser_cut']['contours'][0]['segments'][-1]['end'] = [1, 1]
                if mutation == 'missing-file': (output / 'S0001-cut.dxf').unlink()
                if mutation == 'volume': solid['volume_mm3'] += 1
                if mutation == 'plane': solid['laser_cut']['plane']['origin'] = [20, 0, 0]
                path.write_text(json.dumps(data))
            self.service.native_runner = runner
            before = self.service.get(self.document['id'])
            self.service.generate_laser(self.document['id'], {'expected_revision': before['revision']})
            after = ready(self.service, self.document['id'])
            with self.subTest(mutation=mutation):
                self.assertEqual(after['status'], 'failed')
                self.assertEqual(after['geometry_run_id'], baseline['geometry_run_id'])
                self.assertEqual(after['laser_profiles'], baseline['laser_profiles'])
                self.assertEqual(after['groups'], baseline['groups'])

    def test_laser_requires_existing_geometry_and_current_integer_revision(self):
        with self.assertRaises(RecordError):
            self.service.generate_laser(self.document['id'], {'expected_revision': 0})
        state = self.analyze()
        for payload in ({}, {'expected_revision': True}, {'expected_revision': None},
                        {'expected_revision': state['revision'], 'groups': []}):
            with self.subTest(payload=payload), self.assertRaises(RecordError):
                self.service.generate_laser(self.document['id'], payload)
        with self.assertRaises(WorkbenchError):
            self.service.generate_laser(self.document['id'], {'expected_revision': 0})

    def test_same_active_laser_job_is_reused_with_current_revision(self):
        state = self.analyze()
        entered, released = threading.Event(), threading.Event()
        self.addCleanup(released.set)
        calls = []
        def runner(source, output):
            calls.append(1); entered.set(); released.wait(3)
            native_laser_fixture(source, output)
        self.service.native_runner = runner
        first = self.service.generate_laser(self.document['id'], {'expected_revision': state['revision']})
        self.assertTrue(entered.wait(1))
        current = self.service.get(self.document['id'])
        self.assertEqual(current['stage'], 'laser')
        self.assertEqual(current['geometry_run_id'], state['geometry_run_id'])
        second = self.service.generate_laser(self.document['id'], {'expected_revision': current['revision']})
        self.assertEqual(first['job_id'], second['job_id'])
        released.set(); self.assertEqual(ready(self.service, self.document['id'])['status'], 'ready')
        self.assertEqual(calls, [1])


class LaserHTTPTests(unittest.TestCase):
    def test_laser_route_and_registered_attachment_without_quotation(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Workbench(Path(directory) / 'tasks')
            server = make_server(app, 0)
            server.manufacturing.native_runner = native_identity_fixture
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            def request(path, method='GET', payload=None):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
                connection.request(method, '/workbench/api/cad/' + path,
                    json.dumps(payload) if payload is not None else None,
                    {'X-Workbench-Request': '1', 'Content-Type': 'application/json'})
                response = connection.getresponse()
                result = response.status, response.read(), dict(response.getheaders())
                connection.close(); return result
            try:
                document = server.cad.create('fixture.step', STEP)
                base = 'documents/' + document['id'] + '/manufacturing'
                self.assertEqual(request(base + '/laser', 'POST', {'expected_revision': 0})[0], 400)
                self.assertEqual(request(base + '/analyze', 'POST', {})[0], 202)
                state = ready(server.manufacturing, document['id'])
                server.manufacturing.native_runner = native_laser_fixture
                self.assertEqual(request(base + '/laser', 'POST', {'expected_revision': state['revision']})[0], 202)
                state = ready(server.manufacturing, document['id'])
                self.assertEqual(state['status'], 'ready', state)
                status, data, headers = request(base + '/files/S0001-cut.dxf')
                self.assertEqual((status, data), (200, DXF))
                self.assertEqual(headers['Content-Type'], 'application/dxf')
                self.assertIn('attachment;', headers['Content-Disposition'])
                self.assertIn('S0001-cut.dxf', headers['Content-Disposition'])
                self.assertEqual(request(base + '/files/S0002-cut.dxf')[0], 400)
                self.assertEqual(request(base + '/laser', 'POST', {'expected_revision': 0})[0], 409)
                self.assertEqual(app.tasks(), [])
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
