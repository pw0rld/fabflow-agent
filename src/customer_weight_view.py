"""Read-only customer net-weight views over saved quotation line values.

This module never measures CAD, selects density/material, prices a process or
changes a saved result. Summaries add the saved, unrounded row weights only.
"""
from decimal import Decimal, InvalidOperation, localcontext
import re

from batch_engineering_bom import MATERIAL_LABELS
from batch_quote_costing import _kg
from cad_material import is_placeholder_material_name

WEIGHT_FIELDS = ('estimated_net_weight_kg', 'estimated_set_net_weight_kg', 'estimated_total_net_weight_kg')


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 100:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if (parsed.is_finite() and 0 <= parsed <= Decimal('1e18')
                      and parsed.as_tuple().exponent >= -40) else None


def saved_number(value):
    parsed = number(value)
    if parsed is None:
        return None
    return format(parsed, 'f')


def display_weight(value):
    parsed = number(value)
    return _kg(parsed) if parsed is not None else '尚未估算'


def weight_assumptions(line):
    confidence = line.get('confidence')
    sources = {'cad_material': ('cad_candidate', 'CAD材料候选'),
               'assumed_material': ('order_assumption', '本单材料假设'),
               'engineering_material_candidate': ('engineering_candidate', '工程材料候选'),
               'proxy_material_estimate': ('proxy_estimate', '代理模型粗估'),
               'low': ('low_confidence', '异常几何粗估')}
    source, description = sources.get(confidence, ('unknown', '保存的估重依据待核对'))
    raw = line.get('material_raw')
    raw = ' '.join(raw.split())[:500] if isinstance(raw, str) and raw.strip() else None
    if is_placeholder_material_name(raw):
        raw = None
    density = number(line.get('density_kg_m3'))
    profile = line.get('material_profile')
    label = MATERIAL_LABELS.get(profile) if isinstance(profile, str) else None
    if (profile == 'ss304' and confidence == 'assumed_material' and not raw
            and line.get('material_estimation_label') == '不锈钢估价（参考304）'):
        label = '不锈钢估价（参考304）'
    document = line.get('document')
    hint = None
    if isinstance(document, str) and re.search(r'不锈钢|stainless', document[:1000], re.IGNORECASE):
        conflict = profile not in {'ss304', 'ss316', 'stainless_mesh'}
        hint = {'source': 'filename', 'family': 'stainless_steel', 'grade': None,
                'label': '不锈钢（文件名线索，未指定牌号）', 'conflicts_with_adopted_material': conflict}
    return {'density_kg_m3': saved_number(density) if density is not None and density > 0 else None,
            'cad_material_text': raw, 'adopted_material_label': label,
            'source': source, 'description': description, 'includes_packaging': False, 'requirement_hint': hint}


