"""Behavioural tests for the pi harness-bridge extension itself.

The extension is TypeScript and runs inside pi's extension runtime, so pytest
cannot import it directly.  Instead these tests:

* stand up a fake daemon on a temporary unix socket speaking the real
  version-1 newline-delimited JSON IPC (the same wire the MCP proxy speaks),
* load the shipped ``pi_harness_bridge.ts`` under plain ``node`` (>= 23 strips
  types natively) with a stubbed ``ExtensionAPI`` that captures the registered
  tools, and
* invoke the registered tools exactly the way the pi runtime would, asserting
  the daemon saw the worker's canonical actor + session ref and the tool
  mapped the daemon result faithfully.

Skipped when node or a pi installation (for its bundled typebox) is absent.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from hyprial.daemon import HOLD_REASONS
from hyprial.daemon.impl.harnesses.pi.rpc_client import PI_HARNESS_BRIDGE_EXTENSION

PI_DAEMON_IPC = PI_HARNESS_BRIDGE_EXTENSION.with_name("pi_daemon_ipc.ts")
BATCH_TEXT = "[Harness Network inbox batch cursor=pi-bridge]\nBYTE-EXACT-秘密"

NODE = shutil.which("node")


def _pi_package_root() -> Path | None:
    pi = shutil.which("pi")
    if pi is None:
        return None
    candidate = Path(pi).resolve()
    for parent in (candidate, *candidate.parents):
        manifest = parent / "package.json"
        if not manifest.is_file():
            continue
        try:
            name = json.loads(manifest.read_text(encoding="utf-8")).get("name")
        except json.JSONDecodeError:
            continue
        if name == "@earendil-works/pi-coding-agent":
            return parent
    return None


PI_ROOT = _pi_package_root()
TYPEBOX = PI_ROOT / "node_modules" / "typebox" if PI_ROOT else None

if os.environ.get("HYPRIAL_REQUIRE_PI_SDK") and (NODE is None or TYPEBOX is None or not TYPEBOX.is_dir()):
    # CI installs the pinned SDK; without it every test here skipped in every
    # run, so a missing one there is a broken guard, not a skip.
    raise RuntimeError("HYPRIAL_REQUIRE_PI_SDK is set but node or the pi SDK is missing")

pytestmark = pytest.mark.skipif(
    NODE is None or TYPEBOX is None or not TYPEBOX.is_dir(),
    reason="node and a pi installation (bundled typebox) are required",
)


class FakeDaemon:
    """A recording unix-socket daemon speaking version-1 IPC."""

    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self.socket_path = state_dir / "daemon.sock"
        self.frames: list[dict[str, object]] = []
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        self._server.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            with connection:
                line = b""
                while b"\n" not in line:
                    chunk = connection.recv(64 * 1024)
                    if not chunk:
                        break
                    line += chunk
                if not line.strip():
                    continue
                frame = json.loads(line.partition(b"\n")[0])
                self.frames.append(frame)
                connection.sendall(
                    json.dumps(self._respond(frame), separators=(",", ":")).encode()
                    + b"\n"
                )

    @staticmethod
    def _respond(frame: dict[str, object]) -> dict[str, object]:
        request_id = frame.get("id")
        method = frame.get("method")
        params = frame.get("params") or {}
        result: dict[str, object]
        if method == "identity.whoami":
            result = {
                "ok": True,
                "actor": params.get("actor"),
                "sessionRef": params.get("sessionRef"),
                "nodeId": "node-1",
                "sessionRegistered": True,
            }
        elif method == "message.send":
            result = {"ok": True, "deliveries": [{"to": params.get("to")}]}
        elif method == "message.pending.list":
            result = {
                "ok": True,
                "messages": [
                    {
                        "messageId": "m-1",
                        "deliveryId": "m-1",
                        "conversationId": "c-1",
                        "from": "hq-adjutant",
                        "intent": "request",
                        "message": "status?",
                    }
                ],
            }
        elif method == "message.pending.batch":
            result = {
                "ok": True,
                "schemaVersion": 1,
                "format": "inbox-batch-v1",
                "cursor": "cursor-pi-bridge",
                "daemonEpoch": "epoch-1",
                "actor": params.get("actor"),
                "itemCount": 1,
                "totalBodyBytes": 7,
                "inlineLimitBytes": 16384,
                "items": [{"messageId": "m-1", "deliveryId": "m-1"}],
                "delivery": {
                    "mode": "inline",
                    "text": BATCH_TEXT,
                    "spillPath": None,
                    "spillBytes": None,
                    "spillSha256": None,
                },
            }
        elif method == "progress.list":
            result = {"ok": True, "events": []}
        elif method in {"message.ack", "message.reply"}:
            result = {"ok": True, "messageId": params.get("messageId")}
        elif method == "message.read_body":
            result = {
                "ok": True,
                "messageId": params.get("messageId"),
                "body": "hello body",
                "bodyBytes": 10,
            }
        elif method == "targets":
            result = {"targets": []}
        else:
            return {
                "version": 1,
                "id": request_id,
                "error": {"code": "METHOD_NOT_FOUND", "message": str(method)},
            }
        return {"version": 1, "id": request_id, "result": result}

    def close(self) -> None:
        # close() alone does not wake another thread blocked in accept() on
        # Linux: that thread can still serve the next request after "down".
        # Establish the unavailable-daemon precondition before invoking Pi.
        if self._server.fileno() >= 0:
            try:
                self._server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._server.close()
        self._thread.join(timeout=1)
        assert not self._thread.is_alive(), "fake daemon listener did not stop"
        self.socket_path.unlink(missing_ok=True)


DRIVER = """
import factory from "./pi_harness_bridge.ts";

