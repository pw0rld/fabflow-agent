"""Pure engineering/requirements views over a saved quotation.

Engineering metadata is deliberately separate from the immutable quantity and
price snapshot. This module does not read files, call CAD/AI or reprice a quote.
"""
from __future__ import annotations

import copy
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import html
import io
import json
import re
from urllib.parse import quote
from cad_material import is_placeholder_material_name


ENGINEERING_ARTIFACTS = {'engineering-bom.csv', 'engineering-bom.html', 'materials.html', 'processes.html'}
ENGINEERING_FIELDS = ('part_number', 'drawing_number', 'revision', 'specification', 'material_grade',
                      'thickness_mm', 'surface_treatment', 'supply_type', 'unit', 'parent_line_id')
SUPPLY_LABELS = {'make': '自制', 'buy': '外购', 'subcontract': '外协', 'customer': '客供'}
MATERIAL_LABELS = {'carbon_steel': '碳钢（Q235类）', 'spcc': '冷轧钢板（SPCC类）',
                   'ss304': '304不锈钢', 'ss316': '316类不锈钢', 'al5052': '5052铝合金',
                   'stainless_mesh': '8目不锈钢纱网（牌号未定）'}
MISSING = '—'


def _cad_name_code(source, fields, origins):
    """Expose an original CAD model-code label without adopting it as an ID."""
    if (fields['part_number'] or fields['drawing_number']
            or any(origins.get(key) == 'manual' for key in ('part_number', 'drawing_number'))):
        return None
    name = source.get('part_name')
    if (not isinstance(name, str) or not 3 <= len(name) <= 160
            or not isinstance(source.get('native_definition'), str)
            or not source['native_definition'] or not isinstance(source.get('file_id'), str)
            or source['native_definition'].startswith('@unresolved-')
            or not re.fullmatch(r'[A-Za-z0-9]+(?:[-_][A-Za-z0-9]+){2,}', name)
            or not re.search(r'[0-9]', name) or not re.search(r'[A-Za-z]', name)):
        return None
    return name


def _positive_mesh_source(text, description):
    compact = re.sub(r'\s+', '', text)
    matches = list(re.finditer(re.escape(description), compact))
    if not matches:
        return False
    for match in matches:
        before, after = compact[max(0, match.start() - 24):match.start()], compact[match.end():match.end() + 16]
        if (re.search(r'(?:禁止|禁用|不得|不可|不允许|不能|不应|勿|不要|无需|不需要|不需|无须|取消|去掉|移除|不采用|不使用|不推荐|不选用)[^。；;，,]{0,12}$', before)
                or re.search(r'(?:DONOT|MUSTNOT)[A-Z]{0,10}$', before.upper())
                or re.match(r'[（(]?(?:禁止|禁用|不得|不可|不允许|不采用|取消|去掉|移除)', after)):
            return False
    return True


def _material_description(row):
    """Retain a literal mesh-material description; never turn it into a grade."""
    if (row.get('material_grade') is not None
            or row.get('field_sources', {}).get('material_grade') in {'manual', 'conflict'}):
        return None, None
    field = row.get('recognition', {}).get('fields', {}).get('specification', {})
    if field.get('status') != 'candidate' or field.get('source') not in {'ai_document', 'native_and_drawing'}:
        return None, None
    value = field.get('value')
    if not isinstance(value, str):
        return None, None
    declaration = value.strip()
    if field['source'] == 'native_and_drawing':
        pieces = declaration.split('；图纸规格：', 1)
        if len(pieces) != 2:
            return None, None
        declaration = pieces[1].lstrip()
    match = re.match(r'\d{1,3}\s*目\s*不锈钢(?:纱网|丝网)', declaration)
    if not match:
        return None, None
    description = re.sub(r'\s+', '', match.group())
    if not _positive_mesh_source(declaration, description):
        return None, None
    evidence = field.get('evidence')
    if isinstance(evidence, dict):
        evidence = evidence.get('drawing')
    if not isinstance(evidence, list):
        return None, None
    if any(isinstance(item, dict) and isinstance(item.get('quote'), str)
           and description in re.sub(r'\s+', '', item['quote'])
           and not _positive_mesh_source(item['quote'], description) for item in evidence):
        return None, None
    supported = [item for item in evidence if isinstance(item, dict)
                 and item.get('basis') == 'ai_document' and item.get('status') == 'pending_review'
                 and isinstance(item.get('reference'), str) and bool(item['reference'])
                 and type(item.get('page')) is int and item['page'] > 0
                 and isinstance(item.get('region'), str) and bool(item['region'].strip())
                 and isinstance(item.get('quote'), str)
                 and _positive_mesh_source(item['quote'], description)]
    return (description, {'basis': 'ai_document_material_description', 'status': 'pending_review',
                         'source_text': description, 'references': copy.deepcopy(supported)}) if supported else (None, None)


DOCUMENT_TERMS = re.compile(r'焊接|焊缝|焊点|(?:不[能得]?|无)(?:渗|泄)?漏|防腐|密封|防水|防油|气密|水密|WELD|SEAM|LEAK|SEAL|CORROSION', re.I)


