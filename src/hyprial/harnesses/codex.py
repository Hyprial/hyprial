"""Codex PTY connector and app-server streaming client."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import signal
import socket
import struct
import subprocess
import sys as sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from hyprial import __version__
from hyprial.backoff import capped_exponential
from hyprial.contracts.ports import PortAdmission
from hyprial.daemon.api import HarnessDelivery, HarnessResultStatus
from hyprial.daemon.desired_state import HarnessLaunchSpec
from hyprial.transfer.container import wrap_worker_launch
from hyprial.log import Logger

from .common import (
    ConnectorOptions,
    HarnessStartError,
    PtyHarnessProcess,
    summarize_stderr,
)
from .codex_carrier_store import CodexCarrierStore
from .interactive_carrier_runtime import (
    FETCHED,
    FINAL_OBSERVED,
    TURN_STARTED,
    CarrierDeliverySnapshot,
    CarrierCommand,
    CarrierFetched,
    CarrierFinalObserved,
    CarrierLogRequested,
    CarrierReconcileError,
    CarrierRemoved,
    CarrierSettled,
    CarrierSettlementDeferred,
    CarrierTurnStarted,
    EnqueueTurnRequested,
    InteractiveCarrierRuntime,
    SupplyFinalRequested,
)
from .streaming import (
    TURN_IDLE_TIMEOUT_ENV,
    ProgressObservation,
    StreamingTurnProcess,
    TurnClientFactory,
    resolve_turn_timeout_seconds,
)
from .worker_channel import WorkerChannel
from .model_provider import codex_provider_configuration
from .owned_process import (
    PROCESS_FORCE_KILL_SECONDS,
    OwnedProcessGroup,
    darwin_group_has_live_members,
    linux_group_has_live_members,
    parse_linux_process_stat,
    process_birth_identity,
)
from hyprial.contracts import ipc_errors

STREAM_LIMIT_BYTES = 8 * 1024 * 1024
STDERR_TAIL_BYTES = 16 * 1024
MAX_PENDING_REQUESTS = 1024
#: thread/start and thread/resume are the app-server's session start, which on
#: hq measures 9-43 s (2026-09-05/06, bare app-server, three samples per run;
#: cards 87dc8276 / 54158b16) against the 15 s ordinary-RPC timeout that used to
#: bound them.  A timed-out session start made the harness respawn app-server
#: mid-launch -- three spawns per launch on a slow afternoon -- which is what
#: turned a 45 s E2E launch budget red and stalled a three-connector restore.
THREAD_START_TIMEOUT_SECONDS_DEFAULT = 60.0
#: The ordinary per-request deadline for a managed app-server RPC (initialize,
#: notifications, turn control).  Promoted to a named constant because the
#: startup budget below composes from it.
REQUEST_TIMEOUT_SECONDS_DEFAULT = 15.0
#: The launcher's ``wait_ready`` budget must cover everything
#: ``CodexAppServerClient.__aenter__`` does before it signals ready, and those
#: are two sequential stdio waits: the ``initialize`` request (bounded by
#: ``REQUEST_TIMEOUT_SECONDS_DEFAULT``) followed by a thread/start or
#: thread/resume (bounded by ``THREAD_START_TIMEOUT_SECONDS_DEFAULT``).  There is
#: NO unix-socket wait on this headless path -- the 30 s socket budget belongs to
#: the interactive ``CodexAppServer``, not here.  So the outer budget is those
#: two inner budgets plus a margin, never an independently chosen number: an
#: outer pinned at 20 s < a single measured thread/start (9-43 s) is exactly what
#: failed E2E-006 ``codex.restart-restore`` in controlled run 5091.
#:
#: The margin covers only the non-timeout overhead that sits outside those two
#: waits -- the process spawn, the birth-identity probe (itself bounded at 1.0 s
#: by ``_process_birth_identity``'s ps timeout), the ``initialized`` notify and
#: asyncio task scheduling -- a few seconds in practice.  15 s is conservative
#: slack so a healthy restore never trips the outer while either inner budget is
#: still live.
APP_SERVER_STARTUP_MARGIN_SECONDS = 15.0
APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT = (
    REQUEST_TIMEOUT_SECONDS_DEFAULT
    + THREAD_START_TIMEOUT_SECONDS_DEFAULT
    + APP_SERVER_STARTUP_MARGIN_SECONDS
)
PROCESS_EXIT_GRACE_SECONDS = 0.5
PROCESS_GROUP_TERM_SECONDS = 0.5
PROCESS_GROUP_KILL_SECONDS = 0.5
PROCESS_IO_DRAIN_SECONDS = 0.25
PROCESS_STOP_TIMEOUT_SECONDS = 3.0
PROCESS_FORCE_JOIN_SECONDS = 1.0

# Quiet-period report sensitivity (issue #277 ruling: liveness becomes the
# connector's job via steer probing, and NO timeout ever kills a turn --
# wall-clock caps killed working turns twice over, #270 for managed and
# half of #94 for interactive).  After this long without any NEW
# app-server notification correlated with the running turn, the client
# reports ``turn-stalled`` (and ``turn-resumed`` when activity returns);
# nothing is interrupted.  0 disables reporting.  The level state "thread
# status inProgress" never counts as activity.
MANAGED_TURN_IDLE_TIMEOUT_SECONDS = 900.0


class CodexAppServerRpcError(RuntimeError):
    """An error response returned by Codex app-server."""

    def __init__(
        self, message: str, *, code: int | None = None, data: object = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass(slots=True)
class _CodexSession:
    thread_id: str | None = None
    server_request_methods: list[str] | None = None


@dataclass(frozen=True, slots=True)
class _CodexTurnOutcome:
    result: str
    is_error: bool = False


def _bounded(text: object, *, fallback: str) -> str:
    value = str(text).strip() if text is not None else ""
    if not value:
        value = fallback
    return value if len(value) <= 200 else f"{value[:197]}..."


def _notification_turn_id(params: dict[str, object]) -> str | None:
    value = params.get("turnId")
    if isinstance(value, str):
        return value
    turn = params.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return turn["id"]
    return None


def _codex_item_summary(item: dict[str, object], *, started: bool) -> tuple[str, str | None, dict[str, object]]:
    item_type = _bounded(item.get("type"), fallback="item")
    tool_name: str | None = None
    if item_type == "mcpToolCall":
        server = item.get("server")
        tool = item.get("tool")
        tool_name = ".".join(
            part
            for part in (
                server if isinstance(server, str) else "",
                tool if isinstance(tool, str) else "",
            )
            if part
        ) or item_type
    elif item_type in {"dynamicToolCall", "collabAgentToolCall"}:
        tool = item.get("tool")
        tool_name = tool if isinstance(tool, str) and tool else item_type
    elif item_type in {"commandExecution", "webSearch"}:
        tool_name = item_type

    if started:
        if tool_name is not None:
            summary = f"calling {tool_name}"
        elif item_type == "reasoning":
            summary = "reasoning started"
        elif item_type == "contextCompaction":
            summary = "context compaction started"
        else:
            summary = f"{item_type} started"
    else:
        status = item.get("status")
        suffix = f" ({status})" if isinstance(status, str) and status else ""
        if tool_name is not None:
            summary = f"{tool_name} finished{suffix}"
        elif item_type == "reasoning":
            summary = "reasoning finished"
        elif item_type == "contextCompaction":
            summary = "context compaction finished"
        else:
            summary = f"{item_type} finished{suffix}"

    detail: dict[str, object] = {"itemType": item_type}
    for key in ("status", "durationMs", "exitCode"):
        value = item.get(key)
        if isinstance(value, str | int):
            detail[key] = value
    return _bounded(summary, fallback=item_type), tool_name, detail


def _codex_progress_observation(
    message: dict[str, object], *, thread_id: str, turn_id: str
) -> ProgressObservation | None:
    """Map Codex lifecycle notifications; every delta stream is out of scope."""

    method = message.get("method")
    params = message.get("params")
    if not isinstance(method, str) or not isinstance(params, dict):
        return None
    if params.get("threadId") != thread_id:
        return None
    if _notification_turn_id(params) != turn_id:
        return None

    if method == "turn/started":
        return ProgressObservation(
            phase="turn-start", summary="Codex turn started"
        )
    if method == "turn/completed":
        turn = params.get("turn")
        status = turn.get("status") if isinstance(turn, dict) else None
        return ProgressObservation(
            phase="turn-end",
            summary=(
                "Codex turn completed"
                if status == "completed"
                else f"Codex turn ended ({_bounded(status, fallback='unknown')})"
            ),
            terminal=True,
        )
    if method == "turn/plan/updated":
        plan = params.get("plan")
        steps = [item for item in plan if isinstance(item, dict)] if isinstance(plan, list) else []
        completed = sum(1 for item in steps if item.get("status") == "completed")
        current = next(
            (
                item.get("step")
                for item in steps
                if item.get("status") == "inProgress"
                and isinstance(item.get("step"), str)
            ),
            None,
        )
        return ProgressObservation(
            phase="message-segment",
            summary=(
                f"plan updated ({completed}/{len(steps)} complete)"
                + (f": {current}" if isinstance(current, str) else "")
            ),
            detail={
                "totalSteps": len(steps),
                "completedSteps": completed,
                **({"currentStep": current} if isinstance(current, str) else {}),
            },
        )
    if method in {"item/started", "item/completed"}:
        item = params.get("item")
        if not isinstance(item, dict):
            return None
        started = method == "item/started"
        summary, tool_name, detail = _codex_item_summary(item, started=started)
        item_type = item.get("type")
        if tool_name is not None:
            phase = "tool-call" if started else "tool-result"
        elif item_type == "reasoning":
            phase = "thinking"
        elif item_type == "contextCompaction":
            phase = "compaction"
        else:
            phase = "message-segment"
        return ProgressObservation(
            phase=phase,
            summary=summary,
            tool_call_id=(
                item.get("id") if isinstance(item.get("id"), str) else None
            ),
            tool_name=tool_name,
            detail=detail,
        )
    # Explicit route-B exclusions include item/agentMessage/delta,
    # item/reasoning/*Delta, item/plan/delta, and command/file output deltas.
    return None


_parse_linux_process_stat = parse_linux_process_stat
_process_birth_identity = process_birth_identity
_linux_group_has_live_members = linux_group_has_live_members
_darwin_group_has_live_members = darwin_group_has_live_members


class _OwnedProcessGroup(OwnedProcessGroup):
    """Compatibility surface for existing Codex callers and test seams."""

    def __init__(self) -> None:
        super().__init__(label="Codex app-server")

    @staticmethod
    def _process_birth_identity(pid: int) -> str | None:
        return _process_birth_identity(pid)

    @staticmethod
    def _linux_group_has_live_members(process_group_id: int) -> bool | None:
        return _linux_group_has_live_members(process_group_id)

    @staticmethod
    def _darwin_group_has_live_members(process_group_id: int) -> bool | None:
        return _darwin_group_has_live_members(process_group_id)


class CodexInteractiveAppServer:
    """Own one external Codex app-server plus its launch-time TUI probe.

    PR1 deliberately stops at lifecycle and registration.  Turn injection,
    durable inbox polling, and the per-thread FIFO belong to the PR2 carrier;
    this object only performs the initialize and launch-time thread discovery
    needed to register the attached TUI under a stable thread/session ref.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        cwd: Path,
        command: tuple[str, ...] = ("codex",),
        env: Mapping[str, str] | None = None,
        startup_timeout_seconds: float = 30.0,
        request_timeout_seconds: float = 15.0,
        config_args: tuple[str, ...] = (),
    ) -> None:
        self.socket_path = Path(socket_path)
        self.cwd = cwd
        self.command = command
        # ``-c key=value`` overrides for the app-server invocation; this is
        # codex's session-scoped injection surface (HYPRIAL_HOME plugin MCP
        # servers ride it), so it belongs to the server process that executes
        # tools, not to the remote TUI.
        self.config_args = tuple(config_args)
        self.env = None if env is None else {**env}
        self.startup_timeout_seconds = startup_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self._process: subprocess.Popen[Any] | None = None
        self._group = _OwnedProcessGroup()
        self._socket: _CodexUnixWebSocket | None = None
        # The interactive carrier reads turns from a worker thread while
        # stop()/interrupt() may issue a concurrent RPC from the pump loop.
        # The app-server socket is a single request/response stream, so keep
        # those operations serialized or responses can cross-wire.
        self._rpc_lock = threading.RLock()
        self._next_request_id = 1
        self._discovered_thread_id: str | None = None

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.poll() is None else None

    def start(self) -> None:
        self.socket_path.unlink(missing_ok=True)
        environment = None if self.env is None else {**os.environ, **self.env}
        self._process = subprocess.Popen(
            (
                *self.command,
                "app-server",
                *self.config_args,
                "--listen",
                f"unix://{self.socket_path}",
            ),
            cwd=self.cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            self._group.register(self._process.pid)
            deadline = time.monotonic() + self.startup_timeout_seconds
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise ConnectionError(
                        "Codex app-server exited before its Unix socket became ready"
                    )
                if self.socket_path.exists():
                    try:
                        self._socket = _CodexUnixWebSocket(
                            self.socket_path, timeout=self.request_timeout_seconds
                        )
                        self.request(
                            "initialize",
                            {
                                "clientInfo": {
                                    "name": "hyprial-codex-interactive",
                                    "version": __version__,
                                },
                                "capabilities": {"experimentalApi": True},
                            },
                        )
                        self.notify("initialized", {})
                        return
                    except (
                        OSError,
                        TimeoutError,
                        ConnectionError,
                        CodexAppServerRpcError,
                    ):
                        if self._socket is not None:
                            self._socket.close()
                            self._socket = None
                time.sleep(0.05)
            raise TimeoutError("timed out waiting for Codex app-server Unix socket")
        except BaseException:
            self.stop()
            raise

    def request(self, method: str, params: object = None) -> dict[str, object]:
        client = self._socket
        if client is None:
            raise ConnectionError("Codex interactive app-server is not connected")
        request_id = self._next_request_id
        self._next_request_id += 1
        with self._rpc_lock:
            response = client.request(method, params or {}, request_id)
        if "error" in response:
            error = response["error"]
            if isinstance(error, dict):
                raise CodexAppServerRpcError(
                    str(error.get("message") or f"Codex RPC {method} failed"),
                    code=error.get("code") if isinstance(error.get("code"), int) else None,
                    data=error,
                )
            raise CodexAppServerRpcError(f"Codex RPC {method} failed")
        result = response.get("result")
        if not isinstance(result, dict):
            raise ConnectionError(f"Codex RPC {method} returned no object result")
        return result

    def notify(self, method: str, params: object = None) -> None:
        if self._socket is None:
            raise ConnectionError("Codex interactive app-server is not connected")
        with self._rpc_lock:
            self._socket.send_json({"method": method, "params": params or {}})

    def discover_thread(self) -> tuple[str, str]:
        """Discover the TUI thread through the mandatory three-level ladder."""

        try:
            listed = self.request(
                "thread/list",
                {
                    "cwd": str(self.cwd.resolve()),
                    "limit": 50,
                    "sourceKinds": ["cli", "vscode", "appServer"],
                },
            )
        except CodexAppServerRpcError:
            # Older/experimental servers may omit this projection entirely;
            # the next loaded/list + thread/read levels remain authoritative.
            listed = {"data": []}
        candidates = self._listed_candidates(listed)
        if candidates:
            self._discovered_thread_id = str(candidates[0]["id"])
            return self._discovered_thread_id, "thread/list"

        loaded = self.request("thread/loaded/list", {})
        loaded_ids = [str(item) for item in loaded.get("data", [])]
        for thread_id in loaded_ids:
            try:
                read = self.request(
                    "thread/read", {"threadId": thread_id, "includeTurns": False}
                )
            except CodexAppServerRpcError:
                continue
            thread = read.get("thread")
            if self._is_candidate(thread):
                self._discovered_thread_id = thread_id
                return thread_id, "loaded/list + thread/read fallback"
        raise ConnectionError(
            "Codex interactive TUI thread was not discoverable through "
            "thread/list, loaded/list, or thread/read"
        )

    def start_turn(self, prompt: str) -> str:
        thread_id = self._discovered_thread_id
        if thread_id is None:
            raise ConnectionError("Codex interactive thread is not discovered")
        result = self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        turn = result.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise ConnectionError("Codex app-server did not return a turn ID")
        return turn_id

    def read_turn(self, turn_id: str) -> dict[str, object]:
        thread_id = self._discovered_thread_id
        if thread_id is None:
            raise ConnectionError("Codex interactive thread is not discovered")
        result = self.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        thread = result.get("thread")
        turns = thread.get("turns", []) if isinstance(thread, dict) else []
        for turn in turns if isinstance(turns, list) else []:
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                return turn
        raise CodexAppServerRpcError(
            f"Codex turn {turn_id!r} was not present in thread/read"
        )

    def interrupt_turn(self, turn_id: str) -> None:
        thread_id = self._discovered_thread_id
        if thread_id is None:
            return
        self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def _listed_candidates(self, result: dict[str, object]) -> list[dict[str, object]]:
        data = result.get("data")
        if not isinstance(data, list):
            raise ConnectionError("Codex thread/list returned invalid data")
        return [
            item
            for item in data
            if isinstance(item, dict) and self._is_candidate(item)
        ]

    def _is_candidate(self, thread: object) -> bool:
        if not isinstance(thread, dict):
            return False
        return (
            thread.get("cwd") == str(self.cwd.resolve())
            and thread.get("canAcceptDirectInput") is True
        )

    def stop(self) -> None:
        socket_client = self._socket
        self._socket = None
        if socket_client is not None:
            socket_client.close()
        process = self._process
        self._process = None
        if process is not None:
            if self._group.exists(process.pid):
                self._group.signal(process.pid, signal.SIGTERM)
                if not self._group._wait_gone(process.pid, PROCESS_GROUP_TERM_SECONDS):
                    self._group.signal(process.pid, signal.SIGKILL)
                    self._group._wait_gone(process.pid, PROCESS_GROUP_KILL_SECONDS)
            if process.poll() is None:
                try:
                    process.wait(timeout=PROCESS_GROUP_KILL_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=PROCESS_GROUP_KILL_SECONDS)
            self._group.release_if_gone(process.pid)
        self.socket_path.unlink(missing_ok=True)


class _CodexUnixWebSocket:
    """Minimal Unix-domain RFC 6455 client for Codex's local app-server."""

    def __init__(self, path: Path, *, timeout: float) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall(
            (
                "GET / HTTP/1.1\r\nHost: localhost\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        response = self._read_until(b"\r\n\r\n")
        if b"101" not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError("Codex app-server WebSocket handshake failed")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        )
        if expected not in response:
            raise ConnectionError("Codex app-server WebSocket accept mismatch")

    def send_json(self, value: dict[str, object]) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode()
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        length = len(masked)
        if length < 126:
            header = bytes([0x81, 0x80 | length])
        elif length < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", length)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", length)
        self.sock.sendall(header + mask + masked)

    def request(self, method: str, params: object, request_id: int) -> dict[str, object]:
        self.send_json({"method": method, "id": request_id, "params": params})
        while True:
            message = self.recv_json()
            if message.get("id") == request_id:
                return message
            if "method" in message and "id" in message:
                self.send_json(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "hyprial PR1 does not handle app-server server requests",
                        },
                    }
                )

    def recv_json(self) -> dict[str, object]:
        while True:
            first, second = self._recv_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if second & 0x80 else b""
            payload = bytearray(self._recv_exact(length))
            if mask:
                for index in range(length):
                    payload[index] ^= mask[index % 4]
            if opcode == 0x9:
                continue
            if opcode == 0x8:
                raise ConnectionError("Codex app-server WebSocket closed")
            if opcode == 0x1:
                value = json.loads(payload.decode())
                if not isinstance(value, dict):
                    raise ConnectionError("Codex app-server returned non-object JSON")
                return value

    def close(self) -> None:
        try:
            self.sock.sendall(bytes([0x88, 0x80, 0, 0, 0, 0]))
        except OSError:
            pass
        self.sock.close()

    def _read_until(self, marker: bytes) -> bytes:
        data = bytearray()
        while marker not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Codex app-server closed during handshake")
            data.extend(chunk)
        return bytes(data)

    def _recv_exact(self, length: int) -> bytes:
        data = bytearray()
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise ConnectionError("Codex app-server WebSocket closed unexpectedly")
            data.extend(chunk)
        return bytes(data)

