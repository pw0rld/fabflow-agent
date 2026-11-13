"""Human review overlays and engineering packets; this module never calls AI."""
from __future__ import annotations
import copy
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import tempfile
import zipfile
from .contract import REGION_TYPES, TECHNICAL_TYPES
from .semantic import annotate_manifest, CATEGORIES, DIRECTIONS
from .service import ServiceError, atomic_json, file_hash, no_links, now

REVIEW_STATUSES={'pending_review','checked','questioned'}
MAX_REVISIONS=256
MAX_ASSET_BYTES=256*1024*1024
MAX_EXPORT_BYTES=192*1024*1024

def _fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()

def _string(value,limit,*,empty=True,nullable=False):
    if nullable and value is None:return None
    if not isinstance(value,str) or len(value)>limit:raise ServiceError('核对字段格式或长度无效。','review_invalid')
    result=value.strip()
    if not empty and not result:raise ServiceError('核对字段不能为空。','review_invalid')
    if any(ord(char)<32 and char not in '\n\t' for char in result):raise ServiceError('核对字段包含不支持的字符。','review_invalid')
    return result

def _value(value,depth=0):
    if value is None or isinstance(value,bool):return value
    if isinstance(value,str):return _string(value,2000)
    if type(value) is int and value.bit_length()<=1023:return value
    if type(value) is float and math.isfinite(value):return value
    if isinstance(value,list) and depth==0 and len(value)<=24:return [_value(v,1) for v in value]
    raise ServiceError('参数值仅支持数值、文字、空值或短列表。','review_invalid')

def _manifest(record):
    manifest=record.get('manifest')
    if record.get('status') not in {'succeeded','partial'} or not isinstance(manifest,dict):raise ServiceError('请等待本轮识别完成后核对。','review_not_ready',409)
    return manifest

def _empty(record,base):
    return {'schema_version':1,'attempt':record['attempt'],'base_sha256':_fingerprint(base),'revision':0,'parameter_patches':{},'region_patches':{},'history':[],'requests':{}}

def _load(service,record,base):
    path=service._dir(record['job_id'])/'review.json';no_links(path)
    if not path.exists():return _empty(record,base)
    if path.stat().st_size>32*1024*1024:raise ServiceError('核对记录超过读取上限。','review_invalid',500)
    try:state=json.loads(path.read_text())
    except (OSError,ValueError):raise ServiceError('核对记录暂时无法读取。','review_invalid',500) from None
    if not isinstance(state,dict):raise ServiceError('核对记录格式无效。','review_invalid',500)
    if state.get('attempt')!=record['attempt']:return _empty(record,base)
    if state.get('base_sha256')!=_fingerprint(base):raise ServiceError('识别底稿已变化，旧核对记录保留，请重新连接。','review_base_changed',409)
    if type(state.get('revision')) is not int or any(not isinstance(state.get(key),dict) for key in ('requests','parameter_patches','region_patches')) or not isinstance(state.get('history'),list):raise ServiceError('核对记录格式无效。','review_invalid',500)
    return state

