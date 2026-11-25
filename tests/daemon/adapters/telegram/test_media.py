from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.common.transport import (
    NativeMediaDenied,
    NativeMediaScope,
    SealedNativeMediaAuthority,
)
from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    AuthorizedChatTarget,
    AudienceSnapshot,
    ChatRef,
    NativeTargetResolution,
    SealedProviderMediaRef,
    freeze_json_v1,
)
from hyprial.daemon.impl.adapters.telegram import TelegramCredentials
from hyprial.daemon.impl.adapters.telegram.errors import TelegramMediaDenied
from hyprial.daemon.impl.adapters.telegram.media import TelegramMedia
from hyprial.daemon.impl.adapters.telegram.transport import HttpxTelegramTransport

from .support import LocalHttpServer, Request, json_response
from .test_provider import _instance, _reply_target


TOKEN = "123456:" + "A" * 35
BODY = b"telegram-media"


class _Credentials:
    def __init__(self) -> None:
        self.resolves = 0

    async def resolve(self, credential_ref: str) -> TelegramCredentials:
        assert credential_ref == "receiver:telegram"
        self.resolves += 1
        return TelegramCredentials(TOKEN)


class _Scopes:
    def __init__(self) -> None:
        self.forged = False

    def for_file_id(self, file_id: str) -> NativeMediaScope:
        assert file_id == "file_1"
        return self._scope()

    def for_sealed(self, source: SealedProviderMediaRef) -> NativeMediaScope:
        return self._scope(instance_id="telegram-other" if self.forged else "telegram-1")

    @staticmethod
    def _scope(**changes: object) -> NativeMediaScope:
        values = {
            "owner_principal": "agent:owner",
            "organization_id": "org-1",
            "provider": "telegram",
            "instance_id": "telegram-1",
            "generation": 4,
            "target_ref_id": "target-1",
            "content_digest": hashlib.sha256(b"event").hexdigest(),
            **changes,
        }
        return NativeMediaScope(**values)  # type: ignore[arg-type]


class _Stream:
    name = "picture.jpg"
    media_type = "image/jpeg"
    size = len(BODY)
    sha256 = hashlib.sha256(BODY).hexdigest()

    def __init__(self) -> None:
        self._done = False
        self.closed = False
        self.reads = 0

    async def read(self, max_bytes: int) -> bytes:
        self.reads += 1
        if self._done:
            return b""
        self._done = True
        return BODY

    async def aclose(self) -> None:
        self.closed = True


def _operation(**changes: object) -> SimpleNamespace:
    request = {
        "provider": "telegram",
        "organization_id": "org-1",
        "instance_id": "telegram-1",
        "generation": 4,
        "receiver_ref": "receiver:telegram",
        "method": "sendPhoto",
        "chat_id": "-1001",
        "message_thread_id": "77",
        "reply_parameters": {"message_id": "8"},
        "part_index": 2,
        **changes,
    }
    return SimpleNamespace(kind="file-send", sealed_request=freeze_json_v1(request))


def _foreign_instance() -> AdapterInstanceRef:
    fingerprint = hashlib.sha256(b"telegram\0telegram-cloud\0" + b"999999").hexdigest()
    return AdapterInstanceRef(
        "telegram-other", "telegram", "bot", "main", "org-other", fingerprint, 99
    )


def _scoped_target(
    instance: AdapterInstanceRef | None = None, account_scope: str = "123456"
) -> NativeTargetResolution:
    chat = ChatRef(
        "chat-1",
        _instance() if instance is None else instance,
        account_scope,
        "-1001",
        "thread",
        "77",
        "77",
    )
    target = AuthorizedChatTarget(
        "target-1", chat, AudienceSnapshot("group", (), "unknown", None, 1), "reply", "8"
    )
    return NativeTargetResolution(target, "resolved", "-1001", "77", None)


