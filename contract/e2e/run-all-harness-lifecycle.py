#!/usr/bin/env python3
"""E2E-013: all-harness lifecycle roundtrip with mid-life harness and model switch.

One full lifecycle per harness (agent create -> hyprial agent start -> registration ->
correlated roundtrip) for claude, pi, and codex; the A9 mid-life harness
switch on one living agent (same entity, zeroed context, surviving cell binding);
a same-harness model switch; a #106 kill -9 convergence cross-check; and a
real Lark leg proving an inbound DM reaches the *bound agent*, which then
proactively sends byte-verified image/file attachments in that same turn.

Config-load assertions (coverage gap named by Allen, 2026-08-15: config
*recorded* is not config *effective*): every managed worker's OS-level
process cwd must equal the agent's configured cwd (``lsof`` on the
daemon-descendant worker PID; macOS has no /proc); a runner-generated skill
passed via pi ``--skill`` must provably influence behavior (a CFGPROBE
roundtrip replies with a nonce token that exists only inside the skill
file); and the headless busy-FIFO counterpart of E2E-008's cc TUI check
runs once each against pi and codex (a second message delivered mid-turn
is answered exactly once, in delivery order, with no loss).

The Lark reply bridge (PR #110, docs/reference/p3-mcp.md "Correlated Lark reply
bridge") is asserted in full: the bound worker's `harness_reply` is
bridged through the supervised adapter worker into a native Lark reply,
and the sender-side terminal record reads `fetched`/`ACK_RECEIVED`
held by the sender daemon (the adapter worker is a supervised local
transport endpoint, never a second inbox).
The agent sends attachments through an independent configured route because
``harness_reply`` is text-only; a crafted attachment-bearing reply is asserted
to fail loudly before lookup or acknowledgement.
`--skip-reply-bridge` remains only for checkouts predating PR #110.

Known non-fatal real-environment symptom (same as E2E-012): `codex
app-server` may log `ERROR ... failed to refresh available models: timeout`
on stderr after startup.  This runner never uses stderr content or a
transient `connectors[].error` string as a failure criterion.
"""

from __future__ import annotations

import argparse as argparse
import hashlib as hashlib
import json as json
import os
import re
import shlex as shlex
import shutil as shutil
import signal as signal
import socket as socket
import subprocess as subprocess
import tempfile as tempfile
import time as time
from xml.etree import ElementTree
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4 as uuid4

import sys

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness_lifecycle import processes as _processes_impl  # noqa: E402
from _harness_lifecycle import lark_auth as _lark_auth_impl  # noqa: E402
from _harness_lifecycle import lark_messages as _lark_messages_impl  # noqa: E402
from _harness_lifecycle import configuration as _configuration_impl  # noqa: E402
from _harness_lifecycle.phases import worker as _worker_impl  # noqa: E402
from _harness_lifecycle.phases import queue as _queue_impl  # noqa: E402
from _harness_lifecycle.phases import migration as _migration_impl  # noqa: E402
from _harness_lifecycle.phases import recovery as _recovery_impl  # noqa: E402
from _harness_lifecycle.phases import lark as _lark_impl  # noqa: E402
from _harness_lifecycle import scenario as _scenario_impl  # noqa: E402

from _diagnostic_text import clip, one_line
from _daemon_runner import (desired_state_document as desired_state_document,
    IsolatedDaemon,
    hyprial_executable as hyprial_executable,
    short_temp_root as short_temp_root,
    teardown_guard as teardown_guard,
)
from _deepseek import (
    MODEL as DEEPSEEK_MODEL,
    PROVIDER as DEEPSEEK_PROVIDER,
    DeepSeekUnavailable as DeepSeekUnavailable,
    deepseek_environment as deepseek_environment,
)

# Canonical XML module exposed through the live entry context (also patchable).
ET = ElementTree

SCENARIO = "E2E-013"

# Every real harness uses one API budget.  The alternate Claude spelling is
# deliberately a sonnet alias: DeepSeek's Anthropic endpoint maps it to
# deepseek-flash, while desired-state still observes a changed model id.
CLAUDE_MODEL = DEEPSEEK_MODEL
CLAUDE_MODEL_ALT = os.environ.get(
    "HYPRIAL_E2E013_CLAUDE_MODEL_ALT", "claude-sonnet-4-5"
)
PI_PROVIDER = DEEPSEEK_PROVIDER
PI_MODEL = DEEPSEEK_MODEL
REPLY_TIMEOUT = float(os.environ.get("HYPRIAL_E2E013_REPLY_TIMEOUT", "240"))
LARK_REPLY_TIMEOUT = float(os.environ.get("HYPRIAL_E2E013_LARK_REPLY_TIMEOUT", "240"))

MORPH_NAME = "e2e013-morph"
PIN_PROBE_ADAPTER = "e2e013-pin-probe"

# -- Lark leg (dedicated test App only; production Apps are a red line) ------
LARK_APP_NAME = "allenwoods-hyprial-e2e"
LARK_APP_ID = "cli_aaf9ea19d4b8dbe4"
LARK_SECRET_PATH = Path.home() / ".hyprial" / "secrets" / "lark-allenwoods-hyprial-e2e.json"
#: CI credential injection (e2e-macos-credentialed.yml).  When both are set the
#: Lark leg takes the App credential from the environment and never opens
#: the production secret file; a wrong injected id blocks loudly.
LARK_APP_ID_ENV_VAR = "HYPRIAL_E2E_LARK_APP_ID"
LARK_APP_SECRET_ENV_VAR = "HYPRIAL_E2E_LARK_APP_SECRET"
LARK_CLI = Path("/opt/homebrew/bin/lark-cli")
LARK_CHANNEL_ACTOR = f"adapter:lark:{LARK_APP_NAME}"
LARK_OUTBOUND_ROUTE = "e2e013-agent-outbound"
ONIT_TIMEOUT = 60.0


