"""Shared rig for the pi interactive attach carrier extension tests.

FakeDaemon speaks the real version-1 IPC over a unix socket. Carrier drives the
shipped ``pi_harness_attach.ts`` under plain node with a stubbed ExtensionAPI.
See the per-module test docstrings for what each side pins.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.harnesses.pi.rpc_client import PI_HARNESS_BRIDGE_EXTENSION

NODE = shutil.which("node")
ATTACH_EXTENSION = PI_HARNESS_BRIDGE_EXTENSION.with_name("pi_harness_attach.ts")
DAEMON_IPC = PI_HARNESS_BRIDGE_EXTENSION.with_name("pi_daemon_ipc.ts")
INBOX_WATCH = PI_HARNESS_BRIDGE_EXTENSION.with_name("pi_inbox_watch.ts")


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

SDK_AVAILABLE = NODE is not None and TYPEBOX is not None and TYPEBOX.is_dir()

ACTOR = "agent:op:node-1:attach-pi"
SESSION_REF = "attach-session-1"
POLL_MS = 40


# --------------------------------------------------------------------------- #
# Programmable fake daemon (real version-1 IPC over a unix socket)
# --------------------------------------------------------------------------- #
class FakeDaemon:
    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self.socket_path = state_dir / "daemon.sock"
        self.frames: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self.batch_text: str | None = None
        self._batch_counter = 0
        self._batch_members: dict[str, tuple[str, ...]] = {}
        self._batch_parents: dict[str, str | None] = {}
        self.epoch = "epoch-1"
        self.confirm_guard = True
        # Off: message.pending.wait answers "not held" at once.  On: hold like
        # the real daemon until an unseen id is pending or holdMs elapses.
        self.hold_waits = False
        # Seconds every fetched=true read is delayed (race windows).
        self.fetched_delay = 0.0
        # Seconds every message.reply answer is delayed (race windows).
        self.reply_delay = 0.0
        # method -> ([error codes to raise, one per call], then persist last)
        self._errors: dict[str, list[str]] = {}
        # Send-links contract (wp5c): settle_hint answers per id (default ack),
        # turn reports are deduplicated by (method, reportId) like the daemon.
        self.hints: dict[str, str] = {}
        self.received_at: list[float] = []
        self.turn_delay: dict[str, float] = {}
        # method -> number of upcoming calls that are ACCEPTED but whose
        # response is lost (connection closed without an answer).
        self.drop_response: dict[str, int] = {}
        self.accepted_turns: list[tuple[str, str, str | None]] = []
        # E1 current-run refresh frames, in arrival order (params only).
        self.accepted_refreshes: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        # A rebound path (daemon-restart scenarios) must not trip EADDRINUSE.
        self.socket_path.unlink(missing_ok=True)
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        self._server.listen(8)
        self._closed = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def fail_next(self, method: str, code: str, times: int = 1) -> None:
        with self._lock:
            self._errors.setdefault(method, []).extend([code] * times)

    def clear_errors(self, method: str) -> None:
        with self._lock:
            self._errors.pop(method, None)

    def _serve(self) -> None:
        while not self._closed:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handle, args=(connection,), daemon=True
            ).start()

    def _handle(self, connection: socket.socket) -> None:
        with connection:
            line = b""
            while b"\n" not in line:
                chunk = connection.recv(64 * 1024)
                if not chunk:
                    return
                line += chunk
            frame = json.loads(line.partition(b"\n")[0])
            with self._lock:
                self.frames.append(frame)
                self.received_at.append(time.monotonic())
            answer = self._respond(frame)
            with self._lock:
                method = str(frame.get("method"))
                drop = self.drop_response.get(method, 0)
                if drop and "error" not in answer:
                    self.drop_response[method] = drop - 1
                    return
            connection.sendall(
                json.dumps(answer, separators=(",", ":")).encode() + b"\n"
            )

    def _respond(self, frame: dict[str, Any]) -> dict[str, Any]:
        request_id = frame.get("id")
        method = frame.get("method")
        params = frame.get("params") or {}
        if method == "message.reply" and self.reply_delay:
            time.sleep(self.reply_delay)
        if method in self.turn_delay:
            time.sleep(self.turn_delay[method])
        with self._lock:
            errors = self._errors.get(method)
            code = errors.pop(0) if errors else None
        if code is not None:
            return {
                "version": 1,
                "id": request_id,
                "error": {"code": code, "message": f"injected {code}"},
            }
        result: dict[str, Any]
        if method in {"session.register", "session.register_guarded"}:
            result = {
                "ok": True,
                "registered": True,
                "actor": params.get("actor"),
                "sessionRef": params.get("sessionRef"),
                "daemonEpoch": self.epoch,
                **(
                    {"liveIncumbentGuardApplied": True}
                    if method == "session.register_guarded" and self.confirm_guard
                    else {}
                ),
            }
        elif method in {"session.refresh", "session.heartbeat"}:
            result = {"ok": True, "daemonEpoch": self.epoch}
        elif method == "session.unregister":
            result = {"ok": True, "unregistered": True, "actor": params.get("actor")}
        elif method == "message.pending.list":
            with self._lock:
                messages = list(self.pending)
            if params.get("fetched") and self.fetched_delay:
                # The snapshot is taken first, then the answer is slow: a
                # message arriving meanwhile is absent from this read.
                time.sleep(self.fetched_delay)
            result = {"ok": True, "messages": messages, "daemonEpoch": self.epoch}
        elif method == "message.pending.batch":
            requested_cursor = params.get("cursor")
            after_cursor = params.get("afterCursor")
            with self._lock:
                pending = list(self.pending)
                if isinstance(requested_cursor, str):
                    cursor = requested_cursor
                    admitted = set(self._batch_members.get(cursor, ()))
                    messages = [m for m in pending if m["messageId"] in admitted]
                else:
                    excluded: set[str] = set()
                    ancestor = after_cursor if isinstance(after_cursor, str) else None
                    while ancestor is not None:
                        excluded.update(self._batch_members.get(ancestor, ()))
                        ancestor = self._batch_parents.get(ancestor)
                    messages = [m for m in pending if m["messageId"] not in excluded]
                    self._batch_counter += 1
                    cursor = f"cursor-pi-attach-{self._batch_counter}"
                    self._batch_members[cursor] = tuple(
                        str(m["messageId"]) for m in messages
                    )
                    self._batch_parents[cursor] = (
                        after_cursor if isinstance(after_cursor, str) else None
                    )
            delivery_text = self.batch_text or (
                f"[Fake daemon inbox batch cursor={cursor}]\n"
                + "\n".join(str(m["messageId"]) for m in messages)
            )
            result = {
                "ok": True,
                "schemaVersion": 1,
                "format": "inbox-batch-v1",
                "cursor": cursor,
                "daemonEpoch": self.epoch,
                "actor": ACTOR,
                "itemCount": len(messages),
                "totalBodyBytes": sum(m["bodyBytes"] for m in messages),
                "inlineLimitBytes": 16384,
                "items": messages,
                "delivery": {
                    "mode": "inline",
                    "text": delivery_text,
                    "spillPath": None,
                    "spillBytes": None,
                    "spillSha256": None,
                },
            }
        elif method == "message.pending.wait":
            held = False
            changed = False
            if self.hold_waits:
                known = set(params.get("knownMessageIds") or ())
                deadline = time.monotonic() + min(5.0, (params.get("holdMs") or 5000) / 1000)
                while True:
                    with self._lock:
                        changed = any(m["messageId"] not in known for m in self.pending)
                    if changed or time.monotonic() >= deadline or self._closed:
                        break
                    held = True
                    time.sleep(0.01)
            result = {
                "ok": True,
                "changed": changed,
                "held": held,
                "daemonEpoch": self.epoch,
            }
        elif method == "message.settle_hint":
            message_id = str(params.get("messageId"))
            result = {
                "ok": True,
                "messageId": message_id,
                "hint": self.hints.get(message_id, "ack"),
                "pending": True,
            }
        elif method in {"session.turn.started", "session.turn.ended"}:
            phase = "started" if method.endswith("started") else "ended"
            ids = (
                [params.get("reportId")]
                if phase == "started"
                else list(params.get("reportIds") or ())
            )
            with self._lock:
                for report_id in ids:
                    key = (phase, str(report_id), params.get("status"))
                    if not any(k[:2] == key[:2] for k in self.accepted_turns):
                        self.accepted_turns.append(key)
            result = {"ok": True}
        elif method == "session.turn.refresh":
            with self._lock:
                self.accepted_refreshes.append(dict(params))
            result = {
                "ok": True,
                "schemaVersion": 1,
                "applied": True,
                "resyncCleared": False,
            }
        elif method in {"message.reply", "message.ack"}:
            result = {
                "ok": True,
                "messageId": params.get("messageId"),
                "acknowledged": True,
            }
        else:
            return {
                "version": 1,
                "id": request_id,
                "error": {"code": "METHOD_NOT_FOUND", "message": str(method)},
            }
        return {"version": 1, "id": request_id, "result": result}

    def methods(self) -> list[str]:
        with self._lock:
            return [str(frame.get("method")) for frame in self.frames]

    def frames_for(self, method: str) -> list[dict[str, Any]]:
        with self._lock:
            return [f for f in self.frames if f.get("method") == method]

    def close(self) -> None:
        self._closed = True
        try:
            self._server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._server.close()
        self.socket_path.unlink(missing_ok=True)


BODY_SENTINEL = "BODY-SENTINEL"


def request_message(
    message_id: str, text: str, *, intent: str = "request"
) -> dict[str, Any]:
    """A pending row as the daemon lists it: summary-bearing, body still present.

    ``text`` is the summary.  The row still carries the body under ``message``
    (the pending.list shape is unchanged), so a carrier that reads it fails the
    sentinel assertions.
    """

    return {
        "messageId": message_id,
        "deliveryId": message_id,
        "conversationId": "conv-1",
        "from": "agent:op:node-1:hq",
        "intent": intent,
        "kind": "message",
        "summary": text,
        "bodyBytes": len(text.encode("utf-8")),
        "message": BODY_SENTINEL,
    }


# --------------------------------------------------------------------------- #
# Node driver: stub ExtensionAPI + stdin-steered pi event pump
# --------------------------------------------------------------------------- #
DRIVER = r"""
import factory from "./pi_harness_attach.ts";
import readline from "node:readline";

