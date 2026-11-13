"""Dated reference adoption, explicit tax assumptions and immutable quote evidence."""
import copy
import csv
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_material_prices import VERSION, pricing_catalog, resolve_material_price
from batch_quote_costing import calculate_batch, render_artifact, validate_settings
from test_batch_quote_costing import document, settings, solid


def adopted_row(rate='3.18', *, profile='carbon_steel', tax=True, kind='market_reference', source='', day=None):
    references = {'carbon_steel': 'FS-Q235B-HRC-20261103', 'spcc': 'FS-SPCC-CRC-20261105',
                  'ss304': 'FS-304-2B-20261105', 'ss316': 'FS-316L-2B-20261103'}
    return {'rate': rate, 'tax_included': tax, 'source_kind': kind,
            'reference_id': references.get(profile) if kind == 'market_reference' else None,
            'source_text': source, 'quote_date': day}


def market_settings(rows=None, *, vat='13', **changes):
    result = settings(order_sets=1, rate_profile=VERSION)
    result['material_pricing'] = {'vat_rate_percent': vat, 'confirmed': True,
                                 'rows': {'carbon_steel': adopted_row()} if rows is None else rows}
    result.update(changes)
    return result


class BatchMaterialPriceTests(unittest.TestCase):
    def test_catalog_is_dated_copied_and_never_invents_aluminium_price_or_source_vat(self):
        first = pricing_catalog()
        self.assertEqual((first['version'], first['researched_on']), (VERSION, '2026-11-06'))
        refs = {item['id']: item for item in first['references']}
        self.assertEqual(refs['FS-Q235B-HRC-20261103']['price_cny_per_kg'], '3.18')
        self.assertEqual(refs['FS-SPCC-CRC-20261105']['price_cny_per_kg'], '3.61')
        self.assertEqual(refs['FS-304-2B-20261105']['price_cny_per_kg'], '14.50')
        self.assertEqual(refs['FS-316L-2B-20261103']['grade'], '316L')
        self.assertTrue(refs['FS-Q235B-HRC-20261103']['tax_included'])
        self.assertIsNone(refs['FS-304-2B-20261105']['tax_included'])
        self.assertTrue(all(ref['source_vat_rate_percent'] is None for ref in refs.values()))
        aluminium = next(row for row in first['profiles'] if row['id'] == 'al5052')
        self.assertIsNone(aluminium['reference_id'])
        self.assertNotIn('al5052', {ref['material_profile'] for ref in refs.values()})
        first['references'][0]['price_cny_per_kg'] = '999'
        self.assertEqual(pricing_catalog()['references'][0]['price_cny_per_kg'], '3.18')

    def test_operator_confirmation_and_tax_assumption_are_required_and_bounded(self):
        for vat in (None, 13, True, 'NaN', '-1', '101', '1e1', '', '9' * 41):
            with self.subTest(vat=vat), self.assertRaises(ValueError):
                validate_settings(market_settings(vat=vat))
        bad = market_settings()
        bad['material_pricing']['confirmed'] = False
        with self.assertRaises(ValueError):
            validate_settings(bad)
        bad = market_settings()
        bad['material_pricing'].pop('vat_rate_percent')
        with self.assertRaises(ValueError):
            validate_settings(bad)
        good = market_settings(vat='13.00')
        good['material_pricing']['rows']['carbon_steel']['rate'] = '3.1800'
        normalized = validate_settings(good)
        self.assertEqual(normalized['material_pricing']['vat_rate_percent'], '13')
        self.assertEqual(normalized['material_pricing']['rows']['carbon_steel']['rate'], '3.18')
        self.assertEqual(good['material_pricing']['vat_rate_percent'], '13.00')
        for vat in ('0', '100'):
            self.assertEqual(validate_settings(market_settings(vat=vat))['material_pricing']['vat_rate_percent'], vat)

    def test_reference_identity_original_price_and_known_tax_cannot_be_relabelled(self):
        bad_rows = [adopted_row('3.19'), adopted_row(tax=False), adopted_row(day='2026-11-05')]
        cross_profile = adopted_row()
        cross_profile['reference_id'] = 'FS-304-2B-20261105'
        no_reference = adopted_row()
        no_reference['reference_id'] = None
        bad_rows += [cross_profile, no_reference]
        for row in bad_rows:
            with self.subTest(row=row), self.assertRaises(ValueError):
                validate_settings(market_settings({'carbon_steel': row}))
        for value in (None, [], {'unknown': adopted_row()}):
            choice = market_settings()
            choice['material_pricing']['rows'] = value
            with self.subTest(rows=value), self.assertRaises(ValueError):
                validate_settings(choice)

    def test_manual_supplier_inputs_need_provenance_and_strict_valid_values(self):
        for kind in ('manual', 'supplier_quote'):
            for source in ('', '  '):
                with self.subTest(kind=kind, source=source), self.assertRaises(ValueError):
                    validate_settings(market_settings({'carbon_steel': adopted_row('6.5', kind=kind, source=source)}))
        for field, value in [('rate', '0'), ('rate', '-1'), ('rate', '1000000001'), ('rate', 6.5),
                             ('tax_included', None), ('tax_included', 1), ('source_kind', []),
                             ('quote_date', '2026-02-30'), ('source_text', 'x' * 2001), ('source_text', '\x00')]:
            row = adopted_row('6.5', kind='supplier_quote', tax=False, source='供应商询价单 A，未核验')
            row[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_settings(market_settings({'carbon_steel': row}))

    def test_vat_normalization_has_independent_cost_and_quote_answers(self):
        # 1e6 mm³ steel = 7.85 kg. Raw cost = 7.85/.85 * 3.18/1.13 = 25.99.
        # Processing 62.80 + setup 30.00 => 118.79; cost-plus20% => 142.55.
        result = calculate_batch([document([solid(1, grade='Q235B')])], market_settings())
        line = result['lines'][0]
        snapshot = line['material_price_snapshot']
        self.assertTrue(line['priced'])
        self.assertEqual(line['material_cost'], '25.99')
        self.assertEqual(line['total_cost'], '118.79')
        self.assertEqual(line['total_price'], '142.55')
        self.assertTrue(line['material_rate'].startswith('2.814159292035398230'))
        self.assertEqual(snapshot['original_rate'], '3.18')
        self.assertEqual(snapshot['quote_date'], '2026-11-03')
        self.assertEqual(snapshot['adopted_vat_rate_percent'], '13')
        self.assertIsNone(snapshot['reference_vat_rate_percent'])
        self.assertTrue(snapshot['vat_rate_is_assumption'])
        self.assertFalse(snapshot['supplier_price_verified'])
        self.assertTrue(result['simulated'])
        self.assertEqual(result['status'], 'pending_review')

    def test_unknown_source_tax_stays_unknown_when_operator_adopts_included_price(self):
        choices = validate_settings(market_settings({'ss304': adopted_row('14.50', profile='ss304')}, material_profile='ss304'))
        snapshot = resolve_material_price('ss304', 'SS304', choices)
        self.assertIsNone(snapshot['reference_tax_included'])
        self.assertIsNone(snapshot['reference']['tax_included'])
        self.assertIsNone(snapshot['reference']['source_vat_rate_percent'])
        self.assertTrue(snapshot['adopted_tax_included'])
        self.assertTrue(snapshot['tax_basis_is_assumption'])
        self.assertTrue(snapshot['normalized_net_rate'].startswith('12.831858407079646'))
        self.assertTrue(any('税口径未说明' in note for note in snapshot['notes']))

    def test_family_reference_does_not_claim_exact_grade_or_specification_match(self):
        cases = [('Q355B', 'carbon_steel', '3.18', 'Q235B'), ('SUS316', 'ss316', '29', '316L'),
                 ('DC01', 'spcc', '3.61', 'SPCC')]
        for raw, profile, rate, reference_grade in cases:
            rows = {profile: adopted_row(rate, profile=profile)}
            result = calculate_batch([document([solid(1, grade=raw)])], market_settings(rows))
            line = result['lines'][0]
            snapshot = line['material_price_snapshot']
            with self.subTest(raw=raw):
                self.assertEqual(line['material_raw'], raw)
                self.assertEqual(line['material_profile'], profile)
                self.assertEqual(snapshot['reference']['grade'], reference_grade)
                self.assertEqual(snapshot['match_state'], 'assumed_reference_spec')
                self.assertIn('未自动核对', '；'.join(snapshot['notes']))

    def test_supplier_and_manual_net_price_override_reference_without_changing_geometry(self):
        docs = [document([solid(1)])]
        before = copy.deepcopy(docs)
        baseline = calculate_batch(docs, market_settings())
        for kind in ('supplier_quote', 'manual'):
            row = adopted_row('6.5', kind=kind, tax=False, source='供应商 A 口头报价，规格待核对', day='2026-11-06')
            result = calculate_batch(docs, market_settings({'carbon_steel': row}))
            line = result['lines'][0]
            self.assertEqual(line['material_rate'], '6.5')
            self.assertEqual(line['material_cost'], '60.03')
            self.assertEqual(line['total_price'], '183.40')
            self.assertEqual(line['estimated_net_weight_kg'], baseline['lines'][0]['estimated_net_weight_kg'])
            self.assertEqual(line['material_price_snapshot']['source_kind'], kind)
            self.assertFalse(line['material_price_snapshot']['supplier_price_verified'])
            self.assertIsNone(line['material_price_snapshot']['source_url'])
        self.assertEqual(docs, before)
        self.assertEqual(baseline['lines'][0]['material_cost'], '25.99')
        self.assertFalse(baseline['lines'][0]['material_price_snapshot']['tax_basis_is_assumption'])
        row['reference_id'] = 'FS-Q235B-HRC-20261103'
        referenced = calculate_batch(docs, market_settings({'carbon_steel': row}))
        self.assertTrue(referenced['lines'][0]['material_price_snapshot']['tax_basis_is_assumption'])
        self.assertEqual(referenced['lines'][0]['material_rate'], '6.5')

    def test_missing_row_or_price_never_falls_back_to_market_or_demo_prices(self):
        for rows in ({}, {'al5052': adopted_row(None, profile='al5052', kind='manual')}):
            result = calculate_batch([document([solid(1, grade='5052')])], market_settings(rows))
            line = result['lines'][0]
            self.assertFalse(line['priced'])
            self.assertIsNone(line['material_rate'])
            self.assertIsNone(line['material_cost'])
            self.assertIsNone(line['total_cost'])
            self.assertIsNone(result['totals']['total_price'])
            self.assertEqual(line['estimated_net_weight_kg'], '2.68')
            self.assertEqual(result['weight_summary']['estimated_total_net_weight_kg'], '2.68')
            self.assertEqual(line['material_price_snapshot']['match_state'], 'missing')
        result = calculate_batch([document([solid(1)])], market_settings({}))
        self.assertFalse(result['lines'][0]['priced'])  # Even a catalog reference is never silently adopted.

    def test_missing_material_line_and_bought_parts_keep_different_unknown_zero_states(self):
        result = calculate_batch([document([solid(1, grade='5052'),
            solid(2, definition='screw', name='M4x20', volume=1000)])], market_settings())
        self.assertEqual(result['totals']['unpriced_lines'], 1)
        self.assertEqual(result['totals']['material_cost'], '0.00')  # Priced bought part has no raw material cost.
        self.assertEqual(result['totals']['purchase_cost'], '0.80')
        self.assertIsNone(result['lines'][0]['material_cost'])
        self.assertNotIn('material_price_snapshot', result['lines'][1])
        page = render_artifact(result, 'quote.html')[0].decode()
        self.assertIn('1 行未计价项目未包含在图中', page)
        self.assertIn('未计价，不代表免费', page)

    def test_legacy_settings_money_and_csv_columns_are_preserved(self):
        doc = document([solid(1, 'plate/solid-1'), solid(2, 'plate/solid-1')]
            + [solid(i, 'screw/solid-1', name='M4x20', volume=1000) for i in range(3, 7)])
        result = calculate_batch([doc], settings())
        self.assertEqual(result['totals']['total_price'], '898.62')
        self.assertEqual(result['lines'][0]['material_rate'], '6')
        self.assertNotIn('material_price_sources', result)
        self.assertNotIn('material_price_snapshot', result['lines'][0])
        choices = settings()
        choices['material_pricing'] = market_settings()['material_pricing']
        with self.assertRaises(ValueError):
            validate_settings(choices)
        page = render_artifact(result, 'quote.html')[0].decode()
        self.assertNotIn('本次材料价格来源与采用口径', page)
        self.assertIn('采用显式演示费率', page)
        self.assertIn('898.62', page)
        for name in ('cost.csv', 'bom.csv'):
            header = render_artifact(result, name)[0].decode('utf-8-sig').splitlines()[0]
            self.assertNotIn('材料行情参考编号', header)

    def test_rendering_uses_saved_quote_sources_without_recalculating_or_mutating_history(self):
        result = calculate_batch([document([solid(1)])], market_settings())
        result['name'] = '旧报价快照'
        original = copy.deepcopy(result)
        old_json = render_artifact(result, 'quote.json')[0]
        with (patch('batch_quote_costing.resolve_material_price', side_effect=AssertionError('must not reprice')),
              patch('batch_material_prices.REFERENCES', [])):
            page = render_artifact(result, 'quote.html')[0].decode()
            self.assertIn('FS-Q235B-HRC-20261103', json.dumps(result))
            self.assertIn('2026-11-03', page)
            self.assertIn('3.18', page)
            self.assertIn('本次材料价格来源与采用口径', page)
            self.assertIn('2.814159', page)
            self.assertIn('已计价部分的成本构成', page)
            self.assertEqual(render_artifact(result, 'quote.json')[0], old_json)
        self.assertEqual(result, original)

    def test_csv_appends_traceable_price_fields_and_escapes_source_text(self):
        row = adopted_row('6.5', kind='supplier_quote', tax=False, source='=supplier A', day='2026-11-06')
        result = calculate_batch([document([solid(1)])], market_settings({'carbon_steel': row}))
        for name in ('bom.csv', 'cost.csv'):
            content = render_artifact(result, name)[0].decode('utf-8-sig')
            output = list(csv.DictReader(io.StringIO(content)))
            self.assertEqual(output[0]['材料未税采用价_CNY每kg'], '6.5')
            self.assertEqual(output[0]['材料价格来源类型'], 'supplier_quote')
            self.assertEqual(output[0]['材料价格来源说明'], "'=supplier A")
            self.assertEqual(output[0]['材料价格来源日期'], '2026-11-06')
            self.assertEqual(output[0]['材料行情参考编号'], '')


if __name__ == '__main__':
    unittest.main()
