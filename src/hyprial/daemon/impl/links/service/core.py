"""``LinkService`` assembly, lifecycle, controller passes and shared helpers.

Internal module of ``hyprial.daemon.impl.links.service`` (spec §9.5); the public
import path stays ``hyprial.daemon.impl.links.service``.
"""

from __future__ import annotations

import random
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import hyprial.daemon.impl.links.controller as controller
import hyprial.daemon.impl.links.policy as policy
from hyprial.daemon.impl.links.controller import (
    DEFAULTS,
    ControllerConstants,
    ControllerInput,
    DwellSample,
    ReplySample,
    RttSample,
)
from hyprial.daemon.impl.links.store import (
    CensoredRecord,
    LedgerResult,
    LedgerRow,
    LedgerUpdate,
    LinkInboxPort,
    LinkPacPort,
    LinkPresencePort,
    LinkStore,
)
from hyprial.kernel import (
    HookEventPublisher,
    parse_agent_uri,
    safe_turn_identifier,
    uri_scheme,
)

from ._base import ACTIVE_WINDOW_MS, SAMPLE_CAP, TICK_INTERVAL_MS, _ActorState
from .gate import GateMixin
from .holds import HoldsMixin
from .reclaim import ReclaimMixin
from .views import ViewsMixin


class LinkService(GateMixin, HoldsMixin, ReclaimMixin, ViewsMixin):
    """Implements ``LinkGate``, ``LinkSendObserver``, ``LinkQueries``,
    ``LinkAdmin`` and ``LinkChangeSource``; duck-types inbox's
    ``LinkLedgerSink``."""

    # -- lifecycle ------------------------------------------------------

    def __init__(
        self,
        *,
        store: LinkStore,
        inbox: LinkInboxPort,
        presence: LinkPresencePort,
        pac: LinkPacPort | None,
        is_local_agent: Callable[[str], bool],
        local_agents: Callable[[], tuple[str, ...]],
        internal_actor_names: frozenset[str],
        publisher: HookEventPublisher | None,
        clock_ms: Callable[[], int],
        message_ttl_ms: int,
        constants: ControllerConstants = DEFAULTS,
        seed: int | None = None,
        tick_interval_ms: int = TICK_INTERVAL_MS,
        window_mode: policy.WindowMode = "observe",
        logger: Callable[..., None] | None = None,
    ) -> None:
        if window_mode not in ("observe", "enforce"):
            raise ValueError(f"unknown window mode: {window_mode!r}")
        self._store = store
        self._inbox = inbox
        self._presence = presence
        self._pac = pac
        self._is_local_agent = is_local_agent
        self._local_agents = local_agents
        self._internal_actor_names = frozenset(internal_actor_names)
        self._publisher = publisher
        self._clock_ms = clock_ms
        self._message_ttl_ms = int(message_ttl_ms)
        self._constants = constants
        self._seed = seed
        self._tick_interval_ms = tick_interval_ms
        self._window_mode: policy.WindowMode = window_mode
        self._logger = logger
        self._mem = threading.Lock()
        self._states: dict[str, _ActorState] = {}
        self._seen: dict[str, int] = {}
        self._observed: set[tuple[str, str, str]] = set()
        self._subscribers: list[Callable[[], None]] = []
        self._rng = random.Random(seed)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Synchronous: seed from durable facts, then run the thread."""

        now = self._clock_ms()
        self._store.refresh()
        self._refresh_seen(now)
        for actor in self._local_agents():
            self._state(actor)
        rows = self._inbox.ledger_snapshot(now)
        held = self._inbox.held_outbox()
        bindings = self._inbox.task_bindings()
        with self._mem:
            self._rebuild(rows, held, bindings)
        self.tick(now)
        thread = threading.Thread(target=self._loop, name="link-service", daemon=True)
        self._thread = thread
        thread.start()

    def close(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._store.close()
        return thread is None or not thread.is_alive()

    def kick(self) -> None:
        """Any thread; request a pass soon; never blocks."""

        self._wake.set()

    def tick(self, now_ms: int | None = None) -> None:
        """One pass; the thread calls it; tests call it directly (§3.2)."""

        now = self._clock_ms() if now_ms is None else int(now_ms)
        self._store.refresh()
        local = tuple(self._local_agents())
        self._ensure_local_files(local)
        self._apply_pending(local)
        self._store.flush()
        self._refresh_seen(now)
        rows = self._inbox.ledger_snapshot(now)
        held = self._inbox.held_outbox()
        bindings = self._inbox.task_bindings()
        with self._mem:
            self._merge_held(held)
            for actor in local:
                self._state(actor)
            self._merge_bindings(bindings)
            self._reconcile(rows, now)
        self._sync_references(local, now)
        control = self._run_controllers(local, rows, now)
        pac = self._pac_lifecycle(local, now)
        with self._mem:
            bindings_update = self._drain_bindings(local)
        holds = self._evaluate_holds(local, rows, now)
        with self._mem:
            opens = self._drain_pending_open(local)
            drains = self._drain_pending_drains(local)
        update = _merge_updates(
            control,
            pac,
            bindings_update,
            LedgerUpdate(open_outbound=opens, drain=drains),
            holds,
        )
        update = _merge_updates(
            update, LedgerUpdate(purge_before_ms=now - self._message_ttl_ms)
        )
        try:
            result = self._inbox.apply(update, now_ms=now)
        except Exception as error:  # noqa: BLE001 - retry the whole pass (B15)
            self._log("link.apply_failed", errorType=type(error).__name__)
            return
        with self._mem:
            self._merge_apply(local, update, result, now)
            self._retract_single_sided(local, now)
        self._apply_pending(local)
        self._store.flush()
        if _has_movement(result, update):
            self._notify()


    # -- internals: state -----------------------------------------------

    def _state(self, actor: str) -> _ActorState:
        state = self._states.get(actor)
        if state is None:
            state = _ActorState()
            self._states[actor] = state
        return state

    def _is_internal(self, uri: str) -> bool:
        parsed = parse_agent_uri(uri)
        return parsed is not None and parsed[2] in self._internal_actor_names


    # -- internals: controller pass -------------------------------------

    def _ensure_controller(self, st: _ActorState, now_ms: int) -> None:
        n_active = self._n_active(None, now_ms)
        if st.controller is None:
            st.controller = controller.initial_state(n_active, self._constants)
        if st.limits is None:
            st.controller, st.limits = controller.evaluate(
                st.controller,
                ControllerInput(
                    n_active=n_active,
                    dwell=(),
                    replies=(),
                    rtt=(),
                    links=frozenset(),
                    unanswered={},
                    held_peers=st.held_cwnd_peers,
                ),
                now_ms,
                0.0,
                self._constants,
            )

    def _n_active(self, actor: str | None, now_ms: int) -> int:
        return sum(
            1
            for agent, seen in self._seen.items()
            if agent != actor and now_ms - seen <= ACTIVE_WINDOW_MS
        )

    def _refresh_seen(self, now_ms: int) -> None:
        # P-4: ``_seen`` is read by ``_n_active`` on inbox-actor/IPC threads.
        # Update it under ``_mem`` so a concurrent ``admit_dispatch`` never
        # iterates a dict that changes size.
        online = tuple(self._presence.online_agents())
        with self._mem:
            for agent in online:
                if uri_scheme(agent) == "agent":
                    self._seen[agent] = now_ms
            horizon = ACTIVE_WINDOW_MS * 2
            for agent in [
                item for item, seen in self._seen.items() if now_ms - seen > horizon
            ]:
                del self._seen[agent]

    def _run_controllers(
        self, local: Sequence[str], rows: Sequence[LedgerRow], now_ms: int
    ) -> LedgerUpdate:
        release_outbound: list[tuple[str, str]] = []
        censors: list[CensoredRecord] = []
        sampled: list[tuple[str, str]] = []
        for actor in local:
            with self._mem:
                st = self._state(actor)
                self._ensure_controller(st, now_ms)
                view = self._view(actor, now_ms)
                before_k = view.limits.k
                state, output = controller.evaluate(
                    st.controller,  # type: ignore[arg-type]
                    ControllerInput(
                        n_active=self._n_active(actor, now_ms),
                        dwell=tuple(st.dwell),
                        replies=tuple(st.replies),
                        rtt=tuple(st.rtt),
                        links=frozenset(st.established),
                        unanswered=_unanswered(st),
                        held_peers=st.held_cwnd_peers,
                    ),
                    now_ms,
                    self._rng.uniform(-1, 1),
                    self._constants,
                )
                st.controller = state
                st.limits = output
                if output.k < before_k:
                    # ``shrink_to_k`` reads the new topology budget.
                    view = self._view(actor, now_ms)
                    shrink = policy.shrink_to_k(view, now_ms=now_ms)
                else:
                    shrink = ()
                for message_id in policy.abandoned(view, now_ms=now_ms):
                    if message_id in st.released_outbound:
                        continue
                    peer = _peer_of(view, message_id)
                    threshold = policy.request_threshold_ms(view, peer)
                    if threshold is None:
                        continue
                    st.rtt.append(
                        RttSample(
                            peer=peer,
                            at_ms=now_ms,
                            rtt_ms=threshold,
                            censored=True,
                        )
                    )
                    st.released_outbound.add(message_id)
                    st.outbound = [
                        item for item in st.outbound if item.message_id != message_id
                    ]
                    release_outbound.append((actor, message_id))
                fresh_censors = [
                    (message_id, threshold)
                    for message_id, threshold in policy.censored_inbound(
                        view, now_ms=now_ms
                    ).items()
                    if message_id not in st.censored_inbound
                ]
                pending = _requester_snapshot(view, [item[0] for item in fresh_censors])
                for message_id, threshold in fresh_censors:
                    requester, received_at = pending[message_id]
                    record = CensoredRecord(
                        actor=actor,
                        message_id=message_id,
                        requester=requester,
                        received_at_ms=received_at,
                        threshold_ms=threshold,
                        expires_at_ms=now_ms + self._message_ttl_ms,
                    )
                    st.censored_inbound[message_id] = record
                    st.dwell.append(
                        DwellSample(at_ms=now_ms, dwell_ms=threshold, censored=True)
                    )
                    st.pending_inbound.pop(message_id, None)
                    st.admitted.discard(message_id)
                    censors.append(record)
            for peer in shrink:
                self._reclaim(actor, peer, "k_shrink", now_ms)
            sampled.extend(self._sampling_writeback(actor, rows, now_ms))
        return LedgerUpdate(
            release_outbound=tuple(release_outbound),
            censor=tuple(censors),
            sampled=tuple(sampled),
        )

    def _sampling_writeback(
        self, actor: str, rows: Sequence[LedgerRow], now_ms: int
    ) -> list[tuple[str, str]]:
        writeback: list[tuple[str, str]] = []
        for row in rows:
            if row.actor != actor or not row.sample_pending:
                continue
            with self._mem:
                st = self._state(actor)
                if row.direction == "out" and row.state == "closed":
                    match = next(
                        (
                            item
                            for item in st.outbound
                            if item.message_id == row.message_id
                        ),
                        None,
                    )
                    if match is not None:
                        st.outbound.remove(match)
                        sent = (
                            row.sent_at_ms
                            if row.sent_at_ms is not None
                            else row.opened_at_ms
                        )
                        event = (
                            row.event_at_ms
                            if row.event_at_ms is not None
                            else now_ms
                        )
                        st.rtt.append(
                            RttSample(
                                peer=row.peer,
                                at_ms=event,
                                rtt_ms=max(1, event - sent),
                            )
                        )
                        st.replies.append(
                            ReplySample(
                                at_ms=event, peer=row.peer, direction="received"
                            )
                        )
                st.sampled_writeback.add(row.message_id)
                writeback.append((actor, row.message_id))
        return writeback

    def _trim_samples(self) -> None:
        for state in self._states.values():
            if len(state.dwell) > SAMPLE_CAP:
                for _ in range(len(state.dwell) - SAMPLE_CAP):
                    state.dwell.popleft()
            if len(state.replies) > SAMPLE_CAP:
                for _ in range(len(state.replies) - SAMPLE_CAP):
                    state.replies.popleft()
            if len(state.rtt) > SAMPLE_CAP:
                per_peer: dict[str, deque[RttSample]] = {}
                for sample in state.rtt:
                    per_peer.setdefault(sample.peer, deque()).append(sample)
                kept: list[RttSample] = []
                for queue in per_peer.values():
                    kept.extend(list(queue)[-SAMPLE_CAP:])
                state.rtt = deque(sorted(kept, key=lambda item: (item.at_ms, item.peer)))

    def _pac_lifecycle(self, local: Sequence[str], now_ms: int) -> LedgerUpdate:
        if self._pac is None:
            return LedgerUpdate()
        task_ids: set[str] = set()
        with self._mem:
            for actor in local:
                st = self._states.get(actor)
                if st is None:
                    continue
                for tasks in st.task_bindings.values():
                    task_ids.update(tasks)
        if not task_ids:
            return LedgerUpdate()
        states = self._pac.task_states(frozenset(task_ids))
        unbind: list[tuple[str, str, str]] = []
        for actor in local:
            with self._mem:
                st = self._states.get(actor)
                targets = sorted(st.task_bindings) if st is not None else []
            for peer in targets:
                with self._mem:
                    state = self._states.get(actor)
                    tasks = sorted(state.task_bindings.get(peer, ())) if state else []
                for task_id in tasks:
                    task_state = states.get(task_id, "unknown")
                    if task_state == "ended":
                        unbind.append((actor, peer, task_id))
                    elif task_state == "active" and (
                        self._pac.worker_active(task_id, actor) is False
                    ):
                        unbind.append((actor, peer, task_id))
        reclaimed: set[tuple[str, str]] = set()
        for actor, peer, task_id in unbind:
            with self._mem:
                st = self._state(actor)
                st.task_bindings.get(peer, set()).discard(task_id)
                remaining = not st.task_bindings.get(peer)
            if remaining and self._entry_lifecycle(actor, peer) == "pac_task":
                if (actor, peer) not in reclaimed:
                    reclaimed.add((actor, peer))
                    self._reclaim(actor, peer, "task_ended", now_ms)
        return LedgerUpdate(unbind_tasks=tuple(unbind))

    def _entry_lifecycle(self, actor: str, peer: str) -> str | None:
        for file in (
            self._store.intents().get(actor),
            self._store.index().files.get(actor),
        ):
            if file is None:
                continue
            entry = file.entries.get(peer)
            if entry is not None:
                return entry.lifecycle
        return None


    # -- internals: events ----------------------------------------------

    def _emit(
        self, event: str, actor: str, now_ms: int, payload: Mapping[str, Any]
    ) -> None:
        if self._publisher is None:
            return
        clean = {key: _clean(value) for key, value in payload.items()}
        try:
            self._publisher.publish(
                event, payload=clean, emitted_at_ms=now_ms, actor=actor
            )
        except Exception as error:  # noqa: BLE001 - events are never authority
            self._log("link.event_failed", errorType=type(error).__name__)

    def _notify(self) -> None:
        for callback in list(self._subscribers):
            try:
                callback()
            except Exception as error:  # noqa: BLE001 - a subscriber cannot break us
                self._log("link.subscriber_failed", errorType=type(error).__name__)


    # -- thread ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._tick_interval_ms / 1000.0)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 - one pass must not kill us
                self._log("link.tick_failed", errorType=type(error).__name__)

    @property
    def window_mode(self) -> policy.WindowMode:
        """The H1 switch this service was composed with (never changes)."""

        return self._window_mode

    def _window_observed(
        self, actor: str, peer: str, bound: str, site: str, *, log: bool = True
    ) -> None:
        """Count one skipped W/cwnd bound, and log it (H1, observe only).

        Called with ``_mem`` released: the count is taken under the lock and
        the ``link.window_observed`` write happens outside it, so no caller
        blocks on a log write.  ``log=False`` is for the inbox actor thread,
        where the ``LinkGate`` contract forbids I/O: that path still counts.
        """

        with self._mem:
            state = self._state(actor)
            state.window_observed += 1
            count = state.window_observed
        if not log:
            return
        self._log(
            "link.window_observed",
            actor=actor,
            peer=peer,
            bound=bound,
            site=site,
            observedCount=count,
        )

    def _log(self, event: str, **fields: object) -> None:
        if self._logger is None:
            return
        try:
            self._logger("info", "daemon", event, **fields)
        except Exception:  # noqa: BLE001 - logging never breaks the service
            pass




def _unanswered(state: _ActorState) -> dict[str, int]:
    oldest: dict[str, int] = {}
    for item in state.outbound:
        current = oldest.get(item.peer)
        if current is None or item.sent_at_ms < current:
            oldest[item.peer] = item.sent_at_ms
    return oldest

def _peer_of(view: policy.NodeView, message_id: str) -> str:
    for item in view.outbound:
        if item.message_id == message_id:
            return item.peer
    return ""

def _requester_snapshot(
    view: policy.NodeView, message_ids: Sequence[str]
) -> dict[str, tuple[str, int]]:
    snapshot: dict[str, tuple[str, int]] = {}
    for message_id in message_ids:
        request = view.pending_inbound.get(message_id)
        if request is not None:
            snapshot[message_id] = (request.requester, request.received_at_ms)
    return snapshot

def _clean(value: Any) -> Any:
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        return safe_turn_identifier(value)
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _clean(item) for key, item in value.items()}
    return safe_turn_identifier(str(value))

def _has_movement(result: LedgerResult, update: LedgerUpdate) -> bool:
    return bool(
        result.released
        or result.failed
        or result.expired
        or result.drained
        or result.cancelled
        or update.reasons
        or update.censor
        or update.release_outbound
        or update.cancel_outbound
        or update.drain
    )

def _merge_updates(*updates: LedgerUpdate) -> LedgerUpdate:
    release: list[str] = []
    reasons: dict[str, str] = {}
    fail: dict[str, str] = {}
    drain: dict[str, tuple[str, ...]] = {}
    cancel: dict[str, str] = {}
    release_outbound: list[tuple[str, str]] = []
    censor: list[CensoredRecord] = []
    sampled: list[tuple[str, str]] = []
    open_outbound: list[LedgerRow] = []
    close_inbound: list[tuple[str, str, int]] = []
    bind_tasks: list[tuple[str, str, str, int]] = []
    unbind_tasks: list[tuple[str, str, str]] = []
    purge: int | None = None
    for update in updates:
        release.extend(update.release)
        reasons.update(update.reasons)
        fail.update(update.fail)
        for actor, ids in update.drain.items():
            drain[actor] = tuple(sorted({*drain.get(actor, ()), *ids}))
        cancel.update(update.cancel_outbound)
        release_outbound.extend(update.release_outbound)
        censor.extend(update.censor)
        sampled.extend(update.sampled)
        open_outbound.extend(update.open_outbound)
        close_inbound.extend(update.close_inbound)
        bind_tasks.extend(update.bind_tasks)
        unbind_tasks.extend(update.unbind_tasks)
        if update.purge_before_ms is not None:
            purge = (
                update.purge_before_ms
                if purge is None
                else min(purge, update.purge_before_ms)
            )
    return LedgerUpdate(
        release=tuple(dict.fromkeys(release)),
        reasons=reasons,
        fail=fail,
        drain=drain,
        cancel_outbound=cancel,
        release_outbound=tuple(release_outbound),
        censor=tuple(censor),
        sampled=tuple(sampled),
        open_outbound=tuple(open_outbound),
        close_inbound=tuple(close_inbound),
        bind_tasks=tuple(bind_tasks),
        unbind_tasks=tuple(unbind_tasks),
        purge_before_ms=purge,
    )
