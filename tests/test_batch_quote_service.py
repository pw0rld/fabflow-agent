"""ZIP intake and durable quotation jobs; no native/model calls."""
import copy
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_service import BatchQuoteService
from workbench_store import WorkbenchError

STEP = b'ISO-10303-21;\nHEADER;ENDSEC;DATA;ENDSEC;END-ISO-10303-21;'
SETTINGS = {'order_sets': 10, 'pricing_method': 'cost_plus', 'rate_percent': '20',
            'material_profile': 'carbon_steel', 'rate_profile': 'demo-v1',
            'demo_confirmed': True, 'scope_confirmed': True}


def zipped(files):
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files:
            archive.writestr(name, content)
    return result.getvalue()


def fixture(row, content):
    return {'schema_version': 1, 'source_sha256': hashlib.sha256(content).hexdigest(),
            'output_unit': 'mm', 'solids': [{'id': 'S0001', 'name': 'Panel',
            'definition_id': 'DEF-PANEL', 'occurrence_path': ['assembly', 'panel'],
            'volume_mm3': 1000000, 'topology': {'valid': True, 'closed': True},
            'mesh': {'positions': [0, 0, 0]},
            'material': {'status': 'not_found', 'value': None}}]}


class BatchQuoteServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'batches'
        self.calls, self.failed_files = [], set()
        def processor(row, content):
            self.calls.append(row['name'])
            if row['name'] in self.failed_files:
                raise ValueError('fixture/private/secret must not reach browser')
            return fixture(row, content)
        self.processor = processor
        self.service = BatchQuoteService(self.root, None, None, processor=processor)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def create(self, files=None, event='upload-one'):
        return self.service.create('parts.zip', zipped(files or [('part.stp', STEP)]), event)

    def start(self, state, request='start-one', settings=None):
        return self.service.start(state['id'], {'expected_revision': state['revision'],
            'request_id': request, 'settings': copy.deepcopy(settings or SETTINGS)})

    def test_inventory_paths_reference_files_and_content_deduplication(self):
        state = self.create([('cad/part.stp', STEP), ('duplicate/part.step', STEP),
            ('notes/process.png', b'PNG'), ('notes/readme.txt', b'note'),
            ('__MACOSX/._part.stp', b'ignored'), ('.DS_Store', b'ignored')])
        self.assertEqual(self.calls, [])
        self.assertEqual(state['status'], 'uploaded')
        self.assertEqual(state['progress']['total'], 1)
        self.assertEqual(len(state['files']), 4)
        self.assertEqual(state['files'][0]['relative_path'], 'cad/part.stp')
        self.assertEqual(state['files'][1]['duplicate_of'], 'F0001')
        self.assertEqual(state['files'][1]['status'], 'reference')
        self.assertEqual(state['files'][2]['kind'], 'image')
        self.assertEqual(state['files'][3]['kind'], 'other')
        self.assertNotIn('_job', state)
        self.assertEqual(self.service.file(state['id'], 'F0003'), (b'PNG', 'image/png', 'process.png'))

    def test_upload_idempotency_and_content_conflict(self):
        # A retransmission sends identical bytes. Rebuilding the ZIP can change
        # its entry timestamps even when the enclosed STEP is unchanged.
        payload = zipped([('part.stp', STEP)])
        first = self.service.create('parts.zip', payload, 'upload-one')
        self.assertEqual(first['id'], self.service.create('parts.zip', payload, 'upload-one')['id'])
        self.assertEqual(len(self.service.list()), 1)
        with self.assertRaises(WorkbenchError) as rejected:
            self.create([('different.stp', STEP)])
        self.assertEqual(rejected.exception.status, 409)
        self.assertEqual(self.service.get(first['id'])['files'], first['files'])

    def test_direct_step_keeps_original_without_native_work(self):
        state = self.service.create('assembly.STEP', STEP, 'direct')
        self.assertEqual(state['files'][0]['name'], 'assembly.STEP')
        self.assertEqual(self.calls, [])

    def test_malformed_paths_and_nested_archives_leave_no_partial_batch(self):
        for index, name in enumerate(['../outside.stp', '/absolute.stp', 'C:/drive.stp',
                                      'cad\\part.stp', 'cad/../part.stp', 'nested.zip']):
            with self.subTest(name=name), self.assertRaises(WorkbenchError):
                self.create([(name, STEP)], event='bad-' + str(index))
        self.assertEqual(self.service.list(), [])
        self.assertFalse(any(path.name.startswith('.upload-') for path in self.root.iterdir()))

    def test_links_and_duplicate_paths_are_rejected(self):
        info = zipfile.ZipInfo('linked.stp')
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(WorkbenchError):
            self.create([(info, b'target')], event='link')
        with self.assertRaises(WorkbenchError):
            self.create([('cad/part.stp', STEP), ('CAD/PART.STP', STEP)], event='same-path')

    def test_bad_zip_and_limits(self):
        with self.assertRaises(WorkbenchError):
            self.service.create('broken.zip', b'not zip', 'broken')
        with patch('batch_quote_service.MAX_FILES', 1), self.assertRaises(WorkbenchError):
            self.create([('a.stp', STEP), ('b.stp', STEP + b'2')], event='count')
        with patch('batch_quote_service.MAX_UNPACKED_BYTES', 10), self.assertRaises(WorkbenchError):
            self.create(event='expanded')
        with patch('batch_quote_service.MAX_BATCH_BYTES', 5), self.assertRaises(WorkbenchError):
            self.create(event='compressed')

    def test_reference_only_package_cannot_generate_a_quote(self):
        state = self.create([('reference.png', b'image')])
        with self.assertRaises(WorkbenchError):
            self.start(state)
        current = self.service.get(state['id'])
        self.assertEqual(current['status'], 'uploaded')
        self.assertEqual(current['runs'], [])
        self.assertEqual(self.calls, [])

    def test_empty_step_does_not_discard_other_valid_files(self):
        state = self.create([('empty.stp', b''), ('good.stp', STEP), ('blank-note.txt', b'')])
        self.start(state)
        self.service.process_pending_once()
        done = self.service.get(state['id'])
        self.assertEqual(done['status'], 'completed_with_issues')
        self.assertEqual(done['files'][0]['status'], 'failed')
        self.assertIn('为空', done['files'][0]['error'])
        self.assertEqual(done['files'][1]['status'], 'ready')
        self.assertEqual(self.calls, ['good.stp'])

    def test_explicit_settings_validation_precedes_queueing(self):
        state = self.create()
        for field, value in [('order_sets', True), ('demo_confirmed', False),
            ('scope_confirmed', False), ('pricing_method', None), ('rate_percent', 'NaN')]:
            invalid = copy.deepcopy(SETTINGS)
            invalid[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.start(state, settings=invalid)
        self.assertEqual(self.service.get(state['id'])['status'], 'uploaded')
        self.assertEqual(self.calls, [])

    def test_async_start_and_request_replay_do_not_duplicate_work(self):
        state = self.create()
        request = {'expected_revision': state['revision'], 'request_id': 'same', 'settings': SETTINGS}
        queued = self.service.start(state['id'], request)
        self.assertEqual(queued['revision'], self.service.start(state['id'], request)['revision'])
        self.assertEqual(queued['status'], 'queued')
        self.assertEqual(self.calls, [])
        self.assertTrue(self.service.process_pending_once())
        done = self.service.get(state['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(len(done['runs']), 1)
        self.assertEqual(self.service.start(state['id'], request)['status'], 'completed')
        self.assertFalse(self.service.process_pending_once())
        self.assertEqual(self.calls, ['part.stp'])

    def test_revision_and_idempotency_conflicts(self):
        state = self.create()
        self.start(state)
        with self.assertRaises(WorkbenchError) as rejected:
            self.start(state, request='another')
        self.assertEqual(rejected.exception.status, 409)
        changed = copy.deepcopy(SETTINGS)
        changed['order_sets'] = 11
        with self.assertRaises(WorkbenchError) as rejected:
            self.start(state, settings=changed)
        self.assertEqual(rejected.exception.code, 'event_conflict')

    def test_retry_failed_file_reuses_good_geometry_and_retains_old_snapshot(self):
        self.failed_files.add('bad.stp')
        state = self.create([('good.stp', STEP), ('bad.stp', STEP + b'bad'), ('notes.pdf', b'PDF')])
        self.start(state)
        self.service.process_pending_once()
        partial = self.service.get(state['id'])
        self.assertEqual(partial['status'], 'completed_with_issues')
        self.assertEqual([row['status'] for row in partial['files']], ['ready', 'failed', 'reference'])
        self.assertNotIn('secret', partial['files'][1]['error'])
        first_run = partial['runs'][0]['id']
        first_json = self.service.artifact(state['id'], first_run, 'quote.json')[0]
        self.failed_files.clear()
        queued = self.service.retry(state['id'], {'expected_revision': partial['revision'],
            'request_id': 'retry-bad', 'file_ids': ['F0002']})
        self.assertEqual(queued['status'], 'queued')
        self.service.process_pending_once()
        complete = self.service.get(state['id'])
        self.assertEqual(complete['status'], 'completed')
        self.assertEqual(len(complete['runs']), 2)
        self.assertEqual(self.calls, ['good.stp', 'bad.stp', 'bad.stp'])
        self.assertEqual(first_json, self.service.artifact(state['id'], first_run, 'quote.json')[0])
        self.assertNotEqual(complete['result']['run_id'], first_run)

    def test_retry_only_accepts_failed_step_identifiers(self):
        state = self.create([('part.stp', STEP), ('ref.jpg', b'image')])
        self.start(state)
        self.service.process_pending_once()
        state = self.service.get(state['id'])
        for values in [['F0001'], ['F0002'], ['missing'], [], [{}]]:
            with self.subTest(values=values), self.assertRaises(WorkbenchError):
                self.service.retry(state['id'], {'expected_revision': state['revision'],
                    'request_id': 'invalid-retry', 'file_ids': values})
        self.assertEqual(len(self.service.get(state['id'])['runs']), 1)

    def test_new_service_resumes_queued_work_with_saved_settings(self):
        state = self.create()
        self.start(state)
        resumed = BatchQuoteService(self.root, None, None, processor=self.processor)
        self.assertTrue(resumed.process_pending_once())
        done = resumed.get(state['id'])
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['settings'], SETTINGS)
        self.assertEqual(self.calls, ['part.stp'])

    def test_interrupt_and_resume_skip_finished_files(self):
        state = self.create([('first.stp', STEP), ('second.stp', STEP + b'2')])
        def interrupt(row, content):
            result = self.processor(row, content)
            self.service._stop.set()
            return result
        self.service.processor = interrupt
        self.start(state)
        self.service.process_pending_once()
        paused = self.service.get(state['id'])
        self.assertEqual(paused['status'], 'queued')
        self.assertEqual(paused['files'][0]['status'], 'ready')
        resumed = BatchQuoteService(self.root, None, None, processor=self.processor)
        resumed.process_pending_once()
        self.assertEqual(resumed.get(state['id'])['status'], 'completed')
        self.assertEqual(self.calls, ['first.stp', 'second.stp'])

    def test_worker_fence_prevents_two_instances_processing_same_job(self):
        state = self.create()
        self.start(state)
        other = BatchQuoteService(self.root, None, None, processor=self.processor)
        with self.service._worker_lease() as acquired:
            self.assertTrue(acquired)
            self.assertFalse(other.process_pending_once())
        self.assertEqual(self.calls, [])
        self.assertTrue(other.process_pending_once())
        self.assertEqual(self.calls, ['part.stp'])

    def test_snapshot_before_state_commit_recovers_same_run_once(self):
        state = self.create()
        self.start(state)
        with self.service._lock(state['id']):
            job = copy.deepcopy(self.service._read(state['id'])['_job'])
        self.service.process_pending_once()
        complete = self.service.get(state['id'])
        with self.service._lock(state['id']):
            interrupted = self.service._read(state['id'])
            interrupted.update(status='running', result=None, runs=[], _job=job)
            self.service._save(interrupted)
        other = BatchQuoteService(self.root, None, None, processor=self.processor)
        other.process_pending_once()
        self.assertEqual(other.get(state['id'])['result'], complete['result'])
        self.assertEqual(len(other.get(state['id'])['runs']), 1)
        self.assertEqual(self.calls, ['part.stp'])

    def test_downloads_are_real_snapshots_with_no_mesh_or_server_path(self):
        state = self.create()
        self.start(state)
        self.service.process_pending_once()
        done = self.service.get(state['id'])
        run = done['runs'][0]['id']
        for filename in ['quote.html', 'bom.csv', 'cost.csv', 'quote.json']:
            content, mime, download = self.service.artifact(state['id'], run, filename)
            self.assertIsInstance(content, bytes)
            self.assertTrue(content)
            self.assertIn(filename, download)
            self.assertNotIn('mesh', content.decode('utf-8', errors='replace'))
        record = json.loads(self.service.artifact(state['id'], run, 'quote.json')[0])
        self.assertEqual(record['batch_id'], state['id'])
        self.assertEqual(record['settings']['order_sets'], 10)
        # Scalar candidate provenance may legitimately say "native_geometry".
        # Reject actual geometry payloads and private fields at any depth,
        # rather than matching a word embedded in an evidence-source label.
        forbidden = {'geometry', 'mesh', 'positions', 'indices', '_job',
                     'private_path', 'native_id', 'step_file', '_diagnostic'}

        def inspect_public(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertNotIn(key, forbidden, 'Private/display data leaked through key ' + str(key))
                    inspect_public(item)
            elif isinstance(value, list):
                for item in value:
                    inspect_public(item)

        inspect_public(done)
        inspect_public(record)
        self.assertIn('engineering_native', record)
        self.assertNotIn(str(self.root), json.dumps(done))
        self.assertNotIn(str(self.root), json.dumps(record))

    def test_identifiers_cannot_select_paths_outside_batch(self):
        state = self.create()
        with self.assertRaises(WorkbenchError):
            self.service.file(state['id'], '../outside')
        with self.assertRaises(WorkbenchError):
            self.service.artifact(state['id'], 'RUN-' + 'a' * 32, '../state.json')
        with self.assertRaises(WorkbenchError):
            self.service.get('../outside')

    def test_attachment_hash_failure_prevents_native_processing(self):
        state = self.create()
        (self.root / state['id'] / 'files/F0001.bin').write_bytes(STEP[:-1] + b'!')
        self.start(state)
        self.service.process_pending_once()
        done = self.service.get(state['id'])
        self.assertEqual(done['files'][0]['status'], 'failed')
        self.assertEqual(done['status'], 'completed_with_issues')
        self.assertIsNone(done['result']['totals']['total_price'])
        self.assertEqual(self.calls, [])

    def test_configured_model_analyzer_is_rejected_before_native_work(self):
        class Unsafe:
            analyzer = staticmethod(lambda *args: (_ for _ in ()).throw(AssertionError('model prohibited')))
        service = BatchQuoteService(self.root, None, Unsafe())
        state = self.create()
        with self.assertRaises(WorkbenchError):
            service.start(state['id'], {'expected_revision': state['revision'],
                'request_id': 'unsafe', 'settings': SETTINGS})
        self.assertEqual(self.service.get(state['id'])['status'], 'uploaded')


if __name__ == '__main__':
    unittest.main()
