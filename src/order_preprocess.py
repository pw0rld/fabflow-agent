"""Bounded, replayable drawing views. Run native document decoders in a subprocess."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if sys.platform.startswith('linux') and (ROOT / 'vendor/python-linux').is_dir():
    sys.path.insert(0, str(ROOT / 'vendor/python-linux'))

MAX_PAGES = 60
MAX_PIXELS = 120_000_000
MAX_SIDE = 2600
VERSION = 'order-views-1'


class BudgetError(ValueError):
    pass


def convert(files, destination):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = 40_000_000
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    views, warnings = [], []
    pixels = 0
    for source in files:
        path = Path(source['path'])
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if content_hash != source['sha256']:
            raise ValueError('原始文件已变化，停止本次预处理。')
        suffix = path.suffix.lower()
        if suffix in {'.dwg', '.step', '.stp'}:
            warnings.append(source['original_name'] + '：当前后台自动分件尚不支持此 CAD 格式，请人工分件或补充 PDF/图片；原件仍可预览。')
            continue
        if suffix == '.dxf':
            raw = path.read_bytes()
            if b'\x00' in raw[:1024]:
                warnings.append(source['original_name'] + '：二进制 DXF 暂不支持后台识别，请补充 PDF/图片。')
                continue
            views.append({**source, 'origin_id': source['id'], 'page': None, 'kind': 'cad_text'})
            continue
        pdf = image = page = bitmap = current = None
        try:
            if suffix == '.pdf':
                import pypdfium2 as pdfium
                pdf = pdfium.PdfDocument(path)
                count = len(pdf)
            else:
                image = Image.open(path)
                count = getattr(image, 'n_frames', 1)
            if len(views) + count > MAX_PAGES:
                raise BudgetError('本批资料超过 60 页，请分成较小批次。')
            for index in range(count):
                if pdf is not None:
                    page = pdf[index]
                    width, height = page.get_size()
                    if width <= 0 or height <= 0:
                        raise ValueError('PDF 页面尺寸无效。')
                    scale = min(2.0, MAX_SIDE / max(width, height))
                    bitmap = page.render(scale=scale)
                    current = bitmap.to_pil().convert('RGB')
                else:
                    image.seek(index)
                    current = ImageOps.exif_transpose(image.copy()).convert('RGB')
                    current.thumbnail((MAX_SIDE, MAX_SIDE))
                pixels += current.width * current.height
                if pixels > MAX_PIXELS:
                    raise BudgetError('本批资料像素总量过大，请分批处理。')
                view_id = source['id'] + '-p' + str(index + 1)
                target = destination / (view_id + '.png')
                current.save(target, format='PNG')
                views.append({'id': view_id, 'path': str(target), 'original_name': source['original_name'] + ' 第' + str(index + 1) + '页.png',
                              'origin_id': source['id'], 'page': index + 1, 'kind': 'page_image',
                              'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'original_sha256': content_hash,
                              'width': current.width, 'height': current.height, 'converter': VERSION})
                current.close()
                current = None
                if pdf is not None:
                    bitmap.close()
                    page.close()
                    bitmap = page = None
            if pdf is not None:
                pdf.close()
            else:
                image.close()
        except BudgetError:
            raise
        except Exception:
            warnings.append(source['original_name'] + '：页面转换未完成，请检查文件或补充清晰图片。')
        finally:
            for resource_object in (current, bitmap, page, pdf, image):
                if resource_object is not None:
                    try:
                        resource_object.close()
                    except Exception:
                        pass
    if not views:
        raise ValueError('没有可供后台读取的图片、PDF 页面或文本 DXF。请人工分件或补充资料。')
    return {'views': views, 'warnings': warnings, 'converter': VERSION}


def main():
    if sys.platform.startswith('linux'):
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
        resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
    if len(sys.argv) != 3:
        raise ValueError('manifest and output folder required')
    manifest = Path(sys.argv[1])
    result = convert(json.loads(manifest.read_text())['files'], Path(sys.argv[2]))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'error': str(exc)[:500]}, ensure_ascii=False))
        sys.exit(1)
