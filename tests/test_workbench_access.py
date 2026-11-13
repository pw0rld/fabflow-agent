"""Loopback route and customer-ownership checks; execute only on ZeroClave.

Basic authentication is an explicit Nginx deployment boundary and has separate
isolated proxy/browser checks. The loopback fixture never pretends to validate
a Basic password or grant staff rights using a client-supplied role header.
"""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, ProxyHandler, build_opener

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from workbench_access import (staff_identity, guest_identity, canonical_root, login_destination,
                              login_url, legacy_quotation_login, legacy_customer_root,
                              STAFF_API, IDENTITY_PATH, LOGIN_PATH, DEFAULT_NEXT)
from customer_upload_service import ADMIN_API_PREFIX
import test_customer_process_http as fixtures

BATCH = 'BATCH-' + 'a' * 32
RUN = 'RUN-' + 'b' * 32


class AccessValueTests(unittest.TestCase):
    def test_fixed_roles_are_detached_capability_metadata(self):
        self.assertEqual(guest_identity(), {'role': 'customer', 'capabilities': ['customer_upload', 'customer_orders']})
        staff = staff_identity()
        self.assertEqual(staff['role'], 'staff')
        self.assertTrue({'staff_costing', 'staff_orders', 'image_recognition', 'step_engineering'} <= set(staff['capabilities']))
        staff['capabilities'].clear()
        self.assertTrue(staff_identity()['capabilities'])

    def test_login_defaults_and_all_whitelisted_workflows(self):
        self.assertEqual(login_destination(''), DEFAULT_NEXT)
        self.assertEqual(canonical_root('/workbench/'), '/workbench/')
        targets = ['/workbench/?workspace=inquiry',
                   '/workbench/?workspace=orders&scope=customer&id=' + BATCH,
                   '/workbench/?workspace=orders&scope=poland&id=' + BATCH,
                   '/workbench/?workspace=engineering&flow=step&document=CAD-' + '1'*32 + '&view=manufacturing',
                   '/workbench/?workspace=engineering&flow=image&job=SPL-' + '2'*32,
                   '/workbench/?flow=step&sample=step&view=source']
        for target in targets:
            with self.subTest(target=target):
                self.assertEqual(login_destination(urlencode({'next': target})), canonical_root(target))
                self.assertEqual(urlsplit(login_url(target)).path, LOGIN_PATH)

    def test_login_rejects_external_encoded_duplicate_and_unknown_targets(self):
        targets = ['https://outside.invalid/workbench/', '//outside.invalid/workbench/', '/\\outside.invalid',
                   '/workbench/../private', '/workbench/%2e%2e/private', '/workbench/%252e/',
                   '/workbench/?workspace=orders#outside', '/workbench/?workspace=orders&workspace=inquiry',
                   '/workbench/?role=staff', '/workbench/?workspace=outside', '/workbench/?scope=other',
                   '/workbench/?workspace=orders&id=BAD', '/workbench/?document=../../state.json',
                   '/workbench/?job=CAD-'+'1'*32, '/workbench/?sample=1', '/workbench/?view=cost',
                   '/workbench/?%77orkspace=orders', '/workbench/\r\nLocation:https://outside.invalid', '',
                   '/workbench/?scope=public;next=https://outside.invalid']
        for target in targets:
            with self.subTest(target=repr(target)), self.assertRaises(ValueError):
                login_destination(urlencode({'next': target}))
        for query in ('next=/workbench/&next=/workbench/', 'next=/workbench/&role=staff',
                      'next=%252f%252foutside.invalid', 'next', 'next=/workbench/&'):
            with self.subTest(query=query), self.assertRaises(ValueError): login_destination(query)

    def test_legacy_paths_preserve_intent_without_changing_stores_or_exposing_data(self):
        url = legacy_quotation_login(urlencode({'scope': 'customer', 'id': BATCH}))
        self.assertEqual(urlsplit(url).path, LOGIN_PATH)
        self.assertEqual(parse_qs(urlsplit(url).query)['next'], ['/workbench/?workspace=orders&scope=customer&id=' + BATCH])
        self.assertEqual(parse_qs(urlsplit(legacy_quotation_login('')).query)['next'], ['/workbench/?workspace=orders&scope=public'])
        self.assertEqual(legacy_customer_root(''), '/workbench/?workspace=inquiry')
        self.assertEqual(legacy_customer_root('id='+BATCH), '/workbench/?workspace=inquiry&id='+BATCH)
        for query in ('id=BAD', 'scope=staff', 'id='+BATCH+'&id='+BATCH):
            with self.subTest(query=query), self.assertRaises(ValueError): legacy_quotation_login(query)
        with self.assertRaises(ValueError): legacy_customer_root('scope=customer')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


class WorkbenchAccessRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerProcessHTTPTests(methodName='test_bent_part_does_not_offer_flat_pattern_or_full_quotation')
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        self.fixture.mixed = True
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, cookie=None, headers=None):
        values = dict(headers or {})
        if cookie: values['Cookie'] = cookie
        try: response = self.opener.open(Request(self.fixture.base + path, headers=values), timeout=15)
        except HTTPError as error: response = error
        with response: return response.status, response.read(), dict(response.headers)

    def test_staff_identity_and_login_are_exact_readonly_upstream_endpoints(self):
        f = self.fixture
        self.assertEqual(f.server.server_address[0], '127.0.0.1')
        before = sorted(path.relative_to(f.temporary.name).as_posix() for path in Path(f.temporary.name).rglob('*') if path.is_file())
        status, data, headers = self.request(IDENTITY_PATH)
        self.assertEqual(status, 200); self.assertEqual(json.loads(data), staff_identity())
        self.assertNotIn('Set-Cookie', headers); self.assertIn('no-store', headers['Cache-Control'])
        for header in ({'X-Workbench-Role': 'staff'}, {'X-Workbench-Role': 'customer'}, {'Authorization': 'Basic invalid'}):
            self.assertEqual(json.loads(self.request(IDENTITY_PATH, headers=header)[1]), staff_identity())
        self.assertEqual(self.request(IDENTITY_PATH+'?role=staff')[0], 404)
        self.assertEqual(self.request(STAFF_API+'/identity/')[0], 404)
        status, _, headers = self.request(LOGIN_PATH)
        self.assertEqual(status, 303); self.assertEqual(headers['Location'], DEFAULT_NEXT)
        self.assertNotIn('Set-Cookie', headers)
        bad = LOGIN_PATH + '?' + urlencode({'next': 'https://outside.invalid/'})
        self.assertEqual(self.request(bad)[0], 400)
        after = sorted(path.relative_to(f.temporary.name).as_posix() for path in Path(f.temporary.name).rglob('*') if path.is_file())
        self.assertEqual(before, after)

    def test_old_entry_redirects_do_not_intercept_customer_api_parts_or_public_viewer(self):
        for path in ('/workbench/customer', '/workbench/customer/', '/workbench/customer/index.html'):
            status, _, headers = self.request(path)
            self.assertEqual(status, 308); self.assertEqual(headers['Location'], '/workbench/?workspace=inquiry')
        for path in ('/workbench/quotation', '/workbench/quotation/', '/workbench/quotation/index.html'):
            status, _, headers = self.request(path+'?scope=customer&id='+BATCH)
            self.assertEqual(status, 303)
            self.assertEqual(headers['Location'], legacy_quotation_login('scope=customer&id='+BATCH))
        self.assertEqual(self.request('/workbench/customer/api/orders')[0], 401)
        # Public page shells contain no saved order data and retain old links.
        for path in ('/workbench/quotation/part/', '/workbench/customer/part/'):
            status, content, _ = self.request(path)
            self.assertEqual(status, 200, content)
            self.assertNotIn(b'Flat panel', content); self.assertNotIn(b'Bent bracket', content)

    def test_staff_cached_manifest_and_drawings_reuse_customer_evidence_without_reprice(self):
        f = self.fixture; owner, outsider = f.session(), f.session(); order = f.finished(owner)
        batch, run = order['id'], order['quote']['run_id']
        folder = f.server.customer_batches._folder(batch)
        before = {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
        staff_prefix = ADMIN_API_PREFIX + '/' + batch + '/runs/' + run + '/'
        with (patch.object(f.server.customer_batches, '_native', side_effect=AssertionError('No CAD rerun')),
              patch.object(f.server.customer_batches.engineering_recognition, 'enqueue', side_effect=AssertionError('No model'))):
            status, data, _ = self.request(staff_prefix+'manufacturing')
            self.assertEqual(status, 200, data); staff = json.loads(data)
            self.assertEqual(staff['source'], 'staff'); self.assertEqual(len(staff['parts']), 2)
            status, customer = f.request(order['quote']['manufacturing_url'], cookie=owner)
            self.assertEqual(status, 200, customer); self.assertNotIn('source', customer)
            for a, b in zip(staff['parts'], customer['parts']):
                self.assertEqual(a['id'], b['id']); self.assertEqual(a['processes'], b['processes'])
                self.assertEqual(a['quantity_total'], b['quantity_total'])
                self.assertTrue(a['geometry_url'].startswith(staff_prefix+'customer-parts/'))
                self.assertEqual(self.request(a['geometry_url'])[0], 200)
                self.assertEqual(self.request(a['thumbnail_url'])[0], 200)
                self.assertEqual(f.request(b['geometry_url'], cookie=outsider)[0], 404)
                if a['laser']['status'] == 'ready':
                    for key in ('svg_url', 'dxf_url'):
                        self.assertTrue(a['laser'][key].startswith(staff_prefix+'manufacturing/'))
                        own = self.request(a['laser'][key]); original = f.request(b['laser'][key], cookie=owner)
                        self.assertEqual((own[0], own[1]), original)
                        self.assertEqual(f.request(b['laser'][key], cookie=outsider)[0], 404)
                else:
                    self.assertIsNone(a['laser']['dxf_url'])
                    self.assertEqual(self.request(staff_prefix+'manufacturing/'+a['id']+'/laser.dxf')[0], 404)
            def technical_only(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        self.assertNotIn(key, fixtures.PRIVATE)
                        self.assertFalse(key.startswith('_'))
                        technical_only(child)
                elif isinstance(value, list):
                    for child in value: technical_only(child)
            technical_only(staff)
        self.assertEqual({path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob('*') if path.is_file()}, before)
        self.assertEqual(f.request(order['quote']['manufacturing_url'], cookie=outsider)[0], 404)
        self.assertEqual(f.request(order['quote']['quote_url'], cookie=owner)[0], 200)
        self.assertEqual(f.request(order['quote']['quote_url'], cookie=outsider)[0], 404)
        self.assertEqual(len(f.server.customer_batches.get(batch)['runs']), 1)

    def test_staff_native_source_mismatch_cannot_publish_assets(self):
        f = self.fixture; owner = f.session(); order = f.finished(owner)
        prefix = ADMIN_API_PREFIX+'/'+order['id']+'/runs/'+order['quote']['run_id']+'/'
        f.cad.documents[f.native_id]['sha256'] = '0'*64
        status, raw, _ = self.request(prefix+'manufacturing')
        self.assertEqual(status, 200, raw)
        for part in json.loads(raw)['parts']:
            self.assertIsNone(part['geometry_url']); self.assertIsNone(part['thumbnail_url'])
            self.assertIsNone(part['laser']['dxf_url'])
            self.assertEqual(self.request(prefix+'manufacturing/'+part['id']+'/laser.svg')[0], 404)
        for suffix in ('manufacturing/L0001/internal-cost.csv', 'manufacturing/L0001/S0001-cut.dxf',
                       'manufacturing/L0001/laser.dxf?source=staff'):
            self.assertEqual(self.request(prefix+suffix)[0], 404)


if __name__ == '__main__': unittest.main()
