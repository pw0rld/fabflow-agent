"""Versioned market samples and explicit operator-adopted material prices.

No network or supplier verification; quoted tax facts and assumptions stay apart.
"""
from __future__ import annotations

import copy
from datetime import date
from decimal import Decimal, InvalidOperation, localcontext
import re

VERSION = 'foshan-reference-20261106'
RESEARCHED_ON = '2026-11-06'
PROFILE_INFO = [
    {'id': 'carbon_steel', 'name': '碳钢（Q235 类）', 'density_kg_m3': '7850', 'reference_id': 'FS-Q235B-HRC-20261103'},
    {'id': 'spcc', 'name': '冷轧钢板（SPCC 类）', 'density_kg_m3': '7850', 'reference_id': 'FS-SPCC-CRC-20261105'},
    {'id': 'ss304', 'name': '304 不锈钢', 'density_kg_m3': '7930', 'reference_id': 'FS-304-2B-20261105'},
    {'id': 'ss316', 'name': '316 类不锈钢（行情为316L）', 'density_kg_m3': '7980', 'reference_id': 'FS-316L-2B-20261103'},
    {'id': 'al5052', 'name': '5052 铝合金（暂无行情样本）', 'density_kg_m3': '2680', 'reference_id': None},
]
REFERENCES = [
    {'id': 'FS-Q235B-HRC-20261103', 'material_profile': 'carbon_steel', 'grade': 'Q235B',
     'form': '热轧板卷', 'surface': None, 'thickness_mm': '4.75', 'width_mm': '1500', 'steel_mill': '柳钢',
     'region': '佛山乐从', 'price_cny_per_kg': '3.18', 'quote_date': '2026-11-03',
     'source_name': 'Mysteel 乐从市场热轧板卷行情',
     'source_url': 'https://rezha.mysteel.com/m/26110310/E920EAE5BDA5AC3B.html',
     'tax_included': True, 'source_vat_rate_percent': None, 'delivery_basis': '含税、仓库自提；不等于到厂价'},
    {'id': 'FS-SPCC-CRC-20261105', 'material_profile': 'spcc', 'grade': 'SPCC',
     'form': '冷轧卷', 'surface': None, 'thickness_mm': '1.0', 'width_mm': '1250', 'steel_mill': '柳钢',
     'region': '佛山乐从', 'price_cny_per_kg': '3.61', 'quote_date': '2026-11-05',
     'source_name': 'Mysteel 乐从市场冷轧板卷行情',
     'source_url': 'https://lengzha.mysteel.com/m/26110510/FEDC8C6489BD2FFB.html',
     'tax_included': True, 'source_vat_rate_percent': None, 'delivery_basis': '现款含税库提；不含出库费、运费'},
    {'id': 'FS-304-2B-20261105', 'material_profile': 'ss304', 'grade': '304',
     'form': '冷轧毛边卷', 'surface': '2B', 'thickness_mm': '2.0', 'width_mm': None, 'steel_mill': '甬金',
     'region': '佛山', 'price_cny_per_kg': '14.50', 'quote_date': '2026-11-05',
     'source_name': '钢之家 国内不锈钢市场简述',
     'source_url': 'https://news.steelhome.com/2026/11/05/n4789055.html',
     'tax_included': None, 'source_vat_rate_percent': None, 'delivery_basis': None},
    {'id': 'FS-316L-2B-20261103', 'material_profile': 'ss316', 'grade': '316L',
     'form': '冷轧卷板', 'surface': '2B', 'thickness_mm': '2.0', 'width_mm': '1240', 'steel_mill': '甬金',
     'region': '佛山', 'price_cny_per_kg': '29.00', 'quote_date': '2026-11-03',
     'source_name': 'Mysteel 佛山市场300系不锈钢冷轧卷板行情',
     'source_url': 'https://bxg.mysteel.com/juanban/m/26110310/875E818C0B1639F1.html',
     'tax_included': None, 'source_vat_rate_percent': None, 'delivery_basis': None},
]
PROFILE_IDS = {item['id'] for item in PROFILE_INFO}
ROW_KEYS = {'rate', 'tax_included', 'source_kind', 'reference_id', 'source_text', 'quote_date'}


def pricing_catalog():
    return copy.deepcopy({'version': VERSION, 'researched_on': RESEARCHED_ON,
        'profiles': PROFILE_INFO, 'references': REFERENCES,
        'notes': ['行情为带日期的公开样本，并非实时接口或好运来的实际采购成交价。',
                  '参考规格未与本件自动核对，仅作为同类估价基准。',
                  '原文未确认的税率、税运条件保持未知；本单税率是操作员估算假设。',
                  '材料价调整不改变利用率、加工、外购及准备费的演示性质。']})


