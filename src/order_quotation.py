"""Pure multi-part costing: immutable input snapshots, BOM expansion and Decimal."""
from __future__ import annotations
import copy
from decimal import Decimal, localcontext
import hashlib

from prep_record import require, number, stamp
from quotation import validate_policy, money
from order_store import demand


def pricing(spec):
    require(isinstance(spec, dict) and set(spec) == {'pricing_method', 'rate_percent', 'tax_excluded', 'freight_excluded'}, '请完整填写本单定价及税运口径。')
    require(spec['tax_excluded'] is True and spec['freight_excluded'] is True, '当前版本为人民币、未计税费、不含运费。')
    require(isinstance(spec['rate_percent'], str) and spec['rate_percent'].strip(), '请填写本单定价比例。')
    policy = {'schema_version': 1, 'pricing_method': spec['pricing_method'], 'rate': str(number(spec['rate_percent']) / 100),
              'currency': 'CNY', 'tax_included': False, 'freight_included': False, 'simulated': True,
              'actor': '当前操作者（姓名未提供）', 'source_text': str(spec)}
    validate_policy(policy)
    return policy


def confirmed_route(order, part_id):
    steps = order['routes'].get(part_id, [])
    require(isinstance(steps, list) and steps, '请先确认零件的工艺步骤。')
    names = []
    for step in steps:
        require(isinstance(step, dict) and step.get('status') == 'confirmed', '工艺建议尚未确认，请核对顺序并保存。')
        operation = step.get('operation')
        require(isinstance(operation, str) and 0 < len(operation.strip()) <= 100, '工序名称无效。')
        require(operation not in names, '同名重复工序需要按步骤计量的新接口，当前不能合并计费。')
        names.append(operation)
    return names


