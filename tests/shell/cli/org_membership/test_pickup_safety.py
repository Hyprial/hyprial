"""Post-commit failures are separate, secret-free results."""
import json

import pytest

from hyprial.shell.impl.invites import pending


def test_failure_text_does_not_echo_account_or_bearer_secrets(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise RuntimeError("bearer-private-value refresh-token-private-value")

    monkeypatch.setattr(pending, "run_pending", fail)
    result = pending.pickup_after_login(tmp_path, request=lambda *a: {}, confirm=None)
    assert result["status"] == "failed"
    assert "private-value" not in json.dumps(result)


def test_interrupt_during_pickup_is_a_separate_result(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(pending, "run_pending", fail)
    result = pending.pickup_after_login(tmp_path, request=lambda *a: {}, confirm=None)
    assert result["status"] == "failed"



@pytest.mark.parametrize("json_output,isatty", [(False, False), (True, True), (False, True)])
def test_login_never_requests_bulk_accept(monkeypatch, tmp_path, json_output, isatty):
    # This test also runs against the pre-O2c login implementation.
    import inspect
    from types import SimpleNamespace
    from hyprial.shell.impl.cli.commands.login import aftercare

    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs)
        return {"joined": [], "failed": [], "skipped": 0, "warnings": [],
                "pending": [], "declined": [], "unusable": []}

    monkeypatch.setattr(pending, "run_pending", run)
    monkeypatch.setattr(aftercare, "get_services", lambda: SimpleNamespace(
        runtime=SimpleNamespace(request=lambda *a: {}), _stdin_isatty=lambda: isatty
    ))
    parameters = inspect.signature(aftercare._pickup_org_invites_after_login).parameters
    kwargs = {"json_output": json_output} if "json_output" in parameters else {}
    aftercare._pickup_org_invites_after_login(tmp_path, **kwargs)
    assert calls[0]["accept_all"] is False
    assert (calls[0]["confirm"] is not None) == (isatty and not json_output)
