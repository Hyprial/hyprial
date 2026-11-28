"""Terminology lint (design §4-2): actual src/hyprial sources may only
hit whitelisted wire keys / wire-format prose or the explicit model-vendor
vocabulary shared by Squire, Agent and harness launch selection, plus the typed
external-adapter provider vocabulary. Any new hit
fails here, preventing regressions of the provider -> harness rename.

Owner-held provider keys (#2195, contract freeze r3, OC4 owner_store): the
provider_keys modules, the local owner-store path router (`org/membership/owner_store.py`
routing `directory/provider-keys` / `directory/provider-grants` to `provider_keys.storage.local`),
the `user provider` CLI module and `agent provider` access module use "provider"
only as the model vendor whose credential a key wraps or as the frozen wire names
user.provider.* / agent.provider.*; other files may only name that package in an
import line, and the composition root is pinned to its exact registration lines.
The migration registry allowance covers only frozen provider wire names as exact
source lines, including their actual quoting; it does not authorize new provider terms.

Native Auth inventory adds the user-approved agent.provider.auth.list wire
name alongside the existing login/logout surface. Its declaration, example,
registration and imports are admitted as exact lines, never a package bypass.

Startup failure observations compare the saved and current model-vendor values.
The snapshot comparison is admitted as one exact line; it does not rename a harness.

The temporary credential test names the existing Secret model-vendor family.
Only its exact method declaration and migration registration are admitted.
Its existing wire name also selects cancellation and deadline policy at four
transport call sites. These exact lines do not introduce a new vendor concept.

First-run snapshots retain the selected Agent model vendor independently of
the harness. Only the exact capture, typed field and equality-check lines are
admitted; unrelated paths or harness-as-provider assignments remain rejected.
The agy ACP driver rule admits the four agy modules' model-vendor
vocabulary (model_provider / provider_environment / provider_error and the
vendor prose) and the two exact runtime/launcher.py lines importing and calling
agy_provider_environment from model_provider: provider there names the model
vendor behind the frozen launch-spec field, never a renamed harness (same
class as model_routes and streaming).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from tests.tooling.architecture.lint.adapter_source_terms import ADAPTER_SOURCE_PROVIDER_LINES
from tests.tooling.architecture.lint.pi_model_connection_terms import PI_MODEL_CONNECTION_LINES

REPO_ROOT = Path(__file__).resolve().parents[4]

# Two active meanings: model vendor and adapter delivery platform (e.g. lark;
# published in adapter list/status). Legacy wire spellings remain separately
# classified below. Adapter entries are exact paths and complete literal lines:
# no directory, wildcard or whole-file allowance (owner approval ccaf6019).
_ADAPTER_PROVIDER_LINES: tuple[tuple[str, str], ...] = (
    ('src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py', '        names = tuple(sorted(spec.name for spec in specs if spec.provider == "lark"))'),
    ('src/hyprial/daemon/impl/adapters/spec.py', 'def is_adapter_provider(value: object) -> bool:'),
    ('src/hyprial/daemon/impl/adapters/spec.py', '    provider: str'),
    ('src/hyprial/daemon/impl/adapters/spec.py', '        if not is_adapter_provider(self.provider):'),
    ('src/hyprial/daemon/impl/adapters/spec.py', '            raise DesiredStateError("adapter has an unsupported provider")'),
    ('src/hyprial/daemon/impl/adapters/storage.py', '    provider TEXT NOT NULL,'),
    ('src/hyprial/daemon/impl/adapters/storage.py', '        for row in db.execute("SELECT provider,name,status,legacy_launch_json FROM adapters ORDER BY rowid")'),
    ('src/hyprial/daemon/impl/adapters/storage.py', '        "INSERT INTO adapters(provider,name,status,legacy_launch_json) VALUES(?,?,?,?)",'),
    ('src/hyprial/daemon/impl/adapters/storage.py', '                       [(spec.provider, spec.name) for spec in state.adapters])'),
    ('src/hyprial/daemon/impl/application/actors/agents_registry.py', 'from hyprial.daemon.impl.adapters.spec import is_adapter_provider'),
    ('src/hyprial/daemon/impl/application/actors/agents_registry.py', '            if status.get("adapter") is not None or is_adapter_provider(status.get("runtime")):'),
    ('src/hyprial/daemon/impl/application/actors/lifecycle_ops.py', 'from hyprial.daemon.impl.adapters.spec import is_adapter_provider'),
    ('src/hyprial/daemon/impl/application/actors/lifecycle_ops.py', '        if is_adapter_provider(harness):'),
    ('src/hyprial/daemon/impl/application/actors/lifecycle_ops.py', '                    removed.append(f"{adapter.provider}:{adapter.name}")'),
    ('src/hyprial/daemon/impl/application/actors/lifecycle_ops.py', '                    if ((harness is None and target in {adapter.name, f"{adapter.provider}:{adapter.name}"})'),
    ('src/hyprial/daemon/impl/application/actors/lifecycle_ops.py', '                            or (harness == adapter.provider and target == adapter.name)):'),
    ('src/hyprial/daemon/impl/application/actors/lifecycle_ops.py', '                            removed.append(f"{adapter.provider}:{adapter.name}")'),
    ('src/hyprial/daemon/impl/application/actors/lifecycle_ops.py', '            if not removed and not matched_adapter and not is_adapter_provider(harness):'),
    ('src/hyprial/daemon/impl/application/actors/agent_start/__init__.py', 'from hyprial.daemon.impl.adapters.spec import is_adapter_provider'),
    ('src/hyprial/daemon/impl/application/actors/agent_start/__init__.py', '        if is_adapter_provider(harness):'),
    ('src/hyprial/daemon/impl/application/messaging/status/snapshots.py', 'from hyprial.daemon.impl.adapters.spec import is_adapter_provider'),
    ('src/hyprial/daemon/impl/application/messaging/status/snapshots.py', '            if status.get("adapter") is not None or is_adapter_provider(status.get("runtime")):'),
)

# Published ``agent.provider.*`` wire methods and their legacy handler entry.
# These exact lines are path-scoped to the M-02 declaration module; they name
# the existing wire vocabulary, not a renamed harness or a model-vendor value.
_AGENT_PROVIDER_WIRE_LINES: tuple[str, ...] = (
    '"""``agent.provider.*`` declarations over the existing provider handler.',
    'Decision record (M-02, 2026-10-31): the three provider methods stay one wire',
    'method each.  ``agent.provider.default`` keeps its read, legacy provider-write',
    '``agent.provider.login`` is the existing credential installation surface, not',
    'a new provider OAuth flow.  The CLI may read a host login file or a setup token',
    'documents or token and never returns them.  ``agent.provider.logout`` removes',
    "only the current agent's provider files and keeps the upstream-token semantics",
    'owned by the provider CLI.',
    'handler accepts ``provider: null``, ``clear: null``/``"yes"``, unknown keys,',
    'clears the legacy provider-name file; a truthy non-boolean ``clear`` still',
    'structural and leaves ``provider`` requiredness to the handler; this preserves',
    'provider.  Unknown keys remain ignored.',
    'AGENT_PROVIDER_DEFAULT = "agent.provider.default"',
    'AGENT_PROVIDER_LOGIN = "agent.provider.login"',
    'AGENT_PROVIDER_LOGOUT = "agent.provider.logout"',
    '        "provider": _NULLABLE_STRING,',
    '    "required": ["ok", "provider", "previous"],',
    '        "provider": {',
    '                "Legacy provider target: a present non-null value is validated "',
    '                "Exact boolean true clears the legacy provider-name file. A "',
    '        "provider": {',
    '        "provider": _STRING,',
    '    "required": ["ok", "actor", "provider"],',
    '        "provider": {',
    '        "provider": _STRING,',
    '    "required": ["ok", "actor", "provider", "removed", "absent"],',
    '    idempotency=IdempotencySpec(mode="never", scope="provider-default-read-write"),',
    '    idempotency=IdempotencySpec(mode="never", scope="provider-credential-install-awaits-H-03-write-admission"),',
    '    idempotency=IdempotencySpec(mode="never", scope="provider-credential-revoke-awaits-H-03-write-admission"),',
    'def provider_default(',
    '    return app._handle_agent_provider_request(',
    'def provider_login(',
    '    return app._handle_agent_provider_request(',
    'def provider_logout(',
    '    return app._handle_agent_provider_request(',
    '    if field in {"actor", "provider", "filename", "contentBase64"} and validator in {',
    '        provider_default,',
    '        provider_login,',
    '        provider_logout,',
    '    "provider_default",',
    '    "provider_login",',
    '    "provider_logout",',
)

