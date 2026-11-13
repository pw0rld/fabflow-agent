"""Source-bound synthetic customer manufacturing HTTP boundary checks.

Execute only on ZeroClave. Native geometry has separate real-CAD acceptance;
these fixtures check protocol/privacy without a CAD or model invocation.
"""
import copy
import hashlib
from http.cookies import SimpleCookie
import json
import math
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from workbench_store import Workbench
from workbench_server import make_server
from test_batch_quote_service import STEP
from test_batch_part_preview import ExistingCad, ExistingManufacturing, PNG
from test_cad_laser_contract import laser_profile, unsupported_profile
from test_cad_bend_contract import bend_profile, no_bends

API = '/workbench/customer/api'
PRIVATE = {'total_cost', 'unit_cost', 'material_cost', 'process_cost', 'setup_cost',
           'rate_cny', 'amount_cny', 'rate_snapshot', 'estimated_cost_subtotal', 'material_pricing',
           'rate_percent', 'pricing_method', 'settings', '_customer_owner'}
DXF = b'0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n'


def process_geometry(sha, *, mixed=False):
    """Analytic fixtures with stable native identities and registered assets."""
    solids = []
    for index in range(1, 3 if mixed else 2):
        sid = 'S' + str(index).zfill(4)
        flat = index == 1
        size = [10, 8, 1.5] if flat else [3, 3, 4]
        thickness = 1.5 if flat else 1
        points = [[0, 0, 0], [size[0], 0, 0], [size[0], size[1], 0], [0, size[1], 0],
                  [0, 0, size[2]], [size[0], 0, size[2]], [size[0], size[1], size[2]], [0, size[1], size[2]]]
        solids.append({'id': sid, 'name': 'Flat panel' if flat else 'Bent bracket',
            'definition_id': ('PANEL' if flat else 'BRACKET') + '/solid-1',
            'occurrence_path': ['ASSEMBLY', str(index)], 'volume_mm3': (80-math.pi*.25)*1.5 if flat else 30,
            'area_mm2': 200 if flat else 80,
            'bounds_mm': {'min': [0, 0, 0], 'max': size, 'size': size},
            'topology': {'faces': 8, 'edges': 12, 'vertices': 8, 'valid': True, 'closed': True},
            'mesh': {'positions': [number for point in points for number in point],
                     'indices': [0, 1, 2, 0, 2, 3, 4, 5, 6, 4, 6, 7]},
            'thumbnail': sid+'.png', 'step_file': sid+'.step',
            'material': {'status': 'unresolved', 'value': None, 'entries': [],
                         'reason': 'No material assignment in this synthetic fixture.', 'warnings': []},
            'thickness': {'status': 'candidate', 'value_mm': thickness, 'method': 'parallel_faces',
                'confidence': 'high', 'reason': 'Synthetic paired faces for protocol checking.',
                'evidence': [{'kind': 'parallel_faces', 'face_ids': ['F0001', 'F0002'],
                              'thickness_mm': thickness, 'support_area_mm2': 40}], 'warnings': []},
            'laser_cut': laser_profile(sid) if flat else unsupported_profile(),
            'bend_features': no_bends() if flat else bend_profile()})
    return {'schema_version': 1, 'source_sha256': sha, 'output_unit': 'mm', 'solids': solids}


class CachedManufacturing(ExistingManufacturing):
    def file(self, native_id, filename):
        return self.files[(native_id, filename)], 'application/dxf' if filename.endswith('.dxf') else 'image/png'


class CustomerProcessHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyl-customer-process-')
        self.server = make_server(Workbench(Path(self.temporary.name) / 'tasks'), 0)
        self.cad, self.manufacturing = ExistingCad(), CachedManufacturing()
        self.server.customer_batches.cad = self.cad
        self.server.customer_batches.manufacturing = self.manufacturing
        self.mixed = False
        self.native_id = None

        def processor(row, content):
            sha = hashlib.sha256(content).hexdigest();self.native_id = 'CAD-' + sha[:32]
            geometry = process_geometry(sha, mixed=self.mixed)
            self.cad.documents[self.native_id] = {'id': self.native_id, 'sha256': sha}
            self.manufacturing.geometries[self.native_id] = geometry
            for solid in geometry['solids']:
                self.manufacturing.files[(self.native_id, solid['thumbnail'])] = PNG
                if solid['laser_cut']['status'] == 'ready':
                    self.manufacturing.files[(self.native_id, solid['laser_cut']['dxf_file'])] = DXF
            return geometry, self.native_id

        self.server.customer_batches.processor = processor
        self.server.customer_batches.start_worker()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True);self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        for name in ('customer_batches', 'batch_quotes', 'case_quotes', 'orders'):
            getattr(self.server, name).close()
        self.server.shutdown();self.server.server_close();self.thread.join(5);self.temporary.cleanup()

    def request(self, path, data=None, *, cookie=None, headers=None):
        supplied = {'Origin': self.base, 'X-Workbench-Request': '1'}
        if cookie:supplied['Cookie'] = cookie
        if isinstance(data, dict):supplied['Content-Type'] = 'application/json';data = json.dumps(data).encode()
        supplied.update(headers or {})
        try:response = urlopen(Request(self.base+path, data=data, headers=supplied), timeout=10)
        except HTTPError as error:response = error
        with response:
            raw = response.read()
            return response.status, json.loads(raw) if response.headers.get('Content-Type', '').startswith('application/json') else raw

    def session(self):
        request = Request(self.base+API+'/session', data=b'{}', headers={'X-Workbench-Request': '1', 'Content-Type': 'application/json', 'Origin': self.base})
        with urlopen(request, timeout=10) as response:
            cookie = SimpleCookie();cookie.load(response.headers['Set-Cookie'])
            return 'HYL_CUSTOMER_SESSION=' + cookie['HYL_CUSTOMER_SESSION'].value

    def finished(self, cookie):
        status, order = self.request(API+'/orders?name='+quote('assembly.step'), STEP, cookie=cookie,
            headers={'X-Event-Id': 'manufacturing-upload', 'Content-Type': 'application/octet-stream'})
        self.assertEqual(status, 201, order)
        status, queued = self.request(API+'/orders/'+order['id']+'/start',
            {'event_id': 'manufacturing-start', 'expected_revision': order['revision'], 'order_sets': 10}, cookie=cookie)
        self.assertEqual(status, 202, queued)
        until = time.monotonic()+15
        while time.monotonic() < until:
            status, value = self.request(API+'/orders/'+order['id'], cookie=cookie)
            self.assertEqual(status, 200, value)
            if value['status'] not in {'queued', 'running'}:return value
            time.sleep(.05)
        self.fail('Synthetic processing timed out.')

    def assert_no_costs(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                self.assertNotIn(key, PRIVATE);self.assertFalse(key.startswith('_'));self.assert_no_costs(item)
        elif isinstance(value, list):
            for item in value:self.assert_no_costs(item)
        self.assertNotIn('/ortho2cad-test/', json.dumps(value, ensure_ascii=False))

    def test_flat_part_manifest_and_native_drawings_are_owner_bound_and_cost_free(self):
        owner, outsider = self.session(), self.session();order = self.finished(owner)
        self.assertEqual(order['quote']['costing_mode'], 'process-v1')
        prefix = API+'/orders/'+order['id']+'/runs/'+order['quote']['run_id']+'/manufacturing'
        status, manifest = self.request(prefix, cookie=owner)
        self.assertEqual(status, 200, manifest);self.assert_no_costs(manifest)
        self.assertEqual(manifest['summary']['parts_count'], 1)
        part = manifest['parts'][0]
        self.assertEqual(part['quantity_total'], 10);self.assertEqual(part['weight_role'], 'cross_check')
        self.assertEqual(part['laser']['status'], 'ready');self.assertTrue(part['processes'])
        self.assertEqual(float(part['thickness_mm']), 1.5)
        for key, mime_marker in (('svg_url', b'<svg'), ('dxf_url', b'ENTITIES')):
            url = part['laser'][key]
            status, drawing = self.request(url, cookie=owner)
            self.assertEqual(status, 200, drawing);self.assertIn(mime_marker, drawing)
            self.assertEqual(self.request(url, cookie=outsider)[0], 404)
            self.assertEqual(self.request(url)[0], 401)
        self.assertEqual(self.request(prefix, cookie=outsider)[0], 404)
        self.assertEqual(self.request(prefix)[0], 401)
        self.assertEqual(self.request(prefix+'/L0001/internal-cost.csv', cookie=owner)[0], 404)
        self.assertEqual(self.request(prefix+'/L0001/S0001-cut.dxf', cookie=owner)[0], 404)

    def test_bent_part_does_not_offer_flat_pattern_or_full_quotation(self):
        self.mixed = True;owner = self.session();order = self.finished(owner)
        self.assertIsNone(order['quote']['total_price'])
        self.assertIn(order['quote']['pricing_status'], ('partial', 'blocked'))
        prefix = API+'/orders/'+order['id']+'/runs/'+order['quote']['run_id']+'/manufacturing'
        status, manifest = self.request(prefix, cookie=owner)
        self.assertEqual(status, 200, manifest);self.assert_no_costs(manifest)
        self.assertEqual(manifest['summary']['parts_count'], 2)
        bent = next(part for part in manifest['parts'] if part['part_name'] == 'Bent bracket')
        self.assertEqual(bent['laser']['status'], 'unavailable');self.assertIsNone(bent['laser']['dxf_url'])
        self.assertEqual(bent['bends'][0]['angle_deg'], 90);self.assertTrue(bent['blockers'])
        self.assertEqual(self.request(prefix+'/'+bent['id']+'/laser.dxf', cookie=owner)[0], 404)

    def test_invalid_native_source_cannot_publish_drawing_or_geometry_links(self):
        owner = self.session();order = self.finished(owner)
        self.cad.documents[self.native_id]['sha256'] = '0'*64
        prefix = API+'/orders/'+order['id']+'/runs/'+order['quote']['run_id']+'/manufacturing'
        status, manifest = self.request(prefix, cookie=owner)
        self.assertEqual(status, 200, manifest)
        part = manifest['parts'][0]
        self.assertIsNone(part['geometry_url']);self.assertIsNone(part['laser']['svg_url'])
        self.assertEqual(self.request(prefix+'/L0001/laser.svg', cookie=owner)[0], 404)

    def test_manual_partial_reprice_uses_current_subtotal_and_keeps_full_price_pending(self):
        from batch_cost_sheet import build_cost_sheet, reprice_cost_sheet
        from customer_upload_service import CustomerUploadService
        self.mixed = True;owner = self.session();order = self.finished(owner)
        state = self.server.customer_batches.get(order['id'])
        before = copy.deepcopy(state)
        original = state['result']
        old_subtotal = original['manufacturing']['estimated_price_subtotal']
        flat = next(line for line in original['lines'] if line['priced'])
        sheet = build_cost_sheet(original)
        self.assertIsNone(sheet['totals']['total_cost'])
        self.assertEqual({entry['line_id'] for entry in sheet['entries']}, {flat['id']},
                         'Unpriced process candidates must not become retained or adopted fees.')
        policy = copy.deepcopy(sheet['policy'])
        policy['entries'].append({'id': 'manual-handling', 'line_id': flat['id'], 'category': 'other',
            'label': '本单明确采用的搬运费用', 'method': 'fixed', 'source_kind': 'manual_adopted', 'amount': '11.11'})
        updated = reprice_cost_sheet(original, policy)
        self.assertEqual(updated['totals']['unpriced_lines'], 1)
        self.assertNotEqual(updated['totals']['total_price'], old_subtotal)
        self.assertEqual(updated['manufacturing']['estimated_price_subtotal'], old_subtotal,
                         'Engineering evidence is intentionally preserved by manual reprice.')
        adopted = copy.deepcopy(state);adopted['result'] = updated
        projected = CustomerUploadService._project(adopted)
        self.assertIsNone(projected['quote']['total_price'])
        self.assertEqual(projected['quote']['pricing_status'], 'partial')
        self.assertEqual(projected['quote']['estimated_subtotal'], updated['totals']['total_price'])
        self.assertEqual(state, before, 'Customer projection may not rewrite prior state or finance.')
        self.assert_no_costs(projected)

    def test_withheld_full_totals_do_not_relax_legacy_or_unbound_cost_validation(self):
        from batch_cost_sheet import build_cost_sheet
        self.mixed = True;owner = self.session();order = self.finished(owner)
        original = self.server.customer_batches.get(order['id'])['result']
        self.assertTrue(build_cost_sheet(original)['entries'])
        for change in ('legacy', 'wrong-version', 'complete', 'source', 'parts-count', 'category-total'):
            broken = copy.deepcopy(original)
            if change == 'legacy':broken['settings'].pop('costing_mode')
            if change == 'wrong-version':broken['manufacturing']['version'] = 'legacy-v1'
            if change == 'complete':broken['manufacturing']['complete'] = True
            if change == 'source':broken['source_files'][0]['sha256'] = 'not-a-source-hash'
            if change == 'parts-count':broken['manufacturing']['parts_count'] += 1
            if change == 'category-total':broken['totals']['material_cost'] = '999999.00'
            with self.subTest(change=change), self.assertRaises(ValueError):
                build_cost_sheet(broken)


if __name__ == '__main__':unittest.main()
