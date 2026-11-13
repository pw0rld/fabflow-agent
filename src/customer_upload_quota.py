"""Durable global customer-upload allowance; callers hold the portal flock.

Existing orders are not charged. A reservation is persisted before creating a
new order and counts immediately, so interrupted work can only recover using
the same owner/event/signature. Ambiguous storage failures never reopen slots.
"""
import copy
import hashlib
import json
from pathlib import Path
import re
import secrets

from prep_record import atomic_write, stamp
from workbench_store import WorkbenchError

LIMIT = 10
MAX_BYTES = 20 * 1024 * 1024
RETRY_LIMIT = 1
VERSION = 'customer-step-upload-quota-v1'
QUOTA_FILE = 'step-upload-quota.json'
ACTIVATION_FILE = 'step-upload-quota-activation.json'
HEX = re.compile(r'[a-f0-9]{64}\Z')
EVENT = re.compile(r'customer-[a-f0-9]{64}\Z')
STEP_MARKER = re.compile(r"/\*|'")


def unavailable():
    return WorkbenchError('上传额度记录暂不可用，请联系工厂核对；现有订单仍可查看。',
                          'customer_upload_quota_unavailable', 503)


def _step_tokens(text):
    """Skip comments/quoted strings in one forward pass, including escapes."""
    cursor, segments = 0, []
    while cursor < len(text):
        marker = STEP_MARKER.search(text, cursor)
        if marker is None:
            segments.append(text[cursor:])
            break
        segments.append(text[cursor:marker.start()])
        if marker.group() == '/*':
            end = text.find('*/', marker.end())
            if end < 0:
                raise WorkbenchError('STEP注释未完整结束。', 'invalid_step', 400)
            cursor = end+2
        else:
            cursor = marker.end()
            while True:
                end = text.find("'", cursor)
                if end < 0:
                    raise WorkbenchError('STEP文字标注未完整结束。', 'invalid_step', 400)
                cursor = end+1
                if cursor < len(text) and text[cursor] == "'":
                    cursor += 1
                else:
                    break
        segments.append(' ')
    return ''.join(segments)


def validate_step(name, content):
    if (not isinstance(name, str) or not 0 < len(name) <= 240 or name != Path(name).name
            or any(char in name for char in ('/', '\\', ':'))
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or Path(name).suffix.lower() not in {'.step', '.stp'}):
        raise WorkbenchError('客户入口每次只接收一份 STEP/STP 原文件，不接收 ZIP、RAR、图片或其他格式。',
                             'customer_step_only', 415)
    if not isinstance(content, bytes) or not content:
        raise WorkbenchError('请选择非空的 STEP/STP 原文件。', 'invalid_step', 400)
    if len(content) > MAX_BYTES:
        raise WorkbenchError('单份 STEP/STP 文件不能超过20 MB。', 'too_large', 413)
    text = content.decode('latin-1')
    if text.startswith('\xef\xbb\xbf'):
        text = text[3:]
    if any(ord(char) < 32 and char not in '\t\r\n' for char in text):
        raise WorkbenchError('文件不是可读取的 STEP 文本，请上传原始 STEP/STP 文件。', 'invalid_step', 400)
    # Ignore legal comments/string literals before checking physical-file
    # delimiters; their text is never interpreted as geometry or instructions.
    clean = _step_tokens(text)
    start = re.match(r'\s*ISO-10303-21\s*;\s*HEADER\s*;', clean, re.I)
    data = re.search(r'\bDATA\b', clean, re.I)
    data_end = clean.find(';', data.end()) if data else -1
    data_header_valid = (data is not None and data_end >= 0
                         and re.fullmatch(r'\s*(?:\([^;]*\))?\s*', clean[data.end():data_end]) is not None)
    end = re.search(r'\bEND-ISO-10303-21\s*;\s*\Z', clean, re.I)
    if (not start or not data_header_valid or not end or data.start() <= start.end()
            or "'" in clean or '/*' in clean or '*/' in clean
            or len(re.findall(r'(?<![A-Za-z0-9_-])ISO-10303-21\s*;', clean, re.I)) != 1
            or len(re.findall(r'\bEND-ISO-10303-21\s*;', clean, re.I)) != 1
            or not re.search(r'\bENDSEC\s*;', clean[start.end():data.start()], re.I)
            or not re.search(r'\bENDSEC\s*;', clean[data_end+1:end.start()], re.I)):
        raise WorkbenchError('文件内容不是一份完整的 STEP；不接收伪装格式、拼接文件或压缩包。', 'invalid_step', 400)
    return hashlib.sha256(content).hexdigest()


