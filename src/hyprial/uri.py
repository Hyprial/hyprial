"""Pure URI grammar helpers and target-kind constants.

The network-visible identity of an agent is ``agent:<owner>:<machine>:<actor>``
— the same shape the TS harness pins with ``parseActorId``.  This leaf module
has no daemon, settings, owner, or filesystem dependencies so schema modules
can import the canonical grammar without executing ``hyprial.daemon``.
"""

from __future__ import annotations

AGENT_URI_PREFIX = "agent:"
_AGENT_URI_SEGMENTS = 4


def canonical_agent_uri(owner: str, machine: str, actor: str) -> str:
    """Compose the four-segment ``agent:<owner>:<machine>:<actor>`` URI."""

    for label, value in (("owner", owner), ("machine", machine), ("actor", actor)):
        if not value or not value.strip():
            raise ValueError(f"agent identity {label} must not be empty")
        if ":" in value:
            raise ValueError(f"agent identity {label} must not contain ':'")
    return f"{AGENT_URI_PREFIX}{owner}:{machine}:{actor}"


def parse_agent_uri(value: str) -> tuple[str, str, str] | None:
    """Decompose a canonical ``agent:<owner>:<machine>:<actor>`` URI.

    Returns ``(owner, machine, actor)`` for exactly four non-empty segments,
    ``None`` for every other shape.  This is the ONLY agent-URI deconstructor
    — anywhere else splitting on ``:`` to read a URI's parts is a guard
    violation (URI 产生点唯一 covers parsing too: one writer, one reader).
    """

    if not value.startswith(AGENT_URI_PREFIX):
        return None
    parts = value.split(":")
    if len(parts) != _AGENT_URI_SEGMENTS or not all(parts[1:]):
        return None
    return parts[1], parts[2], parts[3]


def agent_uri_actor(value: str) -> str | None:
    """Return the short actor name when ``value`` is a four-segment agent URI."""

    parsed = parse_agent_uri(value)
    return parsed[2] if parsed is not None else None


#: The legacy ``channel:<owner>:<machine>:<name>`` adapter-address prefix.
#: Historical data only after the 2026-08-24 naming migration (PAC
#: b8d2b52b6358 / NAMB efeeaf0a01ba) -- no writer mints this spelling
#: anymore, but it must keep parsing: existing channels.json/desired-state/
#: inbox records still carry it and are never rewritten.
CHANNEL_URI_PREFIX = "channel:"

#: The current ``adapter:<owner>:<machine>:<name>`` adapter-address prefix.
#: Every new writer uses this spelling (Allen's channel=IRC / adapter=
#: external-platform-adapter ruling).
ADAPTER_URI_PREFIX = "adapter:"

_CHANNEL_URI_PREFIXES = (CHANNEL_URI_PREFIX, ADAPTER_URI_PREFIX)


def parse_channel_uri(value: str) -> tuple[str, str, str] | None:
    """Decompose a ``channel:``/``adapter:`` ``<owner>:<machine>:<name>`` adapter address.

    Dual-read: accepts both the legacy ``channel:`` spelling and the current
    ``adapter:`` spelling, and both parse to the identical (owner, machine,
    name) tuple -- the prefix text itself carries no identity, only the
    grammar delimiter differs.  Same one-reader rule as
    :func:`parse_agent_uri`.  (The three-segment ``channel:lark:<adapter>``/
    ``adapter:lark:<adapter>`` reply-bridge form is owned and parsed by
    ``hyprial.adapters.lark.lark_reply_adapter``.)
    """

    if not value.startswith(_CHANNEL_URI_PREFIXES):
        return None
    parts = value.split(":")
    if len(parts) != 4 or not all(parts[1:]):
        return None
    return parts[1], parts[2], parts[3]


def short_actor_name(value: str) -> str:
    """Display form: the actor segment of a canonical URI, else the value."""

    parsed = parse_agent_uri(value) or parse_channel_uri(value)
    return parsed[2] if parsed is not None else value


TARGET_KIND_AGENT = "agent"
TARGET_KIND_HOST = "host"
TARGET_KIND_USER = "user"
TARGET_KIND_CHANNEL_ROUTE = "channel_route"
TARGET_KIND_UNKNOWN = "unknown"

#: Every kind the classifier can return.  ``unknown`` is a real verdict, not
#: an else-branch accident: shapes that match no known type are labeled
#: unknown (never rejected -- a wrong rejection can take a healthy machine
#: off the network, a wrong label only hides a row).
TARGET_KINDS = frozenset(
    {
        TARGET_KIND_AGENT,
        TARGET_KIND_HOST,
        TARGET_KIND_USER,
        TARGET_KIND_CHANNEL_ROUTE,
        TARGET_KIND_UNKNOWN,
    }
)
