"""Durable, workspace-local admission for the OpenAI Agents executor.

These five methods are an internal adapter contract, not an HTTP service.
An admitted prompt is never automatically submitted again, even after a crash.
Only complete, bounded results and allowlisted progress events leave this store.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
import uuid


DIRECTORY = '.fabflow-agent-runtime'
MAX_STATE_BYTES = 40 * 1024 * 1024
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 512
MAX_SESSIONS = 1024
MAX_ACTIVE = 2
ACTIVE = {'queued', 'running'}
TERMINAL = {'completed', 'failed', 'stopped', 'interrupted', 'unknown'}
SID = re.compile(r'session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
RPC = re.compile(r'[A-Za-z0-9:_-]{1,180}\Z')
_GUARD = threading.RLock()
_PID = os.getpid()
_LIVE = {}
_SLOTS = threading.BoundedSemaphore(MAX_ACTIVE)


class AgentRuntimeError(RuntimeError):
    def __init__(self, message, code='runtime-error'):
        super().__init__(message)
        self.code = code


def _fail(message, code='invalid-request'):
    raise AgentRuntimeError(message, code)


def _json(raw):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    def invalid(_value):
        raise ValueError('Nonfinite JSON value')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _digest(value):
    return hashlib.sha256(_encode(value)).hexdigest()


def _now():
    return time.time_ns() // 1_000_000


def _owner(pid=None):
    pid = os.getpid() if pid is None else pid
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        # Fields after the final ')' start at proc field 3; starttime is 22.
        birth = Path('/proc/' + str(pid) + '/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        boot, birth = None, None
    return {'pid': pid, 'birth': birth, 'boot': boot}


def _process_registry():
    """A fork inherits no authority to execute its parent's admitted work."""
    global _PID, _LIVE, _SLOTS
    if _PID != os.getpid():
        for task in _LIVE.values():
            try:
                os.close(task.lease)
            except OSError:
                pass
        _PID, _LIVE, _SLOTS = os.getpid(), {}, threading.BoundedSemaphore(MAX_ACTIVE)


def _after_fork():
    global _GUARD
    _GUARD = threading.RLock()
    _process_registry()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


def _workspace(value):
    path = Path(value)
    if not path.is_absolute() or str(path) != str(path.resolve(strict=True)):
        _fail('Workspace must be its exact absolute, nonsymlink path.')
    for ancestor in (path, *path.parents):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _fail('Workspace ancestors must be real directories.')
    return path


def _sid(value):
    if not isinstance(value, str) or not SID.fullmatch(value) or str(uuid.UUID(value[8:])) != value[8:]:
        _fail('A preallocated session-UUID is required.')
    return value


def _safe_config(snapshot):
    """Reject a misbehaving injected config instead of persisting credentials."""
    if not isinstance(snapshot, dict):
        _fail('Agent profile snapshot is invalid.', 'configuration-error')
    banned = {'api_key', 'apikey', 'authorization', 'headers', 'base_url', 'baseurl', 'token', 'password', 'reasoning', 'thoughts'}
    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or key.lower().replace('-', '_') in banned:
                    _fail('Agent profile snapshot contains a private field.', 'configuration-error')
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(snapshot)
    if len(_encode(snapshot)) > 64 * 1024:
        _fail('Agent profile snapshot is too large.', 'configuration-error')
    return copy.deepcopy(snapshot)


def _receipt(result, state, result_hash):
    """Private audit data; absence is unknown usage, never a fabricated zero."""
    fields = {'requests', 'input_tokens', 'output_tokens', 'total_tokens', 'tool_calls'}
    raw = result.get('usage')
    usage = None
    if raw is not None:
        if (not isinstance(raw, dict) or set(raw) - fields
                or any(type(value) is not int or not 0 <= value <= 2**63 - 1 for value in raw.values())):
            _fail('Agent returned invalid usage metadata.', 'invalid-usage')
        usage = dict(raw) if raw else None
        if usage is not None and {'input_tokens', 'output_tokens', 'total_tokens'} <= set(usage):
            if usage['total_tokens'] != usage['input_tokens'] + usage['output_tokens']:
                _fail('Agent usage totals are inconsistent.', 'invalid-usage')
    profile = state['server_inputs']['profile']
    return {'schema': 1, 'result_sha256': result_hash, 'evidence_sha256': state['evidence_sha256'],
            'prompt_digest': state['prompt_digest'], 'model': profile['model'], 'model_api': profile['model_api'],
            'model_source': 'saved_server_profile', 'usage': usage, 'usage_complete': usage is not None and set(usage) == fields,
            'usage_source': 'executor_observed' if usage is not None else 'unavailable'}


