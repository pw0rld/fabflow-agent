"""Source-bound native CAD candidates for the engineering BOM.

Inputs are existing snapshots. No CAD execution, file access, model requests,
pricing or adoption occurs here. Bounding boxes describe assembly-coordinate
product envelopes; they never determine sheet thickness or a flat pattern.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import copy
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re

from cad_bom import validate_material
from cad_material import material_grade_candidate

FIELDS = ('specification', 'thickness_mm', 'material_grade', 'cad_material_raw', 'surface_treatment')
SAMPLE_LIMIT = 16


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and (result > 0 if positive else result >= 0) and result < Decimal('1e24') else None


def _text(value):
    value = format(value, 'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


def _display(value):
    return '<0.01' if 0 < value < Decimal('.01') else format(value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f')


def _base(line):
    return {'values': {key: None for key in FIELDS}, 'evidence': {},
            'field_status': {key: 'unresolved' for key in FIELDS}, 'notes': [], 'status': 'pending_review'}


def _proof(context, bodies, basis, status, reason, **extra):
    identities = [solid.get('id') for solid in bodies if isinstance(solid.get('id'), str)]
    return {'basis': basis, 'status': status, 'reason': reason,
            'source_sha256': context.get('source_sha256'), 'solid_count': len(bodies),
            'solid_ids': identities[:128], 'solid_ids_omitted': max(0, len(identities) - 128), **extra}


def _context(file_id, geometry, source):
    if isinstance(geometry, dict) and isinstance(geometry.get('geometry'), dict):
        wrapper = geometry
        geometry = geometry['geometry']
    else:
        wrapper = {}
    if not isinstance(geometry, dict) or geometry.get('output_unit') != 'mm':
        return None, '本文件没有毫米单位的已保存原生几何。'
    solids = geometry.get('solids')
    if not isinstance(solids, list) or not solids or len(solids) > 50000 or not all(isinstance(row, dict) for row in solids):
        return None, '本文件的已保存原生实体记录无效。'
    sha = source.get('sha256') if isinstance(source, dict) else None
    actual_sha = geometry.get('source_sha256')
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
        return None, '本次报价没有登记该几何对应的有效源文件摘要。'
    if not isinstance(actual_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', actual_sha):
        return None, '已保存原生几何缺少有效的源文件摘要。'
    if (isinstance(sha, str) and sha != actual_sha or wrapper.get('source_sha256') not in {None, actual_sha}):
        return None, '原生几何与本次报价的源文件摘要不一致。'
    grouped = defaultdict(list)
    for solid in solids:
        definition = solid.get('definition_id')
        if isinstance(definition, str) and definition.strip():
            grouped[re.sub(r'/solid-[0-9]+$', '', definition)].append(solid)
    return {'geometry': geometry, 'groups': grouped, 'source_sha256': actual_sha, 'file_id': file_id}, None


def _match(line, context, definition_count):
    definition = line.get('native_definition')
    if (not isinstance(definition, str) or not definition or definition.startswith('@unresolved-')
            or len(definition) > 512 or definition_count != 1):
        return None, '报价行没有唯一且明确的原生零件定义，未自动关联参数。'
    bodies = context['groups'].get(definition, [])
    if (not bodies or type(line.get('native_solid_count')) is not int
            or len(bodies) != line['native_solid_count']):
        return None, '当前原生实体数量与原报价分组不一致，未关联参数。'
    # Reuse the same scalar identity/instance/volume contract as quote preview.
    from batch_quote_costing import _quantity_and_volume, _weight
    quantity, volume, _ = _quantity_and_volume(bodies)
    if (quantity is None or quantity != line.get('quantity_per_set') or volume is None
            or _weight(volume) != line.get('volume_mm3')):
        return None, '装配实例或单件体积与原报价分组不一致，未关联参数。'
    return bodies, None


def _envelope(line, context, bodies, output):
    paths = sorted({tuple(row['occurrence_path']) for row in bodies})
    representative = [row for row in bodies if tuple(row['occurrence_path']) == paths[0]]
    boxes = []
    for row in representative:
        bounds = row.get('bounds_mm')
        if not isinstance(bounds, dict) or not all(isinstance(bounds.get(key), list) and len(bounds[key]) == 3 for key in ('min', 'max')):
            boxes = []
            break
        points = []
        for low, high in zip(bounds['min'], bounds['max']):
            # Coordinates may be negative; dimensions are checked separately.
            try:
                a, b = Decimal(str(low)), Decimal(str(high))
            except (InvalidOperation, ValueError):
                points = []
                break
            if not a.is_finite() or not b.is_finite() or abs(a) >= Decimal('1e24') or abs(b) >= Decimal('1e24') or b <= a:
                points = []
                break
            points.append((a, b))
        if len(points) != 3:
            boxes = []
            break
        boxes.append(points)
    if not boxes:
        output['evidence']['specification'] = _proof(context, bodies, 'native_geometry', 'unresolved', '代表实例缺少完整的原生包络坐标。')
        return
    size = [max(box[axis][1] for box in boxes) - min(box[axis][0] for box in boxes) for axis in range(3)]
    dimensions = ' × '.join(_display(value) for value in size)
    description = '约 ' + dimensions + ' mm（成品包络，装配坐标；非展开尺寸）'
    name = line.get('part_name')
    if (line.get('mode') == 'buy' and isinstance(name, str) and len(name) <= 180
            and re.search(r'(?:^|[ _-])(?:M[0-9]+(?:[X×.\-]|$)|CLS(?:[ _-]|$)|DIN[ _-]?[0-9]|ISO[ _-]?[0-9]|GB/T)', name.upper())):
        description = name + '（CAD名称规格）；' + description
    output['values']['specification'] = description
    output['field_status']['specification'] = 'candidate'
    output['evidence']['specification'] = _proof(context, representative, 'native_geometry', 'candidate',
        '由一个实际装配实例的原生包络合并量测；装配旋转会影响包络尺寸，不是图纸标注或钣金展开。',
        size_mm=[_text(value) for value in size], occurrence_path=list(paths[0]),
        unit='mm', label='成品包络（装配坐标）')


def _valid_thickness(solid):
    value = solid.get('thickness')
    if not isinstance(value, dict) or value.get('status') != 'candidate':
        return None
    number = _number(value.get('value_mm'), positive=True)
    evidence = value.get('evidence')
    method, confidence = value.get('method'), value.get('confidence')
    if (number is None or number > 10000 or method not in {'parallel_faces', 'coaxial_cylinders', 'combined'}
            or confidence not in {'high', 'medium'} or not isinstance(evidence, list) or not evidence or len(evidence) > 128):
        return None
    kinds = set()
    for item in evidence:
        if not isinstance(item, dict):
            return None
        kinds.add(item.get('kind'))
        measured = _number(item.get('thickness_mm'), positive=True)
        support = _number(item.get('support_area_mm2'), positive=True)
        faces = item.get('face_ids')
        if (item.get('kind') not in {'parallel_faces', 'coaxial_cylinders'} or measured is None or support is None
                or abs(measured - number) > max(Decimal('.001'), number * Decimal('.001'))
                or not isinstance(faces, list) or len(faces) != 2 or len(set(map(str, faces))) != 2
                or not all(isinstance(face, str) and re.fullmatch(r'F[0-9]{4,7}', face) for face in faces)):
            return None
        count = solid.get('topology', {}).get('faces') if isinstance(solid.get('topology'), dict) else None
        if type(count) is int and any(not 1 <= int(face[1:]) <= count for face in faces):
            return None
    if kinds != ({'parallel_faces', 'coaxial_cylinders'} if method == 'combined' else {method}):
        return None
    return number


def _thickness(context, bodies, output):
    found = [_valid_thickness(row) for row in bodies]
    known = [value for value in found if value is not None]
    def face_samples(measurement):
        rows = measurement.get('evidence', [])
        selected = copy.deepcopy(rows[:4])
        seen = {(item.get('kind'), item.get('thickness_mm')) for item in selected}
        for item in rows[4:]:
            identity = (item.get('kind'), item.get('thickness_mm'))
            if identity not in seen and len(selected) < 16:
                selected.append(copy.deepcopy(item))
                seen.add(identity)
        return selected
    evidence = [{'solid_id': row.get('id'), 'status': row.get('thickness', {}).get('status'),
                 'value_mm': _text(value) if value is not None else None,
                 'method': row.get('thickness', {}).get('method'),
                 'reason': row.get('thickness', {}).get('reason'),
                 'warnings': copy.deepcopy(row.get('thickness', {}).get('warnings', [])[:8]),
                 'face_evidence': face_samples(row.get('thickness', {}))}
                for row, value in zip(bodies[:SAMPLE_LIMIT], found[:SAMPLE_LIMIT])
                if isinstance(row.get('thickness'), dict)]
    if len(known) >= 2 and max(known) - min(known) > max(Decimal('.001'), max(known) * Decimal('.001')):
        status, reason = 'conflict', '同一报价分组的实体板厚候选不一致，不能共用一个板厚。'
    elif len(known) == len(bodies):
        status, reason = 'candidate', '全部对应实体的原生薄壁面量测一致，仍待工程核对。'
        output['values']['thickness_mm'] = _text((sum(known, Decimal(0)) / len(known)).quantize(Decimal('.00000001'), rounding=ROUND_HALF_UP))
    elif all(isinstance(row.get('thickness'), dict) and row['thickness'].get('status') == 'not_applicable' for row in bodies):
        status, reason = 'not_applicable', '原生量测标记该组没有适用的板厚候选。'
    else:
        status, reason = 'unresolved', '对应实体缺少完整的薄壁面量测候选；没有从外形最短边猜测板厚。'
        details = list(dict.fromkeys(item['reason'] for item in evidence
                       if isinstance(item.get('reason'), str) and item['status'] != 'candidate'))
        if details:
            reason = '；'.join(details[:4])
    output['field_status']['thickness_mm'] = status
    output['evidence']['thickness_mm'] = _proof(context, bodies, 'native_geometry', status, reason,
        unit='mm', samples=evidence, samples_omitted=max(0, len(bodies) - len(evidence)))


def _material(context, bodies, output):
    materials = []
    for row in bodies:
        candidate = row.get('material')
        try:
            validate_material(candidate)
        except (ValueError, TypeError, KeyError):
            materials.append(None)
        else:
            materials.append(candidate)
    grades = [material_grade_candidate(row) for row in materials]
    keys = {row['grade_key'] for row in grades if row['status'] == 'candidate'}
    if any(row['status'] == 'conflict' for row in grades) or len(keys) > 1:
        status, reason = 'conflict', '同一分组的CAD材料名或描述包含不同具体牌号，未自动选择。'
    elif keys and all(row['status'] == 'candidate' for row in grades):
        status, reason = 'candidate', '全部对应实体的CAD材料名或描述有一致的具体牌号，仍待工程核对。'
        output['values']['material_grade'] = grades[0]['value']
    else:
        status, reason = 'unresolved', 'CAD未为全部对应实体提供有效具体牌号；模板字段和通用名称仅保留原文，未从文件名或外观推定。'
    samples = [{'solid_id': solid.get('id'), 'value': material['value'],
                'entries': copy.deepcopy(material['entries'][:8]),
                'grade_status': grade['status'], 'grade_evidence': copy.deepcopy(grade['evidence'][:16])}
               for solid, material, grade in zip(bodies, materials, grades) if material is not None][:SAMPLE_LIMIT]
    output['field_status']['material_grade'] = status
    output['evidence']['material_grade'] = _proof(context, bodies, 'cad_property', status, reason, samples=samples)
    # Verbatim source material names are observations, not proof of a grade or
    # an estimating profile. In particular COMPANY/PTC_MASTER_MATERIAL survive
    # here for audit while material_grade remains unresolved.
    raw_values = []
    for material in materials:
        names = ([material['value']] if material and material['value'] else
                 [entry['name'].strip() for entry in material['entries'] if entry['name'].strip()] if material else [])
        raw_values.append(' / '.join(sorted(set(names))) or None)
    known_raw = {value for value in raw_values if value is not None}
    if len(known_raw) == 1 and all(value is not None for value in raw_values):
        raw_status, raw_reason = 'observed', '仅转录CAD材料属性原文，不表示有效牌号或已采用的估价材料。'
        output['values']['cad_material_raw'] = next(iter(known_raw))
    elif len(known_raw) > 1:
        raw_status, raw_reason = 'conflict', '同一分组的CAD材料属性原文不同；各侧原文保留在来源记录。'
    else:
        raw_status, raw_reason = 'unresolved', '未取得所有对应实体的完整材料原始名称。'
    output['field_status']['cad_material_raw'] = raw_status
    output['evidence']['cad_material_raw'] = _proof(context, bodies, 'cad_property_raw', raw_status, raw_reason,
                                                 samples=copy.deepcopy(samples))


def enrich_native(result, geometries):
    """Return pending-review candidates keyed by original quote line ID.

    ``geometries`` is keyed by the quotation's ``file_id`` and accepts a raw
    geometry snapshot or ``{geometry, source_sha256}`` preview context. Matching
    is the existing definition/instance/volume contract, never a name heuristic.
    """
    result = result if isinstance(result, dict) else {}
    geometries = geometries if isinstance(geometries, dict) else {}
    lines = [row for row in result.get('lines', []) if isinstance(row, dict)]
    sources = {row.get('id'): row for row in result.get('source_files', []) if isinstance(row, dict)}
    def line_key(row):
        return tuple(row.get(key) if isinstance(row.get(key), str) else None for key in ('file_id', 'native_definition'))
    counts = Counter(line_key(row) for row in lines)
    contexts = {}
    output = {}
    with localcontext() as context:
        context.prec = 64
        for line in lines:
            identity = line.get('id')
            if not isinstance(identity, str):
                continue
            row = _base(line)
            output[identity] = row
            file_id = line_key(line)[0]
            if file_id not in contexts:
                contexts[file_id] = _context(file_id, geometries.get(file_id), sources.get(file_id))
            native, error = contexts[file_id]
            if error:
                row['notes'].append(error)
                continue
            bodies, error = _match(line, native, counts[line_key(line)])
            if error:
                row['notes'].append(error)
                continue
            _envelope(line, native, bodies, row)
            _thickness(native, bodies, row)
            _material(native, bodies, row)
            row['evidence']['surface_treatment'] = _proof(native, bodies, 'cad_property', 'unresolved',
                '已保存的原生几何中未提供明确表面处理属性；此项需要从图纸或工艺说明读取。')
    return {'lines': output, 'stats': {'lines': len(output),
            **{field + '_candidates': sum(row['values'][field] is not None for row in output.values()) for field in FIELDS},
            'thickness_conflicts': sum(row['field_status']['thickness_mm'] == 'conflict' for row in output.values()),
            'material_conflicts': sum(row['field_status']['material_grade'] == 'conflict' for row in output.values())}}
