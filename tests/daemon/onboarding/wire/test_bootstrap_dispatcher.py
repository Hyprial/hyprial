"""The transport-neutral bootstrap dispatcher: methods, refusals, locks and record.

Decision: daemon-first-z1-dispatcher-tests-20261115.  One admitted dispatcher is
exercised under the wire policy, the daemon-socket behaviour (the GUI policy
these cases once pinned beside it is deleted, Z-6b).  No transport is involved
here.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from hyprial import __version__
from hyprial.daemon.impl.application.onboarding import _OnboardingAssemblyMixin
from hyprial.daemon.impl.application.onboarding.bootstrap import (
    dispatcher as dispatcher_module,
)
from hyprial.daemon.impl.application.onboarding.bootstrap.dispatcher import (
    BOOTSTRAP_CALLABLE_METHODS,
    ACCOUNT_REQUIRED,
    WIRE_POLICY,
    BootstrapDispatcher,
)
from hyprial.daemon.impl.request_catalog.migration import ROUTER_ONLY_METHODS
from hyprial.kernel import DaemonRequestError, ipc_errors

from wire_support import BUSY, ControlledIdentity, assert_home_released, gateway_options


class _App(_OnboardingAssemblyMixin):
    pass


def _admit(home, policy):
    return BootstrapDispatcher.admit(
        app=_App(),
        home=home.home,
        state_dir=home.state,
        socket_path=home.socket,
        policy=policy,
        gateway_options=gateway_options(),
    )


@pytest.fixture
def dispatcher(ownerless):
    admitted = _admit(ownerless, WIRE_POLICY)
    try:
        yield admitted
    finally:
        admitted.close()


def test_admission_publishes_the_bootstrap_record_and_holds_both_locks(ownerless):
    admitted = _admit(ownerless, WIRE_POLICY)
    try:
        record = json.loads((ownerless.state / "bootstrap.json").read_text())
        assert set(record) == {"pid", "birthIdentity", "epoch", "socket", "phase"}
        assert record["pid"] == os.getpid()
        assert record["epoch"] == admitted.epoch
        assert record["socket"] == str(ownerless.socket)
        assert record["phase"] == "awaiting-auth"
        with pytest.raises(BUSY):
            _admit(ownerless, WIRE_POLICY)
        assert (ownerless.state / "bootstrap.json").exists()
    finally:
        admitted.close()
    assert not (ownerless.state / "bootstrap.json").exists()
    again = _admit(ownerless, WIRE_POLICY)
    again.close()


def test_close_is_idempotent_and_a_closed_dispatcher_is_unavailable(ownerless):
    admitted = _admit(ownerless, WIRE_POLICY)
    admitted.close()
    admitted.close()
    with pytest.raises(ipc_errors.DaemonUnavailableError):
        admitted.call("ping")
    with pytest.raises(ipc_errors.DaemonUnavailableError):
        admitted.call("onboarding.status")


def test_failed_admission_releases_nothing_it_does_not_hold(ownerless):
    first = _admit(ownerless, WIRE_POLICY)
    try:
        with pytest.raises(BUSY):
            _admit(ownerless, WIRE_POLICY)
        record = json.loads((ownerless.state / "bootstrap.json").read_text())
        assert record["epoch"] == first.epoch
        assert first.call("ping")["epoch"] == first.epoch
    finally:
        first.close()


def test_ping_reports_the_bootstrap_phase_without_an_owner(dispatcher, ownerless):
    ping = dispatcher.call("ping")
    assert ping["phase"] == "bootstrap"
    assert ping["owner"] is None
    assert ping["version"] == __version__
    assert ping["epoch"] == dispatcher.epoch
    assert isinstance(ping["nodeId"], str) and ping["nodeId"]
    assert ping["running"] is True
    assert ping["pid"] == os.getpid()
    assert ping["socket"] == str(ownerless.socket)
    assert ping["restorePending"] is False


def test_methods_outside_the_callable_set_are_refused_as_account_required(dispatcher):
    with pytest.raises(DaemonRequestError) as refused:
        dispatcher.call("agent.list", {})
    assert refused.value.code == ACCOUNT_REQUIRED == "ACCOUNT_REQUIRED"
    assert refused.value.data == {
        "reason": "ACCOUNT_REQUIRED",
        "phase": "bootstrap",
        "method": "agent.list",
    }


def test_undeclared_names_are_unknown_methods(dispatcher):
    with pytest.raises(DaemonRequestError) as refused:
        dispatcher.call("no.such.method", {})
    assert refused.value.code == ipc_errors.METHOD_NOT_FOUND
    assert refused.value.data is None


@pytest.mark.parametrize("method", sorted(ROUTER_ONLY_METHODS))
def test_every_router_only_name_is_refused_with_account_required_on_the_wire(
    ownerless, method
):
    admitted = _admit(ownerless, WIRE_POLICY)
    try:
        with pytest.raises(DaemonRequestError) as refused:
            admitted.call(method, {})
    finally:
        admitted.close()
    assert refused.value.code == ACCOUNT_REQUIRED
    assert refused.value.data == {
        "reason": "ACCOUNT_REQUIRED",
        "phase": "bootstrap",
        "method": method,
    }


@pytest.mark.parametrize(
    "method",
    [
        "no.such.method",
        "agent.bogus",
        "agent.migrate.bogus",
        "workflow.bogus",
        "routine.bogus",
        "session.bogus",
    ],
)
def test_a_name_no_daemon_answers_stays_unknown_on_the_wire(ownerless, method):
    admitted = _admit(ownerless, WIRE_POLICY)
    try:
        with pytest.raises(DaemonRequestError) as refused:
            admitted.call(method, {})
    finally:
        admitted.close()
    assert refused.value.code == ipc_errors.METHOD_NOT_FOUND
    assert refused.value.data is None


def test_initialize_is_refused_until_a_foreground_owner_enables_promotion(dispatcher):
    assert "onboarding.initialize" not in dispatcher.callable_methods()
    with pytest.raises(DaemonRequestError) as before:
        dispatcher.call("onboarding.initialize", {"operationKey": "new-device-init-0001"})
    assert before.value.code == ACCOUNT_REQUIRED
    wake = threading.Event()
    dispatcher.enable_promotion(wake.set)
    assert dispatcher.promotion_enabled
    assert "onboarding.initialize" in dispatcher.callable_methods()
    with pytest.raises(DaemonRequestError) as reached:
        dispatcher.call("onboarding.initialize", {"operationKey": "new-device-init-0001"})
    assert reached.value.code == ipc_errors.IDENTITY_UNBOUND
    with pytest.raises(RuntimeError):
        dispatcher.enable_promotion(wake.set)


def test_callable_set_is_the_seven_onboarding_methods():
    assert BOOTSTRAP_CALLABLE_METHODS == {
        "onboarding.status",
        "auth.login.begin",
        "auth.operation.get",
        "auth.operation.cancel",
        "auth.relogin",
        "auth.logout",
        "onboarding.device.ensure",
    }


def test_catalog_projects_unavailable_methods_as_not_ready(dispatcher):
    catalog = dispatcher.call("daemon.catalog")
    assert catalog["epoch"] == dispatcher.call("ping")["epoch"]
    assert catalog["packageVersion"] == __version__
    entries = {entry["name"]: entry for entry in catalog["methods"]}
    callable_now = {name for name, entry in entries.items() if entry["callable"]}
    assert callable_now == BOOTSTRAP_CALLABLE_METHODS
    for name in callable_now:
        assert entries[name]["state"] == "available"
        assert entries[name]["reason"] is None
    others = [entry for name, entry in entries.items() if name not in callable_now]
    assert others
    for entry in others:
        assert entry["declared"] is True
        assert entry["callable"] is False
        if entry["httpExposed"]:
            assert (entry["state"], entry["reason"]) == ("not-ready", "ACCOUNT_REQUIRED")
        else:
            assert (entry["state"], entry["reason"]) == ("disabled", None)


def test_wire_projection_keeps_every_http_exposed_method_routable(ownerless):
    admitted = _admit(ownerless, WIRE_POLICY)
    try:
        before = admitted.call("daemon.catalog")["methods"]
        admitted.enable_promotion(threading.Event().set)
        after = admitted.call("daemon.catalog")["methods"]
    finally:
        admitted.close()

    def routes(methods):
        return {
            entry["name"]
            for entry in methods
            if entry["state"] in {"available", "not-ready"}
        }

    exposed = {entry["name"] for entry in before if entry["httpExposed"]}
    assert routes(before) == exposed == routes(after)


def test_onboarding_methods_reach_the_real_gateway(dispatcher):
    dispatcher.runtime._identity_factory = ControlledIdentity
    status = dispatcher.call("onboarding.status", {})
    assert status["phase"] == "bootstrap"
    begun = dispatcher.call(
        "auth.login.begin", {"intent": "login", "operationKey": "dispatcher-login-001"}
    )
    assert begun["verification"]["userCode"] == "WIRE-CODE"
    operation = dispatcher.call(
        "auth.operation.get", {"operationId": begun["operationId"]}
    )
    assert operation["operation"]["state"] == "authenticated"
    assert dispatcher.runtime.view.record.phase == "authenticated"


def test_the_post_reply_is_armed_only_by_an_accepted_initialize(dispatcher):
    dispatcher.runtime._identity_factory = ControlledIdentity
    woke = threading.Event()
    dispatcher.enable_promotion(woke.set)
    begun = dispatcher.call(
        "auth.login.begin", {"intent": "login", "operationKey": "dispatcher-login-002"}
    )
    dispatcher.call("auth.operation.get", {"operationId": begun["operationId"]})
    assert dispatcher.take_post_reply() is None
    accepted = dispatcher.call(
        "onboarding.initialize", {"operationKey": "new-device-init-0002"}
    )
    assert accepted["state"] == "promoting" and accepted["initialized"] is False
    assert not woke.is_set()
    after = dispatcher.take_post_reply()
    assert after is not None
    assert dispatcher.take_post_reply() is None
    after()
    assert woke.is_set()
    assert dispatcher.runtime.view.record.phase == "promoting"


class _FailingApp:
    def install_onboarding(self, _gateway):
        raise RuntimeError("the onboarding port could not be installed")


def test_admission_releases_both_locks_when_the_gateway_cannot_be_installed(ownerless):
    with pytest.raises(RuntimeError) as failed:
        BootstrapDispatcher.admit(
            app=_FailingApp(),
            home=ownerless.home,
            state_dir=ownerless.state,
            socket_path=ownerless.socket,
            policy=WIRE_POLICY,
            gateway_options=gateway_options(),
        )
    assert "could not be installed" in str(failed.value)
    assert_home_released(ownerless)


def test_admission_releases_both_locks_when_the_call_context_cannot_be_built(
    ownerless, monkeypatch
):
    def broken(**_fields):
        raise RuntimeError("the call context could not be built")

    monkeypatch.setattr(dispatcher_module, "CallContext", broken)
    with pytest.raises(RuntimeError) as failed:
        _admit(ownerless, WIRE_POLICY)
    assert "could not be built" in str(failed.value)
    assert_home_released(ownerless)
