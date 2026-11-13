"""Reprice a saved simulation against bound engineering requirements.

No files, models, CAD operations or supplier calls are performed here. A policy
can change the explicitly labelled demo rates, never a source quotation,
geometry, quantity, material grade or approval. The old result is immutable.
"""
from __future__ import annotations

from collections import defaultdict
import copy
from decimal import Decimal, ROUND_DOWN, localcontext
import re

from batch_material_prices import VERSION as MARKET_VERSION, resolve_material_price
from batch_quote_costing import AMOUNT_KEYS, PROFILES, _decimal, _grade, _money, _text, _weight, validate_settings
from prep_record import digest


VERSION = 'engineering-demo-v1'
ZERO = Decimal('0')
ENTRY_KEYS = {'line_id', 'field', 'requirement', 'method', 'unit', 'rate', 'minimum', 'source_kind', 'inclusions'}
POLICY_KEYS = {'version', 'base_digest', 'requirements_digest', 'entries'}
PIPE_NOTE = '此圆管焊接固定/不能漏水漏油/316不锈钢'
MESH_NOTE = '焊接8目不锈钢纱网'
MESH_DESCRIPTION = '8目不锈钢纱网'
ZINC_NOTES = {'热侵锌处理', '热浸锌处理', '热浸镀锌', '热镀锌'}
SNAPSHOT_KEYS = ('material_price_snapshot', 'material_price_original_rate', 'material_price_tax_included',
                 'material_price_vat_rate_percent', 'material_price_source_kind', 'material_price_source',
                 'material_price_quote_date', 'material_price_reference_id')
CHANGE_KEYS = ('material_profile', 'material_raw', 'material_requirement', 'material_rate', 'mode',
               'density_kg_m3', 'estimated_net_weight_kg', 'estimated_total_net_weight_kg',
               'material_usage_kg', *AMOUNT_KEYS, 'unit_cost', 'unit_price', 'notes', 'weight_basis')


def _number(value, label, *, positive=False):
    number = _decimal(value)
    if number is None or (positive and number <= 0):
        raise ValueError(label + '必须是有效的正数。' if positive else label + '必须是有效的非负数。')
    return number


def _policy_number(value, label, *, positive=False):
    if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?', value):
        raise ValueError(label + '须为最多八位小数的十进制数字字符串。')
    number = _number(value, label, positive=positive)
    if number > Decimal('1000000'):
        raise ValueError(label + '超出本次演示允许范围。')
    return number


def _identity(row):
    return (row.get('line_id'), row.get('field'), row.get('requirement'))


def _registered_sources(base):
    sources = base.get('source_files')
    if not isinstance(sources, list):
        raise ValueError('原报价缺少来源文件记录。')
    by_id = {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get('id'), str) or source['id'] in by_id:
            raise ValueError('原报价来源文件身份无效或重复。')
        sha = source.get('sha256')
        if not isinstance(sha, str) or not re.fullmatch(r'[a-f0-9]{64}', sha):
            raise ValueError('原报价缺少来源文件哈希，不能关联核价。')
        by_id[source['id']] = source
    return by_id