_M03_AGENT_PROVIDER_WIRE_LINES: tuple[str, ...] = (
    '                "no captured default exists, so the historical provider name "',
    '- Provider-default and provider-login deliberately accept every JSON object at',
    '- Every value is synthetic: provider and agent names are illustrative, the',
    '    method="agent.provider.default",',
    '            description="a provider is the current default",',
    '            description="no default provider has been configured",',
    '            reason="a node with no default provider is a legal unconfigured state",',
    '            description="the handler validates a schema-admitted provider value",',
    '            description="the named provider is not supported on this machine",',
    '                ipc_errors.PROVIDER_UNSUPPORTED, "the provider is not supported on this machine"',
    '    method="agent.provider.login",',
    '            description="one synthetic provider file is installed",',
    '    method="agent.provider.logout",',
    '            description="the agent\'s synthetic provider file is removed",',
    '            description="the agent has no provider file to remove",',
    '            description="the handler requires a provider after the actor",',
)

# GUI v1 agents line (agent.create/stop/start specs): the docstring and wire
# descriptions name the agent.provider.* slice and its providerDefault result key.
_AGENT_LIFECYCLE_PROVIDER_LINES: tuple[str, ...] = (
    '  M-01 reads and the M-02 provider slice; the ``agent`` family stays open, so',
    '  accepts it, including the ``agent.provider.default`` merge wrapper: the',
    '  ``providerDefault`` is part of every successful create result.  Legacy value',
    '- All three share the write profile of the provider slice: ``mutates=True``,',
    '  real ``providerDefault`` branches (null, captured Claude default object with',
    '  ``provider``/``credentialSource``/``defaultCredential``, historical',
    '  provider-name string).  ``capabilities`` and ``harnessArgs`` are declared in',
    '                        "Historical provider-name default from the legacy "',
    '                "Present only when the provider-default read failed; the "',
    '  still leaves the requested provider/model and runtime-capability updates',
)

_AGENT_LIFECYCLE_OPS_PROVIDER_LINES: tuple[str, ...] = (
    '  provider/model and runtime-capability updates applied.  The cwd/command',
    '        if requested_provider is not None and harness != "jev":',
)

_RELATIONS_PROVIDER_LINES: tuple[str, ...] = (
    '  {tier, harness, provider, model, cwd, cwdPolicy, args}; tier mode adds',
    '  tier plus the resolved harness/provider/model, explicit mode keeps',
    '        "key set: tier mode adds tier plus the resolved harness/provider/model "',
    '        "(the resolved provider may be null), explicit mode keeps harness "',
    '        "(provider/model optional); cwd/cwdPolicy only when declared; args is "',
)

# Existing secret wire name/source vocabulary, not harness aliases.
_SETUP_SECRET_WIRE_LINES = (
    'SECRET_PROVIDER_WRITE = "agent.secret.provider-write"',
    'SECRET_PROVIDER_LIST = "agent.secret.provider-list"',
    '     "source": {"type": "string", "enum": ["user-provider", "agent-private", "agent-shared"]},',
    '    "actor": "squire", "grantId": "example-model", "source": "user-provider",',
)

# Each rule is (path regex, line-content regex). A hit is allowed when at
# least one rule matches both the path and the matched line content.
_ADAPTER_PROVIDER_LINES += ADAPTER_SOURCE_PROVIDER_LINES

_NATIVE_AUTH_WIRE_LINES: tuple[tuple[str, str], ...] = (
    ("agent/provider_auth/__init__.py", "Imports and removal retain agent.provider.login/logout. No timestamp or launch"),
    ("agent/provider_auth/__init__.py", '    name="agent.provider.auth.list", owner="daemon.agents",'),
    ("agent/provider_auth/__init__.py", '    method="agent.provider.auth.list",'),
    ("agent/examples/__init__.py", "from ..provider_auth import EXAMPLES as NATIVE_AUTH_EXAMPLES"),
    ("registry.py", "    from .agent.provider_auth import BINDINGS as NATIVE_AUTH_BINDINGS"),
    ("migration.py", "        'agent.provider.auth.list',"),
)

_MODEL_ROUTE_LINES = (
    ("daemon/impl/harnesses/model_routes/__init__.py", "from ..model_provider import DEEPSEEK_API_KEY_ENV, DEEPSEEK_MODEL, DEEPSEEK_PROVIDER, OPENAI_BASE_URL_ENV"),
    ("daemon/impl/harnesses/model_routes/__init__.py", '    if (spec.harness != "pi" or spec.model_provider != DEEPSEEK_PROVIDER'),
    ("daemon/impl/harnesses/model_routes/__init__.py", '            or route.vendor_id != spec.model_provider or route.protocol != "openai-completions"):'),
    ("daemon/impl/request_catalog/migration.py", "        'agent.secret.provider-configure',"),
    ("daemon/impl/request_catalog/setup/routes.py", 'METHOD = "agent.secret.provider-configure"'),
    ("identity/impl/agents/secret_store/defaults.py", '            "CHECK (route_required IN (0,1) AND (route_required=0 OR source=\'user-provider\'))"'),
)

_FIRST_RUN_MODEL_VENDOR_LINES = (
    ('daemon/impl/application/onboarding/first_run/selection.py', '            provider=agent.provider,'),
    ('daemon/impl/application/onboarding/first_run/selection.py', '            or selection.provider != attempt.provider'),
    ('daemon/impl/application/onboarding/first_run/service.py', '    provider: str | None'),
    ('daemon/impl/application/onboarding/first_run/service.py', '                    provider=selection.provider,'),
    ('daemon/impl/application/onboarding/first_run/service.py', '        provider=source.provider,'),
    ('daemon/impl/onboarding/first_run.py', '    provider: str | None'),
    ('daemon/impl/onboarding/first_run_attempt.py', '    provider: str | None'),
    ('daemon/impl/onboarding/first_run_attempt.py', '                provider=selection.provider,'),
    ('daemon/impl/onboarding/first_run_attempt.py', '            and attempt.provider == selection.provider'),
)

_MODEL_ROUTE_LINES += PI_MODEL_CONNECTION_LINES