def _option_values(args: tuple[str, ...], *options: str) -> list[str]:
    values: list[str] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value in options:
            if index + 1 >= len(args) or args[index + 1].startswith("-"):
                raise ValueError(f"Codex app-server argument {value!r} needs a value")
            values.append(args[index + 1])
            index += 2
            continue
        matched = next(
            (option for option in options if value.startswith(f"{option}=")), None
        )
        if matched is not None:
            option_value = value[len(matched) + 1 :]
            if not option_value:
                raise ValueError(
                    f"Codex app-server argument {matched!r} needs a value"
                )
            values.append(option_value)
        index += 1
    return values


def _thread_execution_params(args: tuple[str, ...]) -> dict[str, object]:
    """Translate Codex CLI execution-policy flags to app-server fields."""

    value_options = {
        "--model": ("--model", "-m"),
        "--ask-for-approval": ("--ask-for-approval", "-a"),
        "--sandbox": ("--sandbox", "-s"),
    }
    flags = {
        "--approve-for-me",
        "--dangerously-bypass-approvals-and-sandbox",
    }
    consumed: set[int] = set()
    parsed: dict[str, str] = {}
    index = 0
    while index < len(args):
        argument = args[index]
        matched_name = next(
            (
                name
                for name, spellings in value_options.items()
                if argument in spellings
                or any(argument.startswith(f"{spelling}=") for spelling in spellings)
            ),
            None,
        )
        if matched_name is not None:
            values = _option_values(args, *value_options[matched_name])
            if len(values) != 1:
                raise ValueError(
                    f"Codex app-server argument {matched_name!r} must appear once"
                )
            parsed[matched_name] = values[0]
            if "=" not in argument:
                consumed.update({index, index + 1})
                index += 2
            else:
                consumed.add(index)
                index += 1
            continue
        if argument in flags:
            consumed.add(index)
        index += 1
    unsupported = [value for i, value in enumerate(args) if i not in consumed]
    if unsupported:
        raise ValueError(
            "Codex app-server argument is not supported in managed mode: "
            + ", ".join(repr(value) for value in unsupported)
        )
    if "--approve-for-me" in args and "--ask-for-approval" in parsed:
        raise ValueError(
            "Codex app-server arguments '--approve-for-me' and "
            "'--ask-for-approval' cannot be combined"
        )
    if "--dangerously-bypass-approvals-and-sandbox" in args and (
        "--ask-for-approval" in parsed or "--sandbox" in parsed
    ):
        raise ValueError(
            "Codex app-server argument '--dangerously-bypass-approvals-and-sandbox' "
            "cannot be combined with approval or sandbox overrides"
        )

    params: dict[str, object] = {}
    model = parsed.get("--model")
    if model is not None:
        params["model"] = model
    approval = parsed.get("--ask-for-approval")
    if approval is not None:
        if approval not in {"untrusted", "on-request", "never"}:
            raise ValueError(
                f"Codex app-server argument '--ask-for-approval' has invalid value {approval!r}"
            )
        params["approvalPolicy"] = approval
    sandbox = parsed.get("--sandbox")
    if sandbox is not None:
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError(
                f"Codex app-server argument '--sandbox' has invalid value {sandbox!r}"
            )
        params["sandbox"] = sandbox
    if "--approve-for-me" in args:
        params.update(
            {"approvalsReviewer": "auto_review", "sandbox": "workspace-write"}
        )
    if "--dangerously-bypass-approvals-and-sandbox" in args:
        params.update(
            {"approvalPolicy": "never", "sandbox": "danger-full-access"}
        )
    return params


