"""Deterministic, explicitly simulated pricing over native CAD measurements.

This module performs no CAD/model calls, file reads or business approvals.
Geometry is an internal input; only measured scalar values and provenance are
returned. Product net mass and raw-material purchasing usage remain separate.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import copy
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import html
import io
import json
import re
from urllib.parse import quote

from batch_material_prices import VERSION as MARKET_VERSION, resolve_material_price, validate_material_pricing
from batch_artifact_preview import PREVIEW_FILES, render_artifact_preview
from cad_material import is_placeholder_material_name, material_grade_candidate


PROFILES = {
    'carbon_steel': {'name': '碳钢（Q235 类演示假设）', 'density': '7850', 'rate': '6'},
    'spcc': {'name': '冷轧钢板（SPCC 类假设）', 'density': '7850', 'rate': '6'},
    'ss304': {'name': '304 不锈钢', 'density': '7930', 'rate': '26'},
    'ss316': {'name': '316 不锈钢', 'density': '7980', 'rate': '28'},
    'al5052': {'name': '5052 铝合金', 'density': '2680', 'rate': '20'},
}
SETTINGS_KEYS = {'order_sets', 'pricing_method', 'rate_percent', 'material_profile',
                 'rate_profile', 'demo_confirmed', 'scope_confirmed'}
AMOUNT_KEYS = ('material_cost', 'process_cost', 'purchase_cost', 'setup_cost', 'total_cost', 'total_price')
ZERO = Decimal(0)


def validate_settings(settings):
    """Validate explicit operator choices before starting expensive CAD work."""
    market = isinstance(settings, dict) and settings.get('rate_profile') == MARKET_VERSION
    expected = SETTINGS_KEYS | ({'material_pricing'} if market else set())
    if isinstance(settings, dict) and 'costing_mode' in settings:
        expected |= {'costing_mode'}
        if settings['costing_mode'] != 'process-v1':
            raise ValueError('工艺核价方案无效。')
    if not isinstance(settings, dict) or set(settings) != expected:
        raise ValueError('请填写套数、定价方案及演示假设确认。')
    if type(settings['order_sets']) is not int or not 0 < settings['order_sets'] <= 1000000:
        raise ValueError('订单套数须为 1～1000000 的整数。')
    if settings['pricing_method'] not in {'cost_plus', 'gross_margin'}:
        raise ValueError('请选择成本加成或目标毛利率。')
    rate = _decimal(settings['rate_percent'])
    if not isinstance(settings['rate_percent'], str) or rate is None or rate > 1000:
        raise ValueError('定价比例须为 0～1000 的十进制百分数。')
    if settings['pricing_method'] == 'gross_margin' and rate >= 100:
        raise ValueError('目标毛利率须小于 100%。')
    if settings['material_profile'] not in PROFILES or settings['rate_profile'] not in {'demo-v1', MARKET_VERSION}:
        raise ValueError('请选择当前可用的材料假设与演示费率方案。')
    if settings['demo_confirmed'] is not True or settings['scope_confirmed'] is not True:
        raise ValueError('请确认演示费率，以及每份 STEP 根装配各一套的成套范围。')
    result = copy.deepcopy(settings)
    result['rate_percent'] = _text(rate)
    if market:
        result['material_pricing'] = validate_material_pricing(settings['material_pricing'])
    return result


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    if isinstance(value, str) and (len(value) > 80 or not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', value)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() and 0 <= number < Decimal('1e24') else None


def _text(value):
    if value is None:
        return None
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _weight(value):
    if value is None:
        return None
    rounded = value.quantize(Decimal('.00000001'), rounding=ROUND_HALF_UP)
    # Tiny positive quantities must not silently become zero kilograms.
    return _text(value if value > 0 and rounded == 0 else rounded)


def _money(value):
    return None if value is None else format(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f')


def _grade(raw, *, market=False):
    """Recognize explicit grades only; generic Steel is not a known Q235 grade."""
    if not isinstance(raw, str):
        return None
    compact = re.sub(r'[\s_\-]+', '', raw.upper())
    if market and re.fullmatch(r'(?:SPCC|DC01)', compact):
        return 'spcc'
    if re.search(r'(?:^|[^0-9])(?:SUS|SS|AISI)?316L?(?:$|[^0-9])', compact):
        return 'ss316'
    if re.search(r'(?:^|[^0-9])(?:SUS|SS|AISI)?304L?(?:$|[^0-9])', compact):
        return 'ss304'
    if re.search(r'(?:Q235[A-D]?|Q355[A-E]?|S235JR|ASTMA36|AISI1018)', compact):
        return 'carbon_steel'
    if re.search(r'(?:^|[^0-9])5052(?:H32)?(?:$|[^0-9])', compact):
        return 'al5052'
    return None


def _material(solids, fallback, *, market=False):
    values = [solid.get('material', {}) for solid in solids]
    raw, recognized_values = set(), []
    for value in values:
        if not isinstance(value, dict):
            recognized_values.append(None)
            continue
        original = value.get('value')
        if (value.get('status') == 'candidate' and isinstance(original, str)
                and original.strip() and not is_placeholder_material_name(original)):
            raw.add(original)
        if 'entries' in value:
            candidate = material_grade_candidate(value)
            grade = candidate['value'] if candidate['status'] == 'candidate' else None
            if grade:
                raw.add(grade)
            recognized_values.append(_grade(grade, market=market))
        else:
            # Historical scalar-only candidates have no assignment entries.
            # Never use this compatibility path to bypass a failed full record.
            recognized_values.append(_grade(original, market=market)
                if value.get('status') == 'candidate' and not is_placeholder_material_name(original) else None)
    raw = sorted(raw)
    recognized = (recognized_values[0] if recognized_values and all(recognized_values)
                  and len(set(recognized_values)) == 1 else None)
    profile = recognized or fallback
    basis = ('CAD 材料牌号原文 ' + ' / '.join(raw)[:1000] + '；密度和价格仍采用本次演示表。' if recognized else
             '材料未完整确认；本次按所选 ' + PROFILES[profile]['name'] + ' 估重及演示计价。')
    if market:
        basis = ('CAD 材料牌号原文 ' + ' / '.join(raw)[:1000] + '；密度按材料基准估算，材料价格来源另行记录。' if recognized else
                 '材料未完整确认；本次按所选 ' + PROFILES[profile]['name'] + ' 估重，采用价为本单明确价格假设。')
    return (' / '.join(raw)[:1000] or None, profile, basis, bool(recognized))


def _groups(solids):
    groups = defaultdict(list)
    for index, solid in enumerate(solids):
        definition = solid.get('definition_id')
        if isinstance(definition, str) and definition.strip():
            key = re.sub(r'/solid-[0-9]+$', '', definition)
        else:
            # No identity is invented for an unidentified body. Its quantity
            # stays unknown even though one visible body happens to exist.
            key = '@unresolved-' + str(index)
        groups[key].append(solid)
    return groups


def _quantity_and_volume(solids):
    instances = defaultdict(list)
    notes = []
    for solid in solids:
        definition, occurrence = solid.get('definition_id'), solid.get('occurrence_path')
        if (not isinstance(definition, str) or not definition.strip()
                or not isinstance(occurrence, list) or not occurrence
                or not all(isinstance(item, str) and item.strip() for item in occurrence)):
            return None, None, ['原生定义或装配实例路径不完整，数量和单件体积未自动推定。']
        instances[tuple(occurrence)].append(solid)
    components, instance_volumes = None, []
    for bodies in instances.values():
        identities = [body['definition_id'] for body in bodies]
        if len(set(identities)) != len(identities):
            return None, None, ['同一装配实例的实体定义重复，未自动重复计数。']
        current = sorted(identities)
        if components is not None and current != components:
            return None, None, ['同一产品定义的各实例包含不同实体，数量待核对。']
        components = current
        values = [_decimal(body.get('volume_mm3')) for body in bodies]
        if any(value is None or value <= 0 for value in values):
            return len(instances), None, ['部分实体没有正的原生体积，未估重或计价。']
        instance_volumes.append(sum(values, ZERO))
    if not instance_volumes:
        return None, None, ['未读取到可汇总的原生实体。']
    if any(abs(value - instance_volumes[0]) > max(Decimal('.000001'), instance_volumes[0] * Decimal('.0000001'))
           for value in instance_volumes[1:]):
        return None, None, ['同一产品定义的各实例体积不一致，未按一种零件自动计价。']
    return len(instances), sum(instance_volumes, ZERO) / len(instances), notes


def _base_line(document, index):
    return {'id': f'L{index:04d}', 'document': document.get('relative_path') or document.get('name') or '未命名文件',
            'file_id': document.get('id'), 'part_name': document.get('name') or '未命名文件',
            'mode': 'unresolved', 'quantity_per_set': None, 'quantity_total': None,
            'material_raw': None, 'material_assumption': None, 'density_kg_m3': None,
            'volume_mm3': None, 'estimated_net_weight_kg': None, 'estimated_set_net_weight_kg': None,
            'estimated_total_net_weight_kg': None, 'weight_basis': '没有足够的原生几何依据，未编造重量。',
            'confidence': 'unavailable', 'material_usage_kg': None,
            **{key: None for key in (*AMOUNT_KEYS, 'unit_cost', 'unit_price')},
            'priced': False, 'notes': []}


def _failed_line(document, index, order_sets, reason):
    line = _base_line(document, index)
    line.update(quantity_per_set=1, quantity_total=order_sets,
                notes=['此行为原始根装配占位记录，内部零件尚未读取；整份文件未计价。', reason])
    return line


def _source(document, solids=None):
    public = {key: copy.deepcopy(document.get(key)) for key in ('id', 'name', 'relative_path', 'sha256', 'size', 'kind', 'status', 'error')}
    public['solid_count'] = len(solids) if solids is not None else None
    return public


def calculate_batch(documents, settings):
    """Return a scalar-only simulated quotation and its explicit assumptions."""
    settings = validate_settings(settings)
    market = settings['rate_profile'] == MARKET_VERSION
    if not isinstance(documents, list) or not documents or len(documents) > 500:
        raise ValueError('订单资料数量无效。')
    lines, source_files, limitations = [], [], []
    with localcontext() as context:
        context.prec = 64
        for document in documents:
            if not isinstance(document, dict):
                raise ValueError('订单资料记录无效。')
            if document.get('status') == 'reference' or document.get('kind') not in {None, 'step'}:
                source_files.append(_source(document))
                continue
            geometry = document.get('geometry')
            solids = geometry.get('solids') if isinstance(geometry, dict) else None
            valid_file = (document.get('status') == 'ready' and isinstance(solids, list) and bool(solids)
                          and len(solids) <= 50000 and all(isinstance(solid, dict) for solid in solids)
                          and geometry.get('output_unit') == 'mm'
                          and (not document.get('sha256') or geometry.get('source_sha256') == document['sha256']))
            if not valid_file:
                public = _source(document)
                public.update(status='failed', error='未完成原生读取，或原生数据与文件身份、单位不一致。')
                source_files.append(public)
                reason = str(document.get('error') or public['error'])[:1000]
                lines.append(_failed_line(document, len(lines) + 1, settings['order_sets'], reason))
                limitations.append((document.get('name') or '文件') + ' 未计价；原文件保留，可单独重试。')
                continue
            source_files.append(_source(document, solids))
            for definition, bodies in _groups(solids).items():
                line = _base_line(document, len(lines) + 1)
                name = str(bodies[0].get('name') or document.get('name') or '未命名部件')[:500]
                name = re.sub(r' / solid [0-9]+$', '', name)
                bought = bool(re.search(r'(?:^|[ _-])(?:M[0-9]+(?:[X×.\-]|$)|CLS(?:[ _-]|$)|DIN[ _-]?[0-9]|ISO[ _-]?[0-9]|GB/T)', name.upper())) or any(
                    term in name for term in ('螺钉', '螺栓', '螺母', '垫圈', '铆螺母'))
                count, volume, notes = _quantity_and_volume(bodies)
                raw, profile_key, material_basis, known_grade = _material(bodies, settings['material_profile'], market=market)
                profile = PROFILES[profile_key]
                price_snapshot = resolve_material_price(profile_key, raw, settings) if market and not bought else None
                material_rate = price_snapshot['normalized_net_rate'] if price_snapshot is not None else profile['rate']
                closed_valid = all(isinstance(body.get('topology'), dict)
                                   and body['topology'].get('valid') is True
                                   and body['topology'].get('closed') is True for body in bodies)
                line.update(part_name=name, mode='buy' if bought else 'make', quantity_per_set=count,
                            quantity_total=count * settings['order_sets'] if count is not None else None,
                            material_raw=raw, material_assumption=material_basis,
                            density_kg_m3=profile['density'], volume_mm3=_weight(volume), notes=notes,
                            native_definition=definition if not definition.startswith('@unresolved-') else None,
                            native_solid_count=len(bodies), material_profile=profile_key,
                            material_rate=material_rate if not bought else None)
                if price_snapshot is not None:
                    line.update(material_price_snapshot=price_snapshot,
                        material_price_original_rate=price_snapshot['adopted_rate_cny_per_kg'],
                        material_price_tax_included=price_snapshot['adopted_tax_included'],
                        material_price_vat_rate_percent=price_snapshot['adopted_vat_rate_percent'],
                        material_price_source_kind=price_snapshot['source_kind'],
                        material_price_source=price_snapshot['source_text'],
                        material_price_quote_date=price_snapshot['quote_date'],
                        material_price_reference_id=price_snapshot['reference']['id'] if price_snapshot['reference'] else None)
                    line['notes'].extend(price_snapshot['notes'])
                if volume is not None:
                    net = Decimal(_weight(volume * Decimal(profile['density']) / Decimal('1000000000')))
                    line.update(estimated_net_weight_kg=_weight(net),
                                estimated_set_net_weight_kg=_weight(net * count),
                                estimated_total_net_weight_kg=_weight(net * line['quantity_total']),
                                confidence=('low' if not closed_valid else 'cad_material' if known_grade else 'assumed_material'),
                                weight_basis='原生单件体积 × 密度 ' + profile['density'] + ' kg/m³；' + material_basis)
                if not closed_valid:
                    line['notes'].append('原生几何未通过有效且封闭的计量条件；有体积时仅粗估重量，本行未计价。')
                line['priced'] = count is not None and volume is not None and closed_valid and (bought or material_rate is not None)
                if line['priced']:
                    quantity = line['quantity_total']
                    amounts = {key: ZERO for key in AMOUNT_KEYS[:4]}
                    if bought:
                        amounts['purchase_cost'] = Decimal('.80') * quantity
                        line['notes'].append('名称规则识别为外购候选；每件 0.80 元仅为演示价。')
                    else:
                        usage = net / Decimal('.85')
                        line['material_usage_kg'] = _weight(usage)
                        amounts['material_cost'] = Decimal(_money(usage * Decimal(material_rate) * quantity))
                        amounts['process_cost'] = Decimal(_money(max(Decimal('5'), net * Decimal('8')) * quantity))
                        amounts['setup_cost'] = Decimal('30')
                    cost = sum(amounts.values(), ZERO)
                    rate = Decimal(settings['rate_percent']) / 100
                    unit_price = cost / quantity * (1 + rate) if settings['pricing_method'] == 'cost_plus' else cost / quantity / (1 - rate)
                    line.update({key: _money(value) for key, value in amounts.items()})
                    line.update(unit_cost=_money(cost / quantity), total_cost=_money(cost),
                                unit_price=_money(unit_price), total_price=_money(Decimal(_money(unit_price)) * quantity))
                lines.append(line)
        priced = [line for line in lines if line['priced']]
        totals = {key: _money(sum((Decimal(line[key]) for line in priced), ZERO)) if priced else None for key in AMOUNT_KEYS}
        totals.update(priced_lines=len(priced), unpriced_lines=len(lines) - len(priced))
        known_weights = [line for line in lines if line['estimated_total_net_weight_kg'] is not None]
        known = sum((Decimal(line['estimated_total_net_weight_kg']) for line in known_weights), ZERO) if known_weights else None
        complete = bool(lines) and len(known_weights) == len(lines)
        weight_summary = {
            'estimated_set_net_weight_kg': _weight(sum((Decimal(line['estimated_set_net_weight_kg']) for line in known_weights), ZERO)) if complete else None,
            'estimated_total_net_weight_kg': _weight(known) if complete else None,
            'known_total_net_weight_kg': _weight(known), 'missing_estimated_lines': len(lines) - len(known_weights),
            'low_confidence_lines': sum(line['confidence'] == 'low' for line in lines),
            'low_confidence_total_net_weight_kg': _weight(sum((Decimal(line['estimated_total_net_weight_kg']) for line in known_weights if line['confidence'] == 'low'), ZERO)),
        }
    if not lines:
        limitations.append('没有可计价的 STEP 文件；参考附件没有生成零件或费用。')
    if totals['unpriced_lines']:
        limitations.append(str(totals['unpriced_lines']) + ' 行尚未计价；所示金额只覆盖已计价范围，未计价不代表免费。')
    if any(document.get('status') == 'reference' or document.get('kind') not in {None, 'step'} for document in documents):
        limitations.append('附图、PDF 或其他参考文件已保留；本版未自动解释其工艺要求或将其加入费用。')
    limitations.extend([
        '成套范围为每份 STEP 根装配各一套；重复、上下级装配重叠或客户另定数量须人工核对。',
        '本版未自动识别具体激光、折弯、钻孔或焊接工艺；综合加工费只用于演示，不代表完整工艺核算。',
        '重量按 CAD 体积与明确材料密度假设估算，未经称重，不含包装；异常几何的粗估不能用于制造放行。',
        '当前不是工厂真实价目表；交期、付款条件及报价有效期待人工约定。',
    ])
    assumptions = [
        {'name': '成套范围', 'value': '每份 STEP 根装配各 1 套', 'scope': '套数 ' + str(settings['order_sets']) + '；不跨文件自动去重或合并'},
        {'name': '未知材料回退', 'value': PROFILES[settings['material_profile']]['name'], 'scope': '原文件无明确支持牌号时使用；是估重及计价假设'},
        {'name': '演示材料表', 'value': '碳钢 7850 / 6；304 7930 / 26；316 7980 / 28；5052 2680 / 20', 'scope': '依次为 kg/m³ 与元/kg；明确 CAD 牌号优先，否则使用所选材料假设'},
        {'name': '原料利用率', 'value': '85%', 'scope': '自制件采购计价量 = 预估净重 ÷ 0.85，非排版计算'},
        {'name': '综合加工演示费', 'value': 'max(5 元/件, 预估净重 × 8 元/kg)', 'scope': '自制件；未声称识别了具体工艺、工时或特殊工序'},
        {'name': '准备费', 'value': '30 元/自制分组/单', 'scope': '每个源文件内的产品定义分组一次，不按套重复计费'},
        {'name': '外购候选', 'value': '0.80 元/件', 'scope': '仅按名称规则分类并标演示价，仍须核对规格和采购价'},
        {'name': '定价', 'value': ('成本加成' if settings['pricing_method'] == 'cost_plus' else '目标毛利率') + ' ' + settings['rate_percent'] + '%', 'scope': '报价单价四舍五入到分，再乘订单件数；不从已四舍五入单件成本反算'},
    ]
    if market:
        assumptions[2] = {'name': '材料采用价', 'value': '佛山行情参考 / 供应商录入 / 手工采用价',
            'scope': '逐行保留原价、原税口径、日期、来源、采用价与换算未税价；不代表已经核验实际采购成交价。'}
        assumptions.append({'name': '采用税率假设', 'value': settings['material_pricing']['vat_rate_percent'] + '%',
            'scope': '仅含税采用价按此换算未税成本；行情原文未说明的税率与税口径仍为未知，不认定为来源税率。'})
        limitations.extend(['行情样本日期为2026-11-03或2026-11-05，不是自动更新的当日报价；本件规格与行情样本未自动匹配。',
                            '参考报价可能不含开平、分条、贴膜、出库和运输费用；这些费用没有自动加入。',
                            '只有材料采用价按本单来源调整，利用率、综合加工费、外购单价和准备费仍为演示假设。'])
    result = {'schema_version': 1, 'simulated': True, 'status': 'pending_review',
            'engineering_approved': False, 'business_approved': False,
            'tax_included': False, 'freight_included': False, 'currency': 'CNY',
            'pricing_scope': '已计价范围' if totals['unpriced_lines'] else '本次演示成套范围',
            'settings': settings, 'source_files': source_files, 'lines': lines,
            'totals': totals, 'weight_summary': weight_summary, 'assumptions': assumptions,
            'limitations': limitations, 'rounding_rule': 'Decimal ROUND_HALF_UP；分项成本到分，单价到分，行报价=报价单价×件数。'}
    if market:
        price_sources, seen = [], set()
        for line in lines:
            snapshot = line.get('material_price_snapshot')
            if snapshot and snapshot['material_profile'] not in seen:
                price_sources.append(copy.deepcopy(snapshot))
                seen.add(snapshot['material_profile'])
        result.update(price_profile=MARKET_VERSION, price_profile_label='行情参考与本单采用价 · 含演示工序费',
                      material_pricing_snapshot=copy.deepcopy(settings['material_pricing']), material_price_sources=price_sources)
        if not totals['unpriced_lines']:
            result['pricing_scope'] = '本次参考成套范围'
    if settings.get('costing_mode') == 'process-v1':
        from batch_process_quote import apply_process_costing
        return apply_process_costing(result, documents)
    return result


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    text = str(value)
    if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')):
        text = "'" + text
    return text


def _csv(result, columns):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\r\n')
    writer.writerow([name for name, key in columns])
    for line in result['lines']:
        writer.writerow([_cell(line.get(key)) for name, key in columns])
    return ('\ufeff' + stream.getvalue()).encode('utf-8')


def _esc(value, unknown='未计入'):
    return html.escape(unknown if value is None else str(value), quote=True)


def _display_number(value, unknown='未提供'):
    """Format business scalars only; stored quantities and identifiers are untouched."""
    number = _decimal(value)
    if number is None:
        return _esc(unknown)
    with localcontext() as context:
        context.prec = 64
        if 0 < number < Decimal('.01'):
            return '&lt;0.01'
        return format(number.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), ',.2f')


def _kg(value):
    measured = _decimal(value)
    if measured is None:
        return '尚无法完整估算'
    with localcontext() as context:
        context.prec = 64
        if 0 < measured < 1:
            grams = measured * 1000
            if grams < Decimal('.01'):
                return '小于 0.01 g'
            return '约 ' + format(grams.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), ',.2f') + ' g'
        return '约 ' + format(measured.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), ',.2f') + ' kg'


def _display_count(value, unknown='未提供'):
    number = _decimal(value)
    if number is None or number != number.to_integral_value():
        return _esc(unknown)
    return format(number, ',.0f')


def _display_money(value, unknown='未计价'):
    amount = _decimal(value)
    return _esc(unknown) if amount is None else format(amount, ',.2f')


def _cost_chart_html(result):
    """Only saved priced-cost totals are visualized; unknowns stay visible."""
    specs = [('material_cost', '材料费', '#2b819d'), ('process_cost', '加工费', '#657fb3'),
             ('purchase_cost', '外购费', '#78a28e'), ('setup_cost', '准备费', '#c49961')]
    def parsed(value):
        if value is None or value == '':
            return None
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None
        return number if number.is_finite() and number >= 0 else None
    def cash(value):
        return '未提供' if value is None else '¥' + format(value, ',.2f')
    with localcontext() as context:
        context.prec = 64
        totals = result['totals']
        rows = [(key, label, color, parsed(totals.get(key))) for key, label, color in specs]
        total = parsed(totals.get('total_cost'))
        known = [value for _, _, _, value in rows if value is not None]
        subtotal = sum(known, ZERO)
        missing = len(rows) - len(known)
        mismatch = total is not None and (subtotal > total or not missing and subtotal != total)
        def percent(value):
            if value is None:
                return '占比未提供'
            if mismatch:
                return '占比待核对'
            if value == 0:
                return '0.00%'
            if not total:
                return '暂无占比'
            fraction = value / total * 100
            return '小于0.01%' if fraction < Decimal('.01') else format(fraction.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f') + '%'
        bar = ''
        if total is not None and total > 0 and subtotal > 0 and not mismatch:
            for _, label, color, value in rows:
                if value is not None:
                    bar += '<span title="' + _esc(label + ' ' + cash(value)) + '" style="width:' + format(value / total * 100, '.6f') + '%;background:' + color + '"></span>'
            if subtotal < total:
                bar += '<span title="构成尚未完整提供" style="width:' + format((total-subtotal)/total*100, '.6f') + '%;background:#d4e0e7"></span>'
            label = '；'.join(name + ' ' + cash(value) + '，' + percent(value) for _, name, _, value in rows)
            bar = '<div class="cost-chart-bar" role="img" aria-label="' + _esc(label) + '">' + bar + '</div>'
        else:
            message = ('各项明细与成本合计不一致，暂不绘制占比。' if mismatch else
                       '成本合计尚未提供，已知明细保留在下方。' if known and total is None else
                       '当前没有正的已计价成本，暂无可展示的占比。' if total == 0 else
                       '尚未提供成本明细，暂无可展示的成本构成。')
            bar = '<p class="cost-chart-empty">' + message + '</p>'
        legends = ''.join('<li><span class="cost-chart-number" style="background:' + color + '">' + str(index) + '</span><div><span>' + label + '</span><strong>' + cash(value) + '</strong><small>' + percent(value) + '</small></div></li>' for index, (_, label, color, value) in enumerate(rows, 1))
        notice = '<p>明细小计 ' + cash(subtotal) + ' 与成本合计 ' + cash(total) + ' 不一致，请核对。</p>' if mismatch else '<p>' + str(missing) + ' 类成本未提供，已知明细小计 ' + cash(subtotal) + '。</p>' if missing and known else ''
        unpriced = totals.get('unpriced_lines')
        scope = str(unpriced) + ' 行未计价项目未包含在图中。' if type(unpriced) is int and unpriced > 0 else '图中仅含已计价项目。'
        return '<section class="cost-chart"><div class="cost-chart-heading"><div><h2>已计价部分的成本构成</h2><p>按成本金额展示，报价加成另计。</p></div><div>成本合计<strong>' + cash(total) + '</strong></div></div>' + bar + '<ul class="cost-chart-legend">' + legends + '</ul>' + notice + '<p class="muted">' + scope + '费用依据见报价明细。</p></section>'


def _material_sources_html(result):
    snapshots = result.get('material_price_sources')
    if not isinstance(snapshots, list) or not snapshots:
        return ''
    rows = []
    for item in snapshots:
        net = _decimal(item.get('normalized_net_rate'))
        net_display = '未提供'
        if net is not None:
            with localcontext() as context:
                context.prec = 64
                rounded = net.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                net_display = ('小于 0.01' if 0 < net < Decimal('.01') else
                               ('约 ' if rounded != net else '') + format(rounded, ',.2f'))
        reference = item.get('reference') or {}
        specification = ' / '.join((html.unescape(_display_number(reference[key])) + ' mm') if key == 'thickness_mm' else str(reference[key])
                                   for key in ('grade', 'form', 'surface', 'thickness_mm') if reference.get(key))
        original_tax = ('原行情含税，税率未注明' if reference.get('tax_included') is True else '原行情税运口径未明确') if item.get('source_kind') == 'market_reference' else '采用价来自人工录入，未核验'
        adopted_tax = '采用含税价' if item.get('adopted_tax_included') is True else '采用未税价' if item.get('adopted_tax_included') is False else '采用价未提供'
        kind = {'market_reference': '网上行情参考', 'supplier_quote': '供应商录入价（未核验）', 'manual': '手工采用价', 'missing': '未提供'}.get(item.get('source_kind'), '待核对')
        url = item.get('source_url')
        link = '<a href="' + _esc(url) + '" target="_blank" rel="noopener noreferrer">查看行情来源</a>' if isinstance(url, str) and re.match(r'^https?://', url) else ''
        rows.append('<tr><td>' + _esc(item.get('material_name') or item.get('material_profile')) + '<small>' + _esc(specification, '')
            + '</small></td><td>' + kind + '<small>' + _esc(item.get('source_text'), '') + '</small>' + link
            + '</td><td>' + _esc(item.get('quote_date')) + '</td><td>' + _display_number(item.get('original_rate'))
            + '<small>' + original_tax + '</small></td><td title="' + _esc(item.get('normalized_net_rate'), '') + '">' + _esc(net_display)
            + '<small>' + adopted_tax + '；采用税率假设 ' + _display_number(item.get('adopted_vat_rate_percent')) + '%</small></td><td>'
            + _esc('；'.join(item.get('notes') or []), '') + '</td></tr>')
    return '<section><h2>本次材料价格来源与采用口径</h2><p>金额单位：元/kg。网上行情只作同类参考，采用规格和税率假设待核对；供应商录入价不等于已核验成交价。</p><div class="table"><table><thead><tr><th>材料与参考规格</th><th>价格来源</th><th>来源日期</th><th>原采用价</th><th>未税采用价</th><th>依据与假设</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div></section>'


def material_display(line):
    """Separate recorded CAD text from adopted estimating material and rate source."""
    labels = {'carbon_steel': '碳钢（Q235类）', 'spcc': '冷轧钢板（SPCC类）',
              'ss304': '304不锈钢', 'ss316': '316类不锈钢', 'al5052': '5052铝合金',
              'stainless_mesh': '8目不锈钢纱网（牌号未定）'}
    raw = line.get('material_raw')
    raw = raw.strip() if isinstance(raw, str) else ''
    snapshot = line.get('material_price_snapshot')
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    adopted = labels.get(line.get('material_profile') or snapshot.get('material_profile'))
    if (line.get('material_profile') == 'ss304' and line.get('confidence') == 'assumed_material'
            and not raw and line.get('material_estimation_label') == '不锈钢估价（参考304）'):
        adopted = '不锈钢估价（参考304）'
    rate = line.get('material_rate') if 'material_rate' in line else snapshot.get('normalized_net_rate')
    kind = line.get('material_price_source_kind') or (snapshot.get('source_kind') if isinstance(snapshot, dict) else None)
    if line.get('mode') == 'buy':
        price = '本单指定采购价' if line.get('purchase_price_source_kind') == 'manual' else '外购件演示价'
    elif rate is None or rate == '':
        price = '暂未录价'
    else:
        price = {'market_reference': '行情参考价', 'supplier_quote': '供应商录入价',
                 'manual': '手工录入价'}.get(kind, '价格来源待核对' if kind else '演示材料价')
    engineering_adoption = bool(line.get('adopted_material_source')) and any(
        method in {'adopt_material', 'purchase_mesh'} for method in line.get('engineering_pricing_methods', []))
    return {'cad_text': raw,
            'adopted_label': '按图纸核价采用' if engineering_adoption else '估重采用' if line.get('mode') == 'buy' else '估价采用',
            'adopted_text': adopted or '暂未采用', 'price_text': price,
            'note': ('尚未采用估价材料。' if not adopted else
                     '依据图纸纱网要求按件计价，采用本单指定采购价；丝径及采购规格待核对，模型估重不等同于实际网重。' if engineering_adoption and line.get('material_profile') == 'stainless_mesh' and line.get('purchase_price_source_kind') == 'manual' else
                     '依据图纸纱网要求按件演示计价；丝径及采购规格待核对，模型估重不等同于实际网重。' if engineering_adoption and line.get('material_profile') == 'stainless_mesh' else
                     '已按图纸材料类别核价；' + price + '，供货规格与实际采购条件仍需核对。' if engineering_adoption else
                     'CAD原文已保留，具体牌号和采购规格需核对。' if raw else
                     '按本单所选材料估算，材料牌号需核对。')}


def _display_group_key(line):
    """Only well-specified bought items may share a presentation row.

    This is not a procurement identity or a CAD shape comparison. In particular,
    a generic name and matching volume do not identify the same made part.
    Missing scalar evidence remains a separate row instead of being made zero.
    """
    name = line.get('part_name')
    if (line.get('mode') != 'buy' or line.get('priced') is not True
            or line.get('confidence') not in {'cad_material', 'assumed_material'}
            or not isinstance(name, str) or not 3 <= len(name) <= 160
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _./×-]*[A-Za-z0-9]', name)
            or not re.search(r'[A-Za-z]', name) or not re.search(r'[0-9]', name)
            or not re.search(r'[-_./×]|[0-9][xX][0-9]', name)
            or re.search(r'(?i)^(?:solid|body|part|unnamed|unknown)(?:[ _.-]|[0-9]|$)', name)
            or line.get('material_profile') not in PROFILES):
        return None
    if (not isinstance(line.get('id'), str) or not re.fullmatch(r'L[0-9]{4,7}', line['id'])
            or not isinstance(line.get('file_id'), str) or not line['file_id']
            or not isinstance(line.get('document'), str) or not line['document']):
        return None
    per_set, total = line.get('quantity_per_set'), line.get('quantity_total')
    if type(per_set) is not int or type(total) is not int or per_set <= 0 or total <= 0 or total % per_set:
        return None
    unit_keys = ('volume_mm3', 'estimated_net_weight_kg', 'density_kg_m3', 'unit_cost', 'unit_price')
    numbers = [_decimal(line.get(key)) for key in unit_keys]
    summed_keys = ('estimated_set_net_weight_kg', 'estimated_total_net_weight_kg', *AMOUNT_KEYS)
    if any(value is None or value <= 0 for value in numbers) or any(_decimal(line.get(key)) is None for key in summed_keys):
        return None
    if _decimal(line['total_price']) != numbers[-1] * total:
        return None
    if (_decimal(line['estimated_set_net_weight_kg']) != numbers[1] * per_set
            or _decimal(line['estimated_total_net_weight_kg']) != numbers[1] * total):
        return None
    # Compare all retained material/price assumptions, including explicit nulls.
    # Unknown raw material may agree as an estimating assumption, never as a
    # declaration that CAD supplied a verified grade.
    evidence_keys = ('material_raw', 'material_profile', 'material_assumption', 'material_rate',
                     'material_price_snapshot', 'material_price_source_kind', 'material_price_source',
                     'material_price_original_rate', 'material_price_tax_included', 'material_price_vat_rate_percent',
                     'material_price_quote_date', 'material_price_reference_id', 'material_usage_kg',
                     'confidence', 'weight_basis', 'notes')
    evidence = [(key, key in line, line.get(key)) for key in evidence_keys]
    return (name, total // per_set, tuple(_text(value) for value in numbers),
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')))


def aggregate_display_groups(result):
    """Derive read-only rows; keep original quantities, fees and source line IDs.

    Amounts and weights are sums of the saved rows, not another pricing pass.
    CSV/JSON exports and native previews continue to refer to the original rows.
    """
    lines = result.get('lines') if isinstance(result, dict) else None
    if not isinstance(lines, list):
        return []
    ids = [line.get('id') for line in lines if isinstance(line, dict)]
    duplicates = {value for value, count in Counter(ids).items() if count > 1}
    groups, buckets = [], {}
    with localcontext() as context:
        context.prec = 64
        for line in lines:
            if not isinstance(line, dict):
                continue
            key = _display_group_key(line) if line.get('id') not in duplicates else None
            if key is None or key not in buckets:
                group = copy.deepcopy(line)
                group.update(member_ids=[line.get('id')], member_count=1,
                             source_documents=[line.get('document')], aggregated=False)
                groups.append(group)
                if key is not None:
                    buckets[key] = group
                continue
            group = buckets[key]
            group['member_ids'].append(line['id'])
            group['member_count'] += 1
            group['aggregated'] = True
            if line['document'] not in group['source_documents']:
                group['source_documents'].append(line['document'])
            for field in ('quantity_per_set', 'quantity_total'):
                group[field] += line[field]
            for field in ('estimated_set_net_weight_kg', 'estimated_total_net_weight_kg'):
                group[field] = _text(_decimal(group[field]) + _decimal(line[field]))
            for field in AMOUNT_KEYS:
                group[field] = _money(_decimal(group[field]) + _decimal(line[field]))
    return groups


def _preview_cell(line, previews):
    """Present a verified read-only association without changing quote data."""
    preview = previews.get(line['id'], {})
    source = preview.get('geometry_url')
    valid = (isinstance(source, str) and bool(re.fullmatch(
        r'(?:/workbench/api|/ortho2cad-test/quote-workbench/api)/quote-batches/BATCH-[a-f0-9]{32}/runs/RUN-[a-f0-9]{32}/parts/L[0-9]{4,6}'
        r'|/ortho2cad-test/customer-cases/[A-Za-z0-9_-]+/parts/L[0-9]{4,6}\.json', source)))
    if (preview.get('status') != 'ready' or not valid
            or source.rsplit('/', 1)[-1] not in {line['id'], line['id'] + '.json'}):
        return '<td class="part-preview-cell"><span class="part-unavailable">暂无预览</span></td>'
    thumbnail = preview.get('thumbnail_url')
    allowed_thumbnail = source + '/thumbnail' if not source.endswith('.json') else source[:-5] + '/thumbnail.png'
    image = ('<img loading="lazy" src="' + _esc(thumbnail) + '" alt="' + _esc(line['part_name']) + ' 的原生零件预览">'
             if isinstance(thumbnail, str) and thumbnail == allowed_thumbnail else '<span class="part-placeholder">3D</span>')
    url = '/workbench/quotation/part/?preview=' + quote(source, safe='')
    return ('<td class="part-preview-cell"><a class="part-preview-link" href="' + _esc(url)
            + '" target="_blank" rel="noopener noreferrer">' + image + '<span>查看零件 ↗</span></a></td>')


def _quote_html(result, part_previews=None):
    totals, weights = result['totals'], result['weight_summary']
    amount = '尚无可计价金额' if totals['total_price'] is None else '¥' + _display_money(totals['total_price'])
    previews = {row['line_id']: row for row in (part_previews or [])
                if isinstance(row, dict) and isinstance(row.get('line_id'), str)}
    rows = []
    groups = aggregate_display_groups(result)
    originals = {line['id']: line for line in result['lines']}
    for line in groups:
        notes = '；'.join(line.get('notes') or [])
        material = material_display(line)
        cad_html = ('<span>CAD材料</span><strong>' + _esc(material['cad_text']) + '</strong>') if material['cad_text'] else ''
        material_evidence = ('<div class="material-basis">' + cad_html + '<span>' + _esc(material['adopted_label']) + '</span><strong>' + _esc(material['adopted_text'])
                         + '</strong><span>价格来源</span><strong>' + _esc(material['price_text'])
                         + '</strong><small>' + _esc(material['note']) + '</small></div>')
        material_html = ('<div class="material-summary"><strong>' + _esc(material['adopted_text'])
                         + '</strong><small>' + _esc(material['adopted_label']) + ' · 价格来源：'
                         + _esc(material['price_text']) + '</small></div>')
        preview_cell = _preview_cell(line, previews) if part_previews is not None else ''
        note_html = ('<details class="part-notes"><summary>查看依据</summary>' + material_evidence
                     + '<p>' + _esc(line.get('weight_basis'), '') + '</p><p>' + _esc(notes, '') + '</p></details>')
        origin = (str(line['member_count']) + ' 条来源 · 汇总显示' if line['aggregated'] else str(line['document']) + ' · ' + str(line['id']))
        if line['aggregated']:
            source_rows = []
            for member_id in line['member_ids']:
                member = originals[member_id]
                source_rows.append('<tr data-source-line="' + _esc(member_id) + '">'
                    + (_preview_cell(member, previews) if part_previews is not None else '')
                    + '<td>' + _esc(member['document']) + '<small>' + _esc(member_id) + '</small></td>'
                    + '<td>' + _display_count(member['quantity_per_set']) + '</td><td>' + _display_count(member['quantity_total'])
                    + '</td><td>' + _kg(member['estimated_total_net_weight_kg']) + '</td><td>'
                    + _display_money(member['total_price']) + '</td></tr>')
            note_html += ('<details class="group-sources"><summary>查看 ' + str(line['member_count'])
                          + ' 条来源明细</summary><p>按原记录相加，准备费及计价依据保留。</p>'
                          + '<div class="table"><table><thead><tr>' + ('<th>原件</th>' if part_previews is not None else '')
                          + '<th>来源</th><th>每套</th><th>订单量</th><th>订单估重</th><th>报价金额</th></tr></thead><tbody>'
                          + ''.join(source_rows) + '</tbody></table></div></details>')
        rows.append('<tr data-quote-group="' + _esc(line['id']) + '" data-member-count="' + str(line['member_count']) + '">'
                    + preview_cell + '<td>' + _esc(line['part_name']) + '<small>' + _esc(origin) + '</small></td><td class="quantity">'
                    + _display_count(line['quantity_total'], '待核对') + '</td><td>' + material_html
                    + '</td><td class="numeric">' + _kg(line['estimated_net_weight_kg']) + '</td><td class="numeric">' + _kg(line['estimated_total_net_weight_kg'])
                    + '</td><td class="numeric">' + _display_money(line['unit_price'], '未提供' if line['priced'] else '未计价') + '</td><td class="numeric quote-price" data-raw-amount="' + _esc(line['total_price'], '') + '">' + _display_money(line['total_price'])
                    + ('' if line['priced'] else '<small>未计价，不代表免费</small>') + '</td><td>' + note_html + '</td></tr>')
    sources = ''.join('<tr><td>' + _esc(doc.get('relative_path') or doc.get('name')) + '</td><td>'
                      + _esc({'ready': '已读取', 'failed': '未完成', 'reference': '参考附件'}.get(doc.get('status'), doc.get('status')))
                      + '</td><td>' + _display_count(doc.get('solid_count'), '—') + '</td></tr>' for doc in result.get('source_files', []))
    assumptions = ''.join('<tr><th>' + _esc(item['name']) + '</th><td>' + _esc(item['value']) + '</td><td>'
                          + _esc(item['scope']) + '</td></tr>' for item in result['assumptions'])
    limitations = ''.join('<li>' + _esc(item) + '</li>' for item in result['limitations'])
    cost_rows = ''.join('<div><span>' + label + '</span><strong>' + _display_money(totals[key], '未提供') + '</strong></div>' for label, key in (
        ('材料成本', 'material_cost'), ('工序加工费（演示）' if result.get('settings', {}).get('costing_mode') == 'process-v1' else '综合加工演示费', 'process_cost'), ('外购成本', 'purchase_cost'), ('一次性准备费', 'setup_cost'), ('成本合计', 'total_cost')))
    settings = result['settings']
    unpriced_groups = sum(not line.get('priced') for line in groups)
    order_label = _display_count(settings.get('order_sets'), '—') + ' 套合计'
    rate_basis = (_esc(result['price_profile_label']) + '；未经工厂核价。' if result.get('price_profile_label') else '采用显式演示费率，未经工厂核价。')
    known = ('<p>已知重量小计 ' + _kg(weights['known_total_net_weight_kg']) + '；' + str(weights['missing_estimated_lines']) + ' 行未纳入完整估重。</p>') if weights['missing_estimated_lines'] else ''
    low = ('<p>' + str(weights['low_confidence_lines']) + ' 行异常几何仅作粗估，合计 ' + _kg(weights['low_confidence_total_net_weight_kg']) + '。</p>') if weights['low_confidence_lines'] else ''
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>' + _esc(result.get('name') or '整单模拟报价') + '</title><style>'
            '*{box-sizing:border-box}body{margin:0;background:#f3f6f8;color:#243b4b;font:14px/1.65 system-ui,sans-serif}'
            'main{max-width:1280px;margin:auto;padding:28px}header,section{background:white;border:1px solid #dce5eb;border-radius:12px;padding:24px;margin:16px 0}'
            'h1{margin:4px 0;font-size:26px}h2{font-size:18px;margin-top:0}p{margin:8px 0}.tag{font-size:12px;color:#986321;background:#fff4df;border-radius:5px;padding:4px 8px}'
            '.links{display:flex;gap:10px;flex-wrap:wrap}.links a{color:#176c8c;border:1px solid #c4d7e2;border-radius:6px;padding:7px 12px;text-decoration:none}'
            '.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}.metrics strong{display:block;font-size:24px}.muted,small{color:#6f8290}'
            '.table{overflow:auto}table{border-collapse:collapse;width:100%;font-size:12px}th,td{text-align:left;padding:10px;border-bottom:1px solid #dce5eb;vertical-align:top;overflow-wrap:anywhere}'
            'th{background:#f6f9fb}small{display:block;max-width:300px}.costs{max-width:480px}.costs>div{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #e4ebf0}'
            '.quote-lines{min-width:1080px}.quote-lines td.numeric,.quote-lines td.quantity{white-space:nowrap;overflow-wrap:normal;font-variant-numeric:tabular-nums}.quote-lines td.numeric small{white-space:normal;max-width:115px}'
            '.quote-lines td{min-width:85px}.quote-lines td:first-child{min-width:100px}.part-preview-cell{width:110px}.part-preview-link{display:flex;align-items:center;flex-direction:column;gap:5px;color:#176c8c;text-decoration:none;font-size:11px}.part-preview-link img,.part-placeholder{display:grid;place-items:center;width:88px;height:70px;object-fit:contain;border:1px solid #dde8ee;border-radius:7px;background:#f7fafc}.part-preview-link:hover img{border-color:#4593b1}.part-unavailable{font-size:11px;color:#8999a3}.part-notes{min-width:170px;max-width:300px}.part-notes summary{cursor:pointer;color:#33718b;font-size:11px}.part-notes p{font-size:11px;line-height:1.8}'
            '.material-basis{min-width:155px;max-width:240px}.material-basis>span{display:block;font-size:10px;color:#7e919f;margin-top:7px}.material-basis>span:first-child{margin-top:0}.material-basis>strong{font-size:12px;font-weight:500;display:block}.material-basis>small{margin-top:7px;font-size:10px}'
            '.group-sources{margin-top:12px}.group-sources>summary{cursor:pointer;color:#176c8c;font-size:12px;font-weight:600}.group-sources p{font-size:10px;color:#6f8290}.group-sources table{font-size:11px}.quote-lines .group-sources td{min-width:65px}.group-sources .part-preview-link img{width:56px;height:44px}.group-sources .part-preview-cell{width:68px}'
            '.quote-lines{font-size:13px}.quote-lines th{font-weight:600;color:#334d5e}.quote-lines td{vertical-align:middle;padding:10px}.quote-lines td.numeric,.quote-lines td.quantity{text-align:right}.quote-lines td.quote-price{font-weight:650;color:#204d68}.quote-lines td:first-child{min-width:90px}.part-preview-link img,.part-placeholder{width:72px;height:55px}.part-preview-cell{width:90px}.material-summary{min-width:155px;max-width:230px}.material-summary>strong{font-size:13px;font-weight:500;color:#334d5e}.material-summary>small{margin-top:4px;color:#627887;font-size:11px}.part-notes{min-width:100px}.part-notes>summary{font-size:12px}.part-notes .material-basis{margin-top:10px}.quote-source-stats{margin:8px 0 15px;color:#627887;font-size:12px}.quote-source-stats>summary{cursor:pointer}.quote-source-stats p{margin:6px 0}.quote-item-summary{font-weight:600;color:#334d5e}.common-basis{padding:12px 14px;background:#f5f9fb;border-radius:7px;color:#536b7d;font-size:12px}.group-sources .numeric{text-align:right}.quote-lines [data-member-count]>td{border-color:#e2e9ee}'
            '.cost-chart-heading{display:flex;justify-content:space-between;gap:20px}.cost-chart-heading p{color:#6f8290}.cost-chart-heading strong{display:block;font-size:22px}'
            '.cost-chart-bar{height:26px;display:flex;overflow:hidden;border-radius:6px;background:#e7eef2;margin:22px 0}.cost-chart-bar span{display:block;height:100%}'
            '.cost-chart-legend{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;list-style:none;margin:18px 0;padding:0}.cost-chart-legend li{display:flex;gap:9px}'
            '.cost-chart-number{display:inline-flex;align-items:center;justify-content:center;flex:none;width:22px;height:22px;border-radius:4px;color:white;font-size:12px}.cost-chart-legend strong{display:block;font-size:19px;font-variant-numeric:tabular-nums}.cost-chart-empty{background:#f3f7fa;padding:18px;border-radius:6px}'
            '@media(max-width:640px){main{padding:10px}header,section{padding:16px}.metrics{grid-template-columns:1fr}.metrics strong{font-size:22px}.cost-chart-legend{grid-template-columns:repeat(2,minmax(0,1fr))}.cost-chart-heading{flex-direction:column;gap:4px}}'
            '@media print{body{background:white}main{padding:0;max-width:none}.links{display:none}header,section{border:0;border-radius:0;padding:10px 0}.table{overflow:visible}table{font-size:9px}th,td{padding:5px}thead{display:table-header-group}}'
            '</style></head><body><main><header><span class="tag">模拟报价草稿 · 待工程与商务审核</span><h1>'
            + _esc(result.get('name') or '整单模拟报价') + '</h1><p>' + _display_count(settings['order_sets']) + ' 套 · 人民币 · 未税 · 不含运费</p>'
            '<p class="muted">' + rate_basis + '</p><nav class="links"><a href="engineering-bom.html">工程 BOM</a><a href="materials.html">备料汇总</a><a href="processes.html">工艺明细</a><a href="preview-summary.html">报价物料汇总</a><a href="preview-source.html">预览来源明细</a><a href="quote-print.html" target="_blank" rel="noopener noreferrer">预览打印版</a><a href="preview-cost.html">预览成本明细</a><a href="preview-record.html">预览报价记录</a></nav></header>'
            '<section><div class="metrics"><div><span>' + _esc(result['pricing_scope']) + '报价</span><strong data-total-price="' + _esc(totals['total_price'], '') + '">' + amount + '</strong></div><div><span>每套预估净重</span><strong>'
            + _kg(weights['estimated_set_net_weight_kg']) + '</strong></div><div><span>整单预估净重</span><strong>' + _kg(weights['estimated_total_net_weight_kg'])
            + '</strong></div></div>' + known + low + '<p class="muted">重量按 CAD 体积与明确密度假设估算，不含包装，未经称重。</p></section>' + _cost_chart_html(result) +
            '<section><h2>报价与物料明细</h2><p class="quote-item-summary">' + _display_count(settings.get('order_sets'), '—') + ' 套 · ' + str(len(groups))
            + ' 项物料 · ' + str(unpriced_groups) + ' 项未计价</p>'
            + '<details class="quote-source-stats"><summary>查看来源口径 · ' + str(len(result['lines'])) + ' 条来源记录</summary><p>'
            + str(totals['priced_lines']) + ' 行已演示计价；' + str(totals['unpriced_lines']) + ' 行未计价。未知或未计价项目不代表免费。</p><p>'
            + str(len(groups)) + ' 个汇总条目 · ' + str(len(result['lines'])) + ' 条原始明细；相同规格及估价条件的外购候选汇总显示，可展开查看来源。自制件及条件不一致项分别保留。</p></details>'
            + '<p class="common-basis">材料按本单采用条件估价；重量为原生体积与密度假设的计算值。统一价格假设见表后，逐项依据可展开查看。'
            + ('点击缩略图查看原生 3D。' if part_previews is not None else '') + '</p>'
            + '<div class="table"><table class="quote-lines"><thead><tr>' + ('<th>零件预览</th>' if part_previews is not None else '')
            + '<th>物料 / 来源</th><th>' + order_label + '</th><th>材料与计价依据</th><th>预估单重</th><th>预估总重</th><th>单价（元）</th><th>金额（元）</th><th>依据</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></div></section><section><h2>成本汇总</h2><div class="costs">' + cost_rows
            + '</div></section>' + _material_sources_html(result) + '<section><h2>输入资料</h2><div class="table"><table><thead><tr><th>源文件</th><th>读取状态</th><th>原生实体</th></tr></thead><tbody>'
            + sources + '</tbody></table></div></section><section><h2>演示假设与计价口径</h2><div class="table"><table><thead><tr><th>条件</th><th>采用值</th><th>适用范围</th></tr></thead><tbody>'
            + assumptions + '</tbody></table></div></section><section><h2>范围与待核对事项</h2><ul>' + limitations
            + '</ul></section></main></body></html>').encode('utf-8')


def _summary_bom_csv(result):
    """A derived grouped export; original BOM and saved result stay byte-stable."""
    rows = []
    for group in aggregate_display_groups(result):
        material = material_display(group)
        row = copy.deepcopy(group)
        row.update(material_adopted_label=material['adopted_label'],
                   material_adopted_text=material['adopted_text'])
        rows.append(row)
    columns = [('汇总编号', 'id'), ('物料名称', 'part_name'), ('候选分类', 'mode'),
               ('材料采用口径', 'material_adopted_label'), ('采用材料', 'material_adopted_text'),
               ('每套用量', 'quantity_per_set'), ('订单总件数', 'quantity_total'),
               ('单件预估净重_kg', 'estimated_net_weight_kg'), ('订单预估净重_kg', 'estimated_total_net_weight_kg'),
               ('报价单价_CNY', 'unit_price'), ('报价金额_CNY', 'total_price'),
               ('来源行数', 'member_count'), ('来源物料编号', 'member_ids'), ('来源文件', 'source_documents')]
    return _csv({'lines': rows}, columns)


def _quote_print_html(result, part_previews=None):
    """Readable internal print view over existing scalar data and safe thumbnails."""
    groups = aggregate_display_groups(result)
    totals, weights, settings = result['totals'], result['weight_summary'], result['settings']
    unpriced = [group for group in groups if not group.get('priced')]
    previews = {row['line_id']: row for row in (part_previews or [])
                if isinstance(row, dict) and isinstance(row.get('line_id'), str)}
    rows, source_rows, issue_rows = [], [], []
    originals = {line['id']: line for line in result['lines']}
    for group in groups:
        material = material_display(group)
        thumbnail = _preview_cell(group, previews).replace('loading="lazy"', 'loading="eager"') if part_previews is not None else ''
        source_label = _esc(group['id']) + (' · ' + str(group['member_count']) + ' 条来源' if group['aggregated'] else '')
        rows.append('<tr data-print-group="' + _esc(group['id']) + '" data-member-count="' + str(group['member_count']) + '">'
                    + thumbnail + '<td class="part"><strong>' + _esc(group['part_name']) + '</strong><small>' + source_label + '</small></td>'
                    + '<td>' + _esc(material['adopted_text']) + '</td><td class="number">' + _display_count(group['quantity_per_set'], '待核对')
                    + '</td><td class="number">' + _display_count(group['quantity_total'], '待核对') + '</td><td class="number">'
                    + _kg(group['estimated_net_weight_kg']) + '</td><td class="number">' + _kg(group['estimated_total_net_weight_kg'])
                    + '</td><td class="number">' + _display_money(group['unit_price'], '未提供' if group['priced'] else '未计价') + '</td><td class="number amount" data-raw-amount="'
                    + _esc(group['total_price'], '') + '">' + _display_money(group['total_price']) + '</td></tr>')
        if group['aggregated']:
            for identifier in group['member_ids']:
                member = originals[identifier]
                source_rows.append('<tr data-print-source="' + _esc(identifier) + '"><td>' + _esc(group['part_name'])
                                   + '</td><td>' + _esc(identifier) + '</td><td>' + _esc(member['document'])
                                   + '</td><td class="number">' + _display_count(member['quantity_total'], '待核对')
                                   + '</td><td class="number">' + _display_money(member['total_price']) + '</td></tr>')
        if not group.get('priced'):
            issue_rows.append('<tr><td>' + _esc(group['id']) + '</td><td>' + _esc(group['part_name'])
                              + '</td><td>' + _esc(group.get('document')) + '</td><td>'
                              + _esc('；'.join(group.get('notes') or []), '需核对资料和计价条件') + '</td></tr>')
    assumptions = ''.join('<tr><th>' + _esc(item.get('name')) + '</th><td>' + _esc(item.get('value'))
                          + '</td><td>' + _esc(item.get('scope')) + '</td></tr>' for item in result.get('assumptions', []))
    limitations = ''.join('<li>' + _esc(item) + '</li>' for item in result.get('limitations', []))
    costs = ''.join('<div><span>' + name + '</span><strong>' + ('¥' + _display_money(totals.get(key)) if _decimal(totals.get(key)) is not None else '未提供') + '</strong></div>'
                    for name, key in [('材料', 'material_cost'), ('加工', 'process_cost'), ('外购', 'purchase_cost'), ('准备费', 'setup_cost'), ('成本合计', 'total_cost')])
    source_table = ('<section class="appendix-block"><h2>汇总物料的来源明细</h2><table><thead><tr><th>物料</th><th>原编号</th><th>来源文件</th><th>订单件数</th><th>原行金额（元）</th></tr></thead><tbody>'
                    + ''.join(source_rows) + '</tbody></table></section>') if source_rows else ''
    issue_table = ('<section class="appendix-block"><h2>未计价项目</h2><p>以下项目保留在物料表中，未作为零成本计入报价。</p><table><thead><tr><th>编号</th><th>物料</th><th>来源</th><th>待核对依据</th></tr></thead><tbody>'
                   + ''.join(issue_rows) + '</tbody></table></section>') if issue_rows else ''
    # Source evidence remains accessible without repeating the same assumptions
    # in every material cell or pretending that a selected profile is CAD text.
    raw_rows = ''.join('<tr><td>' + _esc(group['id']) + '</td><td>' + _esc(group['part_name']) + '</td><td>'
                       + _esc(material_display(group)['cad_text']) + '</td></tr>' for group in groups if material_display(group)['cad_text'])
    raw_table = ('<section class="appendix-block"><h2>CAD 材料原文</h2><table><thead><tr><th>编号</th><th>物料</th><th>原文</th></tr></thead><tbody>'
                 + raw_rows + '</tbody></table></section>') if raw_rows else ''
    identity = result.get('run_id')
    identity = '<span>报价记录：' + _esc(identity) + '</span>' if isinstance(identity, str) and identity else ''
    generated = result.get('created_at') or result.get('generated_at')
    identity += '<span>记录时间：' + _esc(generated) + '</span>' if isinstance(generated, str) and generated else ''
    back = 'quote-visual.html' if any(str(item.get('geometry_url', '')).startswith('/ortho2cad-test/customer-cases/') for item in (part_previews or []) if isinstance(item, dict)) else 'quote.html'
    price_scope = '已计价范围报价' if unpriced else '参考报价总额'
    price_text = '¥' + _display_money(totals.get('total_price')) if _decimal(totals.get('total_price')) is not None else '尚无可计价金额'
    return ('<!doctype html><html lang="zh-CN" data-print-layout="A4-landscape"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>内部参考报价单 · ' + _esc(result.get('name') or '整单报价') + '</title><style>'
            '*{box-sizing:border-box}body{margin:0;color:#1e293b;background:#eef2f5;font:10.5pt/1.5 Arial,"Noto Sans CJK SC",sans-serif}'
            '.print-toolbar{max-width:277mm;margin:14px auto;padding:12px 16px;background:#fff;border:1px solid #cbd5e1;display:flex;justify-content:space-between;gap:16px;font-size:10pt}.print-toolbar a{color:#075985}main{max-width:277mm;margin:0 auto 20px;padding:12mm 10mm;background:#fff}'
            'header{border-bottom:2px solid #334155;padding-bottom:4mm;margin-bottom:4mm}h1{font-size:22pt;margin:0 0 2mm}h2{font-size:13pt;margin:0 0 3mm}p{margin:2mm 0}.case-name{font-size:11pt;font-weight:600;overflow-wrap:anywhere}.status{display:inline-block;border:1px solid #92400e;color:#78350f;padding:1mm 2mm;margin-bottom:2mm;font-size:9pt}'
            '.identity{display:flex;gap:5mm;flex-wrap:wrap;font-size:8pt;color:#475569}.summary{display:grid;grid-template-columns:1.5fr 1fr 1fr;gap:5mm;margin:4mm 0}.summary>div{padding:3mm;border:1px solid #cbd5e1}.summary span{display:block;color:#475569;font-size:9pt}.summary strong{display:block;font-size:17pt;font-variant-numeric:tabular-nums}.summary small{display:block;font-size:9pt;color:#475569}.scope{font-size:9pt;color:#334155;margin-bottom:4mm}'
            'table{border-collapse:collapse;width:100%;table-layout:fixed;font-size:10pt}th,td{border-bottom:1px solid #cbd5e1;padding:2mm 1.6mm;vertical-align:middle;overflow-wrap:anywhere}th{text-align:left;background:#f1f5f9;font-size:9pt;color:#334155}td small{display:block;color:#475569;font-size:8pt;font-weight:400}.number{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.amount{font-weight:600}.part strong{font-size:10pt;font-weight:600}.bom th:nth-last-child(-n+6){text-align:right}'
            '.part-preview-cell{width:13mm}.part-preview-link{display:block;pointer-events:none}.part-preview-link img{display:block;width:11mm;height:9mm;object-fit:contain}.part-preview-link span,.part-unavailable{display:none}.part-placeholder{font-size:8pt;color:#64748b}.appendix{margin-top:8mm}.appendix-block{margin:0 0 6mm}.appendix table{font-size:9pt}.appendix td{padding:1.8mm}.costs{display:flex;gap:4mm;flex-wrap:wrap;margin-bottom:5mm}.costs>div{min-width:32mm;padding:2mm 3mm;background:#f1f5f9}.costs span{display:block;font-size:9pt}.costs strong{font-size:11pt}.limitations{padding-left:5mm;font-size:9pt}li{margin-bottom:1.5mm}.common-note{font-size:9pt;color:#334155}.print-footer{margin-top:5mm;border-top:1px solid #cbd5e1;padding-top:2mm;font-size:9pt;color:#475569}'
            '@page{size:A4 landscape;margin:12mm 10mm 14mm;@bottom-left{content:"内部参考 · 待工程与商务审核";font-size:8pt;color:#475569}@bottom-right{content:"第 " counter(page) " 页 / 共 " counter(pages) " 页";font-size:8pt;color:#475569}}'
            '@media print{body{background:#fff}main{max-width:none;margin:0;padding:0}.print-toolbar{display:none}thead{display:table-header-group}tr{break-inside:avoid;page-break-inside:avoid}header,.summary,h2{break-inside:avoid}h2{break-after:avoid}.appendix{break-before:page;margin-top:0}.appendix-block{break-inside:auto}a{color:inherit;text-decoration:none}}'
            '</style></head><body><nav class="print-toolbar"><a href="' + back + '">返回报价页</a><span>打印版 · A4 横向 · 使用浏览器打印或另存为 PDF</span></nav><main>'
            '<header><span class="status">模拟报价草稿 · 待工程与商务审核</span><h1>内部参考报价单</h1><p class="case-name">'
            + _esc(result.get('name') or '整单报价') + '</p><div class="identity">' + identity + '</div></header>'
            '<section class="summary"><div><span>' + price_scope + '</span><strong data-total-price="' + _esc(totals.get('total_price'), '') + '">'
            + price_text + '</strong><small>人民币 · 未税 · 不含运费</small></div><div><span>订单范围</span><strong>'
            + _display_count(settings.get('order_sets'), '待核对') + ' 套</strong><small>' + str(len(groups)) + ' 项物料 · ' + str(len(unpriced))
            + ' 项未计价</small></div><div><span>整单预估净重</span><strong>' + _kg(weights.get('estimated_total_net_weight_kg'))
            + '</strong><small>不含包装 · 未经称重</small></div></section><p class="scope">数量按本单成套范围汇总；材料为本单估价或估重采用类别。单重、总重均为预估值，价格及费用假设见附页。'
            + ('未计价项目未包含在上述金额中。' if unpriced else '') + '</p>'
            '<table class="bom"><colgroup>' + ('<col style="width:6%">' if part_previews is not None else '')
            + '<col style="width:' + ('24' if part_previews is not None else '30') + '%"><col style="width:14%"><col style="width:6%"><col style="width:6%"><col style="width:11%"><col style="width:11%"><col style="width:11%"><col style="width:11%"></colgroup><thead><tr>'
            + ('<th>零件</th>' if part_previews is not None else '') + '<th>物料 / 原编号</th><th>采用材料</th><th>每套</th><th>总件数</th><th>预估单重</th><th>预估总重</th><th>单价（元）</th><th>金额（元）</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table><section class="appendix"><h2>核价依据与待办</h2><div class="costs">' + costs + '</div>'
            + issue_table + '<section class="appendix-block"><h2>本次采用条件</h2><table><thead><tr><th>条件</th><th>采用值</th><th>适用范围</th></tr></thead><tbody>'
            + assumptions + '</tbody></table></section>' + _material_sources_html(result) + source_table + raw_table
            + ('<section class="appendix-block"><h2>范围与待核对事项</h2><ul class="limitations">' + limitations + '</ul></section>' if limitations else '')
            + '<p class="common-note">共有 ' + str(len(result['lines'])) + ' 条原始来源记录。汇总展示只相加已保存数量、重量和金额，未删除来源或重新计算工序；原始明细和计价记录保留在下载文件中。</p>'
            + '<footer class="print-footer">工程审核：________________　商务审核：________________　审核日期：________________<br>本页为内部参考草稿，签署栏为空白；不代表已审核或已对外报价。</footer></section></main></body></html>').encode('utf-8')


def _engineering_repricing_summary(result):
    """Show the saved new-version calculation, including explicit charge scopes."""
    snapshot = result.get('engineering_repricing')
    if not isinstance(snapshot, dict) or snapshot.get('after_totals') != result.get('totals'):
        return b''
    before = snapshot.get('before_totals', {}).get('total_price')
    after = snapshot.get('after_totals', {}).get('total_price')
    delta = _decimal(after) - _decimal(before) if _decimal(before) is not None and _decimal(after) is not None else None
    delta_text = ('−' if delta is not None and delta < 0 else '') + _display_money(abs(delta) if delta is not None else None)
    methods = {'adopt_material': '按图纸材料核价', 'purchase_mesh': '纱网按件采购（演示）',
               'pipe_weld_seal': '圆管焊接与防漏（演示）', 'mesh_weld': '网片焊接（演示）',
               'hotdip': '热浸锌（演示，按整单工序组核计）'}
    rows = []
    for item in snapshot.get('applied_requirements', []):
        proof = item.get('cost_evidence', {})
        inclusions = proof.get('inclusions')
        scope = '；'.join(str(value) for value in inclusions) if isinstance(inclusions, list) else str(inclusions or proof.get('basis') or '')
        label = methods.get(item.get('method'), item.get('method'))
        if proof.get('source_kind') == 'manual':
            label = str(label).replace('演示', '本单指定采用价')
        rows.append('<tr><td>' + _esc(item.get('line_id')) + '<small>' + _esc(item.get('requirement'))
                    + '</small></td><td>' + _esc(label)
                    + '</td><td style="text-align:right;white-space:nowrap">¥' + _display_money(proof.get('amount'))
                    + '</td><td>' + _esc(scope) + '</td></tr>')
    amended = isinstance(result.get('quote_amendment'), dict)
    heading = '已采用的工程要求与费用' if amended else '按工程要求核价 · 新版本（演示）'
    totals_note = '' if amended else ('<p>原报价 ¥' + _display_money(before) + ' → 本次报价 <strong>¥' + _display_money(after)
            + '</strong>；调整 ¥' + delta_text + '。原报价和原始工程资料保留。</p>')
    return ('<section data-engineering-repricing style="border:1px solid #a8c7bc;background:#f6faf8;padding:14px 18px;margin:16px 0;break-inside:avoid">'
            '<h2 style="margin:0 0 8px;font-size:16px">' + heading + '</h2>' + totals_note
            + '<p>下表为本次采用的订单成本，报价按本单定价策略计算；行情参考、演示或本单指定采用价均待审核。</p>'
            '<div style="overflow-x:auto"><table><thead><tr><th>来源与工程要求</th><th>核算方式</th><th>订单成本（元）</th><th>包含范围 / 计算依据</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></div></section>').encode('utf-8')


def _quote_amendment_summary(result):
    snapshot = result.get('quote_amendment')
    if not isinstance(snapshot, dict) or snapshot.get('after_totals') != result.get('totals'):
        return b''
    def value_text(value, field):
        if isinstance(value, dict):
            return '；'.join(str(key) + '：' + value_text(item, field) for key, item in value.items())
        if field == 'pricing_method':
            return {'cost_plus': '成本加成', 'gross_margin': '目标毛利率'}.get(value, str(value))
        if field == 'material_profile':
            return PROFILES.get(value, {}).get('name', str(value))
        if field in {'purchase_rate', 'process_rate', 'hotdip_rate', 'hotdip_minimum', 'rate_percent'}:
            return _display_money(value)
        return str(value) if value is not None else '—'
    before, after = snapshot.get('before_totals', {}), snapshot.get('after_totals', {})
    changes = ''.join('<tr><td>' + _esc(change.get('label')) +
        ('<small>' + _esc('、'.join(change['line_ids'])) + '</small>' if change.get('line_ids') else '<small>整单</small>')
        + '</td><td>' + _esc(value_text(change.get('before'), change.get('field'))) + '</td><td>'
        + _esc(value_text(change.get('after'), change.get('field'))) + '</td></tr>' for change in snapshot.get('changes', []))
    return ('<section data-quote-amendment style="border:1px solid #adcad8;background:#f5fafc;padding:14px 18px;margin:16px 0;break-inside:avoid">'
        '<h2 style="margin:0 0 8px;font-size:16px">报价修改记录 · 新版本待审核</h2><p>修改前 ¥'
        + _display_money(before.get('total_price')) + ' → 本次报价 <strong>¥' + _display_money(after.get('total_price'))
        + '</strong>。原报价保留，以下为本次明确采用的条件。</p><table><thead><tr><th>修改条件</th><th>原值</th><th>新值</th></tr></thead><tbody>'
        + changes + '</tbody></table><p>用户指定的本单采用价不代表供应商实报；原有未修改的演示条件继续沿用，工程与商务均待审核。</p></section>').encode('utf-8')


def _engineering_pricing_notice(content, result, review, recognition):
    """Annotate an explicitly derived view; never reprice its saved snapshot."""
    summary = _quote_amendment_summary(result) + _engineering_repricing_summary(result)
    if summary:
        content = content.replace(b'</header>', b'</header>' + summary, 1)
    if review is None and recognition is None:
        return content
    from batch_engineering_bom import build_engineering_bom
    manifest = build_engineering_bom(result, aggregate_display_groups(result), review=review, recognition=recognition)
    pending = [row for row in manifest['quote_basis']['requirements_review']
               if row['status'] in {'not_adopted', 'not_separately_priced'}]
    if not pending:
        return content
    items = ''.join('<li><strong>' + _esc(row['line_id']) + ' · ' + _esc(row['part_name'])
                    + '</strong>：' + _esc(row['requirement']) + '；' + _esc(row['reason']) + '</li>' for row in pending)
    notice = ('<section data-engineering-pricing-notice style="border:1px solid #e5bc76;background:#fffaf0;padding:14px 18px;margin:16px 0;break-inside:avoid">'
              '<h2 style="margin:0 0 8px;font-size:16px">工程要求待核价</h2><p>已读取到 ' + str(len(pending))
              + ' 项尚未落实到原报价的材料或工艺要求。下方为原模拟金额，需按这些要求重新核价；未计价项不代表免费。</p><ul>'
              + items + '</ul></section>').encode('utf-8')
    return content.replace(b'</header>', b'</header>' + notice, 1)


def render_artifact(result, filename, *, part_previews=None, engineering_review=None, engineering_recognition=None):
    """Render an immutable public result without reading or writing any files."""
    if filename in {'customer-quote.html', 'customer-print.html'} or (result.get('cost_sheet') and filename in {'quote.html', 'quote-print.html'}):
        from batch_customer_quote import render_customer_quote
        return render_customer_quote(result, part_previews=part_previews,
                                     engineering_review=engineering_review, engineering_recognition=engineering_recognition,
                                     printable=filename in {'customer-print.html', 'quote-print.html'}), 'text/html; charset=utf-8'
    if filename == 'internal-cost.html':
        from batch_cost_sheet import render_cost_sheet_html
        return render_cost_sheet_html(result), 'text/html; charset=utf-8'
    if filename == 'internal-cost.csv':
        from batch_customer_quote import render_cost_sheet_csv
        return render_cost_sheet_csv(result), 'text/csv; charset=utf-8'
    if filename in {'engineering-bom.html', 'engineering-bom.csv', 'materials.html', 'processes.html'}:
        from batch_engineering_bom import render_engineering_artifact
        return render_engineering_artifact(result, filename, aggregate_display_groups(result),
                                          part_previews=part_previews, review=engineering_review, recognition=engineering_recognition)
    if filename in PREVIEW_FILES:
        kind, source = PREVIEW_FILES[filename]
        content, _ = render_artifact(result, source)
        static_case = any(isinstance(item, dict) and str(item.get('geometry_url', '')).startswith('/ortho2cad-test/customer-cases/')
                          for item in (part_previews or []))
        return render_artifact_preview(content, kind, result,
            back_href='quote-visual.html' if static_case else 'quote.html'), 'text/html; charset=utf-8'
    if filename == 'quote.json':
        return (json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8'), 'application/json; charset=utf-8'
    if filename == 'quote.html':
        return _engineering_pricing_notice(_quote_html(result, part_previews), result, engineering_review, engineering_recognition), 'text/html; charset=utf-8'
    if filename == 'quote-print.html':
        return _engineering_pricing_notice(_quote_print_html(result, part_previews), result, engineering_review, engineering_recognition), 'text/html; charset=utf-8'
    if filename == 'bom-summary.csv':
        return _summary_bom_csv(result), 'text/csv; charset=utf-8'
    common = [('物料编号', 'id'), ('源文件', 'document'), ('物料名称', 'part_name'), ('候选分类', 'mode'),
              ('每套用量', 'quantity_per_set'), ('订单总件数', 'quantity_total')]
    weights = [('单件预估净重_kg', 'estimated_net_weight_kg'), ('每套预估净重_kg', 'estimated_set_net_weight_kg'),
               ('订单预估净重_kg', 'estimated_total_net_weight_kg'), ('估重依据', 'weight_basis'),
               ('估重可信说明', 'confidence')]
    if filename == 'bom.csv':
        columns = common + [('材料原文', 'material_raw'), ('材料假设', 'material_assumption'), ('密度_kg每m3', 'density_kg_m3'),
                            ('原生单件体积_mm3', 'volume_mm3')] + weights + [('单件原料采购计价量_kg', 'material_usage_kg'), ('说明', 'notes')]
    elif filename == 'cost.csv':
        columns = common + weights + [('材料成本_CNY', 'material_cost'), ('加工成本_CNY', 'process_cost'),
                   ('外购成本_CNY', 'purchase_cost'), ('准备费_CNY', 'setup_cost'), ('单件成本_CNY', 'unit_cost'),
                   ('总成本_CNY', 'total_cost'), ('报价单价_CNY', 'unit_price'), ('报价金额_CNY', 'total_price'),
                   ('已演示计价', 'priced'), ('说明', 'notes')]
        if result.get('cost_sheet'):
            columns += [('模具成本_CNY', 'tooling_cost'), ('其他费用_CNY', 'other_cost')]
    else:
        raise ValueError('不支持的报价文件。')
    if any('material_price_snapshot' in line for line in result['lines']):
        columns += [('材料未税采用价_CNY每kg', 'material_rate'), ('材料原采用价_CNY每kg', 'material_price_original_rate'),
                    ('材料采用价含税', 'material_price_tax_included'), ('材料采用税率假设_percent', 'material_price_vat_rate_percent'),
                    ('材料价格来源类型', 'material_price_source_kind'), ('材料价格来源说明', 'material_price_source'),
                    ('材料价格来源日期', 'material_price_quote_date'), ('材料行情参考编号', 'material_price_reference_id')]
    return _csv(result, columns), 'text/csv; charset=utf-8'
