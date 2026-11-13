"""Independent cost-sheet arithmetic. Execute only on ZeroClave."""
import copy
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_cost_sheet import build_cost_sheet, reprice_cost_sheet, render_cost_sheet_html
from prep_record import digest


def fixture():
    made = dict(id='L0001', file_id='F0001', native_definition='D1', native_solid_count=1,
        document='assembly.stp', part_name='Panel', mode='make', priced=True,
        quantity_per_set=1, quantity_total=1, volume_mm3='1000000', material_raw=None,
        material_cost='60.00', process_cost='30.00', purchase_cost='0.00', setup_cost='10.00',
        total_cost='100.00', total_price='120.00', unit_cost='100.00', unit_price='120.00', notes=[])
    bought = dict(made, id='L0002', native_definition='D2', part_name='Fastener', mode='buy',
        material_cost='0.00', process_cost='0.00', purchase_cost='20.00', setup_cost='0.00',
        total_cost='20.00', total_price='24.00', unit_cost='20.00', unit_price='24.00')
    unknown = dict(made, id='L0003', native_definition='D3', part_name='Invalid geometry', priced=False,
        material_cost=None, process_cost=None, purchase_cost=None, setup_cost=None, total_cost=None,
        total_price=None, unit_cost=None, unit_price=None, geometry_valid=False)
    return {'name': 'Test order', 'run_id': 'RUN-' + '1' * 32, 'batch_id': 'BATCH-' + '2' * 32,
        'source_sha256': 'a' * 64, 'source_files': [{'id': 'F0001', 'sha256': 'b' * 64}],
        'settings': {'order_sets': 1, 'pricing_method': 'cost_plus', 'rate_percent': '20'},
        'lines': [made, bought, unknown], 'totals': dict(material_cost='60.00', process_cost='30.00',
        purchase_cost='20.00', setup_cost='10.00', total_cost='120.00', total_price='144.00',
        priced_lines=2, unpriced_lines=1)}


def entry(identity, line, category, method='fixed', **fields):
    return dict(id=identity, line_id=line, category=category, label='人工费用', method=method,
                source_kind='manual_adopted', **fields)


def policy(base):
    return build_cost_sheet(base)['policy']


def remove(choice, line, *categories):
    choice['entries'] = [e for e in choice['entries'] if e['line_id'] != line or e['category'] not in categories]


