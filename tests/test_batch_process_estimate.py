"""Independent estimate arithmetic and source-contract checks. Server only."""
import copy
from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
from batch_quote_costing import calculate_batch
from batch_process_estimate import QUANTITY_KEYS, safe_quantities, MATERIAL_LABEL
from batch_process_quote import apply_process_costing
from cad_quote_quantities import validate_quote_quantities
from test_batch_quote_costing import document, settings, solid, SHA
from test_batch_process_quote import flat
from customer_upload_service import CustomerUploadService


def inventory(body, sha, *, bends=1, pierces=3):
    """Analytic receipt fixture; protocol arithmetic, not native CAD accuracy."""
    volume, area = Decimal(str(body['volume_mm3'])), Decimal(str(body['area_mm2']))
    quantities = {'material_area_mm2': str(area/2), 'material_gross_area_mm2': None,
        'cut_length_mm': '1000', 'pierce_count': pierces, 'bend_count': bends,
        'bend_length_mm': '50' if bends else None, 'thickness_mm': '1',
        'blank_width_mm': None, 'blank_height_mm': None}
    return {'schema_version': 1, 'version': 'cad-quote-quantities-v1',
        'solid_id': body['id'], 'source_sha256': sha, 'unit': 'mm',
        'status': 'estimated' if bends is not None and pierces is not None else 'unresolved',
        'method': 'native_brep_inventory', 'manufacturing_release': False, **quantities,
        'quantity_statuses': {key: 'estimated' if value is not None else 'unresolved' for key, value in quantities.items()},
        'source_face_ids': ['F0001', 'F0002', 'F0003', 'F0004'],
        'assumptions': ['Synthetic evidence for protocol tests only.'], 'warnings': [],
        'measurements': {'native_surface_area_mm2': str(area), 'native_volume_mm3': str(volume),
            'native_nondegenerate_edge_length_mm': '2000', 'native_skin_area_proxy_mm2': str(area/2),
            'volume_equivalent_area_mm2': str(volume), 'area_volume_thickness_ratio': str(area/(2*volume)),
            'geometry_classification': 'thin_wall_proxy' if Decimal('.8') <= area/(2*volume) <= Decimal('1.35') else 'non_uniform_or_stock_like',
            'topological_through_channels': pierces-1 if pierces is not None else None,
            'euler_characteristic': 2-2*(pierces-1) if pierces is not None else None, 'shell_count': 1,
            'cylindrical_bend_group_count': 0, 'cylindrical_bend_groups': [], 'sharp_fold_junction_count': 1,
            'junction_graph_cycle_rank': 0, 'known_bend_candidate_count': 1,
            'sharp_fold_junctions': [{'kind': 'sharp_fold_junction', 'face_ids': ['F0001','F0002','F0003','F0004'],
                                      'length_mm': '50', 'angle_deg': '90'}]},
        'thickness_basis': {'source': 'native_uniform_candidate', 'adopted_for_estimation': True, 'candidates_mm': ['1']},
        'unfolding': {'status': 'not_performed', 'production_pattern': False}}


def estimate_body(*, bends=1, pierces=3, grade=None):
    body = solid(1, definition='panel/solid-1', volume=1000, grade=grade)
    body.update(id='S0001', area_mm2=2000)
    body['topology']['faces'] = 8
    body['quote_quantities'] = inventory(body, SHA, bends=bends, pierces=pierces)
    return body


