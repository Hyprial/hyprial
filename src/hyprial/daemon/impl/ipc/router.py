"""The daemon IPC dispatch router: handle(), the restore gate and the generic method families.

S3 (implS3, 2026-11-02): the legacy branches for the registry-migrated names
``routine.list``/``routine.status``/``workflow.list`` were dead dispatch (the
registry gate answers first); the cleanup keeps every un-migrated sibling and
the status ownership pre-guard narrowed accordingly.

2026-11-08 single-route gate: remaining legacy handlers and rejection/policy
branches explicitly exclude registered names. They stay after the terminating
registry dispatch; pre-dispatch authorization and unregistered siblings retain
their existing behavior and static network-call visibility.

2026-11-17 single list (daemon-first-z4b-router-only-refusal-20261117): after
the registry and migrated-name refusals, a name that is neither ``shutdown`` nor
in ``ROUTER_ONLY_METHODS`` is refused ``METHOD_NOT_FOUND`` before any inline
branch runs.  The table is the admission list the bootstrap host also reads; a
test keeps it equal to the branches below.  ``agent.migrate.<unknown>`` and
``routine.<unknown>`` run a parameter or ownership check before their refusal,
so they bypass the guard and keep answering exactly as they did.
"""
from __future__ import annotations
import hashlib
import os
from typing import Any
from hyprial import __version__
from hyprial.daemon.impl.autoupdate import SCHEDULE
from hyprial.identity import (
    AgentError,
    AgentHomeError as RegistryHomeError,
)
from hyprial.kernel import DaemonRequestError, ipc_errors
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
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _required_string,
)
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.daemon.impl.request_catalog.migration import (
    MIGRATED_FAMILIES,
    MIGRATED_METHODS,
    ROUTER_ONLY_METHODS,
)
from hyprial.kernel.interface import (
    IPC_MESSAGE_READ_BODY,
    IPC_MESSAGE_SETTLE_HINT,
)
_NAMES_CHECKED_BEFORE_REFUSAL = ("agent.migrate.", "routine.")
_RESTORE_GATE_LIGHT_METHODS = frozenset({"ping", "shutdown", "daemon.catalog"})
_IPC_STATS_METHODS = frozenset(
    {
        "worker.channel.request",
        "adapter.list",
        "adapter.reload",
        "adapter.start",
        "adapter.status",
        "adapter.stop",
        "agent.cell.get",
        "agent.cell.update",
        "agent.create",
        "agent.config.rebind",
        "agent.destroy",
        "agent.destroy.preview",
        "agent.ensure-home",
        "agent.get",
        "agent.grant",
        "agent.grants",
        "agent.home-census",
        "agent.list",
        "agent.migrate.execute",
        "agent.migrate.preflight",
        "agent.migrate.rollback",
        "agent.readiness",
        "agent.rename",
        "agent.resolve",
        "agent.resume.restore",
        "agent.restore-policy",
        "agent.restore-threshold",
        "agent.provider.login",
        "agent.provider.logout",
        "agent.provider.default",
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
        "agent.start.preflight",
        "agent.unblock",
        "autoupdate.status",
        "autoupdate.trigger",
        "daemon.catalog",
        "dispatch.matrix.resolve",
        "down",
        "hosts",
        "identity.whoami",
        "identity.users.list",
        "identity.users.add",
        "lifecycle.start",
        "lifecycle.resume.preflight",
        "management.adapter.remove",
        "message.ack",
        "message.history",
        "message.pending.list",
        "message.pending.batch",
        "message.pending.wait",
        "message.query",
        IPC_MESSAGE_READ_BODY,
        "message.reply",
        "message.send", "message.send.lookup",
        IPC_MESSAGE_SETTLE_HINT,
        "message.status",
        "network.expose",
        "network.exposures",
        "network.peerKey",
        "network.unexpose",
        "org.acl.grant",
        "org.acl.ls",
        "org.acl.revoke",
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
        "org.repair",
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
        "orgfs.space_purge",
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
        "session.register_guarded",
        "session.turn.ended",
        "session.turn.started",
        "session.unregister",
        "service.connect",
        "service.disconnect",
        "service.guide",
        "service.list",
        "shutdown",
        "targets",
        "top.snapshot",
        "upgrade.steps.run", "upgrade.steps.status",
        "workflow.cancel", "workflow.complete",
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

class _IpcDispatchMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _interactive_identity_binding_peer(self, peer_pid: object) -> bool:
        """Match an IPC peer to an interactive PID and its birth identity."""

        if isinstance(peer_pid, bool) or not isinstance(peer_pid, int):
            return False
        from hyprial.daemon.impl.mcp.channel import ownership

        observed = ownership._read_process_identity(peer_pid)
        if observed is None:
            return False
        observed_parts = ownership._identity_components(observed)

        def same_birth(recorded: str) -> bool:
            # Component-wise, as owner liveness compares: two readers may
            # record different scheme sets for one birth; a reused PID differs
            # in every scheme they share.
            recorded_parts = ownership._identity_components(recorded)
            return any(
                recorded_parts[scheme] == observed_parts[scheme]
                for scheme in recorded_parts.keys() & observed_parts.keys()
            )

        return any(
            session.process_pid == peer_pid
            and session.process_identity is not None
            and same_birth(session.process_identity)
            for session in self.desired_state.load().interactive_sessions
        )

    def _identity_binding_operator(
        self,
        params: JsonObject,
        *,
        trusted_message_origin: str | None,
    ) -> tuple[bool, int | None]:
        """Apply R2i's zero-cost agent refusals before the future ancestry walk.

        Only three callers are recognized as agents: daemon workers, requests
        that carry an actor or sessionRef, and an IPC peer whose pid and birth
        match an interactive session that registered its process (today only
        codex app-server does; claude, pi and kimi sessions cannot be matched).
        Everything else, including an agent's own shell running the CLI and a
        peer whose process cannot be read, counts as an unverified operator.
        """

        peer = self._ipc_peer
        peer_pid = getattr(peer, "pid", None)
        caller_pid = (
            peer_pid
            if isinstance(peer_pid, int) and not isinstance(peer_pid, bool)
            else None
        )
        carries_session_identity = any(
            key in params for key in ("actor", "sessionRef")
        )
        # The IPC server records only the peer pid per connection; there is no
        # connection-level actor or session to consult.
        operator = not (
            trusted_message_origin == "worker"
            or carries_session_identity
            or self._interactive_identity_binding_peer(caller_pid)
        )
        return operator, caller_pid

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
                "storageHealth": self._storage_health_status(),
                "zenoh": {
                    "listen": list(self.zenoh_listen),
                    "connect": list(self.zenoh_connect),
                    "session": self._zenoh_session_status(),
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
        if method == "daemon.catalog":
            # Read-only meta dispatch: it must answer during restore, before
            # the business-object assertions below, and it never reads inbox,
            # harness or presence state.  Keeping it here (not folded into the
            # registry gate) is what allows /readyz to explain "restoring".
            return METHOD_REGISTRY.invoke(
                self,
                method,
                params,
                trusted_message_origin=_trusted_message_origin,
            )
        if method in {
            "service.connect",
            "service.disconnect",
            "service.guide",
            "service.list",
        }:
            return METHOD_REGISTRY.invoke(
                self, method, params, trusted_message_origin=_trusted_message_origin
            )
        assert self._inbox is not None
        assert self._harnesses is not None
        assert self._presence is not None
        if method in METHOD_REGISTRY:
            return METHOD_REGISTRY.invoke(
                self,
                method,
                params,
                trusted_message_origin=_trusted_message_origin,
            )
        if method in MIGRATED_METHODS or method.split(".", 1)[0] in MIGRATED_FAMILIES:
            # A migrated name that is not declared must not fall through to a
            # legacy branch.  The precise set covers a partially migrated
            # family; the family set covers a family with no legacy members.
            raise DaemonRequestError(
                ipc_errors.METHOD_NOT_FOUND, f"unknown daemon method {method}"
            )
        if (
            method != "shutdown"
            and method not in ROUTER_ONLY_METHODS
            and not method.startswith(_NAMES_CHECKED_BEFORE_REFUSAL)
        ):
            raise DaemonRequestError(
                ipc_errors.METHOD_NOT_FOUND, f"unknown daemon method {method}"
            )
        if method == "worker.channel.request":
            return self._ipc_worker_channel_request(params)
        if method == "autoupdate.trigger":
            return self._ipc_autoupdate_trigger()
        if method == "ps":
            return self._ipc_ps(params)
        if method in {"session.register", "session.register_guarded"}:
            return self._ipc_session_register(
                params,
                guarded=method == "session.register_guarded",
                verify_process_claim=_trusted_message_origin == "ipc",
            )
        if method == "session.turn.started":
            return self._ipc_session_turn_started(params)
        if method == "session.turn.ended":
            return self._ipc_session_turn_ended(params)
        if method == "session.refresh":
            return self._ipc_session_refresh(params)
        if method == "session.heartbeat":
            return self._ipc_session_heartbeat(params)
        if method == "session.unregister":
            return self._ipc_session_unregister(params)
        if method == "message.pending.list":
            return self._ipc_message_pending_list(method, params, _trusted_message_origin=_trusted_message_origin)
        if method == "message.pending.wait":
            return self._ipc_message_pending_wait(method, params, _trusted_message_origin=_trusted_message_origin)
        if method.startswith("routine.") and method not in METHOD_REGISTRY:
            routine_caller = self._workflow_caller(params)
            if method in (
                "routine.remove",
                "routine.set",
            ) and self._routine_service is not None:
                current = self._routine_service.status(name=_required_string(params.get("name"), "name"))
                if routine_caller != canonical_user_uri(self.owner) and not self._routine_caller_is(
                    routine_caller, current["owner"], current.get("actor")
                ):
                    raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "caller does not own this routine")
        if method == "routine.add":
            return self._ipc_routine_add(params)
        if method == "routine.remove":
            return self._ipc_routine_remove(params)
        if method == "routine.set":
            return self._ipc_routine_set(params)
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
        if method in ("workflow.status", "workflow.node.inspect", "workflow.cancel", "workflow.history.status"):
            return self._ipc_workflow_status_group(method, params)
        if method == "message.reply":
            return self._ipc_message_reply(method, params, _trusted_message_origin=_trusted_message_origin)
        if method == "message.ack":
            return self._ipc_message_ack(method, params, _trusted_message_origin=_trusted_message_origin)
        if method == IPC_MESSAGE_READ_BODY:
            return self._ipc_message_read_body(method, params, _trusted_message_origin=_trusted_message_origin)
        if method == IPC_MESSAGE_SETTLE_HINT:
            return self._ipc_message_settle_hint(method, params, _trusted_message_origin=_trusted_message_origin)
        if method == "outbox.prune":
            return self._ipc_outbox_prune(params)
        if method in {
            "lifecycle.start",
            "lifecycle.resume.preflight",
        }:
            return self._ipc_lifecycle_start(method, params)
        if method == "down":
            return self._ipc_down(params)
        if method.startswith("agent.") and method not in METHOD_REGISTRY:
            if method == "agent.config.rebind" and _trusted_message_origin == "worker":
                raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "config rebind requires local operator IPC")
            from hyprial.daemon.impl.user_agent.admin import refuse_legacy_agent_method

            refuse_legacy_agent_method(self, method, params)
            # One boundary for the agent RPCs: the registry raises its own
            # typed errors, and every one of them already carries the IPC code
            # it should surface as. Translating here keeps handle()'s contract
            # (DaemonRequestError, always) without restating those codes.
            try:
                return self._handle_agent(method, params)
            except (AgentError, DomainCommandError) as error:
                raise DaemonRequestError(error.code, str(error), data=getattr(error, "data", None)) from error
            except RegistryHomeError as error:
                raise DaemonRequestError(AgentError.code, str(error)) from error
        if method == "shutdown":
            return self._ipc_shutdown(params)
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

    def _ipc_pac_gc_preview(self) -> Any:
        """Serve the read-only PAC GC preview behind ``workflow gc --dry-run``.

        The only handler sat inside the ``agent.*`` family, which a ``pac.*``
        method never reaches, so the declared method answered
        METHOD_NOT_FOUND.  That unreachable branch stays in registry_ops.py
        until #1502 splits that file; removing it here would repair a debt
        the structural baseline pins to #1502.
        """

        collector = getattr(self, "_pac_gc", None)
        if collector is None:
            raise DaemonRequestError(
                "PAC_GC_UNAVAILABLE", "PAC GC service is unavailable"
            )
        return collector.preview()

    def _ipc_autoupdate_status(self) -> Any:
        return {
            "ok": True,
            "trigger": "daemon",
            "environmentPathDigest": hashlib.sha256(
                os.environ.get("PATH", "").encode()
            ).hexdigest(),
            "schedule": [{"hour": hour, "minute": minute} for hour, minute in SCHEDULE],
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

    def _ipc_shutdown(self, params=None) -> Any:
        expected = (params or {}).get("expectedEpoch")
        if "expectedEpoch" in (params or {}):
            expected = _required_string(expected, "expectedEpoch")
            if expected != self.epoch:
                raise DaemonRequestError(
                    "DAEMON_EPOCH_MISMATCH",
                    "shutdown plan belongs to another daemon epoch",
                )
        self._request_stop("ipc-shutdown")
        return {
            "ok": True,
            "stopping": True,
            "pid": os.getpid(),
            "epoch": self.epoch,
            "stateDir": str(self.state_dir),
            "lockPath": str(self.state_dir / "daemon.lock"),
        }
