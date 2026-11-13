"""Read-only coverage checks over validated drawing output, never engineering approval.

The input is recognition_view's current-run projection, not model JSON or adopted
business fields. Missing means not extracted in this run, not absent on a drawing.
"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter

from reading_bom import parse_bom_row, project_bom


STATUSES = ('recorded', 'pending_review', 'missing', 'conflict', 'unreadable', 'not_evaluated')
_CHECKS = (
    ('material', '材料牌号', {'material_grade', 'material_family'}, None),
    ('thickness', '板厚', {'thickness'}, None),
    ('drawing_units', '图纸单位', {'drawing_units'}, None),
    ('dimensions', '尺寸与孔位', set(), r'dimension|overall_size|尺寸|孔径|直径|外形|孔位|坐标|hole_diameter|hole_position|coordinates'),
    ('quantity', '数量与统计口径', {'quantity'}, r'part_quantity'),
    ('surface_treatment', '表面处理', {'surface_treatment'}, None),
    ('bend_requirement', '折弯要求', {'inner_bend_radius'}, r'bend|fold|折弯|内弯|弯曲|固定面|fixed face'),
    ('weld_requirement', '焊接要求', set(), r'weld|焊|solder'),
    ('tolerance', '公差要求', set(), r'tolerance|公差|gdt|gd&t|±|(?:\+\s*\d.*-\s*\d)'),
    ('technical_requirements', '工艺与技术要求', {'process_route', 'engineering_open_items'}, None),
)
_CORE = {'material', 'thickness', 'drawing_units', 'dimensions', 'quantity', 'surface_treatment'}
MAX_EVIDENCE = 32
_BEND_TEXT = r'\bbend\w*|\bfold\w*|折弯|内弯|弯曲|固定面|fixed\s*face'
_CHAMFER_TEXT = r'chamfer|倒角|\d+(?:\.\d+)?\s*[x×]\s*45\s*(?:°|deg\b|(?=[,;，；\-]|$))'
_TECHNICAL_TEXT = (
    _BEND_TEXT + r'|\bweld\w*|\bsolder\w*|焊|\bdeburr\w*|burrs?|sharp\s+edges?'
    r'|\bgrounding\b|\bearthing\b|\bpaint\w*|powder\s*coat|surface\s*(?:finish|treatment)'
    r'|\btolerances?\b|\bISO\s*\d|\bGD\s*&\s*T\b|公差|去毛刺|接地|喷粉|喷涂|镀锌'
    r'|打磨|抛光|钻孔|切割|铣削|工艺要求|加工要求|技术要求|倒角|relief\s*cut'
    r'|\bseal(?:ing)?\b|密封|防水|气密|防护等级'
)
_TECHNICAL_LABEL = r'technical[_ ](?:requirements|notes)|engineering[_ ]notes|process[_ ]route|技术要求|工艺要求|加工要求'
_ADMINISTRATIVE_TEXT = (
    r'(?:drawn|checked|approved)\s+by|\bdate\s*[:：]|last\s+revision|sheet\s+\d+\s+of'
    r'|signature|copyright|未经许可|不得复制|绘图人|制图人|签名|绘图日期'
)


def _text(row):
    return str(row.get('raw_text') or '')


def _labels(row):
    return ' '.join(str(row.get(key) or '') for key in ('source_name', 'label'))


def _is_chamfer_only(row):
    # A free-form label cannot turn a chamfer count into a bend instruction.
    text = _text(row)
    return bool(re.search(_CHAMFER_TEXT, text, re.I) and not re.search(_BEND_TEXT, text, re.I))


def _is_technical_requirement(row):
    if row.get('name') == 'process_route':
        return True
    text = _text(row)
    # BOM rows describe components. Keep their text with those components,
    # rather than treating every component description as a process directive.
    if parse_bom_row(row):
        return False
    if re.search(_TECHNICAL_TEXT, text, re.I) or _is_chamfer_only(row):
        return True
    if re.search(_ADMINISTRATIVE_TEXT, text, re.I):
        return False
    labels = _labels(row)
    # engineering_open_items also contains titles, signatures and page metadata.
    # Only explicit technical labels can admit an otherwise unreadable note.
    return bool(re.search(_TECHNICAL_LABEL, labels, re.I))


def _matches(row, field, names, pattern):
    if field == 'technical_requirements':
        return _is_technical_requirement(row)
    if field == 'bend_requirement' and _is_chamfer_only(row):
        return False
    if field == 'quantity' and parse_bom_row(row):
        return True
    if row.get('name') in names:
        return True
    if not pattern:
        return False
    # Semantic labels can locate a dimension; raw numeric text alone cannot.
    labels = _labels(row)
    text = labels if pattern.startswith('dimension') or pattern == 'part_quantity' else labels + ' ' + _text(row)
    return bool(re.search(pattern, text, re.I))


def _evidence(row, field=None):
    result = {**copy.deepcopy(row['evidence']), 'basis': 'drawing_text'}
    if 'raw_text' in row:
        result['raw_text'] = row['raw_text']
    else:
        raw = row.get('raw_value') if row.get('raw_value') is not None else row.get('value')
        result['raw_text'] = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        result['value'] = copy.deepcopy(row.get('value'))
    if row.get('raw_unit') or row.get('unit'):
        result['unit'] = row.get('raw_unit') or row['unit']
    if field == 'quantity':
        bom = parse_bom_row(row)
        if bom:
            result.update({name: copy.deepcopy(bom[name]) for name in
                           ('item_no', 'quantity', 'quantity_scope')})
    return result


def _review_state(row, field):
    if field == 'quantity':
        bom = parse_bom_row(row)
        if bom:
            # An unknown unit in a component description does not invalidate
            # an explicit, independently parsed QTY cell.
            return bom['status']
    return row.get('status')


def project(recognition):
    """Build bounded, source-backed checks without modifying any input or file."""
    run_status = recognition.get('status', 'not_started')
    status = ('not_started' if run_status == 'not_started' else
              'stale' if recognition.get('source_current') is False else
              'current' if run_status == 'completed' and not recognition.get('partial') else 'incomplete')
    complete = status == 'current'
    items = []
    parts = recognition.get('parts') or []
    # Global candidates never silently fill a part: a packet may contain several
    # materials, revisions and quantities, even if only one part was recognized.
    scopes = [(part['key'], part.get('observations', []), part) for part in parts]
    if not scopes:
        scopes = [(None, recognition.get('observations', []) + recognition.get('candidates', []), None)]
    else:
        def identity(row):
            return json.dumps({name: row.get(name) for name in
                               ('name', 'source_name', 'raw_text', 'evidence')},
                              sort_keys=True, ensure_ascii=False)
        assigned = {identity(row) for _, rows, _ in scopes for row in rows}
        unassigned = [row for row in recognition.get('observations', []) if identity(row) not in assigned]
        unassigned += recognition.get('candidates', [])
        if unassigned:
            # Preserve packet-wide warnings and requirements without assigning
            # shared material, quantities or units to an individual part.
            scopes.append((None, unassigned, None))
    for key, rows, part in scopes:
        bom = project_bom(part) if part is not None else None
        for field, label, names, pattern in _CHECKS:
            found = [row for row in rows if _matches(row, field, names, pattern)]
            states = {_review_state(row, field) for row in found}
            if field == 'quantity' and bom and bom['quantity_status'] == 'conflict':
                states.add('conflict')
            readable = [row for row in found if _review_state(row, field) not in ('unreadable', 'conflict')]
            if 'conflict' in states:
                item_status, message = 'conflict', '识别记录标记了差异，请逐项对照来源核对；未替你选择采用值。'
            elif 'unreadable' in states:
                if readable:
                    item_status, message = 'pending_review', '已保留可读标注；另有部分标注尚未读清，请逐项查看原文或补充清晰资料。'
                else:
                    item_status, message = 'unreadable', '本类已提取的相关标注尚未读清，请查看原图或补充清晰资料。'
            elif 'unit_unknown' in states:
                item_status, message = 'pending_review', '已保留标注原文，但单位或含义尚待核对。'
            elif found:
                item_status, message = 'pending_review', '已提取相关原文，数值、归属及工程含义仍待人工核对。'
            elif complete and field in _CORE and (part is not None or not parts):
                item_status, message = 'missing', '本轮未提取到明确标注，请查看原图；不代表原图没有该信息。'
            else:
                item_status, message = 'not_evaluated', ('尚未形成完整结果，暂不判断缺项。' if not complete else
                                                        '尚无相关识别依据，适用性及要求待工程人员判断。')
            if field == 'quantity' and found:
                if any(parse_bom_row(row) for row in found):
                    message += ' 已保留 BOM 明细行用量，数量口径为原表该行；不是订单数量，也未确认套件内实物件数。'
                else:
                    message += ' 需区分每套用量、订单数量和孔、折弯、焊点等特征数量。'
            if field == 'bend_requirement' and found:
                message += ' 图纸转录不等于已生成展开图或机床折弯参数。'
            if field == 'weld_requirement' and found:
                message += ' 焊接位置、方法及验收要求需工程核对；不自动套用焊接标准。'
            items.append({'id': (key or 'drawing') + ':' + field, 'part_key': key,
                          'field': field, 'label': label, 'status': item_status,
                          'message': message, 'evidence_count': len(found),
                          'evidence': [_evidence(row, field) for row in sorted(found, key=lambda row:
                                       {'conflict': 0, 'unreadable': 1, 'unit_unknown': 2}.get(row.get('status'), 3))[:MAX_EVIDENCE]]})
    # Same drawing number with distinct explicit versions is a review issue,
    # not permission to merge the corresponding parts or choose the newest.
    numbered = {}
    for part in parts:
        if part.get('part_number') and part.get('version'):
            numbered.setdefault(part['part_number'], []).append(part)
    for number, linked in numbered.items():
        if len({part['version'] for part in linked}) > 1:
            items.append({'id': 'revision:' + number, 'part_key': None, 'field': 'revision',
                          'label': '同图号版本差异', 'status': 'conflict',
                          'message': f'图号 {number} 存在多个版本，可能为不同用途或历史版本，请核对适用关系。',
                          'evidence': [{**copy.deepcopy(e), 'basis': 'drawing_text', 'raw_text':
                                        f"{part['part_number']} / {part['version']}"}
                                       for part in linked for e in part.get('evidence', [])[:1]][:MAX_EVIDENCE]})
    counts = Counter(row['status'] for row in items)
    return {'schema_version': 1, 'status': status,
            'scope': '仅检查本轮已验证来源的识别结果覆盖情况；未提取到不等于原图缺失，未做制造合规或生产放行。',
            'summary': {name: counts[name] for name in STATUSES}, 'items': items}