#: The thread-scoped MCP server name under which the worker channel is
#: registered.  It is also the ONLY server whose tool approvals the unattended
#: client grants (card 87dc8276): the tools behind it are hyprial's own.
HARNESS_BRIDGE_MCP_SERVER_NAME = "harness-bridge"


def _worker_channel_config(channel: WorkerChannel) -> dict[str, object]:
    """The channel's MCP server as a thread-scoped Codex config override.

    Codex app-server applies thread/start (and thread/resume) ``config``
    entries through its normal override stack, and per-thread MCP servers
    listed there are assembled for that thread (verified live against
    codex-cli 0.147.0: the injected server reports
    mcpServer/startupStatus/updated = ready, bound to the thread id).
    Codex infers the stdio transport from ``command`` and has no ``type``
    field, so the Claude-SDK-shaped key is dropped here.
    """

    server = {key: value for key, value in channel.mcp_server.items() if key != "type"}
    # `default_tools_approval_mode = "auto"` on this server: accepted and kept
    # by codex (2026-08-31 arm B, `codex mcp get`), and MEASURED INERT on the
    # app-server path 2026-09-05 (codex-cli 0.152.0, gpt-5.6-sol, E2E-006
    # codex case, rollouts kept): under `-a never` the model's harness_reply
    # call inside codex's exec runtime is refused ("MCP tool call requires
    # approval, but approval policy is never") with this key present; under
    # `-a on-request` the native reply succeeds with no prompt, with or
    # without this key.  The approval policy decides; this key does not
    # beat a global `never` and is not needed otherwise.  It stays because
    # the wire tests pin it by name and codex drops unknown keys silently
    # (2026-08-31 arm C), so its presence is at least verifiable.
    server["default_tools_approval_mode"] = "auto"
    # ``approvals_reviewer = "user"`` routes this thread's approvals to the
    # client (hyprial) instead of codex's auto-reviewer.  Measured 2026-09-06 (CI
    # #4971 rollouts): under ``-a on-request`` the global ``auto_review``
    # reviewer runs on the worker's model provider and, on DeepSeek, fails --
    # every harness_* call was rejected and the model replied nothing.  With
    # the reviewer set per thread, ChatGPT and DeepSeek workers behave alike
    # and the approval lands in ``_server_request_response`` below.
    return {
        "mcp_servers": {HARNESS_BRIDGE_MCP_SERVER_NAME: server},
        "approvals_reviewer": "user",
    }