def _document_requirements(row, registered_sources):
    """Retain only line-bound AI quotations from registered drawing references."""
    notes, seen = [], set()
    for field in row.get('recognition', {}).get('fields', {}).values():
        if field.get('source') not in {'ai_document', 'native_and_drawing', 'multiple_sources'} or field.get('status') not in {'candidate', 'conflict'}:
            continue
        evidence = field.get('evidence')
        if isinstance(evidence, dict):
            evidence = evidence.get('drawing')
        for item in evidence if isinstance(evidence, list) else []:
            if not isinstance(item, dict):
                continue
            text, reference, page, region = (item.get(key) for key in ('quote', 'reference', 'page', 'region'))
            source = registered_sources.get(reference) if isinstance(reference, str) else None
            if (not isinstance(source, dict) or source.get('kind') not in {'image', 'pdf'}
                    or source.get('status') not in {'reference', 'ready'}
                    or not isinstance(source.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', source['sha256'])
                    or item.get('basis') != 'ai_document' or item.get('status') != 'pending_review'
                    or type(page) is not int or page <= 0 or not isinstance(region, str) or not region.strip()
                    or not isinstance(text, str) or not text.strip() or len(text) > 3000 or not DOCUMENT_TERMS.search(text)):
                continue
            identity = (reference, page, text)
            if identity in seen:
                continue
            seen.add(identity)
            notes.append({'text': text, 'reference': reference, 'page': page, 'region': region, 'status': 'pending_review'})
    return notes


def _document_note_needs_review(text):
    """Positive process/quality constraints need costing review, not invented fees."""
    for clause in re.split(r'[。；;，,\n/]|但是|但需|但要|而是', text):
        if re.search(r'(?:不能|不得|不|无)(?:渗|泄)?漏|LEAK.?PROOF|NO\s+LEAK|MUST\s+NOT\s+LEAK|WATERTIGHT', clause, re.I):
            return True
        for match in DOCUMENT_TERMS.finditer(clause):
            if _positive_mesh_source(clause, match.group()):
                return True
    return False


def _scalar(value):
    return value if type(value) in (str, int, float) else None


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and 0 <= number < Decimal('1e24') else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _text(value):
    number = _number(value)
    if number is None:
        return None
    return format(number, 'f')


def _sum(values):
    numbers = [_number(value) for value in values]
    return None if not numbers or any(value is None for value in numbers) else format(sum(numbers, Decimal(0)), 'f')


def _sum_count(values):
    numbers = [_number(value) for value in values]
    if not numbers or any(value is None or value != value.to_integral_value() for value in numbers):
        return None
    return int(sum(numbers, Decimal(0)))


def _same(values):
    return copy.deepcopy(values[0]) if values and all(value == values[0] for value in values) else None


def _count(value):
    number = _number(value)
    return '待核对' if number is None or number != number.to_integral_value() else format(number, ',.0f')


def _weight(value):
    number = _number(value)
    if number is None:
        return '待估算'
    with localcontext() as context:
        context.prec = 64
        return '<0.01' if 0 < number < Decimal('.01') else format(number.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), ',.2f')


def _operations(line):
    source = next((line[key] for key in ('operations', 'process_route', 'processes')
                   if isinstance(line.get(key), list)), [])
    rows = []
    for item in source:
        name = item if isinstance(item, str) else item.get('operation', item.get('name')) if isinstance(item, dict) else None
        if not isinstance(name, str) or not name.strip():
            continue
        rows.append({'sequence': len(rows) + 1, 'operation': name,
                     'notes': item.get('notes') if isinstance(item, dict) and isinstance(item.get('notes'), str) else None,
                     'status': 'pending_review'})
    return rows


def _priced_requirement(result, source, requirement):
    """Only a matching, conserved cost record can resolve a read requirement."""
    snapshot = result.get('engineering_repricing')
    if (not isinstance(snapshot, dict) or snapshot.get('version') != 'engineering-demo-v1'
            or snapshot.get('after_totals') != result.get('totals') or source.get('priced') is not True):
        return None
    allowed = {'material_grade': {'adopt_material'}, 'material_description': {'purchase_mesh'},
               'document_note': {'pipe_weld_seal', 'mesh_weld'}, 'surface_treatment': {'hotdip'}, 'operations': {'hotdip'}}
    cost = next((item for item in snapshot.get('line_costs', []) if isinstance(item, dict)
                 and item.get('line_id') == requirement['line_id']), None)
    if not cost or any(_number(cost.get(key)) is None or _number(cost.get(key)) != _number(source.get(key))
                       for key in ('material_cost', 'process_cost', 'purchase_cost', 'setup_cost',
                                   'total_cost', 'total_price', 'unit_cost', 'unit_price')):
        return None
    for applied in snapshot.get('applied_requirements', []):
        if (not isinstance(applied, dict) or applied.get('status') != 'priced'
                or any(applied.get(key) != requirement[key] for key in ('line_id', 'field', 'requirement'))
                or applied.get('method') not in allowed.get(requirement['field'], set())):
            continue
        evidence = applied.get('cost_evidence')
        if (not isinstance(evidence, dict) or evidence.get('line_id') != requirement['line_id']
                or evidence.get('method') != applied['method'] or _number(evidence.get('amount')) is None
                or not evidence.get('basis') or not evidence.get('source_kind')
                or not applied.get('source_evidence')):
            continue
        component = {key: value for key, value in evidence.items() if key != 'line_id'}
        if component not in cost.get('components', []):
            continue
        if requirement['field'] in {'material_grade', 'material_description'} and applied.get('material_profile') != source.get('material_profile'):
            continue
        return copy.deepcopy(evidence)
    return None


