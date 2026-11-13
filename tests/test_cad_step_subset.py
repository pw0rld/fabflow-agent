"""STEP-byte subset protocol boundaries; native case acceptance is separate."""
from pathlib import Path
import importlib.util
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cad_step_subset import SourceStepSubset, SubsetError, masked


def product(base=0, name="same name"):
    def n(index): return base+index
    return f"""
#{n(1)}=APPLICATION_CONTEXT('mechanical');
#{n(2)}=PRODUCT_CONTEXT('',#{n(1)},'design');
#{n(3)}=PRODUCT('{name}','{name}','', (#{n(2)}));
#{n(4)}=PRODUCT_DEFINITION_FORMATION('','',#{n(3)});
#{n(5)}=PRODUCT_DEFINITION_CONTEXT('part',#{n(1)},'design');
#{n(6)}=PRODUCT_DEFINITION('part','',#{n(4)},#{n(5)});
#{n(7)}=PRODUCT_DEFINITION_SHAPE('','',#{n(6)});
#{n(8)}=CARTESIAN_POINT('',(1.234567890123456E-17,0.,0.));
#{n(9)}=DIRECTION('',(0.,0.,1.));
#{n(10)}=DIRECTION('',(1.,0.,0.));
#{n(11)}=AXIS2_PLACEMENT_3D('',#{n(8)},#{n(9)},#{n(10)});
#{n(12)}=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));
#{n(14)}=(GEOMETRIC_REPRESENTATION_CONTEXT(3)GLOBAL_UNIT_ASSIGNED_CONTEXT((#{n(12)}))REPRESENTATION_CONTEXT('',''));
#{n(15)}=CLOSED_SHELL('',());
#{n(16)}=MANIFOLD_SOLID_BREP('',#{n(15)});
#{n(17)}=ADVANCED_BREP_SHAPE_REPRESENTATION('',(#{n(11)},#{n(16)}),#{n(14)});
#{n(18)}=SHAPE_DEFINITION_REPRESENTATION(#{n(7)},#{n(17)});
""".encode()


def document(data):
    return b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('test #999; DATA;'),'2;1');\nENDSEC;\nDATA;\n"+data+b"ENDSEC;\nEND-ISO-10303-21;\n"


class StepSubsetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyl-step-subset-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.source = self.folder/'original.step'
        self.output = self.folder/'part.step'

    def model(self, data):
        self.source.write_bytes(document(data))
        return SourceStepSubset(self.source)

    def test_keeps_original_precision_units_and_one_product_only(self):
        model = self.model(product()+product(100)+b"#999=CARTESIAN_POINT('unrelated',(999.,0.,0.));\n")
        before = self.source.read_bytes()
        seen = []
        def validate(path):
            data = path.read_bytes(); seen.append(data)
            if b'#116=MANIFOLD_SOLID_BREP' in data:
                raise ValueError('native geometric mismatch')
            return {'solid_count': 1}
        result, provenance = model.write_matching(self.output, validate)
        data = self.output.read_bytes()
        self.assertEqual(result, {'solid_count': 1})
        self.assertEqual(len(seen), 2)
        self.assertIn(model.entities[8]['raw'], data)
        self.assertIn(b'1.234567890123456E-17', data)
        self.assertIn(b'SI_UNIT(.MILLI.,.METRE.)', data)
        self.assertNotIn(b'#116=', data)
        self.assertNotIn(b'#999=CARTESIAN_POINT', data)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(provenance['geometry_rebuilt'])
        self.assertEqual(provenance['source_root_entity'], 18)
        self.assertEqual(provenance['source_brep_entity'], 16)
        self.assertEqual(provenance['checked_candidates'], 2)
        self.assertEqual(list(self.folder.glob('.source-step-subsets-*')), [])

    def test_strings_escaped_quotes_and_comments_do_not_create_dependencies(self):
        model = self.model(product(name="label #888 ''quoted;DATA;'')")+b"/* #777; 'not a string' */\n")
        self.assertEqual(len(model.candidates()), 1)
        model.write_matching(self.output, lambda _: {'solid_count': 1})
        self.assertIn(b"label #888 ''quoted;DATA;'')", self.output.read_bytes())
        self.assertNotIn(888, model.closure(18))
        self.assertNotIn(777, model.closure(18))

    def test_two_native_matches_fail_closed_without_publishing_either(self):
        model = self.model(product()+product(100))
        with self.assertRaisesRegex(SubsetError, '未唯一匹配'):
            model.write_matching(self.output, lambda _: {'solid_count': 1})
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.folder.glob('.source-step-subsets-*')), [])

    def test_no_native_match_keeps_output_absent(self):
        model = self.model(product())
        def reject(_): raise ValueError('wrong volume/bounds')
        with self.assertRaises(SubsetError):
            model.write_matching(self.output, reject)
        self.assertFalse(self.output.exists())

    def test_second_brep_in_same_representation_is_never_a_part(self):
        data = product().replace(b'(#11,#16)', b'(#11,#16,#26)')+b'#26=MANIFOLD_SOLID_BREP(\'\',#15);\n'
        with self.assertRaises(SubsetError): self.model(data).candidates()

    def test_assembly_or_shape_aspect_root_is_not_accepted_as_product(self):
        data = product().replace(b"PRODUCT_DEFINITION('part','',#4,#5)", b"NEXT_ASSEMBLY_USAGE_OCCURRENCE('part','',#4,#5,$)")
        with self.assertRaises(SubsetError): self.model(data).candidates()

    def test_more_than_eight_candidates_rejects_before_native_validator(self):
        model = self.model(b''.join(product(i*100) for i in range(9)))
        with self.assertRaisesRegex(SubsetError, '超过保真导出预算'):
            model.write_matching(self.output, lambda _: self.fail('Native validation must not run'))

    def test_dangling_dependencies_duplicate_ids_and_multiple_data_sections_reject(self):
        for data in (product().replace(b'#15)', b'#999)'), product()+b'#16=MANIFOLD_SOLID_BREP(\'\',#15);\n',
                     product()+b'ENDSEC;DATA;'):
            with self.subTest(data=data[-60:]), self.assertRaises(SubsetError):
                self.model(data).candidates()

    def test_unclosed_strings_or_comments_are_rejected(self):
        for value in (b"'unclosed", b'/* unclosed'):
            with self.subTest(value=value), self.assertRaises(SubsetError): masked(value)

    def test_existing_target_and_source_symlink_reject_without_overwrite(self):
        model = self.model(product()); self.output.write_bytes(b'existing')
        with self.assertRaises(SubsetError): model.write_matching(self.output, lambda _: {})
        self.assertEqual(self.output.read_bytes(), b'existing')
        link = self.folder/'link.step'; link.symlink_to(self.source)
        with self.assertRaises(SubsetError): SourceStepSubset(link)

    def test_filesystem_failures_are_not_mislabeled_as_geometric_mismatches(self):
        model = self.model(product())
        with patch.object(Path, 'write_bytes', side_effect=PermissionError('cannot write')), self.assertRaises(PermissionError):
            model.write_matching(self.output, lambda _: {})
        self.assertFalse(self.output.exists())


