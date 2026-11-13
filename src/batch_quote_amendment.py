"""Typed, deterministic amendments to saved simulated quotations.

This module never reads files, calls an AI/CAD engine, or mutates its inputs.
The caller persists the preview and explicitly commits it as a new RUN. Model
output is restricted to the public patch; geometry and pricing evidence are
always taken from the saved quotation, never from that patch.
"""
from __future__ import annotations

import copy
from decimal import Decimal, localcontext
import re

from batch_quote_costing import AMOUNT_KEYS, PROFILES, _decimal, _grade, _money, _text, _weight, validate_settings
from batch_engineering_pricing import _allocation, _material_price, _retotal, SNAPSHOT_KEYS
from prep_record import digest


VERSION = 'quote-amendment-v1'
ZERO = Decimal('0')
GLOBAL_FIELDS = {'order_sets', 'pricing_method', 'rate_percent'}
LINE_FIELDS = {'purchase_rate', 'process_rate', 'hotdip_rate', 'hotdip_minimum', 'material_profile'}
PROCESS_METHODS = {'generic_processing', 'pipe_weld_seal', 'mesh_weld'}
ENGINEERING_METHODS = {'adopt_material', 'purchase_mesh', 'pipe_weld_seal', 'mesh_weld', 'hotdip'}
LABELS = {'order_sets': '订单套数', 'pricing_method': '定价方式', 'rate_percent': '定价比例（%）',
          'purchase_rate': '采购采用价（元/件）', 'process_rate': '加工采用价（元/件）',
          'hotdip_rate': '热浸锌采用价（元/kg）', 'hotdip_minimum': '整单热浸锌最低收费（元）',
          'material_profile': '本单估价材料'}
SOURCE_LABEL = '用户指定的本单采用价，待审核；非已核实供应商成交价'


def _number(value, name, *, positive=False):
    number = _decimal(value)
    if number is None or positive and number <= 0:
        raise ValueError(name + ('须为正数。' if positive else '须为非负数。'))
    return number


