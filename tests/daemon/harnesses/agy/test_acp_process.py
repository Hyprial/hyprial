"""Tests for AgyAcpProcess integration with StreamingTurnProcess."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hyprial.daemon.impl.api import HarnessResultStatus
from hyprial.daemon.impl.harnesses.agy.acp_client import AgyAcpClient
from hyprial.daemon.impl.harnesses.agy.native_env import AgyAgentHomeError
from hyprial.daemon.impl.harnesses.agy.process import AgyAcpProcess
from hyprial.daemon.impl.harnesses.agy.protocol import AgyStartPreconditionError
from hyprial.kernel import AdmissionResult
from hyprial.identity import ChildEnvironmentLaunch
from hyprial.identity.impl.agents.home.environment import CompleteChildEnvironment
from tests.daemon.harnesses.agy.test_native_env import _make_context
from tests.daemon.harnesses.agy.conftest import (
    agy_delivery,
    agy_spec,
    fake_acp_command,
)


def _wait_results(process: AgyAcpProcess, count: int, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    results = []
    while time.monotonic() < deadline:
        results.extend(process.drain_results())
        if len(results) >= count:
            return results
        time.sleep(0.01)
    pytest.fail(f"AgyAcpProcess produced {len(results)}/{count} results")


def test_process_completes_turn_records_progress_tools_and_facts(tmp_path: Path) -> None:
    completed_calls = []
    spec = agy_spec(cwd=str(tmp_path), model="gemini-3.8-flash")
    process = AgyAcpProcess(
        spec,
        command=fake_acp_command(),
        env={"FAKE_ACP_MODE": "normal"},
        on_turn_completed=lambda delivery, result, **kw: completed_calls.append(
            (delivery.delivery_id, result.status, kw["tool_names"])
        ),
        reconnect_delay_seconds=0.01,
    )
    try:
        assert process.wait_ready(timeout=5.0) is True
        assert process.read_run_facts().running is True
        assert process.enqueue(agy_delivery("d-1", "hello process")) is True
        results = _wait_results(process, 1)
        assert results[0].status is HarnessResultStatus.COMPLETED
        assert "echo:" in (results[0].output or "")
        assert "hello process" in (results[0].output or "")
        progress = process.drain_progress()
        assert len(progress) >= 3
        assert completed_calls[0][0] == "d-1"
        assert completed_calls[0][1] is HarnessResultStatus.COMPLETED
        assert "mcp_harness-bridge_harness_whoami" in completed_calls[0][2]
    finally:
        process.stop()


def test_process_auth_failure_reports_to_failure_observer(tmp_path: Path) -> None:
    observed_failures = []
    spec = agy_spec(cwd=str(tmp_path), model_provider="google", model="gemini-3.8-flash")
    process = AgyAcpProcess(
        spec,
        command=fake_acp_command(),
        env={"FAKE_ACP_MODE": "require_api_key_in_prompt", "GEMINI_API_KEY": "bad-key"},
        on_turn_failure_for_spec=lambda failure, **kw: (
            observed_failures.append((failure, kw["harness"], kw["provider"], kw["worker"])),
            AdmissionResult.ACCEPTED,
        )[1],
        reconnect_delay_seconds=0.01,
    )
    try:
        assert process.wait_ready(timeout=5.0) is True
        assert process.enqueue(agy_delivery("d-auth", "ping")) is True
        results = _wait_results(process, 1)
        assert results[0].status is HarnessResultStatus.FAILED
        assert results[0].failure_code == "PROVIDER_AUTHENTICATION_FAILED"
    finally:
        process.stop()
    assert observed_failures
    assert observed_failures[0][1] == "agy"
    assert observed_failures[0][2] == "google"


def test_process_reconnects_after_mid_turn_crash_and_resumes_session(tmp_path: Path) -> None:
    state_file = tmp_path / "crash.marker"
    log_path = tmp_path / "reconnect.log"
    spec = agy_spec(cwd=str(tmp_path))
    process = AgyAcpProcess(
        spec,
        command=fake_acp_command(),
        env={
            "FAKE_ACP_MODE": "crash_mid_turn_once",
            "FAKE_ACP_STATE": str(state_file),
            "FAKE_ACP_LOG": str(log_path),
        },
        reconnect_delay_seconds=0.01,
    )
    try:
        assert process.wait_ready(timeout=5.0) is True
        assert process.enqueue(agy_delivery("d-crash", "retry me")) is True
        results = _wait_results(process, 1, timeout=8.0)
        assert results[0].status is HarnessResultStatus.COMPLETED
        assert "retry me" in (results[0].output or "")
        assert process.session_ref == "known-session-1"
    finally:
        process.stop()


def test_process_validates_spec_and_provider_preconditions(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="managed headless"):
        AgyAcpProcess(
            agy_spec(cwd=str(tmp_path)).__class__(
                harness="pi", name="wrong", headless=True
            )
        )
    with pytest.raises(AgyStartPreconditionError):
        AgyAcpProcess(
            agy_spec(cwd=str(tmp_path), model_provider="anthropic"),
            command=fake_acp_command(),
        )
    with pytest.raises(AgyStartPreconditionError):
        AgyAcpProcess(
            agy_spec(cwd=str(tmp_path), args=("--mode", "auto")),
            command=fake_acp_command(),
        )


def test_process_read_run_activity_delegates_to_client(tmp_path: Path) -> None:
    spec = agy_spec(cwd=str(tmp_path))
    process = AgyAcpProcess(
        spec,
        client_factory=lambda: AgyAcpClient(
            spec,
            command=fake_acp_command(),
            env={"FAKE_ACP_MODE": "normal"},
        ),
        reconnect_delay_seconds=0.01,
    )
    try:
        assert process.wait_ready(timeout=5.0) is True
        assert process.read_run_activity() is None
    finally:
        process.stop()


def test_complete_launch_runtime_context_projects_and_reconnects(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    token = context.roots.native_root / "antigravity-oauth-token"
    token.write_bytes(b"oauth-token")
    launch = ChildEnvironmentLaunch(
        CompleteChildEnvironment((("HOME", str(context.roots.tool_home)),)),
        actor=context.actor,
        runtime_context=context,
        agent_home_profile_applied=True,
    )
    spec = agy_spec(cwd=str(tmp_path), session_ref="known-session-1")
    client = AgyAcpClient(spec, command=fake_acp_command(), complete_launch=launch)
    assert str(context.roots.session_root) in client.command
    process = AgyAcpProcess(spec, command=fake_acp_command(), complete_launch=launch)
    try:
        rebuilt = process._build_client()
        assert rebuilt._complete_launch is launch
        assert str(context.roots.session_root) in rebuilt.command
        assert (context.roots.session_root / "antigravity-cli" / "antigravity-oauth-token").read_bytes() == b"oauth-token"
    finally:
        process.stop()
    token.unlink()
    from hyprial.daemon.impl.harnesses.agy.native_env import prepare_agy_runtime_context

    prepare_agy_runtime_context(context)
    assert not (context.roots.session_root / "antigravity-cli" / "antigravity-oauth-token").exists()


def test_complete_launch_runtime_context_gate_rejects_invalid_handoffs(tmp_path: Path) -> None:
    context = _make_context(tmp_path)
    launch = ChildEnvironmentLaunch(
        CompleteChildEnvironment((("HOME", str(context.roots.tool_home)),)),
        actor=context.actor,
        runtime_context=context,
        agent_home_profile_applied=True,
    )
    spec = agy_spec(cwd=str(tmp_path))
    with pytest.raises(AgyAgentHomeError, match="actor"):
        AgyAcpClient(spec, command=fake_acp_command(), complete_launch=ChildEnvironmentLaunch(
            launch.environment, actor="other", runtime_context=context, agent_home_profile_applied=True
        ))
    with pytest.raises(AgyAgentHomeError, match="disagrees"):
        AgyAcpClient(spec, command=fake_acp_command(), complete_launch=ChildEnvironmentLaunch(
            CompleteChildEnvironment((("HOME", str(tmp_path / "wrong")),)), actor=context.actor,
            runtime_context=context, agent_home_profile_applied=True
        ))
    with pytest.raises(AgyAgentHomeError, match="requires an AgentRuntimeContext"):
        AgyAcpClient(spec, command=fake_acp_command(), complete_launch=ChildEnvironmentLaunch(
            launch.environment, actor=context.actor, agent_home_profile_applied=True
        ))
    with pytest.raises(AgyAgentHomeError, match="disagree"):
        AgyAcpClient(
            spec,
            command=fake_acp_command(),
            complete_launch=launch,
            worker_channel=type("Channel", (), {"runtime_context": object()})(),
        )
    with pytest.raises(AgyAgentHomeError, match="requires complete_launch"):
        AgyAcpClient(
            spec,
            command=fake_acp_command(),
            worker_channel=type("Channel", (), {"runtime_context": context})(),
        )
