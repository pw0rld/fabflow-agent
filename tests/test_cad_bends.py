"""Actual cylindrical sheet bends and rejection controls, built with OCCT."""
import importlib.util
import math
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from cad_bends import recognize_bends
from cad_thickness import infer_thickness


def make_bend(angle_deg=90., inner_radius=5., thickness=2., length=40., flange=40.):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire, BRepBuilderAPI_MakeFace
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.GC import GC_MakeArcOfCircle
    from OCP.gp import gp_Pnt, gp_Vec
    beta = math.radians(angle_deg); ri = inner_radius; ro = ri+thickness
    def p(r, a): return gp_Pnt(r*math.cos(a), r*math.sin(a), 0)
    outer0, outer1 = p(ro, 0), p(ro, beta)
    inner0, inner1 = p(ri, 0), p(ri, beta)
    far0o, far0i = gp_Pnt(ro, -flange, 0), gp_Pnt(ri, -flange, 0)
    far1o = gp_Pnt(outer1.X()-flange*math.sin(beta), outer1.Y()+flange*math.cos(beta), 0)
    far1i = gp_Pnt(inner1.X()-flange*math.sin(beta), inner1.Y()+flange*math.cos(beta), 0)
    wire = BRepBuilderAPI_MakeWire()
    wire.Add(BRepBuilderAPI_MakeEdge(far0o, outer0).Edge())
    wire.Add(BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(outer0, p(ro, beta/2), outer1).Value()).Edge())
    for a, b in [(outer1, far1o), (far1o, far1i), (far1i, inner1)]: wire.Add(BRepBuilderAPI_MakeEdge(a, b).Edge())
    wire.Add(BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(inner1, p(ri, beta/2), inner0).Value()).Edge())
    for a, b in [(inner0, far0i), (far0i, far0o)]: wire.Add(BRepBuilderAPI_MakeEdge(a, b).Edge())
    return BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(wire.Wire()).Face(), gp_Vec(0, 0, length)).Shape()


def face_map(shape):
    from OCP.TopExp import TopExp
    from OCP.TopAbs import TopAbs_FACE
    from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
    result = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
    TopExp.MapShapes_s(shape, TopAbs_FACE, result)
    return result


