from __future__ import annotations

import hashlib
from collections.abc import Mapping

from hyprial.daemon.impl.adapters.contracts import AdapterInstanceRef


def instance(generation: int = 3) -> AdapterInstanceRef:
    fingerprint = hashlib.sha256(
        b"matrix\0https://matrix.example\0@bridge:example"
    ).hexdigest()
    return AdapterInstanceRef(
        "matrix-main",
        "matrix",
        "client",
        "main",
        "org-a",
        fingerprint,
        generation,
    )


class StateStore:
    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.commits: list[str] = []
        self.outbound_by_txn: dict[str, object] = {}

    async def load_sync_token(self, instance_id: str, generation: int) -> str | None:
        assert instance_id == "matrix-main"
        assert generation == 3
        return self.token

    async def commit_sync_token(
        self, instance_id: str, generation: int, token: str
    ) -> None:
        assert instance_id == "matrix-main"
        assert generation == 3
        self.token = token
        self.commits.append(token)

    async def record_outbound(
        self,
        instance_id: str,
        generation: int,
        room_id: str,
        transaction_id: str,
        event_id: str | None,
        payload_digest: str,
        outcome: str,
    ) -> None:
        from hyprial.daemon.impl.adapters.matrix import MatrixOutboundRecord

        assert instance_id == "matrix-main"
        assert generation == 3
        self.outbound_by_txn[transaction_id] = MatrixOutboundRecord(
            room_id, event_id, payload_digest, outcome
        )

    async def lookup_outbound_transaction(
        self, instance_id: str, generation: int, transaction_id: str
    ) -> object | None:
        assert instance_id == "matrix-main"
        assert generation == 3
        return self.outbound_by_txn.get(transaction_id)


class Credentials:
    async def resolve(self, credential_ref: str):
        from hyprial.daemon.impl.adapters.matrix import MatrixCredentials

        assert credential_ref == "receiver:matrix"
        return MatrixCredentials(
            instance_id="matrix-main",
            organization_id="org-a",
            generation=3,
            homeserver="https://matrix.example",
            user_id="@bridge:example",
            device_id="DEVICE",
            access_token="receiver-secret-token",
            password=None,
        )


class Transport:
    def __init__(self, sync_rows: list[object] | None = None) -> None:
        self.sync_rows = list(sync_rows or [])
        self.sync_since: list[str | None] = []
        self.send_calls: list[tuple[str, str, object, str]] = []
        self.room_encryption_calls: list[str] = []
        self.reconcile_calls: list[tuple[str, str]] = []
        self.room_encryption_result: object | None = None
        self.reconcile_result: object = None
        self.send_result: object = {"event_id": "$sent"}
        self.connect_calls = 0
        self.close_calls = 0
        self.login_result: object = {
            "user_id": "@bridge:example",
            "device_id": "DEVICE",
            "access_token": "opaque",
        }

    async def login(self, credentials: object) -> object:
        return self.login_result

    async def whoami(self, credentials: object) -> object:
        return {"user_id": "@bridge:example", "device_id": "DEVICE"}

    async def connect(self, credentials: object) -> None:
        self.connect_calls += 1

    async def sync(self, since: str | None, *, timeout_ms: int) -> object:
        self.sync_since.append(since)
        return self.sync_rows.pop(0)

    async def room_encryption(self, room_id: str) -> bool:
        self.room_encryption_calls.append(room_id)
        result = self.room_encryption_result
        if isinstance(result, BaseException):
            raise result
        return room_id.startswith("!encrypted") if result is None else bool(result)

    async def send_event(
        self, room_id: str, event_type: str, content: object, transaction_id: str
    ) -> object:
        self.send_calls.append((room_id, event_type, content, transaction_id))
        if isinstance(self.send_result, BaseException):
            raise self.send_result
        return self.send_result

    async def reconcile_event(self, room_id: str, transaction_id: str) -> object:
        self.reconcile_calls.append((room_id, transaction_id))
        if isinstance(self.reconcile_result, BaseException):
            raise self.reconcile_result
        return self.reconcile_result

    async def close(self) -> None:
        self.close_calls += 1


class Crypto:
    def __init__(
        self,
        *,
        available: bool = True,
        ready: bool = True,
        descriptor: object | None = None,
    ) -> None:
        self.available = available
        self.ready = ready
        self._descriptor = descriptor
        self.decrypt_calls: list[tuple[str, object]] = []
        self.encrypt_calls: list[tuple[str, str, object, str]] = []

    @property
    def dependency(self) -> str:
        return "matrix-nio[e2e]+vodozemac"

    @property
    def descriptor(self) -> object:
        if self._descriptor is not None:
            return self._descriptor
        return {
            "matrix_nio": {
                "package": "matrix-nio[e2e]",
                "version": "0.26.0",
                "artifact": "wheel",
                "artifact_sha256": "1803d7ba6dbc54a101a02695a5a767a1fb53632e46aced9d116990f4a98eadf3",
                "source_commit": "331c93ed3e9ca86e7444b4dfdf05086654842ace",
                "license": "ISC",
            },
            "vodozemac": {
                "package": "vodozemac",
                "version": "0.10.0",
                "artifact": "sdist",
                "artifact_sha256": "9602edb87ab7d16e7b4aad5a087473d8fb6b3ee99043f9aaf0f54bb78970dde9",
                "source_commit": "810836143ea0ca36f1f849f43e63331990c6575a",
                "license": "MIT",
                "embedded_rust": {
                    "package": "vodozemac",
                    "version": "0.10.0",
                    "crate_sha256": "b98bf83c0992966775b8012f194b07b44928996163e5a05b741b43891571ae5b",
                    "license": "Apache-2.0",
                },
            },
        }

    async def prepare(self, instance_id: str, generation: int) -> bool:
        return self.available and self.ready

    async def room_ready(self, room_id: str) -> bool:
        return self.available and self.ready

    async def decrypt(self, room_id: str, event: Mapping[str, object]) -> object:
        if not await self.room_ready(room_id):
            raise RuntimeError("keys unavailable")
        self.decrypt_calls.append((room_id, event))
        return {
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "decrypted"},
        }

    async def encrypt(
        self, room_id: str, event_type: str, content: object, transaction_id: str
    ) -> tuple[str, object]:
        if not await self.room_ready(room_id):
            raise RuntimeError("keys unavailable")
        self.encrypt_calls.append((room_id, event_type, content, transaction_id))
        return "m.room.encrypted", {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "opaque",
            "device_id": "DEVICE",
            "sender_key": "curve25519:sender",
            "session_id": "megolm-session",
        }
