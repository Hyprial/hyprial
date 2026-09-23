"""Durable, bounded native reaction effects for one Lark worker.

Only native message ids and effect-control metadata enter this database.  The
actor owns every row mutation; SDK calls run on two independent bounded lanes
and return generation/token-fenced completions.  A reply is terminal for a
message: a later ACK request is suppressed, and a late successful ACK effect
is followed by another reply cleanup.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable

from hyprial.actor_runtime import ActorHandle, ActorRuntime, ActorSpec, AdmissionResult

REACTION_EFFECTS_SCHEMA_VERSION = 1
DEFAULT_EFFECT_CAPACITY = 10_000
DEFAULT_OVERFLOW_CAPACITY = 500
DEFAULT_EFFECT_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_OVERFLOW_TTL_SECONDS = 24 * 60 * 60
_BUSY_TIMEOUT_MS = 5_000


class ReactionEffectKind(StrEnum):
    ACK = "ack"
    REPLY = "reply"


class ReactionEffectAdmission(StrEnum):
    ACCEPTED = "accepted"
    OVERLOADED = "overloaded"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ReactionEffectRecord:
    native_message_id: str
    desired_kind: ReactionEffectKind
    generation: int
    token: str
    status: str
    attempts: int
    created_at: float
    updated_at: float
    expires_at: float
    error_code: str | None


@dataclass(frozen=True, slots=True)
class ReactionEffectsSnapshot:
    effects: tuple[ReactionEffectRecord, ...]
    overflow_count: int


@dataclass(frozen=True, slots=True)
class _Submit:
    kind: ReactionEffectKind
    native_message_id: str
    persisted: Future[ReactionEffectAdmission]
    activate: bool = True


@dataclass(frozen=True, slots=True)
class _Completed:
    kind: ReactionEffectKind
    native_message_id: str
    generation: int
    token: str
    succeeded: bool
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class _Recover:
    pass


@dataclass(frozen=True, slots=True)
class _CollectGarbage:
    pass


@dataclass(frozen=True, slots=True)
class _Barrier:
    reached: threading.Event


class _ReactionEffectStore:
    """SQLite rows owned by the reaction-effect actor."""

    def __init__(
        self,
        path: Path,
        *,
        effect_capacity: int,
        overflow_capacity: int,
        effect_ttl: float,
        overflow_ttl: float,
        clock: Callable[[], float],
    ) -> None:
        if effect_capacity < 1 or overflow_capacity < 1:
            raise ValueError("reaction effect capacities must be positive")
        if effect_ttl <= 0 or overflow_ttl <= 0:
            raise ValueError("reaction effect TTLs must be positive")
        self.path = path
        self.effect_capacity = effect_capacity
        self.overflow_capacity = overflow_capacity
        self.effect_ttl = effect_ttl
        self.overflow_ttl = overflow_ttl
        self.clock = clock
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        self.connection = sqlite3.connect(
            path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = NORMAL")
        self._create_schema()
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _create_schema(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, REACTION_EFFECTS_SCHEMA_VERSION}:
            raise RuntimeError("unsupported reaction effects schema version")
        with self.connection:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reaction_effects (
                    native_message_id TEXT PRIMARY KEY,
                    desired_kind TEXT NOT NULL CHECK (desired_kind IN ('ack', 'reply')),
                    generation INTEGER NOT NULL CHECK (generation > 0),
                    token TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN ('held', 'pending', 'running', 'applied', 'failed')),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    error_code TEXT
                )
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS reaction_effects_status_updated
                ON reaction_effects(status, updated_at)
                """
            )
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS reaction_effect_overflow (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    effect_kind TEXT NOT NULL CHECK (effect_kind IN ('ack', 'reply')),
                    message_digest TEXT NOT NULL,
                    generation INTEGER NOT NULL CHECK (generation >= 0),
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            self.connection.execute(
                """
                CREATE INDEX IF NOT EXISTS reaction_effect_overflow_expiry
                ON reaction_effect_overflow(expires_at)
                """
            )
            if version == 0:
                self.connection.execute(
                    f"PRAGMA user_version = {REACTION_EFFECTS_SCHEMA_VERSION}"
                )

    def get(self, native_message_id: str) -> ReactionEffectRecord | None:
        row = self.connection.execute(
            "SELECT * FROM reaction_effects WHERE native_message_id = ?",
            (native_message_id,),
        ).fetchone()
        return self._decode(row) if row is not None else None

    def submit(
        self,
        kind: ReactionEffectKind,
        native_message_id: str,
        *,
        activate: bool,
    ) -> tuple[ReactionEffectRecord | None, str | None]:
        """Persist a desired state, returning ``(row, overflow_reason)``."""

        now = self.clock()
        self.collect_garbage(now)
        current = self.get(native_message_id)
        if current is not None:
            # Reply is terminal.  ACK callbacks can be repeated or reordered by
            # websocket/reconcile, but may never resurrect the reaction.
            if (
                current.desired_kind is ReactionEffectKind.REPLY
                and kind is ReactionEffectKind.ACK
            ):
                return current, None
            if current.desired_kind is kind and current.status != "failed":
                if activate and current.status == "held":
                    self.connection.execute(
                        """
                        UPDATE reaction_effects
                        SET status = 'pending', updated_at = ?
                        WHERE native_message_id = ? AND generation = ? AND token = ?
                        """,
                        (now, native_message_id, current.generation, current.token),
                    )
                    return self.get(native_message_id), None
                return current, None
            generation = current.generation + 1
            created_at = current.created_at
        else:
            count = int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM reaction_effects"
                ).fetchone()[0]
            )
            if count >= self.effect_capacity:
                self._evict_terminal(now, count - self.effect_capacity + 1)
                count = int(
                    self.connection.execute(
                        "SELECT COUNT(*) FROM reaction_effects"
                    ).fetchone()[0]
                )
            if count >= self.effect_capacity:
                self.record_overflow(kind, native_message_id, 0, "ledger-capacity")
                return None, "ledger-capacity"
            generation = 1
            created_at = now
        token = uuid.uuid4().hex
        self.connection.execute(
            """
            INSERT INTO reaction_effects (
                native_message_id, desired_kind, generation, token, status,
                attempts, created_at, updated_at, expires_at, error_code
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, NULL)
            ON CONFLICT(native_message_id) DO UPDATE SET
                desired_kind = excluded.desired_kind,
                generation = excluded.generation,
                token = excluded.token,
                status = excluded.status,
                attempts = 0,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at,
                error_code = NULL
            """,
            (
                native_message_id,
                kind.value,
                generation,
                token,
                "pending" if activate else "held",
                created_at,
                now,
                now + self.effect_ttl,
            ),
        )
        return self.get(native_message_id), None

    def recover(self) -> tuple[ReactionEffectRecord, ...]:
        now = self.clock()
        self.collect_garbage(now)
        rows = tuple(
            self._decode(row)
            for row in self.connection.execute(
                """
                SELECT * FROM reaction_effects
                WHERE status IN ('held', 'pending', 'running')
                ORDER BY updated_at, native_message_id
                """
            ).fetchall()
        )
        recovered: list[ReactionEffectRecord] = []
        for row in rows:
            token = uuid.uuid4().hex
            self.connection.execute(
                """
                UPDATE reaction_effects
                SET generation = generation + 1, token = ?, status = 'pending',
                    updated_at = ?, error_code = NULL
                WHERE native_message_id = ? AND generation = ? AND token = ?
                """,
                (token, now, row.native_message_id, row.generation, row.token),
            )
            current = self.get(row.native_message_id)
            if current is not None:
                recovered.append(current)
        return tuple(recovered)

    def pending(self, kind: ReactionEffectKind) -> tuple[ReactionEffectRecord, ...]:
        return tuple(
            self._decode(row)
            for row in self.connection.execute(
                """
                SELECT * FROM reaction_effects
                WHERE desired_kind = ? AND status = 'pending'
                ORDER BY updated_at, native_message_id
                """,
                (kind.value,),
            ).fetchall()
        )

    def mark_running(self, row: ReactionEffectRecord) -> bool:
        updated = self.connection.execute(
            """
            UPDATE reaction_effects
            SET status = 'running', attempts = attempts + 1, updated_at = ?
            WHERE native_message_id = ? AND generation = ? AND token = ?
                AND desired_kind = ? AND status = 'pending'
            """,
            (
                self.clock(),
                row.native_message_id,
                row.generation,
                row.token,
                row.desired_kind.value,
            ),
        )
        return updated.rowcount == 1

    def complete(self, completion: _Completed) -> ReactionEffectRecord | None:
        current = self.get(completion.native_message_id)
        if current is None:
            return None
        if (
            current.generation != completion.generation
            or current.token != completion.token
            or current.desired_kind is not completion.kind
        ):
            return current
        self.connection.execute(
            """
            UPDATE reaction_effects
            SET status = ?, updated_at = ?, error_code = ?
            WHERE native_message_id = ? AND generation = ? AND token = ?
            """,
            (
                "applied" if completion.succeeded else "failed",
                self.clock(),
                completion.error_code,
                completion.native_message_id,
                completion.generation,
                completion.token,
            ),
        )
        return self.get(completion.native_message_id)

    def compensate_late_ack(
        self, completion: _Completed
    ) -> ReactionEffectRecord | None:
        current = self.get(completion.native_message_id)
        if (
            not completion.succeeded
            or completion.kind is not ReactionEffectKind.ACK
            or current is None
            or current.desired_kind is not ReactionEffectKind.REPLY
            or (
                current.generation == completion.generation
                and current.token == completion.token
            )
        ):
            return None
        now = self.clock()
        token = uuid.uuid4().hex
        self.connection.execute(
            """
            UPDATE reaction_effects
            SET generation = generation + 1, token = ?,
                status = CASE WHEN status = 'held' THEN 'held' ELSE 'pending' END,
                attempts = 0, updated_at = ?, expires_at = ?, error_code = NULL
            WHERE native_message_id = ? AND desired_kind = 'reply'
            """,
            (
                token,
                now,
                now + self.effect_ttl,
                completion.native_message_id,
            ),
        )
        return self.get(completion.native_message_id)

    def record_overflow(
        self,
        kind: ReactionEffectKind,
        native_message_id: str,
        generation: int,
        reason: str,
    ) -> None:
        now = self.clock()
        digest = hashlib.sha256(native_message_id.encode("utf-8")).hexdigest()[:24]
        self.connection.execute(
            """
            INSERT INTO reaction_effect_overflow (
                effect_kind, message_digest, generation, reason, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (kind.value, digest, generation, reason, now, now + self.overflow_ttl),
        )
        excess = (
            int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM reaction_effect_overflow"
                ).fetchone()[0]
            )
            - self.overflow_capacity
        )
        if excess > 0:
            self.connection.execute(
                """
                DELETE FROM reaction_effect_overflow WHERE sequence IN (
                    SELECT sequence FROM reaction_effect_overflow
                    ORDER BY sequence LIMIT ?
                )
                """,
                (excess,),
            )

    def collect_garbage(self, now: float | None = None) -> None:
        current = self.clock() if now is None else now
        expiring = self.connection.execute(
            """
            SELECT native_message_id, desired_kind, generation
            FROM reaction_effects
            WHERE expires_at <= ? AND status IN ('held', 'pending', 'running')
            """,
            (current,),
        ).fetchall()
        for row in expiring:
            self.record_overflow(
                ReactionEffectKind(row["desired_kind"]),
                row["native_message_id"],
                row["generation"],
                "ttl-expired",
            )
        self.connection.execute(
            "DELETE FROM reaction_effects WHERE expires_at <= ?", (current,)
        )
        self.connection.execute(
            "DELETE FROM reaction_effect_overflow WHERE expires_at <= ?", (current,)
        )

    def _evict_terminal(self, now: float, count: int) -> None:
        if count <= 0:
            return
        self.connection.execute(
            """
            DELETE FROM reaction_effects WHERE native_message_id IN (
                SELECT native_message_id FROM reaction_effects
                WHERE status IN ('applied', 'failed')
                ORDER BY updated_at, native_message_id LIMIT ?
            )
            """,
            (count,),
        )
        self.connection.execute(
            "DELETE FROM reaction_effect_overflow WHERE expires_at <= ?", (now,)
        )

    def snapshot(self) -> ReactionEffectsSnapshot:
        effects = tuple(
            self._decode(row)
            for row in self.connection.execute(
                "SELECT * FROM reaction_effects ORDER BY native_message_id"
            ).fetchall()
        )
        overflow_count = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM reaction_effect_overflow"
            ).fetchone()[0]
        )
        return ReactionEffectsSnapshot(effects, overflow_count)

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _decode(row: sqlite3.Row) -> ReactionEffectRecord:
        return ReactionEffectRecord(
            native_message_id=row["native_message_id"],
            desired_kind=ReactionEffectKind(row["desired_kind"]),
            generation=row["generation"],
            token=row["token"],
            status=row["status"],
            attempts=row["attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            expires_at=row["expires_at"],
            error_code=row["error_code"],
        )


class _NativeLane:
    """One independent client/executor with a hard running+queued bound."""

    def __init__(
        self,
        kind: ReactionEffectKind,
        effect: Callable[[str], None],
        completion: Callable[[_Completed], None],
        *,
        capacity: int,
    ) -> None:
        if capacity < 1:
            raise ValueError("reaction effect lane capacity must be positive")
        self.kind = kind
        self.effect = effect
        self.completion = completion
        self.slots = threading.BoundedSemaphore(capacity)
        self.pool = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"hyprial-lark-reaction-{kind.value}",
        )
        self.closing = False

    def reserve(self) -> bool:
        if self.closing or not self.slots.acquire(blocking=False):
            return False
        return True

    def release_reservation(self) -> None:
        self.slots.release()

    def submit_reserved(self, row: ReactionEffectRecord) -> None:
        future = self.pool.submit(self.effect, row.native_message_id)

        def completed(done: Future[None]) -> None:
            try:
                done.result()
            except BaseException as error:
                # SDK exception strings can contain URLs, request bodies or
                # credentials. Persist only the exception class taxonomy.
                result = _Completed(
                    self.kind,
                    row.native_message_id,
                    row.generation,
                    row.token,
                    False,
                    type(error).__name__[:80],
                )
            else:
                result = _Completed(
                    self.kind,
                    row.native_message_id,
                    row.generation,
                    row.token,
                    True,
                )
            finally:
                self.slots.release()
            self.completion(result)

        future.add_done_callback(completed)

    def close(self) -> None:
        self.closing = True
        self.pool.shutdown(wait=False, cancel_futures=True)


class _ReactionEffectHandler:
    def __init__(
        self,
        store: _ReactionEffectStore,
        ack_lane: _NativeLane,
        reply_lane: _NativeLane,
        overflow_report: Callable[[dict[str, object]], None],
    ) -> None:
        self.store = store
        self.lanes = {
            ReactionEffectKind.ACK: ack_lane,
            ReactionEffectKind.REPLY: reply_lane,
        }
        self.overflow_report = overflow_report
        self.recovered = False

    def __call__(self, command: object) -> None:
        # Handler construction is the actor-generation boundary.  Reconcile
        # before that generation accepts any command, including after an
        # in-process guardian restart rather than only a worker-process restart.
        if not self.recovered:
            self.store.recover()
            self.recovered = True
            self._pump(ReactionEffectKind.ACK)
            self._pump(ReactionEffectKind.REPLY)
        if isinstance(command, _Recover):
            return
        if isinstance(command, _Submit):
            row, overflow = self.store.submit(
                command.kind,
                command.native_message_id,
                activate=command.activate,
            )
            if overflow is not None:
                self._report(command.kind, overflow)
                command.persisted.set_result(ReactionEffectAdmission.OVERLOADED)
            else:
                # The caller may now publish its higher-level receipt: desired
                # state and generation/token are durable, while native I/O
                # remains isolated on the independent lane.
                command.persisted.set_result(ReactionEffectAdmission.ACCEPTED)
            if row is not None and command.activate:
                self._pump(row.desired_kind)
            return
        if isinstance(command, _Completed):
            compensated = self.store.compensate_late_ack(command)
            if compensated is None:
                self.store.complete(command)
            self._pump(ReactionEffectKind.ACK)
            self._pump(ReactionEffectKind.REPLY)
            return
        if isinstance(command, _CollectGarbage):
            self.store.collect_garbage()
            return
        if isinstance(command, _Barrier):
            command.reached.set()
            return
        raise TypeError(
            f"unsupported reaction effect command: {type(command).__name__}"
        )

    def _pump(self, kind: ReactionEffectKind) -> None:
        lane = self.lanes[kind]
        for row in self.store.pending(kind):
            if not lane.reserve():
                self.store.record_overflow(
                    kind, row.native_message_id, row.generation, "lane-capacity"
                )
                self._report(kind, "lane-capacity")
                return
            if not self.store.mark_running(row):
                lane.release_reservation()
                raise RuntimeError("reaction effect generation changed during dispatch")
            lane.submit_reserved(row)

    def _report(self, kind: ReactionEffectKind, reason: str) -> None:
        self.overflow_report(
            {
                "event": "lark.reaction-effect.overflow",
                "kind": kind.value,
                "reason": reason,
            }
        )


class ReactionEffectsRuntime:
    """Bounded worker facade; native ACK/reply I/O never runs on its caller."""

    def __init__(
        self,
        path: Path,
        *,
        ack_effect: Callable[[str], None],
        reply_effect: Callable[[str], None],
        overflow_report: Callable[[dict[str, object]], None] | None = None,
        mailbox_capacity: int = 256,
        ack_capacity: int = 128,
        reply_capacity: int = 128,
        effect_capacity: int = DEFAULT_EFFECT_CAPACITY,
        overflow_capacity: int = DEFAULT_OVERFLOW_CAPACITY,
        effect_ttl: float = DEFAULT_EFFECT_TTL_SECONDS,
        overflow_ttl: float = DEFAULT_OVERFLOW_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self._runtime = ActorRuntime()
        self._handle: ActorHandle | None = None
        self._closing = False
        self._store = _ReactionEffectStore(
            path,
            effect_capacity=effect_capacity,
            overflow_capacity=overflow_capacity,
            effect_ttl=effect_ttl,
            overflow_ttl=overflow_ttl,
            clock=clock,
        )

        def completion(value: _Completed) -> None:
            handle = self._handle
            if handle is None or self._closing:
                return
            deadline = time.monotonic() + 5.0
            while self._runtime.tell(handle, value) is not AdmissionResult.ACCEPTED:
                if self._closing or time.monotonic() >= deadline:
                    return
                time.sleep(0.01)

        self._ack_lane = _NativeLane(
            ReactionEffectKind.ACK,
            ack_effect,
            completion,
            capacity=ack_capacity,
        )
        self._reply_lane = _NativeLane(
            ReactionEffectKind.REPLY,
            reply_effect,
            completion,
            capacity=reply_capacity,
        )
        report = overflow_report or (lambda _payload: None)
        # close() needs to say something when the actor refuses to stop, and
        # that is a process-integrity event rather than routine telemetry --
        # so it must not vanish into the no-op default the way an overflow
        # report may.  Keep the caller's channel and whether one was given.
        self._overflow_report = overflow_report
        self._handle = self._runtime.start(
            ActorSpec(
                name=f"lark-reaction-effects:{path.parent.name}",
                handler_factory=lambda: _ReactionEffectHandler(
                    self._store, self._ack_lane, self._reply_lane, report
                ),
                mailbox_capacity=mailbox_capacity,
                supervision_profile="external_io",
            )
        )
        if self._runtime.tell(self._handle, _Recover()) is not AdmissionResult.ACCEPTED:
            raise RuntimeError("reaction effect recovery was not admitted")

    def ack(self, native_message_id: str) -> ReactionEffectAdmission:
        return self._submit(ReactionEffectKind.ACK, native_message_id)

    def reply(self, native_message_id: str) -> ReactionEffectAdmission:
        return self._submit(ReactionEffectKind.REPLY, native_message_id)

    def persist_reply(self, native_message_id: str) -> ReactionEffectAdmission:
        """Hold durable reply intent until the higher-level receipt is sent."""

        return self._submit(
            ReactionEffectKind.REPLY,
            native_message_id,
            activate=False,
        )

    def collect_garbage(self) -> ReactionEffectAdmission:
        return self._admit(_CollectGarbage())

    def snapshot(self) -> ReactionEffectsSnapshot:
        # Read-only diagnostics use an independent connection so the facade
        # never mutates the actor-owned connection.
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            effects = tuple(
                _ReactionEffectStore._decode(row)
                for row in connection.execute(
                    "SELECT * FROM reaction_effects ORDER BY native_message_id"
                ).fetchall()
            )
            overflow_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM reaction_effect_overflow"
                ).fetchone()[0]
            )
            return ReactionEffectsSnapshot(effects, overflow_count)
        finally:
            connection.close()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() <= deadline:
            reached = threading.Event()
            if self._admit(_Barrier(reached)) is not ReactionEffectAdmission.ACCEPTED:
                return False
            if not reached.wait(max(0.0, deadline - time.monotonic())):
                return False
            snapshot = self.snapshot()
            if all(
                row.status not in {"pending", "running"} for row in snapshot.effects
            ):
                return True
            time.sleep(0.01)
        return False

    def close(self, timeout: float = 5.0) -> None:
        handle = self._handle
        if handle is None:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        self.wait_idle(max(0.0, deadline - time.monotonic()))
        self._closing = True
        stopped = self._runtime.stop(handle, max(0.0, deadline - time.monotonic()))
        self._ack_lane.close()
        self._reply_lane.close()
        if not stopped:
            # The actor is still running. Closing the store here frees a
            # sqlite connection opened with check_same_thread=False while the
            # actor thread may be inside pending()'s execute() -- a C-level
            # use-after-free that takes down the whole worker process with
            # SIGSEGV, not just this adapter. Observed on CI (task 2713) and
            # reproduced locally.
            #
            # wait_idle above can consume the entire budget whenever an effect
            # is still in flight -- an ordinary Lark reaction call that has not
            # come back yet -- which leaves stop() a timeout of ~0 and makes
            # this the expected shutdown path on a slow network, not a rare
            # one.
            #
            # So: leak the connection rather than crash the process. It is
            # closing anyway; the file descriptor outlives us by milliseconds,
            # while the crash costs the whole daemon. What must NOT happen is
            # for this to be silent -- an actor that would not stop is the
            # interesting half, and the segfault was previously the only way
            # anyone found out.
            self._report_unstopped()
            return
        # Keep the handle until stop succeeds. ActorRuntime deliberately keeps
        # a timed-out endpoint reachable so a later close can finish cleanup;
        # dropping our handle first made that retry path unreachable and left
        # the sqlite store open for the rest of an embedding process.
        self._handle = None
        self._store.close()

    def _report_unstopped(self) -> None:
        payload: dict[str, object] = {
            "event": "lark.reaction-effect.close-abandoned",
            "reason": "actor did not stop within the close budget",
            "consequence": "effect store left open on purpose; closing it "
            "under a live actor segfaults the worker",
            "path": str(self.path),
        }
        if self._overflow_report is not None:
            self._overflow_report(payload)
            return
        # No reporter was wired. This event is worth more than the no-op
        # default gives it, so it goes to stderr rather than nowhere.
        print(json.dumps(payload, sort_keys=True), file=sys.stderr, flush=True)

    def _submit(
        self,
        kind: ReactionEffectKind,
        native_message_id: str,
        *,
        activate: bool = True,
    ) -> ReactionEffectAdmission:
        if not isinstance(native_message_id, str) or not native_message_id.strip():
            raise ValueError("native_message_id must not be blank")
        persisted: Future[ReactionEffectAdmission] = Future()
        admission = self._admit(_Submit(kind, native_message_id, persisted, activate))
        if admission is not ReactionEffectAdmission.ACCEPTED:
            return admission
        try:
            return persisted.result(timeout=5.0)
        except FutureTimeout:
            return ReactionEffectAdmission.OVERLOADED

    def _admit(self, command: object) -> ReactionEffectAdmission:
        handle = self._handle
        if handle is None or self._closing:
            return ReactionEffectAdmission.CLOSED
        admitted = self._runtime.tell(handle, command)
        if admitted is AdmissionResult.ACCEPTED:
            return ReactionEffectAdmission.ACCEPTED
        if admitted is AdmissionResult.OVERLOADED:
            return ReactionEffectAdmission.OVERLOADED
        return ReactionEffectAdmission.CLOSED


__all__ = [
    "REACTION_EFFECTS_SCHEMA_VERSION",
    "ReactionEffectAdmission",
    "ReactionEffectKind",
    "ReactionEffectRecord",
    "ReactionEffectsRuntime",
    "ReactionEffectsSnapshot",
]
