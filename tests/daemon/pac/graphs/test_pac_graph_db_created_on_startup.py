"""The daemon's own startup must provision pac-graph.sqlite3.

Every other PAC test builds a ``PacGraphStore`` first, which creates the file as
a side effect of connecting -- so the whole suite asserts "PAC works once the
database exists" and nothing asks who creates it in production.  These tests
must therefore never construct a store: they boot the daemon and then read the
file with stdlib sqlite3, so the only thing that can have created it is the
startup path itself.
"""

from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path

import pytest

from hyprial.identity.impl.agents.registry._core import AgentRegistry
from hyprial.kernel import DaemonStartupPhase
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.transport.zenoh.config import ZenohConfig
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

# The tables `store.SCHEMA` creates; asserted by name so a database that exists
# but was never migrated still fails.
_EXPECTED_TABLES = {"graphs", "nodes", "edges", "flag_events", "notifications"}


def _assert_loopback_transport(config: ZenohConfig) -> None:
    """Fail before native open if startup could reach an ambient peer."""
    assert not config.gossip_scouting
    assert not config.multicast_scouting
    for endpoint in (*config.listen, *config.connect):
        host = endpoint.partition("/")[2].rsplit(":", 1)[0].strip("[]")
        assert host in {"127.0.0.1", "::1", "localhost"}, endpoint


def _boot_startup_only(
    application: DaemonApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the daemon's startup, including `_start_runtime`.

    ⛔ `_start_runtime` is deliberately NOT stubbed -- it is where
    `PacActorService` is assembled (application.py:1718), so stubbing it would
    remove the very code under test.  Everything that needs a socket or a
    serving loop is stubbed instead.
    """

    # The direct constructor deliberately does not consume the environment's
    # isolation switch. Set the actual application policy before real startup,
    # and validate every native config before Zenoh can open it.
    application.network_isolated = True
    application.zenoh_connect = ()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        application.zenoh_listen = (f"tcp/127.0.0.1:{reservation.getsockname()[1]}",)
    native_build = ZenohConfig.build

    def guarded_build(config: ZenohConfig):
        _assert_loopback_transport(config)
        return native_build(config)

    monkeypatch.setattr(ZenohConfig, "build", guarded_build)
    monkeypatch.setattr(application, "_start_server", lambda: None)
    monkeypatch.setattr(application, "_restore_adapters", lambda: None)
    monkeypatch.setattr(application, "_write_pid_file", lambda: None)
    monkeypatch.setattr(application, "_serve", lambda: None)
    application.run()
    assert application._restore_done.wait(5.0), "restore thread never finished"


def _tables(database: Path) -> set[str]:
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def test_startup_creates_pac_graph_database_in_a_fresh_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    state_dir = home / "state"
    state_dir.mkdir(parents=True, mode=0o700)
    database = state_dir / "pac-graph.sqlite3"
    assert not database.exists(), "precondition: the fresh home has no database"

    application = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="pac-create-fresh",
        hyprial_home=home,
        owner="tester",
    **_deep_module_daemon_dependencies())
    _boot_startup_only(application, monkeypatch)

    assert database.is_file(), (
        "daemon startup left pac-graph.sqlite3 absent; PacActorService.submit_tick "
        "returns early when it is missing, so PAC never runs"
    )
    assert _EXPECTED_TABLES <= _tables(database)


def test_normal_startup_records_actor_runtime_substeps_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    state_dir = home / "state"
    state_dir.mkdir(parents=True, mode=0o700)
    application = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="startup-substeps",
        hyprial_home=home,
        owner="tester",
    **_deep_module_daemon_dependencies())

    _boot_startup_only(application, monkeypatch)

    records = [
        json.loads(line)
        for line in (state_dir / "logs" / "daemon.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    phases = [
        record["phase"]
        for record in records
        if record.get("event") == "daemon.start.begin"
    ]
    # This fixture explicitly isolates networking: no early forwarding work
    # and no obsolete late forwarding-reconfigure checkpoint are emitted.
    assert DaemonStartupPhase.ACTOR_RUNTIME_FORWARDING_RECONFIGURE not in phases
    assert phases[:8] == [
        DaemonStartupPhase.ACTOR_RUNTIME,
        DaemonStartupPhase.ACTOR_RUNTIME_AGENTS_STORE,
        DaemonStartupPhase.ACTOR_RUNTIME_TRANSPORT,
        DaemonStartupPhase.ACTOR_RUNTIME_ORGFS_TRANSPORT,
        DaemonStartupPhase.ACTOR_RUNTIME_IDENTITY_BINDINGS,
        DaemonStartupPhase.ACTOR_RUNTIME_CASDOOR_SYNC,
        DaemonStartupPhase.ACTOR_RUNTIME_INBOX_STORE,
        DaemonStartupPhase.ACTOR_RUNTIME_LIFECYCLE_STORE,
    ]


def test_startup_creates_pac_tables_in_an_existing_home_without_touching_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The upgrade half: a home that predates PAC v2 keeps its data."""

    home = tmp_path / "home"
    state_dir = home / "state"
    state_dir.mkdir(parents=True, mode=0o700)

    # Stand in for a pre-PAC home.  Built through the real registry, not by
    # hand: the daemon reads this file on startup, so a hand-rolled schema
    # crashes it and the test would go red for its own reason instead of the
    # one under test.
    legacy = state_dir / "agents.sqlite3"
    registry = AgentRegistry(legacy, owner="tester", machine="pac-create-upgrade")
    try:
        registry.create("legacy-agent")
    finally:
        registry.close()

    application = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="pac-create-upgrade",
        hyprial_home=home,
        owner="tester",
    **_deep_module_daemon_dependencies())
    _boot_startup_only(application, monkeypatch)

    database = state_dir / "pac-graph.sqlite3"
    assert database.is_file(), "upgrade path left pac-graph.sqlite3 absent"
    assert _EXPECTED_TABLES <= _tables(database)

    surviving = AgentRegistry(legacy, owner="tester", machine="pac-create-upgrade")
    try:
        assert surviving.get("legacy-agent") is not None, (
            "startup damaged the existing home"
        )
    finally:
        surviving.close()


