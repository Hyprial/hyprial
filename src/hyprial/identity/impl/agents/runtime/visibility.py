"""AT09: see-actors / send-to decisions over the host capability ledger.

Card 358 wants ``targets`` and ``send`` to answer per *verified* caller
identity, with three hard rules:

* a hosted visitor with no grant sees only itself;
* a grant is a JSON array of principal URIs, and withdrawal takes effect on the
  next call (the decision reads the ledger, it does not cache);
* the caller identity must come from a controlled channel, never from a
  model-supplied field.

This module owns the *decision* half of that: given the caller, the candidate
rows and the current ledger records it says what may be seen and sent to, and
why.  Wiring it into the daemon needs the verified-identity channel; until that
exists the daemon keeps its present behaviour, and nothing here silently
becomes a product default.

Host-local operations stay compatible: with no caller identity the operator is
unrestricted, which is exactly how ``ps``/``targets`` behave today on the box
that owns the daemon.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

__all__ = [
    "SEE_ACTORS",
    "SEND_TO",
    "VisibilityError",
    "GrantScopeError",
    "CallerIdentity",
    "GrantRecord",
    "Decision",
    "parse_scope",
    "visible_targets",
    "may_send",
    "explain_target",
]

SEE_ACTORS = "see-actors"
SEND_TO = "send-to"


class VisibilityError(Exception):
    """Base class for visibility decisions."""

    code = "visibility_error"


class GrantScopeError(VisibilityError):
    """A grant scope is not the shape the capability requires."""

    code = "visibility_scope_invalid"


@dataclass(frozen=True)
class CallerIdentity:
    """Who is asking, as established by the controlled channel.

    ``verified`` must be set by the transport, never by a payload field: an
    unverified caller is treated as having no identity at all, which is why
    ``visible_targets`` refuses it instead of granting host-local powers.
    """

    uri: str
    hosted: bool = False
    verified: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri or self.uri != self.uri.strip():
            raise VisibilityError("caller uri must be a non-empty, unpadded string")


@dataclass(frozen=True)
class GrantRecord:
    """The ledger fields a decision needs (mirrors ``CapabilityGrant``)."""

    actor: str
    capability: str
    scope: str
    revision: int = 1

    def __post_init__(self) -> None:
        if self.capability not in (SEE_ACTORS, SEND_TO):
            raise GrantScopeError(
                f"{self.capability!r} is not a visibility capability"
            )
        if type(self.revision) is not int or self.revision < 1:
            raise GrantScopeError("revision must be a positive integer")


@dataclass(frozen=True)
class Decision:
    """One allow/deny answer plus the reason an operator can audit."""

    allowed: bool
    reason: str
    capability: str
    caller: str | None
    target: str

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "capability": self.capability,
            "caller": self.caller,
            "target": self.target,
        }


def parse_scope(capability: str, scope: str) -> tuple[str, ...]:
    """Return the principal URIs a grant scope names.

    ``see-actors`` / ``send-to`` scopes are JSON arrays of principal URIs (the
    shape ``hyprial.identity.impl.agents.runtime.grants.validate_scope`` already enforces).
    """

    if capability not in (SEE_ACTORS, SEND_TO):
        raise GrantScopeError(f"{capability!r} is not a visibility capability")
    try:
        value = json.loads(scope)
    except (TypeError, json.JSONDecodeError) as error:
        raise GrantScopeError(f"{capability} scope must be one JSON value") from error
    if not isinstance(value, list) or not value:
        raise GrantScopeError(f"{capability} scope must be a non-empty JSON array")
    principals: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise GrantScopeError(f"{capability} scope items must be principal URIs")
        principals.append(item)
    return tuple(principals)


def _scope_targets(
    caller: str,
    capability: str,
    grants: Iterable[GrantRecord],
) -> tuple[set[str], set[str], int]:
    """Collect what the caller's live grants name.

    Returns ``(principals, mismatched_actors, revision_max)``.  A record whose
    ``actor`` is not the caller is ignored -- it is someone else's grant --
    but counted so the reason can say so.
    """

    principals: set[str] = set()
    mismatched: set[str] = set()
    newest = 0
    for grant in grants:
        if grant.capability != capability:
            continue
        if grant.actor != caller:
            mismatched.add(grant.actor)
            continue
        principals.update(parse_scope(capability, grant.scope))
        newest = max(newest, grant.revision)
    return principals, mismatched, newest


def explain_target(
    target: str,
    *,
    capability: str,
    caller: CallerIdentity | None = None,
    grants: Sequence[GrantRecord] = (),
) -> Decision:
    """Decide one target for one capability, and say why."""

    if capability not in (SEE_ACTORS, SEND_TO):
        raise GrantScopeError(f"{capability!r} is not a visibility capability")

    if caller is None:
        return Decision(
            allowed=True,
            reason="host-local operator: no caller identity is imposed",
            capability=capability,
            caller=None,
            target=target,
        )
    if not caller.verified:
        return Decision(
            allowed=False,
            reason="caller identity is not verified by a controlled channel",
            capability=capability,
            caller=None,
            target=target,
        )
    if target == caller.uri:
        return Decision(
            allowed=True,
            reason="self",
            capability=capability,
            caller=caller.uri,
            target=target,
        )

    principals, mismatched, revision = _scope_targets(
        caller.uri, capability, grants
    )
    if target in principals:
        return Decision(
            allowed=True,
            reason=f"granted by {capability} (revision {revision})",
            capability=capability,
            caller=caller.uri,
            target=target,
        )
    if mismatched:
        return Decision(
            allowed=False,
            reason=(
                f"{capability} grant exists for {sorted(mismatched)[0]}, "
                f"not for {caller.uri}"
            ),
            capability=capability,
            caller=caller.uri,
            target=target,
        )
    return Decision(
        allowed=False,
        reason=(
            f"no {capability} grant: a hosted visitor without a grant sees "
            "only itself"
            if caller.hosted
            else f"no {capability} grant names {target}"
        ),
        capability=capability,
        caller=caller.uri,
        target=target,
    )


def visible_targets(
    rows: Iterable[Mapping[str, object]],
    *,
    caller: CallerIdentity | None = None,
    grants: Sequence[GrantRecord] = (),
    uri_key: str = "targetUri",
) -> list[Mapping[str, object]]:
    """Filter candidate target rows down to the ones the caller may see."""

    visible: list[Mapping[str, object]] = []
    for row in rows:
        uri = row.get(uri_key)
        if not isinstance(uri, str):
            raise VisibilityError(f"target row has no {uri_key}: {row!r}")
        if explain_target(
            uri, capability=SEE_ACTORS, caller=caller, grants=grants
        ).allowed:
            visible.append(row)
    return visible


def may_send(
    target: str,
    *,
    caller: CallerIdentity | None = None,
    grants: Sequence[GrantRecord] = (),
) -> Decision:
    """Whether the caller may deliver to ``target`` (send-to enforcement)."""

    return explain_target(
        target, capability=SEND_TO, caller=caller, grants=grants
    )
