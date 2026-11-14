"""Private trial profiles and sales requests, without SMS or identity claims.

Owners are supplied only by the existing authenticated browser-session layer.
This store never sends messages, invokes AI/CAD, prices parts, or grants a
verified identity. Unverified contact values never identify another owner.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from prep_record import stamp
from workbench_store import WorkbenchError

OWNER = re.compile(r'[a-f0-9]{64}\Z')
LEAD = re.compile(r'LEAD-[a-f0-9]{32}\Z')
EVENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z')
BATCH = re.compile(r'BATCH-[a-f0-9]{32}\Z')
RUN = re.compile(r'RUN-[a-f0-9]{32}\Z')
MANUFACTURING_TYPES = {'sheet_metal', 'machining', 'other'}
STATUSES = {'new', 'contacted', 'trialing', 'proposal', 'won', 'do_not_contact'}
NOTICE_VERSION = 'trial-sales-contact-v1'
MAX_LEADS = 10000
MAX_APPLICATIONS = 30
MAX_TRIALS = 50
MAX_DATABASE_BYTES = 64 * 1024 * 1024


def _require(condition, message, code='invalid_request', status=400):
    if not condition:
        raise WorkbenchError(message, code, status)


def _text(value, maximum, label, *, empty=False, multiline=False):
    _require(isinstance(value, str), label + '格式无效。')
    value = value.strip()
    _require((empty or bool(value)) and len(value) <= maximum
             and not any((ord(char) < 32 and not (multiline and char in '\n\r\t'))
                         or ord(char) == 127 for char in value), label + '为空、过长或包含无效字符。')
    return value.replace('\r\n', '\n').replace('\r', '\n')


def _owner(value):
    _require(isinstance(value, str) and OWNER.fullmatch(value), '客户会话不可用。',
             'customer_session_required', 401)
    return value


def _event(value):
    _require(isinstance(value, str) and EVENT.fullmatch(value), '请求编号无效。')
    return value


def _payload(value, allowed, required):
    _require(isinstance(value, dict) and not (set(value) - set(allowed))
             and set(required) <= set(value), '提交字段无效，请刷新页面重试。')


def _revision(value):
    _require(type(value) is int and value >= 0, '资料版本无效。')
    return value


def _boolean(value):
    _require(type(value) is bool, '请选择是否允许联系。')
    return value


def _contact(method, value):
    _require(isinstance(method, str) and method in {'phone', 'wechat'}, '联系方式类型无效。')
    value = _text(value, 80, '联系方式')
    if method == 'phone':
        value = re.sub(r'[ ()-]', '', value)
        if value.startswith('+86'):
            value = value[3:]
        _require(bool(re.fullmatch(r'1[3-9][0-9]{9}', value)), '请输入中国大陆手机号。')
    else:
        _require(bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{2,79}', value)), '请输入有效的微信联系方式。')
    return value


def _masked(method, value):
    return value[:3] + '****' + value[-4:] if method == 'phone' else value[:1] + '***' + value[-1:]


def _signature(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _redact_contact(value, contact, masked):
    # A user may repeat their contact in a free-text request or a staff note.
    # Default masking also covers that known value, without rewriting IDs.
    if isinstance(value, str):
        return value.replace(contact, masked)
    if isinstance(value, list):
        return [_redact_contact(item, contact, masked) for item in value]
    if isinstance(value, dict):
        protected = {'id', 'status', 'follow_up_status', 'verification_status', 'interest', 'action',
                     'from_status', 'contact_method', 'manufacturing_type', 'notice_version'}
        return {key: item if key in protected or key.endswith('_id') else _redact_contact(item, contact, masked)
                for key, item in value.items()}
    return value


def _binding(value):
    if value is None:
        return None
    _payload(value, {'batch_id', 'run_id', 'name', 'status', 'part_count'}, {'batch_id'})
    _require(isinstance(value['batch_id'], str) and BATCH.fullmatch(value['batch_id']), '关联资料编号无效。')
    result = {'batch_id': value['batch_id']}
    if value.get('run_id') is not None:
        _require(isinstance(value['run_id'], str) and RUN.fullmatch(value['run_id']), '关联报价编号无效。')
        result['run_id'] = value['run_id']
    if 'name' in value:
        result['name'] = _text(value['name'], 240, '关联资料名称')
    if 'status' in value:
        _require(isinstance(value['status'], str) and value['status'] in {'uploaded', 'queued', 'running', 'completed', 'completed_with_issues',
                                     'failed', 'interrupted'}, '关联资料状态无效。')
        result['status'] = value['status']
    if 'part_count' in value:
        _require(type(value['part_count']) is int and 0 <= value['part_count'] <= 50000, '零件数量无效。')
        result['part_count'] = value['part_count']
    return result


class TrialLeadStore:
    """A small SQLite store in a dedicated private directory."""

    def __init__(self, root):
        self.root = Path(root)
        _require(not self.root.is_symlink(), '体验资料存储不可用。', 'trial_store_unavailable', 503)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _require(self.root.is_dir(), '体验资料存储不可用。', 'trial_store_unavailable', 503)
        self.root.chmod(0o700)
        self.path = self.root / 'leads.sqlite3'
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            _require(stat.S_ISREG(os.fstat(fd).st_mode), '体验资料存储不可用。', 'trial_store_unavailable', 503)
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS leads (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL UNIQUE,
                    company TEXT NOT NULL, contact_name TEXT NOT NULL,
                    contact_method TEXT NOT NULL, contact_value TEXT NOT NULL,
                    manufacturing_type TEXT NOT NULL, contact_consent INTEGER NOT NULL,
                    status TEXT NOT NULL, assignee TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS applications (
                    id TEXT PRIMARY KEY, lead_id TEXT NOT NULL REFERENCES leads(id),
                    interest TEXT NOT NULL, note TEXT NOT NULL, contact_consent INTEGER NOT NULL,
                    order_binding TEXT, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS applications_lead ON applications(lead_id, created_at);
                CREATE TABLE IF NOT EXISTS trials (
                    id TEXT PRIMARY KEY, lead_id TEXT NOT NULL REFERENCES leads(id),
                    batch_id TEXT NOT NULL, order_binding TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(lead_id, batch_id)
                );
                CREATE INDEX IF NOT EXISTS trials_lead ON trials(lead_id, updated_at);
                CREATE TABLE IF NOT EXISTS events (
                    scope TEXT NOT NULL, event_id TEXT NOT NULL, operation TEXT NOT NULL,
                    signature TEXT NOT NULL, result_id TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(scope, event_id)
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT NOT NULL REFERENCES leads(id),
                    actor TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS consent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT NOT NULL REFERENCES leads(id),
                    actor TEXT NOT NULL, contact_consent INTEGER NOT NULL,
                    notice_version TEXT NOT NULL, created_at TEXT NOT NULL
                );
            ''')
            pages = MAX_DATABASE_BYTES // db.execute('PRAGMA page_size').fetchone()[0]
            db.execute('PRAGMA max_page_count=' + str(pages))

    @contextmanager
    def _db(self, *, write=False):
        _require(not self.path.is_symlink() and self.path.is_file(), '体验资料存储不可用。',
                 'trial_store_unavailable', 503)
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA busy_timeout=5000')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            if db.in_transaction:
                db.commit()
        except sqlite3.Error as error:
            if db is not None and db.in_transaction:
                db.rollback()
            raise WorkbenchError('体验资料暂时无法保存，请稍后重试。', 'trial_store_unavailable', 503) from error
        except BaseException:
            if db is not None and db.in_transaction:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _lead(db, *, owner=None, lead_id=None):
        if owner is not None:
            return db.execute('SELECT * FROM leads WHERE owner=?', (owner,)).fetchone()
        _require(isinstance(lead_id, str) and LEAD.fullmatch(lead_id), '线索不存在。', 'not_found', 404)
        row = db.execute('SELECT * FROM leads WHERE id=?', (lead_id,)).fetchone()
        _require(row is not None, '线索不存在。', 'not_found', 404)
        return row

    @staticmethod
    def _can_contact(row):
        return bool(row['contact_consent']) and row['status'] != 'do_not_contact'

    @staticmethod
    def _profile(row):
        if row is None:
            return None
        fields = ('company', 'contact_name', 'contact_method', 'contact_value', 'manufacturing_type',
                  'revision', 'created_at', 'updated_at')
        return {**{key: row[key] for key in fields}, 'contact_consent': bool(row['contact_consent']),
                'verification_status': 'unverified'}

    @staticmethod
    def _replay(db, scope, event, operation, signature):
        row = db.execute('SELECT * FROM events WHERE scope=? AND event_id=?', (scope, event)).fetchone()
        if row is not None:
            _require(row['operation'] == operation and row['signature'] == signature,
                     '这个请求编号已经用于其他内容，请刷新后重试。', 'event_conflict', 409)
        return row

    @staticmethod
    def _save_event(db, scope, event, operation, signature, result_id, now):
        db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)', (scope, event, operation, signature, result_id, now))

    @staticmethod
    def _audit(db, lead_id, actor, action, details, now):
        db.execute('INSERT INTO audit(lead_id,actor,action,details,created_at) VALUES(?,?,?,?,?)',
                   (lead_id, actor, action, json.dumps(details, ensure_ascii=False, allow_nan=False), now))

    @staticmethod
    def _consent(db, lead_id, actor, choice, now):
        db.execute('INSERT INTO consent_events(lead_id,actor,contact_consent,notice_version,created_at) VALUES(?,?,?,?,?)',
                   (lead_id, actor, int(choice), NOTICE_VERSION, now))

    def profile(self, owner):
        with self._db() as db:
            return self._profile(self._lead(db, owner=_owner(owner)))

    def save_profile(self, owner, payload):
        owner = _owner(owner)
        required = {'event_id', 'expected_revision', 'company', 'contact_method', 'contact_value',
                    'manufacturing_type', 'contact_consent'}
        _payload(payload, required | {'contact_name'}, required)
        event, revision = _event(payload['event_id']), _revision(payload['expected_revision'])
        values = {'company': _text(payload['company'], 120, '公司名称'),
                  'contact_name': _text(payload.get('contact_name', ''), 80, '联系人', empty=True),
                  'contact_method': payload['contact_method'],
                  'contact_value': _contact(payload['contact_method'], payload['contact_value']),
                  'manufacturing_type': payload['manufacturing_type'],
                  'contact_consent': _boolean(payload['contact_consent'])}
        _require(isinstance(values['manufacturing_type'], str) and values['manufacturing_type'] in MANUFACTURING_TYPES,
                 '加工类型无效。')
        signature = _signature({'expected_revision': revision, **values})
        with self._db(write=True) as db:
            previous = self._replay(db, owner, event, 'profile', signature)
            row = self._lead(db, owner=owner)
            if previous:
                _require(row is not None, '体验资料记录不可用。', 'trial_store_unavailable', 503)
                return {'profile': self._profile(row), 'replayed': True}
            _require(revision == (row['revision'] if row is not None else 0),
                     '资料已有更新，请刷新后重新提交。', 'revision_conflict', 409)
            now, lead_id = stamp(), row['id'] if row is not None else 'LEAD-' + uuid.uuid4().hex
            choice = values['contact_consent']
            if row is None:
                _require(db.execute('SELECT COUNT(*) FROM leads').fetchone()[0] < MAX_LEADS,
                         '当前暂不接受新的体验资料，请联系工作人员。', 'trial_store_limit', 429)
                db.execute('''INSERT INTO leads(id,owner,company,contact_name,contact_method,contact_value,
                    manufacturing_type,contact_consent,status,revision,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (lead_id, owner, values['company'], values['contact_name'], values['contact_method'],
                     values['contact_value'], values['manufacturing_type'], int(choice),
                     'new' if choice else 'do_not_contact', 1, now, now))
            else:
                next_status = ('do_not_contact' if not choice else 'new' if row['status'] == 'do_not_contact' else row['status'])
                db.execute('''UPDATE leads SET company=?,contact_name=?,contact_method=?,contact_value=?,
                    manufacturing_type=?,contact_consent=?,status=?,revision=revision+1,updated_at=? WHERE id=?''',
                    (values['company'], values['contact_name'], values['contact_method'], values['contact_value'],
                     values['manufacturing_type'], int(choice), next_status, now, lead_id))
            self._consent(db, lead_id, 'customer', choice, now)
            self._audit(db, lead_id, 'customer', 'profile_saved', {'changed_fields': sorted(values)}, now)
            self._save_event(db, owner, event, 'profile', signature, lead_id, now)
            return {'profile': self._profile(self._lead(db, owner=owner)), 'replayed': False}

    def _application(self, row, lead):
        return {'id': row['id'], 'interest': row['interest'], 'note': row['note'], 'status': 'submitted',
                'created_at': row['created_at'], 'contact_consent': bool(row['contact_consent']),
                'can_contact': self._can_contact(lead), 'follow_up_status': lead['status'],
                'verification_status': 'unverified',
                'order_binding': json.loads(row['order_binding']) if row['order_binding'] else None}

    def applications(self, owner):
        with self._db() as db:
            lead = self._lead(db, owner=_owner(owner))
            if lead is None:
                return []
            return [self._application(row, lead) for row in db.execute(
                'SELECT * FROM applications WHERE lead_id=? ORDER BY created_at DESC,id DESC', (lead['id'],))]

    @staticmethod
    def _trial(row):
        binding = json.loads(row['order_binding'])
        return {'id': row['id'], **binding, 'order_binding': binding,
                'created_at': row['created_at'], 'updated_at': row['updated_at']}

    def record_trial(self, owner, order_binding):
        """Track an existing owner-checked task; never create a lead or work.

        The caller supplies the authoritative allowlisted task projection after
        checking ownership. Identical polling does not write, update consent,
        or change the profile revision. Keep the 50 most recently changed tasks.
        """
        owner = _owner(owner)
        with self._db(write=True) as db:
            lead = self._lead(db, owner=owner)
            if lead is None:
                return
            binding = _binding(order_binding)
            _require(binding is not None, '关联体验资料无效。')
            encoded = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            existing = db.execute('SELECT * FROM trials WHERE lead_id=? AND batch_id=?',
                                  (lead['id'], binding['batch_id'])).fetchone()
            if existing is not None and existing['order_binding'] == encoded:
                return
            now = stamp()
            if existing is None:
                count = db.execute('SELECT COUNT(*) FROM trials WHERE lead_id=?', (lead['id'],)).fetchone()[0]
                if count >= MAX_TRIALS:
                    # This only bounds lead metadata, never orders or quotes.
                    db.execute('DELETE FROM trials WHERE id IN (SELECT id FROM trials WHERE lead_id=? '
                               'ORDER BY updated_at ASC,id ASC LIMIT ?)', (lead['id'], count - MAX_TRIALS + 1))
                db.execute('INSERT INTO trials VALUES(?,?,?,?,?,?)',
                           ('TRIAL-' + uuid.uuid4().hex, lead['id'], binding['batch_id'], encoded, now, now))
            else:
                db.execute('UPDATE trials SET order_binding=?,updated_at=? WHERE id=?',
                           (encoded, now, existing['id']))
            db.execute('UPDATE leads SET updated_at=? WHERE id=?', (now, lead['id']))
            self._audit(db, lead['id'], 'system', 'trial_recorded',
                        {'batch_id': binding['batch_id'], 'status': binding.get('status')}, now)

    def record_interest(self, owner, payload, order_binding=None):
        owner = _owner(owner)
        required = {'event_id', 'interest', 'contact_consent'}
        _payload(payload, required | {'note', 'batch_id', 'run_id'}, required)
        event, interest = _event(payload['event_id']), payload['interest']
        _require(isinstance(interest, str) and interest in {'factory_trial', 'consultation'}, '申请类型无效。')
        note = _text(payload.get('note', ''), 2000, '需求说明', empty=True, multiline=True)
        choice, binding = _boolean(payload['contact_consent']), _binding(order_binding)
        for key in ('batch_id', 'run_id'):
            if key in payload:
                _require(binding is not None and payload[key] == binding.get(key), '申请与关联资料不一致。')
        # Dynamic task status/name may change during acknowledgment recovery.
        identity = {key: binding.get(key) for key in ('batch_id', 'run_id')} if binding else None
        signature = _signature({'interest': interest, 'note': note, 'contact_consent': choice, 'binding': identity})
        with self._db(write=True) as db:
            previous = self._replay(db, owner, event, 'interest', signature)
            lead = self._lead(db, owner=owner)
            _require(lead is not None, '请先保存公司与联系资料。', 'trial_profile_required', 409)
            if previous:
                row = db.execute('SELECT * FROM applications WHERE id=? AND lead_id=?',
                                 (previous['result_id'], lead['id'])).fetchone()
                _require(row is not None, '申请记录不可用。', 'trial_store_unavailable', 503)
                return {'application': self._application(row, lead), 'profile': self._profile(lead), 'replayed': True}
            _require(db.execute('SELECT COUNT(*) FROM applications WHERE lead_id=?', (lead['id'],)).fetchone()[0]
                     < MAX_APPLICATIONS, '申请次数已达上限，请联系工作人员。', 'trial_application_limit', 429)
            now, application_id = stamp(), 'APP-' + uuid.uuid4().hex
            db.execute('INSERT INTO applications VALUES(?,?,?,?,?,?,?)',
                       (application_id, lead['id'], interest, note, int(choice),
                        json.dumps(binding, ensure_ascii=False) if binding else None, now))
            next_status = 'do_not_contact' if not choice else 'new' if lead['status'] == 'do_not_contact' else lead['status']
            db.execute('UPDATE leads SET contact_consent=?,status=?,revision=revision+1,updated_at=? WHERE id=?',
                       (int(choice), next_status, now, lead['id']))
            self._consent(db, lead['id'], 'customer', choice, now)
            self._audit(db, lead['id'], 'customer', 'interest_submitted',
                        {'application_id': application_id, 'interest': interest, 'has_order': binding is not None}, now)
            self._save_event(db, owner, event, 'interest', signature, application_id, now)
            updated = self._lead(db, owner=owner)
            row = db.execute('SELECT * FROM applications WHERE id=?', (application_id,)).fetchone()
            return {'application': self._application(row, updated), 'profile': self._profile(updated), 'replayed': False}

    def _staff_projection(self, db, lead, *, detail=False):
        fields = ('id', 'company', 'contact_name', 'contact_method', 'manufacturing_type', 'status',
                  'assignee', 'revision', 'created_at', 'updated_at')
        masked = _masked(lead['contact_method'], lead['contact_value'])
        can_contact = self._can_contact(lead)
        reveal = bool(detail and can_contact)
        rows = db.execute('SELECT * FROM applications WHERE lead_id=? ORDER BY created_at DESC,id DESC',
                          (lead['id'],)).fetchall()
        applications = [self._application(row, lead) for row in rows]
        trials = [self._trial(row) for row in db.execute(
            'SELECT * FROM trials WHERE lead_id=? ORDER BY updated_at DESC,id DESC', (lead['id'],))]
        result = {**{key: lead[key] for key in fields}, 'verification_status': 'unverified',
                  'contact_consent': bool(lead['contact_consent']), 'can_contact': can_contact,
                  'contact_value': lead['contact_value'] if reveal else masked,
                  'contact_masked': masked, 'contact_revealed': reveal,
                  'application_count': len(applications), 'latest_application': applications[0] if applications else None,
                  'trial_count': len(trials), 'latest_trial': trials[0] if trials else None}
        if detail:
            result.update(note=lead['note'], applications=applications, trials=trials,
                audit=[{'id': row['id'], 'actor': row['actor'], 'action': row['action'],
                        'details': json.loads(row['details']), 'created_at': row['created_at']}
                       for row in db.execute('SELECT * FROM audit WHERE lead_id=? ORDER BY id DESC LIMIT 200', (lead['id'],))],
                consent_history=[{'id': row['id'], 'actor': row['actor'],
                                  'contact_consent': bool(row['contact_consent']),
                                  'notice_version': row['notice_version'], 'created_at': row['created_at']}
                                 for row in db.execute('SELECT * FROM consent_events WHERE lead_id=? ORDER BY id DESC LIMIT 200',
                                                       (lead['id'],))])
        return result if reveal else _redact_contact(result, lead['contact_value'], masked)

    def staff_list(self, *, limit=50, offset=0, status=None, contactable_only=False):
        _require(type(limit) is int and 1 <= limit <= 100 and type(offset) is int and 0 <= offset <= MAX_LEADS,
                 '分页参数无效。')
        _require(status is None or isinstance(status, str) and status in STATUSES, '跟进状态无效。')
        _require(type(contactable_only) is bool, '联系筛选无效。')
        clauses, arguments = [], []
        if status is not None:
            clauses.append('status=?'); arguments.append(status)
        if contactable_only:
            clauses.append("contact_consent=1 AND status<>'do_not_contact'")
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self._db() as db:
            total = db.execute('SELECT COUNT(*) FROM leads' + where, arguments).fetchone()[0]
            rows = db.execute('SELECT * FROM leads' + where + ' ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?',
                              [*arguments, limit, offset]).fetchall()
            return {'leads': [self._staff_projection(db, row) for row in rows],
                    'total': total, 'limit': limit, 'offset': offset}

    def staff_get(self, lead_id, *, actor=None):
        if actor is not None:
            actor = _text(actor, 80, '操作员')
        with self._db(write=actor is not None) as db:
            lead = self._lead(db, lead_id=lead_id)
            if actor is not None and self._can_contact(lead):
                self._audit(db, lead_id, actor, 'contact_viewed', {}, stamp())
            return self._staff_projection(db, lead, detail=True)

    def staff_update(self, lead_id, payload, *, actor='staff'):
        actor = _text(actor, 80, '操作员')
        required = {'event_id', 'expected_revision', 'status', 'assignee', 'note'}
        _payload(payload, required, required)
        event, revision = _event(payload['event_id']), _revision(payload['expected_revision'])
        status = payload['status']
        _require(isinstance(status, str) and status in STATUSES, '跟进状态无效。')
        assignee = _text(payload['assignee'], 80, '跟进人', empty=True)
        note = _text(payload['note'], 2000, '跟进备注', empty=True, multiline=True)
        signature = _signature({'actor': actor, 'expected_revision': revision,
                                'status': status, 'assignee': assignee, 'note': note})
        with self._db(write=True) as db:
            lead = self._lead(db, lead_id=lead_id)
            previous = self._replay(db, 'staff:' + lead_id, event, 'staff_update', signature)
            if previous:
                return {'lead': self._staff_projection(db, lead, detail=True), 'replayed': True}
            _require(revision == lead['revision'], '线索已有更新，请刷新后重试。', 'revision_conflict', 409)
            choice = bool(lead['contact_consent']) and status != 'do_not_contact'
            now = stamp()
            db.execute('UPDATE leads SET status=?,assignee=?,note=?,contact_consent=?,revision=revision+1,updated_at=? WHERE id=?',
                       (status, assignee, note, int(choice), now, lead_id))
            if bool(lead['contact_consent']) != choice:
                self._consent(db, lead_id, actor, choice, now)
            self._audit(db, lead_id, actor, 'follow_up_updated',
                        {'from_status': lead['status'], 'status': status, 'assignee': assignee, 'note': note}, now)
            self._save_event(db, 'staff:' + lead_id, event, 'staff_update', signature, lead_id, now)
            return {'lead': self._staff_projection(db, self._lead(db, lead_id=lead_id), detail=True), 'replayed': False}
