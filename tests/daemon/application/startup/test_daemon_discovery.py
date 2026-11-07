"""Bootstrap must not depend on a constant somebody typed in.

`docs/guides/onboarding-checklist.md` used to name HQ's address, and every
joining machine copied it. HQ changing address meant everyone editing by hand
with nothing tracking who had, and the line meant nothing to anyone else
running this software.

These tests pin the replacement, and specifically the properties that make
it survive what is already built: the interface deals in **endpoints** (the
fixed zenoh slot pool the Tailcat sidecar splices -- socket paths, not peer
addresses), and a backend that cannot answer degrades rather than taking
the daemon down with it. Since the stable-zenoh-session-slots design
(2026-10-08) the forwarding backend answers the FIXED slot set: which
peer sits on which slot is binding (the slot reconcile), not discovery,
so the connect set never changes while members come and go.
"""

from __future__ import annotations

from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _app_injections

from hyprial.shell.impl.cli.commands.common import daemon_start
import pytest

from hyprial.daemon.impl.forwarding_config import ForwardingPolicy
from hyprial.daemon.impl.bootstrap.discovery import (
    EndpointDiscovery,
    StaticEndpoints,
    ForwardingEndpoints,
    merge_endpoints,
)


#: The fixed slot pool this backend answers: two unix slots, assertable.
SLOTS = (
    "unixsock-stream/run/hyprial/slots/slot-00",
    "unixsock-stream/run/hyprial/slots/slot-01",
)
#: The slots' wire ids -- what ``map-peer`` names for the socket paths.
SLOT0_PATH = "run/hyprial/slots/slot-00"
SLOT1_PATH = "run/hyprial/slots/slot-01"


class _SlotController:
    """The v5 control surface: peers bind slots named by wire id.

    Speaks the slots-only shape: ``map-peer`` takes the slot's wire id
    (an absolute socket path on the unix transport), ``status`` answers
    {peer: slot}.
    """

    peers_reported = True

    def __init__(self) -> None:
        self.mappings: dict[str, str] = {}
        self.operations: list[tuple[str, ...]] = []
        self.closed = False

    def status(self) -> tuple[tuple[str, ...], dict[str, str]]:
        return tuple(sorted(self.mappings)), dict(self.mappings)

    def map_peer(self, peer: str, address: str, slot: str) -> str:
        self.operations.append(("map", peer, slot))
        self.mappings[peer] = slot
        return slot

    def unmap_peer(self, peer: str) -> None:
        self.operations.append(("unmap", peer))
        self.mappings.pop(peer, None)

    def close(self) -> None:
        self.closed = True


def test_static_and_forwarding_both_satisfy_the_one_interface() -> None:
    """Backends are interchangeable, which is the whole point of the seam."""

    assert isinstance(StaticEndpoints(), EndpointDiscovery)
    backend = ForwardingEndpoints(_SlotController(), slots=lambda: SLOTS)
    assert isinstance(backend, EndpointDiscovery)


def test_forwarding_backend_answers_the_fixed_slot_set_not_peer_addresses() -> None:
    """The connect answer is the slot pool; a bound peer is never named."""

    controller = _SlotController()
    backend = ForwardingEndpoints(controller, slots=lambda: SLOTS)

    backend.bind("owner/peer-two", "tailcat://peer-two", 0)
    found = backend.list_reachable_endpoints()

    assert found == SLOTS
    assert all("peer-two" not in endpoint for endpoint in found)
    assert controller.operations == [("map", "owner/peer-two", SLOT0_PATH)]


def test_forwarding_backend_tracks_peer_add_remove_without_changing_interface() -> None:
    """The design's stable-session property, pinned at the seam: peers
    bind and unbind onto slots, and the answered set never moves."""

    controller = _SlotController()
    backend = ForwardingEndpoints(controller, slots=lambda: SLOTS)
    assert backend.list_reachable_endpoints() == SLOTS

    backend.bind("owner/peer-two", "tailcat://peer-two", 0)
    backend.bind("owner/peer-four", "tailcat://peer-four", 1)
    backend.unbind("owner/peer-two")

    assert backend.list_reachable_endpoints() == SLOTS
    assert controller.operations == [
        ("map", "owner/peer-two", SLOT0_PATH),
        ("map", "owner/peer-four", SLOT1_PATH),
        ("unmap", "owner/peer-two"),
    ]


