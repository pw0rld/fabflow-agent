"""Independent values for batch quoting; synthetic geometry is not CAD accuracy."""
import copy
import csv
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_costing import calculate_batch, render_artifact, validate_settings

SHA = 'a' * 64


def settings(**changes):
    result = {'order_sets': 3, 'pricing_method': 'cost_plus', 'rate_percent': '20',
              'material_profile': 'carbon_steel', 'rate_profile': 'demo-v1',
              'demo_confirmed': True, 'scope_confirmed': True}
    result.update(changes)
    return result


def solid(index, definition='plate', path=None, volume=1000000, name='Plate', grade=None, valid=True, closed=True):
    return {'id': 'S' + str(index), 'definition_id': definition,
            'occurrence_path': path if path is not None else ['assembly', str(index)],
            'name': name, 'volume_mm3': volume, 'topology': {'valid': valid, 'closed': closed},
            'material': {'status': 'candidate' if grade else 'not_found', 'value': grade},
            'mesh': {'positions': [0, 0, 0], 'indices': []}, 'thumbnail': '/private/never-export.png'}


def document(solids, *, file_id='F001', name='assembly.stp', status='ready'):
    return {'id': file_id, 'name': name, 'relative_path': 'drawings/' + name, 'sha256': SHA,
            'status': status, 'error': None, 'geometry': {'output_unit': 'mm', 'source_sha256': SHA,
            'solids': solids, 'private_path': '/private/never-export/geometry.json'}}


