"""Process-first commercial documents cannot present a partial scope as total."""
import copy
from pathlib import Path
import sys,unittest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parent)]
from batch_quote_costing import calculate_batch
from batch_customer_quote import render_customer_quote
from test_batch_process_quote import flat,bent,process_settings,document
from test_batch_process_estimate import estimate_body
from batch_process_quote import apply_process_costing

class ProcessCustomerQuoteTests(unittest.TestCase):
    def quote(self,bodies):return calculate_batch([document(bodies)],process_settings())
    def test_supported_process_quote_shows_engineering_quantities_without_rates(self):
        result=self.quote([flat()]);original=copy.deepcopy(result)
        page=render_customer_quote(result).decode()
        self.assertIn('激光切割',page);self.assertIn('激光穿孔',page)
        self.assertIn('data-total-price="'+result['totals']['total_price']+'"',page)
        for text in ('综合加工','rate_cny','amount_cny','材料成本','准备费','加成比例','max(5'):
            self.assertNotIn(text,page)
        self.assertEqual(result,original)
    def test_bent_missing_blank_keeps_known_subtotal_separate_from_full_price(self):
        result=self.quote([flat(),bent()]);page=render_customer_quote(result).decode()
        self.assertIn('待工程核价',page);self.assertIn('data-total-price=""',page)
        self.assertIn('已识别项目参考小计',page);self.assertIn(result['manufacturing']['estimated_price_subtotal'],page)
        self.assertIn('不能作为整单报价',page)
    def test_partial_manual_cost_save_uses_latest_subtotal_without_publishing_total(self):
        result=self.quote([flat(),bent()]);result['cost_sheet']={'version':'cost-sheet-v1'}
        result['totals']['total_price']='123.45'
        page=render_customer_quote(result).decode()
        self.assertIn('待工程核价',page);self.assertIn('data-total-price=""',page)
        self.assertIn('参考小计：¥123.45',page)
    def test_explicit_complete_manual_cost_snapshot_preserves_sale_amount_and_engineering_notes(self):
        result=self.quote([flat()]);result['cost_sheet']={'version':'cost-sheet-v1'}
        result['manufacturing']['complete']=False
        result['totals']['total_price']='234.56'
        page=render_customer_quote(result).decode()
        self.assertIn('data-total-price="234.56"',page);self.assertIn('模拟报价',page)
        self.assertNotIn('不能作为整单报价',page)
    def test_adopted_route_is_explicit_on_customer_quote_and_print_without_internal_costs(self):
        body=estimate_body(bends=None);source=document([body]);result=self.quote([body])
        apply_process_costing(result,[source],route_assumption='sheet_form_weld_demo')
        result['process_estimate']={'version':'process-estimate-20261113-v1',
                                    'route_assumption':{'id':'sheet_form_weld_demo'}}
        original=copy.deepcopy(result)
        for printable in (False,True):
            with self.subTest(printable=printable):
                page=render_customer_quote(result,printable=printable).decode()
                self.assertIn('data-engineering-estimate-scope',page)
                self.assertIn('工程假设：钣金成形＋拼焊；拟定范围估算，待工程审核',page)
                self.assertIn('data-total-price="'+result['totals']['total_price']+'"',page)
                for private in ('rate_cny','amount_cny','材料成本','成本合计','20.00 元/件','15.00 元/件'):
                    self.assertNotIn(private,page)
        self.assertEqual(result,original)
    def test_estimate_without_adopted_route_does_not_claim_forming_and_weld_route(self):
        result=self.quote([estimate_body()])
        for printable in (False,True):
            page=render_customer_quote(result,printable=printable).decode()
            self.assertIn('data-engineering-estimate-scope',page)
            self.assertIn('拟定范围估算，待工程审核',page)
            self.assertNotIn('工程假设：钣金成形＋拼焊',page)
    def test_legacy_strict_flat_quote_does_not_gain_an_unadopted_engineering_assumption(self):
        result=self.quote([flat()])
        for printable in (False,True):
            page=render_customer_quote(result,printable=printable).decode()
            self.assertNotIn('data-engineering-estimate-scope',page)
            self.assertNotIn('工程假设：钣金成形＋拼焊',page)
if __name__=='__main__':unittest.main()
