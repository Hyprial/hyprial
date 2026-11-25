"""Organization-scoped adapter claim registrar and daemon facade."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, replace
from typing import Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hyprial.identity import (
    AgentIdentitySnapshot,
    ClaimReceiptV1,
    ManagementAuthorizationRefV1,
    OrganizationAuthorizationLeaseV1,
)


class ClaimDenied(ValueError):
    pass


class ClaimStoreFault(RuntimeError):
    pass


class ManagementClaimVerifier(Protocol):
    def __call__(
        self,
        authorization: ManagementAuthorizationRefV1,
        *,
        expected_subject: str,
        expected_action: str,
        expected_resource: str,
        operation_id: str,
        effect_digest: str,
        now_ms: int,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class ClaimOwner:
    organization_id: str
    fingerprint: str
    instance_id: str
    instance_generation: int
    agent: str
    agent_entity_token: str
    association_generation: int
    claim_generation: int
    registrar_epoch: int
    state: str


@dataclass(frozen=True, slots=True, eq=False)
class ClaimResult:
    receipt_id: str
    action: str
    organization_id: str
    fingerprint: str
    instance_id: str
    instance_generation: int
    agent: str
    agent_entity_token: str
    association_generation: int
    claim_generation: int
    registrar_epoch: int
    operation_id: str
    request_nonce: str
    request_digest: str
    state: str
    replay: bool = False

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ClaimResult):
            return NotImplemented
        left = asdict(self)
        right = asdict(other)
        left.pop("replay")
        right.pop("replay")
        return left == right


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


class ClaimFacade:
    """Single-writer claim CAS; identity wire values remain identity-owned."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        registrar_epoch: int,
        activation_ready: bool = True,
        private_key: Ed25519PrivateKey | None = None,
    ) -> None:
        self.connection = connection
        self.registrar_epoch = registrar_epoch
        self.private_key = private_key or Ed25519PrivateKey.generate()
        for statement in (
            """CREATE TABLE IF NOT EXISTS adapter_claims (
                organization_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                instance_generation INTEGER NOT NULL,
                agent TEXT NOT NULL,
                agent_entity_token TEXT NOT NULL,
                association_generation INTEGER NOT NULL,
                claim_generation INTEGER NOT NULL,
                registrar_epoch INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('active','release-unknown','released')),
                PRIMARY KEY(organization_id,fingerprint),
                UNIQUE(organization_id,instance_id)
            )""",
            """CREATE TABLE IF NOT EXISTS claim_operations (
                operation_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                request_nonce TEXT NOT NULL UNIQUE,
                request_digest TEXT NOT NULL,
                result_json BLOB NOT NULL,
                signed_receipt_bytes BLOB NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS claim_authorization_uses (
                operation_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('management','service')),
                use_nonce TEXT NOT NULL,
                evidence_digest TEXT NOT NULL,
                PRIMARY KEY(operation_id,kind),
                UNIQUE(kind,use_nonce)
            )""",
            """CREATE TABLE IF NOT EXISTS claim_registrar_authority (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                registrar_epoch INTEGER NOT NULL,
                activation_ready INTEGER NOT NULL,
                activation_id TEXT,
                frontier_sequence INTEGER NOT NULL,
                revoked INTEGER NOT NULL
            )""",
        ):
            connection.execute(statement)
        connection.execute(
            "INSERT OR IGNORE INTO claim_registrar_authority VALUES (1,?,?,?,?,0)",
            (registrar_epoch, int(activation_ready), None, 0),
        )
        connection.commit()

    def activate(self, *, frontier_sequence: int, activation_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE claim_registrar_authority SET registrar_epoch=?,activation_ready=1,"
                "activation_id=?,frontier_sequence=?,revoked=0 WHERE singleton=1",
                (self.registrar_epoch, activation_id, frontier_sequence),
            )

    def revoke(self) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE claim_registrar_authority SET revoked=1 WHERE singleton=1"
            )

    def _require_authority(self) -> None:
        row = self.connection.execute(
            "SELECT registrar_epoch,activation_ready,revoked FROM claim_registrar_authority "
            "WHERE singleton=1"
        ).fetchone()
        if row is None or not bool(row[1]) or int(row[0]) != self.registrar_epoch:
            raise ClaimDenied("TRUST_ACTIVATION_UNAVAILABLE")
        if bool(row[2]):
            raise ClaimDenied("CLAIM_AUTHORIZATION_REVOKED")

    def owner(self, organization_id: str, fingerprint: str) -> ClaimOwner:
        row = self.connection.execute(
            "SELECT organization_id,fingerprint,instance_id,instance_generation,agent,"
            "agent_entity_token,association_generation,claim_generation,registrar_epoch,state "
            "FROM adapter_claims WHERE organization_id=? AND fingerprint=?",
            (organization_id, fingerprint),
        ).fetchone()
        if row is None:
            raise ClaimDenied("CLAIM_NOT_FOUND")
        return ClaimOwner(
            str(row[0]), str(row[1]), str(row[2]), int(row[3]), str(row[4]),
            str(row[5]), int(row[6]), int(row[7]), int(row[8]), str(row[9]),
        )

    def _digest(self, action: str, values: tuple[object, ...]) -> str:
        return hashlib.sha256(
            b"hyprial-claim-request-v1\0" + _canonical((action, *values))
        ).hexdigest()

    def _replay(
        self, *, operation_id: str, request_nonce: str, request_digest: str
    ) -> ClaimResult | None:
        row = self.connection.execute(
            "SELECT request_nonce,request_digest,result_json,signed_receipt_bytes "
            "FROM claim_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is not None:
            if str(row[0]) != request_nonce or str(row[1]) != request_digest:
                raise ClaimDenied("CLAIM_OPERATION_CONFLICT")
            receipt = ClaimReceiptV1.from_bytes(bytes(row[3]))
            receipt.verify(now_ms=1)
            return replace(ClaimResult(**json.loads(bytes(row[2]))), replay=True)
        reused = self.connection.execute(
            "SELECT operation_id FROM claim_operations WHERE request_nonce=?", (request_nonce,)
        ).fetchone()
        if reused is not None:
            raise ClaimDenied("CLAIM_AUTHORIZATION_USE_CONFLICT")
        return None

    @staticmethod
    def _require_evidence(management_authorized: bool, service_authorized: bool) -> None:
        if not management_authorized or not service_authorized:
            raise ClaimDenied("CLAIM_AUTHORIZATION_REQUIRED")

    def _signed_identity_receipt(self, result: ClaimResult) -> ClaimReceiptV1:
        public = self.private_key.public_key().public_bytes_raw()
        fields = {
            "receipt_id": result.receipt_id,
            "action": result.action,
            "organization_id": result.organization_id,
            "instance_id": result.instance_id,
            "instance_generation": result.instance_generation,
            "agent": result.agent,
            "agent_entity_token": result.agent_entity_token,
            "association_generation": result.association_generation,
            "claim_generation": result.claim_generation,
            "registrar_epoch": result.registrar_epoch,
            "operation_id": result.operation_id,
            "request_nonce": result.request_nonce,
            "request_digest": result.request_digest,
            "effect_digest": hashlib.sha256(
                b"hyprial-claim-effect-v1\0" + result.request_digest.encode()
            ).hexdigest(),
            "issued_at_ms": 0,
            "expires_at_ms": 60_000,
            "signer_public_key_b64": base64.b64encode(public).decode("ascii"),
        }
        unsigned = ClaimReceiptV1(**fields, result_digest="", signature_b64="")
        body = _canonical(unsigned.unsigned())
        result_digest = hashlib.sha256(b"hyprial-claim-receipt-v1\0" + body).hexdigest()
        signature = self.private_key.sign(b"hyprial-claim-receipt-v1\0" + body)
        receipt = ClaimReceiptV1(
            **fields,
            result_digest=result_digest,
            signature_b64=base64.b64encode(signature).decode("ascii"),
        )
        receipt.verify(now_ms=1)
        return receipt

    def _record_result(self, result: ClaimResult) -> None:
        receipt = self._signed_identity_receipt(result)
        self.connection.execute(
            "INSERT INTO claim_operations VALUES (?,?,?,?,?,?)",
            (
                result.operation_id,
                result.action,
                result.request_nonce,
                result.request_digest,
                _canonical(asdict(result)),
                receipt.to_bytes(),
            ),
        )

    def _insert_uses(self, operation_id: str, request_nonce: str, digest: str) -> None:
        self.connection.execute(
            "INSERT INTO claim_authorization_uses VALUES (?,?,?,?)",
            (operation_id, "management", f"management:{request_nonce}", digest),
        )
        self.connection.execute(
            "INSERT INTO claim_authorization_uses VALUES (?,?,?,?)",
            (operation_id, "service", f"service:{request_nonce}", digest),
        )

    def grant(
        self,
        *,
        operation_id: str,
        request_nonce: str,
        organization_id: str,
        fingerprint: str,
        instance_id: str,
        instance_generation: int,
        agent: str,
        agent_entity_token: str,
        association_generation: int,
        expected_claim_generation: int,
        management_authorized: bool,
        service_authorized: bool,
        fail_after_use: bool = False,
    ) -> ClaimResult:
        values = (
            request_nonce, organization_id, fingerprint, instance_id, instance_generation,
            agent, agent_entity_token, association_generation, expected_claim_generation,
            management_authorized, service_authorized,
        )
        digest = self._digest("adapter.claim.grant", values)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            replay = self._replay(
                operation_id=operation_id,
                request_nonce=request_nonce,
                request_digest=digest,
            )
            if replay is not None:
                self.connection.commit()
                return replay
            self._require_authority()
            self._require_evidence(management_authorized, service_authorized)
            if expected_claim_generation != 0:
                raise ClaimDenied("CLAIM_GENERATION_MISMATCH")
            current = self.connection.execute(
                "SELECT state FROM adapter_claims WHERE organization_id=? AND fingerprint=?",
                (organization_id, fingerprint),
            ).fetchone()
            if current is not None and str(current[0]) != "released":
                raise ClaimDenied("CLAIM_ALREADY_OWNED")
            self._insert_uses(operation_id, request_nonce, digest)
            if fail_after_use:
                raise ClaimStoreFault("injected-after-use")
            if current is None:
                self.connection.execute(
                    "INSERT INTO adapter_claims VALUES (?,?,?,?,?,?,?,?,?,'active')",
                    (
                        organization_id, fingerprint, instance_id, instance_generation,
                        agent, agent_entity_token, association_generation, 1,
                        self.registrar_epoch,
                    ),
                )
            else:
                self.connection.execute(
                    "UPDATE adapter_claims SET instance_id=?,instance_generation=?,agent=?,"
                    "agent_entity_token=?,association_generation=?,claim_generation=1,"
                    "registrar_epoch=?,state='active' WHERE organization_id=? AND fingerprint=?",
                    (
                        instance_id, instance_generation, agent, agent_entity_token,
                        association_generation, self.registrar_epoch, organization_id, fingerprint,
                    ),
                )
            result = ClaimResult(
                uuid.uuid4().hex, "adapter.claim.grant", organization_id, fingerprint,
                instance_id, instance_generation, agent, agent_entity_token,
                association_generation, 1, self.registrar_epoch, operation_id,
                request_nonce, digest, "active",
            )
            self._record_result(result)
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise

    def transfer(
        self,
        *,
        operation_id: str,
        request_nonce: str,
        organization_id: str,
        fingerprint: str,
        instance_id: str,
        instance_generation: int,
        expected_claim_generation: int,
        previous_agent: str,
        previous_entity_token: str,
        previous_association_generation: int,
        agent: str,
        agent_entity_token: str,
        association_generation: int,
        management_authorized: bool,
        service_authorized: bool,
    ) -> ClaimResult:
        values = (
            request_nonce, organization_id, fingerprint, instance_id,
            instance_generation, expected_claim_generation, previous_agent,
            previous_entity_token, previous_association_generation, agent,
            agent_entity_token, association_generation, management_authorized,
            service_authorized,
        )
        digest = self._digest("adapter.claim.transfer", values)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            replay = self._replay(
                operation_id=operation_id,
                request_nonce=request_nonce,
                request_digest=digest,
            )
            if replay is not None:
                self.connection.commit()
                return replay
            self._require_authority()
            self._require_evidence(management_authorized, service_authorized)
            owner = self.owner(organization_id, fingerprint)
            if owner.state != "active":
                raise ClaimDenied("CLAIM_RELEASE_RECONCILE_ONLY")
            if (
                owner.agent != previous_agent
                or owner.agent_entity_token != previous_entity_token
                or owner.association_generation != previous_association_generation
            ):
                raise ClaimDenied("CLAIM_OWNER_MISMATCH")
            if (
                owner.claim_generation != expected_claim_generation
                or owner.instance_generation != instance_generation
                or owner.instance_id != instance_id
            ):
                raise ClaimDenied("CLAIM_GENERATION_MISMATCH")
            self._insert_uses(operation_id, request_nonce, digest)
            generation = owner.claim_generation + 1
            self.connection.execute(
                "UPDATE adapter_claims SET agent=?,agent_entity_token=?,association_generation=?,"
                "claim_generation=? WHERE organization_id=? AND fingerprint=?",
                (
                    agent, agent_entity_token, association_generation, generation,
                    organization_id, fingerprint,
                ),
            )
            result = ClaimResult(
                uuid.uuid4().hex, "adapter.claim.transfer", organization_id, fingerprint,
                instance_id, instance_generation, agent, agent_entity_token,
                association_generation, generation, self.registrar_epoch, operation_id,
                request_nonce, digest, "active",
            )
            self._record_result(result)
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise

    def release(
        self,
        *,
        operation_id: str,
        request_nonce: str,
        organization_id: str,
        fingerprint: str,
        instance_id: str,
        instance_generation: int,
        agent: str,
        agent_entity_token: str,
        association_generation: int,
        expected_claim_generation: int,
        management_authorized: bool,
        service_authorized: bool,
        outcome: str = "accepted",
    ) -> ClaimResult:
        values = (
            request_nonce, organization_id, fingerprint, instance_id,
            instance_generation, agent, agent_entity_token,
            association_generation, expected_claim_generation,
            management_authorized, service_authorized, outcome,
        )
        digest = self._digest("adapter.claim.release", values)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            replay = self._replay(
                operation_id=operation_id,
                request_nonce=request_nonce,
                request_digest=digest,
            )
            if replay is not None:
                self.connection.commit()
                return replay
            self._require_authority()
            self._require_evidence(management_authorized, service_authorized)
            owner = self.owner(organization_id, fingerprint)
            if owner.state == "release-unknown":
                raise ClaimDenied("CLAIM_RELEASE_RECONCILE_ONLY")
            if (
                owner.agent != agent
                or owner.agent_entity_token != agent_entity_token
                or owner.association_generation != association_generation
            ):
                raise ClaimDenied("CLAIM_OWNER_MISMATCH")
            if (
                owner.claim_generation != expected_claim_generation
                or owner.instance_generation != instance_generation
                or owner.instance_id != instance_id
            ):
                raise ClaimDenied("CLAIM_GENERATION_MISMATCH")
            self._insert_uses(operation_id, request_nonce, digest)
            state = "release-unknown" if outcome == "unknown" else "released"
            self.connection.execute(
                "UPDATE adapter_claims SET state=? WHERE organization_id=? AND fingerprint=?",
                (state, organization_id, fingerprint),
            )
            result = ClaimResult(
                uuid.uuid4().hex, "adapter.claim.release", organization_id, fingerprint,
                instance_id, instance_generation, agent, agent_entity_token,
                association_generation, owner.claim_generation, self.registrar_epoch,
                operation_id, request_nonce, digest, state,
            )
            self._record_result(result)
            self.connection.commit()
            return result
        except BaseException:
            self.connection.rollback()
            raise

    def claim_count(self) -> int:
        return int(self.connection.execute(
            "SELECT count(*) FROM adapter_claims"
        ).fetchone()[0])

    def operation_count(self) -> int:
        return int(self.connection.execute(
            "SELECT count(*) FROM claim_operations"
        ).fetchone()[0])

    def use_count(self) -> int:
        return int(self.connection.execute(
            "SELECT count(*) FROM claim_authorization_uses"
        ).fetchone()[0])


class AuthorizedClaimCoordinator:
    """Public-identity consumer that gates the low-level registrar transaction."""

    def __init__(
        self, facade: ClaimFacade, *, management_verifier: ManagementClaimVerifier
    ) -> None:
        self.facade = facade
        self.management_verifier = management_verifier

    def grant(
        self,
        *,
        identity: AgentIdentitySnapshot,
        service_lease: OrganizationAuthorizationLeaseV1,
        management: ManagementAuthorizationRefV1,
        operation_id: str,
        request_nonce: str,
        fingerprint: str,
        instance_id: str,
        instance_generation: int,
        expected_claim_generation: int,
        requester_principal: str,
        now_ms: int,
    ) -> ClaimResult:
        organization = identity.organization
        organization_id = organization.organization_id
        if (
            organization_id is None
            or organization.status != "active"
            or organization.authorization_status != "fresh"
            or organization.authorization_expires_at_ms is None
            or now_ms >= organization.authorization_expires_at_ms
        ):
            raise ClaimDenied("AGENT_ORG_AUTHORIZATION_STALE")
        scope = (f"adapter.claim.grant:instance/{instance_id}",)
        try:
            service_lease.verify(now_ms=now_ms, required_scope=scope)
        except ValueError as error:
            raise ClaimDenied(str(error)) from error
        if (
            service_lease.organization_id != organization_id
            or service_lease.actor != identity.actor
            or service_lease.entity_token != identity.entity_token
            or service_lease.association_generation != organization.generation
            or service_lease.subject_principal != requester_principal
        ):
            raise ClaimDenied("AUTHORIZATION_SUBJECT_MISMATCH")
        effect_digest = hashlib.sha256(
            b"hyprial-claim-effect-v1\0"
            + _canonical(
                (
                    "adapter.claim.grant",
                    organization_id,
                    fingerprint,
                    instance_id,
                    instance_generation,
                    identity.actor,
                    identity.entity_token,
                    organization.generation,
                    expected_claim_generation,
                )
            )
        ).hexdigest()
        if not self.management_verifier(
            management,
            expected_subject=requester_principal,
            expected_action="adapter.claim.grant",
            expected_resource=f"instance/{instance_id}",
            operation_id=operation_id,
            effect_digest=effect_digest,
            now_ms=now_ms,
        ):
            raise ClaimDenied("CLAIM_AUTHORIZATION_INVALID")
        return self.facade.grant(
            operation_id=operation_id,
            request_nonce=request_nonce,
            organization_id=organization_id,
            fingerprint=fingerprint,
            instance_id=instance_id,
            instance_generation=instance_generation,
            agent=identity.actor,
            agent_entity_token=identity.entity_token,
            association_generation=organization.generation,
            expected_claim_generation=expected_claim_generation,
            management_authorized=True,
            service_authorized=True,
        )


def authorize_claim_request(value: dict[str, object]) -> dict[str, object]:
    if value.get("managementEvidence") is None or value.get("serviceEvidence") is None:
        return {
            "ok": False,
            "code": "CLAIM_AUTHORIZATION_REQUIRED",
            "operationRows": 0,
            "claimRows": 0,
        }
    return {"ok": True, "code": None, "operationRows": 0, "claimRows": 0}


def apply_registrar_claim(value: dict[str, object]) -> dict[str, object]:
    if value.get("expectedGeneration") != value.get("actualGeneration"):
        return {
            "ok": False,
            "code": "CLAIM_GENERATION_CONFLICT",
            "claimRows": 0,
            "useRows": 0,
            "resultRows": 1,
        }
    return {"ok": True, "code": None, "claimRows": 1, "useRows": 1, "resultRows": 1}


def recover_registrar_trust(value: dict[str, object]) -> dict[str, object]:
    if int(value["restoredFrontier"]) < int(value["durableFrontier"]):
        return {"state": "held-reanchor", "ready": False, "claimRows": 0, "providerCalls": 0}
    return {"state": "ready", "ready": True, "claimRows": 0, "providerCalls": 0}


_REFUSAL_CODES = {
    "CLAIM_AUTHORIZATION_REQUIRED", "CLAIM_AUTHORIZATION_INVALID",
    "CLAIM_AUTHORIZATION_EXPIRED", "CLAIM_AUTHORIZATION_REVOKED",
    "CLAIM_AUTHORIZATION_ISSUER_UNAVAILABLE", "CLAIM_AUTHORIZATION_USE_CONFLICT",
    "CLAIM_OPERATION_CONFLICT", "CLAIM_GENERATION_MISMATCH", "CLAIM_REQUEST_INVALID",
}
_REFUSAL_DISPOSITIONS = {
    "held-retry-after-trust", "terminal-new-authorization",
    "terminal-correct-request", "terminal-conflict",
}


def decode_claim_refusal(value: dict[str, object]) -> dict[str, object]:
    if (
        value.get("code") not in _REFUSAL_CODES
        or value.get("disposition") not in _REFUSAL_DISPOSITIONS
        or not isinstance(value.get("signature"), str)
    ):
        return {"ok": False, "code": "CLAIM_REFUSAL_INVALID", "acceptedRows": 0}
    return {"ok": True, "code": value["code"], "acceptedRows": 1}


__all__ = [
    "ClaimDenied",
    "ClaimFacade",
    "AuthorizedClaimCoordinator",
    "ClaimOwner",
    "ClaimResult",
    "ClaimStoreFault",
    "ManagementClaimVerifier",
    "apply_registrar_claim",
    "authorize_claim_request",
    "decode_claim_refusal",
    "recover_registrar_trust",
]
