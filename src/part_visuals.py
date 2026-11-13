"""Read-only, source-bound part views; no image generation or native decoders."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import quote

from prep_record import identifier
from reading_inputs import (_confined, _read_json, _root, validate_pdf_views,
                            MAX_PNG_BYTES)

CONFIG_PATH = Path(__file__).resolve().parents[1] / 'config' / 'part-visuals.json'
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_IMAGES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}
MAX_VIEWS = 24
MAX_HOTSPOTS = 16
MAX_POINTS = 64


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value):
    return (isinstance(value, str) and 0 < len(value) <= 128
            and value == value.strip() and value.isprintable())


def _coordinate(value):
    return type(value) in {int, float} and 0 <= value <= 1000 and math.isfinite(value)


def validate_view(value, *, references=None, page_counts=None):
    """Validate only declarative page coordinates, never URLs, HTML or CAD code."""
    _require(isinstance(value, dict) and set(value) ==
             {'label', 'reference', 'page', 'bbox', 'hotspots'}, 'invalid part view shape')
    _require(_text(value['label']) and _text(value['reference']), 'invalid part view label or source')
    reference, page = value['reference'], value['page']
    _require(references is None or reference in references, 'part view outside its sources')
    _require(type(page) is int and 1 <= page <= 10000, 'invalid part view page')
    _require(page_counts is None or page <= page_counts.get(reference, 0), 'part view page outside source')
    bbox = value['bbox']
    _require(isinstance(bbox, list) and len(bbox) == 4 and all(_coordinate(x) for x in bbox),
             'invalid part view bounds')
    x, y, width, height = bbox
    _require(width > 0 and height > 0 and x + width <= 1000 and y + height <= 1000,
             'part view bounds outside page')
    hotspots = value['hotspots']
    _require(isinstance(hotspots, list) and len(hotspots) <= MAX_HOTSPOTS, 'too many part view hotspots')
    for hotspot in hotspots:
        _require(isinstance(hotspot, dict) and set(hotspot) == {'label', 'polygon'}
                 and _text(hotspot['label']), 'invalid part hotspot')
        polygon = hotspot['polygon']
        _require(isinstance(polygon, list) and 3 <= len(polygon) <= MAX_POINTS, 'invalid hotspot polygon')
        _require(all(isinstance(point, list) and len(point) == 2 and all(_coordinate(n) for n in point)
                     and x <= point[0] <= x + width and y <= point[1] <= y + height
                     for point in polygon), 'hotspot polygon outside part view')
        _require(len({tuple(point) for point in polygon}) >= 3
                 and abs(sum(a[0] * b[1] - b[0] * a[1]
                             for a, b in zip(polygon, polygon[1:] + polygon[:1]))) > 0,
                 'hotspot polygon has no area')
    return copy.deepcopy(value)


def _pdf_bundle(record, run, reader_root):
    """Verify the already committed bundle and its exact current-run identity."""
    _require(isinstance(run, dict) and run.get('id') and run.get('session_id'), 'reading page cache unavailable')
    workspace = Path(reader_root) / identifier(record['task_id']) / identifier(run['id'])
    if not workspace.exists():
        raise FileNotFoundError('reading page cache unavailable')
    for parent in (workspace, workspace.parent, Path(reader_root)):
        _require(not parent.is_symlink(), 'reading page workspace symlink prohibited')
    root, _ = _root(workspace)
    manifest_path = root / '.dsh-bridge-run.json'
    if not manifest_path.exists():
        raise FileNotFoundError('reading page cache unavailable')
    manifest = _read_json(_confined(root, manifest_path))
    _require(manifest.get('task_id') == record['task_id']
             and manifest.get('session_id') == run['session_id'], 'reading page run identity changed')
    files = manifest.get('files')
    references = set(run.get('source_ids', []))
    _require(isinstance(files, list) and {row.get('id') for row in files if isinstance(row, dict)} == references
             and len(files) == len(references), 'reading page sources changed')
    for row in files:
        source = record['sources'].get(row['id'], {})
        _require(source.get('kind') == 'drawing' and row.get('sha256') == source.get('sha256')
                 and row.get('original_name') == source.get('original_name'), 'reading page original changed')
    if not isinstance(manifest.get('pdf_views'), dict):
        raise FileNotFoundError('reading page cache unavailable')
    data = validate_pdf_views(root, files, manifest['pdf_views'])
    return root, data


def read_pdf_page(record, run, reader_root, reference, page, run_id):
    """Return an existing verified PNG. This function cannot build a cache."""
    identifier(reference)
    identifier(run_id)
    if not isinstance(run, dict) or run.get('id') != run_id:
        raise FileNotFoundError('reading run is no longer current')
    _require(type(page) is int and 1 <= page <= 10000, 'invalid reading page')
    root, data = _pdf_bundle(record, run, reader_root)
    row = next((row for row in data['views'] if row['origin_id'] == reference and row['page'] == page), None)
    if row is None:
        raise FileNotFoundError('reading page unavailable')
    path = _confined(root, row['path'])
    content = path.read_bytes()
    _require(0 < len(content) <= MAX_PNG_BYTES
             and hashlib.sha256(content).hexdigest() == row['sha256'], 'reading page cache changed')
    return content


def _sources(task_id, record, run, reader_root):
    references = set((run or {}).get('source_ids', []))
    sources = {}
    for reference in references:
        source = record['sources'].get(reference, {})
        if source.get('kind') != 'drawing':
            continue
        suffix = Path(source.get('original_name', '')).suffix.lower()
        if suffix in _IMAGES:
            sources[(reference, 1)] = {'image_url': '/workbench/api/tasks/' + quote(task_id, safe='')
                                     + '/attachments/' + quote(reference, safe=''),
                                     'source_sha256': source['sha256']}
    try:
        _, data = _pdf_bundle(record, run, reader_root)
    except (OSError, ValueError, KeyError, TypeError):
        return sources
    for row in data['views']:
        reference, page = row['origin_id'], row['page']
        sources[(reference, page)] = {
            'image_url': '/workbench/api/tasks/' + quote(task_id, safe='') + '/reading-pages/'
                         + quote(reference, safe='') + '/' + str(page) + '.png?run_id=' + quote(run['id'], safe=''),
            'source_sha256': row['original_sha256'], 'width': row['width'], 'height': row['height']}
    return sources


def load_config(path=CONFIG_PATH):
    """Optional prepared examples contain coordinates and hashes, never images."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        _require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 256 * 1024,
                 'invalid prepared part configuration')
        value = json.loads(path.read_text(encoding='utf-8'))
        _require(isinstance(value, dict) and set(value) == {'schema_version', 'drawings'}
                 and type(value['schema_version']) is int and value['schema_version'] == 1
                 and isinstance(value['drawings'], list) and len(value['drawings']) <= 100,
                 'invalid prepared part configuration')
        return value['drawings']
    except (OSError, ValueError, TypeError):
        return []


