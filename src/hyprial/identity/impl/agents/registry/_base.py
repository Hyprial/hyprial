from __future__ import annotations

from hyprial.identity.impl.agents.home.config import AgentConfig
from typing import Any
from hyprial.identity.impl.agents.home.effects import HomeFilesystemPlan
from collections.abc import Iterable
from collections.abc import Mapping
from pathlib import Path
from typing import Self
from dataclasses import dataclass
from dataclasses import field
import base64
import hashlib
from hyprial.kernel import CHANNEL_LIVENESS_TTL_SECONDS, ipc_errors
import json
import logging
from hyprial.identity.impl.agents.home.config import normalize_agent_config
import re
import sqlite3
import time
import uuid

"""The Agent entity: one durable record of who an agent is and how it is configured.

Before this module hyprial had an agent's *address* but no agent *identity*: how to
launch it lived in ``HarnessLaunchSpec`` keyed by ``(harness, name)``, its
session binding lived in ``InteractiveSession`` keyed by ``actor``, and the
only proof it ever existed on the network was a URI string in ``users.json``.
Three shapes, three keys, one concept — and because the process layer keyed on
``(harness, name)`` while the network layer keyed on ``actor``, ``claude:foo``
and ``pi:foo`` minted the *same* four-segment URI and silently fought over it.

This module owns the identity.  Storage is one real SQLite database
(``~/.hyprial/state/agents.sqlite3`` — the name says sqlite because it is
sqlite), following the conventions ``inbox.sqlite3`` established
(WAL journal, ``synchronous=NORMAL``, a busy timeout for cross-process
writers, one ``RLock`` in-process).  The invariants live in the schema
instead of in code:

* A1 uniqueness — ``actor`` is the PRIMARY KEY: a duplicate name is an
  ``IntegrityError`` before it is anything else.
* Pin one-to-one — the ``pins`` table carries UNIQUE on both columns:
  one adapter binds one agent, one agent is bound by one adapter.
* destroy erases the pins — ``ON DELETE CASCADE``: deleting the agent row
  and its pins is a single transaction, so no cleanup code can forget.

Deliberately absent from :class:`Agent`:

``harness``
    A harness is a *runtime binding*, not configuration. An agent may start on
    ``claude`` and be swapped to ``pi`` mid-life without touching a byte of its
    record; only ``harness_args`` (how to launch under each harness) and
    ``preferred_harness`` (a default for ``hyprial start``) are persisted here.
``state``
    There is no ``retired`` state. Existing means active; ``destroy`` deletes
    (design §6.3, decision A6) — irreversibly, with no tombstone.
``last_seen_ms``
    Liveness is runtime state for one daemon generation, so it lives in
    :mod:`hyprial.identity.impl.agents.state.liveness`, never on the persisted record.
"""
_LOG = logging.getLogger(__name__)
def _uri() -> Any:
    """Import the dependency-free canonical URI helpers."""

    from hyprial.kernel import uri

    return uri
ACTOR_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_ACTOR_NAME_LENGTH = 128
AGENT_ACTIVITY_WRITE_INTERVAL_MS = 60_000
# A destroyed window retries much more slowly than the six-second liveness
# lease. Keep its terminal fence for 432,000 leases (30 days) so every normal
# client retry horizon is covered without turning the table into a tombstone.
RETIRED_SESSION_REF_RETENTION_MS = int(CHANNEL_LIVENESS_TTL_SECONDS * 1000) * 432_000
# Re-registering an existing ref refreshes one row, so only distinct terminal
# takeovers consume this budget. 256 therefore covers roughly one new terminal
# per working day for a year while keeping every Agent incarnation bounded.
AGENT_SESSION_REF_HISTORY_LIMIT = 256
HOSTED_BY_VALUES = ("transfer-receive", "squire-container", "host-invite")
class AgentError(RuntimeError):
    """Base class for every agent-registry failure, carrying an IPC code."""

    code = "AGENT_ERROR"
class AgentExistsError(AgentError):
    """Decision A1: a second agent may never claim a name already in use."""

    code = ipc_errors.AGENT_EXISTS
