"""Real loopback media REDs: multipart upload, sealed fetch, inbound capture."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.common.transport import (
    NativeMediaDenied,
    SealedNativeMediaAuthority,
)
from hyprial.daemon.impl.adapters.contracts import (
    AuthorizedChatTarget,
    AudienceSnapshot,
    ChatRef,
    freeze_json_v1,
)
from hyprial.daemon.impl.adapters.discord import (
    DiscordGatewayRuntime,
    DiscordEndpointPolicy,
    DiscordGatewayTransport,
    DiscordMedia,
    DiscordMediaScopeError,
    MAX_DISCORD_UPLOAD_BYTES,
    discord_nonce,
)

from .support import (
    Credentials,
    Ledger,
    LoopbackGateway,
    ScriptedHttpServer,
    discord_api_handler,
    instance_ref,
    json_response,
    raw_response,
)

CHUNK = 64 * 1024


class ByteStream:
    """AuthorizedByteStream stand-in that tracks reads and close."""

    def __init__(
        self,
        body: bytes,
        *,
        name: str | None = "a.txt",
        media_type: str | None = "text/plain",
        declared_size: int | None = None,
        declared_sha256: str | None = None,
        chunk: int = CHUNK,
    ) -> None:
        self._body = body
        self._offset = 0
        self.name = name
        self.media_type = media_type
        self.size = len(body) if declared_size is None else declared_size
        self.sha256 = declared_sha256 or hashlib.sha256(body).hexdigest()
        self._chunk = chunk
        self.read_calls = 0
        self.bytes_read = 0
        self.closed = False

    async def read(self, max_bytes: int) -> bytes:
        self.read_calls += 1
        end = min(len(self._body), self._offset + min(max_bytes, self._chunk))
        chunk = self._body[self._offset : end]
        self._offset = end
        self.bytes_read += len(chunk)
        return chunk

    async def aclose(self) -> None:
        self.closed = True


class EndlessStream:
    """Declares an over-sized upload without materializing 25 MiB."""

    def __init__(self) -> None:
        self.name = "big.bin"
        self.media_type = "application/octet-stream"
        self.size = MAX_DISCORD_UPLOAD_BYTES + 1
        self.sha256 = "0" * 64
        self.read_calls = 0
        self.bytes_read = 0

    async def read(self, max_bytes: int) -> bytes:
        self.read_calls += 1
        if self.bytes_read >= self.size:
            return b""
        take = min(max_bytes, CHUNK, self.size - self.bytes_read)
        self.bytes_read += take
        return b"x" * take

    async def aclose(self) -> None:
        return None


class CountingAuthority:
    def __init__(self, inner: SealedNativeMediaAuthority) -> None:
        self._inner = inner
        self.open_calls = 0
        self.seal_calls = 0

    def seal(self, **kwargs: object) -> object:
        self.seal_calls += 1
        return self._inner.seal(**kwargs)  # type: ignore[arg-type]

    def open(self, source: object, *, scope: object, max_bytes: int) -> object:
        self.open_calls += 1
        return self._inner.open(source, scope=scope, max_bytes=max_bytes)  # type: ignore[arg-type]

    def resolve(self, source: object, *, scope: object) -> object:
        return self._inner.resolve(source, scope=scope)  # type: ignore[arg-type]

    def close(self) -> None:
        self._inner.close()


@dataclass
class MediaHarness:
    transport: DiscordGatewayTransport
    media: DiscordMedia
    authority: CountingAuthority
    http: ScriptedHttpServer
    responses: list

    async def stop(self) -> None:
        await self.http.stop()
        deadline = asyncio.get_running_loop().time() + 2.0
        await self.transport.aclose(deadline)
        self.authority.close()


def _scope_facts(**overrides: object) -> SimpleNamespace:
    return SimpleNamespace(
        owner_principal="principal-1",
        organization_id="org-1",
        account_scope="app-1",
        instance_id="discord-1",
        generation=3,
        **overrides,
    )


def _file_operation(**overrides: object) -> SimpleNamespace:
    request = {
        "provider": "discord",
        "organization_id": "org-1",
        "instance_id": "discord-1",
        "generation": 3,
        "receiver_ref": "receiver:discord",
        "action": "create_file_message",
        "channel_id": "c-1",
        "handle_id": "handle-1",
        "content": "",
        "filename": "a.txt",
        "part_index": 0,
        **overrides,
    }
    return SimpleNamespace(
        sealed_request=freeze_json_v1(request),
        kind="file-send",
        idempotency_key="file-1",
    )


def _resolution(
    *,
    status: str = "resolved",
    account_scope: str = "app-1",
    organization_id: str = "org-1",
    generation: int = 3,
) -> object:
    from hyprial.daemon.impl.adapters.contracts import NativeTargetResolution

    instance = instance_ref(generation=generation)
    instance = (
        instance
        if organization_id == "org-1"
        else replace(instance, organization_id=organization_id)
    )
    chat_ref = ChatRef(
        "chat-ref:v1:discord:discord-1:c-1",
        instance,  # type: ignore[arg-type]
        account_scope,
        "c-1",
        "group",
        None,
        None,
    )
    audience = AudienceSnapshot("group", (), "partial", None, 1)
    target = AuthorizedChatTarget("target-1", chat_ref, audience, "send", None)
    return NativeTargetResolution(
        target,  # type: ignore[arg-type]
        status,
        "c-1" if status == "resolved" else None,
        None,
        None if status == "resolved" else "discord-target-unresolved",
    )


async def _media_harness(
    responses: list, tmp_path: Path, *, scope: object | None = None
) -> MediaHarness:
    from collections import deque

    queue = deque(responses)

    def handler(exchange: object) -> object:
        path = getattr(exchange, "path", "")
        if path == "/oauth2/applications/@me":
            return json_response({"id": "app-1"})
        if path == "/users/@me":
            return json_response({"id": "bot-1", "bot": True})
        return queue.popleft() if queue else None

    http = ScriptedHttpServer(handler)
    base = await http.start()
    policy = DiscordEndpointPolicy(
        rest_base_url=base,
        gateway_hosts=("127.0.0.1",),
        gateway_schemes=("ws",),
        cdn_hosts=("127.0.0.1",),
        cdn_schemes=("http",),
        cdn_allow_private_addresses=True,
    )
    transport = DiscordGatewayTransport(
        policy=policy, request_timeout=2.0, ws_open_timeout=1.0, ws_close_timeout=1.0
    )
    await transport.authenticate(SimpleNamespace(bot_token="discord-bot-token"))
    http.exchanges.clear()
    authority = CountingAuthority(
        SealedNativeMediaAuthority(tmp_path / "media.db", max_blob_bytes=1 << 20)
    )
    media = DiscordMedia(
        transport=transport,
        authority=authority,  # type: ignore[arg-type]
        scope=scope if scope is not None else _scope_facts(),
    )
    return MediaHarness(transport, media, authority, http, responses)


def _attachment_frame(url: str, *, size: int = 4, attachment_id: str = "a-9") -> object:
    return freeze_json_v1(
        {
            "op": 0,
            "s": 10,
            "t": "MESSAGE_CREATE",
            "d": {
                "id": "m-10",
                "channel_id": "c-1",
                "attachments": [
                    {
                        "id": attachment_id,
                        "filename": "a.txt",
                        "size": size,
                        "content_type": "text/plain",
                        "url": url,
                    }
                ],
            },
        }
    )


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_upload_posts_multipart_payload_json_and_files_native_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        harness = await _media_harness([json_response({"id": "m-file"})], tmp_path)
        try:
            stream = ByteStream(b"abcd")
            outcome = await harness.media.upload(
                _file_operation(), _resolution(), stream
            )
            assert outcome.state == "accepted"
            assert outcome.provider_message_id == "m-file"
            exchange = harness.http.exchanges[0]
            assert exchange.method == "POST"
            assert exchange.path == "/channels/c-1/messages?enforce_nonce=true"
            assert exchange.headers["content-type"].startswith("multipart/form-data")
            body = exchange.body.decode()
            assert 'name="payload_json"' in body
            assert 'name="files[0]"' in body
            assert "abcd" in body
            payload = json.loads(
                body.split('name="payload_json"')[1]
                .split("\r\n\r\n", 1)[1]
                .split("\r\n--", 1)[0]
            )
            assert payload["nonce"] == discord_nonce("file-1")
            assert stream.read_calls > 0
            assert stream.closed is False  # media never closes borrowed streams
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_multipart_global_429_fences_following_text_on_same_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Media and runtime text sends share the production transport's global fence."""

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        gateway = LoopbackGateway()
        ws_url = await gateway.start()
        api = discord_api_handler(ws_url)

        def handler(exchange: object) -> object:
            if getattr(exchange, "path", "").startswith("/channels/"):
                return (
                    429,
                    {"x-ratelimit-bucket": "media-bucket"},
                    json.dumps({"retry_after": 5, "global": True}).encode(),
                )
            return api(exchange)

        http = ScriptedHttpServer(handler)
        base = await http.start()
        authority = CountingAuthority(
            SealedNativeMediaAuthority(tmp_path / "shared-rate.db", max_blob_bytes=1 << 20)
        )
        transport = DiscordGatewayTransport(
            policy=DiscordEndpointPolicy(
                rest_base_url=base,
                gateway_hosts=("127.0.0.1",),
                gateway_schemes=("ws",),
            ),
            request_timeout=2.0,
        )
        runtime = DiscordGatewayRuntime(
            transport, Ledger(), Credentials(), "receiver:discord"
        )
        media = DiscordMedia(
            transport=transport,
            authority=authority,  # type: ignore[arg-type]
            scope=_scope_facts(),
        )
        try:
            started = asyncio.create_task(runtime.start(instance_ref(), SimpleNamespace()))
            connection = await gateway.wait_connection(0, 3.0)
            await connection.send({"op": 10, "d": {"heartbeat_interval": 60000}})
            await connection.send(
                {"op": 0, "s": 1, "t": "READY", "d": {"session_id": "sess-1"}}
            )
            await started

            limited = await media.upload(_file_operation(), _resolution(), ByteStream(b"abcd"))
            assert limited.state == "failed-retryable"
            assert limited.definitely_not_sent is True
            assert limited.reason_code == "discord-rate-limited"
            assert transport.rate_limits.pending_retry_ms(
                route="channels/c-1/messages", now_ms=transport.now_ms()
            ) >= 4900

            text = await runtime.submit(
                SimpleNamespace(
                    sealed_request=freeze_json_v1(
                        {
                            "provider": "discord",
                            "organization_id": "org-1",
                            "instance_id": "discord-1",
                            "generation": 3,
                            "receiver_ref": "receiver:discord",
                            "action": "create_message",
                            "channel_id": "c-2",
                            "content": "must not send",
                            "part_index": 1,
                        }
                    ),
                    kind="text-send",
                    idempotency_key="text-after-media-429",
                )
            )
            assert text.submissions[0].state == "failed-retryable"
            assert text.submissions[0].definitely_not_sent is True
            assert text.retry_after_ms is not None and text.retry_after_ms >= 4900
            channel_requests = [
                exchange for exchange in http.exchanges if exchange.path.startswith("/channels/")
            ]
            assert len(channel_requests) == 1
            assert channel_requests[0].path.startswith("/channels/c-1/messages")
        finally:
            await runtime.close(2000)
            await gateway.stop()
            await http.stop()
            authority.close()

    asyncio.run(scenario())


