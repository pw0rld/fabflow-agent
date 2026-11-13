"""Quote estimates stay separate from production profiles and unknown routes."""
import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from cad_quote_quantities import (measure_quote_quantities,validate_quote_quantities,
                                  VERSION,QUANTITIES,_empty)

SHA='a'*64


def flat_receipt():
    return {'schema_version':1,'version':VERSION,'source_sha256':SHA,'solid_id':'S0001',
        'unit':'mm','status':'measured','method':'native_flat_profile','manufacturing_release':False,
        'material_area_mm2':'8000','material_gross_area_mm2':'8000','cut_length_mm':'360',
        'pierce_count':1,'bend_count':0,'bend_length_mm':'0','thickness_mm':'2',
        'blank_width_mm':'100','blank_height_mm':'80',
        'quantity_statuses':{name:'measured' for name in QUANTITIES},'source_face_ids':['F0001'],
        'assumptions':['每个完整轮廓暂按一次穿孔。'],'warnings':[],
        'measurements':{'native_volume_mm3':'16000'},
        'thickness_basis':{'source':'native_flat_prism','adopted_for_estimation':True},
        'unfolding':{'status':'not_required_flat','production_pattern':False}}


class QuoteReceiptTests(unittest.TestCase):
    def test_flat_receipt_and_unknown_receipt_validate_without_ocp(self):
        self.assertEqual(validate_quote_quantities(flat_receipt(),'S0001',SHA,6,volume_mm3=16000)['status'],'measured')
        self.assertEqual(validate_quote_quantities(_empty('S0001',SHA,'未完成'),'S0001',SHA,6)['bend_count'],None)

    def test_stale_sources_wrong_faces_and_fake_production_are_rejected(self):
        for changes in ({'source_sha256':'b'*64},{'solid_id':'S0002'},
                        {'manufacturing_release':True},{'source_face_ids':['F9999']},
                        {'pierce_count':True},{'bend_count':1},{'material_gross_area_mm2':'7900'}):
            with self.subTest(changes=changes):
                item=flat_receipt();item.update(changes)
                with self.assertRaises(ValueError):validate_quote_quantities(item,'S0001',SHA,6,volume_mm3=16000)

    def test_missing_quantity_cannot_be_marked_measured(self):
        item=flat_receipt();item['bend_count']=None
        with self.assertRaises(ValueError):validate_quote_quantities(item,'S0001',SHA,6)
        item=flat_receipt();item['method']='native_brep_inventory'
        with self.assertRaises(ValueError):validate_quote_quantities(item,'S0001',SHA,6)


NATIVE=importlib.util.find_spec('OCP') is not None


