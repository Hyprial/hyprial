# Decision record (file header; no inline comments by repository rule):
# - Shared runtime helpers for contract/e2e/run-agy-acp-lifecycle.py split
#   out to keep both E2E modules comfortably under the 1000-line structural cap.
# - Permission verification proves ACP negotiation via a transparent single-process
#   selectors/self-pipe stdio wire-tap around the real agy binary recording only
#   non-sensitive session/request_permission metadata and selected outcome
#   (harness_whoami allowed vs exact one write_file rejected), paired with
#   transcript tool execution checks and disk non-existence.
"""Shared helpers for the manual AGY ACP lifecycle acceptance runner."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import IsolatedDaemon, desired_state_document

SCENARIO = "AGY-ACP-LIFECYCLE"
DRIVER = "agyacp-driver"
TOKEN_FILENAME = "antigravity-oauth-token"
GOOGLE_MODEL = "gemini-3.8-flash"
NATIVE_MARKER = "hyprial:reply:{message_id}"
REPLY_TIMEOUT_DEFAULT = 300.0
RESTORE_REFUSAL_WINDOW = 90.0

CORE_CHECKS = (
    "agent-create",
    "provider-login",
    "launch",
    "ps-running",
    "desired-state",
    "token-projected",
    "message-reply",
    "system-prompt-append",
    "permission-policy",
    "session-ref",
)
CONCURRENCY_CHECKS = ("agent-b-launch", "agent-b-running", "distinct-projection")
RESTART_CHECKS = ("restart-restore", "same-session-id", "resume-roundtrip", "whoami-recovered")
LOGOUT_CHECKS = ("logout-native-removed", "session-copy-removed", "restore-refused", "start-refused")
GOOGLE_CHECKS = ("google-grant", "google-launch", "google-roundtrip")

ALL_PHASE_CHECKS = CORE_CHECKS + CONCURRENCY_CHECKS + RESTART_CHECKS + LOGOUT_CHECKS + GOOGLE_CHECKS


@dataclass(frozen=True, slots=True)
class WorkerCase:
    name: str
    nickname: str
    cwd: Path

    @property
    def connector_id(self) -> str:
        return f"agy:{self.name}"


class Report:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def add(self, status: str, check: str, detail: str) -> None:
        clean = " ".join(str(detail).split())[:400]
        self.results.append((status, check, clean))
        print(f"RESULT {status} {SCENARIO} {check} {clean}", flush=True)

    def block(self, checks: tuple[str, ...], reason: str) -> None:
        for check in checks:
            self.add("BLOCKED", check, reason)

    def exit_code(self) -> int:
        counts = {
            status: sum(1 for item in self.results if item[0] == status)
            for status in ("PASS", "FAIL", "BLOCKED")
        }
        overall = (
            "FAIL"
            if counts["FAIL"]
            else ("BLOCKED" if counts["BLOCKED"] else "PASS")
        )
        print(
            f"RESULT {overall} {SCENARIO} manual "
            f"pass={counts['PASS']} fail={counts['FAIL']} blocked={counts['BLOCKED']}",
            flush=True,
        )
        return 1 if counts["FAIL"] else (2 if counts["BLOCKED"] else 0)

    def refuse_all(self, check: str, reason: str) -> int:
        self.add("BLOCKED", check, reason)
        self.block(ALL_PHASE_CHECKS, reason)
        return self.exit_code()


def run_cli(
    daemon: IsolatedDaemon, *args: str, timeout: float = 120.0, stdin: str | None = None
) -> tuple[int, dict[str, Any] | None, str]:
    completed = subprocess.run(
        [str(daemon.executable), *args],
        env=daemon.env,
        input=stdin,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    value: dict[str, Any] | None = None
    try:
        decoded = json.loads(completed.stdout)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        value = decoded
    diagnostic = completed.stderr or completed.stdout or "no diagnostic output"
    return completed.returncode, value, diagnostic[-4000:]


def connector_status(value: dict[str, Any], case: WorkerCase) -> dict[str, Any] | None:
    connectors = value.get("connectors", [])
    if not isinstance(connectors, list):
        return None
    return next(
        (
            item
            for item in connectors
            if isinstance(item, dict) and item.get("id") == case.connector_id
        ),
        None,
    )


def wait_running(daemon: IsolatedDaemon, case: WorkerCase, timeout: float = 180.0) -> dict[str, Any]:
    return daemon.wait(
        lambda: daemon.ps(),
        lambda value: bool(
            (status := connector_status(value, case)) and status.get("running") is True
        ),
        timeout=timeout,
    )


def desired_agy_row(root: Path, name: str) -> dict[str, Any]:
    document = desired_state_document(root)
    matches = [
        item
        for item in document.get("providers", [])
        if isinstance(item, dict)
        and item.get("provider") == "agy"
        and item.get("name") == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one desired agy row for {name}, found {len(matches)}")
    return matches[0]


def persisted_session_ref(root: Path, name: str) -> str | None:
    ref = desired_agy_row(root, name).get("sessionRef")
    return ref if isinstance(ref, str) and ref else None


def agent_home(root: Path, name: str) -> Path:
    return root / "home" / "agents" / name


def native_token_path(root: Path, name: str) -> Path:
    return agent_home(root, name) / "secrets" / "native" / "agy" / TOKEN_FILENAME


def session_token_copy(root: Path, name: str) -> Path:
    return (
        agent_home(root, name)
        / "state"
        / "sessions"
        / "agy"
        / "antigravity-cli"
        / TOKEN_FILENAME
    )


def token_copy_fingerprint(path: Path) -> tuple[int, int] | None:
    """(size, mtime_ns) of a projected token copy; contents are never read."""

    try:
        metadata = path.stat()
    except OSError:
        return None
    return (metadata.st_size, metadata.st_mtime_ns)


def ready_session_ids(root: Path, case: WorkerCase) -> dict[str, int]:
    """sessionId -> worker.ready count from this worker's daemon-owned logs."""

    found: dict[str, int] = {}
    for path in root.rglob(f"agy-{case.name}.jsonl"):
        if path.is_symlink() or not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("event") != "worker.ready":
                continue
            session_id = entry.get("sessionId")
            if isinstance(session_id, str) and session_id:
                found[session_id] = found.get(session_id, 0) + 1
    return found