def _requirements_review(lines, sources, settings, result):
    """Compare observed requirements with a saved price basis, without repricing."""
    from batch_quote_costing import _grade, MARKET_VERSION
    reviewed = []
    for row in lines:
        saved = sources[row['id']]
        profile = saved.get('material_profile')
        adopted = MATERIAL_LABELS.get(profile, '未保存的材料类别')
        grade = row.get('material_grade')
        if grade:
            required_profile = _grade(grade, market=settings.get('rate_profile') == MARKET_VERSION)
            matches = required_profile is not None and required_profile == profile
            reviewed.append({'line_id': row['id'], 'part_name': row['part_name'], 'field': 'material_grade',
                'requirement': grade, 'status': 'category_matches' if matches else 'not_adopted',
                'reason': ('原报价材料类别与该牌号所属类别相符；具体采购牌号、厚度、供货规格及实际费率仍需核对。' if matches else
                           '原报价仍采用' + adopted + '，尚未按已读到的材料牌号落实核价。')})
        description = row.get('material_description')
        if description:
            matches = '不锈钢' in description and profile in {'ss304', 'ss316'}
            reviewed.append({'line_id': row['id'], 'part_name': row['part_name'], 'field': 'material_description',
                'requirement': description, 'status': 'category_matches' if matches else 'not_adopted',
                'reason': ('原报价仅在不锈钢材料大类上相符；网目、丝径、面积、供货形式和具体牌号仍需按采购规格核价。' if matches else
                           '已读到不锈钢纱网要求，原报价仍采用' + adopted + '，未按纱网采购规格落实核价；未推断 304 或 316。')})
        requirements = ([('surface_treatment', row['surface_treatment'])] if row.get('surface_treatment') else [])
        requirements.extend(('operations', step['operation']) for step in _operations(saved))
        for field, requirement in requirements:
            reviewed.append({'line_id': row['id'], 'part_name': row['part_name'], 'field': field,
                'requirement': requirement, 'status': 'not_separately_priced',
                'reason': '原报价未保存与该要求对应的独立金额或明确包含项证据；综合加工费不能证明已包含该项。'})
        reviewed_notes = {value for _, value in requirements}
        for note in row.get('document_requirements', []):
            if _document_note_needs_review(note['text']) and note['text'] not in reviewed_notes:
                reviewed.append({'line_id': row['id'], 'part_name': row['part_name'], 'field': 'document_note',
                    'requirement': note['text'], 'status': 'not_separately_priced',
                    'reason': '已读工程标注仍需核定工序、计量、工时或费率；未生成工序顺序，也未据原文直接加收费用。'})
                reviewed_notes.add(note['text'])
    for requirement in reviewed:
        proof = _priced_requirement(result, sources[requirement['line_id']], requirement)
        if proof:
            requirement.update(status='priced', cost_evidence=proof,
                reason='已计入本次新报价；' + proof['basis'] + '。' +
                       ('费率为演示假设，仍待审核。' if proof['source_kind'] == 'demo' else
                        '采用用户指定的本单价格条件，尚非供应商实报，仍待审核。' if proof['source_kind'] == 'manual' else
                        '采用已保存的行情参考价，具体采购规格与供应商实价仍待核对。'))
    return reviewed