class CustomerUploadQuota:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / QUOTA_FILE
        self.marker = self.root / ACTIVATION_FILE

    def load(self):
        """Read an atomic snapshot without creating a lock, marker or ledger."""
        try:
            if not self.path.exists():
                if self.path.is_symlink() or self.marker.exists() or self.marker.is_symlink():
                    raise unavailable()
                return None
            if (self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size > 65536
                    or self.marker.is_symlink() or not self.marker.is_file() or self.marker.stat().st_size > 4096):
                raise unavailable()
            value = json.loads(self.path.read_text(encoding='utf-8'))
            marker = json.loads(self.marker.read_text(encoding='utf-8'))
            if (not isinstance(value, dict) or value.get('version') != VERSION or value.get('limit') != LIMIT
                    or not isinstance(value.get('epoch'), str) or not HEX.fullmatch(value['epoch'])
                    or marker != {'version': VERSION, 'epoch': value['epoch'], 'limit': LIMIT}
                    or not isinstance(value.get('events'), dict) or len(value['events']) > LIMIT):
                raise unavailable()
            for event, row in value['events'].items():
                if (not EVENT.fullmatch(event) or not isinstance(row, dict)
                        or row.get('status') not in {'reserved', 'accepted'}
                        or not isinstance(row.get('owner'), str) or not HEX.fullmatch(row['owner'])
                        or row.get('batch_id') != 'BATCH-' + hashlib.sha256(event.encode()).hexdigest()[:32]
                        or not isinstance(row.get('signature'), str) or not HEX.fullmatch(row['signature'])
                        or not isinstance(row.get('source_sha256'), str) or not HEX.fullmatch(row['source_sha256'])
                        or type(row.get('size')) is not int or not 0 < row['size'] <= MAX_BYTES
                        or not isinstance(row.get('name'), str) or not isinstance(row.get('reserved_at'), str)):
                    raise unavailable()
            return value
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise unavailable() from error

    def status(self):
        ledger = self.load()
        records = list(ledger['events'].values()) if ledger else []
        used = len(records)
        return {'limit': LIMIT, 'used': used, 'remaining': LIMIT-used, 'scope': 'global',
                'accepted_extensions': ['.step', '.stp'], 'max_files': 1, 'max_bytes': MAX_BYTES,
                'retry_limit': RETRY_LIMIT, 'active': ledger is not None, 'includes_existing_orders': False,
                'unit': 'new_upload', 'blocked': used >= LIMIT,
                'accepted_count': sum(row['status'] == 'accepted' for row in records),
                'reserved_count': sum(row['status'] == 'reserved' for row in records)}

    def save(self, ledger):
        try:
            if not self.marker.exists():
                if self.marker.is_symlink() or self.path.exists() or self.path.is_symlink():
                    raise unavailable()
                # Arm the durable fail-closed marker first. A crash between
                # these writes requires administrator recovery, never reset.
                atomic_write(self.marker, json.dumps({'version': VERSION, 'epoch': ledger['epoch'], 'limit': LIMIT}))
            if self.path.is_symlink():
                raise unavailable()
            atomic_write(self.path, json.dumps(ledger, ensure_ascii=False, allow_nan=False, indent=2) + '\n')
        except (OSError, ValueError, TypeError) as error:
            raise unavailable() from error

    def reserve(self, event, owner, batch_id, name, content, source_sha, signature):
        ledger = self.load()
        if ledger is None:
            ledger = {'version': VERSION, 'limit': LIMIT, 'epoch': secrets.token_hex(32),
                      'activated_at': stamp(), 'events': {}}
        previous = ledger['events'].get(event)
        if previous is not None:
            if previous['owner'] != owner or previous['signature'] != signature or previous['batch_id'] != batch_id:
                raise WorkbenchError('同一上传请求对应的文件已变化，请使用原文件重试。', 'event_conflict', 409)
            return ledger, copy.deepcopy(previous)
        if len(ledger['events']) >= LIMIT:
            raise WorkbenchError('所有客户合计10次新上传额度已用完；现有订单和报价仍可查看，请联系工厂。',
                                 'customer_upload_quota_exhausted', 429)
        row = {'owner': owner, 'batch_id': batch_id, 'name': name, 'size': len(content),
               'source_sha256': source_sha, 'signature': signature, 'status': 'reserved', 'reserved_at': stamp()}
        ledger['events'][event] = row
        self.save(ledger)
        return ledger, copy.deepcopy(row)

    def accept(self, ledger, event):
        if ledger['events'][event]['status'] != 'accepted':
            ledger['events'][event].update(status='accepted', accepted_at=stamp())
            self.save(ledger)

    def release_validation_failure(self, ledger, event):
        """Only after a synchronous known validation failure and no order."""
        if ledger['events'][event]['status'] == 'reserved':
            del ledger['events'][event]
            self.save(ledger)
