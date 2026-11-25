"""Pure URI grammar helpers and target-kind constants.

The network-visible identity of an agent is ``agent:<owner>:<machine>:<actor>``
— the same shape the TS harness pins with ``parseActorId``.  This leaf module
has no daemon, settings, owner, or filesystem dependencies so schema modules
can import the canonical grammar without executing ``hyprial.daemon``.

Decision notes (I4b, owner items Q0-a and Q0-d, 2026-11-23): the
``route:<adapter>:<route>`` address family is retired: ``ROUTE_URI_PREFIX``,
``parse_route_uri``, its ``ADDRESS_FAMILIES`` entry and the
``channel_route`` target kind are deleted.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import re
import secrets
from urllib.parse import quote, unquote

AGENT_URI_PREFIX = "agent:"
_AGENT_URI_SEGMENTS = 4

# Immutable-id addressing P1 issues only canonical lowercase spellings.  P3
# will use these same predicates while parsing address segments, so name
# admission and address interpretation cannot drift.
# User ids are the Casdoor ``sub``. Production reading (infra-ops, read-only
# SQL on production Casdoor, 2026-10-07): 9/9 Feishu-bound users are
# ``ou_`` + 32 lowercase hex; the admin-created account is a canonical
# lowercase UUID. The predicate is deliberately wider than the sample
# (org-improver ruling): a too-narrow id shape would parse a future valid sub
# as a name and break addressing; a too-broad one only refuses implausible
# names.
_FEISHU_USER_ID_SEGMENT = re.compile(r"u\.ou_[0-9a-f]{16,64}")
_UUID_USER_ID_SEGMENT = re.compile(  # any UUID version
    r"u\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_DEVICE_ID_SEGMENT = re.compile(r"d\.[0-9a-f]{32}")
_AGENT_ID_SEGMENT = re.compile(r"a\.[a-z2-7]{26}")
_THREAD_ID_SEGMENT = re.compile(r"t\.[a-z2-7]{26}")
_CELL_ID_SEGMENT = re.compile(r"c\.[a-z2-7]{26}")
_GROUP_ID_SEGMENT = re.compile(r"g\.[a-z2-7]{26}")


def is_user_id_segment(value: str) -> bool:
    """Whether *value* is an exact P1 user-id address segment."""

    return bool(
        _FEISHU_USER_ID_SEGMENT.fullmatch(value)
        or _UUID_USER_ID_SEGMENT.fullmatch(value)
    )


def is_device_id_segment(value: str) -> bool:
    """Whether *value* is an exact P1 device-id address segment."""

    return _DEVICE_ID_SEGMENT.fullmatch(value) is not None


def is_agent_id_segment(value: str) -> bool:
    """Whether *value* is an exact P1 agent-id address segment."""

    return _AGENT_ID_SEGMENT.fullmatch(value) is not None


def is_thread_id_segment(value: str) -> bool:
    """Whether *value* is an exact thread-id (``t.``) segment."""

    return _THREAD_ID_SEGMENT.fullmatch(value) is not None


def is_cell_id_segment(value: str) -> bool:
    """Whether *value* is an exact cell-id (``c.``) segment."""

    return _CELL_ID_SEGMENT.fullmatch(value) is not None


def is_group_id_segment(value: str) -> bool:
    """Whether *value* is an exact group conversation id (``g.``)."""

    return _GROUP_ID_SEGMENT.fullmatch(value) is not None


def is_identity_id_segment(value: str) -> bool:
    """Whether *value* has any exact immutable identity-id shape."""

    return (
        is_user_id_segment(value)
        or is_device_id_segment(value)
        or is_agent_id_segment(value)
        or is_thread_id_segment(value)
        or is_cell_id_segment(value)
        or is_group_id_segment(value)
    )


def _mint_stable_id(prefix: str, domain: bytes, seed: bytes | None) -> str:
    material = secrets.token_bytes(16) if seed is None else seed
    digest = hashlib.sha256(domain + material).digest()[:16]
    return prefix + base64.b32encode(digest).decode("ascii").rstrip("=").lower()


def mint_thread_id(seed: bytes | None = None) -> str:
    """A ``t.`` id; deterministic for one *seed*, random without one."""

    return _mint_stable_id("t.", b"hyprial-thread-id-v1\0", seed)


def mint_cell_id(seed: bytes | None = None) -> str:
    """A ``c.`` id; deterministic for one *seed*, random without one."""

    return _mint_stable_id("c.", b"hyprial-cell-id-v1\0", seed)


def mint_group_id(seed: bytes | None = None) -> str:
    """A group conversation ``g.`` id; deterministic for one *seed*, random without one."""

    return _mint_stable_id("g.", b"hyprial-group-id-v1\0", seed)


_CELL_KIND = re.compile(r"[a-z][a-z0-9-]{0,63}")


def _framed_seed(tag: bytes, *parts: str) -> bytes:
    framed = bytearray(tag)
    for part in parts:
        encoded = part.encode("utf-8")
        framed += len(encoded).to_bytes(4, "big") + encoded
    return bytes(framed)


def _require_cell_kind(kind: str) -> None:
    if not isinstance(kind, str) or _CELL_KIND.fullmatch(kind) is None:
        raise ValueError(f"cell kind must match [a-z][a-z0-9-]{{0,63}}: {kind!r}")


def keyed_cell_id(kind: str, authority: str, key: str) -> str:
    """The ``c.`` id of the *kind* cell that *authority* binds to *key*.

    Deterministic, so every node that knows (kind, authority, key) derives the
    same id without coordination.  Fields are length-framed under a ``keyed``
    tag, so no choice of key can collide with another kind, authority or a
    :func:`subject_cell_id`.
    """

    _require_cell_kind(kind)
    if not isinstance(authority, str) or not isinstance(key, str):
        raise ValueError("cell authority and key must be strings")
    return mint_cell_id(_framed_seed(b"keyed\0", kind, authority, key))


def subject_cell_id(kind: str, subject: str) -> str:
    """The ``c.`` id of the *kind* cell whose identity is one stable subject id.

    Used where the subject id is already globally unique (an agent's ``a.``
    id), so the cell id needs no authority: ``subject_cell_id('agent', a.x)``
    is the agent cell on every node.
    """

    _require_cell_kind(kind)
    if not isinstance(subject, str) or not subject:
        raise ValueError("cell subject must be a non-empty string")
    return mint_cell_id(_framed_seed(b"subject\0", kind, subject))


def canonical_agent_uri(owner: str, machine: str, actor: str) -> str:
    """Compose the four-segment ``agent:<owner>:<machine>:<actor>`` URI."""

    for label, value in (("owner", owner), ("machine", machine), ("actor", actor)):
        if not value or not value.strip():
            raise ValueError(f"agent identity {label} must not be empty")
        if ":" in value:
            raise ValueError(f"agent identity {label} must not contain ':'")
    return f"{AGENT_URI_PREFIX}{owner}:{machine}:{actor}"


def member_actor_uri(name: str, member: str) -> str:
    """Encode a member launch as a reversible public actor segment."""

    if member == name:
        return name
    if not name or not member or ":" in name or ":" in member:
        raise ValueError("member actor name and member must be non-empty and colon-free")
    return f"{name}@{quote(member, safe='-._~')}"


def member_actor_parts(value: str) -> tuple[str, str] | None:
    """Decode a member actor segment, preserving legacy bare names."""

    if "@" not in value:
        return None
    name, encoded_member = value.split("@", 1)
    member = unquote(encoded_member)
    if not name or not member or member == name or "@" in name or ":" in name or ":" in member:
        return None
    if member_actor_uri(name, member) != value:
        return None
    return name, member


def member_actor_registry_key(value: str) -> str:
    """Map a public member actor to one legal, reversible registry key."""

    parts = member_actor_parts(value)
    if parts is None:
        return value
    name, member = parts
    payload = base64.urlsafe_b64encode(f"{name}\0{member}".encode()).decode().rstrip("=")
    return f"m_{payload}"


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


#: The ``orgfs:<owner>:<spaceId>:<nodeId>`` shared-folder node prefix.  The
#: owner segment is ``SpaceInfo.owner`` with its ``user:`` prefix STRIPPED
#: (a segment may never contain ``:``); readers that need a deliverable
#: address re-attach it through :func:`canonical_user_uri`.
ORGFS_URI_PREFIX = "orgfs:"
_ORGFS_URI_SEGMENTS = 4


def canonical_orgfs_uri(owner: str, space_id: str, node_id: str) -> str:
    """Compose the four-segment ``orgfs:<owner>:<spaceId>:<nodeId>`` URI.

    Same purity bar as :func:`canonical_agent_uri`, plus the ``/`` ban that
    keeps the HTTP path mapping (``/<owner>/<spaceId>/<nodeId>``) bijective:
    every segment is non-empty, contains no ``:`` and no ``/``, and equals
    its own ``.strip()``.  No escaping and no percent-encoding, ever.
    """

    for label, value in (
        ("owner", owner),
        ("spaceId", space_id),
        ("nodeId", node_id),
    ):
        if not value or value.strip() != value:
            raise ValueError(
                f"orgfs identity {label} must be non-empty without "
                "surrounding whitespace"
            )
        if ":" in value:
            raise ValueError(f"orgfs identity {label} must not contain ':'")
        if "/" in value:
            raise ValueError(f"orgfs identity {label} must not contain '/'")
    return f"{ORGFS_URI_PREFIX}{owner}:{space_id}:{node_id}"


def parse_orgfs_uri(value: str) -> tuple[str, str, str] | None:
    """Decompose a canonical ``orgfs:<owner>:<spaceId>:<nodeId>`` URI.

    Returns ``(owner, space_id, node_id)`` for exactly the valid shape,
    ``None`` for every other shape (including a ``user:``-prefixed owner,
    which splits into five segments).  This is the ONLY orgfs-URI
    deconstructor — the same one-reader rule as :func:`parse_agent_uri`.
    """

    if not value.startswith(ORGFS_URI_PREFIX):
        return None
    parts = value.split(":")
    if len(parts) != _ORGFS_URI_SEGMENTS or not all(parts[1:]):
        return None
    if any(part.strip() != part or "/" in part for part in parts[1:]):
        # Same whitespace-padded-segment rejection as parse_agent_uri
        # (2026-09-14 S4), plus the orgfs ``/`` ban.
        return None
    return parts[1], parts[2], parts[3]


@dataclass(frozen=True, slots=True)
class OrgfsWebNetwork:
    """The local-node network facts :func:`orgfs_web_url` derives a host from.

    Every field is optional: a node that is not on a tailnet (or whose name
    is unknown) derives NO web host — surfaces show the bare URI and a host
    is never guessed.
    """

    #: Explicit ``orgfs.web_host`` config override — a bare host name, for
    #: orgs whose fs service is not at ``fs.<tailnet domain>``.
    web_host: str | None = None
    #: ``CurrentTailnet.MagicDNSSuffix`` from the node's own tailscale status
    #: — authoritative when present, needs no label-stripping.
    magic_dns_suffix: str | None = None
    #: The node's own tailnet FQDN (``Self.DNSName`` on the host-tailnet join
    #: path, ``state/tsnet/node.json`` ``hostname`` on the sidecar path).
    fqdn: str | None = None


def orgfs_web_host(network: OrgfsWebNetwork) -> str | None:
    """The per-tailnet web host for orgfs links, or None when underivable.

    Precedence: the explicit ``orgfs.web_host`` override, then ``fs.`` + the
    MagicDNS suffix (preferred over the FQDN — it is authoritative), then
    ``fs.`` + the node's own FQDN minus its first label.  Never a constant:
    the host belongs to the viewer's own tailnet.
    """

    override = (network.web_host or "").strip().rstrip(".")
    if override:
        return override
    suffix = (network.magic_dns_suffix or "").strip().rstrip(".")
    if suffix:
        return f"fs.{suffix}"
    fqdn = (network.fqdn or "").strip().rstrip(".")
    if fqdn:
        labels = fqdn.split(".")
        if len(labels) > 1 and all(labels[1:]):
            return "fs." + ".".join(labels[1:])
    return None


def orgfs_web_url(value: str, *, network: OrgfsWebNetwork) -> str | None:
    """Derive the read-only web URL for one canonical orgfs node URI.

    ``https://<host>/<owner>/<spaceId>/<nodeId>`` — the three payload
    segments verbatim (the grammar's ``:``/``/`` bans make the mapping a
    pure bijection).  Render-time derivation only: the URL is never
    persisted and is absent (None) when no host is derivable.  Raises
    ``ValueError`` when ``value`` is not a canonical orgfs URI.
    """

    parsed = parse_orgfs_uri(value)
    if parsed is None:
        raise ValueError(f"not a canonical orgfs URI: {value!r}")
    host = orgfs_web_host(network)
    if host is None:
        return None
    owner, space_id, node_id = parsed
    return f"https://{host}/{owner}/{space_id}/{node_id}"


ADAPTER_URI_PREFIX = "adapter:"


def parse_adapter_uri(value: str) -> tuple[str, str, str] | None:
    """Decompose an ``adapter:<owner>:<machine>:<name>`` adapter address.

    The one reader of the four-segment adapter address; ``None`` for any
    other shape, including the retired ``channel:`` spelling.  The
    three-segment ``adapter:<platform>:<name>`` form belongs to
    :func:`parse_adapter_short_uri`.
    """

    if not value.startswith(ADAPTER_URI_PREFIX):
        return None
    parts = value.split(":")
    if len(parts) != 4 or not all(parts[1:]):
        return None
    return parts[1], parts[2], parts[3]


def parse_adapter_short_uri(value: str) -> tuple[str, str] | None:
    """Decompose the three-segment ``adapter:<platform>:<name>`` form.

    The reply-bridge spelling a platform worker uses as a sender, for example
    ``adapter:lark:<name>``.  The two forms never overlap: this one has
    exactly three non-empty segments, the four-segment form belongs to
    :func:`parse_adapter_uri`.
    """

    if not value.startswith(ADAPTER_URI_PREFIX):
        return None
    parts = value.split(":")
    if len(parts) != 3 or not all(parts[1:]):
        return None
    return parts[1], parts[2]


USER_URI_PREFIX = "user:"


@dataclass(frozen=True, slots=True)
class AddressFamily:
    name: str
    grammar: str
    usage: str | None
    setup: str | None
    escalation_supported: bool
    escalation_addresses: tuple[str, ...]


ADDRESS_FAMILIES: dict[str, AddressFamily] = {
    "agent": AddressFamily(
        "agent", "agent:<owner>:<machine>:<actor>", None, None, True, ()
    ),
    "user": AddressFamily("user", "user:<owner>", None, None, True, ()),
}


def delivery_address_error(value: str) -> str | None:
    """``None`` when ``value`` is a deliverable address shape, else the reason.

    Deliverable shapes today: a canonical agent URI or ``user:<owner>``.
    The ``route:<adapter>:<route>`` address is retired (I4b, 2026-11-24) and
    gets the generic answer like any other shape.  Bare names are NOT addresses either — they parse as nothing and land in
    stores nobody reads.
    """

    if not isinstance(value, str) or not value.strip():
        return "address must be a non-empty string"
    candidate = value.strip()
    if parse_agent_uri(candidate) is not None:
        return None
    if parse_user_uri(candidate) is not None:
        return None
    return (
        f"{candidate!r} is not a deliverable address; write a full agent URI "
        "(agent:<owner>:<machine>:<actor>) or user:<owner> — bare names are "
        "undeliverable"
    )


def short_actor_name(value: str) -> str:
    """Display form: the actor segment of a canonical URI, else the value."""

    parsed = parse_agent_uri(value) or parse_adapter_uri(value)
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


def legacy_user_uri(owner: object) -> str:
    """Reproduce the pre-validation ``user:<owner>`` spelling exactly.

    This compatibility escape hatch is only for migration inputs and existing
    stored records that predate :func:`canonical_user_uri` validation.  New
    identity construction must use the canonical helper instead.
    """

    return USER_URI_PREFIX + str(owner)


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


def is_own_user_uri(value: object, owner: str | None) -> bool:
    """Whether *value* is the strict ``user:<owner>`` URI of *owner*.

    The one test for "a message to this node's own person": a ``user:``
    address of another owner, a malformed spelling or a missing owner is
    not.  *owner* is the bare owner key, as ``canonical_user_uri`` takes it.
    """

    return (
        isinstance(value, str)
        and bool(owner)
        and parse_user_uri(value) == owner
    )


TARGET_KIND_AGENT = "agent"
TARGET_KIND_HOST = "host"
TARGET_KIND_USER = "user"
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
        TARGET_KIND_UNKNOWN,
    }
)
