"""Display separates CAD evidence, estimating material and rate provenance."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_costing import calculate_batch, material_display, render_artifact
from test_batch_material_prices import market_settings
from test_batch_quote_costing import document, solid


class BatchMaterialDisplayTests(unittest.TestCase):
    def line(self):
        return calculate_batch([document([solid(1)])], market_settings())['lines'][0]

    def test_missing_cad_grade_has_explicit_adopted_material_and_a_concrete_gap(self):
        display = material_display(self.line())
        self.assertEqual(display['cad_text'], '')
        self.assertEqual(display['adopted_label'], '估价采用')
        self.assertEqual(display['adopted_text'], '碳钢（Q235类）')
        self.assertEqual(display['price_text'], '行情参考价')
        self.assertIn('材料牌号需核对', display['note'])
        self.assertNotIn('文件没有', display['note'])

    def test_generic_known_or_conflicting_original_text_is_retained_verbatim(self):
        for raw, profile in [('Steel', 'carbon_steel'), ('SS304', 'ss304'), ('Q355B', 'carbon_steel'), ('SS304 / SS316', 'ss304')]:
            line = {**self.line(), 'material_raw': raw, 'material_profile': profile}
            self.assertEqual(material_display(line)['cad_text'], raw)
            self.assertNotIn('未读到', material_display(line)['cad_text'])
        result = calculate_batch([document([solid(1, grade='SS304')])], market_settings())
        page = render_artifact(result, 'quote.html')[0].decode()
        self.assertIn('<span>CAD材料</span><strong>SS304</strong>', page)

    def test_rate_provenance_and_missing_rate_do_not_fabricate_a_procurement_price(self):
        for kind, label in [('market_reference', '行情参考价'), ('supplier_quote', '供应商录入价'), ('manual', '手工录入价')]:
            line = {**self.line(), 'material_price_source_kind': kind}
            self.assertEqual(material_display(line)['price_text'], label)
            line['material_rate'] = None
            self.assertEqual(material_display(line)['price_text'], '暂未录价')
        self.assertEqual(material_display({'material_rate': '6'})['price_text'], '演示材料价')
        self.assertEqual(material_display({'material_rate': '6', 'material_price_source_kind': 'unknown'})['price_text'], '价格来源待核对')

    def test_bought_part_material_is_a_weight_basis_separate_from_purchase_price(self):
        display = material_display({**self.line(), 'mode': 'buy', 'material_rate': None})
        self.assertEqual(display['adopted_label'], '估重采用')
        self.assertEqual(display['price_text'], '外购件演示价')

    def test_structured_legacy_snapshot_is_compatible_but_explicit_null_stays_missing(self):
        line = {'material_price_snapshot': {'material_profile': 'ss304', 'normalized_net_rate': '12', 'source_kind': 'market_reference'}}
        self.assertEqual(material_display(line)['adopted_text'], '304不锈钢')
        self.assertEqual(material_display(line)['price_text'], '行情参考价')
        line['material_rate'] = None
        self.assertEqual(material_display(line)['price_text'], '暂未录价')
        self.assertEqual(material_display({})['adopted_text'], '暂未采用')

    def test_display_preserves_historical_json_csv_amounts_and_input_data(self):
        result = calculate_batch([document([solid(1)])], market_settings())
        original = copy.deepcopy(result)
        before = {name: render_artifact(result, name)[0] for name in ('quote.json', 'bom.csv', 'cost.csv')}
        page = render_artifact(result, 'quote.html')[0].decode()
        for label in ('材料与计价依据', '估价采用', '行情参考价'):
            self.assertIn(label, page)
        self.assertNotIn('<span>CAD材料</span>', page)
        self.assertNotIn('未读到明确牌号', page)
        self.assertNotIn('<th>材料与假设</th>', page)
        self.assertEqual(result, original)
        for name, content in before.items():
            self.assertEqual(render_artifact(result, name)[0], content)


if __name__ == '__main__':
    unittest.main()
