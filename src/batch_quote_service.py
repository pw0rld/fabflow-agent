"""Durable ZIP/RAR/STEP quotation batches over the existing native CAD engine.

The archive is inspected before any CAD work is queued. A single fenced worker
processes source files serially; quote runs are immutable calculation snapshots.
Reference drawings are retained without claiming to interpret their processes.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from decimal import localcontext
import fcntl
import hashlib
import io
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
import time
import unicodedata
from urllib.parse import quote
import uuid
import zipfile
import zlib

from prep_record import atomic_write, digest, only_keys, stamp
from workbench_store import WorkbenchError
from batch_quote_costing import calculate_batch, render_artifact, validate_settings, _quantity_and_volume, _weight, aggregate_display_groups
from cad_manufacturing import validate_geometry
from batch_quote_archive import ArchiveError, unpack_rar


MAX_BATCH_BYTES = 50 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 200
BATCH_ID = re.compile(r'BATCH-[0-9a-f]{32}\Z')
FILE_ID = re.compile(r'F[0-9]{4}\Z')
RUN_ID = re.compile(r'RUN-[0-9a-f]{32}\Z')
LINE_ID = re.compile(r'L[0-9]{4,7}\Z')
NATIVE_ID = re.compile(r'CAD-[0-9a-f]{32}\Z')
EVENT_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
PRICING_RECEIPT = re.compile(r'[0-9a-f]{64}\.json\Z')
MAX_PRICING_BYTES = 128 * 1024 * 1024
ARCHIVES = {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.tgz'}
BUSY = {'queued', 'running'}
ARTIFACTS = {'quote.html', 'quote-print.html', 'bom-summary.csv', 'bom.csv', 'cost.csv', 'quote.json',
             'preview-summary.html', 'preview-source.html', 'preview-cost.html', 'preview-record.html',
             'engineering-bom.html', 'engineering-bom.csv', 'materials.html', 'processes.html',
             'customer-quote.html', 'customer-print.html', 'internal-cost.html', 'internal-cost.csv'}


def demand(condition, message, code='invalid_request', status=400):
    if not condition:
        raise WorkbenchError(message, code, status)


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'


def _kind(name):
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in {'.step', '.stp'}:
        return 'step'
    if suffix in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}:
        return 'image'
    return 'pdf' if suffix == '.pdf' else 'other'


def _leaf(name):
    demand(isinstance(name, str) and 0 < len(name) <= 240 and name == Path(name).name
           and '/' not in name and '\\' not in name and ':' not in name
           and not any(ord(char) < 32 for char in name), '文件名无效或过长。')
    return name


def _zip_path(name):
    demand(isinstance(name, str) and 0 < len(name) <= 1024 and '\\' not in name
           and not any(ord(char) < 32 for char in name), '压缩包内含无效路径。')
    value = name[:-1] if name.endswith('/') else name
    parts = value.split('/')
    demand(parts and all(part not in {'', '.', '..'} and ':' not in part for part in parts)
           and not PurePosixPath(value).is_absolute(), '压缩包内含不允许的路径。')
    return value


class _Interrupted(Exception):
    pass


class BatchQuoteService:
    API_PREFIX = '/workbench/api/quote-batches'

    def __init__(self, root, cad, manufacturing, *, processor=None, poll_seconds=0.5,
                 file_timeout=300):
        self.root = Path(root)
        demand(not self.root.is_symlink(), '批量报价存储目录无效。')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cad, self.manufacturing, self.processor = cad, manufacturing, processor
        self.poll_seconds, self.file_timeout = poll_seconds, file_timeout
        self._thread = None
        self._stop = threading.Event()
        self._mutex = threading.RLock()
        from batch_engineering_recognition import EngineeringRecognition
        self.engineering_recognition = EngineeringRecognition(self)
        from batch_quote_assistant_service import QuoteAssistantService
        self.assistant_parser = None
        self.assistant = QuoteAssistantService(self)
        from batch_cost_sheet_service import BatchCostSheetService
        self.cost_sheet = BatchCostSheetService(self)
        from batch_process_estimate_service import BatchProcessEstimateService
        self.process_pricing = BatchProcessEstimateService(self)

    def _folder(self, batch_id):
        demand(isinstance(batch_id, str) and BATCH_ID.fullmatch(batch_id), '批量报价编号无效。')
        folder = self.root / batch_id
        demand(not folder.is_symlink(), '批量报价目录无效。')
        return folder

    @contextmanager
    def _lock(self, batch_id):
        self._folder(batch_id)
        with self._mutex:
            path = self.root / (batch_id + '.lock')
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'r+') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                yield

    def _read(self, batch_id):
        path = self._folder(batch_id) / 'state.json'
        demand(path.is_file() and not path.is_symlink(), '批量报价记录不存在。', 'not_found', 404)
        demand(path.stat().st_size <= 32 * 1024 * 1024, '批量报价记录过大。')
        state = json.loads(path.read_text(encoding='utf-8'))
        demand(state.get('id') == batch_id, '批量报价记录编号不一致。')
        return state

    def _save(self, state):
        state['updated_at'] = stamp()
        atomic_write(self._folder(state['id']) / 'state.json', _json(state))

    @classmethod
    def _public(cls, state):
        public = copy.deepcopy({key: value for key, value in state.items() if not key.startswith('_')})
        if isinstance(public.get('result'), dict) and isinstance(public['result'].get('lines'), list):
            public['result']['display_groups'] = aggregate_display_groups(public['result'])
            batch_id, run_id = public.get('id'), public['result'].get('run_id')
            if isinstance(batch_id, str) and BATCH_ID.fullmatch(batch_id) and isinstance(run_id, str) and RUN_ID.fullmatch(run_id):
                links = public['result'].get('links')
                links = copy.deepcopy(links) if isinstance(links, dict) else {}
                prefix = cls.API_PREFIX + '/' + batch_id + '/runs/' + run_id + '/'
                links.update(quote_url=prefix + 'customer-quote.html', bom_url=prefix + 'bom.csv',
                             cost_url=prefix + 'cost.csv', json_url=prefix + 'quote.json',
                             summary_bom_url=prefix + 'bom-summary.csv', print_url=prefix + 'customer-print.html',
                             summary_bom_preview_url=prefix + 'preview-summary.html',
                             bom_preview_url=prefix + 'preview-source.html',
                             cost_preview_url=prefix + 'preview-cost.html',
                             record_preview_url=prefix + 'preview-record.html',
                             engineering_url=prefix + 'engineering',
                             engineering_bom_url=prefix + 'engineering-bom.html',
                             engineering_bom_csv_url=prefix + 'engineering-bom.csv',
                             materials_url=prefix + 'materials.html', processes_url=prefix + 'processes.html',
                             cost_sheet_url=prefix + 'cost-sheet', internal_cost_url=prefix + 'internal-cost.html',
                             internal_cost_csv_url=prefix + 'internal-cost.csv')
                public['result']['links'] = links
        batch_id = public.get('id')
        for run in public.get('runs', []):
            run_id = run.get('id') if isinstance(run, dict) else None
            if isinstance(batch_id, str) and BATCH_ID.fullmatch(batch_id) and isinstance(run_id, str) and RUN_ID.fullmatch(run_id):
                prefix = cls.API_PREFIX + '/' + batch_id + '/runs/' + run_id + '/'
                run['links'] = {'quote_url': prefix + 'customer-quote.html', 'bom_url': prefix + 'bom.csv',
                                'cost_url': prefix + 'cost.csv', 'json_url': prefix + 'quote.json'}
        return public

    def get(self, batch_id):
        with self._lock(batch_id):
            return self._public(self._read(batch_id))

    def list(self):
        rows = []
        for folder in self.root.iterdir():
            if not BATCH_ID.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
                continue
            with self._lock(folder.name):
                state = self._read(folder.name)
                row = {key: copy.deepcopy(state.get(key)) for key in
                       ('id', 'name', 'status', 'revision', 'created_at', 'updated_at', 'progress', 'settings')}
                row['files_count'] = len(state['files'])
                row['result'] = ({key: copy.deepcopy(state['result'].get(key)) for key in
                                 ('totals', 'weight_summary', 'links', 'run_id')} if state.get('result') else None)
                rows.append(row)
        return sorted(rows, key=lambda row: (row['updated_at'], row['id']), reverse=True)[:100]

    @staticmethod
    def _event(value):
        demand(isinstance(value, str) and EVENT_ID.fullmatch(value), '请求编号无效，请重新提交。')
        return value

    def create(self, name, content, event_id):
        _leaf(name)
        self._event(event_id)
        demand(isinstance(content, bytes) and 0 < len(content) <= MAX_BATCH_BYTES,
               '请选择非空、大小不超过 50 MB 的 ZIP、RAR 或 STEP 文件。')
        suffix = Path(name).suffix.lower()
        demand(suffix in {'.zip', '.rar', '.stp', '.step'}, '整单报价支持 ZIP、RAR、STEP 或 STP 文件。')
        if suffix in {'.stp', '.step'}:
            demand(len(content) <= MAX_FILE_BYTES, '单份 STEP 文件不能超过 20 MB。')
        source_sha = hashlib.sha256(content).hexdigest()
        signature = digest([name, source_sha])
        batch_id = 'BATCH-' + hashlib.sha256(event_id.encode()).hexdigest()[:32]
        with self._lock(batch_id):
            folder = self._folder(batch_id)
            if folder.exists():
                old = self._read(batch_id)
                demand(old.get('_create_signature') == signature, '同一上传请求对应的文件已变化。', 'event_conflict', 409)
                return self._public(old)
            staging = self.root / ('.upload-' + uuid.uuid4().hex)
            staging.mkdir(mode=0o700)
            (staging / 'files').mkdir(mode=0o700)
            try:
                files = self._unpack(staging, name, content)
                demand(files, '压缩包内没有可保留的文件。')
                (staging / 'original.bin').write_bytes(content)
                now = stamp()
                state = {'id': batch_id, 'name': name, 'status': 'uploaded', 'revision': 0,
                         'created_at': now, 'updated_at': now, 'source_sha256': source_sha,
                         'files': files, 'settings': None, 'result': None, 'runs': [], 'error': None,
                         'progress': {'total': sum(row['kind'] == 'step' and not row.get('duplicate_of') for row in files),
                                      'done': 0, 'current': None, 'message': '文件清单已保存，填写报价条件后开始。'},
                         '_create_signature': signature, '_events': {}, '_job': None}
                atomic_write(staging / 'state.json', _json(state))
                staging.rename(folder)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            return self._public(state)

    def _unpack(self, staging, name, content):
        files, hashes = [], {}

        def retain(relative, data):
            demand(len(files) < MAX_FILES, '每个压缩包最多保留 200 个文件。')
            demand(len(data) <= MAX_FILE_BYTES, '压缩包内含超过 20 MB 的文件。')
            fid = 'F' + str(len(files) + 1).zfill(4)
            kind = _kind(relative)
            sha = hashlib.sha256(data).hexdigest()
            duplicate = hashes.get(sha) if kind == 'step' else None
            row = {'id': fid, 'name': PurePosixPath(relative).name, 'relative_path': relative,
                   'kind': kind, 'size': len(data), 'sha256': sha,
                   'status': 'reference' if kind != 'step' or duplicate else 'waiting',
                   'error': None, 'solid_count': None, 'duplicate_of': duplicate,
                   'note': ('与包内另一份 STEP 内容相同，不重复计价。' if duplicate else
                            '附图已保留，未自动提取或计入工艺费用。' if kind in {'image', 'pdf'} else
                            '此格式暂不自动处理，原件保留。' if kind == 'other' else '')}
            files.append(row)
            (staging / 'files' / (fid + '.bin')).write_bytes(data)
            if kind == 'step' and not duplicate:
                hashes[sha] = fid

        if Path(name).suffix.lower() == '.rar':
            try:
                unpack_rar(content, staging, retain, max_file_bytes=MAX_FILE_BYTES,
                           max_unpacked_bytes=MAX_UNPACKED_BYTES, max_files=MAX_FILES)
            except ArchiveError as error:
                raise WorkbenchError(error.message, error.code, error.status) from error
            return files
        if Path(name).suffix.lower() != '.zip':
            retain(name, content)
            return files
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                entries = archive.infolist()
                demand(len(entries) <= 1000, '压缩包内目录或文件数量过多。')
                declared = actual = 0
                seen = set()
                for info in entries:
                    relative = _zip_path(info.filename)
                    mode = info.external_attr >> 16
                    demand(not stat.S_ISLNK(mode) and (not stat.S_IFMT(mode) or
                           stat.S_ISREG(mode) or stat.S_ISDIR(mode)), '压缩包不支持链接或特殊文件。')
                    demand(not info.flag_bits & 1, '请先移除 ZIP 密码后再上传。')
                    demand(info.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}, 'ZIP 使用了暂不支持的压缩方式。')
                    declared += info.file_size
                    demand(0 <= info.file_size <= MAX_FILE_BYTES and declared <= MAX_UNPACKED_BYTES,
                           'ZIP 展开后文件过大，单文件限 20 MB、总量限 200 MB。')
                    demand(info.file_size <= max(1024 * 1024, info.compress_size * 1000), 'ZIP 压缩比例超出处理范围。')
                    parts = relative.split('/')
                    if info.is_dir() or any(part == '__MACOSX' or part == '.DS_Store' or part.startswith('._') for part in parts):
                        continue
                    key = unicodedata.normalize('NFC', relative).casefold()
                    demand(key not in seen, 'ZIP 内含重复文件路径，请整理后重新上传。')
                    seen.add(key)
                    demand(PurePosixPath(relative).suffix.lower() not in ARCHIVES, '请展开内部压缩包后再上传，不支持嵌套压缩包。')
                    demand(len(PurePosixPath(relative).name) <= 240, 'ZIP 内有过长文件名。')
                    chunks, size = [], 0
                    with archive.open(info) as stream:
                        while True:
                            chunk = stream.read(256 * 1024)
                            if not chunk:
                                break
                            size += len(chunk)
                            actual += len(chunk)
                            demand(size <= MAX_FILE_BYTES and actual <= MAX_UNPACKED_BYTES, 'ZIP 展开大小超出处理范围。')
                            chunks.append(chunk)
                    demand(size == info.file_size, 'ZIP 文件长度校验失败。')
                    retain(relative, b''.join(chunks))
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError, zlib.error) as error:
            raise WorkbenchError('ZIP 无法完整读取，请重新压缩后上传。') from error
        return files

    def _guard(self, state, payload, operation):
        demand(not isinstance((state.get('result') or {}).get('cost_sheet'), dict),
               '该版本已采用内部核价单，请在核价单调整并另存新版本，避免按旧费率覆盖明细。', 'cost_sheet_required', 409)
        self._no_pending_pricing(state['id'])
        self._event(payload.get('request_id'))
        signature = digest([operation, payload])
        previous = state['_events'].get(payload['request_id'])
        if previous is not None:
            demand(previous == signature, '同一请求编号的内容已变化。', 'event_conflict', 409)
            return False
        demand(type(payload.get('expected_revision')) is int and payload['expected_revision'] == state['revision'],
               '任务进度已更新，请刷新后重试。', 'revision_conflict', 409)
        demand(state['status'] not in BUSY, '当前任务仍在后台处理。', 'already_running', 409)
        demand(len(state['_events']) < 1000 and len(state['runs']) < 100, '本任务历史过多，请建立新的报价任务。')
        state['_events'][payload['request_id']] = signature
        return True

    def _enqueue(self, state, file_ids, settings):
        run_id = 'RUN-' + uuid.uuid4().hex
        state['settings'] = copy.deepcopy(settings)
        state['status'], state['error'] = 'queued', None
        state['_job'] = {'run_id': run_id, 'file_ids': file_ids, 'settings': copy.deepcopy(settings)}
        for row in state['files']:
            if row['id'] in file_ids:
                row.update(status='waiting', error=None)
        self._progress(state, '已加入后台队列，关闭页面后仍会继续。')
        state['revision'] += 1
        self._save(state)

    def start(self, batch_id, payload):
        only_keys(payload, {'expected_revision', 'request_id', 'settings'}, 'batch quotation start')
        settings = validate_settings(payload.get('settings'))
        if self.processor is None:
            demand(getattr(self.manufacturing, 'analyzer', None) is None,
                   '批量报价须使用独立的原生 CAD 服务，当前未开始处理。')
        with self._lock(batch_id):
            state = self._read(batch_id)
            if not self._guard(state, payload, 'start'):
                return self._public(state)
            steps = [row for row in state['files'] if row['kind'] == 'step' and not row.get('duplicate_of')]
            demand(steps, '资料包没有 STEP/STP 文件，暂不能生成整单报价。')
            self._enqueue(state, [row['id'] for row in steps if row['status'] != 'ready'], settings)
            return self._public(state)

    def retry(self, batch_id, payload):
        only_keys(payload, {'expected_revision', 'request_id', 'file_ids'}, 'batch quotation retry')
        with self._lock(batch_id):
            state = self._read(batch_id)
            if not self._guard(state, payload, 'retry'):
                return self._public(state)
            selected = payload.get('file_ids')
            demand(isinstance(selected, list) and 0 < len(selected) <= MAX_FILES
                   and all(isinstance(value, str) for value in selected)
                   and len(set(selected)) == len(selected), '请选择需要重试的失败文件。')
            failed = {row['id'] for row in state['files'] if row['kind'] == 'step' and row['status'] == 'failed' and not row.get('duplicate_of')}
            demand(all(isinstance(value, str) and value in failed for value in selected), '只能重试本任务读取失败的 STEP 文件。')
            demand(state['settings'] is not None, '请先选择报价条件。')
            self._enqueue(state, selected, state['settings'])
            return self._public(state)

    @staticmethod
    def _progress(state, message, current=None):
        steps = [row for row in state['files'] if row['kind'] == 'step' and not row.get('duplicate_of')]
        state['progress'] = {'total': len(steps), 'done': sum(row['status'] in {'ready', 'failed'} for row in steps),
                             'current': current, 'message': message}

    def file(self, batch_id, file_id):
        demand(isinstance(file_id, str) and FILE_ID.fullmatch(file_id), '附件编号无效。')
        with self._lock(batch_id):
            state = self._read(batch_id)
            row = next((row for row in state['files'] if row['id'] == file_id), None)
            demand(row is not None, '附件不存在。', 'not_found', 404)
            path = self._folder(batch_id) / 'files' / (file_id + '.bin')
            demand(path.is_file() and not path.is_symlink() and path.stat().st_size == row['size'], '附件校验失败。')
            content = path.read_bytes()
            demand(hashlib.sha256(content).hexdigest() == row['sha256'], '附件校验失败。')
            mime = {'image': mimetypes.guess_type(row['name'])[0] or 'application/octet-stream',
                    'pdf': 'application/pdf'}.get(row['kind'], 'application/octet-stream')
            return content, mime, row['name']

    def artifact(self, batch_id, run_id, filename):
        demand(isinstance(run_id, str) and RUN_ID.fullmatch(run_id) and filename in ARTIFACTS, '报价文件编号无效。')
        with self._lock(batch_id):
            state = self._read(batch_id)
            demand(any(run['id'] == run_id for run in state['runs']), '报价记录不存在。', 'not_found', 404)
            path = self._folder(batch_id) / 'runs' / (run_id + '.json')
            demand(path.is_file() and not path.is_symlink(), '报价记录不可用。')
            result = json.loads(path.read_text(encoding='utf-8'))
        if filename in {'engineering-bom.html', 'engineering-bom.csv', 'materials.html', 'processes.html',
                        'internal-cost.html', 'internal-cost.csv', 'customer-quote.html', 'customer-print.html'}:
            from batch_engineering_review import read_review
            with self._lock(batch_id):
                review = read_review(self._folder(batch_id), run_id, result)
                recognition = self.engineering_recognition.read(batch_id, run_id, result)
            previews = self.parts_manifest(batch_id, run_id)['lines'] if filename in {'customer-quote.html', 'customer-print.html'} else None
            content, mime = render_artifact(result, filename, engineering_review=review, engineering_recognition=recognition,
                                            part_previews=previews)
        elif filename in {'quote.html', 'quote-print.html'}:
            previews = self.parts_manifest(batch_id, run_id)['lines']
            content, mime = render_artifact(result, filename, part_previews=previews)
        else:
            content, mime = render_artifact(result, filename)
        return content, mime, run_id + '-' + filename

    def engineering(self, batch_id, run_id):
        from batch_engineering_review import read_review
        from batch_engineering_bom import build_engineering_bom
        with self._lock(batch_id):
            state = self._read(batch_id)
            result = self._preview_run_locked(state, run_id)
            review = read_review(self._folder(batch_id), run_id, result)
            recognition = self.engineering_recognition.read(batch_id, run_id, result)
        return {'revision': review['revision'], 'review': review,
                'recognition': self.engineering_recognition.public(result, recognition),
                'manifest': build_engineering_bom(result, aggregate_display_groups(result), review=review, recognition=recognition)}

    def save_engineering(self, batch_id, run_id, payload):
        from batch_engineering_review import save_review
        from batch_engineering_bom import build_engineering_bom
        with self._lock(batch_id):
            self._no_pending_pricing(batch_id)
            state = self._read(batch_id)
            result = self._preview_run_locked(state, run_id)
            review = save_review(self._folder(batch_id), run_id, result, payload)
            recognition = self.engineering_recognition.read(batch_id, run_id, result)
        return {'revision': review['revision'], 'review': review,
                'recognition': self.engineering_recognition.public(result, recognition),
                'manifest': build_engineering_bom(result, aggregate_display_groups(result), review=review, recognition=recognition)}

    def recognize_engineering(self, batch_id, run_id, payload):
        with self._lock(batch_id):
            self._no_pending_pricing(batch_id)
        self.engineering_recognition.enqueue(batch_id, run_id, payload)
        return self.engineering(batch_id, run_id)

    def _preview_run(self, batch_id, run_id):
        with self._lock(batch_id):
            state = self._read(batch_id)
            return state, self._preview_run_locked(state, run_id)

    def _preview_run_locked(self, state, run_id):
        """Read a validated immutable snapshot while the caller holds its lock."""
        demand(isinstance(run_id, str) and RUN_ID.fullmatch(run_id), '报价运行编号无效。')
        batch_id = state['id']
        demand(any(run['id'] == run_id for run in state['runs']), '报价记录不存在。', 'not_found', 404)
        directory = self._folder(batch_id) / 'runs'
        path = directory / (run_id + '.json')
        demand(not directory.is_symlink() and path.is_file() and not path.is_symlink()
               and path.stat().st_size <= 64 * 1024 * 1024, '报价记录不可用。')
        result = json.loads(path.read_text(encoding='utf-8'))
        demand(result.get('batch_id') == batch_id and result.get('run_id') == run_id
               and result.get('source_sha256') == state['source_sha256'], '报价记录来源不一致。')
        lines, sources = result.get('lines'), result.get('source_files')
        demand(isinstance(lines, list) and len(lines) <= 50000 and all(isinstance(line, dict) for line in lines)
               and isinstance(sources, list) and len(sources) <= MAX_FILES
               and all(isinstance(source, dict) for source in sources), '报价预览记录无效。')
        ids = [line.get('id') for line in lines]
        demand(all(isinstance(value, str) and LINE_ID.fullmatch(value) for value in ids)
               and len(set(ids)) == len(ids), '报价物料编号无效或重复。')
        return result

    def _engineering_pricing_context(self, state, run_id):
        from batch_engineering_review import read_review
        from batch_engineering_bom import build_engineering_bom
        result = self._preview_run_locked(state, run_id)
        review = read_review(self._folder(state['id']), run_id, result)
        recognition = self.engineering_recognition.read(state['id'], run_id, result)
        binding = {'batch_revision': state['revision'], 'run_id': run_id,
                   'source_digest': digest(result), 'review_revision': review['revision'],
                   'review_digest': digest(review), 'recognition_id': recognition.get('id'),
                   'recognition_digest': digest(recognition)}
        manifest = build_engineering_bom(result, aggregate_display_groups(result), review=review, recognition=recognition)
        return result, review, recognition, binding, manifest

    def engineering_pricing(self, batch_id, run_id):
        """Read a source-bound plan; viewing never starts CAD/AI or creates files."""
        from batch_engineering_pricing import pricing_plan
        with self._lock(batch_id):
            state = self._read(batch_id)
            result, review, recognition, binding, manifest = self._engineering_pricing_context(state, run_id)
            plan = pricing_plan(result, manifest)
            plan['policy']['source_binding'] = binding
            return {'plan': plan, 'expected_revision': state['revision'],
                    'review_revision': review['revision'], 'recognition_id': recognition.get('id'),
                    'source_digest': binding['source_digest'], 'source_binding': binding,
                    'current_run_id': (state.get('result') or {}).get('run_id'),
                    'can_reprice': state['status'] not in BUSY and recognition['status'] not in BUSY
                                   and (state.get('result') or {}).get('run_id') == run_id
                                   and not isinstance(result.get('cost_sheet'), dict)}

    def _pricing_directory(self, batch_id, *, create=False):
        directory = self._folder(batch_id) / 'engineering-pricing'
        demand(not directory.is_symlink() and (not directory.exists() or directory.is_dir()), '工程核价记录目录无效。')
        if create:
            directory.mkdir(mode=0o700, exist_ok=True)
        return directory

    def _pricing_records(self, batch_id):
        directory = self._pricing_directory(batch_id)
        if not directory.exists():
            return []
        paths = sorted(directory.iterdir())
        demand(len(paths) <= 100, '本任务核价历史过多，请建立新的报价任务。')
        rows = []
        for path in paths:
            demand(PRICING_RECEIPT.fullmatch(path.name) and not path.is_symlink() and path.is_file()
                   and path.stat().st_size <= MAX_PRICING_BYTES, '工程核价记录无效。')
            row = json.loads(path.read_text(encoding='utf-8'))
            demand(isinstance(row, dict), '工程核价记录格式无效。')
            event = row.get('event_id')
            demand(isinstance(event, str) and EVENT_ID.fullmatch(event)
                   and path.name == hashlib.sha256(event.encode()).hexdigest() + '.json'
                   and row.get('batch_id') == batch_id and row.get('status') in {'prepared', 'committed'}
                   and isinstance(row.get('run_id'), str) and RUN_ID.fullmatch(row['run_id'])
                   and isinstance(row.get('source_run_id'), str) and RUN_ID.fullmatch(row['source_run_id']),
                   '工程核价记录来源无效。')
            rows.append((path, row))
        return rows

    def _no_pending_pricing(self, batch_id):
        demand(not any(row['status'] == 'prepared' for _, row in self._pricing_records(batch_id)),
               '上一笔工程核价正在恢复，请稍后刷新后重试。', 'pricing_pending', 409)
        self.assistant.no_prepared(batch_id)
        self.cost_sheet.no_prepared(batch_id)

    @staticmethod
    def _write_pricing_snapshot(path, value):
        """Complete a prepared transaction without replacing any saved snapshot."""
        demand(not path.parent.is_symlink() and not path.is_symlink(), '核价快照位置无效。')
        if path.exists():
            demand(path.is_file() and path.stat().st_size <= MAX_PRICING_BYTES
                   and digest(json.loads(path.read_text(encoding='utf-8'))) == digest(value),
                   '核价快照已存在且内容不一致。', 'source_conflict', 409)
        else:
            atomic_write(path, _json(value))

    def _commit_engineering_pricing(self, state, path, record):
        run_id = record['run_id']
        existing = next((run for run in state['runs'] if run['id'] == run_id), None)
        if record['status'] == 'prepared':
            result, review, recognition = (record.get(key) for key in ('result', 'review', 'recognition'))
            demand(isinstance(result, dict) and isinstance(review, dict) and isinstance(recognition, dict)
                   and result.get('batch_id') == state['id'] and result.get('run_id') == run_id
                   and result.get('source_sha256') == state['source_sha256']
                   and review.get('run_id') == recognition.get('run_id') == run_id
                   and review.get('source_digest') == recognition.get('source_digest') == digest(result),
                   '待恢复核价快照来源不一致。', 'source_conflict', 409)
            if existing is None:
                demand(state['status'] not in BUSY and state['revision'] == record['base_revision']
                       and (state.get('result') or {}).get('run_id') == record['source_run_id'],
                       '原报价已更新，不能恢复到另一笔报价。', 'revision_conflict', 409)
                _, _, current_recognition, binding, _ = self._engineering_pricing_context(state, record['source_run_id'])
                demand(binding == record['source_binding'] and current_recognition['status'] not in BUSY,
                       '工程资料已更新，请重新核价。', 'source_conflict', 409)
                if record.get('kind') == 'process_estimate':
                    self.process_pricing.validate_prepared(state, record)
            directory = self._folder(state['id']) / 'runs'
            self._write_pricing_snapshot(directory / (run_id + '.json'), result)
            self._write_pricing_snapshot(directory / (run_id + '-engineering.json'), review)
            self._write_pricing_snapshot(directory / (run_id + '-engineering-recognition.json'), recognition)
            if existing is None:
                state['settings'] = copy.deepcopy(result['settings'])
                self._complete(state, result, result['source_files'])
            # The journal is durable before the result pointer is committed. A
            # lost response or restart can finish that same run, never mint two.
            record = {key: value for key, value in record.items() if key not in {'result', 'review', 'recognition'}}
            record.update(status='committed', committed_at=stamp())
            atomic_write(path, _json(record))
        demand(any(run['id'] == run_id for run in state['runs']), '核价提交记录与报价历史不一致。', 'source_conflict', 409)
        saved = self._preview_run_locked(state, run_id)
        demand(digest(saved) == record['result_digest'], '核价提交记录与快照不一致。', 'source_conflict', 409)
        return record

    def price_engineering(self, batch_id, run_id, payload):
        """Price known source requirements into a new immutable pending run."""
        from batch_engineering_pricing import reprice_quote
        only_keys(payload, {'event_id', 'expected_revision', 'policy'}, 'engineering pricing')
        event = self._event(payload.get('event_id'))
        demand(type(payload.get('expected_revision')) is int and payload['expected_revision'] >= 0,
               '请重新读取报价版本。')
        demand(isinstance(payload.get('policy'), dict), '请提交本次工程核价规则。')
        demand(isinstance(run_id, str) and RUN_ID.fullmatch(run_id), '报价运行编号无效。')
        signature = digest([run_id, payload])
        with self._lock(batch_id):
            state = self._read(batch_id)
            records = self._pricing_records(batch_id)
            previous = next(((path, row) for path, row in records if row['event_id'] == event), None)
            if previous is not None:
                path, record = previous
                demand(record.get('signature') == signature, '相同核价请求的内容已变化。', 'event_conflict', 409)
                record = self._commit_engineering_pricing(state, path, record)
                return {'batch': self._public(state), 'run_id': record['run_id'],
                        'source_run_id': record['source_run_id'], 'replayed': True}
            self._no_pending_pricing(batch_id)
            demand(not isinstance((state.get('result') or {}).get('cost_sheet'), dict),
                   '该版本已采用内部核价单，请返回核价单调整并另存新版本。', 'cost_sheet_required', 409)
            demand(len(records) < 100 and len(state['runs']) < 100, '本任务核价历史过多，请建立新的报价任务。')
            demand(state['status'] not in BUSY, '当前任务仍在后台处理。', 'already_running', 409)
            demand(payload['expected_revision'] == state['revision'], '任务进度已更新，请刷新后重试。', 'revision_conflict', 409)
            demand((state.get('result') or {}).get('run_id') == run_id,
                   '已有更新的报价，请从当前报价重新核价。', 'source_conflict', 409)
            result, review, recognition, binding, manifest = self._engineering_pricing_context(state, run_id)
            demand(digest(state['result']) == digest(result), '当前报价与保存快照不一致。', 'source_conflict', 409)
            demand(recognition['status'] not in BUSY, '工程识别仍在后台处理，请完成后再核价。', 'already_running', 409)
            policy = copy.deepcopy(payload['policy'])
            demand(policy.pop('source_binding', None) == binding, '工程资料已更新，请重新读取核价规则。', 'source_conflict', 409)
            new_result = reprice_quote(result, manifest, policy)
            new_id, now = 'RUN-' + uuid.uuid4().hex, stamp()
            prefix = self.API_PREFIX + '/' + batch_id + '/runs/' + new_id + '/'
            new_result.update(batch_id=batch_id, run_id=new_id, created_at=now, status='pending_review', simulated=True,
                              links={'quote_url': prefix + 'quote.html', 'bom_url': prefix + 'bom.csv',
                                     'cost_url': prefix + 'cost.csv', 'json_url': prefix + 'quote.json'})
            provenance = {'source_run_id': run_id, 'source_quote_digest': binding['source_digest'],
                          'source_review_revision': review['revision'], 'source_recognition_id': recognition.get('id'),
                          'source_recognition_digest': binding['recognition_digest'], 'pricing_event_id': event}
            new_result.setdefault('engineering_repricing', {}).update(provenance)
            identity = digest(new_result)
            new_review = {'schema_version': 1, 'run_id': new_id, 'source_digest': identity, 'revision': 0,
                          'header': copy.deepcopy(review['header']), 'lines': copy.deepcopy(review['lines']),
                          'saved_at': now, 'status': 'pending_review', 'quotation_unchanged': True,
                          '_events': {}, 'history': [], 'parent': provenance}
            new_recognition = {'id': 'ENG-' + uuid.uuid4().hex, 'run_id': new_id, 'source_digest': identity,
                               'status': 'completed', 'stage': 'completed', 'started_at': None, 'updated_at': now,
                               'message': '沿用已读取的工程资料，新的模拟核价仍待审核。',
                               'native': copy.deepcopy(recognition.get('native') or result.get('engineering_native')),
                               'ai': copy.deepcopy(recognition.get('ai')), 'error': recognition.get('error'),
                               'partial': bool(recognition.get('partial')), '_events': [], 'parent': provenance}
            demand(len(_json(new_result).encode('utf-8')) <= 64 * 1024 * 1024
                   and len(_json(new_review).encode('utf-8')) <= 4 * 1024 * 1024
                   and len(_json(new_recognition).encode('utf-8')) <= 32 * 1024 * 1024,
                   '核价快照过大，请拆分订单。')
            record = {'schema_version': 1, 'batch_id': batch_id, 'event_id': event, 'signature': signature,
                      'status': 'prepared', 'run_id': new_id, 'source_run_id': run_id, 'base_revision': state['revision'],
                      'source_binding': binding, 'created_at': now, 'result_digest': identity,
                      'result': new_result, 'review': new_review, 'recognition': new_recognition}
            content = _json(record)
            demand(len(content.encode('utf-8')) <= MAX_PRICING_BYTES, '本次核价记录过大，请拆分订单。')
            directory = self._pricing_directory(batch_id, create=True)
            path = directory / (hashlib.sha256(event.encode()).hexdigest() + '.json')
            demand(not path.exists() and not path.is_symlink(), '核价请求编号已存在。', 'event_conflict', 409)
            atomic_write(path, content)
            self._commit_engineering_pricing(state, path, record)
            return {'batch': self._public(state), 'run_id': new_id, 'source_run_id': run_id, 'replayed': False}

    def _preview_file(self, batch_id, state, result, file_id, *, preview_index=False):
        demand(isinstance(file_id, str) and FILE_ID.fullmatch(file_id), '报价物料没有有效源文件。')
        originals = [row for row in state['files'] if row['id'] == file_id]
        sources = [row for row in result['source_files'] if row.get('id') == file_id]
        demand(len(originals) == len(sources) == 1, '报价物料来源有歧义。')
        original, source = originals[0], sources[0]
        sha = source.get('sha256')
        demand(source.get('kind') == 'step' and source.get('status') == 'ready' and not source.get('duplicate_of')
               and isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{64}', sha)
               and sha == original.get('sha256'), '本轮源文件尚无可关联的原生几何。')
        self.file(batch_id, file_id)
        directory = self._folder(batch_id) / 'processed'
        path = directory / (file_id + '.json')
        demand(not directory.is_symlink() and path.is_file() and not path.is_symlink()
               and path.stat().st_size <= 32 * 1024 * 1024, '原生几何关联记录不可用。')
        receipt = json.loads(path.read_text(encoding='utf-8'))
        demand(isinstance(receipt, dict) and receipt.get('sha256') == sha, '原生几何关联来源不一致。')
        cached = receipt.get('geometry')
        demand(cached is None or isinstance(cached, dict) and cached.get('source_sha256') in {None, sha},
               '缓存几何来源不一致。')
        demand(self.cad is not None and self.manufacturing is not None, '没有现成的原生显示几何。')
        # Existing content-derived CAD identity is the only legacy fallback.
        expected_id = 'CAD-' + sha[:32]
        native_id = receipt.get('native_id', expected_id)
        demand(isinstance(native_id, str) and NATIVE_ID.fullmatch(native_id)
               and native_id == expected_id, '原生文件关联编号不一致。')
        document = self.cad.get(native_id)
        demand(document.get('id') == native_id and document.get('sha256') == sha, '原生文件身份不一致。')
        index_reader = getattr(self.manufacturing, 'preview_index', None)
        if preview_index and callable(index_reader):
            geometry = index_reader(native_id)
            demand(geometry.get('source_sha256') == sha and geometry.get('output_unit') == 'mm',
                   '原生预览索引与上传原件不一致。')
        else:
            geometry = validate_geometry(self.manufacturing.geometry(native_id), sha)
        return {'native_id': native_id, 'source_sha256': sha, 'geometry': geometry}

    @staticmethod
    def _preview_match(line, geometry, siblings):
        definition = line.get('native_definition')
        demand(isinstance(definition, str) and definition and not definition.startswith('@unresolved-')
               and len(definition) <= 512, '该报价行没有确定的原生零件定义。')
        demand(sum(sibling.get('native_definition') == definition for sibling in siblings) == 1,
               '报价行的原生分组存在歧义。')
        solids = [solid for solid in geometry['solids'] if isinstance(solid.get('definition_id'), str)
                  and re.sub(r'/solid-[0-9]+$', '', solid['definition_id']) == definition]
        demand(solids and type(line.get('native_solid_count')) is int
               and len(solids) == line['native_solid_count'], '当前实体数量与原报价分组不一致。')
        with localcontext() as context:
            context.prec = 64
            quantity, volume, _ = _quantity_and_volume(solids)
            demand(quantity is not None and quantity == line.get('quantity_per_set') and volume is not None
                   and _weight(volume) == line.get('volume_mm3'), '装配实例或体积与原报价分组不一致。')
        paths = sorted({tuple(solid['occurrence_path']) for solid in solids})
        occurrences = [{'id': 'O' + str(index).zfill(4),
                        'solid_ids': sorted(solid['id'] for solid in solids if tuple(solid['occurrence_path']) == path)}
                       for index, path in enumerate(paths, 1)]
        representative = sorted((solid for solid in solids if tuple(solid['occurrence_path']) == paths[0]),
                                key=lambda solid: solid['id'])
        bounds = {'min': [min(solid['bounds_mm']['min'][axis] for solid in representative) for axis in range(3)],
                  'max': [max(solid['bounds_mm']['max'][axis] for solid in representative) for axis in range(3)]}
        bounds['size'] = [hi - lo for lo, hi in zip(bounds['min'], bounds['max'])]
        return {'solid_ids': [solid['id'] for solid in solids],
                'representative_solid_id': representative[0]['id'], 'occurrences': occurrences,
                'representative_solid_ids': [solid['id'] for solid in representative], 'bounds_mm': bounds,
                'thumbnail': representative[0].get('thumbnail')}

    def _preview_context(self, batch_id, run_id, line_id, *, preview_index=False):
        state, result = self._preview_run(batch_id, run_id)
        selected = next((item for item in result['lines'] if item['id'] == line_id), None)
        demand(selected is not None, '报价物料不存在。', 'not_found', 404)
        files, matches = {}, {}
        for line in result['lines']:
            file_id = line.get('file_id')
            if file_id != selected.get('file_id'):
                continue
            if not isinstance(file_id, str):
                matches[line['id']] = None
                continue
            if file_id not in files:
                try:
                    files[file_id] = (self._preview_file(batch_id, state, result, file_id, preview_index=True)
                                      if preview_index else self._preview_file(batch_id, state, result, file_id))
                except (ValueError, OSError, TypeError, KeyError):
                    files[file_id] = None
            context = files.get(file_id)
            match = None
            if context:
                try:
                    siblings = [item for item in result['lines'] if item.get('file_id') == file_id]
                    match = self._preview_match(line, context['geometry'], siblings)
                except (ValueError, TypeError, KeyError):
                    pass
            matches[line['id']] = match
        return result, files, matches

    def _preview_thumbnail(self, context, match):
        filename = match.get('thumbnail')
        demand(filename == match['representative_solid_id'] + '.png', '该零件没有已登记的原生缩略图。', 'not_found', 404)
        binding = context['geometry'].get('preview_binding')
        content, mime = (self.manufacturing.file(context['native_id'], filename, expected_geometry=binding)
                         if binding is not None else self.manufacturing.file(context['native_id'], filename))
        demand(mime == 'image/png' and isinstance(content, bytes) and content.startswith(b'\x89PNG\r\n\x1a\n'),
               '原生缩略图不可用。', 'not_found', 404)
        return content, mime

    def parts_manifest(self, batch_id, run_id):
        state, result = self._preview_run(batch_id, run_id)
        prefix = self.API_PREFIX + '/' + batch_id + '/runs/' + run_id + '/parts/'
        files, rows = {}, {}
        for line in result['lines']:
            fid = line.get('file_id')
            files.setdefault(fid if isinstance(fid, str) else None, []).append(line)
        # At most one source geometry remains in memory while enumerating a ZIP.
        for file_id, siblings in files.items():
            context = None
            try:
                context = self._preview_file(batch_id, state, result, file_id)
            except (ValueError, OSError, TypeError, KeyError):
                pass
            for line in siblings:
                match = None
                if context:
                    try:
                        match = self._preview_match(line, context['geometry'], siblings)
                    except (ValueError, TypeError, KeyError):
                        pass
                geometry_url = prefix + line['id'] if match else None
                row = {'line_id': line['id'], 'file_id': line.get('file_id'),
                       'status': 'ready' if match else 'unavailable',
                       'reason': '原生几何已关联，报价金额沿用原快照。' if match else '原生几何缺失、未通过来源或分组校验，暂不可预览。',
                       'thumbnail_url': None, 'geometry_url': geometry_url,
                       'viewer_url': '/workbench/quotation/part/?preview=' + quote(geometry_url, safe='') if match else None,
                       'solid_ids': match['solid_ids'] if match else [],
                       'representative_solid_id': match['representative_solid_id'] if match else None,
                       'representative_solid_ids': match['representative_solid_ids'] if match else [],
                       'bounds_mm': match['bounds_mm'] if match else None}
                if match:
                    try:
                        self._preview_thumbnail(context, match)
                        row['thumbnail_url'] = prefix + line['id'] + '/thumbnail'
                    except (ValueError, OSError, TypeError, KeyError):
                        pass
                rows[line['id']] = row
        return {'batch_id': batch_id, 'run_id': run_id, 'lines': [rows[line['id']] for line in result['lines']]}

    def part_geometry(self, batch_id, run_id, line_id):
        demand(isinstance(line_id, str) and LINE_ID.fullmatch(line_id), '报价物料编号无效。')
        result, files, matches = self._preview_context(batch_id, run_id, line_id)
        line = next((item for item in result['lines'] if item['id'] == line_id), None)
        demand(line is not None, '报价物料不存在。', 'not_found', 404)
        match = matches[line_id]
        demand(match is not None, '该报价物料暂不可预览。', 'not_found', 404)
        context = files[line['file_id']]
        geometry = context['geometry']
        public_solids = []
        for solid in geometry['solids']:
            public_solids.append({**{key: solid[key] for key in ('id', 'name', 'volume_mm3', 'area_mm2')},
                'mesh': {key: copy.deepcopy(solid['mesh'][key]) for key in ('positions', 'indices')},
                'bounds_mm': {key: copy.deepcopy(solid['bounds_mm'][key]) for key in ('min', 'max', 'size')},
                'topology': {key: solid['topology'][key] for key in ('faces', 'edges', 'vertices', 'valid', 'closed')}})
        public_geometry = {'schema_version': 1, 'source_sha256': context['source_sha256'], 'output_unit': 'mm',
                           'solids': public_solids}
        groups = [{'id': item['id'], 'name': item['part_name'],
                   'solid_ids': matches[item['id']]['solid_ids'] if matches[item['id']] else []}
                  for item in result['lines'] if item.get('file_id') == line['file_id']]
        scalar_fields = ('id', 'file_id', 'part_name', 'document', 'mode', 'quantity_per_set', 'quantity_total',
            'material_raw', 'material_assumption', 'material_profile', 'material_estimation_label', 'material_price_source_kind',
            'density_kg_m3', 'volume_mm3', 'confidence', 'weight_basis',
            'estimated_net_weight_kg', 'estimated_set_net_weight_kg', 'estimated_total_net_weight_kg',
            'material_usage_kg', 'material_rate', 'material_cost', 'process_cost', 'purchase_cost', 'setup_cost',
            'unit_cost', 'unit_price', 'total_cost', 'total_price', 'priced')
        snapshot = {key: value for key in scalar_fields if (value := line.get(key)) is None or type(value) in {str, int, float, bool}}
        settings = {key: result['settings'][key] for key in ('order_sets', 'pricing_method', 'rate_percent', 'material_profile', 'rate_profile')}
        return {'batch_id': batch_id, 'run_id': run_id, 'line_id': line_id, 'part_name': line['part_name'],
                'source_sha256': context['source_sha256'], 'geometry': public_geometry, 'groups': groups,
                'representative_solid_id': match['representative_solid_id'], 'representative_solid_ids': match['representative_solid_ids'],
                'occurrences': match['occurrences'], 'selected_id': line_id, 'line_snapshot': snapshot,
                'settings': settings, 'order_sets': settings['order_sets']}

    def customer_part_geometry(self, batch_id, run_id, line_id):
        """Technical customer projection over the same validated cached part."""
        from batch_customer_quote import customer_part_projection
        return customer_part_projection(self.part_geometry(batch_id, run_id, line_id))

    def part_thumbnail(self, batch_id, run_id, line_id):
        demand(isinstance(line_id, str) and LINE_ID.fullmatch(line_id), '报价物料编号无效。')
        result, files, matches = self._preview_context(batch_id, run_id, line_id, preview_index=True)
        line = next((item for item in result['lines'] if item['id'] == line_id), None)
        demand(line is not None and matches[line_id] is not None, '报价物料缩略图不可用。', 'not_found', 404)
        try:
            content, mime = self._preview_thumbnail(files[line['file_id']], matches[line_id])
        except (ValueError, OSError, TypeError, KeyError) as error:
            raise WorkbenchError('报价物料没有可用的原生缩略图。', 'not_found', 404) from error
        return content, mime, line_id + '.png'

    def start_worker(self):
        self.engineering_recognition.start_worker()
        self.assistant.start_worker()
        self.cost_sheet.start_worker()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='batch-quote-worker')
        self._thread.start()

    def close(self):
        self._stop.set()
        self.assistant.close()
        self.cost_sheet.close()
        self.engineering_recognition.close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _recover(self):
        for folder in self.root.iterdir():
            if not BATCH_ID.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
                continue
            with self._lock(folder.name):
                state = self._read(folder.name)
                for path, record in self._pricing_records(folder.name):
                    if record['status'] == 'prepared':
                        self._commit_engineering_pricing(state, path, record)
                self.cost_sheet.recover_locked(state)
                self.assistant.recover_locked(state)
                if state['status'] == 'running':
                    state['status'] = 'queued'
                    for row in state['files']:
                        if row['status'] == 'processing':
                            row['status'] = 'waiting'
                    self._progress(state, '服务已恢复，继续本次后台报价。')
                    state['revision'] += 1
                    self._save(state)

    @contextmanager
    def _worker_lease(self):
        fd = os.open(self.root / '.worker.lock', os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'r+') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True

    def _loop(self):
        while not self._stop.is_set():
            try:
                with self._worker_lease() as acquired:
                    if acquired:
                        self._recover()
                        while not self._stop.is_set():
                            if not self._run_next():
                                self._stop.wait(1)
                    else:
                        self._stop.wait(1)
            except Exception:
                # User-visible failure state is saved per batch, not printed with paths.
                self._stop.wait(1)

    def process_pending_once(self):
        """Synchronous worker seam for isolated server tests; not an HTTP action."""
        with self._worker_lease() as acquired:
            if not acquired:
                return False
            self._recover()
            return self._run_next()

    def _run_next(self):
        queued = []
        for folder in self.root.iterdir():
            if not BATCH_ID.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
                continue
            with self._lock(folder.name):
                state = self._read(folder.name)
                if state['status'] == 'queued':
                    queued.append((state['updated_at'], state['id']))
        if not queued:
            return False
        batch_id = min(queued)[1]
        with self._lock(batch_id):
            state = self._read(batch_id)
            state['status'] = 'running'
            state['revision'] += 1
            self._save(state)
        try:
            self._execute(batch_id)
        except _Interrupted:
            with self._lock(batch_id):
                state = self._read(batch_id)
                state['status'] = 'queued'
                for row in state['files']:
                    if row['status'] == 'processing':
                        row['status'] = 'waiting'
                self._progress(state, '本轮已保存，服务恢复后继续。')
                state['revision'] += 1
                self._save(state)
        except Exception:
            with self._lock(batch_id):
                state = self._read(batch_id)
                state.update(status='failed', error='本轮报价整理未完成；已保存资料和已有报价保留，可重新开始。')
                state['revision'] += 1
                self._save(state)
        return True

    def _native(self, row, content):
        if self.processor is not None:
            return self.processor(copy.deepcopy(row), content)
        demand(getattr(self.manufacturing, 'analyzer', None) is None, '批量报价原生服务配置已变化。')
        document = self.cad.create(row['name'], content)
        state = self.manufacturing.get(document['id'])
        if state['status'] not in {'ready', 'queued', 'running'}:
            state = self.manufacturing.start(document['id'], {'expected_revision': state['revision'], 'retry': state['status'] == 'failed'})
        started = time.monotonic()
        while state['status'] in {'queued', 'running'}:
            if self._stop.wait(self.poll_seconds):
                raise _Interrupted()
            demand(time.monotonic() - started <= self.file_timeout, '原生 CAD 读取超时，请稍后重试。')
            state = self.manufacturing.get(document['id'])
        demand(state['status'] == 'ready', '原生 CAD 读取未完成，请检查文件或引擎后重试。')
        geometry = self.manufacturing.geometry(document['id'])
        return geometry, document['id']

    @staticmethod
    def _measurements(geometry):
        """Copy scalar quotation evidence, never accumulate display meshes."""
        from batch_process_evidence import summarise_native_processes
        demand(isinstance(geometry, dict) and isinstance(geometry.get('solids'), list), '原生 CAD 没有返回可用实体。')
        solids = []
        for original in geometry['solids']:
            demand(isinstance(original, dict), '原生 CAD 实体记录无效。')
            row = {key: copy.deepcopy(original.get(key)) for key in
                   ('id', 'name', 'definition_id', 'occurrence_path', 'volume_mm3', 'area_mm2', 'material', 'thickness', 'bounds_mm')}
            topology = original.get('topology', {})
            row['topology'] = {key: topology.get(key) for key in ('valid', 'closed', 'faces')} if isinstance(topology, dict) else {}
            row['process_evidence'] = summarise_native_processes(original)
            if isinstance(original.get('quote_quantities'), dict):
                from batch_process_estimate import validated_quantities
                quantities = validated_quantities(original, geometry.get('source_sha256'))
                demand(quantities is not None, '原生报价工程量的实体或来源校验未通过。')
                row['quote_quantities'] = quantities
            solids.append(row)
        return {'source_sha256': geometry.get('source_sha256'), 'output_unit': geometry.get('output_unit'), 'solids': solids}

    def _execute(self, batch_id):
        with self._lock(batch_id):
            initial = self._read(batch_id)
            job = copy.deepcopy(initial['_job'])
        folder = self._folder(batch_id)
        snapshot = folder / 'runs' / (job['run_id'] + '.json')
        if snapshot.is_file() and not snapshot.is_symlink():
            # A process can stop between saving the immutable snapshot and
            # updating its public task pointer. Commit that same run once.
            recovered = json.loads(snapshot.read_text(encoding='utf-8'))
            demand(recovered.get('batch_id') == batch_id and recovered.get('run_id') == job['run_id']
                   and recovered.get('settings') == job['settings']
                   and recovered.get('source_sha256') == initial['source_sha256'], '报价快照与恢复任务不一致。')
            with self._lock(batch_id):
                self._complete(self._read(batch_id), recovered, recovered['source_files'])
            self._enqueue_engineering(batch_id, recovered)
            return
        (folder / 'processed').mkdir(mode=0o700, exist_ok=True)
        for fid in job['file_ids']:
            if self._stop.is_set():
                raise _Interrupted()
            with self._lock(batch_id):
                state = self._read(batch_id)
                row = next(row for row in state['files'] if row['id'] == fid)
                if row['status'] == 'ready' and (folder / 'processed' / (fid + '.json')).is_file():
                    continue
                row.update(status='processing', error=None)
                self._progress(state, '正在读取 STEP 并计算报价依据。', row['relative_path'])
                state['revision'] += 1
                self._save(state)
                current = copy.deepcopy(row)
            try:
                content, _, _ = self.file(batch_id, fid)
                demand(content, 'STEP 文件为空。')
                processed = self._native(current, content)
                if isinstance(processed, tuple):
                    raw_geometry, native_id = processed
                    receipt = {'native_id': native_id}
                else:
                    raw_geometry = processed
                    receipt = {}
                geometry = self._measurements(raw_geometry)
                del processed, raw_geometry
                if 'native_id' not in receipt:
                    receipt['geometry'] = geometry
                demand(geometry.get('source_sha256') in {None, current['sha256']}, 'CAD 结果与原件不一致。')
                receipt['sha256'] = current['sha256']
                atomic_write(folder / 'processed' / (fid + '.json'), _json(receipt))
                values = {'status': 'ready', 'error': None, 'solid_count': len(geometry['solids'])}
            except _Interrupted:
                raise
            except Exception as error:
                message = '原生 CAD 读取未完成，请检查文件或引擎后重试。'
                if isinstance(error, WorkbenchError) and '超时' in str(error):
                    message = '原生 CAD 读取超时，可单独重试此文件。'
                elif isinstance(error, WorkbenchError) and 'STEP 格式' in str(error):
                    message = '文件内容不是可识别的 STEP 格式，请检查原件。'
                elif isinstance(error, WorkbenchError) and 'STEP 文件为空' in str(error):
                    message = 'STEP 文件为空，请补充有效原件。'
                values = {'status': 'failed', 'error': message, 'solid_count': None}
            with self._lock(batch_id):
                state = self._read(batch_id)
                next(row for row in state['files'] if row['id'] == fid).update(values)
                self._progress(state, '已保存单文件处理结果。')
                state['revision'] += 1
                self._save(state)
        documents = []
        with self._lock(batch_id):
            state = self._read(batch_id)
            sources = copy.deepcopy(state['files'])
        for row in sources:
            if row.get('duplicate_of'):
                continue
            document = copy.deepcopy(row)
            if row['kind'] == 'step' and row['status'] == 'ready':
                path = folder / 'processed' / (row['id'] + '.json')
                demand(path.is_file() and not path.is_symlink(), '原生 CAD 结果记录缺失。')
                receipt = json.loads(path.read_text())
                demand(receipt['sha256'] == row['sha256'], '原生 CAD 结果来源不一致。')
                document['geometry'] = self._measurements(receipt['geometry'] if 'geometry' in receipt else
                                                         self.manufacturing.geometry(receipt['native_id']))
            documents.append(document)
        result = calculate_batch(documents, job['settings'])
        prefix = self.API_PREFIX + '/' + batch_id + '/runs/' + job['run_id'] + '/'
        links = {'quote_url': prefix + 'quote.html', 'bom_url': prefix + 'bom.csv',
                 'cost_url': prefix + 'cost.csv', 'json_url': prefix + 'quote.json'}
        result.update(batch_id=batch_id, run_id=job['run_id'], name=initial['name'], created_at=stamp(),
                      settings=copy.deepcopy(job['settings']), source_sha256=initial['source_sha256'],
                      source_files=sources, links=links,
                      source_scope='每份不重复的 STEP 根装配暂按每套各 1 份；同名或跨文件装配关系仍需工程核对。')
        from batch_engineering_native import enrich_native
        result['engineering_native'] = enrich_native(result, {row['id']: row.get('geometry') for row in documents if row.get('geometry')})
        (folder / 'runs').mkdir(mode=0o700, exist_ok=True)
        path = folder / 'runs' / (job['run_id'] + '.json')
        demand(not path.exists(), '报价运行编号已使用，不覆盖历史报价。')
        atomic_write(path, _json(result))
        with self._lock(batch_id):
            state = self._read(batch_id)
            self._complete(state, result, sources)
        self._enqueue_engineering(batch_id, result)

    def _enqueue_engineering(self, batch_id, result):
        """Admit engineering work for the selected runtime, including SDK mode.

        A legacy RPC URL is not an SDK capability. Manually injected readers
        retain their explicit recognition path without enabling new auto work.
        """
        if not 1 <= len(result.get('lines', [])) <= 1000:
            return
        recognition = self.engineering_recognition
        from agent_bridge import is_configured
        if not is_configured(recognition.rpc_url):
            return
        run_id = result['run_id']
        recognition.enqueue(batch_id, run_id, {'event_id': 'auto-engineering-' + run_id})

    def _complete(self, state, result, sources):
        state['result'] = result
        has_issues = any(row['kind'] == 'step' and row['status'] == 'failed' for row in sources)
        state['status'] = 'completed_with_issues' if has_issues or result['totals'].get('unpriced_lines', 0) else 'completed'
        state['error'] = None
        if not any(run['id'] == result['run_id'] for run in state['runs']):
            state['runs'].append({'id': result['run_id'], 'created_at': result['created_at'], 'links': result['links'],
                                  'total_price': result['totals'].get('total_price'), 'status': 'pending_review'})
        state['_job'] = None
        self._progress(state, '报价草稿已生成，待人工审核。')
        state['revision'] += 1
        self._save(state)
