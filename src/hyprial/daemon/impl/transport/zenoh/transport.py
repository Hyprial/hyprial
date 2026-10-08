"""ZenohTransport: the synchronous zenoh session boundary."""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Self

import zenoh


from hyprial.daemon.impl.transport.api  import TransportSample
from hyprial.daemon.impl.transport.query_actor  import QueryIoOwner
from hyprial.daemon.impl.transport.zenoh.config import (
    DEFAULT_CALLBACK_QUEUE_CAPACITY,
    DEFAULT_LOCK_WAIT_SECONDS,
    DEFAULT_PUT_SLOW_SECONDS,
    DEFAULT_QUERY_QUEUE_CAPACITY,
    ZenohConfig,
    _environment_float,
    _environment_integer,
    _validate_endpoints,
)
from hyprial.daemon.impl.transport.zenoh.dispatchers import (
    _CallbackDispatcher,
    _QueryDispatcher,
    _sample,
)
from hyprial.daemon.impl.transport.zenoh.locks import (
    TransportLockTimeout,
    _OwnedLock,
    _thread_stack,
)

logger = logging.getLogger(__name__)

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

_DIAGNOSTIC_QUEUE_CAPACITY = 1024

class ZenohTransport:
    def __init__(
        self,
        config: ZenohConfig | None = None,
        *,
        event_sink: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self._config = config or ZenohConfig()
        self._event_sink = event_sink
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
        # Bounded: a flapping link must not grow memory without limit.  A
        # full queue drops the event and counts it; the logger reports the
        # count with the next event it writes.
        self._diagnostic_events: queue.Queue[
            tuple[str, dict[str, object]] | None
        ] = queue.Queue(maxsize=_DIAGNOSTIC_QUEUE_CAPACITY)
        self._diagnostic_dropped = 0
        self._diagnostic_thread = threading.Thread(
            target=self._log_diagnostic_events,
            name="hyprial-zenoh-events",
            daemon=True,
        )
        self._diagnostic_thread.start()
        self._diagnostic_listeners: list[Any] = []
        self._install_diagnostic_listeners(self._session)
        zid = self._session_zid(self._session)
        self._emit_diagnostic(
            "zenoh.session.opened",
            "zenoh.session.opened zid=%s listen=%s connect=%s",
            zid,
            list(self._config.listen),
            list(self._config.connect),
            zid=zid,
            listen=list(self._config.listen),
            connect=list(self._config.connect),
            reason="initial",
        )

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
            old_zid = self._session_zid(old)
            old.close()
            self._emit_diagnostic(
                "zenoh.session.closed",
                "zenoh.session.closed zid=%s reason=%s",
                old_zid,
                "reconfigure",
                zid=old_zid,
                reason="reconfigure",
            )
            try:
                opened = zenoh.open(built)
                config = candidate
            except Exception as error:  # noqa: BLE001 - restored, then re-raised
                failure = error
                opened = zenoh.open(previous.build())
        finally:
            # Listener declaration can touch Zenoh; install it before taking
            # the transport lock, under the same #891 rule as close/open.
            if opened is not None:
                self._install_diagnostic_listeners(opened)
            with self._lock:
                if opened is not None:
                    self._session = opened
                    self._config = config
                    self._replay()
                self._rebuilding = None
                self._rebuilt.notify_all()
                closed_meanwhile = self._closed
            if closed_meanwhile and opened is not None:
                opened_zid = self._session_zid(opened)
                opened.close()
                self._emit_diagnostic(
                    "zenoh.session.closed",
                    "zenoh.session.closed zid=%s reason=%s",
                    opened_zid,
                    "transport-close-during-reconfigure",
                    zid=opened_zid,
                    reason="transport-close-during-reconfigure",
                )
        if opened is not None and not closed_meanwhile:
            zid = self._session_zid(opened)
            self._emit_diagnostic(
                "zenoh.session.opened",
                "zenoh.session.opened zid=%s listen=%s connect=%s",
                zid,
                list(config.listen),
                list(config.connect),
                zid=zid,
                listen=list(config.listen),
                connect=list(config.connect),
                reason="reconfigure-restored" if failure is not None else "reconfigure",
            )
            self._emit_diagnostic(
                "zenoh.session.rebuilt",
                "zenoh.session.rebuilt zid=%s listen=%s connect=%s",
                zid,
                list(config.listen),
                list(config.connect),
                zid=zid,
                listen=list(config.listen),
                connect=list(config.connect),
            )
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

    @staticmethod
    def _session_zid(session: Any) -> str:
        zid = getattr(session, "zid", None)
        return str(zid()) if callable(zid) else "unavailable"

    def _install_diagnostic_listeners(self, session: Any) -> None:
        """Attach optional Zenoh 1.9 diagnostics outside caller registrations.

        Diagnostics are optional and must never decide whether the transport
        works: by the time this runs the session is already open and the drain
        thread is already started, so a raising ``declare_*_listener`` would
        leak both and stop the daemon over a monitoring hook.  Record it and
        carry on without listeners instead.
        """

        info = getattr(session, "info", None)
        if info is None:
            self._diagnostic_listeners = []
            return
        listeners: list[Any] = []
        try:
            listeners.append(
                info.declare_transport_events_listener(
                    self._queue_transport_event, history=True
                )
            )
            listeners.append(
                info.declare_link_events_listener(self._queue_link_event, history=True)
            )
        except Exception as error:  # noqa: BLE001 - diagnostics must not disable transport
            for listener in listeners:
                undeclare = getattr(listener, "undeclare", None)
                if callable(undeclare):
                    try:
                        undeclare()
                    except Exception:  # noqa: BLE001 - cleanup is best effort
                        logger.debug(
                            "zenoh diagnostic listener undeclare failed", exc_info=True
                        )
            self._diagnostic_listeners = []
            self._emit_diagnostic(
                "zenoh.diagnostic.unavailable",
                "zenoh.diagnostic.unavailable reason=declare_failed error=%s",
                type(error).__name__,
                reason="declare_failed",
                error=type(error).__name__,
            )
            return
        self._diagnostic_listeners = listeners

    def _queue_transport_event(self, event: Any) -> None:
        """Copy one transport event off Zenoh's callback thread."""

        transport = event.transport
        self._offer_diagnostic(
            (
                "transport",
                {
                    "kind": str(event.kind).rsplit(".", 1)[-1].lower(),
                    "zid": str(transport.zid),
                    "whatami": str(transport.whatami),
                },
            )
        )

    def _queue_link_event(self, event: Any) -> None:
        """Copy one link event off Zenoh's callback thread."""

        link = event.link
        self._offer_diagnostic(
            (
                "link",
                {
                    "kind": str(event.kind).rsplit(".", 1)[-1].lower(),
                    "zid": str(link.zid),
                    "src": str(link.src),
                    "dst": str(link.dst),
                },
            )
        )

    def _offer_diagnostic(self, item: tuple[str, dict[str, object]]) -> None:
        """Never block Zenoh's callback thread; count what does not fit."""

        try:
            self._diagnostic_events.put_nowait(item)
        except queue.Full:
            self._diagnostic_dropped += 1

    def _stop_diagnostic_logger(self) -> None:
        """Stop the drain thread; the marker gets in even when the queue is full."""

        while True:
            try:
                self._diagnostic_events.put_nowait(None)
                break
            except queue.Full:
                try:
                    self._diagnostic_events.get_nowait()
                except queue.Empty:
                    pass

    def _log_diagnostic_events(self) -> None:
        while True:
            item = self._diagnostic_events.get()
            if item is None:
                return
            dropped, self._diagnostic_dropped = self._diagnostic_dropped, 0
            if dropped:
                self._emit_diagnostic(
                    "zenoh.diagnostic.dropped",
                    "zenoh.diagnostic.dropped count=%s",
                    dropped,
                    count=dropped,
                )
            category, fields = item
            self._emit_diagnostic(
                f"zenoh.{category}.event",
                "zenoh.%s.event kind=%s zid=%s src=%s dst=%s",
                category,
                fields["kind"],
                fields["zid"],
                fields.get("src", ""),
                fields.get("dst", ""),
                **fields,
            )

    def _emit_diagnostic(
        self,
        event: str,
        message: str,
        *args: object,
        **fields: object,
    ) -> None:
        logger.info(message, *args, extra={"event": event, **fields})
        if self._event_sink is not None:
            try:
                self._event_sink(event, dict(fields))
            except Exception:  # noqa: BLE001 - diagnostics must not kill the logger thread
                logger.debug("zenoh diagnostic sink failed for %s", event, exc_info=True)

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

    def get_liveliness(
        self, key_expr: str, *, timeout: float = 1.0
    ) -> list[TransportSample]:
        """One bounded read of native live tokens, not an ordinary data get.

        No positive reply is only 'not confirmed online': channel completion
        alone cannot establish why a remote token was absent. Explicit native
        timeout/error replies remain errors; callers fail closed for all three.
        """
        if timeout <= 0:
            raise TimeoutError("liveliness query budget expired")
        session = self._current_session("get_liveliness", key_expr)
        replies = session.liveliness().get(key_expr, timeout=timeout)
        results: list[TransportSample] = []
        while True:
            try:
                reply = replies.recv()
            except Exception as error:
                reason = str(error).lower()
                if "timeout" in reason:
                    raise TimeoutError("liveliness query timed out") from error
                if "disconnected" in reason or "empty and closed" in reason:
                    break
                raise
            if reply.ok is None:
                detail = _reply_error(reply)
                if "timeout" in detail.lower():
                    raise TimeoutError("liveliness query timed out")
                raise RuntimeError("liveliness query returned an error")
            results.append(_sample(reply.ok))
        return results

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
                zid = self._session_zid(self._session)
                self._session.close()
                self._native_closed = True
                self._emit_diagnostic(
                    "zenoh.session.closed",
                    "zenoh.session.closed zid=%s reason=%s",
                    zid,
                    "transport-close",
                    zid=zid,
                    reason="transport-close",
                )
            self._dispatcher.stop()
        self._stop_diagnostic_logger()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
