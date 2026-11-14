"""Native solid evidence and explicitly reviewed manufacturing groups."""
from __future__ import annotations

import copy
from collections import OrderedDict
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

from prep_record import atomic_write, only_keys, require, stamp
from workbench_store import WorkbenchError
from cad_bom import (group_material_summary, group_quantity_summary, solid_identity,
                     total_quantity, validate_bom_settings, validate_material)
from cad_laser_contract import validate_laser_profile
from cad_bend_contract import validate_bend_profile
from cad_quote_quantities import validate_quote_quantities

ROOT = Path(__file__).resolve().parents[1]
SOLID_ID = re.compile(r'S[0-9]{4}\Z')
GROUP_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z')
RUN_ID = re.compile(r'R[0-9a-f]{32}\Z')
FACE_ID = re.compile(r'F[0-9]{4,7}\Z')
CLASSES = {'make', 'buy', 'assembly', 'needs_design', 'unclassified', 'exclude'}
GROUP_FIELDS = {'id', 'name', 'classification', 'solid_ids', 'status',
                'quantity_per_set', 'material', 'thickness_mm', 'notes'}
STAGES = {'queued': '等待原生 CAD 读取', 'geometry': '正在读取真实实体与几何',
          'thickness': '正在量测 CAD 板厚',
          'properties': '正在读取 CAD 材料与装配实例',
          'laser': '正在生成平板轮廓与孔位草稿',
          'bends': '正在识别真实折弯与相切边界',
          'ai': '正在整理 AI 拆件建议', 'ready': '候选已保存，待工程确认',
          'failed': '本轮处理未完成', 'not_started': '尚未开始制造件识别'}
MAX_SOLIDS = 200
MAX_GEOMETRY_BYTES = 96 * 1024 * 1024
PREVIEW_INDEX_CACHE_SIZE = 16
NATIVE_DIAGNOSTIC_CHARS = 8192


def finite(value, label, *, positive=False):
    require(type(value) in {int, float} and math.isfinite(value) and
            (not positive or value > 0), label + '无效。')
    return value


def text(value, limit, label, empty=True):
    require(isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip())),
            label + '无效或过长。')
    return value


def validate_thickness(value, face_count):
    """Validate measured evidence independently of adopted manufacturing data."""
    fields = {'status', 'value_mm', 'method', 'confidence', 'reason', 'evidence', 'warnings'}
    require(isinstance(value, dict) and set(value) == fields, '板厚量测字段无效。')
    require(isinstance(value['status'], str) and value['status'] in
            {'candidate', 'unresolved', 'not_applicable'}, '板厚量测状态无效。')
    method = value['method']
    require(isinstance(method, str) and method in
            {'parallel_faces', 'coaxial_cylinders', 'combined', 'none'}, '板厚量测方法无效。')
    require(isinstance(value['confidence'], str) and value['confidence'] in
            {'high', 'medium', 'none'}, '板厚证据等级无效。')
    text(value['reason'], 2000, '板厚量测说明', empty=False)
    warnings = value['warnings']
    require(isinstance(warnings, list) and len(warnings) <= 32, '板厚量测提示过多。')
    for warning in warnings:
        text(warning, 1000, '板厚量测提示', empty=False)
    evidence = value['evidence']
    require(isinstance(evidence, list) and len(evidence) <= 128, '板厚量测证据过多。')
    kinds = set()
    for item in evidence:
        require(isinstance(item, dict) and set(item) ==
                {'kind', 'face_ids', 'thickness_mm', 'support_area_mm2'}, '板厚面证据字段无效。')
        require(isinstance(item['kind'], str) and item['kind'] in
                {'parallel_faces', 'coaxial_cylinders'}, '板厚面证据方法无效。')
        kinds.add(item['kind'])
        ids = item['face_ids']
        require(isinstance(ids, list) and len(ids) == 2 and
                all(isinstance(face, str) and FACE_ID.fullmatch(face) and
                    1 <= int(face[1:]) <= face_count for face in ids) and
                len(set(ids)) == 2, '板厚面证据编号无效。')
        finite(item['thickness_mm'], '面间板厚', positive=True)
        require(item['thickness_mm'] <= 10000, '面间板厚超出范围。')
        finite(item['support_area_mm2'], '板厚证据面积', positive=True)
    if method == 'none':
        require(not evidence, '未量测状态不能附带面间证据。')
    elif method == 'combined':
        require(kinds == {'parallel_faces', 'coaxial_cylinders'}, '组合测厚证据不完整。')
    else:
        require(kinds == {method}, '板厚方法与面证据不一致。')
    if value['status'] == 'candidate':
        finite(value['value_mm'], '候选板厚', positive=True)
        require(value['value_mm'] <= 10000 and evidence and method != 'none' and
                value['confidence'] != 'none', '候选板厚缺少有效量测依据。')
        tolerance = max(.001, value['value_mm'] * .001)
        require(all(abs(item['thickness_mm'] - value['value_mm']) <= tolerance for item in evidence),
                '候选板厚与量测证据不一致。')
    else:
        require(value['value_mm'] is None, '未确定板厚不能保存候选数值。')
        require(value['confidence'] == 'none', '未确定板厚不能声明候选证据等级。')
    return value


