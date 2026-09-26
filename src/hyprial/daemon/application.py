"""Production daemon composition and versioned Unix-socket IPC server."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import hmac
import json
import os
import shlex
import signal
import socket
import sys
import threading
import time
import traceback
import weakref
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit
from types import FrameType
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from hyprial import __version__
from hyprial.actor_runtime.policies import DEFAULT_POLICIES, EXTERNAL_IO
from hyprial.actor_runtime.scheduler import GenerationScheduler
from hyprial.adapters.lark import LarkSdkGateway
from hyprial.adapters.lark import lifecycle as lark_lifecycle
from hyprial.alarm import Alarm
from hyprial.autoupdate import SCHEDULE, InProcessAutoUpdateScheduler
from hyprial.agents import (
    AGENT_HEARTBEAT_TTL_SECONDS,
    RUNTIME_HEADLESS,
    RUNTIME_INTERACTIVE,
    Agent,
    AgentAlreadyRunning,
    AgentError,
    AgentHomeError as RegistryHomeError,
    HandoverNotice,
    PinConflictError,
    normalize_capabilities,
    normalize_harness_args,
)
from hyprial.adapters.lark.sdk import LarkApiError
from hyprial.transfer.container import CONTAINER_PYTHON as _CONTAINER_PYTHON
from hyprial.transfer.session_files import (
    TRANSFERABLE_HARNESSES,
    SessionFileError,
    SessionFileNotFound,
    locate_session_file,
    pi_session_file,
)
from hyprial.adapters.lark.scopes import (
    LarkScopeClient,
    LarkScopeRecovery,
    LarkScopeThrottleStore,
)
from hyprial.adapters.lark.runtime import (
    AdapterStartError,
    AdapterRuntime,
    LarkWorkerLauncher,
)
from hyprial.adapters.lark.reply_bridge import (
    LarkReplyBridgeTransport,
    lark_reply_adapter,
)
from hyprial.contracts import ipc_errors
from hyprial.contracts.daemon_diagnostics import DaemonStartupPhase
from hyprial.contracts.ipc_errors import DaemonRequestError
from hyprial.contracts.ports import PortAdmission
from hyprial.contracts.readiness import ReadinessReport
from hyprial.contracts.channel import (
    CHANNEL_LIVENESS_TTL_SECONDS,
    CHANNEL_PROTOCOL_VERSION,
    channel_generation,
    safe_channel_build_version,
)
from hyprial.contracts.session import (
    SESSION_CARRIER_SOURCES,
    is_session_fetch,
    reply_message_id,
)
from hyprial.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryLifecycle,
    DeliveryStatus,
    DeliveryStatusEndpoint,
    HoldPolicy,
    InboxAuthorityTimeout,
    InboxAuthorityUnavailable,
    InboxMessage,
    LocalFirstDeliveryTransport,
    OutboxItem,
    StatusQueryReport,
    StatusQueryServed,
    TerminalState,
    ZenohDeliveryTransport,
    ZenohInboxEndpoint,
    conflicting_message_ids,
    merge_delivery_status,
    query_delivery_status,
    publish_fetch_receipt,
)
from hyprial.inbox.progress import decode_progress_event
from hyprial.home import configured_hyprial_home
from hyprial.log import Logger, migrate_pre_trajectory_logs
from hyprial.management import (
    EnsureSquireRegistryCommand,
    RegistryManagementHandler,
)
from hyprial.org import OrgContextMesh, OrgContextStore
from hyprial.persistent_config import PersistentConfigStore, PersistentConfiguration
from hyprial.squire import (
    ReceiverUserDelivery,
    UserAdapterRegistry,
    UserDeliveryLedger,
    UserDeliveryRequest,
    UserDeliveryTarget,
    UserProfileStore,
    ZenohUserDeliveryEndpoint,
    ZenohUserDeliveryTransport,
    is_user_target,
)
from hyprial.status import build_actor_status_snapshot
from hyprial.contracts.forwarding import FORWARDING_COMMAND_ENV, FORWARDING_UP_ENV
from hyprial.forwarding_config import (
    FORWARDING_DEFAULT_MODE,
    FORWARDING_MODE_ENV,
    AutomaticForwarding,
    ForwardingConfigurationError,
    ForwardingPolicy,
    automatic_forwarding,
    daemon_forwarding_environment,
    forwarding_policy,
)
from .deprecations import deprecation_notices
from .discovery import (
    CommandEndpoints,
    ForwardingEndpoints,
    TailscaleEndpoints,
    local_tailnet_endpoint,
    merge_endpoints,
)
from .forwarding import ForwardingSidecarSupervisor
from hyprial.transport import (
    KeySpace,
    LivelinessDirectory,
    ZenohConfig,
    ZenohTransport,
    zenoh_environment_flag,
)
from hyprial.quota_watchdog import QuotaWatchdog
from hyprial.inbox.api import InboxPruneItem
from hyprial.inbox_watchdog import InboxWatchdog
from hyprial.usage import UsageCache, usage_collection_disabled
from .duplicate_watch import DUPLICATE_INSTANCE_EVENT, DuplicateInstanceWatch
from hyprial.contracts.agent_task import (
    AgentTaskError,
    validate_activity as validate_agent_task_activity,
    validate_cancel_request as validate_agent_task_cancel,
    validate_capabilities_request as validate_agent_task_capabilities,
    validate_result_request as validate_agent_task_result,
    validate_start as validate_agent_task_start,
    validate_status_request as validate_agent_task_status,
)
from hyprial.pac.agent_task import PacAgentTaskError, PacAgentTaskService
from hyprial.pac.workflow_runtime import GraphWorkflowService, WorkflowServiceError
from hyprial.dispatch.alarm import DispatchAlarm
from hyprial.inbox.io import (
    CorrelatedInboxEventRouter,
    InboxDeliveryIoAdapter,
    InboxIoError,
)
from hyprial.assign_reconcile import AssignReconcileReport
from hyprial.routine.pac_dispatch import PacRoutineDispatch
from hyprial.routine.service import RoutineService, RoutineServiceError
from hyprial.routine.store import RoutineStore

from .state_db import StateDatabase
from .desired_state import (
    DesiredState,
    DesiredStateStore,
    HarnessLaunchSpec,
    InteractiveSession,
)
from .exit_independence import do_not_exit, finish_shutdown
from .shutdown_stall import (
    STALL_DUMP_SIGNAL,
    arm_shutdown_stall_dump,
    dump_live_threads,
    live_thread_summary,
    register_stall_signal,
    stall_dump_path,
)
from .ownership import DaemonOwnershipBusy, DaemonStateOwnershipFence
from .composition import (
    AgentSessionDomains,
    CorrelatedDomainEvents,
    DomainCommandError,
    HarnessPortClient,
    LarkDesiredStatePort,
    LarkPortClient,
    harness_launch_projection,
)
from .atomic_lifecycle_ports import (
    AtomicLifecycleDomainPort,
    HarnessLifecycleDomainPort,
)
from .correlation import CorrelationEventRouter
from hyprial.contracts.lifecycle_budgets import (
    LIFECYCLE_OPERATION_DEADLINE_SECONDS,
    LIFECYCLE_WAIT_MARGIN_SECONDS,
)
from hyprial.dispatch.admission import dispatch_gate
from hyprial.dispatch.identity import dispatch_service_actor_uri
from hyprial.pac.workflow_schema import WorkflowSpec
from .lifecycle_manager import (
    LifecycleKind,
    LifecycleOperation,
    LifecyclePorts,
    LifecycleProcessManager,
    LifecycleSpec,
    LifecycleState,
    backfill_domain_attested_effects,
)
from .route_ports import RouteSpec
from .route_registration import RouteRegistrationClient, RouteRegistrationIo
from .session_actor import owner_only_relocation
from .session_ports import (
    HeartbeatSessionCommand,
    RefreshSessionCommand,
    RegisterSessionCommand,
    SessionMutationCompleted,
    SessionProjection,
    UnregisterSessionCommand,
)
from .owner_migration import migrate_owner_if_needed
from .identity import (
    DELIVERABLE_TARGET_KINDS,
    classify_target_identity,
    normalize_agent_recipient,
    read_settings_identity_metadata,
    resolve_node_owner,
)
from hyprial.uri import (
    ADAPTER_URI_PREFIX,
    AGENT_URI_PREFIX,
    CHANNEL_URI_PREFIX,
    TARGET_KIND_AGENT,
    TARGET_KIND_CHANNEL_ROUTE,
    TARGET_KIND_HOST,
    TARGET_KIND_UNKNOWN,
    TARGET_KIND_USER,
    agent_uri_actor,
    canonical_agent_uri,
    parse_agent_uri,
    parse_channel_uri,
)
from .ipc_stats import CallCostCounters, ipc_stats_payload
from .home_guard import ActiveDaemonHeartbeat, keepalive_duration_from_environment
from .route_delivery import (
    RouteDeliveryError,
    RouteResource,
    RouteTarget,
    find_gateway,
    is_route_target,
    map_lark_send_error,
    parse_route_resources,
    resolve_gateway_routes,
)
from .runtime import DaemonEventBridge, ForwardOutcome
from .top import build_top_snapshot
from .harness_actor import HarnessRuntimeActor

JsonObject = dict[str, Any]

#: How often the "nobody has taken this mail" sweep may run.  It is a GROUP BY
#: over every inbox row, so it rides a timer rather than the 1s reconcile tick;
#: one minute is far below the alert threshold it feeds (half the hold TTL), so
#: the gate costs no timeliness.
_INBOX_WATCH_INTERVAL_MS = 60_000

# _ensure_interactive_route declares an interactive session's zenoh liveliness
# token on THIS daemon's own transport session (see its docstring), not on the
# connector's. That token therefore survives for the daemon's entire lifetime
# regardless of whether the connector process (the stdio MCP child a Claude
# Code session owns) is still alive -- a dead connector and a live one are
# indistinguishable by raw presence alone. This TTL gates a real, independent
# signal instead: the most recent daemon contact from that exact session,
# touched on every session.register / session.refresh and on every fenced
# per-poll call (message.pending.list et al. via _fence_interactive_session).
# The production poll loop's default interval is 0.5s and its failure backoff
# caps at 5s (hyprial.mcp.channel._run_channel_poll_loop / _poll_backoff), so this
# generously outlives a few stalled retries. It assumes that default: the
# hidden `hyprial mcp claude-channel --poll-interval` override is debug-only and
# unused by any production launch path, but a caller who raises it well above
# this TTL would see spurious offline reports.
#
# A7 convergence: the value itself now lives with the one liveness model that
# uses it (hyprial.agents.liveness). This alias keeps the local name meaningful and
# guarantees ps, targets and delivery cannot drift onto different windows.
_INTERACTIVE_HEARTBEAT_TTL_SECONDS = AGENT_HEARTBEAT_TTL_SECONDS

# Grace between "shutdown finished" and "exit or be killed".  Only has to
# outlast a normal interpreter teardown, not the close itself -- `_close` runs
# to completion before this is armed.
#
# ⚠️ The note that used to sit here said the longest close observed in
# production was ~9s.  That was true when written and is not any more: the
# 2026-08-30 upgrade measured 20.5s (10s of untraced lifecycle drains, 5.0s
# harnesses, 5.4s zenoh).  Left visible rather than silently corrected,
# because the stale number is what the waits below were sized against.
_EXIT_BACKSTOP_SECONDS = 15.0

# ⚠️ How long a shutdown may take before this daemon guarantees it is gone.
#
# This constant exists because on 2026-08-31 nothing could answer that
# question. Three separate callers each needed it, none could derive it, so
# each invented its own literal -- the CLI waited 10s for the teardown
# receipt, a replacement daemon waited 15s for the lock, and the backstop
# fired at 15s. A real teardown took 20.5s. The upgrade path gave up first,
# never launched a replacement, and production was down for 56 minutes.
#
# 🔑 The failure was not that a number was too small. It was that the number
# being waited on **did not exist**, so every waiter guessed, and a wrong
# guess raised nothing anywhere.
#
# ⛔ And it is still NOT a hard bound, which has to be said plainly instead of
# being implied away by a confident name. `_close` also runs steps with **no
# timeout at all** -- `zenoh-transport` took 5.437s in that same shutdown and
# could in principle take longer. Worse, the exit backstop is armed only after
# `_close` *returns*, so a step that blocks forever is bounded by nothing at
# all. What this gives is the budgeted portion plus the backstop: enough that
# no waiter is wrong by construction, not enough to call the shutdown bounded.
# ⇒ The first draft of this constant was named ..._GUARANTEE_SECONDS. The name
#   was the overclaim: it described what we wanted rather than what the code
#   does, and every reader would have inherited the mistake.
#
# Two tests hold this, and they catch different failures:
#   * the sum test    -- a budget that CHANGED  (it is in the table, number drifted)
#   * the roster test -- a step that is MISSING (it is in no table at all)
# The first cannot see a step added with no timeout, because such a step does
# not move the sum. It would even leave the total looking more conservative
# while making it less true.
_CLOSE_STEP_BUDGETS = (
    ("maintenance-scheduler", 5.0),
    ("lifecycle-manager", 5.0),
    ("lifecycle-port:agent", 5.0),
    ("lifecycle-port:session", 5.0),
    ("lifecycle-port:harness", 5.0),
    ("route-registration", 5.0),
    ("harnesses", 5.0),
    # Bounded join of the restore thread (`_RESTORE_THREAD_JOIN_TIMEOUT`); a
    # restore that outlives it is left to the daemon-thread/backstop path.
    ("restore-thread", 2.0),
    ("remote-workflow", 5.0),
)

#: Closing steps that carry no timeout, each with the reason it is tolerated.
#: ⚠️ Membership here is a claim, and the roster test forces someone to make
#: it: a new step belongs to neither table, the suite goes red, and the author
#: has to decide which one it is rather than say nothing at all.
_CLOSE_UNBUDGETED_STEPS = (
    ("autoupdate", "cancels a timer; no I/O"),
    ("ipc-server", "closes a listening socket"),
    ("reserve-fd", "a single os.close"),
    ("ipc-clients", "closes accepted sockets already marked closing"),
    ("socket-file", "one unlink"),
    ("daemon-json", "one unlink"),
    ("routine-service", "joins a queue the step before it drained"),
    ("workflow-service", "same shape as routine-service"),
    (
        "degraded-workflow-component",
        "retries close() on workflow/remote/PAC-actor handles that already failed "
        "to drain at a degraded startup; same shapes as the steps above, and the "
        "list is empty unless startup degraded",
    ),
    ("remote-workflow-registrations", "Zenoh subscriber undeclare; no Python worker join, native transport has no deadline"),
    ("route-registrations", "in-memory unregister; observed 16ms"),
    ("adapters", "signals workers; observed 24ms"),
    ("inbox", "flushes a sqlite handle"),
    ("inbox-store", "closes that handle"),
    ("usage-cache", "stops a daemon thread"),
    ("actor-runtime", "pykka stop; its actors are daemon threads"),
    # ⚠️ The one that is not fine. No timeout, and 5.437s in the 2026-08-31
    # shutdown -- the largest unbudgeted contributor, and the reason this file
    # no longer claims a guarantee.
    ("zenoh-transport", "UNBOUNDED, observed 5.437s -- see the note above"),
    (
        "forwarding-sidecar",
        "UNBOUNDED after protocol/terminate deadlines: final killed-process wait has no timeout",
    ),
    ("home-lock", "closes the lock stream"),
)

_CLOSE_BUDGET_SECONDS = sum(seconds for _name, seconds in _CLOSE_STEP_BUDGETS)
TEARDOWN_BUDGETED_SECONDS = _CLOSE_BUDGET_SECONDS + _EXIT_BACKSTOP_SECONDS

# Distinguishes "the daemon had to be forced out" from a clean exit, so a
# supervisor or an operator reading exit codes can tell that a thread refused
# to be joined rather than that shutdown failed.
_EXIT_CODE_STUCK = 75

_IPC_MAX_CLIENTS = 64
_IPC_ACCEPT_RETRY_INITIAL = 0.05
_IPC_ACCEPT_RETRY_MAX = 1.0
_IPC_CLIENT_IDLE_TIMEOUT = 15.0
_IPC_CLIENT_POLL_INTERVAL = 0.1
_IPC_CLIENT_SHUTDOWN_GRACE = 0.1
_IPC_CLIENT_SHUTDOWN_TIMEOUT = 2.0

# Methods served while the restore gate is closed (restore in flight).
# `ping` is the readiness probe the 0.5s CLI pollers exist for; `shutdown`
# only sets an Event and is what keeps a daemon whose restore has wedged
# stoppable over IPC.  Everything else -- ps, top.snapshot, message.*,
# agent.*, ... -- is heavy: it reads desired state, actors or the mesh, and
# while restore has not finished it gets an immediate DAEMON_RESTORING
# refusal rather than a queued wait behind work that may take a minute.
_RESTORE_GATE_LIGHT_METHODS = frozenset({"ping", "shutdown"})
# The fixed key set of the per-method IPC cost counters (``ps`` →
# ``daemon.ipcStats.methods``): every method name ``handle`` and its
# delegates dispatch on.  Anything else -- a typo, a retired method, a
# hostile client inventing names -- folds into ``"(other)"``, so the counter
# map cannot be grown from outside.  A drift test re-derives this set from
# the method-name comparisons in this module, so a new method that is not
# added here fails CI instead of silently landing in ``"(other)"``.
_IPC_STATS_METHODS = frozenset(
    {
        "adapter.list",
        "adapter.pin",
        "adapter.pins",
        "adapter.reload",
        "adapter.start",
        "adapter.status",
        "adapter.stop",
        "adapter.unpin",
        "agent.create",
        "agent.destroy",
        "agent.destroy.preview",
        "agent.get",
        "agent.grant",
        "agent.grants",
        "agent.host-invite",
        "agent.list",
        "agent.resolve",
        "agent.revoke",
        "agent.runtime-context",
        "agent.secret.grant",
        "agent.secret.list",
        # Spelled split exactly like its dispatch site (term lint).
        "agent.secret." + "provider-write",
        "agent.secret.revoke",
        "agent.task.cancel",
        "agent.task.capabilities",
        "agent.task.observe",
        "agent.task.result",
        "agent.task.start",
        "agent.task.status",
        "autoupdate.notify",
        "autoupdate.status",
        "autoupdate.trigger",
        "dispatch.matrix.resolve",
        "down",
        "hosts",
        "identity.whoami",
        "lifecycle.start",
        "management.adapter.remove",
        "management.squire.ensure",
        "message.ack",
        "message.pending.list",
        "message.query",
        "message.reply",
        "message.send",
        "message.status",
        "org.fetch",
        "org.publish",
        "outbox.list",
        "outbox.prune",
        "pac.actor.stop",
        "pac.flag.reset",
        "pac.flag.set",
        "pac.graph.activate",
        "pac.graph.close",
        "ping",
        "progress.list",
        "ps",
        "routine.add",
        "routine.audit",
        "routine.list",
        "routine.pause",
        "routine.remove",
        "routine.resume",
        "routine.status",
        "session.heartbeat",
        "session.refresh",
        "session.register",
        "session.unregister",
        "shutdown",
        "targets",
        "top.snapshot",
        "transfer.complete",
        "transfer.plan",
        "transfer.precheck",
        "transfer.quiesce",
        "transfer.receive",
        "transfer.resume",
        "workflow.cancel",
        "workflow.complete",
        "workflow.fail",
        "workflow.history.list",
        "workflow.history.status",
        "workflow.list",
        "workflow.node.inspect",
        "workflow.remote.current",
        "workflow.start",
        "workflow.status",
        "workflow.worker.restart",
        "workflow.worker.stop",
    }
)
# How long `_close` waits for the restore thread before moving on.  The
# thread is a daemon and the loops it drives poll `stop_event`, so a join
# that outlasts this is the exit backstop's case, not a reason to hold
# teardown.
_RESTORE_THREAD_JOIN_TIMEOUT = 2.0


def _registration_handle_unhealthy_layers(
    handle: object, layer: str
) -> tuple[str, ...]:
    """Name every observable closed/unhealthy layer without exposing content."""

    closed = getattr(handle, "_closed", None)
    if closed is True:
        return (layer,)

    unhealthy: list[str] = []
    token = getattr(handle, "_token", None)
    endpoint = getattr(handle, "_endpoint", None)
    if token is not None:
        unhealthy.extend(
            _registration_handle_unhealthy_layers(token, f"{layer}.token")
        )
    if endpoint is not None:
        unhealthy.extend(
            _registration_handle_unhealthy_layers(endpoint, f"{layer}.endpoint")
        )
    children = getattr(handle, "_registrations", None)
    if isinstance(children, list):
        for index, child in enumerate(children):
            unhealthy.extend(
                _registration_handle_unhealthy_layers(
                    child, f"{layer}.registrations[{index}]"
                )
            )
    if unhealthy:
        return tuple(unhealthy)

    health = getattr(handle, "healthy", None)
    return (layer,) if health is False else ()


class _HarnessActorRegistration:
    """One harness connector's liveliness token plus its inbox endpoint."""

    def __init__(
        self,
        token: object,
        endpoint: object,
        *,
        actor_uri: str,
        layer: str,
        event_sink: Callable[..., None] | None = None,
    ) -> None:
        self._token = token
        self._endpoint = endpoint
        self._actor_uri = actor_uri
        self._layer = layer
        self._event_sink = event_sink
        self._closed = False
        self._invalidation_reported = False
        self._lock = threading.Lock()

    @property
    def healthy(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            unhealthy_layers = (
                *_registration_handle_unhealthy_layers(
                    self._token, f"{self._layer}.token"
                ),
                *_registration_handle_unhealthy_layers(
                    self._endpoint, f"{self._layer}.endpoint"
                ),
            )
            report = bool(unhealthy_layers) and not self._invalidation_reported
            if report:
                self._invalidation_reported = True
        if report:
            self._emit(
                "warn",
                "harness.actor.registration.invalidated",
                reason="child-handle-closed",
                initiator="health-probe",
                origin="externally-observed",
                unhealthyLayers=list(unhealthy_layers),
            )
        return not unhealthy_layers

    def _emit(self, level: str, event: str, **fields: Any) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(
                level,
                event,
                actorId=self._actor_uri,
                registrationLayer=self._layer,
                **fields,
            )
        except OSError:
            # Losing diagnostics must not prevent route teardown or recovery.
            return

    @staticmethod
    def _close_child(handle: object, *, reason: str, initiator: str) -> None:
        if isinstance(handle, _HarnessActorRegistration):
            handle.close(reason=reason, initiator=initiator)
            return
        close = getattr(handle, "close", None)
        if callable(close):
            close()

    def close(
        self,
        *,
        reason: str = "unspecified",
        initiator: str = "external-caller",
    ) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        attribution: dict[str, Any] = {}
        if reason == "unspecified":
            caller = traceback.extract_stack(limit=2)[0]
            attribution["caller"] = (
                f"{Path(caller.filename).name}:{caller.name}:{caller.lineno}"
            )
        self._emit(
            "warn" if reason == "unspecified" else "info",
            "harness.actor.registration.closed",
            reason=reason,
            initiator=initiator,
            origin="explicit-close",
            **attribution,
        )
        self._close_child(self._endpoint, reason=reason, initiator=initiator)
        self._close_child(self._token, reason=reason, initiator=initiator)


class _LocalPresence:
    """Presence for ps/targets/delivery, all sharing this one judgment call.

    Raw zenoh liveliness is trusted as-is for this node's own identity, for
    daemon-managed harness workers (their token tracks a real supervised
    subprocess -- see ``_reconcile_harness_actors``), and for genuinely remote
    peers. For a daemon-owned interactive-session route, the raw token alone
    cannot tell a dead connector from a live one (see the TTL constant's
    docstring), so ``interactive_liveness`` -- when it recognizes the actor --
    overrides the raw token with a real heartbeat-freshness verdict.
    """

    def __init__(
        self,
        inner: LivelinessDirectory,
        actor: str,
        *,
        interactive_liveness: Callable[[str], bool | None] | None = None,
    ) -> None:
        self.inner = inner
        self.actor = actor
        self._interactive_liveness = interactive_liveness

    def actor_online(self, actor: str) -> bool:
        if actor == self.actor:
            return True
        if not self.inner.actor_online(actor):
            return False
        if self._interactive_liveness is not None:
            verdict = self._interactive_liveness(actor)
            if verdict is not None:
                return verdict
        return True

    def online_mailboxes(self) -> tuple[str, ...]:
        return self.inner.online_mailboxes()

    def online_actors(self) -> tuple[str, ...]:
        candidates = self.raw_online_actors()
        if self._interactive_liveness is None:
            return candidates
        return tuple(sorted(actor for actor in candidates if self.liveness_keeps(actor)))

    def liveness_keeps(self, actor: str) -> bool:
        """The interactive-liveness half of ``online_actors``, for one candidate.

        Identity-first batch paths (``hosts``, ``org.fetch``) reuse exactly
        this predicate on the host-kind survivors of their filter, so their
        output stays byte-identical to ``online_actors()``-then-filter --
        including the name-collision case where a local worker's bare name
        equals a remote node id and a stopped worker answers False
        (h2b-developer 2026-09-15 批复①:逐字保真,不依赖「病态配置不会
        出现」).  Inside a request snapshot the verdict is table lookups --
        no store lock, no SQLite -- so the recheck costs nothing per host.
        """

        if actor == self.actor or self._interactive_liveness is None:
            return True
        return self._interactive_liveness(actor) is not False

    def raw_online_actors(self) -> tuple[str, ...]:
        """The candidate set ``online_actors`` starts from, before any liveness.

        Host-identity batch paths (``hosts`` rows, ``org.fetch`` candidate
        enumeration) filter on identity first and only then pay the per-actor
        interactive-liveness verdict for the host-kind survivors: a bare
        node id normally carries no local verdict (``AgentLiveness.verdict``
        answers None for one), and the survivors' recheck is what keeps the
        name-collision case byte-faithful.  Same union as ``online_actors``
        -- this node's own token plus the raw directory -- so the two views
        cannot drift apart on the candidate set itself.
        """

        return tuple(sorted({self.actor, *self.inner.online_actors()}))


class _RouteGatewayCache:
    """Thread-safe cache for outbound route SDK resources."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gateways: dict[str, LarkSdkGateway] = {}

    def get(self, name: str) -> LarkSdkGateway | None:
        with self._lock:
            return self._gateways.get(name)

    def put(self, name: str, gateway: LarkSdkGateway) -> LarkSdkGateway:
        with self._lock:
            incumbent = self._gateways.setdefault(name, gateway)
            return incumbent


class _WorkerStatusSnapshot:
    """One request's shared view of managed-worker state (ps / agent.list / agent.get).

    The ps storm this replaces: every agent verdict paid one supervisor
    ``status()`` round trip of its own, and ``_canonical_harness_uri``'s
    fallback paid one desired-state load per connector per verdict — N agents
    × M connectors of full loads for a single ps.  Building the two lookup
    tables once per request collapses both to constants, and every row in the
    response then reads the same tables, so one ps is internally consistent
    by construction.  That row consistency is an intentional semantics
    choice, not a side effect of the fix.
    """

    def __init__(
        self,
        *,
        statuses: tuple[dict[str, object], ...],
        running_by_actor: dict[str, bool],
        session_ref_by_actor: dict[str, str | None],
        desired: "DesiredState | None" = None,
    ) -> None:
        self.statuses = statuses
        self.running_by_actor = running_by_actor
        self.session_ref_by_actor = session_ref_by_actor
        # The one desired-state load this request paid (card 259 P1 meets
        # the request snapshot): _actor_status_snapshot reuses it instead of
        # loading a second time.  None when the store had nothing to give.
        self.desired = desired



#: A forward (user-proxy) whose recipient can never resolve by retrying.
FORWARD_TARGET_UNKNOWN = "FORWARD_TARGET_UNKNOWN"
#: Send-boundary refusals that are about the ADDRESS, not the moment: the
#: name matches nothing / matches twice / is malformed, or the route or its
#: adapter is not configured.  Anything else (a Lark send fault) is retried.
_FORWARD_UNKNOWN_TARGET_CODES = frozenset(
    {
        ipc_errors.TARGET_IS_NODE,
        ipc_errors.UNSUPPORTED_TARGET,
        ipc_errors.AMBIGUOUS_TARGET,
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.ROUTE_ADAPTER_UNCONFIGURED,
        "ROUTE_NOT_CONFIGURED",
        "ROUTE_FANOUT_MEMBER_INVALID",
    }
)

class DaemonApplication:
    """Own the real runtime graph and expose it through newline-delimited IPC."""

    def __init__(
        self,
        *,
        state_dir: Path,
        socket_path: Path,
        node_id: str,
        hyprial_home: Path | None = None,
        zenoh_listen: tuple[str, ...] = (),
        zenoh_connect: tuple[str, ...] = (),
        owner: str | None = None,
        usage_cache: UsageCache | None = None,
        keepalive_duration: float = 5.0,
        forwarding_discovery: ForwardingEndpoints | None = None,
        forwarding_environment: Mapping[str, str] | None = None,
        forwarding_policy: ForwardingPolicy | None = None,
        forwarding_automatic: AutomaticForwarding | None = None,
        forwarding_unavailable: str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.socket_path = Path(socket_path)
        self.node_id = node_id
        # The seam that makes leaving testable without ending the test runner.
        # Production keeps `os._exit`: skipping `atexit` is the point, since
        # those handlers are where the un-timed joins we are escaping live.
        self._exit_process: Callable[[int], object] = os._exit
        self.hyprial_home = Path(hyprial_home) if hyprial_home is not None else (
            self.state_dir.parent if self.state_dir.name == "state" else self.state_dir
        )
        self.owner = (
            owner
            if owner is not None
            else resolve_node_owner(hyprial_home=self.hyprial_home)
        )
        self.identity_mode, self.identity_issuer = read_settings_identity_metadata(
            hyprial_home=self.hyprial_home
        )
        # ⭐ Rewrite the owner segment of stored addresses, once, before any
        # store below reads them.  Ordering is the whole point: DesiredStateStore
        # and the registries are constructed a few lines down, and a store that
        # had already loaded the pre-migration document would keep serving the
        # old owner for the life of this process.
        #
        # Idempotent — it detects the previous owner from the data and does
        # nothing once none remains.  ⚠️ An abort here stops the daemon
        # starting, deliberately: state this cannot account for should not be
        # served half-rewritten.  See owner_migration for what "account for"
        # means and what the abort reports.
        # Held, not logged here: the logger does not exist yet (it is built
        # below, after the stores).  A migration that rewrote thousands of
        # cells and reported nothing is indistinguishable from one that found
        # nothing to do, so the count is emitted as soon as there is somewhere
        # to emit it.
        self._owner_migration_rewrites = migrate_owner_if_needed(
            state_dir=self.state_dir, hyprial_home=self.hyprial_home, owner=self.owner
        )
        self.zenoh_listen = zenoh_listen
        self.zenoh_connect = zenoh_connect
        #: Set by from_environment when HYPRIAL_NETWORK_ISOLATED is on: no
        #: path off this machine (see _isolated_endpoints for the inventory).
        self.network_isolated = False
        #: What startup ACTUALLY did on the network paths the switch governs,
        #: recorded where each decision is made -- so `ps` reports the
        #: effective state, not an echo of the environment variable.
        self._startup_network: dict[str, bool] = {
            "listenDerived": False,
            "discoveryConsulted": False,
            "gossip": False,
        }
        self._forwarding_discovery = forwarding_discovery
        # The sidecar launch variables this daemon resolved for itself
        # (``from_environment``), so every launch path -- direct ``daemon
        # run``, a watchdog, a service manager -- gets forwarding from the
        # same operator inputs, not only the CLI launcher that used to
        # precompute them in the parent.  Constructed directly (tests), the
        # already-generated variables are read from the environment.
        self._forwarding_environment: dict[str, str] = (
            dict(forwarding_environment)
            if forwarding_environment is not None
            else {
                name: os.environ[name]
                for name in (FORWARDING_COMMAND_ENV, FORWARDING_UP_ENV)
                if os.environ.get(name)
            }
        )
        # The operator policy (``HYPRIAL_FORWARDING``) and, in ``auto``/``on``,
        # either an eligible home's plan -- completed once startup has bound
        # the loopback inbound port -- or the reason it is not eligible.
        self._forwarding_policy = forwarding_policy or ForwardingPolicy(
            FORWARDING_DEFAULT_MODE, "default"
        )
        self._forwarding_automatic = forwarding_automatic
        self._forwarding_unavailable = forwarding_unavailable
        self._forwarding_effective: tuple[str, ...] = ()
        self._forwarding_start_attempted = forwarding_discovery is not None
        self._forwarding_supervisor: ForwardingSidecarSupervisor | None = None
        self._forwarding_dialed: tuple[str, ...] | None = None
        # The other two parts of the session's connect set, kept apart so a
        # forwarding redial recomposes it instead of dropping them: the
        # configured endpoints and what the host-tailnet directory returned
        # at startup (kept additive during migration, plan §F).
        self._connect_configured: tuple[str, ...] = ()
        self._connect_discovered: tuple[str, ...] = ()
        # The forwarding set a redial was last attempted for: a failed
        # rebuild is retried when the sidecar's set changes again, not on
        # every tick (a persistent failure stays visible as restartRequired).
        self._forwarding_redial_attempted: tuple[str, ...] | None = None
        # One shared state database (U0a-2 丙): desired state and the
        # lifecycle journal write through the same serialized connection
        # owner, so in-process contention between the two is gone by
        # construction.
        self.state_db = StateDatabase(self.state_dir / "lifecycle-operations.sqlite3")
        self.desired_state = DesiredStateStore(
            self.state_dir / "desired-state.json", state_db=self.state_db
        )
        self.persistent_config = PersistentConfigStore(self.hyprial_home, self.state_dir)
        self.user_profiles = UserProfileStore(self.state_dir / "users.json")
        self.user_adapters = UserAdapterRegistry()
        self.stop_event = threading.Event()
        self.epoch = uuid4().hex
        self._workflow_service: GraphWorkflowService | None = None
        self._routine_service: RoutineService | None = None
        self._routine_coordinator_lock = threading.RLock()
        self._pac_notification_io: InboxDeliveryIoAdapter | None = None
        self._pac_actor_service: Any | None = None
        self._lifecycle_manager: LifecycleProcessManager | None = None
        self._lifecycle_domain_ports: tuple[AtomicLifecycleDomainPort, ...] = ()
        self._route_registration: RouteRegistrationIo | None = None
        self._routes: RouteRegistrationClient | None = None
        self._lifecycle_router: CorrelationEventRouter | None = None
        self._runtime: DaemonEventBridge | None = None
        self._transport: ZenohTransport | None = None
        self._remote_workflow = None
        self._degraded_workflow_handles: list[Any] = []
        self._presence: _LocalPresence | None = None
        self._directory: LivelinessDirectory | None = None
        self._inbox: DeliveryCustodyFacade | None = None
        self._inbox_coordinator: DeliveryCustodyCoordinator | None = None
        self._harnesses: HarnessPortClient | None = None
        self._registry_management: RegistryManagementHandler | None = None
        self._registry_management_lock = threading.Lock()
        self._adapters: AdapterRuntime | None = None
        self._lark_events: CorrelatedDomainEvents | None = None
        self._lark_client: LarkPortClient | None = None
        self._endpoint: ZenohInboxEndpoint | None = None
        self._status_endpoint: DeliveryStatusEndpoint | None = None
        self._user_endpoint: ZenohUserDeliveryEndpoint | None = None
        self._user_delivery: ZenohUserDeliveryTransport | None = None
        self._org_endpoint: OrgContextMesh | None = None
        self._actor_token: Any | None = None
        self._duplicate_watch: DuplicateInstanceWatch | None = None
        # Set by the home guard's pre-claim copied-home detection: the frozen
        # record detail when this home was started from a live daemon's copy.
        self._startup_duplicate: JsonObject | None = None
        self._route_gateway_cache = _RouteGatewayCache()
        self._interactive_route_lock = threading.RLock()
        self._clock: Callable[[], float] = time.monotonic
        self._maintenance_scheduler = GenerationScheduler()
        self._maintenance_generation = 0
        self._owns_process_exit = False
        # The daemon.readiness projection (phase ③): one entry per connector
        # that has ever reported, keyed by the report's source, holding the
        # latest report.  `_readiness_expected` is the desired set the
        # startup restore declared; `_readiness_first_round` flips when every
        # expected source has handed in a first report -- the EVENT "the
        # first round dispositioned every desired connector", which ping
        # surfaces as phase "reconciled".  The daemon records arrival and
        # forwards verdicts; it never interprets report content.
        self._readiness_expected: frozenset[str] = frozenset()
        self._readiness_reports: dict[str, ReadinessReport] = {}
        self._readiness_first_round = threading.Event()
        # The raw registry/liveness stores are private to these two actors.
        # Application keeps only typed command/projection facades.
        application_ref = weakref.ref(self)

        def actor_worker_running(
            actor: str, desired: DesiredState | None = None
        ) -> bool | None:
            application = application_ref()
            if application is None:
                return None
            return application._managed_worker_running(actor, desired)

        def actor_clock() -> float:
            application = application_ref()
            return time.monotonic() if application is None else application._clock()

        self._agent_session_domains = AgentSessionDomains(
            database=self.state_dir / "agents.sqlite3",
            desired_state=self.desired_state,
            owner=self.owner,
            node_id=self.node_id,
            daemon_epoch=self.epoch,
            hyprial_home=self.hyprial_home,
            worker_running=actor_worker_running,
            clock=actor_clock,
        )
        self.agents = self._agent_session_domains.agents
        self._agent_liveness = self._agent_session_domains.liveness
        # P1b B1: the compose path needs the registry's grant/receipt
        # surface, which the compatibility facade deliberately does not
        # expose — mutations go through Agent commands; the daemon-side
        # environment composition is a read, not a mutation.
        self._agent_registry = self._agent_session_domains._registry  # noqa: SLF001
        self._agent_domains_finalizer = weakref.finalize(
            self, self._agent_session_domains.close
        )
        self._server: socket.socket | None = None
        self._accept_reserve_fd: int | None = None
        self._ipc_client_slots = threading.BoundedSemaphore(_IPC_MAX_CLIENTS)
        # Per-method handler cost (see ipc_stats for the attribution rule and
        # the calibration contract).  Owned here, not per connection: the
        # client threads are short-lived and the totals must outlive them.
        self._ipc_stats = CallCostCounters(_IPC_STATS_METHODS, wall=True)
        self._ipc_clients: set[socket.socket] = set()
        self._ipc_client_threads: set[threading.Thread] = set()
        self._ipc_clients_lock = threading.Lock()
        self._ipc_closing = False
        # The restore gate: while a restore is pending, the dispatch layer
        # (`handle`) answers only the light set (`_RESTORE_GATE_LIGHT_METHODS`)
        # and refuses everything else with DAEMON_RESTORING immediately --
        # probes never queue behind a restore the way 0.5s `ps` polls once
        # filled the listen backlog before the accept loop even ran.
        #
        # Default-open on purpose: the gate means "a restore thread owns the
        # remaining startup", so it is cleared by `run()` when that thread is
        # created and set again when restore completes.  A DaemonApplication
        # whose `_serve` is driven directly (the IPC test suites) never has a
        # restore pending, and gating it would change what those tests cover.
        self._restore_done = threading.Event()
        self._restore_done.set()
        self._provider_auth: Any = None
        self._restore_thread: threading.Thread | None = None
        self._restore_error: BaseException | None = None
        # One startup-only diagnostic per interactive session whose binding
        # could not be restored beside an already-live connector.  Desired
        # state remains the authority and is never edited here: the operator
        # needs both conflicting records intact to decide which one to stop.
        self._agent_recovery_failures: dict[str, JsonObject] = {}
        # A persisted session is removed only when its PID plus birth identity
        # proves that exact carrier process is gone.  Keep the startup decision
        # visible in ps after the row itself has been retired.
        self._agent_recovery_cleanups: dict[str, JsonObject] = {}
        # Per-request worker-status snapshot (ps / agent.list / agent.get).
        # IPC clients each run on their own thread, so the "current" snapshot
        # is a thread-local: one request's table never leaks into a
        # concurrent request's verdicts.
        self._worker_snapshot_local = threading.local()
        self._lock_stream: Any | None = None
        self._logger = Logger.daemon(self.state_dir, name=self.node_id)
        # A3 dispatch gate (design-dispatch-always-pac-2026-09-03 §三②): the
        # live numerator of the PAC bypass rate, surfaced on ps/top.  The
        # durable record is the daemon.jsonl dispatch.without_pac stream;
        # this in-memory count covers the current daemon epoch only.
        self._dispatch_without_pac_lock = threading.Lock()
        self._dispatch_without_pac_count = 0
        # Narrowed gate (spec-dispatch-gate-classifier-2026-09-04): the
        # request-shape sends the gate does NOT count, same epoch scope.
        self._dispatch_conversation_count = 0
        # Gate condition (a) bookkeeping: conversation id → the operation id
        # that opened it through message.send.  A send opens its
        # conversation iff no DIFFERENT operation used it first (an
        # idempotent replay of the same send still opens it).  Epoch-scoped
        # like the counters; the durable record stays daemon.jsonl.
        self._dispatch_gate_conversations: dict[str, str] = {}
        # Subscription quota cache for `hyprial top`: refreshed by a background
        # thread (started in run()), read-only on every IPC path.  Injected
        # by from_environment so directly-constructed test applications never
        # start a network-touching refresher.
        self._usage_cache = usage_cache
        # Quota watchdog (Allen 09-17): evaluates each cache refresh and each
        # PROVIDER_USAGE_LIMIT turn, and tells the owner through Squire.  It
        # exists only with the cache -- no readings, nothing to watch.
        self._quota_watchdog: QuotaWatchdog | None = None
        if usage_cache is not None:
            try:
                self._quota_watchdog = QuotaWatchdog(
                    state_dir=self.state_dir,
                    deliver=self._quota_watchdog_deliver,
                    readings=usage_cache.snapshots,
                    clock_ms=lambda: time.time_ns() // 1_000_000,
                )
            except (OSError, ValueError) as error:
                # An unreadable state file disables the watchdog loudly; it
                # never blocks the daemon from starting.
                self._log(
                    "error",
                    "daemon",
                    "quota_watchdog.disabled",
                    error=f"{type(error).__name__}: {error}",
                )
            else:
                usage_cache.set_refresh_observer(self._on_usage_refreshed)
        # Inbox watchdog (hq-adjutant 2026-09-18, after 11 messages expired
        # unread while their recipient kept working): says so BEFORE the
        # deadline, and says so again if the sweep throws mail away while the
        # recipient is running.  ⛔ It does not change what the sweep reaps.
        self._inbox_watchdog: InboxWatchdog | None = None
        self._inbox_watchdog_checked_at_ms = 0
        try:
            self._inbox_watchdog = InboxWatchdog(
                state_dir=self.state_dir,
                deliver=self._inbox_watchdog_deliver,
                clock_ms=lambda: time.time_ns() // 1_000_000,
            )
        except (OSError, ValueError) as error:
            self._log(
                "error",
                "daemon",
                "inbox_watchdog.disabled",
                error=f"{type(error).__name__}: {error}",
            )
        self._home_guard = ActiveDaemonHeartbeat(
            self.hyprial_home,
            keepalive_duration=keepalive_duration,
            ownership_lost=self.stop_event.set,
            duplicate_detected=self._on_startup_duplicate_detected,
            duplicate_check_failed=self._on_duplicate_check_failed,
        )
        self._autoupdate = InProcessAutoUpdateScheduler(
            state_dir=self.state_dir,
            hyprial_home=self.hyprial_home,
            logger=lambda level, event, **fields: self._log(
                level, "autoupdate", event, **fields
            ),
        )

    def _on_startup_duplicate_detected(self, detail: dict[str, Any]) -> None:
        """Home guard's copied-home verdict: log it and keep it for ps/doctor."""

        self._startup_duplicate = dict(detail)
        self._log("error", "daemon", DUPLICATE_INSTANCE_EVENT, **detail)

    def _on_duplicate_check_failed(self, detail: str) -> None:
        """The background copied-home check died: warn, never silent."""

        self._log("warn", "daemon", "daemon.identity.duplicate_check_failed", detail=detail)

    def _duplicate_instance_payload(self) -> JsonObject:
        """Duplicate-instance verdict for ps/doctor, from both detectors.

        Two sources: the mesh watch (foreign generations of this node
        identity seen on liveliness) and the home guard's pre-claim
        copied-home detection.  The mesh half only sees peers that declare
        a generation liveliness token -- a pre-generation duplicate is
        invisible to it and only the startup detection (same machine)
        covers that case.  That limit ships in the payload so no reader
        can mistake this for whole-mesh coverage.
        """

        mesh = (
            self._duplicate_watch.status_payload()
            if self._duplicate_watch is not None
            else {"active": False, "meshPeerGenerations": []}
        )
        return {
            "active": bool(mesh["active"]) or self._startup_duplicate is not None,
            "meshPeerGenerations": mesh["meshPeerGenerations"],
            "startupRecord": self._startup_duplicate,
            "meshDetectionCoverage": (
                "only peers that declare a generation liveliness token; "
                "a pre-generation duplicate is invisible to mesh detection "
                "and is covered only by startup copied-home detection on "
                "the same machine"
            ),
        }

    @classmethod
    def from_environment(
        cls, *, state_dir: Path, socket_path: Path
    ) -> DaemonApplication:
        node_id = os.environ.get("HYPRIAL_NODE_ID", socket.gethostname()).strip()
        if not node_id:
            raise ValueError("HYPRIAL_NODE_ID must not be empty")
        zenoh_listen = _endpoint_list("HYPRIAL_ZENOH_LISTEN")
        zenoh_connect = _endpoint_list("HYPRIAL_ZENOH_CONNECT")
        isolated = network_isolated_from_environment()
        if isolated:
            zenoh_listen, zenoh_connect = _isolated_endpoints(
                zenoh_listen, zenoh_connect
            )
        hyprial_home = configured_hyprial_home()[0]
        (
            policy,
            forwarding_environment,
            automatic,
            unavailable,
        ) = _resolve_forwarding(
            hyprial_home, node_id, isolated=isolated, zenoh_listen=zenoh_listen
        )
        app = cls(
            state_dir=state_dir,
            socket_path=socket_path,
            node_id=node_id,
            # HARNESS_STATE_DIR can live elsewhere, but it must not silently
            # redefine which HYPRIAL home this daemon owns.
            hyprial_home=hyprial_home,
            zenoh_listen=zenoh_listen,
            zenoh_connect=zenoh_connect,
            usage_cache=(
                None
                if isolated or usage_collection_disabled()
                else UsageCache()
            ),
            keepalive_duration=keepalive_duration_from_environment(),
            forwarding_environment=forwarding_environment,
            forwarding_policy=policy,
            forwarding_automatic=automatic,
            forwarding_unavailable=unavailable,
        )
        app.network_isolated = isolated
        return app

    def run(
        self,
        *,
        ownership_stream: Any | None = None,
        identity_transaction_stream: Any | None = None,
    ) -> None:
        if ownership_stream is not None:
            if self._lock_stream is not None:
                raise RuntimeError("daemon state ownership is already installed")
            self._lock_stream = ownership_stream
        previous_handlers: dict[int, Any] = {}
        try:
            self._home_guard.claim()
        except BaseException:
            # ``daemon_run`` transfers the already-held ownership descriptor
            # before the home guard is claimed.  A guard failure must release
            # that descriptor, but must not run the broad partial-runtime
            # shutdown path used after startup has begun.
            if ownership_stream is not None and self._lock_stream is not None:
                self._lock_stream.close()
                self._lock_stream = None
            raise
        # Startup announces each phase for the same reason shutdown does, and
        # for a sharper one: everything between resolving endpoints and
        # `daemon.ready` used to emit nothing at all. A daemon stuck in here
        # was indistinguishable from a daemon doing nothing -- 66 seconds of
        # silence with no way to tell which step owned them, while the caller
        # gave up on a ready timeout and reported a failure naming no cause.
        # A `begin` with no `end` names the step; the elapsed time on each
        # `end` is what turns "slow startup" into a number that can be
        # compared against that timeout.
        def step(operation: Callable[[], Any], phase: DaemonStartupPhase) -> None:
            if not isinstance(phase, DaemonStartupPhase):
                raise TypeError("daemon startup phases must use DaemonStartupPhase")
            phase_name = phase.value
            self._log_trace("info", "daemon.start.begin", phase=phase_name)
            started = time.monotonic()
            try:
                operation()
            except BaseException as error:
                self._log_trace(
                    "warn",
                    "daemon.start.failed",
                    phase=phase_name,
                    errorType=type(error).__name__,
                    error=str(error)[:500],
                    elapsedMs=int((time.monotonic() - started) * 1000),
                )
                # The launch summary reads the launch capture (stderr), not
                # daemon.jsonl -- mirror the failure name there or a failed
                # `hyprial init` reports `daemonEvents: []`.
                _mirror_startup_event_to_stderr(
                    "daemon.start.failed", phase=phase_name
                )
                raise
            self._log_trace(
                "info",
                "daemon.start.end",
                phase=phase_name,
                elapsedMs=int((time.monotonic() - started) * 1000),
            )

        run_failed = False
        try:
            # ⚠️ Nothing above `_migrate_logs_at_startup` may write a log line.
            # Migration decides what to archive by looking at what this home
            # already contains, so a phase event emitted first would be a log
            # this startup created being treated as one it inherited -- which
            # is exactly what `test_empty_home_is_marked_before_new_contract_
            # logs_are_created` exists to catch. These two steps are also the
            # cheap ones; the silence this instrumentation was added for was
            # never here.
            if self._lock_stream is None:
                self._acquire_lock()
            previous_handlers = self._install_signal_handlers()
            # Armed here rather than at teardown: the point of the signal path
            # is that it answers at *any* moment, including while the daemon is
            # perfectly healthy but not responding to something else.
            self._register_stall_signal_if_owned()
            try:
                self._migrate_logs_at_startup()
            except Exception as error:  # noqa: BLE001 - startup must continue
                self._warn_log_migration_failure(error)
            # ⛔ Emitted HERE, not where the migration runs.  The owner rewrite
            # happens in __init__, long before the logger exists, and the line
            # above is the boundary this file states in the comment overhead:
            # nothing may write a log line until `_migrate_logs_at_startup`
            # has decided what this home inherited.
            #
            # ⚠️ The first version of this emit sat next to the logger's
            # construction and fired only when `rewrites > 0`.  That passed CI
            # for the wrong reason -- no test migrates anything, so the line
            # was never written -- while on a real migrating node it would
            # have written a pre-archival log every time.  A branch that only
            # runs in production is only tested in production.
            #
            # Unconditional, including the zero: firing only on a non-zero
            # count restores the "ran, found nothing" / "never ran" ambiguity
            # this count exists to remove, and the zero is precisely the
            # reading that says a node is already migrated.
            self._log(
                "info",
                "daemon",
                "identity.owner_migration.applied",
                owner=self.owner,
                rewrittenCells=self._owner_migration_rewrites,
            )
            self._warn_if_worker_proxy_absent()
            step(self._start_runtime, DaemonStartupPhase.ACTOR_RUNTIME)
            step(self._start_server, DaemonStartupPhase.IPC_SERVER)
            # daemon.json now means "serving", not "restored": it is written
            # at the phase-① boundary -- socket bound, accept about to run,
            # ping answerable -- so the CLI can key its readiness probe on it
            # without waiting for restore.  Everyone reading it as "restore
            # complete" was already wrong once: the CLI's 0.5s `ps` probes
            # filled the backlog because accept had not started, marker or no.
            step(self._write_pid_file, DaemonStartupPhase.PID_FILE)
            if self._usage_cache is not None:
                step(self._usage_cache.start, DaemonStartupPhase.USAGE_CACHE)
            step(self._autoupdate.start, DaemonStartupPhase.AUTOUPDATE)
            # Restore leaves the startup path here.  Adapter workers receive
            # this socket path and may use it as soon as their native stream
            # becomes ready (including history replay), so the socket must
            # exist while they restore -- and now answers them: the accept
            # loop runs on the main thread while restore finishes in the
            # background, light methods (ping/shutdown) are served throughout,
            # and heavy methods get an immediate DAEMON_RESTORING rather than
            # parking in the listen backlog until accept starts.
            #
            # Shape (b) of the two candidates: restore goes to a thread, the
            # main thread enters `_serve`.  Signal handling and the exit path
            # keep their main-thread assumptions; the thread's wait for the
            # fleet is bounded by the per-start settlement timers the actor
            # arms at admission (a reconcile strategy parameter), and restore
            # progress is observed through the phase-③ readiness reports,
            # never through daemon.json.
            self._start_restore_thread(step)
            # The inherited identity-transaction descriptor is the daemon's
            # startup authorization.  Release it only once this generation is
            # ready to accept ping/shutdown; the parent keeps its duplicate
            # through post-launch verification.  Direct ``daemon run`` uses
            # this same path, so it cannot race a login stop/commit window.
            if identity_transaction_stream is not None:
                identity_transaction_stream.close()
                identity_transaction_stream = None
            self._serve()
            # A restore step that escapes its own recovery must still fail the
            # daemon, as it did when restore ran inline -- just through the
            # stop path now that the accept loop owns the main thread.
            if self._restore_error is not None:
                raise self._restore_error
        except BaseException:
            # Cleanup success must not erase the failure that sent us here.
            # Production leaves from the finally block below via os._exit, so
            # the exception itself never reaches the parent process; carry its
            # existence into the status chosen after every shutdown step runs.
            run_failed = True
            raise
        finally:
            if identity_transaction_stream is not None:
                identity_transaction_stream.close()
                identity_transaction_stream = None
            try:
                self._log("info", "daemon", "daemon.stopping", nodeId=self.node_id)
                self._close()
            finally:
                # Armed *before* the closing steps rather than after them.
                # After, a hang inside `_home_guard.close` would leave nothing
                # armed at all -- the guarantee would be missing in exactly the
                # case it exists for. It costs nothing when the steps finish,
                # because the process is gone before its timer matures.
                self._arm_exit_backstop()
                shutdown_errors = self._finish_shutdown_and_leave(
                    previous_handlers, failed=run_failed
                )
                # Reached only when this daemon does not own the process; in
                # production `_finish_shutdown_and_leave` does not return.
                # ⚠️ Raised rather than swallowed so a failed teardown step is
                # not something the suite can pass through in silence.
                if shutdown_errors:
                    raise BaseExceptionGroup(
                        "daemon shutdown steps failed", list(shutdown_errors)
                    )

    def owns_process_exit(self) -> None:
        """Declare that this daemon *is* the process, so it may force the exit.

        Off by default, and the default is the safe one. A DaemonApplication
        does not always own the interpreter it runs in -- the test suite starts
        real daemons in-process, and forcing an exit there kills the host, not
        a daemon. That is not hypothetical: arming this unconditionally took
        down a full pytest run at 28% with the backstop's own exit code.

        So only the entry point that spawned an interpreter *to be* a daemon
        turns it on: `hyprial daemon run`.
        """

        self._owns_process_exit = True

    def _arm_shutdown_stall_dump_if_owned(self) -> bool:
        """Arm the stall watchdog, but only when this process is ours to watch.

        Arming a process-wide timer is a process-level act, exactly like ending
        the process, so it answers to the same question and must not grow a
        second answer to it. `_arm_exit_backstop` already asks
        `_owns_process_exit`; two independent readings of "do we own this
        interpreter" would drift, and the day they disagree nothing reports it.

        ⚠️ Not a precaution -- the ungated version did damage. A full suite
        armed this from an in-process `_close()`, and thirty seconds later it
        printed seventy-nine thread stacks into an unrelated end-to-end test
        that was waiting on a thirty-second subprocess budget. The diagnostic
        became the disturbance, in a process it had no business watching.
        """

        if not self._owns_process_exit:
            return False
        return arm_shutdown_stall_dump()

    def _register_stall_signal_if_owned(self) -> object | None:
        """Wire the on-demand stack dump, but only in a process that is ours.

        Third of the three process-level acts behind one predicate, with the
        arming watchdog and the forced exit. ⚠️ This one needs the gate most,
        and it is the one that would have been easiest to leave ungated,
        because its damage is the only kind that produces no failure:

            forcing an exit      kills the host      -> the suite stops dead
            dumping to stderr    floods the host     -> a test goes red
            claiming a signal    silently replaces the host's own handler
                                 -> nothing fails, and one day somebody's
                                    program stops responding to SIGUSR1

        The first two announce themselves. The third is only discovered by
        whoever eventually depended on the behaviour we took away.
        """

        if not self._owns_process_exit:
            return None
        handle = register_stall_signal(path=stall_dump_path(self.state_dir))
        self._stall_signal_handle = handle
        if handle is not None:
            self._log_trace(
                "info",
                "daemon.stall.dump.armed",
                signal=STALL_DUMP_SIGNAL,
                stackDump=str(stall_dump_path(self.state_dir)),
                detail=(
                    "send this signal to have the daemon write every thread's "
                    "stack to the file above; no privileges and no restart"
                ),
            )
        return handle

    def _finish_shutdown_and_leave(
        self, previous_handlers: Any, *, failed: bool = False
    ) -> tuple[BaseException, ...]:
        """Discharge the last obligations, record who is still here, then go.

        Allen's ruling, 2026-08-30: **hyprial's graceful exit must not be built on
        anyone else's.** Once our own teardown is done, end the process rather
        than returning and letting the interpreter join threads belonging to
        dependencies -- `websockets` refuses daemon threads on purpose so that
        open connections are not "terminated brutally", `anyio`'s selector
        joins without a timeout from an atexit hook, and the HTTP client that
        `mcp` pulls in transitively starts two websocket threads that never set
        `daemon` at all. Only the first is a deliberate tradeoff, but the
        remedy does not depend on which: our departure stops being their
        decision.

        (That third library is named indirectly on purpose. It is retired from
        this codebase's own surface, and the retirement gate scans product
        source for its name as a plain substring -- so naming it here, even to
        explain a shutdown hazard it still creates through `mcp`, would read as
        resurrecting it. `tests/test_exit_independence.py` names it.)

        ⚠️ The exit belongs exactly here and nowhere else. Earlier drops work
        we owe. Later is the region those threads live in, and that region is
        not ours to wait in.

        🔑 `live_thread_summary` is taken **before** leaving, and it is not
        decoration. Once this lands, the backstop stops firing, every visible
        symptom of the leak disappears, and the leak itself is untouched -- we
        merely outrun it. Without this record the next reader would have no way
        to tell "fixed" from "no longer observable", and a fix that deletes the
        observation of whether the problem persists cannot report its own
        relapse. The list getting *shorter* is the success metric; the list
        staying the same means only that we are faster than it now.

        Not exiting is the default, and the steps run either way: an embedded
        daemon owes the same teardown, and only the departure is withheld.
        """

        def record_departure() -> None:
            self._log_trace(
                "info",
                "daemon.exiting",
                nodeId=self.node_id,
                owned=self._owns_process_exit,
                threads=list(live_thread_summary()),
            )

        def flush_records() -> None:
            """Last step, and it has to be last -- `os._exit` drops buffers.

            ⚠️ Added because the dependency was invisible. The departure record
            survives today only because `Logger._append` writes with a raw
            `os.write`, so nothing of ours is sitting in a Python buffer when
            the process ends. That is true, and nothing said it: the steps
            tuple read as though flushing had been considered and found
            unnecessary, when in fact it had been considered *in the docstring*
            and then not expressed here. Whoever adds a buffered writer above
            would be relying on a property this code never claimed.

            So the guarantee becomes explicit rather than inherited. The std
            streams are what a future step is most likely to reach for, and
            flushing them costs nothing on a path that is about to end.
            """

            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except BaseException:  # noqa: BLE001 - never block the exit
                    pass

        return finish_shutdown(
            steps=(
                self._home_guard.close,
                lambda: self._restore_signal_handlers(previous_handlers),
                record_departure,
                flush_records,
            ),
            exit_process=(
                self._exit_process if self._owns_process_exit else do_not_exit
            ),
            failed=failed,
        )

    def _arm_exit_backstop(self) -> None:
        """Guarantee the process dies once it has finished dying.

        Everything above has run: the lock is released, the socket is gone,
        `daemon.json` is gone. This daemon is over. But CPython will not exit
        until it has joined every non-daemon thread, and a thread parked in a
        `queue.get()` with no sentinel is never coming back -- so the process
        stays alive holding nothing, and its connector fleet, still attached to
        its open pipes, stays alive with it.

        That is not a tidy leak. Those orphans outlive the daemon that wanted
        them, keep serving actors nobody is coordinating, and stall the *next*
        daemon's start on resources they still hold -- which is how one hung
        exit turned into a bus that could not come back without killing 134
        processes by hand (2026-08-29).

        A daemon that has released its identity has no business voting on
        whether the process continues, so this stops asking. `os._exit` skips
        interpreter shutdown entirely, which is exactly the part that hangs.

        The thread is itself a daemon thread, so arming this can never be what
        keeps the process alive; and if a clean exit happens first, the timer
        dies with the interpreter and nothing is forced.

        ⚠️ This is a backstop, not the fix. Threads that cannot be joined are
        still a defect and are still worth repairing one by one. The value of
        this is that it holds for the thread nobody has written yet.
        """

        if not self._owns_process_exit:
            # Embedded in somebody else's interpreter; their exit is theirs.
            return

        def _force_exit() -> None:
            time.sleep(_EXIT_BACKSTOP_SECONDS)
            # Reached only if interpreter shutdown is still parked on a join --
            # the state nobody has managed to observe, because by the time an
            # operator looks the process is gone or the machine was restarted.
            # So the evidence is taken here, on the way out.  Without it every
            # forced exit produces the same line, saying that the process did
            # not leave and never which thread kept it: a year of those logs
            # would not narrow the search by one thread.
            try:
                threads = list(live_thread_summary())
            except BaseException:  # noqa: BLE001 - a diagnostic must not block the exit
                threads = []
            # Named in the record on purpose. A dump nobody can locate is the
            # same as no dump, and this one used to land in the daemon's
            # per-launch stderr capture -- which the restart that follows the
            # stall replaces within seconds.
            #
            # ⚠️ Resolved defensively for the same reason the inventory above
            # is: everything in this function is diagnostics running on the
            # shutdown path, and a diagnostic that raises here would stop the
            # forced exit -- turning the thing that guarantees departure into
            # one more reason to stay. That is not theoretical; an earlier
            # revision resolved this eagerly and a caller without `state_dir`
            # left the backstop silently never firing.
            try:
                dump_path = stall_dump_path(self.state_dir)
            except BaseException:  # noqa: BLE001 - see above
                dump_path = None
            self._log_trace(
                "warn",
                "daemon.exit.forced",
                afterSeconds=_EXIT_BACKSTOP_SECONDS,
                threads=threads,
                stackDump=str(dump_path) if dump_path is not None else None,
                detail=(
                    "shutdown completed but the process did not exit; forcing "
                    "so the connector fleet is not orphaned"
                ),
            )
            dump_live_threads(path=dump_path)
            sys.stderr.flush()
            os._exit(_EXIT_CODE_STUCK)

        threading.Thread(
            target=_force_exit,
            name="daemon-exit-backstop",
            daemon=True,
        ).start()

    def _migrate_logs_at_startup(self) -> None:
        result = migrate_pre_trajectory_logs(self.state_dir)
        if result is None:
            return
        self._log(
            "info",
            "daemon",
            "logs.migrated",
            fileCount=result.file_count,
        )

    def _warn_if_worker_proxy_absent(self) -> None:
        """One warning when model-vendor workers will connect directly.

        The 2026-09-25 incident was a restart from a shell with no usable
        proxy: nothing said so, and it took a 40-minute hung worker to find
        out.  This is the cheap half of noticing -- a fact about the
        configuration read at startup, with no network probe.  A damaged
        setting is reported too, because every worker launch will now fail
        on it.
        """

        from hyprial.agents.worker_proxy import (
            WORKER_PROXY_SETTINGS_KEY,
            WorkerProxyError,
            ambient_proxy_absent,
            read_worker_proxy,
        )

        try:
            setting = read_worker_proxy(self.hyprial_home)
        except WorkerProxyError as error:
            self._log(
                "warn",
                "daemon",
                "daemon.proxy.settings_invalid",
                code=error.code,
                detail=str(error),
            )
            return
        if ambient_proxy_absent(setting, os.environ):
            self._log(
                "warn",
                "daemon",
                "daemon.proxy.absent",
                detail=(
                    f"no {WORKER_PROXY_SETTINGS_KEY} setting and no "
                    "HTTP(S)_PROXY/ALL_PROXY in the daemon environment: "
                    "model-vendor workers will connect directly"
                ),
            )

    def _worker_proxy_status_json(self) -> dict[str, object]:
        """``workerProxy`` for status/ps: what the NEXT worker launch uses.

        Read at request time, like the launch itself, so it never shows a
        value the daemon cached at startup.  A damaged setting is reported,
        not raised: ``ps`` must stay answerable while it is broken.
        """

        from hyprial.agents.worker_proxy import WorkerProxyError, read_worker_proxy

        try:
            setting = read_worker_proxy(self.hyprial_home)
        except WorkerProxyError as error:
            return {"configured": False, "error": error.code, "detail": str(error)}
        if setting is None:
            return {"configured": False}
        return {"configured": True, **setting.to_json()}

    def _warn_log_migration_failure(self, error: Exception) -> None:
        detail = str(error) or type(error).__name__
        print(
            f"WARNING: pre-trajectory log migration failed: {detail}",
            file=sys.stderr,
            flush=True,
        )
        try:
            self._log(
                "warn",
                "daemon",
                "logs.migration_failed",
                errorType=type(error).__name__,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - stderr is the final warning seam
            pass

    def load_persistent_configuration(self) -> PersistentConfiguration:
        """Load the exact persistent config validated during normal startup."""

        return self.persistent_config.load()

    def configure_user_adapters(
        self, configuration: PersistentConfiguration
    ) -> tuple[str, ...]:
        """Attach receiver-owned Lark adapters from validated local config."""

        adapter_configs = {
            adapter.name: adapter for adapter in configuration.channels.gateways
        }
        instances: dict[str, LarkSdkGateway] = {}
        configured: list[str] = []
        for profile in self.user_profiles.list():
            adapter_uri = profile.squire_adapter
            if (
                adapter_uri is None
                or profile.preferred_receiver.machine != self.node_id
            ):
                continue
            parsed_adapter = parse_channel_uri(adapter_uri)
            adapter_name = parsed_adapter[2] if parsed_adapter is not None else adapter_uri
            adapter_config = adapter_configs.get(adapter_name)
            if adapter_config is None:
                continue
            adapter = instances.get(adapter_config.name)
            if adapter is None:
                secret_path = (
                    self.hyprial_home
                    / "secrets"
                    / f"{adapter_config.credential_ref}.json"
                )
                raw_secret = json.loads(secret_path.read_text(encoding="utf-8"))
                app_secret = (
                    raw_secret.get("appSecret")
                    if isinstance(raw_secret, dict)
                    else None
                )
                if not isinstance(app_secret, str) or not app_secret:
                    raise ValueError(
                        f"credential {adapter_config.credential_ref} is missing appSecret"
                    )
                adapter = self._lark_gateway_with_scope_recovery(
                    adapter_config, app_secret, self.state_dir
                )
                instances[adapter_config.name] = adapter
            self.user_adapters.register(adapter_uri, adapter)
            configured.append(adapter_uri)
        return tuple(sorted(configured))

    def _expire_previous_generation_lifecycle_state(self) -> None:
        """U0c startup sweep: cancel the dead generation's lifecycle receipts.

        Three moves, in order:

        1. BACKFILL -- the durable receipts (desired-state harness/session
           + the agents registry) are each a domain's atomic attestation
           that an effect RAN; the ones whose journal completion never
           landed (daemon died between the domain commit and the journal
           write) are journaled now, so compensation can undo them like any
           other completed effect.
        2. ROLL BACK orphans -- receipts the journal cannot account for
           (lost/legacy databases) are undone domain-locally; this is what
           collects the U0b ghost receipts on homes whose journal predates
           them.
        3. EXPIRE -- no receipt crosses the generation; the leftovers are
           retirement-handshake crash windows and are deleted.

        All of this runs BEFORE the harness actor is constructed (its
        restore-deferral set is seeded from incomplete receipts -- a dead
        generation's receipt must never hand a resource to an executor that
        no longer exists) and BEFORE the lifecycle manager boots (its
        recovery must find no cross-generation receipts and no resumable
        forward progress).  The journal is never pruned: it is the
        compensation input and the U0b rescue evidence.
        """

        claims = [
            *self.desired_state.lifecycle_effect_claims(),
            *self._agent_session_domains.lifecycle_effect_claims(),
        ]
        backfilled = (
            backfill_domain_attested_effects(self.state_db, claims)
            if claims
            else ()
        )
        rolled_back, expired = (
            self.desired_state.expire_interrupted_lifecycle_receipts()
        )
        agent_receipts = self._agent_session_domains.expire_lifecycle_receipts()
        if backfilled or rolled_back or expired or agent_receipts:
            self._log(
                "info",
                "daemon",
                "lifecycle.receipts.expired",
                backfilled=len(backfilled),
                rolledBack=rolled_back,
                desiredStateReceipts=expired,
                agentRegistryReceipts=agent_receipts,
                detail=(
                    "no lifecycle receipt crosses a daemon generation: "
                    "domain-attested effects were journaled for compensation, "
                    "orphan admissions rolled back, retirement crash windows "
                    "deleted; interrupted sagas are compensated, never resumed"
                ),
            )

    def _start_runtime(self) -> None:
        # U0c ordering: the previous generation's receipts must be gone
        # before ANY lifecycle consumer of this daemon is constructed.
        self._expire_previous_generation_lifecycle_state()
        persistent = self.load_persistent_configuration()
        self.configure_user_adapters(persistent)
        self._normalize_persisted_interactive_sessions()
        desired = self.desired_state.load()
        # Deprecated fields speak up; unknown ones stay silent.
        #
        # A field we REMOVED is different from a field we have never heard of.
        # The second is tolerated by design -- it comes from a newer version,
        # and refusing it would make every downgrade fatal with three release
        # tracks running side by side. The first must be loud: someone may
        # still be editing it, and "I changed it and nothing happened" is the
        # failure this codebase keeps reproducing in other forms.
        #
        # The registry is also what migration will read (Allen 2026-08-28), so
        # the notice and the eventual rewrite cannot drift apart.
        for _field, notice in deprecation_notices(desired.to_json()):
            self._log(
                "warn",
                "config",
                "config.field.deprecated",
                field=".".join(_field.path),
                sinceSchemaVersion=_field.since_schema_version,
                detail=notice,
            )
        listen, connect = _zenoh_endpoints(
            env_listen=self.zenoh_listen,
            env_connect=self.zenoh_connect,
            stored_listen=(),
            stored_connect=(),
        )
        # Peer discovery is additive: configured endpoints are kept in full
        # and tried first, so turning this on cannot disconnect a node that
        # works today.  That is what lets it ship without a coordinated
        # migration -- see `discovery.py` for why the interface deals in
        # endpoints rather than tailnet addresses.
        # A node that only connects out is invisible to everyone else, and
        # -- the part that made this hard to find -- it cannot tell.  It sees
        # every peer it dialed, so from the inside nothing is wrong.  So a
        # node with no listen endpoint derives its own rather than silently
        # becoming unreachable.
        if self.network_isolated:
            self._log(
                "info",
                "zenoh",
                "network.isolated",
                listen=list(listen),
                connect=list(connect),
                detail=(
                    "HYPRIAL_NETWORK_ISOLATED: loopback endpoints only; no "
                    "listen derivation, peer discovery, forwarding, gossip "
                    "or usage fetch"
                ),
            )
        if not listen and not self.network_isolated:
            derived = self._derive_listen_endpoint()
            if derived is not None:
                listen = (derived,)
                self._startup_network["listenDerived"] = True
                self._log(
                    "info",
                    "zenoh",
                    "zenoh.listen.derived",
                    endpoint=derived,
                    detail=(
                        "no listen endpoint was configured; derived this "
                        "node's tailnet address so other nodes can reach it"
                    ),
                )
        # Automatic forwarding's inbound listener is ADDED after the node's
        # default listener is derived: setting it as the listen list used
        # to suppress derivation and cost the official listener (plan §A).
        forwarding_listen: tuple[str, ...] = ()
        if self._forwarding_automatic is not None:
            forwarding_listen = (_reserve_loopback_endpoint(),)
            listen = merge_endpoints(listen, forwarding_listen)
        configured_count = len(connect)
        self._connect_configured = connect
        discovered: tuple[str, ...] = ()
        if not self.network_isolated:
            self._startup_network["discoveryConsulted"] = True
            discovered = self._discover_peer_endpoints()
        if discovered:
            connect = merge_endpoints(connect, discovered)
        # Logged whether or not anything was found, and whether or not it is
        # enabled.  Discovery is on by default, so it must be a *visible*
        # default rather than a silent behaviour change on upgrade: an
        # operator reading the startup log should be able to see that it ran,
        # what the directory returned, and what this node ended up dialing --
        # without having to know the switch exists.
        self._log(
            "info",
            "zenoh",
            "zenoh.endpoints.resolved",
            discovery=(
                "enabled"
                if zenoh_environment_flag("HYPRIAL_PEER_DISCOVERY", default=True)
                else "disabled"
            ),
            configured=configured_count,
            discovered=len(discovered),
            effective=len(connect),
        )
        # Retain the effective endpoints so `ps` reports what this daemon
        # actually uses after the desired-state fallback merge.
        self.zenoh_listen, self.zenoh_connect = listen, connect
        if not listen:
            # Distinct from the isolation warning below, which asks whether
            # this node can reach out.  Nothing previously asked whether
            # anyone can reach IN, and that is the question whose silence
            # hid member-to-member blindness: every affected node looked
            # healthy to itself.
            self._log(
                "warn",
                "zenoh",
                "zenoh.listen.absent",
                detail=(
                    "this node has no listen endpoint, so other nodes cannot "
                    "connect to it and it will not appear in their targets -- "
                    "it will still see peers it dials, so this is not "
                    "observable from here"
                ),
            )
        if not listen and not connect:
            # Fail loudly at the log level without blocking startup:
            # single-node local development without endpoints is legitimate,
            # but an endpoint-less node in a mesh is silently isolated.
            self._log(
                "warn",
                "zenoh",
                "zenoh.endpoints.unset",
                detail=(
                    "no explicit listen/connect endpoints and discovery is "
                    "disabled; this node is isolated from every other node "
                    "until endpoints are configured"
                ),
            )
        config = ZenohConfig.from_environment(
            listen=listen,
            connect=connect,
            mode="peer",
            # Off by default, and opt-in per node.  With discovery disabled a
            # node reaches exactly the endpoints it was configured with, so
            # peers sharing a hub never learn about each other: the hub sees
            # everyone and the spokes see only themselves and the hub.
            gossip_scouting=self._gossip_for_startup(),
        )
        transport, forwarding_listen = _open_transport(config, forwarding_listen)
        if forwarding_listen:
            self.zenoh_listen = transport.config.listen
        directory = LivelinessDirectory(transport)
        presence = _LocalPresence(
            directory,
            self.node_id,
            interactive_liveness=self._interactive_actor_liveness,
        )
        network_delivery = ZenohDeliveryTransport(
            transport, presence, origin_node=self.node_id
        )
        # Local-first wrapper (#101): a managed local actor is delivered by
        # calling ``inbox.receive`` directly, because one Zenoh session cannot
        # receipt its own publication.  The terminal-state semantics survive
        # unchanged on this path: ``receive`` writes the authoritative
        # ``fetched`` record (RECIPIENT_COMMIT) inside the transaction that
        # commits the message, and the holder's mirrored RECEIPT_CONFIRMED
        # upsert is a no-op against it -- so a local delivery stays exactly as
        # decidable as a remote one.
        local_delivery = LocalFirstDeliveryTransport(
            network_delivery,
            origin_node=self.node_id,
            local_node_id=self.node_id,
            local_owner=self.owner,
        )
        lark_events = CorrelatedDomainEvents()
        adapters = AdapterRuntime(
            LarkWorkerLauncher.from_environment(
                hyprial_home=self.hyprial_home,
                state_dir=self.state_dir,
                socket_path=self.socket_path,
                config_store=self.persistent_config,
            ),
            persistent.channels,
            event_sink=lark_events,
            configuration_source=lambda: self.load_persistent_configuration().channels,
            desired_state=LarkDesiredStatePort(self.desired_state),
        )
        lark_client = LarkPortClient(adapters, lark_events)

        def deliver_human_alarm(alarm: Any, text: str) -> bool:
            adapter = lark_reply_adapter(alarm.sender)
            if adapter is None:
                return False
            return lark_client.deliver_alarm(
                adapter,
                alarm.message_id,
                text,
                idempotency_key=(
                    f"alarm:{alarm.message_id}:{alarm.reason}"
                ),
            )
        # Correlated replies to channel:lark:<adapter> remain ordinary durable
        # outbox rows.  The outer transport recognizes only that exact shape
        # and asks the supervised worker to run its native-reply path; every
        # other target keeps the local-first/network behavior unchanged.
        delivery = LarkReplyBridgeTransport(local_delivery, lark_client)
        inbox_database = self.state_dir / "inbox.sqlite3"
        shared_inbox_events = CorrelatedInboxEventRouter()
        inbox_coordinator = DeliveryCustodyCoordinator(
            inbox_database,
            delivery,
            shared_inbox_events,
            node_id=self.node_id,
            service_options={
                "hold_policy": HoldPolicy.from_environment(),
                "logger": self._logger,
                "alarm_human_delivery": deliver_human_alarm,
                # Resolve the actor projection live; session mutations no
                # longer require rebuilding delivery policy.
                "interactive_recipient": lambda recipient: recipient
                in self._registered_interactive_actors(),
            },
        )
        inbox = DeliveryCustodyFacade(inbox_coordinator, inbox_database)
        local_delivery.bind_receiver(inbox.receive)
        # PAC owns this outbound path independently of WorkflowService.  The
        # v1 service may share the same typed inbox authority while it exists,
        # but PAC notification and actor startup have no service precondition.
        shared_inbox_io = InboxDeliveryIoAdapter(
            inbox_coordinator,
            shared_inbox_events,
            resolve_target=lambda name: self._resolve_agent_alias(
                normalize_agent_recipient(name)
            ),
            deliver_user=self._deliver_report_to_user,
        )
        self._pac_notification_io = shared_inbox_io
        # Deferred import: hyprial.harnesses eagerly imports .agent_sdk, which
        # imports hyprial.daemon.api; hyprial.daemon eagerly imports this module, so a
        # module-level import here would re-enter while hyprial.harnesses is still
        # partially initialized (import cycle introduced by merging the Agent
        # SDK driver with the application lifecycle). Both sides stay intact;
        # by call time every module is fully loaded.
        from hyprial.harnesses import HarnessLauncher
        from hyprial.harnesses.worker_channel import WorkerChannel, build_worker_channel

        def worker_channel(spec: HarnessLaunchSpec) -> WorkerChannel:
            # Mint the worker's own canonical identity from the SAME source the
            # actor registrar advertises (self._canonical_harness_uri), so the
            # worker's MCP inbox key never drifts from where deliveries land.
            actor = self._canonical_harness_uri(spec.name, spec)
            session_ref = uuid4().hex
            from hyprial.agents.runtime import (
                DEFAULT_AGENT_TOOL_PROFILE,
                resolve_agent_runtime_context,
            )

            runtime_context = resolve_agent_runtime_context(
                registry=self._agent_registry,
                agent_name=spec.name,
                harness=spec.harness,
                cwd=spec.cwd,
                tool_profile=DEFAULT_AGENT_TOOL_PROFILE,
                containerized=spec.containerized,
            )
            return build_worker_channel(
                actor=actor,
                session_ref=session_ref,
                node_id=self.node_id,
                owner=self.owner,
                hyprial_home=self.hyprial_home,
                state_dir=self.state_dir,
                # A containerized worker spawns its MCP channel INSIDE the
                # container: the interpreter must be the image's python,
                # not this daemon's host path.
                python_executable=(
                    _CONTAINER_PYTHON if spec.containerized else None
                ),
                runtime_context=runtime_context,
            )

        def child_environment(
            spec: HarnessLaunchSpec, channel: WorkerChannel
        ) -> "object | None":
            """P1b B1: the pi worker's complete replacement environment.

            The composition itself is the shared, tested implementation in
            :func:`compose_daemon_worker_launch` (which adds the per-launch
            ``workerProxy`` route); this closure binds it to THIS daemon's
            authority (registry, home, channel, environment).
            """

            return compose_daemon_worker_launch(
                registry=self._agent_registry,
                hyprial_home=self.hyprial_home,
                spec=spec,
                channel=channel,
                environ=os.environ,
            )

        harness_events = CorrelatedDomainEvents()
        provider_auth = self._build_provider_auth_coordinator()
        self._provider_auth = provider_auth
        harness_actor = HarnessRuntimeActor(
            HarnessLauncher(
                worker_channel_factory=worker_channel,
                child_environment_factory=child_environment,
                state_dir=self.state_dir,
                turn_failure_observer=(
                    provider_auth.handle_turn_failure
                    if provider_auth is not None
                    else None
                ),
            ),
            # A dead child is first exposed as stopped/error and its actor
            # registration is withdrawn.  Desired-state recovery starts a
            # replacement on a later one-second maintenance pass instead of
            # hiding the death behind a same-tick replacement.
            restart_backoff_seconds=1.0,
            orphan_state_path=self.state_dir / "orphan-processes.json",
            orphan_logger=self._log,
            event_sink=harness_events,
            desired_state=self.desired_state,
        )
        harnesses = HarnessPortClient(harness_actor, harness_events)
        # `as_mailbox` used to declare the liveliness token and nothing else:
        # senders saw a mailbox and published custody to it, but no node ever
        # subscribed to `custody/<node>`, so the custody receipt never came
        # back, `_attempt_custody` reported failure, and the sender silently
        # kept the message.  A mailbox that advertises itself must be able to
        # take delivery, or the advertisement is a lie.
        endpoint = ZenohInboxEndpoint(
            transport,
            inbox,
            self.node_id,
            mailbox_node=self.node_id if desired.as_mailbox else None,
            notice_node=self.node_id,
            # Route C: the node-level endpoint is also the ONLY progress
            # subscriber.  The actor endpoints below (and interactive-route
            # endpoints) deliberately do not pass progress_node.
            progress_node=self.node_id,
            consume_fetch_receipts=True,
        )
        # One status queryable serves the whole node: terminal records are
        # keyed by sender, not by recipient, and any node may hold the outcome
        # of any sender's message.  This is what makes a terminal state
        # pullable after the fact -- the sender is routinely offline at the
        # moment its message ends, so there is nobody to notify.
        # The named empty response is load-bearing: it distinguishes a node
        # that answered with no record from a node that did not answer.
        status_endpoint = DeliveryStatusEndpoint(
            transport,
            inbox.delivery_status,
            holder=self.node_id,
            observer=self._log_status_query,
        )
        user_receiver = ReceiverUserDelivery(
            node_id=self.node_id,
            profiles=self.user_profiles,
            adapters=self.user_adapters,
            ledger=UserDeliveryLedger(self.state_dir / "user-deliveries.json"),
            reload_adapters=self._reload_user_adapters,
        )
        user_endpoint = ZenohUserDeliveryEndpoint(transport, user_receiver)
        user_delivery = ZenohUserDeliveryTransport(
            transport,
            logger=self._logger.bind(component="user-delivery"),
        )
        org_endpoint = OrgContextMesh(
            transport,
            OrgContextStore(self.hyprial_home),
            self.node_id,
            logger=lambda level, event, **fields: self._log(
                level, "org", event, **fields
            ),
        )

        runtime = DaemonEventBridge(
            state_dir=self.state_dir,
            node_id=self.node_id,
            desired_state=self.desired_state,
            transport=transport,
            inbox=inbox,
            harnesses=harnesses,
            presence=presence,
            delivery=delivery,
            # LifecycleProcessManager + RouteRegistrationIo are the sole
            # owner of harness routes.  Keeping the legacy runtime registrar
            # here would recreate a second route/liveliness authority.
            harness_actor_registrar=None,
            harness_actor_uri=self._canonical_harness_uri,
            logger=lambda level, component, event, **fields: self._log(
                level, component, event, **fields
            ),
            workflow_outcome=lambda result: (
                self._record_workflow_outcome(result)
                if self._workflow_service is not None and result.delivery_id.startswith("workflow-") else False
            ),
            usage_limit_observer=(
                self._on_usage_limit_failure
                if self._quota_watchdog is not None
                else None
            ),
            forwarder=self._forward_as_actor,
            owner_notifier=self._owner_alert_notifier,
        )
        duplicate_watch: DuplicateInstanceWatch | None = None
        try:
            recovery = runtime.start()
            actor_token = transport.declare_liveliness(
                KeySpace().actor_liveliness(self.node_id)
            )
            # The watch's own constructor closes its token if observing
            # fails, so a raised construction leaves nothing behind.
            duplicate_watch = DuplicateInstanceWatch(
                transport,
                self.node_id,
                self._home_guard.generation,
                logger=lambda level, event, **fields: self._log(
                    level, "daemon", event, **fields
                ),
            )
        except BaseException as startup_error:
            cleanup_errors: list[BaseException] = []
            for cleanup in (
                harnesses.stop,
                adapters.stop,
                lark_events.close,
                org_endpoint.close,
                user_endpoint.close,
                status_endpoint.close,
                endpoint.close,
                directory.close,
                inbox.close,
                *(() if duplicate_watch is None else (duplicate_watch.close,)),
                transport.close,
            ):
                try:
                    cleanup()
                except BaseException as error:
                    cleanup_errors.append(error)
            if cleanup_errors:
                raise BaseExceptionGroup(
                    "daemon startup and composition cleanup failed",
                    [startup_error, *cleanup_errors],
                ) from startup_error
            raise
        self._transport = transport
        if self._forwarding_automatic is not None and forwarding_listen:
            # Only now is the inbound port certainly this daemon's: the
            # session bound it.  The first dial rides the reconciler's redial
            # on the next maintenance tick (~1 s).
            target = forwarding_listen[0].removeprefix("tcp/")
            self._forwarding_environment = self._forwarding_automatic.environment(
                target
            )
            self._start_forwarding_supervisor()
        self._directory = directory
        self._presence = presence
        self._inbox = inbox
        self._inbox_coordinator = inbox_coordinator
        self._harnesses = harnesses
        self._adapters = adapters
        self._lark_events = lark_events
        self._lark_client = lark_client
        self._endpoint = endpoint
        self._status_endpoint = status_endpoint
        self._user_endpoint = user_endpoint
        self._user_delivery = user_delivery
        self._org_endpoint = org_endpoint
        self._actor_token = actor_token
        self._duplicate_watch = duplicate_watch
        self._runtime = runtime
        self._start_lifecycle_manager(
            transport=transport,
            inbox=inbox,
            local_delivery=local_delivery,
            harnesses=harnesses,
        )
        from .pac_actor import (
            DaemonActorRuntime,
            DaemonPacNotificationSender,
            PacActorService,
        )

        # U0BRACE: nothing else may live here.  This used to be the call site
        # of ``_bootstrap_lifecycle_harnesses``, which submitted one create
        # saga per non-Lark running row -- the same selection predicate
        # ``_restore_harnesses`` uses, so every such row had TWO owners and
        # only timing decided which one started the process (the loser
        # either deferred or, worse, reset the winner's record and started
        # a second process).  Restore owns those rows now;
        # ``tests/test_daemon_restart_ownership.py`` pins that the startup
        # window submits no lifecycle operations at all.
        from hyprial.pac.legacy_workflows import cutover

        workflow_cutover_ok = False
        try:
            self._legacy_workflow_cutover = cutover(self.state_dir)
        except Exception as error:  # noqa: BLE001 - autoupdate must remain reachable
            self._legacy_workflow_cutover = None
            self._log(
                "error",
                "workflow",
                "workflow.cutover_failed",
                phase="cutover",
                exceptionClass=type(error).__name__,
                detail=str(error),
                databasePath=str(self.state_dir / "workflows.sqlite3"),
                sealed=False,
                disabledCapabilities=(
                    "workflow.dispatch",
                    "workflow.mutation",
                    "workflow.recovery",
                    "workflow.remote",
                    "routine.dispatch",
                    "pac.actor.cadence",
                ),
            )
        else:
            workflow_cutover_ok = True
            self._log(
                "info",
                "workflow",
                "workflow.cutover_applied",
                present=self._legacy_workflow_cutover["present"],
                at=self._legacy_workflow_cutover["at"],
                sealed=self._legacy_workflow_cutover["sealed"],
                terminated=self._legacy_workflow_cutover["terminated"],
            )
            for cancelled in self._legacy_workflow_cutover["cancelled"]:
                self._log(
                    "warn",
                    "workflow",
                    "workflow.cutover_cancelled",
                    runId=cancelled["runId"],
                    sender=cancelled["sender"],
                    table=cancelled["table"],
                    priorState=cancelled["priorState"],
                    reason=cancelled["reason"],
                    at=self._legacy_workflow_cutover["at"],
                )

        workflow_disabled = (
            "workflow.dispatch",
            "workflow.mutation",
            "workflow.recovery",
            "routine.dispatch",
            "pac.actor.cadence",
        )

        def suspend_pac_actor() -> None:
            actor = self._pac_actor_service
            self._pac_actor_service = None
            if actor is None:
                return
            try:
                if actor.close(5.0) is False:
                    raise RuntimeError("PAC actor service did not drain")
            except Exception as error:  # noqa: BLE001 - preserve startup
                self._log(
                    "error",
                    "pac",
                    "workflow.degrade_cleanup_failed",
                    phase="pac-actor",
                    exceptionClass=type(error).__name__,
                    detail=str(error),
                )
                self._degraded_workflow_handles.append(actor)

        def degrade_workflow_components() -> None:
            suspend_pac_actor()
            remote = self._remote_workflow
            self._remote_workflow = None
            if remote is not None:
                try:
                    remote.close()
                except Exception as error:  # noqa: BLE001 - preserve startup
                    self._log(
                        "error",
                        "pac",
                        "workflow.degrade_cleanup_failed",
                        phase="remote",
                        exceptionClass=type(error).__name__,
                        detail=str(error),
                    )
                    self._degraded_workflow_handles.append(remote)
            workflow_service = self._workflow_service
            self._workflow_service = None
            if workflow_service is not None:
                try:
                    if workflow_service.close() is False:
                        raise RuntimeError("workflow service did not drain")
                except Exception as error:  # noqa: BLE001 - preserve startup
                    self._log(
                        "error",
                        "pac",
                        "workflow.degrade_cleanup_failed",
                        phase="workflow",
                        exceptionClass=type(error).__name__,
                        detail=str(error),
                    )
                    self._degraded_workflow_handles.append(workflow_service)

        from hyprial.dispatch.remote_workflow import RemoteWorkflow
        dispatch_alarm = DispatchAlarm(inbox.alarm_emitter, self._workflow_deliver_user, self._logger)
        if workflow_cutover_ok:
            try:
                self._pac_actor_service = PacActorService(
                    state_dir=self.state_dir,
                    reference_root=self.hyprial_home,
                    runtime=DaemonActorRuntime(self),
                    sender=DaemonPacNotificationSender(self),
                    daemon_epoch=self.epoch,
                    logger=self._log,
                )
            except Exception as error:  # noqa: BLE001 - cadence is optional
                self._pac_actor_service = None
                self._log(
                    "error",
                    "pac",
                    "workflow.pac_actor_unavailable",
                    phase="pac-actor",
                    exceptionClass=type(error).__name__,
                    detail=str(error),
                    disabledCapabilities=workflow_disabled,
                )
        if workflow_cutover_ok:
            try:
                self._workflow_service = GraphWorkflowService(
                    state_dir=self.state_dir, owner=self.owner, machine=self.node_id,
                    sender=DaemonPacNotificationSender(self),
                    admit=self._workflow_admit, logger=self._log,
                )
            except Exception as error:  # noqa: BLE001 - autoupdate must remain reachable
                self._log(
                    "error",
                    "pac",
                    "workflow.recovery_unavailable",
                    phase="construct",
                    exceptionClass=type(error).__name__,
                    detail=str(error),
                    disabledCapabilities=workflow_disabled,
                )
                suspend_pac_actor()
            else:
                try:
                    self._remote_workflow = RemoteWorkflow(self, transport)
                except Exception as error:  # noqa: BLE001 - remote is optional
                    self._remote_workflow = None
                    self._log(
                        "error",
                        "pac",
                        "workflow.remote_unavailable",
                        phase="remote-construction",
                        exceptionClass=type(error).__name__,
                        detail=str(error),
                        disabledCapabilities=(
                            "workflow.remote.admission",
                            "workflow.remote.completion",
                            "workflow.remote.returns",
                            "routine.remote.dispatch",
                        ),
                    )
                try:
                    adopted = self._workflow_service.recover()
                except Exception as error:  # noqa: BLE001 - autoupdate must remain reachable
                    self._log(
                        "error",
                        "pac",
                        "workflow.recovery_unavailable",
                        phase="recover",
                        exceptionClass=type(error).__name__,
                        detail=str(error),
                        disabledCapabilities=workflow_disabled,
                    )
                    degrade_workflow_components()
                else:
                    if adopted:
                        self._log("info", "daemon", "workflow.recovered", runs=adopted)

        # Self-drive routines (design-selfdrive-routine): deterministic duty
        # cycles producing one PAC graph per task (U3).  The old dispatcher is
        # no longer in this path; the alarm sink still comes from it, which is
        # U2 residue the retirement removes in U6/U7.
        adopted_routines = 0
        if self._workflow_service is not None:
            try:
                self._routine_service = RoutineService(
                    pac=PacRoutineDispatch(
                        state_dir=self.state_dir,
                        workflow=self._workflow_service,
                        deliver=self._deliver_routine_task,
                        clock_ms=lambda: time.time_ns() // 1_000_000,
                        resolve_principal=self._resolve_routine_principal,
                    ),
                    alarm=dispatch_alarm,
                    state_dir=self.state_dir,
                    # Stored bare-name addresses migrate ONLY via this machine's
                    # agents registry (approved plan Q1): a unique roster match
                    # rewrites the stored spec; anything else quarantines loudly.
                    migrate_address=self._migrate_stored_routine_address,
                    logger=self._logger,
                )
                adopted_routines = self._routine_service.recover()
                self._reconcile_routine_coordinators()
            except Exception as error:  # noqa: BLE001 - routine is optional
                routine = self._routine_service
                self._routine_service = None
                if routine is not None:
                    try:
                        routine.close()
                    except Exception as cleanup_error:  # noqa: BLE001
                        self._log(
                            "error",
                            "pac",
                            "workflow.degrade_cleanup_failed",
                            phase="routine",
                            exceptionClass=type(cleanup_error).__name__,
                            detail=str(cleanup_error),
                        )
                self._log(
                    "error",
                    "pac",
                    "routine.recovery_unavailable",
                    phase="routine",
                    exceptionClass=type(error).__name__,
                    detail=str(error),
                    disabledCapabilities=("routine.dispatch",),
                )
        if adopted_routines:
            self._log("info", "daemon", "routine.recovered", routines=adopted_routines)
        # U3 deleted the retired dispatcher's rows when the store opened.  The
        # ruling was 「迁移时直接删除」, and a deletion whose only trace is an
        # absence cannot be checked afterwards -- so name the rows here, once,
        # at the startup that dropped them.
        dropped = (
            self._routine_service.migrated_u3
            if self._routine_service is not None
            else {"effects": (), "inFlight": ()}
        )
        if dropped["effects"] or dropped["inFlight"]:
            self._log(
                "info",
                "routine",
                "routine.migrated_u3",
                effects=list(dropped["effects"]),
                inFlight=list(dropped["inFlight"]),
            )
        # Every registered agent gets its daemon-backed network presence:
        # remote senders see a deliverable target and deliveries land in
        # this node's durable inbox even with no connector running.
        self._restore_persona_routes()
        try:
            try:
                org_endpoint.publish_accepted()
            except Exception as error:  # noqa: BLE001 - startup stays available
                self._log(
                    "warn",
                    "org",
                    "org.context.publish_failed",
                    detail=str(error),
                )
            self._clean_dead_interactive_sessions()
            self._restore_interactive_routes()
            # Bindings are per daemon generation; re-derive them from desired
            # state so the A1 uniqueness gate survives a restart.
            self._seed_agent_bindings()
            # The registry imported any pre-sqlite agents/*.json records at
            # construction; the registry itself has no logging seam, so the
            # import is reported here where it can be seen.
            if self.agents.imported_legacy:
                self._log(
                    "info",
                    "agents",
                    "agent.registry.imported",
                    files=list(self.agents.imported_legacy),
                    detail=(
                        "pre-sqlite agent records imported into "
                        "agents.sqlite3; originals kept as *.json.imported"
                    ),
                )
            # Adapter pins moved into the agents database; drain any legacy
            # desired-state entries before adapters (their workers query the
            # daemon's pin index) come up.
            self._migrate_legacy_channel_pins()
        except BaseException:
            self._close()
            raise
        self._log(
            "info",
            "daemon",
            "service.recovery.completed",
            # attempted/restored/failed are deliberately absent: the event
            # bridge stopped restoring harnesses, so its summary reports 0 for
            # all three regardless of what was declared. Logging them here
            # described a recovery that had not happened, in a success shape
            # -- failed=0 reads as "nothing went wrong" when the truth was
            # "nothing ran". The real counts are in harness.recovery.completed
            # and adapter.recovery.completed, emitted where the work occurs.
            previousRunUnclean=recovery.previous_run_unclean,
        )

    def _reload_user_adapters(self) -> None:
        """Refresh receiver-owned adapters after setup changed local config."""

        self.configure_user_adapters(self.load_persistent_configuration())

    def _start_lifecycle_manager(
        self,
        *,
        transport: ZenohTransport,
        inbox: DeliveryCustodyFacade,
        local_delivery: LocalFirstDeliveryTransport,
        harnesses: HarnessPortClient,
    ) -> None:
        """Compose the durable cross-domain lifecycle control plane."""

        router = CorrelationEventRouter()

        agent_port = AtomicLifecycleDomainPort(
            domain="agent",
            router=router,
            generation=lambda: self._agent_session_domains.agent.generation,
            version=lambda: self._agent_session_domains.agent.version,
            submit_domain=self._agent_session_domains.agent.submit,
            wait_domain=self._agent_session_domains.wait_agent_lifecycle,
            retire_receipt=(
                self._agent_session_domains.retire_agent_lifecycle_receipt
            ),
            confirm_receipt_retired=(
                self._agent_session_domains.confirm_agent_lifecycle_receipt
            ),
        )
        session_port = AtomicLifecycleDomainPort(
            domain="session",
            router=router,
            generation=lambda: self._agent_session_domains.session.generation,
            version=lambda: self._agent_session_domains.session.version,
            submit_domain=self._agent_session_domains.session.submit,
            wait_domain=self._agent_session_domains.wait_session_lifecycle,
            retire_receipt=lambda attempt, token: (
                self.desired_state.retire_lifecycle_receipt(
                    "session", attempt, token
                )
            ),
            confirm_receipt_retired=lambda attempt, token: (
                self.desired_state.confirm_lifecycle_receipt_retired(
                    "session", attempt, token
                )
            ),
        )
        harness_port = HarnessLifecycleDomainPort(
            failure_control=harnesses.fail_lifecycle,
            domain="harness",
            router=router,
            generation=lambda: harnesses.generation,
            version=lambda: harnesses.version,
            submit_domain=harnesses.submit_lifecycle,
            wait_domain=harnesses.wait_lifecycle,
            retire_receipt=lambda attempt, token: (
                self.desired_state.retire_lifecycle_receipt(
                    "harness", attempt, token
                )
            ),
            confirm_receipt_retired=lambda attempt, token: (
                self.desired_state.confirm_lifecycle_receipt_retired(
                    "harness", attempt, token
                )
            ),
            release_replay_claim=harnesses.actor.release_lifecycle_replay,
        )

        def register_route(spec: RouteSpec) -> tuple[Any, Any]:
            local = local_delivery.register_actor(spec.route_id)
            try:
                network = (
                    transport.declare_liveliness(spec.liveliness_key)
                    if spec.advertise
                    else None
                )
                combined = _HarnessActorRegistration(
                    local,
                    network,
                    actor_uri=spec.route_id,
                    layer="lifecycle-route",
                    event_sink=lambda level, event, **fields: self._log(
                        level, "daemon", event, **fields
                    ),
                )
                try:
                    endpoint = ZenohInboxEndpoint(
                        transport,
                        inbox,
                        spec.route_id,
                        declare_receipts=False,
                    )
                except BaseException:
                    combined.close(
                        reason="route-endpoint-start-failed",
                        initiator="lifecycle-manager",
                    )
                    raise
                return combined, endpoint
            except BaseException:
                local.close()
                raise

        route_port = RouteRegistrationIo(
            transport,
            lambda _route_id: lambda _selector: b"ok",
            router,
            registration_factory=register_route,
        )
        manager = LifecycleProcessManager(
            self.state_db,
            LifecyclePorts(agent_port, session_port, harness_port, route_port),
            router,
            event_sink=self._lifecycle_thread_event,
        )
        self._lifecycle_router = router
        self._lifecycle_domain_ports = (agent_port, session_port, harness_port)
        self._route_registration = route_port
        self._routes = RouteRegistrationClient(route_port, router)
        self._lifecycle_manager = manager

    def _lifecycle_spec(self, spec: HarnessLaunchSpec) -> LifecycleSpec:
        actor = self._canonical_harness_uri(spec.name, spec)
        keys = KeySpace()
        return LifecycleSpec(
            agent_name=spec.name,
            actor=actor,
            harness=harness_launch_projection(spec),
            route=RouteSpec(
                route_id=actor,
                liveliness_key=keys.actor_liveliness(actor),
                inbox_key=keys.inbox_all(actor),
            ),
        )

    def _run_lifecycle_operation(
        self,
        operation: LifecycleOperation,
        *,
        # Outlast the manager's operation deadline so this wait receives the
        # terminal FAILED it writes, rather than timing out first (card 104164aa
        # (c)).
        timeout: float = (
            LIFECYCLE_OPERATION_DEADLINE_SECONDS + LIFECYCLE_WAIT_MARGIN_SECONDS
        ),
    ) -> Any:
        manager = self._lifecycle_manager
        if manager is None:
            raise DaemonRequestError(
                ipc_errors.LIFECYCLE_MANAGER_UNAVAILABLE,
                "lifecycle manager is not running",
            )
        # Instrumentation for a failure that is otherwise unobservable.  The
        # caller's error carries an operation id but no elapsed and no budget,
        # so an operator reading logs/daemon.jsonl after the fact cannot tell a
        # 3-second refusal from a deadline that expired at 80 seconds
        # (2026-09-22: neither the settle timeout nor the admission refusal
        # wrote anything at all).
        operation_id = operation.operation_id
        kind = operation.kind.value
        budget_ms = int(timeout * 1000)
        started = time.monotonic()
        self._log_lifecycle_operation(
            "info",
            "daemon.lifecycle.operation.submitted",
            operationId=operation_id,
            kind=kind,
            budgetMs=budget_ms,
        )
        admission = manager.submit(operation)
        if admission is not PortAdmission.ACCEPTED:
            self._log_lifecycle_operation(
                "error",
                "daemon.lifecycle.operation.refused",
                operationId=operation_id,
                kind=kind,
                admission=admission.value,
                elapsedMs=int((time.monotonic() - started) * 1000),
                budgetMs=budget_ms,
            )
            raise DaemonRequestError(
                (
                    ipc_errors.LIFECYCLE_MANAGER_UNAVAILABLE
                    if manager.crashed
                    else ipc_errors.DAEMON_START_FAILED
                ),
                f"lifecycle operation admission: {admission.value}",
            )
        try:
            result = manager.wait(operation_id, timeout)
        except TimeoutError as error:
            self._log_lifecycle_operation(
                "error",
                "daemon.lifecycle.operation.unsettled",
                operationId=operation_id,
                kind=kind,
                elapsedMs=int((time.monotonic() - started) * 1000),
                budgetMs=budget_ms,
            )
            raise DaemonRequestError(
                ipc_errors.LIFECYCLE_OPERATION_UNSETTLED,
                f"lifecycle operation did not settle: {operation_id}",
            ) from error
        if result.state is not LifecycleState.COMPLETED:
            self._log_lifecycle_operation(
                "error",
                "daemon.lifecycle.operation.failed",
                operationId=operation_id,
                kind=kind,
                state=result.state.value,
                errorCode=result.error_code,
                elapsedMs=int((time.monotonic() - started) * 1000),
                budgetMs=budget_ms,
            )
            raise DaemonRequestError(
                result.error_code or ipc_errors.DAEMON_START_FAILED,
                result.error
                or f"lifecycle operation ended in {result.state.value}",
            )
        self._log_lifecycle_operation(
            "info",
            "daemon.lifecycle.operation.settled",
            operationId=operation_id,
            kind=kind,
            elapsedMs=int((time.monotonic() - started) * 1000),
        )
        if operation.kind in {LifecycleKind.CREATE, LifecycleKind.TRANSFER}:
            launch = operation.target.harness
            if launch.harness != "lark":
                self._ensure_agent(
                    operation.target.actor,
                    harness=launch.harness,
                    interactive=operation.target.session is not None,
                    cwd=launch.cwd,
                    args=launch.args,
                    provider=launch.model_provider,
                    model=launch.model,
                )
        return result

    def _lifecycle_thread_event(self, event: str, **fields: Any) -> None:
        """Surface lifecycle consumer-thread faults on the daemon event log.

        Both names are runtime-thread events, not startup failures.  They
        sit behind the event-sink seam (a method reference, not a call), so
        the startup-event scanner never classifies them; if this ever moves
        onto a directly-called ``self._log`` site on the startup path, add
        the two names to _STARTUP_EVENT_EXCLUSIONS with that reason.  State
        (_crashed/_last_error, visible via ps) is written by the manager
        before this sink is called, so a logging failure here cannot hide
        the fault.
        """

        if event == "thread_exited":
            self._log("error", "daemon", "daemon.lifecycle.thread_exited", **fields)
        elif event == "thread_recovered":
            self._log("info", "daemon", "daemon.lifecycle.thread_recovered", **fields)
        else:
            self._log("error", "daemon", "daemon.lifecycle.thread_error", **fields)

    def _log_lifecycle_operation(self, level: str, event: str, **fields: Any) -> None:
        """Best-effort instrumentation for one lifecycle operation's run.

        The calls below sit on the failure paths of ``_run_lifecycle_operation``
        (a refused admission, a deadline that expired, a terminal non-COMPLETED
        state).  A log sink that raised here would replace the operation's own
        error with a logging error -- the same shape
        ``_mirror_startup_event_to_stderr`` guards against, for the same reason:
        this line exists to describe a failure, so losing it must never be worse
        than the failure it describes.
        """

        try:
            self._log(level, "daemon", event, **fields)
        except Exception:  # noqa: BLE001 - must not mask the failure it describes
            pass

    def _lifecycle_status(self) -> JsonObject:
        """The ps-visible health of the lifecycle consumer thread."""

        manager = self._lifecycle_manager
        if manager is None:
            return {"running": False, "crashed": False, "lastError": None}
        return {
            "running": manager.is_running,
            "crashed": manager.crashed,
            "lastError": manager.last_error,
        }

    def _route_lark_gateway(self, gateway_config: Any) -> LarkSdkGateway:
        """Return a cached SDK facade for one configured Lark adapter."""

        existing = self._route_gateway_cache.get(gateway_config.name)
        if existing is not None:
            return existing
        secret_path = (
            self.hyprial_home / "secrets" / f"{gateway_config.credential_ref}.json"
        )
        raw_secret = json.loads(secret_path.read_text(encoding="utf-8"))
        app_secret = (
            raw_secret.get("appSecret") if isinstance(raw_secret, dict) else None
        )
        if not isinstance(app_secret, str) or not app_secret:
            raise DaemonRequestError(
                ipc_errors.ROUTE_ADAPTER_UNCONFIGURED,
                f"credential {gateway_config.credential_ref} is missing appSecret",
                {"adapter": gateway_config.name},
            )
        gateway = self._lark_gateway_with_scope_recovery(
            gateway_config, app_secret, self.state_dir
        )
        return self._route_gateway_cache.put(gateway_config.name, gateway)

    @staticmethod
    def _lark_gateway_with_scope_recovery(
        gateway_config: Any, app_secret: str, state_dir: Path
    ) -> LarkSdkGateway:
        """Build a gateway whose permission notification cannot recurse."""

        # Send outcomes (code + msg + native message id) belong on the
        # adapter log so a route send answers "what did Feishu return for
        # this om_" after the fact, in the same file the worker writes.
        gateway_name = getattr(gateway_config, "name", None)
        gateway_logger = (
            Logger.adapter(state_dir, name=gateway_name)
            if isinstance(gateway_name, str) and gateway_name
            else None
        )
        gateway = LarkSdkGateway.from_credentials(gateway_config.app_id, app_secret)
        configure_logger = getattr(gateway, "set_logger", None)
        if callable(configure_logger):
            configure_logger(gateway_logger, gateway_name=gateway_name)
        default_route = next(
            (
                item
                for item in gateway_config.routes
                if item.name == gateway_config.default_route
            ),
            None,
        )
        notification_routes = (
            (default_route,)
            if default_route is not None and default_route.type == "direct"
            else tuple(
                item
                for item in gateway_config.routes
                if default_route is not None
                and default_route.type == "fanout"
                and item.name in default_route.members
                and item.type == "direct"
            )
        )
        notification_gateway = (
            LarkSdkGateway.from_credentials(gateway_config.app_id, app_secret)
            if notification_routes
            else None
        )
        if notification_gateway is not None:
            configure_logger = getattr(notification_gateway, "set_logger", None)
            if callable(configure_logger):
                configure_logger(gateway_logger, gateway_name=gateway_name)

        def notify(text: str) -> None:
            if notification_gateway is None:
                return
            digest = hashlib.sha256(text.encode()).hexdigest()[:16]
            for route in notification_routes:
                if route.native_id is not None:
                    notification_gateway.send_chat(
                        route.native_id,
                        text,
                        idempotency_key=(f"scope-authorization:{route.name}:{digest}"),
                    )

        client = LarkScopeClient(gateway_config.app_id, app_secret)
        recovery = LarkScopeRecovery(
            app_id=gateway_config.app_id,
            apply=client.apply_scopes,
            notify=notify,
            throttle=LarkScopeThrottleStore(
                state_dir / "adapters" / "lark" / "scope-authorization.json"
            ),
        )
        configure = getattr(gateway, "set_permission_recovery", None)
        if callable(configure):
            configure(recovery.handle)
        return gateway

    def _workflow_deliver_user(self, recipient: str, text: str, message_id: str) -> bool:
        """Escalation delivery to a ``user:<owner>`` target via the Squire
        user-delivery path.

        True when accepted.  A transient timeout (receipt never arrived) is
        raised as a non-permanent ``InboxIoError`` so the report path can
        retry instead of permanently failing; unconfigured / rejected returns
        False (permanent)."""

        if self._user_delivery is None:
            return False
        try:
            user_target = UserDeliveryTarget.parse(recipient)
        except ValueError:
            return False
        outcome = self._user_delivery.deliver(
            UserDeliveryRequest(
                message_id=message_id,
                idempotency_key=f"workflow-escalate:{message_id}",
                owner=user_target.owner,
                sender="workflow",
                message=text,
                conversation_id="workflow",
            )
        )
        if outcome.accepted:
            return True
        if outcome.code == ipc_errors.USER_DELIVERY_TIMEOUT:
            raise InboxIoError(
                f"owner-DM delivery for {recipient} timed out waiting for "
                "the receiver's squire receipt",
                permanent=False,
            )
        return False

    def _deliver_report_to_user(self, recipient: str, text: str, message_id: str) -> bool:
        """Delivery-layer ``user:<owner>`` split for run/PAC reports.

        Wired into ``InboxDeliveryIoAdapter`` so every report consumer — run
        reports, PAC notifications, legacy workflow effects — shares one
        squire DM path instead of writing inbox messages no transport
        consumes (2026-09-14 defect class B). Returns False when the owner
        DM route is unavailable; the adapter then fails the delivery loudly
        without queueing a doomed retry cycle.
        """

        return self._workflow_deliver_user(recipient, text, message_id)

    def _migrate_stored_routine_address(self, name: str) -> str | None:
        """Resolve one bare actor name against THIS machine's agents registry.

        Migration rule (approved Q1): rewrite a stored bare-name address only
        when the local agents roster answers with exactly one agent; no
        profile inference, no presence scan, no guessing. Unknown or
        ambiguous names return None and the routine quarantines loudly
        instead of being silently resumed or rewritten.
        """

        if ":" in name or not name.strip():
            return None
        try:
            agent = self.agents.get(name.strip())
        except Exception:
            return None
        if agent is None:
            return None
        candidate = agent.uri
        return candidate if parse_agent_uri(candidate) is not None else None

    def _quota_watchdog_deliver(self, idempotency_key: str, text: str) -> bool:
        """Watchdog alerts go to this daemon's owner through Squire."""

        if self._user_delivery is None:
            return False
        outcome = self._user_delivery.deliver(
            UserDeliveryRequest(
                message_id=f"quota-watchdog-{uuid4().hex[:12]}",
                idempotency_key=idempotency_key,
                owner=self.owner,
                sender="quota-watchdog",
                message=text,
                conversation_id="quota-watchdog",
            )
        )
        return outcome.accepted

    def _inbox_watchdog_deliver(self, idempotency_key: str, text: str) -> bool:
        """Mail-collection alerts go to this daemon's owner through Squire."""

        if self._user_delivery is None:
            return False
        outcome = self._user_delivery.deliver(
            UserDeliveryRequest(
                message_id=f"inbox-watchdog-{uuid4().hex[:12]}",
                idempotency_key=idempotency_key,
                owner=self.owner,
                sender="inbox-watchdog",
                message=text,
                conversation_id="inbox-watchdog",
            )
        )
        return outcome.accepted

    def _running_actor_uris(self) -> frozenset[str]:
        """Actors this node currently supervises as running.

        Liveness lives here and ⛔ not in the inbox store: the store knows
        mail, the daemon knows processes.  The same split `prune_outbox`
        already uses for its address predicates.
        """

        # A batch path like ps/top: without the request snapshot every online
        # actor's liveness probe costs one supervisor round trip plus one
        # full desired-state load per connector (card 259 / T4).  This runs
        # on every maintenance tick, so the per-actor fallback kept the
        # production daemon at ~0.8 core idle (2026-09-25, SIGUSR1 dump:
        # _watch_inbox_collection -> _running_actor_uris -> ... ->
        # _canonical_harness_uri -> desired_state.load, 273 agents).
        try:
            with self._worker_status_snapshot():
                statuses = self._actor_status_snapshot()
        except Exception:  # noqa: BLE001 - a watchdog must never break the tick
            return frozenset()
        live: set[str] = set()
        for status in statuses:
            if status.get("running") is True or status.get("status") == "online":
                actor = status.get("actor") or status.get("uri")
                if isinstance(actor, str) and actor:
                    live.add(actor)
        return frozenset(live)

    def _on_usage_refreshed(self) -> None:
        watchdog = self._quota_watchdog
        if watchdog is None:
            return
        alerts = watchdog.observe_readings()
        held = watchdog.flush_failures()
        for alert in [*alerts, *([held] if held is not None else [])]:
            self._log("info", "daemon", "quota_watchdog.alerted", kind=alert.kind, key=alert.key)

    def _on_usage_limit_failure(self, recipient: str) -> None:
        watchdog = self._quota_watchdog
        if watchdog is None:
            return
        alert = watchdog.observe_usage_limit_failure(recipient)
        if alert is not None:
            self._log("info", "daemon", "quota_watchdog.alerted", kind=alert.kind, key=alert.key)

    def _deliver_routine_task(
        self, *, target: str, conversation_id: str, text: str, sender: str
    ) -> bool:
        """Deliver one routine task message through the durable inbox.

        PAC notifications carry a reference, never a body, and activating a
        graph does not dispatch its root task -- so the task text travels
        here, on the same inbox IO the PAC notifications use.  The delivery
        is keyed by the conversation, so a replayed dispatch for the same
        task reuses the same message id instead of queueing a second copy.
        """

        io = self._pac_notification_io
        if io is None:
            return False
        try:
            io.deliver(
                # Naming debt, not a workflow dependency: the id minted here
                # still carries the old prefix (inbox/io.py message_id), and
                # renaming a durable message id belongs with U7.
                effect_id=f"routine-task:{conversation_id}",
                sender=sender,
                target=target,
                conversation_id=conversation_id,
                text=text,
            )
        except Exception as error:  # noqa: BLE001 - reported as a dispatch failure
            self._log(
                "warn",
                "routine",
                "routine.task.delivery_failed",
                target=target,
                conversationId=conversation_id,
                error=f"{type(error).__name__}: {error}",
            )
            return False
        return True

    def _assign_routine_probe(self, name: str) -> bool:
        """§C.1.1 三态例程探针,供 assign 周期性核对读「这条 routine 还在吗」。

        True = 存在;False = 查询【成功】且确定没有 —— 删除是决定,
        所以这是核对里唯一算死的「不存在」(§C.1.1① 明确停止);
        读失败让它抛 —— 判定层把异常读成 unknown,⧗ 不算死。
        刻意不看 ``enabled``:disable 是暂停,退役是另一个未落地的决定
        (design-assign §G2),把暂停当死会因一次误操作释放一批 agent。
        打开/关闭一次 store 是刻意的:probe 只在有 routine 边时才会被调,
        而今天没有任何写入方会写 routine 边(assign.py producer registry),
        为此常驻一个连接是伪装成优化的一笔生命周期责任。
        """

        store = RoutineStore(self.state_dir / "routines.sqlite3")
        try:
            return store.get_routine(name) is not None
        finally:
            store.close()

    def _on_assign_reconcile_report(self, report: AssignReconcileReport) -> None:
        """Make each §C.1 pass visible: releases and unknowns both get a line.

        ``unknown`` is why this sink exists: 查不到不释放是安全侧,但安全侧
        的沉默会把一次持续性的读失败藏成永远没人看见的盲区 —— 所以 unknown
        为零且无释放时才静默。Runs in the workflow actor; structlog is
        thread-safe, and the sink must never raise (it is called after the
        stamps are committed, but its own failure must not ripple into the
        timer either).
        """

        try:
            if report.error is not None:
                self._log(
                    "error",
                    "daemon",
                    "assign.reconcile_failed",
                    detail=report.error[:500],
                )
                return
            if not report.dead and not report.unknown:
                return
            self._log(
                "info",
                "daemon",
                "assign.reconciled",
                released=len(report.dead),
                unknown=len(report.unknown),
                alive=report.alive,
                deadRows=[f"{a}|{k}|{r}" for a, k, r in report.dead][:20],
                unknownRows=[f"{a}|{k}|{r}" for a, k, r in report.unknown][:20],
            )
        except Exception:  # noqa: BLE001 - observer is best-effort by contract
            pass

    def _unresolvable_outbox_recipient(self, recipient: str) -> bool:
        """C1: provably NONEXISTENT agent target — never a merely-offline one.

        The rule and why each clause exists:
        - must parse as a canonical agent URI (bare/host shapes are the
          scheme predicate's domain, not this one's);
        - the URI's machine must be THIS node: only this node's registry is
          authoritative for nonexistence.  A REMOTE node's agent that is
          offline looks identical to a dead one from here — never prunable;
        - a REGISTERED agent is real by definition (even down) → keep;
        - a PRESENT agent is alive right now → keep.
        Everything left over is a local identity nothing claims and nothing
        can revive: unresolvable.
        """

        parsed = parse_agent_uri(recipient)
        if parsed is None:
            return False
        owner, machine, actor = parsed
        if machine != self.node_id or owner != self.owner:
            return False
        if self.agents.get(actor) is not None:
            return False
        if self._presence is not None and self._presence.actor_online(recipient):
            return False
        return True

    def _forward_as_actor(
        self, original: InboxMessage, to: str, text: str
    ) -> ForwardOutcome:
        """Send one relayed turn AS the worker that received ``original``.

        The worker (user-proxy) only names the recipient; this is the same
        send boundary ``message.send`` uses, so aliases, ``user:`` and
        ``route:`` targets resolve exactly as they do for any agent.  The
        operation id is derived from the original row, so a redelivered
        turn resends idempotently instead of posting twice.
        """

        operation_id = f"forward:{original.message_id}"
        try:
            if is_route_target(to):
                # A route post can only speak as the bot, so it is attributed
                # in the text.  Attribute it to whoever the proxy is relaying
                # (the original sender), not to the proxy itself.
                self._deliver_route_target(
                    to,
                    text=text,
                    sender=original.sender,
                    conversation=original.conversation_id,
                    operation_id=operation_id,
                    index=0,
                    resources=(),
                )
                return ForwardOutcome(True)
            reply = self.handle(
                "message.send",
                {
                    "from": original.recipient,
                    "to": [to],
                    "message": text,
                    "conversationId": original.conversation_id,
                    "idempotencyKey": operation_id,
                },
            )
        except DaemonRequestError as error:
            code = (
                FORWARD_TARGET_UNKNOWN
                if error.code in _FORWARD_UNKNOWN_TARGET_CODES
                else error.code
            )
            return ForwardOutcome(False, code, f"{error.code}: {error}"[:500])
        deliveries = reply.get("deliveries") if isinstance(reply, dict) else None
        first = deliveries[0] if isinstance(deliveries, list) and deliveries else {}
        if isinstance(first, dict) and first.get("accepted") is True:
            return ForwardOutcome(True)
        code = first.get("code") if isinstance(first, dict) else None
        return ForwardOutcome(
            False,
            "HARNESS_TRANSIENT_FAILURE",
            f"forward to {to} was not accepted ({code or 'no code'})",
        )

    def _deliver_route_target(
        self,
        target: str,
        *,
        text: str,
        sender: str,
        conversation: str,
        operation_id: str,
        index: int,
        resources: tuple[RouteResource, ...],
    ) -> list[JsonObject]:
        """Post to one ``route:<adapter>:<route>`` target, expanding fanout."""

        try:
            route_target = RouteTarget.parse(target)
        except ValueError as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        try:
            configuration = self.load_persistent_configuration()
            gateway_config = find_gateway(
                configuration.channels, route_target.adapter
            )
            resolved = resolve_gateway_routes(gateway_config, route_target.route)
        except RouteDeliveryError as error:
            raise DaemonRequestError(error.code, str(error), error.data) from error
        gateway = self._route_lark_gateway(gateway_config)
        target_key = f"{operation_id}:{target}"
        # Allen: the receiver must be able to tell WHO sent it.  A Lark app
        # can only post as the bot, so the sender is attributed in the text
        # — the same 转述自 <full-actor>： marker the user:<owner> DM path
        # renders.  Text-only injection: the message body structure and any
        # file/image posts stay untouched.
        attributed_text = f"转述自 {sender}：\n\n{text}"
        deliveries: list[JsonObject] = []
        for item in resolved:
            member_key = (
                f"{target_key}:{item.route}" if len(resolved) > 1 else target_key
            )
            message_id = str(
                uuid5(NAMESPACE_URL, f"hyprial:route-send:{member_key}:{index}")
            )
            try:
                native_message_id = gateway.send_chat(
                    item.chat_id, attributed_text, idempotency_key=member_key
                )
                resource_deliveries: list[JsonObject] = []
                for resource_index, resource in enumerate(resources):
                    resource_key = f"{member_key}:resource:{resource_index}"
                    if resource.kind == "file":
                        evidence = gateway.send_chat_file(
                            item.chat_id,
                            resource.path,
                            media_type=resource.media_type,
                            idempotency_key=resource_key,
                        )
                    else:
                        evidence = gateway.send_chat_image(
                            item.chat_id,
                            resource.path,
                            media_type=resource.media_type,
                            idempotency_key=resource_key,
                        )
                    resource_deliveries.append(evidence)
            except LarkApiError as error:
                mapped = map_lark_send_error(
                    error,
                    adapter=gateway_config.name,
                    route=item.route,
                    chat_id=item.chat_id,
                )
                raise DaemonRequestError(
                    mapped.code, str(mapped), mapped.data
                ) from error
            except DaemonRequestError:
                raise
            except (NameError, ImportError):
                raise
            except Exception as error:  # noqa: BLE001 - harness failure boundary
                raise DaemonRequestError(
                    ipc_errors.ROUTE_SEND_FAILED,
                    f"route {item.route!r} (chat_id={item.chat_id}) send via "
                    f"adapter {gateway_config.name!r} failed: {error}",
                    {
                        "adapter": gateway_config.name,
                        "route": item.route,
                        "chatId": item.chat_id,
                    },
                ) from error
            deliveries.append(
                {
                    "target": (
                        target
                        if len(resolved) == 1
                        else f"route:{gateway_config.name}:{item.route}"
                    ),
                    "messageId": message_id,
                    "accepted": True,
                    "queued": False,
                    "routeDelivery": True,
                    "chatId": item.chat_id,
                    "nativeMessageId": native_message_id,
                    **(
                        {"resourceDeliveries": resource_deliveries}
                        if resource_deliveries
                        else {}
                    ),
                    **(
                        {"fanoutOf": target}
                        if len(resolved) > 1
                        else {}
                    ),
                }
            )
            self._log(
                "info",
                "daemon",
                "send.received",
                messageId=message_id,
                correlationId=message_id,
                node="daemon-send",
                conversationId=conversation,
                sender=sender,
                target=target,
                chatId=item.chat_id,
                nativeMessageId=native_message_id,
            )
        return deliveries

    def _restore_adapters(self) -> None:
        assert self._lark_client is not None
        state = self.desired_state.load()
        names = tuple(
            sorted(spec.name for spec in state.harnesses if spec.harness == "lark")
        )
        attempted = 0
        restored = 0
        failed = 0
        for name in names:
            # Restore now runs on its own thread, so it must answer a stop
            # itself: a shutdown that lands mid-restore cannot wait out a
            # full fleet's worth of adapter starts.
            if self.stop_event.is_set():
                break
            attempted += 1
            try:
                self._lark_client.start(name)
                restored += 1
            except (DomainCommandError, RuntimeError):
                failed += 1
        self._log(
            "info",
            "adapter",
            "adapter.recovery.completed",
            attempted=attempted,
            restored=restored,
            failed=failed,
        )

    def _restore_harnesses(self) -> None:
        """Bring back every non-Lark harness this node declared.

        ``DaemonEventBridge.start`` used to do this and deliberately stopped
        (see ``test_event_bridge_start_never_restores_harness_processes``): the
        responsibility moved to whoever composes the daemon.  Nothing here took
        it up, so a restart silently left every declared connector down --
        ``contract/daemon-lifecycle`` caught it and sat red for 38 hours,
        because that contract is not in anybody's routine gate.

        Lark is excluded because :meth:`_restore_adapters` owns exactly those
        specs (``spec.harness == "lark"``).  The two halves partition the
        declaration between them; restoring Lark here would make a second
        owner for adapter routes and liveliness, which is the thing the
        original ``harness_selector=lambda spec: spec.harness != "lark"``
        existed to prevent.  ⚠️ Nothing in the type system keeps these two
        filters complementary -- ``test_daemon_restores_declared_harnesses``
        asserts it instead.

        U0BRACE (hq-adjutant 2026-09-04): this method is also the ONLY
        restart-time starter of non-Lark harness processes.  Construction
        used to submit one create saga per ``status == running`` row via
        ``_bootstrap_lifecycle_harnesses`` -- a predicate IDENTICAL to the
        one below -- so in steady state every such row had two submitters
        and only timing decided who started the process: the restore batch
        deferred to the saga (F1 made that settle), or the saga's claim
        landed after ``_on_restore`` had reset ``_records`` and restore
        started a SECOND process for a row the saga already owned.  That
        second submitter is gone: the startup window submits no lifecycle
        operations at all (``tests/test_daemon_restart_ownership.py``), so
        "each declared row has exactly one owner" is structural, not a
        race outcome.  Durable rows, bindings and persona routes are
        re-derived at construction from their registries; a restart only
        ever owes the PROCESSES, and it owes them here.

        Placement is after ``_start_server``: restored workers talk to the
        daemon over the IPC socket, exactly as the adapter comment above
        ``_restore_adapters`` says of adapter workers. The old call ran inside
        ``runtime.start()``, before the socket existed.

        Since the accept loop moved ahead of restore, both halves run on the
        restore thread (``_restore_then_open_gate``) and must answer
        ``stop_event`` themselves -- a shutdown can no longer wait for them
        on the main thread.
        """

        if self._harnesses is None:
            return
        if self.stop_event.is_set():
            return
        state = self.desired_state.load()
        # U0b (Allen 2026-09-03): desired state is intent + LAST KNOWN
        # RESULT.  Only rows whose last result was "running" are re-run;
        # "failed" rows (ran before, did not come back) are displayed and
        # left for a human, and rows that never earned a result do not
        # exist.  Together with start-after-success this is what makes
        # "a daemon restart never retries a start that never succeeded"
        # true.
        declared = tuple(
            spec
            for spec in state.harnesses
            if spec.harness != "lark" and spec.status == "running"
        )
        # The phase-③ expectation: every declared connector owes one first
        # readiness report.  Recorded before restore runs so the maintenance
        # loop (which starts only after restore completes) never reads a
        # half-written set; keyed exactly like the actor keys its records.
        self._readiness_expected = frozenset(
            f"{spec.harness}:{spec.name}" for spec in declared
        )
        if not declared:
            self._log(
                "info",
                "daemon",
                "harness.recovery.completed",
                attempted=0,
                restored=0,
                failed=0,
            )
            return
        # A restore that fails or times out must not take the daemon with it.
        # It used to: one connector whose start never settled raised out of
        # here, and because this runs before `daemon.ready`, the whole daemon
        # aborted -- taking down every other connector, the IPC socket and the
        # bus, to punish one bad connector. Worse, the abort left the fleet
        # orphaned, and those orphans then stalled the next start the same way,
        # so the daemon could never come back without manual cleanup.
        #
        # Reconcile runs every tick once we are ready and already owns the
        # start timeout, the failure budget and the backoff, so anything
        # missed here is retried by the component whose job that is. Coming up
        # degraded and saying so beats not coming up at all.
        try:
            summary = self._harnesses.restore(declared)
        except Exception as error:  # noqa: BLE001 - startup outlives one connector
            self._log(
                "warn",
                "daemon",
                "harness.recovery.failed",
                declared=len(declared),
                errorType=type(error).__name__,
                error=str(error)[:500],
                detail=(
                    "restore did not complete; the daemon is starting without "
                    "these connectors and reconcile owns bringing them up"
                ),
            )
            # Degraded starts must be visible in the launch summary too: the
            # summary reads stderr, not daemon.jsonl.
            _mirror_startup_event_to_stderr(
                "harness.recovery.failed", declared=len(declared)
            )
            return
        self._log(
            "info",
            "daemon",
            "harness.recovery.completed",
            attempted=summary.attempted,
            restored=summary.restored,
            failed=summary.failed,
            deferred=summary.deferred,
        )

    def _start_restore_thread(
        self,
        step: Callable[[Callable[[], Any], DaemonStartupPhase], None],
    ) -> None:
        """Move restore off the startup path: accept first, restore alongside.

        The gate is cleared here, not inside the thread, so a client that
        connects between socket bind and thread start already reads
        ``phase: "restoring"`` from ping.  The thread is a daemon: a restore
        that cannot be unwound must never hold the interpreter open (the
        exit backstop exists for exactly that class of stuck thread), and
        ``_close`` gives it a bounded join before teardown proceeds.
        """

        self._restore_done.clear()
        thread = threading.Thread(
            target=self._restore_then_open_gate,
            args=(step,),
            name="hyprial-daemon-restore",
            daemon=True,
        )
        self._restore_thread = thread
        thread.start()

    def _restore_then_open_gate(
        self, step: Callable[[Callable[[], Any], DaemonStartupPhase], None]
    ) -> None:
        """Run both restore halves, then open the dispatch gate.

        Ordering inside the thread preserves the pre-thread contract:
        restore completes *before* `daemon.ready` is logged and before the
        first maintenance tick is scheduled, so reconcile still never races
        restore.  A step that escapes (only adapter/harness restore are
        wrapped, each already failure-isolated) aborts the daemon exactly
        as it did inline: `stop_event` takes the accept loop down, and
        `run()` re-raises the captured error once `_serve` returns.
        """

        try:
            step(self._restore_adapters, DaemonStartupPhase.RESTORE_ADAPTERS)
            step(self._restore_harnesses, DaemonStartupPhase.RESTORE_HARNESSES)
            step(
                self._restore_provider_auth,
                DaemonStartupPhase.RESTORE_PROVIDER_AUTH,
            )
        except BaseException as error:
            self._restore_error = error
            self.stop_event.set()
            return
        if self.stop_event.is_set():
            # Stopped mid-restore: the gate stays closed and `daemon.ready`
            # is not claimed for a daemon that is leaving.
            return
        self._restore_done.set()
        self._log("info", "daemon", "daemon.ready", nodeId=self.node_id)
        self._start_maintenance_scheduler()

    def _restore_provider_auth(self) -> None:
        """Re-open broken provider-auth episodes after a daemon restart.

        追加 1 ①: the dispatch mark blocks the failures that would re-trigger
        a relogin flow, so restore is the bounded re-trigger point.  The
        coordinator reconciles first — a credential fixed while the daemon
        was down is a recovery, not a new flow.
        """

        if self._provider_auth is not None:
            self._provider_auth.resume_after_restore()

    def _join_restore_thread(self) -> None:
        """Give the restore thread a bounded chance to leave before teardown.

        Absence of a raise is deliberate: the thread is a daemon, the loops
        it drives poll `stop_event`, and a restore that outlives this join
        is the exit backstop's case -- holding teardown hostage to it would
        recreate the "process that cannot die" class this codebase keeps
        meeting.
        """

        thread = self._restore_thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=_RESTORE_THREAD_JOIN_TIMEOUT)

    def _start_server(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.socket_path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            server.listen(32)
            server.settimeout(0.25)
        except BaseException:
            server.close()
            raise
        self._server = server
        # Keep one descriptor in reserve so an EMFILE accept can discard one
        # queued peer and return to bounded retrying instead of spinning or
        # permanently terminating the dispatcher.
        self._accept_reserve_fd = os.open(os.devnull, os.O_RDONLY)

    def _serve(self) -> None:
        assert self._server is not None
        assert self._runtime is not None
        # Direct-`_serve` drivers (the IPC test suites) have no restore
        # thread, so the maintenance fallback still fires for them.  In a
        # real `run()` the restore thread owns starting the scheduler when
        # restore completes -- firing it here would start reconcile ticks
        # that race the very restore they must come after.
        if self._maintenance_generation == 0 and self._restore_thread is None:
            self._start_maintenance_scheduler()
        accept_retry = _IPC_ACCEPT_RETRY_INITIAL
        while not self.stop_event.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                connection = None
            except OSError as error:
                if self.stop_event.is_set() or error.errno in {
                    errno.EBADF,
                    errno.EINVAL,
                }:
                    break
                if error.errno in {errno.EMFILE, errno.ENFILE}:
                    error_code = errno.errorcode.get(error.errno, "RESOURCE_EXHAUSTED")
                    self._log_accept_resource_exhausted(
                        type(error).__name__, error_code
                    )
                    if error.errno == errno.EMFILE:
                        self._discard_one_client_with_reserve()
                    if self.stop_event.wait(accept_retry):
                        break
                    accept_retry = min(accept_retry * 2, _IPC_ACCEPT_RETRY_MAX)
                    continue
                raise
            if connection is not None:
                accept_retry = _IPC_ACCEPT_RETRY_INITIAL
                self._start_ipc_client(connection)

    def _start_maintenance_scheduler(self) -> None:
        self._maintenance_generation += 1
        self._schedule_maintenance(self._maintenance_generation, delay=1.0)

    def _schedule_maintenance(self, generation: int, *, delay: float) -> None:
        if self.stop_event.is_set():
            return
        self._maintenance_scheduler.schedule(
            "daemon-maintenance",
            generation,
            delay,
            self._scheduled_maintenance,
        )

    def _scheduled_maintenance(self, generation: int) -> None:
        if (
            generation != self._maintenance_generation
            or self.stop_event.is_set()
        ):
            return
        started = time.monotonic()
        try:
            outcome, adapter_events, adapter_restarts, phases = (
                self._run_scheduled_domains(started)
            )
            self._record_maintenance_outcome(
                started,
                outcome,
                adapter_events,
                adapter_restarts,
                phases,
            )
        except Exception as error:  # noqa: BLE001 - maintenance isolation boundary
            self._log(
                "error",
                "daemon",
                "daemon.reconcile_failed",
                errorType=type(error).__name__,
                detail=str(error)[:500],
            )
        finally:
            self._schedule_maintenance(generation, delay=1.0)

    def _record_maintenance_outcome(
        self,
        started: float,
        outcome: Any,
        adapter_events: tuple[Any, ...],
        adapter_restarts: int,
        phases: tuple[tuple[str, int], ...],
    ) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        budget = _reconcile_tick_budget()
        if budget > 0 and duration_ms > int(budget * 1000):
            sub_phases = [
                (f"runtime.{name}", ms)
                for name, ms in getattr(outcome, "phase_ms", ())
            ]
            candidates = [
                entry
                for entry in phases
                if not (sub_phases and entry[0] == "runtime.timer")
            ] + sub_phases
            item = max(candidates, key=lambda entry: entry[1])[0] if candidates else "unknown"
            # The whole tick paid the overrun, so name the worst offenders,
            # not just the single slowest step: the old one-item form read as
            # "everything timed out" whenever one step did (2026-09-14).
            top = sorted(candidates, key=lambda entry: entry[1], reverse=True)[:3]
            self._log(
                "error",
                "daemon",
                "daemon.reconcile_overrun",
                durationMs=duration_ms,
                budgetMs=int(budget * 1000),
                item=item,
                items=[{"item": name, "ms": ms} for name, ms in top],
            )
        for failed in getattr(outcome, "harness_failed", ()):
            self._log(
                "error",
                "daemon",
                "daemon.harness.failed",
                harness=failed,
                detail="harness retry budget exhausted; explicit start is required",
            )
            self._raise_harness_failed_alarm(failed)
        self._aggregate_readiness_reports(getattr(outcome, "harness_readiness", ()))
        for adapter_event in adapter_events:
            fields = dict(adapter_event)
            event = str(fields.pop("event", "lark.inbound.health"))
            level = (
                "error"
                if event in {"lark.inbound.stale", "lark.adapter.lifecycle.timeout"}
                else "warn"
                if event
                in {"lark.pin.query_failed", "lark.identity.lookup_failed"}
                else "info"
            )
            self._log(level, "lark-adapter", event, **fields)
        if outcome.harness_restarts or outcome.inbox_results or adapter_restarts:
            self._log(
                "info",
                "daemon",
                "daemon.reconciled",
                harnessRestarts=outcome.harness_restarts,
                inboxResults=outcome.inbox_results,
                inboxPruned=outcome.inbox_pruned,
                adapterRestarts=adapter_restarts,
            )
        for item in outcome.inbox_pruned_items:
            self._log(
                "info",
                "daemon",
                "inbox.pruned",
                messageId=item.message_id,
                correlationId=item.message_id,
                node="delivery-terminal",
                recipient=item.recipient,
                reason=item.reason,
                createdAtMs=item.created_at_ms,
                receivedAtMs=item.received_at_ms,
            )
        self._watch_inbox_collection(outcome.inbox_pruned_items)

    def _watch_inbox_collection(
        self, pruned: tuple[InboxPruneItem, ...] = ()
    ) -> None:
        """Report mail nobody is collecting -- before and after the deadline.

        Both readings need the same liveness answer, so they share one
        snapshot: the periodic "nobody has taken this" check runs at most
        once per ``_INBOX_WATCH_INTERVAL_MS`` because it is a GROUP BY over
        the whole inbox (129 MB on the production node), while the
        prune-time check rides the sweep that just produced its input.
        """

        watchdog = self._inbox_watchdog
        if watchdog is None:
            return
        now_ms = time.time_ns() // 1_000_000
        due = now_ms - self._inbox_watchdog_checked_at_ms >= _INBOX_WATCH_INTERVAL_MS
        lost = tuple(
            (item.recipient, item.message_id)
            for item in pruned
            if item.reason == "INBOX_TTL_EXPIRED"
        )
        if not due and not lost:
            return
        live = self._running_actor_uris()
        alerts = []
        try:
            if lost:
                alerts += watchdog.observe_pruned(lost, is_running=live.__contains__)
            if due:
                self._inbox_watchdog_checked_at_ms = now_ms
                stats = getattr(self._inbox, "unfetched_recipient_stats", None)
                if callable(stats):
                    alerts += watchdog.observe_unfetched(
                        stats(), is_running=live.__contains__
                    )
        except Exception as error:  # noqa: BLE001 - a watchdog never breaks the tick
            self._log(
                "error",
                "daemon",
                "inbox_watchdog.failed",
                error=f"{type(error).__name__}: {error}",
            )
            return
        for alert in alerts:
            self._log(
                "warn", "daemon", "inbox_watchdog.alerted", kind=alert.kind, key=alert.key
            )

    def _aggregate_readiness_reports(
        self, reports: tuple[ReadinessReport, ...]
    ) -> None:
        """Fold one tick's disposition reports into the daemon.readiness projection.

        Two questions only: did the report arrive, and what verdict did it
        carry.  First arrival per connector and the first complete round are
        different observations and are logged under different events
        (``daemon.readiness.report`` / ``daemon.readiness.reconciled``).  A
        verdict outside the known set is recorded verbatim, never branched
        on: the schema contract is the three fields, the vocabulary grows
        with reporters, and an aggregator that interprets content would be
        claiming a knowledge the design deliberately withholds from it.
        """

        for report in reports:
            if report.source in self._readiness_reports:
                self._readiness_reports[report.source] = report
                continue
            self._readiness_reports[report.source] = report
            self._log(
                "info",
                "daemon",
                "daemon.readiness.report",
                connector=report.source,
                phase=report.phase,
                verdict=report.verdict,
            )
        if (
            not self._readiness_first_round.is_set()
            and self._readiness_expected <= self._readiness_reports.keys()
        ):
            self._readiness_first_round.set()
            # A histogram, not a branch: failure must look different from
            # success in the record, and counting per verdict value is the
            # whole of what the daemon may do with the content.
            tally: dict[str, int] = {}
            for key in sorted(self._readiness_expected):
                verdict = self._readiness_reports[key].verdict
                tally[verdict] = tally.get(verdict, 0) + 1
            self._log(
                "info",
                "daemon",
                "daemon.readiness.reconciled",
                expected=len(self._readiness_expected),
                verdicts=tally,
            )

    def _run_scheduled_domains(
        self, now: float
    ) -> tuple[Any, tuple[Any, ...], int, tuple[tuple[str, int], ...]]:
        """Submit one scheduler-owned cadence event to each domain.

        Client reads are concurrent, so the dispatcher no longer implicitly
        orders maintenance against request handling. Route expiry must not act
        on a stale observation after a heartbeat has already returned alive;
        runtime and adapter reconciliation likewise mutate daemon-owned state.
        """

        assert self._runtime is not None
        phases: list[tuple[str, int]] = []

        def _timed(name: str, fn: Callable[[], Any]) -> Any:
            started_at = time.monotonic()
            try:
                return fn()
            finally:
                phases.append((name, int((time.monotonic() - started_at) * 1000)))

        _timed("routes.expire", lambda: self._expire_stale_channel_routes(now))
        _timed("forwarding.timer", self._reconcile_forwarding_endpoints)
        observed_at_ms = int(time.time_ns() // 1_000_000)
        outcome = _timed("runtime.timer", lambda: self._runtime_timer(observed_at_ms))

        # Timer admission is bounded and never shares ownership with local IPC.
        # Each domain actor serializes its own cadence with its business commands.
        if self._workflow_service is not None:
            _timed(
                "workflow.timer",
                lambda: self._workflow_service.submit_timer(observed_at_ms),
            )
        if self._routine_service is not None:
            _timed(
                "routine.timer",
                lambda: self._routine_service.submit_timer(observed_at_ms),
            )
        if self._pac_actor_service is not None:
            _timed("pac.actor.timer", self._pac_actor_service.submit_tick)

        adapter_events = (
            _timed("adapters.drain_health", self._lark_client.drain_health_events)
            if self._lark_client
            else ()
        )
        adapter_restarts = (
            int(
                _timed(
                    "adapters.timer",
                    lambda: self._lark_client.timer(
                        int(time.time_ns() // 1_000_000)
                    ),
                ).changed
            )
            if self._lark_client
            else 0
        )
        return outcome, adapter_events, adapter_restarts, tuple(phases)

    def _raise_harness_failed_alarm(self, key: str) -> None:
        """Best-effort operator alarm for one harness whose budget ran out.

        Failed is terminal -- nothing retries it again -- so this alarm is the
        only path that gets a human involved.  The emitter is already a
        non-raising, non-recursive boundary; when no delivery channel is
        configured the trajectory events
        (``alarm.raised``/``alarm.failed``) are still written, which is the
        guaranteed-loud part.
        """

        inbox = self._inbox
        emitter = getattr(inbox, "alarm_emitter", None) if inbox is not None else None
        if emitter is None:
            return
        try:
            emitter.emit(
                Alarm(
                    correlation_id=f"harness-failed:{key}",
                    message_id=f"harness-failed:{key}",
                    conversation_id=f"daemon-harness:{self.node_id}",
                    sender=self.node_id,
                    recipient=self.owner,
                    reason=f"HARNESS_FAILED:{key}",
                    audience="operator",
                ),
                terminal=False,
            )
        except Exception:  # noqa: BLE001 - alarming must never hurt the loop
            pass

    def _log_accept_resource_exhausted(
        self, error_type: str, error_code: str
    ) -> None:
        """Best-effort classification that cannot amplify fd exhaustion."""

        try:
            self._log(
                "warn",
                "daemon",
                "daemon.ipc.accept_resource_exhausted",
                errorType=error_type,
                errorCode=error_code,
            )
        except OSError as log_error:
            if log_error.errno not in {errno.EMFILE, errno.ENFILE}:
                raise

    def _discard_one_client_with_reserve(self) -> None:
        """Use the reserved fd to drain one queued peer after process EMFILE."""

        reserve_fd = self._accept_reserve_fd
        if reserve_fd is None or self._server is None:
            return
        self._accept_reserve_fd = None
        os.close(reserve_fd)
        try:
            connection, _ = self._server.accept()
        except OSError:
            pass
        else:
            connection.close()
        finally:
            try:
                self._accept_reserve_fd = os.open(os.devnull, os.O_RDONLY)
            except OSError:
                # The retry loop remains bounded even if system-wide pressure
                # prevents restoring the reserve immediately.
                self._accept_reserve_fd = None

    def _start_ipc_client(self, connection: socket.socket) -> None:
        """Admit one peer atomically with shutdown, within a fixed cap.

        Capacity is an explicit local IPC contract: when all 64 client slots
        are occupied, the newly accepted overflow connection is closed without
        starting a worker. Callers may retry after an existing request exits.
        """

        capacity_exhausted = False
        with self._ipc_clients_lock:
            if self.stop_event.is_set():
                self._ipc_closing = True
            if self._ipc_closing:
                connection.close()
                return
            if not self._ipc_client_slots.acquire(blocking=False):
                capacity_exhausted = True
            else:
                worker = self._new_ipc_client_worker(connection)
                self._ipc_clients.add(connection)
                self._ipc_client_threads.add(worker)
                try:
                    # Registration and start share the shutdown lock. Closing
                    # therefore observes either no worker or a started worker,
                    # never a registered thread that it cannot yet wake/join.
                    worker.start()
                except BaseException:
                    self._ipc_clients.discard(connection)
                    self._ipc_client_threads.discard(worker)
                    self._ipc_client_slots.release()
                    connection.close()
                    raise

        if capacity_exhausted:
            connection.close()
            try:
                self._log(
                    "warn",
                    "daemon",
                    "daemon.ipc.client_capacity_exhausted",
                    errorType="ClientCapacityError",
                    errorCode="IPC_CLIENT_CAPACITY",
                )
            except OSError as log_error:
                if log_error.errno not in {errno.EMFILE, errno.ENFILE}:
                    raise

    def _new_ipc_client_worker(
        self, connection: socket.socket
    ) -> threading.Thread:
        """Build the worker whose registration/start are admission-locked."""

        def serve() -> None:
            try:
                self._serve_client(connection)
            finally:
                current = threading.current_thread()
                with self._ipc_clients_lock:
                    self._ipc_clients.discard(connection)
                    self._ipc_client_threads.discard(current)
                self._ipc_client_slots.release()

        return threading.Thread(
            target=serve,
            name="hyprial-daemon-ipc-client",
            daemon=True,
        )

    def _close_ipc_clients(self) -> None:
        """Wake and reap all client workers without leaking daemon threads."""

        current = threading.current_thread()
        with self._ipc_clients_lock:
            # This is the shutdown/admission linearization point. Once set, no
            # accepted connection can be registered or start a worker.
            self._ipc_closing = True
        grace_deadline = time.monotonic() + _IPC_CLIENT_SHUTDOWN_GRACE
        while time.monotonic() < grace_deadline:
            with self._ipc_clients_lock:
                threads = tuple(
                    item
                    for item in self._ipc_client_threads
                    if item is not current and item.is_alive()
                )
            if not threads:
                return
            for thread in threads:
                thread.join(timeout=0.01)

        with self._ipc_clients_lock:
            clients = tuple(self._ipc_clients)
            threads = tuple(
                item for item in self._ipc_client_threads if item is not current
            )
        for connection in clients:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        deadline = time.monotonic() + _IPC_CLIENT_SHUTDOWN_TIMEOUT
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._ipc_clients_lock:
            still_running = tuple(
                item
                for item in self._ipc_client_threads
                if item is not current and item.is_alive()
            )
        if still_running:
            raise RuntimeError(
                "daemon IPC client workers did not stop within "
                f"{_IPC_CLIENT_SHUTDOWN_TIMEOUT:g}s"
            )

    def _serve_client(self, connection: socket.socket) -> None:
        """Serve one local client without letting it terminate the daemon."""

        try:
            with connection:
                # A close/shutdown from another thread does not reliably wake
                # an AF_UNIX recv on every supported kernel (notably Darwin).
                # Polling keeps the existing 15-second idle bound while making
                # daemon shutdown observable without depending on that wakeup.
                connection.settimeout(_IPC_CLIENT_POLL_INTERVAL)
                self._serve_connection(connection)
        except (OSError, DaemonRequestError) as error:
            # Timeouts, resets and broken pipes describe only this local IPC
            # connection.  Likewise, an oversized request is client-scoped.
            # Log classifications only: exception text can contain client
            # controlled or otherwise sensitive data.
            fields: JsonObject = {"errorType": type(error).__name__}
            if isinstance(error, DaemonRequestError):
                fields["errorCode"] = error.code
            self._log("warn", "daemon", "daemon.ipc.client_error", **fields)

    def _serve_connection(self, connection: socket.socket) -> None:
        buffer = bytearray()
        idle_deadline = time.monotonic() + _IPC_CLIENT_IDLE_TIMEOUT
        while len(buffer) <= 8 * 1024 * 1024:
            try:
                chunk = connection.recv(64 * 1024)
            except TimeoutError:
                with self._ipc_clients_lock:
                    closing = self._ipc_closing
                if closing or self.stop_event.is_set():
                    return
                if time.monotonic() >= idle_deadline:
                    raise
                continue
            if not chunk:
                return
            buffer.extend(chunk)
            idle_deadline = time.monotonic() + _IPC_CLIENT_IDLE_TIMEOUT
            if b"\n" not in buffer:
                continue
            line, _, _ = buffer.partition(b"\n")
            # Domain actors/facades own business ordering.  This lock only
            # fences socket admission against shutdown; no slow lifecycle,
            # workflow or adapter operation can convoy unrelated IPC.
            with self._ipc_clients_lock:
                if self._ipc_closing or self.stop_event.is_set():
                    return
            response = self._response(line)
            connection.sendall(
                json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode()
                + b"\n"
            )
            return
        raise DaemonRequestError(
            ipc_errors.IPC_REQUEST_TOO_LARGE, "daemon IPC request exceeded 8 MiB"
        )

    def _response(self, line: bytes) -> JsonObject:
        request_id: object = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise DaemonRequestError(
                    ipc_errors.INVALID_REQUEST, "daemon request must be an object"
                )
            request_id = request.get("id")
            if request.get("version") != 1:
                raise DaemonRequestError(
                    ipc_errors.VERSION_MISMATCH, "unsupported daemon IPC version"
                )
            method = _required_string(request.get("method"), "method")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "params must be an object")
            if (
                method == "message.send"
                and isinstance(request_id, str)
                and request_id
                and "idempotencyKey" not in params
            ):
                params = {**params, "idempotencyKey": request_id}
            result = self._timed_handle(method, params)
            return {"version": 1, "id": request_id, "result": result}
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - stable daemon error boundary
            code = getattr(error, "code", ipc_errors.DAEMON_ERROR)
            failure: JsonObject = {"code": str(code), "message": str(error)}
            data = getattr(error, "data", None)
            if data is not None:
                failure["data"] = data
            return {"version": 1, "id": request_id, "error": failure}

    def _timed_handle(self, method: str, params: JsonObject) -> Any:
        """``handle`` for one IPC request, charged to ``daemon.ipcStats``.

        This is the single point every socket request passes through; the
        in-process ``self.handle(...)`` calls (restore, lifecycle helpers)
        deliberately bypass it, so the counters mean "cost of serving IPC".
        Only this thread's CPU is read: time the handler spends parked on a
        domain actor is charged on that actor's own thread, never here (the
        attribution rule in ``ipc_stats``).  Request framing -- JSON parse,
        envelope, ``sendall`` -- stays outside the timed span and is a named
        uncovered category.
        """

        stats = self._ipc_stats
        if not stats.enabled:
            return self.handle(method, params)
        failed = True
        started_cpu = time.thread_time()
        started_wall = time.perf_counter()
        try:
            result = self.handle(method, params)
            failed = False
            return result
        finally:
            stats.record(
                method,
                cpu_seconds=time.thread_time() - started_cpu,
                wall_seconds=time.perf_counter() - started_wall,
                error=failed,
            )

    def _registry_management_handler(self) -> RegistryManagementHandler:
        with self._registry_management_lock:
            management = self._registry_management
            if management is None:
                assert self._harnesses is not None
                management = RegistryManagementHandler(
                    self._harnesses,
                    self.agents,
                    start_harness=lambda spec: self.handle(
                        "lifecycle.start", spec.to_json()
                    ),
                )
                self._registry_management = management
            return management

    @staticmethod
    def _routine_coordinator_marker(name: str, registration_id: object = None) -> str:
        prefix = f"routine:{name}:{registration_id}" if registration_id else f"routine:{name}"
        return f"{prefix}:coordinator"

    def _reconcile_routine_coordinators(self) -> None:
        """Isolate persisted routine faults so daemon startup remains operable."""
        assert self._routine_service is not None
        try:
            routines = self._routine_service.list()["routines"]
        except Exception as error:  # noqa: BLE001 - corrupt rows must not brick daemon
            self._log(
                "error",
                "daemon",
                "routine.coordinator.read_failed",
                errorType=type(error).__name__,
                detail=str(error),
            )
            return
        for routine in routines:
            assert isinstance(routine, dict)
            try:
                coordinator = self._ensure_routine_coordinator(
                    routine, recovering=True
                )
            except Exception as error:  # noqa: BLE001 - isolate one bad routine row
                self._log(
                    "error",
                    "daemon",
                    "routine.coordinator.reconcile_failed",
                    routine=routine.get("name"),
                    errorType=type(error).__name__,
                    detail=str(error),
                )
                continue
            if coordinator is not None:
                self._log(
                    "info",
                    "daemon",
                    "routine.coordinator.reconciled",
                    routine=routine["name"],
                    **coordinator,
                )

    def _ensure_routine_coordinator(self, routine: dict[str, object], *, recovering: bool) -> dict[str, object] | None:
        schema_error = routine.get("schemaError")
        if isinstance(schema_error, str):
            raise DaemonRequestError("ROUTINE_SCHEMA_ERROR", schema_error)
        produced = routine.get("produces")
        if not isinstance(produced, str):
            return None
        parsed = parse_agent_uri(produced)
        if parsed is None:
            raise DaemonRequestError("ROUTINE_COORDINATOR_INVALID", "produces must be a canonical agent URI")
        if parsed[:2] != (self.owner, self.node_id):
            raise DaemonRequestError("ROUTINE_COORDINATOR_IDENTITY_MISMATCH",
                                     f"coordinator {produced} must belong to agent:{self.owner}:{self.node_id}:*")
        name, actor_name = str(routine["name"]), parsed[2]
        marker = self._routine_coordinator_marker(name, routine.get("registrationId"))
        existing = [item for item in self.desired_state.load().harnesses if item.name == actor_name]
        if existing:
            if existing[0].nickname != marker:
                raise DaemonRequestError("ROUTINE_COORDINATOR_CONFLICT",
                                         f"actor {produced} is not owned by routine {name}")
            return {"actor": produced, "restored": recovering, "changed": False}
        launch = routine.get("launch")
        if not isinstance(launch, dict):
            from hyprial.dispatch.matrix import resolve
            try:
                choice = resolve("fast")
            except (RuntimeError, ValueError) as error:
                raise DaemonRequestError("ROUTINE_COORDINATOR_UNAVAILABLE", str(error)) from error
            launch = {"harness": choice.harness, "model": choice.model, "provider": choice.provider}
        launched = self.handle("lifecycle.start", {
            "provider": launch["harness"], "name": actor_name, "headless": True,
            "nickname": marker, "model": launch.get("model"),
            **({"modelProvider": launch["provider"]} if launch.get("provider") is not None else {}),
            **({"cwd": launch["cwd"]} if launch.get("cwd") is not None else {}),
            **({"args": launch["args"]} if launch.get("args") else {}),
            "operationId": f"{marker}:start",
        })
        if not isinstance(launched, dict) or launched.get("actor") != produced:
            self.handle("down", {"target": actor_name, "provider": launch["harness"]})
            raise DaemonRequestError("ROUTINE_COORDINATOR_IDENTITY_MISMATCH",
                                     f"coordinator did not resolve as {produced}")
        return {**launched, "restored": False}

    def _retire_routine_coordinator(self, routine: dict[str, object]) -> dict[str, object] | None:
        produced = routine.get("produces")
        name = str(routine["name"])
        marker = self._routine_coordinator_marker(name, routine.get("registrationId"))
        if isinstance(routine.get("schemaError"), str):
            # The schema is unreadable, so its raw ``produces`` value cannot
            # authorize a stop. The durable ownership marker still can.
            matches = [
                item
                for item in self.desired_state.load().harnesses
                if item.nickname == marker
            ]
            if not matches:
                return None
            item = matches[0]
            actor = canonical_agent_uri(self.owner, self.node_id, item.name)
            result = self.handle(
                "down", {"target": item.name, "provider": item.harness}
            )
            return {
                "actor": actor,
                "retired": True,
                "changed": bool(result.get("removed")),
            }
        if not isinstance(produced, str):
            return None
        parsed = parse_agent_uri(produced)
        if parsed is None:
            raise DaemonRequestError("ROUTINE_COORDINATOR_INVALID", "produces must be a canonical agent URI")
        actor_name = parsed[2]
        matches = [item for item in self.desired_state.load().harnesses if item.name == actor_name]
        if not matches:
            return {"actor": produced, "retired": False, "changed": False}
        item = matches[0]
        if item.nickname != self._routine_coordinator_marker(name, routine.get("registrationId")):
            raise DaemonRequestError("ROUTINE_COORDINATOR_CONFLICT",
                                     f"actor {produced} is not owned by routine {name}")
        result = self.handle("down", {"target": actor_name, "provider": item.harness})
        return {"actor": produced, "retired": True, "changed": bool(result.get("removed"))}

    def handle(self, method: str, params: JsonObject) -> Any:
        # The phase-① probe.  Everything it reports is already in memory --
        # it must never grow a read of desired state, an actor round trip or
        # a lark call: it exists so 0.5s readiness pollers have something
        # that answers while restore is still running, and becoming a new
        # load storm would defeat the point.  `zenoh` rides along because
        # `_start_runtime` resolves the endpoints before the socket exists,
        # and `hyprial init`'s endpoint warning reads them from this payload.
        if method == "ping":
            restore_done = self._restore_done.is_set()
            return {
                "running": True,
                "pid": os.getpid(),
                "epoch": self.epoch,
                "version": __version__,
                "nodeId": self.node_id,
                # owner/socket keep init's spread payload at parity with the
                # ps daemon block it used to carry; all of this is in-memory
                # identity, resolved before the socket exists.
                "owner": self.owner,
                "identityMode": self.identity_mode,
                "identityIssuer": self.identity_issuer,
                "socket": str(self.socket_path),
                "migration": {
                    "status": "applied",
                    "rewrittenCells": self._owner_migration_rewrites,
                },
                # Three phases: "restoring" while the restore thread runs,
                # "serving" once the gate opens, and "reconciled" once the
                # first reconcile round holds a readiness report for every
                # connector the restore declared -- the phase-③ event ("this
                # round dispositioned every desired connector"), which is
                # monotonic: later rounds update the projection but never
                # revoke it.
                "phase": (
                    "restoring"
                    if not restore_done
                    else (
                        "reconciled"
                        if self._readiness_first_round.is_set()
                        else "serving"
                    )
                ),
                "restorePending": not restore_done,
                "zenoh": {
                    "listen": list(self.zenoh_listen),
                    "connect": list(self.zenoh_connect),
                    "isolated": self._network_isolation_status()["effective"],
                    "isolation": self._network_isolation_status(),
                },
                "forwarding": self._forwarding_status_json(),
                "workerProxy": self._worker_proxy_status_json(),
            }
        # The restore gate lives at dispatch, not inside each method: while
        # restore is running, half-initialised collaborators must not be
        # reachable at all, and a probe must never queue behind a minute of
        # adapter starts.  Refusing here is what keeps the light set light.
        if (
            not self._restore_done.is_set()
            and method not in _RESTORE_GATE_LIGHT_METHODS
        ):
            # PR #332 F4②: the minted exception IS the registered class; the
            # envelope's ``code`` is serialised from it (``_response`` reads
            # ``.code``), and every client deserialises that code back into
            # this same class.
            raise ipc_errors.DaemonRestoringError(
                "daemon is restoring its connectors; only the light methods "
                "(ping, shutdown) are served until restore completes"
            )
        assert self._inbox is not None
        assert self._harnesses is not None
        assert self._presence is not None
        if method == "management.squire.ensure":
            command = EnsureSquireRegistryCommand.from_payload(params)
            result = self._registry_management_handler().ensure_squire(command)
            return {"ok": True, **result.to_payload()}
        if method == "management.adapter.remove":
            from hyprial.adapter_registration import (
                AdapterConfigConflictError,
                AdapterNotFoundError,
                remove_lark_gateway,
            )
            from hyprial.daemon.desired_state import DesiredStateError
            from hyprial.persistent_config import PersistentConfigError

            name = _required_string(params.get("name"), "name")
            try:
                return remove_lark_gateway(
                    hyprial_home=self.hyprial_home,
                    state_dir=self.state_dir,
                    name=name,
                    management=self._registry_management_handler(),
                )
            except AdapterNotFoundError as error:
                raise DaemonRequestError(
                    ipc_errors.ADAPTER_NOT_FOUND, str(error)
                ) from error
            except AdapterConfigConflictError as error:
                raise DaemonRequestError(error.code, str(error)) from error
            except PersistentConfigError as error:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT, str(error)
                ) from error
            except DesiredStateError as error:
                raise DaemonRequestError("DESIRED_STATE_ERROR", str(error)) from error
        if method == "autoupdate.status":
            return {
                "ok": True,
                "trigger": "daemon",
                "environmentPathDigest": hashlib.sha256(
                    os.environ.get("PATH", "").encode()
                ).hexdigest(),
                "schedule": [
                    {"hour": hour, "minute": minute}
                    for hour, minute in SCHEDULE
                ],
                **self._autoupdate.status(),
            }
        if method == "autoupdate.trigger":
            triggered = self._autoupdate.trigger("ipc")
            return {
                "ok": True,
                "triggered": triggered,
                "reason": (
                    "accepted by daemon scheduler"
                    if triggered
                    else "an update is already active or pending"
                ),
                "scheduler": self._autoupdate.status(),
            }
        if method == "autoupdate.notify":
            return self._deliver_autoupdate_restart_notification(params)
        if method == "ps":
            now = time.monotonic()
            # Read the session projections once for the whole request and hand
            # them to route expiry: closing a route never edits the session
            # store, so the pre-expiry read is exactly what expiry would read
            # itself — the old second read was one redundant desired-state
            # load per ps.
            interactive_sessions = self._agent_session_domains.session.read_sessions()
            self._expire_stale_channel_routes(now, sessions=interactive_sessions)
            # Node-wide pending: rows wait under agent URIs, not the node
            # id, so scoping this to the node inbox would report an empty
            # node with a full one (the runner-observation blind spot).
            pending = self._inbox.pending_all()
            historical_inbox_recipients = self._historical_inbox_recipients()
            # One request, one worker-status snapshot: connectors[] and every
            # agents[] verdict read the same tables instead of each verdict
            # paying its own supervisor round trip and per-connector loads.
            with self._worker_status_snapshot() as worker_status:
                connector_statuses = [dict(item) for item in worker_status.statuses]
                desired_by_key = {
                    (spec.harness, spec.name): spec
                    for spec in (worker_status.desired.harnesses if worker_status.desired else ())
                }
                for row in connector_statuses:
                    if row.get("runtime") not in {"jev", "user-proxy"}:
                        continue
                    spec = desired_by_key.get((row.get("runtime"), row.get("name")))
                    actor = (
                        self._canonical_harness_uri(str(row["name"]), spec)
                        if spec is not None
                        else str(row.get("name"))
                    )
                    pending_count = sum(
                        1 for message in pending
                        if getattr(message, "recipient", None) in {actor, row.get("name")}
                    )
                    in_flight = row.get("inFlight")
                    row["queueDepth"] = max(
                        0,
                        pending_count - (in_flight if isinstance(in_flight, int) else 0),
                    )
                return {
                    "daemon": {
                        "running": True,
                        "pid": os.getpid(),
                        "epoch": self.epoch,
                        "version": __version__,
                        "nodeId": self.node_id,
                        "owner": self.owner,
                        "socket": str(self.socket_path),
                        "lifecycle": self._lifecycle_status(),
                        "dispatchWithoutPacCount": self._dispatch_without_pac_snapshot(),
                        "dispatchConversationCount": self._dispatch_conversation_snapshot(),
                        # Additive and read-only: the per-method cost
                        # counters plus processCpuSeconds read at the same
                        # moment, so two ps snapshots reconcile the
                        # counters against process CPU (ipc_stats docstring).
                        "ipcStats": ipc_stats_payload(
                            methods=self._ipc_stats,
                            session_actor=(
                                self._agent_session_domains.session.command_costs
                            ),
                            agent_actor=self._agent_session_domains.agent.command_costs,
                            effect_admission=(
                                self._agent_session_domains.session.effect_admission_costs
                            ),
                        ),
                    },
                    "zenoh": {
                        "listen": list(self.zenoh_listen),
                        "connect": list(self.zenoh_connect),
                        "isolated": self._network_isolation_status()["effective"],
                        "isolation": self._network_isolation_status(),
                    },
                    "duplicateInstance": self._duplicate_instance_payload(),
                    "forwarding": self._forwarding_status_json(),
                    "workerProxy": self._worker_proxy_status_json(),
                    "connectors": connector_statuses,
                    "orphanProcesses": list(self._orphan_process_status()),
                    "adapters": (
                        [item.to_payload() for item in self._lark_client.read_adapters()]
                        if self._lark_client is not None
                        else []
                    ),
                    # The agent entity's own view: identity + configuration +
                    # the single liveness verdict. connectors[]/interactiveSessions[]
                    # stay as the per-runtime detail they always were, but they no
                    # longer each answer "is it alive" their own way.
                    "agents": [
                        self._agent_status_json(agent) for agent in self.agents.list()
                    ],
                    "interactiveSessions": [
                        self._interactive_session_status_projection(session, now)
                        for session in interactive_sessions
                    ],
                    "pendingMessages": [
                        {
                            "messageId": message.message_id,
                            "deliveryId": f"{self.epoch}:{index}:{message.message_id}",
                            "conversationId": message.conversation_id,
                            "from": message.sender,
                            "recipient": message.recipient,
                        }
                        for index, message in enumerate(pending)
                    ],
                    # A node-id change mints a new canonical URI for the same
                    # short actor name.  Do not silently adopt the old identity:
                    # same owner/name does not prove machine-generation
                    # continuity.  Surface the stranded key so an operator can
                    # inspect and acknowledge it explicitly with the old URI.
                    "historicalInboxRecipients": historical_inbox_recipients,
                    "outboxCount": self._inbox.outbox_count(),
                    # Custody is ownership transfer, not replication (design G1):
                    # a message sits in exactly one of these two at a time, so
                    # reporting only the outbox made a transferred message look
                    # like a message that had vanished.
                    "custodyCount": self._inbox.custody_count(),
                    "mailboxes": list(self._presence.online_mailboxes()),
                }
        if method == "top.snapshot":
            now_ms = int(time.time() * 1000)
            # A batch path like ps: without the request snapshot every online
            # actor's liveness probe falls back to one supervisor round trip
            # plus one full desired-state load per connector
            # (_managed_worker_running).  In production that was ~475 SQLite
            # reads for 77 actors and `hyprial top` timed out at 15 s every
            # time while `ps`, which installs the snapshot, answered.
            with self._worker_status_snapshot():
                actor_statuses = self._actor_status_snapshot()
            result = build_top_snapshot(
                state_dir=self.state_dir,
                owner=self.owner,
                node_id=self.node_id,
                epoch=self.epoch,
                daemon_pid=os.getpid(),
                connectors=[],
                actor_statuses=actor_statuses,
                pending=self._pending_recipient_stats(),
                stranded=self._historical_inbox_recipients(),
                now_ms=now_ms,
                dispatch_without_pac_count=self._dispatch_without_pac_snapshot(),
                dispatch_conversation_count=self._dispatch_conversation_snapshot(),
            )
            result["quota"] = (
                self._usage_cache.snapshot_payload(now_ms)
                if self._usage_cache is not None
                else {"sources": []}
            )
            return result
        if method == "identity.whoami":
            actor = self._mcp_actor(params)
            session_ref = _required_string(params.get("sessionRef"), "sessionRef")
            channel_session = self._carrier_session(actor, session_ref)
            registered = (
                any(
                    item.actor == actor and item.session_ref == session_ref
                    for item in self._agent_session_domains.session.read_sessions()
                )
                or self._worker_session_ref(actor) == session_ref
            )
            current_epoch = (
                self._channel_current_epoch(actor, session_ref)
                if channel_session is not None
                else None
            )
            # Identity introspection is deliberately not fenced: a stale MCP
            # child asking "who am I" should get an answer that says so, not
            # a STALE_SESSION error.
            return {
                "ok": True,
                "actor": actor,
                "sessionRef": session_ref,
                "nodeId": self.node_id,
                "sessionRegistered": registered,
                "daemonEpoch": self.epoch,
                **(
                    {
                        "channelCurrentThisGeneration": (
                            current_epoch is not None
                        ),
                        "channelCurrentEpoch": current_epoch,
                    }
                    if channel_session is not None
                    else {}
                ),
            }
        if method == "org.publish":
            if self._org_endpoint is None:
                raise DaemonRequestError(
                    ipc_errors.DAEMON_NOT_READY, "org-context mesh endpoint is not ready"
                )
            return {"published": self._org_endpoint.publish_accepted()}
        if method == "org.fetch":
            if self._org_endpoint is None:
                raise DaemonRequestError(
                    ipc_errors.DAEMON_NOT_READY, "org-context mesh endpoint is not ready"
                )
            raw_timeout = params.get("timeoutSeconds", 2.0)
            if (
                isinstance(raw_timeout, bool)
                or not isinstance(raw_timeout, (int, float))
                or not 0 < float(raw_timeout) <= 30
            ):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "timeoutSeconds must be greater than 0 and at most 30",
                )
            if params.get("requestMode") == "neighbors":
                return self._org_endpoint.fetch(timeout=float(raw_timeout)).to_json()

            # Every daemon advertises its bare node id through the same actor
            # liveliness directory used by ``hyprial targets``.  Resident and
            # interactive agents advertise canonical ``agent:...`` URIs too,
            # but they do not own org/request queryables: their host daemon
            # does.  Offering those actor URIs as update sources would create
            # selectable targets that can never answer.
            #
            # Batch path like ps/targets (production 2026-09-15: enumerating
            # through ``online_actors`` paid the interactive-liveness verdict
            # for every actor on the mesh, one supervisor ``status()`` round
            # trip plus desired-state loads each, ~38 s for one call).
            # Identity filter first off the raw candidate set, then presence's
            # keep predicate on the host-kind survivors (byte-faithful,
            # including the worker-name/node-id collision case); the request
            # snapshot underneath makes both halves table lookups.  The
            # snapshot deliberately covers ONLY the enumeration (review
            # 2026-09-15, 低3): the source gate and the mesh fetch below are
            # plain computation and network I/O (up to 30 s) and hold no
            # verdict work, so the thread-local is released before them.
            with self._worker_status_snapshot():
                candidates = frozenset(
                    actor
                    for actor in self._presence.raw_online_actors()
                    if actor != self.node_id
                    and classify_target_identity(actor) == TARGET_KIND_HOST
                    and self._presence.liveness_keeps(actor)
                )
            raw_source = params.get("from")
            source = (
                None
                if raw_source is None
                else normalize_agent_recipient(_required_string(raw_source, "from"))
            )
            if source is not None and source not in candidates:
                raise DaemonRequestError(
                    ipc_errors.ORG_SOURCE_UNREACHABLE,
                    f"org-context source {raw_source!r} is not an online hyprial target",
                    {
                        # Honest display: candidates are presence actors —
                        # canonical URIs and bare node ids — listed
                        # verbatim, never re-wrapped as agent:<node>.
                        "availableSources": sorted(candidates)
                    },
                )
            return self._org_endpoint.fetch(
                source=source,
                allowed_sources=candidates,
                timeout=float(raw_timeout),
            ).to_json()
        if method == "targets":
            # targets is a batch path like ps: one request snapshot answers every
            # row's liveness verdict (production 2026-09-05: ps 0.2s, targets 15s).
            with self._worker_status_snapshot():
                now = time.monotonic()
                self._expire_stale_channel_routes(now)
                kind = params.get("kind")
                # targets answers "what can I deliver a message to?" (Allen's
                # definition).  Agents, users, and channel routes are the three
                # deliverable kinds this generation: user:<owner> lands in the
                # owner's squire DM with the sender attributed in the text, and
                # route:<adapter>:<route> posts to the configured chat with the
                # same 转述自 marker — both legs of the promise (deliverable,
                # and the receiver can tell who sent it) hold.  host never
                # appears here at all -- a node is announced on the network but
                # is not a delivery target; node visibility lives in the
                # "hosts" method below.
                if kind is not None and kind not in DELIVERABLE_TARGET_KINDS:
                    raise DaemonRequestError(
                        ipc_errors.INVALID_ARGUMENT,
                        "kind must be agent, user, or channel_route",
                    )
                rows: dict[str, JsonObject] = {}
                local_agents = {agent.uri: agent for agent in self.agents.list()}
                # Agent rows are a projection of the same canonical snapshot top
                # consumes. A registered but stopped actor remains visible as
                # offline; an interactive/MCP actor has processState=n/a in the
                # source snapshot instead of being invented as a stopped process.
                for actor_status in self._actor_status_snapshot():
                    actor = str(actor_status["actor"])
                    rows[actor] = {
                        "targetKind": TARGET_KIND_AGENT,
                        "targetUri": actor,
                        "actor": actor,
                        "status": actor_status["status"],
                        "deliverable": True,
                    }
                    agent = local_agents.get(actor)
                    if agent is not None:
                        rows[actor].update(
                            hosted=agent.hosted_by is not None,
                            hostedBy=agent.hosted_by,
                        )
                # Users: an owner with a completed squire profile (adapter +
                # open_id binding) is deliverable via receiver-owned DM; the
                # delivery text already carries the sender attribution marker.
                for profile in self.user_profiles.list():
                    if profile.squire_adapter is None or profile.owner_open_id is None:
                        continue
                    uri = f"user:{profile.owner}"
                    if uri in rows:
                        continue
                    rows[uri] = {
                        "targetKind": TARGET_KIND_USER,
                        "targetUri": uri,
                        "actor": uri,
                        "status": "configured",
                        "deliverable": True,
                    }
                # Channel routes: sender-local configured outbound routes.  The
                # sender-attribution marker is applied at post time, so a listed
                # route keeps both legs of the targets promise.
                configuration = self.load_persistent_configuration()
                for gateway_config in configuration.channels.gateways:
                    for route in gateway_config.routes:
                        uri = f"route:{gateway_config.name}:{route.name}"
                        if uri in rows:
                            continue
                        rows[uri] = {
                            "targetKind": TARGET_KIND_CHANNEL_ROUTE,
                            "targetUri": uri,
                            "actor": uri,
                            "status": "configured",
                            "deliverable": True,
                        }
                targets = [rows[uri] for uri in sorted(rows)]
                if kind is not None:
                    targets = [item for item in targets if item["targetKind"] == kind]
                return {"targets": targets}
        if method == "hosts":
            # Node visibility, deliberately separate from targets: a host is
            # announced on the network (its daemon's bare node_id liveliness
            # token) but accepts no actor messages.  This is the view for
            # "is that machine online?", not "what can I send to?".
            #
            # Host identity first: classify before any liveness so the mesh's
            # agents never reach a verdict, then re-check the host-kind
            # survivors with presence's own keep predicate -- inside the
            # snapshot that is pure table lookups, and it is what keeps the
            # output byte-identical to the old enumerate-then-filter shape
            # (production 2026-09-15: that old shape ran the per-actor
            # verdict for EVERY actor, ~38 s for one hosts call;
            # h2b-developer 批复①: the name-collision case -- a local worker
            # whose bare name equals a remote node id -- stays byte-faithful
            # too, so "output unchanged" no longer leans on "pathological
            # configs never happen").
            with self._worker_status_snapshot():
                nodes = sorted(
                    actor
                    for actor in self._presence.raw_online_actors()
                    if classify_target_identity(actor) == TARGET_KIND_HOST
                    and self._presence.liveness_keeps(actor)
                )
                return {
                    "hosts": [
                        {"nodeId": node, "status": "online"} for node in nodes
                    ]
                }
        if method == "session.register":
            actor = self._mcp_actor(params)
            session_ref = _required_string(params.get("sessionRef"), "sessionRef")
            source = _required_string(params.get("source"), "source")
            process_pid = _optional_positive_integer(
                params.get("processPid"), "processPid"
            )
            process_identity = _optional_string_param(
                params.get("processIdentity"), "processIdentity"
            )
            if (process_pid is None) != (process_identity is None):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "processPid and processIdentity must be provided together",
                )
            if process_pid is not None and source != "codex-app-server":
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "process identity is only valid for codex-app-server sessions",
                )
            command = params.get("command")
            if (
                not isinstance(command, list)
                or not command
                or any(not isinstance(item, str) or not item for item in command)
            ):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT, "command must be a non-empty array of strings"
                )
            session = InteractiveSession(
                actor=actor,
                cwd=_required_string(params.get("cwd"), "cwd"),
                command=tuple(command),
                source=source,
                session_ref=session_ref,
                runtime=str(params.get("runtime") or "claude_interactive"),
                channel_confirmed=params.get("channelConfirmed") is True,
                channel_build_version=_optional_channel_build_version(
                    params.get("channelBuildVersion"), "channelBuildVersion"
                ),
                channel_protocol_version=_optional_positive_integer(
                    params.get("channelProtocolVersion"), "channelProtocolVersion"
                ),
                owner_fence=_optional_boolean(params.get("ownerFence"), "ownerFence"),
                channel_lease_digest=_channel_lease_digest_for_registration(
                    source=_required_string(params.get("source"), "source"),
                    protocol_version=_optional_positive_integer(
                        params.get("channelProtocolVersion"),
                        "channelProtocolVersion",
                    ),
                    token=params.get("channelLeaseToken"),
                ),
                tmux_session=_optional_string_param(
                    params.get("tmuxSession"), "tmuxSession"
                ),
                process_pid=process_pid,
                process_identity=process_identity,
            )
            harness = _session_harness(session)
            self._refuse_if_running(
                actor,
                harness=harness,
                runtime=RUNTIME_INTERACTIVE,
                refuse_runtimes=(RUNTIME_HEADLESS,),
            )
            from hyprial.agents.capabilities import option_value

            existing_agent = self.agents.get(actor)
            same_harness = existing_agent is not None and (
                existing_agent.capabilities.get("harness", existing_agent.preferred_harness) == harness
            )
            agent = self._ensure_agent(
                actor,
                harness=harness,
                interactive=True,
                cwd=session.cwd,
                provider=option_value(session.command, "--provider") or (
                    existing_agent.provider if same_harness else None
                ),
                model=option_value(session.command, "--model") or (
                    existing_agent.model if same_harness else None
                ),
            )
            handover = (
                HandoverNotice(
                    actor=agent.actor,
                    previous_harness=agent.last_harness,
                    previous_session_id=agent.last_session_id,
                    next_harness=harness,
                )
                if agent is not None
                and agent.last_harness is not None
                and agent.last_harness != harness
                else None
            )
            with self._interactive_route_lock:
                prior_sessions = tuple(
                    item
                    for item in self._agent_session_domains.session.read_sessions()
                    if item.actor == actor or item.session_ref == session_ref
                )
                route_created = self._ensure_interactive_route(actor, session_ref)
                try:
                    completed = self._call_session(
                        RegisterSessionCommand(
                            correlation_id=f"session:register:{uuid4().hex}",
                            actor=actor,
                            cwd=session.cwd,
                            command=session.command,
                            source=session.source,
                            session_ref=session_ref,
                            runtime=session.runtime or "claude_interactive",
                            channel_confirmed=session.channel_confirmed,
                            channel_build_version=session.channel_build_version,
                            channel_protocol_version=session.channel_protocol_version,
                            owner_fence=session.owner_fence,
                            channel_lease_token=params.get("channelLeaseToken")
                            if isinstance(params.get("channelLeaseToken"), str)
                            else None,
                            tmux_session=session.tmux_session,
                            process_pid=session.process_pid,
                            process_identity=session.process_identity,
                            manage_agent=agent is not None,
                        )
                    )
                except BaseException:
                    if route_created:
                        self._close_interactive_route(actor, session_ref)
                    raise
                for prior in prior_sessions:
                    if (prior.actor, prior.session_ref) != (actor, session_ref):
                        self._close_interactive_route(
                            prior.actor, prior.session_ref
                        )
            result = completed.result.to_payload()
            return {
                **result,
                **(
                    {"harnessHandover": handover.to_json()}
                    if handover is not None
                    else {}
                ),
            }
        if method == "session.refresh":
            actor = self._mcp_actor(params)
            session_ref = _required_string(params.get("sessionRef"), "sessionRef")
            with self._interactive_route_lock:
                completed = self._call_session(
                    RefreshSessionCommand(
                        correlation_id=f"session:refresh:{uuid4().hex}",
                        actor=actor,
                        session_ref=session_ref,
                        channel_lease_token=(
                            params.get("channelLeaseToken")
                            if isinstance(params.get("channelLeaseToken"), str)
                            else None
                        ),
                        manage_agent=self.agents.get(actor) is not None,
                    )
                )
                # The result carries the canonical actor: after an owner-only
                # relocation the route is ensured under the migrated spelling,
                # never the caller's stale one.
                self._ensure_interactive_route(
                    completed.result.actor, session_ref
                )
            return completed.result.to_payload()
        if method == "session.heartbeat":
            actor = self._mcp_actor(params)
            session_ref = _required_string(params.get("sessionRef"), "sessionRef")
            with self._interactive_route_lock:
                completed = self._call_session(
                    HeartbeatSessionCommand(
                        correlation_id=f"session:heartbeat:{uuid4().hex}",
                        actor=actor,
                        session_ref=session_ref,
                        channel_lease_token=(
                            params.get("channelLeaseToken")
                            if isinstance(params.get("channelLeaseToken"), str)
                            else None
                        ),
                        manage_agent=self.agents.get(actor) is not None,
                    )
                )
                # Canonical actor, same rule as session.refresh.
                self._ensure_interactive_route(
                    completed.result.actor, session_ref
                )
            return completed.result.to_payload()
        if method == "session.unregister":
            actor = self._mcp_actor(params)
            session_ref = _required_string(params.get("sessionRef"), "sessionRef")
            with self._interactive_route_lock:
                completed = self._call_session(
                    UnregisterSessionCommand(
                        correlation_id=f"session:unregister:{uuid4().hex}",
                        actor=actor,
                        session_ref=session_ref,
                        manage_agent=self.agents.get(actor) is not None,
                    )
                )
                if completed.result.unregistered:
                    self._close_interactive_route(actor, session_ref)
            return completed.result.to_payload()
        if method == "message.query":
            return self._message_query(params)
        if method == "message.pending.list":
            actor = self._message_consumer_actor(params)
            if is_session_fetch(params) and "sessionRef" not in params:
                # The explicit-fetch marker belongs to the session-carrier
                # contract: it commits custody (fetched stamp, sender-outbox
                # retirement, fetch receipt). A caller that signs no
                # sessionRef owns no custody, so an unfenced fetch must be
                # rejected BEFORE anything is committed -- the row stays
                # pending for whichever session actually holds the actor.
                raise DaemonRequestError(
                    ipc_errors.STALE_SESSION,
                    "an explicit inbox fetch (fetched=true) requires the "
                    "session fence (sessionRef); an unfenced caller cannot "
                    "commit another session's fetch",
                )
            actor = self._fence_interactive_session(actor, params)
            keys = self._message_consumer_keys(params)
            messages: list[JsonObject] = []
            drain_notices = getattr(self._inbox, "drain_system_notices", None)
            notices = tuple(
                notice
                for key in keys
                for notice in (drain_notices(key) if callable(drain_notices) else ())
            )
            fetch_pending = getattr(self._inbox, "fetch_pending", None)
            if is_session_fetch(params) and callable(fetch_pending):
                pending = fetch_pending(actor)
            else:
                seen: set[str] = set()
                pending_list: list[Any] = []
                for key in keys:
                    for item in self._inbox.pending_messages(key):
                        if item.message_id not in seen:
                            seen.add(item.message_id)
                            pending_list.append(item)
                pending = tuple(pending_list)
            if is_session_fetch(params) and self._transport is not None:
                for message in pending:
                    publish_fetch_receipt(self._transport, message)
            for message in (*notices, *pending):
                try:
                    body = json.loads(message.payload)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = {}
                text = body.get("message", "") if isinstance(body, dict) else ""
                row: JsonObject = {
                    "messageId": message.message_id,
                    # Stable across daemon restart/resume; never use queue index.
                    "deliveryId": message.message_id,
                    "conversationId": message.conversation_id,
                    "from": message.sender,
                    # Every delivered row names both ends.  ``to`` matters when
                    # one reader drains several keys (aliases, a session that
                    # holds more than one actor): without it the reader cannot
                    # tell which of its identities was addressed.
                    "to": message.recipient,
                    "intent": message.intent,
                    "message": text if isinstance(text, str) else str(text),
                }
                origin = body.get("origin") if isinstance(body, dict) else None
                if isinstance(origin, dict) and origin:
                    # Present only when the reporting adapter sent one.  A
                    # reader must treat the absent key as "unknown", not as a
                    # direct message -- those are different answers and only one
                    # of them is safe to act on.
                    row["origin"] = origin
                messages.append(row)
            return {"ok": True, "messages": messages, "daemonEpoch": self.epoch}
        if method.startswith("routine."):
            routine_caller = self._workflow_caller(params)
            if method in ("routine.remove", "routine.pause", "routine.resume", "routine.status") and self._routine_service is not None:
                current = self._routine_service.status(name=_required_string(params.get("name"), "name"))
                if routine_caller not in (f"user:{self.owner}", current["owner"], current.get("actor")):
                    raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "caller does not own this routine")
        if method == "routine.add":
            if self._routine_service is None:
                raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
            yaml_text = _required_string(params.get("yaml"), "yaml")
            import yaml
            from hyprial.routine.schema import RoutineSchemaError, load_routine_text
            try:
                spec = load_routine_text(yaml_text)
            except RoutineSchemaError as error:
                raise DaemonRequestError("ROUTINE_SCHEMA_ERROR", str(error)) from error
            source = self._workflow_caller(params)
            if spec.actor is not None:
                if self._remote_workflow is not None and self._remote_workflow.remote(spec.actor):
                    from types import SimpleNamespace
                    from hyprial.pac.errors import PacError
                    try:
                        self._remote_workflow.admit(SimpleNamespace(owner=spec.actor, role=spec.role,
                            first_output_eta=None, human_gates=None))
                    except PacError as error:
                        raise DaemonRequestError(error.code, str(error)) from error
                else:
                    self._resolve_send_sender(spec.actor)
            elif spec.produces is None:
                document = yaml.safe_load(yaml_text)
                from hashlib import sha256
                actor_name = f"routine-{spec.name}" if len(spec.name) <= 48 else "routine-" + sha256(spec.name.encode()).hexdigest()[:16]
                document["produces"] = canonical_agent_uri(self.owner, self.node_id, actor_name)
                yaml_text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
                spec = load_routine_text(yaml_text)
            with self._routine_coordinator_lock:
                if spec.produces is not None and any(
                    item.get("produces") == spec.produces
                    for item in self._routine_service.list()["routines"]
                ):
                    raise DaemonRequestError("ROUTINE_COORDINATOR_CONFLICT",
                                             f"coordinator already owned: {spec.produces}")
                try:
                    result = self._routine_service.add(yaml_text=yaml_text, owner=source, enabled=False)
                except RoutineServiceError as error:
                    raise DaemonRequestError(error.code, str(error)) from error
                routine = {**self._routine_service.status(name=spec.name), "name": spec.name}
                try:
                    coordinator = self._ensure_routine_coordinator(routine, recovering=False)
                    self._routine_service.resume(name=spec.name, align_schedule=True)
                    result["enabled"] = True
                except Exception:
                    self._routine_service.remove(name=spec.name)
                    raise
                return {**result, **({"coordinator": coordinator} if coordinator is not None else {})}
        if method == "routine.list":
            if self._routine_service is None:
                raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
            return self._routine_service.list()
        if method == "routine.status":
            if self._routine_service is None:
                raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
            name = _required_string(params.get("name"), "name")
            try:
                return self._routine_service.status(name=name)
            except RoutineServiceError as error:
                raise DaemonRequestError(error.code, str(error)) from error
        if method == "routine.audit":
            if self._routine_service is None:
                raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
            # Doctor surface (approved Q1): quarantined routines and the
            # stored-spec address-migration ledger, both machine-readable.
            quarantined = [
                item
                for item in self._routine_service.list()["routines"]
                if isinstance(item, dict) and item.get("quarantined") is True
            ]
            return {
                "quarantined": quarantined,
                "addressMigrations": self._routine_service.address_migrations(),
            }
        if method == "routine.remove":
            if self._routine_service is None:
                raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
            name = _required_string(params.get("name"), "name")
            with self._routine_coordinator_lock:
                try:
                    routine = {**self._routine_service.status(name=name), "name": name}
                    if routine.get("enabled") is True:
                        self._routine_service.pause(name=name)
                    from hyprial.pac.graph import close_graph
                    from hyprial.pac.store import PacGraphStore, default_database_path
                    store = PacGraphStore(default_database_path(self.state_dir))
                    try:
                        for task in routine.get("inFlight", []):
                            graph_id = task["runId"]
                            graph = store.graph(graph_id)
                            if graph is not None:
                                close_graph(store, graph_id, actor=str(routine["owner"]))
                    finally:
                        store.close()
                    coordinator = self._retire_routine_coordinator(routine)
                    result = self._routine_service.remove(name=name)
                    return {**result, **({"coordinator": coordinator} if coordinator is not None else {})}
                except RoutineServiceError as error:
                    raise DaemonRequestError(error.code, str(error)) from error
        if method == "routine.pause":
            if self._routine_service is None:
                raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
            name = _required_string(params.get("name"), "name")
            try:
                return self._routine_service.pause(name=name)
            except RoutineServiceError as error:
                raise DaemonRequestError(error.code, str(error)) from error
        if method == "routine.resume":
            if self._routine_service is None:
                raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
            name = _required_string(params.get("name"), "name")
            try:
                return self._routine_service.resume(name=name)
            except RoutineServiceError as error:
                raise DaemonRequestError(error.code, str(error)) from error
        if method == "dispatch.matrix.resolve":
            from hyprial.dispatch.matrix import TIERS, resolve

            tier = _required_string(params.get("tier"), "tier")
            if tier not in TIERS:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "tier must be fast, strong, or super")
            name = _required_string(params.get("name"), "name")
            # Static selection: no profile mark is read and no liveness probe
            # runs, so this cannot fail on availability and no longer needs a
            # composed probe deadline on the caller side (2026-09-21).
            choice = resolve(tier)
            payload = choice.to_json()
            self._log("info", "daemon", "dispatch.matrix.resolved", agentName=name, ok=True, **payload)
            return {"ok": True, **payload}
        if method == "workflow.remote.current":
            from hyprial.pac.errors import PacError
            if self._remote_workflow is None:
                raise DaemonRequestError("WORKFLOW_REMOTE_UNAVAILABLE", "remote workflow service is not running")
            try:
                return self._remote_workflow.delivery_current(_required_string(params.get("messageId"), "messageId"))
            except PacError as error:
                raise DaemonRequestError(error.code, str(error)) from error
        if method in ("workflow.worker.stop", "workflow.worker.restart"):
            if self._workflow_service is None:
                raise DaemonRequestError(
                    ipc_errors.WORKFLOW_UNAVAILABLE,
                    "workflow service is not running",
                )
            graph_id = _required_string(params.get("graphId"), "graphId")
            actor_name = _required_string(params.get("actorName"), "actorName")
            caller = self._workflow_caller(params)
            try:
                operation = (
                    self._workflow_service.stop_worker
                    if method.endswith("stop")
                    else self._workflow_service.restart_worker
                )
                return operation(graph_id=graph_id, actor_name=actor_name, actor=caller)
            except WorkflowServiceError as error:
                raise DaemonRequestError(error.code, str(error)) from error
        if method == "workflow.start":
            if self._workflow_service is None:
                raise DaemonRequestError(ipc_errors.WORKFLOW_UNAVAILABLE, "workflow service is not running")
            yaml_text = _required_string(params.get("yaml"), "yaml")
            source = self._workflow_caller(params)
            try:
                return self._workflow_service.start(
                    yaml_text=yaml_text, sender=source,
                    operation_key=params.get("operationKey"),
                )
            except WorkflowServiceError as error:
                raise DaemonRequestError(error.code, str(error)) from error
        if method in ("workflow.complete", "workflow.fail"):
            if self._workflow_service is None:
                raise DaemonRequestError(ipc_errors.WORKFLOW_UNAVAILABLE, "workflow service is not running")
            source = self._workflow_caller(params)
            graph_id = _required_string(params.get("graphId"), "graphId")
            node_id = _required_string(params.get("nodeId"), "nodeId")
            request = _required_string(params.get("requestId"), "requestId")
            reason = _required_string(params.get("reasonRef"), "reasonRef")
            from hyprial.pac.errors import PacError
            try:
                if self._remote_workflow is not None:
                    forwarded = self._remote_workflow.forward(method, params, source)
                    if forwarded is not None:
                        return forwarded
                if method == "workflow.fail":
                    return self._workflow_service.fail(graph_id=graph_id, node_id=node_id,
                                                       actor=source, request_id=request, reason_ref=reason)
                from hyprial.pac.reactor import PacReactor
                from hyprial.pac.store import PacGraphStore, default_database_path
                store = PacGraphStore(default_database_path(self.state_dir))
                try:
                    outcome = PacReactor(store).set_flag(graph_id, node_id, actor=source,
                                                         reason_ref=reason, expected_request=request)
                finally:
                    store.close()
                self._workflow_service.submit_timer(time.time_ns() // 1_000_000)
                return {"ok": True, "event": outcome.event}
            except (WorkflowServiceError, PacError) as error:
                raise DaemonRequestError(error.code, str(error)) from error
        if method in (
            "pac.flag.set",
            "pac.flag.reset",
            "pac.graph.activate",
            "pac.graph.close",
            "pac.actor.stop",
        ):
            return self._handle_pac_write(method, params)
        if method == "agent.task.capabilities":
            service = self._require_agent_task_service()
            try:
                validate_agent_task_capabilities(_agent_task_body(params))
                return service.agent_task_capabilities()
            except AgentTaskError as error:
                raise DaemonRequestError(error.code, str(error), error.data) from error
        if method == "agent.task.start":
            service = self._require_agent_task_service()
            caller = self._agent_task_bound_caller(params)
            try:
                request = validate_agent_task_start(_agent_task_body(params))
                return service.agent_task_start(request=request, caller=caller)
            except (AgentTaskError, PacAgentTaskError) as error:
                raise DaemonRequestError(error.code, str(error), error.data) from error
        if method == "agent.task.status":
            service = self._require_agent_task_service()
            self._agent_task_bound_caller(params)
            try:
                run_id = validate_agent_task_status(_agent_task_body(params))
                return service.agent_task_status(run_id=run_id)
            except (AgentTaskError, PacAgentTaskError) as error:
                raise DaemonRequestError(error.code, str(error), error.data) from error
        if method == "agent.task.result":
            service = self._require_agent_task_service()
            self._agent_task_bound_caller(params)
            try:
                run_id, target_ref = validate_agent_task_result(
                    _agent_task_body(params)
                )
                return service.agent_task_result(
                    run_id=run_id, target_ref=target_ref
                )
            except (AgentTaskError, PacAgentTaskError) as error:
                raise DaemonRequestError(error.code, str(error), error.data) from error
        if method == "agent.task.cancel":
            service = self._require_agent_task_service()
            caller = self._agent_task_bound_caller(params)
            try:
                run_id, reason = validate_agent_task_cancel(_agent_task_body(params))
                return service.agent_task_cancel(
                    run_id=run_id, caller=caller, reason=reason
                )
            except (AgentTaskError, PacAgentTaskError) as error:
                raise DaemonRequestError(error.code, str(error), error.data) from error
        if method == "agent.task.observe":
            service = self._require_agent_task_service()
            submitter = self._agent_task_bound_caller(params)
            try:
                activity = validate_agent_task_activity(_agent_task_body(params))
                return service.agent_task_observe(
                    activity=activity,
                    submitter=submitter,
                    message_id=activity.event_id,
                )
            except (AgentTaskError, PacAgentTaskError) as error:
                raise DaemonRequestError(error.code, str(error), error.data) from error
        if method in ("workflow.status", "workflow.list", "workflow.node.inspect", "workflow.cancel", "workflow.history.list", "workflow.history.status"):
            caller = self._workflow_caller(params)
            from hyprial.pac.legacy_workflows import LegacyWorkflowHistory
            from hyprial.pac.errors import PacError
            try:
                if method.startswith("workflow.history."):
                    history = LegacyWorkflowHistory(self.state_dir)
                    viewer = None if caller == f"user:{self.owner}" else caller
                    if method.endswith("list"):
                        return history.list(limit=int(params.get("limit", 50)), viewer=viewer)
                    return history.status(_required_string(params.get("runId"), "runId"), viewer=viewer)
                if self._workflow_service is None:
                    raise DaemonRequestError(ipc_errors.WORKFLOW_UNAVAILABLE, "workflow service is not running")
                if method == "workflow.list":
                    return self._workflow_service.list(limit=int(params.get("limit", 50)),
                        viewer=None if caller == f"user:{self.owner}" else caller)
                run_id = _required_string(params.get("runId"), "runId")
                if method == "workflow.cancel":
                    return self._workflow_service.cancel(run_id=run_id, actor=caller)
                if method == "workflow.node.inspect" and self._remote_workflow is not None:
                    forwarded = self._remote_workflow.forward(method, params, caller)
                    if forwarded is not None:
                        return forwarded
                result = self._workflow_service.status(run_id=run_id)
                if caller != f"user:{self.owner}" and caller != result["sender"] and not any(n["owner"] == caller for n in result["nodes"]):
                    raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "caller is not a participant of this graph")
                if method == "workflow.node.inspect":
                    target = _required_string(params.get("target"), "target")
                    node = next((n for n in result["nodes"] if n["nodeId"] == target), None)
                    if node is None:
                        raise DaemonRequestError("WORKFLOW_NODE_NOT_FOUND", target)
                    if caller not in (f"user:{self.owner}", result["sender"], node["owner"]):
                        raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "only the creator or node owner may read execution progress")
                    from hyprial.dispatch.workflow_observation import observe_node
                    return observe_node(self._workflow_service.database, result, node, self._inbox,
                                        recipient=self._dispatch_service_actor,
                                        at=time.time_ns() // 1_000_000, epoch=self.epoch)
                return result
            except (WorkflowServiceError, PacError) as error:
                raise DaemonRequestError(error.code, str(error)) from error
        if method == "progress.list":
            actor = self._message_consumer_actor(params)
            actor = self._fence_interactive_session(actor, params)
            keys = self._message_consumer_keys(params)
            delivery_id = _optional_string_param(params.get("deliveryId"), "deliveryId")
            since_seq = params.get("sinceSeq")
            if since_seq is not None and (
                isinstance(since_seq, bool)
                or not isinstance(since_seq, int)
                or since_seq < 0
            ):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT, "sinceSeq must be a non-negative integer"
                )
            list_progress = getattr(self._inbox, "list_progress_events", None)
            if not callable(list_progress):
                return {"ok": True, "events": [], "daemonEpoch": self.epoch}
            seen: set[str] = set()
            events: list[JsonObject] = []
            for key in keys:
                for message in list_progress(key, delivery_id=delivery_id):
                    if message.message_id in seen:
                        continue
                    seen.add(message.message_id)
                    event = decode_progress_event(message.payload)
                    if event is None:
                        continue
                    if since_seq is not None and event.seq <= since_seq:
                        continue
                    record: JsonObject = {
                        "messageId": message.message_id,
                        "from": message.sender,
                        "recipient": message.recipient,
                        "intent": message.intent,
                        "createdAtMs": message.created_at_ms,
                        **event.to_payload_dict(),
                    }
                    events.append(record)
            return {"ok": True, "events": events, "daemonEpoch": self.epoch}
        if method == "message.send":
            source = _actor(params)
            if "sessionRef" in params:
                source = self._canonical_interactive_actor(source)
            else:
                # Allen A(b): the sender resolves at the daemon send
                # boundary, before anything reaches the wire — CLI, MCP
                # client, and worker all converge here.  Resolve-or-reject:
                # no registry row, no send, and nothing is ever minted, so
                # no legitimate path can author a bare-sender message —
                # which is what allows the inbox ingress gate to reject them.
                source = self._resolve_send_sender(source)
            source = self._fence_interactive_session(source, params)
            targets = params.get("to")
            if not isinstance(targets, list) or not targets:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT, "to must be a non-empty array"
                )
            requested_targets = tuple(
                _required_string(target, "to item") for target in targets
            )
            raw_resources = params.get("resourcePaths")
            if isinstance(raw_resources, list) and raw_resources:
                if params.get("replyTo") is not None:
                    raise DaemonRequestError(
                        ipc_errors.ROUTE_RESOURCE_REPLY_UNSUPPORTED,
                        "attachments on replyTo/harness_reply paths are not "
                        "implemented; no text or attachment was sent",
                    )
                unsupported = [
                    target
                    for target in requested_targets
                    if not is_route_target(target)
                ]
                if unsupported:
                    raise DaemonRequestError(
                        ipc_errors.ROUTE_RESOURCES_UNSUPPORTED,
                        "attachments are supported only for "
                        "route:<adapter>:<route> targets in this build; no "
                        "message was queued or sent",
                        {"unsupportedTargets": unsupported},
                    )
            try:
                resources = parse_route_resources(raw_resources)
            except RouteDeliveryError as error:
                raise DaemonRequestError(error.code, str(error), error.data) from error
            text = _required_string(params.get("message"), "message")
            metadata = params.get("providerMetadata")
            sender_open_id = (
                metadata.get("senderId")
                if isinstance(metadata, dict)
                and isinstance(metadata.get("senderId"), str)
                else None
            )
            # Where this message came from, in the reporting adapter's words.  The
            # adapter already knows and says so -- Lark parses ``chat_type``
            # off the event and sends it in "providerMetadata" -- but only
            # ``senderId`` was ever read, so the value arrived and was dropped
            # before storage.  A reader could not then tell a group from a
            # direct message: Feishu uses the same ``oc_`` prefix for both, so
            # nothing else in the row distinguishes them.
            origin = _message_origin(metadata)
            conversation = str(params.get("conversationId") or uuid4())
            operation_id = str(params.get("idempotencyKey") or uuid4())
            # Gate condition (a) (spec-dispatch-gate-classifier-2026-09-04):
            # only a send that OPENS its conversation can dispatch; later
            # sends under the same conversationId are answers/updates inside
            # it.  Keyed by the establishing operation id so an idempotent
            # replay of the opening send still counts as opening, while a
            # different send reusing the id does not.
            with self._dispatch_without_pac_lock:
                establisher = self._dispatch_gate_conversations.get(conversation)
                conversation_is_new = (
                    establisher is None or establisher == operation_id
                )
                if establisher is None:
                    self._dispatch_gate_conversations[conversation] = operation_id
            deliveries: list[JsonObject] = []
            for index, target_value in enumerate(requested_targets):
                target = self._resolve_agent_alias(
                    normalize_agent_recipient(target_value)
                )
                _require_deliverable_send_target(target)
                if is_user_target(target):
                    target_key = f"{operation_id}:{target}"
                    message_id = str(
                        uuid5(NAMESPACE_URL, f"hyprial:send:{target_key}:{index}")
                    )
                    try:
                        user_target = UserDeliveryTarget.parse(target)
                    except ValueError as error:
                        raise DaemonRequestError(
                            ipc_errors.INVALID_ARGUMENT, str(error)
                        ) from error
                    if self._user_delivery is None:
                        raise DaemonRequestError(
                            ipc_errors.USER_DELIVERY_UNAVAILABLE,
                            "user delivery transport is unavailable",
                        )
                    outcome = self._user_delivery.deliver(
                        UserDeliveryRequest(
                            message_id=message_id,
                            idempotency_key=target_key,
                            owner=user_target.owner,
                            sender=source,
                            message=text,
                            conversation_id=conversation,
                        )
                    )
                    if not outcome.accepted:
                        raise DaemonRequestError(
                            outcome.code or ipc_errors.USER_DELIVERY_FAILED,
                            outcome.message or "user delivery failed",
                            {"target": target, "messageId": outcome.message_id},
                        )
                    deliveries.append(
                        {
                            "target": target,
                            "messageId": outcome.message_id,
                            "accepted": True,
                            "queued": False,
                            "receiverAdapter": True,
                            **(
                                {"nativeMessageId": outcome.native_message_id}
                                if outcome.native_message_id is not None
                                else {}
                            ),
                            **({"duplicate": True} if outcome.duplicate else {}),
                        }
                    )
                    self._log(
                        "info",
                        "daemon",
                        "send.received",
                        messageId=outcome.message_id,
                        correlationId=outcome.message_id,
                        node="daemon-send",
                        conversationId=conversation,
                        sender=source,
                        target=target,
                        **(
                            {"nativeMessageId": outcome.native_message_id}
                            if outcome.native_message_id is not None
                            else {}
                        ),
                    )
                    continue
                if is_route_target(target):
                    deliveries.extend(
                        self._deliver_route_target(
                            target,
                            text=text,
                            sender=source,
                            conversation=conversation,
                            operation_id=operation_id,
                            index=index,
                            resources=resources,
                        )
                    )
                    continue
                if classify_target_identity(target) != TARGET_KIND_AGENT:
                    # Unreachable: _require_deliverable_send_target has
                    # already rejected every non-deliverable shape.  Kept as
                    # a fail-closed guard in case a future address form is
                    # added to the classifier without a delivery branch here.
                    raise DaemonRequestError(
                        ipc_errors.UNSUPPORTED_TARGET,
                        f"target {target!r} uses an address scheme this build "
                        "cannot deliver; deliverable forms are "
                        "agent:<owner>:<machine>:<agent>, user:<owner>, "
                        "and route:<adapter>:<route>",
                        {"target": target},
                    )
                # Agent deliveries must survive the proxy's reconnect retry:
                # StatelessDaemonProxy replays the same operation id after an
                # "accepted, then the socket closed" ambiguity, expecting the
                # daemon to deduplicate.  User and route targets already derive
                # an idempotent id from the operation; agent targets minted a
                # fresh uuid4 with no idempotency key, so a replayed send
                # enqueued a second distinct probe.  The delivery pump then
                # dispatched both and the auto-reply path answered each — the
                # observed "two replies to one probe".  Derive the id the same
                # way so a replay dedups at the recipient instead.
                agent_key = f"{operation_id}:{target}:{index}"
                entity = self.agents.get(target)
                dispatch_gate(
                    target=target,
                    capabilities=entity.capabilities if entity is not None else {},
                    role=params.get("role"),
                    first_output_eta=_optional_string_param(params.get("first_output_eta"), "first_output_eta"),
                    accepted_text=text,
                    human_gates_declared="human_gates" in params,
                    emit=self._log,
                    source="message.send",
                )
                message = InboxMessage(
                    message_id=str(uuid5(NAMESPACE_URL, f"hyprial:send:{agent_key}")),
                    conversation_id=conversation,
                    sender=source,
                    recipient=target,
                    payload=json.dumps(
                        {
                            "message": text,
                            "topic": params.get("topic"),
                            # Absent when the adapter reports no origin: rows
                            # written before this change and messages from an
                            # adapter with no such concept simply lack the key.
                            **({"origin": origin} if origin is not None else {}),
                        },
                        separators=(",", ":"),
                    ).encode(),
                    intent="reply" if params.get("replyTo") else "request",
                    lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                    idempotency_key=agent_key,
                    created_at_ms=time.time_ns() // 1_000_000,
                )
                self._log(
                    "info",
                    "daemon",
                    "send.received",
                    messageId=message.message_id,
                    correlationId=message.message_id,
                    node="daemon-send",
                    conversationId=conversation,
                    sender=source,
                    target=target,
                    **(
                        {"senderOpenId": sender_open_id}
                        if sender_open_id is not None
                        else {}
                    ),
                )
                result = self._inbox.submit(message)
                # A3 dispatch gate (design-dispatch-always-pac §三②,
                # narrowed by spec-dispatch-gate-classifier-2026-09-04):
                # classify an accepted coordinator→worker send — a dispatch
                # (counted) or a conversation (observable, uncounted).
                # Record-only by spec (先记不拦): nothing here blocks or
                # alters the delivery that just happened.  Only an accepted
                # submission dispatched anything; refusals count nothing.
                if (
                    result.accepted
                    and self._is_dispatch_candidate(
                        sender=source, recipient=target, intent=message.intent
                    )
                ):
                    self._classify_dispatch_send(
                        message, conversation_is_new=conversation_is_new
                    )
                unresolved = result.queued and self._is_unresolved_local_identity(
                    target
                )
                if unresolved:
                    # The incident shape: a canonical URI naming THIS node
                    # that nothing here claims can never leave the durable
                    # queue.  Say so on the wire and in the log instead of
                    # the historic accepted/queued silence.
                    self._log(
                        "warn",
                        "daemon",
                        "send.target_unresolved",
                        messageId=message.message_id,
                        correlationId=message.message_id,
                        node="daemon-send",
                        conversationId=conversation,
                        sender=source,
                        target=target,
                    )
                code = result.code or (
                    "TARGET_UNRESOLVED" if unresolved else None
                )
                deliveries.append(
                    {
                        "target": target,
                        "messageId": message.message_id,
                        "accepted": result.accepted,
                        "queued": result.queued,
                        **({"code": code} if code else {}),
                        # queued=True means neither direct delivery nor a
                        # custody mailbox accepted the message; surface why
                        # instead of letting ok:true mask a silent deferral.
                        **(
                            {"reason": "target-not-visible"}
                            if result.queued
                            else {}
                        ),
                    }
                )
            return {
                "ok": all(item["accepted"] for item in deliveries),
                "operationId": operation_id,
                "conversationId": conversation,
                "deliveries": deliveries,
                **(
                    {"replyPathUnavailable": True}
                    if self._reply_path_unavailable(source)
                    else {}
                ),
                **(
                    {"messageId": deliveries[0]["messageId"]}
                    if len(deliveries) == 1
                    else {}
                ),
            }
        if method == "message.reply":
            # A reply AUTHORS a message: its sender must be as backed as a
            # send sender (resolve-or-reject, Allen b).  Reads stay
            # non-raising; writes require a registry row.
            actor = (
                self._mcp_actor(params)
                if "sessionRef" in params
                else self._resolve_send_sender(_actor(params))
            )
            actor = self._fence_interactive_session(actor, params)
            raw_resources = params.get("resourcePaths")
            if raw_resources is not None and (
                not isinstance(raw_resources, list) or raw_resources
            ):
                raise DaemonRequestError(
                    ipc_errors.MESSAGE_REPLY_RESOURCES_UNSUPPORTED,
                    "harness_reply attachments are not implemented; the "
                    "original pending message was not acknowledged and no "
                    "reply was queued",
                )
            message_id = _required_string(params.get("messageId"), "messageId")
            text = _required_string(params.get("message"), "message")
            original_key = actor
            original = None
            for key in self._message_consumer_keys(params):
                original = next(
                    (
                        item
                        for item in self._inbox.pending_messages(key)
                        if item.message_id == message_id
                    ),
                    None,
                )
                if original is not None:
                    original_key = key
                    break
            if original is None:
                raise DaemonRequestError(
                    ipc_errors.MESSAGE_REPLY_UNAVAILABLE, "pending message was not found"
                )
            adapter_name = lark_reply_adapter(original.sender)
            if (
                original.sender.startswith((CHANNEL_URI_PREFIX, ADAPTER_URI_PREFIX))
                and adapter_name is None
            ):
                raise DaemonRequestError(
                    ipc_errors.MESSAGE_REPLY_UNAVAILABLE,
                    "the pending message came from a channel actor without a "
                    "reply bridge",
                )
            if (
                adapter_name is not None
                and self._adapters is not None
                and adapter_name not in self._lark_gateway_names()
            ):
                raise DaemonRequestError(
                    ipc_errors.MESSAGE_REPLY_UNAVAILABLE,
                    f"Lark reply adapter is not configured: {adapter_name}",
                )
            reply_body: JsonObject = {"message": text}
            if adapter_name is not None:
                reply_body["replyTo"] = message_id
            # A dispatcher that sent with a bare --from leaves a bare sender
            # on the pending message; queued under that spelling the reply is
            # an outbox island no mailbox drains (it expires silently). Resolve
            # the recipient with the same pipeline message.send applies to its
            # targets; channel:/user: and other scheme-carrying senders pass
            # through untouched, and an ambiguous alias fails loudly here.
            recipient = self._resolve_agent_alias(
                normalize_agent_recipient(original.sender)
            )
            # Reply recipients that are node addresses hit the same silent
            # hole message.send now rejects; refuse loudly and leave the
            # original message pending instead.  channel:/user: senders pass
            # through: their bridges own delivery.
            if classify_target_identity(recipient) == TARGET_KIND_HOST:
                raise DaemonRequestError(
                    ipc_errors.TARGET_IS_NODE,
                    f"reply recipient {recipient!r} is a node address, not "
                    "an agent: nodes are announced on the network but do "
                    "not receive actor messages",
                    {"target": recipient},
                )
            # No TARGET_UNRESOLVED refusal here anymore: a canonical URI
            # whose machine segment is this node is delivered into the
            # local durable inbox by the local-first transport whether or
            # not a connector currently claims it, and the read side
            # canonicalizes the same way — the row is drainable, so the
            # refusal would be a false rejection.
            reply = InboxMessage(
                # Retrying the same harness_reply must address the same outbox
                # row.  Old rows use the same reply:<inbound-id> key but had a
                # random message id; the transport accepts both shapes.
                message_id=reply_message_id(message_id),
                conversation_id=original.conversation_id,
                sender=actor,
                recipient=recipient,
                payload=json.dumps(reply_body, separators=(",", ":")).encode(),
                intent="reply",
                lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
                idempotency_key=f"reply:{message_id}",
                created_at_ms=time.time_ns() // 1_000_000,
            )
            try:
                submitted = self._inbox.submit(reply)
            except (InboxAuthorityTimeout, InboxAuthorityUnavailable) as error:
                if isinstance(error, InboxAuthorityUnavailable) and (
                    "CORRELATION_IN_FLIGHT" not in str(error)
                ):
                    raise
                # Ambiguous external completion is not failure and must not
                # ACK the inbound question.  The deterministic reply message
                # id/correlation lets the caller retry and join the same
                # durable receipt without another native send.
                return {
                    "ok": False,
                    "messageId": message_id,
                    "replyMessageId": reply.message_id,
                    "replied": False,
                    "acknowledged": False,
                    "queued": True,
                    "code": "REPLY_SETTLEMENT_PENDING",
                    "retryable": True,
                }
            # A Lark bridge reply is not complete merely because its outbox
            # row was durably admitted.  ``queued`` means the native reply did
            # not succeed yet; the background outbox owns the single native
            # attempt and will settle the same stable receipt.  Keep the
            # inbound message pending and hand the caller the deterministic
            # reply id so a retry joins that receipt instead of triggering a
            # second native send or losing the user's question to an early ACK.
            if submitted.accepted and adapter_name is not None and submitted.queued:
                return {
                    "ok": False,
                    "messageId": message_id,
                    "replyMessageId": reply.message_id,
                    "replied": False,
                    "acknowledged": False,
                    "queued": True,
                    "code": "REPLY_SETTLEMENT_PENDING",
                    "retryable": True,
                }
            if not submitted.accepted:
                return {
                    "ok": False,
                    "messageId": message_id,
                    "replied": False,
                    "acknowledged": False,
                    "queued": submitted.queued,
                    "code": submitted.code or "REPLY_DELIVERY_PENDING",
                }
            acknowledged = self._inbox.ack(original_key, message_id)
            return {
                "ok": acknowledged.acknowledged,
                "messageId": message_id,
                "replyMessageId": reply.message_id,
                "replied": True,
                "acknowledged": acknowledged.acknowledged,
                "queued": submitted.queued,
                **({"code": acknowledged.code} if acknowledged.code else {}),
            }
        if method == "message.status":
            # The answer message.send structurally cannot give: send-time
            # accepted/queued predates the outcome, so it says nothing about
            # it.  This reads the persisted verdict instead -- locally first,
            # then from any other holder on the mesh, since custody may have
            # moved the message to a mailbox that outlived this node.
            return self._delivery_status_result(params)
        if method == "message.ack":
            actor = self._message_consumer_actor(params)
            actor = self._fence_interactive_session(actor, params)
            message_id = _required_string(params.get("messageId"), "messageId")
            result = self._inbox.ack(actor, message_id)
            if not result.acknowledged:
                # Rows written before the canonicalizing boundary carry the
                # verbatim key; try the fallback spellings before failing.
                for key in self._message_consumer_keys(params)[1:]:
                    result = self._inbox.ack(key, message_id)
                    if result.acknowledged:
                        break
            return {
                "ok": result.acknowledged,
                "messageId": result.message_id,
                "acknowledged": result.acknowledged,
                **({"code": result.code} if result.code else {}),
            }
        if method == "outbox.list":
            return {
                "ok": True,
                "entries": [_outbox_entry_json(item) for item in self._inbox.outbox_items()],
            }
        if method == "outbox.prune":
            dry_run = params.get("dryRun") is True
            pruned = self._inbox.prune_outbox(
                undeliverable=_undeliverable_outbox_recipient,
                unresolvable=self._unresolvable_outbox_recipient,
                dry_run=dry_run,
            )
            for item in pruned:
                self._log(
                    "info",
                    "daemon",
                    "outbox.prune.dry-run" if dry_run else "outbox.pruned",
                    messageId=item.message_id,
                    correlationId=item.message_id,
                    **({"node": "delivery-terminal"} if not dry_run else {}),
                    recipient=item.recipient,
                    reason=item.reason,
                    attempts=item.attempts,
                )
            return {
                "ok": True,
                "dryRun": dry_run,
                "pruned": [
                    {
                        "messageId": item.message_id,
                        "recipient": item.recipient,
                        "reason": item.reason,
                        "createdAtMs": item.created_at_ms,
                        "attempts": item.attempts,
                    }
                    for item in pruned
                ],
                "prunedCount": len(pruned),
                "remainingCount": self._inbox.outbox_count(),
            }
        if method == "lifecycle.start":
            # The IPC key stays "provider" so new CLIs and old daemons can
            # mix in either direction; locally it names a harness.
            harness = _required_string(params.get("provider"), "provider")
            if harness == "lark":
                raise DaemonRequestError(
                    ipc_errors.USE_ADAPTER_COMMAND,
                    "Lark is an external-platform adapter; use 'hyprial adapter start <name>'",
                )
            try:
                name = self.agents.native_actor(
                    _required_string(params.get("name"), "name")
                )
            except AgentError as error:
                raise DaemonRequestError(error.code, str(error)) from error
            spec = HarnessLaunchSpec.from_json(
                {**params, "provider": harness, "name": name}, "provider"
            )
            hosted_owner = self._host_invited_owner(name)
            if hosted_owner is not None:
                if spec.pinned_owner is None:
                    spec = replace(spec, pinned_owner=hosted_owner)
                elif spec.pinned_owner != hosted_owner:
                    raise DaemonRequestError(
                        ipc_errors.INVALID_ARGUMENT,
                        "start cannot re-own a host-invited agent; "
                        f"registry row is owned by {hosted_owner}",
                    )
            if (
                spec.pinned_owner is not None
                and spec.pinned_owner != self.owner
                and spec.pinned_owner != hosted_owner
            ):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "start cannot create a foreign-owner entity; use transfer receive",
                )
            # An explicit resume request: only when the CALLER sent a ref.
            # A spec that merely carries one (every running spec does after
            # sync_harness_session_refs) keeps #190's quiet fallback on the
            # daemon-restart path; a person who asked for a conversation must
            # get that conversation or a refusal, never a fresh session.
            resume_ref = spec.session_ref if "sessionRef" in params else None
            if resume_ref is not None:
                self._require_resumable_session(spec, resume_ref)
            actor_uri = self._canonical_harness_uri(spec.name, spec)
            prior_agent = self.agents.get(actor_uri)
            operation_id = str(
                params.get("operationId")
                or f"lifecycle-start:{uuid4().hex}"
            )
            result = self._run_lifecycle_operation(
                LifecycleOperation.create(
                    operation_id,
                    self._lifecycle_spec(spec),
                )
            )
            if resume_ref is not None:
                # The create operation has settled; readiness is expected to
                # be there already, so the check waits one margin, not a
                # budget of its own.
                self._verify_started_resume(
                    spec, resume_ref, timeout=LIFECYCLE_WAIT_MARGIN_SECONDS
                )
            handover = (
                HandoverNotice(
                    actor=prior_agent.actor,
                    previous_harness=prior_agent.last_harness,
                    previous_session_id=prior_agent.last_session_id,
                    next_harness=harness,
                )
                if prior_agent is not None
                and prior_agent.last_harness is not None
                and prior_agent.last_harness != harness
                else None
            )
            return {
                "ok": True,
                "id": f"{harness}:{name}",
                "operationId": operation_id,
                "changed": bool(result.completed_effects),
                "actor": actor_uri,
                **({"sessionRef": resume_ref} if resume_ref is not None else {}),
                **(
                    {"harnessHandover": handover.to_json()}
                    if handover is not None
                    else {}
                ),
            }
        if method == "down":
            state = self.desired_state.load()
            removed: list[str] = []
            if params.get("all") is True:
                for spec in state.harnesses:
                    if spec.harness == "lark" and self._lark_client is not None:
                        self._lark_client.stop(spec.name)
                    else:
                        operation_id = f"lifecycle-down:{uuid4().hex}"
                        self._run_lifecycle_operation(
                            LifecycleOperation.deactivate(
                                operation_id,
                                self._lifecycle_spec(spec),
                            )
                        )
                    if spec.containerized:
                        self._retire_container_artifacts(
                            spec.harness, spec.name
                        )
                    removed.append(f"{spec.harness}:{spec.name}")
            else:
                target = _required_string(params.get("target"), "target")
                harness = params.get("provider")
                for spec in state.harnesses:
                    if (
                        harness is None
                        and target in {spec.name, f"{spec.harness}:{spec.name}"}
                    ) or (harness == spec.harness and target == spec.name):
                        if spec.harness == "lark" and self._lark_client is not None:
                            self._lark_client.stop(spec.name)
                        else:
                            operation_id = f"lifecycle-down:{uuid4().hex}"
                            self._run_lifecycle_operation(
                                LifecycleOperation.deactivate(
                                    operation_id,
                                    self._lifecycle_spec(spec),
                                )
                            )
                        if spec.containerized:
                            self._retire_container_artifacts(
                                spec.harness, spec.name
                            )
                        removed.append(f"{spec.harness}:{spec.name}")
                if not removed:
                    # Nothing in desired state matched, but `down` is also the
                    # documented remedy when a start is refused. Release the
                    # binding anyway so a connector whose spec vanished
                    # without a clean stop can never strand its agent's name
                    # behind an error that tells the user to run this.
                    released = self._agent_name_for(target)
                    if released is not None:
                        self._release_agent_binding(self.agents.uri_for(released))
            return {"ok": True, "removed": removed}
        if method == "transfer.plan":
            return self._transfer_plan(params)
        if method == "transfer.quiesce":
            return self._transfer_quiesce(params)
        if method == "transfer.precheck":
            return self._transfer_precheck(params)
        if method == "transfer.receive":
            return self._transfer_receive(params)
        if method == "transfer.resume":
            return self._transfer_resume(params)
        if method == "transfer.complete":
            return self._transfer_complete(params)
        if method == "adapter.reload":
            # adapter add writes config without contacting the daemon; until a
            # reload (or restart) the supervisor only knows its startup
            # snapshot, so a freshly added adapter cannot start.  Reloading is
            # incremental: running workers are never restarted or stopped.
            assert self._lark_client is not None
            completed = self._call_lark_reload()
            summary = (
                completed.reload.to_payload()
                if completed.reload is not None
                else {
                    "added": [],
                    "updated": [],
                    "removed": [],
                    "removedRunning": [],
                }
            )
            try:
                self._reload_user_adapters()
            except (OSError, ValueError) as error:
                raise DaemonRequestError(
                    ipc_errors.ADAPTER_RELOAD_FAILED,
                    f"gateway configs reloaded ({summary}) but receiver-owned "
                    f"adapters failed to reload: {error}",
                    {"reloaded": summary},
                ) from error
            return {"ok": True, **summary}
        if method == "adapter.list":
            assert self._lark_client is not None
            return {
                "ok": True,
                "adapters": [
                    item.to_payload() for item in self._lark_client.read_adapters()
                ],
            }
        if method == "adapter.status":
            assert self._lark_client is not None
            name = _required_string(params.get("name"), "name")
            status = self._lark_client.read_adapter(name)
            if status is None:
                raise DaemonRequestError(
                    ipc_errors.ADAPTER_NOT_FOUND, f"adapter is not configured: {name}"
                )
            return {"ok": True, "adapter": status.to_payload()}
        if method == "adapter.start":
            assert self._lark_client is not None
            name = _required_string(params.get("name"), "name")
            try:
                event = self._lark_client.start(name)
            except (AdapterStartError, DomainCommandError) as error:
                # Lifecycle verdicts keep their stable code (G1/G2); the rest
                # of the start failures keep the historical mapping.
                verdict = getattr(error, "code", None)
                code = (
                    verdict
                    if verdict == lark_lifecycle.ADAPTER_START_TIMEOUT
                    else (
                        ipc_errors.ADAPTER_NOT_FOUND
                        if "not configured" in str(error)
                        else "ADAPTER_START_FAILED"
                    )
                )
                raise DaemonRequestError(code, str(error)) from error
            projection = self._lark_client.read_adapter(name)
            if projection is None:
                raise DaemonRequestError(
                    ipc_errors.ADAPTER_NOT_FOUND, f"adapter is not configured: {name}"
                )
            return {
                "ok": True,
                "changed": event.changed,
                "adapter": projection.to_payload(),
            }
        if method == "adapter.stop":
            assert self._lark_client is not None
            name = _required_string(params.get("name"), "name")
            event = self._lark_client.stop(name)
            status = self._lark_client.read_adapter(name)
            if status is None:
                if event.changed:
                    # A detached worker (config removed by a reload): the
                    # stop did happen, and the name is now gone from every
                    # view.  Report the fact instead of raising an error for
                    # a stop that succeeded.
                    return {
                        "ok": True,
                        "changed": True,
                        "adapter": {
                            "name": name,
                            "status": "stopped",
                            "configured": False,
                        },
                    }
                raise DaemonRequestError(
                    ipc_errors.ADAPTER_NOT_FOUND, f"adapter is not configured: {name}"
                )
            return {
                "ok": True,
                "changed": event.changed,
                "adapter": status.to_payload(),
            }
        if method == "adapter.pin":
            assert self._adapters is not None
            name = self._require_adapter(params.get("name"))
            actor = _required_string(params.get("actor"), "actor")
            # The pin target must be an existing agent on THIS machine; the
            # value stored is always the canonical four-segment URI, so a
            # bare-name pin can never again drift apart from the URI its
            # connector registers (the bare-name/canonical-pin incident).
            agent = self._require_pinnable_agent(actor)
            # The one-to-one rule is the pins table's UNIQUE constraints;
            # this handler only translates the typed violation.
            staged_legacy = dict(self.desired_state.load().channel_pins).get(name)
            try:
                previous = self.agents.pin(name, agent.actor)
            except PinConflictError as error:
                raise DaemonRequestError(
                    error.code,
                    str(error),
                    {
                        "actor": agent.uri,
                        "adapter": name,
                        "pinnedBy": error.holder,
                    },
                ) from error
            # A staged legacy entry for this adapter (an unmigrated
            # ``channelPins`` value) is superseded by this explicit write and
            # must not resurrect through the next startup migration.
            _state, legacy = self.desired_state.remove_channel_pin(name)
            legacy = staged_legacy if legacy is None else legacy
            if previous is None:
                previous = legacy
            return {
                "ok": True,
                "adapter": name,
                "actor": agent.uri,
                "previous": previous,
                "changed": previous != agent.uri,
                "pins": self._adapter_pins(),
            }
        if method == "adapter.unpin":
            assert self._adapters is not None
            name = self._require_adapter(params.get("name"))
            staged_legacy = dict(self.desired_state.load().channel_pins).get(name)
            previous = self.agents.unpin(name)
            # Also drop any unmigrated legacy staging entry, for the same
            # no-resurrection reason as adapter.pin.
            _state, legacy = self.desired_state.remove_channel_pin(name)
            legacy = staged_legacy if legacy is None else legacy
            if previous is None:
                previous = legacy
            return {
                "ok": True,
                "adapter": name,
                "previous": previous,
                "changed": previous is not None,
                "pins": self._adapter_pins(),
            }
        if method == "adapter.pins":
            assert self._adapters is not None
            return {
                "ok": True,
                "pins": self._adapter_pins(),
            }
        if method.startswith("agent."):
            # One boundary for the agent RPCs: the registry raises its own
            # typed errors, and every one of them already carries the IPC code
            # it should surface as. Translating here keeps handle()'s contract
            # (DaemonRequestError, always) without restating those codes.
            try:
                return self._handle_agent(method, params)
            except (AgentError, DomainCommandError) as error:
                raise DaemonRequestError(error.code, str(error)) from error
            except RegistryHomeError as error:
                raise DaemonRequestError(AgentError.code, str(error)) from error
        if method == "shutdown":
            self.stop_event.set()
            return {
                "ok": True,
                "stopping": True,
                "pid": os.getpid(),
                "epoch": self.epoch,
                "stateDir": str(self.state_dir),
                "lockPath": str(self.state_dir / "daemon.lock"),
            }
        raise DaemonRequestError(ipc_errors.METHOD_NOT_FOUND, f"unknown daemon method {method}")

    def _handle_agent(self, method: str, params: JsonObject) -> Any:
        if method in ("agent.grant", "agent.revoke", "agent.grants"):
            # L0: the local host operator records these facts. This ledger is
            # not a caller-authentication or runtime enforcement boundary.
            try:
                if method == "agent.grant":
                    revision = _optional_positive_integer(params.get("revision"), "revision")
                    if revision is None:
                        raise ValueError("revision is required")
                    grant = self._agent_registry.grant_capability(
                        _required_string(params.get("actor"), "actor"),
                        grant_id=_required_string(params.get("grantId"), "grantId"),
                        capability=_required_string(params.get("capability"), "capability"),
                        scope=_required_string(params.get("scope"), "scope"),
                        granted_by=f"user:{self.owner}", revision=revision,
                    )
                    return {"ok": True, "grant": grant.to_json()}
                if method == "agent.revoke":
                    revoked = self._agent_registry.revoke_capability(
                        _required_string(params.get("actor"), "actor"),
                        _required_string(params.get("grantId"), "grantId"),
                        revoked_by=f"user:{self.owner}",
                    )
                    return {"ok": True, "revoked": revoked}
                actor = _optional_string_param(params.get("actor"), "actor")
                if params.get("audit") is True:
                    if actor is None:
                        raise ValueError("actor is required for audit")
                    entries = self._agent_registry.grant_journal(actor)
                    return {"ok": True, "journal": [entry.to_json() for entry in entries]}
                grants = self._agent_registry.capability_grants(actor)
                return {"ok": True, "grants": [grant.to_json() for grant in grants]}
            except (ValueError, TypeError, AgentError) as error:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        if method == "agent.secret." + "provider-write":
            from hyprial.agents.secrets import SecretResolver

            entry_id = _required_string(params.get("entryId"), "entryId")
            field_name = _required_string(params.get("fieldName"), "fieldName")
            value = params.get("value")
            if not isinstance(value, str) or not value:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "secret model-vendor value must be non-empty",
                )
            if "\n" in value or "\r" in value:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "secret model-vendor value must contain one line",
                )
            try:
                resolver = SecretResolver(self.hyprial_home, self._agent_registry)
                getattr(resolver, "write_user_" + "provider")(
                    entry_id, {field_name: value}
                )
            except (ValueError, TypeError, OSError) as error:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
            return {"ok": True, "entryId": entry_id, "fieldNames": [field_name]}
        if method == "agent.secret.grant":
            from hyprial.agents.secrets import SecretSource

            actor = _required_string(params.get("actor"), "actor")
            grant_id = _required_string(params.get("grantId"), "grantId")
            source_raw = _required_string(params.get("source"), "source")
            entry_id = _required_string(params.get("entryId"), "entryId")
            field_name = _required_string(params.get("fieldName"), "fieldName")
            raw_names = params.get("environmentNames")
            if not isinstance(raw_names, list) or not raw_names or any(
                not isinstance(item, str) or not item for item in raw_names
            ):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "environmentNames must be a non-empty string array",
                )
            revision = _optional_positive_integer(params.get("revision"), "revision")
            if revision is None:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "revision is required")
            try:
                source = SecretSource(source_raw)
                grant = self._agent_registry.grant_secret(
                    actor,
                    grant_id=grant_id,
                    source=source,
                    entry_id=entry_id,
                    field_name=field_name,
                    environment_names=tuple(raw_names),
                    revision=revision,
                )
            except (ValueError, TypeError, AgentError) as error:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
            return {
                "ok": True,
                "grant": {
                    "actor": grant.actor,
                    "grantId": grant.grant_id,
                    "source": grant.source.value,
                    "entryId": grant.entry_id,
                    "fieldName": grant.field_name,
                    "environmentNames": list(grant.environment_names),
                    "revision": grant.revision,
                },
            }
        if method == "agent.secret.list":
            actor = params.get("actor")
            if actor is not None and (not isinstance(actor, str) or not actor):
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "actor must be a non-empty string")
            grants = self._agent_registry.secret_inventory(actor)
            return {
                "ok": True,
                "grants": [
                    {
                        "actor": grant.actor,
                        "grantId": grant.grant_id,
                        "source": grant.source.value,
                        "entryId": grant.entry_id,
                        "fieldName": grant.field_name,
                        "environmentNames": list(grant.environment_names),
                        "revision": grant.revision,
                    }
                    for grant in grants
                ],
            }
        if method == "agent.secret.revoke":
            actor = _required_string(params.get("actor"), "actor")
            grant_id = _required_string(params.get("grantId"), "grantId")
            try:
                revoked = self._agent_registry.revoke_secret_grant(actor, grant_id)
            except AgentError as error:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
            return {"ok": True, "revoked": revoked}
        if method == "agent.host-invite":
            # Host-controlled creation is separate from ordinary create/start:
            # neither a URI nor a caller-supplied flag confers hosting authority.
            agent = self.agents.create_host_invited(
                _required_string(params.get("name"), "name"),
                pinned_owner=_required_string(params.get("owner"), "owner"),
                cwd=_optional_string_param(params.get("cwd"), "cwd"),
                preferred_harness=_optional_string_param(
                    params.get("preferredHarness"), "preferredHarness"
                ),
            )
            self._declare_persona_route(agent.uri)
            return {"ok": True, "created": True, "agent": self._agent_status_json(agent)}
        if method == "agent.create":
            # Decision A5: the one creation path. `hyprial agent create` calls it
            # directly; `hyprial start` calls it first and only then launches a
            # harness, so a connector can never exist without an identity.
            name = self.agents.native_actor(
                _required_string(params.get("name"), "name")
            )
            reuse = params.get("existing") == "reuse"
            existing = self.agents.get(name)
            if existing is not None and not reuse:
                raise DaemonRequestError(
                    ipc_errors.AGENT_EXISTS,
                    f"the name {name!r} is already taken on this node "
                    f"({self.owner}@{self.node_id}) — {existing.uri} "
                    f"exists. One name is one agent, whether or not anything "
                    f"is currently running under it. To reuse the name, "
                    f"destroy that agent first ('hyprial agent destroy {name}', "
                    f"which is irreversible); to run this agent on a different "
                    f"harness, just start it there — that is a rebinding of "
                    f"the same agent, not a new one.",
                    {"actor": existing.uri, "agent": name},
                )
            harness = params.get("harness")
            harness_name = harness if isinstance(harness, str) and harness else None
            requested_cwd = _optional_string_param(params.get("cwd"), "cwd")
            effective_cwd = requested_cwd
            if harness_name is not None and effective_cwd is None:
                effective_cwd = str(self._agent_registry.workspace_path(name))
            if existing is None:
                agent = self.agents.create(
                    name,
                    cwd=effective_cwd,
                    config=params.get("config"),
                    # Model vendor, the same word squire uses.
                    provider=_optional_string_param(
                        params.get("provider"), "provider"
                    ),
                    model=_optional_string_param(params.get("model"), "model"),
                    capabilities=normalize_capabilities(params.get("capabilities")),
                    harness_args=normalize_harness_args(params.get("harnessArgs")),
                    preferred_harness=_optional_string_param(
                        params.get("preferredHarness"), "preferredHarness"
                    )
                    or harness_name,
                )
                # A legacy pin naming this agent could not migrate while the
                # record did not exist; it can now.
                self._migrate_legacy_channel_pins()
                self._declare_persona_route(agent.uri)
            else:
                agent = existing
            # When the caller says which harness it is about to launch, refuse
            # here if the agent is already being served -- before a TUI has
            # been spawned -- and otherwise hand back the A9 handover notice so
            # it can be put in front of the agent's first turn.
            handover: HandoverNotice | None = None
            if harness_name is not None:
                self._refuse_if_running(
                    agent.uri,
                    harness=harness_name,
                    runtime=str(params.get("runtime") or RUNTIME_HEADLESS),
                )
                agent = self._ensure_agent(
                    agent.uri, harness=harness_name,
                    interactive=params.get("runtime") == RUNTIME_INTERACTIVE,
                    cwd=effective_cwd,
                    provider=_optional_string_param(params.get("provider"), "provider"),
                    model=_optional_string_param(params.get("model"), "model"),
                )
                assert agent is not None
                if effective_cwd == str(
                    self._agent_registry.workspace_path(agent.actor)
                ):
                    self._agent_registry.ensure_workspace(agent.actor)
            if (
                harness_name is not None
                and agent.last_harness is not None
                and agent.last_harness != harness_name
            ):
                handover = HandoverNotice(
                    actor=agent.uri,
                    previous_harness=agent.last_harness,
                    previous_session_id=agent.last_session_id,
                    next_harness=harness_name,
                )
            return {
                "ok": True,
                "created": existing is None,
                "agent": self._agent_status_json(agent),
                **(
                    {"harnessHandover": handover.to_json()}
                    if handover is not None
                    else {}
                ),
            }
        if method == "agent.list":
            # Same per-request snapshot as ps: agent.list is the same loop
            # over agents[], so without it the storm only moves house.
            with self._worker_status_snapshot():
                return {
                    "ok": True,
                    "agents": [
                        self._agent_status_json(agent) for agent in self.agents.list()
                    ],
                }
        if method == "agent.runtime-context":
            # Non-secret interactive-launch handoff.  The daemon remains the
            # sole root/profile resolver; this projection deliberately omits
            # entity tokens, grant values, and credential material (T16).
            name = self.agents.normalize_actor(
                _required_string(params.get("name"), "name")
            )
            harness = _required_string(params.get("harness"), "harness")
            cwd = _optional_string_param(params.get("cwd"), "cwd")
            from hyprial.agents.runtime import (
                DEFAULT_AGENT_TOOL_PROFILE,
                AgentRuntimeError,
                resolve_agent_runtime_context,
            )
            from hyprial.agents.config import AgentConfigError
            from hyprial.agents.home import AgentHomeError
            from hyprial.harnesses.claude_runtime import (
                ClaudeRuntimeError,
                prepare_claude_runtime_context,
            )

            try:
                context = resolve_agent_runtime_context(
                    registry=self._agent_registry,
                    agent_name=name,
                    harness=harness,
                    cwd=cwd,
                    tool_profile=DEFAULT_AGENT_TOOL_PROFILE,
                    containerized=False,
                )
                if context is not None and context.harness == "claude":
                    prepare_claude_runtime_context(context)
            except (
                AgentRuntimeError,
                AgentConfigError,
                AgentHomeError,
                ClaudeRuntimeError,
            ) as error:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT, str(error)
                ) from error
            if context is None:
                return {"ok": True, "mode": "legacy", "environment": {}}
            return {"ok": True, **context.public_projection()}
        if method == "agent.get":
            name = self.agents.normalize_actor(
                _required_string(params.get("name"), "name")
            )
            with self._worker_status_snapshot():
                return {
                    "ok": True,
                    "agent": self._agent_status_json(self.agents.require(name)),
                }
        if method == "agent.resolve":
            # Read-only view of the message.send targeting pipeline, so the
            # CLI can validate a bare --from at command time without
            # reimplementing (and drifting from) the delivery boundary's
            # rules.  Ambiguity propagates as AMBIGUOUS_TARGET; an unknown
            # bare name is not an error here, it is reported as known=False
            # so the caller decides (durable-queue semantics stay legal).
            name = _required_string(params.get("name"), "name")
            normalized = normalize_agent_recipient(name)
            resolved = self._resolve_agent_alias(normalized)
            reason = "unknown"
            if resolved != normalized:
                reason = "alias"
            elif normalized == self.node_id:
                reason = "node"
            elif ":" in normalized:
                reason = "scheme"
            return {
                "ok": True,
                "input": name,
                "resolved": resolved,
                "known": reason != "unknown",
                "reason": reason,
            }
        if method == "agent.destroy.preview":
            requested = _required_string(params.get("name"), "name")
            agent = self.agents.require(requested)
            return {
                "ok": True,
                "actor": agent.uri,
                "agent": agent.actor,
                "workspace": self._agent_registry.workspace_summary(
                    agent.actor
                ).to_json(),
            }
        if method == "agent.destroy":
            # Resolve the exact identity before dropping URI ownership. Hosted
            # URIs are valid; unrelated same-name foreign URIs are not.
            requested = _required_string(params.get("name"), "name")
            agent = self.agents.get(requested)
            if agent is None:
                try:
                    cleaned = self._agent_registry.cleanup_revoked_home(requested)
                except RegistryHomeError as error:
                    raise DaemonRequestError(AgentError.code, str(error)) from error
                if cleaned is None:
                    self.agents.require(requested)
                    raise AssertionError("require() returned for a missing agent")
                actor = canonical_agent_uri(self.owner, self.node_id, cleaned.actor)
                self._log(
                    "warn",
                    "agents",
                    "agent.destroyed",
                    actor=actor,
                    stopped=[],
                    destroyedMessages=0,
                    unpinnedAdapters=[],
                    cleanupResumed=True,
                )
                return {
                    "ok": True,
                    "destroyed": False,
                    "cleanupResumed": True,
                    "actor": actor,
                    "agent": cleaned.actor,
                    "stopped": [],
                    "destroyedMessages": 0,
                    "unpinnedAdapters": [],
                    "irreversible": True,
                }
            return self._destroy_agent(agent.actor)
        raise DaemonRequestError(ipc_errors.METHOD_NOT_FOUND, f"unknown daemon method {method}")

    def _require_adapter(self, raw: object) -> str:
        """Validate an adapter name against the configured gateways.

        Mirrors the ``adapter.status`` ADAPTER_NOT_FOUND precedent, but the
        message lists the available adapters so a mistyped name is diagnosable.
        """

        name = _required_string(raw, "name")
        available = self._lark_gateway_names()
        if name not in available:
            listing = ", ".join(available) if available else "(none configured)"
            raise DaemonRequestError(
                ipc_errors.ADAPTER_NOT_FOUND,
                f"adapter is not configured: {name}; available adapters: {listing}",
            )
        return name

    def _adapter_pins(self) -> dict[str, str]:
        """Adapter -> agent-URI pins, one query against the pins table.

        Deliberately unfiltered: every pin that exists is visible here, so an
        entry left behind by a removed adapter can be seen and cleaned instead
        of lingering invisibly (the old two-view filter hid exactly those).
        """

        return self.agents.pins()

    def _require_pinnable_agent(self, actor: str) -> Agent:
        """Resolve a pin target to an existing agent on this machine, loudly.

        A pin is the decision "every DM this adapter receives goes to that
        agent" -- aiming it at a name nobody registered would send messages
        into a queue nobody can ever drain, so a nonexistent target is an
        error, not a deferred binding.  Creating the agent must come first
        (``hyprial agent create`` / ``hyprial start``), then the pin.
        """

        agent = self.agents.get(actor)
        if agent is not None:
            return agent
        parsed_actor = parse_agent_uri(actor)
        if parsed_actor is not None:
            if (
                parsed_actor[0] != self.owner
                or parsed_actor[1] != self.node_id
            ):
                raise DaemonRequestError(
                    ipc_errors.AGENT_NOT_FOUND,
                    f"cannot pin {actor!r}: that URI names an agent of owner "
                    f"{parsed_actor[0]!r} on machine {parsed_actor[1]!r}, and a pin can only "
                    f"bind an agent registered on this machine "
                    f"({self.owner}@{self.node_id}).",
                    {"actor": actor},
                )
        raise DaemonRequestError(
            ipc_errors.AGENT_NOT_FOUND,
            f"no agent named {actor!r} on this machine. A pin binds an "
            f"adapter to an existing agent: create it first with "
            f"'hyprial agent create --name {actor}' (or 'hyprial start'), then pin.",
            {"actor": actor},
        )

    def _migrate_legacy_channel_pins(self) -> None:
        """Move desired-state ``channelPins`` into the agents database, loudly.

        Runs at daemon startup and again whenever an agent is created, and is
        idempotent: an entry either migrates (value normalized to the agent's
        canonical URI, binding written to the ``pins`` table) or stays in
        ``channelPins`` with an error-level log naming the exact remedy.  A
        pin is never silently dropped -- with one decided exception: IRC
        ``#channel`` pins, whose feature is removed outright (nothing ever
        read them for delivery; production carried zero).
        """

        state = self.desired_state.load()
        if not state.channel_pins:
            return
        gateways = set(self._lark_gateway_names())
        remaining: dict[str, str] = {}
        for adapter, value in state.channel_pins:
            if adapter.startswith("#"):
                self._log(
                    "warn",
                    "daemon",
                    "pin.migration.irc-dropped",
                    channel=adapter,
                    actor=value,
                    detail="IRC channel pins are removed; nothing read them",
                )
                continue
            if gateways is not None and adapter not in gateways:
                remaining[adapter] = value
                self._log(
                    "error",
                    "daemon",
                    "pin.migration.unknown-adapter",
                    adapter=adapter,
                    actor=value,
                    detail=(
                        f"{adapter!r} is not a configured adapter; the entry "
                        f"is kept unmigrated. Configure the adapter and "
                        f"restart, or drop the entry from desired-state"
                    ),
                )
                continue
            agent = self.agents.get(value)
            if agent is None:
                remaining[adapter] = value
                self._log(
                    "error",
                    "daemon",
                    "pin.migration.unresolved",
                    adapter=adapter,
                    actor=value,
                    detail=(
                        f"pin target {value!r} does not resolve to an agent "
                        f"on this machine; the pin is kept unmigrated and is "
                        f"NOT routing. Create the agent ('hyprial agent create "
                        f"--name {value}' or 'hyprial start') and it migrates "
                        f"immediately, or drop it with 'hyprial adapter unpin "
                        f"{adapter}'"
                    ),
                )
                continue
            try:
                self.agents.pin(adapter, agent.actor)
            except PinConflictError as error:
                # The pins table's UNIQUE(agent) spoke; keep the entry
                # unmigrated and say exactly how to resolve it.
                remaining[adapter] = value
                self._log(
                    "error",
                    "daemon",
                    "pin.migration.conflict",
                    adapter=adapter,
                    actor=agent.uri,
                    pinnedBy=error.holder,
                    detail=(
                        f"agent {agent.uri} is already pinned by "
                        f"{error.holder!r} and adapter/agent bind one-to-one; "
                        f"the entry is kept unmigrated. Keep one: 'hyprial "
                        f"adapter unpin {error.holder}' then 'hyprial adapter "
                        f"pin {adapter} {agent.actor}', or 'hyprial adapter "
                        f"unpin {adapter}'"
                    ),
                )
                continue
            self._log(
                "info",
                "daemon",
                "pin.migrated",
                adapter=adapter,
                actor=agent.uri,
                legacyValue=value,
            )
        if remaining != dict(state.channel_pins):
            self.desired_state.save(
                replace(state, channel_pins=tuple(sorted(remaining.items())))
            )

    def _host_invited_owner(self, name: str) -> str | None:
        """Read the admitted visitor owner; transfer-receive grants no start authority."""
        agent = self.agents.get(self.agents.uri_for(name))
        return agent.owner if agent is not None and agent.hosted_by == "host-invite" else None

    def _canonical_harness_uri(
        self,
        name: str,
        spec: HarnessLaunchSpec | None = None,
        desired: DesiredState | None = None,
    ) -> str:
        """The network identity this daemon mints for a managed harness.

        A transferred or host-invited spec carries ``pinned_owner``: the
        URI keeps the admitted owner instead of this daemon's ambient one,
        so hosting never silently renames the worker. The machine
        segment stays this node's id -- the worker physically lives here.

        The spec=None fallback is a defensive load for single-call paths
        (e.g. identity.whoami).  Batch paths must pass the spec they already
        hold: per-iteration fallback loads are what turned one ps into
        O(agents × connectors) full desired-state reads.  ``desired`` is the
        caller's already-loaded state (card 259) for callers that hold no
        spec but do hold the state -- a snapshot hands its one copy down
        instead of every row re-reading the store.  The URI it returns is
        the same either way.
        """

        owner = self.owner
        if spec is None and desired is None:
            # A batch path that installed the worker-status snapshot already
            # holds one loaded copy; reuse it instead of one full load per
            # call (the runtime timer resolved every streaming actor this way,
            # 2026-09-25 production dump on pid 54206).
            snapshot = self._current_worker_snapshot()
            if snapshot is not None and snapshot.desired is not None:
                desired = snapshot.desired
        if spec is None:
            try:
                state = (
                    self.desired_state.load() if desired is None else desired
                )
                spec = next(
                    (item for item in state.harnesses if item.name == name),
                    None,
                )
            except Exception:  # noqa: BLE001 - identity must never crash
                spec = None
        if spec is not None and spec.pinned_owner:
            owner = spec.pinned_owner
        return canonical_agent_uri(owner, self.node_id, name)

    # ── A3 dispatch gate (design-dispatch-always-pac-2026-09-03 §三②) ────

    def _resolve_routine_principal(self, actor: str) -> str:
        principal = parse_agent_uri(actor)
        if principal is not None and principal[:2] != (self.owner, self.node_id):
            return actor  # explicit target; workflow admission checks its home daemon
        return self._resolve_send_sender(actor)

    def _record_workflow_outcome(self, result):
        if self._remote_workflow is not None and self._remote_workflow.outcome(result):
            return True
        workflow = self._workflow_service
        if workflow is None:
            return False
        return workflow.record_harness_outcome(
            message_id=result.delivery_id, recipient=result.recipient,
            failed=result.status.value != "completed", failure_code=result.failure_code)

    def _workflow_admit(self, spec: WorkflowSpec, sender: str) -> None:
        from .pac_actor import DaemonActorRuntime
        from hyprial.pac.lifecycle import LaunchSpec

        from hyprial.pac.errors import WORKFLOW_REMOTE_OWNER_UNSUPPORTED

        for node in spec.nodes:
            if node.owner is not None:
                if node.owner.startswith("user:"):
                    if node.owner != f"user:{self.owner}":
                        raise DaemonRequestError(WORKFLOW_REMOTE_OWNER_UNSUPPORTED,
                            "workflow completion currently requires this daemon's local user or bound actors")
                    continue
                principal = parse_agent_uri(node.owner)
                if principal is not None and principal[:2] != (self.owner, self.node_id):
                    from hyprial.pac.errors import PacError
                    if self._remote_workflow is None:
                        raise DaemonRequestError("WORKFLOW_REMOTE_UNAVAILABLE", "remote workflow service is not running")
                    try:
                        self._remote_workflow.admit(node)
                    except PacError as error:
                        raise DaemonRequestError(error.code, str(error)) from error
                    continue
                recipient = self._resolve_send_sender(node.owner)
                entity = self.agents.get(recipient)
                capabilities = entity.capabilities if entity is not None else {}
            else:
                assert node.launch is not None
                try:
                    DaemonActorRuntime._launch_spec("plan-worker", LaunchSpec.from_json(node.launch), "plan")
                except (TypeError, ValueError) as error:
                    raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
                recipient = f"workflow-worker:{node.worker}"
                capabilities = {"interactive": False}
            dispatch_gate(target=recipient, capabilities=capabilities, role=node.role,
                          first_output_eta=node.first_output_eta,
                          human_gates_declared=node.human_gates is not None,
                          emit=self._log, source="pac.workflow.admission")

    def _workflow_caller(self, params: JsonObject) -> str:
        actor = params.get("actor")
        if actor == f"user:{self.owner}" and "sessionRef" not in params:
            # The private local control socket is the trusted human boundary.
            return str(actor)
        return self._pac_bound_caller(params)

    @property
    def _dispatch_service_actor(self) -> str:
        """Canonical sender for daemon-owned dispatch and PAC notification IO."""

        return dispatch_service_actor_uri(self.owner, self.node_id)

    def _dispatch_without_pac_snapshot(self) -> int:
        """Dispatch-without-PAC count for this daemon epoch (ps/top read this)."""

        with self._dispatch_without_pac_lock:
            return self._dispatch_without_pac_count

    def _dispatch_conversation_snapshot(self) -> int:
        """Conversation-classified count, same epoch scope, for contrast.

        The denominator-next-door the 2026-09-04 reading needed: ps/top
        show it beside dispatchWithoutPacCount so an operator can see the
        gate is still observing the request-shape traffic it stopped
        counting.
        """

        with self._dispatch_without_pac_lock:
            return self._dispatch_conversation_count

    # Condition (b) of the narrowed gate: the PAC template's delivery-section
    # keywords, verbatim from spec-dispatch-gate-classifier-2026-09-04.  Two
    # conditions are the whole judgment — this list is the template's, not a
    # regex encyclopedia of dispatch phrasings.
    _DISPATCH_FEATURE_KEYWORDS = ("交付物", "分支", "spec", "首个可核产出")

    def _is_dispatch_candidate(
        self, *, sender: str, recipient: str, intent: str
    ) -> bool:
        """The request-intent §三② shape: a dispatch or conversation candidate.

        Four clauses, each one spec term:

        • 非 workflow 发出 -- PAC deliveries never traverse message.send at
          all (they enter the inbox directly under ``workflow:{effect_id}``
          idempotency keys), so this boundary sees only non-workflow sends;
          the sender clause below is the defensive backstop for the one
          actor that must never count even if a future path routes it here.
        • intent=request -- replies (``replyTo``) are answers, not dispatches.
        • 发件方是协调者/人 -- every sender that survives the resolve-or-reject
          boundary is a registered local agent (coordinator), a ``user:``
          human, or an adapter-forwarded human; all of them dispatch.
        • 收件方是本机 agent -- a canonical ``agent:<owner>:<node>:<name>``
          URI naming THIS node; user/route targets and foreign-node agents
          are not workers being assigned work here.

        spec-dispatch-gate-classifier-2026-09-04 keeps this shape unchanged
        as the CANDIDATE set; which candidates count is decided by
        ``_classify_dispatch_send``.
        """

        if intent != "request":
            return False
        if sender == self._dispatch_service_actor:
            return False
        parsed = parse_agent_uri(recipient)
        if parsed is None or parsed[0] != self.owner or parsed[1] != self.node_id:
            return False
        return True

    def _dispatch_send_has_feature(self, message: InboxMessage) -> bool:
        """Condition (b): the body or payload carries a dispatch feature.

        Either the 正文 contains one of the PAC template delivery-section
        keywords, or the payload dict carries a ``task``/``deliverable``
        field.  message.send builds its own ``{"message", "topic"}`` payload
        today, so the field disjunct is the forward-compatible half; both
        are spec terms, kept verbatim.
        """

        try:
            payload = json.loads(message.payload) if message.payload else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        if "task" in payload or "deliverable" in payload:
            return True
        text = payload.get("message")
        if isinstance(text, str):
            return any(
                keyword in text for keyword in self._DISPATCH_FEATURE_KEYWORDS
            )
        return False

    def _classify_dispatch_send(
        self, message: InboxMessage, *, conversation_is_new: bool
    ) -> bool:
        """The narrowed judgment: count only new-conversation dispatch shapes.

        spec-dispatch-gate-classifier-2026-09-04: a candidate counts as
        ``dispatch.without_pac`` only when BOTH hold —

        * (a) 新会话: this send opens its conversation (computed at the
          message.send boundary, before any target loop);
        * (b) 派活特征: delivery-section keyword or task/deliverable field.

        Every other candidate is a conversation: ``dispatch.conversation``
        event plus the contrast counter, never the numerator.  Still
        record-only — this returns whether it counted, changes nothing else
        about the delivery.
        """

        if conversation_is_new and self._dispatch_send_has_feature(message):
            self._record_dispatch_without_pac(message)
            return True
        self._record_dispatch_conversation(
            message,
            reason=(
                "existing-conversation"
                if not conversation_is_new
                else "no-dispatch-feature"
            ),
        )
        return False

    def _record_dispatch_without_pac(self, message: InboxMessage) -> None:
        """Emit the durable event and bump the ps/top counter.  Never blocks.

        A replayed idempotent send re-traverses this path and logs again under
        the same messageId: the daemon.jsonl stream collapses on messageId,
        the epoch counter deliberately does not (it counts send attempts that
        dispatched, matching what a daemon sees).
        """

        self._log(
            "info",
            "daemon",
            "dispatch.without_pac",
            sender=message.sender,
            recipient=message.recipient,
            messageId=message.message_id,
        )
        with self._dispatch_without_pac_lock:
            self._dispatch_without_pac_count += 1

    def _record_dispatch_conversation(
        self, message: InboxMessage, *, reason: str
    ) -> None:
        """Emit dispatch.conversation for an uncounted candidate.  Never blocks.

        The observable residue of the narrowed gate (spec §1): every
        request-shape send the gate does not count stays inspectable in
        daemon.jsonl with WHY it was not counted, and the ps/top contrast
        counter shows the daemon still sees this traffic.
        """

        self._log(
            "info",
            "daemon",
            "dispatch.conversation",
            sender=message.sender,
            recipient=message.recipient,
            messageId=message.message_id,
            reason=reason,
        )
        with self._dispatch_without_pac_lock:
            self._dispatch_conversation_count += 1

    def _resolve_send_sender(self, actor: str) -> str:
        """Resolve an unfenced (CLI) sender to a registered identity — or refuse.

        Allen (b): resolve-or-reject.  Every identity on the wire carries a
        registry row; the daemon never mints one from nothing.  Runs the one
        targeting pipeline (``normalize_agent_recipient`` +
        ``_resolve_agent_alias``, local-only: a sender can only be an
        identity of THIS node).  That local-only property used to need an
        opt-out here (``include_presence=False``) because resolution scanned
        network presence; it is now structural — the resolver reads two local
        tables and nothing else, so no caller can readmit a remote candidate
        by forgetting a keyword.  Scheme-carrying senders — canonical agent
        URIs, ``channel:lark:<adapter>`` inbound forwards, ``user:`` — pass
        through untouched; ambiguity propagates as AMBIGUOUS_TARGET.  The
        error states facts only, no remediation instructions.
        """

        normalized = normalize_agent_recipient(actor)
        if ":" not in normalized:
            resolved = self._resolve_agent_alias(normalized)
            if resolved == normalized:
                raise DaemonRequestError(
                    ipc_errors.SENDER_UNRESOLVED,
                    (f"sender {actor!r} is not a registered agent on this "
                    "node; an unregistered identity cannot receive replies"),
                    {"sender": actor},
                )
            return resolved
        if normalized.startswith(AGENT_URI_PREFIX):
            if classify_target_identity(normalized) != TARGET_KIND_AGENT:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"sender {actor!r} is not a canonical "
                    "agent:<owner>:<machine>:<actor> URI",
                    {"sender": actor},
                )
            # A canonical-shaped sender is not automatically backed: the URI
            # must name THIS node and resolve to a registered identity here.
            # Anything else is a foreign or unbacked identity — sending with
            # it would put an impersonating sender on the wire.
            parsed_sender = parse_agent_uri(normalized)
            assert parsed_sender is not None  # classify proved four segments
            _owner, _machine, short = parsed_sender
            if (
                parsed_sender[0] == self.owner
                and parsed_sender[1] == self.node_id
                and self._resolve_agent_alias(short)
                == normalized
            ):
                return normalized
            raise DaemonRequestError(
                ipc_errors.SENDER_UNRESOLVED,
                (f"sender {actor!r} is not a registered agent on this "
                    "node; an unregistered identity cannot receive replies"),
                {"sender": actor},
            )
        return normalized

    def _resolve_consumer_identity(self, actor: str) -> str:
        """Read-side identity: resolve a bare name when registered, else verbatim.

        Reads never reject: a registered alias reads its canonical key, an
        unregistered bare name reads the verbatim key its durable rows
        carry.  ``_message_consumer_keys`` adds the union spellings.
        """

        normalized = normalize_agent_recipient(actor)
        if ":" in normalized:
            return normalized
        return self._resolve_agent_alias(normalized)

    def _reply_path_unavailable(self, sender: str) -> bool:
        """Best-effort verdict: is a reply to this sender certain to fail?

        Only the two provably-broken shapes return True (#60: 宁可漏报,
        不误报): a ``user:<owner>`` sender whose squire profile or binding
        is incomplete (delivery fails TARGET_SQUIRE_UNCONFIGURED), and a
        ``channel:lark:<adapter>`` sender whose adapter is not configured
        here (replies fail MESSAGE_REPLY_UNAVAILABLE).  Everything else
        returns False — unsure means silent.
        """

        if sender.startswith("user:"):
            owner = sender.removeprefix("user:")
            # `resolve`, not `get_by_owner`: `user:<x>` carries either an owner
            # key (what `resolve_node_owner()` mints) or an owner.
            profile = self.user_profiles.resolve(owner)
            return (
                profile is None
                or profile.squire_adapter is None
                or profile.owner_open_id is None
            )
        adapter = lark_reply_adapter(sender)
        if adapter is not None:
            return (
                self._adapters is None
                or adapter not in self._lark_gateway_names()
            )
        return False

    def _canonical_interactive_actor(self, actor: str) -> str:
        """Normalize one local MCP/Channel actor at the daemon boundary.

        CLI normalization is convenience only: older channel children and raw
        MCP clients can still call ``session.register`` directly.  The daemon
        is the authority that prevents those callers from creating a second,
        bare network registration beside the managed-harness four-segment
        form.  An already-canonical URI remains byte-for-byte unchanged for
        compatibility with callers that supply the complete identity.
        """

        if ":" not in actor:
            try:
                return canonical_agent_uri(self.owner, self.node_id, actor)
            except ValueError as error:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        short = agent_uri_actor(actor)
        if short is None:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"actor {actor!r} is neither a bare actor name nor a canonical "
                "agent:<owner>:<machine>:<actor> URI",
            )
        return actor

    def _mcp_actor(self, params: JsonObject) -> str:
        """Canonicalize only actors carried by fenced MCP/session requests.

        The same message methods also back unfenced CLI operations such as
        ``hyprial ack --from <node>``.  Those actors address durable inbox rows
        verbatim and must not be rewritten as interactive-agent URIs.
        """

        actor = _actor(params)
        return (
            self._canonical_interactive_actor(actor)
            if "sessionRef" in params
            else actor
        )

    def _message_query(self, params: JsonObject) -> JsonObject:
        """Read one local actor's inbox or outbox for a person, changing nothing.

        ``message.pending.list`` is the CONSUMER surface: even without the
        explicit fetch marker it drains the actor's system notices
        (``drain_system_notices`` deletes them), so a person "just looking"
        through it would eat notices meant for the agent.  This method reads
        the same rows through the non-draining readers only -- no fence, no
        fetch stamp, no receipt, no notice drain -- so the agent's next
        ``harness_read`` sees exactly what it would have seen.
        """

        view = _required_string(params.get("view"), "view")
        if view not in {"inbox", "outbox"}:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "view must be inbox or outbox"
            )
        # A person's query never speaks for a session: resolve the name the
        # same way a CLI send/read does, never through a session fence.
        lookup = {key: value for key, value in params.items() if key != "sessionRef"}
        keys = self._message_consumer_keys(lookup)
        actor = keys[0]
        entries: list[JsonObject] = []
        if view == "inbox":
            seen: set[str] = set()
            notices_reader = getattr(self._inbox, "system_notices", None)
            for key in keys:
                for notice in notices_reader(key) if callable(notices_reader) else ():
                    if notice.message_id not in seen:
                        seen.add(notice.message_id)
                        entries.append(_query_entry(notice, kind="notice"))
            for key in keys:
                for message in self._inbox.pending_messages(key):
                    if message.message_id not in seen:
                        seen.add(message.message_id)
                        entries.append(_query_entry(message, kind="message"))
        else:
            senders = set(keys)
            for item in self._inbox.outbox_items():
                if item.message.sender in senders:
                    entry = _query_entry(item.message, kind="outbox")
                    entry["attempts"] = item.attempts
                    entry["expiresAtMs"] = item.expires_at_ms
                    entries.append(entry)
        return {"ok": True, "actor": actor, "view": view, "entries": entries}

    def _message_consumer_actor(self, params: JsonObject) -> str:
        """Resolve an inbox consumer with the same rule as ``message.send``.

        A bare CLI name resolves exactly like a send sender: a registered
        alias reads its registered four-segment identity's key, an unknown
        bare name reads the verbatim key.  Fenced session calls still use
        :meth:`_mcp_actor` and are always canonical.  Reads union the other
        spellings (see ``_message_consumer_keys``) so rows written under a
        different era's key stay visible.
        """

        if "sessionRef" in params:
            return self._mcp_actor(params)
        return self._resolve_consumer_identity(_actor(params))

    def _message_consumer_keys(self, params: JsonObject) -> tuple[str, ...]:
        """Read keys for a consumer: resolved first, then the other eras' keys.

        Three spellings can hold rows for one typed name: the registered
        canonical URI (current), the verbatim bare name (pre-boundary), and
        — for an unregistered bare name — the local four-segment spelling
        the short-lived mint era (between A and the resolve-or-reject
        decision) wrote.  The minted probe is a READ key composed through
        the one URI constructor, never a new identity — a transitional
        compatibility window, removable once that era's rows are drained.  Union all that
        apply, resolved first; losing sight of durable rows is not an
        acceptable form of breaking compatibility.
        """

        actor = self._message_consumer_actor(params)
        if "sessionRef" in params:
            return (actor,)
        raw = _actor(params)
        if actor != raw:
            return (actor, raw)
        if ":" not in raw:
            return (actor, canonical_agent_uri(self.owner, self.node_id, raw))
        return (actor,)

    def _pending_recipient_stats(self) -> dict[str, tuple[int, int | None]]:
        """Per-recipient (pending count, oldest arrival ms), payload-free.

        Older/custom InboxPort implementations may only know the counts; ages
        then degrade to ``None`` instead of failing the whole snapshot.
        """

        assert self._inbox is not None
        summarize = getattr(self._inbox, "pending_recipient_stats", None)
        if callable(summarize):
            return {
                str(recipient): (int(count), int(oldest))
                for recipient, count, oldest in summarize()
            }
        counts = getattr(self._inbox, "pending_recipient_counts", None)
        if not callable(counts):
            return {}
        return {str(recipient): (int(count), None) for recipient, count in counts()}

    def _historical_inbox_recipients(self) -> list[JsonObject]:
        """Report, but never claim, same-name inbox keys under another node.

        A matching owner and actor segment is useful diagnostic evidence, not
        an identity migration proof.  The old recipient therefore remains the
        only key that can read or acknowledge its rows.
        """

        assert self._inbox is not None
        summarize = getattr(self._inbox, "pending_recipient_counts", None)
        if not callable(summarize):
            # Keep older/custom InboxPort implementations compatible with ps.
            return []
        historical: list[JsonObject] = []
        for recipient, pending in summarize():
            parsed_recipient = parse_agent_uri(recipient)
            if (
                parsed_recipient is None
                or parsed_recipient[0] != self.owner
                or parsed_recipient[1] == self.node_id
            ):
                continue
            actor = parsed_recipient[2]
            current = canonical_agent_uri(self.owner, self.node_id, actor)
            try:
                resolved = self._resolve_agent_alias(actor)
            except DaemonRequestError:
                # Ambiguity is exactly where diagnostics must not guess that
                # an old recipient belongs to this actor generation.
                continue
            if resolved != current:
                continue
            historical.append(
                {
                    "recipient": recipient,
                    "currentRecipient": current,
                    "pending": pending,
                }
            )
        return historical

    def _normalize_persisted_interactive_sessions(self) -> None:
        """Upgrade legacy bare desired-state sessions before mesh restore.

        A daemon restart must not briefly re-publish the old bare route while
        its surviving Channel child reconnects.  If legacy state somehow
        contains both spellings, the already-canonical record wins: it is the
        only record whose identity satisfied the post-fix registration
        contract, and restoring both would recreate the defect.
        """

        state = self.desired_state.load()
        normalized: dict[str, InteractiveSession] = {}
        changed = False
        for session in state.interactive_sessions:
            actor = self._canonical_interactive_actor(session.actor)
            candidate = (
                session
                if actor == session.actor
                else replace(session, actor=actor)
            )
            changed = changed or candidate is not session
            existing = normalized.get(actor)
            if existing is None or session.actor == actor:
                normalized[actor] = candidate
            else:
                changed = True
        sessions = tuple(normalized[actor] for actor in sorted(normalized))
        if changed or sessions != state.interactive_sessions:
            self.desired_state.save(replace(state, interactive_sessions=sessions))

    def _resolve_agent_alias(self, target: str) -> str:
        """Resolve a bare short name to one canonical agent URI **by lookup**.

        Two local tables answer, each by exact match, and nothing else is
        consulted:

        - the machine's agent registry (``actor`` is one row per machine), and
        - this daemon's managed streaming harnesses, matched on either the
          managed ``name`` or the operator-facing ``nickname``.  The minted
          candidate is always derived from ``spec.name`` regardless of which
          alias was typed — a connector registers its liveliness token and
          inbox endpoint under the name-minted URI (see
          ``_reconcile_harness_actors``), so minting from the typed nickname
          would recreate the queue-forever trap under a key nobody drains.

        ⛔ Presence and interactive sessions are deliberately NOT scanned.
        Scanning them meant matching on ``owner + actor`` while ignoring
        ``machine``, which was harmless only because the owner segment used to
        differ per host: it came from the local login.  Now that owner is a
        *user* identity — one value across all of a user's machines — a peer
        running the same actor name would have become a second candidate for
        every bare send, including ``hq-adjutant``.  Presence was already
        known to be unsafe in one direction (a sender resolved through it
        could mint a message impersonating a remote actor); a table lookup
        removes that hazard in both directions instead of guarding one.

        ⚠️ More than one candidate stays a loud error, and it is **not** dead
        code even with only two exact lookups: ``_canonical_harness_uri``
        honours ``spec.pinned_owner`` (decision D-D), so a transferred spec
        mints the SOURCE owner while a registry row for the same short name
        mints this daemon's.  That disagreement is real and must never be
        resolved by guessing.

        An unknown bare name is left untouched, so node actors and durable
        queueing keep their existing semantics — ``_destroy_agent_messages``
        drains both spellings precisely because of it.  Targets that already
        carry a scheme pass through untouched.
        """

        if ":" in target:
            return target
        candidates: set[str] = set()
        # Deferred import mirrors _start_runtime: hyprial.harnesses imports
        # hyprial.daemon at module load, so a top-level import would re-enter
        # while either package is partially initialized.
        from hyprial.harnesses import is_streaming_spec

        state = self.desired_state.load()
        for spec in state.harnesses:
            if is_streaming_spec(spec) and target in (spec.name, spec.nickname):
                candidates.add(self._canonical_harness_uri(spec.name, spec))
        # Agent rows retain the canonical key even while a connector is
        # offline, so a compatible bare send queues under the key the next
        # process will drain instead of creating a bare outbox island.
        # ``get`` is machine-safe by construction: ``local_actor`` rejects a
        # URI naming another owner or machine, so no peer row can answer here.
        agent = self.agents.get(target)
        if agent is not None:
            candidates.add(agent.uri)
        if len(candidates) > 1:
            raise DaemonRequestError(
                ipc_errors.AMBIGUOUS_TARGET,
                f"actor name {target!r} matches more than one agent; "
                "address the canonical URI directly",
                {"target": target, "candidates": sorted(candidates)},
            )
        return next(iter(candidates), target)

    def _is_unresolved_local_identity(self, target: str) -> bool:
        """True when a queued target names THIS node but nothing claims it.

        This node is the authority for ``agent:<its owner>:<its node>:…``
        URIs: if no streaming harness, interactive session (either
        spelling), managed worker, or online actor carries the URI, the
        durable queue can never drain it — the silent-loss shape of the
        bare-name/canonical-pin mismatch.  Foreign-namespace URIs and bare
        node-actor names keep their quiet durable-queue semantics.
        """

        parsed_target = parse_agent_uri(target)
        if parsed_target is None:
            return False
        actor_segment = parsed_target[2]
        if parsed_target[0] != self.owner or parsed_target[1] != self.node_id:
            return False
        if self._presence is not None and self._presence.actor_online(target):
            return False
        # Deferred import mirrors _resolve_agent_alias.
        from hyprial.harnesses import is_streaming_spec

        state = self.desired_state.load()
        for spec in state.harnesses:
            if (
                is_streaming_spec(spec)
                and self._canonical_harness_uri(spec.name, spec) == target
            ):
                return False
        for session in self._agent_session_domains.session.read_sessions():
            if session.actor in (target, actor_segment):
                return False
        return self._worker_session_ref(target) is None

    def _restore_persona_routes(self) -> None:
        """(Re)declare daemon-backed presence for every registered agent."""

        for agent in self.agents.list():
            self._declare_persona_route(agent.uri)

    def _declare_persona_route(self, actor_uri: str) -> None:
        """Acquire the durable persona lease on the one physical route."""

        routes = self._routes
        if routes is None:
            return
        routes.ensure(
            self._actor_route_spec(actor_uri),
            owner_lease=f"persona:{actor_uri}",
        )

    def _drop_persona_route(self, actor_uri: str) -> None:
        routes = self._routes
        if routes is None:
            return
        routes.drop(actor_uri, owner_lease=f"persona:{actor_uri}")

    def _ensure_interactive_route(
        self, actor: str, session_ref: str | None = None
    ) -> bool:
        with self._interactive_route_lock:
            return self._ensure_interactive_route_locked(actor, session_ref)

    def _ensure_interactive_route_locked(
        self, actor: str, session_ref: str | None = None
    ) -> bool:
        """Publish and receive for a CC actor distinct from this daemon node."""

        if actor == self.node_id:
            return False
        routes = self._routes
        if routes is None:
            return False
        lease = self._interactive_route_lease(actor, session_ref)
        canonical = classify_target_identity(actor) == TARGET_KIND_AGENT
        completed = routes.ensure(
            self._actor_route_spec(actor, advertise=canonical),
            owner_lease=lease,
        )
        if not canonical:
            # Admission gate, downgrade-don't-reject: a pre-canonical
            # desired-state session can still carry a bare actor name.  The
            # inbox endpoint stays open so in-flight and locally addressed
            # messages keep landing, but a non-canonical actor is never
            # advertised into the agent liveliness keyspace again -- that
            # keyspace is what `targets` reads, and a bare name there is
            # indistinguishable from a node's self-token.
            self._log(
                "warn",
                "daemon",
                "daemon.interactive_route.not_advertised",
                actor=actor,
                targetKind=classify_target_identity(actor),
                detail=(
                    "non-canonical interactive actor receives but is not "
                    "advertised as an agent"
                ),
            )
        return completed.changed

    def _restore_interactive_routes(self) -> None:
        for session in self._agent_session_domains.session.read_sessions():
            # Persisted Channel state is recovery intent, never liveness. The
            # surviving child reopens its route via lease-matched refresh.
            if session.source != "claude-channel":
                self._ensure_interactive_route(session.actor, session.session_ref)

    def _clean_dead_interactive_sessions(self) -> None:
        """Retire only sessions whose PID/birth fence proves the carrier dead."""

        from hyprial.mcp.channel import _owner_process_status

        self._agent_recovery_cleanups.clear()
        sessions = self._agent_session_domains.session.read_sessions()
        for session in sessions:
            if (
                session.source != "codex-app-server"
                or session.process_pid is None
                or session.process_identity is None
            ):
                continue
            verdict = _owner_process_status(
                session.process_pid, session.process_identity
            ).value
            if verdict not in {"pid-missing", "identity-mismatch"}:
                continue
            if session.session_ref is None:
                continue
            completed = self._call_session(
                UnregisterSessionCommand(
                    correlation_id=f"session:recovery-cleanup:{uuid4().hex}",
                    actor=session.actor,
                    session_ref=session.session_ref,
                    manage_agent=self.agents.get(session.actor) is not None,
                )
            )
            if not completed.result.unregistered:
                continue
            cleanup: JsonObject = {
                "source": session.source,
                "harness": _session_harness(session),
                "runtime": session.runtime,
                "sessionId": session.session_ref,
                "processPid": session.process_pid,
                "liveness": verdict,
            }
            self._agent_recovery_cleanups[session.actor] = cleanup
            self._log(
                "info",
                "agents",
                "agent.recovery.cleaned",
                actor=session.actor,
                source=session.source,
                harness=_session_harness(session),
                runtime=session.runtime,
                sessionId=session.session_ref,
                processPid=session.process_pid,
                liveness=verdict,
                detail=(
                    "interactive session registration removed before route and "
                    "binding recovery because PID plus birth identity proved "
                    "the exact carrier process is dead"
                ),
            )

    def _expire_stale_channel_routes(
        self, now: float, sessions: tuple[SessionProjection, ...] | None = None
    ) -> None:
        # Session heartbeat/refresh and route mutation are one local domain
        # transaction.  This narrow lock replaces the removed daemon-wide IPC
        # lock: a stale observation cannot close the route restored by a newer
        # heartbeat, while unrelated workflow/Lark/lifecycle IPC stays free.
        #
        # Callers that already hold a fresh session read for the same request
        # (ps) pass it in: closing routes below never edits the session store,
        # so a pre-read is exactly what the locked section would read itself.
        with self._interactive_route_lock:
            sessions_by_actor = {
                session.actor: session
                for session in (
                    self._agent_session_domains.session.read_sessions()
                    if sessions is None
                    else sessions
                )
                if session.source == "claude-channel"
                and session.channel_lease_backed
            }
            for actor, session in sessions_by_actor.items():
                if not self._channel_alive(session, now):
                    self._close_interactive_route_locked(
                        actor, session.session_ref
                    )

    def _channel_alive(self, session: InteractiveSession, now: float) -> bool:
        del now
        runtime = self._agent_session_domains.session.read_runtime(session.actor)
        lease_backed = bool(
            getattr(
                session,
                "channel_lease_backed",
                getattr(session, "channel_lease_digest", None) is not None,
            )
        )
        return bool(
            lease_backed
            and runtime is not None
            and runtime.current_epoch == self.epoch
            and runtime.alive
        )

    def _close_interactive_route(
        self, actor: str, session_ref: str | None = None
    ) -> None:
        with self._interactive_route_lock:
            self._close_interactive_route_locked(actor, session_ref)

    def _close_interactive_route_locked(
        self, actor: str, session_ref: str | None = None
    ) -> None:
        routes = self._routes
        if routes is None:
            return
        if session_ref is not None:
            owners = (self._interactive_route_lease(actor, session_ref),)
        else:
            owners = tuple(
                owner
                for owner in routes.owners(actor)
                if owner.startswith("session:")
            )
        for owner in owners:
            routes.drop(actor, owner_lease=owner)

    @staticmethod
    def _interactive_route_lease(actor: str, session_ref: str | None) -> str:
        return f"session:{session_ref or f'legacy:{actor}'}"

    @staticmethod
    def _actor_route_spec(actor: str, *, advertise: bool = True) -> RouteSpec:
        keys = KeySpace()
        return RouteSpec(
            route_id=actor,
            liveliness_key=keys.actor_liveliness(actor),
            inbox_key=keys.inbox_all(actor),
            advertise=advertise,
        )

    def _fence_interactive_session(
        self, actor: str, params: JsonObject
    ) -> str:
        """Reject an MCP child after a newer TUI owns the actor registration.

        Returns the canonical actor the request must continue under. An
        owner-segment-only relocation -- the owner migration rewrote this same
        session's stored address -- is NOT a supersede: the surviving carrier
        keeps serving under the migrated (canonical) spelling, so its fetches,
        replies, and acks key on the rows the migration left behind (#352).
        """

        if "sessionRef" not in params:
            return actor
        session_ref = _required_string(params.get("sessionRef"), "sessionRef")
        current = next(
            (
                item
                for item in self._agent_session_domains.session.read_sessions()
                if item.actor == actor
            ),
            None,
        )
        if current is None or current.session_ref != session_ref:
            # A daemon-managed worker owns no interactive session; accept its
            # pre-registered worker session so its own harness_send/read pass.
            if self._worker_session_ref(actor) == session_ref:
                return actor
            if current is not None:
                # A *different* interactive session now owns this actor: a newer
                # register superseded this one (last-writer-wins, keyed by actor).
                # This is a terminal ownership verdict -- the losing stdio Channel
                # child must go quiet, never re-register. A distinct code lets it
                # tell this apart from "no session registered" below, which stays
                # a transient re-register signal so a daemon restart still self
                # heals. Keep in sync with hyprial.mcp.api.SESSION_SUPERSEDED_CODE.
                raise DaemonRequestError(
                    ipc_errors.SESSION_SUPERSEDED,
                    f"actor {actor} is now owned by interactive session "
                    f"{current.session_ref}, not {session_ref}",
                )
            relocated = next(
                (
                    item
                    for item in self._agent_session_domains.session.read_sessions()
                    if item.session_ref == session_ref
                ),
                None,
            )
            if relocated is not None:
                if not owner_only_relocation(actor, relocated.actor):
                    raise DaemonRequestError(
                        ipc_errors.SESSION_SUPERSEDED,
                        f"interactive session {session_ref} moved from actor {actor} "
                        f"to {relocated.actor}",
                    )
                # Same session, owner migration only: continue under the
                # canonical spelling the state now carries.
                return relocated.actor
            raise DaemonRequestError(
                ipc_errors.STALE_SESSION,
                f"interactive session {session_ref} no longer owns actor {actor}",
            )
        # This is an ownership fence, not a liveness signal.  Only the explicit
        # session.heartbeat operation possessing the opaque lease token may
        # renew a carrier; ordinary identity/inbox traffic cannot keep a dead
        # channel online accidentally.
        return actor

    def _require_agent_task_service(self) -> PacAgentTaskService:
        """Bind the frozen facade to the PAC graph store, never workflow."""

        try:
            return PacAgentTaskService(
                state_dir=self.state_dir,
                service_actor=self._dispatch_service_actor,
                delivery_io=self._pac_notification_io,
            )
        except PacAgentTaskError as error:
            raise DaemonRequestError(error.code, str(error), error.data) from error

    def _pac_bound_caller(self, params: JsonObject) -> str:
        """Authenticate the acting PAC principal via the daemon session binding.

        Same fence shape as ``_agent_task_bound_caller``: actor + sessionRef
        must name a binding THIS daemon minted.  PAC owner-only checks
        (G1=A, exact URI equality) run against the verified identity this
        returns -- a presented identity without the binding is a claim,
        never a credential (design-pac-owner-full-uri §5.1).
        """

        if "actor" not in params or "sessionRef" not in params:
            raise DaemonRequestError(
                ipc_errors.CALLER_NOT_AUTHORIZED,
                "pac write methods require an authenticated daemon-managed "
                "session binding (actor + sessionRef)",
            )
        actor = self._mcp_actor(params)
        try:
            actor = self._fence_interactive_session(actor, params)
        except DaemonRequestError as error:
            raise DaemonRequestError(
                ipc_errors.CALLER_NOT_AUTHORIZED,
                "caller does not hold the pac write session binding",
                {"cause": error.code},
            ) from error
        return actor

    def _handle_pac_write(self, method: str, params: JsonObject) -> JsonObject:
        """Fenced PAC write surface (flag set/reset, graph activate/close,
        actor stop) -- the local path that writes PAC state under an agent
        identity. Remote workflow requests use the scoped daemon delegation
        protocol after this same local session fence.  The human CLI path stays local with ``user:<owner>`` from
        the trusted local boundary; agent identities never write the local
        database unverified.
        """

        from hyprial.pac.errors import PacError
        from hyprial.pac.graph import activate_graph, close_graph
        from hyprial.pac.lifecycle import request_actor_stop
        from hyprial.pac.reactor import PacReactor, planned_to_json
        from hyprial.pac.store import PacGraphStore, default_database_path

        from .pac_actor import DaemonPacNotificationSender

        caller = self._pac_bound_caller(params)
        if self._remote_workflow is not None and method in ("pac.flag.set", "pac.flag.reset"):
            graph_id = _required_string(params.get("graphId"), "graphId")
            node_id = _required_string(params.get("nodeId"), "nodeId")
            if self._remote_workflow._lookup(graph_id=graph_id, node_id=node_id, actor=caller) is not None:
                try:
                    remote_params = dict(params)
                    if method == "pac.flag.set":
                        remote_params["requestId"] = _required_string(params.get("expectedRequest"), "expectedRequest")
                        remote_params["reasonRef"] = _required_string(params.get("reasonRef"), "reasonRef")
                    forwarded = self._remote_workflow.forward("workflow.complete" if method == "pac.flag.set" else method,
                                                              remote_params, caller)
                    if forwarded is not None:
                        return forwarded
                except PacError as error:
                    raise DaemonRequestError(error.code, str(error)) from error
        store = PacGraphStore(default_database_path(self.state_dir))
        try:
            if method in ("pac.flag.set", "pac.flag.reset"):
                graph_id = _required_string(params.get("graphId"), "graphId")
                node_id = _required_string(params.get("nodeId"), "nodeId")
                reason_ref = params.get("reasonRef")
                reactor = PacReactor(store, sender=DaemonPacNotificationSender(self))
                try:
                    if method == "pac.flag.set":
                        outcome = reactor.set_flag(
                            graph_id, node_id, actor=caller, reason_ref=reason_ref,
                            expected_request=params.get("expectedRequest")
                        )
                    else:
                        outcome = reactor.reset_flag(
                            graph_id, node_id, actor=caller, reason_ref=reason_ref
                        )
                finally:
                    reactor.close()
                document: JsonObject = {
                    "ok": True,
                    "event": outcome.event,
                    "notifications": [
                        planned_to_json(item) for item in outcome.planned
                    ],
                    "delivered": len(outcome.delivered),
                    "undelivered": len(outcome.undelivered),
                }
                if outcome.delivery_error:
                    document["deliveryError"] = outcome.delivery_error
                return document
            if method == "pac.graph.activate":
                graph_id = _required_string(params.get("graphId"), "graphId")
                return {"ok": True, **activate_graph(store, graph_id, actor=caller)}
            if method == "pac.graph.close":
                graph_id = _required_string(params.get("graphId"), "graphId")
                return {"ok": True, **close_graph(store, graph_id, actor=caller)}
            if method == "pac.actor.stop":
                graph_id = _required_string(params.get("graphId"), "graphId")
                actor_name = _required_string(params.get("actorName"), "actorName")
                request_actor_stop(store, graph_id, actor_name, actor=caller)
                return {
                    "ok": True,
                    "graphId": graph_id,
                    "actorName": actor_name,
                    "desired": "down",
                }
            raise DaemonRequestError(  # pragma: no cover - dispatch guards this
                ipc_errors.METHOD_NOT_FOUND, f"unknown PAC method {method!r}"
            )
        except PacError as error:
            raise DaemonRequestError(error.code, str(error), error.data) from error
        finally:
            store.close()

    def _agent_task_bound_caller(self, params: JsonObject) -> str:
        """Authenticate one daemon-provisioned caller-to-service binding."""

        if "actor" not in params or "sessionRef" not in params:
            raise DaemonRequestError(
                ipc_errors.CALLER_NOT_AUTHORIZED,
                "agent.task requires an authenticated daemon-managed session binding",
            )
        actor = self._mcp_actor(params)
        try:
            actor = self._fence_interactive_session(actor, params)
        except DaemonRequestError as error:
            raise DaemonRequestError(
                ipc_errors.CALLER_NOT_AUTHORIZED,
                "caller does not hold the agent.task service binding",
                {"cause": error.code},
            ) from error
        return actor

    def _interactive_actor_liveness(self, actor: str) -> bool | None:
        """Liveness verdict for an actor this daemon speaks for, or None.

        A7 convergence: this used to be one of three unrelated judgements and
        answered only for interactive-session actors. It now delegates to the
        single AgentLiveness model, which folds in the harness supervisor's
        real subprocess state as well. None still means "not this daemon's to
        judge" -- _LocalPresence then falls back to the raw zenoh token, which
        is the right answer for a genuinely remote peer whose own daemon owns
        that verdict.
        """

        return self._agent_liveness.verdict(actor)

    def _interactive_session_status_projection(
        self, session: Any, now: float
    ) -> JsonObject:
        runtime = self._agent_session_domains.session.read_runtime(session.actor)
        last_observed = None
        if (
            runtime is not None
            and runtime.current_epoch is not None
            and runtime.last_heartbeat_ms is not None
        ):
            last_observed = (
                session.session_ref,
                runtime.current_epoch,
                runtime.last_heartbeat_ms / 1000,
            )
        return _interactive_session_status(
            session,
            current_epoch=None if runtime is None else runtime.current_epoch,
            online=(self._presence.actor_online(session.actor) if self._presence else False),
            last_observed=last_observed,
            now=now,
        )

    # -- Agent entity: registry, bindings and liveness inputs ---------------

    def _registered_interactive_actors(self) -> frozenset[str]:
        """Actors with a persisted interactive session, for the liveness model."""

        return frozenset(
            session.actor
            for session in self._agent_session_domains.session.read_sessions()
        )

    def _runtime_timer(self, observed_at_ms: int) -> Any:
        """One runtime cadence tick inside one worker-status snapshot.

        ``_dispatch_harness_deliveries`` resolves the canonical URI of every
        streaming actor on every tick; outside a snapshot each resolution was
        a full desired-state load (the ~0.6 core left after #787, production
        2026-09-25).  One snapshot per tick makes that one load per tick.
        """

        assert self._runtime is not None
        with self._worker_status_snapshot():
            return self._runtime.on_timer(observed_at_ms)

    def _current_worker_snapshot(self) -> _WorkerStatusSnapshot | None:
        """The snapshot installed for this request thread, when there is one."""

        return getattr(self._worker_snapshot_local, "current", None)

    def _orphan_process_status(self) -> tuple[dict[str, object], ...]:
        status = getattr(self._harnesses, "orphan_status", None)
        return status() if callable(status) else ()

    @contextlib.contextmanager
    def _worker_status_snapshot(self) -> Iterator[_WorkerStatusSnapshot]:
        """Install one shared worker-status snapshot for the calling request.

        The liveness ``worker_running`` callback is injected once at
        construction time, so a per-request snapshot cannot be threaded
        through it as an argument — it is swapped behind this handle instead.
        The handle is a thread-local because IPC clients each serve on their
        own thread: one request's tables never leak into a concurrent
        request's verdicts.
        """

        snapshot = self._build_worker_status_snapshot()
        previous = self._current_worker_snapshot()
        self._worker_snapshot_local.current = snapshot
        try:
            yield snapshot
        finally:
            self._worker_snapshot_local.current = previous

    def _build_worker_status_snapshot(self) -> _WorkerStatusSnapshot:
        """One supervisor round trip + one desired-state load, as two tables.

        Both lookups reproduce their per-call predecessors exactly, including
        first-match-wins ordering: ``running_by_actor`` scans statuses in
        order and claims both the canonical URI and the bare name;
        ``session_ref_by_actor`` scans harness specs in order and keeps the
        first spec's ref even when that ref is absent (None).
        """

        report = getattr(self._harnesses, "status", None)
        statuses = report() if callable(report) else ()
        if not isinstance(statuses, tuple | list):
            statuses = ()
        try:
            desired_loaded = self.desired_state.load()
            specs = desired_loaded.harnesses
        except Exception:  # noqa: BLE001 - mirrors _canonical_harness_uri
            desired_loaded = None
            specs = ()
        # First-by-name, the same lookup _canonical_harness_uri's fallback
        # performs when no spec is passed.
        specs_by_name: dict[str, HarnessLaunchSpec] = {}
        for spec in specs:
            specs_by_name.setdefault(spec.name, spec)
        running_by_actor: dict[str, bool] = {}
        for status in statuses:
            if not isinstance(status, dict):
                continue
            name = status.get("name")
            if not isinstance(name, str) or not name:
                continue
            if status.get("runtime") == "lark":
                continue
            running = bool(status.get("running"))
            running_by_actor.setdefault(
                self._canonical_harness_uri(name, specs_by_name.get(name)), running
            )
            running_by_actor.setdefault(name, running)
        read_refs = getattr(self._harnesses, "projected_worker_session_refs", None)
        refs = read_refs() if callable(read_refs) else {}
        session_ref_by_actor: dict[str, str | None] = {}
        for spec in specs:
            if spec.harness == "lark":
                continue
            session_ref_by_actor.setdefault(
                self._canonical_harness_uri(spec.name, spec),
                refs.get((spec.harness, spec.name)),
            )
        return _WorkerStatusSnapshot(
            statuses=tuple(statuses),
            running_by_actor=running_by_actor,
            session_ref_by_actor=session_ref_by_actor,
            desired=desired_loaded,
        )

    def _managed_worker_running(
        self, actor: str, desired: DesiredState | None = None
    ) -> bool | None:
        """Supervised-subprocess state for a daemon-managed worker, else None.

        This is the signal ``connectors[].running`` already reported on its
        own; feeding it into AgentLiveness is what collapses two of the three
        old models into one instead of adding a fourth.  ``desired`` is the
        snapshot's already-loaded desired state (card 259), threaded to
        ``_canonical_harness_uri`` so each row stops re-reading the store.
        """

        snapshot = self._current_worker_snapshot()
        if snapshot is not None:
            return snapshot.running_by_actor.get(actor)
        # Single-call paths (identity.whoami, session fencing) reach here
        # without a request snapshot and keep the live per-call query.
        # Batch paths must install _worker_status_snapshot instead: every
        # call below costs one supervisor round trip plus one desired-state
        # load per connector.
        # Unit-level application.handle seams stand in a bare object for the
        # supervisor; absent a real one there is simply no worker signal.
        report = getattr(self._harnesses, "status", None)
        if not callable(report):
            return None
        statuses = report()
        if not isinstance(statuses, tuple | list):
            return None
        for status in statuses:
            if not isinstance(status, dict):
                continue
            name = status.get("name")
            if not isinstance(name, str) or not name:
                continue
            if status.get("runtime") == "lark":
                continue
            if (
                self._canonical_harness_uri(name, desired=desired) == actor
                or name == actor
            ):
                return bool(status.get("running"))
        return None

    def _worker_session_ref(self, actor: str) -> str | None:
        """Project a managed worker session ref from the Harness authority."""

        snapshot = self._current_worker_snapshot()
        if snapshot is not None:
            return snapshot.session_ref_by_actor.get(actor)
        harnesses = self._harnesses
        if harnesses is None:
            return None
        read_refs = getattr(harnesses, "projected_worker_session_refs", None)
        if not callable(read_refs):
            return None
        refs = read_refs()
        for spec in self.desired_state.load().harnesses:
            if spec.harness == "lark":
                continue
            if self._canonical_harness_uri(spec.name, spec) == actor:
                return refs.get((spec.harness, spec.name))
        return None

    def _seed_agent_bindings(self) -> None:
        """Re-derive runtime bindings after a restart, before serving requests.

        Bindings are per daemon generation, so without this a restarted daemon
        would forget that ``foo`` is already running headless and let a second
        connector claim the same URI -- the exact collision A1 forbids.
        """

        from hyprial.agents.capabilities import option_value

        state = self.desired_state.load()
        self._agent_recovery_failures.clear()
        for spec in state.harnesses:
            if spec.harness == "lark":
                continue
            self._ensure_agent(
                self._canonical_harness_uri(spec.name, spec),
                harness=spec.harness, interactive=False, cwd=spec.cwd,
                args=spec.args, provider=spec.model_provider, model=spec.model,
            )
            self._agent_liveness.bind(
                self._canonical_harness_uri(spec.name, spec),
                harness=spec.harness,
                runtime=RUNTIME_HEADLESS,
                session_id=spec.session_ref,
            )
        for session in state.interactive_sessions:
            agent = self.agents.get(session.actor)
            self._ensure_agent(
                session.actor, harness=_session_harness(session), interactive=True,
                cwd=session.cwd,
                provider=option_value(session.command, "--provider") or (agent.provider if agent else None),
                model=option_value(session.command, "--model") or (agent.model if agent else None),
            )
            harness = _session_harness(session)
            try:
                self._agent_liveness.bind(
                    session.actor,
                    harness=harness,
                    runtime=RUNTIME_INTERACTIVE,
                    session_id=session.session_ref,
                )
            except DomainCommandError as error:
                # This is the per-agent recovery boundary.  The preceding
                # headless bind is deliberately left in place, so an old
                # interactive-session row cannot evict or stop the running
                # connector.  Other domain failures still abort actor-runtime:
                # a closed/overloaded actor or corrupt global state is not one
                # agent's connector conflict and must remain fail-loud.
                if error.code != AgentAlreadyRunning.code:
                    raise
                failure: JsonObject = {
                    "code": error.code,
                    "errorType": type(error).__name__,
                    "error": str(error),
                    "source": session.source,
                    "harness": harness,
                    "runtime": session.runtime,
                    "sessionId": session.session_ref,
                }
                self._agent_recovery_failures[session.actor] = failure
                self._log(
                    "warn",
                    "agents",
                    "agent.recovery.failed",
                    actor=session.actor,
                    source=session.source,
                    harness=harness,
                    runtime=session.runtime,
                    sessionId=session.session_ref,
                    errorCode=error.code,
                    errorType=type(error).__name__,
                    error=str(error)[:500],
                    detail=(
                        "interactive session recovery was isolated to this "
                        "agent; its persistent record and the live connector "
                        "were left unchanged for operator resolution"
                    ),
                )

    def _agent_name_for(self, actor: str) -> str | None:
        """The machine-local agent name behind an actor, when there is one.

        A four-segment URI minted by another owner or machine is somebody
        else's agent: this daemon holds no record for it and must not invent
        one.
        """

        if actor == self.node_id:
            return None
        return self.agents.local_actor(actor)

    def _ensure_agent(
        self,
        actor: str,
        *,
        harness: str,
        interactive: bool,
        cwd: str | None = None,
        args: tuple[str, ...] = (),
        provider: str | None = None,
        model: str | None = None,
    ) -> Agent | None:
        """Get-or-create this machine's record for ``actor`` (A5).

        ``hyprial start`` reaches agent creation through here, so a connector can
        never come up without an identity behind it; ``hyprial agent create`` is
        the same code path with duplicate-name creation left fatal.
        """

        name = self._agent_name_for(actor)
        if name is None:
            return None
        from hyprial.agents.capabilities import runtime_capabilities

        facts = runtime_capabilities(
            harness, interactive=interactive, provider=provider, model=model, args=args
        )
        agent = self.agents.get(name)
        if agent is None:
            agent = self.agents.create(
                name,
                cwd=cwd,
                capabilities=facts,
                provider=facts["provider"],
                model=facts["model"],
                harness_args={harness: args} if args else None,
                preferred_harness=harness,
            )
            self._log(
                "info", "agents", "agent.created", actor=agent.uri, harness=harness
            )
            self._declare_persona_route(agent.uri)
            # A legacy pin naming this agent becomes migratable the moment
            # the record exists (see _migrate_legacy_channel_pins).
            self._migrate_legacy_channel_pins()
            return agent
        changes: dict[str, Any] = {
            "capabilities": {**agent.capabilities, **facts},
            "provider": facts["provider"],
            "model": facts["model"],
        }
        if cwd is not None and agent.cwd != cwd:
            changes["cwd"] = cwd
        if args and agent.args_for(harness) != args:
            changes["harness_args"] = {**agent.harness_args, harness: args}
        if changes:
            agent = self.agents.save(replace(agent, **changes))
        return agent

    def _bind_agent(
        self,
        actor: str,
        *,
        harness: str,
        runtime: str,
        session_id: str | None,
        cwd: str | None = None,
        args: tuple[str, ...] = (),
        refuse_runtimes: tuple[str, ...] = (RUNTIME_HEADLESS, RUNTIME_INTERACTIVE),
    ) -> HandoverNotice | None:
        """Point an agent at a harness, refusing to attach to a running one.

        Starting a harness for an existing agent is **not** a name collision:
        an agent name is unique because only one Agent record may hold it, and
        that is enforced at creation. ``hyprial start pi --name foo`` on an
        existing ``foo`` is *that same agent* moving to pi -- one agent, one
        identity, a different runtime binding.

        But it may not move while something is still speaking for it. Two
        connectors on one actor is the original defect: both mint the identical
        four-segment URI, dispatch hands every message to whichever sorts
        first, and the other reports online forever while receiving nothing.
        Rather than stopping the incumbent automatically, this refuses and
        names the remedy -- stopping a headless worker out from under its
        in-flight work, or deregistering an interactive session whose window is
        still open, are both worse than an error the user can act on. The move
        is then ``hyprial down`` followed by ``hyprial start``, and A9 reports the
        previous harness and session id on the way back up.

        ``refuse_runtimes`` narrows which incumbents object. ``lifecycle.start``
        objects to any of them. ``session.register`` objects only to a headless
        worker, because interactive-to-interactive is the existing
        supersede-by-actor handoff (last writer wins, loser fenced off with
        SESSION_SUPERSEDED) -- still one connector, and not a path a user's
        ``hyprial start`` reaches without passing the check above first.
        """

        name = self._agent_name_for(actor)
        if name is None:
            # Not a local agent identity (a foreign URI, or this node itself):
            # no record to own, nothing to bind.
            return None
        self._refuse_if_running(
            actor, harness=harness, runtime=runtime, refuse_runtimes=refuse_runtimes
        )
        self._ensure_agent(
            actor, harness=harness, interactive=runtime == RUNTIME_INTERACTIVE,
            cwd=cwd, args=args,
        )
        superseded = self._agent_liveness.displaced_by(
            actor, harness=harness, runtime=runtime
        )
        if superseded is not None:
            # The incumbent is already known dead (the check above passed), so
            # this only clears its leftovers -- a desired-state entry that
            # would otherwise be restored beside the new connector.
            stopped = self._stop_agent_runtime(actor, keep=(harness, runtime))
            self._log(
                "info",
                "agents",
                "agent.binding.superseded",
                actor=actor,
                previous=superseded.to_json(),
                stopped=stopped,
                harness=harness,
                runtime=runtime,
            )
            # The Agent actor independently rechecks live ownership at bind.
            # Retire the proven-dead incumbent after its connector resources
            # are removed so the new typed bind cannot observe an ambiguous
            # unknown process state and conservatively reject the handoff.
            self._agent_liveness.release(actor)
        prior_agent = self.agents.require(name)
        self._agent_liveness.bind(
            actor, harness=harness, runtime=runtime, session_id=session_id
        )
        notice = (
            HandoverNotice(
                actor=prior_agent.actor,
                previous_harness=prior_agent.last_harness,
                previous_session_id=prior_agent.last_session_id,
                next_harness=harness,
            )
            if prior_agent.last_harness is not None
            and prior_agent.last_harness != harness
            else None
        )
        if notice is not None:
            self._log(
                "info",
                "agents",
                "agent.harness.handover",
                actor=actor,
                previousHarness=notice.previous_harness,
                previousSessionId=notice.previous_session_id,
                nextHarness=harness,
            )
        return notice

    def _release_agent_binding(self, actor: str) -> None:
        self._agent_liveness.release(actor)

    def _refuse_if_running(
        self,
        actor: str,
        *,
        harness: str,
        runtime: str,
        refuse_runtimes: tuple[str, ...] = (RUNTIME_HEADLESS, RUNTIME_INTERACTIVE),
    ) -> None:
        """Reject a start that would attach to an agent already being served.

        Deliberately not a uniqueness check -- that lives at agent creation and
        does not consult liveness. This one is purely about the present: is
        something speaking for this actor right now? A dead connector never
        objects, so a crash self-heals without operator action.

        The same harness objects too, not just a different one: ``hyprial start``
        does not attach to a running agent, full stop. Re-running it is an
        error that names the remedy rather than a silent no-op.
        """

        existing = self._agent_liveness.live_binding(actor)
        if existing is None or existing.runtime not in refuse_runtimes:
            return
        raise DaemonRequestError(
            AgentAlreadyRunning.code,
            str(AgentAlreadyRunning(actor, existing, harness)),
            {
                "actor": actor,
                "existing": existing.to_json(),
                "requestedHarness": harness,
                "requestedRuntime": runtime,
            },
        )

    def _stop_agent_runtime(
        self, actor: str, *, keep: tuple[str, str] | None
    ) -> list[str]:
        """Stop every connector speaking for ``actor`` except ``keep``.

        ``keep`` is the ``(harness, runtime)`` about to take over, so a plain
        restart of the same connector is left alone. ``None`` keeps nothing,
        which is what destroy wants.

        An interactive session cannot be killed from here -- the TUI belongs to
        somebody's terminal, not to this daemon -- so it is unregistered and
        its route closed instead. That is not a new mechanism: the losing side
        then gets SESSION_SUPERSEDED on its next fenced call, exactly as it
        already does when a newer session takes the same actor.
        """

        state = self.desired_state.load()
        stopped: list[str] = []
        for spec in state.harnesses:
            if spec.harness == "lark":
                continue
            if self._canonical_harness_uri(spec.name, spec) != actor:
                continue
            if keep == (spec.harness, RUNTIME_HEADLESS):
                continue
            self._run_lifecycle_operation(
                LifecycleOperation.deactivate(
                    f"agent-stop-runtime:{uuid4().hex}",
                    self._lifecycle_spec(spec),
                )
            )
            stopped.append(f"{spec.harness}:{spec.name}")
        interactive = next(
            (
                item
                for item in self._agent_session_domains.session.read_sessions()
                if item.actor == actor
            ),
            None,
        )
        if interactive is not None and (
            keep is None or keep != (_session_harness(interactive), RUNTIME_INTERACTIVE)
        ):
            if interactive.session_ref is not None:
                self._call_session(
                    UnregisterSessionCommand(
                        correlation_id=f"session:stop-runtime:{uuid4().hex}",
                        actor=actor,
                        session_ref=interactive.session_ref,
                        manage_agent=self.agents.get(actor) is not None,
                    )
                )
            self._close_interactive_route(actor, interactive.session_ref)
            stopped.append(f"interactive:{actor}")
        return stopped

    def _actor_status_snapshot(self) -> list[JsonObject]:
        """One canonical actor table for top, targets, and future consumers."""

        assert self._harnesses is not None
        assert self._presence is not None
        # One load per request: the ps/agent handlers install a worker
        # snapshot that already loaded the state (intg U2); reuse it, and load
        # only when this is called bare (card 259 P1 on dev did the load here).
        snapshot = self._current_worker_snapshot()
        desired = (
            snapshot.desired
            if snapshot is not None and snapshot.desired is not None
            else self.desired_state.load()
        )
        sessions = {
            session.actor: session for session in desired.interactive_sessions
        }
        if snapshot is not None:
            # The request snapshot already holds this supervisor read; a
            # second live status() per request is what made targets N x.
            connectors = snapshot.statuses
        else:
            report_connectors = getattr(self._harnesses, "status", None)
            connectors = report_connectors() if callable(report_connectors) else ()

        def status_for_actor(actor: str, online: frozenset[str]) -> str:
            session = sessions.get(actor)
            if (
                session is not None
                and session.source == "claude-channel"
                and session.channel_lease_digest is not None
                and not self._channel_alive(session, self._clock())
            ):
                return "offline"
            agent = self.agents.get(actor)
            if agent is not None:
                return self._registered_agent_status(agent, desired)
            return self._agent_target_status(actor, online, desired)

        return build_actor_status_snapshot(
            owner=self.owner,
            node_id=self.node_id,
            connectors=connectors,
            interactive_sessions=(
                {
                    "actor": session.actor,
                    "runtime": session.runtime,
                    "source": session.source,
                }
                for session in desired.interactive_sessions
            ),
            registered_actors=(agent.uri for agent in self.agents.list()),
            presence_actors=self._presence.online_actors(),
            status_for_actor=status_for_actor,
        )

    def _agent_status_json(self, agent: Agent) -> JsonObject:
        spelling = (
            agent.actor
            if self._agent_liveness.binding(agent.uri) is None
            and self._agent_liveness.binding(agent.actor) is not None
            else agent.uri
        )
        recovery_failure = self._agent_recovery_failures.get(
            agent.uri
        ) or self._agent_recovery_failures.get(agent.actor)
        recovery_cleanup = self._agent_recovery_cleanups.get(
            agent.uri
        ) or self._agent_recovery_cleanups.get(agent.actor)
        return {
            # ``Agent.to_json`` is also the durable/lifecycle round-trip form,
            # so it carries the internal ``entityToken`` incarnation fence;
            # that is authority state, not a public ``ps`` field, and the frozen
            # snapshot contract omits it.
            **{
                key: value
                for key, value in agent.to_json().items()
                if key != "entityToken"
            },
            **self._agent_liveness.snapshot(spelling),
            "status": self._registered_agent_status(agent),
            **(
                {
                    "recoveryStatus": "failed",
                    "recoveryError": dict(recovery_failure),
                }
                if recovery_failure is not None
                else {}
            ),
            **(
                {
                    "recoveryStatus": "cleaned",
                    "recoveryCleanup": dict(recovery_cleanup),
                }
                if recovery_failure is None and recovery_cleanup is not None
                else {}
            ),
        }

    def _gossip_for_startup(self) -> bool:
        gossip = not self.network_isolated and zenoh_environment_flag(
            "HYPRIAL_ZENOH_GOSSIP"
        )
        self._startup_network["gossip"] = gossip
        return gossip

    def _network_isolation_status(self) -> JsonObject:
        """Whether this daemon IS isolated, read off what it did and holds.

        ``effective`` is true only when isolation was requested AND every
        governed path is observably closed: endpoints all loopback, no listen
        derivation, discovery never consulted, gossip off, no forwarding
        sidecar or forwarded endpoints, no usage fetcher.  A path added later
        without a check here leaves ``effective`` unable to vouch for it --
        which is why the checks are listed, not summarised.
        """

        endpoints = (*self.zenoh_listen, *self.zenoh_connect)
        checks = {
            "endpointsLoopbackOnly": all(
                _endpoint_host(endpoint) in _LOOPBACK_HOSTS for endpoint in endpoints
            ),
            "listenNotDerived": not self._startup_network["listenDerived"],
            "discoveryNotConsulted": not self._startup_network["discoveryConsulted"],
            "gossipOff": not self._startup_network["gossip"],
            "forwardingOff": (
                self._forwarding_supervisor is None
                and not self._forwarding_effective
                and not self._forwarding_start_attempted
            ),
            "usageFetchOff": self._usage_cache is None,
        }
        return {
            "requested": self.network_isolated,
            "effective": self.network_isolated and all(checks.values()),
            "checks": checks,
        }

    def _derive_listen_endpoint(self) -> str | None:
        """This node's own tailnet address, when nothing was configured.

        Gated on the same switch as discovery: a deployment that turns peer
        discovery off is managing its topology by hand, and having the daemon
        bind an address nobody asked for would be a surprise in exactly the
        setup that least wants surprises.

        Returns None rather than raising -- an undiscoverable address is a
        node that cannot be reached, which the caller reports, not a node that
        cannot start.
        """

        if not zenoh_environment_flag("HYPRIAL_PEER_DISCOVERY", default=True):
            return None
        # ⛔ Only the node's PRIMARY daemon may claim the node's address.
        #
        # A daemon pointed at a custom home is by definition not that daemon:
        # isolated tests, the E2E gates, a second instance for debugging. Left
        # ungated, every one of them tries to bind this machine's real tailnet
        # address. When the production daemon holds it they die with "address
        # already in use" -- and that is the LUCKY outcome. When it does not,
        # the isolated daemon binds the node's public address successfully and
        # starts answering as this node on the real network, which is a test
        # fixture impersonating production.
        #
        # Which daemon is the node's primary one is decided explicitly, not
        # by whether HYPRIAL_HOME/HARNESS_STATE_DIR happen to be set: the
        # launchd/systemd units set both for the REAL daemon, and keying on
        # their presence left every service-managed daemon without a tailnet
        # listen -- outbound-only and invisible to peers (2026-09-26, a
        # member's macOS node; HQ listened only because it was shell-started).
        #   * HYPRIAL_NETWORK_ISOLATED always wins: never claim the address.
        #   * HYPRIAL_SERVICE_MANAGED=1 (set by the units) is the primary
        #     daemon, whatever home it was pointed at.
        #   * Otherwise only the default home/state is the primary daemon;
        #     a custom one (a debug instance, a fixture) stays off the
        #     node's address.
        if network_isolated_from_environment():
            return None
        if os.environ.get("HYPRIAL_SERVICE_MANAGED") != "1" and _custom_home_or_state():
            return None
        try:
            return local_tailnet_endpoint()
        except Exception as error:  # noqa: BLE001 - startup must not depend on it
            self._log(
                "warn",
                "zenoh",
                "zenoh.listen.derive_failed",
                detail=str(error),
            )
            return None

    def _discover_peer_endpoints(self) -> tuple[str, ...]:
        """Endpoints from forwarding and the peer directory, combined.

        Both sources are on by default and their union is the dial set:
        forwarding endpoints first, directory ones after, no duplicates.
        Either half failing leaves the other intact -- forwarding failures log
        `zenoh.forwarding.*` events and a directory that cannot answer is
        silence by construction (see `discovery.py`).  Nothing here may raise:
        the node's configured endpoints are already sufficient, and a sidecar
        or directory having a bad day must not be able to stop a daemon from
        starting.

        Two switches cut halves away, on purpose:

        * `HYPRIAL_PEER_DISCOVERY=0` turns off the system tailnet directory
          (leaving forwarding endpoints, if configured).  The default is on
          for the same reason it always was: discovery is additive (configured
          endpoints are all kept, so no working node can be cut off) and
          bounded by the tailnet ACL.  It does not create reachability; it
          acts on reachability that already existed.
        * `HYPRIAL_FORWARDING_EXCLUSIVE=1` restores the pre-coexist behaviour
          where configured forwarding is the *only* source: the S5 acceptance
          and the isolated negative controls depend on "sidecar stopped means
          traffic stopped", which a silent host-tailnet fallback would falsify.

        The forwarding sidecar is owned by `ForwardingSidecarSupervisor`,
        which relaunches it under the registered process-lifecycle budget;
        while it is down the forwarding half is simply empty rather than
        fatal.
        """

        forwarding_configured = bool(self._forwarding_environment)
        if forwarding_configured:
            # Synchronous on purpose: Zenoh fixes its connect set when the
            # session opens, so the first attempt belongs on this startup
            # path; every relaunch after a failure rides the scheduler.
            self._start_forwarding_supervisor()
        forwarding: tuple[str, ...] = ()
        backend = self._forwarding_backend()
        if backend is not None:
            forwarding = backend.list_reachable_endpoints()
            self._forwarding_effective = forwarding
        if self._forwarding_dialed is None:
            # What the Zenoh session actually dials is fixed by the FIRST
            # pass -- including the empty answer of a first-start failure,
            # which is exactly the "recovered later, dialed never" gap that
            # must stay visible until a redial closes it; only a successful
            # rebuild (``_redial_forwarding``) advances it after this.
            self._forwarding_dialed = forwarding
        if forwarding_configured and zenoh_environment_flag(
            "HYPRIAL_FORWARDING_EXCLUSIVE", default=False
        ):
            # Negative-control mode: explicitly configured forwarding never
            # falls back to the host's system tailnet.  Otherwise stopping
            # the sidecar could leave the same Zenoh traffic working and
            # make the negative control false.  Coexistence is the default;
            # exclusivity is now an explicit operator choice.
            return forwarding
        if not zenoh_environment_flag("HYPRIAL_PEER_DISCOVERY", default=True):
            return forwarding
        # HYPRIAL_PEER_DISCOVERY_COMMAND selects the escape-hatch backend: any
        # command printing one endpoint per line.  It is what makes a
        # multi-node mesh testable on a single host (the well-known port
        # belongs to the tailscale backend, not to discovery), and it is the
        # seam for a deployment whose directory is neither tailscale nor a
        # static list.
        command = os.environ.get("HYPRIAL_PEER_DISCOVERY_COMMAND", "").strip()
        backend: object
        if command:
            backend = CommandEndpoints(tuple(shlex.split(command)))
        else:
            backend = TailscaleEndpoints()
        try:
            discovered = backend.list_reachable_endpoints()  # type: ignore[attr-defined]
        except Exception as error:  # noqa: BLE001 - startup must not depend on it
            self._log(
                "warn",
                "zenoh",
                "zenoh.discovery.failed",
                detail=str(error),
            )
            discovered = ()
        self._connect_discovered = discovered
        # Forwarding first, directory after: a deliberately pinned peer
        # behaves predictably instead of racing the directory, and a daemon
        # that configured forwarding keeps its sidecar ports dialled first.
        return merge_endpoints(forwarding, discovered)

    def _start_forwarding_supervisor(self) -> None:
        if (
            not self._forwarding_environment
            or self._forwarding_discovery is not None
            or self._forwarding_supervisor is not None
            or self._forwarding_start_attempted
        ):
            return
        self._forwarding_start_attempted = True
        supervisor = ForwardingSidecarSupervisor(
            {**os.environ, **self._forwarding_environment},
            event_log=lambda level, event, **fields: self._log(
                level, "zenoh", event, **fields
            ),
            scheduler=self._maintenance_scheduler,
        )
        self._forwarding_supervisor = supervisor
        supervisor.ensure_started()

    def _forwarding_backend(self) -> ForwardingEndpoints | None:
        """The live forwarding backend, supervisor-owned or test-injected."""

        if self._forwarding_supervisor is not None:
            return self._forwarding_supervisor.endpoints()
        return self._forwarding_discovery

    def _forwarding_status_json(self) -> dict[str, object]:
        """The forwarding half's verdict for status/ps -- never silently absent.

        A forwarding daemon used to have no state surface at all: a dead
        sidecar looked exactly like a healthy one with no peers. The state
        words are the supervisor's (running / degraded / restarting /
        failed); "off" is the not-configured answer, which is a fact about
        this node rather than a missing field.

        ``endpoints`` vs ``dialed`` answers the question the process state
        cannot: Zenoh fixes its connect set when the session opens, so a
        relaunch that changed the local-port set leaves the session dialing
        ports the new child no longer owns until ``_redial_forwarding``
        rebuilds it. Reporting ``running`` alone would masquerade as
        connected; the gap is reported instead (review finding D). The name
        ``restartRequired`` is published and kept: it is True while the
        session does not dial what the sidecar reports -- no session yet, a
        failed rebuild, or a sidecar that currently reports fewer peers.
        """

        policy = self._forwarding_policy.to_json()
        supervisor = self._forwarding_supervisor
        if supervisor is not None:
            status: dict[str, object] = {
                "state": supervisor.state,
                "failures": supervisor.failures,
                "pid": supervisor.current_pid,
            }
        elif self._forwarding_discovery is not None:
            status = {"state": "running", "failures": 0, "pid": None}
        else:
            off: dict[str, object] = {
                "state": "off",
                "failures": 0,
                "pid": None,
                "policy": policy,
            }
            if self._forwarding_unavailable is not None:
                off["unavailable"] = self._forwarding_unavailable
            return off
        status["policy"] = policy
        effective = self._forwarding_effective
        # The session-open capture; before the first discovery pass it is
        # whatever the first pass returned -- including (), which is exactly
        # the "recovered later, dialed never" gap that must stay visible.
        dialed = (
            self._forwarding_dialed
            if self._forwarding_dialed is not None
            else effective
        )
        status["endpoints"] = list(effective)
        status["dialed"] = list(dialed)
        status["restartRequired"] = effective != dialed
        # Who the sidecar reports and whether each is in the session's
        # dialed set, plus the last poll's outcome: the questions "can we see
        # that node?" and "did we dial it?" had no answer before (plan §C).
        backend = self._forwarding_backend()
        poll = getattr(backend, "last_poll", None) if backend is not None else None
        if isinstance(poll, dict):
            peers = poll.get("peers")
            peer_endpoints = peers if isinstance(peers, dict) else {}
            status["lastPoll"] = {
                "atMs": poll.get("atMs"),
                "ok": poll.get("ok"),
                "error": poll.get("error"),
                "peersReported": poll.get("peersReported"),
                "peerCount": len(peer_endpoints),
            }
            status["peers"] = [
                {"peer": peer, "endpoint": endpoint, "dialed": endpoint in dialed}
                for peer, endpoint in sorted(peer_endpoints.items())
            ]
        return status

    def _reconcile_forwarding_endpoints(self) -> None:
        backend = self._forwarding_backend()
        if backend is None:
            return
        previous = self._forwarding_effective
        current = backend.list_reachable_endpoints()
        self._forwarding_effective = current
        self._redial_forwarding(current)
        if current == previous:
            return
        # Reported after the redial attempt, so ``restartRequired`` is the
        # outcome: False once the session dials the new set, True while it
        # still cannot (no session yet, or the rebuild failed).
        self._log(
            "warn",
            "zenoh",
            "zenoh.forwarding.changed",
            previous=list(previous),
            current=list(current),
            restartRequired=current != self._forwarding_dialed,
        )

    def _redial_forwarding(self, current: tuple[str, ...]) -> None:
        """Rebuild the session to dial ``current`` when it names a new endpoint.

        Zenoh fixes its connect set when the session opens, so a peer the
        sidecar mapped later -- or a relaunch that re-mapped every port -- was
        never dialed until a daemon restart (plan §C, step 3). Only a NEW
        endpoint triggers a rebuild: a pure removal (the sidecar died and
        reports nothing) leaves Zenoh retrying a dead port, which is harmless,
        instead of costing two interruptions per sidecar blip. The configured
        and host-tailnet endpoints are recomposed in startup order, so a
        redial never drops them. Each distinct set is attempted once; a failed
        rebuild restores the old session and stays visible as
        ``restartRequired`` until the set changes again.
        """

        dialed = self._forwarding_dialed or ()
        if not set(current) - set(dialed):
            return
        transport = self._transport
        if transport is None or current == self._forwarding_redial_attempted:
            return
        self._forwarding_redial_attempted = current
        connect = merge_endpoints(
            self._connect_configured, current, self._connect_discovered
        )
        started_at = time.monotonic()
        try:
            transport.reconfigure_connect(connect)
        except Exception as error:  # noqa: BLE001 - the old session is restored
            self._log(
                "warn",
                "zenoh",
                "zenoh.forwarding.redial_failed",
                dialed=list(dialed),
                wanted=list(current),
                detail=str(error)[:300],
            )
            return
        self._forwarding_dialed = current
        self.zenoh_connect = connect
        self._log(
            "info",
            "zenoh",
            "zenoh.forwarding.redialed",
            previous=list(dialed),
            dialed=list(current),
            connect=len(connect),
            durationMs=int((time.monotonic() - started_at) * 1000),
        )

    def _agent_target_status(
        self, actor: str, online: frozenset[str], desired: DesiredState | None = None
    ) -> str:
        """The real online/offline verdict behind a targets entry.

        Replaces a hard-coded ``"online"`` literal that was built from a list
        of online actors and therefore could not report offline even in
        principle. Presence membership is still what answers for a remote peer
        -- its own daemon owns that judgement -- but a locally registered agent
        now gets this machine's real verdict, which can and does say offline.
        """

        status = self._agent_liveness.status(actor, desired_state=desired)
        if status is not None:
            return status
        return "online" if actor in online else "offline"

    def _registered_agent_status(
        self, agent: Agent, desired: DesiredState | None = None
    ) -> str:
        """Verdict for a registry agent, tolerating either identity spelling.

        A connector normally registers under the canonical four-segment URI,
        but the daemon has always accepted a bare actor name too, and the
        binding is recorded under whichever spelling arrived. Checking both
        keeps a bare-name session from being reported offline while it is
        demonstrably alive.
        """

        for spelling in (agent.uri, agent.actor):
            status = self._agent_liveness.status(spelling, desired_state=desired)
            if status is not None:
                return status
        return "offline"

    # -- Worker transfer (design: notes/design-hyprial-transfer.md) -------------
    #
    # P0 cold migration: quiesce on the source, receive on the target, resume
    # as the source-side rollback, complete as the source-side cleanup once
    # the target ACKs.  Strict resume inverts #190's quiet cold-start
    # fallback ON PURPOSE for this one path: a transfer that cannot resume
    # has lost the conversation it promised to carry, so it fails and rolls
    # back instead of degrading silently.

    def _transfer_desired_spec(self, params: JsonObject) -> HarnessLaunchSpec:
        raw_harness = params.get("provider")
        harness = (
            _required_string(raw_harness, "provider")
            if raw_harness is not None
            else None
        )
        name = _required_string(params.get("name"), "name")
        state = self.desired_state.load()
        matches = [
            spec
            for spec in state.harnesses
            if spec.name == name and (harness is None or spec.harness == harness)
        ]
        if not matches:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_WORKER_NOT_FOUND,
                f"no managed harness named {name!r} in desired state",
            )
        if len(matches) > 1:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_AMBIGUOUS,
                f"more than one managed harness named {name!r}; "
                "pass --harness to disambiguate",
            )
        spec = matches[0]
        if spec.harness not in TRANSFERABLE_HARNESSES:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                f"{spec.harness} workers cannot be transferred: P0 supports "
                "pi, codex, and claude headless workers only (dsh has no "
                "resume by design; lark is an adapter, not a worker)",
            )
        # Deferred import: hyprial.harnesses re-enters hyprial.daemon at module load.
        from hyprial.harnesses import is_streaming_spec

        if not is_streaming_spec(spec):
            raise DaemonRequestError(
                ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                f"{spec.harness}:{spec.name} is not a streaming headless "
                "worker; only streaming workers carry a resumable sessionRef",
            )
        return spec

    def _actor_pending_count(self, actor_uri: str, name: str) -> int | None:
        """Undrained inbox rows under both spellings; None when inbox is down."""

        if self._inbox is None:
            return None
        count = 0
        for key in {actor_uri, name}:
            count += len(self._inbox.pending_messages(key))
        return count

    def _transfer_payload(self, spec: HarnessLaunchSpec) -> JsonObject:
        agent = self.agents.get(spec.name)
        actor_uri = self._canonical_harness_uri(spec.name, spec)
        return {
            "ok": True,
            "spec": spec.to_json(),
            "actor": actor_uri,
            "nodeId": self.node_id,
            "owner": self.owner,
            # Same strip as ``_agent_status_json``: ``Agent.to_json`` carries
            # the internal ``entityToken`` incarnation fence, which is
            # authority state, not a public IPC field.  The receiving side
            # never consumes it — ``transfer.receive`` mints a fresh hosted
            # incarnation — so removing it leaks nothing and breaks nothing.
            "agent": (
                {
                    key: value
                    for key, value in agent.to_json().items()
                    if key != "entityToken"
                }
                if agent is not None
                else None
            ),
            "unreadInbox": self._actor_pending_count(actor_uri, spec.name),
        }

    def _transfer_plan(self, params: JsonObject) -> JsonObject:
        """Read-only transfer inspection: the payload a quiesce would snapshot."""

        return self._transfer_payload(self._transfer_desired_spec(params))

    def _transfer_quiesce(self, params: JsonObject) -> JsonObject:
        """Stop the worker and snapshot everything the target needs.

        The session ref is force-synced BEFORE the snapshot so the payload
        carries the freshest ref, not the last reconcile tick's.  Removal
        mirrors ``down``: process stopped, desired-state entry dropped,
        liveness binding released.  The agent row and inbox rows stay --
        they leave only in ``transfer.complete``, after the target ACKs.
        """

        # Validate the target before asking the actor for a node-wide write-back.
        self._transfer_desired_spec(params)
        assert self._harnesses is not None
        self._harnesses.reconcile_session_refs()
        spec = self._transfer_desired_spec(params)
        operation_id = str(
            params.get("operationId") or f"transfer-quiesce:{uuid4().hex}"
        )
        self._run_lifecycle_operation(
            LifecycleOperation.deactivate(
                operation_id,
                self._lifecycle_spec(spec),
            )
        )
        payload = self._transfer_payload(spec)
        payload["stopped"] = True
        self._log(
            "info",
            "transfer",
            "transfer.quiesced",
            actor=payload["actor"],
            sessionRef=spec.session_ref,
        )
        return payload

    def _transfer_precheck(self, params: JsonObject) -> JsonObject:
        """Target-side admission: identity facts plus every name conflict."""

        harness = _required_string(params.get("provider"), "provider")
        name = _required_string(params.get("name"), "name")
        if harness not in TRANSFERABLE_HARNESSES:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                f"{harness} workers cannot be transferred: P0 supports pi, "
                "codex, and claude headless workers only",
            )
        conflicts: list[str] = []
        # ⚠️ PREMISE, load-bearing and easy to break silently: all three
        # sources below are LOCAL TO THIS NODE.  The comparisons match on the
        # actor name alone — neither ``<owner>`` nor ``<machine>`` — which is
        # correct only because "does this node already speak for this name?"
        # is exactly the question, and nothing here can see another node.
        #
        # ⛔ It fails like this: give any of these three a cross-machine
        # source (a peer-aware session view, a registry that federates, a
        # desired-state that carries other nodes' harnesses) and a name in use
        # on a *peer* starts reporting as a local conflict — blocking a
        # transfer that is perfectly legal here.  Nothing would go red; the
        # name match would simply start matching more.
        #
        # This is the same shape as the defect ``_resolve_agent_alias`` just
        # had: a comparison that ignores ``<machine>`` and is safe only while
        # something unwritten keeps peers out of its inputs.  There the
        # protection was "owner happens to differ per host" and owner
        # unification removed it.  Here the protection is "these sources
        # happen to be local" — so it is written down.
        state = self.desired_state.load()
        for spec in state.harnesses:
            if spec.name == name:
                conflicts.append(f"managed harness {spec.harness}:{spec.name}")
        if self.agents.exists(name):
            conflicts.append(f"registered agent {name!r}")
        for session in self._agent_session_domains.session.read_sessions():
            parsed = parse_agent_uri(session.actor)
            if parsed is not None and parsed[2] == name:
                conflicts.append(f"interactive session {session.actor}")
        return {
            "ok": True,
            "nodeId": self.node_id,
            "owner": self.owner,
            "conflicts": conflicts,
        }

    def _transfer_receive(self, params: JsonObject) -> JsonObject:
        """Adopt a transferred worker: identity, spec, pins, strict resume.

        Failure at ANY point undoes every trace (desired-state entry, agent
        row, pins, persona route, liveness binding) so a rejected receive
        leaves the target exactly as it was -- the source still holds the
        worker and rolls back cleanly.
        """

        spec = HarnessLaunchSpec.from_json(params.get("spec"), "spec")
        if spec.harness not in TRANSFERABLE_HARNESSES:
            raise DaemonRequestError(
                ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                f"{spec.harness} workers cannot be transferred: P0 supports "
                "pi, codex, and claude headless workers only",
            )
        from hyprial.harnesses import is_streaming_spec

        if not is_streaming_spec(spec):
            raise DaemonRequestError(
                ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
                f"{spec.harness}:{spec.name} is not a streaming headless worker",
            )
        state = self.desired_state.load()
        if any(existing.name == spec.name for existing in state.harnesses):
            raise DaemonRequestError(
                ipc_errors.TRANSFER_CONFLICT,
                f"a managed harness named {spec.name!r} already exists on "
                f"{self.node_id}",
            )
        if self.agents.exists(spec.name):
            raise DaemonRequestError(
                ipc_errors.TRANSFER_CONFLICT,
                f"the name {spec.name!r} is already taken on {self.node_id}",
            )
        raw_pins = params.get("pins", [])
        if not isinstance(raw_pins, list) or any(
            not isinstance(item, str) or not item for item in raw_pins
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "pins must be an array of adapter names"
            )
        timeout_raw = params.get("strictTimeoutSeconds", 90.0)
        if isinstance(timeout_raw, bool) or not isinstance(
            timeout_raw, (int, float)
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "strictTimeoutSeconds must be a number"
            )
        if spec.containerized:
            # Container mode (docs/design-transfer-container.md): load the
            # staged image tar when present (docker save chain, D-C),
            # create the per-worker credential volume, and shred the
            # staging dir -- the credential residual window on disk is
            # this call itself (D-A).
            from hyprial.transfer import container as xfer_container

            credentials = params.get("credentials", True) is not False
            staging = xfer_container.staging_dir(self.state_dir, spec.name)
            if credentials and not staging.is_dir():
                raise DaemonRequestError(
                    ipc_errors.TRANSFER_CREDENTIALS,
                    f"no credential staging at {staging}; the orchestrator "
                    "ships the bundle before receive",
                )
            try:
                xfer_container.prepare_worker(
                    xfer_container.DockerRunner(),
                    image=spec.container_image or xfer_container.default_image(),
                    name=spec.name,
                    staging=staging,
                    state_dir=self.state_dir,
                    harness=spec.harness,
                    credentials=credentials,
                )
            except xfer_container.ContainerError as error:
                raise DaemonRequestError(error.code, str(error)) from error
        actor_uri = self._canonical_harness_uri(spec.name, spec)
        operation_id = str(
            params.get("operationId") or f"transfer-receive:{uuid4().hex}"
        )
        # Decision A: receive is the only implemented authority for hosting a
        # pinned owner. Insert before lifecycle bind/release can resolve its
        # URI. A plain create/start must never manufacture this authority.
        if spec.pinned_owner is not None:
            self.agents.create_transfer_hosted(
                spec.name, pinned_owner=spec.pinned_owner, cwd=spec.cwd,
                harness_args={spec.harness: spec.args},
                preferred_harness=spec.harness,
            )
        try:
            self._run_lifecycle_operation(
                LifecycleOperation.create(
                    operation_id,
                    self._lifecycle_spec(spec),
                ),
                timeout=max(70.0, float(timeout_raw) + 5.0),
            )
            for adapter in raw_pins:
                # A re-pin is a MOVE by registry semantics: without this gate
                # a transfer would silently steal the adapter from whichever
                # agent holds it on the target.  Refuse and name the holder.
                holder = self.agents.pins().get(adapter)
                if holder is not None and holder != actor_uri:
                    raise DaemonRequestError(
                        ipc_errors.TRANSFER_PIN_CONFLICT,
                        f"adapter {adapter!r} is already pinned to {holder} "
                        f"on {self.node_id}; unpin it there first",
                    )
                try:
                    self.agents.pin(adapter, spec.name)
                except PinConflictError as error:
                    raise DaemonRequestError(
                        ipc_errors.TRANSFER_PIN_CONFLICT, str(error)
                    ) from error
            assert self._harnesses is not None
            if spec.session_ref is not None:
                ready = self._harnesses.wait_ready(
                    spec.harness, spec.name, float(timeout_raw)
                )
                if not ready:
                    raise DaemonRequestError(
                        ipc_errors.STRICT_RESUME_FAILED,
                        f"{spec.harness}:{spec.name} did not become ready "
                        f"within {float(timeout_raw)}s on {self.node_id}; "
                        "the transferred session could not be resumed",
                    )
                resumed = self._harnesses.session_refs().get(
                    (spec.harness, spec.name)
                )
                if resumed != spec.session_ref:
                    raise DaemonRequestError(
                        ipc_errors.STRICT_RESUME_FAILED,
                        f"resume of session {spec.session_ref!r} did not "
                        f"hold on {self.node_id}: the worker established "
                        f"{resumed!r} instead (a cold start would silently "
                        "lose the transferred conversation)",
                    )
        except BaseException:
            self._transfer_undo_receive(spec, actor_uri)
            raise
        self._log(
            "info",
            "transfer",
            "transfer.received",
            actor=actor_uri,
            sessionRef=spec.session_ref,
        )
        return {
            "ok": True,
            "actor": actor_uri,
            "nodeId": self.node_id,
            "sessionRef": spec.session_ref,
            "operationId": operation_id,
        }

    def _require_resumable_session(self, spec: HarnessLaunchSpec, session_ref: str) -> None:
        """Refuse a resume whose transcript the harness would not find.

        Checked BEFORE anything starts, in the same HOME the daemon hands its
        children: pi given an unknown ``--session-id`` warns and begins a
        fresh session under that very id, so the post-start id comparison in
        :meth:`_verify_started_resume` passes on a cold start.  For pi this
        file check is the gate that holds.
        """

        if spec.harness not in TRANSFERABLE_HARNESSES or not spec.headless:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"resuming a session is supported for headless "
                f"{', '.join(sorted(TRANSFERABLE_HARNESSES))} only, not "
                f"{spec.harness}{'' if spec.headless else ' (interactive)'}",
            )
        if spec.cwd is not None:
            cwd = spec.cwd
        else:
            try:
                cwd = str(self._agent_registry.ensure_workspace(spec.name))
            except RegistryHomeError as error:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"cannot resolve default workspace for {spec.name}: {error}",
                ) from error
        runtime_context = None
        agent = self.agents.get(spec.name)
        if agent is not None and agent.config is not None:
            from hyprial.agents.config import AgentConfigError
            from hyprial.agents.home import AgentHomeError
            from hyprial.agents.runtime import (
                DEFAULT_AGENT_TOOL_PROFILE,
                AgentRuntimeError,
                resolve_agent_runtime_context,
            )

            try:
                runtime_context = resolve_agent_runtime_context(
                    registry=self._agent_registry,
                    agent_name=agent.actor,
                    harness=spec.harness,
                    cwd=cwd,
                    tool_profile=DEFAULT_AGENT_TOOL_PROFILE,
                    containerized=spec.containerized,
                )
            except (AgentConfigError, AgentHomeError, AgentRuntimeError) as error:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"cannot resolve agent-home P2 session root for "
                    f"{spec.harness}:{spec.name}: {error}",
                ) from error
            if runtime_context is None:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"agent-home P2 session root is unavailable for "
                    f"{spec.harness}:{spec.name}",
                )
        try:
            agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
            if runtime_context is None and spec.harness == "pi" and agent_dir:
                located = pi_session_file(Path(agent_dir).expanduser(), cwd, session_ref)
            else:
                located = locate_session_file(
                    spec.harness,
                    cwd,
                    session_ref,
                    home=Path.home(),
                    runtime_context=runtime_context,
                )
            if not located.is_file():
                raise SessionFileNotFound(f"not a regular session file: {located}")
        except SessionFileError as error:
            raise DaemonRequestError(
                ipc_errors.RESUME_SESSION_NOT_FOUND,
                f"cannot resume {spec.harness} session {session_ref!r} for "
                f"{spec.name}: {error}; nothing was started",
                {
                    "harness": spec.harness,
                    "name": spec.name,
                    "sessionRef": session_ref,
                    "cwd": cwd,
                    "reason": (
                        "not-found"
                        if isinstance(error, SessionFileNotFound)
                        else "unusable"
                    ),
                },
            ) from error

    def _verify_started_resume(
        self, spec: HarnessLaunchSpec, session_ref: str, *, timeout: float
    ) -> None:
        """The started worker must be ON the requested session, or not run.

        Same check as transfer.receive's strict resume.  On failure the worker
        is deactivated: an error with a fresh-session worker still running
        behind it under the same name is the one outcome worse than either.
        """

        assert self._harnesses is not None
        ready = self._harnesses.wait_ready(spec.harness, spec.name, timeout)
        resumed = (
            self._harnesses.session_refs().get((spec.harness, spec.name))
            if ready
            else None
        )
        if ready and resumed == session_ref:
            return
        self._run_lifecycle_operation(
            LifecycleOperation.deactivate(
                f"lifecycle-start-resume-undo:{uuid4().hex}",
                self._lifecycle_spec(spec),
            )
        )
        detail = (
            f"did not become ready within {timeout}s"
            if not ready
            else f"established session {resumed!r} instead"
        )
        raise DaemonRequestError(
            ipc_errors.STRICT_RESUME_FAILED,
            f"resume of {spec.harness} session {session_ref!r} for {spec.name} "
            f"did not hold: the worker {detail}; it was stopped rather than "
            "left running on a fresh session",
            {
                "harness": spec.harness,
                "name": spec.name,
                "sessionRef": session_ref,
                "established": resumed,
            },
        )

    def _transfer_undo_receive(self, spec: HarnessLaunchSpec, actor_uri: str) -> None:
        """Compensate a failed receive through the same durable saga owner.

        REMOVE (not DEACTIVATE) erases the pre-inserted hosted row and its
        cascading pins even when CREATE reused that row. This closes the
        agent-row residue in b39d7d26; down deliberately keeps the entity.
        """

        del actor_uri
        self._run_lifecycle_operation(
            LifecycleOperation.remove(
                f"transfer-undo:{uuid4().hex}",
                self._lifecycle_spec(spec),
            )
        )
        if spec.containerized:
            self._retire_container_artifacts(spec.harness, spec.name)

    def _retire_container_artifacts(
        self, harness: str, name: str
    ) -> list[str]:
        """Decision D-A: retirement removes containers, the volume, the home.

        Best effort and loud: leftovers come back in the result (and the
        log) instead of masking the operation that triggered the cleanup.
        """

        from hyprial.transfer import container as xfer_container

        try:
            problems = xfer_container.prune_worker(
                xfer_container.DockerRunner(),
                harness=harness,
                name=name,
                state_dir=self.state_dir,
            )
        except Exception as error:  # noqa: BLE001 - report, never mask
            problems = [str(error)]
        if problems:
            self._log(
                "warning",
                "transfer",
                "container.retire.leftovers",
                harness=harness,
                name=name,
                leftovers=problems,
            )
        return problems

    def _transfer_resume(self, params: JsonObject) -> JsonObject:
        """Source-side rollback: put the quiesced worker back exactly as it was.

        Deliberately NOT strict: this path restores the operator's pre-transfer
        state on the machine where the session files never left, so a dead ref
        degrades to #190's ordinary cold-start fallback rather than compounding
        the original failure.
        """

        spec = HarnessLaunchSpec.from_json(params.get("spec"), "spec")
        actor_uri = self._canonical_harness_uri(spec.name, spec)
        prior_agent = self.agents.get(actor_uri)
        operation_id = str(
            params.get("operationId") or f"transfer-resume:{uuid4().hex}"
        )
        result = self._run_lifecycle_operation(
            LifecycleOperation.create(
                operation_id,
                self._lifecycle_spec(spec),
            )
        )
        handover = (
            HandoverNotice(
                actor=prior_agent.actor,
                previous_harness=prior_agent.last_harness,
                previous_session_id=prior_agent.last_session_id,
                next_harness=spec.harness,
            )
            if prior_agent is not None
            and prior_agent.last_harness is not None
            and prior_agent.last_harness != spec.harness
            else None
        )
        assert self._harnesses is not None
        resumed = self._harnesses.session_refs().get((spec.harness, spec.name))
        self._log(
            "info",
            "transfer",
            "transfer.rolled_back",
            actor=actor_uri,
            sessionRef=resumed,
        )
        return {
            "ok": True,
            "actor": actor_uri,
            "changed": bool(result.completed_effects),
            "operationId": operation_id,
            "sessionRef": resumed,
            **(
                {"harnessHandover": handover.to_json()}
                if handover is not None
                else {}
            ),
        }

    def _transfer_complete(self, params: JsonObject) -> JsonObject:
        """Source-side cleanup after the target ACKs: retire the old identity.

        Gentler than ``agent.destroy`` on purpose: pending inbox rows are NOT
        drained -- they remain readable under the old URI (P0's keep-inbox
        semantics; forwarding is P1).  The agent row's deletion cascades the
        pins, and the persona route leaves so the old URI stops promising
        delivery it can no longer drain into a live worker.
        """

        name = self.agents.normalize_actor(
            _required_string(params.get("name"), "name")
        )
        agent = self.agents.get(name)
        if agent is None:
            return {"ok": True, "removedAgent": False}
        actor = agent.uri
        unpinned = sorted(agent.pinned_adapters)
        requested_harness = params.get("provider")
        harness = (
            requested_harness
            if isinstance(requested_harness, str) and requested_harness
            else agent.last_harness or "pi"
        )
        operation_id = str(
            params.get("operationId") or f"transfer-complete:{uuid4().hex}"
        )
        self._run_lifecycle_operation(
            LifecycleOperation.remove(
                operation_id,
                self._lifecycle_spec(
                    HarnessLaunchSpec(harness, name, True)
                ),
            )
        )
        removed = self.agents.get(name) is None
        # D-A: the worker moved away -- its container artifacts (labelled
        # containers, credential volume, worker home) retire with it.
        container_leftovers: list[str] = []
        if isinstance(requested_harness, str) and requested_harness:
            container_leftovers = self._retire_container_artifacts(
                requested_harness, name
            )
        self._log(
            "info",
            "transfer",
            "transfer.completed",
            actor=actor,
            unpinnedAdapters=unpinned,
        )
        return {
            "ok": True,
            "removedAgent": removed,
            "operationId": operation_id,
            "actor": actor,
            "unpinnedAdapters": unpinned,
            "unreadInbox": self._actor_pending_count(actor, name),
            **(
                {"containerLeftovers": container_leftovers}
                if container_leftovers
                else {}
            ),
        }

    def _destroy_agent(self, name: str) -> JsonObject:
        """Delete an agent outright: record, connectors, routes and messages.

        Irreversible by decision A6 -- no tombstone, no revival path. The cost
        is recorded rather than hidden: messages already addressed to this
        agent lose a resolvable recipient.
        """

        agent = self.agents.require(name)
        actor = agent.uri
        workspace = self._agent_registry.workspace_summary(name)
        stopped = self._stop_agent_runtime(actor, keep=None)
        self._drop_persona_route(actor)
        self._release_agent_binding(actor)
        destroyed_messages = self._destroy_agent_messages(agent)
        # Snapshot the pins for the report; the deletion itself needs no pin
        # code at all -- ON DELETE CASCADE erases them in the same
        # transaction that removes the agent row.
        unpinned = sorted(agent.pinned_adapters)
        removed = self.agents.destroy(name)
        workspace_deleted = workspace.exists and not Path(workspace.path).exists()
        self._log(
            "warn",
            "agents",
            "agent.destroyed",
            actor=actor,
            stopped=stopped,
            destroyedMessages=destroyed_messages,
            unpinnedAdapters=unpinned,
        )
        return {
            "ok": True,
            "destroyed": removed,
            "actor": actor,
            "agent": name,
            "stopped": stopped,
            "destroyedMessages": destroyed_messages,
            "unpinnedAdapters": unpinned,
            "irreversible": True,
            "workspace": {
                **workspace.to_json(),
                "deleted": workspace_deleted,
            },
        }

    def _destroy_agent_messages(self, agent: Agent) -> int:
        """Discard this agent's undelivered messages through the inbox's own API.

        Both spellings are drained, and that is not belt-and-braces. A send
        addressed to the canonical URI stores the URI verbatim
        (``normalize_agent_recipient`` only strips the two-segment display
        form), but ``_resolve_agent_alias`` leaves an *unresolvable* bare name
        untouched -- so a message sent to ``foo`` while nothing was running
        under that name is stored under ``foo``, not under its URI. Draining
        only one spelling would report success while leaving the other queue
        addressed to an agent that no longer exists.

        Only the reachable half of A6: the delivery line owns message storage
        and is being rewritten in parallel, so this deliberately does not reach
        into its schema. Draining pending messages leaves nothing routable to a
        name that no longer exists; row-level erasure of consumed history needs
        a purge entry point on the inbox service that does not exist yet.
        """

        if self._inbox is None:
            return 0
        discarded = 0
        for spelling in dict.fromkeys((agent.uri, agent.actor)):
            for message in self._inbox.pending_messages(spelling):
                if self._inbox.ack(spelling, message.message_id).acknowledged:
                    discarded += 1
        return discarded

    def _carrier_session(
        self, actor: str, session_ref: str
    ) -> Any | None:
        """The persisted session one carrier-source caller owns.

        The closed carrier source set (claude-channel, pi-extension,
        codex-app-server -- hyprial.contracts.session.SESSION_CARRIER_SOURCES)
        shares the refresh/heartbeat protocol; only claude-channel sessions
        additionally carry an operational liveness lease.
        """

        return next(
            (
                session
                for session in self._agent_session_domains.session.read_sessions()
                if session.actor == actor
                and session.session_ref == session_ref
                and session.source in SESSION_CARRIER_SOURCES
            ),
            None,
        )

    def _call_session(self, command: object) -> SessionMutationCompleted:
        try:
            return self._agent_session_domains.call_session(
                command, SessionMutationCompleted
            )
        except DomainCommandError as error:
            raise DaemonRequestError(error.code, error.detail) from error

    def _call_lark_reload(self) -> Any:
        assert self._lark_client is not None
        try:
            return self._lark_client.reload()
        except (DomainCommandError, ValueError) as error:
            raise DaemonRequestError(
                ipc_errors.ADAPTER_RELOAD_FAILED, str(error)
            ) from error

    def _lark_gateway_names(self) -> tuple[str, ...]:
        if self._lark_client is not None:
            return tuple(
                sorted(item.name for item in self._lark_client.read_adapters())
            )
        # Unit-level composition doubles may expose only the immutable name
        # projection. Production always installs `_lark_client`.
        names = getattr(self._adapters, "gateway_names", ())
        return tuple(sorted(str(item) for item in names))

    def _channel_current_epoch(
        self, actor: str, session_ref: str
    ) -> str | None:
        runtime = self._agent_session_domains.session.read_runtime(actor)
        if runtime is None or runtime.session_ref != session_ref:
            return None
        return runtime.current_epoch

    def _acquire_lock(self) -> None:
        # A previous daemon for this state directory releases this flock only at
        # the very end of _close(), *after* the control socket it advertises has
        # already been unlinked. `hyprial daemon stop` reports success as soon as
        # that socket disappears (cli.py) and IsolatedDaemon.sigterm() likewise
        # only waits for socket-absence, so a rapid restart routinely overtakes
        # the old process's teardown. Failing immediately on that overlap
        # surfaced as DAEMON_START_FAILED on the third rapid restart in E2E-006.
        # Wait for the lock to become free (bounded) instead of losing the race.
        timeout = _lock_wait_timeout()
        try:
            fence = DaemonStateOwnershipFence.acquire(
                self.state_dir, timeout=timeout
            )
        except DaemonOwnershipBusy as error:
            raise RuntimeError(
                "daemon lock for this state directory is still held after "
                f"{timeout:g}s; another daemon is running or a previous "
                "one has not finished shutting down"
            ) from error
        self._lock_stream = fence.detach()

    def _write_pid_file(self) -> None:
        path = self.state_dir / "daemon.json"
        temporary = self.state_dir / f".daemon.json.{os.getpid()}.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "pid": os.getpid(),
                    "nodeId": self.node_id,
                    "socket": str(self.socket_path),
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _log_status_query(self, served: StatusQueryServed) -> None:
        """Record every ``msg/status`` query this node answered, and how."""

        self._log(
            "info",
            "daemon",
            "message.status.served",
            selector=served.selector,
            querySender=served.sender,
            messageId=served.message_id,
            records=served.records,
            answered=served.answered,
        )

    def _delivery_status_result(self, params: JsonObject) -> JsonObject:
        """Read the persisted verdict on messages this sender sent.

        Local records first, then the mesh: custody is ownership transfer, so
        a message this daemon queued may now belong to a mailbox that will be
        the one to record its outcome.  The mesh leg is best effort -- a holder
        that is unreachable right now simply is not represented, which is why
        the record is persistent and re-pullable rather than pushed once.
        """

        assert self._inbox is not None
        sender = _actor(params)
        if "sessionRef" in params:
            sender = self._canonical_interactive_actor(sender)
            sender_keys = (sender,)
        else:
            # Records are keyed by the wire sender.  The send boundary
            # resolves bare names to registered identities (Allen A(b));
            # records from earlier eras carry the verbatim bare spelling or
            # the short-lived mint era's local four-segment spelling.  The
            # local read unions every spelling the name could have — losing
            # sight of durable rows we promised to keep is not an
            # acceptable form of breaking compatibility.  The mesh leg
            # stays resolved-only.
            resolved = self._resolve_consumer_identity(sender)
            keys = [resolved]
            if resolved != sender:
                keys.append(sender)
            elif ":" not in sender:
                keys.append(canonical_agent_uri(self.owner, self.node_id, sender))
            sender_keys = tuple(keys)
            sender = resolved
        raw_message_id = params.get("messageId")
        message_id = (
            None
            if raw_message_id is None
            else _required_string(raw_message_id, "messageId")
        )
        local_by_id: dict[str, DeliveryStatus] = {}
        for key in sender_keys:
            for record in self._inbox.delivery_status_records(
                key, message_id=message_id
            ):
                local_by_id.setdefault(record.message_id, record)
        # A recipient asking about a message it RECEIVED: the sender-keyed
        # lookup above can never match (records are keyed by the original
        # sender), and the mesh query would fan out to every holder and come
        # back empty.  Its own node recorded the receipt, so that local row
        # is the answer and the mesh is not asked.  This is what a carrier's
        # ack-unavailable settlement needs; without it the carrier polled the
        # mesh every 5 s without end (codex-router, 2026-09-25: 56k queries).
        recipient_read = getattr(self._inbox, "delivery_status_for_recipient", None)
        if not local_by_id and message_id is not None and callable(recipient_read):
            for key in sender_keys:
                received = recipient_read(key, message_id)
                if received is not None:
                    local_by_id[received.message_id] = received
                    break
        local = tuple(local_by_id.values())
        received_locally = bool(local) and all(
            record.recipient in sender_keys for record in local
        )
        mesh = StatusQueryReport()
        mesh_error: str | None = None
        if self._transport is not None and not received_locally:
            try:
                mesh = query_delivery_status(
                    self._transport,
                    sender,
                    message_id=message_id,
                    timeout=_status_query_timeout(params),
                )
            except (NameError, ImportError):
                raise
            except Exception as error:  # noqa: BLE001 - best-effort mesh read
                mesh_error = str(error)
                self._log(
                    "warn",
                    "daemon",
                    "message.status.mesh_unreachable",
                    sender=sender,
                    detail=mesh_error,
                )
        records: list[DeliveryStatus] = [*local, *mesh.records]
        conflicts = conflicting_message_ids(records)
        if conflicts:
            # The only case where the merge has to pick a winner, and so the
            # only case where a short set can be confidently wrong.
            self._log(
                "warn",
                "daemon",
                "message.status.conflicting_claims",
                sender=sender,
                messageIds=list(conflicts),
                holders=sorted({record.holder for record in records}),
            )
        merged = merge_delivery_status(records)
        projected = [
            _delivery_status_projection(
                record,
                local_node_id=self.node_id,
                responded_holders=frozenset(mesh.responded_holders),
                undecodable_holders=frozenset(mesh.undecodable_holders),
            )
            for record in merged
        ]
        affected = [
            item for item in projected if item["state"] == "unconfirmed"
        ]
        local_holder = _canonical_holder_name(self.node_id)
        responded = {
            _canonical_holder_name(holder) for holder in mesh.responded_holders
        }
        missing_holders = sorted(
            {
                parsed[1]
                for record in merged
                if record.state is TerminalState.EXPIRED
                and (parsed := parse_agent_uri(record.recipient)) is not None
                and _canonical_holder_name(parsed[1]) != local_holder
                and _canonical_holder_name(parsed[1]) not in responded
            }
        )
        for dropped in mesh.undecodable:
            # A malformed record set is still a positive reply from its named
            # holder when the additive envelope metadata survived decoding.
            self._log(
                "warn",
                "daemon",
                "message.status.undecodable_reply",
                sender=sender,
                messageId=message_id,
                key=dropped.key,
                holder=dropped.holder,
                detail=dropped.detail,
            )
        for failed in mesh.errors:
            fields: JsonObject = {
                "sender": sender,
                "messageId": message_id,
                "key": mesh.key,
                "missingRecipientHolders": missing_holders,
                "affectedMessageCount": len(affected),
                "detail": failed,
            }
            if len(missing_holders) == 1:
                fields["holder"] = missing_holders[0]
            self._log(
                "warn",
                "daemon",
                "message.status.reply_error",
                **fields,
            )
        if affected:
            missing_fields: JsonObject = {
                "sender": sender,
                "key": mesh.key,
                "holders": missing_holders,
                "affectedMessageCount": len(affected),
            }
            if message_id is not None:
                missing_fields["messageId"] = message_id
            self._log(
                "warn",
                "daemon",
                "message.status.holders_missing",
                **missing_fields,
            )
        if duplicates := mesh.duplicate_holders:
            # One name answered several times: the wire-side signature of two
            # daemons sharing one node identity.  Name-keyed diagnostics
            # (meshHolders, responded_holders) collapse these answers, so the
            # pull counts them while it still has the replies apart.  The
            # liveliness watch raises daemon.identity.duplicate_instance from
            # its own vantage; this is the same condition seen through a
            # status query, which only nodes holding records for this sender
            # can observe.
            duplicate_fields: JsonObject = {
                "sender": sender,
                "key": mesh.key,
                "holders": [
                    {
                        "holder": entry.holder,
                        "replies": entry.replies,
                        "records": entry.records,
                    }
                    for entry in duplicates
                ],
            }
            if message_id is not None:
                duplicate_fields["messageId"] = message_id
            self._log(
                "warn",
                "daemon",
                "message.status.duplicate_holder",
                **duplicate_fields,
            )
        result: JsonObject = {
            "ok": True,
            "from": sender,
            "records": projected,
            # Deliberately not a "complete" flag: how many holders exist is not
            # knowable, so no pull can claim completeness.  These are the raw
            # facts about how the set was assembled, which is what lets a
            # caller decide whether to trust one pull or pull again.
            "diagnostics": {
                "localRecords": len(local),
                "meshReplies": mesh.replies,
                "meshRecords": len(mesh.records),
                # meshHolders keeps its published meaning: holders whose
                # records are in the merged set (contract/e2e-scenarios
                # 01-two-machine-roundtrip).  Nodes that answered with no
                # record are a separate fact, reported as meshResponders.
                "meshHolders": list(mesh.holders),
                "meshResponders": list(mesh.responded_holders),
                "undecodableReplies": len(mesh.undecodable),
                "undecodableHolders": list(mesh.undecodable_holders),
                "replyErrors": len(mesh.errors),
                "meshError": mesh_error,
                "holders": sorted({record.holder for record in records}),
                "missingRecipientHolders": missing_holders,
                "conflictingMessageIds": list(conflicts),
                "duplicateHolderRecords": [
                    {
                        "holder": entry.holder,
                        "replies": entry.replies,
                        "records": entry.records,
                    }
                    for entry in mesh.duplicate_holders
                ],
                "noKnownLoss": mesh_error is None and mesh.no_known_loss,
            },
        }
        if message_id is None:
            return result
        result["messageId"] = message_id
        if projected:
            result["state"] = projected[0]["state"]
            return result
        # No terminal record yet.  These two are deliberately NOT terminal
        # states: "pending" means this node still holds it and will itself
        # write the verdict, "unknown" means no reachable holder has one.
        held_expiry = self._inbox.held_expiry_ms(message_id)
        result["state"] = "pending" if held_expiry is not None else "unknown"
        if held_expiry is not None:
            result["holdExpiresAtMs"] = held_expiry
        return result

    def _deliver_autoupdate_restart_notification(
        self, params: JsonObject
    ) -> JsonObject:
        """Deliver and receipt-confirm the restart notice over the user bus."""

        if self._user_delivery is None:
            raise DaemonRequestError(
                ipc_errors.AUTOUPDATE_NOTIFICATION_UNAVAILABLE,
                "user delivery transport is unavailable",
            )
        old_version = _required_string(params.get("oldVersion"), "oldVersion")
        new_version = _required_string(params.get("newVersion"), "newVersion")
        resolved_tag = _required_string(params.get("resolvedTag"), "resolvedTag")
        resolved_commit = _required_string(
            params.get("resolvedCommit"), "resolvedCommit"
        )
        expected_seconds = params.get("expectedInterruptionSeconds", 90)
        if not isinstance(expected_seconds, int) or expected_seconds <= 0:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "expectedInterruptionSeconds must be positive"
            )
        # 3c116ad2 (P3): the field stays a validated positive int (the CLI
        # now derives it from this machine's desired state; 90 remains the
        # fallback for an older CLI), but the text no longer speaks a
        # duration -- measured fleet restores ran 2-10 minutes against the
        # old fixed-second promise, and the follow-up notice, not a number,
        # is what closes the interruption now.
        text = (
            f"Harness Bridge 已完成升级：{old_version} → {new_version} "
            f"({resolved_tag}@{resolved_commit[:12]})。即将重启 daemon，"
            "重启中，恢复完成会再通知。若长时间仍未恢复，请运行 "
            "hyprial service --json 检查状态，并查看 "
            "$HYPRIAL_HOME/state/logs/daemon.jsonl 中本次重启之后最近一条 "
            "daemon.start.failed 的 phase/errorType/error；这些操作不会改配置。"
            "升级已安装，不要重复执行升级。"
        )
        sender = canonical_agent_uri(self.owner, self.node_id, "squire")
        message_id = str(
            uuid5(
                NAMESPACE_URL,
                "hyprial:autoupdate-restart:"
                f"{self.owner}:{old_version}:{new_version}:"
                f"{resolved_commit}:{os.getpid()}",
            )
        )
        outcome = self._user_delivery.deliver(
            UserDeliveryRequest(
                message_id=message_id,
                idempotency_key=(
                    "autoupdate-restart:"
                    f"{old_version}:{new_version}:{resolved_tag}:"
                    f"{resolved_commit}:{os.getpid()}"
                ),
                owner=self.owner,
                sender=sender,
                message=text,
                conversation_id="autoupdate-restart",
            )
        )
        if not outcome.accepted:
            raise DaemonRequestError(
                outcome.code or "AUTOUPDATE_NOTIFICATION_UNDELIVERED",
                outcome.message or "restart notification was not delivered",
                {"messageId": outcome.message_id},
            )
        self._log(
            "info",
            "autoupdate",
            "autoupdate.restart_notification.delivered",
            messageId=outcome.message_id,
            oldVersion=old_version,
            newVersion=new_version,
            resolvedCommit=resolved_commit,
        )
        return {
            "ok": True,
            "delivered": True,
            "deliveryConfirmed": True,
            "messageId": outcome.message_id,
            **(
                {"nativeMessageId": outcome.native_message_id}
                if outcome.native_message_id is not None
                else {}
            ),
        }

    def _close(self) -> None:
        """Shut down, saying which resource is being closed and what failed.

        The previous version collected exceptions into a list and surfaced
        them once as ``ExceptionGroup("daemon shutdown failed", errors)`` --
        so an operator learned that two things failed and never which two.
        Worse, that shape cannot report the failure that actually matters
        here: a close that HANGS never raises, so it contributes nothing to
        the list and leaves no trace at all. The log went quiet and the
        process stayed alive, which reads as "still starting" when it is in
        fact "never finished stopping" -- and those two need opposite
        remedies.

        So each step announces itself before running and confirms after.
        A hang is then visible as a `begin` with no matching `end`, which is
        the only way a blocking close can be seen from outside.
        """

        errors: list[BaseException] = []

        # Armed before the first step, because the backstop covers *this*
        # function hanging, and anything armed later would sit behind the very
        # hang it exists to explain.  It complements the per-resource trace
        # below rather than duplicating it: a `begin` with no `end` names the
        # door that did not open, the dump names who is standing behind it.
        #
        self._arm_shutdown_stall_dump_if_owned()

        def attempt(operation: Callable[[], Any], resource: str) -> None:
            self._log_trace("info", "daemon.close.begin", resource=resource)
            started = time.monotonic()
            try:
                operation()
            except BaseException as error:
                errors.append(error)
                self._log_trace(
                    "warn",
                    "daemon.close.failed",
                    resource=resource,
                    errorType=type(error).__name__,
                    error=str(error)[:500],
                    elapsedMs=int((time.monotonic() - started) * 1000),
                )
            else:
                self._log_trace(
                    "info",
                    "daemon.close.end",
                    resource=resource,
                    elapsedMs=int((time.monotonic() - started) * 1000),
                )

        attempt(self._autoupdate.stop, "autoupdate")
        # Before any collaborator the restore thread might be inside is torn
        # down.  Bounded on purpose -- see the join method's docstring.
        attempt(self._join_restore_thread, "restore-thread")

        if self._server is not None:
            server = self._server
            self._server = None
            attempt(server.close, "ipc-server")
        if self._accept_reserve_fd is not None:
            reserve_fd = self._accept_reserve_fd
            self._accept_reserve_fd = None
            attempt(lambda: os.close(reserve_fd), "reserve-fd")
        attempt(self._close_ipc_clients, "ipc-clients")
        attempt(lambda: self.socket_path.unlink(missing_ok=True), "socket-file")
        attempt(lambda: (self.state_dir / "daemon.json").unlink(missing_ok=True), "daemon-json")
        self._maintenance_generation += 1
        if not self._maintenance_scheduler.shutdown(5.0):
            raise RuntimeError(
                "maintenance callback did not stop; refusing unsafe domain teardown"
            )
        if self._routine_service is not None:
            routine_service = self._routine_service
            self._routine_service = None
            attempt(routine_service.close, "routine-service")
        if self._remote_workflow is not None:
            remote_workflow = self._remote_workflow
            self._remote_workflow = None
            attempt(remote_workflow.close_registrations, "remote-workflow-registrations")
            def close_remote_workflow():
                if not remote_workflow.shutdown(5.0):
                    raise RuntimeError("remote workflow handlers did not drain before deadline")
            attempt(close_remote_workflow, "remote-workflow")
        if self._workflow_service is not None:
            workflow_service = self._workflow_service
            self._workflow_service = None
            attempt(workflow_service.close, "workflow-service")
        for handle in self._degraded_workflow_handles:
            attempt(handle.close, "degraded-workflow-component")
        self._degraded_workflow_handles.clear()
        if self._pac_actor_service is not None:
            pac_actor_service = self._pac_actor_service
            self._pac_actor_service = None
            if not pac_actor_service.close(5.0):
                errors.append(RuntimeError("PAC actor service did not drain before deadline"))
        if self._lifecycle_manager is not None:
            lifecycle_manager = self._lifecycle_manager
            self._lifecycle_manager = None
            if not lifecycle_manager.drain(5.0):
                errors.append(
                    RuntimeError("lifecycle manager did not drain before deadline")
                )
        for lifecycle_port in self._lifecycle_domain_ports:
            if not lifecycle_port.drain(5.0):
                errors.append(
                    RuntimeError(
                        f"{lifecycle_port.domain} lifecycle port did not drain"
                    )
                )
        self._lifecycle_domain_ports = ()
        if self._route_registration is not None:
            route_registration = self._route_registration
            self._route_registration = None
            self._routes = None
            attempt(route_registration.close_registrations, "route-registrations")
            if not route_registration.drain(5.0):
                errors.append(
                    RuntimeError("route registration did not drain before deadline")
                )
        if self._lifecycle_router is not None:
            lifecycle_router = self._lifecycle_router
            self._lifecycle_router = None
            lifecycle_router.close()
        if self._agent_session_domains is not None:
            agent_session_domains = self._agent_session_domains
            if not agent_session_domains.close():
                errors.append(
                    RuntimeError("session/agent domains did not drain before deadline")
                )
            self._agent_domains_finalizer.detach()
        with self._registry_management_lock:
            self._registry_management = None
        if self._harnesses is not None:
            harnesses = self._harnesses
            self._harnesses = None
            stop_harnesses = getattr(harnesses, "stop", None)
            if callable(stop_harnesses):
                attempt(stop_harnesses, "harnesses")
            if getattr(harnesses, "drain_complete", True) is False:
                errors.append(
                    RuntimeError("harness domain did not drain before deadline")
                )
        if self._adapters is not None:
            adapters = self._adapters
            self._adapters = None
            attempt(adapters.stop, "adapters")
        self._lark_client = None
        if self._lark_events is not None:
            self._lark_events.close()
            self._lark_events = None
        if self._inbox is not None:
            inbox = self._inbox
            self._inbox = None
            shutdown_inbox = getattr(inbox, "shutdown", None)
            if callable(shutdown_inbox):
                attempt(shutdown_inbox, "inbox")
            else:
                # Isolated legacy test fixtures can still inject the old
                # state engine directly. Production composition never does.
                inbox_type = type(inbox)
                if (
                    inbox_type.__module__ == "hyprial.inbox.service"
                    and inbox_type.__name__ == "InboxService"
                ):
                    attempt(lambda: inbox_type.close(inbox), "inbox-store")
        self._inbox_coordinator = None
        if self._usage_cache is not None:
            usage_cache = self._usage_cache
            self._usage_cache = None
            attempt(usage_cache.stop, "usage-cache")
        for resource_name in (
            "_actor_token",
            "_duplicate_watch",
            "_org_endpoint",
            "_user_endpoint",
            "_status_endpoint",
            "_endpoint",
            "_directory",
        ):
            resource = getattr(self, resource_name)
            if resource is not None:
                setattr(self, resource_name, None)
                attempt(resource.close, resource_name.lstrip("_"))
        if self._runtime is not None:
            runtime = self._runtime
            self._runtime = None
            attempt(runtime.stop, "actor-runtime")
        self._presence = None
        self._user_delivery = None
        if self._transport is not None:
            transport = self._transport
            self._transport = None
            attempt(transport.close, "zenoh-transport")
        if self._forwarding_supervisor is not None:
            supervisor = self._forwarding_supervisor
            self._forwarding_supervisor = None
            attempt(supervisor.close, "forwarding-sidecar")
        if self._forwarding_discovery is not None:
            forwarding_discovery = self._forwarding_discovery
            self._forwarding_discovery = None
            attempt(forwarding_discovery.close, "forwarding-sidecar")
        if self._lock_stream is not None:
            lock_stream = self._lock_stream
            self._lock_stream = None
            attempt(lock_stream.close, "home-lock")
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("daemon shutdown failed", errors)

    def _install_signal_handlers(self) -> dict[int, Any]:
        previous: dict[int, Any] = {}

        def stop(_signum: int, _frame: FrameType | None) -> None:
            self.stop_event.set()

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, stop)
        return previous

    @staticmethod
    def _restore_signal_handlers(previous: dict[int, Any]) -> None:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    def _owner_alert_notifier(self, text: str, *, idempotency_key: str) -> None:
        """The owner-DM channel for alerts (autoupdate.alert.notify_owner)."""

        from hyprial.autoupdate.alert import notify_owner

        notify_owner(
            hyprial_home=self.hyprial_home,
            state_dir=self.state_dir,
            text=text,
            idempotency_key=idempotency_key,
        )

    def _log(self, level: str, component: str, event: str, **fields: Any) -> None:
        self._logger.bind(component=component).log(level, event, **fields)

    def _build_provider_auth_coordinator(self) -> Any:
        """Wire provider-auth relogin/alert coordination (spec 2026-09-14).

        Returns None when the feature cannot be wired: a daemon that starts
        without it is degraded (no auth-failure alerts), while a daemon that
        cannot start because its *alerting* feature failed is the worse
        object -- the same call autoupdate.alert makes.  The degradation is
        logged, not silent.
        """

        try:
            import shutil
            import socket

            from hyprial.autoupdate.alert import notify_owner
            from hyprial.provider_auth import (
                DeviceLoginRunner,
                ProviderAuthCoordinator,
            )
            from hyprial.squire.profile import UserProfileStore

            store = UserProfileStore(self.state_dir / "users.json")
            profile = (
                store.get_by_owner(self.owner) if store.path.exists() else None
            )
            agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
            auth_path = (
                Path(agent_dir).expanduser() / "auth.json"
                if agent_dir
                else Path.home() / ".pi" / "agent" / "auth.json"
            )
            pi_binary = shutil.which("pi")

            def runtime_context_valid(context: Any) -> bool:
                try:
                    current = self._agent_registry.require(context.actor)
                except Exception:  # noqa: BLE001 -- stale context is rejection
                    return False
                return (
                    current.uri == context.actor
                    and current.entity_token == context.entity_token
                )

            def runtime_helper(context: Any) -> Any:
                from hyprial.agents.environment import (
                    apply_runtime_environment_profile,
                )

                environment = apply_runtime_environment_profile(
                    os.environ, context.environment()
                )
                return DeviceLoginRunner(
                    pi_command=(pi_binary,) if pi_binary else ("pi",),
                    environment=environment,
                )

            return ProviderAuthCoordinator(
                profile_store=store,
                owner_key=profile.owner_key if profile is not None else None,
                notifier=lambda text, *, idempotency_key: notify_owner(
                    hyprial_home=self.hyprial_home,
                    state_dir=self.state_dir,
                    text=text,
                    idempotency_key=idempotency_key,
                ),
                helper_runner=DeviceLoginRunner(
                    pi_command=(pi_binary,) if pi_binary else ("pi",)
                ),
                auth_path=auth_path,
                host=socket.gethostname(),
                stop=self.stop_event,
                logger=lambda event, **fields: self._log(
                    "info", "daemon", event, **fields
                ),
                runtime_helper_factory=runtime_helper,
                runtime_context_validator=runtime_context_valid,
            )
        except Exception as error:  # noqa: BLE001 -- see docstring
            self._log(
                "warn",
                "daemon",
                "provider.auth.init.failed",
                errorType=type(error).__name__,
            )
            return None

    def _log_trace(self, level: str, event: str, **fields: Any) -> None:
        """Log a step marker, accepting that the write itself may fail.

        These events exist only to say which step is running.  Every one costs
        an ``os.open``, so under fd exhaustion the instrumentation becomes the
        thing that fails -- and a daemon that cannot start *because* it is
        describing its own startup is a worse failure than the silence this
        was added to fix.  ``test_real_low_fd_pressure_recovers_without_
        restarting_daemon`` holds that line: the daemon has to survive fd
        pressure, and it may not be a step marker that stops it.

        Only for markers.  Anything a caller must act on keeps using
        ``_log``, where a lost write is a real defect rather than a
        degradation.
        """

        try:
            self._log(level, "daemon", event, **fields)
        except OSError:
            pass


def _mirror_startup_event_to_stderr(event: str, **fields: Any) -> None:
    """Mirror one startup failure event to stderr as a single-line JSON envelope.

    The daemon's structured events go to ``logs/daemon.jsonl`` -- a file the
    CLI's failure-report path does not read. What ``hyprial init`` summarizes on a
    failed start is the launch capture: the daemon child's stdout+stderr,
    owned by this launch behind a marker. An event that never reaches stderr
    is an event the failure report cannot name, which is how a daemon that
    died binding its IPC socket reported ``daemonEvents: []``.

    Two rules keep this channel safe:

    * Fields carry only values the daemon authors itself (event names, phase
      names, counts) -- never exception text or anything derived from
      external input. The structured log redacts; stderr does not, and the
      summary reader must not become a channel for bytes a failure dragged
      in. (The summary keeps only event names regardless; this rule is what
      keeps the raw launch log clean for the human who opens it next.)
    * Called from exactly two startup branches: a ``step()`` failure in
      ``run()`` and a degraded restore in ``_restore_harnesses``. A daemon
      that talks on stderr on its normal path turns the launch log into a
      noise source; ``tests/test_startup_event_reconciliation.py`` pins the
      call sites.

    Never raises: this runs on failure paths, and a broken mirror must not
    mask the failure it describes.
    """

    try:
        print(
            json.dumps({"event": event, **fields}, separators=(",", ":")),
            file=sys.stderr,
            flush=True,
        )
    except Exception:  # noqa: BLE001 - the mirror must never mask its failure
        pass


def _optional_string_param(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, f"{label} must be a string when present"
        )
    return value or None


def _session_harness(session: InteractiveSession) -> str:
    """Which harness an interactive session is running on.

    An InteractiveSession never stored a harness (it did not need to when
    nothing compared connectors across harnesses); its ``runtime`` is spelled
    ``<harness>_interactive`` and its ``source`` ``<harness>-channel``, so the
    harness is recoverable without changing that record's schema.
    """

    runtime = (session.runtime or "").strip()
    if runtime:
        candidate = runtime.split("_", 1)[0]
        if candidate:
            return candidate
    source = (session.source or "").strip()
    if source:
        candidate = source.split("-", 1)[0]
        if candidate:
            return candidate
    return "claude"


#: The one switch that keeps a test daemon on this machine.  Parsed like the
#: other zenoh flags (unset or empty = off; 1/true/yes/on = on).  Isolation
#: by HOME + HYPRIAL_PEER_DISCOVERY=0 alone left paths open (2026-09-16 ban;
#: inventory 2026-09-23): forwarding runs before the discovery switch and
#: joins the tailnet through a sidecar, zenoh may add a default all-interface
#: listener when ``listen`` is empty, and the usage fetcher calls vendors.
NETWORK_ISOLATED_ENV = "HYPRIAL_NETWORK_ISOLATED"
#: Listen here when isolated and nothing loopback was configured: explicit,
#: so zenoh never falls back to its own default listener.
ISOLATED_DEFAULT_LISTEN = "tcp/127.0.0.1:0"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _custom_home_or_state() -> bool:
    """True when HYPRIAL_HOME or HARNESS_STATE_DIR points off the default.

    The default home is ``~/.hyprial`` and its state dir ``~/.hyprial/state``;
    an explicit value equal to those is the node's own home, not a custom one.
    """

    from hyprial.home import default_hyprial_home

    default_home = default_hyprial_home()[0]
    home = os.environ.get("HYPRIAL_HOME")
    state = os.environ.get("HARNESS_STATE_DIR")
    if home and Path(home).expanduser().resolve() != default_home:
        return True
    return bool(state) and Path(state).expanduser().resolve() != default_home / "state"


def network_isolated_from_environment() -> bool:
    """Fail closed once set: only UNSET means "not isolated".

    The shared zenoh flag parser treats an empty value as unset (default),
    which is exactly how ``export HYPRIAL_NETWORK_ISOLATED=`` or an unfilled
    template would leak a test daemon onto the network.  So an empty or
    whitespace value is refused here, and an unknown value is refused by the
    shared parser.  Unset stays "not isolated" because production does not
    set it; the test entry points refuse to start without it instead.
    """

    raw = os.environ.get(NETWORK_ISOLATED_ENV)
    if raw is None:
        return False
    if not raw.strip():
        raise ValueError(
            f"{NETWORK_ISOLATED_ENV} is set but empty; use 1 to isolate "
            "this daemon or unset it"
        )
    return zenoh_environment_flag(NETWORK_ISOLATED_ENV)


def _endpoint_host(endpoint: str) -> str | None:
    """``tcp/127.0.0.1:7447`` -> ``127.0.0.1``; ``tcp/[::1]:0`` -> ``::1``."""

    locator = endpoint.split("#", 1)[0].split("?", 1)[0]
    _, slash, address = locator.partition("/")
    if not slash:
        return None
    # A zenoh locator's address is host:port (IPv6 bracketed): the standard
    # authority parser handles both, brackets included.
    try:
        return urlsplit(f"//{address}").hostname
    except ValueError:
        return None


def compose_daemon_worker_launch(
    *,
    registry: Any,
    hyprial_home: Path,
    spec: HarnessLaunchSpec,
    channel: Any,
    environ: Mapping[str, str],
) -> Any:
    """The daemon's child-environment factory body, one call per worker launch.

    Every managed carrier -- pi, codex app-server, the Claude agent SDK,
    jev and the PTY connectors -- receives its environment from here, so
    this is the one place the ``workerProxy`` route is decided.  It is read
    per launch, never at startup: ``hyprial config set workerProxy.*``
    applies to the next worker without a daemon restart, and a damaged
    setting fails THIS start loudly.  Module level so the wiring itself is
    testable without building a daemon.
    """

    from hyprial.agents.environment import compose_worker_child_launch
    from hyprial.agents.worker_proxy import launch_worker_proxy_route

    return compose_worker_child_launch(
        registry=registry,
        hyprial_home=hyprial_home,
        channel=channel,
        environ=environ,
        agent_name=spec.name,
        worker_proxy=launch_worker_proxy_route(hyprial_home, spec),
    )


def _resolve_forwarding(
    hyprial_home: Path,
    node_id: str,
    *,
    isolated: bool,
    zenoh_listen: tuple[str, ...],
) -> tuple[ForwardingPolicy, dict[str, str], AutomaticForwarding | None, str | None]:
    """The daemon's forwarding decision: policy, launch variables, plan, reason.

    Precedence: isolation vetoes; ``off`` suppresses every source (a durable
    rollback, even over explicit or generated variables); explicit or
    generated variables win otherwise (step 4a); ``auto``/``on`` then use a
    sidecar-joined home's automatic plan. ``on`` refuses to start when that
    plan is unavailable; ``auto`` records the reason and stays off.
    """

    try:
        policy = forwarding_policy(os.environ, hyprial_home)
    except ForwardingConfigurationError as error:
        raise ValueError(f"{error.code}: {error}") from error
    if isolated:
        if policy.mode == "on":
            raise ValueError(
                f"{NETWORK_ISOLATED_ENV} is set but {FORWARDING_MODE_ENV}=on; "
                "an isolated daemon never forwards"
            )
        return policy, {}, None, None
    if policy.mode == "off":
        return policy, {}, None, None
    environment = _resolve_forwarding_environment(node_id)
    if environment:
        if not zenoh_listen:
            raise ValueError("forwarding requires explicit HYPRIAL_ZENOH_LISTEN")
        return policy, environment, None, None
    if policy.mode not in ("auto", "on"):
        return policy, {}, None, None
    automatic, reason = automatic_forwarding(
        hyprial_home, os.environ, node_id=node_id
    )
    if automatic is not None and zenoh_listen:
        # An explicit listen list stays a complete override: reuse a bound
        # loopback entry in it, never append a hidden one.
        target = next(
            (
                endpoint.removeprefix("tcp/")
                for endpoint in zenoh_listen
                if _endpoint_host(endpoint) == "127.0.0.1"
                and not endpoint.endswith(":0")
            ),
            None,
        )
        if target is None:
            automatic, reason = None, "LISTEN_CONFLICT"
        else:
            return policy, automatic.environment(target), None, None
    if automatic is None and policy.mode == "on":
        raise ValueError(
            f"FORWARDING_UNAVAILABLE: {FORWARDING_MODE_ENV}=on but {reason}"
        )
    return policy, {}, automatic, reason


def _reserve_loopback_endpoint() -> str:
    """A free loopback port for this daemon's forwarding inbound listener.

    Zenoh 1.9 cannot report the port a ``:0`` listener bound, so the OS
    picks one here and the session binds it right after; a collision in
    between raises at session open and ``_open_transport`` picks again.
    """

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return f"tcp/127.0.0.1:{probe.getsockname()[1]}"


def _open_transport(
    config: ZenohConfig, forwarding_listen: tuple[str, ...]
) -> tuple[ZenohTransport, tuple[str, ...]]:
    """Open the session; re-pick ONLY the forwarding loopback port on a bind
    collision on that exact endpoint. Any other failure -- including the
    node's own tailnet listener being held -- surfaces unchanged. The number
    of re-picks is the registered external-I/O restart budget."""

    rebinds = DEFAULT_POLICIES[EXTERNAL_IO].max_restarts
    while True:
        try:
            return ZenohTransport(config), forwarding_listen
        except Exception as error:
            if (
                not forwarding_listen
                or rebinds <= 0
                or forwarding_listen[0] not in str(error)
            ):
                raise
        rebinds -= 1
        replacement = (_reserve_loopback_endpoint(),)
        config = replace(
            config,
            listen=tuple(
                replacement[0] if endpoint == forwarding_listen[0] else endpoint
                for endpoint in config.listen
            ),
        )
        forwarding_listen = replacement


def _resolve_forwarding_environment(node_id: str) -> dict[str, str]:
    """The sidecar launch variables for this daemon, resolved in-process.

    Already-generated variables (the CLI launcher, a watchdog, a fixture)
    pass through unchanged. Otherwise the operator inputs -- the sidecar
    binary and inbound target -- are resolved here with the same function
    the launcher uses, so a direct ``daemon run`` or a watchdog restart
    keeps forwarding instead of silently starting without it. An invalid
    explicit configuration refuses startup, as the launcher does.
    """

    generated = {
        name: os.environ[name]
        for name in (FORWARDING_COMMAND_ENV, FORWARDING_UP_ENV)
        if os.environ.get(name)
    }
    if generated:
        return generated
    try:
        return daemon_forwarding_environment(
            configured_hyprial_home()[0], os.environ, node_id=node_id
        )
    except ForwardingConfigurationError as error:
        raise ValueError(f"{error.code}: {error}") from error


def _isolated_endpoints(
    listen: tuple[str, ...], connect: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate endpoints for an isolated daemon; refuse, never silently drop.

    Forwarding is refused by the PRESENCE of any HYPRIAL_FORWARDING_* variable,
    not by endpoint address: its sidecar exposes loopback endpoints that tunnel
    to the tailnet, so an address check would wave it through.
    """

    leaked = sorted(name for name in os.environ if name.startswith("HYPRIAL_FORWARDING_"))
    if leaked:
        raise ValueError(
            f"{NETWORK_ISOLATED_ENV} is set but forwarding is configured "
            f"({', '.join(leaked)}); unset them -- forwarding joins the tailnet"
        )
    remote = [
        endpoint
        for endpoint in (*listen, *connect)
        if _endpoint_host(endpoint) not in _LOOPBACK_HOSTS
    ]
    if remote:
        raise ValueError(
            f"{NETWORK_ISOLATED_ENV} is set but HYPRIAL_ZENOH_LISTEN/CONNECT "
            f"name non-loopback endpoints: {', '.join(remote)}"
        )
    return (listen or (ISOLATED_DEFAULT_LISTEN,)), connect


def _endpoint_list(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _reconcile_tick_budget() -> float:
    """Wall-clock budget for one periodic reconcile tick, in seconds.

    The tick runs inside the accept loop, so an over-budget tick is an IPC
    availability event: it is logged loudly (``daemon.reconcile_overrun``)
    with the slowest phase named.  Overrunning the budget NEVER terminates
    the serve loop -- this daemon is not launchd-supervised, so a dead loop
    is an outage with no restarter (postmortem 2026-08-23).
    ``HYPRIAL_RECONCILE_TICK_BUDGET`` overrides the 1s default; 0 disables.
    """

    raw = os.environ.get("HYPRIAL_RECONCILE_TICK_BUDGET")
    if raw is None:
        return 1.0
    try:
        value = float(raw)
    except ValueError:
        return 1.0
    return value if value >= 0 else 1.0


def _lock_wait_timeout() -> float:
    """Seconds to wait for a previous daemon to release the state-dir lock.

    ⚠️ This used to be its own literal, "sized to clear a normal shutdown".
    A *normal* shutdown was the wrong thing to size against: on 2026-08-31 a
    replacement daemon gave up after 15s while the outgoing one was still
    inside a teardown budgeted at :data:`TEARDOWN_BUDGETED_SECONDS`. Waiting
    less than that is refusing to start for a reason the outgoing daemon was
    still working through.

    ⚠️ Waiting *at least* that long does not make the start safe. The budget
    is not a bound -- see the note beside the constant -- so this can still
    expire while the outgoing daemon sits in an unbudgeted step. It removes
    the case where we were wrong **by construction**; it does not remove the
    case where we are unlucky.

    So it derives from the budget rather than restating a number.
    Overridable via ``HYPRIAL_DAEMON_LOCK_WAIT_TIMEOUT`` (0 disables the wait and
    restores fail-fast behaviour).
    """

    raw = os.environ.get("HYPRIAL_DAEMON_LOCK_WAIT_TIMEOUT")
    if raw is None:
        return TEARDOWN_BUDGETED_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return TEARDOWN_BUDGETED_SECONDS
    return value if value >= 0 else TEARDOWN_BUDGETED_SECONDS


def _zenoh_endpoints(
    *,
    env_listen: tuple[str, ...],
    env_connect: tuple[str, ...],
    stored_listen: tuple[str, ...],
    stored_connect: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Merge per-launch overrides with the persistent desired-state config.

    Environment variables win per side; an empty environment side falls back
    to the stored endpoints so `hyprial init --listen/--connect` keeps working
    across restarts without any exported variable.
    """

    return (
        env_listen if env_listen else stored_listen,
        env_connect if env_connect else stored_connect,
    )


def _require_deliverable_send_target(target: str) -> None:
    """Send-time admission gate: only deliverable address forms pass.

    Post-alias-resolution a bare name IS a node address — the bare
    namespace is the node namespace.  Delivering to one lands in the node
    inbox, receipt-signed, with no consumer: the delivered-and-receipted
    silent hole.  Allen: reject outright, no backwards-compat shim; the
    error states facts only, no remediation instructions.
    """

    target_kind = classify_target_identity(target)
    if target_kind == TARGET_KIND_HOST:
        raise DaemonRequestError(
            ipc_errors.TARGET_IS_NODE,
            f"target {target!r} is a node address, not an agent: nodes are "
            "announced on the network but do not receive actor messages",
            {"target": target},
        )
    if target_kind == TARGET_KIND_UNKNOWN:
        raise DaemonRequestError(
            ipc_errors.UNSUPPORTED_TARGET,
            f"target {target!r} matches no deliverable address form; "
            "deliverable forms are agent:<owner>:<machine>:<agent>, "
            "user:<owner>, and route:<adapter>:<route>",
            {"target": target},
        )


def _undeliverable_outbox_recipient(recipient: str) -> bool:
    """Mirror the CURRENT send-time admission gate for queued outbox rows.

    C1 (2026-08-22 dead-letter audit): the previous predicate deliberately
    kept bare node addresses deliverable "for the pending migration card" —
    that migration is DONE, and the send gate now rejects node-addressed
    sends outright (TARGET_IS_NODE: delivered-and-receipted, never consumed).
    Rows that can never pass today's gate are dead by construction:
    ``host`` and ``unknown`` are undeliverable; ``agent``/``user``/
    ``channel_route`` keep their queue semantics.  Existence questions
    (offline vs nonexistent) are NOT answered here — see the daemon's
    unresolvable predicate, which must never prune a merely-offline target.
    """

    if lark_reply_adapter(recipient) is not None:
        return False
    kind = classify_target_identity(recipient)
    return kind not in (
        TARGET_KIND_AGENT,
        TARGET_KIND_USER,
        TARGET_KIND_CHANNEL_ROUTE,
    )


def _query_entry(message: InboxMessage, *, kind: str) -> JsonObject:
    """One row of ``message.query``: the fields a person reads, text included."""

    try:
        body = json.loads(message.payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}
    text = body.get("message", "") if isinstance(body, dict) else ""
    return {
        "kind": kind,
        "messageId": message.message_id,
        "conversationId": message.conversation_id,
        "from": message.sender,
        "to": message.recipient,
        "intent": message.intent,
        "createdAtMs": message.created_at_ms,
        "message": text if isinstance(text, str) else str(text),
    }


def _outbox_entry_json(item: OutboxItem) -> JsonObject:
    return {
        "messageId": item.message.message_id,
        "conversationId": item.message.conversation_id,
        "sender": item.message.sender,
        "recipient": item.message.recipient,
        "intent": item.message.intent,
        "createdAtMs": item.message.created_at_ms,
        "attempts": item.attempts,
        "nextAttemptMs": item.next_attempt_ms,
        "expiresAtMs": item.expires_at_ms,
        "undeliverableScheme": _undeliverable_outbox_recipient(
            item.message.recipient
        ),
    }


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, f"{label} must be a non-empty string"
        )
    return value


def _message_origin(metadata: object) -> JsonObject | None:
    """Carry a message's reported origin through to its reader.

    ``None`` means the adapter said nothing, which a reader must keep distinct
    from any particular answer: "unknown" and "a direct message" are different
    facts, and only one of them is safe to act on.  The block stays
    adapter-scoped rather than flattened into a generic key, because a bare
    ``chatType`` would claim a meaning that the other adapters never agreed to.
    """

    if not isinstance(metadata, dict):
        return None
    origin: JsonObject = {}
    chat_type = metadata.get("chatType")
    if isinstance(chat_type, str) and chat_type:
        origin["chatType"] = chat_type
    sender = _reported_sender(metadata.get("sender"))
    if sender is not None:
        origin["sender"] = sender
    if not origin:
        return None
    reported_by = metadata.get("provider")
    if isinstance(reported_by, str) and reported_by:
        origin["provider"] = reported_by
    return origin


#: The sender block an adapter may report (see the Lark adapter's
#: ``_resolved_sender``).  Resolution happens in the adapter, which owns the
#: identities and user stores; the daemon only carries the answer, bounded,
#: so it travels with the payload to whichever node the recipient is on.
#: ``userKey`` .. ``realName`` come from the per-machine user store; they are
#: ``None`` when the identities fallback answered.
_SENDER_TEXT_FIELDS = (
    "kind",
    "platformId",
    "unionId",
    "displayName",
    "owner",
    "standing",
    "source",
    "userKey",
    "userKind",
    "nickname",
    "realName",
)
_SENDER_STANDINGS = frozenset({"verified", "observed", "ambiguous", "unresolved"})
_SENDER_FIELD_MAX_CHARS = 256


def _reported_sender(value: object) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    standing = value.get("standing")
    if standing not in _SENDER_STANDINGS:
        # An answer without a known confidence cannot be acted on safely;
        # dropping it keeps "no sender block" meaning "the adapter said
        # nothing we can trust", never a silently upgraded standing.
        return None
    sender: JsonObject = {}
    for field in _SENDER_TEXT_FIELDS:
        text = value.get(field)
        sender[field] = (
            text[:_SENDER_FIELD_MAX_CHARS] if isinstance(text, str) and text else None
        )
    if standing != "verified":
        # Only a person's confirmation names an owner.
        sender["owner"] = None
    for candidates_field in ("candidateOwners", "candidateUsers"):
        candidates = value.get(candidates_field)
        if standing == "ambiguous" and isinstance(candidates, list):
            sender[candidates_field] = [
                candidate[:_SENDER_FIELD_MAX_CHARS]
                for candidate in candidates[:8]
                if isinstance(candidate, str) and candidate
            ]
    if value.get("lookupFailed") is True:
        sender["lookupFailed"] = True
    return sender


def _optional_channel_build_version(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not safe_channel_build_version(value):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, f"{label} must be a safe package-version token"
        )
    return value


def _optional_positive_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, f"{label} must be a positive integer"
        )
    return value


def _optional_boolean(value: object, label: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, f"{label} must be a boolean")
    return value


def _channel_lease_token(value: object) -> str:
    if not isinstance(value, str) or not (32 <= len(value) <= 256):
        raise DaemonRequestError(
            ipc_errors.INVALID_CHANNEL_LEASE,
            "channelLeaseToken must be an opaque 32-256 character token",
        )
    return value


def _channel_lease_digest_for_registration(
    *, source: str, protocol_version: int | None, token: object
) -> str | None:
    if source != "claude-channel":
        if token is not None:
            raise DaemonRequestError(
                ipc_errors.INVALID_SESSION_SOURCE,
                "channelLeaseToken is only valid for claude-channel sessions",
            )
        return None
    if protocol_version != CHANNEL_PROTOCOL_VERSION:
        # Protocol-v1 and older registrations remain readable and routable, but
        # cannot claim lease-backed operational liveness.
        return None
    lease = _channel_lease_token(token)
    return hashlib.sha256(lease.encode()).hexdigest()


def _verify_channel_lease(session: InteractiveSession, token: object) -> None:
    expected = session.channel_lease_digest
    if expected is None:
        raise DaemonRequestError(
            ipc_errors.CHANNEL_LIVENESS_UNAVAILABLE,
            "channel registration has no operational liveness lease",
        )
    supplied = hashlib.sha256(_channel_lease_token(token).encode()).hexdigest()
    if not hmac.compare_digest(supplied, expected):
        raise DaemonRequestError(
            ipc_errors.INVALID_CHANNEL_LEASE,
            "channel liveness lease did not match the registered session",
        )


def _interactive_session_status(
    session: Any,
    *,
    current_epoch: str | None,
    online: bool,
    last_observed: tuple[str, str, float] | None = None,
    now: float | None = None,
) -> JsonObject:
    """Separate durable registration, generation, observation, and liveness."""

    serialize = getattr(session, "to_json", None)
    status = serialize() if callable(serialize) else session.to_payload()
    status["online"] = online
    # The one-way lease verifier is daemon recovery state, not operator
    # telemetry. The digest is a same-OS-user misuse check, not a cryptographic
    # authentication boundary or an anti-replay promise.
    status.pop("channelLeaseDigest", None)
    if session.source == "claude-channel":
        observed_age: float | None = None
        if (
            last_observed is not None
            and current_epoch is not None
            and last_observed[:2] == (session.session_ref, current_epoch)
        ):
            observed_age = max(
                0.0,
                (time.monotonic() if now is None else now) - last_observed[2],
            )
        recently_observed = (
            observed_age is not None
            and observed_age <= CHANNEL_LIVENESS_TTL_SECONDS
        )
        lease_capable = bool(
            getattr(session, "channel_lease_backed", False)
            or getattr(session, "channel_lease_digest", None) is not None
        )
        status["channelRegistered"] = True
        status["channelCurrentThisGeneration"] = current_epoch is not None
        status["channelCurrentEpoch"] = current_epoch
        status["channelRecentlyObserved"] = recently_observed
        status["channelAlive"] = bool(
            lease_capable and current_epoch is not None and recently_observed
        )
        if status["channelAlive"]:
            status["channelLiveness"] = "alive"
        elif lease_capable:
            status["channelLiveness"] = "stale"
        else:
            status["channelLiveness"] = "unverified"
        if observed_age is not None:
            status["channelLastObservedAgeMs"] = int(observed_age * 1000)
    if session.source == "claude-channel" or session.runtime == "claude_interactive":
        status["channelGeneration"] = channel_generation(
            build_version=session.channel_build_version,
            protocol_version=session.channel_protocol_version,
        )
    return status


def _actor(params: JsonObject) -> str:
    """Accept the MCP identity field while retaining the existing CLI wire."""

    return _required_string(params.get("actor") or params.get("from"), "actor")


def _agent_task_body(params: JsonObject) -> JsonObject:
    return {
        key: value
        for key, value in params.items()
        if key not in {"actor", "sessionRef"}
    }


def _status_query_timeout(params: JsonObject) -> float:
    """Bound the mesh leg of message.status; the local read never blocks."""

    raw = params.get("timeoutSeconds")
    if isinstance(raw, int | float) and not isinstance(raw, bool) and raw > 0:
        return min(float(raw), 10.0)
    return 2.0


def _canonical_holder_name(name: str) -> str:
    """Case-insensitive identity for holder names.

    A recipient URI's machine segment and a mesh holder (node id) name the
    same kind of thing, but nothing forces them to agree on casing, and a
    raw comparison would silently read a casing-only mismatch as "holder
    absent".  Every holder comparison must go through this normalizer.
    """
    return name.casefold()


def _delivery_status_projection(
    record: DeliveryStatus,
    *,
    local_node_id: str,
    responded_holders: frozenset[str],
    undecodable_holders: frozenset[str],
) -> JsonObject:
    """Expose uncertainty without rewriting the persisted terminal fact."""

    projected = record.to_json()
    if record.state is not TerminalState.EXPIRED:
        return projected
    parsed = parse_agent_uri(record.recipient)
    if parsed is None:
        reason = "RECIPIENT_HOLDER_UNRESOLVED"
    else:
        recipient_holder = _canonical_holder_name(parsed[1])
        # A recipient hosted on this node is confirmed by the local store
        # itself, so no mesh frame -- however malformed -- can unconfirm it.
        if recipient_holder == _canonical_holder_name(local_node_id):
            return projected
        if recipient_holder in {
            _canonical_holder_name(holder) for holder in undecodable_holders
        }:
            reason = "RECIPIENT_STATUS_UNDECODABLE"
        elif recipient_holder in {
            _canonical_holder_name(holder) for holder in responded_holders
        }:
            return projected
        else:
            reason = "RECIPIENT_HOLDER_UNANSWERED"
    projected.update(
        {
            "state": "unconfirmed",
            "reason": reason,
            "observedState": record.state.value,
            "observedReason": record.reason,
        }
    )
    return projected
