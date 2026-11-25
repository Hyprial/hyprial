from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("nio", reason="install the adapter-matrix extra for SDK tests")

from nio import (
    AsyncClient,
    LoginResponse,
    MatrixRoom,
    MessageDirection,
    MegolmEvent,
    RoomGetStateEventResponse,
    RoomMessageText,
    RoomMessagesResponse,
    RoomSendResponse,
    SyncResponse,
    WhoamiResponse,
)

from hyprial.daemon.impl.adapters.contracts import (
    CustodyReceipt,
    canonical_json_v1,
    freeze_json_v1,
)
from hyprial.daemon.impl.adapters.matrix import (
    MatrixCryptoBlocked,
    MatrixEventCodec,
    MatrixNioCryptoBackend,
    MatrixNioTransport,
    MatrixProductionPaths,
    MatrixProductionUnavailable,
    MatrixRuntime,
    MatrixSqliteStateStore,
    MatrixJsonCredentialResolver,
    REVIEWED_MATRIX_CRYPTO_DESCRIPTOR,
    create_default_matrix_runtime,
    matrix_transaction_id,
)

from .support import instance


def _sync_response(next_batch: str, events: list[dict[str, object]]) -> SyncResponse:
    return SyncResponse.from_dict(
        {
            "next_batch": next_batch,
            "rooms": {
                "join": {
                    "!room:example": {
                        "timeline": {
                            "limited": False,
                            "prev_batch": "p-1",
                            "events": events,
                        },
                        "state": {"events": []},
                        "ephemeral": {"events": []},
                        "account_data": {"events": []},
                        "unread_notifications": {},
                    }
                }
            },
            "device_one_time_keys_count": {},
            "device_lists": {"changed": [], "left": []},
            "to_device": {"events": []},
            "presence": {"events": []},
            "account_data": {"events": []},
        }
    )


def _event(event_id: str = "$event-1") -> dict[str, object]:
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": "@alice:example",
        "origin_server_ts": 1_710_000_000_000,
        "content": {
            "msgtype": "m.image",
            "body": "diagram.png",
            "url": "mxc://example/media-1",
            "info": {"mimetype": "image/png", "size": 12},
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$root",
                "m.in_reply_to": {"event_id": "$parent"},
            },
        },
        "unsigned": {"transaction_id": "txn-from-client"},
    }


class _Olm:
    def __init__(self) -> None:
        self.should_share = True

    def should_share_group_session(self, room_id: str) -> bool:
        assert room_id == "!room:example"
        return self.should_share


