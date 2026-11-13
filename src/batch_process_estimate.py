"""Source-bound quotation estimates, deliberately separate from manufacturing.

Native inventory proxies can support a reviewable, explicitly limited estimate.
They never create a flat pattern, CAM path, approved route or actual CAD grade.
All monetary arithmetic is Decimal; unknown engineering quantities remain null.
"""
from __future__ import annotations

import copy
from decimal import Decimal
import re

from batch_process_quote import (COST_KEYS, MONEY_KEYS, DEMO_PROCESS_RATES, ZERO,
                                 number, money, scalar, text, _instances,
                                 _requirement_text, _block, _process)

VERSION = 'process-estimate-20261113-v1'
MATERIAL_LABEL = '不锈钢估价（参考304）'
YIELD = Decimal('.85')
QUANTITY_KEYS = ('material_area_mm2', 'material_gross_area_mm2', 'cut_length_mm',
                 'pierce_count', 'bend_count', 'bend_length_mm', 'thickness_mm',
                 'blank_width_mm', 'blank_height_mm')
SCOPE_LABEL = '拟定工艺范围的估算方案价，待工程审核'
ROUTE_ID = 'sheet_form_weld_demo'
ROUTE_RATES = {'version': 'sheet-form-weld-demo-20261113-v1', 'simulated': True,
    'source': '明确工程假设方案的每件预算；未经工厂工时或采购成交价核验',
    'forming_budget': {'rate': '20.00', 'unit': '件'},
    'assembly_weld_budget': {'rate': '15.00', 'unit': '件'}}


def validated_quantities(body, source_sha256):
    """Accept only a native receipt bound to this body and original file."""
    from cad_quote_quantities import validate_quote_quantities
    value = body.get('quote_quantities')
    if value is None:
        return None
    topology = body.get('topology') or {}
    try:
        validate_quote_quantities(value, body.get('id'), source_sha256, topology.get('faces'),
                                  area_mm2=body.get('area_mm2'), volume_mm3=body.get('volume_mm3'))
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return None
    return copy.deepcopy(value)


def safe_quantities(value):
    """Public technical projection. No fee, cost or unbounded model text."""
    if not isinstance(value, dict):
        return None
    quantities = {key: value.get(key) for key in QUANTITY_KEYS}
    statuses = value.get('quantity_statuses') or {}
    basis = ['报价工程量包含暂估；未生成制造展开或机床刀路。']
    if value.get('method') == 'native_flat_profile':
        basis = ['按原生平面轮廓整理，穿孔次数仍为报价估算。']
    elif value.get('method') == 'native_brep_inventory':
        basis += ['面积为原生表面积的一半；行程为边界长度代理，可能含非切割分界。',
                  '材料采购量按原生净体积与本单密度及利用率假设另算。']
    measurements = value.get('measurements') or {}
    safe_measurements = {key: measurements.get(key) for key in (
        'native_skin_area_proxy_mm2', 'volume_equivalent_area_mm2', 'area_volume_thickness_ratio',
        'native_surface_area_mm2', 'native_volume_mm3') if number(measurements.get(key)) is not None}
    return {'status': value.get('status', 'unresolved'), 'method': value.get('method'),
            'quantities': quantities,
            'quantity_statuses': {key: 'estimated' if key == 'pierce_count' and quantities[key] is not None
                                 else statuses.get(key, 'unresolved') for key in QUANTITY_KEYS},
            'basis': basis, 'measurements': safe_measurements, 'manufacturing_release': False}


def _adopt_material(line, bodies, settings):
    """An estimating choice creates a new run, never rewrites CAD metadata."""
    from batch_quote_costing import _material, PROFILES
    from batch_material_prices import resolve_material_price, VERSION as MARKET_VERSION
    raw, profile, basis, known = _material(bodies, settings['material_profile'],
                                         market=settings.get('rate_profile') == MARKET_VERSION)
    stainless_hint = bool(re.search(r'不锈钢|stainless', str(line.get('document') or ''), re.I))
    assumed_stainless = not known and stainless_hint
    if assumed_stainless:
        profile, basis = 'ss304', MATERIAL_LABEL
        line['material_estimation_label'] = MATERIAL_LABEL
    else:
        line.pop('material_estimation_label', None)
    line.update(material_raw=raw, material_profile=profile, material_assumption=basis,
                density_kg_m3=PROFILES[profile]['density'], confidence='cad_material' if known else 'assumed_material')
    if settings.get('rate_profile') == MARKET_VERSION:
        price = resolve_material_price(profile, raw, settings)
        line.update(material_price_snapshot=price, material_rate=price['normalized_net_rate'],
                    material_price_original_rate=price['adopted_rate_cny_per_kg'],
                    material_price_tax_included=price['adopted_tax_included'],
                    material_price_vat_rate_percent=price['adopted_vat_rate_percent'],
                    material_price_source_kind=price['source_kind'])
    else:
        line['material_rate'] = PROFILES[profile]['rate']
    volume = number(line.get('volume_mm3'), positive=True)
    if volume is not None:
        weight = volume * Decimal(PROFILES[profile]['density']) / Decimal('1000000000')
        line.update(estimated_net_weight_kg=scalar(weight),
                    estimated_set_net_weight_kg=scalar(weight * line['quantity_per_set']),
                    estimated_total_net_weight_kg=scalar(weight * line['quantity_total']))


