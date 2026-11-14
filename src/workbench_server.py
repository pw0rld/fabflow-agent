#!/usr/bin/env python3
"""Same-origin UI/API behind the existing Nginx authentication boundary."""
from __future__ import annotations

import argparse
import base64
import hashlib
from decimal import DecimalException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading
from urllib.parse import parse_qs, unquote, urlsplit, quote

from prep_record import RecordError, identifier, only_keys, require
from workbench_store import Workbench, WorkbenchError, MAX_UPLOAD, calculation_message
from cad_documents import CadDocumentStore, MAX_STEP_BYTES
from cad_manufacturing import CadManufacturingService
from batch_quote_service import BatchQuoteService, MAX_BATCH_BYTES as MAX_PACKAGE_BYTES
from batch_quote_cases import ProtectedCaseQuoteService, API_PREFIX as CASE_API
from customer_upload_service import (CustomerBatchQuoteService, CustomerUploadService,
                                     API_PREFIX as CUSTOMER_API, ADMIN_API_PREFIX as CUSTOMER_ADMIN_API)
from workbench_access import (IDENTITY_PATH, LOGIN_PATH, staff_identity, login_destination,
                              legacy_quotation_login, legacy_customer_root)
from customer_upload_quota import MAX_BYTES as MAX_CUSTOMER_STEP_BYTES

APP_ROOT = Path(__file__).resolve().parents[1]
API = '/workbench/api'
MAX_JSON = 1024 * 1024
MAX_COST_SHEET_JSON = 4 * 1024 * 1024
STATIC = {'/workbench/': ('index.html', 'text/html; charset=utf-8'),
          '/workbench/index.html': ('index.html', 'text/html; charset=utf-8'),
          '/workbench/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/workbench/styles.css': ('styles.css', 'text/css; charset=utf-8'),
          '/workbench/recognition.js': ('recognition.js', 'text/javascript; charset=utf-8'),
          '/workbench/recognition.css': ('recognition.css', 'text/css; charset=utf-8'),
          '/workbench/part-visuals.js': ('part-visuals.js', 'text/javascript; charset=utf-8'),
          '/workbench/part-visuals.css': ('part-visuals.css', 'text/css; charset=utf-8')}
STATIC.update({'/workbench/orders/': ('orders/index.html', 'text/html; charset=utf-8'),
               '/workbench/orders/orders.js': ('orders/orders.js', 'text/javascript; charset=utf-8'),
               '/workbench/orders/orders.css': ('orders/orders.css', 'text/css; charset=utf-8')})
STATIC['/workbench/customer/print.js'] = ('customer-print.js', 'text/javascript; charset=utf-8')
STATIC['/workbench/legacy/'] = ('index.html', 'text/html; charset=utf-8')
FRONTEND_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
                  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
                  '.txt': 'text/plain; charset=utf-8', '.png': 'image/png', '.jpg': 'image/jpeg',
                  '.jpeg': 'image/jpeg', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
                  '.woff2': 'font/woff2', '.woff': 'font/woff', '.webp': 'image/webp'}
# Only bundled export assets are registered; neither request nor API data can
# choose a disk path. The old screen remains available for existing bookmarks.
for asset in (APP_ROOT / 'web/demo').rglob('*'):
    if asset.is_file() and asset.suffix in FRONTEND_TYPES and not any(
            item.is_symlink() for item in (asset, *asset.parents)):
        relative = asset.relative_to(APP_ROOT / 'web/demo').as_posix()
        route = '/workbench/' if relative == 'index.html' else '/workbench/' + relative
        STATIC[route] = ('demo/' + relative, FRONTEND_TYPES[asset.suffix])
if (APP_ROOT / 'web/demo/index.html').is_file():
    STATIC['/workbench/index.html'] = ('demo/index.html', 'text/html; charset=utf-8')
if (APP_ROOT / 'web/demo/recognition/index.html').is_file():
    STATIC['/workbench/recognition/'] = ('demo/recognition/index.html', 'text/html; charset=utf-8')
if (APP_ROOT / 'web/demo/cad/index.html').is_file():
    STATIC['/workbench/cad/'] = ('demo/cad/index.html', 'text/html; charset=utf-8')
if (APP_ROOT / 'web/demo/quotation/index.html').is_file():
    STATIC['/workbench/quotation/'] = ('demo/quotation/index.html', 'text/html; charset=utf-8')
if (APP_ROOT / 'web/demo/quotation/part/index.html').is_file():
    STATIC['/workbench/quotation/part/'] = ('demo/quotation/part/index.html', 'text/html; charset=utf-8')
if (APP_ROOT / 'web/demo/customer/index.html').is_file():
    STATIC['/workbench/customer/'] = ('demo/customer/index.html', 'text/html; charset=utf-8')
if (APP_ROOT / 'web/demo/customer/part/index.html').is_file():
    STATIC['/workbench/customer/part/'] = ('demo/customer/part/index.html', 'text/html; charset=utf-8')
if (APP_ROOT / 'web/demo/experience/index.html').is_file():
    STATIC['/workbench/experience/'] = ('demo/experience/index.html', 'text/html; charset=utf-8')
CAD_TYPES = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
             '.css': 'text/css; charset=utf-8', '.wasm': 'application/wasm',
             '.json': 'application/json; charset=utf-8', '.txt': 'text/plain; charset=utf-8',
             '.md': 'text/plain; charset=utf-8'}
