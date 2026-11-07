"""Pairing protocol/state-machine tests with a pure in-memory authority.

The fake authority below models the S2 transaction boundary only for S1
tests: it is not a store, it is not the operator approval path, and it never
grants sessions.  It exists to prove the pure state machine's idempotency,
conflict, expiry and fail-closed behaviour without touching SQLite or host.py.
"""

from __future__ import annotations

import copy
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from hyprial.shell.impl.gui.adapter.mobile import models
from hyprial.shell.impl.gui.adapter.mobile.pairing import (
    APPROVED,
    EXPIRED,
    PENDING,
    REVOKED,
    AuthorityError,
    ClientPairingState,
    HostPairingState,
    PairingError,
    b64url_encode,
    host_key_fingerprint,
    pairing_grants,
    project_status,
)
from hyprial.shell.impl.gui.adapter.mobile.pairing import protocol as pairing_protocol

NODE = "node:dev000000001.local"
OTHER_NODE = "node:other.local"
PIN = "sha256:" + "ab" * 32
OWNER = "agent:chloe.martin@example.com:dev000000001.local:mini-gui-worker"
VALID_CODE = "ABCDEFGHJK"
DEVICE_ID = "dev-a"
DEVICE_UID = "uid-a"
DEVICE_NAME = "mate phone"
PERMISSIONS = ["sessions.read", "sessions.write"]

CLIENT_PRIVATE_KEY = ec.derive_private_key(int("11" * 32, 16), ec.SECP256R1())
OTHER_CLIENT_PRIVATE_KEY = ec.derive_private_key(
    int("44" * 32, 16), ec.SECP256R1()
)
HOST_SIGNING_PRIVATE_KEY = ec.derive_private_key(
    int("22" * 32, 16), ec.SECP256R1()
)
OTHER_SIGNING_PRIVATE_KEY = ec.derive_private_key(
    int("55" * 32, 16), ec.SECP256R1()
)
HOST_DERIVE_PRIVATE_KEY = ec.derive_private_key(
    int("33" * 32, 16), ec.SECP256R1()
)
PAIRING_ATTEMPT_ID = b64url_encode(bytes(range(64, 96)))


