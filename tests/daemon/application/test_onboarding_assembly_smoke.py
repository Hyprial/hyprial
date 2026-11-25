"""Startup smoke: the daemon's ready boundary still calls the real PID_FILE step.

The account-port assembly rides on ``_write_pid_file``, which the startup
sequence invokes as a callable step; inserting methods between a ``@property``
and its function silently turns three things into properties and only a real
startup path notices.
"""

from __future__ import annotations

import inspect

from hyprial.daemon.impl.application import DaemonApplication


def test_the_ready_boundary_and_link_admission_stay_callables() -> None:
    for name in ("_write_pid_file", "_assemble_onboarding", "onboarding_context"):
        attribute = inspect.getattr_static(DaemonApplication, name)
        assert not isinstance(attribute, property), f"{name} must stay a method"
    assert not hasattr(DaemonApplication, "link_admission")
    assert callable(DaemonApplication._write_pid_file)
    # The startup sequence passes the bound method as the step operation.
    source = inspect.getsource(DaemonApplication._write_pid_file)
    assert "_assemble_onboarding()" in source
