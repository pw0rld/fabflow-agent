"""Saved customer net weights: coverage, precision, privacy and read-only use.

Run only on ZeroClave. Geometry/HTTP fixtures are synthetic and isolated; this
suite does not run a CAD engine, call a model, or write a production quotation.
"""
import copy
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from batch_customer_quote import render_customer_quote, customer_part_projection
from batch_quote_costing import calculate_batch, render_artifact, aggregate_display_groups
from customer_manufacturing_view import part_projection
from customer_weight_view import weight_summary, weight_projection, display_weight
from customer_upload_service import CustomerUploadService
from prep_record import digest
from test_batch_quote_costing import document, solid, settings
from test_batch_quote_presentation import example
import test_customer_process_http as http_fixture


def known_result():
    return calculate_batch([document([solid(1, 'plate/solid-1', name='Plate', volume=1000000),
                                      solid(2, 'screw/solid-1', name='M4x20', volume=1000)])], settings(order_sets=3))


def set_weight(line, unit, per_set, order):
    for key, value in zip(('estimated_net_weight_kg', 'estimated_set_net_weight_kg', 'estimated_total_net_weight_kg'),
                          (unit, per_set, order)):
        line[key] = value


class Page(HTMLParser):
    def __init__(self, content):
        super().__init__(); self.attributes = []; self.text = []; self.feed(content)
    def handle_starttag(self, tag, attrs): self.attributes.append((tag, dict(attrs)))
    def handle_data(self, data): self.text.append(data)


class CustomerWeightViewTests(unittest.TestCase):
    def test_complete_summary_adds_saved_weights_without_mutation_or_geometry_inference(self):
        value = known_result(); before = copy.deepcopy(value); summary = weight_summary(value)
        self.assertTrue(summary['complete']); self.assertEqual(summary['status'], 'complete')
        self.assertEqual(Decimal(summary['estimated_set_net_weight_kg']), Decimal('7.85785'))
        self.assertEqual(Decimal(summary['estimated_total_net_weight_kg']), Decimal('23.57355'))
        self.assertEqual(summary['missing_estimated_lines'], 0); self.assertEqual(summary['known_weight_lines'], 2)
        self.assertEqual(summary['unit'], 'kg'); self.assertEqual(summary['role'], 'cross_check')
        self.assertFalse(summary['includes_packaging']); self.assertEqual(value, before)

    def test_partial_coverage_withholds_full_total_even_when_old_summary_is_stale(self):
        value = known_result(); set_weight(value['lines'][1], None, None, None)
        before = copy.deepcopy(value); summary = weight_summary(value)
        self.assertFalse(summary['complete']); self.assertEqual(summary['status'], 'partial')
        self.assertEqual(summary['missing_estimated_lines'], 1)
        self.assertIsNone(summary['estimated_set_net_weight_kg']); self.assertIsNone(summary['estimated_total_net_weight_kg'])
        self.assertEqual(Decimal(summary['known_set_net_weight_kg']), Decimal('7.85'))
        self.assertEqual(Decimal(summary['known_total_net_weight_kg']), Decimal('23.55'))
        page = render_customer_quote(value).decode()
        self.assertIn('已知小计', page); self.assertIn('1 项重量缺失', page)
        self.assertEqual(value, before)

    def test_unknown_invalid_and_zero_weights_are_not_conflated(self):
        value = known_result()
        for invalid in (None, False, 'NaN', 'Infinity', '-1', '1e-1000000000'):
            for line in value['lines']: set_weight(line, invalid, invalid, invalid)
            summary = weight_summary(value)
            self.assertEqual(summary['status'], 'unavailable')
            self.assertIsNone(summary['known_total_net_weight_kg']); self.assertIsNone(summary['known_set_net_weight_kg'])
            self.assertEqual(display_weight(invalid), '尚未估算')
        for line in value['lines']: set_weight(line, '0', '0', '0')
        self.assertEqual(weight_summary(value)['status'], 'complete')
        self.assertEqual(display_weight('0'), '约 0.00 kg')

    def test_display_is_adaptive_two_decimals_and_tiny_parts_never_become_zero(self):
        self.assertEqual(display_weight('3.431234'), '约 3.43 kg')
        self.assertEqual(display_weight('0.002353'), '约 2.35 g')
        self.assertEqual(display_weight('0.000000001'), '小于 0.01 g')
        self.assertNotIn('0.00 kg', display_weight('0.002353'))
        self.assertEqual(display_weight(None), '尚未估算')

    def test_summary_uses_unrounded_rows_instead_of_adding_visible_values(self):
        value = known_result()
        for line in value['lines']: set_weight(line, '0.004004', '0.004004', '0.012012')
        summary = weight_summary(value)
        self.assertEqual(display_weight(value['lines'][0]['estimated_set_net_weight_kg']), '约 4.00 g')
        self.assertEqual(Decimal(summary['estimated_set_net_weight_kg']), Decimal('0.008008'))
        self.assertEqual(display_weight(summary['estimated_set_net_weight_kg']), '约 8.01 g')

    def test_material_and_density_identity_are_saved_candidates_or_assumptions(self):
        line = known_result()['lines'][0]
        assumed = weight_projection(line)['weight_assumptions']
        self.assertEqual(assumed['source'], 'order_assumption')
        self.assertEqual(assumed['density_kg_m3'], '7850'); self.assertIsNone(assumed['cad_material_text'])
        self.assertIn('Q235', assumed['adopted_material_label'])
        line.update(confidence='engineering_material_candidate', material_raw='旧CAD材料Q235', material_profile='ss316', density_kg_m3='7980')
        candidate = weight_projection(line)['weight_assumptions']
        self.assertEqual(candidate['source'], 'engineering_candidate')
        self.assertEqual(candidate['cad_material_text'], '旧CAD材料Q235')
        self.assertIn('316', candidate['adopted_material_label']); self.assertEqual(candidate['density_kg_m3'], '7980')
        line.update(confidence='proxy_material_estimate', material_profile='stainless_mesh', material_raw=None)
        proxy = weight_projection(line)['weight_assumptions']
        self.assertEqual(proxy['source'], 'proxy_estimate'); self.assertIn('牌号未定', proxy['adopted_material_label'])
        self.assertNotIn('304', proxy['adopted_material_label']); self.assertNotIn('316', proxy['adopted_material_label'])

    def test_rough_geometry_is_marked_without_hiding_saved_weight_or_pricing_it(self):
        value = known_result(); line = value['lines'][0]
        line['confidence'] = 'low'; before = copy.deepcopy(value)
        summary = weight_summary(value)
        self.assertTrue(summary['complete']); self.assertEqual(summary['low_confidence_lines'], 1)
        self.assertEqual(Decimal(summary['low_confidence_total_net_weight_kg']), Decimal(line['estimated_total_net_weight_kg']))
        page = render_customer_quote(value).decode()
        self.assertIn('1 项粗估', page); self.assertIn('异常几何粗估', page)
        self.assertEqual(value, before)


