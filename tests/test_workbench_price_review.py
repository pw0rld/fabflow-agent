"""Focused catalogue adoption integration; local temporary records only."""
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from prep_record import RecordError
from price_catalog import PriceCatalog
from workbench_store import Workbench


class PriceAdoptionIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='price-adoption-review-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog = PriceCatalog(self.root / 'prices.sqlite3')
        self.app = Workbench(self.root / 'tasks', catalog=self.catalog)
        self.task = self.app.create({'event_id': 'review-inquiry', 'inquiry': '离线模拟支架询价', 'simulated': True})
        self.task_id = self.task['id']
        self.price_payload = {
            'kind': 'material', 'name': '独立构造材料测试价', 'rate': '20', 'unit': 'CNY/kg',
            'material_grade': 'Q235B', 'material_form': '板材', 'thickness_min': '2', 'thickness_max': '4',
            'qty_min': 1, 'qty_max': 99, 'valid_from': '2020-01-01', 'valid_to': '2099-12-31',
            'currency': 'CNY', 'tax_included': False, 'freight_included': False,
            'simulated': True, 'source': '仅供本地回归的人工构造费率', 'inclusions': ['材料价未计税费运费'],
        }
        draft = self.catalog.create(self.price_payload)
        self.published = self.catalog.publish(draft['entry_id'], 1)
        self.save('requirements', 'requirements', {
            'product_name': '测试支架', 'quantity': 10, 'material_family': '碳钢',
            'material_grade': 'Q235B', 'thickness': '3', 'surface_treatment': '镀锌',
        })

    def save(self, stage, event_id, values, **extra):
        self.task = self.app.save_fields(self.task_id, stage, {
            'event_id': event_id, 'expected_revision': self.task['revision'],
            'values': copy.deepcopy(values), **extra,
        })
        return self.task

    def adopt(self, complete=True):
        values = {'material_rate': '20'}
        if complete:
            values.update({
                'material_usage_per_piece': '0.4', 'process_route': ['切割', '折弯', '镀锌', '包装'],
                'operation_rates': [
                    {'operation': operation, 'value': amount, 'unit': 'CNY/piece'}
                    for operation, amount in [('切割', '12'), ('折弯', '18'), ('镀锌', '6'), ('包装', '4')]],
                'setup_fee': '200', 'fee_inclusions': ['工序已含人工设备耗材；准备费独立收取。'],
            })
        return self.save('technical', 'adopt-material', values,
                         pricing_context={'material_form': '板材', 'product_family': '测试支架'},
                         price_selections={'material_rate': {
                             'entry_id': self.published['entry_id'], 'version': 1}})

    def issue(self, event_id):
        return self.app.issue(self.task_id, {
            'event_id': event_id, 'expected_revision': self.task['revision'],
            'policy': {'pricing_method': 'cost_plus', 'rate_percent': '20',
                       'tax_excluded': True, 'freight_excluded': True},
        })

    def quote_hashes(self, quote_id):
        directory = self.app.records.task_path(self.task_id) / 'quotes' / quote_id
        return {file.name: hashlib.sha256(file.read_bytes()).hexdigest() for file in directory.iterdir()}

    def test_material_can_be_adopted_before_process_route_is_known(self):
        task = self.adopt(complete=False)
        self.assertEqual(task['fields']['material_rate']['value'], '20')
        self.assertEqual(task['fields']['material_rate']['status'], 'verified')
        self.assertIsNone(task['fields']['process_route']['value'])
        self.assertFalse(task['validation']['ready'])
        self.assertEqual(task['price_selections']['material_rate']['entry_id'], self.published['entry_id'])

    def test_quantity_outside_adopted_price_range_blocks_only_new_quotation(self):
        self.adopt()
        old_quote = self.issue('before-quantity-change')
        before = self.quote_hashes(old_quote['quote_id'])
        self.save('requirements', 'quantity-200', {'quantity': 200})
        self.assertFalse(self.app.task(self.task_id)['validation']['ready'])
        with self.assertRaises(RecordError):
            self.issue('after-quantity-change')
        self.assertFalse((self.app.records.task_path(self.task_id) / 'quotes' / 'Q-after-quantity-change').exists())
        historical = self.app._quote_result(self.task_id, old_quote['quote_id'])
        self.assertEqual(historical['quantity'], 10)
        self.assertEqual(historical['amounts']['total_price'], '816.00')
        self.assertEqual(before, self.quote_hashes(old_quote['quote_id']))

    def test_new_publication_requires_reconfirmation_before_next_quotation(self):
        self.adopt()
        old_quote = self.issue('before-new-price')
        before = self.quote_hashes(old_quote['quote_id'])
        newer = self.catalog.new_version(self.published['entry_id'], dict(self.price_payload, rate='40'))
        self.catalog.publish(newer['entry_id'], newer['version'])
        self.assertFalse(self.app.task(self.task_id)['validation']['ready'])
        with self.assertRaises(RecordError):
            self.issue('after-new-price')
        self.assertFalse((self.app.records.task_path(self.task_id) / 'quotes' / 'Q-after-new-price').exists())
        self.assertEqual(self.app.task(self.task_id)['fields']['material_rate']['value'], '20')
        self.assertEqual(self.app._quote_result(self.task_id, old_quote['quote_id'])['amounts']['total_price'], '816.00')
        self.assertEqual(before, self.quote_hashes(old_quote['quote_id']))

    def test_unrelated_fee_edit_cannot_clear_unreviewed_material_price(self):
        self.adopt()
        self.save('requirements', 'outside-material-range', {'quantity': 200})
        self.assertFalse(self.task['validation']['ready'])
        self.save('technical', 'change-only-setup', {'setup_fee': '201'})
        self.assertIn('material_rate', self.task['price_selections'])
        self.assertFalse(self.task['validation']['ready'])
        with self.assertRaises(RecordError):
            self.issue('blocked-after-unrelated-fee-edit')

    def test_partial_adoption_keeps_other_unsubmitted_price_references(self):
        self.adopt()
        self.save('requirements', 'outside-material-range', {'quantity': 200})
        setup_payload = dict(self.price_payload, kind='setup', name='独立准备费测试价', rate='200',
                             unit='CNY/order', qty_max=999, product_family='测试支架')
        draft = self.catalog.create(setup_payload)
        setup_price = self.catalog.publish(draft['entry_id'], 1)
        self.save('technical', 'adopt-only-setup', {'setup_fee': '200'},
                  price_selections={'setup_fee': {'entry_id': setup_price['entry_id'], 'version': 1}})
        self.assertEqual(set(self.task['price_selections']), {'material_rate', 'setup_fee'})
        self.assertEqual(self.task['price_selections']['material_rate']['entry_id'], self.published['entry_id'])
        self.assertFalse(self.task['validation']['ready'])
        with self.assertRaises(RecordError):
            self.issue('blocked-after-partial-adoption')

    def test_record_cli_fee_change_cannot_be_attributed_to_old_selected_price(self):
        self.adopt()
        source = self.root / 'cli-technical-reply.txt'
        source.write_text('模拟技术员明确将本单材料单价更正为25元/kg，替代此前20元/kg。', encoding='utf-8')
        actor = '离线回归技术员'
        self.app.records.apply(self.task_id, 'apply_technical_input', {
            'sources': [{'id': 'CLI-TECHNICAL', 'path': str(source), 'kind': 'technical_input',
                         'actor': actor, 'simulated': True}],
            'fields': [{'name': 'material_rate', 'raw_value': '25', 'raw_unit': 'CNY/kg',
                        'value': '25', 'unit': 'CNY/kg', 'status': 'verified',
                        'evidence': {'source_type': 'technical_input', 'reference': 'CLI-TECHNICAL',
                                     'actor': actor, 'simulated': True, 'page': None, 'region': None},
                        'reason': '依照本次技术补录明确替代此前材料单价。'}],
        }, self.task['revision'], 'cli-technical-change')
        self.task = self.app.task(self.task_id)
        self.assertEqual(self.task['fields']['material_rate']['value'], '25')
        self.assertEqual(self.task['price_selections']['material_rate']['rate'], '20')
        self.assertFalse(self.task['validation']['ready'])
        with self.assertRaises(RecordError):
            self.issue('blocked-mismatched-price-source')


if __name__ == '__main__':
    unittest.main()
