#!/usr/bin/env python3
"""PR gate: real Pi, Codex, and Claude processes complete local model turns.

Only the provider endpoint is synthetic.  Each scenario uses the real hyprial
CLI, an isolated daemon, and an installed harness binary.  Both supported Pi
and Codex launch modes are exercised because their headless and interactive
start paths have different launch/environment implementations.

The workflow supplied one Claude headless cold-start reading of about 156 s;
the attempted independent sample yielded no readiness measurement.
BUDGET_BASIS=UNMEASURED(补采样受阻:connector running=true 但 pid=null,runner 谓词要求 int pid)
The missing pid is tracked independently as KP-016.
Its 240 s readiness budget includes 84 s of policy margin for the unknown
latency distribution, not a measured upper bound. The agent-start command
retains its independent 90 s budget and all other waits retain 75 s.

Runs 54882/54830 exposed an outer 480 s step bound below the SDK's 600 s
budget. The gate now allows 900 s = 600 s pre-warm + 300 s policy margin
for all five turns, setup and teardown (not a measured worst-case bound).
Pre-warm allows at most two 300 s attempts under one 600 s deadline,
reusing the agent's cache without clearing successfully fetched artifacts.
"""

from __future__ import annotations

import json
import os
import pty
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Scenario discovery loads this file by path, not through the script CLI.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import IsolatedDaemon, hyprial_executable
from _deepseek import local_provider_environment
from _fake_provider import FakeProvider

_REPLY = "LOOPBACK_REPLY"
_TIMEOUT_SECONDS = 75.0
_CLAUDE_HEADLESS_READY_TIMEOUT_SECONDS = 240.0
_NO_PROXY_NAMES = ("NO_PROXY", "no_proxy")
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _loopback_no_proxy(environ: dict[str, str]) -> dict[str, str]:
    """Bypass any proxy for the local provider, and only for it.

    The job's own proxy is inherited on purpose.  Only the model endpoint is
    synthetic: the real Claude worker is launched by the product as
    ``uv run --isolated --with claude-agent-sdk==...`` inside an isolated
    agent HOME, so its first start resolves the SDK from the package index.
    This file used to blank every proxy name.  Once the CI runners lost direct
    egress (after the 2026-10-29 Forgejo host move) that cold resolve hung in
    the worker's ``sdk-ready`` stage with no output until the 60 s start
    budget killed it (run 50549: SIGTERM, empty stderr), so every run failed
    with "harness start did not complete within 60s" while Pi and Codex --
    installed before the gate -- kept passing.  Loopback traffic still never
    touches a proxy: the provider hosts are prepended to NO_PROXY.
    """

    inherited = environ.get("NO_PROXY") or environ.get("no_proxy") or ""
    entries = [entry.strip() for entry in inherited.split(",") if entry.strip()]
    merged = ",".join([*_LOOPBACK_HOSTS, *(e for e in entries if e not in _LOOPBACK_HOSTS)])
    return {name: merged for name in _NO_PROXY_NAMES}


@dataclass(frozen=True)
class Scenario:
    harness: str
    mode: str
    expected_path: str

    @property
    def key(self) -> str:
        return f"{self.harness}-{self.mode}"


SCENARIOS = (
    Scenario("pi", "headless", "/v1/chat/completions"),
    Scenario("pi", "interactive", "/v1/chat/completions"),
    Scenario("codex", "headless", "/v1/responses"),
    Scenario("codex", "interactive", "/v1/responses"),
    Scenario("claude", "headless", "/v1/messages"),
)


def _require_harnesses() -> None:
    missing = [name for name in ("pi", "codex", "claude") if shutil.which(name) is None]
    if missing:
        raise AssertionError(
            "required real harness binaries are missing from PATH: " + ", ".join(missing)
        )


