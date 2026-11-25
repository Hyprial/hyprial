from __future__ import annotations

import asyncio
import hashlib
import time
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.contracts import (
    AdapterInstanceRef,
    CustodyReceipt,
    HydratedEvent,
    NativeCheckpoint,
    OnboardingAttempt,
    frozen_json_digest_v1,
    freeze_json_v1,
)
from hyprial.daemon.impl.adapters.slack import (
    SlackHttpResult,
    SlackCredentials,
    SlackEventCodec,
    SlackOnboarding,
    SlackSocketRuntime,
)
from hyprial.daemon.impl.adapters.common.transport import (
    HttpEffectObservation,
    HttpEffectState,
    HttpRequestPhase,
    ResourceCloseOutcome,
    ResourceCloseState,
)


class _Ledger:
    def __init__(self) -> None:
        self.value: str | None = None

    async def load(self, instance_id: str, generation: int) -> str | None:
        return self.value

    async def commit(self, instance_id: str, generation: int, token: str) -> None:
        self.value = token


class _Credentials:
    async def resolve(self, credential_ref: str) -> SlackCredentials:
        assert credential_ref == "receiver:slack"
        return SlackCredentials("xoxb-" + "b" * 20, "xapp-" + "a" * 20)


class _Transport:
    def __init__(self, frames: list[object]) -> None:
        self.frames = frames
        self.acks: list[str] = []
        self.calls: list[tuple[str, object, str]] = []
        self.connected_url: str | None = None
        self.auth_calls = 0
        self.close_socket_calls: list[str] = []
        self.aclose_calls = 0
        self.outcome: object = {"ok": True, "ts": "172.2"}

    async def authenticate(self, credentials: SlackCredentials) -> object:
        self.auth_calls += 1
        return {"ok": True, "team_id": "T1", "user_id": "B1", "bot_id": "BOT1"}

    async def open_socket(self, credentials: SlackCredentials) -> str:
        return "wss://wss-primary.slack.com/link/?ticket=opaque"

    async def connect(self, url: str, *, max_frame_bytes: int) -> None:
        self.connected_url = url
        assert max_frame_bytes == 1_048_576

    async def receive(self, *, max_frame_bytes: int) -> object:
        return self.frames.pop(0)

    async def acknowledge(self, envelope_id: str) -> None:
        self.acks.append(envelope_id)

    async def call(self, method: str, payload: object, idempotency_key: str) -> object:
        self.calls.append((method, payload, idempotency_key))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def reconcile(
        self, method: str, payload: object, idempotency_key: str
    ) -> object:
        return None

    async def close_socket(
        self, reason: str, timeout_at: float
    ) -> ResourceCloseOutcome:
        self.close_socket_calls.append(reason)
        return ResourceCloseOutcome(ResourceCloseState.CLOSED, 1, True, False, None)

    async def aclose(self, timeout_at: float) -> ResourceCloseOutcome:
        self.aclose_calls += 1
        return ResourceCloseOutcome(ResourceCloseState.CLOSED, 1, True, True, None)


def _instance() -> AdapterInstanceRef:
    fingerprint = hashlib.sha256(b"slack\0slack-cloud\0T1\0BOT1").hexdigest()
    return AdapterInstanceRef(
        "slack-1", "slack", "socket-mode", "main", "org-1", fingerprint, 2
    )


def _operation(**overrides: object) -> SimpleNamespace:
    request = {
        "provider": "slack",
        "organization_id": "org-1",
        "instance_id": "slack-1",
        "generation": 2,
        "receiver_ref": "receiver:slack",
        "method": "chat.postMessage",
        "channel": "D1",
        "text": "hello",
        "part_index": 0,
        **overrides,
    }
    return SimpleNamespace(
        sealed_request=freeze_json_v1(request),
        kind="text-send",
        idempotency_key="idem-slack-1",
    )


