"""Pure Decimal cost sheets; no IO, model, CAD, persistence or approvals."""
from __future__ import annotations
import copy
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP, localcontext
import html
import re
from prep_record import digest

VERSION = 'cost-sheet-v1'
ZERO = Decimal('0')
CENT = Decimal('.01')
MAX_ENTRIES, MAX_LINES = 20000, 5000
MAX_ALLOCATION_CELLS = 100000
MANAGEMENT_ID = 'AUTO-MANAGEMENT-FEE'
CATEGORIES = ('material', 'purchase', 'process', 'setup', 'tooling', 'other')
COST_KEYS = tuple(value + '_cost' for value in CATEGORIES)
LABELS = dict(zip(CATEGORIES, ('材料', '外购与辅料', '加工', '准备', '模具', '其他')))
LINE_ID = re.compile(r'L[0-9]{4,8}\Z')
ENTRY_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z')
TERMS = ('delivery', 'payment', 'validity', 'tax_shipping')
UNITS = {'piece', 'set', 'order', 'minute', 'hour', 'kg', 'm', 'm2', 'dm2', 'hole', 'bend', 'point'}
MATRIX = {'material': {'fixed', 'quantity_rate', 'material'}, 'purchase': {'fixed', 'quantity_rate'},
          'process': {'fixed', 'quantity_rate'}, 'setup': {'fixed', 'quantity_rate'},
          'tooling': {'fixed', 'tooling_allocation'}, 'other': {'fixed', 'quantity_rate'}}
PROCESS_LABELS = (
    ('下料', '数剪(落料)', 'minute'), ('冲压', '普冲(装夹)', 'minute'),
    ('冲压', '普冲(成型)', 'minute'), ('冲压', '数控冲床(装夹)', 'minute'),
    ('冲压', '数控冲床(冲)', 'minute'), ('折弯', '数控折弯(装夹)', 'minute'),
    ('折弯', '数控折弯(折)', 'minute'), ('孔加工', '倒沉孔', 'minute'),
    ('孔加工', '拉伸孔', 'minute'), ('孔加工', '攻丝', 'minute'),
    ('孔加工', '扩孔', 'minute'), ('连接', '压铆', 'minute'),
    ('连接', '焊接', 'minute'), ('连接', '点焊', 'minute'),
    ('连接', '熔焊', 'minute'), ('修整', '去毛刺', 'minute'),
    ('修整', '整形', 'minute'), ('修整', '锉刀', 'minute'),
    ('修整', '钳打磨', 'minute'), ('表面与标识', '喷涂(油漆/塑粉)', 'm2'),
    ('表面与标识', '镀锌', 'dm2'), ('表面与标识', '丝印', 'minute'),
    ('装配与辅助', '装配', 'minute'), ('装配与辅助', '辅助', 'minute'))
PROCESS_CATALOG = [dict(id='QUOTE-A-P' + str(i).zfill(2), label=label, group=group,
    category=group, default_unit=unit, default_rate=None)
    for i, (group, label, unit) in enumerate(PROCESS_LABELS, 1)] + [
    dict(id='EXTRA-LASER', label='激光切割', group='下料', category='下料', default_unit='m', default_rate=None),
    dict(id='EXTRA-DRILL', label='钻孔', group='孔加工', category='孔加工', default_unit='hole', default_rate=None),
    dict(id='EXTRA-POLISH', label='抛光', group='修整', category='修整', default_unit='minute', default_rate=None)]
PROCESS_IDS = {row['id'] for row in PROCESS_CATALOG}
COMMON = {'id', 'line_id', 'category', 'label', 'method', 'source_kind'}
FIELDS = {'retained': {'amount'}, 'fixed': {'amount'}, 'quantity_rate': {'quantity', 'rate', 'unit'},
          'material': {'net_kg', 'loss_kg', 'unit_price', 'scrap_price'},
          'tooling_allocation': {'tooling_id', 'tooling_amount', 'amortization_quantity'}}

def _require(condition, message):
    if not condition:
        raise ValueError(message)

def _keys(value, required, optional, label):
    _require(isinstance(value, dict) and required <= set(value) <= required | optional,
             label + '字段缺失或包含不支持的字段。')

def _text(value, label, maximum=240, *, empty=False):
    _require(isinstance(value, str) and (empty or value.strip()) and len(value) <= maximum
             and not any((ord(c) < 32 and c not in '\n\t') or ord(c) == 127 for c in value),
             label + '须为长度受限的文字。')
    return value

def _number(value, label, *, input_value=False, positive=False):
    if input_value:
        _require(isinstance(value, str) and len(value) <= 32
                 and re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]{1,8})?', value),
                 label + '须为最多八位小数的十进制数字字符串。')
    else:
        _require(isinstance(value, (str, int, Decimal)) and not isinstance(value, bool)
                 and len(str(value)) <= 100, label + '不是有效的已保存数字。')
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation) as error:
        raise ValueError(label + '不是有效数字。') from error
    _require(number.is_finite() and 0 <= number <= (Decimal('1e12') if input_value else Decimal('1e24'))
             and (not positive or number > 0), label + '超出处理范围。')
    return number

def _money(value):
    _require(value.is_finite() and 0 <= value <= Decimal('1e24'), '计算金额超出处理范围。')
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), '.2f')

