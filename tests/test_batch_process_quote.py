"""Independent process-cost examples; no native engine/model or local runner."""
import copy
from decimal import Decimal
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_costing import calculate_batch, validate_settings
from test_batch_quote_costing import document, settings, solid


def process_settings(**changes):
    return settings(order_sets=10, costing_mode='process-v1', **changes)


def flat(index=1, *, definition='panel', occurrence=None, grade='Q235', name='Panel'):
    body = solid(index, definition=definition + '/solid-1', path=occurrence or ['root', str(index)],
                 volume=12000 - 36*math.pi, grade=grade, name=name)
    body['id'] = 'S' + str(index).zfill(4)
    body['process_evidence'] = {
        'schema_version': 1, 'solid_id': body['id'],
        'laser_cut': {'status':'ready','unit':'mm','thickness_mm':'2',
                      'cut_length_mm':str(320 + 12*math.pi), 'pierce_count':3, 'contour_count':3,
                      'net_area_mm2':str(6000 - 18*math.pi), 'blank_width_mm':'100', 'blank_height_mm':'60',
                      'source_face_id':'F0001','dxf_file':body['id']+'-cut.dxf'},
        'bends': {'status':'not_applicable','candidate_count':0,'candidates':[]}}
    return body


def bent(index=2):
    body = solid(index, definition='bracket/solid-1', volume=20000, grade='Q235', name='Bracket')
    body['id'] = 'S' + str(index).zfill(4)
    body['process_evidence'] = {'schema_version':1,'solid_id':body['id'],
        'laser_cut':{'status':'unsupported','reason':'Folded solid has no flat blank'},
        'bends':{'status':'candidate','candidate_count':1,'candidates':[
            {'id':'B0001','status':'pending_review','rotation_angle_deg':90,'included_angle_deg':90,
             'inner_radius_mm':2,'thickness_mm':2,'length_mm':50,'direction':'unconfirmed'}]}}
    return body


