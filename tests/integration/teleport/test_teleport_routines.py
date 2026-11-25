"""Slice 6 scenarios 5, 6 and 7: routines executed by a teleported agent.

Decision record:

- Same two real isolated daemons and real Zenoh as slice 5 (``conftest.py``);
  this module gets its own pair.  Teleports go through the raw
  ``teleport.run`` request only.
- Routines are registered, read and paused through the published daemon
  surface (``routine.add`` / ``routine.list`` / ``routine.status`` /
  ``routine.pause``) as the daemon owner's user principal, ``user:<owner>``,
  the identity the CLI sends for an operator outside any worker session.
  The owner is the isolated daemon's own ``HYPRIAL_OWNER``.
- Every agent created with ``agent.create`` also gets its default
  ``agent-home-setup-*`` routine executed by that agent; it migrates with the
  agent and is asserted like any other enabled routine, by actor.
- The stored YAML text is read from the daemon's own private
  ``routines.sqlite3`` read-only, like ``agent_rows`` reads ``agents.sqlite3``,
  because no published method returns it.  The target re-dumps a rewritten
  document, so migrated YAML is compared as parsed documents; YAML that must
  stay untouched is compared byte for byte.
- Scenario 5: one enabled and one paused routine executed by the agent, the
  enabled one escalating to a second, different agent, plus a third routine
  executed by that second agent that only escalates to the teleported one.
  Intervals are ``15m`` so nothing is due during the test; "no dispatch on the
  target while paused" is read from what the surface shows: no in-flight
  rows, no outcomes, no schedule events and no last dispatch on the target.
- Scenario 6: the target already holds a routine with the same name
  (registered on the target for a target-local agent), so ``prepare`` answers
  ``TELEPORT_ROUTINE_CONFLICT`` before the source pauses anything.
- Scenario 7: there is no knob for ``ROUTINE_DRAIN_TIMEOUT_SECONDS`` (120 s,
  ``daemon/impl/teleport/source/service.py``; the wiring in
  ``teleport/wiring/source.py`` passes no override) and adding one would be
  product code, so the test waits it out once.  The in-flight row is real: a
  ``scheduled`` routine with the minimum ``1m`` interval is due one interval
  after ``routine.add``, the daemon's own maintenance timer dispatches it as a
  real PAC graph to the agent, and the graph stays running because the
  protocol double answers turns but never settles PAC tasks.  "Never stopped"
  is the unchanged worker pid; "home untouched" is the agent home's bytes
  after the in-flight row exists against the bytes after the refusal.
- The reply is checked after waiting out the drain window plus
  ``SETTLE_SECONDS``, and the failure message carries the routine states
  right after the reply and after that window, so a reply that arrives
  before the daemon has finished (for example a generic IPC deadline shorter
  than the drain timeout) still shows what the source ended up as.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

import yaml

from hyprial.daemon.impl.teleport.source.service import ROUTINE_DRAIN_TIMEOUT_SECONDS

from .conftest import (
    CHUNK,
    TARGET_NODE,
    IsolatedDaemon,
    Pair,
    Worker,
    agent_home,
    agent_rows,
    connector,
    desired_harness,
    ok,
    running,
    seed_worker,
    staging_entries,
    teleport,
    tree_digest,
    wait_running,
)

PAYLOAD = 2 * CHUNK + 4096
DISPATCH_WAIT_SECONDS = 200.0
SETTLE_SECONDS = 20.0


def operator(daemon: IsolatedDaemon) -> str:
    return f"user:{daemon.env['HYPRIAL_OWNER']}"


def routine_yaml(name: str, actor: str, *, escalate_to: str,
                 interval: str = "15m") -> str:
    return yaml.safe_dump(
        {
            "version": 2,
            "mode": "scheduled",
            "name": name,
            "schedule": {"interval": interval},
            "actor": actor,
            "role": "dispatch",
            "task": f"routine work for {name}",
            "on_task_timeout": {"action": "escalate", "escalate_to": escalate_to},
        },
        allow_unicode=True,
        sort_keys=False,
    )


def add_routine(daemon: IsolatedDaemon, principal: str, text: str) -> str:
    added = ok(daemon, "routine.add", {"yaml": text, "actor": principal})
    assert added["enabled"] is True, added
    return str(added["name"])


def pause_routine(daemon: IsolatedDaemon, principal: str, name: str) -> None:
    paused = ok(daemon, "routine.pause", {"name": name, "actor": principal})
    assert paused["enabled"] is False, paused


def routines(daemon: IsolatedDaemon, principal: str) -> dict[str, dict[str, Any]]:
    listed = ok(daemon, "routine.list", {"actor": principal})["routines"]
    return {item["name"]: item for item in listed}


def status(daemon: IsolatedDaemon, principal: str, name: str) -> dict[str, Any]:
    return ok(daemon, "routine.status", {"name": name, "actor": principal})


def stored_yaml(daemon: IsolatedDaemon) -> dict[str, str]:
    """Routine name -> stored YAML text, from the daemon's private database."""

    database = daemon.root / "state" / "routines.sqlite3"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
        return dict(conn.execute("SELECT name, yaml_text FROM routines").fetchall())


