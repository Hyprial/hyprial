"""Immutable accepted-operation observations; never drive lifecycle policy."""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class PendingOperation:
    actor: str
    fence: tuple
    state: Literal["starting", "stopping"]


def capture_operation(operation, read_fence):
    if read_fence is None:
        return ()
    # Transfer first removes its source. Its later target startup is observed
    # by the harness owner, not by relabelling the source reservation.
    spec = operation.source if operation.kind == "transfer" else operation.target
    state = "starting" if operation.kind in {"create", "resume"} else "stopping"
    try:
        fence = read_fence(spec.actor)
    except Exception:
        return ()  # Observation failure cannot refuse or fail an operation.
    if fence is None or (spec.expected_entity_token is not None and fence[0] != spec.expected_entity_token):
        return ()
    return (PendingOperation(spec.actor, fence, state),)
