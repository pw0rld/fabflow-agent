"""Serve only an explicit set of bundled, reviewed public CAD examples.

There is no customer-store fallback, session creation, CAD parsing or model call.
The request chooses a registry entry, never a filesystem path or storage root.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat


PREFIX = '/workbench/public-examples/'
EXAMPLE_IDS = frozenset({'sheet-metal-enclosure', 'motor-bracket', 'flanged-bushing'})
MAX_SMALL_BYTES = 512 * 1024
MAX_GEOMETRY_BYTES = 24 * 1024 * 1024
MAX_STEP_BYTES = 20 * 1024 * 1024
_FILES = {
    'example.json': ('application/json; charset=utf-8', MAX_SMALL_BYTES),
    'geometry.json': ('application/json; charset=utf-8', MAX_GEOMETRY_BYTES),
    'source.step': ('application/step', MAX_STEP_BYTES),
    'LICENSE.txt': ('text/plain; charset=utf-8', MAX_SMALL_BYTES),
    'README.txt': ('text/plain; charset=utf-8', MAX_SMALL_BYTES),
}


@dataclass(frozen=True)
class PublicExampleAsset:
    content: bytes
    content_type: str
    filename: str | None = None


def _missing():
    # Avoid revealing whether a private path, source directory or file exists.
    raise FileNotFoundError()


def _json(data):
    def nonfinite(_value):
        raise ValueError('Nonfinite JSON')
    try:
        return json.loads(data, parse_constant=nonfinite)
    except (UnicodeError, ValueError):
        _missing()


def _text(value, maximum=300):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _catalog(data):
    catalog = _json(data)
    if (not isinstance(catalog, dict) or set(catalog) != {'schema_version', 'examples'}
            or type(catalog.get('schema_version')) is not int
            or catalog['schema_version'] != 1 or not isinstance(catalog.get('examples'), list)
            or not 1 <= len(catalog['examples']) <= len(EXAMPLE_IDS)):
        _missing()
    seen = set()
    for row in catalog['examples']:
        fields = {'id', 'title', 'category', 'description', 'filename', 'source_label', 'license',
                  'part_count', 'occurrence_count', 'detail_url', 'geometry_url', 'download_url'}
        if not isinstance(row, dict) or set(row) != fields:
            _missing()
        sample = row.get('id')
        if not isinstance(sample, str) or sample not in EXAMPLE_IDS or sample in seen:
            _missing()
        seen.add(sample)
        for key in ('title', 'category', 'description', 'filename', 'source_label', 'license'):
            if not _text(row.get(key), 1000 if key == 'description' else 300):
                _missing()
        if any(char in row['filename'] for char in '/\\'):
            _missing()
        for key in ('part_count', 'occurrence_count'):
            if type(row.get(key)) is not int or not 1 <= row[key] <= 10000:
                _missing()
        for key, filename in (('detail_url', 'example.json'), ('geometry_url', 'geometry.json'),
                              ('download_url', 'source.step')):
            if row.get(key) != PREFIX + sample + '/' + filename:
                _missing()
    return catalog


class PublicExamples:
    """The production caller passes its fixed APP_ROOT/web/public-examples.

    An explicit root is injectable for isolated tests only. No route, environment
    variable, query parameter or uploaded manifest controls this value.
    """
    def __init__(self, root):
        self.root = Path(root).absolute()
        if any(part in {'.', '..'} for part in self.root.parts):
            raise ValueError('Public example root must be an absolute ordinary directory.')

    def _read(self, parts, maximum):
        """Walk directory descriptors so parent and file symlinks cannot escape."""
        directory = file = None
        nofollow = getattr(os, 'O_NOFOLLOW', 0)
        try:
            directory = os.open(self.root.anchor, os.O_RDONLY | os.O_DIRECTORY)
            for component in (*self.root.parts[1:], *parts[:-1]):
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=directory)
                os.close(directory)
                directory = child
            file = os.open(parts[-1], os.O_RDONLY | nofollow | os.O_NONBLOCK, dir_fd=directory)
            info = os.fstat(file)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
                _missing()
            with os.fdopen(file, 'rb') as stream:
                file = None
                data = stream.read(maximum + 1)
            if not 0 < len(data) <= maximum:
                _missing()
            return data
        except OSError:
            _missing()
        finally:
            if file is not None:
                os.close(file)
            if directory is not None:
                os.close(directory)

    def read(self, path, *, query=''):
        if not isinstance(path, str) or query or '%' in path or not path.startswith(PREFIX):
            _missing()
        suffix = path[len(PREFIX):]
        if suffix == 'catalog.json':
            data = self._read(('catalog.json',), MAX_SMALL_BYTES)
            _catalog(data)
            return PublicExampleAsset(data, 'application/json; charset=utf-8')
        parts = suffix.split('/')
        if len(parts) != 2 or parts[0] not in EXAMPLE_IDS or parts[1] not in _FILES:
            _missing()
        sample, leaf = parts
        mime, maximum = _FILES[leaf]
        data = self._read((sample, leaf), maximum)
        if leaf == 'example.json':
            metadata = _json(data)
            if not isinstance(metadata, dict) or metadata.get('id') != sample:
                _missing()
        return PublicExampleAsset(data, mime, sample + '.step' if leaf == 'source.step' else None)
