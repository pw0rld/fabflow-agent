"""Customer-visible weight inputs, read from saved native evidence only.

Run on ZeroClave only. The fixtures are synthetic scalar geometry; these tests
never ask an engine/model to infer a thickness, material or production drawing.
"""
import copy
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from batch_customer_quote import customer_part_projection, render_customer_quote
from batch_engineering_native import enrich_native
from batch_quote_costing import calculate_batch, render_artifact
from customer_weight_view import weight_evidence, weight_projection, result_weight_evidence, with_technical_evidence
from prep_record import digest
from test_batch_engineering_native import body, thickness
from test_batch_quote_costing import document, settings
import test_customer_process_http as http_fixture


def saved_case(*, occurrences=1):
    bodies = []
    for index in range(1, occurrences + 1):
        row = body(index, offset=(index-1)*2000)
        row['volume_mm3'] = 1827330.18791872
        row['bounds_mm'] = {'min': [(index-1)*2000, 0, 0],
                            'max': [(index-1)*2000+1490, 200, 60], 'size': [1490, 200, 60]}
        row['thickness'] = thickness(4)
        row['material'] = {'status': 'unresolved', 'value': None, 'entries': [], 'reason': '未赋材料', 'warnings': []}
        bodies.append(row)
    source = document(bodies)
    value = calculate_batch([source], settings(order_sets=10))
    value['engineering_native'] = enrich_native(value, {source['id']: source['geometry']})
    return value, source['geometry']


class CustomerWeightEvidenceTests(unittest.TestCase):
    def test_saved_volume_is_single_part_not_occurrence_or_order_volume(self):
        value, _ = saved_case(occurrences=2); before = copy.deepcopy(value)
        line = value['lines'][0]; facts = result_weight_evidence(value)[line['id']]
        self.assertEqual(line['quantity_per_set'], 2); self.assertEqual(line['quantity_total'], 20)
        self.assertEqual(facts['native_volume_mm3'], '1827330.18791872')
        self.assertEqual(facts['volume_scope'], 'per_piece'); self.assertEqual(facts['volume_status'], 'native')
        self.assertEqual(facts['dimensions_mm'], ['1490', '200', '60'])
        self.assertEqual(facts['thickness_mm'], '4'); self.assertEqual(facts['thickness_status'], 'candidate')
        self.assertEqual(facts['density_kg_m3'], '7850'); self.assertEqual(facts['material_source'], 'order_assumption')
        self.assertIsNone(facts['cad_material_text']); self.assertIn('Q235', facts['adopted_material_label'])
        self.assertEqual(value, before)

    def test_source_mismatch_unknown_volume_and_bbox_do_not_invent_thickness_or_volume(self):
        value, _ = saved_case(); line = value['lines'][0]; native = value['engineering_native']['lines'][line['id']]
        for sha in (None, 'b'*64, '<invalid>'):
            facts = weight_evidence(line, native, source_sha256=sha)
            self.assertIsNone(facts['dimensions_mm']); self.assertIsNone(facts['thickness_mm'])
        missing = copy.deepcopy(line); missing['volume_mm3'] = None
        facts = weight_evidence(missing)
        self.assertIsNone(facts['native_volume_mm3'], 'A saved weight/density must never be inverted to invent volume.')
        self.assertEqual(facts['volume_status'], 'unavailable')
        no_measurement = copy.deepcopy(native); no_measurement['evidence'].pop('thickness_mm')
        self.assertIsNone(weight_evidence(line, no_measurement, source_sha256='a'*64)['thickness_mm'])

    def test_rough_and_proxy_saved_volumes_are_explicitly_marked(self):
        value, _ = saved_case()
        for confidence, status, marker in (('low', 'low_confidence', '异常几何粗估'),
                                           ('proxy_material_estimate', 'proxy', '代理模型')):
            line = value['lines'][0]; line['confidence'] = confidence
            self.assertEqual(weight_projection(line)['weight_evidence']['volume_status'], status)
            page = render_customer_quote(value).decode()
            self.assertIn('单件体积依据', page); self.assertIn(marker, page)
            self.assertNotIn('单件原生体积：', page)

    def test_technical_projection_cannot_replace_saved_volume_density_material_or_private_text(self):
        value, _ = saved_case(); line = value['lines'][0]
        technical = {'weight_evidence': {**result_weight_evidence(value)[line['id']],
            'native_volume_mm3': '1', 'density_kg_m3': '1', 'adopted_material_label': 'PRIVATE-RATE-999',
            'material_description': 'PRIVATE-MARGIN-80', 'material_source': 'cad_candidate'}}
        facts = with_technical_evidence(line, technical)
        self.assertEqual(facts['native_volume_mm3'], line['volume_mm3']); self.assertEqual(facts['density_kg_m3'], '7850')
        self.assertEqual(facts['material_source'], 'order_assumption'); self.assertNotIn('PRIVATE', str(facts))
        technical['weight_evidence'].update(dimensions_mm=[True, 2, 3], thickness_mm='NaN')
        facts = with_technical_evidence(line, technical)
        self.assertIsNone(facts['dimensions_mm']); self.assertIsNone(facts['thickness_mm'])

    def test_customer_quote_and_print_default_visible_inputs_are_two_decimals(self):
        value, _ = saved_case(); before = digest(value)
        exports = {name: render_artifact(value, name)[0] for name in ('quote.json', 'bom.csv', 'cost.csv')}
        for printable in (False, True):
            page = render_customer_quote(value, printable=printable).decode()
            self.assertIn('<section class="customer-weight-evidence" data-customer-weight-evidence data-line-id="L0001">', page)
            for key in ('volume', 'dimensions', 'thickness', 'material', 'density', 'source'):
                self.assertIn('data-weight-evidence="'+key+'"', page)
            for text in ('1,827,330.19 mm³', '1,490.00 × 200.00 × 60.00 mm · 非展开',
                         '4.00 mm · CAD量测候选', '7,850.00 kg/m³', '本单材料假设'):
                self.assertIn(text, page)
            self.assertNotIn('<details class="customer-weight-basis"', page)
            self.assertIn('.customer-weight-evidence{display:block!important', page)
        self.assertEqual(digest(value), before)
        self.assertEqual(exports, {name: render_artifact(value, name)[0] for name in exports})

    def test_manifest_facts_supply_cached_candidates_without_changing_original_result(self):
        value, _ = saved_case(); original = copy.deepcopy(value)
        facts = result_weight_evidence(value)['L0001']; facts['dimensions_source'] = facts['thickness_source'] = 'cached_native_geometry'
        value.pop('engineering_native')
        parts = [{'id': 'L0001', 'weight_evidence': facts}]
        page = render_customer_quote(value, customer_parts=parts).decode()
        self.assertIn('4.00 mm · CAD量测候选', page)
        self.assertIn('1,490.00 × 200.00 × 60.00 mm', page)
        self.assertEqual(value['lines'], original['lines']); self.assertNotIn('engineering_native', value)
        for line in value['lines']:
            line['estimated_net_weight_kg'] = line['estimated_set_net_weight_kg'] = line['estimated_total_net_weight_kg'] = None
        page = render_customer_quote(value, customer_parts=parts).decode()
        self.assertIn('尚未估算', page); self.assertIn('1,827,330.19 mm³', page)

    def test_customer_geometry_projection_exposes_only_saved_volume_and_safe_material_basis(self):
        value, _ = saved_case(); line = copy.deepcopy(value['lines'][0])
        line.update(material_rate='PRIVATE-RATE', material_assumption='PRIVATE-ASSUMPTION', weight_basis='PRIVATE-COST')
        projected = customer_part_projection({'line_snapshot': line})['line_snapshot']
        self.assertEqual(projected['native_volume_mm3'], '1827330.18791872')
        self.assertEqual(projected['weight_evidence']['volume_scope'], 'per_piece')
        self.assertNotIn('PRIVATE', str(projected)); self.assertNotIn('material_rate', projected)


class CachedWeightEvidenceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = http_fixture.CustomerProcessHTTPTests(methodName='test_bent_part_does_not_offer_flat_pattern_or_full_quotation')
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown); self.fixture.mixed = True

    def test_legacy_missing_features_use_verified_cached_face_measurements_read_only(self):
        f = self.fixture; owner, outsider = f.session(), f.session(); order = f.finished(owner)
        service = f.server.customer_batches
        state, result = service._preview_run(order['id'], order['quote']['run_id'])
        legacy = copy.deepcopy(result); legacy.pop('engineering_native', None)
        for line in legacy['lines']: line.pop('manufacturing', None)
        folder = service._folder(order['id'])
        before = {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
        with (patch.object(service, '_preview_run', return_value=(state, legacy)),
              patch.object(service, '_native', side_effect=AssertionError('No new CAD work')),
              patch.object(service.engineering_recognition, 'enqueue', side_effect=AssertionError('No model'))):
            status, manifest = f.request(order['quote']['manufacturing_url'], cookie=owner)
            self.assertEqual(status, 200, manifest); f.assert_no_costs(manifest)
            for index, part in enumerate(manifest['parts']):
                self.assertEqual(part['weight_evidence']['thickness_source'], 'cached_native_geometry')
                self.assertEqual(Decimal(part['thickness_mm']), Decimal('1.5' if index == 0 else '1'))
                self.assertEqual(part['weight_evidence']['dimensions_source'], 'cached_native_geometry')
                self.assertEqual(part['native_volume_mm3'], legacy['lines'][index]['volume_mm3'])
            self.assertEqual(f.request(order['quote']['manufacturing_url'], cookie=outsider)[0], 404)
        self.assertEqual(before, {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob('*') if path.is_file()})

    def test_wrong_cached_source_does_not_relabel_saved_or_guessed_measurements_as_native(self):
        f = self.fixture; owner = f.session(); order = f.finished(owner)
        f.cad.documents[f.native_id]['sha256'] = '0'*64
        state, result = f.server.customer_batches._preview_run(order['id'], order['quote']['run_id'])
        result.pop('engineering_native', None)
        with patch.object(f.server.customer_batches, '_preview_run', return_value=(state, result)):
            status, manifest = f.request(order['quote']['manufacturing_url'], cookie=owner)
        self.assertEqual(status, 200, manifest)
        for part in manifest['parts']:
            self.assertIsNone(part['thickness_mm']); self.assertIsNone(part['weight_evidence']['thickness_mm'])
            self.assertIsNone(part['weight_evidence']['dimensions_mm'])
            self.assertEqual(part['weight_evidence']['thickness_source'], 'unavailable')


if __name__ == '__main__': unittest.main()
