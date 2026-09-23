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
    if any(part.strip() != part for part in parts[1:]):
        # Whitespace-padded segments are not identities: ``agent:a: :c`` must
        # not parse as a valid machine (2026-09-14 S4).
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


#: The ``route:<adapter>:<route>`` outbound channel-route prefix.  Grammar
#: mirror of ``hyprial.daemon.route_delivery.RouteTarget.parse`` — that
#: module belongs to the daemon package, so schema validators use this leaf
#: copy instead of importing the daemon.
ROUTE_URI_PREFIX = "route:"

#: The ``user:<owner>`` human-recipient prefix.  Grammar mirror of
#: ``hyprial.squire.addressing.UserDeliveryTarget.parse`` — same leaf-module
#: reasoning as ``route:`` above.
USER_URI_PREFIX = "user:"


def parse_route_uri(value: str) -> tuple[str, str] | None:
    """Decompose ``route:<adapter>:<route>``; ``None`` for any other shape."""

    if not value.startswith(ROUTE_URI_PREFIX):
        return None
    parts = value.split(":")
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def delivery_address_error(value: str) -> str | None:
    """``None`` when ``value`` is a deliverable address shape, else the reason.

    Deliverable shapes today: a canonical agent URI or ``user:<owner>``.
    ``route:<adapter>:<route>`` is parsed but deliberately NOT accepted yet:
    the alarm/report delivery path has no route: transport wired, so a route:
    escalate_to / report_to would pass schema and then fail at delivery
    (2026-09-14 B4).  It is rejected loud until route: delivery exists.
    Bare names are NOT addresses either — they parse as nothing and land in
    stores nobody reads.
    """

    if not isinstance(value, str) or not value.strip():
        return "address must be a non-empty string"
    candidate = value.strip()
    if parse_agent_uri(candidate) is not None:
        return None
    if parse_user_uri(candidate) is not None:
        return None
    if parse_route_uri(candidate) is not None:
        return (
            f"{candidate!r} uses route:<adapter>:<route>, which is not yet "
            "supported for escalate_to/report_to delivery; use a full agent "
            "URI (agent:<owner>:<machine>:<actor>) or user:<owner>"
        )
    return (
        f"{candidate!r} is not a deliverable address; write a full agent URI "
        "(agent:<owner>:<machine>:<actor>) or user:<owner> — bare names are "
        "undeliverable"
    )


def short_actor_name(value: str) -> str:
    """Display form: the actor segment of a canonical URI, else the value."""

    parsed = parse_agent_uri(value) or parse_channel_uri(value)
    return parsed[2] if parsed is not None else value


#: The ``user:<owner>`` person-address prefix.  A person is not bound to a
#: machine: delivery is receiver-owned (the owner's own Squire routes it), so
#: the URI carries no machine or actor segment.
USER_URI_PREFIX = "user:"


def uri_scheme(value: str) -> str | None:
    """The lowercase scheme before the first colon, or None for a bare word.

    The one classification point for "what kind of address string is this";
    consumers (e.g. PAC's principal grammar) must not sniff scheme prefixes
    themselves (URI 产生点唯一,读写同源).  Saying the scheme says nothing
    about validity: parsing stays with the per-scheme parse helpers.
    """

    scheme, separator, _ = value.partition(":")
    if not separator:
        return None
    return scheme.lower() if scheme else None


def canonical_user_uri(owner: str) -> str:
    """Compose the two-segment ``user:<owner>`` URI.

    Same purity bar as :func:`canonical_agent_uri`: non-empty, no colon, no
    leading/trailing whitespace -- the strict shape
    ``squire.addressing.UserDeliveryTarget.parse`` has always enforced, lifted
    here so schema modules can use it without importing Squire's transport
    dependencies.  The grammar deliberately says nothing about what an owner
    may contain beyond that (it is a user identity, not a host login).
    """

    if not owner or not owner.strip() or owner.strip() != owner:
        raise ValueError("user identity owner must be non-empty without surrounding whitespace")
    if ":" in owner:
        raise ValueError("user identity owner must not contain ':'")
    return f"{USER_URI_PREFIX}{owner}"


def parse_user_uri(value: str) -> str | None:
    """Decompose a canonical ``user:<owner>`` URI.

    Returns the owner segment for exactly the strict two-segment shape,
    ``None`` for every other shape.  This is the ONLY user-URI deconstructor
    -- the same one-reader rule as :func:`parse_agent_uri`.

    Mirrors ``hyprial.squire.addressing.UserDeliveryTarget.parse``: an empty
    owner, an embedded ``:``, or leading/trailing whitespace (``user: x``) is
    not a user target, so schema must reject it exactly where the transport
    would (2026-09-14 S4).
    """

    if not value.startswith(USER_URI_PREFIX):
        return None
    owner = value[len(USER_URI_PREFIX):]
    if not owner or ":" in owner or owner.strip() != owner:
        return None
    return owner


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
