"""Fixtures for the S3 TLS/loopback pairing-server tests."""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from hyprial.shell.impl.gui.adapter.mobile.pairing.server import PairingServer
from tests.shell.gui.adapter.mobile.helpers import Clock, make_store
from tests.shell.gui.adapter.mobile.pairing.server.support import (
    AuditRecorder,
    TlsMaterial,
    build_server,
    make_tls_material,
)


@pytest.fixture(scope="session")
def tls_material(tmp_path_factory: pytest.TempPathFactory) -> TlsMaterial:
    return make_tls_material(tmp_path_factory.mktemp("s3-tls"))


@pytest.fixture(scope="session")
def other_fingerprint(tmp_path_factory: pytest.TempPathFactory) -> str:
    return make_tls_material(
        tmp_path_factory.mktemp("s3-other-tls")
    ).fingerprint


@pytest.fixture
def audit_recorder() -> AuditRecorder:
    return AuditRecorder()


@pytest.fixture
def store(tmp_path: Path):
    store, _clock = make_store(tmp_path, clock=Clock(time.time()))
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def server_factory(tls_material: TlsMaterial, audit_recorder: AuditRecorder):
    """Start explicit servers on ephemeral loopback ports and close them."""

    servers: list[PairingServer] = []

    def make(
        *,
        authority: object,
        audit: object | None = None,
        **overrides: object,
    ) -> PairingServer:
        if audit is not None:
            overrides["audit"] = audit
        else:
            overrides.setdefault("audit", audit_recorder)
        server = build_server(authority, tls_material, **overrides)
        server.start()
        servers.append(server)
        return server

    try:
        yield make
    finally:
        for server in reversed(servers):
            server.close()


@pytest.fixture
def raw_socket():
    """A plain TCP socket helper, for handshake/plaintext failure tests."""

    sockets: list[socket.socket] = []

    def connect(server: PairingServer) -> socket.socket:
        sock = socket.create_connection(
            ("127.0.0.1", server.bound_port), timeout=3.0
        )
        sockets.append(sock)
        return sock

    try:
        yield connect
    finally:
        for sock in sockets:
            try:
                sock.close()
            except OSError:
                pass
