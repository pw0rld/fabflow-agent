"""Artifact preview pages are inline, while explicit downloads remain unchanged."""
import hashlib
import unittest

import test_batch_quote_http as helper


class ArtifactPreviewHTTPTests(unittest.TestCase):
    setUp = helper.BatchQuoteHTTPTests.setUp
    tearDown = helper.BatchQuoteHTTPTests.tearDown
    request = helper.BatchQuoteHTTPTests.request
    upload = helper.BatchQuoteHTTPTests.upload
    finish = helper.BatchQuoteHTTPTests.finish

    def test_each_preview_is_inline_and_retains_the_original_download(self):
        batch = self.upload()[1]
        status, _, _ = self.request(self.api + '/' + batch['id'] + '/start', {
            'expected_revision': batch['revision'], 'request_id': 'preview-http-start',
            'settings': helper.SETTINGS})
        self.assertEqual(status, 202)
        state = self.finish(batch['id'])
        links = state['result']['links']
        folder = self.server.batch_quotes._folder(batch['id'])
        paths = [folder / 'state.json', folder / 'runs' / (state['result']['run_id'] + '.json')]
        fingerprints = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
        calls = list(self.calls)
        for kind, link_key, download_key, filename in [
            ('summary', 'summary_bom_preview_url', 'summary_bom_url', 'bom-summary.csv'),
            ('source', 'bom_preview_url', 'bom_url', 'bom.csv'),
            ('cost', 'cost_preview_url', 'cost_url', 'cost.csv'),
            ('record', 'record_preview_url', 'json_url', 'quote.json'),
        ]:
            before = self.request(links[download_key])[1]
            code, content, headers = self.request(links[link_key])
            self.assertEqual(code, 200)
            self.assertTrue(headers['Content-Type'].startswith('text/html'))
            self.assertTrue(headers['Content-Disposition'].startswith('inline;'))
            text = content.decode('utf-8')
            self.assertIn('data-preview-kind="' + kind + '"', text)
            self.assertIn('data-preview-download', text)
            self.assertIn('href="' + filename + '"', text)
            self.assertEqual(self.request(links[download_key])[1], before)
        self.assertEqual(self.calls, calls)
        self.assertEqual({str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}, fingerprints)


if __name__ == '__main__':
    unittest.main()
