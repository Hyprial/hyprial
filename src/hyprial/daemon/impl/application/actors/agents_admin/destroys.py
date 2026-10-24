"""Agent destroy/stop operations with destroy reservations and runtime stop."""

from __future__ import annotations

from __future__ import annotations
import hashlib
import os
import signal
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING
from uuid import uuid4
from hyprial.daemon.impl.network.routing.session_route_coordinator  import (
    SessionRefRetirement,
    SessionRouteKind,
)
from hyprial.identity import (
    RUNTIME_HEADLESS,
    RUNTIME_INTERACTIVE,
    Agent,
    AgentAlreadyRunning,
    AgentHomeError,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.lifecycle_manager  import (
    LifecycleOperation,
)
from hyprial.daemon.impl.operations.session_ports  import (
    UnregisterSessionCommand,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.application.actors.agents_admin.registry_ops import (
    _AGENT_OPERATION_NAMESPACE,
)
from hyprial.daemon.impl.application.actors.sessions.registry import (
    _session_harness,
)
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
)


class _AgentDestroyMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _reserve_agent_destroy(self, actor: str) -> Any:
        from hyprial.identity import (
            AgentDestroyReservationCompleted,
            ReserveAgentDestroyCommand,
        )

        return self._agent_session_domains.call_agent_settled(
            ReserveAgentDestroyCommand(
                f"{_AGENT_OPERATION_NAMESPACE}:destroy:reserve:{uuid4().hex}", actor
            ),
            AgentDestroyReservationCompleted,
        )

    def _release_agent_destroy_reservation(self, token: str) -> Any:
        from hyprial.identity import (
            AgentDestroyReservationCompleted,
            ReleaseAgentDestroyReservationCommand,
        )

        return self._agent_session_domains.call_agent_settled(
            ReleaseAgentDestroyReservationCommand(
                f"{_AGENT_OPERATION_NAMESPACE}:destroy:release:{uuid4().hex}", token
            ),
            AgentDestroyReservationCompleted,
        )

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

        The same harness objects too, not just a different one: ``hyprial agent start``
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
        self,
        actor: str,
        *,
        keep: tuple[str, str] | None,
        terminate_interactive: bool = False,
    ) -> list[str]:
        """Stop every connector speaking for ``actor`` except ``keep``.

        ``keep`` is the ``(harness, runtime)`` about to take over, so a plain
        restart of the same connector is left alone. ``None`` keeps nothing,
        which is what destroy wants.

        An explicit switch can terminate the fenced interactive carrier through
        ``terminate_interactive``: the daemon signals the verified PID and waits
        for it to exit.  Destroy keeps the non-signalling path: it unregisters
        the route and retires the session ref instead.
        """

        interactive = next(
            (
                item
                for item in self._agent_session_domains.session.read_sessions()
                if item.actor == actor
            ),
            None,
        )
        interactive_applies = interactive is not None and (
            keep is None or keep != (_session_harness(interactive), RUNTIME_INTERACTIVE)
        )
        # Prove the fenced carrier is stopped BEFORE touching any desired-state
        # row: a refusal must leave the actor exactly as the caller found it.
        if interactive_applies and terminate_interactive:
            self._terminate_interactive_carrier(interactive)
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
        if interactive_applies:
            if interactive.session_ref is not None:
                self._call_session_route(
                    SessionRouteKind.UNREGISTER,
                    actor=actor,
                    session_ref=interactive.session_ref,
                    command=UnregisterSessionCommand(
                        correlation_id=f"session:stop-runtime:{uuid4().hex}",
                        actor=actor,
                        session_ref=interactive.session_ref,
                        manage_agent=self.agents.get(actor) is not None,
                    ),
                )
            else:
                self._close_interactive_route(actor, None)
            stopped.append(f"interactive:{actor}")
        return stopped

    def _terminate_interactive_carrier(self, interactive: Any) -> None:
        """Signal the fenced TUI process and wait for it to exit.

        The PID/identity pair is the same birth fence used for liveness; a
        reused PID is never signalled.  A carrier without a fence, or whose
        process state is unknown, is refused; only proven exit is accepted.
        """

        pid = getattr(interactive, "process_pid", None)
        identity = getattr(interactive, "process_identity", None)
        if pid is None or identity is None:
            raise DaemonRequestError(
                ipc_errors.INTERACTIVE_CARRIER_STOP_FAILED,
                "interactive carrier has no PID/birth fence; cannot prove it exited",
                {"actor": interactive.actor},
            )
        from hyprial.daemon.impl.mcp.channel.ownership import _owner_process_status

        status = _owner_process_status(pid, identity)
        if status.value in {"pid-missing", "identity-mismatch"}:
            return
        if status.value != "alive":
            raise DaemonRequestError(
                ipc_errors.INTERACTIVE_CARRIER_STOP_FAILED,
                "interactive carrier state is unknown; cannot prove it exited",
                {"actor": interactive.actor, "pid": pid, "state": status.value},
            )
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _owner_process_status(pid, identity).value in {
                "pid-missing",
                "identity-mismatch",
            }:
                return
            time.sleep(0.05)
        raise DaemonRequestError(
            ipc_errors.INTERACTIVE_CARRIER_STOP_FAILED,
            "interactive carrier did not exit within 5s; refusing the switch",
            {"actor": interactive.actor, "pid": pid},
        )

    def _destroy_agent(
        self,
        name: str,
        *,
        expected_entity_token: str | None = None,
        require_unpinned: bool = False,
        require_offline: bool = False,
    ) -> JsonObject:
        """Delete an agent outright: record, connectors, routes and messages.

        Irreversible by decision A6 -- no tombstone, no revival path. The cost
        is recorded rather than hidden: messages already addressed to this
        agent lose a resolvable recipient.
        """

        agent = self.agents.require(name)
        if expected_entity_token is not None and agent.entity_token != expected_entity_token:
            return {"ok": True, "destroyed": False, "reason": "agent-incarnation-mismatch"}
        actor = agent.uri
        reservation = self._call_session_route(
            SessionRouteKind.RETIRE,
            actor=actor,
            session_ref=None,
            retirement=SessionRefRetirement(
                entity_token=agent.entity_token,
                session_refs=(),
                reason="agent-destroyed",
            ),
        )
        try:
            # A GC scan is only an observation. Recheck its exact incarnation and
            # eligibility after reserving the existing launch/destroy boundary,
            # before stopping a runtime or removing any routes/messages/home.
            current = self.agents.require(agent.actor)
            reason = None
            if current.entity_token != agent.entity_token:
                reason = "agent-incarnation-mismatch"
            elif require_unpinned and current.pinned_adapters:
                reason = "pinned"
            elif require_offline:
                from hyprial.daemon.impl.pac.actors.coordinator import worker_in_use
                from hyprial.daemon.impl.pac.actors.daemon  import DaemonActorRuntime
                observation = DaemonActorRuntime(self).observe(current.actor)
                if worker_in_use(observation):
                    reason = "worker-in-use"
                elif observation.present:
                    reason = "online"
            if reason is not None:
                self.agents.rollback_retired_session_refs(
                    reservation.reservation_token
                )
                self._release_agent_destroy_reservation(reservation.reservation_token)
                return {"ok": True, "destroyed": False, "reason": reason}
            workspace = self._read_workspace_summary_for_destroy(name)
            stopped = self._stop_agent_runtime(actor, keep=None)
            self._drop_persona_route(actor)
            self._release_agent_binding(actor)
            removed_routines = self._remove_agent_routines(agent)
            destroyed_messages = self._destroy_agent_messages(agent)
            # Snapshot the pins for the report; the deletion itself needs no pin
            # code at all -- ON DELETE CASCADE erases them in the same
            # transaction that removes the agent row.
            unpinned = sorted(agent.pinned_adapters)
            removed = self.agents.destroy(
                name, expected_entity_token=agent.entity_token
            )
        except BaseException:
            if self.agents.get(name) is not None:
                try:
                    rolled_back = self.agents.rollback_retired_session_refs(
                        reservation.reservation_token
                    )
                    self._log(
                        "warn",
                        "agents",
                        "agent.session_retirement.rolled_back",
                        actor=actor,
                        changed=rolled_back,
                    )
                except Exception as error:  # noqa: BLE001 - retain original failure
                    self._log(
                        "error",
                        "agents",
                        "agent.session_retirement.rollback_failed",
                        actor=actor,
                        errorType=type(error).__name__,
                        detail=str(error),
                    )
            try:
                self._release_agent_destroy_reservation(
                    reservation.reservation_token
                )
            except Exception as error:  # noqa: BLE001 - retain original failure
                self._log(
                    "error",
                    "agents",
                    "agent.destroy_reservation.release_failed",
                    actor=actor,
                    errorType=type(error).__name__,
                    detail=str(error),
                )
            raise
        if workspace is None:
            workspace_payload: JsonObject = {
                "path": str(Path(self.hyprial_home) / "agents" / name),
                "exists": False,
                "files": 0,
                "bytes": 0,
                "unreadableEntries": 0,
                "deleted": True,
                "absent": True,
            }
        else:
            workspace_payload = {
                **workspace.to_json(),
                # An existing workspace whose directory is gone was deleted as
                # part of the completed transfer-out this destroy is finishing.
                "deleted": workspace.exists and not Path(workspace.path).exists(),
            }
        self._log(
            "warn",
            "agents",
            "agent.destroyed",
            actor=actor,
            stopped=stopped,
            destroyedMessages=destroyed_messages,
            unpinnedAdapters=unpinned,
            removedRoutines=removed_routines,
        )
        return {
            "ok": True,
            "destroyed": removed,
            "actor": actor,
            "agent": name,
            "stopped": stopped,
            "destroyedMessages": destroyed_messages,
            "unpinnedAdapters": unpinned,
            "removedRoutines": removed_routines,
            "irreversible": True,
            "workspace": workspace_payload,
        }

    def _read_workspace_summary_for_destroy(self, name: str) -> Any | None:
        """Inventory the workspace, tolerating a home a transfer-out removed.

        AT11 (``transfer-bundle-complete``) deletes the source home by identity
        fence and leaves the registry row for this operation to clear.  Reading
        the summary *before* deleting the row is what broke that handover: the
        home was already gone, the fence raised, and the row -- plus its routes,
        bindings and undelivered messages -- could never be removed, so the
        source kept advertising a delivery target nobody could answer.

        Only the two "there is no home to inventory" shapes are tolerated, and
        only for this read: no home was ever provisioned, or the home root is
        gone.  Every other fence failure (symlink, wrong owner, wrong mode,
        receipt mismatch) still refuses, and the deletion, route release and
        message drain below still run under the destroy reservation.
        """

        try:
            return self._agent_session_domains.agent.read_workspace_summary(name)
        except AgentHomeError as error:
            home = Path(self.hyprial_home) / "agents" / name
            if not self._home_is_absent(error, home):
                raise
            self._log(
                "warn",
                "agents",
                "agent.destroy.workspace_absent",
                actor=name,
                phase=error.phase,
                category=error.category,
            )
            return None

    @staticmethod
    def _home_is_absent(error: AgentHomeError, home: Path) -> bool:
        """True only when the fence failure *is* the home being gone."""

        if error.category == "not-provisioned" and error.phase == "read-registry":
            return True
        if error.category != "io":
            # unsafe-path / wrong-owner / unsafe-mode / receipt-mismatch keep
            # refusing: those are live homes this process must not touch.
            return False
        if error.phase not in {
            "validate-home-root",
            "validate-agents-root",
            "validate-agent-root",
        }:
            return False
        # ``safe_directory`` reports every ``lstat`` failure as "io", so the
        # absence has to be confirmed here: only an explicit ENOENT means the
        # home is gone.  EACCES or ENOTDIR still raise -- answering False hands
        # the caller back its classified fence failure, so an unreadable path
        # can never be read as "the home is absent".
        try:
            os.lstat(home)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False


    def _remove_agent_routines(self, agent: Agent) -> list[str]:
        routines = self._routines_bound_to(agent.uri)
        if not routines:
            return []
        coordinator = self._routine_coordinator
        if coordinator is None:
            raise DaemonRequestError(
                ipc_errors.ROUTINE_UNAVAILABLE,
                "routine coordinator is not running",
            )
        removed: list[str] = []
        for routine in routines:
            name = str(routine["name"])
            operation_id = (
                f"agent-routine-remove:{agent.entity_token}:"
                f"{hashlib.sha256(name.encode()).hexdigest()[:16]}"
            )
            admission = coordinator.begin_remove(
                operation_id=operation_id,
                name=name,
                enforce_last=False,
            )
            if admission is not PortAdmission.ACCEPTED:
                raise DaemonRequestError(
                    ipc_errors.ROUTINE_UNAVAILABLE,
                    f"routine removal admission {admission.value}",
                )
            coordinator.wait(operation_id, timeout=70.0)
            removed.append(name)
        return removed

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