def _effective(base,state):
    result=annotate_manifest(copy.deepcopy(base))
    for row in result.get('regions',[]):
        original=copy.deepcopy(next((v for v in base.get('regions',[]) if v.get('id')==row.get('id')),row))
        patch=state['region_patches'].get(row['id'],{})
        row.update(copy.deepcopy(patch));row['original_candidate']=original;row['review_modified']=bool(patch)
        if patch:row['review_source']='human'
    for row in result.get('parameters',[]):
        original=copy.deepcopy(next((v for v in base.get('parameters',[]) if v.get('id')==row.get('id')),row))
        patch=copy.deepcopy(state['parameter_patches'].get(row['id'],{}))
        row.update({key:value for key,value in patch.items() if key!='region_id'})
        row['original_candidate']=original;row['original_evidence']=copy.deepcopy(original.get('evidence',[]));row['review_modified']=bool(patch)
        if patch:row['review_source']='human';row['human_fields']=sorted(patch)
        if 'category' in patch:row['category_source']='human'
        if 'direction' in patch:row['direction_source']='human'
        if 'region_id' in patch:
            chosen=patch['region_id'];row['region_id']=chosen;row['human_association']=True
            row['association']={'region_id':chosen,'source':'human','original_region_ids':list(dict.fromkeys(ev.get('region_id') for ev in original.get('evidence',[]) if ev.get('region_id')))}
            # Navigation changes, but original printed text never moves.
            row['evidence']=[{'region_id':chosen,'raw_text':ev.get('raw_text',''),'original_region_id':ev.get('region_id'),'human_association':True} for ev in original.get('evidence',[])] if chosen else []
    result=annotate_manifest(result);result['engineering_approved']=False
    result['relationships']={'view_region_ids':[r['id'] for r in result.get('regions',[]) if r.get('type') not in TECHNICAL_TYPES],'supplement_region_ids':[r['id'] for r in result.get('regions',[]) if r.get('type') in TECHNICAL_TYPES]}
    result['review']={'attempt':state['attempt'],'revision':state['revision'],'engineering_approved':False}
    return result

def _public(record,base,state):
    manifest=_effective(base,state)
    return {'job_id':record['job_id'],'attempt':record['attempt'],'revision':state['revision'],'source_sha256':record['source'].get('sha256'),'base_manifest_sha256':state['base_sha256'],'regions':manifest.get('regions',[]),'parameters':manifest.get('parameters',[]),'history':copy.deepcopy(state['history']),'manifest':manifest,'model':copy.deepcopy(record.get('model')),'engineering_approved':False,'model_called':False,'editable':True}

def get_review(service,job_id):
    with service.lock:
        record=service._load(job_id);base=_manifest(record);return _public(record,base,_load(service,record,base))

def _action(payload,base):
    action=payload.get('action')
    if not isinstance(action,dict) or set(action)-{'type','id','patch'}:raise ServiceError('核对操作格式无效。','review_invalid')
    kind=action.get('type');identifier=action.get('id')
    if not isinstance(kind,str) or kind not in {'parameter','region','reset_parameter','reset_region'} or not isinstance(identifier,str):raise ServiceError('不支持此核对操作。','review_invalid')
    group='parameters' if kind in {'parameter','reset_parameter'} else 'regions'
    candidate=next((row for row in base.get(group,[]) if row.get('id')==identifier),None)
    if candidate is None:raise ServiceError('核对对象不属于本轮识别结果。','review_target_missing',404)
    patch=action.get('patch',{})
    if not isinstance(patch,dict):raise ServiceError('核对内容格式无效。','review_invalid')
    if kind.startswith('reset_'):
        if patch:raise ServiceError('恢复操作不接受替代字段。','review_invalid')
        return {'type':kind,'id':identifier},candidate
    allowed={'label','value','unit','category','direction','datum','feature','region_id','status','note'} if group=='parameters' else {'title','type','bbox'}
    if not patch or set(patch)-allowed:raise ServiceError('核对包含不支持的字段。','review_invalid')
    cleaned={}
    for key,value in patch.items():
        if key=='value':cleaned[key]=_value(value)
        elif key in {'label','title'}:cleaned[key]=_string(value,160,empty=False)
        elif key=='unit':cleaned[key]=_string(value,40,nullable=True)
        elif key in {'datum','feature'}:cleaned[key]=_string(value,240,nullable=True)
        elif key=='note':cleaned[key]=_string(value,1000)
        elif key=='category':
            if not isinstance(value,str) or value not in CATEGORIES:raise ServiceError('参数分类无效。','review_invalid')
            cleaned[key]=value
        elif key=='direction':
            if not isinstance(value,str) or value not in DIRECTIONS:raise ServiceError('尺寸方向无效。','review_invalid')
            cleaned[key]=value
        elif key=='status':
            if not isinstance(value,str) or value not in REVIEW_STATUSES:raise ServiceError('核对状态无效。','review_invalid')
            cleaned[key]=value
        elif key=='region_id':
            if value is not None and (not isinstance(value,str) or value not in {r['id'] for r in base.get('regions',[])}):raise ServiceError('所选分区不属于本轮识别。','review_invalid')
            cleaned[key]=value
        elif key=='type':
            if not isinstance(value,str) or value not in REGION_TYPES:raise ServiceError('分区类型无效。','review_invalid')
            cleaned[key]=value
        elif key=='bbox':
            width=base['source']['width'];height=base['source']['height']
            if not isinstance(value,list) or len(value)!=4 or any(type(v) is not int for v in value) or value[0]<0 or value[1]<0 or value[2]>width or value[3]>height or value[2]-value[0]<3 or value[3]-value[1]<3:raise ServiceError('分区坐标须位于原图内且具有有效面积。','review_invalid')
            cleaned[key]=list(value)
    return {'type':kind,'id':identifier,'patch':cleaned},candidate

