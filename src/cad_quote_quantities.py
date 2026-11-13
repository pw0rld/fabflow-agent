"""Bounded CAD quantities for explicitly reviewed quotation estimates.

Manufacturing profiles remain governed by cad_laser/cad_bends. A folded-part
inventory below is neither a developed pattern nor a machine program. Native
surface/edge/topology measurements and the adopted estimating assumptions are
retained separately, without deriving processing fees from mass.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
import re
import time

VERSION = 'cad-quote-quantities-v1'
MAX_FACES = 512
MAX_EDGES = 4096
MAX_SECONDS = 4.0
MAX_PAIRS = 16000
TOL = 1e-5
QUANTITIES = ('material_area_mm2', 'material_gross_area_mm2', 'cut_length_mm',
              'pierce_count', 'bend_count', 'bend_length_mm', 'thickness_mm',
              'blank_width_mm', 'blank_height_mm')
REQUIRED = ('material_area_mm2', 'cut_length_mm', 'pierce_count', 'bend_count', 'thickness_mm')


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        if len(str(value))>128:return None
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or result > Decimal('1e18') or positive and result == 0:
            return None
        if result and result.adjusted() < -12:return None
        return result
    except InvalidOperation:
        return None


def _scalar(value):
    if value is None:
        return None
    out = format(Decimal(str(value)).quantize(Decimal('.00000001'), rounding=ROUND_HALF_UP), 'f')
    return out.rstrip('0').rstrip('.') if '.' in out else out


def _empty(solid_id, source_sha256, reason):
    return {'schema_version': 1, 'version': VERSION, 'solid_id': solid_id,
            'source_sha256': source_sha256, 'unit': 'mm', 'status': 'unresolved',
            'method': 'native_brep_inventory', 'manufacturing_release': False,
            **{name: None for name in QUANTITIES},
            'quantity_statuses': {name: 'unresolved' for name in QUANTITIES},
            'source_face_ids': [], 'assumptions': [], 'warnings': [reason],
            'measurements': {}, 'thickness_basis': {},
            'unfolding': {'status': 'not_performed', 'production_pattern': False,
                          'reason': '本证据用于报价估算，尚未完成生产用展开。'}}


def _adopt_thickness(measurement):
    if not isinstance(measurement, dict):
        return None, {}, []
    values, support = {}, defaultdict(dict)
    for item in measurement.get('evidence', []):
        if not isinstance(item, dict):
            continue
        value, area = _number(item.get('thickness_mm'), positive=True), _number(item.get('support_area_mm2'), positive=True)
        ids = item.get('face_ids')
        if value is None or area is None or not isinstance(ids, list) or len(ids) != 2:
            continue
        key = round(float(value), 3)
        values[key] = float(value)
        for fid in ids:
            support[key][fid] = max(float(area), support[key].get(fid, 0.0))
    direct = _number(measurement.get('value_mm'), positive=True)
    if measurement.get('status') == 'candidate' and direct is not None:
        adopted, source = float(direct), 'native_uniform_candidate'
    elif values:
        key = min(values, key=lambda val: (-sum(support[val].values()), val))
        adopted, source = values[key], 'dominant_local_measurement_assumption'
    else:
        return None, {}, []
    return adopted, {'source': source, 'adopted_for_estimation': True,
                     'native_status': measurement.get('status'),
                     'candidates_mm': [_scalar(values[key]) for key in sorted(values)]}, [
        '报价采用有原生面证据支持的厚度；该采用不改写CAD统一板厚结论。',
        '局部混厚、圆角或实体组合仍需工程复核；采用厚度仅服务本次估价。']


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _sub(a, b):
    return tuple(x-y for x, y in zip(a, b))


def _unit(a):
    length = math.sqrt(_dot(a, a))
    return tuple(v/length for v in a) if length > 1e-12 else None


def _xyz(value):
    return (value.X(), value.Y(), value.Z())


def _cycle_rank(edges):
    parents={}
    def root(node):
        parents.setdefault(node,node)
        while parents[node]!=node:
            parents[node]=parents[parents[node]];node=parents[node]
        return node
    cycles=0
    for first,second in edges:
        a,b=root(first),root(second)
        if a==b:cycles+=1
        else:parents[a]=b
    return cycles


def measure_quote_quantities(shape, solid, source_sha256, *, faces=None, deadline=None):
    """Read native BRep quantities; imports and computation run in CAD worker only."""
    sid = solid.get('id')
    result = _empty(sid, source_sha256, '尚无可用于完整估价的原生工程量。')
    stop = min(deadline or float('inf'), time.monotonic()+MAX_SECONDS)
    def check():
        if time.monotonic() > stop:
            raise ValueError('报价量测已达到本轮原生计算预算。')
    topology = solid.get('topology') or {}
    if topology.get('valid') is not True or topology.get('closed') is not True:
        result['warnings'] = ['实体未通过有效封闭校验，不能用本估量生成完整报价。']
        return result
    try:
        # Reuse proven flat profiles, including every outer/hole contour. This
        # does not turn a folded-solid projection into a planar profile.
        from batch_process_evidence import summarise_native_processes
        strict = summarise_native_processes(solid)
        laser = strict['laser_cut']
        if laser.get('status') == 'ready':
            result.update(status='measured', method='native_flat_profile', warnings=[],
                material_area_mm2=laser['net_area_mm2'],
                material_gross_area_mm2=_scalar(Decimal(laser['blank_width_mm'])*Decimal(laser['blank_height_mm'])),
                cut_length_mm=laser['cut_length_mm'], pierce_count=laser['pierce_count'],
                bend_count=0, bend_length_mm='0', thickness_mm=_scalar(laser['thickness_mm']),
                blank_width_mm=laser['blank_width_mm'], blank_height_mm=laser['blank_height_mm'],
                source_face_ids=[laser['source_face_id']],
                quantity_statuses={name: 'measured' for name in QUANTITIES},
                assumptions=['每个完整闭合轮廓暂按一次激光穿孔；不是机床刀路。'],
                measurements={'native_volume_mm3': _scalar(solid.get('volume_mm3'))},
                thickness_basis={'source': 'native_flat_prism', 'adopted_for_estimation': True},
                unfolding={'status': 'not_required_flat', 'production_pattern': False,
                           'reason': '已验证原生平板截面；生产采用及排版仍需工程确认。'})
            return result
        adopted, thickness_basis, assumptions = _adopt_thickness(solid.get('thickness'))
        area, volume = _number(solid.get('area_mm2'), positive=True), _number(solid.get('volume_mm3'), positive=True)
        if adopted is None or area is None or volume is None:
            result['warnings'] = ['没有受支持的局部板厚、原生面积或体积，保持工程量缺失。']
            return result
        ratio = float(area)*adopted/(2*float(volume))
        if not .35 <= ratio <= 2.5:
            result['warnings'] = ['局部厚度与原生面积/体积严重不一致，不能按薄板代理估价。']
            return result
        from OCP.BRep import BRep_Tool
        from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
        from OCP.BRepClass import BRepClass_FaceClassifier
        from OCP.BRepClass3d import BRepClass3d_SolidClassifier
        from OCP.BRepGProp import BRepGProp
        from OCP.BRepTools import BRepTools
        from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Line
        from OCP.GProp import GProp_GProps
        from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX, TopAbs_WIRE, TopAbs_SHELL, TopAbs_IN, TopAbs_REVERSED
        from OCP.TopExp import TopExp
        from OCP.TopoDS import TopoDS
        from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
        from OCP.gp import gp_Pnt, gp_Vec
        def mapped(obj, kind):
            table = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
            TopExp.MapShapes_s(obj, kind, table)
            return table
        faces = faces if faces is not None else mapped(shape, TopAbs_FACE)
        edges = mapped(shape, TopAbs_EDGE)
        if not 0 < faces.Extent() <= MAX_FACES or edges.Extent() > MAX_EDGES:
            raise ValueError('报价面/边量测超出本轮范围。')
        lengths, effective_edges, edge_lengths, straight_edges = [], 0, {}, set()
        for index in range(1, edges.Extent()+1):
            check(); edge = TopoDS.Edge(edges.FindKey(index))
            if BRep_Tool.Degenerated_s(edge):
                continue
            props = GProp_GProps(); BRepGProp.LinearProperties_s(edge, props, False, False)
            length = props.Mass()
            if not math.isfinite(length) or length <= 0:
                raise ValueError('原生边长无效，不能完整计量。')
            effective_edges += 1; lengths.append(length); edge_lengths[index] = length
            if BRepAdaptor_Curve(edge).GetType() == GeomAbs_Line:
                straight_edges.add(index)
        check()
        face_cells, cylinders, planes, edge_faces = 0, [], {}, defaultdict(list)
        for index in range(1, faces.Extent()+1):
            check(); face = TopoDS.Face(faces.FindKey(index)); fid = f'F{index:04d}'
            wires = mapped(face, TopAbs_WIRE).Extent()
            if wires < 1:
                raise ValueError('闭合曲面缺少可核验的面边界，通道计量保持未知。')
            face_cells += 2-wires
            surface = BRepAdaptor_Surface(face, True)
            local_edges=mapped(face,TopAbs_EDGE)
            edge_ids=[]
            for j in range(1,local_edges.Extent()+1):
                eid=edges.FindIndex(local_edges.FindKey(j));edge_ids.append(eid);edge_faces[eid].append(fid)
            if surface.GetType() == GeomAbs_Plane:
                props=GProp_GProps();BRepGProp.SurfaceProperties_s(face,props,False,False)
                planes[fid]={'normal':_xyz(surface.Plane().Axis().Direction()),'area':props.Mass(),
                    'edge_ids':edge_ids,'longest_edge':max((edge_lengths.get(eid,0) for eid in edge_ids),default=0)}
            if surface.GetType() != GeomAbs_Cylinder:
                continue
            cylinder = surface.Cylinder(); axis = _xyz(cylinder.Axis().Direction())
            for component in axis:
                if abs(component) > 1e-10:
                    if component < 0: axis = tuple(-x for x in axis)
                    break
            point = _xyz(cylinder.Location())
            uv = BRepTools.UVBounds_s(face); angle = abs(uv[1]-uv[0])
            if not math.radians(5) <= angle <= math.pi+1e-5:
                continue
            values = [_dot(_xyz(surface.Value((uv[0]+uv[1])/2, v)), axis) for v in uv[2:]]
            if max(values)-min(values) < 5*adopted:
                continue
            mid_u,mid_v=(uv[0]+uv[1])/2,(uv[2]+uv[3])/2
            point3,du,dv=gp_Pnt(),gp_Vec(),gp_Vec();surface.D1(mid_u,mid_v,point3,du,dv)
            sign=-1 if face.Orientation()==TopAbs_REVERSED else 1
            cross=du.Crossed(dv);normal=_unit(tuple(sign*x for x in _xyz(cross)))
            delta=_sub(_xyz(point3),point);radial=_unit(_sub(delta,tuple(x*_dot(delta,axis) for x in axis)))
            if normal is None or radial is None:continue
            cylinders.append({'face':face,'surface':surface,'id':fid,'radius':cylinder.Radius(),
                'axis':axis,'point':point,'axial':sorted(values),'uv':uv,
                'edge_ids':edge_ids,'radial_sign':_dot(normal,radial)})
        vertex_count = mapped(shape, TopAbs_VERTEX).Extent()
        shell_count = mapped(shape, TopAbs_SHELL).Extent()
        characteristic = vertex_count-effective_edges+face_cells
        channels_twice = 2*shell_count-characteristic
        channels = channels_twice//2 if shell_count == 1 and channels_twice >= 0 and channels_twice%2 == 0 else None
        if channels is not None and channels > 1023:
            channels = None
        # A sharp CAD fold has no inner/outer circular bands. Require two
        # independently paired, broad planar skins meeting on a real long
        # straight edge. This only supports a declared fold-route assumption,
        # never a radius, K-factor, direction, developed length or cut pattern.
        skin_pairs={};face_skins=defaultdict(set)
        for evidence in (solid.get('thickness') or {}).get('evidence',[]):
            if evidence.get('kind')!='parallel_faces':continue
            ids=evidence.get('face_ids',[]);local_t=_number(evidence.get('thickness_mm'),positive=True)
            if local_t is None or len(ids)!=2 or any(fid not in planes for fid in ids):continue
            local_t=float(local_t)
            if any(planes[fid]['longest_edge']<=0 or planes[fid]['area']/planes[fid]['longest_edge']<2.5*local_t for fid in ids):continue
            key=tuple(sorted(ids));skin_pairs[key]={'face_ids':list(key),'thickness_mm':local_t}
            for fid in ids:face_skins[fid].add(key)
        junctions={}
        for eid,fids in edge_faces.items():
            check()
            if eid not in straight_edges or len(set(fids))!=2:continue
            first,second=sorted(set(fids))
            if len(face_skins[first])!=1 or len(face_skins[second])!=1:continue
            first_skin=next(iter(face_skins[first]));second_skin=next(iter(face_skins[second]))
            if first_skin==second_skin:continue
            cosine=abs(_dot(planes[first]['normal'],planes[second]['normal']))
            if cosine>math.cos(math.radians(15)):continue
            thick=max(skin_pairs[first_skin]['thickness_mm'],skin_pairs[second_skin]['thickness_mm'])
            if edge_lengths.get(eid,0)<5*thick:continue
            key=tuple(sorted((first_skin,second_skin)))
            row=junctions.setdefault(key,{'kind':'sharp_fold_junction','face_ids':sorted(set(first_skin+second_skin)),
                'edge_ids':[],'length_mm':0.0,'angle_deg':math.degrees(math.acos(min(1,cosine)))})
            row['edge_ids'].append(f'E{eid:04d}')
            row['length_mm']=max(row['length_mm'],edge_lengths[eid])
        classifier = BRepClass3d_SolidClassifier(shape)
        pairs, pairs_checked = [], 0
        for i, first in enumerate(cylinders):
            for second in cylinders[i+1:]:
                check(); pairs_checked += 1
                if pairs_checked > MAX_PAIRS:
                    raise ValueError('曲带配对已达到本轮预算。')
                if abs(_dot(first['axis'], second['axis'])) < 1-1e-8:
                    continue
                inner, outer = sorted((first, second), key=lambda row: row['radius'])
                if inner['radial_sign']>-.99 or outer['radial_sign']<.99:continue
                if abs(outer['radius']-inner['radius']-adopted) > max(.001, adopted*.001):
                    continue
                if not adopted*.01 < inner['radius'] <= adopted*20:
                    continue
                shift = _sub(outer['point'],inner['point']); axis=inner['axis']
                perpendicular = _sub(shift,tuple(x*_dot(shift,axis) for x in axis))
                if math.sqrt(_dot(perpendicular,perpendicular)) > TOL:
                    continue
                lo=max(inner['axial'][0],outer['axial'][0]); hi=min(inner['axial'][1],outer['axial'][1])
                if hi-lo < 5*adopted:
                    continue
                accepted = False
                for fraction in (.5,.25,.75):
                    check(); u0,u1,v0,v1=inner['uv']; u=u0+(u1-u0)*fraction; v=(v0+v1)/2
                    p=inner['surface'].Value(u,v); a=_xyz(p)
                    center=tuple(inner['point'][j]+axis[j]*_dot(_sub(a,inner['point']),axis) for j in range(3))
                    radial=_unit(_sub(a,center))
                    if radial is None: continue
                    q=tuple(center[j]+radial[j]*outer['radius'] for j in range(3))
                    if BRepClass_FaceClassifier(inner['face'],p,TOL).State()!=TopAbs_IN or BRepClass_FaceClassifier(outer['face'],gp_Pnt(*q),TOL).State()!=TopAbs_IN:
                        continue
                    # Projected samples must straddle actual metal, not an empty
                    # cavity between unrelated coaxial surfaces.
                    occupied=True
                    for weight in (.2,.5,.8):
                        check(); sample=tuple(a[j]+weight*(q[j]-a[j]) for j in range(3))
                        classifier.Perform(gp_Pnt(*sample),TOL)
                        if classifier.State()!=TopAbs_IN: occupied=False;break
                    if occupied: accepted=True;break
                if not accepted: continue
                origin=tuple(inner['point'][j]-axis[j]*_dot(inner['point'],axis) for j in range(3))
                key=tuple(round(x,4) for x in (*axis,*origin,inner['radius'],outer['radius']))
                pairs.append({'key':key,'start':lo,'end':hi,'face_ids':[inner['id'],outer['id']]})
        groups=defaultdict(list)
        for pair in pairs:groups[pair['key']].append(pair)
        cycle_rank=_cycle_rank(junctions)
        known_candidates=len(groups)+len(junctions)
        nonuniform=ratio<.8 or ratio>1.35
        bend_count=known_candidates if known_candidates and cycle_rank==0 and not nonuniform else None
        bend_length=0.0;cylindrical_groups=[]
        for key,items in groups.items():
            intervals=sorted((item['start'],item['end']) for item in items); merged=[]
            for lo,hi in intervals:
                if merged and lo<=merged[-1][1]+TOL:merged[-1][1]=max(merged[-1][1],hi)
                else:merged.append([lo,hi])
            group_length=sum(hi-lo for lo,hi in merged);bend_length+=group_length
            cylindrical_groups.append({'kind':'paired_cylindrical_bands',
                'face_ids':sorted({fid for item in items for fid in item['face_ids']}),
                'length_mm':_scalar(group_length),'axis_direction':list(key[:3]),
                'inner_radius_mm':_scalar(key[-2]),'outer_radius_mm':_scalar(key[-1])})
        bend_length+=sum(item['length_mm'] for item in junctions.values())
        raw_area=float(area)/2
        equivalent_area=float(volume)/adopted
        result.update(material_area_mm2=_scalar(raw_area),
            cut_length_mm=_scalar(math.fsum(lengths)/2),
            pierce_count=channels+1 if channels is not None else None,
            bend_count=bend_count,bend_length_mm=_scalar(bend_length) if bend_count is not None else None,
            thickness_mm=_scalar(adopted),source_face_ids=[f'F{i:04d}' for i in range(1,faces.Extent()+1)],
            assumptions=assumptions+[
                '料耗净面积代理取原生表面积的一半；包含边缘/曲面误差，不是生产展开面积。',
                '体积/采用厚度仅单列等效面积作差异比较，不是展开结果，也不是对体积法的独立验证。',
                '采购毛坯面积、排版利用率和切缝由报价采用条件单独处理，不在此伪造展开宽高。',
                '切割行程按原生非退化边长总和的一半估算，含曲面分界冗余；不是实际展开轮廓或机床刀路。',
                '穿孔暂按闭合单壳的拓扑贯通通道数加一次外轮廓；成形通道与实际激光穿孔仍待工程核对。',
                '同轴同半径、材料连续的局部圆柱曲带合并按一道候选折弯估价；不确认生产次数、方向或顺序。'],
            warnings=['所有折弯件工程量均为报价代理估算，不生成制造展开图，不代表工序范围已经穷尽。'],
            measurements={'native_surface_area_mm2':_scalar(area),'native_volume_mm3':_scalar(volume),
                'native_nondegenerate_edge_length_mm':_scalar(math.fsum(lengths)),
                'native_skin_area_proxy_mm2':_scalar(raw_area),
                'volume_equivalent_area_mm2':_scalar(equivalent_area),
                'area_volume_thickness_ratio':_scalar(ratio),
                'geometry_classification':'non_uniform_or_stock_like' if nonuniform else 'thin_wall_proxy',
                'bend_route_status':'route_unresolved' if cycle_rank or bend_count is None else 'assumed_from_native_junctions',
                'euler_characteristic':characteristic,'shell_count':shell_count,
                'topological_through_channels':channels,'bend_surface_pair_count':len(pairs),
                'cylindrical_bend_group_count':len(groups),'sharp_fold_junction_count':len(junctions),
                'cylindrical_bend_groups':cylindrical_groups,
                'junction_graph_cycle_rank':cycle_rank,'known_bend_candidate_count':known_candidates,
                'bend_candidate_face_ids':sorted({fid for pair in pairs for fid in pair['face_ids']}|{fid for item in junctions.values() for fid in item['face_ids']}),
                'sharp_fold_junctions':[{**item,'length_mm':_scalar(item['length_mm']),'angle_deg':_scalar(item['angle_deg'])} for item in junctions.values()]},
            thickness_basis=thickness_basis)
        if junctions:
            result['assumptions'].append('已配对薄壁大平面的真实长直交接边，按一次折弯路线暂估；锐角也可能属于焊接/组合结构，工程师须确认，不补半径或K值。')
        if nonuniform:
            result['warnings'].append('表面积代理×采用厚度与真实体积偏差明显；材料成本应优先用真实体积估重，不能把本面积当精确展开料耗。')
            result['warnings'].append('当前实体可能混厚或含框体/型材结构；局部厚度不代表整件统一板厚，制造路线待工程确认。')
        result['quantity_statuses']={name:'estimated' if result[name] is not None else 'unresolved' for name in QUANTITIES}
        if cycle_rank:
            result['warnings'].append('薄壁交接构成闭环，含折弯/焊接/拼装路线歧义；保留交接候选，但完整折弯次数未知，不把所有交接计成折弯。')
        elif bend_count is None:
            result['warnings'].append('没有受支持的薄壁折弯候选，折弯次数保持未知，不填零。')
        if channels is None:result['warnings'].append('拓扑通道核验不完整，穿孔次数保持未知。')
        result['status']='estimated' if all(result[name] is not None for name in REQUIRED) else 'unresolved'
        return result
    except (ValueError,RuntimeError,TypeError,KeyError,OverflowError,AttributeError) as error:
        result['warnings']=[' '.join(str(error).split())[:500] or '原生报价量测未完成。']
        return result


def validate_quote_quantities(value, solid_id, source_sha256, face_count, *, area_mm2=None, volume_mm3=None):
    """Fail closed when a compact quote receipt is stale or mislabels certainty."""
    def need(ok,message):
        if not ok: raise ValueError(message)
    need(isinstance(value,dict) and value.get('schema_version')==1 and value.get('version')==VERSION,'报价工程量版本无效。')
    need(value.get('solid_id')==solid_id and value.get('source_sha256')==source_sha256 and re.fullmatch(r'[0-9a-f]{64}',str(source_sha256)) is not None,'报价工程量来源不一致。')
    need(value.get('unit')=='mm' and value.get('manufacturing_release') is False,'报价工程量单位或用途无效。')
    need(type(face_count) is int and face_count>0,'报价原生面计数无效。')
    need(value.get('status') in {'measured','estimated','unresolved'} and value.get('method') in {'native_flat_profile','native_brep_inventory'},'报价量测身份无效。')
    states=value.get('quantity_statuses');need(isinstance(states,dict) and set(states)==set(QUANTITIES),'报价工程量状态缺失。')
    for name in QUANTITIES:
        quantity=value.get(name);state=states[name]
        need(state in {'measured','estimated','unresolved'},'工程量采用状态无效。')
        if quantity is None:need(state=='unresolved','未知工程量不能冒充已量测。');continue
        need(state!='unresolved','已给工程量缺少采用身份。')
        if name in {'pierce_count','bend_count'}:
            need(type(quantity) is int and 0<=quantity<=1024 and (name!='pierce_count' or quantity>0),'工序计数无效。')
        else:need(_number(quantity,positive=name!='bend_length_mm') is not None,'报价工程量数值无效。')
    if value['status']!='unresolved':need(all(value.get(name) is not None for name in REQUIRED),'完整估价工程量缺项。')
    if value['method']=='native_brep_inventory':
        need(value.get('blank_width_mm') is None and value.get('blank_height_mm') is None,'几何估量不能伪造实际展开宽高。')
        need(all(state!='measured' for state in states.values()),'几何代理不能标记为精确量测工艺。')
        need(value['status']!='measured','几何代理不能标记为完整精确量测。')
    else:
        need(value['status']=='measured' and all(state=='measured' for state in states.values()),'原生平板量测状态不一致。')
        need(value['bend_count']==0 and _number(value['bend_length_mm'])==0,'平板不能虚构折弯。')
    ids=value.get('source_face_ids');need(isinstance(ids,list) and len(ids)<=MAX_FACES and len(ids)==len(set(ids)),'报价面来源无效。')
    need(all(isinstance(fid,str) and re.fullmatch(r'F[0-9]{4,8}',fid) and 0<int(fid[1:])<=face_count for fid in ids),'报价面来源越界。')
    for name in ('assumptions','warnings'):
        entries=value.get(name);need(isinstance(entries,list) and len(entries)<=32 and all(isinstance(s,str) and len(s)<=1000 for s in entries),'报价量测说明无效。')
    for name in ('measurements','thickness_basis','unfolding'):need(isinstance(value.get(name),dict),'报价量测原始依据缺失。')
    need(value['unfolding'].get('production_pattern') is False,'报价估量不能批准生产展开。')
    need(value['unfolding'].get('status')==('not_required_flat' if value['method']=='native_flat_profile' else 'not_performed'),'报价展开状态无效。')
    measurements=value['measurements']
    for name,reference in (('native_surface_area_mm2',area_mm2),('native_volume_mm3',volume_mm3)):
        if name in measurements:
            actual=_number(measurements[name],positive=True);expected=_number(reference,positive=True)
            need(actual is not None and (expected is None or abs(actual-expected)<=max(Decimal('.00001'),expected*Decimal('.000001'))),'报价原生面积/体积不一致。')
    def same(actual,expected):
        a,b=_number(actual),_number(expected)
        return a is not None and b is not None and abs(a-b)<=max(Decimal('.00002'),abs(b)*Decimal('.000001'))
    if value['method']=='native_flat_profile':
        need(same(value['material_gross_area_mm2'],_number(value['blank_width_mm'])*_number(value['blank_height_mm'])),'平板毛坯面积不一致。')
        reference_volume=_number(volume_mm3,positive=True);reference_area=_number(area_mm2,positive=True)
        net=_number(value['material_area_mm2']);thick=_number(value['thickness_mm']);cut=_number(value['cut_length_mm'])
        if reference_volume is not None:need(same(reference_volume,net*thick),'平板面积×厚度与原生体积不一致。')
        if reference_area is not None:need(same(reference_area,2*net+cut*thick),'平板轮廓与原生表面积不一致。')
    elif value['material_area_mm2'] is not None:
        native_area=_number(measurements.get('native_surface_area_mm2'),positive=True)
        native_volume=_number(measurements.get('native_volume_mm3'),positive=True)
        edge_length=_number(measurements.get('native_nondegenerate_edge_length_mm'),positive=True)
        adopted=_number(value['thickness_mm'],positive=True)
        need(all(item is not None for item in (native_area,native_volume,edge_length,adopted)),'几何估量缺少原生依据。')
        need(same(value['material_area_mm2'],native_area/2) and same(value['cut_length_mm'],edge_length/2),'面积或切割代理与原生测量不一致。')
        need(value['material_gross_area_mm2'] is None,'几何估量不能虚构采购毛坯面积。')
        need(same(measurements.get('native_skin_area_proxy_mm2'),native_area/2) and same(measurements.get('volume_equivalent_area_mm2'),native_volume/adopted),'面积对比依据不一致。')
        need(same(measurements.get('area_volume_thickness_ratio'),native_area*adopted/(2*native_volume)),'厚度面积/体积差异比不一致。')
        ratio=native_area*adopted/(2*native_volume)
        nonuniform=ratio<Decimal('.8') or ratio>Decimal('1.35')
        need(measurements.get('geometry_classification')==('non_uniform_or_stock_like' if nonuniform else 'thin_wall_proxy'),'几何估价分类与原始差异不一致。')
        channels=measurements.get('topological_through_channels')
        characteristic=measurements.get('euler_characteristic');shells=measurements.get('shell_count')
        if value['pierce_count'] is not None:
            need(type(channels) is int and channels>=0 and type(characteristic) is int and shells==1 and characteristic==2-2*channels and value['pierce_count']==channels+1,'穿孔拓扑代理依据不一致。')
        for name in ('cylindrical_bend_group_count','sharp_fold_junction_count','junction_graph_cycle_rank','known_bend_candidate_count'):
            count=measurements.get(name);need(type(count) is int and 0<=count<=1024,'折弯候选计数依据无效。')
        expected=measurements['cylindrical_bend_group_count']+measurements['sharp_fold_junction_count']
        need(measurements['known_bend_candidate_count']==expected,'折弯候选合计不一致。')
        junctions=measurements.get('sharp_fold_junctions')
        need(isinstance(junctions,list) and len(junctions)==measurements['sharp_fold_junction_count'],'锐角交接依据缺失。')
        for item in junctions:
            need(isinstance(item,dict) and item.get('kind')=='sharp_fold_junction' and isinstance(item.get('face_ids'),list) and len(item['face_ids'])==4 and all(fid in ids for fid in item['face_ids']),'锐角交接面来源无效。')
            need(_number(item.get('length_mm'),positive=True) is not None and _number(item.get('angle_deg'),positive=True) is not None,'锐角交接量测无效。')
        curved=measurements.get('cylindrical_bend_groups')
        need(isinstance(curved,list) and len(curved)==measurements['cylindrical_bend_group_count'],'圆柱曲带组依据缺失。')
        for item in curved:
            need(isinstance(item,dict) and item.get('kind')=='paired_cylindrical_bands' and isinstance(item.get('face_ids'),list) and len(item['face_ids'])>=2 and len(item['face_ids'])<=MAX_FACES and all(fid in ids for fid in item['face_ids']),'圆柱曲带面来源无效。')
            need(_number(item.get('length_mm'),positive=True) is not None and _number(item.get('inner_radius_mm'),positive=True) is not None and _number(item.get('outer_radius_mm'),positive=True) is not None,'圆柱曲带量测无效。')
        if value['bend_count'] is not None:
            need(not nonuniform and measurements['junction_graph_cycle_rank']==0 and value['bend_count']==expected and expected>0,'折弯估量不能隐去路线歧义或虚构次数。')
            need(same(value['bend_length_mm'],sum((_number(item['length_mm']) for item in junctions+curved),Decimal(0))),'折弯长度与保留的候选不一致。')
        if measurements['junction_graph_cycle_rank']:
            need(value['bend_count'] is None and value['bend_length_mm'] is None,'闭环交接不能冒充完整折弯路线。')
        basis=value['thickness_basis']
        need(basis.get('adopted_for_estimation') is True and basis.get('source') in {'native_uniform_candidate','dominant_local_measurement_assumption'},'板厚估价采用身份缺失。')
        candidates=basis.get('candidates_mm')
        need(isinstance(candidates,list) and len(candidates)<=64 and all(_number(item,positive=True) is not None for item in candidates),'局部厚度依据无效。')
        need(not candidates or any(same(adopted,item) for item in candidates),'采用厚度没有局部面量测依据。')
    return value
