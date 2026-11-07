from __future__ import annotations

import asyncio
import inspect
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
import pytest
from hyprial.daemon.impl.api import HarnessResultStatus
from hyprial.daemon.impl.inbox import (
    DeliveryLifecycle,
    InboxMessage,
    InboxService,
    MemoryDeliveryTransport,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses import HarnessLauncher, PtyHarnessProcess, is_streaming_spec
from hyprial.daemon.impl.harnesses.codex import APP_SERVER_STARTUP_MARGIN_SECONDS, APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT, REQUEST_TIMEOUT_SECONDS_DEFAULT, THREAD_START_TIMEOUT_SECONDS_DEFAULT, HARNESS_BRIDGE_MCP_SERVER_NAME, _sandbox_writable_roots_config, _server_request_response, _worker_channel_config, CodexAgentHomeError, CodexAppServerClient, CodexAppServerProcess, _OwnedProcessGroup, _darwin_group_has_live_members, _linux_group_has_live_members, _parse_linux_process_stat, _process_birth_identity
from hyprial.daemon.impl.harnesses.streaming import StreamingTurnProcess
from hyprial.kernel.impl.log import Logger

from ..support import (
    FAKE_CODEX,
    _delayed_thread_start_client,
    _elicitation,
    _git_repo,
    _prompt,
    _wire_worker_channel,
    assert_owned_group_gone,
    delivery,
    fake_process,
    read_log,
    spawn_owned_group,
    spec,
    wait_for_results,
)


def test_client_command_owns_app_server_stdio_transport() -> None:
    client = CodexAppServerClient(spec(), command=("codex",))
    assert client.command == ("codex", "app-server", "--stdio")


def test_codex_fake_driver_writes_redacted_lifecycle_log(
    tmp_path: Path,
) -> None:
    code = """
import json, sys, time
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    identifier = message.get('id')
    if method == 'initialize':
        print(json.dumps({'id': identifier, 'result': {}}), flush=True)
    elif method == 'thread/start':
        print(json.dumps({'id': identifier, 'result': {'thread': {'id': 'thread-1'}}}), flush=True)
        sys.stderr.write('api_key=codex-lifecycle-secret\\n')
        sys.stderr.flush()
        time.sleep(0.05)
        raise SystemExit(23)
"""

    class ProcessGroup:
        def register(self, _pid: int) -> None:
            return None

        def exists(self, _pid: int) -> bool:
            return False

        def release_if_gone(self, _pid: int) -> bool:
            return True

        def signal(self, _pid: int, _signum: int) -> None:
            return None

    async def drive() -> None:
        logger = Logger.worker(
            tmp_path / "state", runtime="codex", name="codex-worker"
        )
        legacy_log = tmp_path / "state" / "codex-app-server.log"
        legacy_log.parent.mkdir(parents=True)
        legacy_log.write_bytes(b"historical-codex-log\n")
        client = CodexAppServerClient(
            spec(),
            command=(sys.executable, "-c", code),
            process_group=ProcessGroup(),  # type: ignore[arg-type]
            logger=logger,
        )
        await client.__aenter__()
        assert client._exit_task is not None
        await asyncio.wait_for(client._exit_task, timeout=2.0)
        await client.__aexit__(None, None, None)

        raw = logger.path.read_text(encoding="utf-8")
        records = [json.loads(line) for line in raw.splitlines()]
        assert [record["event"] for record in records] == [
            "worker.started",
            "worker.ready",
            "worker.exited",
        ]
        assert all(record["component"] == "codex-worker" for record in records)
        assert all(record["name"] == "codex-worker" for record in records)
        assert records[-1]["returnCode"] == 23
        assert records[-1]["pid"] > 0
        assert "codex-lifecycle-secret" not in raw
        assert "[REDACTED]" in records[-1]["stderrTail"]
        assert legacy_log.read_bytes() == b"historical-codex-log\n"

    asyncio.run(drive())


def test_skill_loader_failure_still_starts_with_warning_and_daemon_log(tmp_path, monkeypatch):
    from ...native_home.support import _native_responses

    root, cwd, initialize, config, account, skills = _native_responses(tmp_path)
    skills["data"][0]["errors"] = [{
        "path": str(root / "skills/squire/SKILL.md"),
        "message": "missing YAML frontmatter delimited by --- api_key=PRIVATE_SENTINEL",
    }]
    responses = {"initialize": initialize, "config/read": config,
                 "account/read": account, "skills/list": skills,
                 "thread/start": {"thread": {"id": "thread-squire"}}}

    async def drive():
        logger = Logger.worker(tmp_path / "state", runtime="codex", name="squire")
        client = CodexAppServerClient(
            spec(), command=(sys.executable, str(FAKE_CODEX)), logger=logger,
        )
        # Exercise actual client startup/cleanup with controlled native receipts.
        client._p2_runtime = True
        client._native_root = root
        monkeypatch.setattr(client, "_working_directory", lambda: str(cwd))

        async def request(method, params, **kwargs):
            return responses[method]

        monkeypatch.setattr(client, "request", request)
        try:
            await client.__aenter__()
            assert client.running
            assert client.session_ref == "thread-squire"
            assert "missing YAML frontmatter delimited by ---" in client.startup_warnings[0]
            assert "PRIVATE_SENTINEL" not in client.startup_warnings[0]
            raw = (tmp_path / "state/logs/daemon.jsonl").read_text()
            assert "missing YAML frontmatter delimited by ---" in raw
            assert "PRIVATE_SENTINEL" not in raw
            assert "skills/squire/SKILL.md" in raw
            assert json.loads(raw.splitlines()[-1])["level"] == "warn"
        finally:
            await client.__aexit__(None, None, None)
        assert client._process is None or client._process.returncode is not None

    asyncio.run(drive())


def test_ready_codex_process_publishes_startup_warnings_to_process_owner():
    from hyprial.daemon.impl.processes.process_owner import ProcessOwner

    warning = "Codex skills/list reported loader errors: missing YAML frontmatter delimited by ---"

    class Client(CodexAppServerClient):
        @property
        def startup_warnings(self):
            return (warning,)

    process = CodexAppServerProcess(
        spec(), client_factory=lambda: Client(
            spec(), command=(sys.executable, str(FAKE_CODEX)),
        ),
    )
    owner = ProcessOwner(process)
    try:
        assert owner.wait_ready(timeout=3)
        assert owner.startup_warnings == (warning,)
        assert owner.running
        assert owner.last_error is None
    finally:
        owner.stop()


def test_process_requires_a_managed_headless_codex_spec() -> None:
    interactive = HarnessLaunchSpec(harness="codex", name="tui", headless=False)
    with pytest.raises(ValueError, match="managed headless"):
        CodexAppServerProcess(interactive)
    pi = HarnessLaunchSpec(harness="pi", name="pi", headless=True)
    with pytest.raises(ValueError, match="managed headless"):
        CodexAppServerProcess(pi)


def test_register_identity_failure_cleans_group_not_only_leader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _OwnedProcessGroup()
    leader, child_pid = spawn_owned_group(tmp_path)
    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.codex.process._process_birth_identity", lambda _pid: None
    )
    try:
        with pytest.raises(ConnectionError, match="identity"):
            owner.register(leader.pid)
        assert_owned_group_gone(leader, child_pid)
        assert owner.stopped()
    finally:
        try:
            os.killpg(leader.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_linux_zombie_only_group_is_not_reported_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.process.sys.platform", "linux")
    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.process.os.killpg", lambda *_args: None)
    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.codex.process._linux_group_has_live_members", lambda _pid: False
    )

    assert not _OwnedProcessGroup._group_has_live_members(4242)