class AgentNotFoundError(AgentError):
    code = ipc_errors.AGENT_NOT_FOUND
class AgentDestroySettlementUnknown(AgentError):
    code = "AGENT_DESTROY_SETTLEMENT_UNKNOWN"
class AgentEntityConflict(AgentError):
    code = "AGENT_VERSION_CONFLICT"
class InvalidAgentNameError(AgentError):
    code = "INVALID_AGENT_NAME"
class PinConflictError(AgentError):
    """The pins table's UNIQUE(agent) spoke: one agent, one adapter."""

    code = "PIN_CONFLICT"

    def __init__(self, agent_uri: str, adapter: str, holder: str) -> None:
        super().__init__(
            f"agent {agent_uri} is already pinned by adapter {holder!r}. "
            f"Adapter and agent bind one-to-one: run 'hyprial adapter unpin "
            f"{holder}' first if this pin should move."
        )
        self.agent_uri = agent_uri
        self.adapter = adapter
        self.holder = holder
def _now_ms() -> int:
    return time.time_ns() // 1_000_000
def _is_compensating_attempt(attempt_token: str) -> bool:
    """Whether a lifecycle mutation is a saga's compensation, not a forward step.

    Attempt tokens are ``<operation_id>:<direction>:<ordinal>`` with the
    direction second from the right (operation ids may contain colons, so the
    split is anchored on the right, matching ``backfill_domain_attested_effects``).
    A compensating destroy rolls back a create in the same saga; a forward
    destroy is an operator action.  Only the former may auto-remove a home.
    """

    parts = attempt_token.rsplit(":", 2)
    return len(parts) == 3 and parts[1] == "compensation"