def _evidence(row, field, text, registered):
    """Export a bounded public projection, never arbitrary recognition fields."""
    source = registered[row['file_id']]
    result = [{'basis': 'engineering_manifest', 'status': 'pending_review', 'source_text': text,
               'file_id': row['file_id'], 'native_definition': row.get('native_definition'),
               'document': row.get('document'), 'sha256': source['sha256'],
               'field_source': row.get('field_sources', {}).get(field)}]
    values = []
    if field == 'document_note':
        values = [note for note in row.get('document_requirements', []) if note.get('text') == text]
    elif field == 'material_description':
        material = row.get('material_description_evidence')
        values = material.get('references', []) if isinstance(material, dict) else []
    else:
        candidate = row.get('recognition', {}).get('fields', {}).get(field, {})
        value = candidate.get('evidence') if isinstance(candidate, dict) else None
        if isinstance(value, dict):
            values = value.get('drawing', [])
            native = value.get('native')
            if isinstance(native, dict):
                public = {key: copy.deepcopy(native[key]) for key in
                          ('basis', 'status', 'source_text', 'file_id', 'native_definition', 'document')
                          if key in native and type(native[key]) in (str, int, type(None))}
                if public:
                    result.append(public)
        elif isinstance(value, list):
            values = value
    if not isinstance(values, list):
        values = []
    for value in values[:100]:
        if not isinstance(value, dict):
            continue
        reference = value.get('reference')
        evidence_source = registered.get(reference)
        quote = value.get('quote', value.get('text'))
        page, region = value.get('page'), value.get('region')
        if (not evidence_source or evidence_source.get('kind') not in {'image', 'pdf'}
                or evidence_source.get('status') not in {'reference', 'ready'}
                or type(page) is not int or page <= 0 or not isinstance(region, str) or not region.strip()
                or not isinstance(quote, str) or not quote.strip() or len(quote) > 3000
                or value.get('status') != 'pending_review'):
            continue
        result.append({'basis': 'ai_document', 'status': 'pending_review', 'reference': reference,
                       'sha256': evidence_source['sha256'], 'page': page, 'region': region, 'quote': quote})
    return result


def _inputs(base, manifest):
    if not isinstance(base, dict) or not isinstance(manifest, dict):
        raise ValueError('报价或工程资料格式无效。')
    settings = validate_settings(base.get('settings'))
    if base.get('engineering_repricing'):
        raise ValueError('请从原报价建立工程重算版本，不能叠加已有演示核价。')
    originals, engineering = base.get('lines'), manifest.get('lines')
    if (not isinstance(originals, list) or not originals or len(originals) > 50000
            or not isinstance(engineering, list) or len(originals) != len(engineering)):
        raise ValueError('原报价与工程 BOM 的行范围不一致。')
    sources = _registered_sources(base)
    lines, rows = {}, {}
    for line in originals:
        if not isinstance(line, dict) or not isinstance(line.get('id'), str) or line['id'] in lines:
            raise ValueError('原报价行号无效或重复。')
        if line.get('file_id') not in sources:
            raise ValueError('原报价行未关联已登记文件。')
        lines[line['id']] = line
    for row in engineering:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or row['id'] in rows or row['id'] not in lines:
            raise ValueError('工程 BOM 行号无效或重复。')
        line = lines[row['id']]
        for key in ('file_id', 'native_definition', 'quantity_per_set', 'quantity_total'):
            if row.get(key) != line.get(key) or type(row.get(key)) is not type(line.get(key)):
                raise ValueError('工程资料与原报价的文件、零件定义或数量不一致：' + row['id'])
        if 'volume_mm3' in row and row['volume_mm3'] != line.get('volume_mm3'):
            raise ValueError('工程资料与原 CAD 体积不一致：' + row['id'])
        rows[row['id']] = row
    basis = manifest.get('quote_basis', {})
    required = basis.get('requirements_review') if isinstance(basis, dict) else None
    if not isinstance(required, list) or not required or len(required) > 50000:
        raise ValueError('没有可关联的已读工程要求。')
    bound, seen = [], set()
    for requirement in required:
        if not isinstance(requirement, dict):
            raise ValueError('工程要求格式无效。')
        line_id, field, text = _identity(requirement)
        if (not isinstance(line_id, str) or line_id not in rows or not isinstance(field, str) or not isinstance(text, str)
                or not text.strip() or len(text) > 3000 or (line_id, field, text) in seen):
            raise ValueError('工程要求的行号、字段、原文无效或重复。')
        row = rows[line_id]
        if field == 'document_note':
            matches = [note for note in row.get('document_requirements', []) if
                       isinstance(note, dict) and note.get('text') == text and note.get('status') == 'pending_review']
            if not matches:
                raise ValueError('工艺要求未保留匹配的来源原文：' + line_id)
        elif field in {'material_grade', 'material_description', 'surface_treatment'}:
            if row.get(field) != text or row.get('field_sources', {}).get(field) == 'conflict':
                raise ValueError('工程要求与已保存字段不一致或仍有冲突：' + line_id)
        elif field != 'operations':
            raise ValueError('不支持的工程要求字段：' + field)
        seen.add((line_id, field, text))
        bound.append({'line_id': line_id, 'field': field, 'requirement': text,
                      'source_evidence': _evidence(row, field, text, sources)})
    # A truncated review list must not turn an omitted requirement into free work.
    from batch_engineering_bom import _document_note_needs_review
    for row in engineering:
        for field in ('material_grade', 'material_description', 'surface_treatment'):
            value = row.get(field)
            if value and (row['id'], field, value) not in seen:
                raise ValueError('工程核价未覆盖全部已读要求：' + row['id'] + ' / ' + field)
        for note in row.get('document_requirements', []):
            if (isinstance(note, dict) and isinstance(note.get('text'), str)
                    and _document_note_needs_review(note['text'])
                    and (row['id'], 'document_note', note['text']) not in seen
                    and not any(key[0] == row['id'] and key[2] == note['text'] for key in seen)):
                raise ValueError('工程核价未覆盖全部工艺原文：' + row['id'])
    return settings, lines, rows, sorted(bound, key=_identity)


