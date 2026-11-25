"""§4: one teleport per agent at a time (in-memory lock)."""

from __future__ import annotations

import threading
from pathlib import Path

from hyprial.daemon.impl.teleport import TeleportError

from .fakes import AGENT, Gate, expect_failure, make_agent, rig


def _start(r, outcome: dict, **kwargs) -> threading.Thread:
    def body() -> None:
        try:
            outcome["result"] = r.run(**kwargs)
        except BaseException as error:
            outcome["error"] = error

    thread = threading.Thread(target=body, daemon=True)
    thread.start()
    return thread


def test_second_teleport_of_the_same_agent_is_operation_conflict(tmp_path: Path) -> None:
    r = rig(tmp_path)
    gate = Gate(AGENT)
    r.link.gate = gate
    first: dict = {}
    thread = _start(r, first)
    assert gate.entered.wait(10)
    before = len(r.events)
    error = expect_failure(r.run)
    assert error.code == "TELEPORT_OPERATION_CONFLICT"
    assert [e for e in r.events[before:] if e[0] in {"prepare", "quiesce", "abort"}] == []
    gate.release.set()
    thread.join(10)
    assert first.get("result", {}).get("ok") is True


def test_a_dry_run_also_holds_and_respects_the_lock(tmp_path: Path) -> None:
    r = rig(tmp_path)
    gate = Gate(AGENT)
    r.link.gate = gate
    first: dict = {}
    thread = _start(r, first, dry_run=True)
    assert gate.entered.wait(10)
    error = expect_failure(lambda: r.run(dry_run=True))
    assert error.code == "TELEPORT_OPERATION_CONFLICT"
    gate.release.set()
    thread.join(10)
    assert first.get("result", {}).get("dryRun") is True


def test_other_agents_are_not_blocked(tmp_path: Path) -> None:
    r = rig(tmp_path)
    other_home = tmp_path / "agents" / "w2"
    (other_home / "workspace").mkdir(parents=True)
    r.host.agents["w2"] = make_agent(other_home, name="w2", uri="agent://owner-1/node-a/w2")
    gate = Gate(AGENT)
    r.link.gate = gate
    first: dict = {}
    thread = _start(r, first, dry_run=True)
    assert gate.entered.wait(10)
    assert r.run("w2", dry_run=True)["agent"] == "w2"
    gate.release.set()
    thread.join(10)


def test_lock_is_released_after_a_failure(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.link.failures["prepare"] = TeleportError("TELEPORT_AGENT_EXISTS", "taken")
    assert expect_failure(r.run).code == "TELEPORT_AGENT_EXISTS"
    del r.link.failures["prepare"]
    assert r.run()["ok"] is True


def test_lock_is_released_after_unresolved(tmp_path: Path) -> None:
    r = rig(tmp_path)
    r.link.failures["receive"] = TeleportError("TELEPORT_UNRESOLVED", "silent")
    assert expect_failure(r.run).code == "TELEPORT_UNRESOLVED"
    del r.link.failures["receive"]
    assert r.run(dry_run=True)["ok"] is True
