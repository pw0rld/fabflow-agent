"""Quotation preview links are presentation only; money and saved JSON stay fixed."""
import copy
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_costing import calculate_batch, render_artifact
from test_batch_quote_costing import document, settings, solid


class Links(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.links, self.images = [], []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'a' and values.get('class') == 'part-preview-link':
            self.links.append(values['href'])
        if tag == 'img':
            self.images.append(values)


class BatchPartHTMLTests(unittest.TestCase):
    def setUp(self):
        self.result = calculate_batch([document([solid(1)])], settings())
        self.line = self.result['lines'][0]['id']
        self.url = '/workbench/api/quote-batches/BATCH-' + 'a' * 32 + '/runs/RUN-' + 'b' * 32 + '/parts/' + self.line

    def row(self, url=None):
        return {'line_id': self.line, 'status': 'ready', 'geometry_url': url or self.url,
                'thumbnail_url': (url or self.url) + '/thumbnail'}

    def test_valid_links_and_png_thumbnails_do_not_change_quote_money_json_or_csv(self):
        original = copy.deepcopy(self.result)
        before = {name: render_artifact(self.result, name)[0] for name in ('quote.json', 'bom.csv', 'cost.csv')}
        page = render_artifact(self.result, 'quote.html', part_previews=[self.row()])[0].decode()
        links = Links(page)
        self.assertEqual(len(links.links), 1)
        self.assertEqual(parse_qs(urlsplit(links.links[0]).query)['preview'], [self.url])
        self.assertEqual(links.images[0]['src'], self.url + '/thumbnail')
        self.assertEqual(links.images[0]['loading'], 'lazy')
        self.assertIn('零件预览', page)
        self.assertIn(self.result['totals']['total_price'], page)
        self.assertEqual(self.result, original)
        for name, expected in before.items():
            self.assertEqual(render_artifact(self.result, name, part_previews=[self.row()])[0], expected)

    def test_static_protected_case_links_reuse_the_same_viewer_and_source(self):
        url = '/ortho2cad-test/customer-cases/public-demo-case/parts/' + self.line + '.json'
        row = self.row(url)
        row['thumbnail_url'] = url[:-5] + '/thumbnail.png'
        page = Links(render_artifact(self.result, 'quote.html', part_previews=[row])[0].decode())
        self.assertEqual(parse_qs(urlsplit(page.links[0]).query)['preview'], [url])
        self.assertEqual(page.images[0]['src'], row['thumbnail_url'])

    def test_missing_preview_does_not_invent_image_or_hide_unpriced_rows(self):
        self.result['lines'][0].update(priced=False, total_price=None, unit_price=None)
        page = render_artifact(self.result, 'quote.html', part_previews=[])[0].decode()
        self.assertIn(self.result['lines'][0]['part_name'], page)
        self.assertIn('暂无预览', page)
        self.assertIn('未计价，不代表免费', page)
        self.assertEqual(Links(page).images, [])
        legacy = render_artifact(self.result, 'quote.html')[0].decode()
        self.assertNotIn('<th>零件预览</th>', legacy)

    def test_untrusted_or_cross_line_urls_never_become_image_or_viewer_requests(self):
        for url in ('https://example.com/private.json', '//example.com/private.json', 'javascript:alert(1)',
                    '/workbench/api/quote-batches/../secret', self.url + '/../../secret',
                    self.url[:-1] + '9'):
            with self.subTest(url=url):
                page = Links(render_artifact(self.result, 'quote.html', part_previews=[self.row(url)])[0].decode())
                self.assertEqual(page.links, [])
                self.assertEqual(page.images, [])
        row = self.row()
        row['thumbnail_url'] = 'https://example.com/tracker.png'
        page = Links(render_artifact(self.result, 'quote.html', part_previews=[row])[0].decode())
        self.assertEqual(len(page.links), 1)
        self.assertEqual(page.images, [])


if __name__ == '__main__':
    unittest.main()