_RULES: tuple[tuple[str, str], ...] = (
    *((f"^{re.escape('src/hyprial/' + path)}$", f"^{re.escape(line)}$") for path, line in _FIRST_RUN_MODEL_VENDOR_LINES),
    *((f"^{re.escape('src/hyprial/' + path)}$", f"^{re.escape(line)}$") for path, line in _MODEL_ROUTE_LINES),
    # The type-only facade keeps the existing public ProviderKeys alias after
    # interface.py was split to meet the structural line limit.
    (
        r"^src/hyprial/identity/impl/facade/types\.py$",
        r"^\s*from hyprial\.identity\.impl\.provider_keys import ProviderKeys as ProviderKeys$",
    ),
    *((r"^src/hyprial/daemon/impl/request_catalog/setup/specs\.py$", f"^{re.escape(line)}$") for line in _SETUP_SECRET_WIRE_LINES),
    (r"^src/hyprial/daemon/impl/request_catalog/setup/probe\.py$",
     r'^METHOD = "agent\.secret\.provider-test"$'),
    (r"^src/hyprial/daemon/impl/cells/observability/model\.py$", r"^    provider_block: bool = False$"),
    (r"^src/hyprial/daemon/impl/cells/observability/reducer\.py$", r"^        elif facts\.provider_block:$"),
    (r"^src/hyprial/daemon/impl/composition/cell_observation/run_states\.py$", r"^\s+provider_block=bool\(blocks\.get\(entity\.uri, entity\.block is not None\)\)\)$"),
    (r"^src/hyprial/shell/impl/cli/output/status/__init__\.py$", r"^\s+(?:# A diagnostic label must never become a raw prompt/provider error renderer\.|text \+= \" provider-blocked=true\")$"),
    *((f"^{re.escape(path)}$", f"^{re.escape(line)}$") for path, line in _ADAPTER_PROVIDER_LINES),
    *((f"^{re.escape('src/hyprial/daemon/impl/request_catalog/' + path)}$", f"^{re.escape(line)}$") for path, line in _NATIVE_AUTH_WIRE_LINES),
    *(
        (r"^src/hyprial/daemon/impl/request_catalog/agent/provider\.py$", f"^{re.escape(line)}$")
        for line in _AGENT_PROVIDER_WIRE_LINES
    ),
    *(
        (
            r"^src/hyprial/daemon/impl/request_catalog/agent/examples/__init__\.py$",
            f"^{re.escape(line)}$",
        )
        for line in _M03_AGENT_PROVIDER_WIRE_LINES
    ),
    *(
        (r"^src/hyprial/daemon/impl/request_catalog/agent/lifecycle\.py$", f"^{re.escape(line)}$")
        for line in _AGENT_LIFECYCLE_PROVIDER_LINES
    ),
    *(
        (r"^src/hyprial/daemon/impl/application/actors/agent_start/__init__\.py$", f"^{re.escape(line)}$")
        for line in _AGENT_LIFECYCLE_OPS_PROVIDER_LINES
    ),
    *(
        (r"^src/hyprial/daemon/impl/request_catalog/agent/relations\.py$", f"^{re.escape(line)}$")
        for line in _RELATIONS_PROVIDER_LINES
    ),
    (
        r"^src/hyprial/daemon/impl/request_catalog/registry\.py$",
        r"^    from \.agent.provider import AGENT_PROVIDER_BINDINGS$",
    ),
    (
        r"^src/hyprial/daemon/impl/request_catalog/migration\.py$",
        r"^        '(?:agent\.provider\.(?:allow|revoke|status|default|login|logout)"
        r"|agent\.secret\.provider-(?:write|list|test)|user\.provider\.(?:list|login|logout|rotate))',$",
    ),
    (r"^src/hyprial/daemon/impl/user_agent/admin\.py$", r'^    "agent\.provider\.(?:login|logout)",$'),
    (r"^src/hyprial/daemon/impl/user_agent/admin\.py$", r'^    "agent\.provider\.revoke",$'),
    (
        r"^src/hyprial/shell/impl/cli/commands/user/key\.py$",
        r"^from hyprial\.shell\.impl\.cli\.commands\.agent\.provider\.commands import refuse_owner_key_in_worker_shell$",
    ),
    # Preserve the existing host credential filename in the export exclusion
    # set. This exact on-disk name must not be renamed or exported.
    (r"^(?:src/hyprial/daemon/impl/transfer/archive/bundle/_base\.py)$", r'^\s*"user-provider\.json",\s*$'),
    # The archive envelope's docstring names the same legacy on-disk
    # ``user-provider`` entry (never a harness nickname).
    (r"^(?:src/hyprial/daemon/impl/transfer/archive/envelope\.py)$", r"user-provider"),
    # The controlled Pi loader reads the provider/model a session recorded
    # (the pi SDK's own entry shape) and passes the explicit model-vendor
    # selection through pi's modelProvider field; "provider" here is the
    # model vendor, never a renamed harness.
    (
        r"^(?:src/hyprial/daemon/impl/harnesses/pi/resources/pi_controlled_loader\.mjs)$",
        r"recordedProvider|configuredProvider|session\.provider\.rebound|\.provider"
        r"|provider/model|provider calls|provider: \{ maxRetries",
    ),
    # The pi stderr reader mirrors that same model-vendor rebind event from the
    # loader; "provider" names the vendor a session was rebound to, never a
    # renamed harness.
    (
        r"^(?:src/hyprial/daemon/impl/harnesses/pi/stderr_warnings\.py)$",
        r"session's provider|session/provider/model|session\.provider\.rebound",
    ),
    # The daemon lifecycle launch preparation consumes the one model-vendor
    # effort codec; `provider` here is the module name, not a renamed harness.
    (
        r"^(?:src/hyprial/daemon/impl/application/actors/lifecycle_ops\.py)$",
        re.escape("from hyprial.daemon.impl.harnesses.model_provider import ("),
    ),
    # User-owned provider keys (#2195): "provider" is the model vendor whose
    # credential the key wraps -- the model-vendor meaning, never a renamed
    # harness. Domain strings and purposes are frozen wire values (R1 v1).
    (
        r"^(?:src/hyprial/identity/impl/provider_keys/(?:__init__|models|ports)\.py)$",
        r"provider-key|def read_grant\(self, agent: str, provider: str\)",
    ),
    (
        r"^(?:src/hyprial/identity/impl/provider_keys/records\.py"
        r"|src/hyprial/daemon/impl/provider_keys/(?:[a-z_]+/)?[a-z_]+\.py"
        r"|src/hyprial/shell/impl/cli/commands/user/provider\.py"
        r"|src/hyprial/shell/impl/cli/commands/agent/provider/access\.py)$",
        r"provider",
    ),
    (
        r"^src/hyprial/[a-z_/]+\.py$",
        r"^\s*from hyprial\.daemon\.impl\.provider_keys(?:\.[a-z_]+)* import [A-Za-z_, ()]+$",
    ),
    (
        r"^src/hyprial/daemon/impl/org/membership/owner_store\.py$",
        r"(?:provider_keys\.storage\.local|directory/provider-(?:keys|grants))",
    ),
    # The user-key docstrings name what they are NOT: the provider envelope,
    # grant, wrap and wire formats owned by provider_keys above.
    (
        r"^(?:src/hyprial/identity/impl/user_key/(?:crypto|purposes|encryption/(?:crypto|models|store))\.py)$",
        r"provider (?:envelope|wrap|wire|rewrap)",
    ),
    (r"^(?:src/hyprial/error/codes\.py)$", r"User-owned provider envelopes and grants"),
    # Planning validates the model with the launch's own validate_model_selection (#1224).
    # Planning validates the model with the launch's own validate_model_selection (#1224).
    (r"^(?:src/hyprial/daemon/impl/pac/contracts/workflow\.py)$", r"--provider|model_provider=|model_providers\.|harnesses\.model_provider import"),
    # The launch-args merge keeps the model-vendor CLI option name (--provider)
    # as an option spelling; this is the vendor flag, not a renamed harness.
    (r"^(?:src/hyprial/kernel/impl/facts/launch_spec/args\.py)$", r'"--provider": \("--provider", True\),'),
    # Workflow launch declarations use provider strictly as a model vendor,
    # separately from the harness field; static matrix selection preserves it.
    (r"^(?:src/hyprial/daemon/impl/pac/contracts/workflow\.py)$", r"choose tier OR explicit harness/provider/model|harness=choice.harness, provider=choice.provider, model=choice.model"),
    # `agent tiers probe`（plan F2）把 harness:provider:model 组合作为显式探测
    # 入参并在 tiers 视图中原样展示——这里的 provider 是模型厂商段，不是被
    # 改名的 harness（plan F2，2026-10-10）。
    (r"^(?:src/hyprial/shell/impl/cli/commands/agent/runtime/tiers\.py)$", r"provider"),
    # squire: "provider" is the correct term for a model vendor.
    (r"^(?:src/hyprial/daemon/impl/adapters/user_delivery/__init__\.py|src/hyprial/daemon/impl/adapters/user_delivery/actor\.py|src/hyprial/daemon/impl/adapters/user_delivery/ledger_actor\.py|src/hyprial/daemon/impl/adapters/user_delivery/receiver\.py|src/hyprial/daemon/impl/harnesses/probe\.py|src/hyprial/identity/impl/users/runtime_capabilities\.py)$", r"provider"),
    # provider_auth: the whole package is model-vendor OAuth terminology
    # (kimi-coding/openai-codex credential relogin), the same vocabulary
    # class as squire's — no harness is renamed here.
    (r"^(?:src/hyprial/identity/interface\.py|src/hyprial/identity/impl/provider_auth/__init__\.py|src/hyprial/identity/impl/provider_auth/actor/__init__\.py|src/hyprial/identity/impl/provider_auth/actor/_authority\.py|src/hyprial/identity/impl/provider_auth/actor/_base\.py|src/hyprial/identity/impl/provider_auth/actor/_episode\.py|src/hyprial/identity/impl/provider_auth/classification\.py|src/hyprial/identity/impl/provider_auth/coordinator/__init__\.py|src/hyprial/identity/impl/provider_auth/coordinator/_base\.py|src/hyprial/identity/impl/provider_auth/coordinator/_coordinator\.py|src/hyprial/identity/impl/provider_auth/helper\.py|src/hyprial/identity/impl/provider_auth/pi_device_login\.mjs)$", r"provider"),
    # availability_loud: this module exists to name the model-vendor
    # availability policy and its notification vocabulary; "provider" is the
    # subject of the policy, not a renamed harness.
    (r"^(?:src/hyprial/daemon/impl/correlation/availability_loud\.py)$", r"provider"),
    # Fail-loud wiring names the retained owner channel and explains why the
    # raw vendor error text is not copied into a sender-facing notice.
    (
        r"^(?:src/hyprial/daemon/impl/runtime/forwards\.py)$",
        r"provider-auth alerts|raw provider text",
    ),
    # PAC says in prose that a diagnostic mark no longer precedes launch.
    (r"^(?:src/hyprial/daemon/impl/pac/actors/coordinator/types\.py)$", r"marked provider is still launched"),
    # The harness seams that carry the provider_auth observer name it in
    # comments/signatures; the observer is model-vendor failure handling.
    (r"^(?:src/hyprial/daemon/impl/harnesses/runtime/launcher\.py|src/hyprial/daemon/impl/harnesses/streaming/failure\.py|src/hyprial/daemon/impl/harnesses/streaming/process\.py|src/hyprial/daemon/impl/harnesses/streaming/protocol\.py|src/hyprial/daemon/impl/harnesses/pi/rpc_process\.py|src/hyprial/daemon/impl/harnesses/pi/rpc_client\.py|src/hyprial/daemon/impl/harnesses/pi/messages\.py)$", r"provider_auth"),
    # streaming.py hosts the observer Protocol, whose fields use the squire
    # model-vendor vocabulary (provider/model).
    (r"^(?:src/hyprial/daemon/impl/harnesses/streaming/protocol\.py|src/hyprial/daemon/impl/harnesses/streaming/process\.py)$", r"provider/model/name|provider: str \| None"),
    (
        r"^(?:src/hyprial/daemon/impl/harnesses/agy/(?:acp_client|process|projection|protocol)\.py)$",
        r"model_provider|provider_environment|provider_error|model provider|agy provider",
    ),
    (
        r"^src/hyprial/daemon/impl/harnesses/runtime/launcher\.py$",
        r"^(?:from hyprial\.daemon\.impl\.harnesses\.model_provider import agy_provider_environment|        agy_provider_environment\()$",
    ),
    # daemon composition wires the provider_auth coordinator.
    (r"^(?:src/hyprial/daemon/impl/application/__init__\.py|src/hyprial/daemon/impl/application/restore\.py|src/hyprial/daemon/impl/application/shutdown\.py|src/hyprial/daemon/impl/application/startup\.py|src/hyprial/daemon/impl/application/wiring/transport\.py|src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops\.py|src/hyprial/daemon/impl/application/actors/agents_registry\.py|src/hyprial/daemon/impl/application/actors/lifecycle_ops\.py|src/hyprial/daemon/impl/application/actors/agent_start/__init__\.py|src/hyprial/daemon/impl/application/actors/routines\.py|src/hyprial/daemon/impl/application/actors/sessions/registry\.py|src/hyprial/daemon/impl/application/actors/transfer\.py|src/hyprial/daemon/impl/application/messaging/visibility\.py|src/hyprial/daemon/impl/application/messaging/inbox_surface/queries\.py|src/hyprial/daemon/impl/application/messaging/inbox_surface/send\.py)$", r"provider_auth|provider\.auth\.|provider-auth"),
    # The same startup phase name, moved into the shared phase enum (#26); only
    # that one member line, whose value the daemon emits unchanged.
    (r"^(?:src/hyprial/kernel/impl/contracts/daemon/daemon_diagnostics\.py)$", r'^\s*RESTORE_PROVIDER_AUTH = "restore-provider-auth"$'),
    # alert.py's shared notify_owner docstring names the provider-auth
    # relogin feature as its first reuse caller.
    (r"^src/hyprial/daemon/impl/autoupdate/alert\.py$", r"provider-auth"),
    # CAPMX matrix uses the exact Squire harness/vendor/model billing key.
    (r"^(?:src/hyprial/daemon/impl/dispatch/matrix\.py|src/hyprial/kernel/impl/facts/tier_vocab\.py)$", r"provider"),
    # Runtime facts on entities: narrowly allow the model-vendor plumbing,
    # not a return to calling a harness a provider.
    (
        r"^(?:src/hyprial/daemon/impl/application/__init__\.py|src/hyprial/daemon/impl/application/restore\.py|src/hyprial/daemon/impl/application/shutdown\.py|src/hyprial/daemon/impl/application/startup\.py|src/hyprial/daemon/impl/application/wiring/transport\.py|src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops\.py|src/hyprial/daemon/impl/application/actors/agents_registry\.py|src/hyprial/daemon/impl/application/actors/lifecycle_ops\.py|src/hyprial/daemon/impl/application/actors/agent_start/__init__\.py|src/hyprial/daemon/impl/application/actors/routines\.py|src/hyprial/daemon/impl/application/actors/sessions/registry\.py|src/hyprial/daemon/impl/application/actors/transfer\.py|src/hyprial/daemon/impl/application/messaging/visibility\.py|src/hyprial/daemon/impl/application/messaging/inbox_surface/queries\.py|src/hyprial/daemon/impl/application/messaging/inbox_surface/send\.py)$",
        r"provider=(?:launch|spec)\.model_provider|provider=option_value"
        r"|existing_agent\.provider|provider: str \| None = None"
        r"|interactive=interactive, provider=provider",
    ),
    # Guarded resume inherits an omitted model-vendor selection only when the
    # Agent remains on the same harness. These exact lines preserve that field;
    # they do not use provider as a synonym for harness.
    (
        r"^src/hyprial/daemon/impl/application/actors/agents_registry\.py$",
        r"effective_provider|^\s*provider\s*$|if provider is not None"
        r"|agent\.provider if same_harness|provider=effective_provider",
    ),
    # The message-origin projection carries the sender's provider-reported
    # origin as the "providerMetadata" wire value carried by adapter
    # deliveries; the prose keeps "provider said nothing" distinct from "no
    # record at all" and explains why the field stays provider-scoped.
    (
        r"^(?:src/hyprial/daemon/impl/application/__init__\.py|src/hyprial/daemon/impl/application/restore\.py|src/hyprial/daemon/impl/application/shutdown\.py|src/hyprial/daemon/impl/application/startup\.py|src/hyprial/daemon/impl/application/wiring/transport\.py|src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops\.py|src/hyprial/daemon/impl/application/actors/agents_registry\.py|src/hyprial/daemon/impl/application/actors/lifecycle_ops\.py|src/hyprial/daemon/impl/application/actors/agent_start/__init__\.py|src/hyprial/daemon/impl/application/actors/routines\.py|src/hyprial/daemon/impl/application/actors/sessions/registry\.py|src/hyprial/daemon/impl/application/actors/transfer\.py|src/hyprial/daemon/impl/application/messaging/visibility\.py|src/hyprial/daemon/impl/application/messaging/inbox_surface/queries\.py|src/hyprial/daemon/impl/application/messaging/inbox_surface/send\.py)$",
        r"sender's provider reported|as its provider reported|providerMetadata"
        r"|provider reports no origin|provider-reported origin|provider said nothing"
        r"|provider-scoped|isinstance\(provider, str\) and provider",
    ),
    # Wire keys and wire-format strings ("provider"/"providers"/
    # "providerMetadata"/f"providers[...]"), including comments and error
    # prose that quote the key. On-disk/IPC/CLI-output keys stay at
    # schemaVersion=1 until the schema v2 cutover.
    (r"^src/", r'"provider'),
    # The provider_metadata field is coupled to the "providerMetadata" wire
    # key carried by adapter deliveries.
    (r"^(?:src/hyprial/daemon/impl/adapters/lark/contracts/wire\.py|src/hyprial/daemon/impl/adapters/lark/inbound/pipeline\.py|src/hyprial/daemon/impl/adapters/lark/inbound/reconcile\.py|src/hyprial/daemon/impl/adapters/lark/runtime/statuses\.py|src/hyprial/daemon/impl/adapters/lark/ports/ports\.py|src/hyprial/daemon/impl/adapters/lark/worker/daemon_link\.py|src/hyprial/daemon/impl/adapters/lark/state/recovery\.py|src/hyprial/daemon/impl/adapters/lark/state/store\.py)$", r"provider_metadata"),
    # squire probe CLI surface in cli.py: harness:provider:model combos are
    # model-vendor terminology.
    (
        r"^(?:src/hyprial/shell/impl/cli/commands/agent/runtime/start\.py|src/hyprial/shell/impl/cli/commands/agent/(?:admin|create|grant|home|restore|secret)\.py|src/hyprial/shell/impl/cli/commands/agent/runtime/claude\.py|src/hyprial/shell/impl/cli/commands/agent/runtime/sessions\.py|src/hyprial/shell/impl/cli/commands/login/commands\.py)$",
        r"harness:provider:model|provider, model = parts|provider or None"
        r"|capability\.provider|harness/provider/model|provider may be empty",
    ),
    # Legacy wire-format prose describing the pre-v1 on-disk layout.
    (r"^src/", r"provider array|provider/name|\.provider must be|requires providers"),
    # capabilities.py discusses model vendors on purpose (MODEL_SELECT).
    (r"^(?:src/hyprial/daemon/impl/harnesses/capabilities\.py|src/hyprial/kernel/impl/facts/capabilities\.py)$", r"provider"),
    # The Agent entity stores a model-vendor preference (agent-entity design
    # §3), reusing squire's RuntimeCapability(harness, provider, model)
    # vocabulary rather than inventing a second word for the same thing.
    (r"^(?:src/hyprial/identity/impl/agents/__init__\.py|src/hyprial/identity/impl/agents/actor/__init__\.py|src/hyprial/identity/impl/agents/actor/_base\.py|src/hyprial/identity/impl/agents/actor/_core\.py|src/hyprial/identity/impl/agents/actor/_generation_dispatch\.py|src/hyprial/identity/impl/agents/actor/_generation_ops\.py|src/hyprial/identity/impl/agents/actor/ports\.py|src/hyprial/identity/impl/agents/home/__init__\.py|src/hyprial/identity/impl/agents/home/config\.py|src/hyprial/identity/impl/agents/home/environment\.py|src/hyprial/identity/impl/agents/home/provisioner\.py|src/hyprial/identity/impl/agents/migration/__init__\.py|src/hyprial/identity/impl/agents/migration/_base\.py|src/hyprial/identity/impl/agents/migration/_bindings\.py|src/hyprial/identity/impl/agents/migration/actor\.py|src/hyprial/identity/impl/agents/migration/coordinator\.py|src/hyprial/identity/impl/agents/migration/entry\.py|src/hyprial/identity/impl/agents/migration/migration-support-matrix\.json|src/hyprial/identity/impl/agents/registry/__init__\.py|src/hyprial/identity/impl/agents/registry/_base\.py|src/hyprial/identity/impl/agents/registry/_core\.py|src/hyprial/identity/impl/agents/registry/_home_create\.py|src/hyprial/identity/impl/agents/registry/_lifecycle\.py|src/hyprial/identity/impl/agents/registry/_records\.py|src/hyprial/identity/impl/agents/runtime/__init__\.py|src/hyprial/identity/impl/agents/runtime/capabilities\.py|src/hyprial/identity/impl/agents/runtime/context\.py|src/hyprial/identity/impl/agents/runtime/enforcement\.py|src/hyprial/identity/impl/agents/runtime/grants\.py|src/hyprial/identity/impl/agents/runtime/secrets\.py|src/hyprial/identity/impl/agents/runtime/visibility\.py|src/hyprial/identity/impl/agents/state/__init__\.py|src/hyprial/identity/impl/agents/state/activity\.py|src/hyprial/identity/impl/agents/state/keep_actor\.py|src/hyprial/identity/impl/agents/state/liveness\.py|src/hyprial/identity/impl/agents/state/read_port\.py|src/hyprial/identity/impl/agents/state/worker_proxy\.py|src/hyprial/identity/impl/agents/state/worker_state\.py)$", r"provider"),
    # Composition projects that same Agent model-vendor field without changing
    # its meaning.
    (r"^(?:src/hyprial/daemon/impl/composition/views\.py)$", r"provider"),
    # `hyprial agent create --provider` is that same field's CLI surface, and the
    # daemon glue forwards that one field through to the registry.
    (
        r"^(?:src/hyprial/shell/impl/cli/commands/agent/runtime/start\.py|src/hyprial/shell/impl/cli/commands/agent/(?:admin|create|grant|home|restore|secret)\.py|src/hyprial/shell/impl/cli/commands/agent/runtime/claude\.py|src/hyprial/shell/impl/cli/commands/agent/runtime/sessions\.py|src/hyprial/shell/impl/cli/commands/login/commands\.py)$",
        r"--provider|Model vendor|provider: str \| None = typer\.Option"
        r"|if provider is not None",
    ),
    (
        r"^(?:src/hyprial/daemon/impl/application/__init__\.py|src/hyprial/daemon/impl/application/restore\.py|src/hyprial/daemon/impl/application/shutdown\.py|src/hyprial/daemon/impl/application/startup\.py|src/hyprial/daemon/impl/application/wiring/transport\.py|src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops\.py|src/hyprial/daemon/impl/application/actors/agents_registry\.py|src/hyprial/daemon/impl/application/actors/lifecycle_ops\.py|src/hyprial/daemon/impl/application/actors/agent_start/__init__\.py|src/hyprial/daemon/impl/application/actors/routines\.py|src/hyprial/daemon/impl/application/actors/sessions/registry\.py|src/hyprial/daemon/impl/application/actors/transfer\.py|src/hyprial/daemon/impl/application/messaging/visibility\.py|src/hyprial/daemon/impl/application/messaging/inbox_surface/queries\.py|src/hyprial/daemon/impl/application/messaging/inbox_surface/send\.py)$",
        r"provider=_optional_string_param|choice\.provider|modelProvider",
    ),
    # Accepted #425 matrix vocabulary and its PAC launch adapters all carry
    # Squire's model-vendor field; none rename a harness to provider.
    (
        r"^(?:src/hyprial/daemon/impl/dispatch/matrix\.py|src/hyprial/kernel/impl/facts/tier_vocab\.py)$",
        r"provider: str|def provider|provider\)|provider, model|provider \(None\)"
        r"|candidate\.provider|selected\.provider",
    ),
    (
        r"^(?:src/hyprial/daemon/impl/pac/actors/coordinator/types\.py)$",
        r"provider: str|self\.provider|\"provider\"|value\.get\(\"provider\"\)"
        r"|choice\.provider|provider=provider|provider =",
    ),
    (r"^(?:src/hyprial/daemon/impl/pac/actors/daemon\.py)$", r"model_provider|launch\.provider"),
    # The management facade projects Squire's model-vendor field, while the
    # changed-field token is a frozen public wire value.
    (
        r"^(?:src/hyprial/daemon/impl/operations/management\.py)$",
        r"model_provider|\"desiredState\.providers\"",
    ),
    # Public harness model selection is the same model-vendor concept. Keep
    # the whitelist pinned to the adapter modules that translate it; a new
    # ``provider`` hit elsewhere still fails this guard.
    (
        r"^(?:src/hyprial/daemon/interface\.py|src/hyprial/daemon/impl/harnesses/agent_sdk/__init__\.py|src/hyprial/daemon/impl/harnesses/agy/(?:acp_client|process|projection|protocol)\.py|src/hyprial/daemon/impl/harnesses/claude/__init__\.py|src/hyprial/daemon/impl/harnesses/codex/app_server\.py|src/hyprial/daemon/impl/harnesses/codex/client\.py|src/hyprial/daemon/impl/harnesses/codex/native_env\.py|src/hyprial/daemon/impl/harnesses/codex/launch/inputs\.py|src/hyprial/daemon/impl/harnesses/codex/launch/validation\.py|src/hyprial/daemon/impl/harnesses/codex/process\.py|src/hyprial/daemon/impl/harnesses/codex/carrier\.py|src/hyprial/daemon/impl/harnesses/codex/settlement\.py|src/hyprial/daemon/impl/harnesses/pi/__init__\.py|src/hyprial/daemon/impl/harnesses/pi/rpc_client\.py|src/hyprial/daemon/impl/harnesses/pi/rpc_process\.py|src/hyprial/daemon/impl/harnesses/pi/messages\.py|src/hyprial/daemon/impl/harnesses/model_provider\.py)$",
        r"provider",
    ),
    # P23-pi's SDK bridge carries only the explicit model-vendor selection
    # from HarnessLaunchSpec/Pi settings into Pi's modelProvider field.  Root,
    # trust, loader, and session identities remain named separately.
    (
        r"^(?:src/hyprial/daemon/impl/harnesses/pi/loader\.py)$",
        r"default_provider|--provider|model_provider|modelProvider|model provider",
    ),
    # Card 260: the providerError category.  pi_rpc.py quotes the model
    # vendor's own error text verbatim (whitelisted above); turn_runtime
    # decides retryability from it and streaming threads it into
    # worker.turn.failed.  The vocabulary is spec-mandated ("providerError"),
    # so the allowance is pinned to exactly those identifiers -- prose in
    # these two modules says "model vendor" / "model backend" instead.
    (
        r"^(?:src/hyprial/daemon/impl/harnesses/turn_delivery/turn/turn_runtime\.py|src/hyprial/daemon/impl/harnesses/turn_delivery/turn/__init__\.py|src/hyprial/daemon/impl/harnesses/streaming/process\.py|src/hyprial/daemon/impl/harnesses/streaming/protocol\.py|src/hyprial/daemon/impl/harnesses/streaming/failure\.py)$",
        r"_TERMINAL_PROVIDER_ERROR_MARKERS|provider_failure_is_terminal"
        r"|_turn_provider_error|provider_error|providerError",
    ),
    # Desired-state and actor projections store the explicit model-vendor
    # field as model_provider / modelProvider, distinct from the legacy
    # harness-valued wire key ``provider``.
    (
        r"^(?:src/hyprial/kernel/impl/facts/launch_spec/spec\.py|src/hyprial/daemon/impl/desired_state/documents\.py|src/hyprial/daemon/impl/desired_state/sqlite\.py|src/hyprial/daemon/impl/harnesses/actor/contracts\.py|src/hyprial/daemon/impl/harnesses/runtime/ports\.py|src/hyprial/daemon/impl/lifecycle_manager/steps\.py)$",
        r"model_provider|modelProvider|provider`` remains the harness",
    ),
    (
        r"^src/hyprial/daemon/impl/application/messaging/status/snapshots\.py$",
        r"^                or target\.harness\.model_provider != agent\.provider$",
    ),
    # Operator restart guidance renders the persisted model-vendor choice as
    # the public `hyprial agent start --provider` flag; it never renames a harness.
    (
        r"^src/hyprial/daemon/impl/application/messaging/operator_alarm\.py$",
        r"model_provider|--provider|harness/provider/model",
    ),
    # ``hyprial agent start --provider`` plumbing in the shared CLI. Match only the
    # model-provider identifiers introduced by that path, not every provider
    # spelling in cli.py.
    (
        r"^(?:src/hyprial/shell/impl/cli/commands/agent/runtime/start\.py|src/hyprial/shell/impl/cli/commands/agent/(?:admin|create|grant|home|restore|secret)\.py|src/hyprial/shell/impl/cli/commands/agent/runtime/claude\.py|src/hyprial/shell/impl/cli/commands/agent/runtime/sessions\.py|src/hyprial/shell/impl/cli/commands/login/commands\.py)$",
        r"model_provider|provider_(?:spec|environment|args|argv)"
        r"|provider=model_provider|provider: str \| None = None"
        r"|harnesses\.model_provider|modelProvider",
    ),
    # `squire setup` lives in cli.py but speaks the squire vocabulary: its
    # --provider/--model pair names the model vendor exactly as
    # ``src/hyprial/squire/`` does (that package is whitelisted wholesale), and
    # the first-run apply path reaches the same operation. Matched as the two
    # literal spellings the squire surface uses, not as a bare `provider`.
    (
        r"^(?:src/hyprial/shell/impl/cli/commands/agent/runtime/start\.py|src/hyprial/shell/impl/cli/commands/agent/(?:admin|create|grant|home|restore|secret)\.py|src/hyprial/shell/impl/cli/commands/agent/runtime/claude\.py|src/hyprial/shell/impl/cli/commands/agent/runtime/sessions\.py|src/hyprial/shell/impl/cli/commands/login/commands\.py)$",
        r'provider: str = "deepseek"|provider=provider,',
    ),
    (
        r"^(?:src/hyprial/shell/impl/cli/commands/agent/runtime/interactive\.py)$",
        r"model_provider|--provider",
    ),
    (
        r"^(?:src/hyprial/skills/hyprial-ops/SKILL\.md)$",
        r"--provider/--model",
    ),
    # login U2 deliberately documents that the network ``--provider`` flag
    # is gone — collected by ``profile.controlPlane.kind`` (D-U2-3, design
    # §9.6). This is the opposite of a provider regression: the module must
    # be able to name the retired flag while explaining its replacement.
    (r"^src/hyprial/shell/impl/login/flow\.py$", r"--provider"),
    # OIDC calls the remote issuer the identity provider. ROUND 5 requires
    # this exact operator-facing diagnosis when its ``sub`` shape is unknown.
    (
        r"^src/hyprial/shell/impl/login/oidc\.py$",
        r"identity provider returned an id format hyprial does not recognise",
    ),
    # `agent provider login` (Allen 2026-10-14): the feature's public surface
    # names the model vendor with the Squire/Agent vocabulary, exactly as
    # `agent tiers probe` and squire's own modules already do. The command
    # spelling is operator-facing, so the word is part of the contract here
    # rather than a renamed harness.
    (r"^src/hyprial/shell/impl/cli/commands/agent/provider/.*\.py$", r"provider"),
    (r"^src/hyprial/daemon/impl/application/actors/agents_admin/provider_credential\.py$", r"provider"),
    (
        r"^src/hyprial/daemon/impl/application/actors/agents_admin/registry_ops\.py$",
        r"agent\.provider\.login|handle_provider_login|provider_login|provider_credential",
    ),
    (
        r"^src/hyprial/daemon/impl/ipc/router\.py$",
        r'^\s*"agent\.provider\.(?:login|logout|default)",\s*$'
        r"|^\s*return self\._handle_agent_provider\(method, params, operator=operator\)\s*$",
    ),
    # hyprial-ops teaches operators the provider-login and key-grant commands by
    # their exact operator-facing spelling. Pinned to those command shapes.
    (
        r"^src/hyprial/skills/hyprial-ops/SKILL\.md$",
        r"下发 provider 凭据|provider-login-2026-10-14|secret provider-write|--source user-provider"
        r"|hyprial agent provider (?:grant-key|login|logout|default)|providerSeed"
        r"|--provider (?:hyprial|openai)",
    ),
    # `agent create` seeds the new agent through the provider-login package and
    # its help names that command and its revoke path. Pinned to those shapes.
    (
        r"^src/hyprial/shell/impl/cli/commands/agent/create\.py$",
        r"^from hyprial\.shell\.impl\.cli\.commands\.agent\.provider\.commands import seed_new_agent$"
        r"|'hyprial agent provider$|^\s*agent provider logout'\)",
    ),
    # Doctor prints the exact public provider-login command needed to restore a
    # Codex credential backup; ``provider`` is the command group's model-vendor name.
    (
        r"^src/hyprial/shell/impl/cli/commands/doctor/credential_backups\.py$",
        r"hyprial agent provider",
    ),
    # The application composes the provider-login mixin exactly as it composes
    # the other agents_admin mixins. Pinned to the one import line.
    (
        r"^src/hyprial/daemon/impl/application/__init__\.py$",
        r"^from hyprial\.daemon\.impl\.application\.actors\.agents_admin\.provider_credential import _AgentProviderMixin$",
    ),
    (
        r"^src/hyprial/error/codes\.py$",
        r"# Provider credential seeding",
    ),
    # The Shell composition root imports and registers `agent provider login`
    # exactly as it does `agent tiers`; the word is the feature's operator-facing
    # surface, not a renamed harness. Pinned to the registration import and the
    # one add_family line that registers the group.
    (
        r"^src/hyprial/shell/impl/cli/runtime/registration\.py$",
        r"^from hyprial\.shell\.impl\.cli\.commands\.agent\.provider import "
        r"|^    runtime\.add_family\(provider_app, name=\"provider\", parent=agent_app\)$",
    ),
    (
        r"^src/hyprial/shell/impl/cli/runtime/registration\.py$",
        r"^from hyprial\.shell\.impl\.cli\.commands\.user\.provider import provider_app as user_provider_app$"
        r"|^    runtime\.add_family\(user_provider_app, name=\"provider\", parent=user_app\)$",
    ),
    # T3 first-hop (F-a): the landing path carries the source row's
    # model-vendor wiring (provider/model -- squire-class vocabulary shared
    # with the Agent entity/registry) into the sealed bundle identity and then
    # onto the landed row.  Pinned to the exact shapes these two modules use:
    # identifier/guard shapes first, then prose shapes; a new provider hit in
    # either file still fails this gate.
    (
        r"^(?:src/hyprial/daemon/impl/transfer/landing/registry\.py|src/hyprial/daemon/impl/teleport/wiring/source_ops\.py)$",
        r"provider: str \| None|provider=provider,|provider=wiring\.provider,"
        r"|isinstance\(provider, str\) and provider",
    ),
    (
        r"^(?:src/hyprial/daemon/impl/transfer/landing/registry\.py|src/hyprial/daemon/impl/teleport/wiring/source_ops\.py)$",
        r"provider``/``model``|pre-fix ``provider``|provider/model",
    ),
    # T3 D4: a P2 Codex resume reads the target's own config.toml.  The
    # ``model_provider`` argument and ``provider-owned`` prose name the model
    # vendor exactly as the launch spec's model_provider does -- model-vendor
    # vocabulary, not a renamed harness.
    (
        r"^(?:src/hyprial/daemon/impl/harnesses/codex/projection/config\.py)$",
        r"model_provider|provider-owned",
    ),
    # T3 env-key export: the exclusion docstring names the user-level
    # ``user-provider.json`` entry (same on-disk name as the bundle/_base.py
    # allowance above); it lives outside the agent home and is never exported.
    (
        r"^(?:src/hyprial/daemon/impl/transfer/archive/envelope\.py)$",
        r"user-provider entry",
    ),
    # The hyprial-ops skill links the existing reference page by its file name
    # (docs/reference/harness-model-providers.md); the word is part of that
    # path, not a renamed harness.
    (
        r"^(?:src/hyprial/skills/hyprial-ops/SKILL\.md)$",
        r"`docs/reference/harness-model-providers\.md`",
    ),
    # #2042's explicit credential command names the model-vendor login surface;
    # these exact runtime/CLI messages and skill instructions are not a harness
    # rename and are registered line-by-line (no path-wide allowance).
    (
        r"^src/hyprial/daemon/impl/harnesses/claude/runtime\.py$",
        r"Claude credential is missing: use agent provider login claude --actor <agent> ",
    ),
    (
        r"^src/hyprial/daemon/impl/ipc/router\.py$",
        r"return self\._handle_agent_provider_request\(",
    ),
    (
        r"^src/hyprial/identity/impl/agents/secret_store/defaults\.py$",
        r"source TEXT NOT NULL CHECK \(source IN \('user-provider','agent-private','agent-shared'\)\), ",
    ),
    (
        r"^src/hyprial/shell/impl/cli/commands/agent/create\.py$",
        "|".join(
            __import__("re").escape(line)
            for line in (
                "    Credentials: a captured Claude default ('agent provider login claude",
                "    Legacy provider-name defaults still seed from host login in this CLI only;",
                "    re-run provider login --default --actor OWNER to bind a Claude source for PAC.",
            )
        ),
    ),
    (
        r"^src/hyprial/skills/hyprial-ops/SKILL\.md$",
        "|".join(
            __import__("re").escape(line)
            for line in (
                "  `agent provider login claude --setup-token --actor <owner> --default`。",
                "  `agent provider default --json` 查引用，`agent secret ls <consumer> --json` 查 grant。",
                "  `agent secret revoke <consumer> default-claude` 撤 grant；`agent provider default --clear`",
                "  原生复制仍用 `agent provider login claude --actor <owner> --default`。",
                "  旧版 provider-name 默认不支持 PAC 自动复制，必须重跑 `login claude --default --actor <owner>`。",
            )
        ),
    ),
    # The 0.5.5.3 cell run-state contract (contract/daemon-lifecycle/run-state*)
    # publishes providerBlocked / "provider-block"; dev renames them to the
    # agent-block vocabulary in #2203. Pinned to exactly these identifiers
    # until that rename is promoted with the contract.
    (
        r"^(?:src/hyprial/daemon/impl/cells/observability/(?:model|reducer)\.py|src/hyprial/daemon/impl/composition/cell_observation/run_states\.py|src/hyprial/shell/impl/cli/output/status/__init__\.py)$",
        r"provider_block|providerBlocked|\"provider-block\"|provider-blocked=true|prompt/provider error",
    ),
)


