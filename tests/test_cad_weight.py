"""Weight meaning, unknown values and quotation compatibility checks."""
import copy
import csv
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cad_weight import weight_summary
from cad_quotation import CadQuotationService, normalize_inputs
from workbench_store import WorkbenchError
import test_cad_quotation as quotation_cases


def line(group_id='P1', *, weight='2', usage='2.3', quantity=3, mode='make', selected=True):
    return {'group_id': group_id, 'selected': selected, 'mode': mode,
            'quantity_per_set': quantity, 'net_weight_kg': weight, 'material_usage': usage}


class WeightSummaryTests(unittest.TestCase):
    def test_product_weight_and_material_usage_are_independent(self):
        result = weight_summary({'order_sets': 10, 'lines': [line()]})
        self.assertEqual(result['lines']['P1'], {
            'net_weight_kg': '2', 'set_net_weight_kg': '6', 'total_net_weight_kg': '60',
            'total_material_usage_kg': '69', 'source': 'entered'})
        self.assertEqual(result['totals']['total_net_weight_kg'], '60')
        self.assertEqual(result['totals']['total_material_usage_kg'], '69')
        self.assertEqual(result['totals']['missing_net_weight_lines'], 0)

    def test_missing_net_mass_does_not_become_procurement_usage_or_zero(self):
        result = weight_summary({'order_sets': 10, 'lines': [line(), line('P2', weight=None)]})
        self.assertIsNone(result['totals']['total_net_weight_kg'])
        self.assertEqual(result['totals']['known_net_weight_kg'], '60')
        self.assertEqual(result['totals']['missing_net_weight_lines'], 1)
        self.assertIsNone(result['lines']['P2']['net_weight_kg'])
        self.assertEqual(result['lines']['P2']['source'], 'unknown')
        self.assertEqual(result['totals']['total_material_usage_kg'], '138')

    def test_quantity_or_order_sets_missing_preserves_unit_weight_only(self):
        for quantity, sets in [(None, 10), (3, None)]:
            result = weight_summary({'order_sets': sets, 'lines': [line(quantity=quantity)]})
            self.assertEqual(result['lines']['P1']['net_weight_kg'], '2')
            self.assertIsNone(result['lines']['P1']['total_net_weight_kg'])
            self.assertIsNone(result['totals']['total_net_weight_kg'])
            self.assertIsNone(result['totals']['known_net_weight_kg'])
            self.assertEqual(result['totals']['missing_net_weight_lines'], 1)

    def test_buy_mass_counts_as_product_mass_but_not_raw_material_usage(self):
        result = weight_summary({'order_sets': 10, 'lines': [line(mode='buy', quantity=1, usage='999')]})
        self.assertEqual(result['totals']['total_net_weight_kg'], '20')
        self.assertIsNone(result['totals']['total_material_usage_kg'])
        self.assertIsNone(result['totals']['known_material_usage_kg'])
        self.assertEqual(result['totals']['missing_material_usage_lines'], 0)
        self.assertEqual(result['totals']['material_usage_lines'], 0)

    def test_excluded_rows_do_not_change_totals_or_missing_counts(self):
        result = weight_summary({'order_sets': 10, 'lines': [line(), line('P2', selected=False, weight=None)]})
        self.assertEqual(result['totals']['selected_lines'], 1)
        self.assertEqual(result['totals']['total_net_weight_kg'], '60')
        self.assertNotIn('P2', result['lines'])

    def test_empty_summary_is_not_a_confirmed_zero_weight(self):
        result = weight_summary({'order_sets': 10, 'lines': []})
        self.assertIsNone(result['totals']['total_net_weight_kg'])
        self.assertIsNone(result['totals']['total_material_usage_kg'])

    def test_decimal_arithmetic_does_not_round_small_positive_mass_to_zero(self):
        result = weight_summary({'order_sets': 7, 'lines': [line(weight='0.000000001', quantity=3)]})
        self.assertEqual(result['lines']['P1']['net_weight_kg'], '0.000000001')
        self.assertEqual(result['totals']['total_net_weight_kg'], '0.000000021')
        result = weight_summary({'order_sets': 3, 'lines': [line(weight='0.1', quantity=3)]})
        self.assertEqual(result['totals']['total_net_weight_kg'], '0.9')

    def test_invalid_mass_is_rejected_instead_of_inferred(self):
        for value in ['0', '-1', 'NaN', 'Infinity', '1e3', '1000000000000', True, 2, [], {}, '0.' + '1' * 40]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                weight_summary({'order_sets': 10, 'lines': [line(weight=value)]})

    def test_summary_does_not_mutate_input(self):
        data = {'order_sets': 10, 'lines': [line()]}
        original = copy.deepcopy(data)
        weight_summary(data)
        self.assertEqual(data, original)


class WeightQuotationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = quotation_cases.CadQuotationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_legacy_payload_is_accepted_and_mass_remains_optional(self):
        state = self.fixture.prepared()
        old_input = copy.deepcopy(state['inputs'])
        for row in old_input['lines']:
            row.pop('net_weight_kg', None)
        normalized = normalize_inputs(old_input, state['groups'])
        self.assertTrue(all(row['net_weight_kg'] is None for row in normalized['lines']))
        self.assertTrue(all('net_weight_kg' not in row for row in old_input['lines']))
        self.assertTrue(state['validation']['ready'])
        result = self.fixture.service.estimate(self.fixture.did, self.fixture.guards(state))
        self.assertIsNone(result['weight_summary']['totals']['total_net_weight_kg'])
        self.assertEqual(result['totals']['total_price'], '91.20')

    def test_weight_updates_do_not_change_costs_or_cad(self):
        state = self.fixture.prepared()
        before = self.fixture.service.estimate(self.fixture.did, self.fixture.guards(state))
        cad_before = self.fixture.manufacturing.get(self.fixture.did)
        state['inputs']['lines'][0]['net_weight_kg'] = '2'
        state['inputs']['lines'][1]['net_weight_kg'] = '0.1'
        saved = self.fixture.service.save_inputs(self.fixture.did, {**self.fixture.guards(state), 'inputs': state['inputs']})
        after = self.fixture.service.estimate(self.fixture.did, self.fixture.guards(saved))
        self.assertEqual(after['totals'], before['totals'])
        self.assertEqual(after['weight_summary']['totals']['total_net_weight_kg'], '6.6')
        self.assertEqual(after['weight_summary']['totals']['total_material_usage_kg'], '1.5')
        self.assertEqual(after['lines'][0]['set_net_weight_kg'], '2')
        self.assertEqual(after['lines'][1]['total_net_weight_kg'], '0.6')
        self.assertEqual(saved['weight_summary'], after['weight_summary'])
        self.assertEqual(self.fixture.manufacturing.get(self.fixture.did), cad_before)

    def test_invalid_mass_fails_before_saving(self):
        state = self.fixture.prepared()
        for bad in ['0', '-1', 'NaN', 'Infinity', '1e3', True, [], {}, '1000000000000']:
            inputs = copy.deepcopy(state['inputs'])
            inputs['lines'][0]['net_weight_kg'] = bad
            with self.subTest(value=bad), self.assertRaises(WorkbenchError):
                self.fixture.service.save_inputs(self.fixture.did, {**self.fixture.guards(state), 'inputs': inputs})
        self.assertEqual(self.fixture.service.get(self.fixture.did)['revision'], state['revision'])

    def test_new_quote_exports_weights_and_old_quote_stays_unchanged(self):
        state = self.fixture.prepared()
        old = self.fixture.service.issue(self.fixture.did, {**self.fixture.guards(state), 'request_id': 'without-net-weight'})['quote']
        original_snapshot = copy.deepcopy(old['input_snapshot'])
        state['inputs']['lines'][0]['net_weight_kg'] = '2'
        state['inputs']['lines'][1]['net_weight_kg'] = '0.1'
        saved = self.fixture.service.save_inputs(self.fixture.did, {**self.fixture.guards(state), 'inputs': state['inputs']})
        new = self.fixture.service.issue(self.fixture.did, {**self.fixture.guards(saved), 'request_id': 'with-net-weight'})['quote']
        rows = list(csv.DictReader(io.StringIO(CadQuotationService._csv(new, 'bom.csv'))))
        self.assertEqual(rows[0]['单件净重kg'], '2')
        self.assertEqual(rows[0]['整单净重kg'], '6')
        self.assertEqual(rows[0]['原料采购计价总重量kg'], '1.5')
        self.assertEqual(rows[1]['原料采购计价总重量kg'], '')
        self.assertIn('6.6 kg', CadQuotationService._html(new))
        self.assertIn('整单净重', CadQuotationService._html(new))
        historic = self.fixture.service.get_quote(self.fixture.did, old['id'])
        self.assertEqual(historic['input_snapshot'], original_snapshot)
        self.assertIsNone(historic['weight_summary']['totals']['total_net_weight_kg'])
        self.assertEqual(new['totals'], historic['totals'])

    def test_pre_feature_quote_renders_without_recomputing_current_mass(self):
        state = self.fixture.prepared()
        quote = self.fixture.service.issue(self.fixture.did, {**self.fixture.guards(state), 'request_id': 'legacy-shape'})['quote']
        quote.pop('weight_summary')
        for row in quote['lines']:
            for key in ['net_weight_kg', 'set_net_weight_kg', 'total_net_weight_kg', 'total_material_usage_kg', 'source']:
                row.pop(key, None)
        before = copy.deepcopy(quote)
        self.assertNotIn('重量与采购计价用量', CadQuotationService._html(quote))
        rows = list(csv.reader(io.StringIO(CadQuotationService._csv(quote, 'bom.csv'))))
        self.assertEqual(len(rows[0]), 10)
        self.assertEqual(quote, before)


if __name__ == '__main__':
    unittest.main()
