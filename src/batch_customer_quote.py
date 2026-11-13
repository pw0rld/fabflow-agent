"""Customer-facing quotation projections, without internal costs or margins.

All figures come from a saved result. This module does not price, inspect CAD,
read files, change approval status or infer missing engineering requirements.
"""
from __future__ import annotations

import csv
import copy
import html
import io
import re
from urllib.parse import urlencode

from batch_quote_costing import aggregate_display_groups, material_display, _display_count, _display_money, _esc, _preview_cell
from customer_weight_view import (display_weight, weight_projection, weight_assumptions, weight_summary,
                                  weight_evidence, result_weight_evidence)


STYLE = '''*{box-sizing:border-box}body{margin:0;background:#f2f5f7;color:#203d4e;font:14px/1.65 system-ui,-apple-system,"PingFang SC",sans-serif}main{max-width:1280px;margin:auto;padding:28px}header,.customer-summary,.customer-terms,.customer-items{background:#fff;border:1px solid #dce5eb;border-radius:13px;margin-bottom:20px;padding:26px}.customer-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}.customer-brand{font-size:12px;letter-spacing:2px;color:#6e8b9b}.customer-heading h1{font-size:28px;margin:6px 0}.customer-subtitle{color:#748a98;margin:0}.customer-badge{font-size:12px;border:1px solid #dfd2b9;border-radius:20px;color:#8a683f;background:#fff8eb;padding:5px 11px;white-space:nowrap}.customer-summary{display:flex;justify-content:space-between;align-items:center;gap:20px;border-left:4px solid #286579}.customer-total{color:#163e50;font-size:35px;line-height:1.3;display:block;letter-spacing:-1px;font-variant-numeric:tabular-nums}.customer-summary small{display:block;color:#78909d}.customer-actions a,.customer-actions button{display:inline-block;text-decoration:none;font:inherit;font-size:12px;padding:8px 14px;border:1px solid #c8dbe4;border-radius:7px;background:#fff;color:#23617b;cursor:pointer}.customer-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:14px}.customer-scope{font-size:12px;color:#976f37;margin:8px 0 0}.customer-section-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}.customer-section-heading h2,.customer-terms h2{font-size:17px;margin:0}.customer-section-heading span{color:#7a909d;font-size:12px}.customer-table-wrap{overflow:auto}.customer-table{border-collapse:collapse;width:100%;font-size:12px}.customer-table th{color:#638090;background:#f1f6f9;font-weight:500}.customer-table th,.customer-table td{padding:12px 10px;border-bottom:1px solid #e1e9ee;text-align:left;vertical-align:middle;overflow-wrap:anywhere}.customer-table th.numeric,.customer-table td.numeric{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}.customer-table .customer-amount{color:#20536b;font-weight:650}.customer-table .customer-product{min-width:145px}.customer-table .customer-spec{min-width:190px;max-width:330px;color:#728a98;font-size:11px}.customer-table small{display:block;color:#879ba6;font-size:10px}.customer-preview a{color:#39788e;text-decoration:none;font-size:10px;display:flex;align-items:center;flex-direction:column;gap:4px}.customer-preview img{width:82px;height:64px;object-fit:contain;border:1px solid #dce7ed;border-radius:6px}.customer-preview .part-placeholder{display:none}.customer-terms dl{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:20px;margin:16px 0 0}.customer-terms dt{font-size:11px;color:#8195a1;margin-bottom:6px}.customer-terms dd{margin:0;font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere}.customer-footer{text-align:center;color:#93a4ae;font-size:11px;padding:6px}.qps-customer-spec{font-size:11px!important;color:#748b99!important;margin-top:7px!important;overflow-wrap:anywhere}.qps-items{align-items:start}.customer-print .customer-preview img{width:57px;height:43px}.customer-print .customer-table th,.customer-print .customer-table td{padding:7px 8px}.customer-print .customer-total{font-size:27px}.customer-print header,.customer-print .customer-summary,.customer-print .customer-terms,.customer-print .customer-items{padding:17px}.customer-print main{max-width:1200px}@media(max-width:640px){main{padding:12px}.customer-heading{flex-direction:column}.customer-heading h1{font-size:22px}.customer-summary{align-items:flex-start;flex-direction:column;padding:20px}.customer-total{font-size:30px}.customer-terms dl{grid-template-columns:repeat(2,minmax(0,1fr))}.customer-items{padding:15px}.customer-table{min-width:720px}}@media print{@page{size:A4 landscape;margin:12mm}body{background:white;font-size:10px}main,.customer-print main{max-width:none;padding:0}header,.customer-summary,.customer-terms,.customer-items,.customer-print header,.customer-print .customer-summary,.customer-print .customer-terms,.customer-print .customer-items{border:0;box-shadow:none;border-radius:0;padding:8px 0;margin:0 0 10px}.customer-actions,.customer-footer{display:none}.customer-heading h1{font-size:20px}.customer-total,.customer-print .customer-total{font-size:24px}.customer-summary{border-bottom:1px solid #b8cbd6}.customer-table-wrap{overflow:visible}.customer-table{font-size:9px;min-width:0!important}.customer-table thead{display:table-header-group}.customer-table tr{break-inside:avoid}.customer-table th,.customer-table td,.customer-print .customer-table th,.customer-print .customer-table td{padding:5px 6px}.customer-table .customer-spec{min-width:0;max-width:230px}.customer-table .customer-product{min-width:100px}.customer-terms{break-inside:avoid}.customer-preview img{width:48px!important;height:37px!important}.customer-badge{font-size:10px}.customer-subtitle{font-size:11px}}'''


