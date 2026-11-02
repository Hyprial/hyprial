"""Turn-coverage claims: producer declarations, plus the legacy source table.

Decision record (design §3.1, §5 E2): `scope` is a claim about what a producer
can actually observe. A producer that follows every native turn declares
`whole-session`; a producer that only sees its own deliveries declares
`carrier-deliveries` and must not support a whole-session idle claim. Sessions
whose installed producer never declares anything keep the pre-existing
behaviour through the explicit table below, so an undeclared kind is
carrier-only by default and the session turn module stays harness-neutral.
Adding a source to that table is a coverage claim, not a convenience.
"""

from __future__ import annotations

from hyprial.daemon.impl.harnesses.capabilities import WHOLE_SESSION_SOURCES

TURN_COVERAGE_VALUES = frozenset({"whole-session", "carrier-deliveries"})

# The legacy table is the harness capability table (single source of truth,
# eeea03f727); this module must not spell harness sources itself.
LEGACY_WHOLE_SESSION_SOURCES = WHOLE_SESSION_SOURCES


def declare_turn_coverage(value: object) -> str:
    """Validate one producer declaration; unknown values are rejected."""

    if not isinstance(value, str) or value not in TURN_COVERAGE_VALUES:
        raise ValueError(
            "turnCoverage must be whole-session or carrier-deliveries"
        )
    return value


def whole_session_scope(declared: str | None, *, source: str) -> bool:
    """Resolve the effective claim: explicit declaration, else the legacy table."""

    if declared is not None:
        return declared == "whole-session"
    return source in LEGACY_WHOLE_SESSION_SOURCES