def _dimensions(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    values = [number(item) for item in value]
    return [saved_number(item) for item in values] if all(item is not None and item > 0 for item in values) else None


def weight_evidence(line, native=None, *, source_sha256=None, geometry_source='saved_native_evidence'):
    """Project saved scalar facts, without deriving thickness or recalculating mass.

    ``native`` is a source-bound engineering-native candidate, either saved in
    the quote or freshly projected from its existing verified geometry cache.
    Volume is the saved *single part* value, not an occurrence/order sum.
    """
    assumptions = weight_assumptions(line)
    volume = number(line.get('volume_mm3'))
    volume = saved_number(volume) if volume is not None and volume > 0 else None
    volume_status = ('unavailable' if volume is None else 'low_confidence' if line.get('confidence') == 'low'
                     else 'proxy' if line.get('confidence') == 'proxy_material_estimate' else 'native')
    output = {'native_volume_mm3': volume, 'volume_scope': 'per_piece',
              'volume_source': 'saved_native_volume' if volume is not None else 'unavailable',
              'volume_status': volume_status,
              'dimensions_mm': None, 'dimensions_source': 'unavailable',
              'thickness_mm': None, 'thickness_status': 'unresolved', 'thickness_source': 'unavailable',
              'measured_thickness_candidates_mm': [],
              'density_kg_m3': assumptions['density_kg_m3'],
              'adopted_material_label': assumptions['adopted_material_label'],
              'cad_material_text': assumptions['cad_material_text'],
              'material_source': assumptions['source'], 'material_description': assumptions['description'],
              'material_requirement_hint': assumptions['requirement_hint'],
              'includes_packaging': False, 'role': 'cross_check'}
    native = native if isinstance(native, dict) else {}
    evidence = native.get('evidence') if isinstance(native.get('evidence'), dict) else {}
    values = native.get('values') if isinstance(native.get('values'), dict) else {}
    if (not isinstance(source_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', source_sha256)
            or geometry_source not in {'saved_native_evidence', 'cached_native_geometry'}):
        return output
    for field in ('specification', 'thickness_mm'):
        proof = evidence.get(field)
        if (not isinstance(proof, dict) or proof.get('basis') != 'native_geometry'
                or proof.get('unit') != 'mm' or proof.get('source_sha256') != source_sha256):
            continue
        if field == 'specification' and proof.get('status') == 'candidate':
            dimensions = _dimensions(proof.get('size_mm'))
            if dimensions:
                output.update(dimensions_mm=dimensions, dimensions_source=geometry_source)
        elif field == 'thickness_mm':
            status = proof.get('status')
            if status not in {'candidate', 'conflict', 'unresolved', 'not_applicable'}:
                continue
            measured = set()
            samples = proof.get('samples')
            for sample in samples[:32] if isinstance(samples, list) else []:
                if not isinstance(sample, dict):
                    continue
                for item in sample.get('face_evidence', [])[:16]:
                    if not isinstance(item, dict) or item.get('kind') not in {'parallel_faces', 'coaxial_cylinders'}:
                        continue
                    value = number(item.get('thickness_mm'))
                    if value is not None and 0 < value <= 10000 and isinstance(item.get('face_ids'), list) and len(item['face_ids']) == 2:
                        measured.add(value)
            output['measured_thickness_candidates_mm'] = [saved_number(value) for value in sorted(measured)[:16]]
            thickness = number(values.get('thickness_mm'))
            if status == 'candidate' and thickness is not None and 0 < thickness <= 10000:
                output.update(thickness_mm=saved_number(thickness), thickness_status=status,
                              thickness_source=geometry_source)
            elif status != 'candidate':
                output.update(thickness_status=status, thickness_source=geometry_source)
    return output


def with_technical_evidence(line, technical):
    """Whitelist geometry scalars from an already owner-bound manifest part.

    Material, density and volume always come from the original saved line; a
    manifest cannot replace those with pricing assumptions or arbitrary text.
    """
    output = weight_evidence(line)
    candidate = technical.get('weight_evidence') if isinstance(technical, dict) else None
    if not isinstance(candidate, dict):
        return output
    sources = {'saved_native_evidence', 'cached_native_geometry'}
    dimensions = _dimensions(candidate.get('dimensions_mm'))
    if dimensions and candidate.get('dimensions_source') in sources:
        output.update(dimensions_mm=dimensions, dimensions_source=candidate['dimensions_source'])
    status, source = candidate.get('thickness_status'), candidate.get('thickness_source')
    thickness = number(candidate.get('thickness_mm'))
    if source in sources and status in {'candidate', 'conflict', 'unresolved', 'not_applicable'}:
        observations = candidate.get('measured_thickness_candidates_mm')
        if isinstance(observations, list) and len(observations) <= 16:
            parsed = [number(value) for value in observations]
            if all(value is not None and 0 < value <= 10000 for value in parsed):
                output['measured_thickness_candidates_mm'] = [saved_number(value) for value in sorted(set(parsed))]
        if status == 'candidate' and thickness is not None and 0 < thickness <= 10000:
            output.update(thickness_mm=saved_number(thickness), thickness_status=status, thickness_source=source)
        elif status != 'candidate':
            output.update(thickness_status=status, thickness_source=source)
    return output


def result_weight_evidence(result, customer_parts=None):
    """Read-only evidence by original line ID, with no name-based matching."""
    sources = {row.get('id'): row.get('sha256') for row in result.get('source_files', []) if isinstance(row, dict)}
    native = result.get('engineering_native')
    native = native.get('lines', {}) if isinstance(native, dict) else {}
    native = native if isinstance(native, dict) else {}
    parts = {row.get('id'): row for row in customer_parts if isinstance(row, dict)} if isinstance(customer_parts, list) else {}
    output = {}
    for line in result.get('lines', []):
        if not isinstance(line, dict) or not isinstance(line.get('id'), str):
            continue
        identity = line['id']
        output[identity] = (with_technical_evidence(line, parts[identity]) if identity in parts
                            else weight_evidence(line, native.get(identity), source_sha256=sources.get(line.get('file_id'))))
    return output


def weight_projection(line):
    evidence = weight_evidence(line)
    return {**{field: saved_number(line.get(field)) for field in WEIGHT_FIELDS},
            'weight_role': 'cross_check', 'weight_assumptions': weight_assumptions(line),
            'native_volume_mm3': evidence['native_volume_mm3'], 'weight_evidence': evidence}


def weight_summary(result):
    values = result.get('lines') if isinstance(result, dict) else None
    lines = [line for line in values if isinstance(line, dict)] if isinstance(values, list) else []
    with localcontext() as context:
        context.prec = 80
        sets = [number(line.get('estimated_set_net_weight_kg')) for line in lines]
        totals = [number(line.get('estimated_total_net_weight_kg')) for line in lines]
        known_sets = [value for value in sets if value is not None]
        known_totals = [value for value in totals if value is not None]
        known_set = sum(known_sets, Decimal(0)) if known_sets else None
        known_total = sum(known_totals, Decimal(0)) if known_totals else None
        known_lines = sum(a is not None and b is not None for a, b in zip(sets, totals))
        complete = bool(lines) and known_lines == len(lines)
        rough = [index for index, line in enumerate(lines) if line.get('confidence') in {'low', 'proxy_material_estimate'}]
        rough_totals = [totals[index] for index in rough if totals[index] is not None]
        order_sets = (result.get('settings') or {}).get('order_sets') if isinstance(result, dict) else None
        return {'status': 'complete' if complete else 'partial' if known_sets or known_totals else 'unavailable',
                'complete': complete, 'unit': 'kg', 'role': 'cross_check', 'source': 'saved_line_weights',
                'includes_packaging': False, 'order_sets': order_sets if type(order_sets) is int and order_sets > 0 else None,
                'line_count': len(lines), 'known_weight_lines': known_lines,
                'missing_estimated_lines': len(lines)-known_lines,
                'estimated_set_net_weight_kg': saved_number(known_set) if complete else None,
                'estimated_total_net_weight_kg': saved_number(known_total) if complete else None,
                'known_set_net_weight_kg': saved_number(known_set), 'known_total_net_weight_kg': saved_number(known_total),
                'low_confidence_lines': len(rough),
                'low_confidence_total_net_weight_kg': saved_number(sum(rough_totals, Decimal(0))) if rough_totals else None}
