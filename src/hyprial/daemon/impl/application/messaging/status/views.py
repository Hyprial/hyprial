"""Operator status views: ps/top/targets/hosts/org/whoami projections.

Decision (UA2, 2026-11-18): ``targets`` lists ``user:<owner>`` with status
``user-agent`` while this node holds the owner's user agent (a message to it
is queued and recorded here); user agent addresses stay hidden (UA1b).

Decision (T1b, 2026-11-21): ``top`` keeps reading the link service's window
fields (``capacityW``, ``outboundInFlight``) through ``link_top_fields``, a
read of service state that never asks the gate; the fields stay published
until T1c.  ``inboundAwaitingInjection`` is no longer computed: it counted
governed rows the gate had not yet admitted, and with dispatch no longer
asking the gate no row is held, so the field reports 0.
"""

from __future__ import annotations

import os
import time
from typing import Any, TYPE_CHECKING
from hyprial import __version__
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.configuration.identity import (
    DELIVERABLE_TARGET_KINDS,
    classify_target_identity,
    normalize_agent_recipient,
)
from hyprial.identity import (
    visible_targets,
)
from hyprial.kernel import (
    TARGET_KIND_AGENT,
    TARGET_KIND_HOST,
    parse_agent_uri,
)
from hyprial.daemon.impl.ipc.ipc_stats  import ipc_stats_payload
from hyprial.daemon.impl.operations.top  import build_top_snapshot
from hyprial.daemon.impl.user_agent.runtime import (
    listed_agents,
    user_agent_status,
    without_user_agents,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _required_string,
)
from hyprial.daemon.impl.application.messaging.status.presence import _LocalPresence as _LocalPresence
from hyprial.daemon.impl.application.messaging.status.refresh import (
    refresh_response_fields,
    request_status_refresh,
    status_refresh_fields,
    storage_health_status,
    zenoh_session_status,
)