def test_capture_refreshes_file_path_and_seals_only_authorized_bytes(tmp_path: Path) -> None:
    async def scenario() -> None:
        get_file_calls = 0

        async def handler(request: Request):  # type: ignore[no-untyped-def]
            nonlocal get_file_calls
            if request.target.endswith("/getFile"):
                get_file_calls += 1
                path = "expired/old.bin" if get_file_calls == 1 else "documents/new.bin"
                return json_response(
                    200,
                    {
                        "ok": True,
                        "result": {
                            "file_id": "file_1",
                            "file_unique_id": "unique_1",
                            "file_size": len(BODY),
                            "file_path": path,
                        },
                    },
                )
            if request.target.endswith("/expired/old.bin"):
                return json_response(404, {"ok": False})
            assert request.target == f"/file/bot{TOKEN}/documents/new.bin"
            return 200, {"content-type": "application/octet-stream"}, BODY

        authority = SealedNativeMediaAuthority(
            tmp_path / "telegram-media.sqlite3", max_blob_bytes=20 * 1024 * 1024
        )
        scopes = _Scopes()
        async with LocalHttpServer(handler) as server:
            transport = HttpxTelegramTransport(api_base_url=server.base_url)
            media = TelegramMedia(
                transport, authority, scopes, _Credentials(), "receiver:telegram",
                _instance(), "123456",
            )
            sealed = await media.capture(
                "file_1",
                max_bytes=len(BODY),
                expected_sha256=hashlib.sha256(BODY).hexdigest(),
                name="asset.bin",
                media_type="application/octet-stream",
            )
            assert get_file_calls == 2
            metadata = authority.resolve(sealed, scope=scopes.for_sealed(sealed))
            assert dict(metadata.native_metadata) == {
                "file_id": "file_1",
                "file_unique_id": "unique_1",
            }
            stream = await media.fetch(sealed, len(BODY))
            assert await stream.read(1024) == BODY
            scopes.forged = True
            with pytest.raises(NativeMediaDenied, match="SCOPE_DENIED"):
                await media.fetch(sealed, len(BODY))
            await transport.aclose(1000)
        authority.close()

    asyncio.run(scenario())

@pytest.mark.parametrize(
    ("file_id", "result", "reason"),
    [
        ("https://evil.example/a", None, "native-file-id"),
        ("file_1", {"file_id": "file_1", "file_path": "../secret"}, "file-path"),
        ("file_1", {"file_id": "file_1", "file_path": "https://evil.example/a"}, "file-path"),
        (
            "file_1",
            {"file_id": "file_1", "file_path": "a.bin", "file_size": 20 * 1024 * 1024 + 1},
            "too-large",
        ),
    ],
)
def test_capture_rejects_raw_urls_traversal_and_size_forgery(
    tmp_path: Path, file_id: str, result: object, reason: str
) -> None:
    async def scenario() -> None:
        async def handler(request: Request):  # type: ignore[no-untyped-def]
            return json_response(200, {"ok": True, "result": result})

        authority = SealedNativeMediaAuthority(
            tmp_path / f"{reason}.sqlite3", max_blob_bytes=20 * 1024 * 1024
        )
        async with LocalHttpServer(handler) as server:
            transport = HttpxTelegramTransport(api_base_url=server.base_url)
            media = TelegramMedia(
                transport, authority, _Scopes(), _Credentials(), "receiver:telegram",
                _instance(), "123456",
            )
            with pytest.raises(TelegramMediaDenied, match=reason):
                await media.capture(file_id, max_bytes=20 * 1024 * 1024)
            await transport.aclose(1000)
        authority.close()

    asyncio.run(scenario())