def _raw(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


OTHER_CLIENT_PUBLIC = b64url_encode(_raw(OTHER_CLIENT_PRIVATE_KEY.public_key()))


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeAuthority:
    """A transactional stand-in for the S2 PairingAuthority implementation."""

    def __init__(self, clock: Clock, *, node_id: str = NODE) -> None:
        self.clock = clock
        self.node_id = node_id
        self._lock = threading.Lock()
        self._code_expires_at = clock.now + models.PAIRING_CODE_TTL_SECONDS
        self._consumed = False
        self._requests: dict[str, dict] = {}
        self._attempts: dict[str, tuple[tuple, tuple, str]] = {}
        self._generation = 0
        self.fail_next: AuthorityError | None = None
        self.session_grants: list[dict] = []

    def redeem_pairing_code(
        self,
        *,
        code: str | None,
        device_id: str,
        device_uid: str,
        name: str,
        owner: str,
        client_public: str,
        permissions: list[str],
        pairing_attempt_id: str,
        mode: str,
        now: float | None = None,
    ) -> dict:
        now = self.clock.now if now is None else now
        with self._lock:
            if self.fail_next is not None:
                error = self.fail_next
                self.fail_next = None
                raise error

            base = (
                device_id,
                device_uid,
                name,
                owner,
                client_public,
                tuple(sorted(permissions)),
            )
            normalized_code = models.normalize_pairing_code(code)
            start = base + (normalized_code,)

            existing = self._attempts.get(pairing_attempt_id)
            if existing is not None:
                stored_start, stored_base, request_id = existing
                expected = start if mode == "start" else base
                stored = stored_start if mode == "start" else stored_base
                if expected != stored:
                    raise AuthorityError(
                        "pairing-attempt-conflict",
                        "pairingAttemptId was already used with other fields",
                    )
                return copy.deepcopy(self._requests[request_id])

            if mode == "resume":
                raise AuthorityError(
                    "pairing-request-unknown",
                    "no request exists for this pairingAttemptId",
                )
            if now >= self._code_expires_at:
                raise AuthorityError("pairing-code-expired", "pairing code expired")
            if self._consumed:
                raise AuthorityError(
                    "pairing-code-consumed", "pairing code already consumed"
                )
            if normalized_code != VALID_CODE:
                raise AuthorityError("pairing-code-invalid", "invalid pairing code")

            self._consumed = True
            request_id = "pair-" + hashlib.sha256(
                (pairing_attempt_id + "|" + str(len(self._requests))).encode()
            ).hexdigest()[:32]
            fingerprint = models.pairing_fingerprint(
                node_id=self.node_id,
                device_id=device_id,
                device_uid=device_uid,
                name=name,
                owner=owner,
                client_public=client_public,
                permissions=list(permissions),
            )
            record = {
                "requestId": request_id,
                "nodeId": self.node_id,
                "fingerprint": fingerprint,
                "requestedAt": int(now),
                "expiresAt": int(
                    min(
                        now + models.PAIRING_REQUEST_TTL_SECONDS,
                        self._code_expires_at,
                    )
                ),
                "status": PENDING,
                "deviceId": device_id,
                "deviceUid": device_uid,
                "name": name,
                "owner": owner,
                "clientPublic": client_public,
                "permissions": sorted(set(permissions)),
                "approvedAt": None,
                "rejectedAt": None,
            }
            self._requests[request_id] = record
            self._attempts[pairing_attempt_id] = (start, base, request_id)
            return copy.deepcopy(record)

    def pairing_status(
        self,
        request_id: str,
        *,
        pairing_attempt_id: str,
        now: float | None = None,
    ) -> dict:
        with self._lock:
            attempt = self._attempts.get(pairing_attempt_id)
            if attempt is None or attempt[2] != request_id:
                raise AuthorityError(
                    "pairing-request-unknown",
                    "pairing request is unknown",
                )
            record = self._requests.get(request_id)
            if record is None:
                raise AuthorityError(
                    "pairing-request-unknown", "pairing request is unknown"
                )
            return copy.deepcopy(record)

    # Test-only local operator simulation.  This is never a wire operation.
    def approve(self, request_id: str, *, generation: int = 1) -> dict:
        with self._lock:
            record = self._requests[request_id]
            assert record["status"] == PENDING
            self._generation = generation
            record["status"] = APPROVED
            record["approvedAt"] = int(self.clock.now)
            record["device"] = {
                "deviceId": record["deviceId"],
                "deviceGeneration": generation,
                "targetNodeId": self.node_id,
                "clientPublic": record["clientPublic"],
                "owner": record["owner"],
                "permissions": list(record["permissions"]),
                "approvedAt": int(self.clock.now),
                "status": "active",
                "revokedAt": None,
            }
            return copy.deepcopy(record)

    def revoke(self, request_id: str) -> dict:
        with self._lock:
            record = self._requests[request_id]
            record["device"]["status"] = "revoked"
            record["device"]["revokedAt"] = int(self.clock.now)
            record["revokedAt"] = int(self.clock.now)
            return copy.deepcopy(record)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return sum(
                1 for record in self._requests.values() if record["status"] == PENDING
            )


def _objects(
    clock: Clock | None = None,
    *,
    client_public_key: ec.EllipticCurvePublicKey | None = None,
) -> tuple[Clock, FakeAuthority, HostPairingState, ClientPairingState]:
    clock = clock or Clock()
    authority = FakeAuthority(clock)
    host = HostPairingState(
        node_id=NODE,
        host_key_fingerprint=PIN,
        host_signing_key=HOST_SIGNING_PRIVATE_KEY,
        host_derive_key=HOST_DERIVE_PRIVATE_KEY,
        authority=authority,
        clock=clock,
    )
    client = ClientPairingState(
        node_id=NODE,
        host_key_fingerprint=PIN,
        host_public_key=client_public_key
        or HOST_SIGNING_PRIVATE_KEY.public_key(),
        client_private_key=CLIENT_PRIVATE_KEY,
        clock=clock,
    )
    return clock, authority, host, client


def _bootstrap(
    clock: Clock | None = None,
    *,
    client_public_key: ec.EllipticCurvePublicKey | None = None,
) -> tuple[
    Clock,
    FakeAuthority,
    HostPairingState,
    ClientPairingState,
    dict,
    dict,
]:
    clock, authority, host, client = _objects(
        clock, client_public_key=client_public_key
    )
    hello = client.hello()
    challenge = host.challenge(hello)
    return clock, authority, host, client, hello, challenge


def _redeem(
    client: ClientPairingState,
    *,
    code: str = VALID_CODE,
    attempt: str = PAIRING_ATTEMPT_ID,
    device_id: str = DEVICE_ID,
    device_uid: str = DEVICE_UID,
    name: str = DEVICE_NAME,
    owner: str = OWNER,
    permissions: list[str] | None = None,
) -> dict:
    return client.redeem_start(
        code=code,
        device_id=device_id,
        device_uid=device_uid,
        name=name,
        owner=owner,
        permissions=permissions or list(PERMISSIONS),
        pairing_attempt_id=attempt,
    )


@dataclass
class Pairing:
    clock: Clock
    authority: FakeAuthority
    host: HostPairingState
    client: ClientPairingState
    hello: dict
    challenge: dict
    redeem: dict
    response: dict


def _setup(
    *,
    code: str = VALID_CODE,
    attempt: str = PAIRING_ATTEMPT_ID,
) -> Pairing:
    clock, authority, host, client, hello, challenge = _bootstrap()
    client.accept_challenge(challenge)
    redeem = _redeem(client, code=code, attempt=attempt)
    response = host.redeem(hello, challenge, redeem)
    client.accept_status(response)
    return Pairing(
        clock, authority, host, client, hello, challenge, redeem, response
    )


def _pairing_error(callable_) -> PairingError:
    with pytest.raises(PairingError) as caught:
        callable_()
    return caught.value


def test_pending_status_has_zero_business_authority() -> None:
    pairing = _setup()
    response = pairing.response
    assert response["status"] == PENDING
    assert response["pollAfterMs"] == 2_000
    assert set(response) == {
        "v",
        "type",
        "requestId",
        "nodeId",
        "fingerprint",
        "requestedAt",
        "expiresAt",
        "status",
        "pollAfterMs",
    }
    assert "device" not in response
    assert "permissions" not in response
    assert "token" not in response
    assert "grant" not in response
    assert pairing_grants(response) == frozenset()
    assert pairing.authority.session_grants == []
    assert pairing.authority.pending_count == 1


def test_approved_then_revoked_status_projection() -> None:
    pairing = _setup()
    request_id = pairing.response["requestId"]
    pairing.authority.approve(request_id)
    accepted = pairing.client.accept_status(
        pairing.host.status(
            pairing.challenge, pairing.client.status_query(request_id)
        )
    )
    assert accepted["status"] == APPROVED
    device = accepted["device"]
    assert device["deviceGeneration"] == 1
    assert device["targetNodeId"] == NODE
    assert device["clientPublic"] == b64url_encode(
        _raw(CLIENT_PRIVATE_KEY.public_key())
    )
    assert device["permissions"] == sorted(PERMISSIONS)
    assert "pollAfterMs" not in accepted
    assert pairing_grants(accepted) == frozenset()

    pairing.authority.revoke(request_id)
    revoked = pairing.client.accept_status(
        pairing.host.status(
            pairing.challenge, pairing.client.status_query(request_id)
        )
    )
    assert revoked["status"] == REVOKED
    assert revoked["device"]["status"] == "revoked"
    assert revoked["device"]["revokedAt"] is not None
    assert pairing_grants(revoked) == frozenset()
    with pytest.raises(PairingError):
        pairing.client.status_query(request_id)


def test_wrong_node_and_pin_are_host_mismatch() -> None:
    _, authority, host, client, hello, challenge = _bootstrap()
    wrong_node = dict(challenge, nodeId=OTHER_NODE)
    assert _pairing_error(lambda: client.accept_challenge(wrong_node)).code == (
        "pairing-host-mismatch"
    )
    wrong_pin = dict(challenge, hostKeyFingerprint="sha256:" + "00" * 32)
    assert _pairing_error(lambda: client.accept_challenge(wrong_pin)).code == (
        "pairing-host-mismatch"
    )
    assert authority.pending_count == 0
    assert hello["nodeId"] == NODE


def test_wrong_host_key_is_host_mismatch() -> None:
    _, authority, host, client = _objects(
        client_public_key=OTHER_SIGNING_PRIVATE_KEY.public_key()
    )
    hello = client.hello()
    challenge = host.challenge(hello)
    assert _pairing_error(lambda: client.accept_challenge(challenge)).code == (
        "pairing-host-mismatch"
    )
    assert authority.pending_count == 0


def test_challenge_echo_protocol_and_profile_are_strict() -> None:
    _, _authority, _host, client, _hello, challenge = _bootstrap()
    wrong_echo = dict(
        challenge, clientNonce=b64url_encode(b"\xAA" * 32)
    )
    assert _pairing_error(lambda: client.accept_challenge(wrong_echo)).code == (
        "pairing-frame-invalid"
    )
    wrong_public_echo = dict(
        challenge, clientPublic=b64url_encode(_raw(OTHER_CLIENT_PRIVATE_KEY.public_key()))
    )
    assert _pairing_error(
        lambda: client.accept_challenge(wrong_public_echo)
    ).code == "pairing-frame-invalid"

    wrong_protocol = dict(challenge, protocol="m11-pairing/0")
    assert _pairing_error(
        lambda: client.accept_challenge(wrong_protocol)
    ).code == "pairing-protocol-unsupported"

    wrong_profile = dict(challenge, deriveProfile="p256-hkdf-sha256-v0")
    assert _pairing_error(
        lambda: client.accept_challenge(wrong_profile)
    ).code == "pairing-protocol-unsupported"


def test_challenge_signature_tamper_is_rejected() -> None:
    _, authority, host, client, hello, challenge = _bootstrap()
    tampered = dict(challenge, signature=b64url_encode(b"\x00" * 64))
    assert _pairing_error(lambda: client.accept_challenge(tampered)).code == (
        "pairing-host-mismatch"
    )
    assert authority.pending_count == 0
    assert hello["clientNonce"] == challenge["clientNonce"]


def test_channel_binding_and_client_proof_tamper_are_rejected() -> None:
    _, authority, host, client, hello, challenge = _bootstrap()
    client.accept_challenge(challenge)
    redeem = _redeem(client)

    wrong_binding = dict(redeem, channelBinding="sha256:" + "00" * 32)
    assert _pairing_error(
        lambda: host.redeem(hello, challenge, wrong_binding)
    ).code == "pairing-channel-unbound"

    wrong_proof = dict(redeem, clientProof=b64url_encode(b"\x00" * 32))
    assert _pairing_error(
        lambda: host.redeem(hello, challenge, wrong_proof)
    ).code == "pairing-client-proof-invalid"
    assert authority.pending_count == 0


def test_wrong_code_is_generic_code_invalid() -> None:
    _, authority, host, client, hello, challenge = _bootstrap()
    client.accept_challenge(challenge)
    redeem = _redeem(client, code="ZZZZZZZZZZ")
    assert _pairing_error(
        lambda: host.redeem(hello, challenge, redeem)
    ).code == "pairing-code-invalid"
    assert authority.pending_count == 0


def test_code_normalization_accepts_operator_formatting() -> None:
    pairing = _setup(code="abcd-efgh jk")
    assert pairing.response["status"] == PENDING
    assert pairing.authority.pending_count == 1


def test_attempt_idempotency_resume_and_conflict() -> None:
    pairing = _setup()
    first_request_id = pairing.response["requestId"]
    second = pairing.host.redeem(
        pairing.hello, pairing.challenge, pairing.redeem
    )
    assert second["requestId"] == first_request_id
    assert pairing.authority.pending_count == 1

    resume = pairing.client.redeem_resume()
    resumed = pairing.host.redeem(pairing.hello, pairing.challenge, resume)
    assert resumed["requestId"] == first_request_id
    assert pairing.authority.pending_count == 1

    conflicting = pairing.client.redeem_resume(name="a different phone")
    assert _pairing_error(
        lambda: pairing.host.redeem(
            pairing.hello, pairing.challenge, conflicting
        )
    ).code == "pairing-attempt-conflict"
    assert pairing.authority.pending_count == 1


def test_concurrent_same_attempt_creates_one_pending_request() -> None:
    _, authority, host, client, hello, challenge = _bootstrap()
    client.accept_challenge(challenge)
    redeem = _redeem(client)
    barrier = threading.Barrier(8)

    def run(_index: int) -> str:
        barrier.wait()
        return host.redeem(hello, challenge, redeem)["requestId"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        request_ids = list(pool.map(run, range(8)))
    assert len(set(request_ids)) == 1
    assert authority.pending_count == 1


def test_concurrent_different_attempts_consume_code_once() -> None:
    _, authority, host, client, hello, challenge = _bootstrap()
    client.accept_challenge(challenge)
    frames = [
        _redeem(
            client,
            attempt=b64url_encode(bytes([index]) * 32),
        )
        for index in range(8)
    ]
    barrier = threading.Barrier(8)

    def run(frame: dict) -> tuple[str, str]:
        barrier.wait()
        try:
            return "ok", host.redeem(hello, challenge, frame)["requestId"]
        except PairingError as error:
            return "error", error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, frames))
    successful = [value for kind, value in results if kind == "ok"]
    failures = [value for kind, value in results if kind == "error"]
    assert len(successful) == 1
    assert sorted(failures) == ["pairing-code-invalid"] * 7
    assert authority.pending_count == 1