def normalize_capabilities(value: object) -> dict[str, Any]:
    """Validate and detach JSON facts; booleans/null/numbers stay typed."""

    if value is None:
        return {}

    def check(item: object) -> None:
        if item is None or isinstance(item, (str, bool, int, float)):
            return
        if isinstance(item, list):
            for child in item:
                check(child)
            return
        if isinstance(item, Mapping) and all(isinstance(key, str) for key in item):
            for child in item.values():
                check(child)
            return
        raise AgentError("capabilities must contain only JSON values with string keys")

    if not isinstance(value, Mapping):
        raise AgentError("capabilities must be a JSON object")
    check(value)
    try:
        return json.loads(json.dumps(dict(value), sort_keys=True, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise AgentError("capabilities must contain only finite JSON values") from error
def normalize_pinned_adapters(value: object) -> tuple[str, ...]:
    """Normalize the adapters pinned to an agent into a sorted unique tuple.

    Adapter pins live beside the agent rows on purpose: deleting the agent
    cascades the pins away, so no separate store can go stale.  The schema
    enforces one adapter per agent and one agent per adapter; the tuple shape
    merely keeps the served field order-stable.
    """

    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise AgentError("pinnedAdapters must be a sequence of adapter names")
    adapters = tuple(value)
    if any(not isinstance(item, str) or not item for item in adapters):
        raise AgentError("pinnedAdapters must contain non-empty adapter names")
    return tuple(sorted(set(adapters)))
def normalize_harness_args(value: object) -> dict[str, tuple[str, ...]]:
    """Normalize per-harness launch arguments into ``{harness: (arg, ...)}``.

    Same reasoning as :func:`normalize_capabilities`: accepting ``object`` and
    validating here is what lets callers pass any ``Iterable[str]`` (or a JSON
    list) while the stored field stays exactly ``tuple[str, ...]``, with no
    variance escape hatch at any call site.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AgentError("harness_args must be a mapping of harness name to arguments")
    result: dict[str, tuple[str, ...]] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise AgentError("harness_args keys must be non-empty harness names")
        if isinstance(item, str) or not isinstance(item, Iterable):
            raise AgentError(f"harness_args[{key}] must be a sequence of strings")
        args = tuple(item)
        if any(not isinstance(entry, str) for entry in args):
            raise AgentError(f"harness_args[{key}] must contain only strings")
        result[key] = args
    return dict(sorted(result.items()))
@dataclass(frozen=True, slots=True)
class Agent:
    """The durable "who is this agent, how is it configured" record (design §3).

    Contains no runtime state whatsoever: not the harness currently running it,
    not its pid, not whether it is online.  Those belong to the daemon
    generation that owns the binding.
    """

    # -- identity (this class is the sole owner; no other layer composes it) --
    uri: str
    actor: str
    owner: str
    machine: str
    #: Opaque incarnation, regenerated by destructive rebuild/account transfer.
    #: Grants and home receipts bind to this rather than to the reusable name.
    entity_token: str = field(default_factory=lambda: uuid.uuid4().hex)

    # -- configuration independent of any harness --
    cwd: str | None = None
    config: AgentConfig | None = None
    #: Model-vendor preference, the same vocabulary as squire's
    #: ``RuntimeCapability(harness, provider, model)``.
    provider: str | None = None
    model: str | None = None
    capabilities: Mapping[str, Any] = field(default_factory=dict)

    # -- per-harness launch arguments; NOT "which harness is current" --
    harness_args: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: Only a default for ``hyprial start``; any harness may override it.
    preferred_harness: str | None = None

    # -- last session clues, for the A9 handover notice after a harness swap --
    last_harness: str | None = None
    last_session_id: str | None = None
    last_active_at_ms: int | None = None

    # -- adapter pins: which adapters route their inbound DMs to this agent --
    #: Composed from the ``pins`` table at read time; writes go through
    #: ``AgentRegistry.pin``/``unpin`` only (``save`` ignores this field).
    #: Kept on the record (and in its JSON shape) so ``agent get/list``
    #: show the binding.  Besides inbound routing, ``message.send`` uses the
    #: pin to select this local agent's own bot for eligible ``user:`` DMs.
    #: Kept a *list* for shape-compatibility even
    #: though UNIQUE(agent) makes it hold at most one adapter.  Deleting the
    #: agent row cascades the pins away.  It is not a bijection: correlated
    #: replies still use each message's own adapter rather than this pin.
    pinned_adapters: tuple[str, ...] = ()

    # -- lifecycle: existing is active, destroy is deletion (no retired state) --
    created_at_ms: int = 0
    #: Explicit hosting authority, not an inference from the URI's owner.
    hosted_by: str | None = None

    def __post_init__(self) -> None:
        if self.hosted_by not in (None, *HOSTED_BY_VALUES):
            raise AgentError(f"invalid hosting authority: {self.hosted_by!r}")
        object.__setattr__(self, "capabilities", normalize_capabilities(self.capabilities))
        object.__setattr__(self, "config", normalize_agent_config(self.config))
        object.__setattr__(
            self, "harness_args", normalize_harness_args(self.harness_args)
        )
        object.__setattr__(
            self, "pinned_adapters", normalize_pinned_adapters(self.pinned_adapters)
        )

    def args_for(self, harness: str) -> tuple[str, ...]:
        """Launch arguments recorded for ``harness``, empty when unknown."""

        return tuple(self.harness_args.get(harness, ()))

    @property
    def agent_id(self) -> str:
        """Immutable typed id derived from this incarnation's entity token."""

        try:
            raw = bytes.fromhex(self.entity_token)
        except ValueError as error:
            raise AgentError("entity_token must be uuid4 hexadecimal") from error
        if len(raw) != 16:
            raise AgentError("entity_token must be uuid4 hexadecimal")
        digest = hashlib.sha256(b"hyprial-agent-id-v1" + raw).digest()[:16]
        encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
        return f"a.{encoded}"

    def to_json(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "actor": self.actor,
            "owner": self.owner,
            "machine": self.machine,
            "entityToken": self.entity_token,
            "cwd": self.cwd,
            "config": None if self.config is None else self.config.to_json(),
            # Wire/on-disk key for the model vendor, mirroring squire.
            "provider": self.provider,
            "model": self.model,
            "capabilities": dict(self.capabilities),
            "harnessArgs": {
                harness: list(args) for harness, args in self.harness_args.items()
            },
            "preferredHarness": self.preferred_harness,
            "lastHarness": self.last_harness,
            "lastSessionId": self.last_session_id,
            "lastActiveAtMs": self.last_active_at_ms,
            "pinnedAdapters": list(self.pinned_adapters),
            "createdAtMs": self.created_at_ms,
            "hosted": self.hosted_by is not None,
            "hostedBy": self.hosted_by,
        }

    @classmethod
    def from_json(cls, value: object, label: str) -> Self:
        if not isinstance(value, dict):
            raise AgentError(f"{label} must be a JSON object")
        actor = _string(value.get("actor"), f"{label}.actor")
        owner = _string(value.get("owner"), f"{label}.owner")
        machine = _string(value.get("machine"), f"{label}.machine")
        uri = value.get("uri")
        if not isinstance(uri, str) or not uri:
            uri = _uri().canonical_agent_uri(owner, machine, actor)
        raw_args = value.get("harnessArgs") or {}
        if not isinstance(raw_args, dict):
            raise AgentError(f"{label}.harnessArgs must be an object")
        raw_capabilities = value.get("capabilities") or {}
        if not isinstance(raw_capabilities, dict):
            raise AgentError(f"{label}.capabilities must be an object")
        created = value.get("createdAtMs")
        return cls(
            uri=uri,
            actor=actor,
            owner=owner,
            machine=machine,
            entity_token=(
                _optional_string(value.get("entityToken"), f"{label}.entityToken")
                or uuid.uuid4().hex
            ),
            cwd=_optional_string(value.get("cwd"), f"{label}.cwd"),
            config=(
                None
                if value.get("config") is None
                else AgentConfig.from_json(value.get("config"), f"{label}.config")
            ),
            provider=_optional_string(value.get("provider"), f"{label}.provider"),
            model=_optional_string(value.get("model"), f"{label}.model"),
            capabilities=normalize_capabilities(raw_capabilities),
            harness_args=normalize_harness_args(raw_args),
            preferred_harness=_optional_string(
                value.get("preferredHarness"), f"{label}.preferredHarness"
            ),
            last_harness=_optional_string(
                value.get("lastHarness"), f"{label}.lastHarness"
            ),
            last_session_id=_optional_string(
                value.get("lastSessionId"), f"{label}.lastSessionId"
            ),
            last_active_at_ms=(
                int(value["lastActiveAtMs"])
                if isinstance(value.get("lastActiveAtMs"), int)
                else None
            ),
            pinned_adapters=normalize_pinned_adapters(value.get("pinnedAdapters")),
            created_at_ms=int(created) if isinstance(created, int) else 0,
            hosted_by=_optional_string(value.get("hostedBy"), f"{label}.hostedBy"),
        )
def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentError(f"{label} must be a non-empty string")
    return value
def _optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentError(f"{label} must be a string when present")
    return value or None
@dataclass(frozen=True, slots=True)
class AgentHomeReservation:
    operation: str
    agent: Agent | None
    plan: HomeFilesystemPlan | None
    claim_key: str | None = None
    claim_payload: str | None = None
    lifecycle_attempt: str | None = None
    changed: bool = False
@dataclass(frozen=True, slots=True)
class RestoreDisposition:
    actor: str
    entity_token: str
    desired_generation: str
    disposition_token: str
    status: str
    last_active_at_ms: int | None
    idle_age_ms: int | None
    restore_threshold_ms: int
    restore_override: str
    activity_unknown: bool
    recorded_at_ms: int
@dataclass(frozen=True, slots=True)
class AgentBlock:
    actor: str
    entity_token: str
    reason: str
    blocked_at_ms: int
@dataclass(frozen=True, slots=True)
class HandoverNotice:
    """Design §6.2 (A9): a harness swap zeroes context but must not be silent."""

    actor: str
    previous_harness: str
    previous_session_id: str | None
    next_harness: str

    def text(self) -> str:
        resume = (
            f" Its session id was {self.previous_session_id}."
            if self.previous_session_id
            else " No session id was recorded for it."
        )
        return (
            f"Harness handover for {self.actor}: this agent last ran on "
            f"{self.previous_harness} and is now starting on {self.next_harness}. "
            f"Conversation context does NOT carry across harnesses and is starting "
            f"empty.{resume} Restore it yourself in {self.previous_harness} if you "
            f"need the earlier conversation."
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "previousHarness": self.previous_harness,
            "previousSessionId": self.previous_session_id,
            "nextHarness": self.next_harness,
            "contextCarriedOver": False,
            "notice": self.text(),
        }
_HOSTED_BY_ALTER = (
    "ALTER TABLE agents ADD COLUMN hosted_by TEXT DEFAULT NULL "
    "CHECK (hosted_by IN ('transfer-receive', 'squire-container', 'host-invite'))"
)
_AGENTS_TABLE_DDL = """CREATE TABLE IF NOT EXISTS __TABLE_NAME__ (
    actor TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    machine TEXT NOT NULL,
    uri TEXT NOT NULL UNIQUE,
    entity_token TEXT NOT NULL UNIQUE,
    cwd TEXT,
    config TEXT,
    provider TEXT,
    model TEXT,
    capabilities TEXT NOT NULL DEFAULT '{}',
    harness_args TEXT NOT NULL DEFAULT '{}',
    preferred_harness TEXT,
    last_harness TEXT,
    last_session_id TEXT,
    last_active_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL
)"""
_SCHEMA = "\n".join(
    (
        _AGENTS_TABLE_DDL.replace("__TABLE_NAME__", "agents") + ";",
        """CREATE TABLE IF NOT EXISTS pins (
    adapter TEXT NOT NULL UNIQUE,
    agent TEXT NOT NULL UNIQUE
        REFERENCES agents(actor) ON DELETE CASCADE
);""",
        """CREATE TABLE IF NOT EXISTS lifecycle_resources (
    resource_key TEXT PRIMARY KEY,
    resource_token TEXT NOT NULL,
    active INTEGER NOT NULL,
    payload TEXT NOT NULL
);""",
        """CREATE TABLE IF NOT EXISTS lifecycle_receipts (
    attempt_token TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    expected_resource_token TEXT,
    created_by_operation INTEGER NOT NULL,
    changed INTEGER NOT NULL,
    resource_token TEXT NOT NULL,
    retired INTEGER NOT NULL DEFAULT 0
);""",
        """CREATE TABLE IF NOT EXISTS agent_restore_dispositions (
    actor TEXT PRIMARY KEY REFERENCES agents(actor) ON DELETE CASCADE,
    entity_token TEXT NOT NULL,
    desired_generation TEXT NOT NULL,
    disposition_token TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status = 'idle-suppressed'),
    last_active_at_ms INTEGER,
    idle_age_ms INTEGER,
    restore_threshold_ms INTEGER NOT NULL,
    restore_override TEXT NOT NULL,
    activity_unknown INTEGER NOT NULL,
    recorded_at_ms INTEGER NOT NULL
);""",
        """CREATE TABLE IF NOT EXISTS agent_blocks (
    actor TEXT PRIMARY KEY REFERENCES agents(actor) ON DELETE CASCADE,
    entity_token TEXT NOT NULL,
    reason TEXT NOT NULL,
    blocked_at_ms INTEGER NOT NULL
);""",
        """CREATE TABLE IF NOT EXISTS retired_session_refs (
    actor TEXT NOT NULL,
    entity_token TEXT NOT NULL,
    session_ref TEXT NOT NULL,
    retired_at_ms INTEGER NOT NULL,
    reason TEXT NOT NULL,
    destroy_attempt TEXT NOT NULL,
    PRIMARY KEY (actor, session_ref)
);""",
        """CREATE INDEX IF NOT EXISTS retired_session_refs_retired_at
    ON retired_session_refs(retired_at_ms);""",
    )
)
_AGENTS_TABLE_COLUMNS = (
    "actor",
    "owner",
    "machine",
    "uri",
    "entity_token",
    "cwd",
    "config",
    "provider",
    "model",
    "capabilities",
    "harness_args",
    "preferred_harness",
    "last_harness",
    "last_session_id",
    "last_active_at_ms",
    "created_at_ms",
    "hosted_by",
)
_AGENTS_TABLE_REQUIRED_COLUMNS = ("actor", "owner", "machine", "uri", "created_at_ms")
def _rebuild_agents_table(connection: sqlite3.Connection) -> None:
    """Swap in an ``agents`` table whose ``entity_token`` is NOT NULL UNIQUE.

    Follows sqlite's documented 12-step procedure under the caller's write
    transaction (with ``foreign_keys=OFF`` set outside it): create the
    replacement from :data:`_AGENTS_TABLE_DDL`, copy every row with its
    token materialized *before* any write (never UPDATE while iterating a
    live cursor), drop the old table, rename.  Rows carrying a NULL or blank
    token — the mixed-version-writer residue — get a fresh incarnation token:
    no grant can exist for a blank-token row (only newer binaries write
    grants, and those never write blank tokens), so nothing is silently
    inherited.  Duplicate surviving tokens fail the UNIQUE copy loudly
    instead of quietly keeping two agents on one incarnation.
    """

    rows = connection.execute("SELECT * FROM agents").fetchall()
    present = set(rows[0].keys()) if rows else {
        row["name"] for row in connection.execute("PRAGMA table_info(agents)")
    }
    missing = [
        column for column in _AGENTS_TABLE_REQUIRED_COLUMNS if column not in present
    ]
    if missing:
        raise RuntimeError(
            "agents table is missing required column(s) "
            f"{missing}; refusing to migrate this database"
        )
    connection.execute(
        _AGENTS_TABLE_DDL.replace("__TABLE_NAME__", "agents_entity_migrated")
    )
    copy_columns = tuple(
        column for column in _AGENTS_TABLE_COLUMNS if column != "hosted_by"
    )
    for row in rows:
        values: dict[str, object] = {
            column: row[column] if column in present else None
            for column in _AGENTS_TABLE_COLUMNS
        }
        for column, default in (("capabilities", "{}"), ("harness_args", "{}")):
            if values[column] is None:
                values[column] = default
        token = values["entity_token"]
        values["entity_token"] = (
            token if isinstance(token, str) and token else uuid.uuid4().hex
        )
        try:
            connection.execute(
                "INSERT INTO agents_entity_migrated ({columns}) "
                "VALUES ({placeholders})".format(
                    columns=", ".join(copy_columns),
                    placeholders=", ".join("?" for _ in copy_columns),
                ),
                tuple(values[column] for column in copy_columns),
            )
        except sqlite3.IntegrityError as error:
            raise RuntimeError(
                f"agents entity_token migration cannot copy row "
                f"{row['actor']!r}: {error}. Refusing to open a database whose "
                "incarnation tokens are not globally unique."
            ) from error
    hosted_values = [
        (str(row["hosted_by"]), str(row["actor"]))
        for row in rows
        if "hosted_by" in present and row["hosted_by"] is not None
    ]
    connection.execute("DROP TABLE agents")
    connection.execute(
        "ALTER TABLE agents_entity_migrated RENAME TO agents"
    )
    # The renamed table gets hosted_by from the same single ALTER the
    # fresh-database path uses, then prior hosting values are restored by
    # primary key (never by iterating a live cursor).
    connection.execute(_HOSTED_BY_ALTER)
    connection.executemany(
        "UPDATE agents SET hosted_by = ? WHERE actor = ?", hosted_values
    )
def _hosted_check_needs_rebuild(
    connection: sqlite3.Connection, columns: Mapping[str, sqlite3.Row],
) -> bool:
    # A missing column can use ALTER. An existing CHECK cannot: in particular,
    # modern databases already have NOT NULL tokens but only two hosting values.
    if "hosted_by" not in columns:
        return False
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'agents'"
    ).fetchone()
    return row is not None and "'host-invite'" not in str(row["sql"])
def _connect(database: Path) -> sqlite3.Connection:
    """Open the agents database with the repo's established sqlite settings.

    Mirrors ``inbox.sqlite3`` (`hyprial.inbox.service`): WAL journal and
    ``synchronous=NORMAL``; adds a busy timeout because two processes legally
    write this database — the daemon, and offline CLI tooling such as
    ``hyprial adapter remove``.  ``foreign_keys=ON`` is what arms the pin
    cascade; SQLite leaves it off per-connection by default.
    """

    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(database, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(_SCHEMA)
    # Explicit migration also upgrades existing agents tables. Serialize the
    # check and ALTER across daemon/offline CLI opens; CREATE IF NOT EXISTS
    # alone would leave pre-hosting databases without the column.
    #
    # A pre-entity-token database cannot be upgraded with ``ALTER TABLE``
    # alone: SQLite would add ``entity_token`` nullable and without
    # single-column uniqueness, and the one writer that can put a NULL there
    # is a *mixed-version* binary (an older build whose INSERT predates the
    # column).  A NULL token stringifies to ``"None"`` on read
    # (``_row_agent``) and again at the resolver fence, where ``"None" ==
    # "None"`` turns the incarnation check into a constant.  So the weak
    # shape itself is rebuilt, not patched: the replacement table carries the
    # full ``NOT NULL UNIQUE`` contract and the old binary's NULL INSERT now
    # fails loudly at the write.  ``PRAGMA foreign_keys`` is a no-op inside a
    # transaction, so the rebuild runs with it off. This also upgrades an
    # existing hosted_by CHECK. foreign_key_check detects dangling references;
    # only row-for-row regression tests detect accidental cascade deletions.
    rebuild = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        columns = {
            row["name"]: row for row in connection.execute("PRAGMA table_info(agents)")
        }
        token_column = columns.get("entity_token")
        if (
            token_column is not None and int(token_column["notnull"])
            and not _hosted_check_needs_rebuild(connection, columns)
        ):
            if "config" not in columns:
                connection.execute("ALTER TABLE agents ADD COLUMN config TEXT DEFAULT NULL")
            if "last_active_at_ms" not in columns:
                connection.execute(
                    "ALTER TABLE agents ADD COLUMN last_active_at_ms INTEGER DEFAULT NULL"
                )
            if "hosted_by" not in columns:
                connection.execute(_HOSTED_BY_ALTER)
        else:
            rebuild = True
            connection.rollback()
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            # Re-read under the write lock: another process may have rebuilt
            # the table between the two transactions.
            columns = {
                row["name"]: row
                for row in connection.execute("PRAGMA table_info(agents)")
            }
            token_column = columns.get("entity_token")
            if (
                token_column is None or not int(token_column["notnull"])
                or _hosted_check_needs_rebuild(connection, columns)
            ):
                _rebuild_agents_table(connection)
            else:
                if "config" not in columns:
                    connection.execute(
                        "ALTER TABLE agents ADD COLUMN config TEXT DEFAULT NULL"
                    )
                if "last_active_at_ms" not in columns:
                    connection.execute(
                        "ALTER TABLE agents ADD COLUMN last_active_at_ms INTEGER DEFAULT NULL"
                    )
                if "hosted_by" not in columns:
                    connection.execute(_HOSTED_BY_ALTER)
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS agents_actor_entity_token "
            "ON agents(actor, entity_token)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS agent_session_ref_history ("
            "history_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "actor TEXT NOT NULL, entity_token TEXT NOT NULL, "
            "session_ref TEXT NOT NULL, registered_at_ms INTEGER NOT NULL, "
            "UNIQUE(actor, entity_token, session_ref), "
            "FOREIGN KEY(actor, entity_token) REFERENCES agents(actor, entity_token) "
            "ON DELETE CASCADE)"
        )
        retired_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(retired_session_refs)")
        }
        if "entity_token" not in retired_columns:
            connection.execute(
                "ALTER TABLE retired_session_refs ADD COLUMN "
                "entity_token TEXT NOT NULL DEFAULT ''"
            )
        if "destroy_attempt" not in retired_columns:
            connection.execute(
                "ALTER TABLE retired_session_refs ADD COLUMN "
                "destroy_attempt TEXT NOT NULL DEFAULT 'legacy'"
            )
        disposition_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(agent_restore_dispositions)"
            )
        }
        if "disposition_token" not in disposition_columns:
            connection.execute(
                "ALTER TABLE agent_restore_dispositions ADD COLUMN "
                "disposition_token TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            "UPDATE agent_restore_dispositions "
            "SET disposition_token = lower(hex(randomblob(16))) "
            "WHERE disposition_token = ''"
        )
        # CREATE TABLE ran before the old agents table was upgraded, so an old
        # database needs the grants table created once more after the composite
        # parent key exists.
        connection.execute(
            "CREATE TABLE IF NOT EXISTS agent_secret_grants ("
            "agent TEXT NOT NULL, entity_token TEXT NOT NULL, grant_id TEXT NOT NULL, "
            "source TEXT NOT NULL CHECK (source IN ('user-provider', 'agent-private')), "
            "entry_id TEXT NOT NULL, field_name TEXT, environment_names TEXT NOT NULL, "
            "revision INTEGER NOT NULL CHECK (revision > 0), "
            "PRIMARY KEY (agent, grant_id), "
            "FOREIGN KEY (agent, entity_token) REFERENCES agents(actor, entity_token) "
            "ON DELETE CASCADE)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS agent_capability_grants ("
            "actor TEXT NOT NULL, entity_token TEXT NOT NULL, grant_id TEXT NOT NULL, "
            "capability TEXT NOT NULL, scope TEXT NOT NULL, granted_by TEXT NOT NULL, "
            "revision INTEGER NOT NULL CHECK (revision > 0), "
            "PRIMARY KEY (actor, grant_id), "
            "FOREIGN KEY (actor, entity_token) REFERENCES agents(actor, entity_token) "
            "ON DELETE CASCADE)"
        )
        # Historical facts survive actor destruction and incarnation changes.
        connection.execute(
            "CREATE TABLE IF NOT EXISTS agent_grant_journal ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL, "
            "actor TEXT NOT NULL, entity_token TEXT NOT NULL, "
            "action TEXT NOT NULL CHECK (action IN ('host-invite','grant','revoke')), "
            "grant_id TEXT NOT NULL, capability TEXT, scope TEXT, "
            "\"by\" TEXT NOT NULL, revision INTEGER, note TEXT)"
        )
        # `agent:<name>` looked like a canonical URI while actually naming an
        # internal lifecycle resource.  Migrate it transactionally to an explicit
        # non-address namespace before any actor reads receipts, preserving every
        # resource token and attempt fence across upgrades.
        old_agent_resources = tuple(
            connection.execute(
                "SELECT resource_key FROM lifecycle_resources "
                "WHERE resource_key LIKE 'agent:%'"
            )
        )
        for row in old_agent_resources:
            old_key = str(row["resource_key"])
            new_key = f"agent-record:{old_key.removeprefix('agent:')}"
            collision = connection.execute(
                "SELECT 1 FROM lifecycle_resources WHERE resource_key = ?",
                (new_key,),
            ).fetchone()
            if collision is not None:
                raise RuntimeError(
                    f"agent lifecycle resource migration collision: {old_key} -> {new_key}"
                )
            connection.execute(
                "UPDATE lifecycle_receipts SET resource_key = ? WHERE resource_key = ?",
                (new_key, old_key),
            )
            connection.execute(
                "UPDATE lifecycle_resources SET resource_key = ? WHERE resource_key = ?",
                (new_key, old_key),
            )
        if rebuild:
            # Finish sqlite's documented rebuild procedure: prove pins/grants
            # still resolve before anything is committed.
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    "agents entity_token rebuild broke a foreign key: "
                    f"{[tuple(row) for row in violations[:10]]}"
                )
            remaining = connection.execute(
                "SELECT count(*) FROM agents "
                "WHERE entity_token IS NULL OR entity_token = ''"
            ).fetchone()[0]
            if int(remaining):
                raise RuntimeError(
                    "agents entity_token rebuild left blank tokens; refusing to open"
                )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        if rebuild:
            connection.execute("PRAGMA foreign_keys=ON")
    return connection
