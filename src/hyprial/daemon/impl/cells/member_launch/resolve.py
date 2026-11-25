"""Pure resolution helpers for member launch identity."""

from __future__ import annotations

from collections.abc import Iterable

from hyprial.kernel import HarnessLaunchSpec

from .model import MemberLaunchFact, MemberLaunchKey


def member_name(spec: HarnessLaunchSpec) -> str:
    """Return the explicit member name, or the legacy agent URI."""

    return spec.member or spec.name


def member_launch_key(spec: HarnessLaunchSpec) -> MemberLaunchKey:
    return MemberLaunchKey(member_name(spec), spec.harness)


def resolve_member_launch(
    specs: Iterable[HarnessLaunchSpec], member: str, harness: str | None = None
) -> HarnessLaunchSpec | None:
    """Select one local desired launch by typed member identity."""

    candidates = (
        spec
        for spec in specs
        if member_name(spec) == member and (harness is None or spec.harness == harness)
    )
    return next(candidates, None)


def resolve_member_fact(
    facts: Iterable[MemberLaunchFact], member: str, harness: str | None = None
) -> MemberLaunchFact | None:
    for fact in facts:
        if fact.key.member == member and (
            harness is None or fact.key.harness == harness
        ):
            return fact
    return None


__all__ = [
    "member_launch_key",
    "member_name",
    "resolve_member_fact",
    "resolve_member_launch",
]
