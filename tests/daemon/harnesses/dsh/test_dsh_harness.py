from __future__ import annotations

import asyncio
from pathlib import Path
import time
from typing import Any

import pytest

from hyprial.daemon.impl.api import HarnessDelivery, HarnessResultStatus
from hyprial.identity.impl.agents.home.environment import (
    ChildEnvironmentLaunch,
    CompleteChildEnvironment,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.dsh import DshApiClient, DshApiError, DshHarnessProcess
from hyprial.daemon.impl.harnesses.runtime.launcher import HarnessLauncher, is_streaming_spec
from hyprial.daemon.impl.harnesses.worker_channel import WorkerChannel, build_worker_channel


def _spec(*, args: tuple[str, ...] = ()) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness="dsh",
        name="dsh-worker",
        headless=True,
        args=args,
        cwd="/workspace/project",
    )


class FakeDshApi:
    def __init__(self, dsh_home: Path | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.histories: list[object] = []
        self.running: list[bool] = []
        self.dsh_home = dsh_home

    async def call(self, method: str, payload: dict[str, object]) -> object:
        self.calls.append((method, payload))
        if method == "host.describe":
            return {"version": "0.0.1"}
        if method == "session.create":
            return {"sessionId": "session-created", "agentPreset": "standard"}
        if method == "agentPreset.copy":
            if self.dsh_home is None:
                raise AssertionError("fake DSH_HOME is required for preset copy")
            preset_id = payload["agentPreset"]
            assert isinstance(preset_id, str)
            directory = self.dsh_home / ".agent-presets" / preset_id
            directory.mkdir(parents=True)
            (directory / "agent.cordis.yml").write_text(
                "- id: persona\n  name: '@deepseek-ai/dsh-persona'\n",
                encoding="utf-8",
            )
            return {"agentPreset": preset_id}
        if method == "agentPreset.remove":
            return {}
        if method == "session.prompt":
            return {"accepted": True}
        if method == "session.cancel":
            return {"accepted": True}
        if method == "session.history":
            return self.histories.pop(0) if self.histories else {"events": []}
        if method == "session.list":
            running = self.running.pop(0) if self.running else True
            return {"items": [{"sessionId": "session-created", "running": running}]}
        raise AssertionError(f"unexpected DSH method {method}")


def _history(*events: dict[str, Any]) -> dict[str, object]:
    return {"events": [{"event": event} for event in events], "hasMore": False}


def _channel(tmp_path: Path) -> WorkerChannel:
    return build_worker_channel(
        actor="agent:allen:node-1:dsh-worker",
        session_ref="worker-session-ref",
        hyprial_home=tmp_path / "hyprial-home",
        state_dir=tmp_path / "hyprial-state",
        python_executable="/opt/hyprial/python",
    )


def test_client_creates_session_queues_prompt_and_observes_completion() -> None:
    async def scenario() -> None:
        api = FakeDshApi()
        api.histories = [
            _history({"type": "turn/end", "seq": 10, "data": {}}),
            _history(
                {"type": "turn/start", "seq": 11, "data": {"turn": 2}},
                {
                    "type": "assistant/message",
                    "seq": 12,
                    "data": {
                        "turn": 2,
                        "message": {
                            "content": [
                                {"type": "reasoning", "text": "hidden"},
                                {"type": "text", "text": "DSH final answer"},
                            ]
                        },
                    },
                },
                {
                    "type": "turn/end",
                    "seq": 13,
                    "data": {"turn": 2, "reason": {"kind": "completed"}},
                },
            ),
        ]
        client = DshApiClient(_spec(), api=api, poll_interval_seconds=0.001)

        async with client:
            assert client.running
            assert client.session_id == "session-created"
            await client.query("injected from HYPRIAL")
            outcomes = [outcome async for outcome in client.receive_response()]

        assert outcomes[0].result == "DSH final answer"
        assert outcomes[0].is_error is False
        assert (
            "session.create",
            {
                "agentPreset": "standard",
                "cwd": "/workspace/project",
            },
        ) in api.calls
        assert (
            "session.prompt",
            {
                "sessionId": "session-created",
                "mode": "queue",
                "content": [{"type": "text", "text": "injected from HYPRIAL"}],
            },
        ) in api.calls
        assert not client.running

    asyncio.run(scenario())


def test_client_resumes_explicit_session_and_interrupts_active_turn() -> None:
    async def scenario() -> None:
        api = FakeDshApi()
        # Enter probe, then query baseline.
        api.histories = [_history(), _history()]
        client = DshApiClient(_spec(args=("--session-id", "session-existing")), api=api)

        async with client:
            await client.query("please wait")
            await client.interrupt()

        assert client.session_id == "session-existing"
        assert not any(method == "session.create" for method, _ in api.calls)
        assert ("session.cancel", {"sessionId": "session-existing"}) in api.calls

    asyncio.run(scenario())


def test_noncompleted_turn_becomes_error_outcome() -> None:
    async def scenario() -> None:
        api = FakeDshApi()
        api.histories = [
            _history(),
            _history(
                {
                    "type": "turn/end",
                    "seq": 2,
                    "data": {"reason": {"kind": "cancelled"}},
                }
            ),
        ]
        client = DshApiClient(_spec(), api=api)
        async with client:
            await client.query("cancel me")
            outcomes = [outcome async for outcome in client.receive_response()]
        assert outcomes[0].is_error is True
        assert "cancelled" in outcomes[0].result

    asyncio.run(scenario())


def test_error_turn_preserves_dsh_code_and_message_in_failure_and_worker_log() -> None:
    secret = "provider-sentinel-must-not-leak"
    api = FakeDshApi()
    api.histories = [
        _history(),
        _history(
            {
                "type": "turn/end",
                "seq": 2,
                "data": {
                    "reason": {
                        "kind": "error",
                        "error": {
                            "code": "INVALID_REQUEST",
                            "message": "selected model is unavailable",
                            "detail": secret + ("x" * 5000),
                            "status": 400,
                        },
                    }
                },
            }
        ),
    ]
    logged: list[tuple[str, dict[str, object]]] = []

    class CapturingLogger:
        def info(self, event: str, **fields: object) -> None:
            logged.append((event, fields))

    process = DshHarnessProcess(
        _spec(),
        client_factory=lambda: DshApiClient(
            _spec(),
            api=api,
            poll_interval_seconds=0.001,
            redact_secrets=lambda text: text.replace(secret, "[REDACTED]"),
        ),
    )
    process._logger = CapturingLogger()  # type: ignore[assignment]
    try:
        assert process.wait_ready(timeout=2)
        assert process.enqueue(
            HarnessDelivery(
                delivery_id="dsh-detail",
                conversation_id="dsh-detail",
                sender="agent:test:sender",
                recipient="agent:test:dsh",
                message="exercise the DSH error detail",
            )
        )
        deadline = time.monotonic() + 2
        result = None
        while result is None and time.monotonic() < deadline:
            drained = process.drain_results()
            result = drained[0] if drained else None
            time.sleep(0.005)
        assert result is not None
        assert result.status is HarnessResultStatus.FAILED
        assert result.failure_code == "HARNESS_TRANSIENT_FAILURE"
        assert "INVALID_REQUEST" in (result.error or "")
        assert "selected model is unavailable" in (result.error or "")
        assert secret not in (result.error or "")
        assert "[REDACTED]" in (result.error or "")
        assert len(result.error or "") <= 4096
        failure = next(
            fields["failure"]
            for event, fields in logged
            if event == "worker.turn.failed"
        )
        assert "INVALID_REQUEST" in str(failure)
        assert "selected model is unavailable" in str(failure)
    finally:
        process.stop()


def test_launcher_dispatches_native_dsh_api_process() -> None:
    launched: list[HarnessLaunchSpec] = []

    class Ready:
        def wait_ready(self, *, timeout: float | None = None) -> bool:
            assert timeout is not None and timeout > 0
            return True

        def stop(self) -> None:
            pass

    sentinel = Ready()
    launcher = HarnessLauncher(
        dsh_api_factory=lambda spec: launched.append(spec) or sentinel  # type: ignore[arg-type,return-value]
    )
    spec = _spec()

    assert is_streaming_spec(spec)
    assert launcher.start(spec) is sentinel
    assert launched == [spec]


def test_process_requires_headless_dsh_spec() -> None:
    spec = HarnessLaunchSpec(harness="dsh", name="worker", headless=False)
    try:
        DshHarnessProcess(spec)
    except ValueError as error:
        assert "headless dsh" in str(error)
    else:
        raise AssertionError("invalid DSH spec was accepted")


def test_worker_channel_creates_session_scoped_mcp_preset(tmp_path: Path) -> None:
    async def scenario() -> None:
        api = FakeDshApi(tmp_path / "dsh-home")
        spec = _spec()
        client = DshApiClient(
            spec,
            api=api,
            worker_channel=_channel(tmp_path),
            dsh_home=tmp_path / "dsh-home",
        )

        async with client:
            assert client.worker_preset is not None
            preset = client.worker_preset
            assert preset.preset_id.startswith("hyprial-")
            assert preset.server_name == preset.preset_id

        copied = next(payload for method, payload in api.calls if method == "agentPreset.copy")
        assert copied["from"] == "standard"
        created = next(payload for method, payload in api.calls if method == "session.create")
        assert created["agentPreset"] == preset.preset_id
        text = preset.path.read_text(encoding="utf-8")
        assert "@deepseek-ai/dsh-mcp-client" in text
        assert f"serverName: {preset.server_name}" in text
        assert "transport: stdio" in text
        assert "command: /opt/hyprial/python" in text
        assert "- hyprial.daemon.impl.mcp.entry" in text
        assert "- agent-channel" in text
        assert "agent:allen:node-1:dsh-worker" in text
        assert "worker-session-ref" in text
        assert "failOnStartupError: true" in text
        assert "allowedTools" not in text  # DSH publishes MCP tools natively.
        assert preset.path.stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())


