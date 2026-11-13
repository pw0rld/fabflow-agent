"""Read-only weight summaries; product net mass is separate from material usage."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import re


def _decimal(value, *, positive):
    if value is None or value == '':
        return None
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', value):
        raise ValueError('重量须为非负十进制字符串或留空。')
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise ValueError('重量数值无效。') from error
    if not result.is_finite() or result >= Decimal('1e12') or (positive and result <= 0):
        raise ValueError('净重须为正数且小于 1e12 kg。')
    return result


def _quantity(value):
    return value if type(value) is int and 0 < value <= 1000000 else None


def _text(value):
    if value is None:
        return None
    result = format(value, 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def weight_summary(inputs):
    """Summarize selected rows without inventing density, quantity or net mass.

    All arithmetic is decimal and exact within the input bounds. A known
    subtotal never becomes a complete order total while any selected row is
    missing its applicable weight or quantity.
    """
    rows = {}
    selected = [line for line in inputs.get('lines', []) if line.get('selected')]
    order_sets = _quantity(inputs.get('order_sets'))
    known_net, known_material = [], []
    missing_net = missing_material = material_lines = 0
    with localcontext() as context:
        context.prec = 64
        for line in selected:
            per_set = _quantity(line.get('quantity_per_set'))
            net = _decimal(line.get('net_weight_kg'), positive=True)
            set_net = net * per_set if net is not None and per_set is not None else None
            total_net = set_net * order_sets if set_net is not None and order_sets is not None else None
            if total_net is None:
                missing_net += 1
            else:
                known_net.append(total_net)
            total_material = None
            if line.get('mode') == 'make':
                material_lines += 1
                usage = _decimal(line.get('material_usage'), positive=False)
                if usage is not None and per_set is not None and order_sets is not None:
                    total_material = usage * per_set * order_sets
                    known_material.append(total_material)
                else:
                    missing_material += 1
            rows[line['group_id']] = {
                'net_weight_kg': _text(net),
                'set_net_weight_kg': _text(set_net),
                'total_net_weight_kg': _text(total_net),
                'total_material_usage_kg': _text(total_material),
                'source': 'entered' if net is not None else 'unknown',
            }
        net_subtotal = sum(known_net, Decimal(0)) if known_net else None
        material_subtotal = sum(known_material, Decimal(0)) if known_material else None
    return {'lines': rows, 'totals': {
        'total_net_weight_kg': _text(net_subtotal) if selected and not missing_net else None,
        'known_net_weight_kg': _text(net_subtotal),
        'missing_net_weight_lines': missing_net,
        'selected_lines': len(selected),
        'total_material_usage_kg': _text(material_subtotal) if material_lines and not missing_material else None,
        'known_material_usage_kg': _text(material_subtotal),
        'missing_material_usage_lines': missing_material,
        'material_usage_lines': material_lines,
    }}
