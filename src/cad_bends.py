"""Verified native constant-radius sheet-bend candidates; no unfolding."""
from __future__ import annotations
import math
import time
from cad_thickness import _dot, _sub, _add, _unit, _norm, _xyz, _cross

TOL = 1e-5
ANGLE_TOL = 1e-7
MAX_FACES = 512
MAX_BENDS = 128
MAX_VERTICES = 50000
MAX_TRIANGLES = 100000
MAX_SECONDS = 4.0


class _Rejected(ValueError):
    pass


class _Limit(Exception):
    pass


def _need(condition, message):
    if not condition:
        raise _Rejected(message)


def _number(value):
    value = float(value)
    _need(math.isfinite(value) and abs(value) < 1e9, '折弯测量无效。')
    return round(value, 8)


def _vector(value):
    return [_number(v) for v in value]


def _profile(status='unresolved', reason='尚未取得足够的圆柱折弯证据。', bends=None, warnings=None):
    return {'status': status, 'reason': reason, 'unit': 'mm', 'bends': bends or [], 'warnings': warnings or []}


def recognize_bends(shape, faces, thickness, *, deadline=None, laser_cut=None):
    value = thickness.get('value_mm') if isinstance(thickness, dict) else None
    if not isinstance(thickness, dict) or thickness.get('status') != 'candidate' or not isinstance(value, (float, int)) or isinstance(value, bool) or value <= 0:
        return _profile(reason='没有可靠的统一板厚候选，折弯保持待确认。')
    if faces.Extent() > MAX_FACES:
        return _profile(reason='实体面数量超出本轮折弯处理预算。')
    stop = min(deadline or float('inf'), time.monotonic()+MAX_SECONDS)

    def budget():
        if time.monotonic() > stop:
            raise _Limit('折弯处理已达到本轮预算。')

    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Line
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_REVERSED, TopAbs_IN
    from OCP.TopExp import TopExp
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
    from OCP.gp import gp_Pnt

    records = {}
    classifier = BRepClass3d_SolidClassifier(shape)

    def inside(point):
        budget()
        classifier.Perform(gp_Pnt(*point), TOL)
        return classifier.State() == TopAbs_IN

    def radial(point, origin, axis):
        delta = _sub(point, origin)
        return _sub(delta, tuple(x*_dot(delta, axis) for x in axis))

    def on_face(record, point):
        return BRepClass_FaceClassifier(record['face'], gp_Pnt(*point), TOL).State() == TopAbs_IN

    try:
        budget()
        _need(BRepCheck_Analyzer(shape).IsValid(), '原生实体拓扑未通过校验，不能采用折弯候选。')
        for index in range(1, faces.Extent()+1):
            budget()
            face = TopoDS.Face(faces.FindKey(index))
            surface = BRepAdaptor_Surface(face, True)
            edges = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
            TopExp.MapShapes_s(face, TopAbs_EDGE, edges)
            props = GProp_GProps(); BRepGProp.SurfaceProperties_s(face, props, False, False)
            records[f'F{index:04d}'] = {'face': face, 'surface': surface, 'edges': edges,
                                       'area': props.Mass(), 'id': f'F{index:04d}'}

        def tangent_boundaries(record, origin, axis, radius, outward_sign):
            result = []
            for j in range(1, record['edges'].Extent()+1):
                budget()
                edge = TopoDS.Edge(record['edges'].FindKey(j))
                curve = BRepAdaptor_Curve(edge)
                if curve.GetType() != GeomAbs_Line:
                    continue
                a, b = _xyz(curve.Value(curve.FirstParameter())), _xyz(curve.Value(curve.LastParameter()))
                length = _norm(_sub(b, a))
                if length < 5*value or abs(_dot(_unit(_sub(b, a)), axis)) < 1-ANGLE_TOL:
                    continue
                direction = _unit(radial(tuple((x+y)/2 for x, y in zip(a, b)), origin, axis))
                if direction is None:
                    continue
                for plane in records.values():
                    if plane['surface'].GetType() != GeomAbs_Plane or not plane['edges'].Contains(edge):
                        continue
                    normal = _xyz(plane['surface'].Plane().Axis().Direction())
                    sign = -1 if plane['face'].Orientation() == TopAbs_REVERSED else 1
                    normal = tuple(sign*x for x in normal)
                    if abs(_dot(normal, axis)) > ANGLE_TOL or outward_sign*_dot(normal, direction) < 1-ANGLE_TOL or plane['area'] < 5*value*length:
                        continue
                    if _dot(_sub(b, origin), axis) < _dot(_sub(a, origin), axis):
                        a, b = b, a
                    result.append({'face': plane, 'start': a, 'end': b, 'radial': direction, 'normal': normal, 'length': length})
            _need(len(result) == 2 and result[0]['face']['id'] != result[1]['face']['id'],
                  '圆柱带没有两侧直接相切的完整薄板平面。')
            return result

        def display_mesh(pair):
            positions, indices = [], []
            for record in pair:
                budget()
                mesher = BRepMesh_IncrementalMesh(record['face'], .1, False, .15, False)
                _need(mesher.IsDone(), '原生折弯曲面无法生成显示网格。')
                location = TopLoc_Location()
                mesh = BRep_Tool.Triangulation_s(record['face'], location)
                _need(mesh is not None, '原生折弯曲面缺少网格。')
                offset = len(positions)//3
                for j in range(1, mesh.NbNodes()+1):
                    positions.extend(_vector(_xyz(mesh.Node(j).Transformed(location.Transformation()))))
                for j in range(1, mesh.NbTriangles()+1):
                    a, b, c = mesh.Triangle(j).Get()
                    if record['face'].Orientation() == TopAbs_REVERSED:
                        b, c = c, b
                    indices.extend((offset+a-1, offset+b-1, offset+c-1))
            return {'positions': positions, 'indices': indices}

        bends, used = [], set()
        pairs = [item for item in thickness.get('evidence', []) if item.get('kind') == 'coaxial_cylinders']
        for source in pairs:
            budget()
            ids = source.get('face_ids', [])
            if len(ids) != 2 or any(fid not in records for fid in ids):
                continue
            try:
                bend = _recognize_pair(records[ids[0]], records[ids[1]], value, radial, tangent_boundaries,
                                       on_face, inside, display_mesh, budget, GeomAbs_Cylinder)
                _need(not any(fid in used for fid in ids), '同一圆柱面不能重复作为两处折弯。')
                _need(len(bends) < MAX_BENDS, '折弯数量超出本轮预算。')
                bend['id'] = f'B{len(bends)+1:04d}'
                bends.append(bend); used.update(ids)
            except _Rejected:
                continue
        budget()
        _need(sum(len(b['mesh']['positions'])//3 for b in bends) <= MAX_VERTICES and
              sum(len(b['mesh']['indices'])//3 for b in bends) <= MAX_TRIANGLES, '折弯显示几何超出本轮预算。')
        if bends:
            return _profile('candidate', '已验证所列原生圆柱折弯候选；不代表已穷尽全部加工特征。', bends,
                            ['折弯方向待选择基准面；未展开、未采用 K 因子或机床参数。'])
        if not pairs and isinstance(laser_cut, dict) and laser_cut.get('status') == 'ready':
            return _profile('not_applicable', '本轮没有受支持的等厚圆柱折弯证据。')
        return _profile(reason='圆柱面未同时满足相切薄板邻接、真实曲域及材料连续性要求。')
    except _Limit as error:
        return _profile(reason=str(error), warnings=['预算不足时不发布部分折弯候选。'])
    except _Rejected as error:
        return _profile(reason=str(error), warnings=['本轮未把部分处理结果当作完整折弯结论。'])
    except Exception:
        return _profile(reason='本件原生折弯几何核验未完成，保留待人工确认。')


def _recognize_pair(inner, outer, thickness, radial, boundaries, on_face, inside, mesh, budget, cylinder_kind):
    a, b = inner['surface'], outer['surface']
    _need(a.GetType() == cylinder_kind and b.GetType() == cylinder_kind, '配对不是两个真实圆柱面。')
    if a.Cylinder().Radius() > b.Cylinder().Radius():
        inner, outer = outer, inner
        a, b = b, a
    ri, ro = a.Cylinder().Radius(), b.Cylinder().Radius()
    _need(ri > TOL and abs(ro-ri-thickness) <= max(TOL, thickness*.001), '内外半径差不等于已测板厚。')
    axis, origin = _xyz(b.Cylinder().Axis().Direction()), _xyz(b.Cylinder().Location())
    largest = max(range(3), key=lambda i: abs(axis[i]))
    if axis[largest] < 0:
        axis = tuple(-x for x in axis)
    _need(abs(_dot(axis, _xyz(a.Cylinder().Axis().Direction()))) >= 1-ANGLE_TOL and
          _norm(radial(_xyz(a.Cylinder().Location()), origin, axis)) <= TOL, '内外圆柱并不同轴。')
    angles = [s.LastUParameter()-s.FirstUParameter() for s in (a, b)]
    lengths = [s.LastVParameter()-s.FirstVParameter() for s in (a, b)]
    _need(all(math.isfinite(x) for x in angles+lengths), '圆柱裁切范围无效。')
    _need(1e-4 < angles[0] < math.pi-1e-4 and abs(angles[0]-angles[1]) <= 1e-5,
          '不是受支持的单段小于180度圆柱弯曲带。')
    _need(abs(lengths[0]-lengths[1]) <= TOL and lengths[0] >= 5*thickness, '内外曲带轴向覆盖不一致。')
    for rec, radius, angle, length in zip((inner, outer), (ri, ro), angles, lengths):
        _need(abs(rec['area']-radius*angle*length) <= max(1e-5, rec['area']*1e-6), '曲带存在缺口或非矩形裁切，不能采用参数包络。')
    inner_lines = boundaries(inner, origin, axis, ri, -1)
    outer_lines = boundaries(outer, origin, axis, ro, 1)
    flanges = []
    for line in outer_lines:
        matching = [candidate for candidate in inner_lines if _dot(candidate['radial'], line['radial']) >= 1-ANGLE_TOL]
        _need(len(matching) == 1, '内外曲带的切线方位不一致。')
        other = matching[0]
        n1 = _xyz(other['face']['surface'].Plane().Axis().Direction())
        n2 = _xyz(line['face']['surface'].Plane().Axis().Direction())
        _need(abs(_dot(n1, n2)) >= 1-ANGLE_TOL, '两侧薄板面不平行。')
        p1 = _xyz(other['face']['surface'].Plane().Location())
        p2 = _xyz(line['face']['surface'].Plane().Location())
        _need(abs(abs(_dot(_sub(p1, p2), n1))-thickness) <= max(TOL, thickness*.001), '相邻薄板面间距与板厚不一致。')
        _need(all(abs(_dot(_sub(line[end], other[end]), axis)) <= TOL for end in ('start', 'end')), '内外相切边轴向范围不一致。')
        flanges.append([other['face']['id'], line['face']['id']])
    angle = math.acos(max(-1., min(1., _dot(outer_lines[0]['radial'], outer_lines[1]['radial']))))
    _need(abs(angle-angles[1]) <= 1e-5, '真实切线转角与圆柱扫角不一致。')
    panel_turn = math.acos(max(-1., min(1., _dot(outer_lines[0]['normal'], outer_lines[1]['normal']))))
    _need(abs(panel_turn-angle) <= 1e-5, '同一物理板侧的面法向转角与曲带扫角不一致。')
    length = outer_lines[0]['length']
    _need(abs(length-outer_lines[1]['length']) <= TOL and abs(length-lengths[1]) <= TOL, '两条外侧切线长度与曲带长度不一致。')
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TopAbs import TopAbs_REVERSED
    for rec, sign in ((inner, -1), (outer, 1)):
        surface = rec['surface']
        point, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
        surface.D1((surface.FirstUParameter()+surface.LastUParameter())/2,
                   (surface.FirstVParameter()+surface.LastVParameter())/2, point, du, dv)
        orientation = -1 if rec['face'].Orientation() == TopAbs_REVERSED else 1
        outward = _unit(tuple(orientation*x for x in _cross(_xyz(du), _xyz(dv))))
        direction = _unit(radial(_xyz(point), origin, axis))
        _need(outward is not None and sign*_dot(outward, direction) >= 1-ANGLE_TOL,
              '内外曲带的物理外法向与材料侧不一致。')
    for uf in (.2, .5, .8):
        for vf in (.2, .5, .8):
            budget()
            point = _xyz(b.Value(b.FirstUParameter()+angles[1]*uf, b.FirstVParameter()+lengths[1]*vf))
            direction = _unit(radial(point, origin, axis))
            center = _sub(point, tuple(ro*x for x in direction))
            _need(on_face(outer, point) and on_face(inner, _add(center, direction, ri)), '圆柱裁切角域未覆盖对应的内外曲带。')
            for fraction in (.2, .5, .8):
                _need(inside(_add(center, direction, ri+(ro-ri)*fraction)), '折弯带内存在空腔或材料不连续。')
    axis_start = _add(origin, axis, _dot(_sub(outer_lines[0]['start'], origin), axis))
    return {'id': '', 'status': 'pending_review', 'inner_face_ids': [inner['id']], 'outer_face_ids': [outer['id']],
            'flange_face_ids': flanges, 'axis': {'point': _vector(axis_start), 'direction': _vector(axis)},
            'inner_radius_mm': _number(ri), 'outer_radius_mm': _number(ro), 'thickness_mm': _number(thickness),
            'rotation_angle_deg': _number(math.degrees(angle)), 'included_angle_deg': _number(180-math.degrees(angle)),
            'length_mm': _number(length), 'tangent_lines': [{'start': _vector(x['start']), 'end': _vector(x['end'])} for x in outer_lines],
            'mesh': mesh((inner, outer)), 'direction': 'unconfirmed', 'reference_face_id': None,
            'warnings': ['仅为原生折弯标注候选；UP/DOWN须由基准板面及观察方向确定。']}
