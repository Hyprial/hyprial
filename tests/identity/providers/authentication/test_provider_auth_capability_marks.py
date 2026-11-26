"""S3c: provider-auth marks live in the runtime capability cache, not in a profile.

The coordinator takes ``capability_store`` and a mandatory ``owner_key``; no
``users.json`` exists in these homes.  Marks must persist across store objects
(a daemon restart) and recovery must clear them.

Decision (2026-11-26, PR #78 gate run 45792478153): the last statement of
``test_a_restarted_coordinator_resumes_from_the_persisted_marks`` releases the
first coordinator's round, and that ``hyprial-provider-auth-*`` thread then
clears its marks, i.e. it writes ``state/`` after the test has returned.  On a
loaded runner the write met the suite's protocol-end reclamation of
``tmp_path`` and the xdist worker died on ``ENOTEMPTY``.  The test now ends
with a bounded wait for the ``hyprial-provider-auth-*`` threads it started.
The wait only covers such threads that appeared after the test began: a
same-named thread that already existed when the test started (leaked by an
earlier test in the same process) is not awaited.
Only that name prefix is awaited: the ``org-network-*`` projection threads
live until the process exits and are unrelated to this test.
"""

from __future__ import annotations

import inspect
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from hyprial.daemon.impl.dispatch import matrix
from hyprial.identity import RuntimeCapabilityStore
from hyprial.identity.impl.provider_auth import (
    REASON_PROVIDER_AUTH_INVALID,
    DeviceCodeAnnouncement,
    HelperOutcome,
    ProviderAuthCoordinator,
)

OWNER = "owner-key"
KIMI_A = (
    "OAuth refresh failed for kimi-coding: Kimi Code token refresh "
    "unauthorized (status 400): The provided authorization grant is invalid"
)
ANNOUNCEMENT = DeviceCodeAnnouncement(
    user_code="ABCD-EFGH",
    verification_uri="https://auth.example.com/device",
    interval_seconds=5,
    expires_in_seconds=900,
)


def wait_until(predicate, *, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class Notifier:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def __call__(self, text: str, *, idempotency_key: str):
        self.texts.append(text)
        return SimpleNamespace(delivered=True, message_id="m-1")


class Runner:
    """One call = one round; stays in flight until released."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._release = threading.Event()

    def __call__(self, provider, on_device_code, *, stop=None) -> HelperOutcome:
        self.calls.append(provider)
        self._release.wait(timeout=10)
        on_device_code(ANNOUNCEMENT)
        return HelperOutcome.OK

    def release(self) -> None:
        self._release.set()


def kimi_combos() -> list[tuple[str, str, str]]:
    combos: list[tuple[str, str, str]] = []
    for candidates in matrix.TIERS.values():
        for candidate in candidates:
            combo = (candidate.harness, candidate.provider, candidate.model)
            if candidate.harness == "pi" and candidate.provider == "kimi-coding":
                if combo not in combos:
                    combos.append(combo)
    return combos


def build(tmp_path: Path, runner: Runner, notifier: Notifier, store: RuntimeCapabilityStore):
    auth_path = tmp_path / "auth.json"
    return ProviderAuthCoordinator(
        capability_store=store,
        owner_key=OWNER,
        notifier=notifier,
        helper_runner=runner,
        auth_path=auth_path,
        host="test-host",
        stop=threading.Event(),
    )


def marks(store: RuntimeCapabilityStore) -> list:
    return [item for item in store.list(OWNER) if item.provider == "kimi-coding"]


def test_constructor_takes_a_capability_store_and_a_mandatory_owner_key() -> None:
    parameters = inspect.signature(ProviderAuthCoordinator).parameters

    assert "capability_store" in parameters
    assert "profile_store" not in parameters
    assert parameters["owner_key"].default is inspect.Parameter.empty
    assert "None" not in str(parameters["owner_key"].annotation)


def test_failure_marks_persist_and_recovery_clears_them_without_users_json(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    store = RuntimeCapabilityStore(state / "runtime-capabilities.json")
    runner, notifier = Runner(), Notifier()
    coordinator = build(tmp_path, runner, notifier, store)

    coordinator.handle_turn_failure(
        harness="pi", provider="kimi-coding", model="k3",
        worker="pi-worker-1", failure=KIMI_A,
    )
    wait_until(lambda: len(runner.calls) == 1, what="helper round")

    reopened = RuntimeCapabilityStore(state / "runtime-capabilities.json")
    broken = marks(reopened)
    assert len(broken) == len(kimi_combos())
    assert all(item.status == "unavailable" for item in broken)
    assert all(item.reason == REASON_PROVIDER_AUTH_INVALID for item in broken)
    document = json.loads(reopened.path.read_text(encoding="utf-8"))
    assert set(document) == {"version", "owners"}
    assert [owner["ownerKey"] for owner in document["owners"]] == [OWNER]

    runner.release()
    wait_until(lambda: len(notifier.texts) == 2, what="alert + recovery notice")

    cleared = marks(RuntimeCapabilityStore(state / "runtime-capabilities.json"))
    assert cleared and all(item.status == "available" for item in cleared)
    assert all(item.reason is None for item in cleared)
    assert not (state / "users.json").exists()
    assert not (tmp_path / "users.json").exists()


def test_a_restarted_coordinator_resumes_from_the_persisted_marks(tmp_path: Path) -> None:
    threads_before = set(threading.enumerate())
    store = RuntimeCapabilityStore(tmp_path / "state" / "runtime-capabilities.json")
    first_runner = Runner()
    first = build(tmp_path, first_runner, Notifier(), store)
    first.handle_turn_failure(
        harness="pi", provider="kimi-coding", model="k3",
        worker="pi-worker-1", failure=KIMI_A,
    )
    wait_until(lambda: len(marks(store)) == len(kimi_combos()), what="marks written")

    runner, notifier = Runner(), Notifier()
    second = build(
        tmp_path, runner, notifier,
        RuntimeCapabilityStore(tmp_path / "state" / "runtime-capabilities.json"),
    )
    second.resume_after_restore()
    wait_until(lambda: len(runner.calls) == 1, what="one reopened round")
    time.sleep(0.1)

    assert len(runner.calls) == 1
    runner.release()
    wait_until(lambda: len(notifier.texts) == 2, what="alert + recovery notice")
    assert all(item.status == "available" for item in marks(store))
    first_runner.release()

    still_running: list[str] = []

    def round_threads_are_done() -> bool:
        still_running[:] = sorted(
            thread.name
            for thread in threading.enumerate()
            if thread not in threads_before and thread.name.startswith("hyprial-provider-auth-")
        )
        return not still_running

    try:
        wait_until(round_threads_are_done, what="provider-auth round threads to finish")
    except AssertionError as error:
        raise AssertionError(
            f"timed out waiting for provider-auth round threads to finish; "
            f"still alive: {still_running}"
        ) from error
