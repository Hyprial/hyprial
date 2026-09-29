"""Synchronous Zenoh adapter implementing the public transport protocol."""

from __future__ import annotations

import itertools
import json
import logging
import math
import os
import queue
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, Self

import zenoh

from hyprial.actor_runtime import AdmissionResult

from .api import TransportSample
from .keys import KeySpace
from .presence_actor import (
    ActorOnlineTransition,
    PresenceAuthority,
    PresenceProjection,
)
from .query_actor import QueryIoOwner

logger = logging.getLogger(__name__)

DEFAULT_LEASE_MS = 6_000
DEFAULT_KEEP_ALIVE = 4
DEFAULT_CONNECT_RETRY_INITIAL_MS = 500
DEFAULT_CONNECT_RETRY_MAX_MS = 8_000
DEFAULT_CONNECT_RETRY_MULTIPLIER = 2.0
# How long a publisher or declaration waits for the transport lock (or for a
# session rebuild to finish) before failing loudly with the holder's name and
# stack.  Measured rebuilds on hq took 0.7-9.2 s (zenoh.forwarding.redialed,
# 2026-09-26/27); 30 s leaves room for the slowest without hiding a wedge.
DEFAULT_LOCK_WAIT_SECONDS = 30.0
# A single session.put slower than this is logged: with congestion control
# DROP (zenoh's default) a put should never take seconds.
DEFAULT_PUT_SLOW_SECONDS = 5.0
# Samples waiting for a subscriber callback, per hand-off lane.
DEFAULT_CALLBACK_QUEUE_CAPACITY = 4096
CALLBACK_LANES = 4
# Queries waiting for one of the shared query workers.  The count is global to
# one transport, not one thread (or queue) per queryable declaration.
DEFAULT_QUERY_QUEUE_CAPACITY = 256
QUERY_WORKERS = 4
OVERFLOW_LOG_EVERY = 100


class TransportLockTimeout(RuntimeError):
    """A bounded wait for the transport lock ran out; names the holder."""


def _thread_stack(ident: int | None) -> str:
    frame = sys._current_frames().get(ident) if ident is not None else None
    if frame is None:
        return "<no Python frame>"
    return "".join(traceback.format_stack(frame))


class _OwnedLock:
    """An RLock that remembers which thread holds it and since when.

    The 2026-09-27 stall could not answer "who holds the lock": faulthandler
    stops at 100 threads and py-spy needs root on macOS.  Recording the owner
    on first acquire makes the answer one attribute read away, and the
    ``_release_save``/``_acquire_restore`` pair keeps that record correct
    while a ``threading.Condition`` waits on this lock.
    """

    def __init__(self) -> None:
        self._inner = threading.RLock()
        self._depth = 0
        self._owner: tuple[int | None, str, float] | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._inner.acquire(blocking, timeout)
        if acquired:
            self._depth += 1
            if self._depth == 1:
                self._claim()
        return acquired

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
        self._inner.release()

    __enter__ = acquire

    def __exit__(self, *_: object) -> None:
        self.release()

    def _at_fork_reinit(self) -> None:
        self._inner._at_fork_reinit()  # type: ignore[attr-defined]
        self._depth = 0
        self._owner = None

    def _claim(self) -> None:
        current = threading.current_thread()
        self._owner = (current.ident, current.name, time.monotonic())

    def locked(self) -> bool:
        # Python 3.14's threading.Condition binds ``lock.locked`` at construction;
        # without it the daemon fails at startup.  RLock.locked() is 3.14-only.
        return self._depth > 0

    # Condition protocol: a wait fully releases a reentrant hold and restores it.
    def _is_owned(self) -> bool:
        return self._inner._is_owned()  # type: ignore[attr-defined]

    def _release_save(self) -> Any:
        depth = self._depth
        self._depth = 0
        self._owner = None
        return self._inner._release_save(), depth  # type: ignore[attr-defined]

    def _acquire_restore(self, state: Any) -> None:
        inner_state, depth = state
        self._inner._acquire_restore(inner_state)  # type: ignore[attr-defined]
        self._depth = depth
        self._claim()

    def owner(self) -> tuple[int | None, str, float] | None:
        return self._owner


