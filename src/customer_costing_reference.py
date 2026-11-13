"""Empty factory-reference adapter for the public source distribution.

No customer workbook, source fingerprint, sample rate, process route or sample
accounting is distributed. A saved order remains the authority for its pricing;
viewing this reference cannot adopt or change any charge.
"""
from __future__ import annotations

import copy


VERSION = 'customer-costing-reference-v1'
SOURCE_SHA256 = ''
SHEETS = ()
PRICING_SAMPLE = {}


def factory_reference(current_pricing):
    """Preserve the existing display contract without supplying a price list.

    ``value_kind`` is the legacy frontend compatibility tag, not evidence of
    configured sample values. ``status`` and the empty source fields explicitly
    identify this reference as unconfigured. The displayed pricing is only a
    detached copy of the current order policy, with no new rate defaults.
    """
    return {
        'version': VERSION,
        'source': {
            'name': '工厂核价参考未配置',
            'status': 'unconfigured',
            'sha256': SOURCE_SHA256,
            'sheet_names': [],
            'formula_count': 0,
            'value_kind': 'hardcoded',
        },
        'auto_applied': False,
        'current_pricing': copy.deepcopy(current_pricing),
        'items': [],
        'pricing_sample': copy.deepcopy(current_pricing),
        'limitations': [
            '公开版本未配置工厂参考表，不包含客户资料、样品费率、工时或金额。',
            '参考区仅展示当前订单已有定价条件，不提供新增默认费率，也不会自动采用费用。',
            '材料、工序及采购费用须按本订单明确录入并审核。',
        ],
        'sample_accounting': {
            'direct_cost': None,
            'management_cost': None,
            'full_cost': None,
            'profit': None,
            'quoted_price': None,
        },
        'process_catalog': [],
    }
