"""Fee guidance against real temporary records/calculator; no server or model."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from prep_record import RecordError
from price_catalog import PriceCatalog
import workbench_store
from workbench_store import Workbench, WorkbenchError


REQUIREMENTS = {
    'product_name': '测试支架', 'quantity': 10, 'material_family': '碳钢',
    'material_grade': 'Q235B', 'thickness': '3', 'surface_treatment': '镀锌',
}
TECHNICAL = {
    'material_usage_per_piece': '0.4', 'material_rate': '20',
    'process_route': ['折弯'],
    'operation_rates': [{'operation': '折弯', 'value': '18', 'unit': 'CNY/piece'}],
    'setup_fee': '200', 'fee_inclusions': ['折弯按件费；准备费另计。'],
}
POLICY = {
    'pricing_method': 'cost_plus', 'rate_percent': '20',
    'tax_excluded': True, 'freight_excluded': True,
}
ORDER_RATE = [{'operation': '折弯', 'value': '100', 'unit': 'CNY/order'}]


class FeeGuidanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='fee-guidance-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = Workbench(self.root / 'tasks')
        self.task = self.app.create({
            'event_id': 'fee-inquiry', 'inquiry': '仅供离线测试的模拟支架询价。',
            'simulated': True,
        })
        self.task_id = self.task['id']

    def save(self, stage, event_id, values, **extra):
        self.task = self.app.save_fields(self.task_id, stage, {
            'event_id': event_id, 'expected_revision': self.task['revision'],
            'values': copy.deepcopy(values), **extra,
        })
        return self.task

    def complete(self, *, by_order=False):
        self.save('requirements', 'requirements', REQUIREMENTS)
        technical = copy.deepcopy(TECHNICAL)
        if by_order:
            technical['operation_rates'] = copy.deepcopy(ORDER_RATE)
        self.save('technical', 'technical', technical)

    def overlaps(self, task=None):
        return [issue for issue in (task or self.task)['validation']['issues']
                if issue['code'] == 'fee_overlap']

    def issue(self, event_id):
        return self.app.issue(self.task_id, {
            'event_id': event_id, 'expected_revision': self.task['revision'],
            'policy': copy.deepcopy(POLICY),
        })

    def files(self, directory):
        return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.rglob('*') if path.is_file()}

    def adopt_setup_price(self):
        catalog = PriceCatalog(self.root / 'prices.sqlite3')
        self.app.catalog = catalog
        self.setup_price_payload = {
            'kind': 'setup', 'name': '离线模拟准备费', 'rate': '200', 'unit': 'CNY/order',
            'material_grade': 'Q235B', 'material_form': '板材', 'product_family': '测试支架',
            'thickness_min': '2', 'thickness_max': '4', 'qty_min': 1, 'qty_max': 99,
            'valid_from': '2020-01-01', 'valid_to': '2099-12-31', 'currency': 'CNY',
            'tax_included': False, 'freight_included': False, 'simulated': True,
            'source': '仅用于本地回归的人工构造费率', 'inclusions': ['准备费单独收取。'],
        }
        draft = catalog.create(self.setup_price_payload)
        published = catalog.publish(draft['entry_id'], 1)
        self.save('technical', 'adopt-setup', {'setup_fee': '200'},
                  pricing_context={'material_form': '板材', 'product_family': '测试支架'},
                  price_selections={'setup_fee': {'entry_id': published['entry_id'], 'version': 1}})
        return published

    def interrupt_fee_save_after_record_commit(self, *, legacy=False):
        payload = {
            'event_id': 'interrupted-zero', 'expected_revision': self.task['revision'],
            'values': {'setup_fee': '0'},
        }
        form_name = 'form-interrupted-zero.json'
        original_save = self.app._save_state
        original_write = workbench_store.atomic_write

        def fail_final_sidecar(task_id, state):
            if state['events'].get(payload['event_id'], {}).get('status') == 'done':
                raise OSError('offline simulated interruption after record commit')
            return original_save(task_id, state)

        def write_form(path, text):
            if legacy and path.name == form_name:
                saved = json.loads(text)
                saved.pop('resulting_price_selections', None)
                text = workbench_store.json_text(saved)
            return original_write(path, text)

        with patch.object(self.app, '_save_state', side_effect=fail_final_sidecar), \
                patch.object(workbench_store, 'atomic_write', side_effect=write_form):
            with self.assertRaises(OSError):
                self.app.save_fields(self.task_id, 'technical', payload)
        self.assertEqual(self.app.records.load(self.task_id)['data_revision'],
                         payload['expected_revision'] + 1)
        self.assertEqual(self.app._state(self.task_id)['events'][payload['event_id']]['status'],
                         'pending')
        self.assertIn('setup_fee', self.app._state(self.task_id)['price_selections'])
        return payload, self.app.records.task_path(self.task_id) / form_name

    def test_overlap_is_locatable_before_other_required_fields_are_complete(self):
        self.save('technical', 'only-conflicting-fees', {
            'setup_fee': '200', 'operation_rates': ORDER_RATE,
        })
        self.assertIsNone(self.task['fields']['quantity']['value'])
        self.assertIsNone(self.task['fields']['process_route']['value'])
        self.assertFalse(self.task['validation']['ready'])
        issues = self.overlaps()
        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue['field'], 'setup_fee')
        self.assertEqual(issue['audience'], 'technical')
        self.assertEqual(issue['related_fields'],
                         ['operation_rates', 'setup_fee', 'fee_inclusions'])
        self.assertEqual(issue['target'],
                         {'stage': 'technical', 'section': 'fees', 'field': 'setup_fee'})
        self.assertEqual(issue['details'], {'setup_fee': '200', 'operation_rates': ORDER_RATE})
        for concrete_value in ('折弯', '100', '200'):
            self.assertIn(concrete_value, issue['message'])

    def test_editing_only_inclusions_does_not_remove_financial_guard(self):
        self.complete(by_order=True)
        self.assertEqual(len(self.overlaps()), 1)
        self.save('technical', 'explain-only', {
            'fee_inclusions': ['操作者补充说明：折弯与准备费包含项已经核对。'],
        })
        self.assertEqual(len(self.overlaps()), 1)
        self.assertFalse(self.task['validation']['ready'])
        self.assertEqual(self.task['fields']['setup_fee']['value'], '200')
        self.assertEqual(self.task['fields']['operation_rates']['value'], ORDER_RATE)
        with self.assertRaises(RecordError):
            self.issue('still-blocked')
        self.assertFalse((self.app.records.task_path(self.task_id) / 'quotes' /
                          'Q-still-blocked').exists())

    def test_explicit_saved_zero_resolves_overlap_preserves_values_and_old_quote(self):
        self.complete()
        quote = self.issue('before-fee-edit')
        quote_path = self.app.records.task_path(self.task_id) / 'quotes' / quote['quote_id']
        quote_hashes = self.files(quote_path)
        self.save('technical', 'change-to-order', {'operation_rates': ORDER_RATE})
        self.assertEqual(len(self.overlaps()), 1)
        previous_fields = copy.deepcopy(self.task['fields'])
        self.save('technical', 'explicit-zero', {'setup_fee': '0'})
        self.assertFalse(self.overlaps())
        self.assertTrue(self.task['validation']['ready'])
        for name, previous in previous_fields.items():
            if name != 'setup_fee':
                self.assertEqual(self.task['fields'][name], previous)
        setup = self.task['fields']['setup_fee']
        self.assertEqual(setup['value'], '0')
        self.assertEqual(setup['status'], 'verified')
        self.assertEqual(setup['evidence']['source_type'], 'operator_review')
        self.assertEqual(setup['evidence']['reference'], 'FORM-explicit-zero')
        record = self.app.records.load(self.task_id)
        event = next(item for item in record['events'] if item['id'] == 'explicit-zero')
        self.assertEqual(event['operation'], 'apply_technical_input')
        form = json.loads((self.app.records.task_path(self.task_id) /
                           'form-explicit-zero.json').read_text())
        self.assertEqual(form['submission']['values'], {'setup_fee': '0'})
        self.assertEqual(self.files(quote_path), quote_hashes)
        self.assertFalse(self.app._quote_result(self.task_id, quote['quote_id'])['source_current'])
        new_quote = self.issue('after-explicit-zero')
        self.assertEqual(new_quote['amounts']['total_price'], '216.00')
        self.assertEqual(self.files(quote_path), quote_hashes)

    def test_piece_rate_and_setup_fee_continue_to_use_existing_calculator(self):
        self.complete()
        self.assertTrue(self.task['validation']['ready'])
        self.assertFalse(self.overlaps())
        quote = self.issue('piece-plus-setup')
        self.assertEqual(quote['amounts']['total_price'], '552.00')
        self.assertEqual(quote['amounts']['unit_price'], '55.20')
        self.save('technical', 'order-pending-review', {'operation_rates': ORDER_RATE})
        self.assertEqual(len(self.overlaps()), 1)
        self.save('technical', 'explicit-piece-basis', {
            'operation_rates': [{'operation': '折弯', 'value': '18', 'unit': 'CNY/piece'}],
        })
        self.assertFalse(self.overlaps())
        self.assertTrue(self.task['validation']['ready'])
        self.assertEqual(self.task['fields']['setup_fee']['value'], '200')

    def test_unknown_setup_is_not_zero_and_explicit_zero_is_valid(self):
        self.save('requirements', 'requirements', REQUIREMENTS)
        values = copy.deepcopy(TECHNICAL)
        values['operation_rates'] = copy.deepcopy(ORDER_RATE)
        values['setup_fee'] = None
        self.save('technical', 'unknown-setup', values)
        self.assertIsNone(self.task['fields']['setup_fee']['value'])
        self.assertEqual(self.task['fields']['setup_fee']['status'], 'missing')
        self.assertFalse(self.task['validation']['ready'])
        self.assertTrue(any(issue['field'] == 'setup_fee'
                            for issue in self.task['validation']['issues']))
        self.assertFalse(self.overlaps())
        with self.assertRaises(RecordError):
            self.issue('unknown-not-free')
        self.save('technical', 'explicit-no-setup-fee', {'setup_fee': '0'})
        self.assertEqual(self.task['fields']['setup_fee']['value'], '0')
        self.assertEqual(self.task['fields']['setup_fee']['status'], 'verified')
        self.assertTrue(self.task['validation']['ready'])
        self.assertFalse(self.overlaps())

    def test_reading_fee_guidance_changes_no_task_files(self):
        self.complete(by_order=True)
        task_path = self.app.records.task_path(self.task_id)
        before = self.files(task_path)
        revision = self.task['revision']
        for _ in range(2):
            task = self.app.task(self.task_id)
            self.assertEqual(len(self.overlaps(task)), 1)
            self.assertEqual(task['revision'], revision)
            self.assertEqual(self.app.tasks()[0]['id'], self.task_id)
        self.assertEqual(self.files(task_path), before)

    def test_manual_zero_replaces_selected_setup_price_without_false_attribution(self):
        self.complete()
        self.adopt_setup_price()
        self.assertEqual(self.task['price_selections']['setup_fee']['rate'], '200')
        adoption_path = self.app.records.task_path(self.task_id) / 'form-adopt-setup.json'
        adoption_bytes = adoption_path.read_bytes()
        self.save('technical', 'order-fee', {'operation_rates': ORDER_RATE})
        self.assertEqual(len(self.overlaps()), 1)
        self.save('technical', 'manual-zero', {'setup_fee': '0'})
        self.assertNotIn('setup_fee', self.task['price_selections'])
        self.assertEqual(self.task['fields']['setup_fee']['evidence']['reference'], 'FORM-manual-zero')
        self.assertFalse(self.overlaps())
        self.assertTrue(self.task['validation']['ready'])
        self.assertEqual(adoption_path.read_bytes(), adoption_bytes)
        source = json.loads((self.app.records.task_path(self.task_id) /
                             'form-manual-zero.json').read_text())
        self.assertEqual(source['adopted_prices'], {})

    def test_same_fee_resubmission_preserves_price_reference_and_cannot_bypass_review(self):
        self.complete()
        published = self.adopt_setup_price()
        selection = copy.deepcopy(self.task['price_selections']['setup_fee'])
        self.save('technical', 'same-fee-explain', {
            'setup_fee': '200', 'operation_rates': TECHNICAL['operation_rates'],
            'fee_inclusions': ['仅补充说明：折弯按件，准备费按单。'],
        })
        self.assertEqual(self.task['price_selections']['setup_fee'], selection)
        self.assertTrue(self.task['validation']['ready'])
        newer = self.app.catalog.new_version(
            published['entry_id'], dict(self.setup_price_payload, rate='250'))
        self.app.catalog.publish(newer['entry_id'], newer['version'])
        self.task = self.app.task(self.task_id)
        self.assertFalse(self.task['validation']['ready'])
        self.assertTrue(any(issue['code'] == 'price_review'
                            for issue in self.task['validation']['issues']))
        self.save('technical', 'same-fee-after-price-change', {
            'setup_fee': '200', 'fee_inclusions': ['仅补充说明，不重新采用价格。'],
        })
        self.assertEqual(self.task['price_selections']['setup_fee'], selection)
        self.assertFalse(self.task['validation']['ready'])
        self.assertTrue(any(issue['code'] == 'price_review'
                            for issue in self.task['validation']['issues']))
        with self.assertRaises(RecordError):
            self.issue('unchanged-fee-is-not-reconfirmation')

    def test_retry_after_record_commit_restores_snapshotted_price_decision(self):
        self.complete()
        self.adopt_setup_price()
        payload, form_path = self.interrupt_fee_save_after_record_commit()
        saved_bytes = form_path.read_bytes()
        self.assertEqual(json.loads(saved_bytes)['resulting_price_selections'], {})
        task = self.app.save_fields(self.task_id, 'technical', payload)
        self.assertEqual(task['revision'], payload['expected_revision'] + 1)
        self.assertNotIn('setup_fee', task['price_selections'])
        self.assertEqual(task['fields']['setup_fee']['value'], '0')
        self.assertEqual(task['fields']['setup_fee']['evidence']['reference'], 'FORM-interrupted-zero')
        self.assertTrue(task['validation']['ready'])
        self.assertEqual(form_path.read_bytes(), saved_bytes)
        events = self.app.records.load(self.task_id)['events']
        self.assertEqual(sum(event['id'] == payload['event_id'] for event in events), 1)

    def test_legacy_retry_reconciles_price_value_without_rewriting_source(self):
        self.complete()
        self.adopt_setup_price()
        payload, form_path = self.interrupt_fee_save_after_record_commit(legacy=True)
        saved_bytes = form_path.read_bytes()
        self.assertNotIn('resulting_price_selections', json.loads(saved_bytes))
        task = self.app.save_fields(self.task_id, 'technical', payload)
        self.assertEqual(task['revision'], payload['expected_revision'] + 1)
        self.assertNotIn('setup_fee', task['price_selections'])
        self.assertEqual(task['fields']['setup_fee']['value'], '0')
        self.assertTrue(task['validation']['ready'])
        self.assertEqual(form_path.read_bytes(), saved_bytes)

    def test_pending_retry_cannot_overwrite_later_successful_price_adoption(self):
        self.complete()
        published = self.adopt_setup_price()
        payload, _ = self.interrupt_fee_save_after_record_commit()
        self.task = self.app.task(self.task_id)
        self.save('technical', 'later-successful-adoption', {'setup_fee': '200'},
                  price_selections={'setup_fee': {'entry_id': published['entry_id'], 'version': 1}})
        self.assertTrue(self.task['validation']['ready'])
        self.assertEqual(self.task['fields']['setup_fee']['evidence']['reference'],
                         'FORM-later-successful-adoption')
        before = self.files(self.app.records.task_path(self.task_id))
        revision = self.task['revision']
        with self.assertRaises(WorkbenchError) as raised:
            self.app.save_fields(self.task_id, 'technical', payload)
        self.assertEqual(raised.exception.code, 'revision_conflict')
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.files(self.app.records.task_path(self.task_id)), before)
        task = self.app.task(self.task_id)
        self.assertEqual(task['revision'], revision)
        self.assertEqual(task['price_selections']['setup_fee']['rate'], '200')
        self.assertEqual(task['fields']['setup_fee']['evidence']['reference'],
                         'FORM-later-successful-adoption')


if __name__ == '__main__':
    unittest.main()
