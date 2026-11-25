"""The P3 attempt attaches to the gateway's one trusted status snapshot."""

from __future__ import annotations

from hyprial.daemon.impl.application.onboarding.first_run.port import FirstRunAttemptPort
from hyprial.daemon.impl.request_catalog.onboarding.handlers import onboarding_status
from hyprial.daemon.impl.request_catalog.onboarding.state import CallContext, Surface

SURFACE = Surface(
    home_binding="/tmp/home",
    host_control_session_id="host-1",
    view_session_id="local-gui",
    transport_generation="1",
)
CONTEXT = CallContext(surface=SURFACE, phase="serving", caller="local-gui", owner="alice")


class _Port:
    first_run_attempt = None

    def status(self, _context):
        return {"ok": True, "firstRun": {"state": "none", "revision": 0,
                                         "confirmed": [], "updatedAt": None}}


class _Attempt:
    def status(self):
        return {"attemptId": "att-1", "state": "bound"}


class _App:
    def __init__(self) -> None:
        self.onboarding = _Port()

    @staticmethod
    def onboarding_context(*, trusted_message_origin=None):
        del trusted_message_origin
        return CONTEXT


class _Snapshot:
    def as_status(self):
        return {"attemptId": "att-1", "state": "bound"}


class _Service:
    def read(self, subject):
        return _Snapshot() if subject == "subject-1" else None


def test_status_projection_is_owned_by_the_same_gateway_snapshot():
    app = _App()
    app.onboarding.first_run_attempt = _Attempt()
    result = onboarding_status(app, {})
    assert result["ok"] is True
    assert "firstRunAttempt" not in result


def test_the_gateway_attaches_the_attempt_for_its_trusted_subject():
    projected = {"ok": True}
    FirstRunAttemptPort(object(), _Service()).attach_status(projected, "subject-1")
    assert projected["firstRunAttempt"] == {"attemptId": "att-1", "state": "bound"}


def test_no_trusted_subject_or_attempt_attaches_nothing():
    projected = {"ok": True}
    port = FirstRunAttemptPort(object(), _Service())
    port.attach_status(projected, None)
    port.attach_status(projected, "subject-2")
    assert "firstRunAttempt" not in projected
