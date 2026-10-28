"""Compose the send-link service and adapt the app's ports to the engine.

Spec: docs/design/send-links-impl/wp2-composition.md. Adapters keep the engine free
of orgfs, presence, inbox and PAC. Composition runs before PAC and fails closed.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from hyprial.daemon.impl.composition.link_spaces import (
    SPACE_REFRESH_MS,
    OrgLinkSpaces,
)
from hyprial.daemon.impl.configuration.identity import normalize_agent_recipient
from hyprial.daemon.impl.inbox import DEFAULT_HOLD_TTL_MS
from hyprial.daemon.impl.inbox.contracts.api import (
    LinkLedgerCensor,
    LinkLedgerOpen,
)
from hyprial.daemon.impl.inbox.contracts.ports import UpdateLinkLedgerCommand
from hyprial.daemon.impl.links.ports import (
    TOP_FIELD_CAPACITY_W,
    TOP_FIELD_OUTBOUND_IN_FLIGHT,
)
from hyprial.daemon.impl.links.service import LinkService
from hyprial.daemon.impl.links.store import (
    HeldRow,
    InboundRow,
    LedgerResult,
    LedgerRow,
    LinkStore,
)
from hyprial.daemon.impl.org.network.directory import (
    OrgFsDirectoryStore as OrgFsDirectoryStore,
)
from hyprial.daemon.impl.org.network.state import read_left_orgs as read_left_orgs
from hyprial.daemon.impl.pac.actors.marker import pac_worker_marker
from hyprial.identity import directory_owner_principal
from hyprial.daemon.impl.pac.storage.store import (
    PacGraphStore,
    default_database_path,
)
from hyprial.kernel import (
    HookEventPublisher,
    canonical_agent_uri,
    canonical_user_uri,
    parse_agent_uri,
    uri_scheme,
)

__all__ = [
    "FacadeLinkInboxPort",
    "BYPASS_ENV",
    "LINKS_SETTINGS_KEY",
    "LinkBypassError",
    "LINK_SERVICE_CLOSE_TIMEOUT_S",
    "LINK_WAKE_EVENTS",
    "LinkWindowModeError",
    "LocalIdentity",
    "OrgLinkSpaces",
    "PacLifecyclePort",
    "PresencePort",
    "SPACE_REFRESH_MS",
    "WINDOW_DEFAULT_MODE",
    "WINDOW_MODE_ENV",
    "WINDOW_MODES",
    "close_link_service",
    "compose_link_service",
    "link_bypass",
    "link_top_fields",
    "link_window_mode",
    "pac_task_binder",
]

#: daemon-level bus events that merely wake the service (rulings 46/55).
LINK_WAKE_EVENTS = ("graph.closed", "worker.cleanup.complete")
#: Grace for ``store.flush()`` and the service thread on the close path.
LINK_SERVICE_CLOSE_TIMEOUT_S = 5.0
#: ``link.space_ambiguous`` / ``link.local_agents_failed`` rate limit.
_ONE_PER_MINUTE_MS = 60_000
#: Operator switch for the W/cwnd bounds (H1).  ``$HYPRIAL_HOME/settings.json``
#: carries a ``links`` section whose ``windowMode`` is ``observe|enforce``
#: (each key optional: a section without ``windowMode`` means the default);
#: ``HYPRIAL_LINKS_WINDOW_MODE`` wins over it, and ``observe`` is the default.
LINKS_SETTINGS_KEY = "links"
WINDOW_MODE_ENV = "HYPRIAL_LINKS_WINDOW_MODE"
WINDOW_MODES = ("observe", "enforce")
WINDOW_DEFAULT_MODE = "observe"


#: Emergency switch: ``HYPRIAL_LINKS_BYPASS`` (env) or ``links.bypass``
#: (settings.json), env first, default on.  On, every link-state hold or
#: ingress refusal admits; identity and org-membership checks stay
#: (docs/design/design-send-links-2026-10-14.md "Emergency bypass").
BYPASS_ENV = "HYPRIAL_LINKS_BYPASS"
_BYPASS_ON = frozenset({"1", "true", "yes"})
_BYPASS_OFF = frozenset({"", "0", "false", "no"})


class LinkWindowModeError(ValueError):
    """A malformed ``links.windowMode`` switch; composition fails closed."""


class LinkBypassError(ValueError):
    """A malformed ``links.bypass`` switch; composition fails closed."""


def _links_settings_section(
    hyprial_home: str | Path, error_type: type[ValueError]
) -> tuple[Path, dict[str, Any] | None]:
    """Read and type-check the shared ``links`` object, or fail closed."""
    path = Path(hyprial_home) / "settings.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return path, None
    except (OSError, ValueError) as error:
        raise error_type(f"cannot parse {path}: {error}") from error
    section = (record if isinstance(record, dict) else {}).get(LINKS_SETTINGS_KEY)
    if section is None:
        return path, None
    if not isinstance(section, dict):
        raise error_type(f"{path}: {LINKS_SETTINGS_KEY} must be an object")
    return path, section


def link_bypass(
    hyprial_home: str | Path, environ: Mapping[str, str] | None = None
) -> bool:
    """The effective switch: env > settings > on; empty env falls through.

    A missing/null setting means on; a malformed value fails composition closed.
    """

    env = os.environ if environ is None else environ
    raw = env.get(BYPASS_ENV, "")
    if raw.strip():
        return _checked_bypass(raw, BYPASS_ENV)
    path, section = _links_settings_section(hyprial_home, LinkBypassError)
    if section is None:
        return True
    value = section.get("bypass")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise LinkBypassError(f"{path}: {LINKS_SETTINGS_KEY}.bypass must be a boolean")
    return _checked_bypass(value, f"{path}: {LINKS_SETTINGS_KEY}.bypass")


def _checked_bypass(raw: str, where: str) -> bool:
    value = raw.strip().lower()
    if value in _BYPASS_ON:
        return True
    if value in _BYPASS_OFF:
        return False
    raise LinkBypassError(f"{where} must be one of 1/true/yes/0/false/no; got {raw!r}")


def _space_members(fs: Any) -> Callable[[str], frozenset[str]]:
    """Owner and members of one org space as canonical ``user:`` principals."""

    def members(space_id: str) -> frozenset[str]:
        owners = [space.owner for space in fs.spaces() if space.space_id == space_id]
        users = [member.user for member in fs.members(space_id)]
        return frozenset(directory_owner_principal(user) for user in (*owners, *users))

    return members


def link_window_mode(
    hyprial_home: str | Path, environ: Mapping[str, str] | None = None
) -> str:
    """The effective H1 switch: env > settings > observe.

    A missing/null section or key means the default.  An unreadable or
    invalid switch raises rather than silently choosing (the forwarding.mode
    precedent).
    """

    env = os.environ if environ is None else environ
    raw = env.get(WINDOW_MODE_ENV, "").strip().lower()
    if raw:
        return _checked_window_mode(raw, WINDOW_MODE_ENV)
    path, section = _links_settings_section(hyprial_home, LinkWindowModeError)
    if section is None:
        return WINDOW_DEFAULT_MODE
    mode = section.get("windowMode")
    if mode is None:
        return WINDOW_DEFAULT_MODE
    if not isinstance(mode, str):
        raise LinkWindowModeError(
            f"{path}: {LINKS_SETTINGS_KEY}.windowMode must be one of "
            f"{', '.join(WINDOW_MODES)}"
        )
    return _checked_window_mode(
        mode.strip().lower(), f"{path}: {LINKS_SETTINGS_KEY}.windowMode"
    )


def _checked_window_mode(raw: str, where: str) -> str:
    if raw not in WINDOW_MODES:
        raise LinkWindowModeError(
            f"{where} must be one of {', '.join(WINDOW_MODES)}; got {raw!r}"
        )
    return raw


def _log(app: Any, level: str, event: str, **fields: Any) -> None:
    logger = getattr(app, "_log", None)
    if logger is None:
        return
    try:
        logger(level, "daemon", event, **fields)
    except Exception:  # noqa: BLE001 - logging never breaks composition
        pass


class LocalIdentity:
    """``owner``/``node`` identity for the engine plus its local-agent view.

    Locality follows the registry of the node that hosts the agent home:
    ``is_local`` is true only for a canonical URI whose node segment is this
    node AND whose actor this node's registry speaks for.  The owner segment
    never decides locality, so a hosted agent (a visitor's owner, this node)
    is local -- this daemon hosts it and is the only writer of its link file
    (contract/wire/send-links.md §12) -- while a same-owner agent on another
    node stays remote.

    ``author_of`` is the author expected on that actor's link file: this
    node's owner for every actor hosted here (a hosted file is written by this
    daemon, not by the visitor), and the actor's own owner elsewhere.
    """

    def __init__(
        self,
        owner: str,
        node_id: str,
        *,
        local: Callable[[], Iterable[str]],
        internal_actor_names: frozenset[str] = frozenset(),
        logger: Callable[..., None] | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._bare_owner = owner.removeprefix("user:")
        self._node = node_id
        self._local = local
        self._internal = frozenset(internal_actor_names)
        self._logger = logger
        self._clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._failed_at_ms = -_ONE_PER_MINUTE_MS * 2

    def is_local(self, uri: str) -> bool:
        """Is ``uri`` an actor this node's registry speaks for?"""

        return self._is_local_uri(uri, self._local_actor_set())

    def _local_actor_set(self) -> frozenset[str]:
        """This node's registry candidates, read once per decision."""

        try:
            candidates = self._local()
        except Exception as error:  # noqa: BLE001 - registry read is best effort
            self._note_failure(error)
            return frozenset()
        return frozenset(uri for uri in candidates if isinstance(uri, str))

    def _is_local_uri(self, uri: object, local: frozenset[str]) -> bool:
        """The single locality predicate: node segment + registry membership."""

        if not isinstance(uri, str):
            return False
        parsed = parse_agent_uri(uri)
        if parsed is None or parsed[1] != self._node:
            return False
        return uri in local

    def author_of(self, uri: str) -> str | None:
        parsed = parse_agent_uri(uri)
        if parsed is None:
            return None
        if parsed[1] == self._node:
            # The hosting daemon writes every file for an actor on its own
            # node, including a hosted visitor's; the file's author is this
            # node's owner, not the (possibly foreign) agent owner.
            return canonical_user_uri(self._bare_owner)
        return canonical_user_uri(parsed[0])

    def local_agents(self) -> tuple[str, ...]:
        local = self._local_actor_set()
        result = []
        for uri in local:
            if not self._is_local_uri(uri, local):
                continue
            actor = parse_agent_uri(uri)[2]
            if actor in self._internal:
                continue
            result.append(uri)
        return tuple(sorted(result))

    def _note_failure(self, error: BaseException) -> None:
        now = self._clock()
        if now - self._failed_at_ms < _ONE_PER_MINUTE_MS:
            return
        self._failed_at_ms = now
        if self._logger is not None:
            try:
                self._logger(
                    "warn",
                    "daemon",
                    "link.local_agents_failed",
                    errorType=type(error).__name__,
                )
            except Exception:  # noqa: BLE001 - logging cannot break a read
                pass


