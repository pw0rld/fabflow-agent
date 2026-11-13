"""Order facade integration using real stores/calculator and offline bridge doubles."""
import copy
import csv
import io
import hashlib
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from order_service import OrderService
from price_catalog import PriceCatalog
from prep_record import RecordError
from workbench_store import Workbench, WorkbenchError

POLICY = {'pricing_method': 'cost_plus', 'rate_percent': '20', 'tax_excluded': True, 'freight_excluded': True}
DXF = b'0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n'


def part(key, mode='make', **extra):
    return {'id': key, 'name': key, 'part_number': key, 'version': 'A', 'mode': mode,
            'document_ids': [], **extra}


def route(*names, status='confirmed'):
    return [{'id': 'S' + str(index), 'operation': name, 'status': status, 'reason': '本地人工构造核对'}
            for index, name in enumerate(names)]


class OfflineDiscovery:
    def __init__(self, parts):
        self.parts, self.starts, self.inspections = copy.deepcopy(parts), 0, 0

    def start(self, job_id, files, inquiry, output, *, on_session):
        self.starts += 1
        on_session('session-offline')
        return {'status': 'running', 'session_id': 'session-offline'}

    def inspect(self, session):
        self.inspections += 1
        return {'status': 'completed', 'parts': copy.deepcopy(self.parts), 'warnings': []}


class OrderServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='order-service-offline-')
        self.addCleanup(self.tmp.cleanup)
        self.app = Workbench(Path(self.tmp.name) / 'tasks')
        self.service = OrderService(self.app, poll_seconds=0)
        self.addCleanup(self.service.close)
        self.order = self.service.create({'event_id': 'create', 'name': '离线多文档订单', 'inquiry': '本地人工构造，不是客户业务数据。', 'simulated': True})

    def upload(self, event='file', name='part.dxf', content=DXF):
        self.order = self.service.upload(self.order['id'], event, self.order['revision'], name, content)
        return self.order['documents'][-1]

    def replace(self, **structure):
        self.order = self.service.replace(self.order['id'], {'event_id': 'structure-' + str(self.order['revision']),
                                                           'expected_revision': self.order['revision'], **copy.deepcopy(structure)})
        return self.order

    def prepare(self):
        self.order = self.service.prepare(self.order['id'], {'event_id': 'prepare-' + str(self.order['revision']),
                                                            'expected_revision': self.order['revision']})
        return self.order

    def make_order(self, quantity=10):
        document = self.upload()
        self.replace(parts=[part('MAKE', document_ids=[document['id']])],
                     lines=[{'id': 'L1', 'part_id': 'MAKE', 'quantity': quantity, 'unit': 'piece'}],
                     routes={'MAKE': route('切割', '折弯')})
        self.prepare()
        return self.app.task(self.order['parts'][0]['task_id'])

    def ready(self, task):
        requirements = {'material_family': '碳钢', 'material_grade': 'Q235B', 'thickness': '3', 'surface_treatment': '不处理'}
        technical = {'material_usage_per_piece': '0.4', 'material_rate': '20',
                     'operation_rates': [{'operation': '切割', 'value': '12', 'unit': 'CNY/piece'},
                                         {'operation': '折弯', 'value': '18', 'unit': 'CNY/piece'}],
                     'setup_fee': '200', 'fee_inclusions': ['工序含人工设备耗材，准备费另计。']}
        for stage, values in [('requirements', requirements), ('technical', technical)]:
            task = self.app.save_fields(task['id'], stage, {'event_id': stage + '-' + str(task['revision']),
                                                           'expected_revision': task['revision'], 'values': values})
        self.assertTrue(task['validation']['ready'])
        return task

    def issue(self, event='quote', fees=None):
        self.order = self.service.get(self.order['id'])
        payload = {'event_id': event, 'expected_revision': self.order['revision'], 'policy': POLICY, 'shared_fees': fees or []}
        # A lock regression must fail this check instead of hanging the suite.
        previous = signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('order quotation lock exceeded 5s')))
        signal.setitimer(signal.ITIMER_REAL, 5)
        try:
            return self.service.issue(self.order['id'], payload)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def test_real_make_prepare_manual_parameters_and_complete_order_quote(self):
        task = self.make_order()
        self.assertEqual(task['fields']['product_name']['value'], 'MAKE')
        self.assertEqual(task['fields']['quantity']['value'], 10)
        self.assertEqual(task['fields']['process_route']['value'], ['切割', '折弯'])
        self.assertEqual(len(task['attachments']), 1)
        self.assertIsNone(task['fields']['material_usage_per_piece']['value'])
        self.ready(task)
        quote = self.issue()
        self.assertEqual(quote['amounts']['total_cost'], '580.00')
        self.assertEqual(quote['amounts']['total_price'], '696.00')
        self.assertTrue(quote['source_current'])
        self.assertTrue(quote['links']['quote_url'].endswith('/quote.html'))
        content, mime, download = self.service.document(self.order['id'], quote['quote_id'], 'quote.md')
        self.assertIn('696.00', content.decode())
        self.assertIn('markdown', mime)
        self.assertTrue(download)

    def test_prepare_event_replay_and_quantity_change_preserve_user_technical_fields(self):
        task = self.ready(self.make_order())
        original_id, original_usage = task['id'], task['fields']['material_usage_per_piece']['value']
        saved = self.service.store.get(self.order['id'])
        # The first prepare event used the previous order revision.
        replay = {'event_id': 'prepare-' + str(saved['revision'] - 1), 'expected_revision': saved['revision'] - 1}
        self.service.prepare(self.order['id'], replay)
        self.assertEqual(len(self.app.tasks()), 1)
        self.order = self.service.get(self.order['id'])
        self.replace(lines=[{'id': 'L1', 'part_id': 'MAKE', 'quantity': 20, 'unit': 'piece'}])
        self.prepare()
        new_task = self.app.task(self.order['parts'][0]['task_id'])
        self.assertEqual(new_task['id'], original_id)
        self.assertEqual(new_task['fields']['quantity']['value'], 20)
        self.assertEqual(new_task['fields']['material_usage_per_piece']['value'], original_usage)
        quote = self.issue('changed-quantity')
        self.assertEqual(quote['amounts']['total_cost'], '960.00')
        self.assertEqual(quote['amounts']['total_price'], '1152.00')

    def test_unknown_quantities_routes_and_missing_cost_block_without_quotes(self):
        doc = self.upload()
        self.replace(parts=[part('MAKE', document_ids=[doc['id']])],
                     lines=[{'id': 'L1', 'part_id': 'MAKE', 'quantity': None, 'unit': 'piece'}],
                     routes={'MAKE': route('切割', status='pending_review')})
        with self.assertRaisesRegex(RecordError, '未知'):
            self.prepare()
        self.replace(lines=[{'id': 'L1', 'part_id': 'MAKE', 'quantity': 1, 'unit': 'piece'}])
        with self.assertRaisesRegex(RecordError, '尚未确认'):
            self.prepare()
        self.replace(routes={'MAKE': route('切割')})
        self.prepare()
        with self.assertRaisesRegex(RecordError, '核对完整'):
            self.issue('not-ready')
        self.assertEqual(self.service.store.get(self.order['id'])['quotes'], [])

    def test_structure_rejects_foreign_task_and_preserves_prepared_link(self):
        foreign = self.app.create({'event_id': 'foreign', 'inquiry': '另一任务', 'simulated': True})
        with self.assertRaisesRegex(RecordError, '不能将其他任务'):
            self.replace(parts=[part('MAKE', task_id=foreign['id'])])
        task = self.make_order()
        edited = copy.deepcopy(self.order['parts'])
        edited[0]['task_id'] = foreign['id']
        with self.assertRaisesRegex(RecordError, '不能将其他任务'):
            self.replace(parts=edited)
        edited[0].pop('task_id')
        edited[0].pop('prepared_key', None)
        self.replace(parts=edited)
        self.assertEqual(self.order['parts'][0]['task_id'], task['id'])

    def test_upload_hash_duplicate_provenance_and_cross_order_reads(self):
        doc = self.upload()
        self.assertEqual(doc['sha256'], hashlib.sha256(DXF).hexdigest())
        self.assertEqual(self.service.attachment(self.order['id'], doc['id'])[0], DXF)
        duplicate = self.upload('duplicate', 'same-content.dxf')
        self.assertEqual(duplicate['duplicate_of'], doc['id'])
        other = self.service.create({'event_id': 'other', 'name': '另一订单', 'inquiry': '', 'simulated': True})
        with self.assertRaises(FileNotFoundError):
            self.service.attachment(other['id'], doc['id'])
        _, path = self.service._file(self.order['id'], doc['id'])
        path.write_bytes(DXF + b'changed')
        with self.assertRaisesRegex(RecordError, '校验不一致'):
            self.service.attachment(self.order['id'], doc['id'])

    def test_upload_retry_and_structure_event_conflict_do_not_duplicate(self):
        revision = self.order['revision']
        self.upload()
        repeated = self.service.upload(self.order['id'], 'file', revision, 'part.dxf', DXF)
        self.assertEqual(len(repeated['documents']), 1)
        self.assertEqual(repeated['revision'], self.order['revision'])
        payload = {'event_id': 'structure-once', 'expected_revision': repeated['revision'], 'parts': [part('A', 'buy')]}
        self.service.replace(self.order['id'], payload)
        self.service.replace(self.order['id'], payload)
        with self.assertRaises(WorkbenchError) as error:
            self.service.replace(self.order['id'], {**payload, 'parts': [part('B', 'buy')]})
        self.assertEqual(error.exception.code, 'event_conflict')

    def test_actual_multipage_pdf_only_selected_page_enters_child_task(self):
        from PIL import Image
        red, blue = Image.new('RGB', (100, 80), (255, 0, 0)), Image.new('RGB', (100, 80), (0, 0, 255))
        buffer = io.BytesIO()
        red.save(buffer, format='PDF', save_all=True, append_images=[blue])
        red.close()
        blue.close()
        doc = self.upload(name='two-parts.pdf', content=buffer.getvalue())
        self.replace(parts=[part('SECOND', document_ids=[doc['id']],
                                 source_refs=[{'document_id': doc['id'], 'page': 2, 'region': '整页'}])],
                     lines=[{'id': 'L', 'part_id': 'SECOND', 'quantity': 1, 'unit': 'piece'}],
                     routes={'SECOND': route('切割')})
        self.prepare()
        task = self.app.task(self.order['parts'][0]['task_id'])
        self.assertEqual(len(task['attachments']), 1)
        selected = task['attachments'][0]
        self.assertIn('第2页', selected['name'])
        content, mime, _ = self.app.attachment(task['id'], selected['id'])
        self.assertEqual(mime, 'image/png')
        with Image.open(io.BytesIO(content)) as actual:
            r, g, b = actual.convert('RGB').getpixel((actual.width // 2, actual.height // 2))
        self.assertLess(r, 20)
        self.assertLess(g, 20)
        self.assertGreater(b, 230)

    def test_changed_part_version_requires_prepare_before_reusing_ready_task(self):
        self.ready(self.make_order())
        changed = copy.deepcopy(self.order['parts'])
        changed[0]['version'] = 'B'
        self.replace(parts=changed)
        self.assertFalse(self.order['part_status']['MAKE']['ready'])
        with self.assertRaisesRegex(RecordError, '版本已变化'):
            self.issue('wrong-version')
        self.assertEqual(self.service.store.get(self.order['id'])['quotes'], [])

    def test_catalog_adopted_price_source_and_new_version_block_new_order_quote(self):
        self.app.catalog = PriceCatalog(Path(self.tmp.name) / 'catalog.sqlite3')
        task = self.ready(self.make_order())
        price = {'kind': 'material', 'name': '整单测试材料价', 'rate': '20', 'unit': 'CNY/kg',
                 'material_grade': 'Q235B', 'material_form': '板材', 'thickness_min': '2', 'thickness_max': '4',
                 'qty_min': 1, 'qty_max': 99, 'valid_from': '2020-01-01', 'valid_to': '2099-12-31',
                 'currency': 'CNY', 'tax_included': False, 'freight_included': False, 'simulated': True,
                 'source': '本地离线人工构造采购表', 'inclusions': ['材料价未计税费和运费']}
        draft = self.app.catalog.create(price)
        published = self.app.catalog.publish(draft['entry_id'], 1)
        task = self.app.save_fields(task['id'], 'technical', {'event_id': 'adopt-catalog', 'expected_revision': task['revision'],
            'values': {'material_rate': '20'}, 'pricing_context': {'material_form': '板材', 'product_family': 'MAKE'},
            'price_selections': {'material_rate': {'entry_id': published['entry_id'], 'version': 1}}})
        self.assertTrue(task['validation']['ready'])
        adopted = self.app._state(task['id'])['price_selections']['material_rate']
        self.assertEqual(adopted['entry_id'], published['entry_id'])
        self.assertEqual(adopted['version'], 1)
        quote = self.issue('catalog-quote')
        self.assertEqual(quote['amounts']['total_cost'], '580.00')
        frozen_price = quote['part_costs'][0]['single_part_calculation']['adopted_price_sources']['material_rate']
        self.assertEqual(frozen_price['entry_id'], published['entry_id'])
        self.assertEqual(frozen_price['version'], 1)
        self.assertEqual(frozen_price['rate'], '20')
        self.assertEqual(frozen_price['source'], price['source'])
        saved = copy.deepcopy(self.service.store.get(self.order['id'])['quotes'][0])
        record = saved['record_snapshots']['MAKE']
        source = record['sources']['FORM-adopt-catalog']
        file = self.app.records.task_path(task['id']) / source['path']
        self.assertEqual(hashlib.sha256(file.read_bytes()).hexdigest(), source['sha256'])
        original_form = file.read_bytes()
        self.assertEqual(json.loads(original_form)['adopted_prices']['material_rate']['entry_id'], published['entry_id'])
        newer = self.app.catalog.new_version(published['entry_id'], dict(price, rate='40'))
        self.app.catalog.publish(newer['entry_id'], newer['version'])
        self.assertFalse(self.app.task(task['id'])['validation']['ready'])
        with self.assertRaisesRegex(RecordError, '采用价格尚未核对完整'):
            self.issue('blocked-new-catalog')
        self.assertEqual(self.service.store.get(self.order['id'])['quotes'], [saved])
        self.assertEqual(file.read_bytes(), original_form)
        historical = self.service.quote(self.order['id'], quote['quote_id'])
        self.assertEqual(historical['amounts']['total_price'], '696.00')
        self.assertEqual(historical['part_costs'][0]['single_part_calculation']['adopted_price_sources']['material_rate'], frozen_price)

    def test_quote_retry_is_idempotent_old_snapshot_immutable_and_staleness_visible(self):
        task = self.ready(self.make_order())
        payload = {'event_id': 'one-quote', 'expected_revision': self.service.get(self.order['id'])['revision'],
                   'policy': POLICY, 'shared_fees': []}
        first = self.issue('one-quote')
        raw = copy.deepcopy(self.service.store.get(self.order['id'])['quotes'][0])
        replay = self.service.issue(self.order['id'], payload)
        self.assertEqual(first['quote_id'], replay['quote_id'])
        self.assertEqual(len(self.service.store.get(self.order['id'])['quotes']), 1)
        task = self.app.save_fields(task['id'], 'technical', {'event_id': 'new-rate', 'expected_revision': task['revision'],
                                                            'values': {'material_rate': '30'}})
        old = self.service.quote(self.order['id'], first['quote_id'])
        self.assertFalse(old['source_current'])
        self.assertEqual(self.service.store.get(self.order['id'])['quotes'][0], raw)
        projected = self.service.get(self.order['id'])
        self.assertFalse(projected['quotes'][0]['source_current'])
        self.assertNotIn('record_snapshots', projected['quotes'][0])
        second = self.issue('new-quote')
        self.assertNotEqual(second['quote_id'], first['quote_id'])
        self.assertEqual(second['amounts']['total_cost'], '620.00')

    def test_changed_bom_marks_history_stale_and_exports_keep_original_quantity(self):
        self.replace(parts=[part('BUY', 'buy', purchase_price='5', cost_confirmed=True, cost_inclusions='本地采购来源，独立件。')],
                     lines=[{'id': 'L', 'part_id': 'BUY', 'quantity': 10, 'unit': 'piece'}])
        first = self.issue()
        self.order = self.service.get(self.order['id'])
        self.replace(lines=[{'id': 'L', 'part_id': 'BUY', 'quantity': 20, 'unit': 'piece'}])
        history = self.service.quote(self.order['id'], first['quote_id'])
        self.assertFalse(history['source_current'])
        bom, _, _ = self.service.document(self.order['id'], first['quote_id'], 'bom.csv')
        rows = list(csv.DictReader(io.StringIO(bom.decode('utf-8-sig'))))
        self.assertEqual(rows[0]['总需求量'], '10')
        self.assertEqual(rows[0]['需求单位'], '件')

    def test_late_discovery_is_candidate_only_and_never_overwrites_manual_parts(self):
        doc = self.upload()
        proposed = [{'key': 'ai-part', 'name': 'AI候选', 'part_number': None, 'version': None,
                     'document_ids': [doc['id']], 'evidence': [{'reference': doc['id'], 'page': 1, 'region': '标题栏'}],
                     'quantity': None, 'quantity_source': None, 'suggested_route': []}]
        bridge = OfflineDiscovery(proposed)
        self.service.discovery_factory = lambda workspace: bridge
        self.order = self.service.discover(self.order['id'], {'event_id': 'discover', 'expected_revision': self.order['revision']})
        self.replace(parts=[part('MANUAL', 'buy', purchase_price='8', cost_confirmed=True, cost_inclusions='本地人工确认')],
                     lines=[{'id': 'L', 'part_id': 'MANUAL', 'quantity': 3, 'unit': 'piece'}])
        before = copy.deepcopy(self.service.store.get(self.order['id']))
        job = self.service.queue.claim('offline-worker', lease_seconds=120)
        document, path = self.service._file(self.order['id'], doc['id'])
        views = {'views': [{'id': doc['id'], 'path': str(path), 'original_name': document['name'], 'sha256': document['sha256']}], 'warnings': []}
        with patch.object(self.service, '_views', return_value=views):
            self.service._execute(job)
        after = self.service.store.get(self.order['id'])
        self.assertEqual(after, before)
        self.assertEqual(self.service.queue.get(job['id'])['status'], 'completed')
        view = self.service.get(self.order['id'])
        self.assertEqual(view['parts'][0]['name'], 'MANUAL')
        self.assertEqual(view['discovery']['parts'][0]['name'], 'AI候选')
        self.assertEqual(bridge.starts, 1)
        self.assertEqual(bridge.inspections, 1)

    def test_csv_exports_neutralize_formula_cells(self):
        self.replace(parts=[part('BUY', 'buy', name='=CMD()', part_number='+123', purchase_price='1',
                                 cost_confirmed=True, cost_inclusions='离线来源')],
                     lines=[{'id': 'L', 'part_id': 'BUY', 'quantity': 1, 'unit': 'piece'}])
        result = self.issue()
        content, _, _ = self.service.document(self.order['id'], result['quote_id'], 'bom.csv')
        text = content.decode('utf-8-sig')
        self.assertIn("'=CMD()", text)
        self.assertIn("'+123", text)

    def test_csv_ignores_unselected_parent_and_keeps_dual_top_level_role(self):
        self.replace(parts=[part('A', 'assembly', assembly_unit_cost='0', assembly_setup_fee='0',
                                 cost_confirmed=True, cost_inclusions='纯结构层，不另计装配费。'),
                            part('B', 'buy', purchase_price='2', cost_confirmed=True, cost_inclusions='外购单件。'),
                            part('UNSELECTED', 'assembly')],
                     lines=[{'id': 'L1', 'part_id': 'A', 'quantity': 2, 'unit': 'set'},
                            {'id': 'L2', 'part_id': 'B', 'quantity': 3, 'unit': 'piece'}],
                     bom=[{'id': 'E1', 'parent_id': 'A', 'part_id': 'B', 'quantity': '2', 'unit': 'piece'},
                          {'id': 'E2', 'parent_id': 'UNSELECTED', 'part_id': 'B', 'quantity': '99', 'unit': 'piece'}])
        quote = self.issue()
        self.assertEqual(quote['demands'], {'A': '2', 'B': '7'})
        content, _, _ = self.service.document(self.order['id'], quote['quote_id'], 'bom.csv')
        text = content.decode('utf-8-sig')
        self.assertNotIn('UNSELECTED', text)
        self.assertIn('顶层', text)
        self.assertIn('子件', text)
        rows = list(csv.DictReader(io.StringIO(text)))
        child = next(row for row in rows if row['零件号'] == 'B')
        self.assertEqual(child['总需求量'], '7')
        self.assertEqual(child['需求单位'], '件')
        self.assertEqual(child['模式'], 'buy')


if __name__ == '__main__':
    unittest.main()