def test_upload_over_25_mib_rejects_before_any_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        harness = await _media_harness([], tmp_path)
        try:
            stream = EndlessStream()
            outcome = await harness.media.upload(
                _file_operation(), _resolution(), stream
            )
            assert outcome.state == "failed-final"
            assert "too-large" in (outcome.reason_code or "")
            assert stream.bytes_read <= MAX_DISCORD_UPLOAD_BYTES + CHUNK
            assert harness.http.hits == 0
        finally:
            await harness.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mutation",
    [
        {"organization_id": "org-2"},
        {"instance_id": "discord-2"},
        {"generation": 4},
        {"account_scope": "app-2"},
        {"channel_id": "c-2"},
    ],
)
def test_upload_scope_mutation_reads_zero_bytes_and_hits_no_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutation: dict
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        harness = await _media_harness([], tmp_path)
        try:
            stream = ByteStream(b"abcd")
            if "organization_id" in mutation:
                resolution = _resolution(organization_id="org-9")
            elif "account_scope" in mutation:
                resolution = _resolution(account_scope="app-2")
            elif "generation" in mutation:
                resolution = _resolution(generation=4)
            else:
                resolution = _resolution()
            with pytest.raises(DiscordMediaScopeError):
                await harness.media.upload(
                    _file_operation(**mutation), resolution, stream
                )
            assert stream.read_calls == 0
            assert harness.http.hits == 0
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_upload_unresolved_target_is_rejected_before_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        harness = await _media_harness([], tmp_path)
        try:
            stream = ByteStream(b"abcd")
            with pytest.raises(DiscordMediaScopeError):
                await harness.media.upload(
                    _file_operation(), _resolution(status="unresolved"), stream
                )
            assert stream.read_calls == 0
            assert harness.http.hits == 0
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_upload_hash_and_size_mismatch_reject_before_http(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        harness = await _media_harness([], tmp_path)
        try:
            wrong_hash = ByteStream(b"abcd", declared_sha256="1" * 64)
            outcome = await harness.media.upload(
                _file_operation(), _resolution(), wrong_hash
            )
            assert outcome.state == "failed-final"
            assert "hash" in (outcome.reason_code or "")
            wrong_size = ByteStream(b"abcd", declared_size=5)
            outcome = await harness.media.upload(
                _file_operation(), _resolution(), wrong_size
            )
            assert outcome.state == "failed-final"
            assert "size" in (outcome.reason_code or "")
            assert harness.http.hits == 0
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_fetch_opens_only_common_sealed_bytes_and_cross_scope_denies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        body = b"abcd"
        harness = await _media_harness([raw_response(body)], tmp_path)
        try:
            url = f"{harness.transport.policy.rest_base_url}/attachments/1/a.txt"
            reference = await harness.media.capture(
                _attachment_frame(url),
                attachment_id="a-9",
                target_ref_id="ref-1",
                content_digest=_digest("binding-1"),
                max_bytes=1024,
            )
            assert reference is not None
            sealed = await harness.media.fetch(reference, max_bytes=1024)
            chunks = []
            while True:
                chunk = await sealed.read(1024)
                if not chunk:
                    break
                chunks.append(chunk)
            assert b"".join(chunks) == body
            assert sealed.sha256 == hashlib.sha256(body).hexdigest()
            assert sealed.media_type == "text/plain"

            from hyprial.daemon.impl.adapters.common.transport import NativeMediaScope

            wrong = NativeMediaScope(
                owner_principal="principal-1",
                organization_id="org-2",  # cross-scope: another organization
                provider="discord",
                instance_id="discord-1",
                generation=3,
                target_ref_id="ref-1",
                content_digest=_digest("binding-1"),
            )
            cross_scope = DiscordMedia(
                transport=harness.transport,
                authority=harness.authority,  # type: ignore[arg-type]
                scope=_scope_facts(),
                scope_resolver=lambda source: wrong,
            )
            opens_before = harness.authority.open_calls
            with pytest.raises(NativeMediaDenied):
                await cross_scope.fetch(reference, max_bytes=1024)
            assert harness.authority.open_calls == opens_before  # zero sealed reads
        finally:
            await harness.stop()

    asyncio.run(scenario())


def test_capture_rejects_caller_urls_and_untrusted_hosts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        harness = await _media_harness([], tmp_path)
        production: DiscordGatewayTransport | None = None
        try:
            loopback_url = (
                f"{harness.transport.policy.rest_base_url}/attachments/1/a.txt"
            )
            with pytest.raises(TypeError):
                await harness.media.capture(
                    _attachment_frame(loopback_url),
                    attachment_id="a-9",
                    target_ref_id="ref-1",
                    content_digest=_digest("b"),
                    max_bytes=1024,
                    url=loopback_url,  # type: ignore[call-arg]
                )
            production = DiscordGatewayTransport()
            production_media = DiscordMedia(
                transport=production,
                authority=harness.authority,  # type: ignore[arg-type]
                scope=_scope_facts(),
            )
            with pytest.raises(NativeMediaDenied):
                await production_media.capture(
                    _attachment_frame(loopback_url),
                    attachment_id="a-9",
                    target_ref_id="ref-1",
                    content_digest=_digest("b"),
                    max_bytes=1024,
                )
            with pytest.raises(NativeMediaDenied):
                await production_media.capture(
                    _attachment_frame("ftp://cdn.discordapp.com/attachments/1/a.txt"),
                    attachment_id="a-9",
                    target_ref_id="ref-1",
                    content_digest=_digest("b"),
                    max_bytes=1024,
                )
            assert harness.http.hits == 0
            assert harness.authority.seal_calls == 0
        finally:
            if production is not None:
                deadline = asyncio.get_running_loop().time() + 2.0
                await production.aclose(deadline)
            await harness.stop()

    asyncio.run(scenario())


def test_capture_validates_size_redirects_and_private_addresses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    async def scenario() -> None:
        base_holder: dict[str, str] = {}

        def redirect_handler(exchange: object) -> object:
            path = getattr(exchange, "path", "")
            if path == "/attachments/1/a.txt":
                return (302, {"location": f"{base_holder['base']}/hop2"}, b"")
            if path == "/hop2":
                return raw_response(b"abcd")
            return (404, {}, b"{}")

        http = ScriptedHttpServer(redirect_handler)
        base = await http.start()
        base_holder["base"] = base
        policy = DiscordEndpointPolicy(
            rest_base_url=base,
            gateway_hosts=("127.0.0.1",),
            gateway_schemes=("ws",),
            cdn_hosts=("127.0.0.1",),
            cdn_schemes=("http",),
            cdn_allow_private_addresses=True,
        )
        transport = DiscordGatewayTransport(policy=policy, request_timeout=2.0)
        private_transport: DiscordGatewayTransport | None = None
        authority = CountingAuthority(
            SealedNativeMediaAuthority(tmp_path / "media.db", max_blob_bytes=1 << 20)
        )
        media = DiscordMedia(
            transport=transport,
            authority=authority,
            scope=_scope_facts(),  # type: ignore[arg-type]
        )
        try:
            reference = await media.capture(
                _attachment_frame(f"{base}/attachments/1/a.txt"),
                attachment_id="a-9",
                target_ref_id="ref-1",
                content_digest=_digest("b"),
                max_bytes=1024,
            )
            sealed = await media.fetch(reference, max_bytes=1024)
            assert await sealed.read(1024) == b"abcd"

            with pytest.raises(NativeMediaDenied):
                await media.capture(
                    _attachment_frame(f"{base}/attachments/1/a.txt", size=5),
                    attachment_id="a-9",
                    target_ref_id="ref-1",
                    content_digest=_digest("b"),
                    max_bytes=1024,
                )

            def evil_redirect(exchange: object) -> object:
                path = getattr(exchange, "path", "")
                if path == "/attachments/2/b.txt":
                    return (302, {"location": "http://attacker.example/steal"}, b"")
                return (404, {}, b"{}")

            evil_http = ScriptedHttpServer(evil_redirect)
            evil_base = await evil_http.start()
            try:
                with pytest.raises(NativeMediaDenied):
                    await media.capture(
                        _attachment_frame(f"{evil_base}/attachments/2/b.txt"),
                        attachment_id="a-9",
                        target_ref_id="ref-1",
                        content_digest=_digest("b"),
                        max_bytes=1024,
                    )
                assert evil_http.hits == 1
            finally:
                await evil_http.stop()

            private_policy = DiscordEndpointPolicy(
                rest_base_url=base,
                gateway_hosts=("127.0.0.1",),
                gateway_schemes=("ws",),
                cdn_hosts=("localhost",),
                cdn_schemes=("http",),
            )
            private_transport = DiscordGatewayTransport(
                policy=private_policy, request_timeout=2.0
            )
            private_media = DiscordMedia(
                transport=private_transport,
                authority=authority,  # type: ignore[arg-type]
                scope=_scope_facts(),
            )
            with pytest.raises(NativeMediaDenied):
                await private_media.capture(
                    _attachment_frame(f"{base}/attachments/1/a.txt"),
                    attachment_id="a-9",
                    target_ref_id="ref-1",
                    content_digest=_digest("b"),
                    max_bytes=1024,
                )
            assert authority.seal_calls == 1  # only the first capture sealed
        finally:
            await http.stop()
            deadline = asyncio.get_running_loop().time() + 2.0
            if private_transport is not None:
                await private_transport.aclose(deadline)
            await transport.aclose(deadline)
            authority.close()

    asyncio.run(scenario())
