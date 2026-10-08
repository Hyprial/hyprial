"""The daemon application: a thin composition root over cluster mixins.

``DaemonApplication`` remains the single state host and the construction /
shutdown / rollback / timer-callback authority.  The semantic clusters (IPC
server, dispatch, startup/shutdown, restore, lifecycle ops, routines, agents
admin, orgfs bridge, delivery, maintenance, ...) live in the ``application``
subpackages and ``hyprial.daemon.impl.ipc``; every method has exactly one
owner module and reads application state only through ``self``.
"""


from __future__ import annotations
from __future__ import annotations
import os
import queue
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TYPE_CHECKING
from uuid import uuid4
from hyprial.kernel import GenerationScheduler
from hyprial.daemon.impl.correlation.bounded_cadence  import BoundedCadence
from hyprial.daemon.impl.state_persistence  import StatePersistenceAuthority
from hyprial.daemon.impl.ipc.ipc_request_owner  import IpcRequestOwner
from hyprial.daemon.impl.dispatch.runtime.diagnostics  import DispatchDiagnostics
from hyprial.daemon.impl.network.routing.session_route_coordinator  import (
    SessionRouteCoordinator,
)
from hyprial.daemon.impl.adapters.lark.outbound.gateway import GatewayIoAuthority
from hyprial.daemon.impl.autoupdate.actor import AutoUpdateAuthority as InProcessAutoUpdateScheduler
from hyprial.identity import AgentKeepListAuthority as AgentKeepList
from hyprial.identity import (
    BlockingFailureAuthority,
    RestorePolicyAuthority,
)
from hyprial.daemon.impl.adapters.lark.runtime.runtime import (
    AdapterRuntime,
)
from hyprial.kernel import DaemonRequestError as DaemonRequestError
from hyprial.kernel import ReadinessReport
from hyprial.kernel import resolve_node_id
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryStatusEndpoint,
    RecipientWakeCoordinator,
    ZenohInboxEndpoint,
)
from hyprial.daemon.impl.inbox.tracking.fetch_receipts import FetchReceiptPublisher
from hyprial.kernel import configured_hyprial_home
from hyprial.kernel import Logger
from hyprial.daemon.impl.operations.management_actor import RegistryManagementAuthority as RegistryManagementHandler
from hyprial.daemon.impl.org import OrgContextMesh
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.kernel import PersistentConfigStore
from hyprial.daemon.impl.squire import (
    UserAdapterRegistry,
    ZenohUserDeliveryEndpoint,
    ZenohUserDeliveryTransport,
)
from hyprial.kernel import (
    FORWARDING_COMMAND_ENV,
    FORWARDING_UP_ENV,
)
from hyprial.daemon.impl.forwarding_config import (
    FORWARDING_DEFAULT_MODE,
    AutomaticForwarding,
    ForwardingPolicy,
)
from hyprial.daemon.impl.bootstrap.discovery  import (
    ForwardingEndpoints,
)
from hyprial.daemon.impl.forwarding  import (
    ExposureStore,
    ForwardingSidecarSupervisor,
)
from hyprial.daemon.impl.transport import (
    LivelinessDirectory,
)
from hyprial.daemon.impl.transport.session_actor import TransportSessionAuthority
from hyprial.daemon.impl.application.ports import (
    UsageCollectionFactory,
    QuotaEvaluatorFactory,
    QuotaWatchdogDeps,
    RoutineRuntime as RoutineRuntime,
    RoutineRuntimeFactory,
    RoutineServicePort,
    RoutineTextLoader,
    RoutineTemplateRenderer,
    UsageCollection,
)
from hyprial.daemon.impl.operations.watchdog_actor import AsyncInboxWatchdog as InboxWatchdog
from hyprial.daemon.impl.operations.watchdog_actor import AsyncQuotaWatchdog as QuotaWatchdog
from hyprial.daemon.impl.squire.actors.profile import UserProfileAuthority
from hyprial.daemon.impl.lifecycle.duplicate_actor  import DuplicateInstanceAuthority as DuplicateInstanceWatch
from hyprial.daemon.impl.lifecycle.maintenance_watchdog import ActorMaintenanceWatchdog as MaintenanceWatchdog
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.daemon.impl.inbox.links.io import (
    InboxDeliveryIoAdapter,
)
from hyprial.daemon.impl.state_db  import StateDatabase
from hyprial.daemon.impl.desired_state  import (
    DesiredState,
    DesiredStateStore,
)
from hyprial.daemon.impl.composition  import (
    AgentSessionDomains,
    CorrelatedDomainEvents,
    HarnessPortClient,
    LarkPortClient,
)
from hyprial.daemon.impl.harnesses.runtime.ports  import RestoreEligibilityProjection
from hyprial.daemon.impl.lifecycle.atomic_lifecycle_ports  import (
    AtomicLifecycleDomainPort,
)
from hyprial.daemon.impl.correlation.correlation  import CorrelationEventRouter
from hyprial.kernel import (
    TEARDOWN_BUDGETED_SECONDS as TEARDOWN_BUDGETED_SECONDS,
)
from hyprial.daemon.impl.lifecycle.lifecycle_coordinator  import LifecycleCoordinator
from hyprial.daemon.impl.route_registration  import (
    RouteRegistrationClient,
    RouteRegistrationIo,
)
from hyprial.daemon.impl.owner_migration  import migrate_owner_if_needed
from hyprial.daemon.impl.configuration.identity import (
    read_settings_identity_metadata,
    resolve_node_owner,
)
from hyprial.kernel  import CallCostCounters
from hyprial.daemon.impl.configuration.home_guard  import keepalive_duration_from_environment
from hyprial.daemon.impl.configuration.home_lease_actor  import HomeLeaseAuthority as ActiveDaemonHeartbeat
from hyprial.daemon.impl.runtime  import DaemonEventBridge, ReconcileSummary
from hyprial.daemon.impl.harnesses.turn_delivery.service  import (
    TurnHookService,
)
from hyprial.daemon.impl.operations.top  import InteractiveTurnStats
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.application.startup import _StartupMixin
from hyprial.daemon.impl.application.shutdown import _ShutdownMixin
from hyprial.daemon.impl.application.wiring.transport import _WiringTransportMixin
from hyprial.daemon.impl.application.wiring.services import _WiringServicesMixin
from hyprial.daemon.impl.application.netendpoints.endpoints import _EndpointResolutionMixin
from hyprial.daemon.impl.application.netendpoints.forwarding import _ForwardingOpsMixin
from hyprial.daemon.impl.application.netendpoints.services import _ServiceConnectOpsMixin
from hyprial.daemon.impl.application.restore import _RestoreMixin
from hyprial.daemon.impl.application.workflows import _WorkflowSurfaceMixin
from hyprial.daemon.impl.application.actors.agents_admin.registry_ops import _AgentRegistryOpsMixin
from hyprial.daemon.impl.application.actors.agents_admin.adapters import _AdapterAdminMixin
from hyprial.daemon.impl.application.actors.agents_admin.migration import _AgentMigrationMixin
from hyprial.daemon.impl.application.actors.agents_admin.destroys import _AgentDestroyMixin
from hyprial.daemon.impl.application.actors.agents_registry import _AgentsRegistryMixin
from hyprial.daemon.impl.application.actors.sessions.routes import _SessionRoutesMixin
from hyprial.daemon.impl.application.actors.sessions.registry import _SessionRegistryMixin
from hyprial.daemon.impl.application.actors.transfer import _TransferOpsMixin
from hyprial.daemon.impl.application.actors.lifecycle_ops import _LifecycleOpsMixin
from hyprial.daemon.impl.application.actors.routines import _RoutinesMixin
from hyprial.daemon.impl.application.messaging.delivery.lark import _LarkGatewayMixin
from hyprial.daemon.impl.application.messaging.delivery.sends import _UserDeliveryMixin
from hyprial.daemon.impl.application.messaging.delivery.status_queries import _DeliveryStatusMixin
from hyprial.daemon.impl.application.messaging.inbox_surface.send import _MessageSendMixin
from hyprial.daemon.impl.state_persistence.alarm import (
    CpuOwnerBudgetAlarm,
    StateWriterAlarm,
)
from hyprial.daemon.impl.application.messaging.operator_alarm import OperatorAlarmDelivery
from hyprial.daemon.impl.application.messaging.inbox_surface.queries import (
    _MessageQueriesMixin,
    new_message_wait_slots,
)
from hyprial.daemon.impl.application.messaging.status.views import _StatusViewsMixin
from hyprial.daemon.impl.application.messaging.status.snapshots import _WorkerSnapshotMixin
from hyprial.daemon.impl.application.messaging.orgfs_bridge import _OrgFsBridgeMixin
from hyprial.daemon.impl.application.messaging.visibility import _VisibilityMixin
from hyprial.daemon.impl.application.messaging.maintenance import _MaintenanceMixin
from hyprial.daemon.impl.ipc.router import _IpcDispatchMixin
from hyprial.daemon.impl.ipc.server import _IpcServerMixin
from hyprial.daemon.impl.identity import IdentityResolver