def test_upload_preserves_thread_and_reply_and_does_not_close_stream(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(request: Request):  # type: ignore[no-untyped-def]
            assert request.target == f"/bot{TOKEN}/sendPhoto"
            assert b'name="chat_id"\r\n\r\n-1001' in request.body
            assert b'name="message_thread_id"\r\n\r\n77' in request.body
            assert b'name="reply_parameters"' in request.body
            assert b'"message_id":"8"' in request.body
            assert BODY in request.body
            return json_response(200, {"ok": True, "result": {"message_id": 91}})

        authority = SealedNativeMediaAuthority(
            tmp_path / "upload.sqlite3", max_blob_bytes=20 * 1024 * 1024
        )
        async with LocalHttpServer(handler) as server:
            transport = HttpxTelegramTransport(api_base_url=server.base_url)
            media = TelegramMedia(
                transport, authority, _Scopes(), _Credentials(), "receiver:telegram",
                _instance(), "123456",
            )
            stream = _Stream()
            submitted = await media.upload(_operation(), _reply_target(), stream)
            assert submitted.state == "accepted"
            assert submitted.provider_message_id == "91"
            assert not stream.closed
            await transport.aclose(1000)
        authority.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("provider", "slack", "provider-mismatch"),
        ("organization_id", "org-other", "organization-mismatch"),
        ("instance_id", "telegram-other", "instance-mismatch"),
        ("generation", 99, "generation-mismatch"),
    ],
)
def test_upload_rejects_foreign_sealed_scope_before_any_effect(
    tmp_path: Path, field: str, value: object, reason: str
) -> None:
    """A foreign sealed request must die before credentials, stream or HTTP."""

    async def scenario() -> None:
        async def handler(request: Request):  # type: ignore[no-untyped-def]
            return json_response(200, {"ok": True, "result": {"message_id": 91}})

        authority = SealedNativeMediaAuthority(
            tmp_path / "sealed-scope.sqlite3", max_blob_bytes=20 * 1024 * 1024
        )
        credentials = _Credentials()
        async with LocalHttpServer(handler) as server:
            transport = HttpxTelegramTransport(api_base_url=server.base_url)
            media = TelegramMedia(
                transport, authority, _Scopes(), credentials, "receiver:telegram",
                _instance(), "123456",
            )
            stream = _Stream()
            with pytest.raises(TelegramMediaDenied, match=reason):
                await media.upload(_operation(**{field: value}), _reply_target(), stream)
            assert credentials.resolves == 0
            assert stream.reads == 0
            assert server.requests == []
            await transport.aclose(1000)
        authority.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("instance", "account_scope"),
    [
        (_foreign_instance(), "123456"),
        (None, "999999"),
    ],
)
def test_upload_rejects_foreign_target_scope_before_any_effect(
    tmp_path: Path, instance: AdapterInstanceRef | None, account_scope: str
) -> None:
    """A resolved target from another instance/account must die before effects."""

    async def scenario() -> None:
        async def handler(request: Request):  # type: ignore[no-untyped-def]
            return json_response(200, {"ok": True, "result": {"message_id": 91}})

        authority = SealedNativeMediaAuthority(
            tmp_path / "target-scope.sqlite3", max_blob_bytes=20 * 1024 * 1024
        )
        credentials = _Credentials()
        async with LocalHttpServer(handler) as server:
            transport = HttpxTelegramTransport(api_base_url=server.base_url)
            media = TelegramMedia(
                transport, authority, _Scopes(), credentials, "receiver:telegram",
                _instance(), "123456",
            )
            stream = _Stream()
            with pytest.raises(TelegramMediaDenied, match="target-scope-mismatch"):
                await media.upload(
                    _operation(), _scoped_target(instance, account_scope), stream
                )
            assert credentials.resolves == 0
            assert stream.reads == 0
            assert server.requests == []
            await transport.aclose(1000)
        authority.close()

    asyncio.run(scenario())


def test_upload_valid_control_still_reaches_exactly_one_effect_round(
    tmp_path: Path,
) -> None:
    """The in-scope control: one credential resolve, one stream body, one POST."""

    async def scenario() -> None:
        async def handler(request: Request):  # type: ignore[no-untyped-def]
            assert request.target == f"/bot{TOKEN}/sendPhoto"
            assert BODY in request.body
            return json_response(200, {"ok": True, "result": {"message_id": 91}})

        authority = SealedNativeMediaAuthority(
            tmp_path / "valid-control.sqlite3", max_blob_bytes=20 * 1024 * 1024
        )
        credentials = _Credentials()
        async with LocalHttpServer(handler) as server:
            transport = HttpxTelegramTransport(api_base_url=server.base_url)
            media = TelegramMedia(
                transport, authority, _Scopes(), credentials, "receiver:telegram",
                _instance(), "123456",
            )
            stream = _Stream()
            submitted = await media.upload(
                _operation(), _scoped_target(_instance(), "123456"), stream
            )
            assert submitted.state == "accepted"
            assert submitted.provider_message_id == "91"
            assert credentials.resolves == 1
            assert stream.reads >= 1
            assert len(server.requests) == 1
            await transport.aclose(1000)
        authority.close()

    asyncio.run(scenario())
