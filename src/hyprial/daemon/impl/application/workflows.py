"""Workflow / PAC IPC surface and workflow admission wiring."""

from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.pac.workflows.runtime.types import WorkflowServiceError
from hyprial.daemon.impl.dispatch.admission import dispatch_gate
from hyprial.daemon.impl.dispatch.identity import dispatch_service_actor_uri
from hyprial.daemon.impl.alias_resolver import AliasSurface
from hyprial.daemon.impl.pac.contracts.workflow import WorkflowSpec
from hyprial.kernel import (
    canonical_user_uri,
    parse_agent_uri,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _required_string,
)


class _WorkflowSurfaceMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _ipc_dispatch_matrix_resolve(self, params) -> Any:
        from hyprial.daemon.impl.dispatch.matrix import resolve
        from hyprial.kernel import TIERS

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

    def _ipc_workflow_remote_current(self, params) -> Any:
        from hyprial.identity import PacError
        if self._remote_workflow is None:
            raise DaemonRequestError("WORKFLOW_REMOTE_UNAVAILABLE", "remote workflow service is not running")
        try:
            return self._remote_workflow.delivery_current(_required_string(params.get("messageId"), "messageId"))
        except PacError as error:
            raise DaemonRequestError(error.code, str(error)) from error

    def _ipc_workflow_worker_control(self, method, params) -> Any:
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

    def _ipc_workflow_start(self, params) -> Any:
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

    def _ipc_workflow_plan(self, params) -> Any:
        from hyprial.daemon.impl.pac.contracts.workflow import (
            WorkflowSchemaError,
            load_workflow_text,
        )
        from hyprial.identity import PacError

        source = self._workflow_caller(params)
        try:
            spec = load_workflow_text(_required_string(params.get("yaml"), "yaml"))
            admitted = self._workflow_admit(spec, source)
            return {"ok": True, "plan": admitted.to_json()}
        except (WorkflowServiceError, PacError) as error:
            raise DaemonRequestError(
                error.code, str(error), getattr(error, "data", None)
            ) from error
        except WorkflowSchemaError as error:
            raise DaemonRequestError(error.code, str(error)) from error

    def _ipc_workflow_expansion_plan(self, params) -> Any:
        from hyprial.daemon.impl.pac.graphs.authority.expansion import (
            expansion_preview,
            prepare_expansion,
        )
        from hyprial.daemon.impl.pac.storage.store import (
            PacGraphStore,
            default_database_path,
        )
        from hyprial.identity import PacError
        from hyprial.daemon.impl.pac.workflows.expansion.transactions import (
            expansion_deadline_ms,
        )

        source = self._workflow_caller(params)
        graph_id = _required_string(params.get("graphId"), "graphId")
        placeholder_id = _required_string(params.get("nodeId"), "nodeId")
        if self._remote_workflow is not None:
            forwarded = self._remote_workflow.preview_expansion(params, source)
            if forwarded is not None:
                return forwarded
        database = default_database_path(self.state_dir)
        store = PacGraphStore(database, read_only=True)
        try:
            graph = store.graph(graph_id)
            predecessor = store._db.execute(
                "SELECT e.from_node,w.request_id,w.deadline_ms,w.timeout_ms,n.owner "
                "FROM edges e JOIN workflow_nodes w ON w.graph_id=e.graph_id "
                "AND w.node_id=e.from_node JOIN nodes n ON n.graph_id=w.graph_id "
                "AND n.node_id=w.node_id WHERE e.graph_id=? AND e.to_node=? "
                "AND e.kind='forward'",
                (graph_id, placeholder_id),
            ).fetchone()
            placeholder = store._db.execute(
                "SELECT deadline_ms,timeout_ms FROM workflow_nodes "
                "WHERE graph_id=? AND node_id=? AND node_kind='expansion'",
                (graph_id, placeholder_id),
            ).fetchone()
            if graph is None or predecessor is None or placeholder is None:
                raise PacError(
                    "PAC_EXPANSION_INVALID",
                    "expansion placeholder is unavailable",
                    {"field": "placeholder", "reason": "required"},
                )
            if source not in (graph["created_by"], predecessor["owner"]):
                raise PacError(
                    ipc_errors.CALLER_NOT_AUTHORIZED,
                    "only the creator or corresponding planner may preview expansion",
                )
            request_id = predecessor["request_id"]
            if not isinstance(request_id, str):
                raise PacError(
                    "WORKFLOW_REQUEST_STALE", "the planner has no current request"
                )
            now = time.time_ns() // 1_000_000
            if (
                predecessor["deadline_ms"] is not None
                and now > predecessor["deadline_ms"]
            ):
                raise PacError(
                    "WORKFLOW_DEADLINE_EXPIRED",
                    "the planner deadline has passed",
                )
            deadline_ms = expansion_deadline_ms(
                authored_deadline_ms=placeholder["deadline_ms"],
                timeout_ms=int(placeholder["timeout_ms"]),
                at=now,
            )
        finally:
            store.close()
        try:
            prepare = (
                self._pac_graph_authority.prepare_expansion
                if self._pac_graph_authority is not None
                else prepare_expansion
            )
            context = prepare(
                database=database,
                home=self.hyprial_home,
                raw=_required_string(params.get("yaml"), "yaml"),
                graph_id=graph_id,
                node_id=str(predecessor["from_node"]),
                request_id=request_id,
                actor=str(predecessor["owner"]),
                at=now,
                machine=self.node_id,
                local_owner=self.owner,
                persist_artifacts=False,
            )
            self._workflow_admit(context.spec, source)
            return expansion_preview(context, deadline_ms=deadline_ms)
        except PacError as error:
            raise DaemonRequestError(error.code, str(error), error.data) from error

    def _ipc_workflow_complete_or_fail(self, method, params) -> Any:
        if self._workflow_service is None:
            raise DaemonRequestError(ipc_errors.WORKFLOW_UNAVAILABLE, "workflow service is not running")
        source = self._workflow_caller(params)
        graph_id = _required_string(params.get("graphId"), "graphId")
        node_id = _required_string(params.get("nodeId"), "nodeId")
        request = _required_string(params.get("requestId"), "requestId")
        reason = _required_string(params.get("reasonRef"), "reasonRef")
        expansion = params.get("expansion")
        from hyprial.identity import PacError
        from hyprial.daemon.impl.pac.workflows.outputs import validate_workflow_output_text
        try:
            output_text = validate_workflow_output_text(params.get("outputText"))
        except ValueError as error:
            raise DaemonRequestError(
                "WORKFLOW_OUTPUT_INVALID", str(error)
            ) from error
        try:
            if self._remote_workflow is not None:
                forwarded = self._remote_workflow.forward(method, params, source)
                if forwarded is not None:
                    return forwarded
            if method == "workflow.fail":
                if expansion is not None:
                    raise PacError(
                        "PAC_EXPANSION_INVALID",
                        "workflow.fail cannot carry expansion",
                        {"field": "expansion", "reason": "unexpected"},
                    )
                return self._workflow_service.fail(graph_id=graph_id, node_id=node_id,
                                                   actor=source, request_id=request, reason_ref=reason,
                                                   output_text=output_text)
            authority = self._pac_graph_authority
            context = None
            if expansion is not None:
                from hyprial.daemon.impl.pac.contracts.expansion.document import (
                    expansion_digest,
                )
                from hyprial.daemon.impl.pac.graphs.authority.expansion import (
                    prepare_expansion,
                )
                from hyprial.daemon.impl.pac.storage.store import (
                    PacGraphStore,
                    default_database_path,
                )
                from hyprial.daemon.impl.pac.workflows.expansion.results import (
                    accepted_replay_result,
                )

                database = default_database_path(self.state_dir)
                read = PacGraphStore(database, read_only=True)
                try:
                    replay = accepted_replay_result(
                        read._db,
                        graph_id=graph_id,
                        node_id=node_id,
                        request_id=request,
                        actor=source,
                        reason_ref=reason,
                        output_text=output_text,
                        expansion_digest=expansion_digest(expansion),
                    )
                finally:
                    read.close()
                if replay is not None:
                    return replay
                prepare = (
                    authority.prepare_expansion
                    if authority is not None
                    else prepare_expansion
                )
                context = prepare(
                    database=database,
                    home=self.hyprial_home,
                    raw=expansion,
                    graph_id=graph_id,
                    node_id=node_id,
                    request_id=request,
                    actor=source,
                    at=time.time_ns() // 1_000_000,
                    machine=self.node_id,
                    local_owner=self.owner,
                )
                self._workflow_admit(context.spec, source)
            if authority is not None:
                result = authority.set_flag(
                    graph_id, node_id, actor=source,
                    reason_ref=reason, expected_request=request,
                    output_text=output_text,
                    expansion=expansion,
                    expansion_context=context,
                )
                self._workflow_service.submit_timer(time.time_ns() // 1_000_000)
                return result
            if self._transport is not None:
                raise DaemonRequestError(
                    ipc_errors.WORKFLOW_UNAVAILABLE,
                    "PAC graph authority is not running",
                )
            from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
            from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
            store = PacGraphStore(default_database_path(self.state_dir))
            try:
                outcome = PacReactor(store, expansion_context=context).set_flag(graph_id, node_id, actor=source,
                                                     reason_ref=reason, expected_request=request,
                                                     output_text=output_text,
                                                     expansion=expansion)
            finally:
                store.close()
            self._workflow_service.submit_timer(time.time_ns() // 1_000_000)
            return {"ok": True, "event": outcome.event}
        except (WorkflowServiceError, PacError) as error:
            raise DaemonRequestError(
                error.code, str(error), getattr(error, "data", None)
            ) from error

    def _ipc_pac_write(self, method, params) -> Any:
        return self._handle_pac_write(method, params)

    def _ipc_workflow_status_group(self, method, params) -> Any:
        caller = self._workflow_caller(params)
        from hyprial.daemon.impl.pac.storage.legacy import LegacyWorkflowHistory
        from hyprial.identity import PacError
        try:
            if method.startswith("workflow.history."):
                history = LegacyWorkflowHistory(self.state_dir)
                viewer = None if caller == canonical_user_uri(self.owner) else caller
                if method.endswith("list"):
                    return history.list(limit=int(params.get("limit", 50)), viewer=viewer)
                return history.status(_required_string(params.get("runId"), "runId"), viewer=viewer)
            if self._workflow_service is None:
                raise DaemonRequestError(ipc_errors.WORKFLOW_UNAVAILABLE, "workflow service is not running")
            if method == "workflow.list":
                all_callers = params.get("all", False)
                if type(all_callers) is not bool:
                    raise DaemonRequestError(
                        ipc_errors.INVALID_ARGUMENT, "all must be a boolean"
                    )
                return self._workflow_service.list(limit=int(params.get("limit", 50)),
                    viewer=None if all_callers or caller == canonical_user_uri(self.owner) else caller)
            run_id = _required_string(params.get("runId"), "runId")
            if method == "workflow.cancel":
                return self._workflow_service.cancel(run_id=run_id, actor=caller)
            if method == "workflow.node.inspect" and self._remote_workflow is not None:
                forwarded = self._remote_workflow.forward(method, params, caller)
                if forwarded is not None:
                    return forwarded
            result = self._workflow_service.status(run_id=run_id)
            if caller != canonical_user_uri(self.owner) and caller != result["sender"] and not any(n["owner"] == caller for n in result["nodes"]):
                raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "caller is not a participant of this graph")
            if method == "workflow.node.inspect":
                target = _required_string(params.get("target"), "target")
                node = next((n for n in result["nodes"] if n["nodeId"] == target), None)
                if node is None:
                    raise DaemonRequestError("WORKFLOW_NODE_NOT_FOUND", target)
                if caller not in (canonical_user_uri(self.owner), result["sender"], node["owner"]):
                    raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "only the creator or node owner may read execution progress")
                from hyprial.daemon.impl.dispatch.workflow_observation import observe_node
                return observe_node(self._workflow_service.database, result, node, self._inbox,
                                    recipient=self._dispatch_service_actor,
                                    at=time.time_ns() // 1_000_000, epoch=self.epoch)
            return result
        except (WorkflowServiceError, PacError) as error:
            raise DaemonRequestError(error.code, str(error)) from error

    def _record_workflow_outcome(self, result):
        if self._remote_workflow is not None and self._remote_workflow.outcome(result):
            return True
        workflow = self._workflow_service
        if workflow is None:
            return False
        return workflow.record_harness_outcome(
            message_id=result.delivery_id, recipient=result.recipient,
            failed=result.status.value != "completed", failure_code=result.failure_code)

    def _workflow_admit(self, spec: WorkflowSpec, sender: str) -> WorkflowSpec:
        from hyprial.daemon.impl.pac.actors.daemon  import DaemonActorRuntime
        from hyprial.daemon.impl.pac.actors.coordinator.types import LaunchSpec

        from hyprial.identity import WORKFLOW_REMOTE_OWNER_UNSUPPORTED

        if any(node.expands is not None or node.kind == "expansion" for node in spec.nodes):
            from hyprial.daemon.impl.pac.contracts.expansion import (
                load_expansion_policy,
                validate_parent,
            )

            spec = validate_parent(
                spec, policy=load_expansion_policy(self.hyprial_home)
            )
        if spec.escalate_to is not None:
            self._validate_input_alias(
                spec.escalate_to,
                surface=AliasSurface.PAC,
            )
        for node in spec.nodes:
            if node.kind == "expansion":
                continue
            if node.owner is not None:
                # Existing workflow admission already rejects an unknown
                # owner as SENDER_UNRESOLVED. Preserve that public code and
                # payload while the resolver contributes ambiguity and
                # outside-domain classification.
                self._validate_input_alias(
                    node.owner,
                    surface=AliasSurface.PAC,
                    preserve_unknown=True,
                )
                if node.owner.startswith("user:"):
                    if node.owner != canonical_user_uri(self.owner):
                        raise DaemonRequestError(WORKFLOW_REMOTE_OWNER_UNSUPPORTED,
                            "workflow completion currently requires this daemon's local user or bound actors")
                    continue
                principal = parse_agent_uri(node.owner)
                if principal is not None and principal[:2] != (self.owner, self.node_id):
                    from hyprial.identity import PacError
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
        return spec

    def _workflow_caller(self, params: JsonObject) -> str:
        actor = params.get("actor")
        if actor == canonical_user_uri(self.owner) and "sessionRef" not in params:
            # The private local control socket is the trusted human boundary.
            return str(actor)
        return self._pac_bound_caller(params)

    @property
    def _dispatch_service_actor(self) -> str:
        """Canonical sender for daemon-owned dispatch and PAC notification IO."""

        return dispatch_service_actor_uri(self.owner, self.node_id)

    def _workflow_cleanup_attention_snapshot(self) -> list[JsonObject]:
        """Read durable terminal-cleanup debt for ps and doctor."""

        from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path

        database = default_database_path(self.state_dir)
        if not database.exists():
            return []
        store = PacGraphStore(database, read_only=True)
        try:
            return store.workflow_worker_cleanup_attention(
                time.time_ns() // 1_000_000
            )
        finally:
            store.close()

    def _pac_bound_caller(self, params: JsonObject) -> str:
        """Authenticate the acting PAC principal via the daemon session binding.

        actor + sessionRef must name a binding THIS daemon minted.  PAC owner-only checks
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

        from hyprial.identity import PacError
        from hyprial.daemon.impl.pac.graphs.edits import activate_graph, close_graph
        from hyprial.daemon.impl.pac.actors.coordinator.types import request_actor_stop
        from hyprial.daemon.impl.pac.graphs.reactor import PacReactor, planned_to_json
        from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
        from hyprial.daemon.impl.pac.graphs.authority.commands import PacGraphOverloaded, PacGraphTimeout

        from hyprial.daemon.impl.pac.actors.daemon  import DaemonPacNotificationSender

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
        authority = self._pac_graph_authority
        if authority is not None:
            graph_id = _required_string(params.get("graphId"), "graphId")
            try:
                if method == "pac.flag.set":
                    return authority.set_flag(
                        graph_id,
                        _required_string(params.get("nodeId"), "nodeId"),
                        actor=caller,
                        reason_ref=params.get("reasonRef"),
                        expected_request=params.get("expectedRequest"),
                    )
                if method == "pac.flag.reset":
                    return authority.reset_flag(
                        graph_id,
                        _required_string(params.get("nodeId"), "nodeId"),
                        actor=caller,
                        reason_ref=params.get("reasonRef"),
                    )
                if method == "pac.graph.activate":
                    return authority.activate_graph(graph_id, actor=caller)
                if method == "pac.graph.close":
                    return authority.close_graph(graph_id, actor=caller)
                if method == "pac.actor.stop":
                    return authority.stop_actor(
                        graph_id,
                        _required_string(params.get("actorName"), "actorName"),
                        actor=caller,
                    )
            except PacError as error:
                raise DaemonRequestError(error.code, str(error), error.data) from error
            except (PacGraphOverloaded, PacGraphTimeout) as error:
                raise DaemonRequestError(
                    ipc_errors.WORKFLOW_UNAVAILABLE, str(error)
                ) from error
        if self._transport is not None:
            raise DaemonRequestError(
                ipc_errors.WORKFLOW_UNAVAILABLE,
                "PAC graph authority is not running",
            )
        store = PacGraphStore(default_database_path(self.state_dir))
        try:
            if method in ("pac.flag.set", "pac.flag.reset"):
                graph_id = _required_string(params.get("graphId"), "graphId")
                node_id = _required_string(params.get("nodeId"), "nodeId")
                reason_ref = params.get("reasonRef")
                reactor = PacReactor(
                    store,
                    sender=DaemonPacNotificationSender(self),
                    logger=self._log,
                )
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
