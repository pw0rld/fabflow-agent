"""Validate source-bound, unconfirmed native cylindrical bend evidence.

These are geometry candidates for engineering review. They are not a bend
sequence, machine program, developed blank, or an adopted manufacturing value.
"""
from __future__ import annotations

import math
import re

from prep_record import require

MAX_BENDS = 128
MAX_VERTICES = 50000
MAX_TRIANGLES = 100000
FACE_ID = re.compile(r'F[0-9]{4,7}\Z')
BEND_ID = re.compile(r'B[0-9]{4}\Z')
PROFILE_FIELDS = {'status', 'reason', 'unit', 'bends', 'warnings'}
BEND_FIELDS = {'id', 'status', 'inner_face_ids', 'outer_face_ids', 'flange_face_ids',
               'axis', 'inner_radius_mm', 'outer_radius_mm', 'thickness_mm',
               'rotation_angle_deg', 'included_angle_deg', 'length_mm',
               'tangent_lines', 'mesh', 'direction', 'reference_face_id', 'warnings'}


def _number(value, label, *, positive=False):
    require(type(value) in {int, float} and math.isfinite(value) and
            abs(value) <= 1e9 and (not positive or value > 0), label + '无效。')
    return value


def _text(value, maximum, label):
    require(isinstance(value, str) and value.strip() and len(value) <= maximum and
            not any(ord(char) < 32 and char not in '\n\t' for char in value), label + '无效。')


def _warnings(value):
    require(isinstance(value, list) and len(value) <= 32, '折弯提示过多。')
    for item in value:
        _text(item, 1000, '折弯提示')


def _vector(value, label):
    require(isinstance(value, list) and len(value) == 3, label + '坐标无效。')
    for item in value:
        _number(item, label)
    return value


def _sub(a, b):
    return [x-y for x, y in zip(a, b)]


def _dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def _norm(value):
    return math.sqrt(_dot(value, value))


def _cross(a, b):
    return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]


def _angle(a, b):
    denominator = _norm(a)*_norm(b)
    require(denominator > 0, '折弯曲带径向方向无效。')
    return math.degrees(math.acos(max(-1, min(1, _dot(a, b)/denominator))))


def _face_list(value, face_count, label, *, length=None):
    require(isinstance(value, list) and 0 < len(value) <= 128 and
            (length is None or len(value) == length), label + '数量无效。')
    require(all(isinstance(face, str) and FACE_ID.fullmatch(face) and
                1 <= int(face[1:]) <= face_count for face in value) and
            len(set(value)) == len(value), label + '不属于当前实体或重复。')
    return value


