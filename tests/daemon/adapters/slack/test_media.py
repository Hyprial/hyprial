from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import httpx
import pytest

from hyprial.daemon.impl.adapters.common.transport import (
    HttpEffectState,
    NativeMediaDenied,
    NativeMediaScope,
    SealedNativeMediaAuthority,
)
from hyprial.daemon.impl.adapters.slack import SlackMedia
from hyprial.daemon.impl.adapters.slack.errors import SlackProtocolError

from .support import (
    BOT_TOKEN,
    ByteStream,
    Credentials,
    operation,
    read_request,
    send_json,
    target,
)


def scope(**changes: object) -> NativeMediaScope:
    values: dict[str, object] = {
        "owner_principal": "agent:owner",
        "organization_id": "org-1",
        "provider": "slack",
        "instance_id": "slack-1",
        "generation": 2,
        "target_ref_id": "target-1",
        "content_digest": hashlib.sha256(b"content").hexdigest(),
    }
    values.update(changes)
    return NativeMediaScope(**values)  # type: ignore[arg-type]


class ScopeResolver:
    def __init__(self, value: NativeMediaScope) -> None:
        self.value = value

    def resolve(self, source):
        return self.value


class CountingAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def capture_transport(
    stream: CountingAsyncByteStream,
    *,
    declared_size: int,
    content_length: int,
) -> httpx.MockTransport:
    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files.info"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "file": {
                        "id": "F1",
                        "name": "native.bin",
                        "mimetype": "application/octet-stream",
                        "size": declared_size,
                        "sha256": hashlib.sha256(b"".join(stream.chunks)).hexdigest(),
                        "url_private_download": "https://files.slack.com/download/F1",
                    },
                },
                request=request,
            )
        return httpx.Response(
            200,
            headers={"Content-Length": str(content_length)},
            stream=stream,
            request=request,
        )

    return httpx.MockTransport(respond)


