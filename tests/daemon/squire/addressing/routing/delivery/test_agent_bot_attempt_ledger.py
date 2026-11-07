"""Persistence and retention bounds for sender-side bot attempts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hyprial.daemon.impl.application.messaging.inbox_surface.agent_bot_attempts import (
    ATTEMPT_ACCEPTED,
    ATTEMPT_DEFINITELY_NOT_SENT,
    ATTEMPT_IN_FLIGHT,
    AgentBotAttemptLedger,
)


def test_inflight_and_accepted_attempts_survive_ledger_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "agent-bot-attempts.json"
    first = AgentBotAttemptLedger(path)
    attempt, fresh = first.begin(
        "operation:user:bob",
        adapter="worker-bot",
        lark_idempotency_key="lark-key",
    )

    assert fresh is True
    assert attempt.state == ATTEMPT_IN_FLIGHT
    reopened = AgentBotAttemptLedger(path)
    assert reopened.get("operation:user:bob") == attempt

    delivery = {
        "target": "user:bob",
        "messageId": "message-1",
        "accepted": True,
        "nativeMessageId": "om-1",
    }
    accepted = reopened.settle(
        "operation:user:bob",
        adapter="worker-bot",
        lark_idempotency_key="lark-key",
        state=ATTEMPT_ACCEPTED,
        delivery=delivery,
    )

    assert AgentBotAttemptLedger(path).get("operation:user:bob") == accepted


def test_definitely_not_sent_record_does_not_lock_a_new_attempt(
    tmp_path: Path,
) -> None:
    ledger = AgentBotAttemptLedger(tmp_path / "agent-bot-attempts.json")
    ledger.begin(
        "operation:user:bob",
        adapter="old-bot",
        lark_idempotency_key="old-key",
    )
    settled = ledger.settle(
        "operation:user:bob",
        adapter="old-bot",
        lark_idempotency_key="old-key",
        state=ATTEMPT_DEFINITELY_NOT_SENT,
    )

    replacement, fresh = ledger.begin(
        "operation:user:bob",
        adapter="new-bot",
        lark_idempotency_key="new-key",
    )

    assert settled.locks_fallback is False
    assert fresh is True
    assert replacement.state == ATTEMPT_IN_FLIGHT
    assert replacement.adapter == "new-bot"


def test_attempt_ledger_is_bounded_by_age_and_count(tmp_path: Path) -> None:
    now = [100]
    ledger = AgentBotAttemptLedger(
        tmp_path / "agent-bot-attempts.json",
        max_entries=2,
        retention_ms=10,
        clock_ms=lambda: now[0],
    )
    # Count pressure evicts only definitely-not-sent rows (review 1148 N3).
    for key in ("a", "b", "c"):
        ledger.settle(
            key,
            adapter="bot",
            lark_idempotency_key=f"lark-{key}",
            state=ATTEMPT_DEFINITELY_NOT_SENT,
            reason="adapter-not-running",
        )
        now[0] += 1

    assert ledger.get("a") is None
    assert ledger.get("b") is not None
    assert ledger.get("c") is not None

    now[0] = 113
    assert ledger.get("b") is None
    assert ledger.get("c") is None


def test_save_fsyncs_replaced_files_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: dict[int, Path] = {}
    synced: list[Path] = []
    real_open = os.open
    real_fsync = os.fsync

    def recording_open(
        path: os.PathLike[str] | str, *args: object, **kwargs: object
    ) -> int:
        descriptor = real_open(path, *args, **kwargs)
        opened[descriptor] = Path(path)
        return descriptor

    def recording_fsync(descriptor: int) -> None:
        path = opened.get(descriptor)
        if path is not None:
            synced.append(path)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "fsync", recording_fsync)
    path = tmp_path / "state" / "agent-bot-attempts.json"
    ledger = AgentBotAttemptLedger(path)

    ledger.begin("operation:user:alice", adapter="bot", lark_idempotency_key="key")

    assert path.parent in synced


def test_a_held_key_lock_never_blocks_a_different_key(tmp_path: Path) -> None:
    """The key lock is held across one bounded native send (up to the gateway
    call timeout); another key, even one that a striped lock would have mapped
    to the same stripe, must not wait for it."""
    import threading

    ledger = AgentBotAttemptLedger(tmp_path / "agent-bot-attempts.json")
    first = "operation-a:user:bob"
    # Same stripe under the former hash(key) % 64 striping.
    second = next(
        f"operation-{index}:user:bob"
        for index in range(100_000)
        if f"operation-{index}:user:bob" != first
        and hash(f"operation-{index}:user:bob") % 64 == hash(first) % 64
    )
    holding = threading.Event()
    release = threading.Event()

    def hold_first() -> None:
        with ledger.key_lock(first):
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold_first)
    holder.start()
    try:
        assert holding.wait(5)
        entered = threading.Event()

        def take_second() -> None:
            with ledger.key_lock(second):
                entered.set()

        taker = threading.Thread(target=take_second)
        taker.start()
        assert entered.wait(1), "a different key waited on a held key lock"
        taker.join(5)
    finally:
        release.set()
        holder.join(5)


def test_the_same_key_still_serializes_and_idle_locks_are_dropped(
    tmp_path: Path,
) -> None:
    import threading

    ledger = AgentBotAttemptLedger(tmp_path / "agent-bot-attempts.json")
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with ledger.key_lock("operation-a:user:bob"):
            holding.set()
            release.wait(5)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(5)
    entered = threading.Event()

    def take() -> None:
        with ledger.key_lock("operation-a:user:bob"):
            entered.set()

    taker = threading.Thread(target=take)
    taker.start()
    assert not entered.wait(0.3)
    release.set()
    holder.join(5)
    assert entered.wait(5)
    taker.join(5)
    assert ledger._key_locks == {}


def test_count_eviction_keeps_accepted_rows(tmp_path: Path) -> None:
    """An accepted row turns a replay into a duplicate; evicting it would let
    the replay send a second DM."""

    now = [1_000]
    ledger = AgentBotAttemptLedger(
        tmp_path / "agent-bot-attempts.json", max_entries=2, clock_ms=lambda: now[0]
    )
    ledger.settle(
        "accepted-old",
        adapter="bot",
        lark_idempotency_key="k1",
        state=ATTEMPT_ACCEPTED,
        delivery={"nativeMessageId": "om-1"},
    )
    now[0] += 1
    ledger.settle(
        "not-sent",
        adapter="bot",
        lark_idempotency_key="k2",
        state=ATTEMPT_DEFINITELY_NOT_SENT,
        reason="adapter-not-running",
    )
    now[0] += 1
    ledger.settle(
        "accepted-new",
        adapter="bot",
        lark_idempotency_key="k3",
        state=ATTEMPT_ACCEPTED,
        delivery={"nativeMessageId": "om-3"},
    )

    assert ledger.get("accepted-old") is not None
    assert ledger.get("accepted-new") is not None
    assert ledger.get("not-sent") is None
