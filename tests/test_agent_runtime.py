"""Durable runtime admission/race tests. Run on the build server only.

Executors and attachment readers are explicit fakes: these tests call no model,
CAD engine, SDK, network, production store, or upload quota.
"""
import asyncio
import copy
from dataclasses import replace
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import agent_runtime as runtime
from agent_config import AgentConfig
from agent_runtime import AgentsRuntime, AgentRuntimeError, DIRECTORY


def identity():
    return 'session-' + str(uuid.uuid4())


def fork_inspect(workspace, sid, pipe):
    """A fork must discard inherited task handles without replaying a call."""
    try:
        peer = AgentsRuntime(Path(workspace))
        pipe.send({'list': peer.call('session.list', {}),
                   'history': peer.call('session.history', {'sessionId': sid})})
    except BaseException as error:
        pipe.send({'error': type(error).__name__ + ':' + str(error)})
    finally:
        pipe.close()


def initialize_process(workspace, pipe, attempted, paused=None, release=None):
    """Hold the first constructor between mkdir and registry persistence."""
    original = AgentsRuntime._write
    def write(owner, name, value, **kwargs):
        if name == 'registry.json' and paused is not None:
            paused.set()
            if not release.wait(5):
                raise AssertionError('Initialization release was not signalled')
        return original(owner, name, value, **kwargs)
    try:
        attempted.set()
        with patch.object(AgentsRuntime, '_write', new=write):
            value = AgentsRuntime(Path(workspace))
        pipe.send({'registered': True, 'list': value.call('session.list', {})})
    except BaseException as error:
        pipe.send({'error': type(error).__name__ + ':' + str(error)})
    finally:
        pipe.close()


class Reader:
    def __init__(self, workspace):
        self.workspace = workspace
        self.original = (workspace / 'input.txt').read_bytes()

    def snapshot(self):
        return [{'id': 'source-1', 'path': str(self.workspace / 'input.txt'),
                 'sha256': hashlib.sha256(self.original).hexdigest(), 'size_bytes': len(self.original)}]

    def verify_sources(self):
        if (self.workspace / 'input.txt').read_bytes() != self.original:
            raise ValueError('Secret customer diagnostics must not leak')

    def make_sdk_tools(self):
        return []


def prepare(workspace, config):
    config.validate_for_run()
    reader = Reader(workspace)
    raw = (workspace / 'AGENTS.md').read_bytes()
    return reader, {'profile': runtime._safe_config(config.snapshot()), 'sources': reader.snapshot(),
                    'instructions': raw.decode(), 'instructions_sha256': hashlib.sha256(raw).hexdigest()}


