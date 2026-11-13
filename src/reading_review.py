"""One explicitly scoped, durable drawing review; no business record mutation."""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import time

from prep_record import identifier, require
from reading_inputs import copy_pdf_views
from reading_details import copy_detail_views
from reading_parts import merge_parts
from reading_semantics import compatible_surface, surface_tokens
from recognition_view import RECOGNITION_FIELDS


CRITICAL_FIELDS = ('material_grade', 'thickness', 'drawing_units')
REVIEW_FAILURE_WARNING = '关键标注回查未完成，已保留首轮有效结果和原始标注，请人工核对。'
COMPLETENESS_FAILURE_WARNING = '图纸查漏复读未完成，已保留首轮有效结果和原始标注；尺寸、备注及表格可能仍有遗漏，请结合原图核对。'
CONFLICT_WARNING = '存在单位待确认或相互冲突的标注，相关规范参数暂未采用，请人工核对原始证据。'


def _failure_warning(run):
    return (COMPLETENESS_FAILURE_WARNING if run.get('reading_scope') == 'full_drawing'
            else REVIEW_FAILURE_WARNING)


def _saved_result(value):
    """Keep validated business projections, never model text or progress."""
    result = {'status': 'completed', 'error': None,
            'candidates': copy.deepcopy(value.get('candidates', [])),
            'observations': copy.deepcopy(value.get('observations', [])),
            'warnings': copy.deepcopy(value.get('warnings', []))}
    if 'parts' in value:
        result['parts'] = copy.deepcopy(value['parts'])
    return result


def _unique_observations(rows):
    seen, result = set(), []
    for row in rows:
        identity = {name: row.get(name) for name in
                    ('name', 'raw_text', 'raw_unit', 'status', 'evidence')}
        if 'source_name' in row:
            identity['source_name'] = row['source_name']
        key = json.dumps(identity,
                         ensure_ascii=False, sort_keys=True, allow_nan=False)
        if key not in seen:
            seen.add(key)
            result.append(copy.deepcopy(row))
    return result


def _same_candidate(first, second, definitions):
    if first.get('unit') != second.get('unit'):
        return False
    kind = definitions[first['name']]['kind']
    if kind in {'length', 'quantity', 'decimal'}:
        try:
            return Decimal(str(first.get('value'))) == Decimal(str(second.get('value')))
        except (InvalidOperation, ValueError):
            return False
    return first.get('value') == second.get('value') or compatible_surface(first, second)


def _checked_review(app, record, run, value, fields):
    require(isinstance(value, dict) and value.get('status') == 'completed', 'completed review result required')
    candidates = value.get('candidates', [])
    require(isinstance(candidates, list) and len(candidates) <= len(fields), 'bounded review candidates required')
    seen = set()
    for item in candidates:
        require(isinstance(item, dict) and isinstance(item.get('name'), str)
                and item['name'] in fields and item['name'] not in seen,
                'review candidate outside requested fields or duplicated')
        seen.add(item['name'])
    observed = app._observations(record, run, value.get('observations', []), 'review')
    require(all(item['name'] in fields for item in observed), 'review observation outside requested fields')
    result = _saved_result(value)
    if 'parts' in result:
        result['parts'] = app._parts(record, run, result['parts'], 'review')
    return result


