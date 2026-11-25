# Decision record (UUID addressing, docs/design/uuid-addressing-migration-path.md):
# - P2 (2026-11-03): the registry, the Agent actor projection and the daemon
#   directory facade each parsed agent URIs on their own.  They share this one
#   resolver so an ``a.`` id segment is read the same way on every face.
# - The official address of a registered agent is the ``uri`` its record
#   stores; only names with no record (daemon service actors such as squire)
#   are composed from the name.  P3 changes what the record stores, not this
#   function.
# - P3 staging (2026-11-03): every record URI is minted by mint_agent_uri.
#   ID_FORM_MINTING stays False while the P2/P3/P4 lanes land on the
#   integration branch so the suite stays green; the flip commit sets it True
#   together with the test migration, and a later commit deletes the switch.
#   Ruling U1 (no compatibility window) applies from the flip onward.  Ruling
#   X's frozen short names were withdrawn on 2026-11-19: a destroyed agent's
#   name is free, and a new agent with it is a new agent with a new id.
"""One local-agent address resolver shared by every registry read face."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from hyprial.kernel import (
    canonical_agent_uri,
    is_agent_id_segment,
    parse_agent_uri,
)


class AddressedAgent(Protocol):
    @property
    def actor(self) -> str: ...

    @property
    def uri(self) -> str: ...

    @property
    def owner(self) -> str: ...

    @property
    def machine(self) -> str: ...

    @property
    def hosted_by(self) -> str | None: ...


AgentReader = Callable[[str], AddressedAgent | None]

ID_FORM_MINTING = True


def id_form_minting() -> bool:
    """Whether agent records are minted under their ``a.`` id segment."""

    return ID_FORM_MINTING


def name_taken_remedy(name: str) -> str:
    """The AGENT_EXISTS remedy for a taken ``name`` (integrator decision D10).

    Destroying the holder frees the name; an agent created with it later is
    a new agent with a new id.
    """

    return (
        f"To reuse the name, destroy that agent first ('hyprial agent "
        f"destroy {name}', which is irreversible)"
    )


def mint_agent_uri(owner: str, machine: str, *, actor: str, agent_id: str) -> str:
    """The official address a new or re-incarnated agent record stores."""

    return canonical_agent_uri(owner, machine, agent_id if id_form_minting() else actor)


def resolve_local_actor(
    value: str,
    *,
    owner: str,
    machine: str,
    read: AgentReader,
    normalize: Callable[[str], str] | None = None,
) -> str | None:
    """The machine-local actor key behind ``value``, or None if not ours.

    ``read`` answers one key (actor name, stored URI or ``a.`` id) with the
    live record.  An id names exactly one incarnation, so it resolves only to
    a record whose owner and machine match the address it arrived in.  A
    four-segment name URI for another owner resolves only through an exact
    stored hosted URI, as before.
    """

    parsed = parse_agent_uri(value)
    if parsed is not None:
        if is_agent_id_segment(parsed[2]):
            record = read(parsed[2])
            if record is None or (record.owner, record.machine) != parsed[:2]:
                return None
            return record.actor
        if parsed[0] != owner or parsed[1] != machine:
            record = read(value)
            if record is None or record.uri != value or record.hosted_by is None:
                return None
            return record.actor
        candidate = parsed[2]
    elif ":" in value:
        return None
    elif is_agent_id_segment(value):
        record = read(value)
        return None if record is None else record.actor
    else:
        candidate = value
    if normalize is None:
        return candidate
    try:
        return normalize(candidate)
    except Exception:  # noqa: BLE001
        return None


def official_agent_uri(
    actor: str, *, owner: str, machine: str, read: AgentReader
) -> str:
    """The address an agent is reached at: its stored URI when registered."""

    record = read(actor)
    if record is not None:
        return record.uri
    return canonical_agent_uri(owner, machine, actor)


__all__ = [
    "ID_FORM_MINTING",
    "AddressedAgent",
    "AgentReader",
    "id_form_minting",
    "mint_agent_uri",
    "name_taken_remedy",
    "official_agent_uri",
    "resolve_local_actor",
]
