"""Bounded bend evidence tests; synthetic bands are protocol fixtures."""
import copy
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cad_bend_contract import validate_bend_profile
from prep_record import RecordError

BOUNDS = {'min': [0, 0, 0], 'max': [3, 3, 4], 'size': [3, 3, 4]}


def bend_profile():
    positions = []
    for radius in (1, 2):
        for angle in (0, math.pi/4, math.pi/2):
            for level in (0, 4):
                positions.extend([radius*math.cos(angle), radius*math.sin(angle), level])
    return {'status': 'candidate', 'reason': '同轴内外圆柱与相邻平面形成真实折弯候选。',
            'unit': 'mm', 'warnings': ['工程审核草稿，不代表折弯程序。'], 'bends': [{
                'id': 'B0001', 'status': 'pending_review',
                'inner_face_ids': ['F0001'], 'outer_face_ids': ['F0002'],
                'flange_face_ids': [['F0003', 'F0004'], ['F0005', 'F0006']],
                'axis': {'point': [0, 0, 0], 'direction': [0, 0, 1]},
                'inner_radius_mm': 1, 'outer_radius_mm': 2, 'thickness_mm': 1,
                'rotation_angle_deg': 90, 'included_angle_deg': 90, 'length_mm': 4,
                'tangent_lines': [{'start': [2, 0, 0], 'end': [2, 0, 4]},
                                  {'start': [0, 2, 0], 'end': [0, 2, 4]}],
                'mesh': {'positions': positions,
                         'indices': [0, 1, 2, 1, 3, 2, 2, 3, 4, 3, 5, 4,
                                     6, 7, 8, 7, 9, 8, 8, 9, 10, 9, 11, 10]},
                'direction': 'unconfirmed', 'reference_face_id': None, 'warnings': []}]}


def no_bends(status='not_applicable'):
    return {'status': status, 'reason': '未得到有完整几何依据的折弯候选。',
            'unit': 'mm', 'bends': [], 'warnings': []}