def _template(requirement, settings):
    field, text = requirement['field'], requirement['requirement']
    entry = {key: requirement[key] for key in ('line_id', 'field', 'requirement')}
    if field == 'material_grade' and _grade(text, market=settings['rate_profile'] == MARKET_VERSION):
        entry.update(method='adopt_material', unit='kg', rate=None, minimum='0.00', source_kind='saved_material_price',
                     inclusions=['采用已读材料牌号所属类别和本单已保存材料价；不修改 CAD 材料属性'])
    elif field == 'material_description' and text == MESH_DESCRIPTION:
        entry.update(method='purchase_mesh', unit='piece', rate='2.00', minimum='0.00', source_kind='demo',
                     inclusions=['8目不锈钢纱网采购演示价；不推断304/316或丝径', '替代原材料费，不另加原自制加工费和准备费'])
    elif field == 'document_note' and text == PIPE_NOTE:
        entry.update(method='pipe_weld_seal', unit='piece', rate='15.00', minimum='0.00', source_kind='demo',
                     inclusions=['本件基础加工', '焊接', '常规防漏自检', '准备费', '不表示已完成压力试验或质量验收'])
    elif field == 'document_note' and text == MESH_NOTE:
        entry.update(method='mesh_weld', unit='piece', rate='3.00', minimum='0.00', source_kind='demo',
                     inclusions=['纱网焊接', '准备费', '替代原综合加工和准备费'])
    elif field in {'surface_treatment', 'operations'} and text in ZINC_NOTES:
        entry.update(method='hotdip', unit='kg_order', rate='2.00', minimum='30.00', source_kind='demo',
                     inclusions=['按金属预估净重计热浸锌演示费', '本订单同一热浸锌工序组只收一次最低费', '原基础加工费和准备费不含本次新增表面处理'])
    else:
        raise ValueError('没有覆盖该工程要求的核价模板：' + requirement['line_id'] + ' / ' + field + ' / ' + text)
    return entry


def _material_price(profile, raw, settings):
    if settings['rate_profile'] == MARKET_VERSION:
        snapshot = resolve_material_price(profile, raw, settings)
        rate = _decimal(snapshot.get('normalized_net_rate'))
        if rate is None or rate <= 0:
            raise ValueError('该材料缺少本单有效采用价：' + str(raw or profile))
        return rate, snapshot
    return Decimal(PROFILES[profile]['rate']), None