def pending_texts(daemon: IsolatedDaemon, actor: str) -> list[dict[str, Any]]:
    result = daemon.request("message.pending.list", {"actor": actor})
    messages = result.get("messages")
    if not isinstance(messages, list):
        return []
    return [item for item in messages if isinstance(item, dict)]


def reply_text(daemon: IsolatedDaemon, row: dict[str, Any]) -> str:
    params = {"actor": f"user:{os.environ.get('HYPRIAL_OWNER', 'e2e')}", "onBehalfOf": DRIVER, "messageId": row["messageId"]}
    status = daemon.command("daemon", "status", "--json")
    owner = status.get("daemon", {}).get("owner")
    if isinstance(owner, str) and owner:
        params["actor"] = f"user:{owner}"
    body = daemon.request("message.read_body", params)
    return "\n".join(str(body.get(field) or "") for field in ("summary", "message"))


def native_reply_id(message_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, NATIVE_MARKER.format(message_id=message_id)))


def send_probe(daemon: IsolatedDaemon, case: WorkerCase, prompt: str) -> tuple[str, str]:
    conversation = f"agyacp-{case.name}-{time.time_ns()}"
    summary = f"Action required for {case.nickname}: read full body via harness_read_body or harness_read first."
    sent = daemon.command(
        "message", "send", "--from", DRIVER, "--to", case.nickname,
        "--conversation", conversation, "--summary", summary, prompt, "--json",
    )
    if sent.get("ok") is not True or not isinstance(sent.get("messageId"), str):
        raise AssertionError(f"probe send was not accepted: {sent}")
    return conversation, sent["messageId"]


