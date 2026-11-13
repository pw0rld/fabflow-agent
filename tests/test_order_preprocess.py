"""Real local raster/PDF preprocessing, no DSH, model or network access."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import order_preprocess

try:
    from PIL import Image
    import pypdfium2 as pdfium
except ImportError:
    Image=pdfium=None


@unittest.skipUnless(Image is not None and pdfium is not None,'Pillow and pypdfium2 required for real preprocessing contracts')
class PreprocessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)
        self.output=self.directory/'views'

    def source(self,path,reference='DOC-1',name=None):
        return {'id':reference,'path':str(path),'original_name':name or path.name,
                'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}

    def png(self,name='drawing.png',size=(256,128),color='#2675a0'):
        path=self.directory/name
        with Image.new('RGB',size,color) as image:
            image.save(path,format='PNG')
        return path

    def pdf(self,count=3):
        path=self.directory/'multipage.pdf'
        document=pdfium.PdfDocument.new()
        for index in range(count):
            page=document.new_page(144+index*12,72+index*36)
            page.gen_content();page.close()
        document.save(path);document.close()
        return path

    def verify_view(self,view,source,page):
        self.assertEqual(view['origin_id'],source['id'])
        self.assertEqual(view['page'],page)
        self.assertEqual(view['original_sha256'],source['sha256'])
        self.assertEqual(view['converter'],order_preprocess.VERSION)
        path=Path(view['path'])
        self.assertTrue(path.is_relative_to(self.output))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),view['sha256'])
        with Image.open(path) as image:
            self.assertEqual(image.format,'PNG');self.assertEqual(image.mode,'RGB')
            self.assertEqual(image.size,(view['width'],view['height']))
            self.assertLessEqual(max(image.size),order_preprocess.MAX_SIDE)

    def test_real_png_has_origin_page_hash_and_no_original_mutation(self):
        path=self.png();source=self.source(path,name='支架图.png');before=path.read_bytes()
        result=order_preprocess.convert([source],self.output)
        self.assertEqual(len(result['views']),1);self.assertEqual(result['warnings'],[])
        view=result['views'][0];self.verify_view(view,source,1)
        self.assertEqual(view['id'],'DOC-1-p1');self.assertEqual(view['kind'],'page_image')
        self.assertEqual((view['width'],view['height']),(256,128));self.assertEqual(path.read_bytes(),before)

    def test_real_multipage_pdf_preserves_each_page_without_file_count_quantity(self):
        source=self.source(self.pdf(3),name='三页图纸.pdf')
        result=order_preprocess.convert([source],self.output)
        self.assertEqual(len(result['views']),3);self.assertEqual(result['warnings'],[])
        for number,view in enumerate(result['views'],1):
            self.verify_view(view,source,number)
            self.assertEqual(view['id'],'DOC-1-p'+str(number))
            self.assertNotIn('quantity',view)
        self.assertEqual(len({view['sha256'] for view in result['views']}),3)
        self.assertEqual(len({view['origin_id'] for view in result['views']}),1)

    def test_repeat_conversion_replays_same_paths_bytes_and_mapping(self):
        sources=[self.source(self.pdf(2)),self.source(self.png(),'DOC-2')]
        first=order_preprocess.convert(sources,self.output)
        before={view['path']:Path(view['path']).read_bytes() for view in first['views']}
        second=order_preprocess.convert(sources,self.output)
        self.assertEqual(second,first)
        self.assertEqual(len(list(self.output.glob('*.png'))),3)
        self.assertEqual({view['path']:Path(view['path']).read_bytes() for view in second['views']},before)

    def test_large_image_is_bounded_without_changing_aspect_ratio(self):
        source=self.source(self.png(size=(3000,1500)))
        result=order_preprocess.convert([source],self.output)
        view=result['views'][0];self.verify_view(view,source,1)
        self.assertEqual((view['width'],view['height']),(2600,1300))

    def test_exif_orientation_applied_before_derived_dimensions(self):
        path=self.directory/'rotated.jpg'
        with Image.new('RGB',(80,40),'#b05230') as image:
            exif=Image.Exif();exif[274]=6
            image.save(path,format='JPEG',exif=exif)
        source=self.source(path);result=order_preprocess.convert([source],self.output)
        self.verify_view(result['views'][0],source,1)
        self.assertEqual((result['views'][0]['width'],result['views'][0]['height']),(40,80))

    def test_malformed_png_pdf_preserve_valid_file_and_report_specific_names(self):
        broken_png=self.directory/'broken.png';broken_png.write_bytes(b'\x89PNG\r\n\x1a\ntruncated')
        broken_pdf=self.directory/'broken.pdf';broken_pdf.write_bytes(b'%PDF-1.7\ntruncated')
        good=self.source(self.png(),'DOC-GOOD')
        result=order_preprocess.convert([self.source(broken_png,'DOC-BAD-PNG'),self.source(broken_pdf,'DOC-BAD-PDF'),good],self.output)
        self.assertEqual([view['origin_id'] for view in result['views']],['DOC-GOOD'])
        self.assertTrue(any('broken.png' in warning for warning in result['warnings']))
        self.assertTrue(any('broken.pdf' in warning for warning in result['warnings']))

    def test_only_malformed_or_unsupported_documents_fail_without_fake_views(self):
        broken=self.directory/'bad.png';broken.write_bytes(b'bad image')
        for name,content in [('assembly.step',b'ISO-10303-21;'),('drawing.dwg',b'AC1032'),('assembly.stp',b'ISO-10303-21;')]:
            path=self.directory/name;path.write_bytes(content)
            with self.subTest(name=name),self.assertRaises(ValueError):
                order_preprocess.convert([self.source(path)],self.output)
        with self.assertRaises(ValueError):
            order_preprocess.convert([self.source(broken)],self.output)
        self.assertEqual(list(self.output.glob('*.png')),[])

    def test_cad_formats_keep_warnings_and_ascii_dxf_origin_hash(self):
        sources=[self.source(self.png(),'DOC-IMAGE')]
        for index,(name,content) in enumerate([('assembly.step',b'ISO-10303-21;'),('drawing.dwg',b'AC1032'),('binary.dxf',b'AutoCAD Binary DXF\x00'),('outline.dxf',b'0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n')]):
            path=self.directory/name;path.write_bytes(content);sources.append(self.source(path,'DOC-CAD-'+str(index)))
        result=order_preprocess.convert(sources,self.output)
        self.assertEqual([view['kind'] for view in result['views']],['page_image','cad_text'])
        text=result['views'][1];self.assertEqual(text['origin_id'],'DOC-CAD-3')
        self.assertEqual(text['sha256'],sources[-1]['sha256'])
        self.assertEqual(Path(text['path']).read_bytes(),Path(sources[-1]['path']).read_bytes())
        for name in ('assembly.step','drawing.dwg','binary.dxf'):
            self.assertTrue(any(name in warning for warning in result['warnings']))

    def test_hash_change_is_fatal_before_generating_views(self):
        path=self.png();source=self.source(path)
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'变化'):
            order_preprocess.convert([source],self.output)
        self.assertEqual(list(self.output.glob('*.png')),[])

    def test_total_page_budget_rejects_full_batch_without_silently_truncating(self):
        sources=[self.source(self.pdf(2)),self.source(self.png(),'DOC-2')]
        with patch.object(order_preprocess,'MAX_PAGES',2):
            with self.assertRaisesRegex(ValueError,'页'):
                order_preprocess.convert(sources,self.output)

    def test_total_derived_pixel_budget_is_enforced(self):
        source=self.source(self.png(size=(20,20)))
        with patch.object(order_preprocess,'MAX_PIXELS',399):
            with self.assertRaisesRegex(ValueError,'像素'):
                order_preprocess.convert([source],self.output)

    def test_subprocess_manifest_produces_only_json_views_and_mapping(self):
        source=self.source(self.png());manifest=self.directory/'input.json'
        manifest.write_text(json.dumps({'files':[source]}))
        child=subprocess.run([sys.executable,str(ROOT/'src/order_preprocess.py'),str(manifest),str(self.output)],capture_output=True,text=True,timeout=30)
        self.assertEqual(child.returncode,0,child.stderr)
        result=json.loads(child.stdout)
        self.verify_view(result['views'][0],source,1)
        self.assertEqual(result['converter'],order_preprocess.VERSION)


if __name__=='__main__':
    unittest.main()
