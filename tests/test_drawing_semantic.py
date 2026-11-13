"""Generic review hints: no model calls and no replacement of source observations."""
import copy
import unittest
from drawing_split_service.semantic import annotate_manifest,annotate_parameter

class SemanticTests(unittest.TestCase):
    def test_legacy_label_is_only_hint_and_cannot_invent_direction(self):
        original={'id':'x','label':'纵向尺寸','value':[9,18,20],'unit':'mm','status':'pending_review','evidence':[{'region_id':'top','raw_text':'9 18 20'}]}
        before=copy.deepcopy(original);row=annotate_parameter(original,[])
        self.assertEqual(original,before);self.assertEqual(row['direction'],'unknown')
        self.assertEqual(row['category_source'],'label_hint')
        self.assertEqual(row['value'],[9,18,20]);self.assertEqual(row['status'],'pending_review')
        self.assertTrue({'multiple_values','direction_missing','region_location_only'}.issubset({c['code'] for c in row['checks']}))

    def test_overlap_hints_are_generic_without_reassigning_evidence(self):
        regions=[{'id':'a','type':'orthographic','bbox':[0,0,100,100]},{'id':'b','type':'orthographic','bbox':[0,90,100,190]}]
        row={'label':'圆角半径','value':8,'unit':'mm','evidence':[{'region_id':'a','raw_text':'R8 TYP'},{'region_id':'b','raw_text':'R8 TYP'}]}
        result=annotate_parameter(row,regions)
        self.assertIn('overlapping_view_sources',{x['code'] for x in result['checks']})
        self.assertEqual(result['evidence'],row['evidence'])
        regions[1]['bbox']=[0,110,100,210]
        self.assertNotIn('overlapping_view_sources',{x['code'] for x in annotate_parameter(row,regions)['checks']})

    def test_model_direction_is_retained_but_never_auto_confirms(self):
        row={'label':'底板孔中心到左边距离','value':25,'unit':'mm','direction':'horizontal','category':'hole','feature':'底板安装孔','datum':'左外边','status':'pending_review','evidence':[{'region_id':'a','raw_text':'25','bbox':[1,2,5,6]}]}
        result=annotate_parameter(row,[])
        self.assertEqual(result['direction'],'horizontal');self.assertEqual(result['datum'],'左外边')
        self.assertEqual(result['location_precision'],'annotation');self.assertEqual(result['status'],'pending_review')
        self.assertNotIn('datum_missing',{x['code'] for x in result['checks']})

    def test_missing_value_and_units_remain_unknown_without_zero(self):
        result=annotate_parameter({'label':'孔中心坐标','value':None,'unit':None,'evidence':[]},[])
        self.assertIsNone(result['value']);self.assertIsNone(result['unit']);self.assertIsNone(result['datum'])
        self.assertTrue({'value_missing','unit_missing','datum_missing'}.issubset({x['code'] for x in result['checks']}))

    def test_reading_classification_and_checks_do_not_approve_manifest(self):
        source={'parameters':[{'label':'板厚','value':2,'unit':'mm','status':'pending_review'}],'regions':[],'engineering_approved':True}
        before=copy.deepcopy(source);result=annotate_manifest(source)
        self.assertEqual(source,before);self.assertFalse(result['engineering_approved'])
        self.assertEqual(result['parameters'][0]['category'],'thickness')
        self.assertEqual(result['parameters'][0]['status'],'pending_review')

if __name__=='__main__':
    import platform
    if platform.system()!='Linux':raise SystemExit('ZeroClave only per AGENTS.md')
    unittest.main()
