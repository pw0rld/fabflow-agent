"""Full-face mesh recovery must not alter or silently omit source geometry."""
import copy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import cad_geometry as geometry


class Point:
    def __init__(self, xyz): self.xyz = xyz
    def Transformed(self, transform): return self
    def X(self): return self.xyz[0]
    def Y(self): return self.xyz[1]
    def Z(self): return self.xyz[2]


class TriangleMesh:
    def __init__(self, *, triangles=1, nodes=None):
        self.triangles = triangles
        self.nodes = nodes or [(0., 0., 0.), (1., 0., 0.), (0., 1., 0.)]
    def NbNodes(self): return len(self.nodes)
    def NbTriangles(self): return self.triangles
    def Node(self, index): return Point(self.nodes[index - 1])
    def Triangle(self, index): return SimpleNamespace(Get=lambda: (1, 2, 3))


class MeshRecoveryTests(unittest.TestCase):
    def setup_case(self, initial, recovered=None, *, done=True, fallback_error=None):
        state = {'phase': 0, 'calls': [], 'cleaned': 0}
        source = {'native_geometry': 'unchanged', 'volume': 2.5}
        faces = [SimpleNamespace(index=i, Orientation=lambda: 'forward') for i in range(len(initial))]
        face_map = SimpleNamespace(Extent=lambda: len(faces), FindKey=lambda i: faces[i-1])

        def mesher(shape, *args):
            state['calls'].append(args)
            state['phase'] = 0 if len(args) == 4 else 1
            if state['phase'] and fallback_error:
                raise fallback_error
            return SimpleNamespace(IsDone=lambda: done if not state['phase'] else True,
                                   GetStatusFlags=lambda: 4 if not state['phase'] and (not done or any(x is None for x in initial)) else 0)

        def clean(shape): state['cleaned'] += 1

        deps = {
            'BRepMesh_IncrementalMesh': mesher,
            'TopoDS': SimpleNamespace(Face=lambda value: value),
            'TopLoc_Location': lambda: SimpleNamespace(Transformation=lambda: 'identity'),
            'BRep_Tool': SimpleNamespace(Triangulation_s=lambda face, location: (initial if not state['phase'] else recovered)[face.index]),
            'TopAbs': SimpleNamespace(TopAbs_REVERSED='reversed'),
            'BRepTools': SimpleNamespace(Clean_s=clean),
            'IMeshTools_Parameters': SimpleNamespace,
            'IMeshTools_MeshAlgoType_Delabella': 'delabella',
        }
        return source, face_map, deps, state

    def test_complete_default_mesh_does_not_use_fallback_or_change_parameters(self):
        source, faces, deps, state = self.setup_case([TriangleMesh(), TriangleMesh()])
        before = copy.deepcopy(source)
        mesh, receipt = geometry._tessellate(source, faces, deps)
        self.assertEqual(source, before)
        self.assertEqual(state['calls'], [(0.15, False, 0.35, False)])
        self.assertEqual(state['cleaned'], 0)
        self.assertFalse(receipt['fallback'])
        self.assertEqual(receipt['algorithm'], 'default')
        self.assertEqual(receipt['covered_face_count'], 2)
        self.assertEqual(mesh['indices'], [0, 1, 2, 3, 4, 5])

    def test_missing_face_uses_whole_solid_alternative_with_same_precision(self):
        source, faces, deps, state = self.setup_case([TriangleMesh(), None], [TriangleMesh(), TriangleMesh()])
        before = copy.deepcopy(source)
        mesh, receipt = geometry._tessellate(source, faces, deps)
        self.assertEqual(source, before)
        self.assertEqual(state['cleaned'], 1)
        self.assertEqual(len(state['calls']), 2)
        settings = state['calls'][1][0]
        self.assertEqual((settings.Deflection, settings.Angle), (.15, .35))
        self.assertFalse(settings.Relative)
        self.assertFalse(settings.InParallel)
        self.assertFalse(settings.AllowQualityDecrease)
        self.assertEqual(settings.MeshAlgo, 'delabella')
        self.assertTrue(receipt['fallback'])
        self.assertEqual(receipt['source_face_count'], receipt['covered_face_count'])
        self.assertEqual(receipt['attempts'][0]['missing_face_ids'], ['F0002'])
        self.assertEqual(receipt['attempts'][1]['missing_face_ids'], [])
        self.assertEqual(len(mesh['indices']) // 3, 2)

    def test_is_done_false_is_not_mistaken_for_a_complete_success(self):
        source, faces, deps, state = self.setup_case([TriangleMesh()], [TriangleMesh()], done=False)
        _, receipt = geometry._tessellate(source, faces, deps)
        self.assertTrue(receipt['fallback'])
        self.assertFalse(receipt['attempts'][0]['is_done'])

    def test_recovery_still_missing_face_reports_id_without_skipping_it(self):
        source, faces, deps, state = self.setup_case([TriangleMesh(), None], [TriangleMesh(), None])
        with self.assertRaisesRegex(geometry.GeometryError, 'F0002'):
            geometry._tessellate(source, faces, deps)
        self.assertEqual(len(state['calls']), 2)

    def test_zero_triangle_face_does_not_count_as_covered(self):
        empty = TriangleMesh(triangles=0)
        source, faces, deps, _ = self.setup_case([empty], [empty])
        with self.assertRaisesRegex(geometry.GeometryError, 'F0001'):
            geometry._tessellate(source, faces, deps)

    def test_existing_mesh_budget_still_rejects_oversized_result(self):
        huge = TriangleMesh(triangles=geometry.MAX_TRIANGLES + 1)
        source, faces, deps, _ = self.setup_case([huge])
        with self.assertRaisesRegex(geometry.GeometryError, '处理上限'):
            geometry._tessellate(source, faces, deps)

    def test_nonfinite_mesh_coordinate_is_not_accepted(self):
        malformed = TriangleMesh(nodes=[(float('nan'), 0., 0.), (1., 0., 0.), (0., 1., 0.)])
        source, faces, deps, _ = self.setup_case([malformed])
        with self.assertRaises(geometry.GeometryError):
            geometry._tessellate(source, faces, deps)

    def test_fallback_has_bounded_time_and_no_repeated_attempt_loop(self):
        source, faces, deps, state = self.setup_case([None], [TriangleMesh()])
        with patch.object(geometry.time, 'monotonic', side_effect=[0., 0., 21.]), \
                self.assertRaisesRegex(geometry.GeometryError, '超时'):
            geometry._tessellate(source, faces, deps)
        self.assertEqual(len(state['calls']), 2)

    def test_fallback_native_error_remains_failure(self):
        source, faces, deps, _ = self.setup_case([None], [None], fallback_error=RuntimeError('native error'))
        with self.assertRaisesRegex(geometry.GeometryError, '备选网格生成失败'):
            geometry._tessellate(source, faces, deps)


@unittest.skipUnless(importlib.util.find_spec('OCP'), 'Requires the isolated ZeroClave OCCT runtime.')
class NativeMeshRecoveryTests(unittest.TestCase):
    def test_alternative_mesher_preserves_real_native_geometry(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        deps = geometry._imports()
        shape = BRepPrimAPI_MakeBox(10., 20., 2.).Shape()
        faces = deps['IndexedMap_TopoDS_Shape']()
        deps['TopExp'].MapShapes_s(shape, deps['TopAbs'].TopAbs_FACE, faces)

        def fingerprint():
            volume, area = deps['GProp_GProps'](), deps['GProp_GProps']()
            deps['BRepGProp'].VolumeProperties_s(shape, volume, True, False, False)
            deps['BRepGProp'].SurfaceProperties_s(shape, area, False, False)
            box = deps['Bnd_Box']()
            deps['BRepBndLib'].AddOptimal_s(shape, box, False, False)
            counts = []
            for kind in [deps['TopAbs'].TopAbs_FACE, deps['TopAbs'].TopAbs_EDGE, deps['TopAbs'].TopAbs_VERTEX]:
                mapped = deps['IndexedMap_TopoDS_Shape']()
                deps['TopExp'].MapShapes_s(shape, kind, mapped)
                counts.append(mapped.Extent())
            bounds = (box.GetXMin(), box.GetYMin(), box.GetZMin(),
                      box.GetXMax(), box.GetYMax(), box.GetZMax())
            return (volume.Mass(), area.Mass(), bounds, tuple(counts), bool(deps['BRepCheck_Analyzer'](shape).IsValid()))

        before = fingerprint()
        original = deps['BRepMesh_IncrementalMesh']
        calls = []

        def omit_first_mesh(source, *args):
            calls.append(args)
            if len(calls) == 1:
                return SimpleNamespace(IsDone=lambda: True, GetStatusFlags=lambda: 4)
            return original(source, *args)

        deps['BRepMesh_IncrementalMesh'] = omit_first_mesh
        mesh, receipt = geometry._tessellate(shape, faces, deps)
        self.assertTrue(receipt['fallback'])
        self.assertEqual(receipt['algorithm'], 'delabella')
        self.assertEqual(receipt['source_face_count'], 6)
        self.assertEqual(receipt['covered_face_count'], 6)
        self.assertGreater(len(mesh['indices']), 0)
        self.assertEqual(before, fingerprint())


if __name__ == '__main__':
    unittest.main()