def test_linux_process_stat_parser_handles_spaces_and_closing_parenthesis() -> None:
    fields = ["Z", "1", "4242", *("0" for _ in range(16)), "98765"]

    assert _parse_linux_process_stat(
        f"123 (codex ) tool) {' '.join(fields)}"
    ) == ("Z", 4242, "98765")


def test_linux_process_scan_distinguishes_zombie_only_and_live_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stats = {
        Path("/proc/101/stat"): "101 (zombie) Z 1 4242 "
        + " ".join("0" for _ in range(17)),
        Path("/proc/102/stat"): "102 (worker) S 1 4242 "
        + " ".join("0" for _ in range(17)),
        Path("/proc/103/stat"): "103 (unrelated) R 1 9999 "
        + " ".join("0" for _ in range(17)),
    }
    monkeypatch.setattr(
        Path,
        "iterdir",
        lambda _path: iter(path.parent for path in stats),
    )
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda path, **_kwargs: stats[path],
    )

    assert _linux_group_has_live_members(4242)
    del stats[Path("/proc/102/stat")]
    assert not _linux_group_has_live_members(4242)


def test_linux_unreadable_proc_entry_makes_group_liveness_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "iterdir", lambda _path: iter((Path("/proc/123"),)))
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()),
    )

    assert _linux_group_has_live_members(4242) is None


