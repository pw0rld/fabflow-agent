"""ZeroClave-only review API tests; fake adapter, no model/network provider."""
from __future__ import annotations
import copy
import csv
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
import zipfile
from PIL import Image,ImageChops
from drawing_split_service.service import SplitService,TERMINAL
from drawing_split_service.server import SplitHTTPServer

ORIGIN='http://127.0.0.1:3197'
def observation():
    return {'coordinate_system':'source_pixels','document_kind':'engineering_drawing','analysis_complete':True,
      'parts':[{'id':'part','name':'TEST PLATE','raw_text':'TEST PLATE'}],
      'regions':[{'id':'left','title':'测试主视图','type':'orthographic','part_id':'part','bbox':[4,4,50,65]},
                 {'id':'right','title':'测试说明','type':'technical_notes','part_id':'part','bbox':[54,4,96,70]}],
      'parameters':[{'id':'thickness','label':'板厚','value':2,'unit':'mm','category':'thickness','direction':'not_applicable','basis':'drawing_text','evidence':[{'region_id':'right','raw_text':'t=2 mm','bbox':[60,10,90,20]}]},
                    {'id':'holes','label':'孔数','value':4,'unit':None,'category':'hole','direction':'unknown','basis':'drawing_text','evidence':[{'region_id':'left','raw_text':'4 holes'}]},
                    {'id':'text','label':'测试文字','value':'=SAMPLE()','unit':None,'basis':'drawing_text','evidence':[{'region_id':'right','raw_text':'=SAMPLE()'}]}],
      'process_notes':[],'bend_rows':[]}

def png_bytes():
    source=Image.new('RGB',(100,80),'white')
    for xy,color in [((12,12),(10,40,90)),((38,30),(200,10,20)),((62,12),(30,140,60))]:source.putpixel(xy,color)
    output=io.BytesIO();source.save(output,format='PNG');return output.getvalue()

class FakeAdapter:
    model_info={'provider':'fixture','model':'fake-no-external-model'}
    def __init__(self):self.calls=0
    def analyze(self,path,width,height,progress):self.calls+=1;return copy.deepcopy(observation())

class ReviewHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'jobs';self.adapter=FakeAdapter()
        self.service=SplitService(self.root,self.adapter)
        self.http=SplitHTTPServer(('127.0.0.1',0),self.service,[ORIGIN]);self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.data=png_bytes();code,_,created=self.call('POST','/jobs?name=review-fixture.png',self.data,upload=True)
        self.assertEqual(code,202);self.job=created['job_id']
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            code,_,self.base=self.call('GET',self.path())
            if self.base['status'] in TERMINAL and self.job not in self.service.pending:break
            time.sleep(.01)
        self.assertEqual(self.base['status'],'succeeded')
        self.base_manifest=copy.deepcopy(self.base['manifest']);self.param=self.base_manifest['parameters'][0]['id'];self.other=self.base_manifest['parameters'][1]['id'];self.region=self.base_manifest['regions'][0]['id'];self.notes=self.base_manifest['regions'][1]['id']
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join(timeout=2);self.service.close();self.temp.cleanup()
    def path(self,suffix=''):return '/jobs/'+getattr(self,'job','')+suffix
    def call(self,method,path,payload=None,*,upload=False,headers=None):
        headers=dict(headers or {})
        if method=='POST':
            headers={'Origin':ORIGIN,'X-Drawing-Split-Request':'1','X-Request-Id':str(uuid.uuid4()),'Content-Type':'image/png' if upload else 'application/json',**headers}
        body=payload if upload else json.dumps(payload).encode() if payload is not None else None
        conn=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=6)
        try:
            conn.request(method,path,body=body,headers=headers);response=conn.getresponse();data=response.read();result=dict(response.getheaders())
            if result.get('Content-Type','').startswith('application/json'):data=json.loads(data)
            return response.status,result,data
        finally:conn.close()
    def review(self):
        code,_,value=self.call('GET',self.path('/review'));self.assertEqual(code,200);return value
    def change(self,kind,identifier,patch=None,*,revision=None,attempt=None,request_id=None):
        current=self.review();payload={'request_id':request_id or str(uuid.uuid4()),'expected_revision':current['revision'] if revision is None else revision,'expected_attempt':current['attempt'] if attempt is None else attempt,'action':{'type':kind,'id':identifier}}
        if patch is not None:payload['action']['patch']=patch
        return self.call('POST',self.path('/review'),payload),payload
    def parameter(self,review,identifier=None):return next(p for p in review['parameters'] if p['id']==(identifier or self.param))
    def test_edit_is_persistent_and_keeps_original_candidates_source_and_model_calls(self):
        initial=self.review();self.assertEqual(initial['revision'],0)
        self.assertTrue(all(p['status']=='pending_review' for p in initial['parameters']))
        before_raw=(self.root/self.job/'attempts'/'a001'/'model-output.json').read_bytes()
        (code,_,saved),_=self.change('parameter',self.param,{'value':3,'direction':'unknown','datum':'板面','feature':'母材厚度','note':'由操作者校正'})
        self.assertEqual(code,200);self.assertEqual(saved['revision'],1)
        self.assertEqual(self.parameter(saved)['value'],3);self.assertEqual(self.parameter(saved)['status'],'pending_review')
        self.assertEqual(self.parameter(self.review())['value'],3)
        self.assertEqual(self.parameter(saved)['original_candidate']['value'],2)
        self.assertEqual(self.parameter(saved)['original_candidate']['evidence'],self.base_manifest['parameters'][0]['evidence'])
        self.assertEqual(self.service.get(self.job)['manifest'],self.base_manifest)
        self.assertEqual((self.root/self.job/'attempts'/'a001'/'model-output.json').read_bytes(),before_raw)
        self.assertEqual(self.service.file(self.job,'source.png')[0].read_bytes(),self.data)
        self.assertEqual(self.adapter.calls,1);self.assertFalse(saved['engineering_approved'])
        self.assertEqual(len(saved['history']),1)
    def test_confirm_only_selected_parameter_question_and_reset_keep_history(self):
        (code,_,saved),_=self.change('parameter',self.param,{'status':'checked','note':'逐项核对原图'})
        self.assertEqual(code,200);self.assertEqual(self.parameter(saved)['status'],'checked');self.assertEqual(self.parameter(saved,self.other)['status'],'pending_review')
        (code,_,edited),_=self.change('parameter',self.param,{'value':3})
        self.assertEqual(code,200);self.assertEqual(self.parameter(edited)['status'],'pending_review')
        (code,_,questioned),_=self.change('parameter',self.param,{'status':'questioned','note':'方向未明'})
        self.assertEqual(code,200);self.assertEqual(self.parameter(questioned)['status'],'questioned')
        (code,_,reset),_=self.change('reset_parameter',self.param)
        self.assertEqual(code,200);self.assertEqual(self.parameter(reset)['value'],2);self.assertEqual(self.parameter(reset)['status'],'pending_review')
        self.assertEqual(len(reset['history']),4);self.assertFalse(reset['engineering_approved']);self.assertEqual(self.adapter.calls,1)
    def test_service_restart_recovers_saved_review_without_dispatching_adapter(self):
        (code,_,saved),_=self.change('parameter',self.param,{'value':3,'status':'checked','note':'重启前已核对本项'})
        self.assertEqual(code,200)
        self.http.shutdown();self.http.server_close();self.thread.join(timeout=2);self.service.close()
        recovered_adapter=FakeAdapter();self.service=SplitService(self.root,recovered_adapter,start_worker=False)
        self.http=SplitHTTPServer(('127.0.0.1',0),self.service,[ORIGIN]);self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        recovered=self.review()
        self.assertEqual(recovered['revision'],saved['revision']);self.assertEqual(len(recovered['history']),1)
        self.assertEqual(self.parameter(recovered)['value'],3);self.assertEqual(self.parameter(recovered)['status'],'checked')
        self.assertEqual(recovered_adapter.calls,0)
    def test_adjusted_region_invalidates_only_associated_checked_parameters(self):
        self.change('parameter',self.param,{'status':'checked','note':'核对说明区板厚'})
        self.change('parameter',self.other,{'status':'checked','note':'核对主视图孔数'})
        (code,_,saved),_=self.change('region',self.region,{'bbox':[8,8,45,55]})
        self.assertEqual(code,200);self.assertEqual(self.parameter(saved,self.other)['status'],'pending_review')
        self.assertEqual(self.parameter(saved)['status'],'checked')
        self.assertEqual(self.parameter(saved,self.other)['review_invalidated_by_region']['region_id'],self.region)
        event=saved['history'][-1];self.assertEqual(len(event['invalidated_parameters']),1)
        self.assertEqual(event['invalidated_parameters'][0]['before']['status'],'checked')
        self.assertEqual(event['invalidated_parameters'][0]['after']['status'],'pending_review')
        (code,_,rechecked),_=self.change('parameter',self.other,{'status':'checked','note':'新边界下重新核对本项'})
        self.assertEqual(code,200);self.assertEqual(self.parameter(rechecked,self.other)['status'],'checked')
        self.assertFalse(self.parameter(rechecked,self.other).get('review_invalidated_by_region'))
    def test_idempotency_replay_does_not_append_history_and_conflicting_payload_is_rejected(self):
        (code,_,saved),payload=self.change('parameter',self.param,{'value':3})
        self.assertEqual(code,200)
        status,_,replay=self.call('POST',self.path('/review'),payload)
        self.assertEqual(status,200);self.assertEqual(replay['revision'],saved['revision']);self.assertEqual(len(replay['history']),1)
        payload['action']['patch']['value']=4
        self.assertEqual(self.call('POST',self.path('/review'),payload)[0],409)
        self.assertEqual(self.parameter(self.review())['value'],3)
    def test_revision_attempt_unknown_ids_and_unsupported_values_cannot_mutate(self):
        self.change('parameter',self.param,{'value':3})
        self.assertEqual(self.change('parameter',self.param,{'value':4},revision=0)[0][0],409)
        self.assertEqual(self.change('parameter',self.param,{'value':4},attempt=99)[0][0],409)
        self.assertIn(self.change('parameter','unknown-id',{'value':4})[0][0],(400,404,422))
        for patch in ({'value':{'arbitrary':'object'}},{'status':'approved'},{'direction':'invented-direction'},{'evidence':[{'raw_text':'replace original'}]}):
            with self.subTest(patch=patch):self.assertIn(self.change('parameter',self.param,patch)[0][0],(400,422))
        self.assertEqual(self.review()['revision'],1);self.assertEqual(self.parameter(self.review())['value'],3)
    def test_reassociation_retains_original_evidence_without_claiming_new_source_text(self):
        (code,_,saved),_=self.change('parameter',self.param,{'region_id':self.region,'note':'归属改为主视图，原文仍待核对'})
        self.assertEqual(code,200);param=self.parameter(saved)
        self.assertEqual(param['original_candidate']['evidence'],self.base_manifest['parameters'][0]['evidence'])
        self.assertEqual(param['status'],'pending_review')
        self.assertEqual(param['region_id'],self.region)
        self.assertNotEqual(param['original_candidate']['evidence'][0]['region_id'],self.region)
    def test_region_adjustment_recrops_source_pixels_and_preserves_previous_file(self):
        original=self.base_manifest['regions'][0];old_name=original['file'].rsplit('/',1)[-1];old_path,_=self.service.file(self.job,old_name);old_bytes=old_path.read_bytes()
        (code,_,saved),_=self.change('region',self.region,{'bbox':[8,8,45,55],'title':'主视图已调框'})
        self.assertEqual(code,200);region=next(r for r in saved['regions'] if r['id']==self.region)
        self.assertEqual(region['bbox'],[8,8,45,55]);self.assertNotEqual(region['file'],original['file'])
        new_path,_=self.service.file(self.job,region['file'].rsplit('/',1)[-1])
        with Image.open(io.BytesIO(self.data)) as source,Image.open(new_path) as image:
            self.assertEqual(image.size,(37,47));self.assertIsNone(ImageChops.difference(image,source.crop((8,8,45,55))).getbbox())
        self.assertEqual(old_path.read_bytes(),old_bytes)
        self.assertEqual(self.service.get(self.job)['manifest']['regions'][0],original)
        self.assertEqual(self.adapter.calls,1)
        (code,_,reset),_=self.change('reset_region',self.region)
        self.assertEqual(code,200);self.assertEqual(next(r for r in reset['regions'] if r['id']==self.region)['bbox'],original['bbox'])
    def test_invalid_region_bounds_and_pending_job_are_rejected(self):
        for box in ([0,0,999,80],[50,0,40,30],[0,0,0,0],[True,0,20,30],[0,0,20.5,30]):
            with self.subTest(box=box):self.assertIn(self.change('region',self.region,{'bbox':box})[0][0],(400,422))
        self.assertEqual(self.review()['revision'],0)
        stored=self.service._load(self.job);stored['status']='running';self.service._save(stored)
        self.assertEqual(self.call('POST',self.path('/review'),{'request_id':str(uuid.uuid4()),'expected_revision':0,'expected_attempt':1,'action':{'type':'parameter','id':self.param,'patch':{'value':3}}})[0],409)
    def test_retry_new_attempt_does_not_apply_old_review_but_keeps_history_and_files(self):
        self.change('parameter',self.param,{'value':3,'status':'checked','note':'本次人工核对'})
        old=self.review();self.service.retry(self.job,'new-attempt-request-000001')
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            value=self.service.get(self.job)
            if value['status'] in TERMINAL and self.job not in self.service.pending:break
            time.sleep(.01)
        self.assertEqual(value['attempt'],2);current=self.review()
        self.assertEqual(current['attempt'],2);self.assertEqual(self.parameter(current)['value'],2);self.assertEqual(self.parameter(current)['status'],'pending_review')
        self.assertEqual(self.change('parameter',self.param,{'value':9},attempt=old['attempt'],revision=old['revision'])[0][0],409)
        # At least the immutable previous-attempt overlay file remains private.
        review_files=list((self.root/self.job).rglob('*review*.json'))
        self.assertTrue(review_files);self.assertEqual(self.adapter.calls,2)
    def test_zip_has_effective_values_crops_original_and_formula_safe_csv_without_model_calls(self):
        (code,_,saved),_=self.change('parameter',self.param,{'value':3,'note':'export-test'})
        self.assertEqual(code,200)
        status,headers,payload=self.call('GET',self.path('/export')+'?revision='+str(saved['revision'])+'&attempt='+str(saved['attempt']))
        self.assertEqual(status,200);self.assertIn('application/zip',headers['Content-Type']);self.assertIn('attachment',headers.get('Content-Disposition',''))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names=archive.namelist();self.assertTrue(all(not n.startswith('/') and '..' not in n.split('/') and '\\' not in n for n in names))
            json_names=[n for n in names if n.endswith('.json')];self.assertTrue(json_names)
            manifests=[json.loads(archive.read(n)) for n in json_names]
            effective=next(item for item in manifests if isinstance(item,dict) and isinstance(item.get('parameters'),list))
            self.assertEqual(next(p for p in effective['parameters'] if p['id']==self.param)['value'],3);self.assertFalse(effective['engineering_approved'])
            self.assertTrue(any(archive.read(n)==self.data for n in names if not n.endswith('/')))
            self.assertGreaterEqual(len([n for n in names if n.lower().endswith('.png')]),3)
            csv_name=next(n for n in names if n.endswith('.csv'));csv_bytes=archive.read(csv_name);self.assertTrue(csv_bytes.startswith(b'\xef\xbb\xbf'))
            rows=list(csv.reader(io.StringIO(csv_bytes.decode('utf-8-sig'))));self.assertGreaterEqual(len(rows),4)
            cells=[cell for row in rows for cell in row]
            self.assertNotIn('=SAMPLE()',cells);self.assertIn("'=SAMPLE()",cells)
        self.assertEqual(self.call('GET',self.path('/export')+'?revision=0&attempt=1')[0],409)
        self.assertEqual(self.adapter.calls,1)
    def test_cross_origin_review_writes_remain_blocked_and_no_private_files_are_served(self):
        payload={'request_id':str(uuid.uuid4()),'expected_revision':0,'expected_attempt':1,'action':{'type':'parameter','id':self.param,'patch':{'value':9}}}
        status,headers,_=self.call('POST',self.path('/review'),payload,headers={'Origin':'https://unrelated.test'})
        self.assertEqual(status,403);self.assertNotIn('Access-Control-Allow-Origin',headers);self.assertEqual(self.review()['revision'],0)
        for filename in ('review.json','review-a001.json','model-output.json'):
            self.assertEqual(self.call('GET',self.path('/files/'+filename))[0],404)

if __name__=='__main__':
    import platform
    if platform.system()!='Linux':raise SystemExit('Run only on ZeroClave, per AGENTS.md')
    unittest.main()
