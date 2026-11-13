"""Run on ZeroClave only. These tests use a fake model and tiny generated images."""
from __future__ import annotations
import copy
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from PIL import Image
from drawing_split_service.contract import normalize, ContractError
from drawing_split_service.service import SplitService, ServiceError, TERMINAL
from drawing_split_service.server import SplitHTTPServer


def png_bytes():
    output=io.BytesIO()
    image=Image.new('RGB',(100,80),'white')
    image.putpixel((12,12),(14,42,90))
    image.save(output,format='PNG')
    return output.getvalue()


def observation():
    return {'coordinate_system':'normalized_1000','document_kind':'engineering_drawing',
            'parts':[{'id':'part-A','name':'测试钣金件','raw_text':'TEST PLATE'}],
            'regions':[{'id':'top','type':'orthographic','title':'俯视图','part_id':'part-A','bbox':[100,125,600,750]},
                       {'id':'notes','type':'technical_notes','title':'技术要求','part_id':'part-A','bbox':[650,125,950,500]}],
            'parameters':[{'label':'板厚','value':None,'unit':'mm','basis':'uncertain',
                           'evidence':[{'region_id':'notes','raw_text':'Thickness: illegible'}]}],
            'process_notes':[],'bend_rows':[]}


class FakeAdapter:
    model_info={'provider':'test','model':'fake-vision'}
    def __init__(self,result=None,error=None):
        self.calls=0;self.result=result or observation();self.error=error
    def analyze(self,path,width,height,progress):
        self.calls+=1
        progress('extracting','Ignore provider URL https://private.example.test')
        if self.error:raise self.error
        return copy.deepcopy(self.result)


def wait_terminal(service,job_id):
    deadline=time.monotonic()+8
    while time.monotonic()<deadline:
        result=service.get(job_id)
        # Worker releases queue slot in its finally block immediately afterwards.
        if result['status'] in TERMINAL and job_id not in service.pending:return result
        time.sleep(.01)
    raise AssertionError('test job did not finish')