def estimate_line(line, bodies, settings, source_sha256, *, route_assumption=None):
    """Replace only a native-backed manufacturing draft with explicit estimates.

    Returns False when no quotation receipt is available: legacy strict rules
    continue to apply. It cannot turn missing quantities into zero or approval.
    """
    instances = _instances(bodies)
    if (not instances or len(instances) != line.get('quantity_per_set')
            or len(instances[0]) != 1 or line.get('mode') != 'make'
            or type(line.get('quantity_total')) is not int or line['quantity_total'] <= 0):
        return False
    receipts = [validated_quantities(body, source_sha256) for body in bodies]
    if any(value is None for value in receipts):
        return False
    if any((body.get('topology') or {}).get('valid') is not True
           or (body.get('topology') or {}).get('closed') is not True for body in bodies):
        return False
    receipt = receipts[0]
    # A validated flat pattern retains the established strict blank-volume
    # pricing and its cross-check blockers. Never bypass them with a proxy.
    if receipt.get('method') != 'native_brep_inventory':
        return False
    for other in receipts[1:]:
        if any(other.get(key) != receipt.get(key) for key in QUANTITY_KEYS):
            return False
    _adopt_material(line, bodies, settings)
    quantity = line['quantity_total']
    ids = [instances[0][0]['id']]
    out = {'version': 'process-v1', 'estimate_version': VERSION, 'status': 'blocked',
           'scope': 'proposed_estimate', 'scope_label': SCOPE_LABEL,
           'route_review_required': True, 'manufacturing_release': False,
           'source_solid_ids': ids, 'processes': [], 'blockers': [],
           'features': {**{key: receipt.get(key) for key in QUANTITY_KEYS},
                        'piercing_count': receipt.get('pierce_count')},
           'quotation_quantities': receipt, 'material': {},
           'weight_check': {'role': 'cross_check', 'status': 'unavailable',
                 'geometric_net_volume_mm3': line.get('volume_mm3'),
                 'estimated_net_weight_kg': line.get('estimated_net_weight_kg'),
                 'measurements': copy.deepcopy(receipt.get('measurements') or {}),
                 'basis': '原生净体积×本单密度；面积代理与板厚的差异原样保留，不是独立展开验证'},
           'estimated_cost_subtotal': None, 'estimated_price_subtotal': None}
    line['manufacturing'] = out
    line.update({key: None for key in MONEY_KEYS})
    line.update(priced=False, material_usage_kg=None)
    costs = {key: ZERO for key in COST_KEYS}
    known = False
    adopted_route = route_assumption == ROUTE_ID and receipt.get('bend_count') is None
    volume = number(line.get('volume_mm3'), positive=True)
    density, rate = number(line.get('density_kg_m3'), positive=True), number(line.get('material_rate'), positive=True)
    if volume is not None and density is not None and rate is not None:
        net = volume * density / Decimal('1000000000')
        usage = net / YIELD
        line['material_usage_kg'] = scalar(usage)
        costs['material_cost'] = Decimal(money(usage * rate * quantity))
        known = True
        out['material'] = {'basis': '原生封闭实体净体积×采用密度÷明确利用率；不是排版或展开毛坯',
            'native_volume_mm3': scalar(volume), 'net_weight_kg': scalar(net), 'usage_kg': scalar(usage),
            'utilization': text(YIELD), 'utilization_is_assumption': True,
            'material_source': 'native_cad' if line.get('confidence') == 'cad_material' else 'order_estimation_assumption'}
    else:
        _block(out, 'material_price_required', '材料估算采用价或原生净体积尚未落实。')
    for field, kind, label, unit_scale in (
        ('cut_length_mm', 'laser_cut', '切割行程暂估', Decimal(1000)),
        ('pierce_count', 'laser_piercing', '激光穿孔暂估', Decimal(1)),
        ('bend_count', 'bending', '折弯暂估', Decimal(1))):
        value = number(receipt.get(field))
        if value is None:
            if field == 'bend_count' and adopted_route:
                continue
            _block(out, field + '_required', {'cut_length_mm':'切割行程尚无法估量。',
                'pierce_count':'穿孔次数尚无法估量。','bend_count':'折弯数量尚未取得可靠候选，不能按零折计费。'}[field])
            continue
        proof = {'basis': 'native_flat_profile' if receipt['method'] == 'native_flat_profile' else '原生几何报价代理暂估；不是制造刀路或批准工艺',
                 field: receipt[field], 'quantity_status': receipt['quantity_statuses'].get(field),
                 'source_sha256': source_sha256, 'manufacturing_release': False}
        process = _process(kind, label, value/unit_scale, quantity, ids, proof)
        out['processes'].append(process)
        costs['process_cost'] += Decimal(process['amount_cny'])
        known = True
    if adopted_route:
        # Budget items explicitly stand in for the ambiguous forming/joining
        # route. They do not manufacture a bend count, welding length or hours.
        for kind, label in (('forming_budget', '钣金成形预算'), ('assembly_weld_budget', '拼焊预算')):
            rate_row = ROUTE_RATES[kind]
            amount = Decimal(rate_row['rate'])*quantity
            out['processes'].append({'id':kind,'kind':kind,'label':label,'status':'estimated',
                'quantity_per_piece':'1','quantity_total':str(quantity),'unit':'件',
                'rate_cny':rate_row['rate'],'amount_cny':money(amount),'rate_source':ROUTE_RATES['version'],
                'simulated':True,'source_solid_ids':ids,
                'evidence':{'basis':'操作者明确采用钣金成形＋拼焊工程假设；按真实件数预算，不代表已量测焊长、工时或折弯次数',
                    'route_assumption':ROUTE_ID,'assumption':True,'manufacturing_release':False}})
            costs['process_cost'] += amount
        out.update(route_assumption=ROUTE_ID, route_rate_snapshot=copy.deepcopy(ROUTE_RATES),
                   engineering_complete=False)
        known = True
    if number(receipt.get('cut_length_mm'), positive=True) is not None:
        setup = _process('laser_setup', '切割准备暂估', Decimal(1), quantity, ids,
                         {'basis': '本单同一零件分组一次；不按套数重复'}, per_order=True)
        out['processes'].append(setup)
        costs['setup_cost'] = Decimal(setup['amount_cny'])
    requirements = _requirement_text(line, bodies)
    for kind, pattern, label in (
            ('welding', r'焊|weld|solder', '焊接'),
            ('surface_treatment', r'镀锌|[侵浸]锌|喷粉|喷涂|抛光|酸洗|钝化|galvani|powder.?coat|anodi', '表面处理'),
            ('drilling_tapping', r'钻孔|攻[牙丝]|螺纹|drill|tapp', '钻孔／攻牙')):
        if re.search(pattern, requirements, re.I):
            _block(out, kind + '_required', '已读到' + label + '要求，计量与采用费率仍需核定。')
            out['processes'].append({'id': kind, 'kind': kind, 'label': label,
                'status': 'pending_review', 'quantity_per_piece': None, 'quantity_total': None,
                'unit': None, 'rate_cny': None, 'amount_cny': None, 'source_solid_ids': ids,
                'evidence': {'source_text': requirements[:2000]}})
    if known:
        subtotal = sum(costs.values(), ZERO)
        markup = Decimal(settings['rate_percent'])/100
        unit_price = subtotal/quantity*(1+markup) if settings['pricing_method'] == 'cost_plus' else subtotal/quantity/(1-markup)
        total_price = Decimal(money(unit_price))*quantity
        out.update(status='partial' if out['blockers'] else 'estimated',
                   estimated_cost_subtotal=money(subtotal), estimated_price_subtotal=money(total_price),
                   known_cost_components={key: None if key == 'material_cost' and not out['material']
                                          else money(value) for key, value in costs.items()})
        if not out['blockers']:
            line.update({key: money(value) for key, value in costs.items()})
            line.update(priced=True, total_cost=money(subtotal), total_price=money(total_price),
                        unit_cost=money(subtotal/quantity), unit_price=money(unit_price))
    return True
