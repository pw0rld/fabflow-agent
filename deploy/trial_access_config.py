"""An exact public customer-trial entry, with existing API boundaries intact.

The page uses the existing customer API and its Path-scoped session cookie.
This fragment must be included in the same TLS server as the workbench assets.
It never proxies arbitrary /try/ suffixes or any operator API namespace.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


MARKER = '# FabFlow customer trial entry (SDK).'


def trial_access_fragment(upstream='http://127.0.0.1:3092'):
    if not isinstance(upstream, str) or len(upstream) > 200 or any(ord(char) <= 32 or ord(char) == 127 for char in upstream):
        raise ValueError('The trial upstream must be a loopback HTTP origin.')
    try:
        parsed = urlsplit(upstream)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError('The trial upstream must be a loopback HTTP origin.') from None
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {'', '/'} or not port or not 1 <= port <= 65535):
        raise ValueError('The trial upstream must be a loopback HTTP origin.')
    upstream = upstream.rstrip('/')
    return (f'    {MARKER}\n'
        '    location = /try {\n'
        '        auth_basic off;\n'
        '        access_log off;\n'
        '        return 308 /try/;\n'
        '    }\n\n'
        '    location = /try/ {\n'
        '        auth_basic off;\n'
        '        access_log off;\n'
        '        limit_except GET HEAD { deny all; }\n'
        f'        proxy_pass {upstream}/workbench/experience/;\n'
        '        proxy_http_version 1.1;\n'
        '        proxy_set_header Host $host;\n'
        '        proxy_set_header X-Forwarded-Host $host;\n'
        '        proxy_set_header X-Forwarded-Proto $scheme;\n'
        '        proxy_set_header Authorization "";\n'
        '        proxy_set_header Cookie "";\n'
        '        proxy_redirect off;\n'
        '        proxy_connect_timeout 3s;\n'
        '        proxy_read_timeout 30s;\n'
        '    }\n\n'
        '    location ^~ /try/ {\n'
        '        auth_basic off;\n'
        '        access_log off;\n'
        '        return 404;\n'
        '    }\n')


def append_trial_access(previous, upstream='http://127.0.0.1:3092'):
    """Idempotently append to a server-scope include; reject route conflicts."""
    if not isinstance(previous, str):
        raise ValueError('An existing server-scope fragment is required.')
    fragment = trial_access_fragment(upstream)
    if MARKER in previous:
        if previous.count(MARKER) == 1 and fragment in previous:
            return previous
        raise ValueError('The existing trial entry differs; review before updating.')
    if re.search(r'\blocation\s+(?:(?:=|\^~|~\*?)\s+)?["\']?/try(?:\b|/)', previous):
        raise ValueError('The trial entry conflicts with an existing route.')
    return previous.rstrip() + '\n\n' + fragment
