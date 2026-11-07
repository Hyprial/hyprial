"""P1-P10, G3p, G4p, G5: the pi attach carrier on the send-links contract.

Runs the shipped ``pi_harness_attach.ts`` under node against the fake daemon
(see ``_pi_attach_support``). The carrier injects daemon-rendered batch text,
reports ``session.turn.started``/``ended`` with one reportId and retries them,
asks ``message.settle_hint`` per injected id at the turn end and obeys it, and
never replies on anyone's behalf (wp5c.md 2, 5, 6.1).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from tests.daemon.harnesses.pi.extension._pi_attach_support import (
    ATTACH_EXTENSION,
    BODY_SENTINEL,
    POLL_MS,
    SDK_AVAILABLE,
    Carrier,
    carrier_for,
    ACTOR,
    SESSION_REF,
    request_message,
)

pytestmark = pytest.mark.skipif(
    not SDK_AVAILABLE,
    reason="node and a pi installation (bundled typebox) are required",
)

SRC_ROOT = Path(__file__).parents[5] / "src" / "hyprial"


@pytest.fixture()
def carrier(tmp_path: Path):
    with carrier_for(tmp_path) as rig:
        yield rig


def _wait_sent(carrier: Carrier, count: int = 1, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answer = carrier.command({"cmd": "noop"})
        if len(answer.get("sent", ())) >= count:
            return answer
        time.sleep(0.03)
    raise TimeoutError(f"no injection; daemon saw {carrier.daemon.methods()}")


def _run_turn(carrier: Carrier) -> None:
    carrier.command({"cmd": "agent_start"})
    carrier.command({"cmd": "consumed"})
    carrier.command({"cmd": "agent_end"})
    carrier.command({"cmd": "settled"})


def _turn_frames(carrier: Carrier) -> list[tuple[int, str, dict[str, Any]]]:
    with carrier.daemon._lock:
        frames = list(carrier.daemon.frames)
    return [
        (index, str(f["method"]), f["params"])
        for index, f in enumerate(frames)
        if f["method"] in {"session.turn.started", "session.turn.ended"}
    ]


def _wait_until(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError("condition not reached")


def _index_of(carrier: Carrier, method: str, nth: int = 0) -> int:
    with carrier.daemon._lock:
        hits = [i for i, f in enumerate(carrier.daemon.frames) if f["method"] == method]
    return hits[nth]


# --------------------------------------------------------------------------- #
# Interactive principal carrier
# --------------------------------------------------------------------------- #
def test_session_ref_file_registers_the_interactive_carrier(tmp_path: Path) -> None:
    binding = tmp_path / "session-ref"
    binding.write_text(SESSION_REF + "\n", encoding="utf-8")
    binding.chmod(0o600)
    rig = Carrier(
        tmp_path,
        managed=False,
        extra_env={
            "HYPRIAL_WORKER_ACTOR": ACTOR,
            "HYPRIAL_WORKER_SESSION_REF_FILE": str(binding),
        },
    )
    try:
        rig.command({"cmd": "start"})
        _wait_until(lambda: bool(rig.daemon.frames_for("session.register")))
        registration = rig.daemon.frames_for("session.register")[0]["params"]
        assert registration["actor"] == ACTOR
        assert registration["sessionRef"] == SESSION_REF
    finally:
        rig.stop()


# --------------------------------------------------------------------------- #
# P1, P2: summary-only injection
# --------------------------------------------------------------------------- #
def test_injection_is_daemon_summary_batch_only(carrier: Carrier) -> None:
    batch_text = (
        "[Harness Network inbox batch: 1 item]\n"
        "messageId: m-1; summary: Please review the patch; bodyBytes: 13"
    )
    carrier.daemon.batch_text = batch_text
    row = request_message("m-1", "connector-must-not-render-this")
    row["bodyBytes"] = 13
    carrier.daemon.pending.append(row)
    carrier.command({"cmd": "start"})
    text = _wait_sent(carrier)["sent"][0]["text"]

    assert text == batch_text
    assert "connector-must-not-render-this" not in text
    assert BODY_SENTINEL not in text


def test_injection_does_not_reformat_daemon_batch_text(carrier: Carrier) -> None:
    batch_text = "[daemon bytes]\n<<delimiter-like text>> 来源"
    carrier.daemon.batch_text = batch_text
    carrier.daemon.pending.append(request_message("m-1", "local-summary"))
    carrier.command({"cmd": "start"})

    assert _wait_sent(carrier)["sent"][0]["text"] == batch_text


# --------------------------------------------------------------------------- #
# P3: settle by hint, never reply
# --------------------------------------------------------------------------- #
def test_settled_run_asks_settle_hint_and_never_replies(carrier: Carrier) -> None:
    carrier.daemon.hints = {"m-1": "ack", "m-2": "keep"}
    carrier.daemon.pending.append(request_message("m-1", "first?"))
    carrier.daemon.pending.append(request_message("m-2", "second?"))
    carrier.command({"cmd": "start"})
    _wait_sent(carrier)
    _run_turn(carrier)

    hints = carrier.wait_for_frames("message.settle_hint", 2)
    carrier.wait_for_frames("message.ack", 1)
    time.sleep(POLL_MS * 6 / 1000)
    asked = [f["params"]["messageId"] for f in carrier.daemon.frames_for("message.settle_hint")]
    assert sorted(asked) == ["m-1", "m-2"] and len(hints) >= 2
    acks = carrier.daemon.frames_for("message.ack")
    assert [f["params"]["messageId"] for f in acks] == ["m-1"]
    assert carrier.daemon.frames_for("message.reply") == []
    for frame in carrier.daemon.frames_for("message.settle_hint"):
        assert frame["params"]["sessionRef"]


# --------------------------------------------------------------------------- #
# P4, P5: turn boundaries
# --------------------------------------------------------------------------- #
def test_turn_boundaries_are_reported_in_order(carrier: Carrier) -> None:
    carrier.daemon.pending.append(request_message("m-1", "status?"))
    carrier.command({"cmd": "start"})
    _wait_sent(carrier)
    carrier.command({"cmd": "agent_start"})
    carrier.wait_for_frames("session.turn.started", 1)
    carrier.command({"cmd": "consumed"})
    carrier.command({"cmd": "agent_end"})
    carrier.command({"cmd": "settled"})
    carrier.wait_for_frames("session.turn.ended", 1)
    carrier.wait_for_frames("message.settle_hint", 1)

    turns = _turn_frames(carrier)
    assert [m for _, m, _ in turns] == ["session.turn.started", "session.turn.ended"]
    started, ended = turns[0][2], turns[1][2]
    assert started["reportId"] and started["actor"] and started["sessionRef"]
    assert ended["reportIds"] == [started["reportId"]]
    assert ended["status"] == "completed"
    assert turns[1][0] < _index_of(carrier, "message.settle_hint")


def test_started_is_accepted_before_the_batch_capture(carrier: Carrier) -> None:
    carrier.daemon.turn_delay["session.turn.started"] = 0.3
    carrier.daemon.pending.append(request_message("m-1", "status?"))
    carrier.command({"cmd": "start"})
    _wait_sent(carrier)

    d = carrier.daemon
    with d._lock:
        frames = list(d.frames)
        times = list(d.received_at)
    started = next(i for i, f in enumerate(frames) if f["method"] == "session.turn.started")
    captured = next(
        i for i, f in enumerate(frames) if f["method"] == "message.pending.batch"
    )
    assert started < captured
    # The daemon held its answer for 0.3 s: batch capture can only have been
    # sent after that answer arrived.
    assert times[captured] - times[started] >= 0.28


# --------------------------------------------------------------------------- #
# P6: retry exhaustion
# --------------------------------------------------------------------------- #
def test_retry_exhaustion_reports_failed_and_settles_nothing(carrier: Carrier) -> None:
    carrier.daemon.pending.append(request_message("m-1", "status?"))
    carrier.command({"cmd": "start"})
    _wait_sent(carrier)
    carrier.command({"cmd": "agent_start"})
    for _ in range(3):
        carrier.command({"cmd": "settled"})
    ended = carrier.wait_for_frames("session.turn.ended", 1)
    assert ended[0]["params"]["status"] == "failed"
    carrier.command({"cmd": "settled"})
    time.sleep(POLL_MS * 8 / 1000)

    assert carrier.daemon.frames_for("message.reply") == []
    assert carrier.daemon.frames_for("message.ack") == []
    assert [m["messageId"] for m in carrier.daemon.pending] == ["m-1"]
    assert len(carrier.command({"cmd": "noop"})["sent"]) == 3
    assert any(n["type"] == "warning" for n in carrier.command({"cmd": "noop"})["notifications"])


# --------------------------------------------------------------------------- #
# P7: keep is not reinjected until a daemon epoch change
# --------------------------------------------------------------------------- #
def test_kept_message_is_not_reinjected_until_rewake(carrier: Carrier) -> None:
    carrier.daemon.hints = {"m-1": "keep"}
    carrier.daemon.pending.append(request_message("m-1", "status?"))
    carrier.command({"cmd": "start"})
    _wait_sent(carrier)
    _run_turn(carrier)
    carrier.wait_for_frames("message.settle_hint", 1)
    time.sleep(POLL_MS * 12 / 1000)
    assert len(carrier.command({"cmd": "noop"})["sent"]) == 1
    assert carrier.daemon.frames_for("message.ack") == []

    carrier.daemon.epoch = "epoch-2"
    answer = _wait_sent(carrier, 2)
    time.sleep(POLL_MS * 12 / 1000)
    answer = carrier.command({"cmd": "noop"})
    assert len(answer["sent"]) == 2
    assert carrier.daemon.frames_for("message.reply") == []


# --------------------------------------------------------------------------- #
# P8: shutdown with an open turn
# --------------------------------------------------------------------------- #
def test_shutdown_with_open_turn_reports_failed(carrier: Carrier) -> None:
    carrier.command({"cmd": "start"})
    carrier.command({"cmd": "agent_start"})
    carrier.wait_for_frames("session.turn.started", 1)
    carrier.command({"cmd": "shutdown"})
    ended = carrier.wait_for_frames("session.turn.ended", 1)
    carrier.wait_for_frames("session.unregister", 1)

    assert ended[0]["params"]["status"] == "failed"
    assert _index_of(carrier, "session.turn.ended") < _index_of(carrier, "session.unregister")


# --------------------------------------------------------------------------- #
# P9: report retries keep one reportId
# --------------------------------------------------------------------------- #
def _ended_ids(carrier: Carrier) -> list[str]:
    return [
        rid
        for _, m, p in _turn_frames(carrier)
        if m == "session.turn.ended"
        for rid in p["reportIds"]
    ]


def test_turn_report_retries_with_the_same_report_id(tmp_path: Path) -> None:
    # (a) accepted by the daemon, response lost: retried, accepted once.
    a = tmp_path / "a"
    a.mkdir()
    with carrier_for(a) as rig:
        rig.daemon.drop_response["session.turn.ended"] = 1
        rig.command({"cmd": "start"})
        rig.command({"cmd": "agent_start"})
        rig.command({"cmd": "agent_end"})
        rig.command({"cmd": "settled"})
        _wait_until(lambda: len(_ended_ids(rig)) >= 2)
        assert len(set(_ended_ids(rig))) == 1
        assert len([t for t in rig.daemon.accepted_turns if t[0] == "ended"]) == 1

    # (b) failed before arrival: the next tick retries and succeeds.
    b = tmp_path / "b"
    b.mkdir()
    with carrier_for(b) as rig:
        rig.daemon.fail_next("session.turn.ended", "DAEMON_BUSY")
        rig.command({"cmd": "start"})
        rig.command({"cmd": "agent_start"})
        rig.command({"cmd": "settled"})
        _wait_until(lambda: len([t for t in rig.daemon.accepted_turns if t[0] == "ended"]) == 1)
        assert len(set(_ended_ids(rig))) == 1
        assert len(_ended_ids(rig)) == 2

    # (c) started unconfirmed: ended is not sent and the id does not change.
    c = tmp_path / "c"
    c.mkdir()
    with carrier_for(c) as rig:
        rig.daemon.fail_next("session.turn.started", "DAEMON_BUSY", times=1000)
        rig.command({"cmd": "start"})
        rig.command({"cmd": "agent_start"})
        rig.command({"cmd": "settled"})
        _wait_until(lambda: len(rig.daemon.frames_for("session.turn.started")) >= 3)
        assert rig.daemon.frames_for("session.turn.ended") == []
        ids = {f["params"]["reportId"] for f in rig.daemon.frames_for("session.turn.started")}
        assert len(ids) == 1
        rig.daemon.clear_errors("session.turn.started")
        _wait_until(lambda: len(rig.daemon.frames_for("session.turn.ended")) >= 1)
        assert _ended_ids(rig) == list(ids)

    # (d) SESSION_SUPERSEDED: the queue is dropped, nothing is sent again.
    d = tmp_path / "d"
    d.mkdir()
    with carrier_for(d) as rig:
        rig.daemon.fail_next("session.turn.started", "SESSION_SUPERSEDED")
        rig.command({"cmd": "start"})
        rig.command({"cmd": "agent_start"})
        rig.command({"cmd": "settled"})
        time.sleep(POLL_MS * 6 / 1000)
        before = len(_turn_frames(rig))
        time.sleep(POLL_MS * 10 / 1000)
        assert len(_turn_frames(rig)) == before
        assert rig.daemon.frames_for("session.turn.ended") == []


def test_report_retry_survives_a_timer_that_fires_before_the_deadline(
    carrier: Carrier,
) -> None:
    """B7 lost wakeup: the retry timer fires while ``Date.now()`` is still
    before ``reportRetryAt`` (rounding, or another flush pushing the deadline
    out).  The early return must not strand the queue: nothing else flushes an
    idle session, so the report would be held for good.
    """

    carrier.daemon.fail_next("session.turn.started", "DAEMON_BUSY")
    carrier.command({"cmd": "start"})
    carrier.command({"cmd": "agent_start"})
    # Within the first backoff window: the timer is pending, the clock lags.
    carrier.command({"cmd": "skew_clock", "ms": 30, "forMs": 150})
    carrier.command({"cmd": "settled"})
    _wait_until(
        lambda: any(t[0] == "ended" for t in carrier.daemon.accepted_turns), timeout=5.0
    )
    assert len({f["params"]["reportId"] for f in carrier.daemon.frames_for("session.turn.started")}) == 1


# --------------------------------------------------------------------------- #
# Guards: settle only after agent_settled; transient settle/ack failures
# --------------------------------------------------------------------------- #
def test_no_settle_hint_or_ended_between_agent_end_and_agent_settled(
    carrier: Carrier,
) -> None:
    carrier.daemon.pending.append(request_message("m-1", "status?"))
    carrier.command({"cmd": "start"})
    _wait_sent(carrier)
    carrier.command({"cmd": "agent_start"})
    carrier.command({"cmd": "consumed"})
    carrier.command({"cmd": "agent_end"})
    time.sleep(POLL_MS * 8 / 1000)
    assert carrier.daemon.frames_for("message.settle_hint") == []
    assert carrier.daemon.frames_for("session.turn.ended") == []

    carrier.command({"cmd": "settled"})
    carrier.wait_for_frames("message.settle_hint", 1)
    carrier.wait_for_frames("session.turn.ended", 1)
    time.sleep(POLL_MS * 6 / 1000)
    assert len(carrier.daemon.frames_for("message.settle_hint")) == 1


@pytest.mark.parametrize("method", ["message.settle_hint", "message.ack"])
def test_transient_settlement_failure_is_retried_without_reinjection(
    carrier: Carrier, method: str
) -> None:
    carrier.daemon.fail_next(method, "DAEMON_BUSY", times=2)
    carrier.daemon.pending.append(request_message("m-1", "status?"))
    carrier.command({"cmd": "start"})
    _wait_sent(carrier)
    _run_turn(carrier)
    carrier.wait_for_frames("message.ack", 1)
    time.sleep(POLL_MS * 8 / 1000)

    assert len(carrier.daemon.frames_for(method)) >= 3
    assert len(carrier.command({"cmd": "noop"})["sent"]) == 1
    assert carrier.daemon.frames_for("message.reply") == []


def test_ambiguous_sender_standing_is_rendered_only_by_daemon(
    carrier: Carrier,
) -> None:
    batch_text = "[daemon sender: ambiguous between users guest-a, guest-b]"
    carrier.daemon.batch_text = batch_text
    message = request_message("m-1", "status?")
    message["from"] = "adapter:lark:hq-feishu"
    message["to"] = ACTOR
    message["bodyBytes"] = 7
    message["origin"] = {
        "chatType": "p2p",
        "provider": "lark",
        "sender": {
            "platformId": "ou_x",
            "standing": "ambiguous",
            "candidateOwners": [],
            "candidateUsers": ["guest-0a1b2c3d", "guest-4e5f6a7b"],
        },
    }
    carrier.daemon.pending.append(message)
    carrier.command({"cmd": "start"})
    text = _wait_sent(carrier)["sent"][0]["text"]

    assert text == batch_text


# --------------------------------------------------------------------------- #
# P10: R3 and the recorded restart boundary
# --------------------------------------------------------------------------- #
def test_extension_restart_boundary_is_recorded(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    rig = Carrier(first_dir)
    try:
        rig.daemon.hints = {"m-1": "keep"}
        rig.daemon.pending.append(request_message("m-1", "status?"))
        rig.command({"cmd": "start"})
        _wait_sent(rig)
        _run_turn(rig)
        rig.wait_for_frames("message.settle_hint", 1)
        # Same epoch, ten polls: a kept message is never injected again.
        polls = len(rig.daemon.frames_for("message.pending.list"))
        _wait_until(lambda: len(rig.daemon.frames_for("message.pending.list")) >= polls + 10)
        assert len(rig.command({"cmd": "noop"})["sent"]) == 1
        rig.command({"cmd": "shutdown"})
        rig.command({"cmd": "quit"})

        # The restarted extension is a new session: kept is lost by design, so
        # the still-pending row is injected as an ordinary pending message once.
        second_dir = tmp_path / "second"
        second_dir.mkdir()
        rig.daemon.hints = {}
        again = Carrier(
            second_dir,
            daemon=rig.daemon,
            extra_env={"HYPRIAL_WORKER_SESSION_REF": "attach-session-2"},
        )
        try:
            again.command({"cmd": "start"})
            _wait_sent(again)
            polls = len(rig.daemon.frames_for("message.pending.list"))
            _wait_until(lambda: len(rig.daemon.frames_for("message.pending.list")) >= polls + 6)
            assert len(again.command({"cmd": "noop"})["sent"]) == 1
        finally:
            again.stop()
    finally:
        rig.stop()


# --------------------------------------------------------------------------- #
# Source gates
# --------------------------------------------------------------------------- #
def test_pi_attach_never_replies() -> None:
    text = ATTACH_EXTENSION.read_text(encoding="utf-8")
    for needle in (
        '"message.reply"',
        "finalAssistantText",
        "failOutstanding",
        "settleMessage",
        "CODE_REPLY_UNAVAILABLE",
    ):
        assert needle not in text, needle


def test_pi_attach_never_reads_the_body() -> None:
    assert "record.message" not in ATTACH_EXTENSION.read_text(encoding="utf-8")


def test_no_prompt_promises_an_automatic_reply() -> None:
    offenders = [
        str(path)
        for path in SRC_ROOT.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".ts", ".mjs", ".md", ".txt", ".json"}
        and any(
            phrase in path.read_text(encoding="utf-8", errors="ignore")
            for phrase in ("sends your final reply back", "your final reply is sent back")
        )
    ]
    assert offenders == []
