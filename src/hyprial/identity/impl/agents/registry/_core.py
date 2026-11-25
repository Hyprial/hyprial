from __future__ import annotations

from hyprial.identity.impl.agents.home.provisioner import AgentHomeError
from hyprial.identity.impl.agents.home.provisioner import AgentHomeProvisioner
from collections.abc import Callable
from pathlib import Path
import json
from hyprial.identity.impl.agents.home.config import normalize_agent_config
from hyprial.kernel import RESERVED_AGENT_ACTOR_NAMES
import sqlite3
import threading

from ._base import (
    ACTOR_NAME_PATTERN,
    Agent,
    AgentError,
    AgentNotFoundError,
    InvalidAgentNameError,
    _MAX_ACTOR_NAME_LENGTH,
    _connect,
    _now_ms,
    _uri,
    agent_id_for_entity_token,
    normalize_capabilities,
    normalize_harness_args,
)
from ._lifecycle import _RegistryLifecycleMixin
from ._home_create import _RegistryHomeCreateMixin
from ._home_destroy import _RegistryHomeDestroyMixin
from ._records import _RegistryRecordsMixin
from .config_binding import _RegistryConfigBindingMixin
from .harness_args import _RegistryHarnessArgsMixin
from .naming import _RegistryNamingMixin
from hyprial.identity.impl.agents.secret_store.defaults import DefaultCredentialMixin


