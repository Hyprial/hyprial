"""Cross-node work-item births must survive the owner's strict chain validation.

Defect archive: hq notes/defect-work-item-birth-2026-10-09.md.  A member
daemon's ``work.add`` reached the owner as ``created``(empty) + ``content``
versions whenever the owner caught up through the sync pull (pages flatten
documents meta/tree/content first), and the work-items chain validation --
which requires the birth version itself to parse -- rejected the entry
permanently.  This module drives two real daemon applications over loopback
zenoh and asserts the owner-side projection stays a single atomic birth.
"""

from __future__ import annotations

from tests.support.tailnet_daemon import bind_admitted

import socket
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.application.messaging import work_items as work_commands
from hyprial.daemon.impl.transport import ZenohConfig, ZenohTransport
from hyprial.shell.impl.daemon_wiring import (
    daemon_dependencies as _deep_module_daemon_dependencies,
)

T = TypeVar("T")


class _FakeInbox:
    def pending_messages(self, _actor: str) -> tuple[()]:
        return ()


class _FakePresence:
    def online_actors(self) -> tuple[()]:
        return ()


class _FakeHarnesses:
    def __init__(self) -> None:
        self.refs: dict[tuple[str, str], str | None] = {}
        self.identities: dict[tuple[str, str], tuple[int, str]] = {}

    def projected_worker_session_refs(self) -> dict[tuple[str, str], str | None]:
        return dict(self.refs)

    def projected_worker_process_identities(
        self,
    ) -> dict[tuple[str, str], tuple[int, str]]:
        return dict(self.identities)

    def status(self) -> tuple[()]:
        return ()


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _eventually(action: Callable[[], T], *, timeout: float = 10.0) -> T:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - retry until the deadline
            last_error = exc
            time.sleep(0.05)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true")


def _build_app(
    own_daemon_application,  # type: ignore[no-untyped-def]
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    pid: int,
) -> DaemonApplication:
    app = own_daemon_application(
        DaemonApplication(
            state_dir=tmp_path / f"{name}-state",
            socket_path=tmp_path / f"{name}.sock",
            hyprial_home=tmp_path / f"{name}-home",
            node_id=f"node-{name}",
            owner=name,
            **_deep_module_daemon_dependencies(),
        )
    )
    app._inbox = _FakeInbox()  # type: ignore[assignment]
    app._harnesses = _FakeHarnesses()  # type: ignore[assignment]
    app._presence = _FakePresence()  # type: ignore[assignment]
    app._restore_done.set()
    app._ipc_peer.pid = pid
    monkeypatch.setattr(
        work_commands,
        "_read_process_parent",
        lambda value: 1 if value == pid else 0,
        raising=False,
    )
    monkeypatch.setattr(
        work_commands,
        "process_birth_identity",
        lambda value: f"birth:{value}",
        raising=False,
    )
    return app


@pytest.fixture(autouse=True)
def isolated_daemon_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")


def test_member_work_add_birth_is_valid_on_owner_side(
    tmp_path,
    own_daemon_application,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _build_app(
        own_daemon_application, tmp_path, monkeypatch, name="owner", pid=9100
    )
    member = _build_app(
        own_daemon_application, tmp_path, monkeypatch, name="member", pid=9200
    )
    endpoint = f"tcp/127.0.0.1:{_port()}"
    owner_transport = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    member_transport = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    try:
        online = {"node-owner", "node-member"}.__contains__
        owner_runtime = owner._require_orgfs_runtime()
        member_runtime = member._require_orgfs_runtime()
        # Cold loopback replicas model the explicitly admitted private path.
        bind_admitted(owner_runtime, owner_transport, supplier_online=online)
        bind_admitted(
            member_runtime, member_transport,
            supplier_online=online,
            holder_candidates=lambda: ("node-owner",),
        )

        space_id = owner.handle("orgfs.create", {"name": "work"})["space_id"]
        owner.handle(
            "orgfs.invite",
            {"spaceId": space_id, "user": "user:member", "mode": "rw"},
        )
        owner_runtime._meshes[space_id].announce()
        _eventually(lambda: member.handle("orgfs.join", {"spaceId": space_id}))

        # Hold the member's live pushes so the owner catches up through the
        # sync pull -- the production trigger that splits a cross-node birth.
        member_runtime._meshes[space_id].broadcast_records = (  # type: ignore[method-assign]
            lambda records: 0
        )

        added: dict[str, Any] = member.handle(
            "work.add",
            {
                "spaceId": space_id,
                "title": "member-created work",
                "checklist": ["ship the atomic birth"],
                "depends": [],
                "actor": "user:member",
            },
        )
        item_id = added["item"]["id"]

        imported = owner_runtime._meshes[space_id].sync_from("node-member")
        assert imported > 0

        def owner_history() -> list[tuple[int, str]]:
            resolved = owner.handle(
                "orgfs.resolve",
                {"spaceId": space_id, "path": f"work/{item_id}.md"},
            )
            node_id = resolved["nodes"][0]["node_id"]
            page = owner.handle(
                "orgfs.history", {"spaceId": space_id, "node": f"id:{node_id}"}
            )["history"]
            return [(entry["node"]["size"], entry["changed"]) for entry in page]

        def owner_show() -> dict[str, Any]:
            value = owner.handle(
                "work.show",
                {"spaceId": space_id, "itemId": item_id, "actor": "user:owner"},
            )
            assert value["item"].get("invalid") is not True, value["item"]
            return value

        _eventually(owner_show)
        history = _eventually(owner_history)
        birth_size = history[0][0]
        assert history == [(birth_size, "created")], (
            "owner-side birth must be a single created-with-content version, "
            f"got {history}"
        )
        assert birth_size > 0, "birth entry carries the work item document"

        listed = owner.handle("work.ls", {"spaceId": space_id, "actor": "user:owner"})
        entries = [item for item in listed["items"] if item["id"] == item_id]
        assert len(entries) == 1
        assert entries[0].get("invalid") is not True, entries[0]

        # Follow-up member operations must stay chain-valid on the owner side.
        member.handle(
            "work.note",
            {
                "spaceId": space_id,
                "itemId": item_id,
                "text": "member note",
                "actor": "user:member",
            },
        )
        member.handle(
            "work.check",
            {
                "spaceId": space_id,
                "itemId": item_id,
                "number": 1,
                "actor": "user:member",
            },
        )
        member.handle(
            "work.status",
            {
                "spaceId": space_id,
                "itemId": item_id,
                "status": "doing",
                "actor": "user:member",
            },
        )
        owner_runtime._meshes[space_id].sync_from("node-member")

        final = _eventually(owner_show)
        assert final["item"]["status"] == "doing"
        checklist = final["item"]["checklist"]
        assert checklist and checklist[0]["done"] is True
        final_history = owner_history()
        assert final_history[-1] == (birth_size, "created")
        assert all(changed == "content" for _size, changed in final_history[:-1])
    finally:
        member._close()
        member._close_home_lease()
        owner._close()
        owner._close_home_lease()
        member_transport.close()
        owner_transport.close()