def _pricing(value):
    """Only an explicit policy can adopt management charges or new markup."""
    _keys(value, {'management_rate_percent', 'pricing_method', 'rate_percent', 'rounding'}, set(), '定价口径')
    management = _number(value['management_rate_percent'], '管理费比例', input_value=True)
    factor = _number(value['rate_percent'], '利润比例', input_value=True)
    _require(management <= 1000 and factor <= 1000, '管理费或利润比例不能超过1000%。')
    _require(isinstance(value['pricing_method'], str) and value['pricing_method'] in {'cost_plus', 'gross_margin'}
             and (value['pricing_method'] != 'gross_margin' or factor < 100), '定价方法或毛利率无效。')
    _require(isinstance(value['rounding'], str) and value['rounding'] in {'whole_yuan', 'cent'},
             '管理费取整口径须为整元或分。')
    return copy.deepcopy(value)

def _current_pricing(result, existing=None):
    if existing is not None and 'pricing' in existing:
        return _pricing(existing['pricing'])
    return {'management_rate_percent': '0', 'pricing_method': result['settings']['pricing_method'],
            'rate_percent': str(result['settings']['rate_percent']), 'rounding': 'cent'}

def _accounting(direct, management, full, quote, pricing, unpriced):
    return {**copy.deepcopy(pricing), 'scope': 'priced_lines', 'unpriced_lines': unpriced,
            'direct_cost': _money(direct) if direct is not None else None,
            'management_cost': _money(management) if management is not None else None,
            'full_cost': _money(full) if full is not None else None,
            # A zero-markup line can have a negative cent rounding remainder
            # after unit price × quantity; do not reject that valid policy.
            'profit': format((quote-full).quantize(CENT, rounding=ROUND_HALF_UP), '.2f') if quote is not None and full is not None else None,
            'quoted_price': _money(quote) if quote is not None else None}

def _terms(value=None, previous=None):
    result = {key: '' for key in TERMS}
    for incoming in (previous, value):
        if incoming is not None:
            _keys(incoming, set(), set(TERMS), '商务条件')
            result.update({key: _text(text, key, 1200, empty=True) for key, text in incoming.items()})
    return result

def _incomplete_process_totals(result, rows, priced):
    """Recognize a bound process run that intentionally withholds full totals.

    Only fully priced line amounts can become retained cost entries. Partial
    process candidates and their reference subtotal remain engineering data.
    Legacy runs and manual cost-sheet versions keep the original strict rule.
    """
    settings, manufacturing, totals = result.get('settings'), result.get('manufacturing'), result.get('totals')
    if (not isinstance(settings, dict) or settings.get('costing_mode') != 'process-v1'
            or not isinstance(manufacturing, dict) or manufacturing.get('version') != 'process-v1'
            or manufacturing.get('complete') is not False
            or manufacturing.get('scope') not in {'identified_processes', 'proposed_estimate'}
            or manufacturing.get('scope') == 'proposed_estimate' and manufacturing.get('estimate_version') != 'process-estimate-20261113-v1'
            or manufacturing.get('status') not in {'partial', 'blocked'} or result.get('cost_sheet') is not None
            or not isinstance(totals, dict) or len(priced) >= len(rows)
            or manufacturing.get('parts_count') != len(rows) or manufacturing.get('complete_parts') != len(priced)
            or manufacturing.get('pending_parts') != len(rows)-len(priced)
            or totals.get('priced_lines') != len(priced) or totals.get('unpriced_lines') != len(rows)-len(priced)):
        return False
    source_sha = result.get('source_sha256')
    if not isinstance(source_sha, str) or not re.fullmatch(r'[a-f0-9]{64}', source_sha):
        return False
    sources = result.get('source_files')
    if not isinstance(sources, list) or not sources:
        return False
    registered = {source.get('id'): source.get('sha256') for source in sources if isinstance(source, dict)}
    if any(not isinstance(row.get('file_id'), str) or row['file_id'] not in registered
           or not isinstance(registered[row['file_id']], str)
           or not re.fullmatch(r'[a-f0-9]{64}', registered[row['file_id']])
           or not isinstance(row.get('manufacturing'), dict)
           or row['manufacturing'].get('version') != 'process-v1'
           or row['manufacturing'].get('status') not in ({'estimated'} if row['priced'] else {'partial', 'blocked'})
           for row in rows.values()):
        return False
    for total_key, summary_key in (('known_cost_subtotal', 'estimated_cost_subtotal'),
                                   ('known_price_subtotal', 'estimated_price_subtotal')):
        if totals.get(total_key) != manufacturing.get(summary_key):
            return False
    return totals.get('total_cost') is None and totals.get('total_price') is None

