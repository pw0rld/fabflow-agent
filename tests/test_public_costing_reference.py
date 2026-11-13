"""Synthetic control for the public empty-reference adapter.

Run this with the project test suite on the remote build server only.
"""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_cost_sheet import build_cost_sheet, reprice_cost_sheet, render_cost_sheet_html
from customer_costing_reference import factory_reference


class PublicCostingReferenceTests(unittest.TestCase):
    def test_empty_reference_preserves_saved_price_renders_and_returns_detached_copies(self):
        # Independent analytic control: cost 9.00, cost-plus 20%, one piece.
        line = {
            'id': 'L0001', 'file_id': 'F0001', 'native_definition': 'SYNTHETIC',
            'native_solid_count': 1, 'document': 'synthetic.step', 'part_name': 'Synthetic panel',
            'mode': 'make', 'priced': True, 'quantity_per_set': 1, 'quantity_total': 1,
            'volume_mm3': '1000', 'material_raw': None, 'notes': [],
            'material_cost': '6.00', 'process_cost': '2.00', 'purchase_cost': '0.00',
            'setup_cost': '1.00', 'total_cost': '9.00', 'total_price': '10.80',
            'unit_cost': '9.00', 'unit_price': '10.80',
        }
        result = {
            'name': 'Synthetic control', 'batch_id': 'BATCH-' + '1' * 32,
            'run_id': 'RUN-' + '2' * 32, 'source_sha256': 'a' * 64,
            'source_files': [{'id': 'F0001', 'sha256': 'b' * 64}],
            'settings': {'order_sets': 1, 'pricing_method': 'cost_plus', 'rate_percent': '20'},
            'lines': [line],
            'totals': {'material_cost': '6.00', 'process_cost': '2.00', 'purchase_cost': '0.00',
                       'setup_cost': '1.00', 'total_cost': '9.00', 'total_price': '10.80',
                       'priced_lines': 1, 'unpriced_lines': 0},
        }
        original = copy.deepcopy(result)
        sheet = build_cost_sheet(result)
        reference = sheet['factory_reference']
        self.assertEqual(reference['source']['status'], 'unconfigured')
        self.assertEqual(reference['source']['sha256'], '')
        self.assertEqual(reference['source']['sheet_names'], [])
        self.assertEqual(reference['items'], [])
        self.assertEqual(reference['process_catalog'], [])
        self.assertFalse(reference['auto_applied'])
        self.assertTrue(all(value is None for value in reference['sample_accounting'].values()))
        self.assertEqual(reference['pricing_sample'], reference['current_pricing'])
        self.assertEqual(sheet['totals']['total_price'], '10.80')
        self.assertEqual(result, original)

        updated = reprice_cost_sheet(result, sheet['policy'])
        for key, value in original['totals'].items():
            self.assertEqual(updated['totals'][key], value)
        self.assertEqual(updated['settings'], original['settings'])
        self.assertEqual(updated['source_files'], original['source_files'])
        self.assertIn('工厂核价参考未配置', render_cost_sheet_html(result).decode())
        self.assertEqual(result, original)

        policy = reference['current_pricing']
        supplied = copy.deepcopy(policy)
        detached = factory_reference(supplied)
        detached['current_pricing']['rate_percent'] = '999'
        self.assertEqual(detached['pricing_sample'], policy)
        detached['pricing_sample']['rate_percent'] = '888'
        detached['items'].append({'id': 'synthetic-only'})
        detached['source']['sheet_names'].append('synthetic-only')
        detached['limitations'].clear()
        self.assertEqual(supplied, policy)
        fresh = factory_reference(supplied)
        self.assertEqual(fresh['items'], [])
        self.assertEqual(fresh['source']['sheet_names'], [])
        self.assertEqual(fresh['current_pricing'], policy)
        self.assertEqual(fresh['pricing_sample'], policy)
        self.assertTrue(fresh['limitations'])


if __name__ == '__main__':
    unittest.main()
