"""Read-only visual presentation of saved quotation groups and native previews.

This module formats existing values. It never prices, estimates, classifies,
reads CAD, mutates records or replaces quotation source associations.
The caller publishes the returned section and the two adjacent web assets.
"""
from __future__ import annotations

import html
import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from batch_quote_costing import (aggregate_display_groups, material_display,
                                 _display_count, _display_money, _esc, _kg,
                                 _preview_cell)

SHOWCASE_CSS = 'web/quote-parts-showcase.css'
SHOWCASE_JS = 'web/quote-parts-showcase.js'

_ICONS = {
    'search': '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
    'list': '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    'grid': '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
    'box': '<path d="m12 3 9 5v8l-9 5-9-5V8l9-5Zm0 10v8M3 8l9 5 9-5M7.5 5.5l9 5"/>',
    'arrow': '<path d="M7 17 17 7M7 7h10v10"/>',
    'close': '<path d="m6 6 12 12M18 6 6 18"/>',
}


def _icon(name):
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + _ICONS[name] + '</svg>')


def _text(value):
    return value if isinstance(value, str) else ''


def _thumbnail_override(original, candidate, line_id):
    """Allow only this line's locally derived showcase PNG, never a remote URL."""
    if not isinstance(original, str) or not isinstance(candidate, str) or '\\' in candidate:
        return None
    old, new = urlsplit(original), urlsplit(candidate)
    if (old.scheme or old.netloc or old.query or old.fragment or new.scheme or new.netloc
            or new.query or new.fragment or not old.path.endswith('/thumbnail.png')):
        return None
    relative = 'showcase-thumbs/' + line_id + '.png'
    case_root = old.path.split('/parts/', 1)[0] + '/'
    return candidate if candidate in {relative, case_root + relative} else None


def _preview_link(line, previews, thumbnail_urls, customer=False):
    # Keep the existing URL/line binding validation and anchor fallback exactly.
    cell = _preview_cell(line, previews)
    content = cell[cell.find('>') + 1:cell.rfind('</td>')]
    if 'class="part-preview-link"' not in content:
        return '<div class="qps-preview-unavailable">' + _icon('box') + '<span>暂无原生预览</span></div>'
    source = previews[line['id']]['geometry_url']
    match = re.search(r'href="([^"]+)"', content)
    view = html.unescape(match.group(1)) if match else ''
    if customer and view:
        from batch_customer_quote import customer_viewer_url
        next_view = customer_viewer_url(view)
        content = content.replace('href="' + _esc(view) + '"', 'href="' + _esc(next_view) + '"', 1)
        view = next_view
        source = source.replace('/parts/', '/customer-parts/', 1)
    content = content.replace('class="part-preview-link"',
        'class="part-preview-link" data-part-preview="' + _esc(source)
        + '" data-view-url="' + _esc(view) + '" data-preview-line="' + _esc(line['id']) + '"', 1)
    original_thumbnail = previews[line['id']].get('thumbnail_url')
    replacement = _thumbnail_override(original_thumbnail, thumbnail_urls.get(line['id']), line['id'])
    if replacement:
        content = content.replace('src="' + _esc(original_thumbnail) + '"',
                                  'src="' + _esc(replacement) + '"', 1)
    return content


def _cash(value, *, priced=True, unknown=None):
    missing = unknown or ('未提供' if priced else '未计价')
    formatted = _display_money(value, missing)
    return formatted if formatted == _esc(missing) else '¥' + formatted


def _basis(line):
    material = material_display(line)
    cad = ('<div><dt>CAD 材料原文</dt><dd>' + _esc(material['cad_text']) + '</dd></div>') if material['cad_text'] else ''
    notes = line.get('notes')
    notes = '；'.join(note for note in notes if isinstance(note, str)) if isinstance(notes, list) else _text(notes)
    return ('<div class="qps-basis-content"><dl class="material-basis">' + cad
            + '<div><dt>' + _esc(material['adopted_label']) + '</dt><dd>' + _esc(material['adopted_text']) + '</dd></div>'
            + '<div><dt>价格来源</dt><dd>' + _esc(material['price_text']) + '</dd></div></dl>'
            + '<p>' + _esc(material['note']) + '</p>'
            + ('<p>' + _esc(line.get('weight_basis')) + '</p>' if line.get('weight_basis') else '')
            + ('<p>' + _esc(notes) + '</p>' if notes else '') + '</div>')


