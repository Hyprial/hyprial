"""Codex carrier helpers: hint-settlement port and the durable turn-report relay."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from hyprial.kernel import ipc_errors

from hyprial.daemon.impl.harnesses.carrier.codex_carrier_store import CodexCarrierStore
from hyprial.daemon.impl.harnesses.carrier.contract import IpcCarrierPort

__all__ = ["HintPort", "TurnReportRelay"]

# Reports the daemon will never accept for this session: drop, do not retry.
_UNFENCED_REPORT_CODES = frozenset(
    {
        ipc_errors.SESSION_SUPERSEDED,
        ipc_errors.STALE_SESSION,
        ipc_errors.INVALID_ARGUMENT,
    }
)

Request = Callable[[str, dict[str, Any]], Mapping[str, Any]]
Signed = Callable[[dict[str, Any]], dict[str, Any]]
CodeOf = Callable[[Exception], str | None]


class HintPort(IpcCarrierPort):
    """The shared port, plus why an id deferred and whether its ack found no row."""

    def __init__(self, request: Request, signed: Signed, code_of: CodeOf) -> None:
        super().__init__(request, signed)
        self._code_of = code_of
        self.errors: dict[str, str] = {}
        self.ack_unavailable: set[str] = set()

    def settle_hint(self, message_id: str):  # type: ignore[no-untyped-def]
        try:
            return super().settle_hint(message_id)
        except Exception as error:
            self.errors[message_id] = str(error) or type(error).__name__
            raise

    def ack(self, message_id: str) -> bool:
        try:
            result = self._request("message.ack", self._signed({"messageId": message_id}))
        except Exception as error:
            if self._code_of(error) == ipc_errors.MESSAGE_ACK_UNAVAILABLE:
                self.ack_unavailable.add(message_id)
                return False
            self.errors[message_id] = str(error) or type(error).__name__
            raise
        if result.get("acknowledged") is True:
            return True
        if result.get("code") == ipc_errors.MESSAGE_ACK_UNAVAILABLE:
            self.ack_unavailable.add(message_id)
        else:
            self.errors[message_id] = "daemon did not confirm durable settlement"
        return False


class TurnReportRelay:
    """Journal turn reports in the store and send them in order until confirmed.

    The same ``reportId`` is re-sent after a transport or authority failure
    (the daemon fixes the time on first acceptance); a report the session
    fence rejects is dropped with a diagnostic.  Only the poll thread sends.
    """

    def __init__(
        self,
        store: CodexCarrierStore,
        *,
        actor: str,
        session_ref: str,
        request: Request,
        signed: Signed,
        code_of: CodeOf,
        retry_seconds: float,
        log: Callable[..., None],
    ) -> None:
        self._store = store
        self._actor = actor
        self._session_ref = session_ref
        self._port = IpcCarrierPort(request, signed)
        self._code_of = code_of
        self._retry_seconds = retry_seconds
        self._log = log
        self._failures = 0
        self._retry_at = 0.0
        store.drop_other_session_reports(actor, session_ref)

    def record(
        self, report_id: str, phase: str, status: str | None, delivery_id: str
    ) -> None:
        self._store.add_turn_report(
            self._actor, self._session_ref, report_id, phase, status,
            delivery_id, int(time.time() * 1000),
        )

    def flush(self) -> None:
        if time.monotonic() < self._retry_at:
            return
        store = self._store
        for report in store.pending_turn_reports(self._actor, self._session_ref):
            try:
                if report.phase == "started":
                    self._port.turn_started(report.report_id, delivery_id=report.delivery_id)
                else:
                    self._port.turn_ended(
                        report.report_id,
                        status=report.status or "completed",
                        delivery_id=report.delivery_id,
                    )
            except Exception as error:  # noqa: BLE001 - reports are retried
                detail = str(error) or type(error).__name__
                fields = {"reportId": report.report_id, "phase": report.phase, "error": detail}
                if self._code_of(error) in _UNFENCED_REPORT_CODES:
                    store.delete_turn_report(self._actor, report.report_id, report.phase)
                    self._log("error", "worker.carrier.error", stage="turn-report", **fields)
                    continue
                self._failures += 1
                self._retry_at = time.monotonic() + min(
                    5.0, self._retry_seconds * 2 ** min(self._failures, 8)
                )
                self._log("warn", "worker.carrier.error", stage="turn-report-retry", **fields)
                return
            store.delete_turn_report(self._actor, report.report_id, report.phase)
        self._failures = 0