def test_media_upload_alone_runs_external_three_phase_flow_and_consumes_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        body = b"bounded-upload-body"
        calls: list[tuple[str, dict[str, str], bytes]] = []
        server: asyncio.AbstractServer

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            request = await read_request(reader)
            calls.append(request)
            path, headers, request_body = request
            if path.endswith("/files.getUploadURLExternal"):
                assert headers["authorization"] == f"Bearer {BOT_TOKEN}"
                port = server.sockets[0].getsockname()[1]
                await send_json(
                    writer,
                    {
                        "ok": True,
                        "upload_url": f"http://127.0.0.1:{port}/upload/F1",
                        "file_id": "F1",
                    },
                )
            elif path == "POST /upload/F1":
                assert request_body == body
                await send_json(writer, {"ok": True})
            else:
                assert path.endswith("/files.completeUploadExternal")
                await send_json(writer, {"ok": True, "files": [{"id": "F1"}]})

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        stream = ByteStream(body)
        media = SlackMedia(
            client,
            Credentials(),
            "receiver:slack",
            authority=None,
            scope_resolver=None,
            api_base_url=f"http://127.0.0.1:{port}/api",
            allowed_upload_origins={f"http://127.0.0.1:{port}"},
        )
        try:
            result = await media.upload(operation(kind="file-upload"), target(), stream)
            assert result.state == "accepted"
            assert result.provider_message_id == "F1"
            assert stream.closed
            assert [path for path, _, _ in calls] == [
                "POST /api/files.getUploadURLExternal",
                "POST /upload/F1",
                "POST /api/files.completeUploadExternal",
            ]
        finally:
            assert not client.is_closed
            await client.aclose()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("phase", "limit_path", "retry_header", "expected_ms"),
    [
        ("get-upload-url", "/api/files.getUploadURLExternal", "2.5", 2500),
        ("upload-bytes", "/upload/F1", "2.5", 2500),
        ("complete", "/api/files.completeUploadExternal", "2.5", 2500),
        ("get-upload-url", "/api/files.getUploadURLExternal", "soon", None),
    ],
)
def test_external_upload_phase_rate_limits_map_to_retryable_exact_milliseconds(
    phase: str,
    limit_path: str,
    retry_header: str,
    expected_ms: int | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        body = b"rate-limited-upload-body"
        server: asyncio.AbstractServer

        async def limited(
            writer: asyncio.StreamWriter, path: str
        ) -> bool:
            if path != limit_path:
                return False
            await send_json(
                writer,
                {"ok": False, "error": "ratelimited"},
                status=429,
                headers={"Retry-After": retry_header},
            )
            return True

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            path, _headers, _request_body = await read_request(reader)
            port = server.sockets[0].getsockname()[1]
            if path.endswith("/files.getUploadURLExternal"):
                if await limited(writer, "/api/files.getUploadURLExternal"):
                    return
                await send_json(
                    writer,
                    {
                        "ok": True,
                        "upload_url": f"http://127.0.0.1:{port}/upload/F1",
                        "file_id": "F1",
                    },
                )
            elif path == "POST /upload/F1":
                if await limited(writer, "/upload/F1"):
                    return
                await send_json(writer, {"ok": True})
            else:
                assert path.endswith("/files.completeUploadExternal")
                if await limited(writer, "/api/files.completeUploadExternal"):
                    return
                await send_json(writer, {"ok": True, "files": [{"id": "F1"}]})

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        stream = ByteStream(body)
        media = SlackMedia(
            client,
            Credentials(),
            "receiver:slack",
            authority=None,
            scope_resolver=None,
            api_base_url=f"http://127.0.0.1:{port}/api",
            allowed_upload_origins={f"http://127.0.0.1:{port}"},
        )
        try:
            result = await media.upload(
                operation(kind="file-upload"), target(), stream
            )
            assert result.state == "failed-retryable"
            assert result.definitely_not_sent is True
            assert result.reason_code == f"slack-media-{phase}-rate-limited"
            assert result.retry_after_ms == expected_ms
            assert stream.closed
        finally:
            await client.aclose()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_media_post_body_timeout_settles_unknown_with_native_file_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        server: asyncio.AbstractServer
        admitted = asyncio.Event()

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            path, _headers, _body = await read_request(reader)
            port = server.sockets[0].getsockname()[1]
            if path.endswith("/files.getUploadURLExternal"):
                await send_json(
                    writer,
                    {
                        "ok": True,
                        "upload_url": f"http://127.0.0.1:{port}/upload/F-timeout",
                        "file_id": "Ftimeout",
                    },
                )
                return
            admitted.set()
            await asyncio.sleep(0.2)
            writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = httpx.AsyncClient(
            timeout=0.02, trust_env=False, follow_redirects=False
        )
        settled = []
        stream = ByteStream(b"unknown-after-admission")
        media = SlackMedia(
            client,
            Credentials(),
            "receiver:slack",
            authority=None,
            scope_resolver=None,
            api_base_url=f"http://127.0.0.1:{port}/api",
            allowed_upload_origins={f"http://127.0.0.1:{port}"},
            settle=settled.append,
        )
        try:
            result = await media.upload(operation(kind="file-upload"), target(), stream)
            assert admitted.is_set()
            assert result.state == "unknown"
            assert result.reason_code == "slack-media-upload-bytes-unknown"
            assert settled[-1].state is HttpEffectState.UNKNOWN
            assert settled[-1].native_id == "Ftimeout"
            assert stream.closed
        finally:
            await client.aclose()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_fetch_uses_exact_sealed_scope_and_rejects_substitution(tmp_path: Path) -> None:
    async def scenario() -> None:
        authority = SealedNativeMediaAuthority(
            tmp_path / "media.sqlite3", max_blob_bytes=1024
        )
        ref = authority.seal(
            scope=scope(),
            body=b"sealed",
            name="sealed.bin",
            media_type="application/octet-stream",
            native_metadata={"fileId": "F1"},
        )
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        media = SlackMedia(
            client,
            Credentials(),
            "receiver:slack",
            authority=authority,
            scope_resolver=ScopeResolver(scope()),
        )
        opened = await media.fetch(ref, 10)
        assert await opened.read(10) == b"sealed"
        forged = SlackMedia(
            client,
            Credentials(),
            "receiver:slack",
            authority=authority,
            scope_resolver=ScopeResolver(scope(generation=3)),
        )
        with pytest.raises(NativeMediaDenied, match="NATIVE_MEDIA_SCOPE_DENIED"):
            await forged.fetch(ref, 10)
        assert not client.is_closed
        await client.aclose()
        authority.close()

    asyncio.run(scenario())


def test_inbound_capture_accepts_only_native_id_and_seals_url_free_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        payload = b"captured-native-file"
        authority = SealedNativeMediaAuthority(
            tmp_path / "capture.sqlite3", max_blob_bytes=1024
        )
        server: asyncio.AbstractServer

        async def handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            path, headers, _body = await read_request(reader)
            assert headers["authorization"] == f"Bearer {BOT_TOKEN}"
            port = server.sockets[0].getsockname()[1]
            if path.startswith("GET /api/files.info"):
                await send_json(
                    writer,
                    {
                        "ok": True,
                        "file": {
                            "id": "F1",
                            "name": "native.bin",
                            "mimetype": "application/octet-stream",
                            "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                            "url_private_download": f"http://127.0.0.1:{port}/download/F1",
                        },
                    },
                )
            else:
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: "
                    + str(len(payload)).encode()
                    + b"\r\nConnection: close\r\n\r\n"
                    + payload
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        media = SlackMedia(
            client,
            Credentials(),
            "receiver:slack",
            authority=authority,
            scope_resolver=ScopeResolver(scope()),
            api_base_url=f"http://127.0.0.1:{port}/api",
            allowed_download_origins={f"http://127.0.0.1:{port}"},
        )
        try:
            with pytest.raises(
                SlackProtocolError, match="SLACK_MEDIA_NATIVE_ID_INVALID"
            ):
                await media.capture(
                    "http://caller.invalid/F1", scope=scope(), max_bytes=100
                )
            ref = await media.capture("F1", scope=scope(), max_bytes=100)
            metadata = authority.resolve(ref, scope=scope())
            assert dict(metadata.native_metadata) == {"fileId": "F1"}
            assert authority.open(ref, scope=scope(), max_bytes=100).size == len(
                payload
            )
        finally:
            await client.aclose()
            authority.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_inbound_capture_aborts_stream_at_max_plus_one_without_sealing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        stream = CountingAsyncByteStream([b"a", b"b", b"c", b"d", b"e"])
        authority = SealedNativeMediaAuthority(
            tmp_path / "incremental-bound.sqlite3", max_blob_bytes=1024
        )
        seals: list[bytes] = []
        original_seal = authority.seal

        def track_seal(**kwargs):
            seals.append(kwargs["body"])
            return original_seal(**kwargs)

        monkeypatch.setattr(authority, "seal", track_seal)
        client = httpx.AsyncClient(
            transport=capture_transport(
                stream, declared_size=1, content_length=1
            ),
            follow_redirects=False,
        )
        media = SlackMedia(
            client,
            Credentials(),
            "receiver:slack",
            authority=authority,
            scope_resolver=ScopeResolver(scope()),
        )
        try:
            with pytest.raises(
                SlackProtocolError, match="SLACK_MEDIA_DOWNLOAD_REJECTED"
            ):
                await media.capture("F1", scope=scope(), max_bytes=2)
            assert stream.reads == 3
            assert stream.closed
            assert seals == []
        finally:
            await client.aclose()
            authority.close()

    asyncio.run(scenario())


def test_inbound_capture_rejects_advertised_oversize_without_reading_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        stream = CountingAsyncByteStream([b"a", b"b", b"c"])
        authority = SealedNativeMediaAuthority(
            tmp_path / "advertised-bound.sqlite3", max_blob_bytes=1024
        )
        seals: list[bytes] = []
        original_seal = authority.seal

        def track_seal(**kwargs):
            seals.append(kwargs["body"])
            return original_seal(**kwargs)

        monkeypatch.setattr(authority, "seal", track_seal)
        client = httpx.AsyncClient(
            transport=capture_transport(
                stream, declared_size=1, content_length=3
            ),
            follow_redirects=False,
        )
        media = SlackMedia(
            client,
            Credentials(),
            "receiver:slack",
            authority=authority,
            scope_resolver=ScopeResolver(scope()),
        )
        try:
            with pytest.raises(
                SlackProtocolError, match="SLACK_MEDIA_DOWNLOAD_REJECTED"
            ):
                await media.capture("F1", scope=scope(), max_bytes=2)
            assert stream.reads == 0
            assert stream.closed
            assert seals == []
        finally:
            await client.aclose()
            authority.close()

    asyncio.run(scenario())


def test_inbound_capture_streams_exact_bound_and_seals_url_free_metadata(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        stream = CountingAsyncByteStream([b"a", b"b"])
        authority = SealedNativeMediaAuthority(
            tmp_path / "exact-bound.sqlite3", max_blob_bytes=1024
        )
        client = httpx.AsyncClient(
            transport=capture_transport(
                stream, declared_size=2, content_length=2
            ),
            follow_redirects=False,
        )
        media = SlackMedia(
            client,
            Credentials(),
            "receiver:slack",
            authority=authority,
            scope_resolver=ScopeResolver(scope()),
        )
        try:
            ref = await media.capture("F1", scope=scope(), max_bytes=2)
            metadata = authority.resolve(ref, scope=scope())
            assert dict(metadata.native_metadata) == {"fileId": "F1"}
            opened = authority.open(ref, scope=scope(), max_bytes=2)
            assert await opened.read(2) == b"ab"
            await opened.aclose()
            assert stream.reads == 2
            assert stream.closed
        finally:
            await client.aclose()
            authority.close()

    asyncio.run(scenario())
