"""Native-evidence AI contract with a fake DSH transport; no inference calls."""
from contextlib import nullcontext
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cad_manufacturing_ai import normalize_suggestions, suggest


def geometry():
    return {'schema_version': 1, 'source_sha256': 'a' * 64, 'engine': {'name': 'test'},
            'output_unit': 'mm', 'warnings': [], 'contact_sheets': ['contact-001.png'],
            'solids': [{'id': 'S0001', 'name': 'Plate', 'definition_id': 'D1',
                        'thumbnail': 'S0001.png', 'mesh': {'positions': [0, 0, 0]}}]}


def payload():
    return {'groups': [{'name': '板件候选', 'classification': 'make',
                        'solid_ids': ['S0001'], 'reason': '实体轮廓为薄板，仍需核对材料。'}], 'warnings': []}


class FakeBridge:
    calls = []
    answer = None

    def __init__(self, *args, **kwargs):
        pass

    def _lock(self):
        return nullcontext()

    def _rpc(self, method, body, **kwargs):
        self.calls.append((method, deepcopy(body)))
        if method == 'session.create':
            return {'sessionId': body['sessionId']}
        if method in {'session.prompt', 'session.cancel'}:
            return {'accepted': True}
        if method == 'session.history':
            return {'events': [
                {'event': {'seq': 1, 'type': 'assistant/message', 'data': {'turn': 1,
                   'message': {'content': [{'type': 'text', 'text': json.dumps(self.answer)}]}}}},
                {'event': {'seq': 2, 'type': 'turn/end', 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}},
            ]}
        raise AssertionError(method)


class ManufacturingAITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name) / 'native'
        self.folder.mkdir()
        (self.folder / 'contact-001.png').write_bytes(b'PNG fixture; not a model input')
        FakeBridge.calls, FakeBridge.answer = [], payload()

    def run_ai(self, **kwargs):
        with patch('cad_manufacturing_ai.DshBridge', FakeBridge):
            return suggest(self.folder, geometry(), 'http://127.0.0.1:3080', poll_interval=0, **kwargs)

    def test_bound_result_stays_unconfirmed_with_unknowns_null(self):
        part = self.run_ai()['groups'][0]
        self.assertEqual(part['solid_ids'], ['S0001'])
        self.assertEqual(part['status'], 'pending_review')
        for field in ('material', 'thickness_mm', 'quantity_per_set'):
            self.assertIsNone(part[field])
        prompt = next(body for method, body in FakeBridge.calls if method == 'session.prompt')['content'][0]['text']
        self.assertIn('read/read_image', prompt)
        self.assertIn('geometry-summary.json', prompt)

    def test_same_evidence_reuses_result_without_another_prompt(self):
        first = self.run_ai()
        count = len(FakeBridge.calls)
        self.assertEqual(self.run_ai(), first)
        self.assertEqual(len(FakeBridge.calls), count)

    def test_reader_root_is_separate_from_inquiry(self):
        reader_root = Path(self.temporary.name) / 'separate-readers'
        self.run_ai(reader_root=reader_root)
        created = next(body for method, body in FakeBridge.calls if method == 'session.create')
        self.assertTrue(Path(created['cwd']).is_relative_to(reader_root.resolve()))
        self.assertFalse((self.folder / 'ai').exists())
        summary = json.loads((Path(created['cwd']) / 'geometry-summary.json').read_text())
        self.assertNotIn('mesh', summary['solids'][0])

    def test_invalid_identity_or_confirmation_is_rejected(self):
        for change in ({'solid_ids': ['OTHER']}, {'solid_ids': ['S0001', 'S0001']}, {'status': 'confirmed'}):
            bad = payload()
            bad['groups'][0].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                normalize_suggestions(bad, geometry())

    def test_missing_solid_is_retained(self):
        result = normalize_suggestions({'groups': [], 'warnings': []}, geometry())
        self.assertEqual(result['groups'][0]['classification'], 'unclassified')
        self.assertEqual(result['groups'][0]['solid_ids'], ['S0001'])

    def test_unlike_solids_require_design_review_before_becoming_one_part(self):
        source, model = geometry(), payload()
        source['solids'].append({'id': 'S0002', 'name': 'Other', 'definition_id': 'D2'})
        model['groups'][0]['solid_ids'].append('S0002')
        result = normalize_suggestions(model, source)
        self.assertEqual(result['groups'][0]['classification'], 'needs_design')
        self.assertTrue(result['warnings'])

    def test_timeout_only_cancels_owned_session(self):
        with self.assertRaises(TimeoutError):
            self.run_ai(timeout_seconds=0)
        created = next(body for method, body in FakeBridge.calls if method == 'session.create')
        cancelled = next(body for method, body in FakeBridge.calls if method == 'session.cancel')
        self.assertEqual(created['sessionId'], cancelled['sessionId'])

    def test_changed_cached_result_is_rejected(self):
        self.run_ai()
        (self.folder / 'ai' / 'suggestions.json').write_text('{}')
        with self.assertRaises(ValueError):
            self.run_ai()

    def test_image_name_cannot_escape_native_output(self):
        source = geometry()
        source['contact_sheets'] = ['../outside.png']
        with patch('cad_manufacturing_ai.DshBridge', FakeBridge), self.assertRaises(ValueError):
            suggest(self.folder, source, 'http://127.0.0.1:3080')
        self.assertEqual(FakeBridge.calls, [])


if __name__ == '__main__':
    unittest.main()