def _assumptions(entries, requirements, settings):
    assumptions = [
        {'name': '工程核价版本', 'value': VERSION, 'scope': '沿用原订单数量与定价比例；另存模拟报价，工程和商务均待审核。'},
        {'name': '新费率口径', 'value': '采购与工序费率为演示假设', 'scope': '不是好运来的采购成交价或工厂标准工时；未计税、未计运费。'},
        {'name': '材料采用', 'value': '已读牌号 + 原单已保存的材料采用价', 'scope': '原 CAD 材料属性不改写；行情规格、税率和供货形式沿用已保存假设，仍需采购核实。'},
        {'name': '原料利用率', 'value': '85%', 'scope': '自制材料采购计价量=预估金属净重÷0.85；不是排版结果。'},
    ]
    for entry in entries:
        if entry['method'] == 'adopt_material':
            profile = _grade(entry['requirement'], market=settings['rate_profile'] == MARKET_VERSION)
            rate, snapshot = _material_price(profile, entry['requirement'], settings)
            assumptions.append({'name': entry['line_id'] + ' 材料采用价', 'value': _text(rate) + ' 元/kg（未税换算）',
                                'scope': entry['requirement'] + '；密度 ' + PROFILES[profile]['density'] + ' kg/m³；'
                                         + ('沿用本单保存的行情/采用价，未核实供应商成交价及本件规格。' if snapshot else '沿用原单演示材料表。')})
        else:
            value = entry['rate'] + (' 元/kg；本单工序组最低 ' + entry['minimum'] + ' 元' if entry['method'] == 'hotdip' else ' 元/件')
            assumptions.append({'name': entry['line_id'] + ' ' + entry['requirement'], 'value': value + '（演示）',
                                'scope': '；'.join(entry['inclusions'])})
    if any(entry['method'] == 'purchase_mesh' for entry in entries):
        assumptions.append({'name': '纱网预估重量', 'value': 'CAD简化实体体积 × 7980 kg/m³',
                            'scope': '按不锈钢通用估算密度；几何为简化代理模型，未验证丝径、实际网孔与空隙率，不能视为真实纱网重量，也不指定304/316牌号。'})
    if any(entry['method'] == 'hotdip' for entry in entries):
        assumptions.append({'name': '热浸锌分摊', 'value': '工序组费用取总净重×费率与单次最低费的较大值',
                            'scope': '按各行金属净重分摊到分，余分按最大余数与行号稳定分配；不把最低费按零件或套数重复收取。'})
    return assumptions


def pricing_plan(base_result, manifest):
    """Return a source-bound proposal; obtaining a plan never changes a quote."""
    settings, lines, rows, requirements = _inputs(base_result, manifest)
    entries = [_template(requirement, settings) for requirement in requirements]
    enriched = []
    for requirement, entry in zip(requirements, entries):
        line = lines[entry['line_id']]
        value = {**copy.deepcopy(requirement), 'part_name': line.get('part_name'), 'method': entry['method'],
                 'quantity_total': line.get('quantity_total'), 'estimated_total_net_weight_kg': line.get('estimated_total_net_weight_kg'),
                 'priced': line.get('priced') is True, 'material_profile': None, 'material_rate': None, 'price_source': None}
        if entry['method'] == 'adopt_material':
            profile = _grade(entry['requirement'], market=settings['rate_profile'] == MARKET_VERSION)
            rate, snapshot = _material_price(profile, entry['requirement'], settings)
            value.update(material_profile=profile, material_rate=_text(rate), price_source=copy.deepcopy(snapshot))
        enriched.append(value)
    return {'policy': {'version': VERSION, 'base_digest': digest(base_result),
                       'requirements_digest': digest(requirements), 'entries': entries},
            'requirements': enriched, 'assumptions': _assumptions(entries, requirements, settings)}


def _validate_policy(policy, expected):
    if not isinstance(policy, dict) or set(policy) != POLICY_KEYS:
        raise ValueError('工程核价策略字段无效，不能修改来源、材料类别或内部字段。')
    for key in ('version', 'base_digest', 'requirements_digest'):
        if policy[key] != expected[key]:
            raise ValueError('工程核价策略已过期，原报价或已读要求发生变化，请重新预览。')
    if not isinstance(policy['entries'], list) or len(policy['entries']) != len(expected['entries']):
        raise ValueError('核价策略必须覆盖全部已读工程要求。')
    canonical = {_identity(entry): entry for entry in expected['entries']}
    seen, entries = set(), []
    for entry in policy['entries']:
        if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
            raise ValueError('工程核价条目字段无效。')
        identity = _identity(entry)
        if not all(isinstance(value, str) for value in identity) or identity not in canonical or identity in seen:
            raise ValueError('核价条目的行号、字段或来源原文不匹配，或条目重复。')
        preset = canonical[identity]
        for key in ENTRY_KEYS - {'rate', 'minimum'}:
            if entry[key] != preset[key]:
                raise ValueError('不能修改工程核价的方法、单位、原文或包含项。')
        if entry['method'] == 'adopt_material':
            if entry['rate'] is not None or entry['minimum'] != preset['minimum']:
                raise ValueError('材料价须沿用本单已保存采用价，不能通过核价条目改写。')
        else:
            _policy_number(entry['rate'], '演示费率', positive=True)
            minimum = _policy_number(entry['minimum'], '演示最低费')
            if entry['method'] != 'hotdip' and minimum != 0:
                raise ValueError('按件演示费已包含准备费，不支持另外加最低费。')
        seen.add(identity)
        entries.append(copy.deepcopy(entry))
    return sorted(entries, key=_identity)


