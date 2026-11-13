#!/usr/bin/env python3
"""Isolated HTTP/browser fixture. Scripted reading only; never DSH or a model.

All input files are generated here, and the real Workbench upload, worker,
source validation and persistence paths run against a private temporary root.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from workbench_store import Workbench
from workbench_server import WorkbenchHandler, make_server


def make_pdf(path):
    """Two genuinely renderable PDF pages, with no external resources."""
    content = (b'0.8 w 80 180 300 160 re S 100 200 260 120 re S '
               b'BT /F1 20 Tf 80 420 Td (LOCAL FIXTURE DRAWING A) Tj '
               b'0 -40 Td (SUS304 - T=1.5 mm - 300 x 160) Tj '
               b'0 -40 Td (GENERAL TOLERANCE: +/-0.2 mm) Tj '
               b'0 -40 Td (WELD: a3 L=40 2X) Tj '
               b'0 -40 Td (BOM 10 | PLATE | SUS304 | 2 pcs/set) Tj ET')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 7 0 R >> >> /Contents 4 0 R >>',
               b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 7 0 R >> >> /Contents 6 0 R >>',
               b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    data = b'%PDF-1.4\n'
    offsets = [0]
    for index, value in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(index).encode() + b' 0 obj\n' + value + b'\nendobj\n'
    offset = len(data)
    data += b'xref\n0 8\n0000000000 65535 f \n'
    data += b''.join(f'{value:010d} 00000 n \n'.encode() for value in offsets[1:])
    data += b'trailer\n<< /Size 8 /Root 1 0 R >>\nstartxref\n' + str(offset).encode() + b'\n%%EOF\n'
    path.write_bytes(data)


def fixture_font(size):
    """Use a configured or installed font; fixtures need no host-specific font."""
    from PIL import ImageFont
    configured = os.environ.get('HYL_TEST_FONT')
    candidates = [configured] if configured else [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf',
        '/System/Library/Fonts/Supplemental/Arial.ttf',
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, size)
    raise RuntimeError('Set HYL_TEST_FONT to an installed TrueType font for browser fixtures')


def make_png(path):
    from PIL import Image, ImageDraw
    image = Image.new('RGB', (960, 640), 'white')
    draw = ImageDraw.Draw(image)
    font = fixture_font(24)
    draw.text((45, 35), 'LOCAL FIXTURE DRAWING B', fill='#1e293b', font=font)
    draw.text((45, 80), 'Q235B - T=2.0 mm - 220 x 120', fill='#334155', font=font)
    draw.rectangle((170, 210, 730, 470), outline='#1e293b', width=4)
    for x in (215, 685):
        for y in (255, 425):
            draw.ellipse((x - 12, y - 12, x + 12, y + 12), outline='#334155', width=3)
    draw.line((170, 170, 730, 170), fill='#64748b', width=2)
    draw.text((415, 130), '220 mm', fill='#334155', font=font)
    image.save(path)


class Factory:
    def __init__(self):
        self.lock = threading.RLock()
        self.modes = {}
        self.starts = []
        self.inspections = []
        self.cancelled = []
        self.sessions = {}

    def multipart_result(self, files, reading_pass):
        """Distinct per-part facts, all in this isolated scripted fixture."""
        pdf, image = files[0]['id'], files[1]['id']

        def evidence(reference=pdf, page=1, region='零件视图'):
            return {'reference': reference, 'page': page, 'region': region}

        def row(name, text, *, reference=pdf, page=1, region='零件视图', source_name=None, raw_unit=None):
            result = {'name': name, 'raw_text': text, 'raw_unit': raw_unit,
                      'status': 'observed', 'evidence': evidence(reference, page, region),
                      'reason': '脚本夹具原文，只验证按件展示与归属，不代表模型识别。'}
            if source_name:
                result['source_name'] = source_name
            return result

        def part(key, name, number, version, references, observations):
            return {'key': key, 'name': name, 'part_number': number, 'version': version,
                    'status': 'pending_review', 'evidence': references, 'observations': observations}

        shell = part('shell-a', '机箱外壳', 'SHELL-01', 'A', [evidence(region='左侧机箱外壳')], [
            row('material_grade', 'SUS304 · 仅外壳A'),
            row('thickness', 'T = 1.5 mm · 仅外壳A', raw_unit='mm'),
            row('engineering_open_items', '240 +/-0.2 x 160 x 80 mm · 外壳A完整尺寸',
                source_name='外形尺寸', raw_unit='mm'),
            row('surface_treatment', '外壳A：拉丝'),
            row('engineering_open_items', '外壳A：WELD a3 L=40 2X；全部锐边去毛刺。', source_name='焊接要求'),
        ])
        cover = part('cover-a', '机箱盖板', 'COVER-02', 'A', [evidence(region='右侧盖板')], [
            row('material_grade', 'Q235B · 仅盖板'),
            row('thickness', 'T = 3.0 mm · 仅盖板', raw_unit='mm'),
            row('engineering_open_items', '230 x 150 mm · 盖板尺寸', source_name='外形尺寸', raw_unit='mm'),
            row('surface_treatment', '盖板：喷粉 RAL7035'),
        ])
        parts = [shell, cover]
        if reading_pass == 'review':
            bracket = part('bracket-a', '安装支架', 'BRACKET-03', 'A',
                [evidence(page=2, region='支架主视图'), evidence(image, 1, '同件补充视图')], [
                    row('material_grade', 'AL5052 · 仅支架', page=2),
                    row('thickness', 'T = 2.0 mm · 仅支架', page=2, raw_unit='mm'),
                    row('engineering_open_items', '40 x 30 x 20 mm · 支架尺寸', page=2,
                        source_name='外形尺寸', raw_unit='mm'),
                    row('engineering_open_items', '支架补充图：4 x DIA 6，孔距 24 +/-0.1 mm；完整孔位要求。',
                        reference=image, source_name='孔位尺寸', raw_unit='mm'),
                ])
            shell_b = part('shell-b', '机箱外壳', 'SHELL-01', 'B',
                [evidence(page=2, region='外壳版本B')], [
                    row('material_grade', 'SUS316L · 仅外壳B', page=2),
                    row('thickness', 'T = 2.5 mm · 仅外壳B', page=2, raw_unit='mm'),
                    row('engineering_open_items', '260 x 180 x 90 mm · 外壳B尺寸', page=2,
                        source_name='外形尺寸', raw_unit='mm'),
                ])
            parts.extend([bracket, shell_b])
        return {'status': 'completed', 'error': None, 'parts': parts, 'warnings': [],
                'candidates': [], 'observations': [deepcopy(row) for item in parts for row in item['observations']]}

    def snapshot(self):
        with self.lock:
            return deepcopy({'starts': self.starts, 'inspections': self.inspections,
                             'cancelled': self.cancelled, 'modes': self.modes,
                             'model_called': False, 'production_data_used': False})

    def __call__(self, workspace):
        factory = self

        class Bridge:
            def start(self, task_id, files, inquiry, output_path, *, on_session, review_fields=None):
                # Every run owns its initial and review workspaces; a stable
                # digest distinguishes review directories across tasks/runs.
                session_id = 'fixture-session-' + hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:24]
                reading_pass = 'review' if review_fields is not None else 'initial'
                # The real bridge pins original PDF page counts. Keep that
                # boundary in the fake bridge too, without changing task state
                # or pretending a rendered page/model call took place here.
                import pypdfium2
                page_counts = {}
                for item in files:
                    if Path(item['original_name']).suffix.lower() == '.pdf':
                        document = pypdfium2.PdfDocument(item['path'])
                        try:
                            page_counts[item['id']] = len(document)
                        finally:
                            document.close()
                Path(workspace, '.dsh-bridge-run.json').write_text(json.dumps({
                    'task_id': task_id, 'session_id': session_id,
                    'files': [{'id': item['id'], 'sha256': hashlib.sha256(Path(item['path']).read_bytes()).hexdigest()}
                              for item in files],
                    'pdf_views': {'page_counts': page_counts},
                }), encoding='utf-8')
                with factory.lock:
                    factory.sessions[session_id] = {'task_id': task_id, 'files': deepcopy(files),
                                                    'pass': reading_pass,
                                                    'review_fields': deepcopy(review_fields)}
                    factory.starts.append({'task_id': task_id, 'session_id': session_id,
                                           'pass': reading_pass, 'review_fields': deepcopy(review_fields),
                                           'files': [{key: item[key] for key in ('id', 'original_name')}
                                                     for item in files]})
                on_session(session_id)
                return {'session_id': session_id, 'status': 'running', 'error': None}

            def inspect(self, session_id):
                with factory.lock:
                    session = deepcopy(factory.sessions[session_id])
                    # The initial pass remains controllable for background and
                    # failure scenarios. The scripted omission sweep finishes
                    # without a second browser action or a real model call.
                    control_mode = factory.modes.get(session['task_id'], 'running')
                    if control_mode.startswith('multipart-'):
                        mode = 'running' if session['pass'] == 'review' and control_mode == 'multipart-initial' else 'completed'
                    else:
                        mode = 'completed' if session['pass'] == 'review' else control_mode
                    factory.inspections.append({'session_id': session_id, 'mode': mode,
                                                'pass': session['pass']})
                if mode == 'failed':
                    return {'status': 'failed', 'error': {'message': '本地模拟：图纸读取暂时失败，请重试。'}, 'warnings': []}
                if mode != 'completed':
                    return {'status': 'running', 'error': None, 'warnings': [],
                            'progress': {'stage': 'reading', 'label': '正在读取图纸（本地模拟）',
                                         'last_event_seq': 1, 'last_activity_at': int(time.time() * 1000),
                                         'events': [{'seq': 1, 'stage': 'reading',
                                                     'label': '正在读取图纸（本地模拟）',
                                                     'at': int(time.time() * 1000)}]}}
                files = session['files']
                if control_mode.startswith('multipart-'):
                    return factory.multipart_result(files, session['pass'])
                first = files[0]['id']
                def evidence(reference=first, page=1, region='标题栏（本地模拟）'):
                    return {'reference': reference, 'page': page, 'region': region}
                def candidate(name, value, unit=None):
                    return {'name': name, 'value': value, 'raw_value': value,
                            'raw_unit': unit, 'unit': unit, 'evidence': evidence(),
                            'reason': '脚本夹具返回，仅验证界面，不代表真实模型识别。'}
                if session['pass'] == 'review':
                    # These page-2 notes were intentionally absent from the
                    # initial summary. The UI must show the actual later raw
                    # evidence, including a BOM quantity that is not an order.
                    return {'status': 'completed', 'error': None, 'candidates': [], 'warnings': [],
                            'observations': [
                                {'name': 'engineering_open_items', 'source_name': '尺寸公差',
                                 'raw_text': 'GENERAL TOLERANCE: +/-0.2 mm（本地模拟）',
                                 'raw_unit': 'mm', 'status': 'observed',
                                 'evidence': evidence(page=2, region='一般公差说明'),
                                 'reason': '本地模拟：第二遍补读原文'},
                                {'name': 'engineering_open_items', 'source_name': '焊接标注',
                                 'raw_text': 'WELD: a3 L=40 2X（本地模拟）',
                                 'raw_unit': None, 'status': 'observed',
                                 'evidence': evidence(page=2, region='焊缝技术备注'),
                                 'reason': '本地模拟：第二遍补读原文'},
                                {'name': 'engineering_open_items', 'source_name': 'BOM明细行',
                                 'raw_text': '10 | PLATE | SUS304 | 2 pcs/set（本地模拟）',
                                 'raw_unit': None, 'status': 'observed',
                                 'evidence': evidence(page=2, region='BOM明细表第1行'),
                                 'reason': '保留单套用量原文，不作为订单数量'},
                            ]}
                observations = [
                    {'name': 'material_grade', 'raw_text': 'SUS304（本地模拟）', 'raw_unit': None,
                     'status': 'observed', 'evidence': evidence(), 'reason': '模拟标题栏'},
                    {'name': 'thickness', 'raw_text': 'T = 1.5 mm（本地模拟）', 'raw_unit': 'mm',
                     'status': 'observed', 'evidence': evidence(page=2, region='侧视图板厚标注'), 'reason': '模拟原PDF第2页'},
                    {'name': 'engineering_open_items', 'source_name': 'hole_diameter',
                     'raw_text': '4 x DIA 6（本地模拟）', 'raw_unit': 'mm', 'status': 'observed',
                     'evidence': evidence(page=2, region='孔位标注'), 'reason': '仅记录原文'},
                    {'name': 'engineering_open_items', 'source_name': 'fixture_html',
                     'raw_text': '<img src="https://fixture.invalid/x" onerror="window.fixtureInjected=true">',
                     'raw_unit': None, 'status': 'observed', 'evidence': evidence(page=2),
                     'reason': '<script>window.fixtureInjected=true</script>'},
                ]
                if len(files) > 1:
                    observations.append({'name': 'thickness', 'raw_text': 'T = 2.0 mm（附件B本地模拟）',
                                         'raw_unit': 'mm', 'status': 'observed',
                                         'evidence': evidence(files[1]['id'], 1, 'PNG材料说明'),
                                         'reason': '不同附件原文分别保留'})
                return {'status': 'completed', 'error': None,
                        'candidates': [candidate('product_name', '演示识别结果'),
                                       candidate('material_grade', 'SUS304'), candidate('drawing_units', 'mm'),
                                       candidate('thickness', '1.5', 'mm')],
                        'observations': observations,
                        'warnings': ['本地模拟：密封要求需人工回查原图。']}

            def stop(self, session_id):
                with factory.lock:
                    factory.cancelled.append(session_id)
                    factory.modes[factory.sessions[session_id]['task_id']] = 'failed'
                return {'accepted': True, 'status': 'stopping'}

        return Bridge()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    directory = args.directory.resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fixtures = directory / 'fixtures'
    fixtures.mkdir(mode=0o700)
    make_pdf(fixtures / '模拟零件A-两页.pdf')
    make_png(fixtures / '模拟零件B.png')
    (fixtures / '模拟轮廓C.dxf').write_text(
        '0\nSECTION\n2\nHEADER\n9\n$ACADVER\n1\nAC1015\n0\nENDSEC\n'
        '0\nSECTION\n2\nENTITIES\n0\nLINE\n8\n0\n10\n0\n20\n0\n'
        '11\n100\n21\n50\n0\nENDSEC\n0\nEOF\n', encoding='ascii')
    factory = Factory()
    app = Workbench(directory / 'tasks', reader_root=directory / 'readers', bridge_factory=factory)

    class Handler(WorkbenchHandler):
        def _get(self):
            if urlsplit(self.path).path == '/__fixture__/stats':
                return self._json(factory.snapshot())
            return super()._get()

        def _post(self):
            if urlsplit(self.path).path == '/__fixture__/control':
                self._origin()
                data = self._json_body()
                assert set(data) == {'task_id', 'mode'} and data['mode'] in {
                    'running', 'completed', 'failed', 'multipart-initial', 'multipart-completed'}
                app.task(data['task_id'])
                with factory.lock:
                    factory.modes[data['task_id']] = data['mode']
                return self._json({'ok': True})
            return super()._post()

    server = make_server(app, port=args.port)
    server.RequestHandlerClass = Handler
    print(json.dumps({'origin': 'http://127.0.0.1:' + str(server.server_port),
                      'files': [str(file) for file in sorted(fixtures.iterdir()) if file.suffix in {'.pdf', '.png'}],
                      'cad_file': str(fixtures / '模拟轮廓C.dxf'),
                      'model_called': False, 'production_data_used': False}, ensure_ascii=False), flush=True)
    with server:
        server.serve_forever()


if __name__ == '__main__':
    main()