class StepSubsetReceiptTests(unittest.TestCase):
    def setUp(self):
        import copy
        from test_cad_manufacturing import fixture_geometry
        self.sha = 'a'*64
        self.geometry = fixture_geometry(self.sha)
        self.solid = self.geometry['solids'][0]
        self.proof = {'method': 'source_step_entity_subset', 'geometry_rebuilt': False,
                      'source_sha256': self.sha, 'source_model_entities': 100, 'subset_entities': 40,
                      'checked_candidates': 3, 'source_root_entity': 40, 'source_brep_entity': 30}
        self.solid['export_validation'] = {'status': 'engineering_draft', 'solid_count': 1,
            'volume_relative_delta': 0, 'max_bound_delta_mm': 0,
            'volume_relative_limit': 1e-5, 'max_bound_delta_limit_mm': 1e-5,
            'extra_provenance': copy.deepcopy(self.proof)}

    def validate(self):
        from cad_manufacturing import validate_geometry
        return validate_geometry(self.geometry, self.sha)

    def test_strict_subset_receipt_is_valid_and_original_legacy_receipt_remains_compatible(self):
        self.assertIs(self.validate(), self.geometry)
        del self.solid['export_validation']['extra_provenance']
        self.assertIs(self.validate(), self.geometry)

    def test_subset_provenance_rejects_wrong_source_rebuilt_geometry_and_unbounded_candidates(self):
        import copy
        from prep_record import RecordError
        for key, value in (('method', 'guessed_part'), ('geometry_rebuilt', True),
                           ('source_sha256', 'b'*64), ('source_model_entities', 250001),
                           ('subset_entities', 101), ('checked_candidates', 9),
                           ('checked_candidates', True), ('source_root_entity', 0),
                           ('source_brep_entity', 40)):
            with self.subTest(key=key, value=value):
                self.solid['export_validation']['extra_provenance'] = {**copy.deepcopy(self.proof), key: value}
                with self.assertRaises(RecordError): self.validate()

    def test_provenance_cannot_override_fixed_volume_or_world_bound_limits(self):
        from prep_record import RecordError
        for field in ('volume_relative_delta', 'max_bound_delta_mm'):
            self.solid['export_validation'][field] = 1.00001e-5
            with self.assertRaises(RecordError): self.validate()
            self.solid['export_validation'][field] = 0