def test_expired_pending_is_projected_expired() -> None:
    pairing = _setup()
    request_id = pairing.response["requestId"]
    pairing.clock.advance(models.PAIRING_REQUEST_TTL_SECONDS + 1)
    query = pairing.client.status_query(request_id)
    view = pairing.host.status(pairing.challenge, query)
    assert view["status"] == EXPIRED
    assert "device" not in view
    assert "pollAfterMs" not in view
    accepted = pairing.client.accept_status(view)
    assert accepted["status"] == EXPIRED
    assert pairing.authority.pending_count == 1  # projection is read-only


def test_status_proof_mismatch_is_generic_request_unknown() -> None:
    pairing = _setup()
    query = pairing.client.status_query(pairing.response["requestId"])
    tampered = dict(query, clientProof=b64url_encode(b"\x00" * 32))
    assert _pairing_error(
        lambda: pairing.host.status(pairing.challenge, tampered)
    ).code == "pairing-request-unknown"
    with pytest.raises(PairingError):
        pairing.host.status(
            pairing.challenge,
            dict(query, requestId="pair-" + "00" * 16),
        )


def test_approved_device_field_mismatch_is_rejected() -> None:
    pairing = _setup()
    request_id = pairing.response["requestId"]
    pairing.authority.approve(request_id)
    approved = pairing.host.status(
        pairing.challenge, pairing.client.status_query(request_id)
    )
    pairing.client.accept_status(approved)

    mutations = [
        {"deviceId": "other-device"},
        {"deviceGeneration": 0},
        {"targetNodeId": OTHER_NODE},
        {"clientPublic": OTHER_CLIENT_PUBLIC},
        {"owner": "agent:other"},
        {"permissions": []},
        {"permissions": ["sessions.events"]},
    ]
    for mutation in mutations:
        tampered = copy.deepcopy(approved)
        tampered["device"].update(mutation)
        with pytest.raises(PairingError):
            pairing.client.accept_status(tampered)


