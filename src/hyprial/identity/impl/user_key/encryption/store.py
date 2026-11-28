"""Local-only encryption key custody, explicitly provisioned by a daemon caller.

No login/IPC hook activates this core. Identity inputs must eventually come from
trusted daemon/device context, not an agent request. Local records do not prove
Casdoor identity or device ownership. Never replace a quarantined/revoked key
implicitly: replacement requires the future rewrap/read-back lifecycle.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from hyprial.kernel import atomic_json_write, ipc_errors, read_private_file
from ..crypto import decode
from ..models import Error
from ..store import Store as SigningStore
from .crypto import exchange, generate, validate_pair
from .models import Record


def _unique_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


class Store:
    def __init__(self, *, state_dir: Path, kind: str, casdoor_sub: str | None,
                 device_uid: str, fingerprint: str):
        def valid(value):
            return (isinstance(value, str) and bool(value) and len(value) <= 1024
                    and not any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value))

        if (kind not in ("user", "host") or not valid(device_uid) or not valid(fingerprint)
                or (kind == "user" and not valid(casdoor_sub))
                or (kind == "host" and casdoor_sub is not None)):
            raise Error(ipc_errors.PAYLOAD_INVALID)
        self.kind, self.casdoor_sub = kind, casdoor_sub
        self.device_uid, self.fingerprint = device_uid, fingerprint
        # Structured, collision-resistant locator; never interpolate sub/device
        # into a path. Full identity is rechecked when reading each record.
        locator = json.dumps([kind, casdoor_sub, device_uid], separators=(",", ":")).encode()
        slug = hashlib.sha256(locator).hexdigest()
        # Reuse U1's privacy/locking mechanics, NOT its signing records/lifecycle.
        self._fence = SigningStore(state_dir=state_dir, owner="encryption-local",
                                  casdoor_sub=casdoor_sub or "host", device_uid=device_uid,
                                  fingerprint=fingerprint)
        self.root = Path(state_dir) / "identity" / "encryption-keys" / slug
        self._fence.root = self.root

    def _write(self, record: Record) -> None:
        self._fence.prepare()
        path = self.root / "key.json"
        self._fence.check_file(path, absent=True)
        atomic_json_write(path, record.disk())

    def _read(self) -> Record:
        path = self.root / "key.json"
        self._fence.check_file(path)
        try:
            raw, _ = read_private_file(path, max_bytes=16384)
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
            fields = {"schemaVersion", "algorithm", "keyKind", "casdoorSub", "deviceUid",
                      "machineFingerprint", "keyId", "publicKey", "privateKey", "createdAt", "state"}
            if (not isinstance(value, dict) or set(value) not in (fields, fields - {"privateKey"})
                    or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1
                    or value["algorithm"] != "X25519" or value["keyKind"] != self.kind
                    or value["casdoorSub"] != self.casdoor_sub or value["deviceUid"] != self.device_uid
                    or value["state"] not in ("local", "quarantined", "revoked")
                    or any(not isinstance(v, str) or not v for k, v in value.items()
                           if k not in {"schemaVersion", "casdoorSub"})):
                raise ValueError
            private = value.get("privateKey")
            if (value["state"] != "revoked" and private is None) or (
                    value["state"] == "revoked" and private is not None):
                raise ValueError
            validate_pair(value["publicKey"], private, value["keyId"])
            record = Record(self.kind, self.casdoor_sub, self.device_uid,
                            value["machineFingerprint"], value["keyId"], value["publicKey"],
                            private, value["createdAt"], value["state"])
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            raise Error(ipc_errors.USER_KEY_QUARANTINED) from None
        if record.fingerprint != self.fingerprint and record.state == "local":
            record = replace(record, state="quarantined")
            self._write(record)
        return record

    @staticmethod
    def _require_local(record: Record) -> None:
        if record.state == "quarantined":
            raise Error(ipc_errors.USER_KEY_QUARANTINED)
        if record.state != "local" or record.private_key is None:
            raise Error(ipc_errors.USER_KEY_UNBOUND)

    def ensure(self) -> dict:
        """Idempotent explicit local provisioning, not trusted publication."""
        with self._fence.locked():
            try:
                record = self._read()
            except Error as error:
                if error.code != ipc_errors.USER_KEY_ABSENT:
                    raise
                kid, public, private = generate()
                record = Record(self.kind, self.casdoor_sub, self.device_uid, self.fingerprint,
                                kid, public, private, datetime.now(UTC).isoformat())
                self._write(record)
            self._require_local(record)
            return record.public()

    def status(self) -> dict:
        with self._fence.locked():
            try:
                return self._read().public()
            except Error as error:
                if error.code != ipc_errors.USER_KEY_ABSENT:
                    raise
                return {"algorithm": "X25519", "keyKind": self.kind,
                        "casdoorSub": self.casdoor_sub, "deviceUid": self.device_uid,
                        "keyId": None, "publicKey": None, "state": "absent"}

    def exchange(self, peer_public_key: str, *, key_id: str) -> bytes:
        """Internal crypto operation only. No private-key export or IPC wrapper."""
        with self._fence.locked():
            record = self._read()
            self._require_local(record)
            if key_id != record.key_id:
                raise Error(ipc_errors.USER_KEY_ABSENT)
            return exchange(record.private_key, peer_public_key)

    def _require_custody(self, owner_key=None, *, check_owner: bool = True) -> None:
        """Sealed custody is user-scoped: never a host key, never another sub's envelope."""
        if self.kind != "user" or (check_owner and getattr(owner_key, "owner", None) != self.casdoor_sub):
            raise Error(ipc_errors.USER_KEY_UNBOUND)

    def recipient(self):
        """This device's public recipient; requires an active local key."""
        from hyprial.identity import ProviderKeys
        self._require_custody(check_owner=False)
        with self._fence.locked():
            record = self._read()
            self._require_local(record)
            return ProviderKeys.Recipient(record.key_id, decode(record.public_key, 32))

    def open_sealed(self, envelope: dict, *, owner_key) -> bytes:
        """Verify and open a sealed envelope with this device's key, under the store lock."""
        from hyprial.identity import ProviderKeys
        self._require_custody(owner_key)
        with self._fence.locked():
            record = self._read()
            self._require_local(record)
            return ProviderKeys.open_envelope(envelope, owner_key=owner_key, recipient_key_id=record.key_id,
                                              private_key=decode(record.private_key, 32))

    def open_service_grant(
        self, envelope: dict, *, org: str, grant_id: str, grantor: str,
        service_device: str, port: int, recipient_owner: str, recipient_device: str,
    ) -> bytes:
        """Open a service-grant credential sealed to this device's key; no owner key is involved.

        The caller passes structured grant fields from the document it read; this
        store supplies its own encryptionKeyId and rebuilds the expected header via
        service_grant_header. An envelope whose own header differs is refused.
        Custody stays user-scoped.
        """
        from ...sealed_envelope import open_service_grant
        self._require_custody(check_owner=False)
        with self._fence.locked():
            record = self._read()
            self._require_local(record)
            return open_service_grant(
                envelope, org=org, grant_id=grant_id, grantor=grantor,
                service_device=service_device, port=port,
                recipient_owner=recipient_owner, recipient_device=recipient_device,
                encryption_key_id=record.key_id, private_key=decode(record.private_key, 32),
            )

    def probe_sealed(self, envelope: dict, *, owner_key) -> bool:
        """Verify and unwrap this device's DEK without decrypting the payload; raise on any refusal."""
        from hyprial.identity import ProviderKeys
        self._require_custody(owner_key)
        with self._fence.locked():
            record = self._read()
            self._require_local(record)
            ProviderKeys.unwrap_check(envelope, owner_key=owner_key, recipient_key_id=record.key_id,
                                      private_key=decode(record.private_key, 32))
            return True

    def reseal_sealed(self, envelope: dict, *, owner_key, signer, recipients, created_at: int) -> dict:
        """Re-seal under a fresh DEK (dekVersion + 1) after opening with this device's key."""
        from hyprial.identity import ProviderKeys
        self._require_custody(owner_key)
        with self._fence.locked():
            record = self._read()
            self._require_local(record)
            return ProviderKeys.reseal(envelope, owner_key=owner_key, recipient_key_id=record.key_id,
                                       private_key=decode(record.private_key, 32), signer=signer,
                                       recipients=recipients, created_at=created_at)

    def revoke(self) -> dict:
        """Local disable/scrub only; NOT remote revocation or provider rewrap.

        This destructive primitive is intentionally not wired to a command.
        A future orchestrator must first satisfy the rewrap lifecycle.
        """
        with self._fence.locked():
            record = self._read()
            if record.state == "quarantined":
                raise Error(ipc_errors.USER_KEY_QUARANTINED)
            if record.state != "revoked":
                record = replace(record, state="revoked", private_key=None)
                self._write(record)
            return record.public()