def _git_metadata_roots(cwd: str | Path) -> list[str]:
    """The git metadata paths a commit in ``cwd`` needs to write.

    Codex's ``workspace-write`` sandbox keeps the ``.git`` directory at the
    top of a writable root READ-ONLY (measured 2026-09-14, codex-cli 0.153.4,
    macOS seatbelt; spec ``notes/spec-codex-worker-git-commit-2026-09-14.md``
    table A-E): a repository whose root is the sandbox cwd cannot create
    ``.git/index.lock`` (``Operation not permitted``), while the same
    repository nested one level down is fine -- and so is a repository whose
    ``.git`` is outside the writable root.  The fix is to add the
    repository's own git metadata to
    ``sandbox_workspace_write.writable_roots``.

    Only the worktree ROOT is affected; a cwd below the root already works
    (layout F, measured), so this returns ``[]`` unless ``.git`` sits
    directly at ``cwd``.  A plain clone's ``.git`` is a directory; a linked
    worktree's is a file whose real metadata lives at
    ``git rev-parse --git-dir`` (index/HEAD) and ``--git-common-dir``
    (objects/refs).  Both must be writable, so both are returned; a linked
    worktree needs the common dir for object writes even when its per-tree
    git dir is already writable.

    Paths are read with an absolute-format ``git rev-parse`` so the answer
    does not depend on the daemon's cwd.  A missing/unusable ``git`` falls
    back to ``<cwd>/.git`` rather than silently granting nothing.
    """

    root = Path(cwd)
    if not (root / ".git").exists():
        return []
    candidates: list[str] = []
    for arguments in (
        ("--absolute-git-dir",),
        ("--path-format=absolute", "--git-common-dir"),
    ):
        try:
            completed = subprocess.run(
                ("git", "-C", str(root), "rev-parse", *arguments),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        value = completed.stdout.strip()
        if completed.returncode == 0 and value:
            candidates.append(value)
    if not candidates:
        candidates.append(str((root / ".git").resolve()))
    roots: list[str] = []
    for candidate in candidates:
        if candidate not in roots:
            roots.append(candidate)
    return roots


def _sandbox_writable_roots_config(
    cwd: str | Path, execution: Mapping[str, object]
) -> dict[str, object] | None:
    """Thread-config fragment granting the repo's git metadata write access.

    Only meaningful under ``workspace-write``.  An explicitly non-workspace
    mode (``read-only`` or ``danger-full-access``) gets nothing -- the first
    is unwritable by design and the second needs no grant.  When the CLI did
    not name a sandbox the effective mode comes from the operator's codex
    config (this host sets ``sandbox_mode = "workspace-write"``), so the
    grant is added there too: under ``read-only`` it is inert, and omitting
    it would leave exactly the config-defaulted path broken.
    """

    sandbox = execution.get("sandbox")
    if sandbox is not None and sandbox != "workspace-write":
        return None
    roots = _git_metadata_roots(cwd)
    if not roots:
        return None
    return {"sandbox_workspace_write": {"writable_roots": roots}}


def _thread_config(
    cwd: str | Path,
    execution: Mapping[str, object],
    worker_channel: WorkerChannel | None,
) -> dict[str, object]:
    """Assemble the ``thread/start`` / ``thread/resume`` ``config`` block.

    Both overrides ride the ONE channel hyprial already uses for thread
    config (``mcp_servers`` + ``approvals_reviewer``), so there is no second
    configuration path to keep in sync.  An empty mapping is dropped by the
    caller so threads without either stay byte-identical on the wire.
    """

    config: dict[str, object] = {}
    if worker_channel is not None:
        config.update(_worker_channel_config(worker_channel))
    sandbox_roots = _sandbox_writable_roots_config(cwd, execution)
    if sandbox_roots is not None:
        config.update(sandbox_roots)
    return config


class CodexAppServerClient:
    """One Codex app-server subprocess speaking JSON-RPC over JSONL pipes."""

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        session_ref: str | None = None,
        session: _CodexSession | None = None,
        command: tuple[str, ...] = ("codex",),
        env: Mapping[str, str] | None = None,
        worker_channel: WorkerChannel | None = None,
        request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS_DEFAULT,
        thread_start_timeout_seconds: float = THREAD_START_TIMEOUT_SECONDS_DEFAULT,
        turn_idle_timeout_seconds: float | None = None,
        process_group: _OwnedProcessGroup | None = None,
        logger: Logger | None = None,
    ) -> None:
        self.spec = spec
        base_environment = {**os.environ, **(env or {})}
        provider_args, provider_environment = codex_provider_configuration(
            spec, base_environment
        )
        self.command = (*command, *provider_args, "app-server", "--stdio")
        self._session = session or _CodexSession(session_ref)
        self._worker_channel = worker_channel
        # Same daemon-bound identity the pi and claude carriers inject
        # (WorkerChannel.identity_environment): a codex worker's shell-outs to
        # ``hyprial`` must present the session binding to the fenced PAC write
        # methods, and the exec runtime's own subprocesses inherit the same
        # env -- the three harnesses stay isomorphic on this surface.
        identity_environment = (
            worker_channel.identity_environment() if worker_channel is not None else {}
        )
        if spec.containerized:
            if worker_channel is None:
                raise ValueError(
                    "containerized codex workers require a worker channel"
                )
            self.command = wrap_worker_launch(
                spec,
                inner_argv=self.command,
                env_delta={**(env or {}), **provider_environment, **identity_environment},
                state_dir=worker_channel.state_dir,
            )
            # Bare Docker ``-e KEY`` flags copy from this child environment;
            # values never enter argv or Docker error text.
            combined = {**(env or {}), **provider_environment, **identity_environment}
            self._env = combined or None
        else:
            combined = {
                **(env or {}),
                **provider_environment,
                **identity_environment,
            }
            self._env = combined or None
        self._request_timeout_seconds = request_timeout_seconds
        self._thread_start_timeout_seconds = thread_start_timeout_seconds
        # spec.turn_timeout_seconds is deliberately NOT read: the wall-clock
        # cap is retired (#277) and the persisted field is tolerated only so
        # existing desired-state files keep loading.
        self._turn_idle_timeout_seconds = resolve_turn_timeout_seconds(
            turn_idle_timeout_seconds
            if turn_idle_timeout_seconds is not None
            else spec.idle_timeout_seconds,
            default=MANAGED_TURN_IDLE_TIMEOUT_SECONDS,
            env_var=TURN_IDLE_TIMEOUT_ENV,
        )
        self._process: asyncio.subprocess.Process | None = None
        self._process_group = process_group or _OwnedProcessGroup()
        self._process_group_id: int | None = None
        self._logger = logger
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._exit_task: asyncio.Task[None] | None = None
        self._stderr_tail = bytearray()
        self._expected_stop = False
        self._exit_logged = False
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._notifications: asyncio.Queue[dict[str, object] | BaseException] = (
            asyncio.Queue()
        )
        self._next_request_id = 1
        self._active_turn_id: str | None = None

    @property
    def session_ref(self) -> str | None:
        return self._session.thread_id

    @property
    def server_request_methods(self) -> tuple[str, ...]:
        return tuple(self._session.server_request_methods or ())

    @property
    def active_turn_id(self) -> str | None:
        return self._active_turn_id

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.returncode is None

    @property
    def pid(self) -> int | None:
        process = self._process
        if process is None or process.returncode is not None:
            return None
        return process.pid

    @property
    def exit_error(self) -> str | None:
        process = self._process
        if process is None or process.returncode is None:
            return None
        detail = summarize_stderr(bytes(self._stderr_tail))
        return f"Codex app-server exited with status {process.returncode}" + (
            f": {detail}" if detail else ""
        )

    async def __aenter__(self) -> Self:
        environment = None if self._env is None else {**os.environ, **self._env}
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.spec.cwd,
            env=environment,
            limit=STREAM_LIMIT_BYTES,
            start_new_session=True,
        )
        self._expected_stop = False
        self._exit_logged = False
        if self._logger is not None:
            self._logger.info("worker.started", pid=self._process.pid)
        try:
            self._process_group.register(self._process.pid)
        except ConnectionError:
            # register() already drained the rejected generation's PGID.
            await self._finish_rejected_process(self._process)
            await self._finish_rejected_stderr(self._process)
            self._write_exit_log(self._process, self._process.returncode)
            self._process = None
            raise
        except BaseException:
            # Registration can fail after start_new_session() has spawned
            # descendants.  Drain the new PGID first; leader-only cleanup
            # would orphan those descendants.
            self._process_group._close_unregistered_group(self._process.pid)
            await self._finish_rejected_process(self._process)
            await self._finish_rejected_stderr(self._process)
            self._write_exit_log(self._process, self._process.returncode)
            self._process = None
            raise
        self._process_group_id = self._process.pid
        assert self._process.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._exit_task = asyncio.create_task(self._watch_process_exit())
        self._reader_task = asyncio.create_task(self._read_loop())
        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "harness_bridge",
                        "title": "Harness Bridge",
                        "version": __version__,
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.notify("initialized", {})
            if self._session.thread_id is None:
                await self._start_thread()
            elif not await self._resume_thread():
                # The persisted thread is definitively gone (no rollout and
                # not loaded).  Resume must never become a startup failure
                # source: fall back to a cold start and let the new thread id
                # flow back to desired state through the session-ref sync.
                if self._logger is not None:
                    self._logger.info(
                        "worker.session_ref.lost",
                        threadId=self._session.thread_id,
                    )
                self._session.thread_id = None
                await self._start_thread()
            if self._logger is not None:
                self._logger.info("worker.ready", pid=self._process.pid)
            return self
        except BaseException:
            await self._close_process()
            raise

    async def _start_thread(self) -> None:
        execution = _thread_execution_params(self.spec.args)
        if self.spec.model is not None:
            execution["model"] = self.spec.model
        params: dict[str, object] = {
            "cwd": self._working_directory(),
            **execution,
        }
        config = _thread_config(
            self._working_directory(), execution, self._worker_channel
        )
        if config:
            params["config"] = config
        result = await self.request(
            "thread/start", params, timeout=self._thread_start_timeout_seconds
        )
        self._session.thread_id = _thread_id(result)

    async def _resume_thread(self) -> bool:
        """Resume the stored thread; False only when it is definitively gone.

        A transient resume failure still raises -- the reconnect loop retries
        it.  Only "no rollout found" combined with absence from the loaded
        list means the thread no longer exists, in which case the caller
        falls back to a cold start instead of failing the launch.
        """

        thread_id = self._require_thread_id()
        execution = _thread_execution_params(self.spec.args)
        if self.spec.model is not None:
            execution["model"] = self.spec.model
        params: dict[str, object] = {
            "threadId": thread_id,
            **execution,
        }
        # A reconnect rebuilds the thread config; without the override the
        # resumed thread would lose the harness tools AND the git writable
        # roots, silently reintroducing the very commit failure this change
        # exists to fix (the same door the pre-approval comment warns about).
        config = _thread_config(
            self._working_directory(), execution, self._worker_channel
        )
        if config:
            params["config"] = config
        try:
            result = await self.request(
                "thread/resume", params, timeout=self._thread_start_timeout_seconds
            )
        except CodexAppServerRpcError as error:
            if "no rollout found" not in str(error).lower():
                raise
            loaded = await self.request("thread/loaded/list", {})
            identifiers = loaded.get("data") if isinstance(loaded, dict) else None
            if not isinstance(identifiers, list) or thread_id not in identifiers:
                return False
            result = {"thread": {"id": thread_id}}
        resumed = _thread_id(result)
        if resumed != thread_id:
            raise ConnectionError(
                "Codex app-server resumed an unexpected thread "
                f"{resumed!r} instead of {thread_id!r}"
            )
        return True

    async def __aexit__(self, *args: object) -> bool:
        self._expected_stop = True
        await self._close_process()
        return False

    async def query(self, prompt: str) -> None:
        thread_id = self._require_thread_id()
        if self._active_turn_id is not None:
            raise ConnectionError("Codex app-server already has an active turn")
        result = await self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        if not isinstance(result, dict):
            raise ConnectionError("Codex app-server returned an invalid turn/start result")
        turn = result.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise ConnectionError("Codex app-server did not return a turn ID")
        self._active_turn_id = turn_id

    async def steer_turn(self, prompt: str) -> str:
        thread_id = self._require_thread_id()
        turn_id = self._active_turn_id
        if turn_id is None:
            raise ConnectionError("Codex app-server has no active turn to steer")
        result = await self.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        steered = result.get("turnId") if isinstance(result, dict) else None
        if not isinstance(steered, str) or not steered:
            raise ConnectionError("Codex app-server did not return a steered turn ID")
        self._active_turn_id = steered
        return steered

    async def receive_response(self) -> AsyncIterator[object]:
        turn_id = self._active_turn_id
        if turn_id is None:
            raise ConnectionError("Codex app-server turn was not started")
        try:
            completed: dict[str, object] | None = None
            async for item in self._turn_events(turn_id):
                if isinstance(item, ProgressObservation):
                    yield item
                    continue
                completed = item
                break
            if completed is None:
                raise ConnectionError(
                    f"Codex app-server ended turn {turn_id!r} without a completion"
                )
            status = completed.get("status")
            if status != "completed":
                yield _CodexTurnOutcome(
                    _turn_error(completed, turn_id), is_error=True
                )
                return
            turn = await self._read_turn(turn_id)
            if turn.get("status") != "completed" or turn.get("error") is not None:
                yield _CodexTurnOutcome(_turn_error(turn, turn_id), is_error=True)
                return
            reply = _final_reply(turn)
            if reply is None:
                yield _CodexTurnOutcome(
                    "Codex app-server returned no final agent message",
                    is_error=True,
                )
                return
            yield _CodexTurnOutcome(reply)
        finally:
            self._active_turn_id = None

    async def interrupt(self) -> None:
        turn_id = self._active_turn_id
        if turn_id is None:
            return
        await self.request(
            "turn/interrupt",
            {"threadId": self._require_thread_id(), "turnId": turn_id},
        )

    async def request(
        self, method: str, params: object = None, *, timeout: float | None = None
    ) -> object:
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            raise ConnectionError("too many Codex app-server requests are pending")
        self._require_running_process()
        request_id = self._next_request_id
        self._next_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {"method": method, "id": request_id, "params": params or {}}
            )
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=(
                    self._request_timeout_seconds if timeout is None else timeout
                ),
            )
        except TimeoutError as error:
            future.cancel()
            raise ConnectionError(
                f"timed out waiting for Codex app-server RPC {method}"
            ) from error
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: object = None) -> None:
        await self._write({"method": method, "params": params or {}})

    async def _turn_events(self, turn_id: str) -> AsyncIterator[object]:
        """Yield correlated coarse progress, then the completed turn object."""

        # Producer-local quiet-period watch (#277: no timeout ever kills a
        # turn; liveness will be the connector's job via steer probing).
        # Activity is ONLY a new app-server notification correlated with
        # this turn, observed here in the client -- before the route-C
        # progress channel's drop-oldest queue and coalescing, whose silence
        # therefore never means anything.  Crossing the threshold reports
        # ``turn-stalled``; a report re-fires every further threshold of
        # continued silence so the signal survives log tails and cannot be
        # missed, and the first new correlated notification reports
        # ``turn-resumed`` and clears the condition.
        threshold = self._turn_idle_timeout_seconds
        last_activity = asyncio.get_running_loop().time()
        next_stall_report = last_activity + threshold
        stalled = False
        while True:
            now = asyncio.get_running_loop().time()
            if now >= next_stall_report:
                stalled = True
                next_stall_report = now + threshold
                quiet = now - last_activity
                yield ProgressObservation(
                    phase="turn-stalled",
                    summary=(
                        f"Codex turn quiet for {quiet:.0f}s (no correlated "
                        f"app-server activity; sensitivity {threshold:g}s)"
                    ),
                    detail={
                        "quietSeconds": round(quiet, 3),
                        "thresholdSeconds": threshold,
                        "detector": "producer-quiet-watch",
                    },
                )
            try:
                message = await asyncio.wait_for(
                    self._notifications.get(), timeout=0.5
                )
            except TimeoutError:
                # This poll recovers a dropped terminal notification.  Its
                # "inProgress" answer is level state that can be stuck
                # forever, so it never counts as activity.
                try:
                    polled = await self._read_turn(turn_id)
                except ConnectionError as error:
                    if "was not found" not in str(error):
                        raise
                else:
                    if polled.get("status") not in {None, "inProgress"}:
                        yield ProgressObservation(
                            phase="turn-end",
                            summary=(
                                "Codex turn completed"
                                if polled.get("status") == "completed"
                                else f"Codex turn ended ({polled.get('status')})"
                            ),
                            terminal=True,
                        )
                        yield polled
                        return
                continue
            if isinstance(message, BaseException):
                raise message
            params = message.get("params")
            if (
                isinstance(params, dict)
                and _notification_turn_id(params) == turn_id
            ):
                if stalled:
                    stalled = False
                    quiet = asyncio.get_running_loop().time() - last_activity
                    yield ProgressObservation(
                        phase="turn-resumed",
                        summary=(
                            f"Codex turn active again after {quiet:.0f}s quiet"
                        ),
                        detail={
                            "quietSeconds": round(quiet, 3),
                            "thresholdSeconds": threshold,
                            "detector": "producer-quiet-watch",
                        },
                    )
                last_activity = asyncio.get_running_loop().time()
                next_stall_report = last_activity + threshold
            if message.get("method") != "turn/completed":
                observation = _codex_progress_observation(
                    message,
                    thread_id=self._require_thread_id(),
                    turn_id=turn_id,
                )
                if observation is not None:
                    yield observation
                continue
            turn = params.get("turn") if isinstance(params, dict) else None
            if isinstance(turn, dict) and turn.get("id") == turn_id:
                observation = _codex_progress_observation(
                    message,
                    thread_id=self._require_thread_id(),
                    turn_id=turn_id,
                )
                if observation is not None:
                    yield observation
                yield turn
                return

    async def _read_turn(self, turn_id: str) -> dict[str, object]:
        thread_id = self._require_thread_id()
        result = await self.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        thread = result.get("thread") if isinstance(result, dict) else None
        turns = thread.get("turns") if isinstance(thread, dict) else None
        if isinstance(turns, list):
            for candidate in turns:
                if isinstance(candidate, dict) and candidate.get("id") == turn_id:
                    return candidate
        raise ConnectionError(
            f"completed Codex turn {turn_id!r} was not found in thread {thread_id!r}"
        )

    async def _write(self, message: dict[str, object]) -> None:
        process = self._require_running_process()
        assert process.stdin is not None
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        async with self._write_lock:
            process.stdin.write(payload)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise ConnectionError("Codex app-server input closed") from error

    async def _read_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        failure: BaseException
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError as error:
                    raise ConnectionError(
                        "Codex app-server emitted an oversized line"
                    ) from error
                if not line:
                    detail = self._stderr_tail.decode("utf-8", errors="replace")[-4096:]
                    raise ConnectionError(
                        "Codex app-server exited unexpectedly"
                        + (f": {detail}" if detail else "")
                    )
                try:
                    message = json.loads(line.rstrip(b"\r\n"))
                except json.JSONDecodeError as error:
                    raise ConnectionError(
                        "Codex app-server emitted invalid JSON"
                    ) from error
                if not isinstance(message, dict):
                    raise ConnectionError("Codex app-server message must be an object")
                method = message.get("method")
                identifier = message.get("id")
                if isinstance(method, str) and identifier is not None:
                    if self._session.server_request_methods is None:
                        self._session.server_request_methods = []
                    self._session.server_request_methods.append(method)
                    answer = _server_request_response(
                        identifier, method, message.get("params")
                    )
                    failure = answer.get("error")
                    if (
                        self._logger is not None
                        and isinstance(failure, dict)
                        and failure.get("code") == -32601
                    ):
                        # An UNKNOWN server request is answered fail-closed
                        # only by a transport error; name it in the worker
                        # log so a newly introduced request that could leave
                        # the turn waiting is observable instead of silent.
                        self._logger.error(
                            "codex.server_request.unsupported", method=method
                        )
                    await self._write(answer)
                    continue
                if isinstance(identifier, int):
                    future = self._pending.get(identifier)
                    if future is None or future.done():
                        continue
                    rpc_error = message.get("error")
                    if isinstance(rpc_error, dict):
                        error_message = rpc_error.get("message")
                        code = rpc_error.get("code")
                        future.set_exception(
                            CodexAppServerRpcError(
                                str(error_message or "Codex app-server RPC failed"),
                                code=code if isinstance(code, int) else None,
                                data=rpc_error.get("data"),
                            )
                        )
                    else:
                        future.set_result(message.get("result"))
                    continue
                if isinstance(method, str):
                    await self._notifications.put(message)
        except asyncio.CancelledError:
            failure = ConnectionError("Codex app-server reader stopped")
        except BaseException as error:  # noqa: BLE001 - subprocess boundary
            failure = error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(failure)
        await self._notifications.put(failure)

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while chunk := await self._process.stderr.read(4096):
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > STDERR_TAIL_BYTES:
                del self._stderr_tail[: len(self._stderr_tail) - STDERR_TAIL_BYTES]

    async def _watch_process_exit(self) -> None:
        process = self._process
        if process is None:
            return
        returncode = await process.wait()
        if self._stderr_task is not None:
            await self._stderr_task
        if self._logger is not None:
            self._write_exit_log(process, returncode)

    def _write_exit_log(
        self, process: asyncio.subprocess.Process, returncode: int | None
    ) -> None:
        if self._logger is None or self._exit_logged or returncode is None:
            return
        self._exit_logged = True
        event = "worker.stopped" if self._expected_stop else "worker.exited"
        self._logger.log(
            "info" if self._expected_stop else "error",
            event,
            pid=process.pid,
            returnCode=returncode,
            stderrTail=summarize_stderr(bytes(self._stderr_tail)),
        )

    async def _close_process(self) -> None:
        process = self._process
        if process is None:
            return
        process_group_id = self._process_group_id
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=PROCESS_EXIT_GRACE_SECONDS
                )
            except TimeoutError:
                pass

        # The app-server owns a new process group.  Always drain that group:
        # the leader may exit cleanly on stdin EOF while a tool subprocess that
        # inherited its pipes remains alive.
        if process_group_id is not None and self._process_group.exists(process_group_id):
            self._process_group.signal(process_group_id, signal.SIGTERM)
            exited = await self._wait_for_process_group_exit(
                process_group_id, timeout=PROCESS_GROUP_TERM_SECONDS
            )
            if not exited:
                self._process_group.signal(process_group_id, signal.SIGKILL)
                await self._wait_for_process_group_exit(
                    process_group_id, timeout=PROCESS_GROUP_KILL_SECONDS
                )

        if process.returncode is None:
            if process_group_id is not None:
                self._process_group.signal(process_group_id, signal.SIGKILL)
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=PROCESS_GROUP_KILL_SECONDS
                )
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=PROCESS_GROUP_KILL_SECONDS
                    )
                except TimeoutError:
                    pass

        io_tasks = [
            task
            for task in (self._reader_task, self._stderr_task, self._exit_task)
            if task is not None
        ]
        if io_tasks:
            _done, pending = await asyncio.wait(
                io_tasks, timeout=PROCESS_IO_DRAIN_SECONDS
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*io_tasks, return_exceptions=True)
        self._write_exit_log(process, process.returncode)
        self._process = None
        if process_group_id is not None:
            self._process_group.release_if_gone(process_group_id)
        self._process_group_id = None
        self._reader_task = None
        self._stderr_task = None
        self._exit_task = None

    async def _finish_rejected_process(
        self, process: asyncio.subprocess.Process
    ) -> None:
        try:
            await asyncio.wait_for(
                process.wait(), timeout=PROCESS_FORCE_KILL_SECONDS
            )
        except TimeoutError:
            # Retry the entire PGID, never a leader-only kill.  If signalling
            # remains unavailable, retain the unresolved ownership marker so
            # forced-stop completion fails safe instead of claiming success.
            self._process_group._close_unregistered_group(process.pid)
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=PROCESS_FORCE_KILL_SECONDS
                )
            except TimeoutError:
                pass
        self._process_group.release_if_gone(process.pid)

    async def _finish_rejected_stderr(
        self, process: asyncio.subprocess.Process
    ) -> None:
        if process.stderr is None:
            return
        try:
            chunk = await asyncio.wait_for(
                process.stderr.read(), timeout=PROCESS_IO_DRAIN_SECONDS
            )
        except TimeoutError:
            return
        self._stderr_tail.extend(chunk)
        if len(self._stderr_tail) > STDERR_TAIL_BYTES:
            del self._stderr_tail[: len(self._stderr_tail) - STDERR_TAIL_BYTES]

    async def _wait_for_process_group_exit(
        self, process_group_id: int, *, timeout: float
    ) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while self._process_group.exists(process_group_id):
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.01)
        self._process_group.release_if_gone(process_group_id)
        return True

    def _working_directory(self) -> str:
        return str(Path(self.spec.cwd or os.getcwd()).resolve())

    def _require_running_process(self) -> asyncio.subprocess.Process:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise ConnectionError("Codex app-server is not running")
        return process

    def _require_thread_id(self) -> str:
        thread_id = self._session.thread_id
        if thread_id is None:
            raise ConnectionError("Codex app-server thread is not initialized")
        return thread_id