def test_worker_mcp_injection_requires_worker_home(tmp_path: Path) -> None:
    async def scenario() -> None:
        api = FakeDshApi(tmp_path / "dsh-home")
        client = DshApiClient(_spec(), api=api, worker_channel=_channel(tmp_path))

        try:
            await client.__aenter__()
        except DshApiError as error:
            assert "DSH_HOME" in str(error)
        else:
            raise AssertionError("worker MCP injection ran without a private home")
        assert not any(method == "agentPreset.copy" for method, _ in api.calls)

    asyncio.run(scenario())


def test_worker_mcp_identity_refuses_stale_session_resume(tmp_path: Path) -> None:
    async def scenario() -> None:
        api = FakeDshApi(tmp_path / "dsh-home")
        spec = _spec(
            args=(
                "--session-id",
                "old-dsh-session",
            )
        )
        client = DshApiClient(
            spec,
            api=api,
            worker_channel=_channel(tmp_path),
            dsh_home=tmp_path / "dsh-home",
        )

        try:
            await client.__aenter__()
        except DshApiError as error:
            assert "fresh worker MCP identity" in str(error)
        else:
            raise AssertionError("stale DSH session inherited a fresh worker identity")
        assert not any(method == "agentPreset.copy" for method, _ in api.calls)

    asyncio.run(scenario())


