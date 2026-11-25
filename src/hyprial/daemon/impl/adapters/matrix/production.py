"""Production Matrix I/O backed by matrix-nio's public client APIs."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.kernel import harden_private_path
from ..contracts import AdapterInstanceRef
from .errors import MatrixAdapterError, MatrixDefinitelyFailed, MatrixRateLimited
from .models import (
    MatrixCredentials,
    MatrixOutboundRecord,
    REVIEWED_MATRIX_CRYPTO_DESCRIPTOR,
)


class MatrixProductionUnavailable(MatrixAdapterError):
    """A typed refusal when the reviewed Matrix production stack is unavailable."""

    def __init__(self, reason_code: str) -> None:
        if not isinstance(reason_code, str) or not reason_code:
            raise ValueError("reason_code is required")
        self.reason_code = reason_code
        super().__init__(reason_code)


def _absolute_path(value: str | os.PathLike[str], name: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"Matrix {name} must be an absolute path")
    return path


@dataclass(frozen=True, slots=True)
class MatrixProductionPaths:
    """Explicit durable locations for credentials, checkpoints, and crypto."""

    credential_path: Path
    checkpoint_path: Path
    crypto_store_path: Path

    def __post_init__(self) -> None:
        for name in ("credential_path", "checkpoint_path", "crypto_store_path"):
            object.__setattr__(self, name, _absolute_path(getattr(self, name), name))
        if len({self.credential_path, self.checkpoint_path, self.crypto_store_path}) != 3:
            raise ValueError("Matrix production paths must be distinct")


def _check_instance(instance: AdapterInstanceRef) -> None:
    if instance.kind != "matrix" or instance.variant != "client":
        raise ValueError("Matrix production components require matrix/client authority")


class MatrixJsonCredentialResolver:
    """Resolve one opaque credential ref from an explicit receiver-owned JSON file."""

    _FIELDS = {
        "credential_ref",
        "instance_id",
        "organization_id",
        "generation",
        "homeserver",
        "user_id",
        "device_id",
        "access_token",
        "password",
    }

    def __init__(
        self,
        path: str | os.PathLike[str],
        instance: AdapterInstanceRef,
        credential_ref: str,
    ) -> None:
        _check_instance(instance)
        if not isinstance(credential_ref, str) or not credential_ref:
            raise ValueError("Matrix credential_ref is required")
        self._path = _absolute_path(path, "credential path")
        self._instance = instance
        self._credential_ref = credential_ref

    async def resolve(self, credential_ref: str) -> MatrixCredentials:
        if credential_ref != self._credential_ref:
            raise ValueError("Matrix credential ref mismatch")
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise MatrixProductionUnavailable("matrix-credentials-unavailable") from exc
        if not isinstance(value, dict) or set(value) != self._FIELDS:
            raise ValueError("Matrix credential document has an invalid schema")
        if value["credential_ref"] != credential_ref:
            raise ValueError("Matrix credential ref mismatch")
        expected = {
            "instance_id": self._instance.instance_id,
            "organization_id": self._instance.organization_id,
            "generation": self._instance.generation,
        }
        for field, expected_value in expected.items():
            if value[field] != expected_value:
                raise ValueError(f"Matrix credential {field} mismatch")
        return MatrixCredentials(
            instance_id=value["instance_id"],
            organization_id=value["organization_id"],
            generation=value["generation"],
            homeserver=value["homeserver"],
            user_id=value["user_id"],
            device_id=value["device_id"],
            access_token=value["access_token"],
            password=value["password"],
        )


class MatrixSqliteStateStore:
    """Generation-fenced durable sync and outbound transaction state."""

    def __init__(
        self, path: str | os.PathLike[str], instance: AdapterInstanceRef
    ) -> None:
        _check_instance(instance)
        self._path = _absolute_path(path, "checkpoint path")
        self._instance_id = instance.instance_id
        self._generation = instance.generation
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30.0)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")  # durability: FULL -- matrix production state is cross-restart custody authority
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS matrix_sync_cursor (
                    instance_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    token TEXT NOT NULL,
                    PRIMARY KEY (instance_id, generation)
                );
                CREATE TABLE IF NOT EXISTS matrix_outbound (
                    instance_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    transaction_id TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    event_id TEXT,
                    payload_digest TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN ('accepted', 'unknown')),
                    PRIMARY KEY (instance_id, generation, transaction_id)
                );
                """
            )
        harden_private_path(self._path, mode=0o600)

    def _fence(self, instance_id: str, generation: int) -> None:
        if instance_id != self._instance_id:
            raise ValueError("Matrix state instance mismatch")
        if generation != self._generation:
            raise ValueError("Matrix state generation mismatch")

    async def load_sync_token(self, instance_id: str, generation: int) -> str | None:
        self._fence(instance_id, generation)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT token FROM matrix_sync_cursor "
                "WHERE instance_id = ? AND generation = ?",
                (instance_id, generation),
            ).fetchone()
        return None if row is None else str(row[0])

    async def commit_sync_token(
        self, instance_id: str, generation: int, token: str
    ) -> None:
        self._fence(instance_id, generation)
        if not isinstance(token, str) or not token:
            raise ValueError("Matrix sync token is required")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO matrix_sync_cursor(instance_id, generation, token) "
                "VALUES (?, ?, ?) ON CONFLICT(instance_id, generation) "
                "DO UPDATE SET token = excluded.token",
                (instance_id, generation, token),
            )

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
        self._fence(instance_id, generation)
        record = MatrixOutboundRecord(room_id, event_id, payload_digest, outcome)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO matrix_outbound("
                "instance_id, generation, transaction_id, room_id, event_id, "
                "payload_digest, outcome) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(instance_id, generation, transaction_id) DO UPDATE SET "
                "room_id = excluded.room_id, event_id = excluded.event_id, "
                "payload_digest = excluded.payload_digest, outcome = excluded.outcome",
                (
                    instance_id,
                    generation,
                    transaction_id,
                    record.room_id,
                    record.event_id,
                    record.payload_digest,
                    record.outcome,
                ),
            )

    async def lookup_outbound_transaction(
        self, instance_id: str, generation: int, transaction_id: str
    ) -> MatrixOutboundRecord | None:
        self._fence(instance_id, generation)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT room_id, event_id, payload_digest, outcome "
                "FROM matrix_outbound WHERE instance_id = ? AND generation = ? "
                "AND transaction_id = ?",
                (instance_id, generation, transaction_id),
            ).fetchone()
        return None if row is None else MatrixOutboundRecord(*row)


