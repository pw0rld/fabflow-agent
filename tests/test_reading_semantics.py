"""Same-source finish compatibility and non-destructive display classification."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from reading_semantics import AUTO_CONFLICT_REASON, compatible_surface, display_observations


def finish(value, reference='FILE-A', page=1):
    return {'name': 'surface_treatment', 'value': value,
            'evidence': {'reference': reference, 'page': page, 'region': 'FINISH'}}


def observed(text, source_pass='initial', **overrides):
    return {'name': 'surface_treatment', 'raw_text': text, 'source_pass': source_pass,
            'status': 'conflict', 'reason': AUTO_CONFLICT_REASON,
            'evidence': {'reference': 'FILE-A', 'page': 1, 'region': 'FINISH'}, **overrides}


class ReadingSemanticsTests(unittest.TestCase):
    def test_shorter_same_source_finish_is_compatible_but_colors_are_not(self):
        self.assertTrue(compatible_surface(finish('Painted'), finish('Painted; RAL7035')))
        self.assertTrue(compatible_surface(finish('RAL 7035'), finish('Painted; RAL7035')))
        self.assertFalse(compatible_surface(finish('Painted; RAL9005'), finish('Painted; RAL7035')))
        self.assertFalse(compatible_surface(finish('Unpainted'), finish('Painted')))
        self.assertFalse(compatible_surface(finish('Painted; glossy'), finish('Painted')))
        self.assertFalse(compatible_surface(finish('Painted', 'FILE-B'), finish('Painted; RAL7035')))
        self.assertFalse(compatible_surface(finish('Painted', page=2), finish('Painted; RAL7035')))

    def test_old_auto_conflict_is_reconciled_without_mutating_source(self):
        rows = [observed('标题栏：TREATMENT: Painted；FINISH: RAL7035'),
                observed('Painted; RAL7035'), observed('Painted', 'review')]
        before = deepcopy(rows)
        projected = display_observations(rows)
        self.assertTrue(all(row['status'] == 'observed' for row in projected))
        self.assertEqual(rows, before)
        self.assertEqual([row['raw_text'] for row in projected], [row['raw_text'] for row in rows])

    def test_real_model_conflict_and_distinct_colors_stay_unresolved(self):
        for rows in ([observed('Painted; RAL9005'), observed('Painted; RAL7035', 'review')],
                     [observed('Painted'), observed('Painted; RAL7035', 'review', reason='模型标记存在冲突')],
                     [observed('Painted'), observed('Painted; RAL7035', 'legacy')]):
            self.assertTrue(all(row['status'] == 'conflict' for row in display_observations(rows)))

    def test_table_and_chamfer_labels_keep_the_original_model_label(self):
        rows = [observed('BENDING TABLE\nTag | Direction | Angle | Inner Radius',
                         name='engineering_open_items', label='BOM表头', source_name='BOM表头', status='observed'),
                observed('TOP: 3 X 45°-24Nos.', name='engineering_open_items',
                         label='折弯标注', source_name='折弯标注', status='observed')]
        corrected = display_observations(rows)
        self.assertEqual([row['label'] for row in corrected], ['折弯表', '倒角标注'])
        self.assertEqual([row['source_label'] for row in corrected], ['BOM表头', '折弯标注'])
        self.assertEqual(corrected[1]['source_name'], '折弯标注')
        self.assertEqual(rows[1]['label'], '折弯标注')
        self.assertTrue(corrected[1]['classification_note'])


if __name__ == '__main__':
    unittest.main()