# A fixed map of bundled public assets; request paths never become filesystem paths.
for asset in (APP_ROOT / 'web/cad-preview').rglob('*'):
    asset_type = CAD_TYPES.get(asset.suffix) or ('text/plain; charset=utf-8' if asset.name in {'COPYING', 'LICENSE'} else None)
    if asset_type and asset.is_file() and not any(
            item.is_symlink() for item in (asset, *asset.parents)):
        relative = asset.relative_to(APP_ROOT / 'web').as_posix()
        STATIC['/workbench/' + relative] = (relative, asset_type)


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = 'HaoyunlaiWorkbench'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        # Do not log uploaded content, inquiry text or authentication headers.
        pass

    def _send(self, status, content=b'', content_type='application/json; charset=utf-8', *, filename=None, attachment=False, csp=None, extra_headers=None, filename_fallback=None):
        if filename_fallback is not None:
            require(isinstance(filename_fallback, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,119}', filename_fallback),
                    '下载文件名无效。')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.send_header('Cache-Control', 'private, no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Content-Security-Policy', csp or "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "img-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'")
        if filename:
            disposition = 'attachment' if attachment else 'inline'
            fallback = '; filename="' + filename_fallback + '"' if filename_fallback is not None else ''
            self.send_header('Content-Disposition', disposition + fallback + "; filename*=UTF-8''" + quote(filename, safe=''))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(content)

    def _json(self, value, status=200):
        self._send(status, json.dumps(value, ensure_ascii=False, allow_nan=False).encode())

    def _geometry_response(self, operation):
        # Native previews can expand compact mesh files into several large
        # Python/JSON objects. Admit one such request for this entire server,
        # including its response write; ordinary status/upload requests bypass
        # this gate. Reject instead of accumulating waiting request threads.
        gate = self.server.geometry_request_gate
        if not gate.acquire(blocking=False):
            content = json.dumps({'error': {'code': 'geometry_busy',
                'message': '模型预览正在读取，请稍后手动重试。已保存的资料会保留。'}},
                ensure_ascii=False).encode()
            return self._send(503, content, extra_headers={'Retry-After': '2'})
        try:
            return operation()
        finally:
            gate.release()

    def _geometry_json(self, producer):
        return self._geometry_response(lambda: self._json(producer()))

    def _geometry_file(self, producer, *, attachment=False, ascii_filename=False):
        def send():
            content, mime, filename = producer()
            return self._send(200, content, mime, filename=filename, attachment=attachment,
                              filename_fallback=filename if ascii_filename else None)
        return self._geometry_response(send)

    def _body(self, limit=MAX_JSON):
        raw_length = self.headers.get('Content-Length')
        if not raw_length or not raw_length.isdigit() or self.headers.get('Transfer-Encoding'):
            raise WorkbenchError('请求长度无效。', 'invalid_length', 400)
        length = int(raw_length)
        if length > limit:
            self.close_connection = True
            raise WorkbenchError('文件或提交内容过大。', 'too_large', 413)
        content = self.rfile.read(length)
        require(len(content) == length, '请求内容不完整。')
        return content

    def _json_body(self, limit=MAX_JSON):
        require(self.headers.get('Content-Type', '').split(';', 1)[0] == 'application/json', '请提交 JSON 表单。')
        def reject_constant(value):
            raise ValueError('nonfinite JSON number')
        result = json.loads(self._body(limit), parse_constant=reject_constant)
        require(isinstance(result, dict), '请求必须为对象。')
        return result

    def _origin(self):
        # All mutating requests require the same-origin fetch marker. Together
        # with JSON/raw-body headers and no CORS this rejects cross-site forms.
        if self.headers.get('X-Workbench-Request') != '1':
            raise WorkbenchError('请从报价工作台提交。', 'invalid_origin', 403)
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise WorkbenchError('不接受跨站提交。', 'invalid_origin', 403)
        origin = self.headers.get('Origin')
        if origin:
            parsed = urlsplit(origin)
            expected = self.headers.get('Host', '')
            forwarded = self.headers.get('X-Forwarded-Host')
            # Nginx overwrites X-Forwarded-Host; service is loopback-only.
            if parsed.netloc not in {expected, forwarded} or parsed.scheme not in {'http', 'https'}:
                raise WorkbenchError('请求来源与工作台不一致。', 'invalid_origin', 403)

    def _quote_get(self, service, parts):
        if len(parts) == 1:
            return self._json({'batches': service.list()})
        if parts == ['quote-batches', 'pricing']:
            from batch_material_prices import pricing_catalog
            return self._json(pricing_catalog())
        if parts == ['quote-batches', 'case'] and service is self.server.case_quotes:
            return self._json(service.metadata())
        if len(parts) == 2:
            return self._json(service.get(parts[1]))
        if len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'engineering':
            return self._json(service.engineering(parts[1], parts[3]))
        if len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'engineering-pricing':
            return self._json(service.engineering_pricing(parts[1], parts[3]))
        if len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'process-pricing':
            return self._geometry_json(lambda: service.process_pricing.get(parts[1], parts[3]))
        if len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'cost-sheet':
            return self._json(service.cost_sheet.get(parts[1], parts[3]))
        if len(parts) == 7 and parts[2] == 'runs' and parts[4:6] == ['assistant', 'proposals']:
            return self._json(service.assistant.get(parts[1], parts[3], parts[6]))
        if len(parts) == 4 and parts[2] == 'files':
            content, mime, filename = service.file(parts[1], parts[3])
            return self._send(200, content, mime, filename=filename,
                              attachment=not (mime.startswith('image/') or mime == 'application/pdf'))
        if len(parts) >= 5 and parts[2] == 'runs' and parts[4] == 'customer-parts':
            if len(parts) == 6:
                return self._geometry_json(lambda: service.customer_part_geometry(parts[1], parts[3], parts[5]))
            if len(parts) == 7 and parts[6] == 'thumbnail':
                content, mime, filename = service.part_thumbnail(parts[1], parts[3], parts[5])
                return self._send(200, content, mime, filename=filename)
            raise FileNotFoundError()
        if len(parts) >= 5 and parts[2] == 'runs' and parts[4] == 'parts':
            if len(parts) == 5:
                return self._geometry_json(lambda: service.parts_manifest(parts[1], parts[3]))
            if len(parts) == 6:
                return self._geometry_json(lambda: service.part_geometry(parts[1], parts[3], parts[5]))
            if len(parts) == 7 and parts[6] == 'thumbnail':
                content, mime, filename = service.part_thumbnail(parts[1], parts[3], parts[5])
                return self._send(200, content, mime, filename=filename)
            raise FileNotFoundError()
        if len(parts) == 5 and parts[2] == 'runs':
            if parts[4] in {'customer-quote.html', 'customer-print.html', 'quote.html', 'quote-print.html'}:
                return self._geometry_file(lambda: service.artifact(parts[1], parts[3], parts[4]))
            content, mime, filename = service.artifact(parts[1], parts[3], parts[4])
            return self._send(200, content, mime, filename=filename,
                              attachment=parts[4] not in {'quote.html', 'quote-print.html', 'preview-summary.html',
                                                        'preview-source.html', 'preview-cost.html', 'preview-record.html',
                                                        'engineering-bom.html', 'materials.html', 'processes.html',
                                                        'customer-quote.html', 'customer-print.html', 'internal-cost.html'})
        raise FileNotFoundError()

    def _case_post(self, url):
        # Nginx authenticates this exact namespace. Query strings or headers
        # never select a private store from an otherwise public request.
        if url.query or '%' in url.path:
            raise FileNotFoundError()
        suffix = url.path[len(CASE_API):]
        parts = ['quote-batches'] + suffix[1:].split('/') if suffix.startswith('/') else ['quote-batches']
        service = self.server.case_quotes
        if len(parts) == 6 and parts[2] == 'runs' and parts[4] == 'assistant':
            payload = self._json_body()
            if parts[5] == 'preview':
                return self._json(service.assistant.enqueue(parts[1], parts[3], payload), 202)
            if parts[5] == 'commit':
                return self._json(service.assistant.commit(parts[1], parts[3], payload), 201)
            raise FileNotFoundError()
        if len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'engineering':
            return self._json(service.save_engineering(parts[1], parts[3], self._json_body()))
        if len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'cost-sheet':
            return self._json(service.cost_sheet.reprice(parts[1], parts[3], self._json_body(MAX_COST_SHEET_JSON)), 201)
        legacy_pricing = (len(parts) == 3 and parts[2] in {'start', 'retry'}
                          or len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'engineering-pricing')
        if legacy_pricing and isinstance((service.get(parts[1]).get('result') or {}).get('cost_sheet'), dict):
            raise WorkbenchError('该版本已采用内部核价单，请在核价单调整并另存新版本。', 'cost_sheet_required', 409)
        raise WorkbenchError('此入口复用已解析案例；上传新订单请返回公开报价工作台。', 'case_read_only', 405)

    def _customer_route(self, url, *, post=False):
        if '%' in url.path:
            raise FileNotFoundError()
        portal = self.server.customer_upload
        suffix = url.path[len(CUSTOMER_API):]
        if post and suffix == '/session':
            if url.query or self._json_body() != {}:
                raise WorkbenchError('会话请求无效。', 'invalid_request', 400)
            try:
                portal.session(self.headers.get('Cookie', ''))
                return self._json({'ready': True})
            except WorkbenchError as error:
                if error.status != 401:
                    raise
            token, _ = portal.new_session()
            return self._send(200, b'{"ready":true}', extra_headers={'Set-Cookie': portal.cookie_header(token)})
        owner = portal.session(self.headers.get('Cookie', ''))
        parts = suffix[1:].split('/') if suffix.startswith('/') else []
        if not post and parts == ['quota']:
            if url.query:
                raise FileNotFoundError()
            return self._json(portal.quota(owner))
        if parts == ['orders']:
            if not post:
                if url.query:
                    raise FileNotFoundError()
                return self._json(portal.list(owner))
            query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
            require(set(query) == {'name'} and len(query['name']) == 1, '文件名无效。')
            if self.headers.get('Content-Type', '').split(';', 1)[0] != 'application/octet-stream':
                raise WorkbenchError('每次只能上传一份 STEP/STP 原文件，不接收多文件表单或压缩包。', 'customer_step_only', 415)
            return self._json(portal.upload(owner, query['name'][0], self._body(MAX_CUSTOMER_STEP_BYTES), self.headers.get('X-Event-Id')), 201)
        if url.query or not parts or parts[0] != 'orders':
            raise FileNotFoundError()
        if len(parts) == 2 and not post:
            return self._json(portal.get(owner, parts[1]))
        if post and len(parts) == 3 and parts[2] in {'start', 'retry'}:
            return self._json(getattr(portal, parts[2])(owner, parts[1], self._json_body()), 202)
        if not post and len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'manufacturing':
            portal._owned(owner, parts[1])
            return self._geometry_json(lambda: portal.manufacturing(owner, parts[1], parts[3]))
        if len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'process-pricing':
            if not post:
                portal._owned(owner, parts[1])
                return self._geometry_json(lambda: portal.process_pricing(owner, parts[1], parts[3], None))
            return self._json(portal.process_pricing(owner, parts[1], parts[3], self._json_body()), 201)
        if not post and len(parts) == 7 and parts[2] == 'runs' and parts[4] == 'manufacturing':
            portal._owned(owner, parts[1])
            return self._geometry_file(
                lambda: portal.manufacturing_drawing(owner, parts[1], parts[3], parts[5], parts[6]),
                attachment=parts[6].endswith('.dxf'))
        if not post and len(parts) == 5 and parts[2] == 'runs':
            if parts[4] in {'customer-quote.html', 'customer-print.html'}:
                portal._owned(owner, parts[1])
                return self._geometry_file(lambda: portal.artifact(owner, parts[1], parts[3], parts[4]))
            content, mime, filename = portal.artifact(owner, parts[1], parts[3], parts[4])
            return self._send(200, content, mime, filename=filename)
        if not post and len(parts) >= 6 and parts[2] == 'runs' and parts[4] == 'customer-parts':
            if len(parts) == 6:
                portal._owned(owner, parts[1])
                return self._geometry_json(lambda: portal.customer_part_geometry(owner, parts[1], parts[3], parts[5]))
            if len(parts) == 7 and parts[6] == 'thumbnail':
                content, mime, filename = portal.part_thumbnail(owner, parts[1], parts[3], parts[5])
                return self._send(200, content, mime, filename=filename)
            if len(parts) == 7 and parts[6] == 'step':
                portal._owned(owner, parts[1])
                return self._geometry_file(lambda: portal.part_step(owner, parts[1], parts[3], parts[5]),
                                           attachment=True, ascii_filename=True)
        raise FileNotFoundError()

    def _customer_admin(self, url, *, post=False):
        # The deployed Nginx authenticates this exact staff namespace.
        if url.query or '%' in url.path:
            raise FileNotFoundError()
        suffix = url.path[len(CUSTOMER_ADMIN_API):]
        parts = ['quote-batches'] + suffix[1:].split('/') if suffix.startswith('/') else ['quote-batches']
        service = self.server.customer_batches
        if not post:
            if len(parts) == 5 and parts[2] == 'runs' and parts[4] in {'customer-quote.html', 'customer-print.html'}:
                return self._geometry_file(
                    lambda: self.server.customer_upload.staff_artifact(parts[1], parts[3], parts[4]))
            if len(parts) == 7 and parts[2] == 'runs' and parts[4] == 'customer-parts' and parts[6] == 'step':
                return self._geometry_file(
                    lambda: self.server.customer_upload.staff_part_step(parts[1], parts[3], parts[5]),
                    attachment=True, ascii_filename=True)
            if len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'manufacturing':
                return self._geometry_json(lambda: self.server.customer_upload.staff_manufacturing(parts[1], parts[3]))
            if len(parts) == 7 and parts[2] == 'runs' and parts[4] == 'manufacturing':
                return self._geometry_file(
                    lambda: self.server.customer_upload.staff_manufacturing_drawing(parts[1], parts[3], parts[5], parts[6]),
                    attachment=parts[6].endswith('.dxf'))
            return self._quote_get(service, parts)
        if len(parts) == 3 and parts[2] in {'start', 'retry'}:
            return self._json(getattr(service, parts[2])(parts[1], self._json_body()), 202)
        if len(parts) == 5 and parts[2] == 'runs':
            if parts[4] == 'process-pricing':
                return self._json(service.process_pricing.apply(parts[1], parts[3], self._json_body()), 201)
            if parts[4] == 'cost-sheet':
                return self._json(service.cost_sheet.reprice(parts[1], parts[3], self._json_body(MAX_COST_SHEET_JSON)), 201)
            if parts[4] == 'engineering':
                return self._json(service.save_engineering(parts[1], parts[3], self._json_body()))
            if parts[4] == 'engineering-pricing':
                return self._json(service.price_engineering(parts[1], parts[3], self._json_body()), 201)
        raise FileNotFoundError()

    def _get(self):
        url = urlsplit(self.path)
        path = url.path
        app = self.server.app
        # These exact endpoints rely on Nginx Basic before reaching this
        # loopback-only server. Customer cookies and client role hints never
        # grant operator access. Only identity has an anonymous Nginx fallback.
        if path == IDENTITY_PATH:
            if url.query or url.fragment:
                raise FileNotFoundError()
            return self._json(staff_identity())
        if path == LOGIN_PATH:
            return self._send(303, b'', extra_headers={'Location': login_destination(url.query)})
        if path == CUSTOMER_API or path.startswith(CUSTOMER_API + '/'):
            return self._customer_route(url)
        if path == CUSTOMER_ADMIN_API or path.startswith(CUSTOMER_ADMIN_API + '/'):
            return self._customer_admin(url)
        if path in {'/workbench/customer', '/workbench/customer/', '/workbench/customer/index.html'}:
            return self._send(308, b'', extra_headers={'Location': legacy_customer_root(url.query)})
        if path in {'/workbench/quotation', '/workbench/quotation/', '/workbench/quotation/index.html'}:
            return self._send(303, b'', extra_headers={'Location': legacy_quotation_login(url.query)})
        if path == '/healthz':
            return self._json({'ok': True, 'version': '1.0.0'})
        if path in {'/workbench', '/workbench/orders', '/workbench/legacy', '/workbench/recognition', '/workbench/cad', '/workbench/quotation', '/workbench/quotation/part', '/workbench/experience'}:
            self.send_response(308)
            self.send_header('Location', path + '/' + ('?' + url.query if url.query else ''))
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path in STATIC:
            name, content_type = STATIC[path]
            file = APP_ROOT / 'web' / name
            require(file.is_file() and not file.is_symlink(), '界面文件暂不可用。')
            csp = None
            if name.startswith('demo/') and content_type.startswith('text/html'):
                # Next's static export needs small inline hydration records.
                # Hash only bundled script bytes, without enabling arbitrary
                # inline scripts or eval on the business interface.
                inline = re.findall(r'<script\b[^>]*>(.*?)</script\s*>', file.read_text(), re.S | re.I)
                hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(
                    script.encode('utf-8')).digest()).decode('ascii') + "'" for script in inline if script)
                csp = ("default-src 'self'; script-src 'self' " + hashes + "; "
                       "style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; "
                       "connect-src 'self'; font-src 'self'; frame-src 'self'; "
                       "object-src 'none'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'")
            if name.startswith('cad-preview/'):
                # Upstream OCCT/LibreDWG Embind generates Functions in workers.
                # Keep that permission confined to the disposable parser worker.
                script = "'self' 'unsafe-eval'" if name in {'cad-preview/step-worker.js', 'cad-preview/dwg-worker.js'} else "'self'"
                csp = ("default-src 'self'; script-src " + script + "; worker-src 'self'; connect-src 'self'; "
                       "style-src 'self' 'unsafe-inline'; img-src 'self' blob:; object-src 'none'; "
                       "base-uri 'none'; frame-ancestors 'self'; form-action 'none'")
            return self._send(200, file.read_bytes(), content_type, csp=csp)
        if path.startswith('/quote-preview/'):
            # Also serves local development. On the deployed host Nginx keeps
            # routing this prefix to the existing read-only preview service.
            from quote_preview import PreviewStore, preview_html
            parts = path.split('/')
            if len(parts) != 5:
                raise FileNotFoundError()
            _, _, task_id, quote_id, filename = parts
            query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
            require(not query or query == {'download': ['1']}, 'invalid preview query')
            content, info = PreviewStore(app.root).document(task_id, quote_id, filename)
            if query:
                return self._send(200, content, 'text/markdown; charset=utf-8',
                                  filename=task_id + '-' + quote_id + '-' + filename, attachment=True)
            return self._send(200, preview_html(content, info, filename), 'text/html; charset=utf-8')
        if path == CASE_API or path.startswith(CASE_API + '/'):
            if url.query or '%' in path:
                raise FileNotFoundError()
            suffix = path[len(CASE_API):]
            parts = ['quote-batches'] + suffix[1:].split('/') if suffix.startswith('/') else ['quote-batches']
            return self._quote_get(self.server.case_quotes, parts)
        if not path.startswith(API + '/') or '%' in path:
            raise FileNotFoundError()
        parts = path[len(API) + 1:].split('/')
        if parts[:1] == ['quote-batches']:
            if url.query:
                raise FileNotFoundError()
            return self._quote_get(self.server.batch_quotes, parts)
        if parts[:1] == ['cad']:
            if url.query:
                raise FileNotFoundError()
            cad = self.server.cad
            if len(parts) >= 4 and parts[1] == 'documents' and parts[3] == 'quotation':
                service = self.server.cad_quotation
                if len(parts) == 4:
                    return self._json(service.get(parts[2]))
                if len(parts) == 6 and parts[4] == 'quotes':
                    return self._json(service.get_quote(parts[2], parts[5]))
                if len(parts) == 7 and parts[4] == 'quotes':
                    content, mime, download = service.document(parts[2], parts[5], parts[6])
                    return self._send(200, content, mime, filename=parts[5] + '-' + parts[6], attachment=download)
                raise FileNotFoundError()
            if len(parts) >= 4 and parts[1] == 'documents' and parts[3] == 'manufacturing':
                service = self.server.manufacturing
                if len(parts) == 4:
                    return self._json(service.get(parts[2]))
                if len(parts) == 5 and parts[4] == 'geometry':
                    return self._geometry_json(lambda: service.geometry(parts[2]))
                if len(parts) == 5 and parts[4] == 'bom.csv':
                    return self._geometry_file(lambda: (service.bom(parts[2]), 'text/csv; charset=utf-8',
                                              parts[2] + '-manufacturing-draft.csv'), attachment=True)
                if len(parts) == 6 and parts[4] == 'files':
                    if not parts[5].endswith('.png'):
                        return self._geometry_file(lambda: (*service.file(parts[2], parts[5]), parts[5]),
                                                   attachment=True)
                    content, mime = service.file(parts[2], parts[5])
                    return self._send(200, content, mime, filename=parts[5], attachment=mime != 'image/png')
                raise FileNotFoundError()
            if parts == ['cad', 'documents']:
                return self._json({'documents': cad.list()})
            if parts == ['cad', 'samples', 'enclosure', 'file']:
                sample = APP_ROOT / 'samples/public-enclosure-20261026/input/Enclosure-v3-v9.step'
                require(not sample.is_symlink(), 'CAD 样例无效。')
                return self._send(200, sample.read_bytes(), 'application/octet-stream',
                                  filename='Enclosure-v3-v9.step', attachment=True)
            if len(parts) == 3 and parts[1] == 'documents':
                return self._json(cad.get(parts[2]))
            if len(parts) == 4 and parts[1] == 'documents' and parts[3] == 'file':
                content, name = cad.file(parts[2])
                return self._send(200, content, 'application/octet-stream', filename=name, attachment=True)
            raise FileNotFoundError()
        if parts == ['tasks']:
            return self._json({'tasks': app.tasks()})
        if parts and parts[0] == 'orders':
            orders = self.server.orders
            if len(parts) == 1:
                return self._json({'orders': orders.list()})
            order_id = identifier(parts[1])
            if len(parts) == 2:
                return self._json(orders.get(order_id))
            if len(parts) == 3 and parts[2] == 'jobs':
                return self._json({'jobs': orders.jobs(order_id)})
            if len(parts) == 4 and parts[2] == 'documents':
                content, mime, name = orders.attachment(order_id, parts[3])
                return self._send(200, content, mime, filename=name, attachment=not (mime.startswith('image/') or mime == 'application/pdf'))
            if len(parts) == 4 and parts[2] == 'quotes':
                return self._json(orders.quote(order_id, parts[3]))
            if len(parts) == 4 and parts[2] == 'exports':
                return self._send(200, orders.csv_document(orders.store.get(order_id), parts[3]), 'text/csv; charset=utf-8', filename=order_id + '-' + parts[3], attachment=True)
            if len(parts) == 5 and parts[2] == 'quotes':
                content, mime, download = orders.document(order_id, parts[3], parts[4])
                return self._send(200, content, mime, filename=order_id + '-' + parts[4], attachment=download)
            raise FileNotFoundError()
        if parts == ['prices']:
            return self._json({'entries': app.catalog.list_entries()})
        if parts == ['templates']:
            return self._json({'templates': app.list_templates()})
        if len(parts) == 2 and parts[0] == 'templates':
            return self._json(app.template(identifier(parts[1])))
        if len(parts) >= 2 and parts[0] == 'tasks':
            task_id = identifier(parts[1])
            if len(parts) == 2:
                return self._json(app.task(task_id))
            if len(parts) == 3 and parts[2] == 'price-matches':
                return self._json(app.match_prices(task_id))
            if len(parts) == 4 and parts[2] == 'quotes':
                return self._json(app._quote_result(task_id, identifier(parts[3])))
            if len(parts) == 6 and parts[2] == 'quotes' and parts[4] == 'preview':
                from quote_preview import PreviewStore, preview_html
                require(parts[5] in {'quote.md', 'cost_breakdown.md'}, 'unsupported quote document')
                content, info = PreviewStore(app.root).document(task_id, identifier(parts[3]), parts[5])
                html = preview_html(content, info, parts[5]).replace(
                    b'<a href=', b'<a target="_blank" rel="noopener" href=')
                return self._send(200, html, 'text/html; charset=utf-8')
            if len(parts) == 4 and parts[2] == 'attachments':
                content, mime, name = app.attachment(task_id, parts[3])
                query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
                require(not query or query == {'download': ['1']}, 'invalid attachment query')
                inline = mime.startswith('image/') or mime == 'application/pdf'
                return self._send(200, content, mime, filename=name, attachment=bool(query) or not inline)
            if len(parts) == 5 and parts[2] == 'reading-pages':
                query = parse_qs(url.query, keep_blank_values=True)
                require(set(query) == {'run_id'} and len(query['run_id']) == 1, 'reading page run required')
                page_text = parts[4]
                require(page_text.endswith('.png') and page_text[:-4].isdigit(), 'invalid reading page')
                content = app.reading_page(task_id, identifier(parts[3]), int(page_text[:-4]), identifier(query['run_id'][0]))
                return self._send(200, content, 'image/png')
        raise FileNotFoundError()

    def _post(self):
        self._origin()
        url = urlsplit(self.path)
        if url.path == CUSTOMER_API or url.path.startswith(CUSTOMER_API + '/'):
            return self._customer_route(url, post=True)
        if url.path == CUSTOMER_ADMIN_API or url.path.startswith(CUSTOMER_ADMIN_API + '/'):
            return self._customer_admin(url, post=True)
        if url.path == CASE_API or url.path.startswith(CASE_API + '/'):
            return self._case_post(url)
        if not url.path.startswith(API + '/') or '%' in url.path:
            raise FileNotFoundError()
        parts = url.path[len(API) + 1:].split('/')
        app = self.server.app
        if parts == ['quote-batches']:
            query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
            require(set(query) == {'name'} and len(query['name']) == 1, '文件名无效。')
            require(self.headers.get('Content-Type', '').split(';', 1)[0] in
                    {'application/octet-stream', 'application/zip', 'application/x-zip-compressed',
                     'application/vnd.rar', 'application/x-rar-compressed'},
                    '请上传 ZIP、RAR 或 STEP 原文件。')
            return self._json(self.server.batch_quotes.create(query['name'][0],
                self._body(MAX_PACKAGE_BYTES), self.headers.get('X-Event-Id')), 201)
        if parts == ['cad', 'documents']:
            query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
            require(set(query) == {'name'} and len(query['name']) == 1, '文件名无效。')
            return self._json(self.server.cad.create(query['name'][0], self._body(MAX_STEP_BYTES)), 201)
        if len(parts) == 3 and parts[0] == 'orders' and parts[2] == 'documents':
            query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
            require(set(query) == {'name'} and len(query['name']) == 1, '文件名无效。')
            revision = self.headers.get('X-Expected-Revision', '')
            require(revision.isdigit(), '请重新载入订单修订。')
            return self._json(self.server.orders.upload(identifier(parts[1]), self.headers.get('X-Event-Id'), int(revision), query['name'][0], self._body(MAX_UPLOAD)))
        if len(parts) == 3 and parts[0] == 'tasks' and parts[2] == 'attachments':
            query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
            require(set(query) == {'name'} and len(query['name']) == 1, '文件名无效。')
            revision = self.headers.get('X-Expected-Revision', '')
            require(revision.isdigit(), '请重新载入资料修订。')
            return self._json(app.upload(identifier(parts[1]), self.headers.get('X-Event-Id'), int(revision),
                                         query['name'][0], self._body(MAX_UPLOAD)))
        cost_sheet_request = parts[:1] == ['quote-batches'] and len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'cost-sheet'
        payload = self._json_body(MAX_COST_SHEET_JSON if cost_sheet_request else MAX_JSON)
        if parts[:1] == ['quote-batches']:
            if url.query:
                raise FileNotFoundError()
            if len(parts) == 6 and parts[2] == 'runs' and parts[4] == 'assistant':
                if parts[5] == 'preview':
                    return self._json(self.server.batch_quotes.assistant.enqueue(parts[1], parts[3], payload), 202)
                if parts[5] == 'commit':
                    return self._json(self.server.batch_quotes.assistant.commit(parts[1], parts[3], payload), 201)
                raise FileNotFoundError()
            if len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'engineering':
                return self._json(self.server.batch_quotes.save_engineering(parts[1], parts[3], payload))
            if len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'cost-sheet':
                return self._json(self.server.batch_quotes.cost_sheet.reprice(parts[1], parts[3], payload), 201)
            if len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'engineering-pricing':
                return self._json(self.server.batch_quotes.price_engineering(parts[1], parts[3], payload), 201)
            if len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'process-pricing':
                return self._json(self.server.batch_quotes.process_pricing.apply(parts[1], parts[3], payload), 201)
            if len(parts) == 5 and parts[2] == 'runs' and parts[4] == 'engineering-recognize':
                return self._json(self.server.batch_quotes.recognize_engineering(parts[1], parts[3], payload), 202)
            if len(parts) == 3 and parts[2] in {'start', 'retry'}:
                method = self.server.batch_quotes.start if parts[2] == 'start' else self.server.batch_quotes.retry
                return self._json(method(parts[1], payload), 202)
            raise FileNotFoundError()
        if url.query:
            raise FileNotFoundError()
        if parts[:1] == ['cad']:
            if len(parts) == 5 and parts[1] == 'documents' and parts[3] == 'quotation':
                service = self.server.cad_quotation
                methods = {'inputs': service.save_inputs, 'estimate': service.estimate,
                           'quotes': service.issue, 'demo': service.demo_inputs,
                           'rebase': service.rebase}
                if parts[4] in methods:
                    return self._json(methods[parts[4]](parts[2], payload), 201 if parts[4] == 'quotes' else 200)
            if len(parts) == 5 and parts[1] == 'documents' and parts[3] == 'manufacturing':
                if parts[4] == 'analyze':
                    return self._json(self.server.manufacturing.start(parts[2], payload), 202)
                if parts[4] == 'thickness':
                    return self._json(self.server.manufacturing.measure_thickness(parts[2], payload), 202)
                if parts[4] == 'properties':
                    return self._json(self.server.manufacturing.read_properties(parts[2], payload), 202)
                if parts[4] == 'laser':
                    return self._json(self.server.manufacturing.generate_laser(parts[2], payload), 202)
                if parts[4] == 'bends':
                    return self._json(self.server.manufacturing.identify_bends(parts[2], payload), 202)
                if parts[4] == 'review':
                    return self._json(self.server.manufacturing.review(parts[2], payload))
            if len(parts) == 4 and parts[1] == 'documents' and parts[3] in {'snapshot', 'view'}:
                return self._json(self.server.cad.update(parts[2], parts[3], payload))
            raise FileNotFoundError()
        if parts == ['tasks']:
            return self._json(app.create(payload), 201)
        if parts and parts[0] == 'orders':
            orders = self.server.orders
            if len(parts) == 1:
                return self._json(orders.create(payload), 201)
            order_id = identifier(parts[1])
            if len(parts) == 3 and parts[2] in {'structure', 'discover', 'prepare', 'quotes'}:
                method = {'structure': orders.replace, 'discover': orders.discover, 'prepare': orders.prepare, 'quotes': orders.issue}[parts[2]]
                return self._json(method(order_id, payload), 202 if parts[2] == 'discover' else 201 if parts[2] == 'quotes' else 200)
            if len(parts) == 5 and parts[2] == 'jobs' and parts[4] == 'stop':
                return self._json(orders.stop(order_id, identifier(parts[3])), 202)
            raise FileNotFoundError()
        if parts == ['templates']:
            return self._json(app.create_template(payload), 201)
        if len(parts) == 3 and parts[0] == 'templates' and parts[2] == 'disable':
            return self._json(app.disable_template(identifier(parts[1]), payload))
        if parts and parts[0] == 'prices':
            event_id = identifier(payload.get('event_id'))
            data = {k: v for k, v in payload.items() if k != 'event_id'}
            if len(parts) == 1:
                return self._json(app.catalog.create(data, request_id=event_id), 201)
            if len(parts) == 3 and parts[2] == 'versions':
                return self._json(app.catalog.new_version(identifier(parts[1]), data, request_id=event_id), 201)
            if len(parts) == 4 and parts[2].isdigit() and parts[3] in {'publish', 'disable'}:
                require(not data, '价格发布/停用不接受其他修改。')
                return self._json(getattr(app.catalog, parts[3])(identifier(parts[1]), int(parts[2]), request_id=event_id))
        if len(parts) >= 3 and parts[0] == 'tasks':
            task_id = identifier(parts[1])
            if len(parts) == 3:
                if parts[2] in {'requirements', 'technical'}:
                    return self._json(app.save_fields(task_id, parts[2], payload))
                if parts[2] == 'template-preview':
                    return self._json(app.template_preview(task_id, payload))
                if parts[2] == 'read':
                    return self._json(app.start_read(task_id, payload), 202)
                if parts[2] == 'quotes':
                    return self._json(app.issue(task_id, payload), 201)
            if len(parts) == 4 and parts[2:] == ['read', 'stop']:
                return self._json(app.stop_read(task_id, payload), 202)
        raise FileNotFoundError()

    def _dispatch(self, operation):
        try:
            operation()
        except WorkbenchError as exc:
            self.close_connection = True
            self._json({'error': {'code': exc.code, 'message': str(exc), 'details': exc.details}}, exc.status)
        except FileNotFoundError:
            self.close_connection = True
            self._json({'error': {'code': 'not_found', 'message': '资料或页面不存在。'}}, 404)
        except (RecordError, ValueError, TypeError, KeyError, DecimalException) as exc:
            self.close_connection = True
            message = calculation_message(str(exc))
            status = 409 if any(x in message for x in ('revision', 'different', 'reused', '冲突', '已更新')) else 400
            self._json({'error': {'code': 'validation_error', 'message': message}}, status)
        except OSError:
            self.close_connection = True
            self._json({'error': {'code': 'storage_error', 'message': '暂时无法读取或保存，请保留本次输入后重试。'}}, 503)
        except Exception:
            self.close_connection = True
            self._json({'error': {'code': 'server_error', 'message': '服务暂不可用，请稍后重试；已保存的资料会保留。'}}, 500)

    def do_GET(self):
        self._dispatch(self._get)

    do_HEAD = do_GET

    def do_POST(self):
        self._dispatch(self._post)


