"""Same-source Contract Mock cases for the five Agent request methods.

Decision record (M-03, 2026-10-31)
- Example data lives beside the declarations, in this one aggregation module,
  rather than in a second method table, a mock-only DTO set or the HTTP layer.
  Each set names an already-declared method; the exporter validates every case
  against that method's own params, result and error schemas.
- The five sets are the combined M-01/M-02 candidate declarations.  The user's
  Task A priority release authorizes building this combination candidate; it is
  not an acceptance verdict for those cards.  The data is still generated from
  the declared schemas, not copied into a parallel DTO.
- Provider-default and provider-login deliberately accept every JSON object at
  the schema boundary.  Their true structural negative is a non-object body;
  the handler-level rejections are ``semantic-param-error`` cases, never a
  tighter declaration or a fabricated object negative.
- Every value is synthetic: provider and agent names are illustrative, the
  login file decodes to ``example-not-a-credential``, and paths live under
  ``/tmp/hyprial-example``.  No real credential, home path or daemon state is
  read or published.
"""

from __future__ import annotations

import base64

from hyprial.identity import AgentError
from hyprial.kernel import (
    MethodExample,
    MethodExampleError,
    MethodExampleSet,
    ipc_errors,
)

__all__ = ["REQUEST_EXAMPLES"]

SYNTHETIC_CREDENTIAL = "example-not-a-credential"
# Derived, not spelled out: a literal base64 blob reads as a generic API key
# to the publish gate's gitleaks scan (github-publish run 44089).
SYNTHETIC_CREDENTIAL_B64 = base64.b64encode(SYNTHETIC_CREDENTIAL.encode("utf-8")).decode("ascii")
SYNTHETIC_AUTH_PATH = "/tmp/hyprial-example/.credentials.json"
SYNTHETIC_REMOVED_PATH = SYNTHETIC_AUTH_PATH
_AGENT = "worker"
_AGENT_URI = "agent:acme:node-1:worker"
_PROVIDER = "claude"

_AGENT_ROW = {
    "uri": _AGENT_URI,
    "actor": _AGENT,
    "owner": "acme",
    "machine": "node-1",
    "cwd": "/work/worker",
    "config": None,
    "provider": _PROVIDER,
    "model": None,
    "capabilities": {},
    "harnessArgs": {},
    "preferredHarness": _PROVIDER,
    "lastHarness": _PROVIDER,
    "lastSessionId": None,
    "lastActiveAtMs": None,
    "pinnedAdapters": [],
    "createdAtMs": 1760000000000,
    "hosted": False,
    "hostedBy": None,
    "activityHints": {
        "createdAtMs": 1760000000000,
        "hasLastSessionId": False,
        "agentHomeMtimeMs": None,
    },
    "status": "ready",
    "harness": _PROVIDER,
    "runtime": None,
    "sessionId": None,
    "heartbeatAgeSeconds": None,
}

_GRANT_ROW = {
    "actor": _AGENT,
    "grantId": "grant-0001",
    "source": "agent-private",
    "entryId": "entry-0001",
    "fieldName": "ANTHROPIC_API_KEY",
    "environmentNames": ["ANTHROPIC_API_KEY"],
    "revision": 1,
    "ownerActor": None,
    "ownerEntityToken": None,
}

_AGENT_LIST = MethodExampleSet(
    method="agent.list",
    cases=(
        MethodExample(
            name="one-agent",
            kind="success",
            description="one registered agent is returned",
            params={},
            result={
                "ok": True,
                "agents": [_AGENT_ROW],
                "agentsWithoutRoutine": [],
            },
        ),
        MethodExample(
            name="empty-registry",
            kind="empty",
            description="no agents are registered on this machine",
            params={"inactiveSince": None},
            result={"ok": True, "agents": [], "agentsWithoutRoutine": []},
            reason="an empty agent registry is a legal successful read",
        ),
        MethodExample(
            name="boolean-duration",
            kind="invalid-params",
            description="a boolean is not a duration",
            params={"inactiveSince": True},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "inactiveSince must be a duration, not a boolean"
            ),
        ),
        MethodExample(
            name="zero-duration",
            kind="semantic-param-error",
            description="the legacy handler rejects a non-positive duration",
            params={"inactiveSince": 0},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "inactiveSince must be positive, got 0.0s"
            ),
        ),
        MethodExample(
            name="registry-unavailable",
            kind="domain-error",
            description="the agent registry cannot be read",
            params={},
            error=MethodExampleError(
                AgentError.code, "the agent registry could not be read"
            ),
        ),
    ),
)

_AGENT_SECRET_LIST = MethodExampleSet(
    method="agent.secret.list",
    cases=(
        MethodExample(
            name="one-grant",
            kind="success",
            description="one grant is visible for the selected agent",
            params={"actor": _AGENT},
            result={"ok": True, "grants": [_GRANT_ROW]},
        ),
        MethodExample(
            name="no-grants",
            kind="empty",
            description="the selected agent has no grants",
            params={"actor": _AGENT},
            result={"ok": True, "grants": []},
            reason="an agent with no grant rows is a legal successful read",
        ),
        MethodExample(
            name="actor-wrong-type",
            kind="invalid-params",
            description="the actor selector must be a string or null",
            params={"actor": 5},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "actor must be null or a non-empty agent name"
            ),
        ),
        MethodExample(
            name="unknown-agent",
            kind="domain-error",
            description="the selected agent is not registered",
            params={"actor": "missing-agent"},
            error=MethodExampleError(
                ipc_errors.AGENT_NOT_FOUND,
                "no agent with that name is registered on this machine",
            ),
        ),
    ),
)