def _lines(result):
    _require(isinstance(result, dict) and isinstance(result.get('lines'), list)
             and 0 < len(result['lines']) <= MAX_LINES, '没有可核价的已保存报价。')
    settings = result.get('settings')
    _require(isinstance(settings, dict) and type(settings.get('order_sets')) is int
             and 0 < settings['order_sets'] <= 1000000
             and settings.get('pricing_method') in {'cost_plus', 'gross_margin'}, '原数量或定价方法无效。')
    rate = _number(settings.get('rate_percent'), '原定价比例')
    _require(rate <= 1000 and (settings['pricing_method'] != 'gross_margin' or rate < 100), '原定价比例无效。')
    rows = {}
    for line in result['lines']:
        identity = line.get('id') if isinstance(line, dict) else None
        _require(isinstance(identity, str) and LINE_ID.fullmatch(identity) and identity not in rows,
                 '原报价存在无效或重复行号。')
        _require(type(line.get('priced')) is bool, '原报价缺少计价状态：' + identity)
        if line['priced']:
            _require(type(line.get('quantity_per_set')) is int and line['quantity_per_set'] > 0
                     and type(line.get('quantity_total')) is int
                     and line['quantity_total'] == line['quantity_per_set'] * settings['order_sets'],
                     '原已计价行缺少一致的订单数量：' + identity)
            amounts = [_number(line.get(key, '0.00') if key in {'tooling_cost', 'other_cost'} else line.get(key),
                               '已保存成本分项') for key in COST_KEYS]
            _require(sum(amounts, ZERO) == _number(line.get('total_cost'), '已保存成本合计'),
                     '已保存成本分项不守恒：' + identity)
            for key in ('unit_cost', 'unit_price', 'total_price'):
                _number(line.get(key), '已保存' + key)
        rows[identity] = line
    totals = result.get('totals')
    _require(isinstance(totals, dict), '原报价缺少成本汇总。')
    priced = [line for line in rows.values() if line['priced']]
    incomplete_process = _incomplete_process_totals(result, rows, priced)
    for key in (*COST_KEYS, 'total_cost', 'total_price'):
        expected = sum((_number(line.get(key, '0.00'), '原行合计') for line in priced), ZERO)
        value = totals.get(key, '0.00') if key in {'tooling_cost', 'other_cost'} else totals.get(key)
        withheld_full_total = incomplete_process and key in {'total_cost', 'total_price'} and value is None
        _require(withheld_full_total or (not priced and value is None) or value is not None and _number(value, '原总计') == expected,
                 '原报价汇总与行金额不一致：' + key)
    return rows

def _engineering_categories(result):
    snapshot = result.get('engineering_repricing')
    if not isinstance(snapshot, dict) or snapshot.get('after_totals') != result.get('totals'):
        return set()
    categories = set()
    for cost in snapshot.get('line_costs', []):
        for component in cost.get('components', []):
            method = component.get('method')
            category = ('material' if method == 'adopt_material' else 'purchase' if method == 'purchase_mesh'
                        else 'process' if method in {'pipe_weld_seal', 'mesh_weld', 'hotdip'} else None)
            if category:
                categories.add((cost.get('line_id'), category))
    return categories

def _setup_inclusive_processes(result, lines):
    """Known saved welding templates already include this part's preparation.

    This uses conserved, active engineering records, never labels or a guessed
    factory inclusion. A copied free-text word cannot become an adopted cost
    fact. Retaining any inclusive welding component retains that inclusion.
    """
    snapshot = result.get('engineering_repricing')
    if (not isinstance(snapshot, dict) or snapshot.get('version') != 'engineering-demo-v1'
            or snapshot.get('after_totals') != result.get('totals')):
        return set()
    inclusive = set()
    for cost in snapshot.get('line_costs', []):
        identity = cost.get('line_id')
        line = lines.get(identity)
        if (line is None or line.get('priced') is not True or any(cost.get(key) != line.get(key)
                for key in ('material_cost', 'purchase_cost', 'process_cost', 'setup_cost',
                            'total_cost', 'total_price', 'unit_cost', 'unit_price'))):
            continue
        if any(component.get('method') in {'pipe_weld_seal', 'mesh_weld'}
               for component in cost.get('components', []) if isinstance(component, dict)):
            inclusive.add(identity)
    return inclusive

def _legacy_entries(result, lines):
    engineering, entries = _engineering_categories(result), []
    for line in lines.values():
        if not line['priced']:
            continue
        for category in CATEGORIES:
            if category in {'tooling', 'other'} and category + '_cost' not in line:
                continue
            adopted = (line['id'], category) in engineering
            entries.append({'id': 'CS-' + line['id'] + '-' + category, 'line_id': line['id'],
                'category': category, 'label': '已保存' + LABELS[category] + ('费（含工程核价）' if adopted else '费'),
                'method': 'retained', 'source_kind': 'engineering_adopted' if adopted else 'legacy_estimate',
                'amount': _money(_number(line.get(category + '_cost'), '已保存分类费用')),
                'note': '保留原报价金额，不拆造工序或工时；' + ('已有工程采用记录。' if adopted else '原综合估算或采用价。')})
    _require(len(entries) <= MAX_ENTRIES, '核价条目数量超出处理范围。')
    return entries

def _existing(result, lines):
    value = result.get('cost_sheet')
    if value is None:
        return None
    _require(isinstance(value, dict) and value.get('version') == VERSION
             and value.get('after_totals') == result.get('totals')
             and isinstance(value.get('entries'), list) and len(value['entries']) <= MAX_ENTRIES
             and isinstance(value.get('entry_amounts'), dict)
             and isinstance(value.get('line_costs'), list), '已有核价单与当前报价不一致。')
    snapshots = {row.get('line_id'): row for row in value['line_costs'] if isinstance(row, dict)}
    _require(set(snapshots) == {identity for identity, line in lines.items() if line['priced']}, '已有核价单范围不一致。')
    for identity, row in snapshots.items():
        _require(all(row.get(key) == lines[identity].get(key) for key in
                     (*COST_KEYS, 'total_cost', 'total_price', 'unit_cost', 'unit_price')),
                 '已有核价单金额不一致：' + identity)
    if 'pricing' in value or 'accounting_summary' in value:
        pricing = _current_pricing(result, value)
        if 'pricing' in value:
            _require(all(pricing[key] == result['settings'][key] for key in ('pricing_method', 'rate_percent')),
                     '已保存核价口径与报价定价不一致。')
        priced = [row for row in lines.values() if row['priced']]
        direct = sum((_number(value['entry_amounts'].get(entry['id']), '原直接成本条目') for entry in value['entries']), ZERO)
        quantum = Decimal('1') if pricing['rounding'] == 'whole_yuan' else CENT
        management = (direct * Decimal(pricing['management_rate_percent']) / 100).quantize(quantum, rounding=ROUND_HALF_UP)
        full = sum((_number(row['total_cost'], '原完全成本') for row in priced), ZERO)
        quote = sum((_number(row['total_price'], '原报价') for row in priced), ZERO)
        _require(direct + management == full, '原直接成本与管理费不守恒。')
        expected = _accounting(direct if priced else None, management if priced else None,
                               full if priced else None, quote if priced else None, pricing, len(lines)-len(priced))
        _require(value.get('accounting_summary') == expected, '已保存核算汇总与本单金额不一致。')
    return value

