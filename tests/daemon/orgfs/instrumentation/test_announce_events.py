"""Switch and actor-path coverage for OrgFS announce diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.orgfs.composition.vocabulary import _DirectoryEffectBatch
from hyprial.daemon.impl.orgfs.mesh import OrgFsMesh
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.transport import KeySpace, TransportSample


_ANNOUNCE_EVENTS = {
    "announce.broadcast",
    "announce.buffered",
    "announce.published",
    "announce.received",
    "holder.promoted",
}


class _PutSession:
    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes]] = []

    def put(self, key: str, payload: bytes) -> None:
        self.puts.append((key, payload))


class _Announcer:
    def __init__(self) -> None:
        self.calls = 0

    def announce(self) -> None:
        self.calls += 1


def _sample(node: str, space_id: str) -> TransportSample:
    return TransportSample(
        KeySpace().orgfs_announce(node),
        json.dumps(
            {
                "schemaVersion": 1,
                "type": "orgfs-announce",
                "node": node,
                "durable": True,
                "spaces": [{"spaceId": space_id}],
            }
        ).encode(),
    )


def _exercise_announce_events(
    state_dir: Path, *, enabled: bool
) -> tuple[list[tuple[str, str, dict[str, Any]]], _PutSession]:
    events: list[tuple[str, str, dict[str, Any]]] = []

    def record(level: str, event: str, **fields: Any) -> None:
        events.append((level, event, fields))

    runtime = OrgFsRuntime(
        state_dir,
        node_id="reader-node",
        author="user:reader@example.com",
        logger=record,
        announce_instrumentation_enabled=enabled,
    )
    try:
        runtime._supplier_online = lambda node: node == "live-node"

        buffered = runtime._apply_announcement_command(
            _sample("offline-node", "space-buffered")
        )
        runtime._execute_directory_effect(_DirectoryEffectBatch(tuple(buffered)))

        received = runtime._apply_announcement_command(
            _sample("live-node", "space-promoted")
        )
        runtime._execute_directory_effect(_DirectoryEffectBatch(tuple(received)))

        announcer = _Announcer()
        runtime._session = object()  # read-only presence sentinel; no socket is opened
        runtime._meshes["space-promoted"] = announcer  # type: ignore[assignment]
        runtime._announce_all()
        assert announcer.calls == 1
    finally:
        runtime._session = None
        runtime._meshes.clear()
        runtime.close()

    session = _PutSession()
    mesh = object.__new__(OrgFsMesh)
    mesh.session = session
    mesh.node_id = "owner-node"
    mesh.durable = True
    mesh._keys = KeySpace()
    mesh._announcement_source = lambda: ({"spaceId": "space-promoted"},)
    mesh._logger = record
    mesh._announce_instrumentation_enabled = enabled
    mesh.announce()
    return events, session


def test_announce_instrumentation_switch_is_off_by_default_and_additive(
    tmp_path: Path,
) -> None:
    off_events, off_session = _exercise_announce_events(tmp_path / "off", enabled=False)
    on_events, on_session = _exercise_announce_events(tmp_path / "on", enabled=True)

    assert {event for _level, event, _fields in off_events} & _ANNOUNCE_EVENTS == set()
    assert {event for _level, event, _fields in on_events} & _ANNOUNCE_EVENTS == _ANNOUNCE_EVENTS
    assert len(off_session.puts) == len(on_session.puts) == 1


def test_announce_instrumentation_logger_failure_cannot_change_observed_flow(
    tmp_path: Path,
) -> None:
    def broken_logger(_level: str, _event: str, **_fields: Any) -> None:
        raise RuntimeError("diagnostic sink failed")

    runtime = OrgFsRuntime(
        tmp_path,
        node_id="reader-node",
        author="user:reader@example.com",
        logger=broken_logger,
        announce_instrumentation_enabled=True,
    )
    try:
        runtime._supplier_online = lambda _node: True
        effects = runtime._apply_announcement_command(
            _sample("live-node", "space-promoted")
        )
        runtime._execute_directory_effect(_DirectoryEffectBatch(tuple(effects)))
        assert runtime._holder_snapshot("space-promoted") == ("live-node",)

        announcer = _Announcer()
        runtime._session = object()
        runtime._meshes["space-promoted"] = announcer  # type: ignore[assignment]
        runtime._announce_all()
        assert announcer.calls == 1
    finally:
        runtime._session = None
        runtime._meshes.clear()
        runtime.close()

    session = _PutSession()
    mesh = object.__new__(OrgFsMesh)
    mesh.session = session
    mesh.node_id = "owner-node"
    mesh.durable = True
    mesh._keys = KeySpace()
    mesh._announcement_source = lambda: ({"spaceId": "space-promoted"},)
    mesh._logger = broken_logger
    mesh._announce_instrumentation_enabled = True
    mesh.announce()
    assert len(session.puts) == 1