def customer_part_projection(payload):
    """Allow only native display geometry and technical line facts, never costs."""
    if not isinstance(payload, dict):
        raise ValueError('零件预览数据无效。')
    allowed = ('batch_id', 'run_id', 'line_id', 'part_name', 'source_sha256', 'representative_solid_id',
               'representative_solid_ids', 'selected_id', 'order_sets', 'up_axis', 'source_document',
               'shared_geometry_url', 'solid_ids', 'bounds_mm')
    out = {key: copy.deepcopy(payload[key]) for key in allowed if key in payload}
    for key in ('groups', 'occurrences'):
        if key in payload:
            out[key] = [{field: copy.deepcopy(row[field]) for field in ('id', 'name', 'solid_ids') if field in row}
                        for row in payload[key] if isinstance(row, dict)]
    if isinstance(payload.get('line_snapshot'), dict):
        fields = ('id', 'part_name', 'document', 'mode', 'material_raw', 'material_profile', 'material_estimation_label',
                  'quantity_per_set', 'quantity_total', 'estimated_net_weight_kg', 'estimated_total_net_weight_kg', 'confidence')
        out['line_snapshot'] = {key: copy.deepcopy(payload['line_snapshot'][key]) for key in fields if key in payload['line_snapshot']}
        out['line_snapshot'].update(weight_projection(payload['line_snapshot']))
        out['line_snapshot']['material_raw'] = out['line_snapshot']['weight_assumptions']['cad_material_text']
    geometry = payload.get('geometry')
    if isinstance(geometry, dict):
        safe = {key: copy.deepcopy(geometry[key]) for key in ('source_sha256', 'output_unit') if key in geometry}
        safe['solids'] = []
        for row in geometry.get('solids', []):
            if not isinstance(row, dict):
                continue
            solid = {key: copy.deepcopy(row[key]) for key in ('id', 'name', 'bounds_mm') if key in row}
            mesh = row.get('mesh')
            if isinstance(mesh, dict):
                solid['mesh'] = {key: copy.deepcopy(mesh[key]) for key in ('positions', 'indices', 'normals') if key in mesh}
            safe['solids'].append(solid)
        out['geometry'] = safe
    return out


def customer_viewer_url(url):
    from urllib.parse import parse_qs, urlencode, urlsplit
    parsed = urlsplit(url)
    values = parse_qs(parsed.query)
    if parsed.scheme or parsed.netloc or parsed.path != '/workbench/quotation/part/' or len(values.get('preview', [])) != 1:
        raise ValueError('零件链接无效。')
    source = values['preview'][0]
    if '/parts/' not in source:
        raise ValueError('零件来源链接无效。')
    return parsed.path + '?' + urlencode({'preview': source.replace('/parts/', '/customer-parts/', 1), 'customer': '1'})