def build_cost_sheet(result):
    """Migrate saved costs without recalculating or changing the quotation."""
    with localcontext() as context:
        context.prec = 80
        lines = _lines(result)
        existing = _existing(result, lines)
        entries = copy.deepcopy(existing['entries']) if existing else _legacy_entries(result, lines)
        amounts = copy.deepcopy(existing['entry_amounts']) if existing else {e['id']: e['amount'] for e in entries}
        totals = copy.deepcopy(result['totals'])
        for key in ('tooling_cost', 'other_cost'):
            totals.setdefault(key, '0.00')
        terms = _terms(previous=existing.get('customer_terms') if existing else result.get('customer_terms'))
        policy = {'base_digest': digest(result), 'entries': copy.deepcopy(entries),
                  'customer_terms': copy.deepcopy(terms), 'adopt_unpriced_line_ids': []}
        pricing = _current_pricing(result, existing)
        if existing and 'pricing' in existing:
            policy['pricing'] = copy.deepcopy(pricing)
        priced = [line for line in lines.values() if line['priced']]
        if existing and isinstance(existing.get('accounting_summary'), dict):
            accounting = copy.deepcopy(existing['accounting_summary'])
        else:
            full = sum((_number(line['total_cost'], '已保存成本') for line in priced), ZERO) if priced else None
            quote = sum((_number(line['total_price'], '已保存报价') for line in priced), ZERO) if priced else None
            accounting = _accounting(full, ZERO if priced else None, full, quote, pricing, len(lines)-len(priced))
        from customer_costing_reference import factory_reference
        return {'version': VERSION, 'base_digest': digest(result), 'status': 'pending_review',
                'entries': entries, 'entry_amounts': amounts, 'totals': totals, 'policy': policy,
                'factory_reference': factory_reference(pricing), 'accounting_summary': accounting,
                'catalog': copy.deepcopy(PROCESS_CATALOG), 'process_catalog': copy.deepcopy(PROCESS_CATALOG),
                'customer_terms': terms, 'quantity_basis': '净料、损耗及工程量均按本订单总量填写；未自动推断工时。',
                'lines': [{'line_id': identity, 'part_name': line.get('part_name'), 'document': line.get('document'),
                           'quantity_total': line.get('quantity_total'), 'priced': line['priced']}
                          for identity, line in lines.items()]}

def _entry(value, originals, lines):
    _require(isinstance(value, dict), '核价条目须为对象。')
    method, category = value.get('method'), value.get('category')
    _require(isinstance(method, str) and method in FIELDS and isinstance(category, str) and category in CATEGORIES,
             '核价方法或费用类别无效。')
    required = COMMON | FIELDS[method]
    if category == 'tooling' and method == 'fixed':
        required = required | {'tooling_id'}
    _keys(value, required, {'note'} | ({'process_id'} if category == 'process' else set()), '核价条目')
    identity, line_id = value['id'], value['line_id']
    _require(isinstance(identity, str) and ENTRY_ID.fullmatch(identity) and identity != MANAGEMENT_ID,
             '核价条目编号无效或占用自动管理费编号。')
    _require(line_id in lines if isinstance(line_id, str) else line_id is None and category in {'tooling', 'other'},
             '费用必须关联本单零件；仅模具和其他费用允许整单分摊。')
    _text(value['label'], '费用名称', 160)
    if 'note' in value:
        _text(value['note'], '费用说明', 1200, empty=True)
    if 'process_id' in value:
        _require(isinstance(value['process_id'], str) and value['process_id'] in PROCESS_IDS, '工序目录编号无效。')
    if method == 'retained':
        _require(identity in originals and originals[identity].get('method') == 'retained'
                 and value == originals[identity], '保留原费用须完整绑定原条目，不能改写金额或来源。')
    else:
        _require(value['source_kind'] == 'manual_adopted', '新增或修改费用只能标记为人工采用价。')
        _require(method in MATRIX[category], '费用类别不支持该核价方法。')
    if method in {'retained', 'fixed'}:
        _number(value['amount'], '固定金额', input_value=method != 'retained')
    elif method == 'quantity_rate':
        _number(value['quantity'], '本单工程量', input_value=True, positive=True)
        _number(value['rate'], '采用费率', input_value=True)
        _require(isinstance(value['unit'], str) and value['unit'] in UNITS, '工程量单位无效。')
    elif method == 'material':
        _number(value['net_kg'], '本单净料重量', input_value=True, positive=True)
        for key in ('loss_kg', 'unit_price', 'scrap_price'):
            _number(value[key], key, input_value=True)
        _require(Decimal(value['scrap_price']) <= Decimal(value['unit_price']), '废料回收单价不能高于材料采用单价。')
    elif method == 'tooling_allocation':
        _number(value['tooling_amount'], '模具总费用', input_value=True)
        _number(value['amortization_quantity'], '模具摊销总量', input_value=True, positive=True)
    if category == 'tooling' and method != 'retained':
        _require(isinstance(value['tooling_id'], str) and ENTRY_ID.fullmatch(value['tooling_id']), '模具编号无效。')
    return copy.deepcopy(value)