def calculate(order, records, calculators, spec, shared_fees, quote_id):
    """records/calculators are already snapshotted under per-task read locks.

    calculators[part_id] is the existing *cost-only* single-part result. Prices
    are then applied once to each top-level order line, never by summing old
    quoted prices. Assembly nodes charge only their explicitly confirmed work.
    """
    quantities = demand(order)
    policy = pricing(spec)
    rate = number(policy['rate'])
    parts = {part['id']: part for part in order['parts']}
    children = {key: [] for key in parts}
    for row in order['bom']:
        children[row['parent_id']].append(row)
    require(isinstance(shared_fees, list) and len(shared_fees) <= 100, '整单费用须为有限列表。')
    ids, shared = set(), []
    for fee in shared_fees:
        require(isinstance(fee, dict) and set(fee) == {'id', 'name', 'value', 'inclusions'}, '整单费用格式无效。')
        require(isinstance(fee['id'], str) and fee['id'] and fee['id'] not in ids, '整单费用编号重复。')
        ids.add(fee['id'])
        require(isinstance(fee['name'], str) and 0 < len(fee['name']) <= 200 and isinstance(fee['inclusions'], str) and 0 < len(fee['inclusions'].strip()) <= 2000, '整单费用需要名称和实际包含范围。')
        value = number(fee['value'])
        require(0 <= value < Decimal('1e15'), '整单费用金额无效。')
        shared.append({**fee, 'value': str(value)})
    own, breakdown = {}, []
    with localcontext() as context:
        context.prec = 64
        for part_id, quantity in quantities.items():
            part, count = parts[part_id], number(quantity)
            mode = part['mode']
            if mode == 'make':
                route = confirmed_route(order, part_id)
                record = records[part_id]
                effective = record['fields']['process_route']['effective']
                require(effective and effective['value'] == route, '零件采用工序与订单工艺顺序不一致，请重新准备零件或核对工序。')
                result = calculators[part_id]
                require(number(result['quantity']) == count, '零件加工数量与 BOM 需求量不同，请重新准备零件并核对。')
                amount = number(result['raw_total_cost'])
                detail = {'single_part_calculation': result, 'task_id': part['task_id'], 'source_revision': record['data_revision']}
            elif mode == 'buy':
                require(part.get('cost_confirmed') is True and part.get('purchase_price') is not None, '外购件采购单价尚未核对。')
                require(bool((part.get('cost_inclusions') or '').strip()), '外购件需要明确采购价格来源与包含范围。')
                amount = number(part['purchase_price']) * count
                detail = {'unit_cost': str(part['purchase_price']), 'inclusions': part['cost_inclusions']}
            else:
                require(part.get('cost_confirmed') is True and part.get('assembly_unit_cost') is not None and part.get('assembly_setup_fee') is not None, '装配加工费和准备费尚未核对；明确不收才填 0。')
                require(bool((part.get('cost_inclusions') or '').strip()), '装配费用需要明确包含范围。')
                if number(part['assembly_unit_cost']) > 0 or number(part['assembly_setup_fee']) > 0:
                    confirmed_route(order, part_id)
                amount = number(part['assembly_unit_cost']) * count + number(part['assembly_setup_fee'])
                detail = {'unit_cost': str(part['assembly_unit_cost']), 'setup_fee': str(part['assembly_setup_fee']), 'inclusions': part['cost_inclusions']}
            require(Decimal(0) <= amount < Decimal('1e20'), '零件成本超出可处理范围。')
            own[part_id] = amount / count
            breakdown.append({'part_id': part_id, 'name': part['name'], 'mode': mode, 'quantity': quantity,
                              'own_total_cost': money(amount), 'raw_own_total_cost': str(amount), **detail})
        memo = {}
        def subtree(part_id):
            if part_id not in memo:
                memo[part_id] = own[part_id] + sum((number(row['quantity']) * subtree(row['part_id']) for row in children[part_id]), Decimal(0))
            return memo[part_id]
        price_of = lambda value: value * (1 + rate) if policy['pricing_method'] == 'cost_plus' else value / (1 - rate)
        lines = []
        for row in order['lines']:
            part, count = parts[row['part_id']], number(row['quantity'])
            unit_cost = subtree(part['id'])
            unit_price = Decimal(money(price_of(unit_cost)))
            lines.append({'id': row['id'], 'part_id': part['id'], 'name': part['name'], 'part_number': part.get('part_number'),
                          'version': part.get('version'), 'quantity': str(count), 'unit': row['unit'], 'unit_cost': money(unit_cost),
                          'raw_line_cost': str(unit_cost * count), 'unit_price': money(unit_price), 'total_price': money(unit_price * count)})
        shared_cost = sum((number(row['value']) for row in shared), Decimal(0))
        shared_price = Decimal(money(price_of(shared_cost)))
        line_cost = sum((number(row['raw_line_cost']) for row in lines), Decimal(0))
        total_cost = line_cost + shared_cost
        total_price = sum((number(row['total_price']) for row in lines), Decimal(0)) + shared_price
        return {'schema_version': 1, 'calculator_version': 'order-1', 'id': quote_id, 'quote_id': quote_id,
                'order_id': order['id'], 'order_name': order['name'], 'created_at': stamp(), 'source_revision': order['revision'],
                'status': '模拟报价草稿，待审核', 'simulated': True, 'approved': False,
                'policy': policy, 'lines': lines, 'part_costs': breakdown, 'shared_fees': shared,
                'shared_price': money(shared_price), 'demands': quantities,
                'amounts': {'total_cost': money(total_cost), 'total_price': money(total_price),
                            'markup_amount': money(total_price - total_cost), 'shared_cost': money(shared_cost)},
                'order_snapshot': copy.deepcopy({key: order[key] for key in ('id', 'name', 'inquiry', 'revision', 'documents', 'parts', 'lines', 'bom', 'routes')}),
                'record_snapshots': copy.deepcopy(records),
                'rounding_rule': 'Decimal ROUND_HALF_UP; top-level unit price rounded to CNY 0.01; line total = unit price × quantity; shared charge priced once',
                'tax_included': False, 'freight_included': False}
