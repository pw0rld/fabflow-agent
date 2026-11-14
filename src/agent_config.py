"""Explicit, lazy AI settings. Importing this module never contacts a model.

Canonical key/base URL variables follow the OpenAI client. FABFLOW-prefixed
aliases are supported for deployments that namespace their environment. Model
selection is always explicit; CAD-only startup does not require AI credentials.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
import re
from urllib.parse import urlsplit


SDK_DISTRIBUTION = 'openai-agents'
SDK_VERSION = '0.24.0'
DEFAULT_BASE_URL = 'https://api.openai.com/v1'


class AgentExecutionError(RuntimeError):
    """A safe application error, without raw transport or tool diagnostics."""

    def __init__(self, code, message, ambiguous=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.ambiguous = bool(ambiguous)


def _integer(environment, name, default, maximum):
    value = environment.get(name)
    if value is None or value == '':
        return default
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', value) or int(value) > maximum:
        raise AgentExecutionError('configuration_error', name + ' must be a bounded positive integer.')
    return int(value)


def _boolean(environment, name, default):
    value = environment.get(name)
    if value is None or value == '':
        return default
    if value not in {'true', 'false', '1', '0'}:
        raise AgentExecutionError('configuration_error', name + ' must be true or false.')
    return value in {'true', '1'}


@dataclass(frozen=True)
class AgentConfig:
    model: str | None = None
    api_key: str | None = field(default=None, repr=False, compare=False)
    base_url: str | None = field(default=None, repr=False)
    model_api: str = 'responses'
    store: bool = False
    max_runtime_seconds: int = 600
    max_requests: int = 12
    max_total_tokens: int = 100_000
    max_tool_calls: int = 64
    max_turns: int = 12
    max_output_tokens: int = 12_000
    max_output_bytes: int = 1_048_576
    max_input_bytes: int = 2_097_152
    max_tool_output_bytes: int = 33_554_432
    max_total_tool_bytes: int = 67_108_864

    @classmethod
    def from_env(cls, environ=None):
        """Read server-owned variables, without opening files or requiring a key.

        OPENAI_API_KEY / OPENAI_BASE_URL take precedence over their
        FABFLOW_OPENAI_API_KEY / FABFLOW_OPENAI_BASE_URL aliases. An absent
        FABFLOW_MODEL remains absent: no SDK model fallback is selected.
        """
        environment = os.environ if environ is None else environ
        def value(primary, alias=None):
            supplied = environment.get(primary)
            if supplied in {None, ''} and alias:
                supplied = environment.get(alias)
            return supplied if supplied not in {None, ''} else None
        values = {
            'model': value('FABFLOW_MODEL'),
            'api_key': value('OPENAI_API_KEY', 'FABFLOW_OPENAI_API_KEY'),
            'base_url': value('OPENAI_BASE_URL', 'FABFLOW_OPENAI_BASE_URL'),
            'model_api': value('FABFLOW_MODEL_API') or 'responses',
            'store': _boolean(environment, 'FABFLOW_MODEL_STORE', False),
        }
        limits = {
            'max_runtime_seconds': ('FABFLOW_MAX_RUNTIME_SECONDS', 600, 3600),
            'max_requests': ('FABFLOW_MAX_REQUESTS', 12, 100),
            'max_total_tokens': ('FABFLOW_MAX_TOTAL_TOKENS', 100_000, 2_000_000),
            'max_tool_calls': ('FABFLOW_MAX_TOOL_CALLS', 64, 512),
            'max_turns': ('FABFLOW_MAX_TURNS', 12, 100),
            'max_output_tokens': ('FABFLOW_MAX_OUTPUT_TOKENS', 12_000, 128_000),
            'max_output_bytes': ('FABFLOW_MAX_OUTPUT_BYTES', 1_048_576, 16_777_216),
            'max_input_bytes': ('FABFLOW_MAX_INPUT_BYTES', 2_097_152, 16_777_216),
            'max_tool_output_bytes': ('FABFLOW_MAX_TOOL_OUTPUT_BYTES', 33_554_432, 67_108_864),
            'max_total_tool_bytes': ('FABFLOW_MAX_TOTAL_TOOL_BYTES', 67_108_864, 268_435_456),
        }
        values.update({key: _integer(environment, *definition) for key, definition in limits.items()})
        return cls(**values)

    def validate_for_run(self, *, require_key=True):
        """Reject unusable AI configuration only when an AI run is requested."""
        if (not isinstance(self.model, str) or not 1 <= len(self.model) <= 200
                or self.model != self.model.strip() or not self.model.isprintable()):
            raise AgentExecutionError('model_not_configured', 'Set FABFLOW_MODEL before starting an AI run.')
        if require_key and (not isinstance(self.api_key, str) or not self.api_key.strip()
                            or len(self.api_key) > 4096 or any(c.isspace() for c in self.api_key)):
            raise AgentExecutionError('api_key_not_configured', 'Configure OPENAI_API_KEY before starting an AI run.')
        if self.model_api not in {'responses', 'chat_completions'}:
            raise AgentExecutionError('configuration_error', 'FABFLOW_MODEL_API must be responses or chat_completions.')
        if type(self.store) is not bool:
            raise AgentExecutionError('configuration_error', 'Model storage must be an explicit boolean.')
        if self.base_url is not None:
            if not isinstance(self.base_url, str) or len(self.base_url) > 2000 or any(c.isspace() for c in self.base_url):
                raise AgentExecutionError('configuration_error', 'OPENAI_BASE_URL must be an HTTP(S) origin or API path.')
            try:
                parsed = urlsplit(self.base_url)
                _ = parsed.port
            except ValueError:
                raise AgentExecutionError('configuration_error', 'OPENAI_BASE_URL is invalid.') from None
            if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment):
                raise AgentExecutionError('configuration_error', 'OPENAI_BASE_URL must not contain credentials, query or fragment.')
        limits = {
            'max_runtime_seconds': 3600, 'max_requests': 100, 'max_total_tokens': 2_000_000,
            'max_tool_calls': 512, 'max_turns': 100, 'max_output_tokens': 128_000,
            'max_output_bytes': 16_777_216, 'max_input_bytes': 16_777_216,
            'max_tool_output_bytes': 67_108_864, 'max_total_tool_bytes': 268_435_456,
        }
        for name, maximum in limits.items():
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= maximum:
                raise AgentExecutionError('configuration_error', name + ' must be a bounded positive integer.')
        return self

    def snapshot(self):
        """Non-secret replay/provenance settings; never serialize key or URL."""
        value = {name: getattr(self, name) for name in (
            'model', 'model_api', 'store', 'max_runtime_seconds', 'max_requests',
            'max_total_tokens', 'max_tool_calls', 'max_turns', 'max_output_tokens',
            'max_output_bytes', 'max_input_bytes', 'max_tool_output_bytes', 'max_total_tool_bytes')}
        transport = self.model_api + '\n' + (self.base_url or DEFAULT_BASE_URL).rstrip('/')
        value['transport_fingerprint'] = hashlib.sha256(transport.encode('utf-8')).hexdigest()
        return value