def _terms(result):
    sheet = result.get('cost_sheet')
    value = sheet.get('customer_terms') if isinstance(sheet, dict) else result.get('customer_terms')
    value = value if isinstance(value, dict) else {}
    return {key: value.get(key) if isinstance(value.get(key), str) and value[key].strip() else default
            for key, default in (('delivery', '另行约定'), ('payment', '另行约定'), ('validity', '另行约定'),
                                 ('tax_shipping', '人民币 · 未税 · 不含运费'))}


def _specifications(result, groups, review, recognition):
    if review is None and recognition is None:
        return {}
    from batch_engineering_bom import build_engineering_bom
    manifest = build_engineering_bom(result, groups, review=review, recognition=recognition)
    engineering = {row['id']: row for row in manifest['lines']}
    out = {}
    for group in groups:
        members = [engineering[identity] for identity in group['member_ids'] if identity in engineering]
        fields = []
        for field, prefix in (('specification', ''), ('thickness_mm', '板/壁厚 '), ('surface_treatment', '表面 ')):
            values = list(dict.fromkeys(str(row[field]) for row in members if row.get(field) is not None))
            if len(values) == 1:
                fields.append(prefix + values[0] + (' mm' if field == 'thickness_mm' else ''))
            elif len(values) > 1:
                fields.append(prefix + '按各零件图要求')
        out[group['id']] = '；'.join(fields)
    return out


CUSTOMER_PART_STYLE = '''.customer-preview{min-width:144px;width:144px}.customer-preview .customer-part-image{display:block}.customer-preview img[data-customer-thumbnail]{width:122px;height:92px;object-fit:contain;background:#f7fafb}.customer-part-unavailable{display:grid;place-items:center;min-height:84px;padding:12px;border:1px dashed #dce7ed;border-radius:7px;color:#94a6b0;font-size:11px}.customer-preview .customer-part-actions{display:flex;gap:5px;flex-wrap:wrap;margin-top:9px}.customer-preview .customer-part-actions>a{display:inline-flex;padding:4px 7px;border:1px solid #d5e4eb;border-radius:5px;font-size:10px;background:#fff}.customer-part-more{font-size:10px;color:#7e96a3}.customer-part-more summary{cursor:pointer}.customer-part-more a{display:block!important;margin-top:5px}.customer-print .customer-part-actions{display:none!important}.customer-print .customer-preview{width:86px;min-width:86px}.customer-print img[data-customer-thumbnail]{width:76px;height:58px}.customer-print .customer-part-unavailable{min-height:48px;font-size:9px}@media print{.customer-part-actions{display:none!important}.customer-preview{width:68px;min-width:68px}.customer-preview img[data-customer-thumbnail]{width:62px!important;height:47px!important}.customer-part-image{pointer-events:none}.customer-part-unavailable{min-height:40px;font-size:8px}}'''
CUSTOMER_WEIGHT_STYLE = '''.customer-weights{border:1px solid #dce5eb;border-radius:13px;background:#fff;padding:20px 26px;margin-bottom:20px}.customer-weight-cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}.customer-weight-cards small{color:#78909d}.customer-weight-cards strong{display:block;font-size:23px;font-variant-numeric:tabular-nums;color:#254e65}.customer-weight-state{color:#937241;font-size:11px;margin-left:8px}.customer-weight-note{font-size:11px;color:#8195a1;margin:12px 0 0}.customer-weight-cell{min-width:94px}.customer-weight-source{font-size:10px;color:#879ba6}.customer-weight-basis{margin-top:5px;font-size:10px}.customer-weight-basis summary{cursor:pointer;color:#527d91}.customer-weight-basis div{white-space:normal;max-width:240px}.customer-weight-density{display:block;font-size:10px;color:#8195a1}@media(max-width:640px){.customer-weights{padding:16px}.customer-weight-cards{gap:12px}.customer-weight-cards strong{font-size:19px}}@media print{.customer-weights{border:0;border-bottom:1px solid #dce5eb;border-radius:0;padding:8px 0;margin:0 0 12px;break-inside:avoid}.customer-weight-cards strong{font-size:16px}.customer-weight-note{margin-top:6px;font-size:9px}.customer-weight-cell{min-width:65px}.customer-weight-basis{display:none}.customer-weight-density,.customer-weight-source{font-size:8px}}'''
CUSTOMER_WEIGHT_STYLE += '''.customer-weight-evidence{margin-top:9px;padding-top:7px;border-top:1px solid #e5edf1;font-size:10px;line-height:1.65;color:#6c8594}.customer-weight-evidence h3{font-size:10px;font-weight:600;color:#466c80;margin:0 0 3px}.customer-weight-evidence p{margin:0;white-space:normal}.customer-weight-evidence .evidence-label{color:#879ba6}.customer-weight-evidence [data-weight-evidence=source]{color:#8a714c;font-size:10px}@media print{.customer-weight-evidence{display:block!important;font-size:8px;margin-top:4px;padding-top:4px}.customer-weight-evidence h3,.customer-weight-evidence [data-weight-evidence=source]{font-size:8px}}'''


