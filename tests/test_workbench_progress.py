"""Persistent read progress/recovery tests with fake DSH only; no model or network."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from workbench_store import Workbench


class ProgressBridgeFactory:
    def __init__(self):
        self.starts = []
        self.inspections = []
        self.stops = []
        self.results = []
        self.start_result = {'status': 'running', 'session_id': 'progress-session'}

    def __call__(self, workspace):
        factory = self

        class Bridge:
            def start(self, task_id, files, inquiry, output_path, *, on_session):
                factory.starts.append((task_id, Path(workspace)))
                on_session(factory.start_result['session_id'])
                return deepcopy(factory.start_result)

            def inspect(self, session_id):
                factory.inspections.append(session_id)
                if not factory.results:
                    raise AssertionError('unexpected inspection after scripted results')
                result = factory.results.pop(0)
                return result() if callable(result) else deepcopy(result)

            def stop(self, session_id):
                factory.stops.append(session_id)
                return {'status': 'stopping', 'accepted': True}

        return Bridge()


class WorkbenchProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='workbench-progress-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.factory = ProgressBridgeFactory()
        self.app = self.new_app()
        self.task = self.app.create({'event_id': 'progress-create', 'inquiry': '离线假图，数量待核对。', 'simulated': True})
        self.task_id = self.task['id']
        self.task = self.app.upload(self.task_id, 'progress-image', 0, 'drawing.png', b'\x89PNG\r\n\x1a\nfake-image')
        self.source_id = self.task['attachments'][0]['id']
        self.app._launch_read = Mock()
        sleep_patch = patch('workbench_store.time.sleep', return_value=None)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def new_app(self):
        return Workbench(self.root / 'tasks', reader_root=self.root / 'isolated', bridge_factory=self.factory)

    def start(self):
        self.task = self.app.start_read(self.task_id, {'event_id': 'read-progress', 'expected_revision': self.task['revision']})
        return self.task['run']

    def progress(self, stage, seq, *, retry=None):
        value = {'stage': stage, 'label': '本地测试进度：' + stage, 'last_event_seq': seq,
                 'last_activity_at': 1792371600000,
                 'events': [{'seq': seq, 'stage': stage, 'label': '本地测试事件：' + stage,
                             'at': 1792371600000}]}
        if retry is not None:
            value['retry'] = retry
        return value

    def completed(self):
        return {'status': 'completed', 'warnings': [], 'error': None,
                'progress': self.progress('completed', 30),
                'candidates': [{'name': 'quantity', 'raw_value': 12, 'value': 12,
                                'raw_unit': 'piece', 'unit': 'piece',
                                'evidence': {'reference': self.source_id, 'page': 1, 'region': '图纸数量栏'}}]}

    def work(self, app=None):
        app = app or self.app
        app._read_worker(self.task_id, 'read-progress')
        return app.task(self.task_id)['run']

    def persist_session(self, *, status='running', progress=None):
        state = self.app._state(self.task_id)
        state['run'].update(status=status, session_id='already-dispatched-session')
        if progress is None:
            state['run'].pop('progress', None)
        else:
            state['run']['progress'] = deepcopy(progress)
        self.app._save_state(self.task_id, state)

    def test_polls_update_checked_time_but_only_new_progress_updates_activity(self):
        self.start()
        snapshots = []
        first = {'status': 'running', 'progress': self.progress('reading', 10)}
        retry = {'status': 'running', 'progress': self.progress('retrying', 11,
                                                             retry={'attempt': 2, 'delay_ms': 500})}
        def capture_and_return(result):
            def inspect():
                snapshots.append(deepcopy(self.app._state(self.task_id)['run']))
                return deepcopy(result)
            return inspect
        self.factory.results = [first, capture_and_return(first), capture_and_return(retry),
                                capture_and_return(self.completed())]
        ticks = count()
        epoch = datetime(2026, 10, 19, tzinfo=timezone.utc)
        with patch('workbench_store.stamp', side_effect=lambda: (epoch + timedelta(seconds=next(ticks))).isoformat()):
            final = self.work()
        before_duplicate, after_duplicate, after_retry = snapshots
        self.assertNotEqual(before_duplicate['checked_at'], after_duplicate['checked_at'])
        self.assertEqual(before_duplicate['last_progress_at'], after_duplicate['last_progress_at'])
        self.assertEqual(before_duplicate['progress']['last_activity_at'],
                         after_duplicate['progress']['last_activity_at'])
        self.assertNotEqual(after_duplicate['last_progress_at'], after_retry['last_progress_at'])
        self.assertEqual(after_retry['progress']['last_event_seq'], 11)
        self.assertEqual(after_retry['progress']['retry'], {'attempt': 2, 'delay_ms': 500})
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(final['progress']['stage'], 'completed')
        self.assertEqual(final['progress'], self.app._state(self.task_id)['run']['progress'])
        self.assertEqual(self.new_app().task(self.task_id)['run']['progress'], final['progress'])
        listed = next(task for task in self.app.tasks() if task['id'] == self.task_id)
        self.assertEqual(listed['run']['progress'], final['progress'])

    def test_stage_change_without_new_event_updates_progress_time(self):
        self.start()
        snapshots = []
        def next_inspection(result):
            snapshots.append(deepcopy(self.app._state(self.task_id)['run']))
            return deepcopy(result)
        self.factory.results = [
            {'status': 'running', 'progress': self.progress('reading', 10)},
            lambda: next_inspection({'status': 'running', 'progress': self.progress('checking', 10)}),
            lambda: next_inspection(self.completed()),
        ]
        ticks = count()
        epoch = datetime(2026, 10, 19, tzinfo=timezone.utc)
        with patch('workbench_store.stamp', side_effect=lambda: (epoch + timedelta(seconds=next(ticks))).isoformat()):
            self.work()
        self.assertNotEqual(snapshots[0]['last_progress_at'], snapshots[1]['last_progress_at'])
        self.assertEqual(snapshots[1]['progress']['stage'], 'checking')

    def test_restarted_app_keeps_progress_and_inspects_same_session_without_dispatch(self):
        self.start()
        prior = self.progress('reading', 10)
        self.persist_session(status='unknown', progress=prior)
        restarted = self.new_app()
        restarted._launch_read = Mock()
        restarted.resume_reads()
        restarted._launch_read.assert_called_once_with(self.task_id)
        self.assertEqual(restarted.task(self.task_id)['run']['progress'], prior)
        self.factory.results = [self.completed()]
        final = self.work(restarted)
        self.assertEqual(self.factory.starts, [])
        self.assertEqual(self.factory.inspections, ['already-dispatched-session'])
        self.assertEqual(final['status'], 'completed')
        self.assertTrue(final['candidates_applied'])

    def test_recovery_after_record_commit_does_not_apply_again(self):
        self.start()
        state = self.app._state(self.task_id)
        self.app._apply_reading(self.task_id, state['run'], self.completed())
        state['run'].update(status='running', session_id='already-dispatched-session', candidates_applied=False)
        state['run']['progress'] = self.progress('checking', 20)
        self.app._save_state(self.task_id, state)
        restarted = self.new_app()
        self.factory.results = [self.completed()]
        final = self.work(restarted)
        record = restarted.records.load(self.task_id)
        self.assertEqual(record['data_revision'], 2)
        self.assertEqual(sum(event['id'] == 'READ-read-progress' for event in record['events']), 1)
        self.assertTrue(final['candidates_applied'])
        self.assertEqual(final['status'], 'completed')
        self.assertEqual(self.factory.starts, [])

    def test_stopping_state_survives_restart_and_late_result_is_not_applied(self):
        self.start()
        self.persist_session(status='stopping', progress=self.progress('reading', 10))
        restarted = self.new_app()
        self.factory.results = [self.completed()]
        final = self.work(restarted)
        self.assertEqual(self.factory.starts, [])
        self.assertEqual(self.factory.stops, ['already-dispatched-session'])
        self.assertEqual(final['status'], 'stopped')
        self.assertNotEqual(final['progress']['stage'], 'completed')
        self.assertFalse(final['candidates_applied'])
        self.assertEqual(restarted.records.load(self.task_id)['data_revision'], 1)
        self.assertIsNone(restarted.task(self.task_id)['fields']['quantity']['value'])

    def test_legacy_active_run_without_progress_has_visible_fallback(self):
        self.start()
        self.persist_session()
        restarted = self.new_app()
        before = restarted._state_path(self.task_id).read_bytes()
        run = restarted.task(self.task_id)['run']
        self.assertIsInstance(run.get('progress'), dict)
        self.assertTrue(run['progress'].get('stage'))
        self.assertTrue(run['progress'].get('label'))
        self.assertIsNone(run['progress'].get('last_activity_at'))
        self.assertEqual(run['session_id'], 'already-dispatched-session')
        self.assertNotEqual(run['status'], 'completed')
        self.assertFalse(run['candidates_applied'])
        self.assertEqual(before, restarted._state_path(self.task_id).read_bytes())

    def test_real_server_worker_finishes_without_client_polling(self):
        app = self.new_app()
        inspected = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        original_worker = app._read_worker
        def tracked_worker(*args):
            try:
                return original_worker(*args)
            finally:
                finished.set()
        app._read_worker = tracked_worker
        def complete_after_client_left():
            inspected.set()
            if not release.wait(timeout=3):
                raise AssertionError('test did not release worker')
            return self.completed()
        self.factory.results = [complete_after_client_left]
        try:
            response = app.start_read(self.task_id, {'event_id': 'read-progress', 'expected_revision': 1})
            self.assertEqual(response['run']['id'], 'read-progress')
            self.assertTrue(inspected.wait(timeout=3))
            self.assertFalse(finished.is_set())
            # No task/list/browser request is made while the model is completing.
            release.set()
            self.assertTrue(finished.wait(timeout=3))
            record = app.records.load(self.task_id)
            self.assertEqual(record['fields']['quantity']['effective']['value'], 12)
            self.assertEqual(record['fields']['quantity']['status'], 'pending_review')
            self.assertEqual(app._state(self.task_id)['run']['status'], 'completed')
            self.assertEqual(len(self.factory.starts), 1)
            self.assertNotIn((self.task_id, 'read-progress'), app._workers)
        finally:
            release.set()
            finished.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
