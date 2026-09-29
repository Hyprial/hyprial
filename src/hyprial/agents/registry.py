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
    :mod:`hyprial.agents.liveness`, never on the persisted record.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from hyprial.contracts import ipc_errors
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Self, TYPE_CHECKING

from .config import (
    AgentConfig,
    normalize_agent_config,
    validate_agent_config_location,
)
from .home import (
    AgentHomeError,
    AgentHomeProvisioner,
    HomeProvisioningAttempt,
    HomeReceipt,
    WorkspaceSummary,
)
from .home_effects import (
    CleanupHome,
    HomeFilesystemPlan,
    ProvisionHome,
    ReplaceHome,
)
from .grants import CapabilityGrant, GrantJournalEntry, principal, single_line

#: This module's single logging seam, deliberately narrow: filesystem
#: compensation failures are the one edge whose silence had no other
#: observable surface (the next create would only fail as ``unowned-residue``).
#: Everything else stays on typed errors and the durable database.
_LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    from hyprial.daemon.lifecycle_receipts import DomainEffectClaim
    from .secrets import SecretGrant, SecretSource


def _uri() -> Any:
    """Import the dependency-free canonical URI helpers."""

    from hyprial import uri

    return uri


__all__ = [
    "ACTOR_NAME_PATTERN",
    "Agent",
    "AgentConfig",
    "AgentError",
    "AgentExistsError",
    "AgentNotFoundError",
    "AgentRegistry",
    "AgentBlock",
    "RestoreDisposition",
    "AgentHomeError",
    "PinConflictError",
    "default_registry",
    "local_actors",
    "normalize_agent_config",
    "normalize_capabilities",
    "normalize_harness_args",
    "normalize_pinned_adapters",
    "set_default_registry",
    "verify_fetch_claim",
]

#: ``canonical_agent_uri`` already rejects ``:``; this is stricter on purpose —
#: the actor short name appears in URIs, log lines and CLI arguments, and a
#: name outside this set is rejected at creation rather than becoming an
#: unaddressable registry row.  (It also kept the pre-sqlite one-file-per-agent
#: layout safe; the strictness is retained so no name that ever registered
#: becomes invalid.)
ACTOR_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_MAX_ACTOR_NAME_LENGTH = 128

# Activity is archival evidence, not liveness. Repeated heartbeats and message
# events collapse onto at most one durable write per agent in this window.
AGENT_ACTIVITY_WRITE_INTERVAL_MS = 60_000

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
    #: show the binding, and kept a *list* for shape-compatibility even
    #: though UNIQUE(agent) makes it hold at most one adapter.  Deleting the
    #: agent row cascades the pins away.  Inbound-only and not a bijection:
    #: outbound replies travel with each message's own correlation, never by
    #: looking an adapter up here.
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


#: The one ALTER that adds ``hosted_by`` to any ``agents`` table lacking it,
#: single-sourced across the fresh-database, race-lost and rebuild paths.
#: The hosting scope guard (``tests/test_hosted_scope.py``) expects exactly
#: one runtime enum and one SQLite CHECK spelling — the latter inside an
#: ``ALTER TABLE`` constant — so this stays one plain string literal.
_HOSTED_BY_ALTER = (
    "ALTER TABLE agents ADD COLUMN hosted_by TEXT DEFAULT NULL "
    "CHECK (hosted_by IN ('transfer-receive', 'squire-container', 'host-invite'))"
)

#: The ``agents`` table DDL, parameterized by table name.  The entity-token
#: migration below creates its replacement from this exact spelling so a
#: rebuilt table can never drift from a freshly created one;
#: ``tests/test_agent_home_dirs.py`` pins that parity against ``PRAGMA``
#: reads of both.  ``__TABLE_NAME__`` (not ``{}``) because the defaults
#: contain literal braces.
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
#: Columns an upgradeable ``agents`` table must already have; missing values
#: fall back to the schema defaults.  Anything else means the table is not a
#: shape this registry has ever written, and the rebuild refuses loudly
#: instead of guessing.
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


