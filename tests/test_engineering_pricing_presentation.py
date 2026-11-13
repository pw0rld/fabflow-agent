"""A priced requirement must have saved cost evidence, not just a green label."""
import copy
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_engineering_bom import build_engineering_bom
from batch_engineering_pricing import pricing_plan, reprice_quote
from batch_quote_costing import _engineering_repricing_summary, aggregate_display_groups, calculate_batch, material_display, render_artifact
from test_batch_material_prices import adopted_row, market_settings
from test_batch_quote_costing import document, solid


def fixture():
    base = calculate_batch([document([solid(index, name=name)], file_id=f'F{index:04d}')
                            for index, name in enumerate(('Pipe', 'Mesh', 'Zinc', 'Untouched'), 1)],
        market_settings({'carbon_steel': adopted_row(), 'ss316': adopted_row('29', profile='ss316')}, order_sets=10))
    base['run_id'] = 'RUN-' + 'a' * 32
    base['source_files'].append({'id': 'NOTE-1', 'kind': 'image', 'status': 'reference',
                                 'name': '工艺说明.png', 'sha256': 'c' * 64})
    recognition = {'ai': {'lines': {}}}
    for identity, field, value, text in (
        ('L0001', 'material_grade', '316不锈钢', '此圆管焊接固定/不能漏水漏油/316不锈钢'),
        ('L0002', 'specification', '8目不锈钢纱网', '焊接8目不锈钢纱网'),
        ('L0003', 'surface_treatment', '热侵锌处理', '热侵锌处理')):
        recognition['ai']['lines'][identity] = {'values': {field: value}, 'field_states': {field: 'candidate'},
            'evidence': {field: [{'reference': 'NOTE-1', 'page': 1, 'region': '工程备注', 'quote': text,
                                   'basis': 'ai_document', 'status': 'pending_review'}]}}
    manifest = build_engineering_bom(base, aggregate_display_groups(base), recognition=recognition)
    result = reprice_quote(base, manifest, pricing_plan(base, manifest)['policy'])
    return base, result, recognition


