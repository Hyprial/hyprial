"""Immutable PAC facts consumed by restore policy projections."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PacRestoreFacts:
    terminal: bool = False
    requested: bool = False
    remote_return: bool = False
    #: Every graph holding this actor is ``held`` and none has current work
    #: (#1803 item 5).  Restore leaves such a worker dormant; the coordinator
    #: starts it when a request actually arrives.
    held_idle: bool = False

    @property
    def pending_work(self) -> bool:
        return self.requested or self.remote_return