def _crop(service,record,region,revision,key):
    from PIL import Image
    directory=service._dir(record['job_id']);folder=directory/'review-crops';no_links(folder);folder.mkdir(mode=0o700,exist_ok=True)
    canonical,_=service.file(record['job_id'],'canonical.png')
    identifier=re.sub('[^a-z0-9.-]','-',region['id'].lower())[:24]
    name='rv-a'+str(record['attempt']).zfill(3)+'-r'+str(revision).zfill(4)+'-'+identifier+'-'+key[:8]+'.png'
    path=folder/name;no_links(path)
    with tempfile.NamedTemporaryFile(dir=folder,prefix='.crop-',suffix='.png',delete=False) as stream:temporary=Path(stream.name)
    try:
        with Image.open(canonical) as image:image.crop(tuple(region['bbox'])).save(temporary,format='PNG',optimize=False)
        temporary.chmod(0o600)
        occupied=sum((directory/entry['path']).stat().st_size for entry in record['files'].values() if entry['path'].startswith('review-crops/') and (directory/entry['path']).is_file())
        if occupied+temporary.stat().st_size>MAX_ASSET_BYTES:raise ServiceError('本任务核对裁片已达容量上限，请导出后新建任务。','review_storage_limit',409)
        if path.exists():
            if file_hash(path)!=file_hash(temporary):raise ServiceError('核对裁片已存在且内容不一致。','review_conflict',409)
        else:temporary.replace(path)
        service._register(record,name,path,'image/png')
        return service._file_url(record['job_id'],name)
    finally:temporary.unlink(missing_ok=True)