def _weight_for(line, density, *, mesh=False):
    volume = _number(line.get('volume_mm3'), '原 CAD 单件体积', positive=True)
    quantity = line.get('quantity_total')
    per_set = line.get('quantity_per_set')
    if type(quantity) is not int or quantity <= 0 or type(per_set) is not int or per_set <= 0:
        raise ValueError('工程核价缺少已确认范围的正整数件数。')
    net = Decimal(_weight(volume * density / Decimal('1000000000')))
    line.update(density_kg_m3=_text(density), estimated_net_weight_kg=_weight(net),
                estimated_set_net_weight_kg=_weight(net * per_set), estimated_total_net_weight_kg=_weight(net * quantity),
                weight_basis=('CAD简化代理实体体积 × 不锈钢通用估算密度；未验证丝径和网孔空隙，非实际纱网称重。' if mesh else
                              '原 CAD 体积 × 已读工程牌号采用密度；未经称重，不含包装。'))
    if line.get('confidence') != 'low':
        line['confidence'] = 'proxy_material_estimate' if mesh else 'engineering_material_candidate'
    return net


def _snapshot(line, profile, raw, rate, snapshot):
    line.update(material_profile=profile, material_requirement=raw, material_rate=_text(rate),
                material_assumption='按已读工程要求 ' + raw + ' 采用材料类别和密度；来源原文仍待工程核对，价格条件单独保存。')
    for key in SNAPSHOT_KEYS:
        line.pop(key, None)
    if snapshot is not None:
        line.update(material_price_snapshot=copy.deepcopy(snapshot), material_price_original_rate=snapshot['original_rate'],
                    material_price_tax_included=snapshot['adopted_tax_included'],
                    material_price_vat_rate_percent=snapshot['adopted_vat_rate_percent'],
                    material_price_source_kind=snapshot['source_kind'], material_price_source=snapshot['source_text'],
                    material_price_quote_date=snapshot['quote_date'], material_price_reference_id=snapshot['reference_id'])


def _allocation(entries, lines):
    zinc = [entry for entry in entries if entry['method'] == 'hotdip' and lines[entry['line_id']].get('priced') is True]
    if not zinc:
        return {}
    if len({(_policy_number(entry['rate'], '热浸锌费率'), _policy_number(entry['minimum'], '热浸锌最低费')) for entry in zinc}) != 1:
        raise ValueError('同一订单热浸锌工序组必须采用相同费率和单次最低费。')
    if len({entry['line_id'] for entry in zinc}) != len(zinc):
        raise ValueError('同一零件的热浸锌要求重复，不能重复计费。')
    rate, minimum = Decimal(zinc[0]['rate']), Decimal(zinc[0]['minimum'])
    masses = {entry['line_id']: _number(lines[entry['line_id']].get('estimated_total_net_weight_kg'), '热浸锌金属净重', positive=True) for entry in zinc}
    mass = sum(masses.values(), ZERO)
    total = Decimal(_money(max(mass * rate, minimum)))
    exact = {key: total * value / mass for key, value in masses.items()}
    portions = {key: value.quantize(Decimal('.01'), rounding=ROUND_DOWN) for key, value in exact.items()}
    cents = int((total - sum(portions.values(), ZERO)) * 100)
    order = sorted(exact, key=lambda key: (-(exact[key] - portions[key]), key))
    for key in order[:cents]:
        portions[key] += Decimal('.01')
    return {key: {'amount': _money(portions[key]), 'basis': '本订单热浸锌工序组按金属预估净重分摊',
                  'group': 'order_hotdip', 'group_weight_kg': _weight(mass), 'group_cost': _money(total),
                  'quantity': _weight(masses[key]), 'unit': 'kg_order', 'rate': zinc[0]['rate'], 'minimum': zinc[0]['minimum'],
                  'minimum_applied': mass * rate < minimum, 'source_kind': 'demo', 'method': 'hotdip',
                  'inclusions': copy.deepcopy(zinc[0]['inclusions'])} for key in masses}


