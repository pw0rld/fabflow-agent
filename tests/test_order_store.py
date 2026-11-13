"""Offline transactional order/BOM checks; no customer files or model calls."""
import copy
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from order_store import OrderConflict, OrderStore, demand, validate_structure
from prep_record import RecordError


def part(part_id, mode='make', **extra):
    return {'id': part_id, 'name': part_id, 'part_number': None, 'version': None,
            'mode': mode, 'document_ids': [], **extra}


def line(line_id, part_id, quantity, unit='piece'):
    return {'id': line_id, 'part_id': part_id, 'quantity': quantity, 'unit': unit}


def edge(edge_id, parent_id, part_id, quantity):
    return {'id': edge_id, 'parent_id': parent_id, 'part_id': part_id,
            'quantity': quantity, 'unit': 'piece'}


class OrderStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='orders-offline-')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'orders.sqlite3'
        self.store = OrderStore(self.path)
        self.order = self.store.create({'event_id': 'create', 'name': '离线订单', 'inquiry': '人工构造模拟订单', 'simulated': True})

    def replace(self, **values):
        payload = {'event_id': 'edit-' + str(self.order['revision']), 'expected_revision': self.order['revision'], **values}
        self.order = self.store.replace(self.order['id'], payload)
        return self.order

    def test_create_persist_list_and_detached_results(self):
        self.assertRegex(self.order['id'], r'^ORD-[a-f0-9]{20}$')
        self.assertEqual(self.order['revision'], 1)
        self.assertEqual(OrderStore(self.path).get(self.order['id']), self.order)
        summaries = self.store.list()
        self.assertEqual(summaries[0]['id'], self.order['id'])
        self.assertEqual(summaries[0]['parts_count'], 0)
        self.order['documents'].append({'id': 'unsaved'})
        self.assertEqual(self.store.get(self.order['id'])['documents'], [])

    def test_create_idempotency_and_event_payload_conflict(self):
        payload = {'event_id': 'create', 'name': '离线订单', 'inquiry': '人工构造模拟订单', 'simulated': True}
        self.assertEqual(self.store.create(payload), self.order)
        with self.assertRaises(OrderConflict):
            self.store.create({**payload, 'name': '另一单'})
        self.assertEqual(len(self.store.list()), 1)

    def test_replace_only_given_keys_preserves_documents_quotes_and_metadata(self):
        payload = {'event_id': 'document', 'expected_revision': 1}
        def attach(order):
            order['documents'].append({'id': 'D-1', 'name': '同件资料.dxf'})
            order['quotes'].append({'id': 'Q-1', 'cost': '12.34'})
            order['discovery'] = {'status': 'completed'}
        self.order = self.store.mutate(self.order['id'], payload, 'attach', attach)
        self.replace(parts=[part('P-1', document_ids=['D-1'])])
        self.assertEqual(self.order['documents'][0]['id'], 'D-1')
        self.assertEqual(self.order['quotes'], [{'id': 'Q-1', 'cost': '12.34'}])
        self.assertEqual(self.order['discovery'], {'status': 'completed'})
        self.assertEqual(self.order['lines'], [])

    def test_mutation_replay_returns_original_result_without_running_callback(self):
        payload = {'event_id': 'first', 'expected_revision': 1, 'value': 'A'}
        first = self.store.mutate(self.order['id'], payload, 'edit', lambda order: order.update(name='第一版'))
        later = self.store.mutate(self.order['id'], {'event_id': 'second', 'expected_revision': 2},
                                  'edit', lambda order: order.update(name='第二版'))
        replay = self.store.mutate(self.order['id'], payload, 'edit', lambda order: self.fail('replay executed callback'))
        self.assertEqual(first, replay)
        self.assertEqual(later, self.store.get(self.order['id']))
        with self.assertRaises(OrderConflict):
            self.store.mutate(self.order['id'], {**payload, 'value': 'B'}, 'edit', lambda order: None)
        with self.assertRaises(OrderConflict):
            self.store.mutate(self.order['id'], payload, 'other', lambda order: None)

    def test_custom_result_is_atomic_and_replayed(self):
        payload = {'event_id': 'custom', 'expected_revision': 1}
        def change(order):
            order['name'] = '定制结果'
            return {'ok': True, 'quote_id': 'Q-1'}
        expected = {'ok': True, 'quote_id': 'Q-1'}
        self.assertEqual(self.store.mutate(self.order['id'], payload, 'custom', change), expected)
        self.assertEqual(self.store.mutate(self.order['id'], payload, 'custom', lambda order: None), expected)
        self.assertEqual(self.store.get(self.order['id'])['revision'], 2)

    def test_stale_revision_rejected_before_callback(self):
        self.replace(parts=[part('P-1')])
        with self.assertRaises(OrderConflict):
            self.store.mutate(self.order['id'], {'event_id': 'stale', 'expected_revision': 1},
                              'edit', lambda order: self.fail('stale callback ran'))
        self.assertEqual(self.store.get(self.order['id']), self.order)

    def test_callback_or_validation_failure_rolls_back_event_and_order(self):
        payload = {'event_id': 'rollback', 'expected_revision': 1}
        def fail(order):
            order['name'] = '不能保存'
            raise RuntimeError('simulated failure')
        with self.assertRaises(RuntimeError):
            self.store.mutate(self.order['id'], payload, 'edit', fail)
        self.assertEqual(self.store.get(self.order['id']), self.order)
        with self.assertRaises(RecordError):
            self.store.mutate(self.order['id'], payload, 'edit', lambda order: order.update(parts=[part('D'), part('D')]))
        saved = self.store.mutate(self.order['id'], payload, 'edit', lambda order: order.update(name='重试成功'))
        self.assertEqual(saved['revision'], 2)

    def test_quote_snapshots_are_append_only_and_identity_immutable(self):
        self.order = self.store.mutate(self.order['id'], {'event_id': 'quote', 'expected_revision': 1},
                                       'quote', lambda order: order['quotes'].append({'id': 'Q-1', 'amount': '9.00'}))
        for field, value in [('id', 'ORD-other'), ('revision', 100), ('quotes', []), ('quotes', [{'id': 'Q-1', 'amount': '0'}])]:
            with self.subTest(field=field, value=value), self.assertRaises(RecordError):
                self.store.mutate(self.order['id'], {'event_id': 'bad', 'expected_revision': 2},
                                  'edit', lambda order: order.update({field: value}))
        self.assertEqual(self.store.get(self.order['id']), self.order)

    def test_two_writers_with_same_revision_only_one_commits(self):
        barrier = threading.Barrier(2)
        outcomes = []
        def writer(event):
            store = OrderStore(self.path)
            barrier.wait(timeout=5)
            try:
                result = store.mutate(self.order['id'], {'event_id': event, 'expected_revision': 1},
                                      'edit', lambda order: order.update(name=event))
                outcomes.append(('saved', result['name']))
            except OrderConflict:
                outcomes.append(('conflict', event))
        threads = [threading.Thread(target=writer, args=(name,)) for name in ['writer-A', 'writer-B']]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(status for status, _ in outcomes), ['conflict', 'saved'])
        self.assertEqual(self.store.get(self.order['id'])['revision'], 2)

    def test_independent_root_quantities_and_same_part_multiple_order_lines(self):
        self.replace(parts=[part('A'), part('B')], lines=[line('L1', 'A', 10), line('L2', 'B', '30'), line('L3', 'A', 2)])
        self.assertEqual(demand(self.order), {'A': '12', 'B': '30'})

    def test_multilevel_paths_accumulate_and_keep_assembly_demand(self):
        self.replace(parts=[part('ASSY', 'assembly'), part('SUB', 'assembly'), part('BRACKET'), part('PLATE')],
                     lines=[line('L1', 'ASSY', 10, 'set')],
                     bom=[edge('B1', 'ASSY', 'SUB', '2'), edge('B2', 'ASSY', 'BRACKET', '1'),
                          edge('B3', 'SUB', 'BRACKET', '3'), edge('B4', 'ASSY', 'PLATE', '1.0')])
        self.assertEqual(demand(self.order), {'ASSY': '10', 'BRACKET': '70', 'PLATE': '10', 'SUB': '20'})

    def test_selected_child_also_top_level_accumulates_before_expansion(self):
        self.replace(parts=[part('A', 'assembly'), part('B', 'assembly'), part('C')],
                     lines=[line('L1', 'A', 2), line('L2', 'B', 3)],
                     bom=[edge('E1', 'A', 'B', '2'), edge('E2', 'B', 'C', '4')])
        self.assertEqual(demand(self.order), {'A': '2', 'B': '7', 'C': '28'})

    def test_duplicate_uploaded_documents_do_not_change_demand(self):
        self.order = self.store.mutate(self.order['id'], {'event_id': 'docs', 'expected_revision': 1}, 'docs',
                                       lambda order: order.update(documents=[{'id': 'D1', 'sha256': 'same'}, {'id': 'D2', 'sha256': 'same'}]))
        self.replace(parts=[part('P', document_ids=['D1', 'D2'])], lines=[line('L', 'P', 20)])
        self.assertEqual(demand(self.order), {'P': '20'})

    def test_unknown_quantities_remain_unknown_and_block_only_when_reachable(self):
        self.replace(parts=[part('A', 'assembly'), part('B'), part('UNUSED', 'assembly')],
                     lines=[line('L1', 'A', None)], bom=[edge('B1', 'A', 'B', None), edge('B2', 'UNUSED', 'B', None)])
        with self.assertRaisesRegex(RecordError, '未知'):
            demand(self.order)
        self.replace(lines=[line('L1', 'A', 1)])
        with self.assertRaisesRegex(RecordError, '未知'):
            demand(self.order)
        self.replace(bom=[edge('B1', 'A', 'B', '2'), edge('B2', 'UNUSED', 'B', None)])
        self.assertEqual(demand(self.order), {'A': '1', 'B': '2'})
        self.assertIsNone(self.order['bom'][1]['quantity'])

    def test_fractional_zero_negative_nonfinite_and_boolean_quantity_rejected(self):
        for quantity in ['0.5', '0', '-2', 'NaN', 'Infinity', True, 0.5, '1e100000']:
            with self.subTest(quantity=quantity), self.assertRaises(RecordError):
                self.replace(parts=[part('A'), part('B')], lines=[line('L', 'A', 1)], bom=[edge('E', 'A', 'B', quantity)])

    def test_duplicate_ids_missing_references_and_cycles_rejected(self):
        cases = [
            {'parts': [part('A'), part('A')]},
            {'parts': [part('A')], 'lines': [line('L', 'MISSING', 1)]},
            {'parts': [part('A', document_ids=['MISSING'])]},
            {'parts': [part('A')], 'routes': {'MISSING': []}},
            {'parts': [part('A', 'assembly'), part('B', 'assembly')], 'bom': [edge('E', 'A', 'B', 1), edge('F', 'B', 'A', 1)]},
            {'parts': [part('A', 'assembly')], 'bom': [edge('E', 'A', 'A', 1)]},
            {'parts': [part('A', 'assembly'), part('B')], 'bom': [edge('E', 'A', 'B', 1), edge('E', 'A', 'B', 1)]},
        ]
        for structure in cases:
            with self.subTest(structure=structure), self.assertRaises(RecordError):
                self.replace(**structure)

    def test_buy_parent_cannot_charge_both_purchase_and_children(self):
        with self.assertRaisesRegex(RecordError, '外购总成'):
            self.replace(parts=[part('BUY', 'buy', purchase_price='12.30'), part('CHILD')],
                         lines=[line('L', 'BUY', 2)], bom=[edge('E', 'BUY', 'CHILD', 1)])
        self.replace(parts=[part('BUY', 'buy', purchase_price=None)], lines=[line('L', 'BUY', 2)])
        self.assertEqual(demand(self.order), {'BUY': '2'})

    def test_make_parent_requires_assembly_mode_before_expansion(self):
        with self.assertRaisesRegex(RecordError, '父项须设为装配'):
            self.replace(parts=[part('MAKE'), part('CHILD')],
                         lines=[line('L', 'MAKE', 2)], bom=[edge('E', 'MAKE', 'CHILD', 1)])

    def test_assembly_cost_metadata_preserved_but_invalid_values_rejected(self):
        self.replace(parts=[part('ASSY', 'assembly', assembly_unit_cost='5.25', assembly_setup_fee='0',
                                 cost_confirmed=False, cost_inclusions='仅本层装配，不含子件')])
        self.assertEqual(self.order['parts'][0]['assembly_unit_cost'], '5.25')
        for key, value in [('assembly_unit_cost', '-1'), ('assembly_setup_fee', 'NaN'),
                           ('purchase_price', True), ('cost_confirmed', 'true'), ('cost_inclusions', [])]:
            with self.subTest(key=key), self.assertRaises(RecordError):
                self.replace(parts=[part('ASSY', 'assembly', **{key: value})])

    def test_validate_is_readonly_and_accepts_extensible_evidence_and_routes(self):
        self.replace(parts=[part('A', evidence={'raw_text': '人工核对'}, child_task_signature='test-signature')],
                     routes={'A': {'confirmed': False, 'steps': [{'id': 'S1', 'operation': '折弯', 'unknown_future_field': None}]}})
        before = copy.deepcopy(self.order)
        validate_structure(self.order)
        self.assertEqual(before, self.order)

    def test_depth_and_demand_overflow_are_bounded_without_rounding(self):
        parts = [part('P' + str(i), 'assembly') for i in range(102)]
        with self.assertRaisesRegex(RecordError, '100 层'):
            self.replace(parts=parts, bom=[edge('E' + str(i), 'P' + str(i), 'P' + str(i + 1), 1) for i in range(101)])
        self.replace(parts=[part('A', 'assembly'), part('B')], lines=[line('L', 'A', '1000000000')],
                     bom=[edge('E', 'A', 'B', '1000000000')])
        with self.assertRaisesRegex(RecordError, '过大'):
            demand(self.order)


if __name__ == '__main__':
    unittest.main()