@unittest.skipUnless(importlib.util.find_spec('OCP') is not None, 'Run with .venv-cad.')
class NativeBendTests(unittest.TestCase):
    def test_true_45_90_and_135_degree_bends_use_rotation_not_supplement(self):
        for angle in (45., 90., 135.):
            with self.subTest(angle=angle):
                shape = make_bend(angle)
                result = recognize_bends(shape, face_map(shape), infer_thickness(shape))
                self.assertEqual(result['status'], 'candidate', result)
                self.assertEqual(len(result['bends']), 1)
                bend = result['bends'][0]
                self.assertAlmostEqual(bend['rotation_angle_deg'], angle, places=5)
                self.assertAlmostEqual(bend['included_angle_deg'], 180-angle, places=5)
                self.assertAlmostEqual(bend['inner_radius_mm'], 5)
                self.assertAlmostEqual(bend['outer_radius_mm'], 7)
                self.assertAlmostEqual(bend['length_mm'], 40)
                self.assertEqual(bend['direction'], 'unconfirmed')
                self.assertIsNone(bend['reference_face_id'])
                self.assertTrue(bend['mesh']['positions'])
                for line in bend['tangent_lines']:
                    self.assertAlmostEqual(math.dist(line['start'], line['end']), 40., places=5)
                radii = set()
                origin, axis = bend['axis']['point'], bend['axis']['direction']
                points = bend['mesh']['positions']
                for j in range(0, len(points), 3):
                    delta = [points[j+k]-origin[k] for k in range(3)]
                    along = sum(a*b for a, b in zip(delta, axis))
                    radius = math.sqrt(sum((delta[k]-along*axis[k])**2 for k in range(3)))
                    self.assertLess(min(abs(radius-5), abs(radius-7)), 1e-5)
                    radii.add(round(radius))
                self.assertEqual(radii, {5, 7})

    def test_rotated_translated_bend_keeps_physical_tangent_lines(self):
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
        shape = make_bend(45)
        original = recognize_bends(shape, face_map(shape), infer_thickness(shape))['bends'][0]
        rotation = gp_Trsf(); rotation.SetRotation(gp_Ax1(gp_Pnt(), gp_Dir(1, 2, 3)), .61)
        translation = gp_Trsf(); translation.SetTranslation(gp_Vec(100, -70, 230))
        transform = translation.Multiplied(rotation)
        moved = BRepBuilderAPI_Transform(shape, transform, True).Shape()
        result = recognize_bends(moved, face_map(moved), infer_thickness(moved))
        self.assertEqual(result['status'], 'candidate', result)
        bend = result['bends'][0]
        self.assertAlmostEqual(bend['rotation_angle_deg'], 45., places=5)
        self.assertEqual(bend['direction'], 'unconfirmed')
        actual = [p for line in bend['tangent_lines'] for p in (line['start'], line['end'])]
        for line in original['tangent_lines']:
            for endpoint in (line['start'], line['end']):
                p = gp_Pnt(*endpoint).Transformed(transform)
                expected = [p.X(), p.Y(), p.Z()]
                self.assertLess(min(math.dist(expected, q) for q in actual), 1e-5)

    def test_hole_pipe_and_block_rounding_are_not_sheet_bends(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_EDGE
        from OCP.TopoDS import TopoDS
        from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
        hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(50, 40, 0), gp_Dir(0, 0, 1)), 4, 2).Shape()
        plate = BRepAlgoAPI_Cut(BRepPrimAPI_MakeBox(100, 80, 2).Shape(), hole).Shape()
        pipe = BRepAlgoAPI_Cut(BRepPrimAPI_MakeCylinder(7, 40).Shape(), BRepPrimAPI_MakeCylinder(5, 40).Shape()).Shape()
        block = BRepPrimAPI_MakeBox(30, 30, 30).Shape()
        fillet = BRepFilletAPI_MakeFillet(block)
        fillet.Add(2., TopoDS.Edge(TopExp_Explorer(block, TopAbs_EDGE).Current())); fillet.Build()
        self.assertTrue(fillet.IsDone())
        for shape in (plate, pipe, fillet.Shape()):
            result = recognize_bends(shape, face_map(shape), infer_thickness(shape))
            self.assertNotEqual(result['status'], 'candidate', result)
            self.assertEqual(result['bends'], [])

    def test_missing_thickness_and_unproved_flat_geometry_remain_unresolved(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        shape = make_bend()
        result = recognize_bends(shape, face_map(shape), {'status': 'unresolved', 'value_mm': None})
        self.assertEqual(result['status'], 'unresolved')
        flat = BRepPrimAPI_MakeBox(100, 80, 2).Shape()
        result = recognize_bends(flat, face_map(flat), infer_thickness(flat))
        self.assertEqual(result['status'], 'unresolved')
        proved = recognize_bends(flat, face_map(flat), infer_thickness(flat), laser_cut={'status': 'ready'})
        self.assertEqual(proved['status'], 'not_applicable')

    def test_physical_plane_or_cylinder_normal_reversal_cannot_pass_g1(self):
        from OCP.BRepAdaptor import BRepAdaptor_Surface
        from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder
        from OCP.TopoDS import TopoDS
        from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
        shape = make_bend(); original = face_map(shape); thickness = infer_thickness(shape)
        for kind in (GeomAbs_Plane, GeomAbs_Cylinder):
            changed = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
            for j in range(1, original.Extent()+1):
                face = original.FindKey(j)
                changed.Add(face.Reversed() if BRepAdaptor_Surface(TopoDS.Face(face), True).GetType()==kind else face)
            result = recognize_bends(shape, changed, thickness)
            self.assertEqual(result['status'], 'unresolved')
            self.assertEqual(result['bends'], [])

    def test_early_and_last_pair_budget_exits_never_publish_partial_candidates(self):
        import copy
        from unittest import mock
        import cad_bends
        shape = make_bend(); faces = face_map(shape); thickness = infer_thickness(shape)
        result = recognize_bends(shape, faces, thickness, deadline=time.monotonic()-1)
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(result['bends'], [])
        source = next(item for item in thickness['evidence'] if item['kind']=='coaxial_cylinders')
        thickness = copy.deepcopy(thickness)
        thickness['evidence'].append(copy.deepcopy(source))
        original = cad_bends._recognize_pair
        calls = 0
        def late_limit(*args, **kwargs):
            nonlocal calls
            calls += 1
            value = original(*args, **kwargs)
            if calls == 2:
                raise cad_bends._Limit('test last-pair budget')
            return value
        with mock.patch('cad_bends._recognize_pair', late_limit):
            result = recognize_bends(shape, faces, thickness)
        self.assertEqual(calls, 2)
        self.assertEqual(result['status'], 'unresolved')
        self.assertEqual(result['bends'], [])


if __name__ == '__main__':
    unittest.main()
