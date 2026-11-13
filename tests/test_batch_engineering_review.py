"""Engineering annotations persist independently of immutable financial records."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_engineering_review import read_review, save_review
from workbench_store import WorkbenchError
import test_batch_quote_http as http

RUN = 'RUN-' + 'a' * 32


class EngineeringReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'runs').mkdir()
        self.result = {'lines': [{'id': 'L0001', 'quantity_total': 10}, {'id': 'L0002', 'quantity_total': 20}],
                       'totals': {'total_price': '123.45'}}

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, review, revision=0, event='engineering-test'):
        return {'event_id': event, 'expected_revision': revision, 'review': review}

    def test_read_is_side_effect_free_and_saves_preserve_history_and_quote(self):
        saved = copy.deepcopy(self.result)
        before = list((self.root / 'runs').iterdir())
        self.assertEqual(read_review(self.root, RUN, self.result)['revision'], 0)
        self.assertEqual(list((self.root / 'runs').iterdir()), before)
        first = save_review(self.root, RUN, self.result, self.payload({
            'header': {'product_number': 'PRODUCT-001'},
            'lines': {'L0001': {'drawing_number': 'DRAW-001', 'thickness_mm': '1.005', 'material_grade': 'SUS304'}}}))
        self.assertEqual(first['revision'], 1)
        self.assertEqual(first['status'], 'pending_review')
        second = save_review(self.root, RUN, self.result, self.payload({
            'lines': {'L0002': {'parent_line_id': 'L0001'}}}, 1, 'engineering-second'))
        self.assertEqual(second['lines']['L0001']['thickness_mm'], '1.005')
        self.assertEqual(second['revision'], 2)
        record = json.loads((self.root / 'runs' / (RUN + '-engineering.json')).read_text())
        self.assertEqual([x['revision'] for x in record['history']], [0, 1])
        self.assertEqual(record['history'][1]['lines']['L0001']['drawing_number'], 'DRAW-001')
        self.assertEqual(self.result, saved)

    def test_idempotency_conflicts_and_source_binding(self):
        payload = self.payload({'header': {'bom_revision': 'A'}})
        first = save_review(self.root, RUN, self.result, payload)
        path = self.root / 'runs' / (RUN + '-engineering.json')
        before = path.read_bytes()
        self.assertEqual(save_review(self.root, RUN, self.result, payload), first)
        self.assertEqual(path.read_bytes(), before)
        for supplied, result in [({**payload, 'review': {'header': {'bom_revision': 'B'}}}, self.result),
                                 (self.payload({'header': {'bom_revision': 'B'}}, 0, 'new-event'), self.result),
                                 (self.payload({'header': {'bom_revision': 'B'}}, 1, 'other-source'), {**self.result, 'name': 'changed'})]:
            with self.assertRaises(WorkbenchError) as error:
                save_review(self.root, RUN, result, supplied)
            self.assertEqual(error.exception.status, 409)
        self.assertEqual(path.read_bytes(), before)

    def test_rejects_quantity_price_approval_changes_bad_units_and_cycles(self):
        for review in [
            {'lines': {'L0001': {'quantity_total': 5}}}, {'lines': {'L0001': {'unit_price': '0'}}},
            {'status': 'confirmed'}, {'header': {'base_quantity': 10}},
            {'lines': {'L0001': {'unit': 'kg'}}}, {'lines': {'L0001': {'thickness_mm': 'NaN'}}},
            {'lines': {'L0001': {'thickness_mm': '0'}}}, {'lines': {'L9999': {'revision': 'B'}}},
            {'lines': {'L0001': {'parent_line_id': 'L0002'}, 'L0002': {'parent_line_id': 'L0001'}}},
        ]:
            with self.subTest(review=review), self.assertRaises(WorkbenchError):
                save_review(self.root, RUN, self.result, self.payload(review))
        self.assertFalse((self.root / 'runs' / (RUN + '-engineering.json')).exists())


class EngineeringHTTPTests(unittest.TestCase):
    setUp = http.BatchQuoteHTTPTests.setUp
    tearDown = http.BatchQuoteHTTPTests.tearDown
    request = http.BatchQuoteHTTPTests.request
    upload = http.BatchQuoteHTTPTests.upload
    finish = http.BatchQuoteHTTPTests.finish

    def test_real_http_save_reload_artifacts_and_financial_immutability(self):
        batch = self.upload()[1]
        self.request(self.api + '/' + batch['id'] + '/start', {
            'expected_revision': batch['revision'], 'request_id': 'engineering-http-start', 'settings': http.SETTINGS})
        state = self.finish(batch['id'])
        run = state['result']['run_id']
        prefix = self.api + '/' + state['id'] + '/runs/' + run + '/'
        folder = self.server.batch_quotes._folder(batch['id'])
        originals = [folder / 'state.json', folder / 'runs' / (run + '.json')]
        fingerprints = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in originals}
        before_calls = list(self.calls)
        code, engineering, _ = self.request(prefix + 'engineering')
        self.assertEqual(code, 200)
        self.assertEqual(engineering['revision'], 0)
        self.assertIsNone(engineering['manifest']['lines'][0]['part_number'])
        payload = {'event_id': 'engineering-save', 'expected_revision': 0, 'review': {
            'header': {'product_number': 'HTTP-PRODUCT'},
            'lines': {'L0001': {'drawing_number': 'HTTP-DRAW', 'material_grade': 'SUS304', 'thickness_mm': '1.005'}}}}
        self.assertEqual(self.request(prefix + 'engineering', payload, marker=False)[0], 403)
        code, saved, _ = self.request(prefix + 'engineering', payload)
        self.assertEqual(code, 200, saved)
        self.assertEqual(saved['manifest']['lines'][0]['thickness_mm'], '1.005')
        self.assertEqual(saved['manifest']['lines'][0]['status'], 'pending_review')
        self.assertEqual(self.request(prefix + 'engineering')[1]['review'], saved['review'])
        self.assertEqual(self.request(prefix + 'engineering', payload)[1]['revision'], 1)
        self.assertEqual(self.request(prefix + 'engineering', {**payload, 'event_id': 'stale-event'})[0], 409)
        for name in ('engineering-bom.html', 'engineering-bom.csv', 'materials.html', 'processes.html'):
            code, content, headers = self.request(prefix + name)
            self.assertEqual(code, 200, name)
            self.assertEqual(headers['Content-Disposition'].startswith('attachment;'), name.endswith('.csv'))
            if name in ('engineering-bom.html', 'engineering-bom.csv'):
                self.assertIn('HTTP-DRAW', content.decode('utf-8-sig'))
        self.assertEqual(self.calls, before_calls)
        self.assertEqual({p: hashlib.sha256(p.read_bytes()).hexdigest() for p in originals}, fingerprints)


if __name__ == '__main__':
    unittest.main()
