"""Pure access checks against explicit caller facts, not global freshness proofs."""
from __future__ import annotations

import hashlib

from hyprial.kernel import ipc_errors
from .envelope import verify_envelope
from .grants import verify_grant
from .models import OwnerKey, encode, integer, require, snapshot


def check_access(*, envelope: dict | None, grant: dict | None, owner_key: OwnerKey,
                 agent: str, recipient_key_id: str, now: int, current_grant_digest: str,
                 current_dek_version: int, revoked: bool, grant_owner_key: OwnerKey | None = None) -> dict:
    """Return a verified grant only if it matches the caller's exact current state.

    owner_key verifies the envelope. grant_owner_key, when given, verifies the grant
    instead: after an owner signing-key rotation the envelope and the grant may be
    sealed by two anchor-verified keys of the same owner. Both must be verified U2
    anchors supplied by the caller; omitted, owner_key verifies both.

    The caller must establish revocation/freshness and anchor trust separately.
    A digest from an arbitrary replica is NOT proof of the latest grant. This
    primitive cannot implement R4, agent-incarnation custody, or launch control.
    All current-state inputs are required: there is no legacy/default fallback.
    """
    require(grant is not None, ipc_errors.PROVIDER_GRANT_MISSING)
    require(type(revoked) is bool)
    require(not revoked, ipc_errors.PROVIDER_GRANT_REVOKED)
    require(envelope is not None, ipc_errors.PROVIDER_KEY_NOT_FOUND)
    grant_key = owner_key if grant_owner_key is None else grant_owner_key
    require(isinstance(grant_key, OwnerKey) and isinstance(owner_key, OwnerKey)
            and grant_key.owner == owner_key.owner)
    grant = snapshot(grant)
    value = verify_envelope(envelope, owner_key=owner_key)
    payload = verify_grant(grant, owner_key=grant_key, header=value["header"],
                           agent=agent, recipient_key_id=recipient_key_id, now=now)
    require(integer(current_dek_version, 1) and payload["dekVersion"] == current_dek_version)
    require(type(current_grant_digest) is str and len(current_grant_digest) == 64)
    require(hashlib.sha256(encode(grant)).hexdigest() == current_grant_digest)
    require(any(row["recipientKeyId"] == recipient_key_id for row in value["recipients"]),
            ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING)
    return payload
