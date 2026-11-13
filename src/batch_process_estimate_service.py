"""Cached native quantities -> immutable reviewable estimate, without AI/CAD.

Shares the existing prepared pricing journal and recovery transaction. The
server chooses every quantity/rate; a browser only confirms its bound digest.
"""
from __future__ import annotations

import copy
import hashlib
import re
import uuid

from prep_record import atomic_write, digest, only_keys, stamp
from batch_quote_service import demand, BUSY, RUN_ID, MAX_PRICING_BYTES, _json
from batch_process_estimate import (VERSION, SCOPE_LABEL, MATERIAL_LABEL, ROUTE_ID,
                                    ROUTE_RATES, safe_quantities)


class BatchProcessEstimateService:
    def __init__(self, owner):
        self.owner = owner

    def _native_token(self, native_id, sha):
        value = self.owner.manufacturing.get(native_id)
        token = value.get('geometry_run_id')
        demand(value.get('document_id') == native_id and value.get('source_sha256') == sha
               and isinstance(token, str) and re.fullmatch(r'R[0-9a-f]{32}', token)
               and value.get('status') not in BUSY,
               '现有原生工程量缓存尚未就绪；此操作不会启动新的读图或CAD计算。', 'geometry_not_ready', 409)
        return {'native_id': native_id, 'source_sha256': sha, 'geometry_run_id': token}

    def _context(self, batch_id, run_id):
        owner = self.owner
        with owner._lock(batch_id):
            state = copy.deepcopy(owner._read(batch_id))
            result, review, recognition, binding, _ = owner._engineering_pricing_context(state, run_id)
        documents, tokens, unavailable = [], [], []
        file_ids = list(dict.fromkeys(line.get('file_id') for line in result['lines']))
        for file_id in file_ids:
            try:
                source = next(row for row in result['source_files'] if row.get('id') == file_id)
                sha = source['sha256']
                native_id = 'CAD-' + sha[:32]
                before = self._native_token(native_id, sha)
                context = owner._preview_file(batch_id, state, result, file_id)
                lines = [line for line in result['lines'] if line.get('file_id') == file_id]
                for line in lines:
                    owner._preview_match(line, context['geometry'], lines)
                compact = owner._measurements(context['geometry'])
                after = self._native_token(native_id, sha)
                demand(before == after, '原生缓存已更新，请重新读取估算方案。', 'source_conflict', 409)
                tokens.append({**after, 'file_id': file_id, 'measurement_digest': digest(compact)})
                documents.append({'id': file_id, 'name': source.get('name'), 'geometry': compact})
            except (ValueError, OSError, TypeError, KeyError, StopIteration, AttributeError):
                unavailable.append(file_id)
        from batch_process_quote import apply_process_costing
        prepared = copy.deepcopy(result)
        adopted_route = ((result.get('process_estimate') or {}).get('route_assumption') or {}).get('id')
        if not isinstance(result.get('cost_sheet'), dict):
            apply_process_costing(prepared, documents, route_assumption=adopted_route)
        # Technical-only attachment: strict flat pricing keeps its original
        # blank volume and blockers. A receipt does not request a new quote.
        from batch_quote_costing import _groups
        from batch_process_quote import _instances
        from batch_process_estimate import validated_quantities
        for document in documents:
            compact = document['geometry']
            grouped = _groups(compact['solids'])
            for line in prepared['lines']:
                if line.get('file_id') != document['id']:
                    continue
                manufacturing = line.setdefault('manufacturing', {})
                if manufacturing.get('quotation_quantities'):
                    continue
                instances = _instances(grouped.get(line.get('native_definition'), []))
                if not instances or len(instances[0]) != 1:
                    continue
                receipt = validated_quantities(instances[0][0], compact['source_sha256'])
                if receipt and receipt.get('method') == 'native_flat_profile':
                    manufacturing['quotation_quantities'] = receipt
        geometry_binding = {'version': VERSION, 'files': tokens, 'unavailable_file_ids': unavailable,
                            'documents_digest': digest(documents)}
        signature = digest({'source_binding': binding, 'geometry_binding': geometry_binding,
                            'settings': result['settings'], 'estimate_version': VERSION,
                            'route_rates': ROUTE_RATES, 'adopted_route': adopted_route})
        return state, result, review, recognition, binding, documents, geometry_binding, signature, prepared

    @staticmethod
    def _public_plan(context):
        state, result, _, recognition, binding, documents, geometry, signature, prepared = context
        summary = prepared.get('manufacturing') or {}
        manual = isinstance(result.get('cost_sheet'), dict)
        lines = []
        has_receipt = False
        for line in prepared['lines']:
            manufacturing = line.get('manufacturing') or {}
            receipt = manufacturing.get('quotation_quantities')
            projected = safe_quantities(receipt)
            has_receipt = has_receipt or projected is not None
            lines.append({'id': line['id'], 'part_name': line.get('part_name'),
                'status': projected['status'] if projected else 'unresolved',
                'method': projected.get('method') if projected else None,
                'quantities': projected['quantities'] if projected else {},
                'quantity_statuses': projected['quantity_statuses'] if projected else {},
                'measurements': projected.get('measurements', {}) if projected else {},
                'material_ready': bool((manufacturing.get('material') or {}).get('usage_kg')),
                'basis': projected['basis'] if projected else ['没有已保存的报价工程量；不会把缺项按零计费。'],
                'blockers': [{'code': row['code'], 'message': row['message']} for row in manufacturing.get('blockers', [])],
                'manufacturing_release': False})
        already = manual or isinstance(result.get('process_estimate'), dict) and result['process_estimate'].get('geometry_digest') == digest(geometry)
        if (result.get('manufacturing') or {}).get('estimate_version') == VERSION:
            def receipt_signature(value):
                return [(line['id'], line.get('material_profile'), line.get('material_estimation_label'),
                         (line.get('manufacturing') or {}).get('quotation_quantities')) for line in value['lines']]
            already = already or receipt_signature(result) == receipt_signature(prepared)
        current = (state.get('result') or {}).get('run_id')
        proposal_ids = [line['id'] for line in lines if line.get('method') == 'native_brep_inventory'
                        and line['quantities'].get('bend_count') is None and line['quantities'].get('cut_length_mm') is not None]
        adopted_route = ((result.get('process_estimate') or {}).get('route_assumption') or {}).get('id')
        fallback = any((line.get('manufacturing') or {}).get('estimate_version') == VERSION for line in prepared['lines'])
        can = (has_receipt and fallback and summary.get('estimated_cost_subtotal') is not None
               and state['status'] not in BUSY and recognition['status'] not in BUSY
               and current == result['run_id'] and not isinstance(result.get('cost_sheet'), dict)
               and (not already or bool(proposal_ids) and adopted_route != ROUTE_ID))
        assumed = any(line.get('material_estimation_label') == MATERIAL_LABEL for line in prepared['lines'])
        complete = (bool(result['lines']) and all(line.get('priced') is True for line in result['lines'])
                    and (result.get('totals') or {}).get('total_price') is not None) if manual else bool(summary.get('complete'))
        subtotal = (result.get('totals') or {}).get('total_price') if manual else summary.get('estimated_price_subtotal')
        return {'plan_digest': signature, 'expected_revision': state['revision'],
                'current_run_id': current, 'can_estimate': bool(can), 'already_applied': already,
                'status': 'ready' if complete else 'partial' if subtotal is not None else 'blocked',
                'estimated_subtotal': subtotal,
                'total_price': (prepared.get('totals') or {}).get('total_price') if complete else None,
                'pending_parts': sum(line.get('priced') is not True for line in result['lines']) if manual else summary.get('pending_parts', len(lines)),
                'route_proposal': {'id': ROUTE_ID, 'version': ROUTE_RATES['version'],
                    'label': '钣金成形＋拼焊工程假设方案', 'requires_confirmation': adopted_route != ROUTE_ID,
                    'applies_to': proposal_ids,
                    'description': '对路线不明确的零件按每件成形和拼焊预算暂估；不代表已识别焊长、工时或完整折弯次数。'} if proposal_ids and not manual else None,
                'lines': lines, 'material_estimate': {'label': MATERIAL_LABEL if assumed else '沿用本单材料采用条件',
                                                   'is_assumption': assumed},
                'scope_label': '已采用工厂核价结果，仍待审核；自动估算不会覆盖本版' if manual else SCOPE_LABEL,
                'simulated': True, 'manufacturing_release': False}

    def get(self, batch_id, run_id):
        return self._public_plan(self._context(batch_id, run_id))

    def validate_prepared(self, state, record):
        """Recheck current native run before commit, including crash recovery."""
        binding = record.get('geometry_binding')
        result = record.get('result')
        estimate = result.get('process_estimate') if isinstance(result, dict) else None
        demand(isinstance(binding, dict) and binding.get('version') == VERSION
               and isinstance(binding.get('files'), list) and isinstance(estimate, dict)
               and estimate.get('geometry_digest') == digest(binding)
               and estimate.get('measurements_digest') == binding.get('documents_digest')
               and digest(estimate.get('measurements')) == binding.get('documents_digest'),
               '待恢复工程量估算的来源摘要不一致。', 'source_conflict', 409)
        sources = {row['id']: row for row in result['source_files']}
        for item in binding['files']:
            source = sources.get(item.get('file_id')) or {}
            demand(source.get('sha256') == item.get('source_sha256'), '估算文件摘要不一致。', 'source_conflict', 409)
            token = self._native_token(item['native_id'], item['source_sha256'])
            demand(all(item.get(key) == value for key, value in token.items()),
                   '原生工程量缓存已更新，不能提交旧估算。', 'source_conflict', 409)

    def apply(self, batch_id, run_id, payload):
        owner = self.owner
        only_keys(payload, {'event_id', 'expected_revision', 'plan_digest', 'route_assumption'}, 'process estimate')
        demand('route_assumption' not in payload or payload['route_assumption'] == ROUTE_ID,
               '工程假设方案无效。')
        event = owner._event(payload.get('event_id'))
        demand(type(payload.get('expected_revision')) is int and payload['expected_revision'] >= 0
               and isinstance(payload.get('plan_digest'), str) and re.fullmatch(r'[0-9a-f]{64}', payload['plan_digest'])
               and isinstance(run_id, str) and RUN_ID.fullmatch(run_id), '请重新读取估算报价方案。')
        request_signature = digest([run_id, payload])
        with owner._lock(batch_id):
            state = owner._read(batch_id)
            previous = next(((path, row) for path, row in owner._pricing_records(batch_id) if row['event_id'] == event), None)
            if previous:
                path, record = previous
                demand(record.get('kind') == 'process_estimate' and record.get('signature') == request_signature,
                       '相同估算请求的内容已变化。', 'event_conflict', 409)
                record = owner._commit_engineering_pricing(state, path, record)
                return {'batch': owner._public(state), 'run_id': record['run_id'],
                        'source_run_id': run_id, 'replayed': True}
        context = self._context(batch_id, run_id)
        state, result, review, recognition, binding, documents, geometry, signature, prepared = context
        plan = self._public_plan(context)
        demand(payload['plan_digest'] == signature and payload['expected_revision'] == state['revision'],
               '来源或报价版本已更新，请刷新估算方案后重试。', 'source_conflict', 409)
        if plan['already_applied'] and plan['current_run_id'] == run_id and not payload.get('route_assumption'):
            return {'batch': owner.get(batch_id), 'run_id': run_id, 'source_run_id': run_id,
                    'replayed': True, 'already_applied': True}
        demand(plan['can_estimate'], '此版本没有可采用的现存工程量，或已有新的报价。', 'estimate_not_ready', 409)
        adopted_route = payload.get('route_assumption') or (((result.get('process_estimate') or {}).get('route_assumption') or {}).get('id'))
        if adopted_route:
            demand(isinstance(plan.get('route_proposal'), dict) and plan['route_proposal']['applies_to'],
                   '本单没有需要该工程假设的原生路线候选。')
            from batch_process_quote import apply_process_costing
            prepared = copy.deepcopy(result)
            apply_process_costing(prepared, documents, route_assumption=adopted_route)
        new_id, now = 'RUN-' + uuid.uuid4().hex, stamp()
        prefix = owner.API_PREFIX + '/' + batch_id + '/runs/' + new_id + '/'
        provenance = {'source_run_id': run_id, 'source_quote_digest': binding['source_digest'],
                      'source_review_revision': review['revision'], 'source_recognition_id': recognition.get('id'),
                      'source_recognition_digest': binding['recognition_digest'], 'pricing_event_id': event}
        prepared.update(run_id=new_id, created_at=now, status='pending_review', simulated=True,
                        links={key: prefix + name for key, name in (('quote_url','quote.html'), ('bom_url','bom.csv'),
                               ('cost_url','cost.csv'), ('json_url','quote.json'))})
        prepared['process_estimate'] = {'version': VERSION, 'scope_label': SCOPE_LABEL,
            'geometry_digest': digest(geometry), 'geometry_binding': geometry,
            'measurements_digest': digest(documents), 'measurements': documents,
            'source_quote': {'run_id': run_id, 'digest': binding['source_digest'],
                'estimated_total_weights_kg': {line['id']: line.get('estimated_total_net_weight_kg') for line in result['lines']}},
            'review_required': True, 'manufacturing_release': False, **provenance}
        if adopted_route:
            prepared['process_estimate']['route_assumption'] = {'id': ROUTE_ID, 'version': ROUTE_RATES['version'],
                'label': '钣金成形＋拼焊工程假设方案', 'source': 'operator_adoption',
                'adopted_at': now, 'event_id': event, 'source_quote_digest': binding['source_digest'],
                'source_geometry_digest': digest(geometry)}
        from batch_engineering_native import enrich_native
        prepared['engineering_native'] = enrich_native(prepared, {row['id']: row['geometry'] for row in documents})
        identity = digest(prepared)
        new_review = {'schema_version': 1, 'run_id': new_id, 'source_digest': identity, 'revision': 0,
                      'header': copy.deepcopy(review['header']), 'lines': copy.deepcopy(review['lines']),
                      'saved_at': now, 'status': 'pending_review', 'quotation_unchanged': True,
                      '_events': {}, 'history': [], 'parent': provenance}
        new_recognition = {'id': 'ENG-' + uuid.uuid4().hex, 'run_id': new_id, 'source_digest': identity,
            'status': 'completed', 'stage': 'completed', 'started_at': None, 'updated_at': now,
            'message': '沿用现有原生拆件与工程资料，工程量暂估报价待审核。',
            'native': copy.deepcopy(prepared['engineering_native']), 'ai': copy.deepcopy(recognition.get('ai')),
            'error': recognition.get('error'), 'partial': bool(recognition.get('partial')), '_events': [], 'parent': provenance}
        with owner._lock(batch_id):
            current = owner._read(batch_id)
            # Replays may race between cache reads and acquiring the commit lock.
            previous = next(((path, row) for path, row in owner._pricing_records(batch_id) if row['event_id'] == event), None)
            if previous:
                path, record = previous
                demand(record.get('kind') == 'process_estimate' and record.get('signature') == request_signature,
                       '相同估算请求的内容已变化。', 'event_conflict', 409)
                record = owner._commit_engineering_pricing(current, path, record)
                return {'batch': owner._public(current), 'run_id': record['run_id'], 'source_run_id': run_id, 'replayed': True}
            owner._no_pending_pricing(batch_id)
            demand(current['revision'] == state['revision'] and current['status'] not in BUSY
                   and (current.get('result') or {}).get('run_id') == run_id
                   and digest(current['result']) == binding['source_digest'],
                   '原报价已更新，请重新读取估算方案。', 'revision_conflict', 409)
            _, _, live_recognition, live_binding, _ = owner._engineering_pricing_context(current, run_id)
            demand(live_binding == binding and live_recognition['status'] not in BUSY,
                   '工程资料已更新，请重新读取估算方案。', 'source_conflict', 409)
            demand(len(owner._pricing_records(batch_id)) < 100 and len(current['runs']) < 100,
                   '本任务核价历史过多，请建立新任务。')
            record = {'schema_version': 1, 'kind': 'process_estimate', 'batch_id': batch_id,
                'event_id': event, 'signature': request_signature, 'status': 'prepared',
                'run_id': new_id, 'source_run_id': run_id, 'base_revision': current['revision'],
                'source_binding': binding, 'geometry_binding': geometry, 'created_at': now,
                'result_digest': identity, 'result': prepared, 'review': new_review, 'recognition': new_recognition}
            self.validate_prepared(current, record)
            content = _json(record)
            demand(len(content.encode()) <= MAX_PRICING_BYTES, '本次工程量估算记录过大。')
            path = owner._pricing_directory(batch_id, create=True) / (hashlib.sha256(event.encode()).hexdigest() + '.json')
            demand(not path.exists() and not path.is_symlink(), '核价请求编号已存在。', 'event_conflict', 409)
            atomic_write(path, content)
            owner._commit_engineering_pricing(current, path, record)
            return {'batch': owner._public(current), 'run_id': new_id, 'source_run_id': run_id, 'replayed': False}
