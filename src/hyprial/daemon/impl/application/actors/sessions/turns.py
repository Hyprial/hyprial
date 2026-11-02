"""Interactive carrier turn reports (WP5-A §4).

Every interactive carrier — whatever its runtime — reports the boundaries of
a model turn through two IPC methods.  The daemon stamps the clock, fences the
report against the current interactive session, deduplicates by
``(actor, sessionRef, reportId)`` and publishes ``turn.*`` through the one
``DaemonHookService`` publish path. Boundary reports do not trust carrier
sequence, source or wall time. Current-state refreshes additionally validate
producer sequence and the confirmed daemon epoch. Observation history belongs
to a separate daemon-epoch store; resetting counters/receipts cannot erase it.

This module deliberately does not import the carrier contract: the two IPC
method names are pinned to it by the router test, not by a shared import.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from hyprial.daemon.impl.cells.observability.model import RunFacts
from hyprial.daemon.impl.cells.observability.projection import RunLedger
from typing import Any

from hyprial.kernel import DaemonRequestError, ipc_errors

from hyprial.daemon.impl.application.actors.sessions.coverage import (
    declare_turn_coverage,
    whole_session_scope,
)
from hyprial.daemon.impl.ipc.params import JsonObject, _required_string
from hyprial.daemon.impl.operations.top import InteractiveTurnStats

__all__ = ["CarrierTurnLedger", "_SessionTurnsMixin"]

#: One actor's started/ended report ids retained per session, LRU-bounded.
_TURN_LEDGER_CAPACITY = 1024
_TURN_END_STATUSES = frozenset({"completed", "failed"})
_SESSION_TURN_STARTED = "session.turn.started"
_SESSION_TURN_ENDED = "session.turn.ended"
#: Explicit wire version of the current-run refresh payload.
_TURN_REFRESH_SCHEMA_VERSION = 1
#: Simultaneous open runs one refresh may report; over that is malformed.
_TURN_REFRESH_MAX_RUNS = 64
_TURN_RUN_PHASES = frozenset({"prepared", "executing"})
_TURN_CAPABILITIES = (
    "wholeSessionTurns",
    "preparation",
    "permissionWait",
    "producerActivity",
)
#: Bound on a producer-reported elapsed time (7 days) against overflow/typos.
_TURN_REFRESH_MAX_ELAPSED_MS = 7 * 24 * 60 * 60 * 1000


@dataclass
class CarrierTurnLedger:
    """Resettable receipts with a borrowed reference to the observation owner.

    ``turn_coverage`` is the producer's declared observation scope; None means
    the producer never declared one and the legacy source rule still applies.
    """

    observation: RunLedger
    started: "OrderedDict[str, int]" = field(default_factory=OrderedDict)
    ended: "OrderedDict[str, int]" = field(default_factory=OrderedDict)
    turn_coverage: str | None = None


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

    def _declare_turn_coverage(
        self, params: JsonObject, *, actor: str, session_ref: str
    ) -> None:
        """Record what the authenticated producer says it can observe.

        Coverage is a capability claim, not an inference: a carrier that only
        sees its own deliveries must not make a whole-session idle claim, and
        an undeclared producer keeps the legacy behaviour.
        """

        raw = params.get("turnCoverage")
        if raw is None:
            return
        try:
            declared = declare_turn_coverage(raw)
        except ValueError as error:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, str(error)
            ) from error
        with self._interactive_turn_stats_lock:
            ledger = self._carrier_turn_ledger(actor, session_ref)
            ledger.turn_coverage = declared

    def _require_current_interactive_session(
        self, actor: str, session_ref: str
    ) -> Any:
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
        return session

    def _ipc_session_turn_refresh(self, params: JsonObject) -> Any:
        """Replace stale turn observation with the carrier's current run set.

        Observation only: no turn event is published, no inbox row is read,
        settled, acked or replied to, and no lease is renewed. It is fenced by
        the same actor/session pair as the boundary reports, plus the channel
        lease when the registration has one.
        """

        actor = self._fence_interactive_session(self._mcp_actor(params), params)
        session_ref = _required_string(params.get("sessionRef"), "sessionRef")
        version = params.get("schemaVersion")
        if isinstance(version, bool) or version != _TURN_REFRESH_SCHEMA_VERSION:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"schemaVersion must be {_TURN_REFRESH_SCHEMA_VERSION}",
            )
        producer_epoch = _required_string(params.get("producerEpoch"), "producerEpoch")
        if len(producer_epoch) > 128:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "producerEpoch must be at most 128 characters",
            )
        seq = params.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "seq must be a non-negative integer",
            )
        runs = self._validated_refresh_runs(params.get("openRuns"))
        capabilities = self._validated_refresh_capabilities(params.get("capabilities"))
        session = self._require_current_interactive_session(actor, session_ref)
        if getattr(session, "channel_lease_backed", False):
            from hyprial.daemon.impl.application.actors.sessions.registry import (
                _verify_channel_lease,
            )

            persisted = next(
                (
                    item
                    for item in self.desired_state.load().interactive_sessions
                    if item.actor == actor and item.session_ref == session_ref
                ),
                None,
            )
            if persisted is None or persisted.channel_lease_digest is None:
                raise DaemonRequestError(
                    ipc_errors.CHANNEL_LIVENESS_UNAVAILABLE,
                    "channel registration has no operational liveness lease",
                )
            _verify_channel_lease(persisted, params.get("channelLeaseToken"))
        at_ms = self._clock_ms()
        runtime = self._agent_session_domains.session.read_runtime(actor)
        gap_revision = runtime.observation_gap_revision if runtime else None
        if (params.get("daemonEpoch") != self.epoch
                or runtime is None or runtime.current_epoch != self.epoch):
            applied, resync_cleared, rejection_reason = False, False, "stale_daemon_epoch"
        else:
            applied, resync_cleared, rejection_reason = self._record_carrier_turn_refresh(
                actor,
                session_ref,
                producer_epoch=producer_epoch,
                seq=seq,
                runs=runs,
                capabilities=capabilities,
                at_ms=at_ms,
            )
        gap_cleared = bool(
            applied
            and self._agent_session_domains.session.acknowledge_observation_gap(
                actor, session_ref, gap_revision=gap_revision
            )
        )
        return {
            "ok": True,
            "schemaVersion": _TURN_REFRESH_SCHEMA_VERSION,
            "refreshedAtMs": at_ms,
            "applied": applied,
            "resyncCleared": resync_cleared,
            "observationGapCleared": gap_cleared,
            "rejectionReason": rejection_reason,
        }

    def _validated_refresh_runs(self, value: object) -> list[tuple[str, str, int | None]]:
        if not isinstance(value, list) or len(value) > _TURN_REFRESH_MAX_RUNS:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"openRuns must be an array of at most {_TURN_REFRESH_MAX_RUNS} runs",
            )
        runs = []
        for row in value:
            if not isinstance(row, dict):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT, "openRuns entries must be objects"
                )
            report_id = row.get("reportId")
            if (
                not isinstance(report_id, str)
                or not report_id
                or len(report_id) > 128
            ):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "openRuns reportId must be 1-128 characters",
                )
            phase = row.get("phase")
            if phase not in _TURN_RUN_PHASES:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "openRuns phase must be prepared or executing",
                )
            elapsed_ms = row.get("elapsedMs")
            if elapsed_ms is not None and (
                isinstance(elapsed_ms, bool)
                or not isinstance(elapsed_ms, int)
                or not 0 <= elapsed_ms <= _TURN_REFRESH_MAX_ELAPSED_MS
            ):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "openRuns elapsedMs must be a bounded non-negative integer",
                )
            runs.append((report_id, phase, elapsed_ms))
        return runs

    def _validated_refresh_capabilities(self, value: object) -> dict[str, bool]:
        if not isinstance(value, dict) or set(value) != set(_TURN_CAPABILITIES):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "capabilities must declare exactly " + ", ".join(_TURN_CAPABILITIES),
            )
        if any(not isinstance(flag, bool) for flag in value.values()):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "capabilities values must be booleans"
            )
        return {key: bool(value[key]) for key in _TURN_CAPABILITIES}

    def _carrier_turn_ledger(
        self, actor: str, session_ref: str
    ) -> CarrierTurnLedger:
        key = (actor, session_ref)
        observation = self._carrier_run_ledgers.setdefault(actor, RunLedger())
        ledger = self._carrier_turn_ledgers.get(key)
        if ledger is None:
            ledger = CarrierTurnLedger(observation=observation)
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
            self._observe_carrier_receipt(ledger, actor, session_ref, report_id, "start", at_ms)
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
            self._observe_carrier_receipt(ledger, actor, session_ref, report_id, "end", at_ms)
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

    def _cell_binding_fence(self, actor, session_ref):
        domains = getattr(self, "_agent_session_domains", None)
        if domains is None:
            return None
        agent = domains.agent.read_agent(actor)
        binding = domains.agent.read_binding(actor)
        if (agent is None or binding is None or binding.runtime != "interactive"
                or binding.session_id != session_ref):
            return None
        return (actor, agent.entity_token, binding.revision, session_ref)

    def _advance_carrier_observation(self, observation, actor, fence):
        """Advance the captured identity-owned mount under the receipt lock."""

        if observation.epoch != fence:
            observation.advance_mount(fence)
            self._log(
                "info", "daemon", "observation.mount.advanced",
                actor=actor, bindingRevision=fence[2], daemonEpoch=self.epoch,
            )

    def _observe_carrier_receipt(self, ledger, actor, session_ref, report_id, phase, at_ms):
        fence = self._cell_binding_fence(actor, session_ref)
        if fence is not None:
            self._advance_carrier_observation(ledger.observation, actor, fence)
            ledger.observation.record(fence, report_id, phase, at_ms=at_ms,
                                      monotonic_ms=time.monotonic_ns() // 1_000_000)

    def _record_carrier_turn_refresh(
        self,
        actor: str,
        session_ref: str,
        *,
        producer_epoch: str,
        seq: int,
        runs: list[tuple[str, str, int | None]],
        capabilities: dict[str, bool],
        at_ms: int,
    ) -> tuple[bool, bool, str | None]:
        """Return admission, resync and rejection under the turn ledger lock.

        Only a newer admitted statement can acknowledge a contact gap.
        A valid mount alone does not make a duplicate sequence current.
        """

        with self._interactive_turn_stats_lock:
            fence = self._cell_binding_fence(actor, session_ref)
            if fence is None:
                return False, False, "mount_unavailable"
            ledger = self._carrier_turn_ledger(actor, session_ref)
            self._advance_carrier_observation(ledger.observation, actor, fence)
            applied, resync_cleared = ledger.observation.refresh(
                fence,
                producer_epoch=producer_epoch,
                seq=seq,
                runs=runs,
                capabilities=capabilities,
                at_ms=at_ms,
                monotonic_ms=time.monotonic_ns() // 1_000_000,
            )
            return applied, resync_cleared, ledger.observation.refresh_rejection_reason

    def _cell_interactive_facts(self, *, sessions=None):
        domains = self._agent_session_domains
        rows = {}
        sessions = domains.session.read_sessions() if sessions is None else sessions
        with self._interactive_turn_stats_lock:
            for session in sessions:
                fence = self._cell_binding_fence(session.actor, session.session_ref)
                if fence is None:
                    continue
                observation = self._carrier_run_ledgers.get(session.actor)
                facts = observation.snapshot(fence) if observation else RunFacts(source_epoch=str(fence))
                runtime = domains.session.read_runtime(session.actor)
                process = domains.session.process_observations.read(session)
                capabilities = (
                    observation.capabilities
                    if observation and observation.epoch == fence else None
                ) or {}
                ledger = self._carrier_turn_ledgers.get(
                    (session.actor, session.session_ref)
                )
                declared = ledger.turn_coverage if ledger is not None else None
                if capabilities:
                    whole = capabilities.get("wholeSessionTurns") is True
                else:
                    whole = whole_session_scope(declared, source=session.source)
                current = bool(runtime and runtime.alive and runtime.current_epoch is not None)
                rows[fence] = replace(
                    facts,
                    contact_current=current,
                    known=facts.known and not bool(runtime and runtime.observation_gap),
                    resync_required=facts.resync_required or bool(runtime and runtime.observation_gap),
                    scope="whole-session" if whole else "carrier-deliveries",
                    permission_supported=capabilities.get("permissionWait") is True,
                    activity_supported=capabilities.get("producerActivity") is True,
                    process_exit_supported=process.supported,
                    exit_verified=process.exit_verified,
                )
        return rows

    def _clock_ms(self) -> int:
        hooks = self._daemon_hooks
        if hooks is not None:
            return int(hooks.clock_ms())
        return int(time.time() * 1000)