def test_forwarding_backend_reports_failure_and_keeps_the_slot_set() -> None:
    """A dead sidecar is reported, not faked away: on the unix transport
    a slot's socket path outlives any sidecar incarnation, so the fixed
    set stays answered while zenoh redials (design §5)."""

    failures: list[str] = []

    class Broken(_SlotController):
        def status(self):
            raise ConnectionError("sidecar exited with status 17")

    backend = ForwardingEndpoints(
        Broken(), on_failure=failures.append, slots=lambda: SLOTS
    )
    assert backend.list_reachable_endpoints() == SLOTS
    assert failures == ["sidecar exited with status 17"]
    poll = backend.last_poll
    assert poll is not None and poll["ok"] is False


def test_forwarding_backend_close_is_daemon_owned() -> None:
    controller = _SlotController()
    backend = ForwardingEndpoints(controller, slots=lambda: SLOTS)
    backend.close()
    assert controller.closed is True


def test_daemon_owns_forwarding_backend_through_shutdown(tmp_path, monkeypatch) -> None:
    from hyprial.daemon.impl.application import DaemonApplication

    # Coexist default: pin the discovery half off so the assertion is about
    # the injected forwarding backend alone.
    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    controller = _SlotController()
    backend = ForwardingEndpoints(controller, slots=lambda: SLOTS)
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state/daemon.sock",
        node_id="forward-node",
        hyprial_home=tmp_path / "home",
        owner="tester",
        forwarding_discovery=backend,
        **_app_injections(),
    )
    try:
        assert app._discover_peer_endpoints() == SLOTS
    finally:
        app._close()
    assert controller.closed is True


def test_peer_change_rebinds_slots_and_needs_no_restart(
    tmp_path, monkeypatch
) -> None:
    """The old gap, closed by the design: a peer appearing or leaving
    rebinds a slot and leaves the connect set alone -- no session rebuild,
    no ``zenoh.forwarding.changed``, ``restartRequired`` stays False."""

    from hyprial.daemon.impl.application import DaemonApplication

    monkeypatch.setenv("HYPRIAL_PEER_DISCOVERY", "0")
    controller = _SlotController()
    directory = {"owner/peer-two": "tailcat://peer-two"}
    monkeypatch.setattr(
        DaemonApplication, "_tailcat_directory_peers", lambda self: dict(directory)
    )
    backend = ForwardingEndpoints(controller, slots=lambda: SLOTS)
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state/daemon.sock",
        node_id="forward-node",
        hyprial_home=tmp_path / "home",
        owner="tester",
        forwarding_discovery=backend,
        **_app_injections(),
    )
    events: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        app,
        "_log",
        lambda _level, _component, event, **fields: events.append((event, fields)),
    )
    try:
        initial = app._discover_peer_endpoints()
        app.zenoh_connect = initial
        app._reconcile_forwarding_endpoints()
        assert controller.mappings == {"owner/peer-two": SLOT0_PATH}

        directory.clear()
        directory["owner/peer-three"] = "tailcat://peer-three"
        app._reconcile_forwarding_endpoints()
        status = app._forwarding_status_json()
    finally:
        app._close()

    assert not [event for event, _ in events if event == "zenoh.forwarding.changed"]
    assert status["endpoints"] == list(SLOTS)
    assert status["restartRequired"] is False
    assert {entry["peer"] for entry in status["slots"]["assigned"]} == {
        "owner/peer-three"
    }


def test_configured_endpoints_are_tried_before_discovered_ones() -> None:
    """An explicitly named peer should not have to race a directory of twenty."""

    merged = merge_endpoints(("tcp/pinned:7447",), ("tcp/found:7447",))

    assert merged[0] == "tcp/pinned:7447"


def test_the_same_endpoint_from_two_sources_appears_once() -> None:
    """Configuration and discovery overlap constantly -- HQ is in both."""

    merged = merge_endpoints(
        ("tcp/hq:7447", "tcp/a:7447"),
        ("tcp/hq:7447", "tcp/b:7447"),
    )

    assert merged == ("tcp/hq:7447", "tcp/a:7447", "tcp/b:7447")


def test_discovery_never_replaces_configuration() -> None:
    """The property that makes this safe to turn on.

    A node with configured endpoints keeps every one of them whatever the
    directory says, so enabling discovery cannot disconnect a node that works
    today. That is what allows this to ship without a coordinated migration.
    """

    configured = ("tcp/hq:7447",)

    assert merge_endpoints(configured, ()) == configured
    assert merge_endpoints(configured, ("tcp/other:7447",))[0] == "tcp/hq:7447"


