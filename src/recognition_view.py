"""Read-only projection of the current drawing run, independent of pricing."""
from __future__ import annotations

import copy
from pathlib import Path

from reading_parts import MAX_PARTS, MAX_PART_OBSERVATIONS, PART_KEY, reconcile_parts
from reading_bom import project_bom
from reading_semantics import display_observations
from part_visuals import validate_view, MAX_VIEWS
from drawing_engineering_review import project as project_engineering_review


RECOGNITION_FIELDS = frozenset({
    'product_name', 'quantity', 'material_family', 'material_grade', 'drawing_units',
    'thickness', 'surface_treatment', 'process_route', 'inner_bend_radius',
    'engineering_open_items',
})
_ACTIVE = {'starting', 'running', 'unknown', 'stopping'}
_COST_WORDS = ('material_usage_per_piece', 'material_rate', 'operation_rates',
               'setup_fee', 'fee_inclusions', '材料用量', '材料单价', '工序费率', '准备费')
_ANNOTATION_LABELS = {
    'detail_note': '细节备注', 'document_precedence': '图纸适用说明',
    'drawing_no': '图号', 'hole_diameter': '孔径', 'interface_spec': '接口规格',
    'material_note_block': '材料备注', 'offset_part_dimension': '偏置件尺寸',
    'overall_dimension': '外形尺寸', 'part_listing': '零件清单原文',
    'part_overall_size': '零件外形', 'part_quantity': '零件数量标注',
    'purchaser_supplied_item': '客户提供的部件', 'revision_record': '版本记录',
    'seal_requirement': '密封要求', 'sheet_date': '图纸日期',
    'standoff_spec': '支撑柱规格', 'title_block_admin': '标题栏信息',
    'unreadable_dimension': '待辨认尺寸',
}


