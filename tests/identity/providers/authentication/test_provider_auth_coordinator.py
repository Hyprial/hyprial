"""Coordinator state-machine tests: real profile store, stubbed boundaries.

The stubs sit exactly on the two process boundaries the production wiring
uses — the helper runner (one call = one device-code round) and the owner
notifier — and produce real-shaped objects (``DeviceCodeAnnouncement``,
``AlertOutcome``-like), not invented shapes.  Everything between them is the
production code: classification, episode/round state, profile marking via a
real ``UserProfileStore`` on a tmp file, and ``dispatch.matrix`` reads of
those very marks.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from hyprial.identity.impl.agents import (
    AgentConfig,
    AgentRegistry,
    resolve_agent_runtime_context,
)
from hyprial.daemon.impl.dispatch import matrix
from hyprial.identity.impl.provider_auth import (
    MAX_ROUNDS,
    REASON_PROVIDER_AUTH_ACCOUNT,
    REASON_PROVIDER_AUTH_INVALID,
    RELOGIN_COOLDOWN_SECONDS,
    DeviceCodeAnnouncement,
    HelperOutcome,
    ProviderAuthCoordinator,
)
from hyprial.identity.impl.users.profile import UserProfileStore
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE

KIMI_A = (
    "OAuth refresh failed for kimi-coding: Kimi Code token refresh "
    "unauthorized (status 400): The provided authorization grant is invalid"
)
OPENAI_OAUTH_A = (
    "OAuth refresh failed for openai-codex: OpenAI Codex token refresh "
    "unauthorized (status 400): The provided authorization grant is invalid"
)
CODEX_B = (
    "Codex error: The 'gpt-6-astra' model is not supported when using "
    "Codex with a ChatGPT account."
)
ANNOUNCEMENT = DeviceCodeAnnouncement(
    user_code="ABCD-EFGH",
    verification_uri="https://auth.example.com/device",
    interval_seconds=5,
    expires_in_seconds=900,
)


def wait_until(predicate, *, timeout: float = 5.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(self, text: str, *, idempotency_key: str):
        with self._lock:
            self.messages.append((text, idempotency_key))
        return SimpleNamespace(delivered=True, message_id="m-1")

    def texts(self) -> list[str]:
        with self._lock:
            return [text for text, _ in self.messages]


class FakeRunner:
    """One call = one round.  Script entries: (announcement|None, outcome).

    ``outcome=None`` means the flow stays inflight until ``release()``: the
    block happens BEFORE the announcement, matching production (pi's device
    authorization round-trip takes a moment), so tests can pile up failures
    while the code is being fetched.
    """

    def __init__(self, script: list[tuple[DeviceCodeAnnouncement | None, HelperOutcome | None]]):
        self._script = list(script)
        self.calls: list[str] = []
        self._release = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, provider, on_device_code, *, stop=None) -> HelperOutcome:
        with self._lock:
            self.calls.append(provider)
            spec = self._script.pop(0) if self._script else (None, HelperOutcome.FAILED)
        announcement, outcome = spec
        if outcome is None:
            self._release.wait(timeout=10)
        if announcement is not None:
            on_device_code(announcement)
        return outcome if outcome is not None else HelperOutcome.OK

    def release(self) -> None:
        self._release.set()


def make_coordinator(
    tmp_path: Path,
    runner: FakeRunner,
    *,
    notifier: FakeNotifier | None = None,
    auth: dict | None = None,
    premark: bool = False,
    clock: Callable[[], float] | None = None,
    runtime_helper_factory=None,
    runtime_context_validator=None,
):
    store = UserProfileStore(tmp_path / "users.json")
    store.ensure(
        owner="alicesmith",
        owner_key="owner-key",
        login_name="unit",
        machine="test-node",
        machine_key="machine-key",
    )
    auth_path = tmp_path / "auth.json"
    if auth is not None:
        auth_path.write_text(json.dumps(auth), "utf-8")
    notifier = notifier or FakeNotifier()
    coordinator = ProviderAuthCoordinator(
        profile_store=store,
        owner_key="owner-key",
        notifier=notifier,
        helper_runner=runner,
        auth_path=auth_path,
        host="test-host",
        stop=threading.Event(),
        clock=clock or time.time,
        runtime_helper_factory=runtime_helper_factory,
        runtime_context_validator=runtime_context_validator,
    )
    if premark:
        # What a previous daemon generation left behind: broken marks.
        for harness, provider, model in _kimi_combos():
            store.set_runtime_capability(
                "owner-key",
                _cap(harness, provider, model, "unavailable", REASON_PROVIDER_AUTH_INVALID),
            )
    return coordinator, store, notifier


def _kimi_combos() -> list[tuple[str, str, str]]:
    combos = []
    for candidates in matrix.TIERS.values():
        for candidate in candidates:
            if candidate.harness == "pi" and candidate.provider == "kimi-coding":
                combo = (candidate.harness, candidate.provider, candidate.model)
                if combo not in combos:
                    combos.append(combo)
    return combos


def _cap(harness, provider, model, status, reason=None):
    from hyprial.identity.impl.users.profile import RuntimeCapability

    return RuntimeCapability(
        harness=harness,
        provider=provider,
        model=model,
        status=status,
        reason=reason,
        probed_at="2026-09-14T10:00:00Z",
    )


def _capabilities(store: UserProfileStore, provider: str):
    profile = store.get("owner-key")
    assert profile is not None
    return [
        c
        for c in profile.runtime_capabilities
        if c.provider == provider
    ]


def live_auth(provider: str = "kimi-coding") -> dict:
    return {
        provider: {
            "type": "oauth",
            "access": "stub-access",
            "refresh": "stub-refresh",
            "expires": (time.time() + 3600) * 1000,
        }
    }


# --------------------------------------------------------------------- A 类


def test_t14_codex_auth_failure_never_runs_the_pi_login_helper(
    tmp_path: Path,
) -> None:
    runner = FakeRunner([(ANNOUNCEMENT, HelperOutcome.OK)])
    coordinator, store, notifier = make_coordinator(tmp_path, runner)

    coordinator.handle_turn_failure(
        harness="codex",
        provider="openai-codex",
        model="gpt-6-sol",
        worker="codex-worker-1",
        failure=OPENAI_OAUTH_A,
    )

    assert runner.calls == []
    [message] = notifier.texts()
    assert "codex" in message
    assert "自动重新登录不支持" in message
    capability = next(
        item
        for item in store.get("owner-key").runtime_capabilities
        if item.harness == "codex"
        and item.provider == "openai-codex"
        and item.model == "gpt-6-sol"
    )
    assert capability.status == "unavailable"
    assert capability.reason == REASON_PROVIDER_AUTH_INVALID


def test_t14_codex_p2_failure_notifies_per_context_without_owner_global_mark(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    native = config / "native" / "codex"
    native.mkdir(parents=True)
    (native / "config.toml").write_text('model = "p24"\n', encoding="utf-8")
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="owner",
        machine="node",
        hyprial_home=tmp_path / "hyprial-home",
    )
    try:
        agent = registry.create("worker", config=AgentConfig(str(config)))
        context = resolve_agent_runtime_context(
            registry=registry,
            agent_name=agent.actor,
            harness="codex",
            cwd=None,
            tool_profile=TEST_TOOL_PROFILE,
        )
        assert context is not None
        runner = FakeRunner([(ANNOUNCEMENT, HelperOutcome.OK)])
        coordinator, store, notifier = make_coordinator(
            tmp_path,
            runner,
            runtime_helper_factory=lambda _context: runner,
            runtime_context_validator=lambda candidate: candidate is context,
        )

        coordinator.handle_turn_failure(
            harness="codex",
            provider="openai-codex",
            model="gpt-6-sol",
            worker="codex-worker-p2",
            failure=OPENAI_OAUTH_A,
            runtime_context=context,
        )
        coordinator.handle_turn_failure(
            harness="codex",
            provider="openai-codex",
            model="gpt-6-sol",
            worker="codex-worker-p2-second",
            failure=OPENAI_OAUTH_A,
            runtime_context=context,
        )

        assert runner.calls == []
        [message] = notifier.texts()
        assert "codex" in message
        assert "自动重新登录不支持" in message
        assert _capabilities(store, "openai-codex") == []
    finally:
        registry.close()


def test_a_failure_marks_all_provider_combos_and_announces(tmp_path: Path) -> None:
    runner = FakeRunner([(ANNOUNCEMENT, None)])  # inflight
    coordinator, store, notifier = make_coordinator(tmp_path, runner)

    coordinator.handle_turn_failure(
        harness="pi", provider="kimi-coding", model="k3",
        worker="pi-worker-1", failure=KIMI_A,
    )
    wait_until(lambda: len(runner.calls) == 1, what="helper round")

    marks = _capabilities(store, "kimi-coding")
    assert marks and len(marks) == len(_kimi_combos())
    assert all(m.status == "unavailable" for m in marks)
    assert all(m.reason == REASON_PROVIDER_AUTH_INVALID for m in marks)

    runner.release()  # code issued, then login completes
    wait_until(lambda: len(notifier.texts()) == 2,
               what="device-code alert + recovery notice")

    text = notifier.texts()[0]
    assert "https://auth.example.com/device" in text
    assert "ABCD-EFGH" in text
    assert "pi-worker-1" in text
    assert "test-host" in text
    assert f"1/{MAX_ROUNDS}" in text
    # Safety: the polling secret and token vocabulary never reach the owner.
    for forbidden in ("device_code", "device_auth_id", "access_token",
                      "refresh_token", "stub-access", "stub-refresh"):
        assert forbidden not in text, forbidden

    assert "已恢复" in notifier.texts()[1]
    assert all(m.status == "available" for m in _capabilities(store, "kimi-coding"))


def test_n_workers_one_flow_one_notice(tmp_path: Path) -> None:
    runner = FakeRunner([(ANNOUNCEMENT, None)])
    coordinator, _, notifier = make_coordinator(tmp_path, runner)

    for worker in ("pi-a", "pi-b", "pi-c"):
        coordinator.handle_turn_failure(
            harness="pi", provider="kimi-coding", model="k3",
            worker=worker, failure=KIMI_A,
        )
    wait_until(lambda: len(runner.calls) == 1, what="helper round")
    runner.release()  # the code arrives AFTER all three failures piled up
    wait_until(lambda: len(notifier.texts()) == 2,
               what="single code alert + recovery")
    time.sleep(0.1)
    assert len(runner.calls) == 1  # dedup: no second flow
    text = notifier.texts()[0]
    assert "受影响 worker(3)" in text
    for worker in ("pi-a", "pi-b", "pi-c"):
        assert worker in text


def test_mark_does_not_skip_marked_provider(tmp_path: Path) -> None:
    """PC3 at the coordinator boundary: a real A mark exists and is ignored.

    Migration: this replaces ``test_matrix_skips_marked_provider``, which
    asserted the retired contract (a mark filtered the candidate and an
    all-marked tier raised ``NoCapableHarness``).  The mark is written by the
    real coordinator here, read back from disk, and must leave selection
    exactly where the static pool puts it.
    """

    runner = FakeRunner([(ANNOUNCEMENT, None)])
    coordinator, store, _ = make_coordinator(tmp_path, runner)
    coordinator.handle_turn_failure(
        harness="pi", provider="kimi-coding", model="k3",
        worker="pi-worker-1", failure=KIMI_A,
    )
    wait_until(lambda: len(runner.calls) == 1, what="mark written")
    # Mark every remaining strong candidate too, so no candidate is left
    # unmarked: the old code would have had nothing to fall back to.
    for candidate in matrix.TIERS["strong"]:
        if candidate.provider != "kimi-coding":
            store.set_runtime_capability(
                "owner-key",
                _cap(candidate.harness, candidate.provider, candidate.model,
                     "unavailable", "usage-limit"),
            )
    profile = store.get("owner-key")
    kimi = [c for c in profile.runtime_capabilities if c.provider == "kimi-coding"]
    assert kimi and all(c.status == "unavailable" for c in kimi)

    choice = matrix.resolve("strong")
    assert choice.selected == matrix.TIERS["strong"][0]
    assert choice.probed is False
    assert choice.readings == ()

    runner.release()
    wait_until(
        lambda: all(
            m.status == "available" for m in _capabilities(store, "kimi-coding")
        ),
        what="marks cleared on recovery",
    )
    # Recovery still works and the kimi combo is live again; the other marks
    # here are this test's own scaffolding.
    profile = store.get("owner-key")
    kimi = [c for c in profile.runtime_capabilities if c.provider == "kimi-coding"]
    assert kimi and all(c.status == "available" for c in kimi)
    assert matrix.resolve("strong").selected == matrix.TIERS["strong"][0]


def test_manual_relogin_reconciles_on_next_failure(tmp_path: Path) -> None:
    runner = FakeRunner([(ANNOUNCEMENT, None)])
    auth = live_auth()
    coordinator, store, notifier = make_coordinator(tmp_path, runner, auth=None)
    coordinator.handle_turn_failure(
        harness="pi", provider="kimi-coding", model="k3",
        worker="pi-worker-1", failure=KIMI_A,
    )
    wait_until(lambda: len(runner.calls) == 1, what="helper round")
    # Owner relogins by hand: a fresh credential lands in auth.json.
    (tmp_path / "auth.json").write_text(json.dumps(auth), "utf-8")
    coordinator.handle_turn_failure(
        harness="pi", provider="kimi-coding", model="k3",
        worker="pi-worker-2", failure=KIMI_A,
    )
    wait_until(lambda: len(notifier.texts()) == 1, what="recovery notice")
    assert "已恢复" in notifier.texts()[0]
    assert all(m.status == "available" for m in _capabilities(store, "kimi-coding"))
    assert len(runner.calls) == 1  # no second flow was started

    # The still-inflight helper resolving later must neither announce its
    # now-useless code nor re-announce the recovery.
    runner.release()
    time.sleep(0.2)
    assert len(notifier.texts()) == 1


def test_exhaustion_three_rounds_one_closing_notice(tmp_path: Path) -> None:
    runner = FakeRunner([(ANNOUNCEMENT, HelperOutcome.TIMED_OUT)] * MAX_ROUNDS)
    coordinator, _, notifier = make_coordinator(tmp_path, runner)
    coordinator.handle_turn_failure(
        harness="pi", provider="kimi-coding", model="k3",
        worker="pi-worker-1", failure=KIMI_A,
    )
    wait_until(lambda: len(runner.calls) == MAX_ROUNDS, what="three rounds")
    wait_until(lambda: len(notifier.texts()) == MAX_ROUNDS + 1,
               what="three code alerts + one closing notice")
    time.sleep(0.2)
    assert len(runner.calls) == MAX_ROUNDS  # no fourth round
    closing = notifier.texts()[-1]
    assert "已停止自动重发" in closing
    assert "/login kimi-coding" in closing
    # The mark stays: dispatch keeps skipping while the provider is broken.
    assert all(
        m.status == "unavailable"
        for m in _capabilities(make_store(tmp_path), "kimi-coding")
    )


def make_store(tmp_path: Path) -> UserProfileStore:
    return UserProfileStore(tmp_path / "users.json")


def test_exhaustion_enters_cooldown_then_allows_fresh_episode(
    tmp_path: Path,
) -> None:
    """S2: a broken/expired helper is capped by a per-provider cooldown.

    After MAX_ROUNDS, the next A failure inside the window only updates the
    record (no helper, no DM); after the window a fresh episode may start.
    """

    now = [1000.0]
    runner = FakeRunner(
        [(ANNOUNCEMENT, HelperOutcome.TIMED_OUT)] * MAX_ROUNDS
        + [(ANNOUNCEMENT, None)]
    )
    coordinator, _, notifier = make_coordinator(
        tmp_path, runner, clock=lambda: now[0]
    )
    coordinator.handle_turn_failure(
        harness="pi", provider="kimi-coding", model="k3",
        worker="pi-worker-1", failure=KIMI_A,
    )
    wait_until(lambda: len(notifier.texts()) == MAX_ROUNDS + 1,
               what="3 code alerts + closing notice")
    assert len(runner.calls) == MAX_ROUNDS

    # Inside the cooldown window: no helper, no DM -- only the record grows.
    coordinator.handle_turn_failure(
        harness="pi", provider="kimi-coding", model="k3",
        worker="pi-worker-2", failure=KIMI_A,
    )
    time.sleep(0.1)
    assert len(runner.calls) == MAX_ROUNDS
    assert len(notifier.texts()) == MAX_ROUNDS + 1

    # After the window: the next failure opens a fresh episode, rounds reset.
    now[0] += RELOGIN_COOLDOWN_SECONDS + 1
    coordinator.handle_turn_failure(
        harness="pi", provider="kimi-coding", model="k3",
        worker="pi-worker-3", failure=KIMI_A,
    )
    wait_until(lambda: len(runner.calls) == MAX_ROUNDS + 1,
               what="fresh episode round 1")
    runner.release()
    wait_until(lambda: len(notifier.texts()) >= MAX_ROUNDS + 2,
               what="fresh episode code alert")
    assert f"1/{MAX_ROUNDS}" in notifier.texts()[MAX_ROUNDS + 1]  # rounds reset


def test_restore_reopens_broken_provider_exactly_one_round(tmp_path: Path) -> None:
    runner = FakeRunner([(ANNOUNCEMENT, None)])
    coordinator, store, notifier = make_coordinator(tmp_path, runner, premark=True)
    coordinator.resume_after_restore()
    wait_until(lambda: len(runner.calls) == 1, what="exactly one round")
    time.sleep(0.1)
    assert len(runner.calls) == 1
    runner.release()
    wait_until(lambda: len(notifier.texts()) == 2,
               what="code alert + recovery notice")
    assert f"1/{MAX_ROUNDS}" in notifier.texts()[0]
    assert "已恢复" in notifier.texts()[1]


def test_restore_with_live_credential_recovers_without_flow(tmp_path: Path) -> None:
    runner = FakeRunner([(ANNOUNCEMENT, None)])
    coordinator, store, notifier = make_coordinator(
        tmp_path, runner, auth=live_auth(), premark=True
    )
    coordinator.resume_after_restore()
    wait_until(lambda: len(notifier.texts()) == 1, what="recovery notice")
    assert "已恢复" in notifier.texts()[0]
    assert len(runner.calls) == 0
    assert all(m.status == "available" for m in _capabilities(store, "kimi-coding"))


def test_restore_without_marks_does_nothing(tmp_path: Path) -> None:
    runner = FakeRunner([])
    coordinator, _, notifier = make_coordinator(tmp_path, runner)
    coordinator.resume_after_restore()
    time.sleep(0.1)
    assert runner.calls == []
    assert notifier.texts() == []


def test_restore_and_failure_concurrent_start_exactly_one_helper(
    tmp_path: Path,
) -> None:
    """S1: the claim (create episode + set inflight) is one critical section.

    A worker failure racing daemon restore must not start a second helper for
    the same provider.  Both entries start at a barrier; whichever claims
    first sets inflight under the same lock the other then observes.
    """

    runner = FakeRunner([(ANNOUNCEMENT, None)])
    coordinator, _, notifier = make_coordinator(tmp_path, runner, premark=True)
    gate = threading.Barrier(2)

    def run_failure() -> None:
        gate.wait(timeout=5)
        coordinator.handle_turn_failure(
            harness="pi", provider="kimi-coding", model="k3",
            worker="pi-worker-1", failure=KIMI_A,
        )

    def run_restore() -> None:
        gate.wait(timeout=5)
        coordinator.resume_after_restore()

    threads = [
        threading.Thread(target=run_failure),
        threading.Thread(target=run_restore),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wait_until(lambda: len(runner.calls) == 1, what="exactly one helper round")
    time.sleep(0.1)
    assert len(runner.calls) == 1  # no second flow from the race


# --------------------------------------------------------------------- B 类


def test_b_failure_alerts_without_login(tmp_path: Path) -> None:
    runner = FakeRunner([])
    coordinator, store, notifier = make_coordinator(tmp_path, runner)
    coordinator.handle_turn_failure(
        harness="pi", provider="openai-codex", model="gpt-6-astra",
        worker="astra-agent-home", failure=CODEX_B,
    )
    wait_until(lambda: len(notifier.texts()) == 1, what="account alert")
    text = notifier.texts()[0]
    assert "重新登录无法解决" in text
    assert "astra-agent-home" in text
    assert "squire probe" in text
    assert runner.calls == []  # no login flow for account-side failures
    marks = [
        c
        for c in store.get("owner-key").runtime_capabilities
        if c.provider == "openai-codex"
    ]
    assert len(marks) == 1  # exactly the failing combo, not the whole provider
    assert marks[0].model == "gpt-6-astra"
    assert marks[0].reason == REASON_PROVIDER_AUTH_ACCOUNT

    # A repeat of the same combo adds no second alert.
    coordinator.handle_turn_failure(
        harness="pi", provider="openai-codex", model="gpt-6-astra",
        worker="another-worker", failure=CODEX_B,
    )
    time.sleep(0.1)
    assert len(notifier.texts()) == 1


def test_claude_b_failure_marks_combo_and_never_starts_login(
    tmp_path: Path,
) -> None:
    runner = FakeRunner([])
    coordinator, store, notifier = make_coordinator(tmp_path, runner)
    coordinator.handle_turn_failure(
        harness="claude", provider=None, model="sonnet",
        worker="claude-worker-1",
        failure="Failed to authenticate. API Error: 403 Request not allowed",
    )
    wait_until(lambda: len(notifier.texts()) == 1, what="claude account alert")
    text = notifier.texts()[0]
    assert "Failed to authenticate" in text  # cause text, not just a code
    assert "claude-worker-1" in text
    assert runner.calls == []  # claude has no startable device flow
    marks = [
        c
        for c in store.get("owner-key").runtime_capabilities
        if c.harness == "claude"
    ]
    assert len(marks) == 1
    assert marks[0].model == "sonnet"
    assert marks[0].reason == REASON_PROVIDER_AUTH_ACCOUNT


def test_recovery_preserves_account_mark(tmp_path: Path) -> None:
    """S3: an OAuth recovery must not clear a sticky account (B) mark."""

    runner = FakeRunner([(ANNOUNCEMENT, None)])
    coordinator, store, notifier = make_coordinator(tmp_path, runner)
    # The selected combo carries the 09-14 entitlement (B) mark.
    # Other catalog models may receive separate OAuth marks; this assertion
    # concerns preservation of this account mark, not the catalog size.
    store.set_runtime_capability(
        "owner-key",
        _cap(
            "pi", "openai-codex", "gpt-6-astra", "unavailable",
            REASON_PROVIDER_AUTH_ACCOUNT,
        ),
    )
    # An A-class OAuth failure on the same provider must not overwrite it.
    coordinator.handle_turn_failure(
        harness="pi", provider="openai-codex", model="gpt-6-astra",
        worker="pi-worker-1", failure=OPENAI_OAUTH_A,
    )
    wait_until(lambda: len(runner.calls) == 1, what="A round started")
    marks = _capabilities(store, "openai-codex")
    marked = [m for m in marks if m.harness == "pi" and m.model == "gpt-6-astra"]
    assert len(marked) == 1
    assert marked[0].reason == REASON_PROVIDER_AUTH_ACCOUNT  # B preserved

    runner.release()  # relogin succeeds
    wait_until(
        lambda: any("已恢复" in t for t in notifier.texts()),
        what="recovery notice",
    )
    marks = _capabilities(store, "openai-codex")
    marked = [m for m in marks if m.harness == "pi" and m.model == "gpt-6-astra"]
    assert len(marked) == 1
    assert marked[0].status == "unavailable"
    assert marked[0].reason == REASON_PROVIDER_AUTH_ACCOUNT  # still account-broken


def test_account_failure_realerts_after_probe_clears_mark(
    tmp_path: Path,
) -> None:
    """S4: a B episode key must not outlive the account mark it dedups on."""

    runner = FakeRunner([])
    coordinator, store, notifier = make_coordinator(tmp_path, runner)
    coordinator.handle_turn_failure(
        harness="pi", provider="openai-codex", model="gpt-6-astra",
        worker="astra-agent-home", failure=CODEX_B,
    )
    wait_until(lambda: len(notifier.texts()) == 1, what="first account alert")

    # The operator fixes the account; `squire probe` rewrites the combo available.
    store.set_runtime_capability(
        "owner-key",
        _cap("pi", "openai-codex", "gpt-6-astra", "available"),
    )
    # The same failure is now fresh again: re-alert and re-mark.
    coordinator.handle_turn_failure(
        harness="pi", provider="openai-codex", model="gpt-6-astra",
        worker="astra-agent-home", failure=CODEX_B,
    )
    wait_until(lambda: len(notifier.texts()) == 2, what="second account alert")
    marks = [
        c
        for c in store.get("owner-key").runtime_capabilities
        if c.provider == "openai-codex"
    ]
    assert len(marks) == 1
    assert marks[0].status == "unavailable"
    assert marks[0].reason == REASON_PROVIDER_AUTH_ACCOUNT


# --------------------------------------------------------------------- C 类


def test_c_failure_does_nothing(tmp_path: Path) -> None:
    runner = FakeRunner([])
    coordinator, store, notifier = make_coordinator(tmp_path, runner)
    before = store.path.read_bytes()
    coordinator.handle_turn_failure(
        harness="pi", provider="kimi-coding", model="k3",
        worker="pi-worker-1", failure="fetch failed",
    )
    time.sleep(0.1)
    assert runner.calls == []
    assert notifier.texts() == []
    assert store.path.read_bytes() == before


# ---------------------------------------------------------------- 契约:不抛


def test_exploding_boundaries_never_propagate(tmp_path: Path) -> None:
    def boom_notifier(text, *, idempotency_key):
        raise RuntimeError("lark down")

    def boom_runner(provider, on_device_code, *, stop=None):
        raise RuntimeError("spawn broke")

    coordinator, _, _ = make_coordinator(
        tmp_path, FakeRunner([]), notifier=FakeNotifier()
    )
    # Swap in the exploding boundaries after construction.
    coordinator._notifier = boom_notifier
    coordinator._runner = boom_runner
    # Must not raise even though both boundaries explode.
    coordinator.handle_turn_failure(
        harness="pi", provider="kimi-coding", model="k3",
        worker="pi-worker-1", failure=KIMI_A,
    )
    coordinator.resume_after_restore()
    wait_until(lambda: len(coordinator._episodes) >= 0, what="no crash")