def _git_workspace(path: Path) -> None:
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-q", str(path)],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def _wait_until(
    predicate: Any, description: str, *, timeout_seconds: float = _TIMEOUT_SECONDS
) -> Any:
    started = time.monotonic()
    deadline = started + timeout_seconds
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.25)
    elapsed = time.monotonic() - started
    raise AssertionError(
        f"timed out after {elapsed:.2f}s (budget {timeout_seconds:.2f}s): "
        f"{description}; last={last!r}"
    )


def _actor_uri(daemon: IsolatedDaemon, name: str) -> str:
    agents = daemon.ps().get("agents", [])
    for agent in agents:
        if isinstance(agent, dict) and agent.get("actor") == name:
            uri = agent.get("uri")
            if isinstance(uri, str):
                return uri
    raise AssertionError(f"agent {name!r} has no canonical URI: {agents!r}")


def _wait_headless_ready(daemon: IsolatedDaemon, name: str, harness: str) -> None:
    def ready() -> bool:
        status = daemon.ps()
        connectors = status.get("connectors", [])
        agents = status.get("agents", [])
        connector_ready = any(
            isinstance(item, dict)
            and item.get("name") == name
            and item.get("runtime") == harness
            and item.get("running") is True
            and isinstance(item.get("pid"), int)
            for item in connectors
        )
        agent_ready = any(
            isinstance(item, dict)
            and item.get("actor") == name
            and item.get("status") not in {None, "offline", "starting"}
            for item in agents
        )
        return connector_ready and agent_ready

    timeout_seconds = (
        _CLAUDE_HEADLESS_READY_TIMEOUT_SECONDS if harness == "claude" else _TIMEOUT_SECONDS
    )
    _wait_until(
        ready,
        f"{harness} headless connector {name} to report ready",
        timeout_seconds=timeout_seconds,
    )


def _wait_interactive_ready(daemon: IsolatedDaemon, actor: str, harness: str) -> None:
    expected_source = "pi-extension" if harness == "pi" else "codex-app-server"

    def ready() -> bool:
        sessions = daemon.ps().get("interactiveSessions", [])
        return any(
            isinstance(item, dict)
            and item.get("actor") == actor
            and item.get("source") == expected_source
            for item in sessions
        )

    _wait_until(ready, f"{harness} interactive session for {actor} to register")


class InteractiveProcess:
    """Foreground CLI launch with a real terminal and bounded diagnostics."""

    def __init__(self, argv: list[str], *, cwd: Path, env: dict[str, str]) -> None:
        master, slave = pty.openpty()
        self._master = master
        self._output = bytearray()
        self.process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = os.read(self._master, 4096)
                if not chunk:
                    return
                self._output.extend(chunk)
        except OSError:
            return

    def assert_running(self) -> bool:
        code = self.process.poll()
        if code is not None:
            raise AssertionError(
                f"interactive CLI exited early with {code}: {self.output[-4000:]}"
            )
        return True

    @property
    def output(self) -> str:
        return self._output.decode("utf-8", errors="replace")

    def send_line(self, text: str) -> None:
        os.write(self._master, (text + "\r").encode())

    def stop(self) -> None:
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        os.close(self._master)
        self._reader.join(timeout=1)


def _configure_agent(
    daemon: IsolatedDaemon, scenario: Scenario, name: str, model: str
) -> None:
    daemon.command(
        "agent",
        "create",
        "--name",
        name,
        "--provider",
        "deepseek",
        "--model",
        model,
        "--preferred-harness",
        scenario.harness,
        "--json",
    )
    daemon.command(
        "agent",
        "secret",
        "set",
        "--agent",
        name,
        "--service",
        "deepseek",
        "--field",
        "DEEPSEEK_API_KEY",
        "--no-validate",
        "--json",
        stdin="e2e-local-provider-not-a-real-key\n",
    )
    daemon.request(
        "agent.secret.provider-write",
        {
            "entryId": "loopback-deepseek",
            "fieldName": "apiKey",
            "value": "e2e-local-provider-not-a-real-key",
        },
    )
    daemon.request(
        "agent.secret.grant",
        {
            "actor": name,
            "grantId": "loopback-deepseek",
            "source": "user-provider",
            "entryId": "loopback-deepseek",
            "fieldName": "apiKey",
            "environmentNames": ["DEEPSEEK_API_KEY"],
            "revision": 1,
        },
    )


