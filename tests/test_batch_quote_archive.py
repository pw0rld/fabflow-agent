"""RAR intake tests; execute on ZeroClave, never on the developer workstation.

stored_rar4/stored_rar5 are tiny, uncompressed, ordinary-file fixture writers.
They are deliberately test-only and do not create encrypted or split archives.
RAR5 framing follows https://www.rarlab.com/technote.htm . Optional upstream
fixtures come from libarchive/libarchive's libarchive/test directory.
"""
import hashlib
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from batch_quote_archive import (ArchiveError, RAR4, RAR5, _Paths, _inspect_container,
                                 _library, _path, unpack_rar)


def _rar4_header(kind, flags, body=b''):
    data = struct.pack('<BHH', kind, flags, 7 + len(body)) + body
    return struct.pack('<H', zlib.crc32(data) & 65535) + data


def stored_rar4(files):
    """Public normal/stored RAR4 records, for benign test data only."""
    chunks = [RAR4, _rar4_header(0x73, 0, b'\x00' * 6)]
    for name, data in files:
        encoded = name.encode('utf-8')
        body = struct.pack('<IIBIIBBHI', len(data), len(data), 3,
                           zlib.crc32(data) & 0xffffffff, 0, 20, 0x30,
                           len(encoded), stat.S_IFREG | 0o644) + encoded
        chunks.extend((_rar4_header(0x74, 0x8000, body), data))
    chunks.append(_rar4_header(0x7b, 0))
    return b''.join(chunks)


def _vint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _rar5_header(body):
    data = _vint(len(body)) + body
    return struct.pack('<I', zlib.crc32(data) & 0xffffffff) + data


def stored_rar5(files):
    """Normal RAR5 stored files with data CRC; no extensions or helper code."""
    chunks = [RAR5, _rar5_header(b'\x01\x00\x00')]
    for name, data in files:
        encoded = name.encode('utf-8')
        body = (b'\x02\x02' + _vint(len(data)) + b'\x04' + _vint(len(data))
                + _vint(stat.S_IFREG | 0o644) + struct.pack('<I', zlib.crc32(data) & 0xffffffff)
                + b'\x00\x01' + _vint(len(encoded)) + encoded)
        chunks.extend((_rar5_header(body), data))
    chunks.append(_rar5_header(b'\x05\x00\x00'))
    return b''.join(chunks)


class ArchiveMetadataTests(unittest.TestCase):
    def test_paths_remain_metadata_and_unicode_collisions_are_rejected(self):
        for name in ('../part.stp', '/part.stp', 'C:/part.stp', 'a\\part.stp',
                     'a//part.stp', 'a/./part.stp', 'a/../part.stp', 'a/\x00part.stp',
                     '\uff0e\uff0e/part.stp', 'a/\uff1apart.stp'):
            with self.subTest(name=name), self.assertRaises(ArchiveError):
                _path(name)
        paths = _Paths()
        self.assertEqual(paths.add('cad/caf\u00e9.stp', False), 'cad/caf\u00e9.stp')
        with self.assertRaises(ArchiveError):
            paths.add('CAD/cafe\u0301.STP', False)
        with self.assertRaises(ArchiveError):
            paths.add('cad', False)
        paths = _Paths()
        paths.add('cad', False)
        with self.assertRaises(ArchiveError):
            paths.add('cad/part.stp', False)

    def test_declared_password_and_volume_are_clear_errors(self):
        for flags, code in ((1, 'rar_multivolume'), (0x80, 'rar_encrypted')):
            with self.subTest(flags=flags), self.assertRaises(ArchiveError) as rejected:
                _inspect_container(RAR4 + _rar4_header(0x73, flags, b'\x00' * 6))
            self.assertEqual(rejected.exception.code, code)
        with self.assertRaises(ArchiveError) as rejected:
            _inspect_container(RAR5 + _rar5_header(b'\x01\x00\x01'))
        self.assertEqual(rejected.exception.code, 'rar_multivolume')
        with self.assertRaises(ArchiveError) as rejected:
            _inspect_container(RAR5 + _rar5_header(b'\x04\x00\x00'))
        self.assertEqual(rejected.exception.code, 'rar_encrypted')

    def test_crc_and_truncation_fail_before_native_decode(self):
        for builder in (stored_rar4, stored_rar5):
            data = builder([('part.stp', b'ordinary test data')])
            self.assertIn(_inspect_container(data), {'rar4', 'rar5'})
            for broken in (data[:-1], data[:10], data[:-1] + bytes([data[-1] ^ 1]), b'not a rar'):
                with self.subTest(builder=builder.__name__), self.assertRaises(ArchiveError):
                    _inspect_container(broken)

    def test_short_long_block_reports_archive_error(self):
        # A damaged optional block must never leak struct.error to the API.
        broken = RAR4 + _rar4_header(0x73, 0, b'\x00' * 6) + _rar4_header(0x75, 0x8000)
        with self.assertRaises(ArchiveError) as rejected:
            _inspect_container(broken)
        self.assertEqual(rejected.exception.code, 'rar_invalid')

    def test_missing_engine_has_specific_dependency_error(self):
        with patch('batch_quote_archive.ctypes.CDLL', side_effect=OSError('private path')):
            with self.assertRaises(ArchiveError) as rejected:
                _library()
        self.assertEqual(rejected.exception.code, 'rar_engine_unavailable')
        self.assertNotIn('private', str(rejected.exception))


class NativeRarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def decode(self, content, **limits):
        retained = []
        result = unpack_rar(content, self.folder,
                            lambda name, data: retained.append((name, data)), **limits)
        self.assertEqual(list(self.folder.iterdir()), [])
        return retained, result

    def test_rar4_and_rar5_decode_exact_bytes_through_native_library(self):
        files = [('cad/panel.stp', b'ISO-10303-21;\nEND-ISO-10303-21;'),
                 ('notes/process.txt', '焊接要求'.encode()), ('empty.txt', b'')]
        for builder in (stored_rar4, stored_rar5):
            with self.subTest(builder=builder.__name__):
                retained, result = self.decode(builder(files))
                self.assertEqual(retained, files)
                self.assertEqual(result['files_count'], 3)
                self.assertEqual(result['unpacked_bytes'], sum(len(data) for _, data in files))

    def test_ignored_files_are_checked_without_being_retained(self):
        files = [('part.stp', b'content'), ('__MACOSX/._part.stp', b'ignored'), ('.DS_Store', b'ignored')]
        retained, _ = self.decode(stored_rar4(files))
        self.assertEqual(retained, files[:1])

    def test_limits_apply_to_actual_decoding_and_clean_temporary_blobs(self):
        files = [('a.stp', b'123456'), ('b.stp', b'abcdef')]
        for limits in ({'max_file_bytes': 5}, {'max_unpacked_bytes': 10},
                       {'max_files': 1}, {'max_entries': 1}):
            with self.subTest(limits=limits), self.assertRaises(ArchiveError) as rejected:
                self.decode(stored_rar4(files), **limits)
            self.assertEqual(rejected.exception.code, 'rar_limit')
            self.assertEqual(list(self.folder.iterdir()), [])

    def test_nested_archive_and_case_duplicate_metadata_are_rejected(self):
        for files, code in (([('part.zip', b'ordinary')], 'rar_nested'),
                            ([('a.stp', b'a'), ('A.STP', b'b')], 'rar_path')):
            with self.subTest(code=code), self.assertRaises(ArchiveError) as rejected:
                self.decode(stored_rar4(files))
            self.assertEqual(rejected.exception.code, code)
            self.assertEqual(list(self.folder.iterdir()), [])

    def test_data_crc_failure_is_rejected_and_cleans_partial_outputs(self):
        for builder in (stored_rar4, stored_rar5):
            for name in ('part.stp', '__MACOSX/._part.stp'):
                content = builder([(name, b'ordinary test content')])
                content = content.replace(b'ordinary test content', b'Ordinary test content')
                with self.subTest(builder=builder.__name__, name=name), self.assertRaises(ArchiveError) as rejected:
                    self.decode(content)
                self.assertEqual(rejected.exception.code, 'rar_invalid')
                self.assertEqual(list(self.folder.iterdir()), [])

    @unittest.skipUnless(os.environ.get('HYL_RAR_FIXTURE_DIR'), 'official fixtures supplied by server acceptance')
    def test_official_libarchive_rar4_and_rar5_fixtures(self):
        folder = Path(os.environ.get('HYL_RAR_FIXTURE_DIR'))
        # Upstream basic RAR4 is benign but contains a link; our policy rejects it.
        with self.assertRaises(ArchiveError) as rejected:
            self.decode((folder / 'test_read_format_rar.rar').read_bytes())
        self.assertEqual(rejected.exception.code, 'rar_special')
        retained, result = self.decode((folder / 'test_read_format_rar5_stored.rar').read_bytes())
        self.assertEqual(retained, [('helloworld.txt', b'hello libarchive test suite!\n')])
        self.assertEqual(result['format'], 'rar5')


if __name__ == '__main__':
    unittest.main()
