"""Select an in-process Agents SDK runtime behind existing reading contracts.

The legacy bridge owns mature prompts, source validation and immutable candidate
artifacts. Its RPC envelope is an internal compatibility contract here: SDK runs
do not contact a DSH server. The old transport is available only when explicitly
selected, so an SDK failure cannot silently dispatch a second paid request.
"""
from __future__ import annotations

import os

from dsh_bridge import DshBridge as LegacyDshBridge, DshBridgeError


def selected_backend(value=None):
    value = value if value is not None else os.environ.get('FABFLOW_AGENT_BACKEND', 'openai_agents')
    if value in {'openai-agents', 'openai_agents'}:
        return 'openai_agents'
    if value == 'dsh':
        return value
    raise ValueError('FABFLOW_AGENT_BACKEND must be openai_agents or dsh')


def is_configured(legacy_url=None):
    if selected_backend() == 'dsh':
        return bool(legacy_url)
    from agent_config import AgentConfig, AgentExecutionError
    try:
        AgentConfig.from_env().validate_for_run()
    except AgentExecutionError:
        return False
    return True


def provenance_kind(bridge):
    # Historical and injected fake transports retain their original identity.
    return 'openai-agents' if getattr(bridge, 'backend', None) == 'openai_agents' else 'dsh'


class AgentBridge(LegacyDshBridge):
    """Keep start/inspect/stop and evidence checks, replace model execution."""

    def __init__(self, base_url, workspace_path, timeout=15, *, agent_preset='standard',
                 backend=None, runtime=None):
        self.backend = selected_backend(backend)
        # base_url is the optional legacy service URL, never the model endpoint.
        # Preserve validation of explicitly supplied legacy URLs, without using
        # that transport in the default SDK path.
        super().__init__(base_url or 'http://127.0.0.1:3080', workspace_path,
                         timeout, agent_preset=agent_preset)
        self._agent_runtime = runtime

    def _rpc(self, method, payload, *, rpc_id=None):
        if self.backend == 'dsh':
            return super()._rpc(method, payload, rpc_id=rpc_id)
        from agent_runtime import AgentsRuntime, AgentRuntimeError
        try:
            if self._agent_runtime is None:
                self._agent_runtime = AgentsRuntime(self.workspace)
            return self._agent_runtime.call(method, payload, rpc_id=rpc_id)
        except AgentRuntimeError as error:
            raise DshBridgeError(str(error), getattr(error, 'code', 'agent-runtime-error')) from None
