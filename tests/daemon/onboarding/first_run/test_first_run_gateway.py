"""Serving status binds the account snapshot and the first-run receipt."""

from __future__ import annotations

import pytest

from hyprial.daemon.impl.onboarding.first_run import FirstRunStore
from hyprial.identity import (
    OnboardingSubject,
    read_identity_snapshot,
    read_onboarding_subject,
)
from tests.daemon.onboarding.application.test_onboarding_gateways import (
    ISSUER,
    OWNER,
    _authenticate,
    _begin,
    _context,
    _new_runtime,
    _serving,
    _surface,
    _write_device,
    _write_identity,
)


def test_bootstrap_status_does_not_mix_a_later_identity_with_the_receipt(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    runtime, fake, gateway = _new_runtime(tmp_path)
    try:
        surface = _surface()
        _begin(gateway, surface)
        _authenticate(runtime, fake, surface, user_id="u-1")
        _write_device(runtime.home)
        subject = read_onboarding_subject(runtime.home)
        assert subject is not None
        FirstRunStore(runtime.operation_service.root).confirm(
            subject,
            step="configuration",
            expected_revision=0,
            operation_key="opkey-0123456789abcdef",
            now="2026-11-18T00:00:00Z",
        )
        original = runtime._operations.status

        def switch_after_account_projection(context, **kwargs):
            result = original(context, **kwargs)
            _write_identity(runtime.home, user_id="u-2")
            return result

        monkeypatch.setattr(
            runtime._operations, "status", switch_after_account_projection
        )
        status = gateway.status(_context())
        assert status["firstRun"]["state"] == "unavailable"
    finally:
        runtime.close()


@pytest.mark.parametrize("new_owner", [OWNER, "bob"])
def test_status_does_not_mix_first_account_with_later_receipt(
    tmp_path, monkeypatch, new_owner
):
    service, gateway = _serving(tmp_path)
    home = tmp_path / "home"
    _write_identity(home, user_id="alice")
    _write_device(home)
    gateway._identity_reader = read_identity_snapshot
    FirstRunStore(service.root).confirm(
        OnboardingSubject(
            issuer=ISSUER,
            user_id="bob",
            device_uid="12345678123442348234123456789abc",
        ),
        step="reply",
        expected_revision=0,
        operation_key="first-run-interleave-0001",
        now="2026-11-18T00:00:00Z",
    )
    original = service.status

    def switch_after_account_projection(context, **kwargs):
        result = original(context, **kwargs)
        _write_identity(home, owner=new_owner, user_id="bob")
        return result

    monkeypatch.setattr(service, "status", switch_after_account_projection)
    result = gateway.status(_context("serving", "local-gui"))
    assert result["owner"] == OWNER
    assert result["firstRun"]["state"] != "recorded"


def test_status_does_not_project_a_receipt_for_a_mismatched_device_owner(tmp_path):
    service, gateway = _serving(tmp_path)
    home = tmp_path / "home"
    _write_identity(home, owner="bob", user_id="alice")
    _write_device(home)
    gateway._identity_reader = read_identity_snapshot
    FirstRunStore(service.root).confirm(
        OnboardingSubject(
            issuer=ISSUER,
            user_id="alice",
            device_uid="12345678123442348234123456789abc",
        ),
        step="reply",
        expected_revision=0,
        operation_key="first-run-mismatch-0001",
        now="2026-11-18T00:00:00Z",
    )
    status = gateway.status(_context("serving", "local-gui"))
    assert status["firstRun"]["state"] == "unavailable"