def _weight_summary_html(result):
    summary = weight_summary(result)
    complete = summary['complete']
    cards = []
    for scope, label, field in (('set', '每套预估净重', 'set'), ('order', '整单预估净重', 'total')):
        value = summary[('estimated_' if complete else 'known_') + field + '_net_weight_kg']
        if not complete and value is not None:
            label = '每套已知重量小计' if scope == 'set' else '整单已知重量小计'
        state = '' if complete else '已知小计' if value is not None else '尚未估算'
        cards.append('<div data-weight-scope="' + scope + '" data-weight-value-kg="' + _esc(value, '')
            + '"><small>' + label + ('<span class="customer-weight-state">' + state + '</span>' if state else '')
            + '</small><strong>' + _esc(display_weight(value)) + '</strong></div>')
    coverage = (' · ' + str(summary['missing_estimated_lines']) + ' 项重量缺失' if summary['missing_estimated_lines'] else '')
    rough = (' · ' + str(summary['low_confidence_lines']) + ' 项粗估' if summary['low_confidence_lines'] else '')
    return ('<section class="customer-weights" data-customer-weight-summary data-weight-status="' + summary['status']
        + '"><div class="customer-weight-cards">' + ''.join(cards) + '</div>'
        + '<p class="customer-weight-note">按CAD体积与所用材料密度估算，不含包装' + coverage + rough
        + '；用于工艺核价交叉核对。</p></section>')


def _group_weight_evidence(group, evidence):
    """Grouped purchased parts keep one matching unit's geometric facts."""
    output = weight_evidence(group)
    members = [evidence.get(identity) for identity in group.get('member_ids', [])]
    if not members or not all(isinstance(row, dict) for row in members):
        return output
    for fields in (('dimensions_mm', 'dimensions_source'), ('thickness_mm', 'thickness_status', 'thickness_source', 'measured_thickness_candidates_mm')):
        if all(all(row.get(field) == members[0].get(field) for field in fields) for row in members):
            output.update({field: members[0][field] for field in fields})
    return output


def _weight_basis_html(group, evidence=None):
    basis = evidence if isinstance(evidence, dict) else weight_evidence(group)
    volume = basis['native_volume_mm3']
    volume_label = '单件原生体积' if basis['volume_status'] == 'native' else '单件体积依据'
    volume_text = _display_money(volume) + ' mm³' if volume is not None else '未记录'
    if basis['volume_status'] in {'low_confidence', 'proxy'}:
        volume_text += ' · ' + ('异常几何粗估' if basis['volume_status'] == 'low_confidence' else '代理模型')
    dimensions = basis['dimensions_mm']
    dimensions_text = (' × '.join(_display_money(value) for value in dimensions) + ' mm · 非展开'
                       if dimensions else '尚无原生量测依据')
    thickness = basis['thickness_mm']
    thickness_text = (_display_money(thickness) + ' mm · CAD量测候选' if thickness is not None else
                      {'conflict': '量测候选不一致', 'not_applicable': '不适用'}.get(basis['thickness_status'], '尚无量测候选'))
    observations = basis.get('measured_thickness_candidates_mm', [])
    if thickness is None and observations:
        thickness_text = ' / '.join(_display_money(value) for value in observations) + ' mm · 局部量测候选，未采用为统一板厚'
    density = basis['density_kg_m3']
    density_text = _display_money(density) + ' kg/m³' if density is not None else '未记录'
    rows = [('volume', volume_label, volume_text), ('dimensions', '成品包络', dimensions_text),
            ('thickness', '板/壁厚', thickness_text),
            ('material', '估重采用材料', basis['adopted_material_label'] or '未记录'),
            ('density', '采用密度', density_text), ('source', '材料依据', basis['material_description'])]
    if basis['cad_material_text']:
        rows.append(('cad-material', 'CAD材料原文', basis['cad_material_text']))
    hint = basis.get('material_requirement_hint')
    if isinstance(hint, dict):
        rows.append(('requirement', '资料材料线索', hint['label']))
        if hint['conflicts_with_adopted_material']:
            rows.append(('material-conflict', '采用条件待核对', '文件名标注不锈钢，与当前估重采用材料不一致。'))
    return ('<section class="customer-weight-evidence" data-customer-weight-evidence data-line-id="' + _esc(group['id'])
            + '"><h3>重量依据 · 单件</h3>' + ''.join('<p data-weight-evidence="' + key
            + '"><span class="evidence-label">' + _esc(label) + '：</span>' + _esc(value) + '</p>'
            for key, label, value in rows) + '</section>')


