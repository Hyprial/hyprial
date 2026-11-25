"""Z-6a/Z-6b/Z-6c: the principal watcher never promotes, hosts or takes a lock, and never re-listens.

Decision (2822, B4): the foreground GUI serve wrapper is deleted; the watcher
is driven here through ``watch_principal`` with the same finalizer an embedding
host runs (shut the server down, then close the live host; that order is pinned for
the real embedding host in ``tests/shell/gui/host/local``).

The listener, the host and the identity snapshot are fakes; what is pinned is
the loop's decisions: one server for the life of the host, a rebind (the owner from
durable state, the same listen fields) after the old host is closed on an observed daemon change of a bootstrap or a serving host,
only attaching reopens, a daemon that is gone never ends the host (it waits for
one to return), a transient rebind refusal is waited out and logged once per
distinct code, and only an owner that disagrees with the identity snapshot is
refused after the drain budget.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from hyprial.daemon import DaemonOwnershipBusy
from hyprial.kernel import canonical_user_uri
from hyprial.shell.impl.request_http.bootstrap import gui
from hyprial.shell.impl.request_http.config import (
    DEFAULT_BUDGETS,
    RequestHttpConfig,
    _budgets,
)
from hyprial.shell.impl.request_http.server import StartupRefusal

OWNER = "loop-owner@example.test"


def _config(tmp_path: Path, *, drain: float = 5.0) -> RequestHttpConfig:
    budgets = replace(_budgets(DEFAULT_BUDGETS), shutdown_drain=drain)
    return RequestHttpConfig(
        socket_path=tmp_path / "state" / "daemon.sock",
        owner=None,
        actor=None,
        host="127.0.0.1",
        port=0,
        allowed_origins=(),
        allowed_hosts=("127.0.0.1", "localhost"),
        budgets=budgets,
        runtime_dir=tmp_path / "http",
        source=tmp_path,
        principal_kind="local-gui",
        home=tmp_path,
        state_dir=tmp_path / "state",
    )


class FakeHost:
    def __init__(self, tmp_path: Path, *, phase: str, forwarding: bool, changes_after: int | None = None):
        self.home = tmp_path
        self.phase = phase
        self.forwarding = forwarding
        self.owner = OWNER if phase == "serving" else None
        self.closed = False
        self._changes_after = changes_after
        self._polls = 0
        self.socket_path = tmp_path / "state" / "daemon.sock"

    def daemon_changed(self):
        self._polls += 1
        return self._changes_after is not None and self._polls > self._changes_after

    def close(self):
        self.closed = True


@pytest.fixture
def harness(tmp_path, monkeypatch):
    state = SimpleNamespace(servers=[], owner=None, events=[], rebinds=[], on_rebind=None, stop=threading.Event())

    class Server:
        def __init__(self, config, *, request_once):
            self.config = config
            self.host = request_once
            self.down = False
            state.servers.append(self)

        def start(self):
            state.events.append("start")

        def rebind(self, config, *, request_once):
            state.events.append("rebind")
            state.rebinds.append((config, request_once))
            if state.on_rebind is not None:
                state.on_rebind(len(state.rebinds))

        def shutdown(self):
            self.down = True
            state.events.append("shutdown")

    state.Server = Server
    monkeypatch.setattr(gui, "read_identity_snapshot", lambda home: SimpleNamespace(owner=state.owner))
    return state


def _serve(harness, config, first):
    server = harness.Server(config, request_once=first)
    live = gui.LiveHost(first)
    try:
        server.start()
        gui.watch_principal(server, config, live, harness.stop)
    finally:
        server.shutdown()
        live.host.close()
    return 0


def _stop_after_rebinds(harness, count=1):
    def on_rebind(seen):
        if seen >= count:
            harness.stop.set()

    harness.on_rebind = on_rebind


def test_a_forwarding_bootstrap_rebinds_the_one_server_on_a_daemon_change(tmp_path, monkeypatch, harness):
    first = FakeHost(tmp_path, phase="bootstrap", forwarding=True, changes_after=1)
    second = FakeHost(tmp_path, phase="serving", forwarding=False)
    harness.owner = OWNER
    _stop_after_rebinds(harness)
    original_close = first.close

    def close():
        harness.events.append("close")
        original_close()

    first.close = close
    calls = []

    def new_host(config, **extra):
        calls.append(extra)
        return second

    monkeypatch.setattr(gui, "new_host", new_host)
    config = _config(tmp_path)
    assert _serve(harness, config, first) == 0
    assert calls == [{"attach_only": True}]
    assert len(harness.servers) == 1 and harness.servers[0].host is first
    assert harness.events[:3] == ["start", "close", "rebind"], harness.events
    assert harness.events[-1] == "shutdown" and harness.events.count("shutdown") == 1
    (served, handle), = harness.rebinds
    assert handle is second and first.closed is True
    assert served.owner == OWNER and served.actor == canonical_user_uri(OWNER)
    for field in ("host", "port", "runtime_dir", "socket_path", "principal_kind", "home", "state_dir"):
        assert getattr(served, field) == getattr(config, field), field


def test_a_daemon_that_stays_in_bootstrap_rebinds_without_an_owner(tmp_path, monkeypatch, harness):
    first = FakeHost(tmp_path, phase="bootstrap", forwarding=True, changes_after=1)
    second = FakeHost(tmp_path, phase="bootstrap", forwarding=True)
    harness.owner = OWNER
    _stop_after_rebinds(harness)
    monkeypatch.setattr(gui, "new_host", lambda config, **extra: second)
    assert _serve(harness, _config(tmp_path), first) == 0
    (served, _handle), = harness.rebinds
    assert served.owner is None and served.actor is None
    assert len(harness.servers) == 1


def test_a_serving_host_that_reports_a_daemon_change_is_rebound_too(tmp_path, monkeypatch, harness):
    first = FakeHost(tmp_path, phase="serving", forwarding=False, changes_after=1)
    second = FakeHost(tmp_path, phase="bootstrap", forwarding=True)
    harness.owner = OWNER
    _stop_after_rebinds(harness)
    monkeypatch.setattr(gui, "new_host", lambda config, **extra: second)
    assert _serve(harness, _config(tmp_path), first) == 0
    (served, handle), = harness.rebinds
    assert handle is second and served.owner is None and first.closed is True
    assert len(harness.servers) == 1


def test_every_change_is_rebound_on_the_same_server(tmp_path, monkeypatch, harness):
    first = FakeHost(tmp_path, phase="bootstrap", forwarding=True, changes_after=1)
    second = FakeHost(tmp_path, phase="serving", forwarding=False, changes_after=1)
    third = FakeHost(tmp_path, phase="bootstrap", forwarding=True)
    hosts = iter([second, third])
    harness.owner = OWNER

    def switch_owner(seen):
        harness.owner = None
        if seen >= 2:
            harness.stop.set()

    harness.on_rebind = switch_owner
    monkeypatch.setattr(gui, "new_host", lambda config, **extra: next(hosts))
    assert _serve(harness, _config(tmp_path), first) == 0
    assert [config.owner for config, _ in harness.rebinds] == [OWNER, None]
    assert len(harness.servers) == 1


def test_a_daemon_that_is_unavailable_at_the_rebind_is_waited_for_not_fatal(tmp_path, monkeypatch, harness):
    first = FakeHost(tmp_path, phase="bootstrap", forwarding=True, changes_after=1)
    second = FakeHost(tmp_path, phase="bootstrap", forwarding=True)
    third = FakeHost(tmp_path, phase="serving", forwarding=False)
    hosts = iter([second, third])
    harness.owner = OWNER

    def refuse_once(seen):
        if seen == 1:
            raise StartupRefusal("BACKEND_UNAVAILABLE", "the daemon is not reachable", exit_code=3)
        harness.stop.set()

    harness.on_rebind = refuse_once
    monkeypatch.setattr(gui, "new_host", lambda config, **extra: next(hosts))
    assert _serve(harness, _config(tmp_path), first) == 0
    assert [handle for _, handle in harness.rebinds] == [second, third]
    assert second.closed is True and len(harness.servers) == 1


def test_a_persistent_transient_refusal_is_logged_once_per_distinct_code(tmp_path, monkeypatch, harness, capsys):
    first = FakeHost(tmp_path, phase="bootstrap", forwarding=True, changes_after=1)
    harness.owner = OWNER
    refusals = [
        StartupRefusal("BACKEND_UNAVAILABLE", "the daemon is not reachable", exit_code=3),
        StartupRefusal("BACKEND_UNAVAILABLE", "the daemon is not reachable", exit_code=3),
        StartupRefusal("BACKEND_UNAVAILABLE", "the daemon is not reachable", exit_code=3),
        StartupRefusal("CATALOG_MISMATCH", "epoch", reason="EPOCH_INCONSISTENT"),
        StartupRefusal("CATALOG_MISMATCH", "epoch", reason="EPOCH_INCONSISTENT"),
    ]

    def refuse(seen):
        if seen <= len(refusals):
            raise refusals[seen - 1]
        harness.stop.set()

    harness.on_rebind = refuse
    monkeypatch.setattr(gui, "new_host", lambda config, **extra: FakeHost(tmp_path, phase="bootstrap", forwarding=True))
    assert _serve(harness, _config(tmp_path), first) == 0
    assert len(harness.rebinds) == 6
    events = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert [(event["code"], event["reason"]) for event in events] == [
        ("BACKEND_UNAVAILABLE", None),
        ("CATALOG_MISMATCH", "EPOCH_INCONSISTENT"),
    ]
    assert all(event["event"] == "rebind_waiting" for event in events)


def test_a_contract_refusal_at_the_rebind_ends_the_host(tmp_path, monkeypatch, harness):
    first = FakeHost(tmp_path, phase="bootstrap", forwarding=True, changes_after=1)
    second = FakeHost(tmp_path, phase="serving", forwarding=False)
    harness.owner = OWNER

    def refuse(_seen):
        raise StartupRefusal("CATALOG_MISMATCH", "contract", reason="CONTRACT_CHANGED")

    harness.on_rebind = refuse
    monkeypatch.setattr(gui, "new_host", lambda config, **extra: second)
    with pytest.raises(StartupRefusal) as refusal:
        _serve(harness, _config(tmp_path), first)
    assert refusal.value.code == "CATALOG_MISMATCH"
    assert harness.servers[0].down is True and second.closed is True


def test_reopen_waits_without_a_deadline_while_no_daemon_answers(tmp_path, monkeypatch, harness):
    import threading

    previous = FakeHost(tmp_path, phase="bootstrap", forwarding=True)
    stop = threading.Event()
    attempts = []

    def new_host(config, **extra):
        attempts.append(extra)
        if len(attempts) >= 6:
            stop.set()
        raise DaemonOwnershipBusy("no daemon answers yet")

    monkeypatch.setattr(gui, "new_host", new_host)
    assert gui.reopen(_config(tmp_path, drain=0.05), stop, previous) is None
    assert len(attempts) == 6 and all(extra == {"attach_only": True} for extra in attempts)


def test_a_host_whose_daemon_is_gone_stays_until_stopped_and_never_hosts(tmp_path, monkeypatch, harness):
    first = FakeHost(tmp_path, phase="serving", forwarding=False, changes_after=1)
    harness.owner = OWNER
    attempts = []

    def new_host(config, **extra):
        attempts.append(extra)
        if len(attempts) >= 6:
            harness.stop.set()
        raise DaemonOwnershipBusy("no daemon answers yet")

    monkeypatch.setattr(gui, "new_host", new_host)
    assert _serve(harness, _config(tmp_path, drain=0.05), first) == 0
    assert len(attempts) == 6 and harness.rebinds == [] and len(harness.servers) == 1
    assert harness.servers[0].down is True


def test_the_snapshot_losing_its_owner_does_not_end_a_serving_host(tmp_path, monkeypatch, harness):
    first = FakeHost(tmp_path, phase="serving", forwarding=False)
    harness.owner = None
    polls = []

    def daemon_changed():
        polls.append(1)
        if len(polls) >= 8:
            harness.stop.set()
        return False

    first.daemon_changed = daemon_changed
    monkeypatch.setattr(gui, "new_host", lambda config, **extra: pytest.fail("reopened on a snapshot alone"))
    assert _serve(harness, _config(tmp_path), first) == 0
    assert first.closed is True and harness.rebinds == []


def test_an_owner_that_disagrees_with_the_snapshot_is_bounded_by_the_drain_budget(tmp_path, monkeypatch, harness):
    import threading

    previous = FakeHost(tmp_path, phase="serving", forwarding=False)
    harness.owner = "snapshot-owner@example.test"
    other = FakeHost(tmp_path, phase="serving", forwarding=False)
    other.owner = OWNER
    monkeypatch.setattr(gui, "new_host", lambda config, **extra: other)
    with pytest.raises(StartupRefusal) as refusal:
        gui.reopen(_config(tmp_path, drain=0.3), threading.Event(), previous)
    assert refusal.value.code == "PRINCIPAL_CONFIG_MISMATCH"
    assert other.closed is True


def test_a_mismatched_serving_owner_is_a_principal_refusal(tmp_path, monkeypatch, harness):
    first = FakeHost(tmp_path, phase="bootstrap", forwarding=True, changes_after=1)
    second = FakeHost(tmp_path, phase="serving", forwarding=False)
    second.owner = OWNER
    second.daemon_owner = "someone-else@example.test"
    harness.owner = OWNER
    monkeypatch.setattr(gui, "new_host", lambda config, **extra: second)
    with pytest.raises(StartupRefusal) as refusal:
        _serve(harness, _config(tmp_path, drain=0.3), first)
    assert refusal.value.code == "PRINCIPAL_CONFIG_MISMATCH"

