'Behavioural tests for the pi interactive attach carrier extension.\n\nSame approach as tests/daemon/harnesses/pi/extension/test_pi_harness_bridge_extension.py: the shipped\n``pi_harness_attach.ts`` runs under plain node with a stubbed ExtensionAPI,\nagainst a fake daemon speaking the real version-1 IPC.  The difference is\nthat the attach carrier is *reactive* (poll loop, pi events), so the driver\nis a long-lived process steered over stdin: pytest watches the daemon frames\nand fires pi events (session_start, message_start, agent_end, agent_settled,\nsession_shutdown) at the exact moments a real pi runtime would.\n\nWhat is pinned here (research risk table -> tests):\n\n* register on session_start with source "pi-extension" / runtime\n  "pi_interactive"; unregister on session_shutdown;\n* pending-list polling is observation-only; `message.pending.batch` is the\n  sole consuming read at the injection boundary;\n* injection is ALWAYS sendUserMessage(..., {deliverAs: "followUp"}) and the\n  extension never consults ctx.isIdle() (the stub throws if it does) — the\n  idle/streaming race is designed out, not checked away;\n* completion is reply/ack only: an accepted injection never counts as\n  delivery, and the `input` event pseudo-positive is not used as proof\n  (consumption is proven by the exact injected text entering a run);\n* SESSION_SUPERSEDED -> quiet (never re-register); STALE_SESSION ->\n  re-register; daemon down -> backoff and recovery without crashing;\n* MESSAGE_REPLY_UNAVAILABLE (the model replied via harness_reply itself) is\n  benign, never a retry storm.\n\nSkipped when node or a pi installation (for its bundled typebox) is absent.\n'

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.daemon.harnesses.pi.extension._pi_attach_support import (
    ACTOR,
    POLL_MS,
    SESSION_REF,
    Carrier,
    FakeDaemon,
    SDK_AVAILABLE,
    carrier_for,
    request_message,
)

pytestmark = pytest.mark.skipif(
    not SDK_AVAILABLE,
    reason="node and a pi installation (bundled typebox) are required",
)


@pytest.fixture()
def carrier(tmp_path: Path):
    with carrier_for(tmp_path) as rig:
        yield rig


def test_registers_on_session_start_with_pi_carrier_identity(carrier: Carrier) -> None:
    answer = carrier.command({"cmd": "start"})
    assert answer["ready"] is True

    (frame,) = carrier.wait_for_frames("session.register", 1)
    assert frame["version"] == 1
    params = frame["params"]
    assert params["actor"] == ACTOR
    assert params["sessionRef"] == SESSION_REF
    assert params["source"] == "pi-extension"
    assert params["runtime"] == "pi_interactive"
    assert params["command"] == ["pi"]
    assert isinstance(params["cwd"], str) and params["cwd"]
    # No detached-tmux env on this launch: the field must be absent, not null.
    assert "tmuxSession" not in params


def test_resume_registration_uses_the_dedicated_guarded_method(tmp_path: Path) -> None:
    rig = Carrier(tmp_path, extra_env={"HYPRIAL_RESUME_GUARDED": "1"})
    try:
        answer = rig.command({"cmd": "start"})
        assert answer["ready"] is True
        (frame,) = rig.wait_for_frames("session.register_guarded", 1)
        assert frame["params"]["actor"] == ACTOR
        assert frame["params"]["sessionRef"] == SESSION_REF
        assert rig.daemon.frames_for("session.register") == []
    finally:
        rig.stop()


def test_unconfirmed_guarded_registration_is_retried_and_never_lists(
    tmp_path: Path,
) -> None:
    # A daemon that answers register_guarded without confirming its
    # live-incumbent check must not count as registered.
    rig = Carrier(tmp_path, extra_env={"HYPRIAL_RESUME_GUARDED": "1"})
    rig.daemon.confirm_guard = False
    try:
        answer = rig.command({"cmd": "start"})
        assert answer["ready"] is True
        rig.wait_for_frames("session.register_guarded", 2)
        assert rig.daemon.frames_for("message.pending.list") == []
        assert rig.daemon.frames_for("message.pending.wait") == []
    finally:
        rig.stop()


