"""Scalar quote changes; run on ZeroClave, never on the local computer."""
import copy
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_amendment import amend_quote
from batch_quote_costing import AMOUNT_KEYS, calculate_batch
from batch_engineering_bom import _priced_requirement
from batch_engineering_pricing import pricing_plan, reprice_quote
from test_batch_quote_costing import document, settings, solid
from test_batch_engineering_pricing import add_requirement, fixture


def ordinary():
    rows = [solid(1, 'plate/solid-1'), solid(2, 'plate/solid-1')]
    rows += [solid(i, 'screw/solid-1', name='M4x20', volume=1000) for i in range(3, 7)]
    result = calculate_batch([document(rows)], settings())
    result.update(run_id='RUN-' + '1' * 32, batch_id='BATCH-' + '2' * 32)
    return result


def patch(*changes):
    return {'changes': list(changes)}


def global_change(field, value):
    return {'field': field, 'value': value}


def row_change(field, value, *identities, method=None):
    result = {'field': field, 'value': value, 'line_ids': list(identities)}
    if method is not None:
        result['method'] = method
    return result


def engineered(*, market=False):
    base, manifest = fixture(market=market)
    return reprice_quote(base, manifest, pricing_plan(base, manifest)['policy']), manifest


class BatchQuoteAmendmentTests(unittest.TestCase):
    def test_quantity_plus_markup_reprices_units_but_not_setup_and_keeps_inputs_immutable(self):
        base = ordinary()
        saved = copy.deepcopy(base)
        change = patch(global_change('order_sets', '6'), global_change('rate_percent', '15'))
        original_change = copy.deepcopy(change)
        preview = amend_quote(base, change)
        new = preview['result']
        self.assertEqual(new['totals']['total_price'], '1687.92')
        self.assertEqual(new['lines'][0]['quantity_total'], 12)
        self.assertEqual(new['lines'][1]['quantity_total'], 24)
        self.assertEqual(new['lines'][0]['material_cost'], '664.94')
        self.assertEqual(new['lines'][0]['process_cost'], '753.60')
        self.assertEqual(new['lines'][0]['setup_cost'], '30.00')
        self.assertEqual(new['lines'][0]['total_price'], '1665.84')
        self.assertEqual(new['lines'][1]['total_price'], '22.08')
        self.assertEqual(base, saved)
        self.assertEqual(change, original_change)
        self.assertEqual(preview['summary']['before_totals']['total_price'], '898.62')
        self.assertEqual(preview['summary']['after_order_sets'], 6)
        self.assertTrue(all(isinstance(row['before'], str) and isinstance(row['after'], str) for row in preview['summary']['changes']))
        self.assertEqual(new['source_files'], base['source_files'])
        for old, updated in zip(base['lines'], new['lines']):
            for key in ('id', 'file_id', 'native_definition', 'native_solid_count', 'quantity_per_set', 'volume_mm3', 'material_raw'):
                self.assertEqual(old[key], updated[key], key)
        self.assertEqual(new['status'], 'pending_review')
        self.assertTrue(new['simulated'])
        self.assertFalse(new['engineering_approved'])
        self.assertFalse(new['business_approved'])

    def test_purchase_change_affects_exact_scope_and_repeat_replaces_instead_of_adds(self):
        base = ordinary()
        first = amend_quote(base, patch(row_change('purchase_rate', '0.90', 'L0002')))['result']
        self.assertEqual(first['lines'][1]['purchase_cost'], '10.80')
        self.assertEqual(first['totals']['total_price'], '900.06')
        self.assertEqual(first['lines'][0], base['lines'][0])
        second = amend_quote(first, patch(row_change('purchase_rate', '1.20', 'L0002')))['result']
        self.assertEqual(second['lines'][1]['purchase_cost'], '14.40')
        self.assertEqual(second['totals']['total_price'], '904.38')
        self.assertEqual(second['lines'][1]['purchase_price_source_kind'], 'manual')
        self.assertNotIn('名称规则识别为外购候选；每件 0.80 元仅为演示价。', second['lines'][1]['notes'])
        self.assertEqual(len(second['quote_amendment']['lineage']), 2)

    def test_manual_generic_processing_survives_quantity_round_trip(self):
        base = ordinary()
        first = amend_quote(base, patch(row_change('process_rate', '10', 'L0001', method='generic_processing')))['result']
        self.assertEqual(first['lines'][0]['process_cost'], '60.00')
        self.assertEqual(first['totals']['total_price'], '518.46')
        more = amend_quote(first, patch(global_change('order_sets', '6')))['result']
        self.assertEqual(more['totals']['total_price'], '1000.92')
        self.assertEqual(more['lines'][0]['setup_cost'], '30.00')
        back = amend_quote(more, patch(global_change('order_sets', '3')))['result']
        self.assertEqual(back['totals'], first['totals'])

    def test_cost_plus_and_target_margin_have_different_deterministic_prices(self):
        base = ordinary()
        margin = amend_quote(base, patch(global_change('pricing_method', 'gross_margin')))['result']
        self.assertEqual(margin['totals']['total_price'], '936.06')
        restored = amend_quote(margin, patch(global_change('pricing_method', 'cost_plus')))['result']
        self.assertEqual(restored['totals']['total_price'], '898.62')
        self.assertEqual(restored['totals']['total_cost'], base['totals']['total_cost'])
        with self.assertRaisesRegex(ValueError, '小于'):
            amend_quote(base, patch(global_change('pricing_method', 'gross_margin'), global_change('rate_percent', '100')))

    def test_engineering_quantity_and_markup_keep_six_cost_proofs_and_one_unpriced_line(self):
        base, manifest = engineered()
        saved = copy.deepcopy(base)
        new = amend_quote(base, patch(global_change('order_sets', '20'), global_change('rate_percent', '15')))['result']
        self.assertEqual(new['totals']['total_price'], '4014.80')
        self.assertEqual(new['totals']['total_cost'], '3491.06')
        self.assertEqual((new['totals']['priced_lines'], new['totals']['unpriced_lines']), (4, 1))
        self.assertFalse(new['lines'][4]['priced'])
        self.assertEqual(new['lines'][4]['quantity_total'], 20)
        self.assertIsNone(new['lines'][4]['total_price'])
        self.assertEqual(new['lines'][0]['setup_cost'], '0.00')
        self.assertEqual(new['lines'][1]['setup_cost'], '0.00')
        self.assertEqual(new['lines'][2]['setup_cost'], '30.00')
        self.assertEqual(new['lines'][3]['setup_cost'], '30.00')
        snapshot = new['engineering_repricing']
        zinc = next(item for item in snapshot['applied_requirements'] if item['method'] == 'hotdip')
        self.assertEqual(zinc['cost_evidence']['amount'], '30.00')
        self.assertEqual(zinc['cost_evidence']['group_weight_kg'], '1.57')
        self.assertEqual(snapshot['after_totals'], new['totals'])
        lines = {line['id']: line for line in new['lines']}
        self.assertTrue(all(_priced_requirement(new, lines[item['line_id']], item)
                            for item in manifest['quote_basis']['requirements_review']))
        self.assertEqual(base, saved)

    def test_weld_and_mesh_purchase_rates_update_exact_components_and_sources(self):
        base, _ = engineered()
        new = amend_quote(base, patch(
            row_change('process_rate', '17.50', 'L0001', method='pipe_weld_seal'),
            row_change('purchase_rate', '2.25', 'L0002'),
            row_change('process_rate', '4', 'L0002', method='mesh_weld')))['result']
        self.assertEqual(new['lines'][0]['process_cost'], '175.00')
        self.assertEqual(new['lines'][1]['purchase_cost'], '22.50')
        self.assertEqual(new['lines'][1]['process_cost'], '40.00')
        self.assertEqual(new['lines'][0]['setup_cost'], '0.00')
        self.assertEqual(new['lines'][1]['setup_cost'], '0.00')
        for item in new['engineering_repricing']['applied_requirements']:
            if item['method'] in {'pipe_weld_seal', 'mesh_weld', 'purchase_mesh'}:
                self.assertEqual(item['cost_evidence']['source_kind'], 'manual')
                self.assertIn('用户指定', item['cost_evidence']['source_label'])
        assumptions = {item['name']: item['value'] for item in new['assumptions']}
        self.assertIn('17.5 元/件', assumptions['L0001 此圆管焊接固定/不能漏水漏油/316不锈钢'])
        self.assertIn('4 元/件', assumptions['L0002 焊接8目不锈钢纱网'])
        again = amend_quote(new, patch(row_change('process_rate', '15', 'L0001', method='pipe_weld_seal')))['result']
        self.assertEqual(again['lines'][0]['process_cost'], '150.00')
        self.assertEqual(again['lines'][0]['total_price'], base['lines'][0]['total_price'])

    def test_hotdip_minimum_applies_once_to_the_whole_group_and_repeated_edits_do_not_add(self):
        base, manifest = fixture()
        add_requirement(manifest, 'L0004', 'surface_treatment', '热浸锌处理')
        plan = pricing_plan(base, manifest)
        for entry in plan['policy']['entries']:
            if entry['method'] == 'hotdip':
                entry.update(rate='1.00', minimum='1000.00')
        source = reprice_quote(base, manifest, plan['policy'])
        minimum = amend_quote(source, patch(row_change('hotdip_minimum', '1200', 'L0003', 'L0004')))['result']
        values = [row['cost_evidence'] for row in minimum['engineering_repricing']['applied_requirements'] if row['method'] == 'hotdip']
        self.assertEqual(sum(Decimal(row['amount']) for row in values), Decimal('1200.00'))
        self.assertEqual({row['group_cost'] for row in values}, {'1200.00'})
        self.assertTrue(all(row['source_kind'] == 'manual' for row in values))
        rate = amend_quote(minimum, patch(row_change('hotdip_rate', '10', 'L0003', 'L0004')))['result']
        same_minimum = [row['cost_evidence'] for row in rate['engineering_repricing']['applied_requirements'] if row['method'] == 'hotdip']
        self.assertEqual(sum(Decimal(row['amount']) for row in same_minimum), Decimal('1200.00'))
        no_minimum = amend_quote(rate, patch(row_change('hotdip_minimum', '0', 'L0003', 'L0004')))['result']
        charged = [row['cost_evidence'] for row in no_minimum['engineering_repricing']['applied_requirements'] if row['method'] == 'hotdip']
        self.assertEqual(sum(Decimal(row['amount']) for row in charged), Decimal('792.85'))
        with self.assertRaisesRegex(ValueError, '全部'):
            amend_quote(source, patch(row_change('hotdip_rate', '3', 'L0003')))

    def test_material_estimate_updates_weight_without_changing_cad_or_approving_a_conflict(self):
        base, manifest = engineered()
        new = amend_quote(base, patch(row_change('material_profile', 'ss304', 'L0001')))['result']
        line = new['lines'][0]
        self.assertEqual(line['material_raw'], base['lines'][0]['material_raw'])
        self.assertEqual(line['material_requirement'], '316不锈钢')
        self.assertEqual(line['material_profile'], 'ss304')
        self.assertEqual(line['estimated_net_weight_kg'], '0.793')
        self.assertEqual(line['material_cost'], '242.56')
        material = next(item for item in new['engineering_repricing']['applied_requirements'] if item['method'] == 'adopt_material' and item['line_id'] == 'L0001')
        self.assertEqual(material['status'], 'pending_review')
        self.assertFalse(_priced_requirement(new, line, material))
        self.assertEqual(line['adopted_material_source'], [])
        self.assertTrue(any('L0001' in value for value in new['quote_amendment']['warnings']))
        restored = amend_quote(new, patch(row_change('material_profile', 'ss316', 'L0001')))['result']
        self.assertEqual(restored['totals'], base['totals'])
        requirement = next(item for item in manifest['quote_basis']['requirements_review'] if item['field'] == 'material_grade' and item['line_id'] == 'L0001')
        self.assertTrue(_priced_requirement(restored, restored['lines'][0], requirement))

    def test_missing_market_price_and_mesh_grade_cannot_be_filled_by_guessing(self):
        base, _ = engineered(market=True)
        with self.assertRaisesRegex(ValueError, '有效材料采用价'):
            amend_quote(base, patch(row_change('material_profile', 'al5052', 'L0004')))
        with self.assertRaisesRegex(ValueError, '采购规格'):
            amend_quote(base, patch(row_change('material_profile', 'ss316', 'L0002')))

    def test_invalid_fields_scopes_units_and_duplicate_changes_are_rejected(self):
        base = ordinary()
        cases = [
            {'changes': [], 'price': '1'}, {'changes': []},
            patch({'field': ['rate_percent'], 'value': '15'}),
            patch(global_change('total_price', '1')), patch(global_change('order_sets', '1.5')),
            patch({'field': 'order_sets', 'value': '20', 'line_ids': ['L0001']}),
            patch(global_change('rate_percent', '20%')), patch(global_change('rate_percent', '1e1')),
            patch({'field': 'order_sets', 'value': 20}),
            patch(row_change('purchase_rate', 'NaN', 'L0002')),
            patch(row_change('purchase_rate', '0', 'L0002')),
            patch(row_change('purchase_rate', '1', 'L0001')),
            patch(row_change('purchase_rate', '1', 'L9999')),
            patch(row_change('purchase_rate', '1', 'L0002', 'L0002')),
            patch(row_change('process_rate', '1', 'L0001', method='linear_m')),
            patch(row_change('process_rate', '1', 'L0001', method=['generic_processing'])),
            patch(row_change('process_rate', '1', 'L0002', method='generic_processing')),
            patch(row_change('hotdip_rate', '3', 'L0001')),
            patch(global_change('order_sets', '2'), global_change('order_sets', '3')),
            patch(row_change('purchase_rate', '1', 'L0002'), row_change('purchase_rate', '2', 'L0002')),
        ]
        saved = copy.deepcopy(base)
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                amend_quote(base, payload)
        self.assertEqual(base, saved)

    def test_unpriced_geometry_and_overlapping_processing_cannot_be_forced_into_prices(self):
        base, _ = engineered()
        for change in (row_change('process_rate', '5', 'L0005', method='generic_processing'),
                       row_change('process_rate', '5', 'L0001', method='generic_processing'),
                       row_change('process_rate', '5', 'L0004', method='pipe_weld_seal')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                amend_quote(base, patch(change))

    def test_tampered_internal_amounts_are_rejected_before_a_new_preview(self):
        source, _ = engineered()
        corrupt = copy.deepcopy(source)
        corrupt['engineering_repricing']['line_costs'][0]['process_cost'] = '0.00'
        with self.assertRaisesRegex(ValueError, '金额'):
            amend_quote(corrupt, patch(global_change('order_sets', '20')))
        first = amend_quote(source, patch(global_change('order_sets', '20')))['result']
        broken = copy.deepcopy(first)
        broken['quote_amendment']['pricing_basis']['L0001']['weld_rate'] = '999'
        with self.assertRaisesRegex(ValueError, '单位费用'):
            amend_quote(broken, patch(global_change('rate_percent', '15')))

    def test_all_costs_and_weights_reconcile_after_multiple_amendments(self):
        source, _ = engineered()
        new = amend_quote(source, patch(global_change('order_sets', '17'),
            row_change('purchase_rate', '1.01', 'L0002'), row_change('hotdip_rate', '3.1', 'L0003'),
            row_change('hotdip_minimum', '100', 'L0003'), global_change('rate_percent', '13')))['result']
        for key in AMOUNT_KEYS:
            self.assertEqual(Decimal(new['totals'][key]), sum((Decimal(row[key]) for row in new['lines'] if row['priced']), Decimal(0)))
        self.assertEqual(Decimal(new['weight_summary']['estimated_total_net_weight_kg']),
                         sum(Decimal(row['estimated_total_net_weight_kg']) for row in new['lines']))
        again = amend_quote(new, patch(global_change('rate_percent', '14')))['result']
        self.assertEqual({key: new['totals'][key] for key in AMOUNT_KEYS[:5]},
                         {key: again['totals'][key] for key in AMOUNT_KEYS[:5]})
        self.assertEqual(again['lines'][2]['process_cost'], '185.00')


if __name__ == '__main__':
    unittest.main()
