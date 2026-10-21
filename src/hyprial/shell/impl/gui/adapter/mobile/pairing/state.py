"""Pure S1 pairing protocol/state machine.

This module has no network listener, TLS layer, SQLite access or operator
approval command.  It composes the framing, transcripts and an injected
:class:`~.authority.PairingAuthority`; pending requests carry zero session,
history or tool authority, and a pairing approval never creates a session
grant.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec

from . import crypto, protocol, transcript
from .authority import AuthorityError, PairingAuthority

PENDING = protocol.PENDING
APPROVED = protocol.APPROVED
REJECTED = protocol.REJECTED
EXPIRED = protocol.EXPIRED
REVOKED = protocol.REVOKED

POLL_AFTER_MS = 2_000
_NONCE_BYTES = 32
_REQUEST_KEY_BYTES = 32


def _unavailable() -> protocol.PairingError:
    return protocol.PairingError(
        "pairing-unavailable", "pairing is unavailable", close=True
    )


def pairing_grants(view: Mapping[str, Any]) -> frozenset[str]:
    """Pairing never grants session or tool authority.

    Approval activates a device identity only; shared-session access remains
    an explicit, separately stored grant.  This helper exists so the S1
    boundary is explicit even though the answer is always empty.
    """

    return frozenset()


def _record_int(record: Mapping, key: str, *, minimum: int = 0) -> int:
    value = record.get(key)
    if type(value) is not int or value < minimum:
        raise _unavailable()
    return value


def _device_record(record: Mapping, key: str) -> Mapping:
    value = record.get(key)
    if not isinstance(value, Mapping):
        raise _unavailable()
    return value


def project_status(
    record: Mapping,
    *,
    expected_node_id: str | None = None,
    now: float | None = None,
) -> dict:
    """Project an authority record into the closed status-response shape.

    Pending requests are forced to ``expired`` once ``now >= expiresAt`` even
    if the store has not persisted that transition yet.  An approved device
    whose revocation is visible projects as ``revoked``.  The projection is
    read-only and never contains codes, request keys, tokens or grants.
    """

    if not isinstance(record, Mapping):
        raise _unavailable()
    node_id = record.get("nodeId")
    if not isinstance(node_id, str):
        raise _unavailable()
    if expected_node_id is not None and node_id != expected_node_id:
        raise protocol.PairingError(
            "pairing-host-mismatch", "pairing host does not match"
        )
    request_id = record.get("requestId")
    fingerprint = record.get("fingerprint")
    raw_status = record.get("status")
    if not isinstance(request_id, str) or not isinstance(fingerprint, str):
        raise _unavailable()
    if raw_status not in {PENDING, APPROVED, REJECTED, EXPIRED, REVOKED}:
        raise _unavailable()
    requested_at = _record_int(record, "requestedAt")
    expires_at = _record_int(record, "expiresAt")
    current = int(time.time() if now is None else now)

    status = raw_status
    if raw_status == PENDING and current >= expires_at:
        status = EXPIRED
    if raw_status in {APPROVED, REVOKED}:
        device = _device_record(record, "device")
        device_status = device.get("status")
        if device_status not in {"active", "revoked"}:
            raise _unavailable()
        revoked = (
            raw_status == REVOKED
            or device_status == "revoked"
            or record.get("revokedAt") is not None
            or device.get("revokedAt") is not None
        )
        status = REVOKED if revoked else APPROVED
        approved_at = device.get("approvedAt", record.get("approvedAt"))
        revoked_at = device.get("revokedAt", record.get("revokedAt"))
        if revoked and revoked_at is None:
            revoked_at = current
        target_node_id = device.get(
            "targetNodeId", record.get("targetNodeId", node_id)
        )
        client_public = device.get(
            "clientPublic", record.get("clientPublic")
        )
        owner = device.get("owner", record.get("owner"))
        permissions = device.get(
            "permissions", record.get("permissions", [])
        )
        generation = device.get(
            "deviceGeneration",
            device.get(
                "keyGeneration",
                record.get("deviceGeneration", record.get("keyGeneration")),
            ),
        )
        device_view = {
            "deviceId": device.get("deviceId", record.get("deviceId")),
            "deviceGeneration": generation,
            "targetNodeId": target_node_id,
            "clientPublic": client_public,
            "owner": owner,
            "permissions": permissions,
            "approvedAt": approved_at,
            "status": "revoked" if revoked else "active",
            "revokedAt": revoked_at,
        }
        try:
            return protocol.build_status_response(
                request_id=request_id,
                node_id=node_id,
                fingerprint=fingerprint,
                requested_at=requested_at,
                expires_at=expires_at,
                status=status,
                device=device_view,
            )
        except protocol.PairingError as error:
            raise _unavailable() from error

    if status == PENDING:
        try:
            return protocol.build_status_response(
                request_id=request_id,
                node_id=node_id,
                fingerprint=fingerprint,
                requested_at=requested_at,
                expires_at=expires_at,
                status=PENDING,
                poll_after_ms=POLL_AFTER_MS,
            )
        except protocol.PairingError as error:
            raise _unavailable() from error
    if status == REJECTED:
        rejected_at = record.get("rejectedAt")
        if type(rejected_at) is not int or rejected_at < 0:
            raise _unavailable()
        try:
            return protocol.build_status_response(
                request_id=request_id,
                node_id=node_id,
                fingerprint=fingerprint,
                requested_at=requested_at,
                expires_at=expires_at,
                status=REJECTED,
                rejected_at=rejected_at,
            )
        except protocol.PairingError as error:
            raise _unavailable() from error
    try:
        return protocol.build_status_response(
            request_id=request_id,
            node_id=node_id,
            fingerprint=fingerprint,
            requested_at=requested_at,
            expires_at=expires_at,
            status=EXPIRED,
        )
    except protocol.PairingError as error:
        raise _unavailable() from error


class HostPairingState:
    """The host-side pure state machine for one pairing channel."""

    def __init__(
        self,
        *,
        node_id: str,
        host_key_fingerprint: str,
        host_signing_key: ec.EllipticCurvePrivateKey,
        host_derive_key: ec.EllipticCurvePrivateKey,
        authority: PairingAuthority,
        clock: Callable[[], float] | None = None,
        nonce_source: Callable[[int], bytes] | None = None,
        host_derive_public: str | bytes | None = None,
    ) -> None:
        self.node_id = node_id
        self.host_key_fingerprint = host_key_fingerprint
        self._authority = authority
        self._clock = clock or time.time
        self._nonce_source = nonce_source or secrets.token_bytes
        if not isinstance(host_signing_key, ec.EllipticCurvePrivateKey):
            raise ValueError("host signing key must be a P-256 private key")
        try:
            self._host_signing_public = host_signing_key.public_key()
            crypto.public_key_raw(self._host_signing_public)
            computed = crypto.encode_public_key(host_derive_key.public_key())
        except (AttributeError, crypto.CryptoError) as error:
            raise ValueError("host keys must be P-256 keys") from error
        if host_derive_public is not None:
            try:
                raw = (
                    bytes(host_derive_public)
                    if isinstance(host_derive_public, (bytes, bytearray))
                    else crypto.b64url_decode(host_derive_public)
                )
                provided = crypto.b64url_encode(raw)
            except (TypeError, crypto.CryptoError) as error:
                raise ValueError("host derive public key is invalid") from error
            if provided != computed:
                raise ValueError("host derive public key does not match its key")
        self._host_signing_key = host_signing_key
        self._host_derive_key = host_derive_key
        self._host_derive_public = computed

    def _now(self, now: float | None) -> int:
        value = self._clock() if now is None else now
        return int(value)

    def _host_bind(self, frame: Mapping) -> None:
        if frame.get("nodeId") != self.node_id:
            raise protocol.PairingError(
                "pairing-host-mismatch", "pairing host does not match"
            )
        if frame.get("hostKeyFingerprint") != self.host_key_fingerprint:
            raise protocol.PairingError(
                "pairing-host-mismatch", "pairing host does not match"
            )

    def _challenge_bytes(self, challenge: Mapping) -> bytes:
        return transcript.challenge_transcript(
            node_id=challenge["nodeId"],
            host_key_fingerprint=challenge["hostKeyFingerprint"],
            client_nonce=challenge["clientNonce"],
            host_nonce=challenge["hostNonce"],
            client_public=challenge["clientPublic"],
            host_derive_public=challenge["hostDerivePublic"],
            server_time=challenge["serverTime"],
        )

    def _verified_challenge(self, challenge: Mapping) -> tuple[bytes, str]:
        self._host_bind(challenge)
        preimage = self._challenge_bytes(challenge)
        try:
            crypto.verify_challenge(
                self._host_signing_public,
                preimage,
                challenge["signature"],
            )
        except crypto.CryptoError as error:
            raise protocol.PairingError(
                "pairing-host-mismatch", "pairing host does not match"
            ) from error
        return preimage, transcript.channel_binding(preimage)

    def challenge(
        self,
        hello: Mapping,
        *,
        server_time: int | None = None,
        host_nonce: bytes | str | None = None,
    ) -> dict:
        hello = protocol.validate_frame(hello)
        if hello.get("type") != "pairing.hello":
            raise protocol.PairingError(
                "pairing-frame-invalid", "pairing frame is invalid"
            )
        self._host_bind(hello)
        nonce = host_nonce if host_nonce is not None else self._nonce_source(
            _NONCE_BYTES
        )
        when = self._now(server_time)
        draft = protocol.build_challenge(
            node_id=self.node_id,
            host_key_fingerprint=self.host_key_fingerprint,
            client_nonce=hello["clientNonce"],
            client_public=hello["clientPublic"],
            host_nonce=nonce,
            host_derive_public=self._host_derive_public,
            server_time=when,
            signature=b"\x00" * crypto.SIGNATURE_BYTES,
        )
        preimage = self._challenge_bytes(draft)
        signature = crypto.sign_challenge(self._host_signing_key, preimage)
        return protocol.build_challenge(
            node_id=self.node_id,
            host_key_fingerprint=self.host_key_fingerprint,
            client_nonce=hello["clientNonce"],
            client_public=hello["clientPublic"],
            host_nonce=nonce,
            host_derive_public=self._host_derive_public,
            server_time=when,
            signature=signature,
        )

    def redeem(
        self,
        hello: Mapping,
        challenge: Mapping,
        redeem: Mapping,
        *,
        now: float | None = None,
    ) -> dict:
        hello = protocol.validate_frame(hello)
        challenge = protocol.validate_frame(challenge)
        redeem = protocol.validate_frame(redeem)
        if any(
            frame.get("type") != expected
            for frame, expected in (
                (hello, "pairing.hello"),
                (challenge, "pairing.challenge"),
                (redeem, "pairing.redeem"),
            )
        ):
            raise protocol.PairingError(
                "pairing-frame-invalid", "pairing frame is invalid"
            )
        self._host_bind(hello)
        if (
            challenge["clientNonce"] != hello["clientNonce"]
            or challenge["clientPublic"] != hello["clientPublic"]
        ):
            raise protocol.PairingError(
                "pairing-frame-invalid", "pairing challenge does not echo hello"
            )
        _preimage, binding = self._verified_challenge(challenge)
        if redeem["channelBinding"] != binding:
            raise protocol.PairingError(
                "pairing-channel-unbound", "pairing channel is not bound"
            )
        if redeem["clientPublic"] != hello["clientPublic"]:
            raise protocol.PairingError(
                "pairing-frame-invalid", "pairing frame is invalid"
            )
        try:
            request_key = crypto.derive_request_key(
                self._host_derive_key,
                peer_public=redeem["clientPublic"],
                host_derive_public=self._host_derive_public,
                client_public=redeem["clientPublic"],
            )
        except crypto.CryptoError as error:
            raise _unavailable() from error
        try:
            preimage = transcript.redeem_transcript(
                mode=redeem["mode"],
                channel_binding=binding,
                pairing_attempt_id=redeem["pairingAttemptId"],
                code=redeem.get("code"),
                device_id=redeem["deviceId"],
                device_uid=redeem["deviceUid"],
                name=redeem["name"],
                owner=redeem["owner"],
                client_public=redeem["clientPublic"],
                permissions=redeem["permissions"],
            )
            transcript.verify_client_proof(
                request_key, preimage, redeem["clientProof"]
            )
        except protocol.PairingError:
            raise
        except crypto.CryptoError as error:
            raise _unavailable() from error

        when = self._now(now)
        try:
            record = self._authority.redeem_pairing_code(
                code=redeem.get("code"),
                device_id=redeem["deviceId"],
                device_uid=redeem["deviceUid"],
                name=redeem["name"],
                owner=redeem["owner"],
                client_public=redeem["clientPublic"],
                permissions=redeem["permissions"],
                pairing_attempt_id=redeem["pairingAttemptId"],
                mode=redeem["mode"],
                now=when,
            )
        except AuthorityError as error:
            raise protocol.authority_refusal(
                error.code,
                error.message,
                field=error.field,
                details=error.details,
            ) from None
        except (OSError, TimeoutError) as error:
            raise _unavailable() from error
        return project_status(record, expected_node_id=self.node_id, now=when)

    def status(
        self,
        challenge: Mapping,
        query: Mapping,
        *,
        now: float | None = None,
    ) -> dict:
        challenge = protocol.validate_frame(challenge)
        query = protocol.validate_frame(query)
        if (
            challenge.get("type") != "pairing.challenge"
            or query.get("type") != "pairing.status"
            or "channelBinding" not in query
        ):
            raise protocol.PairingError(
                "pairing-frame-invalid", "pairing frame is invalid"
            )
        _preimage, binding = self._verified_challenge(challenge)
        if query["channelBinding"] != binding:
            raise protocol.PairingError(
                "pairing-channel-unbound", "pairing channel is not bound"
            )
        when = self._now(now)
        try:
            record = self._authority.pairing_status(
                query["requestId"],
                pairing_attempt_id=query["pairingAttemptId"],
                now=when,
            )
        except AuthorityError as error:
            raise protocol.authority_refusal(
                error.code,
                error.message,
                field=error.field,
                details=error.details,
            ) from None
        except (OSError, TimeoutError) as error:
            raise _unavailable() from error
        client_public = record.get("clientPublic")
        if not isinstance(client_public, str):
            raise protocol.PairingError(
                "pairing-request-unknown", "pairing request is unknown"
            )
        try:
            request_key = crypto.derive_request_key(
                self._host_derive_key,
                peer_public=client_public,
                host_derive_public=self._host_derive_public,
                client_public=client_public,
            )
            preimage = transcript.status_transcript(
                channel_binding=binding,
                request_id=query["requestId"],
                pairing_attempt_id=query["pairingAttemptId"],
            )
            transcript.verify_client_proof(
                request_key, preimage, query["clientProof"]
            )
        except (crypto.CryptoError, protocol.PairingError) as error:
            raise protocol.PairingError(
                "pairing-request-unknown", "pairing request is unknown"
            ) from error
        return project_status(record, expected_node_id=self.node_id, now=when)


class ClientPairingState:
    """The client-side pure state machine for one pairing channel."""

    _RANK = {PENDING: 0, APPROVED: 1, REJECTED: 2, EXPIRED: 2, REVOKED: 2}

    def __init__(
        self,
        *,
        node_id: str,
        host_key_fingerprint: str,
        host_public_key: ec.EllipticCurvePublicKey,
        client_private_key: ec.EllipticCurvePrivateKey,
        clock: Callable[[], float] | None = None,
        nonce_source: Callable[[int], bytes] | None = None,
    ) -> None:
        self.node_id = node_id
        self.host_key_fingerprint = host_key_fingerprint
        self._clock = clock or time.time
        self._nonce_source = nonce_source or secrets.token_bytes
        if not isinstance(host_public_key, ec.EllipticCurvePublicKey):
            raise ValueError("host public key must be a P-256 public key")
        try:
            crypto.public_key_raw(host_public_key)
            self._client_public = crypto.encode_public_key(
                client_private_key.public_key()
            )
        except (AttributeError, crypto.CryptoError) as error:
            raise ValueError("client key must be a P-256 private key") from error
        self._host_public_key = host_public_key
        self._client_private_key = client_private_key
        self._hello: dict | None = None
        self._channel_binding: str | None = None
        self._host_derive_public: str | None = None
        self._request_key: bytearray | None = None
        self._attempt_id: str | None = None
        self._submitted: dict | None = None
        self._request_id: str | None = None
        self._status: str | None = None

    def hello(self) -> dict:
        nonce = self._nonce_source(_NONCE_BYTES)
        frame = protocol.build_hello(
            node_id=self.node_id,
            host_key_fingerprint=self.host_key_fingerprint,
            client_nonce=nonce,
            client_public=self._client_public,
        )
        self._hello = frame
        return frame

    def accept_challenge(self, challenge: Mapping) -> dict:
        challenge = protocol.validate_frame(challenge)
        if challenge.get("type") != "pairing.challenge":
            raise protocol.PairingError(
                "pairing-frame-invalid", "pairing frame is invalid"
            )
        if self._hello is None:
            raise protocol.PairingError(
                "pairing-request-invalid", "pairing channel is not opened"
            )
        if (
            challenge["nodeId"] != self.node_id
            or challenge["hostKeyFingerprint"] != self.host_key_fingerprint
        ):
            raise protocol.PairingError(
                "pairing-host-mismatch", "pairing host does not match"
            )
        if (
            challenge["clientNonce"] != self._hello["clientNonce"]
            or challenge["clientPublic"] != self._hello["clientPublic"]
        ):
            raise protocol.PairingError(
                "pairing-frame-invalid", "pairing challenge does not echo hello"
            )
        preimage = transcript.challenge_transcript(
            node_id=challenge["nodeId"],
            host_key_fingerprint=challenge["hostKeyFingerprint"],
            client_nonce=challenge["clientNonce"],
            host_nonce=challenge["hostNonce"],
            client_public=challenge["clientPublic"],
            host_derive_public=challenge["hostDerivePublic"],
            server_time=challenge["serverTime"],
        )
        try:
            crypto.verify_challenge(
                self._host_public_key, preimage, challenge["signature"]
            )
            request_key = crypto.derive_request_key(
                self._client_private_key,
                peer_public=challenge["hostDerivePublic"],
                host_derive_public=challenge["hostDerivePublic"],
                client_public=self._hello["clientPublic"],
            )
        except crypto.CryptoError as error:
            raise protocol.PairingError(
                "pairing-host-mismatch", "pairing host does not match"
            ) from error
        self._host_derive_public = challenge["hostDerivePublic"]
        self._channel_binding = transcript.channel_binding(preimage)
        self._request_key = bytearray(request_key)
        self._status = None
        return challenge

    def _require_channel(self) -> None:
        if (
            self._hello is None
            or self._channel_binding is None
            or self._request_key is None
        ):
            raise _unavailable()

    def _attempt(self, pairing_attempt_id: bytes | str | None) -> str:
        if pairing_attempt_id is None:
            return crypto.b64url_encode(secrets.token_bytes(_NONCE_BYTES))
        if isinstance(pairing_attempt_id, (bytes, bytearray)):
            return crypto.b64url_encode(bytes(pairing_attempt_id))
        return pairing_attempt_id

    def _proof(self, mode: str, *, code: str | None, attempt: str, device_id: str, device_uid: str, name: str, owner: str, permissions: Sequence[str]) -> str:
        preimage = transcript.redeem_transcript(
            mode=mode,
            channel_binding=self._channel_binding or "",
            pairing_attempt_id=attempt,
            code=code,
            device_id=device_id,
            device_uid=device_uid,
            name=name,
            owner=owner,
            client_public=self._hello["clientPublic"] if self._hello else "",
            permissions=list(permissions),
        )
        return transcript.client_proof(self._request_key or b"", preimage)

    def redeem_start(
        self,
        *,
        code: str,
        device_id: str,
        device_uid: str,
        name: str,
        owner: str,
        permissions: Sequence[str],
        pairing_attempt_id: bytes | str | None = None,
    ) -> dict:
        self._require_channel()
        attempt = self._attempt(pairing_attempt_id)
        draft = protocol.build_redeem(
            mode="start",
            channel_binding=self._channel_binding or "",
            pairing_attempt_id=attempt,
            code=code,
            device_id=device_id,
            device_uid=device_uid,
            name=name,
            owner=owner,
            client_public=self._hello["clientPublic"] if self._hello else "",
            permissions=list(permissions),
            client_proof=b"\x00" * 32,
        )
        proof = self._proof(
            "start",
            code=draft["code"],
            attempt=draft["pairingAttemptId"],
            device_id=draft["deviceId"],
            device_uid=draft["deviceUid"],
            name=draft["name"],
            owner=draft["owner"],
            permissions=draft["permissions"],
        )
        frame = protocol.build_redeem(
            mode="start",
            channel_binding=draft["channelBinding"],
            pairing_attempt_id=draft["pairingAttemptId"],
            code=draft["code"],
            device_id=draft["deviceId"],
            device_uid=draft["deviceUid"],
            name=draft["name"],
            owner=draft["owner"],
            client_public=draft["clientPublic"],
            permissions=draft["permissions"],
            client_proof=proof,
        )
        self._attempt_id = frame["pairingAttemptId"]
        self._submitted = {
            "deviceId": frame["deviceId"],
            "deviceUid": frame["deviceUid"],
            "name": frame["name"],
            "owner": frame["owner"],
            "clientPublic": frame["clientPublic"],
            "permissions": list(frame["permissions"]),
        }
        self._request_id = None
        self._status = None
        return frame

    def redeem_resume(
        self,
        *,
        pairing_attempt_id: bytes | str | None = None,
        device_id: str | None = None,
        device_uid: str | None = None,
        name: str | None = None,
        owner: str | None = None,
        permissions: Sequence[str] | None = None,
    ) -> dict:
        self._require_channel()
        if pairing_attempt_id is None:
            attempt = self._attempt_id
        else:
            attempt = self._attempt(pairing_attempt_id)
            if self._attempt_id is not None and attempt != self._attempt_id:
                raise protocol.PairingError(
                    "pairing-attempt-conflict",
                    "pairing attempt is already bound to another request",
                )
        if attempt is None:
            raise protocol.PairingError(
                "pairing-request-invalid", "pairing attempt is not established"
            )
        submitted = self._submitted
        if submitted is None and any(
            value is None
            for value in (device_id, device_uid, name, owner, permissions)
        ):
            raise protocol.PairingError(
                "pairing-request-invalid", "pairing request is not established"
            )
        base = submitted or {}
        values = {
            "deviceId": device_id if device_id is not None else base.get("deviceId"),
            "deviceUid": device_uid if device_uid is not None else base.get("deviceUid"),
            "name": name if name is not None else base.get("name"),
            "owner": owner if owner is not None else base.get("owner"),
            "permissions": (
                list(permissions)
                if permissions is not None
                else list(base.get("permissions", []))
            ),
            "clientPublic": base.get("clientPublic")
            or (self._hello["clientPublic"] if self._hello else ""),
        }
        if any(
            not isinstance(values[key], str)
            for key in ("deviceId", "deviceUid", "name", "owner", "clientPublic")
        ):
            raise protocol.PairingError(
                "pairing-request-invalid", "pairing request is not established"
            )
        draft = protocol.build_redeem(
            mode="resume",
            channel_binding=self._channel_binding or "",
            pairing_attempt_id=attempt,
            code=None,
            device_id=values["deviceId"],
            device_uid=values["deviceUid"],
            name=values["name"],
            owner=values["owner"],
            client_public=values["clientPublic"],
            permissions=values["permissions"],
            client_proof=b"\x00" * 32,
        )
        proof = self._proof(
            "resume",
            code=None,
            attempt=draft["pairingAttemptId"],
            device_id=draft["deviceId"],
            device_uid=draft["deviceUid"],
            name=draft["name"],
            owner=draft["owner"],
            permissions=draft["permissions"],
        )
        return protocol.build_redeem(
            mode="resume",
            channel_binding=draft["channelBinding"],
            pairing_attempt_id=draft["pairingAttemptId"],
            code=None,
            device_id=draft["deviceId"],
            device_uid=draft["deviceUid"],
            name=draft["name"],
            owner=draft["owner"],
            client_public=draft["clientPublic"],
            permissions=draft["permissions"],
            client_proof=proof,
        )

    def status_query(self, request_id: str) -> dict:
        self._require_channel()
        if self._attempt_id is None:
            raise protocol.PairingError(
                "pairing-request-invalid", "pairing request is not established"
            )
        preimage = transcript.status_transcript(
            channel_binding=self._channel_binding or "",
            request_id=request_id,
            pairing_attempt_id=self._attempt_id,
        )
        proof = transcript.client_proof(self._request_key or b"", preimage)
        return protocol.build_status_query(
            channel_binding=self._channel_binding or "",
            request_id=request_id,
            pairing_attempt_id=self._attempt_id,
            client_proof=proof,
        )

    def _clear_key(self) -> None:
        if isinstance(self._request_key, bytearray):
            for index in range(len(self._request_key)):
                self._request_key[index] = 0
        self._request_key = None

    def clear(self) -> None:
        self._clear_key()
        self._status = None

    def accept_status(self, response: Mapping) -> dict:
        response = protocol.validate_frame(response)
        if response.get("type") != "pairing.status" or (
            "channelBinding" in response
            or "nodeId" not in response
            or "fingerprint" not in response
        ):
            raise protocol.PairingError(
                "pairing-frame-invalid", "pairing frame is invalid"
            )
        self._require_channel()
        if self._submitted is None:
            raise protocol.PairingError(
                "pairing-request-invalid", "pairing request is not established"
            )
        if response["nodeId"] != self.node_id:
            raise protocol.PairingError(
                "pairing-host-mismatch", "pairing host does not match"
            )
        if self._request_id is None:
            self._request_id = response["requestId"]
        elif response["requestId"] != self._request_id:
            raise protocol.PairingError(
                "pairing-request-unknown", "pairing request is unknown"
            )
        status = response["status"]
        if self._status is not None:
            if (
                self._status in {REJECTED, EXPIRED, REVOKED}
                or self._RANK[status] < self._RANK[self._status]
            ):
                raise protocol.PairingError(
                    "pairing-request-unknown", "pairing request is unknown"
                )
        if status in {APPROVED, REVOKED}:
            device = response["device"]
            expected = {
                "deviceId": self._submitted["deviceId"],
                "targetNodeId": self.node_id,
                "clientPublic": self._submitted["clientPublic"],
                "owner": self._submitted["owner"],
                "permissions": sorted(set(self._submitted["permissions"])),
            }
            actual = {
                "deviceId": device["deviceId"],
                "targetNodeId": device["targetNodeId"],
                "clientPublic": device["clientPublic"],
                "owner": device["owner"],
                "permissions": device["permissions"],
            }
            if actual != expected:
                raise protocol.PairingError(
                    "pairing-request-invalid",
                    "approved device does not match the pairing request",
                    close=True,
                )
        self._status = status
        if status in {REJECTED, EXPIRED, REVOKED}:
            self._clear_key()
        return dict(response)
