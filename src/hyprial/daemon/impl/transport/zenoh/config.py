"""Zenoh session configuration and environment overrides."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Self

import zenoh



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
