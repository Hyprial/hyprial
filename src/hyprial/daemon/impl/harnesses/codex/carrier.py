"""CodexInteractiveCarrier and its per-turn client.

Turn settlement behaviors live in settlement.py as an explicit mixin;
carrier state is created and owned by CodexInteractiveCarrier.__init__.
"""
from __future__ import annotations


import asyncio
import re
import sys as sys
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Self

from hyprial.kernel import CHANNEL_HEARTBEAT_INTERVAL_SECONDS
from hyprial.daemon.impl.api import HarnessDelivery, HarnessResultStatus
from hyprial.kernel import Logger

from hyprial.daemon.impl.harnesses.carrier.codex_carrier_store  import CodexCarrierStore
from hyprial.daemon.impl.harnesses.carrier.codex_carrier_support import TurnReportRelay
from hyprial.daemon.impl.harnesses.carrier.interactive_carrier_runtime  import (
    FETCHED,
    FINAL_OBSERVED,
    STATUS_UNKNOWN,
    CarrierCommand,
    CarrierFinalObserved,
    CarrierRemoved,
    CarrierTurnStarted,
    InteractiveCarrierRuntime,
)
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.harnesses.codex.native_env import (
    _final_reply,
    _turn_error,
)
from hyprial.daemon.impl.harnesses.codex.process import (
    CodexAppServerRpcError,
    _CodexTurnOutcome,
)
from hyprial.daemon.impl.harnesses.codex.app_server import (
    CodexInteractiveAppServer,
)
from hyprial.daemon.impl.harnesses.codex.observation.whole_session import (
    CarrierTurnOwnership,
    WholeTerminalTurns,
)
from hyprial.daemon.impl.harnesses.codex.settlement import (
    _CarrierSettlementMixin,
)
from hyprial.daemon.impl.inbox_watch import (
    Heartbeat,
    InboxChange,
    InboxWatch,
    ListInbox,
    Ok,
    Quiet,
    Refresh,
    Register,
    Rejected,
    Request,
    Sleep,
    Unreachable,
    Wait,
)

# Quiet-period report sensitivity (issue #277 ruling: liveness becomes the
# connector's job via steer probing, and NO timeout ever kills a turn --
# wall-clock caps killed working turns twice over, #270 for managed and
# half of #94 for interactive).  After this long without any NEW
# app-server notification correlated with the running turn, the client
# reports ``turn-stalled`` (and ``turn-resumed`` when activity returns);
# nothing is interrupted.  0 disables reporting.  The level state "thread
# status inProgress" never counts as activity.
MANAGED_TURN_IDLE_TIMEOUT_SECONDS = 900.0

_INBOX_HOLD_MS = 2_000
_ERROR_CODE_PREFIX = re.compile(r"([A-Z][A-Z0-9_]*):")
_IDLE_HOLDS_PER_LIST = 2

