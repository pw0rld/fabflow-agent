"""Offline drawing-output regression through the bridge and real record store."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from dsh_bridge import DshBridge, GAUGE_WARNING, PROCESS_ROUTE_WARNING
from workbench_store import Workbench


class DrawingAnnotationIntegrationTests(unittest.TestCase):
    def test_original_bracket_annotations_preserve_valid_unverified_candidates(self):
        # This is a contract fixture, not image recognition: no RPC, prompt or
        # provider is invoked. The source is nevertheless a real current-task
        # attachment so both bridge and RecordStore provenance checks execute.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            factory = Mock(side_effect=AssertionError('model dispatch prohibited'))
            app = Workbench(root / 'tasks', reader_root=root / 'readers', bridge_factory=factory)
            task = app.create({'event_id': 'annotation-regression',
                               'inquiry': '按当前支架图纸提取候选，数量未知', 'simulated': True})
            task_id = task['id']
            task = app.upload(task_id, 'annotation-drawing', task['revision'],
                              '支架.png', b'\x89PNG\r\n\x1a\nfake-offline-drawing-fixture')
            source_id = task['attachments'][0]['id']

            def candidate(name, raw_value, value, raw_unit=None, unit=None):
                return {'name': name, 'raw_value': raw_value, 'value': value,
                        'raw_unit': raw_unit, 'unit': unit, 'status': 'verified',
                        'reason': '图纸候选，未经人工核对',
                        'evidence': {'reference': source_id, 'page': 1,
                                     'region': '离线支架标注回归夹具'}}

            output = {'candidates': [
                candidate('product_name', 'Bracket', '支架'),
                candidate('material_family', 'STEEL', '钢'),
                candidate('material_grade', 'A36', 'A36'),
                candidate('drawing_units', 'INCHES', '英寸'),
                candidate('thickness', '16 GAUGE', '16 GAUGE', 'gauge', None),
                candidate('inner_bend_radius', 'R .08', '2.032', 'inch', 'mm'),
                candidate('process_route', 'automated cutting; bending UP 90°',
                          ['自动切割', '向上折弯90°']),
            ], 'warnings': []}
            original_output = deepcopy(output)
            workspace = root / 'bridge'
            workspace.mkdir()
            bridge = DshBridge('http://127.0.0.1:3080', workspace)
            manifest = {'task_id': task_id, 'inquiry': '', 'files': [{'id': source_id}]}
            run = {'id': 'annotations-one', 'base_revision': task['revision'],
                   'candidates_applied': False}

            with patch.object(bridge, '_rpc', side_effect=AssertionError('RPC prohibited')) as rpc:
                candidates, warnings = bridge._candidates(json.dumps(output, ensure_ascii=False), manifest)
                result = {'candidates': candidates, 'warnings': warnings}
                with app.lock(task_id):
                    app._apply_reading(task_id, run, result)
                rpc.assert_not_called()

            factory.assert_not_called()
            self.assertEqual(output, original_output)
            self.assertTrue(run['candidates_applied'])
            expected = {'product_name', 'material_family', 'material_grade',
                        'drawing_units', 'inner_bend_radius'}
            self.assertEqual({item['name'] for item in candidates}, expected)
            self.assertIn(GAUGE_WARNING, warnings)
            self.assertIn(PROCESS_ROUTE_WARNING, warnings)

            record = app.records.load(task_id)
            saved = app.task(task_id)
            self.assertEqual(saved['revision'], task['revision'] + 1)
            for name in expected:
                with self.subTest(field=name):
                    self.assertEqual(saved['fields'][name]['status'], 'pending_review')
                    self.assertEqual(saved['fields'][name]['evidence']['reference'], source_id)
                    self.assertEqual(len(record['fields'][name]['history']), 1)
            radius = record['fields']['inner_bend_radius']['effective']
            self.assertEqual(radius['raw_value'], 'R .08')
            self.assertEqual(radius['raw_unit'], 'inch')
            self.assertEqual(radius['value'], '2.032')
            self.assertEqual(radius['unit'], 'mm')
            self.assertEqual(radius['normalization'], {'from': 'inch', 'to': 'mm', 'factor': '25.4'})
            for name in ('thickness', 'process_route'):
                with self.subTest(deferred=name):
                    self.assertEqual(saved['fields'][name]['status'], 'missing')
                    self.assertIsNone(saved['fields'][name]['value'])
                    self.assertIsNone(record['fields'][name]['effective'])
                    self.assertEqual(record['fields'][name]['history'], [])
            self.assertFalse(any(slot['status'] == 'verified' for slot in saved['fields'].values()))
            self.assertFalse(saved['validation']['ready'])


if __name__ == '__main__':
    unittest.main()
