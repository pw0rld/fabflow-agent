"""Source-bound STEP quotation preparation; geometry never implies procurement usage."""
from __future__ import annotations
import copy
import csv
from decimal import Decimal, localcontext
import hashlib
import html
import io
import json
from pathlib import Path
import re
import uuid
from cad_bom import group_material_summary, group_quantity_summary
from cad_manufacturing import group_thickness_summary
from cad_weight import weight_summary
from prep_record import atomic_write, number, stamp
from quotation import money, validate_policy
from workbench_store import WorkbenchError

QUOTE_ID = re.compile(r'CQ-[0-9a-f]{32}\Z')
REQUEST_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')
LINE_KEYS = {'group_id', 'selected', 'mode', 'quantity_per_set', 'material', 'material_form', 'thickness_mm', 'material_usage', 'material_rate', 'purchase_price', 'operations', 'setup_fee', 'inclusions', 'price_refs', 'net_weight_kg'}
INPUT_KEYS = {'order_sets', 'product_family', 'lines', 'pricing', 'shared_fees', 'remarks', 'review_confirmed'}

def require(condition, message, code='invalid_argument', status=400):
    if not condition:
        raise WorkbenchError(message, code, status)

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def bounded(value, label, limit=2000):
    require(isinstance(value, str) and len(value) <= limit and not any(ord(c) < 32 and c not in '\n\r\t' for c in value), label + '格式无效。')
    return value

