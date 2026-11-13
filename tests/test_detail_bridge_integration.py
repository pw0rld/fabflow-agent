"""Real overview/detail preparation and bridge provenance with fake RPC only.

Every PDF is generated in a temporary directory. The replies are scripted;
these contracts verify source mapping and preservation, not model accuracy.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
from dsh_bridge import MANIFEST
from test_dsh_bridge import FakeBridge, candidate, event, observation
from test_workbench_pdf_review import PdfReviewRpcFactory
from workbench_store import Workbench

try:
    from PIL import Image
    import pypdfium2 as pdfium
except ImportError:
    Image = pdfium = None


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(Image is not None and pdfium is not None,
                     'Pillow and pypdfium2 required for real drawing-detail integration')
class DetailBridgeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='detail-bridge-integration-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / 'isolated-reader'
        self.workspace.mkdir()
        self.pdf = self.workspace / 'two-page-drawing.pdf'
        document = pdfium.PdfDocument.new()
        try:
            for width, height in ((144, 108), (180, 144)):
                page = document.new_page(width, height)
                page.gen_content()
                page.close()
            document.save(self.pdf)
        finally:
            document.close()
        self.files = [{'id': 'drawing-original', 'path': str(self.pdf),
                       'original_name': '本地生成两页图纸.pdf'}]
        self.inquiry = '本地来源映射夹具，没有真实客户数据或制造订单。'
        self.output = self.workspace / 'candidates.json'
        self.bridge = FakeBridge(self.workspace)

    def start(self):
        started = self.bridge.start('TASK-DETAIL', self.files, self.inquiry, self.output)
        self.assertEqual(started['status'], 'running')
        self.sid = started['session_id']
        return json.loads((self.workspace / MANIFEST).read_text())

    @staticmethod
    def second_page_detail(run):
        return next(view for view in run['detail_views']['views'] if view['page'] == 2)

    @staticmethod
    def payload(reference, page=1):
        evidence = {'reference': reference, 'page': page,
                    'region': '侧视图板厚引线附近（本地脚本回复）'}
        return {
            'candidates': [candidate(raw='1.5', value='1.5', raw_unit='mm', unit='mm',
                                     evidence=deepcopy(evidence))],
            'observations': [observation(name='尺寸公差', raw_text='Ø6 ±0.1 mm（本地模拟）',
                                         raw_unit='mm', status='observed',
                                         evidence=deepcopy(evidence))],
            'warnings': [],
        }

    def finish(self, payload, *, bridge=None, session_id=None):
        bridge = bridge or self.bridge
        text = json.dumps(payload, ensure_ascii=False)
        bridge.events = [event(1, 'turn/start'),
                         event(2, 'assistant/message', message={'content': [{'type': 'text', 'text': text}]}),
                         event(3, 'turn/end', reason={'kind': 'completed'})]
        return bridge.inspect(session_id or self.sid)

    def test_new_prompt_contains_every_original_page_and_all_overlapping_details(self):
        original = self.pdf.read_bytes()
        run = self.start()
        self.assertEqual([source['id'] for source in run['files']], ['drawing-original'])
        self.assertEqual(run['pdf_views']['page_counts'], {'drawing-original': 2})
        self.assertEqual(len(run['pdf_views']['views']), 2)
        self.assertEqual(run['detail_views']['page_counts'], {'drawing-original': 2})
        self.assertEqual(len(run['detail_views']['views']), 8)
        prompt = next(payload for method, payload, _ in self.bridge.calls
                      if method == 'session.prompt')['content'][0]['text']
        for page in (1, 2):
            rows = [view for view in run['detail_views']['views'] if view['page'] == page]
            self.assertEqual(len(rows), 4)
            self.assertEqual(len({view['region'] for view in rows}), 4)
        for view in run['pdf_views']['views'] + run['detail_views']['views']:
            self.assertIn(view['id'], prompt)
            self.assertIn(str(self.workspace / view['path']), prompt)
            self.assertEqual(sha(self.workspace / view['path']), view['sha256'])
        self.assertIn('整页概览', prompt)
        self.assertIn('重叠细节图', prompt)
        self.assertEqual(self.pdf.read_bytes(), original)

    def test_detail_evidence_maps_candidate_and_fine_observation_to_original_page_and_region(self):
        run = self.start()
        detail = self.second_page_detail(run)
        payload = self.payload(detail['id'])
        result = self.finish(payload)
        self.assertEqual(result['status'], 'completed', result.get('error'))
        for group in ('candidates', 'observations'):
            evidence = result[group][0]['evidence']
            self.assertEqual(evidence['reference'], 'drawing-original')
            self.assertEqual(evidence['page'], 2)
            self.assertEqual(evidence['region'], detail['region'] + '；' + payload[group][0]['evidence']['region'])
        self.assertEqual(result['candidates'][0]['status'], 'pending_review')
        self.assertEqual(result['observations'][0]['name'], 'engineering_open_items')
        self.assertEqual(result['observations'][0]['source_name'], '尺寸公差')
        self.assertEqual(result['observations'][0]['raw_text'], 'Ø6 ±0.1 mm（本地模拟）')
        saved = self.output.read_bytes()
        self.assertEqual(self.bridge.inspect(self.sid)['status'], 'completed')
        self.assertEqual(self.output.read_bytes(), saved)
        self.assertIn(detail['id'], result['text'], 'raw scripted response is preserved separately from mapped evidence')

    def test_detail_image_page_two_is_rejected_for_either_group_without_partial_artifact(self):
        run = self.start()
        detail = self.second_page_detail(run)
        for group in ('candidates', 'observations'):
            with self.subTest(group=group):
                payload = self.payload(detail['id'], 2)
                payload['observations' if group == 'candidates' else 'candidates'] = []
                result = self.finish(payload)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['error']['code'], 'invalid-candidates')
                self.assertEqual(result['candidates'], [])
                self.assertFalse(self.output.exists())

    def test_tampered_crop_refuses_cached_start_and_inspect_without_repair_or_dispatch(self):
        run = self.start()
        detail = self.second_page_detail(run)
        crop = self.workspace / detail['path']
        crop.write_bytes(crop.read_bytes() + b'changed after dispatch')
        broken = crop.read_bytes()
        manifest = (self.workspace / MANIFEST).read_bytes()
        restarted = FakeBridge(self.workspace)
        with patch('dsh_bridge.prepare_detail_views', side_effect=AssertionError('tampered cache must not be regenerated')):
            with self.assertRaises(ValueError):
                restarted.start('TASK-DETAIL', self.files, self.inquiry, self.output)
        self.assertEqual(restarted.calls, [])
        result = self.finish(self.payload(detail['id']))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error']['code'], 'invalid-candidates')
        self.assertFalse(self.output.exists())
        self.assertEqual(crop.read_bytes(), broken)
        self.assertEqual((self.workspace / MANIFEST).read_bytes(), manifest)
        self.assertEqual(sum(method == 'session.create' for method, _, _ in self.bridge.calls), 1)
        self.assertEqual(sum(method == 'session.prompt' for method, _, _ in self.bridge.calls), 1)

    def test_existing_pdf_run_without_details_replays_successful_bytes_and_never_prepares_details(self):
        # Create the exact prior PDF-only protocol, then upgrade the adapter.
        with patch('dsh_bridge.prepare_detail_views', return_value=None):
            run = self.start()
        self.assertNotIn('detail_views', run)
        view = next(view for view in run['pdf_views']['views'] if view['page'] == 2)
        self.assertEqual(self.finish(self.payload(view['id']))['status'], 'completed')
        protected = [self.workspace / MANIFEST, self.output, self.pdf]
        protected += [self.workspace / row['path'] for row in run['pdf_views']['views']]
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected}
        restarted = FakeBridge(self.workspace)
        restarted.events = deepcopy(self.bridge.events)
        with patch('dsh_bridge.prepare_detail_views', side_effect=AssertionError('legacy run must not acquire details')) as details:
            with patch('dsh_bridge.prepare_pdf_views', side_effect=AssertionError('legacy PDF pages must not rerender')) as overviews:
                replay = restarted.start('TASK-DETAIL', self.files, self.inquiry, self.output)
                self.assertTrue(replay['requires_inspect'])
                self.assertEqual(replay['session_id'], self.sid)
                self.assertEqual(restarted.inspect(self.sid)['status'], 'completed')
                details.assert_not_called()
                overviews.assert_not_called()
        self.assertEqual({path: (path.read_bytes(), path.stat().st_mtime_ns) for path in protected}, before)
        self.assertFalse((self.workspace / '.drawing-details').exists())
        self.assertFalse(any(method in {'session.create', 'session.prompt'} for method, _, _ in restarted.calls))

    def test_real_worker_review_reuses_all_pinned_overviews_and_details_without_second_render(self):
        factory = PdfReviewRpcFactory()
        app = Workbench(self.root / 'tasks', reader_root=self.root / 'readers', bridge_factory=factory)
        task = app.create({'event_id': 'detail-create', 'inquiry': self.inquiry, 'simulated': True})
        task_id = task['id']
        task = app.upload(task_id, 'detail-upload', task['revision'],
                          '本地两页图纸.pdf', self.pdf.read_bytes())
        source_id = task['attachments'][0]['id']
        before = app.records.load(task_id)
        app._launch_read = Mock()
        run_id = 'detail-read'
        app.start_read(task_id, {'event_id': run_id, 'expected_revision': task['revision']})
        native_run = subprocess.run
        with patch('subprocess.run', wraps=native_run) as rendered:
            app._read_worker(task_id, run_id)
        task = app.task(task_id)
        self.assertEqual(task['run']['status'], 'completed', task['run'].get('error'))
        self.assertEqual(task['run']['review']['status'], 'completed')
        self.assertEqual(sorted(Path(call.args[0][1]).name for call in rendered.call_args_list),
                         ['reading_details.py', 'reading_inputs.py'],
                         'only initial overview and detail generation may invoke decoders')
        workspace = app.reader_root / task_id / run_id
        review = workspace / 'review'
        initial = json.loads((workspace / MANIFEST).read_text())
        reviewed = json.loads((review / MANIFEST).read_text())
        self.assertNotEqual(initial['session_id'], reviewed['session_id'])
        for bundle in ('pdf_views', 'detail_views'):
            self.assertEqual(initial[bundle], reviewed[bundle])
            for view in initial[bundle]['views']:
                self.assertEqual((workspace / view['path']).read_bytes(),
                                 (review / view['path']).read_bytes())
                self.assertEqual(sha(review / view['path']), view['sha256'])
        for bundle in ('.pdf-reading', '.drawing-details'):
            self.assertEqual((workspace / bundle / 'manifest.json').read_bytes(),
                             (review / bundle / 'manifest.json').read_bytes())
        self.assertEqual(len(initial['detail_views']['views']), 8)
        self.assertEqual(app.records.load(task_id)['sources'], before['sources'])
        self.assertEqual([item['id'] for item in task['attachments']], [source_id])
        self.assertEqual(task['quotes'], [])


if __name__ == '__main__':
    unittest.main()
