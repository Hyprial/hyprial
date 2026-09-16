"""Daemon-owned protocol-v2 forwarding sidecar lifecycle."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Self

from hyprial.contracts.forwarding import FORWARDING_COMMAND_ENV, FORWARDING_UP_ENV

FORWARD_PROTOCOL_VERSION = 2
_DEFAULT_TIMEOUT = 10.0
_PROXY_KEYS = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)


class ForwardingSidecarError(RuntimeError):
    pass


def _child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith("TS_") and key not in _PROXY_KEYS
    }


class ForwardingSidecarController:
    """Own one candidate sidecar process and its closed v2 JSON-lines wire."""

    def __init__(
        self,
        command: Sequence[str],
        up: Mapping[str, object],
        *,
        environ: Mapping[str, str] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        on_exit: Callable[[int], None] | None = None,
    ) -> None:
        if not command:
            raise ValueError("forwarding sidecar command must not be empty")
        self._up = dict(up)
        if self._up.get("v") != FORWARD_PROTOCOL_VERSION or self._up.get("op") != "up":
            raise ValueError("forwarding sidecar up request must be protocol v2")
        if self._up.get("authKey") is not None or self._up.get("resume") is not True:
            raise ValueError("daemon forwarding must resume state without an auth key")
        self._timeout = timeout
        self._on_exit = on_exit
        self._lock = threading.Lock()
        self._closing = False
        self._startup_complete = False
        self._events: queue.Queue[bytes | None] = queue.Queue()
        self.child_environment = _child_environment(
            os.environ if environ is None else environ
        )
        self._process = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            env=self.child_environment,
        )
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, daemon=True, name="forward-sidecar-stdout"
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True, name="forward-sidecar-stderr"
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        try:
            self._start()
        except BaseException:
            self._terminate_owned_process()
            raise
        self._startup_complete = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_exit, daemon=True, name="forward-sidecar-monitor"
        )
        self._monitor_thread.start()

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
        *,
        on_exit: Callable[[int], None] | None = None,
    ) -> Self:
        try:
            command = json.loads(environ[FORWARDING_COMMAND_ENV])
            up = json.loads(environ[FORWARDING_UP_ENV])
        except (KeyError, TypeError, ValueError) as error:
            raise ForwardingSidecarError(
                "forwarding sidecar environment is incomplete or invalid"
            ) from error
        if not isinstance(command, list) or not command or not all(
            isinstance(item, str) and item for item in command
        ):
            raise ForwardingSidecarError(
                "forwarding sidecar command must be a non-empty JSON string array"
            )
        if not isinstance(up, dict):
            raise ForwardingSidecarError(
                "forwarding sidecar up request must be a JSON object"
            )
        return cls(command, up, environ=environ, on_exit=on_exit)

    @property
    def pid(self) -> int:
        return self._process.pid

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                self._events.put(line)
        finally:
            self._events.put(None)

    def _drain_stderr(self) -> None:
        assert self._process.stderr is not None
        try:
            for _chunk in iter(lambda: self._process.stderr.read(4096), b""):
                pass
        except OSError:
            pass

    def _monitor_exit(self) -> None:
        code = self._process.wait()
        if self._startup_complete and not self._closing and self._on_exit is not None:
            self._on_exit(code)

    def _send(self, payload: Mapping[str, object]) -> None:
        if self._process.poll() is not None:
            raise ForwardingSidecarError(
                f"forwarding sidecar exited with status {self._process.returncode}"
            )
        try:
            assert self._process.stdin is not None
            self._process.stdin.write(
                (json.dumps(dict(payload), separators=(",", ":")) + "\n").encode()
            )
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise ForwardingSidecarError("forwarding sidecar stdin closed") from error

    def _next(self, *, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ForwardingSidecarError("forwarding sidecar response timed out")
        try:
            raw = self._events.get(timeout=remaining)
        except queue.Empty as error:
            raise ForwardingSidecarError(
                "forwarding sidecar response timed out"
            ) from error
        if raw is None:
            code = self._process.poll()
            raise ForwardingSidecarError(
                f"forwarding sidecar closed stdout with status {code}"
            )
        try:
            event = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as error:
            raise ForwardingSidecarError(
                "forwarding sidecar emitted invalid JSON"
            ) from error
        if not isinstance(event, dict) or event.get("v") != FORWARD_PROTOCOL_VERSION:
            raise ForwardingSidecarError(
                "forwarding sidecar emitted a non-v2 event"
            )
        if event.get("event") == "error":
            code = str(event.get("code") or "FORWARD_PROTOCOL")
            message = str(event.get("message") or "forwarding sidecar error")
            raise ForwardingSidecarError(f"{code}: {message}")
        return event

    def _start(self) -> None:
        deadline = time.monotonic() + self._timeout
        hello = self._next(deadline=deadline)
        if hello.get("event") != "hello" or not hello.get("sidecar"):
            raise ForwardingSidecarError("forwarding sidecar v2 hello mismatch")
        self._send(self._up)
        while True:
            event = self._next(deadline=deadline)
            if event.get("event") == "ready":
                return
            if event.get("event") != "state":
                raise ForwardingSidecarError(
                    f"unexpected forwarding startup event {event.get('event')!r}"
                )

    def _request(self, payload: Mapping[str, object], expected: str) -> dict[str, Any]:
        with self._lock:
            self._send(payload)
            deadline = time.monotonic() + self._timeout
            event = self._next(deadline=deadline)
            if event.get("event") != expected:
                raise ForwardingSidecarError(
                    f"expected forwarding event {expected!r}; got {event.get('event')!r}"
                )
            return event

    def status(self) -> tuple[tuple[str, ...], dict[str, int]]:
        event = self._request(
            {"v": FORWARD_PROTOCOL_VERSION, "op": "status"}, "status"
        )
        raw_peers = event.get("peers")
        raw_mappings = event.get("mappings", [])
        if not isinstance(raw_peers, list) or not all(
            isinstance(peer, str) and peer for peer in raw_peers
        ):
            raise ForwardingSidecarError("forwarding status has invalid peers")
        if not isinstance(raw_mappings, list):
            raise ForwardingSidecarError("forwarding status has invalid mappings")
        mappings: dict[str, int] = {}
        for item in raw_mappings:
            if not isinstance(item, dict):
                raise ForwardingSidecarError("forwarding status has invalid mapping")
            peer, port = item.get("peer"), item.get("localPort")
            if not isinstance(peer, str) or not isinstance(port, int):
                raise ForwardingSidecarError("forwarding status has invalid mapping")
            mappings[peer] = port
        return tuple(raw_peers), mappings

    def map_peer(self, peer: str) -> int:
        event = self._request(
            {"v": FORWARD_PROTOCOL_VERSION, "op": "map-peer", "peer": peer},
            "peer-mapped",
        )
        if event.get("peer") != peer or not isinstance(event.get("localPort"), int):
            raise ForwardingSidecarError("peer-mapped event does not match request")
        return int(event["localPort"])

    def unmap_peer(self, peer: str) -> None:
        event = self._request(
            {"v": FORWARD_PROTOCOL_VERSION, "op": "unmap-peer", "peer": peer},
            "peer-unmapped",
        )
        if event.get("peer") != peer:
            raise ForwardingSidecarError("peer-unmapped event does not match request")

    def close(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            if self._process.poll() is None:
                try:
                    self._send({"v": FORWARD_PROTOCOL_VERSION, "op": "down"})
                    deadline = time.monotonic() + self._timeout
                    event = self._next(deadline=deadline)
                    if event.get("event") != "exited":
                        raise ForwardingSidecarError(
                            "forwarding sidecar did not acknowledge down"
                        )
                except ForwardingSidecarError:
                    pass
            self._terminate_owned_process()

    def _terminate_owned_process(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        for stream in (
            self._process.stdin,
            self._process.stdout,
            self._process.stderr,
        ):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