def test_register_includes_tmux_session_on_detached_launch(tmp_path: Path) -> None:
    rig = Carrier(
        tmp_path,
        extra_env={"HYPRIAL_WORKER_TMUX_SESSION": "hyprial-agent-op-node-1-attach-pi"},
    )
    try:
        answer = rig.command({"cmd": "start"})
        assert answer["ready"] is True

        (frame,) = rig.wait_for_frames("session.register", 1)
        assert frame["params"]["tmuxSession"] == "hyprial-agent-op-node-1-attach-pi"
    finally:
        rig.stop()


def test_poll_observes_without_marking_fetched(carrier: Carrier) -> None:
    carrier.command({"cmd": "start"})
    carrier.wait_for_frames("message.pending.list", 1)

    for frame in carrier.daemon.frames_for("message.pending.list"):
        assert "fetched" not in frame["params"]


def test_wait_names_every_id_from_the_last_observation(carrier: Carrier) -> None:
    # The batch is answered (pi's own completed set now holds both ids) but
    # the daemon still lists them: the wait must name every listed id, never
    # pi's completed set -- else a still-listed message ends every hold.
    carrier.daemon.pending.extend(
        [
            request_message("m-1", "first?"),
            request_message("m-2", "second?"),
        ]
    )
    carrier.command({"cmd": "start"})
    _wait_injection(carrier)
    carrier.command({"cmd": "consumed"})
    carrier.command({"cmd": "agent_end"})
    carrier.command({"cmd": "settled"})

    frames = carrier.wait_for_frames("message.pending.wait", 1)
    frame = frames[-1]
    assert frame["params"]["knownMessageIds"] == ["m-1", "m-2"]
    assert frame["params"]["holdMs"] == 5000
    assert frame["params"]["actor"] == ACTOR
    assert frame["params"]["sessionRef"] == SESSION_REF


def test_epoch_change_refreshes_and_reinjects_the_backlog(
    carrier: Carrier,
) -> None:
    carrier.daemon.pending.append(request_message("m-1", "status?"))
    carrier.command({"cmd": "start"})
    first = _wait_injection(carrier)
    assert len(first["sent"]) == 1

    carrier.daemon.epoch = "epoch-2"
    carrier.wait_for_frames("session.refresh", 1)
    deadline = time.monotonic() + 10.0
    answer: dict[str, Any] = {}
    while time.monotonic() < deadline:
        answer = carrier.command({"cmd": "noop"})
        if len(answer["sent"]) >= 2:
            break
        time.sleep(0.05)
    assert len(answer["sent"]) == 2
    assert answer["sent"][1] == answer["sent"][0]


def test_new_message_uses_batch_as_the_only_consumption_boundary(
    carrier: Carrier,
) -> None:
    carrier.daemon.pending.append(request_message("m-1", "status?"))
    carrier.command({"cmd": "start"})

    carrier.wait_for_frames("message.pending.list", 1)
    answer = _wait_injection(carrier)
    assert answer["sent"] == [
        {
            "text": "[Fake daemon inbox batch cursor=cursor-pi-attach-1]\nm-1",
            "options": {"deliverAs": "followUp"},
        }
    ]

    assert all(
        "fetched" not in frame["params"]
        for frame in carrier.daemon.frames_for("message.pending.list")
    )
    (batch,) = carrier.daemon.frames_for("message.pending.batch")
    assert batch["params"]["actor"] == ACTOR
    assert batch["params"]["sessionRef"] == SESSION_REF
    # accepted != delivered: no reply/ack before the run actually settles.
    assert carrier.daemon.frames_for("message.reply") == []
    assert carrier.daemon.frames_for("message.ack") == []


def test_injection_is_the_daemon_batch_text_byte_for_byte(carrier: Carrier) -> None:
    sentinel = "[Harness Network inbox batch cursor=pi-attach]\nBYTE-EXACT-秘密"
    carrier.daemon.batch_text = sentinel
    carrier.daemon.pending.append(request_message("m-1", "must not be rendered"))
    carrier.command({"cmd": "start"})

    answer = _wait_injection(carrier)

    assert answer["sent"] == [
        {"text": sentinel, "options": {"deliverAs": "followUp"}}
    ]
    (frame,) = carrier.daemon.frames_for("message.pending.batch")
    assert frame["params"]["actor"] == ACTOR
    assert frame["params"]["sessionRef"] == SESSION_REF


