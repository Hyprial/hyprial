from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

from hyprial.daemon.impl.adapters.lark.outbound.reactions import (
    ReactionEffectAdmission,
    ReactionEffectKind,
    ReactionEffectsRuntime,
)
def _eventually(predicate, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def _finish_abandoned_close(runtime: ReactionEffectsRuntime) -> None:
    """Release the sqlite store a zero-budget close left open on purpose.

    ``close(0.0)`` reaches production's "the actor would not stop inside the
    budget" path, which deliberately leaves the store open -- closing it under
    a live actor segfaults the worker (see
    test_lark_reaction_effects_close_under_live_actor.py).  A test that
    simulates a crash in-process must release the store itself once the
    in-flight effect is done, otherwise the suite's fd-hygiene fixture reports
    the deliberate leak as a regression (task 15633: 5 descriptors).  The
    retry is the same close() path production uses for a timed-out shutdown.
    """

    runtime.close()


def test_blocking_ack_does_not_block_reply_and_late_ack_is_cleared(
    tmp_path: Path,
) -> None:
    ack_entered = threading.Event()
    release_ack = threading.Event()
    present: set[str] = set()
    clears: list[str] = []

    def ack(message_id: str) -> None:
        ack_entered.set()
        assert release_ack.wait(2.0)
        present.add(message_id)

    def reply(message_id: str) -> None:
        clears.append(message_id)
        present.discard(message_id)

    runtime = ReactionEffectsRuntime(
        tmp_path / "reaction-effects.sqlite3",
        ack_effect=ack,
        reply_effect=reply,
        ack_capacity=1,
        reply_capacity=1,
    )
    try:
        assert runtime.ack("om-blocked") is ReactionEffectAdmission.ACCEPTED
        assert ack_entered.wait(1.0)
        assert runtime.reply("om-blocked") is ReactionEffectAdmission.ACCEPTED
        _eventually(lambda: len(clears) == 1)
        assert not release_ack.is_set(), "reply cleanup joined the blocked ACK lane"

        release_ack.set()
        _eventually(lambda: len(clears) == 2)
        assert runtime.wait_idle()
        assert present == set()
        row = runtime.snapshot().effects[0]
        assert row.desired_kind is ReactionEffectKind.REPLY
        assert row.status == "applied"
        assert row.generation == 3
    finally:
        release_ack.set()
        runtime.close()


def test_ack_arriving_after_reply_is_suppressed(tmp_path: Path) -> None:
    acks: list[str] = []
    replies: list[str] = []
    runtime = ReactionEffectsRuntime(
        tmp_path / "reaction-effects.sqlite3",
        ack_effect=acks.append,
        reply_effect=replies.append,
    )
    try:
        assert runtime.reply("om-late") is ReactionEffectAdmission.ACCEPTED
        assert runtime.wait_idle()
        assert runtime.ack("om-late") is ReactionEffectAdmission.ACCEPTED
        _eventually(
            lambda: (
                runtime.snapshot().effects[0].desired_kind is ReactionEffectKind.REPLY
            )
        )
        time.sleep(0.05)
        assert acks == []
        assert replies == ["om-late"]
    finally:
        runtime.close()


def test_reply_intent_is_held_until_receipt_then_activated(tmp_path: Path) -> None:
    replies: list[str] = []
    runtime = ReactionEffectsRuntime(
        tmp_path / "reaction-effects.sqlite3",
        ack_effect=lambda _message_id: None,
        reply_effect=replies.append,
    )
    try:
        assert runtime.persist_reply("om-held") is ReactionEffectAdmission.ACCEPTED
        row = runtime.snapshot().effects[0]
        assert row.status == "held"
        assert replies == []

        assert runtime.reply("om-held") is ReactionEffectAdmission.ACCEPTED
        assert runtime.wait_idle()
        assert replies == ["om-held"]
        assert runtime.snapshot().effects[0].generation == row.generation
    finally:
        runtime.close()


def test_held_reply_intent_replays_if_worker_crashes_after_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reaction-effects.sqlite3"
    crashed = ReactionEffectsRuntime(
        path,
        ack_effect=lambda _message_id: None,
        reply_effect=lambda _message_id: None,
    )
    assert crashed.persist_reply("om-held-crash") is ReactionEffectAdmission.ACCEPTED
    before = crashed.snapshot().effects[0]
    assert before.status == "held"
    crashed.close(0.0)

    replies: list[str] = []
    recovered = ReactionEffectsRuntime(
        path,
        ack_effect=lambda _message_id: None,
        reply_effect=replies.append,
    )
    try:
        assert recovered.wait_idle()
        after = recovered.snapshot().effects[0]
        assert replies == ["om-held-crash"]
        assert after.status == "applied"
        assert after.generation == before.generation + 1
        assert after.token != before.token
    finally:
        recovered.close()
        _finish_abandoned_close(crashed)


def test_persist_reply_conflict_stays_held_until_explicit_activation(
    tmp_path: Path,
) -> None:
    replies: list[str] = []

    def failed_ack(_message_id: str) -> None:
        raise RuntimeError("synthetic ACK failure")

    runtime = ReactionEffectsRuntime(
        tmp_path / "reaction-effects.sqlite3",
        ack_effect=failed_ack,
        reply_effect=replies.append,
    )
    try:
        assert runtime.ack("om-conflict") is ReactionEffectAdmission.ACCEPTED
        assert runtime.wait_idle()
        assert runtime.snapshot().effects[0].status == "failed"

        assert runtime.persist_reply("om-conflict") is ReactionEffectAdmission.ACCEPTED
        held = runtime.snapshot().effects[0]
        assert held.desired_kind is ReactionEffectKind.REPLY
        assert held.status == "held"
        assert replies == []

        assert runtime.reply("om-conflict") is ReactionEffectAdmission.ACCEPTED
        assert runtime.wait_idle()
        assert replies == ["om-conflict"]
    finally:
        runtime.close()


def test_late_ack_compensation_cannot_activate_held_reply(tmp_path: Path) -> None:
    ack_entered = threading.Event()
    release_ack = threading.Event()
    replies: list[str] = []

    def blocked_ack(_message_id: str) -> None:
        ack_entered.set()
        assert release_ack.wait(2.0)

    runtime = ReactionEffectsRuntime(
        tmp_path / "reaction-effects.sqlite3",
        ack_effect=blocked_ack,
        reply_effect=replies.append,
    )
    try:
        assert runtime.ack("om-held-fence") is ReactionEffectAdmission.ACCEPTED
        assert ack_entered.wait(1.0)
        assert (
            runtime.persist_reply("om-held-fence")
            is ReactionEffectAdmission.ACCEPTED
        )
        held = runtime.snapshot().effects[0]
        assert held.status == "held"

        release_ack.set()
        _eventually(lambda: runtime.snapshot().effects[0].generation > held.generation)
        assert runtime.snapshot().effects[0].status == "held"
        assert replies == []

        assert runtime.reply("om-held-fence") is ReactionEffectAdmission.ACCEPTED
        assert runtime.wait_idle()
        assert replies == ["om-held-fence"]
    finally:
        release_ack.set()
        runtime.close()


def test_running_effect_replays_after_worker_crash(tmp_path: Path) -> None:
    path = tmp_path / "reaction-effects.sqlite3"
    entered = threading.Event()
    release = threading.Event()

    def crashed_ack(_message_id: str) -> None:
        entered.set()
        assert release.wait(3.0)

    crashed = ReactionEffectsRuntime(
        path,
        ack_effect=crashed_ack,
        reply_effect=lambda _message_id: None,
    )
    assert crashed.ack("om-replay") is ReactionEffectAdmission.ACCEPTED
    assert entered.wait(1.0)
    before = crashed.snapshot().effects[0]
    assert before.status == "running"
    crashed.close(0.0)

    replayed: list[str] = []
    recovered = ReactionEffectsRuntime(
        path,
        ack_effect=replayed.append,
        reply_effect=lambda _message_id: None,
    )
    try:
        assert recovered.wait_idle()
        after = recovered.snapshot().effects[0]
        assert replayed == ["om-replay"]
        assert after.status == "applied"
        assert after.generation == before.generation + 1
        assert after.token != before.token
    finally:
        release.set()
        recovered.close()
        _finish_abandoned_close(crashed)


def test_close_retries_store_cleanup_after_actor_stop_times_out(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocked_ack(_message_id: str) -> None:
        entered.set()
        assert release.wait(3.0)

    runtime = ReactionEffectsRuntime(
        tmp_path / "reaction-effects.sqlite3",
        ack_effect=blocked_ack,
        reply_effect=lambda _message_id: None,
    )
    try:
        assert runtime.ack("om-close-retry") is ReactionEffectAdmission.ACCEPTED
        assert entered.wait(1.0)
        runtime.close(0.0)

        release.set()
        runtime.close()

        try:
            runtime._store.snapshot()
        except sqlite3.ProgrammingError:
            pass
        else:
            raise AssertionError("completed close left the sqlite store open")
    finally:
        release.set()
        runtime.close()


def test_double_restart_replays_only_current_generation(tmp_path: Path) -> None:
    path = tmp_path / "reaction-effects.sqlite3"
    releases = [threading.Event(), threading.Event()]
    entered = [threading.Event(), threading.Event()]

    def blocker(index: int):  # type: ignore[no-untyped-def]
        def effect(_message_id: str) -> None:
            entered[index].set()
            assert releases[index].wait(3.0)

        return effect

    first = ReactionEffectsRuntime(
        path, ack_effect=blocker(0), reply_effect=lambda _message_id: None
    )
    assert first.ack("om-double") is ReactionEffectAdmission.ACCEPTED
    assert entered[0].wait(1.0)
    generation_one = first.snapshot().effects[0].generation
    first.close(0.0)

    second = ReactionEffectsRuntime(
        path, ack_effect=blocker(1), reply_effect=lambda _message_id: None
    )
    assert entered[1].wait(1.0)
    generation_two = second.snapshot().effects[0].generation
    second.close(0.0)

    calls: list[str] = []
    third = ReactionEffectsRuntime(
        path, ack_effect=calls.append, reply_effect=lambda _message_id: None
    )
    try:
        assert third.wait_idle()
        generation_three = third.snapshot().effects[0].generation
        assert calls == ["om-double"]
        assert (generation_one, generation_two, generation_three) == (1, 2, 3)
    finally:
        for release in releases:
            release.set()
        third.close()
        _finish_abandoned_close(first)
        _finish_abandoned_close(second)


def test_lane_overload_is_persisted_observable_and_drains(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    reports: list[dict[str, object]] = []

    def ack(message_id: str) -> None:
        calls.append(message_id)
        if message_id == "om-one":
            entered.set()
            assert release.wait(2.0)

    runtime = ReactionEffectsRuntime(
        tmp_path / "reaction-effects.sqlite3",
        ack_effect=ack,
        reply_effect=lambda _message_id: None,
        ack_capacity=1,
        overflow_report=reports.append,
    )
    try:
        assert runtime.ack("om-one") is ReactionEffectAdmission.ACCEPTED
        assert entered.wait(1.0)
        assert runtime.ack("om-two") is ReactionEffectAdmission.ACCEPTED
        _eventually(lambda: runtime.snapshot().overflow_count >= 1)
        # The runtime bumps the counter before it calls overflow_report, so
        # the report is observable only after its own callback has run; wait
        # on the report itself rather than asserting right after the count.
        _eventually(lambda: bool(reports))
        assert reports == [
            {
                "event": "lark.reaction-effect.overflow",
                "kind": "ack",
                "reason": "lane-capacity",
            }
        ]
        assert "om-one" not in repr(reports) and "om-two" not in repr(reports)
        release.set()
        assert runtime.wait_idle()
        assert calls == ["om-one", "om-two"]
    finally:
        release.set()
        runtime.close()


def test_capacity_and_ttl_gc_bound_effect_and_overflow_rows(tmp_path: Path) -> None:
    now = [1_000.0]
    path = tmp_path / "reaction-effects.sqlite3"
    runtime = ReactionEffectsRuntime(
        path,
        ack_effect=lambda _message_id: None,
        reply_effect=lambda _message_id: None,
        effect_capacity=2,
        overflow_capacity=2,
        effect_ttl=10.0,
        overflow_ttl=5.0,
        clock=lambda: now[0],
    )
    try:
        assert runtime.ack("om-a") is ReactionEffectAdmission.ACCEPTED
        assert runtime.ack("om-b") is ReactionEffectAdmission.ACCEPTED
        assert runtime.wait_idle()
        assert runtime.ack("om-c") is ReactionEffectAdmission.ACCEPTED
        assert runtime.wait_idle()
        assert {row.native_message_id for row in runtime.snapshot().effects} == {
            "om-b",
            "om-c",
        }

        now[0] += 11.0
        assert runtime.collect_garbage() is ReactionEffectAdmission.ACCEPTED
        _eventually(lambda: runtime.snapshot().effects == ())
    finally:
        runtime.close()


def test_live_capacity_overflow_is_bounded_and_ttl_collected(tmp_path: Path) -> None:
    now = [2_000.0]
    entered = threading.Event()
    release = threading.Event()
    reports: list[dict[str, object]] = []

    def blocked(_message_id: str) -> None:
        entered.set()
        assert release.wait(2.0)

    runtime = ReactionEffectsRuntime(
        tmp_path / "reaction-effects.sqlite3",
        ack_effect=blocked,
        reply_effect=lambda _message_id: None,
        effect_capacity=1,
        overflow_capacity=1,
        overflow_ttl=5.0,
        clock=lambda: now[0],
        overflow_report=reports.append,
    )
    try:
        assert runtime.ack("om-live") is ReactionEffectAdmission.ACCEPTED
        assert entered.wait(1.0)
        assert runtime.ack("om-dropped") is ReactionEffectAdmission.OVERLOADED
        _eventually(lambda: runtime.snapshot().overflow_count == 1)
        snapshot = runtime.snapshot()
        assert [row.native_message_id for row in snapshot.effects] == ["om-live"]
        assert reports[-1]["reason"] == "ledger-capacity"

        now[0] += 6.0
        assert runtime.collect_garbage() is ReactionEffectAdmission.ACCEPTED
        _eventually(lambda: runtime.snapshot().overflow_count == 0)
    finally:
        release.set()
        runtime.close()


def test_schema_and_failures_never_persist_body_or_credentials(tmp_path: Path) -> None:
    path = tmp_path / "reaction-effects.sqlite3"

    def fail(_message_id: str) -> None:
        raise RuntimeError("Bearer TOP_SECRET message body: private words")

    runtime = ReactionEffectsRuntime(
        path,
        ack_effect=fail,
        reply_effect=lambda _message_id: None,
    )
    try:
        assert runtime.ack("om-safe-id") is ReactionEffectAdmission.ACCEPTED
        assert runtime.wait_idle()
        row = runtime.snapshot().effects[0]
        assert row.error_code == "RuntimeError"

        connection = sqlite3.connect(path)
        try:
            columns = {
                item[1]
                for table in ("reaction_effects", "reaction_effect_overflow")
                for item in connection.execute(f"PRAGMA table_info({table})")
            }
            assert not columns & {
                "text",
                "body",
                "content",
                "credential",
                "secret",
                "access_token",
            }
            values = tuple(
                connection.execute(
                    "SELECT token, error_code FROM reaction_effects"
                ).fetchone()
            )
            assert "TOP_SECRET" not in repr(values)
            assert "private words" not in repr(values)
        finally:
            connection.close()
        assert os.stat(path).st_mode & 0o777 == 0o600
    finally:
        runtime.close()
