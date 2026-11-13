"""Native plate equivalence and exact analytic DXF regression tests."""
import importlib.util
import math
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from cad_laser import export_laser_cut
from cad_thickness import infer_thickness
NATIVE = importlib.util.find_spec('OCP') is not None and importlib.util.find_spec('ezdxf') is not None


@unittest.skipUnless(NATIVE, 'Run with .venv-cad and requirements-cad.txt.')
class NativeLaserTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyl-laser-')
        self.folder = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

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
        return BRepAlgoAPI_Cut(a, b).Shape()

    def fuse(self, a, b):
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
        return BRepAlgoAPI_Fuse(a, b).Shape()

    def volume(self, shape):
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, props, True, False, False)
        return abs(props.Mass())

    def run_export(self, shape, thickness=None, **kwargs):
        from OCP.TopExp import TopExp
        from OCP.TopAbs import TopAbs_FACE
        from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
        faces = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
        TopExp.MapShapes_s(shape, TopAbs_FACE, faces)
        return export_laser_cut(shape, faces, thickness or infer_thickness(shape), 'S0001', self.folder, 'a'*64, **kwargs)

    @staticmethod
    def contour_area(contour):
        area = 0
        for s in contour['segments']:
            if s['type'] == 'circle':
                return math.pi*s['radius']**2
            a, b = s['start'], s['end']
            if s['type'] == 'line':
                area += (a[0]*b[1]-a[1]*b[0])/2
            else:
                c = s['center']
                first = math.atan2(a[1]-c[1], a[0]-c[0])
                last = math.atan2(b[1]-c[1], b[0]-c[0])
                angle = -((first-last) % math.tau) if s['clockwise'] else (last-first) % math.tau
                area += (s['radius']**2*angle+c[0]*(b[1]-a[1])-c[1]*(b[0]-a[0]))/2
        return abs(area)

    def ready(self, shape, thickness=None):
        result = self.run_export(shape, thickness)
        import ezdxf
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(result['unit'], 'mm')
        self.assertEqual(result['bounds_mm']['min'], [0., 0.])
        self.assertEqual(sum(c['role']=='outer' for c in result['contours']), 1)
        area = sum((1 if c['role']=='outer' else -1)*self.contour_area(c) for c in result['contours'])
        self.assertAlmostEqual(area*result['thickness_mm'], self.volume(shape), delta=max(1e-5, self.volume(shape)*1e-7))
        doc = ezdxf.readfile(self.folder/result['dxf_file'])
        self.assertEqual(doc.units, 4)
        self.assertFalse(doc.audit().has_errors)
        self.assertEqual(len(doc.modelspace()), sum(len(c['segments']) for c in result['contours']))
        self.assertTrue(all(e.dxftype() in ('LINE', 'ARC', 'CIRCLE') for e in doc.modelspace()))
        self.assertTrue(all(e.dxf.layer in ('CUT_OUTER', 'CUT_INNER') for e in doc.modelspace()))
        return result

    def test_rotated_translated_plate_with_round_and_rectangular_through_cutouts(self):
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
        plate = self.box(100, 80, 2)
        plate = self.cut(plate, self.cylinder(4, 4, (20, 20, -1)))
        plate = self.cut(plate, self.cylinder(3, 4, (50, 40, -1)))
        plate = self.cut(plate, self.box(10, 8, 4, (65, 55, -1)))
        rotation = gp_Trsf(); rotation.SetRotation(gp_Ax1(gp_Pnt(), gp_Dir(1, 2, 3)), .61)
        translation = gp_Trsf(); translation.SetTranslation(gp_Vec(130, -70, 230))
        transform = translation.Multiplied(rotation)
        plate = BRepBuilderAPI_Transform(plate, transform, True).Shape()
        result = self.ready(plate)
        self.assertEqual(len(result['holes']), 2)
        self.assertEqual(sorted(h['diameter_mm'] for h in result['holes']), [6., 8.])
        self.assertEqual(len(result['contours']), 4)
        plane = result['plane']
        for hole in result['holes']:
            expected_xy = (20, 20) if hole['diameter_mm']==8 else (50, 40)
            source = gp_Pnt(*expected_xy, 2).Transformed(transform)
            actual = [plane['origin'][i]+plane['u'][i]*hole['center'][0]+plane['v'][i]*hole['center'][1] for i in range(3)]
            self.assertLess(math.dist(actual, [source.X(), source.Y(), source.Z()]), 1e-5)

    def test_round_plate_preserves_circle_not_a_polygon(self):
        result = self.ready(self.cylinder(25, 2))
        self.assertEqual(result['bounds_mm']['size'], [50., 50.])
        self.assertEqual(result['contours'][0]['segments'][0]['type'], 'circle')
        self.assertEqual(result['holes'], [])

    def test_arc_outline_preserves_exact_arc_and_area(self):
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire, BRepBuilderAPI_MakeFace
        from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
        from OCP.GC import GC_MakeArcOfCircle
        from OCP.gp import gp_Pnt, gp_Vec
        start, middle, end = gp_Pnt(20, 0, 0), gp_Pnt(0, 20, 0), gp_Pnt(-20, 0, 0)
        wire = BRepBuilderAPI_MakeWire()
        wire.Add(BRepBuilderAPI_MakeEdge(end, start).Edge())
        wire.Add(BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(start, middle, end).Value()).Edge())
        shape = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(wire.Wire()).Face(), gp_Vec(0, 0, 2)).Shape()
        result = self.ready(shape)
        self.assertEqual({s['type'] for s in result['contours'][0]['segments']}, {'line', 'arc'})
        self.assertAlmostEqual(self.contour_area(result['contours'][0]), 200*math.pi, places=5)

    def test_nonround_slot_stays_inner_contour_without_fake_hole_center(self):
        cutter = self.fuse(self.cylinder(5, 4, (40, 40, -1)), self.box(20, 10, 4, (40, 35, -1)))
        cutter = self.fuse(cutter, self.cylinder(5, 4, (60, 40, -1)))
        result = self.ready(self.cut(self.box(100, 80, 2), cutter))
        self.assertEqual(len(result['contours']), 2)
        self.assertEqual(result['holes'], [])
        inner = next(c for c in result['contours'] if c['role']=='inner')
        self.assertEqual({s['type'] for s in inner['segments']}, {'line', 'arc'})

    def test_two_native_semicircles_are_losslessly_one_circle_hole(self):
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
        from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
        from OCP.TopoDS import TopoDS
        from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt, gp_Vec
        polygon = BRepBuilderAPI_MakePolygon()
        for p in [(0, 0, 0), (100, 0, 0), (100, 80, 0), (0, 80, 0)]:
            polygon.Add(gp_Pnt(*p))
        polygon.Close()
        circle = gp_Circ(gp_Ax2(gp_Pnt(40, 30, 0), gp_Dir(0, 0, 1)), 5)
        inner = BRepBuilderAPI_MakeWire()
        inner.Add(BRepBuilderAPI_MakeEdge(circle, 0, math.pi).Edge())
        inner.Add(BRepBuilderAPI_MakeEdge(circle, math.pi, math.tau).Edge())
        face = BRepBuilderAPI_MakeFace(polygon.Wire())
        face.Add(TopoDS.Wire(inner.Wire().Reversed()))
        result = self.ready(BRepPrimAPI_MakePrism(face.Face(), gp_Vec(0, 0, 2)).Shape())
        self.assertEqual(len(result['holes']), 1)
        self.assertEqual(result['holes'][0]['diameter_mm'], 10.)
        profile = next(c for c in result['contours'] if c['role']=='inner')
        self.assertEqual(profile['segments'], [{'type': 'circle', 'center': [40., 30.], 'radius': 5.}])

    def test_folded_stepped_blind_and_counterbored_solids_never_export_a_projection(self):
        bent = self.fuse(self.box(80, 2, 40), self.box(2, 60, 40, (0, 2, 0)))
        stepped = self.fuse(self.box(100, 40, 2), self.box(100, 40, 4, (0, 40, 0)))
        blind = self.cut(self.box(100, 80, 4), self.cylinder(10, 2, (50, 40, 2)))
        counter = self.cut(self.box(100, 80, 4), self.cylinder(3, 6, (50, 40, -1)))
        counter = self.cut(counter, self.cylinder(6, 1, (50, 40, 3)))
        # The stepped average thickness even matches A*t numerically. Native
        # intermediate surfaces and whole-solid equivalence must still reject it.
        for shape, value in [(bent, 2), (stepped, 3), (blind, 4), (counter, 4),
                             (self.box(20, 20, 20), 20), (self.cylinder(6, 100), 12)]:
            result = self.run_export(shape, {'status': 'candidate', 'value_mm': value})
            self.assertEqual(result['status'], 'unsupported', result)
            for field in ('source_face_id', 'plane', 'bounds_mm', 'dxf_file'):
                self.assertIsNone(result[field])
            self.assertEqual(result['contours'], [])
            self.assertEqual(result['holes'], [])
            self.assertEqual(list(self.folder.iterdir()), [])

    def test_ellipse_is_unsupported_without_line_approximation(self):
        from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire, BRepBuilderAPI_MakeFace
        from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
        from OCP.gp import gp_Ax2, gp_Dir, gp_Elips, gp_Pnt, gp_Vec
        edge = BRepBuilderAPI_MakeEdge(gp_Elips(gp_Ax2(gp_Pnt(), gp_Dir(0, 0, 1)), 30, 20)).Edge()
        face = BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(edge).Wire()).Face()
        result = self.run_export(BRepPrimAPI_MakePrism(face, gp_Vec(0, 0, 2)).Shape(), {'status': 'candidate', 'value_mm': 2})
        self.assertEqual(result['status'], 'unsupported')
        self.assertEqual(result['contours'], [])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_budget_and_preexisting_files_do_not_publish_or_delete_other_artifacts(self):
        plate = self.box(100, 80, 2)
        result = self.run_export(plate, deadline=time.monotonic()-1)
        self.assertEqual(result['status'], 'unsupported')
        saved = self.folder/'.S0001-cut.dxf.tmp'
        saved.write_bytes(b'preserved-existing-artifact')
        result = self.run_export(plate)
        self.assertEqual(result['status'], 'unsupported')
        self.assertEqual(saved.read_bytes(), b'preserved-existing-artifact')

    def test_fontless_import_restores_environment_and_removes_temporary_cache_on_error(self):
        script = r'''
import os,sys,tempfile
from pathlib import Path
from unittest import mock
sys.path.insert(0,'src')
import cad_laser
made=[]
original=tempfile.TemporaryDirectory
def temporary(*a,**kw):
 value=original(*a,**kw);made.append(value.name);return value
cad_laser.tempfile.TemporaryDirectory=temporary
os.environ['XDG_CACHE_HOME']='hyl-sentinel-cache-no-write'
library=cad_laser._dxf_library()
assert library.__version__=='1.4.5'
assert len(library.fonts.fonts.font_manager._font_cache._cache)==0
assert os.environ['XDG_CACHE_HOME']=='hyl-sentinel-cache-no-write'
assert not Path('hyl-sentinel-cache-no-write').exists()
assert all(not Path(p).exists() for p in made)
sys.modules.pop('ezdxf')
original_import=__import__
def reject(name,*a,**kw):
 if name=='ezdxf':raise ImportError('intentional test')
 return original_import(name,*a,**kw)
with mock.patch('builtins.__import__',reject):
 try:cad_laser._dxf_library()
 except ImportError:pass
 else:raise AssertionError('Expected import rejection')
assert os.environ['XDG_CACHE_HOME']=='hyl-sentinel-cache-no-write'
assert all(not Path(p).exists() for p in made)
'''
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