const tools = {};
const pi = { registerTool(def) { tools[def.name] = def; } };
factory(pi);

const [toolName, argsJson] = process.argv.slice(2);
const tool = tools[toolName];
if (!tool) {
  process.stdout.write(JSON.stringify({ missing: toolName, have: Object.keys(tools) }) + "\\n");
  process.exit(0);
}
const result = await tool.execute("call-1", JSON.parse(argsJson));
process.stdout.write(JSON.stringify(result) + "\\n");
"""


@pytest.fixture()
def rig(tmp_path: Path):
    # Unix socket paths are length-limited (104 bytes on macOS); the pytest
    # tmp_path is far deeper than that, so the fake daemon lives in /tmp.
    state_dir = Path(tempfile.mkdtemp(prefix="hyprial-pi-ext-"))
    daemon = FakeDaemon(state_dir)
    # Node resolves the extension's runtime `typebox` import from the copy's
    # own directory tree; link pi's bundled copy in.
    (tmp_path / "node_modules").mkdir()
    os.symlink(TYPEBOX, tmp_path / "node_modules" / "typebox")
    shutil.copy(PI_HARNESS_BRIDGE_EXTENSION, tmp_path / "pi_harness_bridge.ts")
    # The bridge imports the shared daemon IPC wire by relative path.
    shutil.copy(PI_DAEMON_IPC, tmp_path / "pi_daemon_ipc.ts")
    (tmp_path / "driver.ts").write_text(DRIVER, encoding="utf-8")

    def invoke(
        tool: str, args: dict[str, object], *, env: dict[str, str] | None = None
    ) -> dict[str, object]:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "HYPRIAL_WORKER_ACTOR": "agent:allen:node-1:pi-worker",
            "HYPRIAL_WORKER_SESSION_REF": "sess-pi-1",
            "HYPRIAL_HOME": str(tmp_path),
            "HARNESS_STATE_DIR": str(state_dir),
            "HARNESS_SOCKET_PATH": str(daemon.socket_path),
            "HYPRIAL_NETWORK_ISOLATED": "1",
            **(env or {}),
        }
        completed = subprocess.run(
            [NODE, "driver.ts", tool, json.dumps(args)],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout.strip())

    yield daemon, invoke
    daemon.close()
    shutil.rmtree(state_dir, ignore_errors=True)


def _result_text(result: dict[str, object]) -> str:
    content = result["content"]
    assert isinstance(content, list)
    return str(content[0]["text"])  # type: ignore[index]


def test_extension_registers_the_harness_toolset(rig) -> None:  # type: ignore[no-untyped-def]
    _, invoke = rig
    probe = invoke("__missing__", {})
    assert set(probe["have"]) == {  # type: ignore[arg-type]
        "harness_ack",
        "harness_progress",
        "harness_read",
        "harness_read_body",
        "harness_reply",
        "harness_send",
        "harness_targets",
        "harness_whoami",
    }


def test_whoami_reports_the_worker_canonical_actor(rig) -> None:  # type: ignore[no-untyped-def]
    daemon, invoke = rig
    result = invoke("harness_whoami", {})

    assert "isError" not in result
    payload = json.loads(_result_text(result))
    assert payload["actor"] == "agent:allen:node-1:pi-worker"
    assert payload["sessionRegistered"] is True
    (frame,) = daemon.frames
    assert frame["version"] == 1
    assert frame["method"] == "identity.whoami"
    params = frame["params"]
    assert params["actor"] == "agent:allen:node-1:pi-worker"
    assert params["sessionRef"] == "sess-pi-1"


def test_send_wraps_one_target_and_carries_a_mutation_id(rig) -> None:  # type: ignore[no-untyped-def]
    daemon, invoke = rig
    result = invoke(
        "harness_send", {"to": "hq-adjutant", "summary": "done", "message": "done"}
    )

    assert "isError" not in result
    (frame,) = daemon.frames
    assert frame["method"] == "message.send"
    # A fresh request id doubles as the daemon's idempotency key.
    assert isinstance(frame["id"], str) and frame["id"]
    params = frame["params"]
    # The tool contract is one explicit target; the daemon wire is an array.
    assert params["to"] == ["hq-adjutant"]
    assert params["summary"] == "done"
    assert params["message"] == "done"


def test_send_accepts_an_array_target_and_carries_summary(rig) -> None:  # type: ignore[no-untyped-def]
    daemon, invoke = rig
    result = invoke(
        "harness_send",
        {
            "to": ["agent:a", "agent:b"],
            "summary": "fan-out summary",
            "message": "hello",
        },
    )

    assert "isError" not in result
    (frame,) = daemon.frames
    assert frame["method"] == "message.send"
    assert frame["params"]["to"] == ["agent:a", "agent:b"]
    assert frame["params"]["summary"] == "fan-out summary"


def test_read_body_reads_one_body_as_a_mutation(rig) -> None:  # type: ignore[no-untyped-def]
    daemon, invoke = rig
    result = invoke("harness_read_body", {"messageId": "m-1"})

    assert "isError" not in result
    (frame,) = daemon.frames
    assert frame["method"] == "message.read_body"
    assert frame["params"]["messageId"] == "m-1"
    assert isinstance(frame["id"], str) and frame["id"]
    assert json.loads(_result_text(result))["body"] == "hello body"


def test_read_returns_daemon_batch_without_rendering(rig) -> None:  # type: ignore[no-untyped-def]
    daemon, invoke = rig
    result = invoke("harness_read", {})

    payload = json.loads(_result_text(result))
    assert payload["delivery"]["text"] == BATCH_TEXT
    assert payload["items"][0]["messageId"] == "m-1"
    assert daemon.frames[0]["method"] == "message.pending.batch"
    assert "fetched" not in daemon.frames[0]["params"]


def test_progress_lists_events_without_fetching_terminal_messages(rig) -> None:  # type: ignore[no-untyped-def]
    daemon, invoke = rig
    result = invoke("harness_progress", {"deliveryId": "d-1", "sinceSeq": 2})

    assert "isError" not in result
    (frame,) = daemon.frames
    assert frame["method"] == "progress.list"
    assert frame["params"]["deliveryId"] == "d-1"
    assert frame["params"]["sinceSeq"] == 2
    assert "fetched" not in frame["params"]


def test_reply_and_ack_are_mutations(rig) -> None:  # type: ignore[no-untyped-def]
    daemon, invoke = rig
    reply = invoke(
        "harness_reply", {"messageId": "m-1", "summary": "ack'd", "message": "ack'd"}
    )
    ack = invoke("harness_ack", {"messageId": "m-1"})

    assert "isError" not in reply
    assert "isError" not in ack
    assert [frame["method"] for frame in daemon.frames] == [
        "message.reply",
        "message.ack",
    ]
    assert daemon.frames[0]["params"]["summary"] == "ack'd"
    assert all(frame["id"] for frame in daemon.frames)


def test_targets_rejects_an_invalid_kind_without_calling_the_daemon(
    rig,  # type: ignore[no-untyped-def]
) -> None:
    daemon, invoke = rig
    result = invoke("harness_targets", {"kind": "bogus"})

    assert result.get("isError") is True
    assert "must be agent, user, or channel_route" in _result_text(result)
    assert daemon.frames == []


def test_unmanaged_session_fails_closed(rig) -> None:  # type: ignore[no-untyped-def]
    daemon, invoke = rig
    env = {"HYPRIAL_WORKER_ACTOR": "", "HYPRIAL_WORKER_SESSION_REF": ""}
    result = invoke("harness_whoami", {}, env=env)

    assert result.get("isError") is True
    assert "not launched as a managed hyprial worker" in _result_text(result)
    assert daemon.frames == []


def test_daemon_down_is_a_clean_tool_error(rig) -> None:  # type: ignore[no-untyped-def]
    daemon, invoke = rig
    daemon.close()
    result = invoke("harness_send", {"to": "hq-adjutant", "summary": "hi", "message": "hi"})

    assert result.get("isError") is True
    assert "DAEMON_UNAVAILABLE" in _result_text(result)


@pytest.mark.parametrize(
    "target",
    [
        "user:allen",
        "user:someone-else",
        "agent:op:node-1:bob",
        "route:test-bot:allen-dm",
    ],
)
def test_send_keeps_the_daemon_verdict_for_every_target_kind(
    rig, monkeypatch, target
) -> None:
    daemon, invoke = rig
    receipt = {
        "ok": True,
        "messageId": "harness-send-1",
        "deliveries": [{
            "target": target, "accepted": True, "queued": False,
            "nativeMessageId": "om_synthetic_receipt",
        }],
    }
    monkeypatch.setattr(daemon, "_respond", lambda frame: {
        "version": 1, "id": frame["id"], "result": receipt,
    })

    result = invoke(
        "harness_send", {"to": target, "summary": "receipt probe", "message": "receipt probe"}
    )

    assert not result.get("isError", False)
    assert result["details"] == receipt
    assert json.loads(_result_text(result)) == receipt
    assert len(daemon.frames) == 1
    assert daemon.frames[0]["params"]["to"] == [target]
    # The tool never mints a link or self-authorizes on the caller's behalf.
    assert [frame["method"] for frame in daemon.frames] == ["message.send"]


@pytest.mark.parametrize("reason", sorted(HOLD_REASONS))
def test_a_hold_is_a_successful_single_call_result(rig, monkeypatch, reason) -> None:
    daemon, invoke = rig
    payload = {
        "ok": True,
        "queued": True,
        "holdReason": reason,
        "deliveries": [{
            "target": "agent:op:node-1:bob", "accepted": True,
            "queued": True, "holdReason": reason,
        }],
    }
    monkeypatch.setattr(daemon, "_respond", lambda frame: {
        "version": 1, "id": frame["id"], "result": payload,
    })

    result = invoke(
        "harness_send",
        {"to": "agent:op:node-1:bob", "summary": "s", "message": "hello"},
    )

    assert not result.get("isError", False)
    assert result["details"] == payload
    assert len(daemon.frames) == 1  # A hold never triggers a resend or a poll.


def test_unknown_send_preserves_admitted_custody_receipt(rig, monkeypatch) -> None:
    daemon, invoke = rig
    data = {
        "commandAdmission": "accepted",
        "outcomeKnown": False,
        "messageId": "unknown-message-1",
        "submissionCorrelationId": "inbox:submit:controlled",
        "idempotencyKey": "unknown-send-key",
    }
    monkeypatch.setattr(daemon, "_respond", lambda frame: {
        "version": 1, "id": frame["id"],
        "error": {
            "code": "SUBMIT_OUTCOME_UNKNOWN",
            "message": "submitted inbox command has no confirmed outcome",
            "data": data,
        },
    })

    result = invoke(
        "harness_send", {"to": "user:allen", "summary": "unknown probe", "message": "unknown probe"}
    )

    assert result.get("isError") is True
    assert result["details"]["code"] == "SUBMIT_OUTCOME_UNKNOWN"
    assert result["details"]["data"] == data
    # The model-visible text must retain the custody identity, not only details.
    assert "inbox:submit:controlled" in _result_text(result)
    assert "unknown-message-1" in _result_text(result)
    assert len(daemon.frames) == 1  # No automatic second send after uncertainty.


@pytest.mark.parametrize("tool,params,receipt", [
    (
        "harness_send",
        {"to": "route:test-bot:allen-dm", "summary": "probe", "message": "probe"},
        {"ok": False, "deliveries": [{
            "accepted": False, "queued": True, "code": "TARGET_UNRESOLVED",
        }]},
    ),
    (
        "harness_reply",
        {"messageId": "original-1", "summary": "reply probe", "message": "reply probe"},
        {
            "ok": False, "code": "REPLY_SETTLEMENT_PENDING",
            "messageId": "original-1", "replyMessageId": "stable-reply-1",
            "replied": False, "acknowledged": False, "queued": True,
            "retryable": True,
        },
    ),
])
def test_negative_application_receipt_is_a_tool_error(
    rig, monkeypatch, tool, params, receipt,
) -> None:
    daemon, invoke = rig
    monkeypatch.setattr(daemon, "_respond", lambda frame: {
        "version": 1, "id": frame["id"], "result": receipt,
    })

    result = invoke(tool, params)

    assert result.get("isError") is True
    assert result["details"] == receipt
    assert json.loads(_result_text(result)) == receipt
    assert len(daemon.frames) == 1
