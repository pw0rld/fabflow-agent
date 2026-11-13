"""Analytic contour contract checks; synthetic paths are not native CAD proof."""
import copy
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cad_laser_contract import validate_laser_profile
from prep_record import RecordError


def rectangle(x, y, width, height):
    points = [[x, y], [x+width, y], [x+width, y+height], [x, y+height]]
    return [{'type': 'line', 'start': point, 'end': points[(index+1) % 4]}
            for index, point in enumerate(points)]


def laser_profile(solid_id='S0001'):
    return {'status': 'ready', 'reason': '真实平面拉伸件，轮廓供工程核对。', 'unit': 'mm',
            'thickness_mm': 1.5, 'source_face_id': 'F0001',
            'plane': {'origin': [0, 0, 0], 'u': [1, 0, 0], 'v': [0, 1, 0], 'normal': [0, 0, 1]},
            'bounds_mm': {'min': [0, 0], 'max': [10, 8], 'size': [10, 8]},
            'contours': [{'id': 'C0001', 'role': 'outer', 'segments': rectangle(0, 0, 10, 8)},
                         {'id': 'C0002', 'role': 'inner', 'segments': [
                             {'type': 'circle', 'center': [2, 3], 'radius': .5}]}],
            'holes': [{'id': 'C0002', 'center': [2, 3], 'diameter_mm': 1}],
            'dxf_file': solid_id + '-cut.dxf',
            'warnings': ['工程审核草稿；未排样、未补偿，不代表机床程序。']}


def unsupported_profile():
    return {'status': 'unsupported', 'reason': '折弯实体未展开，暂不输出切割轮廓。', 'unit': 'mm',
            'thickness_mm': 1, 'source_face_id': None, 'plane': None, 'bounds_mm': None,
            'contours': [], 'holes': [], 'dxf_file': None, 'warnings': []}