def decimal_input(value, label):
    if value is None or value == '':
        return None
    require(isinstance(value, str) and len(value) <= 40 and re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', value), label + '须为非负十进制数字。')
    require(number(value) < Decimal('1e12'), label + '超出范围。')
    return value

def quantity(value, label):
    require(value is None or type(value) is int and 0 < value <= 1000000, label + '须为正整数或留空。')
    return value

def normalize_inputs(value, groups):
    require(isinstance(value, dict) and set(value) == INPUT_KEYS, '报价输入字段不完整或无效。')
    result = copy.deepcopy(value)
    quantity(result['order_sets'], '订单套数')
    bounded(result['product_family'], '产品类别', 200)
    bounded(result['remarks'], '报价说明', 12000)
    require(type(result['review_confirmed']) is bool, '请明确报价输入核对状态。')
    pricing = result['pricing']
    require(isinstance(pricing, dict) and set(pricing) == {'method', 'rate_percent', 'tax_excluded', 'freight_excluded'}, '定价口径格式无效。')
    require(pricing['method'] is None or isinstance(pricing['method'], str) and pricing['method'] in {'cost_plus', 'gross_margin'}, '定价方式无效。')
    pricing['rate_percent'] = decimal_input(pricing['rate_percent'], '定价比例')
    require(type(pricing['tax_excluded']) is bool and type(pricing['freight_excluded']) is bool, '税运口径格式无效。')
    lines = result['lines']
    require(isinstance(lines, list) and len(lines) <= 200, '报价零件列表无效。')
    valid_ids = {g['id'] for g in groups}
    seen = set()
    for line in lines:
        require(isinstance(line, dict) and not set(line).difference(LINE_KEYS) and (LINE_KEYS - {'material_form', 'price_refs', 'net_weight_kg'}).issubset(line), '零件报价字段不完整或无效。')
        gid = line['group_id']
        require(isinstance(gid, str) and gid in valid_ids and gid not in seen, '报价零件已变更或重复，请重新载入。')
        seen.add(gid)
        require(type(line['selected']) is bool and isinstance(line['mode'], str) and line['mode'] in {'make', 'buy', 'unresolved'}, '零件报价类型无效。')
        quantity(line['quantity_per_set'], '每套数量')
        for key in ('material', 'inclusions'):
            bounded(line[key], key, 4000 if key == 'inclusions' else 200)
        line.setdefault('material_form', '')
        bounded(line['material_form'], '材料形态', 200)
        for key in ('thickness_mm', 'material_usage', 'material_rate', 'purchase_price', 'setup_fee'):
            line[key] = decimal_input(line[key], key)
        line['net_weight_kg'] = decimal_input(line.get('net_weight_kg'), '单件净重')
        require(line['net_weight_kg'] is None or number(line['net_weight_kg']) > 0,
                '单件净重须为正数；未知请留空。')
        operations = line['operations']
        require(isinstance(operations, list) and len(operations) <= 40, '工序列表无效。')
        names = set()
        for op in operations:
            require(isinstance(op, dict) and set(op) == {'name', 'rate', 'basis', 'inclusions'}, '工序费用字段无效。')
            bounded(op['name'], '工序名称', 100)
            require(op['name'].strip() and op['name'].strip().casefold() not in names, '工序名称为空或重复。')
            names.add(op['name'].strip().casefold())
            require(isinstance(op['basis'], str) and op['basis'] in {'CNY/piece', 'CNY/order'}, '仅支持明确的按件或按单加工费。')
            op['rate'] = decimal_input(op['rate'], '工序费率')
            bounded(op['inclusions'], '工序包含项', 2000)
        refs = line.setdefault('price_refs', [])
        require(isinstance(refs, list) and len(refs) <= 42, '价格版本引用无效。')
        ref_keys = set()
        for ref in refs:
            require(isinstance(ref, dict) and set(ref) == {'entry_id', 'version'}, '价格版本引用字段无效。')
            require(isinstance(ref['entry_id'], str) and re.fullmatch(r'price_[0-9a-f]{32}', ref['entry_id']) and type(ref['version']) is int and 0 < ref['version'] <= 1000000, '价格版本引用无效。')
            key = (ref['entry_id'], ref['version'])
            require(key not in ref_keys, '价格版本引用重复。')
            ref_keys.add(key)
    require(seen == valid_ids, '报价清单与当前分组不一致，请同步分组。')
    shared = result['shared_fees']
    require(isinstance(shared, list) and len(shared) <= 40, '整单费用列表无效。')
    names = set()
    for row in shared:
        require(isinstance(row, dict) and set(row) == {'name', 'value', 'inclusions'}, '整单费用字段无效。')
        bounded(row['name'], '整单费用名称', 200)
        require(row['name'].strip() and row['name'].strip().casefold() not in names, '整单费用名称为空或重复。')
        names.add(row['name'].strip().casefold())
        row['value'] = decimal_input(row['value'], '整单费用')
        bounded(row['inclusions'], '整单费用包含项', 2000)
    return result

class CadQuotationService:
    def __init__(self, manufacturing, catalog=None):
        self.manufacturing, self.cad, self.catalog = manufacturing, manufacturing.cad, catalog

    def _folder(self, document_id, create=False):
        folder = self.cad._folder(document_id) / 'quotation'
        require(not folder.is_symlink(), '报价目录无效。')
        if create:
            folder.mkdir(mode=0o700, exist_ok=True)
        return folder

    def _source(self, document_id):
        document = self.cad._read(document_id)
        self.cad._original(document)
        state = self.manufacturing._read(document_id, document['sha256'])
        require(state.get('geometry') and state['groups'], '请先完成 STEP 实体读取与分组。')
        info = state['geometry']
        return {'document_id': document_id, 'filename': document.get('name', document_id), 'source_sha256': document['sha256'], 'revision': state['revision'], 'status': state['status'], 'geometry_run_id': info['run_id'], 'groups': copy.deepcopy(state['groups']), 'thickness_measurements': copy.deepcopy(info.get('thickness_measurements', {})), 'material_measurements': copy.deepcopy(info.get('material_measurements', {})), 'solid_identities': copy.deepcopy(info.get('solid_identities', {})), 'geometry_summary': copy.deepcopy(info.get('summary', {})), 'bom_settings': copy.deepcopy(state.get('bom_settings', {'order_sets': None}))}

    @staticmethod
    def _new_line(group):
        mode = group['classification'] if group['classification'] in {'make', 'buy'} else 'unresolved'
        return {'group_id': group['id'], 'selected': mode in {'make', 'buy'}, 'mode': mode, 'quantity_per_set': group.get('quantity_per_set'), 'material': group.get('material') or '', 'material_form': '', 'thickness_mm': str(group['thickness_mm']) if group.get('thickness_mm') is not None else None, 'material_usage': None, 'material_rate': None, 'purchase_price': None, 'operations': [], 'setup_fee': None, 'inclusions': '', 'price_refs': [], 'net_weight_kg': None}

    def _default(self, source):
        return {'schema_version': 1, 'revision': 0, 'source': source, 'demo': None, 'updated_at': None, 'inputs': {'order_sets': source['bom_settings']['order_sets'], 'product_family': '', 'lines': [self._new_line(g) for g in source['groups']], 'pricing': {'method': None, 'rate_percent': None, 'tax_excluded': False, 'freight_excluded': False}, 'shared_fees': [], 'remarks': '', 'review_confirmed': False}}

    def _read(self, document_id, source):
        path = self._folder(document_id) / 'inputs.json'
        if not path.exists():
            return self._default(source)
        require(not path.is_symlink() and path.stat().st_size <= 2000000, '报价输入记录无效。')
        value = json.loads(path.read_text())
        require(value.get('schema_version') == 1 and value['source']['source_sha256'] == source['source_sha256'], '报价输入来源不一致。')
        normalize_inputs(value['inputs'], value['source']['groups'])
        return value

    def _save(self, document_id, state):
        state['updated_at'] = stamp()
        content = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        require(len(content.encode()) <= 2000000, '报价参数过多，请减少明细或缩短备注。')
        atomic_write(self._folder(document_id, True) / 'inputs.json', content)

    @staticmethod
    def _current(state, source):
        return digest(state['source']) == digest(source)

    def _guard(self, state, source, payload, extra=()):
        require(isinstance(payload, dict) and set(payload) == {'expected_revision', 'source_revision', *extra}, '报价请求字段无效。')
        require(type(payload['expected_revision']) is int and type(payload['source_revision']) is int, '报价修订号无效。')
        require(payload['expected_revision'] == state['revision'], '报价输入已更新，请重新载入。', 'revision_conflict', 409)
        require(payload['source_revision'] == source['revision'], 'STEP 分组已更新，请重新载入并同步。', 'revision_conflict', 409)
        require(source['status'] == 'ready', 'STEP 正在处理，请完成后再报价。', 'source_busy', 409)

    def _evidence(self, source):
        result = {}
        for group in source['groups']:
            result[group['id']] = {'solid_ids': group['solid_ids'], 'group_status': group['status'],
                'quantity_candidate': group_quantity_summary(group, source['solid_identities']),
                'material_candidate': group_material_summary(group, source['material_measurements']),
                'thickness_candidate': group_thickness_summary(group, source['thickness_measurements']),
                'notes': group.get('notes', ''), 'usage_note': '几何体积、面积或板厚不等于采购用料；此处不自动推算每件用量。'}
        return result

    def _catalog(self, inputs):
        result = {'status': 'empty', 'matches': {}}
        if self.catalog is None:
            return result
        for line in inputs['lines']:
            if not line['selected'] or line['mode'] != 'make':
                continue
            context = {'material_grade': line['material'] or None, 'material_form': line.get('material_form') or None,
                'thickness': line['thickness_mm'], 'quantity': (line['quantity_per_set'] * inputs['order_sets']) if line['quantity_per_set'] and inputs['order_sets'] else None,
                'product_family': inputs['product_family'] or None, 'process_route': [row['name'] for row in line['operations']]}
            match = self.catalog.match(context)
            result['matches'][line['group_id']] = match
            if any(row.get('candidates') for row in match.get('results', [])):
                result['status'] = 'available'
        return result

    @staticmethod
    def _validation(inputs, source, *, source_current=True):
        issues = []
        def add(field, message, group_id=None):
            issues.append({'field': field, 'message': message, **({'group_id': group_id} if group_id else {})})
        if not source_current:
            add('source', 'STEP 分组或属性已变化，请先同步分组并重新核对报价输入。')
        if source['status'] != 'ready':
            add('source', 'STEP 仍在处理，暂不能出报价。')
        if inputs['order_sets'] is None:
            add('order_sets', '请填写订单套数。')
        selected = [line for line in inputs['lines'] if line['selected']]
        if not selected:
            add('lines', '请至少选择一个报价零件。')
        for line in selected:
            gid = line['group_id']
            def missing(key, label, positive=False):
                value = line[key]
                if value is None or value == '' or positive and number(value) <= 0:
                    add(key, label, gid)
            missing('quantity_per_set', '请填写每套数量。')
            if line['mode'] == 'unresolved':
                add('mode', '请选择自制或外购计价方式；分组分类不会因此被修改。', gid)
            elif line['mode'] == 'buy':
                missing('purchase_price', '请填写外购单价；免费供料请明确填写 0。')
            elif line['mode'] == 'make':
                missing('material', '请填写本次计价采用的材料。')
                missing('material_usage', '请填写每件采购用料 kg；不能把 CAD 体积直接当作采购用量。', True)
                missing('material_rate', '请填写材料单价 CNY/kg。')
                missing('setup_fee', '请填写该零件批次准备费；不收费请明确填写 0。')
                if not line['operations']:
                    add('operations', '请添加本次计价包含的加工工序。', gid)
                for index, op in enumerate(line['operations']):
                    if op['rate'] is None:
                        add(f'operations.{index}.rate', op['name'] + '缺少费率。', gid)
                    if not op['inclusions'].strip():
                        add(f'operations.{index}.inclusions', op['name'] + '需要说明包含范围。', gid)
                    if op['basis'] == 'CNY/order' and op['rate'] is not None and number(op['rate']) > 0 and line['setup_fee'] is not None and number(line['setup_fee']) > 0:
                        add('setup_fee', '按单工序费与准备费同时大于零，请合并费用或明确拆分后采用支持的计价方式。', gid)
            if not line['inclusions'].strip():
                add('inclusions', '请说明该零件费用来源、包含项及未包含项。', gid)
        for index, row in enumerate(inputs['shared_fees']):
            if row['value'] is None or not row['inclusions'].strip():
                add(f'shared_fees.{index}', '整单费用需要金额和包含范围。')
        pricing = inputs['pricing']
        if pricing['method'] is None:
            add('pricing.method', '请选择本单定价方式。')
        if pricing['rate_percent'] is None:
            add('pricing.rate_percent', '请填写本单定价比例，不预设 20%。')
        elif pricing['method'] == 'gross_margin' and number(pricing['rate_percent']) >= 100:
            add('pricing.rate_percent', '目标毛利率必须小于 100%。')
        if not pricing['tax_excluded'] or not pricing['freight_excluded']:
            add('pricing.tax', '请确认当前报价为人民币、未税且不含运费。')
        if not inputs['review_confirmed']:
            add('review_confirmed', '请核对本次报价的范围、数量、费用来源及包含项。')
        return {'ready': not issues, 'issues': issues}

    def _history(self, document_id, state, source):
        folder = self._folder(document_id) / 'quotes'
        if not folder.exists():
            return []
        require(not folder.is_symlink(), '报价历史目录无效。')
        files = list(folder.glob('CQ-*.json'))
        require(len(files) <= 1000, '报价历史过多，请归档后继续。')
        rows = [self._links(document_id, self._read_quote_file(path), state, source, compact=True) for path in files]
        return sorted(rows, key=lambda row: (row['created_at'], row['id']), reverse=True)

    def _public(self, document_id, state, source):
        current = self._current(state, source)
        return {'document_id': document_id, 'source_sha256': source['source_sha256'], 'revision': state['revision'],
            'source_revision': source['revision'], 'source_current': current, 'inputs': copy.deepcopy(state['inputs']),
            'groups': copy.deepcopy(state['source']['groups']), 'current_groups': copy.deepcopy(source['groups']),
            'geometry_evidence': self._evidence(state['source']), 'demo': copy.deepcopy(state.get('demo')),
            'catalog': self._catalog(state['inputs']), 'quotes': self._history(document_id, state, source),
            'weight_summary': weight_summary(state['inputs']),
            'validation': self._validation(state['inputs'], source, source_current=current)}

    def get(self, document_id):
        with self.cad._lock(document_id):
            source = self._source(document_id)
            return self._public(document_id, self._read(document_id, source), source)

    def save_inputs(self, document_id, payload):
        with self.cad._lock(document_id):
            source = self._source(document_id)
            state = self._read(document_id, source)
            self._guard(state, source, payload, ('inputs',))
            require(self._current(state, source), 'STEP 已变化，请先同步分组。', 'revision_conflict', 409)
            state['inputs'] = normalize_inputs(payload['inputs'], source['groups'])
            state['revision'] += 1
            self._save(document_id, state)
            return self._public(document_id, state, source)

    def rebase(self, document_id, payload):
        with self.cad._lock(document_id):
            source = self._source(document_id)
            state = self._read(document_id, source)
            self._guard(state, source, payload)
            old_groups = {group['id']: group for group in state['source']['groups']}
            old_lines = {line['group_id']: line for line in state['inputs']['lines']}
            state['inputs']['lines'] = [copy.deepcopy(old_lines[group['id']])
                if group['id'] in old_lines and set(old_groups[group['id']]['solid_ids']) == set(group['solid_ids'])
                else self._new_line(group) for group in source['groups']]
            state['inputs']['review_confirmed'] = False
            state['source'] = source
            state['revision'] += 1
            self._save(document_id, state)
            return self._public(document_id, state, source)

    def demo_inputs(self, document_id, payload):
        """Opt-in synthetic price/usage fixture; never alters CAD engineering data."""
        with self.cad._lock(document_id):
            source = self._source(document_id)
            state = self._read(document_id, source)
            self._guard(state, source, payload)
            require(self._current(state, source), 'STEP 已变化，请先同步分组。', 'revision_conflict', 409)
            inputs = copy.deepcopy(state['inputs'])
            inputs['order_sets'] = inputs['order_sets'] or 10
            inputs['product_family'] = inputs['product_family'] or 'STEP 机箱演示'
            inputs['review_confirmed'] = False
            applied = []
            for line in inputs['lines']:
                if not line['selected'] or line['mode'] == 'unresolved':
                    continue
                line['quantity_per_set'] = line['quantity_per_set'] or 1
                line['inclusions'] = line['inclusions'] or '演示假设，非工厂实际成本；本行包含所列采购/材料、加工及准备费，不含税费、运费及未列工序。'
                if line['mode'] == 'buy':
                    if line['purchase_price'] is None:
                        line['purchase_price'] = '8'
                        applied.append(line['group_id'] + ': purchase_price=8')
                else:
                    line['material'] = line['material'] or '演示钢材（牌号待核对）'
                    line['material_form'] = line.get('material_form') or '板材'
                    for key, value in [('material_usage', '0.5'), ('material_rate', '6'), ('setup_fee', '20')]:
                        if line[key] is None:
                            line[key] = value
                            applied.append(line['group_id'] + ': ' + key + '=' + value)
                    if not line['operations']:
                        line['operations'] = [{'name': '激光切割（演示综合加工费）', 'rate': '3', 'basis': 'CNY/piece', 'inclusions': '演示按件综合加工费，含本件示例切割与去毛刺；非实际切割路径或工时计算。'}]
                        applied.append(line['group_id'] + ': operation=3 CNY/piece')
            note = ('演示预设 step-quote-demo-v1：仅补缺失项，已有输入与选择范围保留。缺失订单套数按 10、每套数量按 1；自制件采购用料 0.5 kg/件、材料 6 元/kg、综合加工 3 元/件、准备费 20 元/批；外购 8 元/件。以上为演示假设，不是 STEP 测量或工厂报价。采用值以本单输入为准。定价比例需另行填写。')
            if note not in inputs['remarks']:
                inputs['remarks'] = (inputs['remarks'] + '\n' + note).strip()
            state.update(inputs=inputs, demo={'id': 'step-quote-demo-v1', 'applied_at': stamp(), 'simulated': True, 'applied_fields': applied}, revision=state['revision'] + 1)
            self._save(document_id, state)
            return self._public(document_id, state, source)

    def _price_snapshots(self, inputs):
        matches = self._catalog(inputs)['matches']
        snapshots = []
        for line in inputs['lines']:
            if not line['selected']:
                continue
            candidates = [candidate for result in matches.get(line['group_id'], {}).get('results', []) for candidate in result.get('candidates', [])]
            for ref in line.get('price_refs', []):
                row = next((item for item in candidates if item['entry_id'] == ref['entry_id'] and item['version'] == ref['version']), None)
                require(row is not None, '所选价格版本已停用、过期或条件不匹配，请重新匹配。', 'price_changed', 409)
                if row['kind'] == 'material':
                    adopted = line['material_rate']
                    require(row['unit'] == 'CNY/kg', '材料计价单位不匹配。')
                elif row['kind'] == 'setup':
                    adopted = line['setup_fee']
                else:
                    op = next((op for op in line['operations'] if op['name'] == row['operation'] and op['basis'] == row['unit']), None)
                    require(op is not None, '所选工序价格与当前工序不匹配。')
                    adopted = op['rate']
                require(adopted is not None and number(adopted) == number(row['rate']), '采用费率与引用的价格版本不同，请移除旧引用或重新选择。')
                snapshots.append({'group_id': line['group_id'], **copy.deepcopy(row)})
        return snapshots

    def _calculate(self, state, source):
        inputs = state['inputs']
        validation = self._validation(inputs, source, source_current=self._current(state, source))
        require(validation['ready'], '报价资料尚未完整：' + '；'.join(row['message'] for row in validation['issues'][:5]), 'quotation_incomplete', 409)
        spec = inputs['pricing']
        policy = {'schema_version': 1, 'pricing_method': spec['method'], 'rate': str(number(spec['rate_percent']) / 100),
            'currency': 'CNY', 'tax_included': False, 'freight_included': False, 'simulated': True,
            'actor': '当前操作者（姓名未提供）', 'source_text': canonical({'pricing': spec, 'review_confirmed': inputs['review_confirmed']})}
        rate = validate_policy(policy)
        weights = weight_summary(inputs)
        groups = {row['id']: row for row in source['groups']}
        def priced(value):
            amount = value * (1 + rate) if policy['pricing_method'] == 'cost_plus' else value / (1 - rate)
            require(0 <= amount < Decimal('1e26'), '定价结果超出范围，请核对比例与成本。')
            return amount
        with localcontext() as context:
            context.prec = 64
            rows = []
            totals = {key: Decimal(0) for key in ('material_cost', 'process_cost', 'setup_cost', 'shared_cost', 'total_cost', 'total_price')}
            for line in inputs['lines']:
                if not line['selected']:
                    continue
                count = line['quantity_per_set'] * inputs['order_sets']
                require(0 < count <= 1000000000000, '零件总数量超出范围。')
                material = process = setup = Decimal(0)
                breakdown = []
                if line['mode'] == 'make':
                    material = number(line['material_usage']) * number(line['material_rate']) * count
                    breakdown.append({'name': '材料', 'basis': 'CNY/kg', 'rate': line['material_rate'], 'quantity': str(number(line['material_usage']) * count), 'amount': money(material)})
                    setup = number(line['setup_fee'])
                    for op in line['operations']:
                        op_count = count if op['basis'] == 'CNY/piece' else 1
                        amount = number(op['rate']) * op_count
                        process += amount
                        breakdown.append({'name': op['name'], 'basis': op['basis'], 'rate': op['rate'], 'quantity': str(op_count), 'amount': money(amount), 'inclusions': op['inclusions']})
                    breakdown.append({'name': '批次准备费', 'basis': 'CNY/order', 'rate': line['setup_fee'], 'quantity': '1', 'amount': money(setup)})
                else:
                    material = number(line['purchase_price']) * count
                    breakdown.append({'name': '外购', 'basis': 'CNY/piece', 'rate': line['purchase_price'], 'quantity': str(count), 'amount': money(material)})
                cost = material + process + setup
                require(cost < Decimal('1e24'), '报价金额超出范围。')
                unit_cost = cost / count
                unit_price = number(money(priced(unit_cost)))
                total_price = unit_price * count
                rows.append({'group_id': line['group_id'], 'name': groups[line['group_id']]['name'], 'mode': line['mode'],
                    'quantity': count, 'quantity_per_set': line['quantity_per_set'], 'material': line['material'],
                    'thickness_mm': line['thickness_mm'], 'material_usage': line['material_usage'], 'material_cost': money(material),
                    'process_cost': money(process), 'setup_cost': money(setup), 'unit_cost': money(unit_cost),
                    'raw_total_cost': str(cost), 'total_cost': money(cost), 'unit_price': money(unit_price),
                    'total_price': money(total_price), 'breakdown': breakdown, 'inclusions': line['inclusions'],
                    **weights['lines'][line['group_id']]})
                for key, amount in [('material_cost', material), ('process_cost', process), ('setup_cost', setup), ('total_cost', cost), ('total_price', total_price)]:
                    totals[key] += amount
            shared_cost = sum((number(row['value']) for row in inputs['shared_fees']), Decimal(0))
            shared_price = number(money(priced(shared_cost)))
            totals['shared_cost'] = shared_cost
            totals['total_cost'] += shared_cost
            totals['total_price'] += shared_price
            require(totals['total_cost'] < Decimal('1e26') and totals['total_price'] < Decimal('1e26'), '报价总金额超出范围。')
            return {'lines': rows, 'totals': {key: money(value) for key, value in totals.items()},
                'shared_price': money(shared_price), 'policy': policy, 'price_snapshots': self._price_snapshots(inputs),
                'weight_summary': weights,
                'scope': '所选零件报价', 'warnings': ['模拟报价草稿，尚未工程与商务审核。',
                    '采购用料和费用来自本次报价输入，不由 CAD 体积、展开或工时自动推算。',
                    '仅覆盖所选零件，未选及待设计分组不包含在总额内。', '人民币、未税、不含运费；交期、付款条件及有效期待约定。'],
                'rounding_rule': 'Decimal ROUND_HALF_UP；报价单价先保留两位，行金额=单价×数量；整单费用加价后取两位并收取一次。'}

    def estimate(self, document_id, payload):
        with self.cad._lock(document_id):
            source = self._source(document_id)
            state = self._read(document_id, source)
            self._guard(state, source, payload)
            return self._calculate(state, source)

    @staticmethod
    def _read_quote_file(path):
        require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 4000000, '报价文件不存在或无效。', 'not_found', 404)
        envelope = json.loads(path.read_text())
        require(isinstance(envelope, dict) and set(envelope) == {'sha256', 'quote'} and digest(envelope['quote']) == envelope['sha256'], '报价快照校验失败。', 'integrity_error', 409)
        require(envelope['quote']['id'] + '.json' == path.name, '报价文件编号不匹配。')
        return envelope['quote']

    def _links(self, document_id, quote, state, source, compact=False):
        keys = ('id', 'created_at', 'status', 'totals', 'input_revision', 'source_revision')
        result = {key: copy.deepcopy(quote[key]) for key in keys} if compact else copy.deepcopy(quote)
        prefix = '/workbench/api/cad/documents/' + document_id + '/quotation/quotes/' + quote['id'] + '/'
        result.update(quote_url=prefix + 'quote.html', cost_url=prefix + 'cost.csv', bom_url=prefix + 'bom.csv', json_url=prefix + 'quote.json',
            source_current=quote['input_revision'] == state['revision'] and digest(quote['source_snapshot']) == digest(source),
            cad_source_current=digest(quote['source_snapshot']) == digest(source))
        return result

    def issue(self, document_id, payload):
        with self.cad._lock(document_id):
            source = self._source(document_id)
            state = self._read(document_id, source)
            require(isinstance(payload, dict) and set(payload) == {'expected_revision', 'source_revision', 'request_id'} and
                type(payload.get('expected_revision')) is int and type(payload.get('source_revision')) is int and
                isinstance(payload.get('request_id'), str) and REQUEST_ID.fullmatch(payload['request_id']), '报价请求编号或修订号无效。')
            folder = self._folder(document_id, True) / 'quotes'
            require(not folder.is_symlink(), '报价历史目录无效。')
            folder.mkdir(mode=0o700, exist_ok=True)
            files = list(folder.glob('CQ-*.json'))
            require(len(files) < 1000, '报价历史过多，请归档后继续。')
            for path in files:
                old = self._read_quote_file(path)
                if old['request_id'] == payload['request_id']:
                    require(old['input_revision'] == payload.get('expected_revision') and old['source_revision'] == payload.get('source_revision'), '同一请求编号不能用于另一份输入。', 'request_conflict', 409)
                    return {**self._public(document_id, state, source), 'quote': self._links(document_id, old, state, source)}
            self._guard(state, source, payload, ('request_id',))
            result = self._calculate(state, source)
            quote = {**result, 'schema_version': 1, 'calculator_version': 'cad-quotation-1', 'id': 'CQ-' + uuid.uuid4().hex,
                'created_at': stamp(), 'request_id': payload['request_id'], 'status': '模拟报价草稿，待审核',
                'simulated': True, 'approved': False, 'document_id': document_id, 'source_revision': source['revision'],
                'input_revision': state['revision'], 'source_snapshot': copy.deepcopy(source), 'input_snapshot': copy.deepcopy(state['inputs']),
                'demo_snapshot': copy.deepcopy(state.get('demo')), 'excluded_groups': [{'id': g['id'], 'name': g['name'], 'classification': g['classification']}
                    for g in source['groups'] if not next(line['selected'] for line in state['inputs']['lines'] if line['group_id'] == g['id'])]}
            path = folder / (quote['id'] + '.json')
            require(not path.exists(), '报价编号冲突，请重试。', 'conflict', 409)
            content = json.dumps({'sha256': digest(quote), 'quote': quote}, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
            require(len(content.encode()) <= 4000000, '报价快照过大，请减少明细或缩短备注。')
            atomic_write(path, content)
            return {**self._public(document_id, state, source), 'quote': self._links(document_id, quote, state, source)}

    def get_quote(self, document_id, quote_id):
        require(isinstance(quote_id, str) and QUOTE_ID.fullmatch(quote_id), '报价编号无效。')
        with self.cad._lock(document_id):
            source = self._source(document_id)
            state = self._read(document_id, source)
            value = self._read_quote_file(self._folder(document_id) / 'quotes' / (quote_id + '.json'))
            require(value['document_id'] == document_id, '报价来源不匹配。')
            return self._links(document_id, value, state, source)

    def document(self, document_id, quote_id, filename):
        require(filename in {'quote.html', 'quote.json', 'cost.csv', 'bom.csv'}, '不支持此报价文件。')
        quote = self.get_quote(document_id, quote_id)
        if filename == 'quote.html':
            return self._html(quote).encode(), 'text/html; charset=utf-8', False
        if filename == 'quote.json':
            return (json.dumps(quote, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode(), 'application/json; charset=utf-8', True
        return self._csv(quote, filename).encode('utf-8-sig'), 'text/csv; charset=utf-8', True

    @staticmethod
    def _csv(quote, filename):
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        def safe(value):
            text = '' if value is None else str(value)
            return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) else text
        if filename == 'bom.csv':
            include_weights = isinstance(quote.get('weight_summary'), dict)
            weight_headers = ['单件净重kg', '每套净重kg', '整单净重kg', '原料采购计价总重量kg'] if include_weights else []
            writer.writerow(['分组', '名称', '方式', '每套数量', '套数', '总数量', '材料', '板厚mm', '每件采购用料kg', '状态', *weight_headers])
            for line in quote['lines']:
                weight_values = [line.get(key) for key in ('net_weight_kg', 'set_net_weight_kg', 'total_net_weight_kg', 'total_material_usage_kg')] if include_weights else []
                writer.writerow([safe(v) for v in [line['group_id'], line['name'], line['mode'], line['quantity_per_set'], quote['input_snapshot']['order_sets'], line['quantity'], line['material'], line['thickness_mm'], line['material_usage'], quote['status'], *weight_values]])
        else:
            writer.writerow(['分组', '名称', '费用项', '费率', '单位', '计量数量', '成本金额CNY', '包含说明'])
            for line in quote['lines']:
                for row in line['breakdown']:
                    writer.writerow([safe(v) for v in [line['group_id'], line['name'], row['name'], row['rate'], row['basis'], row['quantity'], row['amount'], row.get('inclusions', line['inclusions'])]])
            for row in quote['input_snapshot']['shared_fees']:
                writer.writerow([safe(v) for v in ['', '整单费用', row['name'], row['value'], 'CNY/order', 1, row['value'], row['inclusions']]])
        return stream.getvalue()

    @staticmethod
    def _html(quote):
        esc = lambda value: html.escape(str(value), quote=True)
        rows = ''.join('<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in [r['name'], r['quantity'], r['material'] or '—', r['unit_price'], r['total_price']]) + '</tr>' for r in quote['lines'])
        costs = ''.join('<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in [r['name'], r['material_cost'], r['process_cost'], r['setup_cost'], r['total_cost'], r['inclusions']]) + '</tr>' for r in quote['lines'])
        excluded = '、'.join(row['name'] for row in quote['excluded_groups']) or '无'
        policy = quote['policy']
        method = '成本加成' if policy['pricing_method'] == 'cost_plus' else '目标毛利率'
        stale = '' if quote['source_current'] else '<p class="notice">历史报价：当前 STEP 或报价输入已变化，本页保留当时报价快照。</p>'
        demo = '<p class="notice">使用演示预设：费用与采购用料属于假设，不能作为工厂实际报价。</p>' if quote.get('demo_snapshot') else ''
        weights = quote.get('weight_summary')
        weight_html = ''
        if isinstance(weights, dict):
            def kg(value):
                return '待补' if value is None else esc(value) + ' kg'
            summary = weights['totals']
            weight_rows = ''.join('<tr>' + ''.join('<td>' + esc(value) + '</td>' for value in
                [line['name'], *('待补' if line.get(key) is None else line[key] for key in
                 ('net_weight_kg', 'set_net_weight_kg', 'total_net_weight_kg', 'total_material_usage_kg'))]) + '</tr>'
                for line in quote['lines'])
            weight_html = ('<h2>重量与采购计价用量</h2><p>整单净重：' + kg(summary['total_net_weight_kg']) +
                '；已知净重小计：' + kg(summary['known_net_weight_kg']) + '；缺重量或数量的物料行：' +
                esc(summary['missing_net_weight_lines']) + '。</p><p>原料采购计价总重量：' +
                kg(summary['total_material_usage_kg']) + '；已知原料采购用量小计：' +
                kg(summary['known_material_usage_kg']) + '。</p><table><thead><tr><th>零件</th>' +
                '<th>单件净重 kg</th><th>每套净重 kg</th><th>整单净重 kg</th><th>原料采购计价总量 kg</th>' +
                '</tr></thead><tbody>' + weight_rows + '</tbody></table><p>净重来自本次录入，仍待核对；原料采购计价用量沿用报价输入，'+
                '两者分别记录。外购件不计入原料采购用量；已知部分重量不代表完整整单重量，未包含包装重量。</p>')
        return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(quote['id'])} · STEP 报价草稿</title><style>body{{font:15px/1.7 system-ui,sans-serif;color:#22354a;background:#f4f7fa;margin:0}}main{{max-width:1100px;margin:28px auto;padding:32px;background:white}}h1{{margin:0}}.notice{{background:#fff4dc;padding:12px}}table{{width:100%;border-collapse:collapse;margin:20px 0}}th,td{{text-align:left;padding:10px;border-bottom:1px solid #dce3ec;vertical-align:top}}.total{{font-size:24px;font-weight:700}}nav{{display:flex;gap:18px;flex-wrap:wrap}}pre{{white-space:pre-wrap;font:inherit}}@media print{{body{{background:white}}main{{margin:0;padding:0}}nav{{display:none}}}}</style><main><h1>所选零件报价草稿</h1><p>{esc(quote['status'])} · {esc(quote['id'])} · {esc(quote['created_at'])}</p>{stale}{demo}<p>来源：{esc(quote['source_snapshot']['filename'])} · STEP 修订 {quote['source_revision']} · 报价输入修订 {quote['input_revision']}</p><nav><a href="{esc(quote['bom_url'])}">下载物料表 CSV</a><a href="{esc(quote['cost_url'])}">下载成本明细 CSV</a><a href="{esc(quote['json_url'])}">下载报价快照</a></nav><table><thead><tr><th>零件</th><th>数量</th><th>材料</th><th>报价单价（元）</th><th>行金额（元）</th></tr></thead><tbody>{rows}</tbody></table><p>整单费用报价：¥{esc(quote['shared_price'])}</p><p class="total">所选零件报价总额 ¥{esc(quote['totals']['total_price'])}</p><p>定价：{method} {esc(number(policy['rate']) * 100)}%；人民币、未税、不含运费。交期、付款条件、有效期待约定。</p><p>未纳入范围：{esc(excluded)}</p>{weight_html}<h2>成本与包含范围（内部核对）</h2><table><tr><th>零件</th><th>材料/外购</th><th>加工</th><th>准备费</th><th>成本合计</th><th>包含说明</th></tr>{costs}</table><p>整单费用成本：¥{esc(quote['totals']['shared_cost'])}；总成本：¥{esc(quote['totals']['total_cost'])}</p><pre>{esc(quote['input_snapshot']['remarks'])}</pre><p>{esc(quote['rounding_rule'])}</p><p>本页基于不可变报价快照。数量、采购用料、费率与包含项仍需工程及商务审核；分组确认不代表生产放行。</p></main></html>'''