def _customer_part_assets(result, parts, *, technical_source='customer'):
    """Only a fixed owner/staff namespace may enter this document projection."""
    batch_id, run_id = result.get('batch_id'), result.get('run_id')
    if (not isinstance(batch_id, str) or not re.fullmatch(r'BATCH-[a-f0-9]{32}', batch_id)
            or not isinstance(run_id, str) or not re.fullmatch(r'RUN-[a-f0-9]{32}', run_id)
            or not isinstance(parts, list)):
        return {}
    line_ids = {line.get('id') for line in result.get('lines', []) if isinstance(line, dict)}
    base = ('/ortho2cad-test/customer-upload-admin/api/quote-batches/' if technical_source == 'staff'
            else '/workbench/customer/api/orders/')
    prefix = base + batch_id + '/runs/' + run_id + '/'
    assets = {}
    for part in parts:
        if not isinstance(part, dict):
            continue
        line_id = part.get('id')
        if not isinstance(line_id, str) or not re.fullmatch(r'L[0-9]{4,7}', line_id) or line_id not in line_ids:
            continue
        geometry = prefix + 'customer-parts/' + line_id
        laser = part.get('laser') if isinstance(part.get('laser'), dict) else {}
        drawing = prefix + 'manufacturing/' + line_id + '/'
        query = urlencode({'order': batch_id, 'run': run_id, 'part': line_id})
        process = ('/workbench/?' + urlencode({'workspace': 'orders', 'scope': 'customer', 'id': batch_id})
                   if technical_source == 'staff' else '/workbench/customer/part/?' + query)
        asset = {'id': line_id, 'thumbnail_url': None, 'viewer_url': None,
                 'process_url': process, 'svg_url': None, 'dxf_url': None}
        if part.get('geometry_url') == geometry:
            asset['viewer_url'] = '/workbench/quotation/part/?' + urlencode({'customer': '1', 'preview': geometry})
        if part.get('thumbnail_url') == geometry + '/thumbnail':
            asset['thumbnail_url'] = geometry + '/thumbnail'
        if laser.get('status') == 'ready':
            for key, filename in (('svg_url', 'laser.svg'), ('dxf_url', 'laser.dxf')):
                if laser.get(key) == drawing + filename:
                    asset[key] = drawing + filename
        assets[line_id] = asset
    return assets