def test_sender_metadata_is_not_rendered_again_by_pi(carrier: Carrier) -> None:
    sentinel = "[Daemon-rendered sender standing]\nBYTE-EXACT-来源"
    carrier.daemon.batch_text = sentinel
    message = request_message("m-1", "status?")
    message["from"] = "adapter:lark:hq-feishu"
    message["to"] = ACTOR
    message["origin"] = {
        "chatType": "p2p",
        "provider": "lark",
        "sender": {
            "displayName": "林懿伦",
            "owner": "bob.smith@example.com",
            "standing": "observed",
        },
    }
    carrier.daemon.pending.append(message)
    carrier.command({"cmd": "start"})
    answer = _wait_injection(carrier)

    assert answer["sent"][0]["text"] == sentinel



def test_two_pending_requests_batch_into_one_injection(carrier: Carrier) -> None:
    carrier.daemon.pending.append(request_message("m-1", "first?"))
    carrier.daemon.pending.append(request_message("m-2", "second?"))
    carrier.command({"cmd": "start"})
    answer = _wait_injection(carrier)

    assert len(answer["sent"]) == 1
    assert "m-1" in answer["sent"][0]["text"]
    assert "m-2" in answer["sent"][0]["text"]

    carrier.command({"cmd": "consumed"})
    carrier.command({"cmd": "agent_end"})
    carrier.command({"cmd": "settled"})
    acks = carrier.wait_for_frames("message.ack", 2)
    assert {f["params"]["messageId"] for f in acks} == {"m-1", "m-2"}
    assert carrier.daemon.frames_for("message.reply") == []


def test_completed_message_is_never_reinjected(carrier: Carrier) -> None:
    # The daemon keeps the row pending (no ack processed server-side here);
    # the carrier's own completion memory must still prevent a second turn.
    carrier.daemon.pending.append(request_message("m-1", "status?"))
    carrier.command({"cmd": "start"})
    answer = _wait_injection(carrier)
    carrier.command({"cmd": "consumed"})
    carrier.command({"cmd": "agent_end"})
    carrier.command({"cmd": "settled"})
    carrier.wait_for_frames("message.ack", 1)

    polls_before = len(carrier.daemon.frames_for("message.pending.list"))
    time.sleep(POLL_MS * 8 / 1000)
    answer = carrier.command({"cmd": "noop"})
    assert len(answer["sent"]) == 1
    assert len(carrier.daemon.frames_for("message.pending.list")) > polls_before


def test_outstanding_batch_blocks_second_injection(carrier: Carrier) -> None:
    carrier.daemon.pending.append(request_message("m-1", "first?"))
    carrier.command({"cmd": "start"})
    answer = _wait_injection(carrier)
    # A second message arrives while the first batch is mid-run.
    carrier.daemon.pending.append(request_message("m-2", "second?"))
    time.sleep(POLL_MS * 8 / 1000)
    answer = carrier.command({"cmd": "noop"})
    assert len(answer["sent"]) == 1

    # After the first batch completes, the second is injected next.
    carrier.command({"cmd": "consumed"})
    carrier.command({"cmd": "agent_end"})
    carrier.command({"cmd": "settled"})
    carrier.wait_for_frames("message.ack", 1)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        answer = carrier.command({"cmd": "noop"})
        if len(answer["sent"]) == 2:
            break
        time.sleep(0.05)
    assert "m-2" in answer["sent"][1]["text"]


def test_superseded_carrier_goes_quiet_and_never_reregisters(
    carrier: Carrier,
) -> None:
    carrier.command({"cmd": "start"})
    carrier.wait_for_frames("message.pending.list", 1)
    carrier.daemon.fail_next("message.pending.list", "SESSION_SUPERSEDED", times=100)

    time.sleep(POLL_MS * 10 / 1000)
    frames_before = len(carrier.daemon.frames)
    time.sleep(POLL_MS * 10 / 1000)
    assert len(carrier.daemon.frames) == frames_before
    assert len(carrier.daemon.frames_for("session.register")) == 1


