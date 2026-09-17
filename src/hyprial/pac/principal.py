"""PAC principal grammar: the one validation/classification point for owners.

A PAC principal is the address of a responsible party, stored verbatim:

- a person:   ``user:<owner>``
- an agent:   ``agent:<owner>:<machine>:<actor>``

The two grammars themselves live in :mod:`hyprial.uri` (the one-reader leaf).
This module adds what only the PAC boundary rules on (design
``design-pac-owner-full-uri`` §1):

- **shape**: exactly one of the two canonical forms.  Bare short names,
  ``agent:squire`` (two segments), hosts, and ``adapter:``/``route:``/
  ``channel:`` addresses are NOT principals -- people and agents are
  responsible parties; routes are delivery exits.
- **character hygiene**: no control characters, no whitespace inside or
  around any segment.  The underlying agent parser only guarantees four
  non-empty segments; claiming it already rejects these would be false.
- **no normalization**: never trimmed, lowercased, or alias-folded.  A value
  that needs fixing is rejected naming the offending field.

Validation deliberately says nothing about existence or reachability: a
fully-specified URI the local node does not know is a legal owner (D1 §1.3).
"""

from __future__ import annotations

from dataclasses import dataclass

from hyprial.uri import parse_agent_uri, parse_user_uri, uri_scheme

from .errors import PAC_PRINCIPAL_SHAPE_INVALID, PacError

PRINCIPAL_KIND_AGENT = "agent"
PRINCIPAL_KIND_USER = "user"

@dataclass(frozen=True, slots=True)
class Principal:
    """A validated principal URI and its decomposition."""

    uri: str
    kind: str  # PRINCIPAL_KIND_USER | PRINCIPAL_KIND_AGENT
    owner: str
    machine: str | None = None
    actor: str | None = None

    @property
    def short_name(self) -> str:
        """The name a bare authoring input would use for this principal.

        For a person that is the owner; for an agent the actor segment --
        matching how short names were used before the URI boundary.
        """

        return self.actor if self.kind == PRINCIPAL_KIND_AGENT else self.owner


def _reject_characters(value: str) -> None:
    for character in value:
        if character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F:
            raise PacError(
                PAC_PRINCIPAL_SHAPE_INVALID,
                f"principal {value!r} contains whitespace or control characters; "
                "PAC stores the address verbatim and never normalizes it",
                {"principal": value},
            )


def parse_principal(value: str) -> Principal:
    """Validate one principal URI, raising :class:`PacError` on any deviation."""

    if not isinstance(value, str) or not value:
        raise PacError(
            PAC_PRINCIPAL_SHAPE_INVALID,
            "a principal must be a non-empty user:<owner> or "
            "agent:<owner>:<machine>:<actor> URI",
            {"principal": value},
        )
    _reject_characters(value)
    owner = parse_user_uri(value)
    if owner is not None:
        return Principal(uri=value, kind=PRINCIPAL_KIND_USER, owner=owner)
    parsed_agent = parse_agent_uri(value)
    if parsed_agent is not None:
        owner, machine, actor = parsed_agent
        return Principal(
            uri=value,
            kind=PRINCIPAL_KIND_AGENT,
            owner=owner,
            machine=machine,
            actor=actor,
        )
    detail = "not a canonical principal URI"
    scheme = uri_scheme(value)
    if scheme in ("host", "adapter", "route", "channel"):
        detail = (
            "hosts, adapters, and routes are delivery exits, not responsible "
            "parties; an owner must be a person (user:) or an agent (agent:)"
        )
    elif scheme == "user":
        detail = (
            "a user principal is exactly user:<owner> with a non-empty owner "
            "that contains no ':' and no surrounding whitespace"
        )
    elif scheme == "agent":
        detail = "an agent principal needs all four segments agent:<owner>:<machine>:<actor>"
    elif scheme is None:
        detail = (
            "bare short names are authoring input only; resolve them to a full "
            "URI before the write boundary (unique completion or explicit choice)"
        )
    raise PacError(
        PAC_PRINCIPAL_SHAPE_INVALID,
        f"owner {value!r} is not a storable principal: {detail}",
        {"principal": value},
    )


def principal_kind(value: str) -> str | None:
    """Classify a stored owner for projections: agent / user / None (legacy).

    ``None`` is the honest verdict for pre-URI short names: projections must
    not guess a kind for a value that predates the boundary.
    """

    if parse_user_uri(value) is not None:
        return PRINCIPAL_KIND_USER
    if parse_agent_uri(value) is not None:
        return PRINCIPAL_KIND_AGENT
    return None


def principal_matches_local_actor(
    uri: str, *, owner: str, machine: str, actor_name: str
) -> bool:
    """Full execution binding match: owner AND machine AND actor all equal.

    A foreign same-named agent never associates with a local runtime, and a
    same-owner different-actor URI does not either (design §5.3 context row).
    """

    parsed = parse_agent_uri(uri)
    if parsed is None:
        return False
    return parsed == (owner, machine, actor_name)
