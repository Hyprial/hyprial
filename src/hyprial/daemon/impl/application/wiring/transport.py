"""Runtime graph composition, transport phases: state preparation, endpoints, delivery plane and the runtime bridge."""

from __future__ import annotations

from __future__ import annotations
import os
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any, TYPE_CHECKING
from uuid import uuid4
from hyprial.daemon.impl.transfer.execution.container import CONTAINER_PYTHON as _CONTAINER_PYTHON
from hyprial.daemon.impl.adapters.lark.runtime.runtime import (
    AdapterRuntime,
    )
from hyprial.daemon.impl.adapters.lark.worker.process import (
    LarkWorkerLauncher,
)
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import (
    LarkReplyBridgeTransport,
    lark_reply_adapter,
)
from hyprial.kernel import DaemonStartupPhase, HOOKS_CONFIG_NAME, HookBus
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    DeliveryStatusEndpoint,
    HoldPolicy,
    LocalFirstDeliveryTransport,
    RecipientWakeCoordinator,
    ZenohDeliveryTransport,
    ZenohInboxEndpoint,
)
from hyprial.daemon.impl.inbox.tracking.fetch_receipts import FetchReceiptPublisher
from hyprial.daemon.impl.org import OrgContextMesh, OrgContextStore
from hyprial.daemon.impl.squire import (
    ReceiverUserDelivery,
    UserDeliveryLedger,
    ZenohUserDeliveryEndpoint,
    ZenohUserDeliveryTransport,
)
from hyprial.daemon.impl.correlation.deprecations  import deprecation_notices
from hyprial.daemon.impl.bootstrap.discovery  import (
    merge_endpoints,
)
from hyprial.daemon.impl.transport import (
    KeySpace,
    LivelinessDirectory,
    ZenohConfig,
    zenoh_environment_flag,
)
from hyprial.daemon.impl.transport.presence_actor import ActorOnlineTransition
from hyprial.daemon.impl.lifecycle.duplicate_actor  import DuplicateInstanceAuthority as DuplicateInstanceWatch
from hyprial.daemon.impl.inbox.links.io import (
    CorrelatedInboxEventRouter,
    InboxDeliveryIoAdapter,
)
from hyprial.daemon.impl.desired_state  import (
    DesiredState,
)
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.composition  import (
    AgentIdentityProjectionView,
    CorrelatedDomainEvents,
    HarnessPortClient,
    LarkDesiredStatePort,
    LarkPortClient,
)
from hyprial.daemon.impl.configuration.identity import (
    classify_target_identity,
    normalize_agent_recipient,
)
from hyprial.kernel import (
    TARGET_KIND_HOST,
    canonical_agent_uri,
    parse_agent_uri,
)
from hyprial.daemon.impl.runtime  import DaemonEventBridge
from hyprial.daemon.impl.harnesses.turn_delivery.protocol  import (
    HOOK_CONFIG_NAME,
    harness_supports_before_delivery,
)
from hyprial.daemon.impl.harnesses.turn_delivery.service  import (
    InboxTurnHookInvoker,
    TurnHookService,
    )
from hyprial.daemon.impl.hooks import CombinedTurnObserver, DaemonHookService
from hyprial.daemon.impl.harnesses.actor.actor  import HarnessRuntimeActor
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.application.messaging.status.views import (
    _LocalPresence,
)
from hyprial.daemon.impl.application.netendpoints.endpoints import (
    _open_transport,
    _reserve_loopback_endpoint,
    _zenoh_endpoints,
    compose_daemon_worker_launch,
)
from hyprial.daemon.impl.application.wiring.graph import (
    _StartupGraph,
)


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
        self._token_closed = False
        self._endpoint_closed = False
        self._close_reported = False
        self._invalidation_reported = False
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()

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
        with self._close_lock:
            with self._lock:
                if self._closed:
                    return
                report = not self._close_reported
                self._close_reported = True
            if report:
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
            errors: list[str] = []
            with self._lock:
                endpoint_closed = self._endpoint_closed
                token_closed = self._token_closed
            if not endpoint_closed:
                try:
                    self._close_child(
                        self._endpoint, reason=reason, initiator=initiator
                    )
                except BaseException as error:
                    errors.append(f"endpoint:{type(error).__name__}:{error}")
                else:
                    with self._lock:
                        self._endpoint_closed = True
            if not token_closed:
                try:
                    self._close_child(
                        self._token, reason=reason, initiator=initiator
                    )
                except BaseException as error:
                    errors.append(f"token:{type(error).__name__}:{error}")
                else:
                    with self._lock:
                        self._token_closed = True
            with self._lock:
                self._closed = self._endpoint_closed and self._token_closed
            if errors:
                raise RuntimeError(
                    "partial harness actor registration cleanup: "
                    + "; ".join(errors)
                )