def group_thickness_summary(group, measurements):
    """Aggregate only a complete, agreeing set of native-solid candidates."""
    ids = group['solid_ids']
    found = [measurements.get(sid) for sid in ids]
    details = []
    for sid, measured in zip(ids, found):
        if measured is not None:
            evidence = ['/'.join(item['face_ids']) + ': ' + format(item['thickness_mm'], '.8g') + ' mm'
                        for item in measured['evidence'][:8]]
            if len(measured['evidence']) > 8:
                evidence.append('另有 ' + str(len(measured['evidence']) - 8) + ' 组面证据')
            details.append(sid + ': ' + measured['reason'] +
                           (' [' + '; '.join(evidence) + ']' if evidence else ''))
    result = {'value_mm': None, 'status': 'not_measured', 'reason': '全部实体尚未量测。',
              'evidence_text': '; '.join(details)}
    if all(item is None for item in found):
        return result
    if all(item is not None and item['status'] == 'not_applicable' for item in found):
        return {**result, 'status': 'not_applicable', 'reason': '该分组没有适用的板厚候选。'}
    values = [item['value_mm'] for item in found if item is not None and item['status'] == 'candidate'
              and type(item['value_mm']) in {int, float} and math.isfinite(item['value_mm'])
              and item['value_mm'] > 0]
    if len(values) >= 2 and max(values) - min(values) > max(.001, max(values) * .001):
        return {**result, 'status': 'conflict', 'reason': '分组内实体的候选板厚不一致，需分别核对。'}
    if len(values) != len(found):
        return {**result, 'status': 'unresolved', 'reason': '分组内存在未量测、板厚未确定或未得到适用候选的实体。'}
    return {**result, 'status': 'candidate', 'value_mm': round(sum(values) / len(values), 8),
            'reason': '分组内所有实体的候选板厚一致，仍待人工核对。'}


def validate_export_validation(solid, *, source_sha256=None):
    """A rejected derivative cannot be downloaded as a validated STEP."""
    if 'export_validation' not in solid:
        return  # Original fixtures and earlier saved geometry have no receipt.
    value = solid['export_validation']
    require(isinstance(value, dict), '单件 STEP 校验记录无效。')
    status = value.get('status')
    require(isinstance(status, str) and status in {'engineering_draft', 'rejected'},
            '单件 STEP 校验状态无效。')
    fields = {'solid_count', 'volume_relative_delta', 'max_bound_delta_mm',
              'volume_relative_limit', 'max_bound_delta_limit_mm', 'status'}
    extra={'extra_provenance'} if 'extra_provenance' in value and status=='engineering_draft' else set()
    require(set(value) == (fields | {'reason'} if status == 'rejected' else fields | extra),
            '单件 STEP 校验字段不完整或不受支持。')
    if extra:
        proof=value['extra_provenance']
        require(isinstance(proof,dict) and set(proof)=={'method','geometry_rebuilt','source_sha256',
                'source_model_entities','subset_entities','checked_candidates','source_root_entity','source_brep_entity'},
                'STEP 保真导出来源记录无效。')
        require(proof['method']=='source_step_entity_subset' and proof['geometry_rebuilt'] is False,
                'STEP 保真导出不能声明重建了原始几何。')
        require(isinstance(proof['source_sha256'],str) and re.fullmatch(r'[0-9a-f]{64}',proof['source_sha256'])
                and (source_sha256 is None or proof['source_sha256']==source_sha256),'STEP 保真导出原件身份不一致。')
        require(type(proof['source_model_entities']) is int and 0<proof['source_model_entities']<=250000
                and type(proof['subset_entities']) is int and 0<proof['subset_entities']<=proof['source_model_entities']
                and type(proof['checked_candidates']) is int and 0<proof['checked_candidates']<=8,
                'STEP 保真导出实体或候选范围无效。')
        require(all(type(proof[key]) is int and 0<proof[key]<=1000000000
                for key in ('source_root_entity','source_brep_entity'))
                and proof['source_root_entity']!=proof['source_brep_entity'],'STEP 保真导出产品根或实体编号无效。')
    for key in ('volume_relative_limit', 'max_bound_delta_limit_mm'):
        require(type(value[key]) in {int, float} and value[key] == 1e-5,
                '单件 STEP 校验阈值与固定标准不一致。')
    count = value['solid_count']
    require(count is None or type(count) is int and 0 <= count <= 1000000,
            '单件 STEP 校验实体数无效。')
    for key in ('volume_relative_delta', 'max_bound_delta_mm'):
        measured = value[key]
        if measured is not None:
            finite(measured, '单件 STEP 校验偏差')
            require(measured >= 0, '单件 STEP 校验偏差不能为负。')
    if status == 'engineering_draft':
        require(solid.get('step_file') == solid['id'] + '.step' and count == 1,
                '已校验的单件 STEP 文件或实体数不一致。')
        require(value['volume_relative_delta'] is not None and
                value['max_bound_delta_mm'] is not None and
                value['volume_relative_delta'] <= 1e-5 and
                value['max_bound_delta_mm'] <= 1e-5,
                '单件 STEP 校验未达到固定几何阈值。')
    else:
        require(solid.get('step_file') is None,
                '校验失败的单件 STEP 不能提供下载文件。')
        text(value['reason'], 600, '单件 STEP 拒绝原因', empty=False)
        require(bool(value['reason'].strip()), '单件 STEP 拒绝原因不能为空。')


