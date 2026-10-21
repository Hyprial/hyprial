"""The forwarding sidecar process controller (closed v4 JSON-lines wire)."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Self
import uuid
from hyprial.daemon.impl.network.tailcat.binary import no_console_window_kwargs
from hyprial.kernel import (
    FORWARDING_COMMAND_ENV,
    FORWARDING_UP_ENV,
    SERVICE_CONTROL_TIMEOUT_SECONDS,
    ipc_errors,
)

from .authority import (  # noqa: F401  (re-exported; the authority moved out)
    ForwardingControllerAuthority,
    _AllowSidecar,
    _CloseSidecar,
    _ExposeSidecar,
    _MapSidecarPeer,
    _PeerKeySidecar,
    _SidecarReply,
    _StatusSidecar,
    _UnexposeSidecar,
    _UnmapSidecarPeer,
)
from .policy import (
    FORWARD_PROTOCOL_VERSION,
    ForwardingSidecarError,
    _PROXY_KEYS,
    _normalize_exposure,
)
from .services import (
    ServiceControlError,
    decode_service_response,
    map_service_request,
    service_status_request,
    unmap_service_request,
)


_DEFAULT_TIMEOUT = 10.0
_STDERR_TAIL_LINES = 12
_STALE_SERVICE_REQUEST_CAPACITY = 64
_STALE_SERVICE_REQUEST_MAX_AGE_SECONDS = 60.0
_RESPONSE_BUFFER_CAPACITY = 128
_RESPONSE_BUFFER_MAX_AGE_SECONDS = 60.0




def _child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith("TS_") and key not in _PROXY_KEYS
    }


class ForwardingSidecarController:
    """Own one candidate sidecar process and its closed v4 JSON-lines wire."""

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
            raise ValueError("forwarding sidecar up request must be protocol v4")
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
        # The slot wire ids this sidecar incarnation reported (state
        # ``slots``, request order).  On the tcp transport they carry the
        # kernel-assigned ports the daemon dials; None until a state
        # event names them.
        self.slots_reported: tuple[str, ...] | None = None
        self.connection_telemetry: dict[str, Any] | None = None
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
            **no_console_window_kwargs(),
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
            raise ForwardingSidecarError("forwarding sidecar emitted a non-v4 event")
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
            raise ForwardingSidecarError("forwarding sidecar v4 hello mismatch")
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
                # Protocol v4: the Running state must carry serverPublic,
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
                self._note_slots_reported(event)
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

    def _note_slots_reported(self, event: Mapping[str, object]) -> None:
        """Remember the slot ids a ``state`` event names (tcp transport)."""

        slots = event.get("slots")
        if isinstance(slots, list) and all(isinstance(item, str) for item in slots):
            self.slots_reported = tuple(slots)

    def status(self) -> tuple[tuple[str, ...], dict[str, str]]:
        event = self._request({"v": FORWARD_PROTOCOL_VERSION, "op": "status"}, "state")
        self._note_slots_reported(event)
        # Protocol v4 answers ``status`` with a ``state`` event whose
        # ``peers`` entries are ``{peer, slot}``.  An absent ``peers`` key
        # is forgiven (treated as "no mapped peer yet") for the same
        # reason it always was; an explicit non-list is still a protocol
        # violation.
        raw_peers = event.get("peers")
        self.peers_reported = "peers" in event
        self.connection_telemetry = parse_connection_telemetry(
            event.get("connectionTelemetry")
        )
        self.derp_map_url, self.derp_map_source = parse_derp_map(event)
        if "peers" not in event:
            raw_peers = []
        if not isinstance(raw_peers, list):
            raise self._status_failure("forwarding status has invalid peers")
        peers: list[str] = []
        mappings: dict[str, str] = {}
        for item in raw_peers:
            if not isinstance(item, dict):
                raise self._status_failure("forwarding status has invalid peers")
            peer, slot = item.get("peer"), item.get("slot")
            # The fixed slot pool is the only mapping shape (no rollback to
            # per-peer loopback ports): every peers entry names its slot.
            if (
                not isinstance(peer, str)
                or not peer
                or not isinstance(slot, str)
                or not slot
                or "localPort" in item
            ):
                raise self._status_failure("forwarding status has invalid peers")
            mappings[peer] = slot
            peers.append(peer)
        return tuple(peers), mappings

    def map_peer(self, peer: str, address: str, slot: str) -> str:
        """Bind ``peer`` at ``address`` onto the fixed ``slot``.

        Protocol v4: ``peer-mapped`` answers with the slot (never a
        ``localPort``) and this returns its wire id.  A slot is an
        absolute socket path (unix transport) or ``tcp:127.0.0.1:<port>``
        (tcp transport -- the Windows scheme); port 0 asks the sidecar to
        open a new kernel-assigned listener (pool growth), and the answer
        then names the resolved id instead of echoing the request.
        """
        if not isinstance(address, str) or not address.startswith("tc"):
            raise ForwardingSidecarError(
                "BAD_REQUEST: address must be a tailcat address"
            )
        if not isinstance(slot, str) or not (
            slot.startswith("/") or slot.startswith("tcp:")
        ):
            raise ForwardingSidecarError(
                "BAD_REQUEST: slot must be an absolute socket path "
                "or a tcp:127.0.0.1:<port> spec"
            )
        event = self._request(
            {
                "v": FORWARD_PROTOCOL_VERSION,
                "op": "map-peer",
                "peer": peer,
                "address": address,
                "slot": slot,
            },
            "peer-mapped",
        )
        answered = event.get("slot")
        grow = slot.startswith("tcp:") and slot.endswith(":0")
        matches = (
            isinstance(answered, str) and answered.startswith("tcp:")
            if grow
            else answered == slot
        )
        if event.get("peer") != peer or not matches or "localPort" in event:
            raise ForwardingSidecarError("peer-mapped event does not match request")
        assert isinstance(answered, str)
        return answered

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
        # Protocol v4 writes the Python-side "none" as "" on the wire.
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
        """The v4 replacement for v2 ``whois``: which nodekey owns ``addr``."""

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


_BASE_ERRORS = {"eof", "reset", "timeout", "canceled"}
_CLOSE_SIDES = {"local", "peer", "both"}
_MAX_SLOTS = 256
_MAX_OTHER_LENGTH = 40


def _integer(value: object, *, maximum: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
        and (maximum is None or value <= maximum)
    )


def _error_class(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return value in _BASE_ERRORS or (
        value.startswith("other:")
        and 6 < len(value) <= 6 + _MAX_OTHER_LENGTH
        and all(character.isalnum() or character in "._-" for character in value[6:])
    )


def _invalid() -> ForwardingSidecarError:
    return ForwardingSidecarError("forwarding status has invalid connection telemetry")


def parse_connection_telemetry(value: object) -> dict[str, Any] | None:
    """Return a bounded, detached projection or reject malformed telemetry."""

    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"ringCapacity", "slots"}:
        raise _invalid()
    capacity, slots = value["ringCapacity"], value["slots"]
    if not _integer(capacity, maximum=256) or capacity == 0:
        raise _invalid()
    if not isinstance(slots, list) or len(slots) > _MAX_SLOTS:
        raise _invalid()
    normalized_slots: list[dict[str, Any]] = []
    for item in slots:
        base_keys = {"peer", "mapPort", "closedCount", "errorCounts", "recent"}
        if not isinstance(item, dict) or set(item) not in (
            base_keys,
            base_keys | {"slot"},
        ):
            raise _invalid()
        peer = item["peer"]
        slot, port = _parse_telemetry_identity(item)
        counters, recent = item["errorCounts"], item["recent"]
        if (
            not isinstance(peer, str)
            or not peer
            or len(peer) > 512
            or not _integer(item["closedCount"])
            or not isinstance(counters, dict)
            or not all(
                _error_class(key) and _integer(count)
                for key, count in counters.items()
            )
            or not isinstance(recent, list)
            or len(recent) > capacity
        ):
            raise _invalid()
        normalized = {
            "peer": peer,
            "mapPort": port,
            "closedCount": item["closedCount"],
            "errorCounts": dict(counters),
            "recent": [
                _parse_close(close, peer=peer, slot=slot, port=port)
                for close in recent
            ],
        }
        if slot is not None:
            normalized["slot"] = slot
        normalized_slots.append(normalized)
    return {"ringCapacity": capacity, "slots": normalized_slots}


def _parse_telemetry_identity(
    value: dict[str, Any],
) -> tuple[str | None, int | None]:
    slot = value.get("slot")
    if "slot" in value and (not isinstance(slot, str) or not slot):
        raise _invalid()
    port = value["mapPort"]
    if port is None:
        if slot is None:
            raise _invalid()
    elif not _integer(port, maximum=65535) or port == 0:
        raise _invalid()
    return slot, port


def _parse_close(
    value: object, *, peer: str, slot: str | None, port: int | None
) -> dict[str, Any]:
    base_keys = {
        "peer", "mapPort", "bytesIn", "bytesOut", "durationMs", "closeSide", "errors"
    }
    if not isinstance(value, dict) or set(value) not in (
        base_keys,
        base_keys | {"slot"},
    ):
        raise _invalid()
    close_slot, close_port = _parse_telemetry_identity(value)
    errors = value["errors"]
    if (
        value["peer"] != peer
        or close_slot != slot
        or close_port != port
        or not _integer(value["bytesIn"])
        or not _integer(value["bytesOut"])
        or not _integer(value["durationMs"])
        or not isinstance(value["closeSide"], str)
        or value["closeSide"] not in _CLOSE_SIDES
        or not isinstance(errors, dict)
        or set(errors) != {"in", "out"}
        or not _error_class(errors["in"])
        or not _error_class(errors["out"])
    ):
        raise _invalid()
    normalized = {
        "peer": peer,
        "mapPort": port,
        "bytesIn": value["bytesIn"],
        "bytesOut": value["bytesOut"],
        "durationMs": value["durationMs"],
        "closeSide": value["closeSide"],
        "errors": {"in": errors["in"], "out": errors["out"]},
    }
    if slot is not None:
        normalized["slot"] = slot
    return normalized


def parse_derp_map(event: dict[str, object]) -> tuple[str | None, str | None]:
    """Return the sidecar-reported DERP map URL and source, if any."""

    raw_url = event.get("derpMapURL")
    raw_source = event.get("derpMapSource")
    return (
        raw_url if isinstance(raw_url, str) else None,
        raw_source if isinstance(raw_source, str) else None,
    )