class CodexInteractiveTurnClient:
    """TurnClient adapter over the already-running external app-server."""

    def __init__(
        self,
        server: CodexInteractiveAppServer,
        prepare_delivery: Callable[[HarnessDelivery], HarnessDelivery | None] | None = None,
        *,
        ownership: CarrierTurnOwnership | None = None,
    ) -> None:
        self.server = server
        self._prepare_delivery = prepare_delivery
        self.delivery_skipped = False
        self._ownership = ownership
        self._turn_id: str | None = None
        self._observation_lock = threading.Lock()
        self._reconciled_final: tuple[str, str] | None = None
        self._status_unknown: str | None = None

    @property
    def turn_id(self) -> str | None:
        with self._observation_lock:
            return self._turn_id

    def supply_reconciled_final(self, turn_id: str, reply: str) -> None:
        """Release receive_response when the carrier's independent read won.

        The carrier and pump intentionally read the same authoritative
        ``thread/read`` projection.  Whichever observer sees the completed
        turn first records the final; this handoff prevents a transient pump
        RPC failure from leaving its delivery permanently in-flight.
        """

        with self._observation_lock:
            if self._turn_id == turn_id:
                self._reconciled_final = (turn_id, reply)

    def supply_status_unknown(self, turn_id: str) -> None:
        """Release the local observer without interrupting the native turn."""
        with self._observation_lock:
            if self._turn_id == turn_id:
                self._status_unknown = turn_id

    @property
    def running(self) -> bool:
        return self.server.pid is not None

    @property
    def pid(self) -> int | None:
        return self.server.pid

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def query_delivery(self, delivery: HarnessDelivery) -> None:
        self.delivery_skipped = False
        if self._prepare_delivery is not None:
            prepared = await asyncio.to_thread(self._prepare_delivery, delivery)
            if prepared is None:
                self._turn_id = None
                self.delivery_skipped = True
                return
            delivery = prepared
        await self.query(delivery.model_prompt())

    async def query(self, prompt: str) -> None:
        """Start one carrier-injected turn and claim it before the RPC.

        The whole-terminal observer can be handed this turn's native
        notification before the response arrives, so the claim must already be
        held; the settlement path stays the only reporter of this turn.
        """

        if self._turn_id is not None:
            raise ConnectionError("Codex interactive app-server turn is busy")
        ownership = self._ownership
        if ownership is not None:
            ownership.begin_start()
        try:
            turn_id = await asyncio.to_thread(self.server.start_turn, prompt)
        except BaseException:
            if ownership is not None:
                ownership.abort_start()
            raise
        with self._observation_lock:
            self._turn_id = turn_id
            self._reconciled_final = None
        if ownership is not None:
            ownership.commit_start(turn_id)

    async def receive_response(self) -> AsyncIterator[object]:
        if self.delivery_skipped:
            yield _CodexTurnOutcome("Codex queued delivery no longer pending", is_error=True)
            return
        turn_id = self._turn_id
        if turn_id is None:
            raise ConnectionError("Codex interactive turn was not started")
        # No wall-clock deadline: the interactive 900s cap killed live TUI
        # turns (half of #94), and #277 retires time-based kills entirely.
        # The turn ends when the app-server says so, or on interrupt.
        try:
            while True:
                with self._observation_lock:
                    reconciled = self._reconciled_final
                    status_unknown = self._status_unknown
                if status_unknown == turn_id:
                    yield _CodexTurnOutcome(
                        "Codex native turn status unknown after idle deadline", is_error=True
                    )
                    return
                if reconciled is not None and reconciled[0] == turn_id:
                    yield _CodexTurnOutcome(reconciled[1])
                    return
                try:
                    turn = await asyncio.to_thread(self.server.read_turn, turn_id)
                except CodexAppServerRpcError:
                    await asyncio.sleep(0.5)
                    continue
                status = turn.get("status")
                reply = _final_reply(turn)
                final_phase = any(
                    isinstance(item, dict)
                    and item.get("type") == "agentMessage"
                    and item.get("phase") == "final_answer"
                    for item in turn.get("items", [])
                    if isinstance(turn.get("items"), list)
                )
                if status == "completed" or (reply is not None and final_phase):
                    if reply is None:
                        yield _CodexTurnOutcome(
                            "Codex app-server returned no final agent message",
                            is_error=True,
                        )
                    else:
                        yield _CodexTurnOutcome(reply)
                    return
                if status in {"failed", "interrupted"}:
                    yield _CodexTurnOutcome(_turn_error(turn, turn_id), is_error=True)
                    return
                await asyncio.sleep(0.5)
        finally:
            with self._observation_lock:
                self._turn_id = None
                self._reconciled_final = None
                self._status_unknown = None

    async def interrupt(self) -> None:
        turn_id = self.turn_id
        if turn_id is not None:
            await asyncio.to_thread(self.server.interrupt_turn, turn_id)