def save_review(service,job_id,payload):
    if not isinstance(payload,dict) or set(payload)-{'request_id','expected_revision','expected_attempt','action'}:raise ServiceError('核对请求格式无效。','review_invalid')
    request_id=payload.get('request_id');service._request_file(request_id)
    if type(payload.get('expected_revision')) is not int or type(payload.get('expected_attempt')) is not int:raise ServiceError('缺少本轮核对版本。','review_invalid')
    try:fingerprint=_fingerprint(payload)
    except (ValueError,TypeError,RecursionError):raise ServiceError('核对内容无法保存。','review_invalid') from None
    with service.lock:
        record=service._load(job_id);base=_manifest(record);state=_load(service,record,base)
        if payload['expected_attempt']!=record['attempt']:raise ServiceError('识别轮次已变化，请刷新后再核对。','review_attempt_conflict',409)
        key=hashlib.sha256(request_id.encode()).hexdigest();previous=state['requests'].get(key)
        if previous:
            if previous['fingerprint']!=fingerprint:raise ServiceError('请求编号已用于不同核对内容。','request_conflict',409)
            result=_public(record,base,state);result['replayed']=True;return result
        if payload['expected_revision']!=state['revision']:raise ServiceError('核对记录已更新，请刷新后再保存。','review_revision_conflict',409)
        if state['revision']>=MAX_REVISIONS:raise ServiceError('本轮核对记录已达上限，请导出保留记录。','review_limit',409)
        action,candidate=_action(payload,base);group='parameters' if action['type'] in {'parameter','reset_parameter'} else 'regions'
        target=action['id'];before_manifest=_effective(base,state);before=next(row for row in before_manifest[group] if row['id']==target)
        next_state=copy.deepcopy(state);revision=state['revision']+1
        patches=next_state['parameter_patches' if group=='parameters' else 'region_patches']
        if action['type'].startswith('reset_'):patches.pop(target,None)
        else:
            patch=copy.deepcopy(patches.get(target,{}));changed=action['patch'];patch.update(changed)
            if group=='parameters' and set(changed)-{'status','note'} and 'status' not in changed:patch['status']='pending_review'
            if group=='parameters' and changed.get('status') in {'checked','questioned'}:patch.pop('review_invalidated_by_region',None)
            if group=='regions' and 'bbox' in changed:
                row=copy.deepcopy(before);row.update(changed);patch['file']=_crop(service,record,row,revision,key);patch['status']='pending_review'
            patches[target]=patch
        next_state['revision']=revision;after=next(row for row in _effective(base,next_state)[group] if row['id']==target)
        invalidated=[]
        if group=='regions' and before.get('bbox')!=after.get('bbox'):
            for row in before_manifest['parameters']:
                if row.get('status')!='checked' or not any(ev.get('region_id')==target for ev in row.get('evidence',[])):continue
                parameter_patch=copy.deepcopy(next_state['parameter_patches'].get(row['id'],{}))
                parameter_patch.update(status='pending_review',review_invalidated_by_region={'region_id':target,'revision':revision})
                next_state['parameter_patches'][row['id']]=parameter_patch
                invalidated.append({'id':row['id'],'before':row,'reason':'关联分区的裁图边界已调整，需重新核对。'})
            final_parameters={row['id']:row for row in _effective(base,next_state)['parameters']}
            for item in invalidated:item['after']=final_parameters[item['id']]
        event={'revision':revision,'attempt':record['attempt'],'created_at':now(),'actor':'当前页面操作者（身份未提供）','action':action,'before':before,'after':after,'original_candidate':copy.deepcopy(candidate),'request_fingerprint':fingerprint,'engineering_approved':False}
        if invalidated:event['invalidated_parameters']=invalidated
        folder=service._dir(job_id)/'review-events';no_links(folder);folder.mkdir(mode=0o700,exist_ok=True)
        event_path=folder/('a'+str(record['attempt']).zfill(3)+'-r'+str(revision).zfill(4)+'-'+key[:16]+'.json');no_links(event_path)
        if event_path.exists():
            saved=json.loads(event_path.read_text())
            if saved.get('request_fingerprint')!=fingerprint:raise ServiceError('核对记录冲突，请刷新重试。','review_conflict',409)
            event=saved
        else:atomic_json(event_path,event)
        next_state['history'].append(event);next_state['requests'][key]={'fingerprint':fingerprint,'revision':revision}
        if len(json.dumps(next_state,ensure_ascii=False,allow_nan=False).encode())>24*1024*1024:
            raise ServiceError('核对记录已达容量上限，请先导出保留记录。','review_storage_limit',409)
        current=service._dir(job_id)/'review.json';no_links(current)
        if current.exists():
            old=json.loads(current.read_text())
            if old.get('attempt')!=record['attempt']:
                archive=folder/('previous-a'+str(old.get('attempt',0)).zfill(3)+'-r'+str(old.get('revision',0)).zfill(4)+'.json');no_links(archive)
                if not archive.exists():atomic_json(archive,old)
        # Registration preceding state commit cannot mutate the AI manifest or
        # make an overlay appear successful if the state write fails.
        service._save(record);atomic_json(current,next_state)
        return _public(record,base,next_state)

def _csv_cell(value):
    if value is None:return ''
    text=json.dumps(value,ensure_ascii=False) if isinstance(value,(list,dict)) else str(value)
    if text.lstrip().startswith(('=','+','-','@')) or text.startswith(('\t','\n','\r')):text="'"+text
    return text

