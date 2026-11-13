"""Assignment-backed raw material versus explicit grade; ZeroClave execution only.

These tests use preserved XCAF-shaped scalar entries and a reader boundary
double. They do not run a CAD kernel, model, upload or production quotation.
"""
import copy
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from cad_material import MaterialReader, is_placeholder_material_name, material_grade_candidate
from cad_bom import validate_material
from batch_engineering_native import enrich_native
from test_batch_engineering_native import body, case


def material(name='SS304', description='', *, status='candidate', scope='definition', warnings=None):
    return {'status': status, 'value': name if status == 'candidate' else None,
            'entries': [{'name': name, 'description': description, 'scope': scope,
                         'assignment_label': '0:1:1', 'material_label': '0:1:2'}],
            'reason': 'Synthetic actual assignment boundary', 'warnings': warnings or []}


def reader_result(name, description=''):
    """Exercise MaterialReader.read's real decision branch, with no OCP imports."""
    reader = object.__new__(MaterialReader)
    reader._entry = lambda label: label
    reader._direct = lambda label: ([{'name': name, 'description': description,
                                     'assignment_label': label, 'material_label': 'source-material'}], [])
    reader._children = lambda label: ([], False)
    return reader.read('definition', 'definition', object())


class MaterialGradeTests(unittest.TestCase):
    def test_template_names_remain_verbatim_but_cannot_be_material_candidates(self):
        for name in ('COMPANY', 'Company', '设计单位', 'PTC_MASTER_MATERIAL', 'PTC_SYSTEM_MTRL_PROPS'):
            with self.subTest(name=name):
                self.assertTrue(is_placeholder_material_name(name))
                result = reader_result(name)
                self.assertEqual(result['status'], 'unresolved'); self.assertIsNone(result['value'])
                self.assertEqual(result['entries'][0]['name'], name); self.assertIn('模板', result['reason'])
                validate_material(result)
                self.assertEqual(material_grade_candidate(result)['status'], 'unresolved')

    def test_generic_material_stays_raw_without_inventing_a_specific_grade(self):
        for name in ('Steel', 'Stainless Steel', '不锈钢：牌号未注明', 'Copper'):
            with self.subTest(name=name):
                raw = reader_result(name)
                self.assertEqual(raw['status'], 'candidate'); self.assertEqual(raw['value'], name)
                grade = material_grade_candidate(raw)
                self.assertEqual(grade['status'], 'unresolved'); self.assertIsNone(grade['value'])

    def test_explicit_grade_names_are_extracted_with_original_source_labels(self):
        for name in ('SS304', '304SS', 'SUS316L', '316L SS', 'Q235B', 'SPCC', '5052 H32', 'SS430'):
            with self.subTest(name=name):
                raw = material(name); before = copy.deepcopy(raw)
                grade = material_grade_candidate(raw)
                self.assertEqual(grade['status'], 'candidate'); self.assertEqual(grade['value'], name)
                self.assertEqual(grade['evidence'][0]['field'], 'name')
                self.assertEqual(grade['evidence'][0]['text'], name)
                self.assertEqual(grade['evidence'][0]['assignment_label'], '0:1:1')
                self.assertEqual(raw, before)

    def test_description_grade_is_separate_from_template_or_generic_raw_name(self):
        for name in ('COMPANY', 'PTC_MASTER_MATERIAL', 'Stainless Steel', ''):
            with self.subTest(name=name):
                raw = reader_result(name, 'Material grade: SUS304; original supplier attribute')
                before = copy.deepcopy(raw); grade = material_grade_candidate(raw)
                self.assertEqual(grade['status'], 'candidate'); self.assertEqual(grade['value'], 'SUS304')
                self.assertEqual(grade['evidence'][0]['field'], 'description')
                self.assertEqual(grade['evidence'][0]['text'], raw['entries'][0]['description'])
                self.assertEqual(raw['entries'][0]['name'], name); self.assertEqual(raw, before)
        for description in ('材质：304', 'Material grade: 316L'):
            grade = material_grade_candidate(material('COMPANY', description, status='unresolved'))
            self.assertEqual(grade['status'], 'candidate')
            self.assertEqual(grade['evidence'][0]['field'], 'description')

    def test_ambiguous_name_description_or_inherited_grades_do_not_pick_first(self):
        cases = [material('SS304 / SS316'), material('SS304', 'Grade: SUS316'),
                 material('Stainless Steel', '304 / 316 stainless steel')]
        inherited = material('SS304', status='unresolved')
        inherited['entries'].append({**inherited['entries'][0], 'name': 'SS316', 'scope': 'inherited',
                                     'assignment_label': 'assembly-parent'})
        cases.append(inherited)
        for raw in cases:
            with self.subTest(raw=raw):
                grade = material_grade_candidate(raw)
                self.assertEqual(grade['status'], 'conflict'); self.assertIsNone(grade['value'])
                self.assertGreaterEqual(len(grade['evidence']), 2)

    def test_inherited_partial_and_incomplete_sources_cannot_supply_whole_solid_grade(self):
        cases = [material('SS304', status='unresolved', scope='inherited'),
                 material('COMPANY', 'SUS304', status='unresolved', scope='subshape'),
                 material('COMPANY', 'SUS304', status='unresolved', warnings=['读取预算已用完'])]
        for raw in cases:
            with self.subTest(raw=raw):
                grade = material_grade_candidate(raw)
                self.assertEqual(grade['status'], 'unresolved'); self.assertIsNone(grade['value'])
        # Existing candidate proves an owned complete subshape assignment;
        # unlike unresolved partial evidence it can retain its explicit grade.
        self.assertEqual(material_grade_candidate(material('SS304', scope='subshape'))['value'], 'SS304')

    def test_dimensions_density_and_family_words_do_not_supply_a_grade(self):
        for description in ('Thickness 304 mm; density 7850', '304.00 mm x 316.00 mm',
                            'Stainless steel, grade unspecified; thickness 1.2 mm', '设计单位：COMPANY'):
            with self.subTest(description=description):
                grade = material_grade_candidate(material('COMPANY', description, status='unresolved'))
                self.assertEqual(grade['status'], 'unresolved'); self.assertIsNone(grade['value'])


