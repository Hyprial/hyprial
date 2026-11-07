"""Slot acceptance: membership changes rebind slots, never the session.

The design's N2 acceptance facts (stable-zenoh-session-slots §5/§7), each
asserted so that removing its guard goes red:

* join, leave and rebind leave ``reconfigure_connect`` uncalled -- the
  transport spy is the ``zenoh.session.closed`` proxy, because every
  rebuild emits exactly one ``closed`` (transport.py); the in-file
  control shows the same join DOES rebuild on the legacy path (today's
  reading: 1);
* a daemon restart keeps the connect set and rebinds the same slots;
* pool exhaustion grows the pool once, raises the alarm, and that growth
  is the one remaining ``reconfigure_connect`` caller.
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

from pathlib import Path
import time

import pytest

import hyprial.daemon.impl.org.network as org_network
import hyprial.daemon.impl.forwarding.slots.reconcile as slot_reconcile
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.bootstrap.discovery import ForwardingEndpoints
from hyprial.daemon.impl.forwarding.slots import (
    SLOT_POOL_ENV,
    slot_socket_path,
    slot_store_for_app,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

CONFIGURED = ("tcp/10.0.0.9:7447",)


class _SlotController:
    """In-process §4 control surface: records calls, keeps slot mappings.

    Speaks the real v5 wire shape: ``map-peer.slot`` is the slot's wire
    id (an absolute socket path on the unix transport), never a bare
    index -- indexes are a daemon-side bookkeeping detail.
    """

    peers_reported = True

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.mapped: dict[str, tuple[str, str]] = {}
        self.fail_map: set[str] = set()
        self.fail_unmap = False
        self.remote_up = True

    def status(self) -> tuple[tuple[str, ...], dict[str, int | str]]:
        if not self.remote_up:
            raise RuntimeError("remote peer unavailable")
        return tuple(sorted(self.mapped)), {
            peer: slot for peer, (_address, slot) in self.mapped.items()
        }

    def map_peer(self, peer: str, address: str, slot: str | None = None) -> int | str:
        assert isinstance(slot, str) and slot.startswith("/"), (
            "slot mode must always name a slot by its wire id"
        )
        self.calls.append(("map", peer, address, slot))
        if peer in self.fail_map:
            raise RuntimeError("sidecar refused map")
        self.mapped[peer] = (address, slot)
        return slot

    def unmap_peer(self, peer: str) -> None:
        if self.fail_unmap:
            raise RuntimeError("sidecar refused unmap")
        self.calls.append(("unmap", peer))
        self.mapped.pop(peer, None)

    def close(self) -> None:
        pass


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def reconfigure_connect(self, endpoints: tuple[str, ...]) -> None:
        self.calls.append(tuple(endpoints))


def _build_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: dict[str, str]):
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    monkeypatch.setenv(SLOT_POOL_ENV, "4")
    monkeypatch.setattr(
        org_network, "directory_peers_for_app", lambda _app: dict(directory)
    )
    controller = _SlotController()
    cell: dict[str, DaemonApplication] = {}
    backend = ForwardingEndpoints(
        controller,
        slots=lambda: slot_store_for_app(cell["app"]).endpoints(),
    )
    application = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state/daemon.sock",
        node_id="slots-node",
        hyprial_home=tmp_path / "home",
        owner="tester",
        forwarding_discovery=backend,
    **_deep_module_daemon_dependencies())
    cell["app"] = application
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        application,
        "_log",
        lambda _level, _component, event, **fields: events.append((event, fields)),
    )
    first = application._discover_peer_endpoints()
    assert first == slot_store_for_app(application).endpoints()
    application._connect_configured = CONFIGURED
    application.zenoh_connect = CONFIGURED + first
    transport = _Transport()
    application._transport = transport  # type: ignore[assignment]
    return application, controller, transport, events, directory


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    built = _build_app(tmp_path, monkeypatch, {"alice/dev-a": "tc://alice-a"})
    application = built[0]
    try:
        yield built
    finally:
        application._transport = None
        application._close()


def _named(events, name):
    return [fields for event, fields in events if event == name]


def _wire_id(application, label):
    """The wire id the controller must have been told for a peer's slot."""

    store = slot_store_for_app(application)
    slot = store.slot_for(label)
    assert slot is not None
    return str(slot_socket_path(application.hyprial_home, slot))


