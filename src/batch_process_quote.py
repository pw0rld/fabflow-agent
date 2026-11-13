"""Process quantities drive new quotations; native mass is a parallel check.

This consumes bounded native measurement evidence, without inferring machine
routes, flattening bends, approving processes or manufacturing missing rates.
The fixed rates below are explicit demo assumptions, never factory prices.
"""
from __future__ import annotations

from collections import defaultdict
import copy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re

VERSION = 'process-v1'
DEMO_PROCESS_RATES = {
    'version': 'process-demo-20261111', 'simulated': True,
    'source': '工作台演示采用值；未经工厂费率或采购成交价核验',
    'laser_cut': {'rate': '1.20', 'unit': 'm'},
    'laser_piercing': {'rate': '0.15', 'unit': '次'},
    'bending': {'rate': '1.00', 'unit': '折'},
    'laser_setup': {'rate': '20.00', 'unit': '单'},
}
COST_KEYS = ('material_cost', 'process_cost', 'purchase_cost', 'setup_cost')
MONEY_KEYS = (*COST_KEYS, 'total_cost', 'total_price', 'unit_cost', 'unit_price')
ZERO = Decimal(0)


def number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not result.is_finite() or result < 0 or result >= Decimal('1e24') or positive and result == 0:
        return None
    return result


def text(value):
    if value is None:
        return None
    out = format(value, 'f')
    return out.rstrip('0').rstrip('.') if '.' in out else out


