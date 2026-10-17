"""Derived transfer holds owned by the same mailbox as start admission.

No durable authority lives here. The P2 journal reconstructs these holds. A
launcher timeout does not finish its I/O: retain the correlation until the
process is owned by the runtime or its late cleanup positively completes.
"""

from dataclasses import dataclass, field

from hyprial.kernel import ipc_errors, agent_uri_actor

from hyprial.daemon.impl.harnesses.runtime.transfer_ports import (
    HoldStartsForTransfer,
    ReleaseStartsHold,
    StartsHeld,
    StartsHoldReleased,
)


@dataclass
class _StartHold:
    command: HoldStartsForTransfer
    waiters: set[str] = field(default_factory=set)
    drained: set[str] = field(default_factory=set)


def _hold_key(actor: str) -> str:
    # Harness specs carry local names while lifecycle/P2 callers carry URIs.
    # This key is only an index: identity admission still resolves the original
    # actor and checks its incarnation; parsing alone grants no authority.
    return agent_uri_actor(actor) or actor


class _TransferBehavior:
    def _automatic_custody_clear(self, actor: str) -> bool:
        # Optional for standalone legacy runtimes. Daemon composition supplies
        # a pure projection read; never call SQLite or wait in this mailbox.
        if self._source_custody_clear is None:
            return True
        current = self._transfer_entity(actor)
        if current is None:
            return False
        try:
            return self._source_custody_clear(actor, current) is True
        except Exception:
            # Unlike activity policy, unavailable custody cannot fail open.
            return False

    def _lifecycle_entity_error(self, request) -> tuple[str, str] | None:
        expected = request.expected_entity_token
        if expected is None:
            return None
        if self._agent_identity is None:
            return (ipc_errors.LIFECYCLE_INCARNATION_UNSUPPORTED,
                    "Harness owner has no incarnation projection port")
        payload = request.payload
        spec = getattr(payload, "spec", None)
        actor = getattr(spec, "name", None) if spec is not None else getattr(payload, "name", None)
        if not isinstance(actor, str) or not actor:
            return (ipc_errors.INVALID_ARGUMENT, "Harness lifecycle payload has no actor")
        current = self._transfer_entity(actor)
        if current is None:
            return (ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
                    "Harness owner cannot resolve incarnation")
        if current != expected:
            return (ipc_errors.AGENT_INCARNATION_MISMATCH,
                    "Harness lifecycle incarnation changed")
        return None

    def _on_release_starts(self, command: ReleaseStartsHold) -> None:
        # Authenticated override, durable audit/alert and needs-attention are
        # caller prerequisites. This port only releases the exact derived hold;
        # it neither grants authorization nor deletes durable P2 custody.
        if (not all(isinstance(value, str) and value.strip() for value in (
                command.actor, command.expected_entity_token,
                command.operation_id, command.correlation_id))
                or type(command.generation) is not int
                or command.generation < 1):
            self._reject(command, ipc_errors.INVALID_ARGUMENT, "invalid transfer hold release")
            return
        key = _hold_key(command.actor)
        hold = self._transfer_holds.get(key)
        if hold is None or (
            hold.command.actor != command.actor
            or hold.command.expected_entity_token != command.expected_entity_token
            or hold.command.operation_id != command.operation_id
            or hold.command.generation != command.generation
        ):
            self._reject(command, ipc_errors.TRANSFER_CONFLICT, "transfer hold does not match")
            return
        current = self._transfer_entity(command.actor)
        if current is None:
            self._reject(command, ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE,
                         "cannot resolve transfer hold incarnation")
            return
        if current != command.expected_entity_token:
            self._reject(command, ipc_errors.TRANSFER_CONFLICT, "transfer incarnation changed")
            return
        if hold.waiters or key in self._transfer_inflight.values():
            self._reject(command, ipc_errors.TRANSFER_CONFLICT, "transfer hold drain pending")
            return
        del self._transfer_holds[key]
        self._emit_event(StartsHoldReleased(
            command.correlation_id, command.actor, command.expected_entity_token,
            command.operation_id, command.generation,
            version=self._transfer_read_version(),
        ))

    def _init_transfer_holds(self, mailbox_capacity: int) -> None:
        self._transfer_holds: dict[str, _StartHold] = {}
        self._transfer_waiter_capacity = mailbox_capacity
        self._transfer_inflight: dict[str, str] = {}
        self._transfer_stops: dict[str, str] = {}

    def _transfer_entity(self, actor: str) -> str | None:
        if self._agent_identity is None:
            return None
        try:
            return self._agent_identity.entity_token(actor)
        except Exception:
            return None

    def _transfer_read_version(self) -> int:
        # The owner mailbox bumps its projection version once per delivered
        # command, so an acknowledgement reported from a handler already
        # carries the version of that command. Reading it here keeps the two
        # transfer events fenced like every other HarnessEvent.
        with self._generation_lock:
            return self._version

    def _transfer_start_error(self, actor: str) -> str | None:
        key = _hold_key(actor)
        hold = self._transfer_holds.get(key)
        if hold is None:
            return None
        current = self._transfer_entity(actor)
        if current is None:
            return ipc_errors.TRANSFER_CUSTODY_UNAVAILABLE
        if current != hold.command.expected_entity_token:
            # A positive observation retires only the old incarnation's hold.
            self._finish_transfer_hold(hold, "entity-mismatch")
            self._transfer_holds.pop(key)
            return None
        return ipc_errors.TRANSFER_STARTS_HELD

    def _on_hold_starts(self, command: HoldStartsForTransfer) -> None:
        if (not all(isinstance(value, str) and value for value in (
                command.actor, command.expected_entity_token,
                command.operation_id, command.correlation_id))
                or type(command.generation) is not int
                or command.generation < 1):
            self._reject(command, ipc_errors.INVALID_ARGUMENT, "invalid transfer start hold")
            return
        current = self._transfer_entity(command.actor)
        if current is None or current != command.expected_entity_token:
            self._emit_event(StartsHeld(
                command.correlation_id, command.actor, command.expected_entity_token,
                command.operation_id, command.generation,
                "unavailable" if current is None else "entity-mismatch",
                version=self._transfer_read_version(),
            ))
            return
        key = _hold_key(command.actor)
        hold = self._transfer_holds.get(key)
        if hold is not None and hold.command.expected_entity_token != current:
            self._finish_transfer_hold(hold, "entity-mismatch")
            self._transfer_holds.pop(key)
            hold = None
        if hold is not None and (
            hold.command.actor != command.actor
            or hold.command.operation_id != command.operation_id
            or hold.command.generation != command.generation
        ):
            self._reject(command, ipc_errors.TRANSFER_CONFLICT, "another transfer owns starts")
            return
        if hold is None:
            if len(self._transfer_holds) >= self._restore_eligibility_capacity:
                self._reject(command, "PORT_OVERLOADED", "transfer hold capacity")
                return
            hold = _StartHold(command)
            self._transfer_holds[key] = hold
        if (command.correlation_id not in hold.waiters
                and sum(len(h.waiters) for h in self._transfer_holds.values())
                >= self._transfer_waiter_capacity):
            self._reject(command, "PORT_OVERLOADED", "transfer hold waiter capacity")
            return
        hold.waiters.add(command.correlation_id)
        # Automatic starts normally harvest on a maintenance tick. Finish an
        # already received result now so the ack cannot precede process custody.
        for record in tuple(self._records.values()):
            if _hold_key(record.spec.name) == key and record.completed_start is not None:
                completed = record.completed_start
                record.completed_start = None
                self._on_start_completed(completed)
                self._transfer_start_completed(completed)
        self._maybe_finish_transfer_holds()

    def _finish_transfer_hold(self, hold: _StartHold, disposition: str) -> None:
        command = hold.command
        waiters, hold.waiters = tuple(hold.waiters), set()
        for correlation in waiters:
            self._emit_event(StartsHeld(
                correlation, command.actor, command.expected_entity_token,
                command.operation_id, command.generation, disposition,
                tuple(sorted(hold.drained)),
                version=self._transfer_read_version(),
            ))

    def _maybe_finish_transfer_holds(self) -> None:
        for actor, hold in tuple(self._transfer_holds.items()):
            if actor in self._transfer_inflight.values():
                continue
            current = self._transfer_entity(hold.command.actor)
            disposition = (
                "unavailable" if current is None else
                "held" if current == hold.command.expected_entity_token else
                "entity-mismatch"
            )
            self._finish_transfer_hold(hold, disposition)

    def _transfer_finish_start(self, correlation: str) -> None:
        actor = self._transfer_inflight.pop(correlation, None)
        hold = self._transfer_holds.get(actor)
        if hold is not None:
            hold.drained.add(correlation)
        self._maybe_finish_transfer_holds()

    def _transfer_start_completed(self, event) -> None:
        if event.correlation_id in self._transfer_stops.values():
            return
        record = self._records.get(event.harness_id)
        if record is not None and record.completed_start is event:
            return
        self._transfer_finish_start(event.correlation_id)

    def _transfer_stop_completed(self, event) -> None:
        # A failed stop is not proof of drain. Retain custody; callers observe
        # their deadline as unknown. Never manufacture a successful hold ack.
        if event.stopped:
            start = self._transfer_stops.pop(event.correlation_id, None)
            if start is not None:
                self._transfer_finish_start(start)
