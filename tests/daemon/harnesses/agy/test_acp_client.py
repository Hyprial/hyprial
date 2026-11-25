"""Tests for AgyAcpClient protocol, auth, resume/load, cancel races, permissions, and real MCP."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.agy.acp_client import (
    AgyAcpClient,
    AgySessionNotFoundError,
)
from hyprial.daemon.impl.harnesses.agy.projection import (
    allowed_harness_bridge_tool_names,
    select_permission_option,
    worker_channel_to_acp_mcp_server,
)
from hyprial.daemon.impl.harnesses.agy.protocol import (
    AgyStartPreconditionError,
    AgyTurnOutcome,
)
from hyprial.daemon.impl.harnesses.streaming.protocol import ProgressObservation
from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel
from hyprial.daemon.impl.inbox.tracking.progress import (
    PHASE_MESSAGE_SEGMENT,
    PHASE_THINKING,
    PHASE_TOOL_CALL,
    PHASE_TOOL_RESULT,
)
from tests.support.runtime._tmp import hyprial_test_tmp_root
from tests.daemon.harnesses.agy.conftest import (
    agy_spec,
    fake_acp_command,
    read_fake_log,
)


def _channel(tmp_path: Path):
    home = tmp_path / "hyprial-home"
    state = tmp_path / "hyprial-state"
    home.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    return build_worker_channel(
        actor="agent:op:node-1:agy-worker",
        session_ref="worker-sess-1",
        hyprial_home=home,
        state_dir=state,
        python_executable=sys.executable,
    )


def test_worker_channel_to_acp_mcp_server_converts_env_dict(tmp_path: Path) -> None:
    channel = _channel(tmp_path)
    converted = worker_channel_to_acp_mcp_server(channel)
    assert converted["name"] == "harness-bridge"
    assert "type" not in converted
    assert converted["command"] == sys.executable
    assert isinstance(converted["env"], list)
    env_map = {item["name"]: item["value"] for item in converted["env"]}
    assert env_map["HYPRIAL_WORKER_SESSION_REF"] == "worker-sess-1"
    assert env_map["HYPRIAL_HOME"] == str(tmp_path / "hyprial-home")


@pytest.mark.anyio
async def test_handshake_gemini_dir_api_key_auth_and_turn_observations(tmp_path: Path) -> None:
    log_path = tmp_path / "fake.log"
    session_root = tmp_path / "session-root"
    session_root.mkdir()
    channel = _channel(tmp_path)
    spec = agy_spec(
        cwd=str(tmp_path),
        model="gemini-3.8-flash",
        effort="medium",
        args=("--mode", "accept-edits"),
    )
    client = AgyAcpClient(
        spec,
        command=fake_acp_command(),
        env={
            "FAKE_ACP_MODE": "require_api_key",
            "FAKE_ACP_LOG": str(log_path),
            "GEMINI_API_KEY": "test-gemini-key",
        },
        worker_channel=channel,
        session_root=session_root,
    )
    async with client:
        assert client.reached_ready is True
        await client.query("hello world")
        items = [item async for item in client.receive_response()]

    outcomes = [item for item in items if isinstance(item, AgyTurnOutcome)]
    observations = [item for item in items if isinstance(item, ProgressObservation)]
    assert len(outcomes) == 1
    assert outcomes[0].is_error is False
    assert outcomes[0].result == "echo:hello world"
    assert "SECRET_THOUGHT" not in outcomes[0].result
    assert [obs.phase for obs in observations] == [
        PHASE_THINKING,
        PHASE_TOOL_CALL,
        PHASE_TOOL_RESULT,
        PHASE_MESSAGE_SEGMENT,
    ]

    events = read_fake_log(log_path)
    argv = events[0]["payload"]
    assert "--gemini-dir" in argv
    assert str(session_root) in argv
    assert "--disable-telemetry" in argv
    assert "--mode" not in argv
    methods = [entry["event"] for entry in events]
    assert "authenticate" in methods
    assert methods.index("authenticate") < methods.index("session/new")
    new_payload = next(e["payload"] for e in events if e["event"] == "session/new")
    assert new_payload["_meta"]["antigravity"]["systemPrompt"]["append"]
    assert new_payload["mcpServers"][0]["name"] == "harness-bridge"
    config_calls = [e["payload"] for e in events if e["event"] == "session/set_config_option"]
    assert [(c["configId"], c["value"]) for c in config_calls] == [
        ("model", "gemini-3.8-flash"),
        ("thought_level", "medium"),
        ("mode", "accept-edits"),
    ]


@pytest.mark.anyio
async def test_resume_reapplies_config_and_injects_mcp_and_system_prompt(tmp_path: Path) -> None:
    log_path = tmp_path / "fake.log"
    channel = _channel(tmp_path)
    spec = agy_spec(
        cwd=str(tmp_path),
        session_ref="known-session-1",
        model="gemini-3.8-flash",
        effort="low",
        args=("--mode", "accept-edits"),
    )
    client = AgyAcpClient(
        spec,
        command=fake_acp_command(),
        env={"FAKE_ACP_MODE": "normal", "FAKE_ACP_LOG": str(log_path)},
        worker_channel=channel,
    )
    async with client:
        assert client.session_id == "known-session-1"
        await client.query("after resume")
        items = [item async for item in client.receive_response()]

    assert any(isinstance(i, AgyTurnOutcome) and i.result == "echo:after resume" for i in items)
    events = read_fake_log(log_path)
    resume_payload = next(e["payload"] for e in events if e["event"] == "session/resume")
    assert resume_payload["mcpServers"][0]["name"] == "harness-bridge"
    assert resume_payload["_meta"]["antigravity"]["systemPrompt"]["append"]
    config_calls = [e["payload"] for e in events if e["event"] == "session/set_config_option"]
    assert [(c["configId"], c["value"]) for c in config_calls] == [
        ("model", "gemini-3.8-flash"),
        ("thought_level", "low"),
        ("mode", "accept-edits"),
    ]


@pytest.mark.anyio
async def test_strict_resume_rejects_missing_session_from_list(tmp_path: Path) -> None:
    spec = agy_spec(cwd=str(tmp_path), session_ref="missing-session-999")
    client = AgyAcpClient(
        spec,
        command=fake_acp_command(),
        env={"FAKE_ACP_MODE": "normal"},
        strict_resume=True,
    )
    with pytest.raises(AgySessionNotFoundError):
        async with client:
            pass


@pytest.mark.anyio
async def test_session_load_fallback_suppresses_history_replay(tmp_path: Path) -> None:
    log_path = tmp_path / "fake.log"
    spec = agy_spec(
        cwd=str(tmp_path),
        session_ref="known-session-1",
        model="gemini-3.8-flash",
    )
    client = AgyAcpClient(
        spec,
        command=fake_acp_command(),
        env={"FAKE_ACP_MODE": "no_resume_use_load", "FAKE_ACP_LOG": str(log_path)},
    )
    async with client:
        await client.query("fresh turn")
        items = [item async for item in client.receive_response()]

    outcomes = [i for i in items if isinstance(i, AgyTurnOutcome)]
    assert outcomes[0].result == "echo:fresh turn"
    assert "REPLAYED_OLD_TURN" not in outcomes[0].result
    assert all(
        getattr(obs, "tool_name", None) != "replayed_tool"
        for obs in items
        if isinstance(obs, ProgressObservation)
    )


@pytest.mark.anyio
async def test_session_resume_suppresses_history_replay(tmp_path: Path) -> None:
    spec = agy_spec(cwd=str(tmp_path), session_ref="known-session-1")
    client = AgyAcpClient(
        spec,
        command=fake_acp_command(),
        env={"FAKE_ACP_MODE": "resume_replay"},
    )
    async with client:
        await client.query("fresh turn")
        items = [item async for item in client.receive_response()]
    assert all("REPLAYED_RESUME" not in getattr(item, "result", "") for item in items)


@pytest.mark.anyio
async def test_unsupported_thought_level_and_mode_auto_fail_explicitly(tmp_path: Path) -> None:
    with pytest.raises(AgyStartPreconditionError, match="auto"):
        AgyAcpClient(
            agy_spec(cwd=str(tmp_path), args=("--mode", "auto")),
            command=fake_acp_command(),
        )

    client = AgyAcpClient(
        agy_spec(cwd=str(tmp_path), model="gemini-3.8-flash", effort="xhigh"),
        command=fake_acp_command(),
        env={"FAKE_ACP_MODE": "normal"},
    )
    with pytest.raises(AgyStartPreconditionError, match="thought_level"):
        async with client:
            pass


@pytest.mark.anyio
@pytest.mark.parametrize("skip_permissions", [False, True])
async def test_permission_preserves_id_types_and_enforces_mcp_policy(
    tmp_path: Path, skip_permissions: bool
) -> None:
    log_path = tmp_path / "perm.log"
    channel = _channel(tmp_path)
    args = ("--dangerously-skip-permissions",) if skip_permissions else ()
    client = AgyAcpClient(
        agy_spec(cwd=str(tmp_path), args=args),
        command=fake_acp_command(),
        env={"FAKE_ACP_MODE": "permission_str_id", "FAKE_ACP_LOG": str(log_path)},
        worker_channel=channel,
    )
    async with client:
        await client.query("run perm")
        items = [item async for item in client.receive_response()]

    assert any(isinstance(i, AgyTurnOutcome) and i.result == "echo:run perm" for i in items)
    events = read_fake_log(log_path)
    perm_replies = [e["payload"] for e in events if e["event"] == "permission_reply"]
    assert perm_replies[0]["id"] == "1"
    assert perm_replies[0]["id_type"] == "str"
    assert perm_replies[0]["reply"]["id"] == "1"
    assert perm_replies[0]["reply"]["result"]["outcome"]["optionId"] == "allow"

    assert perm_replies[1]["id"] == 99
    assert perm_replies[1]["id_type"] == "int"
    assert perm_replies[1]["reply"]["id"] == 99
    expected_non_mcp = "allow" if skip_permissions else "deny"
    assert perm_replies[1]["reply"]["result"]["outcome"]["optionId"] == expected_non_mcp

    unknown_reply = next(e["payload"] for e in events if e["event"] == "unknown_reply")
    assert unknown_reply["id"] == "unknown-9"
    assert unknown_reply["error"]["code"] == -32601

    noncanonical = select_permission_option(
        {
            "toolCall": {"name": "mcp__harness-bridge__harness_read"},
            "options": [
                {"optionId": "allow", "kind": "allow_once"},
                {"optionId": "deny", "kind": "reject_once"},
            ],
        },
        allow_all=False,
        allowed_mcp_tools=allowed_harness_bridge_tool_names(channel),
    )
    assert noncanonical["outcome"] == {"outcome": "selected", "optionId": "deny"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mode", "expect_eof"),
    [
        ("cancel_then_cancelled", False),
        ("cancel_then_end_turn", False),
        ("cancel_then_eof", True),
        ("cancel_hang", True),
    ],
)
async def test_interrupt_race_modes_and_double_interrupt_never_deadlock(
    tmp_path: Path, mode: str, expect_eof: bool
) -> None:
    log_path = tmp_path / f"{mode}.log"
    client = AgyAcpClient(
        agy_spec(cwd=str(tmp_path)),
        command=fake_acp_command(),
        env={"FAKE_ACP_MODE": mode, "FAKE_ACP_LOG": str(log_path)},
        cancel_timeout_seconds=0.25,
    )
    async with client:
        await client.query("work")

        async def collect() -> list[object]:
            return [item async for item in client.receive_response()]

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        await asyncio.gather(client.interrupt(), client.interrupt())
        if expect_eof:
            with pytest.raises(ConnectionError):
                await asyncio.wait_for(task, timeout=2.0)
        else:
            items = await asyncio.wait_for(task, timeout=2.0)
            outcomes = [i for i in items if isinstance(i, AgyTurnOutcome)]
            assert len(outcomes) == 1
            if mode == "cancel_then_cancelled":
                assert outcomes[0].is_error is True
                assert outcomes[0].stop_reason == "cancelled"
            else:
                assert outcomes[0].is_error is False
                assert outcomes[0].stop_reason == "end_turn"

    events = read_fake_log(log_path)
    cancels = [e["payload"] for e in events if e["event"] == "session/cancel"]
    assert len(cancels) == 1
    assert cancels[0]["has_id"] is False


@pytest.mark.anyio
async def test_single_flight_lock_and_late_chunk_generation_drop(tmp_path: Path) -> None:
    log_path = tmp_path / "overlap.log"
    client = AgyAcpClient(
        agy_spec(cwd=str(tmp_path)),
        command=fake_acp_command(),
        env={"FAKE_ACP_MODE": "prompt_slow_overlap", "FAKE_ACP_LOG": str(log_path)},
    )
    async with client:
        async def run_one(text: str) -> str:
            await client.query(text)
            items = [item async for item in client.receive_response()]
            return [i for i in items if isinstance(i, AgyTurnOutcome)][0].result

        r1, r2 = await asyncio.gather(run_one("first"), run_one("second"))
        assert {r1, r2} == {"echo:first", "echo:second"}

    late_client = AgyAcpClient(
        agy_spec(cwd=str(tmp_path)),
        command=fake_acp_command(),
        env={"FAKE_ACP_MODE": "late_chunk_after_cancel"},
    )
    async with late_client:
        await late_client.query("turn1")
        task = asyncio.create_task(
            _collect_all(late_client.receive_response())
        )
        await asyncio.sleep(0.05)
        await late_client.interrupt()
        items = await task
        assert all(
            "STALE_LATE_CHUNK" not in getattr(i, "result", "")
            for i in items
        )
        await asyncio.sleep(0.05)


@pytest.mark.anyio
async def test_cancel_reconnect_fences_delayed_chunk_before_second_turn(tmp_path: Path) -> None:
    client = AgyAcpClient(
        agy_spec(cwd=str(tmp_path)),
        command=fake_acp_command(),
        env={"FAKE_ACP_MODE": "late_chunk_after_cancel"},
    )
    async with client:
        await client.query("turn1")
        first = asyncio.create_task(_collect_all(client.receive_response()))
        await asyncio.sleep(0.05)
        await client.interrupt()
        first_items = await first
        await client.query("turn2")
        second_items = [item async for item in client.receive_response()]
    second = [item for item in second_items if isinstance(item, AgyTurnOutcome)]
    assert second[0].result == "echo:turn2"
    assert "STALE_LATE_CHUNK" not in second[0].result
    assert all("STALE_LATE_CHUNK" not in getattr(item, "result", "") for item in first_items)


@pytest.mark.anyio
async def test_stale_prompt_correlation_is_dropped(tmp_path: Path) -> None:
    client = AgyAcpClient(
        agy_spec(cwd=str(tmp_path)),
        command=fake_acp_command(),
        env={"FAKE_ACP_MODE": "stale_correlation"},
    )
    async with client:
        await client.query("clean")
        items = [item async for item in client.receive_response()]
    outcome = next(item for item in items if isinstance(item, AgyTurnOutcome))
    assert outcome.result == "echo:clean"
    assert "STALE_CORRELATED_CHUNK" not in outcome.result
async def _collect_all(ait):
    return [item async for item in ait]


@pytest.mark.anyio
async def test_real_agent_channel_mcp_server_discover_and_whoami(tmp_path: Path) -> None:
    import threading
    from hyprial.kernel import listen_local_ipc

    with tempfile.TemporaryDirectory(prefix="agy-mcp-", dir=hyprial_test_tmp_root()) as raw_sock_dir:
        sock_dir = Path(raw_sock_dir)
        home_dir = tmp_path / "hyprial-home"
        home_dir.mkdir(parents=True, exist_ok=True)
        actor = "agent:op:node-1:agy-worker"
        session_ref = "worker-sess-real-1"
        sock_path = sock_dir / "daemon.sock"
        listener = listen_local_ipc(sock_path)
        stop_serving = threading.Event()

        def serve_once() -> None:
            while not stop_serving.is_set():
                try:
                    conn, _peer = listener.accept()
                except OSError:
                    break
                try:
                    data = bytearray()
                    while b"\n" not in data:
                        chunk = conn.recv(64 * 1024)
                        if not chunk:
                            break
                        data.extend(chunk)
                    if data:
                        req = json.loads(data.partition(b"\n")[0].decode("utf-8"))
                        resp = {
                            "version": 1,
                            "id": req.get("id"),
                            "result": {"actor": actor, "sessionRef": session_ref},
                        }
                        conn.sendall(json.dumps(resp, separators=(",", ":")).encode("utf-8") + b"\n")
                finally:
                    conn.close()

        server_thread = threading.Thread(target=serve_once, daemon=True)
        server_thread.start()
        try:
            log_path = tmp_path / "real_mcp.log"
            channel = build_worker_channel(
                actor=actor,
                session_ref=session_ref,
                hyprial_home=home_dir,
                state_dir=sock_dir,
                python_executable=sys.executable,
            )
            client = AgyAcpClient(
                agy_spec(cwd=str(tmp_path)),
                command=fake_acp_command(),
                env={"FAKE_ACP_MODE": "mcp_handshake_real", "FAKE_ACP_LOG": str(log_path)},
                worker_channel=channel,
            )
            async with client:
                await client.query("check whoami")
                items = [item async for item in client.receive_response()]

            outcomes = [i for i in items if isinstance(i, AgyTurnOutcome)]
            assert outcomes[0].is_error is False
            assert actor in outcomes[0].result
            events = read_fake_log(log_path)
            discover = next(e["payload"] for e in events if e["event"] == "mcp_discover")
            assert "2026-07-28" in discover["result"]["supportedVersions"]
            tools = next(e["payload"] for e in events if e["event"] == "mcp_tools_list")
            tool_names = {tool["name"] for tool in tools["result"]["tools"]}
            assert {"harness_whoami", "harness_read", "harness_reply"} <= tool_names
        finally:
            stop_serving.set()
            listener.close()
            server_thread.join(timeout=2.0)