def test_rate_limit_and_code_error_mapping() -> None:
    _, authority, host, client, hello, challenge = _bootstrap()
    client.accept_challenge(challenge)
    redeem = _redeem(client)

    authority.fail_next = AuthorityError(
        "pairing-rate-limited", "slow down", retry_after_seconds=60
    )
    rate_limited = _pairing_error(
        lambda: host.redeem(hello, challenge, redeem)
    )
    assert rate_limited.code == "pairing-rate-limited"
    assert rate_limited.retry_after_seconds == 60
    assert rate_limited.close is True

    authority.fail_next = AuthorityError(
        "pairing-code-consumed", "internal code must not reach the wire"
    )
    generic = _pairing_error(lambda: host.redeem(hello, challenge, redeem))
    assert generic.code == "pairing-code-invalid"
    assert "consumed" not in generic.message


def test_project_status_forces_expiry_and_revocation() -> None:
    record = {
        "requestId": "pair-" + "cd" * 16,
        "nodeId": NODE,
        "fingerprint": "ef" * 32,
        "requestedAt": 1_000_000,
        "expiresAt": 1_000_300,
        "status": PENDING,
    }
    pending = project_status(record, expected_node_id=NODE, now=1_000_299)
    assert pending["status"] == PENDING
    expired = project_status(record, expected_node_id=NODE, now=1_000_300)
    assert expired["status"] == EXPIRED

    record["status"] = APPROVED
    record["approvedAt"] = 1_000_001
    record["device"] = {
        "deviceId": DEVICE_ID,
        "deviceGeneration": 2,
        "targetNodeId": NODE,
        "clientPublic": b64url_encode(_raw(CLIENT_PRIVATE_KEY.public_key())),
        "owner": OWNER,
        "permissions": list(PERMISSIONS),
        "approvedAt": 1_000_001,
        "status": "active",
        "revokedAt": None,
    }
    approved = project_status(record, expected_node_id=NODE, now=1_000_002)
    assert approved["status"] == APPROVED
    assert approved["device"]["deviceGeneration"] == 2

    record["device"]["status"] = "revoked"
    record["device"]["revokedAt"] = 1_000_003
    revoked = project_status(record, expected_node_id=NODE, now=1_000_004)
    assert revoked["status"] == REVOKED
    assert revoked["device"]["status"] == "revoked"
    assert revoked["device"]["revokedAt"] == 1_000_003

    wrong_node = _pairing_error(
        lambda: project_status(record, expected_node_id=OTHER_NODE, now=1_000_004)
    )
    assert wrong_node.code == "pairing-host-mismatch"