def test_a_command_backend_supplies_arbitrary_endpoints(tmp_path) -> None:
    """The escape hatch, and what makes a mesh testable on one machine."""

    from hyprial.daemon.impl.bootstrap.discovery import CommandEndpoints

    script = tmp_path / "dir.sh"
    script.write_text(
        "#!/bin/sh\necho tcp/127.0.0.1:7801\necho tcp/127.0.0.1:7802\n"
    )
    script.chmod(0o755)

    found = CommandEndpoints((str(script),)).list_reachable_endpoints()

    assert found == ("tcp/127.0.0.1:7801", "tcp/127.0.0.1:7802")


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(("/nonexistent/directory-command",), id="missing"),
        pytest.param(("false",), id="nonzero-exit"),
        pytest.param((), id="empty-command"),
    ],
)
def test_a_command_backend_that_fails_stays_silent(command) -> None:
    """Same contract as every other backend: it may not break startup."""

    from hyprial.daemon.impl.bootstrap.discovery import CommandEndpoints

    assert CommandEndpoints(command).list_reachable_endpoints() == ()


def test_the_switch_defaults_to_on_and_a_typo_still_raises() -> None:
    """On by default, because a dormant fix reads as a broken fix.

    The typo behaviour matters once the default is on: an operator turning
    discovery OFF for a reason must not be silently overridden by a
    misspelling.
    """

    import os

    from hyprial.daemon.impl.transport import zenoh_environment_flag

    name = "HYPRIAL_TEST_DISCOVERY_FLAG"
    os.environ.pop(name, None)
    assert zenoh_environment_flag(name, default=True) is True

    os.environ[name] = "0"
    try:
        assert zenoh_environment_flag(name, default=True) is False
    finally:
        os.environ.pop(name, None)

    os.environ[name] = "defintely"
    try:
        with pytest.raises(ValueError):
            zenoh_environment_flag(name, default=True)
    finally:
        os.environ.pop(name, None)


def test_a_node_with_no_configured_endpoints_is_not_isolated_if_discovery_finds_some() -> None:
    """The isolation warning must ask about EFFECTIVE endpoints, not configured ones."""

    from hyprial.daemon.impl.bootstrap.discovery import StaticEndpoints, merge_endpoints

    configured: tuple[str, ...] = ()
    discovered = StaticEndpoints(("tcp/127.0.0.1:7801",)).list_reachable_endpoints()
    effective = merge_endpoints(configured, discovered)

    assert not configured
    assert effective, "discovery supplied endpoints, so the node is not isolated"


def test_a_node_with_neither_source_really_is_isolated() -> None:
    """The other direction, so the assertion above cannot pass vacuously."""

    from hyprial.daemon.impl.bootstrap.discovery import StaticEndpoints, merge_endpoints

    effective = merge_endpoints((), StaticEndpoints(()).list_reachable_endpoints())

    assert not effective


def test_there_is_no_host_tailnet_to_derive_a_listen_from_any_more() -> None:
    """Since the cutover, listen derivation is an always-None seam.

    Inbound reachability belongs to the Tailcat sidecar's loopback listener,
    not to an address this process could bind.
    """

    from hyprial.daemon.impl import application

    derived = application.DaemonApplication._derive_listen_endpoint(
        object()  # type: ignore[arg-type]
    )

    assert derived is None


def test_the_launcher_turns_the_flags_into_a_one_launch_override() -> None:
    """--listen must still reach the daemon now that nothing is persisted."""

    from pathlib import Path

    source = (
        Path(daemon_start.__file__)
    ).read_text()
    launcher = source[source.index("def _launch_daemon_process") :]
    # The launcher is now two functions (the public wrapper plus the locked
    # body that actually writes the overrides) and owns the end of its module.
    assert "def _launch_daemon_process_locked" in launcher

    assert 'environment["HYPRIAL_ZENOH_LISTEN"] = listen' in launcher
    assert 'environment["HYPRIAL_ZENOH_CONNECT"] = connect' in launcher


def test_init_no_longer_writes_endpoints_to_desired_state() -> None:
    """A machine's address is not hyprial's to remember."""

    from pathlib import Path

    source = (Path(__file__).resolve().parents[4] / 'src/hyprial/cli.py').read_text()

    assert "update_zenoh_endpoints(" not in source, (
        "init must not persist zenoh endpoints"
    )


# --- the last poll is kept for status: who the sidecar saw, and why not ----