def _parameter_csv(manifest):
    output=io.StringIO(newline='');writer=csv.writer(output)
    writer.writerow(['参数ID','名称','值','单位','分类','方向','基准','对象或特征','核对状态','关联分区','原始标注','人工备注'])
    regions={r['id']:r.get('title',r['id']) for r in manifest['regions']}
    for row in manifest['parameters']:
        values=[row.get('id'),row.get('label'),row.get('value'),row.get('unit'),row.get('category'),row.get('direction'),row.get('datum'),row.get('feature'),row.get('status'),'; '.join(regions.get(e.get('region_id'),e.get('region_id','')) for e in row.get('evidence',[])),'\n'.join(e.get('raw_text','') for e in row.get('original_evidence',row.get('evidence',[]))),row.get('note')]
        writer.writerow([_csv_cell(v) for v in values])
    return output.getvalue().encode('utf-8-sig')

def export_review(service,job_id,revision,attempt):
    if type(revision) is not int or type(attempt) is not int:raise ServiceError('请明确要导出的核对版本。','review_invalid')
    with service.lock:
        record=service._load(job_id);base=_manifest(record);state=_load(service,record,base)
        if state['revision']!=revision or record['attempt']!=attempt:raise ServiceError('识别或核对版本已变化，请刷新后导出。','review_revision_conflict',409)
        name='drawing-packet-a'+str(attempt).zfill(3)+'-r'+str(revision).zfill(4)+'.zip'
        cache='packet-a'+str(attempt).zfill(3)+'-r'+str(revision).zfill(4)+'-'+state['base_sha256'][:10]+'.zip'
        if cache in record['files']:
            path,mime=service.file(job_id,cache);return path,mime,name
        manifest=_effective(base,state);packet=copy.deepcopy(manifest)
        packet.update(job_id=job_id,history=copy.deepcopy(state['history']),model=copy.deepcopy(record.get('model')),engineering_approved=False,model_called=False)
        paths=[];total=0
        for row in packet['regions']:
            basename=row['file'].rsplit('/',1)[-1];path,_=service.file(job_id,basename)
            destination='regions/'+re.sub('[^a-zA-Z0-9._-]','-',row['id'])+'.png';row['file']=destination;paths.append((path,destination));total+=path.stat().st_size
        original,_=service.file(job_id,record['original_file']);original_name='source/'+record['original_file'];paths.append((original,original_name));total+=original.stat().st_size
        canonical,_=service.file(job_id,'canonical.png');paths.append((canonical,'source/canonical.png'));total+=canonical.stat().st_size
        packet['source']['image']='source/canonical.png';packet['source']['original_image']=original_name
        json_bytes=json.dumps(packet,ensure_ascii=False,indent=2,allow_nan=False).encode();csv_bytes=_parameter_csv(packet)
        if total+len(json_bytes)+len(csv_bytes)>MAX_EXPORT_BYTES:raise ServiceError('资料包超过大小上限，请分别下载原图与裁片。','export_too_large',413)
        folder=service._dir(job_id)/'exports';no_links(folder);folder.mkdir(mode=0o700,exist_ok=True);target=folder/cache;no_links(target)
        with tempfile.NamedTemporaryFile(dir=folder,prefix='.packet-',suffix='.zip',delete=False) as stream:temporary=Path(stream.name)
        try:
            with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_STORED,allowZip64=False) as archive:
                for path,destination in paths:archive.write(path,destination)
                archive.writestr('parameters.csv',csv_bytes,compress_type=zipfile.ZIP_DEFLATED)
                archive.writestr('drawing-regions.json',json_bytes,compress_type=zipfile.ZIP_DEFLATED)
                archive.writestr('README.txt','原图与分区来自本次上传资料。参数为AI候选及人工核对记录；单项核对不等于工程审核或生产批准。视图数量不代表制造数量。\n'.encode('utf-8'),compress_type=zipfile.ZIP_DEFLATED)
            temporary.chmod(0o600)
            if target.exists():raise ServiceError('资料包缓存尚未完成登记，请联系维护人员。','export_conflict',409)
            temporary.replace(target);service._register(record,cache,target,'application/zip');service._save(record)
        finally:temporary.unlink(missing_ok=True)
        return target,'application/zip',name