class _CallbackDispatcher:
    """Runs subscriber callbacks off the thread zenoh delivers on.

    zenoh-python's default indirect callback runs one ``pyo3-closure`` thread
    per declaration, parked in an unbounded Rust receive.  Direct callbacks
    avoid that receive and do only a non-blocking hand-off here.  Each
    subscription is pinned to one lane, preserving its order without making
    thread count scale with declaration count.
    """

    def __init__(self, capacity: int, lanes: int = CALLBACK_LANES) -> None:
        self._capacity = capacity
        self._lanes: list[queue.Queue[Any]] = [
            queue.Queue(maxsize=capacity) for _ in range(lanes)
        ]
        self._threads: list[threading.Thread | None] = [None] * lanes
        self._next_lane = itertools.count()
        self._dropped: dict[str, int] = {}
        self._guard = threading.Lock()
        self._stopped = False

    def wrap(
        self, key_expr: str, callback: Callable[[TransportSample], None]
    ) -> Callable[[Any], None]:
        index = next(self._next_lane) % len(self._lanes)
        lane = self._lanes[index]

        def deliver(sample: Any) -> None:
            if self._stopped:
                return
            self._ensure_worker(index)
            try:
                lane.put_nowait((key_expr, callback, _sample(sample)))
            except queue.Full:
                self._overflow(key_expr)

        return deliver

    def _ensure_worker(self, index: int) -> None:
        if self._threads[index] is not None:
            return
        with self._guard:
            if self._threads[index] is None:
                worker = threading.Thread(
                    target=self._drain,
                    args=(self._lanes[index],),
                    name=f"hyprial-zenoh-callback-{index}",
                    daemon=True,
                )
                self._threads[index] = worker
                worker.start()

    def _drain(self, lane: queue.Queue[Any]) -> None:
        while True:
            item = lane.get()
            if item is None:
                return
            key_expr, callback, sample = item
            try:
                callback(sample)
            except Exception:  # noqa: BLE001 - one bad callback must not stop the lane
                logger.exception(
                    "zenoh subscriber callback failed for %s",
                    key_expr,
                    extra={"event": "zenoh.subscriber.callback_failed"},
                )

    def _overflow(self, key_expr: str) -> None:
        with self._guard:
            dropped = self._dropped.get(key_expr, 0) + 1
            self._dropped[key_expr] = dropped
        if dropped == 1 or dropped % OVERFLOW_LOG_EVERY == 0:
            logger.error(
                "zenoh.subscriber.overflow key_expr=%s dropped=%d capacity=%d: "
                "the callback is not keeping up; samples are being dropped",
                key_expr,
                dropped,
                self._capacity,
                extra={"event": "zenoh.subscriber.overflow"},
            )

    def stop(self) -> None:
        self._stopped = True
        for index, lane in enumerate(self._lanes):
            if self._threads[index] is None:
                continue
            while True:
                try:
                    lane.put_nowait(None)
                    break
                except queue.Full:
                    # Stop owns the queue now (``_stopped`` rejects new
                    # deliveries).  Drop queued callbacks until the sentinel
                    # fits so a once-full lane cannot park forever in get().
                    try:
                        lane.get_nowait()
                    except queue.Empty:
                        continue


@dataclass(frozen=True, slots=True)
class _QueryJob:
    key_expr: str
    callback: Callable[[Any], None]
    payload: Any
    query: Any
    retire: Callable[[str], None] | None = None
    token: str = ""


@dataclass(frozen=True, slots=True)
class _QueryCleanup:
    job: _QueryJob
    dropped: bool = False


def _drop_query(query: Any, key_expr: str) -> bool:
    """A failed native drop must not end a shared query worker."""

    try:
        query.drop()
        return True
    except Exception:  # noqa: BLE001 - preserve the remaining pool workers
        logger.exception(
            "zenoh.queryable.drop_failed key_expr=%s: a query could not be finalized",
            key_expr,
            extra={"event": "zenoh.queryable.drop_failed"},
        )
        return False