class CostSheetTests(unittest.TestCase):
    def test_build_migrates_actual_saved_categories_without_fabricating_operations(self):
        base = fixture(); saved = copy.deepcopy(base)
        sheet = build_cost_sheet(base)
        self.assertEqual(base, saved)
        self.assertEqual(sheet['base_digest'], digest(base))
        self.assertEqual(sheet['totals']['total_cost'], '120.00')
        self.assertEqual(sheet['totals']['total_price'], '144.00')
        self.assertEqual(len(sheet['entries']), 8)
        self.assertTrue(all(e['method'] == 'retained' and e['source_kind'] == 'legacy_estimate' for e in sheet['entries']))
        self.assertFalse(any(e['line_id'] == 'L0003' for e in sheet['entries']))
        self.assertEqual(len(sheet['catalog']), 27)
        self.assertTrue(all(e['default_rate'] is None for e in sheet['catalog']))
        self.assertEqual(sheet['customer_terms'], dict(delivery='', payment='', validity='', tax_shipping=''))

    def test_unchanged_policy_preserves_every_financial_value_and_source_fact(self):
        base = fixture(); original = copy.deepcopy(base); choice = policy(base)
        new = reprice_cost_sheet(base, choice)
        self.assertEqual(base, original)
        for key, value in base['totals'].items():
            self.assertEqual(new['totals'][key], value)
        self.assertEqual(new['settings'], base['settings'])
        self.assertEqual(new['source_files'], base['source_files'])
        for old, updated in zip(base['lines'], new['lines']):
            for key in ('id', 'file_id', 'native_definition', 'native_solid_count', 'volume_mm3',
                        'quantity_per_set', 'quantity_total', 'material_raw', 'priced'):
                self.assertEqual(updated[key], old[key])
        self.assertEqual(new['lines'][2], base['lines'][2])
        self.assertEqual(new['status'], 'pending_review')
        self.assertEqual(new['cost_sheet']['source_digest'], digest(base))

    def test_material_loss_scrap_formula_uses_whole_order_and_preserves_precision(self):
        base = fixture(); choice = policy(base)
        remove(choice, 'L0001', 'material')
        choice['entries'].append(entry('new-material', 'L0001', 'material', 'material',
            net_kg='10', loss_kg='2', unit_price='5', scrap_price='1'))
        new = reprice_cost_sheet(base, choice)
        self.assertEqual(new['lines'][0]['material_cost'], '58.00')
        self.assertEqual(new['totals']['total_cost'], '118.00')
        self.assertEqual(new['totals']['total_price'], '141.60')
        self.assertEqual(new['cost_sheet']['entry_amounts']['new-material'], '58.00')
        self.assertEqual(new['lines'][0]['volume_mm3'], '1000000')
        self.assertIsNone(new['lines'][0]['material_raw'])

    def test_process_replaces_old_processing_and_setup_instead_of_adding(self):
        base = fixture(); choice = policy(base)
        choice['entries'].append(entry('laser', 'L0001', 'process', 'quantity_rate',
                                      quantity='10', rate='0.7', unit='m', process_id='EXTRA-LASER'))
        with self.assertRaises(ValueError):
            reprice_cost_sheet(base, choice)
        remove(choice, 'L0001', 'process')
        choice['entries'].append(entry('laser', 'L0001', 'process', 'quantity_rate',
                                      quantity='10', rate='0.7', unit='m', process_id='EXTRA-LASER'))
        with self.assertRaises(ValueError):
            reprice_cost_sheet(base, choice)
        remove(choice, 'L0001', 'setup')
        new = reprice_cost_sheet(base, choice)
        self.assertEqual(new['lines'][0]['process_cost'], '7.00')
        self.assertEqual(new['lines'][0]['setup_cost'], '0.00')
        self.assertEqual(new['totals']['total_cost'], '87.00')
        self.assertEqual(new['totals']['total_price'], '104.40')

    def test_largest_remainder_allocation_conserves_cents_and_repeat_does_not_accumulate(self):
        base = fixture(); choice = policy(base)
        choice['entries'].append(entry('other-cent', None, 'other', amount='0.01'))
        first = reprice_cost_sheet(base, choice)
        self.assertEqual([r.get('other_cost') for r in first['lines']], ['0.01', '0.00', None])
        self.assertEqual(sum(Decimal(a['amount']) for a in first['cost_sheet']['allocations']), Decimal('.01'))
        second = reprice_cost_sheet(first, policy(first))
        self.assertEqual(second['totals'], first['totals'])
        self.assertEqual(second['cost_sheet']['allocations'], first['cost_sheet']['allocations'])
        self.assertEqual(second['cost_sheet']['allocation_basis'], {'L0001': '100.00', 'L0002': '20.00'})

    def test_tooling_full_vs_amortized_are_exclusive_and_not_charged_twice(self):
        base = fixture(); choice = policy(base)
        choice['entries'].append(entry('tool-amortized', None, 'tooling', 'tooling_allocation',
            tooling_id='TOOL-1', tooling_amount='1000', amortization_quantity='1000'))
        new = reprice_cost_sheet(base, choice)
        self.assertEqual(new['totals']['tooling_cost'], '1.00')
        self.assertEqual(new['totals']['total_cost'], '121.00')
        choice['entries'].append(entry('tool-full', 'L0001', 'tooling', amount='1000', tooling_id='TOOL-1'))
        with self.assertRaises(ValueError):
            reprice_cost_sheet(base, choice)

    def test_allocation_cell_budget_rejects_cartesian_expansion_before_allocating(self):
        base = fixture(); choice = policy(base)
        choice['entries'].extend([entry('global-one', None, 'other', amount='1'),
                                  entry('global-two', None, 'other', amount='2')])
        original = copy.deepcopy(base)
        with patch('batch_cost_sheet.MAX_ALLOCATION_CELLS', 3), patch('batch_cost_sheet._allocate') as allocate:
            with self.assertRaisesRegex(ValueError, '合并同类整单费用或拆单'):
                reprice_cost_sheet(base, choice)
            allocate.assert_not_called()
        with patch('batch_cost_sheet.MAX_ALLOCATION_CELLS', 4):
            new = reprice_cost_sheet(base, choice)
        self.assertEqual(len(new['cost_sheet']['allocations']), 4)
        self.assertEqual(new['totals']['other_cost'], '3.00')
        self.assertEqual(new['totals']['total_cost'], '123.00')
        self.assertEqual(base, original)

    def test_unpriced_geometry_requires_explicit_complete_manual_adoption(self):
        base = fixture(); choice = policy(base)
        choice['entries'].extend([entry('manual-mat', 'L0003', 'material', amount='20'),
                                  entry('manual-process', 'L0003', 'process', amount='10')])
        with self.assertRaises(ValueError):
            reprice_cost_sheet(base, choice)
        choice['adopt_unpriced_line_ids'] = ['L0003']
        new = reprice_cost_sheet(base, choice)
        self.assertEqual(new['totals']['total_price'], '180.00')
        self.assertEqual(new['totals']['priced_lines'], 3)
        self.assertFalse(new['lines'][2]['geometry_valid'])
        self.assertFalse(new['lines'][2]['cost_sheet_adoption']['native_geometry_approved'])
        remove(choice, 'L0003', 'process')
        with self.assertRaises(ValueError):
            reprice_cost_sheet(base, choice)

    def test_gross_margin_uses_original_policy_without_pdf_defaults(self):
        base = fixture()
        base['settings']['pricing_method'] = 'gross_margin'
        for line, price in zip(base['lines'], ('125.00', '25.00')):
            line.update(total_price=price, unit_price=price)
        base['totals']['total_price'] = '150.00'
        choice = policy(base)
        choice['entries'].append(entry('extra', 'L0001', 'other', amount='4'))
        new = reprice_cost_sheet(base, choice)
        self.assertEqual(new['totals']['total_price'], '155.00')
        self.assertEqual(new['settings']['rate_percent'], '20')

    def test_strict_numbers_fields_ids_sources_and_retained_binding(self):
        base = fixture()
        for field, value in (('amount', '1'), ('source_kind', 'manual_adopted'), ('label', 'changed')):
            choice = policy(base); choice['entries'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                reprice_cost_sheet(base, choice)
        for value in ('', '-1', 'NaN', 'Infinity', '1e3', '01', '0.000000001', True, 3):
            choice = policy(base); choice['entries'].append(entry('bad-number', 'L0001', 'other', amount=value))
            with self.subTest(value=value), self.assertRaises(ValueError):
                reprice_cost_sheet(base, choice)
        choice = policy(base); choice['entries'].append(copy.deepcopy(choice['entries'][0]))
        with self.assertRaises(ValueError): reprice_cost_sheet(base, choice)
        choice = policy(base); choice['base_digest'] = '0' * 64
        with self.assertRaises(ValueError): reprice_cost_sheet(base, choice)
        choice = policy(base); choice['source_binding'] = {}
        with self.assertRaises(ValueError): reprice_cost_sheet(base, choice)
        choice = policy(base); choice['entries'].append(entry('fake-cad', 'L0001', 'other', amount='2'))
        choice['entries'][-1]['source_kind'] = 'cad_verified'
        with self.assertRaises(ValueError): reprice_cost_sheet(base, choice)

    def test_customer_terms_are_explicit_and_internal_html_escapes_sources(self):
        base = fixture(); base['name'] = '<script>bad()</script>'
        choice = policy(base); choice['customer_terms'] = {'delivery': '采用后15天', 'payment': '<img src=x>',
            'validity': '7天', 'tax_shipping': '税运另议'}
        new = reprice_cost_sheet(base, choice)
        rendered = render_cost_sheet_html(new).decode()
        self.assertIn('采用后15天', rendered)
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertNotIn('<img src=x>', rendered)
        self.assertEqual(new['cost_sheet']['customer_terms'], choice['customer_terms'])

    def test_engineering_proofs_are_retained_only_for_unchanged_categories(self):
        from test_batch_engineering_pricing import fixture as engineered_fixture
        from batch_engineering_pricing import pricing_plan, reprice_quote
        from batch_engineering_bom import _priced_requirement
        base, manifest = engineered_fixture(market=True)
        base = reprice_quote(base, manifest, pricing_plan(base, manifest)['policy'])
        choice = policy(base)
        remove(choice, 'L0001', 'process', 'setup')
        choice['entries'].append(entry('new-weld', 'L0001', 'process', amount='99'))
        new = reprice_cost_sheet(base, choice)
        material = next(r for r in manifest['quote_basis']['requirements_review'] if r['line_id'] == 'L0001' and r['field'] == 'material_grade')
        weld = next(r for r in manifest['quote_basis']['requirements_review'] if r['line_id'] == 'L0001' and r['field'] == 'document_note')
        self.assertIsNotNone(_priced_requirement(new, new['lines'][0], material))
        self.assertIsNone(_priced_requirement(new, new['lines'][0], weld))

    def test_saved_pipe_and_mesh_inclusive_processing_blocks_extra_setup_in_both_directions(self):
        from test_batch_engineering_pricing import fixture as engineered_fixture
        from batch_engineering_pricing import pricing_plan, reprice_quote
        source, manifest = engineered_fixture(market=True)
        source = reprice_quote(source, manifest, pricing_plan(source, manifest)['policy'])
        original = copy.deepcopy(source)
        for identity in ('L0001', 'L0002'):
            # Removing the old zero setup row is insufficient: retained pipe
            # and mesh welding still includes preparation in its saved amount.
            choice = policy(source)
            remove(choice, identity, 'setup')
            choice['entries'].append(entry('extra-setup-' + identity, identity, 'setup', amount='10'))
            with self.subTest(identity=identity, direction='retained-inclusive-process'), self.assertRaisesRegex(ValueError, '已包含准备费'):
                reprice_cost_sheet(source, choice)
            # The converse remains forbidden: a new process cannot retain the
            # old preparation row, including a formerly saved zero row.
            choice = policy(source)
            remove(choice, identity, 'process')
            choice['entries'].append(entry('new-process-' + identity, identity, 'process', amount='99'))
            with self.subTest(identity=identity, direction='retained-setup'), self.assertRaisesRegex(ValueError, '同时撤下原准备费'):
                reprice_cost_sheet(source, choice)
        self.assertEqual(source, original)

    def test_explicit_split_replaces_inclusive_weld_group_and_can_keep_total_unchanged(self):
        from test_batch_engineering_pricing import fixture as engineered_fixture
        from batch_engineering_pricing import pricing_plan, reprice_quote
        source, manifest = engineered_fixture(market=True)
        source = reprice_quote(source, manifest, pricing_plan(source, manifest)['policy'])
        choice = policy(source)
        for identity in ('L0001', 'L0002'):
            old = next(line for line in source['lines'] if line['id'] == identity)
            remove(choice, identity, 'process', 'setup')
            choice['entries'].extend([
                entry('split-process-' + identity, identity, 'process', amount=str(Decimal(old['process_cost']) - 10)),
                entry('split-setup-' + identity, identity, 'setup', amount='10')])
        new = reprice_cost_sheet(source, choice)
        self.assertEqual(new['totals']['total_cost'], source['totals']['total_cost'])
        self.assertEqual(new['totals']['total_price'], source['totals']['total_price'])
        self.assertEqual(new['status'], 'pending_review')
        self.assertTrue(all(row['status'] != 'priced' for row in new['engineering_repricing']['applied_requirements']
                            if row['method'] in {'pipe_weld_seal', 'mesh_weld'}))
        repeated = reprice_cost_sheet(new, policy(new))
        self.assertEqual(repeated['totals'], new['totals'])

    def test_setup_inclusion_survives_an_unchanged_cost_sheet_save(self):
        from test_batch_engineering_pricing import fixture as engineered_fixture
        from batch_engineering_pricing import pricing_plan, reprice_quote
        source, manifest = engineered_fixture(market=True)
        source = reprice_quote(source, manifest, pricing_plan(source, manifest)['policy'])
        saved = reprice_cost_sheet(source, policy(source))
        for identity in ('L0001', 'L0002'):
            choice = policy(saved)
            remove(choice, identity, 'setup')
            choice['entries'].append(entry('later-setup-' + identity, identity, 'setup', amount='10'))
            with self.subTest(identity=identity), self.assertRaisesRegex(ValueError, '已包含准备费'):
                reprice_cost_sheet(saved, choice)

if __name__ == '__main__':
    unittest.main()
