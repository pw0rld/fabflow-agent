"""Coverage review must not promote text to engineering truth or leak across parts."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from drawing_engineering_review import project
import test_workbench_recognition as recognition_tests


def observation(name, text, status='observed', **extra):
    return {'name': name, 'raw_text': text, 'raw_unit': None, 'status': status,
            'evidence': {'reference': 'FILE-A', 'page': 1, 'region': 'NOTES'}, **extra}


def drawing(**extra):
    return {'status': 'completed', 'partial': False, 'source_current': True,
            'observations': [], 'parts': [], 'candidates': [], **extra}


def checks(result, key=None):
    return {item['field']: item for item in result['items'] if item.get('part_key') == key}


class DrawingEngineeringReviewTests(unittest.TestCase):
    def test_visible_requirements_are_pending_evidence_not_program_or_order_count(self):
        result = project(drawing(observations=[
            observation('material_grade', '5052 H32 ALUMINUM ALLOY'),
            observation('thickness', '2 mm'),
            observation('inner_bend_radius', 'DEFAULT INSIDE BEND RADIUS 2 mm'),
            observation('engineering_open_items', '90 DOWN; Fixed Face', source_name='bend_table'),
            observation('engineering_open_items', 'SPOT WELD TAB TO BOX; 3 WELDS PER TAB; ALL 4 CORNERS', source_name='weld_note'),
            observation('surface_treatment', 'POWDER COAT BLACK'),
        ]))
        rows = checks(result)
        self.assertEqual(rows['bend_requirement']['status'], 'pending_review')
        self.assertEqual(len(rows['bend_requirement']['evidence']), 2)
        self.assertEqual(rows['weld_requirement']['evidence'][0]['raw_text'],
                         'SPOT WELD TAB TO BOX; 3 WELDS PER TAB; ALL 4 CORNERS')
        self.assertEqual(rows['quantity']['status'], 'missing')
        self.assertFalse(any(item['status'] == 'recorded' for item in result['items']))
        self.assertIn('不代表原图没有', rows['quantity']['message'])

    def test_global_material_and_quantity_never_fill_individual_parts(self):
        first = {'key': 'aluminum', 'name': '箱体', 'part_number': '1', 'version': 'A',
                 'evidence': [observation('', '')['evidence']],
                 'observations': [observation('thickness', '2 mm')]}
        second = {**first, 'key': 'steel', 'observations': [observation('material_grade', 'SS304')]}
        result = project(drawing(parts=[first, second], candidates=[
            {'name': 'material_grade', 'value': '304', 'evidence': observation('', '')['evidence']},
            {'name': 'quantity', 'value': 10, 'evidence': observation('', '')['evidence']}]))
        self.assertEqual(checks(result, 'aluminum')['material']['status'], 'missing')
        self.assertEqual(checks(result, 'steel')['thickness']['status'], 'missing')
        self.assertTrue(all(checks(result, key)['quantity']['status'] == 'missing' for key in ['aluminum', 'steel']))
        self.assertEqual(checks(result, 'aluminum')['thickness']['evidence'][0]['raw_text'], '2 mm')

    def test_incomplete_stopped_and_stale_results_do_not_declare_missing(self):
        for value in (drawing(status='running', partial=True), drawing(status='failed'),
                      drawing(status='stopped'), drawing(source_current=False), drawing(status='not_started')):
            with self.subTest(value=value):
                result = project(value)
                self.assertFalse(any(row['status'] == 'missing' for row in result['items']))
                self.assertTrue(all(row['status'] == 'not_evaluated' for row in result['items']))
        self.assertEqual(project(drawing(source_current=False))['status'], 'stale')

    def test_unassigned_conflict_is_visible_without_becoming_a_part_parameter(self):
        part = {'key': 'box', 'name': '箱体', 'observations': [], 'evidence': []}
        result = project(drawing(parts=[part], observations=[
            observation('material_grade', 'SS304 / Aluminum?', 'conflict')]))
        self.assertEqual(checks(result)['material']['status'], 'conflict')
        self.assertEqual(checks(result, 'box')['material']['status'], 'missing')
        self.assertEqual(checks(result)['quantity']['status'], 'not_evaluated')

    def test_display_evidence_is_bounded_but_late_conflict_is_not_lost(self):
        rows = [observation('thickness', f'{index} mm') for index in range(40)]
        rows[-1]['status'] = 'conflict'
        item = checks(project(drawing(observations=rows)))['thickness']
        self.assertEqual(item['status'], 'conflict')
        self.assertEqual(item['evidence_count'], 40)
        self.assertEqual(len(item['evidence']), 32)
        self.assertEqual(item['evidence'][0]['raw_text'], '39 mm')

    def test_hole_location_annotations_are_dimension_evidence(self):
        item = checks(project(drawing(observations=[observation('engineering_open_items',
                         '孔中心 X=10 mm, Y=20 mm', source_name='hole_position', label='孔位')])) )['dimensions']
        self.assertEqual(item['status'], 'pending_review')
        self.assertEqual(len(item['evidence']), 1)

    def test_conflicts_unreadable_and_unknown_units_keep_raw_sources(self):
        source = drawing(observations=[observation('thickness', '2 or 3?', 'conflict'),
                                       observation('material_grade', '5??2', 'unreadable'),
                                       observation('drawing_units', 'Dimensions?', 'unit_unknown')])
        before = deepcopy(source)
        rows = checks(project(source))
        self.assertEqual(rows['thickness']['status'], 'conflict')
        self.assertEqual(rows['material']['status'], 'unreadable')
        self.assertEqual(rows['drawing_units']['status'], 'pending_review')
        rows['thickness']['evidence'][0]['raw_text'] = 'modified view'
        self.assertEqual(source, before)

    def test_revision_difference_raises_scope_question_without_merging(self):
        evidence = observation('', '')['evidence']
        parts = [{'key': 'p1', 'part_number': 'BOX', 'version': 'A', 'evidence': [evidence], 'observations': []},
                 {'key': 'p2', 'part_number': 'BOX', 'version': 'B', 'evidence': [evidence], 'observations': []}]
        result = project(drawing(parts=parts))
        item = checks(result)['revision']
        self.assertEqual(item['status'], 'conflict')
        self.assertEqual([row['raw_text'] for row in item['evidence']], ['BOX / A', 'BOX / B'])
        self.assertEqual(len(parts), 2)
        self.assertIn('可能为不同用途', item['message'])

    def test_feature_count_and_numbers_do_not_become_dimensions_or_quantity(self):
        rows = checks(project(drawing(observations=[observation('engineering_open_items', '12 welds, 4 corners, 3 each'),
                                                    observation('engineering_open_items', '90 DOWN') ])))
        self.assertEqual(rows['dimensions']['status'], 'missing')
        self.assertEqual(rows['quantity']['status'], 'missing')
        self.assertEqual(rows['tolerance']['status'], 'not_evaluated')

    def test_mislabelled_chamfer_is_not_a_bend_and_bending_table_is_not_bom(self):
        chamfer = observation('engineering_open_items', 'TOP: 3 X45°-24Nos.', source_name='折弯标注')
        bend_table = observation('engineering_open_items',
                                 'BENDING TABLE\nTag | Direction | Angle | Inner Radius\nA | DOWN | 90° | 2',
                                 source_name='BOM表头')
        source = drawing(observations=[chamfer, bend_table])
        before = deepcopy(source)
        rows = checks(project(source))
        self.assertEqual([row['raw_text'] for row in rows['bend_requirement']['evidence']],
                         [bend_table['raw_text']])
        self.assertEqual(rows['quantity']['status'], 'missing')
        self.assertEqual(rows['technical_requirements']['evidence_count'], 2)
        self.assertEqual(source, before)

    def test_chamfer_label_does_not_hide_an_explicit_bend_requirement(self):
        rows = checks(project(drawing(observations=[observation('engineering_open_items',
                            'Bend tabs 45°; chamfer exposed edges 3 X45°', source_name='倒角') ])))
        self.assertEqual(rows['bend_requirement']['status'], 'pending_review')

    def test_sealing_note_is_still_a_technical_requirement(self):
        rows = checks(project(drawing(observations=[observation('engineering_open_items',
                            'SEAL ALLOWANCE TO BE CONFIRMED', source_name='seal_requirement')])))
        self.assertEqual(rows['technical_requirements']['status'], 'pending_review')
        self.assertEqual(rows['technical_requirements']['evidence_count'], 1)

    def test_title_signature_and_bom_do_not_hide_readable_technical_notes(self):
        source = drawing(observations=[
            observation('engineering_open_items', 'DRAWN BY: [unreadable]; DATE: [unreadable]',
                        'unreadable', source_name='技术要求'),
            observation('engineering_open_items', 'DESCRIPTION: CABINET; SHEET 1 OF 4',
                        source_name='标题栏信息'),
            observation('engineering_open_items', '未经许可不得复制，[无法辨认]',
                        'unreadable', source_name='技术要求'),
            observation('engineering_open_items', 'Grounding connection: door to frame',
                        source_name='技术要求'),
            observation('engineering_open_items', '[unreadable manufacturing note]',
                        'unreadable', source_name='技术要求'),
        ])
        before = deepcopy(source)
        item = checks(project(source))['technical_requirements']
        self.assertEqual(item['status'], 'pending_review')
        self.assertEqual(item['evidence_count'], 2)
        self.assertIn('已保留可读标注', item['message'])
        self.assertIn('部分标注尚未读清', item['message'])
        self.assertEqual(source, before)

    def test_readable_tolerance_standard_survives_unreadable_table_cells(self):
        item = checks(project(drawing(observations=[
            observation('engineering_open_items', 'General tolerances: ISO 2768-1 m', source_name='公差'),
            observation('engineering_open_items', 'Tolerance table: [unreadable values]',
                        'unreadable', source_name='公差'),
        ])))['tolerance']
        self.assertEqual(item['status'], 'pending_review')
        self.assertEqual(item['evidence_count'], 2)
        self.assertIn('已保留可读标注', item['message'])

    def test_bom_quantities_are_reviewable_row_usage_without_global_inheritance(self):
        def bom_row(item_no, text, status='observed'):
            return observation('engineering_open_items', text, status,
                               source_name='BOM明细行',
                               evidence={'reference': 'FILE-A', 'page': 1, 'region': f'BOM第{item_no}行'})

        first = bom_row(1, '1 | FRAME-1 | FRAME, RAL7035 | 1')
        second = bom_row(2, '2 | EARTH-8 | Earth strap L=300 | 4', 'unit_unknown')
        parts = [
            {'key': 'assembly', 'observations': [], 'evidence': []},
            {'key': 'bom-1', 'observations': [first], 'evidence': [first['evidence']]},
            {'key': 'bom-2', 'observations': [second], 'evidence': [second['evidence']]},
        ]
        source = drawing(parts=parts, candidates=[
            {'name': 'quantity', 'value': 10, 'evidence': first['evidence']},
            {'name': 'material_grade', 'value': 'ALUMINUM', 'evidence': first['evidence']},
        ])
        before = deepcopy(source)
        result = project(source)
        for key, quantity, item_no in [('bom-1', 1, 1), ('bom-2', 4, 2)]:
            item = checks(result, key)['quantity']
            self.assertEqual(item['status'], 'pending_review')
            self.assertIn('不是订单数量', item['message'])
            self.assertNotIn('单位或含义', item['message'])
            self.assertEqual(item['evidence'][0]['quantity'], quantity)
            self.assertEqual(item['evidence'][0]['item_no'], item_no)
            self.assertEqual(item['evidence'][0]['quantity_scope'], 'source_bom_row')
            self.assertEqual(checks(result, key)['material']['status'], 'missing')
            self.assertEqual(checks(result, key)['technical_requirements']['evidence_count'], 0)
        self.assertEqual(checks(result, 'assembly')['quantity']['status'], 'missing')
        self.assertEqual(checks(result)['quantity']['evidence'][0]['value'], 10)
        self.assertEqual(source, before)

    def test_conflicting_bom_quantity_is_not_hidden_by_readable_rows(self):
        rows = [observation('engineering_open_items', f'16 | HARDWARE | Earth strap | {quantity}',
                            source_name='BOM明细行',
                            evidence={'reference': 'FILE-A', 'page': 1, 'region': 'BOM第16行'})
                for quantity in (4, 8)]
        part = {'key': 'hardware', 'observations': rows, 'evidence': [rows[0]['evidence']]}
        item = checks(project(drawing(parts=[part])), 'hardware')['quantity']
        self.assertEqual(item['status'], 'conflict')
        self.assertEqual([row['quantity'] for row in item['evidence']], [4, 8])


class EngineeringReviewProjectionIntegrationTests(unittest.TestCase):
    def test_projection_ignores_adopted_values_and_never_writes(self):
        helper = recognition_tests.RecognitionProjectionTests()
        self.addCleanup(helper.doCleanups)
        helper.setUp()
        helper.save_human({'material_grade': '304', 'quantity': 10})
        helper.apply_run([helper.candidate('material_grade', '5052 H32')])
        before = helper.hashes()
        result = helper.recognition()['engineering_review']
        self.assertEqual(checks(result)['material']['evidence'][0]['raw_text'], '5052 H32')
        self.assertEqual(checks(result)['quantity']['status'], 'missing')
        self.assertEqual(helper.hashes(), before)
        helper.factory.assert_not_called()