def _amount(entry, lines, settings):
    method = entry['method']
    if method in {'retained', 'fixed'}:
        value = Decimal(entry['amount'])
    elif method == 'quantity_rate':
        value = Decimal(entry['quantity']) * Decimal(entry['rate'])
    elif method == 'material':
        value = ((Decimal(entry['net_kg']) + Decimal(entry['loss_kg'])) * Decimal(entry['unit_price'])
                 - Decimal(entry['loss_kg']) * Decimal(entry['scrap_price']))
    else:
        quantity = lines[entry['line_id']]['quantity_total'] if entry['line_id'] else settings['order_sets']
        _require(type(quantity) is int and quantity > 0, '模具摊销缺少明确订单数量。')
        _require(Decimal(entry['amortization_quantity']) >= quantity,
                 '摊销总量不能小于本单收费数量；全额收取请使用固定费用。')
        value = Decimal(entry['tooling_amount']) * quantity / Decimal(entry['amortization_quantity'])
    return Decimal(_money(value))

def _allocate(amount, basis):
    if amount == 0:
        return {identity: ZERO for identity in basis}
    _require(basis and sum(basis.values(), ZERO) > 0, '整单费用缺少有效已采用行的成本分摊基数。')
    cents, total = int(amount * 100), sum(basis.values(), ZERO)
    raw = {identity: Decimal(cents) * weight / total for identity, weight in basis.items()}
    units = {identity: int(value.to_integral_value(rounding=ROUND_FLOOR)) for identity, value in raw.items()}
    left = cents - sum(units.values())
    order = sorted(raw, key=lambda identity: (-(raw[identity] - units[identity]), identity))
    for identity in order[:left]:
        units[identity] += 1
    _require(sum(units.values()) == cents, '整单费用分摊不守恒。')
    return {identity: Decimal(value) / 100 for identity, value in units.items()}

def _refresh_engineering(base, result, entries, originals):
    snapshot = result.get('engineering_repricing')
    if not isinstance(snapshot, dict) or snapshot.get('after_totals') != base.get('totals'):
        return
    retained = {(e['line_id'], e['category']) for e in entries
                if e['method'] == 'retained' and e == originals.get(e['id'])}
    methods = {'adopt_material': 'material', 'purchase_mesh': 'purchase',
               'pipe_weld_seal': 'process', 'mesh_weld': 'process', 'hotdip': 'process'}
    by_id, kept, costs = {line['id']: line for line in result['lines']}, set(), []
    for old in snapshot.get('line_costs', []):
        identity = old.get('line_id')
        if identity not in by_id or not by_id[identity]['priced']:
            continue
        components = [c for c in old.get('components', []) if (identity, methods.get(c.get('method'))) in retained]
        if components:
            kept.update((identity, c.get('method')) for c in components)
            row = {key: by_id[identity].get(key) for key in (*COST_KEYS, 'total_cost', 'total_price', 'unit_cost', 'unit_price')}
            row.update(line_id=identity, components=components)
            costs.append(row)
    for applied in snapshot.get('applied_requirements', []):
        if (applied.get('line_id'), applied.get('method')) not in kept:
            applied['status'] = 'pending_cost_sheet_review'
    snapshot.update(line_costs=costs, after_totals=copy.deepcopy(result['totals']), cost_sheet_rebased=True)

