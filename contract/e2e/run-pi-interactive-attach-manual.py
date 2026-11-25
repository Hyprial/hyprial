#!/usr/bin/env python3
"""Pi interactive attach manual leg: real pi TUI + real Harness round-trip.

Attended layer-2/3 companion of the layer-1 contracts.  Flow:

  preflight  pi + tmux + hyprial executable must exist
  temp-state disposable HYPRIAL_HOME/HARNESS_STATE_DIR/workspaces (printed for
             the wrapper's last-resort cleanup)
  daemon     worker daemon (zenoh listen) + sender daemon (connect), both
             isolated; actor identity is read back from `ps`
  tui        `hyprial agent start pi --name <bare>` inside tmux; the leg waits for the
             carrier's session.register (source pi-extension) and surfaces
             pi's project-trust prompt as a MANUAL_ACTION if it appears
  inject     a marked message is sent from the sender daemon while Allen is
             asked to keep typing: the followUp must not interrupt him
             (manual observation), then the worker must answer explicitly
             (the carrier never auto-replies; no automatic reply is the
             expected outcome) with the marker in the final text
             (daemon-side assertion over the real two-daemon zenoh path)
  unregister Allen exits the TUI (Ctrl+D); the session must disappear
  cleanup    tmux + both daemons + temp root; failure state is preserved

Output contract (shared with the codex leg): STAGE=<name> START|OK|FAIL|
BLOCKED [detail], MANUAL_ACTION=<text>, RESULT PASS|FAIL|BLOCKED.
Exit codes: 0 PASS, 1 FAIL, 2 BLOCKED.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _daemon_runner import (  # noqa: E402
    IsolatedDaemon,
    env_strip_flags,
    free_tcp_endpoint,
    hyprial_executable,
    preserve_failure_state,
    sanitized_env,
    short_temp_root,
)
from _deepseek import (  # noqa: E402
    MODEL as DEEPSEEK_MODEL,
    PROVIDER as DEEPSEEK_PROVIDER,
)


class Blocked(RuntimeError):
    """A missing real-environment prerequisite."""


class ProbeFailure(RuntimeError):
    """A measured product or protocol assertion failed."""


def emit_stage(name: str, status: str, detail: str | None = None) -> None:
    suffix = f" {detail}" if detail else ""
    print(f"STAGE={name} {status}{suffix}", flush=True)


def emit_manual(text: str) -> None:
    print(f"MANUAL_ACTION={text}", flush=True)


def run(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def tmux_pane(session: str) -> str:
    captured = run("tmux", "capture-pane", "-p", "-S", "-300", "-t", session)
    return captured.stdout if captured.returncode == 0 else captured.stderr


def tmux_alive(session: str) -> bool:
    result = run("tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_dead}")
    return any(
        name == session and dead != "1"
        for name, _, dead in (line.partition("\t") for line in result.stdout.splitlines())
    )


def stop_tui(session: str) -> None:
    if tmux_alive(session):
        # Ctrl+D is pi's exit; fall back to killing the pane if it stalls.
        run("tmux", "send-keys", "-t", session, "C-d")
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and tmux_alive(session):
            time.sleep(0.2)
    if run("tmux", "has-session", "-t", session).returncode == 0:
        run("tmux", "kill-session", "-t", session)


def stop_daemon(daemon: IsolatedDaemon) -> None:
    """Stop a test daemon, accepting only a proved already-dead stale socket."""

    metadata_path = daemon.root / "state" / "daemon.json"
    if metadata_path.exists():
        try:
            daemon.stop()
            return
        except AssertionError:
            metadata = __import__("json").loads(
                metadata_path.read_text(encoding="utf-8")
            )
            pid = metadata.get("pid")
            if isinstance(pid, int):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    (daemon.root / "state" / "daemon.sock").unlink(missing_ok=True)
                    return
            raise


def wait_until(
    probe: Any,
    accept: Any,
    *,
    timeout: float,
    label: str,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = probe()
        if accept(last):
            return last
        time.sleep(0.3)
    raise ProbeFailure(f"timeout waiting for {label}; last={str(last)[:400]!r}")


def interactive_session(daemon: IsolatedDaemon, actor: str) -> dict[str, Any] | None:
    status = daemon.ps()
    sessions = status.get("interactiveSessions", [])
    if not isinstance(sessions, list):
        raise ProbeFailure(f"ps interactiveSessions is invalid: {status}")
    for item in sessions:
        if isinstance(item, dict) and item.get("actor") == actor:
            return item
    return None


def pending(daemon: IsolatedDaemon, actor: str) -> list[dict[str, Any]]:
    result = daemon.request("message.pending.list", {"actor": actor})
    messages = result.get("messages")
    if not isinstance(messages, list):
        raise ProbeFailure(f"invalid pending response: {result}")
    return messages


def tmux_env(env: dict[str, str]) -> list[str]:
    return [
        "env",
        *env_strip_flags(),
        f"HYPRIAL_HOME={env['HYPRIAL_HOME']}",
        f"HARNESS_STATE_DIR={env['HARNESS_STATE_DIR']}",
        "TERM=xterm-256color",
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup-timeout", type=float, default=300.0)
    parser.add_argument("--manual-timeout", type=float, default=300.0)
    parser.add_argument("--model", default=DEEPSEEK_MODEL)
    args = parser.parse_args()

    stage = "boot"
    result = "FAIL"
    root: Path | None = None
    session: str | None = None
    daemons: list[IsolatedDaemon] = []
    try:
        stage = "preflight"
        emit_stage(stage, "START")
        if shutil.which("tmux") is None:
            raise Blocked("tmux is required for a real interactive PTY")
        if shutil.which("pi") is None:
            raise Blocked("pi executable is not installed")
        executable = hyprial_executable()
        pi_version = run("pi", "--version", timeout=10)
        version_text = (pi_version.stdout or pi_version.stderr).strip().splitlines()
        print(f"CLI_VERSION=pi {version_text[0] if version_text else 'unknown'}", flush=True)
        emit_stage(stage, "OK", "pi+tmux+hyprial")

        stage = "temp-state"
        emit_stage(stage, "START")
        root = Path(tempfile.mkdtemp(prefix="hyprial-pi-attach-", dir=short_temp_root()))
        emit_stage(stage, "OK", f"root={root}")

        stage = "daemon"
        emit_stage(stage, "START")
        nonce = uuid4().hex[:10]
        endpoint = free_tcp_endpoint()
        worker = IsolatedDaemon(
            executable, root / "worker", f"pi-worker-{nonce}", listen=endpoint
        )
        worker.env = sanitized_env(worker.env)
        sender = IsolatedDaemon(
            executable, root / "sender", f"pi-sender-{nonce}", connect=endpoint
        )
        sender.env = sanitized_env(sender.env)
        worker.start()
        daemons.append(worker)
        sender.start()
        daemons.append(sender)
        daemon_info = worker.ps().get("daemon", {})
        owner = str(daemon_info.get("owner") or "").strip()
        node_id = str(daemon_info.get("nodeId") or "").strip()
        if not owner or not node_id or ":" in owner or ":" in node_id:
            raise Blocked(f"worker daemon reported no usable identity: {daemon_info}")

        bare_name = f"pi-tui-{nonce}"
        actor = f"agent:{owner}:{node_id}:{bare_name}"
        sender_name = f"pi-sender-{nonce}"
        marker = f"PI-ATTACH-{nonce.upper()}"

        # A CLI-minted sender identity has NO network route (no liveliness
        # token, no inbox endpoint), so a reply to it parks in the worker's
        # outbox forever (attempts=0 while nextAttempt advances). Give the
        # sender a daemon-owned interactive route on the sender daemon: the
        # raw token outlives the heartbeat TTL for remote peers, and the
        # endpoint stores reply rows for pending queries. Source "e2e" is the
        # honest fixture marker -- it never refreshes or heartbeats.
        sender.request(
            "session.register",
            {
                "actor": sender_name,
                "sessionRef": f"sender-{nonce}",
                "cwd": str(root),
                "command": ["e2e-sender"],
                "source": "e2e",
                "runtime": "e2e_interactive",
            },
        )
        metadata = __import__("json").loads(
            (worker.root / "state" / "daemon.json").read_text(encoding="utf-8")
        )
        print(f"STAGE=daemon PID pid={metadata.get('pid')}", flush=True)
        emit_stage(stage, "OK", f"node={node_id}")

        stage = "tui"
        emit_stage(stage, "START")
        session = f"hyprial-pi-attach-{os.getpid()}"
        workspace = worker.root / "pi-workspace"
        workspace.mkdir(mode=0o700, exist_ok=True)
        command = [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session,
            "-c",
            str(workspace),
            "--",
            *tmux_env(worker.env),
            str(executable),
            "agent",
            "start",
            "pi",
            "--name",
            bare_name,
            "--cwd",
            str(workspace),
            "--provider",
            DEEPSEEK_PROVIDER,
            "--model",
            args.model,
        ]
        started = run(*command)
        if started.returncode != 0:
            raise ProbeFailure(f"tmux start failed: {started.stderr[-1000:]}")
        run("tmux", "set-option", "-w", "-t", session, "remain-on-exit", "on")
        print(f"MANUAL_TMUX_ATTACH=tmux attach -t {session}", flush=True)

        trust_hint_printed = False

        def registered() -> dict[str, Any] | None:
            nonlocal trust_hint_printed
            found = interactive_session(worker, actor)
            if found is not None and found.get("source") == "pi-extension":
                return found
            pane = tmux_pane(session)
            lower = pane.lower()
            if not tmux_alive(session):
                raise ProbeFailure(
                    f"pi TUI exited before registration\n{pane[-2000:]}"
                )
            if "trust" in lower and not trust_hint_printed:
                trust_hint_printed = True
                emit_manual(
                    f"pi 项目信任提示出现(E8 场景):请 `tmux attach -t {session}` "
                    "选择 Trust,然后 detach(C-b d);腿会继续等待注册"
                )
            if "no api key" in lower or "run '/login" in lower or "not logged in" in lower:
                raise Blocked(f"pi model auth unavailable\n{pane[-1500:]}")
            return None

        registration = wait_until(
            registered,
            lambda item: item is not None,
            timeout=args.setup_timeout,
            label=f"pi carrier registration actor={actor}",
        )
        emit_stage(
            stage,
            "OK",
            f"actor={actor} sessionRef={registration.get('sessionRef')}",
        )

        stage = "inject"
        emit_stage(stage, "START")
        emit_manual(
            f"请 `tmux attach -t {session}`,在 pi 输入框里**持续慢慢打字(不要回车)**。"
            f"约 20 秒后会注入一条 hyprial 消息;请确认:① 你的打字不被打断、输入不丢;"
            f"② 消息不会插进当前 turn。确认后停止打字等待。"
        )
        time.sleep(20.0)
        text = (
            f"hyprial 连通性检查:请把标记 {marker} 原样放进回复里,不要加其他内容。"
        )
        sent = sender.command(
            "message", "send", "--from", sender_name, "--to", actor,
            "--summary", text, text, "--json"
        )
        if sent.get("ok") is not True:
            raise ProbeFailure(f"marker send was not accepted: {sent}")
        emit_manual(
            "hyprial 消息已发出。请停止打字,观察 [Harness Network message ...] "
            "是否作为 followUp 出现并开始作答。"
        )

        stage = "reply"
        emit_stage(stage, "START")
        wait_until(
            lambda: pending(sender, sender_name),
            lambda items: any(
                marker in str(item.get("summary", "")) for item in items
            ),
            timeout=args.manual_timeout,
            label=f"explicit reply containing {marker} on the sender daemon",
        )
        emit_stage(stage, "OK", "reply 回链确认(含标记);载体不自动回复")

        stage = "unregister"
        emit_stage(stage, "START")
        emit_manual(
            f"请在 TUI 里按 Ctrl+D 退出(`tmux attach -t {session}`);"
            "60 秒未退出将由腿代为关闭。"
        )
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and tmux_alive(session):
            time.sleep(0.5)
        stop_tui(session)
        session = None
        wait_until(
            lambda: interactive_session(worker, actor),
            lambda item: item is None or item.get("online") is False,
            timeout=20.0,
            label=f"pi session released after TUI exit actor={actor}",
        )
        emit_stage(stage, "OK", "ps 中会话已消失")
        result = "PASS"
    except Blocked as error:
        emit_stage(stage, "BLOCKED", str(error).replace("\n", " | ")[:400])
        result = "BLOCKED"
    except ProbeFailure as error:
        emit_stage(stage, "FAIL", str(error).replace("\n", " | ")[:400])
        result = "FAIL"
    except KeyboardInterrupt:
        emit_stage(stage, "BLOCKED", "interrupted-by-user")
        result = "BLOCKED"
    except Exception as error:  # noqa: BLE001 — a gate must never die silent
        emit_stage(stage, "FAIL", f"internal={type(error).__name__}: {error}")
        result = "FAIL"
    finally:
        emit_stage("cleanup", "START")
        if session is not None:
            stop_tui(session)
        for daemon in reversed(daemons):
            try:
                stop_daemon(daemon)
            except AssertionError as error:
                print(f"WARNING: daemon stop failed: {error}", flush=True)
        if result != "PASS" and root is not None:
            preserve_failure_state(root, "pi-attach-manual")
        emit_stage("cleanup", "OK")

    print(f"RESULT {result}", flush=True)
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[result]


if __name__ == "__main__":
    raise SystemExit(main())