def validate_geometry(value, source_sha):
    require(isinstance(value, dict) and value.get('schema_version') == 1,
            '原生几何版本不受支持。')
    require(value.get('source_sha256') == source_sha, '原生几何与上传原件不一致。')
    require(value.get('output_unit') == 'mm', '原生几何输出单位不明确。')
    solids = value.get('solids')
    require(isinstance(solids, list) and 0 < len(solids) <= MAX_SOLIDS, '原生实体数量超出范围。')
    seen, triangles, vertices, bend_vertices, bend_triangles = set(), 0, 0, 0, 0
    for solid in solids:
        require(isinstance(solid, dict), '原生实体记录无效。')
        sid = solid.get('id')
        require(isinstance(sid, str) and SOLID_ID.fullmatch(sid) and sid not in seen,
                '原生实体编号无效或重复。')
        seen.add(sid)
        text(solid.get('name', ''), 512, '实体名称')
        solid_identity(solid)
        bounds = solid.get('bounds_mm')
        require(isinstance(bounds, dict), '实体包络无效。')
        for axis in ('min', 'max', 'size'):
            values = bounds.get(axis)
            require(isinstance(values, list) and len(values) == 3, '实体包络坐标无效。')
            for item in values:
                finite(item, '实体包络坐标')
        for lo, hi, size in zip(bounds['min'], bounds['max'], bounds['size']):
            require(lo <= hi and size >= 0 and abs((hi - lo) - size) <= max(1e-6, abs(size) * 1e-6),
                    '实体包络尺寸不一致。')
        for key in ('area_mm2', 'volume_mm3'):
            finite(solid.get(key), key)
            require(solid[key] >= 0, '实体面积或体积不能为负。')
        topology = solid.get('topology')
        require(isinstance(topology, dict), '实体拓扑信息缺失。')
        for key in ('faces', 'edges', 'vertices'):
            require(type(topology.get(key)) is int and 0 <= topology[key] <= 1000000,
                    '实体拓扑计数无效。')
        require(type(topology.get('valid')) is bool and type(topology.get('closed')) is bool,
                '实体几何有效性状态缺失。')
        if 'thickness' in solid:
            validate_thickness(solid['thickness'], topology['faces'])
        if 'material' in solid:
            validate_material(solid['material'])
        if 'quote_quantities' in solid:
            validate_quote_quantities(solid['quote_quantities'],sid,source_sha,topology['faces'],
                                      area_mm2=solid['area_mm2'],volume_mm3=solid['volume_mm3'])
        if 'laser_cut' in solid:
            validate_laser_profile(solid['laser_cut'], sid, topology['faces'],
                                   solid_bounds=bounds, volume_mm3=solid['volume_mm3'])
        if 'bend_features' in solid:
            validate_bend_profile(solid['bend_features'], topology['faces'], solid_bounds=bounds)
            if solid['bend_features']['status'] == 'candidate':
                require(topology['valid'] and topology['closed'], '无效或未封闭的实体不能提供折弯候选。')
            for bend in solid['bend_features']['bends']:
                bend_vertices += len(bend['mesh']['positions'])//3
                bend_triangles += len(bend['mesh']['indices'])//3
        mesh = solid.get('mesh')
        require(isinstance(mesh, dict), '实体显示几何缺失。')
        positions, indices = mesh.get('positions'), mesh.get('indices')
        require(isinstance(positions, list) and positions and len(positions) % 3 == 0 and
                len(positions) <= 6000000, '实体顶点数据无效。')
        require(all(type(x) in {int, float} and math.isfinite(x) and abs(x) <= 1e9 for x in positions),
                '实体顶点坐标无效。')
        vertex_count = len(positions) // 3
        require(isinstance(indices, list) and indices and len(indices) % 3 == 0 and
                len(indices) <= 3000000 and all(type(x) is int and 0 <= x < vertex_count for x in indices),
                '实体三角索引无效。')
        vertices += vertex_count
        triangles += len(indices) // 3
        for key, suffix in (('thumbnail', '.png'), ('step_file', '.step')):
            filename = solid.get(key)
            require(filename is None or filename == sid + suffix, '原生几何产物路径无效。')
        validate_export_validation(solid, source_sha256=source_sha)
    require(vertices <= 2000000 and triangles <= 1000000, '原生几何超出显示预算。')
    require(bend_vertices <= 500000 and bend_triangles <= 1000000, '折弯曲带超出显示预算。')
    return value


def validate_groups(groups, solid_ids):
    require(isinstance(groups, list) and 0 < len(groups) <= MAX_SOLIDS, '请保留完整的制造件候选。')
    seen_groups, seen_solids, normalized = set(), set(), []
    for row in groups:
        only_keys(row, GROUP_FIELDS, 'manufacturing group')
        require(set(row) == GROUP_FIELDS, '制造件候选缺少字段。')
        gid = row['id']
        require(isinstance(gid, str) and GROUP_ID.fullmatch(gid) and gid not in seen_groups,
                '制造件编号无效或重复。')
        seen_groups.add(gid)
        text(row['name'], 200, '制造件名称', empty=False)
        require(row['classification'] in CLASSES, '制造件分类无效。')
        require(row['status'] in {'pending_review', 'confirmed'}, '制造件确认状态无效。')
        ids = row['solid_ids']
        require(isinstance(ids, list) and ids and all(isinstance(x, str) and x in solid_ids for x in ids),
                '制造件必须关联当前原件的真实实体。')
        require(len(ids) == len(set(ids)) and not seen_solids.intersection(ids),
                '同一实体不能重复分配给多个制造件。')
        seen_solids.update(ids)
        quantity = row['quantity_per_set']
        require(quantity is None or type(quantity) is int and 0 < quantity <= 1000000,
                '每套用量必须为正整数或待确认。')
        material = row['material']
        require(material is None or isinstance(material, str) and 0 < len(material.strip()) <= 512,
                '材料必须为明确文本或待确认。')
        thickness = row['thickness_mm']
        if thickness is not None:
            finite(thickness, '板厚', positive=True)
            require(thickness <= 10000, '板厚超出范围。')
        text(row['notes'], 2000, '制造件说明')
        normalized.append(copy.deepcopy(row))
    require(seen_solids == set(solid_ids), '存在尚未分组的原生实体，请保留或明确排除。')
    return normalized


def fallback_groups(geometry):
    return [{'id': f'P{index:04d}', 'name': solid['name'] or '实体 ' + solid['id'],
             'classification': 'unclassified', 'solid_ids': [solid['id']], 'status': 'pending_review',
             'quantity_per_set': None, 'material': None, 'thickness_mm': None,
             'notes': '由原生 CAD 独立实体建立候选；制造属性、用量与拆分方案待工程确认。'}
            for index, solid in enumerate(geometry['solids'], 1)]


