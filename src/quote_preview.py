#!/usr/bin/env python3
"""Read-only quotation links and previews behind the existing Nginx login."""
from __future__ import annotations

import argparse
import hashlib
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import parse_qs, urlsplit

from markdown_it import MarkdownIt


PREFIX = "/quote-preview/"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
FILES = {"quote.md": "报价单", "cost_breakdown.md": "成本明细", "record.md": "当前询价记录"}
MAX_BYTES = 2 * 1024 * 1024
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config/quote-preview.json"
MARKDOWN = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table").disable(
    ["link", "image", "autolink"])
CSS = """
body{margin:0;background:#f3f5f8;color:#17212f;font:16px/1.7 system-ui,sans-serif}
main{max-width:1060px;margin:28px auto;padding:30px;background:white;border-radius:12px}
nav{display:flex;flex-wrap:wrap;gap:12px 22px;border-bottom:1px solid #dde3eb;padding-bottom:16px}
a{color:#1854b3;text-decoration:underline;text-underline-offset:3px}h1{font-size:25px}h2{font-size:21px}
.status{color:#5a6574;font-size:14px}.history{padding:12px;background:#fff4d6;border-left:4px solid #d69713}
article{overflow-wrap:anywhere;overflow-x:auto}table{border-collapse:collapse;width:100%;margin:18px 0}
th,td{border:1px solid #dce2ea;padding:9px 12px;text-align:left}th{background:#f2f5f9}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f9;padding:14px}code{font-family:monospace}
@media(max-width:700px){main{margin:0;padding:18px;border-radius:0}body{font-size:14px}th,td{padding:6px}}
@media print{body{background:white}main{margin:0;max-width:none;padding:0}nav{display:none}table{font-size:12px}}
"""


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid quotation identifier")
    return value


class PreviewStore:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("quotation task directory required")

    def _read(self, *parts):
        """Walk directory descriptors: never follow a file or directory symlink."""
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = following
            file_descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(file_descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise ValueError("unsupported quotation document")
                content = stream.read(MAX_BYTES + 1)
                if len(content) > MAX_BYTES:
                    raise ValueError("quotation document too large")
                return content
        finally:
            os.close(directory)

    def _info(self, task_id, quote_id):
        identifier(task_id)
        identifier(quote_id)
        base = (task_id, "quotes", quote_id)
        result = json.loads(self._read(*base, "quote.json"))
        if result.get("task_id") != task_id or result.get("quote_id") != quote_id:
            raise ValueError("quotation identity mismatch")
        for filename, key in [("record_snapshot.json", "source_record_sha256"),
                              ("pricing_policy.json", "pricing_policy_sha256")]:
            if hashlib.sha256(self._read(*base, filename)).hexdigest() != result.get(key):
                raise ValueError("quotation source snapshot changed")
        try:
            current = hashlib.sha256(self._read(task_id, "record.json")).hexdigest()
        except FileNotFoundError:
            current = None
        return {"quote_id": quote_id, "task_id": task_id,
                "status": str(result.get("status", "报价草稿，待审核")),
                "source_current": current == result["source_record_sha256"]}

    def document(self, task_id, quote_id, filename):
        if filename not in FILES:
            raise FileNotFoundError("quotation document not found")
        info = self._info(task_id, quote_id)
        parts = (task_id, filename) if filename == "record.md" else (task_id, "quotes", quote_id, filename)
        return self._read(*parts), info

    def links(self, task_id, quote_id, public_base_url):
        origin = urlsplit(public_base_url)
        if (origin.scheme != "https" or not origin.netloc or origin.username or origin.password
                or origin.path not in {"", "/"} or origin.query or origin.fragment):
            raise ValueError("public quotation origin must be an HTTPS origin")
        info = self._info(task_id, quote_id)
        base = public_base_url.rstrip("/") + PREFIX + task_id + "/" + quote_id + "/"
        result = dict(info)
        for filename, key in [("quote.md", "quote_url"), ("cost_breakdown.md", "cost_url"),
                              ("record.md", "record_url")]:
            try:
                self.document(task_id, quote_id, filename)
            except FileNotFoundError:
                if filename == "record.md":
                    continue
                raise
            result[key] = base + filename
        return result


def preview_html(content, info, filename):
    base = PREFIX + info["task_id"] + "/" + info["quote_id"] + "/"
    navigation = "".join('<a href="' + base + name + '">' + label + '</a>' for name, label in FILES.items())
    navigation += '<a href="' + base + filename + '?download=1">下载原文件</a>'
    history = "" if info["source_current"] else (
        '<p class="history">历史报价：当前询价资料已变化。本报价保留生成时的资料版本和金额；'
        '“当前询价记录”展示最新记录。</p>')
    body = MARKDOWN.render(content.decode("utf-8"))
    title = escape(FILES[filename] + " · " + info["quote_id"])
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + title + '</title><style>' + CSS + '</style></head><body><main><nav>'
            + navigation + '</nav><p class="status">' + escape(info["quote_id"]) + ' · '
            + escape(info["status"]) + '</p>' + history + '<article>' + body
            + '</article></main></body></html>').encode("utf-8")


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = "QuotationPreview"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def _send(self, status, content, content_type="text/plain; charset=utf-8", filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; sandbox allow-downloads")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def _get(self):
        try:
            url = urlsplit(self.path)
            if url.path == "/healthz" and not url.query:
                self._send(200, b"ok\n")
                return
            # Percent-encoded separators, dot segments and arbitrary paths are never accepted.
            parts = url.path.split("/")
            if len(parts) != 5 or parts[:2] != ["", "quote-preview"]:
                raise FileNotFoundError()
            _, _, task_id, quote_id, filename = parts
            identifier(task_id)
            identifier(quote_id)
            query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
            if query and query != {"download": ["1"]}:
                raise ValueError("invalid preview query")
            content, info = self.server.store.document(task_id, quote_id, filename)
            if query:
                self._send(200, content, "text/markdown; charset=utf-8", task_id + "-" + quote_id + "-" + filename)
            else:
                self._send(200, preview_html(content, info, filename), "text/html; charset=utf-8")
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            self._send(404, "文件不存在或暂不可预览。".encode("utf-8"))

    do_GET = _get
    do_HEAD = _get

    def do_POST(self):
        self._send(405, b"Read-only quotation preview.\n")


def make_server(store, host="127.0.0.1", port=3091):
    if host != "127.0.0.1":
        raise ValueError("quotation preview must bind to loopback")
    server = ThreadingHTTPServer((host, port), PreviewHandler)
    server.store = store
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    links = subparsers.add_parser("links")
    links.add_argument("--task-id", required=True)
    links.add_argument("--quote-id", required=True)
    links.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    serve = subparsers.add_parser("serve")
    serve.add_argument("--port", type=int, default=3091)
    args = parser.parse_args(argv)
    try:
        store = PreviewStore(args.root)
        if args.operation == "links":
            config = json.loads(args.config.read_text(encoding="utf-8"))
            print(json.dumps(store.links(args.task_id, args.quote_id, config["public_base_url"]),
                             ensure_ascii=False, indent=2))
        else:
            with make_server(store, port=args.port) as server:
                server.serve_forever()
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
