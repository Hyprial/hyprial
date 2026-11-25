"""The member-keyed dispatch/steer gate of the harness delivery behaviour.

Decision record (cell model I5-2, second review round 2026-11-20): this
module is the split-out admission half of ``behaviors/delivery`` — the file
grew past the thousand-line budget when the typed envelope gate landed.
The commands carry no worker name; the record is selected by resolving the
stamp's member through the agent identity projection, and a member that
resolves nowhere is used as-is only when it is already a bare local name
form — a URI-shaped member the projection cannot name is an explicit
``accepted=False`` refusal.  The dispatch entry refuses steer envelopes and
the steer entry refuses turn envelopes; both refuse a stamp whose source is
not headless.  Epoch, revision and cell id stay diagnostic and are never
read here.  The origin reservation is taken over the stamp's origin
message ids before the process enqueue, and the process itself still
receives the immutable input, never the envelope.  ``steer`` is an
OPTIONAL process capability — deliberately not part of the
StreamingHarnessProcess runtime-checkable protocol, so a runtime without
it is still a valid streaming process — and a missing capability is an
explicit not-accepted answer, never a silent no-op.
"""

from __future__ import annotations

from typing import cast

from hyprial.daemon.impl.api import StreamingHarnessProcess
from hyprial.daemon.impl.cells.actor.contracts import (
    CellDispatchEnvelope,
    DISPATCH_STEER,
    DISPATCH_TURN,
    WORK_SOURCE_HEADLESS,
)
from hyprial.daemon.impl.harnesses.runtime.ports import (
    DispatchHarnessDeliveryCommand,
    SteerHarnessDeliveryCommand,
    HarnessDeliveryAdmitted,
    HarnessDeliverySteered,
)
from hyprial.kernel import parse_agent_uri

from ...contracts import _DeliveryReservation


class _DeliveryGateMixin:
    """The envelope admission and steer gate of the delivery behaviour."""

    def _member_record_name(self, member: str) -> str:
        resolve = getattr(self._agent_identity, "actor_name", None)
        if callable(resolve):
            resolved = resolve(member)
            if resolved is not None:
                return resolved
        if parse_agent_uri(member) is None:
            return member
        return ""

    def _on_dispatch(self, command: DispatchHarnessDeliveryCommand) -> None:
        envelope = command.envelope
        if not isinstance(envelope, CellDispatchEnvelope):
            self._emit_event(
                HarnessDeliveryAdmitted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    "",
                    "",
                    False,
                )
            )
            return
        if (
            envelope.dispatch_kind != DISPATCH_TURN
            or envelope.stamp.source != WORK_SOURCE_HEADLESS
        ):
            self._emit_event(
                HarnessDeliveryAdmitted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    "",
                    envelope.delivery_id,
                    False,
                )
            )
            return
        members = tuple(envelope.stamp.origin_message_ids)
        if any(member in self._delivery_reservations for member in members):
            self._emit_event(
                HarnessDeliveryAdmitted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    "",
                    envelope.delivery_id,
                    False,
                )
            )
            return
        record_name = self._member_record_name(envelope.stamp.member)
        selected = next(
            (
                (key, record)
                for key, record in self._records.items()
                if record.spec.name == record_name
                and isinstance(record.process, StreamingHarnessProcess)
                and record.process.running
                and bool(getattr(record.process, "dispatch_ready", False))
            ),
            None,
        )
        if selected is None or selected[1].process is None:
            self._emit_event(
                HarnessDeliveryAdmitted(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    "",
                    envelope.delivery_id,
                    False,
                )
            )
            return
        selected_key, selected_record = selected
        process = cast(StreamingHarnessProcess, selected_record.process)
        reservation = _DeliveryReservation(
            selected_key, selected_record.generation, None, members
        )
        for member in members:
            self._delivery_reservations[member] = reservation
        self._result_reservation_snapshot = frozenset(self._delivery_reservations)
        correlation = self._register_call(
            command.correlation_id,
            selected_record.generation,
            operation="dispatch",
            harness_id=selected_key,
            delivery_id=envelope.delivery_id,
        )
        assert self._io is not None
        self._io.call(
            correlation,
            selected_record.generation,
            self._version,
            lambda: process.enqueue(envelope.input),
        )


    def _on_steer(self, command: SteerHarnessDeliveryCommand) -> None:
        """Route one steer to the live process that owns the queued delivery.

        Selection mirrors ``_on_dispatch``: a steer is only meaningful for a
        running streaming process with a turn in flight.  No process found is
        an explicit ``accepted=False`` (never a silent no-op), because the
        daemon's timeout path reads that as "steer failed" and raises the
        in-channel notice.
        """

        envelope = command.envelope
        if not isinstance(envelope, CellDispatchEnvelope):
            self._emit_event(
                HarnessDeliverySteered(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    "",
                    "",
                    False,
                )
            )
            return
        if (
            envelope.dispatch_kind != DISPATCH_STEER
            or envelope.stamp.source != WORK_SOURCE_HEADLESS
        ):
            self._emit_event(
                HarnessDeliverySteered(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    "",
                    envelope.stamp.origin_message_ids[0],
                    False,
                )
            )
            return
        delivery_id = envelope.stamp.origin_message_ids[0]
        record_name = self._member_record_name(envelope.stamp.member)
        selected = next(
            (
                (key, record)
                for key, record in self._records.items()
                if record.spec.name == record_name
                and isinstance(record.process, StreamingHarnessProcess)
                and record.process.running
            ),
            None,
        )
        if selected is None or selected[1].process is None:
            self._emit_event(
                HarnessDeliverySteered(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    "",
                    delivery_id,
                    False,
                )
            )
            return
        selected_key, selected_record = selected
        process = cast(StreamingHarnessProcess, selected_record.process)
        steer = getattr(process, "steer", None)
        if not callable(steer):
            self._emit_event(
                HarnessDeliverySteered(
                    command.correlation_id,
                    self._handler_generation,
                    self._version,
                    selected_key,
                    delivery_id,
                    False,
                )
            )
            return
        correlation = self._register_call(
            command.correlation_id,
            selected_record.generation,
            operation="steer",
            harness_id=selected_key,
            delivery_id=delivery_id,
        )
        assert self._io is not None
        self._io.call(
            correlation,
            selected_record.generation,
            self._version,
            lambda: steer(delivery_id),
        )
