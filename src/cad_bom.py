"""Source-bound CAD material and instance-count candidates for BOM review."""
from __future__ import annotations

import copy
import json

from prep_record import require

MATERIAL_SCOPES = {'definition', 'occurrence', 'subshape', 'inherited'}


def _text(value, limit, label, *, empty=False):
    require(isinstance(value, str) and len(value) <= limit and
            (empty or bool(value.strip())) and
            not any(ord(char) < 32 and char not in '\n\t' for char in value), label + '无效。')
    return value


def validate_material(value):
    """Keep original material names tied to actual CAD assignment labels."""
    fields = {'status', 'value', 'entries', 'reason', 'warnings'}
    require(isinstance(value, dict) and set(value) == fields, 'CAD 材料来源字段无效。')
    require(isinstance(value['status'], str) and value['status'] in
            {'candidate', 'unresolved'}, 'CAD 材料候选状态无效。')
    _text(value['reason'], 2000, 'CAD 材料说明')
    entries = value['entries']
    require(isinstance(entries, list) and len(entries) <= 128, 'CAD 材料来源过多。')
    for entry in entries:
        require(isinstance(entry, dict) and set(entry) ==
                {'name', 'description', 'material_label', 'assignment_label', 'scope'},
                'CAD 材料原文来源无效。')
        _text(entry['name'], 512, 'CAD 材料名称', empty=True)
        _text(entry['description'], 2000, 'CAD 材料描述', empty=True)
        _text(entry['material_label'], 512, 'CAD 材料标签')
        _text(entry['assignment_label'], 512, 'CAD 材料赋值标签')
        require(isinstance(entry['scope'], str) and entry['scope'] in MATERIAL_SCOPES,
                'CAD 材料赋值范围无效。')
    warnings = value['warnings']
    require(isinstance(warnings, list) and len(warnings) <= 32, 'CAD 材料提示过多。')
    for warning in warnings:
        _text(warning, 1000, 'CAD 材料提示')
    if value['status'] == 'candidate':
        _text(value['value'], 512, 'CAD 材料候选')
        require(entries and all(entry['name'].strip() == value['value'].strip() for entry in entries),
                'CAD 材料候选必须保留一致的原文名称。')
        require(any(entry['scope'] != 'inherited' for entry in entries),
                '仅有上级继承材料时不能作为本实体材料候选。')
    else:
        require(value['value'] is None, '未确定材料不能保存采用候选。')
    return value


def solid_identity(solid):
    """Validate the original definition and path, without inventing identities."""
    definition = solid.get('definition_id')
    if definition is not None:
        _text(definition, 512, 'CAD 实体定义', empty=True)
    occurrence = solid.get('occurrence_path')
    if occurrence is not None:
        require(isinstance(occurrence, list) and len(occurrence) <= 128,
                'CAD 装配实例路径无效。')
        for segment in occurrence:
            _text(segment, 512, 'CAD 装配实例路径')
    return {'definition_id': definition, 'occurrence_path': copy.deepcopy(occurrence)}


def group_material_summary(group, measurements):
    """Only unanimous explicit source material is a group candidate."""
    found = [measurements.get(sid) for sid in group['solid_ids']]
    details = []
    for sid, measured in zip(group['solid_ids'], found):
        if measured is None:
            continue
        entries = [entry['name'] + ' @ ' + entry['assignment_label'] +
                   ' → ' + entry['material_label'] for entry in measured['entries'][:8]]
        if len(measured['entries']) > 8:
            entries.append('另有 ' + str(len(measured['entries']) - 8) + ' 项来源')
        details.append(sid + ': ' + measured['reason'] +
                       (' [' + '; '.join(entries) + ']' if entries else ''))
    result = {'status': 'not_read', 'value': None, 'reason': '尚未读取材料原始属性。',
              'evidence_text': '; '.join(details)}
    if all(value is None for value in found):
        return result
    names = {value['value'].strip() for value in found
             if value is not None and value['status'] == 'candidate'}
    if len(names) > 1:
        return {**result, 'status': 'conflict', 'reason': '分组内材料原文不一致，需分别核对。'}
    if any(value is None or value['status'] != 'candidate' for value in found):
        return {**result, 'status': 'unresolved', 'reason': '分组内部分实体没有明确材料赋值，不能自动共用材料。'}
    return {**result, 'status': 'candidate', 'value': next(iter(names)),
            'reason': '本组实体的 CAD 材料原文一致，仍需核对具体牌号与适用范围。'}


def group_quantity_summary(group, identities):
    """Count distinct occurrences of one solid definition, never mesh count."""
    result = {'status': 'unresolved', 'value': None,
              'solid_ids': list(group['solid_ids']), 'occurrence_ids': [],
              'reason': '需要明确的制造件分组及完整 CAD 实例来源。'}
    if group['classification'] == 'exclude':
        return {**result, 'status': 'not_applicable', 'reason': '该分组已排除，不生成物料用量候选。'}
    if group['classification'] not in {'make', 'buy'}:
        return {**result, 'reason': '装配、待设计或未分类分组不能直接按实体个数确定每套制造用量。'}
    references = [identities.get(sid) for sid in group['solid_ids']]
    if any(not isinstance(item, dict) or not isinstance(item.get('definition_id'), str)
           or not item['definition_id'].strip() or not isinstance(item.get('occurrence_path'), list)
           or not item['occurrence_path'] or not all(isinstance(segment, str) and segment.strip()
                                                  for segment in item['occurrence_path'])
           for item in references):
        return {**result, 'reason': '部分实体缺少原生定义或装配实例路径，请补充读取物料属性。'}
    definitions = {item['definition_id'] for item in references}
    if len(definitions) != 1:
        return {**result, 'reason': '本组包含不同原生实体定义，不能把多个实体直接相加作为一种零件用量。'}
    definition = next(iter(definitions))
    paths = [tuple(item['occurrence_path']) for item in references]
    # The same native definition/path appearing under multiple solid IDs is
    # ambiguous even if those duplicate rows were placed in separate groups.
    inventory = [tuple(item['occurrence_path']) for item in identities.values()
                 if isinstance(item, dict) and item.get('definition_id') == definition
                 and isinstance(item.get('occurrence_path'), list) and item['occurrence_path']
                 and all(isinstance(segment, str) and segment.strip() for segment in item['occurrence_path'])]
    if len(set(paths)) != len(paths) or any(inventory.count(path) != 1 for path in paths):
        return {**result, 'reason': 'CAD 实例引用重复或不唯一，保留待人工核对，不重复计数。'}
    return {**result, 'status': 'candidate', 'value': len(paths),
            'occurrence_ids': [json.dumps(list(path), ensure_ascii=False, separators=(',', ':'))
                               for path in paths],
            'reason': f'同一 CAD 实体定义有 {len(paths)} 处独立装配引用；每套制造用量仍需工程核对。'}


def validate_bom_settings(value):
    require(isinstance(value, dict) and set(value) == {'order_sets'}, '物料表套数字段无效。')
    quantity = value['order_sets']
    require(quantity is None or type(quantity) is int and 0 < quantity <= 1000000,
            '物料表套数必须为正整数或留空。')
    return copy.deepcopy(value)


def total_quantity(per_set, order_sets):
    """Unknown factors stay unknown; candidate products stay separate."""
    return None if per_set is None or order_sets is None else per_set * order_sets