def pi_sessions_root() -> Path:
    """Where the real pi CLI writes session transcripts.

    Read-only and content-matched by nonce, exactly like E2E-012 reads Codex
    rollouts.  PI_CODING_AGENT_DIR is pi's own root: the controlled CI job
    points it at ~/.pi-ci/agent and a P1 pi worker inherits it, so the
    sessions live there, not under the home default.
    """

    configured = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    base = Path(configured).expanduser() if configured else Path.home() / ".pi" / "agent"
    return base / "sessions"


# CLI probe-sender persona (registered at daemon start; Allen b).
DRIVER = "e2e013-driver"

# A valid 1x1 transparent PNG.  The agent sends this exact artifact so the
# user-side download can be compared byte-for-byte, not merely observed.
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)

_NATIVE_ID_PATTERN = re.compile(r"(?<![0-9A-Za-z])(?:oc|ou|om)_[0-9A-Za-z_-]+")


def redact_native_ids(value: str) -> str:
    """Replace Lark chat/user/message ids with stable non-sensitive labels."""

    import hashlib

    def label(match: re.Match[str]) -> str:
        raw = match.group(0)
        kind = {"oc": "chat", "ou": "user", "om": "message"}[raw[:2]]
        return f"{kind}#{hashlib.sha256(raw.encode()).hexdigest()[:12]}"

    return _NATIVE_ID_PATTERN.sub(label, value)


@dataclass(frozen=True, slots=True)
class HarnessCase:
    harness: str
    vendor: str  # agent-record provider (model vendor vocabulary)
    name: str
    nickname: str
    args: tuple[str, ...]
    marker: str


@dataclass(frozen=True, slots=True)
class Result:
    status: str
    check: str
    detail: str


class Report:
    def __init__(self) -> None:
        self.results: list[Result] = []
        #: Values that must never appear in any report line (e.g. app secret).
        self.forbidden: list[str] = []

    def add(self, status: str, check: str, detail: str) -> None:
        clean = one_line(detail)
        clean = redact_native_ids(clean)
        for value in self.forbidden:
            if value and value in clean:
                clean = clean.replace(value, "[REDACTED]")
        clean = clip(clean)
        self.results.append(Result(status, check, clean))
        print(f"RESULT {status} {SCENARIO} {check} {clean}", flush=True)

    def block(self, checks: tuple[str, ...], reason: str) -> None:
        for check in checks:
            self.add("BLOCKED", check, reason)

    def secrets_clean(self) -> bool:
        joined = "\n".join(
            f"{item.status} {item.check} {item.detail}" for item in self.results
        )
        return not any(value and value in joined for value in self.forbidden)

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
            f"{overall} {SCENARIO} all-harness-lifecycle "
            f"pass={counts['PASS']} fail={counts['FAIL']} "
            f"blocked={counts['BLOCKED']}",
            flush=True,
        )
        return 1 if counts["FAIL"] else (2 if counts["BLOCKED"] else 0)


# -- CLI/IPC helpers ---------------------------------------------------------


def run_cli(
    daemon: IsolatedDaemon, *args: str, timeout: float = 90.0
) -> tuple[int, dict[str, Any] | None, str]:
    return _processes_impl.run_cli({'json': json, 'subprocess': subprocess}, daemon, *args, timeout=timeout)


def start_failure_status(diagnostic: str) -> str:
    """Separate missing live prerequisites from Python/CLI contract failures."""
    return _processes_impl.start_failure_status({}, diagnostic)


def daemon_ipc_response(
    root: Path, method: str, params: dict[str, Any]
) -> dict[str, Any]:
    """One request over the isolated daemon's own socket (never production's).

    This exposes both success and typed-error frames.  The success wrapper is
    used for ``message.pending.list``; E2E-013 also inspects one intentional
    ``message.reply`` failure to pin the text-only harness_reply boundary.
    """
    return _processes_impl.daemon_ipc_response({'json': json, 'socket': socket, 'uuid4': uuid4}, root, method, params)