def money(value):
    return None if value is None else format(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f')


def scalar(value):
    return text(value.quantize(Decimal('.00000001'), rounding=ROUND_HALF_UP)) if value is not None else None


def _block(output, code, message):
    if not any(row['code'] == code for row in output['blockers']):
        output['blockers'].append({'code': code, 'message': message})


def _process(kind, label, per_piece, count, solids, evidence, *, status='estimated', per_order=False):
    rate = DEMO_PROCESS_RATES[kind]
    total = per_piece if per_order else per_piece * count
    return {'id': kind, 'kind': kind, 'label': label, 'status': status,
            'quantity_per_piece': None if per_order else text(per_piece), 'quantity_total': text(total),
            'unit': rate['unit'], 'rate_cny': rate['rate'],
            'amount_cny': money(total * Decimal(rate['rate'])),
            'rate_source': DEMO_PROCESS_RATES['version'], 'simulated': True,
            'source_solid_ids': solids, 'evidence': evidence}


def _instances(bodies):
    rows = defaultdict(list)
    for body in bodies:
        path = body.get('occurrence_path')
        if not isinstance(path, list) or not path:
            return []
        rows[tuple(path)].append(body)
    return list(rows.values())


def _metric(body):
    evidence = body.get('process_evidence')
    if not isinstance(evidence, dict) or evidence.get('schema_version') != 1 or evidence.get('solid_id') != body.get('id'):
        return None
    return evidence


def _flat_metrics(bodies):
    results = []
    for body in bodies:
        evidence = _metric(body)
        laser = evidence.get('laser_cut') if evidence else None
        if not isinstance(laser, dict) or laser.get('status') != 'ready':
            return None
        values = {key: number(laser.get(key), positive=True) for key in
                  ('thickness_mm', 'cut_length_mm', 'net_area_mm2', 'blank_width_mm', 'blank_height_mm')}
        pierces = laser.get('pierce_count')
        contours = laser.get('contour_count')
        if (any(value is None for value in values.values()) or type(pierces) is not int or not 0 < pierces <= 1024
                or type(contours) is not int or pierces != contours or not laser.get('source_face_id')):
            return None
        if values['net_area_mm2'] > values['blank_width_mm'] * values['blank_height_mm'] + Decimal('.00001'):
            return None
        results.append((body, laser, values))
    return results or None


def _metric_signature(bodies):
    flat = _flat_metrics(bodies)
    if flat is None:
        return None
    return (sum((row[2]['cut_length_mm'] for row in flat), ZERO),
            sum(row[1]['pierce_count'] for row in flat),
            sum((row[2]['net_area_mm2'] for row in flat), ZERO),
            sorted(row[2]['thickness_mm'] for row in flat))


def _requirement_text(line, bodies):
    # Check actual saved native labels/property text, never model appearance.
    values = [line.get('part_name'), line.get('material_raw')]
    for body in bodies:
        material = body.get('material')
        if isinstance(material, dict):
            values.append(material.get('value'))
            for entry in material.get('entries', [])[:64]:
                if isinstance(entry, dict):
                    values.extend(entry.get(key) for key in ('value', 'name', 'description'))
    return '；'.join(value for value in values if isinstance(value, str))[:16000]


def _line(line, bodies, settings):
    quantity = line.get('quantity_total')
    out = {'version': VERSION, 'status': 'blocked', 'scope': 'identified_processes',
           'route_review_required': True, 'source_solid_ids': [], 'processes': [], 'blockers': [],
           'features': {}, 'material': {},
           'weight_check': {'role': 'cross_check', 'status': 'unavailable',
                            'geometric_net_volume_mm3': line.get('volume_mm3'),
                            'estimated_net_weight_kg': line.get('estimated_net_weight_kg'),
                            'basis': '原生净体积与采用密度；不作为加工工序计量'},
           'estimated_cost_subtotal': None, 'estimated_price_subtotal': None}
    line['manufacturing'] = out
    # Never retain the old comprehensive weight fee on an incomplete process.
    line.update({key: None for key in MONEY_KEYS})
    line.update(priced=False, material_usage_kg=None)
    line['notes'] = [note for note in line.get('notes', []) if '0.80 元' not in note]
    line['notes'].append('按已识别工序工程量核价；材质和重量并行复核，工艺范围仍待工程审核。')
    if type(quantity) is not int or quantity <= 0 or not bodies:
        _block(out, 'native_identity_required', '原生零件身份或装配数量不完整，需先完成拆件核对。')
        return
    instances = _instances(bodies)
    if not instances or len(instances) != line.get('quantity_per_set'):
        _block(out, 'native_identity_required', '装配实例与零件数量不一致，未生成完整报价。')
        return
    representative = instances[0]
    ids = [row.get('id') for row in representative]
    out['source_solid_ids'] = ids
    if any(not isinstance(row.get('topology'), dict) or row['topology'].get('valid') is not True
           or row['topology'].get('closed') is not True for row in bodies):
        _block(out, 'invalid_native_geometry', '原生几何尚未通过有效封闭校验，需工程处理后核价。')
        return
    if line.get('mode') == 'buy':
        _block(out, 'purchase_price_required', '外购物料已拆出；需采用对应规格的采购价，未按名称套用固定单价。')
        return
    if len(representative) != 1:
        _block(out, 'manufacturing_group_required', '本产品定义包含多个实体，需确认制造拆件与装配／焊接关系。')
    flats = _flat_metrics(representative)
    costs = {key: ZERO for key in COST_KEYS}
    known = False
    if flats:
        signature = _metric_signature(representative)
        for instance in instances[1:]:
            other = _metric_signature(instance)
            if (other is None or other[1] != signature[1] or other[3] != signature[3]
                    or any(abs(other[i]-signature[i]) > max(Decimal('.00001'), signature[i]*Decimal('.000001')) for i in (0, 2))):
                _block(out, 'instance_features_conflict', '同名装配实例的工艺特征不一致，不能共用一条工艺报价。')
                return
        cut_length = sum((values['cut_length_mm'] for _, _, values in flats), ZERO)
        pierces = sum(profile['pierce_count'] for _, profile, _ in flats)
        area = sum((values['net_area_mm2'] for _, _, values in flats), ZERO)
        geometric_volume = sum((number(body.get('volume_mm3')) or ZERO for body in representative), ZERO)
        contour_volume = sum((values['net_area_mm2']*values['thickness_mm'] for _, _, values in flats), ZERO)
        mismatch = abs(geometric_volume-contour_volume)
        out['weight_check'].update(status='consistent' if mismatch <= max(Decimal('.001'), geometric_volume*Decimal('.000001')) else 'conflict',
            contour_net_volume_mm3=scalar(contour_volume), volume_difference_mm3=scalar(mismatch),
            basis='轮廓净面积×板厚与原生净体积交叉核对；密度影响材料估算，不影响工序工程量')
        if out['weight_check']['status'] == 'conflict':
            _block(out, 'volume_crosscheck_conflict', '轮廓面积与板厚对应的净体积和原生几何不一致，需复核拆件。')
        out['features'].update(cut_length_mm=scalar(cut_length), piercing_count=pierces,
            net_area_mm2=scalar(area), thickness_mm=scalar(flats[0][2]['thickness_mm']),
            blank_width_mm=scalar(flats[0][2]['blank_width_mm']), blank_height_mm=scalar(flats[0][2]['blank_height_mm']))
        face_ids = [profile['source_face_id'] for _, profile, _ in flats]
        out['processes'].extend([
            _process('laser_cut', '激光切割', cut_length/1000, quantity, ids,
                     {'basis': '原生完整外轮廓及孔槽边界总长', 'source_face_ids': face_ids,
                      'length_mm': scalar(cut_length), 'geometric': True}),
            _process('laser_piercing', '激光穿孔', Decimal(pierces), quantity, ids,
                     {'basis': '每个闭合轮廓暂按一次穿孔；非机床刀路', 'contour_count': pierces,
                      'derived_estimate': True, 'does_not_imply_drilling': True}),
            _process('laser_setup', '激光切割准备', Decimal(1), quantity, ids,
                     {'basis': '同一零件分组整单准备一次；不按套数重复'}, per_order=True)])
        costs['process_cost'] += sum((Decimal(item['amount_cny']) for item in out['processes'] if item['kind'] != 'laser_setup'), ZERO)
        costs['setup_cost'] += Decimal(out['processes'][-1]['amount_cny'])
        known = True
        density, material_rate = number(line.get('density_kg_m3'), positive=True), number(line.get('material_rate'))
        gross_volume = sum((values['blank_width_mm']*values['blank_height_mm']*values['thickness_mm'] for _, _, values in flats), ZERO)
        usage = gross_volume*density/Decimal('1000000000') if density is not None else None
        out['material'] = {'basis': '平面轮廓二维包络毛坯面积×板厚×采用密度；未排版、未计切缝或整板利用率',
                           'blank_volume_mm3': scalar(gross_volume), 'usage_kg': scalar(usage),
                           'material_source': 'native_cad' if line.get('confidence') == 'cad_material' else 'explicit_demo_assumption'}
        if usage is None or material_rate is None:
            _block(out, 'material_price_required', '毛坯规格已整理，材料采用密度或价格尚未落实。')
        else:
            line['material_usage_kg'] = scalar(usage)
            costs['material_cost'] = Decimal(money(usage*material_rate*quantity))
    else:
        _block(out, 'flat_pattern_required', '尚无可核价的平板或展开轮廓；需完成展开或指定制造工艺，不能按重量替代加工费。')
    bend_candidates = []
    bend_statuses = []
    for body in representative:
        evidence = _metric(body)
        bends = evidence.get('bends') if evidence else None
        if isinstance(bends, dict):
            bend_statuses.append(bends.get('status'))
            for candidate in bends.get('candidates', [])[:128]:
                if isinstance(candidate, dict) and number(candidate.get('length_mm'), positive=True) is not None:
                    bend_candidates.append(candidate)
    out['features']['bend_count'] = len(bend_candidates) if bend_statuses else None
    if bend_candidates:
        process = _process('bending', '折弯候选', Decimal(len(bend_candidates)), quantity, ids,
            {'basis': '原生圆柱折弯候选；方向、顺序及完整范围待工程采用',
             'candidates': copy.deepcopy(bend_candidates)}, status='pending_review')
        out['processes'].append(process)
        costs['process_cost'] += Decimal(process['amount_cny'])
        known = True
        _block(out, 'bending_adoption_required', '已取得折弯位置、角度和半径候选；折弯范围、方向与工序顺序仍需工程采用。')
    elif not bend_statuses or any(status not in {'not_applicable'} for status in bend_statuses):
        _block(out, 'bend_review_required', '折弯适用性尚未得到完整原生证据，需工程确认。')
    requirements = _requirement_text(line, representative)
    for kind, pattern, message in (
            ('welding', r'焊|weld|solder', '已读到焊接要求；需落实接头、焊缝计量及工艺费率。'),
            ('surface_treatment', r'镀锌|[侵浸]锌|喷粉|喷涂|抛光|酸洗|钝化|galvani|powder.?coat|anodi', '已读到表面处理要求；需落实处理范围、计量与费率。'),
            ('drilling_tapping', r'钻孔|攻[牙丝]|螺纹|drill|tapp', '已读到钻孔或攻牙要求；需按孔径、深度及规格核价，未与激光孔位重复收费。')):
        if re.search(pattern, requirements, re.I):
            _block(out, kind + '_required', message)
            out['processes'].append({'id': kind, 'kind': kind, 'label': {'welding':'焊接','surface_treatment':'表面处理','drilling_tapping':'钻孔／攻牙'}[kind],
                'status': 'pending_review', 'quantity_per_piece': None, 'quantity_total': None, 'unit': None,
                'rate_cny': None, 'amount_cny': None, 'source_solid_ids': ids,
                'evidence': {'basis': message, 'source_text': requirements[:2000]}})
    if known:
        subtotal = sum(costs.values(), ZERO)
        rate = Decimal(settings['rate_percent'])/100
        unit_price = subtotal/quantity*(1+rate) if settings['pricing_method']=='cost_plus' else subtotal/quantity/(1-rate)
        price = Decimal(money(unit_price))*quantity
        out.update(estimated_cost_subtotal=money(subtotal), estimated_price_subtotal=money(price))
        if out['blockers']:
            out['status'] = 'partial'
        else:
            out['status'] = 'estimated'
            line.update({key:money(value) for key,value in costs.items()})
            line.update(priced=True,total_cost=money(subtotal),unit_cost=money(subtotal/quantity),
                        unit_price=money(unit_price),total_price=money(price))


def apply_process_costing(result, documents, *, route_assumption=None):
    """Replace old estimation amounts while preserving CAD identities and mass."""
    from batch_quote_costing import _groups
    settings = result['settings']
    groups, sources = {}, {}
    for document in documents:
        geometry = document.get('geometry')
        if isinstance(geometry, dict) and isinstance(geometry.get('solids'), list):
            for definition, bodies in _groups(geometry['solids']).items():
                groups[(document.get('id'), definition)] = bodies
                sources[(document.get('id'), definition)] = geometry.get('source_sha256')
    with localcontext() as context:
        context.prec = 64
        for line in result['lines']:
            key = (line.get('file_id'), line.get('native_definition'))
            bodies = groups.get(key, [])
            _line(line, bodies, settings)
            if not line['priced'] and any(isinstance(body.get('quote_quantities'), dict) for body in bodies):
                from batch_process_estimate import estimate_line
                estimate_line(line, bodies, settings, sources.get(key), route_assumption=route_assumption)
        estimated = [line for line in result['lines'] if line['priced']]
        complete = bool(result['lines']) and len(estimated)==len(result['lines'])
        known = [line['manufacturing'] for line in result['lines'] if line['manufacturing']['estimated_cost_subtotal'] is not None]
        known_cost = sum((Decimal(row['estimated_cost_subtotal']) for row in known), ZERO) if known else None
        known_price = sum((Decimal(row['estimated_price_subtotal']) for row in known), ZERO) if known else None
        result['totals'] = {key: money(sum((Decimal(line[key]) for line in estimated), ZERO)) if estimated else None for key in COST_KEYS}
        result['totals'].update(total_cost=money(sum((Decimal(line['total_cost']) for line in estimated), ZERO)) if complete else None,
            total_price=money(sum((Decimal(line['total_price']) for line in estimated), ZERO)) if complete else None,
            priced_lines=len(estimated), unpriced_lines=len(result['lines'])-len(estimated),
            known_cost_subtotal=money(known_cost),known_price_subtotal=money(known_price))
        result['manufacturing'] = {'version':VERSION,'status':'estimated' if complete else 'partial' if known else 'blocked',
            'scope':'identified_processes','parts_count':len(result['lines']),'complete_parts':len(estimated),
            'pending_parts':len(result['lines'])-len(estimated),'process_items':sum(len(line['manufacturing']['processes']) for line in result['lines']),
            'estimated_cost_subtotal':money(known_cost),'estimated_price_subtotal':money(known_price),
            'complete':complete,'route_review_required':True,'rate_snapshot':copy.deepcopy(DEMO_PROCESS_RATES)}
    result['pricing_scope'] = '已识别工艺的演示报价草稿；工艺范围待工程审核' if complete else '工艺核价待完成；已知分项小计不代表整单报价'
    result['price_profile_label'] = '工艺工程量核价 · 演示费率 · 待审核'
    result['assumptions'] = [row for row in result.get('assumptions',[]) if row.get('name') not in
        {'综合加工演示费','准备费','外购候选','原料利用率'}]
    result['assumptions'].append({'name':'工艺核价','value':DEMO_PROCESS_RATES['version'],
        'scope':'按已识别切割长度、闭合轮廓穿孔估算及折弯候选计量；费率为明确演示采用值，工厂仍须审核。'})
    result['limitations'] = [row for row in result.get('limitations',[]) if not any(term in row for term in
        ('综合加工','只有材料采用价','行尚未计价','所示金额只覆盖'))]
    result['limitations'].extend([
        '原生孔槽按激光轮廓计入，不据此推断钻孔、攻牙或焊接工序。',
        '材质与净重作为拆件输出和几何／材料交叉复核；加工费按工艺工程量核算。',
        '平板毛坯按二维轮廓包络估算；折弯件未完成展开时不生成完整下料费或整件报价。',
        '图纸与附加工艺要求仍需工程核对，已识别工艺范围不等于全部制造要求。'])
    if any(line.get('manufacturing', {}).get('estimate_version') for line in result['lines']):
        from batch_process_estimate import VERSION as ESTIMATE_VERSION, SCOPE_LABEL
        result['manufacturing'].update(estimate_version=ESTIMATE_VERSION, scope='proposed_estimate',
                                       scope_label=SCOPE_LABEL, manufacturing_release=False,
                                       engineering_complete=False, estimate_scope_complete=complete)
        if route_assumption:
            result['manufacturing']['route_assumption'] = route_assumption
            result['assumptions'].append({'name':'钣金成形＋拼焊估算方案','value':'sheet-form-weld-demo-20261113-v1',
                'scope':'操作者明确采用的工程假设；未知路线件按每件成形及拼焊预算，不虚构折弯次数、焊长或工时；有可靠折弯候选的零件不重复叠加。'})
        result['pricing_scope'] = SCOPE_LABEL if complete else '已知工艺分项小计；尚有未核价范围'
        result['assumptions'].append({'name': '报价工程量暂估', 'value': ESTIMATE_VERSION,
            'scope': '原生几何量及明确报价代理；材料采购量按净体积与本单密度÷85%利用率。行程、穿孔和折弯按逐项证据估量，费率为保存的演示采用值。'})
        result['limitations'] = [row for row in result['limitations'] if not row.startswith('平板毛坯按二维')]
        result['limitations'].append('折弯件报价代理并非精确展开或机床刀路；需按拟定工艺范围审核估算方案，不能据此制造放行。')
        weights = [line for line in result['lines'] if number(line.get('estimated_total_net_weight_kg')) is not None]
        known_weight = sum((Decimal(line['estimated_total_net_weight_kg']) for line in weights), ZERO)
        all_weights = bool(result['lines']) and len(weights) == len(result['lines'])
        result['weight_summary'] = {
            'estimated_set_net_weight_kg': scalar(sum((Decimal(line['estimated_set_net_weight_kg']) for line in weights), ZERO)) if all_weights else None,
            'estimated_total_net_weight_kg': scalar(known_weight) if all_weights else None,
            'known_total_net_weight_kg': scalar(known_weight),
            'missing_estimated_lines': len(result['lines'])-len(weights),
            'low_confidence_lines': sum(line.get('confidence') == 'low' for line in result['lines']),
            'low_confidence_total_net_weight_kg': scalar(sum((Decimal(line['estimated_total_net_weight_kg']) for line in weights if line.get('confidence') == 'low'), ZERO))}
    return result