def reprice_cost_sheet(result, policy):
    """Replace adopted cost entries; the caller saves the returned new RUN."""
    with localcontext() as context:
        context.prec = 80
        lines = _lines(result)
        sheet = build_cost_sheet(result)
        _keys(policy, {'base_digest', 'entries'}, {'adopt_unpriced_line_ids', 'customer_terms', 'pricing'}, '核价策略')
        _require(policy['base_digest'] == digest(result), '报价来源已变化，请重新加载核价单。')
        # Older editors may omit a field they do not understand. Preserve an
        # already adopted order policy, without applying any sample default.
        explicit_pricing = (_pricing(policy['pricing']) if 'pricing' in policy else
                            _pricing(sheet['policy']['pricing']) if 'pricing' in sheet['policy'] else None)
        values = policy['entries']
        _require(isinstance(values, list) and 0 < len(values) <= MAX_ENTRIES, '核价条目数量无效。')
        originals = {entry['id']: entry for entry in sheet['entries']}
        entries = [_entry(value, originals, lines) for value in values]
        _require(len({e['id'] for e in entries}) == len(entries), '核价条目编号重复。')
        tools = [e['tooling_id'] for e in entries if e['category'] == 'tooling' and e['method'] != 'retained']
        _require(len(tools) == len(set(tools)), '同一模具不能同时全额收取和摊销，也不能重复收费。')
        adopted = policy.get('adopt_unpriced_line_ids', [])
        _require(isinstance(adopted, list) and len(adopted) <= len(lines)
                 and all(isinstance(identity, str) and identity in lines and not lines[identity]['priced'] for identity in adopted)
                 and len(adopted) == len(set(adopted)), '人工采用的未计价行号无效或重复。')
        by_line = {identity: [] for identity in lines}
        for entry in entries:
            if entry['line_id'] is not None:
                _require(lines[entry['line_id']]['priced'] or entry['line_id'] in adopted,
                         '未计价行须显式采用人工核价：' + entry['line_id'])
                by_line[entry['line_id']].append(entry)
        setup_inclusive = _setup_inclusive_processes(result, lines)
        for identity, current in by_line.items():
            retained = {e['category'] for e in current if e['method'] == 'retained'}
            manual = {e['category'] for e in current if e['method'] != 'retained'}
            _require(not retained.intersection(manual), '新增费用前请移除同类别原保留金额，避免重复收费：' + identity)
            _require('process' not in manual or 'setup' not in retained,
                     '采用加工明细时请同时撤下原准备费，按实际包含项另填准备费用：' + identity)
            _require(not ('setup' in manual and 'process' in retained and identity in setup_inclusive),
                     '保留的工程加工费已包含准备费；请先替换该加工组并明确拆项，不能另加准备费：' + identity)
            if lines[identity]['priced'] or identity in adopted:
                categories = {e['category'] for e in current}
                purchase = any(e['category'] == 'purchase' and
                               (e['method'] != 'retained' or lines[identity].get('mode') == 'buy') for e in current)
                _require(purchase or {'material', 'process'} <= categories,
                         '计价行须有明确外购费用，或材料及加工费用：' + identity)
            if identity in adopted:
                _require(type(lines[identity].get('quantity_per_set')) is int and lines[identity]['quantity_per_set'] > 0
                         and type(lines[identity].get('quantity_total')) is int
                         and lines[identity]['quantity_total'] == lines[identity]['quantity_per_set'] * result['settings']['order_sets'],
                         '人工核价仍缺少明确订单件数：' + identity)
                _require(current and all(e['source_kind'] == 'manual_adopted' for e in current),
                         '原未计价行必须使用显式人工采用费用。')
        existing = _existing(result, lines)
        raw_basis = existing.get('allocation_basis') if existing else None
        if raw_basis is None:
            raw_basis = {identity: line['total_cost'] for identity, line in lines.items() if line['priced']}
        _require(isinstance(raw_basis, dict) and all(identity in lines and lines[identity]['priced'] for identity in raw_basis),
                 '原整单费用分摊基数无效。')
        global_count = sum(entry['line_id'] is None for entry in entries)
        basis = {identity: _number(value, '原分摊基数') for identity, value in raw_basis.items()}
        basis_kind = existing.get('allocation_basis_kind', 'original_priced_line_total_cost') if existing else 'original_priced_line_total_cost'
        amounts, allocations = {}, []
        costs = {identity: {key: ZERO for key in COST_KEYS} for identity, line in lines.items()
                 if line['priced'] or identity in adopted}
        # Evaluate local adopted entries before allocating any order-level fee.
        # An unpriced source can use its explicitly adopted direct costs; no
        # fabricated old price or geometric estimate is needed as a basis.
        for entry in entries:
            amount = _amount(entry, lines, result['settings'])
            amounts[entry['id']] = _money(amount)
            if entry['line_id'] is not None:
                costs[entry['line_id']][entry['category'] + '_cost'] += amount
        if global_count and (sum(basis.values(), ZERO) == 0 or basis_kind == 'current_adopted_line_direct_cost'):
            basis = {identity: sum(values.values(), ZERO) for identity, values in costs.items()}
            basis_kind = 'current_adopted_line_direct_cost'
        cells = global_count * len(basis) + (len(costs) if explicit_pricing else 0)
        _require(cells <= MAX_ALLOCATION_CELLS,
                 '整单费用分摊明细数量超出处理范围，请合并同类整单费用或拆单核价。')
        for entry in entries:
            if entry['line_id'] is None:
                amount = Decimal(amounts[entry['id']])
                for identity, share in _allocate(amount, basis).items():
                    costs[identity][entry['category'] + '_cost'] += share
                    allocations.append({'entry_id': entry['id'], 'line_id': identity,
                                        'category': entry['category'], 'amount': _money(share),
                                        'basis': basis_kind})
        direct_basis = {identity: sum(values.values(), ZERO) for identity, values in costs.items()}
        direct_cost = sum(direct_basis.values(), ZERO)
        management_cost = ZERO
        if explicit_pricing:
            quantum = Decimal('1') if explicit_pricing['rounding'] == 'whole_yuan' else CENT
            management_cost = (direct_cost * Decimal(explicit_pricing['management_rate_percent']) / 100).quantize(quantum, rounding=ROUND_HALF_UP)
            if management_cost:
                for identity, share in _allocate(management_cost, direct_basis).items():
                    costs[identity]['other_cost'] += share
                    allocations.append({'entry_id': MANAGEMENT_ID, 'line_id': identity, 'category': 'other',
                                        'amount': _money(share), 'basis': 'current_direct_cost'})
        new = copy.deepcopy(result)
        if explicit_pricing:
            new['settings'].update(pricing_method=explicit_pricing['pricing_method'], rate_percent=explicit_pricing['rate_percent'])
        pricing = new['settings']['pricing_method']
        factor = _number(new['settings']['rate_percent'], '定价比例') / 100
        snapshots = []
        for line in new['lines']:
            identity = line['id']
            if identity not in costs:
                continue
            quantity = Decimal(line['quantity_total'])
            cost = sum(costs[identity].values(), ZERO)
            unit = cost / quantity * (1 + factor) if pricing == 'cost_plus' else cost / quantity / (1 - factor)
            line.update({key: _money(value) for key, value in costs[identity].items()})
            line.update(total_cost=_money(cost), unit_cost=_money(cost / quantity), unit_price=_money(unit),
                        total_price=_money(Decimal(_money(unit)) * quantity), priced=True)
            if identity in adopted:
                line['cost_sheet_adoption'] = {'status': 'pending_review', 'basis': 'manual_adopted',
                                               'native_geometry_approved': False}
                line['notes'] = [*line.get('notes', []), '原未计价行按人工成本预估核价；原几何及工程资料未变，仍待审核。']
            snapshots.append({'line_id': identity, **{key: line[key] for key in
                              (*COST_KEYS, 'total_cost', 'total_price', 'unit_cost', 'unit_price')}})
        priced = [line for line in new['lines'] if line['priced']]
        new['totals'] = {key: _money(sum((Decimal(line[key]) for line in priced), ZERO)) if priced else None
                         for key in (*COST_KEYS, 'total_cost', 'total_price')}
        new['totals'].update(priced_lines=len(priced), unpriced_lines=len(new['lines']) - len(priced))
        new['status'] = 'pending_review'
        terms = _terms(policy.get('customer_terms'), sheet['customer_terms'])
        new['customer_terms'] = copy.deepcopy(terms)
        _refresh_engineering(result, new, entries, originals)
        grouped_before, grouped_after = {}, {}
        for entry in sheet['entries']:
            grouped_before.setdefault((entry['line_id'], entry['category']), []).append(entry)
        for entry in entries:
            grouped_after.setdefault((entry['line_id'], entry['category']), []).append(entry)
        changed = [{'line_id': key[0], 'category': key[1]} for key in
                   sorted(set(grouped_before) | set(grouped_after), key=lambda key: (key[0] or '', key[1]))
                   if grouped_before.get(key) != grouped_after.get(key)]
        new['cost_sheet'] = {'version': VERSION, 'source_digest': digest(result), 'base_run_id': result.get('run_id'),
            'status': 'pending_review', 'entries': entries, 'entry_amounts': amounts, 'allocations': allocations,
            'allocation_basis': {identity: _money(weight) for identity, weight in basis.items()},
            'allocation_basis_kind': basis_kind,
            'adopt_unpriced_line_ids': adopted, 'customer_terms': terms, 'replaced_categories': changed,
            'before_totals': copy.deepcopy(result['totals']), 'after_totals': copy.deepcopy(new['totals']),
            'line_costs': snapshots, 'source_kind': 'manual_adopted', 'engineering_hours_inferred': False}
        if explicit_pricing:
            new['cost_sheet']['pricing'] = copy.deepcopy(explicit_pricing)
        accounting_pricing = explicit_pricing or _current_pricing(new)
        new['cost_sheet']['accounting_summary'] = _accounting(
            direct_cost if priced else None, management_cost if priced else None,
            _number(new['totals']['total_cost'], '完全成本') if priced else None,
            _number(new['totals']['total_price'], '报价合计') if priced else None,
            accounting_pricing, len(new['lines'])-len(priced))
        return new

