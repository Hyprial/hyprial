"""Field-level SQLite store of the desired-state document (U0a-1 -> U0a-2).

U0a-1 mirrored the JSON document into these tables; U0a-2 makes the SQLite
write the commit point: ``DesiredStateStore.save`` writes the tables in ONE
transaction on the shared ``StateDatabase`` (same file, same connection
family as the lifecycle journal) and only then writes the JSON file as a
post-commit projection.  ``read_document`` is the reverse mapping that lets
``load`` read the state back from these tables (the JSON file is the
fallback for homes not yet written by this build).

The mapping consumes and produces the exact ``to_json()`` payload, so the
tables mirror the file rather than a parallel interpretation of the
dataclasses.  Column names follow the JSON keys in snake_case with one
deliberate divergence: the model-vendor column is ``model_vendor`` (JSON
key ``modelProvider``) because the repo-wide terminology lint reserves the
lower-case wire word for the harness-valued key.

Conventions follow ``daemon._LifecycleStore``: WAL, ``synchronous=NORMAL``,
and a 0600 database file, all owned by the shared ``StateDatabase`` (which
also serializes in-process writers and carries the 2s busy timeout for
cross-process contention).  Connections are opened per transaction/read and
closed immediately: the suite enforces strict per-test fd hygiene
(tests/conftest.py ``_fd_hygiene``), and desired-state writes are
low-frequency user operations.

Every save performs a full replace (one transaction of DELETE + INSERT over
the whole document).  Desired state is small -- dozens of rows -- so the
cost is negligible and no incremental-diff logic can drift from the JSON
semantics.  The full-replace strategy is also what keeps rowid order equal
to document order, which ``read_document`` relies on; switching to an
in-transaction upsert would silently break that and must not happen without
deciding about an explicit ``ordinal`` column (the round-trip test is the
tripwire).  The schema is field-level on purpose: dropping a single column
value must be observable by the reconciliation tests.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .state_db import StateDatabase

SHADOW_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS desired_state_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS root_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL,
    as_mailbox INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS harnesses (
    harness TEXT NOT NULL,
    name TEXT NOT NULL,
    headless INTEGER NOT NULL,
    args_json TEXT NOT NULL,
    ownership TEXT NOT NULL,
    nickname TEXT,
    cwd TEXT,
    endpoint TEXT,
    session_ref TEXT,
    command_json TEXT NOT NULL,
    turn_timeout_seconds REAL,
    idle_timeout_seconds REAL,
    containerized INTEGER NOT NULL,
    pinned_owner TEXT,
    container_image TEXT,
    model_vendor TEXT,
    model TEXT,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'failed')),
    PRIMARY KEY (harness, name)
);
CREATE TABLE IF NOT EXISTS interactive_sessions (
    actor TEXT PRIMARY KEY,
    cwd TEXT NOT NULL,
    command_json TEXT NOT NULL,
    source TEXT NOT NULL,
    session_ref TEXT,
    runtime TEXT,
    channel_confirmed INTEGER NOT NULL,
    channel_build_version TEXT,
    channel_protocol_version INTEGER,
    owner_fence INTEGER,
    channel_lease_digest TEXT,
    tmux_session TEXT,
    process_pid INTEGER,
    process_identity TEXT
);
CREATE TABLE IF NOT EXISTS pending_session_agent_effects (
    effect_id TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    actor TEXT NOT NULL,
    harness TEXT,
    runtime TEXT,
    session_id TEXT
);
CREATE TABLE IF NOT EXISTS lifecycle_resources (
    domain TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    resource_token TEXT NOT NULL,
    active INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (domain, resource_key)
);
CREATE TABLE IF NOT EXISTS lifecycle_receipts (
    domain TEXT NOT NULL,
    attempt_token TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    expected_resource_token TEXT,
    created_by_operation INTEGER NOT NULL,
    changed INTEGER NOT NULL,
    resource_token TEXT NOT NULL,
    retired INTEGER NOT NULL,
    completed INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    correlation_id TEXT,
    generation INTEGER,
    version INTEGER,
    PRIMARY KEY (domain, attempt_token)
);
CREATE TABLE IF NOT EXISTS channel_pins (
    channel TEXT PRIMARY KEY,
    agent TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deprecated_shared_channels (
    channel TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS zenoh_endpoints (
    direction TEXT NOT NULL CHECK (direction IN ('listen', 'connect')),
    ordinal INTEGER NOT NULL,
    endpoint TEXT NOT NULL,
    PRIMARY KEY (direction, ordinal)
);
"""


