"""Recognition-only projections use this run's sourced output, never adopted values.

All drawings and model replies are local contract fixtures. The real task,
record, revision, worker and read-only facade code execute; no network or model
is invoked, and no quotation is needed to inspect an extraction result.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from workbench_store import Workbench
from test_workbench_async import BridgeFactory


class RecognitionProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='workbench-recognition-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.factory = Mock(side_effect=AssertionError('model dispatch prohibited'))
        self.app = Workbench(self.directory / 'tasks', reader_root=self.directory / 'readers',
                             bridge_factory=self.factory)
        task = self.app.create({'event_id': 'recognition-create',
                                'inquiry': '仅识别当前上传图纸，参数均待人工核对。', 'simulated': True})
        self.task_id = task['id']
        task = self.app.upload(self.task_id, 'recognition-drawing', task['revision'],
                               'drawing.png', b'\x89PNG\r\n\x1a\nrecognition-only-fixture')
        self.source_id = task['attachments'][0]['id']

    def candidate(self, name='quantity', value=10, *, reference=None, page=1, **changes):
        numeric = name == 'quantity'
        item = {'name': name, 'value': value, 'raw_value': value,
                'unit': 'piece' if numeric else None, 'raw_unit': '件' if numeric else None,
                'status': 'verified', 'reason': '离线模拟读图，尚未人工核对。',
                'evidence': {'reference': reference or self.source_id,
                             'page': page, 'region': '图纸标题栏原始标注'}}
        item.update(deepcopy(changes))
        return item

    def save_human(self, values, event='recognition-human'):
        task = self.app.task(self.task_id)
        return self.app.save_fields(self.task_id, 'requirements', {
            'event_id': event, 'expected_revision': task['revision'], 'values': values})

    def make_run(self, run_id='recognition-current', *, status='completed', **changes):
        value = {'id': run_id, 'status': status, 'session_id': 'recognition-session',
                 'source_ids': [self.source_id],
                 'base_revision': self.app.records.load(self.task_id)['data_revision'],
                 'candidates_applied': False, 'observations': [], 'warnings': []}
        value.update(deepcopy(changes))
        return value

    def install_run(self, run):
        state = self.app._state(self.task_id)
        state['run'] = deepcopy(run)
        self.app._save_state(self.task_id, state)

    def apply_run(self, candidates, *, run_id='recognition-current', **changes):
        run = self.make_run(run_id, **changes)
        with self.app.lock(self.task_id):
            self.app._apply_reading(self.task_id, run,
                                    {'status': 'completed', 'candidates': deepcopy(candidates),
                                     'observations': [], 'warnings': []})
            self.install_run(run)
        return run

    def recognition(self):
        return self.app.task(self.task_id)['recognition']

    def test_legacy_part_missing_optional_observation_fields_still_has_readonly_projection(self):
        from recognition_view import project
        position = self.candidate()['evidence']
        row = {'name': 'engineering_open_items', 'raw_text': 'REMOVE BURRS',
               'status': 'observed', 'evidence': position}
        part = {'key': 'legacy', 'name': ' Legacy panel ', 'part_number': None, 'version': None,
                'status': 'pending_review', 'evidence': [position], 'observations': [row]}
        run = self.make_run(recognition_result={'status': 'completed', 'parts': [part], 'candidates': []})
        before = self.hashes()
        result = project(self.app.records.load(self.task_id), run, {'observations': []})
        self.assertEqual(len(result['parts']), 1)
        self.assertEqual(result['parts'][0]['observations'][0]['raw_text'], 'REMOVE BURRS')
        self.assertTrue(any('旧版条目' in warning for warning in result['warnings']))
        self.assertEqual(self.hashes(), before)

    def hashes(self):
        return {str(path.relative_to(self.directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.directory.rglob('*') if path.is_file()}

    def test_current_read_event_displays_new_pending_values_instead_of_old_verified_values(self):
        self.save_human({'quantity': 20, 'product_name': '旧人工确认名称'})
        self.apply_run([self.candidate(value=99), self.candidate('product_name', '本轮图纸名称')])
        before = self.app.records.load(self.task_id)
        self.assertEqual(before['fields']['quantity']['effective']['value'], 20)
        self.assertEqual(before['fields']['quantity']['effective']['status'], 'verified')

        recognition = self.recognition()
        self.assertEqual(recognition['run_id'], 'recognition-current')
        self.assertEqual(recognition['status'], 'completed')
        self.assertTrue(recognition['available'])
        self.assertFalse(recognition['partial'])
        self.assertTrue(recognition['source_current'])
        self.assertEqual(recognition['source_ids'], [self.source_id])
        rows = {item['name']: item for item in recognition['candidates']}
        self.assertEqual(rows['quantity']['value'], 99)
        self.assertEqual(rows['quantity']['raw_value'], 99)
        self.assertEqual(rows['quantity']['unit'], 'piece')
        self.assertEqual(rows['quantity']['raw_unit'], '件')
        self.assertEqual(rows['product_name']['value'], '本轮图纸名称')
        definitions = before['requirements']['fields']
        for name, row in rows.items():
            self.assertEqual(row['status'], 'pending_review')
            self.assertEqual(row['label'], definitions[name]['label'])
            self.assertEqual(row['evidence'], self.candidate()['evidence'])
            self.assertTrue(row['reason'])
        self.assertEqual(self.app.records.load(self.task_id), before)

    def test_new_run_does_not_leak_previous_event_effective_value_or_history(self):
        self.apply_run([self.candidate(value=17)], run_id='recognition-previous')
        self.assertEqual(self.app.records.load(self.task_id)['fields']['quantity']['effective']['value'], 17)
        self.install_run(self.make_run('recognition-next'))
        result = self.recognition()
        self.assertEqual(result['run_id'], 'recognition-next')
        self.assertEqual(result['candidates'], [])
        self.assertFalse(result['available'])

    def test_verified_parameters_without_current_read_event_are_not_recognition_output(self):
        self.save_human({'quantity': 8, 'product_name': '人工名称'})
        self.install_run(self.make_run())
        result = self.recognition()
        self.assertEqual(result['candidates'], [])
        self.assertFalse(result['available'])
        self.assertEqual(self.app.task(self.task_id)['fields']['quantity']['value'], 8)

    def test_unknown_annotation_label_and_original_pdf_page_evidence_are_preserved(self):
        task = self.app.task(self.task_id)
        task = self.app.upload(self.task_id, 'recognition-pdf', task['revision'],
                               'part-rev04.pdf', b'%PDF-1.4\nlocal projection fixture only')
        pdf_id = next(item['id'] for item in task['attachments'] if item['name'].endswith('.pdf'))
        evidence = {'reference': pdf_id, 'page': 3, 'region': '第三页右侧密封说明'}
        observation = {'name': 'engineering_open_items', 'source_name': 'seal_requirement',
                       'raw_text': 'SEAL ALLOWANCE TO BE CONFIRMED', 'raw_unit': None,
                       'status': 'observed', 'evidence': evidence,
                       'reason': '原图文字，未知数值不估算。', 'source_pass': 'initial'}
        run = self.apply_run([self.candidate('material_grade', 'SUS304', reference=pdf_id, page=3)],
                              source_ids=[self.source_id, pdf_id], observations=[observation])
        result = self.recognition()
        self.assertEqual(result['candidates'][0]['evidence'], self.candidate(reference=pdf_id, page=3)['evidence'])
        self.assertEqual(len(result['observations']), 1)
        projected = result['observations'][0]
        for key, value in observation.items():
            self.assertEqual(projected[key], value)
        self.assertTrue(projected['label'])
        self.assertEqual(run['observations'], [observation])
        self.factory.assert_not_called()

    def test_repeated_task_and_list_reads_do_not_write_call_model_or_issue_quotes(self):
        self.apply_run([self.candidate(), self.candidate('product_name', '图纸支架')],
                       warnings=['板厚需核对。'])
        before = self.hashes()
        with patch('dsh_bridge.DshBridge._rpc', side_effect=AssertionError('RPC prohibited')) as rpc, \
                patch.object(self.app, 'issue', side_effect=AssertionError('quote prohibited')) as issue:
            for _ in range(3):
                detail = self.app.task(self.task_id)
                listed = self.app.tasks()
                self.assertTrue(detail['recognition']['available'])
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]['recognition_status'], 'completed')
                self.assertTrue(listed[0]['recognition_available'])
                self.assertEqual(listed[0]['attachment_count'], 1)
                self.assertEqual(listed[0]['recognition_name'], '图纸支架')
                self.assertEqual(detail['quotes'], [])
            rpc.assert_not_called()
            issue.assert_not_called()
        self.factory.assert_not_called()
        self.assertEqual(self.hashes(), before)
        self.assertFalse((self.app.records.task_path(self.task_id) / 'quotes').exists())

    def test_added_unread_attachment_marks_previous_recognition_source_set_stale(self):
        self.apply_run([self.candidate()])
        self.assertTrue(self.recognition()['source_current'])
        task = self.app.task(self.task_id)
        self.app.upload(self.task_id, 'recognition-new-drawing', task['revision'],
                        'new-drawing.png', b'\x89PNG\r\n\x1a\nnew-unread-fixture')
        result = self.recognition()
        self.assertFalse(result['source_current'])
        self.assertEqual(result['source_ids'], [self.source_id])
        self.assertEqual(len(self.app.task(self.task_id)['attachments']), 2)

    def test_in_progress_initial_result_is_visible_only_as_partial_unverified_output(self):
        initial = {'status': 'completed', 'candidates': [self.candidate(value=5)],
                   'observations': [], 'warnings': ['关键标注正在回查。']}
        self.install_run(self.make_run(status='running', initial_result=initial))
        result = self.recognition()
        self.assertEqual(result['status'], 'running')
        self.assertTrue(result['partial'])
        self.assertTrue(result['available'])
        self.assertEqual(result['candidates'][0]['value'], 5)
        self.assertEqual(result['candidates'][0]['status'], 'pending_review')
        self.assertIsNone(self.app.records.load(self.task_id)['fields']['quantity']['effective'])
        for name in ('warnings', 'questions'):
            self.assertIsInstance(result[name], list)
            self.assertTrue(all(isinstance(item, str) for item in result[name]))

    def test_completed_run_does_not_fall_back_to_initial_result_when_final_is_absent(self):
        self.install_run(self.make_run(initial_result={'candidates': [self.candidate(value=5)],
                                                 'observations': [], 'warnings': []}))
        result = self.recognition()
        self.assertEqual(result['candidates'], [])
        self.assertFalse(result['available'])
        self.assertFalse(result['partial'])

    def test_final_cache_takes_precedence_over_initial_and_stays_private_in_public_runs(self):
        initial = {'candidates': [self.candidate(value=5)], 'observations': [], 'warnings': []}
        final = {'status': 'completed', 'candidates': [self.candidate(value=12)],
                 'observations': [], 'warnings': ['采用最终回查结果。']}
        self.install_run(self.make_run(initial_result=initial, recognition_result=final))
        task = self.app.task(self.task_id)
        result = task['recognition']
        self.assertTrue(result['available'])
        self.assertFalse(result['partial'])
        self.assertEqual(result['candidates'][0]['value'], 12)
        self.assertEqual(result['candidates'][0]['status'], 'pending_review')
        self.assertNotIn('initial_result', task['run'])
        self.assertNotIn('recognition_result', task['run'])
        listed = self.app.tasks()[0]
        self.assertNotIn('initial_result', listed['run'])
        self.assertNotIn('recognition_result', listed['run'])
        self.assertEqual(self.app._state(self.task_id)['run']['recognition_result'], final)
        # A completed review may deliberately discard every initial candidate.
        # An empty final list is still authoritative, not a missing cache.
        final['candidates'] = []
        self.install_run(self.make_run(initial_result=initial, recognition_result=final))
        empty_final = self.recognition()
        self.assertEqual(empty_final['candidates'], [])
        self.assertFalse(empty_final['partial'])

    def test_unknown_fields_and_sources_outside_this_run_are_not_projected_as_candidates(self):
        task = self.app.task(self.task_id)
        task = self.app.upload(self.task_id, 'recognition-other-drawing', task['revision'],
                               'other.png', b'\x89PNG\r\n\x1a\nother-source-fixture')
        other_source = next(item['id'] for item in task['attachments'] if item['id'] != self.source_id)
        candidates = [self.candidate(), self.candidate('invented_dimension', '99'),
                      self.candidate('product_name', '别的任务', reference='FILE-not-this-task'),
                      self.candidate('material_grade', 'A36', reference=other_source)]
        self.install_run(self.make_run(recognition_result={'status': 'completed', 'candidates': candidates,
                                                     'observations': [], 'warnings': []}))
        result = self.recognition()
        self.assertEqual([item['name'] for item in result['candidates']], ['quantity'])
        self.assertFalse(result['source_current'])

    def test_cost_and_fee_fields_are_not_part_of_recognition_projection(self):
        values = [self.candidate(),
                  self.candidate('material_usage_per_piece', '0.4', raw_unit='kg/piece', unit='kg/piece'),
                  self.candidate('material_rate', '20', raw_unit='CNY/kg', unit='CNY/kg'),
                  self.candidate('setup_fee', '200', raw_unit='CNY/order', unit='CNY/order'),
                  self.candidate('operation_rates', [{'operation': '切割', 'value': '2', 'unit': 'CNY/piece'}]),
                  self.candidate('fee_inclusions', ['离线费用说明'])]
        self.apply_run(values)
        result = self.recognition()
        self.assertEqual([item['name'] for item in result['candidates']], ['quantity'])
        self.assertTrue(result['available'])
        self.assertFalse(any(slot['status'] == 'verified'
                             for slot in self.app.records.load(self.task_id)['fields'].values()))

    def test_worker_retains_final_candidates_when_revision_change_prevents_parameter_application(self):
        factory = BridgeFactory()
        self.app.bridge_factory = factory
        self.app._launch_read = Mock()
        task = self.app.task(self.task_id)
        run_id = 'recognition-concurrent-edit'
        self.app.start_read(self.task_id, {'event_id': run_id, 'expected_revision': task['revision']})

        def changed_before_result():
            self.save_human({'quantity': 7}, 'recognition-during-read')
            # Legacy no-observations bridge contract avoids an extra review;
            # the scripted completed result itself is the final model output.
            return {'status': 'completed', 'error': None, 'warnings': [],
                    'candidates': [self.candidate(value=33)]}

        factory.results = [changed_before_result]
        with patch('workbench_store.time.sleep', return_value=None):
            self.app._read_worker(self.task_id, run_id)
        task = self.app.task(self.task_id)
        self.assertEqual(task['run']['status'], 'completed')
        self.assertFalse(task['run']['candidates_applied'])
        self.assertEqual(task['fields']['quantity']['value'], 7)
        self.assertEqual(task['fields']['quantity']['status'], 'verified')
        result = task['recognition']
        self.assertEqual(result['candidates'][0]['value'], 33)
        self.assertEqual(result['candidates'][0]['status'], 'pending_review')
        self.assertFalse(result['partial'])
        self.assertEqual(self.app._state(self.task_id)['run']['recognition_result']['candidates'][0]['value'], 33)
        self.assertNotIn('recognition_result', task['run'])
        self.assertFalse(any(event['id'] == 'READ-' + run_id
                             for event in self.app.records.load(self.task_id)['events']))
        self.assertEqual(len(factory.started), 1)
        self.assertEqual(len(factory.inspections), 1)
        self.assertEqual(task['quotes'], [])


if __name__ == '__main__':
    unittest.main()
