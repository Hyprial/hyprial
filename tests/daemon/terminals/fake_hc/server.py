"""In-process wire-compatible hc server for daemon unit tests."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

from hyprial.kernel import listen_local_ipc


@dataclass
class _FakeTerminal:
    address: str
    name: str | None
    labels: dict[str, str]
    argv: tuple[str, ...]
    cwd: str
    keep: bool
    cols: int
    rows: int
    created_ms: int
    state: str = "live"
    pid: int | None = None
    exit_code: int | None = None
    signal: int | None = None
    reason: str | None = None
    status: dict[str, Any] | None = None
    status_ms: int | None = None
    finished_ms: int | None = None

    def wire(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "name": self.name,
            "labels": dict(self.labels),
            "argv": list(self.argv),
            "cwd": self.cwd,
            "state": self.state,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "signal": self.signal,
            "reason": self.reason,
            "status": dict(self.status) if self.status is not None else None,
            "status_ms": self.status_ms,
            "created_ms": self.created_ms,
            "finished_ms": self.finished_ms,
            "cols": self.cols,
            "rows": self.rows,
        }


@dataclass
class _Subscription:
    stream: Any
    lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, frame: Mapping[str, Any]) -> None:
        data = json.dumps(frame, separators=(",", ":")).encode() + b"\n"
        with self.lock:
            self.stream.sendall(data)


class FakeHcServer:
    """A multi-client fake implementing the hc 0.1.22 JSON-RPC surface."""

    def __init__(
        self,
        endpoint: Path,
        *,
        version: str = "0.1.22",
        generation: int = 1,
        holders: Sequence[Mapping[str, Any]] | None = None,
        error_code: int = -32603,
        heartbeat_interval: float | None = None,
        initial_subscription_event: Mapping[str, Any] | None = None,
    ) -> None:
        self.endpoint = Path(endpoint)
        self.version = version
        self.generation = generation
        self.error_code = error_code
        self.heartbeat_interval = heartbeat_interval
        self.initial_subscription_event = (
            dict(initial_subscription_event)
            if initial_subscription_event is not None
            else None
        )
        self._core = {"id": "fake-core-id", "name": "fake-core"}
        self._holders = [dict(holder) for holder in holders or ()]
        self._listener = listen_local_ipc(self.endpoint)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.frames: list[dict[str, Any]] = []
        self._terminals: dict[str, _FakeTerminal] = {}
        self._subscriptions: list[_Subscription] = []
        self._workers: list[threading.Thread] = []
        self._seq = 0
        self._next_address = 1
        self._next_pid = 41000
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._accept_thread.start()
        self._heartbeat_thread.start()

    def __enter__(self) -> FakeHcServer:
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._listener.close()
        with self._lock:
            subscriptions = list(self._subscriptions)
            self._subscriptions.clear()
        for subscription in subscriptions:
            subscription.stream.close()
        self._accept_thread.join(timeout=5)
        self._heartbeat_thread.join(timeout=5)
        for worker in list(self._workers):
            worker.join(timeout=5)

    def emit_event(
        self,
        event_type: str,
        terminal: str | None,
        data: Mapping[str, Any],
        *,
        seq: int | None = None,
    ) -> int:
        with self._lock:
            if seq is None:
                self._seq += 1
                sequence = self._seq
            else:
                sequence = seq
                self._seq = max(self._seq, sequence)
            frame = {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "seq": sequence,
                    "terminal": terminal,
                    "type": event_type,
                    "data": dict(data),
                },
            }
            self._broadcast_locked(frame)
            return sequence

    def emit_status(self, terminal: str, status: Mapping[str, Any] | None) -> int:
        with self._lock:
            record = self._require_terminal(terminal)
            record.status = dict(status) if status is not None else None
            record.status_ms = _now_ms()
        return self.emit_event("status", terminal, {"status": status})

    def emit_missed(self, terminals: Sequence[Any], *, seq: int) -> None:
        with self._lock:
            self._seq = seq
            records = [_record_wire(item) for item in terminals]
            frame = {
                "jsonrpc": "2.0",
                "method": "missed",
                "params": {"seq": seq, "terminals": records},
            }
            self._broadcast_locked(frame)

    def finish_terminal(
        self,
        terminal: str,
        *,
        exit_code: int | None = 0,
        signal: int | None = None,
        reason: str = "exited",
    ) -> None:
        with self._lock:
            record = self._require_terminal(terminal)
            record.state = "finished"
            record.exit_code = exit_code
            record.signal = signal
            record.reason = reason
            record.finished_ms = _now_ms()
            finished_ms = record.finished_ms
        self.emit_event(
            "finished",
            terminal,
            {
                "exit_code": exit_code,
                "signal": signal,
                "reason": reason,
                "finished_ms": finished_ms,
            },
        )

    def lose_holder(self, terminal: str) -> None:
        with self._lock:
            record = self._require_terminal(terminal)
            record.state = "lost"
            record.reason = "holder_lost"
            record.finished_ms = _now_ms()
        self.emit_event("lost", terminal, {"reason": "holder_lost"})

    def simulate_core_restart(self, *, new_version: str | None = None) -> None:
        with self._lock:
            if new_version is not None:
                self.version = new_version
            self._seq = 0
            subscriptions = list(self._subscriptions)
            self._subscriptions.clear()
        for subscription in subscriptions:
            subscription.stream.close()

    def _accept_loop(self) -> None:
        self._listener.settimeout(0.05)
        while not self._stop.is_set():
            try:
                stream, _peer = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            worker = threading.Thread(target=self._client_loop, args=(stream,), daemon=True)
            self._workers.append(worker)
            worker.start()

    def _client_loop(self, stream: Any) -> None:
        stream.settimeout(0.1)
        subscription: _Subscription | None = None
        try:
            frame = self._receive_frame(stream)
            if frame is None:
                return
            with self._lock:
                self.frames.append(dict(frame))
            response, subscription = self._handle_request(frame, stream)
            if subscription is not None and self.initial_subscription_event is not None:
                self._send_initial_subscription(stream, response)
            else:
                self._send_response(stream, response)
            if subscription is None:
                return
            while not self._stop.is_set():
                if self._receive_frame(stream) is None:
                    return
        finally:
            if subscription is not None:
                with self._lock:
                    if subscription in self._subscriptions:
                        self._subscriptions.remove(subscription)
            stream.close()

    def _handle_request(self, frame: Mapping[str, Any], stream: Any) -> tuple[dict[str, Any], _Subscription | None]:
        request_id = frame.get("id")
        method = frame.get("method")
        params = frame.get("params", {})
        if not isinstance(method, str) or not isinstance(params, Mapping):
            return self._error(request_id, "invalid_params", "invalid request"), None
        try:
            result, subscription = self._dispatch(method, params, stream)
        except _FakeError as error:
            return self._error(request_id, error.kind, error.message), None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}, subscription

    def _error(self, request_id: Any, kind: str, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": self.error_code,
                "message": message,
                "data": {"kind": kind},
            },
        }

    def _dispatch(
        self, method: str, params: Mapping[str, Any], stream: Any
    ) -> tuple[dict[str, Any], _Subscription | None]:
        if method == "hello":
            return {
                "generation": self.generation,
                "version": self.version,
                "core": dict(self._core),
                "framed": False,
                "methods": [
                    "hello",
                    "server.status",
                    "server.stop",
                    "terminal.run",
                    "terminal.list",
                    "terminal.get",
                    "terminal.kill",
                    "terminal.rm",
                    "terminal.label",
                    "terminal.read",
                    "terminal.send",
                    "terminal.keys",
                    "events.subscribe",
                ],
                "features": ["events"],
            }, None
        if method == "server.status":
            return self._status(), None
        if method == "server.stop":
            return self._server_stop(), None
        if method == "terminal.run":
            return self._terminal_run(params), None
        if method == "terminal.list":
            return self._terminal_list(params), None
        if method == "terminal.get":
            return self._require_terminal(str(params.get("terminal"))).wire(), None
        if method == "terminal.kill":
            terminal = str(params.get("terminal"))
            self._finish(terminal, None, 1, "killed")
            return self._require_terminal(terminal).wire(), None
        if method == "terminal.rm":
            terminal = str(params.get("terminal"))
            with self._lock:
                self._require_terminal(terminal)
                del self._terminals[terminal]
            self.emit_event("removed", terminal, {})
            return {"removed": terminal}, None
        if method == "terminal.label":
            return self._terminal_label(params), None
        if method == "terminal.read":
            self._require_terminal(str(params.get("terminal")))
            return {"text": "", "lines": []}, None
        if method == "terminal.send" or method == "terminal.keys":
            self._require_terminal(str(params.get("terminal")))
            return {"sent": True}, None
        if method == "events.subscribe":
            with self._lock:
                snapshot = self._snapshot_locked(params)
                subscription = _Subscription(stream)
                self._subscriptions.append(subscription)
            return snapshot, subscription
        raise _FakeError("not_found", f"unknown method {method}")

    def _terminal_run(self, params: Mapping[str, Any]) -> dict[str, Any]:
        argv = params.get("argv")
        if not isinstance(argv, list) or any(not isinstance(item, str) for item in argv):
            raise _FakeError("invalid_params", "argv must be a string list")
        env = params.get("env", {})
        labels = params.get("labels", {})
        if not _string_mapping(env) or not _string_mapping(labels):
            raise _FakeError("invalid_params", "env and labels must contain strings")
        name = params.get("name")
        if name is not None and not isinstance(name, str):
            raise _FakeError("invalid_params", "name must be a string")
        with self._lock:
            if name is not None and any(
                item.name == name and item.state == "live" for item in self._terminals.values()
            ):
                raise _FakeError("already_exists", "a live terminal has that name")
            address = f"t{self._next_address}"
            self._next_address += 1
            size = params.get("size")
            size_map = size if isinstance(size, Mapping) else {}
            record = _FakeTerminal(
                address=address,
                name=name,
                labels=dict(labels),
                argv=tuple(argv),
                cwd=str(params.get("cwd", "")),
                keep=bool(params.get("keep", False)),
                cols=int(size_map.get("cols", 120)),
                rows=int(size_map.get("rows", 40)),
                created_ms=_now_ms(),
                pid=self._next_pid,
            )
            self._next_pid += 1
            self._terminals[address] = record
            wire = record.wire()
        self.emit_event("created", address, wire)
        return wire

    def _terminal_list(self, params: Mapping[str, Any]) -> dict[str, Any]:
        labels = params.get("labels")
        if labels is not None and not _string_mapping(labels):
            raise _FakeError("invalid_params", "labels must contain strings")
        with self._lock:
            records = [
                item.wire()
                for item in self._terminals.values()
                if labels is None or all(item.labels.get(key) == value for key, value in labels.items())
            ]
        return {"terminals": records, "hidden_finished": 0, "machines": []}

    def _terminal_label(self, params: Mapping[str, Any]) -> dict[str, Any]:
        terminal = str(params.get("terminal"))
        labels = params.get("labels")
        if not _string_mapping(labels):
            raise _FakeError("invalid_params", "labels must contain strings")
        with self._lock:
            record = self._require_terminal(terminal)
            for key, value in labels.items():
                if value == "":
                    record.labels.pop(key, None)
                else:
                    record.labels[key] = value
            wire = record.wire()
        self.emit_event("labels", terminal, {"labels": dict(wire["labels"])})
        return wire

    def _server_stop(self) -> dict[str, Any]:
        with self._lock:
            to_finish = [item.address for item in self._terminals.values() if item.state == "live" and not item.keep]
        for address in to_finish:
            self._finish(address, None, 1, "server_stop")
        return {"stopped": True}

    def _finish(self, address: str, exit_code: int | None, signal: int | None, reason: str) -> None:
        with self._lock:
            record = self._require_terminal(address)
            record.state = "finished"
            record.exit_code = exit_code
            record.signal = signal
            record.reason = reason
            record.finished_ms = _now_ms()
            finished_ms = record.finished_ms
        self.emit_event(
            "finished",
            address,
            {"exit_code": exit_code, "signal": signal, "reason": reason, "finished_ms": finished_ms},
        )

    def _status(self) -> dict[str, Any]:
        with self._lock:
            live = sum(item.state == "live" for item in self._terminals.values())
            finished = sum(item.state != "live" for item in self._terminals.values())
            return {
                "core": dict(self._core),
                "version": self.version,
                "generation": self.generation,
                "pid": 42000,
                "uptime_ms": 1000,
                "holder": None,
                "holders": [dict(holder) for holder in self._holders],
                "live": live,
                "finished": finished,
                "installed_by": None,
            }

    def _snapshot_locked(self, params: Mapping[str, Any]) -> dict[str, Any]:
        labels = params.get("labels")
        records = [
            item.wire()
            for item in self._terminals.values()
            if labels is None or all(item.labels.get(key) == value for key, value in labels.items())
        ]
        return {"seq": self._seq, "channel": None, "terminals": records}

    def _require_terminal(self, address: str) -> _FakeTerminal:
        record = self._terminals.get(address)
        if record is None:
            raise _FakeError("not_found", f"terminal {address} was not found")
        return record

    def _broadcast_locked(self, frame: Mapping[str, Any]) -> None:
        dead: list[_Subscription] = []
        for subscription in self._subscriptions:
            try:
                subscription.send(frame)
            except OSError:
                dead.append(subscription)
        for subscription in dead:
            if subscription in self._subscriptions:
                self._subscriptions.remove(subscription)

    def _send_response(self, stream: Any, response: Mapping[str, Any]) -> None:
        stream.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")

    def _send_initial_subscription(
        self, stream: Any, response: Mapping[str, Any]
    ) -> None:
        with self._lock:
            self._seq += 1
            event = {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "seq": self._seq,
                    "terminal": self.initial_subscription_event.get("terminal"),
                    "type": self.initial_subscription_event.get("type"),
                    "data": self.initial_subscription_event.get("data", {}),
                },
            }
        payload = b"".join(
            json.dumps(frame, separators=(",", ":")).encode() + b"\n"
            for frame in (response, event)
        )
        stream.sendall(payload)

    def _receive_frame(self, stream: Any) -> Mapping[str, Any] | None:
        buffer = bytearray()
        while not self._stop.is_set():
            try:
                chunk = stream.recv(64 * 1024)
            except TimeoutError:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            buffer.extend(chunk)
            newline = buffer.find(b"\n")
            if newline >= 0:
                value = json.loads(buffer[:newline])
                return value if isinstance(value, Mapping) else None
        return None

    def _heartbeat_loop(self) -> None:
        if self.heartbeat_interval is None:
            return
        while not self._stop.wait(self.heartbeat_interval):
            with self._lock:
                self._broadcast_locked(
                    {"jsonrpc": "2.0", "method": "heartbeat", "params": {"seq": self._seq}}
                )


@dataclass(frozen=True)
class _FakeError(Exception):
    kind: str
    message: str


def _now_ms() -> int:
    return int(time.time() * 1000)


def _string_mapping(value: Any) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def _record_wire(value: Any) -> dict[str, Any]:
    if isinstance(value, _FakeTerminal):
        return value.wire()
    if isinstance(value, Mapping):
        return dict(value)
    return {
        "address": value.address,
        "name": value.name,
        "labels": dict(value.labels),
        "argv": list(value.argv),
        "cwd": value.cwd,
        "state": value.state,
        "pid": value.pid,
        "exit_code": value.exit_code,
        "signal": value.signal,
        "reason": value.reason,
        "status": value.status,
        "status_ms": value.status_ms,
        "created_ms": value.created_ms,
        "finished_ms": value.finished_ms,
        "cols": value.cols,
        "rows": value.rows,
    }
