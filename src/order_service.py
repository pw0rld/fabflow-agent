"""Order orchestration over existing single-part records and durable model jobs."""
from __future__ import annotations
from contextlib import ExitStack
import copy
import csv
import hashlib
import html
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from prep_record import RecordError, atomic_write, digest, identifier, number, only_keys, require, stamp
from workbench_store import WorkbenchError, FILE_TYPES, MAX_UPLOAD
from order_store import OrderStore, OrderConflict, demand, validate_structure
from order_jobs import JobQueue, LeaseLost
from order_discovery import DiscoveryBridge
from order_quotation import calculate, confirmed_route, pricing

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {'completed', 'failed', 'stopped'}


def short(value):
    return hashlib.sha256(value.encode()).hexdigest()[:28]


class OrderService:
    def __init__(self, app, *, discovery_factory=None, poll_seconds=2):
        self.app = app
        self.store = OrderStore(app.meta / 'orders.sqlite3')
        self.queue = JobQueue(app.meta / 'order-jobs.sqlite3')
        self.directory = app.meta / 'order-files'
        self.directory.mkdir(mode=0o700, exist_ok=True)
        require(not self.directory.is_symlink(), '订单资料目录无效。')
        self.discovery_factory = discovery_factory
        self.poll_seconds = poll_seconds
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name='order-worker')
        self._thread.start()

    def close(self):
        self._stop.set()

    def _loop(self):
        worker_id = 'worker-' + str(os.getpid()) + '-' + short(str(time.time()))
        while not self._stop.is_set():
            try:
                job = self.queue.claim(worker_id, lease_seconds=120)
                if job:
                    self._execute(job)
                    continue
            except Exception:
                # Failures remain attached to their job, never logged with input.
                pass
            self._stop.wait(1)

    def _mutate(self, order_id, payload, action, callback):
        try:
            return self.store.mutate(order_id, payload, action, callback)
        except OrderConflict as exc:
            raise WorkbenchError(str(exc), exc.code, 409) from exc

    @staticmethod
    def _part_key(order, part):
        sources = {row['id']: row['sha256'] for row in order['documents']}
        return digest([order['id'], part['id'], part['name'], part.get('part_number'), part.get('version'),
                       [(key, sources[key]) for key in part.get('document_ids', [])], part.get('source_refs')])

    def list(self):
        rows = self.store.list()
        for row in rows:
            active = [job for job in self.queue.list(row['id']) if job['status'] in {'queued', 'running', 'unknown'}]
            row['active_jobs'] = len(active)
        return rows

    def create(self, payload):
        return self.get(self.store.create(payload)['id'])

    def get(self, order_id):
        order = self.store.get(order_id)
        order['issues'] = []
        try:
            order['demands'] = demand(order)
        except RecordError as exc:
            order['demands'] = {}
            order['issues'].append(str(exc))
        order['part_status'] = {}
        for part in order['parts']:
            status = {'ready': False, 'issues': [], 'task_revision': None, 'url': None}
            if part.get('task_id'):
                try:
                    task = self.app.task(part['task_id'])
                    status.update(ready=task['validation']['ready'], issues=task['validation']['issues'],
                                  task_revision=task['revision'], url='/workbench/#task/' + task['id'] + '/confirm',
                                  run=task['run'])
                    expected = order['demands'].get(part['id'])
                    if expected is not None and str(task['fields']['quantity']['value']) != expected:
                        status['ready'] = False
                        status['issues'].append({'message': 'BOM 数量已变化，请重新准备零件。'})
                    if part.get('prepared_key') != self._part_key(order, part):
                        status['ready'] = False
                        status['issues'].append({'message': '图纸归属或版本已变化，请重新准备零件。'})
                except (RecordError, FileNotFoundError):
                    status['issues'].append({'message': '关联零件暂不可读，请重新准备或检查记录。'})
            elif part['mode'] in {'buy', 'assembly'}:
                keys = ['purchase_price'] if part['mode'] == 'buy' else ['assembly_unit_cost', 'assembly_setup_fee']
                status['ready'] = part.get('cost_confirmed') is True and all(part.get(key) is not None for key in keys) and bool((part.get('cost_inclusions') or '').strip())
                if not status['ready']:
                    status['issues'].append({'message': '请明确费用及包含范围并核对；未知金额留空，明确不收才填 0。'})
            if part['id'] in order['demands'] and part['mode'] == 'make':
                try:
                    confirmed_route(order, part['id'])
                except RecordError as exc:
                    status['ready'] = False
                    status['issues'].append({'message': str(exc)})
            order['part_status'][part['id']] = status
        if order.get('discovery'):
            try:
                job = self.queue.get(order['discovery']['job_id'])
                result = self._normalized_result(job) if job.get('result') else {}
                current = digest([(row['id'], row['sha256']) for row in order['documents']]) == job['payload']['documents_hash']
                order['discovery'].update(status=job['status'], progress=job['progress'], error=job['error'],
                                           parts=result.get('parts', []), warnings=result.get('warnings', []), source_current=current)
            except (KeyError, OSError, ValueError):
                order['discovery'].update(status='failed', error='运行记录暂不可读。')
        for row in order['documents']:
            row.pop('path', None)
        order['quotes'] = [{key: quote.get(key) for key in ('id', 'quote_id', 'amounts', 'status', 'created_at', 'source_current', 'links')}
                           for quote in (self.quote(order_id, row['quote_id']) for row in order['quotes'])]
        return order

    def _file(self, order_id, doc_id):
        order = self.store.get(order_id)
        document = next((row for row in order['documents'] if row['id'] == identifier(doc_id)), None)
        if document is None:
            raise FileNotFoundError()
        path = self.directory / identifier(order_id) / (document['id'] + Path(document['name']).suffix.lower())
        require(path.is_file() and not path.is_symlink(), '原始文件暂不可读。')
        require(hashlib.sha256(path.read_bytes()).hexdigest() == document['sha256'], '原始文件校验不一致。')
        return document, path

    def attachment(self, order_id, doc_id):
        row, path = self._file(order_id, doc_id)
        return path.read_bytes(), row['mime'], row['name']

    def upload(self, order_id, event_id, revision, name, content):
        require(isinstance(name, str) and 0 < len(name) <= 180 and Path(name).name == name and not any(c in name for c in '\\/:\x00\r\n'), '文件名无效。')
        suffix = Path(name).suffix.lower()
        require(suffix in FILE_TYPES, '请选择图片、PDF 或 CAD 文件。')
        require(0 < len(content) <= MAX_UPLOAD, '单文件须大于 0 且不超过 25 MiB。')
        for extension, signature in {'.pdf': b'%PDF-', '.png': b'\x89PNG\r\n\x1a\n', '.jpg': b'\xff\xd8\xff', '.jpeg': b'\xff\xd8\xff'}.items():
            if suffix == extension:
                require(content.startswith(signature), '文件内容与格式不符。')
        doc_id = 'DOC-' + short(identifier(event_id))
        payload = {'event_id': event_id, 'expected_revision': revision, 'name': name, 'sha256': hashlib.sha256(content).hexdigest()}
        with self.app.lock(order_id):
            self.store.get(order_id)
            folder = self.directory / identifier(order_id)
            folder.mkdir(mode=0o700, exist_ok=True)
            require(not folder.is_symlink(), '资料目录无效。')
            path = folder / (doc_id + suffix)
            if path.exists():
                require(not path.is_symlink() and path.read_bytes() == content, '上传事件已用于其他文件。')
            else:
                with path.open('xb') as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
            def commit(order):
                require(len(order['documents']) < 20, '首版每笔询价最多 20 份文件，请分批处理。')
                same = next((row for row in order['documents'] if row['sha256'] == payload['sha256']), None)
                order['documents'].append({'id': doc_id, 'name': name, 'size': len(content), 'sha256': payload['sha256'],
                                           'mime': FILE_TYPES[suffix], 'duplicate_of': same['id'] if same else None,
                                           'url': '/workbench/api/orders/' + order_id + '/documents/' + doc_id})
            self._mutate(order_id, payload, 'upload', commit)
        return self.get(order_id)

    def replace(self, order_id, payload):
        only_keys(payload, {'event_id', 'expected_revision', 'parts', 'lines', 'bom', 'routes'}, 'order structure')
        def commit(order):
            old = {part['id']: part for part in order['parts']}
            for key in ('parts', 'lines', 'bom', 'routes'):
                if key in payload:
                    order[key] = copy.deepcopy(payload[key])
            validate_structure(order)
            for part in order['parts']:
                previous = old.get(part['id'], {})
                require(part.get('task_id') in {None, previous.get('task_id')}, '不能将其他任务关联为本零件，请使用“准备零件”。')
                for key in ('task_id', 'prepared_key'):
                    if previous.get(key):
                        part[key] = previous[key]
                for ref in part.get('source_refs', []):
                    require(ref.get('document_id') in part.get('document_ids', []), '来源页必须属于本零件实际关联的图纸。')
                    require(ref.get('page') is None or type(ref['page']) is int and 1 <= ref['page'] <= 10000, '来源页码须为正整数。')
                    require(isinstance(ref.get('region', ''), str) and len(ref.get('region', '')) <= 2000, '来源区域说明无效。')
            for steps in order['routes'].values():
                require(isinstance(steps, list) and len(steps) <= 50, '每个零件工艺步骤最多 50 项。')
                seen = set()
                for step in steps:
                    require(isinstance(step, dict) and isinstance(step.get('operation'), str) and 0 < len(step['operation'].strip()) <= 100, '工序名称无效。')
                    identifier(step.get('id'))
                    require(step['id'] not in seen, '工艺步骤编号重复。')
                    seen.add(step['id'])
                    require(step.get('status') in {'pending_review', 'confirmed'}, '工艺步骤状态无效。')
                    require(isinstance(step.get('reason', ''), str) and len(step.get('reason', '')) <= 2000, '工艺说明过长。')
        with self.app.lock(order_id):
            self._mutate(order_id, payload, 'structure', commit)
        return self.get(order_id)

    def discover(self, order_id, payload):
        only_keys(payload, {'event_id', 'expected_revision'}, 'discovery request')
        require(self.discovery_factory is not None, '后台模型服务尚未配置。')
        with self.app.lock(order_id):
            def commit(order):
                require(order['documents'], '请先上传图纸。')
                documents = [row for row in order['documents'] if not row.get('duplicate_of')]
                data = {'documents': copy.deepcopy(documents), 'inquiry': order['inquiry'],
                        'documents_hash': digest([(row['id'], row['sha256']) for row in order['documents']]),
                        'request_id': payload['event_id']}
                job = self.queue.enqueue(order_id, 'discover', digest(data), data, payload['event_id'])
                order['discovery'] = {'job_id': job['id'], 'status': job['status']}
            self._mutate(order_id, payload, 'discover', commit)
        return self.get(order_id)

    def jobs(self, order_id):
        self.store.get(order_id)
        # No workspace paths, raw model text or provider details in browser state.
        return [{key: job.get(key) for key in ('id', 'kind', 'status', 'created_at', 'updated_at', 'progress', 'error', 'warnings', 'stop_requested')}
                for job in self.queue.list(order_id)]

    def stop(self, order_id, job_id):
        job = self.queue.get(job_id)
        require(job['order_id'] == order_id, '此运行不属于当前询价单。')
        self.queue.request_stop(job_id)
        return {'jobs': self.jobs(order_id)}

    def _workspace(self, job):
        path = self.app.reader_root / 'orders' / job['order_id'] / job['id']
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not path.is_symlink(), '后台资料目录无效。')
        return path

    def _views(self, job):
        workspace = self._workspace(job)
        cache = workspace / 'views.json'
        if cache.is_file():
            data = json.loads(cache.read_text())
            for row in data['views']:
                path = Path(row['path'])
                require(path.is_relative_to(workspace) and path.is_file() and not path.is_symlink(), '派生资料位置无效。')
                require(hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256'], '派生资料校验失败。')
            return data
        originals = workspace / 'originals'
        originals.mkdir(mode=0o700, exist_ok=True)
        files = []
        for document in job['payload']['documents']:
            current, path = self._file(job['order_id'], document['id'])
            require(current['sha256'] == document['sha256'], '读图输入已变化。')
            copy_path = originals / (document['id'] + Path(document['name']).suffix.lower())
            if not copy_path.exists():
                copy_path.write_bytes(path.read_bytes())
            files.append({'id': document['id'], 'path': str(copy_path), 'original_name': document['name'], 'sha256': document['sha256']})
        manifest = workspace / 'preprocess-input.json'
        atomic_write(manifest, json.dumps({'files': files}, ensure_ascii=False))
        data = self._convert(manifest, workspace / 'views')
        atomic_write(cache, json.dumps(data, ensure_ascii=False))
        return data

    @staticmethod
    def _convert(manifest, output):
        result = subprocess.run([sys.executable, str(ROOT / 'src/order_preprocess.py'), str(manifest), str(output)],
                                capture_output=True, text=True, timeout=60, cwd=ROOT)
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise RecordError('页面转换进程未完成，请检查文件或补充清晰图片。') from exc
        require(result.returncode == 0, data.get('error', '页面转换失败。'))
        return data

    def _part_files(self, order, part, key):
        """Keep page provenance when a multi-part PDF is assigned to child tasks."""
        workspace = self.app.reader_root / 'order-parts' / order['id'] / key
        workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache = workspace / 'views.json'
        if cache.is_file():
            data = json.loads(cache.read_text())
        else:
            files = []
            for doc_id in part['document_ids']:
                document, original = self._file(order['id'], doc_id)
                target = workspace / (doc_id + Path(document['name']).suffix.lower())
                if not target.exists():
                    target.write_bytes(original.read_bytes())
                files.append({'id': doc_id, 'path': str(target), 'original_name': document['name'], 'sha256': document['sha256']})
            manifest = workspace / 'input.json'
            atomic_write(manifest, json.dumps({'files': files}, ensure_ascii=False))
            data = self._convert(manifest, workspace / 'views')
            atomic_write(cache, json.dumps(data, ensure_ascii=False))
        selected = []
        for row in data['views']:
            refs = [ref for ref in part.get('source_refs', []) if ref['document_id'] == row['origin_id'] and type(ref.get('page')) is int]
            if refs and row.get('page') is not None and row['page'] not in {ref['page'] for ref in refs}:
                continue
            path = Path(row['path'])
            require(path.is_relative_to(workspace) and path.is_file() and not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256'], '零件派生图纸校验不一致。')
            selected.append(row)
        require(selected, part['name'] + '没有可供读取的已选页面，请核对页码或补充图片/PDF。')
        require(len(selected) <= 32, part['name'] + '所选资料超过单零件 32 页读图上限，请核对页码归属或拆成更小资料组。')
        return selected

    def _normalized_result(self, job):
        result = copy.deepcopy(job.get('result') or {})
        manifest = self._workspace(job) / 'views.json'
        views = {row['id']: row for row in json.loads(manifest.read_text())['views']} if manifest.is_file() else {}
        for part in result.get('parts', []):
            part['document_ids'] = list(dict.fromkeys(views[ref]['origin_id'] if ref in views else ref for ref in part['document_ids']))
            for evidence in part.get('evidence', []):
                original = views.get(evidence['reference'])
                if original:
                    evidence['reference'] = original['origin_id']
                    evidence['page'] = original['page'] or evidence['page']
            evidence = part.get('quantity_source')
            if evidence:
                original = views.get(evidence['reference'])
                if original:
                    evidence['reference'] = original['origin_id']
                    evidence['page'] = original['page'] or evidence['page']
        return result

    def _execute(self, job):
        token = job['lease_token']
        try:
            if job['kind'] == 'part_read':
                self._part_read(job)
                return
            require(job['kind'] == 'discover', '未知后台任务类型。')
            current = self.queue.get(job['id'])
            manifest = self._workspace(job) / '.dsh-order-discovery.json'
            if current['stop_requested'] and not current.get('session_id'):
                if manifest.is_file():
                    recovered = json.loads(manifest.read_text()).get('session_id')
                    if recovered:
                        job['session_id'] = recovered
                        self.queue.heartbeat(job['id'], token, 120, session_id=recovered)
                if not job.get('session_id'):
                    self.queue.fail(job['id'], token, {'message': '已在模型提交前停止。'}, stopped=True)
                    return
            with self.app.lock(job['order_id']):
                order = self.store.get(job['order_id'])
                require(order.get('discovery', {}).get('job_id') == job['id'], '此读图运行已被新的批次取代。')
                require(digest([(row['id'], row['sha256']) for row in order['documents']]) == job['payload']['documents_hash'], '资料已变化，请针对当前资料重新整理。')
            self.queue.heartbeat(job['id'], token, 120, progress={'stage': 'preprocessing', 'label': '正在转换图纸页面'})
            views = self._views(job)
            files = [{key: row[key] for key in ('id', 'path', 'original_name', 'sha256')} for row in views['views']]
            if self.queue.get(job['id'])['stop_requested'] and not job.get('session_id') and not manifest.exists():
                self.queue.fail(job['id'], token, {'message': '已在模型提交前停止。'}, stopped=True)
                return
            bridge = self.discovery_factory(self._workspace(job))
            if job.get('session_id'):
                session = job['session_id']
            else:
                def on_session(sid):
                    require(not self.queue.get(job['id'])['stop_requested'], '已在模型提交前请求停止。')
                    self.queue.heartbeat(job['id'], token, 120, session_id=sid)
                started = bridge.start(job['id'], files, job['payload']['inquiry'], self._workspace(job) / 'discovery.json', on_session=on_session)
                require(started['status'] not in {'failed', 'stopped'} and started.get('session_id'), '分件模型未能接收本批资料，请查看格式或连接后重试。')
                session = started['session_id']
                self.queue.heartbeat(job['id'], token, 120, session_id=session)
            started_at = time.monotonic()
            cancel_sent = False
            while not self._stop.is_set():
                current = self.queue.get(job['id'])
                if current['stop_requested'] and not cancel_sent:
                    bridge.stop(session)
                    cancel_sent = True
                result = bridge.inspect(session)
                self.queue.heartbeat(job['id'], token, 120, progress=result.get('progress') or {'stage': result['status'], 'label': '正在整理多文档零件候选'})
                if current['stop_requested'] and result['status'] in TERMINAL:
                    self.queue.fail(job['id'], token, {'message': '本次模型运行已结束，停止后未采用其候选。'}, stopped=True)
                    return
                if result['status'] == 'completed':
                    result['warnings'] = list(dict.fromkeys(views.get('warnings', []) + result.get('warnings', [])))
                    self.queue.finish(job['id'], token, {'parts': result['parts'], 'warnings': result['warnings']})
                    return
                if result['status'] in {'failed', 'waiting', 'stopped'}:
                    self.queue.fail(job['id'], token, {'message': '分件尚未完成，请检查资料或重新整理。'}, stopped=result['status'] == 'stopped')
                    return
                if time.monotonic() - started_at > 1800:
                    self.queue.fail(job['id'], token, {'message': '模型状态待确认，稍后继续查询同一会话。'}, unknown=True, delay_seconds=30)
                    return
                self._stop.wait(self.poll_seconds)
        except LeaseLost:
            return
        except Exception as exc:
            try:
                message = str(exc)[:500] if isinstance(exc, (RecordError, ValueError)) else '后台处理未完成，已保存资料保留，请稍后重试。'
                self.queue.fail(job['id'], token, {'message': message})
            except LeaseLost:
                pass

    def prepare(self, order_id, payload):
        only_keys(payload, {'event_id', 'expected_revision'}, 'prepare request')
        with self.app.lock(order_id):
            order = self.store.get(order_id)
            # A harmless replay is checked before attempting cross-store work.
            if order['revision'] != payload.get('expected_revision'):
                result = self._mutate(order_id, payload, 'prepare', lambda current: None)
                return self.get(order_id)
            quantities = demand(order)
            mappings = {}
            for part in order['parts']:
                if part['mode'] != 'make' or part['id'] not in quantities:
                    continue
                require(part['document_ids'], part['name'] + '尚未关联图纸。')
                route = confirmed_route(order, part['id'])
                key = self._part_key(order, part)
                inquiry = '本任务只处理指定零件：' + part['name'] + '；图号：' + str(part.get('part_number') or '未知') + '；版本：' + str(part.get('version') or '未知') + '。其他零件不得混入本件参数。\n' + order['inquiry'] + '\n已核对资料归属：' + json.dumps(part.get('source_refs', []), ensure_ascii=False)
                task = self.app.create({'event_id': 'OP-' + key[:40], 'inquiry': inquiry, 'simulated': order['simulated']})
                for view in self._part_files(order, part, key):
                    event = 'OD-' + short(view['id'] + view['sha256'])
                    if not any(file['id'] == 'FILE-' + event for file in task['attachments']):
                        view_name = Path(view['original_name'])
                        name = view_name.stem[:155] + view_name.suffix
                        task = self.app.upload(task['id'], event, task['revision'], name, Path(view['path']).read_bytes())
                values = {'product_name': part['name'], 'quantity': int(quantities[part['id']])}
                changed = {key: value for key, value in values.items() if task['fields'][key]['value'] != value or task['fields'][key]['status'] != 'verified'}
                if changed:
                    task = self.app.save_fields(task['id'], 'requirements', {'event_id': 'OR-' + short(payload['event_id'] + part['id']), 'expected_revision': task['revision'], 'values': changed})
                if task['fields']['process_route']['value'] != route or task['fields']['process_route']['status'] != 'verified':
                    task = self.app.save_fields(task['id'], 'technical', {'event_id': 'OT-' + short(payload['event_id'] + part['id']), 'expected_revision': task['revision'], 'values': {'process_route': route}})
                mappings[part['id']] = {'task_id': task['id'], 'prepared_key': key}
            def commit(current):
                for part in current['parts']:
                    if part['id'] in mappings:
                        part.update(mappings[part['id']])
            self._mutate(order_id, payload, 'prepare', commit)
            if self.app.bridge_factory:
                for part_id, info in mappings.items():
                    task = self.app.task(info['task_id'])
                    # Technical edits survive prepare; unchanged completed reads are reused.
                    if task['run'] is None:
                        data = {'task_id': task['id'], 'task_revision': task['revision']}
                        self.queue.enqueue(order_id, 'part_read', digest(data), data, 'read-' + short(task['id'] + str(task['revision'])))
        return self.get(order_id)

    def _part_read(self, job):
        token, task_id = job['lease_token'], job['payload']['task_id']
        task = self.app.task(task_id)
        order = self.store.get(job['order_id'])
        linked = next((part for part in order['parts'] if part.get('task_id') == task_id and part.get('prepared_key') == self._part_key(order, part)), None)
        require(linked is not None, '零件归属已变化，此旧读图作业不再发送模型。')
        quantities = demand(order)
        require(str(task['fields']['quantity']['value']) == quantities.get(linked['id']), '零件数量已变化，请重新准备零件。')
        if task['run'] is None:
            self.queue.heartbeat(job['id'], token, 120)
            require(task['revision'] == job['payload']['task_revision'], '零件参数已更新，请重新准备当前资料后再读图。')
            if self.queue.get(job['id'])['stop_requested']:
                self.queue.fail(job['id'], token, {'message': '已在零件读图前停止。'}, stopped=True)
                return
            if task['validation']['ready']:
                self.queue.finish(job['id'], token, {'task_id': task_id, 'warnings': ['参数已由操作者补齐，本次未调用模型。']})
                return
            task = self.app.start_read(task_id, {'event_id': 'ORUN-' + short(job['id']), 'expected_revision': task['revision']})
        started_at, cancel_sent = time.monotonic(), False
        while not self._stop.is_set():
            current = self.queue.get(job['id'])
            task = self.app.task(task_id)
            run = task['run'] or {}
            if run.get('session_id') and not current.get('session_id'):
                self.queue.heartbeat(job['id'], token, 120, session_id=run['session_id'])
            if current['stop_requested'] and not cancel_sent:
                if run.get('status') in {'running', 'starting', 'unknown'}:
                    self.app.stop_read(task_id, {'event_id': 'OSTOP-' + short(job['id']), 'expected_revision': task['revision']})
                cancel_sent = True
            if current['stop_requested'] and run.get('status') in TERMINAL:
                self.queue.fail(job['id'], token, {'message': '零件模型运行已结束，已停止本次后台作业。'}, stopped=True)
                return
            self.queue.heartbeat(job['id'], token, 120, progress=run.get('progress') or {'stage': run.get('status'), 'label': '正在逐零件读取参数'})
            if run.get('status') == 'completed':
                self.queue.finish(job['id'], token, {'task_id': task_id, 'warnings': run.get('warnings', [])})
                return
            if run.get('status') in {'failed', 'stopped', 'waiting'}:
                self.queue.fail(job['id'], token, {'message': '零件读图未完成，已取得资料保留，可从零件页继续。'}, stopped=run['status'] == 'stopped')
                return
            if time.monotonic() - started_at > 1800:
                self.queue.fail(job['id'], token, {'message': '零件状态待确认，将继续检查原运行。'}, unknown=True, delay_seconds=30)
                return
            self._stop.wait(self.poll_seconds)

    def issue(self, order_id, payload):
        only_keys(payload, {'event_id', 'expected_revision', 'policy', 'shared_fees'}, 'order quotation')
        quote_id = 'OQ-' + short(identifier(payload.get('event_id')))
        def commit(order):
            quantities = demand(order)
            parts = {row['id']: row for row in order['parts']}
            records, calculated = {}, {}
            with ExitStack() as locks:
                task_ids = sorted({parts[part_id]['task_id'] for part_id in quantities if parts[part_id]['mode'] == 'make' and parts[part_id].get('task_id')})
                for task_id in task_ids:
                    locks.enter_context(self.app.lock(task_id))
                cost_policy = pricing({**payload['policy'], 'pricing_method': 'cost_plus', 'rate_percent': '0'})
                for part_id in quantities:
                    part = parts[part_id]
                    if part['mode'] != 'make':
                        continue
                    require(part.get('task_id'), part['name'] + '尚未准备零件资料。')
                    require(part.get('prepared_key') == self._part_key(order, part), part['name'] + '图纸归属或版本已变化，请重新准备零件。')
                    record = self.app.records.load(part['task_id'])
                    require(self.app._validation(record)['ready'], part['name'] + '资料或采用价格尚未核对完整，请进入该零件补充。')
                    records[part_id] = record
                    calculated[part_id] = self.app.quotes._calculate(record, cost_policy)
                    calculated[part_id]['adopted_price_sources'] = copy.deepcopy(self.app._state(part['task_id']).get('price_selections', {}))
                result = calculate(order, records, calculated, payload['policy'], payload.get('shared_fees', []), quote_id)
                for record in records.values():
                    require(digest(self.app.records.load(record['task_id'])) == digest(record), '核算期间零件记录已变化，请重新核对后生成报价。')
            order['quotes'].append(result)
            return {'quote_id': quote_id}
        with self.app.lock(order_id):
            result = self._mutate(order_id, payload, 'quote', commit)
        return self.quote(order_id, result['quote_id'])

    def quote(self, order_id, quote_id):
        order = self.store.get(order_id)
        quote = next((copy.deepcopy(row) for row in order['quotes'] if row['quote_id'] == identifier(quote_id)), None)
        if quote is None:
            raise FileNotFoundError()
        relevant = {key: order[key] for key in quote['order_snapshot']}
        # A quote append changes only order revision; compare actual source data.
        relevant['revision'] = quote['order_snapshot']['revision']
        quote['source_current'] = digest(relevant) == digest(quote['order_snapshot'])
        for record in quote['record_snapshots'].values():
            try:
                quote['source_current'] &= digest(self.app.records.load(record['task_id'])) == digest(record)
                if self.app._state(record['task_id']).get('price_selections'):
                    quote['source_current'] &= self.app._validation(self.app.records.load(record['task_id']))['ready']
            except (RecordError, FileNotFoundError):
                quote['source_current'] = False
        base = '/workbench/api/orders/' + order_id + '/quotes/' + quote_id + '/'
        quote['links'] = {'quote_url': base + 'quote.html', 'cost_url': base + 'cost.html',
                          'bom_url': base + 'bom.csv', 'routes_url': base + 'routes.csv',
                          'quote_download': base + 'quote.md', 'cost_download': base + 'cost.md'}
        # Full snapshots are only returned through internal cost exports.
        quote.pop('record_snapshots', None)
        return quote

    @staticmethod
    def csv_document(order, filename):
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        def safe(value):
            value = '' if value is None else str(value)
            return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value
        quantities = demand(order)
        parts = {row['id']: row for row in order['parts']}
        if filename == 'bom.csv':
            writer.writerow(['层级关系', '父件及每套用量', '零件号', '名称', '版本', '总需求量', '需求单位', '模式', '图纸'])
            top_level = {row['part_id'] for row in order['lines']}
            for part in order['parts']:
                if part['id'] not in quantities:
                    continue
                parents = [row for row in order['bom'] if row['part_id'] == part['id'] and row['parent_id'] in quantities]
                sources = {row['id']: row['name'] for row in order['documents']}
                relation = '顶层及子件' if parents and part['id'] in top_level else '子件' if parents else '顶层'
                writer.writerow([safe(value) for value in [relation,
                    '；'.join(parts[edge['parent_id']]['name'] + ' × ' + str(edge['quantity']) for edge in parents),
                    part.get('part_number'), part['name'], part.get('version'), quantities[part['id']], '套' if part['mode'] == 'assembly' else '件', part['mode'],
                    '、'.join(sources[key] for key in part.get('document_ids', []))]])
        elif filename == 'routes.csv':
            writer.writerow(['零件', '图号', '版本', '顺序', '工序', '说明', '状态'])
            for part in order['parts']:
                if part['id'] not in quantities:
                    continue
                for index, step in enumerate(order['routes'].get(part['id'], []), 1):
                    writer.writerow([safe(value) for value in [part['name'], part.get('part_number'), part.get('version'), index, step['operation'], step.get('reason', ''), '已核对' if step['status'] == 'confirmed' else '建议待核对']])
        else:
            raise FileNotFoundError()
        return b'\xef\xbb\xbf' + output.getvalue().encode()

    def document(self, order_id, quote_id, filename):
        quote = self.quote(order_id, quote_id)
        if filename in {'bom.csv', 'routes.csv'}:
            return self.csv_document(quote['order_snapshot'], filename), 'text/csv; charset=utf-8', True
        require(filename in {'quote.html', 'cost.html', 'quote.md', 'cost.md'}, '未知报价文档。')
        cell = lambda value: str(value if value is not None else '').replace('|', '\\|').replace('\n', ' ')
        lines = ['# ' + ('整单报价草稿' if filename.startswith('quote') else '内部成本明细'), '',
                 '模拟报价草稿，待审核；人民币、未计税费、不含运费。', '', '订单：' + cell(quote['order_name']), '',
                 '| 产品/零件 | 图号 | 版本 | 数量 | 单价 | 金额 |', '|---|---|---|---:|---:|---:|']
        for row in quote['lines']:
            values = [row.get(key) for key in ('name', 'part_number', 'version')]
            values += [row['quantity'] + (' 套' if row['unit'] == 'set' else ' 件'), row['unit_price'], row['total_price']]
            lines.append('| ' + ' | '.join(cell(value) for value in values) + ' |')
        if quote['shared_fees']:
            lines += ['', '整单另列费用报价金额：¥' + quote['shared_price']]
        lines += ['', '**报价总额：¥' + quote['amounts']['total_price'] + '**', '', '交期、付款条件和有效期：待约定。']
        if filename.startswith('cost'):
            lines += ['', '## 各零件自身成本（装配父项不重复计入子件）', '', '| 零件 | 需求量 | 自身成本 |', '|---|---:|---:|']
            lines += ['| ' + ' | '.join(cell(row[key]) for key in ('name', 'quantity', 'own_total_cost')) + ' |' for row in quote['part_costs']]
            lines += ['', '成本合计：¥' + quote['amounts']['total_cost'], '', '定价方式：' + cell(quote['policy']['pricing_method']) + '，比例：' + quote['policy']['rate'], '', '整单费用：' + cell(json.dumps(quote['shared_fees'], ensure_ascii=False))]
            lines += ['', '## 零件成本计费明细', '', '```json', json.dumps(quote['part_costs'], ensure_ascii=False, indent=2), '```']
        if not quote['source_current']:
            lines += ['', '提示：输入已变化，本文件为历史快照，请重新核算当前订单。']
        text = '\n'.join(lines) + '\n'
        if filename.endswith('.md'):
            return text.encode(), 'text/markdown; charset=utf-8', True
        try:
            from markdown_it import MarkdownIt
            body = MarkdownIt('commonmark', {'html': False}).enable('table').render(text)
        except ImportError:
            body = '<pre>' + html.escape(text) + '</pre>'
        page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>整单报价草稿</title><style>body{font:15px/1.65 system-ui;max-width:1100px;margin:32px auto;padding:0 20px;color:#263343}table{border-collapse:collapse;width:100%}td,th{border:1px solid #dbe2eb;padding:8px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f7fa;padding:16px}h1{font-size:24px}</style><body>' + body + '</body></html>'
        return page.encode(), 'text/html; charset=utf-8', False