class AgentRegistry(DefaultCredentialMixin, _RegistryHarnessArgsMixin, _RegistryConfigBindingMixin, _RegistryNamingMixin, _RegistryLifecycleMixin, _RegistryHomeCreateMixin, _RegistryHomeDestroyMixin, _RegistryRecordsMixin):
    """Every agent on this machine, in one real SQLite database.

    The database path is a constructor argument so tests (and a second daemon
    on the same machine) can point at their own file.  Writes are
    transactions; the schema owns the invariants (A1 PRIMARY KEY
    one-to-one UNIQUEs, destroy cascade), so the code translates
    ``IntegrityError`` into the typed errors instead of re-checking by hand.

    On construction, any pre-sqlite ``<legacy>/​*.json`` records are imported
    once and the files renamed to ``*.json.imported`` — kept, not deleted,
    so a rollback to a file-reading build still finds its data.
    """
    @staticmethod
    def local_names(database: Path) -> frozenset[str]:
        """Read local names without creating, migrating, or writing a registry."""
        try:
            database.stat()
        except FileNotFoundError:
            return frozenset()
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return frozenset(row[0] for row in connection.execute("SELECT actor FROM agents"))
        finally:
            connection.close()

    @staticmethod
    def local_names_with_capability(database: Path, name: str, value: str) -> frozenset[str]:
        """Read-only: the local names whose capability fact *name* equals *value*."""
        try:
            database.stat()
        except FileNotFoundError:
            return frozenset()
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = connection.execute("SELECT actor, capabilities FROM agents").fetchall()
        finally:
            connection.close()
        found = set()
        for actor, capabilities in rows:
            try:
                facts = json.loads(capabilities or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(facts, dict) and facts.get(name) == value:
                found.add(actor)
        return frozenset(found)

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
        from hyprial.identity.impl.agents.home.native_imports import initialize

        initialize(self._db)
        self._retired_session_refs: dict[str, set[tuple[str, str]]] = {}
        from hyprial.identity.impl.agents.addressing import id_form_minting

        if id_form_minting():
            from .naming.id_form import migrate_registry_to_id_form

            migrate_registry_to_id_form(self)
        self._refresh_retired_session_refs_locked()
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

    def native_credential_import_record(self, actor: str):
        """Bind a native import receipt to this Agent and its provisioned home."""
        from hyprial.identity.impl.agents.home.native_imports import NativeImportRecord

        with self._lock:
            return NativeImportRecord(self, actor)

    def _refresh_retired_session_refs_locked(self) -> None:
        rows = self._db.execute(
            "SELECT actor, session_ref, entity_token FROM retired_session_refs"
        ).fetchall()
        retired: dict[str, set[tuple[str, str]]] = {}
        for row in rows:
            retired.setdefault(str(row["session_ref"]), set()).add(
                (str(row["actor"]), str(row["entity_token"]))
            )
        self._retired_session_refs = retired
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
    @staticmethod
    def normalize_actor(value: str) -> str:
        """Reduce a bare name or a canonical URI to the machine-local actor name."""

        candidate = _uri().agent_uri_actor(value)
        if candidate is None:
            candidate = value
        else:
            candidate = _uri().member_actor_registry_key(candidate)
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
        """Validate a public creation name without discarding URI ownership.

        Reserved daemon actor names are refused here, at the single creation
        gate, because they belong to daemon-owned delivery identities.  The
        check is exact and reads the name before the pattern guard, so
        ``_turn-hooks`` reports the reserved reason rather than a generic shape
        error.  Read paths keep using :meth:`normalize_actor`, so a legacy
        persisted row stays resolvable and destroyable.
        """

        parsed = _uri().parse_agent_uri(value)
        if parsed is not None and (parsed[0] != self.owner or parsed[1] != self.machine):
            raise InvalidAgentNameError(
                f"cannot create foreign agent {value!r} on {self.owner}@{self.machine}; "
                "hosting requires transfer receive"
            )
        raw_name = parsed[2] if parsed is not None else value
        if raw_name in RESERVED_AGENT_ACTOR_NAMES:
            raise InvalidAgentNameError(
                f"agent name {raw_name!r} is reserved for a daemon internal "
                "actor and cannot be registered"
            )
        candidate = self.normalize_actor(value)
        if _uri().is_identity_id_segment(candidate.lower()):
            raise InvalidAgentNameError(
                "agent name must not have an exact identity id shape"
            )
        return candidate
    def local_actor(self, value: str) -> str | None:
        """The machine-local agent name behind ``value``, or None if not ours.

        Stricter than :meth:`normalize_actor` on purpose: a four-segment URI
        naming a different owner or machine is somebody else's agent, and must
        not resolve to a local record that merely shares the short name. That
        distinction is the whole reason this class owns identity.
        The sole exception is an exact stored URI with explicit hosting authority.
        """

        from hyprial.identity.impl.agents.addressing import resolve_local_actor

        return resolve_local_actor(
            value,
            owner=self.owner,
            machine=self.machine,
            read=self._read_addressed,
            normalize=self._local_name,
        )
    def actor_key(self, value: str) -> str:
        """The immutable actor key behind a name, display name, ``a.`` id or URI.

        Read and mutation paths key every table on ``actor``; an id-form
        address must reach the same row its name does.  Values this registry
        cannot place fall back to :meth:`normalize_actor`, as before.
        """

        return self.local_actor(value) or self.normalize_actor(value)
    def _read_addressed(self, key: str) -> Agent | None:
        if _uri().is_agent_id_segment(key):
            return self.get_by_agent_id(key)
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM agents WHERE actor = ? OR uri = ? OR display_name = ?",
                (key, key, key),
            ).fetchone()
            if row is None:
                return None
            return self._row_agent(row)
    def uri_for(self, actor: str) -> str:
        from hyprial.identity.impl.agents.addressing import official_agent_uri

        name = self.actor_key(actor)
        return official_agent_uri(
            name, owner=self.owner, machine=self.machine, read=self._read_addressed
        )
    def get_by_agent_id(self, agent_id: str) -> Agent | None:
        """The live record whose immutable id is exactly ``agent_id``."""

        if not _uri().is_agent_id_segment(agent_id):
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
            ).fetchone()
            if row is None:
                row = next(
                    (
                        candidate
                        for candidate in self._db.execute(
                            "SELECT * FROM agents WHERE agent_id IS NULL"
                        )
                        if _agent_id_or_none(candidate["entity_token"]) == agent_id
                    ),
                    None,
                )
            if row is None:
                return None
            return self._row_agent(row)
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
            return self._row_agent(row)
    def require(self, actor: str) -> Agent:
        agent = self.get(actor)
        if agent is None:
            raise AgentNotFoundError(f"no agent named {actor!r} on this machine")
        return agent
    def exists(self, actor: str) -> bool:
        return self.get(actor) is not None
    def list(self) -> tuple[Agent, ...]:
        with self._lock:
            return tuple(
                self._row_agent(row)
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
    def _row_agent(self, row: sqlite3.Row) -> Agent:
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
            created_at_ms=int(row["created_at_ms"]),
            hosted_by=row["hosted_by"],
            display_name=row["display_name"],
        )
    def _import_legacy_files(self, directory: Path) -> None:
        """One-shot import of pre-sqlite ``<actor>.json`` records.

        Production never deployed the file layout, so this exists for
        development machines.  Imported files are renamed to
        ``*.json.imported`` — kept as an archive so a rollback to a
        file-reading build finds its data; an unreadable file is left in
        place untouched rather than renamed over.
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
            path.rename(path.with_name(path.name + ".imported"))
            imported.append(path.name)
        self.imported_legacy = tuple(imported)


def _agent_id_or_none(entity_token: object) -> str | None:
    try:
        return agent_id_for_entity_token(str(entity_token))
    except AgentError:
        return None


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
