"""Transactional inquiry orders and BOM quantities; no model or price calculation.

An order owns references to immutable documents and single-part tasks. Callback
mutations must change only the supplied in-memory order: they run inside a DB
transaction and must not perform external side effects or start another mutation.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sqlite3
import uuid

from prep_record import RecordError, digest, encoded, identifier, only_keys, require, stamp

MAX_ITEMS = 5000
MAX_QUANTITY = 10**15
MAX_DEPTH = 100


class OrderConflict(RecordError):
    """Compatible with record errors, with an HTTP conflict hint for facades."""
    status = 409

    def __init__(self, message, code='revision_conflict'):
        super().__init__(message)
        self.code = code
        self.details = None


def _text(value, label, maximum=10000, empty=False):
    require(isinstance(value, str) and len(value) <= maximum and (empty or bool(value.strip())),
            label + '须为有效文字。')
    return value


def _quantity(value, label, *, nullable=True):
    if value is None and nullable:
        return None
    require(value is not None, label + '未知，请先补充并核对。')
    require(isinstance(value, (str, int)) and not isinstance(value, bool), label + '须为正整数，未知可留空。')
    require(not isinstance(value, str) or len(value) <= 100, label + '数值过长。')
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise RecordError(label + '须为正整数。') from exc
    require(amount.is_finite() and 0 < amount < MAX_QUANTITY and amount == amount.to_integral_value(),
            label + '须为合理的正整数件数，不能舍入小数件。')
    return int(amount)


def _rows(order, key):
    rows = order.get(key)
    require(isinstance(rows, list) and len(rows) <= MAX_ITEMS, key + '须为列表且不超过 5000 行。')
    ids = set()
    for row in rows:
        require(isinstance(row, dict), key + '行须为对象。')
        row_id = identifier(row.get('id'))
        require(row_id not in ids, key + '存在重复 ID：' + row_id)
        ids.add(row_id)
    return rows, ids


def validate_structure(order):
    """Validate a draft snapshot without filling unknowns or changing its bytes.

    Unknown quantities may be saved as null. Parts are distinct by internal ID,
    never by filename or visible part number alone. Quantity units are whole
    pieces in this first version, including decimal strings such as ``"2.0"``.
    """
    require(isinstance(order, dict), '订单须为对象。')
    documents, document_ids = _rows(order, 'documents')
    parts, part_ids = _rows(order, 'parts')
    lines, _ = _rows(order, 'lines')
    bom, _ = _rows(order, 'bom')
    require(isinstance(order.get('routes'), dict), '工艺路线须为对象。')
    by_id = {part['id']: part for part in parts}
    for part in parts:
        _text(part.get('name'), '零件名称', 200)
        for key in ('part_number', 'version'):
            value = part.get(key)
            require(value is None or isinstance(value, str) and len(value) <= 200, key + '须为文字或未知。')
        require(isinstance(part.get('mode'), str) and part['mode'] in {'make', 'buy', 'assembly'},
                '零件须指定自制、外购或装配模式。')
        if part.get('task_id') is not None:
            identifier(part['task_id'])
        refs = part.get('document_ids', [])
        require(isinstance(refs, list) and len(refs) <= MAX_ITEMS and all(isinstance(ref, str) for ref in refs),
                '关联图纸须为文档 ID 列表。')
        require(len(set(refs)) == len(refs), '零件关联图纸存在重复。')
        require(set(refs) <= document_ids, '零件引用了不存在的文档。')
        if part.get('source_refs') is not None:
            require(isinstance(part['source_refs'], list), '来源引用须为列表。')
            for ref in part['source_refs']:
                require(isinstance(ref, dict), '来源引用须为对象。')
                ref_id = ref.get('document_id')
                require(isinstance(ref_id, str) and ref_id in document_ids, '来源引用了不存在的文档。')
        for key, label in (('purchase_price', '采购价格'), ('assembly_unit_cost', '单件装配费'),
                           ('assembly_setup_fee', '装配准备费')):
            price = part.get(key)
            if price is not None:
                require(isinstance(price, (str, int)) and not isinstance(price, bool) and len(str(price)) <= 100,
                        label + '须为有效金额或未知。')
                try:
                    amount = Decimal(str(price))
                except InvalidOperation as exc:
                    raise RecordError(label + '无效。') from exc
                require(amount.is_finite() and 0 <= amount < MAX_QUANTITY, label + '须为合理的非负金额。')
        if 'cost_confirmed' in part:
            require(type(part['cost_confirmed']) is bool, '费用核对状态须明确为真或假。')
        if part.get('cost_inclusions') is not None:
            _text(part['cost_inclusions'], '费用包含说明', maximum=4000, empty=True)
    for line in lines:
        require(isinstance(line.get('part_id'), str) and line['part_id'] in part_ids, '订单行引用了不存在的零件。')
        require(isinstance(line.get('unit'), str) and line['unit'] in {'piece', 'set'}, '订单行单位须为件或套。')
        _quantity(line.get('quantity'), '订购数量')
    edges = {part_id: [] for part_id in part_ids}
    for row in bom:
        parent, child = row.get('parent_id'), row.get('part_id')
        require(isinstance(parent, str) and isinstance(child, str) and parent in part_ids and child in part_ids,
                'BOM 引用了不存在的父件或子件。')
        require(row.get('unit') == 'piece', '首版 BOM 用量单位仅支持件。')
        _quantity(row.get('quantity'), 'BOM 每套用量')
        require(by_id[parent]['mode'] != 'buy', '外购总成不能同时展开子件收费，请明确选择采购或自制方案。')
        require(by_id[parent]['mode'] == 'assembly', '含 BOM 子件的父项须设为装配模式，避免重复计入父件材料成本。')
        edges[parent].append(child)
    # Check all components without recursive traversal or path explosion.
    indegree = {part_id: 0 for part_id in part_ids}
    for children in edges.values():
        for child in children:
            indegree[child] += 1
    ready = [part_id for part_id, count in indegree.items() if count == 0]
    depth = {part_id: 0 for part_id in part_ids}
    visited = 0
    while ready:
        parent = ready.pop()
        visited += 1
        for child in edges[parent]:
            depth[child] = max(depth[child], depth[parent] + 1)
            require(depth[child] <= MAX_DEPTH, 'BOM 层级超过 100 层。')
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    require(visited == len(part_ids), 'BOM 存在循环引用。')
    require(set(order['routes']) <= part_ids, '工艺路线引用了不存在的零件。')
    return order


def demand(order):
    """Expand independent top-level order lines and add real repeated instances.

    Results include assembly demand for its own assembly operations; the caller
    must not charge the assembly as another material part. Unknown quantities in
    unselected/orphan BOM fragments do not block the selected quote scope.
    """
    validate_structure(order)
    require(bool(order['lines']), '请先提供至少一条顶层订购行。')
    edges = {part['id']: [] for part in order['parts']}
    for row in order['bom']:
        edges[row['parent_id']].append(row)
    total, reachable = {}, set()
    # Topological propagation avoids exponential path traversal for shared parts.
    def mark(part_id):
        if part_id in reachable:
            return
        reachable.add(part_id)
        for row in edges[part_id]:
            mark(row['part_id'])
    for line in order['lines']:
        qty = _quantity(line.get('quantity'), '订购数量', nullable=False)
        part_id = line['part_id']
        total[part_id] = total.get(part_id, 0) + qty
        require(total[part_id] < MAX_QUANTITY, '累计需求数量过大。')
        mark(part_id)
    indegree = {part_id: 0 for part_id in reachable}
    for parent in reachable:
        for row in edges[parent]:
            indegree[row['part_id']] += 1
    ready = [part_id for part_id, count in indegree.items() if count == 0]
    while ready:
        parent = ready.pop()
        for row in edges[parent]:
            qty = _quantity(row.get('quantity'), 'BOM 每套用量', nullable=False)
            child = row['part_id']
            total[child] = total.get(child, 0) + total.get(parent, 0) * qty
            require(total[child] < MAX_QUANTITY, '累计 BOM 需求数量过大。')
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    return {key: str(total[key]) for key in sorted(total)}


class OrderStore:
    def __init__(self, path):
        self.path = Path(path)
        require(not self.path.is_symlink(), '订单数据库不能是符号链接。')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute('PRAGMA journal_mode=WAL')
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS inquiry_orders (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inquiry_order_events (
                    scope TEXT NOT NULL, event_id TEXT NOT NULL, action TEXT NOT NULL,
                    signature TEXT NOT NULL, order_id TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY (scope, event_id)
                );
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                yield connection
                connection.execute('COMMIT')
            except BaseException:
                connection.execute('ROLLBACK')
                raise

    @staticmethod
    def _read(connection, order_id):
        row = connection.execute('SELECT body FROM inquiry_orders WHERE id=?', (identifier(order_id),)).fetchone()
        if row is None:
            raise FileNotFoundError('订单不存在。')
        return json.loads(row['body'])

    @staticmethod
    def _replay(connection, scope, event_id, action, signature):
        row = connection.execute('SELECT * FROM inquiry_order_events WHERE scope=? AND event_id=?',
                                 (scope, event_id)).fetchone()
        if row is not None:
            if row['action'] != action or row['signature'] != signature:
                raise OrderConflict('该事件编号已用于不同的订单操作。', 'event_conflict')
            return True, json.loads(row['result'])
        return False, None

    @staticmethod
    def _event(connection, scope, event_id, action, signature, order_id, result):
        connection.execute('INSERT INTO inquiry_order_events VALUES (?,?,?,?,?,?)',
                           (scope, event_id, action, signature, order_id, encoded(result)))

    def create(self, payload):
        only_keys(payload, {'event_id', 'name', 'inquiry', 'simulated'}, 'create order')
        event_id = identifier(payload.get('event_id'))
        _text(payload.get('name'), '订单名称', 200)
        _text(payload.get('inquiry'), '询价说明', empty=True)
        require(type(payload.get('simulated')) is bool, '请明确是否使用模拟资料。')
        signature = digest(payload)
        with self._transaction() as connection:
            replay, result = self._replay(connection, '__create__', event_id, 'create', signature)
            if replay:
                return result
            now = stamp()
            order = {'id': 'ORD-' + uuid.uuid4().hex[:20], 'name': payload['name'].strip(),
                     'inquiry': payload['inquiry'], 'simulated': payload['simulated'], 'revision': 1,
                     'documents': [], 'parts': [], 'lines': [], 'bom': [], 'routes': {},
                     'discovery': None, 'quotes': [], 'created_at': now, 'updated_at': now}
            connection.execute('INSERT INTO inquiry_orders VALUES (?,?,?)',
                               (order['id'], order['revision'], encoded(order)))
            self._event(connection, '__create__', event_id, 'create', signature, order['id'], order)
            return copy.deepcopy(order)

    def get(self, order_id):
        with self._connection() as connection:
            return self._read(connection, order_id)

    def list(self):
        with self._connection() as connection:
            rows = connection.execute('SELECT body FROM inquiry_orders').fetchall()
        summaries = []
        for row in rows:
            body = json.loads(row['body'])
            summary = {key: body[key] for key in ('id', 'name', 'inquiry', 'simulated', 'revision', 'created_at', 'updated_at')}
            summary.update(documents_count=len(body['documents']), parts_count=len(body['parts']),
                           lines_count=len(body['lines']), quotes_count=len(body['quotes']),
                           discovery=copy.deepcopy(body['discovery']))
            summaries.append(summary)
        return sorted(summaries, key=lambda item: (item['updated_at'], item['id']), reverse=True)

    def mutate(self, order_id, payload, action, callback):
        require(isinstance(payload, dict), '订单操作须为对象。')
        order_id = identifier(order_id)
        event_id = identifier(payload.get('event_id'))
        identifier(action)
        require(type(payload.get('expected_revision')) is int and payload['expected_revision'] >= 1,
                '请提供有效的订单修订。')
        signature = digest(payload)
        with self._transaction() as connection:
            order = self._read(connection, order_id)
            replay, result = self._replay(connection, order_id, event_id, action, signature)
            if replay:
                return result
            if payload['expected_revision'] != order['revision']:
                raise OrderConflict('订单已被更新，请先刷新并核对本次输入。')
            original = copy.deepcopy(order)
            result = callback(order)
            for key in ('id', 'revision', 'created_at', 'updated_at'):
                require(order.get(key) == original[key], '订单回调不能修改身份、修订或时间。')
            require(isinstance(order.get('quotes'), list), '报价历史须为列表。')
            require(order['quotes'][:len(original['quotes'])] == original['quotes'], '历史报价不能删除或覆盖。')
            for key in ('name', 'inquiry'):
                _text(order.get(key), '订单' + key, maximum=200 if key == 'name' else 10000, empty=key == 'inquiry')
            require(type(order.get('simulated')) is bool, '订单模拟资料标记无效。')
            validate_structure(order)
            order['revision'] += 1
            order['updated_at'] = stamp()
            saved_result = order if result is None else result
            connection.execute('UPDATE inquiry_orders SET revision=?,body=? WHERE id=?',
                               (order['revision'], encoded(order), order_id))
            self._event(connection, order_id, event_id, action, signature, order_id, saved_result)
            return copy.deepcopy(saved_result)

    def replace(self, order_id, payload):
        only_keys(payload, {'event_id', 'expected_revision', 'parts', 'lines', 'bom', 'routes'}, 'replace order structure')
        def apply(order):
            for key in ('parts', 'lines', 'bom', 'routes'):
                if key in payload:
                    order[key] = copy.deepcopy(payload[key])
        return self.mutate(order_id, payload, 'replace', apply)