def _start_args(
    executable: str, scenario: Scenario, name: str, workspace: Path, model: str
) -> list[str]:
    argv = [
        executable,
        "agent",
        "start",
        scenario.harness,
        "--name",
        name,
        "--cwd",
        str(workspace),
        "--provider",
        "deepseek",
        "--model",
        model,
        "--json",
    ]
    if scenario.mode == "headless":
        argv.append("--headless")
    if scenario.harness == "pi":
        argv.append("--approve")
    return argv


def _pending_reply(daemon: IsolatedDaemon, driver: str, worker_actor: str) -> dict[str, Any] | None:
    messages = daemon.request("message.pending.list", {"actor": driver}).get("messages", [])
    for message in messages:
        if (
            isinstance(message, dict)
            and message.get("from") == worker_actor
            and message.get("intent") == "reply"
            and message.get("summary") == _REPLY
        ):
            return message
    return None


def _assert_provider_turn(
    provider: FakeProvider, request_start: int, scenario: Scenario
) -> None:
    paths = provider.requests[request_start:]
    bodies = provider.request_bodies[request_start:]
    if not any(path.startswith(scenario.expected_path) for path in paths):
        raise AssertionError(
            f"{scenario.key} did not call {scenario.expected_path}: paths={paths!r}"
        )
    if "hello" not in json.dumps(bodies, sort_keys=True).lower():
        raise AssertionError(f"{scenario.key} provider requests did not carry hello")


_SDK_PREWARM_TIMEOUT_SECONDS = 600.0


