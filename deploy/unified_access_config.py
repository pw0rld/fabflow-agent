"""Nginx access fragment for one workbench, using the existing Basic realm.

No passwords, account records or customer sessions are created by this module.
The guest fallback applies only to the fixed identity probe, never data APIs.
"""

IDENTITY_PATH = '/ortho2cad-test/customer-upload-admin/api/identity'
LOGIN_PATH = '/ortho2cad-test/customer-upload-admin/api/login'
GUEST_LOCATION = '@haoyunlai_workbench_guest'
GUEST_JSON = '{"role":"customer","capabilities":["customer_upload","customer_orders"]}'


def proxy_location(route, upstream='http://127.0.0.1:3092', *, limit='50m', rate_limit=True):
    return ('    location ' + route + ' {\n'
        '        auth_basic "ZeroClave DSH";\n'
        '        access_log off;\n'
        '        client_max_body_size ' + limit + ';\n'
        + ('        limit_req zone=dsh_per_ip burst=30 nodelay;\n' if rate_limit else '')
        + '        limit_except GET HEAD POST { deny all; }\n'
        '        proxy_pass ' + upstream + ';\n'
        '        proxy_http_version 1.1;\n'
        '        proxy_set_header Host $host;\n'
        '        proxy_set_header X-Forwarded-Host $host;\n'
        '        proxy_set_header X-Forwarded-Proto $scheme;\n'
        '        proxy_set_header Authorization "";\n'
        '        proxy_set_header Cookie "";\n'
        '        proxy_connect_timeout 3s;\n'
        '        proxy_read_timeout 60s;\n'
        '        proxy_send_timeout 60s;\n'
        '    }\n')


def identity_locations(upstream='http://127.0.0.1:3092', *, rate_limit=True):
    probe = proxy_location('= ' + IDENTITY_PATH, upstream, limit='4m', rate_limit=rate_limit)
    # Basic validation must run in the original main request. An internal
    # auth_request subrequest does not run the ordinary Basic access phase.
    # A failed probe becomes a fixed customer response with status 200, not a
    # login challenge. Stock Nginx may retain an advertised Basic realm header;
    # real-browser checks verify it does not prompt or elevate the guest role.
    probe = probe.replace('        access_log off;\n',
        '        access_log off;\n'
        '        error_page 401 = ' + GUEST_LOCATION + ';\n'
        '        add_header Cache-Control "no-store" always;\n'
        '        add_header Vary "Authorization" always;\n', 1)
    probe = probe.replace('limit_except GET HEAD POST', 'limit_except GET HEAD')
    return (probe + '\n    location ' + GUEST_LOCATION + ' {\n'
        '        internal;\n'
        '        auth_basic off;\n'
        '        access_log off;\n'
        '        default_type application/json;\n'
        '        add_header Cache-Control "no-store" always;\n'
        '        add_header Vary "Authorization" always;\n'
        '        add_header X-Content-Type-Options "nosniff" always;\n'
        "        return 200 '" + GUEST_JSON + "';\n"
        '    }\n')


def unified_snippet(previous, upstream='http://127.0.0.1:3092', *, rate_limit=True):
    """Patch only the known customer-upload include, keeping owner API intact."""
    old_customer = '    location = /workbench/customer { auth_basic off; return 308 /workbench/customer/; }'
    old_quotation = '    location = /workbench/quotation { auth_basic "ZeroClave DSH"; return 308 /workbench/quotation/; }'
    if (previous.count(old_customer) != 1 or previous.count(old_quotation) != 1
            or IDENTITY_PATH in previous or 'location ^~ /workbench/api/ {' in previous):
        raise RuntimeError('Unexpected existing access fragment; publication stopped')
    revised = previous.replace(old_customer,
        '    location = /workbench/customer { auth_basic off; return 308 /workbench/?workspace=inquiry; }', 1)
    revised = revised.replace(old_quotation, proxy_location('= /workbench/quotation', upstream, limit='4m', rate_limit=rate_limit).rstrip(), 1)
    return (revised + '\n    # Canonical workbench: silent role discovery; legacy internal APIs remain staff-only.\n'
        + identity_locations(upstream, rate_limit=rate_limit) + '\n'
        + proxy_location('^~ /workbench/api/', upstream, rate_limit=rate_limit))
