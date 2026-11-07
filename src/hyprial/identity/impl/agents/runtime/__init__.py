"""Agent runtime layer: tool/runtime context plus secrets, capabilities and
capability enforcement/visibility collaborators moved in by the layout refactor.
"""

from __future__ import annotations

from .context import (
    AgentRuntimeContext,
    AgentRuntimeError,
    AgentRuntimeNotice,
    AgentRuntimePreparation,
    AgentRuntimeRoots,
    SHARED_CREDENTIAL_CONFLICT,
    SHARED_CREDENTIAL_DIVERGED,
    SHARED_CREDENTIAL_INVALID,
    SHARED_CREDENTIAL_UNSUPPORTED,
    SharedCredentialBinding,
    agent_home_mode,
    build_agent_runtime_preparation,
    materialize_agent_runtime_context,
    preflight_codex_credential,
    resolve_agent_runtime_context,
    shared_credential_status,
    validate_shared_credential_binding,
    validate_shared_credential_environment,
)
from .tool_profile import (
    AgentToolProfile,
    GitAuthorUnresolvedError,
    SshToolAuthorization,
    resolve_agent_tool_profile,
)

__all__ = [
    "AgentRuntimeContext",
    "AgentRuntimeError",
    "AgentRuntimeNotice",
    "AgentRuntimePreparation",
    "AgentRuntimeRoots",
    "AgentToolProfile",
    "GitAuthorUnresolvedError",
    "SHARED_CREDENTIAL_CONFLICT",
    "SHARED_CREDENTIAL_DIVERGED",
    "SHARED_CREDENTIAL_INVALID",
    "SHARED_CREDENTIAL_UNSUPPORTED",
    "SharedCredentialBinding",
    "SshToolAuthorization",
    "agent_home_mode",
    "build_agent_runtime_preparation",
    "materialize_agent_runtime_context",
    "preflight_codex_credential",
    "resolve_agent_runtime_context",
    "resolve_agent_tool_profile",
    "shared_credential_status",
    "validate_shared_credential_binding",
    "validate_shared_credential_environment",
]