class BendContractTests(unittest.TestCase):
    def test_real_cylindrical_band_remains_unconfirmed(self):
        profile = bend_profile()
        self.assertEqual(validate_bend_profile(profile, 8, solid_bounds=BOUNDS), profile)
        self.assertEqual(profile['bends'][0]['direction'], 'unconfirmed')
        self.assertEqual(validate_bend_profile(no_bends('unresolved'), 8), no_bends('unresolved'))
        self.assertEqual(validate_bend_profile(no_bends(), 8), no_bends())

    def test_rigid_axis_change_retains_cylindrical_relationships(self):
        profile = bend_profile(); bend = profile['bends'][0]
        # Rotate +90 degrees about X, then translate. Cylinder axis may have an
        # origin away from the solid's own box; only actual evidence lies inside.
        def transform(pos): return [pos[0]+10, 20-pos[2], pos[1]+30]
        bend['axis'] = {'point': transform([0, 0, -10]), 'direction': [0, -1, 0]}
        for line in bend['tangent_lines']:
            line.update({key: transform(pos) for key, pos in line.items()})
        points = bend['mesh']['positions']
        points[:] = [n for index in range(0, len(points), 3) for n in transform(points[index:index+3])]
        bounds = {'min': [10, 16, 30], 'max': [13, 20, 33], 'size': [3, 4, 3]}
        self.assertEqual(validate_bend_profile(profile, 8, solid_bounds=bounds), profile)

    def test_angles_and_radius_difference_must_have_consistent_evidence(self):
        for change in ({'rotation_angle_deg': 80}, {'included_angle_deg': 80},
                       {'rotation_angle_deg': 181, 'included_angle_deg': -1},
                       {'inner_radius_mm': 0}, {'outer_radius_mm': 3}, {'thickness_mm': 2},
                       {'thickness_mm': True}, {'length_mm': float('nan')}, {'length_mm': 0}):
            profile = bend_profile(); profile['bends'][0].update(change)
            with self.subTest(change=change), self.assertRaises(RecordError):
                validate_bend_profile(profile, 8, solid_bounds=BOUNDS)

    def test_foreign_missing_overlapping_and_repeated_faces_are_rejected(self):
        for change in ({'inner_face_ids': []}, {'outer_face_ids': ['F0009']},
                       {'inner_face_ids': ['F0000']}, {'inner_face_ids': ['F0001', 'F0001']},
                       {'outer_face_ids': ['F0001']},
                       {'flange_face_ids': [['F0003', 'F0004']]},
                       {'flange_face_ids': [['F0003', 'F0004'], ['F0005', 'F0004']]},
                       {'flange_face_ids': [['F0003', 'F0004'], ['F0005', 'F0001']]}):
            profile = bend_profile(); profile['bends'][0].update(change)
            with self.subTest(change=change), self.assertRaises(RecordError):
                validate_bend_profile(profile, 8, solid_bounds=BOUNDS)

    def test_no_confirmation_direction_or_reference_can_be_claimed_by_native(self):
        for change in ({'status': 'confirmed'}, {'direction': 'up'},
                       {'reference_face_id': 'F0003'}, {'reference_face_id': ''}):
            profile = bend_profile(); profile['bends'][0].update(change)
            with self.subTest(change=change), self.assertRaises(RecordError):
                validate_bend_profile(profile, 8, solid_bounds=BOUNDS)

    def test_nonunit_axis_and_false_tangent_lines_are_rejected(self):
        for kind in ('nonunit', 'zero', 'nonfinite', 'skew', 'short', 'inner', 'axial-shift', 'wrong-angle'):
            profile = bend_profile(); bend = profile['bends'][0]
            if kind == 'nonunit': bend['axis']['direction'] = [0, 0, 2]
            if kind == 'zero': bend['axis']['direction'] = [0, 0, 0]
            if kind == 'nonfinite': bend['axis']['point'] = [0, float('inf'), 0]
            if kind == 'skew': bend['tangent_lines'][0]['end'] = [2.1, 0, 4]
            if kind == 'short': bend['tangent_lines'][0]['end'] = [2, 0, 3]
            if kind == 'inner': bend['tangent_lines'][0] = {'start': [1, 0, 0], 'end': [1, 0, 4]}
            if kind == 'axial-shift': bend['tangent_lines'][0] = {'start': [2, 0, 1], 'end': [2, 0, 5]}
            if kind == 'wrong-angle':
                bend.update(rotation_angle_deg=80, included_angle_deg=100)
            with self.subTest(kind=kind), self.assertRaises(RecordError):
                validate_bend_profile(profile, 8, solid_bounds=BOUNDS)

    def test_invalid_empty_degenerate_and_oversized_mesh_is_rejected(self):
        for kind in ('empty', 'nonfinite', 'foreign-index', 'bool-index', 'degenerate', 'unknown-field', 'oversized'):
            profile = bend_profile(); mesh = profile['bends'][0]['mesh']
            if kind == 'empty': mesh['positions'] = []; mesh['indices'] = []
            if kind == 'nonfinite': mesh['positions'][0] = float('nan')
            if kind == 'foreign-index': mesh['indices'][0] = 99
            if kind == 'bool-index': mesh['indices'][0] = True
            if kind == 'degenerate': mesh['indices'][:3] = [0, 0, 1]
            if kind == 'unknown-field': mesh['file'] = '../../foreign'
            if kind == 'oversized': mesh['positions'] = mesh['positions'] * 10000
            with self.subTest(kind=kind), self.assertRaises(RecordError):
                validate_bend_profile(profile, 8, solid_bounds=BOUNDS)

    def test_mesh_must_lie_on_declared_cylinder_sector_and_source_bounds(self):
        for kind in ('radius', 'sector', 'axial', 'source'):
            profile = bend_profile(); mesh = profile['bends'][0]['mesh']
            bounds = copy.deepcopy(BOUNDS)
            if kind == 'radius': mesh['positions'][6:9] = [1, 1, 0]
            if kind == 'sector': mesh['positions'][6:9] = [-1, 0, 0]
            if kind == 'axial': mesh['positions'][8] = 5
            if kind == 'source': bounds = {'min': [10, 10, 10], 'max': [13, 13, 14], 'size': [3, 3, 4]}
            with self.subTest(kind=kind), self.assertRaises(RecordError):
                validate_bend_profile(profile, 8, solid_bounds=bounds)

    def test_strict_profile_schema_unknown_units_and_partial_states(self):
        for change in ({'status': 'ready'}, {'status': []}, {'unit': 'inch'}, {'reason': ''},
                       {'warnings': ['']}, {'bends': []}, {'extra': True}):
            profile = bend_profile(); profile.update(change)
            with self.subTest(change=change), self.assertRaises(RecordError):
                validate_bend_profile(profile, 8, solid_bounds=BOUNDS)
        for status in ('unresolved', 'not_applicable'):
            profile = bend_profile(); profile['status'] = status
            with self.assertRaises(RecordError): validate_bend_profile(profile, 8, solid_bounds=BOUNDS)

    def test_band_requires_both_curved_faces_and_complete_tangent_boundaries(self):
        for kind in ('inner-only', 'outer-only', 'partial-length', 'partial-angle', 'zero-radial'):
            profile = bend_profile(); mesh = profile['bends'][0]['mesh']
            if kind == 'inner-only':
                mesh['positions'] = mesh['positions'][:18]; mesh['indices'] = mesh['indices'][:12]
            if kind == 'outer-only':
                mesh['positions'] = mesh['positions'][18:]
                mesh['indices'] = [index-6 for index in mesh['indices'][12:]]
            if kind == 'partial-length':
                mesh['positions'] = [value/2 if index % 3 == 2 else value
                                     for index, value in enumerate(mesh['positions'])]
            if kind == 'partial-angle':
                for index in range(0, len(mesh['positions']), 3):
                    x, y, level = mesh['positions'][index:index+3]
                    radius = math.hypot(x, y); angle = math.atan2(y, x)/2
                    mesh['positions'][index:index+3] = [radius*math.cos(angle), radius*math.sin(angle), level]
            if kind == 'zero-radial': mesh['positions'][6:9] = [0, 0, 0]
            with self.subTest(kind=kind), self.assertRaises(RecordError):
                validate_bend_profile(profile, 8, solid_bounds=BOUNDS)

    def test_duplicate_bends_and_unvalidated_extra_fields_are_rejected(self):
        profile = bend_profile(); profile['bends'].append(copy.deepcopy(profile['bends'][0]))
        with self.assertRaises(RecordError): validate_bend_profile(profile, 8, solid_bounds=BOUNDS)
        profile = bend_profile(); profile['bends'][0]['machine_program'] = 'not evidence'
        with self.assertRaises(RecordError): validate_bend_profile(profile, 8, solid_bounds=BOUNDS)


if __name__ == '__main__':
    unittest.main()