class _WiringTransportMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _start_runtime(self) -> None:
        """Compose the runtime graph in explicit semantic startup phases.

        Construction and rollback authority is unchanged: each phase owns a
        boundary the rollback path (``retain_runtime_composition`` plus
        ``run``'s finally) already relied on, and ``_StartupGraph`` carries
        only startup-scoped wiring values between the phases.
        """
        desired = self._startup_prepare_state()
        transport, forwarding_listen = self._startup_open_transport()
        graph = self._startup_compose_delivery_plane(transport, forwarding_listen)
        self._startup_compose_runtime_bridge(graph, desired)
        self._startup_adopt_domain_services(graph)
        self._startup_finalize_runtime(graph)

    def _startup_prepare_state(self) -> DesiredState:
        """Load configuration and desired state; deprecated fields speak up."""
        self._startup_step(
            lambda: None, DaemonStartupPhase.ACTOR_RUNTIME_AGENTS_STORE
        )
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
        return desired

    def _startup_open_transport(self) -> tuple[Any, tuple[str, ...]]:
        """Resolve endpoints (isolation, derivation, discovery) and open zenoh."""
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
        # endpoints rather than network addresses.
        # A node that only connects out is invisible to everyone else, and
        # -- the part that made this hard to find -- it cannot tell.  It sees
        # every peer it dialed, so from the inside nothing is wrong.  Before
        # the tailnet cutover a node with no listen endpoint derived its own;
        # since 2026-10-03 inbound reachability is the Tailcat sidecar's, and
        # ``_derive_listen_endpoint`` is an always-None seam kept for the
        # fallback budget below.
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
        derived_listen: str | None = None
        if not listen and not self.network_isolated:
            derived_listen = self._derive_listen_endpoint()
            if derived_listen is not None:
                listen = (derived_listen,)
                self._startup_network["listenDerived"] = True
                self._log(
                    "info",
                    "zenoh",
                    "zenoh.listen.derived",
                    endpoint=derived_listen,
                    detail=(
                        "no listen endpoint was configured; derived this "
                        "node's tailnet address so other nodes can reach it"
                    ),
                )
        # Automatic forwarding's inbound listener is ADDED after the node's
        # own listener step: setting it as the listen list used to suppress
        # derivation and cost the official listener (plan §A).
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
        self.zenoh_connect = connect
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
        transport, forwarding_listen, derived_error = self._startup_step(
            lambda: _open_transport(
                config,
                forwarding_listen,
                derived_listen=derived_listen,
                event_sink=lambda event, fields: self._log(
                    "info", "zenoh", event, **fields
                ),
            ),
            DaemonStartupPhase.ACTOR_RUNTIME_TRANSPORT,
        )
        # Retain what the opened session actually uses.  Only a forwarding-port
        # re-pick or the derived-listener fallback can change the config, so
        # read it back only then; otherwise the requested listen set stands.
        self.zenoh_listen = (
            transport.config.listen
            if forwarding_listen or derived_error is not None
            else listen
        )
        if derived_error is not None:
            self._startup_network["listenDerived"] = False
            self._log(
                "warn",
                "zenoh",
                "zenoh.listen.derived_unavailable",
                endpoint=derived_listen,
                error=derived_error,
                reason="DERIVED_LISTEN_OPEN_FAILED",
                detail=(
                    "the session could not open with the best-effort derived "
                    "listener; reopened once without it"
                ),
            )
        if not self.zenoh_listen:
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
        if not self.zenoh_listen and not connect:
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
        return transport, forwarding_listen

    def _startup_compose_delivery_plane(self, transport: Any, forwarding_listen: tuple[str, ...]) -> _StartupGraph:
        """Compose presence, adapters, durable inbox, turn hooks, harness actor."""
        trace_options = (
            {"observation_sink": self._trace_presence_observation}
            if getattr(self, "_trace_presence_enabled", False) else {}
        )
        directory = LivelinessDirectory(transport, **trace_options)
        presence = _LocalPresence(
            directory,
            self.node_id,
            interactive_liveness=self._interactive_actor_liveness,
        )
        self._require_orgfs_runtime().bind_transport(
            transport,
            observe_supplier_online=directory.observe_actor_online,
            supplier_online=lambda supplier: (
                classify_target_identity(supplier) == TARGET_KIND_HOST
                and presence.actor_online(supplier)
            ),
            holder_candidates=lambda: tuple(
                actor
                for actor in presence.raw_online_actors()
                if classify_target_identity(actor) == TARGET_KIND_HOST
                and presence.liveness_keeps(actor)
            ),
        )
        self._start_org_identity_bindings_safely()
        network_delivery = ZenohDeliveryTransport(
            transport, presence, origin_node=self.node_id
        )
        # Local-first wrapper (#101): a managed local actor is delivered by
        # calling ``inbox.receive`` directly, because one Zenoh session cannot
        # receipt its own publication.  The terminal-state semantics survive
        # unchanged on this path: ``receive`` writes the authoritative
        # ``fetched`` / ``ACK_RECEIVED`` record inside the transaction that
        # commits the message. The holder's receipt mirror carries the same
        # public reason, while recipient-node provenance keeps the direct
        # observation authoritative.
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
            self.load_persistent_configuration().channels,
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
        hold_policy = HoldPolicy.from_environment()
        inbox_coordinator = self._startup_step(
            lambda: DeliveryCustodyCoordinator(
                inbox_database,
                delivery,
                shared_inbox_events,
                node_id=self.node_id,
                service_options={
                    "hold_policy": hold_policy,
                    "logger": self._logger,
                    "alarm_human_delivery": deliver_human_alarm,
                    # Resolve the actor projection live; session mutations no
                    # longer require rebuilding delivery policy.
                    "interactive_recipient": lambda recipient: recipient
                    in self._registered_interactive_actors(),
                },
            ),
            DaemonStartupPhase.ACTOR_RUNTIME_INBOX_STORE,
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

        def before_delivery_supported(actor: str) -> bool:
            parsed = parse_agent_uri(actor)
            if parsed is None or parsed[1] != self.node_id:
                return True
            spec = next(
                (
                    item
                    for item in self.desired_state.load().harnesses
                    if item.name == parsed[2]
                ),
                None,
            )
            if spec is None:
                return True
            return harness_supports_before_delivery(spec.harness)

        hook_invoker = InboxTurnHookInvoker(
            inbox,
            callback_actor=canonical_agent_uri(
                self.owner, self.node_id, "_turn-hooks"
            ),
        )
        turn_hooks = TurnHookService(
            home_for_agent=lambda actor: Path(
                self._agent_session_domains.agent.prevalidate_home(actor).path
            ),
            invoker=hook_invoker,
            logger=lambda level, component, event, **fields: self._log(
                level, component, event, **fields
            ),
            before_delivery_supported=before_delivery_supported,
            config_path_for_agent=lambda actor: (
                Path(self._agent_session_domains.agent.resolve_workspace_path(actor)).parent
                / "config"
                / HOOK_CONFIG_NAME
            ),
            actor_mode=True,
        )
        hook_bus = HookBus(
            logger=lambda level, component, event, **fields: self._log(
                level, component, event, **fields
            )
        )
        daemon_hooks = DaemonHookService(
            bus=hook_bus,
            config_path_for_agent=lambda actor: (
                Path(self._agent_session_domains.agent.resolve_workspace_path(actor)).parent
                / "config"
                / HOOKS_CONFIG_NAME
            ),
            consumers=self._hook_consumers,
            handler_notifier=hook_invoker.notify,
            logger=lambda level, component, event, **fields: self._log(
                level, component, event, **fields
            ),
        )
        self._turn_hooks = turn_hooks
        self._hook_bus = hook_bus
        self._daemon_hooks = daemon_hooks
        # Deferred import: hyprial.harnesses eagerly imports .agent_sdk, which
        # imports hyprial.daemon.impl.api; hyprial.daemon eagerly imports this module, so a
        # module-level import here would re-enter while hyprial.harnesses is still
        # partially initialized (import cycle introduced by merging the Agent
        # SDK driver with the application lifecycle). Both sides stay intact;
        # by call time every module is fully loaded.
        from hyprial.daemon.impl.harnesses import HarnessLauncher
        from hyprial.daemon.impl.harnesses.worker_channel import WorkerChannel, build_worker_channel

        def worker_channel(spec: HarnessLaunchSpec) -> WorkerChannel:
            # Mint the worker's own canonical identity from the SAME source the
            # actor registrar advertises (self._canonical_harness_uri), so the
            # worker's MCP inbox key never drifts from where deliveries land.
            actor = self._canonical_harness_uri(spec.name, spec)
            session_ref = uuid4().hex
            from hyprial.identity import DEFAULT_AGENT_TOOL_PROFILE

            runtime_context = self._agent_session_domains.agent.prepare_runtime_context(
                actor,
                spec.harness,
                spec.cwd,
                DEFAULT_AGENT_TOOL_PROFILE,
                containerized=spec.containerized,
                legacy_reporter=self._report_legacy_agent_home,
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
                daemon_epoch=self.epoch,
                child_environment_factory=child_environment,
                runtime_launch_custody=self._agent_runtime_launch_custody,
                state_dir=self.state_dir,
                turn_failure_observer=(
                    provider_auth.handle_turn_failure
                    if provider_auth is not None
                    else None
                ),
                turn_completed_observer=CombinedTurnObserver(
                    turn_hooks.observe_turn, daemon_hooks
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
            desired_state=getattr(
                self._state_persistence, "settled_desired", self.desired_state
            ),
            persistence_late_result=self._state_persistence.result,
            agent_identity=AgentIdentityProjectionView(
                self._agent_session_domains
            ),
        )
        harnesses = HarnessPortClient(harness_actor, harness_events)
        # `as_mailbox` used to declare the liveliness token and nothing else:
        # senders saw a mailbox and published custody to it, but no node ever
        # subscribed to `custody/<node>`, so the custody receipt never came
        # back, `_attempt_custody` reported failure, and the sender silently
        # kept the message.  A mailbox that advertises itself must be able to
        # take delivery, or the advertisement is a lie.


        return _StartupGraph(
            transport=transport,
            forwarding_listen=forwarding_listen,
            directory=directory,
            presence=presence,
            local_delivery=local_delivery,
            delivery=delivery,
            lark_events=lark_events,
            adapters=adapters,
            lark_client=lark_client,
            inbox_coordinator=inbox_coordinator,
            inbox=inbox,
            shared_inbox_events=shared_inbox_events,
            hold_policy=hold_policy,
            turn_hooks=turn_hooks,
            harnesses=harnesses,
        )

    def _startup_compose_runtime_bridge(self, graph: _StartupGraph, desired: DesiredState) -> None:
        """Bind service endpoints, start the bridge, retain the composition."""
        endpoint = ZenohInboxEndpoint(
            graph.transport,
            graph.inbox,
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
            graph.transport,
            graph.inbox.delivery_status,
            holder=self.node_id,
            observer=self._log_status_query,
        )
        user_receiver = ReceiverUserDelivery(
            node_id=self.node_id,
            profiles=self.user_profiles,
            adapters=self.user_adapters,
            ledger=UserDeliveryLedger(self.state_dir / "user-deliveries.json"),
            reload_adapters=self._reload_user_adapters,
            delivery_agent_delivery=self._deliver_to_live_user_proxy,
            logger=lambda level, event, **fields: self._log(
                level, "user-delivery", event, **fields
            ),
            identity_resolver=self._identity_resolver,
        )
        user_endpoint = ZenohUserDeliveryEndpoint(graph.transport, user_receiver)
        user_delivery = ZenohUserDeliveryTransport(
            graph.transport,
            logger=self._logger.bind(component="user-delivery"),
        )
        graph.org_endpoint = OrgContextMesh(
            graph.transport,
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
            transport=graph.transport,
            inbox=graph.inbox,
            harnesses=graph.harnesses,
            presence=graph.presence,
            delivery=graph.delivery,
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
            blocking_failure_observer=self._blocking_failures.submit,
            blocking_failure_identity=self._blocking_failure_entity_token,
            blocked_actor=self.agents.is_blocked,
            forwarder=self._forward_as_actor,
            owner_notifier=self._owner_alert_notifier,
            owner_requester_addresses=self._owner_requester_addresses(),
            hold_ttl_ms=graph.hold_policy.ttl_ms,
            turn_hooks=graph.turn_hooks,
            actor_mode=True,
        )
        recipient_wakes: RecipientWakeCoordinator | None = None
        stop_recipient_wake_observer: Callable[[], None] | None = None
        wake_admission_lock = threading.Lock()
        wake_admission_open = True

        def submit_recipient_wake(transition: ActorOnlineTransition) -> PortAdmission:
            with wake_admission_lock:
                if not wake_admission_open:
                    return PortAdmission.CLOSING
                assert recipient_wakes is not None
                return recipient_wakes.submit(transition)

        def stop_recipient_wake_observer_if_bound() -> None:
            nonlocal wake_admission_open
            with wake_admission_lock:
                wake_admission_open = False
            if stop_recipient_wake_observer is not None:
                stop_recipient_wake_observer()

        def retain_runtime_composition() -> None:
            """Keep constructed owners reachable for the normal _close retry."""

            self._transport = graph.transport
            self._directory = graph.directory
            self._presence = graph.presence
            self._inbox = graph.inbox
            self._inbox_coordinator = graph.inbox_coordinator
            self._recipient_wakes = recipient_wakes
            self._stop_recipient_wake_observer = stop_recipient_wake_observer
            self._harnesses = graph.harnesses
            self._turn_hooks = graph.turn_hooks
            self._adapters = graph.adapters
            self._lark_events = graph.lark_events
            self._lark_client = graph.lark_client
            self._endpoint = endpoint
            self._status_endpoint = status_endpoint
            self._user_endpoint = user_endpoint
            self._user_delivery = user_delivery
            self._org_endpoint = graph.org_endpoint
            self._actor_token = actor_token
            self._duplicate_watch = duplicate_watch
            self._runtime = runtime

        duplicate_watch: DuplicateInstanceWatch | None = None
        actor_token = None
        try:
            recipient_wakes = RecipientWakeCoordinator(
                graph.inbox_coordinator,
                graph.shared_inbox_events,
                graph.inbox,
                graph.directory.presence_projection,
            )
            # One typed ingress coalesces committed online transitions. It
            # advances eligible due times; the Inbox pump performs delivery.
            stop_recipient_wake_observer = graph.directory.bind_actor_online(
                submit_recipient_wake
            )
            graph.recovery = runtime.start()
            actor_token = graph.transport.declare_liveliness(
                KeySpace().actor_liveliness(self.node_id)
            )
            self._trace_presence_announcement()
            # The watch's own constructor closes its token if observing
            # fails, so a raised construction leaves nothing behind.
            duplicate_watch = DuplicateInstanceWatch(
                graph.transport,
                self.node_id,
                self._home_guard.generation,
                logger=lambda level, event, **fields: self._log(
                    level, "daemon", event, **fields
                ),
            )
        except BaseException as startup_error:
            # Production run() reaches its finally/_close after this raise.
            # Publish every constructed owner first so that one established
            # dependency order handles both successful and partial starts.
            retain_runtime_composition()
            try:
                stop_recipient_wake_observer_if_bound()
            except BaseException as error:
                raise BaseExceptionGroup(
                    "daemon startup and observer rollback failed",
                    [startup_error, error],
                ) from startup_error
            raise
        assert recipient_wakes is not None
        assert stop_recipient_wake_observer is not None
        self._transport = graph.transport
        self._fetch_receipt_publisher = FetchReceiptPublisher(graph.transport, logger=self._log)
        if self._forwarding_automatic is not None and graph.forwarding_listen:
            # Only now is the inbound port certainly this daemon's: the
            # session bound it.  The first dial rides the reconciler's redial
            # on the next maintenance tick (~1 s).
            target = graph.forwarding_listen[0].removeprefix("tcp/")
            self._forwarding_environment = self._forwarding_automatic.environment(
                target
            )
            self._start_forwarding_supervisor()
        self._directory = graph.directory
        self._presence = graph.presence
        self._inbox = graph.inbox
        self._inbox_coordinator = graph.inbox_coordinator
        self._recipient_wakes = recipient_wakes
        self._stop_recipient_wake_observer = stop_recipient_wake_observer
        self._harnesses = graph.harnesses
        self._adapters = graph.adapters
        self._lark_events = graph.lark_events
        self._lark_client = graph.lark_client
        self._endpoint = endpoint
        self._status_endpoint = status_endpoint
        self._user_endpoint = user_endpoint
        self._user_delivery = user_delivery
        self._user_delivery_settled = True
        self._org_endpoint = graph.org_endpoint
        self._actor_token = actor_token
        self._duplicate_watch = duplicate_watch
        self._runtime = runtime
        self._startup_step(
            lambda: self._start_lifecycle_manager(
                transport=graph.transport,
                inbox=graph.inbox,
                local_delivery=graph.local_delivery,
                harnesses=graph.harnesses,
            ),
            DaemonStartupPhase.ACTOR_RUNTIME_LIFECYCLE_STORE,
        )
