"""Bootstrap logout and the failures of a promotion, over the real wire host.

Decision: daemon-first-z6b-ported-bootstrap-rules-20261117.  These cases were
once asserted only through the GUI-hosted child (deleted in Z-6b).  The rules
live on in the daemon: an authenticated bootstrap can log out before
initialize, a refused logout keeps the login, a failed removal is durable and
never replayed, and a failure after the journal migrated updates the receipt of
the serving generation.  Every case drives the real wire host in an isolated
ownerless home with the controlled identity of ``wire_support``.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.application.onboarding.bootstrap import wire as wire_module
from hyprial.daemon.impl.application.onboarding.lifecycle import (
    ownerless as ownerless_lifecycle,
)
from hyprial.daemon.impl.request_catalog.onboarding import CallContext
from hyprial.identity import LogoutError, LogoutPartialRemoval
from hyprial.kernel import RequestPortError, daemon_request_once, ipc_errors

from tests.daemon.onboarding.wire.wire_support import OWNER


def _ask(running, method, params=None):
    return daemon_request_once(
        method, params, socket_path=running.host.socket_path, timeout=10.0
    )


def _authenticate(running, key):
    begun = _ask(running, "auth.login.begin", {"intent": "login", "operationKey": key})
    got = _ask(running, "auth.operation.get", {"operationId": begun["operationId"]})
    assert got["operation"]["state"] == "authenticated"


def test_an_authenticated_bootstrap_logs_out_without_initializing(open_wire, ownerless):
    running = open_wire()
    preserved = ownerless.home / "agents" / "test-agent.txt"
    preserved.parent.mkdir()
    preserved.write_text("keep-agent")
    _authenticate(running, "ported-login-0000001")

    receipt = _ask(
        running,
        "auth.logout",
        {"operationKey": "bootstrap-logout-001", "confirm": True},
    )

    assert receipt["state"] == "completed"
    assert receipt["loggedOut"] is True
    assert receipt["issuerRevocation"] == "not-attempted"
    assert running.wait() == wire_module.REOPEN
    dispatcher = running.host.dispatcher
    assert dispatcher.runtime.reopen_required is True
    assert dispatcher.call("onboarding.status", {})["authenticated"] is False
    operation = dispatcher.call("auth.operation.get", {"operationId": receipt["operationId"]})
    assert operation["operation"]["loggedOut"] is True
    replay = dispatcher.call(
        "auth.logout", {"operationKey": "bootstrap-logout-001", "confirm": True}
    )
    assert replay["operationId"] == receipt["operationId"]
    assert preserved.read_text() == "keep-agent"


@pytest.mark.parametrize(
    "params",
    [
        {"operationKey": "bootstrap-logout-refused", "confirm": False},
        {
            "operationKey": "bootstrap-logout-refused",
            "confirm": True,
            "expectedEpoch": "wrong-generation",
        },
    ],
)
def test_a_refused_bootstrap_logout_preserves_the_login(open_wire, params):
    running = open_wire()
    _authenticate(running, "ported-login-0000002")

    with pytest.raises(RequestPortError):
        _ask(running, "auth.logout", params)

    runtime = running.host.dispatcher.runtime
    assert runtime.verified_owner() == OWNER
    assert not runtime.reopen_required


@pytest.mark.parametrize("partial", [False, True])
def test_a_bootstrap_logout_removal_failure_is_durable_and_never_replayed(
    open_wire, monkeypatch, partial
):
    from hyprial.daemon.impl.onboarding import runtime as runtime_module

    running = open_wire()
    _authenticate(running, "ported-login-0000003")
    calls = []

    def fail_remove(home):
        calls.append(home)
        if partial:
            (home / "settings.json").write_text("{}")
            raise LogoutPartialRemoval(
                "TEST_REMOVAL_FAILED", "injected", removed=("settings.owner",)
            )
        raise LogoutError("TEST_REMOVAL_FAILED", "injected")

    monkeypatch.setattr(runtime_module, "remove_logout_artifacts", fail_remove)
    for key in ("failed-logout-0001", "failed-logout-0002"):
        receipt = _ask(running, "auth.logout", {"confirm": True, "operationKey": key})
        assert receipt["state"] == "failed"
        assert receipt["loggedOut"] is False
        assert receipt["removed"] == (["settings.owner"] if partial else [])
    assert len(calls) == 1
    assert not running.host.dispatcher.runtime.reopen_required
    with pytest.raises(RequestPortError) as refused:
        _ask(running, "onboarding.initialize", {"operationKey": "init-after-failed-logout"})
    assert refused.value.code == (
        ipc_errors.IDENTITY_UNBOUND if partial else ipc_errors.DAEMON_NOT_READY
    )
    runtime = running.host.dispatcher.runtime
    assert runtime.claimed_operation_id is None
    assert running.host.dispatcher.take_post_reply() is None
    assert running.host.outcome is None


@pytest.mark.parametrize("failure, expected", [("install", "failed"), ("run", "unknown")])
def test_a_failure_after_the_migration_updates_the_serving_generation_receipt(
    open_wire, failure, expected
):
    running = open_wire()
    host = running.host
    _authenticate(running, "ported-login-0000004")
    accepted = _ask(
        running, "onboarding.initialize", {"operationKey": "ported-init-0000001"}
    )
    assert accepted["state"] == "promoting"
    assert running.wait() == wire_module.PROMOTE
    runtime = host.dispatcher.runtime

    class Application:
        epoch = "migrated-generation"

        def install_onboarding(self, port):
            if failure == "install":
                raise RuntimeError("injected install failure")

        def run(self, *, ownership_stream, identity_transaction_stream):
            ownership_stream.close()
            identity_transaction_stream.close()
            raise RuntimeError("injected run failure")

    def construct():
        return ownerless_lifecycle._construct_promoted_application(
            runtime,
            host,
            lambda *_: Application(),
            None,
            SimpleNamespace(requested=False),
        )

    with pytest.raises(RuntimeError, match="injected"):
        runtime.run_daemon(construct)

    surface = replace(
        host.dispatcher.context.surface, transport_generation="migrated-generation"
    )
    record = runtime.operation_service.get(
        runtime.call_context(surface=surface, owner=OWNER), accepted["operationId"]
    )
    assert record.state == expected


def test_the_initialize_receipt_completes_only_at_ready_on_the_migrated_generation(open_wire):
    running = open_wire()
    host = running.host
    _authenticate(running, "ported-login-0000005")
    accepted = _ask(
        running, "onboarding.initialize", {"operationKey": "ported-init-0000002"}
    )
    assert accepted["state"] == "promoting" and accepted["initialized"] is False
    assert running.wait() == wire_module.PROMOTE
    runtime = host.dispatcher.runtime
    surface = replace(
        host.dispatcher.context.surface, transport_generation="serving-generation-2"
    )
    context = CallContext(
        surface=surface, phase="serving", caller="local-gui", owner=OWNER
    )
    states = []

    class Application:
        epoch = "serving-generation-2"
        stop_reason = "shutdown"

        def install_onboarding(self, port):
            self.port = port

        def run(self, *, ownership_stream, identity_transaction_stream):
            try:
                states.append(
                    runtime.operation_service.get(context, accepted["operationId"]).state
                )
                self.port.on_daemon_ready()
                self.port.on_daemon_ready()
                states.append(
                    runtime.operation_service.get(context, accepted["operationId"]).state
                )
            finally:
                ownership_stream.close()
                identity_transaction_stream.close()

    runtime.run_daemon(
        lambda: ownerless_lifecycle._construct_promoted_application(
            runtime,
            host,
            lambda *_: Application(),
            None,
            SimpleNamespace(requested=False),
        )
    )

    assert states == ["promoting", "completed"]
    record = runtime.operation_service.get(context, accepted["operationId"])
    assert record.state == "completed"
    assert record.surface.transport_generation == "serving-generation-2"