def _response_error(response: object) -> bool:
    return type(response).__name__.endswith("Error")


def _required_attr(value: object, name: str) -> str:
    result = getattr(value, name, None)
    if not isinstance(result, str) or not result:
        raise MatrixAdapterError(f"Matrix SDK response missing {name}")
    return result


class MatrixNioTransport:
    """Matrix transport that adapts matrix-nio responses to the runtime port."""

    def __init__(
        self,
        crypto_store_path: str | os.PathLike[str],
        *,
        client_factory: Callable[..., object] | None = None,
    ) -> None:
        self._crypto_store_path = _absolute_path(
            crypto_store_path, "crypto store path"
        )
        self._client_factory = client_factory
        self._client: object | None = None
        self._send_client: object | None = None
        self._credentials: MatrixCredentials | None = None
        self._login: dict[str, str] | None = None
        self._sync_ciphertext: dict[
            int, dict[tuple[str, str], dict[str, object]]
        ] = {}
        self._original_receive_response: Callable[[object], object] | None = None

    @staticmethod
    def _load_sdk() -> object:
        try:
            nio = importlib.import_module("nio")
            importlib.import_module("vodozemac")
            if importlib.metadata.version("matrix-nio") != "0.26.0":
                raise MatrixProductionUnavailable("matrix-e2e-dependency-unreviewed")
            if importlib.metadata.version("vodozemac") != "0.10.0":
                raise MatrixProductionUnavailable("matrix-e2e-dependency-unreviewed")
            return nio
        except MatrixProductionUnavailable:
            raise
        except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
            raise MatrixProductionUnavailable(
                "matrix-e2e-dependency-unavailable"
            ) from exc

    def _sdk(self) -> object:
        try:
            return self._load_sdk()
        except MatrixProductionUnavailable:
            raise
        except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
            raise MatrixProductionUnavailable(
                "matrix-e2e-dependency-unavailable"
            ) from exc

    @property
    def client(self) -> object | None:
        return self._client

    @property
    def credentials(self) -> MatrixCredentials | None:
        return self._credentials

    def _factory(self, sdk: object) -> Callable[..., object]:
        return self._client_factory or getattr(sdk, "AsyncClient")

    @staticmethod
    def _sync_event_key(
        room_id: object, source: Mapping[str, object]
    ) -> tuple[str, str]:
        if not isinstance(room_id, str) or not room_id:
            raise MatrixAdapterError("Matrix SDK sync room id is invalid")
        event_id = source.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise MatrixAdapterError("Matrix SDK sync event id is invalid")
        return room_id, event_id

    def _capture_sync_ciphertext(self, response: object) -> None:
        """Snapshot encrypted SDK events before nio replaces them with plaintext."""

        rooms = getattr(getattr(response, "rooms", None), "join", None)
        if not isinstance(rooms, dict):
            return
        captured: dict[tuple[str, str], dict[str, object]] = {}
        for room_id, room_info in rooms.items():
            timeline = getattr(getattr(room_info, "timeline", None), "events", None)
            if not isinstance(timeline, list):
                raise MatrixAdapterError("Matrix SDK sync timeline is invalid")
            for event in timeline:
                source = getattr(event, "source", None)
                if not isinstance(source, dict) or source.get("type") != "m.room.encrypted":
                    continue
                key = self._sync_event_key(room_id, source)
                frozen = json.loads(json.dumps(source, ensure_ascii=False))
                if not isinstance(frozen, dict):
                    raise MatrixAdapterError("Matrix SDK encrypted event is invalid")
                previous = captured.get(key)
                if previous is not None and previous != frozen:
                    raise MatrixAdapterError("Matrix SDK event identity is ambiguous")
                captured[key] = frozen
        if captured:
            self._sync_ciphertext[id(response)] = captured

    def _bind_sync_ciphertext_capture(self, client: object) -> None:
        receive_response = getattr(client, "receive_response", None)
        if not callable(receive_response):
            return
        self._original_receive_response = receive_response

        async def receive_with_ciphertext_custody(response: object) -> None:
            self._capture_sync_ciphertext(response)
            await receive_response(response)

        setattr(client, "receive_response", receive_with_ciphertext_custody)

    def _unbind_sync_ciphertext_capture(self, client: object) -> None:
        if self._original_receive_response is None:
            return
        setattr(client, "receive_response", self._original_receive_response)
        self._original_receive_response = None

    def _custodied_event_source(
        self,
        room_id: str,
        event: object,
        captured: dict[tuple[str, str], dict[str, object]],
    ) -> dict[str, object]:
        source = getattr(event, "source", None)
        if not isinstance(source, dict):
            raise MatrixAdapterError("Matrix SDK event source is invalid")
        key = self._sync_event_key(room_id, source)
        encrypted = captured.pop(key, None)
        sdk_decrypted = getattr(event, "decrypted", False)
        if sdk_decrypted is True:
            if encrypted is None:
                raise MatrixAdapterError(
                    "Matrix SDK decrypted event lost ciphertext custody"
                )
            for name in ("event_id", "sender", "origin_server_ts"):
                if source.get(name) != encrypted.get(name):
                    raise MatrixAdapterError(
                        "Matrix SDK decrypted event identity is inconsistent"
                    )
            event_room_id = getattr(event, "room_id", room_id)
            if event_room_id != room_id:
                raise MatrixAdapterError(
                    "Matrix SDK decrypted event room identity is inconsistent"
                )
            return encrypted
        if encrypted is not None and source.get("type") != "m.room.encrypted":
            raise MatrixAdapterError("Matrix SDK plaintext event lacks decrypted marker")
        return dict(source) if encrypted is None else encrypted

    def _make_clients(self, credentials: MatrixCredentials) -> tuple[object, object]:
        sdk = self._sdk()
        self._crypto_store_path.mkdir(parents=True, exist_ok=True)
        harden_private_path(self._crypto_store_path, mode=0o700)
        store_name = hashlib_sha256(
            f"{credentials.instance_id}\0{credentials.generation}"
        )
        crypto_config = sdk.AsyncClientConfig(
            encryption_enabled=True,
            store_name=store_name,
            store_sync_tokens=False,
        )
        send_config = sdk.AsyncClientConfig(
            store=sdk.store.SqliteMemoryStore,
            encryption_enabled=False,
            store_sync_tokens=False,
        )
        factory = self._factory(sdk)
        primary = factory(
            credentials.homeserver,
            credentials.user_id,
            credentials.device_id or "",
            str(self._crypto_store_path),
            config=crypto_config,
        )
        self._bind_sync_ciphertext_capture(primary)
        sender = factory(
            credentials.homeserver,
            credentials.user_id,
            credentials.device_id or "",
            "",
            config=send_config,
        )
        return primary, sender

    @staticmethod
    def _credential_key(credentials: MatrixCredentials) -> tuple[object, ...]:
        return (
            credentials.instance_id,
            credentials.organization_id,
            credentials.generation,
            credentials.homeserver,
            credentials.user_id,
            credentials.device_id,
            credentials.access_token,
            credentials.password,
        )

    async def connect(self, credentials: MatrixCredentials) -> None:
        await self.close()
        primary, send_client = self._make_clients(credentials)
        try:
            if credentials.access_token is not None:
                if credentials.device_id is None:
                    raise ValueError("Matrix access token requires a device id")
                primary.restore_login(
                    credentials.user_id,
                    credentials.device_id,
                    credentials.access_token,
                )
                send_client.restore_login(
                    credentials.user_id,
                    credentials.device_id,
                    credentials.access_token,
                )
                login = {
                    "user_id": credentials.user_id,
                    "device_id": credentials.device_id,
                    "access_token": credentials.access_token,
                }
            else:
                response = await primary.login(
                    password=credentials.password,
                    device_name=(
                        f"hyprial:{credentials.instance_id}:{credentials.generation}"
                    ),
                )
                if _response_error(response):
                    raise MatrixProductionUnavailable("matrix-login-failed")
                user_id = _required_attr(response, "user_id")
                device_id = _required_attr(response, "device_id")
                access_token = _required_attr(response, "access_token")
                if user_id != credentials.user_id:
                    raise ValueError("Matrix authenticated user mismatch")
                if credentials.device_id is not None and device_id != credentials.device_id:
                    raise ValueError("Matrix authenticated device mismatch")
                send_client.restore_login(user_id, device_id, access_token)
                login = {
                    "user_id": user_id,
                    "device_id": device_id,
                    "access_token": access_token,
                }
            whoami = await primary.whoami()
            if _response_error(whoami):
                raise MatrixProductionUnavailable("matrix-whoami-failed")
            if _required_attr(whoami, "user_id") != credentials.user_id:
                raise ValueError("Matrix authenticated user mismatch")
            whoami_device = getattr(whoami, "device_id", None)
            if credentials.device_id is not None and whoami_device != credentials.device_id:
                raise ValueError("Matrix authenticated device mismatch")
        except Exception:
            self._unbind_sync_ciphertext_capture(primary)
            await primary.close()
            await send_client.close()
            raise
        self._client = primary
        self._send_client = send_client
        self._credentials = credentials
        self._login = login

    async def login(self, credentials: MatrixCredentials) -> object:
        if (
            self._credentials is None
            or self._credential_key(self._credentials) != self._credential_key(credentials)
        ):
            await self.connect(credentials)
        assert self._login is not None
        return dict(self._login)

    async def whoami(self, credentials: MatrixCredentials) -> object:
        await self.login(credentials)
        assert self._client is not None
        response = await self._client.whoami()
        if _response_error(response):
            raise MatrixProductionUnavailable("matrix-whoami-failed")
        return {
            "user_id": _required_attr(response, "user_id"),
            "device_id": getattr(response, "device_id", None),
        }

    def _require_client(self) -> object:
        if self._client is None:
            raise MatrixAdapterError("Matrix transport is not connected")
        return self._client

    async def sync(self, since: str | None, *, timeout_ms: int) -> object:
        client = self._require_client()
        response = await client.sync(timeout=timeout_ms, since=since)
        if _response_error(response):
            raise MatrixAdapterError("Matrix sync failed")
        rooms = getattr(getattr(response, "rooms", None), "join", None)
        if not isinstance(rooms, dict):
            raise MatrixAdapterError("Matrix SDK sync rooms are invalid")
        joined: dict[str, object] = {}
        captured = self._sync_ciphertext.pop(id(response), {})
        sdk_rooms = getattr(client, "rooms", {})
        for room_id, room_info in rooms.items():
            timeline = getattr(getattr(room_info, "timeline", None), "events", None)
            if not isinstance(timeline, list):
                raise MatrixAdapterError("Matrix SDK sync timeline is invalid")
            events: list[object] = []
            for event in timeline:
                events.append(self._custodied_event_source(room_id, event, captured))
            sdk_room = sdk_rooms.get(room_id) if isinstance(sdk_rooms, dict) else None
            is_direct = bool(sdk_room is not None and not sdk_room.is_group)
            room_name = None
            if sdk_room is not None:
                candidate = sdk_room.display_name
                room_name = candidate if isinstance(candidate, str) else None
            joined[room_id] = {
                "is_direct": is_direct,
                "name": room_name,
                "timeline": {"events": events},
            }
        if captured:
            raise MatrixAdapterError("Matrix SDK ciphertext custody is incomplete")
        return {
            "next_batch": _required_attr(response, "next_batch"),
            "rooms": {"join": joined},
        }

    async def room_encryption(self, room_id: str) -> bool:
        response = await self._require_client().room_get_state_event(
            room_id, "m.room.encryption", ""
        )
        if _response_error(response):
            status = str(getattr(response, "status_code", ""))
            message = str(getattr(response, "message", ""))
            if status == "404" or "M_NOT_FOUND" in message:
                return False
            raise MatrixAdapterError("Matrix room encryption query failed")
        content = getattr(response, "content", None)
        if not isinstance(content, dict):
            raise MatrixAdapterError("Matrix room encryption state is invalid")
        algorithm = content.get("algorithm")
        if algorithm != "m.megolm.v1.aes-sha2":
            raise MatrixAdapterError("Matrix room encryption algorithm is unsupported")
        return True

    async def send_event(
        self, room_id: str, event_type: str, content: object, transaction_id: str
    ) -> object:
        if self._send_client is None:
            raise MatrixAdapterError("Matrix transport is not connected")
        if not isinstance(content, Mapping):
            raise ValueError("Matrix send content must be an object")
        response = await self._send_client.room_send(
            room_id,
            event_type,
            dict(content),
            tx_id=transaction_id,
            ignore_unverified_devices=False,
        )
        if _response_error(response):
            retry_after = getattr(response, "retry_after_ms", None)
            if isinstance(retry_after, int) and not isinstance(retry_after, bool):
                raise MatrixRateLimited(retry_after)
            code = getattr(response, "status_code", None)
            raise MatrixDefinitelyFailed(
                f"matrix-send-rejected:{code}" if code else "matrix-send-rejected"
            )
        return {"event_id": _required_attr(response, "event_id")}

    async def reconcile_event(
        self, room_id: str, transaction_id: str
    ) -> object | None:
        if self._send_client is None:
            raise MatrixAdapterError("Matrix transport is not connected")
        response = await self._send_client.room_messages(room_id, limit=100)
        if _response_error(response):
            raise MatrixAdapterError("Matrix transaction reconciliation failed")
        chunk = getattr(response, "chunk", None)
        if not isinstance(chunk, list):
            raise MatrixAdapterError("Matrix room messages response is invalid")
        own_user = self._credentials.user_id if self._credentials is not None else None
        for event in chunk:
            source = getattr(event, "source", None)
            if not isinstance(source, dict) or source.get("sender") != own_user:
                continue
            unsigned = source.get("unsigned")
            if not isinstance(unsigned, dict):
                continue
            if unsigned.get("transaction_id") == transaction_id:
                event_id = source.get("event_id")
                if isinstance(event_id, str) and event_id:
                    return {"event_id": event_id}
        return None

    async def close(self) -> None:
        clients = tuple(
            client for client in (self._client, self._send_client) if client is not None
        )
        if self._client is not None:
            self._unbind_sync_ciphertext_capture(self._client)
        self._client = None
        self._send_client = None
        self._credentials = None
        self._login = None
        self._sync_ciphertext.clear()
        for client in clients:
            await client.close()