def test_join_leave_and_rebind_never_touch_the_session(app) -> None:
    application, controller, transport, events, directory = app
    store = slot_store_for_app(application)
    fixed = store.endpoints()

    # Join: two more devices arrive.
    directory["bob/dev-b"] = "tc://bob-b"
    directory["carol/dev-c"] = "tc://carol-c"
    application._reconcile_forwarding_endpoints()
    assert (
        "map", "bob/dev-b", "tc://bob-b", _wire_id(application, "bob/dev-b")
    ) in controller.calls
    assert store.slot_for("carol/dev-c") is not None

    # Leave: one device is removed from the directory.
    directory.pop("bob/dev-b")
    application._reconcile_forwarding_endpoints()
    assert ("unmap", "bob/dev-b") in controller.calls
    assert store.slot_for("bob/dev-b") is None

    # Rebind: a device comes back with a new address; same slot, new dial.
    carol_slot = store.slot_for("carol/dev-c")
    directory["carol/dev-c"] = "tc://carol-moved"
    application._reconcile_forwarding_endpoints()
    assert (
        "map", "carol/dev-c", "tc://carol-moved", _wire_id(application, "carol/dev-c")
    ) in controller.calls
    assert store.slot_for("carol/dev-c") == carol_slot

    # The acceptance reading: zero session rebuilds through all of it --
    # every reconfigure_connect emits exactly one zenoh.session.closed,
    # so an empty spy IS "zenoh.session.closed == 0".
    assert transport.calls == []
    assert store.endpoints() == fixed
    assert application.zenoh_connect == CONFIGURED + fixed
    assert _named(events, "zenoh.forwarding.changed") == []
    assert _named(events, "zenoh.slots.bound")
    assert all("address" not in fields for fields in _named(events, "zenoh.slots.bound"))


def test_remote_drop_and_return_heals_without_a_session_rebuild(app) -> None:
    application, controller, transport, events, _directory = app
    application._reconcile_forwarding_endpoints()
    fixed = application.zenoh_connect

    started = time.monotonic()
    controller.remote_up = False
    application._reconcile_forwarding_endpoints()
    assert application._forwarding_status_json()["lastPoll"]["ok"] is False

    controller.remote_up = True
    application._reconcile_forwarding_endpoints()
    assert time.monotonic() - started < 1.0
    assert application._forwarding_status_json()["lastPoll"]["ok"] is True
    assert application.zenoh_connect == fixed
    assert transport.calls == []
    assert _named(events, "zenoh.forwarding.redialed") == []
    assert _named(events, "zenoh.forwarding.changed") == []


def test_sidecar_withdrawn_mapping_is_reapplied_without_session_rebuild(app) -> None:
    application, controller, transport, _events, _directory = app
    application._reconcile_forwarding_endpoints()
    fixed = application.zenoh_connect
    expected = dict(controller.mapped)
    # Lease expiry is sidecar-owned. A fresh lease may arrive before Python
    # observes member=false, so the desired peer set need not change.
    controller.mapped.clear()
    application._reconcile_forwarding_endpoints()
    application._reconcile_forwarding_endpoints()
    assert controller.mapped == expected
    assert application.zenoh_connect == fixed
    assert transport.calls == []


def test_already_absent_unmap_releases_memo_and_slot(app, monkeypatch) -> None:
    from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError

    application, controller, transport, _events, directory = app
    application._reconcile_forwarding_endpoints()
    controller.mapped.clear()
    directory.clear()

    def absent(_peer):
        raise ForwardingSidecarError('unknown peer', code='PEER_UNKNOWN')

    monkeypatch.setattr(controller, 'unmap_peer', absent)
    application._reconcile_forwarding_endpoints()
    assert slot_store_for_app(application).slot_for('alice/dev-a') is None
    directory['alice/dev-a'] = 'tc://alice-a'
    application._reconcile_forwarding_endpoints()
    assert 'alice/dev-a' in controller.mapped
    assert transport.calls == []


@pytest.mark.parametrize('code', ['BAD_REQUEST', 'IPC_TIMEOUT', 'PEER_NOT_ALLOWED'])
def test_other_unmap_errors_keep_mapping_and_assignment(app, monkeypatch, code) -> None:
    from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError

    application, controller, _transport, _events, directory = app
    application._reconcile_forwarding_endpoints()
    directory.clear()

    def refused(_peer):
        raise ForwardingSidecarError('refused', code=code)

    monkeypatch.setattr(controller, 'unmap_peer', refused)
    application._reconcile_forwarding_endpoints()
    assert slot_store_for_app(application).slot_for('alice/dev-a') is not None
    assert 'alice/dev-a' in controller.mapped


def test_a_join_grows_only_when_the_pool_is_full(app) -> None:
    """The metric's falsifiability: a join inside the pool never rebuilds,
    and the ONLY rebuild is a pool-exhaustion growth.

    Replaces the old per-peer-loopback control (that dialing path is
    removed): the red case is now exhaustion, asserted directly in
    ``test_exhaustion_grows_once_alarms_and_is_the_only_rebuild``.  Here we
    pin that an ordinary join leaves the session untouched.
    """

    application, controller, transport, events, directory = app
    directory["bob/dev-b"] = "tc://bob-b"
    application._reconcile_forwarding_endpoints()
    assert transport.calls == []
    assert _named(events, "zenoh.slots.pool_exhausted") == []


