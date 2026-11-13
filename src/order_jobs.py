"""Durable batch work with expiring leases and token fencing.

The queue never submits model requests. A reclaimed job retains its session and
must inspect that session; callers use the same immutable discovery workspace.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
import uuid

IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')


class JobQueueError(ValueError):
    pass


class LeaseLost(JobQueueError):
    """The caller no longer owns a live lease and must discard its result."""


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise JobQueueError('invalid job identifier')
    return value


def _json(value, limit=2 * 1024 * 1024):
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise JobQueueError('job data must be finite JSON') from exc
    if len(result.encode()) > limit:
        raise JobQueueError('job data exceeds limit')
    return result


def _duration(value, *, allow_zero=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or value < (0 if allow_zero else 1) or value > 86400):
        raise JobQueueError('invalid lease or retry duration')
    return float(value)


class JobQueue:
    def __init__(self, dbpath, *, clock=time.time):
        selected = Path(dbpath).absolute()
        if selected.is_symlink():
            raise JobQueueError('job database symlinks are prohibited')
        # The caller chooses the directory. Canonicalize macOS /var temporary
        # prefixes once; never follow a symlink at the database filename.
        self.path = selected.parent.resolve() / selected.name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with self._connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS order_jobs (
                    id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    session_id TEXT,
                    result TEXT,
                    error TEXT,
                    progress TEXT,
                    warnings TEXT NOT NULL DEFAULT '[]',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT,
                    lease_until REAL,
                    worker_id TEXT,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    next_attempt_at REAL NOT NULL,
                    UNIQUE(order_id, kind, input_hash)
                );
                CREATE INDEX IF NOT EXISTS order_jobs_ready
                    ON order_jobs(status, next_attempt_at, lease_until);
                CREATE TABLE IF NOT EXISTS order_job_events (
                    order_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    job_id TEXT NOT NULL REFERENCES order_jobs(id),
                    PRIMARY KEY(order_id, event_id)
                );
            ''')

    @contextmanager
    def _connection(self):
        if self.path.is_symlink():
            raise JobQueueError('job database symlinks are prohibited')
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _view(row):
        if row is None:
            raise KeyError('job not found')
        result = dict(row)
        for name in ('payload', 'result', 'error', 'progress', 'warnings'):
            result[name] = json.loads(result[name]) if result[name] is not None else None
        result['stop_requested'] = bool(result['stop_requested'])
        result['requires_inspect'] = bool(result['session_id']) or result['status'] == 'unknown'
        return result

    def enqueue(self, order_id, kind, input_hash, payload, event_id):
        _identifier(order_id), _identifier(kind), _identifier(event_id)
        if not isinstance(input_hash, str) or not HASH.fullmatch(input_hash):
            raise JobQueueError('input_hash must be a SHA-256 hex digest')
        if not isinstance(payload, dict):
            raise JobQueueError('job payload must be an object')
        encoded = _json(payload)
        signature = hashlib.sha256(_json([kind, input_hash, payload]).encode()).hexdigest()
        job_id = 'JOB-' + hashlib.sha256(_json([order_id, kind, input_hash]).encode()).hexdigest()[:32]
        now = self.clock()
        with self._transaction() as db:
            previous = db.execute('SELECT * FROM order_job_events WHERE order_id=? AND event_id=?',
                                  (order_id, event_id)).fetchone()
            if previous:
                if previous['signature'] != signature:
                    raise JobQueueError('event id reused with different job inputs')
                return self._view(db.execute('SELECT * FROM order_jobs WHERE id=?', (previous['job_id'],)).fetchone())
            existing = db.execute('SELECT * FROM order_jobs WHERE order_id=? AND kind=? AND input_hash=?',
                                  (order_id, kind, input_hash)).fetchone()
            if existing and existing['payload'] != encoded:
                raise JobQueueError('same input hash has different job payload')
            if not existing:
                db.execute('''INSERT INTO order_jobs
                    (id,order_id,kind,input_hash,payload,status,created_at,updated_at,next_attempt_at)
                    VALUES (?,?,?,?,?,'queued',?,?,?)''',
                           (job_id, order_id, kind, input_hash, encoded, now, now, now))
            else:
                job_id = existing['id']
            db.execute('INSERT INTO order_job_events VALUES (?,?,?,?)', (order_id, event_id, signature, job_id))
            return self._view(db.execute('SELECT * FROM order_jobs WHERE id=?', (job_id,)).fetchone())

    def get(self, job_id):
        _identifier(job_id)
        with self._connection() as db:
            return self._view(db.execute('SELECT * FROM order_jobs WHERE id=?', (job_id,)).fetchone())

    def list(self, order_id):
        _identifier(order_id)
        with self._connection() as db:
            return [self._view(row) for row in db.execute(
                'SELECT * FROM order_jobs WHERE order_id=? ORDER BY created_at,id', (order_id,))]

    def claim(self, worker_id, lease_seconds=60):
        _identifier(worker_id)
        duration = _duration(lease_seconds)
        now = self.clock()
        with self._transaction() as db:
            row = db.execute('''SELECT * FROM order_jobs WHERE
                ((status IN ('queued','unknown') AND next_attempt_at<=?) OR
                 (status='running' AND lease_until<=?))
                ORDER BY next_attempt_at,created_at,id LIMIT 1''', (now, now)).fetchone()
            if row is None:
                return None
            recovered = row['status'] in {'running', 'unknown'} or bool(row['session_id'])
            token = uuid.uuid4().hex
            db.execute('''UPDATE order_jobs SET status='running',worker_id=?,lease_token=?,lease_until=?,
                attempt=attempt+1,updated_at=? WHERE id=?''', (worker_id, token, now + duration, now, row['id']))
            result = self._view(db.execute('SELECT * FROM order_jobs WHERE id=?', (row['id'],)).fetchone())
            result['requires_inspect'] = recovered
            result['recovered'] = recovered
            return result

    def _owned(self, db, job_id, token, now):
        _identifier(job_id)
        if not isinstance(token, str) or not token:
            raise LeaseLost('job lease is no longer owned')
        row = db.execute('SELECT * FROM order_jobs WHERE id=?', (job_id,)).fetchone()
        if (row is None or row['status'] != 'running' or row['lease_token'] != token
                or row['lease_until'] is None or row['lease_until'] <= now):
            raise LeaseLost('job lease is no longer owned')
        return row

    def heartbeat(self, job_id, token, lease_seconds=60, *, progress=None, session_id=None):
        duration = _duration(lease_seconds)
        if progress is not None and not isinstance(progress, dict):
            raise JobQueueError('job progress must be an object')
        encoded = _json(progress, 32768) if progress is not None else None
        if session_id is not None:
            _identifier(session_id)
        now = self.clock()
        with self._transaction() as db:
            row = self._owned(db, job_id, token, now)
            if session_id and row['session_id'] and row['session_id'] != session_id:
                raise JobQueueError('job session is immutable; recover the existing session')
            db.execute('''UPDATE order_jobs SET lease_until=?,updated_at=?,
                session_id=COALESCE(?,session_id),progress=COALESCE(?,progress) WHERE id=?''',
                       (now + duration, now, session_id, encoded, job_id))
            return self._view(db.execute('SELECT * FROM order_jobs WHERE id=?', (job_id,)).fetchone())

    def finish(self, job_id, token, result):
        if not isinstance(result, dict):
            raise JobQueueError('job result must be an object')
        encoded = _json(result)
        warnings = result.get('warnings', [])
        if not isinstance(warnings, list) or len(warnings) > 256 or any(
                not isinstance(value, str) or len(value) > 2000 for value in warnings):
            raise JobQueueError('invalid job warnings')
        now = self.clock()
        with self._transaction() as db:
            row = self._owned(db, job_id, token, now)
            if row['stop_requested']:
                raise JobQueueError('job stop requested; discard late completion')
            db.execute('''UPDATE order_jobs SET status='completed',result=?,warnings=?,error=NULL,
                lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?''',
                       (encoded, _json(warnings), now, job_id))
            return self._view(db.execute('SELECT * FROM order_jobs WHERE id=?', (job_id,)).fetchone())

    def fail(self, job_id, token, error, *, retryable=False, delay_seconds=0, unknown=False, stopped=False):
        if type(retryable) is not bool or type(unknown) is not bool or type(stopped) is not bool:
            raise JobQueueError('explicit boolean job failure options required')
        delay = _duration(delay_seconds, allow_zero=True)
        encoded = _json(error, 16384)
        status = 'stopped' if stopped else 'unknown' if unknown else 'queued' if retryable else 'failed'
        now = self.clock()
        with self._transaction() as db:
            row = self._owned(db, job_id, token, now)
            if row['stop_requested'] and not stopped:
                # A request to cancel is not evidence that the admitted model
                # has ended. Preserve its session for a later inspect/cancel.
                # ``unknown`` also covers child-task sessions stored by the
                # existing single-part reader rather than this queue row.
                if row['session_id'] or unknown:
                    status = 'unknown'
                    delay = max(delay, 15)
                else:
                    status = 'stopped'
            db.execute('''UPDATE order_jobs SET status=?,error=?,lease_token=NULL,lease_until=NULL,
                updated_at=?,next_attempt_at=? WHERE id=?''', (status, encoded, now, now + delay, job_id))
            return self._view(db.execute('SELECT * FROM order_jobs WHERE id=?', (job_id,)).fetchone())

    def request_stop(self, job_id):
        _identifier(job_id)
        now = self.clock()
        with self._transaction() as db:
            row = db.execute('SELECT * FROM order_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise KeyError('job not found')
            if row['status'] in {'completed', 'failed', 'stopped'}:
                return self._view(row)
            # A queued request without an admitted session can stop immediately.
            status = 'stopped' if row['status'] == 'queued' and not row['session_id'] else row['status']
            db.execute('UPDATE order_jobs SET stop_requested=1,status=?,updated_at=? WHERE id=?',
                       (status, now, job_id))
            return self._view(db.execute('SELECT * FROM order_jobs WHERE id=?', (job_id,)).fetchone())
