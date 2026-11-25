"""Agent administration aimed at the user agent: one dedicated code, catalog and legacy.

The refused and allowed method lists below are literal on purpose: a new
``agent.*`` method (catalog or legacy router) fails the guard until someone
decides which list it belongs to.  ``NAME_GATED`` methods take the reserved
name as input and the name gate refuses it with INVALID_ARGUMENT;
``agent.destroy`` carries only an operationId, and its preview is refused.

Outside ``agent.*`` the same holds for every catalog method with a param
named ``agent``, ``actor``, ``name``, ``target``, ``owner``, ``to`` or
``recipient``, or ending in ``Agent``, ``Actor``, ``Uri`` or ``URI``, at any
depth of its params schema (``properties``, ``items``, ``oneOf``,
``anyOf``, ``allOf``, ``additionalProperties``), and for every legacy
non-``agent.*`` method: ``REFUSED_OTHER`` refuse the user agent
as the target of administration in their handlers, ``REFUSED_OTHER_LEGACY``
(no spec) refuse to bind a session to it; ``OTHER_NOT_A_TARGET``
take no agent to administer (the param is the caller, an adapter, a user, a
host or a graph actor) or are covered elsewhere (the name gate, the work
refusals of ``test_work_refusals``, the harness requirement of
``onboarding.firstRun.begin`` in ``test_first_run_refusal``).  A new method in that shape fails the
guard until it is classified.

Widening the match to any depth and to those names (PR 2941 second review)
surfaced no new method; it surfaced these params of methods already
classified, each with its reason in ``DEEP_PARAMS``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.daemon.impl.user_agent import USER_AGENT_NAME, local_user_agent, run_user_agent_ensure
from hyprial.daemon.impl.user_agent import admin
from hyprial.kernel import ipc_errors
from tests.identity.agents.registry.entity.support import (
    OWNER,
    _close_test_applications as _close_test_applications,
    application,
    enable_routines,
)

ROOT = Path(__file__).resolve().parents[4]
CODE = "USER_AGENT_IMMUTABLE"

REFUSED_CATALOG = frozenset({
    "agent.attach", "agent.destroy.preview",
    "agent.detach", "agent.harness-args.clear",
    "agent.harness-args.set", "agent.provider.allow", "agent.provider.login",
    "agent.provider.logout", "agent.rename", "agent.secret.grant",
    "agent.start", "agent.stop",
})
REFUSED_LEGACY = frozenset({
    "agent.config.rebind", "agent.ensure-home", "agent.grant", "agent.migrate.execute",
    "agent.migrate.preflight", "agent.migrate.rollback", "agent.restore-policy",
    "agent.resume.restore", "agent.runtime-context", "agent.unblock",
})
REVOKES = frozenset({"agent.provider.revoke", "agent.revoke", "agent.secret.revoke"})
"""Allowed on purpose: revocation only removes (``test_revoke_allowed``)."""
NAME_GATED = frozenset({"agent.create", "agent.start.preflight"})
ALLOWED = REVOKES | frozenset({
    "agent.destroy",
    "agent.get", "agent.grant.check", "agent.grant.visibility", "agent.grants",
    "agent.harness-args.get", "agent.harness-args.validate", "agent.home-census",
    "agent.list", "agent.provider.auth.list", "agent.provider.default",
    "agent.provider.status", "agent.readiness", "agent.resolve", "agent.restore-threshold",
    "agent.runtime-launch.acquire", "agent.runtime-launch.custody",
    "agent.runtime-launch.release", "agent.secret.list", "agent.secret.provider-configure",
    "agent.secret.provider-list", "agent.secret.provider-test",
    "agent.secret." + "provider-write",
}) | admin.CELL_DOCUMENT_EXEMPT
REFUSED_OTHER = frozenset({
    "org.acl.grant", "teleport.run",
})
REFUSED_OTHER_LEGACY = frozenset({"session.register", "session.register_guarded"})
"""Legacy router methods (no spec) that refuse the user agent: ``test_session_refusals``."""
_GROUP_WHY = (
    "its only target-like param is the caller alias ``actor``; a user-agent member "
    "resolves to the owner and is never administered"
)
GROUP_NOT_A_TARGET = {
    "conversations.create": "creates a group; " + _GROUP_WHY,
    "conversations.get": "reads a group; " + _GROUP_WHY,
    "conversations.members.add": "adds a group member; " + _GROUP_WHY,
    "conversations.members.remove": "removes a group member; " + _GROUP_WHY,
    "conversations.post": (
        "posts to a group; its caller alias is ``actor``/``from`` and ``mentions`` "
        "names current members, where a user-agent member resolves to the owner and "
        "is never administered"
    ),
}
"""The group conversation methods (slice GC2) in ``OTHER_NOT_A_TARGET``, and why."""
OTHER_NOT_A_TARGET = frozenset({
    "adapter.authorization.begin", "adapter.authorization.operation",
    "adapter.authorization.status", "adapter.identities.list", "adapter.identities.sync",
    "adapter.onboard.begin", "adapter.onboard.operation", "adapter.register",
    "adapter.routes.add",
    "adapter.routes.candidates", "adapter.routes.list", "adapter.routes.remove",
    "adapter.start", "adapter.status", "adapter.stop",
    "conversations.history", "conversations.list", "identity.users.add",
    "identity.whoami", "management.adapter.remove.preview",
    "message.history", "message.pending.batch", "message.query",
    "message.send", "message.send.key_lookup", "message.send.lookup", "message.status",
    "network.expose", "org.acl.ls", "org.acl.revoke", "orgfs.checkout", "orgfs.create", "orgfs.export",
    "orgfs.history", "orgfs.import", "orgfs.invite", "orgfs.join", "orgfs.ls",
    "orgfs.members", "orgfs.mkdir", "orgfs.move", "orgfs.purge", "orgfs.purge_plan",
    "orgfs.purge_status", "orgfs.read", "orgfs.read_at", "orgfs.remove",
    "orgfs.remove_member", "orgfs.resolve", "orgfs.restore", "orgfs.serve",
    "orgfs.space_purge", "orgfs.spaces", "orgfs.stat", "orgfs.stat_at", "orgfs.status",
    "orgfs.trash", "orgfs.unban", "orgfs.watch", "orgfs.write", "progress.list",
    "routine.audit", "routine.list", "routine.pause", "routine.plan", "routine.resume",
    "routine.status", "service.connect", "service.disconnect", "service.guide",
    "session.turn.refresh", "targets", "userKey.sign", "workflow.history.list",
    "workflow.list", "workflow.node.context",
    "autoupdate.trigger", "dispatch.matrix.resolve", "down",
    "lifecycle.resume.preflight", "lifecycle.start",
    "message.ack", "message.pending.list", "onboarding.firstRun.begin",
    "message.pending.wait", "message.read_body", "message.reply", "message.settle_hint",
    "outbox.prune", "pac.actor.stop", "pac.flag.reset", "pac.flag.set",
    "pac.graph.activate", "pac.graph.close", "ping", "ps", "routine.add",
    "routine.remove", "routine.set", "session.heartbeat", "session.refresh",
    "session.turn.ended",
    "session.turn.started", "session.unregister", "shutdown", "worker.channel.request",
    "workflow.cancel", "workflow.complete", "workflow.expansion.plan", "workflow.fail",
    "workflow.history.status", "workflow.node.inspect", "workflow.plan",
    "workflow.remote.current", "workflow.start", "workflow.status",
    "workflow.worker.restart", "workflow.worker.stop",
}) | frozenset(GROUP_NOT_A_TARGET)
ALLOWED_TARGETS = {
    "org.acl.revoke": "removes a grant (an older build may have written one for the user "
    "agent); grants nothing",
}
"""``OTHER_NOT_A_TARGET`` methods that do name an agent and are allowed on purpose."""
_TARGET_KEYS = frozenset({"agent", "actor", "name", "target", "owner", "to", "recipient"})
_TARGET_SUFFIX = re.compile(r"(?:Agent|Actor|Uri|URI)$")
DEEP_PARAMS = {
    ("onboarding.firstRun.begin", "agentUri"): "the first-run agent must have a harness and "
    "one credential; the user agent has neither and is refused before any write "
    "(``test_first_run_refusal``)",
    ("orgfs.purge_plan", "targets[].to"): "the end of a tree or document revision range, "
    "not an agent",
}
"""Params the depth/name widening matched beyond the top-level six, and why each is fine."""


def _target_params(schema: object, path: str = "") -> set[str]:
    """Every param path in *schema* whose name looks like an agent target."""

    found: set[str] = set()
    if isinstance(schema, Mapping):
        for key, value in (schema.get("properties") or {}).items():
            if key in _TARGET_KEYS or _TARGET_SUFFIX.search(key):
                found.add(path + key)
            found |= _target_params(value, f"{path}{key}.")
        for combinator in ("oneOf", "anyOf", "allOf"):
            for branch in schema.get(combinator) or ():
                found |= _target_params(branch, path)
        if "items" in schema:
            found |= _target_params(schema["items"], path[:-1] + "[]." if path else "[].")
        if isinstance(schema.get("additionalProperties"), Mapping):
            found |= _target_params(schema["additionalProperties"], path + "*.")
    elif isinstance(schema, Sequence) and not isinstance(schema, str):
        for branch in schema:
            found |= _target_params(branch, path)
    return found


def test_the_param_walker_reaches_every_nesting() -> None:
    from types import MappingProxyType

    schema = MappingProxyType({"properties": {
        "plain": {"type": "string"},
        "agentUri": {"type": "string"},
        "list": {"items": {"properties": {"recipient": {}}}},
        "choice": {"oneOf": ({"properties": {"peerActor": {}}},
                             {"anyOf": [{"allOf": [{"properties": {"owner": {}}}]}]})},
        "map": {"additionalProperties": {"properties": {"targetURI": {}}}},
    }})

    assert _target_params(schema) == {
        "agentUri", "list[].recipient", "choice.peerActor", "choice.owner", "map.*.targetURI",
    }


def _legacy_methods() -> set[str]:
    baseline = json.loads((ROOT / "scripts/ci/legacy-method-baseline.json").read_text())
    return set(baseline["methods"])


def _legacy_agent_methods() -> set[str]:
    return {name for name in _legacy_methods() if name.startswith("agent.")}


def _other_methods_naming_a_target() -> set[str]:
    catalog = {
        name for name in METHOD_REGISTRY.names
        if not name.startswith("agent.")
        and _target_params(METHOD_REGISTRY.get(name).spec.params_schema or {})
    }
    legacy = {
        name for name in _legacy_methods()
        if not name.startswith("agent.") and name not in METHOD_REGISTRY
    }
    return catalog | legacy


def test_every_other_method_naming_a_target_is_classified() -> None:
    every = _other_methods_naming_a_target()
    refused = REFUSED_OTHER | REFUSED_OTHER_LEGACY
    assert not refused & OTHER_NOT_A_TARGET, "a method sits in two lists"
    unclassified = every - refused - OTHER_NOT_A_TARGET
    assert unclassified == set(), f"classify these for the user agent: {sorted(unclassified)}"
    assert refused | OTHER_NOT_A_TARGET <= every
    assert not REFUSED_OTHER_LEGACY & set(METHOD_REGISTRY.names)


def test_every_deep_or_suffixed_target_param_has_a_reason() -> None:
    deep = {
        (name, param)
        for name in METHOD_REGISTRY.names if not name.startswith("agent.")
        for param in _target_params(METHOD_REGISTRY.get(name).spec.params_schema or {})
        if param not in _TARGET_KEYS
    }
    assert deep == set(DEEP_PARAMS), f"give these params a reason: {sorted(deep ^ set(DEEP_PARAMS))}"


def test_the_purposely_allowed_targets_are_not_refused() -> None:
    assert set(ALLOWED_TARGETS) <= OTHER_NOT_A_TARGET
    for name in ALLOWED_TARGETS:
        declared = {error.code for error in METHOD_REGISTRY.get(name).spec.errors}
        assert CODE not in declared, name


def test_every_refused_other_method_declares_the_code() -> None:
    for name in REFUSED_OTHER:
        declared = {error.code for error in METHOD_REGISTRY.get(name).spec.errors}
        assert CODE in declared, name


def _catalog_agent_methods() -> set[str]:
    return {name for name in METHOD_REGISTRY.names if name.startswith("agent.")}


def test_every_agent_method_is_classified() -> None:
    every = _legacy_agent_methods() | _catalog_agent_methods()
    lists = (REFUSED_CATALOG, REFUSED_LEGACY, NAME_GATED, ALLOWED)
    assert not set.intersection(*map(set, lists[:2])), "a method sits in two lists"
    unclassified = every - REFUSED_CATALOG - REFUSED_LEGACY - NAME_GATED - ALLOWED
    assert unclassified == set(), f"classify these for the user agent: {sorted(unclassified)}"
    assert (REFUSED_CATALOG | REFUSED_LEGACY | NAME_GATED | ALLOWED) <= every


def _revoke_methods() -> set[str]:
    every = _legacy_agent_methods() | _catalog_agent_methods()
    return {name for name in every if name.rpartition(".")[2] == "revoke"}


def test_the_revoke_methods_are_exactly_the_allowed_revokes() -> None:
    """A fourth ``agent.x.revoke`` fails here until someone decides about it."""

    assert _revoke_methods() == REVOKES


def test_the_code_allows_exactly_the_allowed_revokes() -> None:
    assert admin.NEVER_REFUSED == REVOKES


def test_the_code_refuses_exactly_the_listed_methods() -> None:
    catalog = {
        name for name in _catalog_agent_methods()
        if admin.refused_target_param(METHOD_REGISTRY.get(name).spec) is not None
    }
    legacy = set(getattr(admin, "LEGACY_TARGET_PARAMS", {})) - _catalog_agent_methods()
    assert catalog == REFUSED_CATALOG
    assert legacy == REFUSED_LEGACY


def test_the_handler_refused_methods_are_exactly_these() -> None:
    assert admin.HANDLER_REFUSED == {
        "agent.harness-args.clear", "agent.harness-args.set",
        "agent.provider.login", "agent.provider.logout",
    }
    assert admin.HANDLER_REFUSED <= REFUSED_CATALOG


def test_every_refused_catalog_method_declares_the_code() -> None:
    for name in REFUSED_CATALOG:
        declared = {error.code for error in METHOD_REGISTRY.get(name).spec.errors}
        assert CODE in declared, name
    for name in REVOKES & set(METHOD_REGISTRY.names):
        declared = {error.code for error in METHOD_REGISTRY.get(name).spec.errors}
        assert CODE not in declared, name
    assert getattr(ipc_errors, "USER_AGENT_IMMUTABLE", None) == CODE


def _ensured(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = application(tmp_path)
    enable_routines(app, tmp_path)
    assert run_user_agent_ensure(app) == "created"
    agent = local_user_agent(app.agents.list(), OWNER)
    assert agent is not None
    app.handle("agent.create", {"name": "plain"})
    return app, agent


def _attempts(target: str) -> dict[str, dict[str, object]]:
    return {
        "agent.attach": {"name": target},
        "agent.destroy.preview": {"name": target},
        "agent.detach": {"name": target},
        "agent.harness-args.clear": {"name": target, "harness": "codex",
                                     "expectedHarnessArgs": {}},
        "agent.harness-args.set": {"name": target, "harness": "codex", "args": ["--x"],
                                   "expectedHarnessArgs": {}},
        "agent.provider.allow": {"agent": target, "provider": "deepseek"},
        "agent.provider.login": {"actor": target, "provider": "anthropic"},
        "agent.provider.logout": {"actor": target},
        "agent.rename": {"name": target, "displayName": "me"},
        "agent.secret.grant": {"actor": target, "grantId": "g-1", "entryId": "e-1",
                               "fieldName": "f", "environmentNames": ["X"], "revision": 1},
        "agent.start": {"name": target, "harness": "codex"},
        "agent.stop": {"name": target},
        "agent.config.rebind": {"name": target, "source": "elsewhere"},
        "agent.ensure-home": {"name": target},
        "agent.grant": {"actor": target, "grantId": "g-1", "capability": "c", "scope": "s",
                        "revision": 1},
        "agent.migrate.execute": {"agent": target, "migrationId": "m-1"},
        "agent.migrate.preflight": {"agent": target, "manifest": {}},
        "agent.migrate.rollback": {"agent": target, "migrationId": "m-1",
                                   "authorizationWindow": {}},
        "agent.restore-policy": {"name": target, "policy": "always"},
        "agent.resume.restore": {"name": target, "expected": {}, "replacement": {}},
        "agent.runtime-context": {"name": target, "harness": "codex"},
        "agent.unblock": {"name": target},
    }


def _code(app, method: str, params: dict[str, object], origin: str | None) -> str | None:  # type: ignore[no-untyped-def]
    try:
        app.handle(method, params, _trusted_message_origin=origin)
    except DaemonRequestError as error:
        return error.code
    return None


@pytest.mark.parametrize("by", ["name", "uri"])
def test_every_refused_method_answers_the_dedicated_code(tmp_path: Path, by: str) -> None:
    app, agent = _ensured(tmp_path)
    target = USER_AGENT_NAME if by == "name" else agent.uri

    answers = {
        method: _code(app, method, params, "ipc")
        for method, params in _attempts(target).items()
    }

    assert set(answers) == REFUSED_CATALOG | REFUSED_LEGACY
    assert {method: code for method, code in answers.items() if code != CODE} == {}
    current = app.agents.get(USER_AGENT_NAME)
    assert current is not None and current.display == USER_AGENT_NAME
    assert dict(current.capabilities) == {"template": "user"}
    assert app.desired_state.load().harnesses == ()


_AUTHORIZATION = {ipc_errors.CALLER_NOT_AUTHORIZED, ipc_errors.TARGET_NOT_AUTHORIZED}


def _unauthorized_attempts(target: str) -> list[tuple[str, dict[str, object], str | None]]:
    """Each refused method as a worker, plus callers that carry a session identity."""

    attempts = [(method, params, "worker") for method, params in _attempts(target).items()]
    attempts += [
        (method, {**params, "sessionRef": "s-1"}, "ipc")
        for method, params in _attempts(target).items()
    ]
    attempts.append(("agent.provider.login",
                     {"actor": target, "provider": "anthropic", "makeDefault": True}, "worker"))
    return attempts


def test_an_unauthorized_caller_keeps_its_authorization_refusal(tmp_path: Path) -> None:
    """A caller refused for authorization on an ordinary agent is refused the same way here."""

    app, agent = _ensured(tmp_path)
    mismatched = {}
    authorization_refused = set()
    ordinary_attempts = _unauthorized_attempts("plain")
    for index, (method, params, origin) in enumerate(ordinary_attempts):
        ordinary = _code(app, method, params, origin)
        if ordinary not in _AUTHORIZATION:
            continue
        authorization_refused.add((method, origin, "makeDefault" in params))
        _, mine_params, _ = _unauthorized_attempts(agent.uri)[index]
        mine = _code(app, method, mine_params, origin)
        if mine != ordinary:
            mismatched[(method, origin)] = (ordinary, mine)

    assert {("agent.destroy.preview", "worker", False),
            ("agent.harness-args.set", "worker", False),
            ("agent.harness-args.clear", "worker", False),
            ("agent.provider.login", "worker", True)} <= authorization_refused
    assert mismatched == {}


def test_the_destroy_preview_orders_authorization_first(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)

    for origin, expected in (("worker", ipc_errors.CALLER_NOT_AUTHORIZED), ("ipc", CODE)):
        with pytest.raises(DaemonRequestError) as raised:
            app.handle("agent.destroy.preview", {"name": agent.uri},
                       _trusted_message_origin=origin)
        assert raised.value.code == expected, origin


def test_reserved_name_input_stays_an_invalid_argument(tmp_path: Path) -> None:
    app, _agent = _ensured(tmp_path)

    for method, params in (
        ("agent.create", {"name": USER_AGENT_NAME}),
        ("agent.rename", {"name": "plain", "displayName": USER_AGENT_NAME}),
    ):
        with pytest.raises(DaemonRequestError) as raised:
            app.handle(method, params)
        assert raised.value.code == ipc_errors.INVALID_ARGUMENT, method


def test_reads_stay_allowed(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)

    row = app.handle("agent.get", {"name": agent.uri})["agent"]
    assert row["userAgent"] is True
    assert app.handle("agent.harness-args.get", {"name": agent.uri}) is not None
    assert app.handle("agent.grants", {"actor": agent.uri})["ok"] is True


def test_the_restore_policy_list_read_stays_allowed(tmp_path: Path) -> None:
    """``list: true`` is the read form; only exactly ``true`` reads, as in the handler."""

    app, agent = _ensured(tmp_path)

    for target in (USER_AGENT_NAME, agent.uri):
        listed = app.handle("agent.restore-policy", {"list": True, "name": target})
        assert listed["ok"] is True
        assert _code(app, "agent.restore-policy",
                     {"list": 1, "name": target, "policy": "always"}, "ipc") == CODE
