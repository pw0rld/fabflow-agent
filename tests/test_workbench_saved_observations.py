"""Legacy source annotations are a read-only view, never another model call."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from dsh_bridge import MANIFEST
from workbench_store import Workbench


class SavedObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.app = Workbench(root / 'tasks', reader_root=root / 'readers')
        task = self.app.create({'event_id': 'saved-observations', 'inquiry': '旧版读图记录', 'simulated': True})
        task = self.app.upload(task['id'], 'old-image', 0, 'plate.png', b'\x89PNG\r\n\x1a\noffline fixture')
        self.task_id, self.source_id = task['id'], task['attachments'][0]['id']
        self.workspace = self.app.reader_root / self.task_id / 'old-read'
        self.workspace.mkdir(parents=True)
        source = self.app.records.load(self.task_id)['sources'][self.source_id]
        self.manifest = {'task_id': self.task_id, 'session_id': 'old-session', 'inquiry': task['inquiry'],
                         'files': [{'id': self.source_id, 'sha256': source['sha256']}],
                         'output_path': str(self.workspace / 'candidates.json')}
        evidence = {'reference': self.source_id, 'page': 1, 'region': '左上角材料说明'}
        gauge = {'name': 'thickness', 'raw_value': '16 GAUGE', 'raw_unit': 'gauge',
                 'value': '16 gauge', 'unit': None, 'evidence': evidence, 'reason': '板号未换算'}
        self.saved = {'task_id': self.task_id, 'session_id': 'old-session', 'status': 'completed',
                      'candidates': [], 'warnings': [], 'error': None,
                      'text': json.dumps({'candidates': [gauge], 'warnings': []}, ensure_ascii=False)}
        self.write_artifact()
        state = self.app._state(self.task_id)
        state['run'] = {'id': 'old-read', 'status': 'completed', 'session_id': 'old-session',
                        'source_ids': [self.source_id], 'base_revision': task['revision'],
                        'warnings': [], 'candidates_applied': False}
        self.app._save_state(self.task_id, state)

    def write_artifact(self):
        (self.workspace / MANIFEST).write_text(json.dumps(self.manifest))
        (self.workspace / 'candidates.json').write_text(json.dumps(self.saved, ensure_ascii=False))

    def test_legacy_gauge_visible_without_mutating_old_artifact_or_parameters(self):
        paths = [self.app._state_path(self.task_id), self.workspace / MANIFEST,
                 self.workspace / 'candidates.json', self.app.records.task_path(self.task_id) / 'record.json']
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        with patch('dsh_bridge.DshBridge._rpc', side_effect=AssertionError('RPC prohibited')) as rpc:
            task = self.app.task(self.task_id)
            self.app.task(self.task_id)
            self.app.tasks()
            rpc.assert_not_called()
        observations = task['run']['observations']
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]['raw_text'], '16 GAUGE')
        self.assertEqual(observations[0]['source_pass'], 'legacy')
        self.assertEqual(observations[0]['status'], 'unit_unknown')
        self.assertIsNone(task['fields']['thickness']['value'])
        self.assertEqual(task['fields']['thickness']['status'], 'missing')
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})

    def test_mismatched_saved_source_does_not_invent_observations(self):
        self.manifest['files'][0]['sha256'] = '0' * 64
        self.write_artifact()
        with patch('dsh_bridge.DshBridge._rpc', side_effect=AssertionError('RPC prohibited')):
            self.assertEqual(self.app.task(self.task_id)['run']['observations'], [])
        self.manifest['files'][0]['sha256'] = self.app.records.load(self.task_id)['sources'][self.source_id]['sha256']
        parsed = json.loads(self.saved['text'])
        parsed['candidates'][0]['evidence']['reference'] = 'different-task-source'
        self.saved['text'] = json.dumps(parsed)
        self.write_artifact()
        self.assertEqual(self.app.task(self.task_id)['run']['observations'], [])

    def test_private_review_cache_never_leaks_as_public_run_data(self):
        state = self.app._state(self.task_id)
        state['run']['initial_result'] = {'text': 'private bookkeeping'}
        state['run']['review'] = {'status': 'completed', 'fields': ['thickness'], 'result': {'internal': True}}
        self.app._save_state(self.task_id, state)
        public = self.app.task(self.task_id)['run']
        self.assertNotIn('initial_result', public)
        self.assertNotIn('result', public['review'])
        self.assertEqual(public['review']['fields'], ['thickness'])


if __name__ == '__main__':
    unittest.main()
