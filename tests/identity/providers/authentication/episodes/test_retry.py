"""provider_auth_actor: retry scenarios."""

import threading
from types import SimpleNamespace
from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.identity.impl.provider_auth.helper import HelperOutcome

from .support import (
    ACCOUNT_FAILURE,
    _authority,
    _fail,
    _wait,
)


def test_helper_base_exception_keeps_completion_and_releases_credit(tmp_path):
    delivered = threading.Event()

    def runner(*args, **kwargs):
        raise KeyboardInterrupt

    def notify(_text, *, idempotency_key):
        if ":failed:" in idempotency_key:
            delivered.set()
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        runner,
        notify,
        helper_capacity=1,
        notification_capacity=1,
    )
    try:
        assert _fail(actor) is AdmissionResult.ACCEPTED
        assert delivered.wait(1)
        _wait(lambda: actor.status()["helpers"] == 0)
        assert actor.status()["helperPending"] == 0
    finally:
        assert actor.close(2)


def test_failed_notice_retry_does_not_starve_independent_notice(tmp_path):
    account_delivered = threading.Event()
    allow_failure = threading.Event()
    keys = []

    def notify(_text, *, idempotency_key):
        keys.append(idempotency_key)
        if ":failed:" in idempotency_key and not allow_failure.is_set():
            return SimpleNamespace(delivered=False)
        if ":account:" in idempotency_key:
            account_delivered.set()
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        lambda *args, **kwargs: HelperOutcome.FAILED,
        notify,
        notification_retry_seconds=0.02,
    )
    try:
        _fail(actor)
        _wait(lambda: any(":failed:" in key for key in keys))
        actor.handle_turn_failure(
            ACCOUNT_FAILURE,
            harness="pi",
            provider="openai-codex",
            model="gpt-6-astra",
            worker="account-worker",
        )
        assert account_delivered.wait(1)
        assert actor.status()["notificationsPending"] >= 1
        allow_failure.set()
        _wait(lambda: actor.status()["notificationsPending"] == 0)
    finally:
        allow_failure.set()
        assert actor.close(2)


def test_deferred_helper_retry_revalidates_on_episode_owner(tmp_path, monkeypatch):
    first, second, release = threading.Event(), threading.Event(), threading.Event()
    retry_threads = []

    def runner(provider, announce, *, stop):
        if provider == "kimi-coding":
            first.set()
            assert release.wait(3)
        else:
            second.set()
        return HelperOutcome.STOPPED

    actor = _authority(
        tmp_path, runner, helper_capacity=1, notification_retry_seconds=0.01
    )
    start = actor._start_helper

    def tracked(episode, **kwargs):
        if kwargs.get("allow_defer") is False:
            retry_threads.append(threading.current_thread().name)
        return start(episode, **kwargs)

    monkeypatch.setattr(actor, "_start_helper", tracked)
    try:
        assert _fail(actor) is AdmissionResult.ACCEPTED
        assert first.wait(1)
        assert actor.handle_turn_failure(
            "OAuth refresh failed for openai-codex: invalid_grant",
            harness="pi", provider="openai-codex", model="gpt", worker="second",
        ) is AdmissionResult.ACCEPTED
        _wait(lambda: actor.status()["helperStartsPending"] == 1)
        release.set()
        assert second.wait(2)
        _wait(lambda: actor.status()["helpers"] == 0)
        assert retry_threads
        assert set(retry_threads) == {"provider-auth-state-0"}
    finally:
        release.set()
        assert actor.close(2)
