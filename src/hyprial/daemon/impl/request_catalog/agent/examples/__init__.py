"""Same-source Contract Mock cases for the Agent request methods.

Decision record (UA1f, 2026-11-18)
- ``agent.list`` gains a ``user-agent-opt-in`` case after the existing ones,
  so the mock's first success (``one-agent``) and its empty case stay what
  they were; the opt-in row lives in the alice world because it stands for
  a user who created no agents.  ``_USER_AGENT_ROW`` is the shape of a real
  ``includeUserAgent: true`` row: no harness, no cwd, the ``user`` template
  fact, and the two user-agent markers.

Decision record (S2 second-submission merge, 2026-11-02)
- Merged infra-ops/gui-agents-relations-methods @ 5cae74f971: the relations
  example sets (agent.home-census, routine.list, routine.status,
  workflow.list) join REQUEST_EXAMPLES beside the lifecycle slice, so the
  Contract Mock serves all twelve declared methods with examples.

Decision record (implB2, 2026-11-02)
- ``agent.start`` is registered again with its real handler; its example set
  returns to ``REQUEST_EXAMPLES`` with the already-running and
  outcome-unknown refusals that the real start walk produces.

Decision record (codex-daemon-request REWORK of PR #2270, 2026-11-02)
- ``agent.start`` is unregistered (registered empty handler forbidden once in
  a PR), so its example set leaves ``REQUEST_EXAMPLES`` until implB2 registers
  the method with its real handler; the dormant ``_AGENT_START`` set and its
  schema stay in this module for that registration.

Decision record (M-03, 2026-10-31)
- Example data lives beside the declarations, in this one aggregation module,
  rather than in a second method table, a mock-only DTO set or the HTTP layer.
  Each set names an already-declared method; the exporter validates every case
  against that method's own params, result and error schemas.
- The eight sets are the combined M-01/M-02 candidate declarations plus the
  implBc lifecycle slice (``agent.create``/``agent.stop``/``agent.start``).  The
  user's Task A priority release authorizes building this combination candidate; it is
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
- The relations sets (agent.home-census, routine.list, routine.status,
  workflow.list) live in the sibling ``relations`` module (1000-line gate,
  UUID merge into 0.5.5.4); this package still aggregates them.
- UUID addressing lane C (2026-11-03): ``agent.rename`` adds a sixth set.
  The M-03 example gate keeps requiring its original five methods; the new
  set is validated by the same exporter against its own declaration.
"""

from __future__ import annotations

from hyprial.identity import AgentAlreadyRunning, AgentError
from hyprial.kernel import (
    MethodExample,
    MethodExampleError,
    MethodExampleSet,
    ipc_errors,
)

from .relations import RELATIONS_EXAMPLES
from ..destroy.examples import EXAMPLES as DESTROY_EXAMPLES
from ..provider_auth import EXAMPLES as NATIVE_AUTH_EXAMPLES
from ...model.examples import GUI_CONFIG, GUI_OPERATION, GUI_SAVED

__all__ = ["REQUEST_EXAMPLES"]

SYNTHETIC_CREDENTIAL = "example-not-a-credential"
SYNTHETIC_CREDENTIAL_B64 = "ZXhhbXBsZS1ub3QtYS1jcmVkZW50aWFs"
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

_USER_AGENT_URI = "agent:alice:node:a.jdabi7xber55h2fgqxbmhajhw4"

_USER_AGENT_ROW = {
    **_AGENT_ROW,
    "uri": _USER_AGENT_URI,
    "actor": "hyprial-user",
    "owner": "alice",
    "machine": "node",
    "cwd": None,
    "provider": None,
    "capabilities": {"template": "user"},
    "preferredHarness": None,
    "lastHarness": None,
    "agentId": "a.jdabi7xber55h2fgqxbmhajhw4",
    "displayName": None,
    "status": "offline",
    "harness": None,
    "userAgent": True,
    "userActor": "user:alice",
}

