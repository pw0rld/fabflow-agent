"""Native engineering candidates require actual identity and measurement evidence."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_engineering_native import enrich_native
from batch_quote_costing import calculate_batch
from test_batch_quote_costing import document, settings, solid


def material(name='SS304', scope='definition'):
    return {'status': 'candidate', 'value': name, 'entries': [{
        'name': name, 'description': '', 'material_label': '0:1:2',
        'assignment_label': '0:1:1', 'scope': scope}],
        'reason': '实际 CAD 标签赋值', 'warnings': []}


def thickness(value=1):
    return {'status': 'candidate', 'value_mm': value, 'method': 'parallel_faces',
        'confidence': 'high', 'reason': '成对平面量测', 'warnings': [],
        'evidence': [{'kind': 'parallel_faces', 'face_ids': ['F0001', 'F0002'],
                      'thickness_mm': value, 'support_area_mm2': 5000}]}


def body(index, path=None, name='Plate', offset=0):
    item = solid(index, 'plate/solid-1', path or ['root', str(index)], volume=5000, name=name)
    item['bounds_mm'] = {'min': [offset, 0, 0], 'max': [offset + 100, 50, 1], 'size': [100, 50, 1]}
    item['thickness'] = thickness()
    item['topology']['faces'] = 6
    item['material'] = material()
    return item


def case(bodies=None):
    doc = document(bodies or [body(1), body(2, offset=200)])
    result = calculate_batch([doc], settings(order_sets=10))
    return result, {doc['id']: doc['geometry']}


class NativeEngineeringTests(unittest.TestCase):
    def test_unfinished_measurement_retains_its_actual_reason_and_warnings(self):
        bodies = [body(1)]
        bodies[0]['thickness'] = {'status': 'unresolved', 'value_mm': None, 'method': 'none',
            'confidence': 'none', 'reason': '板厚分析预算已用完', 'warnings': ['未采用局部结论'], 'evidence': []}
        result, geometry = case(bodies)
        row = enrich_native(result, geometry)['lines']['L0001']
        self.assertIsNone(row['values']['thickness_mm'])
        self.assertEqual(row['evidence']['thickness_mm']['reason'], '板厚分析预算已用完')
        self.assertEqual(row['evidence']['thickness_mm']['samples'][0]['warnings'], ['未采用局部结论'])

    def test_matching_measurements_produce_evidence_bound_pending_candidates(self):
        result, geometry = case()
        original, original_geometry = copy.deepcopy(result), copy.deepcopy(geometry)
        output = enrich_native(result, geometry)
        row = output['lines']['L0001']
        self.assertEqual(row['values']['thickness_mm'], '1')
        self.assertEqual(row['values']['material_grade'], 'SS304')
        self.assertEqual(row['values']['cad_material_raw'], 'SS304')
        self.assertIn('100.00 × 50.00 × 1.00 mm', row['values']['specification'])
        self.assertIn('非展开尺寸', row['values']['specification'])
        self.assertEqual(row['evidence']['specification']['size_mm'], ['100', '50', '1'])
        self.assertEqual(row['evidence']['specification']['occurrence_path'], ['root', '1'])
        self.assertEqual(row['evidence']['thickness_mm']['source_sha256'], 'a' * 64)
        self.assertEqual(row['evidence']['thickness_mm']['solid_ids'], ['S1', 'S2'])
        self.assertEqual(row['status'], 'pending_review')
        self.assertIsNone(row['values']['surface_treatment'])
        self.assertEqual(output['stats']['thickness_mm_candidates'], 1)
        self.assertEqual(result, original)
        self.assertEqual(geometry, original_geometry)
        json.dumps(output, allow_nan=False)

    def test_shortest_bound_and_price_sample_never_supply_thickness_or_grade(self):
        bodies = [body(1)]
        bodies[0].pop('thickness')
        bodies[0]['material'] = {'status': 'unresolved', 'value': None, 'entries': [],
                                'reason': '未赋材料', 'warnings': []}
        result, geometry = case(bodies)
        result['lines'][0]['material_price_snapshot'] = {'reference': {'thickness_mm': '4.75', 'grade': 'Q235B', 'surface': '镀锌'}}
        row = enrich_native(result, geometry)['lines']['L0001']
        self.assertIsNone(row['values']['thickness_mm'])
        self.assertIsNone(row['values']['material_grade'])
        self.assertIsNone(row['values']['surface_treatment'])
        self.assertEqual(row['field_status']['thickness_mm'], 'unresolved')
        self.assertIn('1.00 mm', row['values']['specification'])

    def test_disagreeing_and_incomplete_thickness_remain_unresolved_or_conflicted(self):
        for second, expected in [(thickness(.8), 'conflict'), (None, 'unresolved')]:
            with self.subTest(expected=expected):
                bodies = [body(1), body(2)]
                bodies[1]['thickness'] = second
                result, geometry = case(bodies)
                row = enrich_native(result, geometry)['lines']['L0001']
                self.assertIsNone(row['values']['thickness_mm'])
                self.assertEqual(row['field_status']['thickness_mm'], expected)

    def test_unsubstantiated_or_invalid_face_measurements_are_not_candidates(self):
        bad = []
        for edit in ({'evidence': []}, {'method': 'none'}, {'confidence': 'none'}, {'value_mm': -1}):
            bad.append({**thickness(), **edit})
        wrong_face = thickness()
        wrong_face['evidence'][0]['face_ids'] = ['F0001', 'F9999']
        bad.append(wrong_face)
        for candidate in bad:
            with self.subTest(candidate=candidate):
                bodies = [body(1)]
                bodies[0]['thickness'] = candidate
                result, geometry = case(bodies)
                row = enrich_native(result, geometry)['lines']['L0001']
                self.assertIsNone(row['values']['thickness_mm'])
                self.assertEqual(row['field_status']['thickness_mm'], 'unresolved')

    def test_material_requires_all_explicit_matching_assignments(self):
        for candidate, expected in [(material('SS316'), 'conflict'), (None, 'unresolved'),
                                     (material('SS304', 'inherited'), 'unresolved')]:
            with self.subTest(expected=expected, material=candidate):
                bodies = [body(1), body(2)]
                bodies[1]['material'] = candidate
                result, geometry = case(bodies)
                row = enrich_native(result, geometry)['lines']['L0001']
                self.assertIsNone(row['values']['material_grade'])
                self.assertEqual(row['field_status']['material_grade'], expected)

    def test_source_units_hash_and_original_group_counts_must_match(self):
        for change in ('units', 'hash', 'solid_count', 'definition', 'volume', 'quantity'):
            with self.subTest(change=change):
                result, geometry = case()
                if change == 'units': geometry['F001']['output_unit'] = 'inch'
                if change == 'hash': geometry['F001']['source_sha256'] = 'b' * 64
                if change == 'solid_count': result['lines'][0]['native_solid_count'] += 1
                if change == 'definition': result['lines'][0]['native_definition'] = 'other'
                if change == 'volume': result['lines'][0]['volume_mm3'] = '1'
                if change == 'quantity': result['lines'][0]['quantity_per_set'] += 1
                row = enrich_native(result, geometry)['lines']['L0001']
                self.assertTrue(row['notes'])
                self.assertTrue(all(value is None for value in row['values'].values()))

    def test_ambiguous_quote_rows_are_not_matched_by_name(self):
        result, geometry = case()
        second = copy.deepcopy(result['lines'][0])
        second['id'] = 'L0002'
        result['lines'].append(second)
        output = enrich_native(result, geometry)
        for row in output['lines'].values():
            self.assertIsNone(row['values']['specification'])
            self.assertIsNone(row['values']['thickness_mm'])
            self.assertTrue(row['notes'])

    def test_composite_part_envelope_uses_one_real_instance_not_every_placement(self):
        bodies = []
        for instance, offset in [('first', -100), ('second', 300)]:
            for component in (1, 2):
                row = body(len(bodies) + 1, ['root', instance], offset=offset)
                row['definition_id'] = 'plate/solid-' + str(component)
                row['volume_mm3'] = 2500
                low = offset + (component - 1) * 50
                row['bounds_mm'] = {'min': [low, 0, 0], 'max': [low + 50, 50, 1], 'size': [50, 50, 1]}
                bodies.append(row)
        result, geometry = case(bodies)
        output = enrich_native(result, geometry)['lines']['L0001']
        self.assertEqual(output['evidence']['specification']['size_mm'], ['100', '50', '1'])
        self.assertEqual(len(output['evidence']['specification']['solid_ids']), 2)
        self.assertEqual(output['values']['thickness_mm'], '1')

    def test_standard_part_name_is_preserved_as_named_specification_not_decoded(self):
        result, geometry = case([body(1, name='CLS-M6-2')])
        row = enrich_native(result, geometry)['lines']['L0001']
        self.assertIn('CLS-M6-2（CAD名称规格）', row['values']['specification'])
        self.assertIn('成品包络', row['values']['specification'])
        self.assertIsNone(row['values']['surface_treatment'])

    def test_preview_context_wrapper_and_missing_geometry_are_explicit(self):
        result, geometry = case()
        wrapped = {'F001': {'geometry': geometry['F001'], 'source_sha256': 'a' * 64}}
        self.assertEqual(enrich_native(result, wrapped)['stats']['thickness_mm_candidates'], 1)
        row = enrich_native(result, {})['lines']['L0001']
        self.assertTrue(row['notes'])
        self.assertEqual(row['field_status']['thickness_mm'], 'unresolved')
        self.assertTrue(all(value is None for value in row['values'].values()))


if __name__ == '__main__':
    unittest.main()
