#!/usr/bin/env python3
"""Build fixed public STEP examples on ZeroClave, without customer data or AI."""
from __future__ import annotations

import argparse
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import gc
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = (
    {'id': 'motor-bracket', 'title': '电机安装支架', 'category': '折弯与孔位',
     'description': '常见 NEMA-17 安装支架，查看原生几何、孔槽与板厚候选。',
     'input': 'samples/cad-test-20261019/step/Nema-17_MountBracket.step',
     'filename': 'Nema-17_MountBracket.step',
     'sha256': 'ee28ab7d424d166a28d2e92eabe38c1a5dab59d0ca169cd2e3516e0e8015af41',
     'repository': 'FreeCAD/FreeCAD-library', 'commit': 'f5ca01cdcaa2f69b9295a6382719c2d90628780c',
     'url': 'https://github.com/FreeCAD/FreeCAD-library/blob/f5ca01cdcaa2f69b9295a6382719c2d90628780c/Electrical%20Parts/Motors/Stepper/NEMA/Nema-17_Mount_Bracket/Nema-17_MountBracket.step',
     'license': 'CC-BY-3.0', 'license_url': 'https://creativecommons.org/licenses/by/3.0/',
     'license_file': 'samples/cad-test-20261019/step/ATTRIBUTION.txt',
     'attribution': 'FreeCAD-library contributors; contributed by Charles Fox. Original model author not confirmed; the contributor is not assumed to be the author.'},
    {'id': 'sheet-metal-enclosure', 'title': '钣金机箱总装', 'category': '装配与拆件',
     'description': '公开开源机箱，包含壳体、盖板、隔板及重复角件，查看原有装配与候选 BOM。',
     'input': 'samples/public-enclosure-20261026/input/Enclosure-v3-v9.step',
     'filename': 'Enclosure-v3-v9.step',
     'sha256': '57a5ecf18133054f7498211d9120e7d101f1e7a0afb7a86e6de42ef473fbe935',
     'repository': 'Laser4DIY/Laser4DIY_Enclosure', 'commit': '67dce7facd462c5cb7868c0735f005edb44ef3dd',
     'url': 'https://github.com/Laser4DIY/Laser4DIY_Enclosure/blob/67dce7facd462c5cb7868c0735f005edb44ef3dd/CAD/Enclosure%20v3%20v9.step',
     'license': 'CERN-OHL-P-2.0', 'license_url': 'https://ohwr.org/cern_ohl_p_v2.txt',
     'license_file': 'samples/public-enclosure-20261026/LICENSE-CERN-OHL-P-v2.txt',
     'attribution': 'Copyright FabLab München e.V. 2021. Laser4DIY Enclosure; CERN-OHL-P v2.'},
    {'id': 'flanged-bushing', 'title': '法兰衬套', 'category': '机加工零件',
     'description': '常见带法兰套筒单件，查看外形、内孔、体积与示例估价。',
     'input': 'samples/public-cad-examples-20261114/flanged-bushing.step',
     'filename': 'flanged-bushing.step',
     'sha256': 'ff1c4f89904db8655a32e4714a3a2cfba4a8490c4ea7c774805bd3a7a67e9b9d',
     'repository': 'cadprobs-a11y/cadprops-step-models', 'commit': 'cfffc2facc1700e763149f979efdd65ee73751ed',
     'url': 'https://github.com/cadprobs-a11y/cadprops-step-models/blob/cfffc2facc1700e763149f979efdd65ee73751ed/parts/sleeves-and-washers/flanged-bushing/flanged-bushing.step',
     'license': 'CC0-1.0', 'license_url': 'https://creativecommons.org/publicdomain/zero/1.0/',
     'license_file': 'samples/public-cad-examples-20261114/CC0-1.0.txt',
     'attribution': 'CADProps original procedural CAD; dedicated to the public domain under CC0-1.0.'},
)
BASE = '/workbench/public-examples/'


def money(value):
    return str(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))


def write_json(path, value):
    content = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    assert len(content) <= 24 * 1024 * 1024
    path.write_bytes(content)