from hyprial.daemon.impl.application.netendpoints.endpoints import (
    _lock_wait_timeout as _lock_wait_timeout,
    _zenoh_endpoints as _zenoh_endpoints,
    compose_daemon_worker_launch as compose_daemon_worker_launch,
)
from hyprial.daemon.impl.application.messaging.visibility import (
    _message_origin as _message_origin,
)
from hyprial.daemon.impl.application.messaging.delivery.sends import (
    FORWARD_TARGET_UNKNOWN as FORWARD_TARGET_UNKNOWN,
    _undeliverable_outbox_recipient as _undeliverable_outbox_recipient,
)
from hyprial.daemon.impl.application.messaging.status.views import (
    _LocalPresence as _LocalPresence,
)
from hyprial.daemon.impl.application.messaging.maintenance import (
    _BoundedRecipientWakes as _BoundedRecipientWakes,
)
from hyprial.daemon.impl.alias_resolver import daemon_alias_resolver
from hyprial.daemon.impl.application.shutdown import (
    _StopRequestTrace as _StopRequestTrace,
)
from hyprial.daemon.impl.application.wiring.transport import (
    _HarnessActorRegistration as _HarnessActorRegistration,
)
from hyprial.daemon.impl.ipc.router import _IPC_STATS_METHODS as _IPC_STATS_METHODS
from hyprial.daemon.impl.ipc.server import _write_ipc_response as _write_ipc_response


