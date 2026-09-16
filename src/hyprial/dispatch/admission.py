"""One dispatch admission policy shared by legacy workflow and PAC paths."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from hyprial.contracts import ipc_errors

DISPATCH_ROLES = frozenset({"plan", "dispatch", "review", "execute"})

_RHYTHM = re.compile(
    r"明早|明天|上班后|下周|\btomorrow\b|\bnext morning\b", re.IGNORECASE
)
_ACCEPTED = re.compile(r"\bACCEPTED\b", re.IGNORECASE)
_HUMAN_GATES_FIELD = re.compile(r"\bhuman_gates\s*:")


def dispatch_gate(
    *,
    target: str,
    capabilities: Mapping[str, Any],
    role: str | None,
    first_output_eta: str | None = None,
    accepted_text: str = "",
    human_gates_declared: bool = False,
    emit: Callable[..., None],
    source: str,
) -> tuple[str, ...]:
    """Reject only an execute dispatch to a known interactive entity.

    ``role=None`` denotes ordinary messaging/receipt observation, not a PAC
    target (whose schema defaults to execute). Unknown facts are observable,
    never treated as headless or as interactive. Rhythm language warns only.
    """

    if role is not None and (not isinstance(role, str) or role not in DISPATCH_ROLES):
        raise ipc_errors.DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            "role must be plan, dispatch, review, or execute",
        )
    if role is not None and not isinstance(capabilities.get("interactive"), bool):
        emit(
            "warn",
            "daemon",
            "dispatch.capabilities_unknown",
            target=target,
            role=role,
            source=source,
        )
    if role == "execute" and capabilities.get("interactive") is True:
        detail = "交互会话只做 plan/dispatch/review；role=execute 必须派给 headless 实体"
        emit(
            "warn",
            "daemon",
            "dispatch.role_mismatch",
            target=target,
            role=role,
            source=source,
        )
        raise ipc_errors.DaemonRequestError(
            ipc_errors.DISPATCH_ROLE_MISMATCH,
            detail,
            {"target": target, "role": role},
        )
    receipt = accepted_text if _ACCEPTED.search(accepted_text) else ""
    if human_gates_declared or (receipt and _HUMAN_GATES_FIELD.search(receipt)):
        return ()
    fields = (("first_output_eta", first_output_eta or ""), ("ACCEPTED", receipt))
    warnings = []
    for field, text in fields:
        match = _RHYTHM.search(text)
        if match is not None:
            warning = (
                f"dispatch.rhythm_lint target={target} field={field} "
                f"word={match.group(0)} (无 human_gates；先警不拒)"
            )
            emit(
                "warn",
                "daemon",
                "dispatch.rhythm_lint",
                target=target,
                field=field,
                word=match.group(0),
                source=source,
                detail=warning,
            )
            warnings.append(warning)
    return tuple(warnings)


__all__ = ["DISPATCH_ROLES", "dispatch_gate"]