def test_startup_reports_a_provisioning_failure_and_still_serves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The failure branch must reach the log, and must not stop the daemon.

    ⛔ Both halves matter and they pull against each other. Raising would let
    one subsystem take down a node that has other work; swallowing would
    restore exactly the silent inertness this change removes -- "no database"
    and "nothing to do" would take the same branch again, one layer up.

    A directory where the file belongs makes `sqlite3.connect` fail without
    any monkeypatching of the code under test.
    """

    home = tmp_path / "home"
    state_dir = home / "state"
    state_dir.mkdir(parents=True, mode=0o700)
    # Not a file: sqlite cannot open it, so provisioning raises for real.
    (state_dir / "pac-graph.sqlite3").mkdir()

    application = DaemonApplication(
        state_dir=state_dir,
        socket_path=state_dir / "daemon.sock",
        node_id="pac-create-failure",
        hyprial_home=home,
        owner="tester",
    **_deep_module_daemon_dependencies())
    _boot_startup_only(application, monkeypatch)

    daemon_log = state_dir / "logs" / "daemon.jsonl"
    events = [
        json.loads(line)
        for line in daemon_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    provisioning = [
        event for event in events if event.get("event") == "pac.database.provision_failed"
    ]
    assert len(provisioning) == 1, (
        "the provisioning failure never reached the log; a swallowed failure is "
        "indistinguishable from the silent inertness this change removes"
    )
    assert provisioning[0]["level"] == "error"
    assert provisioning[0]["detail"]

    # And the daemon still came up: startup completed rather than raising.
    assert application._restore_done.is_set()