_PROBE_TRANSPORT_LINES = (
    ('src/hyprial/kernel/impl/ipc/client.py', '    cancellation = current_request_cancellation() if method == "agent.secret.provider-test" else None'),
    ('src/hyprial/daemon/impl/ipc/server.py', '                if method == "agent.secret.provider-test":'),
    ('src/hyprial/shell/impl/request_http/app.py', '            if name == "agent.secret.provider-test":'),
    ('src/hyprial/shell/impl/request_http/server.py', '        if name == "agent.secret.provider-test" and "timeoutSeconds" in params:'),
)
_RULES += tuple(("^" + re.escape(path) + "$", "^" + re.escape(line) + "$")
                for path, line in _PROBE_TRANSPORT_LINES)


@pytest.mark.parametrize(('path', 'line'), _PROBE_TRANSPORT_LINES)
def test_probe_transport_wire_allowance_is_exact(path, line):
    def allowed(candidate_path, candidate_line):
        return any(re.search(p, candidate_path) and re.search(c, candidate_line) for p, c in _RULES)
    assert allowed(path, line)
    assert not allowed(path, line.replace('provider-test', 'provider-unregistered'))
    assert not allowed(path, line + ' provider = legacy_harness')
    assert not allowed('src/hyprial/unregistered.py', line)


