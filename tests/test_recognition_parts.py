"""Part candidates through real workers, durable review and read-only views.

All files and bridge results are synthetic local fixtures. No real model,
network, geometry decomposition, business BOM or quotation is involved.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from workbench_store import Workbench
from test_workbench_evidence_review import EvidenceBridgeFactory, SimulatedWorkerExit


class RecognitionPartsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='recognition-parts-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.factory = EvidenceBridgeFactory()
        self.app = self.new_app()
        self.task = self.app.create({'event_id': 'parts-create', 'inquiry': '仅识别本次上传的多零件图纸。',
                                    'simulated': True})
        self.task_id = self.task['id']
        for index in range(2):
            self.task = self.app.upload(self.task_id, f'parts-upload-{index}', self.task['revision'],
                                        f'packet-rev0{index + 1}.png',
                                        b'\x89PNG\r\n\x1a\nsynthetic part ' + str(index).encode())
        self.sources = [item['id'] for item in self.task['attachments']]
        self.app._launch_read = Mock()
        sleeping = patch('workbench_store.time.sleep', return_value=None)
        sleeping.start()
        self.addCleanup(sleeping.stop)

    def new_app(self):
        return Workbench(self.directory / 'tasks', reader_root=self.directory / 'readers',
                         bridge_factory=self.factory)

    def evidence(self, source=None, *, page=1, region='标题栏'):
        return {'reference': source or self.sources[0], 'page': page, 'region': region}

    def annotation(self, text='4 x Ø12 ±0.1', *, source=None, page=1, label='hole_diameter'):
        return {'name': 'engineering_open_items', 'source_name': label,
                'raw_text': text, 'raw_unit': 'mm', 'status': 'observed', 'reason': None,
                'evidence': self.evidence(source, page=page, region='主视图原始标注')}

    def part(self, key='part-01', number='01', *, name='焊接机箱', version='REV04',
             source=None, page=1, observations=None):
        return {'key': key, 'name': name, 'part_number': number, 'version': version,
                'status': 'pending_review', 'evidence': [self.evidence(source, page=page)],
                'observations': [self.annotation(source=source, page=page)] if observations is None
                else deepcopy(observations)}

    def candidate(self, name='quantity', value=80):
        return {'name': name, 'raw_value': value, 'raw_unit': '件' if name == 'quantity' else None,
                'value': value, 'unit': 'piece' if name == 'quantity' else None,
                'status': 'pending_review', 'evidence': self.evidence()}

    def completed(self, parts=None, *, candidates=None, observations=None, legacy=False):
        value = {'status': 'completed', 'error': None, 'warnings': [],
                 'candidates': [self.candidate()] if candidates is None else deepcopy(candidates),
                 'observations': [] if observations is None else deepcopy(observations)}
        if not legacy:
            value['parts'] = [] if parts is None else deepcopy(parts)
        return value

    def start(self):
        self.task = self.app.task(self.task_id)
        self.task = self.app.start_read(self.task_id, {
            'event_id': 'parts-read', 'expected_revision': self.task['revision']})
        return self.task['run']

    def work(self, app=None):
        app = app or self.app
        app._read_worker(self.task_id, 'parts-read')
        self.task = app.task(self.task_id)
        return self.task['run']

    def run_parts(self, initial, reviewed):
        self.start()
        self.factory.results['main-session'] = [initial]
        self.factory.results['review-session'] = [reviewed]
        run = self.work()
        self.assertEqual(run['status'], 'completed', run.get('error'))
        return self.task['recognition']

    def hashes(self):
        return {path.relative_to(self.directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.directory.rglob('*') if path.is_file()}

    def test_optional_view_coordinates_survive_durable_review_without_changing_raw_text(self):
        first = self.part()
        view = {"label": "轴测图", "reference": self.sources[0], "page": 1,
                "bbox": [100, 100, 600, 500], "hotspots": []}
        first['views'] = [view]
        reviewed = self.part('review-part')
        reviewed['views'] = [{**view, 'reference': self.sources[1]}]
        self.run_parts(self.completed([first]), self.completed([reviewed], candidates=[]))
        saved = self.app._state(self.task_id)['run']['recognition_result']['parts'][0]
        self.assertEqual(saved['views'], [view])
        self.assertEqual(saved['observations'][0]['raw_text'], first['observations'][0]['raw_text'])
        self.assertEqual(saved['status'], 'pending_review')
        again = self.new_app()._state(self.task_id)['run']['recognition_result']['parts'][0]
        self.assertEqual(again, saved)

    def test_bad_optional_views_do_not_make_successful_drawing_run_fail(self):
        for value in (None, {}, [False], [{'url': 'https://example.invalid/drawing.svg'}]):
            part = self.part()
            part['views'] = value
            record = self.app.records.load(self.task_id)
            run = {'source_ids': self.sources}
            result = self.app._parts(record, run, [part], 'initial')[0]
            self.assertEqual(result['views'], [])
            self.assertEqual(result['observations'][0]['raw_text'], part['observations'][0]['raw_text'])

    def test_two_pass_merge_retains_each_parts_raw_text_and_does_not_create_bom_or_single_product(self):
        first = self.part()
        second = self.part('part-02', '02', source=self.sources[1], name='检修门')
        extra = self.annotation('WELD ALL AROUND; ⌀0.2 | A', label='weld_and_gdt')
        reviewed = self.part('second-pass-01', observations=[first['observations'][0], extra])
        recognition = self.run_parts(self.completed([first, second]),
                                     self.completed([reviewed], candidates=[]))
        self.assertTrue(recognition['parts_supported'])
        self.assertEqual(len(recognition['parts']), 2)
        projected = recognition['parts'][0]
        self.assertEqual(projected['part_number'], '01')
        self.assertEqual([row['raw_text'] for row in projected['observations']],
                         ['4 x Ø12 ±0.1', extra['raw_text']])
        self.assertEqual([row['label'] for row in projected['observations']], ['孔径', 'weld_and_gdt'])
        self.assertEqual([row['source_pass'] for row in projected['observations']], ['initial', 'review'])
        self.assertEqual(projected['status'], 'pending_review')
        state = self.app._state(self.task_id)['run']
        self.assertEqual(len(state['initial_result']['parts']), 2)
        self.assertEqual(len(state['review']['result']['parts']), 1)
        self.assertEqual(len(state['recognition_result']['parts']), 2)
        self.assertEqual(state['reading_scope'], 'full_drawing')
        self.assertEqual(len(self.factory.started), 2)
        record = self.app.records.load(self.task_id)
        self.assertTrue(all(slot['effective'] is None for slot in record['fields'].values()))
        event = next(row for row in record['events'] if row['id'] == 'READ-parts-read')
        self.assertEqual(event['payload']['fields'], [])
        self.assertEqual(recognition['candidates'][0]['value'], 80, 'global summary stays visible only')
        self.assertFalse(any('尚未形成' in text for text in recognition['questions']))
        self.assertFalse((self.app.records.task_path(self.task_id) / 'bom.json').exists())
        self.assertEqual(self.task['quotes'], [])

    def test_conflicting_versions_are_separate_and_repeated_keys_become_unique(self):
        recognition = self.run_parts(self.completed([self.part(version='REV04')]),
                                     self.completed([self.part(version='REV02')], candidates=[]))
        self.assertEqual([row['version'] for row in recognition['parts']], ['REV04', 'REV02'])
        self.assertEqual(len({row['key'] for row in recognition['parts']}), 2)
        self.assertIsNone(self.app.records.load(self.task_id)['fields']['quantity']['effective'])

    def test_same_number_and_version_without_shared_original_source_stay_separate(self):
        recognition = self.run_parts(self.completed([self.part()]), self.completed([
            self.part('another-file', source=self.sources[1])], candidates=[]))
        self.assertEqual(len(recognition['parts']), 2)
        self.assertEqual({row['evidence'][0]['reference'] for row in recognition['parts']}, set(self.sources))

    def test_no_number_merges_only_explicit_same_name_version_and_shared_source(self):
        recognition = self.run_parts(self.completed([self.part(number=None)]), self.completed([
            self.part('review-name-match', None, observations=[self.annotation('t = 1.5 mm')])], candidates=[]))
        self.assertEqual(len(recognition['parts']), 1)
        self.assertEqual(len(recognition['parts'][0]['observations']), 2)
        self.assertIsNone(recognition['parts'][0]['part_number'])
        self.assertEqual(self.app.records.load(self.task_id)['fields']['quantity']['effective']['value'], 80,
                         'explicit single-part reads keep legacy pending-candidate behavior')

    def test_failed_review_keeps_first_parts_and_never_applies_packet_total_to_single_product(self):
        first = [self.part(), self.part('part-02', '02', source=self.sources[1])]
        recognition = self.run_parts(self.completed(first), {'status': 'failed', 'error': 'scripted failure'})
        self.assertTrue(recognition['parts_supported'])
        self.assertEqual(len(recognition['parts']), 2)
        self.assertTrue(all(row['status'] == 'pending_review' for row in recognition['parts']))
        self.assertEqual(self.task['run']['review']['status'], 'failed')
        self.assertTrue(any('查漏复读未完成' in value for value in recognition['warnings']))
        self.assertIsNone(self.app.records.load(self.task_id)['fields']['quantity']['effective'])

    def test_first_parts_are_partial_during_review_and_restart_reuses_same_two_sessions(self):
        self.start()
        self.factory.results['main-session'] = [self.completed([self.part()])]
        self.factory.results['review-session'] = [self.completed([
            self.part('part-02', '02', source=self.sources[1])], candidates=[])]
        self.factory.after_session['review-session'] = lambda: (_ for _ in ()).throw(SimulatedWorkerExit())
        with self.assertRaises(SimulatedWorkerExit):
            self.work()
        before = self.hashes()
        interim = self.app.task(self.task_id)
        self.assertTrue(interim['recognition']['partial'])
        self.assertTrue(interim['recognition']['parts_supported'])
        self.assertEqual(len(interim['recognition']['parts']), 1)
        self.assertNotIn('initial_result', interim['run'])
        self.assertEqual(self.hashes(), before)
        self.assertIsNone(self.app.records.load(self.task_id)['fields']['quantity']['effective'])
        restarted = self.new_app()
        run = self.work(restarted)
        self.assertEqual(run['status'], 'completed', run.get('error'))
        self.assertFalse(self.task['recognition']['partial'])
        self.assertEqual(len(self.task['recognition']['parts']), 2)
        self.assertEqual(len(self.factory.started), 2)
        self.assertEqual(self.factory.inspected, ['main-session', 'review-session'])

    def test_repeated_gets_do_not_dispatch_write_or_promote_existing_human_fields(self):
        task = self.app.task(self.task_id)
        self.app.save_fields(self.task_id, 'requirements', {
            'event_id': 'human-before-parts', 'expected_revision': task['revision'],
            'values': {'quantity': 7, 'product_name': '人工保留的原件'}})
        self.run_parts(self.completed([self.part(), self.part('part-02', '02', source=self.sources[1])]),
                       self.completed([], candidates=[]))
        before = self.hashes()
        self.app.bridge_factory = Mock(side_effect=AssertionError('read-only view must not dispatch'))
        with patch('dsh_bridge.DshBridge._rpc', side_effect=AssertionError('RPC prohibited')) as rpc:
            for _ in range(3):
                task = self.app.task(self.task_id)
                self.assertEqual(len(task['recognition']['parts']), 2)
                self.assertEqual(task['fields']['quantity']['value'], 7)
                self.assertEqual(task['fields']['product_name']['value'], '人工保留的原件')
                self.assertEqual(len(self.app.tasks()), 1)
            rpc.assert_not_called()
        self.app.bridge_factory.assert_not_called()
        self.assertEqual(self.hashes(), before)
        self.assertEqual(self.task['quotes'], [])

    def test_legacy_result_never_infers_parts_from_file_names_or_part_listing_annotation(self):
        recognition = self.run_parts(self.completed(observations=[self.annotation(
            '01 / 02 / 32 / 44 / 64, five parts', label='part_listing')], legacy=True),
            self.completed(candidates=[], legacy=True))
        self.assertFalse(recognition['parts_supported'])
        self.assertEqual(recognition['parts'], [])
        run = self.app._state(self.task_id)['run']
        self.assertNotIn('parts', run['initial_result'])
        self.assertNotIn('parts', run['recognition_result'])
        before = self.hashes()
        for _ in range(3):
            self.assertEqual(self.app.task(self.task_id)['recognition']['parts'], [])
        self.assertEqual(self.hashes(), before)

    def test_explicit_empty_parts_are_supported_and_final_empty_list_is_authoritative(self):
        recognition = self.run_parts(self.completed([]), self.completed([], candidates=[]))
        self.assertTrue(recognition['parts_supported'])
        self.assertEqual(recognition['parts'], [])
        state = self.app._state(self.task_id)
        state['run']['initial_result']['parts'] = [self.part()]
        state['run']['recognition_result']['parts'] = []
        self.app._save_state(self.task_id, state)
        self.assertEqual(self.app.task(self.task_id)['recognition']['parts'], [])

    def test_saved_parts_projection_rejects_foreign_sources_and_impossible_image_pages(self):
        self.run_parts(self.completed([self.part()]), self.completed([], candidates=[]))
        state = self.app._state(self.task_id)
        good = deepcopy(state['run']['recognition_result']['parts'][0])
        good['status'] = 'verified'
        foreign = self.part('foreign', source='FILE-not-in-this-task')
        invalid_page = self.part('invalid-page', page=2)
        observation = self.annotation('FOREIGN', source='FILE-not-in-this-task')
        good['observations'].append(observation)
        state['run']['recognition_result']['parts'] = [good, foreign, invalid_page]
        self.app._save_state(self.task_id, state)
        before = self.hashes()
        projected = self.app.task(self.task_id)['recognition']['parts']
        self.assertEqual(len(projected), 1)
        self.assertEqual(projected[0]['status'], 'pending_review')
        self.assertEqual([row['raw_text'] for row in projected[0]['observations']], ['4 x Ø12 ±0.1'])
        self.assertEqual(self.hashes(), before)

    def test_original_pdf_page_is_pinned_from_existing_manifest_and_never_becomes_derived_source(self):
        task = self.app.task(self.task_id)
        task = self.app.upload(self.task_id, 'parts-pdf', task['revision'],
                               'drawing-rev04.pdf', b'%PDF-1.4\nsynthetic fixture; native rendering is not invoked')
        pdf_id = next(row['id'] for row in task['attachments'] if row['name'].endswith('.pdf'))
        self.start()
        record = self.app.records.load(self.task_id)
        workspace = self.app.reader_root / self.task_id / 'parts-read'
        workspace.mkdir(parents=True)
        manifest = {'task_id': self.task_id, 'session_id': 'main-session',
                    'files': [{'id': source, 'sha256': record['sources'][source]['sha256']}
                              for source in [*self.sources, pdf_id]],
                    'pdf_views': {'page_counts': {pdf_id: 2}}}
        (workspace / '.dsh-bridge-run.json').write_text(json.dumps(manifest))
        self.factory.results['main-session'] = [self.completed([self.part(source=pdf_id, page=2)])]
        self.factory.results['review-session'] = [{'status': 'failed', 'error': 'scripted optional review failure'}]
        self.work()
        self.assertEqual(self.task['run']['status'], 'completed', self.task['run'].get('error'))
        projected = self.task['recognition']['parts'][0]
        self.assertEqual(projected['evidence'][0]['reference'], pdf_id)
        self.assertEqual(projected['evidence'][0]['page'], 2)
        self.assertEqual(projected['observations'][0]['evidence']['page'], 2)
        self.assertEqual(self.app._state(self.task_id)['run']['source_page_counts'][pdf_id], 2)
        self.assertEqual(len(self.task['attachments']), 3)
        before = self.hashes()
        self.app.task(self.task_id)
        self.assertEqual(self.hashes(), before)

    def test_invalid_part_page_is_rejected_before_creating_a_read_event(self):
        self.start()
        self.factory.results['main-session'] = [self.completed([self.part(page=2)])]
        run = self.work()
        self.assertEqual(run['status'], 'failed')
        self.assertIn('part page', run['error'])
        self.assertFalse(any(row['id'] == 'READ-parts-read' for row in self.app.records.load(self.task_id)['events']))
        self.assertEqual(len(self.factory.started), 1)

    def test_invalid_review_part_source_cannot_discard_independently_valid_first_parts(self):
        first = [self.part(), self.part('part-02', '02', source=self.sources[1])]
        recognition = self.run_parts(self.completed(first), self.completed([
            self.part('foreign-review', source='FILE-other-task')], candidates=[]))
        self.assertEqual(self.task['run']['review']['status'], 'failed')
        self.assertEqual([row['part_number'] for row in recognition['parts']], ['01', '02'])
        self.assertTrue(all(row['evidence'][0]['reference'] in self.sources for row in recognition['parts']))

    def test_direct_application_keeps_parts_cache_and_does_not_adopt_packet_fields(self):
        self.start()
        state = self.app._state(self.task_id)
        result = self.completed([self.part(), self.part('part-02', '02', source=self.sources[1])])
        with self.app.lock(self.task_id):
            self.app._apply_reading(self.task_id, state['run'], result)
            state['run']['status'] = 'completed'
            self.app._save_state(self.task_id, state)
        projected = self.app.task(self.task_id)['recognition']
        self.assertEqual(len(projected['parts']), 2)
        self.assertEqual(projected['candidates'][0]['value'], 80)
        self.assertIsNone(self.app.records.load(self.task_id)['fields']['quantity']['effective'])
        self.assertEqual(self.factory.started, [])

    def test_part_identity_requires_original_drawing_evidence_and_not_inquiry_or_derived_image_ids(self):
        self.start()
        record, run = self.app.records.load(self.task_id), self.app._state(self.task_id)['run']
        for reference, page in (('inquiry', None), ('DETAIL-pdf-p1-tl', 1), ('PDF-page-2', 1)):
            with self.subTest(reference=reference):
                part = self.part(source=reference, page=page)
                with self.assertRaises(ValueError):
                    self.app._parts(record, run, [part], 'initial')

    def test_pdf_part_without_saved_page_count_cannot_invent_a_page(self):
        task = self.app.task(self.task_id)
        task = self.app.upload(self.task_id, 'uncached-pdf', task['revision'],
                               'uncached.pdf', b'%PDF-1.4\nsynthetic invalid PDF')
        source = next(row['id'] for row in task['attachments'] if row['name'] == 'uncached.pdf')
        self.start()
        record, run = self.app.records.load(self.task_id), self.app._state(self.task_id)['run']
        with self.assertRaisesRegex(ValueError, 'part page'):
            self.app._parts(record, run, [self.part(source=source, page=1)], 'initial')

    def test_parts_cache_survives_revision_conflict_without_overwriting_human_change(self):
        self.start()
        first = [self.part(), self.part('part-02', '02', source=self.sources[1])]
        def changed_during_read():
            task = self.app.task(self.task_id)
            self.app.save_fields(self.task_id, 'requirements', {
                'event_id': 'human-during-parts', 'expected_revision': task['revision'], 'values': {'quantity': 9}})
            return self.completed(first)
        self.factory.results['main-session'] = [changed_during_read]
        self.factory.results['review-session'] = [self.completed([], candidates=[])]
        run = self.work()
        self.assertEqual(run['status'], 'completed', run.get('error'))
        self.assertEqual(len(self.task['recognition']['parts']), 2)
        self.assertEqual(self.task['recognition']['candidates'][0]['value'], 80)
        self.assertEqual(self.task['fields']['quantity']['value'], 9)
        self.assertFalse(run['candidates_applied'])
        self.assertFalse(any(row['id'] == 'READ-parts-read' for row in self.app.records.load(self.task_id)['events']))


if __name__ == '__main__':
    unittest.main()