class FakeAsyncClient:
    """Deterministic double whose call signatures equal nio 0.26.0 public APIs."""

    created: list[FakeAsyncClient] = []
    sync_responses: list[object] = []
    message_responses: list[object] = []
    send_responses: list[object] = []
    fail_send_once = False

    def __init__(
        self,
        homeserver: str,
        user: str = "",
        device_id: str | None = "",
        store_path: str | None = "",
        config: object | None = None,
        ssl: bool | None = None,
        proxy: str | None = None,
    ) -> None:
        self.homeserver = homeserver
        self.user_id = user
        self.device_id = device_id
        self.store_path = store_path
        self.config = config
        self.ssl = ssl
        self.proxy = proxy
        self.access_token = ""
        self.closed = False
        self.restores: list[tuple[str, str, str]] = []
        self.login_calls: list[tuple[str | None, str | None, str | None]] = []
        self.sync_calls: list[tuple[int | None, str | None]] = []
        self.send_calls: list[tuple[str, str, object, str | None, bool]] = []
        self.message_calls: list[tuple[str, str | None, int]] = []
        self.keys_upload_calls = 0
        self.keys_query_calls = 0
        self.share_calls: list[tuple[str, bool]] = []
        self.olm = _Olm()
        room = MatrixRoom("!room:example", "@bridge:example", encrypted=True)
        room.members_synced = True
        room.add_member("@bridge:example", "Bridge", None)
        self.rooms = {"!room:example": room}
        self.should_upload_keys = True
        self.should_query_keys = True
        type(self).created.append(self)

    async def login(
        self,
        password: str | None = None,
        device_name: str | None = "",
        token: str | None = None,
    ) -> LoginResponse:
        self.login_calls.append((password, device_name, token))
        self.user_id = "@bridge:example"
        self.device_id = "DEVICE"
        self.access_token = "sdk-login-token"
        return LoginResponse(self.user_id, self.device_id, self.access_token)

    def restore_login(self, user_id: str, device_id: str, access_token: str) -> None:
        self.restores.append((user_id, device_id, access_token))
        self.user_id = user_id
        self.device_id = device_id
        self.access_token = access_token

    async def whoami(self) -> WhoamiResponse:
        return WhoamiResponse(self.user_id, self.device_id, False)

    async def sync(
        self,
        timeout: int | None = 0,
        sync_filter: None | str | dict[object, object] = None,
        since: str | None = None,
        full_state: bool | None = None,
        set_presence: str | None = None,
    ) -> object:
        del sync_filter, full_state, set_presence
        self.sync_calls.append((timeout, since))
        return type(self).sync_responses.pop(0)

    async def room_get_state_event(
        self, room_id: str, event_type: str, state_key: str = ""
    ) -> RoomGetStateEventResponse:
        assert (room_id, event_type, state_key) == (
            "!room:example",
            "m.room.encryption",
            "",
        )
        return RoomGetStateEventResponse(
            {"algorithm": "m.megolm.v1.aes-sha2"}, event_type, state_key, room_id
        )

    async def room_send(
        self,
        room_id: str,
        message_type: str,
        content: dict[object, object],
        tx_id: str | None = None,
        ignore_unverified_devices: bool = False,
    ) -> object:
        self.send_calls.append(
            (room_id, message_type, content, tx_id, ignore_unverified_devices)
        )
        if type(self).fail_send_once:
            type(self).fail_send_once = False
            raise TimeoutError("response lost after homeserver accepted the txn")
        if type(self).send_responses:
            return type(self).send_responses.pop(0)
        return RoomSendResponse("$sent", room_id)

    async def room_messages(
        self,
        room_id: str,
        start: str | None = None,
        end: str | None = None,
        direction: MessageDirection = MessageDirection.back,
        limit: int = 10,
        message_filter: dict[object, object] | None = None,
    ) -> object:
        del end, direction, message_filter
        self.message_calls.append((room_id, start, limit))
        return type(self).message_responses.pop(0)

    async def keys_upload(self) -> object:
        self.keys_upload_calls += 1
        self.should_upload_keys = False
        return SimpleNamespace()

    async def keys_query(self) -> object:
        self.keys_query_calls += 1
        self.should_query_keys = False
        return SimpleNamespace()

    async def share_group_session(
        self, room_id: str, ignore_unverified_devices: bool = False
    ) -> object:
        self.share_calls.append((room_id, ignore_unverified_devices))
        self.olm.should_share = False
        return SimpleNamespace()

    def room_contains_unverified(self, room_id: str) -> bool:
        assert room_id == "!room:example"
        return False

    def encrypt(
        self, room_id: str, message_type: str, content: dict[object, object]
    ) -> tuple[str, dict[str, str]]:
        assert (room_id, message_type) == ("!room:example", "m.room.message")
        assert content["body"] == "secret words"
        return "m.room.encrypted", {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "opaque-megolm-ciphertext",
            "device_id": "DEVICE",
            "sender_key": "curve25519:sender",
            "session_id": "megolm-session",
        }

    def decrypt_event(self, event: MegolmEvent) -> RoomMessageText:
        assert isinstance(event, MegolmEvent)
        assert event.room_id == "!room:example"
        return RoomMessageText.from_dict(
            {
                "type": "m.room.message",
                "event_id": event.event_id,
                "sender": event.sender,
                "origin_server_ts": event.server_timestamp,
                "content": {"msgtype": "m.text", "body": "decrypted secret"},
            }
        )

    async def close(self) -> None:
        self.closed = True


def _credential_file(path: Path, *, password: bool = False) -> None:
    value: dict[str, object] = {
        "credential_ref": "receiver:matrix",
        "instance_id": "matrix-main",
        "organization_id": "org-a",
        "generation": 3,
        "homeserver": "https://matrix.example",
        "user_id": "@bridge:example",
        "device_id": "DEVICE",
        "access_token": None if password else "receiver-secret-token",
        "password": "receiver-password" if password else None,
    }
    path.write_text(json.dumps(value), encoding="utf-8")