def test_start_failure_model_vendor_comparison_is_exact() -> None:
    path = "src/hyprial/daemon/impl/application/messaging/status/snapshots.py"
    line = "                or target.harness.model_provider != agent.provider"
    def accepts(candidate_path: str, candidate: str) -> bool:
        return any(re.search(p, candidate_path) and re.search(c, candidate) for p, c in _RULES)
    assert accepts(path, line)
    assert not accepts(path, line + " or legacy_provider")
    assert not accepts(path, "provider = legacy_harness")
    assert not accepts("src/hyprial/daemon/impl/unrelated.py", line)


def test_wave2_provider_allowances_are_token_scoped() -> None:
    rules = {path: line for path, line in _RULES}
    for path in (
        r"^(?:src/hyprial/daemon/impl/dispatch/matrix\.py|src/hyprial/kernel/impl/facts/tier_vocab\.py)$",
        r"^(?:src/hyprial/daemon/impl/pac/actors/coordinator/types\.py)$",
    ):
        assert rules[path] != r"provider"
        assert ".provider" in rules[path] or "provider:" in rules[path]


def _source_paths(repo_root: Path) -> list[str]:
    """Read working-tree content, including unstaged native modules."""
    paths: set[str] = set()
    for flags in ((), ("--others", "--exclude-standard")):
        result = subprocess.run(
            ["git", "ls-files", "-z", *flags, "--", "src/hyprial/"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        paths.update(filter(None, result.stdout.split("\0")))
    excluded = {".git", ".venv", "__pycache__", "node_modules"}
    actual = []
    for relative in sorted(paths):
        path = Path(relative)
        assert path.parts[:2] == ("src", "hyprial") and ".." not in path.parts
        if any(part in excluded for part in path.parts) or path.parts[2:3] == ("_gui",):
            continue
        absolute = repo_root / path
        assert not absolute.is_symlink(), f"source symlink requires review: {relative}"
        if absolute.is_file():  # tracked-but-retired paths are not actual source
            actual.append(relative)
    return actual


def _grep_hits(repo_root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    hits = []
    paths = _source_paths(repo_root)
    assert paths, "sanity: actual src/hyprial source coverage must not be empty"
    for path in paths:
        content = (repo_root / path).read_bytes()
        if b"\0" in content:  # match grep's text scope, not binary payloads
            continue
        for line in content.decode("utf-8").splitlines():
            if "provider" in line:
                hits.append((path, line))
    return hits


def test_untracked_native_source_is_scanned_and_unknown_term_rejected(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    files = {
        "src/hyprial/daemon/impl/unregistered.py": "provider = 'legacy harness'\n",
        "src/hyprial/daemon/impl/squire/unregistered.py": "provider = 'legacy harness'\n",
        "src/hyprial/identity/impl/agents/unregistered.py": "provider = 'legacy harness'\n",
        "src/hyprial/identity/impl/provider_auth/unregistered.py": "provider = 'legacy harness'\n",
        "src/hyprial/daemon/impl/adapters/lark/unregistered.py": "provider_metadata = legacy\n",
        "src/hyprial/kernel/impl/unregistered_wire.py": 'WIRE_KEY = "provider"\n',
        "src/hyprial/daemon/impl/harnesses/probe.py": "model_provider = vendor\n",
        "src/gui/unknown.py": "provider = 'GUI is a separate source scope'\n",
        "src/hyprial/__pycache__/unknown.py": "provider = 'dependency cache'\n",
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    assert not subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=tmp_path
    ), "the negative control must not depend on staging"
    hits = _grep_hits(tmp_path)
    assert {path for path, _ in hits} == {
        "src/hyprial/daemon/impl/unregistered.py",
        "src/hyprial/daemon/impl/squire/unregistered.py",
        "src/hyprial/identity/impl/agents/unregistered.py",
        "src/hyprial/identity/impl/provider_auth/unregistered.py",
        "src/hyprial/daemon/impl/adapters/lark/unregistered.py",
        "src/hyprial/kernel/impl/unregistered_wire.py",
        "src/hyprial/daemon/impl/harnesses/probe.py",
    }
    unmatched = [
        path for path, content in hits
        if not any(re.search(p, path) and re.search(c, content) for p, c in _RULES)
    ]
    assert unmatched == [
        "src/hyprial/daemon/impl/adapters/lark/unregistered.py",
        "src/hyprial/daemon/impl/squire/unregistered.py",
        "src/hyprial/daemon/impl/unregistered.py",
        "src/hyprial/identity/impl/agents/unregistered.py",
        "src/hyprial/identity/impl/provider_auth/unregistered.py",
    ]


def test_agent_provider_allowance_is_exact_path_and_line(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    relative = "src/hyprial/daemon/impl/request_catalog/agent/provider.py"
    allowed = 'AGENT_PROVIDER_DEFAULT = "agent.provider.default"'
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text(allowed + "\nprovider = legacy_harness\n", encoding="utf-8")
    assert allowed in target.read_text(encoding="utf-8").splitlines()
    matched_allowed = any(
        re.search(path_pattern, relative) and re.search(line_pattern, allowed)
        for path_pattern, line_pattern in _RULES
    )
    matched_new = any(
        re.search(path_pattern, relative)
        and re.search(line_pattern, "provider = legacy_harness")
        for path_pattern, line_pattern in _RULES
    )
    assert matched_allowed
    assert not matched_new
    other_relative = "src/hyprial/daemon/impl/request_catalog/unknown.py"
    other = tmp_path / other_relative
    other.write_text(allowed + "\n", encoding="utf-8")
    assert not any(
        re.search(path_pattern, other_relative) and re.search(line_pattern, allowed)
        for path_pattern, line_pattern in _RULES
    )
    for relative, allowed in (
        (
            "src/hyprial/daemon/impl/request_catalog/agent/examples/__init__.py",
            _M03_AGENT_PROVIDER_WIRE_LINES[0],
        ),
        (
            "src/hyprial/daemon/impl/request_catalog/registry.py",
            "    from .agent.provider import AGENT_PROVIDER_BINDINGS",
        ),
        (
            "src/hyprial/daemon/impl/request_catalog/migration.py",
            "        'agent.provider.allow',",
        ),
        (
            "src/hyprial/daemon/impl/request_catalog/migration.py",
            "        'agent.secret.provider-write',",
        ),
        (
            "src/hyprial/daemon/impl/request_catalog/migration.py",
            "        'user.provider.rotate',",
        ),
        (
            "src/hyprial/shell/impl/cli/commands/user/key.py",
            "from hyprial.shell.impl.cli.commands.agent.provider.commands import refuse_owner_key_in_worker_shell",
        ),
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(allowed + "\nprovider = legacy_harness\n", encoding="utf-8")
        assert any(
            re.search(path_pattern, relative)
            and re.search(line_pattern, allowed)
            for path_pattern, line_pattern in _RULES
        )
        assert not any(
            re.search(path_pattern, relative)
            and re.search(line_pattern, "provider = legacy_harness")
            for path_pattern, line_pattern in _RULES
        )


@pytest.mark.parametrize(("relative", "line"), [
    ("agent/provider_auth/__init__.py", "Imports and removal retain agent.provider.login/logout. No timestamp or launch"),
    ("agent/provider_auth/__init__.py", '    name="agent.provider.auth.list", owner="daemon.agents",'),
    ("agent/provider_auth/__init__.py", '    method="agent.provider.auth.list",'),
    ("agent/examples/__init__.py", "from ..provider_auth import EXAMPLES as NATIVE_AUTH_EXAMPLES"),
    ("registry.py", "    from .agent.provider_auth import BINDINGS as NATIVE_AUTH_BINDINGS"),
    ("migration.py", "        'agent.provider.auth.list',"),
    ("migration.py", "        'agent.secret.provider-test',"),
    ("setup/probe.py", 'METHOD = "agent.secret.provider-test"'),
])
def test_catalog_wire_allowance_is_exact(relative: str, line: str) -> None:
    path = "src/hyprial/daemon/impl/request_catalog/" + relative

    def allowed(candidate_path: str, candidate_line: str) -> bool:
        return any(re.search(p, candidate_path) and re.search(c, candidate_line) for p, c in _RULES)

    assert allowed(path, line)
    assert not allowed(path, line + " provider = legacy_harness")
    if "auth.list" in line:
        assert not allowed(path, line.replace("auth.list", "auth.unregistered"))
    if "provider-test" in line:
        assert not allowed(path, line.replace("provider-test", "provider-unregistered"))
    assert not allowed(path, "provider = legacy_harness")
    assert not allowed("src/hyprial/daemon/impl/request_catalog/unregistered.py", line)


@pytest.mark.parametrize("path", [
    "src/hyprial/daemon/impl/adapters/spec.py",
    "src/hyprial/daemon/impl/adapters/storage.py",
    "src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py",
    "src/hyprial/daemon/impl/adapters/contracts/values.py",
    "src/hyprial/daemon/impl/adapters/common/transport/media.py",
    "src/hyprial/daemon/impl/adapters/telegram/provider.py",
])
def test_adapter_provider_allowance_does_not_cover_new_lines(path: str) -> None:
    assert not any(
        re.search(p, path) and re.search(c, "provider = legacy_harness")
        for p, c in _RULES
    )


@pytest.mark.parametrize("line", _SETUP_SECRET_WIRE_LINES)
def test_setup_secret_wire_allowance_is_exact(line: str) -> None:
    path = "src/hyprial/daemon/impl/request_catalog/setup/specs.py"
    assert any(re.search(p, path) and re.search(c, line) for p, c in _RULES)
    assert not any(re.search(p, path) and re.search(c, "provider = legacy_harness") for p, c in _RULES)
    assert not any(re.search(p, "src/hyprial/unrelated.py") and re.search(c, line) for p, c in _RULES)


def test_empty_source_coverage_is_rejected(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(AssertionError, match="source coverage must not be empty"):
        _grep_hits(tmp_path)


@pytest.mark.parametrize("relative,line", _MODEL_ROUTE_LINES + _FIRST_RUN_MODEL_VENDOR_LINES)
def test_model_route_terms_only_admit_the_exact_wire_or_selection_line(relative, line):
    path = "src/hyprial/" + relative
    def allowed(candidate_path, candidate_line):
        return any(re.search(p, candidate_path) and re.search(c, candidate_line) for p, c in _RULES)
    assert allowed(path, line)
    assert not allowed(path, "provider = legacy_harness")
    assert not allowed(path, line + " provider = legacy_harness")
    assert not allowed("src/hyprial/unregistered.py", line)


def test_remaining_provider_hits_are_all_whitelisted() -> None:
    hits = _grep_hits()
    assert hits, "sanity: the whitelisted wire keys must still exist"

    unmatched = []
    for path, content in hits:
        if not any(
            re.search(path_pattern, path) and re.search(line_pattern, content)
            for path_pattern, line_pattern in _RULES
        ):
            unmatched.append(f"{path}: {content.strip()}")

    assert not unmatched, (
        "non-whitelisted 'provider' hits (must be wire keys on the §1.3 "
        "list or squire model-vendor terminology):\n" + "\n".join(unmatched)
    )