def build_engineering_bom(result, groups, review=None, recognition=None):
    """Derive engineering, requirements and process rows without mutating inputs.

    ``review`` is a separately validated metadata record: ``{revision, header,
    lines:{original_line_id:{field:value}}}``. Parent links organize the display;
    they never multiply or recalculate the original quotation quantities.
    """
    result = result if isinstance(result, dict) else {}
    review = review if isinstance(review, dict) else {}
    submitted_header = review.get('header') if isinstance(review.get('header'), dict) else {}
    submitted_lines = review.get('lines') if isinstance(review.get('lines'), dict) else {}
    from batch_engineering_recognition import recognition_fields
    recognized = recognition_fields(result, recognition or {})
    settings = result.get('settings') if isinstance(result.get('settings'), dict) else {}
    registered_sources = {source.get('id'): source for source in result.get('source_files', []) if isinstance(source, dict)}
    header = {key: _scalar(submitted_header.get(key)) for key in
              ('product_name', 'product_number', 'bom_revision', 'basis_notes')}
    header.update(product_name=header['product_name'] or _scalar(result.get('name')),
                  base_quantity=1, order_sets=settings.get('order_sets'),
                  review_revision=review.get('revision', 0), status='pending_review',
                  quantity_basis='原报价按每份 STEP 根模型各1套；上传范围及上下级重叠待核对。',
                  quantity_effect='工程补录和父项归属不修改原报价数量、重量或金额。')
    lines = []
    for index, source in enumerate(result.get('lines', [])):
        if not isinstance(source, dict):
            continue
        line_id = source.get('id')
        submitted = submitted_lines.get(line_id) if isinstance(submitted_lines.get(line_id), dict) else {}
        fields, origins = {}, {}
        for field in ENGINEERING_FIELDS:
            value = submitted[field] if field in submitted else source.get(field)
            value = _scalar(value)
            fields[field] = value if value != '' else None
            origins[field] = 'manual' if field in submitted else 'source' if fields[field] is not None else None
            candidate = recognized.get(line_id, {}).get('fields', {}).get(field, {})
            if field not in submitted and candidate.get('status') == 'conflict':
                fields[field], origins[field] = None, 'conflict'
            elif field not in submitted and fields[field] is None and candidate.get('status') == 'candidate':
                fields[field], origins[field] = candidate.get('value'), candidate.get('source')
        if origins.get('material_grade') != 'manual' and is_placeholder_material_name(fields.get('material_grade')):
            # Old immutable runs may contain a template name as their former
            # grade candidate. Correct the view while retaining source bytes.
            fields['material_grade'], origins['material_grade'] = None, 'unresolved'
        # Existing saved line quantities are exclusively piece counts. A user
        # cannot turn those counts into metres/kg through a display override.
        fields['unit'] = 'piece'
        origins['unit'] = 'quote_count'
        row = {**fields, 'id': line_id, 'line_id': line_id, 'row_number': index + 1,
               'part_name': _scalar(source.get('part_name')), 'document': _scalar(source.get('document')),
               'file_id': _scalar(source.get('file_id')), 'native_definition': _scalar(source.get('native_definition')),
               'cad_material_raw': _scalar(source.get('material_raw')), 'material_raw': _scalar(source.get('material_raw')),
               'supply_type_candidate': source.get('mode') if source.get('mode') in {'make', 'buy'} else None,
               'quantity_per_set': source.get('quantity_per_set'), 'quantity_total': source.get('quantity_total'),
               'estimated_net_weight_kg': _scalar(source.get('estimated_net_weight_kg')),
               'estimated_total_net_weight_kg': _scalar(source.get('estimated_total_net_weight_kg')),
               'field_sources': origins, 'recognition': recognized.get(line_id, {}), 'status': 'pending_review', 'level': None,
               'structure_status': 'pending_link'}
        row['cad_name_code'] = _cad_name_code(source, fields, origins)
        row['cad_name_code_evidence'] = ({'basis': 'cad_definition_name', 'status': 'pending_review',
            'source_text': row['cad_name_code'], 'file_id': row['file_id'],
            'native_definition': row['native_definition'], 'document': row['document']}
            if row['cad_name_code'] else None)
        row['material_description'], row['material_description_evidence'] = _material_description(row)
        row['document_requirements'] = _document_requirements(row, registered_sources)
        required = ('specification',)
        row['missing_fields'] = [field for field in required if row[field] is None]
        if not row['part_number'] and not row['drawing_number']:
            row['missing_fields'].append('part_identity')
        if row['supply_type'] not in {'buy', 'customer'} and row['supply_type_candidate'] != 'buy':
            row['missing_fields'].extend(field for field in ('material_grade', 'thickness_mm', 'surface_treatment') if row[field] is None)
        lines.append(row)
    by_id = {row['id']: row for row in lines}
    parents = {row['id']: row['parent_line_id'] for row in lines}
    for row in lines:
        parent, seen, level = row['parent_line_id'], {row['id']}, 0
        if parent is None:
            continue
        valid = True
        while parent is not None:
            if parent not in by_id or parent in seen:
                valid = False
                break
            seen.add(parent)
            level += 1
            parent = parents[parent]
        if valid:
            row['level'], row['structure_status'] = level, 'manual_link' if row['field_sources']['parent_line_id'] == 'manual' else 'source_link'
        else:
            row['parent_line_id'] = None
            row['structure_status'] = 'invalid_link'
    source_lines = {row['id']: row for row in result.get('lines', []) if isinstance(row, dict) and row.get('id') in by_id}
    materials = []
    with localcontext() as context:
        context.prec = 64
        for group in groups if isinstance(groups, list) else []:
            members = [member for member in group.get('member_ids', [group.get('id')]) if member in by_id]
            # A later manual engineering difference cannot remain silently
            # hidden inside a previously merged quotation display group.
            partitions = {}
            for member in members:
                engineering = by_id[member]
                signature = tuple(None if key == 'specification' and engineering['field_sources'].get(key) == 'native_geometry'
                                  else engineering.get(key) for key in ENGINEERING_FIELDS if key != 'parent_line_id') + (
                                      engineering.get('material_description'), engineering['field_sources'].get('material_grade') == 'manual')
                partitions.setdefault(signature, []).append(member)
            for member_ids in partitions.values():
                engineering_rows = [by_id[member] for member in member_ids]
                originals = [source_lines[member] for member in member_ids]
                first = engineering_rows[0]
                usages = [_number(row.get('material_usage_kg')) for row in originals]
                quantities = [_number(row.get('quantity_total')) for row in originals]
                complete = all(value is not None for value in usages + quantities)
                usage_total = format(sum((usage * quantity for usage, quantity in zip(usages, quantities)), Decimal(0)), 'f') if complete else None
                profile = _same([row.get('material_profile') for row in originals])
                materials.append({key: copy.deepcopy(first[key]) for key in
                    ('id', 'line_id', 'part_name', 'part_number', 'drawing_number', 'revision', 'specification',
                     'material_grade', 'material_description', 'material_description_evidence', 'cad_material_raw', 'material_raw', 'thickness_mm', 'surface_treatment',
                     'supply_type', 'supply_type_candidate', 'unit')})
                materials[-1].update(member_ids=member_ids, source_documents=list(dict.fromkeys(row['document'] for row in engineering_rows)),
                    specification=_same([row['specification'] for row in engineering_rows]),
                    quantity_per_set=_sum_count(row['quantity_per_set'] for row in engineering_rows),
                    quantity_total=_sum_count(row['quantity_total'] for row in engineering_rows),
                    estimated_net_weight_kg=_same([row['estimated_net_weight_kg'] for row in engineering_rows]),
                    estimated_total_net_weight_kg=_sum(row['estimated_total_net_weight_kg'] for row in engineering_rows),
                    field_sources=copy.deepcopy(first['field_sources']), adopted_material=MATERIAL_LABELS.get(profile),
                    material_basis='估价/估重采用类别，非已确认采购牌号；人工工程补录未重算旧报价。',
                    purchase_usage_kg_per_piece=_same([_text(row.get('material_usage_kg')) for row in originals]),
                    purchase_usage_kg_total=usage_total,
                    purchase_usage_status='estimated' if complete else 'not_applicable' if first['supply_type_candidate'] == 'buy' else 'pending',
                    status='pending_review')
    processes = [{'line_id': row['id'], 'part_name': row['part_name'], 'document': row['document'],
                  'document_requirements': copy.deepcopy(row['document_requirements']),
                  'operations': _operations(source_lines[row['id']]),
                  'status': 'pending_review' if _operations(source_lines[row['id']]) else 'pending'} for row in lines]
    stats = {'lines': len(lines), 'material_groups': len(materials),
             'missing_identity_lines': sum(not row['part_number'] and not row['drawing_number'] for row in lines),
             'missing_specification_lines': sum(row['specification'] is None for row in lines),
             'unlinked_structure_lines': sum(row['parent_line_id'] is None for row in lines),
             'process_pending_lines': sum(not row['operations'] for row in processes)}
    coverage = {key: sum(row.get(key) is not None and row.get(key) != '' for row in lines)
                for key in ('specification', 'thickness_mm', 'material_grade', 'material_description', 'surface_treatment', 'cad_name_code')}
    coverage.update(lines=len(lines), formal_identity=sum(bool(row['part_number'] or row['drawing_number']) for row in lines),
                    parent_link=sum(row['parent_line_id'] is not None for row in lines),
                    buy_candidates=sum(row['supply_type_candidate'] == 'buy' for row in lines),
                    thickness_pending_make=sum((row['supply_type'] if row['field_sources'].get('supply_type') == 'manual'
                                                else row['supply_type'] or row['supply_type_candidate']) == 'make'
                        and row['thickness_mm'] is None and row['recognition'].get('fields', {}).get('thickness_mm', {}).get('status') != 'not_applicable'
                        for row in lines))
    profiles = list(dict.fromkeys(source.get('material_profile') for source in source_lines.values()))
    quote_basis = {'order_sets': settings.get('order_sets'),
                   'total_price': _scalar(result.get('totals', {}).get('total_price')),
                   'adopted_materials': [MATERIAL_LABELS[profile] for profile in profiles if profile in MATERIAL_LABELS],
                   'note': '核价与估重沿用本次已保存的材料类别及价格条件；这些采用条件不是 CAD 识别牌号。已读到的要求尚需落实到核价，原金额未自动重算。'}
    quote_basis['requirements_review'] = _requirements_review(lines, source_lines, settings, result)
    quote_basis['unresolved_requirements'] = sum(row['status'] in {'not_adopted', 'not_separately_priced'}
                                               for row in quote_basis['requirements_review'])
    pricing = result.get('engineering_repricing')
    quote_basis['engineering_repricing'] = copy.deepcopy(pricing) if isinstance(pricing, dict) else None
    if quote_basis['engineering_repricing']:
        quote_basis['engineering_repricing']['source_run_id'] = pricing.get('base_run_id')
        quote_basis['note'] = '已按已读要求生成独立的新报价版本；新增采购及工序费率为明确的演示假设，原报价保留。其余未注明材料沿用本单估价材料，仍待审核。'
    adopted_lines = {item['line_id'] for item in quote_basis['requirements_review']
                     if item['status'] == 'priced' and item['field'] in {'material_grade', 'material_description'}}
    from batch_quote_costing import material_display
    for row in lines:
        source = source_lines[row['id']]
        display = material_display(source)
        adopted = row['id'] in adopted_lines
        row['pricing_material'] = {'label': display['adopted_text'],
            'source': ('依据图纸要求核价 · ' + display['price_text'] if adopted else
                       '按本单所选材料估价 / 估重 · ' + display['price_text']),
            'is_assumption': not adopted and source.get('confidence') != 'cad_material'}
    for material in materials:
        material['pricing_material'] = copy.deepcopy(by_id[material['line_id']]['pricing_material'])
    return {'schema_version': 1, 'header': header, 'lines': lines, 'materials': materials, 'processes': processes,
            'coverage': coverage, 'quote_basis': quote_basis, 'stats': stats, 'warnings': [header['quantity_basis'], header['quantity_effect'],
                '来源文件分组不代表总成父子结构；人工父项归属不改变已有数量基准。',
                '材料行情的参考板厚、表面和牌号不作为零件工程属性；缺少的属性保持待补充。',
                '净重由原几何和采用密度估算；采购计价量沿用原报价用量假设，不能视为已完成排样的投料量。',
                '全部工程及工艺记录保持待审核，尚非可直接投产的已发布制造 BOM。']}