def _retotal(result, settings):
    priced = [line for line in result['lines'] if line.get('priced') is True]
    totals = {key: _money(sum((_number(line.get(key), key) for line in priced), ZERO)) if priced else None for key in AMOUNT_KEYS}
    totals.update(priced_lines=len(priced), unpriced_lines=len(result['lines']) - len(priced))
    result['totals'] = totals
    result['pricing_scope'] = '已计价范围' if totals['unpriced_lines'] else '本次工程核价演示成套范围'
    known = [line for line in result['lines'] if _decimal(line.get('estimated_total_net_weight_kg')) is not None]
    complete = bool(result['lines']) and len(known) == len(result['lines'])
    total = sum((Decimal(line['estimated_total_net_weight_kg']) for line in known), ZERO) if known else None
    result['weight_summary'] = {
        'estimated_set_net_weight_kg': _weight(sum((_number(line.get('estimated_set_net_weight_kg'), '每套预估净重') for line in known), ZERO)) if complete else None,
        'estimated_total_net_weight_kg': _weight(total) if complete else None, 'known_total_net_weight_kg': _weight(total),
        'missing_estimated_lines': len(result['lines']) - len(known),
        'low_confidence_lines': sum(line.get('confidence') == 'low' for line in result['lines']),
        'low_confidence_total_net_weight_kg': _weight(sum((Decimal(line['estimated_total_net_weight_kg']) for line in known if line.get('confidence') == 'low'), ZERO))}
    if settings['rate_profile'] == MARKET_VERSION:
        snapshots, seen = [], set()
        for line in result['lines']:
            snapshot = line.get('material_price_snapshot')
            if snapshot and snapshot['material_profile'] not in seen:
                seen.add(snapshot['material_profile'])
                snapshots.append(copy.deepcopy(snapshot))
        result['material_price_sources'] = snapshots