def test_stale_session_triggers_reregister(carrier: Carrier) -> None:
    carrier.command({"cmd": "start"})
    carrier.wait_for_frames("message.pending.list", 1)

    carrier.daemon.fail_next("message.pending.list", "STALE_SESSION")
    carrier.wait_for_frames("session.register", 2)


def test_daemon_down_backs_off_and_recovers(carrier: Carrier) -> None:
    carrier.command({"cmd": "start"})
    carrier.wait_for_frames("message.pending.list", 1)

    carrier.daemon.close()
    time.sleep(0.5)  # several failed polls under backoff; no crash
    assert carrier.process.poll() is None

    carrier.daemon = FakeDaemon(carrier.state_dir)
    carrier.wait_for_frames("message.pending.list", 2, timeout=15.0)


def test_session_shutdown_unregisters_and_stops_polling(carrier: Carrier) -> None:
    carrier.command({"cmd": "start"})
    carrier.wait_for_frames("message.pending.list", 1)

    carrier.command({"cmd": "shutdown"})
    carrier.wait_for_frames("session.unregister", 1)
    (frame,) = carrier.daemon.frames_for("session.unregister")
    assert frame["params"]["actor"] == ACTOR
    assert frame["params"]["sessionRef"] == SESSION_REF

    frames_before = len(carrier.daemon.frames)
    time.sleep(POLL_MS * 10 / 1000)
    assert len(carrier.daemon.frames) == frames_before


def test_dropped_queue_reinjects_then_completes(carrier: Carrier) -> None:
    carrier.daemon.pending.append(request_message("m-1", "status?"))
    carrier.command({"cmd": "start"})
    answer = _wait_injection(carrier)
    # The run settles without our message ever entering it (user abort
    # cleared the follow-up queue): the carrier must re-inject, not reply.
    carrier.command({"cmd": "settled"})
    answer = carrier.command({"cmd": "noop"})
    assert len(answer["sent"]) == 2
    assert carrier.daemon.frames_for("message.reply") == []

    carrier.command({"cmd": "consumed"})
    carrier.command({"cmd": "agent_end"})
    carrier.command({"cmd": "settled"})
    (ack,) = carrier.wait_for_frames("message.ack", 1)
    assert ack["params"]["messageId"] == "m-1"


def test_unmanaged_session_starts_no_carrier_but_keeps_tools(
    tmp_path: Path,
) -> None:
    rig = Carrier(tmp_path, managed=False)
    try:
        answer = rig.command({"cmd": "start"})
        assert answer["ready"] is True
        time.sleep(POLL_MS * 10 / 1000)
        assert rig.daemon.frames == []
        assert answer["tools"] == [
            "harness_ack",
            "harness_progress",
            "harness_read",
            "harness_read_body",
            "harness_reply",
            "harness_send",
            "harness_targets",
            "harness_whoami",
        ]
    finally:
        rig.stop()