def _esc(value, missing=MISSING):
    return html.escape(missing if value is None or value == '' else str(value), quote=True)


def _recognized_cell(row, field):
    observed = row.get('recognition', {}).get('fields', {}).get(field, {})
    manual = row.get('field_sources', {}).get(field) == 'manual'
    value = row.get(field)
    if manual and value is None:
        return '<span class="field-empty" title="人工已清空该字段，识别候选不会自动填回。">人工清空</span>'
    if value is None and field == 'material_grade' and not manual and observed.get('status') != 'conflict':
        value = row.get('cad_material_raw')
        if value is None and row.get('material_description'):
            proof = json.dumps(row.get('material_description_evidence'), ensure_ascii=False)
            content = '<span title="' + _esc(proof, '') + '">' + _esc(row['material_description']) + '</span><small>AI 材料原文 · 牌号未标</small>'
            return content + _pricing_material_cell(row)
    label = '人工修正' if manual else observed.get('label')
    reason = observed.get('reason') or ''
    evidence = observed.get('evidence')
    if evidence:
        reason += ' ' + json.dumps(evidence, ensure_ascii=False)[:2000]
    if value is None and field == 'thickness_mm' and not manual:
        if row.get('supply_type_candidate') == 'buy':
            return '<span class="field-empty" title="外购候选按零件型号和采购规格核对，不将紧固件外形尺寸作为板厚。">外购按型号核对</span>'
        if observed.get('reason') and label:
            return '<span class="field-empty" title="' + _esc(reason, '') + '">' + _esc(label) + '</span>'
    content = '<span title="' + _esc(reason, '') + '">' + _esc(value) + '</span>'
    if value is not None and label:
        content += '<small>' + _esc(label) + '</small>'
    elif observed.get('status') in {'conflict', 'not_stated', 'unreadable', 'not_applicable'}:
        content += '<small>' + _esc(label) + '</small>'
    return content + (_pricing_material_cell(row) if field == 'material_grade' else '')