def _merge(app, record, run, initial, reviewed=None, *, failed=False):
    observations = app._observations(record, run, initial.get('observations', []), 'initial')
    if reviewed is not None:
        observations += app._observations(record, run, reviewed.get('observations', []), 'review')
    definitions = record['requirements']['fields']
    candidates = {item['name']: copy.deepcopy(item) for item in initial.get('candidates', [])}
    conflicts = set()
    for item in (reviewed or {}).get('candidates', []):
        name = item['name']
        if name in candidates and not _same_candidate(candidates[name], item, definitions):
            conflicts.add(name)
            for candidate, source_pass in ((candidates[name], 'initial'), (item, 'review')):
                raw = candidate.get('raw_value')
                raw_text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, allow_nan=False)
                value = {'name': name, 'raw_text': raw_text, 'raw_unit': candidate.get('raw_unit'),
                         'status': 'conflict', 'evidence': copy.deepcopy(candidate['evidence']),
                         'reason': '两轮规范候选不一致，保留各自原始证据，待人工核对。'}
                observations += app._observations(record, run, [value], source_pass)
        elif name not in candidates:
            candidates[name] = copy.deepcopy(item)
        elif compatible_surface(candidates[name], item):
            # Keep the more complete same-source description without treating
            # an omitted RAL color as a contradictory finish.
            if len(surface_tokens(item.get('value'))) > len(surface_tokens(candidates[name].get('value'))):
                candidates[name] = copy.deepcopy(item)
    for observation in observations:
        if observation['name'] in conflicts:
            observation['status'] = 'conflict'
            observation['reason'] = '两轮规范候选不一致，保留各自原始证据，待人工核对。'
    observations = _unique_observations(observations)
    unresolved = {item['name'] for item in observations if item['status'] in {'unit_unknown', 'conflict'}} | conflicts
    warnings = list(initial.get('warnings', [])) + list((reviewed or {}).get('warnings', []))
    if failed:
        warnings.append(_failure_warning(run))
    if unresolved:
        warnings.append(CONFLICT_WARNING)
    result = {'status': 'completed', 'error': None,
            'candidates': [item for name, item in candidates.items() if name not in unresolved],
            'observations': observations, 'warnings': list(dict.fromkeys(warnings))}
    if 'parts' in initial or reviewed is not None and 'parts' in reviewed:
        first_parts = app._parts(record, run, initial.get('parts', []), 'initial')
        next_parts = app._parts(record, run, (reviewed or {}).get('parts', []), 'review')
        result['parts'] = merge_parts(first_parts, next_parts)
    return result


def _copy_files(app, record, run, workspace, files):
    review_directory = workspace / 'review'
    require(not review_directory.is_symlink(), 'review workspace symlink prohibited')
    review_directory.mkdir(mode=0o700, exist_ok=True)
    attachments = review_directory / 'attachments'
    require(not attachments.is_symlink(), 'review attachments symlink prohibited')
    attachments.mkdir(mode=0o700, exist_ok=True)
    copied = []
    require({item['id'] for item in files} == set(run['source_ids']), 'review attachment set changed')
    for item in files:
        source = record['sources'][item['id']]
        source_path = Path(item['path'])
        require(source_path.is_relative_to(workspace) and not source_path.is_symlink(),
                'review source outside initial workspace')
        content = source_path.read_bytes()
        require(hashlib.sha256(content).hexdigest() == source['sha256'], 'review source changed')
        destination = attachments / (identifier(item['id']) + source_path.suffix)
        if destination.exists():
            require(not destination.is_symlink()
                    and hashlib.sha256(destination.read_bytes()).hexdigest() == source['sha256'],
                    'review attachment changed')
        else:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        copied.append({'id': item['id'], 'path': str(destination), 'original_name': item['original_name']})
    # Review sees the identical rendered page bytes and page mapping. This is
    # separate from business source IDs, which continue to identify originals.
    source_files = [{**item, 'sha256': record['sources'][item['id']]['sha256']} for item in files]
    target_files = [{**item, 'sha256': record['sources'][item['id']]['sha256']} for item in copied]
    pdf_views = copy_pdf_views(workspace, review_directory, source_files, target_files)
    pinned = workspace / '.dsh-bridge-run.json'
    if pinned.exists():
        require(not pinned.is_symlink() and pinned.is_file(), 'invalid initial bridge manifest')
        detail_views = json.loads(pinned.read_text()).get('detail_views')
        copy_detail_views(workspace, review_directory, source_files, target_files, detail_views, pdf_views)
    return review_directory, copied


