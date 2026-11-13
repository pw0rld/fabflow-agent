"""Read-only HTML presentations of already rendered quotation artifacts.

No files, geometry, models or pricing routines are accessed here. Every table
cell comes from the supplied CSV bytes; JSON fields come from the JSON bytes.
The source file links are fixed names, not values supplied by a document.
"""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import html
import io
import json


PREVIEW_FILES = {
    'preview-summary.html': ('summary', 'bom-summary.csv'),
    'preview-source.html': ('source', 'bom.csv'),
    'preview-cost.html': ('cost', 'cost.csv'),
    'preview-record.html': ('record', 'quote.json'),
}

SPECS = {
    'summary': {'title': '报价物料汇总', 'source': 'bom-summary.csv', 'preview': 'preview-summary.html',
                'columns': ('汇总编号', '物料名称', '候选分类', '采用材料', '每套用量', '订单总件数',
                            '单件预估净重_kg', '订单预估净重_kg', '报价单价_CNY', '报价金额_CNY')},
    'source': {'title': '来源明细', 'source': 'bom.csv', 'preview': 'preview-source.html',
               'columns': ('物料编号', '物料名称', '源文件', '候选分类', '每套用量', '订单总件数',
                           '单件预估净重_kg', '订单预估净重_kg')},
    'cost': {'title': '成本明细', 'source': 'cost.csv', 'preview': 'preview-cost.html',
             'columns': ('物料编号', '物料名称', '源文件', '订单总件数', '材料成本_CNY', '加工成本_CNY',
                         '外购成本_CNY', '准备费_CNY', '总成本_CNY', '报价单价_CNY', '报价金额_CNY')},
    'record': {'title': '报价记录', 'source': 'quote.json', 'preview': 'preview-record.html'},
}

KEY_LABELS = {
    'name': '订单名称', 'status': '状态', 'simulated': '模拟标识', 'settings': '报价条件',
    'lines': '原始物料记录', 'totals': '金额汇总', 'weight_summary': '预估重量',
    'assumptions': '采用假设', 'limitations': '范围与待核对事项', 'source_files': '来源文件',
    'links': '原文件地址', 'created_at': '记录时间', 'run_id': '报价运行编号',
    'material_price_sources': '材料价格来源', 'material_pricing_snapshot': '材料价格快照',
}


def _esc(value):
    return html.escape(str(value), quote=True)


def _price(value):
    if value is None:
        return '尚无可计价金额'
    try:
        with localcontext() as context:
            context.prec = 64
            number = Decimal(str(value))
            if not number.is_finite() or number < 0 or number >= Decimal('1e24'):
                return '金额待核对'
            return '¥' + format(number, ',.2f')
    except (InvalidOperation, ValueError):
        return '金额待核对'


def _display_cell(header, value):
    if value == '':
        return '<span class="empty" title="原始值为空">—</span>'
    counts = header in {'每套用量', '订单总件数', '来源行数'}
    numeric = counts or header.endswith(
        ('_kg', '_CNY', '_CNY每kg', '_kg每m3', '_mm3', '_percent'))
    if numeric:
        try:
            with localcontext() as context:
                context.prec = 64
                number = Decimal(value)
                if number.is_finite() and 0 <= number < Decimal('1e24'):
                    displayed = (format(number, ',.0f') if counts and number == number.to_integral_value() else
                                 '&lt;0.01' if 0 < number < Decimal('.01') else
                                 format(number.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), ',.2f'))
                    unit = ' kg' if header.endswith('_kg') else ''
                    return '<span title="原始值：' + _esc(value) + unit + '">' + displayed + '</span>'
        except (InvalidOperation, ValueError):
            pass
    if header == '候选分类':
        label = {'make': '自制件（候选）', 'buy': '外购件（候选）', 'unresolved': '待核对'}.get(value)
        if label:
            return '<span title="原始值：' + _esc(value) + '">' + _esc(label) + '</span>'
    return _esc(value)


def _column_label(header):
    return header.replace('_kg', '（kg）').replace('_CNY', '（元）')


