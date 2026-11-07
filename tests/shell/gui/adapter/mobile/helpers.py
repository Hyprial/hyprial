"""Shared fixtures for the mobile-access acceptance tests.

The derive callable is deterministic because the request-key derivation itself
is an external sidecar seam (the N-A2 contract); every other layer under test
-- the authoritative store, the gate, the host serving loop -- is the real
composition root.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from hyprial.shell.impl.gui.adapter.envelope import (
    ENVELOPE_VERSION,
    AuthGate,
    mac_text,
    request_digest,
)
from hyprial.shell.impl.gui.adapter.mobile.store import MobileAccessStore

OWNER = "agent:chloe.martin@example.com:dev000000001.local:mini-gui-worker"
OTHER_OWNER = "agent:chloe.martin@example.com:dev000000001.local:squire"
NODE = "node:dev000000001.local"
PEER_A = "peer-public-a"
PEER_B = "peer-public-b"
OPERATOR = "mate"
PERMISSIONS = ["sessions.read", "sessions.write", "sessions.events"]


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DeterministicDerive:
    """A deterministic stand-in for the sidecar derive op (protocol seam)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, role: str, peer: str) -> bytes:
        self.calls.append((role, peer))
        return hashlib.sha256(f"derive|{role}|{peer}".encode()).digest()


def make_store(
    tmp_path: Path,
    *,
    node_id: str = NODE,
    clock: Clock | None = None,
) -> tuple[MobileAccessStore, Clock]:
    clock = clock or Clock()
    store = MobileAccessStore(
        tmp_path / "mobile-access.sqlite3", node_id=node_id, clock=clock
    )
    return store, clock


def pair_device(
    store: MobileAccessStore,
    *,
    device_id: str = "dev-a",
    device_uid: str = "uid-a",
    name: str = "mate phone",
    owner: str = OWNER,
    client_public: str = PEER_A,
    permissions: list[str] | None = None,
    operator: str = OPERATOR,
) -> dict:
    code, _expires_at = store.issue_pairing_code(operator=operator)
    request = store.redeem_pairing_code(
        code=code,
        device_id=device_id,
        device_uid=device_uid,
        name=name,
        owner=owner,
        client_public=client_public,
        permissions=permissions or list(PERMISSIONS),
    )
    return store.approve_pairing_request(
        request["requestId"],
        operator=operator,
        node_id=store.node_id,
        device_id=device_id,
        client_public=client_public,
        fingerprint=request["fingerprint"],
    )


def make_gate(
    store: MobileAccessStore,
    *,
    derive: DeterministicDerive | None = None,
    node_id: str = NODE,
) -> AuthGate:
    return AuthGate(
        derive=derive or DeterministicDerive(),
        device_row=store.device_row,
        target_node_id=node_id,
        access=store,
        clock=store.clock,
    )


def signed_frame(
    derive: DeterministicDerive,
    *,
    device_id: str = "dev-a",
    generation: int = 1,
    client_public: str = PEER_A,
    target_node_id: str = NODE,
    operation: str = "capabilities.get",
    params: dict | None = None,
    author: str | None = None,
    ts: int = 1_000_000,
    nonce: str = "nonce-1",
    version: int = ENVELOPE_VERSION,
) -> dict:
    body: dict = {"operation": operation, "params": dict(params or {})}
    if author is not None:
        body["author"] = author
    session = body["params"].get("sessionId")
    space = session if isinstance(session, str) and session else ""
    digest = request_digest(body)
    key = derive("server", client_public)
    mac = hmac.new(
        key,
        mac_text(
            version,
            target_node_id,
            generation,
            ts,
            nonce,
            space,
            operation,
            digest,
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        **body,
        "auth": {
            "version": version,
            "deviceId": device_id,
            "deviceGeneration": generation,
            "targetNodeId": target_node_id,
            "ts": ts,
            "nonce": nonce,
            "mac": mac,
        },
    }
