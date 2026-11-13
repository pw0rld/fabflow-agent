"""Versioned engineering annotations beside an immutable quotation run.

Caller holds the batch lock. Reads create no files; writes never touch quote
snapshots, quantities, financial exports, CAD geometry or approval state.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re

from prep_record import atomic_write, digest, stamp
from workbench_store import WorkbenchError

HEADER = {'product_name', 'product_number', 'bom_revision', 'basis_notes', 'base_quantity'}
FIELDS = {'part_number', 'drawing_number', 'revision', 'material_grade', 'thickness_mm',
          'surface_treatment', 'specification', 'supply_type', 'unit', 'parent_line_id'}
RUN = re.compile(r'RUN-[0-9a-f]{32}\Z')
EVENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
MAX_BYTES = 4 * 1024 * 1024


def demand(condition, message, code='invalid_request', status=400, field=None):
    if not condition:
        raise WorkbenchError(message, code, status, {'field': field} if field else None)


def _keys(value, allowed, field):
    demand(isinstance(value, dict) and not set(value).difference(allowed),
           '工程补录包含不支持的字段，请保留输入并刷新。', field=field)


def _text(value, field, maximum=300):
    if value is None or value == '':
        return None
    demand(isinstance(value, str), '请填写文本。', field=field)
    demand(len(value) <= maximum and not any(ord(c) < 32 and c not in '\n\r\t' for c in value),
           '文本过长或包含无效字符。', field=field)
    return value.strip() or None


def normalize_review(review, line_ids):
    _keys(review, {'header', 'lines'}, 'review')
    header = review.get('header', {})
    lines = review.get('lines', {})
    _keys(header, HEADER, 'header')
    demand(isinstance(lines, dict) and len(lines) <= len(line_ids), '工程物料条目无效。', field='lines')
    out = {'header': {}, 'lines': {}}
    for key, value in header.items():
        if key == 'base_quantity':
            demand(type(value) in {str, int} and str(value) == '1',
                   '本版基准数量固定为 1，修改数量口径需要重新核算。', field='header.base_quantity')
            out['header'][key] = 1
        else:
            out['header'][key] = _text(value, 'header.' + key, 2000 if key == 'basis_notes' else 300)
    for identity, values in lines.items():
        demand(identity in line_ids, '补录物料不属于本次报价。', field='lines')
        _keys(values, FIELDS, 'lines.' + identity)
        normalized = {}
        for key, value in values.items():
            field = 'lines.' + identity + '.' + key
            text = _text(value, field)
            if key == 'unit':
                demand(text in {None, 'piece'}, '本次保存的用量单位为件，不能直接改成米或 kg。', field=field)
            elif key == 'supply_type':
                demand(text in {None, 'make', 'buy', 'subcontract', 'customer'}, '请选择有效的供应方式。', field=field)
            elif key == 'parent_line_id':
                demand(text is None or text in line_ids and text != identity, '请选择本次清单内其他物料作为归属项。', field=field)
            elif key == 'thickness_mm' and text is not None:
                try:
                    valid = bool(re.fullmatch(r'(?:0|[1-9]\d*)(?:\.\d+)?', text)) and 0 < Decimal(text) < 10000
                except InvalidOperation:
                    valid = False
                demand(valid, '板厚须为大于零的毫米数，保留图纸精度。', field=field)
            normalized[key] = text
        out['lines'][identity] = normalized
    parents = {key: row.get('parent_line_id') for key, row in out['lines'].items()}
    checked = set()
    for identity in parents:
        visiting, current = set(), identity
        while current is not None and current not in checked:
            demand(current not in visiting, '装配归属形成了循环，请核对父项。', field='lines.' + identity + '.parent_line_id')
            visiting.add(current)
            current = parents.get(current)
        checked.update(visiting)
    return out


def _path(folder, run_id):
    folder = Path(folder)
    demand(isinstance(run_id, str) and RUN.fullmatch(run_id), '报价运行编号无效。')
    runs = folder / 'runs'
    path = runs / (run_id + '-engineering.json')
    demand(folder.is_dir() and not folder.is_symlink() and runs.is_dir() and not runs.is_symlink()
           and not path.is_symlink(), '工程补录目录无效。')
    return path


def _public(record):
    return {key: copy.deepcopy(record.get(key)) for key in
            ('revision', 'header', 'lines', 'saved_at', 'status', 'quotation_unchanged')}


def _read(folder, run_id, result):
    path = _path(folder, run_id)
    identity = digest(result)
    if not path.exists():
        return {'schema_version': 1, 'run_id': run_id, 'source_digest': identity, 'revision': 0,
                'header': {}, 'lines': {}, 'saved_at': None, 'status': 'pending_review',
                'quotation_unchanged': True, '_events': {}, 'history': []}
    demand(path.is_file() and path.stat().st_size <= MAX_BYTES, '工程补录记录无效或过大。')
    record = json.loads(path.read_text(encoding='utf-8'))
    demand(record.get('run_id') == run_id and record.get('source_digest') == identity,
           '工程资料与报价来源不一致，请重新打开对应报价。', 'source_conflict', 409)
    demand(type(record.get('revision')) is int and record['revision'] >= 0
           and isinstance(record.get('_events'), dict) and isinstance(record.get('history'), list),
           '工程补录记录格式无效。')
    normalized = normalize_review({key: record.get(key) for key in ('header', 'lines')}, {x['id'] for x in result['lines']})
    demand(all(record[key] == normalized[key] for key in normalized), '工程补录字段校验失败。')
    return record


def read_review(folder, run_id, result):
    return _public(_read(folder, run_id, result))


def save_review(folder, run_id, result, payload):
    _keys(payload, {'event_id', 'expected_revision', 'review'}, 'request')
    event = payload.get('event_id')
    demand(isinstance(event, str) and EVENT.fullmatch(event), '补录请求编号无效。')
    demand(type(payload.get('expected_revision')) is int and payload['expected_revision'] >= 0, '请重新读取工程资料版本。')
    line_ids = {x['id'] for x in result['lines']}
    patch = normalize_review(payload.get('review'), line_ids)
    record = _read(folder, run_id, result)
    signature = digest(payload)
    if event in record['_events']:
        demand(record['_events'][event] == signature, '相同补录请求的内容已变化。', 'event_conflict', 409)
        return _public(record)
    demand(payload['expected_revision'] == record['revision'], '工程资料已更新，请重新读取后再保存。', 'revision_conflict', 409)
    demand(len(record['_events']) < 500, '本次报价补录历史已满，请建立新的报价版本。')
    merged = {'header': {**record['header'], **patch['header']}, 'lines': copy.deepcopy(record['lines'])}
    for key, values in patch['lines'].items():
        merged['lines'][key] = {**merged['lines'].get(key, {}), **values}
    merged = normalize_review(merged, line_ids)
    demand(merged != {'header': record['header'], 'lines': record['lines']}, '没有待保存的工程信息。')
    record['history'].append({'revision': record['revision'], 'header': record['header'],
                              'lines': record['lines'], 'saved_at': record['saved_at']})
    record.update(merged, revision=record['revision'] + 1, saved_at=stamp(), status='pending_review', quotation_unchanged=True)
    record['_events'][event] = signature
    content = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    demand(len(content.encode('utf-8')) <= MAX_BYTES, '工程补录历史过大，请建立新的报价版本。')
    atomic_write(_path(folder, run_id), content)
    return _public(record)