def daemon_ipc(root: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return _processes_impl.daemon_ipc({'daemon_ipc_response': daemon_ipc_response}, root, method, params)


def pending_for(root: Path, actor: str) -> list[dict[str, Any]]:
    return _processes_impl.pending_for({'daemon_ipc': daemon_ipc}, root, actor)


# -- daemon observation helpers ----------------------------------------------


def daemon_pid(daemon: IsolatedDaemon) -> int:
    return _processes_impl.daemon_pid({}, daemon)


def connector_status(
    value: dict[str, Any], connector_id: str
) -> dict[str, Any] | None:
    return _processes_impl.connector_status({}, value, connector_id)


def wait_running(
    daemon: IsolatedDaemon, connector_id: str, timeout: float = 30.0
) -> dict[str, Any]:
    return _processes_impl.wait_running({'connector_status': connector_status}, daemon, connector_id, timeout)


def process_rows() -> list[tuple[int, int, str]]:
    return _processes_impl.process_rows({'subprocess': subprocess})


class ProbeObservationError(RuntimeError):
    """The probe's own observation channel failed — NOT a product result.

    探针失效 ≠ 被测失败:pid 易主、回读 API 连续失败这类问题只能说明
    探针没看到东西,不能说明被测行为失败。混报会让人修错方向(把观测
    层抖动当成产品回归去修产品),所以调用点必须把这类错误单独归
    BLOCKED,永远不归 FAIL,也绝不允许记绿。
    """


class ReadbackExhausted(ProbeObservationError):
    """A Lark read-back failed every retry; delivery is unproven either way."""


def _is_pi_worker_command(command: str) -> bool:
    return _processes_impl._is_pi_worker_command({}, command)


def _is_codex_appserver_command(command: str) -> bool:
    return _processes_impl._is_codex_appserver_command({}, command)


def _is_claude_cli_command(command: str) -> bool:
    return _processes_impl._is_claude_cli_command({}, command)


def pi_worker_pids(owner_pid: int) -> tuple[int, ...]:
    """PIDs of pi worker children that descend from the isolated daemon.

    Filtering to descendants keeps the check immune to unrelated processes
    the developer's own tools may run (same technique as E2E-012).  The pi
    CLI rewrites its process title to a bare ``pi`` (measured: ``ps -ww``
    shows no argv at all), so the match accepts either the bare title or an
    argv-preserving spelling that still carries ``--mode rpc``.
    """
    return _processes_impl.pi_worker_pids({'_is_pi_worker_command': _is_pi_worker_command, 'daemon_descendant_pids': daemon_descendant_pids}, owner_pid)


def codex_appserver_pids(owner_pid: int) -> tuple[int, ...]:
    """PIDs of `codex app-server` children owned by the isolated daemon.

    Same filter as E2E-012: descendants of the daemon PID whose command names
    a codex app-server, immune to any unrelated developer-owned instance.
    """
    return _processes_impl.codex_appserver_pids({'_is_codex_appserver_command': _is_codex_appserver_command, 'daemon_descendant_pids': daemon_descendant_pids}, owner_pid)


def daemon_descendant_pids(
    owner_pid: int, matcher: Callable[[str], bool]
) -> tuple[int, ...]:
    """PIDs descending from the isolated daemon whose command satisfies ``matcher``.

    The same PPID-chain walk as ``pi_worker_pids``/``codex_appserver_pids``,
    factored for the additional per-harness probes; descendant filtering keeps
    every probe immune to unrelated developer-owned processes.
    """
    return _processes_impl.daemon_descendant_pids({'process_rows': process_rows}, owner_pid, matcher)


def claude_cli_pids(owner_pid: int) -> tuple[int, ...]:
    """PIDs of the Agent SDK's bundled Claude Code CLI child.

    The managed claude worker is a three-deep chain: the daemon spawns a
    ``uv run --isolated`` wrapper, which runs the python SDK worker, and the
    SDK spawns the actual ``claude`` CLI.  Only the CLI child is given the
    spec's cwd (measured: the uv/python wrappers keep the daemon's own cwd),
    so the cwd assertion must target it.  Its argv0 basename is ``claude``,
    which excludes both wrappers (their commands merely *mention*
    ``claude-agent-sdk``) and the worker's
    ``python -m hyprial.daemon.impl.mcp.entry agent-channel``
    python child.
    """
    return _processes_impl.claude_cli_pids({'_is_claude_cli_command': _is_claude_cli_command, 'daemon_descendant_pids': daemon_descendant_pids}, owner_pid)


#: Per-harness probe for THE worker process whose OS-level cwd must equal the
#: agent's configured cwd (pi and codex are spawned with it directly; claude
#: applies it to the bundled CLI child).
WORKER_CWD_PID_PROBES: dict[str, Callable[[int], tuple[int, ...]]] = {
    "claude": claude_cli_pids,
    "pi": pi_worker_pids,
    "codex": codex_appserver_pids,
}

#: The same per-harness command matchers, reused by the pid-reuse guard in
#: worker_cwd_checked (kept identical to the probes by construction).
WORKER_CWD_NAME_MATCHERS: dict[str, Callable[[str], bool]] = {
    "claude": _is_claude_cli_command,
    "pi": _is_pi_worker_command,
    "codex": _is_codex_appserver_command,
}


def process_command(pid: int) -> str | None:
    """The live command line of ``pid``, or None when the pid is dead."""
    return _processes_impl.process_command({'subprocess': subprocess}, pid)


def worker_cwd_checked(harness: str, pid: int) -> Path:
    """process_cwd with a pid-reuse guard (run 971).

    The pid comes from the daemon moments before lsof runs; a short-lived
    worker can exit in between and the OS can hand the pid to an UNRELATED
    process — run 971's lsof then reported the CI runner's own hostexecutor
    cwd, and the probe "failed" against a process that was never the
    worker.  Verify the pid is alive and its command still matches the
    harness family before trusting lsof: a mismatch means the PROBE lost
    its subject (BLOCKED via ProbeObservationError), never that the worker
    ran in the wrong directory (FAIL).
    """
    return _processes_impl.worker_cwd_checked({'ProbeObservationError': ProbeObservationError, 'WORKER_CWD_NAME_MATCHERS': WORKER_CWD_NAME_MATCHERS, 'process_command': process_command, 'process_cwd': process_cwd}, harness, pid)


def process_cwd(pid: int) -> Path:
    """The OS-level working directory of ``pid`` via lsof (macOS has no /proc)."""
    return _processes_impl.process_cwd({'Path': Path, 'subprocess': subprocess}, pid)


def agent_record(daemon: IsolatedDaemon, name: str) -> dict[str, Any]:
    return _processes_impl.agent_record({}, daemon, name)


def target_status(daemon: IsolatedDaemon, uri: str) -> str | None:
    return _processes_impl.target_status({'target_row': target_row}, daemon, uri)


def target_row(daemon: IsolatedDaemon, uri: str) -> dict[str, Any] | None:
    return _processes_impl.target_row({}, daemon, uri)


def desired_entry(root: Path, harness: str, name: str) -> dict[str, Any]:
    return _processes_impl.desired_entry({'desired_state_document': desired_state_document}, root, harness, name)


def desired_absent(root: Path, harness: str, name: str) -> bool:
    """True when schema-v1 desired state carries no entry for this worker."""
    return _processes_impl.desired_absent({'desired_state_document': desired_state_document}, root, harness, name)


def assert_desired_exact(
    root: Path,
    *,
    harness: str,
    name: str,
    nickname: str,
    cwd: Path,
    args: tuple[str, ...],
    model: str = DEEPSEEK_MODEL,
) -> None:
    return _processes_impl.assert_desired_exact({'DEEPSEEK_PROVIDER': DEEPSEEK_PROVIDER, 'desired_entry': desired_entry}, root, harness=harness, name=name, nickname=nickname, cwd=cwd, args=args, model=model)


# -- roundtrip ---------------------------------------------------------------


def roundtrip(
    daemon: IsolatedDaemon,
    root: Path,
    *,
    to: str,
    prompt: str,
    conversation: str,
    timeout: float,
) -> str:
    """Send one probe, await the correlated reply, ack it, return its text."""
    return _processes_impl.roundtrip({'DRIVER': DRIVER, 'pending_for': pending_for}, daemon, root, to=to, prompt=prompt, conversation=conversation, timeout=timeout)


# -- lark-cli helpers (user impersonation only; never a production App) ------


_e2e_lark_profile_name: str | None = None


def e2e_lark_profile() -> str:
    """The lark-cli profile bound to the approved E2E test App.

    lark-cli has no --app-id flag: the App identity rides on named profiles,
    and without an explicit --profile every call would run as whatever
    profile happens to be the local default (which is not the E2E App).  The
    name is resolved from `lark-cli profile list` once per run and cached;
    `profile list` itself never takes --profile.
    """

    global _e2e_lark_profile_name
    if _e2e_lark_profile_name is None:
        _e2e_lark_profile_name = _lookup_e2e_lark_profile()
    return _e2e_lark_profile_name


def _lookup_e2e_lark_profile() -> str:
    return _lark_auth_impl._lookup_e2e_lark_profile({'LARK_APP_ID': LARK_APP_ID, 'LARK_CLI': LARK_CLI, 'clip': clip, 'json': json, 'one_line': one_line, 'os': os, 'redact_native_ids': redact_native_ids, 'subprocess': subprocess})


def lark_cli(*arguments: str, cwd: Path | None = None) -> dict[str, Any]:
    return _lark_auth_impl.lark_cli({'LARK_CLI': LARK_CLI, 'clip': clip, 'e2e_lark_profile': e2e_lark_profile, 'json': json, 'one_line': one_line, 'os': os, 'redact_native_ids': redact_native_ids, 'subprocess': subprocess}, *arguments, cwd=cwd)


def _lark_user_identity() -> tuple[dict[str, Any], dict[str, Any]]:
    return _lark_auth_impl._lark_user_identity({'lark_cli': lark_cli})


def _lark_user_valid(auth: dict[str, Any], user: dict[str, Any]) -> bool:
    # #141: the top-level `verified` is deliberately NOT a criterion -- it
    # reflects the default/auto identity, so a ready bot makes it True while
    # the user identity is 'missing'.  Judge the user identity itself.
    return _lark_auth_impl._lark_user_valid({}, auth, user)


def _lark_reauth_check_summary() -> str:
    """Best-effort: walk the lark-cli device flow to 'one click away' and
    push the link to the configured route (hyprial adapter user-auth check).  Never
    raises; the summary it returns goes into the BLOCKED message so a red
    gate says '凭据问题,链接已推给 X' instead of looking like a product
    failure."""
    return _lark_auth_impl._lark_reauth_check_summary({'Path': Path, '__file__': __file__, 'json': json, 'os': os, 'subprocess': subprocess})


def verify_lark_user() -> None:
    return _lark_auth_impl.verify_lark_user({'_lark_reauth_check_summary': _lark_reauth_check_summary, '_lark_user_identity': _lark_user_identity, '_lark_user_valid': _lark_user_valid, 'lark_cli': lark_cli})


def _bot_open_id(app_id: str, app_secret: str) -> str:
    """The test App bot's own open_id, the p2p DM match key (E2E-004 parity)."""
    return _lark_auth_impl._bot_open_id({'json': json}, app_id, app_secret)


def resolve_bot_dm_chat_id() -> str:
    """The caller's own p2p DM with the test App's bot, resolved by open_id.

    Same contract as E2E-004: a DM chat id is a per-developer private value
    and is never committed; a p2p entry's `p2p_target_id` is the peer's
    open_id, so the DM is the chat whose `p2p_target_id` equals the test
    App bot's open_id -- the bot's display name is arbitrary and never
    consulted -- and exactly one candidate must remain.
    """
    return _lark_auth_impl.resolve_bot_dm_chat_id({'LARK_APP_ID': LARK_APP_ID, 'LARK_APP_NAME': LARK_APP_NAME, 'LARK_SECRET_PATH': LARK_SECRET_PATH, '_bot_open_id': _bot_open_id, 'lark_cli': lark_cli, 'load_test_app_secret': load_test_app_secret})


def find_first_string(value: Any, key: str) -> str | None:
    return _lark_messages_impl.find_first_string({'find_first_string': find_first_string}, value, key)


def lark_read_with_retry(
    description: str,
    read: Callable[[], Any],
    *,
    attempts: int = 3,
) -> Any:
    """Run a Lark READ-BACK with bounded backoff, logging every attempt.

    Why retry is safe here — and only here: the retried operation is pure
    OBSERVATION (reading back what the platform already holds).  The action
    under test (the send) is NEVER retried, so a retry can never mask a
    product regression; it only keeps a single network blip from turning
    "the read-back API hiccuped" into a red run that reads exactly like
    "the message never arrived" (runs 987/928).  Every attempt is logged;
    when the budget is exhausted the failure is still loud, raised as
    ReadbackExhausted so the caller can classify it as an observation-layer
    problem rather than a delivery failure.
    """
    return _lark_messages_impl.lark_read_with_retry({'ReadbackExhausted': ReadbackExhausted, 'subprocess': subprocess, 'time': time}, description, read, attempts=attempts)


def _transcript_text(transcript: Path | None) -> str:
    return _lark_messages_impl._transcript_text({}, transcript)


def attachment_send_receipt(transcript: Path | None, source: Path) -> bool:
    """Local proof the adapter actually delivered THIS run's attachment."""
    return _lark_messages_impl.attachment_send_receipt({'_transcript_text': _transcript_text}, transcript, source)


def reply_send_receipt(transcript: Path | None, marker: str) -> bool:
    """Local proof the pinned worker produced its reply via harness_reply."""
    return _lark_messages_impl.reply_send_receipt({'_transcript_text': _transcript_text}, transcript, marker)


def lark_user_send(chat_id: str, text: str, idempotency_key: str) -> str:
    return _lark_messages_impl.lark_user_send({'find_first_string': find_first_string, 'lark_cli': lark_cli}, chat_id, text, idempotency_key)


def onit_reactions(message_id: str) -> list[dict[str, Any]]:
    return _lark_messages_impl.onit_reactions({'lark_cli': lark_cli, 'lark_read_with_retry': lark_read_with_retry}, message_id)


def bot_reply_with_marker(
    chat_id: str, marker: str, since_ms: int
) -> dict[str, Any] | None:
    """A bot-sent message in the chat carrying ``marker``, newest first."""
    return _lark_messages_impl.bot_reply_with_marker({'json': json, 'lark_cli': lark_cli, 'lark_read_with_retry': lark_read_with_retry}, chat_id, marker, since_ms)


def _lark_get_message_once(message_id: str) -> dict[str, Any]:
    return _lark_messages_impl._lark_get_message_once({'lark_cli': lark_cli}, message_id)


def lark_get_message(message_id: str) -> dict[str, Any]:
    """messages-mget with read-back retry (consistency lag counts too)."""
    return _lark_messages_impl.lark_get_message({'_lark_get_message_once': _lark_get_message_once, 'lark_read_with_retry': lark_read_with_retry}, message_id)


def _lark_message_content(message: dict[str, Any]) -> dict[str, Any]:
    return _lark_messages_impl._lark_message_content({'ET': ET, 'json': json, 're': re}, message)


def bot_resource_message(
    chat_id: str,
    kind: str,
    since_ms: int,
    *,
    file_name: str | None = None,
    route_transcript: Path | None = None,
    route_source: Path | None = None,
) -> tuple[dict[str, Any], str] | None:
    """Return this turn's user-visible resource and its platform key.

    Current lark-cli versions render ``create_time`` at minute precision, so
    the numeric timestamp guard cannot always exclude an older image.  Images
    also have no visible file name and every E2E-013 run intentionally uses
    the same tiny PNG.  When route evidence is supplied, require the candidate
    message id to occur beside this run's source path and resourceDeliveries
    in the pinned agent transcript before selecting it.
    """
    return _lark_messages_impl.bot_resource_message({'_lark_message_content': _lark_message_content, 'lark_cli': lark_cli, 'lark_get_message': lark_get_message, 'lark_read_with_retry': lark_read_with_retry, 'transcript_has_route_delivery': transcript_has_route_delivery}, chat_id, kind, since_ms, file_name=file_name, route_transcript=route_transcript, route_source=route_source)


def assert_downloaded_resource(
    root: Path,
    *,
    message: dict[str, Any],
    resource_key: str,
    kind: str,
    source: Path,
) -> str:
    return _lark_messages_impl.assert_downloaded_resource({'lark_cli': lark_cli}, root, message=message, resource_key=resource_key, kind=kind, source=source)


def transcript_has_route_delivery(
    transcript: Path, *, source: Path, native_message_id: str
) -> bool:
    """Whether the agent transcript has flushed complete route evidence."""
    return _lark_messages_impl.transcript_has_route_delivery({}, transcript, source=source, native_message_id=native_message_id)


# -- disposable channel configuration ----------------------------------------


def write_json(path: Path, value: object) -> None:
    return _configuration_impl.write_json({'json': json}, path, value)


def write_channel_config(
    home: Path,
    *,
    include_real_lark: bool,
    lark_chat_id: str | None = None,
    lark_secret: str | None = None,
) -> None:
    """Write the disposable gateways before ``hyprial init`` loads them.

    The probe gateway exists only for pin semantics and is never started;
    its credential is fabricated.  The real test-App gateway rides only when
    the Lark prerequisites hold; its credential is written fresh from the
    preflight-resolved secret (env injection in CI, the approved local file
    in developer runs) and is never printed.  Inbound DM routing is by the
    adapter binding in the agent's cell document; the sole direct route is
    resolved from this user's live p2p DM and is
    used only for the bound agent's proactive image/file sends.
    """
    return _configuration_impl.write_channel_config({'LARK_APP_ID': LARK_APP_ID, 'LARK_APP_NAME': LARK_APP_NAME, 'LARK_OUTBOUND_ROUTE': LARK_OUTBOUND_ROUTE, 'PIN_PROBE_ADAPTER': PIN_PROBE_ADAPTER, 'json': json, 'write_json': write_json}, home, include_real_lark=include_real_lark, lark_chat_id=lark_chat_id, lark_secret=lark_secret)


def write_probe_skill(root: Path) -> tuple[Path, str]:
    """Generate the disposable CFGPROBE skill and return (directory, token).

    The skill is runner-generated (never a repository fixture) with a fresh
    random nonce per run, so a reply carrying the token can only come from a
    worker that loaded THIS run's skill file — a stale session from an
    earlier run cannot echo it.  The token is the load witness: nothing else
    in the run (prompts included) ever contains it.
    """
    return _configuration_impl.write_probe_skill({'uuid4': uuid4}, root)


def load_test_app_secret() -> str | None:
    """Test-App secret: env injection (CI) first, production file second.

    A partially-set or foreign injected id raises ValueError so the caller
    reports a loud BLOCKED instead of falling back to another credential
    source; the check runs before any file access.
    """
    return _lark_auth_impl.load_test_app_secret({'LARK_APP_ID': LARK_APP_ID, 'LARK_APP_ID_ENV_VAR': LARK_APP_ID_ENV_VAR, 'LARK_APP_SECRET_ENV_VAR': LARK_APP_SECRET_ENV_VAR, 'LARK_SECRET_PATH': LARK_SECRET_PATH, 'json': json, 'os': os})


# -- phases ------------------------------------------------------------------


HARNESS_CHECKS = (
    "agent-create",
    "launch",
    "ps-running",
    "worker-cwd",
    "registration",
    "desired-state",
    "message-reply",
)
SKILL_CHECKS = ("skill.pi-loaded",)


def fifo_checks(harness: str) -> tuple[str, str, str]:
    return _configuration_impl.fifo_checks({}, harness)


MORPH_CHECKS = (
    "morph.agent-create",
    "morph.pin-setup",
    "morph.launch-claude",
    "morph.context-seed",
    "morph.record-before-switch",
    "morph.down-claude",
    "morph.switch-start-pi",
    "morph.handover-notice",
    "morph.same-entity",
    "morph.last-harness-updated",
    "morph.pin-survives-switch",
    "morph.post-switch-roundtrip",
    "morph.context-zeroed",
    "morph.down-pi",
)
MODEL_SWITCH_CHECKS = (
    "model-switch.down",
    "model-switch.restart",
    "model-switch.no-handover",
    "model-switch.desired-state",
    "model-switch.message-reply",
)
LIVENESS_CHECKS = (
    "liveness.worker-pid",
    "liveness.stopped-interval",
    "liveness.exit-log",
    "liveness.recovers-fresh-pid",
    "liveness.post-recovery-roundtrip",
)
LARK_CHECKS = (
    "lark.adapter-online",
    "lark.pin",
    "lark.inbound-onit",
    "lark.pinned-agent-received",
)
LARK_BRIDGE_CHECKS = ("lark.native-reply", "lark.reply-terminal-state")
LARK_AGENT_ATTACHMENT_CHECKS = (
    "lark.agent-image-outbound",
    "lark.agent-file-outbound",
    "lark.harness-reply-attachment-rejected",
)
TEARDOWN_CHECKS = (
    "teardown.down",
    "teardown.process-terminated",
    "teardown.desired-state-removed",
    "teardown.deregistered",
    "teardown.record-persists",
    "teardown.no-revival-after-restart",
)


def start_worker(
    daemon: IsolatedDaemon,
    report: Report,
    *,
    harness: str,
    name: str,
    nickname: str | None,
    cwd: Path,
    args: tuple[str, ...],
    check: str,
    model: str = DEEPSEEK_MODEL,
) -> dict[str, Any] | None:
    """`hyprial agent start --headless` with E2E-006's BLOCKED/FAIL separation."""
    return _worker_impl.start_worker({'DEEPSEEK_PROVIDER': DEEPSEEK_PROVIDER, 'run_cli': run_cli, 'start_failure_status': start_failure_status}, daemon, report, harness=harness, name=name, nickname=nickname, cwd=cwd, args=args, check=check, model=model)


def run_harness_case(
    daemon: IsolatedDaemon, root: Path, case: HarnessCase, report: Report
) -> None:
    return _worker_impl.run_harness_case({
        'DEEPSEEK_MODEL': DEEPSEEK_MODEL,
        'DEEPSEEK_PROVIDER': DEEPSEEK_PROVIDER,
        'HARNESS_CHECKS': HARNESS_CHECKS,
        'ProbeObservationError': ProbeObservationError,
        'REPLY_TIMEOUT': REPLY_TIMEOUT,
        'WORKER_CWD_PID_PROBES': WORKER_CWD_PID_PROBES,
        'assert_desired_exact': assert_desired_exact,
        'daemon_pid': daemon_pid,
        'json': json,
        'roundtrip': roundtrip,
        'run_cli': run_cli,
        'start_worker': start_worker,
        'target_status': target_status,
        'time': time,
        'wait_running': wait_running,
        'worker_cwd_checked': worker_cwd_checked,
    }, daemon, root, case, report)


def run_skill_probe_phase(
    daemon: IsolatedDaemon,
    root: Path,
    case: HarnessCase,
    report: Report,
    token: str,
) -> None:
    """Behavioral proof that ``--skill`` loads from the given path (pi only).

    pi is the one harness exposing an explicit skill *launch flag*
    (``--skill <path>``); claude discovers skills through settings/cwd
    conventions with no path flag on the CLI, and codex has no skill
    mechanism at all — so this assertion group applies only to pi (the
    scenario document records the reason).  The pi worker was started with
    ``--skill`` pointing at the runner-generated CFGPROBE skill; a reply
    carrying the nonce token proves the file at that path was loaded AND
    changed behavior, not merely that the flag round-tripped into desired
    state.
    """
    return _worker_impl.run_skill_probe_phase({'REPLY_TIMEOUT': REPLY_TIMEOUT, 'roundtrip': roundtrip, 'time': time}, daemon, root, case, report, token)


def run_headless_fifo_phase(
    daemon: IsolatedDaemon, root: Path, case: HarnessCase, report: Report
) -> None:
    """Headless busy FIFO (the E2E-008 cc TUI check's managed counterpart).

    While the worker is mid-turn on a deliberately slow first message (a
    shell ``touch <sentinel> && sleep`` witnesses the turn being in
    progress), a second message is delivered.  Both must be answered
    exactly once, in delivery order, with no loss — pending order is the
    daemon's ``arrival_id`` FIFO, the same order contract E2E-002 pins.
    """
    return _queue_impl.run_headless_fifo_phase({'DRIVER': DRIVER, 'REPLY_TIMEOUT': REPLY_TIMEOUT, 'fifo_checks': fifo_checks, 'pending_for': pending_for, 'time': time}, daemon, root, case, report)


def run_morph_phase(
    daemon: IsolatedDaemon, root: Path, report: Report
) -> None:
    return _migration_impl.run_morph_phase({
        'CLAUDE_MODEL': CLAUDE_MODEL,
        'DEEPSEEK_PROVIDER': DEEPSEEK_PROVIDER,
        'MORPH_CHECKS': MORPH_CHECKS,
        'MORPH_NAME': MORPH_NAME,
        'PIN_PROBE_ADAPTER': PIN_PROBE_ADAPTER,
        'PI_MODEL': PI_MODEL,
        'PI_PROVIDER': PI_PROVIDER,
        'REPLY_TIMEOUT': REPLY_TIMEOUT,
        'agent_record': agent_record,
        'roundtrip': roundtrip,
        'run_cli': run_cli,
        'start_worker': start_worker,
        'time': time,
        'wait_running': wait_running,
    }, daemon, root, report)


def run_model_switch_phase(
    daemon: IsolatedDaemon, root: Path, case: HarnessCase, report: Report
) -> None:
    return _migration_impl.run_model_switch_phase({'CLAUDE_MODEL_ALT': CLAUDE_MODEL_ALT, 'MODEL_SWITCH_CHECKS': MODEL_SWITCH_CHECKS, 'REPLY_TIMEOUT': REPLY_TIMEOUT, 'assert_desired_exact': assert_desired_exact, 'json': json, 'roundtrip': roundtrip, 'start_worker': start_worker, 'time': time, 'wait_running': wait_running}, daemon, root, case, report)


def run_liveness_phase(
    daemon: IsolatedDaemon, root: Path, case: HarnessCase, report: Report
) -> None:
    return _recovery_impl.run_liveness_phase({
        'LIVENESS_CHECKS': LIVENESS_CHECKS,
        'REPLY_TIMEOUT': REPLY_TIMEOUT,
        'connector_status': connector_status,
        'daemon_pid': daemon_pid,
        'json': json,
        'os': os,
        'pi_worker_pids': pi_worker_pids,
        'roundtrip': roundtrip,
        'signal': signal,
        'time': time,
        'wait_running': wait_running,
    }, daemon, root, case, report)


def run_lark_phase(
    daemon: IsolatedDaemon,
    root: Path,
    pinned_case: HarnessCase,
    report: Report,
    *,
    chat_id: str,
    skip_reply_bridge: bool,
) -> None:
    return _lark_impl.run_lark_phase({
        'LARK_AGENT_ATTACHMENT_CHECKS': LARK_AGENT_ATTACHMENT_CHECKS,
        'LARK_APP_NAME': LARK_APP_NAME,
        'LARK_BRIDGE_CHECKS': LARK_BRIDGE_CHECKS,
        'LARK_CHANNEL_ACTOR': LARK_CHANNEL_ACTOR,
        'LARK_CHECKS': LARK_CHECKS,
        'LARK_OUTBOUND_ROUTE': LARK_OUTBOUND_ROUTE,
        'LARK_REPLY_TIMEOUT': LARK_REPLY_TIMEOUT,
        'ONIT_TIMEOUT': ONIT_TIMEOUT,
        'ReadbackExhausted': ReadbackExhausted,
        'TINY_PNG': TINY_PNG,
        'assert_downloaded_resource': assert_downloaded_resource,
        'attachment_send_receipt': attachment_send_receipt,
        'bot_reply_with_marker': bot_reply_with_marker,
        'bot_resource_message': bot_resource_message,
        'daemon_ipc_response': daemon_ipc_response,
        'hashlib': hashlib,
        'lark_user_send': lark_user_send,
        'onit_reactions': onit_reactions,
        'pending_for': pending_for,
        'pi_sessions_root': pi_sessions_root,
        'reply_send_receipt': reply_send_receipt,
        'run_cli': run_cli,
        'shlex': shlex,
        'time': time,
        'transcript_has_route_delivery': transcript_has_route_delivery,
    }, daemon, root, pinned_case, report, chat_id=chat_id, skip_reply_bridge=skip_reply_bridge)


def run_teardown_persistence_phase(
    daemon: IsolatedDaemon,
    root: Path,
    cases: dict[str, HarnessCase],
    available: dict[str, bool],
    report: Report,
) -> None:
    """Teardown semantics: `hyprial agent stop` is manual, permanent, and must stick.

    The differential this pins (production incident 2026-08-15: four
    historical workers revived on every daemon restart because they were
    never removed from desired state): E2E-006 proves desired workers DO
    revive; nobody proved a downed worker MUST NOT.  Both halves are
    asserted here against the same restart.
    """
    return _recovery_impl.run_teardown_persistence_phase({
        'TEARDOWN_CHECKS': TEARDOWN_CHECKS,
        'agent_record': agent_record,
        'codex_appserver_pids': codex_appserver_pids,
        'connector_status': connector_status,
        'daemon_pid': daemon_pid,
        'desired_absent': desired_absent,
        'json': json,
        'target_row': target_row,
        'target_status': target_status,
        'time': time,
        'wait_running': wait_running,
    }, daemon, root, cases, available, report)


# -- main --------------------------------------------------------------------


def preflight_binaries(report: Report) -> dict[str, bool]:
    return _configuration_impl.preflight_binaries({'shutil': shutil}, report)


def preflight_lark(report: Report) -> tuple[str, str] | None:
    """Return (test-App secret, live p2p chat id), else None."""
    return _lark_auth_impl.preflight_lark({'LARK_APP_NAME': LARK_APP_NAME, 'LARK_CLI': LARK_CLI, 'LARK_SECRET_PATH': LARK_SECRET_PATH, 'load_test_app_secret': load_test_app_secret, 'os': os, 'resolve_bot_dm_chat_id': resolve_bot_dm_chat_id, 'subprocess': subprocess, 'verify_lark_user': verify_lark_user}, report)


def main() -> int:
    return _scenario_impl.main({
        'DEEPSEEK_MODEL': DEEPSEEK_MODEL,
        'DEEPSEEK_PROVIDER': DEEPSEEK_PROVIDER,
        'DRIVER': DRIVER,
        'DeepSeekUnavailable': DeepSeekUnavailable,
        'HARNESS_CHECKS': HARNESS_CHECKS,
        'HarnessCase': HarnessCase,
        'IsolatedDaemon': IsolatedDaemon,
        'LARK_AGENT_ATTACHMENT_CHECKS': LARK_AGENT_ATTACHMENT_CHECKS,
        'LARK_BRIDGE_CHECKS': LARK_BRIDGE_CHECKS,
        'LARK_CHECKS': LARK_CHECKS,
        'LIVENESS_CHECKS': LIVENESS_CHECKS,
        'MODEL_SWITCH_CHECKS': MODEL_SWITCH_CHECKS,
        'MORPH_CHECKS': MORPH_CHECKS,
        'PI_PROVIDER': PI_PROVIDER,
        'Path': Path,
        'Report': Report,
        'SKILL_CHECKS': SKILL_CHECKS,
        'TEARDOWN_CHECKS': TEARDOWN_CHECKS,
        '__doc__': __doc__,
        '__file__': __file__,
        'argparse': argparse,
        'deepseek_environment': deepseek_environment,
        'fifo_checks': fifo_checks,
        'hyprial_executable': hyprial_executable,
        'preflight_binaries': preflight_binaries,
        'preflight_lark': preflight_lark,
        'run_harness_case': run_harness_case,
        'run_headless_fifo_phase': run_headless_fifo_phase,
        'run_lark_phase': run_lark_phase,
        'run_liveness_phase': run_liveness_phase,
        'run_model_switch_phase': run_model_switch_phase,
        'run_morph_phase': run_morph_phase,
        'run_skill_probe_phase': run_skill_probe_phase,
        'run_teardown_persistence_phase': run_teardown_persistence_phase,
        'short_temp_root': short_temp_root,
        'teardown_guard': teardown_guard,
        'tempfile': tempfile,
        'write_channel_config': write_channel_config,
        'write_probe_skill': write_probe_skill,
    })


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, KeyError, ValueError) as error:
        print(f"RESULT FAIL {SCENARIO} runner {error}")
        raise SystemExit(1) from error
