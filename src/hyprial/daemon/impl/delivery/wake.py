"""The human-wake gate in observe mode (cell model phase A, S5; executor Q6).

Decision notes (phase A, S5):

- Design §6 says system messages do not wake humans.  Phase A ships the gate
  in observe mode only: ``observe`` logs ``delivery.wake.would_suppress`` for
  a SYSTEM message on a human wake path and always answers True.  Each
  enforce step is a separate, revertible commit that needs Allen's go.
- Class decides storage only; the class comes from the injected
  ``message_class_of``, never from a second rule list.
- The gate never fails a wake path: a raising class accessor or logger is
  swallowed and the wake proceeds.
"""

from __future__ import annotations

from hyprial.daemon.impl.delivery.ports import (
    WAKE_GATE_OBSERVE,
    ClassOf,
    EnvelopeLike,
    WakeLog,
)
from hyprial.kernel import MessageClass

WOULD_SUPPRESS_EVENT = "delivery.wake.would_suppress"


class ObserveWakeGate:
    """Logs what an enforcing gate would suppress; suppresses nothing."""

    mode = WAKE_GATE_OBSERVE

    def __init__(self, class_of: ClassOf, *, log: WakeLog | None = None) -> None:
        self._class_of = class_of
        self._log = log

    def observe(self, message: EnvelopeLike, *, path: str) -> bool:
        """Record the decision for one human wake; the wake always proceeds."""

        try:
            message_class = self._class_of(message)
        except Exception:  # noqa: BLE001 - observation never blocks a wake
            return True
        if message_class is not MessageClass.SYSTEM or self._log is None:
            return True
        try:
            self._log(
                "info",
                WOULD_SUPPRESS_EVENT,
                messageId=message.message_id,
                recipient=message.recipient,
                path=path,
                messageClass=message_class.value,
                mode=self.mode,
            )
        except Exception:  # noqa: BLE001 - observation never blocks a wake
            pass
        return True


__all__ = ["WOULD_SUPPRESS_EVENT", "ObserveWakeGate"]
