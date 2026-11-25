# Decision notes (send-link removal plan T1a, 2026-11-20):
# - This module freezes the derivation that decides each conversation's
#   attribution key: the id called "thread id" between 2026-11-03 and
#   2026-11-19, moved verbatim from threads/ids.py and threads/aliases.py
#   (threads itself is deleted by a later slice; until then the equivalence
#   tests in tests/daemon/conversations/keys pin both to the same literals).
# - DIRECT_SEED_TAG and the kernel's mint tag (b"hyprial-thread-id-v1\0" in
#   kernel/impl/primitives/uri.py) are part of the persisted format: changing
#   either moves every existing conversation.  The same holds for
#   PAIR_SEED_TAG in record/contracts.py.
# - The internal actor rule is the baseline's verbatim: the kernel's reserved
#   agent names always, plus any runtime names a caller passes as *internal*
#   (review R1: production passed exactly RESERVED_AGENT_ACTOR_NAMES at
#   application/__init__.py).
# - S3a (2026-11-24): the owner binding is gone.  A ``user:<owner>``
#   counterpart is always its own conversation; no profile or receivers
#   record turns it into the owner's adapter thread.
# - I4a (2026-11-24): the ``route:<adapter>:<route>`` endpoint is retired.
#   A route peer has no key; rows stamped before keep their stored stamp,
#   and nothing re-derives or rewrites them.
# - ``threadId``/``thread_id`` remain the historical spellings of the
#   attribution stamp on rows, envelopes and record entries; only the Python
#   derivation moved.  Pure: no clock, no I/O, no daemon state.
"""Pure conversation keys: the frozen derivation of each conversation's id."""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Literal

from hyprial.kernel import (
    ADAPTER_URI_PREFIX,
    RESERVED_AGENT_ACTOR_NAMES,
    canonical_agent_uri,
    canonical_user_uri,
    mint_thread_id,
    parse_agent_uri,
    parse_adapter_uri,
    parse_user_uri,
)

DirectKind = Literal["agent", "user", "adapter"]
DIRECT_KINDS: tuple[DirectKind, ...] = ("agent", "user", "adapter")
DIRECT_SEED_TAG = b"hyprial-thread-v1\0"

__all__ = [
    "DIRECT_KINDS",
    "DIRECT_SEED_TAG",
    "DirectEndpoint",
    "DirectKey",
    "DirectKind",
    "direct_endpoint",
    "direct_key",
    "direct_seed",
]


@dataclass(frozen=True, slots=True)
class DirectEndpoint:
    """One canonical endpoint address and its conversation kind."""

    kind: DirectKind
    uri: str


@dataclass(frozen=True, slots=True)
class DirectKey:
    """The identity of one direct conversation: kind, agent side, counterpart."""

    kind: DirectKind
    agent: str
    counterpart: str

    @property
    def id(self) -> str:
        return mint_thread_id(direct_seed(self))


def direct_endpoint(uri: str) -> DirectEndpoint | None:
    """The canonical endpoint of *uri*, or None when it has none."""

    if not isinstance(uri, str) or not uri:
        return None
    agent = parse_agent_uri(uri)
    if agent is not None:
        return DirectEndpoint("agent", canonical_agent_uri(*agent))
    owner = parse_user_uri(uri)
    if owner is not None:
        return DirectEndpoint("user", canonical_user_uri(owner))
    adapter = parse_adapter_uri(uri)
    if adapter is not None:
        return DirectEndpoint("adapter", ADAPTER_URI_PREFIX + ":".join(adapter))
    return None


def _internal(endpoint: DirectEndpoint, internal: AbstractSet[str]) -> bool:
    if endpoint.kind != "agent":
        return False
    parsed = parse_agent_uri(endpoint.uri)
    name = parsed[2] if parsed is not None else ""
    return name in RESERVED_AGENT_ACTOR_NAMES or name in internal


def direct_key(
    a: str,
    b: str,
    *,
    internal: AbstractSet[str] = frozenset(),
) -> DirectKey | None:
    """The conversation between two addresses, or None when neither is an agent.

    An agent side whose name is reserved, or listed in *internal*, is a
    daemon-internal actor and has no key.
    """

    left = direct_endpoint(a)
    right = direct_endpoint(b)
    if left is None or right is None:
        return None
    if _internal(left, internal) or _internal(right, internal):
        return None
    if left.kind == "agent" and right.kind == "agent":
        low, high = sorted((left.uri, right.uri))
        key: DirectKey | None = DirectKey("agent", low, high)
    elif left.kind == "agent":
        key = DirectKey(right.kind, left.uri, right.uri)
    elif right.kind == "agent":
        key = DirectKey(left.kind, right.uri, left.uri)
    else:
        key = None
    return key


def direct_seed(key: DirectKey) -> bytes:
    """The length-framed mint seed of one direct key."""

    framed = bytearray(DIRECT_SEED_TAG)
    for part in (key.kind, key.agent, key.counterpart):
        encoded = part.encode("utf-8")
        framed += len(encoded).to_bytes(4, "big") + encoded
    return bytes(framed)