def _wait_injection(carrier: Carrier, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    answer: dict[str, Any] = {}
    while time.monotonic() < deadline:
        answer = carrier.command({"cmd": "noop"})
        if answer.get("sent"):
            return answer
        time.sleep(0.05)
    raise TimeoutError(f"no injection; daemon saw {carrier.daemon.methods()}")


def test_a_message_arriving_mid_batch_is_injected_right_after_the_batch(
    carrier: Carrier,
) -> None:
    # Review 849 on #1155: a message arriving while a batch is in flight
    # became "known" to the doorbell, so a 5 s hold sat it out (4.04 s
    # measured; the pre-migration loop took at most 1 s).  No hold while
    # busy, and completion injects the queue at once.
    carrier.daemon.hold_waits = True
    carrier.daemon.pending.append(request_message("m-1", "first?"))
    carrier.command({"cmd": "start"})
    _wait_injection(carrier)
    carrier.daemon.pending.append(request_message("m-2", "second?"))
    time.sleep(POLL_MS * 5 / 1000)  # let a poll observe m-2 mid-batch
    carrier.command({"cmd": "consumed"})
    carrier.command({"cmd": "agent_end"})
    finished = time.monotonic()
    carrier.command({"cmd": "settled"})

    deadline = finished + 10.0
    answer: dict[str, Any] = {}
    while time.monotonic() < deadline:
        answer = carrier.command({"cmd": "noop"})
        if len(answer["sent"]) >= 2:
            break
        time.sleep(0.02)
    assert len(answer["sent"]) == 2
    second = answer["sent"][1]["text"]
    assert "m-2" in second and "m-1" not in second
    assert time.monotonic() - finished < 1.5


def test_a_restart_rewake_reannounces_only_the_batch_in_flight(carrier: Carrier) -> None:
    # Review 849: the re-wake re-injected messages that were only queued, and
    # injectQueued then injected them again once the batch completed.
    carrier.daemon.pending.append(request_message("m-1", "first?"))
    carrier.command({"cmd": "start"})
    _wait_injection(carrier)
    carrier.daemon.pending.append(request_message("m-2", "second?"))
    time.sleep(POLL_MS * 5 / 1000)  # m-2 observed and queued, not injected
    carrier.daemon.epoch = "epoch-2"
    carrier.wait_for_frames("session.refresh", 1)

    deadline = time.monotonic() + 10.0
    answer: dict[str, Any] = {}
    while time.monotonic() < deadline:
        answer = carrier.command({"cmd": "noop"})
        if len(answer["sent"]) >= 2:
            break
        time.sleep(0.05)
    assert len(answer["sent"]) == 2
    reannounced = answer["sent"][1]["text"]
    assert "m-1" in reannounced
    assert "m-2" not in reannounced


def test_a_message_arriving_during_a_slow_batch_capture_is_not_lost(
    carrier: Carrier,
) -> None:
    # Batch capture and the observation lane must not strand an arrival that
    # lands while the consuming snapshot request is in flight.
    carrier.daemon.pending.append(request_message("m-1", "first?"))
    carrier.command({"cmd": "start"})
    _wait_injection(carrier)
    carrier.daemon.pending.append(request_message("m-2", "second?"))
    time.sleep(POLL_MS * 5 / 1000)  # m-2 queued mid-batch
    carrier.daemon.turn_delay["message.pending.batch"] = 0.4
    carrier.command({"cmd": "consumed"})
    carrier.command({"cmd": "agent_end"})
    # m-3 lands while the successor batch request is in flight.
    arrival = threading.Timer(
        0.15,
        lambda: carrier.daemon.pending.append(request_message("m-3", "third?")),
    )
    arrival.start()
    carrier.command({"cmd": "settled"})
    arrival.join()

    deadline = time.monotonic() + 10.0
    texts: list[str] = []
    while time.monotonic() < deadline:
        answer = carrier.command({"cmd": "noop"})
        texts = [item["text"] for item in answer["sent"]]
        if any("m-3" in text for text in texts):
            break
        if len(answer["sent"]) >= 2 and "m-2" in texts[-1]:
            # m-2's batch is out; finish it so m-3 can follow.
            carrier.command({"cmd": "consumed"})
            carrier.command({"cmd": "agent_end"})
            carrier.command({"cmd": "settled"})
        time.sleep(0.05)
    assert any("m-3" in text for text in texts), texts


def test_a_tool_call_that_learns_superseded_silences_the_carrier(
    carrier: Carrier,
) -> None:
    # Review 849/851: bridge tool calls feed the core; a tool that learns
    # SESSION_SUPERSEDED must stop both lanes (no further list or heartbeat).
    carrier.command({"cmd": "start"})
    carrier.wait_for_frames("message.pending.list", 1)
    carrier.daemon.fail_next("message.ack", "SESSION_SUPERSEDED")
    carrier.command({"cmd": "tool", "name": "harness_ack", "args": {"messageId": "m-x"}})
    time.sleep(POLL_MS * 3 / 1000)
    frames_before = len(carrier.daemon.frames)
    time.sleep(POLL_MS * 10 / 1000)
    assert len(carrier.daemon.frames) == frames_before