class Executor:
    DEFAULT_USAGE = object()

    def __init__(self, text='{"candidates":[],"warnings":[]}', *, blocking=False, flood=0, failure=None, usage=DEFAULT_USAGE):
        self.text, self.flood, self.failure = text, flood, failure
        self.usage = {'requests': 1} if usage is self.DEFAULT_USAGE else usage
        self.entered, self.release, self.finished = threading.Event(), threading.Event(), threading.Event()
        self.calls = []
        if not blocking:
            self.release.set()

    async def run(self, prompt, instructions, tools, emit=None, cancelled=None):
        self.calls.append({'prompt': prompt, 'instructions': instructions, 'tools': tools})
        self.entered.set()
        try:
            emit({'type': 'step/start', 'data': {'step': 1, 'reasoning': 'SECRET_REASONING'}})
            emit({'type': 'tool/call', 'data': {'name': 'read_image', 'callId': 'call-1', 'arguments': 'SECRET_ARGUMENTS'}})
            emit({'type': 'tool/result', 'data': {'message': {'source': {'callId': 'call-1'}, 'content': 'SECRET_TOOL_TEXT'}}})
            emit({'type': 'request/header', 'data': {'authorization': 'SECRET_HEADER'}})
            emit({'type': 'assistant/chunk', 'data': {'text': 'SECRET_PARTIAL'}})
            for step in range(self.flood):
                emit({'type': 'step/start', 'data': {'step': step + 2}})
            while not self.release.is_set():
                # Intentionally ignore cancellation to prove runtime's terminal CAS.
                await asyncio.sleep(.005)
            if self.failure:
                raise self.failure
            result = {'text': self.text, 'status': 'completed', 'model': 'fake'}
            if self.usage is not None:
                result['usage'] = copy.deepcopy(self.usage)
            return result
        finally:
            self.finished.set()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name).resolve()
        (self.workspace / 'AGENTS.md').write_text('Read authorized files only.')
        (self.workspace / 'input.txt').write_text('A synthetic test source, not customer data.')
        self.config = AgentConfig(model='fake-explicit-model', api_key='TEST_KEY_NOT_TO_PERSIST',
            base_url='https://secret.invalid/v1', max_runtime_seconds=1, max_input_bytes=4096,
            max_output_bytes=8192, max_tool_calls=512)
        self.patch = patch.object(runtime, '_prepare', side_effect=prepare)
        self.patch.start()
        self.runtimes, self.executors = [], []

    def tearDown(self):
        for executor in self.executors:
            executor.release.set()
        for value in self.runtimes:
            value.close(wait=True)
        self.patch.stop()
        self.temp.cleanup()

    def make(self, executor=None, config=None, workspace=None):
        executor = executor or Executor()
        value = AgentsRuntime(workspace or self.workspace, config or self.config, executor)
        self.runtimes.append(value)
        self.executors.append(executor)
        return value, executor

    def create(self, value, sid=None, rpc_id=None):
        sid = sid or identity()
        self.assertEqual(value.call('session.create', {'sessionId': sid, 'cwd': str(value.workspace),
                              'agentPreset': 'standard'}, rpc_id=rpc_id), {'sessionId': sid})
        return sid

    def prompt(self, value, sid, text='Extract the synthetic source.', rpc_id='prompt-one'):
        return value.call('session.prompt', {'sessionId': sid, 'mode': 'queue', 'clientTimeZone': 'Asia/Shanghai',
                          'content': [{'type': 'text', 'text': text}]}, rpc_id=rpc_id)

    def state(self, value, sid):
        return value._read(sid + '.json')

    def done(self, value, sid):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            state = self.state(value, sid)
            if state['phase'] in runtime.TERMINAL and (value.key, sid) not in runtime._LIVE:
                return state
            time.sleep(.005)
        self.fail('Fake executor did not settle')

    def error(self, code, call):
        with self.assertRaises(AgentRuntimeError) as context:
            call()
        self.assertEqual(context.exception.code, code)

    def test_create_is_lazy_and_requires_exact_registered_workspace(self):
        value = AgentsRuntime(self.workspace)
        self.runtimes.append(value)
        self.assertIsNone(value.config)
        sid = self.create(value)
        self.assertEqual(value.call('session.history', {'sessionId': sid})['events'], [])
        self.assertEqual(value.call('session.list', {}), {'items': [{'sessionId': sid, 'running': False}]})
        self.error('invalid-request', lambda: value.call('session.create', {'sessionId': identity(), 'cwd': str(self.workspace.parent)}))
        self.error('invalid-request', lambda: value.call('session.create', {'sessionId': '../bad', 'cwd': str(self.workspace)}))
        self.error('session-not-found', lambda: value.call('session.history', {'sessionId': identity()}))

    def test_concurrent_http_instances_wait_for_complete_initial_registration(self):
        original = AgentsRuntime._write
        paused, release, second_attempted, second_finished = (threading.Event() for _ in range(4))
        values, errors = [], []
        def write(owner, name, value, **kwargs):
            if name == 'registry.json':
                paused.set()
                if not release.wait(5):
                    raise AssertionError('Initialization release was not signalled')
            return original(owner, name, value, **kwargs)
        def construct(second=False):
            if second:
                second_attempted.set()
            try:
                values.append(AgentsRuntime(self.workspace))
            except BaseException as error:
                errors.append(error)
            finally:
                if second:
                    second_finished.set()
        first, second = threading.Thread(target=construct), threading.Thread(target=construct, args=(True,))
        with patch.object(AgentsRuntime, '_write', new=write):
            try:
                first.start()
                self.assertTrue(paused.wait(2))
                second.start()
                self.assertTrue(second_attempted.wait(2))
                self.assertFalse(second_finished.wait(.1), 'Peer read a partially initialized store')
            finally:
                release.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
        self.runtimes.extend(values)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(values), 2)
        self.assertEqual(values[0]._registration, values[1]._registration)
        self.assertEqual(values[0].call('session.list', {}), {'items': []})
        self.assertEqual({path.name for path in self.workspace.iterdir()}, {'AGENTS.md', 'input.txt', DIRECTORY})

    @unittest.skipUnless(sys.platform == 'linux', 'Directory flock is verified on ZeroClave')
    def test_cross_process_initialization_holds_directory_lock_until_registered(self):
        context = multiprocessing.get_context('fork')
        paused, release, attempted1, attempted2 = (context.Event() for _ in range(4))
        read1, write1 = context.Pipe(duplex=False)
        read2, write2 = context.Pipe(duplex=False)
        first = context.Process(target=initialize_process, args=(str(self.workspace), write1, attempted1, paused, release))
        second = context.Process(target=initialize_process, args=(str(self.workspace), write2, attempted2))
        try:
            first.start()
            write1.close()
            self.assertTrue(paused.wait(3))
            second.start()
            write2.close()
            self.assertTrue(attempted2.wait(3))
            self.assertFalse(read2.poll(.1), 'Peer bypassed the workspace directory initialization lock')
            release.set()
            self.assertTrue(read1.poll(5))
            self.assertTrue(read2.poll(5))
            for answer in (read1.recv(), read2.recv()):
                self.assertEqual(answer, {'registered': True, 'list': {'items': []}})
        finally:
            release.set()
            for child in (first, second):
                if child.pid is not None:
                    child.join(5)
                    if child.is_alive():
                        child.terminate()
                        child.join(2)
        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        value = AgentsRuntime(self.workspace)
        self.runtimes.append(value)
        self.assertEqual(value.call('session.list', {}), {'items': []})

    def test_durable_server_inputs_precede_executor_and_rpc_replay_never_resends(self):
        value, executor = self.make(Executor(blocking=True))
        sid = self.create(value, rpc_id='create-one')
        self.prompt(value, sid)
        self.assertTrue(executor.entered.wait(2))
        state = self.state(value, sid)
        self.assertIn(state['phase'], runtime.ACTIVE)
        self.assertEqual(state['prompt_inputs']['content'][0]['text'], executor.calls[0]['prompt'])
        self.assertEqual(state['server_inputs']['profile'], self.config.snapshot())
        self.assertEqual(state['evidence_sha256'], runtime._digest(state['server_inputs']))
        self.assertEqual(state['server_inputs']['sources'][0]['sha256'], hashlib.sha256((self.workspace / 'input.txt').read_bytes()).hexdigest())
        self.assertTrue(self.prompt(value, sid)['replayed'])
        self.assertTrue(self.prompt(value, sid, rpc_id='new-id-same-prompt')['replayed'])
        self.assertEqual(len(executor.calls), 1)
        self.error('request-conflict', lambda: self.prompt(value, sid, text='Different text.'))
        self.error('request-conflict', lambda: self.create(value, rpc_id='prompt-one'))
        persisted = b''.join(path.read_bytes() for path in value.root.iterdir() if path.is_file())
        self.assertNotIn(b'TEST_KEY_NOT_TO_PERSIST', persisted)
        self.assertNotIn(b'secret.invalid', persisted)
        executor.release.set()
        self.assertEqual(self.done(value, sid)['phase'], 'completed')
        self.assertTrue(self.prompt(value, sid)['replayed'])
        self.assertEqual(len(executor.calls), 1)

    def test_history_keeps_turn_start_complete_json_and_terminal_under_limit(self):
        value, _ = self.make(Executor(flood=130))
        sid = self.create(value)
        self.prompt(value, sid)
        state = self.done(value, sid)
        self.assertEqual(state['phase'], 'completed')
        history = value.call('session.history', {'sessionId': sid, 'maxMessages': 100})
        events = history['events']
        self.assertEqual(len(events), 100)
        self.assertEqual(events[0]['type'], 'turn/start')
        self.assertEqual(events[-1]['type'], 'turn/end')
        self.assertEqual(events[-1]['data']['reason']['kind'], 'completed')
        message = next(row for row in events if row['type'] == 'assistant/message')
        self.assertEqual(json.loads(message['data']['message']['content'][0]['text']), {'candidates': [], 'warnings': []})
        serialized = json.dumps(history)
        for private in ('SECRET_REASONING', 'SECRET_ARGUMENTS', 'SECRET_TOOL_TEXT', 'SECRET_HEADER', 'SECRET_PARTIAL'):
            self.assertNotIn(private, serialized)
        self.assertEqual([row['seq'] for row in events], sorted({row['seq'] for row in events}))
        self.assertTrue(all(type(row['time']) is int and row['data']['turn'] == 1 for row in events))

    def test_real_tool_occurrences_are_projected_without_tool_contents(self):
        value, _ = self.make()
        sid = self.create(value)
        self.prompt(value, sid)
        self.done(value, sid)
        events = value.call('session.history', {'sessionId': sid})['events']
        call = next(event for event in events if event['type'] == 'tool/call')
        result = next(event for event in events if event['type'] == 'tool/result')
        self.assertEqual(call['data'], {'turn': 1, 'name': 'read_image', 'callId': 'call-1'})
        self.assertEqual(result['data'], {'turn': 1, 'message': {'source': {'callId': 'call-1'}}})
        self.assertNotIn('request/header', [row['type'] for row in events])

    def test_peer_instance_inspects_same_task_and_does_not_cancel_on_its_close(self):
        value, executor = self.make(Executor(blocking=True))
        sid = self.create(value)
        self.prompt(value, sid)
        self.assertTrue(executor.entered.wait(2))
        peer, other = self.make()
        self.assertEqual(peer.call('session.list', {})['items'], [{'sessionId': sid, 'running': True}])
        self.assertTrue(self.prompt(peer, sid)['replayed'])
        peer.close()
        self.assertFalse(runtime._LIVE[(value.key, sid)].cancelled.is_set())
        executor.release.set()
        self.assertEqual(self.done(value, sid)['phase'], 'completed')
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(other.calls, [])

    def test_cancel_terminal_wins_against_ignored_cancellation_and_late_success(self):
        value, executor = self.make(Executor(blocking=True))
        sid = self.create(value)
        self.prompt(value, sid)
        self.assertTrue(executor.entered.wait(2))
        self.assertEqual(value.call('session.cancel', {'sessionId': sid}), {'accepted': True})
        snapshot = (value.root / (sid + '.json')).read_bytes()
        executor.release.set()
        self.assertEqual(self.done(value, sid)['phase'], 'stopped')
        self.assertEqual(snapshot, (value.root / (sid + '.json')).read_bytes())
        self.assertFalse((value.root / (sid + '.result.json')).exists())
        self.assertEqual(value.call('session.history', {'sessionId': sid})['events'][-1]['data']['reason']['kind'], 'aborted')

    def test_shutdown_stops_active_but_never_rewrites_completed_task(self):
        value, executor = self.make(Executor(blocking=True))
        sid = self.create(value)
        self.prompt(value, sid)
        self.assertTrue(executor.entered.wait(2))
        value.shutdown()
        executor.release.set()
        self.assertEqual(self.done(value, sid)['phase'], 'stopped')
        self.error('runtime-closed', lambda: self.create(value))
        other, _ = self.make()
        second = self.create(other)
        self.prompt(other, second, rpc_id='prompt-second')
        self.done(other, second)
        original = (other.root / (second + '.json')).read_bytes()
        other.close()
        other.call('session.cancel', {'sessionId': second})
        self.assertEqual(original, (other.root / (second + '.json')).read_bytes())

    def test_shutdown_all_is_nonblocking_and_only_finishes_registered_local_tasks(self):
        active = []
        for number in range(runtime.MAX_ACTIVE):
            value, executor = self.make(Executor(blocking=True))
            sid = self.create(value)
            self.prompt(value, sid, rpc_id='shutdown-' + str(number))
            self.assertTrue(executor.entered.wait(2))
            active.append((value, executor, sid))
        started = time.monotonic()
        runtime.shutdown_all()
        self.assertLess(time.monotonic() - started, 1)
        for value, executor, sid in active:
            self.assertEqual(self.state(value, sid)['phase'], 'stopped')
            self.assertFalse(executor.finished.is_set())
            executor.release.set()
            self.assertEqual(self.done(value, sid)['phase'], 'stopped')

    def test_orphaned_durable_admission_is_interrupted_without_reexecution(self):
        value, executor = self.make()
        sid = self.create(value)
        # A durable queued record left by a process that exited before starting
        # a provider request is intentionally indistinguishable from an
        # ambiguous sent request. Neither is automatically submitted again.
        state = self.state(value, sid)
        payload = {'sessionId': sid, 'mode': 'queue', 'clientTimeZone': 'Asia/Shanghai',
                   'content': [{'type': 'text', 'text': 'Extract the synthetic source.'}]}
        state.update(phase='queued', prompt_inputs=payload, prompt_digest=runtime._digest(payload),
                     owner={'pid': 2**30, 'birth': 'obsolete', 'boot': 'obsolete'}, admitted_at=runtime._now())
        runtime.AgentsRuntime._event(state, 'turn/start', {})
        value._save(state)
        peer, other = self.make()
        self.assertEqual(peer.call('session.list', {})['items'], [{'sessionId': sid, 'running': False}])
        self.assertEqual(self.state(peer, sid)['phase'], 'interrupted')
        self.assertTrue(self.prompt(peer, sid)['replayed'])
        self.assertEqual(executor.calls, [])
        self.assertEqual(other.calls, [])

    @unittest.skipUnless(sys.platform == 'linux', 'Real process lease contract is checked on ZeroClave')
    def test_fork_discards_inherited_worker_handles_and_respects_parent_lease(self):
        value, executor = self.make(Executor(blocking=True))
        sid = self.create(value)
        self.prompt(value, sid)
        self.assertTrue(executor.entered.wait(2))
        context = multiprocessing.get_context('fork')
        left, right = context.Pipe(duplex=False)
        child = context.Process(target=fork_inspect, args=(str(self.workspace), sid, right))
        child.start()
        right.close()
        self.assertTrue(left.poll(4))
        answer = left.recv()
        child.join(4)
        self.assertEqual(child.exitcode, 0)
        self.assertNotIn('error', answer)
        self.assertEqual(answer['list']['items'], [{'sessionId': sid, 'running': True}])
        self.assertFalse(any(row['type'] == 'turn/end' for row in answer['history']['events']))
        executor.release.set()
        self.assertEqual(self.done(value, sid)['phase'], 'completed')
        self.assertEqual(len(executor.calls), 1)

    def test_invalid_configuration_and_nontext_inputs_fail_before_admission(self):
        value, executor = self.make(config=replace(self.config, model=None))
        sid = self.create(value)
        self.error('model_not_configured', lambda: self.prompt(value, sid))
        self.assertEqual(self.state(value, sid)['phase'], 'created')
        self.assertEqual(executor.calls, [])
        self.error('invalid-request', lambda: value.call('session.prompt', {'sessionId': sid, 'mode': 'queue',
                   'content': [{'type': 'image', 'text': 'x'}]}))
        self.error('invalid-request', lambda: value.call('session.prompt', {'sessionId': sid, 'mode': 'queue',
                   'content': [{'type': 'text', 'text': 'x', 'url': 'https://invalid'}]}))

    def test_environment_configuration_errors_are_runtime_errors_before_admission(self):
        executor = Executor()
        value = AgentsRuntime(self.workspace, executor=executor)
        self.runtimes.append(value)
        self.executors.append(executor)
        sid = self.create(value)
        with patch.dict(os.environ, {'FABFLOW_MODEL': 'explicit-test', 'OPENAI_API_KEY': 'PRIVATE_KEY',
                                     'FABFLOW_MAX_TURNS': 'PRIVATE_INVALID_LIMIT'}, clear=True):
            with self.assertRaises(AgentRuntimeError) as caught:
                self.prompt(value, sid)
        self.assertEqual(caught.exception.code, 'configuration_error')
        self.assertNotIn('PRIVATE_', str(caught.exception))
        self.assertEqual(self.state(value, sid)['phase'], 'created')
        self.assertNotIn('prompt_digest', self.state(value, sid))
        self.assertEqual(value.call('session.history', {'sessionId': sid})['events'], [])
        self.assertEqual(executor.calls, [])

    def test_bounded_threads_reject_extra_prompt_without_admitting_it(self):
        active = []
        for number in range(runtime.MAX_ACTIVE):
            value, executor = self.make(Executor(blocking=True))
            sid = self.create(value)
            self.prompt(value, sid, rpc_id='bounded-' + str(number))
            self.assertTrue(executor.entered.wait(2))
            active.append((value, executor, sid))
        value, executor = self.make()
        sid = self.create(value)
        self.error('capacity-error', lambda: self.prompt(value, sid, rpc_id='bounded-overflow'))
        self.assertEqual(self.state(value, sid)['phase'], 'created')
        self.assertEqual(executor.calls, [])
        for owner, running, identity in active:
            running.release.set()
            self.done(owner, identity)

    def test_oversized_or_truncated_json_never_becomes_success(self):
        for number, text in enumerate(('{"broken":', '{"value":"' + 'a' * 9000 + '"}', '[]')):
            with self.subTest(number=number):
                value, _ = self.make(Executor(text))
                sid = self.create(value)
                self.prompt(value, sid, rpc_id='bad-output-' + str(number))
                self.assertEqual(self.done(value, sid)['phase'], 'failed')
                events = value.call('session.history', {'sessionId': sid})['events']
                self.assertFalse(any(row['type'] == 'assistant/message' for row in events))
                self.assertFalse((value.root / (sid + '.result.json')).exists())

    def test_source_or_instruction_mutation_during_execution_fails_without_result(self):
        for filename in ('input.txt', 'AGENTS.md'):
            with self.subTest(filename=filename):
                path = self.workspace / filename
                original = path.read_bytes()
                value, executor = self.make(Executor(blocking=True))
                sid = self.create(value)
                self.prompt(value, sid, rpc_id='mutation-' + filename.replace('.', '-'))
                self.assertTrue(executor.entered.wait(2))
                path.write_text('Changed while running.')
                executor.release.set()
                self.assertEqual(self.done(value, sid)['phase'], 'failed')
                self.assertFalse((value.root / (sid + '.result.json')).exists())
                path.write_bytes(original)

    def test_registration_symlink_tamper_and_oversized_state_fail_closed(self):
        value, _ = self.make()
        sid = self.create(value)
        state_path = value.root / (sid + '.json')
        original = state_path.read_bytes()
        changed = json.loads(original)
        changed['value']['create_digest'] = '0' * 64
        state_path.write_text(json.dumps(changed))
        self.error('state-tampered', lambda: value.call('session.history', {'sessionId': sid}))
        state_path.write_bytes(original)
        state_path.unlink()
        state_path.symlink_to(self.workspace / 'input.txt')
        self.error('state-tampered', lambda: value.call('session.history', {'sessionId': sid}))
        state_path.unlink()
        state_path.write_bytes(original)
        state_path.chmod(0o600)
        with patch.object(runtime, 'MAX_STATE_BYTES', 100):
            # Explicit maximum bypasses the function default argument captured
            # at definition time and exercises the actual bounded file read.
            self.error('state-tampered', lambda: value._read(sid + '.json', 100))
        registration = value.root / 'registry.json'
        registration.write_text('{}')
        self.error('state-tampered', lambda: value.call('session.list', {}))

    def test_terminal_history_list_and_result_integrity_are_read_only(self):
        value, _ = self.make()
        sid = self.create(value)
        self.prompt(value, sid)
        self.done(value, sid)
        before = {path.name: path.read_bytes() for path in value.root.iterdir() if path.is_file()}
        value.call('session.history', {'sessionId': sid})
        value.call('session.list', {})
        self.assertEqual(before, {path.name: path.read_bytes() for path in value.root.iterdir() if path.is_file()})
        result = value.root / (sid + '.result.json')
        result.write_text('{"not":"the persisted result"}')
        self.error('state-tampered', lambda: value.call('session.history', {'sessionId': sid}))

    def test_observed_usage_receipts_are_private_hashed_and_never_fill_missing_values_with_zero(self):
        full = {'requests': 1, 'input_tokens': 18, 'output_tokens': 7, 'total_tokens': 25, 'tool_calls': 1}
        for number, usage in enumerate((full, None, {'requests': 1})):
            with self.subTest(usage=usage):
                value, _ = self.make(Executor(usage=usage))
                sid = self.create(value)
                self.prompt(value, sid, rpc_id='audit-' + str(number))
                state = self.done(value, sid)
                self.assertEqual(state['phase'], 'completed')
                saved = value._read(sid + '.result.json')
                receipt = saved['receipt']
                self.assertEqual(receipt['usage'], usage)
                self.assertEqual(receipt['usage_complete'], usage == full)
                self.assertEqual(receipt['usage_source'], 'unavailable' if usage is None else 'executor_observed')
                self.assertEqual(receipt['model'], self.config.model)
                self.assertEqual(receipt['model_api'], self.config.model_api)
                self.assertEqual(receipt['model_source'], 'saved_server_profile')
                self.assertEqual(receipt['prompt_digest'], state['prompt_digest'])
                self.assertEqual(receipt['evidence_sha256'], state['evidence_sha256'])
                self.assertEqual(receipt['result_sha256'], state['result_sha256'])
                self.assertEqual(saved['receipt_sha256'], state['result_receipt_sha256'])
                self.assertEqual(saved['receipt_sha256'], runtime._digest(receipt))
                history = json.dumps(value.call('session.history', {'sessionId': sid}))
                self.assertNotIn('input_tokens', history)
                self.assertNotIn(self.config.model, history)
                self.assertNotIn('receipt_sha256', history)
                # Even recomputing the outer storage checksum cannot replace
                # the receipt pinned in the independently saved session state.
                saved['receipt']['usage'] = {'requests': 999}
                value._write(sid + '.result.json', saved)
                self.error('state-tampered', lambda: value.call('session.history', {'sessionId': sid}))

    def test_invalid_usage_types_and_inconsistent_totals_do_not_produce_success(self):
        for number, usage in enumerate(({'requests': True}, {'tool_calls': -1},
                {'input_tokens': 10, 'output_tokens': 3, 'total_tokens': 2}, {'authorization': 'PRIVATE_KEY'})):
            with self.subTest(usage=usage):
                value, _ = self.make(Executor(usage=usage))
                sid = self.create(value)
                self.prompt(value, sid, rpc_id='invalid-audit-' + str(number))
                self.assertEqual(self.done(value, sid)['phase'], 'failed')
                self.assertFalse((value.root / (sid + '.result.json')).exists())
                events = value.call('session.history', {'sessionId': sid})['events']
                self.assertEqual(events[-1]['data']['reason']['error']['code'], 'invalid-usage')
                self.assertNotIn('PRIVATE_KEY', json.dumps(events))


if __name__ == '__main__':
    unittest.main()
