"""Staff/customer document evidence must share the existing native cache.

Execute on ZeroClave only. This uses a synthetic cached face-measurement
fixture, not a native CAD run, AI inference or a production quotation change.
The unit upstream is loopback; real Nginx protects the staff namespace.
"""
import copy
from html.parser import HTMLParser
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from batch_customer_quote import render_customer_quote
import test_customer_process_http as fixture

ADMIN = '/ortho2cad-test/customer-upload-admin/api/quote-batches'


class Assets(HTMLParser):
    def __init__(self, source):
        super().__init__(); self.images = []; self.links = []; self.feed(source)
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'img' and 'data-customer-thumbnail' in values: self.images.append(values)
        if tag == 'a': self.links.append(values)


class StaffCustomerQuoteEvidenceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.CustomerProcessHTTPTests(methodName='test_bent_part_does_not_offer_flat_pattern_or_full_quotation')
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        f = self.fixture; f.mixed = True
        self.owner, self.outsider = f.session(), f.session(); self.order = f.finished(self.owner)
        self.batch, self.run = self.order['id'], self.order['quote']['run_id']
        self.customer = fixture.API + '/orders/' + self.batch + '/runs/' + self.run
        self.staff = ADMIN + '/' + self.batch + '/runs/' + self.run
        # Only the native cache gains new observations; the immutable quoted
        # result deliberately retains its earlier one-value thickness snapshot.
        solid = f.manufacturing.geometries[f.native_id]['solids'][1]
        solid['thickness'] = {'status': 'unresolved', 'value_mm': None, 'method': 'parallel_faces',
            'confidence': 'none', 'reason': 'Synthetic mixed local wall measurements; no uniform wall adopted.',
            'warnings': [], 'evidence': [
                {'kind': 'parallel_faces', 'face_ids': ['F0001', 'F0002'], 'thickness_mm': 1, 'support_area_mm2': 40},
                {'kind': 'parallel_faces', 'face_ids': ['F0003', 'F0004'], 'thickness_mm': 1.2, 'support_area_mm2': 20}]}

    def facts(self, page):
        found = re.search(r'<section class="customer-weight-evidence" data-customer-weight-evidence data-line-id="L0002">(.*?)</section>', page, re.S)
        self.assertIsNotNone(found)
        return found.group(1)

    def test_staff_and_owner_quote_and_print_share_fresh_scoped_evidence_without_repricing(self):
        f = self.fixture; service = f.server.customer_batches; folder = service._folder(self.batch)
        before = {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
        with (patch.object(service, '_native', side_effect=AssertionError('No new CAD work')),
              patch.object(service.engineering_recognition, 'enqueue', side_effect=AssertionError('No AI')),
              patch.object(service.cost_sheet, 'reprice', side_effect=AssertionError('No reprice'))):
            status, customer_manifest = f.request(self.customer + '/manufacturing', cookie=self.owner)
            self.assertEqual(status, 200, customer_manifest)
            status, staff_manifest = f.request(self.staff + '/manufacturing')
            self.assertEqual(status, 200, staff_manifest)
            self.assertEqual(customer_manifest['parts'][1]['weight_evidence'], staff_manifest['parts'][1]['weight_evidence'])
            self.assertIsNone(staff_manifest['parts'][1]['weight_evidence']['thickness_mm'])
            for name in ('customer-quote.html', 'customer-print.html'):
                status, customer = f.request(self.customer + '/' + name, cookie=self.owner)
                self.assertEqual(status, 200, customer); customer = customer.decode()
                status, staff = f.request(self.staff + '/' + name)
                self.assertEqual(status, 200, staff); staff = staff.decode()
                self.assertEqual(self.facts(customer), self.facts(staff))
                self.assertIn('1.00 / 1.20 mm · 局部量测候选，未采用为统一板厚', self.facts(staff))
                self.assertEqual(re.findall(r'data-total-price="([^"]*)"', customer), re.findall(r'data-total-price="([^"]*)"', staff))
                for page in (customer, staff):
                    self.assertNotIn('内部成本', page); self.assertNotIn('rate_percent', page)
                staff_assets, own_assets = Assets(staff), Assets(customer)
                self.assertEqual(len(staff_assets.images), 2); self.assertEqual(len(own_assets.images), 2)
                self.assertTrue(all(image['src'].startswith(self.staff + '/customer-parts/') for image in staff_assets.images))
                self.assertTrue(all(image['src'].startswith(self.customer + '/customer-parts/') for image in own_assets.images))
                self.assertNotIn(self.customer + '/', staff); self.assertNotIn(ADMIN, customer)
                processes = [link['href'] for link in staff_assets.links if 'data-customer-process-link' in link]
                self.assertTrue(processes); self.assertTrue(all(url.startswith('/workbench/?workspace=orders&scope=customer&') for url in processes))
                if name == 'customer-print.html':
                    self.assertIn('/workbench/customer/print.js', staff); self.assertNotIn('onclick="window.print()"', staff)
        self.assertEqual(before, {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob('*') if path.is_file()})
        self.assertEqual(len(service.get(self.batch)['runs']), 1)

    def test_staff_customer_documents_use_shared_renderer_but_private_exports_still_use_batch_service(self):
        f = self.fixture; service = f.server.customer_batches
        with patch.object(service, 'artifact', wraps=service.artifact) as original:
            for name in ('customer-quote.html', 'customer-print.html'):
                self.assertEqual(f.request(self.staff + '/' + name)[0], 200)
            original.assert_not_called()
            for name in ('quote.json', 'internal-cost.csv'):
                self.assertEqual(f.request(self.staff + '/' + name)[0], 200)
                original.assert_called_with(self.batch, self.run, name)
        for name in ('customer-quote.html', 'customer-print.html'):
            self.assertEqual(f.request(self.customer + '/' + name, cookie=self.outsider)[0], 404)
            self.assertEqual(f.request(self.customer + '/' + name)[0], 401)
        self.assertEqual(f.request(self.customer + '/quote.json', cookie=self.owner)[0], 404)

    def test_customer_authorization_precedes_shared_render_and_invalid_artifacts_stay_rejected(self):
        f = self.fixture
        with patch.object(f.server.customer_upload, '_artifact', side_effect=AssertionError('Unauthorized render')):
            self.assertEqual(f.request(self.customer + '/customer-quote.html', cookie=self.outsider)[0], 404)
            self.assertEqual(f.request(self.customer + '/customer-quote.html')[0], 401)
        self.assertEqual(f.request(self.staff.replace(self.run, 'RUN-' + 'f'*32) + '/customer-quote.html')[0], 404)
        self.assertEqual(f.request(self.staff + '/customer-print.html?source=customer')[0], 404)

    def test_renderer_accepts_only_fixed_source_namespaces_and_customer_default_is_compatible(self):
        f = self.fixture; saved = f.server.customer_batches.get(self.batch)['result']; before = copy.deepcopy(saved)
        staff_parts = f.server.customer_upload.staff_manufacturing(self.batch, self.run)['parts']
        own_parts = f.server.customer_upload.manufacturing(f.server.customer_upload.session(self.owner), self.batch, self.run)['parts']
        self.assertFalse(Assets(render_customer_quote(saved, customer_parts=staff_parts).decode()).images)
        self.assertFalse(Assets(render_customer_quote(saved, customer_parts=own_parts, technical_source='staff').decode()).images)
        forged = copy.deepcopy(staff_parts)
        forged[0]['thumbnail_url'] = 'https://outside.invalid/thumbnail'
        forged[0]['geometry_url'] = self.staff.replace(self.batch, 'BATCH-' + 'e'*32) + '/customer-parts/L0001'
        page = render_customer_quote(saved, customer_parts=forged, technical_source='staff').decode()
        self.assertNotIn('outside.invalid', page); self.assertNotIn('BATCH-' + 'e'*32, page)
        for source in ('https://outside.invalid', 'staff/../customer', ''):
            with self.assertRaises(ValueError): render_customer_quote(saved, customer_parts=own_parts, technical_source=source)
        self.assertEqual(render_customer_quote(saved, customer_parts=own_parts),
                         render_customer_quote(saved, customer_parts=own_parts, technical_source='customer'))
        self.assertEqual(saved, before)


if __name__ == '__main__': unittest.main()