def project(record, run, public_run):
    """Never substitute adopted/manual values for this run's model output."""
    run = run or {}
    public_run = public_run or {}
    definitions = record['requirements']['fields']
    sources = record['sources']
    drawing_ids = {key for key, source in sources.items() if source['kind'] == 'drawing'}
    source_ids = [key for key in run.get('source_ids', []) if key in drawing_ids]
    current = bool(run) and set(run.get('source_ids', [])) == drawing_ids and bool(drawing_ids)

    def evidence(value):
        if not isinstance(value, dict):
            return None
        reference, page, region = value.get('reference'), value.get('page'), value.get('region')
        if not isinstance(reference, str) or not isinstance(region, str) or not region.strip():
            return None
        if reference == 'inquiry':
            if not record['inquiry'].strip() or page is not None or reference not in sources:
                return None
        elif (reference not in source_ids or type(page) is not int or not 1 <= page <= 10000):
            return None
        return {'reference': reference, 'page': page, 'region': region}

    cached = run.get('recognition_result')
    rows, partial, extracted = [], False, None
    if isinstance(cached, dict) and cached.get('status') == 'completed':
        extracted = cached
        rows = cached.get('candidates', [])
    elif run.get('status') in _ACTIVE and isinstance(run.get('initial_result'), dict):
        extracted = run['initial_result']
        rows = extracted.get('candidates', [])
        partial = True
    else:
        event = next((item for item in record['events']
                      if item['id'] == 'READ-' + str(run.get('id'))
                      and item['operation'] == 'record_reading'), None)
        if event is not None:
            rows = event['payload'].get('fields', [])

    candidates = []
    seen = set()
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get('name')
        if not isinstance(name, str) or name not in RECOGNITION_FIELDS or name not in definitions or name in seen or item.get('value') is None:
            continue
        position = evidence(item.get('evidence'))
        if position is None:
            continue
        seen.add(name)
        candidate = {key: copy.deepcopy(item.get(key)) for key in
                     ('name', 'value', 'unit', 'raw_value', 'raw_unit', 'reason')}
        candidate.update(label=definitions[name]['label'], status='pending_review', evidence=position)
        candidates.append(candidate)

    observations = []
    for item in public_run.get('observations', []):
        if (not isinstance(item, dict) or item.get('name') not in RECOGNITION_FIELDS
                or item['name'] not in definitions):
            continue
        position = evidence(item.get('evidence'))
        if position is not None:
            observations.append({**copy.deepcopy(item), 'evidence': position,
                                 'label': _ANNOTATION_LABELS.get(item.get('source_name'),
                                                                item.get('source_name') or definitions[item['name']]['label'])})
    # Only an explicit saved model field enables parts. No filename, legacy
    # annotation or already adopted business value can create a new part.
    parts_supported = isinstance(extracted, dict) and isinstance(extracted.get('parts'), list)

    def part_evidence(value):
        position = evidence(value)
        if position is None or position['reference'] == 'inquiry':
            return None
        source = sources[position['reference']]
        suffix = Path(source.get('original_name', source.get('path', ''))).suffix.lower()
        page_count = run.get('source_page_counts', {}).get(position['reference']) if suffix == '.pdf' else 1
        if type(page_count) is not int or not 1 <= position['page'] <= page_count:
            return None
        return position

    parts, part_keys = [], set()
    for item in (extracted.get('parts', []) if parts_supported else [])[:2 * MAX_PARTS]:
        if not isinstance(item, dict):
            continue
        key, name = item.get('key'), item.get('name')
        if (not isinstance(key, str) or not PART_KEY.fullmatch(key) or key in part_keys
                or not isinstance(name, str) or not name.strip() or len(name) > 256
                or not isinstance(item.get('evidence'), list)):
            continue
        identities = (item.get('part_number'), item.get('version'))
        if any(value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 128)
               for value in identities):
            continue
        positions = [position for value in item['evidence']
                     if (position := part_evidence(value)) is not None]
        if not positions:
            continue
        attached = {position['reference'] for position in positions}
        part_observations = []
        raw_observations = item.get('observations', [])
        if not isinstance(raw_observations, list):
            continue
        for observation in raw_observations[:2 * MAX_PART_OBSERVATIONS]:
            if (not isinstance(observation, dict) or observation.get('name') not in RECOGNITION_FIELDS
                    or observation['name'] not in definitions
                    or not isinstance(observation.get('raw_text'), str) or not observation['raw_text'].strip()
                    or observation.get('status') not in {'observed', 'unit_unknown', 'conflict', 'unreadable'}):
                continue
            position = part_evidence(observation.get('evidence'))
            if position is None or position['reference'] not in attached:
                continue
            source_name = observation.get('source_name')
            if source_name is not None and (not isinstance(source_name, str) or not source_name.strip()
                                            or len(source_name) > 128 or not source_name.isprintable()):
                continue
            label = _ANNOTATION_LABELS.get(source_name, source_name or definitions[observation['name']]['label'])
            part_observations.append({**copy.deepcopy(observation), 'evidence': position, 'label': label})
        part_keys.add(key)
        parts.append({'key': key, 'name': name, 'part_number': identities[0], 'version': identities[1],
                      'status': 'pending_review', 'evidence': positions, 'observations': part_observations})
        if isinstance(item.get('views'), list):
            allowed_pages = {(position['reference'], position['page']) for position in positions}
            located = []
            for value in item['views'][:MAX_VIEWS]:
                try:
                    checked = validate_view(value, references=attached)
                    if (checked['reference'], checked['page']) in allowed_pages:
                        located.append(checked)
                except (ValueError, KeyError, TypeError):
                    pass
            if located:
                parts[-1]['views'] = located
    observations = display_observations(observations)
    reconciliation_warning = None
    try:
        parts = reconcile_parts(parts)
    except (ValueError, TypeError, KeyError):
        # Older stored projections accepted a looser observation shape. Keep
        # their already source-filtered display instead of failing the GET.
        reconciliation_warning = '部分旧版条目的格式不满足自动合并条件，已保留原始条目，请核对重复项。'
    for part in parts:
        part['observations'] = display_observations(part['observations'])
        bom = project_bom(part)
        if bom:
            part['bom'] = bom
    warnings = list(dict.fromkeys(item for item in public_run.get('warnings', [])
                                 if isinstance(item, str) and not any(word in item for word in _COST_WORDS)))
    if reconciliation_warning:
        warnings.append(reconciliation_warning)
    questions = []
    if run.get('status') == 'completed':
        for name in ('material_grade', 'thickness', 'drawing_units'):
            if name not in seen and len(parts) <= 1:
                questions.append(definitions[name]['label'] + '尚未形成明确识别结果，请结合原图核对。')
        for item in observations:
            if item.get('status') in {'unit_unknown', 'conflict', 'unreadable'}:
                label = item['label']
                state = {'unit_unknown': '单位待核对', 'conflict': '标注存在差异', 'unreadable': '标注尚未读清'}[item['status']]
                question = label + '：' + state + '，请结合原图核对。'
                if question not in questions:
                    questions.append(question)
    result = {'run_id': run.get('id'), 'status': run.get('status', 'not_started'),
            'available': bool(candidates or observations or parts), 'partial': partial,
            'candidates': candidates, 'observations': observations,
            'parts': parts, 'parts_supported': parts_supported,
            'warnings': warnings, 'questions': questions, 'source_ids': source_ids,
            'source_current': current}
    result['engineering_review'] = project_engineering_review(result)
    return result
