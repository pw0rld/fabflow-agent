"""Real intake/recovery admission with synthetic CAD; no SDK or model calls."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_service import BatchQuoteService
from test_batch_quote_service import SETTINGS, STEP, fixture, zipped


class AutoEngineeringAdmissionTests(unittest.TestCase):
    def exercise(self, *, environment, recovered=False, rpc=None, injected=False, expected=True):
        with tempfile.TemporaryDirectory(prefix='fabflow-agent-auto-engineering-') as temporary:
            calls = []
            def processor(row, content):
                calls.append(row['id'])
                return fixture(row, content)
            service = BatchQuoteService(Path(temporary) / 'batches', None, None, processor=processor)
            self.addCleanup(service.close)
            state = service.create('synthetic.zip', zipped([('part.stp', STEP), ('notes.png', b'synthetic-reference')]), 'synthetic-upload')
            service.start(state['id'], {'request_id': 'synthetic-start', 'expected_revision': state['revision'],
                                       'settings': copy.deepcopy(SETTINGS)})
            with service._lock(state['id']):
                job = copy.deepcopy(service._read(state['id'])['_job'])
            if recovered:
                # Persist the immutable quote before simulating a lost state
                # commit. The recovered worker must never re-read geometry.
                with patch.dict('os.environ', {}, clear=True):
                    self.assertTrue(service.process_pending_once())
                with service._lock(state['id']):
                    interrupted = service._read(state['id'])
                    interrupted.update(status='running', result=None, runs=[], _job=job)
                    service._save(interrupted)
            recognition = service.engineering_recognition
            recognition.rpc_url = rpc
            if injected:
                recognition.ai_reader = lambda *args, **kwargs: self.fail('Enqueue must not execute AI synchronously')
            with patch.dict('os.environ', environment, clear=True), \
                    patch.object(recognition, 'enqueue', wraps=recognition.enqueue) as enqueue:
                self.assertTrue(service.process_pending_once())
                done = service.get(state['id'])
                self.assertIn(done['status'], {'completed', 'completed_with_issues'})
                _, result = service._preview_run(done['id'], done['result']['run_id'])
                self.assertEqual(len(done['runs']), 1)
                self.assertEqual(calls, ['F0001'])
                record = recognition.read(done['id'], result['run_id'], result)
                if expected:
                    enqueue.assert_called_once_with(done['id'], result['run_id'],
                        {'event_id': 'auto-engineering-' + result['run_id']})
                    self.assertEqual(record['status'], 'queued')
                    self.assertIsNone(record['ai'])
                    self.assertIsNone(recognition._thread)
                else:
                    enqueue.assert_not_called()
                    self.assertEqual(record['status'], 'not_started')
                snapshot = service._folder(done['id']) / 'runs' / (result['run_id'] + '.json')
                saved = snapshot.read_bytes()
                self.assertEqual(json.loads(saved)['totals'], result['totals'])
                self.assertFalse(service.process_pending_once())
                self.assertEqual(snapshot.read_bytes(), saved)
                if injected:
                    observed = []
                    def explicit_reader(folder, lines, sources, native, rpc, **kwargs):
                        observed.append((folder, len(sources)))
                        return {'lines': {}, 'coverage': [], 'warnings': [],
                                'provenance': {'kind': 'synthetic', 'status': 'completed'}}
                    recognition.ai_reader = explicit_reader
                    queued = service.recognize_engineering(done['id'], result['run_id'], {'event_id': 'explicit-synthetic-read'})
                    self.assertEqual(queued['recognition']['status'], 'queued')
                    self.assertTrue(recognition.process_pending_once())
                    self.assertEqual(len(observed), 1)
                    self.assertEqual(observed[0][1], 1)
                    self.assertEqual(recognition.read(done['id'], result['run_id'], result)['status'], 'completed')
                    self.assertEqual(snapshot.read_bytes(), saved)

    def test_configured_sdk_enqueues_both_new_and_recovered_runs_without_a_dsh_url(self):
        environment = {'FABFLOW_AGENT_BACKEND': 'openai_agents', 'FABFLOW_MODEL': 'synthetic-model',
                       'OPENAI_API_KEY': 'synthetic-key'}
        for recovered in (False, True):
            with self.subTest(recovered=recovered):
                self.exercise(environment=environment, recovered=recovered)

    def test_missing_sdk_configuration_does_not_admit_ai_even_with_a_stale_legacy_url(self):
        for recovered in (False, True):
            with self.subTest(recovered=recovered):
                self.exercise(environment={'FABFLOW_AGENT_BACKEND': 'openai_agents'}, recovered=recovered,
                              rpc='http://127.0.0.1:3080', expected=False)

    def test_explicit_legacy_backend_with_url_keeps_automatic_admission(self):
        for recovered in (False, True):
            with self.subTest(recovered=recovered):
                self.exercise(environment={'FABFLOW_AGENT_BACKEND': 'dsh'}, recovered=recovered,
                              rpc='http://127.0.0.1:3080')

    def test_legacy_backend_without_url_does_not_admit_an_unconfigured_run(self):
        for recovered in (False, True):
            with self.subTest(recovered=recovered):
                self.exercise(environment={'FABFLOW_AGENT_BACKEND': 'dsh'}, recovered=recovered, expected=False)

    def test_explicit_reader_remains_usable_without_provider_credentials(self):
        for recovered in (False, True):
            with self.subTest(recovered=recovered):
                self.exercise(environment={}, recovered=recovered, injected=True, expected=False)

    def test_empty_or_oversized_quote_does_not_enqueue_unsupported_engineering_work(self):
        with tempfile.TemporaryDirectory(prefix='fabflow-agent-auto-cap-') as temporary:
            service = BatchQuoteService(Path(temporary) / 'batches', None, None)
            self.addCleanup(service.close)
            with patch.object(service.engineering_recognition, 'enqueue') as enqueue, \
                    patch.dict('os.environ', {'FABFLOW_MODEL': 'synthetic', 'OPENAI_API_KEY': 'synthetic-key'}, clear=True):
                for count in (0, 1001):
                    service._enqueue_engineering('unsubmitted-synthetic', {'lines': [{}] * count, 'run_id': 'unsubmitted'})
                enqueue.assert_not_called()


if __name__ == '__main__':
    import platform
    if platform.system() != 'Linux':
        raise SystemExit('Run tests only on the configured remote build server.')
    unittest.main()
