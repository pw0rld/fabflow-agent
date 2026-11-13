"""Synthetic native scalars verify deterministic prices, not CAD accuracy."""
import copy
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_engineering_pricing import MESH_DESCRIPTION, MESH_NOTE, PIPE_NOTE, pricing_plan, reprice_quote
from batch_quote_costing import calculate_batch
from test_batch_quote_costing import document, settings, solid
from test_batch_material_prices import adopted_row, market_settings


def manifest_for(base):
    rows = [{**{key: copy.deepcopy(line.get(key)) for key in
                ('id', 'file_id', 'native_definition', 'document', 'part_name', 'quantity_per_set', 'quantity_total')},
             'material_grade': None, 'material_description': None, 'surface_treatment': None,
             'field_sources': {}, 'document_requirements': [], 'recognition': {'fields': {}}} for line in base['lines']]
    return {'lines': rows, 'quote_basis': {'order_sets': base['settings']['order_sets'], 'requirements_review': []}}


def add_requirement(manifest, line_id, field, requirement):
    row = next(row for row in manifest['lines'] if row['id'] == line_id)
    source = {'reference': 'IMG1', 'page': 1, 'region': '对应零件工程备注', 'quote': requirement,
              'status': 'pending_review', 'basis': 'ai_document', 'private_path': '/private/do-not-export'}
    if field == 'document_note':
        row['document_requirements'].append({**source, 'text': requirement})
    elif field == 'material_description':
        row[field] = requirement
        row['material_description_evidence'] = {'source_text': requirement, 'references': [source]}
    else:
        row[field] = requirement
        row['field_sources'][field] = 'ai_document'
        row['recognition']['fields'][field] = {'source': 'ai_document', 'status': 'candidate',
                                               'value': requirement, 'evidence': [source]}
    manifest['quote_basis']['requirements_review'].append({'line_id': line_id, 'part_name': row['part_name'],
        'field': field, 'requirement': requirement, 'status': 'not_adopted', 'reason': '原模拟金额未包含'})


def fixture(*, market=False, pricing_method='cost_plus'):
    records = [solid(1, 'pipe/solid-1', volume=100000, name='Pipe', grade='Steel'),
               solid(2, 'mesh/solid-1', volume=50000, name='Mesh'),
               solid(3, 'zinc/solid-1', volume=10000, name='Zinc'),
               solid(4, 'untouched/solid-1', volume=1000000, name='Untouched'),
               solid(5, 'invalid/solid-1', volume=5000, name='Invalid', valid=False)]
    choice = (market_settings({'carbon_steel': adopted_row(), 'ss316': adopted_row('29', profile='ss316')},
                              order_sets=10, pricing_method=pricing_method) if market else
              settings(order_sets=10, pricing_method=pricing_method))
    drawing = {'id': 'IMG1', 'name': 'requirements.png', 'relative_path': 'requirements.png',
               'sha256': 'b' * 64, 'kind': 'image', 'status': 'reference', 'error': None}
    base = calculate_batch([document(records), drawing], choice)
    base.update(run_id='RUN-original', batch_id='BATCH-example')
    manifest = manifest_for(base)
    for line_id, field, text in [('L0001', 'material_grade', '316不锈钢'), ('L0001', 'document_note', PIPE_NOTE),
        ('L0002', 'material_description', MESH_DESCRIPTION), ('L0002', 'document_note', MESH_NOTE),
        ('L0003', 'material_grade', 'Q235'), ('L0003', 'surface_treatment', '热侵锌处理')]:
        add_requirement(manifest, line_id, field, text)
    return base, manifest


def entry_for(policy, method):
    return next(entry for entry in policy['entries'] if entry['method'] == method)


class EngineeringRepricingTests(unittest.TestCase):
    def test_six_requirements_keep_source_original_and_unaffected_lines_immutable(self):
        base, manifest = fixture()
        original, saved_manifest = copy.deepcopy(base), copy.deepcopy(manifest)
        plan = pricing_plan(base, manifest)
        self.assertEqual(plan['policy']['version'], 'engineering-demo-v1')
        self.assertEqual(len(plan['policy']['entries']), 6)
        result = reprice_quote(base, manifest, plan['policy'])
        self.assertEqual(base, original)
        self.assertEqual(manifest, saved_manifest)
        self.assertEqual(result['source_files'], base['source_files'])
        for old, new in zip(base['lines'], result['lines']):
            for key in ('id', 'file_id', 'native_definition', 'quantity_per_set', 'quantity_total', 'volume_mm3', 'material_raw'):
                self.assertEqual(new[key], old[key], key)
        self.assertEqual(result['lines'][3:], base['lines'][3:])
        self.assertEqual(result['totals']['total_cost'], '1790.53')
        self.assertEqual(result['totals']['total_price'], '2148.50')
        self.assertEqual((result['totals']['priced_lines'], result['totals']['unpriced_lines']), (4, 1))
        self.assertEqual(result['weight_summary']['estimated_total_net_weight_kg'], '91.6475')
        self.assertEqual(result['weight_summary']['low_confidence_total_net_weight_kg'], '0.3925')
        audit = result['engineering_repricing']
        self.assertEqual(audit['base_run_id'], 'RUN-original')
        self.assertEqual(audit['before_totals'], base['totals'])
        self.assertEqual(audit['after_totals'], result['totals'])
        self.assertEqual(len(audit['applied_requirements']), 6)
        self.assertEqual(len(audit['line_costs']), 3)
        self.assertTrue(all(row['status'] == 'priced' and row['cost_evidence']['amount'] is not None for row in audit['applied_requirements']))
        self.assertTrue(result['simulated'])
        self.assertEqual(result['status'], 'pending_review')
        for flag in ('engineering_approved', 'business_approved', 'tax_included', 'freight_included'):
            self.assertFalse(result[flag])

    def test_pipe_weld_replaces_process_and_setup_and_preserves_native_material_raw(self):
        base, manifest = fixture()
        result = reprice_quote(base, manifest, pricing_plan(base, manifest)['policy'])
        line = result['lines'][0]
        self.assertEqual((line['material_profile'], line['density_kg_m3'], line['material_raw']), ('ss316', '7980', 'Steel'))
        self.assertEqual(line['material_requirement'], '316不锈钢')
        self.assertEqual(line['estimated_net_weight_kg'], '0.798')
        # .798/.85 *28*10 =262.870588...; inclusive processing15*10.
        self.assertEqual((line['material_cost'], line['process_cost'], line['setup_cost']), ('262.87', '150.00', '0.00'))
        self.assertEqual((line['total_cost'], line['unit_price'], line['total_price']), ('412.87', '49.54', '495.40'))
        proof = next(row['cost_evidence'] for row in result['engineering_repricing']['applied_requirements'] if row['method'] == 'pipe_weld_seal')
        self.assertEqual(proof['replaces'], {'process_cost': '62.80', 'setup_cost': '30.00'})
        self.assertIn('不表示已完成压力试验或质量验收', proof['inclusions'])
        self.assertEqual(proof['source_kind'], 'demo')

    def test_mesh_purchase_replaces_raw_material_without_inventing_grade_or_wire_gauge(self):
        base, manifest = fixture()
        result = reprice_quote(base, manifest, pricing_plan(base, manifest)['policy'])
        line = result['lines'][1]
        self.assertEqual((line['mode'], line['material_profile'], line['material_requirement']), ('buy', 'stainless_mesh', MESH_DESCRIPTION))
        self.assertEqual((line['density_kg_m3'], line['estimated_total_net_weight_kg']), ('7980', '3.99'))
        self.assertEqual(line['confidence'], 'proxy_material_estimate')
        self.assertIn('代理', line['weight_basis'])
        self.assertIn('未验证丝径', line['weight_basis'])
        for key in ('material_raw', 'material_rate', 'material_usage_kg'):
            self.assertIsNone(line[key])
        self.assertNotIn('material_price_snapshot', line)
        self.assertEqual((line['material_cost'], line['purchase_cost'], line['process_cost'], line['setup_cost']),
                         ('0.00', '20.00', '30.00', '0.00'))
        self.assertEqual((line['total_cost'], line['unit_price'], line['total_price']), ('50.00', '6.00', '60.00'))
        notes = json.dumps(result['engineering_repricing']['assumptions'], ensure_ascii=False)
        self.assertIn('不指定304/316牌号', notes)
        self.assertIn('不是好运来的采购成交价', notes)

    def test_market_material_uses_saved_tax_rate_and_retains_reference_form_limit(self):
        base, manifest = fixture(market=True)
        result = reprice_quote(base, manifest, pricing_plan(base, manifest)['policy'])
        pipe = result['lines'][0]
        # .798/.85 *(29/1.13)*10 =240.937012...; no fallback to28.
        self.assertEqual(pipe['material_cost'], '240.94')
        self.assertTrue(pipe['material_rate'].startswith('25.663716814159292035'))
        snapshot = pipe['material_price_snapshot']
        self.assertEqual((snapshot['reference_id'], snapshot['original_rate']), ('FS-316L-2B-20261103', '29'))
        self.assertTrue(snapshot['adopted_tax_included'])
        self.assertFalse(snapshot['supplier_price_verified'])
        self.assertEqual(snapshot['reference']['form'], '冷轧卷板')
        self.assertTrue(any('未自动核对' in note for note in snapshot['notes']))
        self.assertEqual({item['material_profile'] for item in result['material_price_sources']}, {'carbon_steel', 'ss316'})
        missing = copy.deepcopy(base)
        missing['settings']['material_pricing']['rows'].pop('ss316')
        with self.assertRaisesRegex(ValueError, '缺少本单有效采用价'):
            pricing_plan(missing, manifest)

    def test_zinc_minimum_is_once_per_order_and_allocated_cents_are_conserved(self):
        base = calculate_batch([document([solid(i, 'part-' + str(i) + '/solid-1', volume=10000) for i in range(1, 4)])], settings(order_sets=10))
        manifest = manifest_for(base)
        for line in base['lines']:
            add_requirement(manifest, line['id'], 'surface_treatment', '热侵锌处理')
        policy = pricing_plan(base, manifest)['policy']
        for entry in policy['entries']:
            entry['minimum'] = '30.01'
        result = reprice_quote(base, manifest, policy)
        proofs = [row['cost_evidence'] for row in result['engineering_repricing']['applied_requirements']]
        self.assertEqual([proof['amount'] for proof in proofs], ['10.01', '10.00', '10.00'])
        self.assertEqual(sum(Decimal(proof['amount']) for proof in proofs), Decimal('30.01'))
        self.assertTrue(all(proof['group_cost'] == '30.01' and proof['group_weight_kg'] == '2.355' for proof in proofs))
        self.assertTrue(all(proof['minimum_applied'] for proof in proofs))
        self.assertEqual([line['setup_cost'] for line in result['lines']], ['30.00'] * 3)
        self.assertEqual([line['process_cost'] for line in result['lines']], ['60.01', '60.00', '60.00'])
        reverse = copy.deepcopy(policy)
        reverse['entries'].reverse()
        again = reprice_quote(base, manifest, reverse)
        self.assertEqual(result['engineering_repricing']['line_costs'], again['engineering_repricing']['line_costs'])
        mismatch = copy.deepcopy(policy)
        mismatch['entries'][0]['rate'] = '3.00'
        with self.assertRaisesRegex(ValueError, '相同费率'):
            reprice_quote(base, manifest, mismatch)

    def test_unit_price_rounding_precedes_order_total_and_margin_formula_differs(self):
        base, manifest = fixture()
        result = reprice_quote(base, manifest, pricing_plan(base, manifest)['policy'])
        zinc = result['lines'][2]
        self.assertEqual((zinc['material_cost'], zinc['process_cost'], zinc['setup_cost']), ('5.54', '80.00', '30.00'))
        self.assertEqual((zinc['total_cost'], zinc['unit_price'], zinc['total_price']), ('115.54', '13.86', '138.60'))
        self.assertNotEqual(zinc['total_price'], '138.65')
        margin_base, margin_manifest = fixture(pricing_method='gross_margin')
        margin = reprice_quote(margin_base, margin_manifest, pricing_plan(margin_base, margin_manifest)['policy'])
        self.assertEqual((margin['lines'][0]['unit_price'], margin['lines'][0]['total_price']), ('51.61', '516.10'))
        self.assertEqual(margin['lines'][2]['total_price'], '144.40')

    def test_policy_rejects_changed_ids_text_private_fields_methods_and_missing_entries(self):
        base, manifest = fixture()
        policy = pricing_plan(base, manifest)['policy']
        for key, value in [('line_id', 'L0004'), ('requirement', '热镀锌'), ('method', 'free'), ('unit', 'hour'),
                           ('source_kind', 'supplier_quote'), ('inclusions', ['已通过验收']), ('material_profile', 'ss316'), ('private_path', '/private/no')]:
            modified = copy.deepcopy(policy)
            entry_for(modified, 'hotdip')[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                reprice_quote(base, manifest, modified)
        missing, duplicate = copy.deepcopy(policy), copy.deepcopy(policy)
        missing['entries'].pop()
        duplicate['entries'][-1] = copy.deepcopy(duplicate['entries'][0])
        for modified in (missing, duplicate):
            with self.assertRaises(ValueError):
                reprice_quote(base, manifest, modified)
        changed_base = copy.deepcopy(base)
        changed_base['source_files'][0]['sha256'] = 'c' * 64
        with self.assertRaisesRegex(ValueError, '已过期'):
            reprice_quote(changed_base, manifest, policy)
        changed_source = copy.deepcopy(manifest)
        changed_source['lines'][0]['recognition']['fields']['material_grade']['evidence'][0]['region'] = '新的区域'
        with self.assertRaisesRegex(ValueError, '已过期'):
            reprice_quote(base, changed_source, policy)

    def test_manifest_cannot_substitute_source_quantity_or_omit_requirements(self):
        base, manifest = fixture()
        for key, value in [('file_id', 'F-other'), ('native_definition', 'other'), ('quantity_per_set', 2),
                           ('quantity_total', True), ('volume_mm3', '999')]:
            altered = copy.deepcopy(manifest)
            altered['lines'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                pricing_plan(base, altered)
        truncated = copy.deepcopy(manifest)
        truncated['quote_basis']['requirements_review'].pop()
        with self.assertRaisesRegex(ValueError, '未覆盖全部'):
            pricing_plan(base, truncated)
        forged = copy.deepcopy(manifest)
        forged['quote_basis']['requirements_review'][0]['requirement'] = 'SUS304'
        with self.assertRaisesRegex(ValueError, '已保存字段不一致'):
            pricing_plan(base, forged)
        unknown = copy.deepcopy(manifest)
        add_requirement(unknown, 'L0004', 'surface_treatment', '特殊涂层')
        with self.assertRaisesRegex(ValueError, '没有覆盖'):
            pricing_plan(base, unknown)

    def test_rate_validation_rejects_nondecimal_free_fees_and_duplicate_preparation(self):
        base, manifest = fixture()
        policy = pricing_plan(base, manifest)['policy']
        for value in (True, 15, None, '', '0', '-1', 'NaN', 'Infinity', '1e2', '1000001', '1.000000001'):
            altered = copy.deepcopy(policy)
            entry_for(altered, 'pipe_weld_seal')['rate'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                reprice_quote(base, manifest, altered)
        altered = copy.deepcopy(policy)
        entry_for(altered, 'purchase_mesh')['minimum'] = '30'
        with self.assertRaisesRegex(ValueError, '已包含准备费'):
            reprice_quote(base, manifest, altered)
        altered = copy.deepcopy(policy)
        entry_for(altered, 'adopt_material')['rate'] = '6.00'
        with self.assertRaisesRegex(ValueError, '已保存采用价'):
            reprice_quote(base, manifest, altered)

    def test_invalid_geometry_stays_excluded_and_repricing_cannot_compound(self):
        base, manifest = fixture()
        add_requirement(manifest, 'L0005', 'material_grade', 'Q235')
        add_requirement(manifest, 'L0005', 'surface_treatment', '热侵锌处理')
        result = reprice_quote(base, manifest, pricing_plan(base, manifest)['policy'])
        self.assertEqual(result['lines'][4], base['lines'][4])
        self.assertNotIn('L0005', {row['line_id'] for row in result['engineering_repricing']['applied_requirements']})
        self.assertEqual(result['totals']['total_price'], '2148.50')
        with self.assertRaisesRegex(ValueError, '不能叠加'):
            pricing_plan(result, manifest)
        with self.assertRaisesRegex(ValueError, '不能叠加'):
            reprice_quote(result, manifest, result['engineering_repricing']['policy'])

    def test_public_provenance_preserves_original_quote_and_hash_without_private_keys(self):
        base, manifest = fixture()
        audit = reprice_quote(base, manifest, pricing_plan(base, manifest)['policy'])['engineering_repricing']
        self.assertNotIn('/private/do-not-export', json.dumps(audit, ensure_ascii=False))
        for row in audit['applied_requirements']:
            self.assertEqual(row['source_evidence'][0]['source_text'], row['requirement'])
            image = next(evidence for evidence in row['source_evidence'] if evidence.get('reference') == 'IMG1')
            self.assertEqual((image['quote'], image['sha256'], image['status']), (row['requirement'], 'b' * 64, 'pending_review'))
            self.assertNotIn('private_path', image)


if __name__ == '__main__':
    unittest.main()