_CREATE_AGENT_ROW = {
    key: value
    for key, value in _AGENT_ROW.items()
    if key not in {"activityHints", "lastActiveAtMs"}
}

_MODEL_AGENT_ROW = {**_AGENT_ROW, "agentId": "a.example", "preferredHarness": "pi",
                    "provider": None, "model": "hy4-flash", "harness": None, "lastHarness": None,
                    "status": "offline", "capabilities": {"modelConfigurationRequired": True}}

_DEFAULT_CREDENTIAL = {
    "ownerActor": _AGENT_URI,
    "ownerEntityToken": SYNTHETIC_CREDENTIAL,
    "entryId": "entry-0001",
    "fieldName": "OAUTH",
    "kind": "setup-token",
}

_ALREADY_RUNNING_DATA = {
    "actor": _AGENT_URI,
    "existing": {
        "actor": _AGENT_URI,
        "harness": "pi",
        "runtime": "headless",
        "sessionId": None,
        "boundAtMs": 1760000000000,
    },
    "requestedHarness": _PROVIDER,
    "requestedRuntime": "headless",
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
            name="user-agent-opt-in",
            kind="success",
            description=(
                "alice created no agents; includeUserAgent adds her user agent, "
                "which is never offered a routine"
            ),
            params={"includeUserAgent": True},
            result={
                "ok": True,
                "agents": [_USER_AGENT_ROW],
                "agentsWithoutRoutine": [],
            },
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
        MethodExample(name="manual-model-agent", kind="success", description="Configured identity; not yet started.",
                      params={}, result={"ok": True, "agents": [_MODEL_AGENT_ROW], "agentsWithoutRoutine": []}),
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

_AGENT_CREATE = MethodExampleSet(
    method="agent.create",
    empty_not_applicable=(
        "creation reports created=true or hands back the reused agent; it has "
        "no legal empty result"
    ),
    cases=(
        MethodExample(
            name="new-agent",
            kind="success",
            description="a fresh agent identity is created",
            params={"name": _AGENT},
            result={
                "ok": True,
                "created": True,
                "agent": _CREATE_AGENT_ROW,
                "providerDefault": None,
            },
        ),
        MethodExample(
            name="reused-existing",
            kind="success",
            description='existing="reuse" hands back the registered agent',
            params={"name": _AGENT, "existing": "reuse"},
            result={
                "ok": True,
                "created": False,
                "agent": _CREATE_AGENT_ROW,
                "providerDefault": {
                    "provider": _PROVIDER,
                    "credentialSource": "setup-token",
                    "defaultCredential": _DEFAULT_CREDENTIAL,
                },
            },
        ),
        MethodExample(
            name="historical-name-default",
            kind="success",
            description=(
                "no captured default exists, so the historical provider name "
                "from the legacy store is reported; a caller must not read it "
                "as a captured credential"
            ),
            params={"name": _AGENT},
            result={
                "ok": True,
                "created": True,
                "agent": _CREATE_AGENT_ROW,
                "providerDefault": _PROVIDER,
            },
        ),
        MethodExample(
            name="name-missing",
            kind="invalid-params",
            description="the agent name is required",
            params={"provider": _PROVIDER},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "name must be a non-empty string"
            ),
        ),
        MethodExample(
            name="name-taken",
            kind="semantic-param-error",
            description="the name is already taken on this node",
            params={"name": _AGENT},
            error=MethodExampleError(
                ipc_errors.AGENT_EXISTS,
                "the name 'worker' is already taken on this node",
                data={"actor": _AGENT_URI, "agent": _AGENT},
            ),
        ),
        MethodExample(
            name="already-running",
            kind="semantic-param-error",
            description=(
                "a harness-bound create refuses a served agent; the data "
                "carries the live binding and the request"
            ),
            params={"name": _AGENT, "existing": "reuse", "harness": _PROVIDER},
            error=MethodExampleError(
                AgentAlreadyRunning.code,
                "agent agent:acme:node-1:worker already has a live connector on pi",
                data=_ALREADY_RUNNING_DATA,
            ),
        ),
        MethodExample(name="manual-model-created", kind="success", description="Identity and explicit binding saved; no start.",
                      params={"name": "worker", "preferredHarness": "pi", "modelConfiguration": {
                          "operationId": GUI_OPERATION, "configuration": GUI_CONFIG}},
                      result={"ok": True, "created": True, "agent": {
                          key: value for key, value in _MODEL_AGENT_ROW.items()
                          if key not in {"activityHints", "lastActiveAtMs"}},
                          "providerDefault": None, "modelConfiguration": GUI_SAVED}),
    ),
)

