from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from hyprial.daemon.impl.api import (
    HarnessDelivery,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.codex import (
    CodexAppServerClient,
    CodexAppServerProcess,
    _OwnedProcessGroup,
    _parse_linux_process_stat,
)
from hyprial.daemon.impl.harnesses.worker_channel import WorkerChannel, build_worker_channel
from hyprial.kernel.impl.log import Logger

FAKE_CODEX = Path(__file__).resolve().parents[5] / 'tests/fixtures/harnesses/fake_codex_app_server.py'


class _FakeWindowsProcessGroup:
    def __init__(self, *, label: str) -> None:
        self.label = label
        self.spawned: tuple[object, ...] | None = None

    async def spawn(self, command: tuple[str, ...], **options: object):
        self.spawned = (command, options)
        return object()

    def force_close(self) -> None:
        return None

    def stopped(self) -> bool:
        return True

    def liveness(self) -> None:
        return None


def _fake_windows_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("hyprial.kernel.impl.platform.windows_owned_process")
    module.WindowsOwnedProcessGroup = _FakeWindowsProcessGroup
    monkeypatch.setitem(sys.modules, module.__name__, module)
    # Do not mutate the process-global os.name: pathlib.Path would become
    # WindowsPath on a POSIX host and break unrelated product code.  Give the
    # codex process module a local os facade instead.
    process_module = importlib.import_module("hyprial.daemon.impl.harnesses.codex.process")
    class _WindowsOs:
        name = "nt"
        def __getattr__(self, name: str):
            return getattr(os, name)
    monkeypatch.setattr(process_module, "os", _WindowsOs())
    monkeypatch.setattr("hyprial.kernel.impl.platform.ports.sys.platform", "win32")








def spec(
    *,
    args: tuple[str, ...] = (),
    session_ref: str | None = None,
    cwd: str | None = None,
) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(
        harness="codex",
        name="codex-worker",
        headless=True,
        args=args,
        cwd=cwd or str(Path.cwd()),
        session_ref=session_ref,
    )


def delivery(delivery_id: str, message: str) -> HarnessDelivery:
    return HarnessDelivery(
        delivery_id=delivery_id,
        conversation_id="conversation-1",
        sender="requester",
        recipient="agent:op:node-1:codex-worker",
        message=message,
    )


def _prompt(body: str) -> str:
    """Header plus ``body``: the exact prompt for a ``delivery()``; the fake echoes it + " done"."""
    return f"[Harness Network message from requester to agent:op:node-1:codex-worker]\n{body}"


def fake_process(
    mode: str,
    *,
    log_path: Path | None = None,
    state_path: Path | None = None,
    release_path: Path | None = None,
) -> CodexAppServerProcess:
    environment = {"FAKE_CODEX_MODE": mode}
    if log_path is not None:
        environment["FAKE_CODEX_LOG"] = str(log_path)
    if state_path is not None:
        environment["FAKE_CODEX_STATE"] = str(state_path)
    if release_path is not None:
        environment["FAKE_CODEX_RELEASE"] = str(release_path)
    launch = spec()
    return CodexAppServerProcess(
        launch,
        command=(sys.executable, str(FAKE_CODEX)),
        env=environment,
        reconnect_delay_seconds=0.01,
    )


def wait_for_results(
    process: CodexAppServerProcess, count: int, timeout: float = 5.0
):
    deadline = time.monotonic() + timeout
    results = []
    while time.monotonic() < deadline:
        results.extend(process.drain_results())
        if len(results) >= count:
            return results
        time.sleep(0.01)
    pytest.fail(f"Codex app-server pump produced {len(results)}/{count} results")


def read_log(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return True
        observed = _parse_linux_process_stat(stat)
        return observed is None or observed[0] not in {"X", "Z"}
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["ps", "-o", "state=", "-p", str(pid)],
                check=False,
                capture_output=True,
                text=True,
                timeout=1.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            return True
        state = result.stdout.strip()
        if result.returncode != 0 and not state:
            return False
        return not state or state[0] not in {"X", "Z"}
    return True


def wait_for_pid_file(
    path: Path,
    *,
    description: str,
    leader: subprocess.Popen[bytes] | None = None,
    timeout: float = 5.0,
) -> int:
    """Wait until a spawned process has published a complete PID value."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if leader is not None:
            assert leader.poll() is None, f"{description} exited before publishing PID"
        try:
            raw_pid = path.read_text(encoding="utf-8").strip()
            pid = int(raw_pid)
        except (FileNotFoundError, ValueError):
            time.sleep(0.01)
            continue
        assert pid > 0, f"{description} published invalid PID {pid}"
        return pid
    pytest.fail(f"{description} did not publish PID")


def spawn_owned_group(tmp_path: Path) -> tuple[subprocess.Popen[bytes], int]:
    child_pid_path = tmp_path / "owned-child.pid"
    script = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
        f"open({str(child_pid_path)!r},'w').write(str(child.pid)); "
        "time.sleep(120)"
    )
    leader = subprocess.Popen(
        [sys.executable, "-c", script],
        start_new_session=True,
    )
    child_pid = wait_for_pid_file(
        child_pid_path,
        description="owned process child",
        leader=leader,
    )
    return leader, child_pid


def assert_owned_group_gone(leader: subprocess.Popen[bytes], child_pid: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        leader_gone = leader.poll() is not None
        group_running = _OwnedProcessGroup._group_has_live_members(leader.pid)
        if leader_gone and not group_running and not process_is_running(child_pid):
            return
        time.sleep(0.01)
    pytest.fail(
        f"owned group survived stop: leader={leader.poll()} "
        f"group_running={group_running} child_running={process_is_running(child_pid)}"
    )


def assert_process_group_gone(pid: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    pytest.fail(f"process group {pid} survived stop deadline")






































































def _wire_worker_channel(tmp_path: Path) -> WorkerChannel:
    return build_worker_channel(
        actor="agent:op:node-1:codex-worker",
        session_ref="session-ref-1",
        hyprial_home=tmp_path / "home",
        state_dir=tmp_path / "state",
    )








# ---------------------------------------------------------------------------
# git metadata writable roots (spec
# notes/spec-codex-worker-git-commit-2026-09-14.md, acceptance 1/3/6)
# ---------------------------------------------------------------------------


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README").write_text("probe\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.email=probe@hyprial.local",
            "-c",
            "user.name=probe",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    return path










































def _elicitation(server: str, kind: str | None = "mcp_tool_call") -> dict[str, object]:
    """The measured shape of codex's MCP tool-call approval (0.152.0)."""
    meta: dict[str, object] = {
        "persist": ["session", "always"],
        "tool_params": {"text": "PING"},
    }
    if kind is not None:
        meta["codex_approval_kind"] = kind
    return {
        "threadId": "t",
        "turnId": "u",
        "serverName": server,
        "mode": "form",
        "_meta": meta,
        "message": f'Allow the {server} MCP server to run tool "echo"?',
        "requestedSchema": {"type": "object", "properties": {}},
    }




















def _delayed_thread_start_client(
    tmp_path: Path, *, delay: float, request_timeout: float, thread_start_timeout: float
) -> CodexAppServerClient:
    """A fake app-server that answers thread/start only after ``delay`` seconds."""
    code = """
import json, os, sys, time
delay = float(os.environ["FAKE_THREAD_START_DELAY"])
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    identifier = message.get('id')
    if method == 'initialize':
        print(json.dumps({'id': identifier, 'result': {}}), flush=True)
    elif method == 'thread/start':
        time.sleep(delay)
        print(json.dumps({'id': identifier, 'result': {'thread': {'id': 'thread-slow'}}}), flush=True)
"""

    return CodexAppServerClient(
        spec(),
        command=(sys.executable, "-c", code),
        env={"FAKE_THREAD_START_DELAY": str(delay)},
        request_timeout_seconds=request_timeout,
        thread_start_timeout_seconds=thread_start_timeout,
        process_group=_OwnedProcessGroup(),
        logger=Logger.worker(tmp_path / "state", runtime="codex", name="codex-slow"),
    )












# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'FAKE_CODEX',
    '_FakeWindowsProcessGroup',
    '_delayed_thread_start_client',
    '_elicitation',
    '_fake_windows_process_group',
    '_git_repo',
    '_prompt',
    '_wire_worker_channel',
    'assert_owned_group_gone',
    'assert_process_group_gone',
    'delivery',
    'fake_process',
    'process_is_running',
    'read_log',
    'spawn_owned_group',
    'spec',
    'wait_for_pid_file',
    'wait_for_results',
]
