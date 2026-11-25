"""Gateway close table, generation-fenced heartbeat, nonce and rate-limit state."""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from ..common.transport import WebSocketCloseOrigin

RESUME_CLOSE_CODES = frozenset({4000, 4001, 4002, 4005, 4008})
IDENTIFY_CLOSE_CODES = frozenset({4003, 4007, 4009})
FATAL_CLOSE_CODES = frozenset({4004, 4010, 4011, 4012, 4013, 4014})
LOCAL_FINAL_CLOSE_CODES = frozenset({1000, 1001})
NONCE_RETENTION_MS = 15 * 60 * 1000


class GatewayAction(StrEnum):
    RESUME = "resume"
    RESUME_ONCE = "resume-once"
    IDENTIFY = "identify"
    FATAL = "fatal"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class GatewayDecision:
    action: GatewayAction
    reason_code: str


def classify_gateway_close(
    *,
    origin: WebSocketCloseOrigin,
    rcvd_code: int | None,
    has_resume_tuple: bool,
    conservative_attempted: bool,
) -> GatewayDecision:
    """Exact Discord reconnect table for normalized close observations."""

    if origin is WebSocketCloseOrigin.OPENING:
        raise ValueError("opening failures are not close-table rows")
    if origin is WebSocketCloseOrigin.LOCAL:
        if rcvd_code in LOCAL_FINAL_CLOSE_CODES:
            return GatewayDecision(GatewayAction.STOP, "discord-local-final-close")
        return GatewayDecision(GatewayAction.STOP, "discord-local-close")
    if rcvd_code is None or rcvd_code == 1006:
        if has_resume_tuple:
            return GatewayDecision(GatewayAction.RESUME, "discord-close-absent-resume")
        return GatewayDecision(GatewayAction.IDENTIFY, "discord-close-absent-identify")
    if rcvd_code in FATAL_CLOSE_CODES:
        return GatewayDecision(GatewayAction.FATAL, f"discord-close-fatal-{rcvd_code}")
    if rcvd_code in IDENTIFY_CLOSE_CODES:
        return GatewayDecision(GatewayAction.IDENTIFY, f"discord-close-invalidate-{rcvd_code}")
    if rcvd_code in RESUME_CLOSE_CODES:
        if has_resume_tuple:
            return GatewayDecision(GatewayAction.RESUME, f"discord-close-resume-{rcvd_code}")
        return GatewayDecision(GatewayAction.IDENTIFY, f"discord-close-resume-{rcvd_code}")
    if rcvd_code in LOCAL_FINAL_CLOSE_CODES:
        if conservative_attempted or not has_resume_tuple:
            return GatewayDecision(GatewayAction.IDENTIFY, "discord-close-clean-identify")
        return GatewayDecision(GatewayAction.RESUME_ONCE, "discord-close-clean-resume")
    return GatewayDecision(GatewayAction.IDENTIFY, f"discord-close-unknown-{rcvd_code}")


def discord_nonce(idempotency_key: str) -> str:
    """Stable <=25 character nonce digest of the operation idempotency key."""

    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("Discord nonce requires a non-empty idempotency key")
    return hashlib.sha256(idempotency_key.encode()).hexdigest()[:25]


class NonceLedger:
    """Retained recent-send window for exact-request reconciliation."""

    def __init__(self) -> None:
        self._sent_at: dict[str, int] = {}

    def record(self, nonce: str, *, at_ms: int) -> None:
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("nonce must be non-empty")
        self._sent_at[nonce] = at_ms
        if len(self._sent_at) > 1024:
            cutoff = at_ms - NONCE_RETENTION_MS
            self._sent_at = {
                key: stamp for key, stamp in self._sent_at.items() if stamp >= cutoff
            }

    def retained(self, nonce: str, *, now_ms: int) -> bool:
        stamp = self._sent_at.get(nonce)
        return stamp is not None and 0 <= now_ms - stamp <= NONCE_RETENTION_MS


@dataclass(frozen=True, slots=True)
class RateRejection:
    route: str
    bucket: str | None
    global_limited: bool
    retry_until_ms: int


