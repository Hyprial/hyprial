"""provider_auth_actor: notification scenarios."""

import threading
import time
from types import SimpleNamespace
from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.identity.impl.provider_auth.helper import HelperOutcome, DeviceCodeAnnouncement

from .support import (
    ACCOUNT_FAILURE,
    _authority,
    _fail,
    _wait,
)


def test_credential_probe_does_not_block_turn_callback(tmp_path):
    entered, release = threading.Event(), threading.Event()
    actor = _authority(tmp_path, lambda *args, **kwargs: HelperOutcome.STOPPED)

    def probe(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return False

    actor._core._credential_live = probe
    try:
        _fail(actor)
        assert entered.wait(1)
        assert actor.status()["rejected"] == 0
        assert not actor.close(0.01)
    finally:
        release.set()
        assert actor.close(2)


def test_helper_wait_does_not_block_episode_updates_and_completions_are_fenced(
    tmp_path,
):
    entered, release = threading.Event(), threading.Event()
    code_delivered = threading.Event()
    sent = []
    calls = []

    def runner(provider, announce, *, stop):
        calls.append(provider)
        entered.set()
        assert release.wait(3)
        announce(
            DeviceCodeAnnouncement(
                "SYNTHETIC", "https://auth.example.test/device", 5, 60
            )
        )
        assert code_delivered.wait(1)
        return HelperOutcome.OK

    def notify(text, *, idempotency_key):
        sent.append((text, idempotency_key))
        if ":code:" in idempotency_key:
            code_delivered.set()
        return SimpleNamespace(delivered=True)

    actor = _authority(tmp_path, runner, notify)
    try:
        _fail(actor, "worker-1")
        assert entered.wait(1)
        _fail(actor, "worker-2")
        _wait(lambda: actor.status()["completed"] >= 2)
        assert calls == ["kimi-coding"]
        release.set()
        _wait(lambda: actor.status()["helpers"] == 0)
    finally:
        release.set()
        assert actor.close(2)
    assert len(sent) == 2
    assert any("worker-2" in text and "SYNTHETIC" in text for text, key in sent)
    assert any("恢复" in text for text, key in sent)


def test_false_then_success_retries_same_key_and_marks_failure_only_delivered(
    tmp_path,
):
    calls = []
    first = threading.Event()
    release_retry = threading.Event()

    def notify(text, *, idempotency_key):
        calls.append((text, idempotency_key))
        if len(calls) == 1:
            first.set()
            return SimpleNamespace(delivered=False)
        assert release_retry.wait(3)
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        lambda *args, **kwargs: HelperOutcome.FAILED,
        notify,
        notification_retry_seconds=0.1,
    )
    try:
        _fail(actor)
        assert first.wait(1)
        episode = actor._core._episodes["kimi-coding"]
        assert episode.failure_alert_pending
        assert not episode.failure_alerted
        assert actor.status()["notificationsPending"] == 1
        assert actor.status()["notificationsSent"] == 0
        release_retry.set()
        _wait(lambda: actor.status()["notificationsSent"] == 1)
        assert len(calls) == 2
        assert calls[0][1] == calls[1][1]
        assert not episode.failure_alert_pending
        assert episode.failure_alerted
        assert actor.status()["notificationsPending"] == 0
    finally:
        release_retry.set()
        assert actor.close(2)


def test_delivered_none_is_pending_until_same_key_success(tmp_path):
    calls = []

    def notify(_text, *, idempotency_key):
        calls.append(idempotency_key)
        return (
            SimpleNamespace(delivered=None)
            if len(calls) == 1
            else SimpleNamespace(delivered=True)
        )

    actor = _authority(
        tmp_path,
        lambda *args, **kwargs: HelperOutcome.FAILED,
        notify,
        notification_retry_seconds=0.01,
    )
    try:
        _fail(actor)
        _wait(lambda: len(calls) == 2)
        _wait(lambda: actor.status()["notificationsPending"] == 0)
        assert calls[0] == calls[1]
        assert actor.status()["notificationsSent"] == 1
    finally:
        assert actor.close(2)


def test_slow_notifier_does_not_block_episode_decisions(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def notify(_text, *, idempotency_key):
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        lambda *args, **kwargs: HelperOutcome.FAILED,
        notify,
    )
    try:
        _fail(actor, "worker-1")
        assert entered.wait(1)
        _fail(actor, "worker-2")
        # Wait for the condition asserted, not for the completed counter:
        # worker-1's own finished-helper decision can complete after its
        # notifier has entered, so "completed > before" may count worker-1.
        # A notifier holding the decision lane would time this out (3 s)
        # while worker-1's notify is still blocked on release.
        _wait(lambda: "worker-2" in actor._core._episodes["kimi-coding"].workers)
    finally:
        release.set()
        assert actor.close(2)


