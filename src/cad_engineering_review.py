"""Read-only checks of saved CAD manufacturing evidence, never engineering approval.

Input is the service's public manufacturing state. No CAD files, AI output,
order databases or customer reference answers are opened by this projection.
"""
from __future__ import annotations

import copy
import math

from cad_bom import group_material_summary

STATUSES = ('recorded', 'pending_review', 'missing', 'conflict', 'unreadable', 'not_evaluated')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _different(left, right):
    return abs(left - right) > max(.001, max(left, right) * .001)


def _manual(value, unit=None):
    result = {'basis': 'manual_entry', 'value': copy.deepcopy(value)}
    if unit is not None:
        result['unit'] = unit
    return result


def _material(group, measurements):
    summary = group_material_summary(group, measurements)
    evidence = []
    for sid in group['solid_ids']:
        measured = measurements.get(sid)
        if not measured:
            continue
        for entry in measured.get('entries', []):
            evidence.append({'solid_id': sid, 'basis': 'cad_property',
                             'raw_text': entry.get('name', '') + ' @ ' + entry.get('assignment_label', '') +
                             ' → ' + entry.get('material_label', '')})
    adopted = group.get('material')
    if adopted:
        evidence.insert(0, _manual(adopted))
    if summary['status'] == 'conflict':
        return 'conflict', '本组 CAD 材料原文不一致；已填写材料不消除原文差异，请分别核对。', evidence
    if adopted:
        if summary['status'] == 'candidate' and adopted.strip() != summary['value'].strip():
            return ('pending_review', '已填写材料与 CAD 原文文字不同；可能是牌号补充或修改，需核对适用范围。', evidence)
        return 'recorded', '材料已填写；分组确认不等于材料牌号或适用范围已完成工程审核。', evidence
    if summary['status'] == 'candidate':
        return 'pending_review', summary['reason'], evidence
    if evidence:
        return 'pending_review', summary['reason'], evidence
    return 'missing', '当前保存结果未形成明确材料值；不能据此认定原 CAD 没有材料。', evidence


def _thickness(group, measurements):
    found = [measurements.get(sid) for sid in group['solid_ids']]
    values, evidence = [], []
    for sid, measured in zip(group['solid_ids'], found):
        if not measured:
            continue
        if measured.get('status') == 'candidate' and _number(measured.get('value_mm')):
            values.append(measured['value_mm'])
        for row in measured.get('evidence', []):
            evidence.append({'solid_id': sid, 'face_ids': list(row['face_ids']),
                             'basis': 'native_geometry', 'value': row['thickness_mm'], 'unit': 'mm'})
    adopted = group.get('thickness_mm')
    if _number(adopted):
        evidence.insert(0, _manual(adopted, 'mm'))
    if len(values) > 1 and _different(min(values), max(values)):
        return 'conflict', '本组实体的几何板厚候选不一致，请核对分组及各件厚度。', evidence
    if _number(adopted) and any(_different(adopted, value) for value in values):
        return 'conflict', '已填写板厚与原生几何候选有差异；保留两侧数值，需工程核对。', evidence
    if _number(adopted):
        return 'recorded', '板厚已填写；分组确认不等于板厚已通过工程审核。', evidence
    if found and all(value and value.get('status') == 'not_applicable' for value in found):
        return 'not_evaluated', '已有几何结果标记板厚不适用，请结合零件类别核对。', evidence
    if values:
        message = ('已有一致几何板厚候选，尚未采用，需工程核对。' if len(values) == len(found)
                   else '部分实体有板厚候选，其余依据不足；不能共用一个厚度。')
        return 'pending_review', message, evidence
    return 'missing', '当前保存结果未得到可采用的板厚；未识别不代表零件没有板厚。', evidence


def _quantity(group, candidates):
    candidate = candidates.get(group['id'], {})
    evidence = []
    if candidate.get('status') == 'candidate':
        evidence = [{'basis': 'assembly_instance', 'value': candidate['value'],
                     'unit': 'piece/set', 'raw_text': candidate.get('reason', '')}]
        ids = candidate.get('solid_ids', [])
        for index, path in enumerate(candidate.get('occurrence_ids', [])):
            row = {'basis': 'assembly_instance', 'raw_text': path}
            if index < len(ids):
                row['solid_id'] = ids[index]
            evidence.append(row)
    adopted = group.get('quantity_per_set')
    if _number(adopted):
        evidence.insert(0, _manual(adopted, 'piece/set'))
        if candidate.get('status') == 'candidate' and candidate.get('value') != adopted:
            return 'conflict', '已填写每套数量与装配实例候选不同；需核对制造分组和数量口径。', evidence
        return 'recorded', '每套数量已填写；它不是订单总件数，仍须核对装配与订购口径。', evidence
    if candidate.get('status') == 'candidate':
        return 'pending_review', candidate['reason'], evidence
    return 'missing', '尚无明确每套数量；不能以节点、网格或混合实体个数代替制造用量。', evidence