def test_pairing_grants_are_always_empty() -> None:
    pairing = _setup()
    assert pairing_grants(pairing.response) == frozenset()
    pairing.authority.approve(pairing.response["requestId"])
    approved = pairing.host.status(
        pairing.challenge,
        pairing.client.status_query(pairing.response["requestId"]),
    )
    assert pairing_grants(approved) == frozenset()
    assert "grant" not in str(approved)
    assert "token" not in str(approved)


def test_protocol_does_not_accept_approve_or_reject_frames() -> None:
    hello = {
        "v": 1,
        "type": "pairing.approve",
        "protocol": "m11-pairing/1",
        "nodeId": NODE,
        "hostKeyFingerprint": PIN,
        "clientNonce": b64url_encode(b"\x00" * 32),
        "clientPublic": b64url_encode(_raw(CLIENT_PRIVATE_KEY.public_key())),
    }
    with pytest.raises(PairingError):
        pairing_protocol.encode_frame(hello)
    with pytest.raises(PairingError):
        pairing_protocol.encode_frame(
            {
                "v": 1,
                "type": "pairing.reject",
                "mode": "reject",
            }
        )


def test_host_key_fingerprint_helper_is_sha256_prefixed() -> None:
    fingerprint = host_key_fingerprint(b"test-spki")
    assert fingerprint == "sha256:" + hashlib.sha256(b"test-spki").hexdigest()
    assert len(fingerprint) == 71
