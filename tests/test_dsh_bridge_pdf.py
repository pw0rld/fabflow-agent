"""Real local PDF pages + fake DSH RPC; no model, network or customer data."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
from dsh_bridge import MANIFEST
from test_dsh_bridge import FakeBridge, candidate, event, observation

try:
    from PIL import Image
    import pypdfium2 as pdfium
except ImportError:
    Image = pdfium = None


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(Image is not None and pdfium is not None,
                     'Pillow and pypdfium2 required for real bridge PDF contracts')
class BridgePdfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.pdf = self.write_pdf(2)
        self.png = self.workspace / 'summary.png'
        with Image.new('RGB', (128, 64), '#557da4') as image:
            image.save(self.png, format='PNG')
        self.files = [
            {'id': 'drawing-pdf', 'path': str(self.pdf), 'original_name': '两页原始图纸.pdf'},
            {'id': 'drawing-png', 'path': str(self.png), 'original_name': '汇总图.png'},
        ]
        self.output = self.workspace / 'candidates.json'
        self.bridge = FakeBridge(self.workspace)
        self.inquiry = '本地PDF读取兼容性测试；没有实际制造数量或业务结论。'

    def write_pdf(self, count):
        path = self.workspace / 'drawing.pdf'
        document = pdfium.PdfDocument.new()
        try:
            for index in range(count):
                page = document.new_page(144 + index, 72 + index)
                page.gen_content()
                page.close()
            document.save(path)
        finally:
            document.close()
        return path

    def start(self, **kwargs):
        result = self.bridge.start('TASK-PDF', self.files, self.inquiry, self.output, **kwargs)
        self.assertEqual(result['status'], 'running')
        self.sid = result['session_id']
        return json.loads((self.workspace / MANIFEST).read_text())

    def local_view(self, view, workspace=None):
        path = Path(view['path'])
        self.assertFalse(path.is_absolute(), 'PDF manifest views must remain run-relative')
        return (workspace or self.workspace) / path

    def second_page(self, run):
        return next(view for view in run['pdf_views']['views']
                    if view['origin_id'] == 'drawing-pdf' and view['page'] == 2)

    def payload(self, reference, page):
        evidence = {'reference': reference, 'page': page,
                    'region': '本地模拟回复的页码映射位置，并非真实读图结论'}
        value = candidate(raw='3.2', value='3.2', raw_unit='mm', unit='mm',
                          evidence=deepcopy(evidence), reason='仅测试原页来源映射')
        seen = observation(raw_text='3.2 mm（本地模拟返回）', raw_unit='mm',
                           status='observed', evidence=deepcopy(evidence))
        return {'candidates': [value], 'observations': [seen], 'warnings': []}

    def finish(self, payload, bridge=None, sid=None):
        bridge = bridge or self.bridge
        text = '```json\n' + json.dumps(payload, ensure_ascii=False) + '\n```'
        bridge.events = [event(1, 'turn/start'),
                         event(2, 'assistant/message', message={'content': [{'type': 'text', 'text': text}]}),
                         event(3, 'turn/end', reason={'kind': 'completed'})]
        return bridge.inspect(sid or self.sid), text

    def assert_no_dispatch(self):
        self.assertEqual(self.bridge.calls, [], 'preprocessing failure must precede every RPC')
        self.assertFalse((self.workspace / MANIFEST).exists())
        self.assertFalse(self.output.exists())

    def test_mixed_prompt_replaces_pdf_with_every_page_but_manifest_keeps_originals(self):
        originals = {path: path.read_bytes() for path in (self.pdf, self.png)}
        run = self.start()
        self.assertEqual([item['id'] for item in run['files']], ['drawing-pdf', 'drawing-png'])
        self.assertEqual(Path(run['files'][0]['path']), self.pdf)
        self.assertEqual(run['files'][0]['sha256'], sha(self.pdf))
        data = run['pdf_views']
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['converter'], 'order-views-1')
        self.assertEqual(data['page_counts'], {'drawing-pdf': 2})
        self.assertEqual(data['sources'], [{'id': item['id'], 'sha256': sha(Path(item['path'])),
                                           'original_name': item['original_name']} for item in self.files])
        self.assertEqual(len(data['views']), 2)
        prompt = next(call for call in self.bridge.calls if call[0] == 'session.prompt')[1]['content'][0]['text']
        self.assertNotIn(str(self.pdf), prompt)
        self.assertIn(str(self.png), prompt)
        self.assertIn('drawing-pdf', prompt)
        for page_number, view in enumerate(data['views'], 1):
            with self.subTest(page=page_number):
                self.assertEqual(view['origin_id'], 'drawing-pdf')
                self.assertEqual(view['page'], page_number)
                self.assertEqual(view['original_sha256'], sha(self.pdf))
                self.assertIn(view['id'], prompt)
                self.assertIn(self.local_view(view).name, prompt)
                self.assertIn(view['original_name'], prompt)
                self.assertEqual(sha(self.local_view(view)), view['sha256'])
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_second_page_alias_maps_candidate_and_observation_to_original_pdf_page(self):
        run = self.start()
        view = self.second_page(run)
        result, text = self.finish(self.payload(view['id'], 1))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['text'], text, 'raw model response must remain unchanged')
        for group in ('candidates', 'observations'):
            self.assertEqual(result[group][0]['evidence']['reference'], 'drawing-pdf')
            self.assertEqual(result[group][0]['evidence']['page'], 2)
        self.assertEqual(result['candidates'][0]['status'], 'pending_review')
        artifact = json.loads(self.output.read_text())
        self.assertEqual(artifact['text'], text)
        self.assertEqual(artifact['candidates'][0]['evidence']['page'], 2)
        self.assertEqual(artifact['observations'][0]['evidence']['reference'], 'drawing-pdf')
        before = self.output.read_bytes()
        repeated = self.bridge.inspect(self.sid)
        self.assertEqual(repeated['status'], 'completed')
        self.assertEqual(self.output.read_bytes(), before)

    def test_original_pdf_reference_can_name_its_real_second_page(self):
        self.start()
        result, _ = self.finish(self.payload('drawing-pdf', 2))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['candidates'][0]['evidence']['reference'], 'drawing-pdf')
        self.assertEqual(result['candidates'][0]['evidence']['page'], 2)

    def test_alias_page_and_original_pdf_page_range_are_strict(self):
        run = self.start()
        alias = self.second_page(run)['id']
        invalid = [(alias, 2), (alias, 0), (alias, True), (alias, '1'),
                   ('drawing-pdf', 0), ('drawing-pdf', 3), ('drawing-pdf', True),
                   ('PDFPAGE-unknown-2', 1)]
        for reference, page in invalid:
            for group in ('candidates', 'observations'):
                with self.subTest(reference=reference, page=page, group=group):
                    payload = self.payload(reference, page)
                    payload['observations' if group == 'candidates' else 'candidates'] = []
                    with self.assertRaises(ValueError):
                        self.bridge._candidates(json.dumps(payload), run)
        self.assertFalse(self.output.exists())

    def test_changed_rendered_page_is_rejected_before_output_creation(self):
        run = self.start()
        page = self.second_page(run)
        self.local_view(page).write_bytes(b'changed derived PNG')
        result, _ = self.finish(self.payload(page['id'], 1))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'invalid-candidates')
        self.assertFalse(self.output.exists())
        self.assertEqual(sum(call[0] == 'session.create' for call in self.bridge.calls), 1)

    def test_changed_original_pdf_is_rejected_even_with_intact_page_images(self):
        run = self.start()
        self.pdf.write_bytes(self.pdf.read_bytes() + b'\n% modified after dispatch\n')
        result, _ = self.finish(self.payload(self.second_page(run)['id'], 1))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'invalid-candidates')
        self.assertFalse(self.output.exists())

    def test_restarting_start_reuses_session_without_render_rpc_or_byte_changes(self):
        run = self.start()
        protected = [self.workspace / MANIFEST, self.pdf, self.png]
        protected += [self.local_view(view) for view in run['pdf_views']['views']]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected}
        restarted = FakeBridge(self.workspace)
        with patch('subprocess.run', side_effect=AssertionError('replay must not rerender')):
            replay = restarted.start('TASK-PDF', self.files, self.inquiry, self.output)
        self.assertEqual(replay['session_id'], self.sid)
        self.assertTrue(replay['requires_inspect'])
        self.assertEqual(restarted.calls, [])
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected}, before)

    def test_legacy_pdf_manifest_and_saved_artifact_are_never_upgraded_in_place(self):
        legacy = {'schema_version': 1, 'task_id': 'TASK-PDF', 'session_id': 'session-legacy-pdf',
                  'files': [{**item, 'sha256': sha(Path(item['path']))} for item in self.files],
                  'inquiry': self.inquiry, 'output_path': str(self.output),
                  'prompt_rpc_id': 'bridge-prompt-legacy', 'phase': 'accepted'}
        manifest = self.workspace / MANIFEST
        manifest.write_text(json.dumps(legacy, ensure_ascii=False, indent=2) + '\n')
        self.sid = legacy['session_id']
        self.bridge.sid = self.sid
        first, _ = self.finish({'candidates': [], 'warnings': ['Local legacy fixture; no actual inference.']})
        self.assertEqual(first['status'], 'completed')
        before_manifest, before_output = manifest.read_bytes(), self.output.read_bytes()
        restarted = FakeBridge(self.workspace)
        with patch('subprocess.run', side_effect=AssertionError('legacy inputs must not be rendered')):
            again = restarted.start('TASK-PDF', self.files, self.inquiry, self.output)
            restarted.events = deepcopy(self.bridge.events)
            second = restarted.inspect(self.sid)
        self.assertTrue(again['requires_inspect'])
        self.assertEqual(second['status'], 'completed')
        self.assertEqual(manifest.read_bytes(), before_manifest)
        self.assertEqual(self.output.read_bytes(), before_output)
        self.assertNotIn('pdf_views', json.loads(manifest.read_text()))
        self.assertFalse((self.workspace / '.pdf-reading').exists())
        self.assertFalse(any(call[0] in {'session.create', 'session.prompt'} for call in restarted.calls))

    def test_invalid_pdf_does_not_fall_back_to_summary_png_and_create_a_session(self):
        self.pdf.write_bytes(b'%PDF-1.7\ntruncated input')
        with self.assertRaises(ValueError):
            self.bridge.start('TASK-PDF', self.files, self.inquiry, self.output)
        self.assert_no_dispatch()

    def test_total_page_view_budget_counts_the_other_image_before_dispatch(self):
        self.write_pdf(32)  # 32 PDF pages plus the summary PNG exceed the 32 input budget.
        with self.assertRaises(ValueError):
            self.bridge.start('TASK-PDF', self.files, self.inquiry, self.output)
        self.assert_no_dispatch()

    def test_review_copies_exact_first_pass_views_without_rerendering_or_losing_mapping(self):
        from reading_inputs import copy_pdf_views
        from reading_details import copy_detail_views
        run = self.start()
        review_directory = self.workspace / 'review'
        attachments = review_directory / 'attachments'
        attachments.mkdir(parents=True)
        copied = []
        for item in run['files']:
            target = attachments / Path(item['path']).name
            shutil.copyfile(item['path'], target)
            copied.append({**item, 'path': str(target)})
        review = FakeBridge(review_directory)
        with patch('subprocess.run', side_effect=AssertionError('review must reuse exact first-pass PNG bytes')):
            pdf_views = copy_pdf_views(self.workspace, review_directory, run['files'], copied)
            copy_detail_views(self.workspace, review_directory, run['files'], copied,
                              run.get('detail_views'), pdf_views)
            started = review.start('TASK-PDF', copied, self.inquiry,
                                   review_directory / 'candidates.json', review_fields=['thickness'])
        self.assertEqual(started['status'], 'running')
        reviewed_run = json.loads((review_directory / MANIFEST).read_text())
        self.assertEqual(reviewed_run['pdf_views']['page_counts'], run['pdf_views']['page_counts'])
        self.assertEqual([(view['id'], view['origin_id'], view['page'], view['sha256'])
                          for view in reviewed_run['pdf_views']['views']],
                         [(view['id'], view['origin_id'], view['page'], view['sha256'])
                          for view in run['pdf_views']['views']])
        for view in reviewed_run['pdf_views']['views']:
            self.assertEqual(sha(self.local_view(view, review_directory)), view['sha256'])
        result, _ = self.finish(self.payload(self.second_page(reviewed_run)['id'], 1),
                                bridge=review, sid=started['session_id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['candidates'][0]['evidence']['reference'], 'drawing-pdf')
        self.assertEqual(result['candidates'][0]['evidence']['page'], 2)


if __name__ == '__main__':
    unittest.main()
