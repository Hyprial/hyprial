"""provider_auth_actor: custody scenarios."""

import threading
from types import SimpleNamespace
from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.identity.impl.provider_auth.coordinator._base import (
    REASON_PROVIDER_AUTH_INVALID,
)
from hyprial.identity.impl.provider_auth.helper import HelperOutcome, DeviceCodeAnnouncement

from .support import (
    ACCOUNT_FAILURE,
    _authority,
    _fail,
    _mark_provider,
    _status_with_retry_heap_count,
    _wait,
)


def test_capacity_one_helper_ok_supersedes_undelivered_code(tmp_path):
    code_attempted = threading.Event()
    recovered = threading.Event()
    keys = []

    def runner(_provider, announce, *, stop):
        announce(DeviceCodeAnnouncement(
            "STALE", "https://auth.example.test/device", 1, 60
        ))
        assert code_attempted.wait(1)
        return HelperOutcome.OK

    def notify(_text, *, idempotency_key):
        keys.append(idempotency_key)
        if ":code:" in idempotency_key:
            code_attempted.set()
            return SimpleNamespace(delivered=False)
        if ":recovered:" in idempotency_key:
            recovered.set()
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        runner,
        notify,
        notification_capacity=1,
        helper_capacity=1,
        notification_retry_seconds=0.02,
    )
    try:
        assert _fail(actor) is AdmissionResult.ACCEPTED
        assert code_attempted.wait(1)
        assert recovered.wait(1)
        _wait(lambda: "kimi-coding" not in actor._core._episodes)
        _wait(lambda: actor.status()["notificationsPending"] == 0)
        code_keys = [key for key in keys if ":code:" in key]
        assert code_keys and len(set(code_keys)) == 1
        assert actor.status()["helpers"] == 0
        assert actor.status()["businessCredits"] == 0
    finally:
        assert actor.close(2)


def test_old_recovery_completion_cannot_release_new_episode_credit(tmp_path):
    old_notice_entered = threading.Event()
    release_old_notice = threading.Event()
    new_helper_entered = threading.Event()
    release_new_helper = threading.Event()
    recovered = []
    calls = 0

    def runner(_provider, _announce, *, stop):
        nonlocal calls
        calls += 1
        if calls == 1:
            return HelperOutcome.OK
        new_helper_entered.set()
        assert release_new_helper.wait(3)
        return HelperOutcome.OK

    def notify(text, *, idempotency_key):
        if ":recovered:" in idempotency_key:
            recovered.append(idempotency_key)
            if len(recovered) == 1:
                old_notice_entered.set()
                assert release_old_notice.wait(3)
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        runner,
        notify,
        helper_capacity=2,
        notification_capacity=2,
        observation_capacity=2,
        notification_retry_seconds=0.01,
    )
    try:
        assert _fail(actor, "old") is AdmissionResult.ACCEPTED
        assert old_notice_entered.wait(1)
        assert _fail(actor, "new") is AdmissionResult.ACCEPTED
        assert new_helper_entered.wait(1)
        refs = {
            ref: token
            for ref, token in actor._episode_credits.items()
            if ref[0] == "kimi-coding"
        }
        assert len(refs) == 2
        assert len(set(refs.values())) == 2

        release_old_notice.set()
        _wait(
            lambda: len(
                [
                    ref
                    for ref in actor._episode_credits
                    if ref[0] == "kimi-coding"
                ]
            )
            == 1
        )
        refs = {
            ref: token
            for ref, token in actor._episode_credits.items()
            if ref[0] == "kimi-coding"
        }
        assert len(refs) == 1
        assert next(iter(refs)) == (
            "kimi-coding",
            actor._core._episodes["kimi-coding"].episode_id,
        )
        assert actor.status()["businessCredits"] == 1

        release_new_helper.set()
        _wait(lambda: len(recovered) == 2)
        _wait(lambda: actor.status()["businessCredits"] == 0)
    finally:
        release_old_notice.set()
        release_new_helper.set()
        assert actor.close(2)


def test_restore_fanout_children_settle_independently(tmp_path):
    second_helper_entered = threading.Event()
    release_second_helper = threading.Event()
    first_recovered = threading.Event()
    recovered = []

    def runner(provider, _announce, *, stop):
        if provider == "openai-codex":
            second_helper_entered.set()
            assert release_second_helper.wait(3)
        return HelperOutcome.OK

    def notify(text, *, idempotency_key):
        if ":recovered:" in idempotency_key:
            recovered.append(text.splitlines()[0])
            if "kimi-coding" in text:
                first_recovered.set()
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        runner,
        notify,
        helper_capacity=2,
        notification_capacity=2,
        observation_capacity=2,
        notification_retry_seconds=0.01,
    )
    _mark_provider(actor, "kimi-coding", "kimi-for-coding")
    _mark_provider(actor, "openai-codex", "gpt")
    try:
        assert actor.resume_after_restore() is AdmissionResult.ACCEPTED
        assert second_helper_entered.wait(1)
        assert first_recovered.wait(1)
        _wait(lambda: actor.status()["notificationsSent"] == 1)
        second = actor._core._episodes["openai-codex"]
        assert (second.key, second.episode_id) in actor._episode_credits
        assert actor.status()["businessCredits"] == 1
        assert actor.status()["episodeCredits"] == 1

        release_second_helper.set()
        _wait(lambda: len(recovered) == 2)
        _wait(lambda: actor.status()["businessCredits"] == 0)
        assert actor._core._providers_marked(
            REASON_PROVIDER_AUTH_INVALID
        ) == []
    finally:
        release_second_helper.set()
        assert actor.close(2)


