"""Exact native contours of proven planar constant-section plate solids.

No folded-part projection or curve approximation: a face prism must match the
entire original solid before a cutting DXF is saved.
"""
from __future__ import annotations

import math
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

MAX_FACES = 512
MAX_EDGES = 2000
MAX_WIRES = 256
MAX_SECONDS = 4.0
MAX_DXF_BYTES = 20 * 1024 * 1024
TOL = 1e-6
ANGLE_TOL = 1e-10


class _Unsupported(ValueError):
    pass


def _need(condition, reason):
    if not condition:
        raise _Unsupported(reason)


def _xyz(value):
    return [value.X(), value.Y(), value.Z()]


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _sub(a, b):
    return [x-y for x, y in zip(a, b)]


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _distance(a, b):
    return math.sqrt(sum((x-y)**2 for x, y in zip(a, b)))


def _number(value):
    value = float(value)
    _need(math.isfinite(value) and abs(value) <= 1e9, '平板轮廓坐标超出本轮导出范围。')
    return round(value, 9)


def _unsupported(reason, thickness=None):
    return {'status': 'unsupported', 'reason': reason, 'unit': 'mm',
            'thickness_mm': thickness, 'source_face_id': None, 'plane': None,
            'bounds_mm': None, 'contours': [], 'holes': [], 'dxf_file': None, 'warnings': []}


def _circle_angle(point, segment):
    center = segment['center']
    return math.atan2(point[1]-center[1], point[0]-center[0]) % math.tau


def _sweep(segment):
    a, b = _circle_angle(segment['start'], segment), _circle_angle(segment['end'], segment)
    return (a-b) % math.tau if segment['clockwise'] else (b-a) % math.tau


def _contour_bounds(contours):
    points = []
    for contour in contours:
        for segment in contour['segments']:
            if segment['type'] == 'line':
                points.extend((segment['start'], segment['end']))
            elif segment['type'] == 'circle':
                x, y = segment['center']; r = segment['radius']
                points.extend(([x-r, y-r], [x+r, y+r]))
            else:
                points.extend((segment['start'], segment['end']))
                first, sweep = _circle_angle(segment['start'], segment), _sweep(segment)
                for angle in (0., math.pi/2, math.pi, 3*math.pi/2):
                    offset = (first-angle) % math.tau if segment['clockwise'] else (angle-first) % math.tau
                    if offset <= sweep+1e-10:
                        points.append([segment['center'][0]+segment['radius']*math.cos(angle),
                                       segment['center'][1]+segment['radius']*math.sin(angle)])
    _need(points, '平板没有可导出的完整边界。')
    return ([min(p[i] for p in points) for i in range(2)],
            [max(p[i] for p in points) for i in range(2)])


def _dxf_library():
    if 'ezdxf' in sys.modules:
        return sys.modules['ezdxf']
    # ezdxf 1.4.5 loads its font manager on import. These DXFs have no text;
    # a valid empty cache avoids loading every system font. Cache v2 is the
    # pinned library's own format. User caches/configuration remain untouched.
    previous = os.environ.get('XDG_CACHE_HOME')
    with tempfile.TemporaryDirectory(prefix='hyl-dxf-no-text-') as folder:
        cache = Path(folder)/'ezdxf'
        cache.mkdir()
        (cache/'font_manager_cache.json').write_text(json.dumps({'version': 2, 'font-faces': []}))
        os.environ['XDG_CACHE_HOME'] = folder
        try:
            import ezdxf
        finally:
            if previous is None:
                os.environ.pop('XDG_CACHE_HOME', None)
            else:
                os.environ['XDG_CACHE_HOME'] = previous
    return ezdxf