class LaserContractTests(unittest.TestCase):
    def test_line_circle_profile_and_source_volume_are_consistent(self):
        profile = laser_profile()
        bounds = {'min': [0, 0, 0], 'max': [10, 8, 1.5], 'size': [10, 8, 1.5]}
        expected = (80-math.pi*.5**2)*1.5
        self.assertEqual(validate_laser_profile(profile, 'S0001', 8,
                         solid_bounds=bounds, volume_mm3=expected), profile)
        with self.assertRaises(RecordError):
            validate_laser_profile(profile, 'S0001', 8, volume_mm3=expected+1)
        profile['plane']['origin'] = [100, 0, 0]
        with self.assertRaises(RecordError):
            validate_laser_profile(profile, 'S0001', 8, solid_bounds=bounds)

    def test_arcs_are_validated_without_straight_line_approximation(self):
        profile = laser_profile()
        points = [[10, 5], [5, 10], [0, 5], [5, 0]]
        profile.update(bounds_mm={'min': [0, 0], 'max': [10, 10], 'size': [10, 10]}, holes=[])
        profile['contours'] = [{'id': 'C1', 'role': 'outer', 'segments': [
            {'type': 'arc', 'start': point, 'end': points[(i+1) % 4], 'center': [5, 5],
             'radius': 5, 'clockwise': False} for i, point in enumerate(points)]}]
        self.assertEqual(validate_laser_profile(profile, 'S0001', 8,
                         volume_mm3=math.pi*25*1.5), profile)
        profile['contours'][0]['segments'][0]['radius'] = 6
        with self.assertRaises(RecordError): validate_laser_profile(profile, 'S0001', 8)

    def test_half_arcs_need_real_arc_extrema_and_clockwise_orientation(self):
        profile = laser_profile()
        profile.update(bounds_mm={'min': [0, 0], 'max': [10, 5], 'size': [10, 5]}, holes=[])
        profile['contours'] = [{'id': 'C1', 'role': 'outer', 'segments': [
            {'type': 'arc', 'start': [0, 0], 'end': [10, 0], 'center': [5, 0],
             'radius': 5, 'clockwise': True},
            {'type': 'line', 'start': [10, 0], 'end': [0, 0]}]}]
        self.assertEqual(validate_laser_profile(profile, 'S0001', 8,
                         volume_mm3=math.pi*25/2*1.5), profile)
        profile['bounds_mm']['max'][1] = profile['bounds_mm']['size'][1] = 4
        with self.assertRaises(RecordError): validate_laser_profile(profile, 'S0001', 8)

    def test_non_circular_inner_cutouts_are_not_fabricated_as_round_holes(self):
        profile = laser_profile()
        profile['contours'][1]['segments'] = rectangle(2, 3, 1, 1)
        profile['holes'] = []
        self.assertEqual(validate_laser_profile(profile, 'S0001', 8, volume_mm3=79*1.5), profile)
        profile['holes'] = [{'id': 'H1', 'center': [2.5, 3.5], 'diameter_mm': 1}]
        with self.assertRaises(RecordError): validate_laser_profile(profile, 'S0001', 8)

    def test_missing_forged_hole_and_duplicate_contour_are_rejected(self):
        for mutation in ('missing', 'wrong-center', 'wrong-diameter', 'duplicate'):
            profile = laser_profile()
            if mutation == 'missing': profile['holes'] = []
            if mutation == 'wrong-center': profile['holes'][0]['center'] = [3, 3]
            if mutation == 'wrong-diameter': profile['holes'][0]['diameter_mm'] = 2
            if mutation == 'duplicate': profile['contours'][1]['id'] = 'C0001'
            with self.subTest(mutation=mutation), self.assertRaises(RecordError):
                validate_laser_profile(profile, 'S0001', 8)

    def test_closed_path_does_not_mean_nonzero_material_area(self):
        profile = laser_profile()
        profile.update(holes=[], contours=[{'id': 'C1', 'role': 'outer', 'segments': [
            {'type': 'line', 'start': [0, 0], 'end': [10, 8]},
            {'type': 'line', 'start': [10, 8], 'end': [0, 0]}]}])
        with self.assertRaises(RecordError): validate_laser_profile(profile, 'S0001', 8)

    def test_invalid_units_paths_faces_vectors_and_nonfinite_values_are_rejected(self):
        changes = [{'unit': 'inch'}, {'unit': []}, {'status': []}, {'reason': ''},
                   {'thickness_mm': 0}, {'thickness_mm': True}, {'thickness_mm': float('nan')},
                   {'source_face_id': 'F0000'}, {'source_face_id': 'F0009'},
                   {'dxf_file': '../S0001-cut.dxf'}, {'dxf_file': 'S0002-cut.dxf'},
                   {'extra': True}, {'warnings': ['']}, {'contours': []}]
        for change in changes:
            profile = laser_profile(); profile.update(change)
            with self.subTest(change=change), self.assertRaises(RecordError):
                validate_laser_profile(profile, 'S0001', 8)
        for mutation in ('nonunit', 'nonorthogonal', 'lefthanded', 'invalid-origin'):
            profile = laser_profile()
            if mutation == 'nonunit': profile['plane']['u'] = [2, 0, 0]
            if mutation == 'nonorthogonal': profile['plane']['v'] = [1, 0, 0]
            if mutation == 'lefthanded': profile['plane']['normal'] = [0, 0, -1]
            if mutation == 'invalid-origin': profile['plane']['origin'][0] = float('inf')
            with self.subTest(mutation=mutation), self.assertRaises(RecordError):
                validate_laser_profile(profile, 'S0001', 8)

    def test_open_zero_length_and_unrecognized_edges_are_rejected(self):
        for mutation in ('open', 'zero', 'type', 'circle-mixed', 'wrong-role'):
            profile = laser_profile(); segments = profile['contours'][0]['segments']
            if mutation == 'open': segments[-1]['end'] = [.1, 0]
            if mutation == 'zero': segments[0]['end'] = [0, 0]
            if mutation == 'type': segments[0]['type'] = 'spline'
            if mutation == 'circle-mixed': segments.append({'type': 'circle', 'center': [5, 4], 'radius': 1})
            if mutation == 'wrong-role': profile['contours'][0]['role'] = []
            with self.subTest(mutation=mutation), self.assertRaises(RecordError):
                validate_laser_profile(profile, 'S0001', 8)

    def test_unsupported_profiles_never_offer_partial_cutting_files(self):
        profile = unsupported_profile()
        self.assertEqual(validate_laser_profile(profile, 'S0001', 8), profile)
        for change in ({'dxf_file': 'S0001-cut.dxf'}, {'source_face_id': 'F0001'},
                       {'contours': laser_profile()['contours']}, {'plane': laser_profile()['plane']}):
            value = copy.deepcopy(profile); value.update(change)
            with self.subTest(change=change), self.assertRaises(RecordError):
                validate_laser_profile(value, 'S0001', 8)


if __name__ == '__main__':
    unittest.main()