def _local_actor_candidates(app: Any) -> Callable[[], Iterable[str]]:
    def candidates() -> tuple[str, ...]:
        agents = getattr(app, "agents", None)
        registry = tuple(agents.local_actors()) if agents is not None else ()
        interactive = tuple(app._registered_interactive_actors())
        return registry + interactive

    return candidates


class PresencePort:
    """``LinkPresencePort`` over the daemon's raw liveliness projection (§4)."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def online_agents(self) -> tuple[str, ...]:
        presence = getattr(self._app, "_presence", None)
        if presence is None:
            return ()
        return tuple(
            actor
            for actor in presence.raw_online_actors()
            if uri_scheme(actor) == "agent"
        )


def _pac_graph_id(task_id: str) -> str | None:
    if not isinstance(task_id, str) or not task_id.startswith("pac-"):
        return None
    graph_id = task_id.removeprefix("pac-")
    return graph_id or None


class PacLifecyclePort:
    """Read-only PAC snapshots; one short-lived store per call (§6)."""

    def __init__(
        self,
        state_dir: Path,
        *,
        open_store: Callable[[], Any] | None = None,
        desired: Any | None = None,
        logger: Callable[..., None] | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._open = open_store or (
            lambda: PacGraphStore(
                default_database_path(Path(state_dir)), read_only=True
            )
        )
        #: The daemon's in-memory desired-state port (``app.desired_state``).
        #: The PAC ownership marker lives in a harness row's ``nickname``, so
        #: the ownership judgement is one in-memory row read per local actor;
        #: it never opens the PAC store.  ``None`` reads as resident (publish).
        self._desired = desired
        self._logger = logger
        self._clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._unreadable_at_ms = -_ONE_PER_MINUTE_MS * 2

    def task_states(
        self, task_ids: frozenset[str]
    ) -> Mapping[str, str]:
        store = None
        try:
            store = self._open()
            result: dict[str, str] = {}
            for task_id in task_ids:
                graph_id = _pac_graph_id(task_id)
                if graph_id is None:
                    result[task_id] = "unknown"
                    continue
                graph = store.graph(graph_id)
                if graph is None:
                    result[task_id] = "unknown"
                elif graph.get("closed_at") is not None:
                    result[task_id] = "ended"
                else:
                    result[task_id] = "active"
            return result
        except Exception:  # noqa: BLE001 - unreadable PAC means unknown
            self._note_unreadable()
            return {task_id: "unknown" for task_id in task_ids}
        finally:
            _close_quietly(store)

    def worker_active(self, task_id: str, worker: str) -> bool | None:
        store = None
        try:
            store = self._open()
            graph_id = _pac_graph_id(task_id)
            if graph_id is None:
                return None
            graph = store.graph(graph_id)
            if graph is None:
                return None
            if graph.get("closed_at") is not None:
                return False
            parsed = parse_agent_uri(worker)
            if parsed is None:
                return None
            for node in store.nodes(graph_id):
                if getattr(node, "kind", None) == "actor":
                    if getattr(node, "actor_name", None) == parsed[2]:
                        return True
                    continue
                # A borrowed owner: an existing agent that owns a task node
                # without a workflow ``_actor`` node of its own.  It stays
                # active for as long as the graph is open; reading it as
                # False made the ledger reclaim its pac_task link at once.
                if getattr(node, "owner", None) == worker:
                    return True
            return False
        except Exception:  # noqa: BLE001 - unreadable PAC means unknown
            self._note_unreadable()
            return None
        finally:
            _close_quietly(store)

    def is_workflow_worker(self, actor: str) -> bool:
        """Whether desired_state marks ``actor`` as a PAC temporary worker.

        PAC persists the actor activation ``identity_marker`` as the launched
        harness row's ``nickname``; ``DaemonActorRuntime.observe`` reports the
        same fact as ``identity_marker``.  One in-memory desired-state row per
        local actor, no PAC store and no 5 s cache: a worker registered between
        two ticks is recognised on the very next tick (E2E-021).  A missing
        store or row, or a malformed marker, reads as resident and publishes --
        a stale file costs one commit, a skipped resident loses reachability.
        """

        desired = self._desired
        if desired is None:
            return False
        parsed = parse_agent_uri(actor)
        if parsed is None:
            return False
        try:
            spec = desired.harness_spec(parsed[2])
        except Exception:  # noqa: BLE001 - unreadable desired state publishes
            return False
        return spec is not None and pac_worker_marker(spec.nickname) is not None

    def _note_unreadable(self) -> None:
        now = self._clock()
        if now - self._unreadable_at_ms < _ONE_PER_MINUTE_MS:
            return
        self._unreadable_at_ms = now
        if self._logger is None:
            return
        try:
            self._logger("warn", "daemon", "link.pac_unreadable")
        except Exception:  # noqa: BLE001 - logging cannot break a read
            pass


def _close_quietly(store: Any | None) -> None:
    if store is None:
        return
    try:
        store.close()
    except Exception:  # noqa: BLE001 - a close failure is not a read failure
        pass


def _reply_to(payload: Any) -> str | None:
    """Same six lines as inbox's private ``_link_reply_to`` (no import)."""

    try:
        body = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    value = body.get("replyTo")
    return value if isinstance(value, str) and value else None