def test_a_restart_keeps_the_connect_set_and_the_slots(app, tmp_path, monkeypatch) -> None:
    application, controller, transport, _events, directory = app
    directory["bob/dev-b"] = "tc://bob-b"
    application._reconcile_forwarding_endpoints()
    store = slot_store_for_app(application)
    before = {
        label: store.slot_for(label) for label in ("alice/dev-a", "bob/dev-b")
    }
    fixed = store.endpoints()

    # Restart: in-memory state is gone, the state dir and home survive.
    del application._slot_assignments
    fresh_controller = _SlotController()
    fresh_backend = ForwardingEndpoints(
        fresh_controller,
        slots=lambda: slot_store_for_app(application).endpoints(),
    )
    application._forwarding_discovery = fresh_backend
    application._reconcile_forwarding_endpoints()

    reloaded = slot_store_for_app(application)
    assert reloaded.endpoints() == fixed
    for label, slot in before.items():
        assert reloaded.slot_for(label) == slot
        assert (
            "map", label, directory[label], _wire_id(application, label)
        ) in fresh_controller.calls
    assert transport.calls == []


def test_exhaustion_grows_once_alarms_and_is_the_only_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    monkeypatch.setenv(SLOT_POOL_ENV, "2")
    directory = {"alice/dev-a": "tc://alice-a"}
    monkeypatch.setattr(
        org_network, "directory_peers_for_app", lambda _app: dict(directory)
    )
    controller = _SlotController()
    cell: dict[str, DaemonApplication] = {}
    backend = ForwardingEndpoints(
        controller,
        slots=lambda: slot_store_for_app(cell["app"]).endpoints(),
    )
    application = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state/daemon.sock",
        node_id="exhaust-node",
        hyprial_home=tmp_path / "home",
        owner="tester",
        forwarding_discovery=backend,
    **_deep_module_daemon_dependencies())
    cell["app"] = application
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        application,
        "_log",
        lambda _level, _component, event, **fields: events.append((event, fields)),
    )
    first = application._discover_peer_endpoints()
    assert len(first) == 2
    application.zenoh_connect = first
    transport = _Transport()
    application._transport = transport  # type: ignore[assignment]
    try:
        directory["bob/dev-b"] = "tc://bob-b"
        directory["carol/dev-c"] = "tc://carol-c"
        application._reconcile_forwarding_endpoints()

        store = slot_store_for_app(application)
        assert store.pool_size == 4
        alarms = _named(events, "zenoh.slots.pool_exhausted")
        assert len(alarms) == 1
        assert alarms[0]["previousPoolSize"] == 2
        assert alarms[0]["poolSize"] == 4
        # The growth is dialed through the one remaining rebuild path.
        assert transport.calls == [store.endpoints()]
        assert application.zenoh_connect == store.endpoints()

        # Steady state after the growth: no second rebuild.
        application._reconcile_forwarding_endpoints()
        assert len(transport.calls) == 1
    finally:
        application._transport = None
        application._close()


def test_reconfigure_maps_replacement_before_removing_old_peer(app) -> None:
    """A directory swap never exposes an empty mapped endpoint set."""

    application, controller, _transport, _events, directory = app
    application._reconcile_forwarding_endpoints()
    observed: list[tuple[str, ...]] = []
    real_map = controller.map_peer
    real_unmap = controller.unmap_peer

    def map_observed(peer, address, slot=None):  # type: ignore[no-untyped-def]
        result = real_map(peer, address, slot)
        observed.append(tuple(sorted(controller.mapped)))
        return result

    def unmap_observed(peer):  # type: ignore[no-untyped-def]
        real_unmap(peer)
        observed.append(tuple(sorted(controller.mapped)))

    controller.map_peer = map_observed  # type: ignore[method-assign]
    controller.unmap_peer = unmap_observed  # type: ignore[method-assign]
    directory.clear()
    directory["bob/dev-b"] = "tc://bob-b"

    application._reconcile_forwarding_endpoints()

    assert observed
    assert all(snapshot for snapshot in observed)
    assert tuple(controller.mapped) == ("bob/dev-b",)


