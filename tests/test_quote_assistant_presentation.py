"""Chat changes must be visible in quotes without altering original evidence."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_amendment import amend_quote
from batch_quote_costing import aggregate_display_groups, material_display, render_artifact
from batch_engineering_bom import build_engineering_bom
from test_engineering_pricing_presentation import fixture


class QuoteAssistantPresentationTests(unittest.TestCase):
    def test_quote_and_print_show_immediate_parent_and_confirmed_new_conditions(self):
        _, base, recognition = fixture()
        saved = copy.deepcopy(base)
        originals = {name: render_artifact(base, name)[0] for name in ('quote.json', 'bom.csv', 'cost.csv', 'quote-print.html')}
        result = amend_quote(base, {'changes': [{'field': 'order_sets', 'value': '20'},
            {'field': 'rate_percent', 'value': '15'}]})['result']
        for name in ('quote.html', 'quote-print.html'):
            page = render_artifact(result, name, engineering_recognition=recognition)[0].decode()
            self.assertIn('data-quote-amendment', page)
            self.assertIn('报价修改记录', page)
            self.assertIn('已采用的工程要求与费用', page)
            self.assertIn('订单套数', page)
            self.assertIn('15.00', page)
            self.assertNotIn('data-engineering-pricing-notice', page)
        self.assertEqual(base, saved)
        self.assertEqual({name: render_artifact(base, name)[0] for name in originals}, originals)

    def test_manual_mesh_and_weld_prices_are_not_relabelled_as_demo_or_supplier_quotes(self):
        _, base, recognition = fixture()
        result = amend_quote(base, {'changes': [
            {'field': 'purchase_rate', 'value': '0.90', 'line_ids': ['L0002']},
            {'field': 'process_rate', 'value': '4.00', 'line_ids': ['L0002'], 'method': 'mesh_weld'}]})['result']
        mesh = result['lines'][1]
        self.assertEqual(material_display(mesh)['price_text'], '本单指定采购价')
        self.assertNotIn('按件演示计价', material_display(mesh)['note'])
        manifest = build_engineering_bom(result, aggregate_display_groups(result), recognition=recognition)
        requirements = [item for item in manifest['quote_basis']['requirements_review'] if item['line_id'] == 'L0002']
        self.assertTrue(all(item['status'] == 'priced' for item in requirements))
        self.assertTrue(all('用户指定' in item['reason'] and '行情参考价' not in item['reason'] for item in requirements))
        page = render_artifact(result, 'quote-print.html')[0].decode()
        self.assertIn('网片焊接（本单指定采用价）', page)
        self.assertIn('用户指定的本单采用价不代表供应商实报', page)
        self.assertIsNone(mesh.get('material_raw'))


if __name__ == '__main__':
    unittest.main()
