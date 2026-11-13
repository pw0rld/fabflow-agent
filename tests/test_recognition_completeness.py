"""Dense drawing evidence and one durable completeness pass; no model or network.

The bridge contract uses scripted RPC; worker cases execute real source copies,
records, review merging and public task projections with a fake in-process
bridge. The deliberately synthetic annotations are not drawing ground truth.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from dsh_bridge import MAX_OBSERVATIONS, MAX_TEXT_CHARS
from reading_review import _merge
from recognition_view import RECOGNITION_FIELDS
from workbench_store import Workbench
from test_dsh_bridge import FakeBridge, candidate, event, observation
from test_workbench_evidence_review import EvidenceBridgeFactory, SimulatedWorkerExit


class DenseBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='dense-drawing-bridge-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        source = self.directory / 'drawing.png'
        source.write_bytes(b'local synthetic source; no actual image recognition')
        self.output = self.directory / 'candidates.json'
        self.bridge = FakeBridge(self.directory)
        self.sid = self.bridge.start('DENSE-1', [
            {'id': 'drawing-1', 'path': str(source), 'original_name': 'dense.png'}
        ], '', self.output)['session_id']

    def complete(self, payload):
        text = json.dumps(payload, ensure_ascii=False)
        self.bridge.events = [
            event(1, 'turn/start'),
            event(2, 'assistant/message', message={'content': [{'type': 'text', 'text': text}]}),
            event(3, 'turn/end', reason={'kind': 'completed'}),
        ]
        return self.bridge.inspect(self.sid), text

    def rows(self, amount):
        return [observation('dimension_note', f'PART-{index}: 4 x Ø12 ±0.1; ' + '原始技术备注' * 40,
                            'mm', 'observed', evidence={
                                'reference': 'drawing-1', 'page': index % 4 + 1,
                                'region': f'视图局部尺寸标注 {index}'}, reason=None)
                for index in range(amount)]

    def test_dense_output_above_old_count_and_text_limits_is_saved_and_read_without_loss(self):
        rows = self.rows(MAX_OBSERVATIONS)
        self.assertGreater(len(rows), 256)
        result, text = self.complete({'candidates': [], 'observations': rows, 'warnings': []})
        self.assertGreater(len(text), 500_000, 'exercise the former whole-response bottleneck too')
        self.assertLess(len(text), MAX_TEXT_CHARS)
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(len(result['observations']), len(rows))
        for original, retained in zip(rows, result['observations']):
            self.assertEqual(retained['name'], 'engineering_open_items')
            self.assertEqual(retained['source_name'], 'dimension_note')
            for key in ('raw_text', 'raw_unit', 'status', 'evidence', 'reason'):
                self.assertEqual(retained[key], original[key])
        saved = self.output.read_bytes()
        self.assertEqual(json.loads(saved)['observations'], result['observations'])
        again = self.bridge.inspect(self.sid)
        self.assertEqual(again['observations'], result['observations'])
        self.assertEqual(self.output.read_bytes(), saved)
        self.assertEqual(sum(call[0] == 'session.prompt' for call in self.bridge.calls), 1)

    def test_observation_count_overflow_rejects_entire_artifact_without_partial_results(self):
        result, _ = self.complete({'candidates': [candidate()],
                                   'observations': self.rows(MAX_OBSERVATIONS + 1)})
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['candidates'], [])
        self.assertFalse(self.output.exists())

    def test_deferred_quantity_cannot_push_explicit_rows_over_the_shared_bound(self):
        deferred = candidate('quantity', '80', 80, 'boxes', 'piece')
        result, _ = self.complete({'candidates': [deferred],
                                   'observations': self.rows(MAX_OBSERVATIONS)})
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(self.output.exists())


class CompletenessWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='drawing-completeness-worker-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.factory = EvidenceBridgeFactory()
        self.app = self.new_app()
        self.task = self.app.create({'event_id': 'completeness-create', 'simulated': True,
                                    'inquiry': '只识别这两份模拟图纸，技术备注按原文保留。'})
        self.task_id = self.task['id']
        for index in range(2):
            self.task = self.app.upload(self.task_id, f'completeness-source-{index}',
                                       self.task['revision'], f'part-{index}.png',
                                       b'\x89PNG\r\n\x1a\nsynthetic source ' + str(index).encode())
        self.sources = [source['id'] for source in self.task['attachments']]
        self.app._launch_read = Mock()
        sleeping = patch('workbench_store.time.sleep', return_value=None)
        sleeping.start()
        self.addCleanup(sleeping.stop)

    def new_app(self):
        return Workbench(self.directory / 'tasks', reader_root=self.directory / 'readers',
                         bridge_factory=self.factory)

    def candidate(self, name, value, *, raw_unit=None, unit=None):
        return {'name': name, 'raw_value': value, 'raw_unit': raw_unit, 'value': value,
                'unit': unit, 'status': 'pending_review', 'evidence': {
                    'reference': self.sources[0], 'page': 1, 'region': '图纸标题栏'}}

    def critical(self):
        return [self.candidate('material_grade', 'Q235B'),
                self.candidate('thickness', '2', raw_unit='mm', unit='mm'),
                self.candidate('drawing_units', 'mm')]

    def annotation(self, text, *, source=None, page=1, region='主视图尺寸', label='尺寸与公差'):
        return {'name': 'engineering_open_items', 'source_name': label,
                'raw_text': text, 'raw_unit': 'mm', 'status': 'observed', 'reason': None,
                'evidence': {'reference': source or self.sources[0], 'page': page, 'region': region}}

    def completed(self, candidates=None, observations=None):
        return {'status': 'completed', 'error': None, 'warnings': [],
                'candidates': self.critical() if candidates is None else candidates,
                'observations': [] if observations is None else observations}

    def start(self):
        self.task = self.app.start_read(self.task_id, {
            'event_id': 'completeness-read', 'expected_revision': self.task['revision']})
        return self.task['run']

    def work(self, app=None):
        app = app or self.app
        app._read_worker(self.task_id, 'completeness-read')
        self.task = app.task(self.task_id)
        return self.task['run']

    def reviews(self):
        return [call for call in self.factory.started if call['review_fields'] is not None]

    def test_complementary_finish_keeps_color_without_a_false_conflict(self):
        self.start()
        self.factory.results['main-session'] = [self.completed(candidates=[
            self.candidate('surface_treatment', 'Painted; RAL7035')])]
        self.factory.results['review-session'] = [self.completed(candidates=[
            self.candidate('surface_treatment', 'Painted')])]
        run = self.work()
        self.assertEqual(run['status'], 'completed')
        candidates = self.task['recognition']['candidates']
        self.assertEqual(next(row['value'] for row in candidates if row['name'] == 'surface_treatment'),
                         'Painted; RAL7035')
        self.assertFalse(any(row['name'] == 'surface_treatment' and row['status'] == 'conflict'
                             for row in self.task['recognition']['observations']))
        before = self.hashes()
        self.new_app().task(self.task_id)
        self.assertEqual(before, self.hashes())

    def test_different_finish_colors_still_require_review(self):
        self.start()
        self.factory.results['main-session'] = [self.completed(candidates=[
            self.candidate('surface_treatment', 'Painted; RAL7035')])]
        self.factory.results['review-session'] = [self.completed(candidates=[
            self.candidate('surface_treatment', 'Painted; RAL9005')])]
        self.work()
        self.assertFalse(any(row['name'] == 'surface_treatment'
                             for row in self.task['recognition']['candidates']))
        self.assertTrue(any(row['name'] == 'surface_treatment' and row['status'] == 'conflict'
                            for row in self.task['recognition']['observations']))

    def hashes(self):
        return {path.relative_to(self.directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.directory.rglob('*') if path.is_file() and not path.name.endswith('.lock')}

    def test_new_full_drawing_run_reviews_all_recognition_fields_despite_complete_critical_candidates(self):
        self.assertEqual(self.start()['reading_scope'], 'full_drawing')
        self.factory.results['main-session'] = [self.completed()]
        self.factory.results['review-session'] = [self.completed(candidates=[], observations=[
            self.annotation('4 x Ø12 ±0.1', label='孔径及公差')])]
        run = self.work()
        self.assertEqual(run['status'], 'completed', run.get('error'))
        self.assertEqual(run['review']['mode'], 'completeness')
        self.assertEqual(set(self.reviews()[0]['review_fields']), RECOGNITION_FIELDS)
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(len(self.factory.started), 2)
        self.assertEqual(self.task['recognition']['observations'][0]['raw_text'], '4 x Ø12 ±0.1')
        self.assertEqual(self.task['recognition']['observations'][0]['label'], '孔径及公差')

    def test_verified_critical_values_do_not_skip_a_new_full_drawing_completeness_review(self):
        self.task = self.app.save_fields(self.task_id, 'requirements', {
            'event_id': 'completeness-verified', 'expected_revision': self.task['revision'],
            'values': {'material_grade': 'Q235B', 'thickness': '2', 'drawing_units': 'mm'}})
        self.start()
        self.factory.results['main-session'] = [self.completed(candidates=[])]
        self.factory.results['review-session'] = [self.completed(candidates=[], observations=[
            self.annotation('REMOVE ALL BURRS', label='技术要求')])]
        run = self.work()
        self.assertEqual(run['status'], 'completed', run.get('error'))
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(set(self.reviews()[0]['review_fields']), RECOGNITION_FIELDS)
        self.assertEqual(self.task['fields']['thickness']['status'], 'verified')
        self.assertEqual(self.task['fields']['thickness']['value'], '2')
        self.assertEqual(self.task['recognition']['observations'][0]['raw_text'], 'REMOVE ALL BURRS')

    def test_dense_two_pass_results_survive_merge_persistence_and_read_only_api_projection(self):
        self.start()
        initial = [self.annotation(f'FIRST-{index}: 4 x Ø12 ±0.1',
                                   source=self.sources[index % 2], page=index % 3 + 1,
                                   region=f'初读尺寸 {index}') for index in range(1300)]
        reviewed = [self.annotation(f'SECOND-{index}: R2 / 90° ±0.5°',
                                    source=self.sources[index % 2], page=index % 3 + 1,
                                    region=f'补读尺寸 {index}', label='折弯角度与半径')
                    for index in range(1300)]
        self.factory.results['main-session'] = [self.completed(observations=initial)]
        self.factory.results['review-session'] = [self.completed(candidates=[], observations=reviewed)]
        run = self.work()
        self.assertEqual(run['status'], 'completed', run.get('error'))
        expected = initial + reviewed
        for actual in (run['observations'], self.task['recognition']['observations'],
                       self.new_app().task(self.task_id)['recognition']['observations']):
            self.assertEqual(len(actual), 2600)
            for original, retained in zip(expected, actual):
                for key in ('name', 'source_name', 'raw_text', 'raw_unit', 'status', 'reason', 'evidence'):
                    self.assertEqual(retained[key], original[key])
        self.assertEqual({row['source_pass'] for row in run['observations']}, {'initial', 'review'})
        calls, before = deepcopy(self.factory.started), self.hashes()
        with patch('dsh_bridge.DshBridge._rpc', side_effect=AssertionError('RPC prohibited')):
            for _ in range(2):
                self.assertEqual(len(self.app.task(self.task_id)['recognition']['observations']), 2600)
                self.assertEqual(len(self.app.tasks()), 1)
        self.assertEqual(self.factory.started, calls)
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.app.records.task_path(self.task_id) / 'quotes').exists())

    def test_merge_deduplicates_only_identical_source_page_region_and_original_label(self):
        self.start()
        original = self.annotation('Ø12')
        others = [self.annotation('Ø12', source=self.sources[1]), self.annotation('Ø12', page=2),
                  self.annotation('Ø12', region='右视图孔位'), self.annotation('Ø12', label='孔径')]
        record, run = self.app.records.load(self.task_id), self.app._state(self.task_id)['run']
        result = _merge(self.app, record, run, self.completed(observations=[original]),
                        self.completed(candidates=[], observations=[deepcopy(original), *others]))
        self.assertEqual(len(result['observations']), 5)
        self.assertEqual(result['observations'][0]['source_pass'], 'initial')
        self.assertTrue(all(row['raw_text'] == 'Ø12' for row in result['observations']))
        self.assertEqual(len({json.dumps(row['evidence'], sort_keys=True) + row['source_name']
                              for row in result['observations']}), 5)

    def test_legacy_run_without_full_drawing_marker_keeps_previous_no_review_policy(self):
        self.start()
        state = self.app._state(self.task_id)
        state['run'].pop('reading_scope', None)
        self.app._save_state(self.task_id, state)
        self.factory.results['main-session'] = [self.completed()]
        run = self.work()
        self.assertEqual(run['status'], 'completed', run.get('error'))
        self.assertEqual(self.reviews(), [])
        self.assertEqual(len(self.factory.started), 1)

    def test_failed_completeness_pass_retains_first_result_and_explicitly_reports_incomplete_review(self):
        self.start()
        annotation = self.annotation('M6 - 4 PLACES')
        self.factory.results['main-session'] = [self.completed(observations=[annotation])]
        self.factory.results['review-session'] = [{'status': 'failed', 'error': {'message': 'scripted failure'}}]
        run = self.work()
        self.assertEqual(run['status'], 'completed', run.get('error'))
        self.assertEqual(run['review']['status'], 'failed')
        self.assertEqual(self.task['recognition']['observations'][0]['raw_text'], annotation['raw_text'])
        self.assertTrue(any('未完成' in warning for warning in run['warnings']))
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual({item['name'] for item in self.task['recognition']['candidates']},
                         {'material_grade', 'thickness', 'drawing_units'})

    def test_restart_reuses_persisted_completeness_session_and_completed_reads_do_not_reprompt(self):
        self.start()
        self.factory.results['main-session'] = [self.completed()]
        def exit_after_review_session_saved():
            saved = self.app._state(self.task_id)['run']['review']
            self.assertEqual(saved['session_id'], 'review-session')
            self.assertEqual(saved['mode'], 'completeness')
            raise SimulatedWorkerExit()
        self.factory.after_session['review-session'] = exit_after_review_session_saved
        with self.assertRaises(SimulatedWorkerExit):
            self.work()
        started = deepcopy(self.factory.started)
        self.factory.results['review-session'] = [self.completed(candidates=[], observations=[
            self.annotation('GENERAL TOLERANCE ±0.2', label='通用公差')])]
        restarted = self.new_app()
        run = self.work(restarted)
        self.assertEqual(run['status'], 'completed', run.get('error'))
        self.assertEqual(self.factory.started, started)
        before = self.hashes()
        for _ in range(2):
            restarted.task(self.task_id)
            restarted.tasks()
        self.assertEqual(self.factory.started, started)
        self.assertEqual(self.hashes(), before)
        self.assertEqual(sum(row['id'] == 'READ-completeness-read'
                             for row in restarted.records.load(self.task_id)['events']), 1)


if __name__ == '__main__':
    unittest.main()
