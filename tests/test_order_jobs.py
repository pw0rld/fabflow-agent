"""Durable queue contracts; temporary SQLite only, no remote/model calls."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from order_jobs import JobQueue, JobQueueError, LeaseLost


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'jobs.sqlite3'
        self.now = 1000.0
        self.queue = JobQueue(self.path, clock=lambda: self.now)

    def enqueue(self, index=1, order='ORDER-A', event=None):
        payload = {'documents':[{'id':'DOC-' + str(index), 'sha256':str(index)}]}
        digest = hashlib.sha256(str(index).encode()).hexdigest()
        return self.queue.enqueue(order, 'discover', digest, payload, event or 'EV-' + str(index))

    def test_enqueue_event_and_content_idempotency_are_scoped(self):
        first = self.enqueue()
        self.assertEqual(self.enqueue()['id'], first['id'])
        self.assertEqual(self.enqueue(event='EV-NEW')['id'], first['id'])
        self.assertEqual(len(self.queue.list('ORDER-A')), 1)
        self.assertNotEqual(self.enqueue(order='ORDER-B')['id'], first['id'])
        with self.assertRaises(JobQueueError):
            self.enqueue(2, event='EV-1')
        with self.assertRaises(JobQueueError):
            self.queue.enqueue('ORDER-A', 'discover', first['input_hash'], {'other':1}, 'EV-OTHER')
        self.assertEqual(self.queue.get(first['id'])['payload'], first['payload'])

    def test_restart_preserves_queued_input_and_completed_result(self):
        first = self.enqueue()
        other = JobQueue(self.path, clock=lambda:self.now)
        claimed = other.claim('WORKER-1', 30)
        self.assertEqual(claimed['id'], first['id'])
        completed = other.finish(first['id'], claimed['lease_token'], {'parts':[], 'warnings':['待补图纸']})
        self.assertEqual(completed['status'], 'completed')
        restarted = JobQueue(self.path, clock=lambda:self.now)
        self.assertIsNone(restarted.claim('WORKER-2'))
        self.assertEqual(restarted.get(first['id'])['result']['warnings'], ['待补图纸'])

    def test_expired_lease_recovers_same_job_and_fences_old_worker(self):
        first = self.enqueue()
        a = self.queue.claim('WORKER-A', 10)
        self.queue.heartbeat(a['id'], a['lease_token'], 10, session_id='session-persisted')
        self.now += 11
        b = JobQueue(self.path, clock=lambda:self.now).claim('WORKER-B', 30)
        self.assertEqual(b['id'], first['id'])
        self.assertEqual(b['session_id'], 'session-persisted')
        self.assertEqual(b['attempt'], 2)
        self.assertTrue(b['requires_inspect'])
        self.assertNotEqual(a['lease_token'], b['lease_token'])
        for method, args in [(self.queue.heartbeat, ()), (self.queue.finish, ({'parts':[]},)),
                             (self.queue.fail, ('late failure',))]:
            with self.subTest(method=method.__name__), self.assertRaises(LeaseLost):
                method(a['id'], a['lease_token'], *args)
        self.queue.finish(b['id'], b['lease_token'], {'parts':[]})

    def test_expired_owner_cannot_commit_even_before_reclaim(self):
        self.enqueue()
        claimed = self.queue.claim('WORKER-A', 1)
        self.now += 1
        with self.assertRaises(LeaseLost):
            self.queue.finish(claimed['id'], claimed['lease_token'], {'parts':[]})
        with self.assertRaises(LeaseLost):
            self.queue.heartbeat(claimed['id'], claimed['lease_token'])

    def test_heartbeat_renews_and_pins_session_before_dispatch(self):
        self.enqueue()
        claimed = self.queue.claim('WORKER-A', 10)
        self.now += 9
        saved = self.queue.heartbeat(claimed['id'], claimed['lease_token'], 30,
                                     progress={'stage':'reading'}, session_id='session-one')
        self.assertEqual(saved['lease_until'], self.now + 30)
        self.assertEqual(saved['progress']['stage'], 'reading')
        self.now += 2
        self.assertIsNone(self.queue.claim('WORKER-B'))
        with self.assertRaises(JobQueueError):
            self.queue.heartbeat(claimed['id'], claimed['lease_token'], session_id='session-two')
        self.assertEqual(self.queue.get(claimed['id'])['session_id'], 'session-one')

    def test_unknown_and_retry_preserve_session_and_wait_until_backoff(self):
        self.enqueue()
        claimed = self.queue.claim('WORKER-A', 20)
        self.queue.heartbeat(claimed['id'], claimed['lease_token'], session_id='session-one')
        unknown = self.queue.fail(claimed['id'], claimed['lease_token'], {'code':'rpc-timeout'},
                                  unknown=True, delay_seconds=5)
        self.assertEqual(unknown['status'], 'unknown')
        self.assertIsNone(self.queue.claim('WORKER-B'))
        self.now += 5
        recovered = self.queue.claim('WORKER-B')
        self.assertTrue(recovered['requires_inspect'])
        self.assertEqual(recovered['session_id'], 'session-one')
        self.queue.fail(recovered['id'], recovered['lease_token'], 'retry later', retryable=True, delay_seconds=2)
        self.now += 2
        retry = self.queue.claim('WORKER-C')
        self.assertEqual(retry['session_id'], 'session-one')
        self.assertTrue(retry['requires_inspect'])

    def test_parallel_connections_claim_each_job_once(self):
        for index in range(24):
            self.enqueue(index)
        def claim(worker):
            return JobQueue(self.path, clock=lambda:self.now).claim('WORKER-' + str(worker), 60)
        with ThreadPoolExecutor(max_workers=8) as executor:
            claimed = list(executor.map(claim, range(32)))
        jobs = [job for job in claimed if job]
        self.assertEqual(len(jobs), 24)
        self.assertEqual(len({job['id'] for job in jobs}), 24)
        self.assertEqual(len({job['lease_token'] for job in jobs}), 24)

    def test_stop_queued_and_cancel_admitted_unknown_without_late_result(self):
        queued = self.enqueue()
        self.assertEqual(self.queue.request_stop(queued['id'])['status'], 'stopped')
        self.assertIsNone(self.queue.claim('WORKER-A'))
        self.enqueue(2)
        running = self.queue.claim('WORKER-A')
        self.queue.heartbeat(running['id'], running['lease_token'], session_id='session-to-cancel')
        self.queue.request_stop(running['id'])
        with self.assertRaises(JobQueueError):
            self.queue.finish(running['id'], running['lease_token'], {'parts':['late']})
        stopped = self.queue.fail(running['id'], running['lease_token'], 'cancelled', stopped=True)
        self.assertEqual(stopped['status'], 'stopped')
        self.assertIsNone(self.queue.claim('WORKER-B'))

    def test_invalid_json_numbers_hashes_durations_and_symlink_rejected(self):
        for payload in ({'x':float('nan')}, {'x':float('inf')}):
            with self.assertRaises(JobQueueError):
                self.queue.enqueue('ORDER-A','discover','a'*64,payload,'EV-1')
        for bad in ('not-a-hash','A'*64):
            with self.assertRaises(JobQueueError):
                self.queue.enqueue('ORDER-A','discover',bad,{},'EV-1')
        for duration in (0,True,float('nan'),-1):
            with self.assertRaises(JobQueueError):
                self.queue.claim('WORKER-A',duration)
        linked = self.path.parent / 'linked.sqlite3'
        linked.symlink_to(self.path)
        with self.assertRaises(JobQueueError):
            JobQueue(linked)

    def test_cancel_requested_admitted_failure_retains_unknown_and_original_session(self):
        self.enqueue()
        job=self.queue.claim('WORKER-A',30)
        self.queue.heartbeat(job['id'],job['lease_token'],30,session_id='session-admitted')
        self.queue.request_stop(job['id'])
        pending=self.queue.fail(job['id'],job['lease_token'],{'message':'cancel RPC timed out'})
        self.assertEqual(pending['status'],'unknown')
        self.assertTrue(pending['stop_requested'])
        self.assertEqual(pending['session_id'],'session-admitted')
        self.assertEqual(pending['next_attempt_at'],self.now+15)
        self.assertIsNone(self.queue.claim('WORKER-B'))
        self.now+=15
        recovered=self.queue.claim('WORKER-B')
        self.assertTrue(recovered['requires_inspect'])
        self.assertTrue(recovered['stop_requested'])
        stopped=self.queue.fail(recovered['id'],recovered['lease_token'],'termination verified',stopped=True)
        self.assertEqual(stopped['status'],'stopped')

    def test_explicit_unknown_child_session_cannot_be_stopped_without_confirmation(self):
        self.enqueue()
        job=self.queue.claim('WORKER-A',30)
        self.queue.request_stop(job['id'])
        pending=self.queue.fail(job['id'],job['lease_token'],'child reader state unknown',unknown=True)
        self.assertEqual(pending['status'],'unknown')
        self.assertTrue(pending['stop_requested'])
        self.assertEqual(pending['next_attempt_at'],self.now+15)

    def test_unadmitted_stop_can_finish_without_an_external_session(self):
        self.enqueue()
        job=self.queue.claim('WORKER-A',30)
        self.queue.request_stop(job['id'])
        result=self.queue.fail(job['id'],job['lease_token'],'not dispatched')
        self.assertEqual(result['status'],'stopped')


if __name__ == '__main__':
    unittest.main()
