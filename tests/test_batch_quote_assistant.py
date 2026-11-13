"""Literal intent evidence, unambiguous scopes and one-prompt DSH recovery."""
from contextlib import nullcontext
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_assistant import _payload, checked_message, normalize_intent, parse_message
from batch_quote_costing import calculate_batch
from dsh_bridge import DshBridgeError
from test_batch_quote_costing import document, settings, solid

def base():
    return calculate_batch([document([solid(1, name='支架'), solid(2, definition='screw', name='CLS-M6-2', volume=1000)], file_id='F0001'),
                            document([solid(3, definition='other-screw', name='CLS-M6-2', volume=1000)], file_id='F0002')], settings(order_sets=10))
def intent(*changes): return {'status':'ready','message':'待核对修改预览。','changes':list(changes)}
def quantity(): return intent({'field':'order_sets','value':'20','source_text':'改成20套'})

class FakeBridge:
    calls=[]; answer=None; lose_ack=False; use_tool=False; reason='completed'
    def __init__(self,*args,**kwargs): pass
    def _lock(self): return nullcontext()
    def _rpc(self,method,body,**kwargs):
        self.calls.append((method,copy.deepcopy(body),kwargs))
        if method=='session.create': return {'sessionId':body['sessionId']}
        if method=='session.prompt':
            if self.lose_ack:
                type(self).lose_ack=False
                raise DshBridgeError('lost acknowledgement','transport-error')
            return {'accepted':True}
        if method=='session.cancel': return {'accepted':True}
        if method=='session.list': return {'items':[]}
        if method=='session.history':
            if self.use_tool: return {'events':[{'seq':1,'type':'tool/call','data':{'turn':1,'name':'read'}}]}
            return {'events':[
                {'event':{'seq':1,'type':'assistant/message','data':{'turn':1,'message':{'content':[{'type':'text','text':json.dumps(self.answer)}]}}}},
                {'event':{'seq':2,'type':'turn/end','data':{'turn':1,'reason':{'kind':self.reason}}}}]}
        raise AssertionError(method)