def executed_by(rows: dict[str, dict[str, Any]], uri: str) -> dict[str, bool]:
    return {name: bool(row["enabled"]) for name, row in rows.items()
            if row["actor"] == uri}


def _pid(daemon: IsolatedDaemon, name: str) -> int | None:
    item = connector(daemon, name)
    return None if item is None else item.get("pid")


def _error(document: dict[str, Any]) -> dict[str, Any]:
    error = document.get("error")
    assert isinstance(error, dict), document
    return error


def _assert_source_kept(pair: Pair, worker: Worker, *, pid: int | None,
                        uri: str, home: dict[str, str]) -> None:
    assert running(pair.source, worker.name), connector(pair.source, worker.name)
    assert pid is not None and _pid(pair.source, worker.name) == pid
    desired = desired_harness(pair.source, worker.name)
    assert desired is not None and desired["session_ref"] == worker.session_ref, desired
    assert tree_digest(worker.home) == home
    assert agent_rows(pair.source).get(worker.name) == uri
    assert worker.name not in agent_rows(pair.target)
    assert staging_entries(pair.target) == []
    assert pair.stub_calls() == []


def test_enabled_and_disabled_routines_move_with_the_agent(pair: Pair) -> None:
    worker = seed_worker(pair.source, "tp-rt-move", payload_bytes=PAYLOAD)
    ok(pair.source, "agent.create", {"name": "tp-rt-peer"})
    source_rows = agent_rows(pair.source)
    source_uri = source_rows[worker.name]
    peer_uri = source_rows["tp-rt-peer"]
    principal = operator(pair.source)
    on = add_routine(pair.source, principal, routine_yaml(
        "tp-rt-on", source_uri, escalate_to=peer_uri))
    off = add_routine(pair.source, principal, routine_yaml(
        "tp-rt-off", source_uri, escalate_to=principal))
    foreign = add_routine(pair.source, principal, routine_yaml(
        "tp-rt-foreign", peer_uri, escalate_to=source_uri))
    pause_routine(pair.source, principal, off)
    before = routines(pair.source, principal)
    moving = executed_by(before, source_uri)
    assert moving[on] is True and moving[off] is False, moving
    yaml_before = stored_yaml(pair.source)
    foreign_before = before[foreign]

    document = teleport(pair.source, worker.name, TARGET_NODE)

    assert "error" not in document, document
    result = document["result"]
    assert result["ok"] is True, result
    assert {item["name"]: item["enabled"] for item in result["routines"]} == moving
    assert result["commit"] == {"ok": True, "pausedRoutines": []}, result
    target_uri = agent_rows(pair.target)[worker.name]
    assert result["to"]["uri"] == target_uri, result
    assert wait_running(pair.target, worker.name), connector(pair.target, worker.name)

    target_principal = operator(pair.target)
    landed = routines(pair.target, target_principal)
    assert executed_by(landed, target_uri) == moving, landed
    landed_yaml = stored_yaml(pair.target)
    for name in moving:
        expected = yaml.safe_load(yaml_before[name])
        expected["actor"] = target_uri
        assert yaml.safe_load(landed_yaml[name]) == expected, (name, landed_yaml[name])
        row = status(pair.target, target_principal, name)
        assert row["inFlight"] == [], row
        assert row["outcomes"] == [], row
        assert row["scheduleEvents"] == [], row
        assert landed[name].get("lastDispatchAtMs") is None, landed[name]
    on_landed = yaml.safe_load(landed_yaml[on])
    assert on_landed["on_task_timeout"]["escalate_to"] == peer_uri, on_landed
    assert landed[on]["enabled"] is True and landed[off]["enabled"] is False

    after = routines(pair.source, principal)
    assert not set(moving) & set(after), sorted(after)
    assert executed_by(after, source_uri) == {}
    assert after[foreign] == foreign_before, (after[foreign], foreign_before)
    assert stored_yaml(pair.source)[foreign] == yaml_before[foreign]
    assert foreign not in landed
    assert worker.name not in agent_rows(pair.source)
    assert not worker.home.exists()
    assert staging_entries(pair.target) == []
    assert pair.stub_calls() == []