class ProcessQuoteTests(unittest.TestCase):
    def quote(self, bodies=None, **changes):
        return calculate_batch([document(bodies or [flat()])], process_settings(**changes))

    def test_flat_cut_and_piercing_have_independent_known_quantities_and_costs(self):
        result = self.quote()
        line = result['lines'][0]
        self.assertTrue(line['priced'])
        operations = {row['kind']:row for row in line['manufacturing']['processes']}
        self.assertEqual(operations['laser_piercing']['quantity_total'], '30')
        self.assertEqual(operations['laser_piercing']['amount_cny'], '4.50')
        self.assertEqual(operations['laser_cut']['amount_cny'], '4.29')
        self.assertEqual(line['process_cost'], '8.79')
        self.assertEqual(line['setup_cost'], '20.00')
        self.assertEqual(line['material_usage_kg'], '0.0942')
        self.assertEqual(line['material_cost'], '5.65')
        self.assertEqual(result['totals']['total_cost'], '34.44')
        self.assertEqual(result['totals']['total_price'], '41.30')
        self.assertTrue(result['manufacturing']['complete'])
        self.assertEqual(line['manufacturing']['weight_check']['status'], 'consistent')

    def test_processing_cost_does_not_change_with_material_density_or_mass(self):
        steel, aluminium = self.quote(), self.quote([flat(grade='5052')])
        first, second = steel['lines'][0], aluminium['lines'][0]
        self.assertNotEqual(first['estimated_net_weight_kg'], second['estimated_net_weight_kg'])
        self.assertNotEqual(first['material_cost'], second['material_cost'])
        self.assertEqual(first['process_cost'], second['process_cost'])
        self.assertEqual(first['setup_cost'], second['setup_cost'])
        self.assertEqual(first['manufacturing']['features'], second['manufacturing']['features'])

    def test_repeated_instances_are_counted_once_per_occurrence_not_as_extra_setups(self):
        result = self.quote([flat(1), flat(2)])
        self.assertEqual(len(result['lines']), 1)
        line = result['lines'][0]
        self.assertEqual((line['quantity_per_set'],line['quantity_total']), (2,20))
        self.assertEqual(line['process_cost'], '17.58')
        self.assertEqual(line['setup_cost'], '20.00')
        self.assertEqual(line['manufacturing']['source_solid_ids'], ['S0001'])

    def test_laser_holes_do_not_create_drilling_or_tapping_fees(self):
        operations = self.quote()['lines'][0]['manufacturing']['processes']
        self.assertEqual({row['kind'] for row in operations}, {'laser_cut','laser_piercing','laser_setup'})
        self.assertTrue(next(row for row in operations if row['kind']=='laser_piercing')['evidence']['does_not_imply_drilling'])
        self.assertIn('非机床刀路', next(row for row in operations if row['kind']=='laser_piercing')['evidence']['basis'])

    def test_bent_candidate_has_known_bending_subtotal_but_no_complete_price(self):
        result = self.quote([bent()])
        line = result['lines'][0]
        self.assertFalse(line['priced'])
        self.assertIsNone(line['process_cost'])
        self.assertIsNone(result['totals']['total_price'])
        self.assertEqual(line['manufacturing']['estimated_cost_subtotal'], '10.00')
        self.assertEqual(result['manufacturing']['status'], 'partial')
        self.assertEqual({row['code'] for row in line['manufacturing']['blockers']},
                         {'flat_pattern_required','bending_adoption_required'})
        operation = line['manufacturing']['processes'][0]
        self.assertEqual(operation['status'], 'pending_review')
        self.assertEqual(operation['evidence']['candidates'][0]['direction'], 'unconfirmed')

    def test_partially_known_order_never_presents_subtotal_as_entire_order_quote(self):
        result = self.quote([flat(),bent()])
        self.assertEqual((result['totals']['priced_lines'],result['totals']['unpriced_lines']), (1,1))
        self.assertIsNone(result['totals']['total_cost'])
        self.assertIsNone(result['totals']['total_price'])
        self.assertEqual(result['manufacturing']['estimated_cost_subtotal'], '44.44')
        self.assertEqual(result['manufacturing']['estimated_price_subtotal'], '53.30')
        self.assertFalse(result['manufacturing']['complete'])

    def test_missing_process_evidence_never_falls_back_to_weight_fee(self):
        body = flat()
        del body['process_evidence']
        result = self.quote([body])
        line = result['lines'][0]
        self.assertGreater(Decimal(line['estimated_net_weight_kg']),0)
        self.assertIsNone(line['process_cost'])
        self.assertIsNone(line['setup_cost'])
        self.assertEqual(result['manufacturing']['status'], 'blocked')
        self.assertNotIn('综合加工演示费', [row['name'] for row in result['assumptions']])
        self.assertFalse(any('max(5' in str(row) for row in result['assumptions']))

    def test_explicit_unquantified_requirements_are_retained_and_block_the_complete_price(self):
        result = self.quote([flat(name='焊接板 喷粉 钻孔')])
        line = result['lines'][0]
        self.assertIsNone(result['totals']['total_price'])
        required = {row['kind']:row for row in line['manufacturing']['processes'] if row['status']=='pending_review'}
        self.assertEqual(set(required), {'welding','surface_treatment','drilling_tapping'})
        for row in required.values():
            self.assertIsNone(row['quantity_total'])
            self.assertIsNone(row['amount_cny'])
            self.assertIn('焊接板', row['evidence']['source_text'])

    def test_volume_crosscheck_conflict_blocks_price_without_hiding_weight(self):
        body = flat();body['volume_mm3'] += 100
        result = self.quote([body])
        line = result['lines'][0]
        self.assertEqual(line['manufacturing']['weight_check']['status'], 'conflict')
        self.assertIsNotNone(line['estimated_net_weight_kg'])
        self.assertIsNone(result['totals']['total_price'])

    def test_instance_feature_conflict_cannot_share_one_manufacturing_rate_line(self):
        first,second = flat(1),flat(2)
        second['process_evidence']['laser_cut']['cut_length_mm'] = '900'
        line = self.quote([first,second])['lines'][0]
        self.assertFalse(line['priced'])
        self.assertEqual(line['manufacturing']['blockers'][0]['code'], 'instance_features_conflict')

    def test_purchase_candidate_needs_its_actual_specification_price(self):
        line = self.quote([flat(name='CLS-M6-2')])['lines'][0]
        self.assertEqual(line['mode'], 'buy')
        self.assertFalse(line['priced'])
        self.assertIsNone(line['purchase_cost'])
        self.assertEqual(line['manufacturing']['blockers'][0]['code'], 'purchase_price_required')
        self.assertFalse(any('0.80' in row for row in line['notes']))

    def test_gross_margin_stays_distinct_from_cost_plus(self):
        self.assertEqual(self.quote(pricing_method='gross_margin')['totals']['total_price'], '43.10')

    def test_invalid_process_metric_does_not_produce_a_zero_or_weight_based_price(self):
        for value in ('NaN','Infinity','-1',True,None):
            with self.subTest(value=value):
                body = flat();body['process_evidence']['laser_cut']['cut_length_mm'] = value
                result = self.quote([body])
                self.assertFalse(result['lines'][0]['priced'])
                self.assertIsNone(result['totals']['total_price'])

    def test_legacy_settings_and_results_remain_unchanged(self):
        source = [document([flat()])]
        old_settings = settings(order_sets=10)
        before = copy.deepcopy(source)
        legacy = calculate_batch(source, old_settings)
        self.assertNotIn('manufacturing', legacy)
        self.assertNotIn('costing_mode',legacy['settings'])
        self.assertEqual(legacy['lines'][0]['process_cost'], '50.00')
        self.assertEqual(legacy['lines'][0]['setup_cost'], '30.00')
        calculate_batch(source, process_settings())
        self.assertEqual(source,before)
        self.assertEqual(calculate_batch(source,old_settings),legacy)
        with self.assertRaises(ValueError):
            validate_settings(settings(costing_mode='imaginary'))


if __name__ == '__main__':
    unittest.main()