class AgentRegistry:
    """Every agent on this machine, in one real SQLite database.

    The database path is a constructor argument so tests (and a second daemon
    on the same machine) can point at their own file.  Writes are
    transactions; the schema owns the invariants (A1 PRIMARY KEY, pin
    one-to-one UNIQUEs, destroy cascade), so the code translates
    ``IntegrityError`` into the typed errors instead of re-checking by hand.

    On construction, any pre-sqlite ``<legacy>/​*.json`` records are imported
    once and the files renamed to ``*.json.imported`` — kept, not deleted,
    so a rollback to a file-reading build still finds its data.
    """

    def __init__(
        self,
        database: Path,
        *,
        owner: str,
        machine: str,
        clock: Callable[[], int] = _now_ms,
        legacy_directory: Path | None = None,
        hyprial_home: Path | None = None,
    ) -> None:
        self.database = Path(database)
        self.owner = owner
        self.machine = machine
        self._clock = clock
        self._lock = threading.RLock()
        self._db = _connect(self.database)
        self._restore_dispositions = {
            item.actor: item
            for row in self._db.execute(
                "SELECT * FROM agent_restore_dispositions"
            )
            if (item := self._restore_disposition_row(row)) is not None
        }
        self._agent_blocks = {
            item.actor: item
            for row in self._db.execute("SELECT * FROM agent_blocks")
            if (item := self._agent_block_row(row)) is not None
        }
        self._last_activity_write: dict[str, int] = {}
        self._home = (
            None
            if hyprial_home is None
            else AgentHomeProvisioner(Path(hyprial_home).expanduser())
        )
        #: Legacy JSON records imported by this construction, for the daemon
        #: to log (this module's own logging is only the compensation-residue
        #: warning; see :data:`_LOG`).
        self.imported_legacy: tuple[str, ...] = ()
        self._import_legacy_files(
            self.database.parent / "agents"
            if legacy_directory is None
            else Path(legacy_directory)
        )

    @property
    def home_enabled(self) -> bool:
        return self._home is not None

    @property
    def home_provisioner(self) -> AgentHomeProvisioner:
        if self._home is None:
            raise AgentHomeError("not-configured", "(registry)", "provisioner")
        return self._home

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- naming -----------------------------------------------------------

    @staticmethod
    def normalize_actor(value: str) -> str:
        """Reduce a bare name or a canonical URI to the machine-local actor name."""

        candidate = _uri().agent_uri_actor(value) or value
        if not candidate:
            raise InvalidAgentNameError("agent name must not be empty")
        if len(candidate) > _MAX_ACTOR_NAME_LENGTH:
            raise InvalidAgentNameError(
                f"agent name is too long (max {_MAX_ACTOR_NAME_LENGTH}): {candidate!r}"
            )
        if not ACTOR_NAME_PATTERN.fullmatch(candidate):
            raise InvalidAgentNameError(
                f"agent name {candidate!r} must match "
                f"{ACTOR_NAME_PATTERN.pattern} — it is also its registry file name"
            )
        return candidate

    def native_actor(self, value: str) -> str:
        """Validate a public creation name without discarding URI ownership."""

        parsed = _uri().parse_agent_uri(value)
        if parsed is not None and (parsed[0] != self.owner or parsed[1] != self.machine):
            raise InvalidAgentNameError(
                f"cannot create foreign agent {value!r} on {self.owner}@{self.machine}; "
                "hosting requires transfer receive"
            )
        return self.normalize_actor(value)

    def local_actor(self, value: str) -> str | None:
        """The machine-local agent name behind ``value``, or None if not ours.

        Stricter than :meth:`normalize_actor` on purpose: a four-segment URI
        naming a different owner or machine is somebody else's agent, and must
        not resolve to a local record that merely shares the short name. That
        distinction is the whole reason this class owns identity.
        The sole exception is an exact stored URI with explicit hosting authority.
        """

        parsed = _uri().parse_agent_uri(value)
        if parsed is not None:
            if parsed[0] != self.owner or parsed[1] != self.machine:
                with self._lock:
                    row = self._db.execute(
                        "SELECT actor FROM agents WHERE uri = ? "
                        "AND hosted_by IS NOT NULL", (value,),
                    ).fetchone()
                return None if row is None else str(row["actor"])
            candidate = parsed[2]
        elif ":" in value:
            return None
        else:
            candidate = value
        try:
            return self.normalize_actor(candidate)
        except AgentError:
            return None

    def uri_for(self, actor: str) -> str:
        name = self.normalize_actor(actor)
        with self._lock:
            row = self._db.execute(
                "SELECT uri FROM agents WHERE actor = ? AND hosted_by IS NOT NULL",
                (name,),
            ).fetchone()
        if row is not None:
            return str(row["uri"])
        return _uri().canonical_agent_uri(self.owner, self.machine, name)

    # -- reads ------------------------------------------------------------

    def get(self, actor: str) -> Agent | None:
        name = self.local_actor(actor)
        if name is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            if row is None:
                return None
            return self._row_agent(row, self._pinned_adapters_locked(name))

    def require(self, actor: str) -> Agent:
        agent = self.get(actor)
        if agent is None:
            raise AgentNotFoundError(f"no agent named {actor!r} on this machine")
        return agent

    def exists(self, actor: str) -> bool:
        return self.get(actor) is not None

    def list(self) -> tuple[Agent, ...]:
        with self._lock:
            pins: dict[str, list[str]] = {}
            for row in self._db.execute(
                "SELECT adapter, agent FROM pins ORDER BY adapter"
            ):
                pins.setdefault(row["agent"], []).append(row["adapter"])
            return tuple(
                self._row_agent(row, tuple(pins.get(row["actor"], ())))
                for row in self._db.execute(
                    "SELECT * FROM agents ORDER BY actor"
                )
            )

    def now_ms(self) -> int:
        """Return the registry's injectable wall clock in milliseconds."""

        return int(self._clock())

    def activity_hints(self, actor: str | Agent) -> dict[str, int | bool | None]:
        """Return conservative, explicitly non-recorded activity clues."""

        agent = actor if isinstance(actor, Agent) else self.require(actor)
        home_mtime_ms: int | None = None
        if self._home is not None:
            home = self._home.agents_root / agent.actor
            try:
                home_mtime_ms = home.stat().st_mtime_ns // 1_000_000
            except FileNotFoundError:
                pass
        return {
            "createdAtMs": agent.created_at_ms,
            "hasLastSessionId": agent.last_session_id is not None,
            "agentHomeMtimeMs": home_mtime_ms,
        }

    def local_actors(self) -> tuple[str, ...]:
        """Design §5.2: every actor this machine speaks for, as canonical URIs.

        The delivery line consumes this for its batched pull; it must never
        compose an identity itself.
        """

        return tuple(agent.uri for agent in self.list())

    def verify_fetch_claim(
        self, actor: str, signature: bytes, nonce: bytes
    ) -> bool:
        """Design §5.2 / §8.2 (G5): may the caller take ``actor``'s messages?

        **This performs no cryptographic verification.**  The Python daemon
        carries no Ed25519 key material at all (there is no key store, no
        signing on the send path, and no crypto dependency in
        ``pyproject.toml``), so there is nothing to check a signature against.
        Adding a key system is product scope this change was not granted.

        What it does enforce is the half of G5 that is answerable today: the
        claim must name an agent this machine actually owns, and it must carry
        a claim at all.  A holder calling this still rejects a fetch for an
        actor that is not ours — which is the check the receipt queryable was
        missing.  Wire the real signature check in here once key material
        exists; the signature and every call site stay unchanged.
        """

        if not signature or not nonce:
            return False
        name = self.local_actor(actor)
        return False if name is None else self.exists(name)

    # -- writes -----------------------------------------------------------

    def apply_lifecycle(self, request: object) -> tuple[Any, str, bool]:
        """Apply identity/binding mutation with its token receipt atomically.

        The returned tuple is ``(MutationProvenance, operation, replayed)``.
        Runtime liveness is a projection of the durable binding resource and
        is refreshed by :class:`AgentActor` after this transaction commits.
        """

        from hyprial.agents.ports import (
            BindAgentCommand,
            CreateAgentCommand,
            DestroyAgentCommand,
            ReleaseAgentCommand,
        )
        from hyprial.daemon.lifecycle_receipts import (
            LifecycleMutationRequest,
            MutationProvenance,
        )

        if not isinstance(request, LifecycleMutationRequest):
            raise TypeError("request must be LifecycleMutationRequest")
        payload = request.payload
        if self._home is not None and isinstance(
            payload, (CreateAgentCommand, DestroyAgentCommand)
        ):
            reservation, settled, operation, replayed = (
                self.prepare_home_lifecycle(request)
            )
            if reservation is None:
                assert settled is not None
                return settled, operation, replayed
            receipt = (
                None
                if reservation.plan is None
                else self._execute_home_plan(reservation.plan)
            )
            return (
                self.complete_home_lifecycle(reservation, receipt),
                operation,
                False,
            )
        agent_name: str | None = None
        if isinstance(payload, (CreateAgentCommand, DestroyAgentCommand)):
            agent_name = (
                self.native_actor(payload.name) if isinstance(payload, CreateAgentCommand)
                else self.normalize_actor(payload.name)
            )
            key = f"agent-record:{agent_name}"
            operation = "create" if isinstance(payload, CreateAgentCommand) else "destroy"
        elif isinstance(payload, (BindAgentCommand, ReleaseAgentCommand)):
            agent = self.require(payload.actor)
            key = f"binding:{agent.uri}"
            operation = "bind" if isinstance(payload, BindAgentCommand) else "release"
        else:
            raise TypeError(f"unsupported Agent lifecycle payload: {type(payload).__name__}")
        with self._home_transaction() as home_attempts:
            prior = self._db.execute(
                "SELECT * FROM lifecycle_receipts WHERE attempt_token = ?",
                (request.attempt_token,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["operation_id"] != request.operation_id
                    or prior["resource_key"] != key
                    or prior["expected_resource_token"]
                    != request.expected_resource_token
                ):
                    raise ValueError("lifecycle attempt token was reused")
                return (
                    MutationProvenance(
                        bool(prior["created_by_operation"]),
                        bool(prior["changed"]),
                        str(prior["resource_token"]),
                    ),
                    operation,
                    True,
                )

            row = self._db.execute(
                "SELECT resource_token, active, payload FROM lifecycle_resources "
                "WHERE resource_key = ?",
                (key,),
            ).fetchone()
            actual_payload: dict[str, object] = {}
            actual_active = False
            if agent_name is not None:
                agent_row = self._db.execute(
                    "SELECT * FROM agents WHERE actor = ?", (agent_name,)
                ).fetchone()
                if agent_row is not None:
                    actual_active = True
                    actual_agent = self._row_agent(
                        agent_row, self._pinned_adapters_locked(str(agent_row["actor"]))
                    )
                    actual_payload = actual_agent.to_json()
            elif row is not None and bool(row["active"]):
                actual_active = True
                loaded = json.loads(str(row["payload"]))
                if isinstance(loaded, dict):
                    actual_payload = loaded

            if row is None or bool(row["active"]) != actual_active or (
                actual_active and json.loads(str(row["payload"])) != actual_payload
            ):
                token = uuid.uuid4().hex
                self._db.execute(
                    "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(resource_key) DO UPDATE SET "
                    "resource_token=excluded.resource_token, active=excluded.active, "
                    "payload=excluded.payload",
                    (key, token, int(actual_active), json.dumps(actual_payload, sort_keys=True)),
                )
            else:
                token = str(row["resource_token"])

            expected = request.expected_resource_token
            is_create = operation in {"create", "bind"}
            changed = False
            created = False
            stored_payload = actual_payload
            if operation == "bind" and expected is None:
                desired_payload = self._lifecycle_create_payload(payload, row)
                if not actual_active or actual_payload != desired_payload:
                    # A dead connector may leave a durable binding receipt
                    # behind.  Explicitly binding a different harness is a
                    # replacement generation, not a no-op; rotate its fence
                    # while the Agent actor updates projection and resource in
                    # the same SQLite transaction.
                    token = uuid.uuid4().hex
                    changed = created = True
                    stored_payload = desired_payload
            elif is_create:
                if expected is None:
                    if not actual_active:
                        token = uuid.uuid4().hex
                        changed = created = True
                        stored_payload = self._lifecycle_create_payload(payload, row)
                elif not actual_active and token == expected:
                    changed = created = True
                    stored_payload = self._lifecycle_create_payload(
                        payload, row, reuse_resource_payload=True
                    )
            elif actual_active and (expected is None or token == expected):
                changed = created = True

            if changed and operation == "create":
                agent = Agent.from_json(stored_payload, "lifecycle.agent")
                home_attempt = self._provision_home_locked(
                    agent, allow_revoked=expected is not None
                )
                if home_attempt is not None:
                    home_attempts.append(home_attempt)
                self._db.execute(*self._insert_statement(agent))
                if home_attempt is not None:
                    self._record_home_resource_locked(home_attempt.receipt, True)
                for adapter in agent.pinned_adapters:
                    self._db.execute(
                        "INSERT INTO pins(adapter, agent) VALUES(?, ?)",
                        (adapter, agent.actor),
                    )
            elif changed and operation == "destroy":
                assert agent_name is not None
                prior_agent = self.require(agent_name)
                if _is_compensating_attempt(request.attempt_token):
                    # A create saga compensating its own half-built agent: the
                    # home is a half-product of THIS operation, never a
                    # credential-bearing residue, so the rollback removes it
                    # fully instead of leaving it for an explicit operator
                    # cleanup (the T13 residue is only for a forward destroy).
                    self._compensate_home_locked(prior_agent)
                else:
                    self._revoke_home_locked(prior_agent)
                self._db.execute(
                    "DELETE FROM agents WHERE actor = ?", (agent_name,)
                )
            elif changed and operation == "bind":
                assert isinstance(payload, BindAgentCommand)
                agent = self.require(payload.actor)
                self._db.execute(
                    "UPDATE agents SET last_harness = ?, last_session_id = ?, "
                    "preferred_harness = COALESCE(preferred_harness, ?) "
                    "WHERE actor = ?",
                    (
                        payload.harness,
                        payload.session_id,
                        payload.harness,
                        agent.actor,
                    ),
                )
                # U0c: the saga's own bind drifts the agents row (last_harness,
                # preferred_harness), and the agent-record fence compares its
                # payload against exactly that row.  Without this sync the
                # next lifecycle mutation's reconcile would rotate the fence
                # token and the create saga's compensation (destroy with the
                # CREATE token) would be fenced off as if an EXTERNAL
                # replacement had happened -- leaving the half-built agent
                # behind (U0c ④).  Same token, refreshed payload: external
                # mutations still drift and still fence.
                record_key = f"agent-record:{agent.actor}"
                refreshed = self.require(payload.actor)
                self._db.execute(
                    "UPDATE lifecycle_resources SET payload = ? "
                    "WHERE resource_key = ?",
                    (
                        json.dumps(refreshed.to_json(), sort_keys=True),
                        record_key,
                    ),
                )

            active = actual_active
            if changed:
                active = is_create
            self._db.execute(
                "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
                "ON CONFLICT(resource_key) DO UPDATE SET "
                "resource_token=excluded.resource_token, active=excluded.active, "
                "payload=excluded.payload",
                (key, token, int(active), json.dumps(stored_payload, sort_keys=True)),
            )
            self._db.execute(
                "INSERT INTO lifecycle_receipts VALUES(?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    request.attempt_token,
                    request.operation_id,
                    key,
                    expected,
                    int(created),
                    int(changed),
                    token,
                ),
            )
            return MutationProvenance(created, changed, token), operation, False

    def _begin_home_lifecycle(
        self, request: object
    ) -> tuple[dict[str, object] | None, Any | None, str, bool]:
        """Durably claim Agent-home I/O without holding SQLite across it."""

        from hyprial.agents.ports import CreateAgentCommand, DestroyAgentCommand
        from hyprial.daemon.lifecycle_receipts import (
            LifecycleMutationRequest,
            MutationProvenance,
        )

        assert isinstance(request, LifecycleMutationRequest)
        payload = request.payload
        assert isinstance(payload, (CreateAgentCommand, DestroyAgentCommand))
        operation = "create" if isinstance(payload, CreateAgentCommand) else "destroy"
        name = (
            self.native_actor(payload.name)
            if operation == "create"
            else self.normalize_actor(payload.name)
        )
        key = f"agent-record:{name}"
        effect_key = f"agent-home-effect:{request.attempt_token}"
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            prior = self._db.execute(
                "SELECT * FROM lifecycle_receipts WHERE attempt_token = ?",
                (request.attempt_token,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["operation_id"] != request.operation_id
                    or prior["resource_key"] != key
                    or prior["expected_resource_token"]
                    != request.expected_resource_token
                ):
                    raise ValueError("lifecycle attempt token was reused")
                return (
                    None,
                    MutationProvenance(
                        bool(prior["created_by_operation"]),
                        bool(prior["changed"]),
                        str(prior["resource_token"]),
                    ),
                    operation,
                    True,
                )
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
                (effect_key,),
            ).fetchone()
            if pending is not None:
                claim = json.loads(str(pending["payload"]))
                if not isinstance(claim, dict) or any(
                    claim.get(field) != expected
                    for field, expected in (
                        ("attemptToken", request.attempt_token),
                        ("operationId", request.operation_id),
                        ("expectedResourceToken", request.expected_resource_token),
                    )
                ):
                    raise ValueError("lifecycle attempt token was reused")
                return claim, None, operation, False

            row = self._db.execute(
                "SELECT resource_token, active, payload FROM lifecycle_resources "
                "WHERE resource_key = ?",
                (key,),
            ).fetchone()
            agent_row = self._db.execute(
                "SELECT * FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            actual_active = agent_row is not None
            actual_payload: dict[str, object] = {}
            if agent_row is not None:
                actual_payload = self._row_agent(
                    agent_row, self._pinned_adapters_locked(name)
                ).to_json()
            if row is None or bool(row["active"]) != actual_active or (
                actual_active and json.loads(str(row["payload"])) != actual_payload
            ):
                token = uuid.uuid4().hex
            else:
                token = str(row["resource_token"])
            expected = request.expected_resource_token
            changed = created = False
            stored_payload = actual_payload
            if operation == "create":
                if expected is None and not actual_active:
                    token = uuid.uuid4().hex
                    changed = created = True
                    stored_payload = self._lifecycle_create_payload(payload, row)
                elif not actual_active and token == expected:
                    changed = created = True
                    stored_payload = self._lifecycle_create_payload(
                        payload, row, reuse_resource_payload=True
                    )
            elif actual_active and (expected is None or token == expected):
                changed = created = True

            if not changed:
                self._db.execute(
                    "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(resource_key) DO UPDATE SET "
                    "resource_token=excluded.resource_token, active=excluded.active, "
                    "payload=excluded.payload",
                    (key, token, int(actual_active), json.dumps(stored_payload, sort_keys=True)),
                )
                self._db.execute(
                    "INSERT INTO lifecycle_receipts VALUES(?, ?, ?, ?, ?, ?, ?, 0)",
                    (
                        request.attempt_token, request.operation_id, key, expected,
                        0, 0, token,
                    ),
                )
                return None, MutationProvenance(False, False, token), operation, False

            revoked_home: HomeReceipt | None = None
            replacement_home: HomeReceipt | None = None
            prior_home: HomeReceipt | None = None
            prior_home_active: bool | None = None
            if operation == "create":
                assert self._home is not None
                desired_agent = Agent.from_json(stored_payload, "lifecycle.agent")
                home_row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-home:{name}",),
                ).fetchone()
                if home_row is not None:
                    prior_home = HomeReceipt.from_json(
                        json.loads(str(home_row["payload"]))
                    )
                    prior_home_active = bool(home_row["active"])
                    if bool(home_row["active"]):
                        raise AgentHomeError(
                            "create-fenced", name, "lifecycle-claim"
                        )
                    if prior_home.status == "revoked":
                        revoked_home = prior_home
                    elif (
                        prior_home.status == "ready"
                        and prior_home.entity_token == desired_agent.entity_token
                    ):
                        replacement_home = prior_home
                    elif prior_home.status != "cleaned":
                        raise AgentHomeError(
                            "operation-pending", name, "lifecycle-claim"
                        )
                if replacement_home is None:
                    replacement_home = self._home.claim_receipt(
                        actor=name, entity_token=desired_agent.entity_token
                    )
            if operation == "destroy":
                assert agent_row is not None
                prior_agent = self._row_agent(
                    agent_row, self._pinned_adapters_locked(name)
                )
                revoked_home = self._revoke_home_locked(prior_agent)
                self._db.execute("DELETE FROM agents WHERE actor = ?", (name,))
            self._db.execute(
                "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
                "ON CONFLICT(resource_key) DO UPDATE SET "
                "resource_token=excluded.resource_token, active=excluded.active, "
                "payload=excluded.payload",
                (key, token, 0, json.dumps(stored_payload, sort_keys=True)),
            )
            claim: dict[str, object] = {
                "attemptToken": request.attempt_token,
                "operationId": request.operation_id,
                "expectedResourceToken": expected,
                "operation": operation,
                "actor": name,
                "resourceToken": token,
                "createdByOperation": created,
                "payload": stored_payload,
                "homeReplacement": (
                    None
                    if replacement_home is None
                    else replacement_home.to_json()
                ),
                "homeRevoked": (
                    None if revoked_home is None else revoked_home.to_json()
                ),
                "priorHome": (
                    None if prior_home is None else prior_home.to_json()
                ),
                "priorHomeActive": prior_home_active,
            }
            self._db.execute(
                "INSERT INTO lifecycle_resources VALUES(?, ?, 1, ?)",
                (effect_key, uuid.uuid4().hex, json.dumps(claim, sort_keys=True)),
            )
            return claim, None, operation, False

    def prepare_home_lifecycle(
        self, request: object
    ) -> tuple[AgentHomeReservation | None, Any | None, str, bool]:
        claim, settled, operation, replayed = self._begin_home_lifecycle(request)
        if operation == "destroy" and claim is not None:
            actor = str(claim["actor"])
            with self._lock:
                self._restore_dispositions.pop(actor, None)
                self._agent_blocks.pop(actor, None)
        if claim is None:
            return None, settled, operation, replayed
        payload = claim.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("invalid Agent home lifecycle payload")
        actor = str(claim["actor"])
        raw_replacement = claim.get("homeReplacement")
        raw_revoked = claim.get("homeRevoked")
        replacement = (
            None
            if raw_replacement is None
            else HomeReceipt.from_json(raw_replacement)
        )
        revoked = (
            None if raw_revoked is None else HomeReceipt.from_json(raw_revoked)
        )
        plan: HomeFilesystemPlan | None
        agent: Agent | None
        if operation == "create":
            if replacement is None:
                raise AgentHomeError(
                    "invalid-registry-receipt", actor, "lifecycle-claim"
                )
            plan = (
                ProvisionHome(replacement)
                if revoked is None
                else ReplaceHome(revoked, replacement)
            )
            agent = Agent.from_json(payload, "lifecycle.agent")
        else:
            plan = None if revoked is None else CleanupHome(revoked)
            agent = Agent.from_json(payload, "lifecycle.agent")
        encoded = json.dumps(claim, sort_keys=True)
        return (
            AgentHomeReservation(
                operation,
                agent,
                plan,
                f"agent-home-effect:{claim['attemptToken']}",
                encoded,
                str(claim["attemptToken"]),
                changed=True,
            ),
            None,
            operation,
            replayed,
        )

    def _execute_home_plan(self, plan: HomeFilesystemPlan) -> HomeReceipt:
        home = self.home_provisioner
        if isinstance(plan, ProvisionHome):
            return home.provision_claimed(plan.receipt).receipt
        if isinstance(plan, ReplaceHome):
            try:
                return home.provision_claimed(plan.replacement).receipt
            except AgentHomeError as error:
                if error.category not in {"receipt-mismatch", "unowned-residue"}:
                    raise
                home.cleanup(
                    plan.revoked, expected_token=plan.revoked.resource_token
                )
                return home.provision_claimed(plan.replacement).receipt
        if isinstance(plan, CleanupHome):
            return home.cleanup(
                plan.receipt, expected_token=plan.receipt.resource_token
            )
        raise TypeError("unsupported Agent home filesystem plan")

    def complete_home_lifecycle(
        self,
        reservation: AgentHomeReservation,
        receipt: HomeReceipt | None,
    ) -> Any:
        from hyprial.daemon.lifecycle_receipts import MutationProvenance

        if reservation.claim_payload is None:
            raise TypeError("lifecycle home reservation is not durable")
        claim = json.loads(reservation.claim_payload)
        if not isinstance(claim, dict):
            raise TypeError("invalid lifecycle home reservation")
        attempt = str(claim["attemptToken"])
        operation_id = str(claim["operationId"])
        operation = str(claim["operation"])
        actor = str(claim["actor"])
        token = str(claim["resourceToken"])
        expected = claim.get("expectedResourceToken")
        payload = claim.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("invalid Agent home lifecycle payload")
        key = f"agent-record:{actor}"
        effect_key = f"agent-home-effect:{attempt}"
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
                (effect_key,),
            ).fetchone()
            if pending is None or json.loads(str(pending["payload"])) != dict(claim):
                raise AgentHomeError("create-fenced", actor, "lifecycle-commit")
            incumbent = self._db.execute(
                "SELECT * FROM agents WHERE actor = ?", (actor,)
            ).fetchone()
            if operation == "create":
                if incumbent is not None or reservation.agent is None:
                    raise AgentHomeError("create-fenced", actor, "lifecycle-commit")
                replacement = (
                    reservation.plan.receipt
                    if isinstance(reservation.plan, ProvisionHome)
                    else reservation.plan.replacement
                    if isinstance(reservation.plan, ReplaceHome)
                    else None
                )
                if receipt is None or receipt != replacement:
                    raise AgentHomeError("create-fenced", actor, "lifecycle-commit")
                expected_home = claim.get("priorHome")
                expected_active = claim.get("priorHomeActive")
                current_home = self._db.execute(
                    "SELECT active,payload FROM lifecycle_resources "
                    "WHERE resource_key=?",
                    (f"agent-home:{actor}",),
                ).fetchone()
                if expected_home is None:
                    if current_home is not None:
                        raise AgentHomeError(
                            "create-fenced", actor, "lifecycle-commit"
                        )
                elif (
                    current_home is None
                    or bool(current_home["active"]) != bool(expected_active)
                    or json.loads(str(current_home["payload"])) != expected_home
                ):
                    raise AgentHomeError(
                        "create-fenced", actor, "lifecycle-commit"
                    )
                agent = reservation.agent
                self._db.execute(*self._insert_statement(agent))
                self._record_home_resource_locked(receipt, True)
                for adapter in agent.pinned_adapters:
                    self._db.execute(
                        "INSERT INTO pins(adapter, agent) VALUES(?, ?)",
                        (adapter, agent.actor),
                    )
            elif incumbent is not None:
                raise AgentHomeError("cleanup-fenced", actor, "lifecycle-commit")
            elif isinstance(reservation.plan, CleanupHome):
                if receipt is None or receipt.status != "cleaned":
                    raise AgentHomeError(
                        "cleanup-fenced", actor, "lifecycle-commit"
                    )
                current_home = self._db.execute(
                    "SELECT active,payload FROM lifecycle_resources "
                    "WHERE resource_key=?",
                    (f"agent-home:{actor}",),
                ).fetchone()
                if current_home is None or bool(current_home["active"]):
                    raise AgentHomeError(
                        "cleanup-fenced", actor, "lifecycle-commit"
                    )
                if HomeReceipt.from_json(
                    json.loads(str(current_home["payload"]))
                ) != reservation.plan.receipt:
                    raise AgentHomeError(
                        "cleanup-fenced", actor, "lifecycle-commit"
                    )
                self._record_home_resource_locked(receipt, False)
            self._db.execute(
                "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
                "ON CONFLICT(resource_key) DO UPDATE SET "
                "resource_token=excluded.resource_token, active=excluded.active, "
                "payload=excluded.payload",
                (key, token, int(operation == "create"), json.dumps(payload, sort_keys=True)),
            )
            self._db.execute(
                "INSERT INTO lifecycle_receipts VALUES(?, ?, ?, ?, ?, 1, ?, 0)",
                (
                    attempt,
                    operation_id,
                    key,
                    expected,
                    int(bool(claim["createdByOperation"])),
                    token,
                ),
            )
            self._db.execute(
                "DELETE FROM lifecycle_resources WHERE resource_key = ?",
                (effect_key,),
            )
        return MutationProvenance(
            bool(claim["createdByOperation"]), True, token
        )

    def _lifecycle_create_payload(
        self,
        payload: object,
        resource_row: sqlite3.Row | None,
        *,
        reuse_resource_payload: bool = False,
    ) -> dict[str, object]:
        from hyprial.agents.ports import BindAgentCommand, CreateAgentCommand

        if isinstance(payload, BindAgentCommand):
            agent = self.require(payload.actor)
            return {
                "actor": agent.uri,
                "harness": payload.harness,
                "runtime": payload.runtime,
                "sessionId": payload.session_id,
            }
        if resource_row is not None and reuse_resource_payload:
            old = json.loads(str(resource_row["payload"]))
            if isinstance(old, dict) and old:
                return old
        if isinstance(payload, CreateAgentCommand):
            name = self.normalize_actor(payload.name)
            return Agent(
                uri=self.uri_for(name),
                actor=name,
                owner=self.owner,
                machine=self.machine,
                cwd=payload.cwd,
                config=payload.config_payload(),
                provider=payload.provider,
                model=payload.model,
                capabilities=payload.capabilities_payload(),
                harness_args=dict(payload.harness_args),
                preferred_harness=payload.preferred_harness,
                created_at_ms=int(self._clock()),
            ).to_json()
        raise TypeError(f"unsupported lifecycle create payload: {type(payload).__name__}")

    def lifecycle_binding(self, actor: str) -> dict[str, object] | None:
        agent = self.get(actor)
        if agent is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"binding:{agent.uri}",),
            ).fetchone()
            if row is None or not bool(row["active"]):
                return None
            payload = json.loads(str(row["payload"]))
            return payload if isinstance(payload, dict) else None

    def retire_lifecycle_receipt(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT resource_token, retired FROM lifecycle_receipts "
                "WHERE attempt_token = ?",
                (attempt_token,),
            ).fetchone()
            if row is None or str(row["resource_token"]) != resource_token:
                return False
            if not bool(row["retired"]):
                self._db.execute(
                    "UPDATE lifecycle_receipts SET retired = 1 WHERE attempt_token = ?",
                    (attempt_token,),
                )
            return True

    def confirm_lifecycle_receipt_retired(
        self, attempt_token: str, resource_token: str
    ) -> bool:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT resource_token, retired FROM lifecycle_receipts "
                "WHERE attempt_token = ?",
                (attempt_token,),
            ).fetchone()
            if row is None:
                return False
            if str(row["resource_token"]) != resource_token or not bool(row["retired"]):
                raise ValueError("lifecycle receipt retirement mismatch")
            self._db.execute(
                "DELETE FROM lifecycle_receipts WHERE attempt_token = ?",
                (attempt_token,),
            )
            return True

    def lifecycle_effect_claims(self) -> list["DomainEffectClaim"]:
        """Every registry receipt as a U0c backfill claim.

        Registry receipts commit atomically with the agent mutation, so a
        receipt whose journal effect never completed is a mutation the dead
        generation applied and never reported -- the backfill turns it into
        compensable journal truth."""

        from hyprial.daemon.lifecycle_receipts import DomainEffectClaim

        with self._lock:
            rows = self._db.execute(
                "SELECT operation_id, attempt_token, changed, "
                "created_by_operation, resource_token FROM lifecycle_receipts"
            ).fetchall()
        return [
            DomainEffectClaim(
                str(row["operation_id"]),
                str(row["attempt_token"]),
                bool(row["changed"]),
                bool(row["created_by_operation"]),
                str(row["resource_token"]),
            )
            for row in rows
        ]

    def expire_lifecycle_receipts(self) -> int:
        """U0c: delete every lifecycle receipt at daemon startup.

        Registry receipts have no completion phase (an agent mutation and
        its receipt commit in one transaction), so anything still on disk
        at startup is a crash-window leftover of the retirement handshake
        from a dead generation -- worthless, because the restart never
        re-sends a previous generation's attempt tokens (interrupted sagas
        are compensated, not resumed).  Agents rows and resource fences
        are untouched: they are the durable world the restart's
        compensation converges against.  Returns how many rows went.
        """

        with self._lock, self._db:
            row = self._db.execute(
                "SELECT COUNT(*) FROM lifecycle_receipts"
            ).fetchone()
            count = int(row[0]) if row is not None else 0
            if count:
                self._db.execute("DELETE FROM lifecycle_receipts")
        return count

    def create(
        self,
        actor: str,
        *,
        cwd: str | None = None,
        config: object = None,
        provider: str | None = None,
        model: str | None = None,
        capabilities: Mapping[str, Any] | None = None,
        harness_args: Mapping[str, Iterable[str]] | None = None,
        preferred_harness: str | None = None,
    ) -> Agent:
        """Register a new agent, or fail loudly when the name is taken (A1).

        The failure is the point: two agents sharing one actor name mint one
        four-segment URI, and dispatch then hands every message to whichever
        connector sorts first while the other reports online forever.
        """

        name = self.native_actor(actor)
        agent = Agent(
            uri=self.uri_for(name),
            actor=name,
            owner=self.owner,
            machine=self.machine,
            cwd=cwd,
            config=normalize_agent_config(config),
            provider=provider,
            model=model,
            capabilities=normalize_capabilities(capabilities),
            harness_args=normalize_harness_args(harness_args),
            preferred_harness=preferred_harness,
            created_at_ms=int(self._clock()),
        )
        return self._create_record(agent)

    def create_transfer_hosted(
        self, actor: str, *, pinned_owner: str, cwd: str | None = None,
        harness_args: Mapping[str, Iterable[str]] | None = None,
        preferred_harness: str | None = None,
    ) -> Agent:
        """Receive-only insertion; never adopt or overwrite a same-name entity."""

        name = self.normalize_actor(actor)
        return self._create_record(Agent(
            uri=_uri().canonical_agent_uri(pinned_owner, self.machine, name),
            actor=name,
            owner=pinned_owner,
            machine=self.machine,
            hosted_by="transfer-receive",
            cwd=cwd,
            harness_args=normalize_harness_args(harness_args),
            preferred_harness=preferred_harness,
            created_at_ms=int(self._clock()),
        ))

    def create_host_invited(
        self, actor: str, *, pinned_owner: str, cwd: str | None = None,
        harness_args: Mapping[str, Iterable[str]] | None = None,
        preferred_harness: str | None = None,
    ) -> Agent:
        """Explicit host invitation; never adopt or overwrite an existing agent.

        The host asserts the visitor's owner string. This is not proof of a
        login, an OS isolation boundary, or permission to start a worker.
        """

        if (
            not isinstance(pinned_owner, str) or not pinned_owner
            or pinned_owner != pinned_owner.strip() or ":" in pinned_owner
        ):
            raise ValueError("owner must be non-empty, unpadded and contain no ':'")
        if pinned_owner == self.owner:
            raise ValueError("use agent create for the host's own agents")
        name = self.normalize_actor(actor)
        return self._create_record(Agent(
            uri=_uri().canonical_agent_uri(pinned_owner, self.machine, name),
            actor=name,
            owner=pinned_owner,
            machine=self.machine,
            hosted_by="host-invite",
            cwd=cwd,
            harness_args=normalize_harness_args(harness_args),
            preferred_harness=preferred_harness,
            created_at_ms=int(self._clock()),
        ))

    def _compensate_home_fs(self, attempt: HomeProvisioningAttempt, phase: str) -> None:
        """Remove a half-built home and record the failure when it stays.

        ``AgentHomeProvisioner.compensate`` answers False for a residue it
        must not touch (foreign token, non-empty tree, OSError).  That answer
        used to be dropped on the floor at every call site, leaving the
        residue discoverable only when the *next* create failed as
        ``unowned-residue``.  The warning is the observable surface; the
        durable revoked-resource row (written by the saga paths that have one)
        remains the authority.
        """

        if self._home is None:
            return
        if not self._home.compensate(attempt):
            _LOG.warning(
                "agent-home compensation left a residue: actor=%s "
                "resource_token=%s phase=%s; it surfaces as unowned-residue "
                "on the next create until cleaned",
                attempt.receipt.actor,
                attempt.receipt.resource_token,
                phase,
            )

    def prepare_create_record(self, agent: Agent) -> AgentHomeReservation:
        """Commit a durable create claim and return a filesystem-only plan."""

        self._validate_config_location(agent)
        home = self.home_provisioner
        claim_key = f"agent-home-effect:create:{agent.actor}"
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if self._db.execute(
                "SELECT 1 FROM agents WHERE actor = ?", (agent.actor,)
            ).fetchone() is not None:
                if agent.hosted_by == "host-invite":
                    # The public invitation path historically refuses an
                    # already-owned home with AGENT_ERROR. Actor preflight
                    # must preserve that code while rejecting before effects.
                    raise AgentError(
                        f"host invitation cannot adopt existing agent {agent.actor!r}"
                    )
                self._raise_agent_exists(agent)
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
                (claim_key,),
            ).fetchone()
            if pending is not None:
                payload = json.loads(str(pending["payload"]))
                if not isinstance(payload, dict):
                    raise AgentHomeError(
                        "invalid-registry-receipt", agent.actor, "create-replay"
                    )
                return self._create_reservation_from_payload(claim_key, payload)

            home_row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
            revoked: HomeReceipt | None = None
            prior_home: HomeReceipt | None = None
            prior_home_active: bool | None = None
            replacement: HomeReceipt
            record_row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-record:{agent.actor}",),
            ).fetchone()
            if home_row is not None:
                try:
                    prior = HomeReceipt.from_json(json.loads(str(home_row["payload"])))
                except (ValueError, json.JSONDecodeError) as error:
                    raise AgentHomeError(
                        "invalid-registry-receipt", agent.actor, "create-claim"
                    ) from error
                prior_home = prior
                prior_home_active = bool(home_row["active"])
                if bool(home_row["active"]):
                    raise AgentHomeError(
                        "operation-pending", agent.actor, "create-claim"
                    )
                if prior.status == "revoked":
                    revoked = prior
                    replacement = home.claim_receipt(
                        actor=agent.actor, entity_token=agent.entity_token
                    )
                elif prior.status == "ready" and record_row is not None:
                    pending_agent = Agent.from_json(
                        json.loads(str(record_row["payload"])), "agent create replay"
                    )
                    if prior.entity_token != pending_agent.entity_token:
                        raise AgentHomeError(
                            "receipt-mismatch", agent.actor, "create-replay"
                        )
                    agent = pending_agent
                    replacement = prior
                elif prior.status == "cleaned":
                    replacement = home.claim_receipt(
                        actor=agent.actor, entity_token=agent.entity_token
                    )
                else:
                    raise AgentHomeError(
                        "operation-pending", agent.actor, "create-claim"
                    )
            else:
                replacement = home.claim_receipt(
                    actor=agent.actor, entity_token=agent.entity_token
                )
            payload = {
                "operation": "create",
                "agent": agent.to_json(),
                "replacement": replacement.to_json(),
                "revoked": None if revoked is None else revoked.to_json(),
                "priorHome": (
                    None if prior_home is None else prior_home.to_json()
                ),
                "priorHomeActive": prior_home_active,
            }
            encoded = json.dumps(payload, sort_keys=True)
            self._record_external_resource_locked(
                f"agent-record:{agent.actor}", False, agent.to_json()
            )
            self._db.execute(
                "INSERT INTO lifecycle_resources VALUES(?, ?, 1, ?)",
                (claim_key, uuid.uuid4().hex, encoded),
            )
        plan: HomeFilesystemPlan = (
            ProvisionHome(replacement)
            if revoked is None
            else ReplaceHome(revoked, replacement)
        )
        return AgentHomeReservation(
            "create", agent, plan, claim_key, encoded, changed=True
        )

    def prepare_create_command(self, command: object) -> AgentHomeReservation:
        from .ports import (
            CreateAgentCommand,
            CreateHostInvitedAgentCommand,
            CreateTransferHostedAgentCommand,
        )

        if isinstance(command, CreateAgentCommand):
            name = self.native_actor(command.name)
            existing = self.get(name)
            if existing is not None:
                if not command.reuse_existing:
                    self._raise_agent_exists(existing)
                return self.prepare_existing_home(existing)
            agent = Agent(
                uri=self.uri_for(name),
                actor=name,
                owner=self.owner,
                machine=self.machine,
                cwd=command.cwd,
                config=command.config_payload(),
                provider=command.provider,
                model=command.model,
                capabilities=command.capabilities_payload(),
                harness_args=dict(command.harness_args),
                preferred_harness=(
                    command.preferred_harness or command.launch_harness
                ),
                created_at_ms=int(self._clock()),
            )
        elif isinstance(command, CreateTransferHostedAgentCommand):
            name = self.normalize_actor(command.name)
            agent = Agent(
                uri=_uri().canonical_agent_uri(
                    command.pinned_owner, self.machine, name
                ),
                actor=name,
                owner=command.pinned_owner,
                machine=self.machine,
                hosted_by="transfer-receive",
                cwd=command.cwd,
                harness_args=normalize_harness_args(dict(command.harness_args)),
                preferred_harness=command.preferred_harness,
                created_at_ms=int(self._clock()),
            )
        elif isinstance(command, CreateHostInvitedAgentCommand):
            if (
                not command.pinned_owner
                or command.pinned_owner != command.pinned_owner.strip()
                or ":" in command.pinned_owner
                or command.pinned_owner == self.owner
                or not command.entity_token
            ):
                raise ValueError("invalid host-invited owner")
            name = self.normalize_actor(command.name)
            agent = Agent(
                uri=_uri().canonical_agent_uri(
                    command.pinned_owner, self.machine, name
                ),
                actor=name,
                owner=command.pinned_owner,
                machine=self.machine,
                entity_token=command.entity_token,
                hosted_by="host-invite",
                cwd=command.cwd,
                harness_args=normalize_harness_args(dict(command.harness_args)),
                preferred_harness=command.preferred_harness,
                created_at_ms=int(self._clock()),
            )
        else:
            raise TypeError("unsupported Agent create command")
        return self.prepare_create_record(agent)

    def prepare_existing_home(self, agent: Agent) -> AgentHomeReservation:
        home = self.home_provisioner
        claim_key = f"agent-home-effect:ensure:{agent.actor}"
        with self._lock, self._db:
            current = self.require(agent.actor)
            if current.entity_token != agent.entity_token:
                raise AgentHomeError("create-fenced", agent.actor, "ensure-claim")
            row = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources WHERE resource_key=?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
            if row is not None and bool(row["active"]):
                receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
                if receipt.status != "ready" or receipt.entity_token != agent.entity_token:
                    raise AgentHomeError(
                        "receipt-mismatch", agent.actor, "ensure-claim"
                    )
                return AgentHomeReservation(
                    "create", agent, ProvisionHome(receipt), changed=False
                )
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key=?",
                (claim_key,),
            ).fetchone()
            if pending is not None:
                payload = json.loads(str(pending["payload"]))
                if not isinstance(payload, dict):
                    raise AgentHomeError(
                        "invalid-registry-receipt", agent.actor, "ensure-replay"
                    )
                receipt = HomeReceipt.from_json(payload.get("replacement"))
                encoded = json.dumps(payload, sort_keys=True)
                return AgentHomeReservation(
                    "create", agent, ProvisionHome(receipt), claim_key, encoded
                )
            if row is not None:
                prior = HomeReceipt.from_json(json.loads(str(row["payload"])))
                if prior.status != "cleaned":
                    raise AgentHomeError(
                        "operation-pending", agent.actor, "ensure-claim"
                    )
            receipt = home.claim_receipt(
                actor=agent.actor, entity_token=agent.entity_token
            )
            payload = {
                "operation": "ensure",
                "actor": agent.actor,
                "entityToken": agent.entity_token,
                "replacement": receipt.to_json(),
                "priorHome": (
                    None
                    if row is None
                    else json.loads(str(row["payload"]))
                ),
                "priorHomeActive": (
                    None if row is None else bool(row["active"])
                ),
            }
            encoded = json.dumps(payload, sort_keys=True)
            self._db.execute(
                "INSERT INTO lifecycle_resources VALUES(?, ?, 1, ?)",
                (claim_key, uuid.uuid4().hex, encoded),
            )
        return AgentHomeReservation(
            "create", agent, ProvisionHome(receipt), claim_key, encoded
        )

    def commit_existing_home(
        self, reservation: AgentHomeReservation, receipt: HomeReceipt
    ) -> Agent:
        agent = reservation.agent
        if agent is None or not isinstance(reservation.plan, ProvisionHome):
            raise TypeError("not an Agent ensure-home reservation")
        if receipt != reservation.plan.receipt:
            raise AgentHomeError("create-fenced", agent.actor, "ensure-completion")
        with self._lock, self._db:
            current = self.require(agent.actor)
            if current.entity_token != agent.entity_token:
                raise AgentHomeError("create-fenced", agent.actor, "ensure-commit")
            row = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources WHERE resource_key=?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
            if reservation.claim_key is None:
                if (
                    row is None
                    or not bool(row["active"])
                    or HomeReceipt.from_json(json.loads(str(row["payload"])))
                    != receipt
                ):
                    raise AgentHomeError(
                        "create-fenced", agent.actor, "ensure-commit"
                    )
                return current
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key=?",
                (reservation.claim_key,),
            ).fetchone()
            if pending is None or str(pending["payload"]) != reservation.claim_payload:
                raise AgentHomeError("create-fenced", agent.actor, "ensure-commit")
            claim_payload = json.loads(reservation.claim_payload)
            expected_home = claim_payload.get("priorHome")
            expected_active = claim_payload.get("priorHomeActive")
            if expected_home is None:
                if row is not None:
                    raise AgentHomeError(
                        "create-fenced", agent.actor, "ensure-commit"
                    )
            elif (
                row is None
                or bool(row["active"]) != bool(expected_active)
                or json.loads(str(row["payload"])) != expected_home
            ):
                raise AgentHomeError("create-fenced", agent.actor, "ensure-commit")
            self._record_home_resource_locked(receipt, True)
            self._db.execute(
                "DELETE FROM lifecycle_resources WHERE resource_key=?",
                (reservation.claim_key,),
            )
            return current

    @staticmethod
    def _create_reservation_from_payload(
        claim_key: str, payload: Mapping[str, object]
    ) -> AgentHomeReservation:
        agent = Agent.from_json(payload.get("agent"), "agent create claim")
        replacement = HomeReceipt.from_json(payload.get("replacement"))
        raw_revoked = payload.get("revoked")
        revoked = None if raw_revoked is None else HomeReceipt.from_json(raw_revoked)
        plan: HomeFilesystemPlan = (
            ProvisionHome(replacement)
            if revoked is None
            else ReplaceHome(revoked, replacement)
        )
        return AgentHomeReservation(
            "create",
            agent,
            plan,
            claim_key,
            json.dumps(dict(payload), sort_keys=True),
            changed=True,
        )

    def commit_create_record(
        self, reservation: AgentHomeReservation, receipt: HomeReceipt
    ) -> Agent:
        if reservation.operation != "create" or reservation.agent is None:
            raise TypeError("not an Agent create reservation")
        if reservation.claim_key is None or reservation.claim_payload is None:
            raise TypeError("Agent create reservation is not durable")
        agent = reservation.agent
        replacement = (
            reservation.plan.receipt
            if isinstance(reservation.plan, ProvisionHome)
            else reservation.plan.replacement
        )
        if receipt != replacement:
            raise AgentHomeError("create-fenced", agent.actor, "create-completion")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
                (reservation.claim_key,),
            ).fetchone()
            if pending is None or str(pending["payload"]) != reservation.claim_payload:
                raise AgentHomeError("create-fenced", agent.actor, "create-commit")
            if self._db.execute(
                "SELECT 1 FROM agents WHERE actor = ?", (agent.actor,)
            ).fetchone() is not None:
                raise AgentHomeError("create-fenced", agent.actor, "create-commit")
            claim_payload = json.loads(reservation.claim_payload)
            expected_home = claim_payload.get("priorHome")
            expected_active = claim_payload.get("priorHomeActive")
            home_row = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources WHERE resource_key=?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
            if expected_home is None:
                if home_row is not None:
                    raise AgentHomeError(
                        "create-fenced", agent.actor, "create-commit"
                    )
            elif (
                home_row is None
                or bool(home_row["active"]) != bool(expected_active)
                or json.loads(str(home_row["payload"])) != expected_home
            ):
                raise AgentHomeError("create-fenced", agent.actor, "create-commit")
            self._db.execute(*self._insert_statement(agent))
            self._record_external_resource_locked(
                f"agent-record:{agent.actor}", True, agent.to_json()
            )
            self._record_home_resource_locked(receipt, True)
            for adapter in agent.pinned_adapters:
                self._db.execute(
                    "INSERT INTO pins(adapter, agent) VALUES(?, ?)",
                    (adapter, agent.actor),
                )
            if agent.hosted_by == "host-invite":
                self._record_host_invite_locked(agent)
            self._db.execute(
                "DELETE FROM lifecycle_resources WHERE resource_key = ?",
                (reservation.claim_key,),
            )
        return agent

    def revoke_failed_create_record(
        self, reservation: AgentHomeReservation, receipt: HomeReceipt
    ) -> None:
        """Keep a failed commit's exact home under durable destroy custody.

        The create claim stays until ``commit_cleanup_home`` settles the
        filesystem effect.  No agent or grant-journal row is written here.
        """

        agent = reservation.agent
        expected_receipt = (
            reservation.plan.receipt
            if isinstance(reservation.plan, ProvisionHome)
            else reservation.plan.replacement
            if isinstance(reservation.plan, ReplaceHome)
            else None
        )
        if (
            reservation.operation != "create" or agent is None
            or reservation.claim_key is None or reservation.claim_payload is None
            or receipt != expected_receipt
            or receipt.actor != agent.actor or receipt.entity_token != agent.entity_token
        ):
            raise AgentHomeError("create-fenced", receipt.actor, "failed-create")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            claim = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key=?",
                (reservation.claim_key,),
            ).fetchone()
            record = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources WHERE resource_key=?",
                (f"agent-record:{agent.actor}",),
            ).fetchone()
            incumbent = self._db.execute(
                "SELECT 1 FROM agents WHERE actor=?", (agent.actor,)
            ).fetchone()
            if (
                claim is None or str(claim["payload"]) != reservation.claim_payload
                or record is None or bool(record["active"])
                or json.loads(str(record["payload"])) != agent.to_json()
                or incumbent is not None
            ):
                raise AgentHomeError("create-fenced", agent.actor, "failed-create")
            prior = json.loads(reservation.claim_payload).get("priorHome")
            home_row = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources WHERE resource_key=?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
            if prior is None:
                if home_row is not None:
                    raise AgentHomeError("create-fenced", agent.actor, "failed-create")
            elif (
                home_row is None or bool(home_row["active"])
                or json.loads(str(home_row["payload"])) != prior
            ):
                raise AgentHomeError("create-fenced", agent.actor, "failed-create")
            self._record_home_resource_locked(
                replace(receipt, status="revoked"), False
            )

    def abort_create_record(self, reservation: AgentHomeReservation) -> None:
        if reservation.claim_key is None or reservation.claim_payload is None:
            return
        agent = reservation.agent
        if agent is None:
            return
        with self._lock, self._db:
            pending = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
                (reservation.claim_key,),
            ).fetchone()
            if pending is None or str(pending["payload"]) != reservation.claim_payload:
                return
            self._db.execute(
                "DELETE FROM lifecycle_resources WHERE resource_key = ?",
                (reservation.claim_key,),
            )
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-record:{agent.actor}",),
            ).fetchone()
            if (
                row is not None
                and not bool(row["active"])
                and json.loads(str(row["payload"])) == agent.to_json()
            ):
                self._db.execute(
                    "DELETE FROM lifecycle_resources WHERE resource_key = ?",
                    (f"agent-record:{agent.actor}",),
                )

    def prepare_destroy_record(
        self, actor: str, expected_entity_token: str | None = None
    ) -> tuple[bool, AgentHomeReservation | None]:
        name = self.local_actor(actor)
        if name is None:
            return False, None
        with self._lock, self._db:
            previous = self._db.execute(
                "SELECT * FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            if previous is None:
                return False, None
            prior_agent = self._row_agent(
                previous, self._pinned_adapters_locked(name)
            )
            if (
                expected_entity_token is not None
                and prior_agent.entity_token != expected_entity_token
            ):
                raise AgentEntityConflict(
                    f"agent {actor!r} changed since destroy was requested"
                )
            self._db.execute("DELETE FROM agents WHERE actor = ?", (name,))
            revoked = self._revoke_home_locked(prior_agent)
            self._record_external_resource_locked(
                f"agent-record:{name}", False, prior_agent.to_json()
            )
        with self._lock:
            self._restore_dispositions.pop(name, None)
            self._agent_blocks.pop(name, None)
        if revoked is None:
            return True, None
        return True, AgentHomeReservation(
            "destroy", prior_agent, CleanupHome(revoked), changed=True
        )

    def prepare_destroy_settlement(
        self, actor: str, expected_entity_token: str
    ) -> tuple[str | None, AgentHomeReservation | None]:
        """Settle one exact destroy from live identity or durable revoke facts.

        ``None`` disposition means CleanupHome still owns settlement.  A bare
        missing Agent is never success: replay requires the inactive
        ``agent-record`` for the expected incarnation and, when a home existed,
        its exact revoked/cleaned receipt.
        """

        current = self.get(actor)
        if current is not None:
            if current.entity_token != expected_entity_token:
                return "stale-incarnation", None
            changed, reservation = self.prepare_destroy_record(
                actor, expected_entity_token
            )
            assert changed
            return ("destroyed", None) if reservation is None else (None, reservation)

        name = self.local_actor(actor)
        if name is None:
            raise AgentDestroySettlementUnknown(
                f"no local Agent settlement identity for {actor!r}"
            )
        claim_key = f"agent-home-effect:create:{name}"
        with self._lock:
            record = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources "
                "WHERE resource_key=?",
                (f"agent-record:{name}",),
            ).fetchone()
            home = self._db.execute(
                "SELECT active,payload FROM lifecycle_resources "
                "WHERE resource_key=?",
                (f"agent-home:{name}",),
            ).fetchone()
            claim = self._db.execute(
                "SELECT payload FROM lifecycle_resources WHERE resource_key=?",
                (claim_key,),
            ).fetchone()
        if record is None or bool(record["active"]):
            raise AgentDestroySettlementUnknown(
                f"no durable destroy settlement for {name!r}"
            )
        try:
            prior_agent = Agent.from_json(
                json.loads(str(record["payload"])), "destroy settlement"
            )
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentHomeError(
                "invalid-registry-receipt", name, "destroy-settlement"
            ) from error
        if prior_agent.entity_token != expected_entity_token:
            return "stale-incarnation", None
        if claim is not None and home is None and prior_agent.hosted_by == "host-invite":
            # A process can die after the filesystem effect but before the
            # owner promotes its create claim to a revoked home receipt. The
            # claim is the durable identity; cleanup handles either a still
            # matching mirror or an absent, never-created home.
            payload = json.loads(str(claim["payload"]))
            if not isinstance(payload, dict) or payload.get("priorHome") is not None:
                raise AgentDestroySettlementUnknown(
                    f"Agent {name!r} failed create claim is not a fresh invite"
                )
            reservation = self._create_reservation_from_payload(claim_key, payload)
            replacement = (
                reservation.plan.receipt
                if isinstance(reservation.plan, ProvisionHome)
                else None
            )
            if replacement is None or reservation.agent is None or (
                reservation.agent.entity_token != expected_entity_token
            ):
                raise AgentDestroySettlementUnknown(
                    f"Agent {name!r} failed create claim changed incarnation"
                )
            self.revoke_failed_create_record(reservation, replacement)
            return self.prepare_destroy_settlement(actor, expected_entity_token)
        if home is None:
            return "already-cleaned", None
        if bool(home["active"]):
            raise AgentDestroySettlementUnknown(
                f"Agent {name!r} has an active home without a live identity"
            )
        try:
            receipt = HomeReceipt.from_json(json.loads(str(home["payload"])))
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentHomeError(
                "invalid-registry-receipt", name, "destroy-settlement"
            ) from error
        if receipt.entity_token != expected_entity_token:
            raise AgentHomeError(
                "cleanup-fenced", name, "destroy-settlement"
            )
        if receipt.status == "cleaned":
            return "already-cleaned", None
        if receipt.status != "revoked":
            raise AgentDestroySettlementUnknown(
                f"Agent {name!r} home is not revoked or cleaned"
            )
        claim_payload = None if claim is None else str(claim["payload"])
        if claim_payload is not None:
            payload = json.loads(claim_payload)
            if (
                not isinstance(payload, dict)
                or Agent.from_json(payload.get("agent"), "failed create claim").entity_token
                != expected_entity_token
                or HomeReceipt.from_json(payload.get("replacement")).resource_token
                != receipt.resource_token
            ):
                raise AgentDestroySettlementUnknown(
                    f"Agent {name!r} create claim does not own home cleanup"
                )
        return None, AgentHomeReservation(
            "settle-destroy", prior_agent, CleanupHome(receipt),
            claim_key if claim_payload is not None else None,
            claim_payload,
            changed=True,
        )

    def prepare_cleanup_revoked_home(
        self, actor: str
    ) -> AgentHomeReservation | None:
        if self._home is None:
            return None
        name = self.local_actor(actor)
        if name is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{name}",),
            ).fetchone()
            incumbent = self._db.execute(
                "SELECT 1 FROM agents WHERE actor = ?", (name,)
            ).fetchone()
        if row is None or bool(row["active"]) or incumbent is not None:
            return None
        try:
            receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentHomeError(
                "invalid-registry-receipt", name, "cleanup-registry"
            ) from error
        if receipt.actor != name:
            raise AgentHomeError("receipt-mismatch", name, "cleanup-registry")
        if receipt.status != "revoked":
            return None
        return AgentHomeReservation(
            "cleanup-home", None, CleanupHome(receipt), changed=True
        )

    def commit_cleanup_home(
        self, reservation: AgentHomeReservation, cleaned: HomeReceipt
    ) -> None:
        plan = reservation.plan
        if not isinstance(plan, CleanupHome):
            raise TypeError("not an Agent cleanup reservation")
        prior = plan.receipt
        if (
            cleaned.actor != prior.actor or cleaned.status != "cleaned"
            or cleaned.entity_token != prior.entity_token
            or cleaned.path != prior.path or cleaned.owner_uid != prior.owner_uid
        ):
            raise AgentHomeError("cleanup-fenced", prior.actor, "cleanup-completion")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{prior.actor}",),
            ).fetchone()
            incumbent = self._db.execute(
                "SELECT 1 FROM agents WHERE actor = ?", (prior.actor,)
            ).fetchone()
            if row is None or bool(row["active"]) or incumbent is not None:
                raise AgentHomeError("cleanup-fenced", prior.actor, "cleanup-commit")
            current = HomeReceipt.from_json(json.loads(str(row["payload"])))
            if current != prior:
                raise AgentHomeError("cleanup-fenced", prior.actor, "cleanup-commit")
            if reservation.claim_key is not None:
                claim = self._db.execute(
                    "SELECT payload FROM lifecycle_resources WHERE resource_key=?",
                    (reservation.claim_key,),
                ).fetchone()
                if (
                    claim is None
                    or str(claim["payload"]) != reservation.claim_payload
                ):
                    raise AgentHomeError(
                        "cleanup-fenced", prior.actor, "cleanup-commit"
                    )
            self._record_home_resource_locked(cleaned, False)
            if reservation.claim_key is not None:
                self._db.execute(
                    "DELETE FROM lifecycle_resources WHERE resource_key=?",
                    (reservation.claim_key,),
                )

    def _create_record(self, agent: Agent) -> Agent:
        self._validate_config_location(agent)
        resume_claim: HomeReceipt | None = None
        if self._home is not None:
            with self._lock:
                home_row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-home:{agent.actor}",),
                ).fetchone()
                record_row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-record:{agent.actor}",),
                ).fetchone()
                incumbent = self._db.execute(
                    "SELECT 1 FROM agents WHERE actor = ?", (agent.actor,)
                ).fetchone()
            if (
                incumbent is None
                and home_row is not None
                and not bool(home_row["active"])
                and record_row is not None
                and not bool(record_row["active"])
            ):
                try:
                    pending_home = HomeReceipt.from_json(
                        json.loads(str(home_row["payload"]))
                    )
                    pending_agent_payload = json.loads(str(record_row["payload"]))
                    pending_agent = Agent.from_json(
                        pending_agent_payload, "agent create replay"
                    )
                except (ValueError, json.JSONDecodeError):
                    pending_home = None
                if (
                    pending_home is not None
                    and pending_home.status == "ready"
                    and pending_home.actor == agent.actor
                    and pending_home.entity_token == pending_agent.entity_token
                ):
                    # Admission succeeded before a crash/timeout. Replays
                    # converge that exact incarnation instead of allocating a
                    # new token or deleting the already-created filesystem.
                    agent = pending_agent
                    resume_claim = pending_home
        # A daemon may have crashed after committing destroy's durable revoke
        # but before finishing filesystem retirement.  The revoked receipt,
        # not the requested name, authorizes this retry cleanup.
        if resume_claim is None:
            self.cleanup_revoked_home(agent.actor)
        if self._home is None:
            with self._lock:
                try:
                    with self._db:
                        self._db.execute(*self._insert_statement(agent))
                        self._record_external_resource_locked(
                            f"agent-record:{agent.actor}", True, agent.to_json()
                        )
                        for adapter in agent.pinned_adapters:
                            self._db.execute(
                                "INSERT INTO pins(adapter, agent) VALUES(?, ?)",
                                (adapter, agent.actor),
                            )
                        if agent.hosted_by == "host-invite":
                            self._record_host_invite_locked(agent)
                except sqlite3.IntegrityError as error:
                    self._raise_agent_exists(agent, error)
            return agent

        claim = resume_claim or self._home.claim_receipt(
            actor=agent.actor, entity_token=agent.entity_token
        )
        if resume_claim is None:
            with self._lock:
                try:
                    with self._db:
                        self._db.execute("BEGIN IMMEDIATE")
                        if self._db.execute(
                            "SELECT 1 FROM agents WHERE actor = ?", (agent.actor,)
                        ).fetchone() is not None:
                            self._raise_agent_exists(agent)
                        row = self._db.execute(
                            "SELECT active, payload FROM lifecycle_resources "
                            "WHERE resource_key = ?",
                            (f"agent-home:{agent.actor}",),
                        ).fetchone()
                        if row is not None:
                            try:
                                prior = HomeReceipt.from_json(
                                    json.loads(str(row["payload"]))
                                )
                            except (ValueError, json.JSONDecodeError) as error:
                                raise AgentHomeError(
                                    "invalid-registry-receipt",
                                    agent.actor,
                                    "create-claim",
                                ) from error
                            if bool(row["active"]) or prior.status != "cleaned":
                                raise AgentHomeError(
                                    "operation-pending", agent.actor, "create-claim"
                                )
                        # The inactive exact receipt is the durable authority for
                        # the filesystem lane. A retry cannot allocate a new token
                        # or delete a newer incarnation behind this fence.
                        self._record_home_resource_locked(claim, False)
                        self._record_external_resource_locked(
                            f"agent-record:{agent.actor}", False, agent.to_json()
                        )
                except sqlite3.IntegrityError as error:
                    self._raise_agent_exists(agent, error)

        home_attempt: HomeProvisioningAttempt | None = None
        try:
            home_attempt = self._home.provision_claimed(claim)
            with self._lock, self._db:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-home:{agent.actor}",),
                ).fetchone()
                incumbent = self._db.execute(
                    "SELECT 1 FROM agents WHERE actor = ?", (agent.actor,)
                ).fetchone()
                if row is None or bool(row["active"]) or incumbent is not None:
                    raise AgentHomeError(
                        "create-fenced", agent.actor, "create-commit"
                    )
                try:
                    current = HomeReceipt.from_json(json.loads(str(row["payload"])))
                except (ValueError, json.JSONDecodeError) as error:
                    raise AgentHomeError(
                        "invalid-registry-receipt", agent.actor, "create-commit"
                    ) from error
                if current != claim:
                    raise AgentHomeError(
                        "create-fenced", agent.actor, "create-commit"
                    )
                self._db.execute(*self._insert_statement(agent))
                self._record_external_resource_locked(
                    f"agent-record:{agent.actor}", True, agent.to_json()
                )
                self._record_home_resource_locked(claim, True)
                for adapter in agent.pinned_adapters:
                    self._db.execute(
                        "INSERT INTO pins(adapter, agent) VALUES(?, ?)",
                        (adapter, agent.actor),
                    )
                if agent.hosted_by == "host-invite":
                    self._record_host_invite_locked(agent)
        except BaseException as error:
            if home_attempt is not None:
                self._compensate_home_fs(home_attempt, "create-commit")
            with self._lock, self._db:
                row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-home:{agent.actor}",),
                ).fetchone()
                if row is not None and not bool(row["active"]):
                    try:
                        current = HomeReceipt.from_json(json.loads(str(row["payload"])))
                    except (ValueError, json.JSONDecodeError):
                        current = None
                    if current == claim:
                        self._record_home_resource_locked(
                            replace(claim, status="revoked"), False
                        )
            if isinstance(error, sqlite3.IntegrityError):
                self._raise_agent_exists(agent, error)
            raise
        return agent

    def _raise_agent_exists(
        self, agent: Agent, error: BaseException | None = None
    ) -> None:
        message = (
            f"the name {agent.actor!r} is already taken on this node "
            f"({self.owner}@{self.machine}) — {agent.uri} exists "
            f"({self.database}). One name is one agent, whether or "
            f"not anything is currently running under it. To reuse "
            f"the name, destroy that agent first ('hyprial agent destroy "
            f"{agent.actor}', which is irreversible); to run this "
            f"agent on a different harness, just start it there — "
            f"that is a rebinding of the same agent, not a new one."
        )
        if error is None:
            raise AgentExistsError(message)
        raise AgentExistsError(message) from error

    def _validate_config_location(self, agent: Agent) -> None:
        agent_home = (
            None
            if self._home is None
            else self._home.agents_root / agent.actor
        )
        validate_agent_config_location(
            agent.config,
            agent_home=agent_home,
            cwd=agent.cwd,
        )

    def ensure_home(self, actor: str) -> HomeReceipt:
        """Provision or validate the current entity's home in two phases."""

        agent = self.require(actor)
        if self._home is None:
            raise AgentHomeError("not-configured", agent.actor, "provision")
        with self._lock:
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
        if row is not None:
            try:
                receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
            except (ValueError, json.JSONDecodeError) as error:
                raise AgentHomeError(
                    "invalid-registry-receipt", agent.actor, "ensure"
                ) from error
            if bool(row["active"]) and receipt.status == "ready":
                self._home.validate(receipt)
                return receipt
            raise AgentHomeError("operation-pending", agent.actor, "ensure")

        claim = self._home.claim_receipt(
            actor=agent.actor, entity_token=agent.entity_token
        )
        with self._lock, self._db:
            current = self.require(agent.actor)
            if current.entity_token != agent.entity_token:
                raise AgentHomeError("create-fenced", agent.actor, "ensure-claim")
            row = self._db.execute(
                "SELECT 1 FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
            if row is not None:
                raise AgentHomeError("operation-pending", agent.actor, "ensure-claim")
            self._record_home_resource_locked(claim, False)
        attempt: HomeProvisioningAttempt | None = None
        try:
            attempt = self._home.provision_claimed(claim)
            with self._lock, self._db:
                current = self.require(agent.actor)
                row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-home:{agent.actor}",),
                ).fetchone()
                if current.entity_token != agent.entity_token or row is None or bool(row["active"]):
                    raise AgentHomeError("create-fenced", agent.actor, "ensure-commit")
                stored = HomeReceipt.from_json(json.loads(str(row["payload"])))
                if stored != claim:
                    raise AgentHomeError("create-fenced", agent.actor, "ensure-commit")
                self._record_home_resource_locked(claim, True)
            return claim
        except BaseException:
            removed = False
            if attempt is not None:
                removed = self._home.compensate(attempt)
                if not removed:
                    _LOG.warning(
                        "agent-home compensation left a residue: actor=%s "
                        "resource_token=%s phase=ensure; it surfaces as "
                        "unowned-residue on the next create until cleaned",
                        attempt.receipt.actor,
                        attempt.receipt.resource_token,
                    )
            with self._lock, self._db:
                row = self._db.execute(
                    "SELECT active, payload FROM lifecycle_resources "
                    "WHERE resource_key = ?",
                    (f"agent-home:{agent.actor}",),
                ).fetchone()
                if row is not None and not bool(row["active"]):
                    try:
                        stored = HomeReceipt.from_json(json.loads(str(row["payload"])))
                    except (ValueError, json.JSONDecodeError):
                        stored = None
                    if stored == claim:
                        self._record_home_resource_locked(
                            replace(claim, status="cleaned" if removed else "revoked"),
                            False,
                        )
            raise

    def home_receipt(
        self,
        actor: str,
        *,
        require_ready: bool = True,
        validate_mirror: bool = True,
    ) -> HomeReceipt:
        """Read home authority from SQLite, optionally validating its mirror."""

        agent = self.require(actor)
        with self._lock:
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{agent.actor}",),
            ).fetchone()
        if row is None:
            raise AgentHomeError("not-provisioned", agent.actor, "read-registry")
        try:
            receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentHomeError("invalid-registry-receipt", agent.actor, "read-registry") from error
        if receipt.entity_token != agent.entity_token:
            raise AgentHomeError("receipt-mismatch", agent.actor, "read-registry")
        if require_ready and (not bool(row["active"]) or receipt.status != "ready"):
            raise AgentHomeError("revoked", agent.actor, "read-registry")
        if require_ready and validate_mirror:
            if self._home is None:
                # Not ``assert``: this is control flow (a registry opened
                # without a provisioner can still hold home rows), and an
                # assert vanishes under ``python -O`` — the typed error keeps
                # the fence loud in every build.
                raise AgentHomeError("not-configured", agent.actor, "validate")
            self._home.validate(receipt)
        return receipt

    def confirm_home_authority(self, receipt: HomeReceipt) -> None:
        """Fence publication against entity or resource-token replacement."""

        current = self.home_receipt(
            receipt.actor, require_ready=True, validate_mirror=False
        )
        if (
            current.entity_token != receipt.entity_token
            or current.resource_token != receipt.resource_token
            or current.path != receipt.path
        ):
            raise AgentHomeError(
                "receipt-mismatch", receipt.actor, "confirm-home-authority"
            )

    def workspace_path(self, actor: str) -> Path:
        """Return the private default workspace path without creating it."""

        if self._home is None:
            raise AgentHomeError("not-configured", actor, "workspace")
        name = self.native_actor(actor)
        return self._home.agents_root / name / "workspace"

    def ensure_workspace(self, actor: str) -> Path:
        """Create the current incarnation's workspace under its home receipt."""

        if self._home is None:
            raise AgentHomeError("not-configured", actor, "workspace")
        receipt = self.home_receipt(actor)
        return self._home.ensure_workspace(receipt)

    def workspace_summary(self, actor: str) -> WorkspaceSummary:
        """Inventory the current incarnation's workspace without following links."""

        if self._home is None:
            raise AgentHomeError("not-configured", actor, "workspace-summary")
        receipt = self.home_receipt(actor)
        return self._home.workspace_summary(receipt)

    def cleanup_home(self, actor: str, *, expected_token: str) -> HomeReceipt:
        """Clean a destroyed/revoked home only under its durable token fence."""

        if self._home is None:
            raise AgentHomeError("not-configured", actor, "cleanup")
        name = self.local_actor(actor)
        if name is None:
            raise AgentHomeError("cleanup-fenced", actor, "cleanup-registry")
        # Phase 1 is a read-only durable claim check.  Filesystem traversal is
        # deliberately outside both the registry lock and SQLite transaction;
        # phase 2 below revalidates the exact token and absent incarnation
        # before publishing the cleaned receipt.
        with self._lock:
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{name}",),
            ).fetchone()
            incumbent = self._db.execute(
                "SELECT 1 FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            if row is None or bool(row["active"]) or incumbent is not None:
                raise AgentHomeError("cleanup-fenced", name, "cleanup-registry")
            try:
                receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
            except (ValueError, json.JSONDecodeError) as error:
                raise AgentHomeError("invalid-registry-receipt", name, "cleanup-registry") from error
            if receipt.actor != name:
                raise AgentHomeError("receipt-mismatch", name, "cleanup-registry")
        cleaned = self._home.cleanup(receipt, expected_token=expected_token)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{name}",),
            ).fetchone()
            incumbent = self._db.execute(
                "SELECT 1 FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            if row is None or bool(row["active"]) or incumbent is not None:
                raise AgentHomeError("cleanup-fenced", name, "cleanup-commit")
            try:
                current = HomeReceipt.from_json(json.loads(str(row["payload"])))
            except (ValueError, json.JSONDecodeError) as error:
                raise AgentHomeError(
                    "invalid-registry-receipt", name, "cleanup-commit"
                ) from error
            if current != receipt or current.resource_token != expected_token:
                raise AgentHomeError("cleanup-fenced", name, "cleanup-commit")
            self._record_home_resource_locked(cleaned, False)
            return cleaned

    def cleanup_revoked_home(self, actor: str) -> HomeReceipt | None:
        """Resume destroy cleanup only when a durable revoked receipt exists.

        This is the crash-convergence entry point used by both a subsequent
        create and a repeated destroy.  Merely knowing the actor name never
        authorizes deletion: a missing, active, cleaned, malformed, or
        still-owned lifecycle row is either a no-op or a loud refusal.
        """

        if self._home is None:
            return None
        name = self.local_actor(actor)
        if name is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
                (f"agent-home:{name}",),
            ).fetchone()
            if row is None or bool(row["active"]):
                return None
            try:
                receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
            except (ValueError, json.JSONDecodeError) as error:
                raise AgentHomeError(
                    "invalid-registry-receipt", name, "cleanup-registry"
                ) from error
            if receipt.actor != name:
                raise AgentHomeError("receipt-mismatch", name, "cleanup-registry")
            if receipt.status != "revoked":
                return None
        return self.cleanup_home(name, expected_token=receipt.resource_token)

    @contextmanager
    def _home_transaction(self) -> Iterator[list[HomeProvisioningAttempt]]:
        """Serialize DB/FS reservations and compensate caught SQL failures."""

        attempts: list[HomeProvisioningAttempt] = []
        with self._lock:
            try:
                with self._db:
                    self._db.execute("BEGIN IMMEDIATE")
                    yield attempts
            except BaseException:
                if self._home is not None:
                    for attempt in reversed(attempts):
                        self._compensate_home_fs(attempt, "saga-rollback")
                raise

    def _provision_home_locked(
        self, agent: Agent, *, allow_revoked: bool = False
    ) -> HomeProvisioningAttempt | None:
        if self._home is None:
            return None
        row = self._db.execute(
            "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
            (f"agent-home:{agent.actor}",),
        ).fetchone()
        incumbent = None
        if row is not None:
            try:
                incumbent = HomeReceipt.from_json(json.loads(str(row["payload"])))
            except (ValueError, json.JSONDecodeError) as error:
                raise AgentHomeError(
                    "invalid-registry-receipt", agent.actor, "reserve"
                ) from error
        return self._home.provision(
            actor=agent.actor,
            entity_token=agent.entity_token,
            incumbent=incumbent,
            allow_revoked=allow_revoked,
        )

    def _record_home_resource_locked(self, receipt: HomeReceipt, active: bool) -> None:
        self._db.execute(
            "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
            "ON CONFLICT(resource_key) DO UPDATE SET "
            "resource_token=excluded.resource_token, active=excluded.active, "
            "payload=excluded.payload",
            (
                f"agent-home:{receipt.actor}",
                receipt.resource_token,
                int(active),
                json.dumps(receipt.to_json(), sort_keys=True),
            ),
        )

    def _revoke_home_locked(self, agent: Agent) -> HomeReceipt | None:
        row = self._db.execute(
            "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
            (f"agent-home:{agent.actor}",),
        ).fetchone()
        if row is None:
            return None
        try:
            receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentHomeError(
                "invalid-registry-receipt", agent.actor, "revoke"
            ) from error
        if receipt.entity_token != agent.entity_token:
            raise AgentHomeError("receipt-mismatch", agent.actor, "revoke")
        revoked = replace(receipt, status="revoked")
        self._record_home_resource_locked(revoked, False)
        return revoked

    def _compensate_home_locked(self, agent: Agent) -> None:
        """Remove a half-built home when its create saga rolls back."""

        if self._home is None:
            return
        row = self._db.execute(
            "SELECT payload FROM lifecycle_resources WHERE resource_key = ?",
            (f"agent-home:{agent.actor}",),
        ).fetchone()
        if row is None:
            return
        try:
            receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
        except (ValueError, json.JSONDecodeError) as error:
            raise AgentHomeError(
                "invalid-registry-receipt", agent.actor, "compensate"
            ) from error
        if receipt.entity_token != agent.entity_token:
            raise AgentHomeError("receipt-mismatch", agent.actor, "compensate")
        removed = self._home.compensate(HomeProvisioningAttempt(receipt, True))
        # A non-empty home means files this rollback never placed still exist;
        # keep a diagnosable revoked residue rather than dropping authority for
        # on-disk state.  Either way the resource goes inactive so a fresh
        # create re-provisions from zero.
        status = "cleaned" if removed else "revoked"
        self._record_home_resource_locked(replace(receipt, status=status), False)

    def save(self, agent: Agent) -> Agent:
        """Write the record; the caller owns the merge.

        ``pinned_adapters`` is deliberately not written here — pins change
        only through :meth:`pin`/:meth:`unpin`, so a stale copy of the record
        can never clobber a binding.  The upsert updates in place (never
        DELETE+INSERT, which would fire the pin cascade).
        """

        self._validate_config_location(agent)
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT owner, machine, uri, hosted_by, entity_token, last_active_at_ms "
                "FROM agents WHERE actor = ?",
                (agent.actor,),
            ).fetchone()
            if agent.hosted_by is not None or (
                existing is not None and existing["hosted_by"] is not None
            ):
                identity = (agent.owner, agent.machine, agent.uri, agent.hosted_by)
                stored_identity = (
                    None
                    if existing is None
                    else (
                        str(existing["owner"]),
                        str(existing["machine"]),
                        str(existing["uri"]),
                        existing["hosted_by"],
                    )
                )
                if stored_identity != identity:
                    raise AgentError(
                        "save cannot authorize or change hosted identity; "
                        "hosting requires transfer receive"
                    )
            if existing is not None and str(existing["entity_token"]) != agent.entity_token:
                raise AgentError(
                    "save cannot change an agent incarnation; use the authority rotation API"
                )
            if existing is not None:
                agent = replace(
                    agent,
                    last_active_at_ms=(
                        None
                        if existing["last_active_at_ms"] is None
                        else int(existing["last_active_at_ms"])
                    ),
                )
            statement, values = self._insert_statement(agent)
            self._db.execute(
                statement
                + " ON CONFLICT(actor) DO UPDATE SET "
                + ", ".join(
                    f"{column} = excluded.{column}"
                    for column in (
                        "owner",
                        "machine",
                        "uri",
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
                ),
                values,
            )
        return agent

    @staticmethod
    def _insert_statement(agent: Agent) -> tuple[str, tuple[Any, ...]]:
        return (
            "INSERT INTO agents (actor, owner, machine, uri, entity_token, cwd, config, provider, "
            "model, capabilities, harness_args, preferred_harness, "
            "last_harness, last_session_id, last_active_at_ms, created_at_ms, hosted_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent.actor,
                agent.owner,
                agent.machine,
                agent.uri,
                agent.entity_token,
                agent.cwd,
                None
                if agent.config is None
                else json.dumps(agent.config.to_json(), sort_keys=True),
                agent.provider,
                agent.model,
                json.dumps(dict(agent.capabilities), sort_keys=True),
                json.dumps(
                    {
                        harness: list(args)
                        for harness, args in agent.harness_args.items()
                    },
                    sort_keys=True,
                ),
                agent.preferred_harness,
                agent.last_harness,
                agent.last_session_id,
                agent.last_active_at_ms,
                agent.created_at_ms,
                agent.hosted_by,
            ),
        )

    def record_activity(self, actor: str) -> bool:
        """Best-effort durable activity mark, coalesced per agent per minute.

        The in-memory fence avoids even issuing SQL for repeated events in one
        daemon. The conditional update is the cross-process fence, so two
        registry instances racing inside the same window still change one row
        at most once. Unknown or foreign actors are ignored.
        """

        name = self.local_actor(actor)
        if name is None:
            return False
        now_ms = self.now_ms()
        with self._lock:
            previous = self._last_activity_write.get(name)
            if (
                previous is not None
                and now_ms - previous < AGENT_ACTIVITY_WRITE_INTERVAL_MS
            ):
                return False
            with self._db:
                cursor = self._db.execute(
                    "UPDATE agents SET last_active_at_ms = ? WHERE actor = ? "
                    "AND (last_active_at_ms IS NULL OR last_active_at_ms <= ?)",
                    (now_ms, name, now_ms - AGENT_ACTIVITY_WRITE_INTERVAL_MS),
                )
            if cursor.rowcount != 1:
                row = self._db.execute(
                    "SELECT last_active_at_ms FROM agents WHERE actor = ?", (name,)
                ).fetchone()
                if row is not None and row["last_active_at_ms"] is not None:
                    self._last_activity_write[name] = int(row["last_active_at_ms"])
                return False
            self._last_activity_write[name] = now_ms
            return True

    def restore_disposition(
        self, actor: str | Agent, *, desired_generation: str | None = None
    ) -> RestoreDisposition | None:
        agent = actor if isinstance(actor, Agent) else self.get(actor)
        if agent is None:
            return None
        with self._lock:
            disposition = self._restore_dispositions.get(agent.actor)
            if disposition is None:
                return None
            if (
                disposition.entity_token != agent.entity_token
                or (
                    desired_generation is not None
                    and disposition.desired_generation != desired_generation
                )
            ):
                with self._db:
                    self._db.execute(
                        "DELETE FROM agent_restore_dispositions WHERE actor = ?",
                        (agent.actor,),
                    )
                self._restore_dispositions.pop(agent.actor, None)
                return None
            return disposition

    def suppress_restore(
        self,
        actor: str,
        *,
        desired_generation: str,
        last_active_at_ms: int | None,
        idle_age_ms: int | None,
        restore_threshold_ms: int,
        restore_override: str = "none",
        activity_unknown: bool = False,
    ) -> RestoreDisposition:
        agent = self.require(actor)
        recorded_at_ms = self.now_ms()
        with self._lock, self._db:
            prior = self._restore_dispositions.get(agent.actor)
            disposition_token = (
                prior.disposition_token
                if prior is not None
                and prior.entity_token == agent.entity_token
                and prior.desired_generation == desired_generation
                else uuid.uuid4().hex
            )
            self._db.execute(
                "INSERT INTO agent_restore_dispositions "
                "(actor,entity_token,desired_generation,disposition_token,status,last_active_at_ms,"
                "idle_age_ms,restore_threshold_ms,restore_override,activity_unknown,"
                "recorded_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(actor) DO UPDATE SET "
                "entity_token=excluded.entity_token,"
                "desired_generation=excluded.desired_generation,"
                "disposition_token=excluded.disposition_token,"
                "status=excluded.status,"
                "last_active_at_ms=excluded.last_active_at_ms,"
                "idle_age_ms=excluded.idle_age_ms,"
                "restore_threshold_ms=excluded.restore_threshold_ms,"
                "restore_override=excluded.restore_override,"
                "activity_unknown=excluded.activity_unknown,"
                "recorded_at_ms=excluded.recorded_at_ms",
                (
                    agent.actor,
                    agent.entity_token,
                    desired_generation,
                    disposition_token,
                    "idle-suppressed",
                    last_active_at_ms,
                    idle_age_ms,
                    restore_threshold_ms,
                    restore_override,
                    int(activity_unknown),
                    recorded_at_ms,
                ),
            )
            disposition = RestoreDisposition(
                agent.actor,
                agent.entity_token,
                desired_generation,
                disposition_token,
                "idle-suppressed",
                last_active_at_ms,
                idle_age_ms,
                restore_threshold_ms,
                restore_override,
                activity_unknown,
                recorded_at_ms,
            )
            self._restore_dispositions[agent.actor] = disposition
        return disposition

    @staticmethod
    def _restore_disposition_row(row: sqlite3.Row) -> RestoreDisposition:
        return RestoreDisposition(
            actor=str(row["actor"]),
            entity_token=str(row["entity_token"]),
            desired_generation=str(row["desired_generation"]),
            disposition_token=str(row["disposition_token"]),
            status=str(row["status"]),
            last_active_at_ms=(
                None
                if row["last_active_at_ms"] is None
                else int(row["last_active_at_ms"])
            ),
            idle_age_ms=(
                None if row["idle_age_ms"] is None else int(row["idle_age_ms"])
            ),
            restore_threshold_ms=int(row["restore_threshold_ms"]),
            restore_override=str(row["restore_override"]),
            activity_unknown=bool(row["activity_unknown"]),
            recorded_at_ms=int(row["recorded_at_ms"]),
        )

    @staticmethod
    def _agent_block_row(row: sqlite3.Row) -> AgentBlock:
        return AgentBlock(
            actor=str(row["actor"]),
            entity_token=str(row["entity_token"]),
            reason=str(row["reason"]),
            blocked_at_ms=int(row["blocked_at_ms"]),
        )

    def clear_restore_disposition(
        self,
        actor: str,
        *,
        expected_entity_token: str | None = None,
        expected_desired_generation: str | None = None,
        expected_disposition_token: str | None = None,
    ) -> bool:
        name = self.local_actor(actor)
        if name is None:
            return False
        clauses = ["actor = ?"]
        values: list[object] = [name]
        if expected_entity_token is not None:
            clauses.append("entity_token = ?")
            values.append(expected_entity_token)
        if expected_desired_generation is not None:
            clauses.append("desired_generation = ?")
            values.append(expected_desired_generation)
        if expected_disposition_token is not None:
            clauses.append("disposition_token = ?")
            values.append(expected_disposition_token)
        with self._lock, self._db:
            changed = self._db.execute(
                "DELETE FROM agent_restore_dispositions WHERE "
                + " AND ".join(clauses),
                tuple(values),
            ).rowcount == 1
            if changed:
                self._restore_dispositions.pop(name, None)
            return changed

    def block_agent(
        self,
        actor: str,
        *,
        reason: str,
        expected_entity_token: str | None = None,
    ) -> tuple[AgentBlock | None, bool]:
        if reason not in {"provider-quota", "credential-invalid"}:
            raise ValueError(f"unsupported agent block reason: {reason}")
        agent = self.require(actor)
        if (
            expected_entity_token is not None
            and agent.entity_token != expected_entity_token
        ):
            return None, False
        now_ms = self.now_ms()
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT INTO agent_blocks(actor,entity_token,reason,blocked_at_ms) "
                "VALUES (?,?,?,?) ON CONFLICT(actor) DO NOTHING",
                (agent.actor, agent.entity_token, reason, now_ms),
            )
            cleared = self._db.execute(
                "DELETE FROM agent_restore_dispositions WHERE actor = ?",
                (agent.actor,),
            )
            self._restore_dispositions.pop(agent.actor, None)
            block = self._agent_blocks.get(agent.actor)
            if block is None:
                block = AgentBlock(
                    agent.actor, agent.entity_token, reason, now_ms
                )
                self._agent_blocks[agent.actor] = block
        return block, cursor.rowcount == 1 or cleared.rowcount == 1

    def agent_block(self, actor: str | Agent) -> AgentBlock | None:
        agent = actor if isinstance(actor, Agent) else self.get(actor)
        if agent is None:
            return None
        with self._lock:
            block = self._agent_blocks.get(agent.actor)
            if block is None:
                return None
            if block.entity_token != agent.entity_token:
                with self._db:
                    self._db.execute(
                        "DELETE FROM agent_blocks WHERE actor = ?", (agent.actor,)
                    )
                self._agent_blocks.pop(agent.actor, None)
                return None
            return block

    def is_blocked(self, actor: str) -> bool:
        return self.agent_block(actor) is not None

    def unblock_agent(self, actor: str) -> bool:
        agent = self.get(actor)
        if agent is None:
            return False
        with self._lock, self._db:
            changed = self._db.execute(
                "DELETE FROM agent_blocks WHERE actor = ? AND entity_token = ?",
                (agent.actor, agent.entity_token),
            ).rowcount == 1
            if changed:
                self._agent_blocks.pop(agent.actor, None)
            return changed

    def update(self, actor: str, **changes: Any) -> Agent:
        return self.save(replace(self.require(actor), **changes))

    def record_session(
        self, actor: str, *, harness: str, session_id: str | None
    ) -> HandoverNotice | None:
        """Pin the harness/session this agent is running under, for A9.

        Returns a notice exactly when the harness changed, so the caller can
        tell the agent — at the start of its very first turn — which harness it
        came from and which session id to resume by hand.  Context does not
        travel between harnesses; this makes that loss visible instead of
        silent (design §6.2).
        """

        agent = self.require(actor)
        notice: HandoverNotice | None = None
        if agent.last_harness is not None and agent.last_harness != harness:
            notice = HandoverNotice(
                actor=agent.actor,
                previous_harness=agent.last_harness,
                previous_session_id=agent.last_session_id,
                next_harness=harness,
            )
        self.save(
            replace(
                agent,
                last_harness=harness,
                last_session_id=session_id,
                preferred_harness=agent.preferred_harness or harness,
            )
        )
        return notice

    def destroy(self, actor: str) -> bool:
        """Delete the record outright.  No tombstone, no revival (A6).

        One transaction: ``ON DELETE CASCADE`` erases the agent's pins with
        the row, so a crash can never leave a pin aimed at a deleted agent.
        Accepted cost, recorded here so nobody rediscovers it as a bug: after
        a destroy, historical messages addressed to this agent can no longer
        resolve a recipient.  Recreating the same name by hand is the only
        recovery, and it does not restore the destroyed history.
        """

        name = self.local_actor(actor)
        if name is None:
            return False
        revoked: HomeReceipt | None = None
        with self._lock, self._db:
            previous = self._db.execute(
                "SELECT * FROM agents WHERE actor = ?", (name,)
            ).fetchone()
            previous_pins = self._pinned_adapters_locked(name)
            cursor = self._db.execute(
                "DELETE FROM agents WHERE actor = ?", (name,)
            )
            if previous is not None:
                prior_agent = self._row_agent(previous, previous_pins)
                revoked = self._revoke_home_locked(prior_agent)
                self._record_external_resource_locked(
                    f"agent-record:{name}", False, prior_agent.to_json()
                )
            removed = cursor.rowcount > 0
        if removed:
            with self._lock:
                self._restore_dispositions.pop(name, None)
                self._agent_blocks.pop(name, None)
        if revoked is not None:
            # The revoke/delete transaction is the crash fence.  Cleanup is
            # synchronous for the successful API contract, while a crash in
            # this gap remains resumable by cleanup_revoked_home().
            self.cleanup_home(name, expected_token=revoked.resource_token)
        return removed

    def record_external_binding(
        self,
        actor: str,
        *,
        harness: str | None,
        runtime: str | None,
        session_id: str | None,
    ) -> None:
        """Rotate the fence for a non-lifecycle binding mutation."""

        agent = self.require(actor)
        active = harness is not None and runtime is not None
        payload: dict[str, object] = (
            {
                "actor": agent.uri,
                "harness": harness,
                "runtime": runtime,
                "sessionId": session_id,
            }
            if active
            else {}
        )
        with self._lock, self._db:
            self._record_external_resource_locked(
                f"binding:{agent.uri}", active, payload
            )

    def _record_external_resource_locked(
        self, resource_key: str, active: bool, payload: dict[str, object]
    ) -> None:
        row = self._db.execute(
            "SELECT active, payload FROM lifecycle_resources WHERE resource_key = ?",
            (resource_key,),
        ).fetchone()
        encoded = json.dumps(payload, sort_keys=True)
        if row is not None and bool(row["active"]) == active and str(row["payload"]) == encoded:
            return
        self._db.execute(
            "INSERT INTO lifecycle_resources VALUES(?, ?, ?, ?) "
            "ON CONFLICT(resource_key) DO UPDATE SET "
            "resource_token=excluded.resource_token, active=excluded.active, "
            "payload=excluded.payload",
            (resource_key, uuid.uuid4().hex, int(active), encoded),
        )

    # -- secret authority -------------------------------------------------

    def _append_grant_journal_locked(
        self, agent: Agent, *, action: str, grant_id: str, by: str,
        capability: str | None = None, scope: str | None = None,
        revision: int | None = None, note: str | None = None,
    ) -> GrantJournalEntry:
        values = (int(self._clock()), agent.actor, agent.entity_token, action,
                  grant_id, capability, scope, by, revision, note)
        cursor = self._db.execute(
            'INSERT INTO agent_grant_journal '
            '(at_ms, actor, entity_token, action, grant_id, capability, scope, "by", revision, note) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', values,
        )
        return GrantJournalEntry(int(cursor.lastrowid), *values)

    def _record_host_invite_locked(self, agent: Agent) -> GrantJournalEntry:
        existing = self._db.execute(
            "SELECT * FROM agent_grant_journal WHERE actor=? AND entity_token=? "
            "AND action='host-invite' ORDER BY seq LIMIT 1",
            (agent.actor, agent.entity_token),
        ).fetchone()
        if existing is not None:
            return GrantJournalEntry(**dict(existing))
        return self._append_grant_journal_locked(
            agent, action="host-invite", grant_id="host-invite",
            by=f"user:{self.owner}", note="ownerAsserted=host",
        )

    def record_host_invite(
        self, actor: str, *, by: str, note: str | None,
    ) -> GrantJournalEntry:
        """Idempotent invitation fact, always attributed to this host owner."""
        if by != f"user:{self.owner}" or note != "ownerAsserted=host":
            raise ValueError("invitation attribution is fixed by the host")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            agent = self.require(actor)
            if agent.hosted_by != "host-invite":
                raise AgentError("not a host-invited agent")
            return self._record_host_invite_locked(agent)

    def grant_capability(
        self, actor: str, *, grant_id: str, capability: str,
        scope: str, granted_by: str, revision: int,
    ) -> CapabilityGrant:
        """Record one current-incarnation grant and its audit in one transaction."""
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            agent = self.require(actor)
            grant = CapabilityGrant(agent.actor, agent.entity_token, grant_id,
                                    capability, scope, granted_by, revision)
            previous = self._db.execute(
                "SELECT revision FROM agent_capability_grants WHERE actor=? AND grant_id=?",
                (agent.actor, grant_id),
            ).fetchone()
            if previous is not None and revision <= int(previous["revision"]):
                raise AgentError("capability grant revision must increase")
            self._db.execute(
                "INSERT INTO agent_capability_grants VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(actor, grant_id) DO UPDATE SET "
                "entity_token=excluded.entity_token, capability=excluded.capability, "
                "scope=excluded.scope, granted_by=excluded.granted_by, revision=excluded.revision",
                (grant.actor, grant.entity_token, grant.grant_id, grant.capability,
                 grant.scope, grant.granted_by, grant.revision),
            )
            self._append_grant_journal_locked(
                agent, action="grant", grant_id=grant_id, by=granted_by,
                capability=capability, scope=scope, revision=revision,
            )
            return grant

    def capability_grants(self, actor: str | None = None) -> tuple[CapabilityGrant, ...]:
        # A stale/corrupt grant is never returned as an active grant. The join is
        # the incarnation fence, not a same-name or same-owner inference.
        with self._lock:
            name = None if actor is None else self.require(actor).actor
            rows = self._db.execute(
                "SELECT g.* FROM agent_capability_grants g JOIN agents a "
                "ON a.actor=g.actor AND a.entity_token=g.entity_token "
                "WHERE (? IS NULL OR g.actor=?) ORDER BY g.actor,g.grant_id", (name, name),
            ).fetchall()
            return tuple(CapabilityGrant(**dict(row)) for row in rows)

    def revoke_capability(self, actor: str, grant_id: str, *, revoked_by: str) -> bool:
        single_line(grant_id, "grant_id")
        principal(revoked_by)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            agent = self.require(actor)
            row = self._db.execute(
                "SELECT * FROM agent_capability_grants WHERE actor=? AND grant_id=? "
                "AND entity_token=?", (agent.actor, grant_id, agent.entity_token),
            ).fetchone()
            if row is None:
                return False
            grant = CapabilityGrant(**dict(row))
            if grant.capability == "agent-home":
                raise AgentError("agent-home is intrinsic; destroy the agent to retire it")
            self._db.execute(
                "DELETE FROM agent_capability_grants WHERE actor=? AND grant_id=?",
                (agent.actor, grant_id),
            )
            self._append_grant_journal_locked(
                agent, action="revoke", grant_id=grant_id, by=revoked_by,
                capability=grant.capability, scope=grant.scope, revision=grant.revision,
            )
            return True

    def grant_journal(self, actor: str) -> tuple[GrantJournalEntry, ...]:
        # No require(): operators can inspect a destroyed actor's history.
        name = self.normalize_actor(actor)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM agent_grant_journal WHERE actor=? ORDER BY seq", (name,),
            ).fetchall()
            return tuple(GrantJournalEntry(**dict(row)) for row in rows)

    def grant_secret(
        self,
        actor: str,
        *,
        grant_id: str,
        source: "SecretSource",
        entry_id: str,
        field_name: str | None,
        environment_names: Iterable[str],
        revision: int,
        prevalidated_home_token: str | None = None,
    ) -> "SecretGrant":
        """Bind one exact entry to the current agent incarnation."""

        from .secrets import SecretGrant, SecretSource

        agent = self.require(actor)
        grant = SecretGrant(
            actor=agent.actor,
            entity_token=agent.entity_token,
            grant_id=grant_id,
            source=SecretSource(source),
            entry_id=entry_id,
            field_name=field_name,
            environment_names=tuple(environment_names),
            revision=revision,
        )
        validated_home_token = None
        if grant.source is SecretSource.AGENT_PRIVATE:
            validated_home_token = (
                self.home_receipt(agent.actor).entity_token
                if prevalidated_home_token is None
                else prevalidated_home_token
            )
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            incumbent = self._db.execute(
                "SELECT entity_token FROM agents WHERE actor=?", (agent.actor,)
            ).fetchone()
            if incumbent is None or str(incumbent["entity_token"]) != grant.entity_token:
                raise AgentError("agent incarnation changed during secret grant")
            if validated_home_token is not None:
                row = self._db.execute(
                    "SELECT active,payload FROM lifecycle_resources WHERE resource_key=?",
                    (f"agent-home:{agent.actor}",),
                ).fetchone()
                if row is None or not bool(row["active"]):
                    raise AgentHomeError("revoked", agent.actor, "grant-secret")
                receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
                if (
                    receipt.status != "ready"
                    or receipt.entity_token != validated_home_token
                    or receipt.entity_token != grant.entity_token
                ):
                    raise AgentHomeError("receipt-mismatch", agent.actor, "grant-secret")
            prior = self._db.execute(
                "SELECT revision FROM agent_secret_grants "
                "WHERE agent = ? AND grant_id = ?",
                (agent.actor, grant.grant_id),
            ).fetchone()
            if prior is not None and revision <= int(prior["revision"]):
                raise AgentError("secret grant revision must increase")
            self._db.execute(
                "INSERT INTO agent_secret_grants "
                "(agent, entity_token, grant_id, source, entry_id, field_name, "
                "environment_names, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent, grant_id) DO UPDATE SET "
                "entity_token=excluded.entity_token, source=excluded.source, "
                "entry_id=excluded.entry_id, field_name=excluded.field_name, "
                "environment_names=excluded.environment_names, "
                "revision=excluded.revision",
                (
                    grant.actor,
                    grant.entity_token,
                    grant.grant_id,
                    grant.source.value,
                    grant.entry_id,
                    grant.field_name,
                    json.dumps(list(grant.environment_names)),
                    grant.revision,
                ),
            )
        return grant

    def secret_grant(self, actor: str, grant_id: str) -> "SecretGrant | None":
        """Read one named grant; this is the resolver's non-enumerating seam."""

        from .secrets import SecretGrant, SecretSource

        name = self.local_actor(actor)
        if name is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM agent_secret_grants "
                "WHERE agent = ? AND grant_id = ?",
                (name, grant_id),
            ).fetchone()
        if row is None:
            return None
        names = json.loads(str(row["environment_names"]))
        if not isinstance(names, list) or any(not isinstance(item, str) for item in names):
            raise AgentError("secret grant environment metadata is invalid")
        return SecretGrant(
            actor=str(row["agent"]),
            entity_token=str(row["entity_token"]),
            grant_id=str(row["grant_id"]),
            source=SecretSource(str(row["source"])),
            entry_id=str(row["entry_id"]),
            field_name=(
                None if row["field_name"] is None else str(row["field_name"])
            ),
            environment_names=tuple(names),
            revision=int(row["revision"]),
        )

    def secret_inventory(self, actor: str | None = None) -> tuple["SecretGrant", ...]:
        """Operator metadata inventory; values and filesystem discovery excluded."""

        names = (
            tuple(agent.actor for agent in self.list())
            if actor is None
            else (self.require(actor).actor,)
        )
        with self._lock:
            rows = self._db.execute(
                "SELECT agent, grant_id FROM agent_secret_grants "
                "ORDER BY agent, grant_id"
            ).fetchall()
        grants: list[SecretGrant] = []
        allowed = set(names)
        for row in rows:
            name = str(row["agent"])
            if name not in allowed:
                continue
            grant = self.secret_grant(name, str(row["grant_id"]))
            if grant is not None:
                grants.append(grant)
        return tuple(grants)

    def revoke_secret_grant(self, actor: str, grant_id: str) -> bool:
        agent = self.require(actor)
        with self._lock, self._db:
            cursor = self._db.execute(
                "DELETE FROM agent_secret_grants WHERE agent = ? AND grant_id = ?",
                (agent.actor, grant_id),
            )
            return cursor.rowcount > 0

    def rotate_secret_authority(self, actor: str) -> Agent:
        """Fence a real account/hosting transfer without treating it as an alias."""

        agent = self.require(actor)
        replacement = replace(agent, entity_token=uuid.uuid4().hex)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._revoke_home_locked(agent)
            self._db.execute(
                "DELETE FROM agent_secret_grants WHERE agent = ?", (agent.actor,)
            )
            self._db.execute(
                "DELETE FROM agent_capability_grants WHERE actor = ?", (agent.actor,)
            )
            self._db.execute(
                "UPDATE agents SET entity_token = ? WHERE actor = ?",
                (replacement.entity_token, agent.actor),
            )
            self._record_external_resource_locked(
                f"agent-record:{agent.actor}", True, replacement.to_json()
            )
        return replacement

    # -- pins -------------------------------------------------------------

    def pin(self, adapter: str, actor: str) -> str | None:
        """Bind ``adapter`` to ``actor`` one-to-one; return the previous URI.

        Idempotent for the same pair; re-pinning the adapter to a different
        agent is a move (the previous binding is released in the same
        transaction).  The one-to-one rule is the schema's, not this
        method's: UNIQUE(agent) rejects a second adapter on the same agent
        (:class:`PinConflictError` names the holder and the exact unpin
        command), and the FOREIGN KEY rejects a pin to a nonexistent agent.
        """

        name = self.normalize_actor(actor)
        with self._lock:
            previous = self._pin_of_locked(adapter)
            if previous is not None and previous[0] == name:
                return previous[1]
            try:
                with self._db:
                    self._db.execute(
                        "DELETE FROM pins WHERE adapter = ?", (adapter,)
                    )
                    self._db.execute(
                        "INSERT INTO pins (adapter, agent) VALUES (?, ?)",
                        (adapter, name),
                    )
            except sqlite3.IntegrityError as error:
                message = str(error)
                if "FOREIGN KEY" in message.upper():
                    raise AgentNotFoundError(
                        f"no agent named {name!r} on this machine"
                    ) from error
                holder = self._db.execute(
                    "SELECT adapter FROM pins WHERE agent = ?", (name,)
                ).fetchone()
                raise PinConflictError(
                    self.uri_for(name),
                    adapter,
                    holder["adapter"] if holder is not None else "(unknown)",
                ) from error
            return None if previous is None else previous[1]

    def unpin(self, adapter: str) -> str | None:
        """Remove ``adapter``'s pin; return the URI it pointed at, if any."""

        with self._lock:
            previous = self._pin_of_locked(adapter)
            if previous is None:
                return None
            with self._db:
                self._db.execute(
                    "DELETE FROM pins WHERE adapter = ?", (adapter,)
                )
            return previous[1]

    def pins(self) -> dict[str, str]:
        """Every pin, as ``{adapter: canonical agent URI}``, adapter-sorted."""

        with self._lock:
            return {
                row["adapter"]: row["uri"]
                for row in self._db.execute(
                    "SELECT pins.adapter AS adapter, agents.uri AS uri "
                    "FROM pins JOIN agents ON agents.actor = pins.agent "
                    "ORDER BY pins.adapter"
                )
            }

    def pinned_adapters(self, actor: str) -> tuple[str, ...]:
        name = self.local_actor(actor)
        if name is None:
            return ()
        with self._lock:
            return self._pinned_adapters_locked(name)

    def _pinned_adapters_locked(self, name: str) -> tuple[str, ...]:
        return tuple(
            row["adapter"]
            for row in self._db.execute(
                "SELECT adapter FROM pins WHERE agent = ? ORDER BY adapter",
                (name,),
            )
        )

    def _pin_of_locked(self, adapter: str) -> tuple[str, str] | None:
        row = self._db.execute(
            "SELECT pins.agent AS agent, agents.uri AS uri "
            "FROM pins JOIN agents ON agents.actor = pins.agent "
            "WHERE pins.adapter = ?",
            (adapter,),
        ).fetchone()
        return None if row is None else (row["agent"], row["uri"])

    # -- storage ----------------------------------------------------------

    def _row_agent(self, row: sqlite3.Row, pinned: tuple[str, ...]) -> Agent:
        # Fail closed on a blank token instead of stringifying it: ``str(None)``
        # is ``"None"``, and a ``"None"`` agent facing a ``"None"`` grant row
        # would make the incarnation fence constant-true.  The schema (new or
        # rebuilt) makes this unreachable; this guard is for the tampered or
        # foreign-tool-written database.
        token = row["entity_token"]
        if not isinstance(token, str) or not token:
            raise AgentError(
                f"agent {row['actor']!r} has a blank entity_token; the incarnation "
                "fence cannot be evaluated and this database is refused. It was "
                "written by a mixed-version or foreign tool; rebuild it with a "
                "current hyprial before use."
            )
        return Agent(
            uri=row["uri"],
            actor=row["actor"],
            owner=row["owner"],
            machine=row["machine"],
            entity_token=token,
            cwd=row["cwd"],
            config=normalize_agent_config(
                None if row["config"] is None else json.loads(row["config"])
            ),
            provider=row["provider"],
            model=row["model"],
            capabilities=normalize_capabilities(json.loads(row["capabilities"])),
            harness_args=normalize_harness_args(json.loads(row["harness_args"])),
            preferred_harness=row["preferred_harness"],
            last_harness=row["last_harness"],
            last_session_id=row["last_session_id"],
            last_active_at_ms=(
                None
                if row["last_active_at_ms"] is None
                else int(row["last_active_at_ms"])
            ),
            pinned_adapters=pinned,
            created_at_ms=int(row["created_at_ms"]),
            hosted_by=row["hosted_by"],
        )

    def _import_legacy_files(self, directory: Path) -> None:
        """One-shot import of pre-sqlite ``<actor>.json`` records.

        Production never deployed the file layout, so this exists for
        development machines.  Imported files are renamed to
        ``*.json.imported`` — kept as an archive so a rollback to a
        file-reading build finds its data; an unreadable file is left in
        place untouched rather than renamed over.  Pins ride along under the
        same constraints as live writes; on a conflict the alphabetically
        first import wins (``INSERT OR IGNORE``), the same first-wins order
        the desired-state migration uses.
        """

        if not directory.is_dir():
            return
        imported: list[str] = []
        for path in sorted(directory.glob("*.json")):
            if path.name.startswith("."):
                continue
            try:
                record = Agent.from_json(
                    json.loads(path.read_text(encoding="utf-8")), path.name
                )
            except (OSError, ValueError, AgentError):
                continue  # unreadable: keep the file, import the rest
            if record.hosted_by is not None:
                # Legacy configuration is not a hosting authorization source.
                # Leave it untouched, exactly as an invalid legacy record.
                continue
            with self._lock:
                exists = (
                    self._db.execute(
                        "SELECT 1 FROM agents WHERE actor = ?", (record.actor,)
                    ).fetchone()
                    is not None
                )
                if not exists:
                    with self._db:
                        self._db.execute(*self._insert_statement(record))
                        for adapter in record.pinned_adapters:
                            self._db.execute(
                                "INSERT OR IGNORE INTO pins (adapter, agent) "
                                "VALUES (?, ?)",
                                (adapter, record.actor),
                            )
            path.rename(path.with_name(path.name + ".imported"))
            imported.append(path.name)
        self.imported_legacy = tuple(imported)


# -- module-level seam for the delivery line (design §5.2) -----------------
#
# The pull-model side consumes exactly these two names and must not compose an
# agent identity itself.  The daemon installs its registry at startup; before
# that they answer honestly rather than guessing.

_DEFAULT_REGISTRY: AgentRegistry | None = None


def set_default_registry(registry: AgentRegistry | None) -> None:
    global _DEFAULT_REGISTRY
    _DEFAULT_REGISTRY = registry


def default_registry() -> AgentRegistry | None:
    return _DEFAULT_REGISTRY


def local_actors() -> tuple[str, ...]:
    """Every actor URI this machine speaks for (design §5.2)."""

    registry = _DEFAULT_REGISTRY
    return () if registry is None else registry.local_actors()


def verify_fetch_claim(actor: str, signature: bytes, nonce: bytes) -> bool:
    """Design §5.2 / §8.2.  See :meth:`AgentRegistry.verify_fetch_claim`.

    Performs no signature verification — no key material exists yet.
    """

    registry = _DEFAULT_REGISTRY
    return False if registry is None else registry.verify_fetch_claim(
        actor, signature, nonce
    )