def _decimal(value, label, *, maximum, zero=True):
    if (not isinstance(value, str) or len(value) > 40
            or not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?', value)):
        raise ValueError(label + '须为十进制数字字符串。')
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(label + '无效。') from error
    if not parsed.is_finite() or parsed > maximum or (parsed < 0 if zero else parsed <= 0):
        raise ValueError(label + '超出允许范围。')
    return parsed


def _text(value):
    text = format(value, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _date(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('价格日期须为 YYYY-MM-DD，未知可留空。')
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError('价格日期无效。') from error
    return value


def validate_material_pricing(value):
    if not isinstance(value, dict) or set(value) != {'vat_rate_percent', 'confirmed', 'rows'}:
        raise ValueError('请填写本单材料采用价、税率假设和来源确认。')
    vat = _decimal(value['vat_rate_percent'], '估算税率', maximum=Decimal('100'))
    if value['confirmed'] is not True:
        raise ValueError('请确认本次材料价及税运、规格假设。')
    supplied = value['rows']
    if not isinstance(supplied, dict) or not set(supplied).issubset(PROFILE_IDS) or len(supplied) > len(PROFILE_IDS):
        raise ValueError('材料采用价的材料类别无效。')
    by_id = {item['id']: item for item in REFERENCES}
    rows = {}
    for profile, row in supplied.items():
        if not isinstance(row, dict) or set(row) != ROW_KEYS:
            raise ValueError('材料采用价字段无效。')
        rate = None if row['rate'] is None else _decimal(row['rate'], '材料采用单价', maximum=Decimal('1000000000'), zero=False)
        if (type(row['tax_included']) is not bool or not isinstance(row['source_kind'], str)
                or row['source_kind'] not in {'market_reference', 'supplier_quote', 'manual'}):
            raise ValueError('请明确采用价的税口径及来源类型。')
        source = row['source_text']
        if not isinstance(source, str) or len(source) > 2000 or any(ord(char) < 32 and char not in '\n\t' for char in source):
            raise ValueError('请填写有效的材料价格来源说明。')
        quote_date = _date(row['quote_date'])
        reference_id = row['reference_id']
        reference = None
        if reference_id is not None:
            if not isinstance(reference_id, str):
                raise ValueError('行情参考编号无效。')
            reference = by_id.get(reference_id)
            if reference is None or reference['material_profile'] != profile:
                raise ValueError('行情参考编号与材料类别不一致。')
        if row['source_kind'] == 'market_reference':
            if reference is None:
                raise ValueError('行情采用价必须关联有效的原始行情记录。')
            if rate is not None and rate != Decimal(reference['price_cny_per_kg']):
                raise ValueError('修改行情原价后，请改为手工或供应商来源并说明依据。')
            if reference['tax_included'] is True and row['tax_included'] is not True:
                raise ValueError('该原始行情明确含税，不能直接标为未税。')
            if quote_date is not None and quote_date != reference['quote_date']:
                raise ValueError('行情日期须保留原始来源日期。')
            quote_date = reference['quote_date']
            source = reference['source_name'] + ' · ' + reference['source_url']
        elif rate is not None and not source.strip():
            raise ValueError('供应商或手工采用价须填写来源说明。')
        rows[profile] = {'rate': _text(rate) if rate is not None else None,
                         'tax_included': row['tax_included'], 'source_kind': row['source_kind'],
                         'reference_id': reference_id, 'source_text': source.strip(), 'quote_date': quote_date}
    return {'vat_rate_percent': _text(vat), 'confirmed': True, 'rows': rows}


def resolve_material_price(profile_key, raw, settings):
    """Resolve already-validated choices; never fetch or update a quote here."""
    adopted = settings['material_pricing']
    row = adopted['rows'].get(profile_key)
    reference = next((copy.deepcopy(item) for item in REFERENCES
                      if row and item['id'] == row.get('reference_id')), None)
    snapshot = {'catalog_version': VERSION, 'researched_on': RESEARCHED_ON,
        'material_profile': profile_key, 'raw_material': raw,
        'material_name': next((item['name'] for item in PROFILE_INFO if item['id'] == profile_key), profile_key),
        'reference': reference, 'reference_tax_included': reference['tax_included'] if reference else None,
        'reference_id': reference['id'] if reference else None,
        'reference_vat_rate_percent': reference['source_vat_rate_percent'] if reference else None,
        'adopted_rate_cny_per_kg': row['rate'] if row else None,
        'original_rate': row['rate'] if row else None,
        'adopted_tax_included': row['tax_included'] if row else None,
        'adopted_vat_rate_percent': adopted['vat_rate_percent'],
        'normalized_net_rate': None, 'source_kind': row['source_kind'] if row else 'missing',
        'source_text': row['source_text'] if row else '', 'quote_date': row['quote_date'] if row else None,
        'source_url': reference['source_url'] if reference and row['source_kind'] == 'market_reference' else None,
        'match_state': 'assumed_reference_spec' if row and row['source_kind'] == 'market_reference' else 'operator_adopted' if row else 'missing',
        'supplier_price_verified': False, 'vat_rate_is_assumption': True,
        'tax_basis_is_assumption': not (reference and row['source_kind'] == 'market_reference'
                                        and reference['tax_included'] is not None),
        'notes': ['采用价未经供应商实价核验，报价仍待人工审核。']}
    if reference:
        snapshot['notes'].append('行情样本为 ' + reference['grade'] + ' ' + reference['form']
            + ' ' + reference['thickness_mm'] + ' mm；本件牌号、板厚、表面、板卷、边型未自动核对，仅作同类估价基准。')
    if reference and reference['tax_included'] is None:
        snapshot['notes'].append('原行情税口径未说明，本次含税或未税选择是操作员估算假设。')
    if row is None or row['rate'] is None:
        snapshot['match_state'] = 'missing'
        snapshot['notes'].append('没有采用材料单价；保留估重，本行暂不计价，不回填演示材料价。')
        return snapshot
    with localcontext() as context:
        context.prec = 64
        rate = Decimal(row['rate'])
        if row['tax_included']:
            rate /= Decimal(1) + Decimal(adopted['vat_rate_percent']) / 100
            snapshot['notes'].append('含税采用价按本单税率假设换算未税成本；该税率不是来源已确认税率。')
        snapshot['normalized_net_rate'] = _text(rate)
    return snapshot
