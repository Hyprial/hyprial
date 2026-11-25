"""Closed purpose reservation table; production payload schemas are not active.

U1 does not freeze the missing OrgFS mapping or R1 provider wire formats.
Only explicit in-process test injection can supply a signing schema in U1.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from hyprial.kernel import ipc_errors
from .models import Error


@dataclass(frozen=True)
class Purpose:
    name: str
    domain: str
    kind: str
    fields: frozenset[str] | None = None
    key_kind: str = "owner"

    def normalize(self, payload: object, *, owner: str, now_ms: int) -> dict:
        if self.fields is None:
            raise Error(ipc_errors.USER_KEY_UNBOUND)
        if (not isinstance(payload, dict)
                or set(payload) != self.fields - {"issuedAtMs"}
                or payload.get("owner") != owner
                or any(not isinstance(v, str) or not v or len(v) > 1024 or "\x00" in v
                       for v in payload.values())):
            raise Error(ipc_errors.PAYLOAD_INVALID)
        return {**payload, "owner": owner, "issuedAtMs": now_ms}


_DECLARATIONS = ("agent-owner.authorize", "revocation-ledger.append")
_OPERATIONS = ("ledger-head.attest", "org.invite", "org.invite.accept", "org.join",
               "org.leave", "org.acl.grant", "org.acl.revoke", "orgfs.commit-as-user")
# kind is intentionally not guessed for reserved entries. No reserved entry
# can sign until its owning protocol provides a reviewed schema and kind.
PURPOSES = MappingProxyType({
    name: Purpose(name, "hyprial-orgfs-decl-v1" if name in _DECLARATIONS
                  else "hyprial-user-op-v1", "")
    for name in (*_DECLARATIONS, *_OPERATIONS)
})
