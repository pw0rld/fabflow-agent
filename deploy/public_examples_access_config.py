"""Narrow, read-only access to precomputed licensed public CAD examples."""
from deploy.trial_access_config import trial_access_fragment

MARKER = '# FabFlow reviewed public CAD examples.'


def public_examples_fragment(upstream='http://127.0.0.1:3092'):
    trial_access_fragment(upstream)  # Strict loopback-origin validation.
    return ('    ' + MARKER + '\n'
        '    location ^~ /workbench/public-examples/ {\n'
        '        auth_basic off;\n'
        '        access_log off;\n'
        '        limit_except GET HEAD { deny all; }\n'
        '        limit_req zone=dsh_per_ip burst=12 nodelay;\n'
        '        proxy_pass ' + upstream.rstrip('/') + ';\n'
        '        proxy_set_header Host $host;\n'
        '        proxy_set_header X-Forwarded-Host $host;\n'
        '        proxy_set_header X-Forwarded-Proto $scheme;\n'
        '        proxy_set_header Cookie "";\n'
        '        proxy_set_header Authorization "";\n'
        '        proxy_connect_timeout 3s;\n'
        '        proxy_read_timeout 30s;\n'
        '    }\n')


def append_public_examples(previous, upstream='http://127.0.0.1:3092'):
    fragment = public_examples_fragment(upstream)
    if MARKER in previous:
        if previous.count(MARKER) == 1 and fragment in previous:
            return previous
        raise ValueError('Public example routes changed; review first.')
    if '/workbench/public-examples' in previous:
        raise ValueError('Public example route already exists; review first.')
    return previous.rstrip() + '\n\n' + fragment
