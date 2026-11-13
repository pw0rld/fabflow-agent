"""Offline preview HTTP tests using synthetic files and a loopback-only server.

No model, browser, remote server, or real quotation is used. The calculator is
never invoked: all hashes and document bytes belong to temporary test fixtures.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from quote_preview import PreviewHandler, PreviewStore, make_server


def sha(content):
    return hashlib.sha256(content).hexdigest()


class HTMLTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


class QuotePreviewTests(unittest.TestCase):
    TASK = 'TASK-001'
    QUOTE = 'Q-001'
    PUBLIC_BASE = 'https://quotes.example.test'

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='quote-preview-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'tasks'
        self.task = self.root / self.TASK
        self.folder = self.task / 'quotes' / self.QUOTE
        self.folder.mkdir(parents=True)
        self.record_bytes = self.json_bytes({
            'task_id': self.TASK, 'data_revision': 3, 'simulated': True,
            'purpose': 'synthetic preview unit test, not a production record',
        })
        self.policy_bytes = self.json_bytes({
            'pricing_method': 'cost_plus', 'rate': '0.20', 'simulated': True,
        })
        (self.task / 'record.json').write_bytes(self.record_bytes)
        (self.task / 'record.md').write_text(
            '# 询价记录\n\n参数修订：3\n\n数量：10 件。\n', encoding='utf-8')
        (self.folder / 'record_snapshot.json').write_bytes(self.record_bytes)
        (self.folder / 'pricing_policy.json').write_bytes(self.policy_bytes)
        (self.folder / 'quote.json').write_bytes(self.json_bytes({
            'task_id': self.TASK, 'quote_id': self.QUOTE,
            'source_revision': 3,
            'source_record_sha256': sha(self.record_bytes),
            'pricing_policy_sha256': sha(self.policy_bytes),
            'status': '模拟报价草稿，待审核', 'simulated': True,
            'quantity': 10,
            'amounts': {'unit_price': '81.60', 'total_price': '816.00'},
        }))
        (self.folder / 'quote.md').write_text(
            '# 模拟报价单（草稿）\n\n**报价总额：816.00 元。**\n\n未税、不含运费。\n',
            encoding='utf-8')
        (self.folder / 'cost_breakdown.md').write_text(
            '# 内部成本核算\n\n成本合计：680.00 元。\n\n成本加成率：20%。\n',
            encoding='utf-8')
        self.store = PreviewStore(self.root)
        self.server = make_server(self.store, '127.0.0.1', 0)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.prefix = f'/quote-preview/{self.TASK}/{self.QUOTE}/'

    @staticmethod
    def json_bytes(value):
        return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def get(self, target):
        connection = http.client.HTTPConnection(
            '127.0.0.1', self.server.server_address[1], timeout=5)
        try:
            # http.client preserves traversal attempts instead of normalizing them away.
            connection.request('GET', target)
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    def links(self):
        return self.store.links(self.TASK, self.QUOTE, self.PUBLIC_BASE)

    def fingerprint(self):
        return {str(p.relative_to(self.task)): sha(p.read_bytes())
                for p in self.task.rglob('*') if p.is_file()}

    def assert_denied(self, target, secret=None):
        status, _, body = self.get(target)
        self.assertIn(status, (400, 403, 404), body[:250])
        if secret is not None:
            self.assertNotIn(secret, body)

    def test_links_target_the_two_quote_documents_and_current_record(self):
        links = self.links()
        self.assertTrue(links['source_current'])
        self.assertEqual(links['quote_id'], self.QUOTE)
        for key, filename in (('quote_url', 'quote.md'),
                              ('cost_url', 'cost_breakdown.md'),
                              ('record_url', 'record.md')):
            with self.subTest(link=key):
                self.assertEqual(links[key], self.PUBLIC_BASE + self.prefix + filename)
        self.assertIsInstance(self.server.RequestHandlerClass, type)
        self.assertTrue(issubclass(self.server.RequestHandlerClass, PreviewHandler))

    def test_http_previews_render_markdown_and_identify_current_record_navigation(self):
        for filename, text in (('quote.md', '816.00'),
                               ('cost_breakdown.md', '680.00'),
                               ('record.md', '数量')):
            with self.subTest(file=filename):
                status, headers, body = self.get(self.prefix + filename)
                self.assertEqual(status, 200)
                self.assertTrue(headers.get('content-type', '').startswith('text/html'))
                rendered = body.decode('utf-8')
                self.assertIn(text, rendered)
                self.assertIn('当前询价记录', rendered)
                parser = HTMLTags()
                parser.feed(rendered)
                self.assertTrue(any(tag == 'h1' for tag, _ in parser.tags))

    def test_downloads_return_original_bytes_as_attachments(self):
        for filename, path in (('quote.md', self.folder / 'quote.md'),
                               ('cost_breakdown.md', self.folder / 'cost_breakdown.md'),
                               ('record.md', self.task / 'record.md')):
            with self.subTest(file=filename):
                status, headers, body = self.get(self.prefix + filename + '?download=1')
                self.assertEqual(status, 200)
                self.assertEqual(body, path.read_bytes())
                disposition = headers.get('content-disposition', '')
                self.assertIn('attachment', disposition.lower())
                self.assertIn(filename, disposition)

    def test_only_allowlisted_files_and_safe_path_segments_are_served(self):
        secret = b'OUTSIDE-PREVIEW-ROOT-DO-NOT-SERVE'
        (self.base / 'outside.txt').write_bytes(secret)
        targets = [
            self.prefix + 'quote.json', self.prefix + 'pricing_policy.json',
            self.prefix + 'record_snapshot.json',
            '/quote-preview/../Q-001/quote.md',
            '/quote-preview/%2e%2e/Q-001/quote.md',
            '/quote-preview/TASK-001/%2e%2e/quote.md',
            self.prefix + '../../outside.txt',
            self.prefix + '%2e%2e%2foutside.txt',
            self.prefix + '%252e%252e%252foutside.txt',
            '/quote-preview/TASK-001/Q-001%2f..%2f../outside.txt',
            self.prefix + '%2fetc%2fpasswd',
            self.prefix + 'quote.md/extra',
        ]
        for target in targets:
            with self.subTest(target=target):
                self.assert_denied(target, secret)

    def test_links_reject_traversal_task_and_quote_ids(self):
        for task_id, quote_id in (('../outside', self.QUOTE),
                                  (self.TASK, '../outside'),
                                  ('/etc', self.QUOTE),
                                  (self.TASK, 'Q/child')):
            with self.subTest(task=task_id, quote=quote_id):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.store.links(task_id, quote_id, self.PUBLIC_BASE)

    def test_document_symlinks_are_rejected_for_preview_download_and_links(self):
        outside = self.base / 'outside.md'
        secret = b'EXTERNAL-SYMLINK-CONTENT'
        outside.write_bytes(secret)
        target = self.folder / 'quote.md'
        target.unlink()
        target.symlink_to(outside)
        self.assert_denied(self.prefix + 'quote.md', secret)
        self.assert_denied(self.prefix + 'quote.md?download=1', secret)
        with self.assertRaises((ValueError, OSError)):
            self.links()

    def test_symlinked_quote_directory_cannot_escape_root(self):
        external = self.base / 'outside-quote'
        self.folder.rename(external)
        self.folder.symlink_to(external, target_is_directory=True)
        self.assert_denied(self.prefix + 'quote.md', b'816.00')
        with self.assertRaises((ValueError, OSError)):
            self.links()

    def test_raw_html_and_embedded_markdown_links_and_images_are_inert(self):
        text = '''# 模拟报价单

<script>alert("unsafe")</script>
<img src="https://evil.invalid/pixel" onerror="alert(1)">
<svg onload="alert(2)"></svg>
[外部链接](https://evil.invalid/click)
![跟踪图片](https://evil.invalid/track.png)
[执行链接](javascript:alert(3))
'''
        (self.folder / 'quote.md').write_text(text, encoding='utf-8')
        status, headers, body = self.get(self.prefix + 'quote.md')
        self.assertEqual(status, 200)
        parser = HTMLTags()
        parser.feed(body.decode('utf-8'))
        self.assertFalse({'script', 'img', 'svg', 'iframe', 'object', 'embed'} &
                         {tag for tag, _ in parser.tags})
        for tag, attrs in parser.tags:
            self.assertFalse(any(name.lower().startswith('on') for name in attrs))
            if tag == 'a':
                href = attrs.get('href', '')
                self.assertNotIn('evil.invalid', href)
                self.assertFalse(href.lower().startswith(('javascript:', 'data:', 'file:')))
        csp = headers.get('content-security-policy', '')
        directives = {parts[0].lower(): parts[1:]
                      for item in csp.split(';') if (parts := item.strip().split())}
        self.assertEqual(directives.get('script-src', directives.get('default-src')), ["'none'"])
        self.assertIn('nosniff', headers.get('x-content-type-options', '').lower())

    def test_missing_required_documents_do_not_produce_broken_links(self):
        for filename in ('quote.md', 'cost_breakdown.md'):
            with self.subTest(file=filename):
                path = self.folder / filename
                original = path.read_bytes()
                path.unlink()
                try:
                    self.assert_denied(self.prefix + filename)
                    self.assert_denied(self.prefix + filename + '?download=1')
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        self.links()
                finally:
                    path.write_bytes(original)

    def test_preview_links_and_downloads_never_rewrite_record_or_quote_files(self):
        before = self.fingerprint()
        self.links()
        for filename in ('quote.md', 'cost_breakdown.md', 'record.md'):
            self.assertEqual(self.get(self.prefix + filename)[0], 200)
            self.assertEqual(self.get(self.prefix + filename + '?download=1')[0], 200)
        self.links()
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual((self.task / 'record.json').read_bytes(), self.record_bytes)

    def test_historical_quote_is_marked_without_rewriting_original_documents(self):
        current = json.loads(self.record_bytes)
        current['data_revision'] = 4
        (self.task / 'record.json').write_bytes(self.json_bytes(current))
        (self.task / 'record.md').write_text('# 当前询价记录\n\n修订：4\n', encoding='utf-8')
        before = self.fingerprint()
        self.assertFalse(self.links()['source_current'])
        for filename in ('quote.md', 'cost_breakdown.md'):
            status, _, body = self.get(self.prefix + filename)
            self.assertEqual(status, 200)
            self.assertIn('历史', body.decode('utf-8'))
        self.assertIn(b'816.00', self.get(self.prefix + 'quote.md')[2])
        self.assertIn('修订：4', self.get(self.prefix + 'record.md')[2].decode('utf-8'))
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual((self.folder / 'record_snapshot.json').read_bytes(), self.record_bytes)

    def test_tampered_source_snapshots_are_not_presented_as_valid_quotes(self):
        for filename in ('record_snapshot.json', 'pricing_policy.json'):
            with self.subTest(file=filename):
                path = self.folder / filename
                original = path.read_bytes()
                path.write_bytes(original + b'\n')
                try:
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        self.links()
                    self.assert_denied(self.prefix + 'quote.md')
                finally:
                    path.write_bytes(original)


if __name__ == '__main__':
    unittest.main()
