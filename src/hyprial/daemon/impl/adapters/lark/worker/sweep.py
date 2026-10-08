"""One-process-per-gateway Lark adapter worker."""

from __future__ import annotations

import threading
from collections.abc import Callable


from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    ReconcileReport,
)
from hyprial.daemon.impl.adapters.lark.runtime.health import (
    LarkStreamHealthMonitor,
    report_reconcile_failure,
)

STALE_REBUILD_EXIT_CODE = 75
def _reconcile_health_result(report: ReconcileReport | None) -> int:
    """Translate a sweep into a fail-closed, detail-free health result.

    Fail-closed is deliberate and unchanged: a sweep that might have missed
    messages must not report health.  What changed is *which* failures count.

    ``blocked_chats`` are chats this app is not permitted to read; retrying
    cannot clear them, so raising here produced a restart loop that ended in
    quarantine -- the adapter died of a condition no restart could fix, and
    every other chat died with it.  Those are reported by the sweep and
    logged, and they do not fail the probe.

    A permanently-refused chat no longer fails the probe by itself -- it is
    retired out of the scan set (``retired_chats``) -- but a sweep that reached
    *no* live chat at all is still a total failure and must stay stale.

    A missing history scope is the one exception among the *retryable* errors:
    a mention-only Feishu app cannot replay messages it was never permitted to
    read, but its websocket subscription can still receive new @-mentions.
    Keep that live path up rather than restarting forever; all other incomplete
    reconciliation outcomes remain terminal.
    """

    if report is None or any(
        not error.endswith(": history-permission-unavailable")
        for error in report.retryable_errors
    ):
        raise RuntimeError("Lark history reconciliation incomplete")
    if report.chats_scanned > 0 and len(report.retired_chats) >= report.chats_scanned:
        raise RuntimeError("Lark history reconciliation reached no live chat")
    return report.forwarded + report.dead_lettered

class ReconnectSweepCoordinator:
    """Coalesce reconnect storms into one deadline-bound history pipeline."""

    def __init__(
        self,
        *,
        name: str,
        reconcile: Callable[[], ReconcileReport | None],
        timeout: float,
        health: LarkStreamHealthMonitor,
        report: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.name = name
        self._reconcile = reconcile
        self.timeout = timeout
        self._health = health
        self._report = report
        self._lock = threading.Lock()
        self._active = False
        self._pending = False
        self._terminal_failure = False

    def request(self) -> bool:
        """Start one sweep or coalesce this callback into the active sweep."""

        with self._lock:
            if self._terminal_failure or self._health.rebuild_latched():
                self._terminal_failure = True
                return False
            if self._active:
                if not self._health.history_reconcile_started(coalesced=True):
                    self._terminal_failure = True
                    self._pending = False
                    return False
                self._pending = True
                return False
            if not self._health.history_reconcile_started():
                self._terminal_failure = True
                return False
            self._active = True
            self._pending = False
            threading.Thread(
                target=self._run,
                name=f"lark-reconcile-{self.name}",
                daemon=True,
            ).start()
            return True

    def _run(self) -> None:
        while True:
            if self._health.rebuild_latched():
                with self._lock:
                    self._terminal_failure = True
                    self._active = False
                    self._pending = False
                return
            failed, report = self._call_once()
            with self._lock:
                if self._terminal_failure:
                    self._active = False
                    self._pending = False
                    return
            if not failed:
                try:
                    _reconcile_health_result(report)
                except RuntimeError:
                    failed = True
            if failed:
                with self._lock:
                    self._terminal_failure = True
                    self._active = False
                    self._pending = False
                self._health.history_probe_failed()
                return

            with self._lock:
                if self._pending:
                    # Any number of callbacks while the sweep was active need
                    # exactly one final pass covering the latest reconnect.
                    self._pending = False
                    if not self._health.history_reconcile_started(coalesced=True):
                        self._terminal_failure = True
                        self._active = False
                        return
                    continue
                # Keep the coordinator lock while publishing healthy so a new
                # callback cannot slip between settlement and connected().
                if not self._health.connected():
                    self._terminal_failure = True
                    self._active = False
                    self._pending = False
                    return
                self._active = False
                return

    def _call_once(self) -> tuple[bool, ReconcileReport | None]:
        completed = threading.Event()
        outcome: dict[str, object] = {"failed": False, "report": None}

        def invoke() -> None:
            try:
                outcome["report"] = self._reconcile()
            except (NameError, ImportError):
                # Re-raise programming/import defects for visibility, while
                # also making the coordinator fail closed instead of
                # accidentally settling this sweep as healthy.
                outcome["failed"] = True
                raise
            except BaseException as error:  # never surface SDK URL/token details
                outcome["failed"] = True
                # Exception from the sweep never reaches here (the wrapper
                # registered as ``reconcile`` folds it into None first), so
                # this names the non-Exception BaseExceptions that bypass
                # that wrapper: KeyboardInterrupt, SystemExit, CancelledError.
                report_reconcile_failure(self._report, error, stage="sweep-call")
            finally:
                completed.set()

        threading.Thread(
            target=invoke,
            name=f"lark-reconcile-call-{self.name}",
            daemon=True,
        ).start()
        if not completed.wait(self.timeout):
            return True, None
        report = outcome["report"]
        if report is not None and not isinstance(report, ReconcileReport):
            return True, None
        return bool(outcome["failed"]), report
