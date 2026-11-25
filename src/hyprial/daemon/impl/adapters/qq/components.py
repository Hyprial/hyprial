"""QQ wire composition: durable operation store and the default components."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from hyprial.daemon.impl.adapters.common.transport import (
    SealedNativeMediaAuthority,
    TransportResourceOwner,
)

from .errors import QQMsgSeqLimit
from .transport import QQGatewaySocket, QQRestWire, QQTokenManager

OFFICIAL_TOKEN_BASE = "https://bots.qq.com"
OFFICIAL_API_BASE = "https://api.sgroup.qq.com"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_REPLY_LIMIT = 4
DEFAULT_WINDOW_MS = 3_600_000


# ── Durable QQ-local operation store ─────────────────────────────────────


class QQOperationStore:
    """SQLite authority for msg_seq allocation and attempt facts.

    This is a passive-reply sequence authority, not a delivery ledger: it never
    decides delivery outcomes, it only allocates deterministic sequences under
    the source-A budget and records what was attempted and when.
    """

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be pathlib.Path")
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS qq_inbound_msg (
                    instance_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    msg_id TEXT NOT NULL,
                    first_seen_ms INTEGER NOT NULL,
                    last_seen_ms INTEGER NOT NULL,
                    PRIMARY KEY(instance_id, generation, msg_id)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS qq_msg_seq (
                    instance_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    msg_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    msg_seq INTEGER NOT NULL CHECK(msg_seq BETWEEN 0 AND 65535),
                    created_ms INTEGER NOT NULL,
                    PRIMARY KEY(instance_id, generation, msg_id, operation_id),
                    UNIQUE(instance_id, generation, msg_id, msg_seq)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS qq_attempt (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    instance_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    operation_id TEXT NOT NULL,
                    msg_id TEXT NOT NULL,
                    msg_seq INTEGER NOT NULL,
                    attempted_at_ms INTEGER NOT NULL,
                    result TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS qq_media_state (
                    instance_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    operation_id TEXT NOT NULL,
                    state_json BLOB NOT NULL,
                    updated_ms INTEGER NOT NULL,
                    PRIMARY KEY(instance_id, generation, operation_id)
                )
                """
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def record_inbound(
        self, *, instance_id: str, generation: int, msg_id: str, received_at_ms: int
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO qq_inbound_msg(instance_id,generation,msg_id,first_seen_ms,last_seen_ms)
                VALUES(?,?,?,?,?)
                ON CONFLICT(instance_id,generation,msg_id) DO UPDATE SET
                    last_seen_ms=max(last_seen_ms, excluded.last_seen_ms)
                """,
                (instance_id, generation, msg_id, received_at_ms, received_at_ms),
            )

    def inbound_status(
        self,
        *,
        instance_id: str,
        generation: int,
        msg_id: str,
        now_ms: int,
        window_ms: int = DEFAULT_WINDOW_MS,
    ) -> str:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT last_seen_ms FROM qq_inbound_msg
                WHERE instance_id=? AND generation=? AND msg_id=?
                """,
                (instance_id, generation, msg_id),
            ).fetchone()
        if row is None:
            return "unknown"
        if now_ms - row[0] > window_ms:
            return "stale"
        return "known"

    def _deterministic_seq(self, instance_id: str, generation: int, msg_id: str, operation_id: str) -> int:
        digest = hashlib.sha256(
            f"{instance_id}\0{generation}\0{msg_id}\0{operation_id}".encode()
        ).digest()
        return int.from_bytes(digest[:2], "big")

    def claim_msg_seq(
        self,
        *,
        instance_id: str,
        generation: int,
        msg_id: str,
        operation_id: str,
        now_ms: int,
        limit: int = DEFAULT_REPLY_LIMIT,
        window_ms: int = DEFAULT_WINDOW_MS,
    ) -> int:
        """Transactionally return one stable seq per operation under the budget."""
        with self._lock, self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            existing = self._connection.execute(
                """
                SELECT msg_seq FROM qq_msg_seq
                WHERE instance_id=? AND generation=? AND msg_id=? AND operation_id=?
                """,
                (instance_id, generation, msg_id, operation_id),
            ).fetchone()
            if existing is not None:
                return int(existing[0])
            window_start = now_ms - window_ms
            counted = self._connection.execute(
                """
                SELECT count(DISTINCT operation_id) FROM qq_msg_seq
                WHERE instance_id=? AND generation=? AND msg_id=? AND created_ms >= ?
                """,
                (instance_id, generation, msg_id, window_start),
            ).fetchone()[0]
            if int(counted) >= limit:
                raise QQMsgSeqLimit("qq-msg-limit")
            candidate = self._deterministic_seq(instance_id, generation, msg_id, operation_id)
            while True:
                clash = self._connection.execute(
                    """
                    SELECT operation_id FROM qq_msg_seq
                    WHERE instance_id=? AND generation=? AND msg_id=? AND msg_seq=?
                    """,
                    (instance_id, generation, msg_id, candidate),
                ).fetchone()
                if clash is None:
                    break
                candidate = (candidate + 1) % 65536
            self._connection.execute(
                """
                INSERT INTO qq_msg_seq(instance_id,generation,msg_id,operation_id,msg_seq,created_ms)
                VALUES(?,?,?,?,?,?)
                """,
                (instance_id, generation, msg_id, operation_id, candidate, now_ms),
            )
            return candidate

    def record_attempt(
        self,
        *,
        instance_id: str,
        generation: int,
        operation_id: str,
        msg_id: str,
        msg_seq: int,
        attempted_at_ms: int,
        result: str,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO qq_attempt(
                    instance_id,generation,operation_id,msg_id,msg_seq,attempted_at_ms,result
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (instance_id, generation, operation_id, msg_id, msg_seq, attempted_at_ms, result),
            )

    def attempts(self, *, instance_id: str, generation: int, operation_id: str) -> tuple[dict, ...]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT msg_id, msg_seq, attempted_at_ms, result FROM qq_attempt
                WHERE instance_id=? AND generation=? AND operation_id=? ORDER BY attempt_id
                """,
                (instance_id, generation, operation_id),
            ).fetchall()
        return tuple(
            {"msg_id": row[0], "msg_seq": row[1], "attempted_at_ms": row[2], "result": row[3]}
            for row in rows
        )

    def save_media_state(
        self,
        *,
        instance_id: str,
        generation: int,
        operation_id: str,
        state: Mapping[str, object],
        now_ms: int | None = None,
    ) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO qq_media_state(instance_id,generation,operation_id,state_json,updated_ms)
                VALUES(?,?,?,?,?)
                ON CONFLICT(instance_id,generation,operation_id) DO UPDATE SET
                    state_json=excluded.state_json, updated_ms=excluded.updated_ms
                """,
                (
                    instance_id,
                    generation,
                    operation_id,
                    json.dumps(dict(state), separators=(",", ":"), sort_keys=True),
                    now_ms if now_ms is not None else int(time.time() * 1000),
                ),
            )

    def load_media_state(
        self, *, instance_id: str, generation: int, operation_id: str
    ) -> Mapping[str, object] | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT state_json FROM qq_media_state
                WHERE instance_id=? AND generation=? AND operation_id=?
                """,
                (instance_id, generation, operation_id),
            ).fetchone()
        if row is None:
            return None
        try:
            parsed = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None


# ── Default components ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QQLoopbackOverrides:
    """Constructor-only endpoint overrides for isolated local verification."""

    token_base_url: str
    api_base_url: str
    gateway_url: str | None = None
    allow_presigned_hosts: frozenset[str] = frozenset({"127.0.0.1"})

    def __post_init__(self) -> None:
        for host in self.allow_presigned_hosts:
            if host not in LOOPBACK_HOSTS:
                raise ValueError("loopback presigned hosts must stay on loopback")
        for url in (self.token_base_url, self.api_base_url):
            if urlsplit_host(url) not in LOOPBACK_HOSTS:
                raise ValueError("loopback overrides must stay on loopback")
        if self.gateway_url is not None and urlsplit_host(self.gateway_url) not in LOOPBACK_HOSTS:
            raise ValueError("loopback overrides must stay on loopback")


def urlsplit_host(url: str) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower()


@dataclass(frozen=True)
class QQWireComponents:
    """Everything the runtime drives; owns client, sockets, token task, store."""

    client: httpx.AsyncClient
    owner: TransportResourceOwner
    token: QQTokenManager
    rest: QQRestWire
    gateway: QQGatewaySocket
    store: QQOperationStore
    media_authority: SealedNativeMediaAuthority
    loopback: QQLoopbackOverrides | None
    now_ms: Callable[[], int]
    sleep: Callable[[float], Awaitable[None]]

    def close_store(self) -> None:
        self.store.close()

    async def shutdown(self, deadline_ms: int) -> None:
        await self.token.stop()
        timeout_at = asyncio.get_running_loop().time() + deadline_ms / 1000
        await self.owner.aclose(timeout_at)
        self.close_store()


def build_default_components(
    *,
    app_id: str,
    client_secret: Callable[[], str],
    store_path: Path,
    media_authority: SealedNativeMediaAuthority,
    loopback: QQLoopbackOverrides | None = None,
    now_ms: Callable[[], int] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    token_clock: Callable[[], float] | None = None,
    token_sleep: Callable[[float], Awaitable[None]] | None = None,
    token_jitter: Callable[[], float] | None = None,
    gateway_close_timeout: float = 10.0,
    rest_timeout: float = 30.0,
    rest_upload_timeout: float = 120.0,
) -> QQWireComponents:
    """One client, one socket owner, one token task, one durable store.

    The default factory contains only official production endpoints; loopback
    overrides are constructor-only and restricted to 127.0.0.1. The sealed media
    authority is borrowed and never closed here.
    """
    if not app_id:
        raise ValueError("app_id is required")
    token_base = loopback.token_base_url if loopback else OFFICIAL_TOKEN_BASE
    api_base = loopback.api_base_url if loopback else OFFICIAL_API_BASE
    client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
    owner = TransportResourceOwner(client, owns_client=True)
    token = QQTokenManager(
        client,
        token_url=f"{token_base}/app/getAppAccessToken",
        app_id=app_id,
        client_secret=client_secret,
        now=token_clock or _loop_time,
        sleep=token_sleep or asyncio.sleep,
        jitter=token_jitter,
    )
    rest = QQRestWire(
        client,
        api_base=api_base,
        token=token,
        allowed_presigned_hosts=loopback.allow_presigned_hosts if loopback else None,
        default_timeout=rest_timeout,
        upload_timeout=rest_upload_timeout,
    )
    gateway = QQGatewaySocket(owner, close_timeout=gateway_close_timeout)
    return QQWireComponents(
        client=client,
        owner=owner,
        token=token,
        rest=rest,
        gateway=gateway,
        store=QQOperationStore(store_path),
        media_authority=media_authority,
        loopback=loopback,
        now_ms=now_ms or (lambda: int(time.time() * 1000)),
        sleep=sleep or asyncio.sleep,
    )


def _loop_time() -> float:
    return asyncio.get_running_loop().time()


__all__ = [
    "LOOPBACK_HOSTS",
    "OFFICIAL_API_BASE",
    "OFFICIAL_TOKEN_BASE",
    "QQOperationStore",
    "QQWireComponents",
    "QQLoopbackOverrides",
    "build_default_components",
]