def _thread_id(result: object) -> str:
    thread = result.get("thread") if isinstance(result, dict) else None
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if not isinstance(thread_id, str) or not thread_id:
        raise ConnectionError("Codex app-server did not return a thread ID")
    return thread_id


def _server_request_response(
    identifier: object, method: str, params: object = None
) -> dict[str, object]:
    """Fail closed for unattended approvals while keeping the turn alive.

    One exception, deliberately narrow: an MCP tool-call approval for the
    worker's own harness-bridge server is granted.  Measured 2026-09-06
    (codex-cli 0.152.0, ``approvals_reviewer = "user"``): the request arrives
    as ``mcpServer/elicitation/request`` with ``serverName`` and
    ``_meta.codex_approval_kind == "mcp_tool_call"``; answering
    ``{"action": "accept", "content": {}}`` runs the tool, anything else --
    including the -32601 this client used to send -- is recorded by codex as
    "user rejected MCP tool call".  Every other elicitation is declined.

    Every server request that the codex 0.153.4 v2 protocol can send is
    answered with a SCHEMA-VALID fail-closed result (Schemas generated with
    ``codex app-server generate-json-schema`` on 2026-09-14):

    * approvals -> ``decline`` / an empty permission grant;
    * ``item/tool/requestUserInput`` -> ``{"answers": {}}`` (no user is
      attached; codex accepts the empty map and the model continues);
    * ``item/tool/call`` -> an unsuccessful result, so a dynamic tool the
      client does not implement fails the CALL instead of the transport.

    The -32601 fallback is deliberately kept only for methods this client
    has never seen; the read loop logs it (``codex.server_request.unsupported``)
    so a newly introduced request name is visible rather than silently
    treated as answered.
    """

    if method == "mcpServer/elicitation/request":
        request = params if isinstance(params, dict) else {}
        meta = request.get("_meta")
        kind = meta.get("codex_approval_kind") if isinstance(meta, dict) else None
        if (
            kind == "mcp_tool_call"
            and request.get("serverName") == HARNESS_BRIDGE_MCP_SERVER_NAME
        ):
            return {"id": identifier, "result": {"action": "accept", "content": {}}}
        return {"id": identifier, "result": {"action": "decline"}}
    if method in {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
    }:
        return {"id": identifier, "result": {"decision": "decline"}}
    if method == "item/permissions/requestApproval":
        return {
            "id": identifier,
            "result": {"permissions": {}, "scope": "turn"},
        }
    if method == "item/tool/requestUserInput":
        # No human is attached to an unattended worker.  The response shape
        # is required (``answers`` is not nullable), and an empty map is the
        # honest "no answer available"; measured 2026-09-14, codex completes
        # the turn on it instead of leaving the blocking question open.
        return {"id": identifier, "result": {"answers": {}}}
    if method == "item/tool/call":
        return {
            "id": identifier,
            "result": {
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": (
                            "unattended hyprial worker has no client-side "
                            "dynamic tool implementation"
                        ),
                    }
                ],
                "success": False,
            },
        }
    if method in {"execCommandApproval", "applyPatchApproval"}:
        return {
            "id": identifier,
            "result": {
                "decision": {
                    "denied": {
                        "rejection": "unattended hyprial worker cannot grant approval"
                    }
                }
            },
        }
    return {
        "id": identifier,
        "error": {
            "code": -32601,
            "message": f"hyprial Codex app-server client does not support server request {method!r}",
        },
    }


