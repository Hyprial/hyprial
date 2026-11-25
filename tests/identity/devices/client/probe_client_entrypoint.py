#!/usr/bin/env python3
"""Raw-wire behavioral RED/GREEN probe for the device client entrypoint.

Run with
`uv run --with libcst==1.9.0 python tests/identity/devices/client/probe_client_entrypoint.py`.

Base `1d7f5c1b1`: the public identity surface has no device client entrypoint,
so the exact one-request exchange cannot be initiated: the probe records
`ENTRYPOINT_ABSENT`, zero TCP connections and exits 1. That is the
source-appropriate RED for a base that lacks the parser, not a pytest import
failure.

Green head: the entrypoint exists, opens exactly one connection, and converts
the failed TLS handshake into a typed, path-free error; the probe exits 0.

Output is one JSON object on stdout and never contains a path, key or PEM.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import hyprial.identity.interface as surface


def _ca_pem() -> bytes:
    now = datetime.now(timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "probe binding root")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


def _staged():
    home = Path(tempfile.mkdtemp(prefix="hyprial-probe-home-"))
    device_uid = uuid.uuid4().hex
    surface.write_device_record(
        home,
        surface.DeviceRecord(
            device_id="probe-device",
            owner="probe@example.invalid",
            server_public="nodekey:probe-server",
            client_public="nodekey:probe-client",
            device_uid=device_uid,
            name="probe",
        ),
    )
    return surface.stage_transport_credential(
        home,
        operation_id=str(uuid.uuid4()),
        owner="probe@example.invalid",
        device_uid=device_uid,
        expected_credential_generation=0,
    )


def main() -> int:
    record: dict[str, object] = {
        "entrypointPresent": False,
        "connections": 0,
        "errorType": None,
        "errorCode": None,
        "result": None,
    }
    entrypoint = getattr(surface, "DeviceTransportClient", None)
    if entrypoint is None:
        record["result"] = "ENTRYPOINT_ABSENT"
        print(json.dumps(record, sort_keys=True))
        return 1
    record["entrypointPresent"] = True
    config_type = getattr(surface, "DeviceAuthorityConfig", None)
    error_type = getattr(surface, "TransportClientError", None)
    if config_type is None or error_type is None:
        record["result"] = "ENTRYPOINT_INCOMPLETE"
        print(json.dumps(record, sort_keys=True))
        return 2
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def _accept() -> None:
        listener.settimeout(5.0)
        try:
            connection, _address = listener.accept()
        except (socket.timeout, OSError):
            return
        record["connections"] = 1
        connection.close()

    thread = threading.Thread(target=_accept, daemon=True)
    thread.start()
    staged = _staged()
    config = config_type(
        origin=f"https://127.0.0.1:{port}",
        server_name="127.0.0.1",
        trust_bundle_pem=_ca_pem(),
        connect_timeout=2.0,
        handshake_timeout=2.0,
        read_timeout=2.0,
        write_timeout=2.0,
    )
    try:
        entrypoint(config).enroll(staged)
    except error_type as error:  # typed, path-free by contract
        record["errorType"] = "TransportClientError"
        record["errorCode"] = error.code
        record["errorOutcome"] = error.outcome
    except Exception as error:  # noqa: BLE001 - recorded as UNEXPECTED
        record["errorType"] = type(error).__name__
    finally:
        thread.join(timeout=5.0)
        listener.close()
    accepted = (
        "TransportClientError" in str(record["errorType"])
        and record["errorCode"] == "AUTHORITY_UNAVAILABLE"
        and record["connections"] == 1
    )
    record["result"] = "TYPED_SINGLE_CONNECTION" if accepted else "UNEXPECTED"
    print(json.dumps(record, sort_keys=True))
    return 0 if accepted else 2


if __name__ == "__main__":
    sys.exit(main())