_AGENT_STOP = MethodExampleSet(
    method="agent.stop",
    cases=(
        MethodExample(
            name="running-agent",
            kind="success",
            description="the agent's harness spec is deactivated",
            params={"name": _AGENT},
            result={"ok": True, "removed": [f"{_PROVIDER}:{_AGENT}"]},
        ),
        MethodExample(
            name="nothing-running",
            kind="empty",
            description="the agent has no harness spec in desired state",
            params={"name": _AGENT},
            result={"ok": True, "removed": []},
            reason=(
                "stopping an agent with nothing running is a legal empty "
                "removal, not a failure"
            ),
        ),
        MethodExample(
            name="name-missing",
            kind="invalid-params",
            description="the agent name is required and stop never accepts --all",
            params={},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "name must be a non-empty string"
            ),
        ),
        MethodExample(
            name="stop-unsettled",
            kind="domain-error",
            description="the daemon-side wait for the stop operation elapsed",
            params={"name": _AGENT},
            error=MethodExampleError(
                ipc_errors.LIFECYCLE_OPERATION_UNSETTLED,
                "the daemon-side wait for the stop operation elapsed",
            ),
        ),
        MethodExample(
            name="stop-outcome-unknown",
            kind="domain-error",
            description=(
                "the lifecycle reservation outcome is unknown; the stop may "
                "or may not have taken effect, is never retried, and data "
                "names the operation id to reconcile against lifecycle records"
            ),
            params={"name": _AGENT},
            error=MethodExampleError(
                ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
                "lifecycle reservation outcome is unknown: lifecycle-down:"
                "6f2a1c9d",
                data={"operationId": "lifecycle-down:6f2a1c9d"},
            ),
        ),
        MethodExample(
            name="stop-admission-refused",
            kind="domain-error",
            description="the live lifecycle manager refused the stop's admission",
            params={"name": _AGENT},
            error=MethodExampleError(
                ipc_errors.DAEMON_START_FAILED,
                "lifecycle operation admission: closing",
            ),
        ),
    ),
)

_AGENT_START = MethodExampleSet(
    method="agent.start",
    empty_not_applicable=(
        "a start is either accepted or refused; it has no legal empty result"
    ),
    cases=(
        MethodExample(
            name="accepted-not-running",
            kind="success",
            description=(
                "the daemon accepted the background start; running stays false "
                "until runtime evidence exists"
            ),
            params={"name": _AGENT},
            result={"ok": True, "accepted": True, "running": False},
        ),
        MethodExample(
            name="running-evidenced",
            kind="success",
            description="the start was accepted and the runtime is observed running",
            params={"name": _AGENT, "harness": _PROVIDER},
            result={"ok": True, "accepted": True, "running": True},
        ),
        MethodExample(
            name="name-missing",
            kind="invalid-params",
            description="the agent name is required",
            params={"harness": _PROVIDER},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "name must be a non-empty string"
            ),
        ),
        MethodExample(
            name="already-running",
            kind="domain-error",
            description="a live connector is already serving the agent; starting twice is refused, never a silent no-op",
            params={"name": _AGENT},
            error=MethodExampleError(
                AgentAlreadyRunning.code,
                "agent is already being served",
                data={
                    "actor": "agent:example-owner:example-node:alpha",
                    "existing": {
                        "actor": "agent:example-owner:example-node:alpha",
                        "harness": _PROVIDER,
                        "runtime": "headless",
                        "sessionId": None,
                        "boundAtMs": 1760000000000,
                    },
                    "requestedHarness": _PROVIDER,
                    "requestedRuntime": "headless",
                },
            ),
        ),
        MethodExample(
            name="outcome-unknown",
            kind="domain-error",
            description="the lifecycle reservation outcome is unknown; the start is never retried and data names the reconciliation id",
            params={"name": _AGENT},
            error=MethodExampleError(
                ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
                "lifecycle reservation outcome is unknown: lifecycle-down:example",
                data={"operationId": "lifecycle-start:example"},
            ),
        ),
    ),
)