def _turn_error(turn: dict[str, object], turn_id: str) -> str:
    error = turn.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return f"Codex turn {turn_id!r} ended with status {turn.get('status')!r}"


def _final_reply(turn: dict[str, object]) -> str | None:
    items = turn.get("items")
    if not isinstance(items, list):
        return None
    for item in reversed(items):
        if (
            isinstance(item, dict)
            and item.get("type") == "agentMessage"
            and isinstance(item.get("text"), str)
        ):
            return item["text"]
    return None


class CodexInteractiveTurnClient:
    """TurnClient adapter over the already-running external app-server."""

    def __init__(self, server: CodexInteractiveAppServer) -> None:
        self.server = server
        self._turn_id: str | None = None
        self._observation_lock = threading.Lock()
        self._reconciled_final: tuple[str, str] | None = None

    @property
    def turn_id(self) -> str | None:
        with self._observation_lock:
            return self._turn_id

    def supply_reconciled_final(self, turn_id: str, reply: str) -> None:
        """Release receive_response when the carrier's independent read won.

        The carrier and pump intentionally read the same authoritative
        ``thread/read`` projection.  Whichever observer sees the completed
        turn first records the final; this handoff prevents a transient pump
        RPC failure from leaving its delivery permanently in-flight.
        """

        with self._observation_lock:
            if self._turn_id == turn_id:
                self._reconciled_final = (turn_id, reply)

    @property
    def running(self) -> bool:
        return self.server.pid is not None

    @property
    def pid(self) -> int | None:
        return self.server.pid

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def query(self, prompt: str) -> None:
        if self._turn_id is not None:
            raise ConnectionError("Codex interactive app-server turn is busy")
        turn_id = await asyncio.to_thread(self.server.start_turn, prompt)
        with self._observation_lock:
            self._turn_id = turn_id
            self._reconciled_final = None

    async def receive_response(self) -> AsyncIterator[object]:
        turn_id = self._turn_id
        if turn_id is None:
            raise ConnectionError("Codex interactive turn was not started")
        # No wall-clock deadline: the interactive 900s cap killed live TUI
        # turns (half of #94), and #277 retires time-based kills entirely.
        # The turn ends when the app-server says so, or on interrupt.
        try:
            while True:
                with self._observation_lock:
                    reconciled = self._reconciled_final
                if reconciled is not None and reconciled[0] == turn_id:
                    yield _CodexTurnOutcome(reconciled[1])
                    return
                try:
                    turn = await asyncio.to_thread(self.server.read_turn, turn_id)
                except CodexAppServerRpcError:
                    await asyncio.sleep(0.5)
                    continue
                status = turn.get("status")
                reply = _final_reply(turn)
                final_phase = any(
                    isinstance(item, dict)
                    and item.get("type") == "agentMessage"
                    and item.get("phase") == "final_answer"
                    for item in turn.get("items", [])
                    if isinstance(turn.get("items"), list)
                )
                if status == "completed" or (reply is not None and final_phase):
                    if reply is None:
                        yield _CodexTurnOutcome(
                            "Codex app-server returned no final agent message",
                            is_error=True,
                        )
                    else:
                        yield _CodexTurnOutcome(reply)
                    return
                if status in {"failed", "interrupted"}:
                    yield _CodexTurnOutcome(_turn_error(turn, turn_id), is_error=True)
                    return
                await asyncio.sleep(0.5)
        finally:
            with self._observation_lock:
                self._turn_id = None
                self._reconciled_final = None

    async def interrupt(self) -> None:
        turn_id = self.turn_id
        if turn_id is not None:
            await asyncio.to_thread(self.server.interrupt_turn, turn_id)


