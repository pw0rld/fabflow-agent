"""Offline unit-basis regressions; all records and sources are temporary fixtures."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from prep_record import RecordError, RecordStore


class DecimalUnitAliasTests(unittest.TestCase):
    EXPECTED_UNITS = {
        'material_usage_per_piece': 'kg/piece',
        'material_rate': 'CNY/kg',
        'setup_fee': 'CNY/order',
    }

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='unit-basis-regression-')
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'tasks'
        self.inputs = self.base / 'inputs'
        self.inputs.mkdir()
        self.store = RecordStore(self.root)
        self.counter = 0
        self.init('A')

    def init(self, task_id):
        self.store.init(task_id, {
            'inquiry': '纯本地单元测试：费用及用量单位，全部数据为模拟。',
            'simulated': True, 'files': [],
            'versions': {'model': 'not-called', 'skill': 'offline-unit-regression'},
        })

    def field(self, name, value, unit, raw_unit, *, raw_value=None):
        return {'name': name, 'value': value, 'unit': unit,
                'raw_value': value if raw_value is None else raw_value,
                'raw_unit': raw_unit, 'status': 'verified'}

    def payload(self, fields):
        self.counter += 1
        source_id = f'technical-{self.counter}'
        path = self.inputs / (source_id + '.json')
        path.write_text(json.dumps({
            'simulated': True, 'purpose': 'offline test input, not factory data',
            'inputs': [{k: f.get(k) for k in ('name', 'raw_value', 'raw_unit')}
                       for f in fields],
        }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        fields = copy.deepcopy(fields)
        for field in fields:
            field['evidence'] = {
                'source_type': 'technical_input', 'reference': source_id,
                'actor': '本地测试驱动（模拟）', 'simulated': True,
            }
        return {'sources': [{'id': source_id, 'path': str(path),
                            'kind': 'technical_input',
                            'actor': '本地测试驱动（模拟）', 'simulated': True}],
                'fields': fields, 'questions': [], 'notes': []}

    def apply(self, fields, task_id='A'):
        revision = self.store.load(task_id)['data_revision']
        payload = self.payload(fields)
        return self.store.apply(task_id, 'apply_technical_input', payload,
                                revision, f'unit-event-{self.counter}')

    def reject(self, field, *, unit_error=True):
        before = (self.root / 'A/record.json').read_bytes()
        with self.assertRaises(RecordError) as raised:
            self.apply([field])
        self.assertEqual(before, (self.root / 'A/record.json').read_bytes())
        self.assertEqual(self.store.load('A')['data_revision'], 0)
        if unit_error:
            message = str(raised.exception)
            self.assertIn(field['name'], message)
            self.assertIn('expected', message)
            self.assertIn(self.EXPECTED_UNITS[field['name']], message)
            self.assertIn('actual', message)
            self.assertIn(repr(field.get('unit')), message)
            self.assertIn(repr(field.get('raw_unit')), message)

    def test_declared_aliases_persist_original_units_and_values(self):
        cases = [
            ('material_usage_per_piece', '0.400', '0.4', 'kg/piece',
             ('kg/piece', 'kg/件', '千克/件')),
            ('material_rate', '10.00', '10', 'CNY/kg',
             ('CNY/kg', '元/kg', '元/千克')),
            ('setup_fee', '0.00', '0', 'CNY/order',
             ('CNY/order', '元/单', '元/订单')),
        ]
        index = 0
        for name, raw, normalized, unit, aliases in cases:
            for alias in aliases:
                with self.subTest(field=name, raw_unit=alias):
                    index += 1
                    task_id = f'ALIAS-{index}'
                    self.init(task_id)
                    self.apply([self.field(name, raw, unit, alias)], task_id)
                    # Reload through the public API, including source hash checks.
                    record = RecordStore(self.root).load(task_id)
                    slot = record['fields'][name]
                    candidate = slot['effective']
                    self.assertEqual(record['data_revision'], 1)
                    self.assertEqual(slot['status'], 'verified')
                    self.assertEqual(candidate['raw_value'], raw)
                    self.assertEqual(candidate['raw_unit'], alias)
                    self.assertEqual(candidate['value'], normalized)
                    self.assertEqual(candidate['unit'], unit)
                    self.assertEqual(slot['history'], [candidate])
                    reference = candidate['evidence']['reference']
                    source = record['sources'][reference]
                    snapshot = self.root / task_id / source['path']
                    self.assertEqual(hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                                     source['sha256'])
                    self.assertEqual(json.loads(snapshot.read_text())['inputs'][0],
                                     {'name': name, 'raw_value': raw, 'raw_unit': alias})

    def test_reported_three_chinese_bases_save_together_in_one_revision(self):
        # Reproduce the reported values in a new temporary record, never the live task.
        fields = [self.field('material_usage_per_piece', '5', 'kg/piece', 'kg/件'),
                  self.field('material_rate', '10', 'CNY/kg', '元/kg'),
                  self.field('setup_fee', '0', 'CNY/order', '元/单')]
        record = self.apply(fields)
        self.assertEqual(record['data_revision'], 1)
        self.assertEqual(len(record['events']), 1)
        for field in fields:
            with self.subTest(field=field['name']):
                actual = record['fields'][field['name']]['effective']
                self.assertEqual(actual['raw_unit'], field['raw_unit'])
                self.assertEqual(actual['unit'], field['unit'])
                self.assertEqual(actual['value'], field['value'])
                self.assertEqual(actual['status'], 'verified')
        self.assertFalse(self.store.validate('A')['ready'])
        self.assertFalse((self.root / 'A/quotes').exists())

    def test_normalized_unit_still_requires_the_canonical_spelling(self):
        for name, alias in (('material_usage_per_piece', 'kg/件'),
                            ('material_rate', '元/kg'), ('setup_fee', '元/单')):
            with self.subTest(field=name):
                self.reject(self.field(name, '1', alias, alias))

    def test_different_or_incomplete_pricing_bases_are_rejected(self):
        for name, raw_unit in (
            ('material_usage_per_piece', 'kg'),
            ('material_usage_per_piece', 'kg/order'),
            ('material_rate', 'CNY/order'),
            ('material_rate', '元/件'),
            ('setup_fee', 'CNY/piece'),
            ('setup_fee', '元/批'),
            ('setup_fee', '元'),
        ):
            with self.subTest(field=name, raw_unit=raw_unit):
                self.reject(self.field(name, '1', self.EXPECTED_UNITS[name], raw_unit))

    def test_weight_and_rate_scale_conversions_are_not_aliases(self):
        for name, raw, value, raw_unit in (
            ('material_usage_per_piece', '400', '0.4', 'g/件'),
            ('material_usage_per_piece', '0.001', '1', '吨/件'),
            ('material_rate', '0.01', '10', '元/g'),
            ('material_rate', '10000', '10', '元/吨'),
        ):
            with self.subTest(field=name, raw_unit=raw_unit):
                self.reject(self.field(name, value, self.EXPECTED_UNITS[name],
                                       raw_unit, raw_value=raw))

    def test_missing_raw_or_normalized_unit_is_rejected_with_diagnostics(self):
        for name, canonical in self.EXPECTED_UNITS.items():
            for key in ('unit', 'raw_unit'):
                for missing in (None, '', 'omitted'):
                    with self.subTest(field=name, key=key, missing=missing):
                        field = self.field(name, '1', canonical, canonical)
                        if missing == 'omitted':
                            field.pop(key)
                        else:
                            field[key] = missing
                        self.reject(field)

    def test_aliases_do_not_relax_numeric_validation_or_change_raw_numbers(self):
        for name, raw, value, alias in (
            ('material_usage_per_piece', '0', '0', 'kg/件'),
            ('material_usage_per_piece', '-1', '-1', '千克/件'),
            ('material_rate', '-10', '-10', '元/kg'),
            ('setup_fee', '-1', '-1', '元/单'),
            ('material_usage_per_piece', 'NaN', 'NaN', 'kg/件'),
            ('material_rate', 'Infinity', 'Infinity', '元/千克'),
            ('setup_fee', True, True, '元/订单'),
            ('material_usage_per_piece', '400', '0.4', 'kg/件'),
        ):
            with self.subTest(field=name, raw=raw, value=value):
                self.reject(self.field(name, value, self.EXPECTED_UNITS[name],
                                       alias, raw_value=raw), unit_error=False)

    def test_explicit_zero_material_and_setup_fees_remain_valid(self):
        record = self.apply([self.field('material_rate', '0.00', 'CNY/kg', '元/kg'),
                             self.field('setup_fee', '0.00', 'CNY/order', '元/单')])
        for name in ('material_rate', 'setup_fee'):
            self.assertEqual(record['fields'][name]['effective']['value'], '0')
            self.assertEqual(record['fields'][name]['status'], 'verified')

    def test_operation_rate_units_are_not_extended_by_decimal_aliases(self):
        rates = [{'operation': '模拟工序', 'value': '1', 'unit': '元/件'}]
        self.reject(self.field('operation_rates', rates, None, None), unit_error=False)


if __name__ == '__main__':
    unittest.main()