@unittest.skipUnless(NATIVE,'Native tests run only in the ZeroClave CAD runtime.')
class NativeQuoteQuantitiesTests(unittest.TestCase):
    def solid(self,shape):
        from OCP.BRepCheck import BRepCheck_Analyzer
        from OCP.BRep import BRep_Tool
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        from OCP.TopExp import TopExp
        from OCP.TopAbs import TopAbs_FACE,TopAbs_SHELL
        from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
        from cad_thickness import infer_thickness
        faces=IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher();TopExp.MapShapes_s(shape,TopAbs_FACE,faces)
        shells=IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher();TopExp.MapShapes_s(shape,TopAbs_SHELL,shells)
        area=GProp_GProps();volume=GProp_GProps()
        BRepGProp.SurfaceProperties_s(shape,area);BRepGProp.VolumeProperties_s(shape,volume)
        closed=shells.Extent()>0 and all(BRep_Tool.IsClosed_s(shells.FindKey(i)) for i in range(1,shells.Extent()+1))
        return {'id':'S0001','topology':{'valid':BRepCheck_Analyzer(shape).IsValid(),'closed':closed,'faces':faces.Extent()},
                'area_mm2':area.Mass(),'volume_mm3':volume.Mass(),'thickness':infer_thickness(shape)},faces

    def measure(self,shape):
        solid,faces=self.solid(shape)
        item=measure_quote_quantities(shape,solid,SHA,faces=faces)
        validate_quote_quantities(item,'S0001',SHA,faces.Extent(),area_mm2=solid['area_mm2'],volume_mm3=solid['volume_mm3'])
        return item

    def test_through_channels_count_real_one_and_two_hole_controls(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox,BRepPrimAPI_MakeCylinder
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.gp import gp_Ax2,gp_Dir,gp_Pnt
        shape=BRepPrimAPI_MakeBox(100,80,2).Shape()
        for count in (1,2):
            hole=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20*count,40,0),gp_Dir(0,0,1)),3,2).Shape()
            shape=BRepAlgoAPI_Cut(shape,hole).Shape();item=self.measure(shape)
            self.assertEqual(item['pierce_count'],count+1,item)
            self.assertEqual(item['measurements']['topological_through_channels'],count)
            self.assertIsNone(item['bend_count'],'An unproved flat profile cannot silently become zero bends.')
            self.assertIsNone(item['blank_width_mm'])

    def test_true_round_and_sharp_fold_controls_are_estimates(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
        from test_cad_bends import make_bend
        rounded=self.measure(make_bend())
        self.assertEqual(rounded['bend_count'],1,rounded)
        self.assertEqual(rounded['quantity_statuses']['bend_count'],'estimated')
        sharp=BRepAlgoAPI_Fuse(BRepPrimAPI_MakeBox(100,80,2).Shape(),BRepPrimAPI_MakeBox(2,80,30).Shape()).Shape()
        item=self.measure(sharp)
        self.assertEqual(item['bend_count'],1,item)
        self.assertEqual(item['measurements']['sharp_fold_junction_count'],1)
        self.assertFalse(item['manufacturing_release'])
        self.assertEqual(item['unfolding']['status'],'not_performed')

    def test_full_pipe_cannot_be_a_sheet_bend_and_budget_is_bounded(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        pipe=BRepAlgoAPI_Cut(BRepPrimAPI_MakeCylinder(7,40).Shape(),BRepPrimAPI_MakeCylinder(5,40).Shape()).Shape()
        item=self.measure(pipe);self.assertIsNone(item['bend_count'],item)
        solid,faces=self.solid(pipe)
        item=measure_quote_quantities(pipe,solid,SHA,faces=faces,deadline=-1)
        self.assertEqual(item['status'],'unresolved');self.assertIsNone(item['cut_length_mm'])

    def test_closed_junction_graph_does_not_turn_all_weld_seams_into_bends(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.gp import gp_Pnt
        shape=BRepAlgoAPI_Cut(BRepPrimAPI_MakeBox(100,80,30).Shape(),
            BRepPrimAPI_MakeBox(gp_Pnt(2,2,2),96,76,28).Shape()).Shape()
        item=self.measure(shape)
        self.assertGreater(item['measurements']['junction_graph_cycle_rank'],0,item)
        self.assertIsNone(item['bend_count']);self.assertIsNone(item['bend_length_mm'])
        self.assertEqual(item['measurements']['bend_route_status'],'route_unresolved')

    @unittest.skipUnless(importlib.util.find_spec('ezdxf') is not None,'Native DXF dependency absent.')
    def test_verified_flat_profile_reuses_exact_contours_and_proven_zero_bends(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from cad_laser import export_laser_cut
        shape=BRepPrimAPI_MakeBox(100,80,2).Shape();solid,faces=self.solid(shape)
        # Profile validation needs the true source-space bounds too.
        solid['bounds_mm']={'min':[0,0,0],'max':[100,80,2],'size':[100,80,2]}
        with tempfile.TemporaryDirectory() as folder:
            solid['laser_cut']=export_laser_cut(shape,faces,solid['thickness'],'S0001',Path(folder),SHA)
            item=measure_quote_quantities(shape,solid,SHA,faces=faces)
        self.assertEqual(item['method'],'native_flat_profile',item)
        self.assertEqual(item['status'],'measured');self.assertEqual(item['bend_count'],0)
        self.assertAlmostEqual(float(item['material_area_mm2']),8000)
        self.assertAlmostEqual(float(item['cut_length_mm']),360)
        validate_quote_quantities(item,'S0001',SHA,faces.Extent(),area_mm2=solid['area_mm2'],volume_mm3=solid['volume_mm3'])

    def test_inventory_receipt_cannot_hide_a_modified_path_or_fabricated_bend(self):
        from test_cad_bends import make_bend
        shape=make_bend();item=self.measure(shape);solid,faces=self.solid(shape)
        for field,value in (('cut_length_mm','999999'),('material_area_mm2','1'),('bend_count',99)):
            changed=copy.deepcopy(item);changed[field]=value
            with self.assertRaises(ValueError):validate_quote_quantities(changed,'S0001',SHA,faces.Extent(),area_mm2=solid['area_mm2'],volume_mm3=solid['volume_mm3'])


if __name__=='__main__':unittest.main()
