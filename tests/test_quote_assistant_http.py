"""Quote assistant HTTP/transaction tests with a labelled scalar parser fixture.

Run on ZeroClave. No actual model or CAD executable is invoked by this module.
"""
import copy
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from prep_record import atomic_write, digest
import test_engineering_reprice_http as fixture


class QuoteAssistantHTTPTests(unittest.TestCase):
    request = fixture.EngineeringRepriceHTTPTests.request
    upload = fixture.EngineeringRepriceHTTPTests.upload
    setUp = fixture.EngineeringRepriceHTTPTests.setUp
    tearDown = fixture.EngineeringRepriceHTTPTests.tearDown
    tree = fixture.EngineeringRepriceHTTPTests.tree

    def interpreter(self, changes=None):
        calls = []
        def parse(base, message):
            calls.append((digest(base), message))
            return {'status': 'ready', 'message': '已理解修改要求，预览尚未保存。',
                    'patch': {'changes': copy.deepcopy(changes or [
                        {'field': 'order_sets', 'value': '20'}, {'field': 'rate_percent', 'value': '15'}])},
                    'parser': {'kind': 'explicit_test_fixture', 'model_called': False}}
        self.service.assistant_parser = parse
        return calls

    def enqueue(self, message='这单改为20套，成本加成15%。', event='preview-one'):
        status, response, _ = self.request(self.prefix + '/assistant/preview', {'event_id': event, 'message': message})
        self.assertEqual(status, 202, response)
        return response['proposal']

    def ready(self, proposal):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, response, _ = self.request(self.prefix + '/assistant/proposals/' + proposal['id'])
            self.assertEqual(status, 200, response)
            proposal = response['proposal']
            if proposal['status'] not in {'queued', 'running'}:
                return proposal
            time.sleep(.02)
        self.fail('Assistant fixture timed out')

    def prepared(self, event='preview-one'):
        calls = self.interpreter()
        proposal = self.ready(self.enqueue(event=event))
        self.assertEqual(proposal['status'], 'ready', proposal)
        self.service.assistant.close()
        return proposal, calls

    def adopt(self, proposal, event='adopt-one', **extra):
        payload = {'event_id': event, 'proposal_id': proposal['id'], 'expected_revision': proposal['expected_revision'], **extra}
        status, response, _ = self.request(self.prefix + '/assistant/commit', payload)
        return status, response, payload

    def exports(self):
        return {name: self.service.artifact(self.batch_id, self.run_id, name)[0]
                for name in ('quote.json', 'bom.csv', 'cost.csv', 'quote-print.html')}

    def test_preview_queues_without_blocking_http_or_changing_financials(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def parse(base, message):
            calls.append(message)
            entered.set()
            self.assertTrue(release.wait(5))
            return {'status': 'ready', 'message': '20套的金额预览', 'patch': {'changes': [{'field': 'order_sets', 'value': '20'}]}}
        self.service.assistant_parser = parse
        state_before, exports_before = self.service.get(self.batch_id), self.exports()
        try:
            first = self.enqueue()
            self.assertTrue(entered.wait(3))
            replay = self.enqueue()
            self.assertEqual(first['id'], replay['id'])
            self.assertIn(replay['status'], {'queued', 'running'})
            self.assertEqual(self.service.get(self.batch_id), state_before)
            self.assertEqual(self.exports(), exports_before)
            self.assertNotIn('result', replay)
        finally:
            release.set()
        result = self.ready(first)
        self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.native_calls, ['F0001'])

    def test_ready_preview_does_not_save_or_change_old_quotation(self):
        old_tree, original = self.tree(), self.exports()
        proposal, calls = self.prepared()
        self.assertEqual(len(calls), 1)
        self.assertEqual(proposal['before_totals'], self.original['totals'])
        self.assertNotEqual(proposal['after_totals'], proposal['before_totals'])
        self.assertEqual(proposal['before_order_sets'], 10)
        self.assertEqual(proposal['after_order_sets'], 20)
        self.assertTrue(proposal['changes'])
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 1)
        self.assertEqual(self.exports(), original)
        for name, content in old_tree.items():
            self.assertEqual((self.folder / name).read_bytes(), content, name)
        public = json.dumps(proposal, ensure_ascii=False)
        self.assertNotIn('source_binding', public)
        self.assertNotIn('result_digest', public)

    def test_explicit_commit_creates_one_pending_run_and_retains_history(self):
        files, exports = self.tree(), self.exports()
        proposal, calls = self.prepared()
        with (patch.object(self.service, '_native', side_effect=AssertionError('No CAD call')),
              patch.object(self.service.engineering_recognition, 'enqueue', side_effect=AssertionError('No drawing model'))):
            status, response, payload = self.adopt(proposal)
        self.assertEqual(status, 201, response)
        result = response['batch']['result']
        self.assertNotEqual(result['run_id'], self.run_id)
        self.assertEqual(result['settings']['order_sets'], 20)
        self.assertEqual(result['settings']['rate_percent'], '15')
        self.assertEqual(response['batch']['settings'], result['settings'])
        self.assertEqual(result['totals'], proposal['after_totals'])
        self.assertEqual(result['status'], 'pending_review')
        self.assertTrue(result['simulated'])
        self.assertFalse(result['engineering_approved'])
        self.assertFalse(result['business_approved'])
        self.assertEqual(result['source_files'], self.original['source_files'])
        for old, new in zip(self.original['lines'], result['lines']):
            for field in ('id', 'file_id', 'native_definition', 'volume_mm3', 'material_raw'):
                self.assertEqual(old.get(field), new.get(field), field)
            self.assertEqual(new['quantity_total'], old['quantity_per_set'] * 20)
        for name, content in files.items():
            if name != 'state.json':
                self.assertEqual((self.folder / name).read_bytes(), content, name)
        self.assertEqual(self.exports(), exports)
        new_id = result['run_id']
        for suffix in ('-engineering.json', '-engineering-recognition.json'):
            saved = json.loads((self.folder / 'runs' / (new_id + suffix)).read_text())
            self.assertEqual(saved['source_digest'], digest(json.loads((self.folder / 'runs' / (new_id + '.json')).read_text())))
            self.assertEqual(saved['run_id'], new_id)
            self.assertEqual(saved['status'], 'pending_review' if suffix == '-engineering.json' else 'completed')
        again_status, again, _ = self.adopt(proposal)
        self.assertEqual(again_status, 201, again)
        self.assertTrue(again['replayed'])
        self.assertEqual(again['run_id'], new_id)
        self.assertEqual(len(again['batch']['runs']), 2)
        duplicate_status, duplicate, _ = self.adopt(proposal, event='adopt-another-event')
        self.assertEqual(duplicate_status, 201, duplicate)
        self.assertEqual(duplicate['run_id'], new_id)
        self.assertTrue(duplicate['replayed'])
        self.assertEqual(self.adopt(proposal, event='adopt-another-event', expected_revision=proposal['expected_revision'] + 1)[0], 409)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.native_calls, ['F0001'])

    def test_clarification_has_no_usable_amount_or_commit(self):
        self.service.assistant_parser = lambda *_: {'status': 'needs_clarification', 'message': '请指定要修改的零件行号和单价。'}
        proposal = self.ready(self.enqueue('把那个便宜一点'))
        self.assertEqual(proposal['status'], 'needs_clarification')
        self.assertIsNone(proposal['after_totals'])
        self.assertEqual(proposal['changes'], [])
        self.assertEqual(self.adopt(proposal)[0], 409)
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 1)

    def test_invalid_model_patch_or_unavailable_parser_cannot_write_quote_or_leak_error(self):
        self.interpreter([{'field': 'unknown_field', 'value': '1'}])
        invalid = self.ready(self.enqueue(event='invalid-model-patch'))
        self.assertEqual(invalid['status'], 'needs_clarification', invalid)
        self.assertEqual(self.adopt(invalid)[0], 409)
        def broken(*_):
            raise ValueError('sensitive-token-do-not-print')
        self.service.assistant_parser = broken
        failed = self.ready(self.enqueue(event='model-unavailable'))
        self.assertEqual(failed['status'], 'failed')
        self.assertNotIn('sensitive-token', json.dumps(failed))
        self.assertEqual(self.adopt(failed, event='bad-adopt')[0], 409)
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 1)

    def test_review_or_recognition_change_invalidates_ready_preview(self):
        proposal, _ = self.prepared()
        self.service.save_engineering(self.batch_id, self.run_id, {'event_id': 'after-preview',
            'expected_revision': 2, 'review': {'header': {'basis_notes': '工程条件已修订'}}})
        status, response, _ = self.adopt(proposal)
        self.assertEqual(status, 409, response)
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 1)

    def test_pending_preview_fails_when_recognition_changes_during_model_call(self):
        entered, release = threading.Event(), threading.Event()
        def parse(*_):
            entered.set()
            self.assertTrue(release.wait(5))
            return {'status': 'ready', 'message': '预览', 'patch': {'changes': [{'field': 'order_sets', 'value': '20'}]}}
        self.service.assistant_parser = parse
        try:
            proposal = self.enqueue()
            self.assertTrue(entered.wait(3))
            path = self.folder / 'runs' / (self.run_id + '-engineering-recognition.json')
            data = json.loads(path.read_text()); data['updated_at'] = '2026-11-09T01:00:00Z'
            atomic_write(path, json.dumps(data))
        finally:
            release.set()
        proposal = self.ready(proposal)
        self.assertEqual(proposal['status'], 'failed', proposal)
        self.assertEqual(proposal['error_code'], 'source_conflict')
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 1)

    def test_stale_preview_revision_and_changed_idempotency_body_are_rejected(self):
        proposal, _ = self.prepared()
        wrong = {**proposal, 'expected_revision': proposal['expected_revision'] + 1}
        self.assertEqual(self.adopt(wrong)[0], 409)
        status, response, _ = self.adopt(proposal)
        self.assertEqual(status, 201, response)
        self.assertEqual(self.adopt(proposal, expected_revision=proposal['expected_revision'] + 1)[0], 409)
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 2)

    def test_prepared_commit_recovers_same_run_after_pointer_write_interruption(self):
        proposal, _ = self.prepared()
        original_save = self.service._save
        def interrupted(state):
            if len(state['runs']) == 2:
                raise OSError('Controlled interruption before state pointer is saved')
            return original_save(state)
        with patch.object(self.service, '_save', side_effect=interrupted):
            status, _, payload = self.adopt(proposal)
        self.assertEqual(status, 503)
        journal = next((self.folder / 'assistant/commits').glob('*.json'))
        record = json.loads(journal.read_text())
        allocated = record['run_id']
        self.assertEqual(record['status'], 'prepared')
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 1)
        self.assertEqual(self.request(self.prefix + '/engineering', {
            'event_id': 'blocked-engineering-save', 'expected_revision': 2, 'review': {'header': {'basis_notes': '不应写入'}}})[0], 409)
        self.service.assistant.process_pending_once()
        current = self.service.get(self.batch_id)
        self.assertEqual(current['result']['run_id'], allocated)
        self.assertEqual(current['settings']['order_sets'], 20)
        status, replay, _ = self.adopt(proposal)
        self.assertEqual(status, 201, replay)
        self.assertEqual(replay['run_id'], allocated)
        self.assertTrue(replay['replayed'])
        self.assertEqual(len(replay['batch']['runs']), 2)

    def test_lost_response_after_state_commit_retains_same_run_and_revision(self):
        proposal, _ = self.prepared()
        original_save = self.service.assistant._save
        def interrupted(path, record):
            if path.parent.name == 'commits' and record['status'] == 'committed':
                raise OSError('Controlled lost commit receipt')
            return original_save(path, record)
        with patch.object(self.service.assistant, '_save', side_effect=interrupted):
            status, _, _ = self.adopt(proposal)
        self.assertEqual(status, 503)
        current = self.service.get(self.batch_id)
        revision = current['revision']
        self.assertEqual(len(current['runs']), 2)
        status, replay, _ = self.adopt(proposal)
        self.assertEqual(status, 201, replay)
        self.assertEqual(replay['run_id'], current['result']['run_id'])
        self.assertEqual(replay['batch']['revision'], revision)
        self.assertEqual(len(replay['batch']['runs']), 2)

    def test_http_origin_input_limits_and_client_result_injection_are_rejected(self):
        self.interpreter()
        for payload in ({'event_id': 'bad', 'message': ''}, {'event_id': 'long', 'message': 'a' * 4001},
                        {'event_id': 'extra', 'message': '20套', 'result': {}}):
            self.assertEqual(self.request(self.prefix + '/assistant/preview', payload)[0], 400)
        self.assertEqual(self.request(self.prefix + '/assistant/preview', {'event_id': 'foreign', 'message': '20套'},
            headers={'Origin': 'https://example.invalid'})[0], 403)
        proposal, _ = self.prepared()
        self.assertEqual(self.adopt(proposal, result={})[0], 400)
        self.assertEqual(self.request(self.prefix + '/assistant/proposals/AP-' + 'f' * 32)[0], 404)
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 1)


if __name__ == '__main__':
    unittest.main()
