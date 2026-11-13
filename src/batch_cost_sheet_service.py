"""Source-bound internal cost sheets and durable immutable repricing runs.

The scalar kernel performs all financial calculations. This facade only binds
the request to saved quote/engineering snapshots and commits a new version.
Its worker can finish a prepared journal; it never starts CAD or a model.
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

JOURNAL = re.compile(r'[a-f0-9]{64}\.json\Z')
BATCH = re.compile(r'BATCH-[a-f0-9]{32}\Z')
RUN = re.compile(r'RUN-[a-f0-9]{32}\Z')
MAX_BYTES = 128 * 1024 * 1024
BUSY = {'queued', 'running'}
IDENTITY = ('id', 'file_id', 'native_definition', 'native_solid_count', 'quantity_per_set', 'quantity_total', 'volume_mm3')


def require(value, message, code='invalid_request', status=400):
    if not value:
        raise WorkbenchError(message, code, status)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n'


class BatchCostSheetService:
    def __init__(self, owner):
        self.owner = owner
        self._thread = None
        self._stop = threading.Event()
        self._startup_lock = threading.Lock()

    def _directory(self, batch_id, *, create=False):
        folder = self.owner._folder(batch_id) / 'cost-sheet-pricing'
        require(not folder.is_symlink() and (not folder.exists() or folder.is_dir()), '内部核价存储位置无效。')
        if create:
            folder.mkdir(mode=0o700, exist_ok=True)
        return folder

    def _records(self, batch_id):
        folder = self._directory(batch_id)
        if not folder.exists():
            return []
        paths = sorted(folder.iterdir())
        require(len(paths) <= 100, '内部核价历史过多，请建立新的报价任务。')
        records = []
        for path in paths:
            require(JOURNAL.fullmatch(path.name) and not path.is_symlink() and path.is_file()
                    and path.stat().st_size <= MAX_BYTES, '内部核价提交记录无效。')
            record = json.loads(path.read_text(encoding='utf-8'))
            require(isinstance(record, dict) and record.get('schema_version') == 1
                    and record.get('kind') == 'cost_sheet' and record.get('batch_id') == batch_id
                    and record.get('status') in {'prepared', 'committed'}, '内部核价记录来源无效。')
            event = self.owner._event(record.get('event_id'))
            require(path.name == hashlib.sha256(event.encode()).hexdigest() + '.json'
                    and all(isinstance(record.get(key), str) and RUN.fullmatch(record[key])
                            for key in ('run_id', 'source_run_id')), '内部核价记录编号无效。')
            records.append((path, record))
        return records

    def no_prepared(self, batch_id):
        require(not any(record['status'] == 'prepared' for _, record in self._records(batch_id)),
                '上一笔内部核价正在恢复，请稍后刷新，不要重复新建报价。', 'cost_sheet_pending', 409)

    def _pending(self, batch_id):
        return (any(record['status'] == 'prepared' for _, record in self._records(batch_id))
                or any(record['status'] == 'prepared' for _, record in self.owner._pricing_records(batch_id))
                or any(record['status'] == 'prepared' for _, record in self.owner.assistant._records(batch_id, 'commits')))

    def get(self, batch_id, run_id):
        from batch_cost_sheet import build_cost_sheet
        with self.owner._lock(batch_id):
            state = self.owner._read(batch_id)
            result, _, recognition, binding, _ = self.owner._engineering_pricing_context(state, run_id)
            sheet = build_cost_sheet(result)
            require(isinstance(sheet, dict) and sheet.get('base_digest') == binding['source_digest']
                    and isinstance(sheet.get('entries'), list), '内部核价单来源不一致。')
            sheet = copy.deepcopy(sheet)
            policy = copy.deepcopy(sheet.get('policy') or {'base_digest': sheet['base_digest'],
                'entries': sheet['entries'], 'customer_terms': sheet['customer_terms']})
            require(policy.get('base_digest') == sheet['base_digest'] and policy.get('entries') == sheet['entries'],
                    '内部核价编辑策略与已保存单据不一致。')
            policy['source_binding'] = binding
            sheet['policy'] = policy
            current = (state.get('result') or {}).get('run_id')
            can_reprice = (state['status'] not in BUSY and recognition['status'] not in BUSY
                           and current == run_id and digest(state['result']) == digest(result)
                           and not self._pending(batch_id))
            return {'sheet': sheet, 'expected_revision': state['revision'],
                    'source_digest': binding['source_digest'], 'source_binding': binding,
                    'current_run_id': current, 'can_reprice': can_reprice}

    def _commit(self, state, path, record):
        # Shared journal commit validates sources, writes each snapshot once,
        # updates the state pointer and recovers a lost response to this RUN.
        return self.owner._commit_engineering_pricing(state, path, record)

    def reprice(self, batch_id, run_id, payload):
        from batch_cost_sheet import reprice_cost_sheet
        only_keys(payload, {'event_id', 'expected_revision', 'policy'}, 'internal cost sheet')
        event = self.owner._event(payload.get('event_id'))
        require(type(payload.get('expected_revision')) is int and payload['expected_revision'] >= 0,
                '请重新读取内部核价单版本。')
        require(isinstance(payload.get('policy'), dict), '请提交本次内部核价单。')
        require(isinstance(run_id, str) and RUN.fullmatch(run_id), '报价运行编号无效。')
        signature = digest([run_id, payload])
        with self.owner._lock(batch_id):
            state = self.owner._read(batch_id)
            records = self._records(batch_id)
            previous = next(((path, record) for path, record in records if record['event_id'] == event), None)
            if previous:
                path, record = previous
                require(record.get('signature') == signature, '相同内部核价请求的内容已变化。', 'event_conflict', 409)
                saved = self._commit(state, path, record)
                return {'batch': self.owner._public(state), 'run_id': saved['run_id'],
                        'source_run_id': run_id, 'replayed': True}
            self.owner._no_pending_pricing(batch_id)
            require(len(records) < 100 and len(state['runs']) < 100, '内部核价历史过多，请建立新的报价任务。')
            require(state['status'] not in BUSY, '当前报价仍在后台处理，请完成后再核价。', 'already_running', 409)
            require(payload['expected_revision'] == state['revision'], '报价已更新，请重新读取核价单。', 'revision_conflict', 409)
            require((state.get('result') or {}).get('run_id') == run_id, '已有更新的报价，请打开当前版本的核价单。', 'source_conflict', 409)
            result, review, recognition, binding, _ = self.owner._engineering_pricing_context(state, run_id)
            require(digest(state['result']) == digest(result), '当前报价与保存快照不一致。', 'source_conflict', 409)
            require(recognition['status'] not in BUSY, '工程识别尚在执行，请完成后再核价。', 'already_running', 409)
            policy = copy.deepcopy(payload['policy'])
            require(policy.pop('source_binding', None) == binding, '报价或工程资料已更新，请重新读取核价单。', 'source_conflict', 409)
            new_result = reprice_cost_sheet(result, policy)
            require(isinstance(new_result, dict) and isinstance(new_result.get('cost_sheet'), dict), '内部核价结果无效。')
            expected_settings = copy.deepcopy(result['settings'])
            if 'pricing' in policy:
                expected_settings.update({key: policy['pricing'][key] for key in ('pricing_method', 'rate_percent')})
            require(new_result.get('settings') == expected_settings and new_result.get('source_files') == result['source_files']
                    and new_result.get('source_sha256') == result['source_sha256']
                    and [{key: row.get(key) for key in IDENTITY} for row in new_result.get('lines', [])]
                    == [{key: row.get(key) for key in IDENTITY} for row in result['lines']],
                    '内部核价不能修改套数、原生几何或来源用量。')
            new_id, now = 'RUN-' + uuid.uuid4().hex, stamp()
            prefix = self.owner.API_PREFIX + '/' + batch_id + '/runs/' + new_id + '/'
            new_result.update(batch_id=batch_id, run_id=new_id, created_at=now, status='pending_review', simulated=True,
                engineering_approved=False, business_approved=False,
                links={'quote_url': prefix + 'customer-quote.html', 'bom_url': prefix + 'bom.csv',
                       'cost_url': prefix + 'cost.csv', 'json_url': prefix + 'quote.json'})
            provenance = {'source_run_id': run_id, 'source_quote_digest': binding['source_digest'],
                          'source_review_revision': review['revision'], 'source_recognition_id': recognition.get('id'),
                          'source_recognition_digest': binding['recognition_digest'], 'pricing_event_id': event}
            new_result['cost_sheet_repricing'] = provenance
            identity = digest(new_result)
            new_review = {'schema_version': 1, 'run_id': new_id, 'source_digest': identity, 'revision': 0,
                'header': copy.deepcopy(review['header']), 'lines': copy.deepcopy(review['lines']), 'saved_at': now,
                'status': 'pending_review', 'quotation_unchanged': True, '_events': {}, 'history': [], 'parent': provenance}
            new_recognition = {'id': 'ENG-' + uuid.uuid4().hex, 'run_id': new_id, 'source_digest': identity,
                'status': recognition['status'], 'stage': recognition.get('stage', recognition['status']),
                'started_at': None, 'updated_at': now,
                'message': '沿用已保存的工程资料；内部核价另存新版本，仍待审核。',
                'native': copy.deepcopy(recognition.get('native') or result.get('engineering_native')),
                'ai': copy.deepcopy(recognition.get('ai')), 'error': recognition.get('error'),
                'partial': bool(recognition.get('partial')), '_events': [], 'parent': provenance}
            require(len(encoded(new_result).encode()) <= 64 * 1024 * 1024
                    and len(encoded(new_review).encode()) <= 4 * 1024 * 1024
                    and len(encoded(new_recognition).encode()) <= 32 * 1024 * 1024, '核价记录过大，请拆分订单。')
            record = {'schema_version': 1, 'kind': 'cost_sheet', 'batch_id': batch_id, 'event_id': event,
                      'signature': signature, 'status': 'prepared', 'run_id': new_id, 'source_run_id': run_id,
                      'base_revision': state['revision'], 'source_binding': binding, 'created_at': now,
                      'result_digest': identity, 'result': new_result, 'review': new_review, 'recognition': new_recognition}
            content = encoded(record)
            require(len(content.encode()) <= MAX_BYTES, '内部核价提交记录过大。')
            path = self._directory(batch_id, create=True) / (hashlib.sha256(event.encode()).hexdigest() + '.json')
            require(not path.exists() and not path.is_symlink(), '内部核价请求已存在。', 'event_conflict', 409)
            atomic_write(path, content)
            self._commit(state, path, record)
            return {'batch': self.owner._public(state), 'run_id': new_id, 'source_run_id': run_id, 'replayed': False}

    def recover_locked(self, state):
        recovered = False
        for path, record in self._records(state['id']):
            if record['status'] == 'prepared':
                self._commit(state, path, record)
                recovered = True
        return recovered

    @contextmanager
    def _lease(self):
        path = self.owner.root / '.cost-sheet-worker.lock'
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
            recovered = False
            for folder in sorted(self.owner.root.iterdir()):
                if not BATCH.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
                    continue
                with self.owner._lock(folder.name):
                    if any(record['status'] == 'prepared' for _, record in self._records(folder.name)):
                        recovered = self.recover_locked(self.owner._read(folder.name)) or recovered
            return recovered

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.process_pending_once()
            except Exception:
                # A changed source stays blocked for manual reload; never
                # repair it by inventing rates or launching another reading.
                pass
            self._stop.wait(.5)

    def start_worker(self):
        with self._startup_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name='cost-sheet-recovery')
            self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
