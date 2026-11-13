"""Customer technical projections and native contour previews, without costing.

Inputs have already passed the source-bound CAD cache contract. This module
does not price operations, infer a manufacturing route or reconstruct geometry.
"""
from __future__ import annotations

import html
import math
import re

from batch_engineering_bom import MATERIAL_LABELS
from customer_weight_view import weight_projection, weight_evidence

LINE_ID = re.compile(r'L[0-9]{4,7}\Z')


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and abs(number) < 1e12 else None


def _text(value, maximum=500):
    return value[:maximum] if isinstance(value, str) else ''


def _quantity(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 1000000000:
        return value
    return value if isinstance(value, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value) and len(value) <= 60 else None


def _safe_details(evidence):
    """Only known geometric measurements enter a customer process card."""
    if not isinstance(evidence, dict):
        return {}
    out = {}
    for key in ('cut_length_mm', 'pierce_count', 'piercing_count', 'contour_count', 'bend_count',
                'thickness_mm', 'net_area_mm2', 'blank_width_mm', 'blank_height_mm',
                'length_mm', 'rotation_angle_deg', 'included_angle_deg', 'inner_radius_mm'):
        value = evidence.get(key)
        if _number(value) is not None:
            out[key] = value
    for key in ('solid_ids', 'source_solid_ids', 'source_face_ids'):
        value = evidence.get(key)
        if isinstance(value, list):
            out[key] = [item for item in value[:200] if isinstance(item, str) and re.fullmatch(r'[SF][0-9]{4,7}', item)]
    return out


def process_projection(process):
    if not isinstance(process, dict):
        return None
    kind = _text(process.get('kind'), 80)
    basis = {'laser_cut': '按原生闭合轮廓的切割长度计量', 'laser': '按原生闭合轮廓的切割长度计量',
             'piercing': '每个闭合轮廓暂按一次穿孔估算；不是机床刀路', 'laser_piercing': '每个闭合轮廓暂按一次穿孔估算；不是机床刀路',
             'laser_setup': '本单同一零件分组准备一次，不按零件数量重复',
             'bend': '原生圆柱折弯候选，角度、半径和方向待工程核对',
             'bending': '原生圆柱折弯候选，角度、半径和方向待工程核对',
             'drilling': '钻孔与激光贯通孔分开核对', 'welding': '焊接位置、方法及质量要求待工程核对'}.get(kind,
             '按本件已识别的工程量核对，尚未经工程审核')
    if isinstance(process.get('evidence'), dict) and process['evidence'].get('manufacturing_release') is False:
        basis = {'laser_cut': '原生边界行程代理暂估，可能含非切割分界；不是机床刀路',
                 'laser_piercing': '闭合实体拓扑中的贯通通道加外轮廓暂估；不是钻孔工艺',
                 'bending': '按原生折弯候选计量；拟定范围仍待工程核对'}.get(kind, basis)
    return {'id': _text(process.get('id'), 100), 'kind': kind,
            'name': _text(process.get('label') or process.get('name'), 100),
            'status': _text(process.get('status'), 80),
            'quantity_per_piece': _quantity(process.get('quantity_per_piece')),
            'quantity_total': _quantity(process.get('quantity_total')),
            'unit': _text(process.get('unit'), 40), 'basis': basis,
            'details': _safe_details(process.get('evidence'))}


def part_projection(line, native_values=None, *, native_row=None, source_sha256=None,
                    geometry_source='saved_native_evidence'):
    native_values = native_values if isinstance(native_values, dict) else {}
    manufacturing = line.get('manufacturing') if isinstance(line.get('manufacturing'), dict) else {}
    features = manufacturing.get('features') if isinstance(manufacturing.get('features'), dict) else {}
    weight_check = manufacturing.get('weight_check') if isinstance(manufacturing.get('weight_check'), dict) else {}
    weights = weight_projection(line)
    raw = _text(weights['weight_assumptions']['cad_material_text'], 300)
    adopted = weights['weight_assumptions']['adopted_material_label']
    material = {'text': raw or adopted or '未标注', 'source': 'cad' if raw else 'estimate' if adopted else 'unknown',
                'adopted': not bool(raw) and bool(adopted)}
    blockers = []
    for value in manufacturing.get('blockers', []):
        text = value.get('message') if isinstance(value, dict) else value
        if isinstance(text, str) and text.strip():
            blockers.append(_text(text))
    if not manufacturing:
        blockers.append('原报价未记录分项工艺工程量，需工厂重新核对。')
    weights['weight_evidence'] = weight_evidence(line, native_row, source_sha256=source_sha256,
                                               geometry_source=geometry_source)
    from batch_process_estimate import safe_quantities
    return {'id': line['id'], 'part_name': _text(line.get('part_name')),
            'source': _text(line.get('document')), 'quantity_per_set': _quantity(line.get('quantity_per_set')),
            'quantity_total': _quantity(line.get('quantity_total')), 'material': material,
            'thickness_mm': features.get('thickness_mm') if _number(features.get('thickness_mm')) is not None
                            else native_values.get('thickness_mm') if _number(native_values.get('thickness_mm')) is not None else None,
            'dimensions_mm': None,
            **weights,
            'weight_check': {'role': 'cross_check',
                'status': weight_check.get('status') if weight_check.get('status') in {'consistent', 'conflict', 'unavailable'} else 'unavailable'},
            'status': manufacturing.get('status', 'blocked'),
            'quotation_quantities': safe_quantities(manufacturing.get('quotation_quantities')),
            'processes': [row for item in manufacturing.get('processes', []) if (row := process_projection(item)) is not None],
            'blockers': blockers, 'geometry_url': None, 'thumbnail_url': None, 'step_url': None,
            'laser': {'status': 'unavailable', 'svg_url': None, 'dxf_url': None}, 'bends': []}


def bend_projection(profile):
    if not isinstance(profile, dict) or profile.get('status') != 'candidate':
        return []
    return [{'id': _text(row.get('id'), 40), 'angle_deg': row['rotation_angle_deg'],
             'radius_mm': row['inner_radius_mm'], 'length_mm': row['length_mm'], 'status': 'pending_review'}
            for row in profile.get('bends', [])[:128] if isinstance(row, dict)
            and all(_number(row.get(key)) is not None for key in ('rotation_angle_deg', 'inner_radius_mm', 'length_mm'))]


def laser_svg(profile, part_name):
    """Draw original validated native lines/arcs/circles, with no invented labels."""
    if not isinstance(profile, dict) or profile.get('status') != 'ready':
        raise ValueError('此零件没有已验证的平板切割轮廓。')
    bounds = profile['bounds_mm']
    width, height = bounds['size']
    if any(_number(value) is None or value <= 0 for value in (width, height)):
        raise ValueError('切割轮廓尺寸不可用。')
    stroke = max(width, height) / 450
    margin = max(width, height) * .04
    def n(value):
        number = _number(value)
        if number is None:
            raise ValueError('切割轮廓坐标不可用。')
        return format(number, '.9g')
    output = []
    for contour in profile['contours']:
        paths = []
        for segment in contour['segments']:
            kind = segment['type']
            if kind == 'circle':
                center, radius = segment['center'], segment['radius']
                paths.append('<circle cx="'+n(center[0])+'" cy="'+n(center[1])+'" r="'+n(radius)+'"/>')
            elif kind == 'line':
                p, q = segment['start'], segment['end']
                paths.append('<path d="M '+n(p[0])+' '+n(p[1])+' L '+n(q[0])+' '+n(q[1])+'"/>')
            elif kind == 'arc':
                p, q, center, radius = segment['start'], segment['end'], segment['center'], segment['radius']
                start = math.atan2(p[1]-center[1], p[0]-center[0])
                end = math.atan2(q[1]-center[1], q[0]-center[0])
                delta = ((start-end) if segment['clockwise'] else (end-start)) % math.tau
                paths.append('<path d="M '+n(p[0])+' '+n(p[1])+' A '+n(radius)+' '+n(radius)+' 0 '
                             +str(int(delta > math.pi))+' '+str(int(not segment['clockwise']))+' '+n(q[0])+' '+n(q[1])+'"/>')
            else:
                raise ValueError('切割轮廓包含未支持的边界。')
        color = '#24657d' if contour['role'] == 'outer' else '#cf762c'
        output.append('<g stroke="'+color+'">'+''.join(paths)+'</g>')
    return ('<svg xmlns="http://www.w3.org/2000/svg" role="img" viewBox="'+n(-margin)+' '+n(-margin)+' '
            +n(width+margin*2)+' '+n(height+margin*2)+'"><title>'+html.escape(str(part_name))
            +' · 原生平板轮廓，毫米，待工程审核</title><rect x="'+n(-margin)+'" y="'+n(-margin)
            +'" width="'+n(width+margin*2)+'" height="'+n(height+margin*2)+'" fill="#fff"/>'
            +'<g transform="translate(0 '+n(height)+') scale(1 -1)" fill="none" stroke-width="'+n(stroke)
            +'" stroke-linecap="round" stroke-linejoin="round">'+''.join(output)+'</g></svg>').encode('utf-8')