class CadManufacturingService:
    _active = set()
    _active_lock = threading.Lock()
    _native_slots = threading.BoundedSemaphore(1)

    def __init__(self, cad_store, *, native_python=None, analyzer=None, native_runner=None):
        self.cad = cad_store
        self.native_python = native_python or os.environ.get('HYL_CAD_PYTHON') or sys.executable
        self.analyzer = analyzer
        self.native_runner = native_runner or self._extract
        # Keep only source-bound scalar lookup facts here, never display meshes.
        self._preview_indexes = OrderedDict()
        self._preview_index_lock = threading.Lock()
        self._recover()

    def _folder(self, document_id, create=False):
        folder = self.cad._folder(document_id) / 'manufacturing'
        require(not folder.is_symlink(), '制造件存储目录无效。')
        if create:
            folder.mkdir(mode=0o700, exist_ok=True)
        return folder

    def _key(self, document_id):
        return str(self._folder(document_id).absolute())

    def _default(self, document_id, sha):
        return {'schema_version': 1, 'document_id': document_id, 'source_sha256': sha,
                'status': 'not_started', 'stage': 'not_started', 'revision': 0,
                'job_id': None, 'updated_at': None, 'error': None, 'warnings': [],
                'groups': [], 'geometry': None, 'bom_settings': {'order_sets': None},
                'ai': {'status': 'not_requested', 'provenance': None}}

    def _read(self, document_id, sha):
        path = self._folder(document_id) / 'state.json'
        if not path.exists():
            return self._default(document_id, sha)
        require(not path.is_symlink(), '制造件记录无效。')
        state = json.loads(path.read_text())
        require(state['source_sha256'] == sha, '制造件记录与原件不一致。')
        state.setdefault('bom_settings', {'order_sets': None})
        validate_bom_settings(state['bom_settings'])
        return state

    def _save(self, state):
        folder = self._folder(state['document_id'], create=True)
        state['updated_at'] = stamp()
        atomic_write(folder / 'state.json', json.dumps(state, ensure_ascii=False, allow_nan=False, indent=2) + '\n')

    def _recover(self):
        for folder in self.cad.root.iterdir():
            path = folder / 'manufacturing/state.json'
            if folder.is_symlink() or not path.is_file() or path.is_symlink():
                continue
            document_id = folder.name
            with self._active_lock:
                if self._key(document_id) in self._active:
                    continue
            with self.cad._lock(document_id):
                document = self.cad._read(document_id)
                state = self._read(document_id, document['sha256'])
                if state['status'] in {'queued', 'running'}:
                    state.update(status='failed', stage='failed', error='服务中断，本轮未完成；原件和已有结果保留，可重试。')
                    state['revision'] += 1
                    self._save(state)

    def _public(self, state):
        result = {key: copy.deepcopy(state[key]) for key in
                  ('document_id', 'source_sha256', 'status', 'stage', 'revision', 'job_id',
                   'updated_at', 'error', 'warnings', 'groups', 'ai')}
        prefix = '/workbench/api/cad/documents/' + state['document_id'] + '/manufacturing'
        info = state['geometry']
        result.update(stage_label=STAGES[state['stage']], geometry_url=prefix + '/geometry' if info else None,
                      files_base_url=prefix + '/files/' if info else None,
                      bom_url=prefix + '/bom.csv' if state['groups'] else None,
                      geometry_run_id=info['run_id'] if info else None,
                      geometry_summary=copy.deepcopy(info['summary']) if info else None,
                      thickness_measurements=copy.deepcopy(info.get('thickness_measurements', {})) if info else {},
                      material_measurements=copy.deepcopy(info.get('material_measurements', {})) if info else {},
                      laser_profiles=copy.deepcopy(info.get('laser_profiles', {})) if info else {},
                      bend_profiles=copy.deepcopy(info.get('bend_profiles', {})) if info else {},
                      bom_settings=copy.deepcopy(state.get('bom_settings', {'order_sets': None})))
        identities = info.get('solid_identities', {}) if info else {}
        result['quantity_candidates'] = {group['id']: group_quantity_summary(group, identities)
                                         for group in state['groups']}
        result['review_scope'] = '制造件分组与属性草稿；不是生产放行或工艺验证。'
        from cad_engineering_review import project as project_engineering_review
        result['engineering_review'] = project_engineering_review(result)
        return result

    def get(self, document_id):
        with self.cad._lock(document_id):
            document = self.cad._read(document_id)
            self.cad._original(document)
            return self._public(self._read(document_id, document['sha256']))

    def _revision(self, state, payload):
        value = payload.get('expected_revision')
        if value is not None:
            require(type(value) is int and value >= 0, '制造件修订号无效。')
            if value != state['revision']:
                raise WorkbenchError('制造件记录已更新，请重新载入后保存。', 'revision_conflict', 409)

    def start(self, document_id, payload):
        only_keys(payload, {'expected_revision', 'retry'}, 'manufacturing analyze')
        require(type(payload.get('retry', False)) is bool, '重试标记无效。')
        with self.cad._lock(document_id):
            document = self.cad._read(document_id)
            self.cad._original(document)
            state = self._read(document_id, document['sha256'])
            self._revision(state, payload)
            if state['status'] in {'queued', 'running'} or state['status'] == 'ready' and not payload.get('retry'):
                return self._public(state)
            require(not any(group['status'] == 'confirmed' for group in state['groups']),
                    '已有工程确认结果，请保留本次分组，不自动覆盖重跑。')
            job_id = 'R' + uuid.uuid4().hex
            state.update(status='queued', stage='queued', job_id=job_id, error=None,
                         ai={'status': 'not_requested', 'provenance': None})
            state['revision'] += 1
            self._save(state)
            with self._active_lock:
                self._active.add(self._key(document_id))
            threading.Thread(target=self._run, args=(document_id, job_id), daemon=True).start()
            return self._public(state)

    def measure_thickness(self, document_id, payload):
        return self._start_enrichment(document_id, payload, 'thickness')

    def read_properties(self, document_id, payload):
        return self._start_enrichment(document_id, payload, 'properties')

    def generate_laser(self, document_id, payload):
        return self._start_enrichment(document_id, payload, 'laser')

    def identify_bends(self, document_id, payload):
        return self._start_enrichment(document_id, payload, 'bends')

    def _start_enrichment(self, document_id, payload, stage):
        only_keys(payload, {'expected_revision'}, 'manufacturing ' + stage)
        require(set(payload) == {'expected_revision'} and
                type(payload['expected_revision']) is int and payload['expected_revision'] >= 0,
                '补充物料属性需要有效的清单修订号。')
        with self.cad._lock(document_id):
            document = self.cad._read(document_id)
            self.cad._original(document)
            state = self._read(document_id, document['sha256'])
            self._revision(state, payload)
            if state['status'] in {'queued', 'running'}:
                return self._public(state)
            require(state['geometry'] is not None and state['groups'], '请先读取 CAD 实体，再补充物料属性。')
            before = validate_geometry(json.loads(self._artifact_for_state(state, 'geometry.json')),
                                       document['sha256'])
            baseline = self._identity(before)
            if stage == 'bends':
                baseline = {'identity': baseline, 'geometry': before,
                            'info': copy.deepcopy(state['geometry'])}
            job_id = 'R' + uuid.uuid4().hex
            state.update(status='queued', stage=stage, job_id=job_id, error=None)
            state['revision'] += 1
            self._save(state)
            with self._active_lock:
                self._active.add(self._key(document_id))
            threading.Thread(target=self._run_enrichment,
                             args=(document_id, job_id, baseline, stage), daemon=True).start()
            return self._public(state)

    def _update(self, document_id, job_id, **changes):
        with self.cad._lock(document_id):
            document = self.cad._read(document_id)
            state = self._read(document_id, document['sha256'])
            require(state['job_id'] == job_id, '制造件任务已被替换。')
            state.update(changes)
            state['revision'] += 1
            self._save(state)
            return state

    def _extract(self, source, output):
        command = [self.native_python, str(ROOT / 'src/cad_geometry.py'), str(source), '--output', str(output)]
        started = time.monotonic()
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=180,
                                    env={**os.environ, 'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'})
        except subprocess.TimeoutExpired as error:
            self._native_diagnostic(output, None, error.stdout, error.stderr,
                                    elapsed=time.monotonic() - started, timed_out=True)
            raise WorkbenchError('原生 CAD 读取超时，未保存为成功结果。') from error
        except OSError as error:
            self._native_diagnostic(output, None, '', str(error),
                                    elapsed=time.monotonic() - started, launch_failed=True)
            raise WorkbenchError('原生 CAD 引擎无法启动，请检查 HYL_CAD_PYTHON 配置及执行权限。') from error
        self._native_diagnostic(output, result.returncode, result.stdout, result.stderr,
                                elapsed=time.monotonic() - started)
        if result.returncode:
            message = self._native_error(result.returncode, result.stdout, result.stderr)
            raise WorkbenchError(message)

    @staticmethod
    def _native_text(value):
        # TimeoutExpired may contain bytes even when subprocess text=True.
        if isinstance(value, bytes):
            value = value.decode('utf-8', errors='replace')
        return value if isinstance(value, str) else ''

    @classmethod
    def _native_error(cls, returncode, stdout, stderr):
        stderr, stdout = cls._native_text(stderr), cls._native_text(stdout)
        # The native worker writes its structured GeometryError to stderr.
        # Prefer that receipt while retaining compatibility with old stdout.
        for stream in (stderr, stdout):
            for line in stream[-NATIVE_DIAGNOSTIC_CHARS:].splitlines()[::-1]:
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if isinstance(data, dict) and isinstance(data.get('error'), str):
                    message = ' '.join(data['error'].split())[:600]
                    if message:
                        return message
        if re.search(r'(?m)^(?:ModuleNotFoundError|ImportError):\s+\S', stderr):
            return '原生 CAD 运行时依赖不可用，请检查 HYL_CAD_PYTHON 配置及依赖。'
        if returncode < 0:
            number = -returncode
            try:
                name = signal.Signals(number).name
            except ValueError:
                name = 'UNKNOWN'
            return f'原生 CAD 进程异常退出（{name}，信号 {number}），未生成完整结果。'
        return f'原生 CAD 读取未完成（退出码 {returncode}）；请检查 CAD 引擎和 STEP 文件。'

    @classmethod
    def _native_diagnostic(cls, output, returncode, stdout, stderr, *, elapsed,
                           timed_out=False, launch_failed=False):
        """Private, bounded receipt; never registered as a geometry artifact."""
        try:
            folder = Path(output)
            if folder.is_symlink() or not folder.is_dir():
                return
            streams = {key: cls._native_text(value) for key, value in
                       (('stdout', stdout), ('stderr', stderr))}
            receipt = {'recorded_at': stamp(), 'returncode': returncode,
                       'timed_out': timed_out, 'launch_failed': launch_failed,
                       'elapsed_seconds': round(elapsed, 3),
                       'stream_tail_limit_chars': NATIVE_DIAGNOSTIC_CHARS,
                       **{key: value[-NATIVE_DIAGNOSTIC_CHARS:] for key, value in streams.items()},
                       'truncated': {key: len(value) > NATIVE_DIAGNOSTIC_CHARS
                                     for key, value in streams.items()}}
            atomic_write(folder / 'native-diagnostic.json',
                         json.dumps(receipt, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
        except Exception:
            # Storage diagnostics must not replace the worker's original error.
            pass

    def _registered(self, output, geometry):
        names = {'geometry.json'}
        for solid in geometry['solids']:
            names.update(solid[key] for key in ('thumbnail', 'step_file') if solid.get(key))
            profile = solid.get('laser_cut')
            if profile and profile['status'] == 'ready':
                names.add(profile['dxf_file'])
        for name in geometry.get('contact_sheets', []):
            require(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.-]+\.png', name), '几何总览文件名无效。')
            names.add(name)
        files = {}
        for name in names:
            path = output / name
            require(path.is_file() and not path.is_symlink(), '原生几何产物缺失。')
            maximum = MAX_GEOMETRY_BYTES if name == 'geometry.json' else 20 * 1024 * 1024
            require(path.stat().st_size <= maximum, '原生几何产物超出范围。')
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return files

    @staticmethod
    def _identity(geometry):
        return {solid['id']: (solid.get('definition_id'), solid.get('occurrence_path'))
                for solid in geometry['solids']}

    def _native_geometry(self, document_id, job_id, stage):
        # Serialize kernel work on the small host. AI waiting does not hold this slot.
        with self._native_slots:
            self._update(document_id, job_id, status='running', stage=stage)
            content, _ = self.cad.file(document_id)
            folder = self._folder(document_id) / 'runs'
            require(not folder.is_symlink(), '制造件运行目录无效。')
            folder.mkdir(mode=0o700, exist_ok=True)
            output = folder / job_id
            output.mkdir(mode=0o700)
            source = output / 'input.step'
            source.write_bytes(content)
            self.native_runner(source, output)
            path = output / 'geometry.json'
            require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_GEOMETRY_BYTES,
                    '原生几何结果缺失或过大。')
            geometry = validate_geometry(json.loads(path.read_text()), hashlib.sha256(content).hexdigest())
            files = self._registered(output, geometry)
            measurements = {solid['id']: copy.deepcopy(solid['thickness']) for solid in geometry['solids']
                            if 'thickness' in solid}
            materials = {solid['id']: copy.deepcopy(solid['material']) for solid in geometry['solids']
                         if 'material' in solid}
            lasers = {solid['id']: copy.deepcopy(solid['laser_cut']) for solid in geometry['solids']
                      if 'laser_cut' in solid}
            bends = {solid['id']: copy.deepcopy(solid['bend_features']) for solid in geometry['solids']
                     if 'bend_features' in solid}
            identities = {solid['id']: solid_identity(solid) for solid in geometry['solids']}
            info = {'run_id': job_id, 'files': files, 'thickness_measurements': measurements,
                    'material_measurements': materials, 'solid_identities': identities, 'laser_profiles': lasers,
                    'bend_profiles': bends,
                    'summary': {'solid_count': len(geometry['solids']), 'engine': geometry.get('engine'),
                        'output_unit': geometry['output_unit'], 'source_units': geometry.get('source_units'),
                        'valid_count': sum(s['topology']['valid'] for s in geometry['solids']),
                        'closed_count': sum(s['topology']['closed'] for s in geometry['solids'])}}
        return geometry, files, info, output

    def _merge_bend_geometry(self, document_id, baseline, geometry):
        """Add bend evidence while preserving every other validated old artifact."""
        before = baseline['geometry']
        require(self._identity(geometry) == baseline['identity'],
                '更新折弯时实体对应关系发生变化，已保留原分组和几何。')
        old_solids = {solid['id']: solid for solid in before['solids']}
        for solid in geometry['solids']:
            old = old_solids[solid['id']]
            require(solid['topology'] == old['topology'], '更新折弯时实体拓扑发生变化，已保留原结果。')
            for key in ('area_mm2', 'volume_mm3'):
                require(abs(solid[key]-old[key]) <= max(1e-6, abs(old[key])*1e-8),
                        '更新折弯时实体面积或体积发生变化，已保留原结果。')
            require(all(abs(a-b) <= max(1e-6, abs(b)*1e-8)
                        for key in ('min', 'max', 'size')
                        for a, b in zip(solid['bounds_mm'][key], old['bounds_mm'][key])),
                    '更新折弯时实体包络发生变化，已保留原结果。')
        merged = copy.deepcopy(before)
        additions = {solid['id']: solid['bend_features'] for solid in geometry['solids']}
        for solid in merged['solids']:
            solid['bend_features'] = copy.deepcopy(additions[solid['id']])
        validate_geometry(merged, before['source_sha256'])
        old_info = baseline['info']
        prior_state = {'document_id': document_id, 'geometry': old_info}
        old_folder = self._folder(document_id) / 'runs' / old_info['run_id']
        require(not old_folder.is_symlink() and not old_folder.parent.is_symlink(), '原有几何文件目录无效。')
        require(self._registered(old_folder, before) == old_info['files'],
                '原有几何产物注册或哈希发生变化，已保留原结果。')
        run_id = 'R' + uuid.uuid4().hex
        output = self._folder(document_id) / 'runs' / run_id
        output.mkdir(mode=0o700)
        for filename in old_info['files']:
            if filename != 'geometry.json':
                # The old registration is exactly the native whitelist above.
                # Read through the normal path, size and hash checks; input.step
                # and any unregistered native-worker products never enter here.
                content = self._artifact_for_state(prior_state, filename)
                (output / filename).write_bytes(content)
        atomic_write(output / 'geometry.json', json.dumps(merged, ensure_ascii=False,
                     allow_nan=False, separators=(',', ':')) + '\n')
        files = self._registered(output, merged)
        require(all(files[name] == digest for name, digest in old_info['files'].items()
                    if name != 'geometry.json'), '保留的几何产物字节发生变化。')
        info = copy.deepcopy(old_info)
        info.update(run_id=run_id, files=files,
                    bend_profiles={sid: copy.deepcopy(profile) for sid, profile in additions.items()})
        return merged, files, info, output

    def _run_enrichment(self, document_id, job_id, baseline, stage):
        try:
            geometry, files, info, output = self._native_geometry(document_id, job_id, stage)
            identity = baseline['identity'] if stage == 'bends' else baseline
            require(self._identity(geometry) == identity,
                    '更新几何派生资料时实体对应关系发生变化，已保留原分组和几何，请人工检查。')
            if stage != 'bends':
                require(len(info['thickness_measurements']) == len(geometry['solids']),
                        'CAD 引擎没有返回完整板厚量测状态，已保留原结果。')
            if stage == 'properties':
                require(len(info['material_measurements']) == len(geometry['solids']),
                        'CAD 引擎没有返回完整材料读取状态，已保留原结果。')
            if stage == 'laser':
                require(len(info['laser_profiles']) == len(geometry['solids']),
                        'CAD 引擎没有返回完整平板切割图状态，已保留原结果。')
            if stage == 'bends':
                require(len(info['bend_profiles']) == len(geometry['solids']),
                        'CAD 引擎没有返回完整折弯识别状态，已保留原结果。')
            require(files == self._registered(output, geometry), '原生几何结果在量测期间发生变化。')
            if stage == 'bends':
                geometry, files, info, output = self._merge_bend_geometry(document_id, baseline, geometry)
            with self.cad._lock(document_id):
                document = self.cad._read(document_id)
                state = self._read(document_id, document['sha256'])
                require(state['job_id'] == job_id, '制造件任务已被替换。')
                if stage == 'bends':
                    require(state['geometry'] == baseline['info'], '原有几何结果已更新，请重新识别折弯。')
                require(files == self._registered(output, geometry), '原生几何结果在保存期间发生变化。')
                validate_groups(state['groups'], {solid['id'] for solid in geometry['solids']})
                warnings = list(dict.fromkeys(state['warnings'] +
                                [str(w)[:2000] for w in geometry.get('warnings', [])[:100]]))[:200]
                state.update(status='ready', stage='ready', geometry=info, warnings=warnings, error=None)
                state['revision'] += 1
                self._save(state)
        except Exception as error:
            self._fail(document_id, job_id, error)
        finally:
            with self._active_lock:
                self._active.discard(self._key(document_id))

    def _fail(self, document_id, job_id, error):
        message = str(error) if isinstance(error, (WorkbenchError, ValueError)) else '原生 CAD 处理未完成，请检查引擎配置后重试。'
        try:
            self._update(document_id, job_id, status='failed', stage='failed', error=message[:1000])
        except Exception:
            pass

    def _run(self, document_id, job_id):
        try:
            geometry, files, info, output = self._native_geometry(document_id, job_id, 'geometry')
            warnings = [str(w)[:2000] for w in geometry.get('warnings', [])[:100]]
            groups = fallback_groups(geometry)
            self._update(document_id, job_id, geometry=info, groups=groups, warnings=warnings,
                         stage='ai' if self.analyzer else 'geometry',
                         ai={'status': 'running' if self.analyzer else 'not_requested', 'provenance': None})
            ai = {'status': 'not_requested', 'provenance': None}
            if self.analyzer:
                try:
                    result = self.analyzer(output, geometry)
                    proposed = copy.deepcopy(result['groups'])
                    for row in proposed:
                        row.update(status='pending_review', quantity_per_set=None, material=None, thickness_mm=None)
                    groups = validate_groups(proposed, {s['id'] for s in geometry['solids']})
                    warnings.extend(str(w)[:2000] for w in result.get('warnings', [])[:100])
                    provenance = result.get('provenance', {})
                    provenance = {key: provenance[key] for key in ('kind', 'status', 'session_id', 'model_label', 'finished_at') if key in provenance}
                    ai = {'status': 'completed', 'provenance': provenance}
                except Exception as error:
                    warnings.append('AI 拆件建议本轮未完成或未通过校验；已保留真实几何候选，请人工分类。')
                    ai = {'status': 'unavailable', 'provenance': None, 'error_kind': type(error).__name__}
            else:
                warnings.append('本环境尚未启用 AI 拆件建议，当前清单仅来自原生 CAD 实体。')
            require(files == self._registered(output, geometry), '原生几何结果在分析期间发生变化。')
            self._update(document_id, job_id, status='ready', stage='ready', groups=groups,
                         warnings=warnings[:200], ai=ai, error=None)
        except Exception as error:
            self._fail(document_id, job_id, error)
        finally:
            with self._active_lock:
                self._active.discard(self._key(document_id))

    def _artifact(self, document_id, filename, *, expected_geometry=None):
        with self.cad._lock(document_id):
            document = self.cad._read(document_id)
            self.cad._original(document)
            state = self._read(document_id, document['sha256'])
            if expected_geometry is not None:
                info = state.get('geometry') or {}
                require(isinstance(expected_geometry, dict) and
                        set(expected_geometry) == {'run_id', 'geometry_sha256'} and
                        info.get('run_id') == expected_geometry['run_id'] and
                        info.get('files', {}).get('geometry.json') == expected_geometry['geometry_sha256'],
                        '原生几何已更新，请重新载入零件预览。')
            return self._artifact_for_state(state, filename)

    def _artifact_path_for_state(self, state, filename):
        info = state.get('geometry')
        require(info is not None, '尚无原生几何结果。')
        require(filename in info['files'] and Path(filename).name == filename,
                '该文件不属于当前几何结果。')
        require(RUN_ID.fullmatch(info['run_id']), '几何运行编号无效。')
        folder = self._folder(state['document_id']) / 'runs' / info['run_id']
        require(not folder.is_symlink() and not folder.parent.is_symlink(), '几何文件目录无效。')
        path = folder / filename
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_GEOMETRY_BYTES,
                '几何文件无效或过大。')
        return path

    def _artifact_for_state(self, state, filename):
        path = self._artifact_path_for_state(state, filename)
        data = path.read_bytes()
        require(hashlib.sha256(data).hexdigest() == state['geometry']['files'][filename], '几何文件校验失败。')
        return data

    @staticmethod
    def _preview_index_key(state):
        info = state['geometry']
        return (state['document_id'], state['source_sha256'], info['run_id'], info['files']['geometry.json'])

    def _remember_preview_index(self, state, geometry):
        """Called only for fully validated geometry while holding the CAD lock."""
        index = {'source_sha256': geometry['source_sha256'], 'output_unit': geometry['output_unit'],
                 'preview_binding': {'run_id': state['geometry']['run_id'],
                                     'geometry_sha256': state['geometry']['files']['geometry.json']},
                 'solids': [{key: copy.deepcopy(solid[key]) for key in
                             ('id', 'name', 'definition_id', 'occurrence_path', 'volume_mm3', 'bounds_mm', 'thumbnail')
                             if key in solid} for solid in geometry['solids']]}
        key = self._preview_index_key(state)
        with self._preview_index_lock:
            self._preview_indexes[key] = index
            self._preview_indexes.move_to_end(key)
            while len(self._preview_indexes) > PREVIEW_INDEX_CACHE_SIZE:
                self._preview_indexes.popitem(last=False)
        return index

    def preview_index(self, document_id):
        """Read a verified small lookup index without reparsing meshes per image.

        The existing per-document lock also makes concurrent cold lookups share
        one parse. Every hit still hashes the registered geometry from disk in
        bounded chunks; a changed artifact cannot reuse a previously valid index.
        """
        with self.cad._lock(document_id):
            document = self.cad._read(document_id)
            self.cad._original(document)
            state = self._read(document_id, document['sha256'])
            require(state['document_id'] == document_id, '制造件记录与文件编号不一致。')
            path = self._artifact_path_for_state(state, 'geometry.json')
            key = self._preview_index_key(state)
            with self._preview_index_lock:
                index = self._preview_indexes.get(key)
                if index is not None:
                    self._preview_indexes.move_to_end(key)
            if index is None:
                data = self._artifact_for_state(state, 'geometry.json')
                geometry = validate_geometry(json.loads(data), document['sha256'])
                index = self._remember_preview_index(state, geometry)
            else:
                digest, length = hashlib.sha256(), 0
                with path.open('rb') as stream:
                    while True:
                        chunk = stream.read(1024 * 1024)
                        if not chunk:
                            break
                        length += len(chunk)
                        require(length <= MAX_GEOMETRY_BYTES, '几何文件无效或过大。')
                        digest.update(chunk)
                require(digest.hexdigest() == key[-1], '几何文件校验失败。')
            # Callers may adapt their local projection without corrupting hits.
            return copy.deepcopy(index)

    def geometry(self, document_id):
        with self.cad._lock(document_id):
            document = self.cad._read(document_id)
            self.cad._original(document)
            state = self._read(document_id, document['sha256'])
            geometry = validate_geometry(json.loads(self._artifact_for_state(state, 'geometry.json')),
                                         document['sha256'])
            self._remember_preview_index(state, geometry)
            return geometry

    def file(self, document_id, filename, *, expected_geometry=None):
        require(bool(re.fullmatch(r'[A-Za-z0-9_.-]+\.(png|step|dxf)', filename)),
                '仅支持已登记的几何图片、STEP 或 DXF。')
        mime = ('image/png' if filename.endswith('.png') else
                'application/dxf' if filename.endswith('.dxf') else 'application/octet-stream')
        return self._artifact(document_id, filename, expected_geometry=expected_geometry), mime

    def review(self, document_id, payload):
        only_keys(payload, {'expected_revision', 'groups', 'bom_settings'}, 'manufacturing review')
        require({'expected_revision', 'groups'} <= set(payload), '保存分组需要修订号和完整候选清单。')
        require(type(payload['expected_revision']) is int and payload['expected_revision'] >= 0,
                '保存分组需要有效的非负整数修订号。')
        geometry = self.geometry(document_id)
        groups = validate_groups(payload['groups'], {solid['id'] for solid in geometry['solids']})
        settings = validate_bom_settings(payload['bom_settings']) if 'bom_settings' in payload else None
        with self.cad._lock(document_id):
            document = self.cad._read(document_id)
            self.cad._original(document)
            state = self._read(document_id, document['sha256'])
            self._revision(state, payload)
            require(state['status'] == 'ready', '本轮分析尚未完成，请稍候再确认分组。')
            before_settings = state.get('bom_settings', {'order_sets': None})
            after_settings = before_settings if settings is None else settings
            if groups == state['groups'] and before_settings == after_settings:
                return self._public(state)
            revision = state['revision'] + 1
            audit = {'revision': revision, 'at': stamp(), 'actor': '当前工作台操作者',
                     'scope': '制造件分组与属性草稿，不代表工艺或生产放行。',
                     'source_sha256': state['source_sha256'], 'geometry_run_id': state['geometry']['run_id'],
                     'before': state['groups'], 'after': groups,
                     'before_bom_settings': before_settings, 'after_bom_settings': after_settings}
            audit_path = self._folder(document_id) / f'review-{revision:08d}.json'
            require(not audit_path.exists() and not audit_path.is_symlink(), '分组修订记录已存在。')
            atomic_write(audit_path, json.dumps(audit, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
            state.update(groups=groups, bom_settings=copy.deepcopy(after_settings), revision=revision)
            self._save(state)
            return self._public(state)

    def bom(self, document_id):
        state = self.get(document_id)
        require(state['groups'], '尚无制造件清单可导出。')
        geometry = self.geometry(document_id)
        validate_groups(state['groups'], {solid['id'] for solid in geometry['solids']})
        stream = io.StringIO(newline='')
        writer = csv.writer(stream)
        writer.writerow(['记录类型', '原件SHA256', '清单修订', '编号', '名称', '分类', '分组状态', '每套用量', '数量状态', '材料', '板厚(mm)', '关联原生实体', '说明',
                         '测厚候选(mm)', '测厚状态', '测量依据',
                         '材料候选原文', '材料候选状态', '材料来源', '每套用量候选', '用量候选状态', '用量依据',
                         '物料表套数', '已采用总件数', '候选总件数'])
        labels = {'make': '自制件', 'buy': '外购件', 'assembly': '装配体', 'needs_design': '待工程设计',
                  'unclassified': '待分类', 'exclude': '排除'}
        def cell(value):
            value = '' if value is None else str(value)
            return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) else value
        for group in state['groups']:
            if group['classification'] == 'exclude':
                continue
            thickness = group_thickness_summary(group, state['thickness_measurements'])
            thickness_labels = {'candidate': '候选待核对', 'conflict': '存在冲突', 'unresolved': '未确定',
                                'not_applicable': '未得到适用候选', 'not_measured': '尚未量测'}
            material = group_material_summary(group, state['material_measurements'])
            quantity = state['quantity_candidates'][group['id']]
            candidate_labels = {'candidate': '候选待核对', 'conflict': '存在冲突', 'unresolved': '未确定',
                                'not_applicable': '不适用', 'not_read': '尚未读取'}
            order_sets = state['bom_settings']['order_sets']
            writer.writerow([cell(value) for value in [
                '物料图表草稿', state['source_sha256'], state['revision'], group['id'], group['name'], labels[group['classification']],
                '分组已确认' if group['status'] == 'confirmed' else '分组待确认',
                group['quantity_per_set'], '待确认' if group['quantity_per_set'] is None else '人工录入',
                group['material'], group['thickness_mm'], ';'.join(group['solid_ids']), group['notes'],
                thickness['value_mm'], thickness_labels[thickness['status']], thickness['evidence_text'],
                material['value'], candidate_labels[material['status']], material['evidence_text'],
                quantity['value'], candidate_labels[quantity['status']], quantity['reason'] +
                (' [' + '; '.join(quantity['occurrence_ids']) + ']' if quantity['occurrence_ids'] else ''),
                order_sets, total_quantity(group['quantity_per_set'], order_sets),
                total_quantity(quantity['value'], order_sets)]])
        return ('\ufeff' + stream.getvalue()).encode('utf-8')
