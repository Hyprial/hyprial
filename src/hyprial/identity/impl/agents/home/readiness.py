"""Read-only start readiness for an agent, checked in its actual environment.

A fresh agent home can look provisioned while the launch that follows still
fails: an asdf shim resolves under the operator shell but not under the
isolated P2 HOME, a tool exists but no command permission is declared for the
form the task invokes, a Forgejo reference is absent from the agent's own
``XDG_DATA_HOME``, or the source config changed after the running process was
launched.  The implementation evaluates those cases before a task starts.

It is deliberately read-only.  It never calls the runtime-context materializer,
never writes a home/projection/settings file, never restarts a service, and
never reads, copies, or prints credential material.  Every probe is bounded and
uses the environment a launch would actually receive, not the operator's
ambient shell.
"""

from .readiness_checks import (
    BLOCKED,
    PASS,
    READINESS_LIMITS,
    READINESS_ROLES,
    ROLE_TEMPLATES,
    UNKNOWN,
    ReadinessCheck,
    ReadinessError,
    ReadinessReport,
    RoleTemplate,
    ToolDeclaration,
    assess_agent_readiness,
    parse_tool_declaration,
)

__all__ = [
    "BLOCKED",
    "PASS",
    "READINESS_LIMITS",
    "READINESS_ROLES",
    "ROLE_TEMPLATES",
    "UNKNOWN",
    "ReadinessCheck",
    "ReadinessError",
    "ReadinessReport",
    "RoleTemplate",
    "ToolDeclaration",
    "assess_agent_readiness",
    "parse_tool_declaration",
]
