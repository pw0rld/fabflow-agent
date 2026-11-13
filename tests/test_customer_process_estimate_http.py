"""Cached quantity estimate boundary, immutable journal and quota tests.

Run only on ZeroClave. Synthetic native receipts test protocol, not CAD accuracy.
"""
import copy
from decimal import Decimal
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'src'), str(Path(__file__).resolve().parent)]
import test_customer_process_http as fixture
from test_batch_process_estimate import inventory

ADMIN = '/ortho2cad-test/customer-upload-admin/api/quote-batches'


class ProcessEstimateHTTPTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.CustomerProcessHTTPTests(methodName='test_bent_part_does_not_offer_flat_pattern_or_full_quotation')
        self.f.setUp(); self.addCleanup(self.f.tearDown)
        f=self.f;f.mixed=True
        self.owner,self.outsider=f.session(),f.session();self.order=f.finished(self.owner)
        self.batch,self.run=self.order['id'],self.order['quote']['run_id']
        self.customer=fixture.API+'/orders/'+self.batch+'/runs/'+self.run
        self.staff=ADMIN+'/'+self.batch+'/runs/'+self.run
        self.token='R'+'1'*32
        def native_get(identity):
            doc=f.cad.documents[identity]
            return {'document_id':identity,'source_sha256':doc['sha256'],'status':'completed','geometry_run_id':self.token}
        f.manufacturing.get=native_get
        body=f.manufacturing.geometries[f.native_id]['solids'][1]
        body['quote_quantities']=inventory(body,f.cad.documents[f.native_id]['sha256'])

    def get_plan(self):
        status,plan=self.f.request(self.customer+'/process-pricing',cookie=self.owner)
        self.assertEqual(status,200,plan);return plan

    def payload(self,plan,event='estimate-commit'):
        return {'event_id':event,'expected_revision':plan['expected_revision'],'plan_digest':plan['plan_digest']}

    def test_plan_is_source_bound_read_only_cost_free_and_owner_checked(self):
        f=self.f;service=f.server.customer_batches;folder=service._folder(self.batch)
        before={p.relative_to(folder).as_posix():p.read_bytes() for p in folder.rglob('*') if p.is_file()}
        quota=f.request(fixture.API+'/quota',cookie=self.owner)[1]
        with (patch.object(service,'_native',side_effect=AssertionError('No CAD worker')),
              patch.object(service.engineering_recognition,'enqueue',side_effect=AssertionError('No AI'))):
            plan=self.get_plan();self.assertTrue(plan['can_estimate']);self.assertEqual(plan['status'],'ready')
            self.assertFalse(plan['manufacturing_release']);f.assert_no_costs(plan)
            self.assertEqual(f.request(self.staff+'/process-pricing')[1],plan)
            self.assertEqual(f.request(self.customer+'/process-pricing',cookie=self.outsider)[0],404)
            self.assertEqual(f.request(self.customer+'/process-pricing')[0],401)
            self.assertEqual(f.request(self.customer+'/process-pricing?scope=staff',cookie=self.owner)[0],404)
        self.assertEqual(before,{p.relative_to(folder).as_posix():p.read_bytes() for p in folder.rglob('*') if p.is_file()})
        self.assertEqual(f.request(fixture.API+'/quota',cookie=self.owner)[1],quota)
        self.assertEqual(plan['lines'][1]['method'],'native_brep_inventory')
        self.assertEqual(plan['lines'][1]['quantity_statuses']['pierce_count'],'estimated')

    def test_estimate_creates_one_immutable_run_and_replays_without_upload_allowance(self):
        f=self.f;service=f.server.customer_batches;plan=self.get_plan();payload=self.payload(plan)
        source=service._folder(self.batch)/'runs'/(self.run+'.json');original=source.read_bytes()
        quota=f.request(fixture.API+'/quota',cookie=self.owner)[1]
        status,value=f.request(self.customer+'/process-pricing',payload,cookie=self.owner)
        self.assertEqual(status,201,value);self.assertFalse(value['replayed']);f.assert_no_costs(value)
        self.assertNotEqual(value['run_id'],self.run);self.assertEqual(value['source_run_id'],self.run)
        self.assertEqual(value['order']['quote']['run_id'],value['run_id'])
        self.assertEqual(value['order']['quote']['pricing_status'],'estimated')
        self.assertIsNotNone(value['order']['quote']['total_price'])
        self.assertEqual(source.read_bytes(),original)
        again=f.request(self.customer+'/process-pricing',payload,cookie=self.owner)
        self.assertEqual(again[0],201,again);self.assertTrue(again[1]['replayed'])
        self.assertEqual(again[1]['run_id'],value['run_id']);self.assertEqual(len(service.get(self.batch)['runs']),2)
        self.assertEqual(f.request(fixture.API+'/quota',cookie=self.owner)[1],quota)
        new_path=fixture.API+'/orders/'+self.batch+'/runs/'+value['run_id']+'/process-pricing'
        status,new_plan=f.request(new_path,cookie=self.owner)
        self.assertEqual(status,200,new_plan);self.assertFalse(new_plan['can_estimate']);self.assertTrue(new_plan['already_applied'])
        status,same=f.request(new_path,self.payload(new_plan,event='same-geometry'),cookie=self.owner)
        self.assertEqual(status,201,same);self.assertTrue(same['already_applied']);self.assertEqual(len(service.get(self.batch)['runs']),2)
        self.assertEqual(f.request(self.customer+'/process-pricing',self.payload(plan,event='old-run-again'),cookie=self.owner)[0],409)

    def test_changed_native_run_or_quote_revision_rejects_stale_plan(self):
        f=self.f;plan=self.get_plan();self.token='R'+'2'*32
        status,error=f.request(self.customer+'/process-pricing',self.payload(plan),cookie=self.owner)
        self.assertEqual(status,409,error);self.assertEqual(len(f.server.customer_batches.get(self.batch)['runs']),1)
        current=self.get_plan();payload=self.payload(current);payload['expected_revision']+=1
        self.assertEqual(f.request(self.customer+'/process-pricing',payload,cookie=self.owner)[0],409)
        f.manufacturing.geometries[f.native_id]['solids'][1]['quote_quantities']['source_sha256']='f'*64
        blocked=self.get_plan();self.assertFalse(blocked['can_estimate'])

    def test_missing_route_creates_only_partial_known_subtotal_and_factory_can_review(self):
        f=self.f;body=f.manufacturing.geometries[f.native_id]['solids'][1]
        body['quote_quantities']=inventory(body,f.cad.documents[f.native_id]['sha256'],bends=None)
        plan=self.get_plan();self.assertTrue(plan['can_estimate']);self.assertEqual(plan['status'],'partial')
        self.assertIsNotNone(plan['estimated_subtotal']);self.assertIsNone(plan['total_price'])
        status,value=f.request(self.customer+'/process-pricing',self.payload(plan),cookie=self.owner)
        self.assertEqual(status,201,value);self.assertEqual(value['order']['quote']['pricing_status'],'partial')
        self.assertIsNone(value['order']['quote']['total_price']);self.assertIsNotNone(value['order']['quote']['estimated_subtotal'])
        path=ADMIN+'/'+self.batch+'/runs/'+value['run_id']+'/cost-sheet'
        status,sheet=f.request(path);self.assertEqual(status,200,sheet)
        self.assertTrue(sheet['can_reprice'])

    def test_no_client_supplied_rate_or_quantity_is_accepted(self):
        f=self.f;plan=self.get_plan()
        for extra in ({'settings':{}},{'quantities':{'bend_count':0}},{'material_rate':'0'},{'role':'staff'}):
            status,error=f.request(self.customer+'/process-pricing',{**self.payload(plan),**extra},cookie=self.owner)
            self.assertEqual(status,400,error)
        self.assertEqual(len(f.server.customer_batches.get(self.batch)['runs']),1)
        self.assertEqual(f.request(self.customer+'/process-pricing',self.payload(plan),cookie=self.outsider)[0],404)

    def test_lost_response_after_prepared_snapshot_recovers_exact_run_once(self):
        f=self.f;service=f.server.customer_batches;payload=self.payload(self.get_plan())
        with patch.object(service,'_complete',side_effect=RuntimeError('Synthetic crash before pointer commit')):
            self.assertEqual(f.request(self.customer+'/process-pricing',payload,cookie=self.owner)[0],500)
        with service._lock(self.batch):
            records=service._pricing_records(self.batch)
            self.assertEqual(len(records),1);self.assertEqual(records[0][1]['status'],'prepared')
            target=records[0][1]['run_id']
        status,value=f.request(self.customer+'/process-pricing',payload,cookie=self.owner)
        self.assertEqual(status,201,value);self.assertTrue(value['replayed']);self.assertEqual(value['run_id'],target)
        self.assertEqual(len(service.get(self.batch)['runs']),2)
        conflict={**payload,'plan_digest':'f'*64}
        self.assertEqual(f.request(self.customer+'/process-pricing',conflict,cookie=self.owner)[0],409)

    def test_explicit_route_adoption_is_bound_and_keeps_native_unknowns(self):
        f=self.f;body=f.manufacturing.geometries[f.native_id]['solids'][1]
        body['quote_quantities']=inventory(body,f.cad.documents[f.native_id]['sha256'],bends=None)
        plan=self.get_plan();self.assertEqual(plan['route_proposal']['id'],'sheet_form_weld_demo')
        self.assertTrue(plan['route_proposal']['requires_confirmation'])
        status,value=f.request(self.customer+'/process-pricing',
            {**self.payload(plan),'route_assumption':'sheet_form_weld_demo'},cookie=self.owner)
        self.assertEqual(status,201,value);self.assertEqual(value['order']['quote']['pricing_status'],'estimated')
        self.assertIsNotNone(value['order']['quote']['total_price']);f.assert_no_costs(value)
        internal=f.server.customer_batches.get(self.batch)['result']
        self.assertEqual(internal['process_estimate']['route_assumption']['source'],'operator_adoption')
        self.assertEqual(internal['process_estimate']['route_assumption']['event_id'],'estimate-commit')
        self.assertIsNone(internal['lines'][1]['manufacturing']['quotation_quantities']['bend_count'])
        self.assertFalse(internal['manufacturing']['engineering_complete'])
        self.assertEqual(internal['settings']['order_sets'],10)

    def test_partial_run_can_adopt_a_new_explicit_route_without_reupload(self):
        f=self.f;body=f.manufacturing.geometries[f.native_id]['solids'][1]
        body['quote_quantities']=inventory(body,f.cad.documents[f.native_id]['sha256'],bends=None)
        status,value=f.request(self.customer+'/process-pricing',self.payload(self.get_plan()),cookie=self.owner)
        self.assertEqual(status,201,value)
        path=fixture.API+'/orders/'+self.batch+'/runs/'+value['run_id']+'/process-pricing'
        status,plan=f.request(path,cookie=self.owner);self.assertEqual(status,200,plan)
        self.assertTrue(plan['already_applied']);self.assertTrue(plan['can_estimate'])
        status,value=f.request(path,{**self.payload(plan,event='adopt-new-condition'),
            'route_assumption':'sheet_form_weld_demo'},cookie=self.owner)
        self.assertEqual(status,201,value);self.assertIsNotNone(value['order']['quote']['total_price'])
        self.assertEqual(len(f.server.customer_batches.get(self.batch)['runs']),3)

    def test_concurrent_engineering_review_update_rejects_before_preparing_journal(self):
        f=self.f;service=f.server.customer_batches;plan=self.get_plan()
        original_context=service.process_pricing._context
        updates=[];failures=[]
        def concurrent_context(batch_id,run_id):
            context=original_context(batch_id,run_id)
            def update_review():
                try:
                    updates.append(service.save_engineering(self.batch,self.run,{
                        'event_id':'concurrent-review','expected_revision':0,
                        'review':{'header':{'product_number':'CONCURRENT-ENGINEERING'}}}))
                except Exception as error:failures.append(error)
            worker=threading.Thread(target=update_review);worker.start();worker.join(5)
            if worker.is_alive() or failures:raise AssertionError('Concurrent review did not complete')
            return context
        with patch.object(service.process_pricing,'_context',side_effect=concurrent_context):
            status,error=f.request(self.customer+'/process-pricing',self.payload(plan),cookie=self.owner)
        self.assertEqual(status,409,error);self.assertEqual(len(updates),1)
        self.assertEqual(service.get(self.batch)['revision'],plan['expected_revision'])
        self.assertEqual(len(service.get(self.batch)['runs']),1)
        with service._lock(self.batch):self.assertEqual(service._pricing_records(self.batch),[])
        fresh=self.get_plan()
        status,value=f.request(self.customer+'/process-pricing',self.payload(fresh),cookie=self.owner)
        self.assertEqual(status,201,value);self.assertEqual(len(service.get(self.batch)['runs']),2)

    def test_native_flat_receipt_is_displayed_without_changing_strict_blank_pricing(self):
        from test_cad_quote_quantities import flat_receipt
        from batch_process_evidence import summarise_native_processes
        f=self.f;service=f.server.customer_batches;before=service.get(self.batch)['result']
        body=f.manufacturing.geometries[f.native_id]['solids'][0]
        laser=summarise_native_processes(body)['laser_cut']
        body['area_mm2']=float(2*Decimal(laser['net_area_mm2'])+Decimal(laser['cut_length_mm'])*Decimal(laser['thickness_mm']))
        receipt=flat_receipt();receipt.update(source_sha256=f.cad.documents[f.native_id]['sha256'],
            material_area_mm2=laser['net_area_mm2'],material_gross_area_mm2=str(Decimal(laser['blank_width_mm'])*Decimal(laser['blank_height_mm'])),
            cut_length_mm=laser['cut_length_mm'],pierce_count=laser['pierce_count'],
            thickness_mm=laser['thickness_mm'],blank_width_mm=laser['blank_width_mm'],blank_height_mm=laser['blank_height_mm'],
            measurements={'native_volume_mm3':str(body['volume_mm3'])})
        body['quote_quantities']=receipt
        plan=self.get_plan();flat=plan['lines'][0]
        self.assertEqual(flat['method'],'native_flat_profile')
        self.assertEqual(flat['quantities']['cut_length_mm'],laser['cut_length_mm'])
        self.assertEqual(flat['quantities']['bend_count'],0)
        self.assertEqual(flat['quantity_statuses']['cut_length_mm'],'measured')
        self.assertEqual(service.get(self.batch)['result'],before)
        status,value=f.request(self.customer+'/process-pricing',self.payload(plan),cookie=self.owner)
        self.assertEqual(status,201,value)
        updated=service.get(self.batch)['result']['lines'][0]
        for key in ('material_usage_kg','material_cost','process_cost','setup_cost','unit_price','total_price'):
            self.assertEqual(updated[key],before['lines'][0][key],key)

    def test_manual_cost_sheet_amount_has_priority_and_automatic_estimate_cannot_overwrite_it(self):
        f=self.f;service=f.server.customer_batches
        status,calculated=f.request(self.customer+'/process-pricing',self.payload(self.get_plan()),cookie=self.owner)
        self.assertEqual(status,201,calculated)
        run=calculated['run_id'];sheet=service.cost_sheet.get(self.batch,run)
        policy=copy.deepcopy(sheet['sheet']['policy'])
        policy['entries'].append({'id':'manual-extra-1','line_id':None,'category':'other',
            'label':'本单明确人工核价','method':'fixed','source_kind':'manual_adopted','amount':'300'})
        priced=service.cost_sheet.reprice(self.batch,run,{'event_id':'manual-cost-priority',
            'expected_revision':sheet['expected_revision'],'policy':policy})
        saved=priced['batch']['result'];run=saved['run_id']
        path=fixture.API+'/orders/'+self.batch+'/runs/'+run+'/process-pricing'
        with patch('batch_process_quote.apply_process_costing',side_effect=AssertionError('Must not recalculate manual financials')):
            status,plan=f.request(path,cookie=self.owner)
        self.assertEqual(status,200,plan);self.assertFalse(plan['can_estimate']);self.assertTrue(plan['already_applied'])
        self.assertEqual(plan['total_price'],saved['totals']['total_price'])
        self.assertEqual(plan['estimated_subtotal'],saved['totals']['total_price'])
        self.assertIsNone(plan['route_proposal']);self.assertIn('不会覆盖',plan['scope_label']);f.assert_no_costs(plan)
        status,value=f.request(path,self.payload(plan,event='manual-cannot-replace'),cookie=self.owner)
        self.assertEqual(status,201,value);self.assertTrue(value['already_applied'])
        self.assertEqual(service.get(self.batch)['result'],saved)


if __name__=='__main__':unittest.main()
