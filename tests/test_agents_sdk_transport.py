"""Real pinned SDK + mocked HTTP streams, without a provider or customer data.

Run on the isolated server with requirements-agents and image dependencies.
Both API adapters must run the SDK tool loop and preserve actual image bytes.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_config import AgentConfig, AgentExecutionError, SDK_VERSION
from agents_executor import AgentsExecutor


READY = all(importlib.util.find_spec(name) is not None for name in ('agents', 'httpx2', 'PIL'))
MODEL = 'synthetic-vision-model'
FINAL = '{"candidates":[],"observations":[],"warnings":[]}'


def _sse(events, *, done=False):
    chunks=[]
    for row in events:
        chunks.append(('event: '+row['type']+'\n' if 'type' in row else '')+
                      'data: '+json.dumps(row,separators=(',',':'))+'\n\n')
    if done:chunks.append('data: [DONE]\n\n')
    return ''.join(chunks).encode('utf-8')


def _response(identity, output, incoming, outgoing, status='completed'):
    return {'id':identity,'object':'response','created_at':1,'status':status,'model':MODEL,
        'output':output,'parallel_tool_calls':False,'tool_choice':'auto','tools':[],
        'error':None,'incomplete_details':None,'instructions':None,'max_output_tokens':256,
        'metadata':{},'temperature':1,'top_p':1,
        'usage':{'input_tokens':incoming,'output_tokens':outgoing,'total_tokens':incoming+outgoing,
                 'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}}


def _responses_turn(index):
    identity='resp_synthetic_'+str(index)
    function={'id':'fc_synthetic','type':'function_call','name':'read_image',
              'call_id':'call_synthetic_image','arguments':'{"path":"IMAGE-1"}','status':'completed'}
    message={'id':'msg_synthetic','type':'message','role':'assistant','status':'completed',
             'content':[{'type':'output_text','text':FINAL,'annotations':[],'logprobs':[]}]}
    item=function if index==1 else message
    final=_response(identity,[item],11 if index==1 else 20,4 if index==1 else 6)
    created={**final,'status':'in_progress','output':[],'usage':None}
    events=[{'type':'response.created','sequence_number':0,'response':created},
            {'type':'response.output_item.added','sequence_number':1,'output_index':0,
             'item':{**item,'status':'in_progress',**({'arguments':''} if index==1 else {'content':[]})}}]
    if index==1:
        events.extend([
            {'type':'response.function_call_arguments.delta','sequence_number':2,
             'item_id':item['id'],'output_index':0,'delta':item['arguments']},
            {'type':'response.function_call_arguments.done','sequence_number':3,
             'item_id':item['id'],'output_index':0,'arguments':item['arguments']}])
    else:
        part={'type':'output_text','text':'','annotations':[],'logprobs':[]}
        events.extend([
            {'type':'response.content_part.added','sequence_number':2,'item_id':item['id'],
             'output_index':0,'content_index':0,'part':part},
            {'type':'response.output_text.delta','sequence_number':3,'item_id':item['id'],
             'output_index':0,'content_index':0,'delta':FINAL,'logprobs':[]},
            {'type':'response.output_text.done','sequence_number':4,'item_id':item['id'],
             'output_index':0,'content_index':0,'text':FINAL,'logprobs':[]},
            {'type':'response.content_part.done','sequence_number':5,'item_id':item['id'],
             'output_index':0,'content_index':0,'part':message['content'][0]}])
    events.extend([{'type':'response.output_item.done','sequence_number':6,'output_index':0,'item':item},
                   {'type':'response.completed','sequence_number':7,'response':final}])
    return _sse(events)


def _chat_turn(index):
    base={'id':'chatcmpl_synthetic_'+str(index),'object':'chat.completion.chunk','created':1,'model':MODEL}
    delta=({'role':'assistant','tool_calls':[{'index':0,'id':'call_synthetic_image','type':'function',
               'function':{'name':'read_image','arguments':'{"path":"IMAGE-1"}'}}]} if index==1
           else {'role':'assistant','content':FINAL})
    incoming,outgoing=(11,4) if index==1 else (20,6)
    events=[{**base,'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
            {**base,'choices':[{'index':0,'delta':{},'finish_reason':'tool_calls' if index==1 else 'stop'}]},
            {**base,'choices':[],'usage':{'prompt_tokens':incoming,'completion_tokens':outgoing,
                                         'total_tokens':incoming+outgoing}}]
    return _sse(events,done=True)


def _image_payloads(value):
    if isinstance(value,dict):
        if value.get('type')=='input_image':yield value.get('image_url')
        if value.get('type')=='image_url':
            image=value.get('image_url')
            yield image.get('url') if isinstance(image,dict) else image
        for item in value.values():yield from _image_payloads(item)
    elif isinstance(value,list):
        for item in value:yield from _image_payloads(item)


@unittest.skipUnless(READY,'Pinned Agents SDK, httpx2 and image runtime are required on the build server.')
class AgentsSDKTransportTests(unittest.IsolatedAsyncioTestCase):
    async def run_transport(self,api,*,failure=False):
        import agents
        import httpx2
        from importlib.metadata import version
        from openai import AsyncOpenAI as ActualClient, DefaultAsyncHttpxClient
        from PIL import Image
        from agent_tools import AttachmentTools

        self.assertEqual(version('openai-agents'),SDK_VERSION)
        requests=[];events=[];client_options=[]
        def handle(request):
            self.assertEqual(request.url.host,'model.synthetic.invalid')
            self.assertEqual(request.url.path,'/v1/responses' if api=='responses' else '/v1/chat/completions')
            payload=json.loads(request.content)
            requests.append(payload)
            if failure:
                return httpx2.Response(503,json={'error':{'message':'synthetic transient refusal','type':'server_error'}})
            self.assertLessEqual(len(requests),2,'SDK unexpectedly issued an additional model request')
            self.assertTrue(payload['stream']);self.assertIs(payload['store'],False)
            body=_responses_turn(len(requests)) if api=='responses' else _chat_turn(len(requests))
            return httpx2.Response(200,content=body,headers={'content-type':'text/event-stream'})
        def client_factory(**options):
            client_options.append(options)
            return ActualClient(**options,http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handle)))
        config=AgentConfig(model=MODEL,api_key='synthetic-offline-key',base_url='https://model.synthetic.invalid/v1',
                           model_api=api,max_runtime_seconds=10,max_requests=2,max_turns=4,
                           max_output_tokens=256,max_total_tokens=1000)
        with tempfile.TemporaryDirectory(prefix='fabflow-sdk-transport-') as temporary:
            workspace=Path(temporary).resolve()
            buffer=io.BytesIO();Image.new('RGB',(2,2),color=(12,34,56)).save(buffer,format='PNG')
            image=buffer.getvalue();(workspace/'source.png').write_bytes(image)
            tools=AttachmentTools(workspace,allowed_files=[{'id':'IMAGE-1','path':'source.png',
                'sha256':hashlib.sha256(image).hexdigest()}])
            with (patch('openai.AsyncOpenAI',side_effect=client_factory),
                  patch.object(agents.Runner,'run_streamed',wraps=agents.Runner.run_streamed) as sdk_run):
                if failure:
                    with self.assertRaises(AgentExecutionError):
                        await AgentsExecutor(config).run('Inspect IMAGE-1 and return one JSON object.',
                            'Use only the registered read_image tool; never infer missing values.',
                            tools.make_sdk_tools(),emit=events.append)
                    self.assertEqual(len(requests),1,'HTTP max_retries=0 and SDK retry=None must not replay a failed request')
                else:
                    result=await AgentsExecutor(config).run('Inspect IMAGE-1 and return one JSON object.',
                        'Use only the registered read_image tool; never infer missing values.',
                        tools.make_sdk_tools(),emit=events.append)
                    self.assertEqual(json.loads(result['text']),json.loads(FINAL))
                    self.assertEqual(result['usage'],{'requests':2,'input_tokens':31,'output_tokens':10,
                                                       'total_tokens':41,'tool_calls':1})
                    self.assertEqual(len(requests),2)
                    self.assertEqual(list(_image_payloads(requests[0])),[])
                    images=list(_image_payloads(requests[1]));self.assertEqual(len(images),1)
                    if api=='chat_completions':
                        messages=requests[1]['messages']
                        tool_outputs=[row for row in messages if row.get('role')=='tool']
                        self.assertTrue(tool_outputs)
                        self.assertEqual(tool_outputs[0]['tool_call_id'],'call_synthetic_image')
                        position=messages.index(tool_outputs[0])
                        self.assertEqual(messages[position+1]['role'],'user')
                        self.assertTrue(list(_image_payloads(messages[position+1])))
                        self.assertIn('call_synthetic_image',json.dumps(messages[position+1]))
                        self.assertFalse(any(list(_image_payloads(row)) for row in tool_outputs))
                        self.assertTrue(any(row.get('role')=='user' and list(_image_payloads(row)) for row in messages))
                    header,encoded=images[0].split(',',1)
                    self.assertEqual(header,'data:image/png;base64')
                    self.assertEqual(base64.b64decode(encoded,validate=True),image)
                    self.assertNotIn('ToolOutputImage',json.dumps(requests[1]))
                    calls=[row for row in events if row['type']=='tool/call']
                    results=[row for row in events if row['type']=='tool/result']
                    self.assertEqual(len(calls),1);self.assertEqual(len(results),1)
                    self.assertEqual(calls[0]['data']['name'],'read_image')
                    self.assertEqual(calls[0]['data']['callId'],results[0]['data']['message']['source']['callId'])
                    self.assertNotIn(encoded,json.dumps(events))
                self.assertEqual(sdk_run.call_count,1)
                self.assertTrue(sdk_run.call_args.kwargs['run_config'].tracing_disabled)
                self.assertIsNone(sdk_run.call_args.args[0].model_settings.retry)
                self.assertEqual(client_options[0]['max_retries'],0)

    async def test_responses_sdk_loop_sends_original_image_bytes_then_final_json_and_usage(self):
        await self.run_transport('responses')

    async def test_chat_completions_sdk_loop_sends_original_image_bytes_then_final_json_and_usage(self):
        await self.run_transport('chat_completions')

    async def test_responses_sdk_and_client_do_not_retry_a_transient_provider_failure(self):
        await self.run_transport('responses',failure=True)

    async def test_chat_completions_sdk_and_client_do_not_retry_a_transient_provider_failure(self):
        await self.run_transport('chat_completions',failure=True)


if __name__=='__main__':unittest.main()