def test_linux_unreadable_group_members_fail_safe_as_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.process.sys.platform", "linux")
    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.process.os.killpg", lambda *_args: None)
    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.codex.process._linux_group_has_live_members", lambda _pid: None
    )

    assert _OwnedProcessGroup._group_has_live_members(4242)


def test_darwin_process_scan_distinguishes_zombie_only_and_live_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = iter(
        (
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Z 4242\nS 9999\n", stderr=""
            ),
            subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Z 4242\nR+ 4242\n", stderr=""
            ),
        )
    )
    monkeypatch.setattr(
        "hyprial.kernel.impl.processes.owned_process.subprocess.run", lambda *_args, **_kwargs: next(outcomes)
    )

    assert not _darwin_group_has_live_members(4242)
    assert _darwin_group_has_live_members(4242)


def test_darwin_unreadable_group_members_fail_safe_as_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.process.sys.platform", "darwin")
    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.process.os.killpg", lambda *_args: None)
    monkeypatch.setattr(
        "hyprial.daemon.impl.harnesses.codex.process._darwin_group_has_live_members", lambda _pid: None
    )

    assert _OwnedProcessGroup._group_has_live_members(4242)


def test_linux_birth_identity_fails_safe_when_proc_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.process.sys.platform", "linux")
    monkeypatch.setattr(
        Path, "read_text", lambda *_a, **_k: (_ for _ in ()).throw(OSError())
    )
    monkeypatch.setattr(
       "hyprial.kernel.impl.processes.owned_process.subprocess.run",
       lambda *_a, **_k: pytest.fail("Linux identity must not fall back to ps"),
    )

    assert _process_birth_identity(4242) is None


def test_macos_birth_identity_fails_safe_when_ps_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("hyprial.daemon.impl.harnesses.codex.process.sys.platform", "darwin")
    monkeypatch.setattr(
       "hyprial.kernel.impl.processes.owned_process.subprocess.run",
       lambda *_a, **_k: (_ for _ in ()).throw(subprocess.TimeoutExpired("ps", 1)),
    )

    assert _process_birth_identity(4242) is None


