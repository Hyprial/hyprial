"""Strict read projection for the P1 first-run receipt.

Decision: web-auth-r3-p1-20261118.  The storage authority lives in
``daemon/impl/onboarding/first_run.py``; this module only defines the public
status projection so the large onboarding state module stays within the
repository line limit.  Login and device readiness remain in the existing
status fields and are never copied into this object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .schemas import FIRST_RUN_CONFIRMED_STEPS, FIRST_RUN_STATES

_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]{1,6})?Z$"
)


@dataclass(frozen=True, slots=True)
class FirstRunSnapshot:
    """The read-only first-run receipt projection for the current subject.

    Only the confirmation facts that cannot be derived from the live account,
    device, configuration or message authorities are stored.  Login and device
    readiness remain in ``authenticated``/``deviceReady``/``initialized`` and
    are not copied into this object.  ``state`` distinguishes no history from
    a corrupt or unsupported record; the projection never invents completion.
    """

    state: str = "none"
    revision: int = 0
    confirmed: tuple[str, ...] = ()
    updated_at: str | None = None
    skipped: bool = False

    def __post_init__(self) -> None:
        if self.state not in FIRST_RUN_STATES:
            raise ValueError("unknown first-run state")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("first-run revision must be a non-negative integer")
        if not isinstance(self.confirmed, tuple):
            object.__setattr__(self, "confirmed", tuple(self.confirmed))
        if type(self.skipped) is not bool:
            raise ValueError("first-run skipped must be boolean")
        if any(step not in FIRST_RUN_CONFIRMED_STEPS for step in self.confirmed):
            raise ValueError("unknown first-run confirmation")
        if len(set(self.confirmed)) != len(self.confirmed):
            raise ValueError("first-run confirmations must be unique")
        if self.updated_at is not None:
            if (
                not isinstance(self.updated_at, str)
                or _TIMESTAMP.fullmatch(self.updated_at) is None
            ):
                raise ValueError("first-run updatedAt must be a UTC timestamp")
            try:
                datetime.fromisoformat(self.updated_at[:-1] + "+00:00")
            except ValueError as error:
                raise ValueError(
                    "first-run updatedAt must be a calendar time"
                ) from error
        if self.state == "recorded":
            if self.revision < 1 or self.updated_at is None:
                raise ValueError(
                    "a recorded first-run receipt requires a revision and timestamp"
                )
        elif self.revision != 0 or self.confirmed or self.updated_at is not None or self.skipped:
            raise ValueError(
                "a non-recorded first-run state cannot carry receipt facts"
            )

    def as_status(self) -> dict:
        return {
            "state": self.state,
            "revision": self.revision,
            "confirmed": list(self.confirmed),
            "updatedAt": self.updated_at,
            "skipped": self.skipped,
        }