def ack_message(daemon: IsolatedDaemon, message_id: str) -> None:
    ack = daemon.command("message", "ack", "--from", DRIVER, message_id, "--json")
    if ack.get("acknowledged") is not True:
        raise AssertionError(f"reply acknowledgement failed: {ack}")


def wait_for_reply(
    daemon: IsolatedDaemon, conversation: str, request_id: str, timeout: float
) -> dict[str, Any]:
    expected_native = native_reply_id(request_id)
    retry_ids: set[str] = set()

    def is_retry_notice(row: dict[str, Any]) -> bool:
        mid = str(row.get("messageId") or "")
        if mid == expected_native:
            return False
        if mid in retry_ids:
            return True
        if "⚠️" in str(row.get("summary") or "") and "(将重试)" in reply_text(daemon, row):
            retry_ids.add(mid)
            return True
        return False

    def probe() -> list[dict[str, Any]]:
        return [
            item for item in pending_texts(daemon, DRIVER)
            if item.get("conversationId") == conversation and item.get("messageId") != request_id
        ]

    found = daemon.wait(probe, lambda rows: any(not is_retry_notice(r) for r in rows), timeout=timeout)
    final = [row for row in found if not is_retry_notice(row)]
    for notice in found:
        if is_retry_notice(notice):
            ack_message(daemon, notice["messageId"])
    if len(final) != 1:
        raise AssertionError(f"expected exactly one reply, found {len(final)}")
    return final[0]


def native_roundtrip(
    daemon: IsolatedDaemon,
    case: WorkerCase,
    marker: str,
    nonce: str,
    timeout: float,
    extra_instructions: str = "",
) -> dict[str, Any]:
    """One real turn whose ONLY accepted reply path is the harness_reply tool."""

    prompt = (
        f"Harness ACP lifecycle probe for {case.nickname}. As your FIRST action, call "
        "harness_read_body (or harness_read) to read this message, then reply through the "
        "harness_reply tool with messageId=<this message's id> and a message containing "
        f"the exact marker {marker} followed by the exact integrity nonce token required "
        "by your appended system prompt. "
        "The harness_* MCP tools are already permitted; do not end your turn "
        "before harness_reply has succeeded. "
        f"Do not use any non-harness tool.{extra_instructions}"
    )
    conversation, request_id = send_probe(daemon, case, prompt)
    reply = wait_for_reply(daemon, conversation, request_id, timeout)
    if reply.get("messageId") != native_reply_id(request_id):
        raise AssertionError(
            "reply did not come through the native harness_reply tool path: "
            f"sent={request_id} got={reply.get('messageId')} "
            f"expected(native)={native_reply_id(request_id)}"
        )
    text = reply_text(daemon, reply)
    if marker not in text:
        raise AssertionError(f"correlated reply lacks the demanded marker {marker}: {text[:200]!r}")
    if nonce not in text:
        raise AssertionError(
            f"reply lacks the systemPrompt.append nonce {nonce}: {text[:200]!r}"
        )
    ack_message(daemon, reply["messageId"])
    return reply