def hashlib_sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _thaw_descriptor(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_descriptor(child) for key, child in value.items()}
    return value


class MatrixNioCryptoBackend:
    """Megolm backend bound to the transport's reviewed nio/vodozemac session."""

    def __init__(self, transport: MatrixNioTransport) -> None:
        self._transport = transport

    @property
    def dependency(self) -> str:
        return "matrix-nio[e2e]+vodozemac"

    @property
    def descriptor(self) -> object:
        self._transport._sdk()
        return _thaw_descriptor(REVIEWED_MATRIX_CRYPTO_DESCRIPTOR)

    def _client(self) -> object | None:
        return self._transport.client

    async def prepare(self, instance_id: str, generation: int) -> bool:
        credentials = self._transport.credentials
        client = self._client()
        if credentials is None or client is None:
            return False
        if credentials.instance_id != instance_id:
            raise ValueError("Matrix crypto instance mismatch")
        if credentials.generation != generation:
            raise ValueError("Matrix crypto generation mismatch")
        if getattr(client, "olm", None) is None:
            return False
        if getattr(client, "should_upload_keys", False):
            if _response_error(await client.keys_upload()):
                return False
        if getattr(client, "should_query_keys", False):
            if _response_error(await client.keys_query()):
                return False
        return True

    async def room_ready(self, room_id: str) -> bool:
        client = self._client()
        credentials = self._transport.credentials
        if client is None or credentials is None:
            return False
        if not await self.prepare(credentials.instance_id, credentials.generation):
            return False
        rooms = getattr(client, "rooms", None)
        room = rooms.get(room_id) if isinstance(rooms, dict) else None
        if (
            room is None
            or room.encrypted is not True
            or room.members_synced is not True
            or client.room_contains_unverified(room_id)
        ):
            return False
        olm = getattr(client, "olm", None)
        if olm is None:
            return False
        if olm.should_share_group_session(room_id):
            response = await client.share_group_session(
                room_id, ignore_unverified_devices=False
            )
            if _response_error(response):
                return False
        return True

    async def encrypt(
        self, room_id: str, event_type: str, content: object, transaction_id: str
    ) -> tuple[str, object]:
        del transaction_id
        client = self._client()
        if client is None or not await self.room_ready(room_id):
            raise MatrixProductionUnavailable("matrix-e2e-keys-unavailable")
        if not isinstance(content, Mapping):
            raise ValueError("Matrix encryption content must be an object")
        encrypted_type, encrypted_content = client.encrypt(
            room_id, event_type, dict(content)
        )
        if not isinstance(encrypted_type, str) or not isinstance(
            encrypted_content, dict
        ):
            raise MatrixAdapterError("Matrix SDK encryption result is invalid")
        return encrypted_type, encrypted_content

    async def decrypt(self, room_id: str, event: Mapping[str, object]) -> object:
        client = self._client()
        if client is None or not await self.room_ready(room_id):
            raise MatrixProductionUnavailable("matrix-e2e-keys-unavailable")
        sdk = self._transport._sdk()
        source = dict(event)
        source["room_id"] = room_id
        megolm = sdk.MegolmEvent.from_dict(source)
        if type(megolm).__name__.startswith("Bad"):
            raise MatrixAdapterError("Matrix Megolm event is invalid")
        decrypted = client.decrypt_event(megolm)
        decrypted_source = getattr(decrypted, "source", None)
        if not isinstance(decrypted_source, dict):
            raise MatrixAdapterError("Matrix decrypted event is invalid")
        event_type = decrypted_source.get("type")
        content = decrypted_source.get("content")
        if not isinstance(event_type, str) or not isinstance(content, dict):
            raise MatrixAdapterError("Matrix decrypted event is invalid")
        return {"type": event_type, "content": dict(content)}


def create_default_matrix_runtime(
    instance: AdapterInstanceRef,
    credential_ref: str,
    paths: MatrixProductionPaths,
    *,
    client_factory: Callable[..., object] | None = None,
    now_ms: object | None = None,
) -> Any:
    """Build the existing MatrixRuntime from concrete durable components."""

    from .runtime import MatrixRuntime

    _check_instance(instance)
    credentials = MatrixJsonCredentialResolver(
        paths.credential_path, instance, credential_ref
    )
    state = MatrixSqliteStateStore(paths.checkpoint_path, instance)
    transport = MatrixNioTransport(
        paths.crypto_store_path, client_factory=client_factory
    )
    crypto = MatrixNioCryptoBackend(transport)
    return MatrixRuntime(
        transport,
        state,
        credentials,
        credential_ref,
        crypto=crypto,
        now_ms=now_ms,
    )


__all__ = [
    "MatrixJsonCredentialResolver",
    "MatrixNioCryptoBackend",
    "MatrixNioTransport",
    "MatrixProductionPaths",
    "MatrixProductionUnavailable",
    "MatrixSqliteStateStore",
    "create_default_matrix_runtime",
]
