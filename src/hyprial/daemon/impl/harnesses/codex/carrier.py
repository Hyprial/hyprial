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
from hyprial.daemon.impl.harnesses.carrier.interactive_carrier_runtime  import (
    FETCHED,
    FINAL_OBSERVED,
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

    def __init__(self, server: CodexInteractiveAppServer) -> None:
        self.server = server
        self._turn_id: str | None = None
        self._observation_lock = threading.Lock()
        self._reconciled_final: tuple[str, str] | None = None

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

    async def query(self, prompt: str) -> None:
        if self._turn_id is not None:
            raise ConnectionError("Codex interactive app-server turn is busy")
        turn_id = await asyncio.to_thread(self.server.start_turn, prompt)
        with self._observation_lock:
            self._turn_id = turn_id
            self._reconciled_final = None

    async def receive_response(self) -> AsyncIterator[object]:
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
            self._thread = threading.Thread(
                target=self._poll_main,
                name=f"hyprial-codex-interactive-carrier-{session_ref[:8]}",
                daemon=True,
            )
            self._pump = StreamingTurnProcess(
                harness="codex",
                label="Codex interactive app-server",
                client_factory=lambda: CodexInteractiveTurnClient(server),
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

        @property
        def running(self) -> bool:
            return not self._stop.is_set() and self._pump.running

        @property
        def pid(self) -> int | None:
            return self.server.pid

        def start(self) -> None:
            self._thread.start()

        def stop(self) -> None:
            self._stop.set()
            try:
                self._pump.stop()
                self._thread.join(timeout=3.0)
            finally:
                if self._store is not None:
                    self._retry_carrier_facts()
                    self._carrier_runtime.drain(3.0)
                    self._drain_carrier_effects()
                    self._store.close()
                    self._store = None

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
                return self.daemon_request("session.refresh", self._signed())
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
            return not self._carrier_runtime.snapshots() and self._pump.idle

        def _list_may_hold(self, response: Mapping[str, object]) -> bool:
            if not self._inbox_hold_allowed():
                return False
            known = {
                state.delivery.delivery_id
                for state in self._carrier_runtime.snapshots()
            }
            rows = response.get("messages", ())
            return not any(
                isinstance(item, Mapping)
                and isinstance(item.get("messageId"), str)
                and item["messageId"] not in known
                for item in rows if isinstance(rows, list)
            )

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
            known = {
                state.delivery.delivery_id
                for state in self._carrier_runtime.snapshots()
            }
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
                fetched = self.daemon_request(
                    "message.pending.list",
                    self._signed({"fetched": True}),
                ).get("messages", [])
                for item in fetched if isinstance(fetched, list) else []:
                    if not isinstance(item, dict):
                        continue
                    message_id = item.get("messageId")
                    text = item.get("message")
                    if not isinstance(message_id, str) or not isinstance(text, str):
                        continue
                    if message_id in known:
                        # The fetched listing keeps returning a row until it is
                        # settled, so a delivery that already progressed past
                        # FETCHED reappears here on every poll that admits a new
                        # message.  Re-staging it trips the store's
                        # already-correlated guard, and that exception used to
                        # abort the whole poll iteration.
                        continue
                    delivery = HarnessDelivery(
                        delivery_id=message_id,
                        conversation_id=str(item.get("conversationId") or message_id),
                        sender=str(item.get("from") or "unknown"),
                        recipient=self.actor,
                        message=text,
                        origin=(
                            item.get("origin")
                            if isinstance(item.get("origin"), dict)
                            else None
                        ),
                    )
                    try:
                        accepted = self._stage_carrier_fetched(
                            delivery,
                            str(item.get("intent") or "request"),
                        )
                    except Exception as error:  # noqa: BLE001 - isolate one poisoned row
                        self._log(
                            "error",
                            "worker.carrier.error",
                            stage="carrier-admission",
                            messageId=message_id,
                            error=str(error) or type(error).__name__,
                        )
                        continue
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
                state = self._carrier_runtime.snapshot(result.delivery_id)
                if state is None:
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
                    self._log(
                        "error",
                        "worker.carrier.error",
                        state=state,
                        stage="turn-result",
                        error=result.error or result.status.value,
                    )
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
            if not isinstance(client, CodexInteractiveTurnClient):
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
                )
            )
