"""Global STEP upload allowance and durable recovery; ZeroClave execution only."""
import copy
import json
import multiprocessing
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from customer_upload_service import CustomerUploadService, CustomerBatchQuoteService
from customer_upload_quota import CustomerUploadQuota, QUOTA_FILE, ACTIVATION_FILE, MAX_BYTES, validate_step
from workbench_store import WorkbenchError
from test_batch_quote_service import STEP
import test_customer_upload_http as http_fixtures


def services(root):
    root = Path(root)
    batches = CustomerBatchQuoteService(root/'customer-quotes', None, None, processor=lambda row, content: {})
    return batches, CustomerUploadService(root/'access', batches)


def competing_upload(root, owner, event, barrier, results):
    batches, portal = services(root)
    try:
        barrier.wait(timeout=15)
        result = portal.upload(owner, event+'.step', STEP, event)
        results.put(('accepted', result['id']))
    except WorkbenchError as error:
        results.put((error.code, error.status))
    finally:
        batches.close()


class CustomerUploadQuotaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyl-step-quota-')
        self.addCleanup(self.temporary.cleanup)
        self.batches, self.portal = services(self.temporary.name)
        self.addCleanup(self.batches.close)
        _, self.owner = self.portal.new_session(); _, self.other = self.portal.new_session()

    def upload(self, event, owner=None, name='part.step', content=STEP):
        return self.portal.upload(owner or self.owner, name, content, event)

    def error(self, callback, code, status):
        with self.assertRaises(WorkbenchError) as failure: callback()
        self.assertEqual((failure.exception.code, failure.exception.status), (code, status))

    def test_quota_get_is_read_only_and_legacy_orders_do_not_activate_or_count(self):
        before = {str(path): path.read_bytes() for path in Path(self.temporary.name).rglob('*') if path.is_file()}
        quota = self.portal.quota(self.owner)
        self.assertEqual((quota['limit'], quota['used'], quota['remaining'], quota['active']), (10, 0, 10, False))
        self.assertEqual(quota['scope'], 'global'); self.assertEqual(quota['max_files'], 1)
        self.assertEqual(quota['max_bytes'], MAX_BYTES); self.assertEqual(quota['accepted_extensions'], ['.step', '.stp'])
        self.assertEqual(before, {str(path): path.read_bytes() for path in Path(self.temporary.name).rglob('*') if path.is_file()})
        event = self.portal._event(self.owner, 'legacy-event')
        old = self.batches.create('part.step', STEP, event)
        with self.batches._lock(old['id']):
            state = self.batches._read(old['id']); state['_customer_owner'] = self.owner; self.batches._save(state)
        original = (self.batches._folder(old['id'])/'state.json').read_bytes()
        self.assertEqual(self.upload('legacy-event')['id'], old['id'])
        self.assertEqual(self.portal.quota(self.owner)['used'], 0)
        self.assertFalse((self.portal.root/QUOTA_FILE).exists())
        self.assertEqual((self.batches._folder(old['id'])/'state.json').read_bytes(), original)

    def test_ten_global_uploads_survive_new_sessions_and_facade_restart(self):
        for index in range(10): self.upload('upload-'+str(index), self.owner if index % 2 else self.other)
        self.assertEqual((self.portal.quota(self.owner)['used'], self.portal.quota(self.other)['remaining']), (10, 0))
        _, fresh = self.portal.new_session()
        self.error(lambda: self.upload('eleventh', fresh), 'customer_upload_quota_exhausted', 429)
        restored = CustomerUploadService(self.portal.root, self.batches)
        self.assertEqual(restored.quota(fresh)['used'], 10)
        self.assertEqual(len(self.batches.list()), 10)

    def test_same_owner_event_replay_at_zero_budget_is_idempotent_and_conflicts_do_not_charge(self):
        accepted = self.upload('original')
        for index in range(9): self.upload('rest-'+str(index), self.other)
        before = (self.portal.root/QUOTA_FILE).read_bytes()
        self.assertEqual(self.upload('original')['id'], accepted['id'])
        changed = STEP.replace(b'HEADER;', b'HEADER;/* changed */')
        self.error(lambda: self.upload('original', content=changed), 'event_conflict', 409)
        self.error(lambda: self.upload('original', name='changed.stp'), 'event_conflict', 409)
        self.error(lambda: self.upload('original', self.other), 'customer_upload_quota_exhausted', 429)
        self.assertEqual((self.portal.root/QUOTA_FILE).read_bytes(), before)
        self.assertEqual(self.portal.quota(self.owner)['used'], 10)

    def test_rejects_archives_binary_disguises_concatenated_step_and_oversize_without_charge(self):
        for name in ('a.zip', 'a.rar', 'a.png', 'a.pdf', '../a.step', 'a.step/b.stp'):
            self.error(lambda name=name: self.upload('bad-type', name=name), 'customer_step_only', 415)
        invalid = [b'', b'PK\x03\x04fake zip', b'\x89PNG\r\n\x1a\n', b'ISO-10303-21;\x00binary',
                   STEP+STEP, STEP+b'trailing data', b'ISO-10303-21;HEADER;DATA;END-ISO-10303-21;',
                   STEP.replace(b'HEADER;', b'HEADER;/* open '*100), STEP.replace(b'HEADER;', b"HEADER;'unterminated"),
                   STEP.replace(b'DATA;', b'DATA( missing closing parenthesis;')]
        for content in invalid:
            self.error(lambda content=content: self.upload('invalid', content=content), 'invalid_step', 400)
        self.error(lambda: self.upload('too-big', content=b'x'*(MAX_BYTES+1)), 'too_large', 413)
        self.assertFalse((self.portal.root/QUOTA_FILE).exists()); self.assertEqual(self.portal.quota(self.owner)['used'], 0)
        legal = STEP.replace(b'HEADER;', b"HEADER;/* text ISO-10303-21; */FILE_NAME('ISO-10303-21;','',(),(),'','','');")
        self.assertEqual(len(validate_step('assembly.STP', legal)), 64)
        self.assertEqual(self.upload('valid', name='assembly.STP', content=legal)['files'][0]['kind'], 'step')

    def test_reserved_upload_recovers_after_create_failed_before_or_after_order_rename(self):
        real = self.batches.create
        for index, create_then_fail in enumerate((False, True)):
            event = 'recover-'+str(index)
            def failure(*args, **kwargs):
                if create_then_fail: real(*args, **kwargs)
                raise OSError('simulated storage interruption')
            with patch.object(self.batches, 'create', side_effect=failure), self.assertRaises(OSError): self.upload(event)
            quota = self.portal.quota(self.owner)
            self.assertEqual(quota['used'], index+1); self.assertEqual(quota['reserved_count'], 1)
            restored = CustomerUploadService(self.portal.root, self.batches)
            order = restored.upload(self.owner, 'part.step', STEP, event)
            self.assertEqual(restored.quota(self.owner)['used'], index+1)
            self.assertEqual(restored.quota(self.owner)['reserved_count'], 0)
            self.assertEqual(restored.get(self.owner, order['id'])['id'], order['id'])

    def test_owner_bound_order_recovers_after_quota_accept_write_failure(self):
        with patch.object(CustomerUploadQuota, 'accept', side_effect=OSError('interrupted accept')), self.assertRaises(OSError):
            self.upload('after-owner')
        self.assertEqual(self.portal.quota(self.owner)['reserved_count'], 1)
        restored = CustomerUploadService(self.portal.root, self.batches)
        order = restored.upload(self.owner, 'part.step', STEP, 'after-owner')
        self.assertEqual(restored.quota(self.owner)['accepted_count'], 1)
        self.assertEqual(len(restored.list(self.owner)['orders']), 1)
        self.assertTrue(order['can_start'])

    def test_known_validation_failure_without_an_order_releases_only_its_reservation(self):
        with patch.object(self.batches, 'create', side_effect=WorkbenchError('bad validation', 'invalid_step', 400)):
            self.error(lambda: self.upload('validation-error'), 'invalid_step', 400)
        self.assertEqual(self.portal.quota(self.owner)['used'], 0)
        self.upload('validation-error'); self.assertEqual(self.portal.quota(self.owner)['used'], 1)

    def test_corrupt_or_missing_active_ledger_fails_closed_without_resetting(self):
        self.upload('activate')
        path = self.portal.root/QUOTA_FILE; original = path.read_bytes()
        path.write_text('{broken')
        self.error(lambda: self.portal.quota(self.owner), 'customer_upload_quota_unavailable', 503)
        self.error(lambda: self.upload('unknown'), 'customer_upload_quota_unavailable', 503)
        path.write_bytes(original); path.unlink()
        self.assertTrue((self.portal.root/ACTIVATION_FILE).is_file())
        self.error(lambda: self.portal.quota(self.owner), 'customer_upload_quota_unavailable', 503)

    def test_tenth_and_eleventh_cross_process_competitors_cannot_both_succeed(self):
        for index in range(9): self.upload('seed-'+str(index))
        context = multiprocessing.get_context('spawn')
        barrier, results = context.Barrier(2), context.Queue()
        workers = [context.Process(target=competing_upload, args=(self.temporary.name, owner, event, barrier, results))
                   for owner, event in ((self.owner, 'competitor-A'), (self.other, 'competitor-B'))]
        try:
            for worker in workers: worker.start()
            values = [results.get(timeout=30), results.get(timeout=30)]
            for worker in workers: worker.join(timeout=15); self.assertEqual(worker.exitcode, 0)
            self.assertEqual(sorted(value[0] for value in values), ['accepted', 'customer_upload_quota_exhausted'])
            self.assertEqual(self.portal.quota(self.owner)['used'], 10)
            self.assertEqual(len(self.batches.list()), 10)
        finally:
            for worker in workers:
                if worker.is_alive(): worker.terminate(); worker.join(timeout=5)
            results.close(); results.join_thread()