def validate_bend_profile(value, face_count, *, solid_bounds=None):
    require(isinstance(value, dict) and set(value) == PROFILE_FIELDS, '折弯识别字段无效。')
    require(isinstance(value['status'], str) and value['status'] in
            {'candidate', 'unresolved', 'not_applicable'}, '折弯识别状态无效。')
    _text(value['reason'], 2000, '折弯识别说明')
    require(value['unit'] == 'mm', '折弯识别必须采用毫米单位。')
    _warnings(value['warnings'])
    bends = value['bends']
    require(isinstance(bends, list) and len(bends) <= MAX_BENDS, '折弯候选数量超出范围。')
    if value['status'] != 'candidate':
        require(not bends, '未确定或不适用的实体不能附带折弯候选。')
        return value
    require(bends, '折弯候选缺少真实曲面证据。')
    scale = max(1, *solid_bounds['size']) if solid_bounds else 1
    world_tolerance = max(1e-4, scale*1e-7)
    seen, vertices, triangles = set(), 0, 0
    for bend in bends:
        require(isinstance(bend, dict) and set(bend) == BEND_FIELDS, '折弯候选字段无效。')
        bid = bend['id']
        require(isinstance(bid, str) and BEND_ID.fullmatch(bid) and int(bid[1:]) > 0 and bid not in seen,
                '折弯候选编号无效或重复。')
        seen.add(bid)
        require(bend['status'] == 'pending_review' and bend['direction'] == 'unconfirmed' and
                bend['reference_face_id'] is None, '折弯候选必须保留待核对与方向未确认状态。')
        _warnings(bend['warnings'])
        inner = _face_list(bend['inner_face_ids'], face_count, '折弯内侧曲面')
        outer = _face_list(bend['outer_face_ids'], face_count, '折弯外侧曲面')
        flanges = bend['flange_face_ids']
        require(isinstance(flanges, list) and len(flanges) == 2, '折弯必须有两侧翼板的内外平面。')
        flange_ids = [face for pair in flanges for face in
                      _face_list(pair, face_count, '折弯翼板面', length=2)]
        all_faces = inner + outer + flange_ids
        require(len(set(all_faces)) == len(all_faces), '折弯曲面与翼板面证据相互重复。')
        axis = bend['axis']
        require(isinstance(axis, dict) and set(axis) == {'point', 'direction'}, '折弯轴字段无效。')
        point = _vector(axis['point'], '折弯轴点')
        direction = _vector(axis['direction'], '折弯轴方向')
        require(abs(_dot(direction, direction)-1) <= 1e-6, '折弯轴方向必须是单位向量。')
        for key in ('inner_radius_mm', 'outer_radius_mm', 'thickness_mm', 'length_mm'):
            _number(bend[key], '折弯 ' + key, positive=True)
        ri, ro, thickness, length = (bend[key] for key in
                                    ('inner_radius_mm', 'outer_radius_mm', 'thickness_mm', 'length_mm'))
        tolerance = max(1e-4, thickness*1e-3)
        require(thickness <= 10000 and abs(ro-ri-thickness) <= tolerance,
                '折弯内外半径差与板厚不一致。')
        rotation = _number(bend['rotation_angle_deg'], '折弯转角', positive=True)
        included = _number(bend['included_angle_deg'], '折弯夹角')
        require(rotation < 180 and 0 < included < 180 and abs(rotation+included-180) <= 1e-5,
                '折弯转角与夹角不是互补角。')
        lines = bend['tangent_lines']
        require(isinstance(lines, list) and len(lines) == 2, '折弯缺少两条真实相切边界。')
        line_radials, line_levels, source_points = [], [], []

        def cylindrical(position):
            delta = _sub(position, point)
            level = _dot(delta, direction)
            radial = [delta[i]-level*direction[i] for i in range(3)]
            return level, radial

        for line in lines:
            require(isinstance(line, dict) and set(line) == {'start', 'end'}, '折弯切线字段无效。')
            start, end = (_vector(line[key], '折弯切线端点') for key in ('start', 'end'))
            delta = _sub(end, start)
            require(_norm(delta) > 1e-8 and abs(_norm(delta)-length) <= max(1e-4, length*1e-6) and
                    _norm(_cross(delta, direction)) <= max(1e-4, length*1e-6),
                    '折弯切线不平行于轴或长度不一致。')
            levels, radials = zip(*(cylindrical(pos) for pos in (start, end)))
            require(all(abs(_norm(radial)-ro) <= max(1e-4, ro*1e-6) for radial in radials) and
                    _norm(_sub(radials[0], radials[1])) <= world_tolerance,
                    '折弯切线不在外侧圆柱的边界上。')
            line_radials.append(radials[0])
            line_levels.append(sorted(levels))
            source_points.extend((start, end))
        require(all(abs(a-b) <= max(1e-4, length*1e-6)
                    for a, b in zip(*line_levels)), '折弯两侧切线的轴向范围不一致。')
        require(abs(_angle(*line_radials)-rotation) <= .01,
                '折弯转角与两侧切线的径向角度不一致。')
        mesh = bend['mesh']
        require(isinstance(mesh, dict) and set(mesh) == {'positions', 'indices'}, '折弯曲带几何无效。')
        positions, indices = mesh['positions'], mesh['indices']
        require(isinstance(positions, list) and len(positions) >= 12 and len(positions) % 3 == 0,
                '折弯曲带顶点无效。')
        vertex_count = len(positions)//3
        vertices += vertex_count
        require(vertices <= MAX_VERTICES, '折弯曲带顶点超出范围。')
        for number in positions:
            _number(number, '折弯曲带顶点')
        require(isinstance(indices, list) and len(indices) >= 6 and len(indices) % 3 == 0 and
                all(type(index) is int and 0 <= index < vertex_count for index in indices),
                '折弯曲带三角索引无效。')
        triangles += len(indices)//3
        require(triangles <= MAX_TRIANGLES, '折弯曲带三角形超出范围。')
        mesh_points = [positions[index:index+3] for index in range(0, len(positions), 3)]
        for index in range(0, len(indices), 3):
            a, b, c = (mesh_points[indices[index+j]] for j in range(3))
            require(_norm(_cross(_sub(b, a), _sub(c, a))) > 1e-12,
                    '折弯曲带包含退化三角形。')
        radius_evidence = [False, False]
        axial_evidence = [False, False]
        angle_evidence = [False, False]
        referenced = set(indices)
        for vertex_index, pos in enumerate(mesh_points):
            level, radial = cylindrical(pos)
            radius = _norm(radial)
            require(radius > 0 and min(abs(radius-ri), abs(radius-ro)) <= max(1e-4, ro*1e-6) and
                    line_levels[0][0]-world_tolerance <= level <= line_levels[0][1]+world_tolerance,
                    '折弯曲带不在已声明的圆柱带范围内。')
            require(_angle(line_radials[0], radial)+_angle(radial, line_radials[1]) <= rotation+.02,
                    '折弯曲带超出两条切线的角度范围。')
            if vertex_index in referenced:
                for index, measured in enumerate((ri, ro)):
                    radius_evidence[index] |= abs(radius-measured) <= max(1e-4, ro*1e-6)
                for index in range(2):
                    axial_evidence[index] |= abs(level-line_levels[0][index]) <= world_tolerance
                    angle_evidence[index] |= _angle(line_radials[index], radial) <= .01
        require(all(radius_evidence), '折弯曲带缺少内外两侧圆柱面。')
        require(all(axial_evidence) and all(angle_evidence), '折弯曲带没有覆盖已声明的完整边界。')
        source_points.extend(mesh_points)
        if solid_bounds is not None:
            require(all(lo-world_tolerance <= coordinate <= hi+world_tolerance
                        for pos in source_points for coordinate, lo, hi in
                        zip(pos, solid_bounds['min'], solid_bounds['max'])),
                    '折弯曲带或切线不属于当前实体的空间范围。')
    return value
