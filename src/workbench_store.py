"""Business facade for the quotation workbench; records remain authoritative."""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
import copy
import fcntl
import hashlib
import json
import mimetypes
import math
import os
from pathlib import Path
import threading
import time
from urllib.parse import quote

from prep_record import (RecordError, RecordStore, atomic_write, digest, identifier,
                         number, only_keys, require, stamp)
from quotation import QuoteStore, validate_policy
from quotation_templates import TemplateStore, TemplateConflict
from reading_review import review_reading
from recognition_view import project as recognition_view
from part_visuals import project_parts as project_part_visuals, read_pdf_page
from recognition_limits import MAX_OBSERVATIONS
from reading_parts import (MAX_PARTS, MAX_PART_OBSERVATIONS, MAX_PART_LINKS,
                           MAX_PART_EVIDENCE, PART_KEY, PART_KEYS)

MAX_UPLOAD = 25 * 1024 * 1024
FILE_TYPES = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp',
    '.tif': 'image/tiff', '.tiff': 'image/tiff', '.pdf': 'application/pdf',
    '.dxf': 'application/octet-stream', '.dwg': 'application/octet-stream',
    '.step': 'application/octet-stream', '.stp': 'application/octet-stream',
}
ACTOR = '当前操作者（姓名未提供）'
OBSERVATION_STATUSES = {'observed', 'unit_unknown', 'conflict', 'unreadable'}
FORM_LABELS = {'material_usage_per_piece': '每件材料用量', 'material_rate': '材料单价',
               'operation_rates': '各工序加工费', 'setup_fee': '另收准备费 / 开机费（整单）',
               'fee_inclusions': '费用包含说明'}
RUN_LABELS = {
    'starting': ('queued', '读图请求已保存，准备后台执行'),
    'running': ('waiting_model', '正在等待模型读取结果'),
    'unknown': ('unknown', '正在核对后台运行状态'),
    'stopping': ('stopping', '正在停止后台读图'),
    'stopped': ('stopped', '后台读图已停止'),
    'completed': ('completed', '识别结果已保存，待人工核对'),
    'failed': ('failed', '后台读图未完成，请查看原因'),
    'waiting': ('waiting', '读图需要补充资料'),
}


def calculation_message(message):
    if 'exactly one matching rate' in message:
        return '每个采用工序都需要一条对应费率，请补齐并核对工序名称。'
    if 'positive per-order process fee' in message:
        return '整单工序费与另收准备费不能同时计入，请到“加工费用”核对金额和计费方式。'
    if 'requires an explicit measured pricing quantity' in message:
        return '存在当前不支持的工序计费单位，请补充计量与计价依据后再核算。'
    if 'duplicate process route' in message or 'duplicate operation rate' in message:
        return '工序或费率存在重复，请核对后保留每个工序的一条费率。'
    return message


def runtime_message(value):
    if isinstance(value, dict):
        return str(value.get('message', value.get('code', '读图服务未完成请求。')))[:1200]
    return str(value)[:1200] if value else None


def issue_target(field, audience='technical'):
    stage = 'requirements' if audience == 'customer' else 'technical'
    if stage == 'requirements':
        section = 'requirements'
    elif field in {'material_usage_per_piece', 'material_rate'}:
        section = 'material'
    elif field == 'engineering_open_items':
        section = 'advanced'
    else:
        section = 'fees'
    return {'stage': stage, 'section': section, 'field': field}


def fee_overlap_issue(record):
    """Locate the calculator's existing restriction; never change a fee."""
    fields = record['fields']
    setup_slot, rates_slot = fields['setup_fee'], fields['operation_rates']
    if (setup_slot['status'] not in {'verified', 'pending_review'} or
            rates_slot['status'] not in {'verified', 'pending_review'}):
        return None
    setup, rates = setup_slot.get('effective'), rates_slot.get('effective')
    if not setup or not rates or setup.get('unit') != 'CNY/order':
        return None
    try:
        amount = number(setup['value'])
        if amount <= 0:
            return None
        rows = [{'operation': row['operation'], 'value': row['value'], 'unit': row['unit']}
                for row in rates['value'] if row['unit'] == 'CNY/order' and number(row['value']) > 0]
    except (RecordError, KeyError, TypeError):
        return None
    if not rows:
        return None
    names = '、'.join(f'{str(row["operation"])[:100]} ¥{format(number(row["value"]), "f")}/单'
                     for row in rows[:3]) + ('等' if len(rows) > 3 else '')
    return {'field': 'setup_fee', 'code': 'fee_overlap', 'audience': 'technical',
            'message': f'{names}已按整单计费，同时另收准备费 ¥{format(amount, "f")}。请到“加工费用”核对这两笔费用；仅修改说明不能解除限制。',
            'related_fields': ['operation_rates', 'setup_fee', 'fee_inclusions'],
            'target': issue_target('setup_fee'),
            'details': {'setup_fee': format(amount, 'f'), 'operation_rates': rows}}