class BatchQuoteCostingTests(unittest.TestCase):
    def known_case(self):
        # Two identical plates and four bought screws in each of three sets.
        # 1e6 mm³ steel plate = 7.85 kg; each screw = .00785 kg.
        made = [solid(1, 'plate/solid-1'), solid(2, 'plate/solid-1')]
        bought = [solid(i, 'screw/solid-1', name='M4x20', volume=1000) for i in range(3, 7)]
        return document(made + bought)

    def test_explicit_choices_are_required_and_gross_margin_is_bounded(self):
        for changes in ({'order_sets': True}, {'order_sets': 0}, {'rate_percent': None},
                        {'rate_percent': 'NaN'}, {'rate_percent': 20}, {'demo_confirmed': False},
                        {'scope_confirmed': False}, {'material_profile': 'unknown'},
                        {'rate_profile': 'factory-real'}, {'pricing_method': 'gross_margin', 'rate_percent': '100'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_settings(settings(**changes))
        incomplete = settings()
        incomplete.pop('rate_percent')
        with self.assertRaises(ValueError):
            validate_settings(incomplete)
        self.assertEqual(validate_settings(settings(rate_percent='0'))['rate_percent'], '0')

    def test_known_cost_plus_values_and_kg_units(self):
        result = calculate_batch([self.known_case()], settings())
        self.assertEqual(len(result['lines']), 2)
        made, bought = result['lines']
        self.assertEqual((made['quantity_per_set'], made['quantity_total']), (2, 6))
        self.assertEqual(made['estimated_net_weight_kg'], '7.85')
        self.assertEqual(made['estimated_set_net_weight_kg'], '15.7')
        self.assertEqual(made['estimated_total_net_weight_kg'], '47.1')
        self.assertEqual(made['material_usage_kg'], '9.23529412')
        self.assertEqual(made['material_cost'], '332.47')
        self.assertEqual(made['process_cost'], '376.80')
        self.assertEqual(made['setup_cost'], '30.00')
        self.assertEqual(made['total_cost'], '739.27')
        self.assertEqual(made['unit_cost'], '123.21')
        self.assertEqual(made['unit_price'], '147.85')
        self.assertEqual(made['total_price'], '887.10')
        self.assertEqual((bought['quantity_per_set'], bought['quantity_total']), (4, 12))
        self.assertEqual(bought['mode'], 'buy')
        self.assertEqual(bought['purchase_cost'], '9.60')
        self.assertEqual(bought['unit_price'], '0.96')
        self.assertEqual(bought['total_price'], '11.52')
        self.assertIsNone(bought['material_usage_kg'])
        self.assertEqual(result['totals']['total_cost'], '748.87')
        self.assertEqual(result['totals']['total_price'], '898.62')
        self.assertEqual(result['weight_summary']['estimated_set_net_weight_kg'], '15.7314')
        self.assertEqual(result['weight_summary']['estimated_total_net_weight_kg'], '47.1942')
        self.assertEqual(result['totals']['unpriced_lines'], 0)

    def test_target_margin_uses_a_different_formula(self):
        result = calculate_batch([self.known_case()], settings(pricing_method='gross_margin'))
        self.assertEqual(result['lines'][0]['unit_price'], '154.01')
        self.assertEqual(result['lines'][0]['total_price'], '924.06')
        self.assertEqual(result['lines'][1]['unit_price'], '1.00')
        self.assertEqual(result['totals']['total_price'], '936.06')

    def test_composite_solids_count_occurrences_instead_of_meshes(self):
        bodies = [solid(1, 'part/solid-1', ['root', 'a'], volume=400000),
                  solid(2, 'part/solid-2', ['root', 'a'], volume=600000),
                  solid(3, 'part/solid-1', ['root', 'b'], volume=400000),
                  solid(4, 'part/solid-2', ['root', 'b'], volume=600000)]
        result = calculate_batch([document(bodies)], settings())
        self.assertEqual(len(result['lines']), 1)
        line = result['lines'][0]
        self.assertEqual(line['quantity_per_set'], 2)
        self.assertEqual(line['native_solid_count'], 4)
        self.assertEqual(line['volume_mm3'], '1000000')
        self.assertEqual(line['estimated_net_weight_kg'], '7.85')
        self.assertEqual(line['setup_cost'], '30.00')

    def test_source_files_never_merge_same_native_definitions(self):
        first = document([solid(1, 'part/solid-1')])
        second = document([solid(2, 'part/solid-1')], file_id='F002', name='other.stp')
        result = calculate_batch([first, second], settings())
        self.assertEqual(len(result['lines']), 2)
        self.assertEqual([line['quantity_per_set'] for line in result['lines']], [1, 1])
        self.assertEqual(result['totals']['setup_cost'], '60.00')

    def test_missing_or_ambiguous_identity_does_not_invent_quantity(self):
        examples = [
            [solid(1, definition=None)],
            [solid(1, 'part/solid-1', path=[])],
            [solid(1, 'part/solid-1', ['root', 'a']), solid(2, 'part/solid-1', ['root', 'a'])],
            [solid(1, 'part/solid-1', ['root', 'a']), solid(2, 'part/solid-2', ['root', 'b'])],
            [solid(1, 'part/solid-1', volume=1000000), solid(2, 'part/solid-1', volume=1100000)],
        ]
        for bodies in examples:
            with self.subTest(bodies=bodies):
                result = calculate_batch([document(bodies)], settings())
                self.assertTrue(all(line['quantity_per_set'] is None for line in result['lines']))
                self.assertTrue(all(not line['priced'] for line in result['lines']))
                self.assertIsNone(result['totals']['total_price'])
                self.assertIsNone(result['weight_summary']['estimated_total_net_weight_kg'])
                self.assertGreater(result['weight_summary']['missing_estimated_lines'], 0)

    def test_known_grade_overrides_selected_fallback_but_generic_steel_does_not(self):
        known = calculate_batch([document([solid(1, grade='SUS316')])], settings(material_profile='al5052'))['lines'][0]
        self.assertEqual(known['material_raw'], 'SUS316')
        self.assertEqual(known['material_profile'], 'ss316')
        self.assertEqual(known['density_kg_m3'], '7980')
        self.assertEqual(known['estimated_net_weight_kg'], '7.98')
        self.assertEqual(known['material_rate'], '28')
        self.assertEqual(known['confidence'], 'cad_material')
        generic = calculate_batch([document([solid(1, grade='Steel')])], settings(material_profile='al5052'))['lines'][0]
        self.assertEqual(generic['material_raw'], 'Steel')
        self.assertEqual(generic['material_profile'], 'al5052')
        self.assertEqual(generic['estimated_net_weight_kg'], '2.68')
        self.assertEqual(generic['confidence'], 'assumed_material')
        self.assertIn('未完整确认', generic['material_assumption'])

    def test_invalid_geometry_can_estimate_but_cannot_price(self):
        good = document([solid(1)])
        bad = document([solid(2, valid=False, volume=500000)], file_id='F002', name='bad.stp')
        result = calculate_batch([good, bad], settings())
        line = result['lines'][1]
        self.assertEqual(line['confidence'], 'low')
        self.assertEqual(line['estimated_net_weight_kg'], '3.925')
        self.assertEqual(line['estimated_total_net_weight_kg'], '11.775')
        self.assertFalse(line['priced'])
        self.assertIsNone(line['total_cost'])
        self.assertIsNone(line['total_price'])
        self.assertIsNone(line['material_usage_kg'])
        self.assertEqual(result['totals']['priced_lines'], 1)
        self.assertEqual(result['totals']['unpriced_lines'], 1)
        self.assertEqual(result['pricing_scope'], '已计价范围')
        self.assertEqual(result['weight_summary']['estimated_total_net_weight_kg'], '35.325')
        self.assertEqual(result['weight_summary']['low_confidence_total_net_weight_kg'], '11.775')

    def test_nonpositive_volume_or_failed_file_keeps_totals_unknown(self):
        for bad_volume in (0, -1, None, 'NaN'):
            with self.subTest(bad_volume=bad_volume):
                result = calculate_batch([document([solid(1, volume=bad_volume)])], settings())
                self.assertIsNone(result['totals']['total_price'])
                self.assertIsNone(result['weight_summary']['estimated_total_net_weight_kg'])
        failed = document([], status='failed')
        failed['error'] = '读取失败'
        result = calculate_batch([self.known_case(), failed], settings())
        self.assertEqual(result['totals']['total_price'], '898.62')
        self.assertEqual(result['totals']['unpriced_lines'], 1)
        self.assertIsNone(result['weight_summary']['estimated_total_net_weight_kg'])
        self.assertEqual(result['weight_summary']['known_total_net_weight_kg'], '47.1942')
        placeholder = result['lines'][-1]
        self.assertEqual(placeholder['quantity_per_set'], 1)  # original root scope only
        self.assertEqual(placeholder['quantity_total'], 3)
        self.assertIn('根装配占位', placeholder['notes'][0])

    def test_attachments_add_no_invented_parts_or_process_charges(self):
        attachment = {'id': 'F002', 'name': 'requirements.png', 'relative_path': 'requirements.png',
                      'sha256': 'b' * 64, 'status': 'reference', 'error': None}
        baseline = calculate_batch([self.known_case()], settings())
        result = calculate_batch([self.known_case(), attachment], settings())
        self.assertEqual(result['lines'], baseline['lines'])
        self.assertEqual(result['totals'], baseline['totals'])
        self.assertEqual(len(result['source_files']), 2)
        self.assertTrue(any('未自动解释' in note for note in result['limitations']))
        only = calculate_batch([attachment], settings())
        self.assertEqual(only['lines'], [])
        self.assertIsNone(only['totals']['total_price'])

    def test_geometry_identity_or_units_mismatch_excludes_the_file(self):
        for field, value in (('source_sha256', 'b' * 64), ('output_unit', 'inch')):
            doc = self.known_case()
            doc['geometry'][field] = value
            result = calculate_batch([doc], settings())
            self.assertEqual(result['source_files'][0]['status'], 'failed')
            self.assertIsNone(result['totals']['total_price'])
            self.assertIsNone(result['lines'][0]['estimated_net_weight_kg'])

    def test_public_result_is_deterministic_and_does_not_export_geometry(self):
        docs, choices = [self.known_case()], settings()
        originals = copy.deepcopy((docs, choices))
        first = calculate_batch(docs, choices)
        self.assertEqual((docs, choices), originals)
        self.assertEqual(first, calculate_batch(docs, choices))
        body, content_type = render_artifact(first, 'quote.json')
        text = body.decode()
        self.assertEqual(json.loads(text), first)
        self.assertNotIn('never-export', text)
        self.assertNotIn('"mesh"', text)
        self.assertFalse(first['engineering_approved'])
        self.assertFalse(first['business_approved'])
        self.assertTrue(first['simulated'])
        self.assertEqual(first['status'], 'pending_review')
        self.assertIn('application/json', content_type)

    def test_html_and_csv_escape_names_and_preserve_weight_and_missing_cost(self):
        doc = document([solid(1, name='=SUM(1,1)'), solid(2, definition='bad', name='<script>alert(1)</script>', valid=False)], name='a<&>.stp')
        result = calculate_batch([doc], settings())
        result['name'] = '<img src=x onerror=alert(1)>'
        body, content_type = render_artifact(result, 'quote.html')
        text = body.decode()
        self.assertIn('text/html', content_type)
        self.assertNotIn('<script>', text)
        self.assertNotIn('<img src=x', text)
        self.assertIn('&lt;script&gt;', text)
        self.assertIn('&lt;img', text)
        self.assertIn('模拟报价草稿', text)
        self.assertIn('未计价，不代表免费', text)
        self.assertIn('不含运费', text)
        self.assertIn('kg', text)
        for filename in ('bom.csv', 'cost.csv'):
            body, content_type = render_artifact(result, filename)
            rows = list(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))
            self.assertIn('text/csv', content_type)
            self.assertEqual(rows[0]['物料名称'], "'=SUM(1,1)")
            self.assertEqual(rows[0]['单件预估净重_kg'], '7.85')
            self.assertEqual(rows[1]['订单预估净重_kg'], '23.55')
            if filename == 'cost.csv':
                self.assertEqual(rows[1]['报价金额_CNY'], '')
        with self.assertRaises(ValueError):
            render_artifact(result, '../../other')


if __name__ == '__main__':
    unittest.main()