def test_recovered_episode_retires_stale_device_code_without_resend(tmp_path):
    entered, release = threading.Event(), threading.Event()
    recovered = threading.Event()
    calls = []

    def runner(_provider, announce, *, stop):
        announce(DeviceCodeAnnouncement(
            "STALE", "https://auth.example.test/device", 1, 60
        ))
        assert entered.wait(1)
        return HelperOutcome.OK

    def notify(text, *, idempotency_key):
        calls.append((text, idempotency_key))
        if ":code:" in idempotency_key:
            entered.set()
            assert release.wait(3)
            return SimpleNamespace(delivered=False)
        if ":recovered:" in idempotency_key:
            recovered.set()
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        runner,
        notify,
        notification_retry_seconds=0.01,
    )
    try:
        _fail(actor)
        assert entered.wait(1)
        assert recovered.wait(1)
        release.set()
        _wait(lambda: actor.status()["notificationsPending"] == 0)
        time.sleep(0.05)
        code_calls = [key for _text, key in calls if ":code:" in key]
        assert len(code_calls) == 1
        assert any(":recovered:" in key for _text, key in calls)
    finally:
        release.set()
        assert actor.close(2)


def test_close_retains_failed_notice_until_retry_delivers(tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def notify(_text, *, idempotency_key):
        calls.append(idempotency_key)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            return SimpleNamespace(delivered=False)
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        lambda *args, **kwargs: HelperOutcome.FAILED,
        notify,
        notification_retry_seconds=0.01,
    )
    _fail(actor)
    assert entered.wait(1)
    assert not actor.close(0.02)
    assert actor.status()["notificationsPending"] == 1
    release.set()
    _wait(lambda: actor.status()["notificationsPending"] == 0)
    assert calls[0] == calls[1]
    assert actor.close(2)


def test_closing_notice_tracks_pending_separately_from_sent(tmp_path):
    closing_calls = []
    first_closing = threading.Event()
    release_retry = threading.Event()

    def runner(_provider, announce, *, stop):
        announce(DeviceCodeAnnouncement(
            "ROUND", "https://auth.example.test/device", 1, 60
        ))
        return HelperOutcome.TIMED_OUT

    def notify(_text, *, idempotency_key):
        if ":closing:" in idempotency_key:
            closing_calls.append(idempotency_key)
            if len(closing_calls) == 1:
                first_closing.set()
                return SimpleNamespace(delivered=False)
            assert release_retry.wait(3)
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        runner,
        notify,
        notification_retry_seconds=0.1,
    )
    try:
        _fail(actor)
        assert first_closing.wait(2)
        episode = actor._core._episodes["kimi-coding"]
        assert episode.closing_pending
        assert not episode.closing_sent
        release_retry.set()
        _wait(lambda: episode.closing_sent)
        assert not episode.closing_pending
        assert len(closing_calls) == 2
        assert closing_calls[0] == closing_calls[1]
    finally:
        release_retry.set()
        assert actor.close(2)


def test_notification_saturation_coalesces_independent_episode(tmp_path):
    entered, release, account_delivered = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    def notify(_text, *, idempotency_key):
        if ":failed:" in idempotency_key:
            entered.set()
            assert release.wait(3)
        if ":account:" in idempotency_key:
            account_delivered.set()
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        lambda *args, **kwargs: HelperOutcome.FAILED,
        notify,
        notification_capacity=1,
        observation_capacity=2,
        notification_retry_seconds=0.01,
    )
    try:
        assert _fail(actor, "worker-1") is AdmissionResult.ACCEPTED
        assert entered.wait(1)
        result = actor.handle_turn_failure(
            ACCOUNT_FAILURE,
            harness="pi",
            provider="openai-codex",
            model="gpt-6-astra",
            worker="account-worker",
        )
        assert result is AdmissionResult.ACCEPTED
        _wait(
            lambda: "pi:openai-codex:gpt-6-astra"
            in actor._core._episodes
        )
        # Core episode insertion precedes the authority's notice admission.
        _wait(lambda: actor.status()["notificationsPending"] == 2)
        assert actor.status()["notificationsPending"] == 2
        release.set()
        assert account_delivered.wait(1)
        _wait(lambda: actor.status()["notificationsPending"] == 0)
        assert actor.status()["businessCredits"] == 0
    finally:
        release.set()
        assert actor.close(2)