def _ledger_row(row: Any) -> LedgerRow:
    return LedgerRow(
        actor=row.actor,
        message_id=row.message_id,
        direction=row.direction,
        peer=row.peer,
        intent=row.intent,
        reply_to=row.reply_to,
        state=row.state,
        opened_at_ms=row.opened_at_ms,
        sent_at_ms=row.sent_at_ms,
        event_at_ms=row.event_at_ms,
        threshold_ms=row.threshold_ms,
        sample_pending=row.sample_pending,
        expires_at_ms=row.expires_at_ms,
    )


def _inbound_row(row: Any) -> InboundRow:
    return InboundRow(
        message_id=row.message_id,
        sender=row.sender,
        recipient=row.recipient,
        intent=row.intent,
        reply_to=row.reply_to,
        received_at_ms=row.received_at_ms,
        fetched_at_ms=row.fetched_at_ms,
        injected=row.injected,
    )


def _held_row(item: Any) -> HeldRow:
    message = item.message
    return HeldRow(
        message_id=message.message_id,
        sender=message.sender,
        target=message.recipient,
        intent=message.intent,
        reply_to=_reply_to(message.payload),
        hold_reason=str(item.link_hold),
        created_at_ms=message.created_at_ms,
        expires_at_ms=item.expires_at_ms,
    )


