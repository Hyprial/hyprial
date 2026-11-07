"""WP6: ``link.*`` routes dispatch to the wired link service behind the fence.

The router must resolve the actor selector for the read-only queries, require
a real ``LinkAdmin`` signature for the mutations, hand the daemon boundary's
own millisecond clock to ``add``/``drop``, and refuse anything that is not a
canonical agent of this node before the service is ever called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon import (
    IPC_LINK_ADD,
    IPC_LINK_DROP,
    IPC_LINK_LIST,
    IPC_LINK_STATS,
)
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)

ALICE = "agent:op:node-1:alice"
BOB = "agent:op:node-1:bob"
PEER = "agent:op:node-1:carol"


class FakeInbox:
    def pending_messages(self, actor: str) -> tuple[()]:
        return ()

    def fetch_pending(self, actor: str) -> tuple[()]:
        return ()

    def prune_inbox(self) -> list[()]:
        return []

    def outbox_count(self) -> int:
        return 0

    def custody_count(self) -> int:
        return 0


class FakeLinkService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def list_links(self, actor: str) -> dict[str, Any]:
        self.calls.append(("list_links", actor))
        return {"actor": actor, "k": 4, "W": 6, "links": []}

    def stats(self, actor: str) -> dict[str, Any]:
        self.calls.append(("stats", actor))
        return {"actor": actor, "nActive": 1, "k": 4, "W": 6, "links": []}

    def add(self, *, actor: str, peer: str, now_ms: int) -> dict[str, Any]:
        self.calls.append(("add", actor, peer, now_ms))
        return {"actor": actor, "peer": peer, "state": "pending"}

    def drop(self, *, actor: str, peer: str, now_ms: int) -> dict[str, Any]:
        self.calls.append(("drop", actor, peer, now_ms))
        return {"actor": actor, "peer": peer, "state": "reclaiming", "written": False}


def _daemon(
    tmp_path: Path, own_daemon_application: Any, *, wired: bool = True
) -> tuple[DaemonApplication, FakeLinkService]:
    app = own_daemon_application(
        DaemonApplication(
            state_dir=tmp_path / "daemon-state",
            socket_path=tmp_path / "daemon.sock",
            node_id="node-1",
            owner="op",
            **_deep_module_daemon_dependencies(),
        )
    )
    app._inbox = FakeInbox()
    app._harnesses = object()  # type: ignore[assignment]
    app._presence = object()  # type: ignore[assignment]
    app.handle(
        "session.register",
        {
            "actor": "alice",
            "sessionRef": "session-1",
            "cwd": "/work/alice",
            "command": ["claude"],
            "source": "claude-channel",
            "channelConfirmed": True,
        },
    )
    service = FakeLinkService()
    if wired:
        app._link_service = service  # type: ignore[attr-defined]
    return app, service


def test_link_methods_report_daemon_not_ready_without_a_service(
    tmp_path: Path, own_daemon_application: Any
) -> None:
    app, _ = _daemon(tmp_path, own_daemon_application, wired=False)

    for method in (IPC_LINK_LIST, IPC_LINK_STATS):
        with pytest.raises(DaemonRequestError) as caught:
            app.handle(method, {"from": ALICE})
        assert caught.value.code == ipc_errors.DAEMON_NOT_READY
    for method in (IPC_LINK_ADD, IPC_LINK_DROP):
        with pytest.raises(DaemonRequestError) as caught:
            app.handle(method, {"from": ALICE, "peer": PEER})
        assert caught.value.code == ipc_errors.DAEMON_NOT_READY


def test_the_link_methods_are_registered_in_the_stats_registry() -> None:
    # B2: this only pins the cost-counter registration; the dispatch tests
    # below prove the authorization path.
    assert {IPC_LINK_LIST, IPC_LINK_STATS, IPC_LINK_ADD, IPC_LINK_DROP} <= (
        _IPC_STATS_METHODS
    )


def test_read_only_link_queries_resolve_the_actor_selector(
    tmp_path: Path, own_daemon_application: Any
) -> None:
    app, service = _daemon(tmp_path, own_daemon_application)

    listed = app.handle(IPC_LINK_LIST, {"from": "alice"})
    stats = app.handle(IPC_LINK_STATS, {"from": ALICE})

    assert listed == {"ok": True, "actor": ALICE, "k": 4, "W": 6, "links": []}
    assert stats == {
        "ok": True,
        "actor": ALICE,
        "nActive": 1,
        "k": 4,
        "W": 6,
        "links": [],
    }
    assert service.calls == [("list_links", ALICE), ("stats", ALICE)]


def test_operator_on_behalf_of_writes_the_named_agent_link(
    tmp_path: Path, own_daemon_application: Any
) -> None:
    app, service = _daemon(tmp_path, own_daemon_application)

    result = app.handle(
        IPC_LINK_ADD,
        {"from": "user:op", "onBehalfOf": ALICE, "peer": PEER},
    )

    assert result == {"ok": True, "actor": ALICE, "peer": PEER, "state": "pending"}
    (call,) = service.calls
    kind, actor, peer, now_ms = call
    assert (kind, actor, peer) == ("add", ALICE, PEER)
    assert type(now_ms) is int


def test_a_fenced_session_manages_its_own_link(
    tmp_path: Path, own_daemon_application: Any
) -> None:
    app, service = _daemon(tmp_path, own_daemon_application)

    added = app.handle(
        IPC_LINK_ADD, {"from": ALICE, "sessionRef": "session-1", "peer": PEER}
    )
    dropped = app.handle(
        IPC_LINK_DROP, {"from": ALICE, "sessionRef": "session-1", "peer": PEER}
    )

    assert added["state"] == "pending"
    assert dropped == {
        "ok": True,
        "actor": ALICE,
        "peer": PEER,
        "state": "reclaiming",
        "written": False,
    }
    assert [call[0] for call in service.calls] == ["add", "drop"]
    assert all(call[1] == ALICE and call[2] == PEER for call in service.calls)
    assert all(type(call[3]) is int for call in service.calls)


@pytest.mark.parametrize("method", [IPC_LINK_ADD, IPC_LINK_DROP])
def test_an_invalid_session_is_refused_without_calling_the_admin(
    tmp_path: Path, own_daemon_application: Any, method: str
) -> None:
    app, service = _daemon(tmp_path, own_daemon_application)

    with pytest.raises(DaemonRequestError):
        app.handle(method, {"from": ALICE, "sessionRef": "bogus", "peer": PEER})

    assert service.calls == []


@pytest.mark.parametrize("method", [IPC_LINK_ADD, IPC_LINK_DROP])
def test_a_session_cannot_act_for_another_actor(
    tmp_path: Path, own_daemon_application: Any, method: str
) -> None:
    app, service = _daemon(tmp_path, own_daemon_application)

    with pytest.raises(DaemonRequestError):
        app.handle(method, {"from": BOB, "sessionRef": "session-1", "peer": PEER})

    assert service.calls == []


@pytest.mark.parametrize("method", [IPC_LINK_ADD, IPC_LINK_DROP])
def test_a_foreign_or_unbacked_agent_uri_is_refused(
    tmp_path: Path, own_daemon_application: Any, method: str
) -> None:
    app, service = _daemon(tmp_path, own_daemon_application)

    with pytest.raises(DaemonRequestError) as caught:
        app.handle(
            method,
            {"from": "agent:someone-else:other-node:mallory", "peer": PEER},
            _trusted_message_origin="ipc",
        )

    assert caught.value.code == ipc_errors.SENDER_UNRESOLVED
    assert service.calls == []


@pytest.mark.parametrize("method", [IPC_LINK_ADD, IPC_LINK_DROP])
def test_an_owner_without_a_selected_agent_is_refused(
    tmp_path: Path, own_daemon_application: Any, method: str
) -> None:
    app, service = _daemon(tmp_path, own_daemon_application)

    with pytest.raises(DaemonRequestError) as caught:
        app.handle(method, {"from": "user:op", "peer": PEER})

    assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert service.calls == []


@pytest.mark.parametrize("method", [IPC_LINK_ADD, IPC_LINK_DROP])
def test_an_adapter_identity_is_refused(
    tmp_path: Path, own_daemon_application: Any, method: str
) -> None:
    app, service = _daemon(tmp_path, own_daemon_application)

    with pytest.raises(DaemonRequestError) as caught:
        app.handle(method, {"from": "channel:lark:bot", "peer": PEER})

    assert caught.value.code == ipc_errors.CALLER_NOT_AUTHORIZED
    assert service.calls == []


def test_the_admin_methods_receive_keyword_arguments_matching_the_protocol(
    tmp_path: Path, own_daemon_application: Any
) -> None:
    from hyprial.daemon.impl.links import ports

    app, service = _daemon(tmp_path, own_daemon_application)

    app.handle(IPC_LINK_ADD, {"from": "user:op", "onBehalfOf": ALICE, "peer": PEER})

    assert isinstance(service, ports.LinkAdmin)
    (call,) = service.calls
    assert call[0] == "add"


def test_an_undeclared_link_method_has_no_legacy_fallback(
    tmp_path: Path, own_daemon_application: Any
) -> None:
    app, service = _daemon(tmp_path, own_daemon_application)

    with pytest.raises(DaemonRequestError) as caught:
        app.handle("link.unregistered", {"from": ALICE, "peer": PEER})

    assert caught.value.code == ipc_errors.METHOD_NOT_FOUND
    assert service.calls == []


def test_the_registered_handlers_are_the_real_link_functions() -> None:
    from hyprial.daemon.impl import request_catalog
    from hyprial.daemon.impl.request_catalog import registry

    assert request_catalog.link_list is registry.METHOD_REGISTRY.get(
        IPC_LINK_LIST
    ).handler
    assert request_catalog.link_stats is registry.METHOD_REGISTRY.get(
        IPC_LINK_STATS
    ).handler
    assert request_catalog.link_add is registry.METHOD_REGISTRY.get(
        IPC_LINK_ADD
    ).handler
    assert request_catalog.link_drop is registry.METHOD_REGISTRY.get(
        IPC_LINK_DROP
    ).handler


def test_link_schema_rejects_bad_params_before_the_handler(
    tmp_path: Path, own_daemon_application: Any
) -> None:
    app, service = _daemon(tmp_path, own_daemon_application)

    with pytest.raises(DaemonRequestError) as empty_peer:
        app.handle(IPC_LINK_ADD, {"from": ALICE, "peer": ""})
    assert empty_peer.value.code == ipc_errors.INVALID_ARGUMENT
    assert str(empty_peer.value) == "peer must be a non-empty string"

    with pytest.raises(DaemonRequestError) as extra:
        app.handle(
            IPC_LINK_ADD,
            {"from": ALICE, "sessionRef": "session-1", "peer": PEER, "extra": 1},
        )
    assert extra.value.code == ipc_errors.INVALID_ARGUMENT
    assert service.calls == []