def _customer_part_cell(group, assets, *, printable=False):
    members = [assets[identity] for identity in group.get('member_ids', []) if identity in assets]
    # A missing source stays visible instead of implying that every part has a
    # preview or manufacturing drawing. No geometry is reconstructed here.
    selected = next((part for part in members if part['thumbnail_url']), members[0] if members else None)
    if selected is None:
        return '<td class="customer-preview"><span class="customer-part-unavailable">暂无原生预览</span></td>'
    identity = _esc(selected['id'])
    if selected['thumbnail_url']:
        preview = ('<a class="customer-part-image" href="' + _esc(selected['viewer_url'] or selected['process_url'])
                   + '" target="_blank" rel="noopener noreferrer"><img data-customer-thumbnail data-customer-part-id="'
                   + identity + '" src="' + _esc(selected['thumbnail_url']) + '" alt="' + _esc(group.get('part_name'))
                   + ' · 原生 CAD 零件" loading="' + ('eager' if printable else 'lazy') + '"></a>')
    else:
        preview = '<span class="customer-part-unavailable">暂无原生预览</span>'
    links = []
    for key, label, marker in (('viewer_url', '查看单件 3D', '3d'), ('process_url', '查看工艺', 'process'),
                                ('svg_url', '切割图', 'svg'), ('dxf_url', '下载 DXF', 'dxf')):
        if selected[key]:
            links.append('<a data-customer-' + marker + '-link data-customer-part-id="' + identity + '" href="'
                         + _esc(selected[key]) + '" target="_blank" rel="noopener noreferrer"'
                         + (' download' if key == 'dxf_url' else '') + '>' + label + '</a>')
    if len(members) > 1:
        sources = ''.join('<a href="' + _esc(part['process_url']) + '" target="_blank" rel="noopener noreferrer">'
                          + _esc(part['id']) + ' · 查看来源零件</a>' for part in members)
        links.append('<details class="customer-part-more"><summary>' + str(len(members)) + ' 条来源</summary>' + sources + '</details>')
    return ('<td class="customer-preview" data-customer-part-id="' + identity + '">' + preview
            + '<div class="customer-part-actions">' + ''.join(links) + '</div></td>')


