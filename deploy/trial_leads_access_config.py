"""Authenticated internal lead workspace; no public contact-data fallback."""
import re

from deploy.trial_access_config import trial_access_fragment

MARKER = '# FabFlow internal trial leads workspace.'
STAFF_API = '/ortho2cad-test/customer-upload-admin/api/trial-leads'


def leads_access_fragment(upstream='http://127.0.0.1:3092'):
    trial_access_fragment(upstream)  # Reuse the strict loopback-origin check.
    upstream = upstream.rstrip('/')

    def location(route):
        return ('    location ' + route + ' {\n'
            '        auth_basic "ZeroClave DSH";\n'
            '        access_log off;\n'
            '        limit_req zone=dsh_per_ip burst=20 nodelay;\n'
            '        client_max_body_size 16k;\n'
            '        limit_except GET HEAD POST { deny all; }\n'
            '        proxy_pass ' + upstream + ';\n'
            '        proxy_http_version 1.1;\n'
            '        proxy_set_header Host $host;\n'
            '        proxy_set_header X-Forwarded-Host $host;\n'
            '        proxy_set_header X-Forwarded-Proto $scheme;\n'
            '        proxy_set_header X-FabFlow-Staff-User $remote_user;\n'
            '        proxy_set_header Authorization "";\n'
            '        proxy_set_header Cookie "";\n'
            '        proxy_connect_timeout 3s;\n'
            '        proxy_read_timeout 30s;\n'
            '        proxy_send_timeout 30s;\n'
            '    }\n')

    return ('    ' + MARKER + '\n'
        '    location = /workbench/leads { auth_basic off; return 308 /workbench/leads/; }\n'
        + location('^~ /workbench/leads/') + '\n'
        + location('= ' + STAFF_API) + '\n'
        + location('^~ ' + STAFF_API + '/'))


def append_leads_access(previous, upstream='http://127.0.0.1:3092'):
    if not isinstance(previous, str):
        raise ValueError('An existing server-scope fragment is required.')
    fragment = leads_access_fragment(upstream)
    if MARKER in previous:
        if previous.count(MARKER) == 1 and fragment in previous:
            return previous
        raise ValueError('The lead workspace access rules differ; review before publication.')
    if re.search(r'\blocation\s+[^\n{]*(?:/workbench/leads|/api/trial-leads)', previous):
        raise ValueError('The lead workspace conflicts with an existing route.')
    return previous.rstrip() + '\n\n' + fragment
