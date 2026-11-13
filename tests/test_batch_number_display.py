"""Business display precision never changes saved quotation values."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_costing import calculate_batch, render_artifact, _cost_chart_html, _display_number
from test_batch_quote_costing import document, settings, solid


def example():
    return calculate_batch([document([solid(1), solid(2)])], settings())


class BatchNumberDisplayTests(unittest.TestCase):
    def test_screen_and_print_format_business_quantities_but_keep_saved_data(self):
        result = example()
        result.update(name='Order REV04', run_id='RUN-20261109', created_at='2026-11-09T10:00:00Z')
        saved = copy.deepcopy(result)
        raw = {name: render_artifact(result, name)[0] for name in ('bom.csv', 'cost.csv', 'quote.json')}
        for filename in ('quote.html', 'quote-print.html'):
            with self.subTest(filename=filename):
                page = render_artifact(result, filename)[0].decode()
                self.assertIn('3 套', page)
                self.assertIn('>6</td>', page)
                self.assertIn('约 7.85 kg', page)
                self.assertIn('约 47.10 kg', page)
                self.assertIn('Order REV04', page)
                self.assertIn('L0001', page)
        self.assertEqual(result, saved)
        for name, before in raw.items():
            self.assertEqual(render_artifact(result, name)[0], before)

    def test_material_rates_tax_and_thickness_have_two_decimals_without_rewriting_source(self):
        result = example()
        result['material_price_sources'] = [{
            'material_name': 'SS304', 'source_kind': 'manual', 'source_text': 'supplier REV04',
            'quote_date': '2026-11-09', 'original_rate': '6.5',
            'normalized_net_rate': '5.7522123893805309734513274336',
            'adopted_vat_rate_percent': '13', 'adopted_tax_included': True,
            'reference': {'grade': '304', 'thickness_mm': '1.5'},
        }]
        saved = copy.deepcopy(result)
        for filename in ('quote.html', 'quote-print.html'):
            page = render_artifact(result, filename)[0].decode()
            for expected in ('SS304', '304 / 1.50 mm', 'supplier REV04', '2026-11-09',
                             '>6.50<', '>约 5.75<', '采用税率假设 13.00%'):
                self.assertIn(expected, page)
            self.assertIn('title="5.7522123893805309734513274336"', page)
        self.assertEqual(result, saved)

    def test_chart_shows_two_decimal_percentages_and_preserves_tiny_positive_scope(self):
        result = {'totals': {'material_cost': '1', 'process_cost': '2', 'purchase_cost': '0',
                             'setup_cost': '0', 'total_cost': '3', 'unpriced_lines': 0}}
        saved = copy.deepcopy(result)
        page = _cost_chart_html(result)
        for expected in ('33.33%', '66.67%', '0.00%'):
            self.assertIn(expected, page)
        self.assertEqual(result, saved)
        self.assertEqual(_display_number('2.675'), '2.68')
        self.assertEqual(_display_number('0.0001'), '&lt;0.01')
        self.assertEqual(_display_number('0'), '0.00')
        self.assertEqual(_display_number(None, '待核对'), '待核对')


if __name__ == '__main__':
    unittest.main()
