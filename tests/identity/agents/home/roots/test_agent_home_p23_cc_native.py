"""P23-cc native loader evidence against the pinned SDK and real Claude CLI.

The test uses only non-secret sentinels and a loopback Anthropic endpoint.  A
real SDK/CLI request is deliberately answered with HTTP 401: the captured
request proves which config and explicit auth sources reached the loader without
claiming a real authenticated subject or a successful model turn.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from hyprial.identity.impl.agents import AgentRegistry
from hyprial.identity.impl.agents.home.environment import (
    ChildEnvironmentLaunch,
    CompleteChildEnvironment,
    compose_worker_child_launch,
)
from hyprial.identity.impl.agents.runtime.context import (
    AgentRuntimeContext,
    resolve_agent_runtime_context,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.runtime import tmux as tmux_mod
from hyprial.daemon.impl.harnesses.agent_sdk import IsolatedAgentSdkClient
from hyprial.daemon.impl.harnesses.claude.runtime import (
    claude_runtime_session_file,
    prepare_claude_runtime_context,
)
from hyprial.daemon.impl.harnesses.worker_channel import WorkerChannel, build_worker_channel
from tests.support.identity.tool_profile import TEST_TOOL_PROFILE

pytestmark = pytest.mark.skipif(
    os.environ.get("AGENT_HOME_P23_CC_NATIVE") != "1",
    reason="set AGENT_HOME_P23_CC_NATIVE=1 for the real SDK/Claude loader probe",
)

_API_KEY = "P23_CC_API_KEY_NOT_A_SECRET"
_OAUTH_TOKEN = "sk-ant-oat01-P23_CC_OAUTH_TOKEN_NOT_A_SECRET"
_USER_MODEL = "p23-cc-user-model-sentinel"
_PROJECT_MODEL = "p23-cc-project-model-sentinel"
_USER_CONTEXT = "P23_CC_USER_CONTEXT_SENTINEL"
_PROJECT_CONTEXT = "P23_CC_PROJECT_CONTEXT_SENTINEL"
_HOST_CONTEXT = "P23_CC_HOST_CONTEXT_MUST_NOT_LOAD"


class _CaptureServer(ThreadingHTTPServer):
    requests: list[dict[str, Any]]
    observed: threading.Event


class _CaptureHandler(BaseHTTPRequestHandler):
    server: _CaptureServer

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length)
        try:
            body: object = json.loads(raw)
        except json.JSONDecodeError:
            body = {"unparsedBytes": len(raw)}
        self.server.requests.append(
            {
                "path": self.path,
                "apiKey": self.headers.get("x-api-key"),
                "authorization": self.headers.get("authorization"),
                "body": body,
            }
        )
        self.server.observed.set()
        rendered = json.dumps(body, sort_keys=True)
        if 'single \\"title\\" field' in rendered:
            # Claude Code asks for a session title before its main turn.  This
            # is the documented Anthropic Messages streaming event sequence,
            # not a made-up loader receipt; answering it lets the real CLI
            # proceed to the request whose system prompt carries CLAUDE.md.
            events = (
                'event: message_start\ndata: {"type":"message_start","message":'
                '{"id":"msg_p23cc","type":"message","role":"assistant",'
                '"content":[],"model":"p23-cc-loopback","stop_reason":null,'
                '"stop_sequence":null,"usage":{"input_tokens":1,'
                '"output_tokens":0}}}\n\n'
                'event: content_block_start\ndata: {"type":"content_block_start",'
                '"index":0,"content_block":{"type":"text","text":""}}\n\n'
                'event: content_block_delta\ndata: {"type":"content_block_delta",'
                '"index":0,"delta":{"type":"text_delta","text":'
                '"{\\"title\\":\\"Probe Session\\"}"}}\n\n'
                'event: content_block_stop\ndata: {"type":"content_block_stop",'
                '"index":0}\n\n'
                'event: message_delta\ndata: {"type":"message_delta","delta":'
                '{"stop_reason":"end_turn","stop_sequence":null},"usage":'
                '{"output_tokens":5}}\n\n'
                'event: message_stop\ndata: {"type":"message_stop"}\n\n'
            ).encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(events)))
            self.end_headers()
            self.wfile.write(events)
            return
        payload = json.dumps(
            {
                "type": "error",
                "error": {
                    "type": "authentication_error",
                    "message": "P23-cc loopback refusal",
                },
            }
        ).encode()
        self.send_response(401)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *args: object) -> None:
        return


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _configured_launch(
    tmp_path: Path, *, endpoint: str, claude: str, auth_mode: str
) -> tuple[
    AgentRegistry,
    AgentRuntimeContext,
    WorkerChannel,
    ChildEnvironmentLaunch,
    Path,
]:
    personality = tmp_path / "personality"
    project = tmp_path / "project"
    host_home = tmp_path / "host-home"

    _write_json(
        personality / "native" / "claude" / "settings.json",
        {"model": _USER_MODEL},
    )
    _write_text(personality / "native" / "claude" / "CLAUDE.md", _USER_CONTEXT)
    _write_text(
        personality / "shared" / "skills" / "p23-native" / "SKILL.md",
        "# P23 native skill\n",
    )
    _write_json(project / ".claude" / "settings.json", {"model": _PROJECT_MODEL})
    _write_text(project / "CLAUDE.md", _PROJECT_CONTEXT)
    _write_json(
        project / ".mcp.json",
        {
            "mcpServers": {
                "project-must-not-start": {
                    "command": "/usr/bin/touch",
                    "args": [str(tmp_path / "project-mcp-started")],
                }
            }
        },
    )

    # Both baits stay inside tmp_path.  They are evidence that redirecting the
    # user slot worked; neither value is a production credential.
    _write_text(host_home / ".claude" / "CLAUDE.md", _HOST_CONTEXT)
    _write_json(
        host_home / ".claude" / ".credentials.json",
        {"claudeAiOauth": {"accessToken": "P23_CC_HOST_TOKEN_NOT_A_SECRET"}},
    )

    hyprial_home = tmp_path / "hyprial"
    hyprial_home.mkdir()
    registry = AgentRegistry(
        tmp_path / "registry" / "agents.sqlite3",
        owner="p23cc",
        machine="test",
        hyprial_home=hyprial_home,
    )
    registry.create(
        "native-loader",
        cwd=str(project),
        config={
            "sources": [{"path": str(personality), "required": True}],
            "discovery": "explicit-only",
        },
    )
    registry.ensure_home("native-loader")
    context = resolve_agent_runtime_context(
        registry=registry,
        agent_name="native-loader",
        harness="claude",
        cwd=str(project),
        tool_profile=TEST_TOOL_PROFILE,
    )
    assert context is not None
    channel = build_worker_channel(
        actor=registry.require("native-loader").uri,
        session_ref="p23cc-native-loader",
        hyprial_home=hyprial_home,
        state_dir=tmp_path / "state",
        runtime_context=context,
    )
    launch = compose_worker_child_launch(
        registry=registry,
        hyprial_home=hyprial_home,
        channel=channel,
        environ={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        agent_name="native-loader",
    )
    environment = {
        **launch.environment.for_exec(),
        **(
            {"ANTHROPIC_API_KEY": _API_KEY}
            if auth_mode == "api-key"
            else {"CLAUDE_CODE_OAUTH_TOKEN": _OAUTH_TOKEN}
        ),
        "ANTHROPIC_BASE_URL": endpoint,
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "HTTPS_PROXY": "",
        "HTTP_PROXY": "",
        "HYPRIAL_AGENT_SDK_CONNECT_TIMEOUT_SECONDS": "20",
        "HYPRIAL_CLAUDE_EXECUTABLE": claude,
        "NO_PROXY": "127.0.0.1,localhost",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "UV_CACHE_DIR": "/tmp/ah-p23-cc-uv-cache",
        "https_proxy": "",
        "http_proxy": "",
        "no_proxy": "127.0.0.1,localhost",
    }
    launch = dataclasses.replace(
        launch,
        environment=CompleteChildEnvironment(tuple(sorted(environment.items()))),
    )
    return registry, context, channel, launch, host_home


@pytest.mark.parametrize("auth_mode", ["api-key", "oauth-token"])
@pytest.mark.anyio
async def test_real_sdk_and_claude_load_redirected_user_and_project_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth_mode: str
) -> None:
    claude = shutil.which("claude")
    uv = shutil.which("uv")
    if claude is None or uv is None:
        pytest.skip("real claude and uv executables are required")

    server = _CaptureServer(("127.0.0.1", 0), _CaptureHandler)
    server.requests = []
    server.observed = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    registry, context, channel, launch, host_home = _configured_launch(
        tmp_path, endpoint=endpoint, claude=claude, auth_mode=auth_mode
    )
    native_root = context.roots.native_root
    assert not list(native_root.rglob(".credentials.json"))
    project = tmp_path / "project"
    monkeypatch.setenv("HOME", str(host_home))
    client = IsolatedAgentSdkClient(
        HarnessLaunchSpec(
            harness="claude",
            name="p23cc-native-loader",
            headless=True,
            cwd=str(project),
        ),
        uv_executable=uv,
        worker_channel=channel,
        complete_launch=launch,
        startup_timeout_seconds=90,
    )

    try:
        try:
            async with client:
                await client.query("Return one short word.")
                try:
                    async with asyncio.timeout(30):
                        _ = [message async for message in client.receive_response()]
                except (ConnectionError, TimeoutError):
                    # The loopback endpoint intentionally rejects auth.  The
                    # request, not a fabricated success response, is the evidence.
                    pass
        except TimeoutError:
            stderr = bytes(client._stderr_tail).decode(errors="replace")
            pytest.fail(f"real SDK worker startup timed out: {stderr}")
        assert await asyncio.to_thread(server.observed.wait, 5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        registry.close()

    assert server.requests
    request = next(
        (
            candidate
            for candidate in server.requests
            if _PROJECT_CONTEXT in json.dumps(candidate["body"], sort_keys=True)
        ),
        server.requests[-1],
    )
    assert request["path"].split("?", 1)[0].endswith("/v1/messages")
    if auth_mode == "api-key":
        assert request["apiKey"] == _API_KEY
        assert request["authorization"] in {None, ""}
    else:
        assert request["apiKey"] in {None, ""}
        assert request["authorization"] == f"Bearer {_OAUTH_TOKEN}"
    body = request["body"]
    assert isinstance(body, dict)
    assert body["model"] == _PROJECT_MODEL
    rendered = json.dumps(body, sort_keys=True)
    assert _USER_CONTEXT in rendered
    assert _PROJECT_CONTEXT in rendered
    assert _HOST_CONTEXT not in rendered
    assert not (tmp_path / "project-mcp-started").exists()

    assert not list(native_root.rglob(".credentials.json"))
    transcripts = sorted(context.roots.session_root.glob("*/*.jsonl"))
    assert len(transcripts) == 1
    transcript = transcripts[0]
    assert (
        claude_runtime_session_file(context, str(project), transcript.stem)
        == transcript
    )
    assert (native_root / "projects").is_symlink()
    assert (native_root / "projects").readlink() == context.roots.session_root
    assert not list((tmp_path / "host-home" / ".claude").glob("projects/*/*.jsonl"))
    print(
        "P23_CC_SDK_OBSERVATION "
        + json.dumps(
            {
                "authMode": auth_mode,
                "path": request["path"].split("?", 1)[0],
                "model": body["model"],
                "userContext": _USER_CONTEXT in rendered,
                "projectContext": _PROJECT_CONTEXT in rendered,
                "hostContext": _HOST_CONTEXT in rendered,
                "authHeader": "x-api-key"
                if auth_mode == "api-key"
                else "authorization: Bearer",
                "nativeCredentials": bool(list(native_root.rglob(".credentials.json"))),
                "transcripts": len(transcripts),
            },
            sort_keys=True,
        )
    )


def test_real_tmux_claude_tui_uses_redirected_oauth_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = shutil.which("claude")
    tmux = shutil.which("tmux")
    if claude is None or tmux is None:
        pytest.skip("real claude and tmux executables are required")
    server = _CaptureServer(("127.0.0.1", 0), _CaptureHandler)
    server.requests = []
    server.observed = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    registry, context, _channel, launch, host_home = _configured_launch(
        tmp_path,
        endpoint=f"http://127.0.0.1:{server.server_port}",
        claude=claude,
        auth_mode="oauth-token",
    )
    prepare_claude_runtime_context(context)
    native_root = context.roots.native_root
    assert not list(native_root.rglob(".credentials.json"))
    monkeypatch.setenv("HOME", str(host_home))
    wrapper = tmp_path / "isolated-tmux"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(tmux)} -L {shlex.quote("p23cc-" + str(os.getpid()))} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    session = "claude-oauth-token"
    environment = {
        **launch.environment.for_exec(),
        "HOME": str(host_home),
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "TERM": "xterm-256color",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    }
    config = tmp_path / "empty-mcp.json"
    _write_json(config, {"mcpServers": {}})
    argv = [
        claude,
        "--setting-sources",
        "user,project,local",
        "--mcp-config",
        str(config),
        "--strict-mcp-config",
    ]
    pane = ""
    try:
        tmux_mod.new_detached_session(
            str(wrapper),
            session,
            ["/bin/sh", "-c", shlex.join(argv) + "; echo CLAUDE_EXIT:$?; sleep 60"],
            cwd=tmp_path / "project",
            env=environment,
        )
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline and not server.observed.is_set():
            pane = tmux_mod.pane_text(str(wrapper), session)
            if "CLAUDE_EXIT:" in pane:
                break
            if "trust this folder" in pane.lower():
                subprocess.run(
                    [str(wrapper), "send-keys", "-t", session, "Enter"], check=True
                )
            elif "loading development channels" in pane.lower():
                subprocess.run(
                    [str(wrapper), "send-keys", "-t", session, "1", "Enter"], check=True
                )
            elif "❯" in pane or "Type a message" in pane:
                subprocess.run(
                    [
                        str(wrapper),
                        "send-keys",
                        "-t",
                        session,
                        "Return one short word.",
                        "Enter",
                    ],
                    check=True,
                )
            time.sleep(0.5)
        assert not list(native_root.rglob(".credentials.json"))
        print(
            "P23_CC_TUI_STARTUP "
            + json.dumps(
                {
                    "claudeExited": "CLAUDE_EXIT:" in pane,
                    "startup403": "api.anthropic.com: Status 403" in pane,
                    "nativeCredentials": bool(
                        list(native_root.rglob(".credentials.json"))
                    ),
                    "loopbackRequests": len(server.requests),
                },
                sort_keys=True,
            )
        )
        if "api.anthropic.com: Status 403" in pane and not server.observed.is_set():
            pytest.xfail("Claude TUI startup service check returned 403 before a turn")
        assert server.observed.is_set(), f"TUI did not issue a request; pane={pane!r}"
    finally:
        tmux_mod.kill_session(str(wrapper), session)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        registry.close()
    request = next(
        (
            item
            for item in server.requests
            if _PROJECT_CONTEXT in json.dumps(item["body"], sort_keys=True)
        ),
        server.requests[-1],
    )
    body = request["body"]
    assert isinstance(body, dict)
    rendered = json.dumps(body, sort_keys=True)
    assert request["path"].split("?", 1)[0].endswith("/v1/messages")
    assert request["apiKey"] in {None, ""}
    assert request["authorization"] == f"Bearer {_OAUTH_TOKEN}"
    assert body["model"] == _PROJECT_MODEL
    assert _USER_CONTEXT in rendered
    assert _PROJECT_CONTEXT in rendered
    assert _HOST_CONTEXT not in rendered
    assert not list(native_root.rglob(".credentials.json"))
    assert not (tmp_path / "project-mcp-started").exists()
    print(
        "P23_CC_TUI_OBSERVATION "
        + json.dumps(
            {
                "path": request["path"].split("?", 1)[0],
                "model": body["model"],
                "userContext": _USER_CONTEXT in rendered,
                "projectContext": _PROJECT_CONTEXT in rendered,
                "hostContext": _HOST_CONTEXT in rendered,
                "authHeader": "authorization: Bearer",
                "nativeCredentials": bool(list(native_root.rglob(".credentials.json"))),
            },
            sort_keys=True,
        )
    )