def prepare(spec, native_python, cache, output):
    source = ROOT / spec['input']
    assert not source.is_symlink()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == spec['sha256'], 'Public source hash differs'
    native = cache / spec['id']
    if not (native / 'geometry.json').is_file():
        process = subprocess.run([native_python, str(ROOT / 'src/cad_geometry.py'), str(source),
            '--output', str(native)], capture_output=True, text=True, timeout=600)
        (cache / (spec['id'] + '-native.log')).write_text(process.stdout + process.stderr)
        if process.returncode:
            raise RuntimeError(spec['id'] + ': native reader failed; see build receipt')
    raw = json.loads((native / 'geometry.json').read_bytes())
    assert raw['source_sha256'] == spec['sha256'] and raw['output_unit'] == 'mm'
    solids = raw['solids']
    assert 0 < len(solids) <= 200 and len({s['id'] for s in solids}) == len(solids)
    assert all(s['topology']['valid'] and s['topology']['closed'] and s['volume_mm3'] > 0 for s in solids)
    definitions = OrderedDict()
    for solid in solids:
        definitions.setdefault(solid['definition_id'], []).append(solid)
    parts, items, groups, cost = [], [], [], Decimal(0)
    for index, members in enumerate(definitions.values(), 1):
        first, part_id = members[0], f'P{index:04d}'
        # A neutral exporter label is not a useful public part name. Keep it
        # verbatim beside an explicitly curated display name from this registry.
        display_name = spec['title'] if first['name'].startswith('Open CASCADE STEP translator ') else first['name']
        volume = Decimal(str(first['volume_mm3']))
        assert all(abs(Decimal(str(s['volume_mm3']))-volume) <= max(Decimal('.0001'), volume*Decimal('.000001')) for s in members)
        thickness, material = first.get('thickness') or {}, first.get('material') or {}
        weight, count = volume * Decimal('7850') / Decimal('1000000000'), len(members)
        qty = count * 10
        unit_cost = weight / Decimal('.85') * Decimal('3.20') + max(Decimal('5'), weight * Decimal('8')) + Decimal('30') / qty
        price = (unit_cost * Decimal('1.20')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        cost += unit_cost * qty
        groups.append({'id': part_id, 'name': display_name, 'solid_ids': [s['id'] for s in members]})
        parts.append({'id': part_id, 'name': display_name, 'source_name': first['name'], 'quantity': count,
            'solid_ids': [s['id'] for s in members], 'bbox_mm': first['bounds_mm']['size'],
            'thickness_mm': thickness.get('value_mm') if thickness.get('status') == 'candidate' else None,
            'volume_mm3': first['volume_mm3'],
            'material_raw': material.get('value') if material.get('status') == 'candidate' else None,
            'estimated_net_weight_kg': format(weight.quantize(Decimal('.00000001')), 'f'), 'material_assumption': '碳钢（示例估算条件）',
            'quantity_basis': '原 STEP 定义与实例；候选 BOM，未作制造审核',
            'thickness_method': thickness.get('method'), 'thickness_status': thickness.get('status')})
        items.append({'part_id': part_id, 'quantity_total': qty, 'unit_price': money(price), 'amount': money(price * qty)})
    total = sum((Decimal(item['amount']) for item in items), Decimal(0))
    target = output / spec['id']
    target.mkdir()
    write_json(target / 'geometry.json', {'source_sha256': spec['sha256'], 'output_unit': 'mm',
        'solids': [{'id': s['id'], 'name': s['name'], 'mesh': s['mesh']} for s in solids], 'groups': groups})
    shutil.copyfile(source, target / 'source.step')
    notice = spec['attribution'] + '\nSource: ' + spec['url'] + '\nLicense: ' + spec['license'] + '\nLicense URL: ' + spec['license_url'] + '\n\n'
    (target / 'LICENSE.txt').write_text(notice + (ROOT / spec['license_file']).read_text())
    detail = {'schema_version': 1, 'id': spec['id'], 'title': spec['title'], 'category': spec['category'],
        'description': spec['description'], 'filename': spec['filename'], 'units': 'mm',
        'source': {k: spec[k] for k in ('repository', 'url', 'commit', 'sha256', 'filename', 'license', 'license_url', 'attribution')},
        'stats': {'part_count': len(parts), 'occurrence_count': len(solids), 'solid_count': len(solids)},
        'geometry_url': BASE + spec['id'] + '/geometry.json', 'download_url': BASE + spec['id'] + '/source.step',
        'parts': parts, 'quotation': {'order_sets': 10, 'currency': 'CNY', 'subtotal': money(cost),
            'markup_percent': '20', 'total': money(total), 'unit_per_set': money(total / 10),
            'status': 'illustrative', 'items': items,
            'assumptions': ['公开样例模拟估价，不是供应商报价或工厂标准价。',
                '统一采用碳钢估算条件：密度7850kg/m³、材料3.20元/kg、利用率85%；不将此条件认定为CAD材质。',
                '基础加工示例费=max(5元/件,预估净重×8元/kg)，每种源定义一次性准备费30元，成本加成20%；未税、不含运费。',
                '未完成工艺工时核价、排版、展开补偿或制造审核；几何数量与板厚为待工程核对的候选。']},
        'prepared_at': datetime.now(timezone.utc).isoformat()}
    detail['source']['changes'] = '原STEP字节未修改；FabFlow读取原生几何生成网格、候选分组、参数与明确假设的模拟报价。'
    write_json(target / 'example.json', detail)
    row = {k: detail[k] for k in ('id', 'title', 'category', 'description', 'filename', 'geometry_url', 'download_url')}
    row.update(source_label=spec['repository'], license=spec['license'], part_count=len(parts),
        occurrence_count=len(solids), detail_url=BASE + spec['id'] + '/example.json')
    print(json.dumps({'example': spec['id'], 'parts': len(parts), 'occurrences': len(solids),
        'geometry_bytes': (target / 'geometry.json').stat().st_size}), flush=True)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-python', required=True)
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'linux':
        raise SystemExit('CAD builds and tests run on ZeroClave only.')
    assert not args.output.exists(), 'Use a fresh output directory'
    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.mkdir(parents=True)
    entries = []
    for spec in EXAMPLES:
        entries.append(prepare(spec, args.native_python, args.cache, args.output))
        gc.collect()
    write_json(args.output / 'catalog.json', {'schema_version': 1, 'examples': entries})


if __name__ == '__main__':
    main()