def review_reading(app, task_id, run_id, initial, workspace, files, inquiry, deadline):
    """Return a safely merged result, or stopped; all RPC runs outside UI flock.

    The caller owns the final record revision check and READ event. Session IDs,
    first results and review results are durable before they can be applied.
    """
    from workbench_store import WorkbenchError

    workspace = Path(workspace)
    with app.lock(task_id):
        state = app._state(task_id)
        run = state.get('run')
        if not run or run['id'] != run_id:
            return {'status': 'stopped'}
        if run.get('evidence_version') != 1 or 'observations' not in initial:
            return copy.deepcopy(initial)
        record = app.records.load(task_id)
        app._observations(record, run, initial['observations'], 'initial')
        if 'initial_result' not in run:
            run['initial_result'] = _saved_result(initial)
            if 'parts' in initial:
                run['initial_result']['parts'] = app._parts(record, run, initial['parts'], 'initial')
        initial = copy.deepcopy(run['initial_result'])
        first_observations = app._observations(record, run, initial['observations'], 'initial')
        run['observations'] = first_observations
        review = run.get('review')
        if review and review.get('stop_requested'):
            run['status'] = 'stopping'
        if review is None:
            comprehensive = run.get('reading_scope') == 'full_drawing'
            if comprehensive:
                # One durable second pass checks omitted source text even when
                # the first pass already found the basic material parameters.
                fields = [name for name in record['requirements']['fields'] if name in RECOGNITION_FIELDS]
            else:
                present = {item['name'] for item in initial.get('candidates', [])}
                fields = [name for name in CRITICAL_FIELDS
                          if name not in present and record['fields'][name]['status'] != 'verified']
                for observation in first_observations:
                    if observation['status'] in {'unit_unknown', 'conflict', 'unreadable'} and observation['name'] not in fields:
                        fields.append(observation['name'])
            review = {'status': 'starting' if fields else 'not_needed', 'fields': fields,
                      'session_id': None, 'error': None}
            if comprehensive:
                review['mode'] = 'completeness'
            run['review'] = review
            if fields:
                # Different sessions have independent event sequences. Reset
                # only when review is created, not on each subsequent poll.
                run['progress'] = {**run.get('progress', {}), 'last_event_seq': None,
                                   'last_activity_at': None, 'events': []}
                app._set_run_progress(run, {'stage': 'reviewing', 'label': '准备逐页查漏复读' if comprehensive else '准备回查关键标注'})
        app._save_state(task_id, state)
        if review['status'] == 'stopped':
            return {'status': 'stopped'}
        if review['status'] == 'not_needed':
            merged = _merge(app, record, run, initial)
            run['observations'] = merged['observations']
            app._save_state(task_id, state)
            return merged
        if review['status'] in {'completed', 'failed'}:
            if run['status'] == 'stopping':
                review.update(status='stopped', error=None)
                app._save_state(task_id, state)
                return {'status': 'stopped'}
            reviewed = None
            if review['status'] == 'completed':
                try:
                    reviewed = _checked_review(app, record, run, review.get('result'), review['fields'])
                except (ValueError, KeyError, TypeError):
                    review.update(status='failed', error=_failure_warning(run))
                    review.pop('result', None)
            merged = _merge(app, record, run, initial, reviewed, failed=review['status'] == 'failed')
            run['observations'] = merged['observations']
            app._save_state(task_id, state)
            return merged
        if run['status'] == 'stopping' and not review.get('session_id'):
            review.update(status='stopped', error=None)
            app._save_state(task_id, state)
            return {'status': 'stopped'}
        fields = list(review['fields'])
        session_id = review.get('session_id')
        label = ('正在复读图纸，检查尺寸、备注与表格遗漏' if review.get('mode') == 'completeness'
                 else '回查关键标注：' + '、'.join(record['requirements']['fields'][name]['label'] for name in fields))

    def timeout():
        raise WorkbenchError('关键标注回查等待超过本轮期限，正在保留原会话以待确认。', 'run_state_unknown')

    def save_failed():
        with app.lock(task_id):
            state = app._state(task_id)
            run = state.get('run')
            if not run or run['id'] != run_id or run['status'] == 'stopping':
                return {'status': 'stopped'}
            run['review'].update(status='failed', error=_failure_warning(run))
            merged = _merge(app, app.records.load(task_id), run, initial, failed=True)
            run['observations'] = merged['observations']
            app._save_state(task_id, state)
            return merged

    bridge = None
    try:
        if time.monotonic() >= deadline:
            timeout()
        review_directory, review_files = _copy_files(app, record, run, workspace, files)
        bridge = app.bridge_factory(review_directory)
        if not session_id:
            if time.monotonic() >= deadline:
                timeout()
            def on_session(value):
                with app.lock(task_id):
                    state = app._state(task_id)
                    run = state.get('run')
                    if not run or run['id'] != run_id:
                        raise WorkbenchError('原读图任务已结束，不发送回查请求。', 'stopped_before_dispatch')
                    run['review']['session_id'] = value
                    app._save_state(task_id, state)
                    if run['status'] == 'stopping':
                        raise WorkbenchError('读图在回查发送前已停止。', 'stopped_before_dispatch')
            with app.lock(task_id):
                state = app._state(task_id)
                run = state.get('run')
                if not run or run['id'] != run_id:
                    return {'status': 'stopped'}
                if run['status'] == 'stopping':
                    run['review'].update(status='stopped', error=None)
                    app._save_state(task_id, state)
                    return {'status': 'stopped'}
            started = bridge.start(task_id, review_files, inquiry, review_directory / 'candidates.json',
                                   review_fields=fields, on_session=on_session)
            session_id = started['session_id']
            with app.lock(task_id):
                state = app._state(task_id)
                run = state.get('run')
                if not run or run['id'] != run_id:
                    return {'status': 'stopped'}
                run['review'].update(session_id=session_id,
                                     status=started.get('status', 'unknown'),
                                     error=REVIEW_FAILURE_WARNING if started.get('status') == 'failed' else None)
                app._set_run_progress(run, {'stage': 'reviewing', 'label': label})
                stopped_before_dispatch = run['status'] == 'stopping' and started.get('status') == 'failed'
                if stopped_before_dispatch:
                    run['review'].update(status='stopped', error=None)
                app._save_state(task_id, state)
            if stopped_before_dispatch:
                return {'status': 'stopped'}
            if started.get('status') == 'failed':
                return save_failed()

        stop_sent = False
        while time.monotonic() < deadline:
            with app.lock(task_id):
                state = app._state(task_id)
                run = state.get('run')
                if not run or run['id'] != run_id:
                    return {'status': 'stopped'}
                stopping = run['status'] == 'stopping'
                if stopping:
                    run['review']['stop_requested'] = True
                    app._save_state(task_id, state)
            if stopping and not stop_sent:
                stop_sent = bridge.stop(session_id).get('accepted') is True
            result = bridge.inspect(session_id)
            with app.lock(task_id):
                state = app._state(task_id)
                run = state.get('run')
                if not run or run['id'] != run_id:
                    return {'status': 'stopped'}
                stopping = run['status'] == 'stopping'
                if stopping:
                    run['review']['stop_requested'] = True
                    app._save_state(task_id, state)
            # A stop can arrive while the RPC returns a late completed turn.
            if stopping and not stop_sent:
                stop_sent = bridge.stop(session_id).get('accepted') is True
            with app.lock(task_id):
                state = app._state(task_id)
                run = state.get('run')
                if not run or run['id'] != run_id:
                    return {'status': 'stopped'}
                review = run['review']
                status = result.get('status')
                if run['status'] == 'stopping':
                    review['stop_requested'] = True
                    if status in {'completed', 'failed', 'waiting', 'stopped'}:
                        review.update(status='stopped', error=None)
                        app._save_state(task_id, state)
                        return {'status': 'stopped'}
                    app._set_run_progress(run, checked=True)
                else:
                    progress = copy.deepcopy(result.get('progress') or {})
                    progress.update(stage='reviewing', label=label)
                    app._set_run_progress(run, progress, checked=True)
                    if status == 'completed':
                        checked = _checked_review(app, app.records.load(task_id), run, result, fields)
                        review.update(status='completed', error=None, result=checked)
                        merged = _merge(app, app.records.load(task_id), run, initial, review['result'])
                        run['observations'] = merged['observations']
                        app._save_state(task_id, state)
                        return merged
                    if status in {'failed', 'waiting', 'stopped'}:
                        review.update(status='failed', error=_failure_warning(run))
                        merged = _merge(app, app.records.load(task_id), run, initial, failed=True)
                        run['observations'] = merged['observations']
                        app._save_state(task_id, state)
                        return merged
                    review['status'] = status if status in {'running', 'unknown'} else 'unknown'
                    review['error'] = '回查状态待确认，正在查询原会话。' if review['status'] == 'unknown' else None
                app._save_state(task_id, state)
            time.sleep(2)
        timeout()
    except WorkbenchError as exc:
        if exc.code == 'run_state_unknown':
            raise
        return save_failed()
    except Exception:
        # A failed optional review must not discard independently valid first
        # results. BaseException intentionally escapes process-exit simulation.
        return save_failed()
