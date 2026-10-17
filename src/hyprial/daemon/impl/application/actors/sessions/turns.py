"""Interactive carrier turn reports (WP5-A §4).

Every interactive carrier — whatever its runtime — reports the boundaries of
a model turn through two IPC methods.  The daemon stamps the clock, fences the
report against the current interactive session, deduplicates by
``(actor, sessionRef, reportId)`` and publishes ``turn.*`` through the one
``DaemonHookService`` publish path.  The carrier's own ``seq``, ``source`` and
timestamps are never read.

This module deliberately does not import the carrier contract: the two IPC
method names are pinned to it by the router test, not by a shared import.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from hyprial.kernel import DaemonRequestError, ipc_errors

from hyprial.daemon.impl.ipc.params import JsonObject, _required_string
from hyprial.daemon.impl.operations.top import InteractiveTurnStats

__all__ = ["CarrierTurnLedger", "_SessionTurnsMixin"]

#: One actor's started/ended report ids retained per session, LRU-bounded.
_TURN_LEDGER_CAPACITY = 1024
_TURN_END_STATUSES = frozenset({"completed", "failed"})
_SESSION_TURN_STARTED = "session.turn.started"
_SESSION_TURN_ENDED = "session.turn.ended"


@dataclass
class CarrierTurnLedger:
    """Daemon-local report receipts for one ``(actor, sessionRef)`` pair."""

    started: "OrderedDict[str, int]" = field(default_factory=OrderedDict)
    ended: "OrderedDict[str, int]" = field(default_factory=OrderedDict)


def _trim(ledger: "OrderedDict[str, int]") -> None:
    while len(ledger) > _TURN_LEDGER_CAPACITY:
        ledger.popitem(last=False)


class _SessionTurnsMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _ipc_session_turn_started(self, params: JsonObject) -> Any:
        actor = self._fence_interactive_session(self._mcp_actor(params), params)
        session_ref = _required_string(params.get("sessionRef"), "sessionRef")
        report_id = _required_string(params.get("reportId"), "reportId")
        if len(report_id) > 128:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "reportId must be at most 128 characters",
            )
        delivery_id = (
            params["deliveryId"]
            if isinstance(params.get("deliveryId"), str)
            else ""
        )
        self._require_current_interactive_session(actor, session_ref)
        at_ms, is_new = self._record_carrier_turn_started(
            actor, session_ref, report_id
        )
        if is_new:
            if self._daemon_hooks is not None:
                self._daemon_hooks.observe_carrier_turn(
                    actor=actor,
                    phase="started",
                    at_ms=at_ms,
                    started_at_ms=None,
                    delivery_id=delivery_id,
                )
        return {"ok": True, "startedAtMs": at_ms}

    def _ipc_session_turn_ended(self, params: JsonObject) -> Any:
        actor = self._fence_interactive_session(self._mcp_actor(params), params)
        session_ref = _required_string(params.get("sessionRef"), "sessionRef")
        report_ids = params.get("reportIds")
        if (
            not isinstance(report_ids, list)
            or not report_ids
            or len(report_ids) > 1000
            or any(
                not isinstance(report_id, str)
                or not report_id
                or len(report_id) > 128
                for report_id in report_ids
            )
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "reportIds must be an array of 1-1000 non-empty strings up to 128 characters",
            )
        status = params.get("status", "completed")
        if status not in _TURN_END_STATUSES:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "status must be completed or failed",
            )
        delivery_id = (
            params["deliveryId"]
            if isinstance(params.get("deliveryId"), str)
            else ""
        )
        self._require_current_interactive_session(actor, session_ref)
        at_ms = self._clock_ms()
        stats = self._record_interactive_turn_reports(
            actor, session_ref, report_ids, at_ms=at_ms
        )
        phase = "failed" if status == "failed" else "completed"
        first_ended: int | None = None
        for report_id in report_ids:
            ended_at_ms, started_at_ms, is_new = self._record_carrier_turn_ended(
                actor, session_ref, report_id, at_ms
            )
            if first_ended is None:
                first_ended = ended_at_ms
            if is_new and self._daemon_hooks is not None:
                self._daemon_hooks.observe_carrier_turn(
                    actor=actor,
                    phase=phase,
                    at_ms=ended_at_ms,
                    started_at_ms=started_at_ms,
                    delivery_id=delivery_id,
                )
        return {
            "ok": True,
            "turnCount": stats.turn_count,
            "turnCountSinceMs": stats.counting_since_ms,
            "lastTurnEndedAtMs": stats.last_turn_ended_at_ms,
            "endedAtMs": first_ended,
        }

    def _require_current_interactive_session(
        self, actor: str, session_ref: str
    ) -> None:
        session = next(
            (
                item
                for item in self._agent_session_domains.session.read_sessions()
                if item.actor == actor and item.session_ref == session_ref
            ),
            None,
        )
        if session is None:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "turn reports require the actor's current interactive session",
            )

    def _carrier_turn_ledger(
        self, actor: str, session_ref: str
    ) -> CarrierTurnLedger:
        key = (actor, session_ref)
        ledger = self._carrier_turn_ledgers.get(key)
        if ledger is None:
            ledger = CarrierTurnLedger()
            self._carrier_turn_ledgers[key] = ledger
        return ledger

    def _record_carrier_turn_started(
        self, actor: str, session_ref: str, report_id: str
    ) -> tuple[int, bool]:
        at_ms = self._clock_ms()
        with self._interactive_turn_stats_lock:
            ledger = self._carrier_turn_ledger(actor, session_ref)
            if report_id in ledger.started:
                ledger.started.move_to_end(report_id)
                return ledger.started[report_id], False
            ledger.started[report_id] = at_ms
            _trim(ledger.started)
            return at_ms, True

    def _record_carrier_turn_ended(
        self, actor: str, session_ref: str, report_id: str, at_ms: int
    ) -> tuple[int, int | None, bool]:
        with self._interactive_turn_stats_lock:
            ledger = self._carrier_turn_ledger(actor, session_ref)
            if report_id in ledger.ended:
                ledger.ended.move_to_end(report_id)
                return (
                    ledger.ended[report_id],
                    ledger.started.get(report_id),
                    False,
                )
            ledger.ended[report_id] = at_ms
            _trim(ledger.ended)
            return at_ms, ledger.started.get(report_id), True

    def _record_interactive_turn_reports(
        self, actor: str, session_ref: str, report_ids: list[str], *, at_ms: int
    ) -> InteractiveTurnStats:
        """Count idempotent Stop pulses at daemon receipt time (top view)."""

        with self._interactive_turn_stats_lock:
            current = self._interactive_turn_stats.get(actor)
            if current is None or current.session_ref != session_ref:
                current = InteractiveTurnStats(
                    session_ref=session_ref,
                    counting_since_ms=at_ms,
                    turn_count=0,
                    last_turn_ended_at_ms=None,
                )
            new_ids = frozenset(report_ids) - current.observed_report_ids
            updated = InteractiveTurnStats(
                session_ref=session_ref,
                counting_since_ms=current.counting_since_ms,
                turn_count=current.turn_count + len(new_ids),
                last_turn_ended_at_ms=at_ms if new_ids else current.last_turn_ended_at_ms,
                observed_report_ids=current.observed_report_ids | new_ids,
            )
            self._interactive_turn_stats[actor] = updated
            return updated

    def _clock_ms(self) -> int:
        hooks = self._daemon_hooks
        if hooks is not None:
            return int(hooks.clock_ms())
        return int(time.time() * 1000)
