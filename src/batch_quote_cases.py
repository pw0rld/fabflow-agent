"""Isolated password-bound case quotation service over immutable saved geometry.

Only the exact protected HTTP prefix selects this service. It cannot upload,
run CAD, reread drawings or expose private records through the public store.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote

from prep_record import digest
from batch_quote_service import BatchQuoteService, BATCH_ID, FILE_ID, RUN_ID, LINE_ID, demand
from cad_manufacturing import validate_geometry

API_PREFIX = '/ortho2cad-test/quote-workbench/api/quote-batches'
IDENTITY_FIELDS = ('file_id', 'native_definition', 'volume_mm3', 'native_solid_count', 'quantity_per_set')
LINK_FILES = {'quote_url': 'customer-quote.html', 'bom_url': 'bom.csv', 'cost_url': 'cost.csv', 'json_url': 'quote.json',
              'summary_bom_url': 'bom-summary.csv', 'print_url': 'customer-print.html',
              'summary_bom_preview_url': 'preview-summary.html', 'bom_preview_url': 'preview-source.html',
              'cost_preview_url': 'preview-cost.html', 'record_preview_url': 'preview-record.html',
              'engineering_url': 'engineering', 'engineering_bom_url': 'engineering-bom.html',
              'engineering_bom_csv_url': 'engineering-bom.csv', 'materials_url': 'materials.html', 'processes_url': 'processes.html',
              'cost_sheet_url': 'cost-sheet', 'internal_cost_url': 'internal-cost.html', 'internal_cost_csv_url': 'internal-cost.csv'}
ASSET_NAME = re.compile(r'(?:part-previews\.json|parts/L[0-9]{4,7}\.json|parts/L[0-9]{4,7}/thumbnail\.png|geometry/F[0-9]{4}\.json)\Z')
SHA = re.compile(r'[a-f0-9]{64}\Z')


def case_links(batch_id, run_id):
    demand(isinstance(batch_id, str) and BATCH_ID.fullmatch(batch_id)
           and isinstance(run_id, str) and RUN_ID.fullmatch(run_id), '受保护案例报价编号无效。')
    prefix = API_PREFIX + '/' + batch_id + '/runs/' + run_id + '/'
    return {key: prefix + LINK_FILES[key] for key in ('quote_url', 'bom_url', 'cost_url', 'json_url')}


def _safe_asset(root, name):
    demand(isinstance(name, str) and ASSET_NAME.fullmatch(name), '案例预览资产名称无效。')
    demand(root.is_dir() and not root.is_symlink(), '案例预览目录无效。')
    path = root
    for component in Path(name).parts:
        path = path / component
        demand(not path.is_symlink(), '案例预览不允许链接文件。')
    demand(path.is_file() and path.stat().st_size <= 32 * 1024 * 1024, '案例预览文件不存在或过大。', 'not_found', 404)
    return path


def _read_json(path, maximum=32 * 1024 * 1024):
    demand(path.is_file() and not path.is_symlink() and path.stat().st_size <= maximum,
           '案例记录不可用。', 'not_found', 404)
    def reject(value):
        raise ValueError('nonfinite case JSON')
    return json.loads(path.read_text(encoding='utf-8'), parse_constant=reject)


def make_case_manifest(base_result, cache_directory, *, original_source_digest=None, original_run_id=None):
    """Read-only migration helper; the caller writes the returned manifest last."""
    demand(isinstance(base_result, dict), '案例报价格式无效。')
    case_links(base_result.get('batch_id'), base_result.get('run_id'))
    sources = {row['id']: {key: row.get(key) for key in ('sha256', 'kind', 'name')}
               for row in base_result.get('source_files', [])}
    lines = {row['id']: {key: copy.deepcopy(row.get(key)) for key in IDENTITY_FIELDS}
             for row in base_result.get('lines', [])}
    demand(sources and lines and len(lines) == len(base_result.get('lines', [])), '案例文件或物料身份不完整。')
    demand(all(isinstance(key, str) and FILE_ID.fullmatch(key) and isinstance(row.get('sha256'), str)
               and SHA.fullmatch(row['sha256']) for key, row in sources.items()), '案例来源文件身份无效。')
    demand(all(isinstance(key, str) and LINE_ID.fullmatch(key) and row.get('file_id') in sources
               for key, row in lines.items()), '案例物料身份无效。')
    root = Path(cache_directory)
    names = {'part-previews.json'}
    names.update('geometry/' + row['file_id'] + '.json' for row in lines.values())
    names.update('parts/' + identity + '.json' for identity in lines)
    names.update('parts/' + identity + '/thumbnail.png' for identity in lines)
    assets = {name: hashlib.sha256(_safe_asset(root, name).read_bytes()).hexdigest() for name in sorted(names)}
    value = {'schema_version': 1, 'case_id': 'poland', 'batch_id': base_result['batch_id'],
             'base_run_id': base_result['run_id'], 'source_digest': digest(base_result),
             'source_sha256': base_result['source_sha256'], 'source_files': sources,
             'line_identities': lines, 'asset_sha256': assets}
    if original_source_digest is not None:
        demand(isinstance(original_source_digest, str) and SHA.fullmatch(original_source_digest), '案例原报价摘要无效。')
        value['original_source_digest'] = original_source_digest
    if original_run_id is not None:
        demand(isinstance(original_run_id, str) and RUN_ID.fullmatch(original_run_id), '案例原报价编号无效。')
        value['original_run_id'] = original_run_id
    return value


class ProtectedCaseQuoteService(BatchQuoteService):
    API_PREFIX = API_PREFIX

    def __init__(self, root):
        super().__init__(root, None, None)

    def _read(self, batch_id):
        state = super()._read(batch_id)
        root = self._folder(batch_id) / '_case-preview'
        demand(root.is_dir() and not root.is_symlink(), '案例预览缓存不存在。', 'not_found', 404)
        manifest = _read_json(root / 'manifest.json', 4 * 1024 * 1024)
        demand(isinstance(manifest, dict) and manifest.get('case_id') == 'poland'
               and manifest.get('batch_id') == batch_id, '受保护案例不存在。', 'not_found', 404)
        return state

    def _private_links(self, result):
        prefix = self.API_PREFIX + '/' + result['batch_id'] + '/runs/' + result['run_id'] + '/'
        links = result.get('links')
        legacy = {'quote_url': 'quote.html', 'print_url': 'quote-print.html'}
        demand(isinstance(links, dict) and links and all(key in LINK_FILES and value in {prefix + LINK_FILES[key], prefix + legacy.get(key, LINK_FILES[key])}
               for key, value in links.items()), '案例报价链接尚未迁移到受保护入口。')

    def _preview_run_locked(self, state, run_id):
        result = super()._preview_run_locked(state, run_id)
        self._private_links(result)
        return result

    def _case_context(self, state, result):
        root = self._folder(state['id']) / '_case-preview'
        demand(root.is_dir() and not root.is_symlink(), '案例预览缓存不存在。', 'not_found', 404)
        manifest = _read_json(root / 'manifest.json', 4 * 1024 * 1024)
        demand(isinstance(manifest, dict) and manifest.get('schema_version') == 1 and manifest.get('case_id') == 'poland'
               and manifest.get('batch_id') == state['id'] and manifest.get('source_sha256') == state['source_sha256'],
               '案例预览来源无效。')
        base = self._preview_run_locked(state, manifest.get('base_run_id'))
        demand(digest(base) == manifest.get('source_digest') and result.get('source_files') == base.get('source_files')
               and result.get('source_sha256') == base.get('source_sha256'), '案例报价与预览原件不一致。')
        sources = {row['id']: {key: row.get(key) for key in ('sha256', 'kind', 'name')}
                   for row in base['source_files']}
        line_ids = {row['id']: {key: copy.deepcopy(row.get(key)) for key in IDENTITY_FIELDS} for row in result['lines']}
        demand(sources == manifest.get('source_files') and line_ids == manifest.get('line_identities'),
               '当前报价的物料或几何身份与导入案例不同。')
        assets = manifest.get('asset_sha256')
        demand(isinstance(assets, dict) and assets and len(assets) <= 3000 and all(
            isinstance(name, str) and ASSET_NAME.fullmatch(name) and isinstance(value, str) and SHA.fullmatch(value)
            for name, value in assets.items()), '案例资产校验清单无效。')
        return root, manifest

    def _asset(self, root, manifest, name, *, as_json=False):
        demand(name in manifest['asset_sha256'], '案例预览未登记。', 'not_found', 404)
        path = _safe_asset(root, name)
        content = path.read_bytes()
        demand(hashlib.sha256(content).hexdigest() == manifest['asset_sha256'][name], '案例预览内容已变化，请核对导入资料。')
        if not as_json:
            return content
        def reject(value):
            raise ValueError('nonfinite case JSON')
        return json.loads(content.decode('utf-8'), parse_constant=reject)

    def metadata(self):
        rows = self.list()
        return {'scope': 'poland', 'label': '波兰订单案例', 'api_prefix': self.API_PREFIX,
                'available': bool(rows), 'entry_batch_id': rows[0]['id'] if rows else None,
                'editable': bool(rows), 'uses_saved_parsing': True}

    def list(self):
        rows = super().list()
        for row in rows:
            if isinstance(row.get('result'), dict):
                with self._lock(row['id']):
                    state = self._read(row['id'])
                    result = self._preview_run_locked(state, row['result']['run_id'])
                    self._case_context(state, result)
                row['result']['links'] = case_links(row['id'], row['result']['run_id'])
        return rows

    def get(self, batch_id):
        with self._lock(batch_id):
            state = self._read(batch_id)
            result = self._preview_run_locked(state, (state.get('result') or {}).get('run_id'))
            self._case_context(state, result)
            return self._public(state)

    def _engineering_pricing_context(self, state, run_id):
        context = super()._engineering_pricing_context(state, run_id)
        self._case_context(state, context[0])
        return context

    def _validate_saved_run(self, batch_id, run_id):
        with self._lock(batch_id):
            state = self._read(batch_id)
            result = self._preview_run_locked(state, run_id)
            self._case_context(state, result)

    def file(self, batch_id, file_id):
        with self._lock(batch_id):
            state = self._read(batch_id)
            result = self._preview_run_locked(state, (state.get('result') or {}).get('run_id'))
            self._case_context(state, result)
            demand(not (self._folder(batch_id) / 'files').is_symlink(), '案例附件目录无效。')
        return super().file(batch_id, file_id)

    def artifact(self, batch_id, run_id, filename):
        self._validate_saved_run(batch_id, run_id)
        return super().artifact(batch_id, run_id, filename)

    def save_engineering(self, batch_id, run_id, payload):
        self._validate_saved_run(batch_id, run_id)
        super().save_engineering(batch_id, run_id, payload)
        return self.engineering(batch_id, run_id)

    def parts_manifest(self, batch_id, run_id):
        with self._lock(batch_id):
            state = self._read(batch_id)
            result = self._preview_run_locked(state, run_id)
            root, manifest = self._case_context(state, result)
            saved = self._asset(root, manifest, 'part-previews.json', as_json=True)
            demand(isinstance(saved, dict) and saved.get('batch_id') == batch_id
                   and saved.get('run_id') == manifest['base_run_id'] and isinstance(saved.get('lines'), list),
                   '案例物料预览清单来源不一致。')
            entries = {row.get('line_id'): row for row in saved['lines'] if isinstance(row, dict)}
            demand(len(entries) == len(saved['lines']) == len(result['lines'])
                   and set(entries) == {row['id'] for row in result['lines']}, '案例预览未覆盖当前全部物料。')
            rows = []
            for line in result['lines']:
                item = entries[line['id']]
                demand(item.get('file_id') == line['file_id'] and item.get('status') == 'ready'
                       and isinstance(item.get('solid_ids'), list) and item['solid_ids'], '案例物料几何关联无效。')
                path = self.API_PREFIX + '/' + batch_id + '/runs/' + run_id + '/parts/' + line['id']
                rows.append({key: copy.deepcopy(item.get(key)) for key in
                             ('line_id', 'file_id', 'solid_ids', 'representative_solid_id', 'representative_solid_ids', 'bounds_mm')})
                rows[-1].update(status='ready', reason='复用已保存的原生几何，数量与费用来自当前报价版本。',
                    geometry_url=path, thumbnail_url=path + '/thumbnail',
                    viewer_url='/workbench/quotation/part/?preview=' + quote(path, safe=''))
            return {'batch_id': batch_id, 'run_id': run_id, 'lines': rows}

    def part_geometry(self, batch_id, run_id, line_id):
        demand(isinstance(line_id, str) and LINE_ID.fullmatch(line_id), '报价物料编号无效。')
        with self._lock(batch_id):
            state = self._read(batch_id)
            result = self._preview_run_locked(state, run_id)
            line = next((row for row in result['lines'] if row['id'] == line_id), None)
            demand(line is not None, '案例物料不存在。', 'not_found', 404)
            root, manifest = self._case_context(state, result)
            part = self._asset(root, manifest, 'parts/' + line_id + '.json', as_json=True)
            source = manifest['source_files'][line['file_id']]
            demand(isinstance(part, dict) and part.get('batch_id') == batch_id and part.get('line_id') == line_id
                   and part.get('run_id') == manifest['base_run_id'] and part.get('source_sha256') == source['sha256'],
                   '案例单件预览来源不一致。')
            saved = part.get('line_snapshot')
            demand(isinstance(saved, dict) and all(saved.get(key) == line.get(key)
                   for key in ('id', 'file_id', 'volume_mm3', 'quantity_per_set')) and all(
                       key not in saved or saved[key] == line.get(key) for key in ('native_definition', 'native_solid_count')),
                   '案例单件原生几何与当前物料身份不一致。')
            geometry = self._asset(root, manifest, 'geometry/' + line['file_id'] + '.json', as_json=True)
            validate_geometry(geometry, source['sha256'])
            solids = {solid['id'] for solid in geometry['solids']}
            groups = part.get('groups')
            siblings = {item['id'] for item in result['lines'] if item['file_id'] == line['file_id']}
            demand(isinstance(groups, list) and groups and all(isinstance(group, dict)
                and isinstance(group.get('solid_ids'), list) and set(group['solid_ids']) <= solids for group in groups),
                '案例装配分组无效。')
            demand(len(groups) == len(siblings) and {group.get('id') for group in groups} == siblings,
                   '案例装配分组与源文件物料不一致。')
            selected = next((group for group in groups if group.get('id') == line_id), None)
            selected_ids = part.get('solid_ids')
            representatives = part.get('representative_solid_ids')
            demand(selected is not None and isinstance(selected_ids, list) and selected_ids
                   and selected['solid_ids'] == selected_ids and set(selected_ids) <= solids
                   and isinstance(representatives, list) and representatives and set(representatives) <= set(selected_ids),
                   '案例单件实体关联不完整。')
            occurrences = part.get('occurrences')
            demand(part.get('selected_id') == line_id and part.get('representative_solid_id') in representatives
                   and len(selected_ids) == len(set(selected_ids)) == line['native_solid_count']
                   and isinstance(occurrences, list) and len(occurrences) == line['quantity_per_set']
                   and all(isinstance(item, dict) and isinstance(item.get('id'), str)
                       and isinstance(item.get('solid_ids'), list) and item['solid_ids']
                       and set(item['solid_ids']) <= set(selected_ids) for item in occurrences),
                   '案例装配实例与当前每套用量不一致。')
            occurrence_ids = [sid for item in occurrences for sid in item['solid_ids']]
            demand(len({item['id'] for item in occurrences}) == len(occurrences)
                   and len(occurrence_ids) == len(set(occurrence_ids))
                   and set(occurrence_ids) == set(selected_ids)
                   and any(set(item['solid_ids']) == set(representatives) for item in occurrences),
                   '案例实例实体重复或缺失。')
            return {'batch_id': batch_id, 'run_id': run_id, 'line_id': line_id, 'part_name': line['part_name'],
                    'source_sha256': source['sha256'], 'source_document': line['document'],
                    'geometry': geometry, 'groups': copy.deepcopy(groups),
                    'representative_solid_id': part.get('representative_solid_id'),
                    'representative_solid_ids': copy.deepcopy(representatives), 'solid_ids': copy.deepcopy(selected_ids),
                    'occurrences': copy.deepcopy(occurrences), 'selected_id': line_id,
                    'line_snapshot': copy.deepcopy(line), 'order_sets': result['settings']['order_sets'],
                    'settings': {key: result['settings'][key] for key in
                                 ('order_sets', 'pricing_method', 'rate_percent', 'material_profile', 'rate_profile')}}

    def part_thumbnail(self, batch_id, run_id, line_id):
        demand(isinstance(line_id, str) and LINE_ID.fullmatch(line_id), '报价物料编号无效。')
        with self._lock(batch_id):
            state = self._read(batch_id)
            result = self._preview_run_locked(state, run_id)
            demand(any(row['id'] == line_id for row in result['lines']), '案例物料不存在。', 'not_found', 404)
            root, manifest = self._case_context(state, result)
            content = self._asset(root, manifest, 'parts/' + line_id + '/thumbnail.png')
            demand(content.startswith(b'\x89PNG\r\n\x1a\n'), '案例缩略图格式无效。')
            return content, 'image/png', line_id + '.png'

    def engineering(self, batch_id, run_id):
        self._validate_saved_run(batch_id, run_id)
        response = super().engineering(batch_id, run_id)
        with self._lock(batch_id):
            available = {row['id'] for row in self._read(batch_id)['runs']}
        snapshot = response['manifest'].get('quote_basis', {}).get('engineering_repricing')
        if isinstance(snapshot, dict) and snapshot.get('source_run_id') not in available:
            snapshot['source_run_id'] = None
            snapshot['source_quote_available'] = False
        return response

    def start_worker(self):
        self.assistant.start_worker()
        self.cost_sheet.start_worker()

    @staticmethod
    def _read_only(*args, **kwargs):
        demand(False, '此入口复用已解析案例；上传新订单请返回公开报价工作台。', 'case_read_only', 405)

    create = start = retry = recognize_engineering = price_engineering = _read_only