def _input_number(value, name, *, positive=False, maximum=Decimal('1000000')):
    if (not isinstance(value, str) or len(value) > 32
            or not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?', value)):
        raise ValueError(name + '须为最多八位小数的十进制数字字符串。')
    number = _number(value, name, positive=positive)
    if number > maximum:
        raise ValueError(name + '超出可处理范围。')
    return _text(number)


def _lines(base):
    if not isinstance(base, dict) or not isinstance(base.get('lines'), list) or not 1 <= len(base['lines']) <= 50000:
        raise ValueError('当前没有可修改的已保存报价。')
    validate_settings(base.get('settings'))
    lines = {}
    for line in base['lines']:
        identity = line.get('id') if isinstance(line, dict) else None
        if not isinstance(identity, str) or not re.fullmatch(r'L[0-9]{4,8}', identity) or identity in lines:
            raise ValueError('报价行号无效或重复。')
        lines[identity] = line
        per_set, quantity = line.get('quantity_per_set'), line.get('quantity_total')
        if per_set is not None and (type(per_set) is not int or per_set <= 0
                or type(quantity) is not int or quantity != per_set * base['settings']['order_sets']):
            raise ValueError('原报价的成套用量与订单件数不一致：' + identity)
        if line.get('priced') is True:
            if per_set is None or line.get('mode') not in {'make', 'buy'}:
                raise ValueError('已计价行缺少明确用量或采购方式：' + identity)
            costs = [_number(line.get(key), '原成本分项') for key in AMOUNT_KEYS[:4]]
            if sum(costs, ZERO) != _number(line.get('total_cost'), '原成本合计'):
                raise ValueError('原报价成本分项不守恒：' + identity)
    return lines


def _engineering_components(base, lines):
    snapshot = base.get('engineering_repricing')
    if snapshot is None:
        return {}
    if (not isinstance(snapshot, dict) or snapshot.get('version') != 'engineering-demo-v1'
            or snapshot.get('after_totals') != base.get('totals')
            or not isinstance(snapshot.get('line_costs'), list)):
        raise ValueError('已有工程核价依据与当前报价不一致，请先核对原报价。')
    result = {}
    for cost in snapshot['line_costs']:
        identity = cost.get('line_id') if isinstance(cost, dict) else None
        if identity not in lines or identity in result or lines[identity].get('priced') is not True:
            raise ValueError('已有工程核价行来源无效。')
        if any(cost.get(key) != lines[identity].get(key) for key in (*AMOUNT_KEYS, 'unit_cost', 'unit_price')):
            raise ValueError('已有工程核价金额与报价行不一致：' + identity)
        components = cost.get('components')
        if not isinstance(components, list) or not components:
            raise ValueError('已有工程核价缺少包含项记录：' + identity)
        methods = []
        for item in components:
            if not isinstance(item, dict) or item.get('method') not in ENGINEERING_METHODS:
                raise ValueError('已有工程核价存在不支持的费用：' + identity)
            methods.append(item['method'])
            _number(item.get('amount'), '已有工程费用')
        if len(methods) != len(set(methods)):
            raise ValueError('已有工程核价存在重复费用：' + identity)
        result[identity] = copy.deepcopy(components)
    return result


def _basis(base, lines, components):
    """Freeze variable unit bases separately from order-level setup/minima."""
    previous = base.get('quote_amendment')
    if previous is not None:
        if (not isinstance(previous, dict) or previous.get('version') != VERSION
                or previous.get('after_totals') != base.get('totals')
                or not isinstance(previous.get('pricing_basis'), dict)):
            raise ValueError('已有修改记录与当前报价不一致。')
        basis = copy.deepcopy(previous['pricing_basis'])
        if set(basis) != {key for key, line in lines.items() if line.get('priced') is True}:
            raise ValueError('已有修改的计价范围与当前报价不一致。')
        return basis
    basis = {}
    for identity, line in lines.items():
        if line.get('priced') is not True:
            continue
        quantity = Decimal(line['quantity_total'])
        saved = {item['method']: item for item in components.get(identity, [])}
        welds = [method for method in ('pipe_weld_seal', 'mesh_weld') if method in saved]
        if len(welds) > 1:
            raise ValueError('本件存在重叠的焊接包含项，需先核对：' + identity)
        generic = _number(line['process_cost'], '原加工费') - sum(
            (_number(item['amount'], '原工序费') for key, item in saved.items()
             if key in {'pipe_weld_seal', 'mesh_weld', 'hotdip'}), ZERO)
        if generic < 0 or welds and generic != 0:
            raise ValueError('加工费与已保存包含项不一致：' + identity)
        net = _decimal(line.get('estimated_net_weight_kg'))
        material_unit = _number(line['material_cost'], '材料费') / quantity
        rate = _decimal(line.get('material_rate'))
        if line['mode'] == 'make' and net is not None and rate is not None:
            candidate = net / Decimal('.85') * rate
            if _money(candidate * quantity) == line['material_cost']:
                material_unit = candidate
        generic_rate = generic / quantity
        density_based = False
        if line['mode'] == 'make' and not welds and net is not None:
            candidate = max(Decimal('5'), net * Decimal('8'))
            if _money(candidate * quantity) == _money(generic):
                generic_rate, density_based = candidate, True
        basis[identity] = {
            'material_unit_cost': _text(material_unit), 'purchase_rate': _text(_number(line['purchase_cost'], '外购费') / quantity),
            'generic_rate': _text(generic_rate), 'generic_density_based': density_based,
            'setup_cost': line['setup_cost'], 'weld_method': welds[0] if welds else None,
            'weld_rate': saved[welds[0]].get('rate') if welds else None,
            'weld_source_kind': saved[welds[0]].get('source_kind') if welds else None,
            'purchase_source_kind': saved.get('purchase_mesh', {}).get('source_kind') or 'demo',
            'generic_source_kind': 'demo', 'hotdip': copy.deepcopy(saved.get('hotdip')),
            'material_override': False,
        }
    return basis


def validate_patch(base_result, patch):
    """Validate public fields; model output can never supply computed amounts."""
    lines = _lines(base_result)
    components = _engineering_components(base_result, lines)
    if not isinstance(patch, dict) or set(patch) != {'changes'} or not isinstance(patch['changes'], list) or not 1 <= len(patch['changes']) <= 32:
        raise ValueError('请提供1至32项明确修改。')
    output, seen = [], set()
    hotdip_group = {identity for identity, values in components.items() if any(item['method'] == 'hotdip' for item in values)}
    for change in patch['changes']:
        if (not isinstance(change, dict) or not isinstance(change.get('field'), str)
                or change['field'] not in GLOBAL_FIELDS | LINE_FIELDS):
            raise ValueError('该修改字段暂不支持。')
        field = change['field']
        expected = {'field', 'value'} | ({'line_ids'} if field in LINE_FIELDS else set()) | ({'method'} if field == 'process_rate' else set())
        if set(change) != expected or not isinstance(change.get('value'), str):
            raise ValueError('修改需明确数值与适用零件；不能提交金额、来源或内部字段。')
        value = change['value']
        if field == 'order_sets':
            if not re.fullmatch(r'[1-9][0-9]{0,6}', value) or int(value) > 1000000:
                raise ValueError('订单套数须为1至1000000的整数。')
        elif field == 'pricing_method':
            if value not in {'cost_plus', 'gross_margin'}:
                raise ValueError('定价方式须为成本加成或目标毛利率。')
        elif field == 'material_profile':
            if value not in PROFILES:
                raise ValueError('请选择已有材料估价类别；网片牌号和丝径需采购规格及价格单独核定。')
        else:
            value = _input_number(value, LABELS[field], positive=field not in {'rate_percent', 'hotdip_minimum'},
                                  maximum=Decimal('1000') if field == 'rate_percent' else Decimal('1000000'))
        item = {'field': field, 'value': value}
        if field in LINE_FIELDS:
            targets = change['line_ids']
            if (not isinstance(targets, list) or not 1 <= len(targets) <= len(lines)
                    or any(not isinstance(identity, str) or identity not in lines for identity in targets)
                    or len(targets) != len(set(targets))):
                raise ValueError('请明确本次修改的原始报价行。')
            item['line_ids'] = sorted(targets)
            if field in {'hotdip_rate', 'hotdip_minimum'} and (not hotdip_group or set(targets) != hotdip_group):
                raise ValueError('热浸锌修改须覆盖本单已有热浸锌工序组的全部零件；尚未核价的工序请先核价。')
            method = change.get('method')
            if field == 'process_rate':
                if not isinstance(method, str) or method not in PROCESS_METHODS:
                    raise ValueError('请明确修改基础加工、圆管焊接或网片焊接的哪一项。')
                item['method'] = method
            for identity in targets:
                line = lines[identity]
                saved = {entry['method']: entry for entry in components.get(identity, [])}
                if line.get('priced') is not True:
                    raise ValueError('本行原几何或计价依据未完成，不能通过聊天补价：' + identity)
                if field == 'purchase_rate' and line['mode'] != 'buy':
                    raise ValueError('采购单价只适用于已有外购物料：' + identity)
                if field == 'material_profile' and line['mode'] != 'make':
                    raise ValueError('网片及外购件需按采购规格核价，本次材料修改仅支持自制件：' + identity)
                if field == 'process_rate':
                    if method == 'generic_processing' and (line['mode'] != 'make' or {'pipe_weld_seal', 'mesh_weld'} & set(saved)):
                        raise ValueError('本行已有包含基础加工/准备的焊接费，或属于外购件；请指定已有工序：' + identity)
                    if method != 'generic_processing' and method not in saved:
                        raise ValueError('本行尚无该工序的计量与包含项，请先完成工程核价：' + identity)
                key = (field, identity, method)
                if key in seen:
                    raise ValueError('同一字段与零件存在重复修改，请合并为一个明确值。')
                seen.add(key)
        else:
            if (field, None, None) in seen:
                raise ValueError('同一全局条件存在重复修改。')
            seen.add((field, None, None))
        output.append(item)
    settings = copy.deepcopy(base_result['settings'])
    for item in output:
        if item['field'] in GLOBAL_FIELDS:
            settings[item['field']] = int(item['value']) if item['field'] == 'order_sets' else item['value']
    validate_settings(settings)
    return {'changes': output}


def _material_override(line, basis, profile, settings):
    try:
        rate, snapshot = _material_price(profile, None, settings)
    except ValueError as error:
        raise ValueError('本单没有' + PROFILES[profile]['name'] + '的有效材料采用价，请先录价。') from error
    volume = _number(line.get('volume_mm3'), '原生单件体积', positive=True)
    net = Decimal(_weight(volume * Decimal(PROFILES[profile]['density']) / Decimal('1000000000')))
    line.update(material_profile=profile, material_rate=_text(rate), density_kg_m3=PROFILES[profile]['density'],
                estimated_net_weight_kg=_weight(net), material_usage_kg=_weight(net / Decimal('.85')),
                material_assumption='用户指定本单按' + PROFILES[profile]['name'] + '估价；不改写图纸或CAD材料原文，仍待工程核对。',
                weight_basis='原CAD体积 × 本次估价材料密度；未经称重，不含包装。')
    if line.get('confidence') != 'low':
        line['confidence'] = 'assumed_material'
    for key in SNAPSHOT_KEYS:
        line.pop(key, None)
    if snapshot:
        line.update(material_price_snapshot=copy.deepcopy(snapshot), material_price_original_rate=snapshot['original_rate'],
                    material_price_tax_included=snapshot['adopted_tax_included'],
                    material_price_vat_rate_percent=snapshot['adopted_vat_rate_percent'],
                    material_price_source_kind=snapshot['source_kind'], material_price_source=snapshot['source_text'],
                    material_price_quote_date=snapshot['quote_date'], material_price_reference_id=snapshot['reference_id'])
    basis['material_unit_cost'] = _text(net / Decimal('.85') * rate)
    basis['material_override'] = True
    if basis['generic_density_based']:
        basis['generic_rate'] = _text(max(Decimal('5'), net * Decimal('8')))


def _evaluate(result, basis, templates):
    lines = {line['id']: line for line in result['lines']}
    settings = result['settings']
    zinc_entries = []
    new_components = {}
    for identity, line in lines.items():
        per_set = line.get('quantity_per_set')
        line['quantity_total'] = per_set * settings['order_sets'] if type(per_set) is int and per_set > 0 else None
        net = _decimal(line.get('estimated_net_weight_kg'))
        if net is not None and type(per_set) is int:
            line.update(estimated_set_net_weight_kg=_weight(net * per_set),
                        estimated_total_net_weight_kg=_weight(net * line['quantity_total']))
        if line.get('priced') is not True:
            continue
        values = basis[identity]
        quantity = line['quantity_total']
        generic = _number(values['generic_rate'], '基础加工采用价')
        weld = _number(values['weld_rate'], '焊接采用价') if values.get('weld_method') else ZERO
        line.update(material_cost=_money(_number(values['material_unit_cost'], '材料单件成本') * quantity),
                    process_cost=_money((generic + weld) * quantity),
                    purchase_cost=_money(_number(values['purchase_rate'], '采购采用价') * quantity),
                    setup_cost=_money(_number(values['setup_cost'], '按单准备费')))
        components = copy.deepcopy(templates.get(identity, []))
        for proof in components:
            method = proof['method']
            if method == 'adopt_material':
                proof.update(amount=line['material_cost'], quantity=line.get('material_usage_kg'), order_quantity=quantity,
                             material_profile=line.get('material_profile'), material_rate=line.get('material_rate'),
                             density_kg_m3=line.get('density_kg_m3'), rate=line.get('material_rate'),
                             material_price_snapshot=copy.deepcopy(line.get('material_price_snapshot')))
                if values.get('material_override'):
                    proof.update(basis='原CAD体积×本次估价密度÷85%×已保存采用材料价×件数',
                                 source_kind=line.get('material_price_source_kind') or 'demo', source_label='本单材料估价条件，非图纸牌号确认')
            elif method == 'purchase_mesh':
                proof.update(amount=line['purchase_cost'], rate=values['purchase_rate'], quantity=quantity,
                             source_kind=values['purchase_source_kind'])
                if proof['source_kind'] == 'manual':
                    proof.update(source_label=SOURCE_LABEL, basis='本单指定网片采购单价×订单件数')
            elif method in {'pipe_weld_seal', 'mesh_weld'}:
                proof.update(amount=_money(weld * quantity), rate=values['weld_rate'], quantity=quantity,
                             source_kind=values['weld_source_kind'])
                if proof['source_kind'] == 'manual':
                    proof.update(source_label=SOURCE_LABEL, basis='含准备的本单指定工序单价×订单件数；替代原综合加工与准备费')
        new_components[identity] = components
        hotdip = values.get('hotdip')
        if hotdip:
            zinc_entries.append({'line_id': identity, 'method': 'hotdip', 'rate': hotdip['rate'],
                                  'minimum': hotdip['minimum'], 'inclusions': hotdip.get('inclusions', [])})
    allocations = _allocation(zinc_entries, lines)
    for identity, proof in allocations.items():
        line = lines[identity]
        hotdip = basis[identity]['hotdip']
        if hotdip.get('source_kind') == 'manual':
            proof.update(source_kind='manual', source_label=SOURCE_LABEL)
        line['process_cost'] = _money(Decimal(line['process_cost']) + Decimal(proof['amount']))
        components = new_components[identity]
        components[:] = [item for item in components if item['method'] != 'hotdip'] + [proof]
    for identity, line in lines.items():
        if line.get('priced') is not True:
            continue
        quantity = line['quantity_total']
        cost = sum((Decimal(line[key]) for key in AMOUNT_KEYS[:4]), ZERO)
        percent = Decimal(settings['rate_percent']) / 100
        unit = cost / quantity * (1 + percent) if settings['pricing_method'] == 'cost_plus' else cost / quantity / (1 - percent)
        line.update(total_cost=_money(cost), unit_cost=_money(cost / quantity), unit_price=_money(unit),
                    total_price=_money(Decimal(_money(unit)) * quantity))
    _retotal(result, settings)
    snapshot = result.get('engineering_repricing')
    if isinstance(snapshot, dict):
        snapshot['after_totals'] = copy.deepcopy(result['totals'])
        snapshot['line_costs'] = [{'line_id': identity, **{key: lines[identity][key] for key in (*AMOUNT_KEYS, 'unit_cost', 'unit_price')},
                                 'components': copy.deepcopy(new_components[identity])} for identity in templates]
        for item in snapshot.get('applied_requirements', []):
            identity = item['line_id']
            proof = next(entry for entry in new_components[identity] if entry['method'] == item['method'])
            item.update(cost_evidence={'line_id': identity, **copy.deepcopy(proof)},
                        material_profile=lines[identity].get('material_profile'), material_rate=lines[identity].get('material_rate'))
            if item['method'] == 'adopt_material':
                required = _grade(item['requirement'], market=settings['rate_profile'] != 'demo-v1')
                if required != lines[identity].get('material_profile'):
                    item.update(status='pending_review', reason='本单估价材料与工程要求不同，需工程核对。')
                    lines[identity]['adopted_material_source'] = []
                elif item.get('status') == 'pending_review' and item.get('reason') == '本单估价材料与工程要求不同，需工程核对。':
                    item.update(status='priced')
                    item.pop('reason', None)
                    lines[identity]['adopted_material_source'] = copy.deepcopy(item.get('source_evidence', []))
        for entry in snapshot.get('policy', {}).get('entries', []):
            identity, method = entry['line_id'], entry['method']
            values = basis.get(identity)
            if not values:
                continue
            if method == 'hotdip' and values.get('hotdip'):
                entry.update(rate=values['hotdip']['rate'], minimum=values['hotdip']['minimum'], source_kind=values['hotdip']['source_kind'])
            elif method in {'pipe_weld_seal', 'mesh_weld'}:
                entry.update(rate=values['weld_rate'], source_kind=values['weld_source_kind'])
            elif method == 'purchase_mesh':
                entry.update(rate=values['purchase_rate'], source_kind=values['purchase_source_kind'])
        for change in snapshot.get('changes', []):
            line = lines.get(change.get('line_id'))
            if line:
                change['after'] = {key: copy.deepcopy(line.get(key)) for key in change.get('after', {})}
    return new_components


def _prior_value(base, basis, change):
    field = change['field']
    if field in GLOBAL_FIELDS:
        return str(base['settings'][field])
    lines = {line['id']: line for line in base['lines']}
    values = {}
    for identity in change['line_ids']:
        saved = basis[identity]
        if field == 'purchase_rate': value = saved['purchase_rate']
        elif field == 'material_profile': value = lines[identity].get('material_profile')
        elif field == 'process_rate': value = saved['generic_rate'] if change['method'] == 'generic_processing' else saved['weld_rate']
        else: value = saved['hotdip']['rate' if field == 'hotdip_rate' else 'minimum']
        values[identity] = value
    unique = set(values.values())
    return str(next(iter(unique))) if len(unique) == 1 else '；'.join(identity + '=' + str(value) for identity, value in values.items())


def amend_quote(base_result, patch):
    """Preview explicit changes using saved scalars, preserving all source facts."""
    with localcontext() as context:
        context.prec = 64
        normalized = validate_patch(base_result, patch)
        lines = _lines(base_result)
        templates = _engineering_components(base_result, lines)
        basis = _basis(base_result, lines, templates)
        # Before applying the patch, replay the saved unit/whole-order bases.
        # This prevents stale or tampered internal metadata from pricing a new run.
        replay = copy.deepcopy(base_result)
        _evaluate(replay, copy.deepcopy(basis), templates)
        if replay['totals'] != base_result.get('totals') or any(
                any(line.get(key) != lines[line['id']].get(key) for key in AMOUNT_KEYS)
                for line in replay['lines'] if line.get('priced') is True):
            raise ValueError('已有单位费用与当前报价不一致，请先核对计价依据。')
        result = copy.deepcopy(base_result)
        amended_lines = {line['id']: line for line in result['lines']}
        original_basis = copy.deepcopy(basis)
        changes, affected, warnings = [], set(), []
        for change in normalized['changes']:
            field, value = change['field'], change['value']
            before = _prior_value(base_result, original_basis, change)
            summary = {**copy.deepcopy(change), 'label': LABELS[field], 'before': before, 'after': value}
            changes.append(summary)
            if field in GLOBAL_FIELDS:
                result['settings'][field] = int(value) if field == 'order_sets' else value
                affected.update(lines)
            else:
                for identity in change['line_ids']:
                    saved, line = basis[identity], amended_lines[identity]
                    if field == 'purchase_rate':
                        saved.update(purchase_rate=value, purchase_source_kind='manual')
                        line.update(purchase_rate=value, purchase_price_source_kind='manual', purchase_price_source=SOURCE_LABEL)
                        outdated = '名称规则识别为外购候选；每件 0.80 元仅为演示价。'
                        line['notes'] = [note for note in line.get('notes', []) if note != outdated]
                    elif field == 'process_rate':
                        if change['method'] == 'generic_processing':
                            saved.update(generic_rate=value, generic_density_based=False, generic_source_kind='manual')
                        else:
                            saved.update(weld_rate=value, weld_source_kind='manual')
                    elif field in {'hotdip_rate', 'hotdip_minimum'}:
                        saved['hotdip']['rate' if field == 'hotdip_rate' else 'minimum'] = value
                        saved['hotdip']['source_kind'] = 'manual'
                    elif field == 'material_profile':
                        _material_override(line, saved, value, result['settings'])
                        raw = line.get('material_raw') or line.get('material_requirement')
                        if raw and _grade(raw, market=result['settings']['rate_profile'] != 'demo-v1') not in {None, value}:
                            warnings.append(identity + ' 本单估价材料与原工程材料要求不同，工程要求仍待核对。')
                    affected.add(identity)
        result['settings'] = validate_settings(result['settings'])
        _evaluate(result, basis, templates)
        for item in (result.get('engineering_repricing') or {}).get('applied_requirements', []):
            if item.get('status') == 'pending_review' and item.get('reason') == '本单估价材料与工程要求不同，需工程核对。':
                warnings.append(item['line_id'] + ' 本单估价材料与原工程材料要求不同，工程要求仍待核对。')
        result.update(simulated=True, status='pending_review', engineering_approved=False, business_approved=False)
        previous = base_result.get('quote_amendment') or {}
        lineage = copy.deepcopy(previous.get('lineage', []))
        lineage.append({'source_run_id': base_result.get('run_id'), 'source_digest': digest(base_result), 'patch': normalized})
        if len(lineage) > 100:
            raise ValueError('本报价修改历史过长，请建立新的报价任务。')
        result['quote_amendment'] = {
            'version': VERSION, 'source_run_id': base_result.get('run_id'), 'source_digest': digest(base_result),
            'status': 'pending_review', 'patch': normalized, 'pricing_basis': copy.deepcopy(basis),
            'before_totals': copy.deepcopy(base_result['totals']), 'after_totals': copy.deepcopy(result['totals']),
            'changes': changes, 'affected_line_ids': sorted(affected), 'warnings': list(dict.fromkeys(warnings)),
            'lineage': lineage, 'price_source': SOURCE_LABEL,
        }
        result['assumptions'] = [copy.deepcopy(item) for item in base_result.get('assumptions', []) if item.get('name') != '报价修改采用条件']
        for item in result['assumptions']:
            if item.get('name') == '成套范围':
                item['scope'] = '套数 ' + str(result['settings']['order_sets']) + '；不跨文件自动去重或合并'
            elif item.get('name') == '定价':
                item['value'] = ('成本加成' if result['settings']['pricing_method'] == 'cost_plus' else '目标毛利率') + ' ' + result['settings']['rate_percent'] + '%'
            elif item.get('name') in {'综合加工演示费', '外购候选', '原料利用率'}:
                suffix = '；仅适用于未被明确修改覆盖的行，逐项采用条件以最新修改记录为准。'
                if not str(item.get('scope', '')).endswith(suffix):
                    item['scope'] = str(item.get('scope', '')) + suffix
        snapshot = result.get('engineering_repricing')
        if isinstance(snapshot, dict):
            entries = snapshot.get('policy', {}).get('entries', [])
            for container in (result['assumptions'], snapshot.get('assumptions', [])):
                for assumption in container:
                    for entry in entries:
                        identity, method = entry['line_id'], entry['method']
                        if assumption.get('name') == identity + ' ' + entry.get('requirement', '') and method != 'adopt_material':
                            assumption['value'] = entry['rate'] + (' 元/kg；本单工序组最低 ' + entry['minimum'] + ' 元' if method == 'hotdip' else ' 元/件') + ('（本单指定采用价，待审核）' if entry.get('source_kind') == 'manual' else '（演示）')
                        if assumption.get('name') == identity + ' 材料采用价' and basis.get(identity, {}).get('material_override'):
                            line = amended_lines[identity]
                            assumption.update(value=str(line.get('material_rate')) + ' 元/kg（未税采用价）',
                                scope='本单估价采用' + PROFILES[line['material_profile']]['name'] + '；原图纸材料要求保留，尚待工程与采购核对。')
        result['assumptions'].append({'name': '报价修改采用条件', 'value': '以本次确认的修改及当前单位费用为准',
            'scope': SOURCE_LABEL + '；按单准备费不随套数倍增，热浸锌组最低收费重新核计一次。原演示模板未被修改的部分继续沿用。'})
        summary = {'before_totals': copy.deepcopy(base_result['totals']), 'after_totals': copy.deepcopy(result['totals']),
                   'before_order_sets': base_result['settings']['order_sets'], 'after_order_sets': result['settings']['order_sets'],
                   'changes': changes, 'affected_line_ids': sorted(affected), 'warnings': list(dict.fromkeys(warnings))}
        return {'result': result, 'summary': summary}
