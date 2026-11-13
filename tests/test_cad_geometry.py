"""Real OCCT reference checks; skipped only when the isolated runtime is absent."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from cad_geometry import GeometryError,extract_geometry,quiet_native

NATIVE=importlib.util.find_spec('OCP') is not None


@unittest.skipUnless(NATIVE,'Run with HYL_CAD_PYTHON / .venv-cad for native OCCT checks.')
class NativeCadGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory(prefix='hyl-native-cad-')
        cls.folder=Path(cls.temporary.name)
        cls.source=ROOT/'samples/public-enclosure-20261026/input/Enclosure-v3-v9.step'
        cls.before=hashlib.sha256(cls.source.read_bytes()).hexdigest()
        cls.output=cls.folder/'enclosure'
        cls.geometry=extract_geometry(cls.source,cls.output)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def read_shape(self,path):
        from OCP.STEPControl import STEPControl_Reader
        from OCP.IFSelect import IFSelect_RetDone
        reader=STEPControl_Reader()
        with quiet_native():
            self.assertEqual(reader.ReadFile(str(path)),IFSelect_RetDone)
            reader.SetSystemLengthUnit(1.0)
            self.assertGreater(reader.TransferRoots(),0)
        return reader.OneShape()

    def solids(self,shape):
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopExp import TopExp
        from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
        values=IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
        TopExp.MapShapes_s(shape,TopAbs_SOLID,values)
        return [values.FindKey(i) for i in range(1,values.Extent()+1)]

    def bbox(self,shape):
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        box=Bnd_Box();BRepBndLib.AddOptimal_s(shape,box,False,False)
        return [box.GetXMin(),box.GetYMin(),box.GetZMin(),box.GetXMax(),box.GetYMax(),box.GetZMax()]

    def volume(self,shape):
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        props=GProp_GProps();BRepGProp.VolumeProperties_s(shape,props,True,False,False)
        return abs(props.Mass())

    def test_actual_source_identity_and_native_topology(self):
        result=self.geometry
        self.assertEqual(result['source_sha256'],self.before)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),self.before)
        self.assertEqual(result['source_units']['length_names'],['millimetre'])
        self.assertEqual(result['output_unit'],'mm')
        self.assertEqual(len(result['solids']),38)
        self.assertTrue(all(row['topology']['valid'] and row['topology']['closed'] for row in result['solids']))
        self.assertTrue(all(row['topology']['faces']>0 for row in result['solids']))
        self.assertEqual(len({row['id'] for row in result['solids']}),38)
        self.assertEqual(json.loads((self.output/'geometry.json').read_text()),result)

    def test_each_export_is_one_original_solid_with_same_volume_and_placement(self):
        # Read the original independently through STEPControl, not XCAF traversal.
        original=self.solids(self.read_shape(self.source))
        self.assertEqual(len(original),38)
        expected=sorted(tuple(round(v,4) for v in self.bbox(shape)) for shape in original)
        extracted=sorted(tuple(round(v,4) for v in row['bounds_mm']['min']+row['bounds_mm']['max']) for row in self.geometry['solids'])
        self.assertEqual(extracted,expected)
        for row in self.geometry['solids']:
            with self.subTest(solid=row['id']):
                shapes=self.solids(self.read_shape(self.output/row['step_file']))
                self.assertEqual(len(shapes),1)
                self.assertAlmostEqual(self.volume(shapes[0]),row['volume_mm3'],delta=max(1e-5,row['volume_mm3']*1e-5))
                self.assertLessEqual(row['export_validation']['volume_relative_delta'],1e-5)
                bounds=self.bbox(shapes[0])
                for measured,wanted in zip(bounds,row['bounds_mm']['min']+row['bounds_mm']['max']):
                    self.assertAlmostEqual(measured,wanted,delta=1e-5)
                self.assertIn(b'CERN-OHL-P v2',(self.output/row['step_file']).read_bytes())

    def test_repeated_definitions_keep_distinct_occurrences(self):
        rows=[row for row in self.geometry['solids'] if row['name']=='CornerBracket']
        self.assertEqual(len(rows),4)
        self.assertEqual(len({row['definition_id'] for row in rows}),1)
        self.assertEqual(len({tuple(row['occurrence_path']) for row in rows}),4)
        self.assertEqual(len({tuple(row['bounds_mm']['min']) for row in rows}),4)
        compounds=[row for row in self.geometry['solids'] if row['name'].startswith('MainEnclosure /')]
        self.assertEqual(len(compounds),5)
        self.assertEqual(len({row['definition_id'] for row in compounds}),5)

    def test_meshes_and_actual_thumbnail_artifacts(self):
        from PIL import Image
        for row in self.geometry['solids']:
            mesh=row['mesh'];self.assertEqual(len(mesh['positions'])%3,0);self.assertEqual(len(mesh['indices'])%3,0)
            self.assertTrue(all(0<=i<len(mesh['positions'])//3 for i in mesh['indices']))
            with Image.open(self.output/row['thumbnail']) as image:
                self.assertEqual(image.size,(360,300))
                self.assertGreater(len(image.getcolors(image.width*image.height)),10)
        self.assertEqual(len(self.geometry['contact_sheets']),5)

    def test_thickness_candidates_are_additive_unconfirmed_native_evidence(self):
        expected={'status','value_mm','method','confidence','reason','evidence','warnings'}
        for row in self.geometry['solids']:
            value=row['thickness']
            self.assertEqual(set(value),expected)
            if value['status']=='candidate':
                self.assertGreater(value['value_mm'],0)
                self.assertTrue(value['evidence'])
                for evidence in value['evidence']:
                    self.assertGreater(evidence['support_area_mm2'],0)
                    self.assertLessEqual(abs(evidence['thickness_mm']-value['value_mm']),max(.001,value['value_mm']*.001))
                    for face_id in evidence['face_ids']:
                        self.assertTrue(face_id.startswith('F'))
                        self.assertGreaterEqual(int(face_id[1:]),1)
                        self.assertLessEqual(int(face_id[1:]),row['topology']['faces'])
            else:
                self.assertIsNone(value['value_mm'])
                self.assertEqual(value['confidence'],'none')
        body=next(row for row in self.geometry['solids'] if row['id']=='S0001')
        self.assertEqual(body['thickness']['value_mm'],1.)
        self.assertGreater(min(body['bounds_mm']['size']),100.)
        tilted=next(row for row in self.geometry['solids'] if row['id']=='S0006')
        self.assertEqual(tilted['thickness']['value_mm'],1.)
        self.assertGreater(min(tilted['bounds_mm']['size']),2.)

    def test_material_assignments_do_not_spill_across_sibling_solids(self):
        by_id={row['id']:row for row in self.geometry['solids']}
        found=[row for row in self.geometry['solids'] if row['material']['status']=='candidate']
        self.assertEqual(len(found),18)
        self.assertEqual({row['material']['value'] for row in found},{'Steel'})
        self.assertEqual(by_id['S0001']['material']['entries'][0]['scope'],'subshape')
        self.assertEqual(by_id['S0023']['material']['entries'][0]['scope'],'subshape')
        for identity in ['S0002','S0003','S0004','S0005','S0024','S0025','S0026','S0027','S0028']:
            self.assertEqual(by_id[identity]['material']['status'],'unresolved')
            self.assertIsNone(by_id[identity]['material']['value'])
            self.assertEqual(by_id[identity]['material']['entries'],[])

    def test_explicit_metres_are_converted_to_millimetres(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.STEPControl import STEPControl_Writer,STEPControl_AsIs
        from OCP.IFSelect import IFSelect_RetDone
        path=self.folder/'metre-box.step'
        writer=STEPControl_Writer()
        with quiet_native():
            self.assertEqual(writer.Transfer(BRepPrimAPI_MakeBox(1,2,3).Shape(),STEPControl_AsIs),IFSelect_RetDone)
            self.assertEqual(writer.Write(str(path)),IFSelect_RetDone)
        data=path.read_text()
        self.assertIn('SI_UNIT(.MILLI.,.METRE.)',data)
        path.write_text(data.replace('SI_UNIT(.MILLI.,.METRE.)','SI_UNIT($,.METRE.)'))
        result=extract_geometry(path,self.folder/'metres')
        self.assertEqual(result['source_units']['length_names'],['metre'])
        self.assertEqual(result['solids'][0]['bounds_mm']['size'],[1000.,2000.,3000.])
        self.assertEqual(result['solids'][0]['volume_mm3'],6000000000.)

    def test_output_can_contain_input_but_existing_artifacts_are_preserved(self):
        output=self.folder/'with-input';output.mkdir()
        source=output/'input.step'
        shutil.copyfile(ROOT/'samples/cad-test-20261019/step/Nema-17_MountBracket.step',source)
        original=source.read_bytes()
        result=extract_geometry(source,output)
        self.assertEqual(len(result['solids']),1)
        self.assertEqual(source.read_bytes(),original)
        saved=(output/'geometry.json').read_bytes()
        with self.assertRaises(GeometryError):extract_geometry(source,output)
        self.assertEqual((output/'geometry.json').read_bytes(),saved)

    def test_invalid_format_does_not_create_geometry(self):
        source=self.folder/'invalid.step';source.write_bytes(b'not a CAD file')
        output=self.folder/'invalid'
        with self.assertRaises(GeometryError):extract_geometry(source,output)
        self.assertFalse((output/'geometry.json').exists())


if __name__=='__main__':unittest.main()
