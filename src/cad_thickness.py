"""Bounded wall-thickness candidates from original OCCT boundary surfaces.

Paired trimmed surfaces must overlap, face into material and enclose material
at sampled interior points. Bounding boxes and 2V/A never generate thickness:
volume/area is only a consistency veto after a native face distance is found.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

MAX_FACES = 512
MAX_PAIRS = 40000
MAX_CLASSIFICATIONS = 14000
# Complex perforated/formed parts need more native point classifications than
# a simple plate. The extractor's shared 35s deadline still bounds a full file.
MAX_SECONDS = 8.0
MAX_EVIDENCE = 64
ANGLE_TOLERANCE = 1e-10
GEOMETRIC_TOLERANCE = 1e-5


class _Limit(Exception):
    def __init__(self, reason='unknown'):
        self.reason = reason
        super().__init__(reason)


@dataclass
class _Budget:
    deadline: float
    pairs: int = 0
    checks: int = 0
    stage: str = 'face_records'

    def checkpoint(self):
        if self.pairs > MAX_PAIRS:
            raise _Limit('pair_count')
        if self.checks > MAX_CLASSIFICATIONS:
            raise _Limit('classification_count')
        if time.monotonic() > self.deadline:
            raise _Limit('deadline')

    def spend(self, kind):
        if kind == 'pair':
            self.pairs += 1
        else:
            self.checks += 1
        self.checkpoint()


def _result(status='unresolved', reason='没有足够的成对薄壁面证据。', *, value=None,
            method='none', confidence='none', evidence=None, warnings=None):
    return {'status': status, 'value_mm': value, 'method': method,
            'confidence': confidence, 'reason': reason,
            'evidence': evidence or [], 'warnings': warnings or []}


def _xyz(value):
    return (value.X(), value.Y(), value.Z())


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _sub(a, b):
    return tuple(x-y for x, y in zip(a, b))


def _add(a, b, scale=1.0):
    return tuple(x+scale*y for x, y in zip(a, b))


def _norm(a):
    return math.sqrt(_dot(a, a))


def _unit(a):
    length = _norm(a)
    return tuple(x/length for x in a) if length > 1e-15 else None


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _matrix_product(matrix, a, b):
    return sum(a[i]*matrix.Value(i+1, j+1)*b[j] for i in range(3) for j in range(3))


def _face_width(props, u, v):
    """Narrow in-plane spread from exact surface moments; rotation invariant."""
    area, moment = props.Mass(), props.MatrixOfInertia()
    xx, yy = _matrix_product(moment, v, v)/area, _matrix_product(moment, u, u)/area
    xy = -_matrix_product(moment, u, v)/area
    smallest = (xx+yy-math.sqrt(max(0., (xx-yy)**2+4*xy*xy)))/2
    return math.sqrt(max(0., 12*smallest))


def _serialize(evidence):
    ordered = sorted(evidence, key=lambda c: c['support_area_mm2'], reverse=True)
    # Preserve at least one original pair for each measured local distance and
    # surface kind, even when many large 1.2mm faces precede a small 1.0mm wall.
    # Otherwise the compact evidence could erase the reason a part conflicted.
    representatives, base, cluster = {}, None, 0
    for item in sorted(ordered, key=lambda row: row['thickness_mm']):
        value = item['thickness_mm']
        if base is None or abs(value-base) > max(.001, base*.001):
            base, cluster = value, cluster+1
        key = cluster, item['kind']
        if key not in representatives or item['support_area_mm2'] > representatives[key]['support_area_mm2']:
            representatives[key] = item
    selected = list(representatives.values())[:MAX_EVIDENCE]
    identities = {id(item) for item in selected}
    selected.extend(item for item in ordered if id(item) not in identities)
    selected = sorted(selected[:MAX_EVIDENCE], key=lambda c: c['support_area_mm2'], reverse=True)
    return [{'kind': e['kind'], 'face_ids': e['face_ids'],
             'thickness_mm': round(e['thickness_mm'], 6),
             'support_area_mm2': round(e['support_area_mm2'], 6) or e['support_area_mm2']}
            for e in selected]


def _method(evidence):
    kinds = {c['kind'] for c in evidence}
    return 'combined' if len(kinds) > 1 else next(iter(kinds)) if kinds else 'none'


def infer_thickness(shape, faces=None, *, area_mm2=None, volume_mm3=None, deadline=None):
    """Find an unconfirmed thickness or explicit unresolved outcome.

    ``faces`` is the extractor's TopExp map: Fxxxx refers to that exact native
    face order. An optional shared deadline bounds all solids of an extraction.
    """
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve2d
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepClass import BRepClass_FaceClassifier
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE, TopAbs_SHELL, TopAbs_REVERSED, TopAbs_IN
    from OCP.TopExp import TopExp
    from OCP.TopoDS import TopoDS
    from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher
    from OCP.gp import gp_Pnt, gp_Pnt2d, gp_Vec, gp_Vec2d

    budget = _Budget(min(deadline or float('inf'), time.monotonic()+MAX_SECONDS))
    if time.monotonic() > budget.deadline:
        return _result(reason='本轮板厚分析总预算已用完，保留待人工测量。')
    if faces is None:
        faces = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
        TopExp.MapShapes_s(shape, TopAbs_FACE, faces)
    if faces.Extent() > MAX_FACES:
        return _result(reason='实体面数量超过本轮板厚分析预算。', warnings=['复杂实体保留待人工测量。'])
    if not BRepCheck_Analyzer(shape).IsValid():
        return _result(reason='实体拓扑未通过校验，不能采用板厚候选。')
    shells = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
    TopExp.MapShapes_s(shape, TopAbs_SHELL, shells)
    if not shells.Extent() or not all(BRep_Tool.IsClosed_s(shells.FindKey(i)) for i in range(1, shells.Extent()+1)):
        return _result(reason='实体未闭合，无法核实两面之间是否为材料。')
    if area_mm2 is None:
        total = GProp_GProps()
        BRepGProp.SurfaceProperties_s(shape, total, False, False)
        area_mm2 = total.Mass()
    if volume_mm3 is None:
        total = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, total, True, False, False)
        volume_mm3 = abs(total.Mass())
    if not area_mm2 or not volume_mm3:
        return _result(reason='实体面积或体积无效，无法核实薄壁一致性。')
    solid_classifier = BRepClass3d_SolidClassifier(shape)
    records, candidates = [], []
    material_points = {}

    def point_key(point):
        # Repeated trimmed cylinder patches often ask about identical points.
        # A nanometre key is far below the native classification tolerance.
        return tuple(round(value, 9) for value in point)

    def on_face(record, point):
        budget.checkpoint()
        relative = _sub(point, record['point'])
        if record['kind'] == GeomAbs_Plane:
            distance = abs(_dot(relative, record['normal']))
        else:
            radial = _sub(relative, tuple(x*_dot(relative, record['axis']) for x in record['axis']))
            distance = abs(_norm(radial)-record['radius'])
        # FaceClassifier projects into UV; also check actual 3D surface distance.
        if distance > GEOMETRIC_TOLERANCE:
            return False
        cache = record.setdefault('point_classifications', {})
        key = point_key(point)
        if key not in cache:
            budget.spend('check')
            cache[key] = BRepClass_FaceClassifier(record['face'], gp_Pnt(*point), GEOMETRIC_TOLERANCE).State() == TopAbs_IN
        return cache[key]

    def boundary_samples(record, thickness, result):
        """Find interior seeds beside original trimming curves, not their mesh.

        A wide UV window can be almost entirely empty on a narrow frame. Native
        p-curve tangents provide seeds on either side of each edge. Every seed
        must still be inside the original trimmed face before it is used.
        """
        edges = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
        TopExp.MapShapes_s(record['face'], TopAbs_EDGE, edges)
        curves = []
        su = record['radius'] if record['kind'] == GeomAbs_Cylinder else 1.
        for index in range(1, edges.Extent()+1):
            budget.checkpoint()
            curve = BRepAdaptor_Curve2d(TopoDS.Edge(edges.FindKey(index)), record['face'])
            first, last = curve.FirstParameter(), curve.LastParameter()
            if not all(math.isfinite(value) for value in (first, last)) or last <= first:
                continue
            p, q = curve.Value(first+(last-first)*.25), curve.Value(first+(last-first)*.75)
            span = math.hypot((q.X()-p.X())*su, q.Y()-p.Y())
            curves.append((span, curve, first, last))
        seen = {point_key(point) for point in result}
        selected = sorted(curves, key=lambda item: item[0], reverse=True)[:32]
        # Round-robin across trimming edges rather than collecting dozens of
        # near-duplicate points from the first long side of a window.
        for fraction in (.5, .25, .75):
            for _, curve, first, last in selected:
                point, tangent = gp_Pnt2d(), gp_Vec2d()
                curve.D1(first+(last-first)*fraction, point, tangent)
                length = math.hypot(tangent.X()*su, tangent.Y())
                if length <= 1e-15:
                    continue
                du, dv = -tangent.Y()/length/su, tangent.X()*su/length
                accepted = False
                for distance in (max(GEOMETRIC_TOLERANCE*20, thickness*.05),
                                 max(GEOMETRIC_TOLERANCE*20, thickness*.2)):
                    for sign in (-1, 1):
                        u, v = point.X()+sign*distance*du, point.Y()+sign*distance*dv
                        budget.spend('check')
                        if BRepClass_FaceClassifier(record['face'], gp_Pnt2d(u, v), GEOMETRIC_TOLERANCE).State() != TopAbs_IN:
                            continue
                        candidate = _xyz(record['surface'].Value(u, v))
                        key = point_key(candidate)
                        if key not in seen:
                            result.append(candidate)
                            seen.add(key)
                            accepted = True
                        if len(result) >= 16:
                            return
                        if accepted:
                            break
                    if accepted:
                        break

    def samples(record, thickness):
        if 'samples' in record:
            return record['samples']
        u0, u1, v0, v1 = record['uv']
        result = []
        # A regular perforation can cover every coarse grid point while most
        # of the trimmed face is sound material. Refine only when the current
        # grid has fewer than three interior points. Every UV classification
        # still consumes the same bounded budget; no mesh or guessed point
        # outside the original trim is used.
        for cells in (4, 8, 16):
            denominator = cells * 2
            for ui in range(1, denominator, 2):
                for vi in range(1, denominator, 2):
                    u, v = u0+(u1-u0)*ui/denominator, v0+(v1-v0)*vi/denominator
                    budget.spend('check')
                    if BRepClass_FaceClassifier(record['face'], gp_Pnt2d(u, v), GEOMETRIC_TOLERANCE).State() == TopAbs_IN:
                        result.append(_xyz(record['surface'].Value(u, v)))
            if len(result) >= 3:
                break
            if cells == 4:
                boundary_samples(record, thickness, result)
                if len(result) >= 3:
                    break
        record['samples'] = result
        return result

    def material_between(p, q):
        delta = _sub(q, p)
        for fraction in (.2, .5, .8):
            point = _add(p, delta, fraction)
            key = point_key(point)
            if key not in material_points:
                budget.spend('check')
                solid_classifier.Perform(gp_Pnt(*point), GEOMETRIC_TOLERANCE)
                material_points[key] = solid_classifier.State() == TopAbs_IN
            if not material_points[key]:
                return False
        return True

    def overlap(a, b, project, thickness):
        points = samples(a, thickness)
        if len(points) < 3:
            return None
        projected = [(p, project(p)) for p in points]
        matched = [(p, q) for p, q in projected if on_face(b, q)]
        if len(matched)/len(points) < .7:
            return None
        # Reject opposed surfaces spanning an empty cavity or separate walls.
        if not all(material_between(p, q) for p, q in matched):
            return None
        return min(a['area'], b['area'])*len(matched)/len(points)

    try:
        for index in range(1, faces.Extent()+1):
            budget.spend('pair')
            face = TopoDS.Face(faces.FindKey(index))
            surface = BRepAdaptor_Surface(face, True)
            kind = surface.GetType()
            if kind not in (GeomAbs_Plane, GeomAbs_Cylinder):
                continue
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(face, props, False, False)
            if props.Mass() <= 1e-8:
                continue
            record = {'id': f'F{index:04d}', 'face': face, 'surface': surface,
                      'area': props.Mass(), 'kind': kind, 'uv': BRepTools.UVBounds_s(face),
                      'sign': -1 if face.Orientation() == TopAbs_REVERSED else 1}
            if not all(math.isfinite(v) for v in record['uv']):
                continue
            if kind == GeomAbs_Plane:
                plane = surface.Plane()
                record.update(point=_xyz(plane.Location()), normal=_xyz(plane.Axis().Direction()))
                record['outward'] = tuple(record['sign']*v for v in record['normal'])
                record['width'] = _face_width(props, _xyz(plane.Position().XDirection()),
                                              _xyz(plane.Position().YDirection()))
            else:
                cylinder = surface.Cylinder()
                record.update(point=_xyz(cylinder.Location()), axis=_xyz(cylinder.Axis().Direction()),
                              radius=cylinder.Radius())
                u, v = (record['uv'][0]+record['uv'][1])/2, (record['uv'][2]+record['uv'][3])/2
                point, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
                surface.D1(u, v, point, du, dv)
                outward = _unit(tuple(record['sign']*x for x in _cross(_xyz(du), _xyz(dv))))
                relative = _sub(_xyz(point), record['point'])
                radial = _unit(_sub(relative, tuple(x*_dot(relative, record['axis']) for x in record['axis'])))
                record['radial_sign'] = _dot(outward, radial)
            records.append(record)

        pairs = []
        budget.stage = 'pair_prefilter'
        for i, a in enumerate(records):
            for b in records[i+1:]:
                # Most pairs in a perforated part are unrelated hole walls.
                # Keep the wall-clock bound for these cheap broad-phase
                # comparisons, reserving the pair budget for geometrically
                # eligible surfaces that need native overlap validation.
                budget.checkpoint()
                if a['kind'] != b['kind']:
                    continue
                if a['kind'] == GeomAbs_Plane:
                    if _dot(a['outward'], b['outward']) > -1+ANGLE_TOLERANCE:
                        continue
                    thickness = -_dot(_sub(b['point'], a['point']), a['outward'])
                    if thickness <= GEOMETRIC_TOLERANCE or min(a['width'], b['width']) < thickness*5:
                        continue
                    if math.sqrt(min(a['area'], b['area'])) < thickness*5:
                        continue
                else:
                    if a['radial_sign']*b['radial_sign'] > -1+ANGLE_TOLERANCE:
                        continue
                    if abs(_dot(a['axis'], b['axis'])) < 1-ANGLE_TOLERANCE:
                        continue
                    if _norm(_cross(_sub(b['point'], a['point']), a['axis'])) > GEOMETRIC_TOLERANCE:
                        continue
                    thickness = abs(a['radius']-b['radius'])
                    if thickness <= GEOMETRIC_TOLERANCE or min(a['radius'], b['radius']) < thickness*.25:
                        continue
                    if min(r['uv'][3]-r['uv'][2] for r in (a, b)) < thickness*5:
                        continue
                    shift, direction = _dot(_sub(b['point'], a['point']), a['axis']), _dot(a['axis'], b['axis'])
                    blo, bhi = sorted(shift+direction*v for v in b['uv'][2:])
                    if min(a['uv'][3], bhi)-max(a['uv'][2], blo) <= GEOMETRIC_TOLERANCE:
                        continue
                budget.spend('pair')
                pairs.append((thickness, a, b))

        budget.stage = 'native_overlap'
        for thickness, a, b in sorted(pairs, key=lambda p: (-min(p[1]['area'], p[2]['area']), p[0])):
            budget.spend('pair')
            if a['area'] > b['area']:
                a, b = b, a
            if a['kind'] == GeomAbs_Plane:
                delta = _dot(_sub(b['point'], a['point']), a['normal'])
                project = lambda p, a=a, delta=delta: _add(p, a['normal'], delta)
                kind = 'parallel_faces'
            else:
                def project(p, a=a, b=b):
                    relative = _sub(p, a['point'])
                    center = _add(a['point'], a['axis'], _dot(relative, a['axis']))
                    return _add(center, _unit(_sub(p, center)), b['radius'])
                u = (a['uv'][0]+a['uv'][1])/2
                v = (a['uv'][2]+a['uv'][3])/2
                point, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
                a['surface'].D1(u, v, point, du, dv)
                outward = _unit(tuple(a['sign']*x for x in _cross(_xyz(du), _xyz(dv))))
                relative = _sub(_xyz(point), a['point'])
                radial = _unit(_sub(relative, tuple(x*_dot(relative, a['axis']) for x in a['axis'])))
                if (b['radius']-a['radius'])*_dot(outward, radial) >= 0:
                    continue
                kind = 'coaxial_cylinders'
            support = overlap(a, b, project, thickness)
            if support is not None:
                candidates.append({'kind': kind, 'face_ids': sorted((a['id'], b['id'])),
                                   'thickness_mm': thickness, 'support_area_mm2': support})
    except _Limit as error:
        trigger = {'deadline': '计时期限', 'pair_count': '面配对次数',
                   'classification_count': '原生点分类次数'}.get(error.reason, '预算')
        return _result(reason='板厚几何分析已达到本轮预算，保留待人工测量。',
                       warnings=['未把部分面分析结果作为完整板厚结论。',
                                 f'预算触发：{trigger}；阶段 {budget.stage}；已配对 {budget.pairs}，已分类 {budget.checks}。'])
    except (RuntimeError, ValueError, ZeroDivisionError):
        return _result(reason='部分边界面无法稳定测量，板厚保持待确认。')

    if not candidates:
        # Only explicit simple bulk geometry earns "not_applicable". A lack of
        # supported/sampled pairs alone says nothing about an arbitrary shape.
        if len(records) == faces.Extent() == 6 and all(r['kind'] == GeomAbs_Plane for r in records):
            remaining, axes, distances = list(records), [], []
            while remaining:
                a = remaining.pop(0)
                opposite = next((b for b in remaining if _dot(a['outward'], b['outward']) < -1+ANGLE_TOLERANCE), None)
                if opposite is None:
                    break
                remaining.remove(opposite)
                axes.append(a['normal'])
                distances.append(abs(_dot(_sub(opposite['point'], a['point']), a['normal'])))
            if (len(distances) == 3 and all(abs(_dot(a, b)) < ANGLE_TOLERANCE for i, a in enumerate(axes) for b in axes[i+1:])
                    and abs(math.prod(distances)-volume_mm3) <= volume_mm3*1e-6):
                return _result('not_applicable', reason='原生几何为正交六面块体，未满足薄壁比例；不能把最小外尺寸当板厚。')
        planes = [r for r in records if r['kind'] == GeomAbs_Plane]
        cylinders = [r for r in records if r['kind'] == GeomAbs_Cylinder]
        if len(records) == faces.Extent() == 3 and len(planes) == 2 and len(cylinders) == 1:
            cylinder = cylinders[0]
            height = abs(_dot(_sub(planes[0]['point'], planes[1]['point']), cylinder['axis']))
            if (all(abs(_dot(p['normal'], cylinder['axis'])) > 1-ANGLE_TOLERANCE for p in planes)
                    and abs(math.pi*cylinder['radius']**2*height-volume_mm3) <= volume_mm3*1e-6):
                return _result('not_applicable', reason='原生几何为实心圆柱，未发现成对薄壁面，圆柱半径不是板厚。')
        return _result(reason='未取得足够的成对薄壁面证据；复杂曲面或局部特征仍需人工测量。')

    # Nearest opposing surfaces suppress remote portions of the same wall.
    nearest = {}
    for candidate in candidates:
        for face_id in candidate['face_ids']:
            nearest[face_id] = min(nearest.get(face_id, float('inf')), candidate['thickness_mm'])
    tolerance = lambda value: max(.001, value*.001)
    candidates = [c for c in candidates if all(abs(c['thickness_mm']-nearest[f]) <= tolerance(nearest[f])
                                               for f in c['face_ids'])]
    clusters = []
    for candidate in sorted(candidates, key=lambda c: c['thickness_mm']):
        found = next((c for c in clusters if abs(c['value']-candidate['thickness_mm']) <= tolerance(c['value'])), None)
        if found is None:
            found = {'value': candidate['thickness_mm'], 'items': []}
            clusters.append(found)
        found['items'].append(candidate)
    for cluster in clusters:
        by_face = {}
        for candidate in cluster['items']:
            for face_id in candidate['face_ids']:
                by_face[face_id] = max(by_face.get(face_id, 0), candidate['support_area_mm2'])
        cluster['covered'] = sum(by_face.values())
        cluster['coverage'] = cluster['covered']/area_mm2
    clusters.sort(key=lambda c: c['covered'], reverse=True)
    if not clusters:
        return _result(reason='成对面的局部厚度互相不一致，无法形成统一候选。')
    primary = clusters[0]
    evidence = sorted(primary['items'], key=lambda c: c['support_area_mm2'], reverse=True)
    if primary['value'] > 10000:
        return _result(reason='测得间距超出本轮板厚候选范围，需人工核对单位及结构。')
    # Even a small supported second wall distance is a detected nonuniformity.
    conflict = clusters[1:]
    if conflict:
        conflicting = [item for c in [primary]+conflict for item in c['items']]
        labels = {'parallel_faces': '平面', 'coaxial_cylinders': '圆柱壁', 'combined': '平面/圆柱壁'}
        measured = [f'{cluster["value"]:.2f} mm（{labels[_method(cluster["items"])]}，支撑约 '
                    f'{min(cluster["coverage"], 1)*100:.1f}% 边界）' for cluster in [primary]+conflict]
        return _result(reason='不同区域出现多种有实质面积支持的厚度，可能为变厚、槽或组合结构。',
                       method=_method(conflicting), evidence=_serialize(conflicting),
                       warnings=['不能自动指定统一板厚，需逐区测量复核。',
                                 '已量测局部候选：'+'；'.join(measured[:8])+
                                 ('；另有其他局部候选。' if len(measured)>8 else '。')])
    ratio = primary['value']*area_mm2/(2*volume_mm3)
    if primary['coverage'] < .60 or not .80 <= ratio <= 1.35:
        return _result(reason='局部成对面距离未覆盖足够边界，或与整件体积不一致。',
                       method=_method(evidence), evidence=_serialize(evidence),
                       warnings=['局部槽、腔体或块体不能按该距离视为整件板厚。',
                                 f'已量测局部候选 {primary["value"]:.2f} mm；支撑约 '
                                 f'{min(primary["coverage"], 1)*100:.1f}% 边界，体积一致性比值 {ratio:.3f}；'
                                 '未采用为整件统一板厚。'])
    confidence = 'high' if primary['coverage'] >= .80 and .90 <= ratio <= 1.20 else 'medium'
    warnings = ['几何候选尚未经过工程师确认，不代表材料规格或工艺认可。']
    if _method(evidence) == 'coaxial_cylinders':
        warnings.append('该值为曲面壁厚候选，不能据此认定为板材或已经完成展开。')
    return _result('candidate', value=round(primary['value'], 6), method=_method(evidence), confidence=confidence,
                   reason=f'真实成对薄壁面覆盖约 {min(primary["coverage"], 1)*100:.0f}% 边界，并通过实体内点和体积一致性检查。',
                   evidence=_serialize(evidence), warnings=warnings)
