#!/usr/bin/env python3
"""Codex interactive attach gate: authenticated TUI plus a second client.

This is a diagnostic probe, not a hyprial implementation. It uses an isolated
CODEX_HOME and the same secret-safe DeepSeek exec wrapper as the other harness
gates; no ChatGPT login, copied token, or API key in argv is involved.

The experiment answers one narrow question: can an external app-server client
find the TUI's thread, inject a turn, and observe the same conversation from
the TUI and app-server views?
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _deepseek import MODEL as DEEPSEEK_MODEL

from _codex_attach.protocol import (
    ProbeFailure,
    UnixWebSocket,
    remaining,
    response_result,
    thread_candidates,
    thread_read,
    thread_text,
)
from _codex_attach.diagnostics import (
    Blocked,
    ManualWindowStop,
    diagnostic_tail,
    emit_stage,
    environment_block_hint,
    print_diagnostic_tail,
    quota_blocking_hint,
    quota_hint,
)


def run(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )

def pane(session: str) -> str:
    result = run("tmux", "capture-pane", "-p", "-S", "-300", "-t", session, timeout=5)
    return result.stdout if result.returncode == 0 else result.stderr

def tmux_alive(session: str) -> bool:
    result = run("tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_dead}", timeout=5)
    return any(
        name == session and dead != "1"
        for name, _, dead in (line.partition("\t") for line in result.stdout.splitlines())
    )

def wait_for(
    label: str,
    predicate: Any,
    timeout: float = 30.0,
    global_deadline: float | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    if global_deadline is not None:
        deadline = min(deadline, global_deadline)
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    if global_deadline is not None and time.monotonic() >= global_deadline:
        raise ProbeFailure(f"GLOBAL_TIMEOUT stage={label}")
    raise ProbeFailure(f"timeout waiting for {label}")

def send_turn(
    client: UnixWebSocket,
    thread_id: str,
    prompt: str,
    request_id: int,
    global_deadline: float | None = None,
    quota_probe: Any | None = None,
    completion_marker: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    client.send_json(
        {
            "method": "turn/start",
            "id": request_id,
            "params": {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        }
    )
    events: list[dict[str, Any]] = []
    response: dict[str, Any] | None = None
    turn_id: str | None = None
    turn_started = time.monotonic()
    deadline = turn_started + 180
    if global_deadline is not None:
        deadline = min(deadline, global_deadline)
    last_wait_report = time.monotonic()
    last_thread_poll = turn_started - 5
    next_thread_read_id = 9000 + request_id
    last_read_summary = ""

    def report_wait() -> None:
        nonlocal last_wait_report
        now = time.monotonic()
        if now - last_wait_report < 10:
            return
        hint = quota_probe() if quota_probe is not None else None
        if hint:
            emit_stage("external-turn", "WARN", f"quota={hint}")
            blocked = quota_blocking_hint(hint)
            if blocked:
                raise ProbeFailure(f"QUOTA_BLOCKED {blocked}")
        print(
            f"STAGE=external-turn WAIT elapsed={int(now - turn_started)}s",
            flush=True,
        )
        last_wait_report = now

    def poll_thread_read(force: bool = False) -> bool:
        nonlocal last_thread_poll, next_thread_read_id, last_read_summary
        now = time.monotonic()
        if not force and now - last_thread_poll < 5:
            return False
        if now >= deadline:
            return False
        last_thread_poll = now
        read_id = next_thread_read_id
        next_thread_read_id += 1
        try:
            snapshot = thread_read(
                client,
                thread_id,
                read_id,
                timeout=min(5.0, max(0.1, deadline - time.monotonic())),
            )
        except Exception as error:
            last_read_summary = f"threadReadError={error}"
            return False
        thread = snapshot.get("thread")
        turns = thread.get("turns", []) if isinstance(thread, dict) else []
        statuses = [
            turn.get("status")
            for turn in turns
            if isinstance(turn, dict)
        ]
        text = thread_text(snapshot)
        last_read_summary = f"threadReadStatuses={statuses}; threadText={text[-300:]}"
        if (
            response is not None
            and completion_marker is not None
            and text.count(completion_marker) >= 2
        ):
            events.append(
                {
                    "method": "turn/completed",
                    "_probeCompletion": "thread/read-marker",
                }
            )
            return True
        if response is not None and "completed" in statuses:
            events.append(
                {
                    "method": "turn/completed",
                    "_probeCompletion": "thread/read-poll",
                }
            )
            return True
        return False

    try:
        while time.monotonic() < deadline:
            if response is not None and poll_thread_read():
                return response, events
            try:
                message = client.recv_json(
                    timeout=min(5.0, max(0.1, deadline - time.monotonic()))
                )
            except TimeoutError:
                if time.monotonic() < deadline:
                    report_wait()
                    if response is not None and poll_thread_read(force=True):
                        return response, events
                    continue
                if response is not None and poll_thread_read(force=True):
                    return response, events
                raise
            events.append(message)
            report_wait()
            if "method" in message and "id" in message:
                method = str(message["method"])
                if method in {
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                }:
                    client.send_json({"id": message["id"], "result": {"decision": "decline"}})
                elif method == "item/permissions/requestApproval":
                    client.send_json(
                        {
                            "id": message["id"],
                            "result": {"permissions": {}, "scope": "turn"},
                        }
                    )
                elif method in {"execCommandApproval", "applyPatchApproval"}:
                    client.send_json(
                        {
                            "id": message["id"],
                            "result": {
                                "decision": {
                                    "denied": {
                                        "rejection": "gate client does not grant approvals"
                                    }
                                }
                            },
                        }
                    )
                else:
                    client.send_json(
                        {
                            "id": message["id"],
                            "error": {
                                "code": -32601,
                                "message": f"gate client does not support {method!r}",
                            },
                        }
                    )
                continue
            if message.get("id") == request_id:
                response = message
                result = message.get("result")
                turn = result.get("turn") if isinstance(result, dict) else None
                turn_id = turn.get("id") if isinstance(turn, dict) else None
                if poll_thread_read(force=True):
                    return response, events
            params = message.get("params")
            if (
                isinstance(params, dict)
                and params.get("turnId") == turn_id
                and message.get("method") in {"turn/completed", "error"}
            ):
                break
            if response is not None and turn_id is not None and message.get("method") == "turn/completed":
                if params is None or params.get("turnId") in {None, turn_id}:
                    break
    except TimeoutError as error:
        errors = [
            event.get("params", {}).get("error", {}).get("message")
            for event in events
            if event.get("method") == "error" and isinstance(event.get("params"), dict)
        ]
        read_summary = ""
        try:
            snapshot = thread_read(
                client,
                thread_id,
                90,
                timeout=remaining(global_deadline, "external-turn", cap=10.0),
            )
            thread = snapshot.get("thread")
            turns = thread.get("turns", []) if isinstance(thread, dict) else []
            statuses = [
                turn.get("status")
                for turn in turns
                if isinstance(turn, dict)
            ]
            snapshot_text = thread_text(snapshot)
            if (
                response is not None
                and completion_marker is not None
                and snapshot_text.count(completion_marker) >= 2
            ):
                events.append(
                    {
                        "method": "turn/completed",
                        "_probeCompletion": "thread/read-marker",
                    }
                )
                return response, events
            if response is not None and "completed" in statuses:
                events.append(
                    {
                        "method": "turn/completed",
                        "_probeCompletion": "thread/read-poll",
                    }
                )
                return response, events
            read_summary = f"; threadReadStatuses={statuses}; threadText={snapshot_text[-300:]}"
        except Exception as read_error:
            read_summary = f"; {last_read_summary}; threadReadError={read_error}"
        raise ProbeFailure(
            "turn/start receive timeout; events="
            + ",".join(str(event.get("method")) for event in events[-20:])
            + f"; response={json.dumps(response, separators=(",", ":"))[:500]}"
            + (f"; errors={errors[-3:]}" if errors else "")
            + (f"; {last_read_summary}" if last_read_summary else "")
            + read_summary
        ) from error
    if response is None:
        raise ProbeFailure(f"turn/start response {request_id} not observed")
    if not any(event.get("method") == "turn/completed" for event in events):
        errors = [
            event.get("params", {}).get("error", {}).get("message")
            for event in events
            if event.get("method") == "error" and isinstance(event.get("params"), dict)
        ]
        raise ProbeFailure(
            "turn/start did not complete; events="
            + ",".join(str(event.get("method")) for event in events[-20:])
            + (f"; errors={errors[-3:]}" if errors else "")
        )
    return response, events

def wait_for_pane(
    session: str,
    *needles: str,
    timeout: float = 180.0,
    global_deadline: float | None = None,
) -> str:
    def matched() -> bool:
        current = pane(session)
        return all(needle in current for needle in needles)

    wait_for(
        "TUI output: " + ", ".join(needles),
        matched,
        timeout,
        global_deadline=global_deadline,
    )
    return pane(session)

def stop_tui(session: str) -> None:
    if tmux_alive(session):
        for _ in range(3):
            run("tmux", "send-keys", "-t", session, "C-c")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and tmux_alive(session):
                time.sleep(0.1)
            if not tmux_alive(session):
                break
    if run("tmux", "has-session", "-t", session).returncode == 0:
        run("tmux", "kill-session", "-t", session)

def stop_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

def main() -> int:
    emit_stage("boot", "START")
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-root", action="store_true", help="keep disposable artifacts")
    parser.add_argument("--model", default=DEEPSEEK_MODEL)
    parser.add_argument(
        "--setup-timeout",
        type=float,
        default=300.0,
        help="global setup/external-turn deadline before the manual window",
    )
    parser.add_argument(
        "--inject-only",
        action="store_true",
        help="inject the external turn, then wait for a human TUI follow-up",
    )
    parser.add_argument(
        "--manual-timeout",
        type=float,
        default=300.0,
        help="seconds to wait for the human follow-up in --inject-only mode",
    )
    parser.add_argument("--discovery-attempts", type=int, default=10)
    parser.add_argument("--discovery-backoff", type=float, default=2.0)
    parser.add_argument(
        "--resume-thread",
        action="store_true",
        help="rejoin the discovered running thread before the external turn",
    )
    args = parser.parse_args()
    if (
        args.setup_timeout <= 0
        or args.manual_timeout <= 0
        or args.discovery_attempts <= 0
        or args.discovery_backoff < 0
    ):
        print("RESULT BLOCKED", flush=True)
        print("BLOCKED timeouts must be positive", flush=True)
        return 2
    setup_deadline = time.monotonic() + args.setup_timeout
    emit_stage("boot", "OK", f"setup_timeout={args.setup_timeout:g}s")
    nonce = secrets.token_hex(5)
    session = f"codex-attach-gate-{os.getpid()}-{nonce}"
    root = Path(tempfile.mkdtemp(prefix="codex-attach-gate-"))
    process: subprocess.Popen[bytes] | None = None
    client: UnixWebSocket | None = None
    result = "PASS"
    findings: list[str] = []
    codex_wrapper = Path(__file__).with_name("_codex_deepseek_exec.py")
    log_path: Path | None = None
    log_handle: Any | None = None
    current_stage = "boot"

    def start_stage(name: str) -> None:
        nonlocal current_stage
        current_stage = name
        emit_stage(name, "START")

    def finish_stage(name: str, detail: str | None = None) -> None:
        emit_stage(name, "OK", detail)

    try:
        start_stage("preflight")
        if shutil.which("codex") is None:
            raise Blocked("codex executable is not installed")
        if shutil.which("tmux") is None:
            raise Blocked("tmux is required for the authenticated TUI")
        finish_stage("preflight", "codex+tmux+deepseek-credential")
        codex_home = (root / "codex-home").resolve()
        workspace = (root / "workspace").resolve()
        socket_path = (root / "app.sock").resolve()
        start_stage("temp-state")
        codex_home.mkdir(mode=0o700)
        workspace.mkdir(mode=0o700)
        finish_stage("temp-state", f"root={root} isolated-codex-home=true")
        start_stage("codex-version")
        version_result = subprocess.run(
            [sys.executable, str(codex_wrapper), "--version"],
            text=True,
            capture_output=True,
            timeout=remaining(setup_deadline, "codex-version", cap=15.0),
            check=False,
            env={**os.environ, "CODEX_HOME": str(codex_home)},
        )
        if version_result.returncode != 0:
            raise Blocked("codex --version failed")
        version_lines = [
            line.strip()
            for line in (version_result.stdout + "\n" + version_result.stderr).splitlines()
            if line.strip()
        ]
        version = next((line for line in version_lines if "codex" in line.lower()), "unknown")
        finish_stage("codex-version", f"version={version[:120]}")
        start_stage("app-server")
        log_path = root / "app-server.log"
        log_handle = log_path.open("wb")
        process = subprocess.Popen(
            [
                sys.executable,
                str(codex_wrapper),
                "app-server",
                "--listen",
                f"unix://{socket_path}",
            ],
            cwd=workspace,
            env={**os.environ, "CODEX_HOME": str(codex_home)},
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        emit_stage("app-server", "PID", f"pid={process.pid}")
        wait_for(
            "app-server socket",
            lambda: socket_path.exists(),
            timeout=20,
            global_deadline=setup_deadline,
        )
        finish_stage("app-server", f"socket={socket_path}")
        start_stage("tui")
        started = run(
            "tmux",
            "new-session",
            "-d",
            "-s",
            session,
            "-c",
            str(workspace),
            "--",
            "env",
            f"CODEX_HOME={codex_home}",
            "TERM=xterm-256color",
            sys.executable,
            str(codex_wrapper),
            "--no-alt-screen",
            "--model",
            args.model,
            "--remote",
            f"unix://{socket_path}",
            timeout=remaining(setup_deadline, "tui", cap=30.0),
        )
        if started.returncode != 0:
            raise ProbeFailure(f"tmux TUI failed to start: {started.stderr[-1000:]}")
        wait_for_pane(
            session,
            "Codex",
            args.model,
            timeout=60,
            global_deadline=setup_deadline,
        )
        finish_stage("tui", f"tmux={session}")
        findings.append("PASS normal TUI reached authenticated model-ready state")

        start_stage("rpc-initialize")
        client = UnixWebSocket(
            socket_path,
            timeout=remaining(setup_deadline, "rpc-initialize"),
        )
        response_result(
            client.request(
                "initialize",
                {
                    "clientInfo": {"name": "codex-interactive-attach-gate", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
                1,
                timeout=remaining(setup_deadline, "rpc-initialize"),
            ),
            "initialize",
        )
        client.send_json({"method": "initialized", "params": {}})
        finish_stage("rpc-initialize")
        start_stage("thread-discovery")
        bootstrap_marker: str | None = None
        discovery_deadline = setup_deadline
        if args.inject_only:
            bootstrap_marker = f"MANUAL_TUI_BOOTSTRAP_{nonce}"
            bootstrap_deadline = min(
                setup_deadline, time.monotonic() + args.manual_timeout
            )
            discovery_deadline = bootstrap_deadline
            start_stage("manual-bootstrap")
            print("MANUAL_BOOTSTRAP_READY=1", flush=True)
            print(f"MANUAL_TMUX_ATTACH=tmux attach -t {session}", flush=True)
            print(f"MANUAL_BOOTSTRAP_MARKER={bootstrap_marker}", flush=True)
            print(
                "MANUAL_BOOTSTRAP_ACTION=Attach to the printed tmux session and "
                f"type exactly 'Reply with exactly {bootstrap_marker} and do not use tools.' "
                "then press the physical Enter key.",
                flush=True,
            )
            print(
                "MANUAL_BOOTSTRAP_FACT=The probe will retry thread discovery "
                "until this marker is present in thread/read.",
                flush=True,
            )
            finish_stage(
                "manual-bootstrap",
                f"manual_timeout={args.manual_timeout:g}s "
                f"backoff={args.discovery_backoff:g}s attempts=unbounded",
            )
            start_stage("thread-discovery")
        candidates: list[dict[str, Any]] = []
        discovery_mode = ""
        last_discovery_error = ""
        attempt = 0
        last_manual_action = time.monotonic()
        while True:
            attempt += 1
            try:
                candidates, discovery_mode = thread_candidates(
                    client, str(workspace), global_deadline=discovery_deadline
                )
                if args.inject_only and bootstrap_marker is not None:
                    candidate_id = str(candidates[0]["id"]) if len(candidates) == 1 else ""
                    seed = thread_read(
                        client,
                        candidate_id,
                        40 + attempt,
                        timeout=remaining(discovery_deadline, "thread-discovery", cap=10.0),
                    )
                    if bootstrap_marker not in thread_text(seed):
                        raise ProbeFailure(
                            "thread found before manual bootstrap marker was persisted"
                        )
                break
            except ProbeFailure as error:
                last_discovery_error = str(error)
                retryable = (
                    "thread discovery returned no candidate" in last_discovery_error
                    or "manual bootstrap marker" in last_discovery_error
                    or "list_turns is not supported yet" in last_discovery_error
                )
                if not args.inject_only and (
                    not retryable or attempt == args.discovery_attempts
                ):
                    raise ProbeFailure(
                        f"manual discovery exhausted attempts={attempt}: {last_discovery_error}"
                        if args.inject_only
                        else last_discovery_error
                    )
                if args.inject_only and not retryable:
                    raise
                if args.inject_only and time.monotonic() >= discovery_deadline:
                    raise ProbeFailure(
                        "GLOBAL_TIMEOUT stage=manual-bootstrap; "
                        f"manual_timeout={args.manual_timeout:g}s; "
                        f"last={last_discovery_error}"
                    )
                if args.inject_only and time.monotonic() - last_manual_action >= 15:
                    print(
                        "MANUAL_ACTION=Bootstrap still pending: attach to the "
                        f"printed tmux session and submit marker {bootstrap_marker}",
                        flush=True,
                    )
                    last_manual_action = time.monotonic()
                emit_stage(
                    "thread-discovery",
                    "WAIT",
                    (
                        f"attempt={attempt} backoff={args.discovery_backoff:g}s "
                        "action=请先在TUI发一条消息"
                        if args.inject_only
                        else f"attempt={attempt}/{args.discovery_attempts} "
                        f"backoff={args.discovery_backoff:g}s action=请先在TUI发一条消息"
                    ),
                )
                time.sleep(
                    min(
                        args.discovery_backoff,
                        remaining(discovery_deadline, "thread-discovery", cap=args.discovery_backoff or 0.1),
                    )
                )
        if not candidates:
            raise ProbeFailure(
                f"thread discovery returned no candidate: {last_discovery_error}"
            )
        if len(candidates) != 1:
            raise ProbeFailure(f"expected one loaded direct-input thread, found {len(candidates)}")
        thread_id = str(candidates[0]["id"])
        finish_stage("thread-discovery", f"mode={discovery_mode} thread={thread_id}")
        if args.inject_only:
            emit_stage("manual-bootstrap", "OK", f"thread={thread_id}")
        findings.append(f"PASS second client found exactly one TUI thread via {discovery_mode}")
        if args.resume_thread:
            resumed = response_result(
                client.request(
                    "thread/resume",
                    {"threadId": thread_id},
                    12,
                    timeout=remaining(setup_deadline, "thread-resume"),
                ),
                "thread/resume",
            )
            resumed_thread = resumed.get("thread")
            if not isinstance(resumed_thread, dict) or resumed_thread.get("id") != thread_id:
                raise ProbeFailure(f"thread/resume returned unexpected thread: {resumed}")
            findings.append("PASS second client thread/resume rejoined the running thread")

        normal_marker = f"ATTACH_GATE_EXTERNAL_{nonce}"
        start_stage("external-turn")
        normal_response, normal_events = send_turn(
            client,
            thread_id,
            f"Reply with exactly {normal_marker} and do not use tools.",
            4,
            global_deadline=setup_deadline,
            quota_probe=lambda: quota_hint(pane(session)),
            completion_marker=normal_marker,
        )
        if any(event.get("_probeCompletion") == "thread/read-poll" for event in normal_events):
            findings.append("OBSERVED second client needed thread/read polling; turn events were not broadcast")
        normal_text = thread_text(
            thread_read(
                client,
                thread_id,
                5,
                timeout=remaining(setup_deadline, "external-turn", cap=10.0),
            )
        )
        normal_pane = wait_for_pane(
            session,
            normal_marker,
            timeout=30,
            global_deadline=setup_deadline,
        )
        if normal_text.count(normal_marker) < 2 or normal_pane.count(normal_marker) < 2:
            raise ProbeFailure("external turn was not visible in both thread/read and TUI pane")
        findings.append("PASS external turn and response visible in thread/read and TUI pane")
        finish_stage("external-turn")
        if "turn/completed" not in {event.get("method") for event in normal_events}:
            raise ProbeFailure(f"normal external turn had no turn/completed: {normal_response}")

        if args.inject_only:
            followup_marker = f"MANUAL_TUI_FOLLOWUP_{nonce}"
            start_stage("manual-window")
            print("MANUAL_READY=1", flush=True)
            print(f"MANUAL_TMUX_ATTACH=tmux attach -t {session}", flush=True)
            print(f"MANUAL_ENDPOINT=unix://{socket_path}", flush=True)
            print(f"MANUAL_THREAD_ID={thread_id}", flush=True)
            print(f"MANUAL_EXTERNAL_MARKER={normal_marker}", flush=True)
            print(f"MANUAL_FOLLOWUP_MARKER={followup_marker}", flush=True)
            print(
                "MANUAL_ACTION=In the attached TUI, type "
                f"'Reply with exactly {followup_marker} and do not use tools.' "
                "and press the physical Enter key.",
                flush=True,
            )
            print(
                "MANUAL_FACT=This script reports only whether the marker entered "
                "thread/read; Allen records the screen observation separately.",
                flush=True,
            )
            finish_stage("manual-window", f"timeout={args.manual_timeout:g}s")
            deadline = time.monotonic() + args.manual_timeout
            request_id = 60
            last_manual_report = time.monotonic()
            while time.monotonic() < deadline:
                if not tmux_alive(session):
                    result = "FAIL"
                    findings.append("FAIL TUI tmux session ended during manual window")
                    break
                snapshot = thread_read(
                    client,
                    thread_id,
                    request_id,
                    timeout=min(10.0, max(0.1, deadline - time.monotonic())),
                )
                request_id += 1
                if followup_marker in thread_text(snapshot):
                    result = "PASS"
                    findings.append("THREAD_FOLLOWUP_PASS marker observed in thread/read")
                    break
                if time.monotonic() - last_manual_report >= 10:
                    emit_stage("manual-window", "WAIT")
                    last_manual_report = time.monotonic()
                time.sleep(1.0)
            else:
                result = "BLOCKED"
                findings.append("BLOCKED manual timeout: no follow-up marker in thread/read")
            raise ManualWindowStop

        tui_marker = f"ATTACH_GATE_TUI_{nonce}"
        try:
            run(
                "tmux",
                "send-keys",
                "-t",
                session,
                "-l",
                f"Reply with exactly {tui_marker} and do not use tools.",
            )
            # Codex's line editor is PTY-driven; CR is less ambiguous than a
            # tmux symbolic Enter key when running under a non-interactive
            # controller.
            run("tmux", "send-keys", "-t", session, "C-m")
            tui_pane = wait_for_pane(
                session,
                tui_marker,
                timeout=30,
                global_deadline=setup_deadline,
            )
            tui_text = thread_text(
                thread_read(
                    client,
                    thread_id,
                    6,
                    timeout=remaining(setup_deadline, "tui-followup", cap=10.0),
                )
            )
            if tui_text.count(tui_marker) < 2 or tui_pane.count(tui_marker) < 2:
                raise ProbeFailure("subsequent TUI input was not visible in both views")
            findings.append("PASS TUI accepted subsequent input after external turn")
        except ProbeFailure as error:
            findings.append(
                "OBSERVED synthetic PTY follow-up was not accepted by both views; "
                f"non-gating because physical TUI input requires attended proof: {error}"
            )
            try:
                (root / "tui-pane-followup-failure.txt").write_text(
                    pane(session), encoding="utf-8"
                )
            except OSError:
                pass

        start_stage("busy-turn")
        remaining(setup_deadline, "busy-turn", cap=0.1)
        busy_marker = f"ATTACH_GATE_BUSY_{nonce}"
        first = f"Respond with exactly {busy_marker} after producing a detailed 500-word explanation of why this is a busy-turn probe. Do not use tools."
        client.send_json(
            {
                "method": "turn/start",
                "id": 7,
                "params": {"threadId": thread_id, "input": [{"type": "text", "text": first}]},
            }
        )
        first_response: dict[str, Any] | None = None
        busy_observation: dict[str, Any] | None = None
        deadline = min(time.monotonic() + 5, setup_deadline)
        while time.monotonic() < deadline:
            message = client.recv_json(
                timeout=max(0.1, min(5.0, deadline - time.monotonic()))
            )
            if message.get("id") == 7:
                first_response = message
                break
        client.send_json(
            {
                "method": "turn/start",
                "id": 8,
                "params": {"threadId": thread_id, "input": [{"type": "text", "text": "BUSY_SECOND_INPUT"}]},
            }
        )
        deadline = min(time.monotonic() + 10, setup_deadline)
        while time.monotonic() < deadline:
            message = client.recv_json(
                timeout=max(0.1, min(5.0, deadline - time.monotonic()))
            )
            if message.get("id") == 8 or message.get("method") == "error":
                busy_observation = message
                if message.get("id") == 8 or message.get("method") == "error":
                    break
        if first_response is None or busy_observation is None:
            raise ProbeFailure("busy probe did not produce both turn observations")
        findings.append("OBSERVED busy response: " + json.dumps(busy_observation, separators=(",", ":"))[:500])
        client.send_json(
            {
                "method": "turn/interrupt",
                "id": 9,
                "params": {"threadId": thread_id, "turnId": first_response.get("result", {}).get("turn", {}).get("id", "")},
            }
        )
        finish_stage("busy-turn")
    except Blocked as error:
        result = "BLOCKED"
        emit_stage(current_stage, "BLOCKED", str(error))
        findings.append(f"BLOCKED {error}")
    except ManualWindowStop:
        pass
    except TimeoutError as error:
        current = pane(session)
        hint = quota_hint(current)
        log_tail = "\n".join(print_diagnostic_tail(log_path))
        if hint is None:
            hint = quota_hint(log_tail)
        blocking = quota_blocking_hint(current)
        if blocking is None:
            blocking = quota_blocking_hint(log_tail)
        if blocking is not None:
            result = "BLOCKED"
            emit_stage(current_stage, "BLOCKED", f"quota={blocking}")
            findings.append(f"BLOCKED quota/rate-limit: {blocking}")
        else:
            result = "FAIL"
            result = "BLOCKED" if hint is not None else "FAIL"
            detail = f"timeout={error} quota-warning={hint}" if hint else f"timeout={error}"
            emit_stage(current_stage, "BLOCKED" if hint else "FAIL", detail)
            findings.append(
                f"BLOCKED timeout with quota warning: {hint}"
                if hint
                else f"FAIL {error}"
            )
    except ProbeFailure as error:
        try:
            (root / "tui-pane.txt").write_text(pane(session), encoding="utf-8")
        except OSError:
            pass
        current = pane(session)
        hint = quota_hint(current)
        log_tail = "\n".join(diagnostic_tail(log_path))
        if hint is None:
            hint = quota_hint(log_tail)
        blocking = quota_blocking_hint(current)
        if blocking is None:
            blocking = quota_blocking_hint(log_tail)
        environment_hint = environment_block_hint(log_tail)
        message = str(error)
        if blocking is not None or "QUOTA_BLOCKED" in message:
            result = "BLOCKED"
            detail = blocking or message
            emit_stage(current_stage, "BLOCKED", f"quota={detail}")
            findings.append(f"BLOCKED quota/rate-limit: {detail}")
        elif "GLOBAL_TIMEOUT" in message:
            result = "BLOCKED"
            detail = f"{message} quota-warning={hint}" if hint else message
            emit_stage(current_stage, "BLOCKED", detail)
            findings.append(f"BLOCKED {detail}")
        elif "manual discovery exhausted" in message:
            result = "BLOCKED"
            emit_stage(current_stage, "BLOCKED", message)
            findings.append(f"BLOCKED {message}")
        elif environment_hint is not None:
            result = "BLOCKED"
            emit_stage(current_stage, "BLOCKED", f"environment={environment_hint}")
            findings.append(f"BLOCKED external Codex environment: {environment_hint}")
        else:
            result = "FAIL"
            emit_stage(current_stage, "FAIL", message)
            findings.append(f"FAIL {message}")
        print_diagnostic_tail(log_path)
    except (OSError, subprocess.SubprocessError) as error:
        result = "FAIL"
        emit_stage(current_stage, "FAIL", str(error))
        findings.append(f"FAIL {error}")
    except Exception as error:
        result = "FAIL"
        emit_stage(current_stage, "FAIL", f"internal={type(error).__name__}")
        findings.append(f"FAIL probe-internal-error: {type(error).__name__}: {error}")
    except KeyboardInterrupt:
        result = "BLOCKED"
        emit_stage(current_stage, "BLOCKED", "interrupted-by-user")
        findings.append("BLOCKED interrupted by user; cleanup completed")
    finally:
        emit_stage("cleanup", "START")
        if client is not None:
            client.close()
        stop_tui(session)
        stop_process(process)
        if log_handle is not None:
            log_handle.close()
        if args.keep_root:
            findings.append(f"ARTIFACT_ROOT {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)
        emit_stage("cleanup", "OK")
    for finding in findings:
        print(finding)
    print(f"RESULT {result}")
    return {"PASS": 0, "FAIL": 1, "BLOCKED": 2}[result]

if __name__ == "__main__":
    raise SystemExit(main())
