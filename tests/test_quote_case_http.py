"""Real HTTP isolation for protected case amendments; ZeroClave-only tests.

The labelled parser below is a test fixture, not a model-quality evaluation.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from workbench_store import Workbench
from workbench_server import make_server
from batch_quote_cases import API_PREFIX
from batch_quote_amendment import amend_quote
from prep_record import digest
from test_batch_quote_cases import seed_case, BATCH, RUN, RAW_CAD_NOTE, write_json
import test_batch_quote_http as fixture


class ProtectedCaseHTTPTests(unittest.TestCase):
    request = fixture.BatchQuoteHTTPTests.request

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hyl-private-http-')
        self.app = Workbench(Path(self.temporary.name) / 'tasks')
        self.server = make_server(self.app, 0)
        self.service = self.server.case_quotes
        self.original, self.cache, self.manifest = seed_case(self.service)
        self.folder = self.service._folder(BATCH)
        self.prefix = API_PREFIX + '/' + BATCH + '/runs/' + RUN
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.patch = {'changes': [{'field': 'order_sets', 'value': '20'}, {'field': 'rate_percent', 'value': '15'}]}
        self.parser_calls = []
        def parse(base, message):
            self.parser_calls.append((digest(base), message))
            return {'status': 'ready', 'message': '显式 HTTP 夹具：20套、成本加成15%。',
                    'patch': copy.deepcopy(self.patch), 'parser': {'kind': 'explicit_test_fixture', 'model_called': False}}
        self.service.assistant_parser = parse

    def tearDown(self):
        self.service.close()
        self.server.batch_quotes.close()
        self.server.orders.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def ready(self):
        status, response, _ = self.request(self.prefix + '/assistant/preview',
            {'event_id': 'protected-preview', 'message': '这单改成20套，成本加成15%。'})
        self.assertEqual(status, 202, response)
        proposal = response['proposal']
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status, response, _ = self.request(self.prefix + '/assistant/proposals/' + proposal['id'])
            self.assertEqual(status, 200, response)
            proposal = response['proposal']
            if proposal['status'] not in {'queued', 'running'}:
                self.assertEqual(proposal['status'], 'ready', proposal)
                return proposal
            time.sleep(.02)
        self.fail('Explicit private parser fixture timed out')

    def test_private_store_cannot_be_selected_via_public_path_query_or_header(self):
        public = '/workbench/api/quote-batches'
        self.assertEqual(self.request(public)[1]['batches'], [])
        self.assertEqual(self.request(public + '/' + BATCH)[0], 404)
        self.assertEqual(self.request(public + '?scope=poland')[0], 404)
        self.assertEqual(self.request(public, headers={'X-Quote-Scope': 'poland'})[1]['batches'], [])
        self.assertEqual(self.request(public + '/' + BATCH, headers={'X-Quote-Scope': 'poland'})[0], 404)
        self.assertEqual(self.request(API_PREFIX + '/case')[1]['entry_batch_id'], BATCH)
        self.assertEqual(self.request(API_PREFIX)[1]['batches'][0]['id'], BATCH)
        self.assertEqual(self.request(API_PREFIX + '?scope=public')[0], 404)
        self.assertEqual(self.request(API_PREFIX + '-other/' + BATCH)[0], 404)
        self.assertEqual(self.request(API_PREFIX + '/%2e%2e/' + BATCH)[0], 404)

    def test_private_read_exports_and_cached_previews_never_generate_public_data_links(self):
        with (patch.object(self.service, '_native', side_effect=AssertionError('No CAD')),
              patch.object(self.service, '_preview_file', side_effect=AssertionError('No CAD lookup'))):
            status, state, _ = self.request(API_PREFIX + '/' + BATCH)
            self.assertEqual(status, 200, state)
            for value in [state['result'], *state['runs']]:
                self.assertTrue(all(link.startswith(API_PREFIX + '/') for link in value['links'].values()))
            self.assertIn(RAW_CAD_NOTE, state['result']['lines'][0]['notes'])
            self.assertEqual(self.request(API_PREFIX + '/' + BATCH + '/files/F0001')[0], 200)
            parts = self.request(self.prefix + '/parts')[1]['lines']
            preview = self.request(parts[0]['geometry_url'])[1]
            self.assertEqual(preview['run_id'], RUN)
            self.assertEqual(preview['line_snapshot']['quantity_total'], 20)
            self.assertEqual(self.request(parts[0]['thumbnail_url'])[0], 200)
            for filename in ('quote.html', 'quote-print.html', 'preview-summary.html', 'engineering-bom.html',
                             'materials.html', 'processes.html', 'bom.csv', 'cost.csv', 'quote.json'):
                status, artifact, _ = self.request(self.prefix + '/' + filename)
                self.assertEqual(status, 200, (filename, artifact))
                if isinstance(artifact, bytes):
                    self.assertNotIn(b'href="/workbench/api/', artifact)
                    self.assertNotIn(b'src="/workbench/api/', artifact)
                elif filename == 'quote.json':
                    self.assertEqual(artifact, self.original)

    def test_private_print_and_data_preview_return_to_existing_quote_route(self):
        for name in ('quote-print.html', 'preview-summary.html', 'preview-source.html', 'preview-cost.html', 'preview-record.html'):
            status, content, _ = self.request(self.prefix + '/' + name)
            self.assertEqual(status, 200, name)
            self.assertIn(b'href="quote.html"', content, name)
            self.assertNotIn(b'href="quote-visual.html"', content, name)
        self.assertEqual(self.request(self.prefix + '/quote.html')[0], 200)

    def test_preview_commit_and_replay_only_change_private_store_reusing_geometry(self):
        public_root = self.server.batch_quotes.root
        public_before = {str(path.relative_to(public_root)): path.read_bytes()
                         for path in public_root.rglob('*') if path.is_file()}
        original_bytes = (self.folder / 'runs' / (RUN + '.json')).read_bytes()
        preview_bytes = {str(path.relative_to(self.cache)): path.read_bytes()
                         for path in self.cache.rglob('*') if path.is_file()}
        geometry_before = self.request(self.prefix + '/parts/L0001')[1]['geometry']
        with (patch.object(self.service, '_native', side_effect=AssertionError('No CAD')),
              patch.object(self.service, '_preview_file', side_effect=AssertionError('No CAD lookup')),
              patch.object(self.service.engineering_recognition, 'enqueue', side_effect=AssertionError('No drawing model'))):
            proposal = self.ready()
            self.assertEqual(self.service.get(BATCH)['result']['run_id'], RUN)
            self.assertEqual((self.folder / 'runs' / (RUN + '.json')).read_bytes(), original_bytes)
            expected = amend_quote(self.original, self.patch)['result']
            self.assertEqual(proposal['after_totals'], expected['totals'])
            payload = {'event_id': 'protected-commit', 'proposal_id': proposal['id'],
                       'expected_revision': proposal['expected_revision']}
            status, response, _ = self.request(self.prefix + '/assistant/commit', payload)
            self.assertEqual(status, 201, response)
            current = response['batch']['result']
            new_id = current['run_id']
            self.assertNotEqual(new_id, RUN)
            self.assertEqual(current['totals'], expected['totals'])
            self.assertEqual(current['settings']['order_sets'], 20)
            self.assertEqual(response['batch']['settings'], current['settings'])
            self.assertEqual(current['status'], 'pending_review')
            self.assertFalse(current['engineering_approved'])
            self.assertFalse(current['business_approved'])
            self.assertTrue(all(link.startswith(API_PREFIX + '/') for link in current['links'].values()))
            self.assertEqual(self.request(self.prefix + '/assistant/commit', payload)[1]['run_id'], new_id)
            new_prefix = API_PREFIX + '/' + BATCH + '/runs/' + new_id
            preview = self.request(new_prefix + '/parts/L0001')[1]
            self.assertEqual(preview['geometry'], geometry_before)
            self.assertEqual(preview['line_snapshot']['quantity_total'], 40)
            self.assertEqual(preview['line_snapshot']['total_price'], current['lines'][0]['total_price'])
            self.assertEqual(preview['order_sets'], 20)
            self.assertEqual(self.request(new_prefix + '/parts')[1]['run_id'], new_id)
            self.assertEqual(self.request(new_prefix + '/quote-print.html')[0], 200)
            self.assertEqual(self.request(new_prefix + '/engineering')[0], 200)
        recognition = json.loads((self.folder / 'runs' / (new_id + '-engineering-recognition.json')).read_text())
        self.assertEqual(recognition['ai']['lines']['L0001']['field_states']['material_grade'], 'candidate')
        evidence = recognition['ai']['lines']['L0001']['evidence']['material_grade'][0]
        self.assertEqual(evidence['status'], 'pending_review')
        self.assertEqual(len(self.parser_calls), 1)
        self.assertEqual((self.folder / 'runs' / (RUN + '.json')).read_bytes(), original_bytes)
        self.assertEqual({str(path.relative_to(self.cache)): path.read_bytes()
                          for path in self.cache.rglob('*') if path.is_file()}, preview_bytes)
        public_after = {str(path.relative_to(public_root)): path.read_bytes()
                        for path in public_root.rglob('*') if path.is_file()}
        self.assertEqual(public_after, public_before)
        self.assertEqual(self.request('/workbench/api/quote-batches')[1]['batches'], [])
        self.assertEqual(self.request('/workbench/api/quote-batches/' + BATCH + '/runs/' + new_id + '/quote.json')[0], 404)

    def test_only_assistant_and_manual_review_posts_are_allowed_and_origin_is_enforced(self):
        self.assertEqual(self.request(self.prefix + '/assistant/preview',
            {'event_id': 'no-marker', 'message': '20套'}, marker=False)[0], 403)
        self.assertEqual(self.request(self.prefix + '/assistant/preview',
            {'event_id': 'wrong-origin', 'message': '20套'}, headers={'Origin': 'https://other.invalid'})[0], 403)
        for endpoint in ('engineering-recognize', 'engineering-pricing'):
            self.assertEqual(self.request(self.prefix + '/' + endpoint, {'event_id': 'blocked'})[0], 405)
        for endpoint in ('start', 'retry'):
            self.assertEqual(self.request(API_PREFIX + '/' + BATCH + '/' + endpoint, {'request_id': 'blocked'})[0], 405)
        self.assertEqual(self.request(API_PREFIX, b'new-order', headers={'Content-Type': 'application/octet-stream'})[0], 405)
        status, response, _ = self.request(self.prefix + '/engineering',
            {'event_id': 'case-review', 'expected_revision': 0, 'review': {'header': {'product_name': '受保护案例工程备注'}}})
        self.assertEqual(status, 200, response)
        self.assertEqual(response['review']['header']['product_name'], '受保护案例工程备注')
        self.assertEqual(response['review']['status'], 'pending_review')
        self.assertEqual(self.service.get(BATCH)['result']['totals'], self.original['totals'])

    def test_old_engineering_price_source_link_is_suppressed_without_rewriting_provenance(self):
        # A migrated case may omit the earlier, unpriced source run.
        base = copy.deepcopy(self.original)
        base['engineering_repricing'] = {'base_run_id': 'RUN-' + 'a' * 32, 'requirements': [],
                                          'source_quote_digest': 'e' * 64}
        write_json(self.folder / 'runs' / (RUN + '.json'), base)
        state = json.loads((self.folder / 'state.json').read_text())
        state['result'] = copy.deepcopy(base)
        write_json(self.folder / 'state.json', state)
        manifest = copy.deepcopy(self.manifest)
        manifest['source_digest'] = digest(base)
        write_json(self.cache / 'manifest.json', manifest)
        record_path = self.folder / 'runs' / (RUN + '-engineering-recognition.json')
        record = json.loads(record_path.read_text())
        record['source_digest'] = digest(base)
        write_json(record_path, record)
        status, response, _ = self.request(self.prefix + '/engineering')
        self.assertEqual(status, 200, response)
        basis = response['manifest']['quote_basis']['engineering_repricing']
        self.assertIsNone(basis['source_run_id'])
        self.assertFalse(basis['source_quote_available'])
        self.assertEqual(basis['base_run_id'], 'RUN-' + 'a' * 32)
        self.assertEqual(json.loads((self.folder / 'runs' / (RUN + '.json')).read_text()), base)


if __name__ == '__main__':
    unittest.main()
