"""The daemon IPC dispatch router: handle(), the restore gate and the generic method families."""

from __future__ import annotations

from __future__ import annotations
import hashlib
import os
from typing import Any, TYPE_CHECKING
from hyprial import __version__
from hyprial.daemon.impl.autoupdate import SCHEDULE
from hyprial.identity import (
    AgentError,
    AgentHomeError as RegistryHomeError,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.operations.management import (
    EnsureSquireRegistryCommand,
)
from hyprial.kernel import (
    canonical_user_uri,
    DEFAULT_PEER_PORT,
)
from hyprial.daemon.impl.forwarding  import (
    ForwardingSidecarError,
    validate_exposure_target,
    validate_peer_key_address,
)
from hyprial.daemon.impl.composition  import (
    DomainCommandError,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _required_string,
)


_RESTORE_GATE_LIGHT_METHODS = frozenset({"ping", "shutdown"})

_IPC_STATS_METHODS = frozenset(
    {
        "worker.channel.request",
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
        "agent.home-census",
        "agent.host-invite",
        "agent.keep.add",
        "agent.keep.list",
        "agent.keep.remove",
        "agent.list",
        "agent.migrate.execute",
        "agent.migrate.preflight",
        "agent.migrate.rollback",
        "agent.resolve",
        "agent.restore-policy",
        "agent.restore-threshold",
        "agent.revoke",
        "agent.runtime-context",
        "agent.runtime-launch.acquire",
        "agent.runtime-launch.custody",
        "agent.runtime-launch.release",
        "agent.secret.grant",
        "agent.secret.list",
        # Spelled split exactly like its dispatch site (term lint).
        "agent.secret." + "provider-write",
        "agent.secret.revoke",
        "agent.unblock",
        "autoupdate.notify",
        "autoupdate.status",
        "autoupdate.trigger",
        "dispatch.matrix.resolve",
        "down",
        "hosts",
        "identity.whoami",
        "identity.resolve",
        "identity.bindings.list",
        "identity.override.set",
        "identity.override.clear",
        "identity.users.list",
        "identity.users.add",
        "lifecycle.start",
        "lifecycle.start-smolvm",
        "management.adapter.remove",
        "management.squire.ensure",
        "message.ack",
        "message.pending.list",
        "message.pending.wait",
        "message.query",
        "message.reply",
        "message.send",
        "message.status",
        "network.expose",
        "network.exposures",
        "network.peerKey",
        "network.unexpose",
        "org.create",
        "org.delete",
        "org.execute",
        "org.fetch",
        "org.invite",
        "org.join",
        "org.leave",
        "org.list",
        "org.network",
        "org.publish",
        "org.remove",
        "orgfs.create",
        "orgfs.checkout",
        "orgfs.export",
        "orgfs.history",
        "orgfs.import",
        "orgfs.invite",
        "orgfs.join",
        "orgfs.ls",
        "orgfs.members",
        "orgfs.mkdir",
        "orgfs.move",
        "orgfs.purge",
        "orgfs.purge_plan",
        "orgfs.purge_status",
        "orgfs.read",
        "orgfs.read_at",
        "orgfs.remove",
        "orgfs.remove_member",
        "orgfs.resolve",
        "orgfs.restore",
        "orgfs.serve",
        "orgfs.spaces",
        "orgfs.stat",
        "orgfs.stat_at",
        "orgfs.status",
        "orgfs.trash",
        "orgfs.unban",
        "orgfs.watch",
        "orgfs.write",
        "outbox.list",
        "outbox.prune",
        "pac.actor.stop",
        "pac.gc.preview",
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
        "routine.set",
        "routine.status",
        "session.heartbeat",
        "session.refresh",
        "session.register",
        "session.turn.ended",
        "session.unregister",
        "service.connect",
        "service.disconnect",
        "service.guide",
        "service.list",
        "shutdown",
        "targets",
        "top.snapshot",
        "transfer.complete",
        "transfer.land",
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
        "workflow.plan",
        "workflow.expansion.plan",
        "workflow.remote.current",
        "workflow.start",
        "workflow.status",
        "workflow.worker.restart",
        "workflow.worker.stop",
    }
)



# Org network lifecycle (tailnet cutover §4.3/§5): IPC method -> handler name in
# ``daemon.impl.org.network.ipc``. Imported lazily so the router stays cheap.
_ORG_NETWORK_METHODS = {
    "org.create": "ipc_org_create",
    "org.delete": "ipc_org_delete",
    "org.execute": "ipc_org_execute",
    "org.invite": "ipc_org_invite",
    "org.join": "ipc_org_join",
    "org.leave": "ipc_org_leave",
    "org.list": "ipc_org_list",
    "org.network": "ipc_org_network",
    "org.remove": "ipc_org_remove",
}

class _IpcDispatchMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def handle(
        self,
        method: str,
        params: JsonObject,
        *,
        _trusted_message_origin: str | None = None,
    ) -> Any:
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
        if method in {
            "service.connect",
            "service.disconnect",
            "service.guide",
            "service.list",
        }:
            from hyprial.daemon.impl.service_connect import ServiceConnectError

            try:
                return self._handle_service_connect(method, params)
            except ServiceConnectError as error:
                raise DaemonRequestError(error.code, str(error)) from error
        assert self._inbox is not None
        assert self._harnesses is not None
        assert self._presence is not None
        if method == "worker.channel.request":
            return self._ipc_worker_channel_request(params)
        if method.startswith("orgfs."):
            return self._ipc_orgfs(
                method, params, _trusted_message_origin=_trusted_message_origin
            )
        if method == "management.squire.ensure":
            return self._ipc_management_squire_ensure(params)
        if method == "management.adapter.remove":
            return self._ipc_management_adapter_remove(params)
        if method == "network.exposures":
            return self._ipc_network_exposures()
        if method == "network.expose":
            return self._ipc_network_expose(params)
        if method == "network.unexpose":
            return self._ipc_network_unexpose(params)
        if method == "network.peerKey":
            return self._ipc_network_peer_key(params)
        if method == "autoupdate.status":
            return self._ipc_autoupdate_status()
        if method == "autoupdate.trigger":
            return self._ipc_autoupdate_trigger()
        if method == "autoupdate.notify":
            return self._ipc_autoupdate_notify(params)
        if method == "ps":
            return self._ipc_ps(params)
        if method == "top.snapshot":
            return self._ipc_top_snapshot()
        if method == "identity.whoami":
            return self._ipc_identity_whoami(params)
        if method.startswith("identity."):
            from hyprial.daemon.impl.identity import handle_identity_ipc

            return handle_identity_ipc(
                self._identity_resolver,
                method,
                params,
                operator=_trusted_message_origin != "worker",
            )
        if method == "org.publish":
            return self._ipc_org_publish()
        if method == "org.fetch":
            return self._ipc_org_fetch(params)
        if method in _ORG_NETWORK_METHODS:
            from hyprial.daemon.impl.org.network import ipc as org_network_ipc

            handler = getattr(org_network_ipc, _ORG_NETWORK_METHODS[method])
            return handler(self, params)
        if method == "targets":
            return self._ipc_targets(params)
        if method == "hosts":
            return self._ipc_hosts()
        if method == "session.register":
            return self._ipc_session_register(params)
        if method == "session.turn.ended":
            return self._ipc_session_turn_ended(params)
        if method == "session.refresh":
            return self._ipc_session_refresh(params)
        if method == "session.heartbeat":
            return self._ipc_session_heartbeat(params)
        if method == "session.unregister":
            return self._ipc_session_unregister(params)
        if method == "message.query":
            return self._ipc_message_query(params)
        if method == "message.pending.list":
            return self._ipc_message_pending_list(method, params, _trusted_message_origin=_trusted_message_origin)
        if method == "message.pending.wait":
            return self._ipc_message_pending_wait(method, params, _trusted_message_origin=_trusted_message_origin)
        if method.startswith("routine."):
            routine_caller = self._workflow_caller(params)
            if method in (
                "routine.remove",
                "routine.pause",
                "routine.resume",
                "routine.set",
                "routine.status",
            ) and self._routine_service is not None:
                current = self._routine_service.status(name=_required_string(params.get("name"), "name"))
                if routine_caller not in (canonical_user_uri(self.owner), current["owner"], current.get("actor")):
                    raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "caller does not own this routine")
        if method == "routine.add":
            return self._ipc_routine_add(params)
        if method == "routine.list":
            return self._ipc_routine_list(params, routine_caller)
        if method == "routine.status":
            return self._ipc_routine_status(params)
        if method == "routine.audit":
            return self._ipc_routine_audit()
        if method == "routine.remove":
            return self._ipc_routine_remove(params)
        if method == "routine.set":
            return self._ipc_routine_set(params)
        if method == "routine.pause":
            return self._ipc_routine_pause(params)
        if method == "routine.resume":
            return self._ipc_routine_resume(params)
        if method == "dispatch.matrix.resolve":
            return self._ipc_dispatch_matrix_resolve(params)
        if method == "workflow.remote.current":
            return self._ipc_workflow_remote_current(params)
        if method in ("workflow.worker.stop", "workflow.worker.restart"):
            return self._ipc_workflow_worker_control(method, params)
        if method == "workflow.start":
            return self._ipc_workflow_start(params)
        if method == "workflow.plan":
            return self._ipc_workflow_plan(params)
        if method == "workflow.expansion.plan":
            return self._ipc_workflow_expansion_plan(params)
        if method in ("workflow.complete", "workflow.fail"):
            return self._ipc_workflow_complete_or_fail(method, params)
        if method in (
            "pac.flag.set",
            "pac.flag.reset",
            "pac.graph.activate",
            "pac.graph.close",
            "pac.actor.stop",
        ):
            return self._ipc_pac_write(method, params)
        if method in ("workflow.status", "workflow.list", "workflow.node.inspect", "workflow.cancel", "workflow.history.list", "workflow.history.status"):
            return self._ipc_workflow_status_group(method, params)
        if method == "progress.list":
            return self._ipc_progress_list(method, params, _trusted_message_origin=_trusted_message_origin)
        if method == "message.send":
            return self._ipc_message_send(method, params, _trusted_message_origin=_trusted_message_origin)
        if method == "message.reply":
            return self._ipc_message_reply(method, params, _trusted_message_origin=_trusted_message_origin)
        if method == "message.status":
            return self._ipc_message_status(params, _trusted_message_origin=_trusted_message_origin)
        if method == "message.ack":
            return self._ipc_message_ack(method, params, _trusted_message_origin=_trusted_message_origin)
        if method == "outbox.list":
            return self._ipc_outbox_list()
        if method == "outbox.prune":
            return self._ipc_outbox_prune(params)
        if method in {"lifecycle.start", "lifecycle.start-smolvm"}:
            return self._ipc_lifecycle_start(method, params)
        if method == "down":
            return self._ipc_down(params)
        if method == "transfer.land":
            return self._ipc_transfer_land(params)
        if method == "transfer.plan":
            return self._ipc_transfer_plan(params)
        if method == "transfer.quiesce":
            return self._ipc_transfer_quiesce(params)
        if method == "transfer.precheck":
            return self._ipc_transfer_precheck(params)
        if method == "transfer.receive":
            return self._ipc_transfer_receive(params)
        if method == "transfer.resume":
            return self._ipc_transfer_resume(params)
        if method == "transfer.complete":
            return self._ipc_transfer_complete(params)
        if method == "adapter.reload":
            return self._ipc_adapter_reload()
        if method == "adapter.list":
            return self._ipc_adapter_list()
        if method == "adapter.status":
            return self._ipc_adapter_status(params)
        if method == "adapter.start":
            return self._ipc_adapter_start(params)
        if method == "adapter.stop":
            return self._ipc_adapter_stop(params)
        if method == "adapter.pin":
            return self._ipc_adapter_pin(params)
        if method == "adapter.unpin":
            return self._ipc_adapter_unpin(params)
        if method == "adapter.pins":
            return self._ipc_adapter_pins()
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
            return self._ipc_shutdown()
        raise DaemonRequestError(ipc_errors.METHOD_NOT_FOUND, f"unknown daemon method {method}")

    def _ipc_worker_channel_request(self, params) -> Any:
        from hyprial.kernel import WorkerBinding, bound_request

        if set(params) != {"actor", "sessionRef", "daemonEpoch", "request"}:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "Invalid worker envelope")
        binding = WorkerBinding(params["actor"], params["sessionRef"], params["daemonEpoch"])
        inner_method, inner_params = bound_request(params["request"], binding)
        # A relay is a capability of THIS managed worker generation. An
        # interactive session alone must not authorize this transport, and
        # a restart never silently renews an old relay. Reuse the existing
        # supervisor projection and session fence, not a parallel ledger.
        if (
            binding.daemon_epoch != self.epoch
            or self._worker_session_ref(binding.actor) != binding.session_ref
        ):
            raise DaemonRequestError(ipc_errors.STALE_SESSION, "Worker channel binding expired")
        if self._fence_interactive_session(binding.actor, inner_params) != binding.actor:
            raise DaemonRequestError(ipc_errors.STALE_SESSION, "Worker channel actor moved")
        return self.handle(
            inner_method, inner_params, _trusted_message_origin="worker"
        )

    def _ipc_management_squire_ensure(self, params) -> Any:
        command = EnsureSquireRegistryCommand.from_payload(params)
        result = self._registry_management_handler().ensure_squire(command)
        return {"ok": True, **result.to_payload()}

    def _ipc_management_adapter_remove(self, params) -> Any:
        from hyprial.daemon.impl.bootstrap.adapter_registration import (
            AdapterConfigConflictError,
            AdapterNotFoundError,
            remove_lark_gateway,
        )
        from hyprial.kernel import DesiredStateError
        from hyprial.kernel import PersistentConfigError

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

    def _ipc_network_exposures(self) -> Any:
        return {
            "exposures": self._exposure_store.list(),
            "rejected": self._exposure_store.rejected(),
        }

    def _ipc_network_expose(self, params) -> Any:
        port = params.get("port")
        target = params.get("target")
        proxy_protocol = params.get("proxyProtocol", "v2")
        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "port must be between 1 and 65535"
            )
        if port == DEFAULT_PEER_PORT:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"port {port} is reserved for peer forwarding",
            )
        if not isinstance(target, str) or not target:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "target must be a non-empty string"
            )
        if proxy_protocol not in {"v2", "none"}:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "proxyProtocol must be v2 or none",
            )
        try:
            target = validate_exposure_target(target)
        except ForwardingSidecarError as error:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, str(error)
            ) from error
        try:
            exposure = {
                "port": port,
                "target": target,
                "proxyProtocol": proxy_protocol,
            }
            existing = next(
                (
                    item
                    for item in self._exposure_store.list()
                    if item.get("port") == port
                ),
                None,
            )
            if existing is not None and existing != exposure:
                raise ForwardingSidecarError(
                    f"port {port} is already exposed; unexpose it before changing the target"
                )
            self._start_forwarding_supervisor()
            supervisor = self._forwarding_supervisor
            if supervisor is None:
                raise ForwardingSidecarError("forwarding sidecar is not configured")
            applied = supervisor.expose(exposure)
            try:
                self._exposure_store.set(applied)
            except Exception:
                try:
                    supervisor.unexpose(port)
                except Exception:  # noqa: BLE001 - preserve persistence cause
                    pass
                raise
            return {"exposure": applied}
        except ForwardingSidecarError as error:
            code = (
                "SIDECAR_EXPOSE_UNSUPPORTED"
                if str(error).startswith("SIDECAR_EXPOSE_UNSUPPORTED")
                else "FORWARDING_UNAVAILABLE"
            )
            raise DaemonRequestError(code, str(error)) from error

    def _ipc_network_unexpose(self, params) -> Any:
        port = params.get("port")
        if (
            not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "port must be between 1 and 65535"
            )
        self._start_forwarding_supervisor()
        supervisor = self._forwarding_supervisor
        if supervisor is None:
            raise DaemonRequestError(
                "FORWARDING_UNAVAILABLE", "forwarding sidecar is not configured"
            )
        try:
            supervisor.unexpose(port)
            self._exposure_store.remove(port)
        except ForwardingSidecarError as error:
            code = (
                "SIDECAR_EXPOSE_UNSUPPORTED"
                if str(error).startswith("SIDECAR_EXPOSE_UNSUPPORTED")
                else "FORWARDING_UNAVAILABLE"
            )
            raise DaemonRequestError(code, str(error)) from error
        return {"port": port, "removed": True}

    def _ipc_network_peer_key(self, params) -> Any:
        address = params.get("addr")
        if not isinstance(address, str) or not address:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "addr must be an IP:port string"
            )
        try:
            validate_peer_key_address(address)
        except ForwardingSidecarError as error:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, str(error)
            ) from error
        self._start_forwarding_supervisor()
        supervisor = self._forwarding_supervisor
        if supervisor is None:
            raise DaemonRequestError(
                "FORWARDING_UNAVAILABLE", "forwarding sidecar is not configured"
            )
        try:
            return supervisor.peer_key(address)
        except ForwardingSidecarError as error:
            code = (
                "SIDECAR_EXPOSE_UNSUPPORTED"
                if str(error).startswith("SIDECAR_EXPOSE_UNSUPPORTED")
                else "FORWARDING_UNAVAILABLE"
            )
            raise DaemonRequestError(code, str(error)) from error

    def _ipc_autoupdate_status(self) -> Any:
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

    def _ipc_autoupdate_trigger(self) -> Any:
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

    def _ipc_autoupdate_notify(self, params) -> Any:
        return self._deliver_autoupdate_restart_notification(params)

    def _ipc_shutdown(self) -> Any:
        self._request_stop("ipc-shutdown")
        return {
            "ok": True,
            "stopping": True,
            "pid": os.getpid(),
            "epoch": self.epoch,
            "stateDir": str(self.state_dir),
            "lockPath": str(self.state_dir / "daemon.lock"),
        }
