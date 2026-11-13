"""Quotation templates use saved facts and retain immutable pricing provenance."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from prep_record import RecordError
from price_catalog import PriceCatalog
from workbench_store import Workbench, WorkbenchError
from workbench_server import make_server


REQUIREMENTS = {'product_name': '测试支架', 'quantity': 10, 'material_family': '碳钢',
                'material_grade': 'Q235B', 'thickness': '3', 'surface_treatment': '镀锌'}
TECHNICAL = {'material_usage_per_piece': '0.4', 'material_rate': '20',
             'process_route': ['折弯'], 'operation_rates': [{'operation': '折弯', 'value': '18', 'unit': 'CNY/piece'}],
             'setup_fee': '0', 'fee_inclusions': ['人工设备耗材已含，准备费不另计。']}
CONTEXT = {'material_form': '板材', 'product_family': '测试支架'}


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='quotation-templates-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.app = Workbench(self.root / 'tasks')
        self.source = self.new('source')

    def new(self, event):
        return self.app.create({'event_id': event, 'inquiry': '离线模拟询价。', 'simulated': True})

    def save(self, task, stage, event, values, **extra):
        return self.app.save_fields(task['id'], stage, {'event_id': event, 'expected_revision': task['revision'],
                                                       'values': copy.deepcopy(values), **extra})

    def ready_source(self):
        self.source = self.save(self.source, 'requirements', 'requirements', REQUIREMENTS)
        self.source = self.save(self.source, 'technical', 'technical', TECHNICAL, pricing_context=CONTEXT)

    def create(self, event='template', **extra):
        return self.app.create_template({'event_id': event, 'name': '支架工艺', 'source_task_id': self.source['id'],
                                         'expected_revision': self.source['revision'], **extra})

    def preview(self, task, template):
        return self.app.template_preview(task['id'], {'template_id': template['id'], 'expected_revision': task['revision']})

    def adopt(self, task, template, *, event='adopt', values=None, **extra):
        return self.save(task, 'technical', event, values if values is not None else template['values'],
                         template_selection={'id': template['id'], 'version': template['version']},
                         pricing_context=CONTEXT, **extra)

    def hashes(self, directory):
        return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.rglob('*') if path.is_file()}

    def catalog_source(self, kind='material'):
        self.app.catalog = PriceCatalog(self.root / 'prices.sqlite3')
        price = {'kind': kind, 'name': '离线模板费率', 'rate': '20' if kind == 'material' else '18',
                 'unit': 'CNY/kg' if kind == 'material' else 'CNY/piece', 'material_grade': 'Q235B',
                 'material_form': '板材', 'thickness_min': '2', 'thickness_max': '4',
                 'valid_from': '2020-01-01', 'valid_to': '2099-12-31', 'currency': 'CNY',
                 'tax_included': False, 'freight_included': False, 'source': '离线回归人工构造',
                 'simulated': True, 'inclusions': ['测试费率未含税运费'], 'qty_min': 1, 'qty_max': 99}
        if kind == 'operation':
            price.update(product_family='测试支架', operation='折弯')
        draft = self.app.catalog.create(price)
        entry = self.app.catalog.publish(draft['entry_id'], 1)
        refs = ({'material_rate': {'entry_id': entry['entry_id'], 'version': 1}} if kind == 'material' else
                {'operations': {'折弯': {'entry_id': entry['entry_id'], 'version': 1}}})
        self.source = self.save(self.source, 'technical', 'catalog-' + kind, TECHNICAL,
                                pricing_context=CONTEXT, price_selections=refs)
        return entry, price, refs

    def test_persistent_immutable_template_and_idempotent_creation(self):
        self.ready_source()
        self.app.issue(self.source['id'], {'event_id': 'before-template', 'expected_revision': self.source['revision'],
                                          'policy': {'pricing_method': 'cost_plus', 'rate_percent': '20',
                                                     'tax_excluded': True, 'freight_excluded': True}})
        original = self.hashes(self.app.records.task_path(self.source['id']))
        entry = self.create()
        self.assertEqual(self.hashes(self.app.records.task_path(self.source['id'])), original)
        self.assertEqual(entry, self.create())
        restarted = Workbench(self.app.root)
        self.assertEqual(restarted.template(entry['id']), entry)
        self.assertEqual(restarted.list_templates(), [entry])
        self.source = self.save(self.source, 'technical', 'later-edit', {'setup_fee': '8'})
        self.assertEqual(restarted.template(entry['id'])['values']['setup_fee'], '0')
        payload = {'event_id': 'template', 'name': '支架工艺', 'source_task_id': self.source['id'], 'expected_revision': 2}
        self.assertEqual(self.app.create_template(payload), entry)
        with self.assertRaises(WorkbenchError) as error:
            self.app.create_template(dict(payload, name='different'))
        self.assertEqual(error.exception.status, 409)

    def test_whitelist_and_material_usage_require_explicit_opt_in(self):
        self.ready_source()
        self.source = self.save(self.source, 'technical', 'advanced',
                                {'inner_bend_radius': '2', 'engineering_open_items': ['工艺尚待验证']})
        entry = self.create()
        self.assertEqual(set(entry['values']), {'process_route', 'operation_rates', 'material_rate', 'setup_fee', 'fee_inclusions'})
        self.assertEqual(entry['values']['setup_fee'], '0')
        self.assertEqual(entry['applicability'], {'material_grade': 'Q235B', 'thickness': '3', 'surface_treatment': '镀锌'})
        included = self.create('usage-opt-in', include_material_usage=True)
        self.assertEqual(included['values']['material_usage_per_piece'], '0.4')
        self.assertTrue(any('材料用量' in text for text in included['warnings']))

    def test_unknown_and_unverified_are_not_free_or_copied(self):
        self.source = self.save(self.source, 'technical', 'partial', {'process_route': ['折弯']})
        with self.assertRaisesRegex(RecordError, '尚无可存'):
            self.create()
        self.source = self.save(self.source, 'technical', 'explicit-zero', {'setup_fee': '0'})
        entry = self.create(include_material_usage=True)
        self.assertEqual(entry['values'], {'setup_fee': '0'})
        self.assertNotIn('material_rate', entry['values'])
        self.assertTrue(entry['warnings'])
        other = self.new('empty')
        with self.assertRaises(RecordError):
            self.app.create_template({'event_id': 'empty-template', 'name': '空', 'source_task_id': other['id'], 'expected_revision': 0})
        # Pending AI candidates remain candidates and are not copied.
        self.source = self.app.upload(self.source['id'], 'image', self.source['revision'], 'image.png', b'\x89PNG\r\n\x1a\nmock')
        self.app._apply_reading(self.source['id'], {'id': 'read-one', 'base_revision': self.source['revision']},
                                {'candidates': [{'name': 'material_rate', 'value': '25', 'raw_value': '25',
                                                 'unit': 'CNY/kg', 'raw_unit': 'CNY/kg',
                                                 'evidence': {'reference': 'FILE-image', 'page': 1, 'region': '说明'}}]})
        self.source = self.app.task(self.source['id'])
        self.assertEqual(self.source['fields']['material_rate']['status'], 'pending_review')
        self.assertNotIn('material_rate', self.create('pending-not-copied')['values'])

    def test_route_and_rates_must_match_and_overlap_is_rejected(self):
        self.ready_source()
        self.source = self.save(self.source, 'technical', 'mismatch',
                                {'operation_rates': [{'operation': '切割', 'value': '2', 'unit': 'CNY/piece'}]})
        with self.assertRaisesRegex(RecordError, '一一对应'):
            self.create()
        self.source = self.save(self.source, 'technical', 'overlap',
                                {'operation_rates': [{'operation': '折弯', 'value': '8', 'unit': 'CNY/order'}], 'setup_fee': '2'})
        with self.assertRaisesRegex(RecordError, '重复计费'):
            self.create('overlap-template')

    def test_preview_is_read_only_warns_conditions_and_changes_no_revision(self):
        self.ready_source()
        entry = self.create()
        target = self.new('target')
        target = self.save(target, 'requirements', 'different-material',
                           dict(REQUIREMENTS, material_grade='Q355B', thickness='6', surface_treatment='喷粉'))
        before = self.hashes(self.app.root)
        result = self.preview(target, entry)
        self.assertEqual(self.hashes(self.app.root), before)
        self.assertEqual(self.app.task(target['id'])['revision'], target['revision'])
        self.assertEqual(len(result['warnings']), 3)
        self.assertTrue(result['can_apply'])
        self.assertNotIn('quantity', result['values'])
        self.assertNotIn('thickness', result['values'])
        self.assertIsNone(self.app.task(target['id'])['fields']['process_route']['value'])
        with self.assertRaises(WorkbenchError):
            self.app.template_preview(target['id'], {'template_id': entry['id'], 'expected_revision': 0})

    def test_save_only_explicit_fields_with_immutable_template_source(self):
        self.ready_source()
        entry = self.create()
        target = self.new('target')
        target = self.adopt(target, entry, values={'process_route': ['折弯']})
        self.assertEqual(target['fields']['process_route']['status'], 'verified')
        for name in ('material_rate', 'operation_rates', 'setup_fee', 'quantity', 'material_usage_per_piece'):
            self.assertIsNone(target['fields'][name]['value'])
        form = json.loads((self.app.root / target['id'] / 'form-adopt.json').read_text())
        self.assertEqual(form['template_snapshot'], entry)
        self.assertEqual(form['submission']['values'], {'process_route': ['折弯']})
        self.assertEqual(target['template_selection']['id'], entry['id'])

    def test_disable_prevents_new_adoption_and_preserves_old_sources(self):
        self.ready_source()
        entry = self.create()
        target = self.new('target')
        target = self.adopt(target, entry)
        form = self.app.root / target['id'] / 'form-adopt.json'
        saved = form.read_bytes()
        disabled = self.app.disable_template(entry['id'], {'event_id': 'disable'})
        self.assertFalse(disabled['enabled'])
        self.assertEqual(self.app.disable_template(entry['id'], {'event_id': 'disable'}), disabled)
        self.assertFalse(self.preview(target, entry)['can_apply'])
        with self.assertRaisesRegex(RecordError, '停用'):
            self.adopt(self.new('second-target'), entry)
        self.assertEqual(form.read_bytes(), saved)
        # A completed original form remains a replay after disable.
        retry = self.adopt(dict(target, revision=0), entry)
        self.assertEqual(retry['revision'], target['revision'])

    def test_interrupted_form_reuses_snapshot_after_disable(self):
        self.ready_source()
        entry = self.create()
        target = self.new('target')
        original = self.app._save_state
        def stop_after_record(task_id, state):
            if state['events'].get('adopt', {}).get('status') == 'done':
                raise OSError('offline interruption')
            return original(task_id, state)
        with patch.object(self.app, '_save_state', side_effect=stop_after_record):
            with self.assertRaises(OSError):
                self.adopt(target, entry)
        saved = (self.app.root / target['id'] / 'form-adopt.json').read_bytes()
        self.app.disable_template(entry['id'], {'event_id': 'disable'})
        retry = self.adopt(target, entry)
        self.assertEqual(retry['revision'], 1)
        self.assertEqual((self.app.root / target['id'] / 'form-adopt.json').read_bytes(), saved)

    def test_catalog_source_is_revalidated_and_not_downgraded_to_manual(self):
        self.ready_source()
        price, payload, refs = self.catalog_source()
        entry = self.create()
        self.assertEqual(entry['catalog_sources']['material_rate']['entry_id'], price['entry_id'])
        target = self.new('target')
        target = self.save(target, 'requirements', 'target-requirements', REQUIREMENTS)
        preview = self.preview(target, entry)
        self.assertTrue(preview['can_apply'])
        self.assertEqual(preview['price_selections'], refs)
        # Even an omitted frontend reference is restored for a same-value price.
        target = self.adopt(target, entry, price_selections={})
        self.assertEqual(target['price_selections']['material_rate']['entry_id'], price['entry_id'])
        newer = self.app.catalog.new_version(price['entry_id'], dict(payload, rate='40'))
        self.app.catalog.publish(newer['entry_id'], newer['version'])
        second = self.new('second-target')
        second = self.save(second, 'requirements', 'second-requirements', REQUIREMENTS)
        self.assertFalse(self.preview(second, entry)['can_apply'])
        with self.assertRaisesRegex(RecordError, '不再适用'):
            self.adopt(second, entry, price_selections={})
        self.assertIsNone(self.app.task(second['id'])['fields']['material_rate']['value'])
        changed = copy.deepcopy(entry['values'])
        changed['material_rate'] = '25'
        second = self.adopt(second, entry, event='explicit-manual-edit', values=changed, price_selections={})
        self.assertEqual(second['fields']['material_rate']['value'], '25')
        self.assertNotIn('material_rate', second['price_selections'])
        form = json.loads((self.app.root / second['id'] / 'form-explicit-manual-edit.json').read_text())
        self.assertEqual(form['template_snapshot']['catalog_sources']['material_rate']['rate'], '20')

    def test_operation_catalog_source_cannot_be_dropped_for_same_rate(self):
        self.ready_source()
        price, payload, refs = self.catalog_source('operation')
        entry = self.create()
        target = self.new('target')
        target = self.save(target, 'requirements', 'target-requirements', REQUIREMENTS)
        self.app.catalog.disable(price['entry_id'], 1)
        self.assertFalse(self.preview(target, entry)['can_apply'])
        with self.assertRaises(RecordError):
            self.adopt(target, entry, price_selections={})
        changed = copy.deepcopy(entry['values'])
        changed['operation_rates'][0]['value'] = '19'
        result = self.adopt(target, entry, event='different-rate', values=changed, price_selections={})
        self.assertEqual(result['fields']['operation_rates']['value'][0]['value'], '19')
        self.assertNotIn('operations:折弯', result['price_selections'])

    def test_invalid_conditions_prevent_catalog_template_use(self):
        self.ready_source()
        self.catalog_source()
        entry = self.create()
        target = self.new('target')
        missing = self.preview(target, entry)
        self.assertFalse(missing['can_apply'])
        self.assertTrue(any('价格库' in warning for warning in missing['warnings']))
        target = self.save(target, 'requirements', 'mismatch-target', dict(REQUIREMENTS, quantity=1000))
        self.assertFalse(self.preview(target, entry)['can_apply'])

    def test_explicit_new_catalog_reference_can_replace_stale_template_price(self):
        self.ready_source()
        old, payload, refs = self.catalog_source()
        entry = self.create()
        newer = self.app.catalog.new_version(old['entry_id'], payload)
        published = self.app.catalog.publish(newer['entry_id'], newer['version'])
        target = self.new('target')
        target = self.save(target, 'requirements', 'target-requirements', REQUIREMENTS)
        self.assertFalse(self.preview(target, entry)['can_apply'])
        invalid = {'material_rate': {'entry_id': old['entry_id'], 'version': 99}}
        with self.assertRaises(RecordError):
            self.adopt(target, entry, event='invalid-new-price', price_selections=invalid)
        valid = {'material_rate': {'entry_id': published['entry_id'], 'version': published['version']}}
        target = self.adopt(target, entry, event='explicit-new-price', price_selections=valid)
        self.assertEqual(target['price_selections']['material_rate']['version'], published['version'])
        form = json.loads((self.app.root / target['id'] / 'form-explicit-new-price.json').read_text())
        self.assertEqual(form['template_snapshot']['catalog_sources']['material_rate']['version'], 1)
        self.assertEqual(form['adopted_prices']['material_rate']['version'], published['version'])

    def test_incomplete_legacy_route_group_cannot_be_previewed_as_applicable(self):
        self.ready_source()
        entry = self.create()
        path = self.app.meta / 'templates.json'
        saved = json.loads(path.read_text())
        saved['templates'][entry['id']]['values'].pop('operation_rates')
        path.write_text(json.dumps(saved))
        result = self.preview(self.source, entry)
        self.assertFalse(result['can_apply'])
        self.assertTrue(any('未成组' in text for text in result['warnings']))

    def test_symlink_metadata_and_invalid_selection_are_rejected(self):
        self.ready_source()
        other = self.root / 'other.json'
        other.write_text('{}')
        path = self.app.meta / 'templates.json'
        path.symlink_to(other)
        with self.assertRaisesRegex(RecordError, 'symlink'):
            self.app.list_templates()
        with self.assertRaisesRegex(RecordError, 'symlink'):
            self.create()
        self.assertEqual(other.read_text(), '{}')
        path.unlink()
        entry = self.create()
        with self.assertRaises(RecordError):
            self.save(self.source, 'requirements', 'wrong-stage', {'quantity': 20},
                      template_selection={'id': entry['id'], 'version': 1})

    def test_reserved_source_name_is_rejected_before_nested_lock(self):
        with self.assertRaisesRegex(RecordError, '保留名称'):
            self.app.create_template({'event_id': 'reserved', 'name': '保留',
                                       'source_task_id': 'templates', 'expected_revision': 0})

    def test_http_template_routes_and_shared_origin_boundary(self):
        self.ready_source()
        server = make_server(self.app, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(path, payload=None, marker=True):
            data = json.dumps(payload).encode() if payload is not None else None
            headers = {'Content-Type': 'application/json'}
            if marker:
                headers['X-Workbench-Request'] = '1'
            req = Request('http://127.0.0.1:' + str(server.server_port) + '/workbench/api' + path, data=data, headers=headers)
            try:
                response = urlopen(req, timeout=5)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status, json.loads(response.read())
        payload = {'event_id': 'http-template', 'name': 'HTTP模板', 'source_task_id': self.source['id'],
                   'expected_revision': self.source['revision']}
        self.assertEqual(request('/templates', payload, marker=False)[0], 403)
        status, entry = request('/templates', payload)
        self.assertEqual(status, 201)
        self.assertEqual(request('/templates')[1]['templates'], [entry])
        self.assertEqual(request('/templates/' + entry['id'])[1], entry)
        preview = {'template_id': entry['id'], 'expected_revision': self.source['revision']}
        self.assertEqual(request('/tasks/' + self.source['id'] + '/template-preview', preview)[0], 200)
        self.assertFalse(request('/templates/' + entry['id'] + '/disable', {'event_id': 'http-disable'})[1]['enabled'])


if __name__ == '__main__':
    unittest.main()
