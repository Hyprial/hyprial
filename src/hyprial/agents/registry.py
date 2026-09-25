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

HOSTED_BY_VALUES = ("transfer-receive", "squire-container", "host-invite")


class AgentError(RuntimeError):
    """Base class for every agent-registry failure, carrying an IPC code."""

    code = "AGENT_ERROR"


class AgentExistsError(AgentError):
    """Decision A1: a second agent may never claim a name already in use."""

    code = ipc_errors.AGENT_EXISTS


class AgentNotFoundError(AgentError):
    code = ipc_errors.AGENT_NOT_FOUND


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
                if "hosted_by" not in columns:
                    connection.execute(_HOSTED_BY_ALTER)
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS agents_actor_entity_token "
            "ON agents(actor, entity_token)"
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
                config=payload.config,
                provider=payload.provider,
                model=payload.model,
                capabilities=dict(payload.capabilities),
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
    ) -> None:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT resource_token, retired FROM lifecycle_receipts "
                "WHERE attempt_token = ?",
                (attempt_token,),
            ).fetchone()
            if row is None:
                return
            if str(row["resource_token"]) != resource_token or not bool(row["retired"]):
                raise ValueError("lifecycle receipt retirement mismatch")
            self._db.execute(
                "DELETE FROM lifecycle_receipts WHERE attempt_token = ?",
                (attempt_token,),
            )

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

    def _create_record(self, agent: Agent) -> Agent:
        self._validate_config_location(agent)
        # A daemon may have crashed after committing destroy's durable revoke
        # but before finishing filesystem retirement.  The revoked receipt,
        # not the requested name, authorizes this retry cleanup.
        self.cleanup_revoked_home(agent.actor)
        home_attempt: HomeProvisioningAttempt | None = None
        with self._lock:
            try:
                with self._db:
                    # Reserve before touching the filesystem.  This serializes
                    # separate registry connections as well as this process.
                    self._db.execute("BEGIN IMMEDIATE")
                    home_attempt = self._provision_home_locked(agent)
                    self._db.execute(*self._insert_statement(agent))
                    self._record_external_resource_locked(
                        f"agent-record:{agent.actor}", True, agent.to_json()
                    )
                    if home_attempt is not None:
                        self._record_home_resource_locked(home_attempt.receipt, True)
                    if agent.hosted_by == "host-invite":
                        self._record_host_invite_locked(agent)
            except sqlite3.IntegrityError as error:
                if home_attempt is not None:
                    self._compensate_home_fs(home_attempt, "create-duplicate")
                # The PRIMARY KEY spoke: A1 lives in the schema now.
                raise AgentExistsError(
                    f"the name {agent.actor!r} is already taken on this node "
                    f"({self.owner}@{self.machine}) — {agent.uri} exists "
                    f"({self.database}). One name is one agent, whether or "
                    f"not anything is currently running under it. To reuse "
                    f"the name, destroy that agent first ('hyprial agent destroy "
                    f"{agent.actor}', which is irreversible); to run this "
                    f"agent on a different harness, just start it there — "
                    f"that is a rebinding of the same agent, not a new one."
                ) from error
            except BaseException:
                if home_attempt is not None:
                    self._compensate_home_fs(home_attempt, "create-failed")
                raise
        return agent

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
        """Provision or validate the current entity's home under registry lock."""

        agent = self.require(actor)
        if self._home is None:
            raise AgentHomeError("not-configured", agent.actor, "provision")
        attempt: HomeProvisioningAttempt | None = None
        try:
            with self._lock, self._db:
                self._db.execute("BEGIN IMMEDIATE")
                attempt = self._provision_home_locked(agent)
                assert attempt is not None
                self._record_home_resource_locked(attempt.receipt, True)
            return attempt.receipt
        except BaseException:
            if attempt is not None:
                self._compensate_home_fs(attempt, "ensure")
            raise

    def home_receipt(self, actor: str, *, require_ready: bool = True) -> HomeReceipt:
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
        if require_ready:
            if self._home is None:
                # Not ``assert``: this is control flow (a registry opened
                # without a provisioner can still hold home rows), and an
                # assert vanishes under ``python -O`` — the typed error keeps
                # the fence loud in every build.
                raise AgentHomeError("not-configured", agent.actor, "validate")
            self._home.validate(receipt)
        return receipt

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
        with self._lock:
            receipt = self.home_receipt(actor)
            return self._home.ensure_workspace(receipt)

    def workspace_summary(self, actor: str) -> WorkspaceSummary:
        """Inventory the current incarnation's workspace without following links."""

        if self._home is None:
            raise AgentHomeError("not-configured", actor, "workspace-summary")
        with self._lock:
            receipt = self.home_receipt(actor)
            return self._home.workspace_summary(receipt)

    def cleanup_home(self, actor: str, *, expected_token: str) -> HomeReceipt:
        """Clean a destroyed/revoked home only under its durable token fence."""

        if self._home is None:
            raise AgentHomeError("not-configured", actor, "cleanup")
        name = self.local_actor(actor)
        if name is None:
            raise AgentHomeError("cleanup-fenced", actor, "cleanup-registry")
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
                raise AgentHomeError("cleanup-fenced", name, "cleanup-registry")
            try:
                receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
            except (ValueError, json.JSONDecodeError) as error:
                raise AgentHomeError("invalid-registry-receipt", name, "cleanup-registry") from error
            if receipt.actor != name:
                raise AgentHomeError("receipt-mismatch", name, "cleanup-registry")
            cleaned = self._home.cleanup(receipt, expected_token=expected_token)
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
                "SELECT owner, machine, uri, hosted_by, entity_token "
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
            "last_harness, last_session_id, created_at_ms, hosted_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                agent.created_at_ms,
                agent.hosted_by,
            ),
        )

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
        if grant.source is SecretSource.AGENT_PRIVATE:
            self.home_receipt(agent.actor)
        with self._lock, self._db:
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
