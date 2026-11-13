"""High-resolution original-page detail preparation, provenance and safe fallback."""
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
import reading_details as details
import reading_inputs

try:
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw
except ImportError:
    pdfium = Image = ImageDraw = None


@unittest.skipUnless(pdfium is not None and Image is not None, 'Pillow and PDFium required')
class ReadingDetailsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.workspace = self.directory / 'initial'
        self.workspace.mkdir()

    def source(self, path, reference='FILE-PDF', name=None):
        return {'id': reference, 'path': str(path), 'original_name': name or path.name,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def pdf(self, count=1, name='drawing.pdf', rotation=0):
        # A tiny deterministic vector fixture without a PDF-generation dependency.
        # Top-left red, top-right green, bottom-left blue, bottom-right yellow.
        page_ids = [3 + index * 2 for index in range(count)]
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
                   ('<< /Type /Pages /Count ' + str(count) + ' /Kids ['
                    + ' '.join(str(page_id) + ' 0 R' for page_id in page_ids) + '] >>').encode()]
        for page_index, page_id in enumerate(page_ids):
            objects.append(('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1200 800] '
                            '/Rotate ' + str(rotation) + ' /Resources << >> /Contents '
                            + str(page_id + 1) + ' 0 R >>').encode())
            if page_index % 2:
                content = (b'0 0 1 rg 0 400 600 400 re f\n'
                           b'1 1 0 rg 600 400 600 400 re f\n'
                           b'1 0 0 rg 0 0 600 400 re f\n'
                           b'0 1 0 rg 600 0 600 400 re f\n')
            else:
                content = (b'1 0 0 rg 0 400 600 400 re f\n'
                           b'0 1 0 rg 600 400 600 400 re f\n'
                           b'0 0 1 rg 0 0 600 400 re f\n'
                           b'1 1 0 rg 600 0 600 400 re f\n')
            objects.append(b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'endstream')
        data = bytearray(b'%PDF-1.4\n')
        offsets = [0]
        for index, obj in enumerate(objects, 1):
            offsets.append(len(data))
            data.extend(str(index).encode() + b' 0 obj\n' + obj + b'\nendobj\n')
        start = len(data)
        data.extend(b'xref\n0 ' + str(len(offsets)).encode() + b'\n0000000000 65535 f \n')
        for offset in offsets[1:]:
            data.extend(('%010d 00000 n \n' % offset).encode())
        data.extend(b'trailer\n<< /Size ' + str(len(offsets)).encode()
                    + b' /Root 1 0 R >>\nstartxref\n' + str(start).encode() + b'\n%%EOF\n')
        path = self.workspace / name
        path.write_bytes(data)
        return self.source(path, name='客户图纸.pdf')

    def raster(self, size=(2200, 1400), name='drawing.png', reference='FILE-IMAGE'):
        path = self.workspace / name
        with Image.new('RGB', size, 'white') as image:
            draw = ImageDraw.Draw(image)
            width, height = size
            draw.rectangle((0, 0, width // 2 - 1, height // 2 - 1), fill='red')
            draw.rectangle((width // 2, 0, width - 1, height // 2 - 1), fill='#00ff00')
            draw.rectangle((0, height // 2, width // 2 - 1, height - 1), fill='blue')
            draw.rectangle((width // 2, height // 2, width - 1, height - 1), fill='yellow')
            image.save(path)
        return self.source(path, reference)

    def snapshot(self, folder):
        return {str(path.relative_to(folder)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in folder.rglob('*') if path.is_file() and not path.is_symlink()}

    def write_cache(self, data):
        (self.workspace / details.BUNDLE / details.MANIFEST).write_text(json.dumps(data, ensure_ascii=False))

    def copies(self, files, target):
        target.mkdir()
        result = []
        for index, source in enumerate(files):
            path = target / ('copy-' + str(index) + Path(source['path']).suffix)
            path.write_bytes(Path(source['path']).read_bytes())
            result.append({**source, 'path': str(path)})
        return result

    def test_pdf_crops_map_all_pages_and_top_left_quadrants_at_higher_resolution(self):
        files = [self.pdf(2)]
        overview = reading_inputs.prepare_pdf_views(self.workspace, files)
        original_pdf = Path(files[0]['path']).read_bytes()
        overview_before = self.snapshot(self.workspace / reading_inputs.BUNDLE)
        data = details.prepare_detail_views(self.workspace, files, overview)
        self.assertEqual(data['converter'], 'drawing-details-1')
        self.assertEqual(data['page_counts'], {'FILE-PDF': 2})
        self.assertEqual(data['warnings'], [])
        self.assertEqual(len(data['views']), 8)
        self.assertEqual(sorted(set(row['page'] for row in data['views'])), [1, 2])
        colors = {'tl': (255, 0, 0), 'tr': (0, 255, 0), 'bl': (0, 0, 255), 'br': (255, 255, 0)}
        for row in data['views']:
            code = row['id'].rsplit('-', 1)[1]
            self.assertEqual(row['id'], details._alias('FILE-PDF', row['page'], code))
            self.assertFalse(Path(row['path']).is_absolute())
            self.assertEqual(row['original_sha256'], files[0]['sha256'])
            self.assertEqual(row['origin_id'], 'FILE-PDF')
            self.assertGreater(row['width'], overview['views'][row['page'] - 1]['width'] * .55 * 1.7)
            self.assertLessEqual(max(row['width'], row['height']), details.MAX_SIDE)
            self.assertLessEqual(row['width'] * row['height'], details.MAX_TILE_PIXELS)
            with Image.open(self.workspace / row['path']) as image:
                expected = colors[code] if row['page'] % 2 else {
                    'tl': (0, 0, 255), 'tr': (255, 255, 0), 'bl': (255, 0, 0), 'br': (0, 255, 0)}[code]
                self.assertEqual(image.getpixel((image.width // 2, image.height // 2)), expected)
                self.assertEqual(image.size, (row['width'], row['height']))
        self.assertEqual(self.snapshot(self.workspace / reading_inputs.BUNDLE), overview_before)
        self.assertEqual(Path(files[0]['path']).read_bytes(), original_pdf)
        self.assertEqual(details.validate_detail_views(self.workspace, files, data, overview), data)
        self.assertEqual(list(self.workspace.glob(details.BUNDLE + '-stage-*')), [])

    def test_large_raster_quadrants_keep_original_pixels_and_small_or_fake_images_remain_legacy(self):
        source = self.raster()
        original = Path(source['path']).read_bytes()
        data = details.prepare_detail_views(self.workspace, [source])
        self.assertEqual(len(data['views']), 4)
        self.assertEqual(data['page_counts'], {'FILE-IMAGE': 1})
        for row, color in zip(data['views'], ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0))):
            self.assertEqual(row['width'], 1210)
            self.assertIn(row['height'], (770, 771))
            with Image.open(self.workspace / row['path']) as image:
                self.assertEqual(image.getpixel((image.width // 2, image.height // 2)), color)
        self.assertEqual(Path(source['path']).read_bytes(), original)
        for name, content in (('fake.png', b'FakeBridgePNGbytes'), ('unsupported.dxf', b'0\nEOF\n')):
            path = self.directory / name
            path.write_bytes(content)
            folder = self.directory / (name + '-run')
            folder.mkdir()
            local = folder / name
            local.write_bytes(content)
            self.assertIsNone(details.prepare_detail_views(folder, [self.source(local, 'FILE-LEGACY')]))
            self.assertFalse((folder / details.BUNDLE).exists())
        small = self.directory / 'small-run'
        small_files = self.copies([self.raster((100, 80), name='small.png')], small)
        self.assertIsNone(details.prepare_detail_views(small, small_files))

    def test_immutable_cache_reuse_never_rerenders_or_rewrites_sources(self):
        files = [self.pdf()]
        data = details.prepare_detail_views(self.workspace, files)
        before = self.snapshot(self.workspace)
        with patch('reading_details.subprocess.run', side_effect=AssertionError('must reuse')):
            self.assertEqual(details.prepare_detail_views(self.workspace, files), data)
            self.assertEqual(details.validate_detail_views(self.workspace, files, deepcopy(data)), data)
        self.assertEqual(self.snapshot(self.workspace), before)

    def test_rotated_pdf_quadrants_follow_displayed_page_top_left_coordinates(self):
        files = [self.pdf(rotation=90)]
        data = details.prepare_detail_views(self.workspace, files)
        self.assertEqual(len(data['views']), 4)
        colors = {'tl': (0, 0, 255), 'tr': (255, 0, 0), 'bl': (255, 255, 0), 'br': (0, 255, 0)}
        for row in data['views']:
            code = row['id'].rsplit('-', 1)[1]
            self.assertGreater(row['height'], row['width'])
            with Image.open(self.workspace / row['path']) as image:
                self.assertEqual(image.getpixel((image.width // 2, image.height // 2)), colors[code])

    def test_original_hash_changes_and_cached_png_tampering_refuse_without_repair(self):
        files = [self.pdf()]
        data = details.prepare_detail_views(self.workspace, files)
        path = Path(files[0]['path'])
        original = path.read_bytes()
        path.write_bytes(original + b'changed')
        with self.assertRaisesRegex(ValueError, '原附件已变化'):
            details.prepare_detail_views(self.workspace, files)
        path.write_bytes(original)
        crop = self.workspace / data['views'][0]['path']
        crop.write_bytes(crop.read_bytes() + b'changed')
        with patch('reading_details.subprocess.run', side_effect=AssertionError('must not repair')):
            with self.assertRaisesRegex(ValueError, '校验失败'):
                details.prepare_detail_views(self.workspace, files)

    def test_manifest_requires_exact_original_page_region_mapping_and_complete_four_crops(self):
        files = [self.pdf()]
        original = details.prepare_detail_views(self.workspace, files)
        variants = []
        for change in ({'schema_version': True}, {'converter': 'other'}, {'page_counts': {'FILE-PDF': 2}},
                       {'views': original['views'][:3]}, {'unexpected': True}, {'warnings': [3]}):
            variants.append({**deepcopy(original), **change})
        for change in ({'id': 'FILE-PDF'}, {'origin_id': 'OTHER'}, {'page': 0}, {'page': True},
                       {'path': '../outside.png'}, {'region': '错误象限'}, {'original_sha256': '0' * 64},
                       {'width': True}, {'height': 9000}, {'original_name': 'wrong.png'}):
            changed = deepcopy(original)
            changed['views'][0].update(change)
            variants.append(changed)
        repeated = deepcopy(original)
        repeated['views'][1] = deepcopy(repeated['views'][0])
        variants.append(repeated)
        for changed in variants:
            with self.subTest(change=str(changed)[:100]):
                self.write_cache(changed)
                with self.assertRaises(ValueError):
                    details.validate_detail_views(self.workspace, files, changed)
        self.write_cache(original)
        changed_argument = deepcopy(original)
        changed_argument['views'][0]['page'] = 2
        with self.assertRaisesRegex(ValueError, '运行清单不一致'):
            details.validate_detail_views(self.workspace, files, changed_argument)

    def test_paths_outside_workspace_and_symlinked_original_cache_bundle_or_png_refuse(self):
        files = [self.pdf()]
        outside = self.directory / 'outside.pdf'
        outside.write_bytes(Path(files[0]['path']).read_bytes())
        with self.assertRaisesRegex(ValueError, '之外'):
            details.prepare_detail_views(self.workspace, [{**files[0], 'path': str(outside)}])
        link = self.workspace / 'linked.pdf'
        link.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            details.prepare_detail_views(self.workspace, [{**files[0], 'path': str(link)}])
        data = details.prepare_detail_views(self.workspace, files)
        crop = self.workspace / data['views'][0]['path']
        raw = crop.read_bytes()
        external_png = self.directory / 'external.png'
        external_png.write_bytes(raw)
        crop.unlink()
        crop.symlink_to(external_png)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            details.validate_detail_views(self.workspace, files, data)
        crop.unlink()
        crop.write_bytes(raw)
        bundle = self.workspace / details.BUNDLE
        moved = self.workspace / 'moved-details'
        bundle.rename(moved)
        bundle.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '符号链接'):
            details.prepare_detail_views(self.workspace, files)

    def test_timeout_is_an_immutable_explicit_overview_fallback_not_a_failed_read(self):
        files = [self.pdf()]
        overview = reading_inputs.prepare_pdf_views(self.workspace, files)
        overview_before = self.snapshot(self.workspace / reading_inputs.BUNDLE)
        with patch('reading_details.subprocess.run', side_effect=subprocess.TimeoutExpired('renderer', 60)):
            data = details.prepare_detail_views(self.workspace, files, overview)
        self.assertEqual(data['views'], [])
        self.assertEqual(data['page_counts'], {'FILE-PDF': 1})
        self.assertTrue(data['warnings'])
        self.assertEqual(self.snapshot(self.workspace / reading_inputs.BUNDLE), overview_before)
        with patch('reading_details.subprocess.run', side_effect=AssertionError('no retry during inspection')):
            self.assertEqual(details.prepare_detail_views(self.workspace, files, overview), data)
        self.assertEqual(details.validate_detail_views(self.workspace, files, data, overview), data)

    def test_budget_preflight_discards_all_crops_instead_of_first_n_pages(self):
        files = [self.pdf(33)]
        data = details.prepare_detail_views(self.workspace, files)
        self.assertEqual(data['views'], [])
        self.assertEqual(data['page_counts'], {'FILE-PDF': 33})
        self.assertTrue(data['warnings'])
        self.assertEqual(list((self.workspace / details.BUNDLE).glob('*.png')), [])

    def test_pixel_budget_preflight_falls_back_without_committing_partial_page_coverage(self):
        source = self.pdf(2)
        request = self.workspace / 'worker-request.json'
        request.write_text(json.dumps({'files': [source], 'source_workspace': str(self.workspace), 'pdf_counts': None}))
        with patch('reading_details.MAX_PIXELS', 1):
            result = details._render(request, self.workspace / 'worker-result')
        self.assertEqual(result['views'], [])
        self.assertEqual(result['page_counts'], {'FILE-PDF': 2})
        self.assertTrue(result['warnings'])
        self.assertFalse((self.workspace / 'worker-result').exists())

    def test_later_page_conversion_failure_discards_earlier_successful_crops(self):
        files = [self.pdf(2)]
        original_save = Image.Image.save
        calls = []
        def saving(image, *args, **kwargs):
            calls.append(args[0])
            if len(calls) == 5:
                raise OSError('second page unavailable')
            return original_save(image, *args, **kwargs)
        def renderer(command, **kwargs):
            with patch.object(Image.Image, 'save', saving):
                payload = details._render(command[-2], command[-1])
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), '')
        with patch('reading_details.subprocess.run', side_effect=renderer):
            data = details.prepare_detail_views(self.workspace, files)
        self.assertEqual(len(calls), 5)
        self.assertEqual(data['views'], [])
        self.assertEqual(data['page_counts'], {'FILE-PDF': 2})
        self.assertTrue(data['warnings'])
        self.assertEqual(list((self.workspace / details.BUNDLE).glob('*.png')), [])
        self.assertEqual(list(self.workspace.glob(details.BUNDLE + '-stage-*')), [])

    def test_copy_review_reuses_exact_bytes_and_relative_mapping_without_decoders(self):
        files = [self.pdf(), self.raster((80, 60))]
        overview = reading_inputs.prepare_pdf_views(self.workspace, files)
        data = details.prepare_detail_views(self.workspace, files, overview)
        target = self.directory / 'review'
        copied = self.copies(files, target)
        reading_inputs.copy_pdf_views(self.workspace, target, files, copied)
        with patch('reading_details.subprocess.run', side_effect=AssertionError('review must reuse')):
            self.assertEqual(details.copy_detail_views(self.workspace, target, files, copied, data, overview), data)
            before = self.snapshot(target)
            self.assertEqual(details.copy_detail_views(self.workspace, target, files, copied, data, overview), data)
            self.assertEqual(before, self.snapshot(target))
        for row in data['views']:
            self.assertEqual((self.workspace / row['path']).read_bytes(), (target / row['path']).read_bytes())
        self.assertEqual((self.workspace / details.BUNDLE / details.MANIFEST).read_bytes(),
                         (target / details.BUNDLE / details.MANIFEST).read_bytes())
        self.assertEqual(details.validate_detail_views(target, copied, data, overview), data)
        self.assertIsNone(details.copy_detail_views(self.workspace, target, files, copied, None, overview))
        self.assertEqual(list(target.glob(details.BUNDLE + '-stage-*')), [])

    def test_renderer_result_wrong_origin_is_rejected_before_publishing(self):
        files = [self.pdf()]
        def renderer(command, **kwargs):
            payload = details._render(command[-2], command[-1])
            payload['views'][0]['origin_id'] = 'OTHER'
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), '')
        with patch('reading_details.subprocess.run', side_effect=renderer):
            with self.assertRaisesRegex(ValueError, '来源或页码无效'):
                details.prepare_detail_views(self.workspace, files)
        self.assertFalse((self.workspace / details.BUNDLE).exists())
        self.assertEqual(list(self.workspace.glob(details.BUNDLE + '-stage-*')), [])


if __name__ == '__main__':
    unittest.main()
