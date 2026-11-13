"""Immutable PDF page images for the single-part reader; no OCR or model calls.

The original attachment contract stays intact. A separate, versioned bundle
records exactly which PDF page each readable PNG represents. Native decoders
only run in the bounded ``--render`` child process.
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
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import zlib

from order_preprocess import MAX_PIXELS, MAX_SIDE, VERSION


BUNDLE = '.pdf-reading'
MANIFEST = 'manifest.json'
SCHEMA_VERSION = 1
RENDER_TIMEOUT = 60
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_PNG_BYTES = 32 * 1024 * 1024
_IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_RENDER_SLOTS = threading.BoundedSemaphore(1)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _root(workspace):
    original = Path(os.path.abspath(workspace))
    _require(not original.is_symlink() and original.is_dir(), 'PDF 读图目录无效。')
    return original.resolve(strict=True), original


def _confined(root, value, *, original_root=None, regular=True):
    _require(isinstance(value, (str, Path)), 'PDF 读图文件位置无效。')
    path = Path(value)
    _require('..' not in path.parts, 'PDF 读图文件位置无效。')
    if not path.is_absolute():
        path = root / path
    elif original_root is not None and path.is_relative_to(original_root):
        # Canonicalize the selected workspace prefix (/var on macOS), never an
        # attachment or bundle symlink inside it.
        path = root / path.relative_to(original_root)
    _require(path.is_relative_to(root), 'PDF 读图文件位于当前目录之外。')
    for component in (path, *path.parents):
        if component == root:
            break
        _require(not component.is_symlink(), 'PDF 读图文件不允许符号链接。')
    _require(path.exists() and path.resolve(strict=True).is_relative_to(root), 'PDF 读图文件缺失。')
    if regular:
        _require(stat.S_ISREG(path.stat().st_mode), 'PDF 读图文件须为普通文件。')
    else:
        _require(path.is_dir(), 'PDF 读图缓存目录无效。')
    return path


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _budget(max_views):
    _require(type(max_views) is int and 1 <= max_views <= 32, 'PDF 读图页面上限须为 1 至 32。')
    return max_views


def _checked_files(root, original_root, files):
    _require(isinstance(files, list) and 1 <= len(files) <= 32, 'PDF 读图需要 1 至 32 个原附件。')
    result, seen = [], set()
    for entry in files:
        _require(isinstance(entry, dict), 'PDF 原附件信息无效。')
        reference, digest = entry.get('id'), entry.get('sha256')
        name = entry.get('original_name')
        _require(isinstance(reference, str) and _IDENTIFIER.fullmatch(reference)
                 and reference not in seen, 'PDF 原附件标识无效或重复。')
        _require(isinstance(digest, str) and _SHA256.fullmatch(digest), 'PDF 原附件校验信息无效。')
        _require(isinstance(name, str) and bool(name.strip()) and len(name) <= 500,
                 'PDF 原附件名称无效。')
        path = _confined(root, entry.get('path'), original_root=original_root)
        _require(_hash(path) == digest, 'PDF 读图原附件已变化。')
        seen.add(reference)
        result.append({'id': reference, 'path': str(path), 'sha256': digest, 'original_name': name})
    return result


def _sources(files):
    return sorted(({key: item[key] for key in ('id', 'sha256', 'original_name')}
                   for item in files), key=lambda item: item['id'])


def _pdf_files(files):
    return [item for item in files if Path(item['path']).suffix.lower() == '.pdf']


def _alias(reference, page):
    return 'PDFPAGE-' + hashlib.sha256(reference.encode('utf-8')).hexdigest()[:16] + '-' + str(page)


def _read_json(path):
    _require(path.stat().st_size <= MAX_MANIFEST_BYTES, 'PDF 读图缓存清单过大。')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, 'PDF 读图缓存存在重复字段。')
            result[key] = value
        return result
    def invalid(value):
        raise ValueError('PDF 读图缓存包含无效数字。')
    try:
        return json.loads(path.read_text(), object_pairs_hook=unique, parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('PDF 读图缓存清单无效。') from exc


def _png_size(path):
    _require(0 < path.stat().st_size <= MAX_PNG_BYTES, 'PDF 派生图片大小无效。')
    with path.open('rb') as stream:
        header = stream.read(33)
    _require(len(header) == 33 and header[:8] == b'\x89PNG\r\n\x1a\n'
             and header[8:16] == b'\x00\x00\x00\rIHDR'
             and zlib.crc32(header[12:29]) & 0xffffffff == struct.unpack('>I', header[29:33])[0],
             'PDF 派生图片格式无效。')
    return struct.unpack('>II', header[16:24])


def _validate_data(root, files, data, max_views, *, bundle=None):
    _budget(max_views)
    _require(isinstance(data, dict) and set(data) ==
             {'schema_version', 'converter', 'sources', 'page_counts', 'views'}, 'PDF 读图缓存结构无效。')
    _require(type(data['schema_version']) is int and data['schema_version'] == SCHEMA_VERSION
             and data['converter'] == VERSION, 'PDF 读图缓存版本不匹配。')
    _require(data['sources'] == _sources(files), 'PDF 读图缓存与原附件不匹配。')
    pdfs = {item['id']: item for item in _pdf_files(files)}
    counts = data['page_counts']
    _require(pdfs and isinstance(counts, dict) and set(counts) == set(pdfs), 'PDF 读图页码清单无效。')
    _require(all(type(count) is int and count > 0 for count in counts.values()), 'PDF 读图页数无效。')
    total = sum(counts.values())
    _require(total + len(files) - len(pdfs) <= max_views, 'PDF 页数与其他附件总量超过单件读图上限。')
    rows = data['views']
    _require(isinstance(rows, list) and len(rows) == total, 'PDF 读图页面不完整。')
    expected_keys = {'id', 'origin_id', 'page', 'path', 'sha256', 'original_sha256',
                     'original_name', 'width', 'height'}
    all_ids, pairs, aliases, pixels = {item['id'] for item in files}, set(), set(), 0
    bundle = root / BUNDLE if bundle is None else bundle
    _confined(root, bundle, regular=False)
    for row in rows:
        _require(isinstance(row, dict) and set(row) == expected_keys, 'PDF 派生页面结构无效。')
        origin, page = row['origin_id'], row['page']
        _require(isinstance(origin, str) and origin in pdfs and type(page) is int
                 and 1 <= page <= counts[origin], 'PDF 派生页面来源或页码无效。')
        alias = _alias(origin, page)
        _require(row['id'] == alias and alias not in all_ids and alias not in aliases
                 and (origin, page) not in pairs, 'PDF 派生页面标识冲突或重复。')
        _require(row['path'] == BUNDLE + '/' + alias + '.png', 'PDF 派生页面位置无效。')
        _require(row['original_sha256'] == pdfs[origin]['sha256'], 'PDF 派生页面原件校验不一致。')
        _require(row['original_name'] == pdfs[origin]['original_name'] + ' 第' + str(page) + '页.png',
                 'PDF 派生页面名称不匹配。')
        width, height = row['width'], row['height']
        _require(type(width) is int and type(height) is int and 0 < width <= MAX_SIDE
                 and 0 < height <= MAX_SIDE, 'PDF 派生页面尺寸无效。')
        pixels += width * height
        _require(pixels <= MAX_PIXELS, 'PDF 读图像素总量超过上限。')
        path = _confined(root, bundle / (alias + '.png'))
        _require(isinstance(row['sha256'], str) and _SHA256.fullmatch(row['sha256'])
                 and _hash(path) == row['sha256'], 'PDF 派生页面校验失败。')
        _require(_png_size(path) == (width, height), 'PDF 派生页面尺寸与缓存不一致。')
        aliases.add(alias)
        pairs.add((origin, page))
    _require(pairs == {(reference, page) for reference, count in counts.items()
                       for page in range(1, count + 1)}, 'PDF 读图页码不连续或页面缺失。')
    return copy.deepcopy(data)


def validate_pdf_views(workspace, files, data, max_views=32):
    """Read-only verification against the committed bundle and original bytes."""
    root, original_root = _root(workspace)
    checked = _checked_files(root, original_root, files)
    manifest = _confined(root, root / BUNDLE / MANIFEST)
    cached = _read_json(manifest)
    _require(data == cached, 'PDF 读图缓存与运行清单不一致。')
    return _validate_data(root, checked, cached, max_views)


@contextmanager
def _bundle_lock(root):
    lock = root / (BUNDLE + '.lock')
    _require(not lock.is_symlink(), 'PDF 读图缓存锁无效。')
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+') as stream:
        _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'PDF 读图缓存锁无效。')
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _copy_new(source, destination):
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as target, source.open('rb') as stream:
        shutil.copyfileobj(stream, target, length=1024 * 1024)
        target.flush()
        os.fsync(target.fileno())


def _write_manifest(path, data):
    content = (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + '\n').encode('utf-8')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def prepare_pdf_views(workspace, files, max_views=32):
    """Atomically prepare all PDF pages, or reuse a completely verified bundle.

    Native images are left alone. One failed PDF refuses the entire bundle;
    neither an incomplete cache nor a truncated page set is committed.
    """
    _budget(max_views)
    _require(isinstance(files, list), 'PDF 读图原附件清单无效。')
    if not any(isinstance(item, dict) and isinstance(item.get('path'), (str, Path))
               and Path(item['path']).suffix.lower() == '.pdf' for item in files):
        return None
    root, original_root = _root(workspace)
    checked = _checked_files(root, original_root, files)
    bundle = root / BUNDLE
    if bundle.exists() or bundle.is_symlink():
        cached = _read_json(_confined(root, bundle / MANIFEST))
        return validate_pdf_views(root, checked, cached, max_views)
    with _RENDER_SLOTS, _bundle_lock(root):
        if bundle.exists() or bundle.is_symlink():
            cached = _read_json(_confined(root, bundle / MANIFEST))
            return validate_pdf_views(root, checked, cached, max_views)
        temporary = Path(tempfile.mkdtemp(prefix=BUNDLE + '-stage-', dir=root))
        try:
            manifest, output = temporary / 'input.json', temporary / 'rendered'
            pdfs = _pdf_files(checked)
            _write_manifest(manifest, {'files': pdfs, 'source_workspace': str(root),
                                       'max_views': max_views, 'non_pdf_count': len(checked) - len(pdfs)})
            try:
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--render',
                                         str(manifest), str(output)], capture_output=True, text=True,
                                        timeout=RENDER_TIMEOUT, cwd=Path(__file__).resolve().parent)
            except subprocess.TimeoutExpired as exc:
                raise ValueError('PDF 页面转换超过 60 秒，请拆分资料后重试。') from exc
            try:
                rendered = json.loads(result.stdout)
            except (ValueError, UnicodeError) as exc:
                raise ValueError('PDF 页面转换进程未完成，请检查文件。') from exc
            _require(isinstance(rendered, dict), 'PDF 页面转换结果无效。')
            _require(result.returncode == 0, str(rendered.get('error', 'PDF 页面转换失败。'))[:500])
            _require(not rendered.get('warnings') and rendered.get('converter') == VERSION,
                     'PDF 页面转换不完整，请检查全部 PDF。')
            staged_bundle = temporary / 'bundle'
            staged_bundle.mkdir(mode=0o700)
            data = {'schema_version': SCHEMA_VERSION, 'converter': VERSION, 'sources': _sources(checked),
                    'page_counts': rendered.get('page_counts'), 'views': []}
            for view in rendered.get('views', []):
                _require(isinstance(view, dict) and isinstance(view.get('origin_id'), str)
                         and type(view.get('page')) is int, 'PDF 派生页面来源无效。')
                alias = _alias(view['origin_id'], view['page'])
                source = _confined(root, view.get('path'))
                _require(source.is_relative_to(output), 'PDF 转换文件位置无效。')
                _copy_new(source, staged_bundle / (alias + '.png'))
                data['views'].append({'id': alias, 'origin_id': view['origin_id'], 'page': view['page'],
                                      'path': BUNDLE + '/' + alias + '.png',
                                      **{key: view.get(key) for key in ('sha256', 'original_sha256', 'original_name', 'width', 'height')}})
            # Recheck original hashes after the native worker, before commit.
            checked = _checked_files(root, original_root, files)
            _validate_data(root, checked, data, max_views, bundle=staged_bundle)
            _write_manifest(staged_bundle / MANIFEST, data)
            _require(not bundle.exists() and not bundle.is_symlink(), 'PDF 读图缓存位置已被占用。')
            os.rename(staged_bundle, bundle)
            return validate_pdf_views(root, checked, data, max_views)
        finally:
            shutil.rmtree(temporary)


def copy_pdf_views(source_workspace, target_workspace, source_files, target_files):
    """Copy a pinned bundle to a review workspace without invoking decoders."""
    source_root, source_original = _root(source_workspace)
    source_bundle = source_root / BUNDLE
    if not source_bundle.exists() and not source_bundle.is_symlink():
        return None  # Older runs intentionally keep their original behavior.
    source_checked = _checked_files(source_root, source_original, source_files)
    data = _read_json(_confined(source_root, source_bundle / MANIFEST))
    validate_pdf_views(source_root, source_checked, data)
    target_root, target_original = _root(target_workspace)
    target_checked = _checked_files(target_root, target_original, target_files)
    _require(_sources(source_checked) == _sources(target_checked), '回查 PDF 原附件与首轮不一致。')
    target_bundle = target_root / BUNDLE
    if target_bundle.exists() or target_bundle.is_symlink():
        return validate_pdf_views(target_root, target_checked, data)
    with _RENDER_SLOTS, _bundle_lock(target_root):
        if target_bundle.exists() or target_bundle.is_symlink():
            return validate_pdf_views(target_root, target_checked, data)
        temporary = Path(tempfile.mkdtemp(prefix=BUNDLE + '-stage-', dir=target_root))
        try:
            staged_bundle = temporary / 'bundle'
            staged_bundle.mkdir(mode=0o700)
            for view in data['views']:
                path = _confined(source_root, view['path'])
                _copy_new(path, staged_bundle / Path(view['path']).name)
            _copy_new(_confined(source_root, source_bundle / MANIFEST), staged_bundle / MANIFEST)
            _require(_read_json(staged_bundle / MANIFEST) == data, '首轮 PDF 缓存已变化。')
            target_checked = _checked_files(target_root, target_original, target_files)
            _validate_data(target_root, target_checked, data, 32, bundle=staged_bundle)
            _require(not target_bundle.exists() and not target_bundle.is_symlink(), '回查 PDF 缓存位置已被占用。')
            os.rename(staged_bundle, target_bundle)
            return validate_pdf_views(target_root, target_checked, data)
        finally:
            shutil.rmtree(temporary)


def _render(manifest, output):
    """Child-only decoder path, with a full page-count preflight."""
    request = _read_json(Path(manifest))
    root, original_root = _root(request['source_workspace'])
    files = _checked_files(root, original_root, request['files'])
    max_views = _budget(request['max_views'])
    non_pdf = request['non_pdf_count']
    _require(type(non_pdf) is int and non_pdf >= 0 and len(files) + non_pdf <= max_views,
             'PDF 页数与其他附件总量超过单件读图上限。')
    _require(len(_pdf_files(files)) == len(files), 'PDF 转换清单只能包含 PDF。')
    import pypdfium2 as pdfium
    from order_preprocess import convert
    counts = {}
    for source in files:
        document = None
        try:
            document = pdfium.PdfDocument(source['path'])
            count = len(document)
            _require(count > 0, 'PDF 没有可读页面。')
            counts[source['id']] = count
        except Exception as exc:
            raise ValueError(source['original_name'] + '：PDF 无法完整读取，可能损坏或需要密码。') from exc
        finally:
            if document is not None:
                document.close()
    _require(sum(counts.values()) + non_pdf <= max_views,
             'PDF 页数与其他附件总量超过单件读图上限，请拆分资料。')
    result = convert(files, output)
    _require(not result['warnings'], 'PDF 页面转换不完整：' + '；'.join(result['warnings']))
    expected = {(reference, page) for reference, count in counts.items() for page in range(1, count + 1)}
    actual = {(view.get('origin_id'), view.get('page')) for view in result['views']}
    _require(len(result['views']) == len(expected) and actual == expected
             and all(view.get('kind') == 'page_image' for view in result['views']),
             'PDF 页面转换不完整。')
    return {**result, 'page_counts': counts}


def main():
    if sys.platform.startswith('linux'):
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
        resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
    _require(len(sys.argv) == 4 and sys.argv[1] == '--render', 'PDF 转换需要 --render INPUT OUTPUT。')
    print(json.dumps(_render(sys.argv[2], sys.argv[3]), ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)[:500]}, ensure_ascii=False))
        sys.exit(1)
