"""Synthetic private lead workflow; execute on the remote server only.

No SMS, identity verification, AI, CAD or real contacts. The HTTP integration
must check order ownership before constructing this store's allowlisted binding.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from trial_leads import TrialLeadStore
from workbench_store import WorkbenchError

A, B = 'a' * 64, 'b' * 64
PROFILE = {'event_id': 'profile-1', 'expected_revision': 0, 'company': '合成测试工厂',
           'contact_name': '', 'contact_method': 'phone', 'contact_value': '13800000000',
           'manufacturing_type': 'sheet_metal', 'contact_consent': False}
BINDING = {'batch_id': 'BATCH-' + 'c' * 32, 'run_id': 'RUN-' + 'd' * 32,
           'name': 'synthetic.step', 'status': 'running', 'part_count': 2}


class TrialLeadStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='trial-leads-fixture-')
        self.root = Path(self.temp.name) / 'private'
        self.store = TrialLeadStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, owner=A, **changes):
        return self.store.save_profile(owner, copy.deepcopy(PROFILE) | changes)

    def apply(self, owner=A, **changes):
        return self.store.record_interest(owner, {'event_id': 'interest-1', 'interest': 'factory_trial',
            'note': '合成试用需求', 'contact_consent': True} | changes)

    def test_private_persistence_normalization_and_no_verification_claim(self):
        self.assertIsNone(self.store.profile(A))
        self.assertEqual(self.store.applications(A), [])
        saved = self.save(contact_value='+86 138-0000-0000')
        self.assertFalse(saved['replayed'])
        profile = saved['profile']
        self.assertEqual(profile['contact_value'], '13800000000')
        self.assertEqual(profile['revision'], 1)
        self.assertEqual(profile['verification_status'], 'unverified')
        self.assertNotIn('owner', profile)
        self.assertEqual(TrialLeadStore(self.root).profile(A), profile)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)

    def test_privileged_or_invalid_values_are_rejected(self):
        for change in ({'verification_status': 'verified'}, {'verified': True}, {'owner': B},
                       {'status': 'won'}, {'contact_method': []}, {'manufacturing_type': {}},
                       {'contact_value': 'not a phone'}, {'contact_consent': 'true'},
                       {'company': 'bad\ncompany'}, {'expected_revision': True}):
            with self.subTest(change=change), self.assertRaises(WorkbenchError):
                self.store.save_profile(A, copy.deepcopy(PROFILE) | change)
        self.assertIsNone(self.store.profile(A))

    def test_equal_unverified_contacts_never_merge_owners(self):
        self.save(A)
        self.save(B)
        leads = self.store.staff_list()['leads']
        self.assertEqual(len(leads), 2)
        self.assertEqual(len({row['id'] for row in leads}), 2)
        first, second = self.apply(A), self.apply(B)
        self.assertNotEqual(first['application']['id'], second['application']['id'])
        self.assertEqual(len(self.store.applications(A)), 1)
        self.assertEqual(self.store.applications(B)[0]['id'], second['application']['id'])
        self.assertEqual(self.store.profile(A)['contact_value'], self.store.profile(B)['contact_value'])

    def test_profile_events_are_idempotent_and_cannot_revert_later_withdrawal(self):
        self.save(contact_consent=True)
        self.assertTrue(self.save(contact_consent=True)['replayed'])
        self.assertEqual(self.store.profile(A)['revision'], 1)
        self.save(event_id='profile-2', expected_revision=1, contact_consent=False)
        replay = self.save(contact_consent=True)
        self.assertTrue(replay['replayed'])
        self.assertFalse(replay['profile']['contact_consent'])
        self.assertEqual(replay['profile']['revision'], 2)
        for change in ({'company': '其他合成内容'}, {'contact_consent': False}):
            with self.subTest(change=change), self.assertRaises(WorkbenchError) as caught:
                self.store.save_profile(A, PROFILE | {'contact_consent': True} | change)
            self.assertEqual(caught.exception.code, 'event_conflict')
        with self.assertRaises(WorkbenchError) as caught:
            self.save(event_id='stale-profile', expected_revision=0)
        self.assertEqual(caught.exception.code, 'revision_conflict')

    def test_concurrent_acknowledgment_recovery_creates_one_record(self):
        barrier = threading.Barrier(6)
        def save(_):
            barrier.wait(timeout=10)
            return self.save()
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(save, range(6)))
        self.assertEqual(sum(not row['replayed'] for row in results), 1)
        self.assertEqual(self.store.staff_list()['total'], 1)
        detail = self.store.staff_get(self.store.staff_list()['leads'][0]['id'])
        self.assertEqual(len(detail['consent_history']), 1)
        self.assertEqual(len(detail['audit']), 1)

    def test_application_recovery_is_persistent_and_retains_original_binding(self):
        self.save()
        payload = {'event_id': 'bound-interest', 'interest': 'consultation', 'note': '', 'contact_consent': True,
                   'batch_id': BINDING['batch_id'], 'run_id': BINDING['run_id']}
        first = self.store.record_interest(A, payload, BINDING)
        reopened = TrialLeadStore(self.root)
        replay = reopened.record_interest(A, payload, BINDING | {'status': 'completed', 'part_count': 3})
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['application']['id'], first['application']['id'])
        self.assertEqual(replay['application']['order_binding'], BINDING)
        self.assertEqual(replay['application']['status'], 'submitted')
        replay['application']['order_binding']['name'] = 'local mutation'
        self.assertEqual(reopened.applications(A)[0]['order_binding']['name'], 'synthetic.step')
        for extra in ({'path': '/private/synthetic'}, {'unit_cost': '1.00'}, {'geometry': {}}):
            with self.subTest(extra=extra), self.assertRaises(WorkbenchError):
                self.store.record_interest(A, payload | {'event_id': 'bad-binding'}, BINDING | extra)
        for change, binding in (({}, None), ({'order_binding': BINDING}, BINDING),
                                ({'batch_id': 'BATCH-' + 'e' * 32}, BINDING)):
            with self.subTest(change=change), self.assertRaises(WorkbenchError):
                self.store.record_interest(A, payload | {'event_id': 'bad-client-binding'} | change, binding)

    def test_withdrawal_masks_details_removes_contactable_list_and_survives_replay(self):
        self.save(contact_consent=True)
        app = self.apply()
        lead_id = self.store.staff_list()['leads'][0]['id']
        summary = self.store.staff_list(contactable_only=True)['leads'][0]
        self.assertFalse(summary['contact_revealed'])
        self.assertEqual(summary['contact_value'], '138****0000')
        detail = self.store.staff_get(lead_id, actor='synthetic-admin')
        self.assertTrue(detail['contact_revealed'])
        self.assertEqual(detail['contact_value'], '13800000000')
        self.assertEqual(detail['audit'][0]['action'], 'contact_viewed')
        self.save(event_id='withdraw', expected_revision=app['profile']['revision'], contact_consent=False)
        self.assertEqual(self.store.staff_list(contactable_only=True)['total'], 0)
        detail = self.store.staff_get(lead_id, actor='synthetic-admin')
        self.assertFalse(detail['contact_revealed'])
        self.assertNotIn('13800000000', json.dumps(detail))
        self.assertTrue(detail['applications'][0]['contact_consent'])
        self.assertFalse(detail['applications'][0]['can_contact'])
        replay = self.apply()
        self.assertTrue(replay['replayed'])
        self.assertFalse(replay['profile']['contact_consent'])
        self.assertEqual(self.store.profile(A)['contact_value'], '13800000000')

    def test_staff_updates_are_versioned_audited_and_private(self):
        self.save(contact_consent=True)
        lead = self.store.staff_list()['leads'][0]
        payload = {'event_id': 'staff-1', 'expected_revision': lead['revision'],
                   'status': 'contacted', 'assignee': '合成销售', 'note': '仅内部的合成跟进'}
        result = self.store.staff_update(lead['id'], payload, actor='synthetic-admin')
        self.assertFalse(result['replayed'])
        self.assertEqual(result['lead']['audit'][0]['action'], 'follow_up_updated')
        self.assertTrue(self.store.staff_update(lead['id'], payload, actor='synthetic-admin')['replayed'])
        profile = self.store.profile(A)
        self.assertNotIn('assignee', profile)
        self.assertNotIn('note', profile)
        self.assertEqual(profile['verification_status'], 'unverified')
        with self.assertRaises(WorkbenchError) as caught:
            self.store.staff_update(lead['id'], payload | {'event_id': 'staff-stale'})
        self.assertEqual(caught.exception.code, 'revision_conflict')
        with self.assertRaises(WorkbenchError):
            self.store.staff_update(lead['id'], payload | {'contact_consent': True})

    def test_staff_suppression_needs_explicit_customer_opt_in_to_restore_contact(self):
        self.save(contact_consent=True)
        lead = self.store.staff_list()['leads'][0]
        payload = {'event_id': 'staff-stop', 'expected_revision': lead['revision'],
                   'status': 'do_not_contact', 'assignee': '', 'note': ''}
        stopped = self.store.staff_update(lead['id'], payload)['lead']
        self.assertFalse(stopped['can_contact'])
        status_only = self.store.staff_update(lead['id'], payload | {'event_id': 'staff-reopen',
            'expected_revision': stopped['revision'], 'status': 'new'})['lead']
        self.assertFalse(status_only['can_contact'])
        self.assertFalse(status_only['contact_revealed'])
        self.save(event_id='customer-opt-in', expected_revision=status_only['revision'], contact_consent=True)
        self.assertEqual(self.store.staff_list(contactable_only=True)['total'], 1)

    def test_masking_covers_known_contact_repeated_in_notes_without_changing_machine_fields(self):
        self.save(contact_consent=True)
        application = self.apply(note='合成联系值：13800000000')
        lead_id = self.store.staff_list()['leads'][0]['id']
        self.assertNotIn('13800000000', json.dumps(self.store.staff_list()))
        self.save(event_id='withdraw-notes', expected_revision=application['profile']['revision'], contact_consent=False)
        self.assertNotIn('13800000000', json.dumps(self.store.staff_get(lead_id)))
        self.save(B, contact_method='wechat', contact_value='contacted', contact_consent=True)
        other = next(row for row in self.store.staff_list()['leads'] if row['id'] != lead_id)
        self.store.staff_update(other['id'], {'event_id': 'staff-enum', 'expected_revision': other['revision'],
            'status': 'contacted', 'assignee': '', 'note': ''})
        projected = next(row for row in self.store.staff_list()['leads'] if row['id'] == other['id'])
        self.assertEqual(projected['status'], 'contacted')
        self.assertEqual(projected['contact_value'], 'c***d')

    def test_missing_profile_invalid_pagination_and_bounded_applications(self):
        with self.assertRaises(WorkbenchError) as caught:
            self.apply(B)
        self.assertEqual(caught.exception.code, 'trial_profile_required')
        with self.assertRaises(WorkbenchError):
            self.store.profile('invalid-owner')
        for arguments in ({'limit': 0}, {'offset': -1}, {'status': []}, {'contactable_only': 1}):
            with self.subTest(arguments=arguments), self.assertRaises(WorkbenchError):
                self.store.staff_list(**arguments)
        self.save(contact_method='wechat', contact_value='synthetic_wechat')
        with patch('trial_leads.MAX_APPLICATIONS', 2):
            self.apply(event_id='interest-first')
            self.apply(event_id='interest-second')
            with self.assertRaises(WorkbenchError) as caught:
                self.apply(event_id='interest-third')
        self.assertEqual(caught.exception.code, 'trial_application_limit')
        self.assertEqual(len(self.store.applications(A)), 2)
        self.assertEqual(self.store.staff_list()['leads'][0]['contact_value'], 's***t')

    def test_trial_without_profile_does_not_create_a_lead(self):
        self.assertIsNone(self.store.record_trial(A, BINDING))
        self.assertIsNone(self.store.profile(A))
        self.assertEqual(self.store.staff_list()['total'], 0)
        self.save(B)
        self.store.record_trial(A, BINDING)
        only = self.store.staff_list()['leads'][0]
        self.assertEqual(only['trial_count'], 0)
        self.assertIsNone(only['latest_trial'])

    def test_trial_upsert_is_persistent_polling_is_read_only_and_revision_is_unchanged(self):
        saved = self.save()
        self.store.record_trial(A, BINDING)
        summary = self.store.staff_list()['leads'][0]
        first = self.store.staff_get(summary['id'])
        self.assertEqual(summary['trial_count'], 1)
        self.assertEqual(summary['latest_trial']['order_binding'], BINDING)
        self.assertEqual(len(first['trials']), 1)
        self.assertEqual(self.store.profile(A)['revision'], saved['profile']['revision'])
        self.assertFalse(first['can_contact'])
        with patch('trial_leads.stamp', side_effect=AssertionError('Unchanged poll must not write')):
            self.store.record_trial(A, dict(reversed(list(BINDING.items()))))
        unchanged = self.store.staff_get(summary['id'])
        self.assertEqual(first, unchanged)
        revised_binding = BINDING | {'name': 'revised-synthetic.step', 'status': 'completed',
                                     'run_id': 'RUN-' + 'e' * 32, 'part_count': 3}
        self.store.record_trial(A, revised_binding)
        reopened = TrialLeadStore(self.root)
        updated = reopened.staff_get(summary['id'])
        self.assertEqual(updated['trial_count'], 1)
        self.assertEqual(updated['trials'][0]['id'], first['trials'][0]['id'])
        self.assertEqual(updated['trials'][0]['created_at'], first['trials'][0]['created_at'])
        self.assertEqual(updated['trials'][0]['order_binding'], revised_binding)
        self.assertEqual(updated['trials'][0]['part_count'], 3)
        self.assertEqual(reopened.profile(A)['revision'], saved['profile']['revision'])
        self.assertEqual(sum(row['action'] == 'trial_recorded' for row in updated['audit']), 2)
        with self.assertRaises(WorkbenchError):
            self.store.record_trial(A, revised_binding | {'cost': '1.00'})

    def test_trial_metadata_is_bounded_without_touching_profile_or_consent(self):
        self.save(contact_consent=True)
        with patch('trial_leads.MAX_TRIALS', 2):
            for index in range(1, 4):
                binding = BINDING | {'batch_id': 'BATCH-' + format(index, '032x')}
                with patch('trial_leads.stamp', return_value='2026-11-14T00:00:0' + str(index) + '+00:00'):
                    self.store.record_trial(A, binding)
        detail = self.store.staff_get(self.store.staff_list()['leads'][0]['id'])
        self.assertEqual(detail['trial_count'], 2)
        self.assertEqual([row['batch_id'] for row in detail['trials']],
                         ['BATCH-' + format(3, '032x'), 'BATCH-' + format(2, '032x')])
        self.assertEqual(self.store.profile(A)['revision'], 1)
        self.assertTrue(detail['can_contact'])


if __name__ == '__main__':
    unittest.main()
