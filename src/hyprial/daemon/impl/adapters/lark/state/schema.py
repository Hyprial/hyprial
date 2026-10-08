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

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace

from hyprial.daemon.impl.adapters.lark.state.readers import retire_legacy_state
from hyprial.daemon.impl.adapters.lark.state.records import (
    IDENTITIES_SCHEMA_VERSION,
    MAX_CORRELATIONS,
    PendingCommandCapacityError,
    STATE_SCHEMA_VERSION,
    _BUSY_TIMEOUT_MS,
    _SQLITE_MAGIC,
)
class _LarkStateSchema:
    def _initialize(self) -> None:
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        self._switch_to_wal()
        self._db.execute("PRAGMA synchronous=NORMAL")
        # No foreign keys in this schema today; enabled anyway so both host
        # databases (agents.sqlite3, adapters.sqlite3) run one convention.
        self._db.execute("PRAGMA foreign_keys=ON")
        self._create_schema()
        self._migrate_pending_inbound_notice_claim()
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

                CREATE TABLE IF NOT EXISTS pending_inbound_submissions (
                    adapter TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    request TEXT NOT NULL,
                    notice_claimed INTEGER NOT NULL DEFAULT 1 CHECK (notice_claimed IN (0, 1)),
                    PRIMARY KEY (adapter, message_id)
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

    def _migrate_pending_inbound_notice_claim(self) -> None:
        """Serialize check/ALTER across adapter processes sharing this file.

        Old rows have no reliable notification history (audit can be evicted).
        Treat them as claimed rather than risk repeating a native warning.
        New writers explicitly insert zero; unknown legacy writers default to
        claimed as well. The flag is fixed metadata, not request payload bytes.
        """
        with self._transaction() as db:
            columns = {str(row["name"]) for row in db.execute(
                "PRAGMA table_info(pending_inbound_submissions)"
            )}
            if "notice_claimed" not in columns:
                db.execute(
                    "ALTER TABLE pending_inbound_submissions ADD COLUMN"
                    " notice_claimed INTEGER NOT NULL DEFAULT 1 CHECK (notice_claimed IN (0, 1))"
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