def _csv_content(kind, content):
    parsed = list(csv.reader(io.StringIO(content.decode('utf-8-sig'))))
    if not parsed or not parsed[0]:
        raise ValueError('预览文件缺少表头。')
    headers, rows = parsed[0], parsed[1:]
    if any(len(row) != len(headers) for row in rows):
        raise ValueError('预览文件字段数量不一致。')
    selected = [headers.index(name) for name in SPECS[kind]['columns'] if name in headers]
    # Preserve visibility for a valid older export whose column names differ.
    if not selected:
        selected = list(range(min(len(headers), 8)))
    output = []
    for index, row in enumerate(rows):
        cells = ''.join('<td' + (' class="number"' if any(unit in headers[column] for unit in ('_kg', '_CNY', '数量', '件数', '用量')) else '')
                        + '>' + _display_cell(headers[column], row[column]) + '</td>' for column in selected)
        fields = ''.join('<div><dt>' + _esc(header) + '</dt><dd>'
                         + (_display_cell(header, value) if value else '<span class="empty">原始值为空</span>')
                         + '</dd></div>' for header, value in zip(headers, row))
        output.append('<tr data-preview-row="' + str(index) + '">' + cells
                      + '<td class="details-cell"><details class="preview-row-details"><summary>全部字段（'
                      + str(len(headers)) + '）</summary><dl>' + fields + '</dl></details></td></tr>')
    head = ''.join('<th>' + _esc(_column_label(headers[column])) + '</th>' for column in selected)
    empty = '<p class="notice">该文件暂无明细行。</p>' if not rows else ''
    return ('<p class="hint">报价依据中的物料与数值；工程规格请查看工程 BOM。按件数量显示整数，重量与金额保留两位小数；悬停查看原始精度。空白值不代表零。</p>' + empty
            + '<div class="table-wrap"><table data-preview-table data-row-count="' + str(len(rows)) + '"><thead><tr>' + head
            + '<th>完整内容</th></tr></thead><tbody>' + ''.join(output) + '</tbody></table></div>'), len(rows)