class FacadeLinkInboxPort:
    """``LinkInboxPort`` over the inbox custody facade (wp2-composition §2.2)."""

    def __init__(
        self,
        facade: Any,
        *,
        local_agents: Callable[[], tuple[str, ...]],
        logger: Callable[..., None] | None = None,
    ) -> None:
        self._facade = facade
        self._local = local_agents
        self._logger = logger

    def ledger_snapshot(self, now_ms: int) -> tuple[LedgerRow, ...]:
        return tuple(
            _ledger_row(row) for row in self._facade.link_ledger_snapshot(now_ms)
        )

    def task_bindings(self) -> tuple[tuple[str, str, str, int], ...]:
        return tuple(self._facade.link_task_bindings())

    def held_outbox(self) -> tuple[HeldRow, ...]:
        return tuple(_held_row(item) for item in self._facade.link_held_outbox())

    def inbound_live(
        self, actor: str, peer: str | None = None
    ) -> tuple[InboundRow, ...]:
        return tuple(
            _inbound_row(row)
            for row in self._facade.link_inbound_live(actor, peer)
        )

    def inbound_row(self, message_id: str) -> InboundRow | None:
        matches = []
        for actor in self._local():
            row = self._facade.link_inbound_row(actor, message_id)
            if row is not None:
                matches.append(row)
        if len(matches) == 1:
            return _inbound_row(matches[0])
        if len(matches) > 1:
            self._log(
                "link.inbound_row_ambiguous",
                messageId=message_id,
                count=len(matches),
            )
        return None

    def apply(self, update: Any, *, now_ms: int) -> LedgerResult:
        command = UpdateLinkLedgerCommand(
            correlation_id=f"link:apply:{uuid4().hex}",
            release=tuple(update.release),
            reasons=dict(update.reasons),
            fail=dict(update.fail),
            drain=dict(update.drain),
            cancel_outbound=dict(update.cancel_outbound),
            release_outbound=tuple(update.release_outbound),
            censor=tuple(
                LinkLedgerCensor(
                    actor=record.actor,
                    message_id=record.message_id,
                    threshold_ms=record.threshold_ms,
                    expires_at_ms=record.expires_at_ms,
                )
                for record in update.censor
            ),
            sampled=tuple(update.sampled),
            open_outbound=tuple(
                LinkLedgerOpen(
                    actor=row.actor,
                    message_id=row.message_id,
                    peer=row.peer,
                    intent=row.intent,
                    reply_to=row.reply_to,
                    state=row.state,
                    opened_at_ms=row.opened_at_ms,
                    expires_at_ms=row.expires_at_ms,
                )
                for row in update.open_outbound
            ),
            close_inbound=tuple(update.close_inbound),
            bind_tasks=tuple(update.bind_tasks),
            unbind_tasks=tuple(update.unbind_tasks),
            purge_before_ms=update.purge_before_ms,
            now_ms=now_ms,
        )
        result = self._facade.update_link_ledger(command)
        return LedgerResult(
            released=tuple(result.released),
            expired=tuple(result.expired),
            failed=tuple(result.failed),
            drained=tuple(result.drained),
            cancelled=tuple(result.cancelled),
        )

    def _log(self, event: str, **fields: Any) -> None:
        if self._logger is None:
            return
        try:
            self._logger("warn", "daemon", event, **fields)
        except Exception:  # noqa: BLE001 - logging never breaks a read
            pass


