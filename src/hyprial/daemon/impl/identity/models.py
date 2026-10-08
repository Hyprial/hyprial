"""Private records and source vocabulary for the identity resolver."""

from __future__ import annotations

from dataclasses import dataclass


LEGACY_SOURCES = frozenset({"legacy-identities", "legacy-user-bind"})

SOURCES = frozenset(
    {
        "casdoor-login",
        "org-directory",
        "local-override",
        "legacy-identities",
        "legacy-user-bind",
    }
)


@dataclass(frozen=True, slots=True)
class LegacyRow:
    adapter: str
    open_id: str
    union_id: str | None
    owner: str | None
    standing: str
    observed_at_ms: int | None
    display_name: str | None
    source: str


@dataclass(frozen=True, slots=True)
class Candidate:
    user_key: str
    kind: str
    owner: str | None
    source: str
    confirmed_by: str | None
    updated_at: int | None


__all__ = ["Candidate", "LEGACY_SOURCES", "LegacyRow", "SOURCES"]
