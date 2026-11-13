"""Durable, serial engineering recognition beside immutable quote snapshots."""
from __future__ import annotations

import copy
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from prep_record import atomic_write, digest, stamp
from workbench_store import WorkbenchError
from batch_engineering_native import enrich_native

FIELDS = ('specification', 'thickness_mm', 'material_grade', 'surface_treatment')
ACTIVE = {'queued', 'running'}
RUN_FILE = re.compile(r'(RUN-[a-f0-9]{32})-engineering-recognition\.json\Z')
LABELS = {'candidate': '识别候选', 'not_stated': '原文件未标注', 'not_found': '未识别到',
          'unreadable': '标注不清', 'conflict': '信息冲突', 'unresolved': '未获得可靠结果', 'not_applicable': '不适用'}


def recognition_fields(result, record):
    native = record.get('native') or result.get('engineering_native') or {}
    ai = record.get('ai') or {}
    rows = {}
    for line in result['lines']:
        identity = line['id']
        measured = native.get('lines', {}).get(identity, {})
        observed = ai.get('lines', {}).get(identity, {})
        fields = {}
        for field in FIELDS:
            value = measured.get('values', {}).get(field)
            state = measured.get('field_status', {}).get(field, 'not_found')
            evidence = measured.get('evidence', {}).get(field)
            source = 'cad_property' if field == 'material_grade' else 'native_geometry'
            reason = evidence.get('reason') if isinstance(evidence, dict) else None
            candidate = observed.get('values', {}).get(field)
            ai_state = observed.get('field_states', {}).get(field)
            if candidate is not None and ai_state == 'candidate':
                equivalent = str(candidate).strip().casefold() == str(value).strip().casefold()
                if field == 'thickness_mm' and value is not None:
                    try:
                        equivalent = abs(Decimal(str(candidate)) - Decimal(str(value))) <= Decimal('.001')
                    except Exception:
                        equivalent = False
                if value is not None and field == 'specification' and not equivalent:
                    value, state, source = value + '；图纸规格：' + candidate, 'candidate', 'native_and_drawing'
                    evidence = {'native': evidence, 'drawing': observed.get('evidence', {}).get(field)}
                    reason = '成品包络由 CAD 量测，附加规格来自图纸原文，两者分别保留。'
                elif value is not None and field != 'specification' and not equivalent:
                    value, state, source = None, 'conflict', 'multiple_sources'
                    evidence = {'native': evidence, 'drawing': observed.get('evidence', {}).get(field)}
                    reason = 'CAD 属性或量测与图纸原文不一致，请核对。'
                else:
                    value, state, source = candidate, 'candidate', 'ai_document'
                    evidence = observed.get('evidence', {}).get(field)
                    reason = 'AI 读取当前附件原文，待工程核对。'
            elif ai_state == 'conflict':
                value, state, source = None, 'conflict', 'ai_document'
                evidence = observed.get('evidence', {}).get(field)
                reason = '附件原文存在冲突，尚未选用。'
            elif value is None and ai_state and not (field == 'thickness_mm' and evidence and state in {'unresolved', 'not_applicable'}):
                state, source = ai_state, 'ai_document'
                reason = '已读取的资料未给出该标注。' if state == 'not_stated' else '本轮未获得该字段的可靠原文。'
            label = LABELS.get(state, '未识别到')
            if field == 'thickness_mm' and state == 'unresolved':
                label = '板厚分析未完成' if '预算' in (reason or '') else '局部厚度待核对' if '局部' in (reason or '') else '未取得可靠板/壁厚'
            elif field == 'material_grade' and state == 'unresolved':
                label = 'CAD 未提供材料'
            fields[field] = {'value': value, 'status': state, 'source': source,
                             'label': ('CAD 量测 + AI 标注' if source == 'native_and_drawing' else 'AI 识别' if source == 'ai_document' else 'CAD 属性' if source == 'cad_property' else 'CAD 量测')
                                      if state == 'candidate' else label,
                             'evidence': evidence, 'reason': reason}
        rows[identity] = {'fields': fields}
    return rows


