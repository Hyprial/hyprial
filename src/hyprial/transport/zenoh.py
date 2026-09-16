"""Synchronous Zenoh adapter implementing the public transport protocol."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
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
    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        undeclare = getattr(self._inner, "undeclare", None)
        if undeclare is not None:
            undeclare()


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
        self._session = zenoh.open((config or ZenohConfig()).build())
        self._closed = False

    def put(self, key: str, payload: bytes) -> None:
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

        replies = self._session.get(
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
        inner = self._session.declare_subscriber(
            key_expr, lambda sample: callback(_sample(sample))
        )
        return _Registration(inner)

    def declare_queryable(
        self, key_expr: str, handler: Callable[[str], bytes | None]
    ) -> _Registration:
        def answer(query: Any) -> None:
            payload = handler(str(query.selector))
            if payload is not None:
                query.reply(
                    str(query.key_expr), payload, encoding="application/octet-stream"
                )

        return _Registration(
            self._session.declare_queryable(key_expr, answer, complete=True)
        )

    def declare_liveliness(self, key: str) -> _Registration:
        return _Registration(self._session.liveliness().declare_token(key))

    def observe_liveliness(
        self,
        key_expr: str,
        callback: Callable[[TransportSample], None],
        *,
        history: bool = True,
    ) -> _Registration:
        inner = self._session.liveliness().declare_subscriber(
            key_expr, lambda sample: callback(_sample(sample)), history=history
        )
        return _Registration(inner)

    def close(self) -> None:
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

    def actor_online(self, actor: str) -> bool:
        return actor in self._actors

    def online_actors(self) -> tuple[str, ...]:
        return tuple(sorted(self._actors))

    def online_mailboxes(self) -> tuple[str, ...]:
        return tuple(sorted(self._mailboxes))

    def close(self) -> None:
        for registration in reversed(self._registrations):
            registration.close()