def render_customer_quote(result, *, part_previews=None, engineering_review=None, engineering_recognition=None,
                          printable=False, gallery=False, thumbnail_urls=None, customer_parts=None, technical_source='customer'):
    """Return only commercial quantities/amounts and saved technical descriptions."""
    if technical_source not in {'customer', 'staff'}:
        raise ValueError('报价技术来源无效。')
    groups = aggregate_display_groups(result)
    weight_facts = result_weight_evidence(result, customer_parts)
    specifications = _specifications(result, groups, engineering_review, engineering_recognition)
    lines = part_previews.get('lines', []) if isinstance(part_previews, dict) else part_previews or []
    previews = {row['line_id']: row for row in lines if isinstance(row, dict) and isinstance(row.get('line_id'), str)}
    customer_assets = _customer_part_assets(result, customer_parts, technical_source=technical_source) if customer_parts is not None else None
    has_previews = bool(previews) or customer_assets is not None
    total = result.get('totals', {}).get('total_price')
    manufacturing = result.get('manufacturing') or {}
    process_mode = manufacturing.get('version') == 'process-v1'
    estimate = result.get('process_estimate')
    estimate = estimate if isinstance(estimate, dict) else {}
    route = estimate.get('route_assumption')
    route_id = route.get('id') if isinstance(route, dict) else manufacturing.get('route_assumption')
    estimate_scope = ''
    if process_mode and (manufacturing.get('estimate_version') or estimate.get('version')):
        label = ('工程假设：钣金成形＋拼焊；拟定范围估算，待工程审核'
                 if route_id == 'sheet_form_weld_demo' else '拟定范围估算，待工程审核')
        estimate_scope = '<p class="customer-scope" data-engineering-estimate-scope>' + label + '</p>'
    partial_process = process_mode and total is None
    known_subtotal = manufacturing.get('estimated_price_subtotal') if partial_process else None
    by_id = {line['id']: line for line in result.get('lines', [])}
    unpriced = sum(group.get('priced') is not True for group in groups)
    if process_mode and unpriced:
        partial_process = True
        known_subtotal = total if isinstance(result.get('cost_sheet'), dict) and total is not None else manufacturing.get('estimated_price_subtotal')
        total = None
    terms = _terms(result)
    rows = []
    for index, group in enumerate(groups, 1):
        material = material_display(group)
        weights = weight_projection(group)
        process_description = ''
        if process_mode:
            signatures = []
            for identity in group.get('member_ids', []):
                plan = by_id.get(identity, {}).get('manufacturing') or {}
                signature = []
                for process in plan.get('processes', []):
                    label = process.get('label') or process.get('kind') or '工序'
                    quantity, unit = process.get('quantity_per_piece'), process.get('unit')
                    signature.append(label + (' ' + _display_money(quantity) + ' ' + str(unit) + '/件' if quantity is not None else ''))
                if plan.get('blockers'):
                    signature.append('工艺范围待工程核对')
                signatures.append('；'.join(signature))
            texts = list(dict.fromkeys(signatures))
            detail = texts[0] if len(texts) == 1 else '按各零件工艺清单'
            process_description = '<small class="customer-process-summary">' + _esc(detail) + '</small>' if detail else ''
        prices = ('¥' + _display_money(group['unit_price']), '¥' + _display_money(group['total_price'])) if group.get('priced') else ('待核价', '待核价')
        preview = (_customer_part_cell(group, customer_assets, printable=printable) if customer_assets is not None
                   else _preview_cell(group, previews) if previews else '')
        if preview and customer_assets is None:
            preview = re.sub(r'href="([^"]+)"', lambda match: 'href="' + _esc(customer_viewer_url(html.unescape(match.group(1)))) + '"', preview, count=1)
            preview = preview.replace('<td class="part-preview-cell">', '<td class="customer-preview">', 1).replace('loading="lazy"', 'loading="eager"' if printable else 'loading="lazy"')
        rows.append('<tr data-print-group="' + _esc(group['id']) + '" data-member-count="' + str(group['member_count']) + '"><td>' + str(index).zfill(3) + '</td>'
                    + preview + '<td class="customer-product"><strong>' + _esc(group['part_name']) + '</strong>' + process_description + '</td><td class="customer-spec">'
                    + _esc(material['adopted_text']) + '<small>报价采用</small>' + (_esc(specifications[group['id']]) if specifications.get(group['id']) else '')
                    + _weight_basis_html(group, _group_weight_evidence(group, weight_facts))
                    + '</td><td class="numeric">' + _display_count(group.get('quantity_total'), '待核对') + '</td>'
                    + '<td class="numeric customer-weight-cell" data-customer-unit-weight data-line-id="' + _esc(group['id'])
                    + '">' + _esc(display_weight(weights['estimated_net_weight_kg'])) + '<small class="customer-weight-source">'
                    + _esc(weights['weight_assumptions']['description']) + '</small></td>'
                    + '<td class="numeric customer-weight-cell" data-customer-order-weight data-line-id="' + _esc(group['id'])
                    + '">' + _esc(display_weight(weights['estimated_total_net_weight_kg'])) + '</td><td class="numeric">'
                    + prices[0] + '</td><td class="numeric customer-amount">' + prices[1] + '</td></tr>')
    if gallery and not printable and customer_assets is None:
        from batch_parts_showcase import render_parts_showcase
        items = render_parts_showcase(result, part_previews, thumbnail_urls=thumbnail_urls, customer=True, specifications=specifications)
        assets = '<link rel="stylesheet" href="quote-parts-showcase.css?v=20261109-cost-sheet"><script defer src="quote-parts-showcase.js?v=20261109-cost-sheet"></script>'
    else:
        items = ('<section class="customer-items"><div class="customer-section-heading"><h2>报价明细</h2><span>' + str(len(groups)) + ' 项物料</span></div>'
                 + '<div class="customer-table-wrap"><table class="customer-table"><thead><tr><th>序号</th>' + ('<th>拆件预览</th>' if customer_assets is not None else '<th>零件</th>' if has_previews else '')
                 + '<th>名称 / 型号</th><th>材料 / 规格</th><th class="numeric">数量（件）</th><th class="numeric">单件预估净重</th>'
                 + '<th class="numeric">订单预估净重</th><th class="numeric">单价（元）</th><th class="numeric">金额（元）</th></tr></thead><tbody>'
                 + ''.join(rows) + '</tbody></table></div></section>')
        assets = ''
    term_rows = ''.join('<div><dt>' + label + '</dt><dd>' + _esc(terms[key]) + '</dd></div>' for key, label in
                        (('delivery', '交期'), ('payment', '付款方式'), ('validity', '报价有效期'), ('tax_shipping', '税费与运输')))
    title = result.get('name') or '产品报价'
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            + '<title>' + _esc(title) + ' · 报价单</title><style>' + STYLE + (CUSTOMER_PART_STYLE if customer_assets is not None else '') + CUSTOMER_WEIGHT_STYLE + '</style>' + assets + '</head><body class="'
            + ('customer-print' if printable else 'customer-online') + '"><main data-customer-quote><header><div class="customer-heading"><div><span class="customer-brand">好运来 · 产品报价</span>'
            + '<h1>' + _esc(title) + '</h1><p class="customer-subtitle">' + _display_count(result.get('settings', {}).get('order_sets')) + ' 套 · '
            + str(len(groups)) + ' 项物料</p></div><span class="customer-badge">模拟报价 · 待审核</span></div><nav class="customer-actions">'
            + ('<button type="button" onclick="window.print()">打印报价单</button>' if printable else '<a href="customer-print.html" target="_blank" rel="noopener noreferrer">打开打印版</a>')
            + '</nav></header><section class="customer-summary"><div><small>' + ('工艺报价状态' if partial_process else '已计价部分报价' if unpriced else '报价总额') + '</small><strong class="customer-total" data-total-price="'
            + _esc(total, '') + '">' + ('¥' + _display_money(total) if total is not None else '待工程核价' if partial_process else '待核价') + '</strong>'
            + estimate_scope
            + ('<p class="customer-scope">已识别项目参考小计：¥' + _display_money(known_subtotal) + '。工艺范围未完整，不能作为整单报价。</p>' if partial_process and known_subtotal is not None else '')
            + ('<p class="customer-scope">' + str(unpriced) + ' 项工艺或采购条件待核定；详见零件工艺清单。</p>' if partial_process and unpriced else '<p class="customer-scope">另有 ' + str(unpriced) + ' 项待核价，未包含在上述金额中。</p>' if unpriced else '')
            + '</div><div><small>报价口径</small>' + _esc(terms['tax_shipping']) + '</div></section>' + _weight_summary_html(result) + items
            + '<section class="customer-terms"><h2>交付与商务条件</h2><dl>' + term_rows + '</dl></section>'
            + '<footer class="customer-footer">好运来 · 报价草稿，审核后生效</footer></main></body></html>').encode('utf-8')


