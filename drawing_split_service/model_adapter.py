"""Two real Responses calls against the already configured DSH OpenAI gateway.

No sample manifest, geometry, answer file, or browser fixture is consulted here.
The gateway and credential are loaded privately on ZeroClave, never sent to UI.
"""
from __future__ import annotations
import base64
from datetime import datetime, timezone
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
from .prompts import LOCATE, EXTRACT


class ModelUnavailable(RuntimeError):
    pass


def configuration():
    # PyYAML is installed in the dedicated server runtime. Configuration is the
    # existing DSH profile; do not create another provider or credential copy.
    try:
        import yaml
    except ModuleNotFoundError:
        # Reuse Ubuntu's packaged PyYAML, as the existing DSH helper does.
        sys.path.append('/usr/lib/python3/dist-packages')
        import yaml
    settings = yaml.safe_load(Path(os.environ.get('DRAWING_SPLIT_SETTINGS', '/var/lib/dsh/.dsh/settings.yaml')).read_text())
    credentials = yaml.safe_load(Path(os.environ.get('DRAWING_SPLIT_CREDENTIALS', '/var/lib/dsh/.dsh/.credentials.yaml')).read_text())
    def objects(value):
        if isinstance(value, dict):
            yield value
            for item in value.values():
                yield from objects(item)
        elif isinstance(value, list):
            for item in value:
                yield from objects(item)
    providers = [item['openai'] for item in objects(settings) if isinstance(item.get('openai'), dict) and 'baseURL' in item['openai']]
    defaults = [item['agent-default-model'] for item in objects(settings) if isinstance(item.get('agent-default-model'), dict)]
    if len(providers) != 1 or len(defaults) != 1:
        raise ModelUnavailable('Drawing model configuration needs review')
    endpoint = providers[0].get('baseURL')
    model = defaults[0].get('model')
    if model != 'gpt-6-sol':
        raise ModelUnavailable('Configured model changed; review before dispatch')
    key = credentials.get('refs', {}).get('OPENAI_CODEX_API_KEY')
    parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
    if not parsed or parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.fragment or parsed.query:
        raise ModelUnavailable('Existing model endpoint is invalid')
    if not isinstance(key, str) or not key.strip():
        raise ModelUnavailable('Existing model credential is unavailable')
    return endpoint, model, key


def parse_json(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 800_000:
        raise ValueError('Model did not return bounded JSON')
    value = value.strip()
    if value.startswith('```'):
        value = re.sub(r'^```(?:json)?\s*', '', value, count=1)
        value = re.sub(r'\s*```$', '', value, count=1)
    # Refuse embedded prose/extra objects; no eval or code execution.
    result = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
    if not isinstance(result, dict):
        raise ValueError('Model result is not an object')
    return result


def image_input(image, limit=2400):
    from PIL import Image
    if image.mode in {'RGBA','LA'} or 'transparency' in image.info:
        alpha=image.convert('RGBA')
        frame=Image.new('RGB',image.size,'white')
        frame.paste(alpha,mask=alpha.getchannel('A'))
    else:
        frame=image.convert('RGB')
    frame.thumbnail((limit, limit), Image.Resampling.LANCZOS)
    data = io.BytesIO()
    frame.save(data, format='PNG')
    return {'type': 'input_image', 'detail': 'high',
            'image_url': 'data:image/png;base64,' + base64.b64encode(data.getvalue()).decode('ascii')}


def located_regions(raw):
    if raw.get('coordinate_system') != 'normalized_1000':
        raise ValueError('Missing normalized coordinate system')
    valid=[]; seen=set()
    for row in raw.get('regions', [])[:32] if isinstance(raw.get('regions'), list) else []:
        if not isinstance(row, dict):
            continue
        name=row.get('id'); box=row.get('bbox')
        if not isinstance(name, str) or not name or name in seen or len(name)>120:
            continue
        if not isinstance(box, list) or len(box)!=4 or not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in box):
            continue
        if min(box)<0 or max(box)>1000 or box[0]>=box[2] or box[1]>=box[3]:
            continue
        seen.add(name);valid.append(row)
    return valid


def retain_dimension_context(regions):
    """Pad original pixels around model boxes so outer labels stay visible.

    Unmodified model coordinates remain in private raw-call receipts.
    """
    result=[]
    for row in regions:
        box=row['bbox']
        px,py=(25,15) if row.get('type')=='orthographic' else (6,6)
        result.append({**row,'bbox':[max(0,box[0]-px),max(0,box[1]-py),min(1000,box[2]+px),min(1000,box[3]+py)]})
    return result


