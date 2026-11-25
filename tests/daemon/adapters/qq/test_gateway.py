"""Gateway wire: handshake, heartbeat custody, close-code policy, close deadline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.contracts import CustodyReceipt, frozen_json_digest_v1
from hyprial.daemon.impl.adapters.qq.errors import (
    QQFatalGatewayClose,
    QQGatewayClosedClean,
)
from hyprial.daemon.impl.adapters.qq.transport import (
    REFRESH_TOKEN_CLOSE_CODES,
    close_action,
)

from .support import (
    APP_SECRET,
    BOT_ID,
    LocalGateway,
    LocalHTTP,
    ManualClock,
    ManualSleep,
    MemoryLedger,
    StaticCredentials,
    build_components,
    c2c_event,
    group_event,
    json_response,
    qq_instance,
)

DEFAULT_INTENTS = (1 << 25) | (1 << 26)


class Relay:
    """One gateway connection: Hello, handshake reply, heartbeat acks, pushes."""

    def __init__(
        self,
        *,
        interval_ms: int = 30000,
        session: str = "qq-session",
        ack: bool = True,
        hold_after_ready: bool = False,
    ) -> None:
        self.interval_ms = interval_ms
        self.session = session
        self.ack = ack
        self.hold_after_ready = hold_after_ready
        self.identify: dict | None = None
        self.resumes: list[dict] = []
        self.heartbeats: list[dict] = []
        self.client_frames: list[object] = []
        self.closed = asyncio.Event()
        self.pushes: asyncio.Queue[object] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._handshake_done = False

    async def _send(self, connection, frame: object) -> None:
        async with self._lock:
            await connection.send_json(frame)

    async def push(self, frame: object) -> None:
        self.pushes.put_nowait(frame)

    async def close_conn(self, code: int, reason: str = "") -> None:
        self.pushes.put_nowait(("close", code, reason))

    async def run(self, connection) -> None:
        await self._send(
            connection, {"op": 10, "d": {"heartbeat_interval": self.interval_ms}}
        )
        pump = asyncio.create_task(self._pump(connection))
        try:
            while True:
                raw = await connection.recv_json()
                if raw is None:
                    return
                self.client_frames.append(raw)
                frame = raw if isinstance(raw, dict) else {}
                op = frame.get("op")
                if op in (2, 6) and not self._handshake_done:
                    self._handshake_done = True
                    if op == 2:
                        self.identify = frame
                        await self._send(
                            connection,
                            {
                                "op": 0,
                                "s": 1,
                                "t": "READY",
                                "d": {"session_id": self.session},
                            },
                        )
                    else:
                        self.resumes.append(frame)
                        await self._send(
                            connection, {"op": 0, "s": 1, "t": "RESUMED", "d": {}}
                        )
                    if self.hold_after_ready:
                        await asyncio.sleep(3600)
                        return
                elif op == 1:
                    self.heartbeats.append(frame)
                    if self.ack:
                        await self._send(connection, {"op": 11})
        finally:
            pump.cancel()
            self.closed.set()

    async def _pump(self, connection) -> None:
        while True:
            item = await self.pushes.get()
            if isinstance(item, tuple) and item[0] == "close":
                await connection.close(item[1], item[2])
                return
            await self._send(connection, item)


class GatewayScript:
    def __init__(self) -> None:
        self.relays: list[Relay] = []
        self._pending: list[Relay] = []

    def expect(self, **kwargs) -> Relay:
        relay = Relay(**kwargs)
        self._pending.append(relay)
        return relay

    async def __call__(self, connection) -> None:
        relay = self._pending.pop(0) if self._pending else Relay()
        self.relays.append(relay)
        await relay.run(connection)


class _Env:
    """Shared fixture bundle: HTTP + gateway + components + runtime."""

    def __init__(self, tmp_path: Path, *, sleeper: ManualSleep | None = None, gateway_close_timeout: float = 10.0, **relay_kwargs) -> None:
        self.http = LocalHTTP()
        self.gateway = LocalGateway()
        self.script = GatewayScript()
        self.sleeper = sleeper or ManualSleep()
        self.clock = ManualClock()
        self.gateway_close_timeout = gateway_close_timeout
        self.gateway_url = ""
        self.tmp_path = tmp_path
        self.credentials = StaticCredentials()
        self.relay = self.script.expect(**relay_kwargs)
        self._tmp = tmp_path

    async def __aenter__(self) -> "_Env":
        self.http.responder = _rest_handler()
        self.gateway.handler = self.script
        await self.http.start()
        self.gateway_url = await self.gateway.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.runtime is not None:
            try:
                await asyncio.wait_for(self.runtime.close(2000), timeout=5)
            except Exception:
                pass
        if self.components is not None:
            try:
                await self.components.token.stop()
            except Exception:
                pass
            self.components.store.close()
        await asyncio.sleep(0.05)
        await self.gateway.stop()
        await self.http.stop()

    runtime = None
    components = None
    ledger = None

    async def build(self, *, generation: int = 2, ledger_value: str | None = None):
        from hyprial.daemon.impl.adapters.qq.provider import QQOfficialBotRuntime

        self.components = build_components(
            http=self.http,
            gateway_url=self.gateway_url,
            store_path=self.tmp_path / "store.sqlite",
            authority=None,
            clock=self.clock,
            sleep=self.sleeper,
            token_sleep=asyncio.sleep,
            jitter=lambda: 0.0,
            gateway_close_timeout=self.gateway_close_timeout,
        )
        self.ledger = MemoryLedger(ledger_value)
        self.runtime = QQOfficialBotRuntime(
            self.components, self.ledger, self.credentials, "receiver:qq"
        )
        await self.runtime.start(qq_instance(generation), SimpleNamespace())
        return self.runtime

    def token_fetches(self) -> int:
        return sum(1 for r in self.http.requests if r.path == "/app/getAppAccessToken")

    async def ingest_and_commit(self, relay: Relay, sequence: int, message_id: str):
        await relay.push(group_event(sequence, f"event-{sequence}", message_id))
        unit = await self.runtime.next_ingress()
        await self.runtime.commit_checkpoint(
            unit.checkpoint,
            CustodyReceipt("j-1", frozen_json_digest_v1(unit.envelope.payload), 1),
        )
        return unit


def _rest_handler():
    async def respond(request: object) -> tuple[int, dict[str, str], bytes]:
        if request.path == "/app/getAppAccessToken":  # type: ignore[attr-defined]
            return json_response(200, {"access_token": "tok-1", "expires_in": 7200})
        if request.path == "/users/@me":  # type: ignore[attr-defined]
            return json_response(200, {"id": BOT_ID, "username": "hyprial", "bot": True})
        return json_response(404, {"message": "not found"})

    return respond


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.002)


def _alive_heartbeat_tasks() -> int:
    return sum(
        1
        for task in asyncio.all_tasks()
        if task.get_name().startswith("qq-heartbeat") and not task.done()
    )


def test_official_identify_shape_and_single_heartbeat_owner(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=20) as env:
            await env.build()
            relay = env.script.relays[0]
            assert relay.identify is not None
            assert relay.identify == {
                "op": 2,
                "d": {
                    "token": "QQBot tok-1",
                    "intents": DEFAULT_INTENTS,
                    "shard": [0, 1],
                },
            }
            assert all(APP_SECRET not in str(frame) for frame in relay.client_frames)
            await env.ingest_and_commit(relay, 2, "message-1")
            await _wait_until(lambda: relay.heartbeats)
            assert all(frame["d"] == 2 for frame in relay.heartbeats)
            assert _alive_heartbeat_tasks() == 1
            await relay.push({"op": 1, "d": None})
            await _wait_until(lambda: len(relay.heartbeats) >= 2)
            assert relay.heartbeats[-1]["d"] == 2

    asyncio.run(scenario())


def test_qq_c2c_message_alias_records_account_and_chat_scope(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=30_000) as env:
            runtime = await env.build()
            relay = env.script.relays[0]
            await relay.push(c2c_event(2, "event-2", "message-2"))
            unit = await runtime.next_ingress()
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
            ) == ("qq", "app-1", "user-openid-1", "message-2")

    asyncio.run(scenario())


def test_missing_heartbeat_ack_reconnects_once_and_resumes_custody_seq(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=15, ack=False) as env:
            relay2 = env.script.expect()
            await env.build()
            relay1 = env.script.relays[0]
            await env.ingest_and_commit(relay1, 2, "message-1")
            await _wait_until(lambda: len(env.script.relays) == 2)
            await _wait_until(lambda: relay2.resumes)
            resume = relay2.resumes[0]
            assert resume["d"]["session_id"] == "qq-session"
            assert resume["d"]["seq"] == 2, "resume must use custody-committed seq"
            assert resume["d"]["token"] == "QQBot tok-1"
            await asyncio.sleep(0.08)
            assert len(env.script.relays) == 2, "exactly one reconnect, no loop"
            assert _alive_heartbeat_tasks() == 1

    asyncio.run(scenario())


def test_op7_reconnect_resumes_custody_committed_seq_not_last_seen(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=30000) as env:
            runtime = await env.build()
            relay1 = env.script.relays[0]
            unit = await env.ingest_and_commit(relay1, 2, "message-1")
            assert unit.checkpoint.token == "2"
            await relay1.push(group_event(3, "event-3", "message-3"))
            pending = await asyncio.create_task(env.runtime.next_ingress())
            assert pending.checkpoint.token == "3"
            relay2 = env.script.expect()
            task = asyncio.create_task(runtime.next_ingress())
            await relay1.push({"op": 7, "d": None})
            await _wait_until(lambda: relay2.resumes)
            assert relay2.resumes[0]["d"]["seq"] == 2, "seq 3 not yet committed"
            await relay2.push(group_event(4, "event-4", "message-4"))
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())


def test_op9_resumable_true_preserves_pending_custody_and_resumes(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=30000) as env:
            runtime = await env.build()
            relay1 = env.script.relays[0]
            await env.ingest_and_commit(relay1, 2, "message-1")
            await relay1.push(group_event(3, "event-3", "message-3"))
            pending = await runtime.next_ingress()
            relay2 = env.script.expect()

            await relay1.push({"op": 9, "d": True})

            await _wait_until(lambda: relay2.resumes)
            assert relay2.identify is None
            assert relay2.resumes[0]["d"]["session_id"] == "qq-session"
            assert relay2.resumes[0]["d"]["seq"] == 2
            await runtime.commit_checkpoint(
                pending.checkpoint,
                CustodyReceipt(
                    "j-2", frozen_json_digest_v1(pending.envelope.payload), 1
                ),
            )
            assert env.ledger is not None
            assert json.loads(env.ledger.value or "null") == {
                "seq": 3,
                "session_id": "qq-session",
                "v": 1,
            }

    asyncio.run(scenario())


def test_op9_invalid_session_tombstones_and_identifies_fresh(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=30000) as env:
            runtime = await env.build()
            relay1 = env.script.relays[0]
            await env.ingest_and_commit(relay1, 2, "message-1")
            relay2 = env.script.expect()
            task = asyncio.create_task(runtime.next_ingress())
            await relay1.push({"op": 9, "d": False})
            await _wait_until(lambda: relay2.identify is not None)
            assert relay2.identify is not None, "op9 must re-identify"
            assert env.ledger is not None
            assert json.loads(env.ledger.value or "null") == {
                "seq": 2,
                "session_id": None,
                "v": 1,
            }
            await relay2.push(group_event(4, "event-4", "message-4"))
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())


def test_close_code_membership_is_exactly_official_refresh_set() -> None:
    assert REFRESH_TOKEN_CLOSE_CODES == frozenset({4006, 4007, 4009}) | frozenset(
        range(4900, 4914)
    )
    for code in sorted(REFRESH_TOKEN_CLOSE_CODES):
        action = close_action(code)
        assert action.refresh == "token", code
        assert action.clear_session, code
        assert action.reconnect, code
        assert not action.fatal, code
    for code in (4001, 4002, 4005, 4010, 4011, 4012, 4013, 3000, 4500, 4916, 0):
        action = close_action(code)
        assert action.refresh == "none", code
        assert not action.clear_session, code
        assert not action.fatal, code
    assert close_action(None).reconnect is True
    auth = close_action(4004)
    assert auth.refresh == "credentials"
    assert not auth.clear_session
    limited = close_action(4008)
    assert limited.delay_ms == 60000
    assert limited.refresh == "none"
    assert not limited.clear_session
    for code in (4914, 4915):
        fatal = close_action(code)
        assert fatal.fatal
        assert not fatal.reconnect
    assert close_action(1000).reconnect is False
    assert close_action(1000).fatal is False


def test_close_4004_refetches_credentials_and_identifies_with_resume_session(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=30000) as env:
            runtime = await env.build()
            relay1 = env.script.relays[0]
            await env.ingest_and_commit(relay1, 2, "message-1")
            assert env.credentials.calls == 1
            relay2 = env.script.expect()
            task = asyncio.create_task(runtime.next_ingress())
            await relay1.close_conn(4004, "auth failed")
            await _wait_until(lambda: env.sleeper.delays)
            env.sleeper.advance()
            await _wait_until(lambda: len(env.script.relays) == 2)
            await _wait_until(lambda: relay2.resumes)
            assert env.credentials.calls == 2, "4004 must refetch credentials"
            assert env.token_fetches() == 2, "invalidated token must be refetched"
            assert relay2.resumes[0]["d"]["session_id"] == "qq-session"
            await relay2.push(group_event(3, "event-3", "message-3"))
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())


def test_close_4006_refreshes_token_and_reidentifies(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=30000) as env:
            runtime = await env.build()
            relay1 = env.script.relays[0]
            await env.ingest_and_commit(relay1, 2, "message-1")
            relay2 = env.script.expect()
            task = asyncio.create_task(runtime.next_ingress())
            await relay1.close_conn(4006, "invalid session")
            await _wait_until(lambda: env.sleeper.delays)
            env.sleeper.advance()
            await _wait_until(lambda: len(env.script.relays) == 2)
            await _wait_until(lambda: relay2.identify is not None)
            assert relay2.identify is not None, "4006 clears session: identify, not resume"
            assert env.token_fetches() == 2
            await relay2.push(group_event(3, "event-3", "message-3"))
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())


def test_close_4008_waits_sixty_seconds_without_token_refresh(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=30000) as env:
            runtime = await env.build()
            relay1 = env.script.relays[0]
            await env.ingest_and_commit(relay1, 2, "message-1")
            relay2 = env.script.expect()
            task = asyncio.create_task(runtime.next_ingress())
            await relay1.close_conn(4008, "rate limited")
            await _wait_until(lambda: env.sleeper.delays)
            assert env.sleeper.delays[-1] == 60.0
            assert env.token_fetches() == 1, "4008 must not refresh token"
            assert len(env.script.relays) == 1
            env.sleeper.advance()
            await _wait_until(lambda: len(env.script.relays) == 2)
            await _wait_until(lambda: relay2.resumes)
            assert relay2.resumes[0]["d"]["seq"] == 2
            await relay2.push(group_event(3, "event-3", "message-3"))
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())


def test_close_4914_is_fatal_without_reconnect(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=30000) as env:
            runtime = await env.build()
            relay1 = env.script.relays[0]
            await env.ingest_and_commit(relay1, 2, "message-1")
            task = asyncio.create_task(runtime.next_ingress())
            await relay1.close_conn(4914, "insufficient intents")
            with pytest.raises(QQFatalGatewayClose):
                await asyncio.wait_for(task, timeout=3)
            await asyncio.sleep(0.1)
            assert len(env.script.relays) == 1
            assert (await runtime.health()).state == "blocked"

    asyncio.run(scenario())


def test_close_1000_surfaces_clean_close_without_reconnect(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=30000) as env:
            runtime = await env.build()
            relay1 = env.script.relays[0]
            await env.ingest_and_commit(relay1, 2, "message-1")
            task = asyncio.create_task(runtime.next_ingress())
            await relay1.close_conn(1000, "bye")
            with pytest.raises(QQGatewayClosedClean):
                await asyncio.wait_for(task, timeout=3)
            await asyncio.sleep(0.1)
            assert len(env.script.relays) == 1

    asyncio.run(scenario())


def test_plain_close_code_reconnects_without_token_refresh(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=30000) as env:
            runtime = await env.build()
            relay1 = env.script.relays[0]
            await env.ingest_and_commit(relay1, 2, "message-1")
            relay2 = env.script.expect()
            task = asyncio.create_task(runtime.next_ingress())
            await relay1.close_conn(3000, "odd")
            await _wait_until(lambda: env.sleeper.delays)
            assert env.sleeper.delays[-1] == 1.0, "first official backoff delay"
            env.sleeper.advance()
            await _wait_until(lambda: len(env.script.relays) == 2)
            await _wait_until(lambda: relay2.resumes)
            assert env.token_fetches() == 1
            await relay2.push(group_event(3, "event-3", "message-3"))
            await asyncio.wait_for(task, timeout=3)

    asyncio.run(scenario())


def test_durable_checkpoint_resumes_session_across_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        durable = json.dumps({"seq": 5, "session_id": "qq-durable", "v": 1}, separators=(",", ":"))
        async with _Env(tmp_path, interval_ms=30000) as env:
            await env.build(ledger_value=durable)
            relay1 = env.script.relays[0]
            await _wait_until(lambda: relay1.resumes)
            assert relay1.resumes[0]["d"] == {
                "token": "QQBot tok-1",
                "session_id": "qq-durable",
                "seq": 5,
            }

    asyncio.run(scenario())


def test_close_deadline_returns_unknown_when_socket_close_stalls(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=30000) as env:
            runtime = await env.build()
            binding = env.components.owner._socket
            assert binding is not None
            real_socket = binding.resource
            release = asyncio.Event()

            class _NonCooperatingClose:
                async def close(self, code: int = 1000, reason: str = "") -> None:
                    await release.wait()
                    await real_socket.close(code, reason)

            binding.resource = _NonCooperatingClose()
            outcome = await asyncio.wait_for(runtime.close(120), timeout=5)
            assert outcome.state == "unknown"
            env.runtime = None
            release.set()
            await asyncio.sleep(0.6)

    asyncio.run(scenario())


def test_close_returns_clean_when_server_answers(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with _Env(tmp_path, interval_ms=30000) as env:
            runtime = await env.build()
            outcome = await asyncio.wait_for(runtime.close(3000), timeout=5)
            assert outcome.state == "closed"
            env.runtime = None

    asyncio.run(scenario())
