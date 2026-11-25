# Decision record:
# - Why a serving application stopped is policy, so it is recorded here and
#   not read back from the diagnostic ``_StopRequestTrace`` (whose docstring
#   says it carries no stop policy and whose ``last`` is overwritten by the
#   most recent request).
# - ``logout`` is honoured only when the logout request is the only kind of
#   stop that was ever requested for this run.  Any other source (signal,
#   IPC shutdown, home-ownership loss, restore failure, ...) is sticky and
#   makes the reason ``shutdown`` whatever order the requests arrived in, so a
#   SIGTERM can never be turned into a reopened bootstrap by a later or an
#   earlier logout.  A run that recorded no request at all is ``shutdown``.
# - Two independent monotonic booleans are used instead of one overwritten
#   value, so concurrent requests from a signal handler and a worker thread
#   need no lock and cannot race each other into ``logout``.
"""The explicit stop reason a serving ``DaemonApplication`` reports."""

from __future__ import annotations

from collections.abc import Callable

__all__ = ["STOP_LOGOUT", "STOP_SHUTDOWN", "StopFlags"]

STOP_LOGOUT = "logout"
STOP_SHUTDOWN = "shutdown"


class StopFlags:
    """Sticky stop policy of one application run."""

    __slots__ = ("logout", "other")

    def __init__(self) -> None:
        self.logout = False
        self.other = False

    def note(self, source: str) -> None:
        if source == "auth-logout":
            self.logout = True
        else:
            self.other = True

    def bind(self, callback: Callable[[], None], source: str) -> Callable[[], None]:
        def request() -> None:
            self.note(source)
            callback()

        return request

    @property
    def reason(self) -> str:
        return STOP_LOGOUT if self.logout and not self.other else STOP_SHUTDOWN