def _prewarm_claude_sdk(daemon: IsolatedDaemon, name: str) -> float:
    """Resolve the Claude Agent SDK into this agent's own uv cache first.

    The product starts the real Claude worker with ``uv run --isolated
    --with claude-agent-sdk==<pin>`` under the agent's isolated XDG roots, so
    a fresh agent downloads the SDK (80.2 MiB, it bundles the CLI) inside the
    daemon's fixed lifecycle deadline (70 s; harness start 60 s).  Through
    the CI proxy that download alone outlasts the deadline (run 50553:
    pydantic-core and cryptography done, claude-agent-sdk still in flight at
    SIGTERM).  This gate tests one turn through each harness, not cold-start
    download speed, so the same command the product runs is executed with
    at most one retry beforehand -- same uv, same pinned SDK, same interpreter,
    same cache directory -- and the worker start then hits a warm cache. Paths come
    from the product's own builders, never a copied layout.
    """

    from hyprial.daemon.impl.harnesses.agent_sdk import sdk_worker_command
    from hyprial.identity.impl.agents.runtime.context import derive_runtime_roots

    agent_home = Path(daemon.root) / "home" / "agents" / name
    if not agent_home.is_dir():
        raise AssertionError(f"agent home for {name} not found at {agent_home}")
    roots = derive_runtime_roots(
        agent_home=agent_home,
        entity_token="prewarm",
        harness="claude",
        config_revision="prewarm",
        tool_profile_id="prewarm",
        config_source=agent_home,
    )
    command = list(sdk_worker_command())
    if command[-2] != "python":
        raise AssertionError(f"unexpected SDK worker command shape: {command!r}")
    command[-1:] = ["-c", "import claude_agent_sdk"]
    env = dict(daemon.env)
    env.pop("UV_CACHE_DIR", None)
    env["XDG_CACHE_HOME"] = str(roots.xdg_cache_home)
    # The daemon refuses agent runtime directories that are not private
    # (AGENT_HOME_ACCESS_FAILED "... is not private"), so every directory this
    # pre-warm creates is 0700, and uv itself runs under umask 077.
    for directory in (roots.xdg_cache_home.parent, roots.xdg_cache_home):
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    started = time.monotonic()
    deadline = started + _SDK_PREWARM_TIMEOUT_SECONDS
    failures: list[str] = []
    rc: int | str = "timeout"
    for attempt in range(1, 3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            completed = subprocess.run(
                command,
                env=env,
                cwd=agent_home,
                capture_output=True,
                text=True,
                timeout=min(_SDK_PREWARM_TIMEOUT_SECONDS / 2, remaining),
                check=False,
                preexec_fn=lambda: os.umask(0o077),
            )
        except subprocess.TimeoutExpired as exc:
            rc = "timeout"
            stderr = exc.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
        else:
            if completed.returncode == 0:
                return round(time.monotonic() - started, 2)
            rc = completed.returncode
            stderr = completed.stderr
        failures.append(f"attempt {attempt} rc={rc}: {stderr[-2000:]}")
        if attempt < 2 and time.monotonic() < deadline:
            print(f"NOTE claude SDK pre-warm {failures[-1]}; retrying", file=sys.stderr, flush=True)
    seconds = round(time.monotonic() - started, 2)
    raise AssertionError(
        f"Claude SDK pre-warm failed rc={rc} after {seconds}s "
        f"({len(failures)} attempts): {'; '.join(failures)}"
    )


def _worker_log_tail(daemon: IsolatedDaemon, name: str, limit: int = 6) -> list[Any]:
    """The worker's own lifecycle records (spawn, readiness, exit + stderr).

    A start that never settles reports only "did not complete within 60s" at
    the CLI; the cause (a worker that exits at once, or one that hangs in a
    stage) is written only to the daemon's per-worker log, which lives in the
    temporary root and is gone when the job ends.  Keep the last records.
    """

    root = getattr(daemon, "root", None)
    if root is None:
        return []
    logs = Path(root) / "state" / "logs" / "workers"
    records: list[Any] = []
    for path in sorted(logs.glob(f"*{name}*.jsonl")) if logs.is_dir() else ():
        for line in path.read_text(errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if str(record.get("event", "")).startswith(("worker.exited", "worker.readiness", "worker.start")):
                records.append(record)
    return records[-limit:]


def _run_scenario(
    daemon: IsolatedDaemon,
    provider: FakeProvider,
    executable: str,
    workspaces: Path,
    scenario: Scenario,
) -> dict[str, Any]:
    name = f"worker-{scenario.key}"
    driver = f"driver-{scenario.key}"
    model = "haiku" if scenario.harness == "claude" else "deepseek-flash"
    workspace = workspaces / scenario.key
    _git_workspace(workspace)
    daemon.command("agent", "create", "--name", driver, "--json")
    _configure_agent(daemon, scenario, name, model)
    if scenario.harness == "claude":
        seconds = _prewarm_claude_sdk(daemon, name)
        print(f"NOTE claude SDK pre-warmed into the agent cache in {seconds}s", file=sys.stderr, flush=True)
    worker_actor = _actor_uri(daemon, name)
    request_start = len(provider.requests)
    interactive: InteractiveProcess | None = None
    started = time.monotonic()
    try:
        argv = _start_args(executable, scenario, name, workspace, model)
        if scenario.mode == "headless":
            result = daemon.command(*argv[1:], timeout=90)
            if result.get("ok") is not True:
                raise AssertionError(f"{scenario.key} start failed: {result!r}")
            _wait_headless_ready(daemon, name, scenario.harness)
        else:
            interactive = InteractiveProcess(argv, cwd=workspace, env=daemon.env)
            actor = _actor_uri(daemon, name)
            _wait_until(interactive.assert_running, f"{scenario.key} CLI to remain live")
            # Both TUIs create their durable session on the first local turn.
            # Prime that lifecycle before sending the turn under test through
            # hyprial; only the later request carrying "hello" is asserted.
            time.sleep(2)
            interactive.send_line("initialize")
            _wait_interactive_ready(daemon, actor, scenario.harness)
        daemon.command(
            "message",
            "send",
            "--from",
            driver,
            "--to",
            name,
            "--summary",
            "hello",
            "hello",
            "--json",
        )
        approval_sent = False

        def reply() -> dict[str, Any] | None:
            nonlocal approval_sent
            pending = _pending_reply(daemon, driver, worker_actor)
            if pending is not None:
                return pending
            if (
                interactive is not None
                and scenario.harness == "codex"
                and "Action Required" in interactive.output
                and not approval_sent
            ):
                # Interactive Codex deliberately requires on-request approval
                # for MCP calls. Confirm the default one-shot approval in the
                # real TUI instead of weakening the product launch policy.
                interactive.send_line("")
                approval_sent = True
            return None

        _wait_until(reply, f"{scenario.key} reply to arrive through the daemon")
        _assert_provider_turn(provider, request_start, scenario)
        return {
            "scenario": scenario.key,
            "providerPath": next(
                path
                for path in provider.requests[request_start:]
                if path.startswith(scenario.expected_path)
            ),
            "reply": _REPLY,
            "seconds": round(time.monotonic() - started, 2),
        }
    except Exception as error:
        serialized_bodies = json.dumps(provider.request_bodies[request_start:])
        diagnostics = {
            "providerPaths": provider.requests[request_start:],
            "providerBodyCount": len(provider.request_bodies[request_start:]),
            "providerConversations": [
                str(body.get("messages") or body.get("input"))[-4000:]
                for body in provider.request_bodies[request_start:]
                if isinstance(body, dict)
            ],
            "providerMessageIdContexts": [
                serialized_bodies[max(0, match.start() - 80) : match.end() + 80]
                for match in __import__("re").finditer(
                    r"[0-9a-f]{8}-[0-9a-f-]{28,36}", serialized_bodies
                )
            ][:12],
            "driverPending": daemon.request(
                "message.pending.list", {"actor": driver}
            ),
            "workerPending": daemon.request(
                "message.pending.list", {"actor": name}
            ),
            "interactiveSessions": daemon.ps().get("interactiveSessions", []),
            "workerLog": _worker_log_tail(daemon, name),
        }
        error.add_note("loopback diagnostics:\n" + json.dumps(diagnostics, default=str)[-8000:])
        if interactive is not None and interactive.output:
            error.add_note("interactive output:\n" + interactive.output[-4000:])
        raise
    finally:
        if interactive is not None:
            interactive.stop()
        if scenario.mode == "headless":
            daemon.command("agent", "stop", name, "--json", exits=(0, 1))


def main() -> int:
    _require_harnesses()
    executable = hyprial_executable()
    with (
        tempfile.TemporaryDirectory(prefix="hr-", dir="/tmp") as root_text,
        FakeProvider(reply=_REPLY, reply_to_harness_requests=True) as provider,
    ):
        root = Path(root_text)
        results: list[dict[str, Any]] = []
        for scenario in SCENARIOS:
            daemon = IsolatedDaemon(
                executable,
                root / scenario.key / "node",
                f"e2e-loopback-{scenario.key}",
            )
            daemon.env.update(
                local_provider_environment(
                    openai_base_url=provider.openai_base_url,
                    anthropic_base_url=provider.anthropic_base_url,
                )
            )
            daemon.env.update(_loopback_no_proxy(daemon.env))
            try:
                daemon.start()
                result = _run_scenario(
                    daemon,
                    provider,
                    executable,
                    root / scenario.key / "workspaces",
                    scenario,
                )
                results.append(result)
                print("PASS " + json.dumps(result, sort_keys=True), flush=True)
            finally:
                daemon.stop()
    print(json.dumps({"ok": True, "scenarios": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
