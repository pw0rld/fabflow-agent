"""Real single-task worker/PDF/review integration with fake in-process RPC only.

The tiny PDF is generated locally. Candidate text is scripted to exercise source
provenance, not actual drawing interpretation. No network, model or customer data.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from dsh_bridge import DshBridge, MANIFEST
import reading_inputs
from workbench_store import Workbench

try:
    import pypdfium2 as pdfium
    from PIL import Image
except ImportError:
    pdfium = Image = None


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PdfReviewRpcFactory:
    """Replace RPC exclusively; start/inspect/render/review/record code is real."""
    def __init__(self):
        self.calls = []

    def __call__(self, workspace):
        factory = self

        class Bridge(DshBridge):
            def __init__(self):
                super().__init__('http://127.0.0.1:3080', workspace)

            def _rpc(self, method, payload, **kwargs):
                factory.calls.append({'workspace': self.workspace, 'method': method,
                                      'payload': deepcopy(payload)})
                if method == 'session.create':
                    return {'sessionId': payload['sessionId']}
                if method == 'session.prompt':
                    return {'accepted': True}
                if method != 'session.history':
                    raise AssertionError('Unexpected RPC in completed local fixture: ' + method)
                run = self._run(payload['sessionId'])
                view = next(item for item in run['pdf_views']['views'] if item['page'] == 2)
                evidence = {'reference': view['id'], 'page': 1,
                            'region': '本地脚本模拟第二页标注，仅验证来源映射'}

                def candidate(name, value, *, raw_unit=None, unit=None):
                    return {'name': name, 'raw_value': value, 'raw_unit': raw_unit,
                            'value': value, 'unit': unit, 'evidence': deepcopy(evidence),
                            'reason': '本地模拟返回，不代表真实读图结论'}

                if run.get('review_fields') is None:
                    result = {'candidates': [candidate('material_grade', 'Q235B')],
                              'observations': [], 'warnings': []}
                else:
                    result = {
                        'candidates': [candidate('thickness', '3.2', raw_unit='mm', unit='mm'),
                                       candidate('drawing_units', 'mm')],
                        'observations': [
                            {'name': 'thickness', 'raw_text': '3.2 mm（本地模拟）',
                             'raw_unit': 'mm', 'status': 'observed',
                             'evidence': deepcopy(evidence), 'reason': '仅验证原PDF页码'},
                            {'name': 'drawing_units', 'raw_text': 'mm（本地模拟）',
                             'raw_unit': None, 'status': 'observed',
                             'evidence': deepcopy(evidence), 'reason': '仅验证原PDF页码'},
                        ], 'warnings': [],
                    }
                text = json.dumps(result, ensure_ascii=False)
                events = [
                    {'seq': 1, 'type': 'turn/start', 'data': {'turn': 1}},
                    {'seq': 2, 'type': 'assistant/message', 'data': {'turn': 1,
                     'message': {'content': [{'type': 'text', 'text': text}]}}},
                    {'seq': 3, 'type': 'turn/end', 'data': {'turn': 1, 'reason': {'kind': 'completed'}}},
                ]
                return {'events': events, 'hasMore': False}

        return Bridge()


@unittest.skipUnless(pdfium is not None and Image is not None,
                     'Pillow and pypdfium2 required for real worker PDF review contract')
class WorkbenchPdfReviewTests(unittest.TestCase):
    def test_real_worker_review_reuses_rendered_pages_and_only_original_business_sources(self):
        with tempfile.TemporaryDirectory(prefix='workbench-pdf-review-') as temporary:
            root = Path(temporary).resolve()
            pdf_path = root / 'two-pages.pdf'
            pdf = pdfium.PdfDocument.new()
            try:
                for width, height in ((144, 72), (180, 108)):
                    page = pdf.new_page(width, height)
                    page.gen_content()
                    page.close()
                pdf.save(pdf_path)
            finally:
                pdf.close()
            original_bytes = pdf_path.read_bytes()
            factory = PdfReviewRpcFactory()
            app = Workbench(root / 'tasks', reader_root=root / 'readers', bridge_factory=factory)
            task = app.create({'event_id': 'pdf-review-create', 'simulated': True,
                               'inquiry': '本地模拟单件PDF读图，不提供制造数量，不请求报价。'})
            task_id = task['id']
            task = app.upload(task_id, 'pdf-review-source', task['revision'],
                              '两页原始图纸.pdf', original_bytes)
            source_id = task['attachments'][0]['id']
            before = app.records.load(task_id)
            source_hash = before['sources'][source_id]['sha256']
            base_revision = before['data_revision']
            app._launch_read = Mock()  # Drive the actual worker synchronously.
            run_id = 'pdf-review-read'
            task = app.start_read(task_id, {'event_id': run_id, 'expected_revision': base_revision})
            self.assertEqual(task['run']['base_revision'], base_revision)
            native_run = reading_inputs.subprocess.run
            with patch('reading_inputs.subprocess.run', wraps=native_run) as render:
                app._read_worker(task_id, run_id)
            self.assertEqual(render.call_count, 2, 'initial overview and detail renders; review must reuse both bundles')
            task = app.task(task_id)
            self.assertEqual(task['run']['status'], 'completed', task['run'].get('error'))
            self.assertTrue(task['run']['candidates_applied'])
            self.assertEqual(task['run']['review']['status'], 'completed')
            from recognition_view import RECOGNITION_FIELDS
            self.assertEqual(set(task['run']['review']['fields']), RECOGNITION_FIELDS)

            workspace = app.reader_root / task_id / run_id
            review_directory = workspace / 'review'
            initial = json.loads((workspace / MANIFEST).read_text())
            reviewed = json.loads((review_directory / MANIFEST).read_text())
            self.assertNotEqual(initial['session_id'], reviewed['session_id'])
            for manifest in (initial, reviewed):
                self.assertEqual([item['id'] for item in manifest['files']], [source_id])
                self.assertEqual(manifest['files'][0]['sha256'], source_hash)
                self.assertEqual(manifest['pdf_views']['page_counts'], {source_id: 2})
                self.assertEqual(manifest['pdf_views']['sources'], [
                    {'id': source_id, 'sha256': source_hash,
                     'original_name': manifest['files'][0]['original_name']}])
            self.assertEqual(initial['pdf_views'], reviewed['pdf_views'])
            for view in initial['pdf_views']['views']:
                relative = Path(view['path'])
                self.assertFalse(relative.is_absolute())
                first_page, review_page = workspace / relative, review_directory / relative
                self.assertEqual(sha(first_page), view['sha256'])
                self.assertEqual(sha(review_page), view['sha256'])
                self.assertEqual(first_page.read_bytes(), review_page.read_bytes())

            after = app.records.load(task_id)
            self.assertEqual(after['data_revision'], base_revision + 1,
                             'PDF conversion/review must not create separate business source revisions')
            self.assertEqual(after['sources'], before['sources'])
            self.assertEqual(task['run']['source_ids'], [source_id])
            self.assertEqual(sum(event['id'] == 'READ-' + run_id for event in after['events']), 1)
            self.assertEqual([item['id'] for item in task['attachments']], [source_id])
            for name in ('material_grade', 'thickness', 'drawing_units'):
                field = task['fields'][name]
                self.assertIn(field['status'], {'pending_review', 'conflict'})
                self.assertEqual(field['evidence']['reference'], source_id)
                self.assertEqual(field['evidence']['page'], 2)
            self.assertGreaterEqual(len(task['run']['observations']), 2)
            for item in task['run']['observations']:
                self.assertEqual(item['evidence']['reference'], source_id)
                self.assertEqual(item['evidence']['page'], 2)
                self.assertEqual(item['source_pass'], 'review')
            self.assertEqual(pdf_path.read_bytes(), original_bytes)

            creates = [call for call in factory.calls if call['method'] == 'session.create']
            prompts = [call for call in factory.calls if call['method'] == 'session.prompt']
            self.assertEqual(len(creates), 2)
            self.assertEqual(len(prompts), 2)
            self.assertEqual({call['workspace'] for call in creates}, {workspace, review_directory})
            protected = [app.records.task_path(task_id) / 'record.json', app._state_path(task_id),
                         workspace / MANIFEST, review_directory / MANIFEST,
                         workspace / 'candidates.json', review_directory / 'candidates.json']
            saved = {path: path.read_bytes() for path in protected}
            rpc_count = len(factory.calls)
            app.task(task_id)
            app.tasks()
            self.assertEqual(len(factory.calls), rpc_count, 'task GET projections must never dispatch or inspect')
            self.assertEqual({path: path.read_bytes() for path in protected}, saved)


if __name__ == '__main__':
    unittest.main()
