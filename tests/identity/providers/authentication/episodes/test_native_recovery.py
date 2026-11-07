"""provider_auth_actor: native recovery scenarios."""

import threading
import time
from types import SimpleNamespace
from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.identity.impl.provider_auth.actor._authority import ProviderAuthAuthority
from hyprial.identity.impl.provider_auth.coordinator._base import (
    AuthNoticeIntent,
)
from hyprial.identity.impl.provider_auth.helper import HelperOutcome, DeviceCodeAnnouncement

from .support import (
    _authority,
    _fail,
    _status_with_retry_heap_count,
    _wait,
)


def test_failed_first_native_tail_detaches_before_later_rounds(tmp_path):
    old_code_entered = threading.Event()
    release_old_code = threading.Event()
    failure_delivered = threading.Event()
    closing_delivered = threading.Event()
    calls = 0
    keys = []

    def runner(_provider, announce, *, stop):
        nonlocal calls
        calls += 1
        announce(
            DeviceCodeAnnouncement(
                f"ROUND-{calls}",
                "https://auth.example.test/device",
                1,
                60,
            )
        )
        if calls == 1:
            # This scenario needs a native tail already in flight when the
            # helper fails. Announce admission alone does not establish that.
            assert old_code_entered.wait(1)
            return HelperOutcome.FAILED
        return HelperOutcome.TIMED_OUT

    def notify(_text, *, idempotency_key):
        keys.append(idempotency_key)
        if ":code:" in idempotency_key and ":r1" in idempotency_key:
            old_code_entered.set()
            assert release_old_code.wait(3)
            return SimpleNamespace(delivered=False)
        if ":code:" in idempotency_key:
            return SimpleNamespace(delivered=False)
        if ":failed:" in idempotency_key:
            failure_delivered.set()
        if ":closing:" in idempotency_key:
            closing_delivered.set()
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        runner,
        notify,
        helper_capacity=1,
        notification_capacity=2,
        observation_capacity=1,
        notification_retry_seconds=0.01,
    )
    try:
        assert _fail(actor, "round-1") is AdmissionResult.ACCEPTED
        assert old_code_entered.wait(1)
        assert failure_delivered.wait(1)
        _wait(lambda: actor.status()["businessCredits"] == 0)
        old_code_key = next(
            key for key in actor._notices if ":code:" in key and ":r1" in key
        )
        assert old_code_key not in actor._notice_credits
        assert actor.status()["notificationAttempts"] == 1

        assert _fail(actor, "round-2") is AdmissionResult.ACCEPTED
        assert closing_delivered.wait(1)
        assert calls == 3
        assert actor.status()["failed"] == 0
        _wait(lambda: actor.status()["businessCredits"] == 0)
        assert len(actor._episode_credits) == 0
        assert len(actor._notice_credits) == 0
        assert any(":failed:" in key for key in keys)
        assert any(":closing:" in key for key in keys)
    finally:
        release_old_code.set()
        assert actor.close(2)


def test_failed_helper_retires_code_before_native_entry(tmp_path, monkeypatch):
    queued, release = threading.Event(), threading.Event()
    failure_delivered = threading.Event()
    native_codes = []
    original = ProviderAuthAuthority._notify

    def delayed(owner, command):
        if command.intent.kind == "code":
            queued.set()
            assert release.wait(3)
        return original(owner, command)

    monkeypatch.setattr(ProviderAuthAuthority, "_notify", delayed)

    def runner(_provider, announce, *, stop):
        announce(DeviceCodeAnnouncement("EXPIRED", "https://auth.example.test/device", 1, 60))
        assert queued.wait(1)
        return HelperOutcome.FAILED

    def notify(_text, *, idempotency_key):
        if ":code:" in idempotency_key:
            native_codes.append(idempotency_key)
        if ":failed:" in idempotency_key:
            failure_delivered.set()
        return SimpleNamespace(delivered=True)

    actor = _authority(tmp_path, runner, notify, helper_capacity=1,
                       notification_capacity=2, observation_capacity=1)
    try:
        assert _fail(actor) is AdmissionResult.ACCEPTED
        assert queued.wait(1)
        assert failure_delivered.wait(1)
        release.set()
        _wait(lambda: actor.status()["notificationsPending"] == 0)
        assert native_codes == []
        assert actor.status()["businessCredits"] == 0
    finally:
        release.set()
        assert actor.close(2)