class QuoteAssistantIntentTests(unittest.TestCase):
    def setUp(self):
        FakeBridge.calls=[]; FakeBridge.answer=quantity(); FakeBridge.lose_ack=False; FakeBridge.use_tool=False; FakeBridge.reason='completed'
    def test_quantity_and_cost_plus_are_explicit_typed_values_without_financial_results(self):
        old=base(); saved=copy.deepcopy(old); message='这单改成20套，成本加成15%。'
        answer=intent({'field':'order_sets','value':'20','source_text':'改成20套'},
                      {'field':'pricing_method','value':'cost_plus','source_text':'成本加成15%'},
                      {'field':'rate_percent','value':'15','source_text':'成本加成15%'})
        result=normalize_intent(old,message,answer)
        self.assertEqual({r['field']:r['value'] for r in result['patch']['changes']},{'order_sets':'20','pricing_method':'cost_plus','rate_percent':'15'})
        self.assertNotIn('totals',result); self.assertEqual(old,saved)
    def test_chinese_numbers_are_explicit_not_invented_defaults(self):
        answer=intent({'field':'order_sets','value':'20','source_text':'改成二十套'},
                      {'field':'rate_percent','value':'15','source_text':'成本加成百分之十五'})
        result=normalize_intent(base(),'改成二十套，成本加成百分之十五',answer)
        self.assertEqual([r['value'] for r in result['patch']['changes']],['20','15'])
    def test_hallucinated_values_foreign_ids_or_approval_fields_cannot_be_adopted(self):
        message='L0002采购单价改为0.90元/件'
        good={'field':'purchase_rate','value':'0.90','line_ids':['L0002'],'source_text':message}
        for changed in (dict(good,value='9.00'),dict(good,line_ids=['L9999']),dict(good,approved=True),dict(good,source_text='不存在的原文'),dict(good,field='total_price')):
            with self.subTest(changed=changed),self.assertRaises(ValueError): normalize_intent(base(),message,intent(changed))
    def test_line_id_adjacent_to_chinese_does_not_expand_scope(self):
        message='L0002采购单价改为0.90元/件'
        result=normalize_intent(base(),message,intent({'field':'purchase_rate','value':'0.90','line_ids':['L0002'],'source_text':message}))
        self.assertEqual(result['patch']['changes'][0]['line_ids'],['L0002'])
    def test_identifiers_and_other_units_cannot_supply_missing_price_or_percent(self):
        message='所有CLS-M6-2采购价改为0.90元/件'
        with self.assertRaises(ValueError):
            normalize_intent(base(),message,intent({'field':'purchase_rate','value':'2','line_ids':['L0002','L0003'],'source_text':message}))
        message='改成20套，成本加成15%'
        with self.assertRaises(ValueError): normalize_intent(base(),message,intent({'field':'rate_percent','value':'20','source_text':message}))
        with self.assertRaises(ValueError): normalize_intent(base(),message,intent({'field':'order_sets','value':'15','source_text':message}))
    def test_duplicate_name_requires_explicit_all_sources(self):
        message='CLS-M6-2采购价改为0.90元/件'
        change={'field':'purchase_rate','value':'0.90','line_ids':['L0002'],'source_text':message}
        with self.assertRaisesRegex(ValueError,'多条来源'): normalize_intent(base(),message,intent(change))
        message='所有CLS-M6-2采购价改为0.90元/件'; change.update(line_ids=['L0002','L0003'],source_text=message)
        self.assertEqual(len(normalize_intent(base(),message,intent(change))['patch']['changes'][0]['line_ids']),2)
        change['line_ids']=['L0002']
        with self.assertRaises(ValueError): normalize_intent(base(),message,intent(change))
    def test_piece_counts_wrong_fee_units_and_unstated_percentages_are_rejected(self):
        for message,change in [
            ('数量改成20件',{'field':'order_sets','value':'20'}),
            ('L0001加工费改为2元/米',{'field':'process_rate','value':'2','method':'generic_processing','line_ids':['L0001']}),
            ('L0001加工费改为2元/厘米',{'field':'process_rate','value':'2','method':'generic_processing','line_ids':['L0001']}),
            ('L0001加工费改为500元/吨',{'field':'process_rate','value':'500','method':'generic_processing','line_ids':['L0001']}),
            ('利润改成15',{'field':'rate_percent','value':'15'}),
            ('成本加成15%',{'field':'rate_percent','value':'0.15'})]:
            with self.subTest(message=message),self.assertRaises(ValueError): normalize_intent(base(),message,intent({**change,'source_text':message}))
    def test_margin_request_cannot_silently_keep_cost_plus(self):
        message='目标毛利率改为30%'
        change={'field':'rate_percent','value':'30','source_text':message}
        with self.assertRaises(ValueError): normalize_intent(base(),message,intent(change))
        result=normalize_intent(base(),message,intent(change,{'field':'pricing_method','value':'gross_margin','source_text':message}))
        self.assertEqual(next(row['value'] for row in result['patch']['changes'] if row['field']=='pricing_method'),'gross_margin')
    def test_clarification_is_not_a_partial_executable_patch(self):
        answer={'status':'needs_clarification','message':'请给出要改的行号。','changes':[]}
        self.assertEqual(normalize_intent(base(),'这个便宜点',answer),{'status':'needs_clarification','message':answer['message']})
        answer['changes']=quantity()['changes']
        with self.assertRaises(ValueError): normalize_intent(base(),'改成20套',answer)
    def test_malformed_model_json_extra_totals_and_control_characters_are_rejected(self):
        with self.assertRaises(ValueError): normalize_intent(base(),'改成20套',{**quantity(),'total_price':'1.00'})
        for text in ('{"status":"ready","status":"failed"}','```json\n{}\n```\n```json\n{}\n```','{"amount":NaN}'):
            with self.subTest(text=text),self.assertRaises(ValueError): _payload(text)
        for message in ('',' '*20,'x'*4001,'\x00'):
            with self.assertRaises(ValueError): checked_message(message)
    def test_transport_one_prompt_and_cache_recovery_keep_original_quote(self):
        with tempfile.TemporaryDirectory() as folder,patch('batch_quote_assistant.DshBridge',FakeBridge):
            original=base(); saved=copy.deepcopy(original)
            first=parse_message(original,'改成20套',workspace=folder,rpc_url='http://127.0.0.1:3080',poll_interval=0)
            calls=copy.deepcopy(FakeBridge.calls)
            self.assertEqual(parse_message(original,'改成20套',workspace=folder,rpc_url='http://127.0.0.1:3080',poll_interval=0),first)
            self.assertEqual(FakeBridge.calls,calls); self.assertEqual(first['parser']['kind'],'dsh')
            prompts=[b for m,b,_ in calls if m=='session.prompt']; self.assertEqual(len(prompts),1)
            self.assertIn('禁止调用任何工具',prompts[0]['content'][0]['text']); self.assertEqual(original,saved)
            with self.assertRaises(ValueError): parse_message(original,'改成30套',workspace=folder,rpc_url='http://127.0.0.1:3080')
    def test_lost_admission_ack_inspects_same_session_without_resubmission(self):
        FakeBridge.lose_ack=True
        with tempfile.TemporaryDirectory() as folder,patch('batch_quote_assistant.DshBridge',FakeBridge):
            self.assertEqual(parse_message(base(),'改成20套',workspace=folder,rpc_url='http://127.0.0.1:3080',poll_interval=0)['status'],'ready')
            self.assertEqual([m for m,_,_ in FakeBridge.calls].count('session.prompt'),1)
    def test_tool_use_is_cancelled_without_a_saved_patch(self):
        FakeBridge.use_tool=True
        with tempfile.TemporaryDirectory() as folder,patch('batch_quote_assistant.DshBridge',FakeBridge):
            with self.assertRaises(DshBridgeError) as error: parse_message(base(),'改成20套',workspace=folder,rpc_url='http://127.0.0.1:3080',poll_interval=0)
            self.assertEqual(error.exception.code,'unexpected-tool'); self.assertIn('session.cancel',[m for m,_,_ in FakeBridge.calls])
            self.assertFalse((Path(folder)/'assistant-intent-result.json').exists())
    def test_timeout_never_resends_and_failed_model_never_prices(self):
        with tempfile.TemporaryDirectory() as folder,patch('batch_quote_assistant.DshBridge',FakeBridge):
            original=base(); saved=copy.deepcopy(original)
            for _ in range(2):
                with self.assertRaises(TimeoutError): parse_message(original,'改成20套',workspace=folder,rpc_url='http://127.0.0.1:3080',timeout_seconds=0)
            self.assertEqual([m for m,_,_ in FakeBridge.calls].count('session.prompt'),1); self.assertEqual(original,saved)
        FakeBridge.reason='failed'
        with tempfile.TemporaryDirectory() as folder,patch('batch_quote_assistant.DshBridge',FakeBridge):
            with self.assertRaises(DshBridgeError): parse_message(base(),'改成20套',workspace=folder,rpc_url='http://127.0.0.1:3080',poll_interval=0)

if __name__=='__main__': unittest.main()