_RENAMED_ROW = {
    key: value
    for key, value in _AGENT_ROW.items()
    if key not in {"activityHints", "lastActiveAtMs"}
} | {"agentId": "a.aaaaaaaaaaaaaaaaaaaaaaaaaa", "displayName": "reviewer"}

_AGENT_RENAME = MethodExampleSet(
    method="agent.rename",
    empty_not_applicable=(
        "rename either answers the renamed agent row or fails; it has no "
        "legal empty result"
    ),
    cases=(
        MethodExample(
            name="renamed",
            kind="success",
            description="the agent keeps its actor and id and shows the new name",
            params={"name": _AGENT, "displayName": "reviewer"},
            result={"ok": True, "agent": _RENAMED_ROW},
        ),
        MethodExample(
            name="missing-display-name",
            kind="invalid-params",
            description="the new display name is required",
            params={"name": _AGENT},
            error=MethodExampleError(
                ipc_errors.INVALID_ARGUMENT, "displayName must be a non-empty string"
            ),
        ),
        MethodExample(
            name="name-taken",
            kind="domain-error",
            description="another live agent already uses the name",
            params={"name": _AGENT, "displayName": "builder"},
            error=MethodExampleError(
                ipc_errors.AGENT_EXISTS,
                "the name 'builder' is already taken by agent 'builder' on this node",
            ),
        ),
    ),
)

RESOLVE_EXAMPLES = MethodExampleSet(
    method="agent.resolve",
    empty_not_applicable="Every resolution returns a lookup result, including unknown names.",
    cases=(
        MethodExample(name="alias", kind="success", description="A local alias resolves to its agent URI.",
            params={"name": "alpha"}, result={"ok": True, "input": "alpha", "resolved": "agent:op:node-1:alpha", "known": True, "reason": "alias"}),
        MethodExample(name="unknown", kind="success", description="An unknown bare name remains a successful lookup.",
            params={"name": "missing"}, result={"ok": True, "input": "missing", "resolved": "missing", "known": False, "reason": "unknown"}),
        MethodExample(name="non-object", kind="invalid-params", description="IPC params must be an object.",
            params=[], error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "params must be an object")),
        MethodExample(name="missing-name", kind="invalid-params", description="The legacy handler validates the name.",
            params={}, error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "name must be a non-empty string")),
        MethodExample(name="ambiguous", kind="domain-error", description="Conflicting owners require a canonical URI.",
            params={"name": "moved"}, error=MethodExampleError(ipc_errors.AMBIGUOUS_TARGET,
                "actor name 'moved' matches more than one agent; address the canonical URI directly",
                data={"target": "moved", "candidates": ["agent:op:node-1:moved", "agent:someone-else:node-1:moved"]})),
    ),
)

REQUEST_EXAMPLES: tuple[MethodExampleSet, ...] = (
    _AGENT_LIST, RESOLVE_EXAMPLES, _AGENT_SECRET_LIST, _AGENT_CREATE, _AGENT_STOP, _AGENT_START,
    *RELATIONS_EXAMPLES,
    _PROVIDER_DEFAULT, _PROVIDER_LOGIN, _PROVIDER_LOGOUT, _AGENT_RENAME,
    NATIVE_AUTH_EXAMPLES, *DESTROY_EXAMPLES,
)