class EngineeringPricingPresentationTests(unittest.TestCase):
    def test_requirements_become_priced_with_auditable_costs_but_not_cad_material_facts(self):
        base, result, recognition = fixture()
        bom = build_engineering_bom(result, aggregate_display_groups(result), recognition=recognition)
        self.assertEqual(bom['quote_basis']['unresolved_requirements'], 0)
        self.assertEqual(len(bom['quote_basis']['requirements_review']), 5)
        self.assertTrue(all(row['status'] == 'priced' and row['cost_evidence']['basis']
                            for row in bom['quote_basis']['requirements_review']))
        self.assertTrue(all(line.get('material_raw') is None for line in result['lines']))
        rows = {row['id']: row for row in bom['lines']}
        self.assertIn('316', rows['L0001']['pricing_material']['label'])
        self.assertFalse(rows['L0001']['pricing_material']['is_assumption'])
        self.assertIn('8目不锈钢纱网', rows['L0002']['pricing_material']['label'])
        self.assertIsNone(rows['L0002']['material_grade'])
        self.assertTrue(rows['L0004']['pricing_material']['is_assumption'])
        self.assertIsNone(rows['L0004']['material_grade'])
        self.assertEqual(bom['quote_basis']['engineering_repricing']['source_run_id'], base['run_id'])

    def test_tampered_cost_metadata_cannot_resolve_requirements(self):
        _, result, recognition = fixture()
        for change in ('amount', 'source', 'totals', 'cost'):
            bad = copy.deepcopy(result)
            snapshot = bad['engineering_repricing']
            if change == 'amount':
                for item in snapshot['applied_requirements']:
                    item['cost_evidence']['amount'] = '999999.99'
            elif change == 'source':
                for item in snapshot['applied_requirements']:
                    item['source_evidence'] = []
            elif change == 'totals':
                snapshot['after_totals']['total_price'] = '0.00'
            else:
                for item in snapshot['line_costs']:
                    item['unit_price'] = '0.00'
            with self.subTest(change=change):
                bom = build_engineering_bom(bad, aggregate_display_groups(bad), recognition=recognition)
                self.assertFalse(any(item['status'] == 'priced' for item in bom['quote_basis']['requirements_review']))

    def test_later_requirement_text_does_not_inherit_old_priced_status(self):
        _, result, recognition = fixture()
        changed = copy.deepcopy(recognition)
        changed['ai']['lines']['L0001']['values']['material_grade'] = '304不锈钢'
        bom = build_engineering_bom(result, aggregate_display_groups(result), recognition=changed)
        requirement = next(row for row in bom['quote_basis']['requirements_review'] if row['field'] == 'material_grade')
        self.assertEqual(requirement['requirement'], '304不锈钢')
        self.assertEqual(requirement['status'], 'not_adopted')

    def test_quote_and_print_show_new_amount_and_scopes_while_old_snapshot_is_unchanged(self):
        base, result, recognition = fixture()
        saved = copy.deepcopy(base)
        originals = {name: render_artifact(base, name)[0] for name in ('quote.json', 'bom.csv', 'cost.csv')}
        for name in ('quote.html', 'quote-print.html'):
            page = render_artifact(result, name, engineering_recognition=recognition)[0].decode()
            self.assertIn('data-engineering-repricing', page)
            self.assertIn('订单成本（元）', page)
            self.assertIn('替代原综合加工', page)
            self.assertIn('同一热浸锌工序组只收一次最低费', page)
            self.assertNotIn('data-engineering-pricing-notice', page)
            old = render_artifact(base, name, engineering_recognition=recognition)[0].decode()
            self.assertIn('data-engineering-pricing-notice', old)
            self.assertNotIn('data-engineering-repricing', old)
        self.assertEqual(base, saved)
        self.assertEqual(originals, {name: render_artifact(base, name)[0] for name in originals})

    def test_mesh_is_a_procurement_requirement_without_fabricated_grade_or_cad_text(self):
        _, result, _ = fixture()
        mesh = result['lines'][1]
        display = material_display(mesh)
        self.assertEqual(display['cad_text'], '')
        self.assertIn('牌号未定', display['adopted_text'])
        self.assertEqual(display['adopted_label'], '按图纸核价采用')
        self.assertIn('模型估重不等同于实际网重', display['note'])
        self.assertEqual(mesh['material_cost'], '0.00')
        self.assertEqual(mesh['setup_cost'], '0.00')

    def test_lower_new_quote_displays_a_negative_adjustment(self):
        _, result, _ = fixture()
        result['engineering_repricing']['before_totals']['total_price'] = str(Decimal(result['totals']['total_price']) + Decimal('1'))
        summary = _engineering_repricing_summary(result).decode()
        self.assertIn('调整 ¥−1.00', summary)
        self.assertNotIn('调整 ¥未计价', summary)

    def test_demo_material_source_is_not_labelled_as_market_data(self):
        _, result, _ = fixture()
        source = copy.deepcopy(result['lines'][0])
        for key in ('material_price_snapshot', 'material_price_source_kind'):
            source.pop(key, None)
        display = material_display(source)
        self.assertEqual(display['price_text'], '演示材料价')
        self.assertIn('演示材料价', display['note'])
        self.assertNotIn('行情参考', display['note'])

    def test_saved_operation_zinc_is_resolved_by_its_actual_charge(self):
        base, _, recognition = fixture()
        base['lines'][2]['operations'] = ['热浸锌处理']
        recognition['ai']['lines'].pop('L0003')
        manifest = build_engineering_bom(base, aggregate_display_groups(base), recognition=recognition)
        result = reprice_quote(base, manifest, pricing_plan(base, manifest)['policy'])
        updated = build_engineering_bom(result, aggregate_display_groups(result), recognition=recognition)
        operation = next(item for item in updated['quote_basis']['requirements_review'] if item['field'] == 'operations')
        self.assertEqual(operation['status'], 'priced')
        self.assertEqual(operation['cost_evidence']['method'], 'hotdip')


if __name__ == '__main__':
    unittest.main()