def _prepare(workspace, config):
    """Read and freeze only the server-authorized attachment manifest."""
    from agent_tools import AttachmentTools
    config.validate_for_run()
    profile = _safe_config(config.snapshot())
    reader = AttachmentTools(workspace, max_file_bytes=config.max_tool_output_bytes,
                             max_total_bytes=config.max_total_tool_bytes, max_tool_calls=config.max_tool_calls)
    instructions_path = workspace / 'AGENTS.md'
    fd = os.open(instructions_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 256 * 1024:
            _fail('Reader instructions are not a bounded regular file.', 'unsafe-workspace')
        raw = os.read(fd, 256 * 1024 + 1)
    finally:
        os.close(fd)
    instructions = raw.decode('utf-8')
    sources = reader.snapshot()
    snapshot = {'profile': profile, 'sources': sources,
                'instructions': instructions, 'instructions_sha256': hashlib.sha256(raw).hexdigest()}
    return reader, snapshot


class _Task:
    def __init__(self, runtime, sid, lease, reader, config, executor):
        self.runtime, self.sid, self.lease, self.reader = runtime, sid, lease, reader
        self.config, self.executor = config, executor
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.thread = None


class AgentsRuntime:
    def __init__(self, workspace, config=None, executor=None):
        self.workspace = _workspace(workspace)
        self.config, self.executor = config, executor
        self._closed = False
        self._owned = set()
        self._workspace_stat = self.workspace.stat()
        self.root = self.workspace / DIRECTORY
        # Lock the already-existing workspace directory itself: this creates no
        # file outside our registered store. A peer must not see mkdir without
        # the registration/admission/index files that complete initialization.
        with _GUARD:
            workspace_fd = os.open(self.workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                fcntl.flock(workspace_fd, fcntl.LOCK_EX)
                pinned = os.fstat(workspace_fd)
                if (pinned.st_dev, pinned.st_ino) != (self._workspace_stat.st_dev, self._workspace_stat.st_ino):
                    _fail('Runtime workspace was replaced.', 'state-tampered')
                self._initialize()
                os.fsync(workspace_fd)
            finally:
                fcntl.flock(workspace_fd, fcntl.LOCK_UN)
                os.close(workspace_fd)

    def _initialize(self):
        created = False
        try:
            self.root.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            _fail('Runtime state directory is unsafe.', 'unsafe-workspace')
        self._root_stat = info
        self._fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.key = (str(self.workspace), info.st_dev, info.st_ino)
        registration = {'schema': 1, 'kind': 'fabflow-agent-runtime', 'workspace': str(self.workspace),
                        'device': self._workspace_stat.st_dev, 'inode': self._workspace_stat.st_ino}
        try:
            if created:
                self._write('registry.json', registration, exclusive=True)
                self._touch_lock('admission.lock')
                self._write('rpc-index.json', {'entries': {}})
            else:
                if self._read('registry.json') != registration:
                    _fail('Runtime directory registration does not match.', 'state-tampered')
                self._read('rpc-index.json')
            self._registration = registration
        except FileNotFoundError:
            os.close(self._fd)
            self._fd = None
            _fail('Runtime state directory is not registered.', 'state-tampered')
        except BaseException:
            os.close(self._fd)
            self._fd = None
            raise

    def __del__(self):
        fd = getattr(self, '_fd', None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            self._fd = None

    def _check(self):
        try:
            current = self.root.lstat()
            parent = self.workspace.lstat()
            if (current.st_dev, current.st_ino) != (self._root_stat.st_dev, self._root_stat.st_ino) or not stat.S_ISDIR(current.st_mode):
                raise ValueError()
            if current.st_uid != os.getuid() or stat.S_IMODE(current.st_mode) & 0o077:
                raise ValueError()
            if (parent.st_dev, parent.st_ino) != (self._workspace_stat.st_dev, self._workspace_stat.st_ino) or not stat.S_ISDIR(parent.st_mode):
                raise ValueError()
            for ancestor in self.workspace.parents:
                if stat.S_ISLNK(ancestor.lstat().st_mode):
                    raise ValueError()
        except (OSError, ValueError):
            _fail('Runtime workspace was replaced.', 'state-tampered')

    def _read(self, name, maximum=MAX_STATE_BYTES):
        self._check()
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                        or info.st_size > maximum or stat.S_IMODE(info.st_mode) & 0o077):
                    raise ValueError()
                raw = b''
                while len(raw) <= maximum:
                    part = os.read(fd, min(1024 * 1024, maximum + 1 - len(raw)))
                    if not part:
                        break
                    raw += part
                if len(raw) > maximum:
                    raise ValueError()
            finally:
                os.close(fd)
            envelope = _json(raw)
            if set(envelope) != {'sha256', 'value'} or envelope['sha256'] != _digest(envelope['value']):
                raise ValueError()
            return envelope['value']
        except FileNotFoundError:
            raise
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            _fail('Runtime state failed integrity validation.', 'state-tampered')

    def _write(self, name, value, *, exclusive=False):
        self._check()
        raw = _encode({'sha256': _digest(value), 'value': value})
        if len(raw) > MAX_STATE_BYTES:
            _fail('Runtime state exceeds its limit.', 'state-too-large')
        temporary = '.write-' + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if exclusive:
                os.link(temporary, name, src_dir_fd=self._fd, dst_dir_fd=self._fd, follow_symlinks=False)
                os.unlink(temporary, dir_fd=self._fd)
            else:
                try:
                    old = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
                    if not stat.S_ISREG(old.st_mode) or old.st_nlink != 1:
                        _fail('Runtime output path was replaced.', 'state-tampered')
                except FileNotFoundError:
                    pass
                os.rename(temporary, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            os.fsync(self._fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self._fd)
            except FileNotFoundError:
                pass

    def _touch_lock(self, name):
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._fd)
        os.close(fd)

    def _lock_fd(self, name):
        self._check()
        try:
            fd = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._fd)
        except OSError:
            _fail('Runtime lock is missing or unsafe.', 'state-tampered')
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077:
            os.close(fd)
            _fail('Runtime lock is unsafe.', 'state-tampered')
        return fd

    @contextmanager
    def _lock(self, name):
        fd = self._lock_fd(name)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _state(self, sid):
        try:
            state = self._read(sid + '.json')
        except FileNotFoundError:
            _fail('Agent session was not found.', 'session-not-found')
        if (not isinstance(state, dict) or state.get('session_id') != sid or state.get('schema') != 1
                or state.get('phase') not in ACTIVE | TERMINAL | {'created'}
                or not isinstance(state.get('events'), list) or len(state['events']) > MAX_EVENTS):
            _fail('Agent session state is invalid.', 'state-tampered')
        if state.get('create_digest') != _digest(state.get('create_inputs')):
            _fail('Session creation evidence was changed.', 'state-tampered')
        if 'prompt_digest' in state and state['prompt_digest'] != _digest(state.get('prompt_inputs')):
            _fail('Session prompt evidence was changed.', 'state-tampered')
        if 'server_inputs' in state and state.get('evidence_sha256') != _digest(state['server_inputs']):
            _fail('Session source evidence was changed.', 'state-tampered')
        sequence = 0
        for event in state['events']:
            if (not isinstance(event, dict) or type(event.get('seq')) is not int or event['seq'] <= sequence
                    or type(event.get('time')) is not int or not isinstance(event.get('data'), dict)
                    or event['data'].get('turn') != 1
                    or event.get('type') not in {'turn/start', 'step/start', 'tool/call', 'tool/result', 'assistant/message', 'turn/end'}):
                _fail('Session progress evidence is invalid.', 'state-tampered')
            sequence = event['seq']
        if type(state.get('seq')) is not int or state['seq'] != sequence:
            _fail('Session progress sequence was changed.', 'state-tampered')
        return state

    def _save(self, state):
        self._write(state['session_id'] + '.json', state)

    @staticmethod
    def _event(state, kind, data):
        state['seq'] += 1
        state['events'].append({'seq': state['seq'], 'time': _now(), 'type': kind, 'data': {'turn': 1, **data}})
        if len(state['events']) > MAX_EVENTS:
            state['events'] = [state['events'][0], *state['events'][-(MAX_EVENTS - 1):]]

    def _terminal(self, state, phase, code=None):
        if state['phase'] not in ACTIVE and not (state['phase'] == 'created' and phase == 'stopped'):
            return False
        state.update(phase=phase, finished_at=_now())
        reason = {'kind': 'aborted' if phase == 'stopped' else 'error'}
        if code:
            reason['error'] = {'code': code, 'message': 'Agent execution did not complete; this request will not be submitted again.'}
        self._event(state, 'turn/end', {'reason': reason})
        self._save(state)
        return True

    def _reconcile(self, state):
        if state['phase'] not in ACTIVE:
            return state
        task = _LIVE.get((self.key, state['session_id']))
        if task is not None:
            return state
        lease = self._lock_fd(state['session_id'] + '.lease')
        try:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # A different process owns the lease. We may inspect, never run.
                return state
            old_owner = state.get('owner')
            same_process = isinstance(old_owner, dict) and old_owner == _owner()
            self._terminal(state, 'unknown' if same_process else 'interrupted', 'runtime-interrupted')
            return state
        finally:
            os.close(lease)

    def _rpc(self, rpc_id, method, sid, digest):
        index = self._read('rpc-index.json')
        if not isinstance(index, dict) or not isinstance(index.get('entries'), dict) or len(index['entries']) > MAX_SESSIONS * 4:
            _fail('RPC registry is invalid.', 'state-tampered')
        if rpc_id is None:
            return index, None
        if not isinstance(rpc_id, str) or not RPC.fullmatch(rpc_id):
            _fail('RPC identifier is invalid.')
        key = hashlib.sha256(rpc_id.encode()).hexdigest()
        expected = {'method': method, 'session_id': sid, 'digest': digest}
        previous = index['entries'].get(key)
        if previous is not None and previous != expected:
            _fail('RPC identifier already belongs to different inputs.', 'request-conflict')
        if previous is None and len(index['entries']) >= MAX_SESSIONS * 4:
            _fail('RPC registry capacity reached.', 'capacity-error')
        index['entries'][key] = expected
        return index, key

    def call(self, method, payload, rpc_id=None):
        if not isinstance(payload, dict):
            _fail('Agent request must be an object.')
        with _GUARD:
            _process_registry()
            self._check()
            if self._read('registry.json') != self._registration:
                _fail('Runtime registration was changed.', 'state-tampered')
            if method == 'session.list':
                if payload:
                    _fail('Session list accepts no filters.')
                items = []
                for name in sorted(os.listdir(self._fd)):
                    if name.startswith('session-') and name.endswith('.json') and not name.endswith('.result.json'):
                        sid = _sid(name[:-5])
                        with self._lock(sid + '.lock'):
                            state = self._reconcile(self._state(sid))
                        items.append({'sessionId': sid, 'running': state['phase'] in ACTIVE})
                return {'items': items}
            sid = _sid(payload.get('sessionId'))
            if method == 'session.create':
                return self._create(sid, payload, rpc_id)
            self._state(sid)
            if method == 'session.prompt':
                return self._prompt(sid, payload, rpc_id)
            if method not in {'session.history', 'session.cancel'}:
                _fail('Unsupported internal agent method.')
            allowed = {'sessionId', 'maxMessages'} if method == 'session.history' else {'sessionId'}
            if set(payload) - allowed:
                _fail('Unexpected agent request field.')
            with self._lock(sid + '.lock'):
                state = self._reconcile(self._state(sid))
                if method == 'session.cancel':
                    task = _LIVE.get((self.key, sid))
                    if task:
                        task.cancelled.set()
                    self._terminal(state, 'stopped', 'cancelled')
                    return {'accepted': True}
                limit = payload.get('maxMessages', 100)
                if type(limit) is not int or not 3 <= limit <= MAX_EVENTS:
                    _fail('History limit must be between 3 and 512.')
                events = copy.deepcopy(state['events'])
                if state['phase'] == 'completed':
                    result = self._read(sid + '.result.json')
                    if not isinstance(result, dict) or not isinstance(result.get('text'), str) or not isinstance(result.get('receipt'), dict):
                        _fail('Agent result record is invalid.', 'state-tampered')
                    if (result.get('sha256') != state.get('result_sha256')
                            or hashlib.sha256(result.get('text', '').encode()).hexdigest() != state.get('result_sha256')
                            or result.get('evidence_sha256') != state.get('evidence_sha256')
                            or result.get('receipt_sha256') != state.get('result_receipt_sha256')
                            or _digest(result.get('receipt')) != state.get('result_receipt_sha256')):
                        _fail('Agent result evidence was changed.', 'state-tampered')
                    for event in events:
                        if event['type'] == 'assistant/message':
                            event['data'] = {'turn': 1, 'message': {'content': [{'type': 'text', 'text': result['text']}]}}
                if len(events) > limit:
                    important = [event for event in events if event['type'] in {'turn/start', 'assistant/message', 'turn/end'}]
                    selected = {event['seq']: event for event in important}
                    for event in reversed(events):
                        if len(selected) >= limit:
                            break
                        selected[event['seq']] = event
                    events = sorted(selected.values(), key=lambda row: row['seq'])
                return {'events': events, 'hasMore': False}

    def _create(self, sid, payload, rpc_id):
        if (set(payload) - {'sessionId', 'cwd', 'agentPreset'} or payload.get('cwd') != str(self.workspace)
                or not isinstance(payload.get('agentPreset', 'standard'), str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', payload.get('agentPreset', 'standard'))):
            _fail('Session creation must use the exact isolated workspace.')
        if self._closed:
            _fail('Agent runtime is closed.', 'runtime-closed')
        digest = _digest(payload)
        with self._lock('admission.lock'):
            index, key = self._rpc(rpc_id, 'session.create', sid, digest)
            try:
                previous = self._state(sid)
            except AgentRuntimeError as error:
                if error.code != 'session-not-found':
                    raise
            else:
                if previous['create_digest'] != digest:
                    _fail('Session already belongs to different creation inputs.', 'request-conflict')
                if key:
                    self._write('rpc-index.json', index)
                return {'sessionId': sid}
            sessions = [name for name in os.listdir(self._fd) if SID.fullmatch(name[:-5]) and name.endswith('.json')]
            if len(sessions) >= MAX_SESSIONS:
                _fail('Workspace session capacity reached.', 'capacity-error')
            self._touch_lock(sid + '.lock')
            self._touch_lock(sid + '.lease')
            self._save({'schema': 1, 'session_id': sid, 'created_at': _now(), 'phase': 'created',
                        'create_digest': digest, 'create_inputs': copy.deepcopy(payload), 'seq': 0, 'events': []})
            if key:
                self._write('rpc-index.json', index)
            return {'sessionId': sid}

    def _prompt(self, sid, payload, rpc_id):
        if set(payload) - {'sessionId', 'mode', 'clientTimeZone', 'content'} or payload.get('mode') != 'queue':
            _fail('Only queued text prompts are accepted.')
        content = payload.get('content')
        timezone = payload.get('clientTimeZone', 'UTC')
        if (not isinstance(content, list) or not 1 <= len(content) <= 16
                or any(not isinstance(row, dict) or set(row) != {'type', 'text'} or row['type'] != 'text'
                       or not isinstance(row['text'], str) or not row['text'].strip() for row in content)
                or not isinstance(timezone, str) or not 1 <= len(timezone) <= 100):
            _fail('Prompt content must contain bounded text items only.')
        if len(_encode(payload)) > MAX_RESULT_BYTES:
            _fail('Prompt input exceeds the hard limit.', 'input-too-large')
        digest = _digest(payload)
        with self._lock('admission.lock'), self._lock(sid + '.lock'):
            index, key = self._rpc(rpc_id, 'session.prompt', sid, digest)
            state = self._reconcile(self._state(sid))
            if 'prompt_digest' in state:
                if state['prompt_digest'] != digest:
                    _fail('This session already admitted a different prompt.', 'request-conflict')
                if key:
                    self._write('rpc-index.json', index)
                return {'accepted': True, 'replayed': True}
            if self._closed:
                _fail('Agent runtime is closed.', 'runtime-closed')
            if state['phase'] != 'created':
                _fail('This session cannot accept another prompt.', 'request-conflict')
            try:
                if self.config is None:
                    from agent_config import AgentConfig
                    self.config = AgentConfig.from_env()
                reader, inputs = _prepare(self.workspace, self.config)
            except AgentRuntimeError:
                raise
            except Exception as error:
                _fail('Agent configuration or authorized attachments are not ready.', getattr(error, 'code', 'configuration-error'))
            prompt = '\n'.join(row['text'] for row in content)
            if len(prompt.encode()) + len(inputs['instructions'].encode()) > self.config.max_input_bytes:
                _fail('Prompt input exceeds the configured limit.', 'input-too-large')
            if not _SLOTS.acquire(blocking=False):
                _fail('Agent worker capacity reached; no prompt was admitted.', 'capacity-error')
            lease = self._lock_fd(sid + '.lease')
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                state.update(phase='queued', prompt_digest=digest, prompt_inputs=copy.deepcopy(payload),
                             server_inputs=inputs, evidence_sha256=_digest(inputs), owner=_owner(), admitted_at=_now())
                self._event(state, 'turn/start', {})
                self._save(state)  # Durable admission precedes thread start and any provider request.
                if key:
                    self._write('rpc-index.json', index)
                task = _Task(self, sid, lease, reader, self.config, self.executor)
                _LIVE[(self.key, sid)] = task
                self._owned.add(sid)
                task.thread = threading.Thread(target=self._execute, args=(task, prompt, inputs),
                                               name='fabflow-agent-' + sid[8:16], daemon=True)
                try:
                    task.thread.start()
                except BaseException:
                    _LIVE.pop((self.key, sid), None)
                    self._terminal(state, 'interrupted', 'worker-start-failed')
                    raise
                return {'accepted': True, 'replayed': False}
            except BaseException:
                os.close(lease)
                _SLOTS.release()
                raise

    def _emit(self, task, event):
        if task.cancelled.is_set() or not isinstance(event, dict) or not isinstance(event.get('data', {}), dict):
            return
        kind, data = event.get('type'), event.get('data', {})
        safe = None
        if kind == 'step/start' and type(data.get('step', 1)) is int and 0 < data.get('step', 1) <= 10000:
            safe = {'step': data.get('step', 1)}
        elif kind == 'tool/call' and data.get('name') in {'read', 'read_image'} and isinstance(data.get('callId'), str) and RPC.fullmatch(data['callId']):
            safe = {'name': data['name'], 'callId': data['callId']}
        elif kind == 'tool/result':
            source = (data.get('message') or {}).get('source') if isinstance(data.get('message'), dict) else None
            call_id = source.get('callId') if isinstance(source, dict) else None
            if isinstance(call_id, str) and RPC.fullmatch(call_id):
                safe = {'message': {'source': {'callId': call_id}}}
        if safe is not None:
            with self._lock(task.sid + '.lock'):
                state = self._state(task.sid)
                if state['phase'] in ACTIVE:
                    self._event(state, kind, safe)
                    self._save(state)

    def _execute(self, task, prompt, inputs):
        try:
            with self._lock(task.sid + '.lock'):
                state = self._state(task.sid)
                if state['phase'] not in ACTIVE or task.cancelled.is_set():
                    return
                state['phase'] = 'running'
                self._save(state)
            task.reader.verify_sources()
            executor = task.executor
            if executor is None:
                from agents_executor import AgentsExecutor
                executor = AgentsExecutor(task.config)
            async def execute():
                return await asyncio.wait_for(executor.run(prompt, inputs['instructions'], task.reader.make_sdk_tools(),
                    emit=lambda event: self._emit(task, event), cancelled=task.cancelled),
                    timeout=task.config.max_runtime_seconds + 5)
            result = asyncio.run(execute())
            task.reader.verify_sources()
            # Rebuild the frozen server manifest after the run, without calling AI.
            _reader, current = _prepare(self.workspace, task.config)
            if _digest(current) != _digest(inputs):
                _fail('Authorized inputs changed during execution.', 'source-changed')
            text = result.get('text') if isinstance(result, dict) and result.get('status') == 'completed' else None
            if not isinstance(text, str) or not text.strip():
                _fail('Agent returned no complete result.', 'invalid-result')
            if len(text.encode()) > min(task.config.max_output_bytes, MAX_RESULT_BYTES):
                _fail('Agent result exceeds the output limit.', 'output-too-large')
            candidate = text.strip()
            if candidate.startswith('```') and candidate.endswith('```'):
                candidate = candidate.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
            if not isinstance(_json(candidate), dict):
                _fail('Agent result must be one complete JSON object.', 'invalid-result')
            result_hash = hashlib.sha256(text.encode()).hexdigest()
            with self._lock(task.sid + '.lock'):
                state = self._state(task.sid)
                if state['phase'] not in ACTIVE or task.cancelled.is_set():
                    return
                receipt = _receipt(result, state, result_hash)
                receipt_hash = _digest(receipt)
                self._write(task.sid + '.result.json', {'text': text, 'sha256': result_hash,
                            'evidence_sha256': state['evidence_sha256'], 'receipt': receipt,
                            'receipt_sha256': receipt_hash}, exclusive=True)
                state.update(phase='completed', result_sha256=result_hash, result_receipt_sha256=receipt_hash, finished_at=_now())
                self._event(state, 'assistant/message', {'result_sha256': result_hash})
                self._event(state, 'turn/end', {'reason': {'kind': 'completed'}})
                self._save(state)
        except BaseException as error:
            code = getattr(error, 'code', 'execution-failed')
            if not isinstance(code, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', code):
                code = 'execution-failed'
            try:
                with self._lock(task.sid + '.lock'):
                    state = self._state(task.sid)
                    self._terminal(state, 'stopped' if task.cancelled.is_set() else 'failed', code)
            except (AgentRuntimeError, OSError):
                pass  # Corrupt state is never replaced by a fabricated success.
        finally:
            os.close(task.lease)
            with _GUARD:
                _LIVE.pop((self.key, task.sid), None)
                _SLOTS.release()
            task.done.set()

    def close(self, wait=False):
        with _GUARD:
            _process_registry()
            self._closed = True
            tasks = [_LIVE[(self.key, sid)] for sid in self._owned if (self.key, sid) in _LIVE]
            for task in tasks:
                task.cancelled.set()
                with self._lock(task.sid + '.lock'):
                    self._terminal(self._state(task.sid), 'stopped', 'runtime-closed')
        if wait:
            for task in tasks:
                task.done.wait(timeout=task.config.max_runtime_seconds + 6)

    shutdown = close


def shutdown_all():
    """Stop only this process's admitted tasks without waiting on providers."""
    with _GUARD:
        _process_registry()
        tasks = list(_LIVE.values())
        for task in tasks:
            task.cancelled.set()
            owner = task.runtime
            owner._closed = True
            try:
                with owner._lock(task.sid + '.lock'):
                    owner._terminal(owner._state(task.sid), 'stopped', 'runtime-closed')
            except (AgentRuntimeError, OSError):
                # Corruption is preserved for inspection; a shutdown must not
                # fabricate another store or overwrite an unverified record.
                pass
