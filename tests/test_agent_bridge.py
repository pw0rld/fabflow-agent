"""Existing reader contract through real AgentBridge + durable AgentsRuntime.

The executor below is a deterministic test double, not a model. It invokes the
real SDK attachment tool to check actual synthetic PNG bytes. No provider or DSH
request is made. All execution belongs on the configured isolated build server.
"""
import asyncio
import base64
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_bridge import AgentBridge, LegacyDshBridge, provenance_kind, selected_backend
from agent_config import AgentConfig, AgentExecutionError
from agent_runtime import AgentsRuntime
from dsh_bridge import MANIFEST, READER_INSTRUCTIONS


def candidate(reference='drawing-1'):
    return {'name': 'thickness', 'raw_value': '1.20', 'raw_unit': 'mm', 'value': '1.20', 'unit': 'mm',
            'evidence': {'reference': reference, 'page': 1, 'region': '合成测试图的t标注'},
            'reason': '确定性测试候选，未调用识别模型', 'status': 'verified'}


class DeterministicExecutor:
    def __init__(self, source_bytes, *, blocking=False, failure=None, reference='drawing-1'):
        self.source_bytes, self.failure = source_bytes, failure
        self.payload = {'candidates': [candidate(reference)], 'warnings': []}
        self.entered, self.release, self.finished = threading.Event(), threading.Event(), threading.Event()
        self.calls, self.image_checks = [], []
        self.diagnostic = None
        if not blocking:
            self.release.set()

    async def run(self, prompt, instructions, tools, emit=None, cancelled=None):
        self.calls.append({'prompt': prompt, 'instructions': instructions, 'tools': [tool.name for tool in tools]})
        try:
            by_name = {tool.name: tool for tool in tools}
            if set(by_name) != {'read', 'read_image'}:
                raise AssertionError('The reader must expose only the two registered attachment tools')
            if emit:
                emit({'type': 'step/start', 'data': {'step': 1}})
                emit({'type': 'tool/call', 'data': {'name': 'read_image', 'callId': 'bridge-image-1'}})
            from agents.tool_context import ToolContext
            arguments = json.dumps({'path': 'drawing-1'})
            context = ToolContext(context=None, tool_name='read_image', tool_call_id='bridge-image-1',
                                  tool_arguments=arguments)
            image = await by_name['read_image'].on_invoke_tool(context, arguments)
            if image.type != 'image' or image.detail != 'high':
                raise AssertionError('Image result must retain the SDK image type and supported detail')
            header, encoded = image.image_url.split(',', 1)
            if header != 'data:image/png;base64' or base64.b64decode(encoded, validate=True) != self.source_bytes:
                raise AssertionError('The model-facing tool must contain exact registered image bytes')
            self.image_checks.append({'format': 'image/png', 'bytes': len(self.source_bytes),
                                      'sha256': hashlib.sha256(self.source_bytes).hexdigest()})
            if emit:
                emit({'type': 'tool/result', 'data': {'message': {'source': {'callId': 'bridge-image-1'}}}})
            self.entered.set()
            while not self.release.is_set():
                # A late result deliberately ignores cancellation. The real
                # runtime must retain the stopped terminal state regardless.
                await asyncio.sleep(.005)
            if self.failure:
                raise self.failure
            return {'text': json.dumps(self.payload, ensure_ascii=False), 'status': 'completed',
                    'model': 'deterministic-test-model', 'model_api': 'responses',
                    'usage': {'requests': 0, 'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0, 'tool_calls': 1}}
        except BaseException as error:
            self.diagnostic = type(error).__name__ + ': ' + str(error)
            raise
        finally:
            self.finished.set()


class AgentBridgeTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image, ImageDraw
        temporary = tempfile.TemporaryDirectory(prefix='fabflow-agent-bridge-')
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.file = self.workspace / 'input.png'
        stream = io.BytesIO()
        with Image.new('RGB', (180, 80), 'white') as image:
            ImageDraw.Draw(image).text((10, 25), 'SYNTHETIC: t = 1.20 mm', fill='black')
            image.save(stream, format='PNG')
        self.original = stream.getvalue()
        self.file.write_bytes(self.original)
        self.files = [{'id': 'drawing-1', 'path': str(self.file), 'original_name': '合成测试图.png'}]
        self.output = self.workspace / 'candidates.json'
        self.config = AgentConfig(model='deterministic-test-model', api_key='offline-test-key',
                                  max_runtime_seconds=10, max_requests=1, max_input_bytes=2 * 1024 * 1024)
        self.runtimes, self.executors = [], []
        environment = patch.dict('os.environ', {'FABFLOW_AGENT_BACKEND': 'openai_agents'})
        environment.start(); self.addCleanup(environment.stop)

    def tearDown(self):
        for executor in self.executors:
            executor.release.set()
        for runtime in self.runtimes:
            runtime.close(wait=True)

    def bridge(self, executor=None):
        executor = executor or DeterministicExecutor(self.original)
        runtime = AgentsRuntime(self.workspace, self.config, executor)
        self.runtimes.append(runtime); self.executors.append(executor)
        return AgentBridge(None, self.workspace, runtime=runtime), runtime, executor

    def start(self, bridge, **options):
        result = bridge.start('TASK-AGENTS-1', deepcopy(self.files), '只读取本轮合成图，不确认工程结果。', self.output, **options)
        self.assertEqual(result['status'], 'running', result)
        self.assertTrue(result['session_id'].startswith('session-'))
        return result['session_id']

    def terminal(self, bridge, sid):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = bridge.inspect(sid)
            if result['status'] in {'completed', 'failed', 'stopped'}:
                return result
            time.sleep(.01)
        self.fail('The deterministic bridge run did not reach a terminal state')

    def no_legacy(self):
        return patch.object(LegacyDshBridge, '_rpc', side_effect=AssertionError('Default path must not contact DSH'))

    def test_complete_reader_flow_uses_real_image_tool_and_preserves_pending_candidates(self):
        bridge, runtime, executor = self.bridge()
        with self.no_legacy() as old_rpc:
            sid = self.start(bridge)
            result = self.terminal(bridge, sid)
            self.assertEqual(result['status'], 'completed', {'result': result, 'executor': executor.diagnostic})
            self.assertEqual(bridge.backend, 'openai_agents')
            self.assertEqual(provenance_kind(bridge), 'openai-agents')
            self.assertEqual(len(executor.calls), 1)
            self.assertEqual(executor.calls[0]['instructions'], READER_INSTRUCTIONS)
            self.assertIn(str(self.file), executor.calls[0]['prompt'])
            self.assertEqual(executor.image_checks[0]['sha256'], hashlib.sha256(self.original).hexdigest())
            normalized = result['candidates'][0]
            self.assertEqual(normalized['status'], 'pending_review')
            self.assertEqual((normalized['raw_value'], normalized['raw_unit'], normalized['value'], normalized['unit']),
                             ('1.20', 'mm', '1.20', 'mm'))
            self.assertEqual(normalized['evidence']['reference'], 'drawing-1')
            saved = json.loads(self.output.read_bytes())
            self.assertEqual(saved['candidates'], result['candidates'])
            self.assertEqual(self.file.read_bytes(), self.original)
            pinned = json.loads((self.workspace / MANIFEST).read_bytes())
            self.assertEqual(pinned['files'][0]['sha256'], hashlib.sha256(self.original).hexdigest())
            before = self.output.read_bytes()
            again = bridge.inspect(sid)
            self.assertEqual(again['status'], 'completed')
            self.assertEqual(self.output.read_bytes(), before)
            self.assertFalse(runtime.call('session.list', {})['items'][0]['running'])
            old_rpc.assert_not_called()

    def test_manifest_and_session_are_persisted_before_callback_and_prompt_admission(self):
        bridge, runtime, executor = self.bridge()
        callbacks = []
        def on_session(sid):
            saved = json.loads((self.workspace / MANIFEST).read_bytes())
            self.assertEqual(saved['session_id'], sid)
            self.assertEqual(saved['phase'], 'created')
            self.assertEqual(runtime.call('session.history', {'sessionId': sid})['events'], [])
            self.assertEqual(executor.calls, [])
            callbacks.append(sid)
        with self.no_legacy():
            sid = self.start(bridge, on_session=on_session)
            self.assertEqual(self.terminal(bridge, sid)['status'], 'completed')
        self.assertEqual(callbacks, [sid])

    def test_reopening_existing_manifest_inspects_the_same_session_without_dispatching_again(self):
        bridge, runtime, executor = self.bridge()
        with self.no_legacy() as old_rpc:
            sid = self.start(bridge)
            self.assertEqual(self.terminal(bridge, sid)['status'], 'completed')
            original_output = self.output.read_bytes()
            reopened = AgentBridge(None, self.workspace, runtime=runtime)
            reused = reopened.start('TASK-AGENTS-1', self.files, '只读取本轮合成图，不确认工程结果。', self.output)
            self.assertEqual(reused['session_id'], sid)
            self.assertTrue(reused['reused'])
            self.assertTrue(reused['requires_inspect'])
            self.assertEqual(reopened.inspect(sid)['status'], 'completed')
            self.assertEqual(self.output.read_bytes(), original_output)
            self.assertEqual(len(executor.calls), 1)
            old_rpc.assert_not_called()

    def test_executor_failure_does_not_fall_back_to_dsh_or_create_candidates(self):
        failure = AgentExecutionError('tool_budget_exceeded', 'SYNTHETIC_PRIVATE_DIAGNOSTIC')
        bridge, runtime, executor = self.bridge(DeterministicExecutor(self.original, failure=failure))
        with self.no_legacy() as old_rpc:
            sid = self.start(bridge)
            result = self.terminal(bridge, sid)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error']['code'], 'tool_budget_exceeded')
            self.assertNotIn('SYNTHETIC_PRIVATE_DIAGNOSTIC', json.dumps(result))
            self.assertEqual(result['candidates'], [])
            self.assertFalse(self.output.exists())
            self.assertEqual(len(executor.calls), 1)
            self.assertEqual(bridge.inspect(sid)['status'], 'failed')
            self.assertEqual(len(executor.calls), 1)
            old_rpc.assert_not_called()

    def test_stop_contract_preserves_stopped_state_when_executor_returns_a_late_success(self):
        bridge, runtime, executor = self.bridge(DeterministicExecutor(self.original, blocking=True))
        with self.no_legacy() as old_rpc:
            sid = self.start(bridge)
            self.assertTrue(executor.entered.wait(5), executor.diagnostic or 'Deterministic image tool did not finish')
            self.assertEqual(bridge.inspect(sid)['status'], 'running')
            stopped = bridge.stop(sid)
            self.assertEqual((stopped['accepted'], stopped['status']), (True, 'stopping'))
            self.assertTrue(json.loads((self.workspace / MANIFEST).read_bytes())['stop_requested'])
            self.assertEqual(self.terminal(bridge, sid)['status'], 'stopped')
            executor.release.set()
            self.assertTrue(executor.finished.wait(5))
            runtime.close(wait=True)
            final = bridge.inspect(sid)
            self.assertEqual(final['status'], 'stopped')
            self.assertEqual(final['candidates'], [])
            self.assertFalse(self.output.exists())
            self.assertEqual(len(executor.calls), 1)
            old_rpc.assert_not_called()

    def test_changed_attachment_during_execution_cannot_be_adopted_as_completed(self):
        bridge, runtime, executor = self.bridge(DeterministicExecutor(self.original, blocking=True))
        with self.no_legacy() as old_rpc:
            sid = self.start(bridge)
            self.assertTrue(executor.entered.wait(5))
            self.file.write_bytes(b'changed after the source manifest was frozen')
            executor.release.set()
            result = self.terminal(bridge, sid)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['candidates'], [])
            self.assertFalse(self.output.exists())
            self.assertEqual(len(executor.calls), 1)
            old_rpc.assert_not_called()

    def test_bridge_rechecks_original_hash_even_after_runtime_completed_before_inspect(self):
        bridge, runtime, executor = self.bridge()
        with self.no_legacy():
            sid = self.start(bridge)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                history = runtime.call('session.history', {'sessionId': sid})['events']
                if any(event['type'] == 'turn/end' for event in history):
                    break
                time.sleep(.01)
            self.assertTrue(any(event['type'] == 'assistant/message' for event in history))
            self.file.write_bytes(b'a different original at the same allowed path')
            result = bridge.inspect(sid)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error']['code'], 'invalid-candidates')
            self.assertIn('attachment changed', result['error']['message'])
            self.assertFalse(self.output.exists())

    def test_unregistered_evidence_reference_is_rejected_even_when_executor_returns_valid_json(self):
        bridge, _, _ = self.bridge(DeterministicExecutor(self.original, reference='another-order-file'))
        with self.no_legacy():
            sid = self.start(bridge)
            result = self.terminal(bridge, sid)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error']['code'], 'invalid-candidates')
            self.assertEqual(result['candidates'], [])
            self.assertFalse(self.output.exists())

    def test_existing_different_candidate_file_is_never_overwritten(self):
        bridge, _, executor = self.bridge(DeterministicExecutor(self.original, blocking=True))
        with self.no_legacy():
            sid = self.start(bridge)
            self.assertTrue(executor.entered.wait(5))
            protected = b'{"existing":"different service-owned result"}\n'
            self.output.write_bytes(protected)
            executor.release.set()
            result = self.terminal(bridge, sid)
            self.assertEqual(result['status'], 'failed')
            self.assertIn('refusing overwrite', result['error']['message'])
            self.assertEqual(self.output.read_bytes(), protected)

    def test_explicit_dsh_backend_delegates_existing_start_inspect_and_stop_to_legacy_transport(self):
        calls, session = [], {}
        text = json.dumps({'candidates': [candidate()], 'warnings': []})
        def legacy_rpc(receiver, method, payload, *, rpc_id=None):
            calls.append((method, deepcopy(payload), rpc_id))
            if method == 'session.create':
                session['id'] = payload['sessionId']
                return {'sessionId': session['id']}
            if method in {'session.prompt', 'session.cancel'}:
                return {'accepted': True}
            if method == 'session.history':
                return {'events': [
                    {'seq': 1, 'type': 'turn/start', 'data': {'turn': 1}},
                    {'seq': 2, 'type': 'assistant/message', 'data': {'turn': 1, 'message': {'content': [{'type': 'text', 'text': text}]}}},
                    {'seq': 3, 'type': 'turn/end', 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}], 'hasMore': False}
            raise AssertionError(method)
        with patch.object(LegacyDshBridge, '_rpc', autospec=True, side_effect=legacy_rpc), \
                patch('agent_runtime.AgentsRuntime', side_effect=AssertionError('Explicit legacy transport must not create an SDK runtime')):
            bridge = AgentBridge('http://127.0.0.1:3080', self.workspace, backend='dsh')
            sid = self.start(bridge)
            result = bridge.inspect(sid)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['candidates'][0]['status'], 'pending_review')
            self.assertEqual(bridge.stop(sid)['status'], 'stopping')
        self.assertEqual([method for method, _, _ in calls], ['session.create', 'session.prompt', 'session.history', 'session.cancel'])
        self.assertIsNotNone(calls[1][2])
        self.assertEqual(provenance_kind(bridge), 'dsh')
        self.assertIsNone(bridge._agent_runtime)

    def test_invalid_backend_never_silently_selects_either_transport(self):
        self.assertEqual(selected_backend(), 'openai_agents')
        self.assertEqual(selected_backend('openai-agents'), 'openai_agents')
        with self.assertRaises(ValueError):
            AgentBridge(None, self.workspace, backend='unknown-provider')


if __name__ == '__main__':
    unittest.main()
