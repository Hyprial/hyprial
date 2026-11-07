import threading
import time

from hyprial.identity.impl.provider_auth.actor._authority import ProviderAuthAuthority
from hyprial.identity.impl.provider_auth.coordinator._base import (
    REASON_PROVIDER_AUTH_INVALID,
)
from hyprial.identity.impl.users.profile import RuntimeCapability, UserProfileStore

FAILURE = "OAuth refresh failed for kimi-coding: Kimi Code token refresh unauthorized (status 400): The provided authorization grant is invalid"
ACCOUNT_FAILURE = (
    "Codex error: The 'gpt-6-astra' model is not supported when using "
    "Codex with a ChatGPT account."
)


def _wait(predicate, *, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


def _status_with_retry_heap_count(actor):
    """Read both independent retry counters under status's existing lock."""
    condition = actor._retry_condition
    caller = threading.get_ident()
    heap_counts = []

    class ObservedCondition:
        def __getattr__(self, name):
            return getattr(condition, name)

        def __enter__(self):
            return condition.__enter__()

        def __exit__(self, *args):
            try:
                if threading.get_ident() == caller:
                    heap_counts.append(len(actor._retry_heap))
            finally:
                condition.__exit__(*args)

    # All actors keep using the same underlying condition. The hook only
    # observes this caller, before status releases its retry-count lock.
    actor._retry_condition = ObservedCondition()
    try:
        status = actor.status()
    finally:
        actor._retry_condition = condition
    assert len(heap_counts) == 1
    return status, heap_counts[0]


def _authority(
    tmp_path,
    runner,
    notifier=lambda *args, **kwargs: None,
    **options,
):
    store = UserProfileStore(tmp_path / "users.json")
    store.ensure(
        owner="alice",
        owner_key="alice-key",
        login_name="alice",
        machine="node",
        machine_key="node-key",
    )
    return ProviderAuthAuthority(
        profile_store=store,
        owner_key="alice-key",
        notifier=notifier,
        helper_runner=runner,
        auth_path=tmp_path / "auth.json",
        host="node",
        stop=threading.Event(),
        **options,
    )


def _fail(actor, worker="worker"):
    return actor.handle_turn_failure(
        FAILURE,
        harness="pi",
        provider="kimi-coding",
        model="kimi-for-coding",
        worker=worker,
    )


def _mark_provider(actor, provider, model):
    combos = actor._core._combos_for_provider(provider)
    assert combos, (provider, model)
    for harness, marked_provider, marked_model in combos:
        actor._core._store.set_runtime_capability(
            "alice-key",
            RuntimeCapability(
                harness=harness,
                provider=marked_provider,
                model=marked_model,
                status="unavailable",
                reason=REASON_PROVIDER_AUTH_INVALID,
                probed_at="2026-09-28T00:00:00Z",
            ),
        )