class _StatusViewsMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _ipc_stats_snapshot(self) -> dict[str, Any]:
        """The ``ipcStats`` block; also read by the CPU owner budget alarm."""

        domains = self._agent_session_domains
        return ipc_stats_payload(
            methods=self._ipc_stats,
            session_actor=domains.session.command_costs,
            agent_actor=domains.agent.command_costs,
            effect_admission=domains.session.effect_admission_costs,
            session_persistence_io=domains.session.persistence_io_costs,
            state_persistence=self._state_persistence.command_costs,
        )

    def _request_status_refresh(self):  # type: ignore[no-untyped-def]
        return request_status_refresh(self)

    def _status_refresh_fields(
        self, operation_id: str | None = None
    ) -> dict[str, object]:
        return status_refresh_fields(self, operation_id)

    def _zenoh_session_status(
        self, operation_id: str | None = None
    ) -> dict[str, object]:
        return zenoh_session_status(self, operation_id)

    def _storage_health_status(self) -> dict[str, object]:
        return storage_health_status(self)

    def _refresh_response_fields(self, params) -> dict[str, object]:
        return refresh_response_fields(self, params)

    def _ipc_ps(self, params) -> Any:
        now = time.monotonic()
        restore_now_ms = self._restore_now_ms()
        # Status is observational. Recovery and route expiry are maintenance
        # responsibilities and must never run under an IPC read owner.
        interactive_sessions = self._agent_session_domains.session.read_sessions()
        local_agents = self.agents.list()
        # Node-wide pending: rows wait under agent URIs, not the node
        # id, so scoping this to the node inbox would report an empty
        # node with a full one (the runner-observation blind spot).
        pending = self._inbox.pending_all()
        historical_inbox_recipients = self._historical_inbox_recipients()
        # One request, one worker-status snapshot: connectors[] and every
        # agents[] verdict read the same tables instead of each verdict
        # paying its own supervisor round trip and per-connector loads.
        with self._worker_status_snapshot(sessions=interactive_sessions) as worker_status:
            connector_statuses = [dict(item) for item in worker_status.statuses]
            desired_by_key = {
                (spec.harness, spec.name): spec
                for spec in (worker_status.desired.harnesses if worker_status.desired else ())
            }
            for row in connector_statuses:
                if row.get("runtime") != "jev":
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
            restore_rows: list[JsonObject] = []
            for spec in desired_by_key.values():
                agent = self.agents.get(spec.name)
                if agent is None:
                    continue
                projection = self._restore_agent_projection(
                    agent, now_ms=restore_now_ms, spec=spec
                )
                if not projection:
                    continue
                restore_rows.append(projection)
                key = (spec.harness, spec.name)
                existing = next(
                    (
                        row
                        for row in connector_statuses
                        if (row.get("runtime"), row.get("name")) == key
                    ),
                    None,
                )
                if existing is not None:
                    existing.update(projection)
                    continue
                if projection.get("restoreStatus") == "idle-suppressed":
                    connector_statuses.append(
                        {
                            "id": f"{spec.harness}:{spec.name}",
                            "runtime": spec.harness,
                            "name": spec.name,
                            "running": False,
                            **projection,
                        }
                    )
            dormant = [
                row
                for row in restore_rows
                if row.get("restoreStatus") == "idle-suppressed"
            ]
            blocked = [
                row
                for row in restore_rows
                if row.get("restoreStatus") == "blocked"
            ]
            return {
                **self._refresh_response_fields(params),
                "daemon": {
                    "running": True,
                    "pid": os.getpid(),
                    "epoch": self.epoch,
                    "version": __version__,
                    "nodeId": self.node_id,
                    "owner": self.owner,
                    "socket": str(self.socket_path),
                    "lifecycle": self._lifecycle_status(),
                    "systemNotices": (
                        self._inbox.system_notice_counts(self.node_id)
                        if callable(getattr(self._inbox, "system_notice_counts", None)) else None
                    ),
                    "hooks": [
                        {
                            "name": lane.name,
                            "events": list(lane.events),
                            "accepted": lane.accepted,
                            "overloaded": lane.overloaded,
                            "closed": lane.closed,
                            "delivered": lane.delivered,
                            "failed": lane.failed,
                            "shut": lane.shut,
                        }
                        for lane in (
                            self._daemon_hooks.projection()
                            if self._daemon_hooks is not None
                            else ()
                        )
                    ],
                    "dispatchWithoutPacCount": self._dispatch_without_pac_snapshot(),
                    "dispatchConversationCount": self._dispatch_conversation_snapshot(),
                    "fetchReceiptHints": (
                        {
                            "accepted": receipt_status.accepted,
                            "published": receipt_status.published,
                            "failed": receipt_status.failed,
                            "rejected": receipt_status.rejected,
                            "closed": receipt_status.closed,
                        }
                        if self._fetch_receipt_publisher is not None
                        and (receipt_status := self._fetch_receipt_publisher.projection())
                        else None
                    ),
                    "dispatchCadence": (
                        {
                            "accepted": cadence_status.accepted,
                            "overloaded": cadence_status.overloaded,
                            "coalesced": cadence_status.coalesced,
                            "completed": cadence_status.completed,
                            "failed": cadence_status.failed,
                            "active": cadence_status.active,
                        }
                        if self._dispatch_cadence is not None
                        and (cadence_status := self._dispatch_cadence.projection())
                        else None
                    ),
                    "dispatchState": (
                        {
                            "version": dispatch_state.version,
                            "tables": dict(dispatch_state.table_sizes),
                        }
                        if self._runtime is not None
                        and (dispatch_state := getattr(
                            self._runtime, "dispatch_state_projection", None
                        )) is not None
                        else None
                    ),
                    "dispatchOffers": (
                        {
                            "pending": offer_status.pending,
                            "completed": offer_status.completed,
                            "overloaded": offer_status.overloaded,
                            "failed": offer_status.failed,
                            "closing": offer_status.closing,
                        }
                        if self._runtime is not None
                        and (offer_status := getattr(
                            self._runtime, "dispatch_offer_projection", None
                        )) is not None
                        else None
                    ),
                    "holdRefresh": (
                        {
                            "pending": hold_status.pending,
                            "rejected": hold_status.rejected,
                            "completed": hold_status.completed,
                            "failed": hold_status.failed,
                        }
                        if self._runtime is not None
                        and (hold_status := getattr(
                            self._runtime, "hold_refresh_projection", None
                        )) is not None
                        else None
                    ),
                    "progressPublish": (
                        {
                            "pending": progress_status.pending,
                            "published": progress_status.published,
                            "rejected": progress_status.rejected,
                            "failed": progress_status.failed,
                        }
                        if self._runtime is not None
                        and (progress_status := getattr(
                            self._runtime, "progress_publish_projection", None
                        )) is not None
                        else None
                    ),
                    "orphanCollection": (
                        self._harnesses.orphan_collection_status()
                        if self._harnesses is not None
                        and callable(getattr(self._harnesses, "orphan_collection_status", None))
                        else None
                    ),
                    "ipcRequests": (
                        {
                            "accepted": request_status.accepted,
                            "overloaded": request_status.overloaded,
                            "completed": request_status.completed,
                            "failed": request_status.failed,
                            "active": request_status.active,
                            "highWater": request_status.high_water,
                        }
                        if self._ipc_request_owner is not None
                        and (request_status := self._ipc_request_owner.projection())
                        else None
                    ),
                    "ipcClients": self._ipc_client_status(),
                    "routineCoordinator": (
                        self._routine_coordinator.stats()
                        if self._routine_coordinator is not None else None
                    ),
                    "logWriter": (
                        {
                            "accepted": log_status.accepted,
                            "written": log_status.written,
                            "failed": log_status.failed,
                            "rejected": log_status.rejected,
                            **(
                                {"lastError": log_status.last_error}
                                if log_status.last_error is not None
                                else {}
                            ),
                        }
                        if (log_status := self._logger.writer_status()) is not None
                        else None
                    ),
                    # Additive and read-only: the per-method cost
                    # counters plus processCpuSeconds read at the same
                    # moment, so two ps snapshots reconcile the
                    # counters against process CPU (ipc_stats docstring).
                    "ipcStats": self._ipc_stats_snapshot(),
                    "degradedRedemption": self._membership_redemption_projection(),
                    # Additive: the single state writer's queue and latency
                    # (lifecycle root-fix plan S1).  Round trip minus
                    # execution is time queued behind other commands.
                    "statePersistenceLatency": (
                        self._state_persistence.latency_status()
                    ),
                },
                "zenoh": {
                    "listen": list(self.zenoh_listen),
                    "connect": list(self.zenoh_connect),
                    "session": self._zenoh_session_status(
                        params.get("refreshOperationId")
                        if isinstance(params.get("refreshOperationId"), str)
                        else None
                    ),
                    "isolated": self._network_isolation_status()["effective"],
                    "isolation": self._network_isolation_status(),
                },
                "duplicateInstance": self._duplicate_instance_payload(),
                "userAgent": user_agent_status(self, local_agents),
                "maintenance": self._maintenance_watchdog.status(),
                "storageHealth": self._storage_health_status(),
                "orphanPersistence": (
                    self._harnesses.orphan_persistence_stats()
                    if callable(getattr(self._harnesses, "orphan_persistence_stats", None))
                    else {}
                ),
                "forwarding": self._forwarding_status_json(),
                "tailnet": self._tailnet_status_json(),
                "workerProxy": self._worker_proxy_status_json(),
                "workflowWorkerCleanup": {
                    "attention": self._workflow_cleanup_attention_snapshot()
                },
                **(
                    {"pacGc": self._pac_gc.status()}
                    if self._pac_gc is not None
                    else {}
                ),
                "connectors": connector_statuses,
                "restore": {
                    "restoreThresholdMs": self._restore_threshold_for_status(),
                    "suppressedCount": len(dormant),
                    "oldestIdleAgeMs": max(
                        (
                            int(row["idleAgeMs"])
                            for row in dormant
                            if isinstance(row.get("idleAgeMs"), int)
                        ),
                        default=None,
                    ),
                    "unknownActivityCount": len(self._restore_activity_unknown),
                    "blockedCount": len(blocked),
                    "oldestBlockedAgeMs": max(
                        (
                            int(row["blockedAgeMs"])
                            for row in blocked
                            if isinstance(row.get("blockedAgeMs"), int)
                        ),
                        default=None,
                    ),
                    "degraded": self._restore_policy_degraded is not None,
                    **(
                        {"degradedReason": self._restore_policy_degraded}
                        if self._restore_policy_degraded is not None
                        else {}
                    ),
                },
                "executionRuntimes": [],
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
                    self._agent_status_json(agent)
                    for agent in listed_agents(local_agents)
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
                # Same bare node-id presence projection as ``hosts``.
                # Unlike tailnet peers, every entry is known to be a
                # Hyprial daemon rather than an arbitrary device.
                "meshNodes": list(self._online_host_nodes()),
                "mailboxes": list(self._presence.online_mailboxes()),
            }

    def _ipc_top_snapshot(self) -> Any:
        now_ms = int(time.time() * 1000)
        # A batch path like ps: without the request snapshot every online
        # actor's liveness probe falls back to one supervisor round trip
        # plus one full desired-state load per connector
        # (_managed_worker_running).  In production that was ~475 SQLite
        # reads for 77 actors and `hyprial agent top` timed out at 15 s every
        # time while `ps`, which installs the snapshot, answered.
        with self._worker_status_snapshot():
            actor_statuses = without_user_agents(self._actor_status_snapshot(), self.agents.list())
        current_session_refs = {
            session.actor: session.session_ref
            for session in self._agent_session_domains.session.read_sessions()
        }
        with self._interactive_turn_stats_lock:
            interactive_turn_stats = {
                actor: stats
                for actor, stats in self._interactive_turn_stats.items()
                if current_session_refs.get(actor) == stats.session_ref
            }
        result = build_top_snapshot(
            state_dir=self.state_dir,
            owner=self.owner,
            node_id=self.node_id,
            epoch=self.epoch,
            daemon_pid=os.getpid(),
            connectors=[],
            actor_statuses=actor_statuses,
            interactive_turn_stats=interactive_turn_stats,
            pending=self._pending_recipient_stats(),
            agent_address=self._agent_address,
            read_keys=self._consumer_read_keys,
            stranded=self._historical_inbox_recipients(),
            now_ms=now_ms,
            dispatch_without_pac_count=self._dispatch_without_pac_snapshot(),
            dispatch_conversation_count=self._dispatch_conversation_snapshot(),
            batch_claims=(self._runtime.batch_claim_details(now_ms) if self._runtime else {}),
        )
        result["quota"] = (
            self._usage_cache.snapshot_payload(now_ms)
            if self._usage_cache is not None
            else {"sources": []}
        )
        return result

    def _ipc_identity_whoami(self, params) -> Any:
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

    def _ipc_org_publish(self) -> Any:
        if self._org_endpoint is None:
            raise DaemonRequestError(
                ipc_errors.DAEMON_NOT_READY, "org-context mesh endpoint is not ready"
            )
        try:
            published = self._org_endpoint.publish_accepted()
        except Exception as error:  # noqa: BLE001 - orgfs publication is independent
            published = False
            self._log(
                "warn", "org", "org.context.publish_failed", detail=str(error)
            )
        accepted_exists = (self.hyprial_home / "org-context.md").is_file()
        return {
            "published": published,
            "orgfsPublished": (
                self._require_org_context_bridge().publish_accepted()
                if accepted_exists
                else False
            ),
        }

    def _ipc_org_fetch(self, params) -> Any:
        from hyprial.daemon.impl.org.orgfs_migration import (
            OrgMigrationError,
            read_org_fetch_source,
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
        try:
            fetch_source = read_org_fetch_source(self.hyprial_home)
        except ValueError as error:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, str(error)
            ) from error
        if fetch_source == "orgfs":
            raw_source = params.get("from")
            source = None
            if raw_source is not None:
                source = _required_string(raw_source, "from")
                if not source.startswith("user:"):
                    source = normalize_agent_recipient(source)
            try:
                return self._require_org_context_bridge().fetch(
                    source=source,
                    timeout=float(raw_timeout),
                )
            except OrgMigrationError as error:
                raise DaemonRequestError(
                    error.code, str(error), error.data
                ) from error
        if self._org_endpoint is None:
            raise DaemonRequestError(
                ipc_errors.DAEMON_NOT_READY, "org-context mesh endpoint is not ready"
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

    def _ipc_targets(self, params) -> Any:
        with self._worker_status_snapshot():
            kind = params.get("kind")
            if kind is not None and kind not in DELIVERABLE_TARGET_KINDS:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "kind must be agent or user",
                )
            from hyprial.daemon.impl.user_agent.local_target import own_user_target_rows
            from hyprial.daemon.impl.user_agent.naming import is_user_agent, is_user_agent_address

            rows: dict[str, JsonObject] = {}
            local_agents = {agent.uri: agent for agent in self.agents.list()}
            # Agent rows are a projection of the same canonical snapshot top
            # consumes. A registered but stopped actor remains visible as
            # offline; an interactive/MCP actor has processState=n/a in the
            # source snapshot instead of being invented as a stopped process.
            for actor_status in self._actor_status_snapshot():
                actor = str(actor_status["actor"])
                if is_user_agent_address(actor) or is_user_agent(local_agents.get(actor)):
                    continue
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
                        agentId=agent.to_json()["agentId"],
                        displayName=agent.display,
                    )
            if kind in (None, TARGET_KIND_AGENT):
                from hyprial.daemon.impl.org.network.member_agents import chat_directory_for_app

                chat_directory = chat_directory_for_app(self)
                for row in chat_directory["targets"]:
                    uri = str(row["targetUri"])
                    if uri in rows:
                        rows[uri]["orgs"] = row["orgs"]
                        for field in ("agentId", "displayName"):
                            if not rows[uri].get(field) and row.get(field):
                                rows[uri][field] = row[field]
                    else:
                        rows[uri] = row
            rows.update(own_user_target_rows(self))
            targets = [rows[uri] for uri in sorted(rows)]
            if kind is not None:
                targets = [item for item in targets if item["targetKind"] == kind]
            caller = self._visibility_caller(params)
            if caller is not None and caller.hosted:
                # AT09 (card 358): a hosted visitor without a grant sees
                # only itself, and a withdrawal takes effect on the next
                # call because the ledger is read here, not cached.
                targets = visible_targets(
                    targets,
                    caller=caller,
                    grants=self._visibility_grants(caller.uri),
                )
            return {
                "targets": targets,
                **(
                    {"orgDirectoryStatus": chat_directory["orgDirectoryStatus"]}
                    if kind in (None, TARGET_KIND_AGENT)
                    and chat_directory["orgDirectoryStatus"]
                    and (caller is None or not caller.hosted)
                    else {}
                ),
            }

    def _ipc_hosts(self, params) -> Any:
        with self._worker_status_snapshot():
            current = set(self._online_host_nodes())
        prefix = "host-presence:"
        observed_targets = self._status_refresh.targets(prefix)
        global_value = self._status_refresh.snapshot("host-presence")
        global_nodes = set(global_value) if isinstance(global_value, tuple) else set()
        retained_unknown = {
            target.removeprefix(prefix)
            for target in observed_targets
            if self._status_refresh.snapshot(target) != "online"
        }
        nodes = current | retained_unknown
        global_status = self._status_refresh.status("host-presence")
        rows = []
        for node in sorted(nodes):
            target = f"{prefix}{node}"
            evidence = self._status_refresh.snapshot(target)
            fields = self._status_refresh.status(target)
            if fields["observedAtMs"] is None and node in global_nodes:
                evidence, fields = "online", global_status
            rows.append(
                {
                    "nodeId": node,
                    "status": (
                        "online"
                        if node in current
                        or (evidence == "online" and not fields["expired"])
                        else "unknown"
                    ),
                    "observedAtMs": fields["observedAtMs"],
                    "ageMs": fields["ageMs"],
                    "stale": fields["stale"],
                    "lastError": fields["lastError"],
                }
            )
        return {**self._refresh_response_fields(params), "hosts": rows}

    def _online_host_nodes(self) -> tuple[str, ...]:
        """Online bare node identities proven by daemon mesh presence.

        The caller owns a worker-status snapshot so ``liveness_keeps`` stays a
        table lookup. This mirrors the established ``hosts`` authority for the
        ``ps.meshNodes`` allowlist used by peer reachability diagnostics.
        """

        presence = self._presence
        if presence is None:
            return ()
        raw_online_actors = getattr(presence, "raw_online_actors", None)
        liveness_keeps = getattr(presence, "liveness_keeps", None)
        if not callable(raw_online_actors) or not callable(liveness_keeps):
            return ()
        return tuple(
            sorted(
                actor
                for actor in raw_online_actors()
                if classify_target_identity(actor) == TARGET_KIND_HOST
                and liveness_keeps(actor)
            )
        )

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
            current = self._agent_address(actor)
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

    def _orphan_process_status(self) -> tuple[dict[str, object], ...]:
        status = getattr(self._harnesses, "orphan_status", None)
        return status() if callable(status) else ()