def test_a_directory_read_failure_keeps_every_live_mapping(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, controller, _transport, _events, _directory = app
    application._reconcile_forwarding_endpoints()

    def unavailable():
        raise RuntimeError("directory unavailable")

    monkeypatch.setattr(application, "_tailcat_directory_peers", unavailable)

    result = slot_reconcile.reconcile_slot_bindings(
        application, reason="directory-error"
    )

    assert tuple(controller.mapped) == ("alice/dev-a",)
    assert ("unmap", "alice/dev-a") not in controller.calls
    assert result["failed"] == [
        {"operation": "directory", "peer": "alice/dev-a"}
    ]


def test_a_failed_replacement_bind_keeps_the_old_mapping(app) -> None:
    application, controller, _transport, _events, directory = app
    application._reconcile_forwarding_endpoints()
    controller.fail_map.add("bob/dev-b")
    directory.clear()
    directory["bob/dev-b"] = "tc://bob-b"

    result = slot_reconcile.reconcile_slot_bindings(
        application, reason="replacement"
    )

    assert tuple(controller.mapped) == ("alice/dev-a",)
    assert ("unmap", "alice/dev-a") not in controller.calls
    assert result["failed"] == [
        {"operation": "bind", "peer": "bob/dev-b"}
    ]


def test_a_none_replacement_bind_keeps_the_old_mapping(
    app, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, controller, _transport, _events, directory = app
    application._reconcile_forwarding_endpoints()
    real_bind = slot_reconcile.bind_peer_slot

    def bind_or_none(target, label, address):  # type: ignore[no-untyped-def]
        if label == "bob/dev-b":
            return None
        return real_bind(target, label, address)

    monkeypatch.setattr(slot_reconcile, "bind_peer_slot", bind_or_none)
    directory.clear()
    directory["bob/dev-b"] = "tc://bob-b"

    result = slot_reconcile.reconcile_slot_bindings(
        application, reason="replacement"
    )

    assert tuple(controller.mapped) == ("alice/dev-a",)
    assert result["failed"] == [
        {"operation": "bind", "peer": "bob/dev-b"}
    ]


def test_partial_replacement_keeps_only_failed_old_mapping_then_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = {
        "old-a/dev": "tc://old-a",
        "old-b/dev": "tc://old-b",
    }
    application, controller, _transport, _events, directory = _build_app(
        tmp_path, monkeypatch, directory
    )
    try:
        slot_reconcile.reconcile_slot_bindings(application, reason="initial")
        controller.fail_map.add("new-b/dev")
        directory.clear()
        directory.update(
            {
                "new-a/dev": "tc://new-a",
                "new-b/dev": "tc://new-b",
            }
        )

        failed = slot_reconcile.reconcile_slot_bindings(
            application, reason="replacement"
        )

        assert set(controller.mapped) == {"new-a/dev", "old-b/dev"}
        assert failed["failed"] == [
            {"operation": "bind", "peer": "new-b/dev"}
        ]

        controller.fail_map.clear()
        recovered = slot_reconcile.reconcile_slot_bindings(
            application, reason="retry"
        )

        assert set(controller.mapped) == {"new-a/dev", "new-b/dev"}
        assert recovered["failed"] == []
        assert recovered["unbound"] == ["old-b/dev"]
    finally:
        application._transport = None
        application._close()


def test_a_failed_unmap_keeps_the_slot_for_a_retry(app) -> None:
    """unmap-peer first, THEN free: a refused unmap must not hand the slot on."""

    application, controller, _transport, _events, directory = app
    application._reconcile_forwarding_endpoints()
    store = slot_store_for_app(application)
    slot = store.slot_for("alice/dev-a")
    assert slot is not None

    controller.fail_unmap = True
    directory.pop("alice/dev-a")
    application._reconcile_forwarding_endpoints()
    assert store.slot_for("alice/dev-a") == slot

    controller.fail_unmap = False
    application._reconcile_forwarding_endpoints()
    assert store.slot_for("alice/dev-a") is None
    assert ("unmap", "alice/dev-a") in controller.calls


def test_status_views_show_slot_to_peer(app) -> None:
    application, _controller, _transport, _events, directory = app
    directory["bob/dev-b"] = "tc://bob-b"
    application._reconcile_forwarding_endpoints()
    store = slot_store_for_app(application)

    status = application._forwarding_status_json()
    slots = status["slots"]
    assert slots["poolSize"] == store.pool_size
    assert slots["free"] == store.pool_size - 2
    assigned = {entry["peer"]: entry["slot"] for entry in slots["assigned"]}
    assert assigned == {
        "alice/dev-a": store.slot_for("alice/dev-a"),
        "bob/dev-b": store.slot_for("bob/dev-b"),
    }
    for entry in slots["assigned"]:
        assert entry["endpoint"] == store.endpoints()[entry["slot"]]
    by_peer = {entry["peer"]: entry for entry in status["peers"]}
    assert by_peer["bob/dev-b"]["slot"] == store.slot_for("bob/dev-b")
