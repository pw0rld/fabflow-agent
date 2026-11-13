"""Source-bound model observations. No sample defaults or engineering approval."""
from __future__ import annotations
import math
from .semantic import annotate_manifest,CATEGORIES,DIRECTIONS

REGION_TYPES={'orthographic','flat_pattern','isometric','process_table','technical_notes','title_block','detail'}
TECHNICAL_TYPES={'process_table','technical_notes','title_block'}
class ContractError(ValueError):pass

def text(v,limit=1000):
    return str(v).strip()[:limit] if isinstance(v,(str,int,float)) and not isinstance(v,bool) else ''

def finite(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)

def pixel_box(v,width,height,system,warnings,context):
    if not isinstance(v,list) or len(v)!=4 or not all(finite(x) for x in v):
        warnings.append(context+'：坐标无效，未采用。');return None
    limits=[1000]*4 if system=='normalized_1000' else [width,height,width,height]
    tolerance=10 if system=='normalized_1000' else 2
    if any(x < -tolerance or x > limit+tolerance for x,limit in zip(v,limits)):
        warnings.append(context+'：坐标超出原图，未采用。');return None
    box=[max(0,min(float(x),limit)) for x,limit in zip(v,limits)]
    if box!=v:warnings.append(context+'：轻微越界坐标已收回原图边界。')
    if box[0]>=box[2] or box[1]>=box[3]:
        warnings.append(context+'：区域没有有效面积。');return None
    if system=='normalized_1000':box=[box[0]*width/1000,box[1]*height/1000,box[2]*width/1000,box[3]*height/1000]
    box=[math.floor(box[0]),math.floor(box[1]),math.ceil(box[2]),math.ceil(box[3])]
    box=[max(0,min(x,limit)) for x,limit in zip(box,[width,height,width,height])]
    if box[2]-box[0]<3 or box[3]-box[1]<3:
        warnings.append(context+'：区域过小，未裁图。');return None
    return box

def safe_value(v,depth=0):
    if v is None or isinstance(v,bool):return v
    if isinstance(v,str):return v[:2000]
    if finite(v):return v
    if isinstance(v,list) and depth==0 and len(v)<=24:return [safe_value(x,1) for x in v]
    return None

