"""Customer output conserves financial facts and omits internal cost data."""
import copy
from html.parser import HTMLParser
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from batch_quote_costing import aggregate_display_groups, render_artifact
from batch_customer_quote import render_customer_quote, render_cost_sheet_csv, customer_part_projection
from test_batch_quote_presentation import example
from prep_record import digest


class Groups(HTMLParser):
    def __init__(self,content):
        super().__init__();self.groups=[];self.totals=[];self.feed(content)
    def handle_starttag(self,tag,attrs):
        data=dict(attrs)
        if tag=='tr' and 'data-print-group' in data:self.groups.append(data['data-print-group'])
        if 'data-total-price' in data:self.totals.append(data['data-total-price'])


class CustomerQuoteTests(unittest.TestCase):
    def test_customer_and_print_keep_all_amounts_without_costs_or_profit(self):
        result=example();saved=copy.deepcopy(result)
        for name in ('customer-quote.html','customer-print.html'):
            content,mime=render_artifact(result,name);page=content.decode();parsed=Groups(page)
            self.assertEqual(mime,'text/html; charset=utf-8')
            self.assertEqual(parsed.groups,[row['id'] for row in aggregate_display_groups(result)])
            self.assertEqual(parsed.totals,[result['totals']['total_price']])
            self.assertIn('待审核',page);self.assertIn('待核价',page)
            self.assertIn('未包含在上述金额中',page)
            for internal in ('综合加工演示费','成本汇总','利润','成本加成','gross_margin','internal-cost','cost.csv','quote.json','工程要求待核价','单件成本'):
                self.assertNotIn(internal,page)
        self.assertEqual(result,saved)

    def test_terms_and_part_names_are_escaped_and_unknown_terms_stay_unspecified(self):
        result=example();result['lines'][0]['part_name']='<script>alert(1)</script>'
        result['customer_terms']={'delivery':'15天<script>','payment':'月结','validity':'30天','tax_shipping':'未税，不含运费'}
        page=render_customer_quote(result).decode()
        self.assertIn('15天&lt;script&gt;',page);self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;',page)
        self.assertNotIn('<script>',page);self.assertIn('月结',page)
        del result['customer_terms'];page=render_customer_quote(result).decode()
        self.assertIn('另行约定',page);self.assertNotIn('17%',page)

    def test_explicit_customer_gallery_has_no_internal_basis(self):
        result=example();page=render_customer_quote(result,gallery=True).decode()
        self.assertIn('data-parts-showcase',page);self.assertIn('data-parts-search',page)
        for text in ('来源与计价依据','qps-item-details','材料单价','材料成本','采购单价','max(5'):
            self.assertNotIn(text,page)
        self.assertIn('data-total-price="'+result['totals']['total_price']+'"',page)

    def test_original_financial_exports_stay_identical_after_customer_reads(self):
        result=example();names=('quote.json','bom.csv','cost.csv')
        before={name:render_artifact(result,name)[0] for name in names}
        render_artifact(result,'customer-quote.html');render_artifact(result,'customer-print.html')
        self.assertEqual(before,{name:render_artifact(result,name)[0] for name in names})

    def test_internal_csv_uses_the_entry_amount_mapping(self):
        result=example();from batch_cost_sheet import build_cost_sheet
        sheet=build_cost_sheet(result);content=render_cost_sheet_csv(result).decode('utf-8-sig')
        for entry in sheet['entries']:
            self.assertIn(sheet['entry_amounts'][entry['id']],content)
        self.assertIn('来源口径',content)

    def test_cost_sheet_versions_are_blocked_from_legacy_assistant_before_model(self):
        # The service-specific source/worker guards are verified in HTTP tests.
        from batch_quote_assistant_service import QuoteAssistantService
        class Owner:
            def _engineering_pricing_context(self,state,run):
                return {'cost_sheet':{'version':'cost-sheet-v1'}},{},{}, {},{}
        service=QuoteAssistantService(Owner())
        with self.assertRaisesRegex(Exception,'内部核价单'):
            service._context({},'RUN-test')

    def test_customer_preview_projects_technical_data_without_financial_snapshot(self):
        payload={'line_id':'L0001','source_sha256':'a'*64,'groups':[{'id':'P1','name':'plate','solid_ids':['S1'],'internal_cost':'8'}],
                 'settings':{'rate_percent':'20'},'line_snapshot':{'id':'L0001','part_name':'Plate','quantity_total':20,
                 'material_profile':'ss304','total_cost':'100.00','material_rate':'30.00','weight_basis':'private internal note'},
                 'geometry':{'source_sha256':'a'*64,'output_unit':'mm','settings':{'profit':'20'},
                             'solids':[{'id':'S1','name':'Plate','mesh':{'positions':[0,0,0],'indices':[0]},'notes':'private'}]}}
        original=copy.deepcopy(payload);projected=customer_part_projection(payload)
        self.assertEqual(projected['line_snapshot']['quantity_total'],20)
        self.assertEqual(projected['geometry']['solids'][0]['mesh'],payload['geometry']['solids'][0]['mesh'])
        for forbidden in ('settings','total_cost','material_rate','weight_basis','internal_cost','private'):
            self.assertNotIn(forbidden,str(projected))
        projected['geometry']['solids'][0]['mesh']['positions'][0]=4
        self.assertEqual(payload,original)

    def test_customer_links_use_finance_free_preview_endpoints(self):
        result=example();previews=[]
        for line in result['lines']:
            url='/ortho2cad-test/customer-cases/safe-case/parts/'+line['id']+'.json'
            previews.append({'line_id':line['id'],'status':'ready','geometry_url':url,'thumbnail_url':url[:-5]+'/thumbnail.png'})
        for gallery in (True,False):
            page=render_customer_quote(result,part_previews=previews,gallery=gallery).decode()
            self.assertIn('%2Fcustomer-parts%2F',page)
            self.assertIn('customer=1',page)
            self.assertNotIn('%2Fparts%2F',page)

if __name__=='__main__':unittest.main()