class _QueryDispatcher:
    """One bounded native-query effect pool shared by all query declarations.

    The queue plus active workers own at most ``capacity + workers`` queries.
    Direct Zenoh callbacks only reserve and enqueue; accepted native handles
    remain pinned until the worker replies, drops and retires their token.
    """

    def __init__(self, capacity: int, workers: int = QUERY_WORKERS) -> None:
        if capacity < 1 or workers < 1:
            raise ValueError("query dispatcher capacity and worker count must be positive")
        self._capacity = capacity
        self._worker_count = workers
        self._limit = capacity + workers
        self._queue: queue.Queue[_QueryJob | None] = queue.Queue(maxsize=self._limit)
        self._threads: list[threading.Thread] = []
        self._dropped: dict[str, int] = {}
        self._guard = threading.Condition()
        self._cleanup_retry_lock = threading.Lock()
        self._outstanding = 0
        self._cleanup_pending: dict[int, _QueryCleanup] = {}
        self._paused = False
        self._stopped = False
        self._sentinels_sent = False

    def wrap(self, key_expr: str, callback: Callable[[Any], None]) -> Callable[[Any], None]:
        def deliver(query: Any) -> None:
            self.submit(key_expr, callback, query, query)

        return deliver

    @staticmethod
    def reject(key_expr: str, query: Any) -> None:
        _drop_query(query, key_expr)

    def submit(
        self,
        key_expr: str,
        callback: Callable[[Any], None],
        payload: Any,
        query: Any,
        *,
        retire: Callable[[str], None] | None = None,
        token: str = "",
    ) -> AdmissionResult:
        dropped = 0
        with self._guard:
            if self._stopped:
                admission = AdmissionResult.CLOSED
            elif self._paused or self._outstanding >= self._limit:
                dropped = self._dropped.get(key_expr, 0) + 1
                self._dropped[key_expr] = dropped
                admission = AdmissionResult.OVERLOADED
            else:
                self._ensure_workers()
                self._outstanding += 1
                self._queue.put_nowait(_QueryJob(key_expr, callback, payload, query, retire, token))
                return AdmissionResult.ACCEPTED
        _drop_query(query, key_expr)
        self._retire(retire, token, key_expr)
        if dropped == 1 or (dropped and dropped % OVERFLOW_LOG_EVERY == 0):
            logger.error(
                "zenoh.queryable.overflow key_expr=%s dropped=%d capacity=%d: "
                "the query handlers are not keeping up; query was dropped",
                key_expr, dropped, self._capacity,
                extra={"event": "zenoh.queryable.overflow"},
            )
        return admission

    def _ensure_workers(self) -> None:
        if self._threads:
            return
        for index in range(self._worker_count):
            worker = threading.Thread(
                target=self._drain, name=f"hyprial-zenoh-query-{index}", daemon=True
            )
            self._threads.append(worker)
            worker.start()

    @staticmethod
    def _retire(retire: Callable[[str], None] | None, token: str, key_expr: str) -> bool:
        if retire is None:
            return True
        try:
            retire(token)
            return True
        except Exception:  # noqa: BLE001 - do not lose a shared worker on retirement
            logger.exception(
                "zenoh.queryable.retire_failed key_expr=%s", key_expr,
                extra={"event": "zenoh.queryable.retire_failed"},
            )
            return False

    def _finish(self, job: _QueryJob) -> None:
        dropped = _drop_query(job.query, job.key_expr)
        retired = dropped and self._retire(job.retire, job.token, job.key_expr)
        with self._guard:
            if retired:
                self._outstanding -= 1
            else:
                self._cleanup_pending[id(job)] = _QueryCleanup(job, dropped)
            self._guard.notify_all()

    def retry_cleanup(self) -> bool:
        """Try each retained native cleanup once; never rerun its handler."""

        # A close retry and a worker's explicit caller may arrive together.
        # Claim the whole pass, while native cleanup stays outside the global
        # admission condition so direct Zenoh callbacks remain nonblocking.
        with self._cleanup_retry_lock:
            with self._guard:
                pending = tuple(self._cleanup_pending.items())
            for key, state in pending:
                dropped = state.dropped or _drop_query(state.job.query, state.job.key_expr)
                retired = dropped and self._retire(
                    state.job.retire, state.job.token, state.job.key_expr
                )
                with self._guard:
                    if self._cleanup_pending.get(key) is not state:
                        continue
                    if retired:
                        self._cleanup_pending.pop(key)
                        self._outstanding -= 1
                    elif dropped and not state.dropped:
                        self._cleanup_pending[key] = _QueryCleanup(state.job, True)
                    self._guard.notify_all()
            with self._guard:
                return not self._cleanup_pending

    def _drain(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                job.callback(job.payload)
            except Exception:  # noqa: BLE001 - isolate one query from the pool
                logger.exception(
                    "zenoh queryable callback failed for %s", job.key_expr,
                    extra={"event": "zenoh.queryable.callback_failed"},
                )
            finally:
                self._finish(job)

    def quiesce(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            if self._stopped:
                return False
            self._paused = True
            while self._outstanding:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._guard.wait(remaining)
            return True

    def resume(self) -> None:
        with self._guard:
            if not self._stopped:
                self._paused = False
                self._guard.notify_all()

    def stop(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._stopped = True
            self._paused = True
            threads = tuple(self._threads)
            send_sentinels = not self._sentinels_sent
            self._sentinels_sent = True
        self.retry_cleanup()
        if send_sentinels:
            while True:
                try:
                    job = self._queue.get_nowait()
                except queue.Empty:
                    break
                if job is not None:
                    self._finish(job)
            for _ in threads:
                self._queue.put_nowait(None)
        current = threading.current_thread()
        for thread in threads:
            if thread is not current:
                thread.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        with self._guard:
            return all(not thread.is_alive() for thread in threads) and not self._cleanup_pending


def _environment_integer(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def environment_flag(name: str, default: bool = False) -> bool:
    """A switch, fail-loud like its integer/float siblings.

    Unset means ``default``.  An unrecognised value raises rather than
    quietly falling back: a typo would otherwise leave the operator believing
    they had flipped something they had not -- and in the direction that
    matters, believing a thing is ON when it is off.
    """

    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean (1/0, true/false, yes/no, on/off)")


def _environment_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error


def _validate_endpoints(endpoints: tuple[str, ...]) -> None:
    """Refuse a malformed locator before anything live is touched."""

    for endpoint in endpoints:
        protocol, separator, address = endpoint.partition("/")
        host, colon, port = address.rpartition(":")
        if not separator or not protocol or not colon or not host or not port.isdigit():
            raise ValueError(f"not a zenoh endpoint: {endpoint!r}")


@dataclass(frozen=True, slots=True)
class ZenohConfig:
    listen: tuple[str, ...] = ()
    connect: tuple[str, ...] = ()
    mode: str = "peer"
    # Discovery is off by default: a node reaches exactly the endpoints it was
    # told about.  Gossip lets peers learn about each other THROUGH a common
    # peer, which is what makes a hub-and-spoke deployment into a mesh.
    gossip_scouting: bool = False
    multicast_scouting: bool = False
    shared_memory: bool = False
    lease_ms: int = DEFAULT_LEASE_MS
    keep_alive: int = DEFAULT_KEEP_ALIVE
    connect_retry_initial_ms: int = DEFAULT_CONNECT_RETRY_INITIAL_MS
    connect_retry_max_ms: int = DEFAULT_CONNECT_RETRY_MAX_MS
    connect_retry_multiplier: float = DEFAULT_CONNECT_RETRY_MULTIPLIER

    def __post_init__(self) -> None:
        if not isinstance(self.lease_ms, int) or self.lease_ms <= 0:
            raise ValueError("lease_ms must be a positive integer")
        if not isinstance(self.keep_alive, int) or self.keep_alive <= 0:
            raise ValueError("keep_alive must be a positive integer")
        if (
            not isinstance(self.connect_retry_initial_ms, int)
            or self.connect_retry_initial_ms <= 0
        ):
            raise ValueError("connect_retry_initial_ms must be a positive integer")
        if (
            not isinstance(self.connect_retry_max_ms, int)
            or self.connect_retry_max_ms < self.connect_retry_initial_ms
        ):
            raise ValueError(
                "connect_retry_max_ms must be an integer greater than or equal to "
                "connect_retry_initial_ms"
            )
        if (
            not isinstance(self.connect_retry_multiplier, (int, float))
            or not math.isfinite(self.connect_retry_multiplier)
            or self.connect_retry_multiplier < 1
        ):
            raise ValueError("connect_retry_multiplier must be a finite number >= 1")

    @classmethod
    def from_environment(
        cls,
        *,
        listen: tuple[str, ...] = (),
        connect: tuple[str, ...] = (),
        mode: str = "peer",
        gossip_scouting: bool = False,
        multicast_scouting: bool = False,
        shared_memory: bool = False,
    ) -> Self:
        """Build a session config with fail-loud operational overrides."""

        return cls(
            listen=listen,
            connect=connect,
            mode=mode,
            gossip_scouting=gossip_scouting,
            multicast_scouting=multicast_scouting,
            shared_memory=shared_memory,
            lease_ms=_environment_integer("HYPRIAL_ZENOH_LEASE_MS", DEFAULT_LEASE_MS),
            keep_alive=_environment_integer("HYPRIAL_ZENOH_KEEP_ALIVE", DEFAULT_KEEP_ALIVE),
            connect_retry_initial_ms=_environment_integer(
                "HYPRIAL_ZENOH_CONNECT_RETRY_INITIAL_MS",
                DEFAULT_CONNECT_RETRY_INITIAL_MS,
            ),
            connect_retry_max_ms=_environment_integer(
                "HYPRIAL_ZENOH_CONNECT_RETRY_MAX_MS", DEFAULT_CONNECT_RETRY_MAX_MS
            ),
            connect_retry_multiplier=_environment_float(
                "HYPRIAL_ZENOH_CONNECT_RETRY_MULTIPLIER",
                DEFAULT_CONNECT_RETRY_MULTIPLIER,
            ),
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "mode": self.mode,
                "listen": {"endpoints": list(self.listen)},
                "connect": {
                    "endpoints": list(self.connect),
                    # A configured peer endpoint is a desired link, not a
                    # one-shot startup attempt.  Zenoh's closed_link callback
                    # reuses this retry policy after a lease expiry.
                    "exit_on_failure": False,
                    "retry": {
                        "period_init_ms": self.connect_retry_initial_ms,
                        "period_max_ms": self.connect_retry_max_ms,
                        "period_increase_factor": self.connect_retry_multiplier,
                    },
                },
                "scouting": {
                    "multicast": {"enabled": self.multicast_scouting},
                    "gossip": {"enabled": self.gossip_scouting},
                },
                "transport": {
                    "link": {
                        "tx": {
                            # Each side advertises its lease to the other.
                            # With four keepalives this yields one frame every
                            # lease/4 when the link is otherwise idle.
                            "lease": self.lease_ms,
                            "keep_alive": self.keep_alive,
                        }
                    },
                    "shared_memory": {"enabled": self.shared_memory},
                },
            },
            separators=(",", ":"),
        )

    def build(self) -> zenoh.Config:
        return zenoh.Config.from_json5(self.to_json())


class _Registration:
    """A caller's handle on one declaration; survives a session rebuild.

    ``declare`` re-creates the declaration on a new session, so the caller's
    handle keeps working after ``ZenohTransport.reconfigure_connect`` without
    the caller knowing a rebuild happened.  A closed handle is forgotten by its
    transport and never replayed.
    """

    def __init__(
        self,
        inner: Any,
        declare: Callable[[Any], Any] | None = None,
        on_close: Callable[[_Registration], None] | None = None,
    ) -> None:
        self._inner = inner
        self._closed = False
        self._declare = declare
        self._on_close = on_close

    def close(self) -> None:
        if self._closed:
            return
        undeclare = getattr(self._inner, "undeclare", None)
        if undeclare is not None:
            undeclare()
        # A failed native undeclare keeps this declaration and its session
        # available for an exact close retry; forgetting it first loses owner
        # custody while the native queryable may still accept callbacks.
        self._closed = True
        if self._on_close is not None:
            self._on_close(self)

    def _replay(self, session: Any) -> None:
        # The old handle died with the old session; never undeclare it.
        if not self._closed and self._declare is not None:
            self._inner = self._declare(session)


def _sample(sample: Any) -> TransportSample:
    key = str(sample.key_expr)
    payload = sample.payload.to_bytes() if hasattr(sample, "payload") else b""
    kind = str(getattr(sample, "kind", "put")).lower()
    if "." in kind:
        kind = kind.rsplit(".", 1)[-1]
    return TransportSample(key=key, payload=payload, kind=kind)


class _QueryRegistration:
    def __init__(self, inner, owner, forget):
        self._inner = inner
        self._owner = owner
        self._forget = forget
        self._guard = threading.Lock()
        self._closed = False

    def close(self, timeout: float = 5.0):
        with self._guard:
            if self._closed:
                return
            # The owner rejects and drops any racing native callback, while
            # exact accepted cells stay pinned until reply/drop completion.
            if not self._owner.close(timeout):
                raise TimeoutError("native query effects did not drain")
            self._inner.close()
            self._forget(self._owner)
            self._closed = True


def _reply_error(reply: Any) -> str:
    """Describe an error reply without letting the description raise.

    Runs on the query path of a best-effort read, so a surprising payload type
    must degrade to a repr rather than take out the whole pull.
    """

    failure = getattr(reply, "err", None)
    if failure is None:
        return "reply carried neither a sample nor an error"
    payload = getattr(failure, "payload", None)
    if payload is None:
        return str(failure)
    try:
        return str(payload.to_string())
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return repr(payload)


class ZenohTransport:
    def __init__(self, config: ZenohConfig | None = None) -> None:
        self._config = config or ZenohConfig()
        self._lock_wait = _environment_float(
            "HYPRIAL_ZENOH_LOCK_WAIT_SECONDS", DEFAULT_LOCK_WAIT_SECONDS
        )
        self._put_slow = _environment_float(
            "HYPRIAL_ZENOH_PUT_SLOW_SECONDS", DEFAULT_PUT_SLOW_SECONDS
        )
        self._dispatcher = _CallbackDispatcher(
            _environment_integer(
                "HYPRIAL_ZENOH_CALLBACK_QUEUE_CAPACITY",
                DEFAULT_CALLBACK_QUEUE_CAPACITY,
            )
        )
        self._query_dispatcher = _QueryDispatcher(
            _environment_integer(
                "HYPRIAL_ZENOH_QUERY_QUEUE_CAPACITY",
                DEFAULT_QUERY_QUEUE_CAPACITY,
            )
        )
        self._session = zenoh.open(self._config.build())
        self._closed = False
        # Guards the session reference, the registration list and the rebuild
        # flag -- never a network call.  A rebuild runs with the lock RELEASED
        # and ``_rebuilding`` set; publishers and declarations wait it out on
        # ``_rebuilt`` (bounded), so none of them can reach a closing session.
        self._lock = _OwnedLock()
        self._rebuilt = threading.Condition(self._lock)  # type: ignore[arg-type]
        self._rebuilding: tuple[int | None, str, float] | None = None
        self._registrations: list[_Registration] = []
        self._rebuild_hooks: list[Callable[[], None]] = []
        self._query_guard = threading.Condition()
        self._query_registrations: dict[QueryIoOwner, _QueryRegistration] = {}
        self._query_registering = 0
        self._query_closing = False
        self._query_lifetime_lock = threading.Lock()
        self._native_closed = False

    @property
    def config(self) -> ZenohConfig:
        return self._config

    def lock_holder(self) -> dict[str, Any] | None:
        """Who holds the transport lock (or is rebuilding the session), for how
        long, and where -- ``None`` when publishers can proceed right now."""

        owner = self._lock.owner()
        role = "lock"
        if owner is None:
            owner = self._rebuilding
            role = "rebuild"
        if owner is None:
            return None
        ident, name, since = owner
        return {
            "thread": name,
            "role": role,
            "heldSeconds": round(time.monotonic() - since, 3),
            "stack": _thread_stack(ident),
        }

    def _wait_failed(self, what: str, key: str, started: float) -> TransportLockTimeout:
        holder = self.lock_holder()
        waited = time.monotonic() - started
        if holder is None:
            detail = "no holder recorded (released while this waiter gave up)"
            stack = ""
        else:
            detail = (
                f"{holder['role']} held by {holder['thread']} "
                f"for {holder['heldSeconds']:.1f}s"
            )
            stack = holder["stack"]
        logger.error(
            "zenoh.lock.wait_exceeded %s %s waited %.1fs (budget %.1fs); %s\n%s",
            what,
            key,
            waited,
            self._lock_wait,
            detail,
            stack,
            extra={"event": "zenoh.lock.wait_exceeded"},
        )
        return TransportLockTimeout(
            f"{what} {key}: waited {waited:.1f}s for the zenoh transport; {detail}"
        )

    @contextmanager
    def _settled(self, what: str, key: str) -> Iterator[None]:
        """Hold the lock with no rebuild in flight, or fail within the budget."""

        started = time.monotonic()
        if not self._lock.acquire(timeout=self._lock_wait):
            raise self._wait_failed(what, key, started)
        try:
            while self._rebuilding is not None:
                remaining = self._lock_wait - (time.monotonic() - started)
                if remaining <= 0:
                    raise self._wait_failed(what, key, started)
                self._rebuilt.wait(remaining)
            yield
        finally:
            self._lock.release()

    def _current_session(self, what: str, key: str) -> Any:
        with self._settled(what, key):
            return self._session

    def callback_overflow_count(self) -> int:
        """Copy the #891 native hand-off lane's loss counter for its owner.

        The baseline without that emergency adapter has no hand-off dispatcher.
        This read adds observability; it does not change callback/lock mechanics.
        """
        dispatcher = getattr(self, "_dispatcher", None)
        if dispatcher is None:
            return 0
        with dispatcher._guard:
            return sum(dispatcher._dropped.values())

    def reconfigure_connect(self, endpoints: tuple[str, ...]) -> None:
        """Drain accepted queries while the old session can still serve them."""

        with self._query_lifetime_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeError("transport is closed")
            if not self._query_dispatcher.quiesce(5.0):
                self._query_dispatcher.resume()
                raise TimeoutError("native query effects did not drain before rebuild")
            try:
                self._reconfigure_connect_drained(endpoints)
            finally:
                self._query_dispatcher.resume()

    def _reconfigure_connect_drained(self, endpoints: tuple[str, ...]) -> None:
        """Dial ``endpoints`` instead of the current connect set, in place.

        zenoh-python 1.9 has no live setter for a session's connect endpoints,
        so this closes the session, opens one with the same config except
        ``connect``, runs the rebuild hooks (materialized presence is cleared
        so departed peers do not linger), and replays every live declaration
        onto it.  The same listen set is re-bound, which is why the old
        session closes first: there is a brief interruption, approved as Q3 of
        the forwarding defaults plan.  If the new session cannot open, the
        previous config is restored and replayed and the error is re-raised,
        so a caller never records a dial set it does not have.

        The close and the reopen run with the transport lock RELEASED (they
        took 0.7-9.2 s on hq): holding it there serialised every publisher
        behind the rebuild.  ``_rebuilding`` keeps publishers and declarations
        off the closing session instead, and they resume on the new one.
        """

        with self._settled("reconfigure", ",".join(endpoints)):
            if self._closed:
                raise RuntimeError("transport is closed")
            candidate = replace(self._config, connect=tuple(endpoints))
            _validate_endpoints(candidate.connect)
            built = candidate.build()
            previous = self._config
            old = self._session
            current = threading.current_thread()
            self._rebuilding = (current.ident, current.name, time.monotonic())
        opened: Any = None
        config = previous
        failure: BaseException | None = None
        try:
            old.close()
            try:
                opened = zenoh.open(built)
                config = candidate
            except Exception as error:  # noqa: BLE001 - restored, then re-raised
                failure = error
                opened = zenoh.open(previous.build())
        finally:
            with self._lock:
                if opened is not None:
                    self._session = opened
                    self._config = config
                    self._replay()
                self._rebuilding = None
                self._rebuilt.notify_all()
                closed_meanwhile = self._closed
            if closed_meanwhile and opened is not None:
                opened.close()
        if failure is not None:
            raise failure

    def on_rebuild(self, hook: Callable[[], None]) -> Callable[[], None]:
        """Run ``hook`` after each rebuild, before declarations are replayed.

        Returns a function that unregisters it.
        """

        with self._lock:
            self._rebuild_hooks.append(hook)

        def remove() -> None:
            with self._lock:
                if hook in self._rebuild_hooks:
                    self._rebuild_hooks.remove(hook)

        return remove

    def _replay(self) -> None:
        for hook in list(self._rebuild_hooks):
            hook()
        for registration in list(self._registrations):
            registration._replay(self._session)

    def _register(self, declare: Callable[[Any], Any]) -> _Registration:
        with self._settled("declare", ""):
            registration = _Registration(
                declare(self._session), declare, self._forget
            )
            self._registrations.append(registration)
        return registration

    def _forget(self, registration: _Registration) -> None:
        with self._lock:
            if registration in self._registrations:
                self._registrations.remove(registration)

    def put(self, key: str, payload: bytes) -> None:
        # Only the reference is read under the lock; the send runs outside it,
        # so one slow put can no longer queue every other publisher behind it.
        session = self._current_session("put", key)
        started = time.monotonic()
        session.put(key, payload, encoding="application/octet-stream")
        elapsed = time.monotonic() - started
        if elapsed > self._put_slow:
            logger.warning(
                "zenoh.put.slow %s took %.1fs (threshold %.1fs)",
                key,
                elapsed,
                self._put_slow,
                extra={"event": "zenoh.put.slow"},
            )

    def get(
        self,
        key_expr: str,
        *,
        timeout: float = 3.0,
        errors: list[str] | None = None,
        all_replies: bool = False,
    ) -> list[TransportSample]:
        """Collect replies to one query.

        ``all_replies`` turns reply consolidation **off**.  It is required by
        any query whose whole point is "ask every node that might know", and
        without it such a query silently returns exactly one answer.

        Zenoh's default consolidation (``AUTO``, which resolves to ``Latest``
        for a concrete key) keeps replies in a map **keyed by key
        expression**, and every queryable replies under the key that was
        queried.  So N holders answering one concrete key collapse to one
        surviving reply, chosen by arrival race -- no error, no warning, and
        the querier cannot tell a consolidated set from a complete one.  For
        ``msg/status/<sender>``, where every holder is supposed to answer,
        that silently discards holders.

        ``errors``, when supplied, receives one entry per reply that carried
        no sample.  Those replies are still replies: a responder answered and
        the answer was an error, which is NOT the same fact as "nobody
        answered".  Dropping them silently -- as this did -- makes a set that
        lost a holder's verdict indistinguishable from a complete one.
        """

        session = self._current_session("get", key_expr)
        replies = session.get(
            key_expr,
            target=zenoh.QueryTarget.ALL,
            consolidation=(
                zenoh.ConsolidationMode.NONE
                if all_replies
                else zenoh.ConsolidationMode.AUTO
            ),
            timeout=timeout,
        )
        results: list[TransportSample] = []
        while True:
            try:
                reply = replies.recv()
            except Exception as error:
                reason = str(error).lower()
                if (
                    "disconnected" in reason
                    or "timeout" in reason
                    or "empty and closed" in reason
                ):
                    break
                raise
            sample = reply.ok
            if sample is not None:
                results.append(_sample(sample))
            elif errors is not None:
                errors.append(_reply_error(reply))
        return results

    def query(
        self,
        key_expr: str,
        payload: bytes,
        *,
        timeout: float = 3.0,
        errors: list[str] | None = None,
        all_replies: bool = False,
    ) -> list[TransportSample]:
        """Collect replies to one payload-bearing query.

        This is deliberately separate from ``get`` so existing callers keep
        the exact no-payload API and semantics.  Orgfs uses ``all_replies``
        for log-range queries, where every reply is a distinct immutable log
        record and Zenoh's default consolidation would otherwise collapse
        same-key replies.
        """

        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        session = self._current_session("query", key_expr)
        replies = session.get(
            key_expr,
            payload=payload,
            target=zenoh.QueryTarget.ALL,
            consolidation=(
                zenoh.ConsolidationMode.NONE
                if all_replies
                else zenoh.ConsolidationMode.AUTO
            ),
            timeout=timeout,
        )
        results: list[TransportSample] = []
        while True:
            try:
                reply = replies.recv()
            except Exception as error:
                reason = str(error).lower()
                if (
                    "disconnected" in reason
                    or "timeout" in reason
                    or "empty and closed" in reason
                ):
                    break
                raise
            sample = reply.ok
            if sample is not None:
                results.append(_sample(sample))
            elif errors is not None:
                errors.append(_reply_error(reply))
        return results

    def subscribe(
        self, key_expr: str, callback: Callable[[TransportSample], None]
    ) -> _Registration:
        # The hand-off wrapper is built once, so a replayed declaration keeps
        # its lane (and its order) across a session rebuild.
        deliver = zenoh.handlers.Callback(
            self._dispatcher.wrap(key_expr, callback),
            indirect=False,
        )
        return self._register(
            lambda session: session.declare_subscriber(key_expr, deliver)
        )

    def declare_queryable(
        self, key_expr: str, handler: Callable[[str], bytes | None]
    ) -> _QueryRegistration:
        def answer(selector: str, _request: bytes | None):
            payload = handler(selector)
            if payload is not None:
                yield selector.split("?", 1)[0], payload
        return self._register_query(key_expr, answer)

    def declare_query_handler(
        self,
        key_expr: str,
        handler: Callable[[str, bytes | None], Iterable[tuple[str, bytes]]],
    ) -> _QueryRegistration:
        return self._register_query(key_expr, handler)

    def _register_query(self, key_expr, handler):
        with self._query_guard:
            if self._query_closing or self._closed:
                raise RuntimeError("transport query registrations are closing")
            self._query_registering += 1
        try:
            owner = QueryIoOwner(
                key_expr, handler, dispatcher=self._query_dispatcher,
                on_error=lambda code: logger.error(
                    "orgfs query handler failed for %s (%s)", key_expr, code
                ),
            )
            callback = zenoh.handlers.Callback(owner.admit, indirect=False)
            try:
                inner = self._register(
                    lambda session: session.declare_queryable(
                        key_expr, callback, complete=True
                    )
                )
            except BaseException as error:
                try:
                    if not owner.close():
                        error.add_note("query owner did not drain after failed declaration")
                except BaseException as cleanup_error:
                    error.add_note(
                        "query owner cleanup raised "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
                raise
            registration = _QueryRegistration(inner, owner, self._forget_query)
            with self._query_guard:
                self._query_registrations[owner] = registration
            return registration
        finally:
            with self._query_guard:
                self._query_registering -= 1
                self._query_guard.notify_all()

    def _forget_query(self, owner):
        with self._query_guard:
            self._query_registrations.pop(owner, None)

    def query_status(self):
        with self._query_guard:
            owners = tuple(self._query_registrations)
        return tuple(owner.projection() for owner in owners)

    def declare_liveliness(self, key: str) -> _Registration:
        return self._register(lambda session: session.liveliness().declare_token(key))

    def observe_liveliness(
        self,
        key_expr: str,
        callback: Callable[[TransportSample], None],
        *,
        history: bool = True,
    ) -> _Registration:
        # Liveliness callbacks leave zenoh's thread too.  Declaring a token
        # hands it to local observers and waits for them, and orgfs's observer
        # publishes: run inline, it waited for the transport lock that the
        # rebuild replaying that very token held (the 2026-09-27 deadlock).
        deliver = zenoh.handlers.Callback(
            self._dispatcher.wrap(key_expr, callback),
            indirect=False,
        )
        return self._register(
            lambda session: session.liveliness().declare_subscriber(
                key_expr, deliver, history=history
            )
        )

    def close(self) -> None:
        with self._query_lifetime_lock:
            deadline = time.monotonic() + 5.0
            with self._lock:
                self._closed = True
            with self._query_guard:
                self._query_closing = True
                while self._query_registering:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("native query declaration did not settle")
                    self._query_guard.wait(remaining)
                registrations = tuple(self._query_registrations.values())
            for registration in registrations:
                registration.close(max(0.0, deadline - time.monotonic()))
            if not self._query_dispatcher.stop(max(0.0, deadline - time.monotonic())):
                raise TimeoutError("native query pool did not drain")
            if not self._native_closed:
                self._session.close()
                self._native_closed = True
            self._dispatcher.stop()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class PresenceObservation:
    key: str
    kind: str
    change: str
    sample_generation: int | None
    presence_generation: int
    transport_generation: int | None
    history_complete: bool | None
    admission: str


class LivelinessDirectory:
    """Materialized actor/mailbox presence from Zenoh liveliness tokens."""

    def __init__(
        self,
        session: ZenohTransport,
        keys: KeySpace | None = None,
        *,
        on_actor_online: Callable[[str], None] | None = None,
        observation_sink: Callable[[PresenceObservation], None] | None = None,
    ) -> None:
        self._keys = keys or KeySpace()
        self._observation_sink = observation_sink
        self._authority = PresenceAuthority()
        self._legacy_online_unbind: Callable[[], None] | None = None
        if on_actor_online is not None:
            self.set_actor_online_callback(on_actor_online)
        self._session_generation = getattr(session, "projection", None)
        # A rebuilt session re-learns presence from the replayed observers'
        # history; carrying the old sets over would keep departed peers online.
        self._stop_rebuild_hook = session.on_rebuild(self._forget_presence)
        self._registrations = [
            session.observe_liveliness(
                f"{self._keys.prefix}/liveliness/actor/*",
                self._actor_event,
                history=True,
            ),
            session.observe_liveliness(
                self._keys.mailbox_liveliness_all(), self._mailbox_event, history=True
            ),
        ]

    def _actor_event(self, sample: TransportSample) -> None:
        self._update("actor", sample)

    def _mailbox_event(self, sample: TransportSample) -> None:
        self._update("mailbox", sample)

    def _update(self, kind: str, sample: TransportSample) -> None:
        generation = self._authority.projection().generation
        transport_projection = None
        if sample.generation is not None and self._session_generation is not None:
            transport_projection = self._session_generation()
            if sample.generation != transport_projection.generation:
                self._observe(kind, sample, generation, transport_projection, "stale")
                return
        identity = self._keys.decode_identity(sample.key.rsplit("/", 1)[-1])
        admission = self._authority.change(kind, identity, sample.kind != "delete", generation=generation)
        self._observe(kind, sample, generation, transport_projection, admission.value)
        if admission.value != "accepted":
            logger.error("presence admission %s; generation is unknown", admission.value)
            # Production callbacks run under TransportSessionAuthority. Surface
            # the failed domain handoff so its existing callback-failure path
            # advances the generation and replays observer history.
            raise RuntimeError(f"presence admission {admission.value}; history replay required")

    def _observe(self, kind, sample, generation, transport_projection, admission):
        sink = self._observation_sink
        if sink is None:
            return
        try:
            sink(PresenceObservation(
                str(sample.key)[:512], kind, str(sample.kind), sample.generation,
                generation, getattr(transport_projection, "generation", None),
                getattr(transport_projection, "callbacks_complete", None), admission,
            ))
        except Exception:
            # Optional bounded diagnostics never invalidate an otherwise valid
            # presence transition. No payload or exception value is exposed.
            pass

    def _forget_presence(self) -> None:
        self._authority.reset()

    def actor_online(self, actor: str) -> bool:
        return self._callbacks_current() and actor in self._authority.projection().actors

    def presence_projection(self) -> PresenceProjection:
        """Immutable generation-fenced projection for online-wake consumers."""

        return self._authority.projection()

    def set_actor_online_callback(self, callback: Callable[[str], None]) -> None:
        """Preserve the dev observer API over committed presence transitions.

        Callbacks must be nonblocking; production binds the typed wake ingress.
        """
        if self._legacy_online_unbind is not None:
            self._legacy_online_unbind()
        self._legacy_online_unbind = self.bind_actor_online(
            lambda transition: callback(transition.actor)
        )

    def bind_actor_online(
        self, observer: Callable[[ActorOnlineTransition], object]
    ) -> Callable[[], None]:
        """Observe committed online transitions, replaying current actors."""

        return self._authority.bind_actor_online(observer)

    def online_actors(self) -> tuple[str, ...]:
        if not self._callbacks_current():
            return ()
        return tuple(sorted(self._authority.projection().actors))

    def online_mailboxes(self) -> tuple[str, ...]:
        if not self._callbacks_current():
            return ()
        return tuple(sorted(self._authority.projection().mailboxes))

    def _callbacks_current(self) -> bool:
        if self._session_generation is None:
            return True
        projection = self._session_generation()
        return not projection.closed and projection.callbacks_complete

    def close(self) -> None:
        if self._legacy_online_unbind is not None:
            self._legacy_online_unbind()
            self._legacy_online_unbind = None
        self._stop_rebuild_hook()
        for registration in reversed(self._registrations):
            registration.close()
        if not self._authority.close():
            raise TimeoutError("presence authority did not drain")
        self._observation_sink = None