def test_routine_name_taken_on_target_is_conflict_and_source_is_untouched(
    pair: Pair,
) -> None:
    worker = seed_worker(pair.source, "tp-rt-clash", payload_bytes=PAYLOAD)
    source_uri = agent_rows(pair.source)[worker.name]
    principal = operator(pair.source)
    on = add_routine(pair.source, principal, routine_yaml(
        "tp-rt-clash-on", source_uri, escalate_to=principal))
    off = add_routine(pair.source, principal, routine_yaml(
        "tp-rt-clash-off", source_uri, escalate_to=principal))
    pause_routine(pair.source, principal, off)

    ok(pair.target, "agent.create", {"name": "tp-rt-occupant"})
    occupant_uri = agent_rows(pair.target)["tp-rt-occupant"]
    target_principal = operator(pair.target)
    add_routine(pair.target, target_principal, routine_yaml(
        on, occupant_uri, escalate_to=target_principal))
    target_before = routines(pair.target, target_principal)
    target_yaml_before = stored_yaml(pair.target)

    before = routines(pair.source, principal)
    yaml_before = stored_yaml(pair.source)
    home_before = tree_digest(worker.home)
    pid = _pid(pair.source, worker.name)

    document = teleport(pair.source, worker.name, TARGET_NODE)

    error = _error(document)
    assert error["code"] == "TELEPORT_ROUTINE_CONFLICT", error
    assert on in error["message"], error
    _assert_source_kept(pair, worker, pid=pid, uri=source_uri, home=home_before)
    after = routines(pair.source, principal)
    assert executed_by(after, source_uri) == executed_by(before, source_uri)
    assert after[on]["enabled"] is True and after[off]["enabled"] is False
    for name in executed_by(before, source_uri):
        assert after[name]["registrationId"] == before[name]["registrationId"]
        assert after[name]["inFlight"] == [], after[name]
    assert stored_yaml(pair.source) == yaml_before
    assert routines(pair.target, target_principal) == target_before
    assert stored_yaml(pair.target) == target_yaml_before
    assert off not in target_before


def _wait_in_flight(daemon: IsolatedDaemon, principal: str, name: str) -> dict[str, Any]:
    """Poll ``routine.status`` until the routine shows in-flight work.

    A single slow answer is not the result under test: on a loaded runner the
    daemon has taken longer than one request timeout to answer while the first
    dispatch was starting, so a timed-out poll is retried inside the same
    overall wait.  The last poll is not guarded, so a daemon that never answers
    still fails here.
    """
    deadline = time.monotonic() + DISPATCH_WAIT_SECONDS
    row: dict[str, Any] = {"inFlight": False}
    while not row["inFlight"] and time.monotonic() < deadline:
        try:
            row = status(daemon, principal, name)
        except TimeoutError:
            continue
        if not row["inFlight"]:
            time.sleep(1.0)
    return row if row["inFlight"] else status(daemon, principal, name)


def _dispatch_evidence(daemon: IsolatedDaemon, row: dict[str, Any]) -> str:
    """The full status row and the daemon's routine log lines, for a failed wait."""
    log = daemon.root / "state" / "logs" / "daemon.jsonl"
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as error:
        lines = [f"daemon log unreadable: {error}"]
    routine_lines = [line[:400] for line in lines if "routine" in line][-40:]
    return "\n".join([json.dumps(row, sort_keys=True, default=str), *routine_lines])


def test_in_flight_work_that_does_not_drain_times_out_and_restores(
    pair: Pair,
) -> None:
    worker = seed_worker(pair.source, "tp-rt-busy", payload_bytes=PAYLOAD)
    source_uri = agent_rows(pair.source)[worker.name]
    principal = operator(pair.source)
    busy = add_routine(pair.source, principal, routine_yaml(
        "tp-rt-busy-on", source_uri, escalate_to=principal, interval="1m"))
    off = add_routine(pair.source, principal, routine_yaml(
        "tp-rt-busy-off", source_uri, escalate_to=principal))
    pause_routine(pair.source, principal, off)

    dispatched = _wait_in_flight(pair.source, principal, busy)
    assert dispatched["inFlight"], _dispatch_evidence(pair.source, dispatched)
    before = routines(pair.source, principal)
    moving = executed_by(before, source_uri)
    assert moving[busy] is True and moving[off] is False, moving
    yaml_before = stored_yaml(pair.source)
    home_before = tree_digest(agent_home(pair.source, worker.name))
    pid = _pid(pair.source, worker.name)

    started = time.monotonic()
    document = teleport(pair.source, worker.name, TARGET_NODE)
    elapsed = time.monotonic() - started
    during = executed_by(routines(pair.source, principal), source_uri)
    settle = started + ROUTINE_DRAIN_TIMEOUT_SECONDS + SETTLE_SECONDS - time.monotonic()
    if settle > 0:
        time.sleep(settle)
    after = routines(pair.source, principal)
    witness = {
        "reply": document,
        "elapsed": round(elapsed, 1),
        "routinesRightAfterReply": during,
        "routinesAfterDrainWindow": executed_by(after, source_uri),
        "running": running(pair.source, worker.name),
        "pidUnchanged": _pid(pair.source, worker.name) == pid,
    }

    error = _error(document)
    assert error["code"] == "TELEPORT_IN_FLIGHT_TIMEOUT", json.dumps(witness)
    assert busy in error["message"], error
    assert elapsed >= ROUTINE_DRAIN_TIMEOUT_SECONDS - 1.0, elapsed
    _assert_source_kept(pair, worker, pid=pid, uri=source_uri, home=home_before)
    assert executed_by(after, source_uri) == moving, after
    assert stored_yaml(pair.source) == yaml_before
    for name in moving:
        assert after[name]["registrationId"] == before[name]["registrationId"]
    assert not set(moving) & set(routines(pair.target, operator(pair.target)))