from hyprial.daemon.impl.application.actors.sessions.routes import _InteractiveRouteEffectGate
from hyprial.daemon.impl.application.messaging.delivery.lark import _RouteGatewayCache
from hyprial.daemon.impl.application.messaging.visibility import _SENDER_AUTH_ENFORCE, _SENDER_AUTH_MODE_ENV, _SENDER_AUTH_OBSERVE
from hyprial.daemon.impl.application.netendpoints.endpoints import _endpoint_list, _isolated_endpoints, _resolve_forwarding, network_isolated_from_environment
from hyprial.daemon.impl.ipc.params import JsonObject
from hyprial.daemon.impl.ipc.server import _IPC_MAX_CLIENTS, _IpcListener, _IpcStream

class DaemonApplication(
    _StartupMixin,
    _ShutdownMixin,
    _WiringTransportMixin,
    _WiringServicesMixin,
    _EndpointResolutionMixin,
    _ForwardingOpsMixin,
    _ServiceConnectOpsMixin,
    _RestoreMixin,
    _WorkflowSurfaceMixin,
    _AgentRegistryOpsMixin,
    _AdapterAdminMixin,
    _AgentMigrationMixin,
    _AgentDestroyMixin,
    _AgentsRegistryMixin,
    _SessionRoutesMixin,
    _SessionRegistryMixin,
    _TransferOpsMixin,
    _LifecycleOpsMixin,
    _RoutinesMixin,
    _LarkGatewayMixin,
    _UserDeliveryMixin,
    _DeliveryStatusMixin,
    _MessageSendMixin,
    _MessageQueriesMixin,
    _StatusViewsMixin,
    _WorkerSnapshotMixin,
    _OrgFsBridgeMixin,
    _VisibilityMixin,
    _MaintenanceMixin,
    _IpcDispatchMixin,
    _IpcServerMixin,
):
    """Own the real runtime graph and expose it through newline-delimited IPC."""

    def _abort_initialization(self) -> None:
        """Release already-started authorities after a constructor failure."""

        for attribute, close_operation in (
            ("_maintenance_watchdog", "close"),
            ("_operator_alarm_delivery", "close"),
            ("_inbox_watchdog", "close"),
            ("_quota_watchdog", "close"),
            ("_blocking_failures", "close"),
            ("_restore_policy", "close"),
            ("_agent_keep", "close"),
            ("_agent_session_domains", "close"),
            ("user_profiles", "close"),
            ("_state_persistence", "close"),
            ("_gateway_logger", "close"),
            ("_logger", "close"),
        ):
            resource = getattr(self, attribute, None)
            if resource is not None:
                try:
                    getattr(resource, close_operation)()
                except BaseException:
                    pass

    def _construct_or_rollback(self, factory: Callable[[], Any]) -> Any:
        try:
            return factory()
        except BaseException:
            self._abort_initialization()
            raise

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
        usage_cache: UsageCollection | None = None,
        quota_evaluator_factory: QuotaEvaluatorFactory | None = None,
        routine_runtime_factory: RoutineRuntimeFactory,
        routine_text_loader: RoutineTextLoader,
        routine_template_renderer: RoutineTemplateRenderer,
        keepalive_duration: float = 5.0,
        forwarding_discovery: ForwardingEndpoints | None = None,
        forwarding_environment: Mapping[str, str] | None = None,
        forwarding_policy: ForwardingPolicy | None = None,
        forwarding_automatic: AutomaticForwarding | None = None,
        forwarding_unavailable: str | None = None,
        hook_consumers: Mapping[str, Any] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self._exposure_store = ExposureStore(
            self.state_dir / "network-exposures.json"
        )
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
        self._identity_resolver = IdentityResolver(
            state_dir=self.state_dir,
            hyprial_home=self.hyprial_home,
            owner=self.owner,
        )
        # One rollout switch.  This PR observes by default so existing
        # isolated/controlled scenario drivers stay usable while every
        # unauthenticated claim is counted.  The follow-up rollout flips the
        # default only after those callers carry real sessions.
        self._sender_auth_enforced = (
            os.environ.get(_SENDER_AUTH_MODE_ENV, _SENDER_AUTH_OBSERVE)
            .strip()
            .lower()
            == _SENDER_AUTH_ENFORCE
        )
        self._ipc_peer = threading.local()
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
        self._service_manager_lock = threading.Lock()
        self._service_manager: Any | None = None
        self._forwarding_dialed: tuple[str, ...] | None = None
        # The other two parts of the session's connect set, kept apart so a
        # forwarding redial recomposes it instead of dropping them: the
        # configured endpoints and what the host-tailnet directory returned
        # at startup (kept additive during migration, plan §F).
        self._connect_configured: tuple[str, ...] = ()
        self._connect_discovered: tuple[str, ...] = ()
        # Raw document retained from the discovery command already paid for at
        # startup. ``ps`` only projects this snapshot; it never probes peers.
        self._tailnet_status_snapshot: dict[str, object] | None = None
        self._tailnet_directory: object | None = None
        # The forwarding set a redial was last attempted for: a failed
        # rebuild is retried when the sidecar's set changes again, not on
        # every tick (a persistent failure stays visible as restartRequired).
        self._forwarding_redial_attempted: tuple[str, ...] | None = None
        # One shared state database (U0a-2 丙): desired state and the
        # lifecycle journal write through the same serialized connection
        # owner, so in-process contention between the two is gone by
        # construction.
        self.state_db = StateDatabase(self.state_dir / "lifecycle-operations.sqlite3")
        desired_store = DesiredStateStore(
            self.state_dir / "desired-state.json", state_db=self.state_db
        )
        self._state_persistence = self._construct_or_rollback(
            lambda: StatePersistenceAuthority(desired_store)
        )
        self.desired_state = self._state_persistence.desired
        self._state_writer_alarm = StateWriterAlarm()
        self._cpu_owner_alarm = CpuOwnerBudgetAlarm()
        self.persistent_config = PersistentConfigStore(self.hyprial_home, self.state_dir)
        self._profile_refresh_due = 0.0
        self._keep_refresh_due = 0.0
        self.user_adapters = UserAdapterRegistry()
        self.stop_event = threading.Event()
        self._stop_request_trace = _StopRequestTrace()
        self._trace_presence_enabled = os.environ.get("HYPRIAL_E2E_TRACE_PRESENCE") == "1"
        self.epoch = uuid4().hex
        self._workflow_service: GraphWorkflowService | None = None
        self._routine_service: RoutineServicePort | None = None
        self._routine_exists_probe: Callable[[str], bool] | None = None
        # Upper-layer (biz/shell) capabilities arrive through the ports in
        # ``application.ports``: required injected factories, never lazy or
        # biz imports (IR-DAEMON-003).
        self._routine_runtime_factory = routine_runtime_factory
        self._routine_text_loader = routine_text_loader
        self._routine_template_renderer = routine_template_renderer
        self._hook_consumers = dict(hook_consumers or {})
        self._routine_coordinator: Any | None = None
        self._routine_legacy_lock = threading.RLock()
        self._pac_notification_io: InboxDeliveryIoAdapter | None = None
        self._pac_actor_service: Any | None = None
        self._pac_graph_authority: Any | None = None
        self._pac_gc: Any | None = None
        self._lifecycle_manager: LifecycleCoordinator | None = None
        self._lifecycle_domain_ports: tuple[AtomicLifecycleDomainPort, ...] = ()
        self._route_registration: RouteRegistrationIo | None = None
        self._routes: RouteRegistrationClient | None = None
        self._lifecycle_router: CorrelationEventRouter | None = None
        self._runtime: DaemonEventBridge | None = None
        self._dispatch_cadence: BoundedCadence[ReconcileSummary] | None = None
        self._forwarding_cadence: BoundedCadence[None] | None = None
        self._session_route_coordinator: SessionRouteCoordinator | None = None
        self._turn_hooks: TurnHookService | None = None
        self._hook_bus: Any | None = None
        self._daemon_hooks: Any | None = None
        self._transport: TransportSessionAuthority | None = None
        self._fetch_receipt_publisher: FetchReceiptPublisher | None = None
        self._remote_workflow = None
        self._degraded_workflow_handles: list[Any] = []
        self._presence: _LocalPresence | None = None
        self._interactive_route_actor_versions: dict[str, int] = {}
        self._interactive_route_versions: dict[tuple[str, str | None], int] = {}
        self._directory: LivelinessDirectory | None = None
        self._inbox: DeliveryCustodyFacade | None = None
        self._outbox_recipient_wakes = _BoundedRecipientWakes()
        self._inbox_coordinator: DeliveryCustodyCoordinator | None = None
        self._recipient_wakes: RecipientWakeCoordinator | None = None
        self._stop_recipient_wake_observer: Callable[[], None] | None = None
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
        # True only between the runtime assigning ``_user_delivery`` and
        # shutdown clearing it: outside that span a missing transport is
        # "not wired yet / already torn down", not "not configured".
        self._user_delivery_settled = False
        self._operator_alarm_delivery: OperatorAlarmDelivery | None = (
            self._construct_or_rollback(
                lambda: OperatorAlarmDelivery(
                    delivery=lambda: self._user_delivery,
                    log=lambda level, component, event, **fields: self._log(
                        level, component, event, **fields
                    ),
                )
            )
        )
        self._org_endpoint: OrgContextMesh | None = None
        self._actor_token: Any | None = None
        self._duplicate_watch: DuplicateInstanceWatch | None = None
        # Set by the home guard's pre-claim copied-home detection: the frozen
        # record detail when this home was started from a live daemon's copy.
        self._startup_duplicate: JsonObject | None = None
        self._route_gateway_cache = _RouteGatewayCache()
        self._outbound_gateway_owners: set[GatewayIoAuthority] = set()
        self._interactive_route_lock = threading.RLock()
        self._interactive_route_actor_versions: dict[str, int] = {}
        self._interactive_route_versions: dict[tuple[str, str | None], int] = {}
        self._interactive_route_effect_gates: dict[
            str, _InteractiveRouteEffectGate
        ] = {}
        self._interactive_route_resource_tokens: dict[tuple[str, str], str] = {}
        self._interactive_turn_stats_lock = threading.Lock()
        self._interactive_turn_stats: dict[str, InteractiveTurnStats] = {}
        self._clock: Callable[[], float] = time.monotonic
        self._maintenance_scheduler = GenerationScheduler()
        # Sees the tick that never returns, which reconcile_overrun cannot.
        self._maintenance_watchdog = self._construct_or_rollback(lambda: MaintenanceWatchdog(
            log=lambda level, event, **fields: self._log(
                level, "daemon", event, **fields
            ),
            diagnostics=self._transport_lock_holder,
        ))
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
        # Per-request worker-status snapshot (ps / agent.list / agent.get).
        # IPC clients each run on their own thread, so the "current" snapshot
        # is a thread-local: one request's table never leaks into a
        # concurrent request's verdicts.
        self._worker_snapshot_local = threading.local()
        # Domain construction starts actors and replays persisted bindings;
        # their liveness callbacks need this state before construction returns.
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

        self._agent_session_domains = self._construct_or_rollback(lambda: AgentSessionDomains(
            database=self.state_dir / "agents.sqlite3",
            desired_state=self.desired_state,
            owner=self.owner,
            node_id=self.node_id,
            daemon_epoch=self.epoch,
            hyprial_home=self.hyprial_home,
            worker_running=actor_worker_running,
            clock=actor_clock,
            persistence_late_result=self._state_persistence.result,
            session_desired_state=getattr(
                self._state_persistence, "settled_desired", self.desired_state
            ),
        ))
        self.agents = self._agent_session_domains.agents
        self._alias_resolver = daemon_alias_resolver(
            home=self.hyprial_home,
            agents=self.agents,
            owner=self.owner,
            node_id=self.node_id,
            known_agent=self._known_local_agent_alias,
            log=lambda event, **fields: self._log(
                "warn", "daemon", event, **fields
            ),
        )
        self._agent_liveness = self._agent_session_domains.liveness
        # P1b B1: the compose path needs the registry's grant/receipt
        # surface, which the compatibility facade deliberately does not
        # expose — mutations go through Agent commands; the daemon-side
        # environment composition is a read, not a mutation.
        self._agent_registry = self._agent_session_domains._registry  # noqa: SLF001
        self._agent_migration_lock = threading.Lock()
        self._agent_keep = self._construct_or_rollback(lambda: AgentKeepList(
            self.state_dir / "agent-keep.json",
            normalize=self.agents.normalize_actor,
        ))
        self._restore_policy = self._construct_or_rollback(
            lambda: RestorePolicyAuthority(
                self.state_dir / "agent-restore-policy.json",
                normalize=self.agents.normalize_actor,
            )
        )
        self._restore_activity_unknown: set[str] = set()
        self._restore_policy_degraded_version = -1
        self._restore_eligibility_version = 0
        self._restore_eligibility_lock = threading.Lock()
        self._pending_restore_eligibility: dict[
            str, RestoreEligibilityProjection
        ] = {}
        self._restore_eligibility_capacity = 10_000
        self._restore_eligibility_batch = 64
        self._restore_classifying = False
        self._restore_wake_cadence: BoundedCadence[None] | None = None
        self._restore_wake_cursor = 0
        self._restore_wake_batch = 64
        self._restore_wake_lock = threading.Lock()
        self._restore_wake_requests: dict[str, str] = {}
        self._restore_wake_capacity = 10_000
        blocking_application_ref = weakref.ref(self)

        def commit_block(
            recipient: str, reason: str, expected_entity_token: str
        ):
            application = blocking_application_ref()
            if application is None:
                raise RuntimeError("daemon application is gone")
            return application._commit_blocking_failure(
                recipient, reason, expected_entity_token
            )

        def notify_block(text: str, *, idempotency_key: str):
            application = blocking_application_ref()
            if application is None:
                raise RuntimeError("daemon application is gone")
            return application._owner_alert_notifier(
                text, idempotency_key=idempotency_key
            )

        self._blocking_failures = self._construct_or_rollback(
            lambda: BlockingFailureAuthority(
                block=commit_block,
                notify=notify_block,
                capacity=10_000,
            )
        )
        self._restore_policy_finalizer = weakref.finalize(
            self, self._restore_policy.close
        )
        self._blocking_failures_finalizer = weakref.finalize(
            self, self._blocking_failures.close
        )
        # Preserve the authority constructed above; the legacy standalone store
        # must not overwrite the object whose close finalizer is registered.
        self._restore_policy_degraded: str | None = None
        self._restore_activity_unknown: set[str] = set()
        self._agent_activity_queue: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._agent_domains_finalizer = weakref.finalize(
            self, self._agent_session_domains.close
        )
        self._server: _IpcListener | None = None
        self._accept_reserve_fd: int | None = None
        self._ipc_client_slots = threading.BoundedSemaphore(_IPC_MAX_CLIENTS)
        self._ipc_request_owner: IpcRequestOwner | None = None
        self._dispatch_diagnostics: DispatchDiagnostics | None = None
        # Per-method handler cost (see ipc_stats for the attribution rule and
        # the calibration contract).  Owned here, not per connection: the
        # client threads are short-lived and the totals must outlive them.
        self._ipc_stats = CallCostCounters(_IPC_STATS_METHODS, wall=True)
        self._ipc_clients: set[_IpcStream] = set()
        self._ipc_client_threads: set[threading.Thread] = set()
        self._ipc_clients_lock = threading.Lock()
        self._ipc_closing = False
        self._ipc_force_closing = False
        # message.pending.wait holds; bounded apart from the IPC client slots.
        self._message_waits = new_message_wait_slots()
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
        self._lock_stream: Any | None = None
        self._logger = self._construct_or_rollback(lambda: Logger.daemon(
            self.state_dir, name=self.node_id, asynchronous=True, capacity=4096
        ))
        self._gateway_logger = self._construct_or_rollback(lambda: Logger.adapter(
            self.state_dir, name=self.node_id, asynchronous=True, capacity=4096
        ))
        self._legacy_agent_home_log_lock = threading.Lock()
        self._legacy_agent_home_logged: set[str] = set()
        self._orgfs_runtime: OrgFsRuntime | None = None
        self._org_identity_binding_projector: Any | None = None
        self._org_identity_binding_suppression: Any | None = None
        self._org_identity_binding_publication_lock = threading.Lock()
        self._org_identity_binding_pending_all = False
        self._org_identity_binding_pending_orgs: set[str] = set()
        self._org_context_bridge: Any | None = None
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
            if quota_evaluator_factory is None:
                self._log(
                    "error",
                    "daemon",
                    "quota_watchdog.disabled",
                    error="ValueError: quota evaluator factory is required when usage collection is enabled",
                )
            else:
                try:
                    quota_deps = QuotaWatchdogDeps(
                        state_dir=self.state_dir,
                        deliver=self._quota_watchdog_deliver,
                        readings=usage_cache.snapshots,
                        clock_ms=lambda: time.time_ns() // 1_000_000,
                        on_alert=lambda alert: self._log(
                            "info", "daemon", "quota_watchdog.alerted",
                            kind=alert.kind, key=alert.key,
                        ),
                    )
                    evaluator = quota_evaluator_factory(quota_deps)
                    if evaluator is None:
                        self._log(
                            "error",
                            "daemon",
                            "quota_watchdog.disabled",
                            error="quota evaluator factory returned None",
                        )
                    else:
                        self._quota_watchdog = self._construct_or_rollback(lambda: QuotaWatchdog(
                            state_dir=quota_deps.state_dir,
                            deliver=quota_deps.deliver,
                            readings=quota_deps.readings,
                            clock_ms=quota_deps.clock_ms,
                            on_alert=quota_deps.on_alert,
                            evaluator=evaluator,
                        ))
                        usage_cache.set_refresh_observer(self._on_usage_refreshed)
                except (OSError, ValueError) as error:
                    # An unreadable state file disables the watchdog loudly; it
                    # never blocks the daemon from starting.
                    self._log(
                        "error",
                        "daemon",
                        "quota_watchdog.disabled",
                        error=f"{type(error).__name__}: {error}",
                    )
        # Inbox watchdog (hq-adjutant 2026-09-18, after 11 messages expired
        # unread while their recipient kept working): says so BEFORE the
        # deadline, and says so again if the sweep throws mail away while the
        # recipient is running.  ⛔ It does not change what the sweep reaps.
        self._inbox_watchdog: InboxWatchdog | None = None
        self._inbox_watchdog_checked_at_ms = 0
        try:
            self._inbox_watchdog = self._construct_or_rollback(lambda: InboxWatchdog(
                state_dir=self.state_dir,
                deliver=self._inbox_watchdog_deliver,
                clock_ms=lambda: time.time_ns() // 1_000_000,
                on_alert=lambda alert: self._log(
                    "warn", "daemon", "inbox_watchdog.alerted",
                    kind=alert.kind, key=alert.key,
                ),
            ))
        except (OSError, ValueError) as error:
            self._log(
                "error",
                "daemon",
                "inbox_watchdog.disabled",
                error=f"{type(error).__name__}: {error}",
            )
        self._home_guard = self._construct_or_rollback(lambda: ActiveDaemonHeartbeat(
            self.hyprial_home,
            keepalive_duration=keepalive_duration,
            ownership_lost=self._stop_request_trace.bind(
                self.stop_event, "home-ownership-lost"
            ),
            duplicate_detected=self._on_startup_duplicate_detected,
            duplicate_check_failed=self._on_duplicate_check_failed,
        ))
        self._autoupdate = self._construct_or_rollback(lambda: InProcessAutoUpdateScheduler(
            state_dir=self.state_dir,
            hyprial_home=self.hyprial_home,
            logger=lambda level, event, **fields: self._log(
                level, "autoupdate", event, **fields
            ),
        ))
        self.user_profiles = self._construct_or_rollback(
            lambda: UserProfileAuthority(self.state_dir / "users.json")
        )

    @classmethod
    def from_environment(
        cls,
        *,
        state_dir: Path,
        socket_path: Path,
        usage_factory: UsageCollectionFactory,
        routine_runtime_factory: RoutineRuntimeFactory,
        routine_text_loader: RoutineTextLoader,
        routine_template_renderer: RoutineTemplateRenderer,
        quota_evaluator_factory: QuotaEvaluatorFactory | None = None,
        hook_consumers: Mapping[str, Any] | None = None,
    ) -> DaemonApplication:
        node_id = resolve_node_id()
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
            # The shell decides whether collection is enabled; the daemon
            # only consumes the injected usage collection port.
            usage_cache=None if isolated else usage_factory(),
            quota_evaluator_factory=quota_evaluator_factory,
            hook_consumers=hook_consumers,
            routine_runtime_factory=routine_runtime_factory,
            routine_text_loader=routine_text_loader,
            routine_template_renderer=routine_template_renderer,
            keepalive_duration=keepalive_duration_from_environment(),
            forwarding_environment=forwarding_environment,
            forwarding_policy=policy,
            forwarding_automatic=automatic,
            forwarding_unavailable=unavailable,
        )
        app.network_isolated = isolated
        return app