def test_official_rotating_app_tokens_are_accepted_and_token_swap_is_refused() -> (
    None
):
    rotating = "xoxe.xapp-" + "r" * 24
    bot = "xoxb-" + "b" * 20
    static_app = "xapp-" + "a" * 20
    assert SlackCredentials(bot, rotating).app_token == rotating
    assert SlackCredentials(bot, static_app).app_token == static_app
    for misplaced in (bot, "xoxe-" + "e" * 24, "xapp-" + "a"):
        with pytest.raises(ValueError) as swapped:
            SlackCredentials(bot, misplaced)
        assert misplaced not in str(swapped.value)
        assert bot not in str(swapped.value)
    with pytest.raises(ValueError) as in_bot_slot:
        SlackCredentials(rotating, static_app)
    assert rotating not in str(in_bot_slot.value)


def test_socket_mode_ack_waits_for_matching_durable_custody_across_socket_refresh() -> (
    None
):
    async def scenario() -> None:
        trace: list[str] = []

        class OrderedLedger(_Ledger):
            async def commit(
                self, instance_id: str, generation: int, token: str
            ) -> None:
                trace.append("custody")
                await super().commit(instance_id, generation, token)

        class OrderedTransport(_Transport):
            async def acknowledge(self, envelope_id: str) -> None:
                trace.append("ack")
                await super().acknowledge(envelope_id)

        frame = {
            "envelope_id": "env-1",
            "type": "events_api",
            "payload": {
                "team_id": "T1",
                "event_id": "Ev1",
                "event_time": 1710000000,
                "event": {
                    "type": "message",
                    "channel_type": "im",
                    "channel": "D1",
                    "user": "U1",
                    "ts": "171.1",
                    "text": "hello",
                    "files": [
                        {
                            "id": "F1",
                            "name": "a.txt",
                            "mimetype": "text/plain",
                            "size": 4,
                        }
                    ],
                },
            },
        }
        transport = OrderedTransport([frame])
        ledger = OrderedLedger()
        runtime = SlackSocketRuntime(
            transport, ledger, _Credentials(), "receiver:slack"
        )
        session = await runtime.start(_instance(), SimpleNamespace())
        assert session.ingress_mode == "pull"
        unit = await runtime.next_ingress()
        assert unit.checkpoint.token == "env-1"
        assert {alias.alias_kind for alias in unit.envelope.observation.aliases} == {
            "event",
            "message",
        }
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
        ) == ("slack", "T1", "D1", "171.1")
        with pytest.raises(ValueError, match="custody digest"):
            await runtime.commit_checkpoint(
                unit.checkpoint, CustodyReceipt("j-1", "wrong", 1)
            )
        assert transport.acks == []
        digest = frozen_json_digest_v1(unit.envelope.payload)
        await runtime.commit_checkpoint(
            unit.checkpoint, CustodyReceipt("j-1", digest, 1)
        )
        assert transport.acks == ["env-1"]
        assert ledger.value == "env-1"
        assert trace == ["custody", "ack"]

        normalized, platform = SlackEventCodec(_instance(), "T1", "B1").decode(
            HydratedEvent(unit.envelope, unit.envelope.payload)
        )
        assert platform.event_id == "Ev1"
        assert platform.file_ids == ("F1",)
        assert normalized.provenance.native_sender_id == "U1"
        assert normalized.context.source_chat.native_chat_id == "D1"
        assert normalized.context.channel_id.startswith("adapter:slack:")
        assert normalized.attachments[0].handle_id.endswith(":F1")
        await runtime.reconnect("socket-refresh")
        assert transport.auth_calls == 2
        assert transport.close_socket_calls == ["socket-refresh"]
        assert transport.aclose_calls == 0

    asyncio.run(scenario())


