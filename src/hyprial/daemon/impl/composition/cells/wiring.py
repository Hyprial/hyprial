"""Compose the one cell runtime of a daemon generation (plan §1.1, S3).

Decision notes (phase A, S3):

- ``cell_runtime(app)`` composes lazily on first use and caches the runtime
  on ``app._cell_runtime``; concurrent first calls compose exactly one.  It is
  not called at startup in phase A, so a daemon boot performs no cell write
  and creates no owner-home space until a consumer asks for a cell.
- It never raises: it returns None when the
  app is closing or closed, when composition fails (logged as
  ``cell.runtime_unavailable``, then not retried for ``COMPOSE_RETRY_S`` so a
  per-message caller cannot hammer OrgFS), or when another composition holds
  the app's lock past ``COMPOSE_WAIT_S``.  A failure after the cache opened
  closes the cache.
- ``close_cell_runtime`` is the ``cell-runtime`` shutdown step and runs on
  every daemon close, composed or not: it marks the app closed first, so a
  late caller (an in-flight IPC handler) cannot compose a second runtime or
  revive the OrgFS runtime after its own close step.  A composition still in
  flight sees the mark and closes what it built instead of publishing it.
- Each app has its own composition lock; a module lock only guards creating
  it.
- Second review round (2026-11-20, cell model I5-2): a failed boot-time
  composition now heals by itself.  Nothing periodic reads the cell world,
  and the registrar's mutation hooks exist only after a successful
  composition, so the failure path arms one daemon ``threading.Timer``
  (``COMPOSE_RETRY_S``, the registrar's own retry pattern) that calls
  ``cell_runtime`` again; any successful composition or the close step
  cancels it.  No new scheduler and no engine actor are introduced.
- Third review round: the retry timer now follows the registrar timer's
  locking discipline exactly — one dedicated per-app lock (created under
  the module guard, never the composition lock, so the callback's
  ``cell_runtime`` call cannot deadlock) guards the closed check, the
  timer slot's read/clear/arm and the cancel; the callback releases it
  before composing and logs an unexpected failure instead of letting a
  timer thread die silently.
- The runtime's identity (contract OC0 §1.4, §1.5): the authority is
  ``local_cell_authority(app)`` = ``user:u.<app.user_id>`` (resolved once per
  serving generation when the Application is built; never re-read here);
  the writer is ``local_author(app, fs)``, the local OrgFS principal (the
  facade's ``author``, else ``user:<owner>``), which the conversation
  placement keeps using; the issuer is ``app.user_issuer``.  No uid means
  no runtime: ``CellRuntimeUnavailable("authority-uid-missing")``, never a
  fallback to the username and never a made-up uid.
- Before ``CellCache`` opens anything, ``cell_cache_identity_conflict`` checks
  ``<state>/cells.sqlite3`` read-only; any conflict refuses composition
  (``cache-identity-mismatch`` with the key names, or ``cache-unreadable``).
  Nothing here deletes, rewrites or migrates the file: an agent moves it
  by hand (``docs/guides/migration-cell-authority-uid.md``).  When no file
  exists, ``create_cell_cache`` writes a new one recording this identity.
  The refusal is logged as ``cell.runtime_unavailable`` with ``reason`` and
  retried with the usual ``COMPOSE_RETRY_S`` timer, each time read-only.
- ``writer_bindings`` is empty in OC1a (only the local binding exists); the
  org label store supplies it later.  The cache lives at
  ``<state>/cells.sqlite3``; orgs this node has left are excluded.
- Once a runtime is published, ``attach_agent_cells`` (``agents.py``)
  registers every local agent's cell on it and keeps doing so for agents
  created later; ``close_cell_runtime`` detaches it before closing.
- Phase B (S8): the registrar is kept on ``app._agent_cell_registrar`` so
  the attach/detach handlers reconcile through it (one lock, fresh record
  reads) instead of writing the cell from their own snapshot.
- I5-1 (cell actors): after the registrar attaches, ``attach_cell_actors``
  composes the ``CellActorRegistry`` over the app's process-wide actor host
  and does the first reconcile over the local agent registry's cell ids;
  from then on the runtime's ``on_index`` tells the current agent ``Cell``
  values and ``on_health`` tells ``sourceStale``, both memory-value tells
  that never ask an actor.  A failure here never fails composition: the
  registry is skipped (logged ``cell.actor_registry_unavailable``) and the
  runtime keeps its pre-I5 behaviour.  ``close_cell_runtime`` detaches the
  registry before stopping the runtime; the actors themselves live on the
  shared engine, whose own shutdown step stops them.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.cells.actor import CellActorRegistry
from hyprial.daemon.impl.cells.model import Cell
from hyprial.daemon.impl.cells.model import is_authority
from hyprial.daemon.impl.cells.ports import CellIdentity, CellRuntimePort
from hyprial.daemon.impl.cells.reconcile import CellReconcileCoordinator
from hyprial.daemon.impl.cells.runtime import (
    CACHE_FILENAME,
    CellCache,
    CellRuntime,
    cell_cache_identity_conflict,
    create_cell_cache,
    read_cell_cache_identity,
)
from hyprial.daemon.impl.composition.cells.agents import attach_agent_cells
from hyprial.daemon.impl.composition.cells.reconcile import (
    AppLocalMemberFacts,
    AppMemberEffectPort,
    CellReconcileStartBarrier,
)
from hyprial.daemon.impl.composition.cells.spaces import CellSpaces
from hyprial.daemon.impl.forwarding.membership.hardening.left_orgs import read_left_orgs
from hyprial.kernel import canonical_user_uri

CELL_RUNTIME_CLOSE_TIMEOUT_S = 5.0
COMPOSE_WAIT_S = 5.0
COMPOSE_RETRY_S = 30.0
CELL_ACTOR_HOST_OWNER = "cell-actors"
CELL_RECONCILE_HOST_OWNER = "cell-reconcile"
_LOCK_GUARD = threading.Lock()
UID_MISSING = "authority-uid-missing"


class CellRuntimeUnavailable(Exception):
    """Composition refused: no uid, or a cell cache of another identity."""

    def __init__(self, reason: str, keys: str = "") -> None:
        super().__init__(reason if not keys else f"{reason}:{keys}")
        self.reason = reason
        self.keys = keys


def _retry_lock(app: Any) -> threading.Lock:
    lock = getattr(app, "_cell_runtime_retry_lock", None)
    if lock is None:
        with _LOCK_GUARD:
            lock = getattr(app, "_cell_runtime_retry_lock", None)
            if lock is None:
                lock = threading.Lock()
                app._cell_runtime_retry_lock = lock
    return lock


def _arm_compose_retry(app: Any, delay: float) -> None:
    with _retry_lock(app):
        if getattr(app, "_cell_runtime_closed", False):
            return
        if getattr(app, "_cell_runtime_retry_timer", None) is not None:
            return
        retry = threading.Timer(delay, _compose_retry_now, args=(app,))
        retry.daemon = True
        app._cell_runtime_retry_timer = retry
        retry.start()


def _compose_retry_now(app: Any) -> None:
    with _retry_lock(app):
        app._cell_runtime_retry_timer = None
        if getattr(app, "_cell_runtime_closed", False):
            return
    try:
        cell_runtime(app)
    except Exception as error:  # noqa: BLE001 - a timer thread never dies silently
        logger = getattr(app, "_log", None)
        if logger is not None:
            try:
                logger(
                    "warn",
                    "cells",
                    "cell.compose_retry_failed",
                    errorType=type(error).__name__,
                    detail=str(error)[:300],
                )
            except Exception:  # noqa: BLE001 - logging never breaks the retry
                pass


def _cancel_compose_retry(app: Any) -> None:
    with _retry_lock(app):
        retry = getattr(app, "_cell_runtime_retry_timer", None)
        app._cell_runtime_retry_timer = None
    if retry is not None:
        retry.cancel()


def local_author(app: Any, fs: Any) -> str:
    """The principal the local OrgFS facade writes as (the app's owner)."""

    author = getattr(fs, "author", None)
    if isinstance(author, str) and author.startswith("user:"):
        return author
    owner = str(app.owner)
    return owner if owner.startswith("user:") else canonical_user_uri(owner)


def local_cell_authority(app: Any) -> str:
    """The local cell authority ``user:u.<sub>`` of this serving generation."""

    user_id = getattr(app, "user_id", None)
    if not isinstance(user_id, str) or not user_id:
        raise CellRuntimeUnavailable(UID_MISSING)
    try:
        authority = canonical_user_uri(f"u.{user_id}")
    except ValueError as error:
        raise CellRuntimeUnavailable(UID_MISSING) from error
    if not is_authority(authority):
        raise CellRuntimeUnavailable(UID_MISSING)
    return authority


def _no_writer_bindings() -> dict[str, str]:
    return {}


def _log(app: Any, level: str, event: str, **fields: Any) -> None:
    logger = getattr(app, "_log", None)
    if logger is None:
        return
    try:
        logger(level, "cells", event, **fields)
    except Exception:  # noqa: BLE001 - logging never breaks composition
        pass


def _compose_lock(app: Any) -> threading.Lock:
    lock = getattr(app, "_cell_runtime_lock", None)
    if lock is None:
        with _LOCK_GUARD:
            lock = getattr(app, "_cell_runtime_lock", None)
            if lock is None:
                lock = threading.Lock()
                app._cell_runtime_lock = lock
    return lock


def _open_cache(state_dir: Path, identity: CellIdentity) -> CellCache:
    conflict = cell_cache_identity_conflict(
        state_dir, authority=identity.authority, issuer=identity.issuer, writer=identity.writer
    )
    if conflict is not None:
        raise CellRuntimeUnavailable(conflict.reason, conflict.keys_csv)
    path = state_dir / CACHE_FILENAME
    if read_cell_cache_identity(state_dir) == "absent":
        create_cell_cache(
            path, authority=identity.authority, issuer=identity.issuer, writer=identity.writer
        )
    return CellCache(path)


def _compose(app: Any) -> CellRuntime:
    authority = local_cell_authority(app)
    fs = app._require_orgfs_runtime().facade
    home = Path(app.hyprial_home)
    identity = CellIdentity(
        authority=authority,
        issuer=str(getattr(app, "user_issuer", "") or ""),
        writer=local_author(app, fs),
    )
    cache = _open_cache(Path(app.state_dir), identity)
    try:
        runtime = CellRuntime(
            fs=fs,
            spaces=CellSpaces(fs, left_orgs=lambda: read_left_orgs(home)),
            cache=cache,
            identity=identity,
            writer_bindings=_no_writer_bindings,
            logger=getattr(app, "_log", None),
        )
        runtime.start()
    except BaseException:
        cache.close()
        raise
    return runtime


def cell_runtime(app: Any) -> CellRuntime | None:
    """The app's cell runtime, composed and started on first use; never raises."""

    if getattr(app, "_cell_runtime_closed", False):
        return None
    lock = _compose_lock(app)
    if not lock.acquire(timeout=COMPOSE_WAIT_S):
        return None
    runtime: CellRuntime | None = None
    try:
        existing = getattr(app, "_cell_runtime", None)
        if existing is not None:
            return existing
        if getattr(app, "_cell_runtime_closed", False):
            return None
        failed_at = getattr(app, "_cell_runtime_failed_at", None)
        if failed_at is not None and time.monotonic() - failed_at < COMPOSE_RETRY_S:
            return None
        try:
            runtime = _compose(app)
        except Exception as error:  # noqa: BLE001 - composition never raises to a caller
            app._cell_runtime_failed_at = time.monotonic()
            refused = error if isinstance(error, CellRuntimeUnavailable) else None
            reason = {} if refused is None else {"reason": refused.reason, "keys": refused.keys}
            _log(app, "warn", "cell.runtime_unavailable", errorType=type(error).__name__,
                 detail=str(error)[:300], **reason)
            _arm_compose_retry(app, COMPOSE_RETRY_S)
            return None
        if getattr(app, "_cell_runtime_closed", False):
            runtime.close(CELL_RUNTIME_CLOSE_TIMEOUT_S)
            return None
        app._cell_runtime_failed_at = None
        _cancel_compose_retry(app)
        app._cell_runtime = runtime
        _log(app, "info", "cell.runtime_composed", cells=len(runtime.index().cells),
             dirty=len(runtime.dirty()))
        registrar = attach_agent_cells(app, runtime)
        app._agent_cell_registrar = registrar
        app._agent_cells_detach = None if registrar is None else registrar.detach
        actor_registry = attach_cell_actors(app, runtime)
        app._cell_actor_registry = actor_registry
        reconcile_coordinator = attach_cell_reconcile(app, runtime)
        app._cell_reconcile_coordinator = reconcile_coordinator
        if getattr(app, "_cell_runtime_closed", False):
            _detach_cell_reconcile(app)
            if actor_registry is not None:
                actor_registry.detach()
            if registrar is not None:
                registrar.detach()
            app._cell_actor_registry = None
            app._agent_cell_registrar = None
            app._agent_cells_detach = None
            app._cell_runtime = None
            runtime.close(CELL_RUNTIME_CLOSE_TIMEOUT_S)
            return None
        runtime.kick()
        return runtime
    finally:
        lock.release()


def _agent_registry_cells(app: Any, runtime: CellRuntimePort) -> tuple[Cell, ...]:
    """The current ``Cell`` values of the local agent registry's cell ids."""

    agents = getattr(app, "agents", None)
    if agents is None or not callable(getattr(agents, "list", None)):
        return ()
    try:
        cell_ids = [str(agent.cell_id) for agent in agents.list()]
    except Exception as error:  # noqa: BLE001 - a broken directory skips nothing else
        _log(app, "warn", "cell.actor_first_reconcile_failed",
             errorType=type(error).__name__)
        return ()
    index = runtime.index()
    return tuple(cell for cell_id in cell_ids if (cell := index.get(cell_id)) is not None)


def _index_agent_cells(runtime: CellRuntimePort) -> tuple[Cell, ...]:
    """The local agent cells of the current index (the runtime authority's)."""

    identity = getattr(runtime, "identity", None)
    authority = getattr(identity, "authority", None)
    return tuple(
        cell
        for cell in runtime.index().cells.values()
        if cell.kind == "agent" and (authority is None or cell.authority == authority)
    )


def _index_actor_cells(runtime: CellRuntimePort) -> tuple[Cell, ...]:
    """Local-authority agent and org cells present in the current index."""

    identity = getattr(runtime, "identity", None)
    authority = getattr(identity, "authority", None)
    return tuple(
        cell
        for cell in runtime.index().cells.values()
        if (
            cell.kind == "agent"
            and (authority is None or cell.authority == authority)
        )
        or (
            cell.kind == "org"
            and authority is not None
            and cell.authority == authority
        )
    )


def _initial_actor_cells(app: Any, runtime: CellRuntimePort) -> tuple[Cell, ...]:
    agent_cells = _agent_registry_cells(app, runtime)
    seen = {cell.cell_id for cell in agent_cells}
    extra_org = tuple(
        cell
        for cell in _index_actor_cells(runtime)
        if cell.kind == "org" and cell.cell_id not in seen
    )
    return agent_cells + extra_org


def attach_cell_actors(app: Any, runtime: CellRuntimePort) -> CellActorRegistry | None:
    """Compose the cell actor registry on *runtime*; never raises.

    The first reconcile covers the local agent registry's cell ids (plan §E)
    plus local-authority ``org`` cells; afterwards ``on_index`` tells the
    current local-authority cell values and ``on_health`` tells
    ``sourceStale``, both as memory-value tells.
    """

    hosts = getattr(app, "_actor_hosts", None)
    if hosts is None or not callable(getattr(hosts, "host_for", None)):
        _log(app, "info", "cell.actor_registry_skipped", reason="no-actor-hosts")
        return None
    registry: CellActorRegistry | None = None
    undo: list[Any] = []

    def cleanup_hooks() -> None:
        while undo:
            unregister = undo.pop()
            try:
                unregister()
            except Exception:  # noqa: BLE001 - detaching is best effort
                pass

    try:
        registry = CellActorRegistry(
            hosts.host_for(CELL_ACTOR_HOST_OWNER),
            runtime=runtime,
            logger=getattr(app, "_log", None),
        )

        def on_index(_generation: int) -> None:
            registry.on_index(cell.cell_id for cell in _index_actor_cells(runtime))

        def on_health(_health: Any) -> None:
            registry.on_health()

        undo.append(runtime.on_index(on_index))
        subscribe_health = getattr(runtime, "on_health", None)
        if callable(subscribe_health):
            undo.append(subscribe_health(on_health))
        health = getattr(runtime, "health", None)
        current = health() if callable(subscribe_health) and callable(health) else None
        registry.on_index(cell.cell_id for cell in _initial_actor_cells(app, runtime))
        if current is not None:
            registry.on_health()
    except Exception as error:  # noqa: BLE001 - actors never fail composition
        cleanup_hooks()
        if registry is not None:
            registry.detach()
        _log(app, "warn", "cell.actor_registry_unavailable", errorType=type(error).__name__,
             detail=str(error)[:300])
        return None
    app._cell_actor_hooks_detach = cleanup_hooks
    _log(app, "info", "cell.actor_registry_attached", host=CELL_ACTOR_HOST_OWNER)
    return registry


def _detach_cell_reconcile(app: Any) -> None:
    hooks_detach = getattr(app, "_cell_reconcile_hooks_detach", None)
    app._cell_reconcile_hooks_detach = None
    coordinator = getattr(app, "_cell_reconcile_coordinator", None)
    app._cell_reconcile_coordinator = None
    effects = getattr(app, "_cell_reconcile_effects", None)
    app._cell_reconcile_effects = None
    if callable(hooks_detach):
        hooks_detach()
    if coordinator is not None:
        coordinator.close()
    if effects is not None:
        effects.close()


def attach_cell_reconcile(
    app: Any, runtime: CellRuntimePort
) -> CellReconcileCoordinator | None:
    """Compose the document-revision member reconcile coordinator on *runtime*; never raises."""

    hosts = getattr(app, "_actor_hosts", None)
    if hosts is None or not callable(getattr(hosts, "host_for", None)):
        _log(app, "info", "cell.reconcile_skipped", reason="no-actor-hosts")
        return None
    facts_port = AppLocalMemberFacts(app)
    effects_port = AppMemberEffectPort(app, facts_port=facts_port)
    coordinator: CellReconcileCoordinator | None = None
    unregister_index: Any = None
    cancel_initial: Any = None
    barrier = getattr(app, "_cell_reconcile_barrier", None)
    if not isinstance(barrier, CellReconcileStartBarrier):
        barrier = CellReconcileStartBarrier()
    try:
        coordinator = CellReconcileCoordinator(
            facts_port=facts_port,
            effects_port=effects_port,
            actor_host=hosts.host_for(CELL_RECONCILE_HOST_OWNER),
        )
        effects_port.bind_coordinator(coordinator)

        def tell_index() -> None:
            assert coordinator is not None
            coordinator.on_index(_index_agent_cells(runtime))

        def on_index(_generation: int) -> None:
            if barrier.is_open:
                tell_index()

        unregister_index = runtime.on_index(on_index)
        cancel_initial = barrier.when_open(tell_index)
    except Exception as error:  # noqa: BLE001 - reconcile attachment never fails composition
        if callable(cancel_initial):
            cancel_initial()
        if callable(unregister_index):
            try:
                unregister_index()
            except Exception:  # noqa: BLE001 - detaching is best effort
                pass
        if coordinator is not None:
            coordinator.close()
        effects_port.close()
        _log(
            app,
            "warn",
            "cell.reconcile_unavailable",
            errorType=type(error).__name__,
            detail=str(error)[:300],
        )
        return None
    app._cell_reconcile_effects = effects_port

    def detach_hooks() -> None:
        cancel_initial()
        if callable(unregister_index):
            unregister_index()

    app._cell_reconcile_hooks_detach = detach_hooks
    _log(app, "info", "cell.reconcile_attached", host=CELL_RECONCILE_HOST_OWNER)
    return coordinator


def close_cell_runtime(app: Any) -> None:
    """Mark the app closed and stop its cell runtime; idempotent."""

    lock = _compose_lock(app)
    acquired = lock.acquire(timeout=CELL_RUNTIME_CLOSE_TIMEOUT_S)
    if not acquired:
        app._cell_runtime_closed = True
        raise RuntimeError("cell runtime composition did not finish before deadline")
    try:
        app._cell_runtime_closed = True
        app._org_cell_actors_closed = True
        _cancel_compose_retry(app)
        app._agent_cell_registrar = None
        org_actors = getattr(app, "_org_cell_actors", None)
        app._org_cell_actors = None
        detach = getattr(app, "_agent_cells_detach", None)
        if callable(detach):
            detach()
        _detach_cell_reconcile(app)
        actor_registry = getattr(app, "_cell_actor_registry", None)
        app._cell_actor_registry = None
        app._agent_cells_detach = None
        hooks_detach = getattr(app, "_cell_actor_hooks_detach", None)
        app._cell_actor_hooks_detach = None
        if callable(hooks_detach):
            hooks_detach()
        if actor_registry is not None:
            actor_registry.detach()
        running = getattr(app, "_cell_runtime", None)
        app._cell_runtime = None
    finally:
        lock.release()
    org_closer = getattr(org_actors, "close", None)
    if callable(org_closer):
        org_closer()
    closer = getattr(running, "close", None)
    if callable(closer) and not closer(CELL_RUNTIME_CLOSE_TIMEOUT_S):
        raise RuntimeError("cell runtime did not stop before deadline")


def agent_adapter_bindings(app: Any, agent: Any) -> tuple[str, ...]:
    """Adapter names bound to *agent* by its live cell document (I6-4).

    Cell documents are the only adapter-binding authority; an unavailable
    runtime or missing cell reports no binding instead of falling back to any
    retired pin store.
    """

    runtime = getattr(app, "_cell_runtime", None)
    if runtime is None:
        return ()
    from hyprial.daemon.impl.cells.adapter_routing import (
        agent_adapter_bindings as _cell_bindings,
    )

    return _cell_bindings(runtime.lookup(agent.cell_id))


def agent_adapter_bindings_or_unknown(app: Any, agent: Any) -> tuple[str, ...] | None:
    """Adapter names bound to *agent*, or ``None`` when that is unknown.

    Guards in front of irreversible or protective decisions (PAC GC, the
    destroy recheck, the worker-in-use observation, teleport preflight) use
    this form: an unavailable runtime or a cell that is not loaded is not
    proof that the agent is unbound, so callers treat ``None`` as bound.
    """

    runtime = getattr(app, "_cell_runtime", None)
    if runtime is None:
        return None
    cell = runtime.lookup(agent.cell_id)
    if cell is None:
        return None
    from hyprial.daemon.impl.cells.adapter_routing import (
        agent_adapter_bindings as _cell_bindings,
    )

    return _cell_bindings(cell)


__all__ = [
    "agent_adapter_bindings",
    "agent_adapter_bindings_or_unknown",
    "CELL_ACTOR_HOST_OWNER",
    "CELL_RECONCILE_HOST_OWNER",
    "CellRuntimeUnavailable",
    "CELL_RUNTIME_CLOSE_TIMEOUT_S",
    "COMPOSE_RETRY_S",
    "COMPOSE_WAIT_S",
    "attach_cell_actors",
    "attach_cell_reconcile",
    "cell_runtime",
    "close_cell_runtime",
    "local_author",
    "local_cell_authority",
]