def install_acp_permission_wiretap(root: Path, real_agy: str) -> tuple[Path, Path]:
    """Install a transparent stdio wire-tap shim that logs permission frames."""

    audit_dir = root / "acp-permission-audit"
    audit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    bin_dir = root / "wiretap-bin"
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    shim_path = bin_dir / "agy"
    template = '''#!__PYTHON__
"""Transparent agy ACP stdio shim logging redacted permission metadata only."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

REAL_AGY = __REAL_AGY__
AUDIT_DIR = __AUDIT_DIR__
STRIP_DIR = __STRIP_DIR__
TOOL_CALL_KEYS = ("toolCallId", "name", "toolName", "title", "kind")
PATH_INPUT_KEYS = ("Path", "path", "TargetFile", "file_path", "filePath")


def _extract_session_root_hash(argv: list[str]) -> str | None:
    for idx, token in enumerate(argv):
        if token == "--gemini-dir" and idx + 1 < len(argv):
            resolved = str(Path(argv[idx + 1]).resolve())
            return hashlib.sha256(resolved.encode("utf-8")).hexdigest()
        if token.startswith("--gemini-dir="):
            raw = token.split("=", 1)[1]
            if raw:
                resolved = str(Path(raw).resolve())
                return hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    return None


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    strip_resolved = Path(STRIP_DIR).resolve()
    parts = [
        entry
        for entry in env.get("PATH", "").split(os.pathsep)
        if entry and Path(entry).resolve() != strip_resolved
    ]
    env["PATH"] = os.pathsep.join(parts)
    return env


def _write_all(fd: int, data: bytes) -> bool:
    view = memoryview(data)
    written = 0
    while written < len(view):
        try:
            step = os.write(fd, view[written:])
        except BlockingIOError:
            continue
        except OSError:
            return False
        if step <= 0:
            return False
        written += step
    return True


def _redact_tool_call(raw_tc: object) -> dict[str, object]:
    if not isinstance(raw_tc, dict):
        return {}
    tc: dict[str, object] = {}
    for key in TOOL_CALL_KEYS:
        val = raw_tc.get(key)
        if isinstance(val, str):
            tc[key] = val
    raw_input = raw_tc.get("rawInput")
    if isinstance(raw_input, dict):
        for path_key in PATH_INPUT_KEYS:
            raw_path = raw_input.get(path_key)
            if isinstance(raw_path, str) and raw_path:
                basename = Path(raw_path).name
                if basename:
                    tc["targetBasename"] = basename
                    break
    return tc


def _redact_outcome(result: object) -> dict[str, object] | None:
    if not isinstance(result, dict):
        return None
    raw_out = result.get("outcome")
    if not isinstance(raw_out, dict):
        return None
    out: dict[str, object] = {}
    outcome_val = raw_out.get("outcome")
    if isinstance(outcome_val, str):
        out["outcome"] = outcome_val
    option_id = raw_out.get("optionId")
    if isinstance(option_id, str):
        out["optionId"] = option_id
    return out


def main() -> int:
    session_root_hash = _extract_session_root_hash(sys.argv[1:])
    audit_path = Path(AUDIT_DIR) / f"{os.getpid()}.jsonl"
    child = subprocess.Popen(
        [REAL_AGY, *sys.argv[1:]],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        env=_child_env(),
        bufsize=0,
    )
    assert child.stdin is not None and child.stdout is not None
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    child_in_fd = child.stdin.fileno()
    child_out_fd = child.stdout.fileno()
    child_in_open = True

    def _close_child_stdin() -> None:
        nonlocal child_in_open
        if child_in_open:
            child_in_open = False
            try:
                child.stdin.close()
            except OSError:
                pass

    sig_r, sig_w = os.pipe()
    os.set_blocking(sig_r, False)
    os.set_blocking(sig_w, False)

    def _on_signal(_signum: int, _frame: object) -> None:
        if child.poll() is None:
            child.terminate()
        try:
            os.write(sig_w, b"x")
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    tracked_queues: dict[tuple[str, int | str], deque[str | None]] = {}
    audit_file = audit_path.open("ab")

    def _write_record(record: dict[str, object]) -> None:
        audit_file.write(json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\\n")
        audit_file.flush()

    def _inspect_line(raw_line: bytes, is_child_stdout: bool) -> None:
        text = raw_line.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return
        if not isinstance(decoded, dict):
            return
        req_id = decoded.get("id")
        if type(req_id) is not int and not isinstance(req_id, str):
            return
        key = (type(req_id).__name__, req_id)
        if is_child_stdout:
            if decoded.get("method") != "session/request_permission":
                return
            params = decoded.get("params")
            param_map = params if isinstance(params, dict) else {}
            raw_opts = param_map.get("options")
            opts = (
                [
                    {"optionId": opt.get("optionId"), "kind": opt.get("kind")}
                    for opt in raw_opts
                    if isinstance(opt, dict)
                ]
                if isinstance(raw_opts, list)
                else []
            )
            sid = param_map.get("sessionId")
            tracked_queues.setdefault(key, deque()).append(
                sid if isinstance(sid, str) else None
            )
            _write_record(
                {
                    "direction": "request",
                    "sessionRootHash": session_root_hash,
                    "id": req_id,
                    "idType": key[0],
                    "method": "session/request_permission",
                    "sessionId": sid if isinstance(sid, str) else None,
                    "toolCall": _redact_tool_call(param_map.get("toolCall")),
                    "options": opts,
                }
            )
            return
        if "method" in decoded:
            return
        queue = tracked_queues.get(key)
        if not queue:
            return
        sid = queue.popleft()
        _write_record(
            {
                "direction": "response",
                "sessionRootHash": session_root_hash,
                "sessionId": sid,
                "id": req_id,
                "idType": key[0],
                "outcome": _redact_outcome(decoded.get("result")),
                "hasError": decoded.get("error") is not None,
            }
        )

    def _flush_lines(buf: bytearray, is_child_stdout: bool, flush_remainder: bool = False) -> None:
        while True:
            nl = buf.find(b"\\n")
            if nl < 0:
                break
            line = bytes(buf[: nl + 1])
            del buf[: nl + 1]
            _inspect_line(line, is_child_stdout)
        if flush_remainder and buf:
            _inspect_line(bytes(buf), is_child_stdout)
            buf.clear()

    sel = selectors.SelectSelector()
    sel.register(stdin_fd, selectors.EVENT_READ, "stdin")
    sel.register(child_out_fd, selectors.EVENT_READ, "child_out")
    sel.register(sig_r, selectors.EVENT_READ, "signal")
    in_buf = bytearray()
    out_buf = bytearray()
    shutdown_deadline: float | None = None

    try:
        while child_out_fd in sel.get_map():
            timeout: float | None = None
            if shutdown_deadline is not None:
                remaining = shutdown_deadline - time.monotonic()
                if remaining <= 0:
                    break
                timeout = min(0.1, remaining)
            events = sel.select(timeout=timeout)
            for key, _mask in events:
                tag = key.data
                if tag == "signal":
                    while True:
                        try:
                            if not os.read(sig_r, 1024):
                                break
                        except BlockingIOError:
                            break
                    if shutdown_deadline is None:
                        shutdown_deadline = time.monotonic() + 2.0
                    if stdin_fd in sel.get_map():
                        sel.unregister(stdin_fd)
                    _close_child_stdin()
                    if child.poll() is None:
                        child.terminate()
                elif tag == "stdin":
                    try:
                        chunk = os.read(stdin_fd, 65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b""
                    if not chunk:
                        sel.unregister(stdin_fd)
                        _flush_lines(in_buf, False, flush_remainder=True)
                        _close_child_stdin()
                        continue
                    if child_in_open:
                        if not _write_all(child_in_fd, chunk):
                            sel.unregister(stdin_fd)
                            _flush_lines(in_buf, False, flush_remainder=True)
                            _close_child_stdin()
                            continue
                        in_buf.extend(chunk)
                        _flush_lines(in_buf, False)
                elif tag == "child_out":
                    try:
                        chunk = os.read(child_out_fd, 65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b""
                    if not chunk:
                        sel.unregister(child_out_fd)
                        _flush_lines(out_buf, True, flush_remainder=True)
                        break
                    out_buf.extend(chunk)
                    _flush_lines(out_buf, True)
                    if not _write_all(stdout_fd, chunk):
                        sel.unregister(child_out_fd)
                        if child.poll() is None:
                            child.terminate()
                        break
        _close_child_stdin()
        if child.poll() is None:
            if shutdown_deadline is not None:
                child.kill()
            else:
                try:
                    child.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    child.terminate()
                    try:
                        child.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        child.kill()
        rc = child.wait()
        return rc if rc >= 0 else 128 + (-rc)
    finally:
        _close_child_stdin()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        sel.close()
        os.close(sig_r)
        os.close(sig_w)
        audit_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
'''
    shim_source = (
        template.replace("__PYTHON__", sys.executable, 1)
        .replace("__REAL_AGY__", json.dumps(real_agy), 1)
        .replace("__AUDIT_DIR__", json.dumps(str(audit_dir)), 1)
        .replace("__STRIP_DIR__", json.dumps(str(bin_dir)), 1)
    )
    shim_path.write_text(shim_source, encoding="utf-8")
    os.chmod(shim_path, 0o755)
    return bin_dir, audit_dir


