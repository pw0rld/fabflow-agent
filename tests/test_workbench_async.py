"""Real facade/record/quote integration with a fake bridge; no DSH or HTTP."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from workbench_store import Workbench
from test_workbench import REQUIREMENTS, TECHNICAL, POLICY


class BridgeFactory:
    def __init__(self):
        self.instances = []
        self.started = []
        self.inspections = []
        self.cancelled = []
        self.results = []
        self.cancel_results = []
        self.start_status = 'running'
        self.after_session = None

    def __call__(self, workspace):
        factory = self

        class Bridge:
            def start(self, task_id, files, inquiry, output_path, *, on_session):
                factory.started.append({'task_id': task_id, 'files': deepcopy(files), 'inquiry': inquiry,
                                        'output_path': Path(output_path), 'workspace': Path(workspace)})
                on_session('session-fake-current')
                if factory.after_session:
                    factory.after_session()
                return {'session_id': 'session-fake-current', 'status': factory.start_status,
                        'error': {'message': 'explicit start refused'} if factory.start_status == 'failed' else None}

            def inspect(self, session_id):
                factory.inspections.append(session_id)
                if not factory.results:
                    raise AssertionError('unexpected extra polling')
                result = factory.results.pop(0)
                return result() if callable(result) else deepcopy(result)

            def stop(self, session_id):
                factory.cancelled.append(session_id)
                return factory.cancel_results.pop(0) if factory.cancel_results else {'accepted': True, 'status': 'stopping'}

        result = Bridge()
        self.instances.append(result)
        return result


class AsyncFacadeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.factory = BridgeFactory()
        self.app = Workbench(self.directory / 'tasks', reader_root=self.directory / 'isolated', bridge_factory=self.factory)
        self.task = self.app.create({'event_id': 'create-async', 'inquiry': '只读本图，数量未知', 'simulated': True})
        self.task_id = self.task['id']
        self.content = b'\x89PNG\r\n\x1a\nfake-test-drawing'
        self.task = self.app.upload(self.task_id, 'drawing', 0, '支架.png', self.content)
        self.source_id = self.task['attachments'][0]['id']
        # Tests drive the real worker synchronously for deterministic timing.
        # Production still uses _launch_read's thread and flock implementation.
        self.app._launch_read = Mock()
        self.sleep_patch = patch('workbench_store.time.sleep', return_value=None)
        self.sleep_patch.start()
        self.addCleanup(self.sleep_patch.stop)

    def start(self):
        self.task = self.app.start_read(self.task_id, {'event_id': 'run-one', 'expected_revision': self.task['revision']})
        return self.task['run']

    def work(self):
        self.app._read_worker(self.task_id, 'run-one')
        self.task = self.app.task(self.task_id)
        return self.task['run']

    def completion(self, value=10):
        return {'status': 'completed', 'error': None, 'warnings': [], 'candidates': [
            {'name': 'quantity', 'value': value, 'raw_value': value, 'raw_unit': '件', 'unit': 'piece',
             'status': 'verified', 'evidence': {'reference': self.source_id, 'page': 1, 'region': '数量原文'}}]}

    def save(self, stage, values, event_id):
        self.task = self.app.save_fields(self.task_id, stage, {'event_id': event_id,
                'expected_revision': self.task['revision'], 'values': deepcopy(values)})

    def test_bridge_receives_only_isolated_copies_callback_persists_and_values_stay_pending(self):
        self.start()
        def callback_checked():
            self.assertEqual(self.app._state(self.task_id)['run']['session_id'], 'session-fake-current')
        self.factory.after_session = callback_checked
        self.factory.results = [self.completion()]
        original = self.app.records.load(self.task_id)['sources'][self.source_id]
        source_path = self.app.records.task_path(self.task_id) / original['path']
        before = source_path.read_bytes()
        run = self.work()
        self.assertEqual(run['status'], 'completed')
        self.assertTrue(run['candidates_applied'])
        passed = self.factory.started[0]
        self.assertEqual(len(passed['files']), 1)
        copy = Path(passed['files'][0]['path'])
        self.assertTrue(copy.is_relative_to(passed['workspace'] / 'attachments'))
        self.assertEqual(copy.read_bytes(), self.content)
        self.assertEqual(source_path.read_bytes(), before)
        self.assertFalse((passed['workspace'] / 'record.json').exists())
        self.assertFalse((passed['workspace'] / 'skills').exists())
        self.assertEqual(self.task['fields']['quantity']['status'], 'pending_review')
        self.assertFalse(self.task['validation']['ready'])
        self.assertEqual(self.task['revision'], 2)

    def test_unknown_start_and_poll_recover_same_session_without_duplicate_start(self):
        self.start()
        self.factory.start_status = 'unknown'
        self.factory.results = [{'status': 'unknown', 'error': {'message': 'request outcome unknown'}}, self.completion()]
        self.assertEqual(self.work()['status'], 'completed')
        self.assertEqual(len(self.factory.started), 1)
        self.assertEqual(self.factory.inspections, ['session-fake-current', 'session-fake-current'])

    def test_restart_with_persisted_session_inspects_instead_of_start(self):
        self.start()
        state = self.app._state(self.task_id)
        state['run'].update(session_id='session-already-admitted', status='unknown')
        self.app._save_state(self.task_id, state)
        restarted = Workbench(self.directory / 'tasks', reader_root=self.directory / 'isolated', bridge_factory=self.factory)
        restarted._launch_read = Mock()
        restarted.resume_reads()
        restarted._launch_read.assert_called_once_with(self.task_id)
        self.factory.results = [self.completion()]
        restarted._read_worker(self.task_id, 'run-one')
        self.assertEqual(self.factory.started, [])
        self.assertEqual(self.factory.inspections, ['session-already-admitted'])
        self.assertEqual(restarted.task(self.task_id)['run']['status'], 'completed')

    def test_stop_before_session_creation_does_not_admit_prompt(self):
        self.start()
        self.app.stop_read(self.task_id, {'event_id': 'stop-before-start', 'expected_revision': self.task['revision']})
        self.assertEqual(self.work()['status'], 'stopped')
        self.assertEqual(self.factory.started, [])
        self.assertEqual(self.factory.inspections, [])

    def test_stop_during_inflight_inspect_is_not_overwritten_by_running_result(self):
        self.start()
        def concurrent_stop():
            self.app.stop_read(self.task_id, {'event_id': 'stop-inflight', 'expected_revision': self.app.task(self.task_id)['revision']})
            return {'status': 'running', 'warnings': [], 'error': None}
        self.factory.results = [concurrent_stop, {'status': 'stopped', 'error': None}]
        self.assertEqual(self.work()['status'], 'stopped')
        self.assertEqual(self.factory.cancelled, ['session-fake-current'])
        self.assertEqual(self.app.task(self.task_id)['revision'], 1)

    def test_cancel_unknown_is_retried_and_never_applies_late_candidates(self):
        self.start()
        state = self.app._state(self.task_id)
        state['run'].update(status='stopping', session_id='session-existing')
        self.app._save_state(self.task_id, state)
        self.factory.cancel_results = [{'status': 'unknown', 'accepted': None}, {'status': 'stopping', 'accepted': True}]
        self.factory.results = [{'status': 'unknown', 'error': None}, self.completion()]
        self.assertEqual(self.work()['status'], 'stopped')
        self.assertEqual(self.factory.cancelled, ['session-existing', 'session-existing'])
        self.assertIsNone(self.task['fields']['quantity']['value'])
        self.assertEqual(self.task['revision'], 1)

    def test_late_result_does_not_replace_human_edit_or_old_quote(self):
        self.save('requirements', REQUIREMENTS, 'requirements')
        self.save('technical', TECHNICAL, 'technical')
        quote = self.app.issue(self.task_id, {'event_id': 'old-quote', 'expected_revision': self.task['revision'], 'policy': POLICY})
        quote_dir = self.app.records.task_path(self.task_id) / 'quotes' / quote['quote_id']
        hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in quote_dir.iterdir()}
        self.start()
        def human_changed():
            self.save('requirements', {'quantity': 12}, 'human-during-reading')
            return self.completion(99)
        self.factory.results = [human_changed]
        run = self.work()
        self.assertFalse(run['candidates_applied'])
        self.assertTrue(any('未覆盖' in warning for warning in run['warnings']))
        self.assertEqual(self.task['fields']['quantity']['value'], 12)
        self.assertEqual(self.task['fields']['quantity']['status'], 'verified')
        self.assertFalse(self.app._quote_result(self.task_id, quote['quote_id'])['source_current'])
        self.assertEqual(hashes, {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in quote_dir.iterdir()})

    def test_recovery_after_record_commit_does_not_apply_candidate_twice(self):
        self.start()
        state = self.app._state(self.task_id)
        self.app._apply_reading(self.task_id, state['run'], self.completion())
        state['run'].update(status='running', session_id='session-existing', candidates_applied=False)
        self.app._save_state(self.task_id, state)
        self.factory.results = [self.completion()]
        run = self.work()
        self.assertTrue(run['candidates_applied'])
        self.assertEqual(self.task['revision'], 2)
        self.assertEqual(sum(item['id'] == 'READ-run-one' for item in self.app.records.load(self.task_id)['events']), 1)

    def test_explicit_start_refusal_does_not_poll_or_apply(self):
        self.start()
        self.factory.start_status = 'failed'
        run = self.work()
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['error'], 'explicit start refused')
        self.assertEqual(self.factory.inspections, [])
        self.assertEqual(self.task['revision'], 1)

    def test_workspace_creation_failure_clears_worker_and_records_failure(self):
        self.start()
        key = (self.task_id, 'run-one')
        self.app._workers.add(key)
        original = Path.mkdir
        def fail_workspace(path, *args, **kwargs):
            if path == self.app.reader_root / self.task_id / 'run-one':
                raise PermissionError('isolated workspace unavailable')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'mkdir', fail_workspace):
            self.app._read_worker(*key)
        self.assertNotIn(key, self.app._workers)
        self.assertEqual(self.app.task(self.task_id)['run']['status'], 'failed')
        self.assertEqual(self.factory.started, [])


if __name__ == '__main__':
    unittest.main()
