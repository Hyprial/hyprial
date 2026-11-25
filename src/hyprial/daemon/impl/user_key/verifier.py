"""Local verification with injected sources; production composition stays off.

No endpoint, directory space, user/sub mapping or test issuer is selected here.
Source callables are daemon-owned, never IPC parameters. The per-instance
sequence memory is not a durable ledger or a cross-daemon freshness proof.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from hyprial.identity import UserKey
from hyprial.kernel import ipc_errors


@dataclass(frozen=True)
class Lookup:
    state: str = "unavailable"
    public_key: str | None = None
    anchor_ref: dict | None = None
    expires_at: float | None = None
    device_uid: str | None = None
    sub: str | None = None


class OwnerKeyPort:
    def __init__(self, *, issuer=None, keys=None, subject=None, binding=None,
                 revocations=None, binding_fallback=None, revocations_fallback=None,
                 clock=time.time):
        self.issuer, self.keys, self.subject = issuer, keys, subject
        self.binding, self.revocations = binding, revocations
        self.binding_fallback, self.revocations_fallback = binding_fallback, revocations_fallback
        self.clock = clock
        self._seen = {}
        self._cached = {}
        self._lock = threading.RLock()

    def _document(self, primary, fallback, args, verify, cache_key):
        """Directory then remote; cached tokens are reverified, never trusted raw."""
        saw_document = False
        for source in (primary, fallback):
            if source is None:
                continue
            try:
                token = source(*args)
            except Exception:
                continue  # Source failures contain no trusted negative evidence.
            if token is None:
                continue
            saw_document = True
            try:
                result = verify(token)
            except UserKey.Error:
                continue
            self._cached[cache_key] = token
            return result
        # Do not mask a newly supplied invalid document with an older cache.
        if not saw_document and cache_key in self._cached:
            return verify(self._cached[cache_key])
        raise UserKey.Error(ipc_errors.USER_KEY_UNBOUND)

    def lookup(self, owner: str, key_id: str) -> Lookup:
        with self._lock:
            try:
                return self._lookup(owner, key_id)
            except Exception:
                # Untrusted input/source errors must not escape or reveal tokens.
                return Lookup()

    def _lookup(self, owner, key_id):
        if (not isinstance(owner, str) or not owner or len(owner) > 1024
                or not UserKey.valid_key_id(key_id) or not self.issuer
                or self.subject is None or self.keys is None):
            return Lookup()
        sub = self.subject(owner)
        if not isinstance(sub, str) or not sub:
            return Lookup()
        keys, now = self.keys(), self.clock()
        try:
            binding = self._document(
                self.binding, self.binding_fallback, (owner, key_id),
                lambda token: UserKey.verify_binding(token, issuer=self.issuer, keys=keys, now=now),
                ("binding", owner, key_id))
        except UserKey.Error:
            binding = None
        if binding is not None and binding.key_id == key_id and (
            binding.owner != owner or binding.sub != sub
        ):
            return Lookup(state="absent")
        revoked = self._document(
            self.revocations, self.revocations_fallback, (sub,),
            lambda token: UserKey.verify_revocations(token, issuer=self.issuer, keys=keys, now=now),
            ("revocations", sub))
        if revoked.sub != sub:
            return Lookup()
        previous = self._seen.get(sub)
        if previous is not None and (
            revoked.seq < previous.seq or revoked.issued_at < previous.issued_at
            or not previous.revoked_key_ids.issubset(revoked.revoked_key_ids)
            or (revoked.seq == previous.seq and revoked.revoked_key_ids != previous.revoked_key_ids)
        ):
            return Lookup()
        self._seen[sub] = revoked
        if key_id in revoked.revoked_key_ids:
            return Lookup(state="absent")
        if binding is None or binding.key_id != key_id:
            return Lookup()
        return Lookup("verified", binding.public_key,
                      {"iss": self.issuer, "jwksKid": binding.jwks_kid,
                       "credentialSha256": binding.credential_sha256,
                       "revocationSeq": revoked.seq},
                      min(binding.expires_at, revoked.expires_at), binding.device_uid, sub)

    def trust(self, record) -> UserKey.Trust:
        """Local signer adapter: binding must also match this device and key."""
        result = self.lookup(record.owner, record.key_id)
        if (result.state != "verified" or result.public_key != record.public_key
                or result.device_uid != record.device_uid or result.sub != record.casdoor_sub):
            raise UserKey.Error(ipc_errors.USER_KEY_UNBOUND)
        return UserKey.Trust(result.public_key, result.expires_at, result.anchor_ref["revocationSeq"])