def _extract_selected_kind(req: dict[str, Any], resp: dict[str, Any]) -> str:
    if resp.get("hasError"):
        raise AssertionError(f"session/request_permission response returned error: {resp}")
    outcome = resp.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("outcome") != "selected":
        raise AssertionError(f"permission response did not select an option: {resp}")
    option_id = outcome.get("optionId")
    if not isinstance(option_id, str) or not option_id:
        raise AssertionError(f"permission response missing selected optionId: {resp}")
    options = req.get("options")
    if not isinstance(options, list):
        raise AssertionError(f"permission request missing options list: {req}")
    for option in options:
        if isinstance(option, dict) and option.get("optionId") == option_id:
            kind = option.get("kind")
            if isinstance(kind, str) and kind:
                return kind
            raise AssertionError(f"matched option {option_id!r} lacks option kind: {option}")
    raise AssertionError(f"selected optionId {option_id!r} absent from request options {options!r}")


def _tool_call_labels(req: dict[str, Any]) -> tuple[str, str, str]:
    tc = req.get("toolCall")
    tc_map = tc if isinstance(tc, dict) else {}
    raw_name = tc_map.get("name") or tc_map.get("toolName")
    tool_name = raw_name if isinstance(raw_name, str) else ""
    title = str(tc_map.get("title") or "")
    raw_base = tc_map.get("targetBasename")
    target_basename = raw_base if isinstance(raw_base, str) else ""
    return tool_name, title, target_basename


