"""Display grouping conserves saved quote rows and does not infer CAD identity."""
import copy
from decimal import Decimal
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_costing import aggregate_display_groups, calculate_batch, render_artifact
from batch_quote_service import BatchQuoteService
from test_batch_quote_costing import document, settings, solid


def sample():
    docs = []
    for index, count in enumerate((18, 8, 4, 4), 1):
        docs.append(document([solid(i, 'fastener/solid-1', name='CLS-M6-2', volume='299.79960807')
                              for i in range(1, count + 1)], file_id='F' + str(index), name='assembly-' + str(index) + '.stp'))
    return calculate_batch(docs, settings(order_sets=10))


class Tags(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.groups, self.sources, self.details, self.links = [], [], [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'tr' and 'data-quote-group' in values:
            self.groups.append(values)
        if tag == 'tr' and 'data-source-line' in values:
            self.sources.append(values)
        if tag == 'details' and values.get('class') == 'group-sources':
            self.details.append(values)
        if tag == 'a' and values.get('class') == 'part-preview-link':
            self.links.append(values['href'])


class BatchQuoteAggregationTests(unittest.TestCase):
    def test_same_spec_bought_parts_sum_quantities_money_weights_and_preserve_data(self):
        result = sample()
        original = copy.deepcopy(result)
        groups = aggregate_display_groups(result)
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group['member_ids'], ['L0001', 'L0002', 'L0003', 'L0004'])
        self.assertEqual(group['member_count'], 4)
        self.assertTrue(group['aggregated'])
        self.assertEqual(group['quantity_per_set'], 34)
        self.assertEqual(group['quantity_total'], 340)
        self.assertEqual(group['unit_price'], '0.96')
        self.assertEqual(group['total_price'], '326.40')
        self.assertEqual(group['total_cost'], '272.00')
        self.assertEqual(len(group['source_documents']), 4)
        for key in ('estimated_set_net_weight_kg', 'estimated_total_net_weight_kg',
                    'material_cost', 'process_cost', 'purchase_cost', 'setup_cost', 'total_cost', 'total_price'):
            self.assertEqual(Decimal(group[key]), sum(Decimal(line[key]) for line in original['lines']))
        self.assertEqual(result, original)

    def test_full_other_model_number_is_allowed_but_partial_or_generic_names_are_separate(self):
        for name in ('EM-B-M6-15-SL_2', 'M4x20', 'DIN-912-M6-20'):
            result = sample()
            for line in result['lines']:
                line['part_name'] = name
            with self.subTest(name=name):
                self.assertEqual(len(aggregate_display_groups(result)), 1)
        for name in ('SOLID', 'SOLID-1', 'Body-1', 'Part-2', 'M6-', 'M6', 'Bolt', '未命名零件'):
            result = sample()
            for line in result['lines']:
                line['part_name'] = name
            with self.subTest(name=name):
                self.assertEqual(len(aggregate_display_groups(result)), 4)

    def test_price_material_geometry_and_evidence_differences_split_groups(self):
        changes = ({'unit_price': '1.00'}, {'unit_cost': '0.81'}, {'material_profile': 'ss304'},
                   {'material_raw': 'SS304'}, {'density_kg_m3': '7930'}, {'volume_mm3': '300'},
                   {'estimated_net_weight_kg': '0.003'}, {'material_price_source_kind': 'manual'},
                   {'material_price_snapshot': {'source_kind': 'supplier_quote', 'original_rate': '4'}},
                   {'material_price_source': 'a different supplier'}, {'notes': ['different specification']})
        for change in changes:
            result = sample()
            result['lines'][-1].update(change)
            with self.subTest(change=change):
                groups = aggregate_display_groups(result)
                self.assertEqual(len(groups), 2)
                self.assertEqual(groups[0]['member_count'], 3)
                self.assertFalse(groups[1]['aggregated'])

    def test_missing_invalid_low_confidence_and_unpriced_values_are_not_merged(self):
        changes = ({'priced': False}, {'confidence': 'low'}, {'mode': 'make'},
                   {'quantity_total': None}, {'quantity_per_set': 0}, {'quantity_total': True},
                   {'unit_price': None}, {'estimated_total_net_weight_kg': None},
                   {'setup_cost': None}, {'total_cost': None}, {'volume_mm3': None},
                   {'material_profile': None}, {'document': ''}, {'file_id': None})
        for change in changes:
            result = sample()
            for line in result['lines']:
                line.update(change)
            with self.subTest(change=change):
                groups = aggregate_display_groups(result)
                self.assertEqual(len(groups), 4)
                self.assertTrue(all(group['member_count'] == 1 for group in groups))

    def test_same_name_and_volume_do_not_merge_made_parts(self):
        result = calculate_batch([document([solid(1, name='DEMO-PN-31-01')], file_id='F1'),
                                  document([solid(1, name='DEMO-PN-31-01')], file_id='F2')], settings())
        groups = aggregate_display_groups(result)
        self.assertEqual(len(groups), 2)
        self.assertEqual(sum(Decimal(group['setup_cost']) for group in groups), Decimal('60'))

    def test_original_setup_fees_are_summed_without_repricing(self):
        result = sample()
        for index, line in enumerate(result['lines'], 1):
            line['setup_cost'] = str(index)
            line['total_cost'] = str(Decimal(line['total_cost']) + index)
        groups = aggregate_display_groups(result)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['setup_cost'], '10.00')
        self.assertEqual(groups[0]['total_cost'], '282.00')
        self.assertEqual(groups[0]['total_price'], '326.40')

    def test_tiny_saved_weights_sum_exactly_without_display_requantization(self):
        result = sample()
        net = Decimal('0.0000000049')
        for line in result['lines']:
            line['estimated_net_weight_kg'] = format(net, 'f')
            line['estimated_set_net_weight_kg'] = format(net * line['quantity_per_set'], 'f')
            line['estimated_total_net_weight_kg'] = format(net * line['quantity_total'], 'f')
        groups = aggregate_display_groups(result)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['estimated_set_net_weight_kg'], '0.0000001666')
        self.assertEqual(groups[0]['estimated_total_net_weight_kg'], '0.000001666')

    def test_public_response_adds_derived_groups_without_persisting_or_changing_exports(self):
        result = sample()
        state = {'id': 'BATCH-' + 'a' * 32, 'result': result, '_private': 'hidden'}
        original = copy.deepcopy(state)
        exports = {name: render_artifact(result, name)[0] for name in ('quote.json', 'bom.csv', 'cost.csv')}
        public = BatchQuoteService._public(state)
        self.assertNotIn('_private', public)
        self.assertEqual(len(public['result']['display_groups']), 1)
        self.assertEqual(public['result']['lines'], result['lines'])
        self.assertEqual(state, original)
        for name, content in exports.items():
            self.assertEqual(render_artifact(result, name)[0], content)
        self.assertNotIn('display_groups', json.loads(exports['quote.json']))

    def test_html_has_one_summary_four_closed_sources_and_safe_original_preview_links(self):
        result = sample()
        previews = []
        for line in result['lines']:
            url = '/ortho2cad-test/customer-cases/public-demo-case/parts/' + line['id'] + '.json'
            previews.append({'line_id': line['id'], 'status': 'ready', 'geometry_url': url,
                             'thumbnail_url': url[:-5] + '/thumbnail.png'})
        text = render_artifact(result, 'quote.html', part_previews=previews)[0].decode()
        tags = Tags(text)
        self.assertEqual(len(tags.groups), 1)
        self.assertEqual(tags.groups[0]['data-member-count'], '4')
        self.assertEqual([row['data-source-line'] for row in tags.sources], ['L0001', 'L0002', 'L0003', 'L0004'])
        self.assertEqual(len(tags.details), 1)
        self.assertNotIn('open', tags.details[0])
        self.assertEqual(len(tags.links), 5)  # representative plus each original source
        self.assertIn('340', text)
        self.assertIn('326.40', text)
        self.assertIn('1 个汇总条目 · 4 条原始明细', text)
        result['lines'][0]['document'] = '<script>alert(1)</script>.stp'
        previews[1]['geometry_url'] = 'javascript:alert(1)'
        text = render_artifact(result, 'quote.html', part_previews=previews)[0].decode()
        self.assertNotIn('<script>', text)
        self.assertNotIn('javascript:', text)
        self.assertIn('&lt;script&gt;', text)

    def test_duplicate_ids_cannot_be_grouped_into_an_ambiguous_source_mapping(self):
        result = sample()
        for line in result['lines']:
            line['id'] = 'L0001'
        self.assertEqual(len(aggregate_display_groups(result)), 4)


if __name__ == '__main__':
    unittest.main()