def _paths(tmp_path: Path) -> MatrixProductionPaths:
    credential_path = tmp_path / "matrix-credential.json"
    _credential_file(credential_path)
    return MatrixProductionPaths(
        credential_path=credential_path,
        checkpoint_path=tmp_path / "matrix-checkpoint.sqlite3",
        crypto_store_path=tmp_path / "matrix-crypto",
    )


def _reset_fake() -> None:
    FakeAsyncClient.created = []
    FakeAsyncClient.sync_responses = []
    FakeAsyncClient.message_responses = []
    FakeAsyncClient.send_responses = []
    FakeAsyncClient.fail_send_once = False


def _operation(*, key: str, encrypted: bool) -> SimpleNamespace:
    room_id = "!room:example"
    state = "encrypted" if encrypted else "plaintext"
    digest = hashlib.sha256(
        (
            "hyprial-matrix-room-encryption-v1\0"
            f"matrix-main\0{3}\0{room_id}\0{state}"
        ).encode("utf-8")
    ).hexdigest()
    return SimpleNamespace(
        kind="text-send",
        idempotency_key=key,
        sealed_request=freeze_json_v1(
            {
                "provider": "matrix",
                "organization_id": "org-a",
                "instance_id": "matrix-main",
                "generation": 3,
                "room_id": room_id,
                "event_type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "secret words"},
                "encrypted": encrypted,
                "room_encryption_evidence": (
                    f"matrix-room-encryption-v1:{state}:{digest}"
                ),
                "part_index": 0,
            }
        ),
    )


def test_fake_matches_installed_nio_public_method_signatures() -> None:
    for name in (
        "login",
        "restore_login",
        "whoami",
        "sync",
        "room_get_state_event",
        "room_send",
        "room_messages",
        "keys_upload",
        "keys_query",
        "share_group_session",
        "room_contains_unverified",
        "encrypt",
        "decrypt_event",
        "close",
    ):
        fake = inspect.signature(getattr(FakeAsyncClient, name)).parameters
        actual = inspect.signature(getattr(AsyncClient, name)).parameters
        assert tuple(fake) == tuple(actual)
        assert tuple((item.kind, item.default) for item in fake.values()) == tuple(
            (item.kind, item.default) for item in actual.values()
        )