class CustomerQuoteWeightRenderTests(unittest.TestCase):
    def test_customer_table_and_print_have_explicit_unit_order_and_summary_weights(self):
        value = known_result(); before = copy.deepcopy(value)
        for printable in (False, True):
            html = render_customer_quote(value, printable=printable).decode(); page = Page(html)
            self.assertIn('单件预估净重', html); self.assertIn('订单预估净重', html)
            self.assertIn('每套预估净重', html); self.assertIn('整单预估净重', html)
            self.assertIn('约 7.85 g', html); self.assertIn('约 23.55 g', html)
            self.assertIn('约 7.86 kg', html); self.assertIn('约 23.57 kg', html)
            self.assertIn('按CAD体积与所用材料密度估算，不含包装', html)
            self.assertEqual(len([attrs for _, attrs in page.attributes if 'data-customer-unit-weight' in attrs]), 2)
            self.assertEqual(len([attrs for _, attrs in page.attributes if 'data-customer-order-weight' in attrs]), 2)
            summary = next(attrs for _, attrs in page.attributes if 'data-customer-weight-summary' in attrs)
            self.assertEqual(summary['data-weight-status'], 'complete')
            self.assertEqual([attrs['data-total-price'] for _, attrs in page.attributes if 'data-total-price' in attrs], [value['totals']['total_price']])
        self.assertEqual(value, before)

    def test_unknown_table_weight_does_not_render_zero_and_tiny_weight_is_readable(self):
        value = known_result(); set_weight(value['lines'][0], None, None, None)
        set_weight(value['lines'][1], '0.000000001', '0.000000001', '0.000000003')
        page = render_customer_quote(value).decode()
        self.assertIn('尚未估算', page); self.assertIn('小于 0.01 g', page)
        self.assertNotIn('约 0.00 kg', page); self.assertIn('已知小计', page)

    def test_grouped_fasteners_keep_single_weight_and_sum_saved_order_weights_once(self):
        value = example(); groups = aggregate_display_groups(value)
        fastener = next(group for group in groups if group['part_name'] == 'CLS-M6-2')
        self.assertGreater(fastener['member_count'], 1)
        summary = weight_summary(value)
        self.assertEqual(Decimal(summary['known_total_net_weight_kg']), sum(Decimal(line['estimated_total_net_weight_kg']) for line in value['lines']))
        page = render_customer_quote(value).decode()
        self.assertEqual(len([attrs for _, attrs in Page(page).attributes if 'data-customer-unit-weight' in attrs]), len(groups))
        self.assertIn(display_weight(fastener['estimated_net_weight_kg']), page)
        self.assertIn(display_weight(fastener['estimated_total_net_weight_kg']), page)

    def test_private_pricing_and_free_text_assumptions_are_not_projected_as_weight_evidence(self):
        value = known_result(); line = value['lines'][0]
        line.update(material_assumption='PRIVATE-RATE-9999 元/kg 利润30%', weight_basis='PRIVATE-COST-8888',
                    material_raw='<img src=x onerror=alert(1)>')
        page = render_customer_quote(value).decode()
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', page)
        self.assertNotIn('<img src=x', page)
        payload = {'line_snapshot': {**line, 'material_rate': 'PRIVATE-RATE-9999'}, 'settings': value['settings']}
        projected = customer_part_projection(payload)
        self.assertEqual(projected['line_snapshot']['weight_assumptions']['source'], 'order_assumption')
        for marker in ('PRIVATE-RATE', 'PRIVATE-COST', 'material_rate', 'total_cost', 'rate_percent', 'pricing_method'):
            self.assertNotIn(marker, str(projected)); self.assertNotIn(marker, page)

    def test_weight_reads_keep_money_raw_exports_and_saved_snapshot_digest_identical(self):
        value = known_result(); before = copy.deepcopy(value); identity = digest(value)
        names = ('quote.json', 'bom.csv', 'cost.csv'); exports = {name: render_artifact(value, name)[0] for name in names}
        render_customer_quote(value); render_customer_quote(value, printable=True)
        CustomerUploadService._project({'id': 'BATCH-'+'1'*32, 'result': value, 'settings': value['settings']})
        self.assertEqual(exports, {name: render_artifact(value, name)[0] for name in names})
        self.assertEqual(digest(value), identity); self.assertEqual(value, before)


class CustomerWeightHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = http_fixture.CustomerProcessHTTPTests(methodName='test_bent_part_does_not_offer_flat_pattern_or_full_quotation')
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown); self.fixture.mixed = True

    def test_owner_quote_and_manifest_share_safe_saved_summary_without_new_work_or_history_changes(self):
        f = self.fixture; owner, outsider = f.session(), f.session(); order = f.finished(owner)
        folder = f.server.customer_batches._folder(order['id'])
        before = {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob('*') if path.is_file()}
        with (patch.object(f.server.customer_batches, '_native', side_effect=AssertionError('No native rerun')),
              patch.object(f.server.customer_batches.engineering_recognition, 'enqueue', side_effect=AssertionError('No model'))):
            status, shown = f.request(http_fixture.API+'/orders/'+order['id'], cookie=owner)
            self.assertEqual(status, 200, shown); summary = shown['quote']['weight_summary']
            status, manifest = f.request(shown['quote']['manufacturing_url'], cookie=owner)
            self.assertEqual(status, 200, manifest); self.assertEqual(manifest['weight_summary'], summary)
            self.assertTrue(summary['complete']); f.assert_no_costs(manifest)
            self.assertEqual(manifest['parts'][0]['weight_assumptions']['source'], 'order_assumption')
            for key in ('quote_url', 'print_url'):
                status, html = f.request(shown['quote'][key], cookie=owner)
                self.assertEqual(status, 200, html); page = Page(html.decode())
                self.assertTrue(any('data-customer-weight-summary' in attrs for _, attrs in page.attributes))
                self.assertEqual(f.request(shown['quote'][key], cookie=outsider)[0], 404)
            self.assertEqual(f.request(shown['quote']['manufacturing_url'], cookie=outsider)[0], 404)
        self.assertEqual({path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob('*') if path.is_file()}, before)
        self.assertEqual(len(f.server.customer_batches.get(order['id'])['runs']), 1)


if __name__ == '__main__': unittest.main()