def test_worker_channel_injects_harness_mcp_server_into_thread_start(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "wire.jsonl"
    channel = _wire_worker_channel(tmp_path)
    process = CodexAppServerProcess(
        spec(),
        command=(sys.executable, str(FAKE_CODEX)),
        env={"FAKE_CODEX_LOG": str(log_path)},
        worker_channel=channel,
    )
    try:
        assert process.wait_ready(timeout=5.0)
        start = next(
            message
            for message in read_log(log_path)
            if message.get("method") == "thread/start"
        )
        server = start["params"]["config"]["mcp_servers"]["harness-bridge"]
        # Codex infers stdio from command; the Claude-SDK "type" key is dropped.
        assert "type" not in server
        assert server["command"] == channel.mcp_server["command"]
        assert server["args"] == channel.mcp_server["args"]
        assert server["env"] == channel.mcp_server["env"]
        # Harness tools are pre-approved so `-a never` never has to be asked.
        # Asserted by name on the wire: codex drops unrecognised mcp_servers
        # keys silently, so a misspelling would behave exactly like working
        # code and nothing else in the suite would notice.
        assert server["default_tools_approval_mode"] == "auto"
    finally:
        process.stop()


def test_empty_cwd_thread_start_grants_future_git_without_worker_channel(
    tmp_path: Path,
) -> None:
    """The grant must arrive before clone-in-cwd creates `.git`."""

    log_path = tmp_path / "wire.jsonl"
    plain = tmp_path / "plain"
    plain.mkdir()
    process = CodexAppServerProcess(
        spec(cwd=str(plain)),
        command=(sys.executable, str(FAKE_CODEX)),
        env={"FAKE_CODEX_LOG": str(log_path)},
    )
    try:
        assert process.wait_ready(timeout=5.0)
        start = next(
            message
            for message in read_log(log_path)
            if message.get("method") == "thread/start"
        )
        assert start["params"]["config"] == {
            "sandbox_workspace_write": {
                "writable_roots": [str(plain / ".git")]
            }
        }
    finally:
        process.stop()


@pytest.mark.parametrize(
    ("sandbox", "granted"),
    [
        ("workspace-write", True),
        ("read-only", False),
        ("danger-full-access", False),
        (None, True),
    ],
)
def test_sandbox_writable_roots_only_for_workspace_write(
    tmp_path: Path, sandbox: str | None, granted: bool
) -> None:
    repo = _git_repo(tmp_path / "clone")
    execution: dict[str, object] = {}
    if sandbox is not None:
        execution["sandbox"] = sandbox
    config = _sandbox_writable_roots_config(repo, execution)
    if not granted:
        assert config is None
        return
    assert config == {
        "sandbox_workspace_write": {
            "writable_roots": [str((repo / ".git").resolve())]
        }
    }


def test_sandbox_writable_roots_grant_future_git_for_empty_workspace_write(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _sandbox_writable_roots_config(
        plain, {"sandbox": "workspace-write"}
    ) == {
        "sandbox_workspace_write": {
            "writable_roots": [str(plain / ".git")]
        }
    }


@pytest.mark.parametrize("sandbox", ["read-only", "danger-full-access"])
def test_empty_cwd_gets_no_git_grant_outside_workspace_write(
    tmp_path: Path, sandbox: str,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _sandbox_writable_roots_config(plain, {"sandbox": sandbox}) is None


def test_thread_start_grants_git_write_under_workspace_write(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "wire.jsonl"
    repo = _git_repo(tmp_path / "clone")
    process = CodexAppServerProcess(
        spec(args=("-a", "on-request", "-s", "workspace-write"), cwd=str(repo)),
        command=(sys.executable, str(FAKE_CODEX)),
        env={"FAKE_CODEX_LOG": str(log_path)},
    )
    try:
        assert process.wait_ready(timeout=5.0)
        start = next(
            message
            for message in read_log(log_path)
            if message.get("method") == "thread/start"
        )
        roots = start["params"]["config"]["sandbox_workspace_write"][
            "writable_roots"
        ]
        assert roots == [str((repo / ".git").resolve())]
    finally:
        process.stop()


def test_thread_start_does_not_grant_git_write_under_read_only(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "wire.jsonl"
    repo = _git_repo(tmp_path / "clone")
    process = CodexAppServerProcess(
        spec(args=("-a", "on-request", "-s", "read-only"), cwd=str(repo)),
        command=(sys.executable, str(FAKE_CODEX)),
        env={"FAKE_CODEX_LOG": str(log_path)},
    )
    try:
        assert process.wait_ready(timeout=5.0)
        start = next(
            message
            for message in read_log(log_path)
            if message.get("method") == "thread/start"
        )
        assert "config" not in start["params"]
    finally:
        process.stop()


def test_model_argument_is_translated_to_thread_start(tmp_path: Path) -> None:
    log_path = tmp_path / "wire.jsonl"
    launch = spec(args=("--model", "gpt-5.6-codex"))
    process = CodexAppServerProcess(
        launch,
        command=(sys.executable, str(FAKE_CODEX)),
        env={"FAKE_CODEX_LOG": str(log_path)},
    )
    try:
        assert process.wait_ready(timeout=5.0)
        start = next(
            message
            for message in read_log(log_path)
            if message.get("method") == "thread/start"
        )
        assert start["params"]["model"] == "gpt-5.6-codex"
    finally:
        process.stop()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("-a", "never", "-s", "workspace-write"), {"approvalPolicy": "never", "sandbox": "workspace-write"}),
        (("--ask-for-approval=untrusted", "--sandbox=read-only"), {"approvalPolicy": "untrusted", "sandbox": "read-only"}),
        (("--approve-for-me",), {"approvalsReviewer": "auto_review", "sandbox": "workspace-write"}),
        (("--dangerously-bypass-approvals-and-sandbox",), {"approvalPolicy": "never", "sandbox": "danger-full-access"}),
    ],
)
def test_app_server_translates_codex_execution_policy_arguments(
    tmp_path: Path, args: tuple[str, ...], expected: dict[str, str]
) -> None:
    log_path = tmp_path / "wire.jsonl"
    process = CodexAppServerProcess(
        spec(args=args),
        command=(sys.executable, str(FAKE_CODEX)),
        env={"FAKE_CODEX_LOG": str(log_path)},
    )
    try:
        assert process.wait_ready(timeout=5.0)
        start = next(
            message
            for message in read_log(log_path)
            if message.get("method") == "thread/start"
        )
        assert {key: start["params"].get(key) for key in expected} == expected
    finally:
        process.stop()


def test_unattended_client_declines_command_approval_without_stalling() -> None:
    process = fake_process("approval")
    try:
        assert process.enqueue(delivery("delivery-approval", "unsafe command"))
        [result] = wait_for_results(process, 1)

        assert result.status is HarnessResultStatus.COMPLETED
        assert result.output == _prompt("unsafe command done")
        assert process.server_request_methods == (
            "item/commandExecution/requestApproval",
        )
    finally:
        process.stop()


def test_unsupported_server_request_is_answered_logged_and_does_not_stall(
    tmp_path: Path,
) -> None:
    """An unseen request is still answered, named in the log, and the turn ends."""

    log_path = tmp_path / "wire.jsonl"
    channel = _wire_worker_channel(tmp_path)
    process = CodexAppServerProcess(
        spec(),
        command=(sys.executable, str(FAKE_CODEX)),
        env={
            "FAKE_CODEX_MODE": "unsupported-request",
            "FAKE_CODEX_LOG": str(log_path),
        },
        worker_channel=channel,
    )
    item = delivery("delivery-unsupported", "probe")
    now_ms = time.time_ns() // 1_000_000
    inbox = InboxService(
        channel.state_dir / "inbox.sqlite3", MemoryDeliveryTransport()
    )
    assert inbox.receive(
        InboxMessage(
            message_id=item.delivery_id,
            conversation_id=item.conversation_id,
            sender="agent:op:sender-node:requester",
            recipient=item.recipient,
            payload=item.message.encode(),
            intent="request",
            lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
            created_at_ms=now_ms,
        ),
        now_ms=now_ms,
    ).acknowledged
    try:
        assert process.wait_ready(timeout=5.0)
        assert process.enqueue(item)
        [result] = wait_for_results(process, 1)
        assert result.status is HarnessResultStatus.COMPLETED
        assert process.server_request_methods == ("attestation/generate",)
    finally:
        process.stop()
        inbox.close()
    worker_log = (
        channel.state_dir / "logs" / "workers" / "codex-codex-worker.jsonl"
    )
    events = [
        json.loads(line) for line in worker_log.read_text().splitlines() if line
    ]
    named = [
        event
        for event in events
        if event.get("event") == "codex.server_request.unsupported"
    ]
    assert named and named[0].get("method") == "attestation/generate"


@pytest.mark.parametrize(
    "args",
    [
        ("--profile", "personal"),
        ("--search",),
        ("--ask-for-approval",),
        ("--ask-for-approval", "sometimes"),
        ("--sandbox", "host"),
        ("--model", "one", "--model", "two"),
        ("-a", "never", "--approve-for-me"),
        ("-s", "read-only", "--dangerously-bypass-approvals-and-sandbox"),
    ],
)
def test_app_server_rejects_unsupported_or_ambiguous_cli_arguments(
    args: tuple[str, ...],
) -> None:
    process = CodexAppServerProcess(
        spec(args=args),
        command=(sys.executable, str(FAKE_CODEX)),
    )
    try:
        assert not process.wait_ready(timeout=0.5)
        assert process.last_error is not None
        assert "Codex app-server argument" in process.last_error
    finally:
        process.stop()


def test_default_launcher_gives_headless_codex_an_app_server_process() -> None:
    launched = []

    class FakeAppServerProcess:
        running = True

        def wait_ready(self, *, timeout: float | None = None) -> bool:
            return True

        def stop(self) -> None:
            self.running = False

    process = FakeAppServerProcess()
    launcher = HarnessLauncher(
        codex_app_server_factory=lambda launch: launched.append(launch) or process
    )
    launch = spec(args=("--model", "gpt-5.6-codex"))

    assert is_streaming_spec(launch)
    assert launcher.start(launch) is process
    assert launched == [launch]


def test_runtime_launch_custody_rejects_before_codex_native_spawn() -> None:
    class RejectedCustody:
        def __enter__(self):
            raise RuntimeError("AGENT_RUNTIME_CONTEXT_STALE")

        def __exit__(self, *_args):
            return False

    client = CodexAppServerClient(
        spec(),
        command=("/must-not-spawn/codex",),
        runtime_launch_custody=lambda _context: RejectedCustody(),
    )
    client._runtime_context = object()  # type: ignore[assignment]

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="AGENT_RUNTIME_CONTEXT_STALE"):
            await client.__aenter__()

    asyncio.run(scenario())
    assert client._process is None


def test_launcher_fails_loudly_when_app_server_never_becomes_ready() -> None:
    class UnreadyAppServerProcess:
        running = True
        last_error = "Codex authentication failed"

        def wait_ready(self, *, timeout: float | None = None) -> bool:
            return False

        def stop(self) -> None:
            self.running = False

    process = UnreadyAppServerProcess()
    launcher = HarnessLauncher(
        codex_app_server_factory=lambda _launch: process,
        codex_startup_timeout_seconds=0.01,
    )

    with pytest.raises(Exception, match="authentication failed"):
        launcher.start(spec())
    assert process.running is False


def test_codex_client_configuration_error_fails_start_once_and_is_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    backoffs: list[float] = []
    backoff_started = threading.Event()
    release_backoff = threading.Event()

    async def controlled_backoff(delay: float) -> None:
        backoffs.append(delay)
        backoff_started.set()
        await asyncio.to_thread(release_backoff.wait)

    import hyprial.daemon.impl.harnesses.streaming.process as streaming_process_module

    monkeypatch.setattr(streaming_process_module.asyncio, "sleep", controlled_backoff)

    class RecordingLogger:
        def __init__(self) -> None:
            self.entries: list[tuple[str, str, dict[str, object]]] = []

        def error(self, event: str, **fields: object) -> None:
            self.entries.append(("error", event, fields))

    logger = RecordingLogger()

    def refusing_factory():
        nonlocal attempts
        attempts += 1
        raise CodexAgentHomeError("fixture CODEX_HOME is invalid")

    process = StreamingTurnProcess(
        harness="codex",
        label="Codex app-server",
        client_factory=refusing_factory,
        thread_name="codex-permanent-start-test",
        logger=logger,  # type: ignore[arg-type]
        reconnect_delay_seconds=0.25,
    )
    try:
        assert not process.wait_ready(timeout=0.5)
        assert attempts == 1
        assert backoffs == []
        assert process.last_error == "fixture CODEX_HOME is invalid"
        assert logger.entries == [
            (
                "error",
                "worker.start.failed",
                {
                    "harness": "codex",
                    "failure": "fixture CODEX_HOME is invalid",
                },
            )
        ]
    finally:
        release_backoff.set()
        process.stop()


def test_transient_client_construction_error_is_still_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    backoffs: list[float] = []

    async def controlled_backoff(delay: float) -> None:
        backoffs.append(delay)

    import hyprial.daemon.impl.harnesses.streaming.process as streaming_process_module

    monkeypatch.setattr(streaming_process_module.asyncio, "sleep", controlled_backoff)

    class ReadyClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    def recovering_factory():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("fixture transport is unavailable")
        return ReadyClient()

    process = StreamingTurnProcess(
        harness="codex",
        label="Codex app-server",
        client_factory=recovering_factory,
        thread_name="codex-transient-start-test",
        reconnect_delay_seconds=0.25,
    )
    try:
        assert process.wait_ready(timeout=0.5)
        assert attempts == 2
        assert backoffs == [0.25]
        assert process.last_error is None
    finally:
        process.stop()


def test_overridden_codex_command_keeps_the_pty_seam() -> None:
    launcher = HarnessLauncher(
        commands={"codex": (sys.executable, "-c", "import time; time.sleep(30)")}
    )
    process = launcher.start(spec())
    try:
        assert isinstance(process, PtyHarnessProcess)
    finally:
        process.stop()


def test_unattended_client_grants_its_own_harness_bridge_tool_calls() -> None:
    """Card 87dc8276: under -a on-request the approval lands here; -32601
    made codex record "user rejected MCP tool call" and the worker never
    replied natively."""
    answer = _server_request_response(
        7,
        "mcpServer/elicitation/request",
        _elicitation(HARNESS_BRIDGE_MCP_SERVER_NAME),
    )
    assert answer == {"id": 7, "result": {"action": "accept", "content": {}}}


def test_unattended_client_declines_tool_calls_of_any_other_mcp_server() -> None:
    answer = _server_request_response(
        8, "mcpServer/elicitation/request", _elicitation("probe")
    )
    assert answer == {"id": 8, "result": {"action": "decline"}}


def test_unattended_client_declines_elicitations_that_are_not_tool_call_approvals() -> (
    None
):
    for params in (
        _elicitation(HARNESS_BRIDGE_MCP_SERVER_NAME, kind="oauth"),
        _elicitation(HARNESS_BRIDGE_MCP_SERVER_NAME, kind=None),
        None,
    ):
        answer = _server_request_response(9, "mcpServer/elicitation/request", params)
        assert answer == {"id": 9, "result": {"action": "decline"}}, params


@pytest.mark.parametrize(
    "method",
    ["item/commandExecution/requestApproval", "item/fileChange/requestApproval"],
)
def test_unattended_client_declines_v2_command_and_file_approvals(method: str) -> None:
    """Every v2 approval method gets a schema-valid decline, never -32601."""

    answer = _server_request_response(11, method, {"threadId": "t", "turnId": "u"})
    assert answer == {"id": 11, "result": {"decision": "decline"}}


def test_unattended_client_grants_no_permissions_but_answers_the_request() -> None:
    answer = _server_request_response(
        12, "item/permissions/requestApproval", {"threadId": "t", "turnId": "u"}
    )
    assert answer == {"id": 12, "result": {"permissions": {}, "scope": "turn"}}


def test_unattended_client_fails_dynamic_tool_calls_at_the_call() -> None:
    answer = _server_request_response(
        14, "item/tool/call", {"threadId": "t", "turnId": "u", "tool": "probe"}
    )
    assert answer["id"] == 14
    result = answer.get("result")
    assert isinstance(result, dict)
    assert result["success"] is False
    assert isinstance(result["contentItems"], list) and result["contentItems"]


def test_unattended_client_still_names_unknown_server_requests() -> None:
    """Kept for methods this client has never seen; the read loop logs it."""

    answer = _server_request_response(15, "attestation/generate", {})
    assert answer["error"]["code"] == -32601  # type: ignore[index]
    assert "attestation/generate" in str(answer["error"]["message"])  # type: ignore[index]


def test_worker_channel_config_routes_approvals_to_the_client(tmp_path: Path) -> None:
    from hyprial.daemon.impl.harnesses.worker_channel import build_worker_channel

    channel = build_worker_channel(
        actor="agent:op:node-1:worker",
        session_ref="ref-1",
        hyprial_home=tmp_path / "home",
        state_dir=tmp_path / "home" / "state",
    )
    config = _worker_channel_config(channel)
    assert config["approvals_reviewer"] == "user"
    assert set(config["mcp_servers"]) == {HARNESS_BRIDGE_MCP_SERVER_NAME}


def test_thread_start_is_bounded_by_its_own_timeout_not_the_rpc_timeout(
    tmp_path: Path,
) -> None:
    """Card 54158b16: thread/start measures 9-43 s on hq; the 15 s RPC timeout
    that used to bound it made the harness respawn app-server mid-launch."""

    async def drive() -> None:
        client = _delayed_thread_start_client(
            tmp_path, delay=0.5, request_timeout=0.2, thread_start_timeout=2.0
        )
        await client.__aenter__()
        try:
            assert client._session.thread_id == "thread-slow"
        finally:
            await client.__aexit__(None, None, None)

    asyncio.run(drive())


def test_app_server_startup_budget_composes_from_the_inner_waits() -> None:
    # Card 104164aa / 54158b16: the launcher's wait_ready budget is never an
    # independently chosen number. It is the two sequential stdio waits
    # __aenter__ performs -- initialize (request timeout) then thread/start or
    # thread/resume (thread-start timeout) -- plus a margin for spawn/register/
    # notify. There is no unix-socket wait on this headless path. An outer
    # pinned below the inner sum (20 s < a 9-43 s thread/start) is exactly what
    # failed E2E-006 codex.restart-restore in controlled run 5091.
    assert APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT == (
        REQUEST_TIMEOUT_SECONDS_DEFAULT
        + THREAD_START_TIMEOUT_SECONDS_DEFAULT
        + APP_SERVER_STARTUP_MARGIN_SECONDS
    )
    assert APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT >= (
        REQUEST_TIMEOUT_SECONDS_DEFAULT + THREAD_START_TIMEOUT_SECONDS_DEFAULT
    )


def test_launcher_codex_startup_budget_is_the_composed_default() -> None:
    default = (
        inspect.signature(HarnessLauncher.__init__)
        .parameters["codex_startup_timeout_seconds"]
        .default
    )
    assert default == APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT
