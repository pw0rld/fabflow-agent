"""Loopback HTTP API; Nginx supplies the existing authenticated boundary."""
from __future__ import annotations
import argparse
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import importlib
import json
from pathlib import Path
import platform
import threading
from urllib.parse import parse_qs,unquote,urlsplit
from .service import SplitService,ServiceError,MAX_UPLOAD


class SplitHTTPServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,address,service,allowed_origins):
        super().__init__(address,SplitHandler)
        self.service=service;self.origins=set(allowed_origins);self.upload_slots=threading.BoundedSemaphore(4)


class SplitHandler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    server_version='DrawingSplit';sys_version=''
    def log_message(self,*args):pass

    def _headers(self,status,length,mime,download_name=None):
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(length))
        self.send_header('Cache-Control','private, no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','same-origin');self.send_header('X-Frame-Options','SAMEORIGIN')
        if download_name:self.send_header('Content-Disposition','attachment; filename="'+download_name+'"')
        if self.close_connection:self.send_header('Connection','close')
        self.end_headers()

    def _json(self,data,status=200):
        payload=json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
        self._headers(status,len(payload),'application/json; charset=utf-8')
        if self.command!='HEAD':self.wfile.write(payload)

    def _error(self,error):
        self._json({'error':{'code':error.code,'message':str(error)}},error.status)

    def _write_allowed(self):
        if self.headers.get('X-Drawing-Split-Request')!='1':raise ServiceError('缺少同源请求标记。','same_origin_required',403)
        origin=self.headers.get('Origin');site=self.headers.get('Sec-Fetch-Site')
        if not origin or origin not in self.server.origins:
            raise ServiceError('请求来源不受支持。','origin_rejected',403)
        if site and site not in {'same-origin','none'}:raise ServiceError('不接受跨站写入。','cross_site_rejected',403)

    def _body(self,limit):
        raw=self.headers.get('Content-Length')
        if not raw or not raw.isdigit() or len(self.headers.get_all('Content-Length',[]))!=1 or self.headers.get('Transfer-Encoding'):
            self.close_connection=True;raise ServiceError('请求长度无效。','invalid_length')
        length=int(raw)
        if length<1 or length>limit:
            self.close_connection=True;raise ServiceError('上传文件或提交内容过大。','too_large',413)
        self.connection.settimeout(30)
        content=self.rfile.read(length)
        if len(content)!=length:raise ServiceError('请求内容不完整。','incomplete_body')
        return content

    def _route(self):
        parsed=urlsplit(self.path);parts=[unquote(item) for item in parsed.path.strip('/').split('/') if item]
        if any('/' in item or '\\' in item or item in {'.','..'} for item in parts):raise ServiceError('路由不存在。','not_found',404)
        return parsed,parts

    def do_OPTIONS(self):
        self.close_connection=True;self._error(ServiceError('此接口不开放跨域访问。','cors_disabled',403))

    def do_HEAD(self):self.do_GET()

    def do_GET(self):
        try:
            parsed,parts=self._route()
            if parts==['health']:
                return self._json({'status':'ok','service':'drawing-split'})
            if len(parts)==2 and parts[0]=='jobs':return self._json(self.server.service.get(parts[1]))
            if len(parts)==3 and parts[0]=='jobs' and parts[2]=='review':return self._json(self.server.service.get_review(parts[1]))
            if len(parts)==3 and parts[0]=='jobs' and parts[2]=='export':
                query=parse_qs(parsed.query,keep_blank_values=True)
                if set(query)!={'revision','attempt'} or any(len(query[key])!=1 or not query[key][0].isdigit() or len(query[key][0])>8 for key in query):
                    raise ServiceError('请明确要导出的识别轮次和核对版本。','review_invalid')
                path,mime,name=self.server.service.export_review(parts[1],int(query['revision'][0]),int(query['attempt'][0]))
                self._headers(200,path.stat().st_size,mime,name)
                if self.command!='HEAD':
                    with path.open('rb') as stream:
                        while True:
                            chunk=stream.read(64*1024)
                            if not chunk:break
                            self.wfile.write(chunk)
                return
            if len(parts)==4 and parts[0]=='jobs' and parts[2]=='files':
                path,mime=self.server.service.file(parts[1],parts[3]);length=path.stat().st_size
                self._headers(200,length,mime)
                if self.command!='HEAD':
                    with path.open('rb') as stream:
                        while True:
                            chunk=stream.read(64*1024)
                            if not chunk:break
                            self.wfile.write(chunk)
                return
            raise ServiceError('路由不存在。','not_found',404)
        except ServiceError as error:self._error(error)
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:self._error(ServiceError('服务暂时无法读取请求。','internal_error',500))

    def do_POST(self):
        acquired=False
        try:
            self._write_allowed();parsed,parts=self._route()
            if parts==['jobs']:
                acquired=self.server.upload_slots.acquire(blocking=False)
                if not acquired:
                    self.close_connection=True;raise ServiceError('上传连接繁忙，请稍后重试。','upload_busy',429)
                data=self._body(MAX_UPLOAD);request_id=self.headers.get('X-Request-Id','')
                name=parse_qs(parsed.query).get('name',['drawing'])[0]
                return self._json(self.server.service.create(data,name,request_id),202)
            if len(parts)==3 and parts[0]=='jobs' and parts[2]=='retry':
                try:payload=json.loads(self._body(8192))
                except (ValueError,UnicodeError):raise ServiceError('重试请求格式无效。','invalid_json') from None
                if not isinstance(payload,dict):raise ServiceError('重试请求格式无效。','invalid_json')
                request_id=payload.get('request_id') or self.headers.get('X-Request-Id','')
                return self._json(self.server.service.retry(parts[1],request_id),202)
            if len(parts)==3 and parts[0]=='jobs' and parts[2]=='review':
                try:payload=json.loads(self._body(65536))
                except (ValueError,UnicodeError,RecursionError):raise ServiceError('核对请求格式无效。','review_invalid') from None
                if not isinstance(payload,dict):raise ServiceError('核对请求格式无效。','review_invalid')
                return self._json(self.server.service.save_review(parts[1],payload))
            raise ServiceError('路由不存在。','not_found',404)
        except ServiceError as error:
            self.close_connection=True;self._error(error)
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:
            self.close_connection=True;self._error(ServiceError('服务暂时无法接收请求。','internal_error',500))
        finally:
            if acquired:self.server.upload_slots.release()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--host',choices=['127.0.0.1'],default='127.0.0.1')
    parser.add_argument('--port',type=int,default=3199)
    parser.add_argument('--allowed-origin',action='append',required=True)
    parser.add_argument('--public-base',default='api/')
    parser.add_argument('--adapter-module',default='drawing_split_service.model_adapter')
    args=parser.parse_args()
    if platform.system()!='Linux':raise RuntimeError('Run this service only on ZeroClave.')
    for origin in args.allowed_origin:
        url=urlsplit(origin)
        if url.scheme not in {'http','https'} or not url.netloc or url.path or url.query or url.fragment or url.username:raise ValueError('Invalid origin')
    base=args.public_base
    if not base or base.startswith('/') or '..' in base or ':' in base or '\\' in base:raise ValueError('Use a relative public API base')
    adapter=importlib.import_module(args.adapter_module).create_adapter()
    service=SplitService(args.data_dir,adapter,public_base=base)
    server=SplitHTTPServer((args.host,args.port),service,args.allowed_origin)
    try:server.serve_forever(poll_interval=.5)
    finally:server.server_close();service.close()


if __name__=='__main__':main()