class WorkbenchError(RecordError):
    def __init__(self, message, code='invalid_request', status=400, details=None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


def read_json(path, default=None):
    if not path.exists():
        return copy.deepcopy(default)
    require(not path.is_symlink(), 'symlink metadata prohibited')
    return json.loads(path.read_text(encoding='utf-8'))


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n'


class Workbench:
    def __init__(self, root, *, catalog=None, bridge_factory=None, reader_root=None):
        self.records = RecordStore(root)
        self.quotes = QuoteStore(root)
        self.root = self.records.root
        self.catalog = catalog
        self.bridge_factory = bridge_factory
        self.meta = self.root / '.workbench'
        require(not self.meta.is_symlink(), 'symlink workbench metadata prohibited')
        self.meta.mkdir(mode=0o700, exist_ok=True)
        self.templates = TemplateStore(self.meta)
        self.reader_root = Path(reader_root).resolve() if reader_root else self.meta / 'readers'
        self._workers = set()

    @contextmanager
    def lock(self, key):
        identifier(key)
        path = self.meta / (key + '.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _state_path(self, task_id):
        return self.records.task_path(task_id) / 'workbench.json'

    def _state(self, task_id):
        return read_json(self._state_path(task_id), {'events': {}, 'pricing_context': {}, 'run': None})

    def _save_state(self, task_id, state):
        atomic_write(self._state_path(task_id), json_text(state))

    def list_templates(self):
        return self.templates.list()

    def template(self, template_id):
        return self.templates.get(template_id)

    def create_template(self, payload):
        only_keys(payload, {'event_id', 'name', 'source_task_id', 'expected_revision', 'include_material_usage'}, 'template')
        event_id = identifier(payload.get('event_id'))
        require(len(event_id) <= 60, 'event id too long')
        task_id = identifier(payload.get('source_task_id'))
        require(task_id != 'templates', '来源任务标识为模板保留名称，请重新选择任务。')
        name = payload.get('name')
        require(isinstance(name, str) and 0 < len(name.strip()) <= 100, '请填写模板名称（最多 100 字）。')
        include_usage = payload.get('include_material_usage', False)
        require(type(include_usage) is bool, '请明确是否保存本件材料用量。')
        # Fixed lock order: task first, then shared template file. Source task is
        # read only, including on an idempotent replay after later edits.
        with self.lock(task_id), self.lock('templates'):
            try:
                old = self.templates.replay(self.templates._read(), event_id, 'create', payload)
            except TemplateConflict as exc:
                raise WorkbenchError(str(exc), 'event_conflict', 409) from exc
            if old is not None:
                return old
            record = self._revision(task_id, payload.get('expected_revision'))
            require(fee_overlap_issue(record) is None, '整单工序费与另收准备费存在重复计费限制，请先核对费用后再存模板。')
            values = {}
            names = ['process_route', 'operation_rates', 'material_rate', 'setup_fee', 'fee_inclusions']
            if include_usage:
                names.append('material_usage_per_piece')
            for field in names:
                slot = record['fields'][field]
                if slot['status'] == 'verified' and slot.get('effective') is not None:
                    values[field] = copy.deepcopy(slot['effective']['value'])
            warnings = []
            route = values.get('process_route')
            rates = values.get('operation_rates')
            if route is not None:
                require(len(route) == len(set(route)), '工序路线存在重复，请先核对。')
            if rates is not None:
                require(route is not None, '保存工序费率前，请先核对工序路线。')
                operations = [row['operation'] for row in rates]
                require(len(operations) == len(set(operations)) and set(operations) == set(route),
                        '工序与费率须一一对应，请先补齐并核对。')
            elif route is not None:
                values.pop('process_route', None)
                warnings.append('工序和费率未核对完整，本模板未保存该组；请在本单补充核对。')
            require(bool(values), '尚无可存模板的已核对工艺或费用参数。')
            if 'material_usage_per_piece' in values:
                warnings.append('模板包含来源零件的每件材料用量，采用时须按本件重新核对。')
            state = self._state(task_id)
            catalog_sources = {key: copy.deepcopy(item) for key, item in state.get('price_selections', {}).items()
                               if self._template_price_same(values, key, item)}
            applicability = {key: copy.deepcopy(record['fields'][key]['effective']['value'])
                             for key in ('material_grade', 'thickness', 'surface_treatment')
                             if record['fields'][key]['status'] == 'verified' and record['fields'][key].get('effective')}
            snapshot = {'name': name.strip(), 'values': values,
                        'pricing_context': copy.deepcopy(state.get('pricing_context', {})),
                        'applicability': applicability, 'source_task_id': task_id,
                        'source_revision': record['data_revision'], 'catalog_sources': catalog_sources,
                        'simulated': record['simulated'] or any(x.get('simulated', False) for x in catalog_sources.values()),
                        'warnings': warnings}
            return self.templates.created(payload, snapshot)

    def disable_template(self, template_id, payload):
        only_keys(payload, {'event_id'}, 'template disable')
        identifier(payload.get('event_id'))
        with self.lock('templates'):
            try:
                return self.templates.disable(template_id, payload)
            except TemplateConflict as exc:
                raise WorkbenchError(str(exc), 'event_conflict', 409) from exc

    @staticmethod
    def _template_price_same(values, key, item):
        if key.startswith('operations:'):
            actual = next((row for row in values.get('operation_rates', [])
                           if row['operation'] == key[len('operations:'):]), None)
            return (actual is not None and actual['unit'] == item['unit'] and
                    number(actual['value']) == number(item['rate']))
        units = {'material_rate': 'CNY/kg', 'setup_fee': 'CNY/order'}
        return (key in units and key in values and units[key] == item['unit'] and
                number(values[key]) == number(item['rate']))

    @staticmethod
    def _template_references(sources):
        references = {}
        for key, item in sources.items():
            spec = {'entry_id': item['entry_id'], 'version': item['version']}
            if key.startswith('operations:'):
                references.setdefault('operations', {})[key[len('operations:'):]] = spec
            else:
                references[key] = spec
        return references

    def template_preview(self, task_id, payload):
        only_keys(payload, {'template_id', 'expected_revision'}, 'template preview')
        with self.lock(task_id):
            record = self._revision(task_id, payload.get('expected_revision'))
            entry = self.templates.get(payload.get('template_id'))
            values = copy.deepcopy(entry['values'])
            warnings = copy.deepcopy(entry.get('warnings', []))
            can_apply = entry['enabled']
            if not can_apply:
                warnings.append('该模板已停用，请选择其他模板。')
            if ('process_route' in values) != ('operation_rates' in values):
                can_apply = False
                warnings.append('该模板的工序与费率未成组，请补齐核对后另存模板。')
            labels = {'material_grade': '材料牌号', 'thickness': '板厚', 'surface_treatment': '表面处理'}
            for key, old in entry['applicability'].items():
                slot = record['fields'][key]
                if slot['status'] != 'verified' or slot.get('effective') is None:
                    continue
                current = slot['effective']['value']
                same = number(old) == number(current) if key == 'thickness' else old == current
                if not same:
                    warnings.append(f'模板适用{labels[key]}为 {old}，本单为 {current}；请核对工艺和费用。')
            references = self._template_references(entry.get('catalog_sources', {}))
            if references:
                try:
                    self._validate_prices(record, values, entry['pricing_context'], references)
                except (RecordError, ValueError) as exc:
                    can_apply = False
                    warnings.append('模板原采用价格库条目需重新核对：' + str(exc))
            return {'template': {key: entry[key] for key in ('id', 'version', 'name')},
                    'values': values, 'pricing_context': copy.deepcopy(entry['pricing_context']),
                    'applicability': copy.deepcopy(entry['applicability']), 'warnings': warnings,
                    'price_selections': references, 'can_apply': can_apply, 'simulated': entry['simulated']}

    @staticmethod
    def _display_run(run):
        if not run:
            return None
        result = copy.deepcopy(run)
        result.pop('initial_result', None)
        result.pop('recognition_result', None)
        if isinstance(result.get('review'), dict):
            result['review'].pop('result', None)
        stage, label = RUN_LABELS.get(run.get('status'), ('unknown', '后台运行状态待确认'))
        result.setdefault('progress', {'stage': stage, 'label': label, 'last_event_seq': None,
                                       'last_activity_at': None, 'events': []})
        result.setdefault('checked_at', None)
        result.setdefault('last_progress_at', None)
        return result

    def _observations(self, record, run, values, source_pass):
        """Keep sourced drawing text separate from adopted numeric parameters."""
        # Two bounded passes plus at most two conflict annotations per field.
        require(isinstance(values, list) and len(values) <= 2 * MAX_OBSERVATIONS + 2 * len(record['fields']),
                'bounded drawing observations required')
        result = []
        references = set(run.get('source_ids', []))
        if record['inquiry'].strip():
            references.add('inquiry')
        for item in values:
            require(isinstance(item, dict), 'drawing observation must be an object')
            require(item.get('name') in record['fields'], 'unknown observation field')
            text, unit, reason = item.get('raw_text'), item.get('raw_unit'), item.get('reason')
            require(isinstance(text, str) and bool(text.strip()) and len(text) <= 10000, 'bounded observation text required')
            require(unit is None or isinstance(unit, str) and len(unit) <= 4000, 'invalid observation unit')
            require(reason is None or isinstance(reason, str) and len(reason) <= 4000, 'invalid observation reason')
            if 'source_name' in item:
                label = item['source_name']
                require(item['name'] == 'engineering_open_items' and isinstance(label, str)
                        and 1 <= len(label) <= 128 and label == label.strip() and label.isprintable()
                        and label not in record['fields'], 'invalid original observation label')
            require(item.get('status') in OBSERVATION_STATUSES, 'invalid observation status')
            evidence = item.get('evidence')
            require(isinstance(evidence, dict) and set(evidence) == {'reference', 'page', 'region'}, 'invalid observation evidence')
            reference = evidence['reference']
            require(reference in references and reference in record['sources'], 'observation source outside current run')
            require(isinstance(evidence['region'], str) and bool(evidence['region'].strip()) and len(evidence['region']) <= 1000,
                    'observation region required')
            require((reference == 'inquiry' and evidence['page'] is None) or
                    (record['sources'][reference]['kind'] == 'drawing' and type(evidence['page']) is int and evidence['page'] > 0),
                    'observation page does not match source')
            origin = source_pass or item.get('source_pass', 'initial')
            require(origin in {'initial', 'review', 'legacy'}, 'invalid observation pass')
            result.append({'name': item['name'], 'raw_text': text, 'raw_unit': unit, 'status': item['status'],
                           'evidence': copy.deepcopy(evidence), 'reason': reason, 'source_pass': origin})
            if 'source_name' in item:
                result[-1]['source_name'] = item['source_name']
        return result

    def _part_source_pages(self, record, run):
        """Pin existing page counts while saving output; never render on GET."""
        references = set(run.get('source_ids', []))
        counts = {}
        for reference in references:
            source = record['sources'].get(reference, {})
            if source.get('kind') != 'drawing':
                continue
            suffix = Path(source.get('original_name', source.get('path', ''))).suffix.lower()
            if suffix != '.pdf':
                counts[reference] = 1
        for reference, count in run.get('source_page_counts', {}).items():
            if reference in references and reference not in counts and type(count) is int and 1 <= count <= 10000:
                counts[reference] = count
        missing = references - set(counts)
        if missing:
            workspace = self.reader_root / identifier(record['task_id']) / identifier(run['id'])
            manifest_path = workspace / '.dsh-bridge-run.json'
            try:
                require(not workspace.is_symlink() and not manifest_path.is_symlink(),
                        'invalid parts source manifest')
                manifest = read_json(manifest_path)
                require(isinstance(manifest, dict) and manifest.get('task_id') == record['task_id']
                        and manifest.get('session_id') == run.get('session_id'),
                        'parts source manifest identity mismatch')
                files = manifest['files']
                require({item['id'] for item in files} == references
                        and all(item['sha256'] == record['sources'][item['id']]['sha256'] for item in files),
                        'parts source manifest changed')
                for reference, count in manifest.get('pdf_views', {}).get('page_counts', {}).items():
                    if reference in missing and type(count) is int and 1 <= count <= 10000:
                        counts[reference] = count
            except (OSError, ValueError, KeyError, TypeError):
                # A PDF without a pinned render cannot acquire invented pages.
                pass
        run['source_page_counts'] = copy.deepcopy(counts)
        return counts

    def _parts(self, record, run, values, source_pass=None):
        """Validate part candidates separately from the single-product record."""
        require(isinstance(values, list) and len(values) <= 2 * MAX_PARTS,
                'bounded drawing parts required')
        counts = self._part_source_pages(record, run) if values else {}
        references = set(run.get('source_ids', []))

        def position(value):
            require(isinstance(value, dict) and set(value) == {'reference', 'page', 'region'},
                    'invalid part source evidence')
            reference, page, region = value['reference'], value['page'], value['region']
            require(reference in references and reference in record['sources']
                    and record['sources'][reference]['kind'] == 'drawing',
                    'part source outside current drawing run')
            require(type(page) is int and 1 <= page <= counts.get(reference, 0),
                    'part page outside supplied original pages')
            require(isinstance(region, str) and bool(region.strip()) and len(region) <= 1000,
                    'part source region required')
            return copy.deepcopy(value)

        result, keys, links = [], set(), 0
        for item in values:
            require(isinstance(item, dict) and PART_KEYS <= set(item)
                    and not set(item) - PART_KEYS - {'views'}, 'invalid drawing part shape')
            key = item['key']
            require(isinstance(key, str) and PART_KEY.fullmatch(key) and key not in keys,
                    'drawing parts require unique bounded keys')
            keys.add(key)
            for name, limit in (('name', 256), ('part_number', 128), ('version', 128)):
                value = item[name]
                require((name != 'name' and value is None) or
                        (isinstance(value, str) and 1 <= len(value) <= limit
                         and value == value.strip() and value.isprintable()),
                        'bounded drawing part identity required')
            evidence = item['evidence']
            require(isinstance(evidence, list) and 1 <= len(evidence) <= 2 * MAX_PART_EVIDENCE,
                    'drawing part source evidence required')
            checked_evidence = [position(value) for value in evidence]
            attached = {value['reference'] for value in checked_evidence}
            observations = item['observations']
            require(isinstance(observations, list) and len(observations) <= 2 * MAX_PART_OBSERVATIONS,
                    'bounded part observations required')
            checked_observations = self._observations(record, run, observations, source_pass)
            for observation in checked_observations:
                observation['evidence'] = position(observation['evidence'])
                require(observation['evidence']['reference'] in attached,
                        'part observation outside its source references')
            links += len(checked_observations)
            require(links <= 2 * MAX_PART_LINKS, 'too many drawing part observations')
            result.append({name: copy.deepcopy(item[name]) for name in
                           ('key', 'name', 'part_number', 'version')})
            result[-1].update(status='pending_review', evidence=checked_evidence,
                             observations=checked_observations)
            if 'views' in item:
                from reading_parts import validate_saved_views
                pages = {(value['reference'], value['page']) for value in checked_evidence}
                result[-1]['views'] = validate_saved_views(item['views'], pages)
        return result

    def _run_view(self, task_id, run, record):
        result = self._display_run(run)
        if not result or 'observations' in result or run.get('status') != 'completed' or not run.get('session_id'):
            return result
        # Old completed artifacts remain immutable. This read-only projection
        # exposes already saved source annotations without another model call.
        try:
            from dsh_bridge import DshBridge
            workspace = self.reader_root / task_id / identifier(run['id'])
            if not workspace.is_dir() or workspace.is_symlink():
                return result
            bridge = DshBridge('http://127.0.0.1:3080', workspace)
            manifest = bridge._run(run['session_id'])
            require(manifest['task_id'] == task_id, 'legacy task mismatch')
            require({file['id'] for file in manifest['files']} == set(run.get('source_ids', [])), 'legacy source set mismatch')
            for file in manifest['files']:
                require(record['sources'][file['id']]['sha256'] == file['sha256'], 'legacy source changed')
            artifact = bridge._local(manifest['output_path'], exists=True)
            require(artifact.stat().st_size <= 16 * 1024 * 1024, 'legacy artifact too large')
            saved = read_json(artifact)
            require(saved['session_id'] == run['session_id'] and saved['task_id'] == task_id and saved['status'] == 'completed',
                    'legacy reading identity mismatch')
            values = bridge.observations_from_text(saved['text'], manifest)
            result['observations'] = self._observations(record, run, values, 'legacy')
        except (ValueError, OSError, KeyError, TypeError):
            # Missing/invalid old artifacts never invent findings or write state.
            result['observations'] = []
        return result

    @staticmethod
    def _set_run_progress(run, progress=None, *, checked=False):
        """Persist observations; a successful poll is not model activity."""
        previous = run.get('progress') or {}
        if progress is None:
            stage, label = RUN_LABELS.get(run.get('status'), ('unknown', '后台运行状态待确认'))
            progress = {'stage': stage, 'label': label}
        current = copy.deepcopy(previous)
        current.update(stage=str(progress.get('stage', 'unknown'))[:40],
                       label=str(progress.get('label', '后台运行状态待确认'))[:300])
        supplied_seq = progress.get('last_event_seq')
        # Transport uncertainty can omit event history. Preserve the last real
        # event and activity timestamp while showing that state is uncertain.
        if type(supplied_seq) is int and supplied_seq >= 0:
            old_seq = previous.get('last_event_seq')
            if type(old_seq) is not int or supplied_seq >= old_seq:
                current['last_event_seq'] = supplied_seq
                activity = progress.get('last_activity_at')
                if type(activity) in {int, float} and math.isfinite(activity) and activity >= 0:
                    current['last_activity_at'] = activity
                if isinstance(progress.get('events'), list):
                    events = []
                    for event in progress['events'][-12:]:
                        if not isinstance(event, dict) or type(event.get('seq')) is not int:
                            continue
                        item = {'seq': event['seq'], 'stage': str(event.get('stage', 'unknown'))[:40],
                                'label': str(event.get('label', '后台任务活动'))[:300]}
                        at = event.get('at')
                        if type(at) in {int, float} and math.isfinite(at) and at >= 0:
                            item['at'] = at
                        events.append(item)
                    current['events'] = events
        current.setdefault('last_event_seq', None)
        current.setdefault('last_activity_at', None)
        current.setdefault('events', [])
        if isinstance(progress.get('retry'), dict):
            current['retry'] = {k: v for k, v in progress['retry'].items()
                                if k in {'attempt', 'delay_ms'} and type(v) in {int, float} and math.isfinite(v) and v >= 0}
        elif current['stage'] != 'retrying':
            current.pop('retry', None)
        observed_at = stamp()
        if (previous.get('stage'), previous.get('last_event_seq')) != (current['stage'], current['last_event_seq']):
            run['last_progress_at'] = observed_at
        run['progress'] = current
        if checked:
            run['checked_at'] = observed_at

    def _revision(self, task_id, expected):
        record = self.records.load(task_id)
        if type(expected) is not int or expected != record['data_revision']:
            raise WorkbenchError('资料已更新，请重新载入后核对本次修改。', 'revision_conflict', 409,
                                 {'current_revision': record['data_revision']})
        return record

    def _mutation(self, task_id, action, payload, callback):
        event_id = identifier(payload.get('event_id'))
        require(len(event_id) <= 60, 'event id too long')
        signature = digest({'action': action, 'payload': payload})
        with self.lock(task_id):
            state = self._state(task_id)
            old = state['events'].get(event_id)
            if old:
                if old['signature'] != signature:
                    raise WorkbenchError('同一次提交的内容发生变化，请刷新后重新提交。', 'event_conflict', 409)
                if old.get('status') == 'done':
                    return old.get('quote_id')
            else:
                self._revision(task_id, payload.get('expected_revision'))
                state['events'][event_id] = {'signature': signature, 'status': 'pending', 'at': stamp()}
                self._save_state(task_id, state)
            result = callback(state)
            state['events'][event_id] = {'signature': signature, 'status': 'done', 'at': stamp(), 'quote_id': result}
            self._save_state(task_id, state)
            return result

    def create(self, payload):
        only_keys(payload, {'event_id', 'inquiry', 'simulated'}, 'new inquiry')
        event_id = identifier(payload.get('event_id'))
        require(isinstance(payload.get('inquiry'), str) and 0 < len(payload['inquiry'].strip()) <= 20000,
                '请填写本次询价要求或图纸说明（最多 20000 字）。')
        require(type(payload.get('simulated')) is bool, '请明确是否使用模拟数据。')
        task_id = 'INQ-' + hashlib.sha256(event_id.encode()).hexdigest()[:20]
        signature = digest(payload)
        with self.lock(task_id):
            if (self.records.task_path(task_id) / 'record.json').exists():
                state = self._state(task_id)
                record = self.records.load(task_id)
                require(state.get('creation_hash', record['versions'].get('workbench_init_hash')) == signature,
                        'event id reused with different inquiry')
                if not state.get('creation_hash'):
                    state['creation_hash'] = signature
                    self._save_state(task_id, state)
            else:
                self.records.init(task_id, {'inquiry': payload['inquiry'], 'simulated': payload['simulated'],
                                           'files': [], 'versions': {'workbench': '1.0.0', 'workbench_init_hash': signature}})
                self._save_state(task_id, {'creation_hash': signature, 'events': {},
                                          'pricing_context': {}, 'run': None})
        return self.task(task_id)

    def _quote_result(self, task_id, quote_id):
        result = self.quotes.load(task_id, quote_id)
        base = '/quote-preview/' + task_id + '/' + quote_id + '/'
        result['links'] = {'quote_url': base + 'quote.md', 'cost_url': base + 'cost_breakdown.md',
                           'record_url': base + 'record.md', 'quote_download': base + 'quote.md?download=1',
                           'cost_download': base + 'cost_breakdown.md?download=1'}
        embed = '/workbench/api/tasks/' + task_id + '/quotes/' + quote_id + '/preview/'
        result['links'].update(quote_embed_url=embed + 'quote.md', cost_embed_url=embed + 'cost_breakdown.md')
        return result

    def _validation(self, record):
        validation = self.records._validation(record)
        statuses = {'missing': '待补充', 'pending_review': '待核对', 'conflict': '存在冲突', 'unreadable': '标注无法辨认'}
        for issue in validation['issues']:
            name = issue['field']
            issue['target'] = issue_target(name, issue.get('audience'))
            if issue['code'] in statuses:
                label = FORM_LABELS.get(name, record['requirements']['fields'][name]['label'])
                issue['message'] = label + '：' + statuses[issue['code']]
        overlap = fee_overlap_issue(record)
        if overlap:
            if validation['ready']:
                validation.update(status='费用口径待核对', step=4)
            validation['ready'] = False
            validation['issues'].insert(0, overlap)
        selections = self._state(record['task_id']).get('price_selections', {})
        if validation['ready'] and selections:
            matches = self.match_prices(record['task_id'])
            applicable = {(item['entry_id'], item['version'])
                          for result in matches['results'] if result['status'] in {'unique', 'ambiguous'}
                          for item in result['candidates']}
            for key, item in selections.items():
                if key.startswith('operations:'):
                    operation = key[len('operations:'):]
                    current = next((row for row in record['fields']['operation_rates']['effective']['value']
                                    if row['operation'] == operation), None)
                    same_value = current is not None and number(current['value']) == number(item['rate']) and current['unit'] == item['unit']
                else:
                    current = record['fields'][key]['effective']
                    same_value = current is not None and number(current['value']) == number(item['rate']) and current['unit'] == item['unit']
                if (item['entry_id'], item['version']) not in applicable or not same_value:
                    validation.update(ready=False, status='采用价格需重新核对', step=4)
                    validation['issues'].append({'field': 'operation_rates' if key.startswith('operations:') else key,
                                                 'code': 'price_review', 'audience': 'technical',
                                                 'target': issue_target('operation_rates' if key.startswith('operations:') else key),
                                                 'message': '原采用价格的适用条件或有效版本已变化，请重新匹配，或由技术人员明确核对本次费率。'})
                    break
        if validation['ready']:
            # Reuse the actual calculator's cross-field checks. This policy is
            # validation-only, discarded and never saved or offered as a price.
            check_policy = {'schema_version': 1, 'pricing_method': 'cost_plus', 'rate': '0',
                            'currency': 'CNY', 'tax_included': False, 'freight_included': False,
                            'simulated': True, 'actor': '系统输入校验', 'source_text': '仅校验参数，不生成或保存报价。'}
            try:
                self.quotes._calculate(record, check_policy)
            except RecordError as exc:
                validation.update(ready=False, status='技术计费口径待核对', step=4)
                validation['issues'].append({'field': 'operation_rates', 'code': 'calculation_input',
                                             'audience': 'technical', 'target': issue_target('operation_rates'),
                                             'message': calculation_message(str(exc))})
        return validation

    def task(self, task_id):
        record = self.records.load(task_id)
        state = self._state(task_id)
        fields = {}
        for name, slot in record['fields'].items():
            effective = slot['effective'] or {}
            fields[name] = {'value': copy.deepcopy(effective.get('value')), 'status': slot['status'],
                            'evidence': effective.get('evidence'), 'reason': effective.get('reason')}
            if slot['status'] == 'conflict' and slot['history']:
                fields[name]['conflict_candidate'] = slot['history'][-1]
        attachments = []
        for source_id, source in record['sources'].items():
            if source['kind'] != 'drawing':
                continue
            name = source.get('original_name', source_id)
            attachments.append({'id': source_id, 'name': name, 'size': source['size'],
                                'mime': FILE_TYPES.get(Path(name).suffix.lower(), 'application/octet-stream'),
                                'url': '/workbench/api/tasks/' + task_id + '/attachments/' + source_id})
        quotes = []
        parent = self.records.task_path(task_id) / 'quotes'
        if parent.is_dir() and not parent.is_symlink():
            for path in sorted(parent.iterdir(), reverse=True):
                if path.is_dir() and not path.is_symlink() and (path / 'quote.json').is_file():
                    try:
                        result = self._quote_result(task_id, path.name)
                        quotes.append({**{key: result[key] for key in ('quote_id', 'amounts', 'status', 'quantity',
                                                                     'source_current', 'created_at', 'simulated')},
                                       'id': result['quote_id']})
                    except (RecordError, OSError, ValueError):
                        quotes.append({'id': path.name, 'quote_id': path.name, 'error': '旧报价暂不可读取，请检查快照完整性。'})
        run_view = self._run_view(task_id, state.get('run'), record)
        recognition = recognition_view(record, state.get('run'), run_view)
        if recognition['parts']:
            project_part_visuals(task_id, record, state.get('run'), recognition['parts'], self.reader_root)
        return {'id': task_id, 'inquiry': record['inquiry'], 'revision': record['data_revision'],
                'simulated': record['simulated'], 'created_at': record['created_at'], 'updated_at': record['updated_at'],
                'fields': fields, 'definitions': record['requirements']['fields'],
                'validation': self._validation(record), 'attachments': attachments, 'quotes': quotes,
                'run': run_view, 'recognition': recognition,
                'pricing_context': state.get('pricing_context', {}),
                'price_selections': state.get('price_selections', {}),
                'template_selection': state.get('template_selection')}

    def tasks(self):
        result = []
        for path in self.root.iterdir():
            if path.name.startswith('.') or path.is_symlink() or not path.is_dir() or not (path / 'record.json').is_file():
                continue
            try:
                task = self.task(path.name)
                summary = {key: task[key] for key in ('id', 'inquiry', 'revision', 'simulated', 'updated_at', 'run')}
                summary.update(product_name=task['fields']['product_name']['value'],
                               quantity=task['fields']['quantity']['value'], status=task['validation']['status'],
                               ready=task['validation']['ready'], quotes=task['quotes'])
                recognition = task['recognition']
                summary.update(recognition_status=recognition['status'], recognition_available=recognition['available'],
                               recognition_name=next((item['value'] for item in recognition['candidates']
                                                      if item['name'] == 'product_name'), None),
                               attachment_count=len(task['attachments']))
                result.append(summary)
            except (RecordError, OSError, ValueError, KeyError):
                result.append({'id': path.name, 'status': '记录暂不可读取', 'error': True, 'updated_at': ''})
        return sorted(result, key=lambda item: item['updated_at'], reverse=True)

    def attachment(self, task_id, source_id):
        record = self.records.load(task_id)
        source = record['sources'].get(identifier(source_id))
        require(source is not None and source['kind'] == 'drawing', '图纸附件不存在。')
        path = self.records.task_path(task_id) / source['path']
        content = path.read_bytes()
        require(len(content) <= MAX_UPLOAD, '图纸过大。')
        return content, FILE_TYPES.get(Path(source['original_name']).suffix.lower(), 'application/octet-stream'), source['original_name']

    def reading_page(self, task_id, source_id, page, run_id):
        """Existing rendered pages only; GET never starts a decoder or model."""
        record = self.records.load(task_id)
        state = self._state(task_id)
        return read_pdf_page(record, state.get('run'), self.reader_root, source_id, page, run_id)

    def upload(self, task_id, event_id, expected_revision, filename, content):
        require(isinstance(filename, str) and filename and len(filename) <= 180
                and Path(filename).name == filename and not any(c in filename for c in '\\/:\x00\r\n'), '文件名无效。')
        suffix = Path(filename).suffix.lower()
        require(suffix in FILE_TYPES, '请上传图片、PDF、DWG、DXF 或 STEP 文件。')
        require(0 < len(content) <= MAX_UPLOAD, '单个文件须大于 0 且不超过 25 MiB。')
        if suffix == '.pdf':
            require(content.startswith(b'%PDF-'), '文件内容与 PDF 格式不符。')
        if suffix == '.png':
            require(content.startswith(b'\x89PNG\r\n\x1a\n'), '文件内容与 PNG 格式不符。')
        if suffix in {'.jpg', '.jpeg'}:
            require(content.startswith(b'\xff\xd8\xff'), '文件内容与 JPEG 格式不符。')
        payload = {'event_id': event_id, 'expected_revision': expected_revision, 'filename': filename,
                   'sha256': hashlib.sha256(content).hexdigest()}
        def commit(state):
            directory = self.meta / 'uploads' / identifier(task_id) / identifier(event_id)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = directory / filename
            if path.exists():
                require(path.read_bytes() == content, 'upload event reused with different bytes')
            else:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            self.records.apply(task_id, 'record_reading', {'sources': [{'id': 'FILE-' + event_id,
                               'path': str(path), 'kind': 'drawing'}], 'fields': []}, expected_revision, event_id)
        self._mutation(task_id, 'upload', payload, commit)
        return self.task(task_id)

    def save_fields(self, task_id, stage, payload):
        require(stage in {'requirements', 'technical'}, 'invalid stage')
        only_keys(payload, {'event_id', 'expected_revision', 'values', 'pricing_context', 'price_selections', 'template_selection'}, 'form')
        require(isinstance(payload.get('values'), dict), '表单值必须为对象。')
        values = {k: v for k, v in payload['values'].items() if v is not None and v != '' and v != []}
        context = payload.get('pricing_context', {})
        only_keys(context, {'material_form', 'product_family'}, 'pricing context')
        require(all(isinstance(v, str) and len(v) <= 200 for v in context.values()), '价格适用条件无效。')
        selection = payload.get('template_selection')
        if selection is not None:
            require(stage == 'technical', '模板仅可用于工艺与计价步骤。')
            only_keys(selection, {'id', 'version'}, 'template selection')
            identifier(selection.get('id'))
            require(type(selection.get('version')) is int and selection['version'] == 1, '模板版本无效。')

        def retained_prices(record, state, price_snapshots, *, legacy=False):
            retained = copy.deepcopy(state.get('price_selections', {}))
            for name in ('material_rate', 'setup_fee'):
                if name not in values:
                    continue
                if legacy:
                    selected = retained.get(name)
                    before = {'value': selected['rate'], 'unit': selected['unit']} if selected else None
                else:
                    before = record['fields'][name].get('effective')
                unit = record['requirements']['fields'][name]['unit']
                if before is None or before['unit'] != unit or number(before['value']) != number(values[name]):
                    retained.pop(name, None)
            if 'operation_rates' in values:
                previous_rates = {row['operation']: row for row in
                                  (record['fields']['operation_rates'].get('effective') or {}).get('value', [])}
                submitted_rates = {row['operation']: row for row in values['operation_rates']}
                for key in list(retained):
                    if not key.startswith('operations:'):
                        continue
                    operation = key[len('operations:'):]
                    before = ({'value': retained[key]['rate'], 'unit': retained[key]['unit']}
                              if legacy else previous_rates.get(operation))
                    after = submitted_rates.get(operation)
                    if (not before or not after or before['unit'] != after['unit'] or
                            number(before['value']) != number(after['value'])):
                        retained.pop(key, None)
            retained.update(price_snapshots)
            return retained

        def commit(state):
            record = self.records.load(task_id)
            previous = next((e for e in record['events'] if e['id'] == payload['event_id']), None)
            if stage == 'technical' and previous and previous['revision'] != record['data_revision']:
                raise WorkbenchError('该次保存后资料已有更新，请重新载入核对；不会重放旧价格来源。',
                                     'revision_conflict', 409, {'current_revision': record['data_revision']})
            expected_stage = 'customer' if stage == 'requirements' else 'technical'
            for key in values:
                require(key in record['requirements']['fields']
                        and record['requirements']['fields'][key]['stage'] == expected_stage, '该步骤不支持此字段：' + key)
            references = copy.deepcopy(payload.get('price_selections', {}))
            form_id = 'FORM-' + identifier(payload['event_id'])
            path = self.records.task_path(task_id) / ('form-' + payload['event_id'] + '.json')
            if path.exists():
                saved = read_json(path)
                require(saved['submission'] == payload, 'form event reused with different content')
                price_snapshots = saved['adopted_prices']
                simulated = saved['simulated']
                if stage == 'technical':
                    # A record may already have committed while its sidecar did
                    # not. Replay the original price decision, not a comparison
                    # against the now-updated record. Never rewrite old sources.
                    resulting_prices = (copy.deepcopy(saved['resulting_price_selections'])
                                        if 'resulting_price_selections' in saved else
                                        retained_prices(record, state, price_snapshots, legacy=True))
                    require(isinstance(resulting_prices, dict), 'saved price selections invalid')
            else:
                template_snapshot = None
                if selection is not None:
                    # Resolve only for a new form. Interrupted retries reuse the
                    # original immutable source even if it has since been disabled.
                    template_snapshot = self.templates.get(selection['id'])
                    require(template_snapshot['enabled'], '该模板已停用，请重新选择。')
                    require(template_snapshot['version'] == selection['version'], '模板版本已变化，请重新选择。')
                    unchanged_sources = {key: item for key, item in template_snapshot.get('catalog_sources', {}).items()
                                         if self._template_price_same(values, key, item)}
                    forced = self._template_references(unchanged_sources)
                    require(isinstance(references, dict), '价格选择无效。')
                    for key, spec in forced.items():
                        if key == 'operations':
                            require(isinstance(references.get('operations', {}), dict), '价格选择无效。')
                            for operation, source in spec.items():
                                supplied = references.setdefault('operations', {}).get(operation)
                                if supplied is None:
                                    references['operations'][operation] = source
                        else:
                            if references.get(key) is None:
                                references[key] = spec
                price_snapshots = self._validate_prices(record, values, context, references) if references else {}
                simulated = (record['simulated'] or any(x.get('simulated', False) for x in price_snapshots.values())
                             or bool(template_snapshot and template_snapshot['simulated']))
                saved = {'submission': payload, 'adopted_prices': price_snapshots, 'simulated': simulated}
                if template_snapshot is not None:
                    saved['template_snapshot'] = template_snapshot
                if stage == 'technical':
                    resulting_prices = retained_prices(record, state, price_snapshots)
                    saved['resulting_price_selections'] = resulting_prices
                atomic_write(path, json_text(saved))
            evidence = {'source_type': 'operator_review', 'reference': form_id,
                        'actor': ACTOR, 'simulated': simulated, 'page': None, 'region': None}
            fields = []
            for name, value in values.items():
                definition = record['requirements']['fields'][name]
                unit = definition['unit']
                fields.append({'name': name, 'raw_value': value, 'raw_unit': unit,
                               'value': value, 'unit': unit, 'status': 'verified', 'evidence': evidence,
                               'reason': '操作者通过工作台核对并提交本字段；原始表单另存。'})
            event_payload = previous['payload'] if previous else {
                'sources': [{'id': form_id, 'path': str(path), 'kind': 'operator_review',
                             'actor': ACTOR, 'simulated': simulated}], 'fields': fields}
            self.records.apply(task_id, 'apply_customer_reply' if stage == 'requirements' else 'apply_technical_input', event_payload,
                               payload['expected_revision'], payload['event_id'])
            state['pricing_context'].update(context)
            if stage == 'technical':
                state['price_selections'] = resulting_prices
                if saved.get('template_snapshot'):
                    state['template_selection'] = {key: saved['template_snapshot'][key] for key in ('id', 'version', 'name')}
        self._mutation(task_id, 'save-' + stage, payload, commit)
        return self.task(task_id)

    def _validate_prices(self, record, values, context, references):
        # Catalog returns immutable snapshots; actual adopted prices must agree.
        require(self.catalog is not None, '价格库尚不可用。')
        require(isinstance(references, dict), '价格选择无效。')
        matches = self.match_prices(record['task_id'], override_context=context, override_values=values)
        snapshots = {}
        for result in matches['results']:
            if result['kind'] == 'operation' and not result.get('operation'):
                continue
            key = 'operations:' + result['operation'] if result['kind'] == 'operation' else (
                'material_rate' if result['kind'] == 'material' else 'setup_fee')
            spec = references.get('operations', {}).get(result.get('operation')) if result['kind'] == 'operation' else references.get(key)
            if not spec:
                continue
            require(result['status'] in {'unique', 'ambiguous'}, '所选价格已失效或适用条件未完整，请重新匹配。')
            candidate = next((x for x in result['candidates'] if x['entry_id'] == spec.get('entry_id')
                              and x['version'] == spec.get('version')), None)
            require(candidate is not None, '所选价格不再适用，请重新匹配并核对。')
            if result['kind'] == 'operation':
                actual = next((x for x in values.get('operation_rates', []) if x['operation'] == result['operation']), None)
                require(actual is not None and number(actual['value']) == number(candidate['rate'])
                        and actual['unit'] == candidate['unit'], '采用的工序费率与所选价格条目不一致。')
            else:
                require(key in values and number(values[key]) == number(candidate['rate']), '采用的费用与所选价格条目不一致。')
            snapshots[key] = copy.deepcopy(candidate)
        requested = set(references) - {'operations'}
        requested |= {'operations:' + k for k in references.get('operations', {})}
        require(requested == set(snapshots), '存在未确认或不适用的价格选择。')
        return snapshots

    def match_prices(self, task_id, override_context=None, override_values=None):
        require(self.catalog is not None, '价格库尚不可用。')
        record = self.records.load(task_id)
        state = self._state(task_id)
        context = dict(state['pricing_context'])
        context.update(override_context or {})
        for name in ('material_grade', 'thickness', 'quantity', 'process_route'):
            slot = record['fields'][name]
            context[name] = (slot['effective'] or {}).get('value') if slot['status'] == 'verified' else None
        context.update({k: v for k, v in (override_values or {}).items() if k in context})
        return self.catalog.match(context)

    def issue(self, task_id, payload):
        only_keys(payload, {'event_id', 'expected_revision', 'policy'}, 'quotation request')
        spec = payload.get('policy')
        only_keys(spec, {'pricing_method', 'rate_percent', 'tax_excluded', 'freight_excluded'}, 'pricing policy')
        require(set(spec) == {'pricing_method', 'rate_percent', 'tax_excluded', 'freight_excluded'}, '请完整填写本单定价及税运口径。')
        require(spec['tax_excluded'] is True and spec['freight_excluded'] is True, '当前版本需要明确未计税费、不含运费。')
        require(isinstance(spec['rate_percent'], str) and spec['rate_percent'].strip(), '请填写本单定价比例。')
        policy = {'schema_version': 1, 'pricing_method': spec['pricing_method'],
                  'rate': format(number(spec['rate_percent']) / 100, 'f'), 'currency': 'CNY',
                  'tax_included': False, 'freight_included': False, 'simulated': True,
                  'actor': ACTOR, 'source_text': json_text(spec)}
        validate_policy(policy)
        quote_id = 'Q-' + identifier(payload['event_id'])
        require(len(quote_id) <= 79, 'event id too long')
        def commit(state):
            record = self.records.load(task_id)
            quote_path = self.quotes._quote_path(task_id, quote_id)
            if quote_path.is_dir():
                existing = self.quotes.load(task_id, quote_id)
                require(existing['pricing_policy_sha256'] == hashlib.sha256(json_text(policy).encode()).hexdigest(), 'quote policy changed')
                return quote_id
            validation = self._validation(record)
            require(validation['ready'], '资料尚未核对完整，请先补齐需求、技术信息及采用价格。')
            path = self.records.task_path(task_id) / ('policy-' + payload['event_id'] + '.json')
            if path.exists():
                require(read_json(path) == policy, 'policy event reused with different content')
            else:
                atomic_write(path, json_text(policy))
            self.quotes.issue(task_id, quote_id, path, payload['expected_revision'])
            state['last_policy'] = spec
            return quote_id
        result_id = self._mutation(task_id, 'quote', payload, commit)
        return self._quote_result(task_id, result_id)

    def start_read(self, task_id, payload):
        only_keys(payload, {'event_id', 'expected_revision'}, 'reading request')
        require(self.bridge_factory is not None, 'AI 读图服务尚未配置。')
        def commit(state):
            run = state.get('run')
            require(not run or run['status'] not in {'starting', 'running', 'stopping', 'unknown'}, '读图仍在进行，请等待或先停止。')
            record = self.records.load(task_id)
            files = [x for x in record['sources'].values() if x['kind'] == 'drawing']
            require(files, '请先上传本次图纸。')
            state['run'] = {'id': payload['event_id'], 'status': 'starting', 'session_id': None,
                            'base_revision': record['data_revision'], 'started_at': stamp(), 'error': None,
                            'warnings': [], 'candidates_applied': False, 'source_ids': [x['id'] for x in files],
                            'evidence_version': 1, 'reading_scope': 'full_drawing', 'observations': []}
            self._set_run_progress(state['run'])
        self._mutation(task_id, 'read', payload, commit)
        self._launch_read(task_id)
        return self.task(task_id)

    def _launch_read(self, task_id):
        with self.lock(task_id):
            state = self._state(task_id)
            run = state.get('run')
            key = (task_id, run['id']) if run else None
            if not run or run['status'] not in {'starting', 'running', 'stopping', 'unknown'} or key in self._workers:
                return
            self._workers.add(key)
        threading.Thread(target=self._read_worker, args=key, daemon=True, name='drawing-' + task_id).start()

    def resume_reads(self):
        for task in self.tasks():
            if task.get('run'):
                self._launch_read(task['id'])

    def stop_read(self, task_id, payload):
        only_keys(payload, {'event_id', 'expected_revision'}, 'stop request')
        def commit(state):
            run = state.get('run')
            require(run is not None and run['status'] in {'starting', 'running', 'stopping', 'unknown'}, '没有进行中的读图任务。')
            run['status'] = 'stopping'
            run['stop_requested'] = True
            self._set_run_progress(run)
        self._mutation(task_id, 'stop-read', payload, commit)
        self._launch_read(task_id)
        return self.task(task_id)

    def _read_worker(self, task_id, run_id):
        key = (task_id, run_id)
        workspace = self.reader_root / task_id / run_id
        try:
            workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
            bridge = self.bridge_factory(workspace)
            with self.lock(task_id):
                state = self._state(task_id)
                run = state['run']
                if run['id'] != run_id:
                    return
                if run.get('stop_requested'):
                    run['status'] = 'stopping'
                    self._save_state(task_id, state)
                if run['status'] == 'stopping' and not run['session_id']:
                    run.update(status='stopped', finished_at=stamp())
                    self._set_run_progress(run)
                    self._save_state(task_id, state)
                    return
                session_id = run.get('session_id')
                record = self.records.load(task_id)
                if not session_id and run['status'] != 'stopping':
                    self._set_run_progress(run, {'stage': 'preparing_files', 'label': '正在准备本次上传的图纸附件'})
                    self._save_state(task_id, state)
            attachments = workspace / 'attachments'
            attachments.mkdir(mode=0o700, exist_ok=True)
            files = []
            for source_id in run.get('source_ids', [k for k, x in record['sources'].items() if x['kind'] == 'drawing']):
                source = record['sources'][source_id]
                path = attachments / (identifier(source_id) + Path(source['path']).suffix)
                if path.exists():
                    require(not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest() == source['sha256'],
                            'isolated attachment changed')
                else:
                    content = (self.records.task_path(task_id) / source['path']).read_bytes()
                    require(hashlib.sha256(content).hexdigest() == source['sha256'], 'source changed before reading')
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                files.append({'id': source_id, 'path': str(path), 'original_name': source['original_name']})
            if not session_id:
                def on_session(value):
                    with self.lock(task_id):
                        state = self._state(task_id)
                        if state['run']['id'] == run_id:
                            state['run']['session_id'] = value
                            if state['run']['status'] != 'stopping':
                                self._set_run_progress(state['run'], {'stage': 'submitting', 'label': '会话已建立，正在提交读图请求'})
                            self._save_state(task_id, state)
                            if state['run']['status'] == 'stopping':
                                raise WorkbenchError('读图在发送模型前已停止。', 'stopped_before_dispatch')
                started = bridge.start(task_id, files, record['inquiry'], workspace / 'candidates.json', on_session=on_session)
                session_id = started['session_id']
                with self.lock(task_id):
                    state = self._state(task_id)
                    if state['run']['id'] != run_id:
                        return
                    state['run']['session_id'] = session_id
                    if state['run']['status'] != 'stopping':
                        state['run']['status'] = started.get('status', 'running')
                    state['run']['error'] = runtime_message(started.get('error'))
                    if started.get('status') == 'failed' and state['run']['status'] == 'stopping':
                        state['run'].update(status='stopped', error=None, finished_at=stamp())
                    self._set_run_progress(state['run'])
                    self._save_state(task_id, state)
                    if started.get('status') == 'failed':
                        return
            deadline = time.monotonic() + 1800
            stop_sent = False
            while time.monotonic() < deadline:
                with self.lock(task_id):
                    state = self._state(task_id)
                    run = state['run']
                    if run['id'] != run_id:
                        return
                    stopping = run['status'] == 'stopping'
                    cached = copy.deepcopy(run.get('initial_result'))
                if stopping and not stop_sent:
                    # A persisted review owns cancellation once it has begun.
                    if not run.get('review', {}).get('session_id'):
                        cancelled = bridge.stop(session_id)
                        stop_sent = cancelled.get('accepted') is True
                result = cached if cached else bridge.inspect(session_id)
                if result['status'] == 'completed' and (not stopping or run.get('review', {}).get('session_id')):
                    result = review_reading(self, task_id, run_id, result, workspace, files, record['inquiry'], deadline)
                status = result['status']
                with self.lock(task_id):
                    state = self._state(task_id)
                    run = state['run']
                    if run['id'] != run_id:
                        return
                    run['warnings'] = result.get('warnings', [])
                    run['error'] = runtime_message(result.get('error'))
                    if 'observations' in result:
                        run['observations'] = self._observations(self.records.load(task_id), run, result['observations'], None)
                    if run['status'] == 'stopping':
                        self._set_run_progress(run, checked=True)
                    else:
                        self._set_run_progress(run, result.get('progress'), checked=True)
                    if status in {'completed', 'failed', 'waiting', 'stopped'}:
                        if state['run']['status'] == 'stopping':
                            run.update(status='stopped', finished_at=stamp(), error=None)
                            self._set_run_progress(run)
                        elif status == 'completed':
                            run['recognition_result'] = {'status': 'completed',
                                                         'candidates': copy.deepcopy(result.get('candidates', []))}
                            if 'parts' in result:
                                run['recognition_result']['parts'] = self._parts(
                                    self.records.load(task_id), run, result['parts'])
                            self._set_run_progress(run, {'stage': 'saving', 'label': '正在校验并保存识别结果'})
                            self._save_state(task_id, state)
                            self._apply_reading(task_id, run, result)
                            run.update(status='completed', finished_at=stamp(), error=None)
                            self._set_run_progress(run)
                        else:
                            run.update(status=status, finished_at=stamp(), error=runtime_message(result.get('error')))
                            self._set_run_progress(run)
                        self._save_state(task_id, state)
                        return
                    if run['status'] != 'stopping':
                        run['status'] = status if status in {'running', 'unknown'} else 'running'
                    self._save_state(task_id, state)
                time.sleep(2)
            raise WorkbenchError('读图等待超过 30 分钟，运行状态仍需确认；请先停止并检查模型服务。', 'run_state_unknown')
        except Exception as exc:
            with self.lock(task_id):
                state = self._state(task_id)
                if state.get('run', {}).get('id') == run_id:
                    uncertain = isinstance(exc, WorkbenchError) and exc.code == 'run_state_unknown'
                    state['run'].update(status='unknown' if uncertain else 'failed',
                                        error='读图未完成：' + str(exc)[:700], finished_at=stamp())
                    self._set_run_progress(state['run'])
                    self._save_state(task_id, state)
        finally:
            self._workers.discard(key)

    def _apply_reading(self, task_id, run, result):
        event_id = 'READ-' + run['id']
        record = self.records.load(task_id)
        if 'parts' in result:
            # Part annotations are recognition output only. They survive a
            # concurrent human edit and never become single-product fields.
            cached = run.setdefault('recognition_result', {
                'status': 'completed', 'candidates': copy.deepcopy(result.get('candidates', []))})
            cached['parts'] = self._parts(record, run, result['parts'])
        if any(e['id'] == event_id for e in record['events']):
            run['candidates_applied'] = True
            return
        if record['data_revision'] != run['base_revision']:
            run['warnings'] = list(run.get('warnings', [])) + ['读图期间资料有更新，候选已留存但未覆盖本次人工修改；请重新读图或手工核对。']
            return
        fields = []
        # A drawing packet with several parts is not a single product. Its
        # global summary stays visible without adopting the packet's totals or
        # a selected part's properties as this legacy single-product record.
        candidates = [] if len(result.get('parts', [])) > 1 else result.get('candidates', [])
        for item in candidates:
            name = item.get('name')
            require(name in record['fields'], 'model returned unsupported field')
            evidence = item.get('evidence') or {}
            reference = evidence.get('reference')
            require(reference in record['sources'] and record['sources'][reference]['kind'] in {'drawing', 'inquiry'}, 'model evidence outside current task')
            source = record['sources'][reference]
            fields.append({'name': name, 'raw_value': item.get('raw_value'), 'raw_unit': item.get('raw_unit'),
                           'value': item.get('value'), 'unit': item.get('unit'), 'status': 'pending_review',
                           'evidence': {'source_type': source['kind'], 'reference': reference,
                                        'page': evidence.get('page'), 'region': evidence.get('region'),
                                        'actor': source['actor'], 'simulated': source['simulated']},
                           'reason': item.get('reason') or 'AI 读图候选，待人工核对。'})
        self.records.apply(task_id, 'record_reading', {'fields': fields}, run['base_revision'], event_id)
        run['candidates_applied'] = True
