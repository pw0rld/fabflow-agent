"""Independent quotation tests using a read-only live-demo seed copied per test.

No DSH or model calls. Every record/policy variant exists only in a TemporaryDirectory;
the saved live record and independent evaluation references are never changed.
"""
from __future__ import annotations
import copy
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from quotation import QuoteStore, RecordError

DEFAULT_SEED = Path('/Users/pw0rld/Documents/Codex/2026-10-13/haoyunlai-quote-demo-design/inquiry-prep-demo/evaluation/2026-10-19/LIVE-BRACKET-20261018-2c037c9e/task-final')
SEED = Path(os.environ.get('HAOYUNLAI_QUOTE_TEST_SEED', str(DEFAULT_SEED)))


class QuotationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='haoyunlai-quote-test-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'tasks'
        self.root.mkdir()
        self.assertTrue((SEED / 'record.json').is_file(), f'Test seed missing: {SEED}')
        seed_record = json.loads((SEED / 'record.json').read_text(encoding='utf-8'))
        self.task_id = seed_record['task_id']
        self.task = self.root / self.task_id
        shutil.copytree(SEED, self.task)
        self.record_path = self.task / 'record.json'
        self.original_bytes = self.record_path.read_bytes()
        self.store = QuoteStore(self.root)
        self.policy_path = self.base / 'pricing-policy.json'
        self.policy = {
            'schema_version': 1, 'pricing_method': 'cost_plus', 'rate': '0.20',
            'currency': 'CNY', 'tax_included': False, 'freight_included': False,
            'simulated': True, 'actor': '测试驱动（模拟定价规则）',
            'source_text': '模拟定价假设：成本加成20%；税费不含，运输不含；待审核。',
        }
        self.write_policy()
        self.bad_counter = 0

    def write_policy(self, **changes):
        self.policy.update(changes)
        self.policy_path.write_text(json.dumps(self.policy, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def record(self):
        return json.loads(self.record_path.read_text(encoding='utf-8'))

    def save_record(self, record):
        self.record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def set_field(self, name, value, unit=None, *, raw_value=None, raw_unit=None):
        """Prepare an explicitly synthetic variant in the temporary unit-test copy."""
        record = self.record()
        slot = record['fields'][name]
        candidate = copy.deepcopy(slot['effective'])
        candidate.update(value=value, unit=unit,
                         raw_value=value if raw_value is None else raw_value,
                         raw_unit=unit if raw_unit is None else raw_unit)
        slot['effective'] = candidate
        slot['history'][-1] = copy.deepcopy(candidate)
        self.save_record(record)

    def set_rates(self, rates):
        self.set_field('operation_rates', rates)
        self.set_field('process_route', [rate['operation'] for rate in rates])

    def simple_cost(self, amount, quantity=1):
        self.set_field('quantity', quantity, 'piece')
        self.set_field('material_usage_per_piece', '1', 'kg/piece')
        self.set_field('material_rate', amount, 'CNY/kg')
        self.set_rates([{'operation': '模拟工序', 'value': '0', 'unit': 'CNY/piece'}])
        self.set_field('setup_fee', '0', 'CNY/order')

    def issue(self, quote_id='Q-001', expected_revision=3):
        return self.store.issue(self.task_id, quote_id, self.policy_path, expected_revision)

    def reject(self, *, revision=3):
        self.bad_counter += 1
        quote_id = f'Q-bad-{self.bad_counter}'
        before = self.record_path.read_bytes()
        with self.assertRaises(RecordError):
            self.issue(quote_id, revision)
        self.assertEqual(before, self.record_path.read_bytes())
        self.assertFalse((self.task / 'quotes' / quote_id / 'quote.json').exists())

    def assert_amounts(self, quote, **expected):
        for name, value in expected.items():
            with self.subTest(amount=name):
                self.assertEqual(quote['amounts'][name], value)
        for name, value in quote['amounts'].items():
            with self.subTest(decimal_field=name):
                self.assertIsInstance(value, str)
                self.assertRegex(value, r'^-?\d+\.\d{2}$')
                if name != 'rounding_adjustment':
                    self.assertGreaterEqual(Decimal(value), 0)
        self.assertEqual(Decimal(quote['amounts']['total_price']),
                         Decimal(quote['amounts']['unit_price']) * quote['quantity'])
        self.assertEqual(Decimal(quote['amounts']['total_price']),
                         Decimal(quote['amounts']['total_cost']) +
                         Decimal(quote['amounts']['markup_amount']) +
                         Decimal(quote['amounts']['rounding_adjustment']))

    def test_cost_plus_twenty_percent_matches_independent_manual_arithmetic(self):
        # Material 0.400*20*10=80; process (12+18+6+4)*10=400; setup 200 once.
        quote = self.issue()
        self.assertEqual(quote['quantity'], 10)
        self.assertTrue(quote['simulated'])
        self.assertEqual(quote['status'], '模拟报价草稿，待审核')
        self.assert_amounts(quote, material_cost='80.00', process_cost='400.00',
            setup_cost='200.00', total_cost='680.00', unit_cost='68.00',
            unit_price='81.60', total_price='816.00', markup_amount='136.00',
            rounding_adjustment='0.00')

    def test_gross_margin_is_not_the_same_as_cost_plus(self):
        cost_plus = self.issue('Q-cost-plus')
        self.write_policy(pricing_method='gross_margin',
                          source_text='模拟规则：毛利率20%，税运不含；待审核。')
        margin = self.issue('Q-gross-margin')
        self.assert_amounts(margin, total_cost='680.00', unit_cost='68.00',
            unit_price='85.00', total_price='850.00', markup_amount='170.00',
            rounding_adjustment='0.00')
        self.assertNotEqual(cost_plus['amounts']['total_price'], margin['amounts']['total_price'])

    def test_setup_is_not_multiplied_when_quantity_increases(self):
        self.set_field('quantity', 20, 'piece')
        quote = self.issue()
        self.assert_amounts(quote, material_cost='160.00', process_cost='800.00',
            setup_cost='200.00', total_cost='1160.00', unit_cost='58.00',
            unit_price='69.60', total_price='1392.00', markup_amount='232.00',
            rounding_adjustment='0.00')

    def test_per_order_process_rate_is_charged_once(self):
        rates = copy.deepcopy(self.record()['fields']['operation_rates']['effective']['value'])
        rates[0]['unit'] = 'CNY/order'
        self.set_rates(rates)
        self.set_field('setup_fee', '0', 'CNY/order')
        quote = self.issue()
        self.assert_amounts(quote, material_cost='80.00', process_cost='292.00',
            setup_cost='0.00', total_cost='372.00', unit_cost='37.20',
            unit_price='44.64', total_price='446.40', markup_amount='74.40')

    def test_two_positive_preparation_items_require_inclusion_review(self):
        self.set_rates([{'operation': '工序准备费用', 'value': '200.00', 'unit': 'CNY/order'}])
        self.set_field('setup_fee', '200.00', 'CNY/order')
        self.reject()

    def test_high_precision_costs_are_added_before_display_rounding(self):
        self.simple_cost('0.005')
        self.set_rates([{'operation': '模拟工序', 'value': '0.005', 'unit': 'CNY/piece'}])
        self.write_policy(rate='0.00')
        quote = self.issue()
        # Two half-cent inputs total one cent; rounding each input first would wrongly total two cents.
        self.assert_amounts(quote, total_cost='0.01', unit_cost='0.01',
            unit_price='0.01', total_price='0.01', markup_amount='0.00')
        cost_document = (self.task / 'quotes/Q-001/cost_breakdown.md').read_text(encoding='utf-8')
        rows = {}
        for line in cost_document.splitlines():
            if line.startswith('|'):
                cells = [cell.strip() for cell in line.strip().strip('|').split('|')]
                if cells and cells[0] in ('材料', '模拟工序', '准备费用'):
                    rows[cells[0]] = cells[-1]
        self.assertEqual(rows['材料'], '0.005')
        self.assertEqual(rows['模拟工序'], '0.005')
        self.assertEqual(sum(Decimal(value) for value in rows.values()), Decimal('0.01'))
        self.assertIn('0.01', cost_document)

    def test_unit_price_uses_decimal_half_up_at_half_cent(self):
        self.simple_cost('0.02')
        self.write_policy(rate='0.25')
        quote = self.issue()
        # Target unit price .025 must become .03, whereas HALF_EVEN would produce .02.
        self.assert_amounts(quote, total_cost='0.02', unit_cost='0.02',
            unit_price='0.03', total_price='0.03', markup_amount='0.01',
            rounding_adjustment='0.00')

    def test_total_uses_rounded_unit_price_and_explains_cent_difference(self):
        self.simple_cost('0.01', quantity=3)
        quote = self.issue()
        # Raw target total=.036; quoted unit=.01, so 3 pieces total=.03.
        # Target markup=.006 displays .01; the remaining -.01 is explicitly a rounding adjustment.
        self.assert_amounts(quote, total_cost='0.03', unit_cost='0.01',
            unit_price='0.01', total_price='0.03', markup_amount='0.01',
            rounding_adjustment='-0.01')

    def test_original_record_is_byte_unchanged_and_all_quote_artifacts_exist(self):
        self.issue()
        self.assertEqual(self.original_bytes, self.record_path.read_bytes())
        folder = self.task / 'quotes' / 'Q-001'
        for name in ('quote.json', 'quote.md', 'cost_breakdown.md', 'record_snapshot.json', 'pricing_policy.json'):
            with self.subTest(artifact=name):
                self.assertTrue((folder / name).is_file())
                self.assertGreater((folder / name).stat().st_size, 0)
        self.assertEqual(self.original_bytes, (folder / 'record_snapshot.json').read_bytes())
        saved_policy = json.loads((folder / 'pricing_policy.json').read_text(encoding='utf-8'))
        self.assertFalse(saved_policy['tax_included'])
        self.assertFalse(saved_policy['freight_included'])
        paths = self.store.render(self.task_id, 'Q-001')
        self.assertIsInstance(paths, dict)
        self.assertTrue(paths)
        self.assertEqual(self.original_bytes, self.record_path.read_bytes())

    def test_same_id_record_and_policy_are_idempotent(self):
        first = self.issue()
        folder = self.task / 'quotes' / 'Q-001'
        before = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
        again = self.issue()
        self.assertEqual(first, again)
        self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()})

    def test_same_id_cannot_be_reused_for_changed_policy(self):
        self.issue()
        snapshot = (self.task / 'quotes/Q-001/quote.json').read_bytes()
        self.write_policy(rate='0.30')
        with self.assertRaises(RecordError):
            self.issue()
        self.assertEqual(snapshot, (self.task / 'quotes/Q-001/quote.json').read_bytes())

    def test_changed_record_keeps_old_draft_historical_and_requires_new_quote_id(self):
        self.issue()
        snapshot = (self.task / 'quotes/Q-001/quote.json').read_bytes()
        self.set_field('quantity', 20, 'piece')
        record = self.record(); record['data_revision'] = 4; self.save_record(record)
        historical = self.store.load(self.task_id, 'Q-001')
        self.assertFalse(historical['source_current'])
        self.assertEqual(historical['quantity'], 10)
        with self.assertRaises(RecordError):
            self.issue('Q-001', expected_revision=4)
        new_quote = self.issue('Q-002', expected_revision=4)
        self.assertEqual(new_quote['quantity'], 20)
        self.assertEqual(new_quote['amounts']['total_price'], '1392.00')
        self.assertEqual(snapshot, (self.task / 'quotes/Q-001/quote.json').read_bytes())

    def test_load_marks_unchanged_source_current(self):
        self.issue()
        loaded = self.store.load(self.task_id, 'Q-001')
        self.assertTrue(loaded['source_current'])
        self.assertEqual(loaded['amounts']['total_price'], '816.00')

    def test_policy_cannot_remove_simulated_label_from_simulated_record(self):
        self.write_policy(simulated=False)
        quote = self.issue()
        self.assertTrue(quote['simulated'])
        self.assertEqual(quote['status'], '模拟报价草稿，待审核')

    def test_stale_revision_is_rejected(self):
        self.reject(revision=2)

    def test_not_ready_or_unverified_record_is_rejected(self):
        record = self.record()
        record['fields']['quantity']['status'] = 'pending_review'
        self.save_record(record)
        self.reject()

    def test_explicit_open_question_blocks_quotation(self):
        record = self.record()
        record['questions'][0]['status'] = 'open'
        self.save_record(record)
        self.reject()

    def test_source_snapshot_tampering_blocks_new_quote(self):
        record = self.record()
        first = next(iter(record['sources'].values()))
        (self.task / first['path']).write_bytes(b'tampered test snapshot')
        self.reject()

    def test_missing_effective_required_field_is_rejected(self):
        record = self.record()
        record['fields']['material_rate']['effective'] = None
        self.save_record(record)
        self.reject()

    def test_invalid_quantity_is_rejected(self):
        baseline = self.record_path.read_bytes()
        for value in (0, -1, True, 1.5):
            with self.subTest(quantity=value):
                self.record_path.write_bytes(baseline)
                self.set_field('quantity', value, 'piece')
                self.reject()

    def test_invalid_raw_cost_numbers_are_rejected(self):
        baseline = self.record_path.read_bytes()
        for field, value, unit in (
            ('material_usage_per_piece', 'NaN', 'kg/piece'),
            ('material_usage_per_piece', '-0.4', 'kg/piece'),
            ('material_rate', 'NaN', 'CNY/kg'),
            ('material_rate', '-20', 'CNY/kg'),
            ('setup_fee', '-200', 'CNY/order'),
        ):
            with self.subTest(field=field, value=value):
                self.record_path.write_bytes(baseline)
                self.set_field(field, value, unit)
                self.reject()

    def test_operation_rates_need_measurements_for_non_counted_bases(self):
        baseline = self.record_path.read_bytes()
        for unit in ('CNY/hour', 'CNY/m', 'CNY/hole', 'CNY/bend'):
            with self.subTest(unit=unit):
                self.record_path.write_bytes(baseline)
                self.set_rates([{'operation': '需要独立计量的模拟工序', 'value': '12', 'unit': unit}])
                self.reject()

    def test_route_and_rate_must_match_one_to_one_without_duplicates(self):
        baseline = self.record_path.read_bytes()
        variants = [
            (['切割/制孔', '未提供费率工序'], [{'operation': '切割/制孔', 'value': '12', 'unit': 'CNY/piece'}]),
            (['切割/制孔'], [{'operation': '其他工序', 'value': '12', 'unit': 'CNY/piece'}]),
            (['切割/制孔'], [{'operation': '切割/制孔', 'value': '12', 'unit': 'CNY/piece'}, {'operation': '切割/制孔', 'value': '13', 'unit': 'CNY/piece'}]),
            (['切割/制孔', '切割/制孔'], [{'operation': '切割/制孔', 'value': '12', 'unit': 'CNY/piece'}]),
        ]
        for route, rates in variants:
            with self.subTest(route=route, rates=rates):
                self.record_path.write_bytes(baseline)
                self.set_field('process_route', route)
                self.set_field('operation_rates', rates)
                self.reject()

    def test_operation_nan_and_negative_rate_are_rejected(self):
        baseline = self.record_path.read_bytes()
        for value in ('NaN', '-1'):
            with self.subTest(value=value):
                self.record_path.write_bytes(baseline)
                self.set_rates([{'operation': '模拟工序', 'value': value, 'unit': 'CNY/piece'}])
                self.reject()

    def test_required_policy_fields_cannot_be_omitted(self):
        policy = copy.deepcopy(self.policy)
        for name in policy:
            with self.subTest(missing_policy_field=name):
                malformed = {key: value for key, value in policy.items() if key != name}
                self.policy_path.write_text(json.dumps(malformed, ensure_ascii=False), encoding='utf-8')
                self.reject()

    def test_invalid_or_unsupported_policy_is_rejected(self):
        baseline = copy.deepcopy(self.policy)
        variants = [
            {'rate': 'NaN'}, {'rate': 'Infinity'}, {'rate': '-0.01'}, {'rate': 0.2},
            {'schema_version': True}, {'currency': 'USD'}, {'pricing_method': 'unknown'},
            {'tax_included': True}, {'freight_included': True}, {'simulated': 'true'},
            {'actor': '   '}, {'source_text': ''}, {'unrecognized_shipping_fee': '10.00'},
        ]
        for changes in variants:
            with self.subTest(policy_changes=changes):
                self.policy = copy.deepcopy(baseline)
                self.write_policy(**changes)
                self.reject()

    def test_gross_margin_at_or_above_one_is_rejected(self):
        for rate in ('1', '1.20'):
            with self.subTest(rate=rate):
                self.write_policy(pricing_method='gross_margin', rate=rate)
                self.reject()

    def test_quote_id_cannot_escape_quote_directory(self):
        for quote_id in ('../escape', '/tmp/escape', 'Q/subdir'):
            with self.subTest(quote_id=quote_id), self.assertRaises(RecordError):
                self.issue(quote_id)
        self.assertEqual(self.original_bytes, self.record_path.read_bytes())

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(PROJECT / 'src/quotation.py'),
            '--root', str(self.root), *arguments], text=True, capture_output=True,
            cwd=PROJECT, timeout=10)

    def test_cli_issue_show_render_and_idempotent_retry(self):
        arguments = ['issue', '--task-id', self.task_id, '--quote-id', 'Q-cli',
                     '--policy', str(self.policy_path), '--expected-revision', '3']
        first = self.cli(*arguments)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)['amounts']['total_price'], '816.00')
        retry = self.cli(*arguments)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(first.stdout), json.loads(retry.stdout))
        show = self.cli('show', '--task-id', self.task_id, '--quote-id', 'Q-cli')
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertTrue(json.loads(show.stdout)['source_current'])
        render = self.cli('render', '--task-id', self.task_id, '--quote-id', 'Q-cli')
        self.assertEqual(render.returncode, 0, render.stderr)
        self.assertTrue(json.loads(render.stdout))
        self.assertEqual(self.original_bytes, self.record_path.read_bytes())

    def test_cli_invalid_policy_returns_structured_error_and_no_quote(self):
        self.write_policy(rate='NaN')
        result = self.cli('issue', '--task-id', self.task_id, '--quote-id', 'Q-cli-invalid',
                          '--policy', str(self.policy_path), '--expected-revision', '3')
        self.assertEqual(result.returncode, 2)
        self.assertIn('error', json.loads(result.stderr))
        self.assertFalse((self.task / 'quotes/Q-cli-invalid/quote.json').exists())


if __name__ == '__main__':
    unittest.main()