def _write_dxf(path, contours, bounds, metadata):
    ezdxf = _dxf_library()
    doc = ezdxf.new('R2010')
    doc.units = 4
    doc.header['$INSUNITS'] = 4
    doc.header['$MEASUREMENT'] = 1
    doc.header['$LUNITS'] = 2
    doc.header['$LUPREC'] = 6
    doc.header['$INSBASE'] = (0., 0., 0.)
    doc.header['$EXTMIN'] = (*bounds['min'], 0.)
    doc.header['$EXTMAX'] = (*bounds['max'], 0.)
    for layer, color in [('CUT_OUTER', 7), ('CUT_INNER', 1)]:
        doc.layers.new(layer, dxfattribs={'color': color})
    model = doc.modelspace()
    for contour in contours:
        attrs = {'layer': 'CUT_OUTER' if contour['role'] == 'outer' else 'CUT_INNER'}
        for segment in contour['segments']:
            if segment['type'] == 'line':
                model.add_line(segment['start'], segment['end'], dxfattribs=attrs)
            elif segment['type'] == 'circle':
                model.add_circle(segment['center'], segment['radius'], dxfattribs=attrs)
            else:
                a, b = _circle_angle(segment['start'], segment), _circle_angle(segment['end'], segment)
                if segment['clockwise']:
                    a, b = b, a
                model.add_arc(segment['center'], segment['radius'], math.degrees(a), math.degrees(b), dxfattribs=attrs)
    doc.ezdxf_metadata()['HYL_EXPORT_SCOPE'] = 'Native flat plate contours; mm 1:1; engineering draft'
    doc.saveas(path)
    # HEADER comments carry provenance without introducing cutting text,
    # crosses or dimensions into model space.
    text = path.read_text(encoding='utf-8')
    comments = '\n'.join('999\n'+line for line in metadata.splitlines())+'\n'
    text, count = re.subn(r'(?m)^(\s*2\r?\nHEADER\r?\n)', lambda m: m.group(1)+comments, text, count=1)
    _need(count == 1, 'DXF 文件头无法保留原件归属。')
    path.write_text(text, encoding='utf-8')
    _need(path.stat().st_size <= MAX_DXF_BYTES, 'DXF 轮廓超出本轮文件大小限制。')
    reread = ezdxf.readfile(path)
    _need(not reread.audit().has_errors, '导出 DXF 校验未通过。')
    _need(reread.units == 4 and len(reread.modelspace()) == sum(len(c['segments']) for c in contours),
          '导出 DXF 的单位或边界数量不一致。')
    _need(all(e.dxftype() in {'LINE', 'ARC', 'CIRCLE'} and e.dxf.layer in {'CUT_OUTER', 'CUT_INNER'}
              for e in reread.modelspace()), 'DXF 包含未允许的切割实体。')
    pairs = [(c['role'], s) for c in contours for s in c['segments']]
    for entity, (role, segment) in zip(reread.modelspace(), pairs):
        _need(entity.dxf.layer == ('CUT_OUTER' if role == 'outer' else 'CUT_INNER'), 'DXF 内外轮廓图层不一致。')
        if segment['type'] == 'line':
            _need(entity.dxftype() == 'LINE' and _distance(list(entity.dxf.start)[:2], segment['start']) <= TOL
                  and _distance(list(entity.dxf.end)[:2], segment['end']) <= TOL, 'DXF 直线回读与原生轮廓不一致。')
        else:
            _need(entity.dxftype() == segment['type'].upper() and
                  _distance(list(entity.dxf.center)[:2], segment['center']) <= TOL and
                  abs(entity.dxf.radius-segment['radius']) <= TOL, 'DXF 圆或圆弧回读与原生曲线不一致。')
            if segment['type'] == 'arc':
                expected = (segment['end'], segment['start']) if segment['clockwise'] else (segment['start'], segment['end'])
                for angle, point in zip((entity.dxf.start_angle, entity.dxf.end_angle), expected):
                    actual = [entity.dxf.center[0]+entity.dxf.radius*math.cos(math.radians(angle)),
                              entity.dxf.center[1]+entity.dxf.radius*math.sin(math.radians(angle))]
                    _need(_distance(actual, point) <= TOL, 'DXF 圆弧方向或端点回读不一致。')


def _closed(segments):
    _need(segments, '原生边界为空。')
    if any(s['type'] == 'circle' for s in segments):
        _need(len(segments) == 1, '整圆边界混入其他边，不能安全导出。')
        return segments
    for current, following in zip(segments, segments[1:]+segments[:1]):
        _need(_distance(current['end'], following['start']) <= TOL, '原生边界未连续闭合，不能导出激光轮廓。')
    # A full circle represented by two or more exact native arcs stays a
    # CIRCLE. This is analytic consolidation, never a sampled approximation.
    if all(s['type'] == 'arc' for s in segments):
        first = segments[0]
        if (all(_distance(s['center'], first['center']) <= TOL and abs(s['radius']-first['radius']) <= TOL
                and s['clockwise'] == first['clockwise'] for s in segments)
                and abs(sum(_sweep(s) for s in segments)-math.tau) <= 1e-7):
            return [{'type': 'circle', 'center': first['center'], 'radius': first['radius']}]
    return segments


def export_laser_cut(shape, faces, thickness, solid_id, output_dir, source_sha256, *,
                     volume_mm3=None, attribution=None, license_text='', deadline=None):
    """Write one exact flat-plate contour DXF, or return a local unsupported state."""
    value = thickness.get('value_mm') if isinstance(thickness, dict) else None
    value = value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 else None
    temporary = None
    try:
        _need(isinstance(thickness, dict) and thickness.get('status') == 'candidate' and value is not None,
              '尚未取得可靠的统一板厚候选，不能生成平板激光轮廓。')
        _need(re.fullmatch(r'S[0-9]{4}', solid_id) is not None, '实体编号不支持安全导出。')
        _need(0 < faces.Extent() <= MAX_FACES, '实体面数量超出本轮平板轮廓预算。')
        stop = min(deadline or float('inf'), time.monotonic()+MAX_SECONDS)

        def budget():
            _need(time.monotonic() <= stop, '本件平板几何验证已达到本轮预算，未导出部分轮廓。')

        from OCP.BRep import BRep_Tool
        from OCP.BRepAdaptor import BRepAdaptor_Surface, BRepAdaptor_Curve
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.BRepCheck import BRepCheck_Analyzer
        from OCP.BRepGProp import BRepGProp
        from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
        from OCP.BRepTools import BRepTools, BRepTools_WireExplorer
        from OCP.GeomAbs import GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Line, GeomAbs_Circle
        from OCP.GProp import GProp_GProps
        from OCP.TopAbs import TopAbs_SHELL, TopAbs_WIRE, TopAbs_EDGE, TopAbs_REVERSED, TopAbs_FORWARD
        from OCP.TopExp import TopExp
        from OCP.TopoDS import TopoDS
        from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher, List_TopoDS_Shape
        from OCP.gp import gp_Vec
        from cad_thickness import _face_width

        def mapped(item, kind):
            result = IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher()
            TopExp.MapShapes_s(item, kind, result)
            return result

        def volume(item):
            props = GProp_GProps()
            BRepGProp.VolumeProperties_s(item, props, True, False, False)
            return abs(props.Mass())

        def difference_volume(a, b):
            budget()
            arguments, tools = List_TopoDS_Shape(), List_TopoDS_Shape()
            arguments.Append(a); tools.Append(b)
            cut = BRepAlgoAPI_Cut()
            cut.SetArguments(arguments); cut.SetTools(tools)
            cut.SetNonDestructive(True); cut.SetRunParallel(False)
            cut.Build()
            _need(cut.IsDone(), '原生棱柱与实体的布尔核验未完成。')
            budget()
            return volume(cut.Shape())

        budget()
        shells = mapped(shape, TopAbs_SHELL)
        _need(BRepCheck_Analyzer(shape).IsValid() and shells.Extent() and
              all(BRep_Tool.IsClosed_s(shells.FindKey(i)) for i in range(1, shells.Extent()+1)),
              '原实体未通过闭合及拓扑校验，不能导出下料轮廓。')
        source_volume = volume_mm3 if volume_mm3 is not None else volume(shape)
        _need(source_volume > 0, '实体体积无效，无法验证平板。')
        planar, surfaces = [], []
        for index in range(1, faces.Extent()+1):
            budget()
            face = TopoDS.Face(faces.FindKey(index))
            surface = BRepAdaptor_Surface(face, True)
            kind = surface.GetType()
            _need(kind in (GeomAbs_Plane, GeomAbs_Cylinder), '实体含非平面拉伸曲面、倒角或折弯曲面，本轮不生成下料轮廓。')
            surfaces.append(surface)
            if kind != GeomAbs_Plane:
                continue
            props = GProp_GProps()
            BRepGProp.SurfaceProperties_s(face, props, False, False)
            plane = surface.Plane()
            sign = -1 if face.Orientation() == TopAbs_REVERSED else 1
            normal = [sign*x for x in _xyz(plane.Axis().Direction())]
            dominant = max(range(3), key=lambda axis: abs(normal[axis]))
            planar.append({'face': face, 'index': index, 'props': props, 'plane': plane,
                           'normal': normal, 'canonical': normal[dominant] > 0, 'area': props.Mass()})
        _need(len(planar) >= 2, '没有可验证的成对平面，不能把曲面件投影为下料图。')
        planar.sort(key=lambda r: (-round(r['area'], 6), not r['canonical'], r['index']))
        selected = planar[0]
        face, plane, normal = selected['face'], selected['plane'], selected['normal']
        origin = _xyz(plane.Location())
        u = _xyz(plane.Position().XDirection())
        v = _cross(normal, u)
        _need(_face_width(selected['props'], u, v) >= value*5 and math.sqrt(selected['area']) >= value*5,
              '该实体未满足平板薄壁比例，不能把块体或杆件当作板材。')
        volume_tolerance = max(1e-5, source_volume*1e-7)
        _need(abs(selected['area']*value-source_volume) <= volume_tolerance,
              '最大平面的等厚拉伸体积与整件不一致，可能为折弯、阶梯或盲孔结构。')
        for surface in surfaces:
            budget()
            if surface.GetType() == GeomAbs_Plane:
                other = surface.Plane()
                parallel = abs(_dot(_xyz(other.Axis().Direction()), normal))
                if parallel >= 1-ANGLE_TOL:
                    height = _dot(_sub(_xyz(other.Location()), origin), normal)
                    _need(min(abs(height), abs(height+value)) <= TOL,
                          '实体含中间台阶面、盲孔底或沉孔，本轮不能作为贯通平板切割。')
                else:
                    _need(parallel <= ANGLE_TOL, '侧面不垂直于板面，可能为折弯、锥面或斜加工结构。')
            else:
                _need(abs(_dot(_xyz(surface.Cylinder().Axis().Direction()), normal)) >= 1-ANGLE_TOL,
                      '圆柱面的轴线不垂直于板面，可能为折弯或横向钻孔结构。')
        prism = BRepPrimAPI_MakePrism(TopoDS.Face(face.Oriented(TopAbs_FORWARD)),
                                      gp_Vec(*[-x*value for x in normal]), True, True).Shape()
        budget()
        _need(BRepCheck_Analyzer(prism).IsValid() and abs(volume(prism)-source_volume) <= volume_tolerance,
              '平面等厚拉伸未能重建原始实体。')
        _need(difference_volume(shape, prism) <= volume_tolerance and
              difference_volume(prism, shape) <= volume_tolerance,
              '等厚棱柱与原始实体不等价，不能将投影作为下料轮廓。')

        def point2(point):
            delta = _sub(_xyz(point), origin)
            _need(abs(_dot(delta, normal)) <= TOL, '边界曲线不在原始板面内。')
            return [_number(_dot(delta, u)), _number(_dot(delta, v))]

        outer = BRepTools.OuterWire_s(face)
        _need(not outer.IsNull(), '原始板面没有明确外边界。')
        wires = mapped(face, TopAbs_WIRE)
        _need(0 < wires.Extent() <= MAX_WIRES, '原生轮廓数量超出本轮处理范围。')
        ordered = [TopoDS.Wire(wires.FindKey(i)) for i in range(1, wires.Extent()+1)]
        ordered.sort(key=lambda wire: not wire.IsSame(outer))
        _need(sum(w.IsSame(outer) for w in ordered) == 1, '不能唯一确定平板外轮廓。')
        contours, edge_count = [], 0
        for wire_index, wire in enumerate(ordered, 1):
            budget()
            explorer = BRepTools_WireExplorer(wire, face)
            segments = []
            while explorer.More():
                budget()
                edge_count += 1
                _need(edge_count <= MAX_EDGES, '轮廓边数量超出本轮导出范围。')
                edge = explorer.Current()
                curve = BRepAdaptor_Curve(edge)
                kind = curve.GetType()
                _need(kind in (GeomAbs_Line, GeomAbs_Circle), '轮廓包含椭圆、样条或其他未支持曲线，未用折线近似。')
                first, last = curve.FirstParameter(), curve.LastParameter()
                _need(math.isfinite(first) and math.isfinite(last), '原生曲线参数无效。')
                start, end = point2(curve.Value(first)), point2(curve.Value(last))
                reverse = edge.Orientation() == TopAbs_REVERSED
                if reverse:
                    start, end = end, start
                if kind == GeomAbs_Line:
                    _need(_distance(start, end) > TOL, '轮廓含退化直线边，不能安全导出。')
                    segments.append({'type': 'line', 'start': start, 'end': end})
                else:
                    circle = curve.Circle()
                    _need(abs(_dot(_xyz(circle.Axis().Direction()), normal)) >= 1-ANGLE_TOL,
                          '圆弧轴线与板面不一致。')
                    radius = _number(circle.Radius())
                    _need(radius > TOL, '轮廓含退化圆弧。')
                    center = point2(circle.Location())
                    _need(0 < abs(last-first) <= math.tau+1e-7, '圆弧参数跨度无法确认。')
                    if abs(abs(last-first)-math.tau) <= 1e-7 and _distance(start, end) <= TOL:
                        segments.append({'type': 'circle', 'center': center, 'radius': radius})
                    else:
                        _need(_distance(start, end) > TOL, '圆弧端点退化，不能确认切割方向。')
                        clockwise = ((-1 if reverse else 1)*_dot(_xyz(circle.Axis().Direction()), normal)) < 0
                        segments.append({'type': 'arc', 'start': start, 'end': end, 'center': center,
                                         'radius': radius, 'clockwise': clockwise})
                explorer.Next()
            _need(len(segments) == mapped(wire, TopAbs_EDGE).Extent(), '原生边界包含未遍历的边，未导出不完整轮廓。')
            contours.append({'id': f'C{wire_index:04d}', 'role': 'outer' if wire.IsSame(outer) else 'inner',
                             'segments': _closed(segments)})
        minimum, maximum = _contour_bounds(contours)
        local_origin = [_number(origin[i]+u[i]*minimum[0]+v[i]*minimum[1]) for i in range(3)]
        for contour in contours:
            for segment in contour['segments']:
                for field in ('start', 'end', 'center'):
                    if field in segment:
                        segment[field] = [_number(segment[field][i]-minimum[i]) for i in range(2)]
        bounds = {'min': [0., 0.], 'max': [_number(maximum[i]-minimum[i]) for i in range(2)],
                  'size': [_number(maximum[i]-minimum[i]) for i in range(2)]}
        holes = [{'id': c['id'], 'center': c['segments'][0]['center'],
                  'diameter_mm': _number(2*c['segments'][0]['radius'])}
                 for c in contours if c['role'] == 'inner' and len(c['segments']) == 1 and c['segments'][0]['type'] == 'circle']
        filename = solid_id+'-cut.dxf'
        target = Path(output_dir)/filename
        pending = Path(output_dir)/('.'+filename+'.tmp')
        _need(not target.exists() and not target.is_symlink() and not pending.exists() and not pending.is_symlink(),
              '已有 DXF 文件不会被覆盖。')
        temporary = pending
        metadata = '\n'.join(['Engineering draft; exact native flat-plate contours; mm 1:1.',
                              'Source SHA256: '+source_sha256+'; source solid: '+solid_id,
                              'No nesting, kerf compensation or machine instructions.'])
        if attribution:
            metadata += '\n'+'\n'.join(['Derived from '+str(attribution['title'])+'; Copyright '+str(attribution['author']),
                                        'Source: '+str(attribution['source_url']), 'License: '+str(attribution['license']),
                                        'Modification: exact native flat-plate contour export; '+str(attribution['modified_at']),
                                        license_text])
        budget()
        _write_dxf(temporary, contours, bounds, metadata)
        budget()
        temporary.replace(target)
        temporary = None
        return {'status': 'ready', 'reason': '原生平面等厚棱柱与整件几何一致，已导出完整外轮廓和贯通内轮廓。',
                'unit': 'mm', 'thickness_mm': _number(value), 'source_face_id': f'F{selected["index"]:04d}',
                'plane': {'origin': local_origin, 'u': [_number(x) for x in u], 'v': [_number(x) for x in v],
                          'normal': [_number(x) for x in normal]},
                'bounds_mm': bounds, 'contours': contours, 'holes': holes, 'dxf_file': filename,
                'warnings': ['平板轮廓工程草稿，须工程师审核；尚未排版、切缝补偿或生成机床程序。']}
    except _Unsupported as error:
        return _unsupported(str(error), value)
    except ImportError:
        return _unsupported('本环境尚未配置平板 DXF 导出依赖，保留原始三维实体。', value)
    except Exception:
        return _unsupported('本件原生平板核验或 DXF 回读未完成，未发布切割文件。', value)
    finally:
        if temporary is not None and temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
