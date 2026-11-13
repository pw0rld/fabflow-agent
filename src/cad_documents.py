"""Persist original STEP documents and browser-derived assembly snapshots.

This store deliberately does not create quotation records, manufacturing parts,
or verified engineering parameters. A snapshot validates the interchange
contract and its source hash, not the engineering meaning of the CAD geometry.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from prep_record import atomic_write, only_keys, require, stamp
from workbench_store import WorkbenchError


MAX_STEP_BYTES = 20 * 1024 * 1024
MAX_NODES = 20000
MAX_MESHES = 10000
MAX_VERTICES = 2000000
MAX_TRIANGLES = 1000000
DOCUMENT_ID = re.compile(r'CAD-[0-9a-f]{32}\Z')
NODE_ID = re.compile(r'n-0(?:-[0-9]+){0,64}\Z')
EMPTY_VIEW = {'selected_node_id': None, 'isolated_node_id': None, 'hidden_node_ids': []}


def bounded_text(value, maximum, label, *, empty=False):
    require(isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum,
            f'{label}无效或过长。')
    require(not any(ord(c) < 32 and c not in '\t\n\r' for c in value), f'{label}含无效字符。')
    return value


def count(value, maximum, label, *, positive=False):
    require(type(value) is int and (1 if positive else 0) <= value <= maximum,
            f'{label}无效或超出范围。')
    return value


def validate_snapshot(snapshot, source_sha256):
    only_keys(snapshot, {'schema_version', 'source_sha256', 'importer', 'root_id',
                         'nodes', 'meshes', 'stats', 'hierarchy_origin', 'warnings'}, 'CAD snapshot')
    require(type(snapshot.get('schema_version')) is int and snapshot['schema_version'] == 1,
            '不支持此 CAD 结构版本。')
    require(snapshot.get('source_sha256') == source_sha256, 'CAD 解析结果与原文件不匹配。')
    importer = snapshot.get('importer')
    only_keys(importer, {'name', 'version', 'output_unit'}, 'CAD importer')
    require(importer == {'name': 'occt-import-js', 'version': '0.0.23', 'output_unit': 'mm'},
            'CAD 解析组件或输出单位不受支持。')
    require(snapshot.get('hierarchy_origin', 'source') in {'source', 'generated'},
            'CAD 层级来源无效。')
    warnings = snapshot.get('warnings', [])
    require(isinstance(warnings, list) and len(warnings) <= 100, 'CAD 提示过多。')
    for warning in warnings:
        bounded_text(warning, 1000, 'CAD 提示')
    meshes, nodes, stats = snapshot.get('meshes'), snapshot.get('nodes'), snapshot.get('stats')
    require(isinstance(meshes, list) and 0 < len(meshes) <= MAX_MESHES, 'CAD 显示网格数量无效。')
    require(isinstance(nodes, list) and 0 < len(nodes) <= MAX_NODES, 'CAD 部件节点数量无效。')
    only_keys(stats, {'mesh_count', 'triangle_count', 'vertex_count'}, 'CAD stats')
    vertex_total = triangle_total = 0
    for index, mesh in enumerate(meshes):
        only_keys(mesh, {'id', 'index', 'name', 'vertex_count', 'triangle_count'}, 'CAD mesh')
        require(mesh.get('id') == f'm-{index}' and type(mesh.get('index')) is int and mesh['index'] == index,
                'CAD 网格索引不连续。')
        bounded_text(mesh.get('name'), 512, 'CAD 网格名称', empty=True)
        vertex_total += count(mesh.get('vertex_count'), MAX_VERTICES, 'CAD 顶点数', positive=True)
        triangle_total += count(mesh.get('triangle_count'), MAX_TRIANGLES, 'CAD 三角形数', positive=True)
    require(vertex_total <= MAX_VERTICES and triangle_total <= MAX_TRIANGLES, 'CAD 模型超出显示范围。')
    require(type(stats.get('mesh_count')) is int and stats['mesh_count'] == len(meshes) and
            type(stats.get('vertex_count')) is int and stats['vertex_count'] == vertex_total and
            type(stats.get('triangle_count')) is int and stats['triangle_count'] == triangle_total,
            'CAD 网格统计不一致。')
    by_id = {}
    referenced = set()
    for node in nodes:
        only_keys(node, {'id', 'parent_id', 'name', 'mesh_indices', 'children'}, 'CAD node')
        node_id = node.get('id')
        require(isinstance(node_id, str) and NODE_ID.fullmatch(node_id) and node_id not in by_id,
                'CAD 部件编号无效或重复。')
        bounded_text(node.get('name'), 512, 'CAD 部件名称', empty=True)
        indices, children = node.get('mesh_indices'), node.get('children')
        require(isinstance(indices, list) and len(indices) <= len(meshes), 'CAD 部件网格无效。')
        require(all(type(i) is int and 0 <= i < len(meshes) for i in indices) and
                len(set(indices)) == len(indices), 'CAD 部件网格引用无效。')
        require(isinstance(children, list) and len(children) <= MAX_NODES and
                all(isinstance(child, str) for child in children) and len(set(children)) == len(children),
                'CAD 子部件引用无效。')
        parent_id = node.get('parent_id')
        require(parent_id is None or isinstance(parent_id, str), 'CAD 父部件引用无效。')
        by_id[node_id] = node
        referenced.update(indices)
    root_id = snapshot.get('root_id')
    require(root_id == 'n-0' and root_id in by_id and by_id[root_id]['parent_id'] is None,
            'CAD 根部件无效。')
    visited, stack = set(), [root_id]
    while stack:
        node_id = stack.pop()
        require(node_id not in visited, 'CAD 层级存在重复归属或循环。')
        visited.add(node_id)
        node = by_id[node_id]
        for index, child_id in enumerate(node['children']):
            require(child_id in by_id and by_id[child_id]['parent_id'] == node_id,
                    'CAD 父子部件引用不一致。')
            require(child_id == f'{node_id}-{index}', 'CAD 部件路径编号不一致。')
            stack.append(child_id)
    require(visited == set(by_id), 'CAD 存在未归属的部件节点。')
    require(referenced == set(range(len(meshes))), 'CAD 存在未归属的显示网格。')
    return copy.deepcopy(snapshot)


def validate_view(view, snapshot):
    only_keys(view, set(EMPTY_VIEW), 'CAD view')
    require(set(view) == set(EMPTY_VIEW), 'CAD 查看状态缺少字段。')
    ids = {node['id'] for node in snapshot['nodes']} if snapshot else set()
    for field in ('selected_node_id', 'isolated_node_id'):
        value = view[field]
        require(value is None or (isinstance(value, str) and value in ids), 'CAD 查看部件不存在。')
    hidden = view['hidden_node_ids']
    require(isinstance(hidden, list) and len(hidden) <= len(ids) and
            all(isinstance(item, str) and item in ids for item in hidden) and len(set(hidden)) == len(hidden),
            'CAD 隐藏部件不存在或重复。')
    return copy.deepcopy(view)


class CadDocumentStore:
    def __init__(self, root):
        self.root = Path(root)
        require(not self.root.is_symlink(), 'CAD 存储目录无效。')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _folder(self, document_id):
        require(isinstance(document_id, str) and DOCUMENT_ID.fullmatch(document_id), 'CAD 文件编号无效。')
        folder = self.root / document_id
        require(not folder.is_symlink(), 'CAD 文件目录无效。')
        return folder

    @contextmanager
    def _lock(self, document_id):
        self._folder(document_id)
        fd = os.open(self.root / (document_id + '.lock'),
                     os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def _read(self, document_id):
        path = self._folder(document_id) / 'document.json'
        require(not path.is_symlink(), 'CAD 文件记录无效。')
        document = json.loads(path.read_text(encoding='utf-8'))
        require(document.get('id') == document_id, 'CAD 文件记录与编号不一致。')
        return document

    def _save(self, document):
        atomic_write(self._folder(document['id']) / 'document.json',
                     json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + '\n')

    def _original(self, document):
        path = self._folder(document['id']) / 'original.step'
        require(not path.is_symlink() and path.stat().st_size <= MAX_STEP_BYTES, 'CAD 原文件无效。')
        content = path.read_bytes()
        require(len(content) == document['size'] and hashlib.sha256(content).hexdigest() == document['sha256'],
                'CAD 原文件校验失败，请重新上传原件。')
        return content

    def _public(self, document):
        result = copy.deepcopy(document)
        result['file_url'] = '/workbench/api/cad/documents/' + document['id'] + '/file'
        result['processing_status'] = 'parsed' if document['snapshot'] else 'uploaded'
        result['engineering_status'] = 'pending_review'
        result['snapshot_kind'] = 'browser_derived'
        return result

    def create(self, name, content):
        bounded_text(name, 240, 'CAD 文件名')
        require(name == Path(name).name and '/' not in name and '\\' not in name and
                not any(ord(c) < 32 for c in name), 'CAD 文件名无效。')
        require(Path(name).suffix.lower() in {'.step', '.stp'}, '当前请上传 STEP 或 STP 总装文件。')
        require(isinstance(content, bytes) and 0 < len(content) <= MAX_STEP_BYTES,
                'STEP 文件为空或超过 20 MB。')
        signature = content[:512].decode('utf-8', errors='ignore').lstrip('\ufeff')
        require(re.match(r'\s*ISO-10303-21\s*;', signature, re.I), '文件不是可识别的 STEP 格式。')
        sha256 = hashlib.sha256(content).hexdigest()
        document_id = 'CAD-' + sha256[:32]
        with self._lock(document_id):
            folder = self._folder(document_id)
            folder.mkdir(mode=0o700, exist_ok=True)
            if (folder / 'document.json').exists():
                document = self._read(document_id)
                require(document['sha256'] == sha256, 'CAD 文件编号冲突。')
                self._original(document)
                return self._public(document)
            original = folder / 'original.step'
            require(not original.is_symlink(), 'CAD 原文件路径无效。')
            if original.exists():
                require(original.read_bytes() == content, '已有 CAD 原文件与本次上传不一致。')
            else:
                fd, temporary = tempfile.mkstemp(prefix='.upload-', dir=folder)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, original)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            now = stamp()
            document = {'id': document_id, 'name': name, 'sha256': sha256, 'size': len(content),
                        'created_at': now, 'updated_at': now, 'revision': 0,
                        'snapshot': None, 'view': copy.deepcopy(EMPTY_VIEW)}
            self._save(document)
            return self._public(document)

    def get(self, document_id):
        with self._lock(document_id):
            document = self._read(document_id)
            self._original(document)
            return self._public(document)

    def list(self):
        summaries = []
        for folder in self.root.iterdir():
            if not DOCUMENT_ID.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
                continue
            if not (folder / 'document.json').exists():
                continue
            with self._lock(folder.name):
                document = self._read(folder.name)
                summary = self._public(document)
                snapshot = summary.pop('snapshot')
                summary['stats'] = snapshot['stats'] if snapshot else None
                summary['node_count'] = len(snapshot['nodes']) if snapshot else 0
                summaries.append(summary)
        return sorted(summaries, key=lambda document: (document['updated_at'], document['id']), reverse=True)

    def file(self, document_id):
        with self._lock(document_id):
            document = self._read(document_id)
            return self._original(document), document['name']

    def update(self, document_id, kind, payload):
        require(kind in {'snapshot', 'view'}, 'CAD 保存操作无效。')
        only_keys(payload, {'expected_revision', kind}, 'CAD update')
        require(set(payload) == {'expected_revision', kind}, 'CAD 保存内容缺少字段。')
        require(type(payload['expected_revision']) is int and payload['expected_revision'] >= 0,
                '请重新载入 CAD 文件版本。')
        with self._lock(document_id):
            document = self._read(document_id)
            self._original(document)
            if payload['expected_revision'] != document['revision']:
                raise WorkbenchError('CAD 文件已更新，请重新打开后再保存。', 'revision_conflict', 409)
            if kind == 'snapshot':
                value = validate_snapshot(payload[kind], document['sha256'])
                # Re-reading the same source with a changed structure must be
                # explicit; keep the old immutable snapshot until a new import.
                require(document['snapshot'] is None or document['snapshot'] == value,
                        '该原件已有不同的解析快照，请保留现有结果并重新导入。')
            else:
                value = validate_view(payload[kind], document['snapshot'])
            if document[kind] != value:
                document[kind] = value
                document['revision'] += 1
                document['updated_at'] = stamp()
                self._save(document)
            return self._public(document)
