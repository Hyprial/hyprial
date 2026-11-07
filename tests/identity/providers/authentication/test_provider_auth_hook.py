"""Provider-auth observer fires through the real pump's FAILED result path.

Review round 2 B1: the observer was only wired on the pump's exception / 断连
branch, but pi ``stopReason=error`` and claude-sdk ``isError`` are normal
FAILED results that never raise -- so in the exact incident the spec exists to
handle, the feature never triggered.  These tests run the REAL
``StreamingTurnProcess`` (via ``PiRpcProcess`` for the pi case) with a real
``ProviderAuthCoordinator`` as the observer, and assert the full effect:
marking + exactly one helper round / one notice.

The mutation that moves the hook back to the exception-only branch makes
``test_pi_stop_reason_error_triggers_observer`` and
``test_claude_is_error_triggers_observer`` both red.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hyprial.kernel.impl.actor_runtime import AdmissionResult
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses import agent_sdk as agent_sdk_module
from hyprial.daemon.impl.harnesses.agent_sdk import ClaudeAgentSdkProcess
from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerProcess
from hyprial.daemon.impl.harnesses.pi.rpc_client import PiRpcClient
from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess
from hyprial.daemon.impl.harnesses.streaming.process import StreamingTurnProcess
from hyprial.daemon.impl.harnesses.worker_channel import WorkerChannel
from hyprial.identity.impl.provider_auth.actor._authority import ProviderAuthAuthority
from hyprial.identity.impl.provider_auth.helper import HelperOutcome
from hyprial.identity.impl.users.profile import UserProfileStore

from tests.daemon.harnesses.pi.rpc.process.support import FAKE_PI, delivery
from tests.identity.providers.authentication.test_provider_auth_coordinator import ANNOUNCEMENT, FakeRunner, _capabilities, make_coordinator, wait_until

CLAUDE_AUTH_ERROR = "Failed to authenticate. API Error: 403 Request not allowed"


def _worker_channel(tmp_path: Path, name: str, runtime_context: object) -> WorkerChannel:
    return WorkerChannel(
        actor=f"agent:owner:node:{name}",
        session_ref=f"session-{name}",
        hyprial_home=tmp_path / "home",
        state_dir=tmp_path / "state",
        mcp_server={},
        allowed_tools=(),
        runtime_context=runtime_context,
    )


def _pi_launch() -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness="pi",
        name="pi-worker",
        headless=True,
        session_ref="stable-session",
        model_provider="kimi-coding",
        model="k3",
    )


def _drain_until(process: StreamingTurnProcess) -> list[Any]:
    deadline = time.monotonic() + 5.0
    results: list[Any] = []
    while time.monotonic() < deadline:
        results.extend(process.drain_results())
        if results:
            return results
        time.sleep(0.01)
    raise AssertionError("pump produced no result")


def test_pi_stop_reason_error_triggers_observer(tmp_path: Path) -> None:
    """kimi 事故原文 as stopReason=error ⇒ observer called, marked, one flow."""

    runner = FakeRunner([(ANNOUNCEMENT, None)])  # stays inflight until release
    coordinator, store, notifier = make_coordinator(tmp_path, runner)
    launch = _pi_launch()
    process = PiRpcProcess(
        launch,
        client_factory=lambda: PiRpcClient(
            launch,
            "stable-session",
            command=(sys.executable, str(FAKE_PI)),
            env={"FAKE_PI_MODE": "provider-auth-error"},
            startup_timeout_seconds=10.0,
        ),
        on_turn_failure_for_spec=coordinator.handle_turn_failure,
        reconnect_delay_seconds=0.01,
    )
    try:
        assert process.enqueue(delivery("delivery-1", "do the work"))
        wait_until(lambda: len(runner.calls) == 1, what="one helper round")
        marks = _capabilities(store, "kimi-coding")
        assert marks, "no kimi-coding marks written"
        assert all(m.status == "unavailable" for m in marks)
        assert all(m.reason == "provider-auth-invalid" for m in marks)

        runner.release()  # code issued, then login completes
        wait_until(
            lambda: len(notifier.texts()) == 2,
            what="device-code alert + recovery notice",
        )
        text = notifier.texts()[0]
        assert "kimi-coding" in text
        assert "ABCD-EFGH" in text
        assert "https://auth.example.com/device" in text
        assert "pi-worker" in text
        assert len(runner.calls) == 1  # dedup: no second flow
        assert "已恢复" in notifier.texts()[1]
    finally:
        process.stop()


class _ClaudeAuthErrorClient:
    """claude-sdk shape: a result frame with is_error, no provider_error."""

    async def __aenter__(self) -> "_ClaudeAuthErrorClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def query(self, prompt: str) -> None:
        del prompt

    async def interrupt(self) -> None:
        return None

    async def receive_response(self) -> Any:
        class _Outcome:
            result = CLAUDE_AUTH_ERROR
            is_error = True
            # no provider_error attribute at all

        yield _Outcome()


def test_claude_is_error_triggers_observer(tmp_path: Path) -> None:
    """claude-sdk isError ⇒ observer called, B path alerts, no login flow."""

    runner = FakeRunner([])
    coordinator, store, notifier = make_coordinator(tmp_path, runner)

    def adapt(failure: str) -> None:
        coordinator.handle_turn_failure(
            harness="claude",
            provider=None,
            model="sonnet",
            worker="claude-worker-1",
            failure=failure,
        )

    process = StreamingTurnProcess(
        harness="claude",
        label="claude auth test",
        client_factory=_ClaudeAuthErrorClient,
        thread_name="test-claude-auth",
        on_turn_failure=adapt,
        reconnect_delay_seconds=0.01,
        reconnect_delay_max_seconds=0.05,
    )
    try:
        assert process.enqueue(delivery("delivery-claude", "busy work"))
        wait_until(lambda: len(notifier.texts()) == 1, what="one claude alert")
        text = notifier.texts()[0]
        assert "Failed to authenticate" in text  # cause text, not just a code
        assert "claude-worker-1" in text
        assert runner.calls == []  # B never starts a login flow
        marks = [
            c
            for c in store.get("owner-key").runtime_capabilities
            if c.harness == "claude"
        ]
        assert len(marks) == 1
        assert marks[0].model == "sonnet"
        assert marks[0].reason == "provider-auth-account"
    finally:
        process.stop()


def test_managed_codex_failed_result_preserves_spec_and_runtime_context(
    tmp_path: Path,
) -> None:
    runtime_context = object()
    observed = []
    completed = threading.Event()

    def observer(failure, **fields):
        observed.append((failure, fields))
        completed.set()
        return AdmissionResult.ACCEPTED

    spec = HarnessLaunchSpec(
        harness="codex",
        name="codex-worker",
        headless=True,
        model_provider="openai",
        model="gpt-5",
    )
    process = CodexAppServerProcess(
        spec,
        client_factory=_ClaudeAuthErrorClient,
        worker_channel=_worker_channel(
            tmp_path, "codex-worker", runtime_context
        ),
        on_turn_failure_for_spec=observer,
        reconnect_delay_seconds=0.01,
    )
    try:
        assert process.enqueue(delivery("codex-auth", "synthetic"))
        assert _drain_until(process)
        assert completed.wait(1.0)
        assert observed == [
            (
                CLAUDE_AUTH_ERROR,
                {
                    "harness": "codex",
                    "provider": "openai",
                    "model": "gpt-5",
                    "worker": "codex-worker",
                    "runtime_context": runtime_context,
                },
            )
        ]
    finally:
        process.stop()


def test_managed_claude_contexts_stay_route_scoped_without_global_marks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        agent_sdk_module,
        "_validated_claude_launch_environment",
        lambda *_args, **_kwargs: None,
    )
    runner = FakeRunner([])
    coordinator, store, notifier = make_coordinator(
        tmp_path,
        runner,
        runtime_helper_factory=lambda _context: runner,
        runtime_context_validator=lambda _context: True,
    )
    contexts = [
        SimpleNamespace(
            actor=f"agent:owner:node:claude-{index}",
            entity_token=f"entity-{index}",
            roots=SimpleNamespace(native_root=tmp_path / f"native-{index}"),
        )
        for index in (1, 2)
    ]
    processes = [
        ClaudeAgentSdkProcess(
            HarnessLaunchSpec(
                harness="claude",
                name=f"claude-{index}",
                headless=True,
                model_provider="anthropic",
                model="sonnet",
            ),
            client_factory=_ClaudeAuthErrorClient,
            worker_channel=_worker_channel(
                tmp_path, f"claude-{index}", contexts[index - 1]
            ),
            on_turn_failure_for_spec=coordinator.handle_turn_failure,
            reconnect_delay_seconds=0.01,
        )
        for index in (1, 2)
    ]
    try:
        for index, process in enumerate(processes, 1):
            assert process.enqueue(delivery(f"claude-auth-{index}", "synthetic"))
            assert _drain_until(process)
        wait_until(lambda: len(notifier.texts()) == 2, what="two route notices")
        assert _capabilities(store, "anthropic") == []
        episode_keys = frozenset(coordinator._episodes)
        assert any("agent:owner:node:claude-1:entity-1" in key for key in episode_keys)
        assert any("agent:owner:node:claude-2:entity-2" in key for key in episode_keys)
    finally:
        for process in processes:
            process.stop()


class _SuccessEchoClient:
    """A SUCCESSFUL reply whose text happens to contain the account marker."""

    async def __aenter__(self) -> "_SuccessEchoClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def query(self, prompt: str) -> None:
        del prompt

    async def interrupt(self) -> None:
        return None

    async def receive_response(self) -> Any:
        class _Outcome:
            result = (
                "The model replied: 'Failed to authenticate. API Error: "
                "403 Request not allowed' is what the vendor would say."
            )
            is_error = False

        yield _Outcome()


def test_model_output_does_not_trigger_observer(tmp_path: Path) -> None:
    """A COMPLETED result whose text holds the marker must not feed the hook.

    Only FAILED results carry provider/transport error text; a successful
    assistant reply is never classified (review round 2 B1 follow-up).
    """

    runner = FakeRunner([])
    coordinator, _, notifier = make_coordinator(tmp_path, runner)
    calls: list[str] = []

    def adapt(failure: str) -> None:
        calls.append(failure)

    process = StreamingTurnProcess(
        harness="claude",
        label="claude echo test",
        client_factory=_SuccessEchoClient,
        thread_name="test-claude-echo",
        on_turn_failure=adapt,
        reconnect_delay_seconds=0.01,
        reconnect_delay_max_seconds=0.05,
    )
    try:
        assert process.enqueue(delivery("delivery-echo", "echo the phrase"))
        _drain_until(process)  # the COMPLETED result arrived
        time.sleep(0.1)  # any (wrong) observer call would have happened by now
        assert calls == []
        assert notifier.texts() == []
        assert runner.calls == []
    finally:
        process.stop()


def test_blocked_observer_does_not_stall_turn_completion() -> None:
    """A hung observer must not decide the turn's latency (review r2 final).

    The observer is fire-and-forget on a dedicated thread; while it is blocked
    here, the pump must still complete the turn.  The mutation that awaits the
    observer makes this red: the result never drains before the 5s deadline.
    """

    release = threading.Event()
    calls: list[str] = []

    def blocking_observer(failure: str) -> None:
        calls.append(failure)
        release.wait(timeout=30)  # simulate a hung Lark HTTP call

    process = StreamingTurnProcess(
        harness="claude",
        label="blocked observer test",
        client_factory=_ClaudeAuthErrorClient,
        thread_name="test-blocked-observer",
        on_turn_failure=blocking_observer,
        reconnect_delay_seconds=0.01,
        reconnect_delay_max_seconds=0.05,
    )
    try:
        assert process.enqueue(delivery("delivery-blocked", "busy work"))
        results = _drain_until(process)  # completes while the observer blocks
        assert results, "turn did not complete while the observer was blocked"
        assert len(calls) == 1
    finally:
        release.set()  # unblock the observer so its drain thread can exit
        process.stop()


def test_auth_overload_retains_observation_without_blocking_turn_result():
    from hyprial.kernel.impl.actor_runtime import AdmissionResult

    overloaded, admitted = threading.Event(), threading.Event()
    release = threading.Event()
    calls = []

    def observer(failure):
        calls.append(failure)
        if not release.is_set():
            overloaded.set()
            return AdmissionResult.OVERLOADED
        admitted.set()
        return AdmissionResult.ACCEPTED

    process = StreamingTurnProcess(
        harness="claude", label="auth admission retry",
        client_factory=_ClaudeAuthErrorClient,
        thread_name="test-auth-admission-retry",
        on_turn_failure=observer,
        reconnect_delay_seconds=0.01,
    )
    try:
        assert process.enqueue(delivery("retry-observation", "synthetic"))
        assert overloaded.wait(1)
        results = _drain_until(process)
        assert results and not admitted.is_set()
        release.set()
        assert admitted.wait(2)
        assert len(calls) >= 2
        assert set(calls) == {CLAUDE_AUTH_ERROR}
    finally:
        release.set()
        process.stop()


def test_production_auth_overload_settles_before_shutdown_closes_owner(
    tmp_path: Path,
) -> None:
    store = UserProfileStore(tmp_path / "users.json")
    store.ensure(
        owner="owner",
        owner_key="owner-key",
        login_name="owner",
        machine="node",
        machine_key="node-key",
    )
    authority = ProviderAuthAuthority(
        profile_store=store,
        owner_key="owner-key",
        notifier=lambda *_args, **_kwargs: SimpleNamespace(delivered=True),
        helper_runner=lambda *_args, **_kwargs: HelperOutcome.STOPPED,
        auth_path=tmp_path / "auth.json",
        host="node",
        stop=threading.Event(),
        observation_capacity=1,
        notification_retry_seconds=0.01,
    )
    decision_entered = threading.Event()
    release_decision = threading.Event()
    original_handle = authority._core.handle_turn_failure
    decision_calls = 0

    def blocked_handle(*args, **kwargs):
        nonlocal decision_calls
        decision_calls += 1
        if decision_calls == 1:
            decision_entered.set()
            assert release_decision.wait(2.0)
        return original_handle(*args, **kwargs)

    authority._core.handle_turn_failure = blocked_handle
    first_failure = "transient network failure outside provider-auth policy"
    second_failure = "OAuth refresh failed for openai-codex: invalid_grant"
    assert authority.handle_turn_failure(
        first_failure,
        harness="pi",
        provider="kimi-coding",
        model="k3",
        worker="first",
    ) is AdmissionResult.ACCEPTED
    assert decision_entered.wait(1.0)

    admissions = []
    accepted = threading.Event()

    def observe(failure):
        result = authority.handle_turn_failure(
            failure,
            harness="pi",
            provider="openai-codex",
            model="gpt",
            worker="second",
        )
        admissions.append(result)
        if result is AdmissionResult.ACCEPTED:
            accepted.set()
        return result

    process = StreamingTurnProcess(
        harness="pi",
        label="production auth shutdown",
        client_factory=_ClaudeAuthErrorClient,
        thread_name="test-production-auth-shutdown",
        on_turn_failure=observe,
        stop_timeout_seconds=1.0,
    )
    process._dispatch_turn_failure(second_failure)
    wait_until(
        lambda: AdmissionResult.OVERLOADED in admissions,
        what="typed observer overload",
    )
    order = []

    class Harnesses:
        drain_complete = True

        @staticmethod
        def stop():
            order.append("harness.stop")
            process.stop()

    class AgentDomains:
        @staticmethod
        def close():
            order.append("agent.close")
            return True

    class Finalizer:
        @staticmethod
        def detach():
            order.append("finalizer.detach")

    application = object.__new__(DaemonApplication)
    application._harnesses = Harnesses()
    application._provider_auth = authority
    application._agent_session_domains = AgentDomains()
    application._agent_domains_finalizer = Finalizer()
    failures = []

    def close_application() -> None:
        try:
            application._close_harnesses_before_agent_domains()
        except BaseException as error:
            failures.append(error)

    closing = threading.Thread(target=close_application, daemon=True)
    closing.start()
    time.sleep(0.05)
    assert closing.is_alive()
    release_decision.set()
    closing.join(3.0)

    assert not closing.is_alive()
    assert failures == []
    assert accepted.is_set()
    assert admissions.count(AdmissionResult.ACCEPTED) == 1
    assert AdmissionResult.CLOSED not in admissions
    assert application._provider_auth is None
    assert order == ["harness.stop", "agent.close", "finalizer.detach"]


def test_full_observer_queue_drains_and_exits_after_process_stop(monkeypatch):
    from hyprial.daemon.impl.harnesses.streaming import process as streaming

    monkeypatch.setattr(streaming, "_TURN_FAILURE_OBSERVER_QUEUE_MAX", 1)
    entered, release = threading.Event(), threading.Event()
    seen = []

    def observer(failure):
        seen.append(failure)
        if failure == "first":
            entered.set()
            assert release.wait(3)

    process = StreamingTurnProcess(
        harness="claude", label="observer shutdown",
        client_factory=_ClaudeAuthErrorClient,
        thread_name="test-observer-shutdown",
        on_turn_failure=observer,
    )
    try:
        process._dispatch_turn_failure("first")
        assert entered.wait(1)
        process._dispatch_turn_failure("second")
        assert process._observer_queue.full()
        with pytest.raises(RuntimeError, match="failure observer did not drain"):
            process.stop()
        assert process._observer_thread.is_alive()
        process._dispatch_turn_failure("after-close")
        release.set()
        process.stop()
        assert not process._observer_thread.is_alive()
        assert seen == ["first", "second"]
    finally:
        release.set()
        process.stop()
        if process._observer_thread.is_alive():
            process._observer_queue.put(streaming._OBSERVER_STOP, timeout=1)
            process._observer_thread.join(1)