def test_multi_stale_notice_retirement_only_mutates_on_episode_owner(
    tmp_path, monkeypatch
):
    notify_entered = threading.Event()
    release_notify = threading.Event()
    release_runner = threading.Event()
    completion_threads = []

    def runner(_provider, announce, *, stop):
        announce(
            DeviceCodeAnnouncement(
                "CURRENT", "https://auth.example.test/device", 1, 60
            )
        )
        assert release_runner.wait(3)
        return HelperOutcome.OK

    def notify(_text, *, idempotency_key):
        if ":code:" in idempotency_key:
            notify_entered.set()
            assert release_notify.wait(3)
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        runner,
        notify,
        notification_capacity=1,
        notification_retry_seconds=0.2,
    )
    complete_notice = actor._core._complete_notice

    def tracked_complete_notice(intent, *, delivered):
        completion_threads.append(threading.current_thread().name)
        return complete_notice(intent, delivered=delivered)

    monkeypatch.setattr(actor._core, "_complete_notice", tracked_complete_notice)
    stale = []
    try:
        assert _fail(actor) is AdmissionResult.ACCEPTED
        assert notify_entered.wait(1)
        episode = actor._core._episodes["kimi-coding"]
        episode_ref = (episode.key, episode.episode_id)
        for suffix in ("one", "two"):
            intent = AuthNoticeIntent(
                episode.key,
                f"stale-{suffix}",
                episode.round,
                "code",
                f"stale {suffix}",
                f"provider-auth:code:stale-{suffix}:r1",
            )
            stale.append(intent)
        with actor._core._lock:
            actor._core._pending_notices.update(
                intent.idempotency_key for intent in stale
            )
        with actor._guard:
            for intent in stale:
                actor._notices[intent.idempotency_key] = intent
                actor._notice_credits[intent.idempotency_key] = episode_ref

        release_notify.set()
        _wait(lambda: actor.status()["notificationsSent"] == 1)
        for intent in stale:
            actor._schedule_retry("notice", intent.idempotency_key)
        _wait(
            lambda: all(
                intent.idempotency_key not in actor._notices
                for intent in stale
            )
        )
        assert len(completion_threads) >= 3
        assert set(completion_threads) == {"provider-auth-state-0"}
        status = actor.status()
        assert status["notificationsPending"] == 0
        assert len(actor._notice_credits) == 0
        assert status["custody"] <= status["custodyCapacity"]
    finally:
        release_notify.set()
        release_runner.set()
        assert actor.close(2)


def test_h2_n4_blocked_native_rounds_retain_all_terminal_successors(tmp_path):
    # Native entry and owner progress are fixture prerequisites, not a
    # provider latency SLA. Close budgets and custody bounds below are tested.
    progress_timeout = 10.0

    def progress_remaining():
        return max(0.0, progress_deadline - time.monotonic())

    release = threading.Event()
    four_native_attempts = threading.Event()
    both_third_rounds_started = threading.Event()
    native_codes = []
    rounds = {"kimi-coding": 0, "openai-codex": 0}
    native_started = {
        (provider, round_no): threading.Event()
        for provider in rounds for round_no in (1, 2)
    }

    def runner(provider, announce, *, stop):
        rounds[provider] += 1
        round_no = rounds[provider]
        if round_no > 2:
            # The four held native slots belong to rounds 1-2 of both
            # providers. Under load one provider can reach round 3 first;
            # its code would take the last slot and starve the other's
            # round-2 code until the progress deadline.
            assert all(
                started.wait(progress_remaining())
                for started in native_started.values()
            )
        announce(DeviceCodeAnnouncement(
            f"{provider}-{round_no}",
            "https://auth.example.test/device",
            1,
            60,
        ))
        # This control requires four already-entered native calls. A queued
        # code that becomes stale before entry is correctly suppressed.
        if round_no <= 2:
            assert native_started[provider, round_no].wait(progress_remaining())
        elif rounds == {"kimi-coding": 3, "openai-codex": 3}:
            both_third_rounds_started.set()
        return HelperOutcome.TIMED_OUT

    def notify(_text, *, idempotency_key):
        if ":code:" in idempotency_key:
            for (provider, round_no), started in native_started.items():
                if f"code: {provider}-{round_no}" in _text:
                    started.set()
            native_codes.append(idempotency_key)
            if len(native_codes) == 4:
                four_native_attempts.set()
            # This is a fixture deadman, not a native-notification deadline.
            # Keep all four attempts held until the owner assertions release
            # them, including when a helper completion is scheduled late.
            assert release.wait(30)
            return SimpleNamespace(delivered=False)
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        runner,
        notify,
        helper_capacity=2,
        notification_capacity=4,
        observation_capacity=2,
        notification_retry_seconds=0.01,
    )
    progress_deadline = time.monotonic() + progress_timeout
    try:
        assert _fail(actor, "kimi-worker") is AdmissionResult.ACCEPTED
        assert actor.handle_turn_failure(
            "OAuth refresh failed for openai-codex: invalid_grant",
            harness="pi",
            provider="openai-codex",
            model="gpt",
            worker="openai-worker",
        ) is AdmissionResult.ACCEPTED
        assert four_native_attempts.wait(progress_remaining())
        # Every helper returns TIMED_OUT after its native-entry prerequisite;
        # the owner must eventually start both third rounds. This bounds test
        # liveness, not the measured close budget or a product latency SLA.
        assert both_third_rounds_started.wait(progress_remaining())
        assert rounds == {"kimi-coding": 3, "openai-codex": 3}
        _wait(
            lambda: all(
                episode.closing_pending
                for episode in actor._core._episodes.values()
            ),
            timeout=progress_remaining(),
        )
        # Policy closing_pending is visible before both notices enter custody.
        def closing_notices_admitted():
            with actor._guard:
                return sum(":closing:" in key for key in actor._notices) == 2

        _wait(closing_notices_admitted, timeout=progress_remaining())
        keys = set(actor._notices)
        assert sum(":closing:" in key for key in keys) == 2
        status, retry_heap_count = _status_with_retry_heap_count(actor)
        assert status["failed"] == 0
        assert status["businessCredits"] == status["businessCapacity"] == 2
        assert status["notificationsPending"] <= status["noticeCustodyCapacity"]
        assert status["custody"] <= status["custodyCapacity"]
        assert len(actor._episode_credits) <= status["businessCapacity"]
        assert set(actor._notice_credits.values()) <= set(actor._episode_credits)
        assert retry_heap_count == status["retryScheduled"]
        assert not actor.close(0.02)
        progress_deadline = time.monotonic() + progress_timeout
        release.set()
        _wait(lambda: actor.status()["notificationsPending"] == 0, timeout=progress_remaining())
        assert all(
            episode.closing_sent for episode in actor._core._episodes.values()
        )
        assert actor.status()["businessCredits"] == 0
    finally:
        release.set()
        assert actor.close(2)


