"""Behavior tests: real temporary snapshots, deterministic fixtures, no model calls."""
from __future__ import annotations
import base64
import copy
from concurrent.futures import ThreadPoolExecutor
import threading
import json
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from prep_record import RecordStore, RecordError


class RecordBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='prep-record-test-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'input'
        self.source.mkdir()
        # Valid 1x1 PNG; the module stores it without interpreting drawing geometry.
        self.drawing = self.source / 'drawing.png'
        self.drawing.write_bytes(base64.b64decode(
            'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6JbkAAAAASUVORK5CYII='))
        self.customer = self.source / 'customer.txt'
        self.customer.write_text('模拟客户补充：10件，Q235B，不做表面处理。', encoding='utf-8')
        self.technical = self.source / 'technical.txt'
        self.technical.write_text('模拟技术补录：用量0.400kg/件，费率仅作原始输入。', encoding='utf-8')
        self.store = RecordStore(self.base / 'records')
        self.init('A')

    def init(self, task_id):
        return self.store.init(task_id, {
            'inquiry': '请先整理支架资料，随后准备报价，数量待补。',
            'simulated': True,
            'files': [{'id': 'drawing-1', 'path': str(self.drawing)}],
            'versions': {'dsh': 'fixture-not-running', 'model': 'no-model-called', 'skill': 'test-v1'},
        })

    def revision(self, task_id='A'):
        return self.store.load(task_id)['data_revision']

    def field(self, name, value, *, kind='drawing', reference='drawing-1',
              unit=None, raw_value=None, raw_unit=None, status='pending_review',
              reason=None):
        result = {
            'name': name, 'value': value, 'unit': unit,
            'raw_value': value if raw_value is None else raw_value,
            'raw_unit': unit if raw_unit is None else raw_unit,
            'status': status,
            'evidence': {'source_type': kind, 'reference': reference,
                         'actor': None if kind == 'drawing' else 'customer' if kind == 'customer_reply' else 'technician',
                         'simulated': True},
        }
        if kind == 'drawing':
            result['evidence'].update(page=1, region='title-block')
        if reason is not None:
            result['reason'] = reason
        return result

    def payload(self, fields, kind=None, reference=None, **extra):
        payload = {'fields': fields, 'questions': [], 'notes': [], **extra}
        if kind:
            path = self.customer if kind == 'customer_reply' else self.technical
            payload['sources'] = [{'id': reference, 'path': str(path), 'kind': kind,
                                   'actor': 'customer' if kind == 'customer_reply' else 'technician',
                                   'simulated': True}]
        return payload

    def apply(self, operation, payload, *, task_id='A', revision=None, event_id=None):
        if revision is None:
            revision = self.revision(task_id)
        if event_id is None:
            event_id = f'{operation}-{revision}'
        return self.store.apply(task_id, operation, payload, revision, event_id)

    def customer_field(self, name, value, unit=None, reference='customer-1', **kwargs):
        return self.field(name, value, kind='customer_reply', reference=reference,
                          unit=unit, status='verified', **kwargs)

    def technical_field(self, name, value, unit=None, reference='technical-1', **kwargs):
        return self.field(name, value, kind='technical_input', reference=reference,
                          unit=unit, status='verified', **kwargs)

    def assert_rejected_without_mutation(self, operation, payload, **kwargs):
        before = self.store.load(kwargs.get('task_id', 'A'))
        with self.assertRaises(RecordError):
            self.apply(operation, payload, **kwargs)
        self.assertEqual(before, self.store.load(kwargs.get('task_id', 'A')))

    def test_initial_missing_quantity_is_null_and_attachment_count_does_not_fill_it(self):
        record = self.store.load('A')
        self.assertEqual(record['data_revision'], 0)
        self.assertEqual(record['fields']['quantity']['status'], 'missing')
        self.assertIsNone(record['fields']['quantity']['effective'])
        self.assertEqual(record['fields']['quantity']['history'], [])
        self.assertFalse(self.store.validate('A')['ready'])
        self.assertEqual(record['versions']['model'], 'no-model-called')

    def test_task_id_path_traversal_is_rejected(self):
        for task_id in ('../escape', '/tmp/escape', 'A/B', '..'):
            with self.subTest(task_id=task_id), self.assertRaises(RecordError):
                self.init(task_id)

    def test_reinitialization_never_overwrites_a_record(self):
        before = self.store.load('A')
        with self.assertRaises(RecordError):
            self.init('A')
        self.assertEqual(before, self.store.load('A'))

    def test_source_is_snapshotted_and_hashes_do_not_depend_on_later_source_changes(self):
        record = self.store.load('A')
        source = record['sources']['drawing-1']
        self.assertEqual(len(source['sha256']), 64)
        self.assertFalse(Path(source['path']).is_absolute())
        self.drawing.write_bytes(b'changed after intake')
        self.assertEqual(record['sources'], self.store.load('A')['sources'])

    def test_clear_inch_conversion_preserves_raw_and_does_not_round_to_stock_thickness(self):
        field = self.field('thickness', '3.175', unit='mm', raw_value='0.125', raw_unit='in')
        record = self.apply('record_reading', self.payload([field]))
        actual = record['fields']['thickness']['effective']
        self.assertEqual(actual['value'], '3.175')
        self.assertEqual(actual['raw_value'], '0.125')
        self.assertEqual(actual['raw_unit'], 'in')
        self.assertEqual(actual['status'], 'pending_review')
        self.assertEqual(actual['evidence']['page'], 1)

    def test_wrong_normalized_value_is_rejected(self):
        field = self.field('thickness', '3', unit='mm', raw_value='0.125', raw_unit='in')
        self.assert_rejected_without_mutation('record_reading', self.payload([field]))

    def test_radius_label_preserves_raw_with_exact_conversion_and_pending_status(self):
        field = self.field('inner_bend_radius', '2.032', unit='mm', raw_value='R .08', raw_unit='inch')
        record = self.apply('record_reading', self.payload([field]))
        actual = record['fields']['inner_bend_radius']['effective']
        self.assertEqual(actual['raw_value'], 'R .08')
        self.assertEqual(actual['raw_unit'], 'inch')
        self.assertEqual(actual['value'], '2.032')
        self.assertEqual(actual['normalization'], {'from': 'inch', 'to': 'mm', 'factor': '25.4'})
        self.assertEqual(actual['status'], 'pending_review')
        self.assertEqual(self.store.load('A')['fields']['inner_bend_radius']['effective'], actual)

    def test_radius_label_cannot_relax_other_fields_or_rounded_conversion(self):
        cases = [('inner_bend_radius', 'R .08', '2.03'),
                 ('inner_bend_radius', 'R .08 in', '2.032'),
                 ('inner_bend_radius', 'R .08 ± .01', '2.032'),
                 ('inner_bend_radius', 'R 1/8', '3.175'),
                 ('inner_bend_radius', 'R NaN', '2.032'),
                 ('inner_bend_radius', 'R 0', '0'),
                 ('inner_bend_radius', 'R .08', 'NaN'),
                 ('thickness', 'R .08', '2.032')]
        for name, raw, value in cases:
            with self.subTest(name=name, raw=raw, value=value):
                field = self.field(name, value, unit='mm', raw_value=raw, raw_unit='inch')
                self.assert_rejected_without_mutation('record_reading', self.payload([field]))

    def test_thickness_label_preserves_raw_and_exact_conversion_pending_review(self):
        cases = [('T=1.5', 'mm', '1.5', '1'),
                 ('\tt \t= .08\t', 'inch', '2.032', '25.4')]
        for index, (raw, raw_unit, value, factor) in enumerate(cases):
            with self.subTest(raw=raw):
                task_id = f'THICKNESS-{index}'
                self.init(task_id)
                field = self.field('thickness', value, unit='mm', raw_value=raw, raw_unit=raw_unit)
                record = self.apply('record_reading', self.payload([field]), task_id=task_id)
                actual = record['fields']['thickness']['effective']
                self.assertEqual(actual['raw_value'], raw)
                self.assertEqual(actual['raw_unit'], raw_unit)
                self.assertEqual(actual['value'], value)
                self.assertEqual(actual['normalization'], {'from': raw_unit, 'to': 'mm', 'factor': factor})
                self.assertEqual(actual['status'], 'pending_review')
                self.assertEqual(self.store.load(task_id)['fields']['thickness']['effective'], actual)

    def test_thickness_label_rejects_ambiguous_text_and_keeps_numeric_unit_checks(self):
        malformed = ['T 1.5', 'T=1.5 mm', 'T=1.5 ± .1', 'T=1.5/2', 'T=1.5,2',
                     'T=1.5=1.5', 'T=3/2', 'T=1.5+0', 'T=1.5 NOTE', 'T=NaN',
                     'T=Infinity', 'T=-1.5', 'T=+1.5', 'T=1.5e0', 'T=\n1.5', 'T=1.5\n']
        fields = [self.field('thickness', '1.5', unit='mm', raw_value=raw) for raw in malformed]
        fields.extend([
            self.field('thickness', '0', unit='mm', raw_value='T=0'),
            self.field('thickness', '2.03', unit='mm', raw_value='t=.08', raw_unit='in'),
            self.field('thickness', '1.5', unit='mm', raw_value='T=1.5', raw_unit='cm'),
            self.field('thickness', 'NaN', unit='mm', raw_value='T=1.5'),
            self.field('thickness', True, unit='mm', raw_value='T=1.5'),
            self.field('inner_bend_radius', '1.5', unit='mm', raw_value='T=1.5'),
            self.field('material_usage_per_piece', '1.5', unit='kg/piece', raw_value='T=1.5'),
        ])
        for field in fields:
            with self.subTest(field=field):
                self.assert_rejected_without_mutation('record_reading', self.payload([field]))

    def test_ai_reading_cannot_claim_human_verification(self):
        field = self.field('material_family', 'Plain Carbon Steel', status='verified')
        self.assert_rejected_without_mutation('record_reading', self.payload([field]))

    def test_ai_reading_cannot_impersonate_a_customer_reply(self):
        field = self.customer_field('material_grade', 'Q235B')
        payload = self.payload([field], 'customer_reply', 'customer-1')
        self.assert_rejected_without_mutation('record_reading', payload)

    def test_unknown_evidence_reference_is_rejected(self):
        field = self.field('material_family', 'Plain Carbon Steel', reference='not-attached')
        self.assert_rejected_without_mutation('record_reading', self.payload([field]))

    def test_missing_value_stays_null_not_zero(self):
        field = self.field('quantity', None, unit='piece', status='missing')
        record = self.apply('record_reading', self.payload([field]))
        self.assertEqual(record['fields']['quantity']['status'], 'missing')
        effective = record['fields']['quantity']['effective']
        self.assertTrue(effective is None or effective['value'] is None)

    def test_quantity_must_be_a_positive_integer(self):
        for value in (0, -1, 1.5, True):
            with self.subTest(value=value):
                payload = self.payload([self.customer_field('quantity', value, 'piece')],
                                       'customer_reply', 'customer-1')
                self.assert_rejected_without_mutation('apply_customer_reply', payload)

    def test_staged_customer_reply_keeps_source_and_verification(self):
        field = self.customer_field('quantity', 10, 'piece')
        record = self.apply('apply_customer_reply', self.payload([field], 'customer_reply', 'customer-1'))
        actual = record['fields']['quantity']['effective']
        self.assertEqual(actual['value'], 10)
        self.assertEqual(actual['status'], 'verified')
        self.assertEqual(actual['evidence']['reference'], 'customer-1')
        self.assertTrue(record['simulated'])
        self.assertEqual(record['sources']['customer-1']['kind'], 'customer_reply')

    def test_human_source_must_have_a_real_file(self):
        field = self.customer_field('quantity', 10, 'piece')
        payload = self.payload([field], 'customer_reply', 'customer-1')
        payload['sources'][0]['path'] = str(self.base / 'not-present.txt')
        self.assert_rejected_without_mutation('apply_customer_reply', payload)

    def test_human_evidence_actor_and_simulated_flag_must_match_snapshot(self):
        for attribute, invalid in (('actor', 'imaginary-customer'), ('simulated', False)):
            with self.subTest(attribute=attribute):
                field = self.customer_field('quantity', 10, 'piece')
                field['evidence'][attribute] = invalid
                self.assert_rejected_without_mutation('apply_customer_reply',
                    self.payload([field], 'customer_reply', 'customer-1'))

    def test_raw_rates_preserve_basis_and_no_derived_cost_is_created(self):
        rates = [{'operation': '切割/制孔', 'value': '12.00', 'unit': 'CNY/piece'},
                 {'operation': '折弯', 'value': '18.00', 'unit': 'CNY/piece'}]
        fields = [self.technical_field('material_usage_per_piece', '0.400', 'kg/piece'),
                  self.technical_field('material_rate', '20.00', 'CNY/kg'),
                  self.technical_field('operation_rates', rates),
                  self.technical_field('setup_fee', '200.00', 'CNY/order')]
        record = self.apply('apply_technical_input', self.payload(fields, 'technical_input', 'technical-1'))
        actual = record['fields']
        self.assertEqual(actual['material_rate']['effective']['unit'], 'CNY/kg')
        self.assertEqual(actual['setup_fee']['effective']['unit'], 'CNY/order')
        self.assertEqual(actual['operation_rates']['effective']['value'][0]['unit'], 'CNY/piece')
        for prohibited in ('cost_total', 'total_cost', 'quote_total', 'subtotal', 'markup'):
            self.assertNotIn(prohibited, actual)

    def test_rates_without_a_unit_are_rejected(self):
        field = self.technical_field('operation_rates', [{'operation': '切割', 'value': '12.00'}])
        self.assert_rejected_without_mutation('apply_technical_input',
            self.payload([field], 'technical_input', 'technical-1'))

    def test_unknown_derived_price_field_is_rejected(self):
        for name in ('quote_total', 'cost_total', 'sale_unit_price', 'markup'):
            with self.subTest(name=name):
                field = self.technical_field(name, '816', 'CNY/order')
                self.assert_rejected_without_mutation('apply_technical_input',
                    self.payload([field], 'technical_input', 'technical-1'))

    def test_customer_can_say_quote_without_keyword_blocking(self):
        field = self.customer_field('product_name', '用于后续报价的支架')
        record = self.apply('apply_customer_reply', self.payload([field], 'customer_reply', 'customer-1',
                             notes=['客户要求先整理资料，之后准备报价。']))
        self.assertEqual(record['fields']['product_name']['effective']['value'], '用于后续报价的支架')

    def test_stale_revision_rejected_but_other_task_is_unchanged(self):
        self.init('B')
        other = self.store.load('B')
        payload = self.payload([self.field('material_family', 'Plain Carbon Steel')])
        self.apply('record_reading', payload, revision=0, event_id='first')
        self.assert_rejected_without_mutation('record_reading', payload, revision=0, event_id='second')
        self.assertEqual(other, self.store.load('B'))

    def test_retry_same_event_is_idempotent_even_with_old_expected_revision(self):
        payload = self.payload([self.field('material_family', 'Plain Carbon Steel')])
        first = self.apply('record_reading', payload, revision=0, event_id='retry-once')
        again = self.apply('record_reading', payload, revision=0, event_id='retry-once')
        self.assertEqual(first, again)
        self.assertEqual(self.revision(), 1)
        self.assertEqual(len(again['fields']['material_family']['history']), 1)

    def test_reusing_event_id_with_changed_payload_fails(self):
        payload = self.payload([self.field('material_family', 'Plain Carbon Steel')])
        self.apply('record_reading', payload, revision=0, event_id='retry-once')
        different = copy.deepcopy(payload)
        different['fields'][0]['value'] = 'Aluminium'
        self.assert_rejected_without_mutation('record_reading', different, revision=0, event_id='retry-once')

    def test_conflict_does_not_silently_overwrite_verified_quantity(self):
        self.apply('apply_customer_reply', self.payload([self.customer_field('quantity', 10, 'piece')],
                   'customer_reply', 'customer-1'))
        changed = self.customer_field('quantity', 20, 'piece', reference='customer-2')
        record = self.apply('apply_customer_reply', self.payload([changed], 'customer_reply', 'customer-2'))
        self.assertEqual(record['fields']['quantity']['status'], 'conflict')
        self.assertEqual(record['fields']['quantity']['effective']['value'], 10)
        self.assertEqual(len(record['fields']['quantity']['history']), 2)
        self.assertFalse(self.store.validate('A')['ready'])

    def test_reasoned_human_resolution_records_audit_history(self):
        self.apply('apply_customer_reply', self.payload([self.customer_field('quantity', 10, 'piece')],
                   'customer_reply', 'customer-1'))
        revised = self.customer_field('quantity', 20, 'piece', reference='customer-2',
                                      reason='客户明确更正数量，采用第二次回复。')
        record = self.apply('apply_customer_reply', self.payload([revised], 'customer_reply', 'customer-2'))
        self.assertEqual(record['fields']['quantity']['effective']['value'], 20)
        self.assertEqual(record['fields']['quantity']['status'], 'verified')
        self.assertEqual(len(record['fields']['quantity']['history']), 2)
        self.assertTrue(record['fields']['quantity']['effective']['reason'])

    def test_persist_reload_and_render_do_not_advance_revision(self):
        self.apply('record_reading', self.payload([self.field('thickness', '3.175', unit='mm',
                   raw_value='0.125', raw_unit='in')]))
        before = self.store.load('A')
        reopened = RecordStore(self.base / 'records')
        self.assertEqual(before, reopened.load('A'))
        output = reopened.render('A')
        self.assertTrue(Path(output['record_json']).is_file())
        self.assertTrue(Path(output['record_markdown']).is_file())
        markdown = Path(output['record_markdown']).read_text(encoding='utf-8')
        self.assertIn('3.175', markdown)
        self.assertIn('0.125', markdown)
        self.assertEqual(before, reopened.load('A'))

    def test_concurrent_writers_on_same_revision_commit_exactly_once(self):
        barrier = threading.Barrier(2)
        payload = self.payload([self.field('material_family', 'Plain Carbon Steel')])
        def writer(index):
            store = RecordStore(self.base / 'records')
            barrier.wait(timeout=5)
            try:
                store.apply('A', 'record_reading', payload, 0, f'concurrent-{index}')
                return 'committed'
            except RecordError:
                return 'rejected'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(writer, (1, 2)))
        self.assertEqual(sorted(results), ['committed', 'rejected'])
        self.assertEqual(self.revision(), 1)
        self.assertEqual(len(self.store.load('A')['events']), 1)

    def test_snapshot_tampering_is_detected_before_read_and_render(self):
        record = self.store.load('A')
        snapshot = self.base / 'records' / 'A' / record['sources']['drawing-1']['path']
        snapshot.write_bytes(b'tampered snapshot')
        with self.assertRaises(RecordError):
            self.store.load('A')
        with self.assertRaises(RecordError):
            self.store.render('A')

    def test_entire_invalid_event_leaves_no_partially_adopted_fields(self):
        good = self.customer_field('quantity', 10, 'piece')
        bad = self.customer_field('material_grade', 123)
        self.assert_rejected_without_mutation('apply_customer_reply',
            self.payload([good, bad], 'customer_reply', 'customer-1'))
        self.assertIsNone(self.store.load('A')['fields']['quantity']['effective'])
        self.assertEqual(self.revision(), 0)

    def test_one_time_fee_cannot_be_mistaken_for_a_per_piece_rate(self):
        field = self.technical_field('setup_fee', '200.00', 'CNY/piece')
        self.assert_rejected_without_mutation('apply_technical_input',
            self.payload([field], 'technical_input', 'technical-1'))

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(PROJECT / 'src/prep_record.py'),
            '--root', str(self.base / 'records'), *arguments],
            text=True, capture_output=True, cwd=PROJECT, timeout=10)

    def test_cli_show_and_validation_return_parseable_json_without_mutation(self):
        show = self.cli('show', '--task-id', 'A')
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertEqual(json.loads(show.stdout)['task_id'], 'A')
        validation = self.cli('validate', '--task-id', 'A')
        self.assertEqual(validation.returncode, 0, validation.stderr)
        self.assertFalse(json.loads(validation.stdout)['ready'])
        self.assertEqual(self.revision(), 0)

    def test_cli_mutation_requires_file_and_revision_and_returns_structured_error(self):
        before = self.store.load('A')
        missing = self.cli('record_reading', '--task-id', 'A')
        self.assertEqual(missing.returncode, 2)
        self.assertIn('error', json.loads(missing.stderr))
        payload = self.base / 'reading.json'
        payload.write_text(json.dumps(self.payload([self.field('material_family', 'Plain Carbon Steel')])))
        no_revision = self.cli('record_reading', '--task-id', 'A', '--input', str(payload), '--event-id', 'cli-event')
        self.assertEqual(no_revision.returncode, 2)
        self.assertIn('error', json.loads(no_revision.stderr))
        self.assertEqual(before, self.store.load('A'))

    def test_cli_commit_and_retry_match_python_api_contract(self):
        payload = self.base / 'reading.json'
        payload.write_text(json.dumps(self.payload([self.field('material_family', 'Plain Carbon Steel')])))
        args = ['record_reading', '--task-id', 'A', '--input', str(payload),
                '--expected-revision', '0', '--event-id', 'cli-event']
        first, retry = self.cli(*args), self.cli(*args)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(first.stdout), json.loads(retry.stdout))
        self.assertEqual(self.revision(), 1)

    def test_operator_review_requires_an_explicit_review_reason(self):
        field = self.field('product_name', '支架', kind='operator_review',
                           reference='review-1', status='verified')
        field['evidence']['actor'] = 'reviewer'
        payload = self.payload([field])
        payload['sources'] = [{'id': 'review-1', 'path': str(self.customer),
            'kind': 'operator_review', 'actor': 'reviewer', 'simulated': True}]
        self.assert_rejected_without_mutation('apply_customer_reply', payload)

    def test_same_ai_rereading_does_not_downgrade_a_human_verified_value(self):
        self.apply('apply_customer_reply', self.payload([
            self.customer_field('material_family', 'Plain Carbon Steel')],
            'customer_reply', 'customer-1'))
        record = self.apply('record_reading', self.payload([
            self.field('material_family', 'Plain Carbon Steel')]))
        slot = record['fields']['material_family']
        self.assertEqual(slot['status'], 'verified')
        self.assertEqual(slot['effective']['evidence']['source_type'], 'customer_reply')
        self.assertEqual(len(slot['history']), 2)

    def test_unreadable_or_missing_rereading_cannot_clear_a_verified_value(self):
        self.apply('apply_customer_reply', self.payload([
            self.customer_field('material_family', 'Plain Carbon Steel')],
            'customer_reply', 'customer-1'))
        for status in ('missing', 'unreadable'):
            with self.subTest(status=status):
                record = self.apply('record_reading', self.payload([
                    self.field('material_family', None, status=status, reason='本次读图未能取得值')]))
                slot = record['fields']['material_family']
                self.assertEqual(slot['status'], 'verified')
                self.assertEqual(slot['effective']['value'], 'Plain Carbon Steel')
                self.assertEqual(slot['effective']['evidence']['source_type'], 'customer_reply')
                self.assertEqual(slot['history'][-1]['status'], status)

    def test_ai_reason_cannot_replace_a_conflicting_human_verified_value(self):
        self.apply('apply_customer_reply', self.payload([
            self.customer_field('material_family', 'Plain Carbon Steel')],
            'customer_reply', 'customer-1'))
        record = self.apply('record_reading', self.payload([
            self.field('material_family', 'Aluminium', reason='模型重新读图给出不同候选')]))
        slot = record['fields']['material_family']
        self.assertEqual(slot['status'], 'conflict')
        self.assertEqual(slot['effective']['value'], 'Plain Carbon Steel')
        self.assertEqual(slot['effective']['status'], 'verified')
        self.assertEqual(len(slot['history']), 2)

    def test_existing_question_id_cannot_be_reassigned_to_a_different_field(self):
        question = {'id': 'qty-question', 'field': 'quantity', 'audience': 'customer',
                    'text': '本次数量多少？', 'status': 'open'}
        self.apply('record_reading', self.payload([], questions=[question]))
        changed = {**question, 'field': 'material_grade', 'text': '指定材料牌号？'}
        self.assert_rejected_without_mutation('record_reading', self.payload([], questions=[changed]))

    def question(self):
        return {'id': 'qty-recheck', 'field': 'quantity', 'audience': 'customer',
                'text': '已记录10件，请再次核对是否仍是该数量？', 'status': 'open'}

    def test_new_open_question_is_visible_even_when_field_was_previously_verified(self):
        self.complete_raw_input()
        self.assertTrue(self.store.validate('A')['ready'])
        self.apply('record_reading', self.payload([], questions=[self.question()]))
        result = self.store.validate('A')
        self.assertFalse(result['ready'])
        self.assertEqual(result['open_questions'], [self.question()])
        self.assertEqual(self.store.load('A')['fields']['quantity']['status'], 'verified')

    def test_real_staged_field_reply_resolves_earlier_missing_field_question(self):
        self.apply('record_reading', self.payload([], questions=[self.question()]))
        record = self.apply('apply_customer_reply', self.payload([
            self.customer_field('quantity', 10, 'piece')], 'customer_reply', 'customer-1'))
        self.assertEqual(record['questions'][0]['status'], 'resolved')
        self.assertEqual(self.store.validate('A')['open_questions'], [])

    def test_question_newly_opened_in_same_reply_remains_open(self):
        self.complete_raw_input()
        record = self.apply('apply_customer_reply', self.payload([
            self.customer_field('quantity', 10, 'piece', reference='customer-2')],
            'customer_reply', 'customer-2', questions=[self.question()]))
        self.assertEqual(record['questions'][0]['status'], 'open')
        self.assertFalse(self.store.validate('A')['ready'])

    def test_ai_cannot_resolve_new_question_using_only_old_human_confirmation(self):
        self.complete_raw_input()
        self.apply('record_reading', self.payload([], questions=[self.question()]))
        resolved = {**self.question(), 'status': 'resolved'}
        self.assert_rejected_without_mutation('record_reading', self.payload([], questions=[resolved]))
        self.assertFalse(self.store.validate('A')['ready'])

    def complete_raw_input(self):
        customer_fields = [
            self.customer_field('product_name', '支架'),
            self.customer_field('quantity', 10, 'piece'),
            self.customer_field('material_family', 'Plain Carbon Steel'),
            self.customer_field('material_grade', 'Q235B'),
            self.customer_field('thickness', '3.175', 'mm', raw_value='0.125', raw_unit='in'),
            self.customer_field('surface_treatment', '本次模拟订单不做'),
        ]
        self.apply('apply_customer_reply', self.payload(customer_fields, 'customer_reply', 'customer-1'))
        technical_fields = [
            self.technical_field('material_usage_per_piece', '0.400', 'kg/piece'),
            self.technical_field('material_rate', '20.00', 'CNY/kg'),
            self.technical_field('process_route', ['切割/制孔', '折弯与加强筋成形', '去毛刺']),
            self.technical_field('operation_rates', [{'operation': '切割/制孔', 'value': '12.00', 'unit': 'CNY/piece'}]),
            self.technical_field('setup_fee', '200.00', 'CNY/order'),
            self.technical_field('fee_inclusions', ['用量含损耗；费率按件，准备费按单。']),
            self.technical_field('engineering_open_items', ['实际工艺与工装待工厂验证']),
        ]
        return self.apply('apply_technical_input', self.payload(technical_fields, 'technical_input', 'technical-1'))

    def test_complete_raw_input_stops_before_calculation_and_keeps_open_engineering_notes(self):
        record = self.complete_raw_input()
        validation = self.store.validate('A')
        self.assertTrue(validation['ready'], validation)
        self.assertIn('待后续核算', validation['status'])
        self.assertEqual(record['last_operation'], 'apply_technical_input')
        self.assertIn('实际工艺与工装待工厂验证',
                      record['fields']['engineering_open_items']['effective']['value'])
        self.assertNotIn('quote_total', record)
        self.assertNotIn('quote_total', record['fields'])


if __name__ == '__main__':
    unittest.main()