def _escape(value):
    return html.escape('' if value is None else str(value), quote=True)

def _basis_text(entry, lines, settings):
    def shown(key):
        return _money(_number(entry[key], '核价依据'))
    if entry['method'] == 'retained':
        return '原报价保存金额；未拆造工序、工时'
    if entry['method'] == 'fixed':
        return '人工采用固定金额 ' + shown('amount')
    if entry['method'] == 'quantity_rate':
        return '本单工程量 ' + shown('quantity') + ' ' + entry['unit'] + ' × ' + shown('rate')
    if entry['method'] == 'material':
        return '(' + shown('net_kg') + ' + ' + shown('loss_kg') + ') kg × ' + shown('unit_price') + ' − ' + shown('loss_kg') + ' kg × 废料回收价 ' + shown('scrap_price')
    quantity = lines[entry['line_id']]['quantity_total'] if entry['line_id'] else settings['order_sets']
    return '模具 ' + entry['tooling_id'] + ' 总费用 ' + shown('tooling_amount') + ' ÷ 摊销总量 ' + shown('amortization_quantity') + ' × 本单数量 ' + str(quantity)

def _reference_html(reference):
    if not reference['items']:
        return '<p data-factory-reference>工厂核价参考未配置；请按本订单录入并审核材料与工序费用。</p>'
    rows = []
    for item in reference['items']:
        source = item['source']
        rows.append('<tr><td>' + _escape(item['label']) + '</td><td class="number">'
            + _escape(item['sample_amount']) + '</td><td>' + _escape(item['sample_label'])
            + '</td><td>' + _escape(source['sheet'] + ' · ' + ', '.join(source['cells']))
            + '</td><td>' + _escape(item['note']) + '</td></tr>')
    return ('<details data-factory-reference><summary>客户核价样表参考 · 8类成本来源（未自动采用）</summary><p>'
        + _escape(reference['source']['name']) + ' · 5页硬编码、无公式；以下是样品估算，不是本单采用费率。</p>'
        + '<small>来源SHA256：' + _escape(reference['source']['sha256']) + '</small>'
        + '<table><thead><tr><th>类别</th><th>样品金额（元）</th><th>样表说明</th><th>来源</th><th>本单采用前需核对</th></tr></thead><tbody>'
        + ''.join(rows) + '</tbody></table><ul>' + ''.join('<li>' + _escape(note) + '</li>' for note in reference['limitations']) + '</ul></details>')