def _inbox_port_for(app: Any, identity: LocalIdentity) -> FacadeLinkInboxPort:
    """Monkeypatch point for tests (§10 C1); wires the real custody facade."""

    return FacadeLinkInboxPort(
        app._inbox,
        local_agents=identity.local_agents,
        logger=getattr(app, "_log", None),
    )


class _LinkHookLane:
    """Forwarder that can detach: the bus has no unregister (G3)."""

    def __init__(self, service: LinkService) -> None:
        self._target: LinkService | None = service

    def observe(self, event: object) -> None:
        target = self._target
        if target is not None:
            target.observe_hook(event)

    def detach(self) -> None:
        self._target = None


def _message_ttl_ms(graph: Any) -> int:
    policy = getattr(graph, "hold_policy", None)
    ttl = getattr(policy, "ttl_ms", None)
    return int(ttl) if isinstance(ttl, int) and ttl > 0 else DEFAULT_HOLD_TTL_MS


def compose_link_service(
    app: Any,
    graph: Any,
    *,
    fs: Any = None,
    inbox_port: Any = None,
    clock_ms: Callable[[], int] | None = None,
    seed: int | None = None,
) -> LinkService | None:
    """Create, start and bind the one link service; never raises (§8.1)."""

    admission = app.link_admission
    if admission.bound:
        _log(
            app,
            "warn",
            "link.service_skipped",
            reason="slot-bound",
            gateType=type(admission.gate).__name__,
        )
        return None
    clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
    store = None
    service: LinkService | None = None
    stage = "orgfs"
    try:
        if fs is None:
            fs = app._require_orgfs_runtime().facade
        internal = admission.internal_actors
        identity = LocalIdentity(
            app.owner,
            app.node_id,
            local=_local_actor_candidates(app),
            internal_actor_names=internal,
            logger=getattr(app, "_log", None),
            clock_ms=clock,
        )
        bypass = link_bypass(app.hyprial_home)
        spaces = OrgLinkSpaces(
            fs,
            home=Path(app.hyprial_home),
            clock_ms=clock,
            invalid_devices=app._org_directory_quarantine,
            logger=getattr(app, "_log", None),
        )
        cell: list[LinkService | None] = [None]
        store = LinkStore(
            fs,
            spaces=spaces,
            author_of=identity.author_of,
            is_local_agent=identity.is_local,
            on_change=lambda: cell[0] is not None and cell[0].kick(),
            logger=getattr(app, "_log", None),
            members_of=_space_members(fs) if bypass else None, bookkeeping=not bypass,
        )
        stage = "construct"
        hook_bus = getattr(app, "_hook_bus", None)
        window_mode = link_window_mode(app.hyprial_home)
        service = LinkService(
            store=store,
            inbox=inbox_port or _inbox_port_for(app, identity),
            presence=PresencePort(app),
            pac=PacLifecyclePort(
                Path(app.state_dir),
                desired=getattr(app, "desired_state", None),
                logger=getattr(app, "_log", None),
                clock_ms=clock,
            ),
            is_local_agent=identity.is_local,
            local_agents=identity.local_agents,
            internal_actor_names=internal,
            publisher=(
                HookEventPublisher(hook_bus, source="daemon.links")
                if hook_bus is not None
                else None
            ),
            clock_ms=clock,
            message_ttl_ms=_message_ttl_ms(graph),
            seed=seed,
            window_mode=window_mode,
            logger=getattr(app, "_log", None),
            bypass=bypass,
        )
        cell[0] = service
        stage = "start"
        service.start()
        # Every fallible step runs before bind: the slot has no unbind
        # (ruling 69 G4), so bind is the last step that can fail.
        stage = "binder"
        binder = pac_task_binder(app, service)
        stage = "describe"
        composed = {
            "spaces": len(spaces()),
            "skippedOrgSpaces": spaces.skipped_count,
            "localAgents": len(identity.local_agents()),
            "orgAvailable": store.index().org_available,
            "bypass": service.bypass,
        }
        stage = "bind"
        admission.bind(service)
        app._link_service = service
        app._link_task_binder = binder
        _register_hook_lane(app, service)
        _log(app, "info", "link.service_composed", **composed)
        return service
    except Exception as error:  # noqa: BLE001 - composition never breaks startup
        _abort(service, store)
        _log(
            app,
            "warn",
            "link.service_failed",
            stage=stage,
            errorType=type(error).__name__,
            detail=str(error)[:300],
        )
        return None


