"""Read-only engineering evidence transport; fake DSH only, no real inference."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_engineering_ai import normalize_result, recognize
from dsh_bridge import DshBridgeError


def lines():
    return [{'id': 'L0001', 'part_name': '支架', 'document': 'plate.stp'},
            {'id': 'L0002', 'part_name': '机箱', 'document': 'box.stp'}]


def sources():
    return [{'id': 'F0016', 'kind': 'reference', 'original_name': 'technical.png'}]


def payload():
    return {'candidates': [
        {'line_id': 'L0001', 'field': 'material_grade', 'value': 'SUS304',
         'evidence': {'reference': 'F0016', 'page': 1, 'quote': '支架 Material: SUS304', 'region': '技术说明'}},
        {'line_id': 'L0001', 'field': 'surface_treatment', 'value': '拉丝',
         'evidence': {'reference': 'F0016', 'page': 1, 'quote': '支架表面拉丝', 'region': '技术说明'}},
    ], 'coverage': [{'reference': 'F0016', 'page': 1, 'status': 'read', 'note': '标题栏和技术备注已读'}],
    'unresolved': [{'line_id': 'L0002', 'field': 'surface_treatment', 'status': 'not_stated', 'reason': '未找到机箱的表面处理文字。'}],
    'warnings': []}


class FakeBridge:
    calls = []
    answer = None
    prompt_failure = False

    def __init__(self, *args, **kwargs):
        pass

    def _lock(self):
        return nullcontext()

    def _rpc(self, method, body, **kwargs):
        self.calls.append((method, deepcopy(body)))
        if method == 'session.create':
            return {'sessionId': body['sessionId']}
        if method == 'session.prompt':
            if self.prompt_failure:
                self.prompt_failure = False
                raise DshBridgeError('admission acknowledgement lost', 'transport-error')
            return {'accepted': True}
        if method == 'session.cancel':
            return {'accepted': True}
        if method == 'session.history':
            return {'events': [
                {'event': {'seq': 1, 'type': 'assistant/message', 'data': {'turn': 1,
                    'message': {'content': [{'type': 'text', 'text': json.dumps(self.answer)}]}}}},
                {'event': {'seq': 2, 'type': 'turn/end', 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}},
            ]}
        raise AssertionError(method)


class EngineeringAIContractTests(unittest.TestCase):
    def test_values_are_source_bound_and_remain_unconfirmed(self):
        original_lines, original_payload = lines(), payload()
        saved = deepcopy((original_lines, original_payload))
        result = normalize_result(original_payload, original_lines, sources())
        first = result['lines']['L0001']
        self.assertEqual(first['values']['material_grade'], 'SUS304')
        self.assertEqual(first['values']['surface_treatment'], '拉丝')
        self.assertEqual(first['evidence']['surface_treatment'][0]['quote'], '支架表面拉丝')
        self.assertEqual(first['evidence']['surface_treatment'][0]['status'], 'pending_review')
        self.assertNotIn('thickness_mm', first['values'])
        self.assertEqual(result['lines']['L0002']['field_states']['surface_treatment'], 'not_stated')
        self.assertEqual(result['engineering_status'], 'pending_review')
        self.assertEqual((original_lines, original_payload), saved)

    def test_foreign_line_source_page_and_missing_text_cannot_be_adopted(self):
        for key, value in [('line_id', 'L9999'), ('field', 'unit_price')]:
            bad = payload(); bad['candidates'][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                normalize_result(bad, lines(), sources())
        for key, value in [('reference', 'OTHER'), ('page', 2), ('quote', '')]:
            bad = payload(); bad['candidates'][0]['evidence'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                normalize_result(bad, lines(), sources())

    def test_conflicting_surface_values_keep_evidence_without_selecting_one(self):
        model = payload()
        alternate = deepcopy(model['candidates'][1])
        alternate['value'] = '喷粉'
        alternate['evidence']['quote'] = '另一备注写表面喷粉'
        model['candidates'].append(alternate)
        result = normalize_result(model, lines(), sources())['lines']['L0001']
        self.assertNotIn('surface_treatment', result['values'])
        self.assertEqual(result['field_states']['surface_treatment'], 'conflict')
        self.assertEqual(len(result['evidence']['surface_treatment']), 2)

    def test_native_preview_and_unread_pages_are_not_material_evidence(self):
        for supplied in ([{**sources()[0], 'kind': 'native_preview'}], sources()):
            model = payload()
            if supplied[0]['kind'] == 'reference':
                model['coverage'][0]['status'] = 'unreadable'
            with self.subTest(kind=supplied[0]['kind']), self.assertRaises(ValueError):
                normalize_result(model, lines(), supplied)

    def test_partial_pdf_coverage_cannot_claim_source_does_not_state_requirement(self):
        model = payload(); model['candidates'] = []
        result = normalize_result(model, lines(), sources(), {'F0016': 2})
        self.assertEqual(result['lines']['L0002']['field_states']['surface_treatment'], 'not_found')

    def test_numeric_thickness_is_normalized_and_equivalent_precision_is_not_conflict(self):
        model = payload()
        template = deepcopy(model['candidates'][0]); template.update(field='thickness_mm', value=1.5)
        template['evidence']['quote'] = 't=1.5 mm'
        alternate = deepcopy(template); alternate['value'] = '1.50'
        model['candidates'] = [template, alternate]
        result = normalize_result(model, lines(), sources())['lines']['L0001']
        self.assertEqual(result['values']['thickness_mm'], '1.5')
        self.assertEqual(result['field_states']['thickness_mm'], 'candidate')


class EngineeringAITransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        source = self.folder / 'technical.png'
        source.write_bytes(b'PNG synthetic transport fixture; no model call')
        self.registered = [{**sources()[0], 'path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}]
        FakeBridge.calls, FakeBridge.answer, FakeBridge.prompt_failure = [], payload(), False

    def run_ai(self, **kwargs):
        with patch('batch_engineering_ai.DshBridge', FakeBridge):
            return recognize(self.folder, lines(), self.registered, {}, 'http://127.0.0.1:3080', poll_interval=0, **kwargs)

    def test_transport_reads_only_registered_files_and_cached_result_does_not_reprompt(self):
        result = self.run_ai()
        calls = len(FakeBridge.calls)
        self.assertEqual(self.run_ai(), result)
        self.assertEqual(len(FakeBridge.calls), calls)
        prompt = next(row for method, row in FakeBridge.calls if method == 'session.prompt')['content'][0]['text']
        self.assertIn('read/read_image', prompt)
        self.assertIn('engineering-summary.json', prompt)
        self.assertIn('不从外观颜色猜材料牌号或表面处理', prompt)
        self.assertNotIn('files', result['provenance'])
        self.assertEqual(result['provenance']['engineering_status'], 'pending_review')

    def test_ambiguous_prompt_admission_recovers_the_same_session_without_second_prompt(self):
        FakeBridge.prompt_failure = True
        with self.assertRaises(DshBridgeError):
            self.run_ai()
        first = next(row for method, row in FakeBridge.calls if method == 'session.prompt')
        result = self.run_ai()
        prompts = [row for method, row in FakeBridge.calls if method == 'session.prompt']
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result['provenance']['session_id'], first['sessionId'])

    def test_changed_evidence_and_foreign_workspace_paths_are_rejected_before_dispatch(self):
        self.run_ai()
        (self.folder / 'technical.png').write_bytes(b'changed')
        count = len(FakeBridge.calls)
        with self.assertRaises(ValueError):
            self.run_ai()
        self.assertEqual(len(FakeBridge.calls), count)
        registered = [{**self.registered[0], 'path': str(self.folder.parent / 'outside.png')}]
        with patch('batch_engineering_ai.DshBridge', FakeBridge), self.assertRaises(ValueError):
            recognize(self.folder, lines(), registered, {}, 'http://127.0.0.1:3080')

    def test_timeout_cancels_only_its_owned_session(self):
        with self.assertRaises(TimeoutError):
            self.run_ai(timeout_seconds=0)
        created = next(row for method, row in FakeBridge.calls if method == 'session.create')
        cancelled = next(row for method, row in FakeBridge.calls if method == 'session.cancel')
        self.assertEqual(created['sessionId'], cancelled['sessionId'])


if __name__ == '__main__':
    unittest.main()
