import unittest
from engine import adapt, evaluate, compare

class ScoringTests(unittest.TestCase):
    def rule(self, **kw):
        return dict(id='t', track='parameters', status='engineering_verified', path='parameters.0.value', expected=2, comparison='exact', evidence=['fixture'], **kw)
    def test_missing_is_not_zero(self):
        report=evaluate({'parameters': []}, {'assertions':[self.rule()]})
        self.assertEqual(report['tracks']['parameters']['official']['missing'],1)
    def test_provisional_not_official(self):
        rule=self.rule(); rule['status']='provisional'
        r=evaluate({'parameters':[{'value':2}]},{'assertions':[rule]})
        self.assertIsNone(r['tracks']['parameters']['official']['assertion_accuracy'])
        self.assertEqual(r['tracks']['parameters']['development']['matched'],1)
    def test_dispute_excluded(self):
        rule=self.rule(); rule['status']='disputed'
        r=evaluate({}, {'assertions':[rule]})
        self.assertEqual(r['checks'][0]['verdict'],'excluded_pending_adjudication')
    def test_mesh_not_bom(self):
        r=adapt({'meshes':[{},{}]},'step_preview')
        self.assertEqual(r['bom'],[])
        self.assertEqual(r['diagnostics']['display_mesh_count'],2)
    def test_bool_not_number(self):
        self.assertFalse(compare(True,1,{'comparison':'numeric'}))
    def test_numeric_tolerance(self):
        self.assertTrue(compare('2.01',2,{'comparison':'numeric','tolerance':0.02}))
    def test_raw_retained(self):
        raw={'parameters':[{'value':None,'evidence':[{'raw_text':'?'}]}]}
        self.assertEqual(adapt(raw,'image')['raw'],raw)
    def test_wrong(self):
        r=evaluate({'parameters':[{'value':8}]},{'assertions':[self.rule()]})
        self.assertEqual(r['tracks']['parameters']['official']['wrong'],1)

if __name__=='__main__': unittest.main()

class CollectionTests(unittest.TestCase):
    def test_duplicate_penalty(self):
        from engine import score_collection
        r=score_collection([{'part':'a'},{'part':'a'}],dict(items=[{'part':'a'}],keys=['part'],coverage='exhaustive'))
        self.assertEqual(r['extra'],1)
    def test_partial_no_precision(self):
        from engine import score_collection
        r=score_collection([{'id':1}],dict(items=[],keys=['id'],coverage='partial'))
        self.assertIsNone(r['precision'])
    def test_iou(self):
        from engine import iou
        self.assertEqual(iou([0,0,10,10],[0,0,10,10]),1)
    def test_invalid_schema(self):
        from engine import validate
        from jsonschema import ValidationError
        with self.assertRaises(ValidationError): validate({},'ground-truth')
