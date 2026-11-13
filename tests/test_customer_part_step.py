"""Owner-bound saved single-part STEP downloads, without any CAD generation.

Run only on ZeroClave. Geometry and STEP bytes below are explicit protocol
fixtures, not a claim that the synthetic STEP underwent real native analysis.
"""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from workbench_store import WorkbenchError
import test_customer_process_http as fixture

ADMIN = '/ortho2cad-test/customer-upload-admin/api/quote-batches'


def export_receipt():
    return {'status': 'engineering_draft', 'solid_count': 1,
            'volume_relative_delta': 0, 'max_bound_delta_mm': 0,
            'volume_relative_limit': 1e-5, 'max_bound_delta_limit_mm': 1e-5}


def part_bytes(identity):
    return ('ISO-10303-21;\nHEADER;\n/* Synthetic single-part protocol fixture ' + identity
            + ' */\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n').encode()


class CustomerPartStepHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.CustomerProcessHTTPTests(methodName='test_bent_part_does_not_offer_flat_pattern_or_full_quotation')
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)
        f = self.fixture; f.mixed = True
        self.owner, self.outsider = f.session(), f.session()
        self.order = f.finished(self.owner)
        self.geometry = f.manufacturing.geometries[f.native_id]
        for solid in self.geometry['solids']:
            solid['export_validation'] = export_receipt()
            f.manufacturing.files[(f.native_id, solid['step_file'])] = part_bytes(solid['id'])
        self.prefix = fixture.API + '/orders/' + self.order['id'] + '/runs/' + self.order['quote']['run_id']
        self.url = self.prefix + '/customer-parts/L0002/step'

    def manifest(self):
        status, value = self.fixture.request(self.prefix + '/manufacturing', cookie=self.owner)
        self.assertEqual(status, 200, value)
        return value

    def test_owner_download_is_exact_registered_single_step_with_attachment_and_no_new_work(self):
        f = self.fixture; folder = f.server.customer_batches._folder(self.order['id'])
        before = {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
        with (patch.object(f.server.customer_batches, '_native', side_effect=AssertionError('No native generation')),
              patch.object(f.server.customer_batches.engineering_recognition, 'enqueue', side_effect=AssertionError('No model'))):
            manifest = self.manifest(); f.assert_no_costs(manifest)
            target = next(part for part in manifest['parts'] if part['id'] == 'L0002')
            self.assertEqual(target['step_url'], self.url)
            with urlopen(Request(f.base + self.url, headers={'Cookie': self.owner}), timeout=10) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers.get_content_type(), 'application/step')
                self.assertEqual(response.headers['Content-Disposition'],
                                 'attachment; filename="L0002.step"; filename*=UTF-8\'\'L0002.step')
                self.assertEqual(response.read(), part_bytes('S0002'))
            # A supplied per-part export is distinct from both the assembly and
            # every commercial artifact. Nothing is regenerated on download.
            source, _, _ = f.server.customer_batches.file(self.order['id'], 'F0001')
            self.assertNotEqual(source, part_bytes('S0002'))
        self.assertEqual(before, {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob('*') if path.is_file()})
        self.assertEqual(len(f.server.customer_batches.get(self.order['id'])['runs']), 1)

    def test_owner_is_checked_before_any_artifact_lookup_and_queries_or_bad_ids_are_rejected(self):
        f = self.fixture
        with patch.object(f.server.customer_upload, '_part_step', side_effect=AssertionError('Foreign lookup must never run')):
            self.assertEqual(f.request(self.url, cookie=self.outsider)[0], 404)
            self.assertEqual(f.request(self.url)[0], 401)
        for path in (self.url + '?file=quote.json', self.url.replace('L0002', 'L9999'),
                     self.url.replace('/step', '/S0002.step'), self.url.replace('L0002', '..')):
            self.assertEqual(f.request(path, cookie=self.owner)[0], 404, path)

    def test_staff_route_reuses_same_reader_and_manifest_uses_staff_prefix(self):
        # Unit HTTP runs on the protected upstream loopback. Real Nginx Basic
        # enforcement is checked by the separate browser fixture.
        f = self.fixture
        prefix = ADMIN + '/' + self.order['id'] + '/runs/' + self.order['quote']['run_id']
        status, manifest = f.request(prefix + '/manufacturing')
        self.assertEqual(status, 200, manifest)
        target = next(part for part in manifest['parts'] if part['id'] == 'L0002')
        self.assertEqual(target['step_url'], prefix + '/customer-parts/L0002/step')
        status, content = f.request(target['step_url'])
        self.assertEqual(status, 200); self.assertEqual(content, part_bytes('S0002'))
        with urlopen(f.base + target['step_url'], timeout=10) as response:
            self.assertEqual(response.headers['Content-Disposition'],
                             'attachment; filename="L0002.step"; filename*=UTF-8\'\'L0002.step')

    def test_missing_receipt_rejected_export_missing_file_and_nonstep_bytes_have_no_url(self):
        f = self.fixture; original = copy.deepcopy(self.geometry); assets = copy.deepcopy(f.manufacturing.files)
        for change in ('legacy', 'rejected', 'missing', 'not_step'):
            with self.subTest(change=change):
                f.manufacturing.geometries[f.native_id] = copy.deepcopy(original)
                f.manufacturing.files = copy.deepcopy(assets)
                solid = f.manufacturing.geometries[f.native_id]['solids'][1]
                if change == 'legacy': solid.pop('export_validation')
                elif change == 'rejected':
                    solid['step_file'] = None
                    solid['export_validation'].update(status='rejected', reason='Synthetic rejected derivative')
                elif change == 'missing': f.manufacturing.files.pop((f.native_id, solid['step_file']))
                else: f.manufacturing.files[(f.native_id, solid['step_file'])] = b'<html>quotation document</html>'
                target = next(part for part in self.manifest()['parts'] if part['id'] == 'L0002')
                self.assertIsNone(target['step_url']); self.assertEqual(f.request(self.url, cookie=self.owner)[0], 404)

    def test_source_or_registered_hash_failure_cannot_publish_or_download_step(self):
        f = self.fixture
        with patch.object(f.manufacturing, 'file', side_effect=WorkbenchError('Synthetic registered artifact hash mismatch')):
            self.assertTrue(all(part['step_url'] is None for part in self.manifest()['parts']))
            self.assertEqual(f.request(self.url, cookie=self.owner)[0], 404)
        f.cad.documents[f.native_id]['sha256'] = '0'*64
        self.assertTrue(all(part['step_url'] is None for part in self.manifest()['parts']))
        self.assertEqual(f.request(self.url, cookie=self.owner)[0], 404)

    def test_multiple_representative_solids_cannot_offer_an_assembly_as_single_part(self):
        f = self.fixture; service = f.server.customer_batches
        original = service._preview_match
        def composite(line, geometry, siblings):
            value = original(line, geometry, siblings)
            value['representative_solid_ids'] = ['S0001', 'S0002']
            return value
        with patch.object(service, '_preview_match', side_effect=composite):
            self.assertTrue(all(part['step_url'] is None for part in self.manifest()['parts']))
            self.assertEqual(f.request(self.url, cookie=self.owner)[0], 404)


if __name__ == '__main__': unittest.main()
