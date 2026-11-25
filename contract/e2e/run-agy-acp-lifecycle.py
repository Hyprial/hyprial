#!/usr/bin/env python3
# Decision record (file header; no inline comments by repository rule):
# - MANUAL-ONLY real acceptance (coordinator decision 2026-11-22): there is no
#   sustainable managed macOS CI provisioning for an Antigravity OAuth token,
#   so this runner is deliberately NOT registered in contract/e2e/scenarios
#   .json or the release inventory and never runs in the isolated gate or the
#   credentialed nightly; it is executed manually against the final pinned
#   SHA of a change that touches the agy wiring. Its RESULT identity is the
#   literal AGY-ACP-LIFECYCLE, never a release E2E-<number> id.
# - Without AGY_E2E_OAUTH_TOKEN the runner reports BLOCKED for every leg and
#   exits without reading any credential or starting any daemon.
# - The token named by AGY_E2E_OAUTH_TOKEN is copied exactly once into this
#   run's one-shot temp root and staged there for `agent provider login agy
#   --source`; its bytes are never emitted into logs, reports, argv, or commits.
#   The temp root is always deleted in finally, the production daemon socket is never
#   touched, and the user's global ~/.gemini is asserted unchanged before any
#   PASS is allowed to stand.
# - HYPRIAL_BIN and editable source provenance: binary must match this
#   checkout's .venv/bin/hyprial, src_dir must match ROOT, tree_head must
#   match git HEAD, and assert_tree_matches_declared_sha must agree when set.
# - GEMINI_API_KEY coverage is grant-driven only (`agent secret provider-write`
#   + `agent secret grant --environment-name GEMINI_API_KEY`), never ambient
#   daemon env: the daemon env strips GEMINI_API_KEY so the OAuth legs cannot
#   silently switch to the API-key auth path. Without a key the google leg is
#   BLOCKED, never PASS.
# - Evidence reuse: IsolatedDaemon, _runner_shared org bootstrapping, and the
#   uuid5("hyprial:reply:<id>") native-reply discriminator come from
#   run-resident-providers.py; ACP resume and permission evidence combine a
#   transparent stdio wire-tap recording session/request_permission pairs with
#   the daemon's own worker logs and session transcript under the disposable root.
"""Manual real acceptance: Antigravity (agy) ACP lifecycle on an isolated daemon.

Not a registered E2E release scenario (see the header decision record); run
by hand on the final pinned SHA. Flow: agent create -> provider login agy
--source <one-shot token copy> -> agent start agy --headless, then: a real
harness_read/harness_reply roundtrip (never a turn-result synthesis),
systemPrompt.append shaping, the session/request_permission minimal-allow
policy in default mode, a second concurrent worker with independent
home/credential projection, daemon-restart resume onto the same ACP sessionId
with MCP tools restored, provider logout removing the session_root token copy
and refusing credential-less reuse, and the optional GEMINI_API_KEY grant
path.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agy_acp_helpers import (
    ALL_PHASE_CHECKS,
    CONCURRENCY_CHECKS,
    CORE_CHECKS,
    DRIVER,
    GOOGLE_CHECKS,
    GOOGLE_MODEL,
    LOGOUT_CHECKS,
    REPLY_TIMEOUT_DEFAULT,
    RESTART_CHECKS,
    RESTORE_REFUSAL_WINDOW,
    SCENARIO,
    TOKEN_FILENAME,
    Report,
    WorkerCase,
    ack_message,
    connector_status,
    desired_agy_row,
    install_acp_permission_wiretap,
    native_reply_id,
    native_roundtrip,
    native_token_path,
    permission_policy_probe,
    persisted_session_ref,
    ready_session_ids,
    reply_text,
    run_cli,
    send_probe,
    session_token_copy,
    snapshot_tree,
    stage_oauth_token,
    start_worker,
    token_copy_fingerprint,
    wait_for_reply,
    wait_running,
)
from _daemon_runner import IsolatedDaemon, ROOT, teardown_guard
from _hyprial_resolution import (
    assert_tree_matches_declared_sha,
    record_resolution,
    resolve_hyprial,
)
from _runner_shared import org as _org


def validate_core(
    daemon: IsolatedDaemon,
    root: Path,
    case: WorkerCase,
    nonce: str,
    report: Report,
    reply_timeout: float,
) -> str | None:
    code, value, diagnostic = run_cli(daemon, "agent", "create", "--name", case.name, "--json")
    agent_info = value.get("agent") if isinstance(value, dict) else None
    actor_uri = agent_info.get("uri") if isinstance(agent_info, dict) else agent_info
    if code != 0 or value is None or value.get("ok") is not True or not isinstance(actor_uri, str) or not actor_uri:
        report.add("FAIL", "agent-create", f"agent create failed ({code}): {diagnostic}")
        report.block(CORE_CHECKS[1:], "agent was not created")
        return None
    report.add("PASS", "agent-create", f"actor={actor_uri} name={case.name}")

    staged = root / "staged" / TOKEN_FILENAME
    code, value, diagnostic = run_cli(
        daemon,
        "agent", "provider", "login", "agy",
        "--actor", case.name, "--source", str(staged), "--json",
    )
    if code != 0 or value is None or value.get("ok") is not True:
        report.add("FAIL", "provider-login", f"provider login failed ({code}): {diagnostic}")
        report.block(CORE_CHECKS[2:], "agent holds no seeded agy credential")
        return None
    report.add("PASS", "provider-login", "seeded from one-shot staged copy; bytes never read")

    code, value, diagnostic = start_worker(daemon, case, nonce)
    if code != 0 or value is None or value.get("ok") is not True:
        report.add("FAIL", "launch", f"agent start failed ({code}): {diagnostic}")
        report.block(CORE_CHECKS[3:], "agy worker did not launch")
        return None
    report.add("PASS", "launch", f"connector={case.connector_id} headless=true")

    try:
        wait_running(daemon, case)
    except AssertionError as error:
        report.add("FAIL", "ps-running", str(error))
        report.block(CORE_CHECKS[4:], "agy worker never reached running")
        return None
    report.add("PASS", "ps-running", f"id={case.connector_id} running=true")

    try:
        row = desired_agy_row(root, case.name)
        expected = {
            "provider": "agy",
            "name": case.name,
            "nickname": case.nickname,
            "cwd": str(case.cwd.resolve()),
            "headless": True,
            "ownership": "managed",
        }
        mismatches = {
            key: {"expected": want, "actual": row.get(key)}
            for key, want in expected.items()
            if row.get(key) != want
        }
        args = row.get("args")
        if not isinstance(args, list) or "--append-system-prompt" not in args:
            mismatches["args"] = {"expected": "append-system-prompt passthrough", "actual": args}
        if mismatches:
            raise AssertionError(f"desired-state mismatch: {mismatches}")
    except AssertionError as error:
        report.add("FAIL", "desired-state", str(error))
        report.block(CORE_CHECKS[5:], "desired state is not exact")
        return None
    report.add("PASS", "desired-state", "schema-v1 row with append-system-prompt passthrough")

    if not session_token_copy(root, case.name).is_file():
        report.add("FAIL", "token-projected", f"missing session_root copy: {session_token_copy(root, case.name)}")
        report.block(CORE_CHECKS[6:], "credential projection missing")
        return None
    report.add("PASS", "token-projected", "native->session_root one-way projection present")

    marker = f"AGYACP_OK_{uuid4().hex[:12]}"
    try:
        native_roundtrip(daemon, case, marker, nonce, reply_timeout)
    except (AssertionError, KeyError, StopIteration) as error:
        report.add("FAIL", "message-reply", str(error))
        report.block(CORE_CHECKS[7:], "no native reply to inspect")
        return None
    report.add("PASS", "message-reply", f"native harness_read+harness_reply closed loop, marker={marker}")
    report.add("PASS", "system-prompt-append", f"nonce {nonce} honored through the appended system prompt")

    try:
        permission_policy_probe(daemon, root, case, actor_uri, reply_timeout)
    except (AssertionError, KeyError, StopIteration) as error:
        report.add("FAIL", "permission-policy", str(error))
    else:
        report.add(
            "PASS",
            "permission-policy",
            "session/request_permission allowed harness_whoami/harness_reply and rejected the non-harness tool",
        )

    try:
        ref = persisted_session_ref(root, case.name)
        if ref is None:
            raise AssertionError("desired state holds no sessionRef for the running agy worker")
        if ref not in ready_session_ids(root, case):
            raise AssertionError(
                f"persisted sessionRef {ref} never appeared in worker.ready logs: "
                f"{sorted(ready_session_ids(root, case))}"
            )
    except AssertionError as error:
        report.add("FAIL", "session-ref", str(error))
        return None
    report.add("PASS", "session-ref", f"sessionRef recorded and observed ready: {ref}")
    return actor_uri


def validate_concurrency(
    daemon: IsolatedDaemon,
    root: Path,
    case_a: WorkerCase,
    case_b: WorkerCase,
    nonce_b: str,
    report: Report,
    reply_timeout: float,
) -> bool:
    fingerprint_before = token_copy_fingerprint(session_token_copy(root, case_a.name))
    try:
        code, value, diagnostic = run_cli(daemon, "agent", "create", "--name", case_b.name, "--json")
    except (OSError, subprocess.SubprocessError) as error:
        report.add("FAIL", "agent-b-launch", f"agent create exception: {error}")
        report.block(CONCURRENCY_CHECKS[1:], "worker B was not created")
        return False
    if code != 0 or value is None or value.get("ok") is not True:
        report.add("FAIL", "agent-b-launch", f"agent create failed ({code}): {diagnostic}")
        report.block(CONCURRENCY_CHECKS[1:], "worker B was not created")
        return False
    staged = root / "staged" / TOKEN_FILENAME
    try:
        code, value, diagnostic = run_cli(
            daemon, "agent", "provider", "login", "agy", "--actor", case_b.name, "--source", str(staged), "--json"
        )
    except (OSError, subprocess.SubprocessError) as error:
        report.add("FAIL", "agent-b-launch", f"provider login exception: {error}")
        report.block(CONCURRENCY_CHECKS[1:], "worker B has no credential")
        return False
    if code != 0 or value is None or value.get("ok") is not True:
        report.add("FAIL", "agent-b-launch", f"provider login failed ({code}): {diagnostic}")
        report.block(CONCURRENCY_CHECKS[1:], "worker B has no credential")
        return False
    try:
        code, value, diagnostic = start_worker(daemon, case_b, nonce_b)
    except (OSError, subprocess.SubprocessError) as error:
        report.add("FAIL", "agent-b-launch", f"agent start exception: {error}")
        report.block(CONCURRENCY_CHECKS[1:], "worker B did not launch")
        return False
    if code != 0 or value is None or value.get("ok") is not True:
        report.add("FAIL", "agent-b-launch", f"agent start failed ({code}): {diagnostic}")
        report.block(CONCURRENCY_CHECKS[1:], "worker B did not launch")
        return False
    report.add("PASS", "agent-b-launch", f"connector={case_b.connector_id} alongside {case_a.connector_id}")
    try:
        wait_running(daemon, case_b)
    except (AssertionError, OSError, KeyError, subprocess.SubprocessError) as error:
        report.add("FAIL", "agent-b-running", str(error))
        report.add("BLOCKED", "distinct-projection", "worker B never reached running")
        return True
    report.add("PASS", "agent-b-running", "both agy workers running concurrently")

    try:
        marker = f"AGYACP_B_{uuid4().hex[:12]}"
        native_roundtrip(daemon, case_b, marker, nonce_b, reply_timeout)
        copy_a = session_token_copy(root, case_a.name)
        copy_b = session_token_copy(root, case_b.name)
        if not copy_b.is_file():
            raise AssertionError(f"worker B session_root copy missing: {copy_b}")
        if copy_a.parent == copy_b.parent or copy_a == copy_b:
            raise AssertionError("workers share one projected credential path")
        fingerprint_after = token_copy_fingerprint(copy_a)
        if fingerprint_before != fingerprint_after:
            raise AssertionError(
                "worker B's start rewrote worker A's projected credential: "
                f"{fingerprint_before} -> {fingerprint_after}"
            )
        status = connector_status(daemon.ps(), case_a)
        if not status or status.get("running") is not True:
            raise AssertionError("worker A stopped or lost running state during worker B's turn")
    except (AssertionError, OSError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        report.add("FAIL", "distinct-projection", str(error))
    else:
        report.add(
            "PASS",
            "distinct-projection",
            "per-agent roots; A's copy untouched while B roundtripped on its own projection",
        )
    return True


def validate_restart_resume(
    daemon: IsolatedDaemon,
    root: Path,
    case: WorkerCase,
    actor_uri: str,
    nonce: str,
    report: Report,
    reply_timeout: float,
) -> None:
    ref_before = persisted_session_ref(root, case.name)
    ready_before = set(ready_session_ids(root, case))
    try:
        daemon.stop()
        daemon.start()
        wait_running(daemon, case)
    except (AssertionError, OSError, KeyError) as error:
        detail = str(error)
        try:
            connectors = [
                {"id": row.get("id"), "error": row.get("error")}
                for row in daemon.ps().get("connectors", [])
                if isinstance(row, dict) and row.get("id") == case.connector_id
            ]
            if connectors:
                detail = f"{detail}; connector: {connectors}"
        except (OSError, KeyError):
            pass
        report.add("FAIL", "restart-restore", detail)
        report.block(RESTART_CHECKS[1:], "worker did not recover after daemon restart")
        return
    report.add("PASS", "restart-restore", "desired agy worker running=true after daemon restart")

    try:
        ready_after = ready_session_ids(root, case)
        fresh = set(ready_after) - ready_before
        if ref_before is None:
            raise AssertionError("no sessionRef was recorded before the restart")
        if fresh and fresh != {ref_before}:
            raise AssertionError(
                f"restart created fresh ACP session(s) {sorted(fresh)} instead of resuming {ref_before}"
            )
        if ref_before not in ready_after:
            raise AssertionError(
                f"post-restart worker.ready never reported the resumed session {ref_before}: "
                f"{sorted(ready_after)}"
            )
    except AssertionError as error:
        report.add("FAIL", "same-session-id", str(error))
    else:
        report.add("PASS", "same-session-id", f"session/resume reused ACP sessionId {ref_before}")

    marker = f"AGYACP_R_{uuid4().hex[:12]}"
    try:
        native_roundtrip(daemon, case, marker, nonce, reply_timeout)
    except (AssertionError, KeyError, StopIteration) as error:
        report.add("FAIL", "resume-roundtrip", str(error))
        report.add("BLOCKED", "whoami-recovered", "post-restart roundtrip did not complete")
        return
    report.add("PASS", "resume-roundtrip", f"resumed session completed a native turn, marker={marker}")

    try:
        whoami_prompt = (
            "Harness ACP lifecycle post-restart identity probe. Call harness_whoami, "
            "then reply through harness_reply with a message containing one line "
            "WHOAMI=<the actor string> and ending with the exact integrity nonce "
            "token from your appended system prompt."
        )
        conversation, request_id = send_probe(daemon, case, whoami_prompt)
        reply = wait_for_reply(daemon, conversation, request_id, reply_timeout)
        if reply.get("messageId") != native_reply_id(request_id):
            raise AssertionError("post-restart whoami reply did not use harness_reply")
        text = reply_text(daemon, reply)
        whoami_line = next((line for line in text.splitlines() if "whoami=" in line.lower()), "")
        if actor_uri not in whoami_line:
            raise AssertionError(f"post-restart whoami actor missing (expected {actor_uri!r}): {text[:200]!r}")
        if nonce not in text:
            raise AssertionError("post-restart reply lost the systemPrompt.append nonce")
        ack_message(daemon, reply["messageId"])
    except (AssertionError, KeyError, StopIteration) as error:
        report.add("FAIL", "whoami-recovered", str(error))
    else:
        report.add("PASS", "whoami-recovered", "harness_whoami and harness_reply usable after recovery")


def validate_logout(
    daemon: IsolatedDaemon,
    root: Path,
    case: WorkerCase,
    report: Report,
) -> None:
    code, value, diagnostic = run_cli(
        daemon, "agent", "provider", "logout", "agy", "--actor", case.name, "--json"
    )
    if code != 0 or value is None or value.get("ok") is not True:
        report.add("FAIL", "logout-native-removed", f"provider logout failed ({code}): {diagnostic}")
        report.block(LOGOUT_CHECKS[1:], "logout did not complete")
        return
    if native_token_path(root, case.name).exists():
        report.add("FAIL", "logout-native-removed", "native_root token still present after logout")
        report.block(LOGOUT_CHECKS[1:], "native credential was not revoked")
        return
    report.add("PASS", "logout-native-removed", "native_root token removed by provider logout")

    try:
        daemon.stop()
        daemon.start()
    except (AssertionError, OSError, KeyError) as error:
        report.add("FAIL", "session-copy-removed", f"daemon restart failed: {error}")
        report.block(LOGOUT_CHECKS[1:], "restart after logout failed")
        return

    copy_path = session_token_copy(root, case.name)
    deadline = time.monotonic() + RESTORE_REFUSAL_WINDOW
    copy_removed = not copy_path.exists()
    while not copy_removed and time.monotonic() < deadline:
        time.sleep(1.0)
        copy_removed = not copy_path.exists()
    if not copy_removed:
        report.add("FAIL", "session-copy-removed", f"session_root token copy survived the restart: {copy_path}")
    else:
        report.add("PASS", "session-copy-removed", "restart removed the session_root token copy")

    refused = True
    running_seen = None
    deadline = time.monotonic() + RESTORE_REFUSAL_WINDOW
    while time.monotonic() < deadline:
        status = connector_status(daemon.ps(), case)
        if status and status.get("running") is True:
            refused = False
            running_seen = status
            break
        time.sleep(2.0)
    if not refused:
        report.add("FAIL", "restore-refused", f"credential-less desired worker became running again: {running_seen}")
    else:
        report.add("PASS", "restore-refused", f"no credential-less reuse: connector stayed not-running for {RESTORE_REFUSAL_WINDOW:g}s after restart")

    code, value, diagnostic = start_worker(daemon, case, "agyacp-refused-probe")
    survived = subprocess.run(
        [str(daemon.executable), "agent", "stop", case.name, "--json"],
        env=daemon.env, capture_output=True, text=True, timeout=90.0, check=False,
    )
    if code == 0 and value is not None and value.get("ok") is True:
        report.add(
            "FAIL",
            "start-refused",
            f"explicit start succeeded without a credential: {value} (cleanup rc={survived.returncode})",
        )
    else:
        detail = diagnostic[-300:]
        report.add("PASS", "start-refused", f"explicit start refused without credential: {detail}")


def validate_google_key(
    daemon: IsolatedDaemon,
    case: WorkerCase,
    nonce: str,
    report: Report,
    api_key: str | None,
    reply_timeout: float,
) -> None:
    if not api_key:
        reason = (
            "no GEMINI_API_KEY source (AGY_E2E_GEMINI_API_KEY or GEMINI_API_KEY); "
            "the google-provider grant path cannot run without a real key"
        )
        report.block(GOOGLE_CHECKS, reason)
        return
    code, value, diagnostic = run_cli(
        daemon, "agent", "create", "--name", case.name, "--json",
    )
    if code != 0 or value is None or value.get("ok") is not True:
        report.add("FAIL", "google-grant", f"agent create failed ({code}): {diagnostic}")
        report.block(GOOGLE_CHECKS[1:], "google worker was not created")
        return
    code, value, diagnostic = run_cli(
        daemon, "agent", "secret", "provider-write",
        "--entry-id", "agyacp-gemini", "--field-name", "apiKey", "--json",
        stdin=api_key,
    )
    if code != 0 or value is None or value.get("ok") is not True:
        report.add("FAIL", "google-grant", f"provider-write failed ({code}): {diagnostic}")
        report.block(GOOGLE_CHECKS[1:], "GEMINI_API_KEY entry was not stored")
        return
    code, value, diagnostic = run_cli(
        daemon, "agent", "secret", "grant", case.name,
        "--grant-id", "agyacp-gemini", "--source", "user-provider",
        "--entry-id", "agyacp-gemini", "--field-name", "apiKey",
        "--environment-name", "GEMINI_API_KEY", "--revision", "1", "--json",
    )
    if code != 0 or value is None or value.get("ok") is not True:
        report.add("FAIL", "google-grant", f"secret grant failed ({code}): {diagnostic}")
        report.block(GOOGLE_CHECKS[1:], "GEMINI_API_KEY grant was not admitted")
        return
    report.add("PASS", "google-grant", "stdin-only provider-write + GEMINI_API_KEY grant recorded")

    code, value, diagnostic = start_worker(daemon, case, nonce, provider=("google", GOOGLE_MODEL))
    if code != 0 or value is None or value.get("ok") is not True:
        report.add("FAIL", "google-launch", f"google-provider start failed ({code}): {diagnostic}")
        report.add("BLOCKED", "google-roundtrip", "google worker did not launch")
        return
    try:
        wait_running(daemon, case)
    except AssertionError as error:
        report.add("FAIL", "google-launch", str(error))
        report.add("BLOCKED", "google-roundtrip", "google worker never reached running")
        return
    report.add("PASS", "google-launch", f"provider=google model={GOOGLE_MODEL} running=true")

    marker = f"AGYACP_G_{uuid4().hex[:12]}"
    try:
        native_roundtrip(daemon, case, marker, nonce, reply_timeout)
    except (AssertionError, KeyError, StopIteration) as error:
        report.add("FAIL", "google-roundtrip", str(error))
    else:
        report.add("PASS", "google-roundtrip", f"API-key auth roundtrip, marker={marker}")


def _check_checkout_provenance(report: Report) -> object | None:
    resolution = resolve_hyprial()
    record_resolution(resolution)
    print(f"RESULT NOTE {SCENARIO} source-sha={resolution.tree_head or '-'} executable={resolution.path}", flush=True)
    pinned = (ROOT / ".venv" / "bin" / "hyprial").resolve()
    tree_head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    checkout_head = tree_head.stdout.strip() if tree_head.returncode == 0 else ""
    provenance_error: str | None = None
    if resolution.path != pinned:
        provenance_error = f"HYPRIAL_BIN must be pinned to this checkout's {pinned}, got {resolution.path}"
    elif resolution.src_dir is None or resolution.src_dir.resolve() != ROOT.resolve():
        provenance_error = f"executable editable source {resolution.src_dir} does not match checkout {ROOT}"
    elif not checkout_head or not resolution.tree_head or resolution.tree_head != checkout_head:
        provenance_error = f"resolved tree head {resolution.tree_head!r} != checkout HEAD {checkout_head!r}"
    else:
        try:
            assert_tree_matches_declared_sha(resolution)
        except AssertionError as error:
            provenance_error = str(error)
    if provenance_error is not None:
        report.add("FAIL", "preflight.checkout-binary", provenance_error)
        report.block(ALL_PHASE_CHECKS, "binary provenance mismatch against checkout under test")
        return None
    report.add("PASS", "preflight.checkout-binary", f"executable={resolution.path} head={resolution.tree_head}")
    return resolution


def main() -> int:
    report = Report()
    resolution = _check_checkout_provenance(report)
    if resolution is None:
        return report.exit_code()

    real_agy = shutil.which("agy")
    if real_agy is None:
        return report.refuse_all("preflight.agy-binary", "real agy CLI is not on PATH")
    report.add("PASS", "preflight.agy-binary", f"agy={real_agy}")

    token_source = os.environ.get("AGY_E2E_OAUTH_TOKEN", "").strip()
    refusal: str | None = "AGY_E2E_OAUTH_TOKEN does not name an Antigravity OAuth token file"
    if token_source:
        source_path = Path(token_source).expanduser()
        try:
            metadata = source_path.lstat()
        except OSError as error:
            refusal = f"token source unreadable: {error.strerror or error}"
        else:
            if source_path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_size == 0:
                refusal = "token source must be a non-empty regular file, not a symlink"
            else:
                refusal = None
                report.add(
                    "PASS",
                    "preflight.oauth-token",
                    f"source={source_path} bytes={metadata.st_size} (content never read or printed)",
                )
    if refusal is not None:
        return report.refuse_all("preflight.oauth-token", refusal)

    api_key = (
        os.environ.get("AGY_E2E_GEMINI_API_KEY", "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
        or None
    )
    if api_key:
        report.add("PASS", "preflight.gemini-key", "key present via environment; value never printed")
    else:
        report.add(
            "BLOCKED",
            "preflight.gemini-key",
            "AGY_E2E_GEMINI_API_KEY/GEMINI_API_KEY not set; google-provider leg cannot execute",
        )

    reply_timeout = float(os.environ.get("AGY_E2E_REPLY_TIMEOUT", str(REPLY_TIMEOUT_DEFAULT)))
    gemini_home = Path.home() / ".gemini"
    gemini_before = snapshot_tree(gemini_home)

    temp_parent = os.environ.get("TMPDIR") or ("/var/tmp" if Path("/var/tmp").is_dir() else None)
    with tempfile.TemporaryDirectory(prefix="hyprial-agy-acp-lifecycle-", dir=temp_parent) as temporary:
        root = Path(temporary)
        stage_oauth_token(root, source_path)
        case_a = WorkerCase("agyacp-agy-a", "agyacp-agy-a", root / "work-a")
        case_b = WorkerCase("agyacp-agy-b", "agyacp-agy-b", root / "work-b")
        case_g = WorkerCase("agyacp-agy-g", "agyacp-agy-g", root / "work-g")
        for case in (case_a, case_b, case_g):
            case.cwd.mkdir(parents=True, exist_ok=True)
        nonce_a = f"AGYACP_NONCE_A_{uuid4().hex[:12]}"
        nonce_b = f"AGYACP_NONCE_B_{uuid4().hex[:12]}"
        nonce_g = f"AGYACP_NONCE_G_{uuid4().hex[:12]}"
        daemon = IsolatedDaemon(resolution.path, root, "agyacp-agy-node")
        wiretap_bin, _audit_dir = install_acp_permission_wiretap(root, real_agy)
        daemon.env["PATH"] = os.pathsep.join([str(wiretap_bin), daemon.env.get("PATH", "")])
        (root / "tmp").mkdir(mode=0o700, exist_ok=True)
        daemon.env["TMPDIR"] = str(root / "tmp")
        daemon.env.pop("GEMINI_API_KEY", None)
        daemon.env.pop("AGY_E2E_GEMINI_API_KEY", None)

        def _stop_all() -> None:
            try:
                daemon.command("agent", "stop", "--all", "--json", timeout=120.0)
            except (AssertionError, OSError, subprocess.TimeoutExpired):
                pass

        def _stop_daemon() -> None:
            daemon.stop()
            daemon.wait_stopped(timeout=30.0)

        try:
            daemon.start()
        except (AssertionError, OSError, KeyError) as error:
            try:
                daemon.stop()
                daemon.wait_stopped(timeout=15.0)
            except (AssertionError, OSError):
                pass
            report.add("FAIL", "preflight.isolated-daemon", str(error))
            report.block(ALL_PHASE_CHECKS, "isolated daemon did not start")
            if snapshot_tree(gemini_home) != gemini_before:
                report.add("FAIL", "teardown.gemini-home-unchanged", "global ~/.gemini changed during the run")
            return report.exit_code()

        with teardown_guard(
            _stop_all,
            _stop_daemon,
            record=lambda detail: report.add("FAIL", "teardown", f"daemon teardown also raised: {detail}"),
        ):
            try:
                daemon.command("agent", "create", "--name", DRIVER, "--json")
                _org.ensure_local_org(daemon, DRIVER, prefix="agy032")
            except (AssertionError, OSError, KeyError, subprocess.SubprocessError) as error:
                report.add("FAIL", "preflight.isolated-daemon", f"driver/org setup failed: {error}")
                report.block(ALL_PHASE_CHECKS, "isolated daemon setup failed")
            else:
                report.add(
                    "PASS",
                    "preflight.isolated-daemon",
                    f"HYPRIAL_HOME={root / 'home'} HARNESS_STATE_DIR={root / 'state'} loopback+isolated",
                )
                actor_uri = validate_core(daemon, root, case_a, nonce_a, report, reply_timeout)
                if actor_uri is None:
                    report.block(CONCURRENCY_CHECKS + RESTART_CHECKS + LOGOUT_CHECKS, "core agy lifecycle did not complete")
                else:
                    b_launched = validate_concurrency(daemon, root, case_a, case_b, nonce_b, report, reply_timeout)
                    if b_launched:
                        try:
                            code, _val, diag = run_cli(daemon, "agent", "stop", case_b.name, "--json", timeout=90.0)
                        except (OSError, subprocess.SubprocessError) as error:
                            print(f"RESULT NOTE {SCENARIO} worker-b-stop exception={error}", flush=True)
                        else:
                            if code != 0:
                                print(f"RESULT NOTE {SCENARIO} worker-b-stop code={code} {diag[:200]}", flush=True)
                    validate_restart_resume(daemon, root, case_a, actor_uri, nonce_a, report, reply_timeout)
                    validate_logout(daemon, root, case_a, report)
                validate_google_key(daemon, case_g, nonce_g, report, api_key, reply_timeout)
                try:
                    _stop_daemon()
                    report.add("PASS", "teardown", "workers stopped, daemon stopped, temp root deleted")
                except (AssertionError, OSError) as error:
                    report.add("FAIL", "teardown", str(error))

        if snapshot_tree(gemini_home) != gemini_before:
            report.add("FAIL", "teardown.gemini-home-unchanged", "global ~/.gemini changed during the run")
        else:
            report.add("PASS", "teardown.gemini-home-unchanged", "global ~/.gemini untouched")
    return report.exit_code()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
        print(f"RESULT FAIL {SCENARIO} runner {error}")
        raise SystemExit(1) from error
