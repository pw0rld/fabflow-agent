"""Legacy targeted evidence review compatibility through the real worker.

All model results are scripted in-process. No DSH, network, or real drawing
interpretation is used here; source/record/revision behavior is real.
"""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from workbench_store import Workbench


class SimulatedWorkerExit(BaseException):
    """A process can exit without running an application's Exception handler."""


class EvidenceBridgeFactory:
    def __init__(self):
        self.started = []
        self.inspected = []
        self.cancelled = []
        self.results = {'main-session': [], 'review-session': []}
        self.after_session = {}

    def __call__(self, workspace):
        factory = self
        workspace = Path(workspace)

        class Bridge:
            def start(self, task_id, files, inquiry, output_path, *, on_session, review_fields=None):
                session_id = 'review-session' if review_fields is not None else 'main-session'
                factory.started.append({'task_id': task_id, 'files': deepcopy(files),
                                        'workspace': workspace, 'inquiry': inquiry,
                                        'review_fields': deepcopy(review_fields),
                                        'session_id': session_id})
                on_session(session_id)
                callback = factory.after_session.pop(session_id, None)
                if callback:
                    callback()
                return {'session_id': session_id, 'status': 'running', 'error': None}

            def inspect(self, session_id):
                factory.inspected.append(session_id)
                rows = factory.results[session_id]
                if not rows:
                    raise AssertionError('unexpected additional model inspection: ' + session_id)
                result = rows.pop(0)
                return result() if callable(result) else deepcopy(result)

            def stop(self, session_id):
                factory.cancelled.append(session_id)
                return {'accepted': True, 'status': 'stopping'}

        return Bridge()


class WorkbenchEvidenceReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='workbench-evidence-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.factory = EvidenceBridgeFactory()
        self.app = self.new_app()
        self.task = self.app.create({'event_id': 'evidence-create',
                                    'inquiry': '只读本次支架图纸，未知单位不得猜测。', 'simulated': True})
        self.task_id = self.task['id']
        self.content = b'\x89PNG\r\n\x1a\nfake-evidence-drawing'
        self.task = self.app.upload(self.task_id, 'evidence-image', 0, 'bracket.png', self.content)
        self.source_id = self.task['attachments'][0]['id']
        self.app._launch_read = Mock()
        sleep_patch = patch('workbench_store.time.sleep', return_value=None)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def new_app(self):
        return Workbench(self.root / 'tasks', reader_root=self.root / 'isolated', bridge_factory=self.factory)

    def evidence(self, region):
        return {'reference': self.source_id, 'page': 1, 'region': region}

    def observation(self, text='16 GAUGE', *, raw_unit='gauge', status='unit_unknown', name='thickness', region='材料说明'):
        return {'name': name, 'raw_text': text, 'raw_unit': raw_unit, 'status': status,
                'evidence': self.evidence(region), 'reason': '原文已读到，尚需核对其含义或单位。'}

    def candidate(self, name, value, *, raw_unit=None, unit=None):
        return {'name': name, 'raw_value': value, 'raw_unit': raw_unit,
                'value': value, 'unit': unit, 'status': 'pending_review',
                'evidence': self.evidence('图纸标注：' + name)}

    def completed(self, candidates=None, observations=None, *, legacy=False):
        result = {'status': 'completed', 'error': None, 'warnings': [],
                  'candidates': [self.candidate('material_grade', 'A36')] if candidates is None else candidates}
        if not legacy:
            result['observations'] = [] if observations is None else deepcopy(observations)
        return result

    def main_result(self):
        return self.completed(observations=[self.observation()])

    def review_result(self):
        return self.completed(candidates=[], observations=[
            self.observation('(.060)', raw_unit=None, region='右侧视图板厚参考尺寸')])

    def start(self):
        self.task = self.app.start_read(self.task_id, {
            'event_id': 'evidence-read', 'expected_revision': self.task['revision']})
        # These fixtures exercise persisted runs created before full drawing
        # transcription. New-run completeness behavior has its own suite.
        with self.app.lock(self.task_id):
            state = self.app._state(self.task_id)
            state['run'].pop('reading_scope', None)
            self.app._save_state(self.task_id, state)
        self.task = self.app.task(self.task_id)
        return self.task['run']

    def work(self, app=None):
        app = app or self.app
        app._read_worker(self.task_id, 'evidence-read')
        self.task = app.task(self.task_id)
        return self.task['run']

    def reviews_started(self):
        return [row for row in self.factory.started if row['review_fields'] is not None]

    def assert_unknown_thickness(self):
        self.assertIsNone(self.task['fields']['thickness']['value'])
        self.assertNotEqual(self.task['fields']['thickness']['status'], 'verified')
        self.assertFalse(self.task['validation']['ready'])

    def test_gauge_and_second_reference_survive_without_becoming_confirmed_thickness(self):
        run = self.start()
        self.assertEqual(run['evidence_version'], 1)
        self.factory.results['main-session'] = [self.main_result()]
        self.factory.results['review-session'] = [self.review_result()]
        run = self.work()
        self.assertEqual(run['status'], 'completed')
        self.assert_unknown_thickness()
        self.assertEqual(self.task['fields']['material_grade']['value'], 'A36')
        self.assertEqual(self.task['fields']['material_grade']['status'], 'pending_review')
        raw = {item['raw_text'] for item in run['observations'] if item['name'] == 'thickness'}
        self.assertEqual(raw, {'16 GAUGE', '(.060)'})
        self.assertTrue(all(item['evidence']['reference'] == self.source_id for item in run['observations']))
        self.assertEqual(run['observations'], self.new_app().task(self.task_id)['run']['observations'])
        # Multiple UI GETs do not dispatch or rewrite evidence.
        before = self.app._state_path(self.task_id).read_bytes()
        self.app.task(self.task_id)
        self.app.tasks()
        self.assertEqual(before, self.app._state_path(self.task_id).read_bytes())
        self.assertEqual(len(self.reviews_started()), 1)

    def test_missing_critical_fields_trigger_one_targeted_isolated_review(self):
        self.start()
        self.factory.results['main-session'] = [self.main_result()]
        self.factory.results['review-session'] = [self.review_result()]
        run = self.work()
        review = self.reviews_started()
        self.assertEqual(len(review), 1)
        self.assertIn('thickness', review[0]['review_fields'])
        self.assertIn('drawing_units', review[0]['review_fields'])
        self.assertNotIn('material_grade', review[0]['review_fields'])
        self.assertNotIn('material_rate', review[0]['review_fields'])
        self.assertEqual(review[0]['workspace'],
                         (self.root / 'isolated' / self.task_id / 'evidence-read' / 'review').resolve())
        self.assertEqual(run['session_id'], 'main-session')
        self.assertEqual(run['review']['session_id'], 'review-session')
        self.assertEqual(run['review']['status'], 'completed')
        for item in review[0]['files']:
            path = Path(item['path'])
            self.assertTrue(path.is_relative_to(review[0]['workspace'] / 'attachments'))
            self.assertEqual(path.read_bytes(), self.content)
        self.assert_unknown_thickness()

    def test_complete_critical_fields_do_not_create_review(self):
        self.start()
        self.factory.results['main-session'] = [self.completed(candidates=[
            self.candidate('material_grade', 'A36'),
            self.candidate('thickness', '1.5', raw_unit='mm', unit='mm'),
            self.candidate('drawing_units', 'mm')])]
        run = self.work()
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(len(self.factory.started), 1)
        self.assertEqual(self.reviews_started(), [])
        self.assertEqual(self.task['fields']['thickness']['status'], 'pending_review')

    def test_verified_critical_parameters_do_not_retrigger_review_when_first_result_omits_them(self):
        self.task = self.app.save_fields(self.task_id, 'requirements', {
            'event_id': 'known-critical-fields', 'expected_revision': self.task['revision'],
            'values': {'material_grade': 'Q235B', 'thickness': '2', 'drawing_units': 'mm'}})
        self.start()
        self.factory.results['main-session'] = [self.completed(candidates=[])]
        run = self.work()
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(len(self.factory.started), 1)
        self.assertEqual(self.reviews_started(), [])
        self.assertEqual(self.task['fields']['thickness']['value'], '2')
        self.assertEqual(self.task['fields']['thickness']['status'], 'verified')

    def test_legacy_run_or_legacy_bridge_protocol_does_not_add_model_call(self):
        for old_run in (True, False):
            with self.subTest(old_run=old_run):
                # Use a distinct persisted run in each branch without reusing an
                # event id that would intentionally return its previous receipt.
                app = self.new_app()
                created = app.create({'event_id': 'legacy-' + str(old_run), 'inquiry': '旧会话', 'simulated': True})
                created = app.upload(created['id'], 'legacy-image', 0, 'old.png', self.content)
                app._launch_read = Mock()
                app.start_read(created['id'], {'event_id': 'evidence-read', 'expected_revision': 1})
                state = app._state(created['id'])
                state['run'].update(session_id='main-session', status='running')
                if old_run:
                    state['run'].pop('evidence_version', None)
                app._save_state(created['id'], state)
                result = {'status': 'completed', 'error': None, 'warnings': [], 'candidates': []}
                if old_run:
                    result['observations'] = []
                self.factory.results['main-session'] = [result]
                app._read_worker(created['id'], 'evidence-read')
                self.assertEqual(app.task(created['id'])['run']['status'], 'completed')
                self.assertEqual(self.reviews_started(), [])
                self.assertEqual(self.factory.started, [])

    def test_review_failure_keeps_valid_first_result_and_original_observation(self):
        self.start()
        self.factory.results['main-session'] = [self.main_result()]
        self.factory.results['review-session'] = [{'status': 'failed', 'error': {'message': 'scripted review unavailable'}}]
        run = self.work()
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['review']['status'], 'failed')
        self.assertTrue(run['warnings'])
        self.assertEqual(self.task['fields']['material_grade']['value'], 'A36')
        self.assertEqual(self.task['fields']['material_grade']['status'], 'pending_review')
        self.assertIn('16 GAUGE', [item['raw_text'] for item in run['observations']])
        self.assert_unknown_thickness()
        self.assertEqual(len(self.reviews_started()), 1)

    def test_review_with_out_of_scope_candidate_is_rejected_as_a_whole(self):
        self.start()
        self.factory.results['main-session'] = [self.main_result()]
        self.factory.results['review-session'] = [self.completed(candidates=[
            self.candidate('thickness', '1.5', raw_unit='mm', unit='mm'),
            self.candidate('quantity', 99, raw_unit='piece', unit='piece')])]
        run = self.work()
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['review']['status'], 'failed')
        self.assertTrue(run['warnings'])
        self.assertEqual(self.task['fields']['material_grade']['value'], 'A36')
        self.assertIsNone(self.task['fields']['quantity']['value'])
        # Even an otherwise valid in-scope candidate from the rejected
        # response is not partly adopted.
        self.assert_unknown_thickness()
        self.assertIn('16 GAUGE', [item['raw_text'] for item in run['observations']])

    def test_completed_review_cache_is_checked_for_scope_without_new_dispatch(self):
        self.start()
        state = self.app._state(self.task_id)
        state['run'].update(status='running', session_id='main-session', initial_result=self.main_result())
        state['run']['review'] = {
            'status': 'completed', 'session_id': 'review-session', 'error': None,
            'fields': ['thickness', 'drawing_units'],
            'result': self.completed(candidates=[
                self.candidate('quantity', 99, raw_unit='piece', unit='piece')])}
        self.app._save_state(self.task_id, state)
        restarted = self.new_app()
        run = self.work(restarted)
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['review']['status'], 'failed')
        self.assertTrue(run['warnings'])
        self.assertEqual(self.task['fields']['material_grade']['value'], 'A36')
        self.assertIsNone(self.task['fields']['quantity']['value'])
        self.assert_unknown_thickness()
        self.assertEqual(self.factory.started, [])
        self.assertEqual(self.factory.inspected, [])

    def test_different_grade_candidates_keep_conflicting_evidence_without_choosing_one(self):
        self.start()
        self.factory.results['main-session'] = [self.completed(candidates=[
            self.candidate('material_grade', 'A36'),
            self.candidate('thickness', '1.5', raw_unit='mm', unit='mm'),
            self.candidate('drawing_units', 'mm')], observations=[
                self.observation('A36', name='material_grade', raw_unit=None,
                                 status='conflict', region='初次材料标注')])]
        self.factory.results['review-session'] = [self.completed(candidates=[
            self.candidate('material_grade', 'Q235B')], observations=[
                self.observation('Q235B', name='material_grade', raw_unit=None,
                                 status='observed', region='复核材料标注')])]
        run = self.work()
        self.assertEqual(run['status'], 'completed')
        self.assertIn('material_grade', self.reviews_started()[0]['review_fields'])
        self.assertIsNone(self.task['fields']['material_grade']['value'])
        self.assertNotEqual(self.task['fields']['material_grade']['status'], 'verified')
        grades = [item for item in run['observations'] if item['name'] == 'material_grade']
        self.assertTrue({'A36', 'Q235B'}.issubset({item['raw_text'] for item in grades}))
        self.assertTrue(all(item['status'] == 'conflict' for item in grades))
        self.assertTrue(run['warnings'])

    def test_stop_cancels_active_review_and_does_not_apply_late_completion(self):
        self.start()
        self.factory.results['main-session'] = [self.main_result()]
        def stop_then_complete():
            current = self.app.task(self.task_id)
            self.app.stop_read(self.task_id, {'event_id': 'stop-review', 'expected_revision': current['revision']})
            return self.review_result()
        self.factory.results['review-session'] = [stop_then_complete]
        run = self.work()
        self.assertEqual(run['status'], 'stopped')
        self.assertEqual(self.factory.cancelled, ['review-session'])
        self.assertFalse(run['candidates_applied'])
        self.assertIsNone(self.task['fields']['material_grade']['value'])
        self.assertEqual(self.task['revision'], 1)

    def test_persisted_stop_intent_survives_unknown_restart_and_cancels_review(self):
        self.start()
        self.factory.results['main-session'] = [self.main_result()]
        def stop_then_worker_exits():
            current = self.app.task(self.task_id)
            self.app.stop_read(self.task_id, {
                'event_id': 'durable-stop-review', 'expected_revision': current['revision']})
            self.assertTrue(self.app._state(self.task_id)['run']['stop_requested'])
            raise SimulatedWorkerExit()
        self.factory.results['review-session'] = [stop_then_worker_exits]
        with self.assertRaises(SimulatedWorkerExit):
            self.work()
        state = self.app._state(self.task_id)
        self.assertEqual(state['run']['review']['session_id'], 'review-session')
        # A transport/error recovery may subsequently classify the run as
        # unknown. It must not erase the operator's persisted cancellation.
        state['run']['status'] = 'unknown'
        self.app._save_state(self.task_id, state)
        starts_before_restart = deepcopy(self.factory.started)
        restarted = self.new_app()
        restarted._launch_read = Mock()
        restarted.resume_reads()
        restarted._launch_read.assert_called_once_with(self.task_id)
        self.factory.results['review-session'] = [self.review_result()]
        run = self.work(restarted)
        self.assertEqual(run['status'], 'stopped')
        self.assertEqual(run['review']['status'], 'stopped')
        self.assertEqual(self.factory.cancelled, ['review-session'])
        self.assertEqual(self.factory.started, starts_before_restart)
        self.assertFalse(run['candidates_applied'])
        self.assertIsNone(self.task['fields']['material_grade']['value'])
        self.assertEqual(self.task['revision'], 1)
        self.assertFalse(any(event['id'] == 'READ-evidence-read'
                             for event in restarted.records.load(self.task_id)['events']))

    def test_restart_after_review_session_persisted_resumes_it_without_new_start(self):
        self.start()
        self.factory.results['main-session'] = [self.main_result()]
        def exit_after_session_is_durable():
            saved = self.app._state(self.task_id)['run']
            self.assertEqual(saved['review']['session_id'], 'review-session')
            raise SimulatedWorkerExit()
        self.factory.after_session['review-session'] = exit_after_session_is_durable
        with self.assertRaises(SimulatedWorkerExit):
            self.work()
        starts_before_restart = deepcopy(self.factory.started)
        restarted = self.new_app()
        restarted._launch_read = Mock()
        restarted.resume_reads()
        restarted._launch_read.assert_called_once_with(self.task_id)
        # Re-inspecting the completed initial session is safe when a restarted
        # worker reconstructs its pending result. No new prompt is admitted.
        self.factory.results['main-session'] = [self.main_result()]
        self.factory.results['review-session'] = [self.review_result()]
        run = self.work(restarted)
        self.assertEqual(self.factory.started, starts_before_restart)
        self.assertIn('review-session', self.factory.inspected)
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['review']['session_id'], 'review-session')
        self.assertEqual(sum(event['id'] == 'READ-evidence-read'
                             for event in restarted.records.load(self.task_id)['events']), 1)
        self.assert_unknown_thickness()

    def test_human_revision_change_stays_verified_and_drawing_evidence_remains_visible(self):
        self.start()
        self.factory.results['main-session'] = [self.main_result()]
        def human_edits_then_review_finishes():
            current = self.app.task(self.task_id)
            self.app.save_fields(self.task_id, 'requirements', {
                'event_id': 'human-during-review', 'expected_revision': current['revision'],
                'values': {'material_grade': 'Q235B', 'thickness': '2'}})
            return self.review_result()
        self.factory.results['review-session'] = [human_edits_then_review_finishes]
        run = self.work()
        self.assertEqual(run['status'], 'completed')
        self.assertFalse(run['candidates_applied'])
        self.assertEqual(self.task['fields']['material_grade']['value'], 'Q235B')
        self.assertEqual(self.task['fields']['material_grade']['status'], 'verified')
        self.assertEqual(self.task['fields']['thickness']['value'], '2')
        self.assertEqual(self.task['fields']['thickness']['status'], 'verified')
        self.assertTrue(any('未覆盖' in warning for warning in run['warnings']))
        self.assertEqual({item['raw_text'] for item in run['observations']}, {'16 GAUGE', '(.060)'})
        self.assertTrue(all(item['evidence']['reference'] == self.source_id for item in run['observations']))


if __name__ == '__main__':
    unittest.main()
