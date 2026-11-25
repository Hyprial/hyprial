"""Staged adapter values keep their public identity and signed wire contract."""
import base64
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import hyprial.identity as identity
from hyprial.identity import interface
from hyprial.identity.impl.adapter_contracts.claims import sign_claim_receipt


def test_adapter_projections_are_the_same_objects_through_both_public_faces():
    for name in (
        "AgentIdentitySnapshot", "OrgRef", "ClaimReceiptV1",
        "OrganizationAuthorizationLeaseV1", "ManagementAuthorizationRefV1",
    ):
        assert name in identity.__all__ and name in interface.__all__
        assert getattr(identity, name) is getattr(interface, name)
    snapshot = identity.AgentIdentitySnapshot("agent:a:n:x", "a", "n", "entity")
    assert snapshot.public_payload(agent_uri="agent:a:n:x") == {
        "agentUri": "agent:a:n:x", "orgId": None, "associationGeneration": 0,
    }


def test_claim_wire_roundtrip_preserves_signature_and_rejects_changed_effect():
    receipt = sign_claim_receipt(
        Ed25519PrivateKey.generate(), receipt_id="receipt", action="bind",
        organization_id="org", instance_id="instance", instance_generation=1,
        agent="agent:a:n:x", agent_entity_token="entity", association_generation=1,
        claim_generation=1, registrar_epoch=1, operation_id="op", request_nonce="nonce",
        request_digest="request", effect_digest="effect", issued_at_ms=1000,
        expires_at_ms=2000,
    )
    restored = identity.ClaimReceiptV1.from_bytes(receipt.to_bytes())
    assert restored == receipt
    restored.verify(now_ms=1500)
    with pytest.raises(ValueError, match="CLAIM_SIGNATURE_INVALID"):
        replace(restored, effect_digest="changed").verify(now_ms=1500)


def test_lease_reference_keeps_the_signed_digest_and_expiry():
    key = Ed25519PrivateKey.generate()
    lease = identity.OrganizationAuthorizationLeaseV1(
        lease_id="lease", organization_id="org", actor="agent:a:n:x",
        entity_token="entity", association_generation=1, subject_principal="user:a",
        scope=("chat.read",), membership_revision="m1", policy_revision="p1",
        evidence_digest="evidence", generation=1, issued_at_ms=1000, expires_at_ms=2000,
        nonce="nonce", signer_public_key_b64=base64.b64encode(
            key.public_key().public_bytes_raw()).decode(), signature_b64="",
    )
    lease = replace(lease, signature_b64=base64.b64encode(key.sign(
        b"hyprial-org-authorization-lease-v1\0" + lease.signed_body())).decode())
    restored = identity.OrganizationAuthorizationLeaseV1.from_bytes(lease.to_bytes())
    restored.verify(now_ms=1500, required_scope=("chat.read",))
    ref = restored.ref(operation_id="op", use_nonce="use")
    assert ref.lease_digest == restored.lease_digest
    assert ref.operation_id == "op" and ref.use_nonce == "use"
    with pytest.raises(ValueError, match="AUTHORIZATION_LEASE_EXPIRED"):
        restored.verify(now_ms=2000, required_scope=("chat.read",))