def test_replacement_runtime_resumes_committed_unacked_custody_without_eager_ack() -> (
    None
):
    async def scenario() -> None:
        frame = {
            "envelope_id": "env-restart",
            "type": "events_api",
            "payload": {
                "team_id": "T1",
                "event_id": "EvR",
                "event_time": 1710000000,
                "event": {
                    "type": "message",
                    "channel_type": "im",
                    "channel": "D1",
                    "user": "U1",
                    "ts": "171.7",
                    "text": "restart custody",
                },
            },
        }

        class DurableLedger(_Ledger):
            def __init__(self) -> None:
                super().__init__()
                self.loads: list[tuple[str, int]] = []
                self.commits: list[str] = []

            async def load(self, instance_id: str, generation: int) -> str | None:
                self.loads.append((instance_id, generation))
                return await super().load(instance_id, generation)

            async def commit(
                self, instance_id: str, generation: int, token: str
            ) -> None:
                self.commits.append(token)
                await super().commit(instance_id, generation, token)

        class DurableJournalSink:
            """Durable admission store that survives runtime replacement."""

            def __init__(self) -> None:
                self.custody: dict[str, CustodyReceipt] = {}
                self.admitted_effects: list[str] = []

            async def stage(self, unit: object) -> CustodyReceipt:
                observation = unit.envelope.observation
                receipt = self.custody.get(observation.observation_id)
                if receipt is None:
                    receipt = CustodyReceipt(
                        f"journal:{observation.observation_id}",
                        observation.observation_digest,
                        1710000000000,
                    )
                    self.custody[observation.observation_id] = receipt
                    self.admitted_effects.append(observation.observation_id)
                return receipt

        class CrashingAckTransport(_Transport):
            async def acknowledge(self, envelope_id: str) -> None:
                raise RuntimeError(
                    "simulated process death after durable commit, before ACK"
                )

        ledger = DurableLedger()
        sink = DurableJournalSink()
        dying = CrashingAckTransport([frame])
        runtime_a = SlackSocketRuntime(
            dying, ledger, _Credentials(), "receiver:slack"
        )
        await runtime_a.start(_instance(), sink)
        unit_a = await runtime_a.next_ingress()
        receipt_a = await sink.stage(unit_a)
        with pytest.raises(RuntimeError, match="simulated process death"):
            await runtime_a.commit_checkpoint(unit_a.checkpoint, receipt_a)
        assert dying.acks == []
        assert ledger.value == "env-restart"

        replacement = _Transport([dict(frame)])
        runtime_b = SlackSocketRuntime(
            replacement, ledger, _Credentials(), "receiver:slack"
        )
        session_b = await runtime_b.start(_instance(), sink)
        assert session_b.ingress_mode == "pull"
        assert ledger.loads == [("slack-1", 2), ("slack-1", 2)]
        assert replacement.acks == []
        with pytest.raises(ValueError, match="not pending"):
            await runtime_b.commit_checkpoint(
                NativeCheckpoint("callback-ack", "env-restart", None), receipt_a
            )
        assert replacement.acks == []

        unit_b = await runtime_b.next_ingress()
        assert unit_b.checkpoint.token == "env-restart"
        receipt_b = await sink.stage(unit_b)
        assert sink.admitted_effects == [
            unit_a.envelope.observation.observation_id
        ]
        await runtime_b.commit_checkpoint(unit_b.checkpoint, receipt_b)
        assert replacement.acks == ["env-restart"]
        assert ledger.commits == ["env-restart", "env-restart"]
        assert (await runtime_b.health()).pending_count == 0

    asyncio.run(scenario())


def test_link_disabled_blocks_without_reconnect_and_file_handle_fails_closed() -> None:
    async def scenario() -> None:
        transport = _Transport([{"type": "disconnect", "reason": "link_disabled"}])
        runtime = SlackSocketRuntime(
            transport, _Ledger(), _Credentials(), "receiver:slack"
        )
        await runtime.start(_instance(), SimpleNamespace())
        with pytest.raises(RuntimeError, match="SLACK_LINK_DISABLED"):
            await runtime.next_ingress()
        assert transport.auth_calls == 1
        assert transport.close_socket_calls == []
        assert (await runtime.health()).state == "blocked"

        routed = await runtime.submit(
            _operation(
                method="files.uploadV2",
                handle_id="authority-handle-only",
            )
        )
        assert routed.submissions[0].state == "failed-final"
        assert routed.submissions[0].reason_code == "slack-media-route-required"
        assert transport.calls == []

    asyncio.run(scenario())


