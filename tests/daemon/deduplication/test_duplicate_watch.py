"Duplicate-instance watch: generation-bearing liveliness on the mesh.\n\nNetwork tests reuse the established pattern from tests.daemon.transport.contract.test_transport_contract.py\n(real ZenohTransport over 127.0.0.1); de-duplication and delete-handling\ntests drive the component's own ``_on_sample`` seam with real\n``TransportSample`` values -- no fabricated session shape.\n"

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import socket
import time
from typing import Any

from hyprial.daemon.impl.lifecycle.duplicate_watch import (
    DUPLICATE_INSTANCE_EVENT,
    DuplicateInstanceWatch,
)
from hyprial.daemon.impl.transport.api import TransportSample
from hyprial.daemon.impl.transport.keys import KeySpace
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

from hyprial.daemon.impl.transport.zenoh.config import ZenohConfig
from hyprial.daemon.impl.transport.zenoh.transport import ZenohTransport


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Events:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, level: str, event: str, **fields: Any) -> None:
        self.records.append((level, event, fields))

    def duplicates(self) -> list[dict[str, Any]]:
        return [
            fields
            for level, event, fields in self.records
            if event == DUPLICATE_INSTANCE_EVENT
        ]


def _wait_for(predicate: Any, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_two_generations_of_one_node_alert_on_both_sides() -> None:
    """Acceptance 1, mesh arm: same node identity, different generations --
    both daemons emit the error event and expose it via status_payload."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    one = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    two = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    events_one, events_two = _Events(), _Events()
    watch_one = DuplicateInstanceWatch(one, "node-x", "gen-one", logger=events_one)
    watch_two = DuplicateInstanceWatch(two, "node-x", "gen-two", logger=events_two)
    try:
        assert _wait_for(lambda: events_one.duplicates() and events_two.duplicates())

        (level_one, _, fields_one) = next(
            record for record in events_one.records if record[1] == DUPLICATE_INSTANCE_EVENT
        )
        assert level_one == "error"
        assert fields_one["peerGeneration"] == "gen-two"
        assert fields_one["generation"] == "gen-one"
        assert fields_one["nodeId"] == "node-x"

        fields_two = events_two.duplicates()[0]
        assert fields_two["peerGeneration"] == "gen-one"

        assert watch_one.status_payload() == {
            "active": True,
            "meshPeerGenerations": ["gen-two"],
        }
        assert watch_two.status_payload() == {
            "active": True,
            "meshPeerGenerations": ["gen-one"],
        }
    finally:
        watch_two.close()
        watch_one.close()
        two.close()
        one.close()


def test_same_generation_reconnect_does_not_alert() -> None:
    """Acceptance 1, reconnect arm (批复①条件3): a same-process reconnect
    re-declares the same node identity AND the same generation -- that is
    not a duplicate and must stay silent on both sides."""

    endpoint = f"tcp/127.0.0.1:{_port()}"
    one = ZenohTransport(ZenohConfig(listen=(endpoint,)))
    two = ZenohTransport(ZenohConfig(connect=(endpoint,)))
    events_one, events_two = _Events(), _Events()
    watch_one = DuplicateInstanceWatch(one, "node-x", "gen-same", logger=events_one)
    watch_two = DuplicateInstanceWatch(two, "node-x", "gen-same", logger=events_two)
    try:
        # Negative assertion: give liveliness propagation a generous window,
        # then require silence. (No event can legitimately arrive late here
        # -- the only samples on the wire carry the own generation.)
        time.sleep(1.0)
        assert events_one.records == []
        assert events_two.records == []
        assert watch_one.status_payload()["active"] is False
        assert watch_two.status_payload()["active"] is False
    finally:
        watch_two.close()
        watch_one.close()
        two.close()
        one.close()


def _sample(node: str, generation: str, *, kind: str = "put") -> TransportSample:
    return TransportSample(
        key=KeySpace().daemon_liveliness(node, generation), payload=b"", kind=kind
    )


def test_one_alert_per_peer_generation_for_the_process_lifetime() -> None:
    """批复①条件2: repeated liveliness events for one peer generation must
    not each raise an error -- one alert per distinct peer generation."""

    transport = ZenohTransport(ZenohConfig(listen=(f"tcp/127.0.0.1:{_port()}",)))
    events = _Events()
    watch = DuplicateInstanceWatch(transport, "node-x", "gen-self", logger=events)
    try:
        watch._on_sample(_sample("node-x", "gen-peer"))
        watch._on_sample(_sample("node-x", "gen-peer"))
        watch._on_sample(_sample("node-x", "gen-other"))
        watch._on_sample(_sample("node-x", "gen-peer"))

        assert [fields["peerGeneration"] for fields in events.duplicates()] == [
            "gen-peer",
            "gen-other",
        ]
        assert watch.duplicates == ("gen-other", "gen-peer")
    finally:
        watch.close()
        transport.close()


def test_delete_neither_alerts_nor_rearms_a_generation() -> None:
    """A departing peer is not a duplicate, and a flapping peer must not
    re-alert on every reconnect of the same generation."""

    transport = ZenohTransport(ZenohConfig(listen=(f"tcp/127.0.0.1:{_port()}",)))
    events = _Events()
    watch = DuplicateInstanceWatch(transport, "node-x", "gen-self", logger=events)
    try:
        watch._on_sample(_sample("node-x", "gen-peer"))
        watch._on_sample(_sample("node-x", "gen-peer", kind="delete"))
        watch._on_sample(_sample("node-x", "gen-peer"))

        assert len(events.duplicates()) == 1
    finally:
        watch.close()
        transport.close()


def test_own_generation_sample_is_discounted() -> None:
    """Mutation guard for the generation comparison: feeding the watch its
    own generation must never alert (history replay hits this path)."""

    transport = ZenohTransport(ZenohConfig(listen=(f"tcp/127.0.0.1:{_port()}",)))
    events = _Events()
    watch = DuplicateInstanceWatch(transport, "node-x", "gen-self", logger=events)
    try:
        watch._on_sample(_sample("node-x", "gen-self"))
        assert events.records == []
    finally:
        watch.close()
        transport.close()


def test_daemon_liveliness_keys_roundtrip_arbitrary_node_names() -> None:
    keys = KeySpace()
    generation = "98cf37d5f32442209af5b526314077da"
    for node in ("devusr-dev", "host/with spaces", "~tilde"):
        key = keys.daemon_liveliness(node, generation)
        node_segment, generation_segment = key.rsplit("/", 2)[-2:]
        assert keys.decode_identity(node_segment) == node
        assert keys.decode_identity(generation_segment) == generation
        wildcard = keys.daemon_liveliness_for_node(node)
        assert wildcard.endswith("/*")
        assert key.startswith(wildcard[:-1])


# --------------------------------------------------------------------------
# ps payload (DaemonApplication._duplicate_instance_payload)
# --------------------------------------------------------------------------


def test_ps_payload_reports_inactive_before_any_detection(tmp_path: Any) -> None:
    from hyprial.daemon.impl.application import DaemonApplication

    app = DaemonApplication(
        state_dir=tmp_path, socket_path=tmp_path / "daemon.sock", node_id="node-x",
    **_deep_module_daemon_dependencies())
    try:
        payload = app._duplicate_instance_payload()
        assert payload["active"] is False
        assert payload["meshPeerGenerations"] == []
        assert payload["startupRecord"] is None
        # 批复①条件1: the coverage limit ships in the payload itself.
        assert "generation liveliness token" in payload["meshDetectionCoverage"]
    finally:
        app.agents.close()


def test_ps_payload_surfaces_startup_copied_home_detection(tmp_path: Any) -> None:
    from hyprial.daemon.impl.application import DaemonApplication

    app = DaemonApplication(
        state_dir=tmp_path, socket_path=tmp_path / "daemon.sock", node_id="node-x",
    **_deep_module_daemon_dependencies())
    try:
        app._on_startup_duplicate_detected(
            {
                "recordPid": 222,
                "claimantPid": 111,
                "recordGeneration": "gen-old",
                "claimantGeneration": "gen-new",
                "processStatus": "alive",
                "home": str(tmp_path),
                "detail": "copied home",
            }
        )
        payload = app._duplicate_instance_payload()
        assert payload["active"] is True
        assert payload["startupRecord"]["recordPid"] == 222
        assert payload["startupRecord"]["recordGeneration"] == "gen-old"
        assert payload["meshPeerGenerations"] == []
    finally:
        app.agents.close()


def test_ps_payload_combines_mesh_watch_and_startup_sources(tmp_path: Any) -> None:
    from hyprial.daemon.impl.application import DaemonApplication

    app = DaemonApplication(
        state_dir=tmp_path, socket_path=tmp_path / "daemon.sock", node_id="node-x",
    **_deep_module_daemon_dependencies())
    transport = ZenohTransport(ZenohConfig(listen=(f"tcp/127.0.0.1:{_port()}",)))
    events = _Events()
    watch = DuplicateInstanceWatch(transport, "node-x", "gen-self", logger=events)
    app._duplicate_watch = watch
    try:
        watch._on_sample(_sample("node-x", "gen-peer"))
        payload = app._duplicate_instance_payload()
        assert payload["active"] is True
        assert payload["meshPeerGenerations"] == ["gen-peer"]
        assert payload["startupRecord"] is None
    finally:
        app._duplicate_watch = None
        watch.close()
        transport.close()
        app.agents.close()
