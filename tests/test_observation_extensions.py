"""Unmatched drawing labels are sourced notes, never adopted parameters.

Only temporary local fixtures and fake in-process RPC are used. These checks
exercise real bridge parsing, record provenance, review merging and persistence;
they make no claims about model recognition accuracy.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
from dsh_bridge import DshBridge, EXTRA_OBSERVATION_WARNING
from prep_record import RecordError
from reading_review import _checked_review, _merge
from test_dsh_bridge import FakeBridge, candidate, event, observation
from workbench_store import Workbench


def finish(bridge, session_id, payload):
    text = json.dumps(payload, ensure_ascii=False)
    bridge.events = [event(1, 'turn/start'),
                     event(2, 'assistant/message', message={'content': [{'type': 'text', 'text': text}]}),
                     event(3, 'turn/end', reason={'kind': 'completed'})]
    return bridge.inspect(session_id), text


class ExtraObservationBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='observation-extensions-')
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.input = self.workspace / 'input.png'
        self.input.write_bytes(b'\x89PNG\r\n\x1a\nfake-local-contract-fixture')
        self.bridge = FakeBridge(self.workspace)
        self.output = self.workspace / 'candidates.json'
        started = self.bridge.start('TASK-EXTRAS', [
            {'id': 'drawing-1', 'path': str(self.input), 'original_name': '本地测试图.png'}
        ], '本地模拟读图，未请求报价。', self.output)
        self.sid = started['session_id']
        self.run = self.bridge._run(self.sid)

    def test_unknown_labels_preserve_long_reason_and_known_candidate_without_inventing_values(self):
        rows = [observation('孔位及焊接标注', '4 × Ø12; WELD ALL AROUND', 'mm', 'observed',
                            reason='原' * 4000),
                observation('total_price', '原附件写明预算，不是已核定报价', None, 'observed', reason=None),
                observation('X' * 128, '[无法辨认]', None, 'unreadable', reason='原标注模糊')]
        payload = {'candidates': [candidate()], 'observations': rows, 'warnings': ['保留原始警示']}
        before = deepcopy(payload)
        result, text = finish(self.bridge, self.sid, payload)
        self.assertEqual(result['status'], 'completed', result.get('error'))
        self.assertEqual(payload, before)
        self.assertEqual(result['text'], text)
        self.assertEqual(result['candidates'], [{**candidate(), 'status': 'pending_review'}])
        self.assertEqual(result['observations'], [
            {**row, 'name': 'engineering_open_items', 'source_name': row['name']} for row in rows])
        self.assertEqual(result['warnings'].count(EXTRA_OBSERVATION_WARNING), 1)
        self.assertIn('保留原始警示', result['warnings'])
        for row in result['observations']:
            self.assertNotIn('value', row)
            self.assertNotIn('raw_value', row)
        before_bytes = self.output.read_bytes()
        projected = self.bridge.observations_from_text(text, self.run)
        self.assertEqual(projected, result['observations'])
        for _ in range(2):
            repeated = self.bridge.inspect(self.sid)
            self.assertEqual(repeated['status'], 'completed', repeated.get('error'))
            self.assertEqual(repeated['observations'], result['observations'])
            self.assertEqual(self.output.read_bytes(), before_bytes)
        self.assertEqual(sum(call[0] == 'session.create' for call in self.bridge.calls), 1)
        self.assertEqual(sum(call[0] == 'session.prompt' for call in self.bridge.calls), 1)

    def test_unknown_candidate_remains_illegal_even_when_same_label_is_a_valid_observation(self):
        for name in ('total_price', '孔位及焊接标注', 'thickness_mm'):
            with self.subTest(name=name):
                item = candidate(name, raw='3', value='3', raw_unit='mm', unit='mm')
                payload = {'candidates': [item], 'observations': [observation(name)]}
                result, _ = finish(self.bridge, self.sid, payload)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['candidates'], [])
                self.assertNotIn('observations', result)
                self.assertFalse(self.output.exists())

    def test_invalid_labels_and_model_supplied_internal_metadata_remain_atomic_failures(self):
        labels = ('', ' ', ' leading', 'trailing ', 'a\nb', 'a\tb', 'a\x00b',
                  'a\u202eb', 'x' * 129, None, False, 7, [], {})
        rows = [observation(name) for name in labels]
        rows += [observation('孔位', source_name='forged'),
                 observation('engineering_open_items', source_name='forged'),
                 observation('thickness', source_name='forged')]
        for row in rows:
            with self.subTest(row=str(row)[:180]):
                result, _ = finish(self.bridge, self.sid, {'candidates': [candidate()], 'observations': [row]})
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['candidates'], [])
                self.assertNotIn('observations', result)
                self.assertFalse(self.output.exists())

    def test_extra_labels_do_not_relax_state_source_shape_or_text_bounds(self):
        original = observation('孔位', '4 × Ø12', 'mm', 'observed', reason=None)
        rows = [{**original, **updates} for updates in (
            {'status': 'verified'}, {'status': 'pending_review'}, {'status': None},
            {'raw_text': ''}, {'raw_text': 'x' * 10001}, {'raw_unit': 12},
            {'raw_unit': 'x' * 4001}, {'reason': 'x' * 4001}, {'value': '3'})]
        missing = deepcopy(original)
        del missing['reason']
        rows.append(missing)
        for update in ({'reference': 'another-task'}, {'page': 0}, {'page': True},
                       {'page': 10001}, {'region': ''}):
            rows.append({**original, 'evidence': {**original['evidence'], **update}})
        for row in rows:
            with self.subTest(row=str(row)[:160]):
                result, _ = finish(self.bridge, self.sid, {'candidates': [candidate()], 'observations': [row]})
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['candidates'], [])
                self.assertFalse(self.output.exists())

    def test_generic_observations_obey_canonical_review_scope(self):
        row = observation('seal_allowance_part64', '117.5*', 'mm', 'unit_unknown', reason=None)
        text = json.dumps({'candidates': [], 'observations': [row]}, ensure_ascii=False)
        with self.assertRaises(ValueError):
            self.bridge.observations_from_text(text, {**self.run, 'review_fields': ['thickness']})
        actual = self.bridge.observations_from_text(
            text, {**self.run, 'review_fields': ['engineering_open_items']})
        self.assertEqual(actual, [{**row, 'name': 'engineering_open_items', 'source_name': row['name']}])

    def test_successful_legacy_part_artifact_is_still_identical_on_repeated_inspection(self):
        row = observation('feature_part01', '4 × Ø12', 'mm', 'observed', reason='原标注')
        payload = {'candidates': [], 'observations': [row], 'warnings': []}
        text = json.dumps(payload, ensure_ascii=False)
        # Exact pre-extension projection, deliberately independent of the new
        # normalization implementation and new warning constant.
        old_row = {**row, 'name': 'engineering_open_items',
                   'reason': '模型原观察字段：feature_part01；仅保留工程原文，不作为单件参数。原标注'}
        old_result = {'session_id': self.sid, 'task_id': 'TASK-EXTRAS', 'text': text, 'error': None,
                      'candidates': [],
                      'warnings': ['多零件的外形、版本、结构和读图说明已保留为工程观察，未作为单件规范参数采用'],
                      'status': 'completed', 'observations': [old_row]}
        expected = (json.dumps(old_result, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
        self.output.write_bytes(expected)
        for _ in range(2):
            result, _ = finish(self.bridge, self.sid, payload)
            self.assertEqual(result['status'], 'completed', result.get('error'))
            self.assertEqual(result['observations'], [old_row])
            self.assertNotIn(EXTRA_OBSERVATION_WARNING, result['warnings'])
            self.assertEqual(self.output.read_bytes(), expected)


class ExtraObservationStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='observation-store-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.factory = Mock(side_effect=AssertionError('real model dispatch forbidden'))
        self.app = Workbench(self.root / 'tasks', reader_root=self.root / 'readers', bridge_factory=self.factory)
        task = self.app.create({'event_id': 'extras-create', 'simulated': True,
                                'inquiry': '本地模拟标注回归，不请求成本核算或报价。'})
        self.task_id = task['id']
        task = self.app.upload(self.task_id, 'extras-upload', task['revision'], '测试.png',
                               b'\x89PNG\r\n\x1a\nfake-local-contract-fixture')
        self.record = self.app.records.load(self.task_id)
        self.source_id = task['attachments'][0]['id']
        self.run = {'id': 'extras-read', 'base_revision': task['revision'],
                    'source_ids': [self.source_id], 'status': 'completed'}
        workspace = self.root / 'bridge'
        workspace.mkdir()
        self.bridge = DshBridge('http://127.0.0.1:3080', workspace)
        self.manifest = {'task_id': self.task_id, 'inquiry': self.record['inquiry'],
                         'files': [{'id': self.source_id}]}

    def extra(self, label='孔位', **updates):
        item = observation(label, '4 × Ø12', 'mm', 'observed', reason='原始说明')
        item['evidence']['reference'] = self.source_id
        return {**item, **updates}

    def parsed(self, rows, candidates=None):
        text = json.dumps({'candidates': candidates or [], 'observations': rows}, ensure_ascii=False)
        values, warnings = self.bridge._candidates(text, self.manifest)
        observations = self.bridge.observations_from_text(text, self.manifest)
        return {'status': 'completed', 'error': None, 'candidates': values,
                'observations': observations, 'warnings': warnings}

    def test_store_review_merge_and_readonly_projection_preserve_labels_without_adopting_notes(self):
        first = self.extra('孔位', reason='原' * 4000)
        second = self.extra('焊接说明', reason='原' * 4000)
        usable = candidate(raw='3', value='3', raw_unit='mm', unit='mm',
                           evidence=deepcopy(first['evidence']))
        initial = self.parsed([first, second], [usable])
        reviewed = self.parsed([second])
        original = deepcopy((initial, reviewed))
        with patch.object(self.bridge, '_rpc', side_effect=AssertionError('RPC forbidden')) as rpc:
            checked = _checked_review(self.app, self.record, self.run, reviewed, ['engineering_open_items'])
            merged = _merge(self.app, self.record, self.run, initial, checked)
            self.assertEqual((initial, reviewed), original)
            self.assertEqual(len(merged['observations']), 2,
                             'identical text/evidence with distinct source labels must not collapse')
            self.assertEqual({row['source_name'] for row in merged['observations']}, {'孔位', '焊接说明'})
            self.assertTrue(all(row['reason'] == '原' * 4000 for row in merged['observations']))
            self.assertTrue(all(row['source_pass'] == 'initial' for row in merged['observations']))
            self.app._apply_reading(self.task_id, self.run, merged)
            rpc.assert_not_called()
        self.factory.assert_not_called()
        self.run['observations'] = merged['observations']
        state = self.app._state(self.task_id)
        state['run'] = self.run
        self.app._save_state(self.task_id, state)
        saved = self.app.records.load(self.task_id)
        self.assertEqual(saved['data_revision'], self.record['data_revision'] + 1)
        self.assertEqual(saved['sources'], self.record['sources'])
        self.assertIsNone(saved['fields']['engineering_open_items']['effective'])
        self.assertEqual(saved['fields']['engineering_open_items']['history'], [])
        self.assertEqual(saved['fields']['thickness']['status'], 'pending_review')
        self.assertNotIn('孔位', saved['fields'])
        self.assertNotIn('焊接说明', saved['fields'])
        before = {path: path.read_bytes() for path in (
            self.app.records.task_path(self.task_id) / 'record.json', self.app._state_path(self.task_id))}
        for _ in range(2):
            task = self.app.task(self.task_id)
            self.assertEqual(task['run']['observations'], merged['observations'])
            self.assertEqual(task['quotes'], [])
            self.assertFalse(task['validation']['ready'])
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.factory.assert_not_called()

    def test_store_accepts_bounded_original_label_only_on_engineering_observations(self):
        row = self.parsed([self.extra()])['observations'][0]
        result = self.app._observations(self.record, self.run, [row], 'review')
        self.assertEqual(result, [{**row, 'source_pass': 'review'}])
        invalid = [{**row, 'name': 'thickness'}]
        invalid += [{**row, 'source_name': value} for value in
                    ('', ' leading', 'trailing ', 'x' * 129, 'a\nb', 'a\x00b', None, False, 7, [], {})]
        for item in invalid:
            with self.subTest(item=str(item)[:160]):
                with self.assertRaises((RecordError, ValueError)):
                    self.app._observations(self.record, self.run, [item], 'initial')

    def test_review_merge_keeps_existing_conflict_rules_and_requested_scope(self):
        extra = self.extra('外形备注', status='unit_unknown')
        initial = self.parsed([extra])
        with self.assertRaises(RecordError):
            _checked_review(self.app, self.record, self.run, initial, ['thickness'])
        first = candidate(raw='3', value='3', raw_unit='mm', unit='mm',
                          evidence=deepcopy(extra['evidence']))
        second = candidate(raw='4', value='4', raw_unit='mm', unit='mm',
                           evidence=deepcopy(extra['evidence']))
        initial = self.parsed([extra], [first])
        reviewed = self.parsed([], [second])
        merged = _merge(self.app, self.record, self.run, initial, reviewed)
        self.assertEqual(merged['candidates'], [])
        self.assertEqual(sum(row['name'] == 'thickness' and row['status'] == 'conflict'
                             for row in merged['observations']), 2)
        self.assertEqual(next(row for row in merged['observations']
                              if row['name'] == 'engineering_open_items')['source_name'], '外形备注')


try:
    import pypdfium2 as pdfium
    from PIL import Image
except ImportError:
    pdfium = Image = None


@unittest.skipUnless(pdfium is not None and Image is not None,
                     'Pillow and pypdfium2 required for PDF evidence extension contract')
class ExtraObservationPdfTests(unittest.TestCase):
    def test_extra_label_evidence_maps_to_original_pdf_and_rejects_invalid_pages(self):
        with tempfile.TemporaryDirectory(prefix='observation-pdf-') as temporary:
            workspace = Path(temporary)
            path = workspace / 'drawing.pdf'
            pdf = pdfium.PdfDocument.new()
            try:
                for _ in range(2):
                    page = pdf.new_page(144, 72)
                    page.gen_content()
                    page.close()
                pdf.save(path)
            finally:
                pdf.close()
            bridge = FakeBridge(workspace)
            output = workspace / 'candidates.json'
            session = bridge.start('TASK-EXTRA-PDF', [{'id': 'drawing-pdf', 'path': str(path),
                                    'original_name': '两页原图.pdf'}], '本地模拟', output)
            sid = session['session_id']
            run = bridge._run(sid)
            alias = next(row['id'] for row in run['pdf_views']['views'] if row['page'] == 2)
            row = observation('seal_allowance_part64', '117.5*', 'mm', 'unit_unknown',
                              evidence={'reference': alias, 'page': 1, 'region': '第二页局部'}, reason=None)
            for reference, page in ((alias, 2), (alias, True), ('drawing-pdf', 3), ('other-pdf', 1)):
                invalid = {**row, 'evidence': {**row['evidence'], 'reference': reference, 'page': page}}
                result, _ = finish(bridge, sid, {'candidates': [], 'observations': [invalid]})
                self.assertEqual(result['status'], 'failed')
                self.assertFalse(output.exists())
            result, _ = finish(bridge, sid, {'candidates': [], 'observations': [row]})
            self.assertEqual(result['status'], 'completed', result.get('error'))
            expected = {**row, 'name': 'engineering_open_items', 'source_name': row['name'],
                        'evidence': {**row['evidence'], 'reference': 'drawing-pdf', 'page': 2}}
            self.assertEqual(result['observations'], [expected])
            self.assertEqual(result['candidates'], [])


if __name__ == '__main__':
    unittest.main()
