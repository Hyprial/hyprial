"""WP5-B: the interactive Codex thread gets the harness-bridge MCP surface.

Mounting is app-server level (step-0 probe, wp5b.md section 1): the TUI sends
thread/start itself, so the server rides ``codex app-server -c`` and reads the
sessionRef lazily from a launch-private file.  Real TUI model calls are
BLOCKED-on-WP8 (docs/evidence/.../wp5b/blocked.txt); everything below proves
the mount, the tool routing and the session fence with test doubles.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession
from typer.testing import CliRunner

from hyprial.daemon.impl.harnesses.capabilities import support
from hyprial.daemon.impl.mcp import StatelessDaemonProxy, create_mcp_server
from hyprial.daemon.impl.mcp.api import DaemonRequestRejected
from hyprial.kernel import Capability, SupportLevel, ipc_errors
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

pytestmark = pytest.mark.anyio

ACTOR = "agent:op:node-1:codex-tui"
THREAD_ID = "thread-11111111-2222-3333-4444-555555555555"
PENDING = "pending"


class FakeDaemon:
    """The daemon's session fence and inbox rows, as far as the tools see them."""

    def __init__(self) -> None:
        self.current: str | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.rows: dict[str, dict[str, Any]] = {
            "m-req": {"owesReply": True, "fetched": False, "body": "do it"},
            "m-fyi": {"owesReply": False, "fetched": False, "body": "fyi"},
        }

    async def request(
        self, method: str, params: dict[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        ref = params["sessionRef"]
        if self.current is None:
            raise DaemonRequestRejected(ipc_errors.STALE_SESSION, "no session")
        if ref != self.current:
            raise DaemonRequestRejected(ipc_errors.SESSION_SUPERSEDED, "other owner")
        row = self.rows.get(params.get("messageId", ""))
        if method == "message.read_body":
            assert row is not None
            row["fetched"] = True
            return {"ok": True, "body": row["body"], "fetched": True}
        if method == "message.reply":
            assert row is not None and row["owesReply"]
            del self.rows[params["messageId"]]
            return {"ok": True, "replied": True, "replyTo": params["messageId"]}
        if method == "message.ack":
            assert row is not None
            if row["owesReply"]:
                raise DaemonRequestRejected(ipc_errors.MESSAGE_ACK_UNAVAILABLE, "owes")
            del self.rows[params["messageId"]]
            return {"ok": True, "acknowledged": True}
        return {"ok": True}

    @asynccontextmanager
    async def connect(self) -> AsyncIterator["FakeDaemon"]:
        yield self


@asynccontextmanager
async def _thread_tools(
    daemon: FakeDaemon, ref_file: Path
) -> AsyncIterator[ClientSession]:
    """What the model in the interactive thread talks to."""

    from hyprial.daemon.impl.mcp.entry import SessionRefFileProxy

    proxy = SessionRefFileProxy(StatelessDaemonProxy(daemon), ref_file, PENDING)
    server = create_mcp_server(proxy, actor=ACTOR, session_ref=PENDING, workflow_tools=True)
    async with (
        InMemoryTransport(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as client,
    ):
        await client.initialize()
        yield client


def _registered(daemon: FakeDaemon, ref_file: Path) -> None:
    daemon.current = THREAD_ID
    ref_file.write_text(THREAD_ID, encoding="utf-8")


def test_interactive_session_mounts_harness_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {"calls": []}

    def daemon_request(method: str, params: dict[str, object] | None = None, **_k: object):
        if method == "ps":
            return {"daemon": {"owner": "op", "nodeId": "node-1"}}
        if method == "agent.runtime-context":
            return {"ok": True, "mode": "legacy", "environment": {}}
        if method == "session.register":
            seen["file_at_register"] = Path(seen["ref_file"]).exists()
        if method == "message.pending.list":
            seen.setdefault("file_after", Path(seen["ref_file"]).read_text())
        seen["calls"].append(method)
        return {"ok": True}

    class FakeServer:
        pid = 4321

        def __init__(self, socket_path: Path, **kwargs: Any) -> None:
            seen["server_env"] = kwargs.get("env")
            seen["config_args"] = tuple(kwargs.get("config_args", ()))
            args = [v for v in seen["config_args"] if v.startswith("mcp_servers.harness-bridge.args=")]
            decoded = json.loads(args[0].split("=", 1)[1]) if args else []
            if "--session-ref-file" in decoded:
                seen["ref_file"] = decoded[decoded.index("--session-ref-file") + 1]
            seen["decoded_args"] = decoded

        def start(self) -> None: ...
        def discover_thread(self) -> tuple[str, str]:
            return THREAD_ID, "thread/list"
        def stop(self) -> None: ...

    class FakeProcess:
        def __init__(self, argv: list[str], **_k: object) -> None:
            seen["tui_argv"] = argv
            self.returncode: int | None = None
        def poll(self) -> int | None:
            return self.returncode
        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0
            return 0
        def terminate(self) -> None: ...
        def kill(self) -> None: ...

    monkeypatch.setenv("HARNESS_CODEX_BIN", "/test/bin/codex")
    monkeypatch.setattr(_cli_get_services(), "_runtime_context_environment", lambda **_k: None)
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr("hyprial.daemon.CodexInteractiveAppServer", FakeServer)
    monkeypatch.setattr(_cli_subprocess, "Popen", FakeProcess)
    from hyprial.daemon.impl.mcp.channel import ownership as mcp_channel

    monkeypatch.setattr(mcp_channel, "_read_process_identity", lambda _pid: "proc:1")

    result = CliRunner().invoke(
        _cli_root_app, ["agent", "start", "codex", "--name", "codex-tui", "--cwd", str(tmp_path), "--json"]
    )

    assert result.exit_code == 0, result.output
    config = seen["config_args"]
    assert any(v.startswith("mcp_servers.harness-bridge.command=") for v in config)
    decoded = seen["decoded_args"]
    assert decoded[decoded.index("--actor") + 1] == ACTOR
    assert "agent-channel" in decoded
    # Unknown until the TUI thread is discovered, then published for the server.
    assert seen["file_at_register"] is False
    assert seen["file_after"] == THREAD_ID
    environment = seen["server_env"]
    assert environment["HYPRIAL_AGENT_SESSION"] == "1"
    assert environment["HYPRIAL_WORKER_ACTOR"] == ACTOR
    assert environment["HYPRIAL_WORKER_SESSION_REF_FILE"] == seen["ref_file"]
    assert "HYPRIAL_WORKER_SESSION_REF" not in environment
    # The mount is app-server level: nothing MCP rides the remote TUI argv.
    assert not any("mcp_servers" in part for part in seen["tui_argv"])


async def test_interactive_thread_can_reply(tmp_path: Path) -> None:
    daemon, ref_file = FakeDaemon(), tmp_path / "ref"
    _registered(daemon, ref_file)

    async with _thread_tools(daemon, ref_file) as client:
        result = await client.call_tool(
            "harness_reply", {"messageId": "m-req", "summary": "done", "message": "ok"}
        )

    assert result.is_error is not True
    (method, params), = daemon.calls
    assert method == "message.reply"
    assert params["messageId"] == "m-req"
    assert params["summary"] == "done"
    assert params["actor"] == ACTOR
    assert params["sessionRef"] == THREAD_ID
    assert "m-req" not in daemon.rows


async def test_interactive_thread_can_read_body(tmp_path: Path) -> None:
    daemon, ref_file = FakeDaemon(), tmp_path / "ref"
    _registered(daemon, ref_file)

    async with _thread_tools(daemon, ref_file) as client:
        result = await client.call_tool("harness_read_body", {"messageId": "m-req"})

    assert result.is_error is not True
    assert result.structured_content["body"] == "do it"
    assert daemon.rows["m-req"]["fetched"] is True
    assert daemon.calls[0][1]["sessionRef"] == THREAD_ID


async def test_interactive_thread_can_ack(tmp_path: Path) -> None:
    daemon, ref_file = FakeDaemon(), tmp_path / "ref"
    _registered(daemon, ref_file)

    async with _thread_tools(daemon, ref_file) as client:
        closed = await client.call_tool("harness_ack", {"messageId": "m-fyi"})
        owed = await client.call_tool("harness_ack", {"messageId": "m-req"})

    assert closed.is_error is not True
    assert "m-fyi" not in daemon.rows
    assert owed.is_error is True
    assert "m-req" in daemon.rows


async def test_interactive_send_requires_summary(tmp_path: Path) -> None:
    daemon, ref_file = FakeDaemon(), tmp_path / "ref"
    _registered(daemon, ref_file)

    async with _thread_tools(daemon, ref_file) as client:
        missing = await client.call_tool(
            "harness_send", {"to": ["agent:a", "agent:b"], "message": "hi"}
        )
        sent = await client.call_tool(
            "harness_send",
            {"to": ["agent:a", "agent:b"], "summary": "s", "message": "hi"},
        )

    assert missing.is_error is True
    assert sent.is_error is not True
    (method, params), = daemon.calls
    assert method == "message.send"
    assert params["to"] == ["agent:a", "agent:b"]
    assert params["summary"] == "s"
    assert params["sessionRef"] == THREAD_ID


async def test_interactive_mcp_is_session_fenced(tmp_path: Path) -> None:
    daemon, ref_file = FakeDaemon(), tmp_path / "ref"

    async with _thread_tools(daemon, ref_file) as client:
        # sessionRef not yet published: the placeholder is signed and fenced.
        early = await client.call_tool("harness_ack", {"messageId": "m-fyi"})
        assert early.is_error is True
        assert ipc_errors.STALE_SESSION in early.content[0].text
        assert daemon.calls[0][1]["sessionRef"] == PENDING

        _registered(daemon, ref_file)
        ok = await client.call_tool("harness_ack", {"messageId": "m-fyi"})
        assert ok.is_error is not True

        # A newer TUI took the actor: the old server's calls are refused.
        daemon.current = "thread-newer"
        late = await client.call_tool("harness_ack", {"messageId": "m-req"})

    assert late.is_error is True
    assert ipc_errors.SESSION_SUPERSEDED in late.content[0].text


def test_interactive_config_args_carry_no_session_ref() -> None:
    """The file is the only late-bound identity input: actor stays fixed."""

    from hyprial.daemon import interactive_channel_config_args

    args = interactive_channel_config_args(
        actor=ACTOR,
        session_ref_file=Path("/tmp/x/ref"),
        hyprial_home=Path("/tmp/h"),
        state_dir=Path("/tmp/h/state"),
    )
    assert args[0] == "-c"
    joined = " ".join(args)
    assert "HYPRIAL_WORKER_SESSION_REF" not in joined


async def test_native_capabilities_are_backed_by_a_tool_call(tmp_path: Path) -> None:
    for capability in (Capability.PROACTIVE_SEND, Capability.TOOL_INJECTION):
        entry = support("codex", headless=False, capability=capability)
        assert entry is not None
        assert entry.level is SupportLevel.NATIVE
        assert entry.mechanism == "app_server"

    # The declaration is only true while the thread's reply path works.
    await test_interactive_thread_can_reply(tmp_path)


# Ruling 77: under ``-a never`` codex refuses every MCP tool call, so an
# interactive launch asks for ``on-request`` and refuses the policies that
# would silently disable the harness-bridge tools.


def _launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    runtime_args: tuple[str, ...] = (),
    resume: str | None = None,
) -> dict[str, Any]:
    seen: dict[str, Any] = {"resume_args": None}

    def daemon_request(method: str, params: dict[str, object] | None = None, **_k: object):
        if method == "ps":
            return {"daemon": {"owner": "op", "nodeId": "node-1"}}
        if method == "agent.runtime-context":
            return {"ok": True, "mode": "legacy", "environment": {}}
        if method == "agent.start.preflight":
            return {"ok": True, "resumeCommitGuardVersion": 1}
        if method == "session.register_guarded":
            return {"ok": True, "liveIncumbentGuardApplied": True}
        return {"ok": True}

    class FakeServer:
        pid = 4321

        def __init__(self, socket_path: Path, **kwargs: Any) -> None: ...
        def start(self) -> None: ...
        def stop(self) -> None: ...
        def discover_thread(self) -> tuple[str, str]:
            return THREAD_ID, "thread/list"
        def confirm_resumed_thread(self, thread_id: str) -> tuple[str, str]:
            return thread_id, "thread/resume"
        def resume_thread(self, thread_id: str, *, runtime_args: tuple[str, ...] = (), model: str | None = None):
            seen["resume_args"] = runtime_args
            return thread_id, "thread/resume"

    class FakeProcess:
        def __init__(self, argv: list[str], **_k: object) -> None:
            seen["tui_argv"] = argv
            self.returncode: int | None = None
        def poll(self) -> int | None:
            return self.returncode
        def wait(self, timeout: float | None = None) -> int:
            self.returncode = 0
            return 0
        def terminate(self) -> None: ...
        def kill(self) -> None: ...

    monkeypatch.setenv("HARNESS_CODEX_BIN", "/test/bin/codex")
    monkeypatch.setattr(_cli_get_services(), "_runtime_context_environment", lambda **_k: None)
    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    monkeypatch.setattr("hyprial.daemon.CodexInteractiveAppServer", FakeServer)
    monkeypatch.setattr(_cli_subprocess, "Popen", FakeProcess)
    from hyprial.daemon.impl.mcp.channel import ownership as mcp_channel

    monkeypatch.setattr(mcp_channel, "_read_process_identity", lambda _pid: "proc:1")
    _cli_get_services()._start_interactive_codex(
        name="codex-tui",
        nickname=None,
        cwd=tmp_path,
        resume=resume,
        runtime_args=runtime_args,
        model_provider=None,
        model=None,
        json_output=True,
    )
    return seen


def _approval(argv: list[str] | tuple[str, ...]) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv[:-1]) if a in {"-a", "--ask-for-approval"}]


