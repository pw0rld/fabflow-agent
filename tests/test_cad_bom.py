"""Source and unknown-value boundaries for draft CAD material tables."""
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cad_bom import (group_material_summary, group_quantity_summary, solid_identity,
                     total_quantity, validate_bom_settings, validate_material)
from prep_record import RecordError


def material(name='Steel', label='0:1:5:1'):
    return {'status': 'candidate', 'value': name, 'entries': [
        {'name': name, 'description': 'Original CAD value', 'material_label': label,
         'assignment_label': '0:1:1:2', 'scope': 'definition'}],
        'reason': 'CAD 原始材质属性；牌号待核对。', 'warnings': []}


def group(ids=None, kind='make'):
    return {'classification': kind, 'solid_ids': ids or ['S0001', 'S0002']}


def identities(definition='0:1:1:2/solid-1'):
    return {'S0001': {'definition_id': definition, 'occurrence_path': ['0:1:1:1', '0:1:1:1:1']},
            'S0002': {'definition_id': definition, 'occurrence_path': ['0:1:1:1', '0:1:1:1:2']}}


class MaterialSourceTests(unittest.TestCase):
    def test_original_generic_material_stays_generic_and_preserves_assignment(self):
        data = material()
        self.assertEqual(validate_material(data), data)
        result = group_material_summary(group(), {'S0001': data, 'S0002': material()})
        self.assertEqual(result['value'], 'Steel')
        self.assertIn('0:1:1:2', result['evidence_text'])
        for field in ('value', 'reason', 'evidence_text'):
            self.assertNotIn('Q235', result[field])

    def test_material_cannot_claim_a_name_absent_from_source_or_drop_source(self):
        for change in ({'value': 'Q235B'}, {'entries': []}, {'status': 'approved'},
                       {'value': None}, {'extra': True}, {'reason': ''}):
            data = material(); data.update(change)
            with self.subTest(change=change), self.assertRaises(RecordError):
                validate_material(data)
        for change in ({'scope': 'guessed'}, {'assignment_label': ''}, {'name': ''}):
            data = material(); data['entries'][0].update(change)
            with self.subTest(change=change), self.assertRaises(RecordError):
                validate_material(data)

    def test_conflict_and_partial_materials_never_pick_first(self):
        mixed = {'S0001': material(), 'S0002': material('Aluminium')}
        self.assertEqual(group_material_summary(group(), mixed)['status'], 'conflict')
        mixed['S0002'] = {'status': 'unresolved', 'value': None, 'entries': [],
                          'reason': '没有材料赋值', 'warnings': []}
        self.assertEqual(validate_material(mixed['S0002']), mixed['S0002'])
        self.assertEqual(group_material_summary(group(), mixed)['status'], 'unresolved')
        mixed.pop('S0002')
        self.assertIsNone(group_material_summary(group(), mixed)['value'])
        self.assertEqual(group_material_summary(group(), {})['status'], 'not_read')

    def test_inherited_only_material_cannot_be_claimed_as_direct_assignment(self):
        value = material()
        value['entries'][0]['scope'] = 'inherited'
        with self.assertRaises(RecordError):
            validate_material(value)
        value['entries'].append(material()['entries'][0])
        self.assertEqual(validate_material(value), value)

    def test_unresolved_empty_material_name_retains_source_but_cannot_be_adopted(self):
        value = material()
        value.update(status='unresolved', value=None, reason='原赋值只有描述或密度，未提供材料名称。')
        value['entries'][0]['name'] = ''
        self.assertEqual(validate_material(value), value)
        self.assertEqual(value['entries'][0]['assignment_label'], '0:1:1:2')
        self.assertIsNone(group_material_summary(group(['S0001']), {'S0001': value})['value'])
        for name in ('', 'Steel'):
            candidate = copy.deepcopy(value)
            candidate.update(status='candidate', value=name)
            with self.subTest(name=name), self.assertRaises(RecordError):
                validate_material(candidate)


class InstanceQuantityTests(unittest.TestCase):
    def test_distinct_original_occurrences_of_one_definition_count_once_each(self):
        sources = identities()
        result = group_quantity_summary(group(), sources)
        self.assertEqual(result['status'], 'candidate')
        self.assertEqual(result['value'], 2)
        self.assertEqual(len(set(result['occurrence_ids'])), 2)
        self.assertEqual(result['solid_ids'], ['S0001', 'S0002'])
        self.assertIn('仍需', result['reason'])

    def test_multiple_bodies_of_one_component_are_not_part_quantity(self):
        sources = identities()
        sources['S0002']['definition_id'] = '0:1:1:2/solid-2'
        sources['S0002']['occurrence_path'] = sources['S0001']['occurrence_path'][:]
        result = group_quantity_summary(group(), sources)
        self.assertEqual(result['status'], 'unresolved')
        self.assertIsNone(result['value'])

    def test_duplicate_aliases_and_missing_identity_do_not_count_as_instances(self):
        sources = identities()
        sources['S0002']['occurrence_path'] = sources['S0001']['occurrence_path'][:]
        self.assertEqual(group_quantity_summary(group(), sources)['status'], 'unresolved')
        self.assertEqual(group_quantity_summary(group(['S0001']), sources)['status'], 'unresolved')
        for key, value in [('definition_id', None), ('definition_id', ''), ('occurrence_path', None),
                           ('occurrence_path', [])]:
            sources = identities(); sources['S0001'][key] = value
            with self.subTest(key=key, value=value):
                self.assertIsNone(group_quantity_summary(group(), sources)['value'])

    def test_assembly_design_and_excluded_rows_do_not_become_counts(self):
        for kind in ('assembly', 'needs_design', 'unclassified', 'exclude'):
            result = group_quantity_summary(group(kind=kind), identities())
            self.assertIsNone(result['value'])
            self.assertEqual(result['status'], 'not_applicable' if kind == 'exclude' else 'unresolved')
        self.assertEqual(group_quantity_summary(group(kind='buy'), identities())['value'], 2)

    def test_source_path_requires_real_bounded_segments(self):
        data = identities()['S0001']
        self.assertEqual(solid_identity(data), data)
        for value in ('invented/string', [False], [{}], [''], ['x'] * 129):
            changed = copy.deepcopy(data); changed['occurrence_path'] = value
            with self.subTest(value=value), self.assertRaises(RecordError):
                solid_identity(changed)


class BomQuantityTests(unittest.TestCase):
    def test_unknown_table_sets_and_per_set_quantity_stay_unknown(self):
        self.assertEqual(validate_bom_settings({'order_sets': None}), {'order_sets': None})
        self.assertIsNone(total_quantity(2, None))
        self.assertIsNone(total_quantity(None, 3))
        self.assertEqual(total_quantity(2, 3), 6)

    def test_table_sets_accept_only_supported_positive_integer_or_null(self):
        for count in (True, False, 0, -1, 1.5, float('nan'), '2', 1000001):
            with self.subTest(count=count), self.assertRaises(RecordError):
                validate_bom_settings({'order_sets': count})
        for value in ({}, {'order_sets': 2, 'price': 1}):
            with self.subTest(value=value), self.assertRaises(RecordError):
                validate_bom_settings(value)
        self.assertEqual(validate_bom_settings({'order_sets': 1000000})['order_sets'], 1000000)


if __name__ == '__main__':
    unittest.main()