def test_json_credential_ref_and_https_authority_are_fixed_before_login(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        _reset_fake()
        credential_path = tmp_path / "credential.json"
        _credential_file(credential_path, password=True)
        resolver = MatrixJsonCredentialResolver(
            credential_path, instance(), "receiver:matrix"
        )
        credentials = await resolver.resolve("receiver:matrix")
        with pytest.raises(ValueError, match="credential ref"):
            await resolver.resolve("receiver:other")

        transport = MatrixNioTransport(
            tmp_path / "crypto", client_factory=FakeAsyncClient
        )
        login = await transport.login(credentials)
        assert login == {
            "user_id": "@bridge:example",
            "device_id": "DEVICE",
            "access_token": "sdk-login-token",
        }
        assert FakeAsyncClient.created[0].homeserver == "https://matrix.example"
        assert FakeAsyncClient.created[0].login_calls == [
            ("receiver-password", "hyprial:matrix-main:3", None)
        ]

    asyncio.run(scenario())


def test_sync_cursor_mapping_is_durable_across_real_runtime_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        _reset_fake()
        paths = _paths(tmp_path)
        FakeAsyncClient.sync_responses = [
            _sync_response("s-2", [_event()]),
            _sync_response("s-3", []),
        ]
        first = create_default_matrix_runtime(
            instance(),
            "receiver:matrix",
            paths,
            client_factory=FakeAsyncClient,
            now_ms=lambda: 1_000,
        )
        await first.start(instance(), object())
        unit = await first.next_ingress()
        assert FakeAsyncClient.created[0].sync_calls == [(30_000, None)]
        await first.commit_checkpoint(
            unit.checkpoint,
            CustodyReceipt(
                "journal-1", unit.envelope.observation.observation_digest, 1
            ),
        )
        await first.close(2_000)

        restarted = create_default_matrix_runtime(
            instance(),
            "receiver:matrix",
            paths,
            client_factory=FakeAsyncClient,
        )
        await restarted.start(instance(), object())
        with pytest.raises(StopAsyncIteration):
            await restarted.next_ingress()
        assert FakeAsyncClient.created[2].sync_calls == [(30_000, "s-2")]

    asyncio.run(scenario())


def test_sdk_sync_frame_preserves_dm_thread_reply_media_and_txn_mapping(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        _reset_fake()
        FakeAsyncClient.sync_responses = [_sync_response("s-2", [_event()])]
        runtime = create_default_matrix_runtime(
            instance(),
            "receiver:matrix",
            _paths(tmp_path),
            client_factory=FakeAsyncClient,
            now_ms=lambda: 1_000,
        )
        await runtime.start(instance(), object())
        unit = await runtime.next_ingress()
        normalized, provenance = MatrixEventCodec(instance()).decode(
            await runtime.hydrate(unit.envelope)
        )
        assert normalized.context.source_chat.kind == "thread"
        assert normalized.context.source_chat.native_thread_id == "$root"
        assert normalized.context.channel_id.startswith("adapter:matrix:")
        assert normalized.reply_to.source_native_message_id == "$parent"
        assert normalized.attachments[0].handle_id.endswith("mxc://example/media-1")
        assert provenance.txn_id == "txn-from-client"
        assert provenance.media_mxc == ("mxc://example/media-1",)

    asyncio.run(scenario())


def test_real_sdk_sync_keeps_ciphertext_in_custody_until_reviewed_hydration(
    tmp_path: Path,
) -> None:
    encrypted_source = {
        "type": "m.room.encrypted",
        "event_id": "$encrypted-event",
        "sender": "@alice:example",
        "origin_server_ts": 1_710_000_000_000,
        "content": {
            "algorithm": "m.megolm.v1.aes-sha2",
            "ciphertext": "opaque-megolm-ciphertext",
            "device_id": "ALICE",
            "sender_key": "curve25519:alice",
            "session_id": "megolm-session",
        },
        "unsigned": {"transaction_id": "txn-encrypted"},
    }

    class RealSyncAsyncClient(AsyncClient):
        handled_event: RoomMessageText | None = None

        async def whoami(self) -> WhoamiResponse:
            return WhoamiResponse(self.user_id, self.device_id, False)

        async def sync(
            self,
            timeout: int | None = 0,
            sync_filter: None | str | dict[object, object] = None,
            since: str | None = None,
            full_state: bool | None = None,
            set_presence: str | None = None,
        ) -> object:
            del timeout, sync_filter, since, full_state, set_presence
            room = MatrixRoom("!room:example", self.user_id, encrypted=True)
            room.members_synced = True
            room.add_member(self.user_id, "Bridge", None)
            self.rooms[room.room_id] = room
            decrypted = RoomMessageText.from_dict(
                {
                    "type": "m.room.message",
                    "event_id": "$encrypted-event",
                    "sender": "@alice:example",
                    "origin_server_ts": 1_710_000_000_000,
                    "content": {
                        "msgtype": "m.text",
                        "body": "plaintext-before-hydrate",
                    },
                    "unsigned": {"transaction_id": "txn-encrypted"},
                }
            )
            decrypted.decrypted = True
            assert self.olm is not None
            self.olm._decrypt_megolm_no_error = lambda event, room_id=None: decrypted
            response = _sync_response("s-real", [encrypted_source])
            await self.receive_response(response)
            handled = response.rooms.join["!room:example"].timeline.events[0]
            assert isinstance(handled, RoomMessageText)
            assert handled.decrypted is True
            type(self).handled_event = handled
            return response

    class ReviewedGateCrypto:
        def __init__(self, transport: MatrixNioTransport) -> None:
            self.descriptor = MatrixNioCryptoBackend(transport).descriptor
            self.ready = False
            self.decrypt_calls: list[tuple[str, dict[str, object]]] = []

        async def prepare(self, instance_id: str, generation: int) -> bool:
            assert (instance_id, generation) == ("matrix-main", 3)
            return True

        async def room_ready(self, room_id: str) -> bool:
            assert room_id == "!room:example"
            return self.ready

        async def decrypt(self, room_id: str, event: object) -> object:
            assert isinstance(event, dict)
            self.decrypt_calls.append((room_id, dict(event)))
            return {
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "plaintext-after-hydrate"},
            }

    async def scenario() -> None:
        paths = _paths(tmp_path)
        resolver = MatrixJsonCredentialResolver(
            paths.credential_path, instance(), "receiver:matrix"
        )
        transport = MatrixNioTransport(
            paths.crypto_store_path, client_factory=RealSyncAsyncClient
        )
        crypto = ReviewedGateCrypto(transport)
        runtime = MatrixRuntime(
            transport,
            MatrixSqliteStateStore(paths.checkpoint_path, instance()),
            resolver,
            "receiver:matrix",
            crypto=crypto,
            now_ms=lambda: 1_000,
        )
        await runtime.start(instance(), object())
        try:
            unit = await runtime.next_ingress()

            assert RealSyncAsyncClient.handled_event is not None
            assert RealSyncAsyncClient.handled_event.decrypted is True
            sealed = canonical_json_v1(unit.envelope.payload)
            assert b"plaintext-before-hydrate" not in sealed
            assert b"plaintext-after-hydrate" not in sealed
            assert b"opaque-megolm-ciphertext" in sealed
            assert b'"type":"m.room.encrypted"' in sealed
            assert unit.hydration_required is True
            assert unit.envelope.observation.aliases[0].normalized_id == "$encrypted-event"

            with pytest.raises(MatrixCryptoBlocked, match="matrix-e2e-keys-unavailable"):
                await runtime.hydrate(unit.envelope)
            assert crypto.decrypt_calls == []

            crypto.ready = True
            hydrated = await runtime.hydrate(unit.envelope)
            assert b"plaintext-after-hydrate" in canonical_json_v1(hydrated.body)
            assert b"plaintext-after-hydrate" not in canonical_json_v1(
                unit.envelope.payload
            )
            assert crypto.decrypt_calls[0][1]["type"] == "m.room.encrypted"
            assert crypto.decrypt_calls[0][1]["content"] == encrypted_source["content"]
            _, provenance = MatrixEventCodec(instance()).decode(hydrated)
            assert provenance.encrypted is True
        finally:
            await runtime.close(2_000)

    asyncio.run(scenario())


def test_room_query_key_readiness_and_megolm_descriptor_encrypt_decrypt(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        _reset_fake()
        paths = _paths(tmp_path)
        resolver = MatrixJsonCredentialResolver(
            paths.credential_path, instance(), "receiver:matrix"
        )
        credentials = await resolver.resolve("receiver:matrix")
        transport = MatrixNioTransport(
            paths.crypto_store_path, client_factory=FakeAsyncClient
        )
        crypto = MatrixNioCryptoBackend(transport)
        await transport.connect(credentials)
        assert crypto.descriptor == REVIEWED_MATRIX_CRYPTO_DESCRIPTOR
        assert await transport.room_encryption("!room:example") is True
        assert await crypto.prepare("matrix-main", 3) is True
        assert await crypto.room_ready("!room:example") is True
        primary = FakeAsyncClient.created[0]
        assert primary.keys_upload_calls == 1
        assert primary.keys_query_calls == 1
        assert primary.share_calls == [("!room:example", False)]

        event_type, ciphertext = await crypto.encrypt(
            "!room:example",
            "m.room.message",
            {"msgtype": "m.text", "body": "secret words"},
            "txn-1",
        )
        assert event_type == "m.room.encrypted"
        assert set(ciphertext) == {
            "algorithm",
            "ciphertext",
            "device_id",
            "sender_key",
            "session_id",
        }
        decrypted = await crypto.decrypt(
            "!room:example",
            {
                "type": "m.room.encrypted",
                "event_id": "$encrypted",
                "sender": "@alice:example",
                "origin_server_ts": 1,
                "content": ciphertext,
            },
        )
        assert decrypted == {
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "decrypted secret"},
        }

    asyncio.run(scenario())


def test_txn_unknown_is_not_resent_and_reconciles_via_sdk_room_messages(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        _reset_fake()
        paths = _paths(tmp_path)
        runtime = create_default_matrix_runtime(
            instance(),
            "receiver:matrix",
            paths,
            client_factory=FakeAsyncClient,
        )
        await runtime.start(instance(), object())
        operation = _operation(key="unknown-send", encrypted=True)
        FakeAsyncClient.fail_send_once = True
        unknown = await runtime.submit(operation)
        assert unknown.submissions[0].state == "unknown"
        send_client = FakeAsyncClient.created[1]
        transaction_id = matrix_transaction_id("unknown-send")
        assert send_client.send_calls[0][3] == transaction_id

        replay = await runtime.submit(operation)
        assert replay.submissions[0].state == "unknown"
        assert len(send_client.send_calls) == 1

        FakeAsyncClient.message_responses = [
            RoomMessagesResponse.from_dict(
                {
                    "start": "m0",
                    "end": "m1",
                    "chunk": [
                        {
                            "type": "m.room.encrypted",
                            "event_id": "$accepted-late",
                            "sender": "@bridge:example",
                            "origin_server_ts": 2,
                            "content": {
                                "algorithm": "m.megolm.v1.aes-sha2",
                                "ciphertext": "opaque",
                                "device_id": "DEVICE",
                                "sender_key": "curve25519:sender",
                                "session_id": "megolm-session",
                            },
                            "unsigned": {"transaction_id": transaction_id},
                        }
                    ],
                },
                "!room:example",
            )
        ]
        accepted = await runtime.reconcile(operation, replay)
        assert accepted.submissions[0].provider_message_id == "$accepted-late"
        assert len(send_client.send_calls) == 1

    asyncio.run(scenario())


def test_reconnect_and_close_replace_and_close_both_sdk_clients(tmp_path: Path) -> None:
    async def scenario() -> None:
        _reset_fake()
        runtime = create_default_matrix_runtime(
            instance(),
            "receiver:matrix",
            _paths(tmp_path),
            client_factory=FakeAsyncClient,
        )
        await runtime.start(instance(), object())
        original = tuple(FakeAsyncClient.created)
        await runtime.reconnect("network-reset")
        assert all(client.closed for client in original)
        replacement = tuple(FakeAsyncClient.created[2:])
        assert len(replacement) == 2
        outcome = await runtime.close(2_000)
        assert outcome.state == "closed"
        assert all(client.closed for client in replacement)

    asyncio.run(scenario())


def test_default_factory_is_constructible_and_absence_is_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    assert isinstance(
        create_default_matrix_runtime(instance(), "receiver:matrix", paths),
        MatrixRuntime,
    )
    transport = MatrixNioTransport(paths.crypto_store_path)
    assert MatrixNioCryptoBackend(transport).descriptor == (
        REVIEWED_MATRIX_CRYPTO_DESCRIPTOR
    )

    def unavailable() -> object:
        raise ModuleNotFoundError("nio")

    monkeypatch.setattr(transport, "_load_sdk", unavailable)

    async def scenario() -> None:
        credentials = await MatrixJsonCredentialResolver(
            paths.credential_path, instance(), "receiver:matrix"
        ).resolve("receiver:matrix")
        with pytest.raises(MatrixProductionUnavailable) as raised:
            await transport.connect(credentials)
        assert raised.value.reason_code == "matrix-e2e-dependency-unavailable"

    asyncio.run(scenario())


def test_sqlite_state_store_rejects_other_generation_and_persists_unknown(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "state.sqlite3"
        state = MatrixSqliteStateStore(path, instance())
        await state.record_outbound(
            "matrix-main", 3, "!room:example", "txn", None, "digest", "unknown"
        )
        reopened = MatrixSqliteStateStore(path, instance())
        assert (await reopened.lookup_outbound_transaction("matrix-main", 3, "txn")).outcome == (
            "unknown"
        )
        with pytest.raises(ValueError, match="generation"):
            await reopened.load_sync_token("matrix-main", 4)

    asyncio.run(scenario())
