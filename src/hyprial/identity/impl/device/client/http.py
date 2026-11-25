"""One HTTP/1.1 request per TLS connection to the device authority.

The client never uses a proxy, never follows a redirect, never negotiates
chunked or compressed framing, never keeps a connection alive and never sends a
second request.  A response is read under a hard 65536-byte cap and the socket is
closed before the caller sees data.
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import tempfile
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass, field
from math import isfinite
from uuid import UUID

from hyprial.kernel import harden_private_path
from hyprial.identity.impl.device.client.errors import (
    CLIENT_CONFIG_INVALID,
    OUTCOME_NOT_SENT,
    OUTCOME_REJECTED,
    OUTCOME_UNKNOWN,
    OUTCOME_UNRESOLVED,
    POST_SEND_EXCEPTION,
    REDIRECT_REFUSED,
    REPLAY_MISMATCH,
    REQUEST_INVALID,
    REQUEST_TOO_LARGE,
    RESPONSE_INVALID,
    RESPONSE_UNKNOWN,
    TransportClientError,
    _public_error_boundary,
)
from hyprial.identity.impl.device.client.mtls import ClientTlsMaterial

ENVELOPE_VERSION = "hyprial.transport.v1"
DEVICE_CERTIFICATE_ROUTE = "/v1/transport/device-certificate"
RETIRE_BINDING_ROUTE = "/v1/transport/retire-binding"
MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 65536
_MAX_RESPONSE_LENGTH_DIGITS = len(str(MAX_RESPONSE_BYTES))

DEVICE_CERTIFICATE_FIELDS = frozenset(
    {
        "ownerUserId",
        "owner",
        "bindingId",
        "deviceUid",
        "credentialGeneration",
        "certificatePem",
        "chainPem",
        "cn",
        "spkiSha256",
        "serial",
        "notBefore",
        "notAfter",
        "renewableUntil",
        "trustBundleId",
        "policyIssuer",
    }
)
RETIRE_BINDING_FIELDS = frozenset({"retired", "remoteEnforcementState", "operationId"})

# Frozen authority code -> (HTTP status, retryable).  The envelope must agree.
_CODE_CONTRACT = {
    "BAD_REQUEST": (400, False),
    "CSR_INVALID": (400, False),
    "TOKEN_INVALID": (401, False),
    "DEVICE_AUTH_REQUIRED": (401, False),
    "CREDENTIAL_EXPIRED": (401, False),
    "CREDENTIAL_REAUTH_REQUIRED": (401, False),
    "DEVICE_AUTH_MISMATCH": (403, False),
    "DEVICE_REVOKED": (403, False),
    "DEVICE_OWNER_MISMATCH": (403, False),
    "OWNER_DISABLED": (403, False),
    "DEVICE_BINDING_CONFLICT": (409, False),
    "IDEMPOTENCY_CONFLICT": (409, False),
    "KEY_GENERATION_STALE": (409, False),
    "KEY_GENERATION_EXHAUSTED": (409, False),
    "REQUEST_TOO_LARGE": (413, False),
    "REQUEST_HEADER_TOO_LARGE": (431, False),
    "AUTHORITY_UNAVAILABLE": (503, True),
    "ISSUER_INVALID": (503, False),
    "UNSAFE_AUTHORITY_STATE": (503, False),
    "AUTHORITY_ISSUER_MISMATCH": (503, False),
    "AUTHORITY_CONFIG_INVALID": (503, False),
}

_ORIGIN = re.compile(r"https://([A-Za-z0-9.-]+)(?::([0-9]{1,5}))?\Z")
_HEADER_NAME = re.compile(r"[A-Za-z0-9-]+\Z")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_HOST = re.compile(r"[A-Za-z0-9.-]+\Z")
_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z"
)
_DEVICE_UID = re.compile(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}\Z")


@dataclass(frozen=True, slots=True)
class DeviceAuthorityConfig:
    """Caller-supplied trusted configuration; there is no product default."""

    origin: str
    server_name: str
    trust_bundle_pem: bytes = field(repr=False)
    connect_timeout: float
    handshake_timeout: float
    read_timeout: float
    write_timeout: float
    client_tls: ClientTlsMaterial | None = field(default=None, repr=False)

    @_public_error_boundary
    def __post_init__(self) -> None:
        match = _ORIGIN.fullmatch(self.origin) if isinstance(self.origin, str) else None
        if match is None:
            raise _config_error()
        host, port_text = match.group(1), match.group(2)
        if _HOST.fullmatch(host) is None or host.lower() != host:
            raise _config_error()
        if port_text is not None and not 1 <= int(port_text) <= 65535:
            raise _config_error()
        if not isinstance(self.server_name, str) or self.server_name != host:
            raise _config_error()
        if not isinstance(self.trust_bundle_pem, bytes) or not self.trust_bundle_pem:
            raise _config_error()
        try:
            self.trust_bundle_pem.decode("ascii")
        except UnicodeError:
            raise _config_error() from None
        for value in (
            self.connect_timeout,
            self.handshake_timeout,
            self.read_timeout,
            self.write_timeout,
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value <= 0
            ):
                raise _config_error()
        if self.client_tls is not None and not isinstance(
            self.client_tls, ClientTlsMaterial
        ):
            raise _config_error()

    @property
    def host(self) -> str:
        return _ORIGIN.fullmatch(self.origin).group(1)

    @property
    def port(self) -> int:
        text = _ORIGIN.fullmatch(self.origin).group(2)
        return int(text) if text is not None else 443

    @property
    def host_header(self) -> str:
        return self.host if self.port == 443 else f"{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class _Attempt:
    route: str
    body: bytes
    resolved: bool


class DeviceTransportClient:
    """Replay-safe client for the two device-mTLS authority routes."""

    @_public_error_boundary
    def __init__(self, config: DeviceAuthorityConfig) -> None:
        if not isinstance(config, DeviceAuthorityConfig):
            raise _config_error()
        self._config = config
        self._attempts: dict[str, _Attempt] = {}

    @_public_error_boundary
    def enroll(self, staged) -> dict[str, object]:
        return self.request_certificate(
            operation_id=staged.operation_id,
            device_uid=staged.device_uid,
            expected_credential_generation=staged.expected_credential_generation,
            csr_pem=staged.csr_pem,
        )

    @_public_error_boundary
    def renew(self, staged, *, operation_id: str) -> dict[str, object]:
        return self.request_certificate(
            operation_id=operation_id,
            device_uid=staged.device_uid,
            expected_credential_generation=staged.expected_credential_generation,
            csr_pem=staged.csr_pem,
        )

    @_public_error_boundary
    def request_certificate(
        self,
        *,
        operation_id: str,
        device_uid: str,
        expected_credential_generation: int,
        csr_pem: str,
    ) -> dict[str, object]:
        operation_id = _uuid4(operation_id, "operationId")
        device_uid = _device_uid(device_uid)
        generation = _generation(expected_credential_generation, minimum=0)
        csr = _csr(csr_pem)
        body = _body(
            (
                ("version", ENVELOPE_VERSION),
                ("operationId", operation_id),
                ("deviceUid", device_uid),
                ("expectedCredentialGeneration", generation),
                ("csrPem", csr),
            )
        )
        return self._perform(DEVICE_CERTIFICATE_ROUTE, operation_id, body)

    @_public_error_boundary
    def retire(
        self,
        *,
        operation_id: str,
        binding_id: str,
        expected_credential_generation: int,
    ) -> dict[str, object]:
        operation_id = _uuid4(operation_id, "operationId")
        binding_id = _uuid4(binding_id, "bindingId")
        generation = _generation(expected_credential_generation, minimum=1)
        body = _body(
            (
                ("version", ENVELOPE_VERSION),
                ("operationId", operation_id),
                ("bindingId", binding_id),
                ("expectedCredentialGeneration", generation),
            )
        )
        return self._perform(RETIRE_BINDING_ROUTE, operation_id, body)

    def _perform(
        self, route: str, operation_id: str, body: bytes
    ) -> dict[str, object]:
        previous = self._guard_replay(route, operation_id, body)
        request = _request_bytes(self._config, route, body)
        self._attempts[operation_id] = _Attempt(route, body, False)
        try:
            data = _exchange(self._config, request, operation_id, route)
        except TransportClientError as error:
            if error.outcome == OUTCOME_NOT_SENT:
                if previous is None:
                    self._attempts.pop(operation_id, None)
                else:
                    self._attempts[operation_id] = previous
            else:
                self._attempts[operation_id] = _Attempt(
                    route, body, error.outcome == OUTCOME_REJECTED
                )
            raise
        self._attempts[operation_id] = _Attempt(route, body, True)
        return data

    def _guard_replay(
        self, route: str, operation_id: str, body: bytes
    ) -> _Attempt | None:
        existing = self._attempts.get(operation_id)
        if existing is not None:
            if existing.route != route or existing.body != body:
                raise TransportClientError(
                    REPLAY_MISMATCH,
                    retryable=False,
                    outcome=OUTCOME_NOT_SENT,
                    operation_id=operation_id,
                )
            return existing
        for other_id, other in self._attempts.items():
            if other.route == route and not other.resolved:
                raise TransportClientError(
                    OUTCOME_UNRESOLVED,
                    retryable=False,
                    outcome=OUTCOME_UNKNOWN,
                    operation_id=other_id,
                )
        return None


def _body(pairs: tuple[tuple[str, object], ...]) -> bytes:
    payload = json.dumps(
        dict(pairs), separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    encoded = payload.encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise TransportClientError(
            REQUEST_TOO_LARGE, retryable=False, outcome=OUTCOME_NOT_SENT
        )
    return encoded


def _request_bytes(
    config: DeviceAuthorityConfig, route: str, body: bytes
) -> bytes:
    head = (
        f"POST {route} HTTP/1.1\r\n"
        f"Host: {config.host_header}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    return head.encode("ascii") + body


def _exchange(
    config: DeviceAuthorityConfig,
    request: bytes,
    operation_id: str,
    route: str,
) -> dict[str, object]:
    context = _client_context(config)
    try:
        raw = socket.create_connection(
            (config.host, config.port), timeout=config.connect_timeout
        )
    except OSError:
        raise _unavailable(OUTCOME_NOT_SENT) from None
    tls: ssl.SSLSocket | None = None
    post_send_handoff = False
    try:
        try:
            raw.settimeout(config.handshake_timeout)
        except OSError:
            raise _unavailable(OUTCOME_NOT_SENT) from None
        try:
            tls = context.wrap_socket(raw, server_hostname=config.server_name)
        except ssl.SSLCertVerificationError:
            raise TransportClientError(
                "TRANSPORT_CHAIN_UNTRUSTED",
                retryable=False,
                outcome=OUTCOME_NOT_SENT,
                operation_id=operation_id,
            ) from None
        except (ssl.SSLError, OSError, ValueError):
            raise _unavailable(OUTCOME_NOT_SENT) from None
        try:
            tls.settimeout(config.write_timeout)
            tls.sendall(request)
        except (OSError, ssl.SSLError):
            raise _unknown(operation_id) from None
        post_send_handoff = True
        return _post_send(
            tls,
            config=config,
            operation_id=operation_id,
            route=route,
        )
    finally:
        if not post_send_handoff:
            _close_stream(tls if tls is not None else raw)


def _post_send(
    tls: ssl.SSLSocket,
    *,
    config: DeviceAuthorityConfig,
    operation_id: str,
    route: str,
) -> dict[str, object]:
    """Contain every operation after the first successful send in one place."""

    try:
        try:
            tls.settimeout(config.read_timeout)
            payload = _read_to_eof(tls, operation_id)
            return _decode_response(
                payload, operation_id=operation_id, route=route
            )
        finally:
            _close_stream(tls)
    except (KeyboardInterrupt, SystemExit):
        raise
    except TransportClientError:
        raise
    except BaseException as error:
        exception_class = type(error).__name__
        if (
            not exception_class.isascii()
            or not exception_class.isidentifier()
            or len(exception_class) > 128
        ):
            exception_class = "Exception"
    # Raise after leaving the handler so Python does not attach the redacted raw
    # exception as ``__context__`` on the public result.
    raise TransportClientError(
        RESPONSE_INVALID,
        retryable=True,
        outcome=OUTCOME_UNKNOWN,
        operation_id=operation_id,
        reason_code=POST_SEND_EXCEPTION,
        exception_class=exception_class,
    ) from None


def _close_stream(stream) -> None:
    try:
        stream.close()
    except OSError:
        pass


def _read_to_eof(tls: ssl.SSLSocket, operation_id: str) -> bytes:
    buffer = bytearray()
    while True:
        try:
            chunk = tls.recv(16384)
        except (OSError, ssl.SSLError):
            raise _unknown(operation_id) from None
        if not chunk:
            break
        buffer += chunk
        if len(buffer) > MAX_RESPONSE_BYTES:
            raise _unknown(operation_id) from None
    if not buffer:
        raise _unknown(operation_id)
    return bytes(buffer)


def _decode_response(
    payload: bytes, *, operation_id: str, route: str
) -> dict[str, object]:
    status, body = _parse_response(payload, operation_id)
    if 300 <= status <= 399:
        raise TransportClientError(
            REDIRECT_REFUSED,
            retryable=False,
            outcome=OUTCOME_UNKNOWN,
            http_status=status,
            operation_id=operation_id,
        )
    document = _strict_json(body, operation_id)
    if not isinstance(document, dict):
        raise _protocol_invalid(operation_id, status)
    if document.get("version") != ENVELOPE_VERSION:
        raise _protocol_invalid(operation_id, status)
    if document.get("operationId") != operation_id:
        raise _protocol_invalid(operation_id, status)
    if set(document) == {"ok", "version", "operationId", "data"} and (
        document["ok"] is True
    ):
        if status != 200:
            raise _protocol_invalid(operation_id, status)
        return _success_data(document["data"], route, operation_id, status)
    if set(document) == {
        "ok",
        "version",
        "operationId",
        "code",
        "retryable",
        "data",
    } and document["ok"] is False:
        if 200 <= status <= 299:
            raise _protocol_invalid(operation_id, status)
        return _raise_authority_error(document, status, operation_id)
    raise _protocol_invalid(operation_id, status)


def _success_data(
    data: object, route: str, operation_id: str, status: int
) -> dict[str, object]:
    if not isinstance(data, dict):
        raise _protocol_invalid(operation_id, status)
    if route == DEVICE_CERTIFICATE_ROUTE:
        if set(data) != DEVICE_CERTIFICATE_FIELDS:
            raise _protocol_invalid(operation_id, status)
        return data
    if set(data) != RETIRE_BINDING_FIELDS:
        raise _protocol_invalid(operation_id, status)
    if data["retired"] is not True:
        raise _protocol_invalid(operation_id, status)
    if data["operationId"] != operation_id:
        raise _protocol_invalid(operation_id, status)
    state = data["remoteEnforcementState"]
    if not isinstance(state, str) or not state or state != state.strip():
        raise _protocol_invalid(operation_id, status)
    return data


def _raise_authority_error(
    document: dict[str, object], status: int, operation_id: str
) -> dict[str, object]:
    if document["data"] is not None:
        raise _protocol_invalid(operation_id, status)
    code = document["code"]
    retryable = document["retryable"]
    if not isinstance(code, str) or not isinstance(retryable, bool):
        raise _protocol_invalid(operation_id, status)
    contract = _CODE_CONTRACT.get(code)
    if contract is None:
        raise _protocol_invalid(operation_id, status)
    expected_status, expected_retryable = contract
    if status != expected_status or retryable is not expected_retryable:
        raise _protocol_invalid(operation_id, status)
    raise TransportClientError(
        code,
        retryable=expected_retryable,
        outcome=OUTCOME_UNKNOWN if expected_retryable else OUTCOME_REJECTED,
        http_status=status,
        operation_id=operation_id,
    )


def _parse_response(payload: bytes, operation_id: str) -> tuple[int, bytes]:
    separator = payload.find(b"\r\n\r\n")
    if separator < 0:
        raise _unknown(operation_id)
    head = payload[:separator]
    body = payload[separator + 4 :]
    lines = head.split(b"\r\n")
    if not lines:
        raise _unknown(operation_id)
    status_line = lines[0]
    parts = status_line.split(b" ", 2)
    if len(parts) != 3 or parts[0] != b"HTTP/1.1":
        raise _protocol_invalid(operation_id, None)
    if len(parts[1]) != 3 or not parts[1].isdigit():
        raise _protocol_invalid(operation_id, None)
    status = int(parts[1])
    if not 100 <= status <= 599 or 100 <= status <= 199:
        raise _protocol_invalid(operation_id, status)
    # RFC 9112 section 4: both SP separators are mandatory, even for an
    # empty reason; reason octets are HTAB / SP / VCHAR / obs-text.
    if any((byte < 0x20 and byte != 0x09) or byte == 0x7F for byte in parts[2]):
        raise _protocol_invalid(operation_id, status)
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if not line or line[:1] in (b" ", b"\t"):
            raise _protocol_invalid(operation_id, status)
        name, separator, value = line.partition(b":")
        if separator != b":":
            # A field-line without its colon is not HTTP framing.
            raise _protocol_invalid(operation_id, status)
        if any(byte < 0x21 or byte > 0x7E for byte in name):
            raise _protocol_invalid(operation_id, status)
        text_name = name.decode("ascii")
        # Only SP and HTAB are optional whitespace in HTTP.  Python's
        # str.strip() would also remove FF, VT, NEL and NBSP, which are not
        # HTTP whitespace and must stay a rejection.
        stripped = value.strip(b" \t")
        if any(byte < 0x20 or byte > 0x7E for byte in stripped):
            raise _protocol_invalid(operation_id, status)
        if _HEADER_NAME.fullmatch(text_name) is None:
            raise _protocol_invalid(operation_id, status)
        headers.setdefault(text_name.lower(), []).append(stripped.decode("ascii"))
    for forbidden in (
        "transfer-encoding",
        "content-encoding",
        "upgrade",
        "trailer",
        "te",
    ):
        if forbidden in headers:
            raise _protocol_invalid(operation_id, status)
    connection = headers.get("connection", [])
    if len(connection) != 1 or [
        token.strip(" \t").lower() for token in connection[0].split(",")
    ] != ["close"]:
        raise _protocol_invalid(operation_id, status)
    declared = headers.get("content-length", [])
    if len(declared) != 1:
        raise _protocol_invalid(operation_id, status)
    declared_length = declared[0]
    if (
        len(declared_length) > _MAX_RESPONSE_LENGTH_DIGITS
        or _DECIMAL.fullmatch(declared_length) is None
    ):
        # Bound the representation, including any leading zeros, before int().
        # A response body cannot validly exceed MAX_RESPONSE_BYTES.
        raise _protocol_invalid(operation_id, status)
    try:
        expected = int(declared_length)
    except (ValueError, OverflowError):
        # Defence in depth: the length and decimal checks above are the fix.
        raise _protocol_invalid(operation_id, status) from None
    if expected > MAX_RESPONSE_BYTES:
        raise _unknown(operation_id, http_status=status)
    if len(body) > expected:
        raise _protocol_invalid(operation_id, status)
    if len(body) < expected:
        raise _unknown(operation_id, http_status=status)
    return status, body


def _strict_json(payload: bytes, operation_id: str) -> object:
    def _pairs(pairs):
        seen = set()
        for key, _value in pairs:
            if key in seen:
                raise _protocol_invalid(operation_id, None)
            seen.add(key)
        return dict(pairs)

    def _constant(_value):
        raise _protocol_invalid(operation_id, None)

    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_pairs,
        parse_constant=_constant,
    )


def _client_context(config: DeviceAuthorityConfig) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.options |= ssl.OP_NO_COMPRESSION
    try:
        context.set_alpn_protocols(["http/1.1"])
    except NotImplementedError:  # pragma: no cover - platform dependent
        pass
    try:
        context.load_verify_locations(
            cadata=config.trust_bundle_pem.decode("ascii")
        )
    except (ssl.SSLError, ValueError):
        raise _config_error() from None
    if config.client_tls is not None:
        try:
            with _client_chain_files(config.client_tls) as (certificate, key):
                context.load_cert_chain(certfile=certificate, keyfile=key)
        except TransportClientError:
            raise
        except (OSError, ssl.SSLError, TypeError, ValueError, UnicodeError):
            raise _config_error() from None
    return context


@contextmanager
def _client_chain_files(material: ClientTlsMaterial):
    directory: str | None = None
    try:
        directory = tempfile.mkdtemp(prefix="hyprial-mtls-")
        harden_private_path(Path(directory), mode=0o700)
        certificate = os.path.join(directory, "chain.pem")
        key = os.path.join(directory, "key.pem")
        for path, payload in (
            (certificate, material.certificate_chain_pem),
            (key, material.private_key_pem),
        ):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                _write_all(descriptor, payload)
            finally:
                _close_quietly(descriptor)
        yield certificate, key
    finally:
        if directory is not None:
            _discard_material_directory(directory)


def _write_all(descriptor: int, payload: bytes) -> None:
    """Write every byte: retry short writes, fail typed on zero progress."""

    written = 0
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            raise _config_error()
        written += count


def _discard_material_directory(directory: str) -> None:
    """Best effort: a cleanup failure never masks a completed exchange."""

    for name in ("chain.pem", "key.pem"):
        try:
            os.unlink(os.path.join(directory, name))
        except OSError:
            pass
    try:
        os.rmdir(directory)
    except OSError:
        pass


def _close_quietly(descriptor) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _uuid4(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or _UUID4.fullmatch(value) is None
        or str(UUID(value)) != value
    ):
        raise TransportClientError(
            REQUEST_INVALID,
            retryable=False,
            outcome=OUTCOME_NOT_SENT,
        )
    return value


def _device_uid(value: object) -> str:
    if not isinstance(value, str) or _DEVICE_UID.fullmatch(value) is None:
        raise TransportClientError(
            REQUEST_INVALID,
            retryable=False,
            outcome=OUTCOME_NOT_SENT,
        )
    return value


def _generation(value: object, *, minimum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value < 2**31
    ):
        raise TransportClientError(
            REQUEST_INVALID,
            retryable=False,
            outcome=OUTCOME_NOT_SENT,
        )
    return value


def _csr(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TransportClientError(
            REQUEST_INVALID, retryable=False, outcome=OUTCOME_NOT_SENT
        )
    try:
        encoded = value.encode("ascii")
    except UnicodeError:
        raise TransportClientError(
            REQUEST_INVALID, retryable=False, outcome=OUTCOME_NOT_SENT
        ) from None
    if len(encoded) > 16384:
        raise TransportClientError(
            REQUEST_TOO_LARGE, retryable=False, outcome=OUTCOME_NOT_SENT
        )
    return value


def _config_error() -> TransportClientError:
    return TransportClientError(
        CLIENT_CONFIG_INVALID, retryable=False, outcome=OUTCOME_NOT_SENT
    )


def _unavailable(outcome: str) -> TransportClientError:
    return TransportClientError(
        "AUTHORITY_UNAVAILABLE", retryable=True, outcome=outcome
    )


def _unknown(
    operation_id: str, *, http_status: int | None = None
) -> TransportClientError:
    return TransportClientError(
        RESPONSE_UNKNOWN,
        retryable=True,
        outcome=OUTCOME_UNKNOWN,
        http_status=http_status,
        operation_id=operation_id,
    )


def _protocol_invalid(
    operation_id: str, status: int | None
) -> TransportClientError:
    return TransportClientError(
        RESPONSE_INVALID,
        retryable=True,
        outcome=OUTCOME_UNKNOWN,
        http_status=status,
        operation_id=operation_id,
    )
