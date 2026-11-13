"""Customer-owned uploads over a separate native quotation store.

Opaque browser sessions own orders. Explicit customer projections are returned
instead of the operator state, and every artifact rechecks ownership.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
from http.cookies import CookieError, SimpleCookie
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

from batch_quote_service import BatchQuoteService, BATCH_ID, BUSY, RUN_ID, LINE_ID, demand
from batch_quote_costing import aggregate_display_groups
from prep_record import atomic_write, stamp, digest
from workbench_store import WorkbenchError
from customer_upload_quota import CustomerUploadQuota, validate_step, RETRY_LIMIT, VERSION as QUOTA_VERSION, unavailable
from customer_weight_view import weight_summary

API_PREFIX = CUSTOMER_API = '/workbench/customer/api'
ADMIN_API_PREFIX = ADMIN_API = '/ortho2cad-test/customer-upload-admin/api/quote-batches'
COOKIE_NAME = 'HYL_CUSTOMER_SESSION'
SESSION_SECONDS = 30 * 24 * 60 * 60
OWNER_ID = re.compile(r'[0-9a-f]{64}\Z')
MAX_ORDERS_PER_IDENTITY = 50
MAX_STORE_ORDERS = 1000
MAX_STORE_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
MAX_STORE_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ACTIVE_PER_IDENTITY = 2
MAX_ACTIVE_ORDERS = 8
CUSTOMER_ARTIFACTS = {'customer-quote.html', 'customer-print.html'}


def _retryable(state):
    if state.get('status') in {'failed', 'interrupted'}:
        return True
    return state.get('status') == 'completed_with_issues' and any(
        row.get('kind') == 'step' and row.get('status') == 'failed' and not row.get('duplicate_of')
        for row in state.get('files', []))


class CustomerBatchQuoteService(BatchQuoteService):
    """Full operator service, reachable only in its authenticated namespace."""
    API_PREFIX = ADMIN_API


class CustomerUploadService:
    def __init__(self, root, batches):
        self.root = Path(root)
        demand(not self.root.is_symlink(), '客户会话存储不可用。')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.batches = batches
        self._mutex = threading.RLock()

    @contextmanager
    def _lock(self):
        with self._mutex:
            fd = os.open(self.root / '.customer-upload.lock',
                         os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'r+') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                yield

    @staticmethod
    def cookie_header(token):
        demand(isinstance(token, str) and OWNER_ID.fullmatch(token), '客户会话不可用。')
        return (COOKIE_NAME + '=' + token + '; Path=/workbench/customer/; Max-Age='
                + str(SESSION_SECONDS) + '; HttpOnly; Secure; SameSite=Lax')

    def new_session(self):
        token = secrets.token_hex(32)
        owner = hashlib.sha256(token.encode('ascii')).hexdigest()
        path = self.root / (owner + '.json')
        demand(not path.exists() and not path.is_symlink(), '客户会话创建失败，请重试。')
        atomic_write(path, json.dumps({'owner': owner, 'created_at': stamp(),
                                      'expires_at': time.time() + SESSION_SECONDS}, ensure_ascii=False))
        return token, owner

    def session(self, cookie_header):
        """Return an identity only after validating its durable token hash."""
        try:
            demand(isinstance(cookie_header, str) and len(cookie_header) <= 16384,
                   '客户会话已失效，请重新打开上传入口。', 'customer_session_required', 401)
            cookies = SimpleCookie()
            cookies.load(cookie_header)
            value = cookies.get(COOKIE_NAME)
            token = value.value if value is not None else ''
            demand(OWNER_ID.fullmatch(token) is not None,
                   '客户会话已失效，请重新打开上传入口。', 'customer_session_required', 401)
            owner = hashlib.sha256(token.encode('ascii')).hexdigest()
            self._identity(owner)
            return owner
        except (CookieError, ValueError, TypeError, OSError, json.JSONDecodeError) as error:
            raise WorkbenchError('客户会话已失效，请重新打开上传入口。',
                                 'customer_session_required', 401) from error

    def _identity(self, owner):
        demand(isinstance(owner, str) and OWNER_ID.fullmatch(owner),
               '客户会话已失效，请重新打开上传入口。', 'customer_session_required', 401)
        path = self.root / (owner + '.json')
        demand(path.is_file() and not path.is_symlink() and path.stat().st_size <= 2048,
               '客户会话已失效，请重新打开上传入口。', 'customer_session_required', 401)
        value = json.loads(path.read_text(encoding='utf-8'))
        demand(isinstance(value, dict) and value.get('owner') == owner
               and type(value.get('expires_at')) in {int, float}
               and value['expires_at'] > time.time(),
               '客户会话已失效，请重新打开上传入口。', 'customer_session_required', 401)

    def _owned(self, owner, batch_id):
        self._identity(owner)
        demand(isinstance(batch_id, str) and BATCH_ID.fullmatch(batch_id),
               '订单不存在。', 'not_found', 404)
        with self.batches._lock(batch_id):
            state = self.batches._read(batch_id)
            demand(state.get('_customer_owner') == owner, '订单不存在。', 'not_found', 404)
            return state

    @staticmethod
    def _event(owner, supplied):
        BatchQuoteService._event(supplied)
        return 'customer-' + hashlib.sha256((owner + '\0' + supplied).encode()).hexdigest()

    @staticmethod
    def _project(state):
        """Construct the public representation without raw internal fields."""
        out = {key: copy.deepcopy(state.get(key)) for key in
               ('id', 'name', 'status', 'revision', 'created_at', 'updated_at')}
        out['order_sets'] = (state.get('settings') or {}).get('order_sets')
        out['files'] = [{key: copy.deepcopy(row.get(key)) for key in ('id', 'name', 'kind', 'status')}
                        for row in state.get('files', [])]
        progress = state.get('progress') or {}
        messages = {'uploaded': '资料已保存，填写数量后开始。',
                    'queued': '已加入后台队列，离开页面后仍会继续。',
                    'running': '正在读取 STEP 并整理报价。',
                    'completed': '报价草稿已生成，待审核。',
                    'completed_with_issues': '报价草稿已保存，部分资料需核对。',
                    'failed': '本轮处理未完成，已上传资料会保留。',
                    'interrupted': '本轮处理已中断，可稍后重试。'}
        out['progress'] = {'total': progress.get('total', 0), 'done': progress.get('done', 0),
                           'current': None, 'message': messages.get(state.get('status'), '资料已保存。')}
        out['error'] = ('部分 STEP 尚未完成读取，请重试或联系工厂核对文件。'
                        if state.get('status') in {'failed', 'interrupted'} else None)
        retries = set(state.get('_customer_retries', {})) | set(state.get('_customer_retry_signatures', {}))
        out['retry_limit'] = RETRY_LIMIT
        out['retry_remaining'] = max(0, RETRY_LIMIT-len(retries))
        out['can_start'] = state.get('status') == 'uploaded' and not state.get('runs') and not state.get('_events')
        out['can_retry'] = (_retryable(state) and out['retry_remaining'] > 0
                            and isinstance(state.get('settings'), dict))
        out['quote'] = None
        result = state.get('result')
        if isinstance(result, dict) and isinstance(result.get('run_id'), str) and RUN_ID.fullmatch(result['run_id']):
            groups = aggregate_display_groups(result)
            base = CUSTOMER_API + '/orders/' + state['id'] + '/runs/' + result['run_id'] + '/'
            out['quote'] = {'run_id': result['run_id'], 'total_price': (result.get('totals') or {}).get('total_price'),
                            'currency': 'CNY', 'status': 'pending_review', 'simulated': True,
                            'unpriced_lines': sum(row.get('priced') is not True for row in groups),
                            'quoted_lines': sum(row.get('priced') is True for row in groups),
                            'weight_summary': weight_summary(result),
                            'quote_url': base + 'customer-quote.html',
                            'print_url': base + 'customer-print.html'}
            manufacturing = result.get('manufacturing')
            process_mode = ((result.get('settings') or {}).get('costing_mode') == 'process-v1'
                            and isinstance(manufacturing, dict) and manufacturing.get('version') == 'process-v1')
            if process_mode:
                manual_sheet = isinstance(result.get('cost_sheet'), dict)
                all_priced = bool(groups) and all(group.get('priced') is True for group in groups)
                saved_total = out['quote']['total_price']
                manual_complete = manual_sheet and saved_total is not None and all_priced
                process_complete = manufacturing.get('complete') is True and all_priced and saved_total is not None
                # Internal manual versions preserve the original engineering
                # evidence. Their current financial subtotal is authoritative;
                # copying the original process summary would show stale prices.
                estimated_subtotal = saved_total if manual_sheet else manufacturing.get('estimated_price_subtotal')
                pricing_status = ('manual_review' if manual_complete else
                    'partial' if manual_sheet and estimated_subtotal is not None else
                    'blocked' if manual_sheet else manufacturing.get('status', 'blocked'))
                out['quote'].update(costing_mode='process-v1',
                    pricing_status=pricing_status,
                    manufacturing_url=base + 'manufacturing',
                    estimated_subtotal=estimated_subtotal,
                    parts_count=manufacturing.get('parts_count', len(result.get('lines', []))),
                    complete_parts=manufacturing.get('complete_parts', 0),
                    pending_parts=manufacturing.get('pending_parts', 0))
                if manufacturing.get('estimate_version'):
                    out['quote'].update(estimate_version=manufacturing['estimate_version'],
                        scope_label='拟定工艺范围的估算方案价，待工程审核', manufacturing_release=False)
                if not (process_complete or manual_complete):
                    out['quote']['total_price'] = None
                if state.get('status') not in BUSY:
                    out['progress']['message'] = ('人工分项核价已保存，工程结果待审核。' if manual_complete else
                        '拆件与分项核价已完成，待工程审核。' if process_complete else
                        '拆件结果已保存，部分工艺仍需工程核对。')
        return out

    def _states(self):
        for folder in self.batches.root.iterdir():
            if BATCH_ID.fullmatch(folder.name) and folder.is_dir() and not folder.is_symlink():
                with self.batches._lock(folder.name):
                    yield self.batches._read(folder.name)

    def list(self, owner):
        self._identity(owner)
        rows = [self._project(state) for state in self._states() if state.get('_customer_owner') == owner]
        return {'orders': sorted(rows, key=lambda row: (row['updated_at'], row['id']), reverse=True)}

    def get(self, owner, batch_id):
        return self._project(self._owned(owner, batch_id))

    def quota(self, owner):
        self._identity(owner)
        # Atomic read only: even the portal flock file is not created by GET.
        return CustomerUploadQuota(self.root).status()

    def upload(self, owner, name, content, event_id):
        self._identity(owner)
        event = self._event(owner, event_id)
        source_sha = validate_step(name, content)
        signature = digest([name, source_sha])
        batch_id = 'BATCH-' + hashlib.sha256(event.encode()).hexdigest()[:32]
        with self._lock():
            quota = CustomerUploadQuota(self.root)
            ledger = quota.load()
            reservation = ledger['events'].get(event) if ledger else None
            exists = self.batches._folder(batch_id).exists()
            if exists:
                with self.batches._lock(batch_id):
                    saved = self.batches._read(batch_id)
                    demand(saved.get('_create_signature') == signature,
                           '同一上传请求对应的文件已变化。', 'event_conflict', 409)
                    demand(saved.get('_customer_owner') in {None, owner}, '订单不存在。', 'not_found', 404)
                    # A genuinely historical order is not retroactively charged.
                    # A newly metered order with a missing journal fails closed.
                    if reservation is None:
                        if saved.get('_customer_upload_quota') is not None:
                            raise unavailable()
                        if saved.get('_customer_owner') is None:
                            saved['_customer_owner'] = owner
                            saved['_customer_policy'] = 'customer-process-demo-v1'
                            self.batches._save(saved)
                        return self._project(saved)
            elif reservation is not None and reservation['status'] == 'accepted':
                raise unavailable()
            if not exists:
                states = list(self._states())
                own = [state for state in states if state.get('_customer_owner') == owner]
                demand(len(own) < MAX_ORDERS_PER_IDENTITY and len(states) < MAX_STORE_ORDERS,
                       '上传记录已达到当前额度，请联系工厂处理。', 'customer_upload_limit', 429)
                stored_bytes = sum((self.batches._folder(state['id']) / 'original.bin').stat().st_size
                                   for state in states)
                demand(isinstance(content, bytes) and stored_bytes + len(content) <= MAX_STORE_UPLOAD_BYTES,
                       '当前上传空间已满，请联系工厂处理。', 'customer_upload_limit', 429)
                retained_bytes = stored_bytes + sum(row.get('size', 0) for state in states for row in state['files'])
                demand(retained_bytes + 2*len(content) <= MAX_STORE_EXPANDED_BYTES,
                       '当前资料空间已满，请联系工厂处理。', 'customer_upload_limit', 429)
            ledger, _ = quota.reserve(event, owner, batch_id, name, content, source_sha, signature)
            try:
                self.batches.create(name, content, event)
            except WorkbenchError as error:
                if error.status in {400, 413, 415} and not self.batches._folder(batch_id).exists():
                    quota.release_validation_failure(ledger, event)
                raise
            with self.batches._lock(batch_id):
                state = self.batches._read(batch_id)
                previous_owner = state.get('_customer_owner')
                demand(previous_owner in {None, owner}, '订单不存在。', 'not_found', 404)
                demand(len(state['files']) == 1 and state['files'][0]['kind'] == 'step'
                       and state['files'][0]['sha256'] == source_sha,
                       '客户入口每次只能接收一份 STEP/STP 原文件。', 'invalid_step', 400)
                binding = {'version': QUOTA_VERSION, 'epoch': ledger['epoch'], 'event': event}
                if previous_owner is None or state.get('_customer_upload_quota') != binding:
                    state['_customer_owner'] = owner
                    state['_customer_policy'] = 'customer-process-demo-v1'
                    state['_customer_upload_quota'] = binding
                    self.batches._save(state)
            quota.accept(ledger, event)
            return self._project(state)

    @staticmethod
    def _settings(order_sets):
        """Factory-controlled demo policy; customer cannot supply prices."""
        demand(type(order_sets) is int and 0 < order_sets <= 1000000,
               '订单数量须为 1～1000000 的整数。')
        from batch_material_prices import VERSION, pricing_catalog
        catalog = pricing_catalog()
        references = {row['material_profile']: row for row in catalog['references']}
        rows = {}
        for profile in catalog['profiles']:
            reference = references.get(profile['id'])
            rows[profile['id']] = {
                'rate': reference['price_cny_per_kg'] if reference else None,
                'tax_included': reference.get('tax_included') is not False if reference else True,
                'source_kind': 'market_reference' if reference else 'manual',
                'reference_id': reference['id'] if reference else None,
                'source_text': reference['source_name'] if reference else '',
                'quote_date': reference['quote_date'] if reference else None}
        return {'order_sets': order_sets, 'pricing_method': 'cost_plus', 'rate_percent': '20',
                'material_profile': 'carbon_steel', 'rate_profile': VERSION,
                'costing_mode': 'process-v1',
                'demo_confirmed': True, 'scope_confirmed': True,
                'material_pricing': {'vat_rate_percent': '13', 'confirmed': True, 'rows': rows}}

    def _active_guard(self, owner):
        active = [state for state in self._states() if state.get('status') in BUSY]
        demand(sum(state.get('_customer_owner') == owner for state in active) < MAX_ACTIVE_PER_IDENTITY
               and len(active) < MAX_ACTIVE_ORDERS,
               '已有订单正在处理，请等待完成后再提交。', 'customer_busy', 429)

    def start(self, owner, batch_id, payload):
        demand(isinstance(payload, dict) and set(payload) == {'event_id', 'expected_revision', 'order_sets'},
               '请只提交订单数量和当前订单版本。')
        demand(type(payload.get('expected_revision')) is int and payload['expected_revision'] >= 0,
               '请重新读取订单版本。')
        self._owned(owner, batch_id)
        event = self._event(owner, payload['event_id'])
        settings = self._settings(payload['order_sets'])
        with self._lock():
            state = self._owned(owner, batch_id)
            replay = event in state.get('_events', {})
            prepared = state.get('_customer_start_request')
            if prepared is not None:
                demand(isinstance(prepared, dict) and prepared.get('event') == event,
                       '此订单已提交，请查看当前处理进度。', 'already_started', 409)
                demand(prepared.get('signature') == digest(payload),
                       '同一处理请求的条件已变化。', 'event_conflict', 409)
                settings = copy.deepcopy(prepared['settings'])
            elif not replay:
                demand(state['status'] == 'uploaded' and not state.get('runs') and not state.get('_events'),
                       '此订单已提交，请查看当前处理进度。', 'already_started', 409)
                demand(payload['expected_revision'] == state['revision'],
                       '任务进度已更新，请刷新后重试。', 'revision_conflict', 409)
                self._active_guard(owner)
                with self.batches._lock(batch_id):
                    current = self.batches._read(batch_id)
                    current['_customer_start_request'] = {'event': event, 'signature': digest(payload),
                                                          'settings': copy.deepcopy(settings)}
                    self.batches._save(current)
            result = self.batches.start(batch_id, {'request_id': event,
                'expected_revision': payload['expected_revision'], 'settings': settings})
        return self._project(result)

    def retry(self, owner, batch_id, payload):
        demand(isinstance(payload, dict) and set(payload) == {'event_id', 'expected_revision'},
               '重试请求无效，请重新打开订单。')
        self._owned(owner, batch_id)
        event = self._event(owner, payload['event_id'])
        with self._lock():
            state = self._owned(owner, batch_id)
            replay = event in state.get('_events', {})
            previous = state.get('_customer_retry_signatures', {}).get(event)
            saved = state.get('_customer_retries', {}).get(event)
            known = saved is not None or previous is not None
            if previous is not None:
                demand(previous == digest(payload), '同一重试请求的条件已变化。', 'event_conflict', 409)
            if replay and not known:
                raise WorkbenchError('请求编号已用于其他处理步骤。', 'event_conflict', 409)
            if not known:
                demand(_retryable(state),
                       '当前订单不能再次处理，请查看已保存的拆件和报价。', 'nothing_to_retry', 409)
                used = len(set(state.get('_customer_retries', {})) | set(state.get('_customer_retry_signatures', {})))
                demand(used < RETRY_LIMIT, '本订单最多重试1次，次数已用完，请联系工厂核对文件。',
                       'customer_retry_limit_exhausted', 429)
            if not replay:
                self._active_guard(owner)
            failed = [row['id'] for row in state['files'] if row['kind'] == 'step'
                      and row['status'] == 'failed' and not row.get('duplicate_of')]
            action = copy.deepcopy(saved) if isinstance(saved, dict) else {
                'operation': 'retry', 'file_ids': saved if isinstance(saved, list) else failed}
            if not replay:
                demand(type(payload.get('expected_revision')) is int
                       and payload['expected_revision'] == state['revision'],
                       '任务进度已更新，请刷新后重试。', 'revision_conflict', 409)
                if action['operation'] == 'start' or not action.get('file_ids'):
                    # A worker may fail after reading CAD but before saving its
                    # quotation. Requeue the saved conditions and reuse ready
                    # native geometry, instead of stranding that upload.
                    demand(state['status'] in {'failed', 'interrupted'} and isinstance(state.get('settings'), dict),
                           '当前没有可重试的失败 STEP 文件。', 'nothing_to_retry', 409)
                    action = {'operation': 'start', 'settings': copy.deepcopy(state['settings'])}
                with self.batches._lock(batch_id):
                    current = self.batches._read(batch_id)
                    current.setdefault('_customer_retries', {})[event] = action
                    current.setdefault('_customer_retry_signatures', {})[event] = digest(payload)
                    self.batches._save(current)
            native_payload = {'request_id': event, 'expected_revision': payload['expected_revision']}
            if action['operation'] == 'start':
                native_payload['settings'] = action['settings']
                result = self.batches.start(batch_id, native_payload)
            else:
                native_payload['file_ids'] = action['file_ids']
                result = self.batches.retry(batch_id, native_payload)
        return self._project(result)

    def artifact(self, owner, batch_id, run_id, filename):
        self._owned(owner, batch_id)
        return self._artifact(batch_id, run_id, filename, technical_source='customer', owner=owner)

    def staff_artifact(self, batch_id, run_id, filename):
        """Customer-format documents called only by the protected staff route."""
        return self._artifact(batch_id, run_id, filename, technical_source='staff')

    def _artifact(self, batch_id, run_id, filename, *, technical_source, owner=None):
        demand(filename in CUSTOMER_ARTIFACTS, '报价文件不存在。', 'not_found', 404)
        with self.batches._lock(batch_id):
            state = self.batches._read(batch_id)
            result = self.batches._preview_run_locked(state, run_id)
            from batch_engineering_review import read_review
            review = read_review(self.batches._folder(batch_id), run_id, result)
            recognition = self.batches.engineering_recognition.read(batch_id, run_id, result)
        from batch_customer_quote import render_customer_quote
        # Read the existing source-bound CAD cache after releasing the snapshot
        # lock. Previewing a quote never starts parsing, AI, or a new price run.
        parts = (self.staff_manufacturing(batch_id, run_id) if technical_source == 'staff'
                 else self.manufacturing(owner, batch_id, run_id))['parts']
        content = render_customer_quote(result, printable=filename == 'customer-print.html',
                                        engineering_review=review, engineering_recognition=recognition,
                                        customer_parts=parts, technical_source=technical_source)
        if filename == 'customer-print.html':
            content = content.replace(b' onclick="window.print()"', b' data-customer-print-button', 1)
            content = content.replace(b'</head>',
                b'<script defer src="/workbench/customer/print.js"></script></head>', 1)
        return content, 'text/html; charset=utf-8', filename

    def manufacturing(self, owner, batch_id, run_id):
        """Return technical per-part evidence over this customer's saved run."""
        self._owned(owner, batch_id)
        prefix = CUSTOMER_API + '/orders/' + batch_id + '/runs/' + run_id + '/'
        return self._manufacturing(batch_id, run_id, prefix)

    def process_pricing(self, owner, batch_id, run_id, payload=None):
        """Only this owner's existing order; no upload quota or worker bypass."""
        self._owned(owner, batch_id)
        if payload is None:
            return self.batches.process_pricing.get(batch_id, run_id)
        value = self.batches.process_pricing.apply(batch_id, run_id, payload)
        return {key: value[key] for key in ('run_id', 'source_run_id', 'replayed', 'already_applied') if key in value} | {
            'order': self.get(owner, batch_id)}

    def staff_manufacturing(self, batch_id, run_id):
        """Called only by the Nginx-protected operator namespace."""
        prefix = ADMIN_API + '/' + batch_id + '/runs/' + run_id + '/'
        manifest = self._manufacturing(batch_id, run_id, prefix)
        manifest['source'] = 'staff'
        return manifest

    def _manufacturing(self, batch_id, run_id, prefix):
        """Read source-bound cached evidence; never analyze, price or call AI."""
        state, result = self.batches._preview_run(batch_id, run_id)
        from customer_manufacturing_view import part_projection, bend_projection
        from batch_engineering_native import enrich_native
        native_lines = (result.get('engineering_native') or {}).get('lines', {})
        native_lines = native_lines if isinstance(native_lines, dict) else {}
        sources = {row.get('id'): row.get('sha256') for row in result.get('source_files', []) if isinstance(row, dict)}
        parts, grouped = [], {}
        for line in result['lines']:
            grouped.setdefault(line.get('file_id'), []).append(line)
        # Read one verified source geometry at a time. Meshes never enter the
        # manifest, and their memory is released before the next STEP file.
        for file_id, lines in grouped.items():
            context = None
            try:
                context = self.batches._preview_file(batch_id, state, result, file_id)
            except (ValueError, OSError, TypeError, KeyError, FileNotFoundError):
                pass
            cached_native = {}
            if context:
                # This only projects already measured/cache-validated scalars.
                # It cannot run a CAD engine, alter a quote or adopt a material.
                cached_native = enrich_native({'lines': lines, 'source_files': result.get('source_files', [])},
                                              {file_id: context})['lines']
            for line in lines:
                native = cached_native.get(line['id'], {}) if context else native_lines.get(line['id'], {})
                row = part_projection(line, native.get('values') if isinstance(native, dict) else None,
                                      native_row=native, source_sha256=sources.get(file_id),
                                      geometry_source='cached_native_geometry' if context else 'saved_native_evidence')
                # Legacy quotes may predate manufacturing.features. Only the
                # verified face measurement supplies their thickness candidate.
                row['thickness_mm'] = row['weight_evidence']['thickness_mm']
                match = None
                if context:
                    try:
                        match = self.batches._preview_match(line, context['geometry'], lines)
                    except (ValueError, OSError, TypeError, KeyError):
                        pass
                if match:
                    row['geometry_url'] = prefix + 'customer-parts/' + line['id']
                    try:
                        self.batches._preview_thumbnail(context, match)
                        row['thumbnail_url'] = prefix + 'customer-parts/' + line['id'] + '/thumbnail'
                    except (ValueError, OSError, TypeError, KeyError):
                        # A missing cached image must not break the quote or
                        # imply a newly generated substitute; 3D stays usable.
                        row['thumbnail_url'] = None
                    row['dimensions_mm'] = match['bounds_mm']['size']
                    representatives = set(match['representative_solid_ids'])
                    bodies = [solid for solid in context['geometry']['solids'] if solid['id'] in representatives]
                    try:
                        self._cached_part_step(context, match)
                        row['step_url'] = prefix + 'customer-parts/' + line['id'] + '/step'
                    except (ValueError, OSError, TypeError, KeyError, StopIteration):
                        # Only a registered and verified single-solid derivative
                        # may be advertised; never substitute the input assembly.
                        row['step_url'] = None
                    row['bends'] = [item for solid in bodies for item in bend_projection(solid.get('bend_features'))]
                    if len(bodies) == 1 and isinstance(bodies[0].get('laser_cut'), dict) and bodies[0]['laser_cut'].get('status') == 'ready':
                        drawing = prefix + 'manufacturing/' + line['id'] + '/'
                        row['laser'] = {'status': 'ready', 'svg_url': drawing + 'laser.svg', 'dxf_url': drawing + 'laser.dxf'}
                parts.append(row)
        order = {line['id']: index for index, line in enumerate(result['lines'])}
        parts.sort(key=lambda row: order[row['id']])
        summary = result.get('manufacturing') if isinstance(result.get('manufacturing'), dict) else {}
        plan = self.batches.process_pricing.get(batch_id, run_id)
        plan_lines = {row['id']: row for row in plan['lines']}
        for part in parts:
            proposed = plan_lines.get(part['id'])
            if proposed and proposed.get('quantities'):
                part['quotation_quantities'] = {key: proposed[key] for key in
                    ('status', 'method', 'quantities', 'quantity_statuses', 'measurements', 'basis', 'manufacturing_release')}
        preparation = {'can_estimate': plan['can_estimate'], 'status': plan['status'],
            'scope_label': plan['scope_label'], 'pending_parts': sum(bool(row['blockers']) for row in plan['lines']),
            'estimated_parts': sum(row['status'] == 'estimated' for row in plan['lines']),
            'measured_parts': sum(row['status'] == 'measured' for row in plan['lines'])}
        return {'batch_id': batch_id, 'run_id': run_id, 'status': summary.get('status', 'blocked'),
                'quote_preparation': preparation,
                'weight_summary': weight_summary(result),
                'summary': {'parts_count': len(parts),
                            'quantity_total': sum(row['quantity_total'] for row in parts if type(row['quantity_total']) is int),
                            'estimated_parts': sum(row['status'] == 'estimated' for row in parts),
                            'blocked_parts': sum(row['status'] != 'estimated' for row in parts)},
                'parts': parts}

    def manufacturing_drawing(self, owner, batch_id, run_id, line_id, filename):
        """Serve only an owner-bound native laser drawing, never a disk path."""
        self._owned(owner, batch_id)
        return self._manufacturing_drawing(batch_id, run_id, line_id, filename)

    def staff_manufacturing_drawing(self, batch_id, run_id, line_id, filename):
        """Operator access is enforced by the protected admin route."""
        return self._manufacturing_drawing(batch_id, run_id, line_id, filename)

    def _manufacturing_drawing(self, batch_id, run_id, line_id, filename):
        demand(isinstance(line_id, str) and LINE_ID.fullmatch(line_id)
               and filename in {'laser.svg', 'laser.dxf'}, '工艺图不存在。', 'not_found', 404)
        result, files, matches = self.batches._preview_context(batch_id, run_id, line_id)
        line = next(row for row in result['lines'] if row['id'] == line_id)
        match = matches.get(line_id)
        demand(isinstance(match, dict) and len(match.get('representative_solid_ids', [])) == 1,
               '此零件尚没有可用的单件平板切割图。', 'not_found', 404)
        context = files[line['file_id']]
        solid_id = match['representative_solid_ids'][0]
        solid = next(row for row in context['geometry']['solids'] if row['id'] == solid_id)
        laser = solid.get('laser_cut')
        demand(isinstance(laser, dict) and laser.get('status') == 'ready',
               '此零件尚没有已验证的平板切割图。', 'not_found', 404)
        if filename == 'laser.svg':
            from customer_manufacturing_view import laser_svg
            return laser_svg(laser, line['part_name']), 'image/svg+xml', line_id + '-laser.svg'
        asset = laser.get('dxf_file')
        demand(asset == solid_id + '-cut.dxf', '切割文件尚未登记。', 'not_found', 404)
        content, mime = self.batches.manufacturing.file(context['native_id'], asset)
        demand(mime == 'application/dxf' and isinstance(content, bytes), '切割文件不可用。', 'not_found', 404)
        return content, mime, line_id + '-laser.dxf'

    def customer_part_geometry(self, owner, batch_id, run_id, line_id):
        self._owned(owner, batch_id)
        value = self.batches.customer_part_geometry(batch_id, run_id, line_id)
        value.pop('shared_geometry_url', None)
        return value

    def part_step(self, owner, batch_id, run_id, line_id):
        """Read this owner's registered, verified single-part STEP only."""
        self._owned(owner, batch_id)
        return self._part_step(batch_id, run_id, line_id)

    def staff_part_step(self, batch_id, run_id, line_id):
        """Called only by the Nginx-protected operator namespace."""
        return self._part_step(batch_id, run_id, line_id)

    def _part_step(self, batch_id, run_id, line_id):
        demand(isinstance(line_id, str) and LINE_ID.fullmatch(line_id), '单件STEP不存在。', 'not_found', 404)
        result, files, matches = self.batches._preview_context(batch_id, run_id, line_id)
        line = next(row for row in result['lines'] if row['id'] == line_id)
        content = self._cached_part_step(files.get(line.get('file_id')), matches.get(line_id))
        return content, 'application/step', line_id + '.step'

    def _cached_part_step(self, context, match):
        """Reuse source matching plus the native store's registered-file hashes."""
        demand(isinstance(context, dict) and isinstance(match, dict)
               and isinstance(match.get('representative_solid_ids'), list)
               and len(match['representative_solid_ids']) == 1,
               '此分组尚没有已校验的单件STEP，未用总装文件替代。', 'not_found', 404)
        identity = match['representative_solid_ids'][0]
        solid = next((row for row in context['geometry']['solids'] if row['id'] == identity), None)
        proof = solid.get('export_validation') if isinstance(solid, dict) else None
        demand(isinstance(proof, dict) and proof.get('status') == 'engineering_draft'
               and proof.get('solid_count') == 1 and solid.get('step_file') == identity + '.step',
               '本件STEP尚未通过导出回读校验。', 'not_found', 404)
        # CadManufacturingService.file checks original identity, registration
        # and exact artifact hash. No export, CAD execution or pricing occurs.
        try:
            content, _mime = self.batches.manufacturing.file(context['native_id'], solid['step_file'])
        except (ValueError, OSError, TypeError, KeyError):
            raise WorkbenchError('已登记的单件STEP暂不可用，请联系工厂核对导出结果。', 'not_found', 404) from None
        demand(isinstance(content, bytes) and 0 < len(content) <= 20 * 1024 * 1024
               and re.match(rb'\s*ISO-10303-21\s*;', content[:512].lstrip(b'\xef\xbb\xbf'), re.I),
               '已登记的单件STEP内容不可用。', 'not_found', 404)
        return content

    def part_thumbnail(self, owner, batch_id, run_id, line_id):
        self._owned(owner, batch_id)
        return self.batches.part_thumbnail(batch_id, run_id, line_id)
