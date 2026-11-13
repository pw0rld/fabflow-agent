"""Derived BOM/print outputs preserve the saved quote and its source records."""
import copy
import csv
from decimal import Decimal
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_costing import aggregate_display_groups, calculate_batch, render_artifact, _kg
from batch_quote_service import ARTIFACTS, BatchQuoteService
from test_batch_quote_costing import document, settings, solid
from test_batch_quote_service import fixture, STEP


def example():
    documents = [document([solid(i, 'fastener', name='CLS-M6-2', volume='299.79960807')
                           for i in range(1, count + 1)], file_id=f'F{index:04d}', name=f'assembly-{index}.stp')
                 for index, count in enumerate((18, 8, 4, 4), 1)]
    documents.append(document([solid(1, name='Plate', grade='SS304')], file_id='F0005'))
    documents.append(document([solid(1, name='Open part', valid=False)], file_id='F0006'))
    return calculate_batch(documents, settings(order_sets=10))


def rows(content):
    return list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))


class PrintTags(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.groups, self.scripts, self.images = [], 0, []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'tr' and 'data-print-group' in values:
            self.groups.append(values)
        if tag == 'script':
            self.scripts += 1
        if tag == 'img':
            self.images.append(values)


class BatchQuotePresentationTests(unittest.TestCase):
    def test_explicit_engineering_view_marks_unpriced_requirements_without_repricing(self):
        result = example()
        saved = copy.deepcopy(result)
        original = {name: render_artifact(result, name)[0] for name in ('quote.html', 'quote-print.html', 'quote.json', 'bom.csv', 'cost.csv')}
        fields = {'material_grade': '316不锈钢', 'surface_treatment': '热浸锌 <script>'}
        recognition = {'ai': {'lines': {'L0001': {'values': fields,
            'field_states': {key: 'candidate' for key in fields},
            'evidence': {key: [{'reference': 'F-TEST', 'page': 1, 'quote': value, 'region': '合成说明'}] for key, value in fields.items()}}}}}
        for name in ('quote.html', 'quote-print.html'):
            page = render_artifact(result, name, engineering_recognition=recognition)[0].decode()
            self.assertIn('data-engineering-pricing-notice', page)
            self.assertIn('工程要求待核价', page)
            self.assertIn('316不锈钢', page)
            self.assertIn('&lt;script&gt;', page)
            self.assertNotIn('<script>', page)
        self.assertEqual(result, saved)
        self.assertEqual({name: render_artifact(result, name)[0] for name in original}, original)

    def test_reference_price_display_is_readable_without_rounding_saved_data(self):
        result = example()
        raw = '2.8141592920353982300884955752212389'
        result['material_price_sources'] = [{'normalized_net_rate': raw}]
        saved = copy.deepcopy(result)
        for name in ('quote.html', 'quote-print.html'):
            page = render_artifact(result, name)[0].decode()
            self.assertIn('title="' + raw + '">约 2.81', page)
        self.assertEqual(result, saved)

    def test_grouped_bom_has_one_row_for_fasteners_and_retains_source_ids(self):
        result = example()
        content, mime = render_artifact(result, 'bom-summary.csv')
        data = rows(content)
        self.assertEqual(mime, 'text/csv; charset=utf-8')
        self.assertTrue(content.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(len(data), 3)
        fastener = next(row for row in data if row['物料名称'] == 'CLS-M6-2')
        self.assertEqual(fastener['每套用量'], '34')
        self.assertEqual(fastener['订单总件数'], '340')
        self.assertEqual(fastener['报价单价_CNY'], '0.96')
        self.assertEqual(fastener['报价金额_CNY'], '326.40')
        self.assertEqual(json.loads(fastener['来源物料编号']), ['L0001', 'L0002', 'L0003', 'L0004'])
        self.assertEqual(len(json.loads(fastener['来源文件'])), 4)
        self.assertEqual(fastener['材料采用口径'], '估重采用')

    def test_summary_csv_conserves_known_totals_and_leaves_unknown_price_empty(self):
        result = example()
        data = rows(render_artifact(result, 'bom-summary.csv')[0])
        for original_key, column in [('quantity_total', '订单总件数'),
                                     ('estimated_total_net_weight_kg', '订单预估净重_kg'),
                                     ('total_price', '报价金额_CNY')]:
            original_sum = sum(Decimal(str(line[original_key])) for line in result['lines'] if line.get(original_key) is not None)
            exported_sum = sum(Decimal(row[column]) for row in data if row[column])
            self.assertEqual(exported_sum, original_sum)
        unknown = next(row for row in data if row['物料名称'] == 'Open part')
        self.assertEqual(unknown['报价金额_CNY'], '')
        self.assertEqual(unknown['报价单价_CNY'], '')

    def test_display_outputs_never_mutate_original_result_or_raw_exports(self):
        result = example()
        saved = copy.deepcopy(result)
        exports = {name: render_artifact(result, name)[0] for name in ('quote.json', 'bom.csv', 'cost.csv')}
        for name in ('bom-summary.csv', 'quote-print.html', 'quote.html'):
            self.assertTrue(render_artifact(result, name)[0])
        self.assertEqual(result, saved)
        for name, before in exports.items():
            self.assertEqual(render_artifact(result, name)[0], before)
        self.assertEqual(len(rows(exports['bom.csv'])), 6)
        self.assertNotIn('display_groups', json.loads(exports['quote.json']))

    def test_summary_csv_escapes_formula_like_names_and_preserves_quoted_sources(self):
        result = example()
        result['lines'][0]['part_name'] = '=HYPERLINK("unsafe")'
        result['lines'][0]['document'] = 'comma,quote"source.stp'
        data = rows(render_artifact(result, 'bom-summary.csv')[0])
        row = next(item for item in data if item['汇总编号'] == 'L0001')
        self.assertEqual(row['物料名称'], "'=HYPERLINK(\"unsafe\")")
        self.assertEqual(json.loads(row['来源文件']), ['comma,quote"source.stp'])

    def test_print_view_is_complete_grouped_internal_review_document(self):
        result = example()
        content, mime = render_artifact(result, 'quote-print.html')
        page = content.decode()
        tags = PrintTags(page)
        self.assertEqual(mime, 'text/html; charset=utf-8')
        self.assertEqual(len(tags.groups), 3)
        self.assertEqual(tags.groups[0]['data-member-count'], '4')
        self.assertEqual(tags.scripts, 0)
        for marker in ('data-print-layout="A4-landscape"', 'size:A4 landscape',
                       'page-break-inside:avoid', 'display:table-header-group', 'counter(page)',
                       '内部参考报价单', '待工程与商务审核', '未税', '不含运费',
                       '3 项物料 · 1 项未计价', '汇总物料的来源明细', '未计价项目', '本次采用条件'):
            self.assertIn(marker, page)
        self.assertIn('data-total-price="' + result['totals']['total_price'] + '"', page)
        for line in result['lines']:
            self.assertIn(line['id'], page)
        self.assertIn('SS304', page)

    def test_print_uses_only_valid_original_thumbnails_and_escapes_source_text(self):
        result = example()
        previews = []
        for line in result['lines']:
            url = '/ortho2cad-test/customer-cases/safe-case/parts/' + line['id'] + '.json'
            previews.append({'line_id': line['id'], 'status': 'ready', 'geometry_url': url,
                             'thumbnail_url': url[:-5] + '/thumbnail.png'})
        previews[0]['thumbnail_url'] = 'https://tracker.invalid/image.png'
        result['name'] = '<script>unsafe</script>'
        result['lines'][4]['part_name'] = '<img onerror=unsafe>'
        page = render_artifact(result, 'quote-print.html', part_previews=previews)[0].decode()
        self.assertEqual(PrintTags(page).scripts, 0)
        self.assertNotIn('https://tracker.invalid', page)
        self.assertNotIn('<img onerror=', page)
        self.assertIn('&lt;script&gt;unsafe&lt;/script&gt;', page)
        self.assertIn('href="quote-visual.html"', page)
        self.assertIn('/parts/L0005/thumbnail.png', page)
        self.assertIn('loading="eager"', page)
        self.assertNotIn('loading="lazy"', page)

    def test_screen_view_keeps_original_raw_evidence_collapsible_and_clear_counts(self):
        result = example()
        page = render_artifact(result, 'quote.html')[0].decode()
        self.assertIn('10 套 · 3 项物料 · 1 项未计价', page)
        self.assertIn('查看来源口径 · 6 条来源记录', page)
        self.assertIn('<span>CAD材料</span><strong>SS304</strong>', page)
        self.assertIn('class="material-summary"', page)
        self.assertIn('<details class="part-notes"><summary>查看依据</summary>', page)
        self.assertIn('href="preview-summary.html"', page)
        self.assertIn('href="preview-source.html">预览来源明细', page)
        self.assertIn('href="quote-print.html"', page)

    def test_adaptive_weight_display_does_not_round_small_positive_values_to_zero(self):
        for value, expected in [(None, '尚无法完整估算'), ('0', '约 0.00 kg'),
                                ('0.00235343', '约 2.35 g'), ('0.00000001', '小于 0.01 g'),
                                ('3.43094455', '约 3.43 kg'), ('2520.606204', '约 2,520.61 kg')]:
            with self.subTest(value=value):
                self.assertEqual(_kg(value), expected)

    def test_public_links_are_derived_for_existing_runs_without_writing_state(self):
        result = example()
        result['run_id'] = 'RUN-' + 'b' * 32
        state = {'id': 'BATCH-' + 'a' * 32, 'result': result}
        original = copy.deepcopy(state)
        public = BatchQuoteService._public(state)
        prefix = '/workbench/api/quote-batches/' + state['id'] + '/runs/' + result['run_id'] + '/'
        self.assertEqual(public['result']['links']['summary_bom_url'], prefix + 'bom-summary.csv')
        self.assertEqual(public['result']['links']['print_url'], prefix + 'customer-print.html')
        self.assertEqual(state, original)
        self.assertNotIn('links', state['result'])
        invalid = BatchQuoteService._public({'id': '../outside', 'result': result})
        self.assertNotIn('links', invalid['result'])

    def test_service_renders_new_artifacts_from_saved_run_without_reprocessing(self):
        with tempfile.TemporaryDirectory() as folder:
            calls = []
            def processor(row, content):
                calls.append(row['id'])
                return fixture(row, content)
            service = BatchQuoteService(Path(folder), None, None, processor=processor)
            try:
                state = service.create('part.stp', STEP, 'presentation-upload')
                service.start(state['id'], {'expected_revision': state['revision'], 'request_id': 'presentation-start', 'settings': settings(order_sets=10)})
                service.process_pending_once()
                ready = service.get(state['id'])
                run = ready['result']['run_id']
                saved = service.artifact(state['id'], run, 'quote.json')[0]
                before = list(calls)
                for name in ('bom-summary.csv', 'quote-print.html'):
                    self.assertIn(name, ARTIFACTS)
                    data, mime, download = service.artifact(state['id'], run, name)
                    self.assertTrue(data)
                    self.assertTrue(download.endswith(name))
                self.assertEqual(service.artifact(state['id'], run, 'quote.json')[0], saved)
                self.assertEqual(calls, before)
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
