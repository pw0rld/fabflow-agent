"""Immutable quotation parameter snapshots; callers serialize writes with a lock."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from prep_record import RecordError, atomic_write, digest, identifier, require, stamp


class TemplateConflict(RecordError):
    pass


class TemplateStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'templates.json'

    def _read(self):
        require(not self.directory.is_symlink() and not self.path.is_symlink(), 'symlink template metadata prohibited')
        if not self.path.exists():
            return {'schema_version': 1, 'templates': {}, 'events': {}}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        require(data.get('schema_version') == 1 and isinstance(data.get('templates'), dict)
                and isinstance(data.get('events'), dict), 'invalid template metadata')
        return data

    def _write(self, data):
        require(not self.directory.is_symlink() and not self.path.is_symlink(), 'symlink template metadata prohibited')
        atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n')

    def list(self):
        return copy.deepcopy(sorted(self._read()['templates'].values(), key=lambda item: item['created_at'], reverse=True))

    def get(self, template_id):
        result = self._read()['templates'].get(identifier(template_id))
        if result is None:
            raise FileNotFoundError()
        return copy.deepcopy(result)

    @staticmethod
    def replay(data, event_id, action, payload):
        old = data['events'].get(identifier(event_id))
        if not old:
            return None
        if old['signature'] != digest({'action': action, 'payload': payload}):
            raise TemplateConflict('同一次模板提交的内容发生变化，请重新提交。')
        return copy.deepcopy(old['result'])

    def created(self, payload, snapshot):
        data = self._read()
        event_id = payload['event_id']
        replay = self.replay(data, event_id, 'create', payload)
        if replay is not None:
            return replay
        template_id = 'TPL-' + hashlib.sha256(event_id.encode()).hexdigest()[:20]
        require(template_id not in data['templates'], 'template identifier collision')
        entry = {**copy.deepcopy(snapshot), 'id': template_id, 'version': 1,
                 'enabled': True, 'created_at': stamp()}
        data['templates'][template_id] = entry
        data['events'][event_id] = {'signature': digest({'action': 'create', 'payload': payload}),
                                   'result': copy.deepcopy(entry), 'at': stamp()}
        self._write(data)
        return copy.deepcopy(entry)

    def disable(self, template_id, payload):
        template_id = identifier(template_id)
        data = self._read()
        signed = {'template_id': template_id, **payload}
        replay = self.replay(data, payload['event_id'], 'disable', signed)
        if replay is not None:
            return replay
        if template_id not in data['templates']:
            raise FileNotFoundError()
        entry = data['templates'][template_id]
        if entry['enabled']:
            entry['enabled'] = False
            entry['disabled_at'] = stamp()
        data['events'][payload['event_id']] = {'signature': digest({'action': 'disable', 'payload': signed}),
                                              'result': copy.deepcopy(entry), 'at': stamp()}
        self._write(data)
        return copy.deepcopy(entry)