def reprice_quote(base_result, manifest, policy):
    """Return a new pending-review simulation; reject stale or partial policies."""
    settings, originals, rows, requirements = _inputs(base_result, manifest)
    plan = pricing_plan(base_result, manifest)
    entries = _validate_policy(policy, plan['policy'])
    by_line = defaultdict(list)
    for entry in entries:
        by_line[entry['line_id']].append(entry)
    result = copy.deepcopy(base_result)
    result.update(simulated=True, status='pending_review', engineering_approved=False, business_approved=False)
    lines = {line['id']: line for line in result['lines']}
    requirement_map = {_identity(requirement): requirement for requirement in requirements}
    components = defaultdict(list)
    applied = []
    with localcontext() as context:
        context.prec = 64
        for line_id, line_entries in by_line.items():
            line = lines[line_id]
            # Invalid native geometry keeps its original exclusion. Requirements
            # are deliberately left unpriced and cannot be marked resolved.
            if line.get('priced') is not True:
                continue
            quantity = line.get('quantity_total')
            if type(quantity) is not int or quantity <= 0:
                raise ValueError('核价行件数无效：' + line_id)
            methods = [entry['method'] for entry in line_entries]
            if len(methods) != len(set(methods)) or ('purchase_mesh' in methods and 'adopt_material' in methods):
                raise ValueError('同一零件存在重复或冲突的材料/工序核价：' + line_id)
            if 'mesh_weld' in methods and 'purchase_mesh' not in methods:
                raise ValueError('纱网焊接必须关联同一行已读纱网采购规格。')
            if 'pipe_weld_seal' in methods and not any(entry['method'] == 'adopt_material' and
                    _grade(entry['requirement'], market=settings['rate_profile'] == MARKET_VERSION) == 'ss316' for entry in line_entries):
                raise ValueError('圆管焊接防漏模板必须关联同一行的316材料要求。')
            if 'pipe_weld_seal' in methods and 'mesh_weld' in methods:
                raise ValueError('同一行不支持同时套用圆管和纱网焊接模板。')
            for entry in line_entries:
                if entry['method'] == 'adopt_material':
                    profile = _grade(entry['requirement'], market=settings['rate_profile'] == MARKET_VERSION)
                    rate, snapshot = _material_price(profile, entry['requirement'], settings)
                    net = _weight_for(line, Decimal(PROFILES[profile]['density']))
                    _snapshot(line, profile, entry['requirement'], rate, snapshot)
                    line['adopted_material_source'] = copy.deepcopy(requirement_map[_identity(entry)]['source_evidence'])
                    if line.get('mode') != 'buy':
                        usage = net / Decimal('.85')
                        line['material_usage_kg'] = _weight(usage)
                        line['material_cost'] = _money(usage * rate * quantity)
                        # The original generic formula depends on mass. It is
                        # recomputed only when no inclusive welding replaces it.
                        if not {'pipe_weld_seal', 'mesh_weld'}.intersection(methods):
                            line['process_cost'] = _money(max(Decimal('5'), net * Decimal('8')) * quantity)
                    proof = {'method': 'adopt_material', 'amount': line['material_cost'], 'basis': '原CAD体积×采用密度÷85%×本单已保存材料价×件数' if line.get('mode') != 'buy' else '外购件沿用采购计价，材料仅更新估重类别',
                             'source_kind': snapshot['source_kind'] if snapshot else 'demo', 'rate': _text(rate), 'unit': 'kg',
                             'quantity': line.get('material_usage_kg'), 'order_quantity': quantity,
                             'material_profile': profile, 'material_rate': _text(rate), 'density_kg_m3': PROFILES[profile]['density'],
                             'material_price_snapshot': copy.deepcopy(snapshot)}
                    components[line_id].append(proof)
                elif entry['method'] == 'purchase_mesh':
                    _weight_for(line, Decimal('7980'), mesh=True)
                    line.update(mode='buy', material_profile='stainless_mesh', material_requirement=MESH_DESCRIPTION,
                                material_rate=None, material_usage_kg=None,
                                material_assumption='8目不锈钢纱网，牌号未定；按件采购演示计价；不推断304/316或丝径。',
                                material_cost='0.00', process_cost='0.00', setup_cost='0.00',
                                purchase_cost=_money(Decimal(entry['rate']) * quantity))
                    for key in SNAPSHOT_KEYS:
                        line.pop(key, None)
                    line['adopted_material_source'] = copy.deepcopy(requirement_map[_identity(entry)]['source_evidence'])
                    proof = {'method': 'purchase_mesh', 'amount': line['purchase_cost'], 'basis': '纱网采购演示单价×订单件数',
                             'source_kind': 'demo', 'rate': entry['rate'], 'quantity': quantity, 'unit': 'piece',
                             'material_profile': 'stainless_mesh', 'inclusions': copy.deepcopy(entry['inclusions'])}
                    components[line_id].append(proof)
            for entry in line_entries:
                if entry['method'] in {'pipe_weld_seal', 'mesh_weld'}:
                    line.update(process_cost=_money(Decimal(entry['rate']) * quantity), setup_cost='0.00')
                    components[line_id].append({'method': entry['method'], 'amount': line['process_cost'],
                        'basis': '含准备的本件工序演示费×订单件数，替代原综合加工费和准备费', 'source_kind': 'demo',
                        'rate': entry['rate'], 'quantity': quantity, 'unit': 'piece', 'inclusions': copy.deepcopy(entry['inclusions']),
                        'replaces': {'process_cost': originals[line_id]['process_cost'], 'setup_cost': originals[line_id]['setup_cost']}})
        allocations = _allocation(entries, lines)
        for line_id, proof in allocations.items():
            line = lines[line_id]
            line['process_cost'] = _money(_number(line['process_cost'], '基础加工费') + Decimal(proof['amount']))
            components[line_id].append(proof)
        for line_id, line_entries in by_line.items():
            line = lines[line_id]
            if line.get('priced') is not True:
                continue
            cost = sum((_number(line[key], '成本分项') for key in AMOUNT_KEYS[:4]), ZERO)
            quantity, percent = line['quantity_total'], Decimal(settings['rate_percent']) / 100
            unit_price = cost / quantity * (1 + percent) if settings['pricing_method'] == 'cost_plus' else cost / quantity / (1 - percent)
            line.update(unit_cost=_money(cost / quantity), total_cost=_money(cost), unit_price=_money(unit_price),
                        total_price=_money(Decimal(_money(unit_price)) * quantity))
            line['engineering_pricing_methods'] = [entry['method'] for entry in line_entries]
            previous_snapshot = originals[line_id].get('material_price_snapshot')
            outdated = set(previous_snapshot.get('notes', [])) if isinstance(previous_snapshot, dict) else set()
            outdated.add('名称规则识别为外购候选；每件 0.80 元仅为演示价。')
            line['notes'] = [note for note in copy.deepcopy(originals[line_id].get('notes', [])) if note not in outdated]
            snapshot = line.get('material_price_snapshot')
            if isinstance(snapshot, dict):
                line['notes'].extend(copy.deepcopy(snapshot.get('notes', [])))
            line['notes'].extend(entry['requirement'] + '；' + '；'.join(entry['inclusions']) for entry in line_entries)
            if line.get('material_profile') == 'stainless_mesh':
                line['notes'].append(line['weight_basis'])
            line['notes'].append('按已读工程要求另存演示核价版本；采购与新增工序费率为显式假设，工程及商务仍待审核。')
            for entry in line_entries:
                proof = next(value for value in components[line_id] if value['method'] == entry['method'])
                applied.append({**copy.deepcopy(requirement_map[_identity(entry)]), 'status': 'priced', 'method': entry['method'],
                                'material_profile': line.get('material_profile'), 'material_rate': line.get('material_rate'),
                                'cost_evidence': {'line_id': line_id, **copy.deepcopy(proof)}})
        _retotal(result, settings)
    assumptions = _assumptions(entries, requirements, settings)
    changes = [{'line_id': line_id, 'part_name': lines[line_id].get('part_name'),
                'before': {key: copy.deepcopy(originals[line_id].get(key)) for key in CHANGE_KEYS},
                'after': {key: copy.deepcopy(lines[line_id].get(key)) for key in CHANGE_KEYS}}
               for line_id in sorted(components)]
    line_costs = [{'line_id': line_id, **{key: lines[line_id][key] for key in (*AMOUNT_KEYS, 'unit_cost', 'unit_price')},
                   'components': copy.deepcopy(components[line_id])} for line_id in sorted(components)]
    result['engineering_repricing'] = {'version': VERSION, 'base_run_id': base_result.get('run_id'),
        'base_digest': digest(base_result), 'requirements_digest': plan['policy']['requirements_digest'],
        'policy': {**copy.deepcopy(policy), 'entries': entries}, 'before_totals': copy.deepcopy(base_result['totals']),
        'after_totals': copy.deepcopy(result['totals']), 'changes': changes, 'line_costs': line_costs,
        'applied_requirements': sorted(applied, key=_identity), 'assumptions': assumptions}
    result['assumptions'] = copy.deepcopy(base_result.get('assumptions', [])) + copy.deepcopy(assumptions)
    outdated_limitation = '附图、PDF 或其他参考文件已保留；本版未自动解释其工艺要求或将其加入费用。'
    result['limitations'] = [value for value in copy.deepcopy(base_result.get('limitations', [])) if value != outdated_limitation] + [
        '工程要求已按本次演示模板计入；不代表工艺顺序、生产图、压力试验或质量要求已经验证。',
        '仅对有原生有效几何且原先已计价的行进行重算；原未计价行仍未计价，不代表免费。']
    return result
