"""E1: the pi attach carrier publishes its current run set on recovery.

The extension opens a turn before it consumes a batch and on ``agent_start``.
On registration and on a daemon-generation refresh it now reports exactly that
open run (with producer-local elapsed time), so a daemon that lost its receipt
ledger recovers the state without any inbox traffic or extra model turn.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from tests.daemon.harnesses.pi.extension._pi_attach_support import (
    POLL_MS,
    SDK_AVAILABLE,
    Carrier,
    carrier_for,
    request_message,
)

pytestmark = pytest.mark.skipif(
    not SDK_AVAILABLE,
    reason="node and a pi installation (bundled typebox) are required",
)

TURN_REFRESH = "session.turn.refresh"
CAPABILITIES = {
    "wholeSessionTurns": True,
    "preparation": True,
    "permissionWait": False,
    "producerActivity": False,
}


@pytest.fixture()
def carrier(tmp_path: Path):
    with carrier_for(tmp_path) as rig:
        yield rig


def _wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError("condition not reached")


def _wait_refreshes(carrier: Carrier, count: int) -> list[dict[str, Any]]:
    _wait_until(lambda: len(carrier.daemon.frames_for(TURN_REFRESH)) >= count)
    return [frame["params"] for frame in carrier.daemon.frames_for(TURN_REFRESH)]


def test_registration_reports_an_empty_current_run_set(carrier: Carrier) -> None:
    carrier.command({"cmd": "start"})
    params = _wait_refreshes(carrier, 1)[0]

    assert params["actor"] and params["sessionRef"]
    assert params["schemaVersion"] == 1
    assert params["daemonEpoch"] == carrier.daemon.epoch
    assert params["openRuns"] == []
    assert params["seq"] == 0
    assert isinstance(params["producerEpoch"], str) and params["producerEpoch"]
    assert params["capabilities"] == CAPABILITIES
    # Observation only: no extra consumption and no business turn report.
    assert carrier.daemon.frames_for("session.turn.started") == []
    assert carrier.daemon.frames_for("message.pending.batch") == []
    assert carrier.command({"cmd": "noop"})["sent"] == []


def test_reconnect_reports_the_open_run_with_elapsed_time(carrier: Carrier) -> None:
    carrier.command({"cmd": "start"})
    first = _wait_refreshes(carrier, 1)[0]
    carrier.command({"cmd": "agent_start"})
    carrier.wait_for_frames("session.turn.started", 1)
    open_id = carrier.daemon.frames_for("session.turn.started")[0]["params"]["reportId"]

    carrier.daemon.epoch = "epoch-2"
    carrier.wait_for_frames("session.refresh", 1)
    params = _wait_refreshes(carrier, 2)[1]

    assert len(params["openRuns"]) == 1
    row = params["openRuns"][0]
    assert row["reportId"] == open_id
    assert row["phase"] == "executing"
    assert row["elapsedMs"] >= 0
    assert params["seq"] == first["seq"] + 1
    assert params["producerEpoch"] == first["producerEpoch"]
    assert params["daemonEpoch"] == "epoch-2"
    assert first["daemonEpoch"] != params["daemonEpoch"]


def test_a_queued_turn_is_reported_as_preparation(carrier: Carrier) -> None:
    carrier.daemon.pending.append(request_message("m-1", "status?"))
    carrier.command({"cmd": "start"})
    # The batch is captured and a turn opens before any model run starts.
    carrier.wait_for_frames("session.turn.started", 1)
    carrier.daemon.epoch = "epoch-2"
    carrier.wait_for_frames("session.refresh", 1)
    params = _wait_refreshes(carrier, 2)[1]

    assert [row["phase"] for row in params["openRuns"]] == ["prepared"]


def test_reconnect_after_the_turn_ended_reports_no_open_run(carrier: Carrier) -> None:
    carrier.command({"cmd": "start"})
    carrier.command({"cmd": "agent_start"})
    carrier.wait_for_frames("session.turn.started", 1)
    carrier.command({"cmd": "settled"})
    carrier.wait_for_frames("session.turn.ended", 1)

    carrier.daemon.epoch = "epoch-2"
    carrier.wait_for_frames("session.refresh", 1)
    params = _wait_refreshes(carrier, 2)[1]
    assert params["openRuns"] == []


def test_an_old_daemon_without_the_method_keeps_delivery_working(
    carrier: Carrier,
) -> None:
    carrier.daemon.fail_next(TURN_REFRESH, "METHOD_NOT_FOUND", times=1000)
    carrier.daemon.pending.append(request_message("m-1", "status?"))
    carrier.command({"cmd": "start"})

    _wait_until(lambda: bool(carrier.daemon.frames_for(TURN_REFRESH)))
    deadline = time.monotonic() + 10.0
    answer: dict[str, Any] = {}
    while time.monotonic() < deadline:
        answer = carrier.command({"cmd": "noop"})
        if len(answer["sent"]) >= 1:
            break
        time.sleep(0.03)
    assert len(answer["sent"]) == 1
    carrier.wait_for_frames("session.turn.started", 1)
    time.sleep(POLL_MS * 4 / 1000)
    # A refused observation call is not retried in a loop.
    assert len(carrier.daemon.frames_for(TURN_REFRESH)) == 1


def test_a_lost_initial_refresh_is_recovered_by_ordinary_polling(
    carrier: Carrier,
) -> None:
    carrier.daemon.fail_next(TURN_REFRESH, "DAEMON_BUSY", times=2)
    carrier.daemon.pending.append(request_message("m-1", "status?"))
    carrier.command({"cmd": "start"})

    _wait_until(lambda: len(carrier.daemon.frames_for(TURN_REFRESH)) >= 3)
    assert carrier.daemon.frames_for(TURN_REFRESH)[-1]["params"]["seq"] >= 2
    # Delivery is untouched: one batch, one injection, and the turn reports.
    carrier.wait_for_frames("message.pending.batch", 1)
    deadline = time.monotonic() + 10.0
    sent: dict[str, Any] = {}
    while time.monotonic() < deadline:
        sent = carrier.command({"cmd": "noop"})
        if len(sent["sent"]) >= 1:
            break
        time.sleep(0.03)
    assert len(sent["sent"]) == 1
    carrier.wait_for_frames("session.turn.started", 1)


def test_same_epoch_ttl_recovery_refreshes_once_per_gap(
    carrier: Carrier, monkeypatch,
) -> None:
    from hyprial.daemon.impl.session_actor.internals import _SessionRuntimeState

    owner = _SessionRuntimeState("epoch-1", lease_ttl_seconds=1)
    clock = {"now": 0}
    respond = carrier.daemon._respond

    def with_contact_gap(frame):
        answer = respond(frame)
        result = answer.get("result")
        if result is None:
            return answer
        method = frame["method"]
        if method == "session.heartbeat":
            result["observationGap"] = owner.read(
                "actor", "session", now_ms=clock["now"], ttl_seconds=1,
            ).observation_gap
        if method == TURN_REFRESH:
            gap = owner.read("actor", "session", now_ms=clock["now"], ttl_seconds=1)
            owner.acknowledge_observation_gap(
                "actor", "session", now_ms=clock["now"], ttl_seconds=1,
                gap_revision=gap.observation_gap_revision,
            )
        return answer

    monkeypatch.setattr(carrier.daemon, "_respond", with_contact_gap)
    try:
        owner.register("actor", "session", 0, confirmed=True)
        carrier.command({"cmd": "start"})
        _wait_refreshes(carrier, 1)
        carrier.daemon.turn_delay[TURN_REFRESH] = 0.2
        for round_number in (1, 2):
            clock["now"] += 2_000
            owner.register("actor", "session", clock["now"], confirmed=True)
            _wait_refreshes(carrier, round_number + 1)
            _wait_until(lambda: not owner.read(
                "actor", "session", now_ms=clock["now"], ttl_seconds=1,
            ).observation_gap)
            heartbeats = len(carrier.daemon.frames_for("session.heartbeat"))
            _wait_until(lambda: len(carrier.daemon.frames_for("session.heartbeat")) >= heartbeats + 3)
            assert len(carrier.daemon.frames_for(TURN_REFRESH)) == round_number + 1
        assert len(carrier.daemon.frames_for("session.register")) == 1
        assert carrier.daemon.frames_for("session.refresh") == []
        assert carrier.daemon.frames_for("message.pending.batch") == []
        assert carrier.daemon.frames_for("session.turn.started") == []
        assert carrier.command({"cmd": "noop"})["sent"] == []
    finally:
        owner.lifecycle_observations.close()


def test_old_inflight_refusal_does_not_cache_against_new_epoch(carrier: Carrier, monkeypatch) -> None:
    carrier.command({"cmd": "start"})
    _wait_refreshes(carrier, 1)
    gap = {"pending": True}
    respond = carrier.daemon._respond

    def with_gap_hint(frame):
        answer = respond(frame)
        result = answer.get("result")
        if result is not None:
            if frame["method"] == "session.heartbeat":
                result["observationGap"] = gap["pending"]
            elif frame["method"] == TURN_REFRESH:
                gap["pending"] = False
        return answer

    monkeypatch.setattr(carrier.daemon, "_respond", with_gap_hint)
    carrier.daemon.turn_delay[TURN_REFRESH] = 0.5
    carrier.daemon.fail_next(TURN_REFRESH, "METHOD_NOT_FOUND")
    _wait_refreshes(carrier, 2)
    carrier.daemon.epoch = "epoch-2"
    carrier.wait_for_frames("session.refresh", 1)
    _wait_until(lambda: not gap["pending"])
    polls = len(carrier.daemon.frames_for("message.pending.list"))
    _wait_until(lambda: len(carrier.daemon.frames_for("message.pending.list")) >= polls + 12)
    assert len(carrier.daemon.frames_for(TURN_REFRESH)) == 3
    gap["pending"] = True
    _wait_until(lambda: not gap["pending"])
    assert len(carrier.daemon.frames_for(TURN_REFRESH)) == 4
    assert len(carrier.daemon.frames_for("session.register")) == 1


def test_stale_daemon_epoch_refusal_keeps_refresh_owed(carrier: Carrier, monkeypatch) -> None:
    respond = carrier.daemon._respond
    attempts = []

    def refuse_once(frame):
        answer = respond(frame)
        if frame["method"] == TURN_REFRESH:
            attempts.append(frame["params"])
            if len(attempts) == 1:
                answer["result"].update(applied=False, rejectionReason="stale_daemon_epoch")
        return answer

    monkeypatch.setattr(carrier.daemon, "_respond", refuse_once)
    carrier.command({"cmd": "start"})
    _wait_refreshes(carrier, 2)
    polls = len(carrier.daemon.frames_for("message.pending.list"))
    _wait_until(lambda: len(carrier.daemon.frames_for("message.pending.list")) >= polls + 8)
    assert len(attempts) == 2
    assert attempts[1]["seq"] > attempts[0]["seq"]
    assert attempts[1]["daemonEpoch"] == carrier.daemon.epoch
    assert carrier.daemon.frames_for("message.pending.batch") == []


def test_an_unknown_method_is_cached_instead_of_retried(carrier: Carrier) -> None:
    carrier.daemon.fail_next(TURN_REFRESH, "METHOD_NOT_FOUND", times=1000)
    carrier.command({"cmd": "start"})
    _wait_until(lambda: bool(carrier.daemon.frames_for(TURN_REFRESH)))

    # Many further poll/heartbeat contacts: the cached refusal is not retried.
    polls = len(carrier.daemon.frames_for("message.pending.list"))
    _wait_until(
        lambda: len(carrier.daemon.frames_for("message.pending.list")) >= polls + 8
    )
    assert len(carrier.daemon.frames_for(TURN_REFRESH)) == 1
