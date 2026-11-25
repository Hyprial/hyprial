"""Discord Gateway runtime contract tests over a scripted transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.common.transport import (
    WebSocketCloseObservation,
    WebSocketCloseOrigin,
)
from hyprial.daemon.impl.adapters.contracts import (
    HydratedEvent,
    OnboardingAttempt,
    freeze_json_v1,
)
from hyprial.daemon.impl.adapters.discord import (
    DiscordConnectionClosed,
    DiscordCredentials,
    DiscordEventCodec,
    DiscordGatewayRuntime,
    DiscordOnboarding,
    discord_nonce,
)

from .support import (
    Credentials,
    Ledger,
    ManualClock,
    custody_for,
    durable_value,
    instance_ref,
)


class Transport:
    def __init__(self, frames: list[object]) -> None:
        self.frames = frames
        self.sent: list[object] = []
        self.rest_calls: list[tuple[str, object, str]] = []
        self.reconcile_calls: list[str] = []
        self.reconcile_response: object = None
        self.outcome: object = {"status": 200, "body": {"id": "m-out"}}
        self.gateway_url = "wss://gateway.discord.gg/?v=10&encoding=json"
        self.auth_calls = 0
        self.connected_urls: list[str] = []
        self.close_codes: list[int] = []
        self.socket_generation = 0

    async def authenticate(self, credentials: DiscordCredentials) -> object:
        self.auth_calls += 1
        return {"id": "app-1", "bot_user_id": "bot-1"}

    async def gateway(self, credentials: DiscordCredentials) -> object:
        return {
            "url": self.gateway_url,
            "session_start_limit": {"remaining": 4, "reset_after": 1000},
        }

    async def connect(self, url: str, *, max_frame_bytes: int) -> None:
        assert url.startswith("wss://") and ".discord.gg/" in url
        self.connected_urls.append(url)
        self.socket_generation += 1

    async def receive(self, *, max_frame_bytes: int) -> object:
        if self.frames and isinstance(self.frames[0], BaseException):
            raise self.frames.pop(0)
        return self.frames.pop(0)

    async def send(self, payload: object) -> None:
        self.sent.append(payload)

    async def request(
        self, action: str, payload: object, idempotency_key: str
    ) -> object:
        self.rest_calls.append((action, payload, idempotency_key))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def reconcile(
        self, action: str, payload: object, idempotency_key: str
    ) -> object:
        self.reconcile_calls.append(idempotency_key)
        return self.reconcile_response

    async def close(self, code: int) -> None:
        self.close_codes.append(code)

    @property
    def op1_frames(self) -> list[object]:
        return [
            frame
            for frame in self.sent
            if isinstance(frame, dict) and frame.get("op") == 1
        ]


def _message_frame(sequence: int, native: str = "m-1") -> dict:
    return {
        "op": 0,
        "s": sequence,
        "t": "MESSAGE_CREATE",
        "d": {
            "id": native,
            "channel_id": "c-1",
            "guild_id": None,
            "author": {"id": "u-1", "bot": False},
            "content": "hello",
            "attachments": [
                {
                    "id": "a-1",
                    "filename": "a.txt",
                    "size": 3,
                    "content_type": "text/plain",
                }
            ],
            "message_reference": {"message_id": "m-0"},
        },
    }


def _hello(interval: int = 45000) -> dict:
    return {"op": 10, "d": {"heartbeat_interval": interval}}


def _ready(session: str = "sess-1", sequence: int = 5) -> dict:
    return {
        "op": 0,
        "s": sequence,
        "t": "READY",
        "d": {"session_id": session, "resume_gateway_url": "wss://gateway.discord.gg"},
    }


def _resumed(sequence: int = 7) -> dict:
    return {"op": 0, "s": sequence, "t": "RESUMED", "d": {}}


def _operation(**overrides: object) -> SimpleNamespace:
    request = {
        "provider": "discord",
        "organization_id": "org-1",
        "instance_id": "discord-1",
        "generation": 3,
        "receiver_ref": "receiver:discord",
        "action": "create_message",
        "channel_id": "c-1",
        "content": "hello",
        "part_index": 0,
        **overrides,
    }
    return SimpleNamespace(
        sealed_request=freeze_json_v1(request), kind="text-send", idempotency_key="dc-1"
    )


class MutableClock:
    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


async def _wait_until(predicate: object, *, timeout: float = 0.5) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():  # type: ignore[operator]
            await asyncio.sleep(0.001)


def test_gateway_identify_sequence_checkpoint_and_resume_are_custody_fenced() -> None:
    async def scenario() -> None:
        transport = Transport([_hello(), _ready(), _message_frame(6)])
        runtime = DiscordGatewayRuntime(
            transport, Ledger(), Credentials(), "receiver:discord"
        )
        await runtime.start(instance_ref(), SimpleNamespace())
        assert any(payload["op"] == 2 for payload in transport.sent)  # type: ignore[index]
        unit = await runtime.next_ingress()
        assert unit.checkpoint.token == "6"
        message_alias = next(
            alias
            for alias in unit.envelope.observation.aliases
            if alias.alias_kind == "message"
        )
        assert (
            message_alias.platform,
            message_alias.account_scope,
            message_alias.chat_scope,
            message_alias.normalized_id,
        ) == ("discord", "app-1", "c-1", "m-1")
        await runtime.commit_checkpoint(
            unit.checkpoint, custody_for(unit.envelope.payload)
        )
        normalized, platform = DiscordEventCodec(
            instance_ref(), "app-1", "bot-1"
        ).decode(HydratedEvent(unit.envelope, unit.envelope.payload))
        assert platform.gateway_seq == 6
        assert platform.attachment_ids == ("a-1",)
        assert normalized.reply_to is not None
        assert normalized.attachments[0].handle_id.endswith(":a-1")
        assert normalized.context.channel_id.startswith("adapter:discord:")
        transport.frames.extend([_hello(), _resumed(7)])
        await runtime.reconnect("test")
        resume = next(payload for payload in transport.sent if payload["op"] == 6)  # type: ignore[index]
        assert resume["d"]["seq"] == 6
        assert transport.auth_calls == 2
        outcome = await runtime.close(1000)
        assert outcome.state == "closed"
        assert transport.close_codes == [4000, 1000]

    asyncio.run(scenario())


def test_fresh_runtime_resumes_durable_session_and_heartbeat_is_autonomous() -> None:
    async def scenario() -> None:
        ledger = Ledger()
        clock = ManualClock()
        first = DiscordGatewayRuntime(
            Transport([_hello(10), _ready("sess-durable"), _message_frame(6, "m-6")]),
            ledger,
            Credentials(),
            "receiver:discord",
            heartbeat_sleep=clock.sleep,
            heartbeat_jitter=lambda low, high: 0.0,
        )
        await first.start(instance_ref(), SimpleNamespace())
        unit = await first.next_ingress()
        await first.commit_checkpoint(
            unit.checkpoint, custody_for(unit.envelope.payload)
        )
        assert ledger.value is not None and "sess-durable" in ledger.value
        clock.advance()  # jitter elapses: the beat is autonomous of custody
        await _wait_until(lambda: {"op": 1, "d": 6} in first._transport.sent)  # type: ignore[operator]
        assert (await first.health()).state == "degraded"
        crashed_state = ledger.value  # durable tuple as of the last custody commit
        await first.close(1000)
        beats = len(first._transport.op1_frames)  # type: ignore[attr-defined]
        clock.advance()  # a pending interval tick must not beat after final close
        await asyncio.sleep(0.01)
        assert len(first._transport.op1_frames) == beats  # type: ignore[attr-defined]

        ledger.value = crashed_state  # restart from the last durable commit (crash, not close)
        second = DiscordGatewayRuntime(
            Transport([_hello(), _resumed(7)]),
            ledger,
            Credentials(),
            "receiver:discord",
        )
        await second.start(instance_ref(), SimpleNamespace())
        resume = next(frame for frame in second._transport.sent if frame["op"] == 6)  # type: ignore[index]
        assert resume["d"]["session_id"] == "sess-durable"  # type: ignore[index]
        assert second._transport.connected_urls == [
            "wss://gateway.discord.gg/?v=10&encoding=json"
        ]  # type: ignore[attr-defined]
        await second.close(1000)

    asyncio.run(scenario())


def test_heartbeat_tracks_latest_received_sequence_before_custody() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        transport = Transport(
            [
                _hello(20),
                _ready("sess-live", 5),
                _message_frame(6, "m-6"),
                {"op": 11, "d": None},
                _message_frame(7, "m-7"),
            ]
        )
        runtime = DiscordGatewayRuntime(
            transport,
            Ledger(),
            Credentials(),
            "receiver:discord",
            heartbeat_sleep=clock.sleep,
            heartbeat_jitter=lambda low, high: 0.0,
        )
        await runtime.start(instance_ref(), SimpleNamespace())
        assert (await runtime.next_ingress()).checkpoint.token == "6"
        clock.advance()
        await _wait_until(lambda: {"op": 1, "d": 6} in transport.sent)  # type: ignore[operator]
        assert (await runtime.next_ingress()).checkpoint.token == "7"
        clock.advance()
        await _wait_until(lambda: {"op": 1, "d": 7} in transport.sent)  # type: ignore[operator]
        await runtime.close(1000)

    asyncio.run(scenario())


def test_handshake_heartbeat_without_ack_closes_4000_once_and_resumes() -> None:
    async def scenario() -> None:
        clock = ManualClock()
        ledger = Ledger(durable_value())
        transport = Transport(
            [
                _hello(45000),
                {"op": 1, "d": None},
                _resumed(7),
                _hello(45000),
                _resumed(7),
            ]
        )
        runtime = DiscordGatewayRuntime(
            transport,
            ledger,
            Credentials(),
            "receiver:discord",
            heartbeat_sleep=clock.sleep,
            heartbeat_jitter=lambda low, high: 0.0,
        )
        await runtime.start(instance_ref(), SimpleNamespace())
        assert transport.sent.count({"op": 1, "d": 6}) == 1  # type: ignore[union-attr]

        clock.advance()  # jitter elapses; the immediate heartbeat was never acked
        await _wait_until(lambda: transport.auth_calls == 2)
        await asyncio.sleep(0)
        assert transport.close_codes == [4000]
        assert len(transport.connected_urls) == 2
        assert transport.auth_calls == 2
        assert sum(frame.get("op") == 6 for frame in transport.sent) == 2  # type: ignore[union-attr]
        assert (await runtime.health()).state == "online"
        await runtime.close(1000)

    asyncio.run(scenario())


def test_resumable_invalid_session_true_resumes_and_false_invalidates_to_identify() -> (
    None
):
    async def scenario() -> None:
        ledger = Ledger(durable_value())
        transport = Transport(
            [
                _hello(),
                _resumed(7),
                {"op": 9, "d": True},
                _hello(),
                _resumed(7),
                {"op": 9, "d": False},
                _hello(),
                _ready("sess-new", 8),
                _message_frame(9, "m-9"),
            ]
        )
        runtime = DiscordGatewayRuntime(
            transport, ledger, Credentials(), "receiver:discord"
        )
        await runtime.start(instance_ref(), SimpleNamespace())
        assert (await runtime.next_ingress()).checkpoint.token == "9"
        await runtime.close(1000)
        tombstone = json.loads(ledger.value or "{}")
        assert tombstone["session_id"] is None
        assert transport.close_codes == [4000, 4000, 1000]
        resumes = [frame for frame in transport.sent if frame["op"] == 6]  # type: ignore[index]
        assert len(resumes) == 2
        assert all(frame["d"]["seq"] == 6 for frame in resumes)  # type: ignore[index]
        assert any(frame["op"] == 2 for frame in transport.sent)  # type: ignore[index]

    asyncio.run(scenario())


def test_op7_reconnect_requests_resume() -> None:
    async def scenario() -> None:
        ledger = Ledger(durable_value())
        transport = Transport(
            [
                _hello(),
                _resumed(7),
                {"op": 7, "d": None},
                _hello(),
                _resumed(7),
                _message_frame(9),
            ]
        )
        runtime = DiscordGatewayRuntime(
            transport, ledger, Credentials(), "receiver:discord"
        )
        await runtime.start(instance_ref(), SimpleNamespace())
        assert (await runtime.next_ingress()).checkpoint.token == "9"
        await runtime.close(1000)
        assert transport.close_codes == [4000, 1000]
        assert any(frame["op"] == 6 for frame in transport.sent)  # type: ignore[index]

    asyncio.run(scenario())


def test_final_close_tombstones_durable_session_and_is_idempotent() -> None:
    async def scenario() -> None:
        ledger = Ledger(durable_value())
        transport = Transport([_hello(), _resumed(7)])
        runtime = DiscordGatewayRuntime(
            transport, ledger, Credentials(), "receiver:discord"
        )
        await runtime.start(instance_ref(), SimpleNamespace())
        outcome = await runtime.close(1000)
        assert outcome.state == "closed"
        assert transport.close_codes == [1000]
        assert json.loads(ledger.value or "{}")["session_id"] is None
        again = await runtime.close(1000)
        assert again.state == "closed"
        assert transport.close_codes == [1000]  # final close happens exactly once
        assert transport.connected_urls == [
            "wss://resume.gateway.discord.gg/?v=10&encoding=json"
        ]  # final close never reconnects

    asyncio.run(scenario())


def test_stale_generation_close_from_old_socket_cannot_reconnect() -> None:
    async def scenario() -> None:
        ledger = Ledger(durable_value())
        stale = DiscordConnectionClosed(
            WebSocketCloseObservation(
                generation=99,
                origin=WebSocketCloseOrigin.REMOTE,
                clean=False,
                rcvd_code=4000,
                rcvd_reason=None,
                sent_code=None,
                sent_reason=None,
                rcvd_then_sent=None,
                local_code=None,
                local_reason=None,
                opening_error=None,
            )
        )
        transport = Transport([_hello(), _resumed(7), stale, _message_frame(9)])
        runtime = DiscordGatewayRuntime(
            transport, ledger, Credentials(), "receiver:discord"
        )
        await runtime.start(instance_ref(), SimpleNamespace())
        assert transport.socket_generation == 1  # observation generation 99 is stale
        assert (await runtime.next_ingress()).checkpoint.token == "9"
        await runtime.close(1000)
        assert transport.close_codes == [1000]  # stale close never reconnects
        assert len(transport.connected_urls) == 1

    asyncio.run(scenario())


def test_discord_onboarding_requires_intents_and_reports_control_gap() -> None:
    async def scenario() -> None:
        onboarding = DiscordOnboarding(
            Transport([]), Credentials(), "org-1", "discord-1", "main"
        )
        assert (
            await onboarding.inspect({"credential_ref": "receiver:discord"})
        ).state == "blocked"
        identity = await onboarding.authenticate(
            OnboardingAttempt("a-1", {}, "receiver:discord")
        )
        capabilities = await onboarding.probe_capabilities(identity)
        facts = {fact.name: fact.support for fact in capabilities.facts}
        assert facts["reactions"] == "unsupported"
        assert facts["threads"] == "unsupported"
        assert facts["controls"] == "unsupported"

    asyncio.run(scenario())


def test_rate_limit_unknown_and_scope_mutations_never_fake_success() -> None:
    async def scenario() -> None:
        transport = Transport([_hello(), _ready()])
        runtime = DiscordGatewayRuntime(
            transport, Ledger("0"), Credentials(), "receiver:discord"
        )
        await runtime.start(instance_ref(), SimpleNamespace())
        transport.outcome = {
            "status": 429,
            "body": {"retry_after": "2.5", "global": False},
        }
        limited = await runtime.submit(_operation())
        assert limited.submissions[0].state == "failed-retryable"
        assert limited.retry_after_ms == 2500

        transport.outcome = {"status": 200, "body": {"id": "m-2"}}
        tracked = await runtime.submit(
            _operation()
        )  # same bucket: blocked without HTTP
        assert tracked.submissions[0].state == "failed-retryable"
        assert tracked.retry_after_ms is not None and tracked.retry_after_ms > 2000
        assert len(transport.rest_calls) == 1

        other_channel = await runtime.submit(_operation(channel_id="c-2"))
        assert other_channel.submissions[0].state == "accepted"
        assert len(transport.rest_calls) == 2

        transport.outcome = TimeoutError("after write")
        unknown = await runtime.submit(_operation(channel_id="c-3"))
        assert unknown.submissions[0].state == "unknown"
        with pytest.raises(ValueError, match="organization"):
            await runtime.submit(_operation(organization_id="org-2"))
        assert len(transport.rest_calls) == 3
        await runtime.close(1000)

    asyncio.run(scenario())


def test_runtime_rejects_raw_handle_file_upload_without_transport_call() -> None:
    async def scenario() -> None:
        transport = Transport([_hello(), _ready()])
        runtime = DiscordGatewayRuntime(
            transport, Ledger("0"), Credentials(), "receiver:discord"
        )
        await runtime.start(instance_ref(), SimpleNamespace())
        outcome = await runtime.submit(
            _operation(action="create_file_message", handle_id="handle-1")
        )
        assert outcome.submissions[0].state == "failed-final"
        assert "media" in (outcome.submissions[0].reason_code or "")
        assert transport.rest_calls == []
        await runtime.close(1000)

    asyncio.run(scenario())


def test_reconcile_exact_request_only_inside_retained_nonce_window() -> None:
    async def scenario() -> None:
        clock = MutableClock()
        transport = Transport([_hello(), _ready()])
        runtime = DiscordGatewayRuntime(
            transport, Ledger("0"), Credentials(), "receiver:discord", now_ms=clock
        )
        await runtime.start(instance_ref(), SimpleNamespace())
        transport.outcome = TimeoutError("lost")
        unknown = await runtime.submit(_operation())
        assert unknown.submissions[0].state == "unknown"

        transport.reconcile_response = {"id": "m-late"}
        reconciled = await runtime.reconcile(_operation(), unknown)
        assert reconciled.submissions[0].state == "accepted"
        assert reconciled.submissions[0].provider_message_id == "m-late"
        assert transport.reconcile_calls == ["dc-1"]

        clock.now += 900_001
        stale = await runtime.reconcile(_operation(), unknown)
        assert stale.submissions[0].state == "unknown"
        assert len(transport.reconcile_calls) == 1  # outside the window: no lookup
        await runtime.close(1000)

    asyncio.run(scenario())


def test_nonce_matches_transport_digest_convention() -> None:
    key = "dc-1"
    digest = hashlib.sha256(key.encode()).hexdigest()[:25]
    assert discord_nonce(key) == digest
    assert len(digest) <= 25
