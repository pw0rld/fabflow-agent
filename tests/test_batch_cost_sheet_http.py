"""Source-bound internal cost HTTP transactions; ZeroClave-only scalar tests."""
import copy
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from prep_record import digest
from batch_quote_cases import API_PREFIX as PRIVATE_API
import batch_quote_service as owner_module
import test_engineering_reprice_http as fixture
from test_batch_quote_cases import seed_case, BATCH as PRIVATE_BATCH, PNG


class BatchCostSheetHTTPTests(unittest.TestCase):
    request = fixture.EngineeringRepriceHTTPTests.request
    upload = fixture.EngineeringRepriceHTTPTests.upload
    setUp = fixture.EngineeringRepriceHTTPTests.setUp
    tree = fixture.EngineeringRepriceHTTPTests.tree

    def tearDown(self):
        self.server.case_quotes.close()
        fixture.EngineeringRepriceHTTPTests.tearDown(self)

    def sheet(self, prefix=None):
        status, response, _ = self.request((prefix or self.prefix) + '/cost-sheet')
        self.assertEqual(status, 200, response)
        return response

    def payload(self, event='cost-one', *, prefix=None, amount='60'):
        response = self.sheet(prefix)
        policy = copy.deepcopy(response['sheet']['policy'])
        policy['entries'].append({'id': 'manual-extra-1', 'line_id': None, 'category': 'other',
            'label': '显式合成整单费用', 'method': 'fixed', 'source_kind': 'manual_adopted', 'amount': amount})
        policy['customer_terms'] = {'delivery': '10个工作日', 'payment': '30%预付', 'validity': '7天', 'tax_shipping': '人民币未税，不含运费'}
        return {'event_id': event, 'expected_revision': response['expected_revision'], 'policy': policy}

    def post(self, payload, *, prefix=None, **kwargs):
        return self.request((prefix or self.prefix) + '/cost-sheet', payload, **kwargs)

    def exports(self):
        output = {}
        for name in ('quote.json', 'quote.html', 'bom.csv', 'cost.csv', 'internal-cost.html', 'internal-cost.csv'):
            status, data, _ = self.request(self.prefix + '/' + name)
            self.assertEqual(status, 200, (name, data))
            output[name] = data
        return output

    def assert_unit_prices(self, result):
        total = Decimal(0)
        for line in result['lines']:
            if not line['priced']:
                continue
            expected = (Decimal(line['total_cost']) / line['quantity_total'] * Decimal('1.20')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
            self.assertEqual(Decimal(line['unit_price']), expected)
            self.assertEqual(Decimal(line['total_price']), expected * line['quantity_total'])
            total += Decimal(line['total_price'])
        self.assertEqual(total, Decimal(result['totals']['total_price']))

    def assert_technical_only(self, value):
        if isinstance(value, dict):
            for key, child in value.items():
                self.assertNotIn(key, {'settings', 'notes', 'price_basis', 'material_assumption'})
                self.assertFalse(any(term in key.lower() for term in ('cost', 'price', 'profit', 'rate')), key)
                self.assert_technical_only(child)
        elif isinstance(value, list):
            for child in value:
                self.assert_technical_only(child)

    def assert_same_mesh(self, customer, internal):
        self.assertEqual(customer['source_sha256'], internal['source_sha256'])
        self.assertEqual(customer['output_unit'], internal['output_unit'])
        projected = {solid['id']: solid for solid in customer['solids']}
        self.assertEqual(set(projected), {solid['id'] for solid in internal['solids']})
        for solid in internal['solids']:
            self.assertEqual(projected[solid['id']]['mesh'], {key: solid['mesh'][key] for key in ('positions', 'indices', 'normals') if key in solid['mesh']})
            self.assertEqual(projected[solid['id']]['bounds_mm'], solid['bounds_mm'])

    def test_projection_is_read_only_and_bound_to_quote_review_and_recognition(self):
        before = self.tree()
        with (patch.object(self.service, '_native', side_effect=AssertionError('No CAD')),
              patch.object(self.service.engineering_recognition, 'enqueue', side_effect=AssertionError('No model'))):
            response = self.sheet()
        self.assertEqual(self.tree(), before)
        self.assertEqual(response['source_digest'], digest(self.original))
        self.assertEqual(response['sheet']['base_digest'], digest(self.original))
        self.assertEqual(response['sheet']['policy']['source_binding'], response['source_binding'])
        self.assertEqual(response['source_binding']['review_revision'], 2)
        self.assertEqual(response['sheet']['entries'], response['sheet']['policy']['entries'])
        self.assertTrue(response['can_reprice'])
        links = self.service.get(self.batch_id)['result']['links']
        for key, file in [('quote_url', 'customer-quote.html'), ('print_url', 'customer-print.html'),
                          ('internal_cost_url', 'internal-cost.html'), ('internal_cost_csv_url', 'internal-cost.csv')]:
            self.assertEqual(links[key], self.prefix + '/' + file)
        self.assertEqual(self.native_calls, ['F0001'])

    def test_explicit_manual_cost_saves_one_pending_run_and_replays_without_replacing_history(self):
        before, exports = self.tree(), self.exports()
        payload = self.payload()
        with (patch.object(self.service, '_native', side_effect=AssertionError('No CAD')),
              patch.object(self.service.engineering_recognition, 'enqueue', side_effect=AssertionError('No model'))):
            status, response, _ = self.post(payload)
        self.assertEqual(status, 201, response)
        result = response['batch']['result']
        self.assertNotEqual(result['run_id'], self.run_id)
        self.assertEqual(Decimal(result['totals']['total_cost']) - Decimal(self.original['totals']['total_cost']), Decimal('60'))
        self.assertEqual(result['totals']['other_cost'], '60.00')
        self.assert_unit_prices(result)
        self.assertEqual(result['settings'], self.original['settings'])
        self.assertEqual(response['batch']['settings'], result['settings'])
        self.assertEqual(result['status'], 'pending_review')
        self.assertTrue(result['simulated'])
        self.assertFalse(result['engineering_approved'])
        self.assertFalse(result['business_approved'])
        self.assertEqual(result['source_files'], self.original['source_files'])
        for old, new in zip(self.original['lines'], result['lines']):
            for key in ('id', 'file_id', 'native_definition', 'native_solid_count', 'volume_mm3', 'quantity_per_set', 'quantity_total', 'material_raw'):
                self.assertEqual(old[key], new[key], key)
        for name, content in before.items():
            if name != 'state.json':
                self.assertEqual((self.folder / name).read_bytes(), content, name)
        self.assertEqual(self.exports(), exports)
        status, again, _ = self.post(payload)
        self.assertEqual(status, 201, again)
        self.assertTrue(again['replayed'])
        self.assertEqual(again['run_id'], result['run_id'])
        self.assertEqual(len(again['batch']['runs']), 2)
        self.assertFalse(self.sheet()['can_reprice'])
        new_prefix = self.api + '/' + self.batch_id + '/runs/' + result['run_id']
        self.assertTrue(self.sheet(new_prefix)['can_reprice'])
        review = json.loads((self.folder / 'runs' / (result['run_id'] + '-engineering.json')).read_text())
        recognition = json.loads((self.folder / 'runs' / (result['run_id'] + '-engineering-recognition.json')).read_text())
        self.assertEqual((review['status'], review['revision']), ('pending_review', 0))
        self.assertEqual(recognition['ai']['lines']['L0001']['field_states']['material_grade'], 'candidate')
        self.assertEqual(review['source_digest'], digest(json.loads((self.folder / 'runs' / (result['run_id'] + '.json')).read_text())))

    def test_reopening_replaces_manual_amount_instead_of_adding_it_twice(self):
        status, first, _ = self.post(self.payload())
        self.assertEqual(status, 201, first)
        prefix = self.api + '/' + self.batch_id + '/runs/' + first['run_id']
        current = self.sheet(prefix)
        policy = copy.deepcopy(current['sheet']['policy'])
        next(item for item in policy['entries'] if item['id'] == 'manual-extra-1')['amount'] = '100'
        status, second, _ = self.post({'event_id': 'cost-two', 'expected_revision': current['expected_revision'], 'policy': policy}, prefix=prefix)
        self.assertEqual(status, 201, second)
        result = second['batch']['result']
        self.assertEqual(Decimal(result['totals']['total_cost']) - Decimal(first['batch']['result']['totals']['total_cost']), Decimal('40'))
        self.assertEqual(result['totals']['other_cost'], '100.00')
        self.assert_unit_prices(result)

    def test_bad_revision_binding_policy_origin_and_event_cannot_write_a_new_run(self):
        payload = self.payload()
        before = self.tree()
        wrong = copy.deepcopy(payload)
        wrong['expected_revision'] -= 1
        self.assertEqual(self.post(wrong)[0], 409)
        wrong = copy.deepcopy(payload)
        wrong['policy']['source_binding']['review_digest'] = '0' * 64
        self.assertEqual(self.post(wrong)[0], 409)
        wrong = copy.deepcopy(payload)
        wrong['policy']['entries'][-1]['amount'] = 'NaN'
        self.assertEqual(self.post(wrong)[0], 400)
        wrong = copy.deepcopy(payload)
        wrong['policy']['order_sets'] = 20
        self.assertEqual(self.post(wrong)[0], 400)
        self.assertEqual(self.post(payload, marker=False)[0], 403)
        self.assertEqual(self.post(payload, headers={'Origin': 'https://other.invalid'})[0], 403)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.post(payload)[0], 201)
        changed = copy.deepcopy(payload)
        changed['policy']['entries'][-1]['amount'] = '61'
        self.assertEqual(self.post(changed)[0], 409)
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 2)

    def test_review_or_recognition_change_invalidates_an_open_cost_sheet(self):
        payload = self.payload()
        self.service.save_engineering(self.batch_id, self.run_id, {'event_id': 'changed-cost-header', 'expected_revision': 2,
            'review': {'header': {'product_name': '用户新保存的核对文字'}}})
        self.assertEqual(self.post(payload)[0], 409)
        payload = self.payload(event='changed-recognition')
        path = self.folder / 'runs' / (self.run_id + '-engineering-recognition.json')
        record = json.loads(path.read_text())
        record['updated_at'] = '2026-11-09T00:01:00Z'
        path.write_text(json.dumps(record))
        self.assertEqual(self.post(payload)[0], 409)
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 1)

    def test_prepared_snapshot_failure_recovers_same_run_and_fences_competing_writes(self):
        payload = self.payload(event='recover-cost')
        old_bytes = (self.folder / 'runs' / (self.run_id + '.json')).read_bytes()
        with patch.object(self.service, '_complete', side_effect=OSError('explicit write-failure fixture')):
            status, response, _ = self.post(payload)
        self.assertEqual(status, 503, response)
        records = self.service.cost_sheet._records(self.batch_id)
        self.assertEqual(len(records), 1)
        record = records[0][1]
        self.assertEqual(record['status'], 'prepared')
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 1)
        self.assertFalse(self.sheet()['can_reprice'])
        path = self.folder / 'runs' / (self.run_id + '-engineering.json')
        review_before = path.read_bytes()
        status, response, _ = self.request(self.prefix + '/engineering', {'event_id': 'blocked-review', 'expected_revision': 2,
            'review': {'header': {'product_name': '不能跨过待恢复核价'}}})
        self.assertEqual(status, 409, response)
        self.assertEqual(path.read_bytes(), review_before)
        self.assertTrue(self.service.cost_sheet.process_pending_once())
        recovered = self.service.get(self.batch_id)
        self.assertEqual(recovered['result']['run_id'], record['run_id'])
        self.assertEqual(len(recovered['runs']), 2)
        status, replay, _ = self.post(payload)
        self.assertEqual(status, 201, replay)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['run_id'], record['run_id'])
        self.assertEqual((self.folder / 'runs' / (self.run_id + '.json')).read_bytes(), old_bytes)
        self.assertEqual(self.native_calls, ['F0001'])

    def test_lost_response_after_state_commit_cannot_duplicate_the_version(self):
        payload = self.payload(event='lost-cost-response')
        real_write = owner_module.atomic_write
        def fail_committed_journal(path, content):
            if Path(path).parent.name == 'cost-sheet-pricing' and json.loads(content).get('status') == 'committed':
                raise OSError('explicit lost-response fixture')
            return real_write(path, content)
        with patch.object(owner_module, 'atomic_write', side_effect=fail_committed_journal):
            status, response, _ = self.post(payload)
        self.assertEqual(status, 503, response)
        current = self.service.get(self.batch_id)
        self.assertEqual(len(current['runs']), 2)
        run_id = current['result']['run_id']
        self.assertEqual(self.service.cost_sheet._records(self.batch_id)[0][1]['status'], 'prepared')
        self.assertTrue(self.service.cost_sheet.process_pending_once())
        status, replay, _ = self.post(payload)
        self.assertEqual(status, 201, replay)
        self.assertEqual(replay['run_id'], run_id)
        self.assertTrue(replay['replayed'])
        self.assertEqual(len(replay['batch']['runs']), 2)

    def test_cost_sheet_versions_block_simplified_repricing_but_allow_engineering_notes(self):
        status, response, _ = self.post(self.payload())
        self.assertEqual(status, 201, response)
        current = response['batch']
        prefix = self.api + '/' + self.batch_id + '/runs/' + response['run_id']
        for endpoint, payload in [('start', {'request_id': 'old-start', 'expected_revision': current['revision'], 'settings': current['settings']}),
                                  ('retry', {'request_id': 'old-retry', 'expected_revision': current['revision'], 'file_ids': ['F0001']})]:
            status, result, _ = self.request(self.api + '/' + self.batch_id + '/' + endpoint, payload)
            self.assertEqual(status, 409, result)
            self.assertEqual(result['error']['code'], 'cost_sheet_required')
        self.assertFalse(self.request(prefix + '/engineering-pricing')[1]['can_reprice'])
        status, result, _ = self.request(prefix + '/engineering-pricing', {'event_id': 'old-engineering', 'expected_revision': current['revision'], 'policy': {}})
        self.assertEqual(status, 409, result)
        self.assertEqual(result['error']['code'], 'cost_sheet_required')
        status, result, _ = self.request(prefix + '/assistant/preview', {'event_id': 'old-assistant', 'message': '这单改为20套'})
        self.assertEqual(status, 409, result)
        self.assertEqual(len(self.service.get(self.batch_id)['runs']), 2)
        status, result, _ = self.request(prefix + '/engineering', {'event_id': 'cost-note', 'expected_revision': 0,
            'review': {'header': {'basis_notes': '只补记工程说明，不重算金额'}}})
        self.assertEqual(status, 200, result)
        self.assertEqual(result['review']['status'], 'pending_review')

    def test_private_cost_sheet_keeps_cached_geometry_and_never_leaks_to_public_store(self):
        private = self.server.case_quotes
        original, cache, _ = seed_case(private)
        prefix = PRIVATE_API + '/' + PRIVATE_BATCH + '/runs/' + original['run_id']
        public_before = self.service.get(self.batch_id)
        cache_before = {str(path.relative_to(cache)): path.read_bytes() for path in cache.rglob('*') if path.is_file()}
        self.assertEqual(self.request(self.api + '/' + PRIVATE_BATCH)[0], 404)
        payload = self.payload(event='private-cost', prefix=prefix)
        with (patch.object(private, '_native', side_effect=AssertionError('No CAD')),
              patch.object(private.engineering_recognition, 'enqueue', side_effect=AssertionError('No model'))):
            status, response, _ = self.post(payload, prefix=prefix)
        self.assertEqual(status, 201, response)
        result = response['batch']['result']
        self.assertTrue(all(url.startswith(PRIVATE_API + '/') for url in result['links'].values()))
        self.assertEqual(self.service.get(self.batch_id), public_before)
        self.assertEqual(self.request(self.api + '/' + PRIVATE_BATCH + '/runs/' + result['run_id'] + '/cost-sheet')[0], 404)
        self.assertNotIn(PRIVATE_BATCH, [row['id'] for row in self.request(self.api)[1]['batches']])
        self.assertEqual({str(path.relative_to(cache)): path.read_bytes() for path in cache.rglob('*') if path.is_file()}, cache_before)
        new_prefix = PRIVATE_API + '/' + PRIVATE_BATCH + '/runs/' + result['run_id']
        self.assertEqual(self.request(new_prefix + '/parts/L0001')[0], 200)
        for name in ('internal-cost.html', 'internal-cost.csv', 'customer-quote.html', 'customer-print.html'):
            status, body, _ = self.request(new_prefix + '/' + name)
            self.assertEqual(status, 200, (name, body))
            self.assertNotIn(b'href="/workbench/api/quote-batches/', body)

    def test_customer_part_public_http_projects_financials_without_changing_internal_payload(self):
        # The geometry bytes are the existing labelled cached-mesh fixture;
        # the public route's native reader is stubbed, never a CAD executable.
        private = self.server.case_quotes
        original, _, _ = seed_case(private)
        raw = private.part_geometry(PRIVATE_BATCH, original['run_id'], 'L0001')
        raw.update(batch_id=self.batch_id, run_id=self.run_id, settings=copy.deepcopy(self.original['settings']),
                   line_snapshot=copy.deepcopy(self.original['lines'][0]), price_basis='internal fixture', notes=['内部核价记录'])
        before = copy.deepcopy(raw)
        with (patch.object(self.service, 'part_geometry', return_value=raw),
              patch.object(self.service, 'part_thumbnail', return_value=(PNG, 'image/png', 'fixture.png'))):
            status, customer, _ = self.request(self.prefix + '/customer-parts/L0001')
            self.assertEqual(status, 200, customer)
            self.assert_technical_only(customer)
            self.assert_same_mesh(customer['geometry'], raw['geometry'])
            self.assertEqual(customer['groups'], raw['groups'])
            self.assertEqual(customer['line_id'], 'L0001')
            self.assertEqual(self.request(self.prefix + '/customer-parts/L0001/thumbnail')[1], PNG)
            internal = self.request(self.prefix + '/parts/L0001')[1]
            self.assertEqual(internal['line_snapshot']['total_cost'], self.original['lines'][0]['total_cost'])
            self.assertEqual(internal['settings']['rate_percent'], '20')
            self.assertEqual(raw, before)

    def test_customer_part_private_http_reuses_bound_native_geometry_and_stays_out_of_public_store(self):
        private = self.server.case_quotes
        original, cache, _ = seed_case(private)
        prefix = PRIVATE_API + '/' + PRIVATE_BATCH + '/runs/' + original['run_id']
        old_files = {str(path.relative_to(cache)): path.read_bytes() for path in cache.rglob('*') if path.is_file()}
        with patch.object(private, '_native', side_effect=AssertionError('No CAD')):
            status, customer, _ = self.request(prefix + '/customer-parts/L0001')
            self.assertEqual(status, 200, customer)
            self.assert_technical_only(customer)
            internal = self.request(prefix + '/parts/L0001')[1]
            self.assert_same_mesh(customer['geometry'], internal['geometry'])
            self.assertEqual(customer['solid_ids'], internal['solid_ids'])
            self.assertIn('total_cost', internal['line_snapshot'])
            self.assertIn('settings', internal)
            self.assertEqual(self.request(prefix + '/customer-parts/L0001/thumbnail')[1], PNG)
        self.assertEqual(self.request(self.api + '/' + PRIVATE_BATCH + '/runs/' + original['run_id'] + '/customer-parts/L0001')[0], 404)
        self.assertEqual({str(path.relative_to(cache)): path.read_bytes() for path in cache.rglob('*') if path.is_file()}, old_files)


if __name__ == '__main__':
    unittest.main()
