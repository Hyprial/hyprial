#!/usr/bin/env python3
"""E2E-008: real CC TUI worker over isolated Python daemons and Channels."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import (
    IsolatedDaemon,
    free_tcp_endpoint,
    hyprial_executable,
    scenario_tempdir,
    short_temp_root,
)
from _deepseek import CC_WORKER_MODEL, claude_code_environment


class Blocked(RuntimeError):
    """A missing real-environment prerequisite, not a product assertion failure."""


# CLI probe-sender persona, registered on the sender daemon (Allen b).
DRIVER = "e2e008-driver"


def run(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def assert_isolated_network(
    daemon: IsolatedDaemon,
    init_result: dict[str, Any],
    *,
    expected_connect: str | None,
) -> None:
    if expected_connect is not None and not expected_connect.startswith(
        "tcp/127.0.0.1:"
    ):
        raise AssertionError(
            f"isolated daemon connect endpoint is not loopback: {expected_connect}"
        )
    event = next(
        (
            value
            for line in (daemon.root / "state" / "logs" / "daemon.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if (value := json.loads(line)).get("event")
            == "zenoh.endpoints.resolved"
        ),
        None,
    )
    configured = 1 if expected_connect is not None else 0
    if event is None or (
        event.get("discovery"),
        event.get("configured"),
        event.get("discovered"),
        event.get("effective"),
    ) != ("disabled", configured, 0, configured):
        raise AssertionError(f"isolated daemon resolved unsafe endpoints: {event}")
    status = daemon.ps()
    expected = [expected_connect] if expected_connect is not None else []
    observed = status.get("zenoh", {}).get("connect")
    if observed != expected:
        raise AssertionError(
            f"isolated daemon connect endpoints {observed} != {expected}"
        )
    socket_path = daemon.root / "state" / "daemon.sock"
    print(
        "PASS isolated-daemon "
        f"node={daemon.node_id} socketBytes={len(os.fsencode(socket_path))} "
        f"initRc=0 running={init_result.get('running')} "
        f"event={json.dumps(event, sort_keys=True, separators=(',', ':'))} "
        f"psConnect={json.dumps(observed, separators=(',', ':'))}"
    )


def tmux_pane(session: str) -> str:
    captured = run("tmux", "capture-pane", "-p", "-S", "-300", "-t", session)
    return captured.stdout if captured.returncode == 0 else captured.stderr


def tmux_alive(session: str) -> bool:
    result = run("tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_dead}")
    return any(
        name == session and dead != "1"
        for name, _, dead in (
            line.partition("\t") for line in result.stdout.splitlines()
        )
    )


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def session_exists(session: str) -> bool:
    return run("tmux", "has-session", "-t", session).returncode == 0


def stop_tui(session: str) -> None:
    if tmux_alive(session):
        # Let the foreground CC process and its stdio MCP child close before
        # removing the PTY. A raw tmux teardown can orphan CC while the parent
        # hyprial process is still waiting in subprocess.run().
        for _attempt in range(3):
            run("tmux", "send-keys", "-t", session, "C-c")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if not tmux_alive(session):
                    break
                time.sleep(0.1)
            if not tmux_alive(session):
                break
    # remain-on-exit keeps the session alive after the pane dies, and
    # tmux_alive() is then False — gate the teardown on session existence,
    # not pane liveness, or every quick exit leaks a dead session on the
    # runner host.
    if session_exists(session):
        run("tmux", "kill-session", "-t", session)
    wait_until(
        lambda: tmux_alive(session),
        lambda alive: alive is False,
        timeout=3.0,
        label=f"CC tmux pane exit session={session}",
    )


def stop_daemon(daemon: IsolatedDaemon) -> None:
    """Stop a test daemon, accepting only a proved already-dead stale socket."""

    metadata_path = daemon.root / "state" / "daemon.json"
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        metadata = {}
    pid = metadata.get("pid")
    try:
        daemon.stop()
    except AssertionError:
        pass
    if isinstance(pid, int) and pid > 0:
        try:
            wait_until(
                lambda: process_alive(pid),
                lambda alive: alive is False,
                timeout=10.0,
                label=f"isolated daemon exit pid={pid}",
            )
        except AssertionError:
            os.kill(pid, 15)
            wait_until(
                lambda: process_alive(pid),
                lambda alive: alive is False,
                timeout=10.0,
                label=f"isolated daemon SIGTERM exit pid={pid}",
            )
    (daemon.root / "state" / "daemon.sock").unlink(missing_ok=True)


def daemon_ipc(
    daemon: IsolatedDaemon, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    frame = {
        "version": 1,
        "id": str(uuid4()),
        "method": method,
        "params": params,
    }
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(10.0)
    try:
        client.connect(str(daemon.root / "state" / "daemon.sock"))
        client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        data = bytearray()
        while b"\n" not in data:
            chunk = client.recv(64 * 1024)
            if not chunk:
                raise AssertionError(f"daemon closed during {method}")
            data.extend(chunk)
    finally:
        client.close()
    response = json.loads(data.partition(b"\n")[0])
    if not isinstance(response, dict):
        raise AssertionError(f"invalid {method} response: {response!r}")
    failure = response.get("error")
    if isinstance(failure, dict):
        raise AssertionError(f"{method} failed: {failure}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise AssertionError(f"{method} returned no object result")
    return result


def pending(daemon: IsolatedDaemon, actor: str | None = None) -> list[dict[str, Any]]:
    result = daemon_ipc(
        daemon,
        "message.pending.list",
        {"actor": actor or DRIVER},
    )
    messages = result.get("messages")
    if not isinstance(messages, list) or any(
        not isinstance(item, dict) for item in messages
    ):
        raise AssertionError(f"invalid pending response: {result}")
    return messages


def wait_until(
    probe: Callable[[], Any],
    accept: Callable[[Any], bool],
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
        time.sleep(0.2)
    raise AssertionError(f"timeout waiting for {label}; last={last!r}")


def file_match_count(path: Path, pattern: str) -> int:
    if not path.is_file():
        return 0
    return path.read_text(encoding="utf-8", errors="replace").count(pattern)


def interactive_session(
    daemon: IsolatedDaemon, actor: str
) -> dict[str, Any] | None:
    status = daemon.ps()
    sessions = status.get("interactiveSessions", [])
    if not isinstance(sessions, list):
        raise AssertionError(f"ps interactiveSessions is invalid: {status}")
    for item in sessions:
        if isinstance(item, dict) and item.get("actor") == actor:
            return item
    return None


def _approval_keys(label: str, discovered_mcp: str | None) -> tuple[str, ...]:
    """Keystrokes that accept one known CC consent/permission dialog."""

    if discovered_mcp is not None and label == f"MCP server {discovered_mcp}":
        if discovered_mcp == "harness-bridge":
            return ("Enter",)
        return ("Down", "Down", "Enter")  # decline an unrelated MCP server
    if label == "Bash command in cc-workspace":
        return ("Down", "Enter")
    if label == "workspace trust":
        # CC 2.1.2xx's "Quick safety check" dialog defaults to "No, exit"
        # (observed on 2.1.259: the pane shows "❯ No, exit / Yes, I trust
        # this folder"). A bare Enter accepts the default and CC exits
        # (status 1) before registration — move the selection to the trust
        # option first.
        return ("Down", "Enter")
    return ("Enter",)


def approve_attended_prompts(
    session: str, attended: bool, consented: set[str]
) -> str:
    """Detect and, under --attended, approve known CC consent/permission
    dialogs. Must be pumped for as long as the pane may show one — not just
    during the registration wait, since e.g. a Bash permission prompt can
    surface deep into a later turn (the busy scenario).

    A send is only recorded as consented once the dialog actually left the
    pane: keys sent while CC is still bringing up its input handler are
    dropped (observed 2026-09-03: the trust dialog stayed on "No, exit" and
    the registration window timed out), so each approval is verified and
    re-sent until the dialog is gone or the retry budget runs out — the next
    pump retries again anyway.

    Returns the captured pane text so callers can also check it (for
    registration state, sentinel files, exit reasons, ...) without an extra
    capture-pane round trip.
    """

    pane = tmux_pane(session)
    lower = pane.lower()
    discovered_mcp = None
    match = re.search(
        r"new mcp server found in this project:\s*([^\s]+)",
        pane,
        flags=re.IGNORECASE,
    )
    if match is not None:
        discovered_mcp = match.group(1).strip()

    def _mcp_dialog(lower_pane: str) -> bool:
        # Re-evaluated on every fresh pane: once the dialog is accepted the
        # discovery banner disappears and the prompt is gone.
        return "new mcp server found in this project:" in lower_pane

    prompts: dict[str, Callable[[str], bool]] = {
        "workspace trust": (
            lambda lower_pane: (
                "do you trust the files in this folder" in lower_pane
                or "trust project folder" in lower_pane
                # CC 2.1.224 reworded the dialog to "Quick safety check: Is
                # this a project you created or one you trust?"; match the
                # stable menu option text instead of the header prose.
                or "yes, i trust this folder" in lower_pane
            )
        ),
        "local Channel server": (
            lambda lower_pane: (
                "warning: loading development channels" in lower_pane
                and "i am using this for local development" in lower_pane
            )
        ),
        "external CLAUDE.md imports": (
            lambda lower_pane: "allow external claude.md file imports" in lower_pane
        ),
        # --allowedTools only matches an exact command string; CC is free to
        # compose the Bash call differently (e.g. it appended a verifying
        # `ls -la`) and fall through to an interactive prompt. Approve it
        # broadly for the isolated cc-workspace/ instead of trying to keep
        # the allowlist string in lockstep with the model.
        "Bash command in cc-workspace": (
            lambda lower_pane: (
                "do you want to proceed?" in lower_pane and "bash command" in lower_pane
            )
        ),
        **(
            {
                f"MCP server {discovered_mcp}": _mcp_dialog
            }
            if discovered_mcp is not None
            else {}
        ),
    }
    for label, is_present in prompts.items():
        if not is_present(lower) or label in consented:
            continue
        if not attended:
            stop_tui(session)
            raise Blocked(
                f"[ATTENDED] Claude requires {label} consent; rerun with --attended"
            )
        keys = _approval_keys(label, discovered_mcp)
        if discovered_mcp is not None and label.startswith("MCP server "):
            if discovered_mcp == "harness-bridge":
                print(f"[ATTENDED] approving isolated Claude {label} prompt")
            else:
                print(f"[ATTENDED] declining unrelated Claude MCP server {discovered_mcp}")
        else:
            print(f"[ATTENDED] approving isolated Claude {label} prompt")
        # Send once, then give CC time to consume it — a dialog still shown
        # after a full window means the send was dropped while CC was still
        # bringing up its input handler, and only then is it re-sent (rapid
        # resends could leak keys into the next dialog). The label only
        # enters `consented` once the pane no longer shows the dialog; an
        # unconfirmed label is retried on the next pump.
        for _attempt in range(3):
            run("tmux", "send-keys", "-t", session, *keys)
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                time.sleep(0.25)
                if not is_present(tmux_pane(session).lower()):
                    consented.add(label)
                    break
            if label in consented:
                break
        if label not in consented:
            print(f"[ATTENDED] {label} dialog still present after retries; will re-pump")
    return pane


def ci_claude_config_dir() -> Path:
    """Return the CI-dedicated Claude settings dir — validate, never write.

    The model credential comes from HYPRIAL's DeepSeek resolver, not Claude OAuth.
    This directory remains dedicated because it owns workspace trust, Channel
    consent, settings and debug/session artifacts; the developer's real
    ``~/.claude`` is never read or written.

    The runner writes nothing into the directory: onboarding state must
    already exist from the one-time interactive login that created the
    credentials. Claude Code's own session artifacts (transcripts under
    projects/, history) are the same writes any claude process performs.

    An ambient CLAUDE_CONFIG_DIR (the CI job env pins the same path) wins so
    the value under test is the value the job set; local runs fall back to
    ~/.claude-ci. Existence/flag checks only.
    """

    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(raw).expanduser() if raw else Path.home() / ".claude-ci"
    if config_dir.resolve() == (Path.home() / ".claude").resolve():
        raise Blocked(
            "CLAUDE_CONFIG_DIR points at the developer's real ~/.claude; "
            "E2E-008 requires the CI-dedicated login (~/.claude-ci)"
        )
    try:
        onboarding = json.loads(
            (config_dir / ".claude.json").read_text(encoding="utf-8")
        ).get("hasCompletedOnboarding")
    except (OSError, ValueError):
        onboarding = None
    if onboarding is not True:
        raise Blocked(
            "CI-dedicated Claude login has not completed onboarding: "
            f"{config_dir}/.claude.json (run Claude Code once interactively "
            "with CLAUDE_CONFIG_DIR set)"
        )
    return config_dir


def assert_claude_config_dir_used(
    config_dir: Path,
    debug_path: Path,
    *,
    timeout: float = 15.0,
) -> None:
    """Prove the spawned CC really used the given CLAUDE_CONFIG_DIR.

    "The env var was set" and "the process used it" are two different
    things. macOS exposes no other-process environment (SIP), so this probe
    reads CC's own --debug-file: at startup CC logs the config-dir paths it
    resolves (plugins/skills/settings/session state). A healthy isolated run
    logs the CI dir dozens of times and the developer's real ~/.claude/
    prefix zero times (empirically verified); a session transcript under
    projects/ is NOT a usable signal — CC only writes it on the first user
    message, which a smoke run never sends.
    """

    needle = str(config_dir)
    forbidden = str(Path.home() / ".claude") + "/"
    deadline = time.monotonic() + timeout
    text = ""
    while time.monotonic() < deadline:
        if debug_path.is_file():
            text = debug_path.read_text(encoding="utf-8", errors="replace")
            if needle in text:
                break
        time.sleep(0.5)
    else:
        raise AssertionError(
            f"claude debug log never referenced {config_dir}: "
            "CLAUDE_CONFIG_DIR was set but provably not used "
            f"(debug file {debug_path})"
        )
    leaked = [line for line in text.splitlines() if forbidden in line]
    if leaked:
        raise AssertionError(
            "claude resolved the developer's real ~/.claude instead of the "
            f"CI-dedicated dir; first offending debug line: {leaked[0][:300]!r}"
        )
    print(f"PASS claude provably used CLAUDE_CONFIG_DIR={config_dir}")


def build_cc_launch_command(
    *,
    session: str,
    workspace: Path,
    worker_env: dict[str, str],
    claude_config_dir: Path,
    executable: Path,
    actor: str,
    busy_sentinel: Path,
    debug_log: Path,
    cc_environment: dict[str, str],
) -> list[str]:
    """The tmux command that launches the CC worker TUI (E2E-008's subject).

    DeepSeek is selected through its official Claude Code environment
    mapping (``cc_environment`` — see _deepseek.claude_code_environment),
    deliberately NOT through hyprial's ``--provider deepseek``: hyprial's own
    provider mapping overwrites every ANTHROPIC_* variable with the raw
    "deepseek-flash" id, which this CC version's model catalog does not
    describe — the TUI fatal-exits before Channel registration (the red
    E2E-008 controlled-gate signature). The selection stays catalog-known
    (CC_WORKER_MODEL = "haiku"); ANTHROPIC_DEFAULT_HAIKU_MODEL maps it to
    deepseek-flash at request time, preserving the flash budget.

    ``cc_environment`` contains the API key. Callers must never print the
    returned command or write it anywhere — it exists only to be spawned.
    """

    return [
        "tmux",
        "new-session",
        "-d",
        "-s",
        session,
        "-c",
        str(workspace),
        "--",
        "env",
        f"HYPRIAL_HOME={worker_env['HYPRIAL_HOME']}",
        f"HARNESS_STATE_DIR={worker_env['HARNESS_STATE_DIR']}",
        f"CLAUDE_CONFIG_DIR={claude_config_dir}",
        "TERM=xterm-256color",
        *[f"{key}={value}" for key, value in cc_environment.items()],
        str(executable),
        "agent",
        "start",
        "claude",
        "--name",
        actor,
        "--cwd",
        str(workspace),
        "--model",
        CC_WORKER_MODEL,
        "--",
        "--allowedTools",
        # CC splits a compound Bash command on `;` and matches each fragment
        # independently, so a `command:*` glob-prefix per fragment survives
        # CC composing the call differently (e.g. appending a verifying
        # `ls -la`) without falling through to an interactive prompt —
        # verified against a live isolated CC instance before relying on it
        # here. approve_attended_prompts() still recognizes the interactive
        # Bash permission dialog as a backstop for any other composition.
        f"Bash(touch {busy_sentinel}:*)",
        "Bash(sleep 8:*)",
        f"Bash(ls -la {busy_sentinel}:*)",
        "--strict-mcp-config",
        "--debug-file",
        str(debug_log),
    ]


def start_tui(
    executable: Path,
    worker: IsolatedDaemon,
    actor: str,
    busy_sentinel: Path,
    claude_config_dir: Path,
    *,
    suffix: str,
    attended: bool,
    previous_session_ref: str | None = None,
) -> tuple[str, dict[str, Any]]:
    session = f"hyprial-cc-worker-{os.getpid()}-{suffix}"
    workspace = worker.root / "cc-workspace"
    workspace.mkdir(mode=0o700, exist_ok=True)
    command = build_cc_launch_command(
        session=session,
        workspace=workspace,
        worker_env=worker.env,
        claude_config_dir=claude_config_dir,
        executable=executable,
        actor=actor,
        busy_sentinel=busy_sentinel,
        debug_log=worker.root / "cc-debug.log",
        cc_environment=claude_code_environment(),
    )
    started = run(*command)
    if started.returncode != 0:
        raise AssertionError(f"tmux start failed: {started.stderr[-2000:]}")
    remained = run("tmux", "set-option", "-w", "-t", session, "remain-on-exit", "on")
    if remained.returncode != 0:
        raise AssertionError(f"remain-on-exit failed: {remained.stderr[-2000:]}")

    deadline = time.monotonic() + 60.0
    consented: set[str] = set()
    while time.monotonic() < deadline:
        registered = interactive_session(worker, actor)
        if (
            registered is not None
            and registered.get("sessionRef") != previous_session_ref
        ):
            if registered.get("channelConfirmed") is not True:
                stop_tui(session)
                raise AssertionError(f"session did not confirm Channel: {registered}")
            try:
                assert_claude_config_dir_used(
                    claude_config_dir, worker.root / "cc-debug.log"
                )
            except Exception:
                stop_tui(session)
                raise
            return session, registered
        pane = approve_attended_prompts(session, attended, consented)
        lower = pane.lower()
        if not tmux_alive(session):
            if "not logged in" in lower or "run /login" in lower:
                raise Blocked(f"Claude Code authentication unavailable\n{pane[-3000:]}")
            raise AssertionError(f"CC TUI exited before registration\n{pane[-3000:]}")
        time.sleep(0.2)
    pane = tmux_pane(session)
    stop_tui(session)
    raise AssertionError(f"CC TUI did not register within 60s\n{pane[-3000:]}")


def send_request(
    sender: IsolatedDaemon, actor: str, text: str
) -> dict[str, Any]:
    result = sender.command(
        "message",
        "send",
        "--from",
        DRIVER,
        "--to",
        actor,
        "--summary",
        text,
        text,
        "--json",
    )
    deliveries = result.get("deliveries")
    if result.get("ok") is not True or not isinstance(deliveries, list):
        raise AssertionError(f"send was not accepted: {result}")
    return result


def wait_replies(
    sender: IsolatedDaemon,
    expected: list[str],
    *,
    timeout: float = 180.0,
) -> list[dict[str, Any]]:
    messages = wait_until(
        lambda: pending(sender),
        lambda items: [item.get("summary") for item in items] == expected,
        timeout=timeout,
        label=f"ordered replies {expected}",
    )
    assert isinstance(messages, list)
    return messages


def acknowledge_all(sender: IsolatedDaemon, messages: list[dict[str, Any]]) -> None:
    for message in messages:
        result = sender.command(
            "message",
            "ack",
            "--from",
            DRIVER,
            str(message["messageId"]),
            "--json",
        )
        if result.get("acknowledged") is not True:
            raise AssertionError(f"reply ACK failed: {result}")


def wait_worker_drained(
    worker: IsolatedDaemon, actor: str, *, timeout: float = 20.0
) -> None:
    wait_until(
        lambda: pending(worker, actor),
        lambda messages: messages == [],
        timeout=timeout,
        label="worker terminal reply acknowledgements",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--attended",
        action="store_true",
        help="approve CC workspace/channel consent prompts in the isolated TUI",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help=(
            "layer-2 smoke only: real claude starts, attaches, registers "
            "(channelConfirmed), exits, and releases session visibility. The "
            "deep protocol assertions (reply/ack, explicit ack, busy FIFO, "
            "restart re-signal) are layer-1 contract tests in "
            "tests/daemon/sessions/test_session_carrier_protocol_contract.py and friends — "
            "see docs/notes/tui-test-layering.md."
        ),
    )
    args = parser.parse_args()
    if shutil.which("claude") is None:
        raise Blocked("Claude Code executable is not installed")
    if shutil.which("tmux") is None:
        raise Blocked("tmux is required for a real interactive PTY")

    executable = hyprial_executable()
    endpoint = free_tcp_endpoint()
    nonce = uuid4().hex[:10]
    tui: str | None = None
    with scenario_tempdir("e2e-008", directory=short_temp_root()) as root:
        worker = IsolatedDaemon(
            executable, root / "worker", f"cc-worker-{nonce}", listen=endpoint
        )
        # hyprial agent start claude --name only accepts a bare actor name or the
        # canonical agent:<owner>:<machine>:<actor> URI (4a78455); any other
        # colon form is rejected loudly.  Register under the canonical URI of
        # THIS worker daemon so pins, registration, and delivery agree.
        worker_actor = ""
        # Inside the CC workspace: --allowedTools matches Bash by working
        # directory, and a sentinel outside it is rejected by CC's sandbox
        # once --dangerously-skip-permissions is no longer in play.
        busy_sentinel = worker.root / "cc-workspace" / f"busy-started-{nonce}"
        sender = IsolatedDaemon(
            executable, root / "sender", f"cc-sender-{nonce}", connect=endpoint
        )
        try:
            worker_init = worker.start()
            assert_isolated_network(worker, worker_init, expected_connect=None)
            sender_init = sender.start()
            assert_isolated_network(sender, sender_init, expected_connect=endpoint)
            # resolve-or-reject (Allen b): the CLI probe sender must be a
            # registered identity — no more bare node_id as sender.
            sender.command("agent", "create", "--name", DRIVER, "--json")
            daemon_info = worker.ps().get("daemon", {})
            owner = str(daemon_info.get("owner") or "").strip() if isinstance(daemon_info, dict) else ""
            node_id = str(daemon_info.get("nodeId") or "").strip() if isinstance(daemon_info, dict) else ""
            if not owner or not node_id or ":" in owner or ":" in node_id:
                raise Blocked(f"worker daemon did not report a usable identity: {daemon_info}")
            worker_actor = f"agent:{owner}:{node_id}:cc-tui-{nonce}"
            claude_config_dir = ci_claude_config_dir()
            tui, first_registration = start_tui(
                executable,
                worker,
                worker_actor,
                busy_sentinel,
                claude_config_dir,
                suffix=f"first-{nonce}",
                attended=args.attended,
            )
            first_ref = str(first_registration["sessionRef"])
            print(
                f"PASS register actor={worker_actor} node={worker.node_id} "
                f"session={first_ref}"
            )
            if args.smoke:
                # Layer-2 smoke stops here: the terminal proved it can start,
                # attach and register. Everything below this point is a
                # protocol assertion and lives in layer 1 (fake-carrier
                # contract tests), where a PTY flake cannot masquerade as a
                # product failure. What remains terminal-flavored: a clean
                # TUI exit must release the actor's session visibility.
                stop_tui(tui)
                tui = None
                wait_until(
                    lambda: interactive_session(worker, worker_actor),
                    lambda session: session is None
                    or session.get("online") is False,
                    timeout=15.0,
                    label=(
                        "CC session released after TUI exit "
                        f"actor={worker_actor}"
                    ),
                )
                print("PASS tui-exit releases session visibility")
                print(
                    "RESULT PASS E2E-008 cc-tui-worker smoke "
                    "registered=true channelConfirmed=true exitReleases=true"
                )
                return 0
            sender.wait(
                lambda: sender.command("message", "targets", "--json"),
                lambda value: any(
                    item.get("actor") == worker_actor
                    for item in value.get("targets", [])
                    if isinstance(item, dict)
                ),
                timeout=30.0,
            )

            roundtrip_marker = f"CC_TUI_ROUNDTRIP_{nonce}"
            send_request(
                sender,
                worker_actor,
                f"Reply to this Harness request with exactly {roundtrip_marker}",
            )
            replies = wait_replies(sender, [roundtrip_marker])
            wait_worker_drained(worker, worker_actor)
            acknowledge_all(sender, replies)
            print("PASS channel-wake read reply ack")

            ack_marker = f"CC_TUI_ACK_ONLY_{nonce}"
            send_request(
                sender,
                worker_actor,
                (
                    f"This is the ACK-only marker {ack_marker}. Call harness_read, "
                    "then acknowledge this request without replying by calling "
                    "harness_ack with its messageId. Do not call harness_reply."
                ),
            )
            wait_worker_drained(worker, worker_actor, timeout=120.0)
            if pending(sender):
                raise AssertionError("harness_ack-only request unexpectedly replied")
            print("PASS explicit harness_ack no-reply")

            busy_first = f"CC_TUI_BUSY_1_{nonce}"
            busy_second = f"CC_TUI_BUSY_2_{nonce}"
            send_request(
                sender,
                worker_actor,
                (
                    f"Use Bash to run `touch {busy_sentinel}; sleep 8`, then reply "
                    "to this Harness request "
                    f"with exactly {busy_first}"
                ),
            )
            busy_consented: set[str] = set()
            busy_deadline = time.monotonic() + 90.0
            while not busy_sentinel.is_file():
                if time.monotonic() >= busy_deadline:
                    raise AssertionError(
                        "timeout waiting for CC first busy turn Bash sentinel"
                    )
                approve_attended_prompts(tui, args.attended, busy_consented)
                time.sleep(0.2)
            if pending(sender):
                raise AssertionError("first busy reply arrived before second send")
            send_request(
                sender,
                worker_actor,
                f"Reply to this Harness request with exactly {busy_second}",
            )
            replies = wait_replies(sender, [busy_first, busy_second], timeout=240.0)
            wait_worker_drained(worker, worker_actor)
            acknowledge_all(sender, replies)
            print("PASS busy FIFO replies=2 dropped=0 secondSentDuringFirstTurn=true")

            debug_path = worker.root / "cc-debug.log"
            mcp_exit_marker = "MCP server process exited cleanly"
            exit_count = file_match_count(debug_path, mcp_exit_marker)
            stop_tui(tui)
            tui = None
            wait_until(
                lambda: file_match_count(debug_path, mcp_exit_marker),
                lambda count: count > exit_count,
                timeout=10.0,
                label=f"old CC MCP child exit session={first_ref}",
            )
            # The pane and MCP child have exited — but hyprial agent start refuses to
            # attach while the agent's previous connector is still fenced as
            # live, and that fence lags the MCP child's exit (observed
            # 2026-09-03: the restart start died on "already has a live
            # connector"). The daemon must first release the session's
            # visibility — the same boundary the smoke path asserts on
            # exit — before the restart is attempted.
            wait_until(
                lambda: interactive_session(worker, worker_actor),
                lambda session: session is None
                or session.get("online") is False,
                timeout=15.0,
                label=(
                    "CC session released before restart "
                    f"actor={worker_actor}"
                ),
            )
            # The pane and MCP child have exited. Recovery metadata may remain
            # stale; the fresh child must overwrite it with a fenced sessionRef.
            restart_marker = f"CC_TUI_RESTART_{nonce}"
            offline_send = send_request(
                sender,
                worker_actor,
                f"Reply to this Harness request with exactly {restart_marker}",
            )
            queued = any(
                item.get("queued") is True
                for item in offline_send.get("deliveries", [])
                if isinstance(item, dict)
            )
            worker_pending = pending(worker, worker_actor)
            sender_status = sender.ps()
            if queued:
                if worker_pending or sender_status.get("outboxCount") != 1:
                    raise AssertionError(
                        "offline delivery was not retained in sender outbox"
                    )
            elif not any(
                item.get("messageId") for item in worker_pending
            ) or sender_status.get("outboxCount") != 0:
                raise AssertionError(
                    "offline delivery was not retained in worker inbox"
                )
            tui, resumed = start_tui(
                executable,
                worker,
                worker_actor,
                busy_sentinel,
                claude_config_dir,
                suffix=f"restart-{nonce}",
                attended=args.attended,
                previous_session_ref=first_ref,
            )
            if resumed.get("sessionRef") == first_ref:
                raise AssertionError("CC restart reused the killed session identity")
            replies = wait_replies(sender, [restart_marker])
            wait_until(
                lambda: sender.ps().get("outboxCount"),
                lambda count: count == 0,
                timeout=20.0,
                label="sender offline outbox drain after CC restart",
            )
            wait_worker_drained(worker, worker_actor)
            acknowledge_all(sender, replies)
            print("PASS restart pending re-signalled=true")
        except Exception as error:
            if tui is not None:
                pane = tmux_pane(tui)
                if pane:
                    print(f"CC_TUI_PANE_TAIL\n{pane[-3000:]}")
            debug_path = worker.root / "cc-debug.log"
            if debug_path.is_file():
                # scenario_tempdir preserves only the allowlisted daemon logs
                # (#1778), so cc-debug.log is not kept after this handler
                # re-raises and this tail is its only post-mortem trace.
                # Print it unfiltered: a crash line
                # (e.g. a bare "SIGINT") will not necessarily mention
                # "harness-bridge" or "MCP server", so a keyword filter here
                # can silently hide the one line that matters.
                lines = debug_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                print("CC_DEBUG_TAIL\n" + "\n".join(lines[-200:]))
            raise error
        finally:
            cleanup_errors: list[Exception] = []
            cleanups: list[tuple[str, Callable[[], None]]] = []
            if tui is not None:
                cleanups.append(("CC TUI", lambda: stop_tui(tui)))
            cleanups.extend(
                (
                    ("sender daemon", lambda: stop_daemon(sender)),
                    ("worker daemon", lambda: stop_daemon(worker)),
                )
            )
            for label, cleanup in cleanups:
                try:
                    cleanup()
                except Exception as error:  # noqa: BLE001 - finish all cleanups
                    print(f"CLEANUP FAIL {label}: {error!r}")
                    cleanup_errors.append(error)
            if cleanup_errors and sys.exc_info()[0] is None:
                raise cleanup_errors[0]
    print(
        "RESULT PASS E2E-008 cc-tui-worker "
        "registered=true autoWake=true replyAck=true explicitAck=true "
        "busyFifo=2 busyWitness=true restartResignal=true"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Blocked as error:
        print(f"RESULT BLOCKED E2E-008 {error}")
        raise SystemExit(2) from error
    except (AssertionError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(
            f"RESULT FAIL E2E-008 {type(error).__name__}: {error!r}\n"
            + "".join(traceback.format_exception(error))
        )
        raise SystemExit(1) from error
