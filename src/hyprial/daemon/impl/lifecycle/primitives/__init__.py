"""Lifecycle primitives: quiesce, strict resume and runtime-context activation."""

from .activation import (
    ActivationError,
    ActivationPort,
    ActivationReceipt,
    RuntimeContextActivation,
)
from .quiesce import Quiescence, quiesce_agent
from .resume import (
    StrictResumeError,
    require_resumable_harness,
    require_resumable_session,
    session_ref_for,
    strict_resume,
)

__all__ = [
    "ActivationError",
    "ActivationPort",
    "ActivationReceipt",
    "Quiescence",
    "RuntimeContextActivation",
    "StrictResumeError",
    "quiesce_agent",
    "require_resumable_harness",
    "require_resumable_session",
    "session_ref_for",
    "strict_resume",
]