def test_default_launcher_mints_one_channel_and_passes_complete_launch_to_dsh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = _channel(tmp_path)
    captured: dict[str, object] = {}
    minted: list[HarnessLaunchSpec] = []
    composed: list[tuple[HarnessLaunchSpec, WorkerChannel]] = []

    launch = ChildEnvironmentLaunch(
        environment=CompleteChildEnvironment(
            (
                ("PATH", "/usr/bin:/bin"),
                ("HOME", str(tmp_path / "agent-home")),
                ("DEEPSEEK_API_KEY", "SENTINEL-GRANTED-KEY"),
            )
        ),
        actor=channel.actor,
        grants=(("deepseek-r1", 1),),
    )

    def mint(spec: HarnessLaunchSpec) -> WorkerChannel:
        minted.append(spec)
        return channel

    def compose(
        spec: HarnessLaunchSpec, worker_channel: WorkerChannel
    ) -> ChildEnvironmentLaunch:
        composed.append((spec, worker_channel))
        return launch

    class FakeDsh:
        def __init__(self, spec: HarnessLaunchSpec, **kwargs: object) -> None:
            captured["spec"] = spec
            captured.update(kwargs)

        def wait_ready(self, *, timeout: float | None = None) -> bool:
            captured["timeout"] = timeout
            return True

        def stop(self) -> None:
            captured["stopped"] = True

    monkeypatch.setattr("hyprial.daemon.impl.harnesses.runtime.launcher.DshHarnessProcess", FakeDsh)
    spec = _spec()
    process = HarnessLauncher(
        worker_channel_factory=mint,
        child_environment_factory=compose,
        env={"LEGACY": "must-not-be-passed"},
        state_dir=tmp_path,
    ).start(spec)
    assert isinstance(process, FakeDsh)
    assert minted == [spec]
    assert composed == [(spec, channel)]
    assert captured["worker_channel"] is channel
    assert captured["state_dir"] == tmp_path
    assert captured["env"] is None
    assert captured["complete_launch"] is launch
    assert isinstance(captured["timeout"], float) and captured["timeout"] > 0


# The tail DSH (@deepseek-ai/dsh 0.1.0-rc.7) leaves when session.cancel lands
# as it opens a new step after tool results: the step ends, the session goes
# idle (session.list running=false), and turn/end is never written.  Observed
# 3 of 13 live interrupts, always this exact tail; nightly E2E-014 hit it.
_STALLED_TAIL = (
    {"type": "turn/start", "seq": 2, "data": {"turn": 1}},
    {"type": "tool/result", "seq": 3, "data": {}},
    {"type": "step/end", "seq": 4, "data": {}},
    {"type": "step/start", "seq": 5, "data": {}},
    {"type": "step/end", "seq": 6, "data": {}},
)


