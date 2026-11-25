"""Fenced daemon-state key storage; login is the only generation operation."""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyprial.identity.impl.agents.home.provisioner import AgentHomeProvisioner
from hyprial.identity.impl.identity_slug import identity_slug
from hyprial.kernel import (
    ProcessFileLock,
    atomic_json_write,
    inspect_private_file,
    ipc_errors,
    private_access_supported,
    read_private_file,
)
from .crypto import decode, generate, key_id
from .models import Error, Record


class Store:
    def __init__(self, *, state_dir: Path, owner: str, casdoor_sub: str,
                 device_uid: str, fingerprint: str):
        self.state_dir = Path(state_dir)
        self.owner = owner
        self.casdoor_sub = casdoor_sub
        self.device_uid = device_uid
        self.fingerprint = fingerprint
        try:
            slug = identity_slug(owner)
            if (not self.state_dir.is_absolute() or slug in {".", ".."}
                    or not slug[0].isalnum() or len(slug) > 200):
                raise ValueError
        except (ValueError, TypeError, IndexError):
            raise Error(ipc_errors.USER_KEY_QUARANTINED) from None
        self.root = self.state_dir / "identity" / "user-keys" / slug

    @staticmethod
    def supported() -> bool:
        """No permissive fallback for missing privacy or locking primitives."""
        return private_access_supported()

    @staticmethod
    def require_supported() -> None:
        if not Store.supported():
            raise Error(ipc_errors.USER_KEY_UNBOUND)

    def prepare(self) -> None:
        """Fence before every operation, including access to a cached Store."""
        self.require_supported()
        paths = (self.state_dir, self.state_dir / "identity", self.root.parent, self.root)
        try:
            for index, path in enumerate(paths):
                if not (path.exists() or path.is_symlink()):
                    try:
                        path.mkdir(mode=0o700, parents=index == 0)
                    except FileExistsError:
                        pass  # Concurrent creation is validated below.
                AgentHomeProvisioner.safe_directory(
                    path, self.owner, "user-key", mode=None if index == 0 else 0o700)
        except (OSError, RuntimeError, ValueError):
            raise Error(ipc_errors.USER_KEY_QUARANTINED) from None

    @staticmethod
    def check_file(path: Path, *, absent: bool = False) -> None:
        Store.require_supported()
        try:
            inspect_private_file(path)
        except FileNotFoundError:
            if absent:
                return
            raise Error(ipc_errors.USER_KEY_ABSENT) from None
        except (OSError, RuntimeError, ValueError):
            raise Error(ipc_errors.USER_KEY_QUARANTINED) from None

    @contextmanager
    def locked(self):
        self.prepare()
        path = self.root / ".lock"
        lock = ProcessFileLock(path)
        try:
            lock.acquire(timeout=15.0)
            self.check_file(path)
            self.prepare()
        except Error:
            raise
        except (OSError, RuntimeError, ValueError):
            raise Error(ipc_errors.USER_KEY_QUARANTINED) from None
        try:
            yield
        finally:
            lock.release()

    def _write(self, record: Record) -> None:
        self.prepare()
        self.check_file(self.root / "key.json", absent=True)
        atomic_json_write(self.root / "key.json", record.disk())

    def _read(self) -> Record:
        path = self.root / "key.json"
        self.check_file(path)
        try:
            raw, _ = read_private_file(path, max_bytes=16384)
            value = json.loads(raw.decode("utf-8"))
            fields = {"schemaVersion", "algorithm", "keyId", "publicKey", "privateKey", "owner",
                      "casdoorSub", "deviceUid", "machineFingerprint", "createdAt", "state"}
            if (not isinstance(value, dict) or set(value) not in (fields, fields - {"privateKey"})
                    or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1
                    or value["algorithm"] != "Ed25519"
                    or value["state"] not in {"unbound", "bound", "quarantined", "retired"}
                    or any(not isinstance(v, str) or not v for k, v in value.items() if k != "schemaVersion")
                    or value["owner"] != self.owner):
                raise ValueError
            public = decode(value["publicKey"], 32)
            if value["keyId"] != key_id(public):
                raise ValueError
            private = value.get("privateKey")
            if private is not None:
                if Ed25519PrivateKey.from_private_bytes(decode(private, 32)).public_key().public_bytes_raw() != public:
                    raise ValueError
            elif value["state"] not in {"retired", "quarantined"}:
                raise ValueError
            record = Record(value["owner"], value["casdoorSub"], value["deviceUid"],
                            value["machineFingerprint"], value["keyId"], value["publicKey"],
                            private, value["createdAt"], value["state"])
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            raise Error(ipc_errors.USER_KEY_QUARANTINED) from None
        if (record.machine_fingerprint != self.fingerprint
                or record.device_uid != self.device_uid or record.casdoor_sub != self.casdoor_sub):
            if record.state != "quarantined":
                record = replace(record, state="quarantined")
                self._write(record)
        return record

    def load_for_signing(self) -> Record:
        """Private material is consumed by the local signer, never an IPC handler."""
        with self.locked():
            record = self._read()
            if record.state == "quarantined":
                raise Error(ipc_errors.USER_KEY_QUARANTINED)
            if record.state == "retired" or record.private_key is None:
                raise Error(ipc_errors.USER_KEY_UNBOUND)
            return record

    def status(self) -> dict:
        with self.locked():
            try:
                return self._read().public()
            except Error as error:
                if error.code != ipc_errors.USER_KEY_ABSENT:
                    raise
                return {"keyId": None, "publicKey": None, "state": "absent",
                        "bindingExp": None, "revocationSeq": None}

    def ensure(self) -> dict:
        """Idempotent login generation, without binding or network access."""
        if any(not isinstance(v, str) or not v or len(v) > 1024
               for v in (self.owner, self.casdoor_sub, self.device_uid, self.fingerprint)):
            raise Error(ipc_errors.USER_KEY_ABSENT)
        with self.locked():
            try:
                record = self._read()
            except Error as error:
                if error.code != ipc_errors.USER_KEY_ABSENT:
                    raise
            else:
                if record.state in {"unbound", "bound"}:
                    return record.public()
                quarantine = self.root / "quarantine"
                try:
                    quarantine.mkdir(mode=0o700, exist_ok=True)
                    AgentHomeProvisioner.safe_directory(quarantine, self.owner, "quarantine", mode=0o700)
                    destination = quarantine / (record.key_id + ".json")
                    self.check_file(destination, absent=True)
                    # Scrub the current file first. A crash can leave only a
                    # quarantined public record, never an archived private key.
                    record = replace(record, private_key=None)
                    self._write(record)
                    atomic_json_write(destination, record.disk(include_private=False))
                except (OSError, RuntimeError, ValueError):
                    raise Error(ipc_errors.USER_KEY_QUARANTINED) from None
            kid, public, private = generate()
            record = Record(self.owner, self.casdoor_sub, self.device_uid, self.fingerprint,
                            kid, public, private, datetime.now(UTC).isoformat())
            self._write(record)
            return record.public()
