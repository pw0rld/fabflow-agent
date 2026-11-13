"""Conservative reading checks, never geometry reconstruction or approval.

These hints also annotate old saved results without changing their originals.
Labels may themselves be wrong; direction/datum are never invented from labels.
"""
from __future__ import annotations
from copy import deepcopy
import re

CATEGORIES={'identity','material','thickness','outer_size','dimension','hole','slot','bend','surface','weld','quantity','tolerance','other'}
DIRECTIONS={'unknown','horizontal','vertical','diameter','radial','angle','not_applicable'}
DIMENSION_CATEGORIES={'outer_size','dimension','hole','slot','bend'}

def short_text(value,limit=240):
    return value.strip()[:limit] if isinstance(value,str) and value.strip() else None

def category_hint(label):
    label=str(label or '').lower()
    patterns=[
        ('thickness',r'板厚|料厚|材料厚|plate thickness|sheet thickness'),
        ('material',r'材料|材质|牌号|material|alloy'),
        ('surface',r'表面|涂层|喷粉|镀锌|酸洗|钝化|拉丝|finish|coat'),
        ('weld',r'焊|weld'),('bend',r'折弯|折边|弯曲|bend'),
        ('tolerance',r'公差|tolerance'),('hole',r'孔|螺纹|hole|thread'),
        ('slot',r'槽|开口|slot|cut.?out'),
        ('outer_size',r'外形|整体|总宽|总长|总高|overall'),
        ('dimension',r'尺寸|距离|间距|宽度|长度|高度|半径|直径|圆角|角度|横向|纵向|dimension|radius|diameter'),
        ('quantity',r'数量|件数|quantity|qty'),
        ('identity',r'图号|版本|名称|图名|比例|单位|drawing.?no|revision|scale|title'),
    ]
    for category,pattern in patterns:
        if re.search(pattern,label):return category
    return 'other'

def intersection(a,b):
    if not all(isinstance(box,list) and len(box)==4 and all(isinstance(v,(int,float)) and not isinstance(v,bool) for v in box) for box in (a,b)):return 0
    return max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))

def annotate_parameter(item,regions):
    row=deepcopy(item)
    category=row.get('category')
    if category not in CATEGORIES:
        category=category_hint(row.get('label'))
        row['category_source']='label_hint'
    else:row.setdefault('category_source','model')
    row['category']=category
    direction=row.get('direction')
    if direction not in DIRECTIONS:
        direction='unknown';row['direction_source']='missing'
    else:row.setdefault('direction_source','model')
    row['direction']=direction
    row['datum']=short_text(row.get('datum'))
    row['feature']=short_text(row.get('feature'))
    evidence=row.get('evidence') if isinstance(row.get('evidence'),list) else []
    precision=['annotation' if isinstance(ev,dict) and isinstance(ev.get('bbox'),list) and len(ev['bbox'])==4 else 'region' for ev in evidence]
    row['location_precision']='annotation' if precision and all(x=='annotation' for x in precision) else 'mixed' if 'annotation' in precision else 'region'
    checks=[]
    def check(code,message):checks.append({'code':code,'message':message})
    if row.get('value') is None:check('value_missing','尚未读到明确值，不能以零代替。')
    if row.get('basis')=='uncertain':check('uncertain_reading','原识别结果表示不确定，请结合原图核对。')
    if isinstance(row.get('value'),list) and len(row['value'])>1 and category in DIMENSION_CATEGORIES:
        check('multiple_values','本项有多个数值，请逐项核对对象、方向和尺寸基准，必要时分别记录。')
    if category in {'dimension','outer_size'} and direction=='unknown':
        check('direction_missing','尺寸方向未明确，未仅凭参数名称自动采用方向。')
    if category in DIMENSION_CATEGORIES and not row.get('unit'):
        check('unit_missing','本项单位未明确，尚未自动换算。')
    if category in {'hole','slot','dimension'} and re.search(r'孔距|坐标|中心|边距|分段|基准',str(row.get('label',''))) and not row['datum']:
        check('datum_missing','孔位或分段尺寸缺少明确基准，不能据此建立加工坐标。')
    if category=='quantity':check('quantity_scope','请区分单件特征数量、每套用量和订单数量。')
    mapping={r.get('id'):r for r in regions if isinstance(r,dict)}
    refs=list(dict.fromkeys(ev.get('region_id') for ev in evidence if isinstance(ev,dict)))
    selected=[mapping[r] for r in refs if r in mapping and mapping[r].get('type') in {'orthographic','detail','flat_pattern'}]
    if category in DIMENSION_CATEGORIES and any(intersection(a.get('bbox'),b.get('bbox'))>0 for i,a in enumerate(selected) for b in selected[i+1:]):
        check('overlapping_view_sources','本项关联的视图区域有重叠，请检查原文是否来自相邻视图。')
    if evidence and row['location_precision']!='annotation':
        check('region_location_only','部分依据仅定位到来源区域，具体尺寸标注位置尚未定位。')
    row['checks']=checks
    return row

def annotate_manifest(manifest):
    result=deepcopy(manifest)
    regions=result.get('regions',[])
    result['parameters']=[annotate_parameter(row,regions) for row in result.get('parameters',[]) if isinstance(row,dict)]
    result['engineering_approved']=False
    return result
