"""Typed, bounded native wire owner for one forwarding sidecar process."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hyprial.kernel import (
    SERVICE_CONTROL_TIMEOUT_SECONDS,
    ActorRuntime,
    ActorSpec,
    AdmissionResult,
    ipc_errors,
)

from .policy import ForwardingSidecarError
from .services import (
    ServiceControlError,
    _MapServiceSidecar,
    _SERVICE_COMMANDS,
    _ServiceStatusSidecar,
    _UnmapServiceSidecar,
    safe_service_error,
    validate_service_mapping,
)


@dataclass(frozen=True, slots=True)
class _StatusSidecar:
    operation_id: str


@dataclass(frozen=True, slots=True)
class _MapSidecarPeer:
    operation_id: str
    peer: str
    address: str
    slot: str


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
        self._connection_telemetry: dict[str, Any] | None = None
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
    def connection_telemetry(self) -> dict[str, Any] | None:
        with self._guard:
            return self._connection_telemetry

    @property
    def slots_reported(self) -> tuple[str, ...] | None:
        """The slot wire ids the sidecar reported, in request order.

        Read through to the wire owner rather than cached on the status
        path: the ``up`` answer already names the slots, and the tcp
        transport needs them before the first status poll.
        """

        value = getattr(self._controller, "slots_reported", None)
        if isinstance(value, tuple) and all(isinstance(item, str) for item in value):
            return value
        return None

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
                telemetry = getattr(self._controller, "connection_telemetry", None)
                with self._guard:
                    self._peers_reported = reported
                    self._connection_telemetry = telemetry
            elif isinstance(command, _MapSidecarPeer):
                value = self._controller.map_peer(
                    command.peer, command.address, command.slot
                )
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

    def status(self) -> tuple[tuple[str, ...], dict[str, str]]:
        result = self._call(_StatusSidecar(uuid.uuid4().hex))
        assert isinstance(result, tuple)
        return result

    def map_peer(self, peer: str, address: str, slot: str) -> str:
        """Bind ``peer`` onto the fixed ``slot`` (protocol v4).

        Returns the bound slot wire id (the resolved id on a tcp growth
        bind); the fixed slot pool is the only mapping shape.
        """
        result = self._call(_MapSidecarPeer(uuid.uuid4().hex, peer, address, slot))
        assert isinstance(result, str)
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