class ProcessEstimateTests(unittest.TestCase):
    def quote(self, body=None, **changes):
        return calculate_batch([document([body or estimate_body()])],
                               settings(order_sets=10, costing_mode='process-v1', **changes))

    def test_explicit_geometry_proxy_has_independent_process_costs(self):
        result = self.quote(); row = result['lines'][0]
        self.assertTrue(row['priced']); self.assertEqual(row['material_cost'], '0.55')
        self.assertEqual(row['process_cost'], '26.50'); self.assertEqual(row['setup_cost'], '20.00')
        self.assertEqual(result['totals']['total_cost'], '47.05')
        self.assertEqual(result['totals']['total_price'], '56.50')
        self.assertFalse(row['manufacturing']['manufacturing_release'])
        self.assertTrue(row['manufacturing']['route_review_required'])
        self.assertEqual(row['manufacturing']['weight_check']['status'], 'unavailable')
        self.assertEqual(row['manufacturing']['features']['blank_width_mm'], None)

    def test_processing_cost_is_unchanged_when_density_and_material_rate_change(self):
        steel, aluminium = self.quote(), self.quote(estimate_body(grade='5052'))
        self.assertNotEqual(steel['lines'][0]['material_cost'], aluminium['lines'][0]['material_cost'])
        self.assertNotEqual(steel['lines'][0]['estimated_net_weight_kg'], aluminium['lines'][0]['estimated_net_weight_kg'])
        self.assertEqual(steel['lines'][0]['process_cost'], aluminium['lines'][0]['process_cost'])
        self.assertEqual(steel['lines'][0]['setup_cost'], aluminium['lines'][0]['setup_cost'])

    def test_unknown_bend_and_piercing_remain_pending_not_zero_or_complete_total(self):
        result = self.quote(estimate_body(bends=None, pierces=None)); row = result['lines'][0]
        self.assertFalse(row['priced']); self.assertIsNone(row['unit_price'])
        self.assertIsNone(result['totals']['total_price']); self.assertIsNone(result['totals']['total_cost'])
        self.assertEqual(row['manufacturing']['estimated_cost_subtotal'], '32.55')
        self.assertEqual({item['code'] for item in row['manufacturing']['blockers']}, {'bend_count_required','pierce_count_required'})
        self.assertEqual([item['kind'] for item in row['manufacturing']['processes']], ['laser_cut','laser_setup'])

    def test_source_or_identity_mismatch_rejects_proxy_without_legacy_weight_fee(self):
        for field, value in (('source_sha256','b'*64), ('solid_id','S9999')):
            with self.subTest(field=field):
                body=estimate_body();body['quote_quantities'][field]=value
                result=self.quote(body)
                self.assertIsNone(result['totals']['total_price'])
                self.assertIsNone(result['lines'][0]['process_cost'])

    def test_explicit_requirements_still_block_unspecified_manufacturing_costs(self):
        body=estimate_body();body['name']='焊接 喷粉 钻孔'
        result=self.quote(body)
        self.assertIsNone(result['totals']['total_price'])
        self.assertEqual({item['code'] for item in result['lines'][0]['manufacturing']['blockers']},
                         {'welding_required','surface_treatment_required','drilling_tapping_required'})

    def test_stainless_filename_uses_saved_reference_assumption_not_actual_grade(self):
        body=estimate_body(); policy=CustomerUploadService._settings(1)
        result=calculate_batch([document([body],name='不锈钢组件.stp')], policy)
        row=result['lines'][0]
        self.assertEqual(row['material_estimation_label'],MATERIAL_LABEL)
        self.assertIsNone(row['material_raw']);self.assertEqual(row['confidence'],'assumed_material')
        self.assertEqual(row['material_profile'],'ss304');self.assertEqual(row['density_kg_m3'],'7930')
        self.assertEqual(Decimal(row['material_price_snapshot']['adopted_rate_cny_per_kg']),Decimal('14.50'))
        self.assertTrue(row['material_price_snapshot']['tax_basis_is_assumption'])
        self.assertEqual(row['estimated_net_weight_kg'],'0.00793')
        self.assertEqual(result['weight_summary']['estimated_total_net_weight_kg'],'0.00793')

    def test_real_cad_grade_has_priority_over_generic_stainless_filename(self):
        result=calculate_batch([document([estimate_body(grade='316')],name='不锈钢.stp')], CustomerUploadService._settings(1))
        row=result['lines'][0]
        self.assertEqual(row['material_profile'],'ss316');self.assertNotIn('material_estimation_label',row)
        self.assertEqual(row['confidence'],'cad_material')

    def test_strict_flat_price_is_not_overridden_by_general_inventory_presence(self):
        body=flat();body['area_mm2']=20000;body['topology']['faces']=8
        q=inventory(body,SHA);q['source_sha256']='b'*64;body['quote_quantities']=q
        result=self.quote(body)
        self.assertTrue(result['lines'][0]['priced']);self.assertEqual(result['lines'][0]['material_usage_kg'],'0.0942')
        self.assertEqual(result['totals']['total_price'],'41.30')
        self.assertNotIn('estimate_version',result['manufacturing'])

    def test_public_quantities_hide_rates_and_expose_proxy_crosscheck(self):
        value=inventory(estimate_body(),SHA);value['rate_cny']='SECRET'
        projected=safe_quantities(value)
        self.assertNotIn('rate_cny',projected)
        self.assertEqual(projected['measurements']['area_volume_thickness_ratio'],'1')
        self.assertEqual(projected['quantity_statuses']['pierce_count'],'estimated')
        self.assertFalse(projected['manufacturing_release'])

    def test_explicit_forming_and_weld_route_uses_piece_budgets_without_fake_bend_count(self):
        body=estimate_body(bends=None);source=document([body]);result=self.quote(body)
        apply_process_costing(result,[source],route_assumption='sheet_form_weld_demo')
        row=result['lines'][0];self.assertTrue(row['priced'])
        self.assertIsNone(row['manufacturing']['quotation_quantities']['bend_count'])
        operations={item['kind']:item for item in row['manufacturing']['processes']}
        self.assertNotIn('bending',operations)
        self.assertEqual(operations['forming_budget']['amount_cny'],'200.00')
        self.assertEqual(operations['assembly_weld_budget']['amount_cny'],'150.00')
        self.assertEqual(operations['forming_budget']['quantity_total'],'10')
        self.assertEqual(result['totals']['total_price'],'464.50')
        self.assertTrue(result['manufacturing']['estimate_scope_complete'])
        self.assertFalse(result['manufacturing']['engineering_complete'])
        self.assertFalse(result['manufacturing']['manufacturing_release'])

    def test_explicit_route_does_not_duplicate_a_supported_fold_candidate(self):
        body=estimate_body();result=self.quote(body)
        apply_process_costing(result,[document([body])],route_assumption='sheet_form_weld_demo')
        self.assertEqual(result['totals']['total_price'],'56.50')
        self.assertEqual({item['kind'] for item in result['lines'][0]['manufacturing']['processes']},
                         {'laser_cut','laser_piercing','bending','laser_setup'})


if __name__=='__main__':unittest.main()