const tools = {};
const handlers = {};
const sent = [];
const notifications = [];

const pi = {
  registerTool(def) { tools[def.name] = def; },
  on(event, handler) { (handlers[event] ??= []).push(handler); },
  sendUserMessage(text, options) { sent.push({ text, options }); },
};

const ctx = {
  cwd: process.cwd(),
  mode: "tui",
  hasUI: true,
  // The carrier must NEVER branch on isIdle (the check races the async
  // send); a call here is a regression and fails the scenario loudly.
  isIdle() { throw new Error("isIdle must not be consulted"); },
  ui: { notify: (msg, type) => notifications.push({ msg, type }) },
  sessionManager: { getSessionId: () => "pi-session-1" },
};

factory(pi);

async function fire(event, payload) {
  for (const handler of handlers[event] ?? []) await handler(payload, ctx);
}

const DEFAULT_RUN = [
  { role: "user", content: [{ type: "text", text: "<injected>" }] },
  { role: "assistant", content: [{ type: "text", text: "final answer text" }] },
];

// Test hook: Date.now() runs `ms` behind real time for `forMs` real ms, so a
// timer provably fires "early" against a wall-clock deadline.
const realNow = Date.now.bind(Date);
let clockSkew = 0;
Date.now = () => realNow() - clockSkew;

