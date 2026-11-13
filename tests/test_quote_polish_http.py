"""New export routes reuse saved quotes without writes or new CAD calls."""
import csv
import hashlib
import io
import unittest

import test_batch_quote_http as helper


class QuotePolishHTTPTests(unittest.TestCase):
    setUp = helper.BatchQuoteHTTPTests.setUp
    tearDown = helper.BatchQuoteHTTPTests.tearDown
    request = helper.BatchQuoteHTTPTests.request
    upload = helper.BatchQuoteHTTPTests.upload
    finish = helper.BatchQuoteHTTPTests.finish

    def test_summary_and_inline_print_artifacts_are_readonly_for_a_saved_run(self):
        batch = self.upload()[1]
        status, _, _ = self.request(self.api + '/' + batch['id'] + '/start', {
            'expected_revision': batch['revision'], 'request_id': 'polish-http-start',
            'settings': helper.SETTINGS})
        self.assertEqual(status, 202)
        state = self.finish(batch['id'])
        links = state['result']['links']
        self.assertTrue(links['summary_bom_url'].endswith('/bom-summary.csv'))
        self.assertTrue(links['print_url'].endswith('/customer-print.html'))
        folder = self.server.batch_quotes._folder(batch['id'])
        filenames = [folder / 'state.json', folder / 'runs' / (state['result']['run_id'] + '.json')]
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in filenames}
        raw_bom = self.request(links['bom_url'])[1]
        raw_json = self.request(links['json_url'])[1]
        calls = list(self.calls)
        code, summary, headers = self.request(links['summary_bom_url'])
        self.assertEqual(code, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/csv'))
        self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
        rows = list(csv.reader(io.StringIO(summary.decode('utf-8-sig'))))
        self.assertEqual(len(rows) - 1, len(state['result']['display_groups']))
        code, printed, headers = self.request(links['print_url'])
        self.assertEqual(code, 200)
        self.assertTrue(headers['Content-Type'].startswith('text/html'))
        self.assertTrue(headers['Content-Disposition'].startswith('inline;'))
        self.assertIn('@page', printed.decode())
        self.assertIn('待', printed.decode())
        self.assertEqual(self.request(links['bom_url'])[1], raw_bom)
        self.assertEqual(self.request(links['json_url'])[1], raw_json)
        self.assertEqual(self.calls, calls)
        self.assertEqual({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in filenames}, before)


if __name__ == '__main__':
    unittest.main()