def normalize(raw,*,width,height,source,files_base,filename_prefix=''):
    if not isinstance(raw,dict):raise ContractError('模型未返回有效分图结构。')
    system=raw.get('coordinate_system')
    if system not in {'normalized_1000','source_pixels'}:raise ContractError('模型未明确坐标体系，未用于裁图。')
    warnings=[text(v) for v in raw.get('warnings',[])[:64] if text(v)] if isinstance(raw.get('warnings'),list) else []
    parts=[];part_map={}
    for item in raw.get('parts',[])[:48] if isinstance(raw.get('parts'),list) else []:
        if not isinstance(item,dict) or not text(item.get('name'),120):continue
        original=text(item.get('id'),120)
        if not original or original in part_map:
            warnings.append('零件身份为空或重复，未自动归属。');continue
        identifier='p'+str(len(parts)+1).zfill(3);part_map[original]=identifier
        parts.append({'id':identifier,'name':text(item['name'],120),'raw_text':text(item.get('raw_text')),'status':'pending_review','manufacturing_quantity':None})
    inputs=raw.get('regions',[])
    if not isinstance(inputs,list):raise ContractError('模型未返回区域列表。')
    regions=[];region_map={}
    if len(inputs)>48:warnings.append('区域超过上限，仅整理前48个。')
    for index,item in enumerate(inputs[:48],1):
        if not isinstance(item,dict):continue
        original=text(item.get('id'),120)
        if not original or original in region_map:
            warnings.append('区域身份为空或重复，未采用。');continue
        box=pixel_box(item.get('bbox'),width,height,system,warnings,'区域'+str(index))
        if box is None:continue
        identifier='r'+str(len(regions)+1).zfill(3);region_map[original]=identifier
        kind=item.get('type')
        if kind not in REGION_TYPES:kind='detail';warnings.append('未知区域分类保留为局部详图。')
        original_part=text(item.get('part_id'),120);part_id=part_map.get(original_part)
        if original_part and not part_id:warnings.append('区域零件引用无法核实，保留为未归属。')
        regions.append({'id':identifier,'title':text(item.get('title'),120) or '图纸区域'+str(index),'type':kind,'bbox':box,'file':files_base+filename_prefix+'region-'+identifier+'.png','part_id':part_id,'description':text(item.get('description')),'status':'pending_review'})
    region_boxes={region['id']:region['bbox'] for region in regions}
    def evidence(items):
        result=[]
        for item in items[:24] if isinstance(items,list) else []:
            if not isinstance(item,dict):continue
            region_id=region_map.get(text(item.get('region_id'),120));original=text(item.get('raw_text'),2000)
            if not region_id or not original:
                warnings.append('一条依据缺少有效区域或原文，未自动关联。');continue
            row={'region_id':region_id,'raw_text':original}
            if item.get('bbox') is not None:
                box=pixel_box(item['bbox'],width,height,system,warnings,'参数依据')
                region_box=region_boxes[region_id]
                if box is not None:
                    intersects=box[0]<region_box[2] and box[2]>region_box[0] and box[1]<region_box[3] and box[3]>region_box[1]
                    if intersects:row['bbox']=box
                    else:warnings.append('一条依据坐标与引用区域不相交，保留原文但未采用该坐标。')
            result.append(row)
        return result
    parameters=[]
    for index,item in enumerate(raw.get('parameters',[])[:256] if isinstance(raw.get('parameters'),list) else [],1):
        if not isinstance(item,dict):continue
        ev=evidence(item.get('evidence'));label=text(item.get('label'),120)
        if not ev:
            warnings.append('一项参数没有可关联原文依据，未作为规范候选采用。');continue
        if not label:continue
        basis=item.get('basis') if item.get('basis') in {'drawing_text','derived','uncertain'} else 'uncertain'
        parameter={'id':'v'+str(index).zfill(3),'label':label,'value':safe_value(item.get('value')),'unit':text(item.get('unit'),40) or None,'status':'pending_review','basis':basis,'evidence':ev,'reason':text(item.get('reason'))}
        if item.get('category') in CATEGORIES:parameter['category']=item['category'];parameter['category_source']='model'
        if item.get('direction') in DIRECTIONS:parameter['direction']=item['direction'];parameter['direction_source']='model'
        for key in ('datum','feature'):parameter[key]=text(item.get(key),240) or None
        parameters.append(parameter)
    process=[]
    for index,item in enumerate(raw.get('process_notes',[])[:128] if isinstance(raw.get('process_notes'),list) else [],1):
        if not isinstance(item,dict):continue
        ev=evidence(item.get('evidence'));description=text(item.get('text'),2000)
        if ev and description:process.append({'id':'n'+str(index).zfill(3),'title':text(item.get('title'),120) or '工艺说明','text':description,'status':'pending_review','evidence':ev})
    bends=[]
    for item in raw.get('bend_rows',[])[:96] if isinstance(raw.get('bend_rows'),list) else []:
        if not isinstance(item,dict):continue
        ev=evidence(item.get('evidence'))
        if ev:bends.append({'tag':text(item.get('tag'),40),'direction':text(item.get('direction'),40),'angle_deg':item.get('angle_deg') if finite(item.get('angle_deg')) else None,'inner_radius_mm':item.get('inner_radius_mm') if finite(item.get('inner_radius_mm')) else None,'status':'pending_review','evidence':ev})
    notes=[text(v) for v in raw.get('review_notes',[])[:64] if text(v)] if isinstance(raw.get('review_notes'),list) else []
    notes+=['AI分区及参数尚待工程核对。','多个视图不等于多个制造零件；订单数量未自动推定。']
    if not regions:warnings.append('本轮没有可用图纸分区，可查看原件或手工框选。')
    if not parts:warnings.append('未确认零件身份，区域暂不计为独立零件。')
    complete=raw.get('analysis_complete',True) is not False
    kind=raw.get('document_kind') if raw.get('document_kind') in {'engineering_drawing','not_drawing','uncertain'} else 'uncertain'
    if not regions and kind!='not_drawing':complete=False
    return annotate_manifest({'schema_version':1,'mode':'ai_regions','title':text(raw.get('title'),160) or source['filename'],'document_kind':kind,'source':source,'part':parts[0] if len(parts)==1 else None,'parts':parts,'regions':regions,'parameters':parameters,'process_notes':process,'bend_rows':bends,'relationships':{'view_region_ids':[r['id'] for r in regions if r['type'] not in TECHNICAL_TYPES],'supplement_region_ids':[r['id'] for r in regions if r['type'] in TECHNICAL_TYPES]},'warnings':list(dict.fromkeys(warnings))[:128],'review_notes':notes,'analysis_complete':complete,'engineering_approved':False})
