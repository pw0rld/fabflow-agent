"""Native boundary tests for conservative, unconfirmed thickness candidates."""
import importlib.util
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from cad_thickness import infer_thickness, _serialize
NATIVE = importlib.util.find_spec('OCP') is not None


@unittest.skipUnless(NATIVE, 'Run with .venv-cad/bin/python for OCCT tests.')
class NativeThicknessTests(unittest.TestCase):
    def box(self, x, y, z, origin=(0, 0, 0)):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.gp import gp_Pnt
        return BRepPrimAPI_MakeBox(gp_Pnt(*origin), x, y, z).Shape()

    def cylinder(self, radius, height, origin=(0, 0, 0)):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
        from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
        return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*origin), gp_Dir(0, 0, 1)), radius, height).Shape()

    def cut(self, a, b):
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        result = BRepAlgoAPI_Cut(a, b)
        self.assertTrue(result.IsDone())
        return result.Shape()

    def fuse(self, a, b):
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
        result = BRepAlgoAPI_Fuse(a, b)
        self.assertTrue(result.IsDone())
        return result.Shape()

    def rotated(self, shape):
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
        rotation = gp_Trsf()
        rotation.SetRotation(gp_Ax1(gp_Pnt(), gp_Dir(1, 2, 3)), .61)
        translation = gp_Trsf()
        translation.SetTranslation(gp_Vec(130, -70, 230))
        return BRepBuilderAPI_Transform(shape, translation.Multiplied(rotation), True).Shape()

    def candidate(self, shape, expected, method=None):
        value = infer_thickness(shape)
        self.assertEqual(value['status'], 'candidate', value)
        self.assertAlmostEqual(value['value_mm'], expected, places=5)
        self.assertIn(value['confidence'], ('high', 'medium'))
        self.assertTrue(value['evidence'])
        self.assertIn('工程师确认', value['warnings'][0])
        if method:
            self.assertEqual(value['method'], method)
        return value

    def test_flat_plate_and_rotated_plate(self):
        plate = self.box(100, 80, 2)
        self.candidate(plate, 2, 'parallel_faces')
        self.candidate(self.rotated(plate), 2, 'parallel_faces')

    def test_through_holes_retain_plate_thickness(self):
        plate = self.box(100, 80, 1.5)
        for point in ((20, 20, -1), (50, 40, -1), (80, 60, -1)):
            plate = self.cut(plate, self.cylinder(4, 4, point))
        self.candidate(self.rotated(plate), 1.5, 'parallel_faces')

    def test_many_unrelated_slot_walls_do_not_exhaust_native_pair_budget(self):
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Compound
        from OCP.TopExp import TopExp
        from OCP.TopAbs import TopAbs_FACE
        from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
        tools, builder = TopoDS_Compound(), BRep_Builder()
        builder.MakeCompound(tools)
        # 72 rectangular through-holes produce more than 283 planar faces:
        # the old unfiltered all-pairs loop exhausted 40,000 comparisons even
        # though just the broad top/bottom faces prove the sheet thickness.
        for row in range(8):
            for column in range(9):
                builder.Add(tools, self.box(10, 10, 4, (30+column*40, 30+row*40, -1)))
        plate = self.cut(self.box(420, 360, 2), tools)
        faces = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
        TopExp.MapShapes_s(plate, TopAbs_FACE, faces)
        self.assertGreater(faces.Extent(), 283)
        self.candidate(plate, 2, 'parallel_faces')

    def test_perforations_covering_coarse_uv_grid_still_measure_the_sheet(self):
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Compound
        tools, builder = TopoDS_Compound(), BRep_Builder()
        builder.MakeCompound(tools)
        # All sixteen original UV samples fall at hole centres. Finer samples
        # must be classified against the trimmed native face before use.
        for x in (12.5, 37.5, 62.5, 87.5):
            for y in (12.5, 37.5, 62.5, 87.5):
                builder.Add(tools, self.cylinder(4, 4, (x, y, -1)))
        plate = self.cut(self.box(100, 100, 2), tools)
        self.candidate(plate, 2, 'parallel_faces')

    def test_narrow_window_frame_samples_original_trim_instead_of_empty_uv_window(self):
        # All regular 4/8/16 UV centres fall inside the open window, although
        # the two native frame faces are a genuine 1.2mm sheet pair.
        frame = self.cut(self.box(228, 376, 1.2), self.box(220, 368, 4, (4, 4, -1)))
        self.candidate(frame, 1.2, 'parallel_faces')
        self.candidate(self.rotated(frame), 1.2, 'parallel_faces')

    def test_classification_budget_reports_phase_and_does_not_publish_partial_value(self):
        with patch('cad_thickness.MAX_CLASSIFICATIONS', 1):
            value = infer_thickness(self.box(100, 80, 2))
        self.assertEqual(value['status'], 'unresolved')
        self.assertIsNone(value['value_mm'])
        self.assertEqual(value['evidence'], [])
        self.assertTrue(any('原生点分类次数' in text and 'native_overlap' in text for text in value['warnings']))

    def test_real_rounded_bend_combines_planar_and_cylindrical_evidence(self):
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
        bend = self.cut(self.cylinder(7, 40), self.cylinder(5, 40))
        bend = BRepAlgoAPI_Common(bend, self.box(7, 7, 40)).Shape()
        bend = self.fuse(bend, self.box(2, 40, 40, (5, -40, 0)))
        bend = self.fuse(bend, self.box(40, 2, 40, (-40, 5, 0)))
        self.candidate(bend, 2, 'combined')
        self.candidate(self.rotated(bend), 2, 'combined')

    def test_cylindrical_wall_is_supported_but_solid_rods_are_not(self):
        tube = self.cut(self.cylinder(20, 80), self.cylinder(18, 80))
        value = self.candidate(tube, 2, 'coaxial_cylinders')
        self.assertTrue(any('曲面壁厚' in warning for warning in value['warnings']))
        for shape in (self.cylinder(6, 100), self.box(100, 5, 2), self.box(20, 20, 20)):
            value = infer_thickness(shape)
            self.assertNotEqual(value['status'], 'candidate', value)
            self.assertIsNone(value['value_mm'])

    def test_variable_thickness_and_local_recess_stay_unresolved(self):
        stepped = self.fuse(self.box(100, 40, 2), self.box(100, 40, 4, (0, 40, 0)))
        recess = self.cut(self.box(100, 80, 4), self.box(50, 40, 2, (25, 20, 2)))
        deep_pocket = self.cut(self.box(100, 80, 30), self.box(80, 60, 29, (10, 10, 1)))
        for shape in (stepped, recess, deep_pocket):
            value = infer_thickness(shape)
            self.assertEqual(value['status'], 'unresolved', value)
            self.assertIsNone(value['value_mm'])
            self.assertEqual(value['confidence'], 'none')

    def test_cavity_spacing_is_not_wall_thickness(self):
        hollow = self.cut(self.box(24, 24, 24), self.box(20, 20, 20, (2, 2, 2)))
        value = infer_thickness(hollow)
        self.assertNotEqual(value['value_mm'], 20, value)
        if value['status'] == 'candidate':
            self.assertAlmostEqual(value['value_mm'], 2)

    def test_expired_budget_never_publishes_partial_candidate(self):
        value = infer_thickness(self.box(100, 80, 2), deadline=time.monotonic()-1)
        self.assertEqual(value['status'], 'unresolved')
        self.assertIsNone(value['value_mm'])
        self.assertEqual(value['evidence'], [])
        self.assertIn('预算', value['reason'])

    def test_unsupported_surface_is_unresolved_and_evidence_stays_positive(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeSphere
        value = infer_thickness(BRepPrimAPI_MakeSphere(10).Shape())
        self.assertEqual(value['status'], 'unresolved')
        evidence = _serialize([{'kind': 'parallel_faces', 'face_ids': ['F0001', 'F0002'],
                                'thickness_mm': .01, 'support_area_mm2': .00000025}])
        self.assertGreater(evidence[0]['support_area_mm2'], 0)

    def test_compact_evidence_keeps_smaller_conflicting_wall_distance(self):
        large = [{'kind': 'parallel_faces', 'face_ids': [f'F{i:04d}', f'F{i+1:04d}'],
                  'thickness_mm': 1.2, 'support_area_mm2': 1000-i} for i in range(1, 100)]
        local = {'kind': 'parallel_faces', 'face_ids': ['F0201', 'F0202'],
                 'thickness_mm': 1., 'support_area_mm2': .1}
        evidence = _serialize(large+[local])
        self.assertEqual(len(evidence), 64)
        self.assertEqual({row['thickness_mm'] for row in evidence}, {1., 1.2})


if __name__ == '__main__':
    unittest.main()