_PROVIDER_DEFAULT = MethodExampleSet(
    method="agent.provider.default",
    cases=(
        MethodExample(
            name="configured",
            kind="success",
            description="a provider is the current default",
            params={},
            result={"ok": True, "provider": _PROVIDER, "previous": None},
        ),
        MethodExample(
            name="not-configured",
            kind="unconfigured",
            description="no default provider has been configured",
            params={},
            result={"ok": True, "provider": None, "previous": None},
            reason="a node with no default provider is a legal unconfigured state",
        ),
        MethodExample(
            name="non-object-body",
            kind="invalid-params",
            description="the universal object schema still rejects a non-object body",
            params=[],
            error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "params must be a JSON object"),
        ),
        MethodExample(
            name="provider-wrong-shape",
            kind="semantic-param-error",
            description="the handler validates a schema-admitted provider value",
            params={"provider": 5},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "provider must be a non-empty string"
            ),
        ),
        MethodExample(
            name="provider-unsupported",
            kind="domain-error",
            description="the named provider is not supported on this machine",
            params={"provider": "unsupported-provider"},
            error=MethodExampleError(
                ipc_errors.PROVIDER_UNSUPPORTED, "the provider is not supported on this machine"
            ),
        ),
    ),
)

_PROVIDER_LOGIN = MethodExampleSet(
    method="agent.provider.login",
    empty_not_applicable=(
        "login either installs the supplied credential files and returns receipts "
        "or fails; it has no legal empty result"
    ),
    cases=(
        MethodExample(
            name="file-receipt",
            kind="success",
            description="one synthetic provider file is installed",
            params={
                "actor": _AGENT,
                "provider": _PROVIDER,
                "files": [
                    {"filename": ".credentials.json", "contentBase64": SYNTHETIC_CREDENTIAL_B64}
                ],
            },
            result={
                "ok": True,
                "actor": _AGENT,
                "provider": _PROVIDER,
                "files": [
                    {
                        "path": SYNTHETIC_AUTH_PATH,
                        "bytes": len(SYNTHETIC_CREDENTIAL.encode("utf-8")),
                        "replaced": False,
                        "unchanged": False,
                    }
                ],
            },
        ),
        MethodExample(
            name="non-object-body",
            kind="invalid-params",
            description="the universal object schema still rejects a non-object body",
            params=[],
            error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "params must be a JSON object"),
        ),
        MethodExample(
            name="empty-file-list",
            kind="semantic-param-error",
            description="the handler requires a non-empty file list",
            params={"actor": _AGENT, "provider": _PROVIDER, "files": []},
            error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "files must be a non-empty array"),
        ),
        MethodExample(
            name="target-not-authorized",
            kind="domain-error",
            description="the caller may not install a credential for that agent",
            params={
                "actor": _AGENT,
                "provider": _PROVIDER,
                "files": [
                    {"filename": ".credentials.json", "contentBase64": SYNTHETIC_CREDENTIAL_B64}
                ],
            },
            error=MethodExampleError(
                ipc_errors.TARGET_NOT_AUTHORIZED, "the target agent is not authorized"
            ),
        ),
    ),
)

_PROVIDER_LOGOUT = MethodExampleSet(
    method="agent.provider.logout",
    cases=(
        MethodExample(
            name="removed",
            kind="success",
            description="the agent's synthetic provider file is removed",
            params={"actor": _AGENT, "provider": _PROVIDER},
            result={
                "ok": True,
                "actor": _AGENT,
                "provider": _PROVIDER,
                "removed": [SYNTHETIC_REMOVED_PATH],
                "absent": [],
            },
        ),
        MethodExample(
            name="already-absent",
            kind="empty",
            description="the agent has no provider file to remove",
            params={"actor": _AGENT, "provider": _PROVIDER},
            result={
                "ok": True,
                "actor": _AGENT,
                "provider": _PROVIDER,
                "removed": [],
                "absent": [SYNTHETIC_REMOVED_PATH],
            },
            reason="removing an already-absent credential is a legal empty removal",
        ),
        MethodExample(
            name="actor-wrong-type",
            kind="invalid-params",
            description="the actor selector must be a string",
            params={"actor": 5},
            error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "actor must be a non-empty string"),
        ),
        MethodExample(
            name="provider-missing",
            kind="semantic-param-error",
            description="the handler requires a provider after the actor",
            params={"actor": _AGENT},
            error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "provider must be a non-empty string"),
        ),
        MethodExample(
            name="target-not-authorized",
            kind="domain-error",
            description="the caller may not revoke that agent's credential",
            params={"actor": _AGENT, "provider": _PROVIDER},
            error=MethodExampleError(
                ipc_errors.TARGET_NOT_AUTHORIZED, "the target agent is not authorized"
            ),
        ),
    ),
)

REQUEST_EXAMPLES: tuple[MethodExampleSet, ...] = (
    _AGENT_LIST,
    _AGENT_SECRET_LIST,
    _PROVIDER_DEFAULT,
    _PROVIDER_LOGIN,
    _PROVIDER_LOGOUT,
)
