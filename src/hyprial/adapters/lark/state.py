"""Crash-safe persistent Lark message correlation state, backed by SQLite.

One shared database (``~/.hyprial/state/adapters.sqlite3``) serves every adapter
process on the host; rows are namespaced by an ``adapter`` column.  The
storage conventions follow ``hyprial.inbox.service``: WAL journal, full
synchronous, and a busy timeout so concurrent adapter processes queue on the
write lock instead of failing.  Mutations run under ``BEGIN IMMEDIATE`` so a
read-modify-write (capacity checks, upsert-detection) is atomic across
processes, not just across threads.

There is no migration from the JSON era (product decision: the old runtime
state is void after the cutover).  A pre-SQLite state file found on disk is
renamed with a ``.retired`` suffix -- kept for audit, never read -- and the
store starts empty.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .api import (
    MAX_LARK_TEXT_CONTENT_BYTES,
    PendingCommandCapacityStatus,
    encode_lark_text_content,
)

#: Version 2 is the SQLite schema.  Version 1 was the retired JSON layout;
#: a version mismatch fails closed instead of guessing at the contents.
STATE_SCHEMA_VERSION = 2
#: The ``identities`` table is versioned under its own meta key
#: (``identitiesSchemaVersion``) instead of bumping ``schemaVersion``.
#: Deliberate: deployed v2 builds exact-match the global stamp and fail
#: closed on anything else, while the table itself is purely additive and
#: invisible to them -- so an existing database gains the table on first
#: open by this build *without* bricking a v2 worker that restarts before
#: the rollout completes.  Once the fleet is fully on identities-aware
#: builds, a later migration may collapse this key into ``schemaVersion``.
IDENTITIES_SCHEMA_VERSION = 1
MAX_CORRELATIONS = 10_000
#: Pending replies are custody records, not a cache: unresolved records may
#: never be evicted.  New custody fails closed at either independent limit.
MAX_PENDING_COMMAND_RESPONSES = MAX_CORRELATIONS
MAX_PENDING_COMMAND_RESPONSE_BYTES = 16 * 1024 * 1024
#: Dead letters are an operator-facing audit trail, not a queue: bounded hard
#: so a broken deployment cannot grow the database without limit.
MAX_DEAD_LETTERS = 500
#: Inbound text is preserved for traceability, but truncated so one giant
#: message cannot bloat the audit trail.
DEAD_LETTER_TEXT_LIMIT = 2_000
DEAD_LETTER_DETAIL_LIMIT = 500

#: Namespace used when a caller does not name its adapter (tests, ad hoc
#: tooling).  Production workers always pass their gateway name.
DEFAULT_ADAPTER = "default"

#: Closed identity taxonomies.  ``kind`` says what the platform id denotes:
#: a human (``user``, open_id), an app's bot presence (``bot``, open_id) or
#: the app itself (``app``, app_id).  ``standing`` says how much the mapping
#: may be trusted: ``observed`` rows were collected mechanically and may be
#: refreshed by any later observation; ``verified`` rows were confirmed by a
#: human and are never downgraded or overwritten by observations.
IDENTITY_KINDS = ("user", "bot", "app")
IDENTITY_STANDINGS = ("observed", "verified")

_SQLITE_MAGIC = b"SQLite format 3\x00"
_BUSY_TIMEOUT_MS = 5_000


@dataclass(frozen=True, slots=True)
class RequestCorrelation:
    harness_message_id: str
    message_id: str
    chat_id: str
    conversation_id: str


@dataclass(frozen=True, slots=True)
class ReplyRoute:
    message_id: str
    actor_id: str
    actor_key: str
    harness_message_id: str
    conversation_id: str
    chat_id: str


@dataclass(frozen=True, slots=True)
class PendingCommandResponse:
    """Immutable Lark response persisted before its first send attempt."""

    message_id: str
    event_id: str
    kind: str
    text: str
    idempotency_key: str


class PendingCommandCapacityError(RuntimeError):
    """A new response cannot be durably accepted without losing custody."""

    def __init__(self, capacity: PendingCommandCapacityStatus) -> None:
        super().__init__("pending Lark command response capacity exhausted")
        self.capacity = capacity


def pending_command_response_encoded_bytes(response: PendingCommandResponse) -> int:
    """Return one response's exact charged UTF-8 JSON bytes."""

    return _pending_command_responses_encoded_bytes(
        {response.message_id: response}
    )


