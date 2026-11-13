"""Clean presentation thumbnails from immutable, already tessellated CAD meshes.

This is a display-only projection: it neither opens STEP files nor changes the
mesh, manufacturing parameters, source thumbnails, manifests, or quote totals.
Run the CLI on ZeroClave in the isolated build directory, then publish only the
new showcase-thumbs directory. Pillow is already part of the CAD runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import time
from urllib.parse import urlsplit


LINE_ID = re.compile(r'L[0-9]{4,7}\Z')
FILE_ID = re.compile(r'F[0-9]{4}\Z')
SOLID_ID = re.compile(r'S[0-9]{4,7}\Z')
MAX_JSON = 32 * 1024 * 1024
MAX_TRIANGLES = 250000
MAX_VERTICES = 1000000
RENDER_VERSION = 'native-mesh-showcase-v1'


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _read(path):
    _require(path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_JSON,
             'Saved presentation input is missing, linked or too large.')
    raw = path.read_bytes()
    def reject(value):
        raise ValueError('Nonfinite saved mesh value.')
    value = json.loads(raw, parse_constant=reject)
    _require(isinstance(value, dict), 'Saved presentation input must be an object.')
    return value, hashlib.sha256(raw).hexdigest()


def _safe_existing(root, relative):
    _require(isinstance(relative, str) and relative and '\\' not in relative,
             'Invalid saved presentation path.')
    parts = relative.split('/')
    _require(all(part not in {'', '.', '..'} and ':' not in part for part in parts),
             'Invalid saved presentation path.')
    path = root
    for part in parts:
        path = path / part
        _require(not path.is_symlink(), 'Presentation inputs must not use symlinks.')
    return path


def _geometry_name(part):
    url = part.get('shared_geometry_url')
    _require(isinstance(url, str), 'Part has no saved shared geometry.')
    parsed = urlsplit(url)
    _require(not parsed.scheme and not parsed.netloc and not parsed.query and not parsed.fragment,
             'Part geometry must reference a local saved source.')
    components = PurePosixPath(parsed.path).parts
    _require(len(components) >= 2 and components[-2] == 'geometry'
             and components[-1].endswith('.json') and FILE_ID.fullmatch(components[-1][:-5]),
             'Part shared geometry path is invalid.')
    return 'geometry/' + components[-1]


def _representatives(part, geometry):
    _require(geometry.get('output_unit') == 'mm' and geometry.get('schema_version') == 1,
             'Presentation requires the saved millimetre mesh schema.')
    _require(part.get('source_sha256') == geometry.get('source_sha256')
             and isinstance(part.get('source_sha256'), str) and len(part['source_sha256']) == 64,
             'Part and geometry source identities differ.')
    identities = part.get('representative_solid_ids')
    if identities is None:
        identities = [part.get('representative_solid_id')]
    _require(isinstance(identities, list) and 0 < len(identities) <= 200
             and all(isinstance(value, str) and SOLID_ID.fullmatch(value) for value in identities)
             and len(set(identities)) == len(identities), 'Part representative solid identity is invalid.')
    solids = geometry.get('solids')
    _require(isinstance(solids, list), 'Saved geometry has no solids.')
    selected = [solid for solid in solids if solid.get('id') in identities]
    _require(len(selected) == len(identities) and {solid['id'] for solid in selected} == set(identities),
             'Saved representative solid is missing or ambiguous.')
    return selected


def _project(point):
    x, y, z = point
    return ((x - y) / math.sqrt(2), (x + y - 2 * z) / math.sqrt(6),
            (x + y + z) / math.sqrt(3))


def render_thumbnail(part, geometry, *, width=960, height=720, supersample=2,
                     transparent=False):
    """Return a high resolution PNG of exactly the saved representative meshes.

    Assembly translations and relationships stay in their saved coordinates;
    the sole transform is an orthographic camera and uniform fit-to-frame scale.
    Native mesh triangles preserve the available silhouette and hole geometry.
    """
    from PIL import Image, ImageDraw

    _require(type(width) is int and type(height) is int and 160 <= width <= 1920
             and 120 <= height <= 1440 and supersample in {1, 2, 3}, 'Invalid thumbnail dimensions.')
    solids = _representatives(part, geometry)
    vertices, projected, triangles = [], [], []
    # Edges are keyed by physical coordinates within each solid. Native meshes
    # duplicate vertices between CAD faces, so index-only keys create seams.
    edges = {}
    for solid in solids:
        mesh = solid.get('mesh')
        _require(isinstance(mesh, dict), 'Saved representative has no display mesh.')
        positions, indices = mesh.get('positions'), mesh.get('indices')
        _require(isinstance(positions, list) and positions and len(positions) % 3 == 0
                 and isinstance(indices, list) and indices and len(indices) % 3 == 0,
                 'Saved mesh coordinates or triangles are invalid.')
        _require(len(vertices) + len(positions) // 3 <= MAX_VERTICES
                 and len(triangles) + len(indices) // 3 <= MAX_TRIANGLES,
                 'Saved display mesh exceeds thumbnail resource limits.')
        _require(all(type(value) in {int, float} and math.isfinite(value) for value in positions),
                 'Saved mesh has nonfinite coordinates.')
        offset = len(vertices)
        current = [tuple(positions[index:index + 3]) for index in range(0, len(positions), 3)]
        vertices.extend(current)
        projected.extend(_project(point) for point in current)
        keys = [tuple(round(axis, 6) for axis in point) for point in current]
        _require(all(type(index) is int and 0 <= index < len(current) for index in indices),
                 'Saved mesh has invalid triangle indices.')
        for index in range(0, len(indices), 3):
            raw = indices[index:index + 3]
            tri = tuple(offset + value for value in raw)
            a, b, c = (vertices[value] for value in tri)
            u, v = (tuple(b[axis] - a[axis] for axis in range(3)),
                    tuple(c[axis] - a[axis] for axis in range(3)))
            normal = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
                      u[0] * v[1] - u[1] * v[0])
            length = math.sqrt(sum(value * value for value in normal))
            if length <= 1e-14:
                continue
            normal = tuple(value / length for value in normal)
            edge_keys = []
            for first, second in ((raw[0], raw[1]), (raw[1], raw[2]), (raw[2], raw[0])):
                edge = (solid['id'], *sorted((keys[first], keys[second])))
                edge_keys.append(edge)
                edges.setdefault(edge, []).append(normal)
            triangles.append((sum(projected[value][2] for value in tri) / 3, tri, normal, edge_keys))
    _require(vertices and triangles, 'Saved mesh has no drawable triangles.')
    min_x, max_x = min(point[0] for point in projected), max(point[0] for point in projected)
    min_y, max_y = min(point[1] for point in projected), max(point[1] for point in projected)
    scale = min(width * .88 / max(max_x - min_x, 1e-9),
                height * .86 / max(max_y - min_y, 1e-9)) * supersample
    center_x, center_y = (min_x + max_x) / 2, (min_y + max_y) / 2
    screen = [(width * supersample / 2 + (point[0] - center_x) * scale,
               height * supersample / 2 + (point[1] - center_y) * scale) for point in projected]
    # A crease, silhouette or boundary edge is useful. Interior tessellation
    # diagonals are omitted so dense native meshes do not look like wireframes.
    visible_edges = set()
    direction = (1 / math.sqrt(3),) * 3
    for key, normals in edges.items():
        boundary = len(normals) == 1
        crease = any(abs(sum(first[axis] * other[axis] for axis in range(3))) < .94
                     for first in normals[:1] for other in normals[1:])
        views = [sum(normal[axis] * direction[axis] for axis in range(3)) for normal in normals]
        silhouette = min(views) < 0 < max(views)
        if boundary or crease or silhouette:
            visible_edges.add(key)
    image = Image.new('RGBA', (width * supersample, height * supersample),
                      (0, 0, 0, 0) if transparent else '#f6f9fc')
    draw = ImageDraw.Draw(image)
    light = (.309, -.505, .806)
    triangles.sort(key=lambda row: row[0])
    for _, tri, normal, edge_keys in triangles:
        shade = .58 + .38 * abs(sum(normal[axis] * light[axis] for axis in range(3)))
        color = tuple(round(channel * shade) for channel in (112, 177, 211)) + (255,)
        points = [screen[index] for index in tri]
        draw.polygon(points, fill=color)
        for index, edge in enumerate(edge_keys):
            if edge in visible_edges:
                draw.line((points[index], points[(index + 1) % 3]), fill=(49, 87, 114, 255),
                          width=max(1, round(.65 * supersample)))
    if supersample > 1:
        resized = image.resize((width, height), Image.Resampling.LANCZOS)
        image.close()
        image = resized
    import io
    stream = io.BytesIO()
    image.save(stream, format='PNG', optimize=True)
    image.close()
    return stream.getvalue()


def _cached_thumbnail(cache, key):
    if cache is None:
        return None
    image, metadata = cache / (key + '.png'), cache / (key + '.json')
    if not image.exists() and not metadata.exists():
        return None
    _require(image.is_file() and not image.is_symlink() and image.stat().st_size <= 40 * 1024 * 1024,
             'Presentation thumbnail cache image is invalid.')
    record, _ = _read(metadata)
    content = image.read_bytes()
    _require(record.get('key') == key and content.startswith(b'\x89PNG\r\n\x1a\n')
             and record.get('sha256') == hashlib.sha256(content).hexdigest(),
             'Presentation thumbnail cache identity differs.')
    return content


def render_case_thumbnails(case_root, output_dir=None, cache_dir=None, *, line_ids=None,
                           width=960, height=720, supersample=2):
    """Write independent display assets plus a provenance and URL mapping file."""
    root = Path(case_root).resolve()
    output = Path(output_dir) if output_dir is not None else root / 'showcase-thumbs'
    cache = Path(cache_dir) if cache_dir is not None else None
    _require(root.is_dir() and not Path(case_root).is_symlink(), 'Saved case directory is invalid.')
    _require(output.name == 'showcase-thumbs' and not output.is_symlink(),
             'Presentation assets must use a separate showcase-thumbs directory.')
    output.mkdir(mode=0o755, parents=True, exist_ok=True)
    if cache is not None:
        _require(not cache.is_symlink(), 'Presentation cache must not use a symlink.')
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    manifest, manifest_sha = _read(_safe_existing(root, 'part-previews.json'))
    rows = manifest.get('lines')
    _require(isinstance(rows, list) and len(rows) <= 1000, 'Saved preview manifest is invalid.')
    selected = set(line_ids) if line_ids is not None else None
    _require(selected is None or selected and all(isinstance(value, str) and LINE_ID.fullmatch(value)
                                                 for value in selected), 'Requested line identity is invalid.')
    cached, mapping, receipts, skipped, cache_hits = {}, {}, [], [], 0
    started = time.monotonic()
    for row in rows:
        line_id = row.get('line_id')
        _require(isinstance(line_id, str) and LINE_ID.fullmatch(line_id), 'Saved line identity is invalid.')
        if selected is not None and line_id not in selected:
            continue
        if row.get('status') != 'ready':
            skipped.append({'line_id': line_id, 'reason': 'saved-preview-unavailable'})
            continue
        part, part_sha = _read(_safe_existing(root, 'parts/' + line_id + '.json'))
        _require(part.get('line_id') == line_id and part.get('batch_id') == manifest.get('batch_id')
                 and part.get('run_id') == manifest.get('run_id'), 'Saved part preview identity differs.')
        geometry_name = _geometry_name(part)
        if geometry_name not in cached:
            # Keep one source document in memory at a time for large archives.
            cached.clear()
            cached[geometry_name] = _read(_safe_existing(root, geometry_name))
        geometry, geometry_sha = cached[geometry_name]
        identities = sorted(solid['id'] for solid in _representatives(part, geometry))
        key = hashlib.sha256(json.dumps([RENDER_VERSION, part['source_sha256'], geometry_sha,
                                        identities, width, height, supersample, False],
                                       separators=(',', ':')).encode()).hexdigest()
        content = _cached_thumbnail(cache, key)
        if content is None:
            content = render_thumbnail(part, geometry, width=width, height=height, supersample=supersample)
            if cache is not None:
                (cache / (key + '.png')).write_bytes(content)
                (cache / (key + '.json')).write_text(json.dumps({'key': key,
                    'sha256': hashlib.sha256(content).hexdigest()}, separators=(',', ':')) + '\n')
        else:
            cache_hits += 1
        target = output / (line_id + '.png')
        _require(not target.is_symlink(), 'Presentation asset must not overwrite a symlink.')
        target.write_bytes(content)
        mapping[line_id] = 'showcase-thumbs/' + line_id + '.png'
        receipts.append({'line_id': line_id, 'source_sha256': part['source_sha256'],
                         'part_json_sha256': part_sha, 'geometry_json_sha256': geometry_sha,
                         'representative_solid_ids': identities, 'cache_key': key,
                         'thumbnail_sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)})
    receipt = {'schema_version': 1, 'kind': 'saved-native-mesh-presentation',
               'batch_id': manifest.get('batch_id'), 'run_id': manifest.get('run_id'),
               'source_manifest_sha256': manifest_sha, 'width': width, 'height': height,
               'camera': 'orthographic-isometric', 'supersample': supersample, 'renderer': RENDER_VERSION,
               'cache_hits': cache_hits, 'rendered': len(receipts) - cache_hits,
               'elapsed_seconds': round(time.monotonic() - started, 3), 'thumbnail_urls': mapping,
               'generated': receipts, 'skipped': skipped}
    target = output / 'manifest.json'
    _require(not target.is_symlink(), 'Presentation manifest must not overwrite a symlink.')
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return receipt


def generate_showcase_thumbnails(case_directory, output_directory, **options):
    """Compatibility alias for the first isolated preparation script."""
    return render_case_thumbnails(case_directory, output_directory, **options)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case-directory', '--case-root', dest='case_directory', type=Path, required=True)
    parser.add_argument('--output-directory', type=Path, required=True)
    parser.add_argument('--cache-directory', type=Path)
    parser.add_argument('--line-id', action='append', dest='line_ids')
    parser.add_argument('--width', type=int, default=960)
    parser.add_argument('--height', type=int, default=720)
    parser.add_argument('--supersample', type=int, default=2)
    args = parser.parse_args()
    receipt = render_case_thumbnails(args.case_directory, args.output_directory, args.cache_directory,
                                     line_ids=args.line_ids, width=args.width,
                                     height=args.height, supersample=args.supersample)
    print(json.dumps({'generated': len(receipt['generated']), 'skipped': len(receipt['skipped']),
                      'cache_hits': receipt['cache_hits'], 'rendered': receipt['rendered'],
                      'elapsed_seconds': receipt['elapsed_seconds']}, separators=(',', ':')))


if __name__ == '__main__':
    main()
