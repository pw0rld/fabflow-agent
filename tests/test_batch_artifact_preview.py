"""Preview saved CSV/JSON files without changing or downloading them."""
import copy
import csv
import html
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_artifact_preview import PREVIEW_FILES, render_artifact_preview
from batch_quote_costing import render_artifact
from batch_quote_service import ARTIFACTS, BatchQuoteService
from test_batch_quote_presentation import example
from test_batch_quote_service import fixture, STEP
from test_batch_quote_costing import settings


class Tags(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.root, self.rows, self.downloads, self.tabs, self.sections = {}, [], [], [], []
        self.script_count = self.images = 0
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'html': self.root = values
        if tag == 'tr' and 'data-preview-row' in values: self.rows.append(values)
        if tag == 'a' and 'data-preview-download' in values: self.downloads.append(values)
        if tag == 'a' and 'data-preview-tab' in values: self.tabs.append(values)
        if tag == 'details' and values.get('class') == 'preview-json-section': self.sections.append(values)
        self.script_count += tag == 'script'
        self.images += tag == 'img'


class BatchArtifactPreviewTests(unittest.TestCase):
    def test_weight_columns_show_two_decimals_and_preserve_original_precision(self):
        content = ('单件预估净重_kg,订单预估净重_kg,每套用量\r\n'
                   '3.43094455,34.3094455,1\r\n'
                   '0.00235343,0.8001662,2\r\n'
                   '0,2.675,3\r\n').encode()
        for kind in ('summary', 'source'):
            page = render_artifact_preview(content, kind, example()).decode()
            self.assertIn('title="原始值：3.43094455 kg">3.43</span>', page)
            self.assertIn('title="原始值：34.3094455 kg">34.31</span>', page)
            self.assertIn('title="原始值：0.00235343 kg">&lt;0.01</span>', page)
            self.assertIn('title="原始值：0.8001662 kg">0.80</span>', page)
            self.assertIn('title="原始值：0 kg">0.00</span>', page)
            self.assertIn('title="原始值：2.675 kg">2.68</span>', page)
            self.assertIn('<dd><span title="原始值：3.43094455 kg">3.43</span></dd>', page)
            self.assertIn('<dd><span title="原始值：34.3094455 kg">34.31</span></dd>', page)
        self.assertEqual(content.decode().splitlines()[1], '3.43094455,34.3094455,1')

    def test_numeric_business_fields_use_two_decimals_without_changing_identifiers(self):
        content = ('物料编号,物料名称,每套用量,订单总件数,密度_kg每m3,原生单件体积_mm3,'
                   '材料未税采用价_CNY每kg,材料采用税率假设_percent,来源行数\r\n'
                   '001,01,2,20,7850,437062.123456,2.814159,13,4\r\n').encode()
        page = render_artifact_preview(content, 'source', example()).decode()
        for original, displayed in [('2', '2'), ('20', '20'), ('7850', '7,850.00'),
                                    ('437062.123456', '437,062.12'), ('2.814159', '2.81'),
                                    ('13', '13.00'), ('4', '4')]:
            self.assertIn('title="原始值：' + original + '">' + displayed + '</span>', page)
        self.assertIn('<td>001</td>', page)
        self.assertIn('<td>01</td>', page)
        self.assertNotIn('title="原始值：001"', page)
        self.assertEqual(content.decode().splitlines()[1], '001,01,2,20,7850,437062.123456,2.814159,13,4')

    def test_csv_previews_keep_every_original_row_and_field_with_explicit_download(self):
        result = example()
        for preview, (kind, filename) in PREVIEW_FILES.items():
            if kind == 'record': continue
            with self.subTest(preview=preview):
                cells = list(csv.reader(io.StringIO(render_artifact(result, filename)[0].decode('utf-8-sig'))))
                page, mime = render_artifact(result, preview)
                text, tags = page.decode(), Tags(page.decode())
                self.assertEqual(mime, 'text/html; charset=utf-8')
                self.assertEqual(tags.root['data-preview-kind'], kind)
                self.assertEqual([int(row['data-preview-row']) for row in tags.rows], list(range(len(cells) - 1)))
                self.assertEqual(len(tags.downloads), 1)
                self.assertEqual(tags.downloads[0]['href'], filename)
                self.assertEqual(tags.downloads[0]['download'], filename)
                self.assertEqual(tags.script_count, 0)
                self.assertEqual(tags.images, 0)
                self.assertEqual({tab['data-preview-tab'] for tab in tags.tabs}, {'summary', 'source', 'cost', 'record', 'print'})
                self.assertIn('class="preview-row-details"', text)
                for header in cells[0]: self.assertIn(html.escape(header, quote=True), text)
                for row in cells[1:]:
                    for value in row:
                        if value: self.assertIn(html.escape(value, quote=True), text)

    def test_summary_and_original_views_keep_their_distinct_counts(self):
        result = example()
        self.assertEqual(len(Tags(render_artifact(result, 'preview-summary.html')[0].decode()).rows), 3)
        for filename in ('preview-source.html', 'preview-cost.html'):
            self.assertEqual(len(Tags(render_artifact(result, filename)[0].decode()).rows), 6)

    def test_record_preview_keeps_every_top_level_key_and_original_value(self):
        result = example()
        value = json.loads(render_artifact(result, 'quote.json')[0])
        text = render_artifact(result, 'preview-record.html')[0].decode()
        tags = Tags(text)
        self.assertEqual([row['data-record-key'] for row in tags.sections], list(value))
        self.assertEqual(tags.root['data-preview-kind'], 'record')
        self.assertEqual(tags.downloads[0]['href'], 'quote.json')
        self.assertEqual(tags.downloads[0]['download'], 'quote.json')
        self.assertEqual(tags.script_count, 0)
        for item in value.values():
            self.assertIn(html.escape(json.dumps(item, ensure_ascii=False, indent=2, allow_nan=False), quote=True), text)

    def test_preview_never_rewrites_source_exports_or_print_layout(self):
        result = example()
        saved = copy.deepcopy(result)
        filenames = ('quote.json', 'bom-summary.csv', 'bom.csv', 'cost.csv', 'quote-print.html')
        originals = {name: render_artifact(result, name)[0] for name in filenames}
        for filename in PREVIEW_FILES: self.assertTrue(render_artifact(result, filename)[0])
        self.assertEqual(result, saved)
        self.assertEqual({name: render_artifact(result, name)[0] for name in filenames}, originals)

    def test_csv_and_json_markup_is_inert_and_empty_is_not_zero(self):
        result = example()
        result['name'] = '<script>unsafe</script>'
        content = '物料名称,空值,额外字段\r\n"<img src=x onerror=unsafe>",,"javascript:unsafe"\r\n'.encode()
        page = render_artifact_preview(content, 'source', result).decode()
        self.assertEqual(Tags(page).script_count, 0)
        self.assertEqual(Tags(page).images, 0)
        self.assertIn('&lt;img src=x onerror=unsafe&gt;', page)
        self.assertIn('原始值为空', page)
        self.assertIn('&lt;script&gt;unsafe&lt;/script&gt;', page)
        record = render_artifact_preview(json.dumps({'<script>key</script>': '</pre><img src=x>'}).encode(), 'record', result).decode()
        self.assertEqual(Tags(record).images, 0)
        self.assertIn('&lt;/pre&gt;&lt;img src=x&gt;', record)

    def test_invalid_input_is_not_silently_truncated_or_used_as_a_link(self):
        for content in (b'', b'a,b\r\n1,2,3\r\n'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                render_artifact_preview(content, 'source', example())
        with self.assertRaises(ValueError): render_artifact_preview(b'a\r\n', 'other', example())
        with self.assertRaises(ValueError): render_artifact_preview(b'a\r\n', 'source', example(), back_href='https://example.invalid/')

    def test_back_link_follows_current_quote_context(self):
        result = example()
        self.assertIn('href="quote.html"', render_artifact(result, 'preview-summary.html')[0].decode())
        static = render_artifact(result, 'preview-summary.html', part_previews=[{
            'geometry_url': '/ortho2cad-test/customer-cases/allowed/parts/L0001.json'}])[0].decode()
        self.assertIn('href="quote-visual.html"', static)

    def test_public_preview_links_do_not_persist_in_the_saved_state(self):
        result = example()
        result['run_id'] = 'RUN-' + 'c' * 32
        state = {'id': 'BATCH-' + 'a' * 32, 'result': result}
        saved = copy.deepcopy(state)
        public = BatchQuoteService._public(state)
        prefix = '/workbench/api/quote-batches/' + state['id'] + '/runs/' + result['run_id'] + '/'
        for key, name in [('summary_bom_preview_url', 'preview-summary.html'), ('bom_preview_url', 'preview-source.html'),
                          ('cost_preview_url', 'preview-cost.html'), ('record_preview_url', 'preview-record.html')]:
            self.assertEqual(public['result']['links'][key], prefix + name)
        self.assertEqual(state, saved)
        self.assertNotIn('links', state['result'])
        self.assertNotIn('links', BatchQuoteService._public({'id': '../outside', 'result': result})['result'])

    def test_saved_run_preview_never_resolves_native_geometry(self):
        with tempfile.TemporaryDirectory() as folder:
            calls = []
            def processor(row, content):
                calls.append(row['id'])
                return fixture(row, content)
            service = BatchQuoteService(Path(folder), None, None, processor=processor)
            try:
                state = service.create('part.stp', STEP, 'preview-upload')
                service.start(state['id'], {'expected_revision': state['revision'], 'request_id': 'preview-start',
                                            'settings': settings(order_sets=10)})
                service.process_pending_once()
                ready = service.get(state['id'])
                run = ready['result']['run_id']
                before, saved = list(calls), service.artifact(state['id'], run, 'quote.json')[0]
                def no_geometry(*args, **kwargs): raise AssertionError('No geometry in a CSV/JSON preview')
                service.parts_manifest = no_geometry
                for filename in PREVIEW_FILES:
                    self.assertIn(filename, ARTIFACTS)
                    content, mime, download = service.artifact(state['id'], run, filename)
                    self.assertEqual(mime, 'text/html; charset=utf-8')
                    self.assertTrue(content.startswith(b'<!doctype html>'))
                    self.assertTrue(download.endswith(filename))
                self.assertEqual(service.artifact(state['id'], run, 'quote.json')[0], saved)
                self.assertEqual(calls, before)
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