def _pending_command_responses_encoded_bytes(
    responses: dict[str, PendingCommandResponse],
) -> int:
    """Charge the larger exact durable-state or aggregate SDK representation.

    The durable budget is measured against compact, sorted,
    ``ensure_ascii=False`` JSON -- the JSON-era charging rule, kept verbatim
    so the byte limit means the same thing across the storage cutover.
    Lark's generated SDK receives the JSON string produced by
    :func:`encode_lark_text_content`.  The budget charges the larger exact
    representation, so neither persisted escaping nor SDK-wire escaping can
    bypass the byte limit. An empty/released budget reports zero bytes.
    """

    if not responses:
        return 0
    durable_map = {
        message_id: asdict(response)
        for message_id, response in responses.items()
    }
    durable_bytes = len(
        json.dumps(
            durable_map,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    sdk_bytes = sum(
        len(encode_lark_text_content(response.text).encode("utf-8"))
        for response in responses.values()
    )
    return max(durable_bytes, sdk_bytes)


def _pending_responses(
    values: dict[str, Any],
) -> dict[str, PendingCommandResponse]:
    parsed: dict[str, PendingCommandResponse] = {}
    for message_id, raw in values.items():
        try:
            if not isinstance(message_id, str) or not message_id:
                raise ValueError
            if not isinstance(raw, dict):
                raise TypeError
            response = PendingCommandResponse(**raw)
            if response.message_id != message_id or not all(
                isinstance(value, str) and value
                for value in (
                    response.message_id,
                    response.event_id,
                    response.kind,
                    response.text,
                    response.idempotency_key,
                )
            ):
                raise ValueError
            # A pending response must remain sendable after restart.  Apply
            # the platform's strict per-item wire limit at the shared parser
            # used by both load and record, so a stored response can never
            # enter durable custody when Lark would reject its body.
            if (
                len(encode_lark_text_content(response.text).encode("utf-8"))
                >= MAX_LARK_TEXT_CONTENT_BYTES
            ):
                raise ValueError
            # Validate the aggregate encodings eagerly too. Lone surrogates
            # and other corrupt persisted material must fail closed at load.
            pending_command_response_encoded_bytes(response)
        except (TypeError, ValueError, UnicodeError) as error:
            raise ValueError("invalid pending Lark command response") from error
        parsed[message_id] = response
    return parsed


def _pending_capacity(
    values: dict[str, Any], *, force_full: bool = False
) -> PendingCommandCapacityStatus:
    responses = _pending_responses(values)
    encoded_bytes = _pending_command_responses_encoded_bytes(responses)
    full = force_full or len(responses) >= MAX_PENDING_COMMAND_RESPONSES or (
        encoded_bytes >= MAX_PENDING_COMMAND_RESPONSE_BYTES
    )
    return PendingCommandCapacityStatus(
        status="full" if full else "available",
        count=len(responses),
        bytes=encoded_bytes,
        max_count=MAX_PENDING_COMMAND_RESPONSES,
        max_bytes=MAX_PENDING_COMMAND_RESPONSE_BYTES,
    )


@dataclass(frozen=True, slots=True)
class DeadLetter:
    """An inbound user message whose body would otherwise be unrecoverable.

    Keyed by the native Lark message id so an operator can later replay it
    with ``get_inbound_message``/``recover_message``.  Contains only message
    content and routing metadata — never credentials.
    """

    message_id: str
    event_id: str
    chat_id: str
    chat_type: str
    conversation_id: str
    sender_id: str
    sender_type: str
    text: str
    reason: str
    created_at: str
    detail: str | None = None
    create_time: str | None = None
    reply_to: str | None = None
    message_type: str | None = None


@dataclass(frozen=True, slots=True)
class Identity:
    """One platform identity ↔ display name ↔ hyprial ownership mapping.

    Identity lookups must run on recorded data, never on live-chat deduction:
    a member view truncated mid-page once "proved" a wrong who-is-who by
    elimination.  Rows carry their provenance (``source``) and confidence
    (``standing``) so a consumer can always tell a mechanical observation
    from a human-confirmed mapping.
    """

    kind: str
    platform_id: str
    display_name: str | None = None
    #: Feishu's cross-App stable user id.  ``platform_id`` (an open_id) is
    #: namespaced *per App*: the same human has a different open_id under
    #: every App, so rows from different adapters can only be joined through
    #: this column (or a human).  Stored when a source carries it (live
    #: message events do); never required.
    union_id: str | None = None
    hyprial_owner: str | None = None
    standing: str = "observed"
    source: str = ""
    first_seen_ms: int | None = None
    last_seen_ms: int | None = None


#: Identity fields in declaration order, so a row selected with these columns
#: constructs the dataclass directly.
_IDENTITY_COLUMNS = (
    "kind, platform_id, display_name, union_id, hyprial_owner, standing, source,"
    " first_seen_ms, last_seen_ms"
)


def _checked_identity(identity: Identity) -> Identity:
    if identity.kind not in IDENTITY_KINDS:
        raise ValueError(
            f"identity kind must be one of {', '.join(IDENTITY_KINDS)}"
        )
    if identity.standing not in IDENTITY_STANDINGS:
        raise ValueError(
            f"identity standing must be one of {', '.join(IDENTITY_STANDINGS)}"
        )
    if not identity.platform_id:
        raise ValueError("identity platform_id must be non-empty")
    if not identity.source:
        raise ValueError("identity source must be non-empty")
    return identity


def _escape_like(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


#: DeadLetter fields in declaration order, so a row selected with these
#: columns constructs the dataclass directly.
_DEAD_LETTER_COLUMNS = (
    "message_id, event_id, chat_id, chat_type, conversation_id, sender_id,"
    " sender_type, text, reason, created_at, detail, create_time, reply_to,"
    " message_type"
)

_PENDING_COLUMNS = "message_id, event_id, kind, text, idempotency_key"


def _retired_target(path: Path) -> Path:
    target = path.with_name(path.name + ".retired")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.retired.{counter}")
        counter += 1
    return target


def retire_legacy_state(path: Path) -> Path | None:
    """Rename a pre-SQLite state file aside and report where it went.

    The retired file is kept, never deleted: the dead letters inside remain
    available for a manual audit.  Nothing is read back — the cutover starts
    from an empty database by decision, not by accident.
    """

    if not path.is_file():
        return None
    target = _retired_target(path)
    path.rename(target)
    return target


class LarkStateStore:
    def __init__(
        self,
        path: Path,
        *,
        adapter: str = DEFAULT_ADAPTER,
        legacy_path: Path | None = None,
    ) -> None:
        self.path = path
        self.adapter = adapter
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        if legacy_path is not None and legacy_path.resolve() != path.resolve():
            retire_legacy_state(legacy_path)
        self._retire_foreign_database()
        # Explicit transaction control (``isolation_level=None``): Python's
        # implicit mode would start the write transaction only at the first
        # DML statement, leaving the reads of a read-modify-write outside it.
        self._db = sqlite3.connect(
            path,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            self._initialize()
        except BaseException:
            # Constructor failures retain self via their traceback. Closing
            # here rolls back any unfinished schema transaction and avoids
            # pinning WAL handles until the exception is eventually collected.
            self._db.close()
            raise

    def _initialize(self) -> None:
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        self._switch_to_wal()
        self._db.execute("PRAGMA synchronous=NORMAL")
        # No foreign keys in this schema today; enabled anyway so both host
        # databases (agents.sqlite3, adapters.sqlite3) run one convention.
        self._db.execute("PRAGMA foreign_keys=ON")
        self._create_schema()
        self._migrate_identities_owner_column()
        self._check_schema_version()
        # Fail closed at open exactly like the JSON loader did: corrupt
        # pending rows raise ``ValueError``; an over-capacity backlog raises
        # ``PendingCommandCapacityError`` before the adapter starts.
        capacity = self.pending_command_capacity()
        if (
            capacity.count > capacity.max_count
            or capacity.bytes > capacity.max_bytes
        ):
            raise PendingCommandCapacityError(replace(capacity, status="full"))

    def _retire_foreign_database(self) -> None:
        """A file already at the database path must actually be SQLite.

        The JSON era stored state under a ``.sqlite3`` name.  Reading it is
        forbidden (no migration) and silently overwriting would destroy the
        dead letters it may hold, so it is renamed aside instead.
        """

        if not self.path.is_file():
            return
        if self.path.stat().st_size == 0:
            return  # an empty file is a valid SQLite database seed
        with self.path.open("rb") as handle:
            magic = handle.read(len(_SQLITE_MAGIC))
        if magic != _SQLITE_MAGIC:
            retire_legacy_state(self.path)

    def _switch_to_wal(self) -> None:
        """Enable WAL, retrying the one lock upgrade the busy handler skips.

        Switching a rollback-journal database to WAL takes an exclusive
        lock, and SQLite deliberately does not run the busy handler for a
        shared-to-exclusive upgrade (it could deadlock).  When two adapter
        processes first open the database together, the loser therefore
        fails immediately instead of waiting.  Retrying converges: whichever
        process wins leaves the database in WAL, where this pragma is a
        lock-free no-op.
        """

        deadline = time.monotonic() + _BUSY_TIMEOUT_MS / 1000
        while True:
            try:
                self._db.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def _create_schema(self) -> None:
        with self._lock:
            # executescript implicitly commits a pre-existing transaction, so
            # BEGIN belongs INSIDE the script. Acquire the writer lock before
            # inspecting/changing the schema, then commit the DDL as one unit.
            # Autocommit per CREATE previously invited interleaving and paid a
            # separate FULL-sync commit for every table/index on a shared disk.
            self._db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS request_correlations (
                    adapter TEXT NOT NULL,
                    harness_message_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    PRIMARY KEY (adapter, harness_message_id)
                );

                CREATE TABLE IF NOT EXISTS reply_routes (
                    adapter TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_key TEXT NOT NULL,
                    harness_message_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    PRIMARY KEY (adapter, message_id)
                );

                -- The two dedup sets are consulted once per inbound event;
                -- their composite primary keys are the lookup indexes.
                CREATE TABLE IF NOT EXISTS seen_events (
                    adapter TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    PRIMARY KEY (adapter, event_id)
                );

                CREATE TABLE IF NOT EXISTS seen_messages (
                    adapter TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    PRIMARY KEY (adapter, message_id)
                );

                CREATE TABLE IF NOT EXISTS dead_letters (
                    adapter TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    sender_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    detail TEXT,
                    create_time TEXT,
                    reply_to TEXT,
                    message_type TEXT,
                    PRIMARY KEY (adapter, message_id)
                );
                CREATE INDEX IF NOT EXISTS dead_letters_by_time
                    ON dead_letters(adapter, created_at);
                CREATE INDEX IF NOT EXISTS dead_letters_by_reason
                    ON dead_letters(adapter, reason);

                -- A durable, per-window notification claim prevents multiple
                -- adapter processes (or a restart) from alerting on the same
                -- dead-letter backlog window.  It is additive to schema v2 so
                -- older workers can continue to open the shared database.
                CREATE TABLE IF NOT EXISTS dead_letter_alerts (
                    adapter TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (adapter, window_start)
                );

                -- Generic AlarmEmitter suppression.  The older
                -- dead_letter_alerts table remains the threshold alert's
                -- aggregate claim; both now feed the same emitter.
                CREATE TABLE IF NOT EXISTS alarm_throttle (
                    adapter TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    window_start_ms INTEGER NOT NULL,
                    claimed_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (
                        adapter, conversation_id, reason, window_start_ms
                    )
                );

                CREATE TABLE IF NOT EXISTS chat_types (
                    adapter TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    chat_type TEXT NOT NULL,
                    PRIMARY KEY (adapter, chat_id)
                );

                -- Chats the platform permanently refuses history for (the
                -- group was disbanded or the bot is no longer a member).
                -- Retirement only excludes the chat from the reconciliation
                -- scan set; correlations and dead letters are audit records
                -- and are deliberately kept.  Purely additive to schema v2
                -- for the same reason as ``dead_letter_alerts``.
                CREATE TABLE IF NOT EXISTS retired_chats (
                    adapter TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    code INTEGER NOT NULL,
                    retired_at TEXT NOT NULL,
                    PRIMARY KEY (adapter, chat_id)
                );

                CREATE TABLE IF NOT EXISTS conversation_timezones (
                    adapter TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    PRIMARY KEY (adapter, conversation_id)
                );

                CREATE TABLE IF NOT EXISTS pending_command_responses (
                    adapter TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    PRIMARY KEY (adapter, message_id)
                );

                -- Platform identity <-> display name <-> hyprial ownership, with
                -- provenance and confidence.  kind says what platform_id is
                -- (user/bot open_id, app app_id); standing separates
                -- mechanical observations from human-verified mappings.
                -- open_ids are namespaced per App, hence the adapter column
                -- in the key; union_id is the only cross-App join.
                CREATE TABLE IF NOT EXISTS identities (
                    adapter TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    display_name TEXT,
                    union_id TEXT,
                    hyprial_owner TEXT,
                    standing TEXT NOT NULL,
                    source TEXT NOT NULL,
                    first_seen_ms INTEGER,
                    last_seen_ms INTEGER,
                    PRIMARY KEY (adapter, kind, platform_id)
                );
                CREATE INDEX IF NOT EXISTS identities_by_name
                    ON identities(adapter, display_name);
                COMMIT;
                """
            )

    def _migrate_identities_owner_column(self) -> None:
        """Rename the pre-rename ``h2b_owner`` column to ``hyprial_owner``.

        The 2026-09-09 product code rename changed the column name in
        ``_create_schema`` but shipped no migration, so a database created
        before the rename still carries ``h2b_owner`` -- and because
        ``CREATE TABLE IF NOT EXISTS`` is a no-op there, every identity
        read fails with ``no such column: hyprial_owner`` forever.
        Detection is column-based rather than stamp-based because
        pre-rename databases already carry ``identitiesSchemaVersion = 1``.
        Rename is in-place (SQLite ≥ 3.25): no rebuild, no data movement.
        Both columns present means an interrupted or hand-edited state;
        fail closed and name it instead of silently picking one.
        """

        with self._transaction() as db:
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(identities)")
            }
            has_old = "h2b_owner" in columns
            has_new = "hyprial_owner" in columns
            if has_old and has_new:
                raise ValueError(
                    "identities table carries both h2b_owner and"
                    " hyprial_owner; refusing to guess which one is live"
                )
            if not has_old and not has_new:
                raise ValueError(
                    "identities table carries neither h2b_owner nor"
                    " hyprial_owner; unknown schema shape"
                )
            if has_old:
                db.execute(
                    "ALTER TABLE identities RENAME COLUMN h2b_owner"
                    " TO hyprial_owner"
                )

    def _check_schema_version(self) -> None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key = 'schemaVersion'"
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO meta(key, value) VALUES ('schemaVersion', ?)",
                    (str(STATE_SCHEMA_VERSION),),
                )
            elif row["value"] != str(STATE_SCHEMA_VERSION):
                raise ValueError("unsupported Lark state schema")
            # The identities sub-schema carries its own stamp (see
            # IDENTITIES_SCHEMA_VERSION for why it is not part of the global
            # version).  The table itself was already created above by
            # ``_create_schema``'s IF NOT EXISTS, so an existing v2 database
            # is upgraded by stamping alone; an unknown stamp fails closed
            # exactly like the global version does.
            identities_row = db.execute(
                "SELECT value FROM meta WHERE key = 'identitiesSchemaVersion'"
            ).fetchone()
            if identities_row is None:
                db.execute(
                    "INSERT INTO meta(key, value)"
                    " VALUES ('identitiesSchemaVersion', ?)",
                    (str(IDENTITIES_SCHEMA_VERSION),),
                )
            elif identities_row["value"] != str(IDENTITIES_SCHEMA_VERSION):
                raise ValueError("unsupported Lark identities schema")

    @contextmanager
    def _transaction(self):
        """One exclusive read-modify-write unit, atomic across processes.

        ``BEGIN IMMEDIATE`` takes the database write lock up front, so a
        concurrent adapter process queues on the busy timeout instead of
        failing mid-transaction on a deferred lock upgrade.
        """

        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _prune(self, db: sqlite3.Connection, table: str, limit: int) -> None:
        """Evict this adapter's oldest rows beyond ``limit``.

        ``rowid`` is this database's own insertion order — the SQLite twin of
        the JSON store's ``_bounded`` (which kept the newest dict entries).
        Pending command responses are custody records and are never pruned.
        """

        db.execute(
            f"DELETE FROM {table} WHERE adapter = ? AND rowid IN ("
            f" SELECT rowid FROM {table} WHERE adapter = ?"
            "  ORDER BY rowid DESC LIMIT -1 OFFSET ?)",
            (self.adapter, self.adapter, limit),
        )

    def _mark_seen_event(self, db: sqlite3.Connection, event_id: str) -> None:
        db.execute(
            "INSERT OR IGNORE INTO seen_events(adapter, event_id) VALUES (?, ?)",
            (self.adapter, event_id),
        )
        self._prune(db, "seen_events", MAX_CORRELATIONS)

    def _mark_seen_message(self, db: sqlite3.Connection, message_id: str) -> None:
        db.execute(
            "INSERT OR IGNORE INTO seen_messages(adapter, message_id)"
            " VALUES (?, ?)",
            (self.adapter, message_id),
        )
        self._prune(db, "seen_messages", MAX_CORRELATIONS)

    def _clear_dead_letter(self, db: sqlite3.Connection, message_id: str) -> None:
        db.execute(
            "DELETE FROM dead_letters WHERE adapter = ? AND message_id = ?",
            (self.adapter, message_id),
        )

    def request(self, harness_message_id: str) -> RequestCorrelation | None:
        with self._lock:
            row = self._db.execute(
                "SELECT harness_message_id, message_id, chat_id, conversation_id"
                " FROM request_correlations"
                " WHERE adapter = ? AND harness_message_id = ?",
                (self.adapter, harness_message_id),
            ).fetchone()
        return None if row is None else RequestCorrelation(**dict(row))

    def reply(self, native_message_id: str) -> ReplyRoute | None:
        with self._lock:
            row = self._db.execute(
                "SELECT message_id, actor_id, actor_key, harness_message_id,"
                " conversation_id, chat_id FROM reply_routes"
                " WHERE adapter = ? AND message_id = ?",
                (self.adapter, native_message_id),
            ).fetchone()
        return None if row is None else ReplyRoute(**dict(row))

    def seen(self, event_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM seen_events WHERE adapter = ? AND event_id = ?",
                (self.adapter, event_id),
            ).fetchone()
        return row is not None

    def seen_message(self, message_id: str) -> bool:
        """Whether this native message id already reached Harness custody.

        Unlike ``seen`` (keyed by event id), this deduplicates across
        delivery paths: a live websocket event, a manual ``recover_message``
        replay, and a reconnect reconciliation can all surface the same
        native message and only the first may be forwarded.
        """

        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM seen_messages"
                " WHERE adapter = ? AND message_id = ?",
                (self.adapter, message_id),
            ).fetchone()
        return row is not None

    def chat_type(self, chat_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT chat_type FROM chat_types"
                " WHERE adapter = ? AND chat_id = ?",
                (self.adapter, chat_id),
            ).fetchone()
        if row is None:
            return None
        value = row["chat_type"]
        return value if isinstance(value, str) and value else None

    def record_chat_type(self, chat_id: str, chat_type: str) -> None:
        """Remember a chat's type as observed on live events.

        The REST message models carry no ``chat_type``, so recovery paths
        reuse the type observed when the chat last produced a live event.
        """

        with self._transaction() as db:
            db.execute(
                """INSERT INTO chat_types(adapter, chat_id, chat_type)
                   VALUES (?, ?, ?)
                   ON CONFLICT(adapter, chat_id)
                   DO UPDATE SET chat_type = excluded.chat_type""",
                (self.adapter, chat_id, chat_type),
            )
            self._prune(db, "chat_types", MAX_CORRELATIONS)

    def recent_chats(self) -> tuple[str, ...]:
        """Chats with prior inbound activity — the reconciliation scan set.

        Retired chats (permanently refused by the platform) are excluded;
        their rows in the contributing tables are kept as audit records.
        """

        with self._lock:
            rows = self._db.execute(
                """SELECT chat_id FROM request_correlations WHERE adapter = ?
                   UNION
                   SELECT chat_id FROM dead_letters WHERE adapter = ?
                   EXCEPT
                   SELECT chat_id FROM retired_chats WHERE adapter = ?
                   ORDER BY chat_id""",
                (self.adapter, self.adapter, self.adapter),
            ).fetchall()
        return tuple(row["chat_id"] for row in rows)

    def retire_chat(self, chat_id: str, *, code: int, now: datetime) -> bool:
        """Mark a chat permanently unavailable; report if newly retired.

        Idempotent: re-retiring the same chat keeps the first observation.
        """

        with self._transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO retired_chats(
                       adapter, chat_id, code, retired_at
                   ) VALUES (?, ?, ?, ?)""",
                (self.adapter, chat_id, code, now.isoformat()),
            )
            return cursor.rowcount > 0

    def retired_chat_code(self, chat_id: str) -> int | None:
        """The platform code a chat was retired with, or None if active."""

        with self._lock:
            row = self._db.execute(
                "SELECT code FROM retired_chats"
                " WHERE adapter = ? AND chat_id = ?",
                (self.adapter, chat_id),
            ).fetchone()
        return None if row is None else int(row["code"])

    def unretire_chat(self, chat_id: str) -> bool:
        """Lift a chat's retirement; report whether one was lifted.

        Live inbound traffic from the chat is direct evidence the bot is a
        member again (e.g. re-added after removal), so the chat rejoins the
        reconciliation scan set.  Retirement is only ever lifted by this
        out-of-band fact, never by the sweep that imposed it.
        """

        with self._transaction() as db:
            cursor = db.execute(
                "DELETE FROM retired_chats WHERE adapter = ? AND chat_id = ?",
                (self.adapter, chat_id),
            )
            return cursor.rowcount > 0

    def record_dead_letter(self, letter: DeadLetter) -> bool:
        """Persist an audit record and report whether it was newly discovered."""

        # Truncation lives in the store so no caller can bypass it.
        bounded_letter = replace(
            letter,
            text=letter.text[:DEAD_LETTER_TEXT_LIMIT],
            detail=(
                letter.detail[:DEAD_LETTER_DETAIL_LIMIT]
                if letter.detail is not None
                else None
            ),
        )
        with self._transaction() as db:
            # Upsert keyed by native message id: re-processing the same lost
            # message refreshes the record instead of growing the trail.
            created = (
                db.execute(
                    "SELECT 1 FROM dead_letters"
                    " WHERE adapter = ? AND message_id = ?",
                    (self.adapter, bounded_letter.message_id),
                ).fetchone()
                is None
            )
            db.execute(
                """INSERT INTO dead_letters(
                       adapter, message_id, event_id, chat_id, chat_type,
                       conversation_id, sender_id, sender_type, text, reason,
                       created_at, detail, create_time, reply_to, message_type
                   ) VALUES (
                       :adapter, :message_id, :event_id, :chat_id, :chat_type,
                       :conversation_id, :sender_id, :sender_type, :text,
                       :reason, :created_at, :detail, :create_time, :reply_to,
                       :message_type
                   )
                   ON CONFLICT(adapter, message_id) DO UPDATE SET
                       event_id = excluded.event_id,
                       chat_id = excluded.chat_id,
                       chat_type = excluded.chat_type,
                       conversation_id = excluded.conversation_id,
                       sender_id = excluded.sender_id,
                       sender_type = excluded.sender_type,
                       text = excluded.text,
                       reason = excluded.reason,
                       created_at = excluded.created_at,
                       detail = excluded.detail,
                       create_time = excluded.create_time,
                       reply_to = excluded.reply_to,
                       message_type = excluded.message_type""",
                {**asdict(bounded_letter), "adapter": self.adapter},
            )
            self._prune(db, "dead_letters", MAX_DEAD_LETTERS)
            return created

    def dead_letters(
        self,
        *,
        reason: str | None = None,
        chat_id: str | None = None,
        since: str | None = None,
    ) -> tuple[DeadLetter, ...]:
        """The audit trail, optionally filtered on its indexed columns.

        ``since`` compares ``created_at`` lexicographically, which is
        chronological for the ISO-8601 UTC timestamps the adapter writes.
        """

        query = [
            f"SELECT {_DEAD_LETTER_COLUMNS} FROM dead_letters WHERE adapter = ?"
        ]
        params: list[str] = [self.adapter]
        if reason is not None:
            query.append("AND reason = ?")
            params.append(reason)
        if chat_id is not None:
            query.append("AND chat_id = ?")
            params.append(chat_id)
        if since is not None:
            query.append("AND created_at >= ?")
            params.append(since)
        query.append("ORDER BY rowid")
        with self._lock:
            rows = self._db.execute(" ".join(query), params).fetchall()
        return tuple(DeadLetter(**dict(row)) for row in rows)

    def dead_letter_summary(self, *, since: str) -> tuple[int, dict[str, int]]:
        """Return a body-free recent count grouped by machine reason."""

        with self._lock:
            rows = self._db.execute(
                """SELECT reason, COUNT(*) AS count
                   FROM dead_letters
                   WHERE adapter = ? AND created_at >= ?
                   GROUP BY reason
                   ORDER BY reason""",
                (self.adapter, since),
            ).fetchall()
        reasons = {str(row["reason"]): int(row["count"]) for row in rows}
        return sum(reasons.values()), reasons

    def claim_dead_letter_alert(self, *, window_start: str, claimed_at: str) -> bool:
        """Atomically claim one adapter/window notification across processes."""

        with self._transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO dead_letter_alerts(
                       adapter, window_start, claimed_at
                   ) VALUES (?, ?, ?)""",
                (self.adapter, window_start, claimed_at),
            )
            # The table is a dedupe ledger, not an audit trail.  Keep a bounded
            # number of newest claims per adapter without wall-clock guesses.
            db.execute(
                """DELETE FROM dead_letter_alerts
                   WHERE adapter = ? AND rowid NOT IN (
                       SELECT rowid FROM dead_letter_alerts
                       WHERE adapter = ?
                       ORDER BY window_start DESC
                       LIMIT 48
                   )""",
                (self.adapter, self.adapter),
            )
            return cursor.rowcount == 1

    def release_dead_letter_alert(self, *, window_start: str) -> None:
        """Release an unsent claim so a later write may retry the notification."""

        with self._transaction() as db:
            db.execute(
                """DELETE FROM dead_letter_alerts
                   WHERE adapter = ? AND window_start = ?""",
                (self.adapter, window_start),
            )

    def claim_alarm(
        self,
        conversation_id: str,
        reason: str,
        window_start_ms: int,
        claimed_at_ms: int,
    ) -> bool:
        """Atomically claim one conversation/reason alarm window."""

        with self._transaction() as db:
            cursor = db.execute(
                """INSERT OR IGNORE INTO alarm_throttle(
                       adapter, conversation_id, reason, window_start_ms,
                       claimed_at_ms
                   ) VALUES (?, ?, ?, ?, ?)""",
                (
                    self.adapter,
                    conversation_id,
                    reason,
                    window_start_ms,
                    claimed_at_ms,
                ),
            )
            db.execute(
                """DELETE FROM alarm_throttle
                   WHERE adapter = ? AND window_start_ms < ?""",
                (self.adapter, window_start_ms - 3_600_000),
            )
            return cursor.rowcount == 1

    def clear_dead_letter(self, message_id: str) -> None:
        with self._transaction() as db:
            self._clear_dead_letter(db, message_id)

    def record_inbound(
        self,
        correlation: RequestCorrelation,
        *,
        event_id: str,
    ) -> None:
        with self._transaction() as db:
            db.execute(
                """INSERT INTO request_correlations(
                       adapter, harness_message_id, message_id, chat_id,
                       conversation_id
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(adapter, harness_message_id) DO UPDATE SET
                       message_id = excluded.message_id,
                       chat_id = excluded.chat_id,
                       conversation_id = excluded.conversation_id""",
                (
                    self.adapter,
                    correlation.harness_message_id,
                    correlation.message_id,
                    correlation.chat_id,
                    correlation.conversation_id,
                ),
            )
            self._prune(db, "request_correlations", MAX_CORRELATIONS)
            self._mark_seen_event(db, event_id)
            # The native id is the cross-path idempotency key: once Harness
            # accepted custody, no replay of this message may re-forward.
            self._mark_seen_message(db, correlation.message_id)
            # A successful forward resolves any earlier dead letter for the
            # same native message.
            self._clear_dead_letter(db, correlation.message_id)

    def record_reply(self, route: ReplyRoute) -> None:
        with self._transaction() as db:
            db.execute(
                """INSERT INTO reply_routes(
                       adapter, message_id, actor_id, actor_key,
                       harness_message_id, conversation_id, chat_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(adapter, message_id) DO UPDATE SET
                       actor_id = excluded.actor_id,
                       actor_key = excluded.actor_key,
                       harness_message_id = excluded.harness_message_id,
                       conversation_id = excluded.conversation_id,
                       chat_id = excluded.chat_id""",
                (
                    self.adapter,
                    route.message_id,
                    route.actor_id,
                    route.actor_key,
                    route.harness_message_id,
                    route.conversation_id,
                    route.chat_id,
                ),
            )
            self._prune(db, "reply_routes", MAX_CORRELATIONS)

    def record_seen(self, event_id: str) -> None:
        with self._transaction() as db:
            self._mark_seen_event(db, event_id)

    def record_consumed(self, event_id: str, message_id: str) -> None:
        """Mark a Lark-local command consumed across live and replay paths."""

        with self._transaction() as db:
            self._mark_seen_event(db, event_id)
            self._mark_seen_message(db, message_id)
            self._clear_dead_letter(db, message_id)

    def _pending_rows(self, db: sqlite3.Connection) -> dict[str, Any]:
        rows = db.execute(
            f"SELECT {_PENDING_COLUMNS} FROM pending_command_responses"
            " WHERE adapter = ? ORDER BY rowid",
            (self.adapter,),
        ).fetchall()
        return {row["message_id"]: dict(row) for row in rows}

    def pending_command_response(
        self, message_id: str
    ) -> PendingCommandResponse | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_PENDING_COLUMNS} FROM pending_command_responses"
                " WHERE adapter = ? AND message_id = ?",
                (self.adapter, message_id),
            ).fetchone()
        if row is None:
            return None
        # Route the row through the shared parser so corrupt persisted
        # material fails closed exactly like it did at JSON load time.
        return _pending_responses({message_id: dict(row)})[message_id]

    def pending_command_capacity(self) -> PendingCommandCapacityStatus:
        with self._lock:
            return _pending_capacity(self._pending_rows(self._db))

    def record_pending_command_response(
        self, response: PendingCommandResponse
    ) -> PendingCommandResponse:
        """Persist once and return the immutable response owned by message id."""

        with self._transaction() as db:
            current = self._pending_rows(db)
            raw = current.get(response.message_id)
            if raw is not None:
                return PendingCommandResponse(**raw)
            # Validate and measure the candidate map before writing.  This
            # runs inside ``BEGIN IMMEDIATE``, so concurrent writers cannot
            # both observe the last free slot/byte budget.
            candidate = dict(current)
            candidate[response.message_id] = asdict(response)
            capacity = _pending_capacity(candidate)
            if (
                capacity.count > capacity.max_count
                or capacity.bytes > capacity.max_bytes
            ):
                raise PendingCommandCapacityError(
                    replace(_pending_capacity(current), status="full")
                )
            db.execute(
                "INSERT INTO pending_command_responses(adapter, message_id,"
                " event_id, kind, text, idempotency_key)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.adapter,
                    response.message_id,
                    response.event_id,
                    response.kind,
                    response.text,
                    response.idempotency_key,
                ),
            )
            return response

    def complete_command_response(
        self,
        response: PendingCommandResponse,
        *,
        delivered_event_id: str,
    ) -> None:
        """Atomically retire pending response and consume every delivery id."""

        with self._transaction() as db:
            row = db.execute(
                f"SELECT {_PENDING_COLUMNS} FROM pending_command_responses"
                " WHERE adapter = ? AND message_id = ?",
                (self.adapter, response.message_id),
            ).fetchone()
            if row is None or PendingCommandResponse(**dict(row)) != response:
                raise ValueError(
                    "pending Lark command response changed before completion"
                )
            db.execute(
                "DELETE FROM pending_command_responses"
                " WHERE adapter = ? AND message_id = ?",
                (self.adapter, response.message_id),
            )
            self._mark_seen_event(db, response.event_id)
            self._mark_seen_event(db, delivered_event_id)
            self._mark_seen_message(db, response.message_id)
            self._clear_dead_letter(db, response.message_id)

    def conversation_timezone(self, conversation_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT timezone FROM conversation_timezones"
                " WHERE adapter = ? AND conversation_id = ?",
                (self.adapter, conversation_id),
            ).fetchone()
        if row is None:
            return None
        value = row["timezone"]
        return value if isinstance(value, str) and value else None

    def set_conversation_timezone(
        self, conversation_id: str, timezone: str | None
    ) -> None:
        with self._transaction() as db:
            if timezone is None:
                db.execute(
                    "DELETE FROM conversation_timezones"
                    " WHERE adapter = ? AND conversation_id = ?",
                    (self.adapter, conversation_id),
                )
            else:
                db.execute(
                    """INSERT INTO conversation_timezones(
                           adapter, conversation_id, timezone
                       ) VALUES (?, ?, ?)
                       ON CONFLICT(adapter, conversation_id)
                       DO UPDATE SET timezone = excluded.timezone""",
                    (self.adapter, conversation_id, timezone),
                )
                self._prune(db, "conversation_timezones", MAX_CORRELATIONS)

    def observe_identity(
        self,
        kind: str,
        platform_id: str,
        *,
        display_name: str | None = None,
        union_id: str | None = None,
        source: str,
        now_ms: int | None = None,
    ) -> None:
        """Record a mechanically observed identity, idempotently.

        A repeat observation refreshes ``last_seen_ms`` (and the display name
        and union_id when the observation carries them) but never moves
        ``first_seen_ms``.  A ``verified`` row keeps its standing, owner and
        source: observations may only add platform facts, never overwrite a
        human confirmation.
        """

        _checked_identity(
            Identity(kind=kind, platform_id=platform_id, source=source)
        )
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        with self._transaction() as db:
            db.execute(
                """INSERT INTO identities(
                       adapter, kind, platform_id, display_name, union_id,
                       hyprial_owner, standing, source, first_seen_ms,
                       last_seen_ms
                   ) VALUES (?, ?, ?, ?, ?, NULL, 'observed', ?, ?, ?)
                   ON CONFLICT(adapter, kind, platform_id) DO UPDATE SET
                       display_name = COALESCE(
                           NULLIF(excluded.display_name, ''),
                           identities.display_name
                       ),
                       union_id = COALESCE(
                           NULLIF(excluded.union_id, ''),
                           identities.union_id
                       ),
                       source = CASE
                           WHEN identities.standing = 'verified'
                           THEN identities.source
                           ELSE excluded.source
                       END,
                       first_seen_ms = COALESCE(
                           identities.first_seen_ms, excluded.first_seen_ms
                       ),
                       last_seen_ms = excluded.last_seen_ms""",
                (
                    self.adapter,
                    kind,
                    platform_id,
                    display_name,
                    union_id,
                    source,
                    now_ms,
                    now_ms,
                ),
            )

    def upsert_identity(
        self, identity: Identity, *, now_ms: int | None = None
    ) -> Identity:
        """Authoritatively write one identity mapping (operator surface).

        Unlike :meth:`observe_identity` this overwrites display name, owner,
        standing and source -- it is the only path that may set or revoke
        ``verified``.  ``first_seen_ms`` is preserved for an existing row
        unless the caller supplies one explicitly.
        """

        _checked_identity(identity)
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        first_seen = (
            identity.first_seen_ms
            if identity.first_seen_ms is not None
            else now_ms
        )
        last_seen = (
            identity.last_seen_ms
            if identity.last_seen_ms is not None
            else now_ms
        )
        explicit_first_seen = identity.first_seen_ms is not None
        with self._transaction() as db:
            db.execute(
                """INSERT INTO identities(
                       adapter, kind, platform_id, display_name, union_id,
                       hyprial_owner, standing, source, first_seen_ms,
                       last_seen_ms
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(adapter, kind, platform_id) DO UPDATE SET
                       display_name = excluded.display_name,
                       union_id = COALESCE(
                           excluded.union_id, identities.union_id
                       ),
                       hyprial_owner = excluded.hyprial_owner,
                       standing = excluded.standing,
                       source = excluded.source,
                       first_seen_ms = CASE
                           WHEN ? THEN excluded.first_seen_ms
                           ELSE COALESCE(
                               identities.first_seen_ms, excluded.first_seen_ms
                           )
                       END,
                       last_seen_ms = excluded.last_seen_ms""",
                (
                    self.adapter,
                    identity.kind,
                    identity.platform_id,
                    identity.display_name,
                    identity.union_id,
                    identity.hyprial_owner,
                    identity.standing,
                    identity.source,
                    first_seen,
                    last_seen,
                    explicit_first_seen,
                ),
            )
            row = db.execute(
                f"SELECT {_IDENTITY_COLUMNS} FROM identities"
                " WHERE adapter = ? AND kind = ? AND platform_id = ?",
                (self.adapter, identity.kind, identity.platform_id),
            ).fetchone()
        return Identity(**dict(row))

    def identity(self, kind: str, platform_id: str) -> Identity | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT {_IDENTITY_COLUMNS} FROM identities"
                " WHERE adapter = ? AND kind = ? AND platform_id = ?",
                (self.adapter, kind, platform_id),
            ).fetchone()
        return None if row is None else Identity(**dict(row))

    def identities(self, *, kind: str | None = None) -> tuple[Identity, ...]:
        query = [
            f"SELECT {_IDENTITY_COLUMNS} FROM identities WHERE adapter = ?"
        ]
        params: list[str] = [self.adapter]
        if kind is not None:
            query.append("AND kind = ?")
            params.append(kind)
        query.append("ORDER BY kind, platform_id")
        with self._lock:
            rows = self._db.execute(" ".join(query), params).fetchall()
        return tuple(Identity(**dict(row)) for row in rows)

    def find_identities(
        self,
        *,
        name: str | None = None,
        platform_id: str | None = None,
    ) -> tuple[Identity, ...]:
        """Search by display-name substring and/or exact platform id."""

        query = [
            f"SELECT {_IDENTITY_COLUMNS} FROM identities WHERE adapter = ?"
        ]
        params: list[str] = [self.adapter]
        if name is not None:
            query.append(r"AND display_name LIKE ? ESCAPE '\'")
            params.append(f"%{_escape_like(name)}%")
        if platform_id is not None:
            query.append("AND platform_id = ?")
            params.append(platform_id)
        query.append("ORDER BY kind, platform_id")
        with self._lock:
            rows = self._db.execute(" ".join(query), params).fetchall()
        return tuple(Identity(**dict(row)) for row in rows)

    def close(self) -> None:
        with self._lock:
            self._db.close()
