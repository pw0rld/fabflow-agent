"""Saved CAD evidence checks must not invent or confirm engineering facts."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cad_bom import group_quantity_summary
from cad_engineering_review import project, STATUSES
from test_cad_bom import material
from test_cad_bend_contract import bend_profile, no_bends
from test_cad_laser_contract import laser_profile, unsupported_profile


def group(gid='P0001', ids=None, kind='make'):
    return {'id': gid, 'name': '候选零件', 'solid_ids': ids or ['S0001'],
            'classification': kind, 'status': 'pending_review', 'quantity_per_set': None,
            'material': None, 'thickness_mm': None, 'notes': ''}


def thickness(value=1):
    return {'status': 'candidate', 'value_mm': value,
            'evidence': [{'face_ids': ['F0001', 'F0002'], 'thickness_mm': value}],
            'reason': '原生面间距离', 'warnings': []}


def state(groups=None):
    return {'status': 'ready', 'geometry_url': '/native', 'groups': groups or [group()],
            'material_measurements': {}, 'thickness_measurements': {},
            'quantity_candidates': {}, 'bom_settings': {'order_sets': None},
            'laser_profiles': {}, 'bend_profiles': {}}


def item(report, field, gid='P0001'):
    return next(row for row in report['items'] if row['field'] == field and row['group_id'] == gid)


class CadEngineeringReviewTests(unittest.TestCase):
    def test_ai_or_default_classification_is_not_attributed_to_a_human(self):
        for kind in ('make', 'buy', 'unclassified', 'exclude'):
            source = state([group(kind=kind)])
            source['ai'] = {'status': 'completed', 'provenance': {'origin': 'AI suggestion'}}
            row = item(project(source), 'classification')
            self.assertEqual(row['evidence'][0]['value'], kind)
            self.assertEqual(row['evidence'][0]['basis'], 'saved_record')
            self.assertNotEqual(row['status'], 'verified')

    def test_unstarted_and_partial_are_not_complete_reviews(self):
        result = project({'status': 'not_started', 'groups': []})
        self.assertEqual(result['status'], 'not_started')
        self.assertEqual(result['items'], [])
        for status in ('running', 'failed', 'queued'):
            source = state(); source['status'] = status
            result = project(source)
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(item(result, 'snapshot', None)['status'], 'not_evaluated')
        self.assertEqual(project(state())['status'], 'current')

    def test_missing_saved_fields_do_not_assert_source_omissions_or_zeroes(self):
        report = project(state())
        for field in ('material', 'thickness_mm', 'quantity_per_set'):
            row = item(report, field)
            self.assertEqual(row['status'], 'missing')
            self.assertEqual(row['evidence'], [])
        self.assertEqual(item(report, 'order_sets', None)['status'], 'missing')
        self.assertIn('当前保存结果', item(report, 'material')['message'])
        self.assertEqual(item(report, 'drawing_requirements', None)['status'], 'not_evaluated')
        self.assertNotIn('订单总件数', {row['field'] for row in report['items']})

    def test_material_conflict_and_partial_members_cannot_use_first_value(self):
        source = state([group(ids=['S0001', 'S0002'])])
        source['material_measurements'] = {'S0001': material('Steel'), 'S0002': material('Aluminium')}
        source['groups'][0]['material'] = '人工已填牌号'
        row = item(project(source), 'material')
        self.assertEqual(row['status'], 'conflict')
        self.assertEqual({row['solid_id'] for row in row['evidence'] if 'solid_id' in row}, {'S0001', 'S0002'})
        source['material_measurements'].pop('S0002')
        source['groups'][0]['material'] = None
        row = item(project(source), 'material')
        self.assertEqual(row['status'], 'pending_review')
        self.assertIn('部分实体', row['message'])

    def test_material_grade_refinement_does_not_become_false_numerical_conflict(self):
        source = state()
        source['material_measurements'] = {'S0001': material('Steel')}
        row = item(project(source), 'material')
        self.assertEqual(row['status'], 'pending_review')
        self.assertTrue(all('Q235' not in str(value) for value in row.values()))
        source['groups'][0]['material'] = 'Q235B'
        row = item(project(source), 'material')
        self.assertEqual(row['status'], 'pending_review')
        self.assertEqual(row['evidence'][0], {'basis': 'manual_entry', 'value': 'Q235B'})
        self.assertIn('Steel', row['evidence'][1]['raw_text'])

    def test_thickness_conflict_survives_filled_or_confirmed_group(self):
        source = state([group(ids=['S0001', 'S0002'])])
        source['thickness_measurements'] = {'S0001': thickness(1), 'S0002': thickness(2)}
        source['groups'][0].update(thickness_mm=1, status='confirmed')
        row = item(project(source), 'thickness_mm')
        self.assertEqual(row['status'], 'conflict')
        self.assertEqual([entry['value'] for entry in row['evidence']], [1, 1, 2])
        source['thickness_measurements']['S0002'] = thickness(1)
        self.assertEqual(item(project(source), 'thickness_mm')['status'], 'recorded')
        source['groups'][0]['thickness_mm'] = 1.5
        self.assertEqual(item(project(source), 'thickness_mm')['status'], 'conflict')

    def test_partial_thickness_and_not_applicable_remain_distinct(self):
        source = state([group(ids=['S0001', 'S0002'])])
        source['thickness_measurements'] = {'S0001': thickness(1)}
        row = item(project(source), 'thickness_mm')
        self.assertEqual(row['status'], 'pending_review')
        self.assertIn('部分实体', row['message'])
        source['thickness_measurements'] = {sid: {'status': 'not_applicable', 'value_mm': None,
            'evidence': []} for sid in ['S0001', 'S0002']}
        self.assertEqual(item(project(source), 'thickness_mm')['status'], 'not_evaluated')

    def test_each_group_is_isolated_and_confirmation_does_not_verify_parameters(self):
        source = state([group(), group('P0002', ['S0002'])])
        source['groups'][0].update(material='Steel', thickness_mm=1, quantity_per_set=3, status='confirmed')
        report = project(source)
        for field in ('material', 'thickness_mm', 'quantity_per_set'):
            self.assertEqual(item(report, field)['status'], 'recorded')
            self.assertEqual(item(report, field, 'P0002')['status'], 'missing')
        self.assertIn('不等于', item(report, 'thickness_mm')['message'])
        self.assertTrue(all(row['status'] != 'confirmed' for row in report['items']))

    def test_quantity_uses_only_existing_instance_proof_never_node_or_mesh_count(self):
        source = state([group(ids=['S0001', 'S0002'])])
        source['geometry_summary'] = {'solids': 80, 'nodes': 10}
        self.assertEqual(item(project(source), 'quantity_per_set')['status'], 'missing')
        identities = {sid: {'definition_id': 'same', 'occurrence_path': ['root', sid]}
                      for sid in ['S0001', 'S0002']}
        source['quantity_candidates'] = {'P0001': group_quantity_summary(source['groups'][0], identities)}
        row = item(project(source), 'quantity_per_set')
        self.assertEqual(row['status'], 'pending_review')
        self.assertEqual(len(row['evidence']), 3)
        self.assertEqual(row['evidence'][0]['value'], 2)
        self.assertTrue(all(row['basis'] == 'assembly_instance' for row in row['evidence']))
        source['groups'][0]['quantity_per_set'] = 3
        source['bom_settings']['order_sets'] = 10
        self.assertEqual(item(project(source), 'quantity_per_set')['status'], 'conflict')
        self.assertEqual(item(project(source), 'order_sets', None)['evidence'][0]['value'], 10)

    def test_laser_and_bend_candidates_are_drafts_and_absence_is_not_no_feature(self):
        source = state()
        source['laser_profiles'] = {'S0001': laser_profile()}
        source['bend_profiles'] = {'S0001': bend_profile()}
        report = project(source)
        self.assertEqual(item(report, 'laser_profile')['status'], 'pending_review')
        row = item(report, 'bend_features')
        self.assertEqual(row['status'], 'pending_review')
        self.assertIn('方向', row['message'])
        self.assertEqual(row['evidence'][0]['face_ids'], ['F0001', 'F0002'])
        self.assertEqual(row['evidence'][0]['value'], 90)
        source['laser_profiles']['S0001'] = unsupported_profile()
        source['bend_profiles']['S0001'] = no_bends('unresolved')
        report = project(source)
        self.assertEqual(item(report, 'laser_profile')['status'], 'not_evaluated')
        self.assertEqual(item(report, 'bend_features')['status'], 'not_evaluated')
        self.assertIn('未识别不能', item(report, 'bend_features')['message'])

    def test_excluded_and_purchased_groups_do_not_claim_manufacturing_checks(self):
        source = state([group(kind='exclude'), group('P0002', ['S0002'], 'buy')])
        report = project(source)
        self.assertEqual(len([row for row in report['items'] if row['group_id'] == 'P0001']), 1)
        self.assertFalse(any(row['field'] in ('laser_profile', 'bend_features')
                             for row in report['items'] if row['group_id'] == 'P0002'))

    def test_projection_is_immutable_deterministic_bounded_to_saved_evidence(self):
        source = state(); source['material_measurements'] = {'S0001': material()}
        before = deepcopy(source)
        first = project(source); second = project(source)
        self.assertEqual(first, second)
        self.assertEqual(source, before)
        self.assertEqual(first['schema_version'], 1)
        self.assertEqual(set(first['summary']), set(STATUSES))
        self.assertEqual(sum(first['summary'].values()), len(first['items']))
        self.assertEqual(len({row['id'] for row in first['items']}), len(first['items']))
        first['items'][0]['message'] = 'caller changed result'
        self.assertEqual(source, before)

    def test_candidate_occurrences_remain_evidence_without_unavailable_solid_mapping(self):
        source = state()
        source['quantity_candidates'] = {'P0001': {
            'status': 'candidate', 'value': 2, 'occurrence_ids': ['["root","a"]', '["root","b"]'],
            'reason': '两处独立装配实例'}}
        evidence = item(project(source), 'quantity_per_set')['evidence']
        self.assertEqual(evidence[0]['value'], 2)
        self.assertEqual([row['raw_text'] for row in evidence[1:]], ['["root","a"]', '["root","b"]'])
        self.assertFalse(any('solid_id' in row for row in evidence))

    def test_display_evidence_is_capped_while_count_and_full_saved_source_remain(self):
        source = state()
        measurement = material()
        measurement['entries'] *= 100
        source['material_measurements'] = {'S0001': measurement}
        report = project(source)
        row = item(report, 'material')
        self.assertEqual(len(row['evidence']), 32)
        self.assertEqual(row['evidence_count'], 100)
        self.assertEqual(len(source['material_measurements']['S0001']['entries']), 100)

    def test_real_service_public_state_is_supported_without_changing_persisted_files(self):
        from cad_documents import CadDocumentStore
        from cad_manufacturing import CadManufacturingService
        from test_cad_manufacturing import STEP, native_properties_fixture, ready

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = CadDocumentStore(root / 'cad')
            document = store.create('review-fixture.step', STEP)
            service = CadManufacturingService(store, native_runner=native_properties_fixture)
            service.start(document['id'], {'expected_revision': 0})
            saved = ready(service, document['id'])
            self.assertEqual(saved['status'], 'ready')

            def hashes():
                return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in root.rglob('*') if path.is_file()}

            before = hashes()
            report = project(saved)
            self.assertEqual(report, project(service.get(document['id'])))
            self.assertEqual(hashes(), before)
            self.assertEqual(report['status'], 'current')
            self.assertEqual(len({row['group_id'] for row in report['items'] if row['group_id']}), 2)


if __name__ == '__main__':
    unittest.main()
