"""Analytic engineering quantities, source guards and bounded CAD receipts."""
import copy,json,math
from pathlib import Path
import sys,unittest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parent)]
from batch_process_evidence import summarise_native_processes
from batch_quote_service import BatchQuoteService
from test_cad_laser_contract import laser_profile, unsupported_profile
from test_cad_bend_contract import bend_profile, no_bends, BOUNDS

def plate():
    return {'id':'S0001','name':'Flat plate','definition_id':'plate/solid-1',
            'occurrence_path':['root','plate'], 'volume_mm3':(80-math.pi*.5**2)*1.5,
            'topology':{'valid':True,'closed':True,'faces':8},
            'bounds_mm':{'min':[0,0,0],'max':[10,8,1.5],'size':[10,8,1.5]},
            'laser_cut':laser_profile(),'bend_features':no_bends(),
            'mesh':{'positions':[0]*3000,'indices':[0,1,2]*1000}}

class ProcessEvidenceTests(unittest.TestCase):
    def test_exact_rectangle_circle_length_area_and_distinct_blank(self):
        value=summarise_native_processes(plate());laser=value['laser_cut']
        self.assertEqual(laser['status'],'ready')
        self.assertAlmostEqual(float(laser['cut_length_mm']),36+math.pi,places=7)
        self.assertAlmostEqual(float(laser['net_area_mm2']),80-math.pi*.5**2,places=7)
        self.assertEqual((laser['contour_count'],laser['pierce_count'],laser['holes_count']),(2,2,1))
        self.assertEqual(float(laser['blank_width_mm'])*float(laser['blank_height_mm']),80)
        self.assertIn('不是机床刀路',laser['piercing_basis'])

    def test_arcs_use_arc_sweep_instead_of_chord_length(self):
        solid=plate();profile=solid['laser_cut'];points=[[10,5],[5,10],[0,5],[5,0]]
        profile.update(bounds_mm={'min':[0,0],'max':[10,10],'size':[10,10]},holes=[])
        profile['contours']=[{'id':'C1','role':'outer','segments':[
            {'type':'arc','start':point,'end':points[(index+1)%4],'center':[5,5],
             'radius':5,'clockwise':False} for index,point in enumerate(points)]}]
        solid.update(volume_mm3=math.pi*25*1.5,bounds_mm={'min':[0,0,0],'max':[10,10,1.5],'size':[10,10,1.5]})
        laser=summarise_native_processes(solid)['laser_cut']
        self.assertEqual(laser['status'],'ready')
        self.assertAlmostEqual(float(laser['cut_length_mm']),10*math.pi,places=7)

    def test_volume_or_source_face_mismatch_cannot_become_billable_evidence(self):
        for change in ('volume','face'):
            solid=plate()
            if change=='volume':solid['volume_mm3']+=10
            else:solid['laser_cut']['source_face_id']='F9999'
            laser=summarise_native_processes(solid)['laser_cut']
            self.assertEqual(laser['status'],'unresolved');self.assertIsNone(laser['cut_length_mm'])

    def test_finished_envelope_does_not_create_a_flat_pattern(self):
        solid=plate();solid['laser_cut']=unsupported_profile()
        laser=summarise_native_processes(solid)['laser_cut']
        self.assertEqual(laser['status'],'unsupported')
        self.assertIsNone(laser['blank_width_mm']);self.assertIsNone(laser['cut_length_mm'])
        del solid['laser_cut']
        self.assertEqual(summarise_native_processes(solid)['laser_cut']['status'],'unresolved')

    def test_bend_candidates_keep_angle_radius_length_and_unconfirmed_direction(self):
        solid=plate();solid.update(bounds_mm=copy.deepcopy(BOUNDS),bend_features=bend_profile())
        solid.pop('laser_cut')
        value=summarise_native_processes(solid);bend=value['bends']['candidates'][0]
        self.assertEqual(value['bends']['candidate_count'],1)
        self.assertEqual((bend['rotation_angle_deg'],bend['inner_radius_mm'],bend['length_mm']),(90,1,4))
        self.assertEqual(bend['direction'],'unconfirmed');self.assertEqual(bend['status'],'pending_review')
        self.assertNotIn('mesh',json.dumps(value))

    def test_scalar_roundtrip_keeps_evidence_without_meshes_or_path_payloads(self):
        geometry={'source_sha256':'a'*64,'output_unit':'mm','solids':[plate()]}
        once=BatchQuoteService._measurements(geometry);twice=BatchQuoteService._measurements(once)
        self.assertEqual(once,twice)
        self.assertNotIn('mesh',json.dumps(once));self.assertLess(len(json.dumps(once)),6000)
        self.assertEqual(once['solids'][0]['process_evidence']['laser_cut']['status'],'ready')
        twice['solids'][0]['process_evidence']['solid_id']='S9999'
        wrong=BatchQuoteService._measurements(twice)
        self.assertEqual(wrong['solids'][0]['process_evidence']['laser_cut']['status'],'unresolved')

    def test_invalid_or_open_geometry_cannot_supply_manufacturing_quantities(self):
        for field in ('valid','closed'):
            solid=plate();solid['topology'][field]=False
            value=summarise_native_processes(solid)
            self.assertEqual(value['laser_cut']['status'],'unresolved')
            self.assertEqual(value['bends']['candidate_count'],0)

if __name__=='__main__':unittest.main()
