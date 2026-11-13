"""Offline lifecycle regression tests across the real order queue/service."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from order_service import OrderService
from prep_record import digest
from workbench_store import Workbench

DXF=b'0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n'


class LifecycleBridge:
    def __init__(self, result=None, *, reused=False, before_session=None):
        self.result=result or {'status':'completed','parts':[],'warnings':['本地假模型返回，无实际推理']}
        self.reused=reused;self.before_session=before_session
        self.starts=0;self.prompts=0;self.inspections=0;self.cancellations=0

    def start(self,job_id,files,inquiry,output,*,on_session):
        self.starts+=1
        if self.reused:
            return {'status':'unknown','session_id':'session-existing','requires_inspect':True,'reused':True}
        if self.before_session:
            self.before_session()
        try:
            on_session('session-new')
        except Exception:
            return {'status':'failed','session_id':'session-new','error':{'code':'callback-failed'}}
        self.prompts+=1
        return {'status':'running','session_id':'session-new'}

    def inspect(self,session):
        self.inspections+=1
        if self.cancellations and self.result['status']!='unknown':
            return {'status':'stopped','parts':[],'warnings':[]}
        return deepcopy(self.result)

    def stop(self,session):
        self.cancellations+=1
        return {'status':'unknown','accepted':None,'session_id':session}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=Workbench(Path(self.temp.name)/'tasks')
        self.service=OrderService(self.app,poll_seconds=0);self.addCleanup(self.service.close)
        self.order=self.service.create({'event_id':'create','name':'本地生命周期夹具','inquiry':'离线，无模型','simulated':True})
        self.order=self.service.upload(self.order['id'],'upload',self.order['revision'],'fixture.dxf',DXF)
        document=self.order['documents'][0]
        _,path=self.service._file(self.order['id'],document['id'])
        self.views={'views':[{'id':document['id'],'path':str(path),'sha256':document['sha256'],'original_name':document['name']}],'warnings':[]}

    def discover(self,bridge):
        self.service.discovery_factory=lambda workspace:bridge
        self.order=self.service.discover(self.order['id'],{'event_id':'discover','expected_revision':self.order['revision']})
        return self.service.queue.claim('worker-test',120)

    def execute(self,job):
        with patch.object(self.service,'_views',return_value=self.views):
            self.service._execute(job)

    def test_stop_after_claim_before_execute_never_dispatches_discovery(self):
        bridge=LifecycleBridge();job=self.discover(bridge)
        self.service.queue.request_stop(job['id'])
        self.execute(job)
        self.assertEqual(bridge.prompts,0,'停止已保存时不应再派发新模型请求')
        self.assertEqual(self.service.queue.get(job['id'])['status'],'stopped')

    def test_stop_during_session_creation_fences_prompt_at_callback(self):
        bridge=LifecycleBridge();job=self.discover(bridge)
        bridge.before_session=lambda:self.service.queue.request_stop(job['id'])
        self.execute(job)
        self.assertEqual(bridge.prompts,0,'session已创建但尚未发prompt时必须重新检查停止标记')
        self.assertEqual(self.service.queue.get(job['id'])['status'],'stopped')

    def test_unknown_cancellation_at_deadline_keeps_unknown_session_for_inspect(self):
        bridge=LifecycleBridge({'status':'unknown','parts':[],'warnings':[]})
        job=self.discover(bridge)
        self.service.queue.heartbeat(job['id'],job['lease_token'],120,session_id='session-existing')
        self.service.queue.request_stop(job['id'])
        job=self.service.queue.get(job['id'])
        with patch.object(self.service,'_views',return_value=self.views),patch('order_service.time.monotonic',side_effect=[0,1801]):
            self.service._execute(job)
        result=self.service.queue.get(job['id'])
        self.assertEqual(result['status'],'unknown','未确认取消不能报告已停止')
        self.assertEqual(result['session_id'],'session-existing')
        self.assertTrue(result['requires_inspect'])
        self.assertEqual(bridge.starts,0);self.assertEqual(bridge.cancellations,1)

    def test_reused_manifest_session_is_persisted_without_another_prompt(self):
        bridge=LifecycleBridge(reused=True);job=self.discover(bridge)
        self.execute(job)
        result=self.service.queue.get(job['id'])
        self.assertEqual(result['session_id'],'session-existing')
        self.assertEqual(result['status'],'completed')
        self.assertEqual(bridge.prompts,0);self.assertEqual(bridge.inspections,1)

    def test_expired_job_with_session_recovers_by_inspect_without_start(self):
        bridge=LifecycleBridge();now=[1000.0];self.service.queue.clock=lambda:now[0]
        job=self.discover(bridge)
        self.service.queue.heartbeat(job['id'],job['lease_token'],10,session_id='session-existing')
        now[0]+=11
        recovered=self.service.queue.claim('worker-restarted',120)
        self.assertTrue(recovered['requires_inspect'])
        self.execute(recovered)
        self.assertEqual(bridge.starts,0);self.assertEqual(bridge.inspections,1)
        self.assertEqual(self.service.queue.get(job['id'])['status'],'completed')

    def part_job(self):
        document=self.order['documents'][0]
        self.order=self.service.replace(self.order['id'],{'event_id':'part-structure','expected_revision':self.order['revision'],
            'parts':[{'id':'PART','name':'本地零件','part_number':'P1','version':'A','mode':'make','document_ids':[document['id']]}],
            'lines':[{'id':'LINE','part_id':'PART','quantity':1,'unit':'piece'}],
            'routes':{'PART':[{'id':'CUT','operation':'切割','status':'confirmed','reason':'本地人工构造'}]}})
        with patch.object(self.service,'_part_files',return_value=self.views['views']):
            self.order=self.service.prepare(self.order['id'],{'event_id':'part-prepare','expected_revision':self.order['revision']})
        self.part_template=self.app.task(self.order['parts'][0]['task_id'])
        payload={'task_id':self.part_template['id'],'task_revision':self.part_template['revision']}
        self.service.queue.enqueue(self.order['id'],'part_read',digest(payload),payload,'part-run')
        return self.service.queue.claim('worker-part',120)

    def test_part_read_recovery_reuses_existing_completed_task_run(self):
        job=self.part_job()
        task={**deepcopy(self.part_template),'run':{'status':'completed','warnings':[],'progress':{'stage':'completed'}}}
        with patch.object(self.app,'task',return_value=task),patch.object(self.app,'start_read') as start:
            self.service._execute(job)
        start.assert_not_called()
        self.assertEqual(self.service.queue.get(job['id'])['status'],'completed')
        self.assertEqual(self.service.jobs(self.order['id'])[0]['kind'],'part_read')
        self.assertNotIn('lease_token',self.service.jobs(self.order['id'])[0])

    def test_part_read_stopped_before_start_never_starts_model(self):
        job=self.part_job();self.service.queue.request_stop(job['id'])
        idle={**deepcopy(self.part_template),'run':None}
        stopped={**deepcopy(self.part_template),'run':{'status':'stopped','warnings':[]}}
        with patch.object(self.app,'task',side_effect=[idle,stopped]),patch.object(self.app,'start_read',return_value=stopped) as start:
            self.service._execute(job)
        start.assert_not_called()
        self.assertEqual(self.service.queue.get(job['id'])['status'],'stopped')

    def test_stop_unknown_part_at_deadline_preserves_recovery_state(self):
        job=self.part_job();self.service.queue.request_stop(job['id'])
        task={**deepcopy(self.part_template),'run':{'status':'unknown','warnings':[],'progress':{'stage':'unknown'}}}
        with patch.object(self.app,'task',return_value=task),patch.object(self.app,'stop_read',return_value=task),patch('order_service.time.monotonic',side_effect=[0,1801]):
            self.service._execute(job)
        self.assertEqual(self.service.queue.get(job['id'])['status'],'unknown','底层读图仍未知时应保留恢复检查')


if __name__=='__main__':
    unittest.main()