class CustomerUploadProcessingQuotaTests(unittest.TestCase):
    def setUp(self):
        self.fixture = http_fixtures.CustomerUploadHTTPTests(methodName='test_no_cookie_or_forged_cookie_cannot_read_or_create_orders')
        self.fixture.setUp(); self.addCleanup(self.fixture.tearDown)

    def test_http_quota_requires_cookie_and_rejects_multipart_and_disguised_archives(self):
        f = self.fixture
        self.assertEqual(f.request(http_fixtures.API+'/quota')[0], 401)
        cookie, _ = f.session()
        status, quota, _ = f.request(http_fixtures.API+'/quota', cookie=cookie)
        self.assertEqual((status, quota['used']), (200, 0))
        status, error, _ = f.request(http_fixtures.API+'/orders?name=part.step', STEP, cookie=cookie,
            headers={'Content-Type': 'multipart/form-data; boundary=x', 'X-Event-Id': 'multipart'})
        self.assertEqual((status, error['error']['code']), (415, 'customer_step_only'))
        status, error, _ = f.upload(cookie, name='a.step', content=b'PK\x03\x04archive', event='disguise')
        self.assertEqual((status, error['error']['code']), (400, 'invalid_step'))
        self.assertEqual(f.request(http_fixtures.API+'/quota', cookie=cookie)[1]['used'], 0)

    def test_first_start_and_same_event_replay_do_not_allow_reprocessing_a_completed_order(self):
        f = self.fixture; cookie, _ = f.session(); order = f.upload(cookie, name='part.step', content=STEP)[1]
        self.assertTrue(order['can_start'])
        self.assertEqual(f.start(cookie, order, event='bounded-start')[0], 202)
        finished = f.finish(cookie, order['id']); self.assertFalse(finished['can_start'])
        self.assertEqual(f.start(cookie, order, event='bounded-start')[0], 202)
        self.assertEqual(len(f.calls), 1)
        self.assertEqual(f.start(cookie, finished, event='new-start')[0], 409)
        status, error, _ = f.request(http_fixtures.API+'/orders/'+order['id']+'/retry',
            {'event_id': 'rerun-complete', 'expected_revision': finished['revision']}, cookie=cookie)
        self.assertEqual((status, error['error']['code']), (409, 'nothing_to_retry'))
        self.assertEqual(len(f.calls), 1); self.assertFalse(finished['can_retry'])
        self.assertIsNone(f.server.customer_batches.engineering_recognition.rpc_url)

    def test_failed_source_retries_once_even_when_saved_as_completed_with_issues(self):
        f = self.fixture; cookie, _ = f.session(); f.fail_processing = True
        order = f.upload(cookie, name='part.step', content=STEP)[1]
        self.assertEqual(f.start(cookie, order, event='first-start')[0], 202)
        failed = f.finish(cookie, order['id']); self.assertTrue(failed['can_retry']); self.assertEqual(failed['retry_remaining'], 1)
        request = {'event_id': 'one-retry', 'expected_revision': failed['revision']}
        path = http_fixtures.API+'/orders/'+order['id']+'/retry'
        self.assertEqual(f.request(path, request, cookie=cookie)[0], 202)
        final = f.finish(cookie, order['id']); self.assertFalse(final['can_retry']); self.assertEqual(final['retry_remaining'], 0)
        self.assertEqual(f.request(path, request, cookie=cookie)[0], 202)
        self.assertEqual(len(f.calls), 2)
        status, error, _ = f.request(path, {'event_id': 'extra-retry', 'expected_revision': final['revision']}, cookie=cookie)
        self.assertEqual((status, error['error']['code']), (429, 'customer_retry_limit_exhausted'))
        self.assertEqual(len(f.calls), 2)
        altered = {**request, 'expected_revision': final['revision']}
        self.assertEqual(f.request(path, altered, cookie=cookie)[0], 409)

    def test_saved_retry_reservation_recovers_after_enqueue_lost_response_without_an_extra_run(self):
        f = self.fixture; cookie, _ = f.session(); f.fail_processing = True
        order = f.upload(cookie, name='part.step', content=STEP)[1]; f.start(cookie, order, event='start-lost')
        failed = f.finish(cookie, order['id']); path = http_fixtures.API+'/orders/'+order['id']+'/retry'
        request = {'event_id': 'retry-lost', 'expected_revision': failed['revision']}
        real = f.server.customer_batches.retry
        def interrupted(*args, **kwargs):
            real(*args, **kwargs); raise OSError('lost response after queue save')
        with patch.object(f.server.customer_batches, 'retry', side_effect=interrupted):
            self.assertEqual(f.request(path, request, cookie=cookie)[0], 503)
        self.assertEqual(f.request(path, request, cookie=cookie)[0], 202)
        final = f.finish(cookie, order['id']); self.assertEqual(final['retry_remaining'], 0)
        self.assertEqual(len(f.calls), 2)
        self.assertEqual(f.server.customer_upload.quota(f.server.customer_upload.session(cookie))['used'], 1)

    def test_first_start_reservation_recovers_before_or_after_enqueue_only_with_original_event(self):
        f = self.fixture; cookie, _ = f.session(); real = f.server.customer_batches.start
        for index, enqueue_first in enumerate((False, True)):
            order = f.upload(cookie, name='part.step', content=STEP, event='start-upload-'+str(index))[1]
            event = 'recover-start-'+str(index)
            def interrupted(*args, **kwargs):
                if enqueue_first: real(*args, **kwargs)
                raise OSError('lost start submission')
            with patch.object(f.server.customer_batches, 'start', side_effect=interrupted):
                self.assertEqual(f.start(cookie, order, event=event)[0], 503)
            self.assertEqual(f.start(cookie, order, event='different-'+str(index))[0], 409)
            self.assertEqual(f.start(cookie, order, event=event)[0], 202)
            f.finish(cookie, order['id'])
            self.assertEqual(len(f.calls), index+1)
            self.assertEqual(len(f.server.customer_batches.get(order['id'])['runs']), 1)
        self.assertEqual(f.server.customer_upload.quota(f.server.customer_upload.session(cookie))['used'], 2)


if __name__ == '__main__': unittest.main()