def test_queued_codes_that_expire_before_native_entry_are_not_sent(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    delivered_codes = []
    original = ProviderAuthAuthority._notify

    def delayed(owner, command):
        if command.intent.kind == "code":
            entered.set()
            assert release.wait(3)
        return original(owner, command)

    monkeypatch.setattr(ProviderAuthAuthority, "_notify", delayed)

    def runner(provider, announce, *, stop):
        announce(DeviceCodeAnnouncement("STALE", "https://auth.example.test/device", 1, 60))
        return HelperOutcome.TIMED_OUT

    def notify(_text, *, idempotency_key):
        if ":code:" in idempotency_key:
            delivered_codes.append(idempotency_key)
        return SimpleNamespace(delivered=True)

    actor = _authority(tmp_path, runner, notify, helper_capacity=1,
                       notification_capacity=4, observation_capacity=1)
    try:
        assert _fail(actor) is AdmissionResult.ACCEPTED
        assert entered.wait(1)
        _wait(lambda: all(episode.round == 3 and not episode.inflight
                          for episode in actor._core._episodes.values()))
        release.set()
        _wait(lambda: actor.status()["notificationsPending"] == 0)
        assert delivered_codes == []
        assert all(episode.closing_sent for episode in actor._core._episodes.values())
    finally:
        release.set()
        assert actor.close(2)


def test_queued_code_after_helper_success_is_suppressed_while_recovery_settles(
    tmp_path, monkeypatch,
):
    queued, release = threading.Event(), threading.Event()
    recovered = threading.Event()
    native_codes = []
    original = ProviderAuthAuthority._notify

    def delayed(owner, command):
        if command.intent.kind == "code":
            queued.set()
            assert release.wait(3)
        return original(owner, command)

    monkeypatch.setattr(ProviderAuthAuthority, "_notify", delayed)

    def runner(_provider, announce, *, stop):
        announce(DeviceCodeAnnouncement("STALE", "https://auth.example.test/device", 1, 60))
        assert queued.wait(1)
        return HelperOutcome.OK

    def notify(_text, *, idempotency_key):
        if ":code:" in idempotency_key:
            native_codes.append(idempotency_key)
        if ":recovered:" in idempotency_key:
            recovered.set()
        return SimpleNamespace(delivered=True)

    actor = _authority(tmp_path, runner, notify, helper_capacity=1,
                       notification_capacity=2, observation_capacity=1)
    try:
        assert _fail(actor) is AdmissionResult.ACCEPTED
        assert queued.wait(1)
        assert recovered.wait(1)
        release.set()
        _wait(lambda: actor.status()["notificationsPending"] == 0)
        assert native_codes == []
        assert "kimi-coding" not in actor._core._episodes
        assert actor.status()["businessCredits"] == 0
    finally:
        release.set()
        assert actor.close(2)
