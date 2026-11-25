"""Administration outside ``agent.*`` aimed at the user agent: the same dedicated code.

``teleport.run`` would move the owner's identity off this machine;
``org.acl.grant`` would grant an agent that never runs.  Each refuses the
user agent with ``USER_AGENT_IMMUTABLE`` in its handler, after the checks it
already made, so a caller refused before keeps its refusal.  I7a (owner
item A-1): ``agent.cell.get`` and ``agent.cell.update`` are no longer
refused at the gate; the cell service holds the user agent cell to its
allow-list and its reader to the local operator.  The retired
``adapter.pin``/``authority.set`` methods no longer exist.

``org.acl.revoke`` is not refused (PR 2941 second review): a grant an older
build wrote for the user agent must stay removable, and a revoke grants
nothing.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.daemon.impl.application import DaemonRequestError
from hyprial.daemon.impl.user_agent import USER_AGENT_NAME, local_user_agent, run_user_agent_ensure
from tests.identity.agents.registry.entity.support import (
    OWNER,
    _close_test_applications as _close_test_applications,
    application,
)

CODE = "USER_AGENT_IMMUTABLE"
ADAPTER = "lark-a"


def _ensured(tmp_path: Path):  # type: ignore[no-untyped-def]
    app = application(tmp_path)
    app._adapters = AdapterRuntime(  # type: ignore[arg-type]
        SimpleNamespace(startup_timeout=20.0),
        SimpleNamespace(gateways=[SimpleNamespace(name=ADAPTER)]),
    )
    app._lark_client = None  # type: ignore[assignment]
    assert run_user_agent_ensure(app) == "created"
    agent = local_user_agent(app.agents.list(), OWNER)
    assert agent is not None
    app.handle("agent.create", {"name": "plain"})
    return app, agent


def _code(app, method: str, params: dict[str, object], origin: str | None = "ipc"):  # type: ignore[no-untyped-def]
    try:
        app.handle(method, params, _trusted_message_origin=origin)
    except DaemonRequestError as error:
        return error.code
    return None


@pytest.mark.parametrize("by", ["name", "uri"])
def test_the_cell_methods_reach_the_cell_service_for_the_user_agent(
    tmp_path: Path, by: str
) -> None:
    from tests.support.identity.cell_runtime import registry_cell_runtime

    app, agent = _ensured(tmp_path)
    target = USER_AGENT_NAME if by == "name" else agent.uri

    for method, params in (
        ("agent.cell.get", {"name": target}),
        ("agent.cell.update", {"name": target, "expectedRevision": 1, "members": []}),
    ):
        assert _code(app, method, params) == _code(app, method, {**params, "name": "plain"})
    registry_cell_runtime(app)
    shown = app.handle("agent.cell.get", {"name": target}, _trusted_message_origin="ipc")
    assert shown["cell"]["params"]["agent.template"] == "user"
    assert _code(app, "agent.cell.get", {"name": target}, origin="worker") == CODE


def test_the_cell_update_keeps_the_earlier_answers_first(tmp_path: Path) -> None:
    """An unknown agent still answers AGENT_NOT_FOUND before any cell check."""

    app, _agent = _ensured(tmp_path)

    assert _code(app, "agent.cell.update", {
        "name": "ghost", "expectedRevision": 1, "members": [],
    }) == "AGENT_NOT_FOUND"


def test_retired_pin_and_authority_methods_are_not_served(tmp_path: Path) -> None:
    app, _agent = _ensured(tmp_path)

    for method in ("adapter.pin", "adapter.pins", "adapter.unpin",
                   "authority.get", "authority.list", "authority.set"):
        assert _code(app, method, {}) == "METHOD_NOT_FOUND"


@pytest.mark.parametrize("dry_run", [True, False])
def test_the_user_agent_is_never_teleported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    from hyprial.daemon.impl.request_catalog import teleport

    app, agent = _ensured(tmp_path)
    called: list[object] = []
    monkeypatch.setattr(teleport, "source_service", lambda app: called.append(app))

    assert _code(app, "teleport.run", {"agent": agent.uri, "to": "node-2",
                                        "dryRun": dry_run}) == CODE
    assert called == []


_ORG = "group-acme"


def _open_org_acl_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    from hyprial.daemon.impl.org.network import ipc

    monkeypatch.setattr(ipc, "_org_acl_entry_or_forbidden", lambda *_a, **_k: None)
    monkeypatch.setattr(ipc, "_org_acl_owner_space", lambda *_a, **_k: None)


def test_the_user_agent_takes_no_org_acl_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.daemon.impl.org.network.state import read_org_acl_grants

    app, agent = _ensured(tmp_path)
    _open_org_acl_gate(monkeypatch)

    assert _code(app, "org.acl.grant", {"org": _ORG, "agent": agent.uri, "from": _ORG}) == CODE
    assert read_org_acl_grants(app.hyprial_home) == {}
    assert _code(app, "org.acl.grant", {"org": _ORG, "agent": "plain", "from": _ORG}) is None


@pytest.mark.parametrize("by", ["name", "uri"])
def test_an_older_grant_to_the_user_agent_can_still_be_revoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, by: str
) -> None:
    from hyprial.daemon.impl.org.network.state import add_org_acl_grant, read_org_acl_grants

    app, agent = _ensured(tmp_path)
    _open_org_acl_gate(monkeypatch)
    add_org_acl_grant(Path(app.hyprial_home), _ORG, agent.uri, granted_at_ms=1, until_ms=None)
    assert agent.uri in read_org_acl_grants(app.hyprial_home)[_ORG]

    target = USER_AGENT_NAME if by == "name" else agent.uri
    result = app.handle("org.acl.revoke", {"org": _ORG, "agent": target, "from": _ORG},
                        _trusted_message_origin="ipc")

    assert result["revoked"] is True and result["agent"] == agent.uri
    assert read_org_acl_grants(app.hyprial_home).get(_ORG, {}) == {}


def test_the_org_acl_refusal_keeps_the_entry_refusal_first(tmp_path: Path) -> None:
    app, agent = _ensured(tmp_path)

    for method in ("org.acl.grant", "org.acl.revoke"):
        ordinary = _code(app, method, {"org": _ORG, "agent": "plain", "from": _ORG,
                                       "sessionRef": "s-1"})
        mine = _code(app, method, {"org": _ORG, "agent": agent.uri, "from": _ORG,
                                   "sessionRef": "s-1"})
        assert ordinary == mine != CODE, (method, ordinary, mine)


def test_only_the_grant_declares_the_code() -> None:
    from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY

    declared = {
        name: {error.code for error in METHOD_REGISTRY.get(name).spec.errors}
        for name in ("org.acl.grant", "org.acl.revoke")
    }
    assert CODE in declared["org.acl.grant"]
    assert CODE not in declared["org.acl.revoke"]
