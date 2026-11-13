"""RAR uses the same durable intake and quote lifecycle as ZIP/STEP."""
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from batch_quote_service import BatchQuoteService
from workbench_store import WorkbenchError
from test_batch_quote_archive import stored_rar4, stored_rar5
from test_batch_quote_service import STEP, SETTINGS, fixture


class RarBatchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'batches'
        self.calls = []
        def processor(row, content):
            self.calls.append(row['name'])
            return fixture(row, content)
        self.service = BatchQuoteService(self.root, None, None, processor=processor)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_rar_inventory_deduplication_original_and_immutable_quote_flow(self):
        content = stored_rar4([('cad/part.stp', STEP), ('duplicate/part.step', STEP),
                               ('notes/process.png', b'PNG')])
        state = self.service.create('order.RAR', content, 'rar-upload')
        self.assertEqual(self.calls, [])
        self.assertEqual(state['progress']['total'], 1)
        self.assertEqual(state['files'][1]['duplicate_of'], 'F0001')
        self.assertEqual(state['files'][2]['kind'], 'image')
        self.assertEqual(self.service.create('order.RAR', content, 'rar-upload')['id'], state['id'])
        folder = self.root / state['id']
        self.assertEqual((folder / 'original.bin').read_bytes(), content)
        self.assertEqual(state['source_sha256'], hashlib.sha256(content).hexdigest())
        self.assertEqual(sorted(path.name for path in (folder / 'files').iterdir()),
                         ['F0001.bin', 'F0002.bin', 'F0003.bin'])
        queued = self.service.start(state['id'], {'expected_revision': state['revision'],
                                    'request_id': 'rar-start', 'settings': copy.deepcopy(SETTINGS)})
        self.assertEqual(queued['status'], 'queued')
        self.service.process_pending_once()
        ready = self.service.get(state['id'])
        self.assertEqual(ready['status'], 'completed')
        self.assertEqual(self.calls, ['part.stp'])
        self.assertEqual(ready['result']['settings']['order_sets'], 10)
        self.assertTrue(ready['result']['lines'])
        self.assertTrue(ready['result']['links']['quote_url'].endswith('/customer-quote.html'))

    def test_rar5_is_accepted_and_failure_leaves_no_batch_or_staging(self):
        content = stored_rar5([('panel.stp', STEP)])
        state = self.service.create('order.rar', content, 'rar5-upload')
        self.assertEqual(state['files'][0]['name'], 'panel.stp')
        for index, bad in enumerate((b'bad rar', content[:-1])):
            with self.assertRaises(WorkbenchError) as rejected:
                self.service.create('bad.rar', bad, 'rar-bad-' + str(index))
            self.assertEqual(rejected.exception.code, 'rar_invalid')
        self.assertEqual(len(self.service.list()), 1)
        self.assertFalse(any(path.name.startswith('.upload-') for path in self.root.iterdir()))

    def test_rar_reuses_existing_upload_and_unpacked_limits(self):
        content = stored_rar4([('panel.stp', STEP)])
        for constant, value in (('MAX_BATCH_BYTES', 10), ('MAX_FILE_BYTES', 5),
                                ('MAX_UNPACKED_BYTES', 5)):
            with patch('batch_quote_service.' + constant, value), self.assertRaises(WorkbenchError):
                self.service.create('order.rar', content, 'limit-' + constant)
        self.assertEqual(self.service.list(), [])
        self.assertFalse(any(path.name.startswith('.upload-') for path in self.root.iterdir()))


if __name__ == '__main__':
    unittest.main()