def _record_content(content):
    value = json.loads(content.decode('utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('报价记录应为 JSON 对象。')
    sections = []
    for key, item in value.items():
        label = KEY_LABELS.get(key, key)
        size = ' · ' + str(len(item)) + (' 条' if isinstance(item, list) else ' 个字段') if isinstance(item, (list, dict)) else ''
        # A small totals section is useful immediately; large nested records stay collapsed.
        opened = ' open' if key == 'totals' else ''
        text = json.dumps(item, ensure_ascii=False, indent=2, allow_nan=False)
        sections.append('<details class="preview-json-section" data-record-key="' + _esc(key) + '" data-json-key="' + _esc(key) + '"' + opened
                        + '><summary><strong>' + _esc(label) + '</strong><code>' + _esc(key) + '</code><span>'
                        + _esc(size) + '</span></summary><pre>' + _esc(text) + '</pre></details>')
    return ('<p class="hint">按原始 JSON 字段展开查看。所有内容只读，下载后仍为原始记录。</p>'
            + '<div class="json-sections">' + ''.join(sections) + '</div>'), len(value.get('lines', [])) if isinstance(value.get('lines'), list) else None


def render_artifact_preview(content, kind, result, *, back_href='quote.html'):
    """Present one fixed CSV/JSON artifact; never rewrite its source bytes."""
    if kind not in SPECS or not isinstance(content, bytes):
        raise ValueError('不支持的预览文件。')
    if back_href not in {'quote.html', 'quote-visual.html'}:
        raise ValueError('预览返回地址无效。')
    spec = SPECS[kind]
    body, count = _record_content(content) if kind == 'record' else _csv_content(kind, content)
    title, source = spec['title'], spec['source']
    navigation = ('<a href="engineering-bom.html">工程 BOM</a><a href="materials.html">备料汇总</a><a href="processes.html">工艺明细</a>'
                  + ''.join('<a data-preview-tab="' + key + '" href="' + item['preview'] + '"'
                         + (' aria-current="page"' if key == kind else '') + '>' + _esc(item['title']) + '</a>'
                         for key, item in SPECS.items()))
    navigation += '<a data-preview-tab="print" href="quote-print.html">打印版</a>'
    settings = result.get('settings') if isinstance(result.get('settings'), dict) else {}
    totals = result.get('totals') if isinstance(result.get('totals'), dict) else {}
    amount = _price(totals.get('total_price'))
    scope = '已计价范围' if isinstance(totals.get('unpriced_lines'), int) and totals['unpriced_lines'] > 0 else '参考报价'
    count_text = ('本页 ' + str(count) + (' 项汇总物料' if kind == 'summary' else ' 条原始记录')) if count is not None else '原始记录'
    return ('<!doctype html><html lang="zh-CN" data-preview-kind="' + kind + '" data-artifact-preview="' + kind + '"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>' + _esc(title) + '预览</title><style>'
            '*{box-sizing:border-box}body{margin:0;color:#243b4b;background:#f3f6f8;font:14px/1.6 system-ui,sans-serif}'
            'main{max-width:1440px;margin:auto;padding:24px}.toolbar{position:sticky;top:0;z-index:3;background:#fff;border-bottom:1px solid #dce5eb;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}'
            '.toolbar a{color:#276d89;text-decoration:none}.toolbar .download{background:#246f8b;color:#fff;border:1px solid #246f8b;border-radius:6px;padding:8px 18px;font-weight:600}.toolbar a:focus-visible,summary:focus-visible{outline:3px solid #7ab5d1;outline-offset:3px}'
            '.heading{background:white;border:1px solid #dce5eb;border-radius:10px;padding:20px 24px;margin-bottom:16px}h1{font-size:25px;margin:0 0 7px}.name{margin:0;color:#526d7f;overflow-wrap:anywhere}.meta{display:flex;gap:10px 22px;flex-wrap:wrap;margin:14px 0;color:#496a80}.meta strong{color:#254c63}.status{font-size:12px;color:#8e651e}.tabs{display:flex;gap:8px;flex-wrap:wrap;border-top:1px solid #e4ebef;padding-top:15px}.tabs a{text-decoration:none;border:1px solid #d4e2ea;border-radius:6px;padding:7px 13px;color:#426e89}.tabs a[aria-current=page]{background:#e9f4fa;border-color:#92bfd6;color:#195d7d;font-weight:600}'
            '.hint{color:#657e8e;margin:12px 0}.table-wrap{background:#fff;overflow:auto;border:1px solid #dce5eb;border-radius:8px}table{border-collapse:collapse;min-width:1050px;width:100%;font-size:13px}th,td{padding:10px 12px;border-bottom:1px solid #e2e9ee;text-align:left;vertical-align:top;max-width:240px;overflow-wrap:anywhere}th{position:sticky;top:0;background:#edf4f8;color:#34566d;z-index:1;font-weight:600}tbody tr:hover{background:#f8fbfd}.number{font-variant-numeric:tabular-nums;white-space:nowrap;text-align:right}.empty{color:#8a9ca8}.details-cell{min-width:185px;max-width:420px}summary{cursor:pointer;color:#286e8d}.preview-row-details dl{margin:12px 0 0;min-width:260px}.preview-row-details dl>div{margin-bottom:12px}.preview-row-details dt{font-size:11px;color:#7b919f}.preview-row-details dd{margin:3px 0;color:#2b475b;white-space:pre-wrap;overflow-wrap:anywhere}'
            '.json-sections{display:grid;gap:10px}.preview-json-section{background:#fff;border:1px solid #dce5eb;border-radius:8px;min-width:0}.preview-json-section>summary{padding:13px 16px}.preview-json-section summary code{color:#78919f;margin-left:12px;font-size:12px}.preview-json-section summary span{color:#78919f;font-size:12px}.preview-json-section pre{font:12px/1.7 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;padding:0 16px 15px;margin:0;max-height:600px;overflow:auto;color:#29485d}.notice{background:#fff5df;color:#815c1e;padding:12px 16px;border-radius:6px}.footer{font-size:12px;color:#7b919f;margin-top:18px}'
            '@media(max-width:640px){main{padding:12px}.heading{padding:16px}.toolbar{padding:12px 16px}h1{font-size:21px}.tabs{gap:6px}.tabs a{font-size:12px;padding:6px 9px}.meta{gap:6px 15px;font-size:12px}}'
            '</style></head><body><div class="toolbar"><a href="' + back_href + '">← 返回报价页</a>'
            + '<a class="download" data-preview-download data-download-artifact="' + source + '" href="' + source + '" download="' + source + '">下载 '
            + ('JSON' if kind == 'record' else 'CSV') + '</a></div><main><header class="heading"><h1>' + _esc(title) + '预览</h1>'
            + '<p class="name">' + _esc(result.get('name') or '已保存报价') + '</p><div class="meta"><span>'
            + _esc(count_text) + '</span><span>' + _display_cell('每套用量', str(settings.get('order_sets', '—'))) + ' 套</span><span>' + scope
            + ' <strong data-preview-total="' + _esc(totals.get('total_price') if totals.get('total_price') is not None else '')
            + '">' + _esc(amount) + '</strong></span></div><p class="status">模拟报价草稿 · 待工程与商务审核 · 人民币、未税、不含运费</p>'
            + '<nav class="tabs" aria-label="文件预览">' + navigation + '</nav></header>' + body
            + '<p class="footer">预览沿用已保存文件。查看和下载不会修改报价、数量或来源记录。</p></main></body></html>').encode('utf-8')