def _journal_proved_running(db: sqlite3.Connection) -> set[tuple[str, str]]:
    """(harness, name) pairs the durable journal proves were up once.

    ``lifecycle_effects`` is never pruned (⚠️ this rescue DEPENDS on
    that -- see POSITIONAL DEPENDENCY below), and an effect only reaches
    status 'completed' after its step's completion event -- for a forward
    ``harness.ensure`` that is the start-success settlement (the process
    came up, or was already up).  Attribution rides the operation's
    ``request_json``, whose ``target.harness`` is the full launch payload.

    ⚠️ POSITIONAL DEPENDENCY -- if a retention policy ever prunes the
    journal (a TTL, a vacuum, a keep-last-N sweep), this rescue SILENTLY
    stops working: long-running harnesses whose completed history was
    pruned go back to being deleted as ghosts by the migration, and
    nothing else turns red.  Whoever adds journal pruning must come back
    here and decide the migration's evidence story with it;
    tests/test_lifecycle_journal_retention_gate.py is the tripwire that
    goes red the moment a prune of lifecycle_effects/lifecycle_operations
    appears in src/.

    A missing journal (a database older than the journal itself) or an
    unattributable record contributes nothing: absence of evidence must
    not become evidence of absence here -- the caller only uses this set
    to RESCUE rows, never to convict.
    """

    try:
        rows = db.execute(
            "SELECT e.effect_name, e.direction, e.status, o.request_json "
            "FROM lifecycle_effects e "
            "JOIN lifecycle_operations o ON o.operation_id = e.operation_id"
        ).fetchall()
    except sqlite3.OperationalError:
        return set()
    proved: set[tuple[str, str]] = set()
    for effect_name, direction, status, request_json in rows:
        if not str(effect_name).endswith("harness.ensure"):
            continue
        if str(direction) != "forward" or str(status) != "completed":
            continue
        try:
            target = json.loads(request_json)["target"]["harness"]
            proved.add((str(target["provider"]), str(target["name"])))
        except (KeyError, TypeError, ValueError):
            continue
    return proved