def make_server(app, port=3092):
    server = ThreadingHTTPServer(('127.0.0.1', port), WorkbenchHandler)
    server.geometry_request_gate = threading.BoundedSemaphore(1)
    server.app = app
    server.cad = CadDocumentStore(app.meta / 'cad-documents')
    server.manufacturing = CadManufacturingService(server.cad)
    from cad_quotation import CadQuotationService
    server.cad_quotation = CadQuotationService(server.manufacturing, app.catalog)
    batch_cad = CadDocumentStore(app.meta / 'batch-cad-documents')
    batch_manufacturing = CadManufacturingService(batch_cad)
    server.batch_quotes = BatchQuoteService(app.meta / 'batch-quotes', batch_cad, batch_manufacturing)
    server.case_quotes = ProtectedCaseQuoteService(app.meta / 'protected-case-quotes')
    customer_cad = CadDocumentStore(app.meta / 'customer-cad-documents')
    customer_manufacturing = CadManufacturingService(customer_cad)
    server.customer_batches = CustomerBatchQuoteService(app.meta / 'customer-quotes', customer_cad, customer_manufacturing)
    server.customer_upload = CustomerUploadService(app.meta / 'customer-upload-access', server.customer_batches)
    from order_service import OrderService
    server.orders = OrderService(app)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--price-db', type=Path, required=True)
    parser.add_argument('--reader-root', type=Path, required=True)
    from agent_bridge import AgentBridge, selected_backend
    parser.add_argument('--agent-backend', choices=('openai-agents', 'dsh'),
                        default='dsh' if selected_backend() == 'dsh' else 'openai-agents',
                        help='AI executor; default is the in-process OpenAI Agents SDK')
    parser.add_argument('--dsh-url', default='http://127.0.0.1:3080',
                        help='Legacy URL, used only with --agent-backend=dsh')
    parser.add_argument('--port', type=int, default=3092)
    args = parser.parse_args(argv)
    from price_catalog import PriceCatalog
    os.environ['FABFLOW_AGENT_BACKEND'] = selected_backend(args.agent_backend)
    legacy_url = args.dsh_url if args.agent_backend == 'dsh' else None
    app = Workbench(args.root, catalog=PriceCatalog(args.price_db), reader_root=args.reader_root,
                    bridge_factory=lambda workspace: AgentBridge(legacy_url, workspace))
    app.resume_reads()
    with make_server(app, args.port) as server:
        # Existing recognition records derive their workspace from this root.
        # Keep historical roots so restart inspects admitted sessions in place.
        server.batch_quotes.engineering_recognition.configure(app.reader_root / 'batch-engineering', legacy_url)
        server.case_quotes.engineering_recognition.configure(app.reader_root / 'protected-case-engineering', legacy_url)
        server.customer_batches.engineering_recognition.configure(
            server.customer_batches.root / '.engineering-readers', legacy_url)
        from cad_manufacturing_ai import suggest
        server.manufacturing.analyzer = lambda workspace, geometry: suggest(
            workspace, geometry, legacy_url, reader_root=app.reader_root / 'cad-manufacturing')
        from order_discovery import DiscoveryBridge
        server.orders.discovery_factory = lambda workspace: DiscoveryBridge(legacy_url, workspace)
        server.orders.start()
        server.batch_quotes.start_worker()
        server.case_quotes.start_worker()
        server.customer_batches.start_worker()
        print('Quotation workbench listening on 127.0.0.1:' + str(server.server_port), flush=True)
        try:
            server.serve_forever()
        finally:
            server.batch_quotes.close()
            server.case_quotes.close()
            server.customer_batches.close()
            server.orders.close()
            from agent_runtime import shutdown_all
            shutdown_all()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
