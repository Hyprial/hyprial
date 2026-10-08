"""Closed service-connect wire codecs and typed authority commands."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from hyprial.kernel import ipc_errors

from .policy import FORWARD_PROTOCOL_VERSION, ForwardingSidecarError


_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_SERVER_PUBLIC = re.compile(r"^nodekey:[0-9a-f]{64}$")
_PRIVATE_ADDRESS_TEXT = re.compile(r"(?:^|[^a-zA-Z0-9])tc[A-Za-z0-9_-]{8,}")
_MAPPING_FIELDS = frozenset(
    {
        "name",
        "deviceId",
        "remotePort",
        "recordGeneration",
        "localPort",
        "activeConnections",
        "path",
        "pathDetail",
        "observedAtMs",
        "lastDialState",
        "lastDialMs",
        "lastError",
        "lastErrorAtMs",
    }
)
_GO_TO_SERVICE = {
    "BAD_REQUEST": ipc_errors.SERVICE_INVALID_TARGET,
    "NOT_UP": ipc_errors.SERVICE_UNAVAILABLE,
    "ALREADY_UP": ipc_errors.SERVICE_UNAVAILABLE,
    "KEY_FILE_INVALID": ipc_errors.SERVICE_UNAVAILABLE,
    "START_FAILED": ipc_errors.SERVICE_UNAVAILABLE,
    "PEER_UNKNOWN": ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
    "DIAL_FAILED": ipc_errors.SERVICE_UNAVAILABLE,
    "PORT_IN_USE": ipc_errors.SERVICE_PORT_IN_USE,
    "INTERNAL": ipc_errors.SERVICE_UNAVAILABLE,
}


class ServiceControlError(ForwardingSidecarError):
    """A safe, code-bearing service failure with no raw wire diagnostics."""

    def __init__(self, code: str, message: str, *, wire_failed: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.wire_failed = wire_failed


@dataclass(frozen=True, slots=True)
class _MapServiceSidecar:
    operation_id: str
    name: str
    device_id: str
    address: str = field(repr=False)
    server_public: str
    remote_port: int
    record_generation: int
    local_port: int


@dataclass(frozen=True, slots=True)
class _UnmapServiceSidecar:
    operation_id: str
    name: str


@dataclass(frozen=True, slots=True)
class _ServiceStatusSidecar:
    operation_id: str


_SERVICE_COMMANDS = (
    _MapServiceSidecar,
    _UnmapServiceSidecar,
    _ServiceStatusSidecar,
)


def _invalid_request() -> ServiceControlError:
    return ServiceControlError(
        ipc_errors.SERVICE_INVALID_TARGET, "service control request is invalid"
    )


def _invalid_response(*, wire_failed: bool = False) -> ServiceControlError:
    return ServiceControlError(
        ipc_errors.SERVICE_UNAVAILABLE,
        "forwarding service response is invalid",
        wire_failed=wire_failed,
    )


def _strict_int(value: object, *, minimum: int, maximum: int | None = None) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise _invalid_response()
    return value


def _request_int(value: object, *, minimum: int, maximum: int | None = None) -> int:
    try:
        return _strict_int(value, minimum=minimum, maximum=maximum)
    except ServiceControlError as error:
        raise _invalid_request() from error


def map_service_request(
    request_id: str,
    name: str,
    device_id: str,
    address: str,
    server_public: str,
    remote_port: int,
    record_generation: int,
    local_port: int,
) -> dict[str, object]:
    if (
        not isinstance(name, str)
        or not _IDENTIFIER.fullmatch(name)
        or not isinstance(device_id, str)
        or not _IDENTIFIER.fullmatch(device_id)
        or not isinstance(address, str)
        or not address.startswith("tc")
        or not isinstance(server_public, str)
        or not _SERVER_PUBLIC.fullmatch(server_public)
    ):
        raise _invalid_request()
    _request_int(remote_port, minimum=1, maximum=65535)
    _request_int(record_generation, minimum=1)
    _request_int(local_port, minimum=0, maximum=65535)
    if local_port != 0:
        _request_int(local_port, minimum=1024, maximum=65535)
    return {
        "v": FORWARD_PROTOCOL_VERSION,
        "op": "map-service",
        "requestId": request_id,
        "name": name,
        "deviceId": device_id,
        "address": address,
        "serverPublic": server_public,
        "remotePort": remote_port,
        "recordGeneration": record_generation,
        "localPort": local_port,
    }


def unmap_service_request(request_id: str, name: str) -> dict[str, object]:
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise _invalid_request()
    return {
        "v": FORWARD_PROTOCOL_VERSION,
        "op": "unmap-service",
        "requestId": request_id,
        "name": name,
    }


def service_status_request(request_id: str) -> dict[str, object]:
    return {
        "v": FORWARD_PROTOCOL_VERSION,
        "op": "service-status",
        "requestId": request_id,
    }


def _nullable_text(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise _invalid_response()
    if isinstance(value, str):
        if _PRIVATE_ADDRESS_TEXT.search(value) or "nodekey:" in value:
            raise _invalid_response()
        if any(ord(character) < 32 and character not in "\t" for character in value):
            raise _invalid_response()
    return value


def _nullable_timestamp(value: object) -> int | None:
    if value is None:
        return None
    return _strict_int(value, minimum=0)


def _nullable_duration(value: object) -> int | float | None:
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        raise _invalid_response()
    return value


def validate_service_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _MAPPING_FIELDS:
        raise _invalid_response()
    name = value["name"]
    device_id = value["deviceId"]
    if (
        not isinstance(name, str)
        or not _IDENTIFIER.fullmatch(name)
        or not isinstance(device_id, str)
        or not _IDENTIFIER.fullmatch(device_id)
    ):
        raise _invalid_response()
    _strict_int(value["remotePort"], minimum=1, maximum=65535)
    _strict_int(value["recordGeneration"], minimum=1)
    _strict_int(value["localPort"], minimum=1024, maximum=65535)
    _strict_int(value["activeConnections"], minimum=0)
    if value["path"] not in {"direct", "relay", "unknown"}:
        raise _invalid_response()
    if value["lastDialState"] not in {"unknown", "ok", "error"}:
        raise _invalid_response()
    _nullable_text(value["pathDetail"])
    _nullable_timestamp(value["observedAtMs"])
    _nullable_duration(value["lastDialMs"])
    _nullable_text(value["lastError"])
    _nullable_timestamp(value["lastErrorAtMs"])
    return dict(value)


def _service_failure(
    event: Mapping[str, object], *, request_id: str, operation: str, name: str | None
) -> ServiceControlError:
    if set(event) != {"v", "event", "requestId", "op", "name", "code", "message"}:
        return _invalid_response()
    expected_name: str | None = None if operation == "service-status" else name
    if (
        event.get("requestId") != request_id
        or event.get("op") != operation
        or event.get("name") != expected_name
        or event.get("code") not in _GO_TO_SERVICE
        or not isinstance(event.get("message"), str)
        or not event["message"]
    ):
        return _invalid_response()
    code = _GO_TO_SERVICE[str(event["code"])]
    return ServiceControlError(code, "forwarding service operation failed")


def decode_service_response(
    event: object,
    *,
    request_id: str,
    operation: str,
    name: str | None,
    expected_mapping: Mapping[str, object] | None = None,
) -> dict[str, object] | bool:
    if not isinstance(event, dict) or event.get("v") != FORWARD_PROTOCOL_VERSION:
        raise _invalid_response()
    # A reply for another request means the shared stream is out of sync.
    # Once correlation matches, malformed request-local content is isolated
    # to that request and must not tear down unrelated mesh mappings.
    if event.get("requestId") != request_id:
        raise _invalid_response(wire_failed=True)
    if event.get("event") == "service-failed":
        raise _service_failure(
            event, request_id=request_id, operation=operation, name=name
        )
    if operation == "map-service":
        if set(event) != {"v", "event", "requestId", "service"}:
            raise _invalid_response()
        if event.get("event") != "service-mapped" or event.get("requestId") != request_id:
            raise _invalid_response()
        service = validate_service_mapping(event.get("service"))
        if service["name"] != name:
            raise _invalid_response()
        if expected_mapping is None or any(
            service[key] != expected_mapping[key]
            for key in ("deviceId", "remotePort", "recordGeneration")
        ):
            raise _invalid_response()
        requested_port = expected_mapping["localPort"]
        if requested_port != 0 and service["localPort"] != requested_port:
            raise _invalid_response()
        return service
    if operation == "unmap-service":
        if set(event) != {"v", "event", "requestId", "name", "removed"}:
            raise _invalid_response()
        if (
            event.get("event") != "service-unmapped"
            or event.get("requestId") != request_id
            or event.get("name") != name
            or not isinstance(event.get("removed"), bool)
        ):
            raise _invalid_response()
        return bool(event["removed"])
    if set(event) != {"v", "event", "requestId", "services"}:
        raise _invalid_response()
    services = event.get("services")
    if (
        event.get("event") != "service-status"
        or event.get("requestId") != request_id
        or not isinstance(services, list)
    ):
        raise _invalid_response()
    return {"services": [validate_service_mapping(item) for item in services]}


def safe_service_error(error: BaseException) -> ServiceControlError:
    if isinstance(error, ServiceControlError):
        return error
    return ServiceControlError(
        ipc_errors.SERVICE_UNAVAILABLE,
        "forwarding service operation is unavailable",
    )