def test_forwarding_backend_keeps_its_last_poll_for_status() -> None:
    controller = _SlotController()
    controller.connection_telemetry = {  # type: ignore[attr-defined]
        "ringCapacity": 16, "slots": []
    }
    controller.derp_map_url = "https://derpmap.example/map.json"  # type: ignore[attr-defined]
    controller.derp_map_source = "configured"  # type: ignore[attr-defined]
    backend = ForwardingEndpoints(controller, slots=lambda: SLOTS)
    assert backend.last_poll is None

    backend.bind("owner/peer-two", "tailcat://peer-two", 0)
    backend.bind("owner/peer-three", "tailcat://peer-three", 1)
    backend.list_reachable_endpoints()

    poll = backend.last_poll
    assert poll is not None
    assert poll["ok"] is True and poll["error"] is None
    assert poll["peersReported"] is True
    assert poll["connectionTelemetry"] == {"ringCapacity": 16, "slots": []}
    assert poll["derpMapURL"] == "https://derpmap.example/map.json"
    assert poll["derpMapSource"] == "configured"
    # The bound peers and the slot each one dials, keyed by label.
    assert poll["peers"] == {
        "owner/peer-three": SLOTS[1],
        "owner/peer-two": SLOTS[0],
    }
    assert isinstance(poll["atMs"], int)


def test_forwarding_backend_last_poll_records_why_it_failed() -> None:
    class Broken(_SlotController):
        def status(self):
            raise ConnectionError("sidecar exited with status 17")

    backend = ForwardingEndpoints(Broken(), slots=lambda: SLOTS)
    assert backend.list_reachable_endpoints() == SLOTS

    poll = backend.last_poll
    assert poll is not None
    assert poll["ok"] is False
    assert poll["error"] == "sidecar exited with status 17"
    assert "peers" not in poll


def test_forwarding_status_names_each_peer_and_whether_it_was_dialed(tmp_path) -> None:
    from hyprial.daemon.impl.application import DaemonApplication

    controller = _SlotController()
    controller.connection_telemetry = {  # type: ignore[attr-defined]
        "ringCapacity": 16, "slots": []
    }
    controller.derp_map_url = "https://derpmap.example/map.json"  # type: ignore[attr-defined]
    controller.derp_map_source = "configured"  # type: ignore[attr-defined]
    backend = ForwardingEndpoints(controller, slots=lambda: SLOTS)
    backend.bind("owner/peer-two", "tailcat://peer-two", 0)
    backend.bind("owner/peer-three", "tailcat://peer-three", 1)
    effective = backend.list_reachable_endpoints()
    app = object.__new__(DaemonApplication)
    app._forwarding_supervisor = None
    app._forwarding_discovery = backend
    app._forwarding_effective = effective
    app._forwarding_policy = ForwardingPolicy("manual", "default")
    app._forwarding_unavailable = None
    app.hyprial_home = tmp_path
    # The session opened when only the first slot was dialed: a peer on a
    # later slot is visible, and visibly NOT dialed.
    app._forwarding_dialed = (SLOTS[0],)

    status = app._forwarding_status_json()

    assert status["lastPoll"]["ok"] is True
    assert status["lastPoll"]["peersReported"] is True
    assert status["lastPoll"]["peerCount"] == 2
    assert status["connectionTelemetry"] == {"ringCapacity": 16, "slots": []}
    assert status["lastPoll"]["derpMapURL"] == "https://derpmap.example/map.json"
    assert status["lastPoll"]["derpMapSource"] == "configured"
    assert status["derpMapURL"] == "https://derpmap.example/map.json"
    assert status["derpMapSource"] == "configured"
    assert status["peers"] == [
        {"peer": "owner/peer-three", "endpoint": SLOTS[1], "dialed": False},
        {"peer": "owner/peer-two", "endpoint": SLOTS[0], "dialed": True},
    ]
    assert status["restartRequired"] is True


def test_forwarding_status_keeps_capability_absent_distinct(tmp_path) -> None:
    from hyprial.daemon.impl.application import DaemonApplication

    controller = _SlotController()
    controller.connection_telemetry = None  # type: ignore[attr-defined]
    backend = ForwardingEndpoints(controller, slots=lambda: SLOTS)
    effective = backend.list_reachable_endpoints()
    app = object.__new__(DaemonApplication)
    app._forwarding_supervisor = None
    app._forwarding_discovery = backend
    app._forwarding_effective = effective
    app._forwarding_policy = ForwardingPolicy("manual", "default")
    app._forwarding_unavailable = None
    app._forwarding_dialed = ()
    app.hyprial_home = tmp_path

    status = app._forwarding_status_json()

    assert status["connectionTelemetry"] == {
        "state": "unavailable",
        "reason": "capability-absent",
    }