def _source_details(group, originals, previews, thumbnail_urls):
    if not group['aggregated']:
        return ''
    rows = []
    for member_id in group['member_ids']:
        line = originals[member_id]
        rows.append('<tr data-source-line="' + _esc(member_id) + '"><td class="qps-source-preview">'
                    + _preview_link(line, previews, thumbnail_urls) + '</td><td><strong>' + _esc(line['document'])
                    + '</strong><small>' + _esc(member_id) + '</small></td><td>' + _display_count(line.get('quantity_per_set'))
                    + '</td><td>' + _display_count(line.get('quantity_total')) + '</td><td>'
                    + _kg(line.get('estimated_total_net_weight_kg')) + '</td><td data-source-raw-amount="'
                    + _esc(line.get('total_price'), '') + '">' + _cash(line.get('total_price'), priced=line.get('priced') is True) + '</td></tr>')
    return ('<details class="group-sources qps-details"><summary>' + str(group['member_count'])
            + ' 条来源明细<span aria-hidden="true">＋</span></summary><div class="qps-source-content">'
            '<p>汇总展示，原始来源、数量与金额逐条保留。</p><div class="qps-source-scroll"><table><thead><tr>'
            '<th>原件</th><th>来源文件 / 行号</th><th>每套用量</th><th>订单用量</th><th>订单估重</th><th>原行金额</th>'
            '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div></div></details>')