def test_cancelled_turn_that_goes_idle_without_turn_end_ends_once() -> None:
    async def scenario() -> None:
        api = FakeDshApi()
        # baseline, then the stalled tail on every later poll.
        api.histories = [_history()] + [_history(*_STALLED_TAIL)] * 6
        api.running = [False]
        client = DshApiClient(_spec(), api=api, poll_interval_seconds=0.001)

        async with client:
            await client.query("long task")
            await client.interrupt()
            outcomes = [outcome async for outcome in client.receive_response()]

        assert len(outcomes) == 1
        assert outcomes[0].is_error is True
        assert "without turn/end" in outcomes[0].result
        assert ("session.list", {}) in api.calls

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_running_session_after_cancel_keeps_waiting_for_turn_end() -> None:
    """Negative control: while DSH still reports the session running, the
    idle readout must not end the turn -- the real turn/end does."""

    async def scenario() -> None:
        api = FakeDshApi()
        api.histories = (
            [_history()]
            + [_history(*_STALLED_TAIL)] * 3
            + [
                _history(
                    *_STALLED_TAIL,
                    {
                        "type": "turn/end",
                        "seq": 7,
                        "data": {"reason": {"kind": "aborted"}},
                    },
                )
            ]
        )
        api.running = [True, True, True]
        client = DshApiClient(_spec(), api=api, poll_interval_seconds=0.001)

        async with client:
            await client.query("long task")
            await client.interrupt()
            outcomes = [outcome async for outcome in client.receive_response()]

        assert [o.result for o in outcomes] == ["DSH turn ended with reason aborted"]

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_idle_readout_racing_a_late_turn_end_never_ends_the_next_turn() -> None:
    """Ordering race: running=false is read, the turn is ended from it, and
    the cancelled turn's own turn/end lands afterwards.  That late turn/end
    must not settle the NEXT turn; the next turn ends on its own turn/end."""

    async def scenario() -> None:
        api = FakeDshApi()
        late_end = {"type": "turn/end", "seq": 7, "data": {"reason": {"kind": "aborted"}}}
        api.histories = [
            _history(),  # baseline of turn 1
            _history(*_STALLED_TAIL),  # poll: no turn/end
            _history(*_STALLED_TAIL),  # re-read after running=false: still none
            _history(late_end),  # baseline of turn 2 (maxMessages=1) -- if it lands in time
            _history(
                {"type": "turn/start", "seq": 8, "data": {"turn": 2}},
                {
                    "type": "assistant/message",
                    "seq": 9,
                    "data": {"message": {"content": [{"type": "text", "text": "second"}]}},
                },
                {"type": "turn/end", "seq": 10, "data": {"reason": {"kind": "completed"}}},
            ),
        ]
        api.running = [False]
        client = DshApiClient(_spec(), api=api, poll_interval_seconds=0.001)

        async with client:
            await client.query("long task")
            await client.interrupt()
            first = [o async for o in client.receive_response()]
            await client.query("next")
            second = [o async for o in client.receive_response()]

        assert len(first) == 1 and first[0].is_error is True
        assert [(o.result, o.is_error) for o in second] == [("second", False)]

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))


def test_late_turn_end_after_the_next_baseline_does_not_end_the_next_turn() -> None:
    """The harder half of the race: the stale turn/end lands AFTER turn 2's
    baseline was read, so its seq is above the baseline.  It precedes turn
    2's own turn/start and must be ignored."""

    async def scenario() -> None:
        api = FakeDshApi()
        api.histories = [
            _history(),
            _history(*_STALLED_TAIL),
            _history(*_STALLED_TAIL),
            _history(*_STALLED_TAIL),  # baseline of turn 2: max seq 6
            _history(
                {"type": "turn/end", "seq": 7, "data": {"reason": {"kind": "aborted"}}},
                {"type": "turn/start", "seq": 8, "data": {"turn": 2}},
                {
                    "type": "assistant/message",
                    "seq": 9,
                    "data": {"message": {"content": [{"type": "text", "text": "second"}]}},
                },
                {"type": "turn/end", "seq": 10, "data": {"reason": {"kind": "completed"}}},
            ),
        ]
        api.running = [False]
        client = DshApiClient(_spec(), api=api, poll_interval_seconds=0.001)

        async with client:
            await client.query("long task")
            await client.interrupt()
            first = [o async for o in client.receive_response()]
            await client.query("next")
            second = [o async for o in client.receive_response()]

        assert len(first) == 1 and first[0].is_error is True
        assert [(o.result, o.is_error) for o in second] == [("second", False)]

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