def render_cost_sheet_csv(result):
    """Full internal entry export, distinct from the customer quotation."""
    from batch_cost_sheet import build_cost_sheet, MANAGEMENT_ID
    sheet = build_cost_sheet(result)
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    labels = {'material': '材料', 'purchase': '辅料与外购', 'process': '加工', 'setup': '准备', 'tooling': '模具', 'other': '其他费用'}
    writer.writerow(['费用编号', '行号/范围', '类别', '项目', '方法', '金额_元', '工程量', '单位', '费率', '净料_kg', '损耗_kg', '材料单价_元每kg', '废料回收单价_元每kg', '工具编号', '模具总费_元', '分摊数量', '来源口径', '说明'])
    for entry in sheet['entries']:
        values = [entry.get('id'), entry.get('line_id') or '整单', labels.get(entry.get('category'), entry.get('category')), entry.get('label'), entry.get('method'),
                  sheet['entry_amounts'].get(entry['id']), entry.get('quantity'), entry.get('unit'), entry.get('rate'), entry.get('net_kg'), entry.get('loss_kg'), entry.get('unit_price'),
                  entry.get('scrap_price'), entry.get('tooling_id'), entry.get('tooling_amount'), entry.get('amortization_quantity'), entry.get('source_kind'), entry.get('note')]
        writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value for value in values])
    pricing = (result.get('cost_sheet') or {}).get('pricing')
    if pricing is not None:
        accounting = sheet['accounting_summary']
        explanation = ('直接成本基数 ' + str(accounting['direct_cost']) + ' 元 × 管理费率 '
            + pricing['management_rate_percent'] + '%；管理费取整规则 ' + pricing['rounding']
            + ('（整元四舍五入）' if pricing['rounding'] == 'whole_yuan' else '（到分四舍五入）')
            + '；整单一次计取并分摊至其他费用。')
        values = [MANAGEMENT_ID, '整单', '其他', '管理费', 'derived_percentage',
                  accounting['management_cost'], accounting['direct_cost'], '元', pricing['management_rate_percent'] + '%',
                  None, None, None, None, None, None, None, 'explicit_pricing_derived', explanation]
        writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value for value in values])
    return ('\ufeff' + output.getvalue()).encode('utf-8')
