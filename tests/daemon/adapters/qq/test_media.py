"""Chunked media wire: phase-by-phase effects, restart reuse, scope binding."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.common.transport import NativeMediaScope
from hyprial.daemon.impl.adapters.contracts import (
    AudienceSnapshot,
    ChatRef,
    NativeTargetResolution,
    PrincipalRef,
)

from .support import (
    APP_ID,
    BOT_ID,
    LocalGateway,
    LocalHTTP,
    MemoryLedger,
    StaticCredentials,
    WallClockMs,
    build_components,
    default_hello,
    json_response,
    media_operation,
    qq_instance,
    sealed_authority,
)

BODY = bytes(range(256)) * 2  # 512 bytes


class MediaScript:
    """Scriptable REST responder for the pinned chunked-upload wire."""

    def __init__(self, http: LocalHTTP, *, block_size: int = 200) -> None:
        self.http = http
        self.block_size = block_size
        self.prepare_status: tuple[int, object] | None = None
        self.put_status: tuple[int, object] | None = None
        self.finish_status: tuple[int, object] | None = None
        self.complete_status: tuple[int, object] | None = None
        self.message_status: tuple[int, object] = (200, {"id": "qq-out-1"})
        self.stop_after: str | None = None
        self.closed_servers: list[asyncio.AbstractServer] = []
        self.prepare_calls = 0
        self.put_bodies: list[bytes] = []
        self.finish_calls = 0
        self.complete_calls = 0
        self.message_calls = 0

    async def _response(
        self, phase: str, status: int, body: object
    ) -> tuple[int, dict[str, str], bytes]:
        response = json_response(status, body)
        if self.stop_after == phase:
            server = self.http._server
            assert server is not None
            server.close()
            self.http._server = None
            self.closed_servers.append(server)
        return response

    async def respond(self, request: object) -> tuple[int, dict[str, str], bytes]:
        path = request.path  # type: ignore[attr-defined]
        method = request.method  # type: ignore[attr-defined]
        if path == "/app/getAppAccessToken":
            return json_response(200, {"access_token": "tok-1", "expires_in": 7200})
        if path == "/users/@me":
            return json_response(200, {"id": BOT_ID, "username": "hyprial", "bot": True})
        if path.endswith("/upload_prepare"):
            self.prepare_calls += 1
            if self.prepare_status is not None:
                return await self._response("prepare", *self.prepare_status)
            part_count = (len(BODY) + self.block_size - 1) // self.block_size
            parts = [
                {"index": index + 1, "presigned_url": f"{self.http.base_url}/cos-part/{index + 1}"}
                for index in range(part_count)
            ]
            return await self._response(
                "prepare",
                200,
                {"upload_id": "u-1", "block_size": self.block_size, "parts": parts},
            )
        if path.startswith("/cos-part/") and method == "PUT":
            self.put_bodies.append(request.body)  # type: ignore[attr-defined]
            if self.put_status is not None:
                return await self._response("put", *self.put_status)
            return await self._response("put", 200, {})
        if path.endswith("/upload_part_finish"):
            self.finish_calls += 1
            if self.finish_status is not None:
                return await self._response("finish", *self.finish_status)
            return await self._response("finish", 200, {})
        if method == "POST" and path.endswith("/files"):
            self.complete_calls += 1
            if self.complete_status is not None:
                return await self._response("complete", *self.complete_status)
            return await self._response(
                "complete",
                200, {"file_info": "fid-1", "file_uuid": "uuid-1", "ttl": 300}
            )
        if path.endswith("/messages"):
            self.message_calls += 1
            return await self._response("message", *self.message_status)
        return json_response(404, {"message": f"unexpected {method} {path}"})


class MediaEnv:
    def __init__(self, tmp_path: Path, *, block_size: int = 200, rest_timeout: float = 5.0) -> None:
        self.http = LocalHTTP()
        self.gateway = LocalGateway()
        self.authority = sealed_authority(tmp_path)
        self.script = MediaScript(self.http, block_size=block_size)
        self.clock = WallClockMs()
        self.rest_timeout = rest_timeout
        self.tmp_path = tmp_path
        self.runtime = None
        self.components = None
        self.gateway_url = ""

    async def __aenter__(self) -> "MediaEnv":
        self.http.responder = self.script.respond
        self.gateway.handler = default_hello()
        await self.http.start()
        self.gateway_url = await self.gateway.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.runtime is not None:
            try:
                await asyncio.wait_for(self.runtime.close(2000), timeout=5)
            except Exception:
                pass
            self.runtime = None
        if self.components is not None:
            try:
                await self.components.token.stop()
            except Exception:
                pass
            self.components.store.close()
            self.components = None
        await self.gateway.stop()
        if self.http._server is not None:
            await self.http.stop()
        for server in self.script.closed_servers:
            await server.wait_closed()
        self.authority.close()

    def seal(self, *, instance_id: str = "qq-1", generation: int = 2, target: str = "group-1"):
        scope = NativeMediaScope(
            owner_principal="principal-1",
            organization_id="org-1",
            provider="qq",
            instance_id=instance_id,
            generation=generation,
            target_ref_id=target,
            content_digest=hashlib.sha256(b"").hexdigest(),
        )
        ref = self.authority.seal(
            scope=scope,
            body=BODY,
            name="payload.bin",
            media_type="application/octet-stream",
            native_metadata={},
        )
        media_scope = {
            "owner_principal": scope.owner_principal,
            "organization_id": scope.organization_id,
            "provider": scope.provider,
            "instance_id": scope.instance_id,
            "generation": scope.generation,
            "target_ref_id": scope.target_ref_id,
            "content_digest": scope.content_digest,
        }
        media_ref = {
            "instance_id": ref.instance_id,
            "generation": ref.generation,
            "opaque_ref_digest": ref.opaque_ref_digest,
            "authority_handle_id": ref.authority_handle_id,
        }
        return media_ref, media_scope

    async def build(self):
        from hyprial.daemon.impl.adapters.qq.provider import QQOfficialBotRuntime

        self.components = build_components(
            http=self.http,
            gateway_url=self.gateway_url,
            store_path=self.tmp_path / "store.sqlite",
            authority=self.authority,
            now_ms=self.clock,
            rest_timeout=self.rest_timeout,
        )
        self.runtime = QQOfficialBotRuntime(
            self.components, MemoryLedger(), StaticCredentials(), "receiver:qq"
        )
        await self.runtime.start(qq_instance(), SimpleNamespace())
        self.components.store.record_inbound(
            instance_id="qq-1", generation=2, msg_id="message-1", received_at_ms=1_000_000
        )
        return self.runtime

    async def rebuild(self):
        if self.runtime is not None:
            await self.runtime.close(2000)
            self.runtime = None
        if self.components is not None:
            await self.components.token.stop()
            self.components.store.close()
            self.components = None
        return await self.build()

    def operation(self, **overrides: object) -> SimpleNamespace:
        media_ref, media_scope = self.seal()
        return media_operation(media_ref=media_ref, scope=media_scope, **overrides)


def _outcome_states(outcome) -> list[str]:
    return [part.state for part in outcome.submissions]


def test_full_chunked_wire_matches_official_phases(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path, block_size=200) as env:
            runtime = await env.build()
            outcome = await runtime.submit(env.operation())
            assert _outcome_states(outcome) == ["accepted"]
            assert outcome.submissions[0].provider_message_id == "qq-out-1"
            prepare = [
                request
                for request in env.http.requests
                if request.path.endswith("/upload_prepare")
            ][0]
            body = prepare.json()
            assert body["file_name"] == "payload.bin"
            assert body["file_type"] == 4
            assert body["file_size"] == len(BODY)
            assert body["md5"] == hashlib.md5(BODY).hexdigest()
            assert body["sha1"] == hashlib.sha1(BODY).hexdigest()
            assert body["md5_10m"] == body["md5"]
            assert env.script.put_bodies == [
                BODY[index : index + 200] for index in range(0, len(BODY), 200)
            ]
            put_requests = [
                request for request in env.http.requests if request.method == "PUT"
            ]
            assert all(request.header("authorization") is None for request in put_requests)
            finishes = [
                request
                for request in env.http.requests
                if request.path.endswith("/upload_part_finish")
            ]
            for index, request in enumerate(finishes):
                payload = request.json()
                assert payload["upload_id"] == "u-1"
                assert payload["part_index"] == index + 1
                assert payload["block_size"] == len(BODY[index * 200 : (index + 1) * 200])
                assert payload["md5"] == hashlib.md5(BODY[index * 200 : (index + 1) * 200]).hexdigest()
            completion = [
                request for request in env.http.requests if request.path.endswith("/files")
            ][0]
            assert completion.json() == {"upload_id": "u-1"}
            message = [
                request for request in env.http.requests if request.path.endswith("/messages")
            ][0]
            payload = message.json()
            assert payload["msg_type"] == 7
            assert payload["media"] == {"file_info": "fid-1"}
            assert payload["msg_id"] == "message-1"

    asyncio.run(scenario())


def test_prepare_5xx_is_unknown_and_stops_pipeline(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path) as env:
            runtime = await env.build()
            env.script.prepare_status = (500, {"code": 500})
            outcome = await runtime.submit(env.operation())
            assert _outcome_states(outcome) == ["unknown"]
            assert env.script.put_bodies == []
            assert env.script.finish_calls == 0
            assert env.script.complete_calls == 0
            assert env.script.message_calls == 0

    asyncio.run(scenario())


def test_prepare_malformed_response_is_unknown(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path) as env:
            runtime = await env.build()
            env.script.prepare_status = (200, {"upload_id": "u-1"})
            outcome = await runtime.submit(env.operation())
            assert _outcome_states(outcome) == ["unknown"]
            assert env.script.put_bodies == []
            assert env.script.complete_calls == 0

    asyncio.run(scenario())


def test_media_prepare_connection_refusal_is_retryable_without_follow_on_effects(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path, block_size=len(BODY)) as env:
            runtime = await env.build()
            await env.http.stop()
            outcome = await runtime.submit(env.operation())
            part = outcome.submissions[0]
            assert part.state == "failed-retryable"
            assert part.definitely_not_sent is True
            assert part.reason_code == "qq-pre-send-failure"
            assert env.script.prepare_calls == 0
            assert env.script.put_bodies == []
            assert env.script.finish_calls == 0
            assert env.script.complete_calls == 0
            assert env.script.message_calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("stop_after", "expected_counts"),
    [
        ("prepare", (1, 0, 0, 0, 0)),
        ("put", (1, 1, 0, 0, 0)),
        ("finish", (1, 1, 1, 0, 0)),
        ("complete", (1, 1, 1, 1, 0)),
    ],
)
def test_each_chunked_media_follow_on_refusal_stays_pre_send_without_retry(
    tmp_path: Path,
    stop_after: str,
    expected_counts: tuple[int, int, int, int, int],
) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path, block_size=len(BODY)) as env:
            runtime = await env.build()
            env.script.stop_after = stop_after
            outcome = await runtime.submit(env.operation())
            part = outcome.submissions[0]
            assert part.state == "failed-retryable"
            assert part.definitely_not_sent is True
            assert part.reason_code == "qq-pre-send-failure"
            assert (
                env.script.prepare_calls,
                len(env.script.put_bodies),
                env.script.finish_calls,
                env.script.complete_calls,
                env.script.message_calls,
            ) == expected_counts

    asyncio.run(scenario())


def test_part_put_timeout_is_unknown_and_skips_finish(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path, rest_timeout=0.3) as env:
            runtime = await env.build()

            async def hang(request: object) -> tuple[int, dict[str, str], bytes]:
                if request.path.startswith("/cos-part/"):  # type: ignore[attr-defined]
                    await asyncio.sleep(3600)
                return await env.script.respond(request)

            env.http.responder = hang
            outcome = await runtime.submit(env.operation())
            assert _outcome_states(outcome) == ["unknown"]
            assert outcome.submissions[0].definitely_not_sent is False
            assert env.script.finish_calls == 0
            assert env.script.complete_calls == 0
            assert env.script.message_calls == 0

    asyncio.run(scenario())


def test_part_finish_5xx_is_unknown_and_stops_completion(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path) as env:
            runtime = await env.build()
            env.script.finish_status = (500, {"code": 500})
            outcome = await runtime.submit(env.operation())
            assert _outcome_states(outcome) == ["unknown"]
            assert len(env.script.put_bodies) == 1
            assert env.script.complete_calls == 0
            assert env.script.message_calls == 0

    asyncio.run(scenario())


def test_completion_5xx_unknown_then_retry_reuses_upload_id_across_restart(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path) as env:
            runtime = await env.build()
            env.script.complete_status = (500, {"code": 500})
            operation = env.operation(operation_id="qq-media-op-r")
            outcome = await runtime.submit(operation)
            assert _outcome_states(outcome) == ["unknown"]
            assert env.script.prepare_calls == 1
            env.script.complete_status = None
            runtime = await env.rebuild()
            retried = await runtime.submit(operation)
            assert _outcome_states(retried) == ["accepted"]
            assert env.script.prepare_calls == 1, "upload_id persisted: no re-prepare"
            assert env.script.complete_calls == 2, "completion re-runs with same upload"
            assert env.script.message_calls == 1

    asyncio.run(scenario())


def test_completion_result_reuse_skips_upload_on_retry(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path) as env:
            runtime = await env.build()
            operation = env.operation(operation_id="qq-media-op-d")
            assert _outcome_states(await runtime.submit(operation)) == ["accepted"]
            env.script.message_status = (200, {"id": "qq-out-2"})
            again = await runtime.submit(operation)
            assert again.submissions[0].provider_message_id == "qq-out-2"
            assert env.script.prepare_calls == 1
            assert env.script.complete_calls == 1
            assert env.script.message_calls == 2

    asyncio.run(scenario())


def test_cross_scope_media_is_denied_with_zero_effect(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path) as env:
            runtime = await env.build()
            media_ref, media_scope = env.seal(instance_id="qq-9")
            outcome = await runtime.submit(
                media_operation(media_ref=media_ref, scope=media_scope)
            )
            part = outcome.submissions[0]
            assert part.state == "failed-final"
            assert part.reason_code == "qq-media-scope-denied"
            assert env.script.prepare_calls == 0
            assert env.script.message_calls == 0
            media_ref2, media_scope2 = env.seal()
            wrong = dict(media_scope2)
            wrong["target_ref_id"] = "group-2"
            denied = await runtime.submit(
                media_operation(media_ref=media_ref2, scope=wrong)
            )
            assert denied.submissions[0].reason_code == "qq-media-scope-denied"
            assert env.script.prepare_calls == 0

    asyncio.run(scenario())


def test_missing_msg_id_media_rejected_before_authority_or_http(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path) as env:
            runtime = await env.build()
            outcome = await runtime.submit(env.operation(msg_id=None))
            assert outcome.submissions[0].reason_code == "qq-msg-id-required"
            assert env.script.prepare_calls == 0
            assert env.script.message_calls == 0

    asyncio.run(scenario())


def test_caller_urls_and_paths_are_rejected_before_http(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path) as env:
            runtime = await env.build()
            for override in ({"url": "https://evil.example/x"}, {"path": "/etc/passwd"}):
                outcome = await runtime.submit(env.operation(**override))
                part = outcome.submissions[0]
                assert part.state == "failed-final"
                assert part.reason_code == "qq-media-caller-url-rejected"
                assert env.script.prepare_calls == 0

    asyncio.run(scenario())


def test_private_presigned_urls_are_rejected_before_put(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path) as env:
            runtime = await env.build()

            async def respond(request: object) -> tuple[int, dict[str, str], bytes]:
                if request.path.endswith("/upload_prepare"):  # type: ignore[attr-defined]
                    return json_response(
                        200,
                        {
                            "upload_id": "u-1",
                            "block_size": 8,
                            "parts": [
                                {"index": 1, "presigned_url": "http://10.0.0.5/part"}
                            ],
                        },
                    )
                return await env.script.respond(request)

            env.http.responder = respond
            outcome = await runtime.submit(env.operation())
            part = outcome.submissions[0]
            assert part.state == "failed-final"
            assert part.reason_code == "qq-media-presigned-url-denied"
            assert env.script.put_bodies == []
            assert env.script.finish_calls == 0

    asyncio.run(scenario())


def test_adapter_media_seam_rejects_misrouted_stream_handles(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with MediaEnv(tmp_path) as env:
            from hyprial.daemon.impl.adapters.qq.media import QQMedia

            await env.build()
            media_ref, media_scope = env.seal()
            media = QQMedia(
                rest=env.components.rest,
                authority=env.authority,
                store=env.components.store,
                instance=qq_instance(),
                app_id=APP_ID,
            )
            instance = qq_instance()
            chat = ChatRef(
                "chat-ref:v1:qq:qq-1:group:group-1", instance, APP_ID, "group-1", "group", None, None
            )
            sender = PrincipalRef(
                "user-1", None, None, None, None, "unresolved", None, None, None,
                "qq-official-gateway",
            )
            audience = AudienceSnapshot("group", (sender,), "partial", None, 1)

            class _Stream:
                name = "other.bin"
                media_type = "application/octet-stream"
                size = 3
                sha256 = "f" * 64

                async def read(self, max_bytes: int) -> bytes:
                    return b"abc"

                async def aclose(self) -> None:
                    return None

            from hyprial.daemon.impl.adapters.contracts import AuthorizedChatTarget

            target = NativeTargetResolution(
                AuthorizedChatTarget("t-1", chat, audience, "reply", "message-1"),
                "resolved",
                "group-1",
                None,
                None,
            )
            operation = media_operation(media_ref=media_ref, scope=media_scope)
            part = await asyncio.wait_for(
                media.upload(operation, target, _Stream()), timeout=5
            )
            assert part.state == "failed-final"
            assert part.reason_code == "qq-media-handle-mismatch"
            assert env.script.prepare_calls == 0

    asyncio.run(scenario())