def _pricing_material_cell(row):
    material = row.get('pricing_material')
    if not isinstance(material, dict) or not material.get('label'):
        return ''
    return ('<small>本次核价材料：' + _esc(material['label'])
            + ('（估价假设）' if material.get('is_assumption') else '')
            + '</small><small>' + _esc(material.get('source')) + '</small>')


def _identity_cell(row):
    if row.get('part_number') or row.get('drawing_number'):
        return ((_esc(row['part_number']) if row.get('part_number') else '')
                + ('<small>图号：' + _esc(row['drawing_number']) + '</small>' if row.get('drawing_number') else ''))
    if row.get('cad_name_code'):
        return '<span>' + _esc(row['cad_name_code']) + '</span><small>CAD名称编码</small>'
    return MISSING


def _name_cell(row, compact_identity):
    if compact_identity and row.get('cad_name_code'):
        return '<span>' + _esc(row['cad_name_code']) + '</span><small>CAD名称编码</small>'
    return _esc(row['part_name'])


def _supply_cell(row):
    adopted = SUPPLY_LABELS.get(row.get('supply_type'))
    if adopted:
        return _esc(adopted)
    if row.get('field_sources', {}).get('supply_type') == 'manual':
        return '人工清空'
    candidate = SUPPLY_LABELS.get(row.get('supply_type_candidate'))
    return _esc(candidate) + '<small>名称规则候选</small>' if candidate else MISSING


def _coverage_summary(manifest):
    values = manifest['coverage']
    fields = (('尺寸 / 规格', 'specification'), ('板厚 / 壁厚', 'thickness_mm'),
              ('材料牌号', 'material_grade'), ('材料原文描述', 'material_description'), ('表面处理', 'surface_treatment'))
    cards = ''.join('<div><dt>' + label + '</dt><dd>' + str(values[key]) + '<small> / ' + str(values['lines']) + ' 行</small></dd></div>'
                    for label, key in fields)
    missing = ('材料牌号 ' + str(values['lines'] - values['material_grade']) + ' 行；表面处理 '
               + str(values['lines'] - values['surface_treatment']) + ' 行；自制候选的板/壁厚 '
               + str(values['thickness_pending_make']) + ' 行。')
    basis = manifest['quote_basis']
    adopted = '、'.join(basis['adopted_materials'])
    statuses = {'not_adopted': '尚未按要求核价', 'category_matches': '材料类别相符，采购条件待核对',
                'not_separately_priced': '未单列核价，尚未证实包含', 'priced': '已计入本次新报价，待审核'}
    requirements = ''.join('<li data-requirement-line="' + _esc(row['line_id']) + '" data-requirement-field="' + _esc(row['field'])
        + '"><strong>' + _esc(row['line_id']) + ' · ' + _esc(row['part_name']) + '：' + _esc(row['requirement'])
        + '</strong><small>' + _esc(statuses[row['status']]) + '。' + _esc(row['reason']) + '</small></li>'
        for row in basis['requirements_review'])
    requirement_review = ('<p><strong>尚需落实到核价：' + str(basis['unresolved_requirements']) + ' 项</strong></p>'
        + ('<details><summary>查看已读要求与核价状态（' + str(len(basis['requirements_review'])) + ' 项）</summary><ol data-engineering-requirements>' + requirements + '</ol></details>' if requirements else
           '<p>本次工程记录尚无可与报价逐项比对的材料或表面处理要求。</p>'))
    return ('<section class="coverage" data-engineering-coverage><h2>本次资料读取情况</h2><dl class="coverage-grid">' + cards
            + '</dl><p><strong>待核对汇总：</strong>' + missing + '</p><p>以上为有依据的显示值数量，仍待工程核对；外购候选 '
            + str(values['buy_candidates']) + ' 行按型号核对。材料原文描述不等于具体牌号。</p></section>'
            + '<section class="quote-basis" data-engineering-quote-basis><h2>核价采用条件</h2>'
            + ('<p>已保存材料类别：' + _esc(adopted) + '</p>' if adopted else '<p>本次记录未保存可展示的材料采用类别。</p>')
            + '<p>' + _esc(basis['note']) + '</p>' + requirement_review + '</section>')


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, allow_nan=False)
    value = str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r')) else value


def _csv(manifest):
    columns = [('行号', 'id'), ('物料编码', 'part_number'), ('图号', 'drawing_number'), ('CAD名称编码候选', 'cad_name_code'), ('名称', 'part_name'),
        ('版本', 'revision'), ('规格', 'specification'), ('工程材料牌号', 'material_grade'), ('材料原文描述', 'material_description'),
        ('材料原文描述依据', 'material_description_evidence'), ('CAD材料原文', 'cad_material_raw'),
        ('板厚_mm', 'thickness_mm'), ('表面处理', 'surface_treatment'), ('供应方式', 'supply_type'), ('单位', 'unit'),
        ('原报价单套用量', 'quantity_per_set'), ('原报价订单数量', 'quantity_total'), ('父项行号', 'parent_line_id'),
        ('结构状态', 'structure_status'), ('单件预估净重_kg', 'estimated_net_weight_kg'),
        ('订单预估净重_kg', 'estimated_total_net_weight_kg'), ('来源文件', 'document'),
        ('原生定义', 'native_definition'), ('工程状态', 'status')]
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\r\n')
    writer.writerow([name for name, _ in columns])
    for row in manifest['lines']:
        writer.writerow([_cell('件' if key == 'unit' else row.get(key)) for _, key in columns])
    return ('\ufeff' + stream.getvalue()).encode('utf-8')