def test_terminal_close_deadline_reports_unknown_with_retained_counts() -> None:
    class UnknownCloseTransport(_Transport):
        async def aclose(self, timeout_at: float) -> ResourceCloseOutcome:
            self.aclose_calls += 1
            return ResourceCloseOutcome(
                ResourceCloseState.UNKNOWN,
                1,
                False,
                False,
                "transport-close-deadline",
            )

    async def scenario() -> None:
        frame = {
            "envelope_id": "env-retained",
            "type": "events_api",
            "payload": {
                "event_id": "Ev-retained",
                "event_time": 1710000000,
                "event": {
                    "type": "message",
                    "channel_type": "im",
                    "channel": "D1",
                    "user": "U1",
                    "ts": "171.9",
                },
            },
        }
        transport = UnknownCloseTransport([frame])
        runtime = SlackSocketRuntime(
            transport, _Ledger(), _Credentials(), "receiver:slack"
        )
        await runtime.start(_instance(), SimpleNamespace())
        await runtime.next_ingress()
        outcome = await runtime.close(time.time_ns() // 1_000_000 + 10)
        assert outcome.state == "unknown"
        assert outcome.retained_ingress == 1
        assert outcome.retained_deliveries == 0
        assert outcome.reason_code == "transport-close-deadline"
        assert transport.aclose_calls == 1

    asyncio.run(scenario())


def test_slack_submit_fences_scope_and_maps_rate_limit_timeout_and_acceptance() -> None:
    async def scenario() -> None:
        transport = _Transport([])
        runtime = SlackSocketRuntime(
            transport, _Ledger(), _Credentials(), "receiver:slack"
        )
        await runtime.start(_instance(), SimpleNamespace())
        with pytest.raises(ValueError, match="generation"):
            await runtime.submit(_operation(generation=3))
        assert transport.calls == []

        transport.outcome = SlackHttpResult(
            HttpEffectObservation(
                HttpRequestPhase.RESPONSE_RECEIVED,
                HttpEffectState.REJECTED,
                True,
                429,
                None,
                None,
                "http-response-rejected",
            ),
            2500,
            "ratelimited",
        )
        limited = await runtime.submit(_operation())
        assert limited.submissions[0].state == "failed-retryable"
        assert limited.retry_after_ms == 2500

        transport.outcome = SlackHttpResult(
            HttpEffectObservation(
                HttpRequestPhase.MAY_HAVE_SENT,
                HttpEffectState.UNKNOWN,
                False,
                None,
                None,
                "ReadTimeout",
                "http-effect-unknown",
            ),
            None,
            None,
        )
        unknown = await runtime.submit(_operation())
        assert unknown.submissions[0].state == "unknown"
        assert unknown.submissions[0].definitely_not_sent is False

        transport.outcome = SlackHttpResult(
            HttpEffectObservation(
                HttpRequestPhase.RESPONSE_RECEIVED,
                HttpEffectState.ACCEPTED,
                False,
                200,
                "172.2",
                None,
                "http-effect-accepted",
            ),
            None,
            None,
        )
        accepted = await runtime.submit(_operation())
        assert accepted.submissions[0].provider_message_id == "172.2"
        health = await runtime.health()
        assert health.unknown_count == 1

    asyncio.run(scenario())


def test_slack_onboarding_authenticates_identity_and_projects_capabilities() -> None:
    async def scenario() -> None:
        transport = _Transport([])
        onboarding = SlackOnboarding(
            transport, _Credentials(), "org-1", "slack-1", "main"
        )
        assert (await onboarding.inspect({})).state == "blocked"
        identity = await onboarding.authenticate(
            OnboardingAttempt("a-1", {}, "receiver:slack")
        )
        assert await onboarding.fingerprint(identity) == AdapterInstanceRef(
            "slack-1",
            "slack",
            "socket-mode",
            "main",
            "org-1",
            _instance().external_fingerprint,
            1,
        )
        capabilities = await onboarding.probe_capabilities(identity)
        facts = {fact.name: fact.support for fact in capabilities.facts}
        assert facts["threads"] == "native"
        assert facts["reactions"] == "unsupported"

    asyncio.run(scenario())
