"""Durable asynchronous quote amendment previews and explicit immutable commits.

The model only interprets a message. The scalar core calculates the preview and
is run again under a source fence before a user-approved new run is committed.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
import re
import threading
import uuid

from prep_record import atomic_write, digest, only_keys, stamp
from workbench_store import WorkbenchError

PROPOSAL_ID = re.compile(r'AP-[a-f0-9]{32}\Z')
JOURNAL_NAME = re.compile(r'[a-f0-9]{64}\.json\Z')
TERMINAL = {'ready', 'needs_clarification', 'failed'}
MAX_RECORD_BYTES = 128 * 1024 * 1024


def require(condition, message, code='invalid_request', status=400):
    if not condition:
        raise WorkbenchError(message, code, status)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'


class QuoteAssistantService:
    def __init__(self, owner):
        self.owner = owner
        self._stop = threading.Event()
        self._thread = None
        self._startup_lock = threading.Lock()

    def _directory(self, batch_id, kind, *, create=False):
        require(kind in {'proposals', 'commits', 'workspaces'}, '助手记录类型无效。')
        root = self.owner._folder(batch_id) / 'assistant'
        directory = root / kind
        require(not root.is_symlink() and not directory.is_symlink()
                and (not directory.exists() or directory.is_dir()), '报价助手存储目录无效。')
        if create:
            root.mkdir(mode=0o700, exist_ok=True)
            directory.mkdir(mode=0o700, exist_ok=True)
        return directory

    @staticmethod
    def _read(path):
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_RECORD_BYTES,
                '报价助手记录无效。', 'not_found', 404)
        value = json.loads(path.read_text(encoding='utf-8'))
        require(isinstance(value, dict), '报价助手记录格式无效。')
        return value

    def _save(self, path, value):
        require(not path.is_symlink(), '报价助手记录位置无效。')
        content = encoded(value)
        require(len(content.encode('utf-8')) <= MAX_RECORD_BYTES, '本次修改记录过大，请分批调整。')
        atomic_write(path, content)

    def _records(self, batch_id, kind):
        directory = self._directory(batch_id, kind)
        if not directory.exists():
            return []
        paths = sorted(directory.iterdir())
        require(len(paths) <= 100, '本任务助手历史过多，请建立新的报价任务。')
        rows = []
        for path in paths:
            valid = PROPOSAL_ID.fullmatch(path.stem) if kind == 'proposals' else JOURNAL_NAME.fullmatch(path.name)
            require(bool(valid) and path.suffix == '.json', '报价助手记录名称无效。')
            record = self._read(path)
            require(record.get('batch_id') == batch_id, '报价助手记录不属于当前任务。')
            if kind == 'proposals':
                require(record.get('id') == path.stem and record.get('status') in {'queued', 'running', *TERMINAL}
                        and isinstance(record.get('source_run_id'), str)
                        and re.fullmatch(r'RUN-[a-f0-9]{32}', record['source_run_id']),
                        '报价预览记录无效。')
            else:
                event = record.get('event_id')
                require(isinstance(event, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', event)
                        and path.name == hashlib.sha256(event.encode()).hexdigest() + '.json'
                        and record.get('status') in {'prepared', 'committed'}
                        and all(isinstance(record.get(key), str) and re.fullmatch(r'RUN-[a-f0-9]{32}', record[key])
                                for key in ('run_id', 'source_run_id'))
                        and isinstance(record.get('proposal_id'), str) and PROPOSAL_ID.fullmatch(record['proposal_id']),
                        '报价修改提交记录无效。')
            rows.append((path, record))
        return rows

    @staticmethod
    def public(record):
        summary = record.get('summary') if isinstance(record.get('summary'), dict) else {}
        changes = copy.deepcopy(summary.get('changes', []))
        for change in changes:
            for field in ('before', 'after'):
                value = change.get(field)
                if isinstance(value, dict):
                    change[field] = '；'.join(str(key) + '：' + str(item) for key, item in value.items())
                elif isinstance(value, list):
                    change[field] = '、'.join(str(item) for item in value)
        parser = record.get('parser')
        parser = ({key: copy.deepcopy(value) for key, value in parser.items()
                   if key in {'kind', 'model', 'model_called', 'provider', 'status'}
                   and (type(value) in {bool, type(None)} or isinstance(value, str) and len(value) <= 200)}
                  if isinstance(parser, dict) else None)
        return {'proposal': {key: copy.deepcopy(value) for key, value in {
            'id': record['id'], 'status': record['status'], 'message': record.get('message', ''),
            'request_message': record.get('request_message'), 'source_run_id': record['source_run_id'],
            'expected_revision': record['source_binding']['batch_revision'],
            'changes': changes, 'before_totals': summary.get('before_totals'),
            'after_totals': summary.get('after_totals'), 'before_order_sets': summary.get('before_order_sets'),
            'after_order_sets': summary.get('after_order_sets'), 'affected_line_ids': summary.get('affected_line_ids', []),
            'warnings': summary.get('warnings', []), 'created_at': record.get('created_at'),
            'updated_at': record.get('updated_at'), 'error_code': record.get('error_code'),
            'committed_run_id': record.get('committed_run_id'), 'parser': parser}.items()}}

    def _proposal(self, batch_id, run_id, proposal_id):
        require(isinstance(proposal_id, str) and PROPOSAL_ID.fullmatch(proposal_id), '报价预览编号无效。')
        path = self._directory(batch_id, 'proposals') / (proposal_id + '.json')
        record = self._read(path)
        require(record.get('id') == proposal_id and record.get('batch_id') == batch_id
                and record.get('source_run_id') == run_id, '报价预览不属于当前运行。', 'not_found', 404)
        return path, record

    def _context(self, state, run_id):
        result, review, recognition, binding, _ = self.owner._engineering_pricing_context(state, run_id)
        require(not result.get('cost_sheet'), '该版本已采用内部核价单，请在内部核价单调整工程量与费率并另存新报价。', 'cost_sheet_required', 409)
        require(state['status'] not in {'queued', 'running'}, '当前报价仍在后台处理，请完成后再修改。', 'already_running', 409)
        require((state.get('result') or {}).get('run_id') == run_id
                and digest(state['result']) == digest(result), '已有更新的报价，请从当前版本重新预览。', 'source_conflict', 409)
        require(recognition['status'] not in {'queued', 'running'}, '工程识别仍在执行，请完成后再预览修改。', 'already_running', 409)
        return result, review, recognition, binding

    def enqueue(self, batch_id, run_id, payload):
        only_keys(payload, {'event_id', 'message'}, 'assistant preview')
        event = self.owner._event(payload.get('event_id'))
        message = payload.get('message')
        require(isinstance(message, str) and message.strip() and len(message) <= 4000
                and '\x00' not in message, '请输入不超过4000字的报价修改说明。')
        signature = digest([run_id, message])
        proposal_id = 'AP-' + hashlib.sha256((run_id + ':' + event).encode()).hexdigest()[:32]
        with self.owner._lock(batch_id):
            state = self.owner._read(batch_id)
            existing = self._directory(batch_id, 'proposals') / (proposal_id + '.json')
            if existing.exists():
                _, record = self._proposal(batch_id, run_id, proposal_id)
                require(record.get('signature') == signature, '相同预览请求的内容已变化。', 'event_conflict', 409)
                response = self.public(record)
            else:
                self.owner._no_pending_pricing(batch_id)
                require(len(self._records(batch_id, 'proposals')) < 100, '本任务助手历史过多，请建立新的报价任务。')
                result, _, _, binding = self._context(state, run_id)
                require(len(result['lines']) <= 1000, '当前助手支持最多1000条来源物料，请分批修改。')
                now = stamp()
                record = {'id': proposal_id, 'batch_id': batch_id, 'source_run_id': run_id,
                          'event_id': event, 'signature': signature, 'request_message': message,
                          'status': 'queued', 'message': '正在理解修改要求，原报价保持不变。',
                          'source_binding': binding, 'created_at': now, 'updated_at': now,
                          'summary': None, 'patch': None, 'parser': None}
                path = self._directory(batch_id, 'proposals', create=True) / (proposal_id + '.json')
                self._save(path, record)
                response = self.public(record)
        self.start_worker()
        return response

    def get(self, batch_id, run_id, proposal_id):
        with self.owner._lock(batch_id):
            self.owner._read(batch_id)
            _, record = self._proposal(batch_id, run_id, proposal_id)
            return self.public(record)

    def no_prepared(self, batch_id):
        require(not any(record['status'] == 'prepared' for _, record in self._records(batch_id, 'commits')),
                '上一笔报价修改正在恢复，请稍后刷新。', 'pricing_pending', 409)

    def _commit(self, state, path, record):
        run_id = record['run_id']
        if record['status'] == 'prepared':
            result, review, recognition = (record.get(key) for key in ('result', 'review', 'recognition'))
            require(isinstance(result, dict) and isinstance(review, dict) and isinstance(recognition, dict)
                    and result.get('batch_id') == state['id'] and result.get('run_id') == run_id
                    and result.get('source_sha256') == state['source_sha256']
                    and review.get('run_id') == recognition.get('run_id') == run_id
                    and review.get('source_digest') == recognition.get('source_digest') == digest(result),
                    '待恢复报价修改的来源不一致。', 'source_conflict', 409)
            existing = next((row for row in state['runs'] if row['id'] == run_id), None)
            if existing is None:
                _, _, _, binding = self._context(state, record['source_run_id'])
                require(state['revision'] == record['base_revision'] and binding == record['source_binding'],
                        '报价或工程资料已更新，请重新预览。', 'source_conflict', 409)
            directory = self.owner._folder(state['id']) / 'runs'
            self.owner._write_pricing_snapshot(directory / (run_id + '.json'), result)
            self.owner._write_pricing_snapshot(directory / (run_id + '-engineering.json'), review)
            self.owner._write_pricing_snapshot(directory / (run_id + '-engineering-recognition.json'), recognition)
            if existing is None:
                state['settings'] = copy.deepcopy(result['settings'])
                self.owner._complete(state, result, result['source_files'])
            record = {key: value for key, value in record.items() if key not in {'result', 'review', 'recognition'}}
            record.update(status='committed', committed_at=stamp())
            self._save(path, record)
        require(any(row['id'] == run_id for row in state['runs']), '报价修改与历史运行不一致。', 'source_conflict', 409)
        require(digest(self.owner._preview_run_locked(state, run_id)) == record['result_digest'],
                '新报价快照与提交记录不一致。', 'source_conflict', 409)
        proposal_path, proposal = self._proposal(state['id'], record['source_run_id'], record['proposal_id'])
        if proposal.get('committed_run_id') != run_id:
            require(not proposal.get('committed_run_id'), '预览已被另一笔修改采用。', 'source_conflict', 409)
            proposal.update(committed_run_id=run_id, updated_at=stamp())
            self._save(proposal_path, proposal)
        return record

    def commit(self, batch_id, run_id, payload):
        from batch_quote_amendment import amend_quote
        only_keys(payload, {'event_id', 'proposal_id', 'expected_revision'}, 'assistant commit')
        event = self.owner._event(payload.get('event_id'))
        require(type(payload.get('expected_revision')) is int and payload['expected_revision'] >= 0,
                '请重新预览当前报价。')
        signature = digest([run_id, payload])
        with self.owner._lock(batch_id):
            state = self.owner._read(batch_id)
            records = self._records(batch_id, 'commits')
            previous = next(((path, record) for path, record in records if record['event_id'] == event), None)
            if previous:
                path, record = previous
                require(record.get('signature') == signature, '相同采用请求的内容已变化。', 'event_conflict', 409)
                saved = self._commit(state, path, record)
                return {'batch': self.owner._public(state), 'run_id': saved['run_id'], 'source_run_id': run_id, 'replayed': True}
            _, proposal = self._proposal(batch_id, run_id, payload.get('proposal_id'))
            prior = next(((path, record) for path, record in records if record['proposal_id'] == proposal['id']), None)
            if prior:
                require(payload['expected_revision'] == proposal['source_binding']['batch_revision'],
                        '预览修订号已变化。', 'revision_conflict', 409)
                saved = self._commit(state, *prior)
                require(len(records) < 100, '本任务助手历史过多，请复用原采用请求。')
                alias = {**copy.deepcopy(saved), 'event_id': event, 'signature': signature,
                         'replay_of': saved['event_id']}
                alias_path = self._directory(batch_id, 'commits') / (hashlib.sha256(event.encode()).hexdigest() + '.json')
                self._save(alias_path, alias)
                return {'batch': self.owner._public(state), 'run_id': saved['run_id'], 'source_run_id': run_id, 'replayed': True}
            self.owner._no_pending_pricing(batch_id)
            require(len(records) < 100 and len(state['runs']) < 100, '本任务报价历史过多，请建立新的报价任务。')
            require(proposal['status'] == 'ready', '请先完成修改预览，再采用新报价。', 'proposal_not_ready', 409)
            require(payload['expected_revision'] == state['revision'] == proposal['source_binding']['batch_revision'],
                    '报价已更新，请保留原话并重新预览。', 'revision_conflict', 409)
            result, review, recognition, binding = self._context(state, run_id)
            require(binding == proposal['source_binding'], '工程资料已更新，请重新预览修改。', 'source_conflict', 409)
            recalculated = amend_quote(result, proposal['patch'])
            require(digest(recalculated['result']) == proposal.get('result_digest')
                    and digest(recalculated['summary']) == digest(proposal['summary']),
                    '报价预览计算依据已变化，请重新预览。', 'source_conflict', 409)
            new_result = copy.deepcopy(recalculated['result'])
            new_id, now = 'RUN-' + uuid.uuid4().hex, stamp()
            prefix = self.owner.API_PREFIX + '/' + batch_id + '/runs/' + new_id + '/'
            provenance = {'source_run_id': run_id, 'source_quote_digest': binding['source_digest'],
                          'source_review_revision': review['revision'], 'source_recognition_id': recognition.get('id'),
                          'source_recognition_digest': binding['recognition_digest'], 'proposal_id': proposal['id'],
                          'commit_event_id': event}
            new_result.update(batch_id=batch_id, run_id=new_id, created_at=now, status='pending_review', simulated=True,
                              engineering_approved=False, business_approved=False,
                              links={'quote_url': prefix + 'quote.html', 'bom_url': prefix + 'bom.csv',
                                     'cost_url': prefix + 'cost.csv', 'json_url': prefix + 'quote.json'})
            new_result.setdefault('quote_amendment', {}).update(provenance, request_message=proposal['request_message'],
                parser=copy.deepcopy(proposal.get('parser')), summary=copy.deepcopy(proposal['summary']))
            identity = digest(new_result)
            new_review = {'schema_version': 1, 'run_id': new_id, 'source_digest': identity, 'revision': 0,
                          'header': copy.deepcopy(review['header']), 'lines': copy.deepcopy(review['lines']),
                          'saved_at': now, 'status': 'pending_review', 'quotation_unchanged': True,
                          '_events': {}, 'history': [], 'parent': provenance}
            new_recognition = {'id': 'ENG-' + uuid.uuid4().hex, 'run_id': new_id, 'source_digest': identity,
                               'status': 'completed', 'stage': 'completed', 'started_at': None, 'updated_at': now,
                               'message': '沿用已读取资料；报价修改另存新版本，仍待审核。',
                               'native': copy.deepcopy(recognition.get('native') or result.get('engineering_native')),
                               'ai': copy.deepcopy(recognition.get('ai')), 'error': recognition.get('error'),
                               'partial': bool(recognition.get('partial')), '_events': [], 'parent': provenance}
            require(len(encoded(new_result).encode('utf-8')) <= 64 * 1024 * 1024
                    and len(encoded(new_review).encode('utf-8')) <= 4 * 1024 * 1024
                    and len(encoded(new_recognition).encode('utf-8')) <= 32 * 1024 * 1024,
                    '新报价或工程记录过大，请分批调整。')
            record = {'schema_version': 1, 'batch_id': batch_id, 'event_id': event, 'signature': signature,
                      'proposal_id': proposal['id'], 'source_run_id': run_id, 'run_id': new_id,
                      'status': 'prepared', 'base_revision': state['revision'], 'source_binding': binding,
                      'result_digest': identity, 'created_at': now, 'result': new_result,
                      'review': new_review, 'recognition': new_recognition}
            directory = self._directory(batch_id, 'commits', create=True)
            path = directory / (hashlib.sha256(event.encode()).hexdigest() + '.json')
            require(not path.exists(), '采用请求已存在。', 'event_conflict', 409)
            self._save(path, record)
            self._commit(state, path, record)
            return {'batch': self.owner._public(state), 'run_id': new_id, 'source_run_id': run_id, 'replayed': False}

    def recover_locked(self, state, *, running=False):
        for path, record in self._records(state['id'], 'commits'):
            if record['status'] == 'prepared':
                self._commit(state, path, record)
        if running:
            for path, record in self._records(state['id'], 'proposals'):
                if record['status'] == 'running':
                    record.update(status='queued', message='连接已恢复，继续本次修改预览。', updated_at=stamp())
                    self._save(path, record)

    def _process(self):
        from batch_quote_amendment import amend_quote
        for folder in sorted(self.owner.root.iterdir()):
            if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r'BATCH-[a-f0-9]{32}', folder.name):
                continue
            batch_id = folder.name
            chosen = None
            with self.owner._lock(batch_id):
                state = self.owner._read(batch_id)
                self.recover_locked(state)
                chosen = next(((path, record) for path, record in self._records(batch_id, 'proposals') if record['status'] == 'queued'), None)
                if chosen:
                    path, record = chosen
                    record.update(status='running', message='正在理解修改要求并计算预览，尚未保存新报价。', updated_at=stamp())
                    self._save(path, record)
            if not chosen:
                continue
            path, record = chosen
            try:
                with self.owner._lock(batch_id):
                    state = self.owner._read(batch_id)
                    base, _, _, binding = self._context(state, record['source_run_id'])
                    require(binding == record['source_binding'], '报价或工程资料已更新，请重新预览。', 'source_conflict', 409)
                try:
                    parser = getattr(self.owner, 'assistant_parser', None)
                    if parser is not None:
                        parsed = parser(copy.deepcopy(base), record['request_message'])
                    else:
                        from batch_quote_assistant import parse_message
                        workspace = self._directory(batch_id, 'workspaces', create=True) / record['id']
                        require(not workspace.is_symlink(), '助手解析目录无效。')
                        workspace.mkdir(mode=0o700, exist_ok=True)
                        parsed = parse_message(copy.deepcopy(base), record['request_message'], workspace=workspace,
                                               rpc_url=self.owner.engineering_recognition.rpc_url)
                except Exception as error:
                    raise WorkbenchError('本轮修改解析未完成，原报价保持不变。请稍后重新发送修改说明。',
                                         'assistant_unavailable', 503) from error
                require(isinstance(parsed, dict) and parsed.get('status') in {'ready', 'needs_clarification'}
                        and isinstance(parsed.get('message'), str) and len(parsed['message']) <= 6000,
                        '助手返回格式无效，请重新描述修改。')
                record['parser'] = copy.deepcopy(parsed.get('parser'))
                if parsed['status'] == 'ready':
                    calculated = amend_quote(base, parsed.get('patch'))
                    record.update(patch=copy.deepcopy(parsed['patch']), summary=copy.deepcopy(calculated['summary']),
                                  result_digest=digest(calculated['result']))
                record.update(status=parsed['status'], message=parsed['message'], updated_at=stamp())
                with self.owner._lock(batch_id):
                    _, _, _, current = self._context(self.owner._read(batch_id), record['source_run_id'])
                    require(current == record['source_binding'], '报价或工程资料已更新，请重新预览。', 'source_conflict', 409)
                    self._save(path, record)
            except WorkbenchError as error:
                record.update(status='failed', message=str(error)[:1200], error_code=error.code, updated_at=stamp())
                with self.owner._lock(batch_id):
                    self._save(path, record)
            except ValueError as error:
                record.update(status='needs_clarification', message=str(error)[:1200], error_code='amendment_invalid', updated_at=stamp())
                with self.owner._lock(batch_id):
                    self._save(path, record)
            except Exception:
                record.update(status='failed', message='本轮修改解析未完成，原报价保持不变。请稍后重新发送修改说明。',
                              error_code='assistant_failed', updated_at=stamp())
                with self.owner._lock(batch_id):
                    self._save(path, record)
            return True
        return False

    @contextmanager
    def _lease(self):
        path = self.owner.root / '.assistant-worker.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'r+') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True

    def process_pending_once(self):
        with self._lease() as acquired:
            if not acquired:
                return False
            self._recover_running()
            return self._process()

    def _recover_running(self):
        for folder in sorted(self.owner.root.iterdir()):
            if folder.is_dir() and not folder.is_symlink() and re.fullmatch(r'BATCH-[a-f0-9]{32}', folder.name):
                with self.owner._lock(folder.name):
                    self.recover_locked(self.owner._read(folder.name), running=True)

    def _loop(self):
        while not self._stop.is_set():
            try:
                with self._lease() as acquired:
                    if acquired:
                        self._recover_running()
                        while not self._stop.is_set():
                            if not self._process():
                                self._stop.wait(.5)
                    else:
                        self._stop.wait(.5)
            except Exception:
                self._stop.wait(.5)

    def start_worker(self):
        with self._startup_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name='quote-assistant-worker')
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
