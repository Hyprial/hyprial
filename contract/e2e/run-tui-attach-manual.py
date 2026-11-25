#!/usr/bin/env python3
"""Unified human TUI-attach acceptance entrypoint.

The Codex, Claude, and Pi legs are implemented. This wrapper owns common
progress, version, deadline, signal, and summary behavior while each leg keeps
its protocol-specific probe.

Shared convention for every leg: a leg that spawns isolated daemons or
harness TUIs must strip the ambient worker environment first (see
STRIP_ENV_KEYS / sanitized_env / env_strip_flags in _daemon_runner.py) —
inside a hyprial worker, HYPRIAL_WORKER_* and the daemon socket roots otherwise
pin the PRODUCTION daemon and leak the worker's identity into the test.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
from pathlib import Path

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _runner_shared.ps import ps_from_env
from _deepseek import (
    CC_WORKER_MODEL,
    MODEL as DEEPSEEK_MODEL,
    claude_code_environment,
)

ROOT = Path(__file__).resolve().parent
CODEX_LEG = ROOT / "run-codex-interactive-attach-gate.py"
PI_LEG = ROOT / "run-pi-interactive-attach-manual.py"


def stage(name: str, status: str, detail: str | None = None) -> None:
    suffix = f" {detail}" if detail else ""
    print(f"STAGE={name} {status}{suffix}", flush=True)


def cli_version(command: str) -> str:
    executable = shutil.which(command)
    if executable is None:
        return "unavailable"
    try:
        result = subprocess.run(
            [executable, "--version"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "version-check-timeout"
    lines = [
        line.strip()
        for line in (result.stdout + "\n" + result.stderr).splitlines()
        if line.strip()
    ]
    return next((line for line in lines if command.lower() in line.lower()), lines[0] if lines else "unknown")[:160]


def stop_child(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        # The Codex leg owns its app-server child in a separate process group
        # and cleans that group in its Python finally block.  SIGINT lets the
        # leg translate cancellation into KeyboardInterrupt and run cleanup;
        # TERM/KILL are escalation only when that cooperative path stalls.
        os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=20)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def cleanup_owned_artifacts(pids: set[int], roots: set[str]) -> None:
    """Last-resort cleanup for exact artifacts printed by the Codex leg."""

    for _attempt in range(10):
        alive: list[int] = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        for pid in alive:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.3)
        if not alive:
            break
        if _attempt >= 3:
            for pid in alive:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    for root in roots:
        shutil.rmtree(root, ignore_errors=True)


def run_codex(
    args: argparse.Namespace,
    deadline: float,
    reason_out: dict[str, str] | None = None,
) -> str:
    stage("codex", "START")
    print(f"CLI_VERSION=codex {cli_version('codex')}", flush=True)
    command = [
        sys.executable,
        str(CODEX_LEG),
        "--inject-only",
        "--model",
        args.model,
        "--setup-timeout",
        str(args.setup_timeout),
        "--manual-timeout",
        str(args.manual_timeout),
        "--discovery-attempts",
        str(args.discovery_attempts),
        "--discovery-backoff",
        str(args.discovery_backoff),
    ]
    process = subprocess.Popen(
        command,
        cwd=ROOT.parent.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    result = "BLOCKED"
    result_detail: str | None = None
    pids: set[int] = set()
    roots: set[str] = set()
    try:
        while time.monotonic() < deadline:
            for key, _ in selector.select(timeout=0.5):
                line = key.fileobj.readline()
                if line:
                    clean = line.rstrip("\r\n")
                    print(clean, flush=True)
                    root_match = re.search(r"STAGE=temp-state OK root=(\S+)", clean)
                    if root_match:
                        root = Path(root_match.group(1))
                        roots.add(str(root))
                    pid_match = re.search(r"STAGE=\S+ PID pid=(\d+)", clean)
                    if pid_match:
                        pids.add(int(pid_match.group(1)))
                    if clean.startswith("RESULT "):
                        result_payload = clean.split(maxsplit=1)[1]
                        value = result_payload.split()[0]
                        result = value if value in {"PASS", "FAIL", "BLOCKED"} else "FAIL"
                        result_detail = result_payload[len(value):].strip() or None
            if process.poll() is not None:
                break
        else:
            print("STAGE=codex BLOCKED global-timeout", flush=True)
            result = "BLOCKED"
            result_detail = "global-timeout"
    except KeyboardInterrupt:
        print("STAGE=codex BLOCKED interrupted-by-user", flush=True)
        result = "BLOCKED"
    finally:
        selector.close()
        print(
            "STAGE=codex cleanup-targets "
            f"roots={sorted(roots)} pids={sorted(pids)}",
            flush=True,
        )
        stop_child(process)
        cleanup_owned_artifacts(pids, roots)
    if result_detail:
        if result == "BLOCKED" and result_detail.startswith("turn/start receive timeout"):
            result_detail = "turn accepted but no response; " + result_detail
        stage("codex", result, result_detail)
        if reason_out is not None and result == "BLOCKED":
            reason_out["codex"] = result_detail
    else:
        stage("codex", result)
    return result


def claude_leg_launch_command(
    *,
    session: str,
    workspace: Path,
    root: Path,
    config_dir: Path,
    hyprial: list[str],
    name: str,
    cc_environment: dict[str, str],
) -> list[str]:
    """The tmux command that launches the attach-manual Claude leg's TUI.

    Same contract as E2E-008's build_cc_launch_command: DeepSeek rides its
    official Claude Code environment mapping (``cc_environment`` — see
    _deepseek.claude_code_environment), not hyprial's ``--provider deepseek``
    whose mapping would overwrite the official table with the raw
    "deepseek-flash" id that CC's model catalog fatal-exits on. The
    selection stays catalog-known (CC_WORKER_MODEL = "haiku") and maps to
    flash through ANTHROPIC_DEFAULT_HAIKU_MODEL.

    ``cc_environment`` contains the API key; never print or persist the
    returned command.
    """

    return [
        "tmux", "new-session", "-d", "-s", session, "-c", str(workspace), "--",
        "env", f"HYPRIAL_HOME={root}", f"CLAUDE_CONFIG_DIR={config_dir}",
        "UV_CACHE_DIR=/tmp/uv-cache-claude-leg", "TERM=xterm-256color",
        *[f"{key}={value}" for key, value in cc_environment.items()],
        *hyprial, "agent", "start", "claude", "--name", name, "--cwd", str(workspace),
        "--model", CC_WORKER_MODEL,
    ]


def run_claude(args: argparse.Namespace, deadline: float) -> str:
    stage("claude", "START")
    print(f"CLI_VERSION=claude {cli_version('claude')}", flush=True)
    repo = ROOT.parent.parent
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude-ci"))
    if not (config_dir / ".credentials.json").is_file():
        print("RESULT BLOCKED claude CI-dedicated credentials missing", flush=True)
        stage("claude", "BLOCKED", "missing-ci-credentials")
        return "BLOCKED"
    try:
        onboarding = json.loads(
            (config_dir / ".claude.json").read_text(encoding="utf-8")
        ).get("hasCompletedOnboarding")
    except (OSError, ValueError):
        onboarding = None
    if onboarding is not True:
        print("RESULT BLOCKED claude CI onboarding incomplete", flush=True)
        stage("claude", "BLOCKED", "ci-onboarding-incomplete")
        return "BLOCKED"

    root = Path(tempfile.mkdtemp(prefix="hyprial-claude-leg-", dir="/tmp"))
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    session = f"claude-attach-leg-{os.getpid()}-{uuid4().hex[:8]}"
    name = f"claude-leg-{uuid4().hex[:8]}"
    daemon_pid: int | None = None
    pane_pid: int | None = None
    last_prompt_action: tuple[str, ...] | None = None
    env = dict(os.environ)
    for key in ("HARNESS_STATE_DIR", "HARNESS_SOCKET_PATH", "HYPRIAL_ZENOH_LISTEN", "HYPRIAL_ZENOH_CONNECT"):
        env.pop(key, None)
    env.update(
        {
            "HYPRIAL_HOME": str(root),
            "HYPRIAL_OWNER": "e2e",
            "HYPRIAL_NODE_ID": f"claude-leg-{uuid4().hex[:8]}",
            "HYPRIAL_NETWORK_ISOLATED": "1",
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "UV_CACHE_DIR": "/tmp/uv-cache-claude-leg",
        }
    )
    hyprial = ["uv", "run", "--project", str(repo), "hyprial"]

    def command(*args_: str, timeout: float = 30.0) -> dict[str, object]:
        completed = subprocess.run(
            [*hyprial, *args_],
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"hyprial {' '.join(args_)} exited {completed.returncode}: "
                f"{completed.stdout[-1000:]} {completed.stderr[-1000:]}"
            )
        value = json.loads(completed.stdout)
        if not isinstance(value, dict):
            raise RuntimeError(f"hyprial {' '.join(args_)} returned non-object JSON")
        return value

    def kill_pid(pid: int | None) -> None:
        if pid is None or pid <= 0:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def pane_alive() -> bool:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_dead}"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        return any(
            row.split("\t", 1)[0] == session and not row.endswith("\t1")
            for row in result.stdout.splitlines()
        )

    def capture() -> str:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-S", "-300", "-t", session],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        return result.stdout

    def approve_prompts() -> str:
        nonlocal last_prompt_action
        text = capture()
        lower = text.lower()
        prompts: list[str] = []
        if "yes, i trust this folder" in lower:
            prompts.append("workspace trust")
        if "warning: loading development channels" in lower and "local development" in lower:
            prompts.append("local development Channel")
        if "new mcp server found" in lower and "harness-bridge" in lower:
            prompts.append("harness-bridge MCP")
        signature = tuple(prompts)
        if not prompts:
            last_prompt_action = None
        elif signature != last_prompt_action:
            print(f"MANUAL_ACTION=Claude TUI prompt: approve {', '.join(prompts)}", flush=True)
            last_prompt_action = signature
            if args.attended:
                # CC 2.1.2xx's "Quick safety check" dialog defaults to
                # "No, exit"; a bare Enter would exit CC before registration
                # (same fix as run-cc-tui-worker's trust-dialog approval).
                if "workspace trust" in prompts:
                    subprocess.run(["tmux", "send-keys", "-t", session, "Down"], check=False, timeout=5)
                subprocess.run(["tmux", "send-keys", "-t", session, "Enter"], check=False, timeout=5)
                print(f"STAGE=claude-consent OK prompts={','.join(prompts)}", flush=True)
        return text

    def wait_for(label: str, predicate: object, timeout: float) -> None:
        end = min(time.monotonic() + timeout, deadline)
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.2)
        raise RuntimeError(f"claude leg timeout waiting for {label}")

    try:
        stage("claude-daemon", "START")
        print(f"HYPRIAL_SOURCE=uv run --project {repo}", flush=True)
        init = command("init", "--json", timeout=min(30.0, max(0.1, deadline - time.monotonic())))
        # init's readiness answer is the flat ping payload on new builds;
        # older builds nested it under "daemon".
        daemon = init.get("daemon") if isinstance(init.get("daemon"), dict) else init
        daemon_pid = daemon.get("pid")
        if not isinstance(daemon_pid, int) or daemon.get("running") is not True:
            raise RuntimeError(f"isolated daemon not ready: {init}")
        warnings = init.get("warnings")
        print(f"HYPRIAL_ISOLATION=HYPRIAL_HOME={root} HARNESS_STATE_DIR=unset HARNESS_SOCKET_PATH=unset HYPRIAL_ZENOH_LISTEN=unset", flush=True)
        if warnings:
            print(f"HYPRIAL_WARNINGS={warnings}", flush=True)
        stage("claude-daemon", "OK", f"pid={daemon_pid}")

        stage("claude-tui", "START")
        started = subprocess.run(
            claude_leg_launch_command(
                session=session,
                workspace=workspace,
                root=root,
                config_dir=config_dir,
                hyprial=hyprial,
                name=name,
                cc_environment=claude_code_environment(),
            ),
            cwd=repo,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if started.returncode != 0:
            raise RuntimeError(f"tmux claude start failed: {started.stderr[-1000:]}")
        subprocess.run(
            ["tmux", "set-option", "-w", "-t", session, "remain-on-exit", "on"],
            check=False,
            timeout=5,
        )
        pane_pid_text = subprocess.run(
            ["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        ).stdout.strip()
        pane_pid = int(pane_pid_text) if pane_pid_text.isdigit() else None
        stage("claude-tui", "OK", f"tmux={session} pane_pid={pane_pid}")

        stage("channel-registration", "START")
        print(f"MANUAL_ACTION=If Claude shows a trust/Channel prompt, attach with tmux attach -t {session} and approve it", flush=True)
        actor: str | None = None
        registered: dict[str, object] | None = None
        while time.monotonic() < deadline:
            status = ps_from_env(env)
            sessions = status.get("interactiveSessions", [])
            if isinstance(sessions, list):
                for item in sessions:
                    if (
                        isinstance(item, dict)
                        and item.get("channelConfirmed") is True
                        and item.get("runtime") in {"interactive", "claude_interactive"}
                    ):
                        actor = str(item.get("actor"))
                        registered = item
                        break
            if registered is not None:
                break
            pane_text = approve_prompts()
            if not pane_alive():
                if "yes, i trust this folder" in pane_text.lower():
                    raise RuntimeError("Claude TUI exited at workspace trust prompt before channelConfirmed")
                raise RuntimeError(f"Claude TUI exited before channelConfirmed; pane={pane_text[-2000:]}")
            time.sleep(0.5)
        if registered is None or actor is None:
            raise RuntimeError(f"channelConfirmed was not observed within deadline; pane={capture()[-2000:]}")
        stage("channel-registration", "OK", f"actor={actor} channelConfirmed=true sessionRef={registered.get('sessionRef')}")
        marker = f"CLAUDE_TUI_MANUAL_{uuid4().hex[:10]}"
        print(f"MANUAL_ACTION=查看 Claude TUI，等待消息/回复标记 {marker}", flush=True)
        stage("claude-send", "START")
        sender = command(
            "message", "send", "--from", str(ps_from_env(env).get("daemon", {}).get("nodeId")),
            "--to", actor,
            "--summary", f"Reply with exactly {marker} and do not use tools.",
            f"Reply with exactly {marker} and do not use tools.", "--json",
            timeout=30,
        )
        deliveries = sender.get("deliveries")
        if not isinstance(deliveries, list):
            raise RuntimeError(f"isolated hyprial message send returned no deliveries: {sender}")
        stage("claude-send", "OK", f"marker={marker}")
        wait_for("Claude TUI marker", lambda: marker in capture(), timeout=120)
        print(f"PASS claude TUI marker visible marker={marker}", flush=True)
        print(f"RESULT PASS claude channelConfirmed=true sendVisible=true marker={marker}", flush=True)
        return "PASS"
    except TimeoutError as error:
        stage("claude", "BLOCKED", f"timeout={error}")
        print(f"RESULT BLOCKED claude {error}", flush=True)
        return "BLOCKED"
    except Exception as error:
        try:
            print(f"CLAUDE_TUI_PANE_TAIL\n{capture()[-3000:]}", flush=True)
        except Exception:
            pass
        for log_path in (root / "state" / "daemon-launch.log", root / "state" / "daemon.json"):
            try:
                if log_path.is_file():
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    print(f"CLAUDE_DIAGNOSTIC_TAIL {log_path.name}\n" + "\n".join(lines[-80:]), flush=True)
            except OSError:
                pass
        pane_text = capture()
        if "trust this folder" in pane_text.lower() or "channel prompt" in str(error).lower():
            stage("claude", "BLOCKED", f"{type(error).__name__}: {error}")
            print(f"RESULT BLOCKED claude {type(error).__name__}: {error}", flush=True)
            return "BLOCKED"
        stage("claude", "FAIL", f"{type(error).__name__}: {error}")
        print(f"RESULT FAIL claude {type(error).__name__}: {error}", flush=True)
        return "FAIL"
    finally:
        stage("claude-cleanup", "START")
        try:
            if pane_alive():
                subprocess.run(["tmux", "send-keys", "-t", session, "C-c"], check=False, timeout=5)
                time.sleep(1)
            if subprocess.run(["tmux", "has-session", "-t", session], check=False, timeout=5).returncode == 0:
                subprocess.run(["tmux", "kill-session", "-t", session], check=False, timeout=5)
        finally:
            kill_pid(pane_pid)
            kill_pid(daemon_pid)
            shutil.rmtree(root, ignore_errors=True)
            stage("claude-cleanup", "OK")


# LEG INTERFACE FOR FUTURE CLAUDE/PI OWNERS:
#   run_<harness>(args, deadline) -> "PASS" | "BLOCKED" | "FAIL"
#   print STAGE=<leg> START/OK|BLOCKED|FAIL, CLI_VERSION=<harness> ...,
#   MANUAL_ACTION=<human instruction>, and RESULT <status> <details>.
#   Cleanup must be complete before returning; --all consumes the returned
#   status and prints exactly one SUMMARY line per harness. Pi PR2 should
#   replace only skip_leg("pi", ...) with this interface.

def run_pi(args: argparse.Namespace, deadline: float) -> str:
    stage("pi", "START")
    print(f"CLI_VERSION=pi {cli_version('pi')}", flush=True)
    command = [
        sys.executable,
        str(PI_LEG),
        "--setup-timeout",
        str(args.setup_timeout),
        "--manual-timeout",
        str(args.manual_timeout),
    ]
    command += ["--model", args.pi_model or DEEPSEEK_MODEL]
    process = subprocess.Popen(
        command,
        cwd=ROOT.parent.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    result = "BLOCKED"
    pids: set[int] = set()
    roots: set[str] = set()
    try:
        while time.monotonic() < deadline:
            for key, _ in selector.select(timeout=0.5):
                line = key.fileobj.readline()
                if line:
                    clean = line.rstrip("\r\n")
                    print(clean, flush=True)
                    root_match = re.search(r"STAGE=temp-state OK root=(\S+)", clean)
                    if root_match:
                        roots.add(str(Path(root_match.group(1))))
                    pid_match = re.search(r"STAGE=\S+ PID pid=(\d+)", clean)
                    if pid_match:
                        pids.add(int(pid_match.group(1)))
                    if line.startswith("RESULT "):
                        value = line.split(maxsplit=1)[1].split()[0]
                        result = value if value in {"PASS", "FAIL", "BLOCKED"} else "FAIL"
            if process.poll() is not None:
                break
        else:
            print("STAGE=pi BLOCKED global-timeout", flush=True)
            result = "BLOCKED"
    except KeyboardInterrupt:
        print("STAGE=pi BLOCKED interrupted-by-user", flush=True)
        result = "BLOCKED"
    finally:
        selector.close()
        print(
            "STAGE=pi cleanup-targets "
            f"roots={sorted(roots)} pids={sorted(pids)}",
            flush=True,
        )
        stop_child(process)
        cleanup_owned_artifacts(pids, roots)
    stage("pi", result)
    return result


def skip_leg(name: str, pending: str) -> str:
    stage(name, "START")
    print(f"CLI_VERSION={name} {cli_version(name)}", flush=True)
    print(f"MANUAL_ACTION=等待 {pending} 就绪", flush=True)
    print(f"RESULT SKIP {name} waiting {pending}", flush=True)
    stage(name, "SKIP", f"waiting={pending}")
    return "SKIP"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", choices=("codex", "claude", "pi", "all"), required=True)
    parser.add_argument("--model", default=DEEPSEEK_MODEL)
    parser.add_argument(
        "--pi-model",
        default=DEEPSEEK_MODEL,
        help="model for the pi leg (default: deepseek-flash)",
    )
    parser.add_argument("--setup-timeout", type=float, default=300.0)
    parser.add_argument("--manual-timeout", type=float, default=300.0)
    parser.add_argument("--discovery-attempts", type=int, default=10)
    parser.add_argument("--discovery-backoff", type=float, default=2.0)
    parser.add_argument(
        "--attended",
        action="store_true",
        help="auto-approve only isolated Claude setup prompts during self-test",
    )
    args = parser.parse_args()
    if args.setup_timeout <= 0 or args.manual_timeout <= 0 or args.discovery_attempts <= 0 or args.discovery_backoff < 0:
        print("RESULT BLOCKED invalid positive timeout/discovery settings", flush=True)
        return 2

    stage("framework", "START", f"harness={args.harness}")
    global_deadline = time.monotonic() + args.setup_timeout + args.manual_timeout
    results: dict[str, str] = {}
    result_reasons: dict[str, str] = {}
    if args.harness in {"codex", "all"}:
        results["codex"] = run_codex(args, global_deadline, result_reasons)
    if args.harness in {"claude", "all"}:
        results["claude"] = run_claude(args, global_deadline)
    if args.harness in {"pi", "all"}:
        results["pi"] = run_pi(args, global_deadline)

    stage("framework", "OK")
    if args.harness == "all":
        codex_summary = f"SUMMARY codex {results['codex']}"
        if results["codex"] == "BLOCKED" and result_reasons.get("codex"):
            codex_summary += f" reason={result_reasons['codex']}"
        print(codex_summary, flush=True)
        print(f"SUMMARY claude {results['claude']}", flush=True)
        print(f"SUMMARY pi {results['pi']}", flush=True)
    if any(value == "FAIL" for value in results.values()):
        return 1
    if any(value == "BLOCKED" for value in results.values()):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
