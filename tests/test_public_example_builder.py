"""Fixed-input public example receipts; synthetic cached native facts only.

Execute on the remote build server. No CAD process, AI, customer files or live
pricing catalog participates in these mathematical/projection checks.
"""
import copy
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
specification = importlib.util.spec_from_file_location('fabflow_public_example_builder',
                                                     ROOT / 'tools/build_public_examples.py')
builder = importlib.util.module_from_spec(specification)
specification.loader.exec_module(builder)
SOURCE = b'ISO-10303-21;\nHEADER;ENDSEC;DATA;ENDSEC;END-ISO-10303-21;\n'
PRIVATE = 'PRIVATE-NATIVE-FIELD-MUST-NOT-ESCAPE'


class PublicExampleBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='public-example-math-')
        self.root = Path(self.temp.name)
        (self.root / 'public.step').write_bytes(SOURCE)
        (self.root / 'license.txt').write_text('Synthetic CC0 license fixture')
        self.spec = {**copy.deepcopy(builder.EXAMPLES[0]), 'input': 'public.step',
            'filename': 'public.step', 'sha256': hashlib.sha256(SOURCE).hexdigest(),
            'license_file': 'license.txt', 'repository': 'Synthetic/public-models',
            'url': 'https://example.invalid/public-models/public.step',
            'attribution': 'Synthetic author; test data only.'}
        self.cache, self.output = self.root / 'cache', self.root / 'output'
        self.native = self.cache / self.spec['id']
        self.native.mkdir(parents=True); self.output.mkdir()
        self.raw = {'source_sha256': self.spec['sha256'], 'output_unit': 'mm',
                    'source_path': '/private/' + PRIVATE + '.step',
                    'customer_owner': PRIVATE, 'solids': [self.solid('S0001'), self.solid('S0002')]}

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def solid(identity):
        return {'id': identity, 'name': 'Synthetic repeated plate', 'definition_id': 'definition-one',
            'topology': {'valid': True, 'closed': True}, 'volume_mm3': 100000,
            'bounds_mm': {'size': [100, 100, 10]},
            'mesh': {'positions': [0, 0, 0, 1, 0, 0, 0, 1, 0], 'indices': [0, 1, 2]},
            'thickness': {'status': 'candidate', 'value_mm': 10, 'method': 'synthetic-native-fact',
                          'private_note': PRIVATE},
            'material': {'status': 'not_found', 'private_note': PRIVATE},
            'step_file': '/private/' + PRIVATE + '.step', 'thumbnail': '/private/' + PRIVATE + '.png',
            'internal_cost': PRIVATE}

    def prepare(self):
        (self.native / 'geometry.json').write_text(json.dumps(self.raw))
        with patch.object(builder, 'ROOT', self.root), \
             patch.object(builder.subprocess, 'run', side_effect=AssertionError('CAD must not run')) as process:
            try:
                return builder.prepare(self.spec, 'not-a-real-python', self.cache, self.output)
            finally:
                process.assert_not_called()

    def test_public_source_digest_mismatch_is_rejected_before_any_conversion(self):
        self.spec['sha256'] = '0' * 64
        with self.assertRaisesRegex(AssertionError, 'Public source hash differs'):
            self.prepare()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_repeated_native_definition_preserves_quantity_and_cost_basis_without_private_fields(self):
        row = self.prepare()
        target = self.output / self.spec['id']
        detail = json.loads((target / 'example.json').read_bytes())
        geometry = json.loads((target / 'geometry.json').read_bytes())
        self.assertEqual(detail['stats'], {'part_count': 1, 'occurrence_count': 2, 'solid_count': 2})
        self.assertEqual((row['part_count'], row['occurrence_count']), (1, 2))
        part = detail['parts'][0]
        self.assertEqual((part['id'], part['quantity'], part['solid_ids']),
                         ('P0001', 2, ['S0001', 'S0002']))
        self.assertEqual(Decimal(part['estimated_net_weight_kg']), Decimal('0.785'))
        quotation = detail['quotation']
        self.assertEqual(quotation['order_sets'], 10)
        self.assertEqual(quotation['items'], [{'part_id': 'P0001', 'quantity_total': 20,
                                             'unit_price': '12.88', 'amount': '257.60'}])
        # Independent baseline: two instances per set, 10 sets. Thirty yuan is
        # charged once for the definition, not once per occurrence or per set.
        self.assertEqual(quotation['subtotal'], '214.71')
        self.assertEqual(quotation['total'], '257.60')
        self.assertEqual(quotation['unit_per_set'], '25.76')
        self.assertEqual(geometry['groups'], [{'id': 'P0001', 'name': 'Synthetic repeated plate',
                                              'solid_ids': ['S0001', 'S0002']}])
        self.assertEqual(set(geometry), {'source_sha256', 'output_unit', 'solids', 'groups'})
        self.assertTrue(all(set(solid) == {'id', 'name', 'mesh'} for solid in geometry['solids']))
        self.assertNotIn(PRIVATE, json.dumps(detail) + json.dumps(geometry) + json.dumps(row))
        self.assertEqual((target / 'source.step').read_bytes(), SOURCE)
        for field, leaf in (('detail_url', 'example.json'), ('geometry_url', 'geometry.json'),
                            ('download_url', 'source.step')):
            self.assertEqual(row[field], '/workbench/public-examples/motor-bracket/' + leaf)

    def test_inconsistent_volumes_cannot_be_merged_as_the_same_part(self):
        self.raw['solids'][1]['volume_mm3'] = 100001
        with self.assertRaises(AssertionError):
            self.prepare()
        self.assertFalse((self.output / self.spec['id']).exists())


if __name__ == '__main__':
    unittest.main()
