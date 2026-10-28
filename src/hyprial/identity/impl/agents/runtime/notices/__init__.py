"""Typed non-secret notices from agent runtime preparation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentRuntimeNotice:
    """A non-secret operator notice produced while preparing a runtime."""

    code: str
    actor: str
    message: str
    path: str

    def to_json(self) -> dict[str, str]:
        return {
            "code": self.code,
            "actor": self.actor,
            "message": self.message,
            "path": self.path,
        }