def render_parts_showcase(result, part_previews, *, thumbnail_urls=None, customer=False, specifications=None):
    """Return a standalone section with values from the saved quotation.

    ``part_previews`` accepts the existing manifest or its ``lines`` list.
    Optional ``thumbnail_urls`` replaces display PNGs only, retaining original
    geometry URLs, sources and native part-view links. The caller includes the
    CSS/JS assets; no network or file access occurs in this renderer.
    """
    if not isinstance(result, dict) or not isinstance(result.get('lines'), list):
        raise ValueError('Saved quotation lines are required for the parts showcase.')
    rows = part_previews.get('lines', []) if isinstance(part_previews, dict) else part_previews
    previews = {row['line_id']: row for row in (rows or [])
                if isinstance(row, dict) and isinstance(row.get('line_id'), str)}
    thumbnail_urls = thumbnail_urls if isinstance(thumbnail_urls, Mapping) else {}
    specifications = specifications if isinstance(specifications, Mapping) else {}
    originals = {line['id']: line for line in result['lines']}
    groups = aggregate_display_groups(result)
    counts = {'all': len(groups), 'make': sum(row.get('mode') == 'make' for row in groups),
              'buy': sum(row.get('mode') == 'buy' for row in groups),
              'unpriced': sum(row.get('priced') is not True for row in groups)}
    settings = result.get('settings') or {}
    identity = re.sub(r'[^A-Za-z0-9_-]', '', str(result.get('run_id') or 'saved'))[-24:]
    title_id, search_id, dialog_id = ('qps-title-' + identity, 'qps-search-' + identity, 'qps-viewer-' + identity)
    items = []
    for index, line in enumerate(groups, 1):
        material = material_display(line)
        mode = line.get('mode') if line.get('mode') in {'make', 'buy'} else 'other'
        mode_label = {'make': '自制件', 'buy': '外购件', 'other': '待核对'}[mode]
        priced = line.get('priced') is True
        documents = [value for value in line['source_documents'] if isinstance(value, str)]
        origin = str(len(documents)) + ' 份文件 · ' + str(line['member_count']) + ' 条来源' if line['aggregated'] else _text(line.get('document'))
        search = ' '.join([_text(line.get('part_name')), _text(line.get('id')), material['cad_text'], material['adopted_text'],
                           '' if customer else _text(line.get('material_assumption')), *documents, *[str(value) for value in line['member_ids']]])
        items.append('<article class="qps-item" data-quote-group="' + _esc(line['id'])
            + '" data-member-count="' + str(line['member_count']) + '" data-member-ids="' + _esc(','.join(line['member_ids']))
            + '" data-part-mode="' + mode + '" data-priced="' + ('true' if priced else 'false') + '" data-part-search="' + _esc(search) + '">'
            + '<div class="qps-item-main"><div class="qps-preview"><span class="qps-cad-mark">' + _icon('box') + 'CAD</span>'
            + _preview_link(line, previews, thumbnail_urls, customer=customer) + '</div><div class="qps-identity">'
            + '<div class="qps-item-kicker"><span class="qps-index">' + str(index).zfill(3) + '</span><span class="qps-mode qps-mode-' + mode + '">' + mode_label
            + '</span>' + ('<span class="qps-unpriced">未计价</span>' if not priced else '') + '</div><h3>' + _esc(line.get('part_name'), '未命名零件') + '</h3>'
            + '<div class="qps-material"><strong>' + _esc(material['adopted_text']) + '</strong><span>' + _esc('报价采用' if customer else material['adopted_label']) + '</span></div>'
            + ('<p class="qps-customer-spec">' + _esc(specifications[line['id']]) + '</p>' if customer and specifications.get(line['id']) else '')
            + '<p class="qps-origin" title="' + _esc(' · '.join(documents)) + '">' + _esc(origin) + '<span>' + _esc(line['id']) + '</span></p>'
            + '<dl class="qps-measurements"><div><dt>每套用量</dt><dd>' + _display_count(line.get('quantity_per_set')) + '<span> 件</span></dd></div>'
            + '<div><dt>单件预估净重</dt><dd data-part-unit-weight>' + _kg(line.get('estimated_net_weight_kg')) + '</dd></div>'
            + '<div><dt>订单预估净重</dt><dd data-part-order-weight>' + _kg(line.get('estimated_total_net_weight_kg')) + '</dd></div></dl></div>'
            + '<div class="qps-commercial"><div class="qps-quantity"><span>订单用量</span><strong data-part-quantity>' + _display_count(line.get('quantity_total'), '待核对')
            + '<small> 件</small></strong></div><div class="qps-amount"><span>报价金额</span><strong data-raw-amount="' + _esc(line.get('total_price'), '') + '">'
            + _cash(line.get('total_price'), priced=priced) + '</strong><small>报价单价 <b data-part-unit-price>' + _cash(line.get('unit_price'), priced=priced) + '</b></small></div></div></div>'
            + ('' if customer else '<div class="qps-item-details"><details class="part-notes qps-details"><summary>来源与计价依据<span aria-hidden="true">＋</span></summary>'
            + _basis(line) + '</details>' + _source_details(line, originals, previews, thumbnail_urls) + '</div>') + '</article>')
    filters = ''.join('<button type="button" data-parts-filter="' + key + '" aria-pressed="' + ('true' if key == 'all' else 'false')
                      + '">' + label + '<span data-parts-filter-count>' + str(counts[key]) + '</span></button>'
                      for key, label in (('all', '全部'), ('make', '自制件'), ('buy', '外购件'), ('unpriced', '未计价')))
    return ('<section class="qps" data-parts-showcase data-view="cards" data-total-groups="' + str(len(groups))
            + '" data-total-sources="' + str(len(result['lines'])) + '" data-visible-groups="' + str(len(groups)) + '" aria-labelledby="' + title_id + '">'
            + '<div class="qps-heading"><div><p class="qps-eyebrow">PARTS &amp; MATERIALS</p><h2 id="' + title_id + '">报价与物料明细</h2>'
            + '<p class="qps-subtitle">从原生零件，查看每一项报价。</p></div><span class="qps-review"><i aria-hidden="true"></i>模拟报价 · 待审核</span></div>'
            + '<div class="qps-overview"><div><strong>' + str(len(groups)) + '</strong><span>项物料</span></div><div><strong>' + str(len(result['lines']))
            + '</strong><span>条原始来源</span></div><div><strong>' + _display_count(settings.get('order_sets'), '—') + '</strong><span>套订单</span></div>'
            + '<p>点击零件预览，查看原生 3D 与装配位置。</p></div>'
            + '<div class="qps-controls"><label class="qps-search" for="' + search_id + '">' + _icon('search')
            + '<input type="search" id="' + search_id + '" data-parts-search maxlength="200" placeholder="搜索零件、材料或文件" aria-label="搜索零件、材料或文件"></label>'
            + '<div class="qps-layout" role="group" aria-label="物料显示方式"><button type="button" data-parts-layout="cards" aria-pressed="true" title="零件画廊">' + _icon('grid')
            + '<span>零件画廊</span></button><button type="button" data-parts-layout="list" aria-pressed="false" title="报价清单">' + _icon('list') + '<span>报价清单</span></button></div></div>'
            + '<div class="qps-filter-bar"><div class="qps-filters" role="group" aria-label="按物料类别筛选">' + filters
            + '</div><div class="qps-result-count"><span data-parts-visible-count>显示 ' + str(len(groups)) + ' / ' + str(len(groups)) + ' 项物料</span>'
            + '<button type="button" data-parts-clear hidden>清除筛选</button></div></div>'
            + '<div class="qps-items">' + ''.join(items) + '</div><div class="qps-empty" data-parts-empty hidden>' + _icon('search')
            + '<strong>没有找到匹配的物料</strong><p>试试零件名称、材料或来源文件，也可以清除当前筛选。</p><button type="button" data-parts-reset>清除筛选</button></div>'
            + '<div class="qps-footnote">数量、重量与金额沿用已保存报价。零件预览来自原始 CAD。</div>'
            + '<dialog class="qps-viewer" id="' + dialog_id + '" data-parts-viewer aria-labelledby="' + dialog_id + '-title">'
            + '<div class="qps-viewer-heading"><div><span>NATIVE CAD PREVIEW</span><h3 id="' + dialog_id + '-title" data-parts-viewer-title>原生零件预览</h3></div>'
            + '<div><a data-parts-viewer-open target="_blank" rel="noopener noreferrer">独立打开' + _icon('arrow') + '</a>'
            + '<button type="button" data-parts-viewer-close aria-label="关闭零件预览">' + _icon('close') + '</button></div></div>'
            + '<div class="qps-viewer-loading" data-parts-viewer-loading role="status">正在打开原生零件…</div>'
            + '<iframe data-parts-viewer-frame src="about:blank" title="原生 CAD 零件与装配位置" referrerpolicy="same-origin"></iframe></dialog></section>')
