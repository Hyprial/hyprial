"""Kernel identity for a member-scoped harness launch."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, order=True)
class MemberLaunchKey:
    member: str
    harness: str

    def __post_init__(self) -> None:
        if not isinstance(self.member, str) or not self.member.strip():
            raise ValueError("member launch key member must be a non-empty string")
        if not isinstance(self.harness, str) or not self.harness.strip():
            raise ValueError("member launch key harness must be a non-empty string")

    @property
    def value(self) -> tuple[str, str]:
        return self.member, self.harness

    def to_json(self) -> dict[str, str]:
        return {"member": self.member, "harness": self.harness}

    @classmethod
    def from_json(cls, value: object) -> "MemberLaunchKey":
        if not isinstance(value, dict):
            raise ValueError("member launch key must be an object")
        return cls(value.get("member"), value.get("harness"))


__all__ = ["MemberLaunchKey"]