class ResponsesAdapter:
    def __init__(self):
        # Validate before starting a service, but do not call or warm the model.
        _, model, _ = configuration()
        self.model_info={'provider':'openai', 'model':model}

    def analyze(self, image_path, width, height, progress):
        from PIL import Image
        from openai import OpenAI, DefaultHttpxClient
        endpoint, model, key=configuration()
        audit=[]
        proxy=os.environ.get('DRAWING_SPLIT_PROXY', 'http://127.0.0.1:17890') or None
        http=DefaultHttpxClient(proxy=proxy, trust_env=False, timeout=600)
        client=OpenAI(api_key=key,base_url=endpoint,http_client=http,max_retries=0,timeout=600)
        def call(stage, prompt, content, budget):
            started=time.monotonic()
            receipt={'stage':stage,'started_at':datetime.now(timezone.utc).isoformat(),'provider':'openai','model':model}
            try:
                response=client.responses.create(model=model,max_output_tokens=budget,
                    input=[{'role':'system','content':prompt},{'role':'user','content':content}])
                receipt.update(response_id=response.id,status=getattr(response,'status',None),
                    usage=response.usage.model_dump() if response.usage else None,output_text=response.output_text)
                if getattr(response,'status',None)=='incomplete':
                    raise ValueError('Model token budget incomplete')
                return parse_json(response.output_text)
            except Exception as error:
                # Save only class/status, never exception URLs/headers/body.
                receipt.update(error_type=type(error).__name__,http_status=getattr(error,'status_code',None))
                raise
            finally:
                receipt['duration_seconds']=round(time.monotonic()-started,3)
                audit.append(receipt)
        try:
            progress('locating','AI 正在定位视图、展开图和表格')
            with Image.open(image_path) as full:
                full.load()
                located=call('locating',LOCATE,[{'type':'input_text','text':f'分析本次上传的原始图片，原图像素 {width}×{height}。请独立定位全部视图及技术资料区域。'},image_input(full)],7000)
                filtered=located_regions(located)
                original_rows=located.get('regions',[])
                if isinstance(original_rows,list) and len(filtered)!=len(original_rows):
                    existing=located.get('warnings',[])
                    located['warnings']=(existing if isinstance(existing,list) else [])+['部分分区坐标未能通过检查，未自动裁取；请结合整图补充核对。']
                    located['analysis_complete']=False
                regions=retain_dimension_context(filtered)
                located['regions']=regions
                located.setdefault('parameters',[]);located.setdefault('process_notes',[]);located.setdefault('bend_rows',[])
                located['coordinate_system']='normalized_1000'
                if not regions:
                    located['analysis_complete']=located.get('document_kind')=='not_drawing'
                    located['_model_audit']=audit
                    return located
                progress('extracting','AI 正在读取各分区参数与工艺表格')
                content=[{'type':'input_text','text':'首先是同一份原始整页图片。已定位区域如下（坐标以整页为准）：\n'+json.dumps(regions,ensure_ascii=False)}, image_input(full)]
                for row in regions:
                    x0,y0,x1,y1=row['bbox']
                    box=(math.floor(x0*width/1000),math.floor(y0*height/1000),math.ceil(x1*width/1000),math.ceil(y1*height/1000))
                    content.extend([{'type':'input_text','text':f'区域 {row["id"]}：{row.get("title","")}；原图归一化坐标 {row["bbox"]}。下面是此区域的原像素裁片。'},image_input(full.crop(box),1600)])
                try:
                    extracted=call('extracting',EXTRACT,content,14000)
                    for field in ('parameters','process_notes','bend_rows'):
                        located[field]=extracted.get(field,[])
                    located['analysis_complete']=located.get('analysis_complete',True) is not False and extracted.get('analysis_complete',True) is not False
                    for field in ('warnings','review_notes'):
                        a=located.get(field,[]);b=extracted.get(field,[])
                        located[field]=(a if isinstance(a,list) else [])+(b if isinstance(b,list) else [])
                except Exception:
                    located['analysis_complete']=False
                    warnings=located.get('warnings',[])
                    located['warnings']=(warnings if isinstance(warnings,list) else [])+['视图定位已保留；本轮详细参数提取未完成，可先查看分图，或显式重试。']
            located['_model_audit']=audit
            return located
        finally:
            # Failure receipts remain private and unavailable via the file API.
            target=Path(image_path).parent/'model-call-audits'
            target.mkdir(mode=0o700,exist_ok=True)
            filename=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.json'
            path=target/filename
            path.write_text(json.dumps(audit,ensure_ascii=False,indent=2,default=str))
            path.chmod(0o600)
            client.close()


def create_adapter():
    return ResponsesAdapter()