class ContractTests(unittest.TestCase):
    def normalize(self,raw):
        return normalize(raw,width=100,height=80,source={'filename':'input.png'},files_base='api/jobs/test/files/',filename_prefix='a001-')

    def test_invalid_regions_and_unknown_observations_are_isolated(self):
        raw=observation()
        raw['regions'][0].update(type='future_view_type',bbox=[-1,125,600,750])
        raw['regions']+=[{'id':'out','type':'orthographic','bbox':[2000,0,3000,500]}]
        raw['parameters']+=[{'label':'未知自定义参数','value':'X','arbitrary_new_key':1,
                             'evidence':[{'region_id':'top','raw_text':'X'}]},
                            {'label':'无来源尺寸','value':999,'evidence':[{'region_id':'missing','raw_text':'999'}]}]
        result=self.normalize(raw)
        self.assertEqual(len(result['regions']),2)
        self.assertEqual(result['regions'][0]['type'],'detail')
        self.assertEqual(result['regions'][0]['bbox'],[0,10,60,60])
        self.assertEqual([x['label'] for x in result['parameters']],['板厚','未知自定义参数'])
        self.assertIsNone(result['parameters'][0]['value'])
        self.assertTrue(all(x['status']=='pending_review' for x in result['parameters']))
        self.assertGreater(len(result['warnings']),2)

    def test_unknown_part_reference_and_multiple_views_do_not_invent_parts(self):
        raw=observation();raw.pop('parts');raw['regions'][0]['part_id']='unknown'
        result=self.normalize(raw)
        self.assertEqual(result['parts'],[])
        self.assertIsNone(result['part'])
        self.assertIsNone(result['regions'][0]['part_id'])
        self.assertEqual(len(result['regions']),2)
        self.assertFalse(result['engineering_approved'])

    def test_explicit_partial_and_empty_drawing_keep_truthful_status(self):
        raw=observation();raw['analysis_complete']=False
        self.assertFalse(self.normalize(raw)['analysis_complete'])
        raw['regions']=[]
        self.assertFalse(self.normalize(raw)['analysis_complete'])
        raw.update(document_kind='not_drawing',analysis_complete=True)
        self.assertTrue(self.normalize(raw)['analysis_complete'])

    def test_coordinate_system_is_mandatory_and_evidence_is_source_pixels(self):
        raw=observation();raw['parameters'][0]['evidence'][0]['bbox']=[650,125,950,500]
        result=self.normalize(raw)
        self.assertEqual(result['parameters'][0]['evidence'][0]['bbox'],[65,10,95,40])
        del raw['coordinate_system']
        with self.assertRaises(ContractError):self.normalize(raw)

    def test_disjoint_evidence_box_is_not_used_for_source_highlight(self):
        raw=observation();raw['parameters'][0]['evidence'][0]['bbox']=[100,125,500,500]
        result=self.normalize(raw)
        self.assertNotIn('bbox',result['parameters'][0]['evidence'][0])
        self.assertEqual(result['parameters'][0]['evidence'][0]['raw_text'],'Thickness: illegible')
        self.assertTrue(any('不相交' in warning for warning in result['warnings']))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'jobs';self.service=None
    def tearDown(self):
        if self.service:self.service.close()
        self.temp.cleanup()
    def start(self,adapter=None,**options):
        self.service=SplitService(self.root,adapter or FakeAdapter(),**options);return self.service
    def test_upload_replay_refresh_and_pixel_crop_do_not_call_model_twice(self):
        adapter=FakeAdapter();service=self.start(adapter);data=png_bytes()
        created=service.create(data,'customer.png','upload-request-00000001')
        result=wait_terminal(service,created['job_id'])
        self.assertEqual(result['status'],'succeeded')
        replay=service.create(data,'customer.png','upload-request-00000001')
        self.assertEqual(replay['job_id'],created['job_id'])
        self.assertEqual(adapter.calls,1)
        for _ in range(4):service.get(created['job_id'])
        self.assertEqual(adapter.calls,1)
        self.assertNotIn('model-output',json.dumps(result))
        self.assertNotIn('private.example',json.dumps(result))
        self.assertTrue(result['source_url'].endswith('/canonical.png'))
        self.assertTrue(result['original_url'].endswith('/source.png'))
        manifest=result['manifest'];region=manifest['regions'][0]
        crop,_=service.file(created['job_id'],region['file'].split('/')[-1])
        with Image.open(crop) as image:
            self.assertEqual(image.size,(50,50))
            self.assertEqual(image.getpixel((2,2)),(14,42,90))
        self.assertEqual(manifest['parameters'][0]['value'],None)
        with self.assertRaises(ServiceError) as caught:service.create(data,'other.png','upload-request-00000001')
        self.assertEqual(caught.exception.status,409)

    def test_explicit_retry_preserves_previous_crop_and_is_idempotent(self):
        adapter=FakeAdapter();service=self.start(adapter)
        first=wait_terminal(service,service.create(png_bytes(),'drawing.png','upload-request-00000002')['job_id'])
        previous=first['manifest']['regions'][0]['file'].split('/')[-1]
        old_path,_=service.file(first['job_id'],previous);old_bytes=old_path.read_bytes()
        service.retry(first['job_id'],'retry-request-000000001')
        second=wait_terminal(service,first['job_id'])
        service.retry(first['job_id'],'retry-request-000000001')
        self.assertEqual(second['attempt'],2)
        self.assertEqual(adapter.calls,2)
        self.assertNotEqual(second['manifest']['regions'][0]['file'],first['manifest']['regions'][0]['file'])
        self.assertEqual(service.file(first['job_id'],previous)[0].read_bytes(),old_bytes)

    def test_restart_marks_queue_interrupted_without_automatic_request(self):
        adapter=FakeAdapter();service=self.start(adapter,start_worker=False)
        first=service.create(png_bytes(),'drawing.png','upload-request-00000003')
        service.close();self.service=None
        recovered=self.start(adapter,start_worker=False).get(first['job_id'])
        self.assertEqual(recovered['status'],'interrupted')
        self.assertEqual(adapter.calls,0)
        self.assertTrue(recovered['source_url'].endswith('/source.png'))

    def test_same_store_cannot_be_served_by_two_process_owners(self):
        self.start(start_worker=False)
        with self.assertRaises(ServiceError) as caught:SplitService(self.root,FakeAdapter(),start_worker=False)
        self.assertEqual(caught.exception.code,'store_busy')

    def test_invalid_image_is_preserved_and_never_sent_to_model(self):
        adapter=FakeAdapter();service=self.start(adapter)
        with self.assertRaises(ServiceError):service.create(b'%PDF-1.4','paper.pdf','upload-request-00000004')
        created=service.create(b'\x89PNG\r\n\x1a\nnot-an-image','corrupt.png','upload-request-00000005')
        result=wait_terminal(service,created['job_id'])
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['error']['code'],'image_invalid')
        self.assertEqual(adapter.calls,0)
        self.assertEqual(service.file(created['job_id'],'source.png')[0].read_bytes(),b'\x89PNG\r\n\x1a\nnot-an-image')

    def test_adapter_failure_sanitizes_credentials_and_preserves_original(self):
        adapter=FakeAdapter(error=RuntimeError('https://key:secret@example.test Bearer SECRET'))
        service=self.start(adapter)
        created=service.create(png_bytes(),'customer.png','upload-request-00000006')
        result=wait_terminal(service,created['job_id'])
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('SECRET',json.dumps(result))
        self.assertNotIn('example.test',json.dumps(result))
        self.assertIsNotNone(service.file(created['job_id'],'source.png'))

    def test_only_registered_immutable_files_can_be_read(self):
        service=self.start(start_worker=False)
        created=service.create(png_bytes(),'drawing.png','upload-request-00000007');job=created['job_id']
        for name in ('../job.json','job.json','model-output.json'):
            with self.assertRaises(ServiceError):service.file(job,name)
        source,_=service.file(job,'source.png');source.write_bytes(b'changed')
        with self.assertRaises(ServiceError) as caught:service.file(job,'source.png')
        self.assertEqual(caught.exception.code,'file_changed')


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.service=SplitService(Path(self.temp.name)/'jobs',FakeAdapter(),start_worker=False)
        self.http=SplitHTTPServer(('127.0.0.1',0),self.service,['https://example.test'])
        self.worker=threading.Thread(target=self.http.serve_forever,daemon=True);self.worker.start()
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.worker.join(timeout=2)
        self.service.close();self.temp.cleanup()
    def request(self,method,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.http.server_port,timeout=3)
        try:
            conn.request(method,path,body=body,headers=headers or {})
            response=conn.getresponse();payload=response.read()
            return response.status,dict(response.getheaders()),payload
        finally:conn.close()
    def test_cross_site_missing_origin_and_missing_marker_cannot_create(self):
        headers={'Origin':'https://attacker.test','X-Drawing-Split-Request':'1','X-Request-Id':'http-request-000000001'}
        status,response,_=self.request('POST','/jobs?name=x.png',png_bytes(),headers)
        self.assertEqual(status,403)
        self.assertNotIn('Access-Control-Allow-Origin',response)
        headers.pop('Origin')
        self.assertEqual(self.request('POST','/jobs',png_bytes(),headers)[0],403)
        headers={'Origin':'https://example.test','X-Request-Id':'http-request-000000001'}
        self.assertEqual(self.request('POST','/jobs',png_bytes(),headers)[0],403)
        self.assertEqual(self.request('OPTIONS','/jobs')[0],403)
    def test_same_origin_upload_get_and_file_have_private_headers(self):
        headers={'Origin':'https://example.test','Sec-Fetch-Site':'same-origin','X-Drawing-Split-Request':'1','X-Request-Id':'http-request-000000002','Content-Type':'image/png'}
        status,response,payload=self.request('POST','/jobs?name=test.png',png_bytes(),headers)
        self.assertEqual(status,202)
        created=json.loads(payload);job=created['job_id']
        self.assertTrue(created['source_url'].endswith('/source.png'))
        status,response,payload=self.request('GET','/jobs/'+job)
        self.assertEqual(status,200)
        self.assertEqual(response['Cache-Control'],'private, no-store')
        self.assertEqual(response['X-Content-Type-Options'],'nosniff')
        self.assertEqual(json.loads(payload)['status'],'queued')
        self.assertEqual(self.request('GET','/jobs')[0],404)
        self.assertEqual(self.request('GET','/jobs/'+job+'/files/../job.json')[0],404)
        status,response,payload=self.request('GET','/jobs/'+job+'/files/source.png')
        self.assertEqual(status,200)
        self.assertEqual(response['Content-Type'],'image/png')
        self.assertEqual(payload,png_bytes())


if __name__=='__main__':
    import platform
    if platform.system()!='Linux':raise SystemExit('Run tests only on ZeroClave, per AGENTS.md.')
    unittest.main()
