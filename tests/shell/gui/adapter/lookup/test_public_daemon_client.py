"""The GUI setup host must call the existing daemon client, not the CLI entry."""

import pytest

from hyprial.kernel import DaemonRequestError
from hyprial.shell import daemon_request, daemon_request_once


def test_public_ports_select_restore_aware_or_single_attempt_client(monkeypatch):
    import hyprial.kernel as kernel

    seen = []
    monkeypatch.setattr(kernel, "daemon_request", lambda method, params, **kw: seen.append(("normal", method, params)) or {"ok": True})
    monkeypatch.setattr(kernel, "daemon_request_once", lambda method, params, **kw: seen.append(("once", method, params)) or {"ok": True})

    assert daemon_request("agent.list", {}) == {"ok": True}
    assert daemon_request_once("agent.secret.provider-write", {"entryId": "e"}) == {"ok": True}
    assert seen == [
        ("normal", "agent.list", {}),
        ("once", "agent.secret.provider-write", {"entryId": "e"}),
    ]


def test_single_attempt_keeps_unknown_result_data(monkeypatch):
    import hyprial.kernel as kernel

    detail = {"commandAdmission": "accepted", "outcomeKnown": False}

    def unknown(_method, _params, **kwargs):
        raise kernel.RequestPortError("SUBMIT_OUTCOME_UNKNOWN", "pending", detail)

    monkeypatch.setattr(kernel, "daemon_request_once", unknown)
    with pytest.raises(DaemonRequestError) as caught:
        daemon_request_once("agent.secret.provider-write", {"entryId": "e"})
    assert caught.value.code == "SUBMIT_OUTCOME_UNKNOWN"
    assert caught.value.data == detail