def render_cost_sheet_html(result):
    """Internal complete worksheet, distinct from a customer-facing quotation."""
    sheet = build_cost_sheet(result)
    lines = {line['id']: line for line in result['lines']}
    kinds = {'legacy_estimate': '沿用原估算', 'engineering_adopted': '沿用已采用工程核价', 'manual_adopted': '人工采用，待审核'}
    rows = []
    for entry in sheet['entries']:
        line = lines.get(entry['line_id'])
        source = (entry['line_id'] + ' · ' + str(line.get('part_name') or '') + ' · ' + str(line.get('document') or '')) if line else '整单费用，按原已计价行成本分摊'
        rows.append('<tr><td>' + _escape(source) + '</td><td>' + _escape(LABELS[entry['category']])
            + '</td><td>' + _escape(entry['label']) + '</td><td>'
            + _escape(_basis_text(entry, lines, result['settings'])) + '</td><td class="number">'
            + _escape(sheet['entry_amounts'][entry['id']]) + '</td><td>'
            + _escape(kinds[entry['source_kind']]) + '<br>' + _escape(entry.get('note', '')) + '</td></tr>')
    accounting = sheet['accounting_summary']
    saved_pricing = (result.get('cost_sheet') or {}).get('pricing')
    if saved_pricing is not None:
        rows.append('<tr data-management-fee><td>整单自动分摊</td><td>其他</td><td>管理费</td><td>当前直接成本 '
            + _escape(accounting['direct_cost']) + ' × ' + _escape(_money(Decimal(saved_pricing['management_rate_percent'])))
            + '%；管理费按' + ('整元' if saved_pricing['rounding'] == 'whole_yuan' else '分') + '四舍五入</td><td class="number">'
            + _escape(accounting['management_cost']) + '</td><td>本次明确采用的口径；重新编辑从直接成本重算，不重复计费</td></tr>')
    totals = ''.join('<tr><th>' + _escape(LABELS[c]) + '</th><td class="number">'
                     + _escape(sheet['totals'].get(c + '_cost')) + '</td></tr>' for c in CATEGORIES)
    totals += ''.join('<tr><th>' + label + '</th><td class="number">' + _escape(sheet['totals'].get(key)) + '</td></tr>'
                     for key, label in (('total_cost', '成本合计'), ('total_price', '报价合计')))
    accounting_html = ('<section data-accounting-summary><h2>核算汇总 · 当前已计价范围</h2><table>'
        + ''.join('<tr><th>' + label + '</th><td class="number">' + _escape(accounting[key] if accounting[key] is not None else '未计价')
                  + '</td></tr>' for key, label in (('direct_cost', '直接成本'), ('management_cost', '管理费'),
                        ('full_cost', '完全成本'), ('profit', '利润（按实际报价减完全成本）'), ('quoted_price', '报价合计')))
        + '</table><small>报价单价保留两位后乘数量；管理费取整规则不改变利润及单价的分币口径。'
        + ('尚有未计价行，本汇总不代表整单完成报价。' if accounting['unpriced_lines'] else '') + '</small></section>')
    terms = ''.join('<dt>' + label + '</dt><dd>' + _escape(sheet['customer_terms'][key] or '未设置') + '</dd>'
                    for key, label in zip(TERMS, ('交期', '付款', '有效期', '税费与运输')))
    allocations = result.get('cost_sheet', {}).get('allocations', [])
    allocation_rows = ''.join('<tr><td>' + _escape(item['entry_id']) + '</td><td>' + _escape(item['line_id'])
                              + '</td><td>' + _escape(item['category']) + '</td><td class="number">'
                              + _escape(item['amount']) + '</td></tr>' for item in allocations)
    allocation_html = ('<h2>整单费用分摊到分</h2><table><tr><th>费用</th><th>零件</th><th>类别</th><th>金额</th></tr>'
                       + allocation_rows + '</table>') if allocation_rows else ''
    body = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>内部核价明细</title><style>body{font:14px/1.6 system-ui,sans-serif;color:#24364a;margin:28px}h1{font-size:26px}table{border-collapse:collapse;width:100%;margin:18px 0}th,td{border:1px solid #d8e2eb;padding:9px;text-align:left;vertical-align:top}th{background:#f1f6fa}.number{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}small{color:#60758c;overflow-wrap:anywhere}dt{font-weight:600}dd{margin:0 0 8px}@media print{body{margin:0;font-size:10px}thead{display:table-header-group}tr{break-inside:avoid}h1{font-size:20px}}</style></head><body>'
        '<h1>内部核价明细 · 待审核</h1><p>' + _escape(result.get('name') or '报价') + ' · '
        + _escape(result['settings']['order_sets']) + ' 套。工序目录无默认采用费率；原估算与人工采用分别保留。</p>'
        '<small>当前报价摘要：' + _escape(sheet['base_digest']) + '</small><table><thead><tr><th>零件／来源</th><th>类别</th><th>费用项目</th><th>本单计量与计算依据</th><th>金额（元）</th><th>来源与说明</th></tr></thead><tbody>'
        + ''.join(rows) + '</tbody></table>' + accounting_html + '<details><summary>成本分类汇总</summary><table>' + totals + '</table></details>'
        + allocation_html + _reference_html(sheet['factory_reference']) + '<dl>' + terms
        + '</dl><p>新增费用为人工采用，未推断真实工时；不表示CAD工程认可、供应商成交确认或商务批准。未经显式采用的原未计价项仍不计价。</p></body></html>')
    return body.encode('utf-8')