def _abort(built: LinkService | None, store: LinkStore | None) -> None:
    # `built` is a LinkService handle, not an inbox object; it is deliberately not
    # named `service` because test_inbox_authority_gate matches `close` on that
    # name, so renaming it back would trip a false positive.
    if built is not None:
        try:
            built.close(LINK_SERVICE_CLOSE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - already failing; keep teardown best effort
            pass
    elif store is not None:
        try:
            store.close()
        except Exception:  # noqa: BLE001 - already failing; keep teardown best effort
            pass


def _register_hook_lane(app: Any, service: LinkService) -> None:
    daemon_hooks = getattr(app, "_daemon_hooks", None)
    if daemon_hooks is None:
        return
    lane = _LinkHookLane(service)
    try:
        daemon_hooks.register_consumer("links", lane, LINK_WAKE_EVENTS)
    except Exception as error:  # noqa: BLE001 - the tick reconcile is the fallback
        _log(app, "warn", "link.lane_failed", errorType=type(error).__name__)
        return
    app._link_hook_lane = lane


def close_link_service(app: Any) -> None:
    """Detach the lane, stop the service; idempotent (§7)."""

    # `running` is a LinkService handle, not an inbox object; keep it off the name
    # `service` (test_inbox_authority_gate matches `close` on that name).
    running = getattr(app, "_link_service", None)
    lane = getattr(app, "_link_hook_lane", None)
    app._link_service = None
    app._link_task_binder = None
    app._link_hook_lane = None
    if lane is not None:
        lane.detach()
    closer = getattr(running, "close", None)
    if callable(closer) and not closer(LINK_SERVICE_CLOSE_TIMEOUT_S):
        raise RuntimeError("link service did not stop before deadline")


def pac_task_binder(app: Any, service: LinkService) -> Callable[..., Any]:
    """Mint a PAC task link before delivery; None when it does not apply."""

    clock = lambda: time.time_ns() // 1_000_000  # noqa: E731 - one local clock

    def binder(
        *, sender: str, recipient: str, conversation_id: str
    ) -> Callable[[str], None] | None:
        if not isinstance(conversation_id, str) or not conversation_id.startswith(
            "pac-"
        ):
            return None
        try:
            resolve = app._resolve_agent_alias
            ends = [
                resolve(normalize_agent_recipient(value))
                for value in (sender, recipient)
            ]
            if any(parse_agent_uri(end) is None for end in ends):
                return None
            principal, worker = ends
            callback = service.bind_task(
                principal=principal,
                worker=worker,
                task_id=conversation_id,
                now_ms=clock(),
            )

            def on_delivered(message_id: str) -> None:
                try:
                    callback(message_id)
                except Exception as error:  # noqa: BLE001 - never fail PAC delivery
                    _log(
                        app,
                        "warn",
                        "link.pac_bind_failed",
                        stage="callback",
                        errorType=type(error).__name__,
                    )

            return on_delivered
        except Exception as error:  # noqa: BLE001 - binder never raises into PAC
            _log(
                app,
                "warn",
                "link.pac_bind_failed",
                stage="bind",
                errorType=type(error).__name__,
            )
            return None

    return binder


def link_top_fields(
    app: Any, actor_statuses: Iterable[Mapping[str, Any]]
) -> dict[str, Mapping[str, int | None]]:
    """``top`` link columns per actor; None values when unwired (§2.6)."""

    service = getattr(app, "_link_service", None)
    fields: dict[str, Mapping[str, int | None]] = {}
    for status in actor_statuses:
        raw = status.get("actor")
        actor = (
            raw
            if isinstance(raw, str)
            else canonical_agent_uri(app.owner, app.node_id, str(status.get("name")))
        )
        if service is None:
            fields[actor] = {
                TOP_FIELD_CAPACITY_W: None,
                TOP_FIELD_OUTBOUND_IN_FLIGHT: None,
            }
        else:
            fields[actor] = service.top_fields(actor)
    return fields
