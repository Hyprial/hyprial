#!/usr/bin/env python3
"""E2E-012: codex app-server worker launch, roundtrip, and recover.

The dedicated Codex gate over the daemon-mediated path: `hyprial agent start codex
--headless` (managed app-server), exact schema-v1 desired state, a correlated
Harness roundtrip, and daemon-restart recovery with a fresh child PID.  The
thread-identity assertion is capability-driven: native SESSION_LIFECYCLE
requires desired state to persist the pre-restart thread and the restarted
worker to resume that exact thread; if that capability is degraded/removed,
the old fresh-thread assertion applies.  Thread identity is observed read-only
through the real Codex sessions directory (`$CODEX_HOME/sessions`).

Known non-fatal real-environment symptom: `codex app-server` may log
`ERROR ... failed to refresh available models: timeout` on stderr after
startup.  This runner never uses stderr content or a transient
`connectors[].error` string as a failure criterion.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sys

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _diagnostic_text import clip, one_line
from _runner_shared import org as _org
from _daemon_runner import (desired_state_document, 
    IsolatedDaemon,
    hyprial_executable,
    short_temp_root,
    teardown_guard,
)
from _deepseek import (
    MODEL as DEEPSEEK_MODEL,
    PROVIDER as DEEPSEEK_PROVIDER,
    DeepSeekUnavailable,
    deepseek_environment,
    selection_args,
)
from hyprial.daemon.impl.harnesses.capabilities import support
from hyprial.kernel import SupportLevel
from hyprial.kernel import Capability

WORKER_NAME = "e2e-codex-worker"
WORKER_NICKNAME = "e2e-codex"
CONNECTOR_ID = f"codex:{WORKER_NAME}"
ROLLOUT_THREAD = re.compile(
    r"rollout-.+-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)
DEPENDENT_CHECKS = (
    "ps-running",
    "worker-process",
    "desired-state",
    "message-reply",
    "restart-restore",
    "fresh-worker-pid",
    "recover-roundtrip",
    "resumed-thread",
)


@dataclass(frozen=True, slots=True)
class Result:
    status: str
    check: str
    detail: str


class Report:
    def __init__(self) -> None:
        self.results: list[Result] = []

    def add(self, status: str, check: str, detail: str) -> None:
        clean = one_line(detail)
        clean = clip(clean)
        self.results.append(Result(status, check, clean))
        print(f"RESULT {status} E2E-012 {check} {clean}", flush=True)

    def block(self, checks: tuple[str, ...], reason: str) -> None:
        for check in checks:
            self.add("BLOCKED", f"codex.{check}", reason)

    def exit_code(self) -> int:
        counts = {
            status: sum(item.status == status for item in self.results)
            for status in ("PASS", "FAIL", "BLOCKED")
        }
        overall = "FAIL" if counts["FAIL"] else (
            "BLOCKED" if counts["BLOCKED"] else "PASS"
        )
        print(
            "RESULT "
            f"{overall} E2E-012 codex-worker "
            f"pass={counts['PASS']} fail={counts['FAIL']} "
            f"blocked={counts['BLOCKED']}",
            flush=True,
        )
        return 1 if counts["FAIL"] else (2 if counts["BLOCKED"] else 0)


# CLI probe-sender persona (registered at daemon start; Allen b).
DRIVER = "e2e012-driver"


def codex_binary() -> Path | None:
    configured = os.environ.get("HYPRIAL_CODEX_BIN")
    if configured:
        candidate = Path(configured).expanduser()
        return candidate if candidate.is_file() else None
    located = shutil.which("codex")
    return Path(located) if located else None


def sessions_root() -> Path:
    home = os.environ.get("CODEX_HOME")
    base = Path(home).expanduser() if home else Path.home() / ".codex"
    return base / "sessions"


def run_cli(
    daemon: IsolatedDaemon, *args: str, timeout: float = 90.0
) -> tuple[int, dict[str, Any] | None, str]:
    try:
        completed = subprocess.run(
            [str(daemon.executable), *args],
            env=daemon.env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        return 124, None, f"command timed out after {error.timeout}s"
    value: dict[str, Any] | None = None
    try:
        decoded = json.loads(completed.stdout)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        value = decoded
    diagnostic = completed.stderr or completed.stdout or "no diagnostic output"
    return completed.returncode, value, diagnostic[-4000:]


def start_failure_status(diagnostic: str) -> str:
    """Separate missing live prerequisites from Python/CLI contract failures."""

    lowered = diagnostic.lower()
    blocked_markers = (
        "not logged in",
        "authentication",
        "api key",
        "credential",
        "operation not permitted",
        "permission denied",
        "network is unreachable",
        "connection refused",
        "timed out",
    )
    return "BLOCKED" if any(item in lowered for item in blocked_markers) else "FAIL"


def daemon_pid(daemon: IsolatedDaemon) -> int:
    value = daemon.ps()
    metadata = value.get("daemon")
    pid = metadata.get("pid") if isinstance(metadata, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        raise AssertionError(f"daemon ps returned invalid pid: {metadata}")
    return pid


def connector_status(value: dict[str, Any]) -> dict[str, Any] | None:
    connectors = value.get("connectors", [])
    if not isinstance(connectors, list):
        return None
    return next(
        (
            item
            for item in connectors
            if isinstance(item, dict) and item.get("id") == CONNECTOR_ID
        ),
        None,
    )


def wait_running(daemon: IsolatedDaemon, timeout: float = 30.0) -> dict[str, Any]:
    return daemon.wait(
        lambda: daemon.ps(),
        lambda value: bool(
            (status := connector_status(value)) and status.get("running") is True
        ),
        timeout=timeout,
    )


def process_rows() -> list[tuple[int, int, str]]:
    completed = subprocess.run(
        ["ps", "-ww", "-axo", "pid=,ppid=,command="],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.strip() or "ps process inspection failed")
    rows: list[tuple[int, int, str]] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3:
            continue
        try:
            rows.append((int(fields[0]), int(fields[1]), fields[2]))
        except ValueError:
            continue
    return rows


def app_server_pids(owner_pid: int) -> tuple[int, ...]:
    """PIDs of `codex app-server` children owned by the isolated daemon.

    Filtering to descendants of the daemon PID keeps the check immune to any
    unrelated app-server the developer's own Codex installation may run.
    """

    rows = process_rows()
    descendants = {owner_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent, _command in rows:
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return tuple(
        sorted(
            pid
            for pid, _parent, command in rows
            if pid in descendants
            and pid != owner_pid
            and "codex" in command.lower()
            and "app-server" in command
        )
    )


def desired_codex(root: Path, cwd: Path, args: tuple[str, ...]) -> None:
    path = root / "state" / "desired-state.json"  # named for messages only; the store picks the storage
    document = desired_state_document(root)
    providers = document.get("providers", [])
    matches = [
        item
        for item in providers
        if isinstance(item, dict)
        and item.get("provider") == "codex"
        and item.get("name") == WORKER_NAME
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one desired codex provider, found {len(matches)} at {path}"
        )
    persisted = matches[0]
    expected = {
        "provider": "codex",
        "name": WORKER_NAME,
        "nickname": WORKER_NICKNAME,
        "cwd": str(cwd.resolve()),
        "headless": True,
        "ownership": "managed",
        "args": list(args),
        "modelProvider": DEEPSEEK_PROVIDER,
        "model": DEEPSEEK_MODEL,
    }
    mismatches = {
        key: {"expected": expected_value, "actual": persisted.get(key)}
        for key, expected_value in expected.items()
        if persisted.get(key) != expected_value
    }
    if mismatches:
        raise AssertionError(f"desired-state mismatch: {mismatches}")


def desired_codex_session_ref(root: Path) -> str | None:
    """The daemon-persisted Codex thread id, when the spec carries one."""

    try:

        document = desired_state_document(root)

    except Exception:  # noqa: BLE001 - absent/unreadable store means "fresh expected"

        return None
    providers = document.get("providers", [])
    if not isinstance(providers, list):
        return None
    for item in providers:
        if not isinstance(item, dict):
            continue
        if item.get("provider") != "codex" or item.get("name") != WORKER_NAME:
            continue
        value = item.get("sessionRef")
        return value if isinstance(value, str) and value else None
    return None


def codex_session_resume_supported() -> bool:
    """Read the capability table instead of hard-coding the lifecycle row."""

    entry = support(
        "codex",
        headless=True,
        capability=Capability.SESSION_LIFECYCLE,
    )
    return entry is not None and entry.level is SupportLevel.NATIVE


def evaluate_thread_lifecycle(
    *,
    resume_supported: bool,
    persisted_ref: str | None,
    thread_before: str,
    thread_after: str,
    rollout: Path,
) -> str:
    """The restart assertion, shared by the runner and its unit tests."""

    if resume_supported:
        if persisted_ref is None:
            raise AssertionError(
                "capability table declares codex session resume, but desired "
                "state has no sessionRef"
            )
        if persisted_ref != thread_before:
            raise AssertionError(
                "desired-state sessionRef does not match the pre-restart "
                f"thread: {persisted_ref} != {thread_before}"
            )
        if thread_after != persisted_ref:
            raise AssertionError(
                "daemon restart did not resume the persisted app-server "
                f"thread: expected {persisted_ref}, got {thread_after}"
            )
        return (
            "resumed persisted app-server thread "
            f"{persisted_ref} rollout={rollout.name}"
        )
    if persisted_ref is not None:
        raise AssertionError(
            "capability table does not declare codex session resume, but "
            f"desired state carries sessionRef {persisted_ref}"
        )
    if thread_after == thread_before:
        raise AssertionError(
            "daemon restart reused app-server thread "
            f"{thread_before} despite no persisted sessionRef"
        )
    return f"fresh app-server thread {thread_before}->{thread_after} rollout={rollout.name}"


MARKER = "E2E012_CODEX_OK"


def marker_reply(
    items: list[dict[str, Any]],
    *,
    conversation: str,
    request_id: str,
    marker: str,
) -> dict[str, Any] | None:
    """The correlated pending item whose text carries ``marker``, if any.

    Only a real model reply carries the marker; a failure notice in the
    same conversation does not, so it can never stand in for the reply.
    """

    for item in items:
        if (
            item.get("conversationId") == conversation
            and item.get("messageId") != request_id
            and marker in str(item.get("summary") or "")
        ):
            return item
    return None


def roundtrip(
    daemon: IsolatedDaemon, nonce: str, reply_timeout: float
) -> str:
    """Send one nonce-carrying probe, await the correlated reply, ack it."""

    conversation = f"e2e012-{nonce}"
    prompt = (
        f"Harness E2E-012 probe {nonce}. "
        f"Reply with exactly {MARKER} and nothing else."
    )
    sent = daemon.command(
        "message",
        "send",
        "--from",
        DRIVER,
        "--to",
        WORKER_NICKNAME,
        "--conversation",
        conversation,
        "--summary",
        prompt,
        prompt,
        "--json",
    )
    if sent.get("ok") is not True or not isinstance(sent.get("messageId"), str):
        raise AssertionError(f"send was not accepted: {sent}")
    request_id = sent["messageId"]

    # Read the text-carrying surface, never ``ps`` (ids only): any correlated
    # item used to count as the reply, so the daemon's attempt-failure
    # notice (intent=reply, same conversation) passed this scenario for
    # every night codex could not reach its model.
    seen: list[dict[str, Any]] = []

    def probe() -> list[dict[str, Any]]:
        value = daemon.request("message.pending.list", {"actor": DRIVER})
        messages = value.get("messages")
        items = [item for item in messages if isinstance(item, dict)] if isinstance(
            messages, list
        ) else []
        seen[:] = items
        return items

    def accepted(items: list[dict[str, Any]]) -> bool:
        return marker_reply(
            items, conversation=conversation, request_id=request_id, marker=MARKER
        ) is not None

    try:
        items = daemon.wait(probe, accepted, timeout=reply_timeout)
    except AssertionError as error:
        correlated = [
            str(item.get("summary"))[:160]
            for item in seen
            if item.get("conversationId") == conversation
            and item.get("messageId") != request_id
        ]
        raise AssertionError(
            f"no correlated reply carries {MARKER}; correlated={correlated}"
        ) from error
    reply = marker_reply(
        items, conversation=conversation, request_id=request_id, marker=MARKER
    )
    assert reply is not None
    ack = daemon.command(
        "message", "ack", "--from", DRIVER, str(reply["messageId"]), "--json"
    )
    if ack.get("acknowledged") is not True:
        raise AssertionError(f"reply acknowledgement failed: {ack}")
    return conversation


def rollout_thread(nonce: str, since: float) -> tuple[Path, str] | None:
    """The (rollout path, thread id) whose transcript contains ``nonce``.

    Read-only over the real Codex sessions directory; content matching keeps
    the observation immune to unrelated concurrent Codex activity.
    """

    root = sessions_root()
    if not root.is_dir():
        return None
    for path in sorted(root.rglob("rollout-*.jsonl")):
        try:
            if path.stat().st_mtime < since:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if nonce in text:
            match = ROLLOUT_THREAD.search(path.name)
            if match is not None:
                return path, match.group(1).lower()
    return None


def wait_rollout(
    daemon: IsolatedDaemon, nonce: str, since: float, timeout: float = 30.0
) -> tuple[Path, str]:
    value = daemon.wait(
        lambda: rollout_thread(nonce, since),
        lambda found: found is not None,
        timeout=timeout,
    )
    assert value is not None
    return value


def main() -> int:
    report = Report()
    executable = hyprial_executable()
    reply_timeout = float(os.environ.get("HYPRIAL_CODEX_REPLY_TIMEOUT", "180"))
    worker_args = ("-a", "never", "-s", "workspace-write")
    resume_supported = codex_session_resume_supported()
    persisted_ref: str | None = None
    run_started = time.time()

    codex = codex_binary()
    if codex is None:
        reason = "codex executable is not on PATH (or HYPRIAL_CODEX_BIN is invalid)"
        report.add("BLOCKED", "preflight.codex-auth", reason)
        report.add("BLOCKED", "codex.launch", reason)
        report.block(DEPENDENT_CHECKS, reason)
        return report.exit_code()
    version = subprocess.run(
        [str(codex), "--version"], text=True, capture_output=True,
        timeout=10, check=False,
    ).stdout.strip()
    try:
        provider_environment = deepseek_environment()
    except DeepSeekUnavailable as error:
        reason = str(error)
        report.add("BLOCKED", "preflight.codex-auth", reason)
        report.add("BLOCKED", "codex.launch", reason)
        report.block(DEPENDENT_CHECKS, reason)
        return report.exit_code()
    report.add(
        "PASS",
        "preflight.codex-auth",
        f"binary={codex.resolve()} version={version} "
        f"provider={DEEPSEEK_PROVIDER} model={DEEPSEEK_MODEL} credential=ready",
    )

    # A short root keeps state/daemon.sock under the AF_UNIX path limit even
    # after macOS resolves /var/folders/... to /private/var/folders/....
    with tempfile.TemporaryDirectory(
        prefix="hyprial-e2e-012-", dir=short_temp_root()
    ) as temporary:
        root = Path(temporary)
        daemon = IsolatedDaemon(executable, root, "codex-e2e012-node")
        daemon.env.update(provider_environment)
        try:
            daemon.start()
        except (AssertionError, OSError, KeyError) as error:
            report.add("FAIL", "preflight.isolated-daemon", str(error))
            report.add("BLOCKED", "codex.launch", "isolated daemon did not start")
            report.block(DEPENDENT_CHECKS, "isolated daemon did not start")
            return report.exit_code()
        report.add(
            "PASS",
            "preflight.isolated-daemon",
            f"HYPRIAL_HOME={root / 'home'} HARNESS_STATE_DIR={root / 'state'}",
        )
        # resolve-or-reject (Allen b): the CLI probe sender must be a
        # registered identity — no more bare node_id as sender.
        daemon.command("agent", "create", "--name", DRIVER, "--json")
        # Hard cut (send links, 2026-10-15): the worker probe below is
        # agent-to-agent, so this node needs a writable org space.
        _org.ensure_local_org(daemon, DRIVER, prefix="codex-worker")

        def _down_all() -> None:
            try:
                daemon.command("agent", "stop", "--all", "--json")
            except (AssertionError, OSError):
                pass

        with teardown_guard(
            _down_all,
            daemon.stop,
            record=lambda detail: report.add(
                "FAIL",
                "teardown",
                f"daemon teardown also raised: {detail}",
            ),
        ):
            code, value, diagnostic = run_cli(
                daemon,
                "agent",
                "start",
                "codex",
                "--name",
                WORKER_NAME,
                "--nickname",
                WORKER_NICKNAME,
                "--cwd",
                str(root),
                "--headless",
                *selection_args(),
                "--json",
                "--",
                *worker_args,
            )
            if code != 0 or value is None or value.get("ok") is not True:
                detail = (
                    str(value.get("error"))
                    if isinstance(value, dict) and value.get("error")
                    else diagnostic
                )
                status = start_failure_status(detail)
                report.add(
                    status, "codex.launch", f"start command failed ({code}): {detail}"
                )
                report.block(DEPENDENT_CHECKS, "provider launch did not complete")
                return report.exit_code()
            report.add(
                "PASS",
                "codex.launch",
                f"id={value.get('id')} managed headless default is the app-server path",
            )

            try:
                running = wait_running(daemon)
            except (AssertionError, OSError, KeyError) as error:
                report.add("FAIL", "codex.ps-running", str(error))
            else:
                status = connector_status(running)
                report.add(
                    "PASS",
                    "codex.ps-running",
                    f"id={status.get('id') if status else 'missing'} running=true",
                )

            pid_before: int | None = None
            try:
                owner = daemon_pid(daemon)
                pids = daemon.wait(
                    lambda: app_server_pids(owner),
                    lambda found: len(found) == 1,
                    timeout=15.0,
                )
            except (AssertionError, OSError) as error:
                report.add("FAIL", "codex.worker-process", str(error))
            else:
                pid_before = pids[0]
                report.add(
                    "PASS",
                    "codex.worker-process",
                    f"one codex app-server descendant of daemon pid={owner}: "
                    f"child pid={pid_before}",
                )

            try:
                desired_codex(root, root, worker_args)
            except (AssertionError, OSError, json.JSONDecodeError) as error:
                report.add("FAIL", "codex.desired-state", str(error))
            else:
                report.add(
                    "PASS",
                    "codex.desired-state",
                    f"args={json.dumps(list(worker_args), ensure_ascii=False)}",
                )

            thread_before: str | None = None
            nonce_one = f"N{time.time_ns()}"
            try:
                conversation = roundtrip(daemon, nonce_one, reply_timeout)
            except (AssertionError, OSError, KeyError, StopIteration) as error:
                report.add("FAIL", "codex.message-reply", str(error))
                report.add(
                    "BLOCKED",
                    "codex.resumed-thread",
                    "pre-restart roundtrip did not complete",
                )
            else:
                report.add(
                    "PASS",
                    "codex.message-reply",
                    f"conversation={conversation} correlated=true acknowledged=true",
                )
                try:
                    rollout, thread_before = wait_rollout(
                        daemon, nonce_one, run_started
                    )
                except (AssertionError, OSError) as error:
                    report.add(
                        "BLOCKED",
                        "codex.resumed-thread",
                        f"pre-restart rollout not observable: {error}",
                    )
                else:
                    print(
                        f"INFO E2E-012 pre-restart thread={thread_before} "
                        f"rollout={rollout.name}",
                        flush=True,
                    )
                    if resume_supported:
                        try:
                            persisted_ref = daemon.wait(
                                lambda: desired_codex_session_ref(root),
                                lambda ref: ref == thread_before,
                                timeout=10.0,
                            )
                        except AssertionError as error:
                            report.add(
                                "FAIL",
                                "codex.resumed-thread",
                                "desired-state sessionRef did not converge to "
                                f"the pre-restart thread: {error}",
                            )
                    else:
                        persisted_ref = desired_codex_session_ref(root)

            try:
                daemon.stop()
                daemon.start()
                wait_running(daemon)
            except (AssertionError, OSError, KeyError) as error:
                report.add("FAIL", "codex.restart-restore", str(error))
                report.block(
                    ("fresh-worker-pid", "recover-roundtrip", "resumed-thread"),
                    "daemon restart did not restore the worker",
                )
                return report.exit_code()
            report.add(
                "PASS", "codex.restart-restore", "desired codex worker running=true"
            )

            try:
                owner = daemon_pid(daemon)
                pids = daemon.wait(
                    lambda: app_server_pids(owner),
                    lambda found: len(found) == 1,
                    timeout=15.0,
                )
                if pid_before is not None and pids[0] == pid_before:
                    raise AssertionError(
                        f"daemon restart reused app-server child pid={pid_before}"
                    )
            except (AssertionError, OSError) as error:
                report.add("FAIL", "codex.fresh-worker-pid", str(error))
            else:
                detail = (
                    f"child pid {pid_before}->{pids[0]}"
                    if pid_before is not None
                    else f"post-restart child pid={pids[0]} (pre-restart pid unknown)"
                )
                if pid_before is None:
                    report.add("BLOCKED", "codex.fresh-worker-pid", detail)
                else:
                    report.add("PASS", "codex.fresh-worker-pid", detail)

            nonce_two = f"N{time.time_ns()}"
            try:
                conversation = roundtrip(daemon, nonce_two, reply_timeout)
            except (AssertionError, OSError, KeyError, StopIteration) as error:
                report.add("FAIL", "codex.recover-roundtrip", str(error))
                if thread_before is not None:
                    report.add(
                        "BLOCKED",
                        "codex.resumed-thread",
                        "post-restart roundtrip did not complete",
                    )
            else:
                report.add(
                    "PASS",
                    "codex.recover-roundtrip",
                    f"conversation={conversation} correlated=true acknowledged=true",
                )
                if thread_before is not None:
                    try:
                        rollout, thread_after = wait_rollout(
                            daemon, nonce_two, run_started
                        )
                    except (AssertionError, OSError) as error:
                        report.add(
                            "FAIL",
                            "codex.resumed-thread",
                            f"post-restart rollout not observable: {error}",
                        )
                    else:
                        # #147: the capability table owns the expected lifecycle.
                        # Native resume must continue the exact persisted ref;
                        # without that support/ref the older fresh assertion
                        # remains active.
                        try:
                            detail = evaluate_thread_lifecycle(
                                resume_supported=resume_supported,
                                persisted_ref=persisted_ref,
                                thread_before=thread_before,
                                thread_after=thread_after,
                                rollout=rollout,
                            )
                        except AssertionError as error:
                            report.add(
                                "FAIL",
                                "codex.resumed-thread",
                                str(error),
                            )
                        else:
                            report.add(
                                "PASS",
                                "codex.resumed-thread",
                                detail,
                            )
    return report.exit_code()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, KeyError, ValueError) as error:
        print(f"RESULT FAIL E2E-012 runner {error}")
        raise SystemExit(1) from error