class NativeMaterialGradeTests(unittest.TestCase):
    def test_old_cached_template_candidate_keeps_raw_observation_but_no_grade(self):
        for name in ('COMPANY', 'PTC_MASTER_MATERIAL', 'PTC_SYSTEM_MTRL_PROPS'):
            with self.subTest(name=name):
                item = body(1); item['material'] = material(name)
                value, geometry = case([item]); before = copy.deepcopy((value, geometry))
                row = enrich_native(value, geometry)['lines']['L0001']
                self.assertIsNone(row['values']['material_grade'])
                self.assertEqual(row['field_status']['material_grade'], 'unresolved')
                self.assertEqual(row['values']['cad_material_raw'], name)
                self.assertEqual(row['field_status']['cad_material_raw'], 'observed')
                sample = row['evidence']['cad_material_raw']['samples'][0]
                self.assertEqual(sample['entries'][0]['name'], name)
                self.assertEqual((value, geometry), before)

    def test_description_grade_is_source_bound_and_does_not_rewrite_original_raw_name(self):
        item = body(1); item['material'] = material('COMPANY', 'Material: SUS316L', status='unresolved')
        value, geometry = case([item]); original = copy.deepcopy(value)
        row = enrich_native(value, geometry)['lines']['L0001']
        self.assertEqual(row['values']['material_grade'], 'SUS316L')
        self.assertEqual(row['values']['cad_material_raw'], 'COMPANY')
        self.assertEqual(row['field_status']['material_grade'], 'candidate')
        proof = row['evidence']['material_grade']
        self.assertEqual(proof['source_sha256'], 'a'*64)
        evidence = proof['samples'][0]['grade_evidence'][0]
        self.assertEqual(evidence['field'], 'description'); self.assertEqual(evidence['text'], 'Material: SUS316L')
        self.assertEqual(value, original)

    def test_group_requires_effective_grade_for_every_instance_and_preserves_conflicts(self):
        for other, status in ((material('COMPANY', status='unresolved'), 'unresolved'),
                              (material('SS316'), 'conflict')):
            with self.subTest(status=status):
                first, second = body(1), body(2)
                first['material'] = material('SS304'); second['material'] = other
                value, geometry = case([first, second]); row = enrich_native(value, geometry)['lines']['L0001']
                self.assertEqual(row['field_status']['material_grade'], status)
                self.assertIsNone(row['values']['material_grade'])
                self.assertEqual(len(row['evidence']['material_grade']['samples']), 2)

    def test_material_filename_and_part_name_do_not_fill_missing_grade(self):
        item = body(1, name='不锈钢组件304'); item['material'] = material('COMPANY', status='unresolved')
        value, geometry = case([item]); value['source_files'][0]['name'] = '不锈钢组件示例.stp'
        row = enrich_native(value, geometry)['lines']['L0001']
        self.assertIsNone(row['values']['material_grade'])
        self.assertEqual(row['values']['cad_material_raw'], 'COMPANY')


if __name__ == '__main__': unittest.main()
