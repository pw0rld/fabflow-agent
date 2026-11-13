"""Pure adapter contract checks; no credentials, networking or model calls."""
import copy
import base64
import io
import unittest
from drawing_split_service.model_adapter import parse_json, located_regions, retain_dimension_context, image_input


class ModelContractTests(unittest.TestCase):
    def test_json_accepts_plain_object_and_fenced_object(self):
        self.assertEqual(parse_json('{"title":"test"}'),{'title':'test'})
        self.assertEqual(parse_json('```json\n{"regions":[]}\n```'),{'regions':[]})

    def test_json_refuses_prose_multiple_objects_and_nonfinite_constants(self):
        for value in ('hello {"x":1}', '{} {}', '[]','{"x":NaN}', '{"x":Infinity}', '', '{"x":1}\nextra'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):parse_json(value)
        with self.assertRaises(ValueError):parse_json(' '*800001)

    def test_region_filter_rejects_invalid_coordinates_and_duplicate_ids(self):
        valid={'id':'view','bbox':[10,20,300,400],'title':'Current drawing view'}
        rows=[valid,dict(valid),{'id':'reverse','bbox':[20,20,10,30]},
              {'id':'far','bbox':[-20,0,100,100]}, {'id':'nan','bbox':[0,0,float('nan'),10]},
              {'id':'bool','bbox':[False,0,100,100]}, {'id':'empty','bbox':[0,0,0,0]},
              {'id':'big','bbox':[0,0,1001,100]}, {'bbox':[0,0,100,100]}]
        self.assertEqual(located_regions({'coordinate_system':'normalized_1000','regions':rows}),[valid])
        with self.assertRaises(ValueError):located_regions({'regions':[valid]})

    def test_dimension_context_keeps_outer_labels_for_each_region_type(self):
        rows=[{'id':'view','type':'orthographic','bbox':[100,200,800,700]}]
        for kind in ('flat_pattern','isometric','process_table','technical_notes','title_block','detail'):
            rows.append({'id':kind,'type':kind,'bbox':[100,200,800,700]})
        original=copy.deepcopy(rows)
        result=retain_dimension_context(rows)
        self.assertEqual(result[0]['bbox'],[75,185,825,715])
        for row in result[1:]:
            with self.subTest(type=row['type']):self.assertEqual(row['bbox'],[94,194,806,706])
        self.assertEqual(rows,original)
        self.assertIsNot(result[0],rows[0])
        self.assertIsNot(result[0]['bbox'],rows[0]['bbox'])

    def test_dimension_context_clamps_to_whole_image_without_mutating_input(self):
        rows=[{'id':'edge-view','type':'orthographic','bbox':[10,10,990,995],'title':'Edge view'},
              {'id':'edge-table','type':'process_table','bbox':[3,4,997,999],'title':'Edge table'}]
        original=copy.deepcopy(rows)
        result=retain_dimension_context(rows)
        self.assertEqual([row['bbox'] for row in result],[[0,0,1000,1000],[0,0,1000,1000]])
        self.assertEqual([row['title'] for row in result],['Edge view','Edge table'])
        self.assertEqual(rows,original)

    def test_model_preview_composites_transparency_on_white_and_keeps_black_lines(self):
        from PIL import Image
        source=Image.new('RGBA',(2,1),(0,0,0,0))
        source.putpixel((1,0),(0,0,0,255))
        encoded=image_input(source)['image_url'].split(',',1)[1]
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as preview:
            self.assertEqual(preview.mode,'RGB')
            self.assertEqual(preview.getpixel((0,0)),(255,255,255))
            self.assertEqual(preview.getpixel((1,0)),(0,0,0))
        self.assertEqual(source.mode,'RGBA')
        self.assertEqual(source.getpixel((0,0)),(0,0,0,0))
        self.assertEqual(source.getpixel((1,0)),(0,0,0,255))


if __name__=='__main__':
    import platform
    if platform.system()!='Linux':raise SystemExit('Run tests only on ZeroClave, per AGENTS.md.')
    unittest.main()
