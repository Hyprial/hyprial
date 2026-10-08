"""AT10: map each frozen capability to the entry point that enforces it.

Card 359 asks for the capability enum to be bound to real use sites --
tool-surface, channel, shared-path, org-context, isolation -- with default-deny
for anything outside the agent's own home, minimum grant, and withdrawal
handled at the point of use.

Recording a grant is not enforcement (that is the AT02 ledger's own rule, and
``hyprial.identity.impl.agents.runtime.grants`` says so in its docstring).  This module is the other
half: it turns ledger records into allow/deny answers *at the entry point*, and
it names that entry point in the reason so an operator can see which lock the
answer came from.

Scope, stated plainly: this is the decision layer the entry points call.  It
does not yet intercept the daemon's own tool broker or filesystem reads -- that
wiring lands with the same verified-identity channel AT09 needs.  Until then
nothing here changes daemon behaviour, and no capability silently becomes an
implicit allow.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

__all__ = [
    "CAPABILITY_ENTRY_POINTS",
    "GRANTABLE_CAPABILITIES",
    "EnforcementError",
    "EnforcementScopeError",
    "Principal",
    "CapabilityRecord",
    "Verdict",
    "check_channel",
    "check_isolation",
    "check_org_context",
    "check_shared_path",
    "check_tool",
    "explain",
]

#: capability -> the concrete entry point that must consult it.
CAPABILITY_ENTRY_POINTS: Mapping[str, str] = {
    "agent-home": "the agent's own home (inherent, never granted to a visitor)",
    "tool-surface": "harness tool broker: the agent's tool allow-list",
    "channel": "outbound channel routes: adapter send paths",
    "shared-path": "filesystem access outside the agent home",
    "org-context": "org shared folder reads",
    "isolation": "execution isolation mode (directory or container)",
}

#: Capabilities that are granted (as opposed to agent-home, which is inherent).
GRANTABLE_CAPABILITIES = (
    "tool-surface",
    "channel",
    "shared-path",
    "org-context",
    "isolation",
)

_FIXED_SCOPES = {
    "org-context": ("accepted",),
    "isolation": ("directory", "container"),
}


class EnforcementError(Exception):
    """Base class for capability enforcement decisions."""

    code = "enforcement_error"


class EnforcementScopeError(EnforcementError):
    """A grant scope does not have the shape its capability requires."""

    code = "enforcement_scope_invalid"


@dataclass(frozen=True)
class Principal:
    """The verified caller (``verified`` is set by the transport, not a field)."""

    uri: str
    hosted: bool = False
    verified: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.uri, str) or not self.uri or self.uri != self.uri.strip():
            raise EnforcementError("principal uri must be a non-empty, unpadded string")


@dataclass(frozen=True)
class CapabilityRecord:
    """The ledger fields a decision needs (mirrors ``CapabilityGrant``)."""

    actor: str
    capability: str
    scope: str
    revision: int = 1

    def __post_init__(self) -> None:
        if self.capability not in CAPABILITY_ENTRY_POINTS:
            raise EnforcementScopeError(f"unknown capability {self.capability!r}")
        if self.capability == "agent-home":
            raise EnforcementScopeError(
                "agent-home is inherent to the agent; it is never granted"
            )
        if type(self.revision) is not int or self.revision < 1:
            raise EnforcementScopeError("revision must be a positive integer")


@dataclass(frozen=True)
class Verdict:
    """One allow/deny answer plus the entry point it came from."""

    allowed: bool
    reason: str
    capability: str
    entry_point: str
    caller: str | None
    resource: str
    revision: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "capability": self.capability,
            "entryPoint": self.entry_point,
            "caller": self.caller,
            "resource": self.resource,
            "revision": self.revision,
        }


def _parse_scope(capability: str, scope: str) -> object:
    fixed = _FIXED_SCOPES.get(capability)
    if fixed is not None:
        if scope not in fixed:
            raise EnforcementScopeError(
                f"{capability} scope must be one of {list(fixed)}"
            )
        return scope
    try:
        value = json.loads(scope)
    except (TypeError, json.JSONDecodeError) as error:
        raise EnforcementScopeError(f"{capability} scope must be one JSON value") from error
    if capability == "shared-path":
        if not isinstance(value, dict) or set(value) != {"path", "mode"}:
            raise EnforcementScopeError("shared-path scope needs exactly path and mode")
        path = value["path"]
        mode = value["mode"]
        if not isinstance(path, str) or not path:
            raise EnforcementScopeError("shared-path needs a non-empty path")
        if mode not in ("ro", "rw"):
            raise EnforcementScopeError("shared-path mode must be ro or rw")
        if not Path(path).is_absolute():
            raise EnforcementScopeError("shared-path must be an absolute path")
        if ".." in Path(path).parts:
            raise EnforcementScopeError("shared-path must not contain ..")
        return {"path": path, "mode": mode}
    if not isinstance(value, list) or not value:
        raise EnforcementScopeError(f"{capability} scope must be a non-empty JSON array")
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise EnforcementScopeError(f"{capability} scope items must be names")
    return tuple(value)


def _recorded(
    caller: str,
    capability: str,
    records: Iterable[CapabilityRecord],
) -> list[CapabilityRecord]:
    return [
        record
        for record in records
        if record.capability == capability and record.actor == caller
    ]


def explain(
    capability: str,
    resource: str,
    *,
    caller: Principal | None = None,
    records: Sequence[CapabilityRecord] = (),
) -> Verdict:
    """Decide whether ``caller`` may use ``capability`` on ``resource``."""

    if capability not in CAPABILITY_ENTRY_POINTS:
        raise EnforcementScopeError(f"unknown capability {capability!r}")
    entry = CAPABILITY_ENTRY_POINTS[capability]

    if caller is None:
        return Verdict(
            allowed=True,
            reason=f"host-local operator at {entry}: no caller identity is imposed",
            capability=capability,
            entry_point=entry,
            caller=None,
            resource=resource,
        )
    if not caller.verified:
        return Verdict(
            allowed=False,
            reason=(
                f"caller identity is not verified by a controlled channel at {entry}"
            ),
            capability=capability,
            entry_point=entry,
            caller=None,
            resource=resource,
        )
    if capability == "agent-home":
        owned = resource == caller.uri or resource.startswith(caller.uri + "/")
        return Verdict(
            allowed=owned,
            reason=(
                f"agent-home is inherent to {caller.uri}"
                if owned
                else f"agent-home does not extend beyond {caller.uri}"
            ),
            capability=capability,
            entry_point=entry,
            caller=caller.uri,
            resource=resource,
        )

    if capability == "shared-path":
        requested = resource.removesuffix("#write")
        requested_path = Path(requested)
        if not requested_path.is_absolute() or ".." in requested_path.parts:
            return Verdict(
                allowed=False,
                reason=(
                    f"shared-path request {requested!r} must be an absolute path "
                    "with no .. parts; containment cannot be decided otherwise"
                ),
                capability=capability,
                entry_point=entry,
                caller=caller.uri,
                resource=resource,
            )

    live = _recorded(caller.uri, capability, records)
    if not live:
        return Verdict(
            allowed=False,
            reason=(
                f"no live {capability} grant for {caller.uri} at {entry}"
                + (" (hosted visitor: default deny)" if caller.hosted else "")
            ),
            capability=capability,
            entry_point=entry,
            caller=caller.uri,
            resource=resource,
        )

    newest = max(record.revision for record in live)
    for record in sorted(live, key=lambda item: item.revision, reverse=True):
        scope = _parse_scope(capability, record.scope)
        if capability == "shared-path":
            granted_path = str(scope["path"])
            mode = str(scope["mode"])
            if mode == "ro" and resource.endswith("#write"):
                continue
            target = Path(resource.removesuffix("#write"))
            if _within(target, Path(granted_path)):
                return Verdict(
                    allowed=True,
                    reason=(
                        f"{capability} grant {mode} covers {target} "
                        f"(revision {record.revision})"
                    ),
                    capability=capability,
                    entry_point=entry,
                    caller=caller.uri,
                    resource=resource,
                    revision=record.revision,
                )
            continue
        if capability == "isolation":
            if resource == scope:
                return Verdict(
                    allowed=True,
                    reason=f"isolation {resource} granted (revision {record.revision})",
                    capability=capability,
                    entry_point=entry,
                    caller=caller.uri,
                    resource=resource,
                    revision=record.revision,
                )
            continue
        if resource in scope:
            return Verdict(
                allowed=True,
                reason=(
                    f"{capability} grant names {resource} "
                    f"(revision {record.revision})"
                ),
                capability=capability,
                entry_point=entry,
                caller=caller.uri,
                resource=resource,
                revision=record.revision,
            )

    return Verdict(
        allowed=False,
        reason=(
            f"{capability} grants exist for {caller.uri} (newest revision {newest}) "
            f"but none covers {resource}"
        ),
        capability=capability,
        entry_point=entry,
        caller=caller.uri,
        resource=resource,
    )


def _within(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or sits inside it.

    Both sides are normalised first, and a path that still carries ``..`` is
    refused by the caller before this runs, so ``relative_to`` cannot be fooled
    by a lexical prefix (``/srv/shared/../../etc`` is not inside ``/srv/shared``).
    """

    if not path.is_absolute() or not root.is_absolute():
        return False
    try:
        Path(os.path.normpath(path)).relative_to(Path(os.path.normpath(root)))
    except ValueError:
        return False
    return True


def check_tool(tool: str, *, caller: Principal | None = None, records=()) -> Verdict:
    return explain("tool-surface", tool, caller=caller, records=records)


def check_channel(channel: str, *, caller: Principal | None = None, records=()) -> Verdict:
    return explain("channel", channel, caller=caller, records=records)


def check_shared_path(
    path: str,
    *,
    write: bool = False,
    caller: Principal | None = None,
    records=(),
) -> Verdict:
    resource = f"{path}#write" if write else path
    return explain("shared-path", resource, caller=caller, records=records)


def check_org_context(*, caller: Principal | None = None, records=()) -> Verdict:
    """org-context is granted only for the accepted document, never per space."""

    return explain("org-context", "accepted", caller=caller, records=records)


def check_isolation(mode: str, *, caller: Principal | None = None, records=()) -> Verdict:
    return explain("isolation", mode, caller=caller, records=records)