class CodexInteractiveCarrier(_CarrierSettlementMixin):
        def __init__(
            self,
            server: CodexInteractiveAppServer,
            *,
            actor: str,
            session_ref: str,
            cwd: Path,
            command: list[str],
            daemon_request: Callable[[str, dict[str, object]], dict[str, object]],
            state_path: Path,
            process_pid: int | None = None,
            process_identity: str | None = None,
            poll_seconds: float = 0.5,
            logger: Logger | None = None,
            settlement_retry_seconds: float | None = None,
            clock: Callable[[], float] = time.monotonic,
        ) -> None:
            from hyprial.daemon.impl.harnesses.streaming.process  import StreamingTurnProcess
    
            self.server = server
            self.actor = actor
            self.session_ref = session_ref
            self.cwd = cwd
            self.command = command
            self.daemon_request = daemon_request
            if (process_pid is None) != (process_identity is None):
                raise ValueError(
                    "Codex carrier process pid and identity must be provided together"
                )
            self.process_pid = process_pid
            self.process_identity = process_identity
            self.poll_seconds = poll_seconds
            self._heartbeat_clock = clock
            self._inbox_watch = InboxWatch(
                poll_interval_ms=max(1, int(poll_seconds * 1000)),
                heartbeat_interval_ms=max(
                    1, int(CHANNEL_HEARTBEAT_INTERVAL_SECONDS * 1000)
                ),
                hold_ms=_INBOX_HOLD_MS,
            )
            # CLI startup registers and fences the session before constructing
            # this carrier.  The core adopts that fact, confirms it with the
            # carrier's existing first refresh, and owns every later lifecycle
            # transition (including STALE_SESSION -> register).
            self._inbox_watch.adopt_registered_session(
                int(self._heartbeat_clock() * 1000)
            )
            self.settlement_retry_seconds = (
                poll_seconds
                if settlement_retry_seconds is None
                else settlement_retry_seconds
            )
            if self.settlement_retry_seconds <= 0:
                raise ValueError("settlement retry interval must be positive")
            self.logger = logger
            self._store: CodexCarrierStore | None = CodexCarrierStore(state_path)
            self._stop = threading.Event()
            self._carrier_io_lock = threading.RLock()
            self._carrier_queue_context: dict[str, dict[str, object]] = {}
            self._carrier_skipped_deliveries: set[str] = set()
            self._carrier_clients: dict[str, CodexInteractiveTurnClient] = {}
            self._carrier_fact_lock = threading.RLock()
            self._carrier_facts: dict[tuple[str, str], CarrierCommand] = {}
            # ``drain_effects()`` deliberately derives required effects from the
            # actor projection so an actor restart cannot lose them.  That also
            # means a FETCHED projection can yield the same enqueue effect on
            # consecutive poll iterations while the asynchronous TURN_STARTED or
            # FINAL_OBSERVED fact is still waiting in the actor mailbox.  The turn
            # pump releases its own admission as soon as a result is drained, so
            # without this process-local custody fence that short window can start
            # the same native prompt twice.  Keep one accepted enqueue per exact
            # actor generation/version; durable recovery still reissues FETCHED
            # work after a process restart.
            self._carrier_enqueued: set[tuple[str, int, int]] = set()
            self._carrier_settled_pending: set[str] = set()
            # Turn ids whose ended report was journaled by this process; the
            # journal's primary key makes a repeat harmless, this avoids
            # re-adding a row the daemon already confirmed.
            self._carrier_reported_ended: set[str] = set()
            self._turn_ownership = CarrierTurnOwnership()
            self._whole_terminal: WholeTerminalTurns | None = None
            self._whole_terminal_detach: Callable[[], None] | None = None
            self._declared_turn_coverage = "carrier-deliveries"
            self._declared_coverage_gaps = 0
            self._thread = threading.Thread(
                target=self._poll_main,
                name=f"hyprial-codex-interactive-carrier-{session_ref[:8]}",
                daemon=True,
            )
            self._pump = StreamingTurnProcess(
                harness="codex",
                label="Codex interactive app-server",
                client_factory=lambda: CodexInteractiveTurnClient(
                    server, self._prepare_dequeued_delivery, ownership=self._turn_ownership
                ),
                thread_name=f"hyprial-codex-interactive-pump-{session_ref[:8]}",
                logger=logger,
                reconnect_delay_seconds=0.25,
                on_turn_started=self._on_turn_started,
            )
            assert self._store is not None
            self._carrier_runtime = InteractiveCarrierRuntime(
                name=session_ref,
                actor=actor,
                session_ref=session_ref,
                store=self._store,
            )
            self._carrier_fact_capacity = self._carrier_runtime.capacity
            self._turn_reports = TurnReportRelay(
                self._store,
                actor=actor,
                session_ref=session_ref,
                request=self.daemon_request,
                signed=self._signed,
                code_of=self._daemon_error_code,
                retry_seconds=self.settlement_retry_seconds,
                log=lambda level, event, **fields: self._log(level, event, **fields),
            )

        @property
        def running(self) -> bool:
            return not self._stop.is_set() and self._pump.running

        @property
        def pid(self) -> int | None:
            return self.server.pid

        def start(self) -> None:
            self._start_whole_terminal()
            self._thread.start()

        def stop(self) -> None:
            self._stop.set()
            try:
                self._stop_whole_terminal()
                self._pump.stop()
                self._thread.join(timeout=3.0)
            finally:
                if self._store is not None:
                    self._retry_carrier_facts()
                    self._carrier_runtime.drain(3.0)
                    self._drain_carrier_effects()
                    self._flush_whole_terminal_reports()
                    self._store.close()
                    self._store = None

        def _start_whole_terminal(self) -> None:
            """Follow native TUI turns on the shared app-server, when supported.

            The launcher owns that app-server on behalf of the TUI, so a
            foreground session can observe every turn in the terminal. A server
            without the subscription surface, or a detached mode without this
            carrier, keeps carrier-only coverage instead of inventing turns.
            """

            subscribe = getattr(self.server, "subscribe_thread", None)
            read_thread = getattr(self.server, "read_thread_snapshot", None)
            add_listener = getattr(self.server, "add_notification_listener", None)
            if not (
                callable(subscribe) and callable(read_thread) and callable(add_listener)
            ):
                return
            observer = WholeTerminalTurns(
                thread_id=self.session_ref,
                subscribe=subscribe,
                read_thread=read_thread,
                record=self._record_whole_terminal_turn,
                owned=self._turn_ownership.owns,
                log=lambda event, detail: self._log(
                    "warn", "worker.carrier.error", stage=event, error=detail
                ),
            )
            self._whole_terminal_detach = add_listener(observer.notify)
            self._whole_terminal = observer
            observer.start()

        def _stop_whole_terminal(self) -> None:
            detach = self._whole_terminal_detach
            self._whole_terminal_detach = None
            if detach is not None:
                detach()
            observer = self._whole_terminal
            self._whole_terminal = None
            if observer is not None:
                observer.stop()

        def _turn_coverage(self) -> str:
            observer = self._whole_terminal
            if observer is not None and observer.subscribed:
                return "whole-session"
            return "carrier-deliveries"

        def _sync_turn_coverage(self) -> None:
            """Publish a changed observation capability on the existing refresh.

            A session that cannot subscribe must not keep a whole-session claim
            it can no longer honour; a newly subscribed one must stop looking
            carrier-only. This is one daemon read per change, never per poll.
            """

            observer = self._whole_terminal
            gaps = observer.coverage_gaps if observer is not None else 0
            coverage = self._turn_coverage()
            if gaps != self._declared_coverage_gaps:
                if self._declared_turn_coverage == "whole-session":
                    coverage = "carrier-deliveries"
            if coverage != self._declared_turn_coverage:
                self.daemon_request(
                    "session.refresh", self._signed({"turnCoverage": coverage})
                )
                self._declared_turn_coverage = coverage
            self._declared_coverage_gaps = gaps

        def _record_whole_terminal_turn(
            self, turn_id: str, phase: str, status: str | None
        ) -> None:
            if self._store is None or self._stop.is_set():
                raise RuntimeError("Codex turn report store is stopped")
            self._turn_reports.record(
                turn_id,
                phase,
                status if phase == "ended" else None,
                "",
            )

        def _flush_whole_terminal_reports(self) -> None:
            try:
                self._turn_reports.flush()
            except Exception as error:  # noqa: BLE001 - shutdown boundary
                self._log(
                    "warn",
                    "worker.carrier.error",
                    stage="turn-report-flush",
                    error=str(error) or type(error).__name__,
                )

        def _signed(self, extra: dict[str, object] | None = None) -> dict[str, object]:
            return {
                "actor": self.actor,
                "sessionRef": self.session_ref,
                **(extra or {}),
            }

        def _poll_main(self) -> None:
            while not self._stop.is_set():
                try:
                    delay = self._poll_once()
                except Exception as error:  # noqa: BLE001 - carrier retry boundary
                    self._log(
                        "error",
                        "worker.carrier.error",
                        stage="poll",
                        error=str(error) or type(error).__name__,
                    )
                    delay = self.poll_seconds
                if delay is None:
                    return
                self._stop.wait(delay)

        def _poll_once(self) -> float | None:
            """Drive one heartbeat step and one poll-lane step.

            The carrier has one polling thread, so both core lanes are
            serialized here.  No lock spans daemon I/O; ``stop()`` only sets an
            event and a held request is bounded to two seconds.
            """

            if self._stop.is_set():
                return None
            self._reconcile_inflight_turns()
            self._sync_turn_coverage()
            active, _urgent = self._drive_heartbeat()
            if not active:
                return None
            if self._stop.is_set():
                return None

            # The carrier actor runs on its own thread, so the turn it asks for
            # (EnqueueTurnRequested) can arrive after the list that caused it.
            # Drain it on every step, not only after the next list: otherwise a
            # woken message waits a whole poll interval to start its turn.
            self._drain_carrier_effects()
            self._turn_reports.flush()
            self._inbox_watch.poll_interval_ms = max(
                1, int(self.poll_seconds * 1000)
            )
            self._inbox_watch.use_wait = self._inbox_hold_allowed()
            action = self._inbox_watch.poll_next(self._now_ms())
            if isinstance(action, Quiet):
                self._log(
                    "warn",
                    "worker.carrier.stopped",
                    stage="session-fence",
                    error=ipc_errors.SESSION_SUPERSEDED,
                )
                return None
            if isinstance(action, Sleep):
                return action.ms / 1000

            outcome = self._perform_watch(action)
            if isinstance(action, ListInbox) and isinstance(outcome, Ok):
                # Between poll_next and poll_done nothing may raise: the
                # request would stay outstanding and wedge the poll lane.
                try:
                    self._inbox_watch.use_wait = self._list_may_hold(outcome.response)
                except Exception as exc:  # noqa: BLE001 - never leave a request unsettled
                    self._inbox_watch.use_wait = False
                    self._log(
                        "warn",
                        "worker.carrier.error",
                        stage="inbox-hold-check",
                        error=f"{type(exc).__name__}: {exc}",
                    )
            change = self._inbox_watch.poll_done(action, outcome, self._now_ms())
            if not isinstance(outcome, Ok):
                self._report_watch_failure(action, outcome)
            if isinstance(action, ListInbox) and isinstance(outcome, Ok):
                self._apply_inbox_list(change, outcome.response)
            return None if self._stop.is_set() else 0.0

        def _drive_heartbeat(self) -> tuple[bool, bool]:
            """Perform a due heartbeat/refresh; return (active, urgent-poll)."""

            action = self._inbox_watch.heartbeat_next(self._now_ms())
            if isinstance(action, Quiet):
                return False, True
            if isinstance(action, Sleep):
                return True, False
            outcome = self._perform_watch(action)
            self._inbox_watch.heartbeat_done(action, outcome, self._now_ms())
            if not isinstance(outcome, Ok):
                self._report_watch_failure(action, outcome)
            urgent = (
                isinstance(action, Refresh)
                or self._inbox_watch.refresh_owed
                or not self._inbox_watch.registered
            )
            return not self._inbox_watch.quiet, urgent

        def _perform_watch(self, action: Request) -> Ok | Rejected | Unreachable:
            try:
                response = self._request_watch(action)
            except Exception as error:  # noqa: BLE001 - normalize IPC boundary
                code = self._daemon_error_code(error)
                return Rejected(code) if code is not None else Unreachable()
            if not isinstance(response, Mapping):
                return Unreachable()
            return Ok(response)

        def _request_watch(self, action: Request) -> dict[str, object]:
            if isinstance(action, Register):
                return self.daemon_request(
                    "session.register", self._registration_params()
                )
            if isinstance(action, Refresh):
                return self.daemon_request(
                    "session.refresh",
                    self._signed({"turnCoverage": self._turn_coverage()}),
                )
            if isinstance(action, Heartbeat):
                return self.daemon_request("session.heartbeat", self._signed())
            if isinstance(action, ListInbox):
                return self.daemon_request("message.pending.list", self._signed())
            assert isinstance(action, Wait)
            params = self._signed(
                {
                    "knownMessageIds": list(action.known_message_ids),
                    "holdMs": min(_INBOX_HOLD_MS, action.hold_ms or _INBOX_HOLD_MS),
                }
            )
            response = self.daemon_request("message.pending.wait", params)
            # A 2s hold is the largest safe individual IPC block.  Coalesce two
            # unchanged holds before listing again (15 idle lists/minute), while
            # checking liveness between them.  A due refresh/re-register breaks
            # the coalescing immediately, so restart recovery never waits out a
            # second hold.
            for _hold in range(1, _IDLE_HOLDS_PER_LIST):
                if (
                    self._stop.is_set()
                    or response.get("held") is not True
                    or response.get("changed") is True
                ):
                    break
                active, urgent = self._drive_heartbeat()
                if not active or urgent:
                    break
                response = self.daemon_request("message.pending.wait", params)
            return response

        def _registration_params(self) -> dict[str, object]:
            return {
                **self._signed(),
                "cwd": str(self.cwd),
                "command": self.command,
                "source": "codex-app-server",
                "runtime": "codex_interactive",
                **(
                    {
                        "processPid": self.process_pid,
                        "processIdentity": self.process_identity,
                    }
                    if self.process_pid is not None
                    and self.process_identity is not None
                    else {}
                ),
            }

        @staticmethod
        def _daemon_error_code(error: Exception) -> str | None:
            code = getattr(error, "code", None)
            if isinstance(code, str) and code:
                return code
            # Daemon errors reach this thread as "CODE: message" text.
            match = _ERROR_CODE_PREFIX.match(str(error))
            return match.group(1) if match else None

        def _report_watch_failure(
            self, action: Request, outcome: Rejected | Unreachable
        ) -> None:
            if isinstance(outcome, Rejected) and outcome.code == ipc_errors.METHOD_NOT_FOUND:
                return
            self._log(
                "error",
                "worker.carrier.error",
                stage=f"inbox-watch-{action.op}",
                error=(outcome.code if isinstance(outcome, Rejected) else "unreachable"),
            )

        def _inbox_hold_allowed(self) -> bool:
            return not any(
                state.stage != STATUS_UNKNOWN
                for state in self._carrier_runtime.snapshots()
            ) and self._pump.idle

        def _list_may_hold(self, response: Mapping[str, object]) -> bool:
            if not self._inbox_hold_allowed():
                return False
            known = self._known_ids()
            rows = response.get("messages", ())
            return not any(
                isinstance(item, Mapping)
                and isinstance(item.get("messageId"), str)
                and item["messageId"] not in known
                for item in rows if isinstance(rows, list)
            )

        def _known_ids(self) -> set[str]:
            """Ids the carrier already owns: live deliveries plus KEPT ones."""

            known = {
                state.delivery.delivery_id
                for state in self._carrier_runtime.snapshots()
            }
            # A batch claims every member, so a younger member must not look
            # "new" on the next poll just because only the head is the turn key.
            # The durable store is the truth here: a failed/removed delivery
            # drops its row, which is exactly when a re-capture may happen.
            for state in self._carrier_runtime.snapshots():
                known |= set(state.delivery.message_ids)
            if self._store is not None:
                known |= self._store.kept_ids(self.actor)
                known |= {
                    member
                    for stored in self._store.load(self.actor, self.session_ref)
                    for member in stored.delivery.message_ids
                }
            return known

        def _forget_gone_ids(self, response: Mapping[str, object]) -> None:
            """Drop KEPT and pre-upgrade FETCHED rows an authoritative list lacks.

            Only a successful list counts (``ok`` true and ``messages`` an
            array); a failed, malformed or incremental answer proves nothing.
            """

            self._sweep_settled_fetched()
            rows = response.get("messages")
            store = self._store
            if store is None or response.get("ok") is not True or not isinstance(rows, list):
                return
            listed = {
                item["messageId"]
                for item in rows
                if isinstance(item, Mapping) and isinstance(item.get("messageId"), str)
            }
            for message_id in store.kept_ids(self.actor) - listed:
                store.delete_in_state(self.actor, message_id, "KEPT")
            for message_id in store.legacy_fetched_ids(self.actor) - listed:
                store.delete_in_state(self.actor, message_id, "FETCHED")

        def _apply_inbox_list(
            self,
            change: InboxChange | None,
            response: Mapping[str, object],
        ) -> None:
            self._retry_carrier_facts()
            self._refill_staged_fetched()
            fetched_versions = {
                (state.delivery.delivery_id, state.generation, state.version)
                for state in self._carrier_runtime.snapshots()
                if state.stage == FETCHED
            }
            self._carrier_enqueued.intersection_update(fetched_versions)
            self._carrier_settled_pending.intersection_update(
                state.delivery.delivery_id
                for state in self._carrier_runtime.snapshots()
            )
            self._drain_carrier_effects()
            self._forget_gone_ids(response)
            known = self._known_ids()
            # Every listed message the carrier does not hold yet -- not only
            # the core's "new" ones: a row whose staging failed once must be
            # retried on the next list, as before.
            rows = response.get("messages", ())
            listed_ids = {
                item.get("messageId")
                for item in (rows if isinstance(rows, list) else ())
                if isinstance(item, Mapping) and isinstance(item.get("messageId"), str)
            }
            if listed_ids - known:
                # One daemon-rendered batch per injection edge.  The list above
                # was the doorbell; this is the sole consuming read, and its
                # delivery.text is injected byte for byte (no local renderer).
                batch = self.daemon_request("message.pending.batch", self._signed())
                items = batch.get("items")
                delivery_block = batch.get("delivery")
                cursor = batch.get("cursor")
                text = (
                    delivery_block.get("text")
                    if isinstance(delivery_block, Mapping)
                    else None
                )
                members = tuple(
                    item["messageId"]
                    for item in (items if isinstance(items, list) else [])
                    if isinstance(item, Mapping)
                    and isinstance(item.get("messageId"), str)
                )
                if isinstance(text, str) and text and isinstance(cursor, str) and members:
                    fresh = [member for member in members if member not in known]
                    first = next(
                        (
                            item
                            for item in (items if isinstance(items, list) else [])
                            if isinstance(item, Mapping)
                            and item.get("messageId") == (fresh[0] if fresh else members[0])
                        ),
                        {},
                    )
                    delivery = HarnessDelivery(
                        delivery_id=(fresh[0] if fresh else members[0]),
                        conversation_id=str(
                            first.get("conversationId")
                            or (fresh[0] if fresh else members[0])
                        ),
                        sender=str(first.get("from") or "unknown"),
                        recipient=self.actor,
                        message=text,
                        origin=(
                            first.get("origin")
                            if isinstance(first.get("origin"), dict)
                            else None
                        ),
                        batch_cursor=cursor,
                        message_ids=members,
                    )
                    try:
                        accepted = self._stage_carrier_fetched(
                            delivery,
                            str(first.get("intent") or "request"),
                        )
                    except Exception as error:  # noqa: BLE001 - isolate one poisoned edge
                        self._log(
                            "error",
                            "worker.carrier.error",
                            stage="carrier-admission",
                            messageId=delivery.delivery_id,
                            error=str(error) or type(error).__name__,
                        )
                        accepted = True
                    if not accepted:
                        self._log(
                            "error",
                            "worker.carrier.error",
                            stage="carrier-admission",
                            error="carrier actor deferred durable fetched fact",
                        )
    
            # Reconcile first: a completed authoritative thread/read must win over
            # a concurrent pump-side FAILED result caused by a transient RPC gap.
            self._reconcile_inflight_turns()
    
            for result in self._pump.drain_results():
                with self._carrier_io_lock:
                    if result.delivery_id in self._carrier_skipped_deliveries:
                        self._carrier_skipped_deliveries.discard(result.delivery_id)
                        continue
                state = self._carrier_runtime.snapshot(result.delivery_id)
                if state is None or state.stage == STATUS_UNKNOWN:
                    continue
                if result.delivery_id in self._carrier_settled_pending:
                    continue
                if result.status is HarnessResultStatus.COMPLETED:
                    self._submit_carrier_fact(
                        CarrierFinalObserved(
                            generation=state.generation,
                            delivery_id=result.delivery_id,
                            output=result.output,
                        )
                    )
                elif state.stage == FINAL_OBSERVED:
                    self._log(
                        "warn",
                        "worker.carrier.error",
                        state=state,
                        stage="turn-result-after-reconcile",
                        error=result.error or result.status.value,
                    )
                elif state.turn_id is not None:
                    # A pump-side failure is not authoritative once turn/start
                    # returned an id.  Keep the delivery pinned to that turn until
                    # thread/read proves either a final or a terminal turn status.
                    self._log(
                        "warn",
                        "worker.carrier.error",
                        state=state,
                        stage="turn-result-awaiting-reconcile",
                        error=result.error or result.status.value,
                    )
                else:
                    # The pump can report a terminal turn before the actor
                    # applies CarrierTurnStarted (mailbox lag on a loaded host).
                    # _on_turn_started already wrote the durable mapping before
                    # submitting that fact, so journal the ended report from it
                    # before removing the delivery.  The relay's created_ms
                    # order then keeps this turn ahead of the re-injection.
                    turn_id = (
                        self._store.persisted_turn_id(self.actor, result.delivery_id)
                        if self._store is not None
                        else None
                    )
                    self._log(
                        "error",
                        "worker.carrier.error",
                        state=state,
                        stage="turn-result",
                        error=result.error or result.status.value,
                        **({"turnId": turn_id} if turn_id is not None else {}),
                    )
                    if turn_id is not None:
                        self._report_turn_ended(turn_id, "failed", result.delivery_id)
                    self._submit_carrier_fact(
                        CarrierRemoved(
                            generation=state.generation,
                            delivery_id=result.delivery_id,
                        )
                    )
                    with self._carrier_io_lock:
                        self._carrier_clients.pop(result.delivery_id, None)
    
            self._settle_finals()
            self._drain_carrier_effects()

        def _now_ms(self) -> int:
            return int(self._heartbeat_clock() * 1000)

        def _on_turn_started(self, delivery: HarnessDelivery, client: object) -> None:
            if not isinstance(client, CodexInteractiveTurnClient) or client.delivery_skipped:
                return
            turn_id = client.turn_id
            if turn_id is None:
                self._log(
                    "error",
                    "worker.carrier.error",
                    stage="turn-start",
                    error="Codex turn started without a turn id",
                )
                return
            state = self._carrier_runtime.snapshot(delivery.delivery_id)
            # Persist at the exact native turn/start boundary before the callback
            # returns.  A process crash after this line can reconcile thread/read
            # and must never create a second native turn.
            if self._store is not None:
                self._store.record_turn_started(self.actor, delivery.delivery_id, turn_id)
                self._turn_reports.record(turn_id, "started", None, delivery.delivery_id)
            with self._carrier_io_lock:
                self._carrier_clients[delivery.delivery_id] = client
            generation = (
                state.generation
                if state is not None
                else self._carrier_runtime.generation()
            )
            self._submit_carrier_fact(
                CarrierTurnStarted(
                    generation=generation,
                    delivery_id=delivery.delivery_id,
                    turn_id=turn_id,
                    attempt=state.attempt if state is not None else 0,
                )
            )
