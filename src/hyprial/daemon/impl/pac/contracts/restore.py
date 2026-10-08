"""Immutable PAC facts consumed by restore policy projections."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PacRestoreFacts:
    terminal: bool = False
    requested: bool = False
    remote_return: bool = False

    @property
    def pending_work(self) -> bool:
        return self.requested or self.remote_return
