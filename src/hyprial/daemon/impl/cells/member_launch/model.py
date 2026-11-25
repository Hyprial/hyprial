"""Typed identity and local facts for one member's harness launch."""

from __future__ import annotations

from dataclasses import dataclass

from hyprial.kernel import MemberLaunchKey


@dataclass(frozen=True, slots=True)
class MemberLaunchFact:
    """The last locally observed process facts for a member launch."""

    key: MemberLaunchKey
    running: bool
    process_token: str | None = None
    pid: int | None = None
    process_identity: str | None = None
    session_ref: str | None = None


__all__ = ["MemberLaunchFact", "MemberLaunchKey"]
