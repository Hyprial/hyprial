"""Synchronous Zenoh adapter implementing the public transport protocol."""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Self

import zenoh

from .api import TransportSample
from .keys import KeySpace

DEFAULT_LEASE_MS = 6_000
DEFAULT_KEEP_ALIVE = 4
DEFAULT_CONNECT_RETRY_INITIAL_MS = 500
DEFAULT_CONNECT_RETRY_MAX_MS = 8_000
DEFAULT_CONNECT_RETRY_MULTIPLIER = 2.0


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
        self._closed = True
        if self._on_close is not None:
            self._on_close(self)
        undeclare = getattr(self._inner, "undeclare", None)
        if undeclare is not None:
            undeclare()

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
        self._session = zenoh.open(self._config.build())
        self._closed = False
        # Held across a rebuild so puts and declarations wait it out instead
        # of racing a closed session; queries only capture the session.
        self._lock = threading.RLock()
        self._registrations: list[_Registration] = []
        self._rebuild_hooks: list[Callable[[], None]] = []

    @property
    def config(self) -> ZenohConfig:
        return self._config

    def reconfigure_connect(self, endpoints: tuple[str, ...]) -> None:
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
        """

        with self._lock:
            if self._closed:
                raise RuntimeError("transport is closed")
            candidate = replace(self._config, connect=tuple(endpoints))
            _validate_endpoints(candidate.connect)
            built = candidate.build()
            previous = self._config
            self._session.close()
            try:
                self._session = zenoh.open(built)
            except Exception:
                self._session = zenoh.open(previous.build())
                self._replay()
                raise
            self._config = candidate
            self._replay()

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
        with self._lock:
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
        with self._lock:
            self._session.put(key, payload, encoding="application/octet-stream")

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

        with self._lock:
            session = self._session
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

    def subscribe(
        self, key_expr: str, callback: Callable[[TransportSample], None]
    ) -> _Registration:
        return self._register(
            lambda session: session.declare_subscriber(
                key_expr, lambda sample: callback(_sample(sample))
            )
        )

    def declare_queryable(
        self, key_expr: str, handler: Callable[[str], bytes | None]
    ) -> _Registration:
        def answer(query: Any) -> None:
            payload = handler(str(query.selector))
            if payload is not None:
                query.reply(
                    str(query.key_expr), payload, encoding="application/octet-stream"
                )

        return self._register(
            lambda session: session.declare_queryable(key_expr, answer, complete=True)
        )

    def declare_liveliness(self, key: str) -> _Registration:
        return self._register(lambda session: session.liveliness().declare_token(key))

    def observe_liveliness(
        self,
        key_expr: str,
        callback: Callable[[TransportSample], None],
        *,
        history: bool = True,
    ) -> _Registration:
        return self._register(
            lambda session: session.liveliness().declare_subscriber(
                key_expr, lambda sample: callback(_sample(sample)), history=history
            )
        )

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class LivelinessDirectory:
    """Materialized actor/mailbox presence from Zenoh liveliness tokens."""

    def __init__(self, session: ZenohTransport, keys: KeySpace | None = None) -> None:
        self._keys = keys or KeySpace()
        self._actors: set[str] = set()
        self._mailboxes: set[str] = set()
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
        self._update(self._actors, sample)

    def _mailbox_event(self, sample: TransportSample) -> None:
        self._update(self._mailboxes, sample)

    def _update(self, values: set[str], sample: TransportSample) -> None:
        identity = self._keys.decode_identity(sample.key.rsplit("/", 1)[-1])
        if sample.kind == "delete":
            values.discard(identity)
        else:
            values.add(identity)

    def _forget_presence(self) -> None:
        self._actors.clear()
        self._mailboxes.clear()

    def actor_online(self, actor: str) -> bool:
        return actor in self._actors

    def online_actors(self) -> tuple[str, ...]:
        return tuple(sorted(self._actors))

    def online_mailboxes(self) -> tuple[str, ...]:
        return tuple(sorted(self._mailboxes))

    def close(self) -> None:
        self._stop_rebuild_hook()
        for registration in reversed(self._registrations):
            registration.close()
