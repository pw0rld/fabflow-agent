"""Engineering presentations keep original quantities, money and evidence intact."""
import copy
import csv
from decimal import Decimal
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_engineering_bom import build_engineering_bom, render_engineering_artifact
from batch_quote_costing import aggregate_display_groups, calculate_batch, render_artifact
from test_batch_quote_costing import document, settings, solid
from test_batch_quote_presentation import example


class Tags(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.rows, self.links, self.scripts = {}, [], 0
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        for name in ('data-engineering-row', 'data-material-row', 'data-process-row'):
            if tag == 'tr' and name in values:
                self.rows.setdefault(name, []).append(values[name])
        if tag == 'a':
            self.links.append(values.get('href'))
        self.scripts += tag == 'script'


class BatchEngineeringBomTests(unittest.TestCase):
    def document_note_case(self, text):
        result = calculate_batch([document([solid(1, name='Tube')])], settings(order_sets=10))
        result['source_files'].append({'id': 'NOTE-1', 'kind': 'image', 'status': 'reference',
                                       'name': '工艺说明.png', 'sha256': 'c' * 64})
        evidence = {'reference': 'NOTE-1', 'page': 1, 'quote': text, 'region': '蓝色工程标注',
                    'basis': 'ai_document', 'status': 'pending_review'}
        recognition = {'ai': {'lines': {'L0001': {'values': {'specification': text},
            'field_states': {'specification': 'candidate'}, 'evidence': {'specification': [evidence]}}}}}
        return result, recognition

    def test_document_requirements_retain_registered_source_quotes_without_duplicates_or_financial_changes(self):
        text = '此圆管焊接固定/不能漏水漏油/316不锈钢'
        result, recognition = self.document_note_case(text)
        row = recognition['ai']['lines']['L0001']
        row['values']['material_grade'] = '316不锈钢'
        row['field_states']['material_grade'] = 'candidate'
        row['evidence']['material_grade'] = copy.deepcopy(row['evidence']['specification'])
        saved = copy.deepcopy(result)
        financial = {name: render_artifact(result, name)[0] for name in ('quote.json', 'bom.csv', 'cost.csv')}
        groups = aggregate_display_groups(result)
        bom = build_engineering_bom(result, groups, recognition=recognition)
        notes = bom['lines'][0]['document_requirements']
        self.assertEqual(notes, [{'text': text, 'reference': 'NOTE-1', 'page': 1,
                                  'region': '蓝色工程标注', 'status': 'pending_review'}])
        self.assertEqual(bom['processes'][0]['document_requirements'], notes)
        self.assertEqual(bom['processes'][0]['operations'], [])
        review = [r for r in bom['quote_basis']['requirements_review'] if r['field'] == 'document_note']
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]['status'], 'not_separately_priced')
        process_page = render_engineering_artifact(result, 'processes.html', groups, recognition=recognition)[0].decode()
        self.assertIn('原图工程标注 · 待核对', process_page)
        self.assertIn(text, process_page)
        self.assertIn('不代表已形成工序顺序', process_page)
        for change in ({'reference': 'OTHER-JOB'}, {'page': 0}, {'region': ''}, {'status': 'confirmed'}):
            with self.subTest(change=change):
                invalid = copy.deepcopy(recognition)
                for evidence in invalid['ai']['lines']['L0001']['evidence'].values():
                    evidence[0].update(change)
                self.assertEqual(build_engineering_bom(result, groups, recognition=invalid)['lines'][0]['document_requirements'], [])
        self.assertEqual(result, saved)
        self.assertEqual({name: render_artifact(result, name)[0] for name in financial}, financial)

    def test_negative_process_notes_remain_constraints_without_inventing_a_charge(self):
        for text in ('不要焊接', '无需密封', '禁止防腐'):
            with self.subTest(text=text):
                result, recognition = self.document_note_case(text)
                bom = build_engineering_bom(result, aggregate_display_groups(result), recognition=recognition)
                self.assertEqual(bom['lines'][0]['document_requirements'][0]['text'], text)
                self.assertEqual(bom['processes'][0]['operations'], [])
                self.assertFalse(any(row['field'] == 'document_note' for row in bom['quote_basis']['requirements_review']))
        for text in ('焊接固定', '不能漏水漏油', '加强防腐'):
            with self.subTest(text=text):
                result, recognition = self.document_note_case(text)
                bom = build_engineering_bom(result, aggregate_display_groups(result), recognition=recognition)
                self.assertEqual(bom['lines'][0]['document_requirements'][0]['status'], 'pending_review')
                self.assertEqual([row['status'] for row in bom['quote_basis']['requirements_review'] if row['field'] == 'document_note'],
                                 ['not_separately_priced'])

    def test_read_requirements_are_compared_with_the_saved_quote_without_changing_money(self):
        result = calculate_batch([document([solid(1, name=name)], file_id=f'F{index:04d}')
                                  for index, name in enumerate(('Tube', 'Mesh', 'Bracket'), 1)], settings(order_sets=10))
        saved = copy.deepcopy(result)
        financial = {name: render_artifact(result, name)[0] for name in ('quote.json', 'bom.csv', 'cost.csv')}
        review = {'lines': {'L0001': {'material_grade': '316不锈钢'},
                            'L0003': {'material_grade': 'Q235', 'surface_treatment': '热侵锌处理'}}}
        recognition = self.mesh_description_record()
        recognition['ai']['lines']['L0002'] = recognition['ai']['lines'].pop('L0005')
        groups = aggregate_display_groups(result)
        bom = build_engineering_bom(result, groups, review, recognition)
        rows = {(row['line_id'], row['field']): row for row in bom['quote_basis']['requirements_review']}
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows['L0001', 'material_grade']['status'], 'not_adopted')
        self.assertEqual(rows['L0002', 'material_description']['status'], 'not_adopted')
        self.assertEqual(rows['L0003', 'material_grade']['status'], 'category_matches')
        self.assertEqual(rows['L0003', 'surface_treatment']['status'], 'not_separately_priced')
        self.assertEqual(bom['quote_basis']['unresolved_requirements'], 3)
        page = render_engineering_artifact(result, 'engineering-bom.html', groups, review=review, recognition=recognition)[0].decode()
        self.assertIn('尚需落实到核价：3 项', page)
        self.assertIn('热侵锌处理', page)
        self.assertIn('原金额未自动重算', page)
        self.assertEqual(result, saved)
        self.assertEqual({name: render_artifact(result, name)[0] for name in financial}, financial)

    def test_composite_processing_cost_does_not_prove_read_process_requirements_are_included(self):
        result = calculate_batch([document([solid(1, name='Bracket')])], settings(order_sets=10))
        self.assertGreater(Decimal(result['lines'][0]['process_cost']), 0)
        result['lines'][0]['operations'] = ['焊接']
        bom = build_engineering_bom(result, aggregate_display_groups(result),
                                    {'lines': {'L0001': {'surface_treatment': '喷粉'}}})
        rows = bom['quote_basis']['requirements_review']
        self.assertEqual({row['field'] for row in rows}, {'surface_treatment', 'operations'})
        self.assertTrue(all(row['status'] == 'not_separately_priced' for row in rows))
        self.assertTrue(all('综合加工费不能证明' in row['reason'] for row in rows))
        self.assertEqual(bom['quote_basis']['unresolved_requirements'], 2)

    def test_coverage_counts_actual_fields_without_treating_revision_as_missing_specification(self):
        result = example()
        saved = copy.deepcopy(result)
        review = {'lines': {'L0005': {'specification': '100 × 50 mm', 'thickness_mm': '1.5',
                                      'material_grade': 'SUS304', 'surface_treatment': '拉丝'}}}
        bom = build_engineering_bom(result, aggregate_display_groups(result), review)
        self.assertEqual(bom['coverage']['lines'], 6)
        self.assertEqual({key: bom['coverage'][key] for key in ('specification', 'thickness_mm', 'material_grade', 'surface_treatment')},
                         {'specification': 1, 'thickness_mm': 1, 'material_grade': 1, 'surface_treatment': 1})
        self.assertEqual(bom['coverage']['formal_identity'], 0)
        self.assertEqual(bom['coverage']['parent_link'], 0)
        self.assertEqual(bom['stats']['missing_specification_lines'], 5)
        self.assertNotIn('revision', bom['lines'][4]['missing_fields'])
        self.assertNotIn('specification', bom['lines'][4]['missing_fields'])
        self.assertEqual(bom['quote_basis']['order_sets'], result['settings']['order_sets'])
        self.assertEqual(bom['quote_basis']['total_price'], result['totals']['total_price'])
        self.assertTrue(bom['quote_basis']['adopted_materials'])
        self.assertEqual(result, saved)

    def test_static_main_table_hides_empty_metadata_columns_but_retains_material_and_finish_gaps(self):
        result = example()
        groups = aggregate_display_groups(result)
        page = render_engineering_artifact(result, 'engineering-bom.html', groups)[0].decode()
        self.assertNotIn('<dt>产品编号</dt>', page)
        self.assertNotIn('<th>父项归属</th>', page)
        self.assertNotIn('<th>物料编码 / 图号</th>', page)
        self.assertIn('<th>材料</th>', page)
        self.assertIn('<th>表面处理</th>', page)
        self.assertIn('待核对汇总', page)
        self.assertIn('核价采用条件', page)
        self.assertIn('这些采用条件不是 CAD 识别牌号', page)
        self.assertIn('外购<small>名称规则候选</small>', page)
        self.assertNotIn('—<small>名称规则候选', page)
        review = {'header': {'product_number': 'ORDER-01'}, 'lines': {
            'L0005': {'part_number': 'P-005', 'parent_line_id': 'L0006'}}}
        with_ids = render_engineering_artifact(result, 'engineering-bom.html', groups, review=review)[0].decode()
        self.assertIn('<dt>产品编号</dt>', with_ids)
        self.assertIn('<th>父项归属</th>', with_ids)
        self.assertIn('<th>物料编码 / 图号</th>', with_ids)

    def test_digit_leading_cad_model_codes_are_retained_but_numeric_and_generic_names_are_not(self):
        result = example()
        for name, expected in [('5MY-DZ-01', '5MY-DZ-01'), ('123-45-67', None), ('SOLID', None)]:
            with self.subTest(name=name):
                result['lines'][4]['part_name'] = name
                row = build_engineering_bom(result, aggregate_display_groups(result))['lines'][4]
                self.assertEqual(row['cad_name_code'], expected)

    def mesh_description_record(self, *, source='ai_document', quote='焊接8目不锈钢纱网', page=1):
        return {'ai': {'lines': {'L0005': {'values': {'specification': '8目不锈钢纱网（网目规格）'},
            'field_states': {'specification': 'candidate'}, 'evidence': {'specification': [{
                'reference': 'F0007', 'page': page, 'quote': quote, 'region': '工艺说明文字',
                'basis': source, 'status': 'pending_review'}]}}}}}

    def test_literal_ai_mesh_material_description_remains_distinct_from_grade_and_pricing_assumption(self):
        result = example()
        result['lines'][4]['material_raw'] = None
        saved = copy.deepcopy(result)
        groups = aggregate_display_groups(result)
        record = self.mesh_description_record()
        bom = build_engineering_bom(result, groups, recognition=record)
        row = bom['lines'][4]
        self.assertEqual(row['material_description'], '8目不锈钢纱网')
        self.assertIsNone(row['material_grade'])
        self.assertEqual(row['material_description_evidence']['status'], 'pending_review')
        self.assertEqual(row['material_description_evidence']['references'][0]['quote'], '焊接8目不锈钢纱网')
        self.assertEqual(bom['coverage']['material_description'], 1)
        self.assertEqual(bom['coverage']['material_grade'], 0)
        self.assertEqual(next(r for r in bom['materials'] if 'L0005' in r['member_ids'])['material_description'], '8目不锈钢纱网')
        page = render_engineering_artifact(result, 'engineering-bom.html', groups, recognition=record)[0].decode()
        self.assertIn('AI 材料原文 · 牌号未标', page)
        data = list(csv.DictReader(io.StringIO(render_engineering_artifact(result, 'engineering-bom.csv', groups, recognition=record)[0].decode('utf-8-sig'))))
        self.assertEqual(data[4]['材料原文描述'], '8目不锈钢纱网')
        self.assertEqual(data[4]['工程材料牌号'], '')
        self.assertIn('焊接8目不锈钢纱网', data[4]['材料原文描述依据'])
        self.assertEqual(result, saved)

    def test_mesh_description_requires_literal_ai_evidence_and_respects_manual_grade_clear(self):
        result = example()
        groups = aggregate_display_groups(result)
        for record in [self.mesh_description_record(source='native_geometry'),
                       self.mesh_description_record(quote='材料没有写明'), self.mesh_description_record(page=0)]:
            with self.subTest(record=record):
                row = build_engineering_bom(result, groups, recognition=record)['lines'][4]
                self.assertIsNone(row['material_description'])
        for value in (None, 'SUS316'):
            with self.subTest(manual_grade=value):
                review = {'lines': {'L0005': {'material_grade': value}}}
                row = build_engineering_bom(result, groups, review, self.mesh_description_record())['lines'][4]
                self.assertIsNone(row['material_description'])
                self.assertEqual(row['material_grade'], value)

    def test_negative_mesh_notes_are_not_promoted_to_a_material_requirement(self):
        result = example()
        groups = aggregate_display_groups(result)
        for quote in ('禁止使用8目不锈钢纱网', '不得焊接8目不锈钢纱网', '8目不锈钢纱网（不采用）'):
            with self.subTest(quote=quote):
                record = self.mesh_description_record(quote=quote)
                row = build_engineering_bom(result, groups, recognition=record)['lines'][4]
                self.assertIsNone(row['material_description'])
        record = self.mesh_description_record()
        record['ai']['lines']['L0005']['values']['specification'] = '禁止使用8目不锈钢纱网'
        self.assertIsNone(build_engineering_bom(result, groups, recognition=record)['lines'][4]['material_description'])
        self.assertEqual(build_engineering_bom(result, groups, recognition=self.mesh_description_record())['lines'][4]['material_description'], '8目不锈钢纱网')

    def test_grouped_materials_keep_explicit_manual_clear_separate_from_unreviewed_sources(self):
        result = example()
        bom = build_engineering_bom(result, aggregate_display_groups(result), {'lines': {'L0001': {'material_grade': None}}})
        groups = [row for row in bom['materials'] if row['part_name'] == 'CLS-M6-2']
        self.assertEqual(len(groups), 2)
        cleared = next(row for row in groups if row['member_ids'] == ['L0001'])
        self.assertIsNone(cleared['material_grade'])
        self.assertEqual(cleared['field_sources']['material_grade'], 'manual')
        self.assertEqual(sum(row['quantity_total'] for row in groups), 340)

    def test_cad_model_code_is_a_separate_candidate_without_adopting_formal_identity(self):
        result = example()
        result['lines'][4]['part_name'] = 'DEMO-PN-31-01'
        result['lines'][5]['part_name'] = 'SOLID'
        original = copy.deepcopy(result)
        bom = build_engineering_bom(result, aggregate_display_groups(result))
        named = bom['lines'][4]
        self.assertEqual(named['cad_name_code'], 'DEMO-PN-31-01')
        self.assertEqual(named['cad_name_code_evidence']['basis'], 'cad_definition_name')
        self.assertEqual(named['cad_name_code_evidence']['native_definition'], named['native_definition'])
        self.assertEqual(named['cad_name_code_evidence']['status'], 'pending_review')
        self.assertIsNone(named['part_number'])
        self.assertIsNone(named['drawing_number'])
        self.assertIsNone(bom['lines'][5]['cad_name_code'])
        page = render_engineering_artifact(result, 'engineering-bom.html', aggregate_display_groups(result))[0].decode()
        self.assertIn('DEMO-PN-31-01</span><small>CAD名称编码</small>', page)
        self.assertNotIn('图号：—', page)
        exported = list(csv.DictReader(io.StringIO(render_engineering_artifact(result, 'engineering-bom.csv', aggregate_display_groups(result))[0].decode('utf-8-sig'))))
        self.assertEqual(exported[4]['CAD名称编码候选'], 'DEMO-PN-31-01')
        self.assertEqual(exported[4]['物料编码'], '')
        self.assertEqual(result, original)

    def test_manual_identity_and_explicit_clear_suppress_cad_name_fallback(self):
        result = example()
        result['lines'][4]['part_name'] = 'DEMO-PN-31-01'
        for manual in ({'part_number': 'P-005'}, {'drawing_number': 'DWG-005'},
                       {'part_number': None}, {'drawing_number': None}):
            with self.subTest(manual=manual):
                review = {'lines': {'L0005': manual}}
                row = build_engineering_bom(result, aggregate_display_groups(result), review)['lines'][4]
                self.assertIsNone(row['cad_name_code'])
                self.assertIsNone(row['cad_name_code_evidence'])
                self.assertEqual({key: row[key] for key in manual}, manual)

    def test_missing_thickness_explains_measurement_state_and_external_specification(self):
        result = example()
        record = {'native': {'lines': {'L0005': {'values': {}, 'field_status': {'thickness_mm': 'unresolved'},
            'evidence': {'thickness_mm': {'basis': 'native_geometry', 'status': 'unresolved',
            'reason': '原生量测超时，需重试。'}}}}}}
        page = render_engineering_artifact(result, 'engineering-bom.html', aggregate_display_groups(result), recognition=record)[0].decode()
        self.assertIn('外购按型号核对', page)
        self.assertIn('原生量测超时，需重试。', page)
        self.assertIn('板厚 / 壁厚（mm）', page)
        review = {'lines': {'L0005': {'thickness_mm': None}}}
        cleared = render_engineering_artifact(result, 'engineering-bom.html', aggregate_display_groups(result), review=review, recognition=record)[0].decode()
        self.assertIn('人工清空</span>', cleared)

    def test_engineering_identity_and_specification_are_not_guessed_from_names_or_prices(self):
        result = example()
        result['lines'][0]['material_price_snapshot'] = {
            'reference': {'grade': 'Q235B', 'thickness_mm': '4.75', 'surface': '2B'}}
        groups = aggregate_display_groups(result)
        saved = copy.deepcopy(result)
        bom = build_engineering_bom(result, groups)
        self.assertEqual(len(bom['lines']), 6)
        self.assertEqual(bom['header']['base_quantity'], 1)
        self.assertEqual(bom['header']['order_sets'], 10)
        for row in bom['lines']:
            for field in ('part_number', 'drawing_number', 'revision', 'specification', 'material_grade',
                          'thickness_mm', 'surface_treatment', 'supply_type', 'parent_line_id', 'level'):
                self.assertIsNone(row[field], (row['id'], field))
            self.assertEqual(row['unit'], 'piece')
            self.assertEqual(row['structure_status'], 'pending_link')
            self.assertEqual(row['status'], 'pending_review')
        known = next(row for row in bom['lines'] if row['part_name'] == 'Plate')
        self.assertEqual(known['cad_material_raw'], 'SS304')
        self.assertIsNone(known['material_grade'])
        self.assertEqual(result, saved)

    def test_unchanged_material_groups_preserve_all_sources_and_integer_counts(self):
        result = example()
        bom = build_engineering_bom(result, aggregate_display_groups(result))
        self.assertEqual(len(bom['materials']), 3)
        fastener = next(row for row in bom['materials'] if row['part_name'] == 'CLS-M6-2')
        self.assertEqual(fastener['member_ids'], ['L0001', 'L0002', 'L0003', 'L0004'])
        self.assertEqual(fastener['quantity_per_set'], 34)
        self.assertEqual(fastener['quantity_total'], 340)
        self.assertIs(type(fastener['quantity_total']), int)
        self.assertEqual(sorted(member for row in bom['materials'] for member in row['member_ids']),
                         sorted(row['id'] for row in result['lines']))
        self.assertEqual(sum(row['quantity_total'] for row in bom['materials']),
                         sum(row['quantity_total'] for row in result['lines']))

    def test_manual_metadata_remains_pending_and_does_not_rebase_quantities_or_prices(self):
        result = example()
        groups = aggregate_display_groups(result)
        original = copy.deepcopy(result)
        raw = {name: render_artifact(result, name)[0] for name in ('quote.json', 'bom.csv', 'cost.csv')}
        review = {'revision': 2, 'header': {'product_name': '箱体', 'product_number': 'CAB-01',
                  'bom_revision': 'A', 'base_quantity': 2, 'basis_notes': '范围待复核'},
                  'lines': {'L0005': {'part_number': 'P-005', 'drawing_number': 'DWG-005',
                     'revision': 'B', 'material_grade': 'SUS304', 'thickness_mm': '1.5',
                     'surface_treatment': '拉丝', 'specification': '100 × 50', 'supply_type': 'make',
                     'parent_line_id': 'L0006', 'unit': 'kg'}}}
        saved_review = copy.deepcopy(review)
        bom = build_engineering_bom(result, groups, review)
        row = next(row for row in bom['lines'] if row['id'] == 'L0005')
        self.assertEqual(row['part_number'], 'P-005')
        self.assertEqual(row['field_sources']['material_grade'], 'manual')
        self.assertEqual(row['status'], 'pending_review')
        self.assertEqual(row['structure_status'], 'manual_link')
        self.assertEqual(row['unit'], 'piece')
        self.assertEqual(row['quantity_per_set'], result['lines'][4]['quantity_per_set'])
        self.assertEqual(row['quantity_total'], result['lines'][4]['quantity_total'])
        self.assertEqual(bom['header']['base_quantity'], 1)
        self.assertEqual(bom['header']['review_revision'], 2)
        self.assertEqual(result, original)
        self.assertEqual(review, saved_review)
        self.assertEqual({name: render_artifact(result, name)[0] for name in raw}, raw)

    def test_review_differences_split_previously_grouped_requirements_without_losing_demand(self):
        result = example()
        groups = aggregate_display_groups(result)
        review = {'lines': {'L0001': {'material_grade': 'SUS304'}}}
        bom = build_engineering_bom(result, groups, review)
        fasteners = [row for row in bom['materials'] if row['part_name'] == 'CLS-M6-2']
        self.assertEqual(len(fasteners), 2)
        self.assertEqual([row['quantity_total'] for row in fasteners], [180, 160])
        self.assertEqual([row['material_grade'] for row in fasteners], ['SUS304', None])
        self.assertEqual(sorted(member for row in fasteners for member in row['member_ids']),
                         ['L0001', 'L0002', 'L0003', 'L0004'])

    def test_parent_cycles_or_unknown_links_never_invent_a_valid_structure(self):
        result = example()
        for overrides in ({'L0001': {'parent_line_id': 'L0001'}},
                          {'L0001': {'parent_line_id': 'L0002'}, 'L0002': {'parent_line_id': 'L0001'}},
                          {'L0001': {'parent_line_id': 'L9999'}}):
            with self.subTest(overrides=overrides):
                bom = build_engineering_bom(result, aggregate_display_groups(result), {'lines': overrides})
                row = bom['lines'][0]
                self.assertEqual(row['structure_status'], 'invalid_link')
                self.assertIsNone(row['parent_line_id'])
                self.assertIsNone(row['level'])

    def test_net_weight_and_purchase_usage_have_separate_semantics(self):
        result = example()
        bom = build_engineering_bom(result, aggregate_display_groups(result))
        original = next(row for row in result['lines'] if row['part_name'] == 'Plate')
        plate = next(row for row in bom['materials'] if row['part_name'] == 'Plate')
        self.assertEqual(plate['purchase_usage_kg_total'],
                         format(Decimal(original['material_usage_kg']) * original['quantity_total'], 'f'))
        self.assertNotEqual(plate['estimated_total_net_weight_kg'], plate['purchase_usage_kg_total'])
        self.assertEqual(plate['purchase_usage_status'], 'estimated')
        fastener = next(row for row in bom['materials'] if row['part_name'] == 'CLS-M6-2')
        self.assertIsNone(fastener['purchase_usage_kg_total'])
        self.assertEqual(fastener['purchase_usage_status'], 'not_applicable')

    def test_processes_only_use_explicit_saved_operations(self):
        result = example()
        result['lines'][0]['notes'] = ['激光下料、折弯、焊接可能需要']
        result['lines'][4]['operations'] = ['激光下料', {'operation': '折弯', 'notes': '角度待核对'}]
        bom = build_engineering_bom(result, aggregate_display_groups(result))
        self.assertEqual(bom['processes'][0]['operations'], [])
        self.assertEqual(bom['processes'][0]['status'], 'pending')
        saved = bom['processes'][4]
        self.assertEqual([row['operation'] for row in saved['operations']], ['激光下料', '折弯'])
        self.assertEqual(saved['operations'][1]['notes'], '角度待核对')
        self.assertEqual(saved['status'], 'pending_review')

    def test_html_views_have_all_rows_no_prices_and_safe_fixed_navigation(self):
        result = example()
        result['lines'][0]['part_name'] = '<script>name</script>'
        result['name'] = '</header><img src=x>'
        groups = aggregate_display_groups(result)
        for name, attr, expected in [('engineering-bom.html', 'data-engineering-row', 6),
                                      ('materials.html', 'data-material-row', len(groups)),
                                      ('processes.html', 'data-process-row', 6)]:
            with self.subTest(name=name):
                content, mime = render_engineering_artifact(result, name, groups)
                page, tags = content.decode(), Tags(content.decode())
                self.assertEqual(mime, 'text/html; charset=utf-8')
                self.assertEqual(tags.scripts, 0)
                self.assertEqual(len(tags.rows[attr]), expected)
                self.assertIn('data-engineering-artifact="' + name + '"', page)
                self.assertIn('工程 BOM', page)
                self.assertIn('工程补录和父项归属不修改原报价数量、重量或金额。', page)
                self.assertNotIn('<script>name</script>', page)
                self.assertNotIn('<img src=x>', page)
                self.assertIn('engineering-bom.csv', tags.links)
                self.assertIn('quote.html', tags.links)
                self.assertNotIn('报价金额</th>', page)
                self.assertNotIn('单价</th>', page)
                self.assertNotIn(result['totals']['total_price'], page)

    def test_csv_preserves_precision_and_never_promotes_a_row_id_to_a_part_number(self):
        result = example()
        result['lines'][0]['part_name'] = '=HYPERLINK("unsafe")'
        content, mime = render_engineering_artifact(result, 'engineering-bom.csv', aggregate_display_groups(result))
        rows = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
        self.assertEqual(mime, 'text/csv; charset=utf-8')
        self.assertEqual(len(rows), len(result['lines']))
        self.assertEqual(rows[0]['行号'], 'L0001')
        self.assertEqual(rows[0]['物料编码'], '')
        self.assertEqual(rows[0]['图号'], '')
        self.assertTrue(rows[0]['名称'].startswith("'="))
        self.assertEqual(rows[0]['单位'], '件')
        self.assertEqual(rows[0]['单件预估净重_kg'], result['lines'][0]['estimated_net_weight_kg'])
        self.assertEqual(rows[0]['原报价订单数量'], str(result['lines'][0]['quantity_total']))

    def test_preview_geometry_urls_are_restricted_and_do_not_become_arbitrary_links(self):
        result = example()
        groups = aggregate_display_groups(result)
        previews = [{'line_id': 'L0001', 'geometry_url': 'https://tracker.invalid/part'},
                    {'line_id': 'L0002', 'geometry_url': '/workbench/api/quote-batches/BATCH-a/runs/RUN-a/parts/L0002'}]
        page = render_engineering_artifact(result, 'engineering-bom.html', groups, previews)[0].decode()
        self.assertNotIn('tracker.invalid', page)
        self.assertIn('/workbench/quotation/part/?preview=%2Fworkbench%2Fapi', page)
        with self.assertRaises(ValueError):
            render_engineering_artifact(result, '../quote.json', groups)


if __name__ == '__main__':
    unittest.main()
