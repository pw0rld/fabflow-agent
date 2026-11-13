"""Quote-domain checks against synthetic geometry, executed on ZeroClave only."""
import copy
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cad_documents import CadDocumentStore
from cad_manufacturing import CadManufacturingService
from cad_quotation import CadQuotationService
from price_catalog import PriceCatalog
from workbench_store import WorkbenchError
from test_cad_manufacturing import STEP, native_properties_fixture, ready
import test_price_catalog

class CadQuotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cad = CadDocumentStore(Path(self.temp.name) / 'cad')
        self.document = self.cad.create('quote.step', STEP)
        self.did = self.document['id']
        self.manufacturing = CadManufacturingService(self.cad, native_runner=native_properties_fixture)
        self.manufacturing.start(self.did, {'expected_revision': 0})
        state = ready(self.manufacturing, self.did)
        self.assertEqual(state['status'], 'ready')
        for index, group in enumerate(state['groups']):
            group.update(name='板件' if index == 0 else '外购件', classification='make' if index == 0 else 'buy',
                         quantity_per_set=1 if index == 0 else 2, material='Q235B', thickness_mm=1)
        self.manufacturing.review(self.did, {'expected_revision': state['revision'], 'groups': state['groups'], 'bom_settings': {'order_sets': 3}})
        self.service = CadQuotationService(self.manufacturing)

    def tearDown(self):
        self.temp.cleanup()

    def guards(self, state):
        return {'expected_revision': state['revision'], 'source_revision': state['source_revision']}

    def prepared(self):
        state = self.service.get(self.did)
        inputs = copy.deepcopy(state['inputs'])
        inputs['review_confirmed'] = True
        inputs['pricing'] = {'method': 'cost_plus', 'rate_percent': '20', 'tax_excluded': True, 'freight_excluded': True}
        inputs['shared_fees'] = [{'name': '运输前包装', 'value': '10', 'inclusions': '独立包装材料，不含各零件加工和准备费'}]
        for line in inputs['lines']:
            line['inclusions'] = '明确测试输入，不代表工厂费率。'
            if line['mode'] == 'buy':
                line['purchase_price'] = '4'
            else:
                line.update(material_usage='0.5', material_rate='10', setup_fee='12',
                    operations=[{'name': '切割', 'rate': '2', 'basis': 'CNY/piece', 'inclusions': '切割'},
                                {'name': '折弯', 'rate': '3', 'basis': 'CNY/piece', 'inclusions': '折弯'}])
        return self.service.save_inputs(self.did, {**self.guards(state), 'inputs': inputs})

    def test_defaults_do_not_fabricate_usage_price_policy(self):
        state = self.service.get(self.did)
        self.assertEqual(state['inputs']['order_sets'], 3)
        self.assertIsNone(state['inputs']['pricing']['method'])
        self.assertIsNone(state['inputs']['lines'][0]['material_usage'])
        self.assertIsNone(state['inputs']['lines'][0]['material_rate'])
        self.assertFalse(state['validation']['ready'])
        self.assertEqual(state['catalog']['status'], 'empty')
        self.assertIn('quantity_candidate', next(iter(state['geometry_evidence'].values())))

    def test_costing_unit_basis_and_shared_fee_once(self):
        state = self.prepared()
        quote = self.service.estimate(self.did, self.guards(state))
        self.assertEqual(quote['lines'][0]['quantity'], 3)
        self.assertEqual(quote['lines'][0]['total_cost'], '42.00')
        self.assertEqual(quote['lines'][1]['quantity'], 6)
        self.assertEqual(quote['lines'][1]['total_cost'], '24.00')
        self.assertEqual(quote['totals']['total_cost'], '76.00')
        self.assertEqual(quote['totals']['total_price'], '91.20')
        self.assertEqual(quote['shared_price'], '12.00')

    def test_gross_margin_distinct_from_markup(self):
        state = self.prepared()
        state['inputs']['pricing']['method'] = 'gross_margin'
        state = self.service.save_inputs(self.did, {**self.guards(state), 'inputs': state['inputs']})
        quote = self.service.estimate(self.did, self.guards(state))
        self.assertEqual(quote['totals']['total_price'], '95.00')

    def test_unknown_not_zero_and_unsupported_unit_rejected(self):
        state = self.prepared()
        state['inputs']['lines'][0]['setup_fee'] = None
        state = self.service.save_inputs(self.did, {**self.guards(state), 'inputs': state['inputs']})
        self.assertFalse(state['validation']['ready'])
        with self.assertRaises(WorkbenchError):
            self.service.estimate(self.did, self.guards(state))
        state['inputs']['lines'][0]['operations'][0]['basis'] = 'CNY/hour'
        with self.assertRaises(WorkbenchError):
            self.service.save_inputs(self.did, {**self.guards(state), 'inputs': state['inputs']})

    def test_per_order_and_setup_do_not_double_count(self):
        state = self.prepared()
        state['inputs']['lines'][0]['operations'][0]['basis'] = 'CNY/order'
        state = self.service.save_inputs(self.did, {**self.guards(state), 'inputs': state['inputs']})
        self.assertFalse(state['validation']['ready'])
        state['inputs']['lines'][0]['setup_fee'] = '0'
        state = self.service.save_inputs(self.did, {**self.guards(state), 'inputs': state['inputs']})
        quote = self.service.estimate(self.did, self.guards(state))
        self.assertEqual(quote['lines'][0]['process_cost'], '11.00')

    def test_issue_idempotent_immutable_and_source_bound(self):
        state = self.prepared()
        payload = {**self.guards(state), 'request_id': 'test-quote-1'}
        first = self.service.issue(self.did, payload)['quote']
        self.assertEqual(first['source_snapshot']['filename'], 'quote.step')
        again = self.service.issue(self.did, payload)['quote']
        self.assertEqual(first['id'], again['id'])
        self.assertTrue(first['source_current'])
        state['inputs']['remarks'] = '重新核对'
        newer = self.service.save_inputs(self.did, {**self.guards(state), 'inputs': state['inputs']})
        old = self.service.get_quote(self.did, first['id'])
        self.assertFalse(old['source_current'])
        self.assertEqual(old['totals']['total_price'], '91.20')
        self.assertEqual(old['input_snapshot']['remarks'], '')
        self.assertEqual(len(newer['quotes']), 1)
        html, mime, download = self.service.document(self.did, first['id'], 'quote.html')
        self.assertIn('历史报价'.encode(), html)
        self.assertIn('text/html', mime)
        self.assertFalse(download)
        with self.assertRaises(WorkbenchError):
            self.service.issue(self.did, {**self.guards(newer), 'request_id': 'test-quote-1'})

    def test_stale_geometry_requires_rebase_without_mutating_cad(self):
        state = self.prepared()
        manufacturing = self.manufacturing.get(self.did)
        manufacturing['groups'][0]['name'] = '重命名板件'
        changed = self.manufacturing.review(self.did, {'expected_revision': manufacturing['revision'], 'groups': manufacturing['groups'], 'bom_settings': manufacturing['bom_settings']})
        stale = self.service.get(self.did)
        self.assertFalse(stale['source_current'])
        with self.assertRaises(WorkbenchError):
            self.service.issue(self.did, {**self.guards(stale), 'request_id': 'stale-quote'})
        rebased = self.service.rebase(self.did, self.guards(stale))
        self.assertTrue(rebased['source_current'])
        self.assertEqual(rebased['inputs']['lines'][0]['material_rate'], '10')
        self.assertFalse(rebased['inputs']['review_confirmed'])
        self.assertEqual(self.manufacturing.get(self.did)['revision'], changed['revision'])

    def test_demo_is_explicit_and_does_not_choose_pricing(self):
        before = self.manufacturing.get(self.did)
        state = self.service.get(self.did)
        demo = self.service.demo_inputs(self.did, self.guards(state))
        self.assertIsNone(demo['inputs']['pricing']['rate_percent'])
        self.assertIsNone(demo['inputs']['pricing']['method'])
        self.assertFalse(demo['inputs']['review_confirmed'])
        self.assertIsNotNone(demo['demo'])
        self.assertIn('演示', demo['inputs']['remarks'])
        self.assertEqual(self.manufacturing.get(self.did), before)

    def test_bad_numeric_structures_rejected_before_save(self):
        for bad in ['NaN', '-1', '1e300', True, [], {}, '9' * 41]:
            state = self.service.get(self.did)
            state['inputs']['lines'][0]['material_rate'] = bad
            with self.subTest(bad=bad), self.assertRaises(WorkbenchError):
                self.service.save_inputs(self.did, {**self.guards(state), 'inputs': state['inputs']})
        state = self.service.get(self.did)
        state['inputs']['pricing']['method'] = []
        with self.assertRaises(WorkbenchError):
            self.service.save_inputs(self.did, {**self.guards(state), 'inputs': state['inputs']})

    def test_exports_escape_text_and_csv_formulas(self):
        state = self.prepared()
        state['inputs']['remarks'] = '<script>alert(1)</script>'
        state['inputs']['lines'][0]['inclusions'] = '=FORMULA()'
        state = self.service.save_inputs(self.did, {**self.guards(state), 'inputs': state['inputs']})
        quote = self.service.issue(self.did, {**self.guards(state), 'request_id': 'exports'})['quote']
        content, _, _ = self.service.document(self.did, quote['id'], 'quote.html')
        self.assertNotIn(b'<script>alert(1)</script>', content)
        self.assertIn(b'&lt;script&gt;', content)
        content, _, _ = self.service.document(self.did, quote['id'], 'cost.csv')
        self.assertIn("'=FORMULA()", content.decode('utf-8-sig'))
        rows = list(csv.reader(io.StringIO(self.service.document(self.did, quote['id'], 'bom.csv')[0].decode('utf-8-sig'))))
        self.assertEqual(len(rows), 3)

    def test_half_cent_rounding_is_applied_to_unit_price(self):
        state = self.prepared()
        inputs = state['inputs']
        inputs['pricing']['rate_percent'] = '0'
        inputs['shared_fees'] = []
        inputs['lines'][1]['selected'] = False
        line = inputs['lines'][0]
        line.update(material_usage='0.5', material_rate='0.01', setup_fee='0')
        for op in line['operations']:
            op['rate'] = '0'
        state = self.service.save_inputs(self.did, {**self.guards(state), 'inputs': inputs})
        result = self.service.estimate(self.did, self.guards(state))
        self.assertEqual(result['lines'][0]['unit_price'], '0.01')
        self.assertEqual(result['lines'][0]['total_price'], '0.03')

    def test_catalog_adoption_retains_version_and_rejects_changed_rate(self):
        catalog = PriceCatalog(Path(self.temp.name) / 'catalog.sqlite3')
        draft = catalog.create(test_price_catalog.PriceCatalogTests.payload(thickness_min='1', thickness_max='1', rate='10', valid_from='2000-01-01', valid_to='2099-12-31'))
        published = catalog.publish(draft['entry_id'], draft['version'])
        self.service = CadQuotationService(self.manufacturing, catalog)
        state = self.prepared()
        line = state['inputs']['lines'][0]
        line['material_form'] = '板材'
        line['price_refs'] = [{'entry_id': published['entry_id'], 'version': published['version']}]
        state = self.service.save_inputs(self.did, {**self.guards(state), 'inputs': state['inputs']})
        result = self.service.estimate(self.did, self.guards(state))
        self.assertEqual(result['price_snapshots'][0]['snapshot_sha256'], published['snapshot_sha256'])
        state['inputs']['lines'][0]['material_rate'] = '11'
        state = self.service.save_inputs(self.did, {**self.guards(state), 'inputs': state['inputs']})
        with self.assertRaises(WorkbenchError):
            self.service.estimate(self.did, self.guards(state))

if __name__ == '__main__':
    unittest.main()