def test_restore_fanout_defers_at_capacity_one_without_losing_child(tmp_path):
    first_recovered = threading.Event()
    second_helper_entered = threading.Event()
    recovered = []

    def runner(provider, _announce, *, stop):
        if provider == "openai-codex":
            second_helper_entered.set()
        return HelperOutcome.OK

    def notify(text, *, idempotency_key):
        if ":recovered:" in idempotency_key:
            recovered.append(text.splitlines()[0])
            if "kimi-coding" in text:
                first_recovered.set()
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        runner,
        notify,
        helper_capacity=2,
        notification_capacity=1,
        observation_capacity=1,
        notification_retry_seconds=0.01,
    )
    _mark_provider(actor, "kimi-coding", "kimi-for-coding")
    _mark_provider(actor, "openai-codex", "gpt")
    try:
        assert actor.resume_after_restore() is AdmissionResult.ACCEPTED
        assert first_recovered.wait(1)
        assert second_helper_entered.wait(1)
        _wait(lambda: len(recovered) == 2)
        # Native notification entry is not its owner-settled receipt.
        _wait(lambda: actor.status()["notificationsSent"] == 2)
        _wait(lambda: actor.status()["businessCredits"] == 0)
        status = actor.status()
        assert status["restoreChildCapacity"] == 1
        assert status["episodeCredits"] <= status["episodeCustodyCapacity"]
        assert status["businessCredits"] == 0
        assert actor._core._providers_marked(
            REASON_PROVIDER_AUTH_INVALID
        ) == []
    finally:
        assert actor.close(2)


def test_total_custody_is_bounded_and_close_retries_after_saturation(tmp_path):
    attempted = threading.Event()
    deliver = threading.Event()

    def notify(_text, *, idempotency_key):
        attempted.set()
        return SimpleNamespace(delivered=deliver.is_set())

    actor = _authority(
        tmp_path,
        lambda *args, **kwargs: HelperOutcome.FAILED,
        notify,
        notification_capacity=1,
        helper_capacity=1,
        observation_capacity=2,
        notification_retry_seconds=0.01,
    )
    assert _fail(actor, "first") is AdmissionResult.ACCEPTED
    assert attempted.wait(1)
    second = actor.handle_turn_failure(
        ACCOUNT_FAILURE,
        harness="pi",
        provider="openai-codex",
        model="gpt-6-astra",
        worker="second",
    )
    third = actor.handle_turn_failure(
        ACCOUNT_FAILURE,
        harness="claude",
        provider="anthropic",
        model="claude-opus",
        worker="third",
    )
    fourth = actor.handle_turn_failure(
        ACCOUNT_FAILURE,
        harness="codex",
        provider="openai",
        model="gpt-6-sol",
        worker="fourth",
    )
    assert second is AdmissionResult.ACCEPTED
    assert third is AdmissionResult.OVERLOADED
    assert fourth is AdmissionResult.OVERLOADED
    status, retry_heap_count = _status_with_retry_heap_count(actor)
    assert status["businessCredits"] == status["businessCapacity"] == 2
    assert status["custody"] <= status["custodyCapacity"]
    assert status["retryScheduled"] <= (
        status["notificationsPending"]
        + status["observationsPending"]
        + status["helperStartsPending"]
    )
    assert retry_heap_count == status["retryScheduled"]
    assert not actor.close(0.02)
    deliver.set()
    assert actor.close(2)
    assert actor.status()["businessCredits"] == 0


def test_helper_successor_uses_reserved_credit_before_old_completion_ack(tmp_path):
    rounds = []
    code_keys = []
    closing = threading.Event()
    first_code_entered = threading.Event()

    def runner(_provider, announce, *, stop):
        rounds.append(len(rounds) + 1)
        announce(DeviceCodeAnnouncement(
            f"ROUND-{rounds[-1]}", "https://auth.example.test/device", 1, 60
        ))
        if rounds[-1] == 1:
            assert first_code_entered.wait(1)
        return HelperOutcome.TIMED_OUT

    def notify(_text, *, idempotency_key):
        if ":code:" in idempotency_key:
            code_keys.append(idempotency_key)
            first_code_entered.set()
            # Hold the one native slot until helpers finish. This establishes
            # the intended single stale attempt instead of depending on a
            # race between a valid retry and helper completion.
            _wait(lambda: all(episode.round == 3 and not episode.inflight
                              for episode in actor._core._episodes.values()))
            return SimpleNamespace(delivered=False)
        if ":closing:" in idempotency_key:
            closing.set()
        return SimpleNamespace(delivered=True)

    actor = _authority(
        tmp_path,
        runner,
        notify,
        helper_capacity=1,
        notification_capacity=1,
        notification_retry_seconds=0.01,
    )
    try:
        assert _fail(actor) is AdmissionResult.ACCEPTED
        assert closing.wait(1)
        _wait(lambda: actor.status()["helpers"] == 0)
        assert rounds == [1, 2, 3]
        assert code_keys and len(code_keys) == len(set(code_keys))
        assert actor.status()["helperStartsPending"] == 0
        _wait(lambda: actor.status()["notificationsPending"] == 0)
        assert actor.status()["businessCredits"] == 0
    finally:
        assert actor.close(2)