class DiscordRateLimits:
    """Per-route bucket plus global 429 retry-second tracking."""

    def __init__(self) -> None:
        self._route_deadline: dict[str, float] = {}
        self._bucket_deadline: dict[str, float] = {}
        self._route_buckets: dict[str, set[str]] = {}
        self._global_deadline: float = 0.0

    def record_rejection(
        self,
        *,
        route: str,
        bucket: str | None,
        retry_after_ms: int,
        global_limited: bool,
        now_ms: int,
    ) -> None:
        if not route or isinstance(route, bool):
            raise ValueError("rate-limit route must be a non-empty string")
        if retry_after_ms < 0:
            retry_after_ms = 0
        deadline = now_ms + retry_after_ms
        self._route_deadline[route] = max(self._route_deadline.get(route, 0.0), deadline)
        if bucket:
            self._bucket_deadline[bucket] = max(self._bucket_deadline.get(bucket, 0.0), deadline)
            self._route_buckets.setdefault(route, set()).add(bucket)
        if global_limited:
            self._global_deadline = max(self._global_deadline, deadline)

    def pending_retry_ms(self, *, route: str, now_ms: int) -> int:
        deadlines = [self._route_deadline.get(route, 0.0), self._global_deadline]
        for bucket in self._route_buckets.get(route, ()):
            deadlines.append(self._bucket_deadline.get(bucket, 0.0))
        remaining = max(0.0, max(deadlines) - now_ms)
        return int(remaining) if remaining else 0


HeartbeatSender = Callable[[], Awaitable[None]]
MissedAckAction = Callable[[int], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[float, float], float]


def _default_jitter(low: float, high: float) -> float:
    return low + (high - low) * random.random()


class HeartbeatSupervisor:
    """Exactly one heartbeat task, generation-fenced, first beat after jitter."""

    def __init__(
        self,
        *,
        send_heartbeat: HeartbeatSender,
        on_missed_ack: MissedAckAction,
        sleep: Sleep | None = None,
        jitter: Jitter | None = None,
    ) -> None:
        self._send = send_heartbeat
        self._on_missed = on_missed_ack
        self._sleep: Sleep = sleep or asyncio.sleep
        self._jitter: Jitter = jitter or _default_jitter
        self._generation = 0
        self._acked = True
        self._task: asyncio.Task[None] | None = None
        self._missed_dispatched = False

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def acked(self) -> bool:
        return self._acked

    def begin(self, *, generation: int, interval_ms: int) -> None:
        if generation < 1:
            raise ValueError("heartbeat generation must be positive")
        if isinstance(interval_ms, bool) or not isinstance(interval_ms, int) or interval_ms < 1:
            raise ValueError("heartbeat interval must be a positive integer of milliseconds")
        current = asyncio.current_task()
        if self._task is not None and self._task is not current:
            self._task.cancel()
        self._generation = generation
        self._acked = True
        self._missed_dispatched = False
        self._task = asyncio.create_task(
            self._run(generation, interval_ms / 1000),
            name=f"discord-heartbeat-{generation}",
        )

    def cancel_nowait(self) -> None:
        if self._task is not None:
            self._task.cancel()
        self._task = None

    async def cancel(self) -> None:
        self.cancel_nowait()

    def note_ack(self) -> bool:
        """op11 fence: an ACK only counts for the live generation."""

        if self._task is not None and not self._task.done():
            self._acked = True
            return True
        return False

    async def request_immediate(self) -> None:
        """Server-requested op1: answer now with the latest received sequence."""

        if self._task is not None and not self._task.done():
            self._acked = False
            await self._send()

    async def _run(self, generation: int, interval_seconds: float) -> None:
        try:
            await self._sleep(self._jitter(0.0, interval_seconds))
            while self._generation == generation:
                if not self._acked and not self._missed_dispatched:
                    self._missed_dispatched = True
                    await self._on_missed(generation)
                    return
                self._acked = False
                await self._send()
                await self._sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


def default_now_ms() -> int:
    return time.time_ns() // 1_000_000


__all__ = [
    "FATAL_CLOSE_CODES",
    "GatewayAction",
    "GatewayDecision",
    "HeartbeatSupervisor",
    "IDENTIFY_CLOSE_CODES",
    "LOCAL_FINAL_CLOSE_CODES",
    "NONCE_RETENTION_MS",
    "NonceLedger",
    "RESUME_CLOSE_CODES",
    "RateRejection",
    "DiscordRateLimits",
    "classify_gateway_close",
    "default_now_ms",
    "discord_nonce",
]
