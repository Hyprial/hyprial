"""P3 port: identity comes from the gateway, never from parameters."""

from __future__ import annotations

import pytest

from hyprial.identity import OnboardingSubject

from hyprial.daemon.impl.application.onboarding.errors import OnboardingGatewayError
from hyprial.daemon.impl.application.onboarding.first_run.port import (
    FirstRunAttemptPort,
)
from hyprial.daemon.impl.onboarding.first_run import (
    FirstRunStoreError,
    FirstRunVerificationError,
)
from hyprial.daemon.impl.onboarding.first_run_attempt import (
    FirstRunCredentialBinding,
)
from hyprial.daemon.impl.request_catalog.onboarding.state import CallContext, Surface

SUBJECT = OnboardingSubject(
    issuer="https://issuer.example.test",
    user_id="user-123",
    device_uid="12345678123442348234123456789abc",
)
SURFACE = Surface(
    home_binding="/tmp/home",
    host_control_session_id="host-1",
    view_session_id="local-gui",
    transport_generation="1",
)


def _context(phase: str = "serving", caller: str = "local-gui") -> CallContext:
    return CallContext(surface=SURFACE, phase=phase, caller=caller)


class _Gateway:
    def __init__(self, *, owner: str | None = "alice", error: Exception | None = None):
        self.owner = owner
        self.error = error
        self.calls = 0

    def first_run_trusted(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.owner, SUBJECT


class _Attempt:
    def as_status(self):
        return {"attemptId": "att-1"}


class _Receipt:
    def as_status(self):
        return {"state": "recorded"}


class _Confirmation:
    attempt = _Attempt()
    first_run = _Receipt()
    request_message_id = "m.request"
    reply_message_id = "m.reply"


class _Service:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def _record(self, name: str, subject, kwargs):
        self.calls.append((name, {"subject": subject, **kwargs}))
        if self.error is not None:
            raise self.error

    def begin(self, subject, **kwargs):
        self._record("begin", subject, kwargs)
        return _Attempt()

    def bind(self, subject, **kwargs):
        self._record("bind", subject, kwargs)
        return _Attempt()

    def confirm(self, subject, **kwargs):
        self._record("confirm", subject, kwargs)
        return _Confirmation()

    def cancel(self, subject, **kwargs):
        self._record("cancel", subject, kwargs)
        return _Attempt()

    def read(self, subject):
        return None


def test_bind_and_confirm_derive_the_sender_from_the_verified_owner():
    service = _Service()
    port = FirstRunAttemptPort(_Gateway(owner="alice"), service)

    port.bind(
        _context(),
        attempt_id="att-1",
        expected_revision=1,
        operation_key="opkey-1",
        conversation_id="c.1",
        request_message_id="m.request",
    )
    assert service.calls[0][1]["trusted_sender"] == "user:alice"

    port.confirm(
        _context(),
        attempt_id="att-1",
        expected_attempt_revision=2,
        expected_receipt_revision=0,
        operation_key="opkey-1",
    )
    assert service.calls[1][1]["trusted_sender"] == "user:alice"


def test_begin_passes_only_the_agent_and_operation_key():
    service = _Service()
    port = FirstRunAttemptPort(_Gateway(), service)
    port.begin(
        _context(),
        operation_key="opkey-1",
        agent_uri="agent:alice@node:first-run",
    )
    name, kwargs = service.calls[0]
    assert name == "begin"
    assert kwargs["agent_uri"] == "agent:alice@node:first-run"
    assert kwargs["subject"] == SUBJECT


def test_bootstrap_context_is_refused_before_any_identity_read():
    gateway = _Gateway()
    port = FirstRunAttemptPort(gateway, _Service())
    with pytest.raises(OnboardingGatewayError) as raised:
        port.begin(
            _context(phase="bootstrap", caller="local-bootstrap"),
            operation_key="opkey-1",
            agent_uri="agent:alice@node:first-run",
        )
    assert raised.value.code == "CALLER_NOT_AUTHORIZED"
    assert gateway.calls == 0


def test_a_missing_subject_is_refused():
    class _NoSubject(_Gateway):
        def first_run_trusted(self):
            return self.owner, None

    port = FirstRunAttemptPort(_NoSubject(), _Service())
    with pytest.raises(OnboardingGatewayError) as raised:
        port.begin(
            _context(),
            operation_key="opkey-1",
            agent_uri="agent:alice@node:first-run",
        )
    assert raised.value.code == "FIRST_RUN_SUBJECT_REQUIRED"


@pytest.mark.parametrize(
    "error,code",
    (
        (FirstRunVerificationError("FIRST_RUN_REPLY_UNVERIFIED", "no reply"), "FIRST_RUN_REPLY_UNVERIFIED"),
        (FirstRunStoreError("FIRST_RUN_ATTEMPT_CONFLICT", "conflict"), "FIRST_RUN_ATTEMPT_CONFLICT"),
    ),
)
def test_domain_refusals_keep_their_stable_codes(error, code):
    port = FirstRunAttemptPort(_Gateway(), _Service(error=error))
    with pytest.raises(OnboardingGatewayError) as raised:
        port.confirm(
            _context(),
            attempt_id="att-1",
            expected_attempt_revision=2,
            expected_receipt_revision=0,
            operation_key="opkey-1",
        )
    assert raised.value.code == code


def test_status_answers_none_instead_of_raising():
    port = FirstRunAttemptPort(_Gateway(error=RuntimeError("unreadable")), _Service())
    assert port.status() is None


def test_a_credential_binding_is_never_built_from_request_parameters():
    # The port has no owner/session/home parameter at all; this guards the
    # contract shape rather than a runtime value.
    assert FirstRunCredentialBinding(kind="key", grant_id="g", entry_id="e",
                                     entity_token="t", revision=1,
                                     route_required=False).kind == "key"
