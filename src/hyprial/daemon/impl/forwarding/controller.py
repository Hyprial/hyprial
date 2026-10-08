"""ForwardingControllerAuthority and the sidecar process controller."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self
import uuid
from hyprial.kernel import (
    FORWARDING_COMMAND_ENV,
    FORWARDING_UP_ENV,
    SERVICE_CONTROL_TIMEOUT_SECONDS,
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
    ipc_errors,
)

from .policy import (
    FORWARD_PROTOCOL_VERSION,
    ForwardingSidecarError,
    _PROXY_KEYS,
    _normalize_exposure,
)
from .services import (
    ServiceControlError,
    _MapServiceSidecar,
    _SERVICE_COMMANDS,
    _ServiceStatusSidecar,
    _UnmapServiceSidecar,
    decode_service_response,
    map_service_request,
    safe_service_error,
    service_status_request,
    unmap_service_request,
    validate_service_mapping,
)


_DEFAULT_TIMEOUT = 10.0


_STDERR_TAIL_LINES = 12
_STALE_SERVICE_REQUEST_CAPACITY = 64
_STALE_SERVICE_REQUEST_MAX_AGE_SECONDS = 60.0
_RESPONSE_BUFFER_CAPACITY = 128
_RESPONSE_BUFFER_MAX_AGE_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class _StatusSidecar:
    operation_id: str


@dataclass(frozen=True, slots=True)
class _MapSidecarPeer:
    operation_id: str
    peer: str
    address: str


@dataclass(frozen=True, slots=True)
class _UnmapSidecarPeer:
    operation_id: str
    peer: str


@dataclass(frozen=True, slots=True)
class _ExposeSidecar:
    operation_id: str
    exposure: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _UnexposeSidecar:
    operation_id: str
    port: int


@dataclass(frozen=True, slots=True)
class _PeerKeySidecar:
    operation_id: str
    address: str


@dataclass(frozen=True, slots=True)
class _AllowSidecar:
    operation_id: str
    keys: tuple[str, ...]
    allow_any: bool


@dataclass(frozen=True, slots=True)
class _CloseSidecar:
    operation_id: str


class _SidecarReply:
    def __init__(self) -> None:
        self.ready = threading.Event()
        self.value: object | None = None
        self.error: BaseException | None = None


class ForwardingControllerAuthority:
    """Typed, bounded native wire owner for one forwarding sidecar process."""

    def __init__(self, controller: Any, *, capacity: int = 16, timeout: float = 12.0):
        if capacity < 1 or timeout <= 0:
            raise ValueError("forwarding authority capacity and timeout must be positive")
        self._controller = controller
        self._capacity = capacity
        self._guard = threading.Lock()
        self._pending: dict[str, _SidecarReply] = {}
        self._closed = False
        self._timeout = timeout
        self._peers_reported: bool | None = None
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="forwarding-sidecar-io",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
                supervision_profile="external_io",
            )
        )

    @property
    def pid(self) -> int | None:
        value = getattr(self._controller, "pid", None)
        return value if isinstance(value, int) else None

    @property
    def peers_reported(self) -> bool | None:
        with self._guard:
            return self._peers_reported

    @property
    def supports_service_connect(self) -> bool:
        return getattr(self._controller, "supports_service_connect", False) is True

    def _require_service_capability(self) -> None:
        if not self.supports_service_connect:
            raise ServiceControlError(
                ipc_errors.SERVICE_SIDECAR_UNSUPPORTED,
                "forwarding sidecar does not support service connect",
            )

    def _call(self, command: object, *, service: bool = False) -> object | None:
        operation_id = command.operation_id  # type: ignore[attr-defined]
        reply = _SidecarReply()
        with self._guard:
            if self._closed or len(self._pending) >= self._capacity:
                if service:
                    raise ServiceControlError(
                        ipc_errors.SERVICE_UNAVAILABLE,
                        "forwarding service I/O owner is closed or overloaded",
                    )
                raise ForwardingSidecarError("forwarding sidecar I/O owner overloaded")
            self._pending[operation_id] = reply
            admitted = self._runtime.tell(self._handle, command)
            if admitted is not AdmissionResult.ACCEPTED:
                self._pending.pop(operation_id)
                if service:
                    raise ServiceControlError(
                        ipc_errors.SERVICE_UNAVAILABLE,
                        "forwarding service I/O admission was refused",
                    )
                raise ForwardingSidecarError(
                    f"forwarding sidecar I/O admission {admitted.value}"
                )
        wait_timeout = min(self._timeout, SERVICE_CONTROL_TIMEOUT_SECONDS) if service else self._timeout
        if not reply.ready.wait(wait_timeout):
            raise TimeoutError(
                f"forwarding sidecar command {operation_id} remains accepted"
            )
        if reply.error is not None:
            raise safe_service_error(reply.error) if service else reply.error
        return reply.value

    def _receive(self, command: object) -> None:
        try:
            if isinstance(command, _StatusSidecar):
                value = self._controller.status()
                reported = getattr(self._controller, "peers_reported", None)
                with self._guard:
                    self._peers_reported = reported
            elif isinstance(command, _MapSidecarPeer):
                value = self._controller.map_peer(command.peer, command.address)
            elif isinstance(command, _UnmapSidecarPeer):
                value = self._controller.unmap_peer(command.peer)
            elif isinstance(command, _ExposeSidecar):
                value = self._controller.expose(command.exposure)
            elif isinstance(command, _UnexposeSidecar):
                value = self._controller.unexpose(command.port)
            elif isinstance(command, _PeerKeySidecar):
                value = self._controller.peer_key(command.address)
            elif isinstance(command, _AllowSidecar):
                value = self._controller.allow(command.keys, command.allow_any)
            elif isinstance(command, _MapServiceSidecar):
                value = self._controller.map_service(
                    command.name,
                    command.device_id,
                    command.address,
                    command.server_public,
                    command.remote_port,
                    command.record_generation,
                    command.local_port,
                )
            elif isinstance(command, _UnmapServiceSidecar):
                value = self._controller.unmap_service(command.name)
            elif isinstance(command, _ServiceStatusSidecar):
                value = self._controller.service_status()
            elif isinstance(command, _CloseSidecar):
                value = self._controller.close()
            else:
                raise TypeError("unsupported forwarding sidecar command")
            error = None
        except BaseException as caught:
            value = None
            error = safe_service_error(caught) if isinstance(command, _SERVICE_COMMANDS) else caught
        with self._guard:
            reply = self._pending.pop(command.operation_id, None)  # type: ignore[attr-defined]
            if reply is not None:
                reply.value = value
                reply.error = error
                reply.ready.set()

    def status(self) -> tuple[tuple[str, ...], dict[str, int]]:
        result = self._call(_StatusSidecar(uuid.uuid4().hex))
        assert isinstance(result, tuple)
        return result

    def map_peer(self, peer: str, address: str) -> int:
        result = self._call(_MapSidecarPeer(uuid.uuid4().hex, peer, address))
        assert isinstance(result, int)
        return result

    def unmap_peer(self, peer: str) -> None:
        self._call(_UnmapSidecarPeer(uuid.uuid4().hex, peer))

    def expose(self, exposure: Mapping[str, object]) -> dict[str, object]:
        result = self._call(_ExposeSidecar(uuid.uuid4().hex, dict(exposure)))
        assert isinstance(result, dict)
        return result

    def unexpose(self, port: int) -> None:
        self._call(_UnexposeSidecar(uuid.uuid4().hex, port))

    def peer_key(self, address: str) -> dict[str, object]:
        result = self._call(_PeerKeySidecar(uuid.uuid4().hex, address))
        assert isinstance(result, dict)
        return result

    def allow(self, keys: Sequence[str], allow_any: bool) -> dict[str, object]:
        result = self._call(
            _AllowSidecar(uuid.uuid4().hex, tuple(keys), bool(allow_any))
        )
        assert isinstance(result, dict)
        return result

    def map_service(
        self,
        name: str,
        device_id: str,
        address: str,
        server_public: str,
        remote_port: int,
        record_generation: int,
        local_port: int = 0,
    ) -> dict[str, object]:
        self._require_service_capability()
        result = self._call(
            _MapServiceSidecar(
                uuid.uuid4().hex,
                name,
                device_id,
                address,
                server_public,
                remote_port,
                record_generation,
                local_port,
            ),
            service=True,
        )
        try:
            mapping = validate_service_mapping(result)
        except ServiceControlError as error:
            raise safe_service_error(error) from error
        if (
            mapping["name"] != name
            or mapping["deviceId"] != device_id
            or mapping["remotePort"] != remote_port
            or mapping["recordGeneration"] != record_generation
            or (local_port != 0 and mapping["localPort"] != local_port)
        ):
            raise ServiceControlError(
                ipc_errors.SERVICE_UNAVAILABLE,
                "forwarding service authority result is invalid",
            )
        return mapping

    def unmap_service(self, name: str) -> bool:
        self._require_service_capability()
        result = self._call(
            _UnmapServiceSidecar(uuid.uuid4().hex, name), service=True
        )
        if not isinstance(result, bool):
            raise ServiceControlError(
                ipc_errors.SERVICE_UNAVAILABLE,
                "forwarding service authority result is invalid",
            )
        return result

    def service_status(self) -> dict[str, object]:
        self._require_service_capability()
        result = self._call(_ServiceStatusSidecar(uuid.uuid4().hex), service=True)
        if not isinstance(result, dict) or set(result) != {"services"}:
            raise ServiceControlError(
                ipc_errors.SERVICE_UNAVAILABLE,
                "forwarding service authority result is invalid",
            )
        services = result["services"]
        if not isinstance(services, list):
            raise ServiceControlError(
                ipc_errors.SERVICE_UNAVAILABLE,
                "forwarding service authority result is invalid",
            )
        return {"services": [validate_service_mapping(item) for item in services]}

    def close(self) -> None:
        with self._guard:
            if self._closed:
                return
        self._call(_CloseSidecar(uuid.uuid4().hex))
        with self._guard:
            self._closed = True
        if not self._runtime.stop(self._handle, self._timeout):
            raise TimeoutError("forwarding sidecar I/O owner did not drain")


def _child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith("TS_") and key not in _PROXY_KEYS
    }


class ForwardingSidecarController:
    """Own one candidate sidecar process and its closed v3 JSON-lines wire."""

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
            raise ValueError("forwarding sidecar up request must be protocol v3")
        if not isinstance(self._up.get("keyFile"), str) or not self._up["keyFile"]:
            raise ValueError("forwarding sidecar up request must name a keyFile")
        if not isinstance(self._up.get("allow"), list) or not all(
            isinstance(item, str) for item in self._up["allow"]
        ):
            raise ValueError("forwarding sidecar up request must carry an allow list")
        if not isinstance(self._up.get("allowAny"), bool):
            raise ValueError("forwarding sidecar up request must carry allowAny")
        if not isinstance(self._up.get("addressFile"), str) or not self._up["addressFile"]:
            raise ValueError("forwarding sidecar up request must name an addressFile")
        self._timeout = timeout
        self._on_exit = on_exit
        self._expose_capable = False
        self._service_capable = False
        self._forwarding_request_ids = False
        self._service_wire_failed = False
        self._stale_service_requests: dict[str, float] = {}
        self._write_lock = threading.Lock()
        self._service_lock = threading.Lock()
        self._response_condition = threading.Condition()
        self._response_buffer: deque[tuple[float, dict[str, Any]]] = deque()
        self._response_reader = False
        self._active_service_request_id: str | None = None
        self._dropped_service_responses = 0
        self._service_timeout = SERVICE_CONTROL_TIMEOUT_SECONDS
        # Whether the last ``status`` answer carried a ``peers`` key at all.
        # Current sidecars send an explicit ``[]`` for an empty mesh, so an
        # absent key means a build that does not report peers -- which must
        # not look the same as "nobody is online" (forwarding defaults plan
        # §C).  None until the first status.
        self.peers_reported: bool | None = None
        self._lock = threading.Lock()
        self._closing = False
        self._startup_complete = False
        self._events: queue.Queue[bytes | None] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
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
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
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

    @property
    def supports_service_connect(self) -> bool:
        return self._service_capable

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        try:
            for line in self._process.stdout:
                self._events.put(line)
        finally:
            self._events.put(None)

    def _drain_stderr(self) -> None:
        # Keep the tail instead of discarding every chunk.  The sidecar's
        # stderr is the only place a failure explains itself, and the protocol
        # carries a single bounded code: draining to ``pass`` collapses every
        # distinct cause into one indistinguishable message, and leaves
        # nothing to read afterwards.
        assert self._process.stderr is not None
        try:
            for line in self._process.stderr:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self._stderr_tail.append(text)
        except OSError:
            pass

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        """The last few stderr lines the sidecar produced, newest last."""

        return tuple(self._stderr_tail)

    def _status_failure(self, message: str) -> ForwardingSidecarError:
        """A status failure that carries whatever the sidecar said about it."""

        # Reads the tail defensively: this is a diagnostic on a failure path,
        # and a reader that raises AttributeError here would replace the
        # forwarding fault with a reporting fault -- the same trade
        # _mirror_startup_event_to_stderr and _log_lifecycle_operation make.
        # It is reachable uninitialized because tests construct the controller
        # through object.__new__ to stub _request (no process is spawned).
        tail = " | ".join(getattr(self, "_stderr_tail", ()))
        return ForwardingSidecarError(
            message if not tail else f"{message} (sidecar stderr: {tail})"
        )

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
            raise ForwardingSidecarError("forwarding sidecar emitted a non-v3 event")
        return event

    @staticmethod
    def _forwarding_error(event: Mapping[str, object]) -> ForwardingSidecarError:
        code = str(event.get("code") or "FORWARD_PROTOCOL")
        message = str(event.get("message") or "forwarding sidecar error")
        return ForwardingSidecarError(f"{code}: {message}")

    def _ensure_response_state(self) -> None:
        if not hasattr(self, "_response_condition"):
            self._response_condition = threading.Condition()
            self._response_buffer = deque()
            self._response_reader = False
        if not hasattr(self, "_active_service_request_id"):
            self._active_service_request_id = None
        if not hasattr(self, "_dropped_service_responses"):
            self._dropped_service_responses = 0

    def _await_response(
        self,
        match: Callable[[Mapping[str, object]], bool],
        *,
        deadline: float,
    ) -> dict[str, Any]:
        """Demultiplex the shared stream without holding an operation lock."""

        self._ensure_response_state()
        condition = self._response_condition
        while True:
            with condition:
                stale = getattr(self, "_stale_service_requests", {})
                now = time.monotonic()
                if isinstance(stale, set):
                    stale = {request_id: now for request_id in stale}
                    self._stale_service_requests = stale
                cutoff = now - _STALE_SERVICE_REQUEST_MAX_AGE_SECONDS
                for request_id, recorded_at in tuple(stale.items()):
                    if recorded_at < cutoff:
                        stale.pop(request_id, None)
                response_cutoff = now - _RESPONSE_BUFFER_MAX_AGE_SECONDS
                while (
                    self._response_buffer
                    and self._response_buffer[0][0] < response_cutoff
                ):
                    self._response_buffer.popleft()
                selected: int | None = None
                discarded: list[int] = []
                for index, (_recorded_at, event) in enumerate(self._response_buffer):
                    request_id = event.get("requestId")
                    if isinstance(request_id, str) and request_id in stale:
                        discarded.append(index)
                    elif (
                        str(event.get("event", "")).startswith("service-")
                        and request_id != self._active_service_request_id
                    ):
                        discarded.append(index)
                        self._dropped_service_responses += 1
                    elif selected is None and match(event):
                        selected = index
                for index in reversed(discarded):
                    request_id = self._response_buffer[index][1].get("requestId")
                    stale.pop(request_id, None)
                    del self._response_buffer[index]
                    if selected is not None and index < selected:
                        selected -= 1
                if selected is not None:
                    event = self._response_buffer[selected][1]
                    del self._response_buffer[selected]
                    return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ForwardingSidecarError(
                        "forwarding sidecar response timed out"
                    )
                if self._response_reader:
                    condition.wait(remaining)
                    continue
                self._response_reader = True
            try:
                event = self._next(deadline=deadline)
            finally:
                with condition:
                    self._response_reader = False
                    condition.notify_all()
            with condition:
                self._response_buffer.append((time.monotonic(), event))
                while len(self._response_buffer) > _RESPONSE_BUFFER_CAPACITY:
                    self._response_buffer.popleft()
                condition.notify_all()

    def _start(self) -> None:
        deadline = time.monotonic() + self._timeout
        hello = self._next(deadline=deadline)
        if hello.get("event") != "hello" or not hello.get("sidecar"):
            raise ForwardingSidecarError("forwarding sidecar v3 hello mismatch")
        capabilities = hello.get("capabilities")
        capability_set = (
            set(capabilities)
            if isinstance(capabilities, list)
            and all(isinstance(item, str) for item in capabilities)
            else set()
        )
        self._expose_capable = {"expose", "unexpose", "peer-key"} <= capability_set
        self._service_capable = "service-connect-v1" in capability_set
        self._forwarding_request_ids = "forwarding-request-id-v1" in capability_set
        self._send(self._up)
        while True:
            event = self._next(deadline=deadline)
            if event.get("event") == "error":
                raise self._forwarding_error(event)
            if event.get("event") != "state":
                raise ForwardingSidecarError(
                    f"unexpected forwarding startup event {event.get('event')!r}"
                )
            if event.get("state") == "Running":
                # Protocol v3: the Running state must carry serverPublic,
                # clientPublic and a ``peers`` key (possibly empty).
                if not isinstance(event.get("serverPublic"), str) or not isinstance(
                    event.get("clientPublic"), str
                ):
                    raise ForwardingSidecarError(
                        "forwarding sidecar Running state lacks its public keys"
                    )
                if not isinstance(event.get("peers"), list):
                    raise ForwardingSidecarError(
                        "forwarding sidecar Running state lacks a peers list"
                    )
                return

    def _request(self, payload: Mapping[str, object], expected: str) -> dict[str, Any]:
        with self._lock:
            request = dict(payload)
            request_id = None
            if getattr(self, "_forwarding_request_ids", False):
                request_id = uuid.uuid4().hex
                request["requestId"] = request_id
            write_lock = getattr(self, "_write_lock", self._lock)
            with write_lock:
                self._send(request)
            deadline = time.monotonic() + self._timeout

            def matches(candidate: Mapping[str, object]) -> bool:
                candidate_id = candidate.get("requestId")
                if request_id is None:
                    return candidate_id is None
                return candidate_id == request_id or (
                    candidate_id is None and candidate.get("event") == "error"
                )

            event = self._await_response(
                matches,
                deadline=deadline,
            )
            if event.get("event") == "error":
                raise self._forwarding_error(event)
            if event.get("event") != expected:
                raise ForwardingSidecarError(
                    f"expected forwarding event {expected!r}; got {event.get('event')!r}"
                )
            return event

    def status(self) -> tuple[tuple[str, ...], dict[str, int]]:
        event = self._request({"v": FORWARD_PROTOCOL_VERSION, "op": "status"}, "state")
        # Protocol v3 answers ``status`` with a ``state`` event whose
        # ``peers`` is the mapped-peer list ``[{peer, localPort}]``.  An
        # absent ``peers`` key is forgiven (treated as "no mapped peer
        # yet") for the same reason it always was; an explicit non-list is
        # still a protocol violation.
        raw_peers = event.get("peers")
        self.peers_reported = "peers" in event
        if "peers" not in event:
            raw_peers = []
        if not isinstance(raw_peers, list):
            raise self._status_failure("forwarding status has invalid peers")
        peers: list[str] = []
        mappings: dict[str, int] = {}
        for item in raw_peers:
            if not isinstance(item, dict):
                raise self._status_failure("forwarding status has invalid peers")
            peer, port = item.get("peer"), item.get("localPort")
            if not isinstance(peer, str) or not peer or not isinstance(port, int):
                raise self._status_failure("forwarding status has invalid peers")
            peers.append(peer)
            mappings[peer] = port
        return tuple(peers), mappings

    def map_peer(self, peer: str, address: str) -> int:
        if not isinstance(address, str) or not address.startswith("tc"):
            raise ForwardingSidecarError(
                "BAD_REQUEST: address must be a tailcat address"
            )
        event = self._request(
            {
                "v": FORWARD_PROTOCOL_VERSION,
                "op": "map-peer",
                "peer": peer,
                "address": address,
            },
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

    def _require_expose_capability(self) -> None:
        if not self._expose_capable:
            raise ForwardingSidecarError(
                "SIDECAR_EXPOSE_UNSUPPORTED: forwarding sidecar does not advertise "
                "expose, unexpose, and peer-key; install a sidecar with inbound exposure support"
            )

    def expose(self, exposure: Mapping[str, object]) -> dict[str, object]:
        self._require_expose_capability()
        normalized = _normalize_exposure(exposure)
        # Protocol v3 writes the Python-side "none" as "" on the wire.
        wire = dict(normalized)
        if wire["proxyProtocol"] == "none":
            wire["proxyProtocol"] = ""
        event = self._request(
            {"v": FORWARD_PROTOCOL_VERSION, "op": "expose", **wire},
            "exposed",
        )
        answer = _normalize_exposure(event)
        if answer != normalized:
            raise ForwardingSidecarError("exposed event does not match request")
        return answer

    def unexpose(self, port: int) -> None:
        self._require_expose_capability()
        event = self._request(
            {"v": FORWARD_PROTOCOL_VERSION, "op": "unexpose", "port": port},
            "unexposed",
        )
        if event.get("port") != port:
            raise ForwardingSidecarError("unexposed event does not match request")

    def peer_key(self, address: str) -> dict[str, object]:
        """The v3 replacement for v2 ``whois``: which nodekey owns ``addr``."""

        self._require_expose_capability()
        event = self._request(
            {"v": FORWARD_PROTOCOL_VERSION, "op": "peer-key", "addr": address},
            "peer-key",
        )
        if event.get("addr") != address or not isinstance(event.get("found"), bool):
            raise ForwardingSidecarError("peer-key event does not match request")
        keys = ("addr", "found", "key")
        return {key: event[key] for key in keys if key in event}

    def allow(self, keys: Sequence[str], allow_any: bool) -> dict[str, object]:
        """Replace the admission set; the sidecar drops clients not in it."""

        if any(
            not isinstance(key, str) or not key.startswith("nodekey:")
            for key in keys
        ):
            raise ForwardingSidecarError(
                "BAD_REQUEST: keys entries must be nodekey strings"
            )
        event = self._request(
            {
                "v": FORWARD_PROTOCOL_VERSION,
                "op": "allow",
                "keys": [str(key) for key in keys],
                "allowAny": bool(allow_any),
            },
            "allowed",
        )
        if not isinstance(event.get("count"), int) or not isinstance(
            event.get("allowAny"), bool
        ):
            raise ForwardingSidecarError("allowed event does not match request")
        return {"count": int(event["count"]), "allowAny": bool(event["allowAny"])}

    def _require_service_capability(self) -> None:
        if not self._service_capable:
            raise ServiceControlError(
                ipc_errors.SERVICE_SIDECAR_UNSUPPORTED,
                "forwarding sidecar does not support service connect",
            )
        if self._service_wire_failed:
            raise ServiceControlError(
                ipc_errors.SERVICE_UNAVAILABLE,
                "forwarding service control is unavailable",
            )

    def _service_request(
        self, payload: Mapping[str, object], *, operation: str, name: str | None
    ) -> dict[str, object] | bool:
        self._require_service_capability()
        request_id = payload["requestId"]
        assert isinstance(request_id, str)
        service_lock = getattr(self, "_service_lock", self._lock)
        with service_lock:
            self._require_service_capability()
            self._ensure_response_state()
            with self._response_condition:
                self._active_service_request_id = request_id
            try:
                write_lock = getattr(self, "_write_lock", self._lock)
                with write_lock:
                    self._send(payload)
                event = self._await_response(
                    lambda candidate: candidate.get("requestId") == request_id,
                    deadline=time.monotonic() + self._service_timeout,
                )
                return decode_service_response(
                    event,
                    request_id=request_id,
                    operation=operation,
                    name=name,
                    expected_mapping=payload if operation == "map-service" else None,
                )
            except ServiceControlError as error:
                if error.wire_failed:
                    self._service_wire_failed = True
                    self._terminate_owned_process()
                raise
            except ForwardingSidecarError as error:
                if str(error) == "forwarding sidecar response timed out":
                    self._ensure_response_state()
                    with self._response_condition:
                        stale = getattr(self, "_stale_service_requests", None)
                        if not isinstance(stale, dict):
                            stale = {}
                            self._stale_service_requests = stale
                        stale[request_id] = time.monotonic()
                        while len(stale) > _STALE_SERVICE_REQUEST_CAPACITY:
                            stale.pop(next(iter(stale)))
                        cutoff = (
                            time.monotonic()
                            - _STALE_SERVICE_REQUEST_MAX_AGE_SECONDS
                        )
                        for stale_id, recorded_at in tuple(stale.items()):
                            if recorded_at < cutoff:
                                stale.pop(stale_id, None)
                        self._response_condition.notify_all()
                    raise ServiceControlError(
                        ipc_errors.SERVICE_UNAVAILABLE,
                        "forwarding service request timed out",
                    ) from error
                self._service_wire_failed = True
                self._terminate_owned_process()
                raise ServiceControlError(
                    ipc_errors.SERVICE_UNAVAILABLE,
                    "forwarding service control is unavailable",
                    wire_failed=True,
                ) from error
            finally:
                with self._response_condition:
                    if self._active_service_request_id == request_id:
                        self._active_service_request_id = None
                    self._response_condition.notify_all()

    def map_service(
        self,
        name: str,
        device_id: str,
        address: str,
        server_public: str,
        remote_port: int,
        record_generation: int,
        local_port: int = 0,
    ) -> dict[str, object]:
        request_id = uuid.uuid4().hex
        payload = map_service_request(
            request_id,
            name,
            device_id,
            address,
            server_public,
            remote_port,
            record_generation,
            local_port,
        )
        result = self._service_request(payload, operation="map-service", name=name)
        assert isinstance(result, dict)
        return result

    def unmap_service(self, name: str) -> bool:
        request_id = uuid.uuid4().hex
        payload = unmap_service_request(request_id, name)
        result = self._service_request(payload, operation="unmap-service", name=name)
        assert isinstance(result, bool)
        return result

    def service_status(self) -> dict[str, object]:
        request_id = uuid.uuid4().hex
        payload = service_status_request(request_id)
        result = self._service_request(payload, operation="service-status", name=None)
        assert isinstance(result, dict)
        return result

    def close(self) -> None:
        with self._lock:
            if self._closing:
                return
            self._closing = True
            if self._process.poll() is None:
                try:
                    self._send({"v": FORWARD_PROTOCOL_VERSION, "op": "down"})
                    deadline = time.monotonic() + self._timeout
                    event = self._await_response(
                        lambda candidate: candidate.get("requestId") is None,
                        deadline=deadline,
                    )
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
