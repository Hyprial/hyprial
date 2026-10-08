"""Agent destroy/stop operations with destroy reservations and runtime stop."""

from __future__ import annotations

from __future__ import annotations
import hashlib
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
        its route closed instead. Destroy retires that session ref before this
        method runs, so the losing side gets SESSION_SUPERSEDED on its next
        fenced call or registration attempt and goes quiet permanently.
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
                from hyprial.daemon.impl.pac.actors.daemon  import DaemonActorRuntime
                if DaemonActorRuntime(self).observe(current.actor).present:
                    reason = "online"
            if reason is not None:
                self.agents.rollback_retired_session_refs(
                    reservation.reservation_token
                )
                self._release_agent_destroy_reservation(reservation.reservation_token)
                return {"ok": True, "destroyed": False, "reason": reason}
            workspace = self._agent_session_domains.agent.read_workspace_summary(name)
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
        workspace_deleted = workspace.exists and not Path(workspace.path).exists()
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
            "workspace": {
                **workspace.to_json(),
                "deleted": workspace_deleted,
            },
        }

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
