from __future__ import annotations
from typing import TYPE_CHECKING
from hyprial.daemon import Alarm, AlarmDelivery, AlarmResult

if TYPE_CHECKING:
    from hyprial.daemon.impl.inbox.authority.facade import DeliveryCustodyFacade

class ActorAlarmEmitter:
    """``AlarmEmitter.emit`` compatibility backed by typed actor commands."""

    def __init__(self, authority: DeliveryCustodyFacade) -> None:
        self._authority = authority

    def emit(
        self,
        alarm: Alarm,
        *,
        delivery: AlarmDelivery | None = None,
        terminal: bool = True,
        throttle: bool = True,
    ) -> AlarmResult:
        if delivery is not None:
            return AlarmResult("failed", alarm.audience)
        try:
            return self._authority.emit_alarm(
                alarm,
                terminal=terminal,
                throttle=throttle,
            )
        except (NameError, ImportError):
            raise
        except Exception:
            return AlarmResult("failed", alarm.audience)