def recognition_stats(result, record):
    rows = recognition_fields(result, record)
    return {field: sum(row['fields'][field]['status'] == 'candidate' and row['fields'][field]['value'] is not None
                       for row in rows.values()) for field in FIELDS}


class EngineeringRecognition:
    def __init__(self, service, *, reader_root=None, rpc_url=None, ai_reader=None, geometry_loader=None):
        self.service = service
        self.reader_root = Path(reader_root) if reader_root else service.root / '.engineering-readers'
        self.rpc_url, self.ai_reader, self.geometry_loader = rpc_url, ai_reader, geometry_loader
        self._thread = None
        self._stop = threading.Event()

    def configure(self, reader_root, rpc_url):
        self.reader_root, self.rpc_url = Path(reader_root), rpc_url

    def _path(self, batch_id, run_id):
        path = self.service._folder(batch_id) / 'runs' / (run_id + '-engineering-recognition.json')
        if not RUN_FILE.fullmatch(path.name) or path.parent.is_symlink() or path.is_symlink():
            raise WorkbenchError('工程识别记录位置无效。')
        return path

    def read(self, batch_id, run_id, result):
        path = self._path(batch_id, run_id)
        if not path.exists():
            return {'id': None, 'status': 'not_started', 'stage': 'not_started',
                    'message': '可读取已有 CAD 量测，并用 AI 检查图纸与工艺说明。',
                    'source_digest': digest(result), 'native': result.get('engineering_native'), 'ai': None}
        if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise WorkbenchError('工程识别记录无效。')
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('source_digest') != digest(result) or record.get('run_id') != run_id:
            raise WorkbenchError('工程识别与本次报价来源不一致。', 'source_conflict', 409)
        return record

    @staticmethod
    def public(result, record):
        return {**{key: copy.deepcopy(record.get(key)) for key in
                   ('id', 'status', 'stage', 'message', 'started_at', 'updated_at', 'error', 'partial')},
                'stats': recognition_stats(result, record)}

    def _save(self, batch_id, run_id, record):
        record['updated_at'] = stamp()
        atomic_write(self._path(batch_id, run_id), json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + '\n')

    def enqueue(self, batch_id, run_id, payload):
        if not isinstance(payload, dict) or set(payload) != {'event_id'}:
            raise WorkbenchError('工程识别请求无效。')
        event = self.service._event(payload['event_id'])
        _, result = self.service._preview_run(batch_id, run_id)
        if not 1 <= len(result['lines']) <= 1000:
            raise WorkbenchError('一次工程识别支持 1 至 1000 条物料，请将大订单分批处理。')
        with self.service._lock(batch_id):
            old = self.read(batch_id, run_id, result)
            if event in old.get('_events', []) or old['status'] in ACTIVE:
                return old
            events = old.get('_events', [])
            if len(events) >= 500:
                raise WorkbenchError('本次识别历史已满，请建立新的报价版本。')
            record = {'id': 'ENG-' + uuid.uuid4().hex, 'run_id': run_id, 'source_digest': digest(result),
                      'status': 'queued', 'stage': 'queued', 'message': '已排队，关闭页面后后台继续识别。',
                      'started_at': None, 'native': old.get('native'), 'ai': None, 'error': None,
                      '_events': events + [event], 'partial': False}
            self._save(batch_id, run_id, record)
            return record

    def _geometries(self, batch_id, state, result):
        if self.geometry_loader:
            return self.geometry_loader(batch_id, state, result)
        geometries = {}
        for source in result['source_files']:
            if source.get('kind') != 'step' or source.get('status') != 'ready' or source.get('duplicate_of'):
                continue
            if not isinstance(source.get('id'), str) or not re.fullmatch(r'F[0-9]{4}', source['id']):
                continue
            try:
                geometries[source['id']] = self.service._preview_file(batch_id, state, result, source['id'])
            except (WorkbenchError, ValueError, KeyError, OSError):
                directory = self.service._folder(batch_id) / 'processed'
                path = directory / (source['id'] + '.json')
                if directory.is_dir() and not directory.is_symlink() and path.is_file() and not path.is_symlink() and path.stat().st_size <= 32 * 1024 * 1024:
                    receipt = json.loads(path.read_text())
                    if receipt.get('sha256') == source.get('sha256') and isinstance(receipt.get('geometry'), dict):
                        geometries[source['id']] = receipt['geometry']
        return geometries

    def _sources(self, batch_id, state, folder):
        sources = []
        for row in state['files']:
            if row.get('kind') not in {'image', 'pdf'} or row.get('duplicate_of'):
                continue
            if len(sources) >= 32:
                raise WorkbenchError('工程图纸与工艺说明超过 32 份，请分批识别。')
            content, _, _ = self.service.file(batch_id, row['id'])
            target = folder / (row['id'] + Path(row['name']).suffix.lower())
            if target.is_symlink() or target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != row['sha256']:
                raise WorkbenchError('工程读图资料已变化。')
            if not target.exists():
                target.write_bytes(content)
            sources.append({'id': row['id'], 'path': str(target), 'sha256': row['sha256'],
                            'original_name': row.get('relative_path') or row['name'], 'kind': 'reference'})
        return sources

    def process_pending_once(self):
        fence = self.service.root / '.engineering-worker.lock'
        descriptor = os.open(fence, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(descriptor, 'r+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            for batch in sorted(self.service.root.glob('BATCH-*')):
                if batch.is_symlink() or not batch.is_dir():
                    continue
                for path in sorted((batch / 'runs').glob('RUN-*-engineering-recognition.json')):
                    match = RUN_FILE.fullmatch(path.name)
                    if path.is_symlink() or not match:
                        continue
                    run_id = match[1]
                    state, result = self.service._preview_run(batch.name, run_id)
                    with self.service._lock(batch.name):
                        record = self.read(batch.name, run_id, result)
                        if record['status'] not in ACTIVE:
                            continue
                        record.update(status='running', stage='native', message='正在读取原生 CAD 尺寸、板厚与材料属性。',
                                      started_at=record.get('started_at') or stamp())
                        self._save(batch.name, run_id, record)
                    try:
                        native = enrich_native(result, self._geometries(batch.name, state, result))
                        record.update(native=native, stage='reading', message='CAD 量测已保存，正在准备 AI 图纸与工艺说明识别。')
                        with self.service._lock(batch.name):
                            self._save(batch.name, run_id, record)
                        folder = self.reader_root / batch.name / run_id / record['id']
                        if any(p.is_symlink() for p in (folder, *folder.parents)):
                            raise WorkbenchError('工程读图目录不允许符号链接。')
                        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
                        sources = self._sources(batch.name, state, folder)
                        if sources:
                            if self.ai_reader is None and self.rpc_url is None:
                                raise WorkbenchError('工程 AI 模型连接尚未配置。')
                            from batch_engineering_ai import recognize
                            last_progress = [0.0]
                            def progress(message):
                                if time.monotonic() - last_progress[0] < 4:
                                    return
                                last_progress[0] = time.monotonic()
                                record.update(stage='reading', message=message)
                                with self.service._lock(batch.name):
                                    self._save(batch.name, run_id, record)
                            record['ai'] = (self.ai_reader or recognize)(folder, result['lines'], sources, native,
                                self.rpc_url, on_progress=progress)
                            message = 'CAD 与 AI 识别已完成，候选待核对；原报价未自动改价。'
                        else:
                            message = 'CAD 量测已完成；未附图纸或工艺说明，未从外观猜材料和表面处理。'
                        record.update(status='completed', stage='completed', message=message, partial=not bool(sources), error=None)
                    except Exception as error:
                        record.update(status='failed', stage='failed', partial=bool(record.get('native')),
                                      message='本轮 AI 识别未完成，已保存的 CAD 量测仍可查看。',
                                      error='模型或资料读取未完成，请重试；已保存的 CAD 量测仍可查看。',
                                      _diagnostic=type(error).__name__ + ': ' + str(error)[:2000])
                    with self.service._lock(batch.name):
                        self._save(batch.name, run_id, record)
                    return True
        return False

    def start_worker(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def loop():
            while not self._stop.is_set():
                try:
                    worked = self.process_pending_once()
                except Exception:
                    worked = False
                if not worked:
                    self._stop.wait(1)
        self._thread = threading.Thread(target=loop, daemon=True, name='engineering-recognition-worker')
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
