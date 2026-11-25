"""Daemon-only signer; U1 ships no active production purpose or trust source."""
from __future__ import annotations

import base64
import binascii
import hashlib
import math
import re
import threading
import time
from types import MappingProxyType
from uuid import uuid4

from hyprial.identity import ProviderKeys, UserKey
from hyprial.kernel import ipc_errors
from .audit import Audit
from .policy import Caller, RateLimiter, authorize

_OPERATION = re.compile(r"[a-z][a-z0-9-]{0,31}/[a-z][a-z0-9-]{0,31}")


class Signer:
    def __init__(self, store, *, trust=UserKey.unavailable_binding,
                 enabled=False, purposes=None, clock=time.time):
        self.store = store
        self.trust = trust
        self.enabled = enabled
        self.purposes = MappingProxyType(dict(UserKey.PURPOSES if purposes is None else purposes))
        self.clock = clock
        self.audit = Audit(store)
        self.limiter = RateLimiter()
        self._lock = threading.RLock()

    def refuse(self, operation: str, caller: Caller, code: str):
        self.audit.append({
            "ts": int(self.clock() * 1000), "requestId": uuid4().hex,
            "owner": self.store.owner, "keyId": None, "purpose": None,
            "kind": None, "domain": None, "payloadSha256": None, "org": None,
            "target": None, "caller": {"class": caller.classification,
                "actor": caller.actor, "sessionRefSha256": None, "pid": caller.pid},
            "decision": "refused", "code": code,
        })
        raise UserKey.Error(code)

    def status(self) -> dict:
        # Never activate by a record's self-reported state. Binding metadata
        # remains unknown until U2 supplies a verified trust source.
        if not self.store.supported():
            return {"keyId": None, "publicKey": None, "state": "unavailable",
                    "bindingExp": None, "revocationSeq": None}
        result = self.store.status()
        if result["state"] == "bound":
            result["state"] = "unbound"
        return result

    def sign(self, purpose: str, payload: object, caller: Caller) -> dict:
        with self._lock:
            return self._sign(purpose, payload, caller)

    def _sign(self, purpose, payload, caller):
        now = self.clock()
        spec = self.purposes.get(purpose) if isinstance(purpose, str) else None
        normalized = None
        key_id = None
        row = {"ts": int(now * 1000), "requestId": uuid4().hex, "owner": self.store.owner,
               "keyId": None, "purpose": spec.name if spec else None,
               "kind": spec.kind if spec else None, "domain": spec.domain if spec else None,
               "payloadSha256": None, "org": None, "target": None,
               "caller": {"class": caller.classification, "actor": caller.actor,
                          "sessionRefSha256": hashlib.sha256(caller.session_ref.encode()).hexdigest()
                          if caller.session_ref else None, "pid": caller.pid},
               "decision": "refused", "code": None}
        try:
            authorize(caller, purpose, owner=self.store.owner)
            if spec is None:
                raise UserKey.Error(ipc_errors.PURPOSE_UNKNOWN)
            self.limiter.consume(caller, purpose, now)
            normalized = spec.normalize(payload, owner=self.store.owner, now_ms=int(now * 1000))
            if normalized["org"] not in caller.orgs:
                raise UserKey.Error(ipc_errors.CALLER_NOT_AUTHORIZED)
            row.update(payloadSha256=UserKey.digest(normalized), org=normalized["org"],
                       target=normalized.get("target"))
            record = self.store.load_for_signing()
            key_id = record.key_id
            self._require_trusted(record, now)
            signature = UserKey.sign(record.private_key, spec, normalized)
            envelope = {"domain": spec.domain, "kind": spec.kind, "payload": normalized,
                        "signer": {"keyKind": "owner", "owner": record.owner, "keyId": key_id},
                        "signature": signature}
        except UserKey.Error as error:
            row.update(keyId=key_id, code=error.code)
            self.audit.append(row)
            raise
        row.update(keyId=key_id, decision="signed")
        self.audit.append(row)
        return envelope

    def _require_trusted(self, record, now) -> None:
        """The activation gates shared by every signing path: enabled, bound, trusted, fresh."""
        if not self.enabled or record.state != "bound":
            raise UserKey.Error(ipc_errors.USER_KEY_UNBOUND)
        try:
            trust = self.trust(record)
        except UserKey.Error:
            raise
        except Exception:
            raise UserKey.Error(ipc_errors.USER_KEY_UNBOUND) from None
        if (not isinstance(trust, UserKey.Trust) or trust.public_key != record.public_key
                or type(trust.expires_at) not in (int, float)
                or not math.isfinite(trust.expires_at)
                or type(trust.revocation_seq) is not int or trust.revocation_seq < 0):
            raise UserKey.Error(ipc_errors.USER_KEY_UNBOUND)
        if trust.expires_at <= now:
            raise UserKey.Error(ipc_errors.USER_KEY_BINDING_EXPIRED)

    def signing_key(self, operation: str) -> ProviderKeys.SigningKey:
        """Owner Ed25519 key for one daemon-internal owner-record operation.

        The caller is the daemon itself, after its own operator-only admission, so
        the U1 caller/purpose registry does not apply; every activation gate of
        sign() does (supported store, signable record, enabled, bound, trusted,
        fresh). One audit row records the operation label and decision, never key
        material or payloads. The returned key lives only in the caller's memory.
        """
        with self._lock:
            now = self.clock()
            valid = isinstance(operation, str) and _OPERATION.fullmatch(operation) is not None
            row = {"ts": int(now * 1000), "requestId": uuid4().hex, "owner": self.store.owner,
                   "keyId": None, "purpose": operation if valid else None, "kind": None,
                   "domain": None, "payloadSha256": None, "org": None, "target": None,
                   "caller": {"class": "daemon", "actor": "daemon", "sessionRefSha256": None, "pid": None},
                   "decision": "refused", "code": None}
            key_id = None
            try:
                if not valid:
                    raise UserKey.Error(ipc_errors.PURPOSE_UNKNOWN)
                record = self.store.load_for_signing()
                key_id = record.key_id
                self._require_trusted(record, now)
                if not isinstance(self.store.casdoor_sub, str) or not self.store.casdoor_sub:
                    raise UserKey.Error(ipc_errors.USER_KEY_UNBOUND)
                try:
                    raw = base64.b64decode(record.private_key, validate=True)
                except (binascii.Error, ValueError, TypeError):
                    raise UserKey.Error(ipc_errors.USER_KEY_QUARANTINED) from None
                if len(raw) != 32:
                    raise UserKey.Error(ipc_errors.USER_KEY_QUARANTINED)
            except UserKey.Error as error:
                row.update(keyId=key_id, code=error.code)
                self.audit.append(row)
                raise
            row.update(keyId=key_id, decision="signed")
            self.audit.append(row)
            return ProviderKeys.SigningKey(owner=self.store.casdoor_sub, private_key=raw)