class CodexInteractiveCarrier:
    """Settle daemon inbox deliveries through one attached Codex thread.

    The per-delivery state is intentionally separate from the shared turn
    pump.  The pump owns FIFO execution; this carrier owns the durable reply
    boundary and does not forget a final until the daemon proves that the
    original message was replied to and acknowledged.
    """

    def __init__(
        self,
        server: CodexInteractiveAppServer,
        *,
        actor: str,
        session_ref: str,
        cwd: Path,
        command: list[str],
        daemon_request: Callable[[str, dict[str, object]], dict[str, object]],
        state_path: Path,
        process_pid: int | None = None,
        process_identity: str | None = None,
        poll_seconds: float = 0.5,
        logger: Logger | None = None,
        settlement_retry_seconds: float | None = None,
    ) -> None:
        from .streaming import StreamingTurnProcess

        self.server = server
        self.actor = actor
        self.session_ref = session_ref
        self.cwd = cwd
        self.command = command
        self.daemon_request = daemon_request
        if (process_pid is None) != (process_identity is None):
            raise ValueError(
                "Codex carrier process pid and identity must be provided together"
            )
        self.process_pid = process_pid
        self.process_identity = process_identity
        self.poll_seconds = poll_seconds
        self.settlement_retry_seconds = (
            poll_seconds
            if settlement_retry_seconds is None
            else settlement_retry_seconds
        )
        if self.settlement_retry_seconds <= 0:
            raise ValueError("settlement retry interval must be positive")
        self.logger = logger
        self._store: CodexCarrierStore | None = CodexCarrierStore(state_path)
        self._stop = threading.Event()
        self._carrier_io_lock = threading.RLock()
        self._carrier_clients: dict[str, CodexInteractiveTurnClient] = {}
        self._carrier_fact_lock = threading.RLock()
        self._carrier_facts: dict[tuple[str, str], CarrierCommand] = {}
        # ``drain_effects()`` deliberately derives required effects from the
        # actor projection so an actor restart cannot lose them.  That also
        # means a FETCHED projection can yield the same enqueue effect on
        # consecutive poll iterations while the asynchronous TURN_STARTED or
        # FINAL_OBSERVED fact is still waiting in the actor mailbox.  The turn
        # pump releases its own admission as soon as a result is drained, so
        # without this process-local custody fence that short window can start
        # the same native prompt twice.  Keep one accepted enqueue per exact
        # actor generation/version; durable recovery still reissues FETCHED
        # work after a process restart.
        self._carrier_enqueued: set[tuple[str, int, int]] = set()
        self._carrier_settled_pending: set[str] = set()
        self._thread = threading.Thread(
            target=self._poll_main,
            name=f"hyprial-codex-interactive-carrier-{session_ref[:8]}",
            daemon=True,
        )
        self._pump = StreamingTurnProcess(
            harness="codex",
            label="Codex interactive app-server",
            client_factory=lambda: CodexInteractiveTurnClient(server),
            thread_name=f"hyprial-codex-interactive-pump-{session_ref[:8]}",
            logger=logger,
            reconnect_delay_seconds=0.25,
            on_turn_started=self._on_turn_started,
        )
        assert self._store is not None
        self._carrier_runtime = InteractiveCarrierRuntime(
            name=session_ref,
            actor=actor,
            session_ref=session_ref,
            store=self._store,
        )
        self._carrier_fact_capacity = self._carrier_runtime.capacity

    @property
    def running(self) -> bool:
        return not self._stop.is_set() and self._pump.running

    @property
    def pid(self) -> int | None:
        return self.server.pid

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._pump.stop()
            self._thread.join(timeout=3.0)
        finally:
            if self._store is not None:
                self._retry_carrier_facts()
                self._carrier_runtime.drain(3.0)
                self._drain_carrier_effects()
                self._store.close()
                self._store = None

    def _signed(self, extra: dict[str, object] | None = None) -> dict[str, object]:
        return {
            "actor": self.actor,
            "sessionRef": self.session_ref,
            **(extra or {}),
        }

    def _poll_main(self) -> None:
        delay = self.poll_seconds
        while not self._stop.is_set():
            try:
                self._poll_once()
                delay = self.poll_seconds
            except Exception as error:  # noqa: BLE001 - carrier retry boundary
                if str(error).startswith(ipc_errors.SESSION_SUPERSEDED):
                    self._log(
                        "warn",
                        "worker.carrier.stopped",
                        stage="session-fence",
                        error=str(error),
                    )
                    return
                if str(error).startswith(ipc_errors.STALE_SESSION):
                    try:
                        self.daemon_request(
                            "session.register",
                            {
                                **self._signed(),
                                "cwd": str(self.cwd),
                                "command": self.command,
                                "source": "codex-app-server",
                                "runtime": "codex_interactive",
                                **(
                                    {
                                        "processPid": self.process_pid,
                                        "processIdentity": self.process_identity,
                                    }
                                    if self.process_pid is not None
                                    and self.process_identity is not None
                                    else {}
                                ),
                            },
                        )
                    except Exception as register_error:  # noqa: BLE001 - retry boundary
                        self._log(
                            "error",
                            "worker.carrier.error",
                            stage="session-register",
                            error=str(register_error),
                        )
                self._log(
                    "error",
                    "worker.carrier.error",
                    stage="poll",
                    error=str(error) or type(error).__name__,
                )
                delay = min(5.0, max(self.poll_seconds, delay * 2))
            self._stop.wait(delay)

    def _poll_once(self) -> None:
        self._retry_carrier_facts()
        self._refill_staged_fetched()
        fetched_versions = {
            (state.delivery.delivery_id, state.generation, state.version)
            for state in self._carrier_runtime.snapshots()
            if state.stage == FETCHED
        }
        self._carrier_enqueued.intersection_update(fetched_versions)
        self._carrier_settled_pending.intersection_update(
            state.delivery.delivery_id
            for state in self._carrier_runtime.snapshots()
        )
        self._drain_carrier_effects()
        observed = self.daemon_request(
            "message.pending.list", self._signed()
        ).get("messages", [])
        known = {
            state.delivery.delivery_id
            for state in self._carrier_runtime.snapshots()
        }
        if isinstance(observed, list) and any(
            isinstance(item, dict)
            and str(item.get("messageId")) not in known
            for item in observed
        ):
            fetched = self.daemon_request(
                "message.pending.list",
                self._signed({"fetched": True}),
            ).get("messages", [])
            for item in fetched if isinstance(fetched, list) else []:
                if not isinstance(item, dict):
                    continue
                message_id = item.get("messageId")
                text = item.get("message")
                if not isinstance(message_id, str) or not isinstance(text, str):
                    continue
                delivery = HarnessDelivery(
                    delivery_id=message_id,
                    conversation_id=str(item.get("conversationId") or message_id),
                    sender=str(item.get("from") or "unknown"),
                    recipient=self.actor,
                    message=text,
                )
                accepted = self._stage_carrier_fetched(
                    delivery,
                    str(item.get("intent") or "request"),
                )
                if not accepted:
                    self._log(
                        "error",
                        "worker.carrier.error",
                        stage="carrier-admission",
                        error="carrier actor deferred durable fetched fact",
                    )

        # Reconcile first: a completed authoritative thread/read must win over
        # a concurrent pump-side FAILED result caused by a transient RPC gap.
        self._reconcile_inflight_turns()

        for result in self._pump.drain_results():
            state = self._carrier_runtime.snapshot(result.delivery_id)
            if state is None:
                continue
            if result.status is HarnessResultStatus.COMPLETED:
                self._submit_carrier_fact(
                    CarrierFinalObserved(
                        generation=state.generation,
                        delivery_id=result.delivery_id,
                        output=result.output,
                    )
                )
            elif state.stage == FINAL_OBSERVED:
                self._log(
                    "warn",
                    "worker.carrier.error",
                    state=state,
                    stage="turn-result-after-reconcile",
                    error=result.error or result.status.value,
                )
            elif state.turn_id is not None:
                # A pump-side failure is not authoritative once turn/start
                # returned an id.  Keep the delivery pinned to that turn until
                # thread/read proves either a final or a terminal turn status.
                self._log(
                    "warn",
                    "worker.carrier.error",
                    state=state,
                    stage="turn-result-awaiting-reconcile",
                    error=result.error or result.status.value,
                )
            else:
                self._log(
                    "error",
                    "worker.carrier.error",
                    state=state,
                    stage="turn-result",
                    error=result.error or result.status.value,
                )
                self._submit_carrier_fact(
                    CarrierRemoved(
                        generation=state.generation,
                        delivery_id=result.delivery_id,
                    )
                )
                with self._carrier_io_lock:
                    self._carrier_clients.pop(result.delivery_id, None)

        self._settle_finals()
        self._drain_carrier_effects()

    def _on_turn_started(self, delivery: HarnessDelivery, client: object) -> None:
        if not isinstance(client, CodexInteractiveTurnClient):
            return
        turn_id = client.turn_id
        if turn_id is None:
            self._log(
                "error",
                "worker.carrier.error",
                stage="turn-start",
                error="Codex turn started without a turn id",
            )
            return
        state = self._carrier_runtime.snapshot(delivery.delivery_id)
        # Persist at the exact native turn/start boundary before the callback
        # returns.  A process crash after this line can reconcile thread/read
        # and must never create a second native turn.
        if self._store is not None:
            self._store.record_turn_started(self.actor, delivery.delivery_id, turn_id)
        with self._carrier_io_lock:
            self._carrier_clients[delivery.delivery_id] = client
        generation = (
            state.generation
            if state is not None
            else self._carrier_runtime.generation()
        )
        self._submit_carrier_fact(
            CarrierTurnStarted(
                generation=generation,
                delivery_id=delivery.delivery_id,
                turn_id=turn_id,
            )
        )

    def _reconcile_inflight_turns(self) -> None:
        inflight = tuple(
            state
            for state in self._carrier_runtime.snapshots()
            if state.stage == TURN_STARTED and state.turn_id is not None
        )
        for state in inflight:
            assert state.turn_id is not None
            try:
                turn = self.server.read_turn(state.turn_id)
            except CodexAppServerRpcError as error:
                detail = str(error) or type(error).__name__
                if state.last_reconcile_error != detail:
                    self._log(
                        "warn",
                        "worker.carrier.error",
                        state=state,
                        stage="turn-reconcile",
                        error=detail,
                    )
                    self._submit_carrier_fact(
                        CarrierReconcileError(
                            generation=state.generation,
                            delivery_id=state.delivery.delivery_id,
                            detail=detail,
                        )
                    )
                continue
            except Exception as error:  # noqa: BLE001 - independent read retry
                detail = str(error) or type(error).__name__
                if state.last_reconcile_error != detail:
                    self._log(
                        "error",
                        "worker.carrier.error",
                        state=state,
                        stage="turn-reconcile",
                        error=detail,
                    )
                    self._submit_carrier_fact(
                        CarrierReconcileError(
                            generation=state.generation,
                            delivery_id=state.delivery.delivery_id,
                            detail=detail,
                        )
                    )
                continue
            if state.last_reconcile_error is not None:
                self._submit_carrier_fact(
                    CarrierReconcileError(
                        generation=state.generation,
                        delivery_id=state.delivery.delivery_id,
                        detail=None,
                    )
                )
            reply = _final_reply(turn)
            final_phase = any(
                isinstance(item, dict)
                and item.get("type") == "agentMessage"
                and item.get("phase") == "final_answer"
                for item in turn.get("items", [])
                if isinstance(turn.get("items"), list)
            )
            if turn.get("status") == "completed" or (
                reply is not None and final_phase
            ):
                if reply is None:
                    self._log(
                        "error",
                        "worker.carrier.error",
                        state=state,
                        stage="turn-reconcile",
                        error="completed Codex turn had no final agent message",
                    )
                    continue
                self._submit_carrier_fact(
                    CarrierFinalObserved(
                        generation=state.generation,
                        delivery_id=state.delivery.delivery_id,
                        output=reply,
                    )
                )
            elif turn.get("status") in {"failed", "interrupted"}:
                self._log(
                    "error",
                    "worker.carrier.error",
                    state=state,
                    stage="turn-terminal",
                    error=_turn_error(turn, state.turn_id),
                )
                self._submit_carrier_fact(
                    CarrierRemoved(
                        generation=state.generation,
                        delivery_id=state.delivery.delivery_id,
                    )
                )
                with self._carrier_io_lock:
                    self._carrier_clients.pop(state.delivery.delivery_id, None)

    def _settle_finals(self) -> None:
        now = time.monotonic()
        ready = tuple(
            state
            for state in self._carrier_runtime.snapshots()
            if state.stage == FINAL_OBSERVED
            and state.final_output is not None
            and state.next_settlement_at <= now
            and state.delivery.delivery_id not in self._carrier_settled_pending
        )
        for state in ready:
            self._settle_final(state)

    def _settle_final(self, state: CarrierDeliverySnapshot) -> None:
        message_id = state.delivery.delivery_id
        if state.intent in {"reply", "event"}:
            method = "message.ack"
            params = self._signed({"messageId": message_id})
        else:
            method = "message.reply"
            params = self._signed(
                {"messageId": message_id, "message": state.final_output}
            )
        try:
            result = self.daemon_request(method, params)
        except Exception as error:  # noqa: BLE001 - settlement is retried
            self._defer_settlement(state, method=method, error=str(error))
            return
        acknowledged = result.get("acknowledged") is True
        replied = method == "message.ack" or result.get("replied") is True
        if not (replied and acknowledged):
            self._defer_settlement(
                state,
                method=method,
                error="daemon did not confirm durable settlement",
                result=result,
            )
            return
        store = self._store
        if store is None:
            raise RuntimeError("carrier store closed before remote settlement journal")
        store.record_remote_settled(self.actor, message_id)
        self._carrier_settled_pending.add(message_id)
        self._submit_carrier_fact(
            CarrierSettled(
                generation=state.generation,
                delivery_id=message_id,
            )
        )
        with self._carrier_io_lock:
            self._carrier_clients.pop(message_id, None)

    def _defer_settlement(
        self,
        state: CarrierDeliverySnapshot,
        *,
        method: str,
        error: str,
        result: dict[str, object] | None = None,
    ) -> None:
        attempts = state.settlement_attempts + 1
        delay = min(
            5.0,
            max(
                self.settlement_retry_seconds,
                capped_exponential(
                    self.settlement_retry_seconds, 5.0, attempts - 1
                ),
            ),
        )
        next_settlement_at = time.monotonic() + delay
        self._submit_carrier_fact(
            CarrierSettlementDeferred(
                generation=state.generation,
                delivery_id=state.delivery.delivery_id,
                attempts=attempts,
                next_settlement_at=next_settlement_at,
            )
        )
        self._log(
            "warn",
            "worker.carrier.settlement.pending",
            state=state,
            stage=method,
            attempt=attempts,
            retryInSeconds=delay,
            error=error or "unknown settlement error",
            **(
                {
                    "replied": result.get("replied"),
                    "acknowledged": result.get("acknowledged"),
                    "queued": result.get("queued"),
                }
                if result is not None
                else {}
            ),
        )

    def _drain_carrier_effects(self) -> None:
        for effect in self._carrier_runtime.drain_effects():
            if isinstance(effect, EnqueueTurnRequested):
                enqueue_key = (
                    effect.delivery.delivery_id,
                    effect.generation,
                    effect.version,
                )
                if enqueue_key in self._carrier_enqueued:
                    continue
                if not self._pump.enqueue(effect.delivery):
                    self._log(
                        "warn",
                        "worker.carrier.effect.deferred",
                        stage="turn-enqueue",
                        messageId=effect.delivery.delivery_id,
                    )
                else:
                    self._carrier_enqueued.add(enqueue_key)
            elif isinstance(effect, SupplyFinalRequested):
                with self._carrier_io_lock:
                    client = self._carrier_clients.get(effect.delivery_id)
                if client is not None:
                    client.supply_reconciled_final(effect.turn_id, effect.output)
            elif isinstance(effect, CarrierLogRequested):
                self._log(
                    effect.level,
                    effect.event,
                    state=effect.state,
                    **dict(effect.fields),
                )

    def _submit_carrier_fact(self, command: CarrierCommand) -> bool:
        admission = self._carrier_runtime.submit(command, timeout=0.05)
        if admission is PortAdmission.ACCEPTED:
            return True
        if admission is PortAdmission.CLOSING:
            self._log(
                "error",
                "worker.carrier.fact.rejected",
                stage=type(command).__name__,
                error="carrier actor is closing; durable store retains the fact",
            )
            return False
        with self._carrier_fact_lock:
            key = self._carrier_fact_key(command)
            if key in self._carrier_facts:
                self._carrier_facts[key] = command
                return False
            if len(self._carrier_facts) >= self._carrier_fact_capacity:
                self._log(
                    "warn",
                    "worker.carrier.fact.deferred",
                    stage=type(command).__name__,
                    error="carrier durable fact relay is full; store retains fact",
                )
                return False
            self._carrier_facts[key] = command
        self._log(
            "warn",
            "worker.carrier.fact.deferred",
            stage=type(command).__name__,
            retryInSeconds=self.poll_seconds,
        )
        return False

    def _retry_carrier_facts(self) -> None:
        while True:
            with self._carrier_fact_lock:
                item = next(iter(self._carrier_facts.items()), None)
                key, command = item if item is not None else (None, None)
            if command is None:
                return
            admission = self._carrier_runtime.submit(command, timeout=0.02)
            if admission is PortAdmission.OVERLOADED:
                return
            with self._carrier_fact_lock:
                if key is not None and self._carrier_facts.get(key) == command:
                    self._carrier_facts.pop(key, None)
            if admission is PortAdmission.CLOSING:
                self._log(
                    "error",
                    "worker.carrier.fact.rejected",
                    stage=type(command).__name__,
                    error="carrier actor closed before durable fact replay",
                )

    def _stage_carrier_fetched(
        self, delivery: HarnessDelivery, intent: str
    ) -> bool:
        store = self._store
        if store is None:
            raise RuntimeError("carrier store closed before fetched custody journal")
        # fetched=True has already transferred daemon custody. Persist the
        # complete prompt before actor admission so relay pressure or process
        # death cannot lose the only copy.
        store.record_fetched(
            self.actor,
            self.session_ref,
            delivery,
            intent,
        )
        return self._submit_carrier_fact(CarrierFetched(delivery, intent))

    def _refill_staged_fetched(self) -> None:
        store = self._store
        if store is None:
            return
        known = {
            state.delivery.delivery_id
            for state in self._carrier_runtime.snapshots()
        }
        with self._carrier_fact_lock:
            relayed = {
                key[1]
                for key in self._carrier_facts
                if key[0] == CarrierFetched.__name__
            }
        for stored in store.load(self.actor, self.session_ref):
            message_id = stored.delivery.delivery_id
            if (
                stored.state != "FETCHED"
                or not stored.delivery.message
                or message_id in known
                or message_id in relayed
            ):
                continue
            self._submit_carrier_fact(
                CarrierFetched(stored.delivery, stored.intent)
            )

    @staticmethod
    def _carrier_fact_key(command: CarrierCommand) -> tuple[str, str]:
        delivery_id = getattr(command, "delivery_id", None)
        if not isinstance(delivery_id, str):
            delivery = getattr(command, "delivery", None)
            delivery_id = getattr(delivery, "delivery_id", "")
        return type(command).__name__, str(delivery_id)

    def _log(
        self,
        level: str,
        event: str,
        *,
        state: CarrierDeliverySnapshot | None = None,
        **fields: object,
    ) -> None:
        logger = self.logger
        if logger is None:
            return
        try:
            emit = getattr(logger, level)
            emit(
                event,
                actorId=self.actor,
                sessionRef=self.session_ref,
                **(
                    {
                        "messageId": state.delivery.delivery_id,
                        "conversationId": state.delivery.conversation_id,
                        "deliveryState": state.stage,
                        **(
                            {"turnId": state.turn_id}
                            if state.turn_id is not None
                            else {}
                        ),
                    }
                    if state is not None
                    else {}
                ),
                **fields,
            )
        except (NameError, ImportError):
            raise
        except OSError:
            return


