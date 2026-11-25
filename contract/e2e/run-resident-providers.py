#!/usr/bin/env python3
"""E2E-006: exercise real resident providers through an isolated Python daemon."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _diagnostic_text import clip, one_line
from _resident_evidence import resident_root
from _daemon_runner import desired_state_document, IsolatedDaemon, ROOT, hyprial_executable, teardown_guard
from _runner_shared import org as _org
from _deepseek import (
    MODEL as DEEPSEEK_MODEL,
    PROVIDER as DEEPSEEK_PROVIDER,
    DeepSeekUnavailable,
    deepseek_environment,
    selection_args,
)

# The runner must assert against the capability table of the code under
# test, not a hard-coded expectation: a provider whose headless execution is
# honestly declared degraded (e.g. startup-task-only, daemon deliveries are
# not received) cannot roundtrip by design, and the scenario
# locks that honest baseline instead of failing on it.  capabilities.py is
# self-contained (stdlib-only); load it by path so the runner keeps working
# under a bare system interpreter without the project installed.
def _load_capability_module() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "hyprial_capabilities_under_test", ROOT / "src" / "hyprial" / "daemon" / "impl" / "harnesses" / "capabilities.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load the capability table of the code under test")
    module = importlib.util.module_from_spec(spec)
    # dataclass processing resolves the module via sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_capability_support(provider: str) -> Any:
    module = _load_capability_module()
    return module.declare(provider, headless=True).get(module.Capability.HEADLESS_EXEC)


def _probe_marker(provider: str) -> str:
    return f"RESIDENT_{provider.upper()}_OK"


def _tool_injection_native(provider: str) -> bool:
    module = _load_capability_module()
    injection = module.declare(provider, headless=True).get(
        module.Capability.TOOL_INJECTION
    )
    return injection is not None and injection.level == "native"


#: Known reply-ID family. A red run names the matching minting template rather
#: than describing every non-native value as an unknown path.
_REPLY_ID_TEMPLATES = (
    "hyprial:reply:{message_id}",
    "hyprial:sender-rejected:{message_id}",
)


def _matching_reply_template(message_id: str, observed: object) -> str | None:
    """Name the known template whose uuid5 equals ``observed``, if any."""

    if not isinstance(observed, str):
        return None
    for template in _REPLY_ID_TEMPLATES:
        if str(uuid5(NAMESPACE_URL, template.format(message_id=message_id))) == observed:
            return template
    return None


def _probe_prompt(provider: str) -> str:
    """The roundtrip probe, branched on the TOOL_INJECTION capability.

    One prompt for every harness used to say "reply using the Harness reply
    path" -- but a harness WITHOUT tool injection (codex today) holds no such
    tool, so the instruction sent the model hunting for a nonexistent reply
    tool (observed: 13 tool calls, 117s, no reply) instead of finishing its
    turn.  Branch on the capability table (never a hard-coded provider name):

    * TOOL_INJECTION native -> name the tool and demand it as the FIRST
      action.  An explicit tool instruction the model then cannot execute is
      a REAL reply-path signal and must stay red.
    * otherwise -> demand direct output only; the daemon synthesizes the
      reply from the turn result, so "just finish the turn" IS the reply
      path under test.
    """

    marker = _probe_marker(provider)
    if _tool_injection_native(provider):
        # Name the exact argument signature: coding models otherwise guess
        # `reply=`/`text=` for harness_reply, gets a validation error back as
        # a tool RESULT (never raised), and ends its turn without retrying —
        # the reply then comes from turn-result synthesis and the tool-path
        # assertion below goes red for a model dice-roll, not a product break
        # (CI run #1395 vs #1393, same SHA family, same prompt, both ways).
        return (
            "Harness resident-provider E2E probe. As your FIRST action, reply "
            "to this message through the harness_reply tool: call it with "
            "arguments messageId=<this message's id> and "
            f'message="{marker}" (exactly that text). The tool is already '
            "available to you -- call harness_read first if you need this "
            "message's id. If the call returns a validation error, fix the "
            "argument names and call it again; do not end your turn before "
            "harness_reply has succeeded. Do not search for anything else."
        )
    return (
        "Harness resident-provider E2E probe. Output exactly "
        f"{marker} and nothing else. Do not call any tools and do not look "
        "for a reply tool: your turn's final text IS the reply."
    )


@dataclass(frozen=True, slots=True)
class ProviderCase:
    provider: str
    name: str
    nickname: str
    cwd: Path
    args: tuple[str, ...]


def provider_cases(skill: Path, *, pi_cwd: Path | None = None) -> tuple[ProviderCase, ...]:
    return (
        ProviderCase(
            "pi",
            "resident-pi-worker",
            "resident-pi",
            pi_cwd or (skill.parents[2] if skill.is_dir() else ROOT),
            ("--approve",),
        ),
        ProviderCase(
            "codex",
            "resident-codex-worker",
            "resident-codex",
            ROOT,
            # `-a never` (4aab5b0, replacing hyprial's --approve-for-me auto
            # reviewer that DeepSeek could not serve) also refuses every MCP
            # tool call inside codex's exec runtime -- "MCP tool call requires
            # approval, but approval policy is never" -- so the model could
            # never take the native harness_reply path (card 87dc8276).
            # Measured 2026-09-05, codex-cli 0.152.0, three arms: never+auto
            # key refused; on-request+auto key replied natively; on-request
            # without the key replied natively.  The per-server
            # default_tools_approval_mode key is inert; the policy decides.
            ("-a", "on-request", "-s", "workspace-write"),
        ),
        ProviderCase(
            "claude",
            "resident-claude-worker",
            "resident-claude",
            ROOT,
            (),
        ),
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
        print(f"RESULT {status} E2E-006 {check} {clean}", flush=True)

    def block_dependents(self, case: ProviderCase, reason: str) -> None:
        for check in (
            "ps-running",
            "desired-state",
            "message-reply",
            "restart-restore",
        ):
            self.add("BLOCKED", f"{case.provider}.{check}", reason)
        if case.provider == "claude":
            self.add("BLOCKED", "claude.fresh-session-id", reason)

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
            f"{overall} E2E-006 resident-providers "
            f"pass={counts['PASS']} fail={counts['FAIL']} "
            f"blocked={counts['BLOCKED']}",
            flush=True,
        )
        return 1 if counts["FAIL"] else (2 if counts["BLOCKED"] else 0)


def run_cli(
    daemon: IsolatedDaemon,
    *args: str,
    timeout: float = 90.0,
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
        "agent-owned deepseek_api_key",
        "operation not permitted",
        "permission denied",
        "network is unreachable",
        "connection refused",
        "timed out",
    )
    return "BLOCKED" if any(item in lowered for item in blocked_markers) else "FAIL"


def desired_provider(root: Path, case: ProviderCase) -> dict[str, Any]:
    path = root / "state" / "desired-state.json"  # named for messages only; the store picks the storage
    document = desired_state_document(root)
    providers = document.get("providers", [])
    matches = [
        item
        for item in providers
        if isinstance(item, dict)
        and item.get("provider") == case.provider
        and item.get("name") == case.name
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one desired provider, found {len(matches)} at {path}"
        )
    return matches[0]


def connector_status(value: dict[str, Any], case: ProviderCase) -> dict[str, Any] | None:
    connectors = value.get("connectors", [])
    if not isinstance(connectors, list):
        return None
    return next(
        (
            item
            for item in connectors
            if isinstance(item, dict)
            and item.get("id") == f"{case.provider}:{case.name}"
        ),
        None,
    )


def wait_running(daemon: IsolatedDaemon, case: ProviderCase) -> dict[str, Any]:
    return daemon.wait(
        lambda: daemon.ps(),
        lambda value: bool(
            (status := connector_status(value, case))
            and status.get("running") is True
        ),
        # 120 s: greater than the composed codex app-server startup budget
        # (APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT = 90 s = initialize 15 +
        # thread/start 60 + margin 15), so a slow-but-healthy codex restore
        # settles here instead of this wait failing before the launcher's own
        # budget does. A restart restores every desired connector in sequence,
        # and a codex session start alone measures 9-43 s (card 54158b16); 15 s
        # was the budget of a single fast launch, not of a restore phase.
        timeout=120.0,
    )


def native_probe_roundtrip(
    daemon: IsolatedDaemon,
    case: ProviderCase,
    conversation: str,
    reply_timeout: float,
) -> None:
    """One full native-headless probe roundtrip with discriminating assertions.

    Sends the capability-branched probe prompt, waits for the correlated
    reply, and proves HOW it arrived. Every harness whose capability table
    declares native tool injection must obey the explicit harness_reply
    instruction. A deterministic turn-result synthesis is not equivalent to
    executing that native tool. In every case the reply text must carry the
    demanded marker and exactly one reply may exist.
    """

    sent = daemon.command(
        "message",
        "send",
        "--from",
        DRIVER,
        "--to",
        case.nickname,
        "--conversation",
        conversation,
        "--summary",
        _probe_prompt(case.provider),
        _probe_prompt(case.provider),
        "--json",
    )
    if sent.get("ok") is not True or not isinstance(sent.get("messageId"), str):
        raise AssertionError(f"send was not accepted: {sent}")
    reply_state = wait_for_reply(
        daemon, case, conversation, sent["messageId"], reply_timeout
    )
    reply = next(
        item
        for item in reply_state["pendingMessages"]
        if item.get("conversationId") == conversation
        and item.get("messageId") != sent["messageId"]
    )
    # The text-carrying surface for content assertions (ps carries
    # only ids; this is the same non-destructive IPC E2E-013 uses).
    textual = [
        item
        for item in pending_message_texts(daemon, DRIVER)
        if item.get("conversationId") == conversation
        and item.get("messageId") != sent["messageId"]
    ]
    if _tool_injection_native(case.provider):
        # The prompt demanded the harness_reply tool. A carrier never replies
        # by itself (C35): the only reply path left is an explicit
        # message.reply produced by the worker's harness_reply call, whose id
        # is uuid5(URL, "hyprial:reply:<orig>"). Any other value means no
        # explicit reply happened.
        expected_reply_id = str(
            uuid5(NAMESPACE_URL, f"hyprial:reply:{sent['messageId']}")
        )
        if reply.get("messageId") != expected_reply_id:
            # Keep the native path as the pass condition, but name any known
            # fallback template in the failure.
            observed = reply.get("messageId")
            matched = _matching_reply_template(sent["messageId"], observed)
            raise AssertionError(
                "native harness_reply was required but the reply used another "
                f"path: sent={sent['messageId']} got={observed} "
                f"expected(native)={expected_reply_id} "
                f"matched-template={matched or 'UNNAMED (not a known template)'}"
            )
        replies = [
            item
            for item in reply_state["pendingMessages"]
            if item.get("conversationId") == conversation
            and item.get("messageId") != sent["messageId"]
        ]
        if len(replies) != 1:
            raise AssertionError(
                "expected exactly one reply for this probe, found "
                f"{len(replies)}"
            )
    marker = _probe_marker(case.provider)
    if len(textual) != 1 or marker not in str(textual[0].get("summary") or ""):
        raise AssertionError(
            f"correlated reply lacks the demanded marker {marker}: "
            f"{[str(item.get('summary'))[:80] for item in textual]}"
        )
    ack = daemon.command(
        "message", "ack", "--from", DRIVER, reply["messageId"], "--json"
    )
    if ack.get("acknowledged") is not True:
        raise AssertionError(f"reply acknowledgement failed: {ack}")


def claude_transcript_dir(case: ProviderCase) -> Path:
    """The transcript dir Claude Code uses for this worker's cwd.

    Claude Code writes one ``<session-uuid>.jsonl`` per session under
    ``$CLAUDE_CONFIG_DIR/projects/<cwd-slug>`` (slug: resolved cwd with
    every non-alphanumeric except ``-`` replaced by ``-``; verified against
    live SDK-spawned workers). The headless SDK-bundled CLI runs in
    stream-json mode and carries no ``--session-id`` in argv, so the
    transcript filename is the only externally observable per-spawn Claude
    session identity. Filenames/metadata only — transcript contents are
    never read.
    """

    raw = os.environ.get("CLAUDE_CONFIG_DIR", "")
    config_dir = Path(raw).expanduser() if raw else Path.home() / ".claude"
    slug = re.sub(r"[^A-Za-z0-9-]", "-", str(case.cwd.resolve()))
    return config_dir / "projects" / slug


def claude_transcript_sessions(case: ProviderCase, *, since: float) -> frozenset[str]:
    """Session ids (transcript stems) created at or after ``since`` (mtime)."""

    directory = claude_transcript_dir(case)
    if not directory.is_dir():
        return frozenset()
    return frozenset(
        entry.stem
        for entry in directory.glob("*.jsonl")
        if entry.stat().st_mtime >= since
    )


def fresh_transcript_sessions(
    before: frozenset[str], after: frozenset[str]
) -> frozenset[str]:
    """Session ids present after the daemon restart but not before it.

    Factored out pure so the must-stay-red property is directly testable: a
    restart that RESUMED the old session appends to the old transcript and
    yields an empty difference — the assertion must then FAIL.
    """

    return after - before


def resumed_session_ref(daemon_root: Path, provider: str, name: str) -> str | None:
    """The daemon's persisted sessionRef for one worker, if it carries one.

    Reads the daemon's own desired-state (``HARNESS_STATE_DIR``/desired-state
    schema v1) — the source #190 persists and resumes from.  A non-None ref
    means the daemon INTENDS to resume this worker's session on restart;
    None means a fresh spawn is expected.  Reading daemon state rather than
    guessing keeps the freshness assertion branching on the launch spec, as
    the scenario's own maintainer note demands.

    Returns None when the worker is absent or carries no sessionRef — both
    cases mean "fresh expected".
    """

    # The document comes through the store: JSON on dev, SQLite on intg
    # (U0a-2).  An absent store loads as an empty document -> None ("fresh
    # expected"), exactly as "neither candidate file exists" used to; an
    # unreadable one must NOT degrade to the fresh branch (that is a FAIL
    # with the WRONG reason -- #476 burned rounds on exactly this), so the
    # store's own error is surfaced as the assertion.
    try:
        document = desired_state_document(daemon_root)
    except Exception as error:  # noqa: BLE001 - surfaced, not swallowed
        raise AssertionError(
            f"desired-state under {daemon_root} exists but is unreadable: {error}"
        ) from error
    for spec in document.get("providers", []):
        if not isinstance(spec, dict):
            continue
        # Dual-read matches DesiredStateStore.from_json: the on-disk key
        # stays "provider" (schemaVersion=1 downgrade compat), with
        # "harness" accepted and winning when both present.
        raw_harness = spec.get("harness") or spec.get("provider")
        harness = "claude" if raw_harness == "cc" else raw_harness
        if harness == provider and spec.get("name") == name:
            ref = spec.get("sessionRef")
            return ref if isinstance(ref, str) and ref else None
    return None


# CLI probe-sender persona (registered at daemon start; Allen b).
DRIVER = "e2e006-driver"


def pending_message_texts(daemon: IsolatedDaemon, actor: str) -> list[dict[str, Any]]:
    """Non-destructive pending read WITH message text (message.pending.list).

    ``ps --json`` pendingMessages carry only ids/conversation/from -- no
    text -- so the marker assertion must read the IPC surface, never ``ps``.
    """

    frame = {
        "version": 1,
        "id": str(uuid4()),
        "method": "message.pending.list",
        "params": {"actor": actor},
    }
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(15)
    try:
        client.connect(str(daemon.root / "state" / "daemon.sock"))
        client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        buffer = bytearray()
        while b"\n" not in buffer:
            chunk = client.recv(64 * 1024)
            if not chunk:
                raise AssertionError("daemon disconnected during IPC read")
            buffer.extend(chunk)
    finally:
        client.close()
    response = json.loads(buffer.partition(b"\n")[0])
    if isinstance(response.get("error"), dict):
        raise AssertionError(
            f"message.pending.list failed: {response['error'].get('message')}"
        )
    result = response.get("result")
    messages = result.get("messages") if isinstance(result, dict) else None
    return [item for item in messages if isinstance(item, dict)] if isinstance(
        messages, list
    ) else []


def wait_for_reply(
    daemon: IsolatedDaemon,
    case: ProviderCase,
    conversation_id: str,
    request_id: str,
    timeout: float,
) -> dict[str, Any]:
    def probe() -> dict[str, Any]:
        return daemon.ps()

    def accepted(value: dict[str, Any]) -> bool:
        pending = value.get("pendingMessages", [])
        return isinstance(pending, list) and any(
            isinstance(item, dict)
            and item.get("messageId") != request_id
            and item.get("conversationId") == conversation_id
            for item in pending
        )

    return daemon.wait(probe, accepted, timeout=timeout)


def validate_provider(
    daemon: IsolatedDaemon,
    root: Path,
    case: ProviderCase,
    report: Report,
    reply_timeout: float,
) -> None:
    if shutil.which(case.provider) is None:
        reason = f"{case.provider} executable is not on PATH"
        report.add("BLOCKED", f"{case.provider}.launch", reason)
        report.block_dependents(case, reason)
        return

    launched_at = time.time()
    code, value, diagnostic = run_cli(
        daemon,
        "start",
        case.provider,
        "--name",
        case.name,
        "--nickname",
        case.nickname,
        "--cwd",
        str(case.cwd),
        "--headless",
        *selection_args(),
        "--json",
        "--",
        *case.args,
    )
    if code != 0 or value is None or value.get("ok") is not True:
        detail = (
            str(value.get("error"))
            if isinstance(value, dict) and value.get("error")
            else diagnostic
        )
        status = start_failure_status(detail)
        reason = f"start command failed ({code}): {detail}"
        report.add(status, f"{case.provider}.launch", reason)
        if "unexpected extra argument" in detail.lower():
            baseline_name = f"{case.name}-without-args"
            baseline_code, baseline, baseline_diagnostic = run_cli(
                daemon,
                "start",
                case.provider,
                "--name",
                baseline_name,
                "--cwd",
                str(case.cwd),
                "--headless",
                *selection_args(),
                "--json",
            )
            if (
                baseline_code != 0
                or baseline is None
                or baseline.get("ok") is not True
            ):
                baseline_detail = (
                    str(baseline.get("error"))
                    if isinstance(baseline, dict) and baseline.get("error")
                    else baseline_diagnostic
                )
                report.add(
                    start_failure_status(baseline_detail),
                    f"{case.provider}.launch-without-args",
                    f"diagnostic launch failed ({baseline_code}): {baseline_detail}",
                )
            else:
                baseline_case = ProviderCase(
                    case.provider,
                    baseline_name,
                    baseline_name,
                    case.cwd,
                    (),
                )
                try:
                    time.sleep(1.0)
                    wait_running(daemon, baseline_case)
                except AssertionError as error:
                    report.add(
                        "FAIL", f"{case.provider}.launch-without-args", str(error)
                    )
                else:
                    report.add(
                        "PASS",
                        f"{case.provider}.launch-without-args",
                        "daemon launched a running real provider without passthrough args",
                    )
                finally:
                    try:
                        daemon.command(
                            "agent",
                            "stop",
                            baseline_name,
                            "--json",
                        )
                    except (AssertionError, OSError):
                        pass
        report.block_dependents(case, "provider launch did not complete")
        return
    report.add("PASS", f"{case.provider}.launch", f"id={value.get('id')}")

    try:
        running = wait_running(daemon, case)
    except AssertionError as error:
        report.add("FAIL", f"{case.provider}.ps-running", str(error))
    else:
        status = connector_status(running, case)
        report.add(
            "PASS",
            f"{case.provider}.ps-running",
            f"id={status.get('id') if status else 'missing'} running=true",
        )

    try:
        persisted = desired_provider(root, case)
        expected = {
            "provider": case.provider,
            "name": case.name,
            "nickname": case.nickname,
            "cwd": str(case.cwd.resolve()),
            "headless": True,
            "ownership": "managed",
            "args": list(case.args),
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
    except (AssertionError, OSError, json.JSONDecodeError) as error:
        report.add("FAIL", f"{case.provider}.desired-state", str(error))
    else:
        report.add(
            "PASS",
            f"{case.provider}.desired-state",
            f"args={json.dumps(list(case.args), ensure_ascii=False)}",
        )

    conversation = f"resident-{case.provider}-{time.time_ns()}"
    prompt = _probe_prompt(case.provider)
    headless_exec = _load_capability_support(case.provider)
    if headless_exec is not None and headless_exec.level != "native":
        # Honestly-declared degraded headless path (codex today: a plain PTY
        # that runs the startup task only; daemon deliveries are not
        # received).  The contract here is the honest baseline: the send is
        # accepted and durably queued for the worker rather than silently
        # dropped, and no phantom reply ever appears.  When the capability
        # table later promotes this provider to native, this branch stops
        # applying and the full roundtrip below is asserted instead.
        try:
            sent = daemon.command(
                "message",
                "send",
                "--from",
                DRIVER,
                "--to",
                case.nickname,
                "--conversation",
                conversation,
                "--summary",
                prompt,
                prompt,
                "--json",
            )
            if sent.get("ok") is not True or not isinstance(sent.get("messageId"), str):
                raise AssertionError(f"send was not accepted: {sent}")
            deliveries = sent.get("deliveries", [])
            accepted_or_queued = any(
                isinstance(item, dict)
                and (item.get("accepted") is True or item.get("queued") is True)
                for item in deliveries
            ) if isinstance(deliveries, list) else False
            if not accepted_or_queued:
                raise AssertionError(
                    f"send was neither accepted nor durably queued: {sent}"
                )
            try:
                reply_state = wait_for_reply(
                    daemon, case, conversation, sent["messageId"], 15.0
                )
            except AssertionError:
                reply_state = None
            if reply_state is not None:
                raise AssertionError(
                    "declared-degraded provider produced a reply; promote the "
                    "capability table to native and assert the full roundtrip"
                )
        except (AssertionError, KeyError, StopIteration) as error:
            report.add("FAIL", f"{case.provider}.message-reply", str(error))
        else:
            report.add(
                "PASS",
                f"{case.provider}.message-reply",
                f"declared {headless_exec.level} ({headless_exec.mechanism}): "
                f"{headless_exec.note}; delivery durably queued, no phantom reply",
            )
    else:
        try:
            native_probe_roundtrip(daemon, case, conversation, reply_timeout)
        except (AssertionError, KeyError, StopIteration) as error:
            report.add("FAIL", f"{case.provider}.message-reply", str(error))
        else:
            report.add(
                "PASS",
                f"{case.provider}.message-reply",
                f"conversation={conversation} correlated=true acknowledged=true",
            )

    before_sessions: frozenset[str] = frozenset()
    if case.provider == "claude":
        # Baseline: the roundtrip above forced the worker's first turn,
        # which materializes its session transcript. Without a baseline the
        # post-restart freshness comparison is meaningless, so an absent
        # baseline is BLOCKED (discovery broken), never a pass.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            before_sessions = claude_transcript_sessions(case, since=launched_at)
            if before_sessions:
                break
            time.sleep(0.5)
        if not before_sessions:
            report.add(
                "BLOCKED",
                "claude.fresh-session-id",
                "pre-restart: no Claude session transcript appeared under "
                f"{claude_transcript_dir(case)} after the roundtrip",
            )

    try:
        daemon.stop()
        daemon.start()
        wait_running(daemon, case)
    except (AssertionError, OSError, KeyError) as error:
        # The wait message carries a length-bounded ps; on a restore failure the
        # connector's own error is the diagnostic that matters, so print it in
        # full (the ps truncation cannot choose to drop it).
        detail = str(error)
        try:
            ps = daemon.ps()
            connectors = ps.get("connectors", []) if isinstance(ps, dict) else []
            connector_errors = [
                {"id": c.get("id"), "error": c.get("error")}
                for c in connectors
                if isinstance(c, dict) and c.get("error")
            ]
            if connector_errors:
                detail = f"{detail}\n  connector errors (full): {connector_errors}"
        except (OSError, KeyError):
            pass
        report.add("FAIL", f"{case.provider}.restart-restore", detail)
        return
    report.add(
        "PASS", f"{case.provider}.restart-restore", "desired provider running=true"
    )

    if case.provider == "claude" and before_sessions:
        resumed = resumed_session_ref(daemon.root, case.provider, case.name)
        try:
            if resumed is None or resumed not in before_sessions:
                raise AssertionError(
                    f"Claude restart did not retain an established native ref: {resumed!r}; "
                    f"pre-restart sessions={sorted(before_sessions)}"
                )
            native_probe_roundtrip(
                daemon,
                case,
                f"resident-claude-fresh-{time.time_ns()}",
                reply_timeout,
            )
            after_sessions = daemon.wait(
                lambda: claude_transcript_sessions(case, since=launched_at),
                lambda ids: resumed in ids,
                timeout=15.0,
            )
            fresh = fresh_transcript_sessions(before_sessions, after_sessions)
            if fresh:
                raise AssertionError(
                    "daemon restart spawned a NEW Claude session despite "
                    f"persisted sessionRef {resumed!r}: resume contract was not honoured"
                )
        except (AssertionError, OSError, KeyError, StopIteration) as error:
            report.add("FAIL", "claude.fresh-session-id", str(error))
        else:
            report.add(
                "PASS",
                "claude.fresh-session-id",
                f"post-restart roundtrip landed in the RESUMED pre-restart session ({resumed})",
            )


def main() -> int:
    report = Report()
    executable = hyprial_executable()
    skill_override = os.environ.get("HYPRIAL_RESIDENT_SKILL")
    skill = (
        Path(skill_override).expanduser().resolve()
        if skill_override
        else (ROOT.parents[2] / "kanban" / ".claude" / "skills" / "kanban")
    )
    reply_timeout = float(os.environ.get("HYPRIAL_RESIDENT_REPLY_TIMEOUT", "120"))
    if not skill.is_dir():
        report.add(
            "BLOCKED",
            "preflight.pi-skill",
            f"real skill directory is unavailable: {skill}",
        )
    else:
        report.add("PASS", "preflight.pi-skill", f"directory={skill}")

    cases = provider_cases(skill)

    try:
        provider_environment = deepseek_environment()
    except DeepSeekUnavailable as error:
        reason = str(error)
        report.add("BLOCKED", "preflight.deepseek", reason)
        for case in cases:
            report.add("BLOCKED", f"{case.provider}.launch", reason)
            report.block_dependents(case, reason)
        return report.exit_code()
    report.add(
        "PASS",
        "preflight.deepseek",
        f"provider={DEEPSEEK_PROVIDER} model={DEEPSEEK_MODEL} credential=ready",
    )

    with resident_root(lambda: any(item.status == "FAIL" for item in report.results)) as root:
        if skill.is_dir():
            pi_project = stage_pi_project(root, skill)
            cases = provider_cases(skill, pi_cwd=pi_project)
        daemon = IsolatedDaemon(executable, root, "resident-probe-node")
        daemon.env.update(provider_environment)
        try:
            daemon.start()
        except (AssertionError, OSError, KeyError) as error:
            report.add("FAIL", "preflight.isolated-daemon", str(error))
            for case in cases:
                report.add(
                    "BLOCKED",
                    f"{case.provider}.launch",
                    "isolated daemon did not start",
                )
                report.block_dependents(case, "isolated daemon did not start")
            return report.exit_code()
        report.add(
            "PASS",
            "preflight.isolated-daemon",
            f"HYPRIAL_HOME={root / 'home'} HARNESS_STATE_DIR={root / 'state'}",
        )
        # resolve-or-reject (Allen b): the CLI probe sender must be a
        # registered identity — no more bare node_id as sender.
        daemon.command("agent", "create", "--name", DRIVER, "--json")
        # Hard cut (send links, 2026-10-15): every probe send below is
        # agent-to-agent (DRIVER -> the harness worker), so this node needs a
        # writable org space or the first send holds with org-unavailable.
        _org.ensure_local_org(daemon, DRIVER, prefix="resident")

        def _down_all() -> None:
            try:
                daemon.command("agent", "stop", "--all", "--json", timeout=90.0)
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
            for case in cases:
                if case.provider == "pi" and not skill.is_dir():
                    report.add(
                        "BLOCKED", "pi.launch", "real skill directory is unavailable"
                    )
                    report.block_dependents(case, "real skill directory is unavailable")
                    continue
                validate_provider(daemon, root, case, report, reply_timeout)
    return report.exit_code()


def stage_pi_project(root: Path, skill: Path) -> Path:
    """Give P2 a bounded Git project with an explicitly approved local skill."""
    if any(path.is_symlink() for path in skill.rglob("*")):
        raise ValueError("resident Pi skill must not contain symbolic links")
    project = root / "pi-project"
    destination = project / ".pi" / "skills" / skill.name
    shutil.copytree(skill, destination)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    return project


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, KeyError, ValueError) as error:
        print(f"RESULT FAIL E2E-006 runner {error}")
        raise SystemExit(1) from error