def _json_text(value: object) -> str:
    """Canonical compact JSON encoding used for every composite column."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _items(document: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = document.get(key, [])
    return [item for item in value if isinstance(item, dict)]


class DesiredStateSqliteShadow:
    """Field-level SQLite representation of one desired-state document.

    Accepts a ``StateDatabase`` (the shared store connection owner) or a
    bare ``Path`` (wrapped in its own ``StateDatabase``; the standalone-
    test topology where nothing else shares the file).
    """

    def __init__(self, state_db: Path | StateDatabase) -> None:
        self._state_db = (
            state_db if isinstance(state_db, StateDatabase) else StateDatabase(state_db)
        )

    def write_document(self, document: Mapping[str, Any]) -> None:
        """Replace the whole content with ``document`` in ONE transaction.

        This is the commit point of every desired-state mutation (U0a-2):
        failures propagate and leave nothing written -- the JSON projection
        downstream is attempted only after this transaction commits.
        """

        with self._state_db.transaction(schema=_SCHEMA) as db:
            self._delete_all(db)
            self._insert_all(db, document)

    def read_document(self) -> dict[str, Any] | None:
        """Reverse mapping: rebuild the ``to_json()`` document from raw rows.

        U0a-2 counterpart to ``mirror``: reads every field-level table and
        reproduces the exact payload ``save`` wrote (and ``load`` parses), so
        this database -- not the JSON file -- can become the read authority.
        Returns ``None`` when the shadow holds no document yet (the database
        file may exist because ``_LifecycleStore`` created it first, without
        any desired-state row).

        Losslessness is enforced by tests/test_desired_state_sqlite_roundtrip.py:
        it byte-compares the rebuilt document against the authoritative
        ``.v1`` file and pins receipt order.  List order is read
        ``ORDER BY rowid``, which equals the document's order ONLY under the
        full-replace write strategy (one DELETE + INSERT of every row per
        save); the round-trip test is the tripwire that goes red if that
        premise is ever dropped -- decide about an explicit ``ordinal``
        column then, not silently.
        """

        if not self._state_db.exists():
            return None
        with self._state_db.read() as db:
            db.row_factory = sqlite3.Row
            # The file may predate this schema entirely (the lifecycle
            # journal bootstraps lifecycle_* tables first); no desired-state
            # tables means no document yet.
            table = db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'root_state'"
            ).fetchone()
            if table is None:
                return None
            root = db.execute(
                "SELECT schema_version, as_mailbox FROM root_state WHERE id = 1"
            ).fetchone()
            if root is None:
                return None
            document: dict[str, Any] = {
                "schemaVersion": int(root["schema_version"]),
                "asMailbox": bool(root["as_mailbox"]),
                "providers": [
                    self._harness_document(row)
                    for row in db.execute(
                        "SELECT * FROM harnesses ORDER BY rowid"
                    )
                ],
                "interactiveSessions": [
                    self._session_document(row)
                    for row in db.execute(
                        "SELECT * FROM interactive_sessions ORDER BY rowid"
                    )
                ],
            }
            effects = [
                {
                    "effectId": row["effect_id"],
                    "correlationId": row["correlation_id"],
                    "operation": row["operation"],
                    "actor": row["actor"],
                    **(
                        {"harness": row["harness"]}
                        if row["harness"] is not None
                        else {}
                    ),
                    **(
                        {"runtime": row["runtime"]}
                        if row["runtime"] is not None
                        else {}
                    ),
                    **(
                        {"sessionId": row["session_id"]}
                        if row["session_id"] is not None
                        else {}
                    ),
                }
                for row in db.execute(
                    "SELECT * FROM pending_session_agent_effects ORDER BY rowid"
                )
            ]
            if effects:
                document["pendingSessionAgentEffects"] = effects
            resources = [
                {
                    "domain": row["domain"],
                    "resourceKey": row["resource_key"],
                    "resourceToken": row["resource_token"],
                    "active": bool(row["active"]),
                    "payload": json.loads(row["payload_json"]),
                }
                for row in db.execute(
                    "SELECT * FROM lifecycle_resources ORDER BY rowid"
                )
            ]
            if resources:
                document["lifecycleResources"] = resources
            receipts = [
                self._receipt_document(row)
                for row in db.execute(
                    "SELECT * FROM lifecycle_receipts ORDER BY rowid"
                )
            ]
            if receipts:
                document["lifecycleReceipts"] = receipts
            document["channelPins"] = {
                row["channel"]: row["agent"]
                for row in db.execute(
                    "SELECT channel, agent FROM channel_pins ORDER BY rowid"
                )
            }
            document["deprecatedSharedChannels"] = [
                row["channel"]
                for row in db.execute(
                    "SELECT channel FROM deprecated_shared_channels ORDER BY rowid"
                )
            ]
            zenoh: dict[str, list[str]] = {"listen": [], "connect": []}
            for row in db.execute(
                "SELECT direction, endpoint FROM zenoh_endpoints ORDER BY rowid"
            ):
                zenoh[row["direction"]].append(row["endpoint"])
            document["zenoh"] = zenoh
            return document

    def migrate_harness_status(self) -> None:
        """U0b column migration + one-shot ghost-row resolution.

        Adds ``harnesses.status`` to databases written before U0b and, in
        the SAME transaction, resolves rows whose last lifecycle attempt
        never settled.  The rules (hq-adjutant 2026-09-04, evidence over
        defaults -- see the U0b task spec):

        * a row whose resource key has an INCOMPLETE harness receipt and no
          completed receipt was frozen mid-start by a daemon crash.  That
          alone is not yet a verdict, because receipts are TRANSIENT:
          ``_retire_completed_receipt`` deletes them after every successful
          saga step, so a harness that ran for weeks has none.  The durable
          success evidence is the journal (``lifecycle_effects`` is never
          pruned): a completed forward ``harness.ensure`` effect attributed
          to the row proves it was actually up at some point.  So the row is
          deleted (never confirmed succeeded -> no trace, per Allen's
          semantics) only when NEITHER source carries success evidence.
          Journal evidence only RESCUES: a row the journal cannot acquit
          but receipts cannot convict either keeps today's disposition
          (running) -- convicting on absent journal history is deliberately
          not done (staged rows, pre-journal homes).
        * Lark rows are never deleted here: they are adapter registration
          config written by ``upsert`` (no lifecycle, no journal), not start
          traces.
        * every other row (no receipts at all -- the retired steady state
          -- or a completed receipt, or rescued by the journal) backfills
          to 'running'.

        Exactly-once by construction: the column's presence is the
        sentinel.  Callers must guard on ``state_db.exists()`` so read-only
        stores never create the file.

        Why the agents registry is NOT consulted as success evidence
        (checked 2026-09-04 against h2b-developer's counter-example; both
        directions answered per candidate source): an agents row does not
        imply its harness ever started.  (1) Rows are created by ONE
        deliberately shared path -- the ``agent.create`` IPC, which
        ``hyprial start`` calls FIRST ("a connector can never exist without an
        identity") and which also creates rows for agents whose harness
        never started; the schema (agents.registry) has no source/creator
        column, so "who built this row" is not recoverable.  (2) created_at
        ordering cannot separate the flows: the row precedes the harness
        step in both (IPC create-then-launch; saga ``agent.create`` step 1
        vs ``harness.ensure`` step 4).  (3) Connector self-registration
        (``DaemonEventBridge._reconcile_harness_actors``) holds only
        in-memory zenoh liveliness endpoints -- nothing durable exists by
        migration time.  (4) The row's later fields are written by
        ``agent.bind``, which runs BEFORE ``harness.ensure`` in
        ``_create_steps``, carrying the spec's pre-spawn session_ref --
        existing does not imply the harness came up.  Indistinguishable =>
        not consulted, not hard-judged; the residual blind spot (a
        long-running harness whose only successes predate the journal,
        then a restart that crashes between commit and confirm, falls to
        the delete rule and loses the intent -- recovery is a fresh
        ``hyprial start``) is recorded as a known limitation in the PR
        description.
        """

        if not self._state_db.exists():
            return
        with self._state_db.read() as db:
            table = db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'harnesses'"
            ).fetchone()
            if table is None:
                return
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(harnesses)")
            }
        if "status" in columns:
            return
        with self._state_db.transaction(schema=_SCHEMA) as db:
            # Re-check under the write lock: another process may have run
            # the migration between our read and this transaction.
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(harnesses)")
            }
            if "status" in columns:
                return
            db.execute(
                "ALTER TABLE harnesses ADD COLUMN status TEXT NOT NULL "
                "DEFAULT 'running' "
                "CHECK (status IN ('running', 'failed'))"
            )
            proved_running = _journal_proved_running(db)
            candidates = db.execute(
                "SELECT harness, name FROM harnesses WHERE "
                "NOT EXISTS ("
                "  SELECT 1 FROM lifecycle_receipts r "
                "  WHERE r.domain = 'harness' "
                "  AND r.resource_key = "
                "      'harness:' || harnesses.harness || ':' || harnesses.name "
                "  AND r.completed = 1) "
                "AND EXISTS ("
                "  SELECT 1 FROM lifecycle_receipts r "
                "  WHERE r.domain = 'harness' "
                "  AND r.resource_key = "
                "      'harness:' || harnesses.harness || ':' || harnesses.name "
                "  AND r.completed = 0)"
            ).fetchall()
            ghosts = [
                (harness, name)
                for harness, name in candidates
                if str(harness) != "lark"
                and (str(harness), str(name)) not in proved_running
            ]
            db.executemany(
                "DELETE FROM harnesses WHERE harness = ? AND name = ?", ghosts
            )

    def migrate_interactive_process_identity(self) -> None:
        """Add the optional PID/birth fence without judging legacy sessions."""

        if not self._state_db.exists():
            return
        with self._state_db.read() as db:
            table = db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'interactive_sessions'"
            ).fetchone()
            if table is None:
                return
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(interactive_sessions)")
            }
        if {"process_pid", "process_identity"}.issubset(columns):
            return
        with self._state_db.transaction(schema=_SCHEMA) as db:
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(interactive_sessions)")
            }
            for name, definition in (
                ("process_pid", "INTEGER"),
                ("process_identity", "TEXT"),
            ):
                if name not in columns:
                    db.execute(
                        f"ALTER TABLE interactive_sessions ADD COLUMN {name} {definition}"
                    )

    @staticmethod
    def _harness_document(row: sqlite3.Row) -> dict[str, Any]:
        command = json.loads(row["command_json"])
        return {
            "provider": row["harness"],
            "name": row["name"],
            "headless": bool(row["headless"]),
            "args": json.loads(row["args_json"]),
            "ownership": row["ownership"],
            **(
                {"nickname": row["nickname"]}
                if row["nickname"] is not None
                else {}
            ),
            **({"cwd": row["cwd"]} if row["cwd"] is not None else {}),
            **(
                {"endpoint": row["endpoint"]}
                if row["endpoint"] is not None
                else {}
            ),
            **(
                {"sessionRef": row["session_ref"]}
                if row["session_ref"] is not None
                else {}
            ),
            **({"command": command} if command else {}),
            **(
                {"turnTimeoutSeconds": row["turn_timeout_seconds"]}
                if row["turn_timeout_seconds"] is not None
                else {}
            ),
            **(
                {"idleTimeoutSeconds": row["idle_timeout_seconds"]}
                if row["idle_timeout_seconds"] is not None
                else {}
            ),
            **({"containerized": True} if row["containerized"] else {}),
            **(
                {"pinnedOwner": row["pinned_owner"]}
                if row["pinned_owner"] is not None
                else {}
            ),
            **(
                {"containerImage": row["container_image"]}
                if row["container_image"] is not None
                else {}
            ),
            **(
                {"modelProvider": row["model_vendor"]}
                if row["model_vendor"] is not None
                else {}
            ),
            **({"model": row["model"]} if row["model"] is not None else {}),
            # U0b: absent == running in the document form; the column is
            # NOT NULL, so pre-U0b rows take 'running' from the table
            # default during the ALTER migration.
            **(
                {"status": row["status"]}
                if row["status"] is not None and row["status"] != "running"
                else {}
            ),
        }

    @staticmethod
    def _session_document(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "actor": row["actor"],
            "cwd": row["cwd"],
            "command": json.loads(row["command_json"]),
            "source": row["source"],
            **(
                {"sessionRef": row["session_ref"]}
                if row["session_ref"] is not None
                else {}
            ),
            **(
                {"runtime": row["runtime"]}
                if row["runtime"] is not None
                else {}
            ),
            **(
                {"channelConfirmed": True} if row["channel_confirmed"] else {}
            ),
            **(
                {"channelBuildVersion": row["channel_build_version"]}
                if row["channel_build_version"] is not None
                else {}
            ),
            **(
                {"channelProtocolVersion": row["channel_protocol_version"]}
                if row["channel_protocol_version"] is not None
                else {}
            ),
            **(
                {"ownerFence": bool(row["owner_fence"])}
                if row["owner_fence"] is not None
                else {}
            ),
            **(
                {"channelLeaseDigest": row["channel_lease_digest"]}
                if row["channel_lease_digest"] is not None
                else {}
            ),
            **(
                {"tmuxSession": row["tmux_session"]}
                if row["tmux_session"] is not None
                else {}
            ),
            **(
                {"processPid": row["process_pid"]}
                if row["process_pid"] is not None
                else {}
            ),
            **(
                {"processIdentity": row["process_identity"]}
                if row["process_identity"] is not None
                else {}
            ),
        }

    @staticmethod
    def _receipt_document(row: sqlite3.Row) -> dict[str, Any]:
        # Mirrors StoredLifecycleReceipt.to_json(): expectedResourceToken is
        # written UNCONDITIONALLY (null when None); correlationId, generation
        # and version only when not None.
        return {
            "domain": row["domain"],
            "attemptToken": row["attempt_token"],
            "operationId": row["operation_id"],
            "resourceKey": row["resource_key"],
            "expectedResourceToken": row["expected_resource_token"],
            "createdByOperation": bool(row["created_by_operation"]),
            "changed": bool(row["changed"]),
            "resourceToken": row["resource_token"],
            "retired": bool(row["retired"]),
            "completed": bool(row["completed"]),
            "attempts": int(row["attempts"]),
            **(
                {"correlationId": row["correlation_id"]}
                if row["correlation_id"] is not None
                else {}
            ),
            **(
                {"generation": int(row["generation"])}
                if row["generation"] is not None
                else {}
            ),
            **(
                {"version": int(row["version"])}
                if row["version"] is not None
                else {}
            ),
        }

    @staticmethod
    def _delete_all(db: sqlite3.Connection) -> None:
        for table in (
            "desired_state_meta",
            "root_state",
            "harnesses",
            "interactive_sessions",
            "pending_session_agent_effects",
            "lifecycle_resources",
            "lifecycle_receipts",
            "channel_pins",
            "deprecated_shared_channels",
            "zenoh_endpoints",
        ):
            db.execute(f'DELETE FROM "{table}"')

    def _insert_all(
        self, db: sqlite3.Connection, document: Mapping[str, Any]
    ) -> None:
        zenoh = document.get("zenoh")
        zenoh_record = zenoh if isinstance(zenoh, dict) else {}
        db.execute(
            "INSERT INTO root_state(id, schema_version, as_mailbox) "
            "VALUES(1, ?, ?)",
            (
                document["schemaVersion"],
                int(bool(document.get("asMailbox", False))),
            ),
        )
        db.executemany(
            "INSERT INTO harnesses("
            "harness, name, headless, args_json, ownership, nickname, cwd, "
            "endpoint, session_ref, command_json, turn_timeout_seconds, "
            "idle_timeout_seconds, containerized, pinned_owner, "
            "container_image, model_vendor, model, status) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    spec["provider"],
                    spec["name"],
                    int(bool(spec["headless"])),
                    _json_text(spec.get("args", [])),
                    spec["ownership"],
                    spec.get("nickname"),
                    spec.get("cwd"),
                    spec.get("endpoint"),
                    spec.get("sessionRef"),
                    _json_text(spec.get("command", [])),
                    spec.get("turnTimeoutSeconds"),
                    spec.get("idleTimeoutSeconds"),
                    int(bool(spec.get("containerized", False))),
                    spec.get("pinnedOwner"),
                    spec.get("containerImage"),
                    spec.get("modelProvider"),
                    spec.get("model"),
                    spec.get("status", "running"),
                )
                for spec in _items(document, "providers")
            ],
        )
        db.executemany(
            "INSERT INTO interactive_sessions("
            "actor, cwd, command_json, source, session_ref, runtime, "
            "channel_confirmed, channel_build_version, "
            "channel_protocol_version, owner_fence, channel_lease_digest, "
            "tmux_session, process_pid, process_identity) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    session["actor"],
                    session["cwd"],
                    _json_text(session["command"]),
                    session["source"],
                    session.get("sessionRef"),
                    session.get("runtime"),
                    int(bool(session.get("channelConfirmed", False))),
                    session.get("channelBuildVersion"),
                    session.get("channelProtocolVersion"),
                    session.get("ownerFence"),
                    session.get("channelLeaseDigest"),
                    session.get("tmuxSession"),
                    session.get("processPid"),
                    session.get("processIdentity"),
                )
                for session in _items(document, "interactiveSessions")
            ],
        )
        db.executemany(
            "INSERT INTO pending_session_agent_effects("
            "effect_id, correlation_id, operation, actor, harness, runtime, "
            "session_id) VALUES(?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    effect["effectId"],
                    effect["correlationId"],
                    effect["operation"],
                    effect["actor"],
                    effect.get("harness"),
                    effect.get("runtime"),
                    effect.get("sessionId"),
                )
                for effect in _items(document, "pendingSessionAgentEffects")
            ],
        )
        db.executemany(
            "INSERT INTO lifecycle_resources("
            "domain, resource_key, resource_token, active, payload_json) "
            "VALUES(?, ?, ?, ?, ?)",
            [
                (
                    resource["domain"],
                    resource["resourceKey"],
                    resource["resourceToken"],
                    int(bool(resource["active"])),
                    _json_text(resource.get("payload", {})),
                )
                for resource in _items(document, "lifecycleResources")
            ],
        )
        db.executemany(
            "INSERT INTO lifecycle_receipts("
            "domain, attempt_token, operation_id, resource_key, "
            "expected_resource_token, created_by_operation, changed, "
            "resource_token, retired, completed, attempts, correlation_id, "
            "generation, version) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    receipt["domain"],
                    receipt["attemptToken"],
                    receipt["operationId"],
                    receipt["resourceKey"],
                    receipt.get("expectedResourceToken"),
                    int(bool(receipt.get("createdByOperation", False))),
                    int(bool(receipt.get("changed", False))),
                    receipt["resourceToken"],
                    int(bool(receipt.get("retired", False))),
                    int(bool(receipt.get("completed", True))),
                    receipt.get("attempts", 0),
                    receipt.get("correlationId"),
                    receipt.get("generation"),
                    receipt.get("version"),
                )
                for receipt in _items(document, "lifecycleReceipts")
            ],
        )
        channel_pins = document.get("channelPins", {})
        pin_items = (
            channel_pins.items() if isinstance(channel_pins, dict) else []
        )
        db.executemany(
            "INSERT INTO channel_pins(channel, agent) VALUES(?, ?)",
            [(str(channel), str(agent)) for channel, agent in pin_items],
        )
        db.executemany(
            "INSERT INTO deprecated_shared_channels(channel) VALUES(?)",
            [
                (str(channel),)
                for channel in document.get("deprecatedSharedChannels", [])
            ],
        )
        db.executemany(
            "INSERT INTO zenoh_endpoints(direction, ordinal, endpoint) "
            "VALUES(?, ?, ?)",
            [
                (direction, ordinal, str(endpoint))
                for direction in ("listen", "connect")
                for ordinal, endpoint in enumerate(
                    zenoh_record.get(direction, [])
                )
            ],
        )
        db.execute(
            "INSERT INTO desired_state_meta(key, value) VALUES(?, ?)",
            ("shadow_schema_version", str(SHADOW_SCHEMA_VERSION)),
        )
