"""Bounded RAR/RAR5 intake. Native decoding runs outside the web process.

No archive pathname is used as an output pathname. The child writes numbered
blobs, which the parent checks before passing them to the ordinary batch store.
The system libarchive package supplies the RAR decoders; no external executable
or archive-provided helper is invoked.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import selectors
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import zlib


RAR4 = b'Rar!\x1a\x07\x00'
RAR5 = b'Rar!\x1a\x07\x01\x00'
MAX_UPLOAD = 50 * 1024 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
MAX_STDERR = 8192
CHUNK = 256 * 1024
ARCHIVE_SUFFIXES = {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.tgz'}
MESSAGES = {
    'rar_invalid': 'RAR 无法完整读取，文件可能已损坏；请重新压缩后上传。',
    'rar_encrypted': '暂不支持加密 RAR，请移除密码后重新上传。',
    'rar_multivolume': '暂不支持分卷 RAR，请合并为单个非加密压缩包后上传。',
    'rar_path': 'RAR 内含不允许、重复或冲突的文件路径，请整理后重新上传。',
    'rar_special': 'RAR 不支持软链接、硬链接或特殊文件。',
    'rar_nested': '请展开内部压缩包后再上传，不支持嵌套压缩包。',
    'rar_limit': 'RAR 展开超出处理范围：单文件 20 MB、总量 200 MB、最多 200 个文件和 1000 项。',
    'rar_ratio': 'RAR 压缩比例超出处理范围。',
    'rar_timeout': 'RAR 读取超时，请缩小资料包或重新压缩后上传。',
    'rar_resource': 'RAR 解码所需资源超出处理范围，请缩小资料包或重新压缩。',
    'rar_unsupported': 'RAR 使用了当前引擎不支持的压缩方式，请重新压缩为 ZIP 或普通 RAR。',
    'rar_engine_unavailable': '服务器 RAR 解码引擎暂不可用，请联系管理员检查 libarchive。',
}


class ArchiveError(ValueError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in MESSAGES else 'rar_invalid'
        self.message = MESSAGES[self.code]
        self.status = 503 if self.code == 'rar_engine_unavailable' else 400
        super().__init__(self.message)


def _require(condition, code='rar_invalid'):
    if not condition:
        raise ArchiveError(code)


def _path(name):
    """At least as conservative as ZIP intake, including Unicode collisions."""
    _require(isinstance(name, str) and 0 < len(name) <= 1024, 'rar_path')
    value = name[:-1] if name.endswith('/') else name
    folded = unicodedata.normalize('NFKC', value)
    for text in (value, folded):
        parts = text.split('/')
        _require('\\' not in text and not any(ord(c) < 32 or ord(c) == 127 for c in text)
                 and not PurePosixPath(text).is_absolute()
                 and all(part not in {'', '.', '..'} and ':' not in part for part in parts)
                 and len(parts[-1]) <= 240, 'rar_path')
    return value, folded.casefold()


class _Paths:
    def __init__(self):
        self.explicit = set()
        self.kinds = {}

    def add(self, name, directory):
        relative, key = _path(name)
        _require(key not in self.explicit, 'rar_path')
        parts = key.split('/')
        for index in range(1, len(parts)):
            parent = '/'.join(parts[:index])
            _require(self.kinds.get(parent) != 'file', 'rar_path')
            self.kinds[parent] = 'dir'
        _require(directory or key not in self.kinds, 'rar_path')
        _require(self.kinds.get(key) != 'file', 'rar_path')
        self.explicit.add(key)
        self.kinds[key] = 'dir' if directory else 'file'
        return relative


def _vint(data, offset, end):
    value = 0
    for shift in range(0, 70, 7):
        _require(offset < end)
        byte = data[offset]
        offset += 1
        _require(shift < 63 or byte <= 1)
        value |= (byte & 127) << shift
        if not byte & 128:
            return value, offset
    raise ArchiveError('rar_invalid')


def _inspect_container(data, *, checks=None):
    """Check framing and volume/password flags before entering native code."""
    _require(isinstance(data, bytes) and 0 < len(data) <= MAX_UPLOAD, 'rar_limit')
    if data.startswith(RAR4):
        offset, first, blocks = len(RAR4), True, 0
        while offset < len(data):
            blocks += 1
            _require(blocks <= 4000, 'rar_limit')
            _require(len(data) - offset >= 7)
            crc, kind, flags, size = struct.unpack_from('<HBHH', data, offset)
            _require(size >= 7 and offset + size <= len(data))
            end = offset + size
            _require(zlib.crc32(data[offset + 2:end]) & 65535 == crc)
            if first:
                _require(kind == 0x73 and size >= 13)
                _require(not flags & 1, 'rar_multivolume')
                _require(not flags & 0x80, 'rar_encrypted')
                first = False
            if kind == 0x74:
                _require(size >= 32)
                _require(not flags & 3, 'rar_multivolume')
                _require(not flags & 4, 'rar_encrypted')
            if kind == 0x7b:
                _require(not flags & 1, 'rar_multivolume')
                _require(end == len(data))
            _require(not flags & 0x8000 or size >= 11)
            packed = struct.unpack_from('<I', data, offset + 7)[0] if flags & 0x8000 else 0
            if kind == 0x74 and flags & 0x100:
                _require(size >= 40)
                packed += struct.unpack_from('<I', data, offset + 32)[0] << 32
            if kind == 0x74 and checks is not None:
                unpacked = struct.unpack_from('<I', data, offset + 11)[0]
                if flags & 0x100:
                    unpacked += struct.unpack_from('<I', data, offset + 36)[0] << 32
                checks.append({'size': unpacked, 'crc32': struct.unpack_from('<I', data, offset + 16)[0]})
            _require(end + packed <= len(data))
            offset = end + packed
        _require(not first)
        return 'rar4'
    _require(data.startswith(RAR5))
    offset, first, ended, blocks = len(RAR5), True, False, 0
    while offset < len(data):
        blocks += 1
        _require(blocks <= 4000, 'rar_limit')
        _require(len(data) - offset >= 5 and not ended)
        crc = struct.unpack_from('<I', data, offset)[0]
        size, cursor = _vint(data, offset + 4, len(data))
        _require(0 < size <= 2 * 1024 * 1024 and cursor + size <= len(data))
        end = cursor + size
        _require(zlib.crc32(data[offset + 4:end]) & 0xffffffff == crc)
        kind, cursor = _vint(data, cursor, end)
        flags, cursor = _vint(data, cursor, end)
        extra, packed = 0, 0
        if flags & 1:
            extra, cursor = _vint(data, cursor, end)
        if flags & 2:
            packed, cursor = _vint(data, cursor, end)
        _require(extra <= end - cursor)
        _require(not flags & 0x18, 'rar_multivolume')
        _require(kind != 4, 'rar_encrypted')
        if first:
            _require(kind == 1)
            archive_flags, cursor = _vint(data, cursor, end - extra)
            _require(not archive_flags & 3, 'rar_multivolume')
            first = False
        if kind == 5:
            end_flags, cursor = _vint(data, cursor, end - extra)
            _require(not end_flags & 1, 'rar_multivolume')
            ended = True
        if kind == 2 and checks is not None:
            file_flags, cursor = _vint(data, cursor, end - extra)
            unpacked, cursor = _vint(data, cursor, end - extra)
            _, cursor = _vint(data, cursor, end - extra)  # attributes
            if file_flags & 2:
                _require(cursor + 4 <= end - extra)
                cursor += 4  # optional Unix timestamp
            crc = None
            if file_flags & 4:
                _require(cursor + 4 <= end - extra)
                crc = struct.unpack_from('<I', data, cursor)[0]
            checks.append({'size': unpacked, 'crc32': crc})
        _require(end + packed <= len(data))
        offset = end + packed
    _require(not first and ended)
    return 'rar5'


def _kill(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def _run_decoder(command, timeout_seconds):
    # No API/proxy credentials, user configuration, or inherited file handles.
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
                                   env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8'})
    except OSError as error:
        raise ArchiveError('rar_engine_unavailable') from error
    output, diagnostic = bytearray(), bytearray()
    deadline = time.monotonic() + timeout_seconds
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, (output, MAX_OUTPUT))
            selector.register(process.stderr, selectors.EVENT_READ, (diagnostic, MAX_STDERR))
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ArchiveError('rar_timeout')
                for key, _ in selector.select(min(remaining, 0.2)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target, maximum = key.data
                    target.extend(chunk)
                    _require(len(target) <= maximum, 'rar_resource')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ArchiveError('rar_timeout')
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                raise ArchiveError('rar_timeout') from error
        _require(process.returncode == 0, 'rar_resource' if process.returncode < 0 else 'rar_invalid')
        try:
            reply = json.loads(output)
        except (ValueError, UnicodeError) as error:
            raise ArchiveError('rar_invalid') from error
        _require(isinstance(reply, dict))
        if reply.get('ok') is not True:
            raise ArchiveError(reply.get('code'))
        return reply
    finally:
        _kill(process)
        process.stdout.close()
        process.stderr.close()


def unpack_rar(content, staging, retain, *, max_file_bytes=20 * 1024 * 1024,
               max_unpacked_bytes=200 * 1024 * 1024, max_files=200,
               max_entries=1000, timeout_seconds=30):
    """Decode once, validate numbered results, and call retain(relative, bytes)."""
    _inspect_container(content)
    limits = {'file': max_file_bytes, 'total': max_unpacked_bytes,
              'files': max_files, 'entries': max_entries}
    _require(all(type(value) is int and value > 0 for value in limits.values()), 'rar_limit')
    with tempfile.TemporaryDirectory(prefix='.rar-', dir=staging) as temporary:
        folder = Path(temporary)
        os.chmod(folder, 0o700)
        source, decoded = folder / 'source.rar', folder / 'decoded'
        source.write_bytes(content)
        source.chmod(0o600)
        decoded.mkdir(mode=0o700)
        reply = _run_decoder([sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                              '--decode-rar', str(source), str(decoded),
                              json.dumps(limits, separators=(',', ':'))], timeout_seconds)
        rows = reply.get('files')
        _require(isinstance(rows, list) and len(rows) <= max_files)
        paths, actual = _Paths(), 0
        for index, row in enumerate(rows, 1):
            _require(isinstance(row, dict) and row.get('blob') == 'F' + str(index).zfill(4) + '.bin')
            relative = paths.add(row.get('relative_path'), False)
            _require(PurePosixPath(relative).suffix.lower() not in ARCHIVE_SUFFIXES, 'rar_nested')
            _require(type(row.get('size')) is int and 0 <= row['size'] <= max_file_bytes)
            path = decoded / row['blob']
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size == row['size'])
                data = stream.read(max_file_bytes + 1)
            actual += len(data)
            _require(len(data) == row['size'] and actual <= max_unpacked_bytes, 'rar_limit')
            _require(hashlib.sha256(data).hexdigest() == row.get('sha256'))
            _require(not _nested_magic(data), 'rar_nested')
            retain(relative, data)
        return {'format': reply.get('format'), 'files_count': len(rows), 'unpacked_bytes': actual}


def _native_error(library, archive):
    message = (library.archive_error_string(archive) or b'').decode('utf-8', 'replace').lower()
    if any(word in message for word in ('encrypt', 'password', 'passphrase')):
        return ArchiveError('rar_encrypted')
    if any(word in message for word in ('multivolume', 'multi-volume', 'volume', 'split')):
        return ArchiveError('rar_multivolume')
    if any(word in message for word in ('memory', 'dictionary', 'allocation')):
        return ArchiveError('rar_resource')
    if any(word in message for word in ('unsupported', 'not supported')):
        return ArchiveError('rar_unsupported')
    return ArchiveError('rar_invalid')


def _library():
    try:
        library = ctypes.CDLL('libarchive.so.13')
        pointer, integer, text = ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p
        signatures = {
            'archive_read_new': (pointer, []),
            'archive_read_support_filter_none': (integer, [pointer]),
            'archive_read_support_format_rar': (integer, [pointer]),
            'archive_read_support_format_rar5': (integer, [pointer]),
            'archive_read_open_filename': (integer, [pointer, text, ctypes.c_size_t]),
            'archive_read_next_header': (integer, [pointer, ctypes.POINTER(pointer)]),
            'archive_read_data': (ctypes.c_ssize_t, [pointer, pointer, ctypes.c_size_t]),
            'archive_read_close': (integer, [pointer]),
            'archive_read_free': (integer, [pointer]),
            'archive_read_has_encrypted_entries': (integer, [pointer]),
            'archive_format': (integer, [pointer]),
            'archive_error_string': (text, [pointer]),
            'archive_entry_pathname_utf8': (text, [pointer]),
            'archive_entry_symlink': (text, [pointer]),
            'archive_entry_hardlink': (text, [pointer]),
            'archive_entry_filetype': (ctypes.c_uint, [pointer]),
            'archive_entry_size_is_set': (integer, [pointer]),
            'archive_entry_size': (ctypes.c_longlong, [pointer]),
            'archive_entry_is_encrypted': (integer, [pointer]),
        }
        for name, (result, arguments) in signatures.items():
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments
        return library
    except (OSError, AttributeError) as error:
        raise ArchiveError('rar_engine_unavailable') from error


def _nested_magic(data):
    return any(data.startswith(magic) for magic in
               (RAR4, RAR5, b'PK\x03\x04', b'7z\xbc\xaf\x27\x1c', b'\x1f\x8b', b'BZh', b'\xfd7zXZ\x00'))


def _decode(source, output, limits):
    # libarchive releases differ in whether stored RAR members are CRC-checked
    # by archive_read_data. Retain and check the archive's own uncompressed CRC
    # explicitly for each ordinary file/directory, including ignored entries.
    checks = []
    _inspect_container(source.read_bytes(), checks=checks)
    library = _library()
    archive = library.archive_read_new()
    _require(bool(archive), 'rar_resource')
    try:
        for name in ('archive_read_support_filter_none', 'archive_read_support_format_rar',
                     'archive_read_support_format_rar5'):
            _require(getattr(library, name)(archive) == 0, 'rar_engine_unavailable')
        if library.archive_read_open_filename(archive, os.fsencode(source), 65536) != 0:
            raise _native_error(library, archive)
        paths, rows, entry = _Paths(), [], ctypes.c_void_p()
        declared = actual = entries = 0
        ratio_limit = max(1024 * 1024, source.stat().st_size * 1000)
        buffer = ctypes.create_string_buffer(CHUNK)
        while True:
            status = library.archive_read_next_header(archive, ctypes.byref(entry))
            if status == 1:
                break
            if status != 0:
                raise _native_error(library, archive)
            entries += 1
            _require(entries <= limits['entries'], 'rar_limit')
            _require(entries <= len(checks))
            expected = checks[entries - 1]
            _require(library.archive_format(archive) & 0xff0000 in {0xD0000, 0x100000})
            _require(library.archive_entry_is_encrypted(entry) == 0, 'rar_encrypted')
            _require(library.archive_entry_symlink(entry) is None
                     and library.archive_entry_hardlink(entry) is None, 'rar_special')
            kind = library.archive_entry_filetype(entry)
            _require(kind in {stat.S_IFREG, stat.S_IFDIR}, 'rar_special')
            raw_name = library.archive_entry_pathname_utf8(entry)
            try:
                name = raw_name.decode('utf-8') if raw_name is not None else None
            except UnicodeError as error:
                raise ArchiveError('rar_path') from error
            directory = kind == stat.S_IFDIR
            relative = paths.add(name, directory)
            _require(library.archive_entry_size_is_set(entry) != 0)
            size = library.archive_entry_size(entry)
            _require(size == expected['size'])
            declared += size
            _require(0 <= size <= limits['file'] and declared <= limits['total'], 'rar_limit')
            _require(declared <= ratio_limit, 'rar_ratio')
            _require(not directory or size == 0)
            parts = relative.split('/')
            ignored = directory or any(part in {'__MACOSX', '.DS_Store'} or part.startswith('._') for part in parts)
            _require(directory or PurePosixPath(relative).suffix.lower() not in ARCHIVE_SUFFIXES, 'rar_nested')
            if not ignored:
                _require(len(rows) < limits['files'], 'rar_limit')
            blob = 'F' + str(len(rows) + 1).zfill(4) + '.bin'
            stream = None if ignored else open(output / blob, 'xb')
            if stream is not None:
                os.chmod(output / blob, 0o600)
            digest, count, crc = hashlib.sha256(), 0, 0
            try:
                while True:
                    got = library.archive_read_data(archive, buffer, CHUNK)
                    if got < 0:
                        raise _native_error(library, archive)
                    if got == 0:
                        break
                    count += got
                    actual += got
                    _require(count <= size and count <= limits['file'] and actual <= limits['total'], 'rar_limit')
                    _require(actual <= ratio_limit, 'rar_ratio')
                    chunk = buffer.raw[:got]
                    if count == got and not directory:
                        _require(not _nested_magic(chunk), 'rar_nested')
                    digest.update(chunk)
                    crc = zlib.crc32(chunk, crc)
                    if stream is not None:
                        stream.write(chunk)
            finally:
                if stream is not None:
                    stream.close()
            _require(count == size)
            _require(expected['crc32'] is None or crc & 0xffffffff == expected['crc32'])
            if not ignored:
                rows.append({'blob': blob, 'relative_path': relative, 'size': size,
                             'sha256': digest.hexdigest()})
        _require(entries == len(checks))
        _require(library.archive_read_has_encrypted_entries(archive) <= 0, 'rar_encrypted')
        if library.archive_read_close(archive) != 0:
            raise _native_error(library, archive)
        return {'ok': True, 'files': rows, 'format': 'rar5' if library.archive_format(archive) & 0xff0000 == 0x100000 else 'rar4'}
    finally:
        library.archive_read_free(archive)


def _worker_main():
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (15, 16))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (20 * 1024 * 1024, 20 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    if hasattr(resource, 'RLIMIT_NPROC'):
        resource.setrlimit(resource.RLIMIT_NPROC, (0, 0))
    try:
        _require(len(sys.argv) == 5 and sys.argv[1] == '--decode-rar')
        source, output = Path(sys.argv[2]), Path(sys.argv[3])
        limits = json.loads(sys.argv[4])
        _require(isinstance(limits, dict) and set(limits) == {'file', 'total', 'files', 'entries'})
        _require(all(type(value) is int and 0 < value <= cap for value, cap in
                     zip((limits['file'], limits['total'], limits['files'], limits['entries']),
                         (20 * 1024 * 1024, 200 * 1024 * 1024, 200, 1000))), 'rar_limit')
        _require(source.is_file() and not source.is_symlink() and source.stat().st_size <= MAX_UPLOAD)
        _require(output.is_dir() and not output.is_symlink())
        result = _decode(source, output, limits)
    except ArchiveError as error:
        result = {'ok': False, 'code': error.code}
    except (MemoryError, OSError):
        result = {'ok': False, 'code': 'rar_resource'}
    except Exception:
        result = {'ok': False, 'code': 'rar_invalid'}
    sys.stdout.write(json.dumps(result, ensure_ascii=True, separators=(',', ':')))


if __name__ == '__main__':
    _worker_main()
