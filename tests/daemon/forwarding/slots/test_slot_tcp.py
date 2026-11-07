"""The tcp slot transport: the Windows scheme, exercised on any platform.

zenoh 1.9.0 ships no unixsock-stream implementation on Windows (the link
crate is cfg'd to ``target_family = "unix"``;
notes/windows-unixsock-2026-10-08.md), so Windows slots are loopback TCP
listeners with kernel-assigned ports: ``up.slots`` carries grow specs,
the sidecar reports the resolved ``tcp:127.0.0.1:<port>`` ids, and the
daemon builds the connect set from the report instead of deriving it
from the home.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.bootstrap.discovery import ForwardingEndpoints
from hyprial.daemon.impl.forwarding.slots import (
    SLOT_TRANSPORT_ENV,
    TCP_SLOT_GROW_SPEC,
    SlotAssignments,
    slot_locator,
    slot_specs,
    slot_transport,
)


def test_transport_defaults_follow_the_platform(monkeypatch) -> None:
    monkeypatch.delenv(SLOT_TRANSPORT_ENV, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert slot_transport() == "tcp"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert slot_transport() == "unix"
    monkeypatch.setenv(SLOT_TRANSPORT_ENV, "tcp")
    assert slot_transport() == "tcp"
    monkeypatch.setenv(SLOT_TRANSPORT_ENV, "quic")
    with pytest.raises(ValueError):
        slot_transport()


def test_tcp_specs_and_locators(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(SLOT_TRANSPORT_ENV, "tcp")
    assert slot_specs(tmp_path, 3) == (TCP_SLOT_GROW_SPEC,) * 3
    # Nothing is statically derivable: ports are per-incarnation, so the
    # static fallback set is empty and the live backend answers instead.
    assert SlotAssignments(tmp_path, base_size=3).endpoints() == ()
    assert slot_locator("tcp:127.0.0.1:51230") == "tcp/127.0.0.1:51230"
    assert slot_locator("/a/s/slot-00") == "unixsock-stream//a/s/slot-00"


class _TcpController:
    """A v5 control surface whose slots resolve like the real sidecar's."""

    peers_reported = True

    def __init__(self, pool: int) -> None:
        self.next_port = 51230
        self.slots_reported = tuple(self._resolve() for _ in range(pool))
        self.calls: list[tuple[object, ...]] = []

    def _resolve(self) -> str:
        self.next_port += 1
        return f"tcp:127.0.0.1:{self.next_port}"

    def status(self) -> tuple[tuple[str, ...], dict[str, int | str]]:
        return (), {}

    def map_peer(self, peer: str, address: str, slot: str | None = None) -> int | str:
        assert isinstance(slot, str) and slot.startswith("tcp:")
        resolved = self._resolve() if slot == TCP_SLOT_GROW_SPEC else slot
        if resolved not in self.slots_reported:
            self.slots_reported = self.slots_reported + (resolved,)
        self.calls.append(("map", peer, address, slot, resolved))
        return resolved

    def unmap_peer(self, peer: str) -> None:
        self.calls.append(("unmap", peer))

    def close(self) -> None:
        pass


def _backend(controller: _TcpController) -> ForwardingEndpoints:
    return ForwardingEndpoints(
        controller,
        slots=lambda: (),  # the static set: empty on tcp
        slot_transport="tcp",
    )


def test_endpoints_come_from_the_sidecar_report(tmp_path: Path) -> None:
    controller = _TcpController(pool=2)
    backend = _backend(controller)
    assert backend.slot_mode
    assert backend.list_reachable_endpoints() == tuple(
        slot_locator(slot_id) for slot_id in controller.slots_reported
    )


def test_bind_names_the_reported_id_and_growth_records_the_answer(
    tmp_path: Path,
) -> None:
    controller = _TcpController(pool=2)
    backend = _backend(controller)
    reported = controller.slots_reported

    # An in-pool index dials the reported id for that index.
    assert backend.bind("alice/dev-a", "tc://alice", 1)
    assert controller.calls[-1] == (
        "map", "alice/dev-a", "tc://alice", reported[1], reported[1]
    )
    # Idempotent per incarnation, like the unix transport.
    assert not backend.bind("alice/dev-a", "tc://alice", 1)

    # An index beyond the report is a growth bind: the grow spec goes
    # out, the resolved answer joins the table and the connect set.
    assert backend.bind("late/dev-x", "tc://late", 2)
    grown_call = controller.calls[-1]
    assert grown_call[:4] == ("map", "late/dev-x", "tc://late", TCP_SLOT_GROW_SPEC)
    resolved = grown_call[4]
    locators = backend.slot_locators()
    assert locators[2] == slot_locator(resolved)
    assert slot_locator(resolved) in backend.list_reachable_endpoints()

    # A rebind of the grown index names the resolved id, not the spec.
    assert backend.bind("late/dev-x", "tc://late-moved", 2)
    assert controller.calls[-1] == (
        "map", "late/dev-x", "tc://late-moved", resolved, resolved
    )


def test_tcp_slot_ports_are_persisted_and_reused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(SLOT_TRANSPORT_ENV, "tcp")
    store = SlotAssignments(tmp_path, base_size=2)
    reported = ("tcp:127.0.0.1:51230", "tcp:127.0.0.1:51231")
    store.record_tcp_ports(reported)
    assert store.specs() == reported

    reloaded = SlotAssignments(tmp_path, base_size=2)
    assert reloaded.specs() == reported


def test_forwarding_backend_persists_the_sidecar_reported_tcp_ports() -> None:
    controller = _TcpController(pool=2)
    seen: list[tuple[str, ...]] = []
    backend = ForwardingEndpoints(
        controller,
        slots=lambda: (),
        slot_transport="tcp",
        on_slots_reported=seen.append,
    )
    backend.list_reachable_endpoints()
    assert seen == [controller.slots_reported]


def test_persisted_tcp_port_fallback_is_reported_and_logged(
    tmp_path: Path, monkeypatch
) -> None:
    from hyprial.daemon.impl.application.netendpoints.forwarding import (
        record_tcp_slot_ports,
    )

    monkeypatch.setenv(SLOT_TRANSPORT_ENV, "tcp")

    class App:
        hyprial_home = tmp_path

        def __init__(self) -> None:
            self.events: list[tuple[str, str, str, dict[str, object]]] = []

        def _log(self, level, scope, event, **fields) -> None:
            self.events.append((level, scope, event, fields))

    app = App()
    record_tcp_slot_ports(app, ("tcp:127.0.0.1:51230",))
    assert app.events == []

    record_tcp_slot_ports(app, ("tcp:127.0.0.1:51231",))
    assert app.events == [
        (
            "warn",
            "zenoh",
            "zenoh.slots.port_fallback",
            {
                "slot": 0,
                "requested": "tcp:127.0.0.1:51230",
                "reported": "tcp:127.0.0.1:51231",
                "detail": (
                    "persisted tcp slot port was unavailable; "
                    "using the reported port"
                ),
            },
        )
    ]
