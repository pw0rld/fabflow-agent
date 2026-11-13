"""Optional, immutable high-resolution quadrants of original drawing pages.

This is image preparation only: no OCR, geometry interpretation or model calls.
The existing full-page PDF cache is deliberately left unchanged. Native decoders
run only in a resource-bounded child; failure keeps the original overview useful.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
if sys.platform.startswith('linux') and (ROOT / 'vendor/python-linux').is_dir():
    sys.path.insert(0, str(ROOT / 'vendor/python-linux'))

from reading_inputs import (_checked_files, _confined, _copy_new, _hash,
                            _png_size, _read_json, _require, _root, _sources,
                            _write_manifest, validate_pdf_views)

BUNDLE = '.drawing-details'
MANIFEST = 'manifest.json'
SCHEMA_VERSION = 1
VERSION = 'drawing-details-1'
MAX_VIEWS = 128
MAX_PIXELS = 240_000_000
MAX_SIDE = 2400
MAX_TILE_PIXELS = 4_000_000
RENDER_TIMEOUT = 60
_SLOTS = threading.BoundedSemaphore(1)
_RASTERS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.tif', '.tiff', '.bmp'}
# Coordinates are fractions measured from the top-left of the displayed page.
# A 10%-of-page overlap retains dimensions straddling the quadrant boundary.
_QUADRANTS = (
    ('tl', '左上局部（与相邻区域重叠）', (0, 0, .55, .55)),
    ('tr', '右上局部（与相邻区域重叠）', (.45, 0, 1, .55)),
    ('bl', '左下局部（与相邻区域重叠）', (0, .45, .55, 1)),
    ('br', '右下局部（与相邻区域重叠）', (.45, .45, 1, 1)),
)


def _alias(reference, page, quadrant):
    return 'DETAIL-' + hashlib.sha256(reference.encode('utf-8')).hexdigest()[:16] + '-p' + str(page) + '-' + quadrant


def _name(source, page, region):
    return source['original_name'] + ' 第' + str(page) + '页 · ' + region + '.png'


def _pdf_counts(workspace, files, pdf_views):
    if pdf_views is None:
        return None
    return validate_pdf_views(workspace, files, pdf_views)['page_counts']


def _validate(root, files, data, *, bundle=None, expected_pdf_counts=None):
    keys = {'schema_version', 'converter', 'sources', 'page_counts', 'views', 'warnings'}
    _require(isinstance(data, dict) and set(data) == keys, '图纸局部图缓存结构无效。')
    _require(type(data['schema_version']) is int and data['schema_version'] == SCHEMA_VERSION
             and data['converter'] == VERSION, '图纸局部图缓存版本不匹配。')
    _require(data['sources'] == _sources(files), '图纸局部图缓存与原附件不匹配。')
    warnings = data['warnings']
    _require(isinstance(warnings, list) and len(warnings) <= 64
             and all(isinstance(item, str) and 0 < len(item) <= 700 for item in warnings),
             '图纸局部图告警无效。')
    sources = {item['id']: item for item in files}
    counts = data['page_counts']
    _require(isinstance(counts, dict) and all(
        reference in sources and Path(sources[reference]['path']).suffix.lower() in _RASTERS | {'.pdf'}
        and type(count) is int and 1 <= count <= 1_000_000
        for reference, count in counts.items()), '图纸局部图页码清单无效。')
    if expected_pdf_counts is not None:
        _require(all(counts.get(reference) == count for reference, count in expected_pdf_counts.items()),
                 '图纸局部图与 PDF 概览页码不一致。')
    rows = data['views']
    _require(isinstance(rows, list) and len(rows) <= MAX_VIEWS, '图纸局部图数量超过上限。')
    if not rows:
        _require(bool(warnings), '图纸局部图未生成且缺少说明。')
    else:
        _require(len(rows) == sum(counts.values()) * 4, '图纸局部图覆盖不完整。')
        _require(all(Path(item['path']).suffix.lower() != '.pdf' or item['id'] in counts for item in files),
                 '图纸局部图缺少 PDF 文件。')
    bundle = root / BUNDLE if bundle is None else bundle
    _confined(root, bundle, regular=False)
    expected = {(reference, page, code): region
                for reference, count in counts.items() for page in range(1, count + 1)
                for code, region, _ in _QUADRANTS} if rows else {}
    expected_keys = {'id', 'origin_id', 'page', 'path', 'sha256', 'original_sha256',
                     'original_name', 'region', 'width', 'height'}
    seen, aliases, pixels = set(), set(), 0
    for row in rows:
        _require(isinstance(row, dict) and set(row) == expected_keys, '图纸局部图字段无效。')
        origin, page, reference = row['origin_id'], row['page'], row['id']
        _require(isinstance(origin, str) and origin in counts and type(page) is int
                 and 1 <= page <= counts[origin] and isinstance(reference, str), '图纸局部图来源或页码无效。')
        matching = [(code, region) for code, region, _ in _QUADRANTS
                    if reference == _alias(origin, page, code)]
        _require(len(matching) == 1 and reference not in sources and reference not in aliases,
                 '图纸局部图标识无效或重复。')
        code, region = matching[0]
        pair = (origin, page, code)
        _require(pair in expected and pair not in seen and row['region'] == region,
                 '图纸局部图区域或覆盖无效。')
        _require(row['path'] == BUNDLE + '/' + reference + '.png', '图纸局部图位置无效。')
        _require(row['original_sha256'] == sources[origin]['sha256']
                 and row['original_name'] == _name(sources[origin], page, region),
                 '图纸局部图原件关联无效。')
        width, height = row['width'], row['height']
        _require(type(width) is int and type(height) is int and 0 < width <= MAX_SIDE
                 and 0 < height <= MAX_SIDE and width * height <= MAX_TILE_PIXELS,
                 '图纸局部图像素尺寸无效。')
        pixels += width * height
        _require(pixels <= MAX_PIXELS, '图纸局部图总像素超过上限。')
        path = _confined(root, bundle / (reference + '.png'))
        _require(isinstance(row['sha256'], str) and _hash(path) == row['sha256'], '图纸局部图校验失败。')
        _require(_png_size(path) == (width, height), '图纸局部图尺寸与缓存不一致。')
        seen.add(pair)
        aliases.add(reference)
    _require(seen == set(expected), '图纸局部图区域覆盖不完整。')
    return copy.deepcopy(data)


def validate_detail_views(workspace, files, data, pdf_views=None):
    """Verify original bytes and the pinned, complete detail cache without decoding."""
    root, original_root = _root(workspace)
    checked = _checked_files(root, original_root, files)
    counts = _pdf_counts(root, checked, pdf_views)
    cached = _read_json(_confined(root, root / BUNDLE / MANIFEST))
    _require(data == cached, '图纸局部图缓存与运行清单不一致。')
    return _validate(root, checked, cached, expected_pdf_counts=counts)


@contextmanager
def _lock(root):
    path = root / (BUNDLE + '.lock')
    _require(not path.is_symlink(), '图纸局部图缓存锁不允许符号链接。')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+') as stream:
        _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), '图纸局部图缓存锁无效。')
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def prepare_detail_views(workspace, files, pdf_views=None):
    """Build optional details atomically; decoder/budget failure retains overviews.

    Invalid small legacy raster fixtures are not an error here. Unsafe paths,
    changed originals and tampered existing caches always refuse rather than
    silently replacing a pinned input. A warning-only manifest records fallback.
    """
    root, original_root = _root(workspace)
    checked = _checked_files(root, original_root, files)
    if not any(Path(item['path']).suffix.lower() in _RASTERS | {'.pdf'} for item in checked):
        return None
    counts = _pdf_counts(root, checked, pdf_views)
    bundle = root / BUNDLE
    if bundle.exists() or bundle.is_symlink():
        return validate_detail_views(root, checked, _read_json(_confined(root, bundle / MANIFEST)), pdf_views)
    with _SLOTS, _lock(root):
        if bundle.exists() or bundle.is_symlink():
            return validate_detail_views(root, checked, _read_json(_confined(root, bundle / MANIFEST)), pdf_views)
        temporary = Path(tempfile.mkdtemp(prefix=BUNDLE + '-stage-', dir=root))
        try:
            manifest, output = temporary / 'input.json', temporary / 'rendered'
            _write_manifest(manifest, {'files': checked, 'source_workspace': str(root), 'pdf_counts': counts})
            failed = False
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--render',
                                         str(manifest), str(output)], capture_output=True, text=True,
                                        timeout=RENDER_TIMEOUT, cwd=Path(__file__).resolve().parent)
                rendered = json.loads(result.stdout)
                if result.returncode != 0 or not isinstance(rendered, dict) or 'error' in rendered:
                    raise ValueError('optional renderer failed')
            except (subprocess.TimeoutExpired, OSError, ValueError, UnicodeError):
                failed = True
                rendered = {'page_counts': counts or {}, 'views': [],
                            'warnings': ['局部高清图未能在资源限制内完整生成；仍可读取全部原图和 PDF 概览，细小标注可能需要补充清晰资料。']}
            _require(set(rendered) == {'page_counts', 'views', 'warnings'}, '图纸局部图转换结果无效。')
            if not failed and not rendered['page_counts'] and not rendered['views'] and not rendered['warnings'] and not any(
                    Path(item['path']).suffix.lower() == '.pdf' for item in checked):
                # Nonimages and small legacy fixtures continue through the original reader.
                return None
            staged = temporary / 'bundle'
            staged.mkdir(mode=0o700)
            data = {'schema_version': SCHEMA_VERSION, 'converter': VERSION, 'sources': _sources(checked),
                    'page_counts': rendered['page_counts'], 'views': [], 'warnings': rendered['warnings']}
            for view in rendered['views']:
                _require(isinstance(view, dict) and isinstance(view.get('id'), str), '图纸局部图转换映射无效。')
                source = _confined(root, view.get('path'))
                _require(source.is_relative_to(output), '图纸局部图转换位置无效。')
                _require(source.name == view['id'] + '.png', '图纸局部图转换文件名无效。')
                _copy_new(source, staged / source.name)
                data['views'].append({**view, 'path': BUNDLE + '/' + source.name})
            checked = _checked_files(root, original_root, files)
            _validate(root, checked, data, bundle=staged, expected_pdf_counts=counts)
            _write_manifest(staged / MANIFEST, data)
            _require(not bundle.exists() and not bundle.is_symlink(), '图纸局部图缓存位置已被占用。')
            os.rename(staged, bundle)
            return validate_detail_views(root, checked, data, pdf_views)
        finally:
            shutil.rmtree(temporary)


def copy_detail_views(source_workspace, target_workspace, source_files, target_files, data, pdf_views=None):
    """Copy the pinned initial details to a review, preserving IDs and PNG bytes."""
    if data is None:
        return None
    source_root, source_original = _root(source_workspace)
    source_checked = _checked_files(source_root, source_original, source_files)
    validate_detail_views(source_root, source_checked, data, pdf_views)
    target_root, target_original = _root(target_workspace)
    target_checked = _checked_files(target_root, target_original, target_files)
    _require(_sources(source_checked) == _sources(target_checked), '回查图纸局部图原附件与首轮不一致。')
    counts = _pdf_counts(target_root, target_checked, pdf_views)
    target_bundle = target_root / BUNDLE
    if target_bundle.exists() or target_bundle.is_symlink():
        return validate_detail_views(target_root, target_checked, data, pdf_views)
    with _SLOTS, _lock(target_root):
        if target_bundle.exists() or target_bundle.is_symlink():
            return validate_detail_views(target_root, target_checked, data, pdf_views)
        temporary = Path(tempfile.mkdtemp(prefix=BUNDLE + '-stage-', dir=target_root))
        try:
            staged = temporary / 'bundle'
            staged.mkdir(mode=0o700)
            for view in data['views']:
                _copy_new(_confined(source_root, view['path']), staged / Path(view['path']).name)
            _copy_new(_confined(source_root, source_root / BUNDLE / MANIFEST), staged / MANIFEST)
            _require(_read_json(staged / MANIFEST) == data, '首轮图纸局部图缓存已变化。')
            target_checked = _checked_files(target_root, target_original, target_files)
            _validate(target_root, target_checked, data, bundle=staged, expected_pdf_counts=counts)
            _require(not target_bundle.exists() and not target_bundle.is_symlink(), '回查图纸局部图缓存位置已被占用。')
            os.rename(staged, target_bundle)
            return validate_detail_views(target_root, target_checked, data, pdf_views)
        finally:
            shutil.rmtree(temporary)


def _bounds(width, height, fractions, *, raster=False):
    left, top, right, bottom = fractions
    if raster:
        return (math.floor(width * left), math.floor(height * top),
                math.ceil(width * right), math.ceil(height * bottom))
    return width * left, height * top, width * right, height * bottom


def _scale(width, height, cap):
    # Leave rounding headroom for PDFium's ceil-based crop-to-bitmap transform.
    return min(cap, (MAX_SIDE - 1) / max(width, height),
               math.sqrt((MAX_TILE_PIXELS - 10_000) / (width * height)))


def _render(manifest, output):
    """Child-only preflight and all-or-nothing native conversion."""
    request = _read_json(Path(manifest))
    root, original_root = _root(request['source_workspace'])
    files = _checked_files(root, original_root, request['files'])
    from PIL import Image, ImageOps
    import pypdfium2 as pdfium
    Image.MAX_IMAGE_PIXELS = 40_000_000
    plan, counts, warnings = [], {}, []
    try:
        for source in files:
            suffix = Path(source['path']).suffix.lower()
            if suffix == '.pdf':
                document = pdfium.PdfDocument(source['path'])
                try:
                    count = len(document)
                    _require(0 < count <= 1_000_000, 'PDF 没有有效页面。')
                    counts[source['id']] = count
                    if sum(counts.values()) * 4 > MAX_VIEWS:
                        raise ValueError('局部图数量超过上限')
                    for index in range(count):
                        page = document[index]
                        try:
                            width, height = page.get_size()
                        finally:
                            page.close()
                        _require(all(math.isfinite(item) and 0 < item <= 1_000_000 for item in (width, height)),
                                 'PDF 页面尺寸无效。')
                        plan.append((source, index + 1, width, height, 'pdf'))
                finally:
                    document.close()
            elif suffix in _RASTERS:
                try:
                    with Image.open(source['path']) as image:
                        count = getattr(image, 'n_frames', 1)
                        _require(0 < count <= MAX_VIEWS // 4, '图片帧数超过局部图上限。')
                        frames = []
                        for index in range(count):
                            image.seek(index)
                            width, height = image.size
                            _require(width * height <= 40_000_000, '图片尺寸超过上限。')
                            frames.append((source, index + 1, width, height, 'raster'))
                        if not any(max(frame[2:4]) > 1800 and min(frame[2:4]) >= 600 for frame in frames):
                            continue
                        counts[source['id']] = count
                        if sum(counts.values()) * 4 > MAX_VIEWS:
                            raise ValueError('局部图数量超过上限')
                        # All selected frames are covered; no first-frame truncation.
                        plan.extend(frames)
                except (Image.UnidentifiedImageError, OSError):
                    warnings.append(source['original_name'] + '：未生成局部高清图，仍保留原附件供读取。')
        if request['pdf_counts'] is not None:
            _require(all(counts.get(reference) == count for reference, count in request['pdf_counts'].items()),
                     'PDF 局部图页码与概览不一致。')
        _require(len(plan) * 4 <= MAX_VIEWS, '局部图数量超过上限。')
        pixels = 0
        for _, _, width, height, kind in plan:
            for _, _, fractions in _QUADRANTS:
                left, top, right, bottom = _bounds(width, height, fractions, raster=kind == 'raster')
                scale = _scale(right - left, bottom - top, 4 if kind == 'pdf' else 1)
                pixels += math.ceil((right - left) * scale) * math.ceil((bottom - top) * scale)
        _require(pixels <= MAX_PIXELS, '局部图总像素超过上限。')
    except Exception:
        return {'page_counts': request['pdf_counts'] or counts, 'views': [],
                'warnings': warnings + ['本批局部高清图超出生成限制或无法完整转换；已保留全部原图和 PDF 概览，未采用不完整的局部图。']}
    if not plan:
        # No selected high-resolution input, including intentionally fake legacy
        # raster fixtures. Original read_image remains responsible for decoding it.
        return {'page_counts': {}, 'views': [], 'warnings': []}
    output = Path(output)
    output.mkdir(mode=0o700)
    views = []
    try:
        for source, number, width, height, kind in plan:
            document = page = image = oriented = None
            try:
                if kind == 'pdf':
                    document = pdfium.PdfDocument(source['path'])
                    page = document[number - 1]
                else:
                    image = Image.open(source['path'])
                    image.seek(number - 1)
                    oriented = ImageOps.exif_transpose(image.copy()).convert('RGB')
                    width, height = oriented.size
                for code, region, fractions in _QUADRANTS:
                    left, top, right, bottom = _bounds(width, height, fractions, raster=kind == 'raster')
                    scale = _scale(right - left, bottom - top, 4 if kind == 'pdf' else 1)
                    bitmap = current = None
                    try:
                        if kind == 'pdf':
                            # PDFium crops (left, bottom, right, top) AFTER page rotation;
                            # convert the top-left quadrant bounds to crop distances.
                            bitmap = page.render(scale=scale, crop=(left, height - bottom, width - right, top))
                            current = bitmap.to_pil().convert('RGB')
                        else:
                            current = oriented.crop((left, top, right, bottom))
                            current.thumbnail((min(MAX_SIDE - 1, max(1, int(current.width * scale))),
                                               min(MAX_SIDE - 1, max(1, int(current.height * scale)))))
                        reference = _alias(source['id'], number, code)
                        target = output / (reference + '.png')
                        current.save(target, format='PNG')
                        views.append({'id': reference, 'origin_id': source['id'], 'page': number,
                                      'path': str(target), 'sha256': _hash(target),
                                      'original_sha256': source['sha256'],
                                      'original_name': _name(source, number, region), 'region': region,
                                      'width': current.width, 'height': current.height})
                    finally:
                        if current is not None:
                            current.close()
                        if bitmap is not None:
                            bitmap.close()
            finally:
                for resource_object in (oriented, image, page, document):
                    if resource_object is not None:
                        resource_object.close()
        _require(len(views) == len(plan) * 4, '局部图区域覆盖不完整。')
        return {'page_counts': counts, 'views': views, 'warnings': warnings}
    except Exception:
        # Do not publish earlier pages when a later crop fails.
        return {'page_counts': request['pdf_counts'] or counts, 'views': [],
                'warnings': warnings + ['局部高清图未能完整转换；已保留全部原图和 PDF 概览，未采用不完整的局部图。']}


def main():
    if sys.platform.startswith('linux'):
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
        resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
    _require(len(sys.argv) == 4 and sys.argv[1] == '--render', '局部图转换需要 --render INPUT OUTPUT。')
    print(json.dumps(_render(sys.argv[2], sys.argv[3]), ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'error': 'optional detail rendering failed'}))
        sys.exit(1)