@unittest.skipUnless(importlib.util.find_spec('OCP') is not None, 'Requires isolated server native CAD')
class StepSubsetNativePlacementTests(unittest.TestCase):
    def test_same_volume_bounds_and_surface_counts_do_not_hide_a_moved_hole(self):
        import cad_geometry as G
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
        from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
        deps=G._imports()
        def shape_map(shape,kind):
            result=deps['IndexedMap_TopoDS_Shape']();deps['TopExp'].MapShapes_s(shape,kind,result);return result
        def panel(x):
            tool=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x,10,0),gp_Dir(0,0,1)),1,1).Shape()
            cut=BRepAlgoAPI_Cut(BRepPrimAPI_MakeBox(40,20,1).Shape(),tool)
            self.assertTrue(cut.IsDone());return cut.Shape()
        source,other=panel(10),panel(30)
        props=deps['GProp_GProps']();deps['BRepGProp'].VolumeProperties_s(source,props,True,False,False)
        item={'volume_mm3':abs(props.Mass()),'bounds_mm':{'min':[0,0,0],'max':[40,20,1]},
              'topology':{'faces':7,'surface_types':{'Plane':6,'Cylinder':1}}}
        fingerprints=G._solid_face_fingerprints(source,deps,shape_map)
        with tempfile.TemporaryDirectory(prefix='hyl-subset-native-hole-') as temporary:
            path=Path(temporary)/'moved-hole.step';writer=deps['STEPControl_Writer']()
            with G.quiet_native():
                self.assertEqual(writer.Transfer(other,deps['STEPControl_AsIs']),deps['IFSelect_RetDone'])
                self.assertEqual(writer.Write(str(path)),deps['IFSelect_RetDone'])
            receipt={'solid_count':None,'volume_relative_delta':None,'max_bound_delta_mm':None}
            with self.assertRaisesRegex(G.GeometryError,'逐面'):
                G._verify_solid_step(path,item,deps,shape_map,receipt,strict=True,source_fingerprints=fingerprints)
            self.assertEqual(receipt['solid_count'],1)
            self.assertLessEqual(receipt['volume_relative_delta'],1e-5)
            self.assertEqual(receipt['max_bound_delta_mm'],0)

    def test_same_volume_and_faces_cannot_hide_wrong_assembly_placement(self):
        import cad_geometry as G
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        deps=G._imports()
        with tempfile.TemporaryDirectory(prefix='hyl-subset-native-position-') as temporary:
            folder=Path(temporary)
            source=folder/'source.step'
            writer=deps['STEPControl_Writer']()
            with G.quiet_native():
                self.assertEqual(writer.Transfer(BRepPrimAPI_MakeBox(10,20,1).Shape(),deps['STEPControl_AsIs']),deps['IFSelect_RetDone'])
                self.assertEqual(writer.Write(str(source)),deps['IFSelect_RetDone'])
            model=SourceStepSubset(source)
            item={'volume_mm3':200.0,'bounds_mm':{'min':[20,0,0],'max':[30,20,1]},
                  'topology':{'faces':6,'surface_types':{'Plane':6}}}
            attempts=[]
            def shape_map(shape,kind):
                result=deps['IndexedMap_TopoDS_Shape']();deps['TopExp'].MapShapes_s(shape,kind,result);return result
            def verify(path):
                receipt={'solid_count':None,'volume_relative_delta':None,'max_bound_delta_mm':None}
                attempts.append(receipt)
                G._verify_solid_step(path,item,deps,shape_map,receipt,strict=True)
                return receipt
            with self.assertRaises(SubsetError):
                model.write_matching(folder/'wrong-position.step',verify)
            self.assertEqual(len(attempts),1)
            self.assertEqual(attempts[0]['solid_count'],1)
            self.assertLessEqual(attempts[0]['volume_relative_delta'],1e-5)
            self.assertGreater(attempts[0]['max_bound_delta_mm'],1e-5)
            self.assertFalse((folder/'wrong-position.step').exists())


class StepFaceMultisetTests(unittest.TestCase):
    def test_exact_duplicate_faces_match_one_to_one_but_distinct_near_matches_reject(self):
        import copy
        import cad_geometry as G
        face={'kind':'Plane','values':[1.0]*10}
        G._match_face_fingerprints([face,copy.deepcopy(face)],[copy.deepcopy(face),copy.deepcopy(face)])
        near=copy.deepcopy(face);near['values'][0]+=0.000005
        with self.assertRaisesRegex(G.GeometryError,'歧义'):
            G._match_face_fingerprints([face,copy.deepcopy(face)],[copy.deepcopy(face),near])


if __name__ == '__main__':
    unittest.main()
