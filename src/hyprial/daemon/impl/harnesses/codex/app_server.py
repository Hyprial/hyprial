"""Codex interactive app-server lifecycle and thread execution params."""
from __future__ import annotations


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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hyprial import __version__
from hyprial.identity import (
    whitelist_replacement_environment,
)
from hyprial.identity import (
    SharedCredentialBinding,
    validate_shared_credential_binding,
    validate_shared_credential_environment,
)

from hyprial.daemon.impl.harnesses.codex.native_env import (
    CodexAgentHomeError,
    CodexNativeLoadEvidence,
    _validate_codex_native_load,
    prepare_codex_runtime_roots,
)
from hyprial.daemon.impl.harnesses.codex.process import (
    PROCESS_GROUP_KILL_SECONDS,
    PROCESS_GROUP_TERM_SECONDS,
    REQUEST_TIMEOUT_SECONDS_DEFAULT,
    CodexAppServerRpcError,
    _OwnedProcessGroup,
    resolve_codex_executable,
)

#: thread/start and thread/resume are the app-server's session start, which on
#: hq measures 9-43 s (2026-09-05/06, bare app-server, three samples per run;
#: cards 87dc8276 / 54158b16) against the 15 s ordinary-RPC timeout that used to
#: bound them.  A timed-out session start made the harness respawn app-server
#: mid-launch -- three spawns per launch on a slow afternoon -- which is what
#: turned a 45 s E2E launch budget red and stalled a three-connector restore.
THREAD_START_TIMEOUT_SECONDS_DEFAULT = 60.0

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
        model_provider: str | None = None,
        projection_root: Path | None = None,
        native_root: Path | None = None,
        session_root: Path | None = None,
        shared_credential: SharedCredentialBinding | None = None,
        authority_prepared: bool = False,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.cwd = cwd
        # The command is launched as given (argv[0] matters to codex's own
        # arg0 dispatch); only the P2 inventory needs its resolved executable.
        self.command = command
        self.model_provider = model_provider
        # ``-c key=value`` overrides for the app-server invocation; this is
        # codex's session-scoped injection surface (HYPRIAL_HOME plugin MCP
        # servers ride it), so it belongs to the server process that executes
        # tools, not to the remote TUI.
        self.env = None if env is None else {**env}
        roots = (projection_root, native_root, session_root)
        if any(root is not None for root in roots) and not all(
            root is not None for root in roots
        ):
            raise CodexAgentHomeError(
                "interactive Codex requires projection, native, and session roots together"
            )
        self._native_root = Path(native_root) if native_root is not None else None
        self._session_root = Path(session_root) if session_root is not None else None
        self._shared_credential = shared_credential
        if shared_credential is not None and self._native_root is None:
            raise CodexAgentHomeError(
                "interactive Codex shared credential requires P2 runtime roots"
            )
        if self._native_root is not None:
            assert projection_root is not None and self._session_root is not None
            if self.env is None or self.env.get("CODEX_HOME") != str(self._native_root):
                raise CodexAgentHomeError(
                    "interactive environment disagrees with resolved Codex native root"
                )
            if shared_credential is not None:
                validate_shared_credential_binding(shared_credential)
                validate_shared_credential_environment(
                    shared_credential, self.env or {}
                )
            launch_environment = whitelist_replacement_environment(os.environ, self.env)
            prepare_codex_runtime_roots(
                projection_root=Path(projection_root),
                native_root=self._native_root,
                session_root=self._session_root,
                # Resolved only if the native root holds an arg0 alias.
                codex_executable=lambda: resolve_codex_executable(
                    launch_environment, command[0]
                ),
                read_only=authority_prepared,
            )
        self.config_args = tuple(config_args)
        if self._native_root is not None:
            self.config_args = (
                *self.config_args,
                "-c",
                'shell_environment_policy.inherit="all"',
                "-c",
                "allow_login_shell=false",
            )
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
        self._native_load_evidence: CodexNativeLoadEvidence | None = None

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.poll() is None else None

    @property
    def native_load_evidence(self) -> CodexNativeLoadEvidence | None:
        return self._native_load_evidence

    def start(self) -> None:
        self.socket_path.unlink(missing_ok=True)
        environment = (
            None
            if self.env is None
            else whitelist_replacement_environment(os.environ, self.env)
        )
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
                        initialize = self.request(
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
                        if self._native_root is not None:
                            config_read = self.request(
                                "config/read",
                                {"cwd": str(self.cwd), "includeLayers": True},
                            )
                            account_read = self.request("account/read", {})
                            skills_list = self.request(
                                "skills/list",
                                {"cwds": [str(self.cwd)], "forceReload": True},
                            )
                            self._native_load_evidence = _validate_codex_native_load(
                                initialize=initialize,
                                config_read=config_read,
                                account_read=account_read,
                                skills_list=skills_list,
                                native_root=self._native_root,
                                cwd=self.cwd,
                                model_provider=self.model_provider,
                                require_tool_profile=True,
                                shared_credential=self._shared_credential,
                            )
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
