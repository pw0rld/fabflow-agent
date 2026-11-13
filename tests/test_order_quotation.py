"""Order cost arithmetic against real single-part records, without model calls."""
import copy
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from order_quotation import calculate, confirmed_route, pricing
from order_store import OrderStore
from prep_record import RecordError
from workbench_store import Workbench

POLICY = {'pricing_method': 'cost_plus', 'rate_percent': '20', 'tax_excluded': True, 'freight_excluded': True}


def part(key, mode='buy', **extra):
    return {'id': key, 'name': key, 'part_number': key, 'version': 'A', 'document_ids': [], 'mode': mode,
            'cost_confirmed': True, 'purchase_price': '2', 'cost_inclusions': '离线模拟采购价，仅本件。', **extra}


def steps(*names, status='confirmed'):
    return [{'id': 'S' + str(i), 'operation': name, 'reason': '离线人工核对', 'status': status} for i, name in enumerate(names)]


class OrderQuotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='order-quote-offline-')
        self.addCleanup(self.tmp.cleanup)
        self.app = Workbench(Path(self.tmp.name) / 'tasks')
        self.store = OrderStore(Path(self.tmp.name) / 'orders.sqlite3')
        self.order = self.store.create({'event_id': 'new', 'name': '离线报价', 'inquiry': '', 'simulated': True})

    def structure(self, parts, lines, bom=None, routes=None):
        self.order = self.store.replace(self.order['id'], {'event_id': 'structure-' + str(self.order['revision']),
            'expected_revision': self.order['revision'], 'parts': copy.deepcopy(parts), 'lines': copy.deepcopy(lines),
            'bom': copy.deepcopy(bom or []), 'routes': copy.deepcopy(routes or {})})
        return self.order

    def calculate(self, policy=None, fees=None, records=None, calculators=None):
        return calculate(self.order, records or {}, calculators or {}, policy or POLICY, fees or [], 'OQ-offline')

    def test_two_independent_roots_use_own_quantities_and_single_policy(self):
        self.structure([part('A', purchase_price='2.50'), part('B', purchase_price='4')],
                       [{'id': 'L1', 'part_id': 'A', 'quantity': 10, 'unit': 'piece'},
                        {'id': 'L2', 'part_id': 'B', 'quantity': 30, 'unit': 'piece'}])
        result = self.calculate()
        self.assertEqual(result['demands'], {'A': '10', 'B': '30'})
        self.assertEqual(result['amounts']['total_cost'], '145.00')
        self.assertEqual(result['amounts']['total_price'], '174.00')
        self.assertEqual([row['total_price'] for row in result['lines']], ['30.00', '144.00'])

    def test_multilevel_reused_child_setup_and_shared_fee_count_once(self):
        self.structure([part('ASSEMBLY', 'assembly', assembly_unit_cost='3', assembly_setup_fee='12', purchase_price=None),
                        part('SUB', 'assembly', assembly_unit_cost='1', assembly_setup_fee='8', purchase_price=None),
                        part('CHILD', purchase_price='2')],
                       [{'id': 'L1', 'part_id': 'ASSEMBLY', 'quantity': 10, 'unit': 'set'}],
                       [{'id': 'B1', 'parent_id': 'ASSEMBLY', 'part_id': 'SUB', 'quantity': '2', 'unit': 'piece'},
                        {'id': 'B2', 'parent_id': 'ASSEMBLY', 'part_id': 'CHILD', 'quantity': '1', 'unit': 'piece'},
                        {'id': 'B3', 'parent_id': 'SUB', 'part_id': 'CHILD', 'quantity': '3', 'unit': 'piece'}],
                       {'ASSEMBLY': steps('焊接'), 'SUB': steps('组装')})
        fees = [{'id': 'F1', 'name': '整单包装准备', 'value': '10', 'inclusions': '本单共享，仅计一次。'}]
        result = self.calculate(fees=fees)
        self.assertEqual(result['demands'], {'ASSEMBLY': '10', 'CHILD': '70', 'SUB': '20'})
        # Own: assembly 30+12, subassembly 20+8, bought child 70*2.
        self.assertEqual(result['amounts']['total_cost'], '220.00')
        self.assertEqual(result['amounts']['total_price'], '264.00')
        self.assertEqual(result['amounts']['shared_cost'], '10.00')
        self.assertEqual(result['shared_price'], '12.00')
        self.assertEqual(sum(Decimal(row['own_total_cost']) for row in result['part_costs']), Decimal('210.00'))

    def test_same_child_also_top_level_has_no_double_counted_setup(self):
        self.structure([part('A', 'assembly', assembly_unit_cost='0', assembly_setup_fee='12'),
                        part('B', purchase_price='2')],
                       [{'id': 'L1', 'part_id': 'A', 'quantity': 2, 'unit': 'set'},
                        {'id': 'L2', 'part_id': 'B', 'quantity': 3, 'unit': 'piece'}],
                       [{'id': 'E1', 'parent_id': 'A', 'part_id': 'B', 'quantity': '2', 'unit': 'piece'}],
                       {'A': steps('组装')})
        result = self.calculate(policy=dict(POLICY, rate_percent='0'))
        self.assertEqual(result['demands'], {'A': '2', 'B': '7'})
        self.assertEqual(result['amounts']['total_cost'], '26.00')
        self.assertEqual(result['amounts']['total_price'], '26.00')

    def test_unknown_quantity_and_unconfirmed_cost_never_become_zero(self):
        self.structure([part('BUY')], [{'id': 'L', 'part_id': 'BUY', 'quantity': None, 'unit': 'piece'}])
        with self.assertRaisesRegex(RecordError, '未知'):
            self.calculate()
        for patch in [{'purchase_price': None}, {'cost_confirmed': False}, {'cost_inclusions': ''}]:
            self.structure([part('BUY', **patch)], [{'id': 'L', 'part_id': 'BUY', 'quantity': 1, 'unit': 'piece'}])
            with self.subTest(patch=patch), self.assertRaises(RecordError):
                self.calculate()

    def test_assembly_missing_setup_and_unconfirmed_route_block_cost(self):
        for overrides, route in [({'assembly_setup_fee': None}, steps('焊接')),
                                  ({}, steps('焊接', status='pending_review')),
                                  ({}, steps('折弯', '折弯'))]:
            self.structure([part('A', 'assembly', assembly_unit_cost='3', assembly_setup_fee=overrides.get('assembly_setup_fee', '0'))],
                           [{'id': 'L', 'part_id': 'A', 'quantity': 1, 'unit': 'set'}], routes={'A': route})
            with self.subTest(overrides=overrides, route=route), self.assertRaises(RecordError):
                self.calculate()
        self.structure([part('A', 'assembly', assembly_unit_cost='0', assembly_setup_fee='10')],
                       [{'id': 'L', 'part_id': 'A', 'quantity': 1, 'unit': 'set'}])
        with self.assertRaisesRegex(RecordError, '工艺步骤'):
            self.calculate()

    def test_shared_fees_require_unique_ids_and_actual_scope(self):
        self.structure([part('BUY')], [{'id': 'L', 'part_id': 'BUY', 'quantity': 1, 'unit': 'piece'}])
        fee = {'id': 'F', 'name': '共同准备', 'value': '10', 'inclusions': '仅本单共享准备费。'}
        for fees in [[fee, fee], [{**fee, 'inclusions': ''}], [{**fee, 'value': '-1'}]]:
            with self.subTest(fees=fees), self.assertRaises(RecordError):
                self.calculate(fees=fees)

    def test_margin_costplus_explicit_policy_and_documented_rounding(self):
        self.structure([part('BUY', purchase_price='1.005')], [{'id': 'L', 'part_id': 'BUY', 'quantity': 3, 'unit': 'piece'}])
        plus = self.calculate()
        margin = self.calculate(policy=dict(POLICY, pricing_method='gross_margin'))
        self.assertEqual(plus['lines'][0]['unit_price'], '1.21')
        self.assertEqual(plus['amounts']['total_price'], '3.63')
        self.assertEqual(margin['lines'][0]['unit_price'], '1.26')
        self.assertEqual(margin['amounts']['total_price'], '3.78')
        for policy in [dict(POLICY, rate_percent=''), dict(POLICY, pricing_method='gross_margin', rate_percent='100'),
                       dict(POLICY, tax_excluded=False)]:
            with self.subTest(policy=policy), self.assertRaises(RecordError):
                pricing(policy)

    def test_real_single_part_cost_is_used_before_order_markup_and_frozen(self):
        task = self.app.create({'event_id': 'part', 'inquiry': '本地单品构造', 'simulated': True})
        requirements = {'product_name': 'MAKE', 'quantity': 10, 'material_family': '碳钢',
                        'material_grade': 'Q235B', 'thickness': '3', 'surface_treatment': '不处理'}
        technical = {'material_usage_per_piece': '0.4', 'material_rate': '20', 'process_route': ['切割', '折弯'],
                     'operation_rates': [{'operation': '切割', 'value': '12', 'unit': 'CNY/piece'},
                                         {'operation': '折弯', 'value': '18', 'unit': 'CNY/piece'}],
                     'setup_fee': '200', 'fee_inclusions': ['工序已含人工耗材；准备费另计。']}
        for stage, values in [('requirements', requirements), ('technical', technical)]:
            task = self.app.save_fields(task['id'], stage, {'event_id': stage, 'expected_revision': task['revision'], 'values': values})
        record = self.app.records.load(task['id'])
        costs = self.app.quotes._calculate(record, pricing(dict(POLICY, rate_percent='0')))
        self.structure([part('MAKE', 'make', task_id=task['id'])],
                       [{'id': 'L', 'part_id': 'MAKE', 'quantity': 10, 'unit': 'piece'}],
                       routes={'MAKE': steps('切割', '折弯')})
        result = self.calculate(records={'MAKE': record}, calculators={'MAKE': costs})
        self.assertEqual(result['amounts']['total_cost'], '580.00')
        self.assertEqual(result['amounts']['total_price'], '696.00')
        frozen = copy.deepcopy(result)
        record['fields']['material_rate']['effective']['value'] = '999'
        self.order['parts'][0]['name'] = 'later edit'
        self.assertEqual(result, frozen)

    def test_make_quantity_or_route_disagreement_blocks(self):
        self.structure([part('MAKE', 'make', task_id='INQ-test')],
                       [{'id': 'L', 'part_id': 'MAKE', 'quantity': 10, 'unit': 'piece'}], routes={'MAKE': steps('切割')})
        record = {'data_revision': 1, 'fields': {'process_route': {'effective': {'value': ['折弯']}}}}
        with self.assertRaisesRegex(RecordError, '顺序不一致'):
            self.calculate(records={'MAKE': record}, calculators={'MAKE': {'quantity': 10, 'raw_total_cost': '1'}})
        record['fields']['process_route']['effective']['value'] = ['切割']
        with self.assertRaisesRegex(RecordError, '加工数量'):
            self.calculate(records={'MAKE': record}, calculators={'MAKE': {'quantity': 9, 'raw_total_cost': '1'}})


if __name__ == '__main__':
    unittest.main()
