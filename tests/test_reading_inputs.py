"""Real PDF rendering and immutable page bundles, without DSH or network calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import reading_inputs

try:
    import pypdfium2 as pdfium
    from PIL import Image
except ImportError:
    pdfium = Image = None


@unittest.skipUnless(pdfium is not None and Image is not None, 'Pillow and PDFium required')
class ReadingInputsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.workspace = self.directory / 'initial'
        self.workspace.mkdir()

    def source(self, path, reference='FILE-PDF', name=None):
        return {'id': reference, 'path': str(path), 'original_name': name or path.name,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def pdf(self, count=2, name='drawing.pdf'):
        path = self.workspace / name
        document = pdfium.PdfDocument.new()
        for index in range(count):
            page = document.new_page(144 + index * 18, 72 + index * 12)
            page.gen_content()
            page.close()
        document.save(path)
        document.close()
        return self.source(path, name='客户总图.pdf')

    def image(self, name='drawing.png', reference='FILE-IMAGE'):
        path = self.workspace / name
        with Image.new('RGB', (80, 40), '#3874bc') as image:
            image.save(path)
        return self.source(path, reference)

    def snapshot(self, folder):
        return {str(path.relative_to(folder)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in folder.rglob('*') if path.is_file() and not path.is_symlink()}

    def write_cache(self, data):
        path = self.workspace / reading_inputs.BUNDLE / reading_inputs.MANIFEST
        path.write_text(json.dumps(data, ensure_ascii=False))

    def copy_originals(self, files, target):
        target.mkdir()
        copied = []
        for number, source in enumerate(files):
            path = target / ('copy-' + str(number) + Path(source['path']).suffix)
            path.write_bytes(Path(source['path']).read_bytes())
            copied.append({**source, 'path': str(path)})
        return copied

    def test_real_multipage_pdf_and_native_png_have_complete_provenance_without_original_changes(self):
        files = [self.pdf(3), self.image()]
        before = {row['path']: Path(row['path']).read_bytes() for row in files}
        data = reading_inputs.prepare_pdf_views(self.workspace, files)
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['converter'], 'order-views-1')
        self.assertEqual(data['page_counts'], {'FILE-PDF': 3})
        self.assertEqual(data['sources'], sorted([{key: row[key] for key in ('id', 'sha256', 'original_name')}
                                                for row in files], key=lambda row: row['id']))
        self.assertEqual(len(data['views']), 3)
        self.assertEqual([view['page'] for view in data['views']], [1, 2, 3])
        for page, view in enumerate(data['views'], 1):
            self.assertEqual(view['origin_id'], 'FILE-PDF')
            self.assertEqual(view['original_sha256'], files[0]['sha256'])
            self.assertEqual(view['id'], 'PDFPAGE-' + hashlib.sha256(b'FILE-PDF').hexdigest()[:16] + '-' + str(page))
            self.assertFalse(Path(view['path']).is_absolute())
            path = self.workspace / view['path']
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), view['sha256'])
            with Image.open(path) as image:
                self.assertEqual((image.format, image.mode), ('PNG', 'RGB'))
                self.assertEqual(image.size, (view['width'], view['height']))
                self.assertLessEqual(max(image.size), 2600)
            self.assertNotIn('quantity', view)
        self.assertEqual({row['path']: Path(row['path']).read_bytes() for row in files}, before)
        self.assertEqual(reading_inputs.validate_pdf_views(self.workspace, files, data), data)
        self.assertEqual(list(self.workspace.glob('.pdf-reading-stage-*')), [])

    def test_cache_reuse_does_not_run_decoders_or_rewrite_png_manifest_or_originals(self):
        files = [self.pdf(2), self.image()]
        first = reading_inputs.prepare_pdf_views(self.workspace, files)
        before = self.snapshot(self.workspace)
        with patch('reading_inputs.subprocess.run', side_effect=AssertionError('renderer must not run')) as renderer:
            self.assertEqual(reading_inputs.prepare_pdf_views(self.workspace, files), first)
            self.assertEqual(reading_inputs.validate_pdf_views(self.workspace, files, deepcopy(first)), first)
            renderer.assert_not_called()
        self.assertEqual(self.snapshot(self.workspace), before)

    def test_no_pdf_returns_legacy_none_without_decoding_even_an_invalid_png(self):
        path = self.workspace / 'legacy.png'
        path.write_bytes(b'invalid image intentionally never decoded')
        files = [self.source(path, 'FILE-OLD')]
        before = self.snapshot(self.workspace)
        with patch('reading_inputs.subprocess.run', side_effect=AssertionError('renderer prohibited')):
            self.assertIsNone(reading_inputs.prepare_pdf_views(self.workspace, files))
        self.assertEqual(self.snapshot(self.workspace), before)
        self.assertFalse((self.workspace / reading_inputs.BUNDLE).exists())

    def test_changed_original_pdf_or_non_pdf_refuses_cache_without_overwriting_it(self):
        files = [self.pdf(), self.image()]
        data = reading_inputs.prepare_pdf_views(self.workspace, files)
        bundle = self.workspace / reading_inputs.BUNDLE
        before = self.snapshot(bundle)
        for position in (0, 1):
            with self.subTest(source=position):
                path = Path(files[position]['path'])
                original = path.read_bytes()
                path.write_bytes(original + b'changed')
                with self.assertRaisesRegex(ValueError, '原附件已变化'):
                    reading_inputs.prepare_pdf_views(self.workspace, files)
                path.write_bytes(original)
        self.assertEqual(self.snapshot(bundle), before)
        self.assertEqual(reading_inputs.validate_pdf_views(self.workspace, files, data), data)

    def test_corrupt_derived_png_is_rejected_without_repair_or_rerender(self):
        files = [self.pdf()]
        data = reading_inputs.prepare_pdf_views(self.workspace, files)
        page = self.workspace / data['views'][0]['path']
        page.write_bytes(page.read_bytes() + b'changed')
        broken = page.read_bytes()
        with patch('reading_inputs.subprocess.run', side_effect=AssertionError('renderer prohibited')):
            with self.assertRaisesRegex(ValueError, '校验失败'):
                reading_inputs.prepare_pdf_views(self.workspace, files)
        self.assertEqual(page.read_bytes(), broken)

    def test_cache_schema_version_page_completeness_aliases_and_data_match_are_strict(self):
        files = [self.pdf(2), self.image()]
        original = reading_inputs.prepare_pdf_views(self.workspace, files)
        invalid = []
        for update in ({'schema_version': True}, {'converter': 'new-converter'},
                       {'page_counts': {'FILE-PDF': 1}}, {'page_counts': {'OTHER': 2}},
                       {'page_counts': {'FILE-PDF': True}}, {'views': original['views'][:1]},
                       {'unexpected': 'field'}):
            invalid.append({**deepcopy(original), **update})
        for update in ({'page': 0}, {'page': 3}, {'page': True}, {'page': '1'}, {'origin_id': 'OTHER'},
                       {'id': 'FILE-IMAGE'}, {'path': '../outside.png'}, {'path': '/tmp/outside.png'},
                       {'original_sha256': '0' * 64}, {'width': 1}, {'height': True}):
            changed = deepcopy(original)
            changed['views'][0].update(update)
            invalid.append(changed)
        duplicate = deepcopy(original)
        duplicate['views'][1] = deepcopy(duplicate['views'][0])
        invalid.append(duplicate)
        for data in invalid:
            with self.subTest(update=str(data)[:150]):
                self.write_cache(data)
                with self.assertRaises(ValueError):
                    reading_inputs.validate_pdf_views(self.workspace, files, data)
        self.write_cache(original)
        altered_argument = deepcopy(original)
        altered_argument['views'][0]['page'] = 2
        with self.assertRaisesRegex(ValueError, '运行清单不一致'):
            reading_inputs.validate_pdf_views(self.workspace, files, altered_argument)

    def test_symlinked_original_derived_png_and_bundle_are_not_followed(self):
        files = [self.pdf()]
        original_pdf = Path(files[0]['path'])
        linked_pdf = self.workspace / 'linked.pdf'
        linked_pdf.symlink_to(original_pdf)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            reading_inputs.prepare_pdf_views(self.workspace, [{**files[0], 'path': str(linked_pdf)}])
        data = reading_inputs.prepare_pdf_views(self.workspace, files)
        page = self.workspace / data['views'][0]['path']
        outside = self.directory / 'outside.png'
        outside.write_bytes(page.read_bytes())
        page.unlink()
        page.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            reading_inputs.validate_pdf_views(self.workspace, files, data)
        page.unlink()
        page.write_bytes(outside.read_bytes())
        bundle = self.workspace / reading_inputs.BUNDLE
        moved = self.directory / 'moved-bundle'
        bundle.rename(moved)
        bundle.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            reading_inputs.prepare_pdf_views(self.workspace, files)

    def test_a_broken_pdf_refuses_the_full_batch_without_committing_valid_partial_pages(self):
        good = self.pdf(2)
        broken = self.workspace / 'broken.pdf'
        broken.write_bytes(b'%PDF-1.7\ntruncated')
        files = [good, self.source(broken, 'FILE-BROKEN')]
        before = {row['path']: Path(row['path']).read_bytes() for row in files}
        with self.assertRaisesRegex(ValueError, '无法完整读取'):
            reading_inputs.prepare_pdf_views(self.workspace, files)
        self.assertFalse((self.workspace / reading_inputs.BUNDLE).exists())
        self.assertEqual(list(self.workspace.glob('.pdf-reading-stage-*')), [])
        self.assertEqual({row['path']: Path(row['path']).read_bytes() for row in files}, before)
        self.assertEqual(len(reading_inputs.prepare_pdf_views(self.workspace, [good])['views']), 2)

    def test_total_pdf_pages_plus_native_attachments_cannot_be_silently_truncated(self):
        files = [self.pdf(3), self.image()]
        with self.assertRaisesRegex(ValueError, '上限'):
            reading_inputs.prepare_pdf_views(self.workspace, files, max_views=3)
        self.assertFalse((self.workspace / reading_inputs.BUNDLE).exists())
        self.assertEqual(list(self.workspace.glob('.pdf-reading-stage-*')), [])
        data = reading_inputs.prepare_pdf_views(self.workspace, files, max_views=4)
        self.assertEqual(len(data['views']), 3)
        with self.assertRaisesRegex(ValueError, '上限'):
            reading_inputs.validate_pdf_views(self.workspace, files, data, max_views=3)

    def test_alias_collision_with_original_attachment_is_rejected(self):
        pdf = self.pdf(1)
        alias = 'PDFPAGE-' + hashlib.sha256(pdf['id'].encode()).hexdigest()[:16] + '-1'
        files = [pdf, self.image(reference=alias)]
        with self.assertRaisesRegex(ValueError, '标识冲突'):
            reading_inputs.prepare_pdf_views(self.workspace, files)
        self.assertFalse((self.workspace / reading_inputs.BUNDLE).exists())

    def test_render_timeout_cleans_staging_and_releases_slot_for_a_later_run(self):
        files = [self.pdf(1)]
        with patch('reading_inputs.subprocess.run', side_effect=subprocess.TimeoutExpired('renderer', 60)):
            with self.assertRaisesRegex(ValueError, '60 秒'):
                reading_inputs.prepare_pdf_views(self.workspace, files)
        self.assertFalse((self.workspace / reading_inputs.BUNDLE).exists())
        self.assertEqual(list(self.workspace.glob('.pdf-reading-stage-*')), [])
        self.assertEqual(len(reading_inputs.prepare_pdf_views(self.workspace, files)['views']), 1)

    def test_review_copies_exact_png_and_manifest_bytes_without_rendering_and_reuses_existing_copy(self):
        files = [self.pdf(2), self.image()]
        data = reading_inputs.prepare_pdf_views(self.workspace, files)
        target = self.directory / 'review'
        target_files = self.copy_originals(files, target)
        source_before = self.snapshot(self.workspace)
        with patch('reading_inputs.subprocess.run', side_effect=AssertionError('review must not render')) as renderer:
            copied = reading_inputs.copy_pdf_views(self.workspace, target, files, target_files)
            self.assertEqual(copied, data)
            for view in data['views']:
                self.assertEqual((self.workspace / view['path']).read_bytes(), (target / view['path']).read_bytes())
            manifest = Path(reading_inputs.BUNDLE) / reading_inputs.MANIFEST
            self.assertEqual((self.workspace / manifest).read_bytes(), (target / manifest).read_bytes())
            before = self.snapshot(target)
            self.assertEqual(reading_inputs.copy_pdf_views(self.workspace, target, files, target_files), data)
            self.assertEqual(self.snapshot(target), before)
            renderer.assert_not_called()
        self.assertEqual(self.snapshot(self.workspace), source_before)

    def test_review_rejects_changed_original_metadata_or_existing_foreign_bundle_without_overwriting(self):
        files = [self.pdf(1)]
        reading_inputs.prepare_pdf_views(self.workspace, files)
        target = self.directory / 'review'
        target_files = self.copy_originals(files, target)
        changed = [{**target_files[0], 'original_name': 'different.pdf'}]
        with self.assertRaisesRegex(ValueError, '原附件与首轮不一致'):
            reading_inputs.copy_pdf_views(self.workspace, target, files, changed)
        bundle = target / reading_inputs.BUNDLE
        bundle.mkdir()
        foreign = bundle / 'foreign.txt'
        foreign.write_text('must be retained')
        before = self.snapshot(target)
        with self.assertRaises(ValueError):
            reading_inputs.copy_pdf_views(self.workspace, target, files, target_files)
        self.assertEqual(self.snapshot(target), before)

    def test_legacy_run_without_bundle_is_not_upgraded_when_review_is_copied(self):
        files = [self.pdf()]
        target = self.directory / 'review'
        target_files = self.copy_originals(files, target)
        before = self.snapshot(target)
        with patch('reading_inputs.subprocess.run', side_effect=AssertionError('legacy decoder prohibited')):
            self.assertIsNone(reading_inputs.copy_pdf_views(self.workspace, target, files, target_files))
        self.assertEqual(self.snapshot(target), before)
        self.assertFalse((target / reading_inputs.BUNDLE).exists())


if __name__ == '__main__':
    unittest.main()
