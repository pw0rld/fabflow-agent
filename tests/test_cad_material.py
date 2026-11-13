"""Real XCAF assignment and STEP roundtrip tests; no material-name inference."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from cad_material import MaterialReader
from cad_geometry import extract_geometry, quiet_native

NATIVE = importlib.util.find_spec('OCP') is not None


@unittest.skipUnless(NATIVE, 'Run with .venv-cad/bin/python for OCCT tests.')
class NativeMaterialTests(unittest.TestCase):
    def setUp(self):
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
        from OCP.TDocStd import TDocStd_Document
        from OCP.TCollection import TCollection_ExtendedString
        self.document = TDocStd_Document(TCollection_ExtendedString('XCAF'))
        self.shapes = XCAFDoc_DocumentTool.ShapeTool_s(self.document.Main())
        self.materials = XCAFDoc_DocumentTool.MaterialTool_s(self.document.Main())

    def plate(self, x=0):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.gp import gp_Pnt
        return BRepPrimAPI_MakeBox(gp_Pnt(x, 0, 0), 100, 80, 2).Shape()

    def material(self, name='Steel', description='Raw source description', density=7850):
        from OCP.TCollection import TCollection_HAsciiString as H
        return self.materials.AddMaterial(H(name), H(description), density, H('density'), H('kg/m3'))

    def assign(self, label, name='Steel', description='Raw source description'):
        self.materials.SetMaterial(label, self.material(name, description))

    def read(self, definition, occurrence=None, ancestors=(), shape=None):
        return MaterialReader(self.document, self.shapes).read(
            definition, occurrence or definition,
            shape if shape is not None else self.shapes.GetShape_s(definition), ancestors)

    def test_definition_preserves_raw_text_without_grade_mapping(self):
        part = self.shapes.AddShape(self.plate(), False)
        self.assign(part, 'Steel', 'Carbon steel; grade unspecified')
        value = self.read(part)
        self.assertEqual(value['status'], 'candidate')
        self.assertEqual(value['value'], 'Steel')
        self.assertEqual(value['entries'][0]['description'], 'Carbon steel; grade unspecified')
        self.assertEqual(value['entries'][0]['scope'], 'definition')
        self.assertNotIn('Q235', value['value'])

    def test_non_ascii_material_name_and_description_roundtrip(self):
        part = self.shapes.AddShape(self.plate(), False)
        name = '不锈钢：牌号未注明'
        description = '客户原文，待确认；不得推断为304。'
        self.assign(part, name, description)
        with tempfile.TemporaryDirectory(prefix='hyl-material-unicode-') as directory:
            source = Path(directory)/'unicode.step'
            self.write(source)
            result = extract_geometry(source, Path(directory)/'result')
        value = result['solids'][0]['material']
        self.assertEqual(value['value'], name)
        self.assertEqual(value['entries'][0]['name'], name)
        self.assertEqual(value['entries'][0]['description'], description)

    def test_global_unassigned_material_table_does_not_fill_missing_parts(self):
        self.material('Steel')
        part = self.shapes.AddShape(self.plate(), False)
        value = self.read(part)
        self.assertEqual(value['status'], 'unresolved')
        self.assertIsNone(value['value'])
        self.assertEqual(value['entries'], [])

    def test_repeated_instances_keep_own_assignment_and_conflict(self):
        from OCP.TopLoc import TopLoc_Location
        from OCP.gp import gp_Trsf, gp_Vec
        assembly = self.shapes.NewShape()
        part = self.shapes.AddShape(self.plate(), False)
        self.assign(part, 'Steel')
        one = self.shapes.AddComponent(assembly, part, TopLoc_Location())
        transform = gp_Trsf(); transform.SetTranslation(gp_Vec(200, 0, 0))
        two = self.shapes.AddComponent(assembly, part, TopLoc_Location(transform))
        self.assign(two, 'Aluminum')
        self.shapes.UpdateAssemblies()
        reader = MaterialReader(self.document, self.shapes)
        first = reader.read(part, one, self.shapes.GetShape_s(part))
        second = reader.read(part, two, self.shapes.GetShape_s(part))
        self.assertEqual(first['value'], 'Steel')
        self.assertEqual([e['scope'] for e in first['entries']], ['definition'])
        self.assertEqual(second['status'], 'unresolved')
        self.assertIsNone(second['value'])
        self.assertEqual({e['name'] for e in second['entries']}, {'Steel', 'Aluminum'})
        self.assertEqual({e['scope'] for e in second['entries']}, {'definition', 'occurrence'})
        with tempfile.TemporaryDirectory(prefix='hyl-material-instance-') as directory:
            source = Path(directory)/'instance.step'
            self.write(source)
            self.assertNotIn('Aluminum', source.read_text())
            result = extract_geometry(source, Path(directory)/'result')
        # The current OCCT writer cannot preserve a physical-material override
        # on an occurrence. Read only what survived in the actual STEP source.
        self.assertEqual([s['material']['value'] for s in result['solids']], ['Steel', 'Steel'])

    def test_partial_face_and_conflicting_face_are_not_whole_solid_material(self):
        from OCP.TopAbs import TopAbs_FACE
        from OCP.TopExp import TopExp_Explorer
        solid = self.plate()
        part = self.shapes.AddShape(solid, False)
        face = TopExp_Explorer(solid, TopAbs_FACE).Current()
        child = self.shapes.AddSubShape(part, face)
        self.assign(child, 'Aluminum')
        partial = self.read(part)
        self.assertEqual(partial['status'], 'unresolved')
        self.assertEqual(partial['entries'][0]['scope'], 'subshape')
        self.assign(part, 'Steel')
        conflict = self.read(part)
        self.assertEqual(conflict['status'], 'unresolved')
        self.assertEqual({e['name'] for e in conflict['entries']}, {'Steel', 'Aluminum'})

    def test_parent_assembly_material_alone_is_not_assigned_to_children(self):
        from OCP.TopLoc import TopLoc_Location
        assembly = self.shapes.NewShape()
        part = self.shapes.AddShape(self.plate(), False)
        occurrence = self.shapes.AddComponent(assembly, part, TopLoc_Location())
        self.assign(assembly, 'Steel')
        value = self.read(part, occurrence, (assembly,))
        self.assertEqual(value['status'], 'unresolved')
        self.assertIsNone(value['value'])
        self.assertEqual(value['entries'][0]['scope'], 'inherited')

    def test_matching_ancestor_is_only_supplemental_to_direct_assignment(self):
        from OCP.TopLoc import TopLoc_Location
        assembly = self.shapes.NewShape()
        part = self.shapes.AddShape(self.plate(), False)
        occurrence = self.shapes.AddComponent(assembly, part, TopLoc_Location())
        self.assign(assembly, 'Steel')
        self.assign(part, 'Steel')
        value = self.read(part, occurrence, (assembly,))
        self.assertEqual(value['status'], 'candidate')
        self.assertEqual(value['value'], 'Steel')
        self.assertEqual({e['scope'] for e in value['entries']}, {'definition', 'inherited'})

    def test_empty_name_or_density_never_supplies_a_material_name(self):
        part = self.shapes.AddShape(self.plate(), False)
        self.assign(part, '', 'Steel in description is not a declared material name')
        value = self.read(part)
        self.assertEqual(value['status'], 'unresolved')
        self.assertIsNone(value['value'])
        self.assertEqual(value['entries'][0]['name'], '')

    def test_oversized_material_is_unresolved_without_truncated_claim(self):
        part = self.shapes.AddShape(self.plate(), False)
        self.assign(part, 'M'*513)
        value = self.read(part)
        self.assertEqual(value['status'], 'unresolved')
        self.assertEqual(value['entries'], [])
        self.assertTrue(value['warnings'])

    def test_invalid_control_characters_only_affect_assigned_solid(self):
        from cad_bom import validate_material
        first = self.shapes.AddShape(self.plate(), False)
        second = self.shapes.AddShape(self.plate(200), False)
        self.assign(first, 'Steel', 'Original line\r\ncontains CR')
        self.assign(second, 'Copper', 'Valid line\nwith\ttab')
        reader = MaterialReader(self.document, self.shapes)
        invalid = reader.read(first, first, self.shapes.GetShape_s(first))
        valid = reader.read(second, second, self.shapes.GetShape_s(second))
        self.assertEqual(invalid['status'], 'unresolved')
        self.assertEqual(invalid['entries'], [])
        self.assertTrue(invalid['warnings'])
        self.assertEqual(valid['status'], 'candidate')
        self.assertEqual(valid['value'], 'Copper')
        self.assertEqual(valid['entries'][0]['description'], 'Valid line\nwith\ttab')
        validate_material(invalid)
        validate_material(valid)

    def write(self, path):
        from OCP.STEPCAFControl import STEPCAFControl_Writer
        from OCP.IFSelect import IFSelect_RetDone
        writer = STEPCAFControl_Writer()
        writer.SetMaterialMode(True)
        writer.SetNameMode(True)
        with quiet_native():
            self.assertTrue(writer.Transfer(self.document))
            self.assertEqual(writer.Write(str(path)), IFSelect_RetDone)

    def test_compound_subshape_mapping_and_export_loss_is_not_invented_back(self):
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Compound
        compound, builder = TopoDS_Compound(), BRep_Builder()
        builder.MakeCompound(compound)
        first, second, missing = self.plate(), self.plate(200), self.plate(400)
        for shape in (first, second, missing):
            builder.Add(compound, shape)
        part = self.shapes.AddShape(compound, False)
        self.assign(self.shapes.AddSubShape(part, first), 'Steel')
        self.assign(self.shapes.AddSubShape(part, second), 'Vendor exact material X')
        self.assertEqual(self.read(part, shape=first)['value'], 'Steel')
        self.assertEqual(self.read(part, shape=second)['value'], 'Vendor exact material X')
        self.assertIsNone(self.read(part, shape=missing)['value'])
        with tempfile.TemporaryDirectory(prefix='hyl-material-') as directory:
            source = Path(directory)/'compound.step'
            self.write(source)
            result = extract_geometry(source, Path(directory)/'result')
        solids = sorted(result['solids'], key=lambda s: s['bounds_mm']['min'][0])
        self.assertEqual(len(solids), 3)
        # This OCP STEP writer omits physical materials on compound subshapes.
        # The imported file stays missing; no neighbouring name is borrowed.
        self.assertEqual([s['material']['value'] for s in solids], [None, None, None])
        self.assertTrue(all(not s['material']['entries'] for s in solids))

    def test_step_roundtrip_repeated_definition_and_missing_keep_their_scope(self):
        from OCP.TopLoc import TopLoc_Location
        from OCP.gp import gp_Trsf, gp_Vec
        assembly = self.shapes.NewShape()
        part = self.shapes.AddShape(self.plate(), False)
        other = self.shapes.AddShape(self.plate(), False)
        missing = self.shapes.AddShape(self.plate(), False)
        self.assign(part, 'Steel')
        self.assign(other, 'Vendor exact material X')
        for index, definition in enumerate((part, part, other, missing)):
            transform = gp_Trsf(); transform.SetTranslation(gp_Vec(index*200, 0, 0))
            self.shapes.AddComponent(assembly, definition, TopLoc_Location(transform))
        self.shapes.UpdateAssemblies()
        with tempfile.TemporaryDirectory(prefix='hyl-material-') as directory:
            source = Path(directory)/'assembly.step'
            self.write(source)
            result = extract_geometry(source, Path(directory)/'result')
        solids = sorted(result['solids'], key=lambda s: s['bounds_mm']['min'][0])
        self.assertEqual([s['material']['value'] for s in solids], ['Steel', 'Steel', 'Vendor exact material X', None])
        self.assertEqual(solids[0]['definition_id'], solids[1]['definition_id'])
        self.assertNotEqual(solids[0]['occurrence_path'], solids[1]['occurrence_path'])
        self.assertEqual(solids[3]['material']['entries'], [])


if __name__ == '__main__':
    unittest.main()
