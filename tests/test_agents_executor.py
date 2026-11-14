"""Offline SDK-boundary controls. Execute only on the remote build server.

All input, usage, model names, keys and transport objects here are synthetic.
No provider, DSH process, native CAD task or customer record is accessed.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, replace
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_config import AgentConfig, AgentExecutionError, SDK_VERSION
from agents_executor import AgentsExecutor, _Budget, _PreparedRun, _bounded_tools, _chat_image_tool_input


class ScriptedStream:
    def __init__(self, budget, actions=(), output='{"candidates":[],"warnings":[]}', *, close=None):
        self.budget, self.actions, self.final_output = budget, list(actions), output
        self.cancelled, self.close = False, close

    async def stream_events(self):
        for action in self.actions:
            kind = action[0]
            if kind == 'request':
                await self.budget.request()
            elif kind == 'usage':
                self.budget.usage({'input_tokens': action[1], 'output_tokens': action[2], 'total_tokens': action[1]+action[2]})
            elif kind == 'tool':
                identity='synthetic-call-'+str(self.budget.tool_calls+1)
                await self.budget.tool_start(action[1],identity)
                await self.budget.tool_result(action[2],identity)
            elif kind == 'delta':
                yield {'type':'raw_response_event','data':{'type':'response.output_text.delta','delta':action[1]}}
            elif kind == 'wait':
                await action[1].wait()
            elif kind == 'cancel':
                action[1].set()
            elif kind == 'raise':
                raise action[1]
            yield {'type':'synthetic_safe_event'}

    def cancel(self, *, mode):
        assert mode == 'immediate'
        self.cancelled=True


def synthetic_factory(actions=None, output='{"candidates":[],"warnings":[]}', capture=None):
    def factory(**arguments):
        stream=ScriptedStream(arguments['budget'], actions or [('request',),('usage',7,3)],output)
        if capture is not None:capture.append({**arguments,'stream':stream})
        return stream
    return factory


class AgentConfigurationTests(unittest.TestCase):
    def test_empty_configuration_is_lazy_and_requires_explicit_model_and_key_only_for_a_run(self):
        config=AgentConfig.from_env({})
        self.assertIsNone(config.model);self.assertIsNone(config.api_key)
        self.assertEqual(config.model_api,'responses');self.assertFalse(config.store)
        with self.assertRaises(AgentExecutionError) as caught:config.validate_for_run()
        self.assertEqual(caught.exception.code,'model_not_configured')
        with self.assertRaises(AgentExecutionError) as caught:replace(config,model='synthetic-model').validate_for_run()
        self.assertEqual(caught.exception.code,'api_key_not_configured')
        self.assertIsNotNone(replace(config,model='synthetic-model').validate_for_run(require_key=False))

    def test_explicit_environment_aliases_priorities_and_safe_snapshot(self):
        config=AgentConfig.from_env({'FABFLOW_MODEL':'synthetic-model', 'FABFLOW_MODEL_API':'chat_completions',
            'OPENAI_API_KEY':'synthetic-canonical-key','FABFLOW_OPENAI_API_KEY':'synthetic-alias-key',
            'OPENAI_BASE_URL':'https://provider.invalid/v1','FABFLOW_OPENAI_BASE_URL':'https://alias.invalid/v1',
            'FABFLOW_MAX_REQUESTS':'2','FABFLOW_MODEL_STORE':'false'})
        config.validate_for_run()
        self.assertEqual(config.api_key,'synthetic-canonical-key');self.assertEqual(config.max_requests,2)
        self.assertEqual(config.base_url,'https://provider.invalid/v1')
        snapshot=config.snapshot()
        self.assertNotIn('api_key',snapshot);self.assertNotIn('base_url',snapshot)
        self.assertNotIn('synthetic-canonical-key',repr(config));self.assertNotIn('provider.invalid',repr(config))
        self.assertEqual(snapshot['model_api'],'chat_completions');self.assertEqual(snapshot['max_requests'],2)
        self.assertEqual(len(snapshot['transport_fingerprint']),64)
        self.assertNotEqual(snapshot['transport_fingerprint'],replace(config,base_url='https://other.invalid/v1').snapshot()['transport_fingerprint'])
        self.assertEqual(snapshot['transport_fingerprint'],replace(config,api_key='synthetic-rotated-key').snapshot()['transport_fingerprint'])
        alias=AgentConfig.from_env({'FABFLOW_OPENAI_API_KEY':'synthetic-alias','FABFLOW_OPENAI_BASE_URL':'https://alias.invalid/v1'})
        self.assertEqual(alias.api_key,'synthetic-alias');self.assertEqual(alias.base_url,'https://alias.invalid/v1')

    def test_invalid_budgets_and_transport_values_are_bounded(self):
        for supplied in ('0','-1','NaN','1000000000000','1.5',' 1'):
            with self.subTest(value=supplied),self.assertRaises(AgentExecutionError):
                AgentConfig.from_env({'FABFLOW_MAX_REQUESTS':supplied})
        base=AgentConfig(model='synthetic-model',api_key='synthetic-key')
        for changes in ({'model_api':'automatic'},{'store':'false'},{'max_requests':True},
                        {'base_url':'https://user:placeholder@provider.invalid/v1'},
                        {'base_url':'file:///private/forbidden'},{'base_url':'https://provider.invalid/v1?key=placeholder'}):
            with self.subTest(changes=changes),self.assertRaises(AgentExecutionError):replace(base,**changes).validate_for_run()


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    def config(self,**changes):return replace(AgentConfig(model='synthetic-model'),**changes)

    async def execute(self,actions=None,output='{"candidates":[],"warnings":[]}',**changes):
        events=[];captures=[]
        executor=AgentsExecutor(self.config(**changes),runner_factory=synthetic_factory(actions,output,captures))
        result=await executor.run('Synthetic prompt','Read only this synthetic source.',[],emit=events.append)
        return result,events,captures

    async def test_injected_runner_works_without_key_or_sdk_and_preserves_json_usage(self):
        with patch.dict(sys.modules,{'agents':None,'openai':None}):result,events,captures=await self.execute()
        self.assertEqual(result['status'],'completed');self.assertEqual(result['usage'],{
            'requests':1,'input_tokens':7,'output_tokens':3,'total_tokens':10,'tool_calls':0})
        self.assertEqual(result['text'],'{"candidates":[],"warnings":[]}')
        self.assertEqual(result['model'],'synthetic-model');self.assertEqual(result['model_api'],'responses')
        self.assertEqual([row['type'] for row in events],['agent/started','step/start','agent/completed'])
        self.assertNotIn('Synthetic prompt',str(events));self.assertEqual(len(captures),1)

    async def test_tools_emit_id_only_and_do_not_expose_result_content(self):
        result,events,_=await self.execute([('request',),('usage',2,1),('tool','read_image',{'image_url':'synthetic-image-data'}),
                                          ('request',),('usage',4,2)])
        self.assertEqual(result['usage']['requests'],2);self.assertEqual(result['usage']['tool_calls'],1)
        self.assertEqual(result['usage']['total_tokens'],9)
        calls=[row for row in events if row['type']=='tool/call'];returns=[row for row in events if row['type']=='tool/result']
        self.assertEqual(calls[0]['data']['callId'],returns[0]['data']['message']['source']['callId'])
        self.assertNotIn('synthetic-image-data',str(events))

    async def test_request_limit_rejects_before_another_provider_request(self):
        capture=[];runner=synthetic_factory([('request',),('usage',1,1),('request',)],capture=capture)
        executor=AgentsExecutor(self.config(max_requests=1),runner_factory=runner)
        with self.assertRaises(AgentExecutionError) as caught:await executor.run('prompt','instructions',[])
        self.assertEqual(caught.exception.code,'request_budget_exceeded')
        self.assertEqual(capture[0]['budget'].requests,1);self.assertTrue(capture[0]['stream'].cancelled)

    async def test_observed_token_limit_rejects_instead_of_completing_over_budget(self):
        with self.assertRaises(AgentExecutionError) as caught:
            await self.execute([('request',),('usage',7,4)],max_total_tokens=10)
        self.assertEqual(caught.exception.code,'token_budget_exceeded')

    async def test_unknown_or_inconsistent_usage_never_turns_into_zero_tokens(self):
        with self.assertRaises(AgentExecutionError) as caught:await self.execute([('request',)])
        self.assertEqual(caught.exception.code,'usage_unavailable');self.assertTrue(caught.exception.ambiguous)
        budget=_Budget(self.config(),None,None)
        for value in ({'input_tokens':None,'output_tokens':2,'total_tokens':2},
                      {'input_tokens':7,'output_tokens':3,'total_tokens':1}):
            with self.subTest(value=value),self.assertRaises(AgentExecutionError):budget.usage(value)

    async def test_tool_call_and_single_and_total_byte_limits_are_enforced(self):
        for config,actions,code in (
            ({'max_tool_calls':1},[('tool','read','one'),('tool','read','two')],'tool_budget_exceeded'),
            ({'max_tool_output_bytes':8},[('tool','read','123456789')],'byte_budget_exceeded'),
            ({'max_total_tool_bytes':8},[('tool','read','1234'),('tool','read','5678')],'byte_budget_exceeded')):
            with self.subTest(config=config),self.assertRaises(AgentExecutionError) as caught:
                await self.execute([('request',),('usage',1,1),*actions],**config)
            self.assertEqual(caught.exception.code,code)

    async def test_json_output_must_be_one_complete_bounded_object(self):
        for text in ('[]','unfinished {','{"a":1,"a":2}','{"a":NaN}','```json\n{}\n```\nextra'):
            with self.subTest(output=text),self.assertRaises(AgentExecutionError) as caught:await self.execute(output=text)
            self.assertEqual(caught.exception.code,'invalid_model_output')
        with self.assertRaises(AgentExecutionError) as caught:await self.execute(output='{"long":"1234567890"}',max_output_bytes=8)
        self.assertEqual(caught.exception.code,'output_budget_exceeded')
        result,_,_=await self.execute(output='```json\n{"ok":true}\n```')
        self.assertEqual(result['text'],'{"ok":true}')

    async def test_partial_text_stream_exceeds_bytes_without_emitting_raw_text(self):
        events=[];runner=synthetic_factory([('request',),('usage',1,1),('delta','synthetic-private-body')])
        with self.assertRaises(AgentExecutionError) as caught:
            await AgentsExecutor(self.config(max_output_bytes=8),runner_factory=runner).run('p','i',[],emit=events.append)
        self.assertEqual(caught.exception.code,'output_budget_exceeded');self.assertNotIn('synthetic-private-body',str(events))

    async def test_cancel_before_admission_and_during_stalled_stream_stops_local_run(self):
        cancelled=threading.Event();cancelled.set();captures=[]
        executor=AgentsExecutor(self.config(),runner_factory=synthetic_factory(capture=captures))
        with self.assertRaises(AgentExecutionError) as caught:await executor.run('p','i',[],cancelled=cancelled)
        self.assertEqual(caught.exception.code,'cancelled');self.assertEqual(captures,[])
        cancelled.clear();captures=[]
        runner=synthetic_factory([('request',),('cancel',cancelled),('wait',asyncio.Event())],capture=captures)
        with self.assertRaises(AgentExecutionError) as caught:
            await AgentsExecutor(self.config(),runner_factory=runner).run('p','i',[],cancelled=cancelled)
        self.assertEqual(caught.exception.code,'cancelled');self.assertTrue(captures[0]['stream'].cancelled)

    async def test_deadline_applies_even_when_no_new_stream_event_arrives(self):
        captures=[];runner=synthetic_factory([('request',),('wait',asyncio.Event())],capture=captures)
        with self.assertRaises(AgentExecutionError) as caught:
            await AgentsExecutor(self.config(max_runtime_seconds=1),runner_factory=runner).run('p','i',[])
        self.assertEqual(caught.exception.code,'timeout');self.assertTrue(caught.exception.ambiguous)
        self.assertTrue(captures[0]['stream'].cancelled)

    async def test_provider_exception_text_and_key_are_not_exposed(self):
        APITimeoutError=type('APITimeoutError',(RuntimeError,),{})
        with self.assertRaises(AgentExecutionError) as caught:
            await self.execute([('request',),('raise',APITimeoutError('synthetic-sensitive-diagnostic'))])
        self.assertEqual(caught.exception.code,'transport_error');self.assertTrue(caught.exception.ambiguous)
        self.assertNotIn('synthetic-sensitive-diagnostic',str(caught.exception))

    async def test_config_and_prompt_budget_fail_before_runner_construction(self):
        capture=[]
        executor=AgentsExecutor(AgentConfig(),runner_factory=synthetic_factory(capture=capture))
        with self.assertRaises(AgentExecutionError):await executor.run('p','i',[])
        executor=AgentsExecutor(self.config(max_input_bytes=2),runner_factory=synthetic_factory(capture=capture))
        with self.assertRaises(AgentExecutionError) as caught:await executor.run('prompt','instructions',[])
        self.assertEqual(caught.exception.code,'input_budget_exceeded');self.assertEqual(capture,[])

    async def test_default_factory_uses_sdk_runner_with_scoped_privacy_and_no_client_retry(self):
        records={}
        @dataclass
        class Settings:
            max_tokens:int
            store:bool
            parallel_tool_calls:bool
            include_usage:bool
            retry:object
        @dataclass
        class FunctionTool:
            name:str
            on_invoke_tool:object
        class Delegate:
            def __init__(self,**values):records['model']=values
            async def stream_response(self,**values):
                records['settings']=values['model_settings']
                yield SimpleNamespace(type='response.completed',response=SimpleNamespace(
                    usage=SimpleNamespace(input_tokens=7,output_tokens=3,total_tokens=10)))
        class Responses(Delegate):
            def __init__(self,**values):super().__init__(**values);records['api']='responses'
        class Chat(Delegate):
            def __init__(self,**values):super().__init__(**values);records['api']='chat_completions'
        class Agent:
            def __init__(self,**values):self.__dict__.update(values);records['agent']=self
        class RunConfig:
            def __init__(self,**values):self.__dict__.update(values)
        class Client:
            def __init__(self,**values):records['client']=values
            async def close(self):records['closed']=True
        class Stream:
            final_output='{"candidates":[],"warnings":[]}'
            async def stream_events(self):
                async for item in records['agent'].model.stream_response(model_settings=records['agent'].model_settings):
                    yield SimpleNamespace(type='raw_response_event',data=item)
            def cancel(self,**_):records['cancelled']=True
        class Runner:
            @staticmethod
            def run_streamed(agent,prompt,**values):
                records['runner']={'agent':agent,'prompt':prompt,**values}
                return Stream()
        sdk=SimpleNamespace(Model=object,Agent=Agent,Runner=Runner,RunConfig=RunConfig,
            ModelSettings=Settings,FunctionTool=FunctionTool,
            OpenAIResponsesModel=Responses,OpenAIChatCompletionsModel=Chat)
        for api in ('responses','chat_completions'):
            records.clear()
            config=self.config(api_key='synthetic-key',base_url='https://provider.invalid/v1',model_api=api,
                               max_total_tokens=20,max_output_tokens=50)
            with (patch.dict(sys.modules,{'agents':sdk,'openai':SimpleNamespace(AsyncOpenAI=Client)}),
                  patch('importlib.metadata.version',return_value=SDK_VERSION)):
                result=await AgentsExecutor(config).run('prompt','instructions',[])
            self.assertEqual(result['status'],'completed');self.assertEqual(records['api'],api)
            self.assertEqual(records['client']['max_retries'],0)
            self.assertEqual(records['client']['base_url'],'https://provider.invalid/v1')
            self.assertTrue(records['runner']['run_config'].tracing_disabled)
            self.assertFalse(records['settings'].store);self.assertFalse(records['settings'].parallel_tool_calls)
            self.assertTrue(records['settings'].include_usage);self.assertIsNone(records['settings'].retry)
            self.assertEqual(records['settings'].max_tokens,20)
            self.assertEqual(records['runner']['max_turns'],config.max_turns)
            self.assertTrue(records['closed'])

    async def test_function_tool_wrapper_counts_before_invocation_and_retains_real_image_output(self):
        @dataclass
        class FunctionTool:
            name:str
            on_invoke_tool:object
        calls=[]
        async def invoke(context,arguments):
            calls.append(arguments)
            return [{'type':'image','image_url':'data:image/png;base64,SYNTHETIC'}]
        budget=_Budget(self.config(max_tool_calls=1),None,None)
        sdk=SimpleNamespace(FunctionTool=FunctionTool)
        tool=_bounded_tools([FunctionTool('read_image',invoke)],sdk,budget)[0]
        result=await tool.on_invoke_tool(SimpleNamespace(tool_call_id='synthetic-image-1'),'{"path":"source.png"}')
        self.assertEqual(result,[{'type':'image','image_url':'data:image/png;base64,SYNTHETIC'}])
        self.assertEqual(budget.tool_calls,1)
        with self.assertRaises(AgentExecutionError) as caught:
            await tool.on_invoke_tool(SimpleNamespace(tool_call_id='synthetic-image-2'),'{"path":"source.png"}')
        self.assertEqual(caught.exception.code,'tool_budget_exceeded');self.assertEqual(len(calls),1)
        with self.assertRaises(AgentExecutionError):_bounded_tools([object()],sdk,budget)

    async def test_chat_image_projection_keeps_all_adjacent_tool_responses_before_user_images(self):
        image1={'type':'input_image','image_url':'data:image/png;base64,SYNTHETIC-ONE'}
        image2={'type':'input_image','image_url':'data:image/png;base64,SYNTHETIC-TWO'}
        source=[{'type':'function_call','call_id':'call-one','name':'read_image','arguments':'{}'},
                {'type':'function_call','call_id':'call-text','name':'read','arguments':'{}'},
                {'type':'function_call','call_id':'call-two','name':'read_image','arguments':'{}'},
                {'type':'function_call_output','call_id':'call-one','output':[image1]},
                {'type':'function_call_output','call_id':'call-text','output':'synthetic note'},
                {'type':'function_call_output','call_id':'call-two','output':[image2]},
                {'role':'user','content':'An independent later message.'}]
        original=copy.deepcopy(source)
        projected=_chat_image_tool_input(source)
        self.assertEqual(source,original)
        self.assertEqual([item['call_id'] for item in projected[3:6]],['call-one','call-text','call-two'])
        self.assertTrue(all(item['type']=='function_call_output' for item in projected[3:6]))
        self.assertEqual(projected[6]['role'],'user')
        images=[item for item in projected[6]['content'] if item['type']=='input_image']
        self.assertEqual(images,[image1,image2])
        content=projected[6]['content']
        self.assertIn('call-one',content[1]['text']);self.assertEqual(content[2],image1)
        self.assertIn('call-two',content[3]['text']);self.assertEqual(content[4],image2)
        self.assertEqual(projected[7],source[-1])


if __name__=='__main__':unittest.main()
