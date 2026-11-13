"""Compact engineering quantities from validated native CAD, without meshes."""
from __future__ import annotations
import copy
from decimal import Decimal, ROUND_HALF_UP
import json
import math
from cad_laser_contract import validate_laser_profile, _contour_area, _sweep
from cad_bend_contract import validate_bend_profile
from prep_record import RecordError

def _text(value):
    return format(Decimal(str(value)).quantize(Decimal('.00000001'), rounding=ROUND_HALF_UP), 'f')

def _unknown(solid_id):
    return {'schema_version': 1, 'solid_id': solid_id,
            'laser_cut': {'status': 'unresolved', 'reason': '没有可核验的原生切割轮廓。', 'unit': 'mm',
                          'thickness_mm': None, 'cut_length_mm': None, 'pierce_count': None,
                          'contour_count': None, 'holes_count': None, 'net_area_mm2': None,
                          'blank_width_mm': None, 'blank_height_mm': None,
                          'source_face_id': None, 'dxf_file': None, 'warnings': []},
            'bends': {'status': 'unresolved', 'reason': '没有可核验的原生折弯候选。',
                      'candidate_count': 0, 'candidates': [], 'warnings': []}}

def _compact(value, solid_id):
    """A bounded internal scalar receipt; full geometric proof stays in CAD."""
    if not isinstance(value, dict) or set(value) != {'schema_version', 'solid_id', 'laser_cut', 'bends'}:
        return None
    if value.get('schema_version') != 1 or value.get('solid_id') != solid_id:
        return None
    try:
        if len(json.dumps(value, allow_nan=False)) > 256 * 1024:
            return None
        laser, bends = value['laser_cut'], value['bends']
        if not isinstance(laser, dict) or not isinstance(bends, dict):
            return None
        if laser.get('status') not in {'ready', 'unsupported', 'unresolved'} or bends.get('status') not in {'candidate', 'not_applicable', 'unresolved'}:
            return None
        if not isinstance(bends.get('candidates'), list) or len(bends['candidates']) > 128 or bends.get('candidate_count') != len(bends['candidates']):
            return None
        if laser['status'] == 'ready':
            for key in ('cut_length_mm', 'net_area_mm2', 'blank_width_mm', 'blank_height_mm'):
                number = float(laser[key])
                if not math.isfinite(number) or not 0 < number <= 1e18:
                    return None
            if type(laser.get('pierce_count')) is not int or not 0 < laser['pierce_count'] <= 1024 or laser.get('contour_count') != laser['pierce_count']:
                return None
        return copy.deepcopy(value)
    except (ValueError, TypeError, KeyError, OverflowError):
        return None

def summarise_native_processes(solid):
    solid_id = solid.get('id')
    output = _unknown(solid_id)
    if 'process_evidence' in solid and 'laser_cut' not in solid and 'bend_features' not in solid:
        return _compact(solid['process_evidence'], solid_id) or output
    topology = solid.get('topology') or {}
    if topology.get('valid') is not True or topology.get('closed') is not True:
        output['laser_cut']['reason'] = '实体有效性或封闭性尚未满足工艺计量条件。'
        return output
    face_count = topology.get('faces')
    if type(face_count) is not int or face_count <= 0:
        return output
    profile = solid.get('laser_cut')
    if isinstance(profile, dict):
        try:
            validate_laser_profile(profile, solid_id, face_count,
                solid_bounds=solid.get('bounds_mm'), volume_mm3=solid.get('volume_mm3'))
            summary = output['laser_cut']
            summary.update(status=profile['status'], reason=profile['reason'],
                           thickness_mm=profile['thickness_mm'], warnings=copy.deepcopy(profile['warnings']))
            if profile['status'] == 'ready':
                lengths, area = [], 0.0
                for contour in profile['contours']:
                    area += _contour_area(contour['segments']) * (1 if contour['role'] == 'outer' else -1)
                    for segment in contour['segments']:
                        kind = segment['type']
                        if kind == 'line':
                            lengths.append(math.dist(segment['start'], segment['end']))
                        else:
                            lengths.append(segment['radius'] * (math.tau if kind == 'circle' else _sweep(segment)))
                summary.update(cut_length_mm=_text(math.fsum(lengths)),
                    pierce_count=len(profile['contours']), contour_count=len(profile['contours']),
                    piercing_basis='每个闭合轮廓暂按一次穿孔；不是机床刀路。', holes_count=len(profile['holes']),
                    net_area_mm2=_text(area), blank_width_mm=_text(profile['bounds_mm']['size'][0]),
                    blank_height_mm=_text(profile['bounds_mm']['size'][1]),
                    source_face_id=profile['source_face_id'], dxf_file=profile['dxf_file'])
        except (RecordError, ValueError, TypeError, KeyError, OverflowError):
            output['laser_cut'] = _unknown(solid_id)['laser_cut']
            output['laser_cut']['reason'] = '原生轮廓与当前实体的几何或来源校验未通过。'
    profile = solid.get('bend_features')
    if isinstance(profile, dict):
        try:
            validate_bend_profile(profile, face_count, solid_bounds=solid.get('bounds_mm'))
            candidates = [{key: copy.deepcopy(bend[key]) for key in
                ('id', 'status', 'rotation_angle_deg', 'included_angle_deg', 'inner_radius_mm',
                 'thickness_mm', 'length_mm', 'direction')} for bend in profile['bends']]
            output['bends'].update(status=profile['status'], reason=profile['reason'],
                candidate_count=len(candidates), candidates=candidates, warnings=copy.deepcopy(profile['warnings']))
        except (RecordError, ValueError, TypeError, KeyError, OverflowError):
            output['bends']['reason'] = '原生折弯候选与当前实体的几何或来源校验未通过。'
    return output