def _preview(row, previews):
    entry = previews.get(row['id'])
    target = entry.get('geometry_url') if isinstance(entry, dict) else None
    if (isinstance(target, str) and not any(token in target for token in ('\\', '..', '//', '#', '?'))
            and target.startswith(('/workbench/api/quote-batches/', '/ortho2cad-test/customer-cases/',
                                   '/ortho2cad-test/quote-workbench/api/quote-batches/'))):
        return '<a href="/workbench/quotation/part/?preview=' + quote(target, safe='') + '" target="_blank" rel="noopener noreferrer">查看零件</a>'
    return ''


def render_engineering_artifact(result, filename, groups, part_previews=None, review=None, recognition=None):
    """Render fixed same-directory artifacts; originals are never rewritten."""
    if filename not in ENGINEERING_ARTIFACTS:
        raise ValueError('不支持的工程资料。')
    manifest = build_engineering_bom(result, groups, review, recognition)
    if filename.endswith('.csv'):
        return _csv(manifest), 'text/csv; charset=utf-8'
    header = manifest['header']
    previews = {row.get('line_id'): row for row in part_previews or [] if isinstance(row, dict)}
    titles = {'engineering-bom.html': '工程 BOM', 'materials.html': '备料汇总', 'processes.html': '工艺明细'}
    title = titles[filename]
    back = 'quote-visual.html' if any(str(row.get('geometry_url', '')).startswith('/ortho2cad-test/') for row in previews.values()) else 'quote.html'
    nav = ''.join('<a href="' + name + '"' + (' aria-current="page"' if name == filename else '') + '>' + label + '</a>' for name, label in titles.items())
    nav += '<a href="' + back + '">成本与报价</a>'
    product_identity = '<div><dt>产品编号</dt><dd>' + _esc(header['product_number']) + '</dd></div>' if header['product_number'] else ''
    heading = ('<header><p class="eyebrow">ENGINEERING · 待审核</p><h1>' + title + '</h1><p class="product">' + _esc(header['product_name'])
               + '</p><dl class="identity">' + product_identity + '<div><dt>数量基准</dt><dd>每份 STEP 根模型各1套 · 原报价 ' + _esc(_count(header['order_sets']))
               + ' 套</dd></div><div><dt>工程状态</dt><dd>待工程审核</dd></div></dl><nav>' + nav + '</nav></header>')
    basis = '<section class="basis"><strong>数量及资料口径</strong><p>' + _esc(header['quantity_basis']) + '</p><p>' + _esc(header['quantity_effect'])
    if header['basis_notes']:
        basis += '</p><p>人工补充：' + _esc(header['basis_notes'])
    basis += '</p></section>'
    if filename == 'engineering-bom.html':
        formal_identity = manifest['coverage']['formal_identity'] > 0
        parent_links = manifest['coverage']['parent_link'] > 0
        columns = (['行号 / 来源'] + (['物料编码 / 图号'] if formal_identity else [])
                   + ['零件 / 名称', '材料', '成品尺寸 / 规格', '板厚 / 壁厚（mm）', '表面处理', '供应方式', '单位', '单套用量', '订单数量', '单件估重（kg）']
                   + (['父项归属'] if parent_links else []))
        rows = []
        for row in manifest['lines']:
            cells = (['<strong>' + _esc(row['id']) + '</strong><small>' + _esc(row['document']) + '</small>' + _preview(row, previews)]
                + ([_identity_cell(row)] if formal_identity else [])
                + [_name_cell(row, not formal_identity), _recognized_cell(row, 'material_grade'), _recognized_cell(row, 'specification'),
                _recognized_cell(row, 'thickness_mm'), _recognized_cell(row, 'surface_treatment'),
                _supply_cell(row), '件', _count(row['quantity_per_set']), _count(row['quantity_total']), _esc(_weight(row['estimated_net_weight_kg']))]
                + ([_esc(row['parent_line_id'], '待关联') + ('<small>人工归属 · 未重算数量</small>' if row['parent_line_id'] else '')] if parent_links else []))
            rows.append('<tr data-engineering-row="' + _esc(row['id']) + '">' + ''.join('<td>' + cell + '</td>' for cell in cells) + '</tr>')
        intro = _coverage_summary(manifest) + '<p>' + str(len(manifest['lines'])) + ' 条来源物料。尺寸与板厚读取 CAD 量测，材料和表面处理结合 CAD 属性及 AI 读取的图纸原文，提取值标明来源。</p><p>材料、表面处理的缺项保留在本表并集中汇总；未提供的字段显示 —。识别结果仍待工程核对。</p>'
    elif filename == 'materials.html':
        columns = ('行号 / 物料', '规格 / 材料', '来源', '单位', '订单数量', '订单预估净重（kg）', '采购计价量估计（kg）', '用量口径')
        rows = []
        for row in manifest['materials']:
            usage = _weight(row['purchase_usage_kg_total']) if row['purchase_usage_kg_total'] is not None else '外购按件核对' if row['purchase_usage_status'] == 'not_applicable' else MISSING
            cells = (_esc(row['id']) + '<strong>' + _esc(row['part_name']) + '</strong>',
                _esc(row['specification']) + '<small>工程牌号：' + _esc(row['material_grade']) + '</small>'
                + ('<small>材料原文描述：' + _esc(row['material_description']) + '（牌号未标）</small>' if row['material_description'] else '')
                + '<small>核价采用类别：' + _esc(row['adopted_material']) + '（非识别牌号）</small>',
                '<small>' + '<br>'.join(_esc(name) for name in row['source_documents']) + '</small>', '件', _count(row['quantity_total']),
                _esc(_weight(row['estimated_total_net_weight_kg'])), _esc(usage),
                '沿用原报价用量假设，待排样/采购核对' if row['purchase_usage_status'] == 'estimated' else '按采购规格及订单件数复核')
            rows.append('<tr data-material-row="' + _esc(row['id']) + '">' + ''.join('<td>' + cell + '</td>' for cell in cells) + '</tr>')
        intro = '<p>' + str(len(manifest['materials'])) + ' 项备料需求。只在原报价允许汇总且工程属性一致时合并，来源保留。</p><p>净重和采购计价量分别列示。采购计价量仍含原报价用量假设，尚非排样后的实际领料量；单价和金额见成本与报价。</p>'
    else:
        columns = ('行号', '零件', '来源文件', '已保存工序', '工程状态')
        rows = []
        for row in manifest['processes']:
            operations = '<ol>' + ''.join('<li>' + _esc(step['operation']) + ('<small>' + _esc(step['notes']) + '</small>' if step['notes'] else '') + '</li>' for step in row['operations']) + '</ol>' if row['operations'] else '待工程师补充工序路线'
            if row['document_requirements']:
                operations += ('<div class="document-notes"><strong>原图工程标注 · 待核对</strong><ul>'
                    + ''.join('<li>' + _esc(note['text']) + '<small>' + _esc(note['reference']) + ' · 第 ' + str(note['page'])
                              + ' 页 · ' + _esc(note['region']) + '</small></li>' for note in row['document_requirements'])
                    + '</ul><small>保留原文，不代表已形成工序顺序、工时或计价量。</small></div>')
            rows.append('<tr data-process-row="' + _esc(row['line_id']) + '"><td>' + _esc(row['line_id']) + '</td><td>' + _esc(row['part_name']) + '</td><td>' + _esc(row['document']) + '</td><td>' + operations + '</td><td>' + ('待审核' if row['operations'] else '待补充') + '</td></tr>')
        intro = '<p>仅列出原记录中明确保存的工序。综合加工演示费不能作为激光、折弯、钻孔、焊接或打磨工序已识别的依据。</p><p>工序顺序、机台、工时、工装与制造要求待工程师补充核对。</p>'
    table = '<div class="table"><table><thead><tr>' + ''.join('<th>' + value + '</th>' for value in columns) + '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
    toolbar = '<div class="toolbar"><a href="' + back + '">返回报价页</a><span>工程资料 · 可使用浏览器打印</span><a href="engineering-bom.csv" download>下载工程 BOM CSV</a></div>'
    style = ('*{box-sizing:border-box}body{margin:0;background:#f2f6f8;color:#243b4b;font:14px/1.6 system-ui,sans-serif}.toolbar{display:flex;justify-content:space-between;gap:16px;padding:15px 24px;background:#fff;border-bottom:1px solid #dce6ed}a{color:#176b89;text-decoration:none}main{max-width:1600px;margin:auto;padding:24px}header,section{background:#fff;border:1px solid #dce6ed;border-radius:10px;padding:22px;margin-bottom:16px}h1{margin:4px 0;font-size:27px}.eyebrow{font-size:11px;letter-spacing:.08em;color:#a46d22}.product{font-weight:600}.identity{display:flex;gap:18px 35px;flex-wrap:wrap}.identity div{min-width:130px}dt{font-size:11px;color:#7b8e9b}dd{margin:2px 0}nav{display:flex;gap:8px;flex-wrap:wrap;padding-top:15px;border-top:1px solid #e1e9ee}nav a{padding:6px 13px;border:1px solid #cadde8;border-radius:6px}nav a[aria-current]{background:#eaf4fa;font-weight:600}.basis{font-size:12px;color:#627b8b}.table{overflow:auto;background:#fff;border:1px solid #dce6ed;border-radius:8px}table{border-collapse:collapse;width:100%;min-width:1000px;font-size:12px}th,td{padding:11px 10px;vertical-align:top;border-bottom:1px solid #e2e9ee;text-align:left;max-width:250px;overflow-wrap:anywhere}th{background:#edf4f8;color:#365e74}td strong,td small{display:block}td small{margin-top:5px;font-size:11px;color:#758c9a}ol{padding-left:18px;margin:0}.footer{color:#7b8e9b;font-size:12px;margin-top:18px}@media(max-width:640px){main{padding:12px}.toolbar{font-size:12px;flex-wrap:wrap}header,section{padding:16px}}@page{size:A3 landscape;margin:10mm}@media print{body{background:#fff;font-size:10pt}main{max-width:none;margin:0;padding:0}.toolbar,nav{display:none}header,section{border:0;border-radius:0;padding:0;margin-bottom:5mm}.table{overflow:visible;border:0}table{min-width:0;font-size:8pt}th,td{padding:2mm 1.5mm;max-width:none}thead{display:table-header-group}tr{break-inside:avoid}.basis{font-size:8pt}.footer{font-size:8pt}}')
    style += ('h2{font-size:16px;margin:0 0 10px}.coverage-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin:10px 0}.coverage-grid dd{font-size:22px;font-weight:600}.coverage-grid dd small{font-size:12px;font-weight:400;color:#758c9a}.coverage p,.quote-basis p{margin:8px 0;font-size:12px;color:#627b8b}@media(max-width:640px){.coverage-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media print{.coverage-grid{grid-template-columns:repeat(5,minmax(0,1fr));gap:2mm}.coverage-grid dd{font-size:12pt}.coverage p,.quote-basis p{font-size:8pt}}')
    style += '.quote-basis li{margin:10px 0}.quote-basis li small{display:block;color:#627b8b}'
    content = ('<!doctype html><html lang="zh-CN" data-engineering-artifact="' + filename + '"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + title + '</title><style>' + style + '</style></head><body>' + toolbar + '<main>' + heading + basis + intro + table + '<p class="footer">工程资料保持待审核；来源文件分组不代表已核对的装配结构。当前视图不重新识图、估重或计价。</p></main></body></html>')
    return content.encode('utf-8'), 'text/html; charset=utf-8'
