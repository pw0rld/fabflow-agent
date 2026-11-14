"""Trial contact/application routes; synthetic records, no SMS/CAD/model use."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
from deploy.trial_leads_access_config import append_leads_access, leads_access_fragment
from workbench_server import WorkbenchHandler, make_server, TRIAL_STAFF_API
from workbench_store import Workbench, WorkbenchError

API = '/workbench/customer/api'
BATCH, RUN = 'BATCH-' + 'a' * 32, 'RUN-' + 'b' * 32
PROFILE = {'event_id': 'profile-one', 'expected_revision': 0, 'company': 'Synthetic Fabrication',
           'contact_name': '', 'contact_method': 'phone', 'contact_value': '13800000000',
           'manufacturing_type': 'sheet_metal', 'contact_consent': False}


class TrialLeadsFragmentTests(unittest.TestCase):
    def test_contact_page_and_management_paths_are_authenticated(self):
        fragment = leads_access_fragment()
        for route in ('^~ /workbench/leads/', '= ' + TRIAL_STAFF_API, '^~ ' + TRIAL_STAFF_API + '/'):
            self.assertIn('location ' + route + ' {', fragment)
        self.assertEqual(fragment.count('auth_basic "ZeroClave DSH";'), 3)
        self.assertEqual(fragment.count('proxy_set_header X-FabFlow-Staff-User $remote_user;'), 3)
        self.assertNotIn('error_page 401', fragment)
        previous = 'location = /try/ { return 200; }\n'
        result = append_leads_access(previous)
        self.assertTrue(result.startswith(previous.rstrip()))
        self.assertEqual(append_leads_access(result), result)
        with self.assertRaises(ValueError):
            append_leads_access('location /workbench/leads/ { return 200; }')
        with self.assertRaises(ValueError):
            leads_access_fragment('http://outside.invalid:3092')


class TrialLeadsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='trial-leads-http-')
        self.server = make_server(Workbench(Path(self.temp.name) / 'tasks'), 0, trial_flow_enabled=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        for name in ('customer_batches', 'batch_quotes', 'case_quotes', 'orders'):
            getattr(self.server, name).close()
        self.temp.cleanup()

    def request(self, path, cookie='', payload=None):
        headers = {'X-Workbench-Request': '1', 'Cookie': cookie}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        request = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                          headers=headers)
        try:
            response = self.opener.open(request, timeout=8)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read()), dict(response.headers)

    def session(self):
        status, _, headers = self.request(API + '/session', payload={})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';', 1)[0]

    def save(self, cookie, **changes):
        status, result, _ = self.request(API + '/trial/profile', cookie, {**PROFILE, **changes})
        self.assertEqual(status, 200, result)
        return result['profile']

    def test_private_profile_stays_unverified_and_isolated(self):
        self.assertEqual(self.request(API + '/trial/profile')[0], 401)
        cookie, other = self.session(), self.session()
        status, result, _ = self.request(API + '/trial/profile', cookie)
        self.assertEqual((status, result['enabled'], result['verification_available'], result['profile']),
                         (200, True, False, None))
        profile = self.save(cookie)
        self.assertEqual(profile['verification_status'], 'unverified')
        self.assertFalse(profile['contact_consent'])
        self.assertIsNone(self.request(API + '/trial/profile', other)[1]['profile'])
        self.assertEqual(self.request(API + '/trial/profile', cookie,
            {**PROFILE, 'verification_status': 'verified'})[0], 400)

    def test_profile_is_checked_before_upload_body_without_blocking_saved_reads(self):
        cookie = self.session()
        handler = WorkbenchHandler.__new__(WorkbenchHandler)
        handler.server = self.server
        handler.headers = {'Cookie': cookie, 'Content-Type': 'application/octet-stream'}
        handler._body = Mock(side_effect=AssertionError('Unprofiled request consumed upload body'))
        with self.assertRaises(WorkbenchError) as error:
            handler._customer_route(urlsplit(API + '/orders?name=synthetic.step'), post=True)
        self.assertEqual(error.exception.status, 403)
        handler._body.assert_not_called()
        with patch.object(self.server.customer_upload, 'list', return_value={'orders': []}):
            self.assertEqual(self.request(API + '/orders', cookie)[0], 200)
        for action in ('start', 'retry'):
            with patch.object(self.server.customer_upload, action, side_effect=AssertionError('Work submitted')):
                self.assertEqual(self.request(API + '/orders/' + BATCH + '/' + action, cookie, {})[0], 403)

    def test_interest_replays_owned_snapshot_without_upload_or_paid_work(self):
        cookie = self.session(); self.save(cookie)
        payload = {'event_id': 'interest-one', 'interest': 'factory_trial', 'note': 'Synthetic request',
                   'contact_consent': True, 'batch_id': BATCH, 'run_id': RUN}
        state = {'id': BATCH, 'name': 'synthetic.step', 'status': 'completed', 'runs': [{'id': RUN}],
                 'result': {'run_id': RUN, 'lines': [{}, {}]}}
        portal = self.server.customer_upload
        with patch.object(portal, '_owned', return_value=state), \
             patch.object(portal, 'upload', side_effect=AssertionError('Interest uploaded a file')), \
             patch.object(portal, 'start', side_effect=AssertionError('Interest started work')):
            first = self.request(API + '/trial/applications', cookie, payload)
            second = self.request(API + '/trial/applications', cookie, payload)
        self.assertEqual(first[0], 201, first[1]); self.assertEqual(second[0], 201, second[1])
        self.assertEqual(first[1]['application']['id'], second[1]['application']['id'])
        self.assertTrue(second[1]['replayed'])
        result = self.request(API + '/trial/applications', cookie)[1]
        self.assertEqual(len(result['applications']), 1)
        self.assertIn(BATCH, json.dumps(result)); self.assertIn('synthetic.step', json.dumps(result))
        self.assertNotIn('material_cost', json.dumps(result))
        self.assertEqual(self.request(API + '/trial/applications', self.session())[1], {'applications': []})

    def test_foreign_or_client_invented_binding_is_not_saved(self):
        cookie = self.session(); self.save(cookie)
        payload = {'event_id': 'interest-denied', 'interest': 'consultation', 'note': '',
                   'contact_consent': False, 'batch_id': BATCH, 'run_id': RUN}
        with patch.object(self.server.customer_upload, '_owned',
                          side_effect=WorkbenchError('订单不存在。', 'not_found', 404)):
            self.assertEqual(self.request(API + '/trial/applications', cookie, payload)[0], 404)
        injected = {key: value for key, value in payload.items() if key not in {'batch_id', 'run_id'}}
        injected['order_binding'] = {'path': '/private/customer.step'}
        self.assertEqual(self.request(API + '/trial/applications', cookie, injected)[0], 400)
        self.assertEqual(self.request(API + '/trial/applications', cookie)[1], {'applications': []})

    def test_sales_contact_permission_is_separate_from_phone_verification(self):
        cookie = self.session(); profile = self.save(cookie)
        status, listed, _ = self.request(TRIAL_STAFF_API)
        self.assertEqual(status, 200, listed); self.assertEqual(listed['total'], 1)
        lead_id = listed['leads'][0]['id']
        detail = self.request(TRIAL_STAFF_API + '/' + lead_id)[1]
        self.assertNotIn(PROFILE['contact_value'], json.dumps(detail))
        self.assertEqual(self.request(TRIAL_STAFF_API + '?contactable=1')[1]['total'], 0)
        self.save(cookie, event_id='consent-yes', expected_revision=profile['revision'], contact_consent=True)
        self.assertEqual(self.request(TRIAL_STAFF_API + '?contactable=1')[1]['total'], 1)
        self.assertEqual(self.request(API + '/trial/profile', cookie)[1]['profile']['verification_status'], 'unverified')

    def test_existing_authorized_results_link_to_lead_without_sales_application(self):
        cookie = self.session(); profile = self.save(cookie)
        order = {'id': BATCH, 'name': 'synthetic.step', 'status': 'completed',
                 'quote': {'run_id': RUN, 'quoted_lines': 2, 'unpriced_lines': 1,
                           'weight_summary': {'line_count': 4},
                           'total_price': '120.00', 'material_cost': 'private-cost'}}
        with patch.object(self.server.customer_upload, 'get', return_value=order):
            self.assertEqual(self.request(API + '/orders/' + BATCH, cookie)[0], 200)
            self.assertEqual(self.request(API + '/orders/' + BATCH, cookie)[0], 200)
        summary = self.request(TRIAL_STAFF_API)[1]['leads'][0]
        self.assertEqual(summary['trial_count'], 1)
        self.assertEqual(summary['application_count'], 0)
        self.assertFalse(summary['can_contact'])
        trial = self.request(TRIAL_STAFF_API + '/' + summary['id'])[1]['lead']['trials'][0]
        self.assertEqual((trial['batch_id'], trial['run_id'], trial['part_count']), (BATCH, RUN, 4))
        self.assertNotIn('private-cost', json.dumps(trial))
        self.assertNotIn('total_price', json.dumps(trial))
        self.assertEqual(self.request(API + '/trial/profile', cookie)[1]['profile']['revision'], profile['revision'])

    def test_old_order_reads_without_profile_do_not_create_leads(self):
        cookie = self.session()
        order = {'id': BATCH, 'name': 'synthetic.step', 'status': 'uploaded', 'quote': None}
        with patch.object(self.server.customer_upload, 'get', return_value=order):
            self.assertEqual(self.request(API + '/orders/' + BATCH, cookie)[0], 200)
        self.assertEqual(self.request(TRIAL_STAFF_API)[1]['total'], 0)

    def test_trial_telemetry_failure_preserves_successful_order_response(self):
        cookie = self.session(); self.save(cookie)
        order = {'id': BATCH, 'name': 'synthetic.step', 'status': 'uploaded', 'quote': None}
        with patch.object(self.server.customer_upload, 'get', return_value=order), \
             patch.object(self.server.trial_leads, 'record_trial', side_effect=WorkbenchError(
                 'Synthetic storage failure', 'trial_store_unavailable', 503)):
            status, result, _ = self.request(API + '/orders/' + BATCH, cookie)
        self.assertEqual(status, 200)
        self.assertEqual(result, order)


if __name__ == '__main__':
    unittest.main()
