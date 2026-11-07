"""AT07 I1: the read-only P2 preflight describes the context and never writes."""

from __future__ import annotations

import copy

from hyprial.daemon.impl.transfer.p2.orchestration.orchestrator import (
    CONFIG,
    IDENTITY,
    INVALID,
    MISSING,
    NATIVE_ROOT,
    PRESENT,
    SESSION,
    TARGET_DERIVES,
    preflight_plan,
)

OWNER = "user:amy.williams@example.com"
ACTOR = "agent:amy.williams@example.com:devusr-dev:pi-agent"


def _plan(*, config=None, session_ref="/sessions/pi/abc.jsonl", agent=True):
    row = {
        "uri": ACTOR,
        "actor": ACTOR,
        "owner": OWNER,
        "config": config,
        "lastSessionId": None,
    }
    return {
        "ok": True,
        "actor": ACTOR,
        "owner": OWNER,
        "nodeId": "devusr-dev",
        "spec": {"name": "pi-agent", "harness": "pi", "sessionRef": session_ref},
        "agent": row if agent else None,
    }


_CONFIG = {"sources": [{"path": "/home/hyprial/agents/pi-agent/config", "required": True}], "discovery": "explicit-only"}


def test_ready_plan_describes_all_four_facts() -> None:
    report = preflight_plan(_plan(config=_CONFIG))
    assert report.ready is True
    assert report.reason == ""
    assert report.item(IDENTITY).status == PRESENT
    assert report.item(CONFIG).status == PRESENT
    assert report.item(CONFIG).detail.startswith("/home/hyprial/agents/pi-agent/config")
    assert report.item(SESSION).status == PRESENT
    assert report.item(NATIVE_ROOT).status == TARGET_DERIVES
    assert report.to_json()["ready"] is True


def test_harness_worker_without_config_is_not_agent_home() -> None:
    report = preflight_plan(_plan(config=None))
    assert report.ready is False
    assert report.reason == f"{CONFIG}:{MISSING}"
    assert report.item(CONFIG).detail.startswith("agent has no config")


def test_absent_agent_row_blocks_identity() -> None:
    report = preflight_plan(_plan(config=_CONFIG, agent=False))
    assert report.ready is False
    assert report.reason == f"{IDENTITY}:{MISSING}"


def test_relative_config_path_is_invalid_not_missing() -> None:
    report = preflight_plan(
        _plan(config={"sources": [{"path": "relative/config", "required": True}], "discovery": "explicit-only"})
    )
    assert report.ready is False
    assert report.reason == f"{CONFIG}:{INVALID}"


def test_missing_session_blocks_strict_resume() -> None:
    report = preflight_plan(_plan(config=_CONFIG, session_ref=None))
    assert report.ready is False
    assert report.reason == f"{SESSION}:{MISSING}"


def test_preflight_does_not_touch_its_input() -> None:
    plan = _plan(config=_CONFIG)
    before = copy.deepcopy(plan)
    preflight_plan(plan)
    assert plan == before


def test_an_agent_row_that_disagrees_with_the_plan_is_invalid_not_missing() -> None:
    """A wrong row is a different failure from an absent one."""

    plan = _plan(config=_CONFIG)
    plan["agent"]["owner"] = "user:someone-else@example.com"

    report = preflight_plan(plan)

    assert report.ready is False
    assert report.reason == f"{IDENTITY}:{INVALID}"
    assert "disagrees with plan actor/owner" in report.item(IDENTITY).detail


def test_a_last_session_id_alone_is_a_clue_not_a_resumable_session() -> None:
    """H-1: ``lastSessionId`` records a hint; it is not proof of a resume."""

    plan = _plan(config=_CONFIG, session_ref=None)
    plan["agent"]["lastSessionId"] = "sess-42"

    report = preflight_plan(plan)

    assert report.ready is False
    assert report.reason == f"{SESSION}:{MISSING}"
    assert "lastSessionId=sess-42" in report.item(SESSION).detail