class CodexAppServerProcess(StreamingTurnProcess):
    """Serialize daemon deliveries through one persistent Codex thread."""

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        client_factory: TurnClientFactory | None = None,
        command: tuple[str, ...] = ("codex",),
        env: Mapping[str, str] | None = None,
        worker_channel: WorkerChannel | None = None,
        reconnect_delay_seconds: float = 0.25,
        reconnect_delay_max_seconds: float = 30.0,
        max_delivery_attempts: int = 5,
    ) -> None:
        if spec.harness != "codex" or not spec.headless:
            raise ValueError("Codex app-server requires a managed headless spec")
        if spec.endpoint is not None:
            raise ValueError("Codex app-server managed stdio does not accept an endpoint")
        self.spec = spec
        self._session = _CodexSession(spec.session_ref)
        self._process_group = _OwnedProcessGroup()
        self.worker_channel = worker_channel
        logger = (
            Logger.worker(worker_channel.state_dir, runtime="codex", name=spec.name)
            if worker_channel is not None
            else None
        )
        super().__init__(
            harness="codex",
            label="Codex app-server",
            client_factory=client_factory
            or (
                lambda: CodexAppServerClient(
                    spec,
                    session=self._session,
                    command=command,
                    env=env,
                    worker_channel=worker_channel,
                    process_group=self._process_group,
                    logger=logger,
                )
            ),
            thread_name=f"hyprial-codex-app-server-{spec.name}",
            logger=logger,
            reconnect_delay_seconds=reconnect_delay_seconds,
            reconnect_delay_max_seconds=reconnect_delay_max_seconds,
            max_delivery_attempts=max_delivery_attempts,
            stop_timeout_seconds=PROCESS_STOP_TIMEOUT_SECONDS,
            force_stop=self._process_group.force_close,
            force_stopped=self._process_group.stopped,
            force_stop_join_seconds=PROCESS_FORCE_JOIN_SECONDS,
            liveness_probe=self._process_group.liveness,
        )

    @property
    def session_ref(self) -> str | None:
        return self._session.thread_id

    @property
    def server_request_methods(self) -> tuple[str, ...]:
        return tuple(self._session.server_request_methods or ())


class CodexConnector:
    def __init__(self, options: ConnectorOptions | None = None) -> None:
        self.options = options or ConnectorOptions(("codex",))

    def build_argv(self, spec: HarnessLaunchSpec) -> tuple[str, ...]:
        if spec.harness != "codex":
            raise HarnessStartError(spec.harness, (), "Codex connector mismatch")
        provider_args, _provider_environment = codex_provider_configuration(
            spec, {**os.environ, **(self.options.env or {})}
        )
        model_args = ("--model", spec.model) if spec.model is not None else ()
        return (
            *spec.resolved_command(self.options.command),
            *provider_args,
            *model_args,
            *spec.args,
        )

    def launch(self, spec: HarnessLaunchSpec) -> PtyHarnessProcess:
        argv = self.build_argv(spec)
        _provider_args, provider_environment = codex_provider_configuration(
            spec, {**os.environ, **(self.options.env or {})}
        )
        environment = {**(self.options.env or {}), **provider_environment}
        return PtyHarnessProcess.spawn(
            "codex",
            argv,
            cwd=spec.cwd,
            env=environment or None,
            startup_probe_seconds=self.options.startup_probe_seconds,
            stop_grace_seconds=self.options.stop_grace_seconds,
        )