def assert_permission_audit_evidence(root: Path, case: WorkerCase, target_path: Path) -> None:
    """Verify structured ACP session/request_permission frames from the wire-tap."""

    audit_dir = root / "acp-permission-audit"
    if not audit_dir.is_dir():
        raise AssertionError(f"permission audit directory missing: {audit_dir}")
    expected_root = str((agent_home(root, case.name) / "state" / "sessions" / "agy").resolve())
    expected_root_hash = hashlib.sha256(expected_root.encode("utf-8")).hexdigest()
    session_ids = set(ready_session_ids(root, case))
    if not session_ids:
        raise AssertionError(f"no worker.ready sessionId found for {case.name}")

    pairs: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for path in sorted(audit_dir.glob("*.jsonl")):
        if path.is_symlink() or not path.is_file():
            continue
        pending: dict[tuple[str, int | str], list[dict[str, Any]]] = {}
        file_requests: list[tuple[tuple[str, int | str], dict[str, Any]]] = []
        matched_responses: dict[int, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("sessionRootHash") != expected_root_hash:
                continue
            direction = entry.get("direction")
            id_type = entry.get("idType")
            req_id = entry.get("id")
            if not isinstance(id_type, str) or (type(req_id) is not int and not isinstance(req_id, str)):
                continue
            key = (id_type, req_id)
            if direction == "request" and entry.get("method") == "session/request_permission":
                if entry.get("sessionId") not in session_ids:
                    continue
                pending.setdefault(key, []).append(entry)
                file_requests.append((key, entry))
            elif direction == "response":
                queue = pending.get(key)
                if not queue:
                    raise AssertionError(f"unmatched permission response in {path.name}: {entry}")
                req_entry = queue.pop(0)
                matched_responses[id(req_entry)] = entry
        for _key, req_entry in file_requests:
            resp_entry = matched_responses.get(id(req_entry))
            if resp_entry is None:
                raise AssertionError(f"unanswered session/request_permission frame in {path.name}: {req_entry}")
            selected_kind = _extract_selected_kind(req_entry, resp_entry)
            pairs.append((req_entry, resp_entry, selected_kind))

    if not pairs:
        raise AssertionError(
            f"no structured session/request_permission pairs recorded for {case.name} "
            f"(sessionRootHash={expected_root_hash}, sessions={sorted(session_ids)})"
        )

    write_pairs: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    whoami_pairs: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for req_entry, resp_entry, selected_kind in pairs:
        tool_name, title, target_basename = _tool_call_labels(req_entry)
        is_harness = tool_name.startswith("mcp_harness-bridge_") or "mcp_harness-bridge_" in title
        if not is_harness and selected_kind in ("allow_once", "allow_always"):
            raise AssertionError(
                f"permission audit allowed non-harness tool {tool_name or title!r}: {selected_kind}"
            )
        if (tool_name == "write_file" or "write_file" in tool_name or "write_file" in title) and (
            target_basename == target_path.name or target_path.name in title
        ):
            write_pairs.append((req_entry, resp_entry, selected_kind))
        if tool_name == "mcp_harness-bridge_harness_whoami" or "mcp_harness-bridge_harness_whoami" in title:
            whoami_pairs.append((req_entry, resp_entry, selected_kind))

    if len(write_pairs) != 1:
        raise AssertionError(
            f"expected exactly 1 permission request for write_file on {target_path.name}, found {len(write_pairs)}"
        )
    if write_pairs[0][2] not in ("reject_once", "reject_always"):
        raise AssertionError(
            f"write_file permission selected {write_pairs[0][2]!r}, expected reject_once/reject_always"
        )
    if not whoami_pairs:
        raise AssertionError(
            "permission audit lacks allowed session/request_permission frame for harness_whoami"
        )
    if any(kind not in ("allow_once", "allow_always") for _r, _p, kind in whoami_pairs):
        raise AssertionError(
            f"harness_whoami permission was not allowed: {[k for _r, _p, k in whoami_pairs]!r}"
        )


def assert_permission_transcript_evidence(
    root: Path, case: WorkerCase, target_path: Path, actor_uri: str
) -> None:
    """Verify tool_calls execution, allowed whoami, and absence of write_file success."""

    sessions_dir = agent_home(root, case.name) / "state" / "sessions" / "agy"
    session_ids = set(ready_session_ids(root, case))
    if not session_ids:
        raise AssertionError(f"no worker.ready sessionId found for {case.name}")
    transcripts: list[Path] = []
    if sessions_dir.is_dir():
        for candidate in sorted(sessions_dir.rglob("transcript.jsonl")):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if any(sid in candidate.parts for sid in session_ids):
                transcripts.append(candidate)
    if not transcripts:
        raise AssertionError(
            f"no session transcript.jsonl found under {sessions_dir} for {sorted(session_ids)}"
        )
    whoami_called = False
    whoami_allowed = False
    write_calls = 0
    write_succeeded = False
    for path in transcripts:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            calls = entry.get("tool_calls")
            if isinstance(calls, list):
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    name = call.get("name")
                    if name == "mcp_harness-bridge_harness_whoami":
                        whoami_called = True
                    elif name == "write_file" and target_path.name in json.dumps(call.get("args", {})):
                        write_calls += 1
            status = entry.get("status")
            content = str(entry.get("content") or "")
            error_text = str(entry.get("error") or "")
            combined = f"{error_text}\n{content}"
            if status == "DONE" and actor_uri in content and ('"ok": true' in content or '"ok":true' in content):
                whoami_allowed = True
            if (
                status == "DONE"
                and not entry.get("tool_calls")
                and target_path.name in combined
                and "created file" in combined.lower()
            ):
                write_succeeded = True
    if not whoami_called or not whoami_allowed:
        raise AssertionError("transcript lacks allowed harness_whoami tool execution")
    if write_calls != 1:
        raise AssertionError(
            f"transcript expected exactly 1 write_file call for {target_path.name}, found {write_calls}"
        )
    if write_succeeded:
        raise AssertionError(f"transcript shows write_file succeeded for {target_path.name}")


def permission_policy_probe(
    daemon: IsolatedDaemon, root: Path, case: WorkerCase, actor_uri: str, timeout: float
) -> None:
    """Default mode: harness_* MCP tools auto-allowed, other tools rejected."""

    target_path = case.cwd / f"agyacp-sentinel-{uuid4().hex[:8]}.txt"
    prompt = (
        "Harness ACP lifecycle permission probe, execute sequentially without batching. "
        "1) Call harness_whoami MCP tool first and wait for its result. "
        f"2) After step 1 finishes, actually invoke the non-harness write_file tool ONCE to create {target_path}; "
        "do NOT skip this tool call, do NOT use shell, and do NOT retry if it is canceled or rejected. "
        "3) ONLY AFTER step 2 completes or fails, reply via harness_reply with three lines: "
        "WHOAMI=<actor from step 1>, TOOL_TRIED=write_file, OUTCOME=<allowed or rejected>."
    )
    conversation, request_id = send_probe(daemon, case, prompt)
    reply = wait_for_reply(daemon, conversation, request_id, timeout)
    if reply.get("messageId") != native_reply_id(request_id):
        raise AssertionError(f"permission-probe reply did not use harness_reply: got={reply.get('messageId')}")
    if target_path.exists():
        raise AssertionError(f"permission policy allowed a real file write: {target_path}")
    assert_permission_audit_evidence(root, case, target_path)
    assert_permission_transcript_evidence(root, case, target_path, actor_uri)
    text = reply_text(daemon, reply)
    lowered = text.lower()
    whoami_line = next((line for line in text.splitlines() if "whoami=" in line.lower()), "")
    if actor_uri not in whoami_line:
        raise AssertionError(f"harness_whoami evidence missing: line={whoami_line!r} expected={actor_uri!r}")
    outcome_line = next((line for line in lowered.splitlines() if "outcome=" in line), lowered)
    if "outcome=allowed" in lowered or not any(word in outcome_line for word in ("reject", "denied", "not allowed", "refused", "cancel")):
        raise AssertionError(f"non-harness tool was not reported as rejected: {text[:300]!r}")
    ack_message(daemon, reply["messageId"])


def snapshot_tree(path: Path) -> dict[str, tuple[int, int, int]] | None:
    """(size, mtime_ns, mode) per relative path; None when the root is absent."""

    if not path.exists():
        return None
    snapshot: dict[str, tuple[int, int, int]] = {}
    for child in sorted(path.rglob("*")):
        relative = str(child.relative_to(path))
        try:
            metadata = child.lstat()
        except OSError:
            continue
        snapshot[relative] = (
            metadata.st_size,
            metadata.st_mtime_ns,
            stat.S_IMODE(metadata.st_mode),
        )
    return snapshot


def start_worker(
    daemon: IsolatedDaemon,
    case: WorkerCase,
    nonce: str,
    *extra: str,
    provider: tuple[str, str] | None = None,
) -> tuple[int, dict[str, Any] | None, str]:
    argv = [
        "agent", "start", "agy",
        "--name", case.name,
        "--nickname", case.nickname,
        "--cwd", str(case.cwd),
        "--headless",
    ]
    if provider is not None:
        argv.extend(("--provider", provider[0], "--model", provider[1]))
    argv.append("--json")
    argv.append("--")
    argv.extend(
        (
            "--append-system-prompt",
            f"AGY-ACP-LIFECYCLE integrity nonce {nonce}: end EVERY harness_reply "
            f"message with the exact token {nonce}.",
            *extra,
        )
    )
    return run_cli(daemon, *argv, timeout=180.0)


def stage_oauth_token(root: Path, source: Path) -> Path:
    """Copy the named token into the one-shot root once; never read contents."""

    staged_dir = root / "staged"
    staged_dir.mkdir(parents=True, exist_ok=True)
    staged = staged_dir / TOKEN_FILENAME
    if staged.exists():
        raise AssertionError(f"staged token already present: {staged}")
    shutil.copyfile(source, staged)
    os.chmod(staged, 0o600)
    if staged.is_symlink() or not staged.is_file() or staged.stat().st_size == 0:
        raise AssertionError("staged token copy is not a non-empty regular file")
    return staged
