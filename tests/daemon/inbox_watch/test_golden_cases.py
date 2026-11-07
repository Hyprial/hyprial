"""Drive the Python inbox-watch core through the language-neutral golden cases.

Each case in ``contract/inbox-watch/cases`` is an interleaved event stream:
a lane asks for its next action (``expect``) or receives the answer to the
one it is waiting on (``respond``), at a given millisecond.  The TypeScript
port runs the same files; see ``contract/inbox-watch/README.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.inbox_watch import (
    Action,
    InboxChange,
    InboxWatch,
    Ok,
    Rejected,
    Unreachable,
    Wait,
)

CASES = Path(__file__).resolve().parents[3] / "contract" / "inbox-watch" / "cases"


def _cases() -> list[Path]:
    return sorted(CASES.glob("*.json"))


def _action_json(action: Action) -> dict[str, Any]:
    out: dict[str, Any] = {"op": action.op}
    if isinstance(action, Wait):
        out["knownMessageIds"] = list(action.known_message_ids)
        if action.hold_ms is not None:
            out["holdMs"] = action.hold_ms
    if action.op == "sleep":
        out["ms"] = action.ms  # type: ignore[union-attr]
    return out


def _outcome(respond: dict[str, Any]) -> Ok | Rejected | Unreachable:
    if "ok" in respond:
        return Ok(respond["ok"])
    if "rejected" in respond:
        return Rejected(respond["rejected"])
    if respond.get("unreachable") is True:
        return Unreachable()
    raise ValueError(f"unknown respond shape {respond}")


def _change_json(change: InboxChange | None) -> dict[str, Any]:
    if change is None or change.empty:
        return {}
    out: dict[str, Any] = {}
    if change.new:
        out["new"] = [row["messageId"] for row in change.new]
    if change.rewake:
        out["rewake"] = [row["messageId"] for row in change.rewake]
        out["rewakeReason"] = change.rewake_reason
    if change.completed:
        out["completed"] = list(change.completed)
    return out


def run_case(case: dict[str, Any]) -> InboxWatch:
    config = case.get("config", {})
    watch = InboxWatch(
        poll_interval_ms=config.get("pollIntervalMs", 500),
        heartbeat_interval_ms=config.get("heartbeatIntervalMs", 1_000),
        hold_ms=config.get("holdMs"),
        use_wait=config.get("useWait", True),
    )
    now = 0
    outstanding: dict[str, Any] = {}
    for index, step in enumerate(case["steps"]):
        where = f"{case['id']} step {index}: {step}"
        at = step.get("at", now)
        assert at >= now, f"{where}: time went backwards"
        now = at
        lane = step["lane"]
        if "rewake" in step:
            watch.request_rewake(step["rewake"], now=step.get("now", True))
            continue
        if "observe" in step:
            watch.observe(step["observe"])
            continue
        if "useWait" in step:
            watch.use_wait = step["useWait"]
            continue
        if "observeRejected" in step:
            watch.observe_rejection(step["observeRejected"])
            continue
        if "expect" in step:
            action = (watch.poll_next if lane == "poll" else watch.heartbeat_next)(now)
            assert _action_json(action) == step["expect"], where
            if action.op not in ("sleep", "quiet"):
                outstanding[lane] = action
            continue
        action = outstanding.pop(lane)
        outcome = _outcome(step["respond"])
        if lane == "poll":
            change = watch.poll_done(action, outcome, now)
        else:
            watch.heartbeat_done(action, outcome, now)
            change = None
        assert _change_json(change) == step.get("emit", {}), where
    for name, value in case.get("counters", {}).items():
        assert watch.counters.get(name, 0) == value, f"{case['id']}: counter {name}"
    return watch


@pytest.mark.parametrize("path", _cases(), ids=lambda path: path.stem)
def test_golden_case(path: Path) -> None:
    run_case(json.loads(path.read_text()))


def test_every_planned_case_exists() -> None:
    present = {path.stem.split("-", 1)[0] for path in _cases()}
    assert {f"G{n}" for n in range(1, 14)} <= present