def project(state):
    """Return checks based solely on this saved snapshot; leave the input intact."""
    items = []

    def add(group_id, field, label, status, message, evidence=None):
        evidence = evidence or []
        items.append({'id': (group_id or 'document') + ':' + field, 'group_id': group_id,
                      'field': field, 'label': label, 'status': status, 'message': message,
                      'evidence': copy.deepcopy(evidence[:32]), 'evidence_count': len(evidence)})

    groups = state.get('groups', [])
    has_saved = bool(groups or state.get('geometry_url'))
    report_status = ('current' if state.get('status') == 'ready' and has_saved
                     else 'incomplete' if has_saved or state.get('status') not in (None, 'not_started')
                     else 'not_started')
    if has_saved:
        if report_status != 'current':
            add(None, 'snapshot', '识别完成范围', 'not_evaluated',
                '本轮仍在处理或未完成，以下仅检查已保存结果，不能视作完整审图。')
        sets = state.get('bom_settings', {}).get('order_sets')
        add(None, 'order_sets', '订单套数', 'recorded' if _number(sets) else 'missing',
            '套数已填写；与每套数量分别核对后才能形成订单总数。' if _number(sets)
            else '当前未填写订单套数，不能把装配数量或附件数量当作订单数量。',
            [_manual(sets, 'set')] if _number(sets) else [])
        add(None, 'drawing_requirements', '图纸技术要求', 'not_evaluated',
            '本清单仅检查当前保存的 CAD 分组与几何证据；未审查图纸文字、公差、焊接和表面处理要求。')
    for group in groups:
        gid, classification = group['id'], group.get('classification')
        if classification == 'exclude':
            add(gid, 'classification', '制造类别', 'not_evaluated',
                '本组已排除，不进入本轮物料与加工草稿检查。', [{'basis': 'saved_record', 'value': classification}])
            continue
        assigned = classification in ('make', 'buy', 'assembly')
        add(gid, 'classification', '制造类别', 'recorded' if assigned else 'pending_review',
            '制造类别已填写，分组确认不代表工艺或生产放行。' if assigned
            else '本组未明确制造类别或仍需设计，请先核对制造边界。', [{'basis': 'saved_record', 'value': classification}])
        for field, label, builder, source in (
            ('material', '材料', _material, state.get('material_measurements', {})),
            ('thickness_mm', '板厚', _thickness, state.get('thickness_measurements', {})),
            ('quantity_per_set', '每套数量', _quantity, state.get('quantity_candidates', {})),
        ):
            status, message, evidence = builder(group, source)
            add(gid, field, label, status, message, evidence)
        # Purchased and assembly groups may not need fabrication drawings. No
        # successful native feature proves the intended manufacturing route.
        if classification in ('buy', 'assembly'):
            continue
        laser_profiles = [state.get('laser_profiles', {}).get(sid) for sid in group['solid_ids']]
        laser_evidence = []
        for sid, profile in zip(group['solid_ids'], laser_profiles):
            if profile:
                row = {'solid_id': sid, 'basis': 'native_geometry', 'raw_text': profile['reason']}
                if profile.get('source_face_id'):
                    row['face_ids'] = [profile['source_face_id']]
                laser_evidence.append(row)
        laser_ready = bool(laser_profiles) and all(row and row.get('status') == 'ready' for row in laser_profiles)
        add(gid, 'laser_profile', '激光切割草稿', 'pending_review' if laser_ready else 'not_evaluated',
            '已有平板轮廓草稿，尚需工程核对；未包含排版、切缝补偿或机床程序。' if laser_ready
            else '未形成覆盖本组全部实体的可用平板轮廓；不支持或未计算不等于无需切割。', laser_evidence)
        profiles = [state.get('bend_profiles', {}).get(sid) for sid in group['solid_ids']]
        bend_evidence = []
        for sid, profile in zip(group['solid_ids'], profiles):
            if not profile:
                continue
            for bend in profile.get('bends', []):
                bend_evidence.append({'solid_id': sid, 'basis': 'native_geometry',
                                      'face_ids': list(bend['inner_face_ids']) + list(bend['outer_face_ids']),
                                      'raw_text': bend['id'] + '；成形转角，方向及基准待确认',
                                      'value': bend['rotation_angle_deg'], 'unit': 'deg'})
        has_bends = any(profile and profile.get('bends') for profile in profiles)
        add(gid, 'bend_features', '折弯几何与方向', 'pending_review' if has_bends else 'not_evaluated',
            '已有折弯几何候选；方向、基准、加工顺序仍待工程确认，不能直接作为折弯程序。' if has_bends
            else '本组没有可用折弯候选或结果未覆盖；未识别不能据此认定没有折弯。', bend_evidence)
    return {'schema_version': 1, 'status': report_status,
            'scope': '仅检查当前已保存的 CAD 制造分组与已有证据；不是完整图纸审查、参数审核或生产放行。',
            'summary': {status: sum(item['status'] == status for item in items) for status in STATUSES},
            'items': items}
