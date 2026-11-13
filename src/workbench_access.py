"""Public presentation and redirects over the deployed Nginx Basic boundary.

No role is inferred from request headers, query strings, browser storage or a
customer token. Staff endpoints must be protected by Nginx before proxying to
the loopback-only application. This module creates no accounts or sessions.
"""
import re
from urllib.parse import parse_qs, urlencode, urlsplit

STAFF_API = '/ortho2cad-test/customer-upload-admin/api'
IDENTITY_PATH = STAFF_API + '/identity'
LOGIN_PATH = STAFF_API + '/login'
ROOT_PATH = '/workbench/'
DEFAULT_NEXT = ROOT_PATH + '?workspace=orders'
CUSTOMER_CAPABILITIES = ('customer_upload', 'customer_orders')
STAFF_CAPABILITIES = (*CUSTOMER_CAPABILITIES, 'staff_orders', 'staff_costing', 'step_engineering', 'image_recognition')
PARAMETERS = ('workspace', 'scope', 'id', 'flow', 'document', 'job', 'sample', 'view')
ENUMS = {'workspace': {'inquiry', 'orders', 'engineering'}, 'scope': {'customer', 'public', 'poland'},
         'flow': {'step', 'image'}, 'sample': {'step', 'image'}, 'view': {'source', 'manufacturing'}}
IDENTIFIERS = {'id': r'BATCH-[a-f0-9]{32}', 'document': r'CAD-[a-f0-9]{32}', 'job': r'SPL-[a-f0-9]{32}'}


def staff_identity():
    """Called only on the exact upstream location after Basic authentication."""
    return {'role': 'staff', 'capabilities': list(STAFF_CAPABILITIES)}


def guest_identity():
    """The equivalent fixed JSON used by the Nginx identity-only fallback."""
    return {'role': 'customer', 'capabilities': list(CUSTOMER_CAPABILITIES)}


def _query(raw, allowed):
    if not isinstance(raw, str) or len(raw) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError('工作台返回地址无效。')
    try:
        values = parse_qs(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
    except ValueError as error:
        raise ValueError('工作台返回地址参数无效。') from error
    if not set(values) <= set(allowed) or any(len(items) != 1 for items in values.values()):
        raise ValueError('工作台返回地址包含重复或不支持的参数。')
    return {key: items[0] for key, items in values.items()}


def canonical_root(target):
    """Allow only the same-origin root and bounded, known workflow values."""
    if (not isinstance(target, str) or len(target) > 4096 or '%' in target or '\\' in target
            or any(ord(char) < 33 or ord(char) == 127 for char in target)):
        raise ValueError('仅支持返回本工作台。')
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != ROOT_PATH:
        raise ValueError('仅支持返回本工作台根入口。')
    values = _query(parsed.query, PARAMETERS)
    for key, value in values.items():
        valid = value in ENUMS[key] if key in ENUMS else re.fullmatch(IDENTIFIERS[key], value) is not None
        if not valid:
            raise ValueError('工作台返回地址参数值无效：' + key)
    encoded = urlencode({key: values[key] for key in PARAMETERS if key in values})
    return ROOT_PATH + ('?' + encoded if encoded else '')


def login_destination(query):
    values = _query(query, {'next'})
    return canonical_root(values.get('next', DEFAULT_NEXT))


def login_url(target=DEFAULT_NEXT):
    return LOGIN_PATH + '?' + urlencode({'next': canonical_root(target)})


def legacy_quotation_login(query):
    values = _query(query, {'scope', 'id'})
    target = ROOT_PATH + '?' + urlencode({'workspace': 'orders', 'scope': values.get('scope', 'public'),
                                         **({'id': values['id']} if 'id' in values else {})})
    return login_url(canonical_root(target))


def legacy_customer_root(query):
    values = _query(query, {'id'})
    return canonical_root(ROOT_PATH + '?' + urlencode({'workspace': 'inquiry', **values}))