const rl = readline.createInterface({ input: process.stdin });
for await (const line of rl) {
  const command = JSON.parse(line);
  try {
    if (command.cmd === "start") {
      await fire("session_start", { type: "session_start", reason: "startup" });
    } else if (command.cmd === "consumed") {
      const text = sent.length ? sent[sent.length - 1].text : null;
      if (text === null) throw new Error("no injection to consume");
      await fire("message_start", {
        type: "message_start",
        message: { role: "user", content: [{ type: "text", text }] },
      });
    } else if (command.cmd === "agent_start") {
      await fire("agent_start", { type: "agent_start" });
    } else if (command.cmd === "agent_end") {
      await fire("agent_end", {
        type: "agent_end",
        messages: command.messages ?? DEFAULT_RUN,
      });
    } else if (command.cmd === "settled") {
      await fire("agent_settled", { type: "agent_settled" });
    } else if (command.cmd === "skew_clock") {
      clockSkew = command.ms;
      setTimeout(() => { clockSkew = 0; }, command.forMs);
    } else if (command.cmd === "tool") {
      await tools[command.name].execute("call-1", command.args ?? {});
    } else if (command.cmd === "shutdown") {
      await fire("session_shutdown", { type: "session_shutdown" });
    } else if (command.cmd === "quit") {
      process.stdout.write(JSON.stringify({ ready: true, bye: true }) + "\n");
      process.exit(0);
    }
    process.stdout.write(
      JSON.stringify({
        ready: true,
        sent: sent.map((s) => ({ text: s.text, options: s.options ?? null })),
        notifications,
        tools: Object.keys(tools).sort(),
      }) + "\n",
    );
  } catch (error) {
    process.stdout.write(
      JSON.stringify({ ready: false, error: String(error) }) + "\n",
    );
  }
}
"""


class Carrier:
    """One running extension process plus its fake daemon."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        managed: bool = True,
        extra_env: dict[str, str] | None = None,
        daemon: FakeDaemon | None = None,
    ) -> None:
        # A second extension process (restart scenarios) shares the daemon.
        self._owns_daemon = daemon is None
        if daemon is None:
            self.state_dir = Path(tempfile.mkdtemp(prefix="hyprial-pi-attach-"))
            self.daemon = FakeDaemon(self.state_dir)
        else:
            self.daemon = daemon
            self.state_dir = daemon.socket_path.parent
        (tmp_path / "node_modules").mkdir(exist_ok=True)
        os.symlink(TYPEBOX, tmp_path / "node_modules" / "typebox")
        for source in (
            ATTACH_EXTENSION,
            PI_HARNESS_BRIDGE_EXTENSION,
            DAEMON_IPC,
            INBOX_WATCH,
        ):
            shutil.copy(source, tmp_path / source.name)
        (tmp_path / "driver.ts").write_text(DRIVER, encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "HYPRIAL_HOME": str(tmp_path),
            "HARNESS_STATE_DIR": str(self.state_dir),
            "HARNESS_SOCKET_PATH": str(self.daemon.socket_path),
            "HYPRIAL_NETWORK_ISOLATED": "1",
            "HYPRIAL_PI_ATTACH_POLL_MS": str(POLL_MS),
        }
        if managed:
            env["HYPRIAL_WORKER_ACTOR"] = ACTOR
            env["HYPRIAL_WORKER_SESSION_REF"] = SESSION_REF
        env.update(extra_env or {})
        self.process = subprocess.Popen(
            [NODE, "driver.ts"],
            cwd=tmp_path,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def command(self, cmd: dict[str, Any], *, timeout: float = 10.0) -> dict[str, Any]:
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(json.dumps(cmd) + "\n")
        self.process.stdin.flush()
        line = self._readline_with_timeout(timeout)
        return json.loads(line)

    def _readline_with_timeout(self, timeout: float) -> str:
        assert self.process.stdout is not None
        result: list[str] = []

        def read() -> None:
            result.append(self.process.stdout.readline())  # type: ignore[union-attr]

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            stderr = self.process.stderr.read() if self.process.stderr else ""
            raise TimeoutError(f"driver did not answer; stderr: {stderr[-2000:]}")
        return result[0]

    def wait_for_frames(
        self, method: str, count: int, *, timeout: float = 10.0
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frames = self.daemon.frames_for(method)
            if len(frames) >= count:
                return frames
            time.sleep(0.02)
        raise TimeoutError(
            f"expected {count} {method} frames, have {self.daemon.methods()}"
        )

    def stop(self) -> None:
        if self.process.poll() is None:
            try:
                self.command({"cmd": "quit"}, timeout=5.0)
            except Exception:
                self.process.kill()
        self.process.wait(timeout=5.0)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        if self._owns_daemon:
            self.daemon.close()
            shutil.rmtree(self.state_dir, ignore_errors=True)


@contextmanager
def carrier_for(tmp_path: Path):
    rig = Carrier(tmp_path)
    try:
        yield rig
    finally:
        rig.stop()


