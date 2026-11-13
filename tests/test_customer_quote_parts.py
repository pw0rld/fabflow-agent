"""Customer quote parts reuse owner-bound cached assets and saved prices.

Run on ZeroClave only. Protocol fixtures are synthetic; no local runtime,
native CAD processing, model calls, or production-order writes are required.
"""
import copy
from html.parser import HTMLParser
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from batch_customer_quote import render_customer_quote
from batch_quote_costing import calculate_batch, render_artifact, aggregate_display_groups
from test_batch_process_quote import flat, bent, process_settings, document
from test_batch_quote_presentation import example
import test_customer_process_http as fixtures

BATCH = 'BATCH-' + 'a'*32
RUN = 'RUN-' + 'b'*32


class Tags(HTMLParser):
    def __init__(self, content):
        super().__init__(); self.images=[]; self.links=[]; self.totals=[]; self.feed(content)
    def handle_starttag(self, tag, attrs):
        value=dict(attrs)
        if tag=='img': self.images.append(value)
        if tag=='a': self.links.append(value)
        if 'data-total-price' in value: self.totals.append(value['data-total-price'])


def result_and_parts(mixed=True):
    result=calculate_batch([document([flat(), bent()] if mixed else [flat()])], process_settings())
    result.update(batch_id=BATCH, run_id=RUN, name='Customer assembly')
    prefix='/workbench/customer/api/orders/'+BATCH+'/runs/'+RUN+'/'
    parts=[]
    for index,line in enumerate(result['lines']):
        geometry=prefix+'customer-parts/'+line['id']
        drawing=prefix+'manufacturing/'+line['id']+'/'
        parts.append({'id':line['id'], 'geometry_url':geometry, 'thumbnail_url':geometry+'/thumbnail',
            'laser':{'status':'ready' if index==0 else 'unavailable',
                     'svg_url':drawing+'laser.svg' if index==0 else None,
                     'dxf_url':drawing+'laser.dxf' if index==0 else None}})
    return result,parts


class CustomerQuotePartRenderTests(unittest.TestCase):
    def test_customer_quote_contains_each_native_thumbnail_and_exact_part_links(self):
        result,parts=result_and_parts(); original=copy.deepcopy(result)
        page=render_customer_quote(result,customer_parts=parts).decode(); tags=Tags(page)
        self.assertEqual(len(tags.images),2); self.assertIn('拆件预览',page)
        for part in parts:
            identity=part['id']
            image=next(row for row in tags.images if row.get('data-customer-part-id')==identity)
            self.assertEqual(image['src'],part['thumbnail_url'])
            viewer=next(row for row in tags.links if 'data-customer-3d-link' in row and row['data-customer-part-id']==identity)
            self.assertEqual(urlsplit(viewer['href']).path,'/workbench/quotation/part/')
            self.assertEqual(parse_qs(urlsplit(viewer['href']).query),{'customer':['1'],'preview':[part['geometry_url']]})
            link=next(row for row in tags.links if 'data-customer-process-link' in row and row['data-customer-part-id']==identity)
            self.assertEqual(urlsplit(link['href']).path,'/workbench/customer/part/')
            self.assertEqual(parse_qs(urlsplit(link['href']).query),{'order':[BATCH],'run':[RUN],'part':[identity]})
        self.assertEqual(result,original)

    def test_missing_bent_blank_has_no_fake_drawing_and_no_full_total(self):
        result,parts=result_and_parts(); page=render_customer_quote(result,customer_parts=parts).decode(); tags=Tags(page)
        self.assertEqual(tags.totals,['']); self.assertIn('待工程核价',page)
        self.assertIn('不能作为整单报价',page)
        dxf=[row for row in tags.links if 'data-customer-dxf-link' in row]
        self.assertEqual(len(dxf),1); self.assertEqual(dxf[0]['data-customer-part-id'],parts[0]['id'])
        for internal in ('rate_cny','amount_cny','成本汇总','内部核价','加成比例','quote.json','cost.csv','/ortho2cad-test/'):
            self.assertNotIn(internal,page)

    def test_cross_order_external_and_forged_asset_urls_are_not_emitted(self):
        result,parts=result_and_parts(False)
        parts[0].update(geometry_url='https://outside.invalid/geometry',thumbnail_url=parts[0]['thumbnail_url'].replace(BATCH,'BATCH-'+'c'*32))
        parts[0]['laser'].update(svg_url='javascript:alert(1)',dxf_url='/ortho2cad-test/private.dxf')
        page=render_customer_quote(result,customer_parts=parts).decode(); tags=Tags(page)
        self.assertEqual(tags.images,[]); self.assertIn('暂无原生预览',page)
        self.assertFalse(any('data-customer-3d-link' in row or 'data-customer-dxf-link' in row or 'data-customer-svg-link' in row for row in tags.links))
        for item in ('outside.invalid','javascript:','private.dxf','BATCH-'+'c'*32):self.assertNotIn(item,page)
        process=next(row for row in tags.links if 'data-customer-process-link' in row)
        self.assertEqual(parse_qs(urlsplit(process['href']).query)['order'],[BATCH])

    def test_print_keeps_eager_thumbnails_and_saved_sale_amount(self):
        result,parts=result_and_parts(False)
        online=Tags(render_customer_quote(result,customer_parts=parts).decode())
        page=render_customer_quote(result,customer_parts=parts,printable=True).decode(); printed=Tags(page)
        self.assertEqual(online.totals,printed.totals); self.assertEqual(printed.totals,[result['totals']['total_price']])
        self.assertEqual([row['src'] for row in online.images],[row['src'] for row in printed.images])
        self.assertTrue(all(row.get('loading')=='eager' for row in printed.images))
        self.assertIn('.customer-print .customer-part-actions{display:none!important}',page)
        self.assertIn('激光切割',page)

    def test_legacy_documents_and_financial_exports_are_unchanged_without_new_projection(self):
        result=example(); before=copy.deepcopy(result)
        documents={name:render_artifact(result,name)[0] for name in ('quote.json','bom.csv','cost.csv')}
        page=render_customer_quote(result).decode()
        self.assertNotIn('data-customer-3d-link',page); self.assertNotIn('拆件预览',page)
        self.assertEqual(documents,{name:render_artifact(result,name)[0] for name in documents})
        self.assertEqual(result,before)

    def test_aggregated_row_preserves_members_and_exposes_each_source_part(self):
        result=example();result.update(batch_id=BATCH,run_id=RUN)
        prefix='/workbench/customer/api/orders/'+BATCH+'/runs/'+RUN+'/'
        parts=[{'id':line['id'],'geometry_url':prefix+'customer-parts/'+line['id'],
                'thumbnail_url':prefix+'customer-parts/'+line['id']+'/thumbnail','laser':{'status':'unavailable'}} for line in result['lines']]
        page=render_customer_quote(result,customer_parts=parts).decode(); tags=Tags(page)
        groups=aggregate_display_groups(result)
        self.assertEqual(len(tags.images),len(groups))
        self.assertIn('条来源',page)
        for group in groups:
            for identity in group['member_ids']:
                self.assertTrue(any(parse_qs(urlsplit(link.get('href','')).query).get('part')==[identity] for link in tags.links))


class CustomerQuotePartHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.CustomerProcessHTTPTests(methodName='test_bent_part_does_not_offer_flat_pattern_or_full_quotation')
        self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.fixture.mixed=True

    def test_existing_saved_order_gains_quote_previews_without_reprice_and_enforces_owner(self):
        f=self.fixture; owner,outsider=f.session(),f.session();order=f.finished(owner)
        folder=f.server.customer_batches._folder(order['id'])
        before={path.relative_to(folder).as_posix():path.read_bytes() for path in folder.rglob('*') if path.is_file()}
        with patch.object(f.server.customer_batches,'_native',side_effect=AssertionError('No CAD rerun while viewing a quote')):
            for key in ('quote_url','print_url'):
                status,content=f.request(order['quote'][key],cookie=owner)
                self.assertEqual(status,200,content);page=content.decode();tags=Tags(page)
                self.assertEqual(len(tags.images),2);self.assertEqual(tags.totals,[''])
                self.assertEqual(f.request(order['quote'][key],cookie=outsider)[0],404)
                self.assertEqual(f.request(order['quote'][key])[0],401)
                for image in tags.images:
                    self.assertEqual(f.request(image['src'],cookie=owner)[0],200)
                    self.assertEqual(f.request(image['src'],cookie=outsider)[0],404)
                for link in tags.links:
                    if 'data-customer-3d-link' in link:
                        target=parse_qs(urlsplit(link['href']).query)['preview'][0]
                        self.assertEqual(f.request(target,cookie=owner)[0],200)
                        self.assertEqual(f.request(target,cookie=outsider)[0],404)
                for text in ('cost.csv','quote.json','rate_cny','amount_cny','内部核价','/ortho2cad-test/'):
                    self.assertNotIn(text,page)
        after={path.relative_to(folder).as_posix():path.read_bytes() for path in folder.rglob('*') if path.is_file()}
        self.assertEqual(before,after)
        self.assertEqual(len(f.server.customer_batches.get(order['id'])['runs']),1)

    def test_missing_cached_image_keeps_quote_and_process_link_without_fake_image(self):
        f=self.fixture; owner=f.session();order=f.finished(owner)
        del f.manufacturing.files[(f.native_id,'S0002.png')]
        status,content=f.request(order['quote']['quote_url'],cookie=owner)
        self.assertEqual(status,200,content);page=content.decode();tags=Tags(page)
        self.assertEqual(len(tags.images),1);self.assertIn('暂无原生预览',page)
        self.assertEqual(len([row for row in tags.links if 'data-customer-process-link' in row]),2)

if __name__=='__main__':unittest.main()
