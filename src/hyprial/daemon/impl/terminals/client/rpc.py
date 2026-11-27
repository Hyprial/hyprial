"""JSON-RPC client for a private local hc core."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from hyprial.kernel import connect_local_ipc

from .protocol import (
    CoreStatus,
    HcRpcError,
    HcTransportError,
    HelloResult,
    SubscribeSnapshot,
    TerminalRecord,
    parse_core_status,
    parse_hello,
    parse_snapshot,
    parse_terminal,
)

_NAME_PATTERN = re.compile(r"^[a-z0-9._-]{1,32}$")


class HcClient:
    """A small request-per-connection client for the hc plain wire mode."""

    def __init__(self, socket_path: Path, timeout: float = 5.0) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self._request_ids = itertools.count(1)
        self._request_lock = threading.Lock()

    def hello(self) -> HelloResult:
        value = self._request("hello", {"generation": 1, "framed": False})
        return parse_hello(value)

    def server_status(self) -> CoreStatus:
        return parse_core_status(self._request("server.status", {}))

    def server_stop(self) -> Mapping[str, Any]:
        return self._request("server.stop", {})

    def terminal_run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        labels: Mapping[str, str] | None = None,
        name: str | None = None,
        keep: bool = False,
        no_parent: bool = True,
        no_notify: bool = True,
        rm: bool = False,
        wait: bool = False,
        size: Mapping[str, int] | None = None,
    ) -> TerminalRecord:
        if any(not isinstance(item, str) for item in argv):
            raise ValueError("argv values must be strings")
        _validate_string_mapping(env, "env values", allow_empty=True)
        _validate_string_mapping(labels, "label values", allow_empty=True)
        if name is not None and (
            not isinstance(name, str) or _NAME_PATTERN.fullmatch(name) is None
        ):
            raise ValueError("name must contain only lowercase letters, digits, '.', '_' or '-'")
        params: dict[str, Any] = {
            "argv": list(argv),
            "keep": keep,
            "no_parent": no_parent,
            "no_notify": no_notify,
            "rm": rm,
            "wait": wait,
        }
        optional = {"cwd": cwd, "env": env, "labels": labels, "name": name, "size": size}
        params.update({key: value for key, value in optional.items() if value is not None})
        return parse_terminal(self._request("terminal.run", params))

    def terminal_list(
        self,
        *,
        all: bool = True,
        labels: Mapping[str, str] | None = None,
    ) -> tuple[TerminalRecord, ...]:
        _validate_string_mapping(labels, "label values")
        params: dict[str, Any] = {"all": all}
        if labels is not None:
            params["labels"] = dict(labels)
        value = self._request("terminal.list", params)
        return tuple(
            parse_terminal(item)
            for item in value.get("terminals", ())
            if isinstance(item, Mapping)
        )

    def terminal_get(self, terminal: str) -> TerminalRecord:
        return parse_terminal(self._request("terminal.get", {"terminal": terminal}))

    def terminal_kill(self, terminal: str) -> TerminalRecord:
        return parse_terminal(self._request("terminal.kill", {"terminal": terminal}))

    def terminal_rm(self, terminal: str) -> Mapping[str, Any]:
        return self._request("terminal.rm", {"terminal": terminal})

    def terminal_label(
        self, terminal: str, labels: Mapping[str, str]
    ) -> TerminalRecord:
        _validate_string_mapping(labels, "label values", allow_empty=True)
        return parse_terminal(
            self._request(
                "terminal.label", {"terminal": terminal, "labels": dict(labels)}
            )
        )

    def terminal_read(self, terminal: str, **params: Any) -> Mapping[str, Any]:
        return self._request("terminal.read", {"terminal": terminal, **params})

    def terminal_send(self, terminal: str, text: str) -> Mapping[str, Any]:
        return self._request("terminal.send", {"terminal": terminal, "text": text})

    def terminal_keys(self, terminal: str, keys: str) -> Mapping[str, Any]:
        return self._request("terminal.keys", {"terminal": terminal, "keys": keys})

    def subscribe(
        self,
        *,
        labels: Mapping[str, str] | None = None,
        read_timeout: float = 45.0,
    ) -> Any:
        from .events import HcEventSubscription

        return HcEventSubscription(self, labels=labels, read_timeout=read_timeout)

    def events_subscribe(
        self,
        *,
        labels: Mapping[str, str] | None = None,
        read_timeout: float = 45.0,
    ) -> Any:
        return self.subscribe(labels=labels, read_timeout=read_timeout)

    def _request(self, method: str, params: Mapping[str, Any]) -> Any:
        with self._request_lock:
            request_id = next(self._request_ids)
        with self._connect() as stream:
            self._send(stream, request_id, method, params)
            response = _decode_line(_read_line(stream, self.timeout))
        if response.get("id") != request_id:
            raise HcTransportError("hc returned a response for another request")
        if "error" in response:
            raise HcRpcError.from_response(response)
        if "result" not in response:
            raise HcTransportError("hc response has neither result nor error")
        result = response["result"]
        if not isinstance(result, Mapping):
            raise HcTransportError("hc response result is not an object")
        return result

    def _open_subscription(
        self, params: Mapping[str, Any]
    ) -> tuple[Any, SubscribeSnapshot, bytearray]:
        with self._request_lock:
            request_id = next(self._request_ids)
        stream = self._connect()
        pending = bytearray()
        try:
            self._send(stream, request_id, "events.subscribe", params)
            response = _decode_line(_read_line(stream, self.timeout, pending))
            if response.get("id") != request_id:
                raise HcTransportError("hc returned a response for another request")
            if "error" in response:
                raise HcRpcError.from_response(response)
            result = response.get("result")
            if not isinstance(result, Mapping):
                raise HcTransportError("hc subscription snapshot is not an object")
            return stream, parse_snapshot(result), pending
        except BaseException:
            stream.close()
            raise

    def _connect(self) -> Any:
        try:
            stream = connect_local_ipc(self.socket_path, self.timeout)
            stream.settimeout(self.timeout)
            return stream
        except (OSError, TimeoutError) as error:
            raise HcTransportError(str(error)) from error

    @staticmethod
    def _send(stream: Any, request_id: int, method: str, params: Mapping[str, Any]) -> None:
        frame = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params),
        }
        stream.sendall(json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode() + b"\n")


def probe_socket_alive(socket_path: Path, timeout: float = 1.0) -> bool:
    try:
        stream = connect_local_ipc(Path(socket_path), timeout)
    except (OSError, TimeoutError):
        return False
    try:
        stream.close()
    except OSError:
        pass
    return True


def _read_line(
    stream: Any, timeout: float, buffer: bytearray | None = None
) -> bytes:
    stream.settimeout(timeout)
    pending = buffer if buffer is not None else bytearray()
    while True:
        newline = pending.find(b"\n")
        if newline >= 0:
            line = bytes(pending[:newline])
            del pending[: newline + 1]
            return line
        try:
            chunk = stream.recv(64 * 1024)
        except (TimeoutError, OSError) as error:
            raise HcTransportError(str(error)) from error
        if not chunk:
            raise HcTransportError("hc closed the connection before replying")
        pending.extend(chunk)


def _decode_line(line: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError as error:
        raise HcTransportError("hc returned invalid JSON") from error
    if not isinstance(value, Mapping):
        raise HcTransportError("hc returned a non-object JSON frame")
    return value


def _validate_string_mapping(
    values: Mapping[str, str] | None,
    label: str,
    *,
    allow_empty: bool = False,
) -> None:
    if values is None:
        return
    if not isinstance(values, Mapping) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in values.items()
    ):
        raise ValueError(f"{label} must be strings")
    if not allow_empty and any(value == "" for value in values.values()):
        raise ValueError(f"{label} must be non-empty")