def project_part(part, sources, config=None):
    """Display located views only when their source and part association match."""
    allowed = {(row.get('reference'), row.get('page')) for row in part.get('evidence', [])
               if isinstance(row, dict)}
    available = {position: source for position, source in sources.items() if position in allowed}
    output = {'status': 'source', 'views': []}

    def append_view(raw, provenance):
        try:
            value = validate_view(raw, references={reference for reference, _ in allowed})
            position = (value['reference'], value['page'])
            if position not in available:
                return False
            source = available[position]
            output['views'].append({**value, 'id': 'view-' + str(len(output['views']) + 1),
                                    'provenance': provenance, 'image_url': source['image_url'],
                                    **{key: source[key] for key in ('width', 'height') if key in source}})
            output['status'] = 'located'
            return True
        except (ValueError, KeyError, TypeError):
            return False

    for entry in (config or [])[:100]:
        if not isinstance(entry, dict) or set(entry) - {'source_sha256', 'observation_sha256', 'views', 'schematic'}:
            continue
        digest, observed = entry.get('source_sha256'), entry.get('observation_sha256')
        if not isinstance(digest, str) or not _SHA.fullmatch(digest) or not isinstance(observed, str) or not _SHA.fullmatch(observed):
            continue
        matching = {reference for (reference, _), source in available.items()
                    if source['source_sha256'] == digest}
        matching &= {row.get('evidence', {}).get('reference') for row in part.get('observations', [])
                     if isinstance(row, dict) and isinstance(row.get('raw_text'), str)
                     and hashlib.sha256(row['raw_text'].encode('utf-8')).hexdigest() == observed}
        if len(matching) != 1 or not isinstance(entry.get('views'), list):
            continue
        reference = next(iter(matching))
        start = len(output['views'])
        for view in entry['views'][:MAX_VIEWS]:
            if isinstance(view, dict) and set(view) == {'label', 'page', 'bbox', 'hotspots'}:
                append_view({**view, 'reference': reference}, 'prepared')
        if len(output['views']) > start and entry.get('schematic') == {
                'kind': 'prepared-example', 'template': 'sealed-box-v1'}:
            output['schematic'] = copy.deepcopy(entry['schematic'])
        if output['views']:
            break
    if not output['views']:
        for view in part.get('views', [])[:MAX_VIEWS] if isinstance(part.get('views'), list) else []:
            append_view(view, 'ai')
    if not output['views']:
        for reference, page in sorted(available)[:MAX_VIEWS]:
            source = available[(reference, page)]
            output['views'].append({'id': 'view-' + str(len(output['views']) + 1),
                                    'label': '原图第 ' + str(page) + ' 页', 'reference': reference, 'page': page,
                                    'bbox': [0, 0, 1000, 1000], 'hotspots': [], 'provenance': 'source',
                                    'image_url': source['image_url'],
                                    **{key: source[key] for key in ('width', 'height') if key in source}})
    if not output['views']:
        output['reason'] = '该件暂无可用的页面图片，请打开原文件查看。'
    return output


def project_parts(task_id, record, run, parts, reader_root, *, config_path=CONFIG_PATH):
    sources = _sources(task_id, record, run, reader_root)
    config = load_config(config_path)
    for part in parts:
        part['visualization'] = project_part(part, sources, config)
    return parts