def test_interactive_start_requests_on_request_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _launch(monkeypatch, tmp_path)
    assert _approval(seen["tui_argv"]) == ["on-request"]


def test_interactive_resume_requests_on_request_approval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _launch(monkeypatch, tmp_path, resume=THREAD_ID)
    assert _approval(seen["tui_argv"]) == ["on-request"]
    assert _approval(seen["resume_args"]) == ["on-request"]


def test_interactive_keeps_an_explicit_asking_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _launch(monkeypatch, tmp_path, runtime_args=("-a", "untrusted"))
    assert _approval(seen["tui_argv"]) == ["untrusted"]


@pytest.mark.parametrize(
    "args",
    [
        ("-a", "never"),
        ("--ask-for-approval", "never"),
        ("--ask-for-approval=never",),
        ("-a=never",),
        ("--dangerously-bypass-approvals-and-sandbox",),
        ("--yolo",),
        ("-anever",),
    ],
)
@pytest.mark.parametrize("resume", [None, THREAD_ID])
def test_interactive_rejects_policies_that_disable_harness_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    args: tuple[str, ...],
    resume: str | None,
) -> None:
    with pytest.raises(_cli_get_services().CliError) as raised:
        _launch(monkeypatch, tmp_path, runtime_args=args, resume=resume)
    assert raised.value.code == ipc_errors.INVALID_ARGUMENT
    assert "on-request" in str(raised.value)
