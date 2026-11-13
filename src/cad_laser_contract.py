"""Bounded, source-bound flat-part contour evidence for engineering review.

This module validates the native worker's output. It does not project models,
unfold bent parts, merge manufacturing groups, or manufacture missing paths.
"""
from __future__ import annotations

import math
import re

from prep_record import require

MAX_CONTOURS = 1024
MAX_SEGMENTS = 20000
COORDINATE_LIMIT = 1e9
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
FACE_ID = re.compile(r'F[0-9]{4,7}\Z')
FIELDS = {'status', 'reason', 'unit', 'thickness_mm', 'source_face_id', 'plane',
          'bounds_mm', 'contours', 'holes', 'dxf_file', 'warnings'}


def _number(value, label, *, positive=False):
    require(type(value) in {int, float} and math.isfinite(value) and
            abs(value) <= COORDINATE_LIMIT and (not positive or value > 0), label + '无效。')
    return value


def _text(value, maximum, label):
    require(isinstance(value, str) and value.strip() and len(value) <= maximum and
            not any(ord(char) < 32 and char not in '\n\t' for char in value), label + '无效。')


def _vector(value, length, label):
    require(isinstance(value, list) and len(value) == length, label + '坐标无效。')
    for number in value:
        _number(number, label)
    return value


def _distance(a, b):
    return math.sqrt(sum((x-y)**2 for x, y in zip(a, b)))


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _sweep(segment):
    center = segment['center']
    start = math.atan2(segment['start'][1]-center[1], segment['start'][0]-center[0])
    end = math.atan2(segment['end'][1]-center[1], segment['end'][0]-center[0])
    return ((start-end) if segment['clockwise'] else (end-start)) % math.tau


def _arc_extrema(segment):
    center, radius = segment['center'], segment['radius']
    start = math.atan2(segment['start'][1]-center[1], segment['start'][0]-center[0])
    sweep = _sweep(segment)
    points = [segment['start'], segment['end']]
    for angle in (0, math.pi/2, math.pi, 3*math.pi/2):
        distance = ((start-angle) if segment['clockwise'] else (angle-start)) % math.tau
        if distance <= sweep + 1e-8:
            points.append([center[0]+radius*math.cos(angle), center[1]+radius*math.sin(angle)])
    return points


def _circular_contour(segments, tolerance):
    if len(segments) == 1 and segments[0]['type'] == 'circle':
        return segments[0]['center'], segments[0]['radius']
    if not segments or any(item['type'] != 'arc' for item in segments):
        return None
    center, radius = segments[0]['center'], segments[0]['radius']
    if (any(_distance(item['center'], center) > tolerance or
            abs(item['radius']-radius) > tolerance for item in segments)
            or abs(sum(_sweep(item) for item in segments)-math.tau) > 1e-6):
        return None
    return center, radius


def _contour_area(segments):
    if segments[0]['type'] == 'circle':
        return math.pi*segments[0]['radius']**2
    area = 0.0
    for segment in segments:
        p, q = segment['start'], segment['end']
        if segment['type'] == 'line':
            area += p[0]*q[1]-p[1]*q[0]
        else:
            center = segment['center']
            delta = _sweep(segment)*(-1 if segment['clockwise'] else 1)
            area += (center[0]*(q[1]-p[1])-center[1]*(q[0]-p[0]) + segment['radius']**2*delta)
    return abs(area)/2


def validate_laser_profile(value, solid_id, face_count, *, solid_bounds=None, volume_mm3=None):
    require(isinstance(value, dict) and set(value) == FIELDS, '激光轮廓字段无效。')
    require(isinstance(value['status'], str) and value['status'] in {'ready', 'unsupported'},
            '激光轮廓状态无效。')
    _text(value['reason'], 2000, '激光轮廓说明')
    require(value['unit'] == 'mm', '激光轮廓必须采用毫米单位。')
    warnings = value['warnings']
    require(isinstance(warnings, list) and len(warnings) <= 32, '激光轮廓提示过多。')
    for warning in warnings:
        _text(warning, 1000, '激光轮廓提示')
    if value['thickness_mm'] is not None:
        _number(value['thickness_mm'], '激光轮廓板厚', positive=True)
        require(value['thickness_mm'] <= 10000, '激光轮廓板厚超出范围。')
    contours, holes = value['contours'], value['holes']
    require(isinstance(contours, list) and len(contours) <= MAX_CONTOURS,
            '激光轮廓数量超出范围。')
    require(isinstance(holes, list) and len(holes) <= MAX_CONTOURS, '激光孔位数量超出范围。')
    if value['status'] == 'unsupported':
        require(not contours and not holes and all(value[key] is None for key in
                ('source_face_id', 'plane', 'bounds_mm', 'dxf_file')),
                '未支持的实体不能附带下料轮廓或文件。')
        return value
    require(value['thickness_mm'] is not None, '平板轮廓缺少真实厚度。')
    face = value['source_face_id']
    require(isinstance(face, str) and FACE_ID.fullmatch(face) and 1 <= int(face[1:]) <= face_count,
            '激光轮廓的源面不属于当前实体。')
    require(value['dxf_file'] == solid_id + '-cut.dxf', '激光 DXF 文件不属于当前实体。')
    plane = value['plane']
    require(isinstance(plane, dict) and set(plane) == {'origin', 'u', 'v', 'normal'}, '激光工作平面无效。')
    for key in ('origin', 'u', 'v', 'normal'):
        _vector(plane[key], 3, '激光平面 ' + key)
    for key in ('u', 'v', 'normal'):
        require(abs(_dot(plane[key], plane[key])-1) <= 1e-6, '激光平面轴必须为单位向量。')
    require(all(abs(_dot(plane[a], plane[b])) <= 1e-6 for a, b in
                (('u', 'v'), ('u', 'normal'), ('v', 'normal'))) and
            _distance(_cross(plane['u'], plane['v']), plane['normal']) <= 1e-6,
            '激光工作平面轴不正交或方向不一致。')
    bounds = value['bounds_mm']
    require(isinstance(bounds, dict) and set(bounds) == {'min', 'max', 'size'}, '激光平面包络无效。')
    for key in ('min', 'max', 'size'):
        _vector(bounds[key], 2, '激光平面包络')
    scale = max(1, *bounds['size'])
    tolerance = max(1e-5, scale*1e-9)
    require(all(abs(v) <= tolerance for v in bounds['min']), '激光平面轮廓原点尚未规范化。')
    require(all(lo < hi and size > 0 and abs(hi-lo-size) <= tolerance
                for lo, hi, size in zip(bounds['min'], bounds['max'], bounds['size'])),
            '激光平面包络尺寸不一致。')
    require(contours, '激光轮廓不能为空。')
    seen, points, segment_count, circles, outer_count = set(), [], 0, [], 0
    outer_area, inner_area = 0.0, 0.0
    for contour in contours:
        require(isinstance(contour, dict) and set(contour) == {'id', 'role', 'segments'}, '激光轮廓记录无效。')
        cid = contour['id']
        require(isinstance(cid, str) and ID.fullmatch(cid) and cid not in seen, '激光轮廓编号无效或重复。')
        seen.add(cid)
        require(isinstance(contour['role'], str) and contour['role'] in {'outer', 'inner'}, '激光轮廓角色无效。')
        outer_count += contour['role'] == 'outer'
        segments = contour['segments']
        require(isinstance(segments, list) and segments, '激光轮廓缺少完整边。')
        segment_count += len(segments)
        require(segment_count <= MAX_SEGMENTS, '激光轮廓边数量超出范围。')
        for segment in segments:
            require(isinstance(segment, dict), '激光轮廓边无效。')
            kind = segment.get('type')
            fields = {'line': {'type', 'start', 'end'},
                      'arc': {'type', 'start', 'end', 'center', 'radius', 'clockwise'},
                      'circle': {'type', 'center', 'radius'}}
            require(isinstance(kind, str) and kind in fields and set(segment) == fields[kind],
                    '激光轮廓只支持明确的直线、圆弧或圆。')
            if kind in {'line', 'arc'}:
                for key in ('start', 'end'):
                    _vector(segment[key], 2, '激光轮廓边端点')
                require(_distance(segment['start'], segment['end']) > 1e-8, '激光轮廓包含零长度边。')
                points.extend((segment['start'], segment['end']))
            if kind in {'arc', 'circle'}:
                _vector(segment['center'], 2, '激光圆心')
                radius = _number(segment['radius'], '激光圆半径', positive=True)
                if kind == 'arc':
                    require(type(segment['clockwise']) is bool, '激光圆弧方向无效。')
                    require(all(abs(_distance(segment[key], segment['center'])-radius) <= tolerance
                                for key in ('start', 'end')), '激光圆弧端点与半径不一致。')
                    require(_sweep(segment) > 1e-10, '激光圆弧扫角无效。')
                    points.extend(_arc_extrema(segment))
                else:
                    require(len(segments) == 1, '整圆不能与其他边拼接成同一轮廓。')
                    x, y = segment['center']
                    points.extend(([x-radius, y-radius], [x+radius, y+radius]))
        if segments[0]['type'] != 'circle':
            require(all(_distance(segment['end'], segments[(index+1) % len(segments)]['start']) <= tolerance
                        for index, segment in enumerate(segments)), '激光轮廓没有连续闭合。')
        circle = _circular_contour(segments, tolerance)
        if contour['role'] == 'inner' and circle:
            circles.append(circle)
        area = _contour_area(segments)
        require(math.isfinite(area) and area > tolerance**2, '激光轮廓未形成有效封闭面积。')
        if contour['role'] == 'outer':
            outer_area = area
        else:
            inner_area += area
    require(outer_count == 1, '每个原生实体必须有且仅有一个外轮廓。')
    require(outer_area-inner_area > tolerance**2, '激光轮廓未形成有效材料截面。')
    if volume_mm3 is not None:
        expected = (outer_area-inner_area)*value['thickness_mm']
        require(abs(expected-volume_mm3) <= max(1e-5, abs(volume_mm3)*1e-5),
                '激光轮廓截面与原生实体体积不一致。')
    require(all(bounds['min'][axis]-tolerance <= point[axis] <= bounds['max'][axis]+tolerance
                for point in points for axis in range(2)), '激光轮廓超出声明的平面范围。')
    actual_min = [min(p[axis] for p in points) for axis in range(2)]
    actual_max = [max(p[axis] for p in points) for axis in range(2)]
    require(all(abs(bounds['min'][axis]-actual_min[axis]) <= tolerance and
                abs(bounds['max'][axis]-actual_max[axis]) <= tolerance for axis in range(2)),
            '激光轮廓包络与实际几何不一致。')
    if solid_bounds is not None:
        # Each point below is on an actual line/circle/arc, not a fictitious
        # rectangle corner, so arbitrary rigid placement remains supported.
        world_tolerance = max(1e-5, max(solid_bounds['size'])*1e-9)
        for point in points:
            world = [plane['origin'][axis]+point[0]*plane['u'][axis]+point[1]*plane['v'][axis]
                     for axis in range(3)]
            require(all(lo-world_tolerance <= coordinate <= hi+world_tolerance
                        for coordinate, lo, hi in zip(world, solid_bounds['min'], solid_bounds['max'])),
                    '激光工作平面与原生实体空间范围不一致。')
    require(len(holes) == len(circles), '圆孔标注与实际内轮廓数量不一致。')
    hole_ids, available = set(), list(circles)
    for hole in holes:
        require(isinstance(hole, dict) and set(hole) == {'id', 'center', 'diameter_mm'}, '激光孔位字段无效。')
        hid = hole['id']
        require(isinstance(hid, str) and ID.fullmatch(hid) and hid not in hole_ids, '激光孔位编号无效或重复。')
        hole_ids.add(hid)
        _vector(hole['center'], 2, '激光孔位中心')
        diameter = _number(hole['diameter_mm'], '激光孔径', positive=True)
        match = next((i for i, (center, radius) in enumerate(available)
                      if _distance(center, hole['center']) <= tolerance and abs(diameter-2*radius) <= tolerance), None)
        require(match is not None, '激光孔位并非来自当前实体的真实圆孔轮廓。')
        available.pop(match)
    return value
