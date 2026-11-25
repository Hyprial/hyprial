"""A logout on a bootstrap home that holds no login is an idempotent success.

Decision: L1 (owner 2026-11-21, ``hyprial logout`` is one ``auth.logout``).  The
CLI no longer reads or removes login artifacts itself, so the daemon answers the
"nothing to remove" case: an unauthenticated bootstrap writes a completed
receipt with ``removed: []`` instead of refusing with
``BOOTSTRAP_IDENTITY_UNAVAILABLE``, and a replay reads that same receipt.  The
real wire host serves an isolated ownerless home (``wire_support``).
"""

from __future__ import annotations

from hyprial.daemon.impl.application.onboarding.bootstrap import wire as wire_module
from hyprial.kernel import daemon_request_once


def _ask(running, method, params=None):
    return daemon_request_once(
        method, params, socket_path=running.host.socket_path, timeout=10.0
    )


def test_an_empty_bootstrap_home_logs_out_with_nothing_removed(open_wire, ownerless):
    running = open_wire()
    settings_before = (ownerless.home / "settings.json").read_bytes() if (
        ownerless.home / "settings.json"
    ).exists() else None
    epoch = _ask(running, "ping")["epoch"]

    receipt = _ask(
        running,
        "auth.logout",
        {"operationKey": "empty-logout-000001", "confirm": True, "expectedEpoch": epoch},
    )

    assert receipt["state"] == "completed"
    assert receipt["loggedOut"] is True
    assert receipt["removed"] == []
    assert receipt["issuerRevocation"] == "not-attempted"
    dispatcher = running.host.dispatcher
    replay = dispatcher.call(
        "auth.logout", {"operationKey": "empty-logout-000002", "confirm": True}
    )
    assert replay["operationId"] == receipt["operationId"]
    assert replay["receiptId"] == receipt["receiptId"]
    assert replay["removed"] == []
    operation = dispatcher.call("auth.operation.get", {"operationId": receipt["operationId"]})
    assert operation["operation"]["state"] == "completed"
    assert operation["operation"]["removed"] == []
    assert running.wait() == wire_module.REOPEN
    settings_after = (ownerless.home / "settings.json").read_bytes() if (
        ownerless.home / "settings.json"
    ).exists() else None
    assert settings_after == settings_before
    assert not (ownerless.home / "secrets" / "login.json").exists()
