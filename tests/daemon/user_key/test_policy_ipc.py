"""U1 authorization and real CLI/catalog surfaces, with isolated state only."""
import json
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.daemon import DaemonApplication
import hyprial.identity as identity
from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.cli.commands.common.root import app as cli
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401


def policy():
    core = getattr(identity, "UserKey", None)
    assert core is not None, "user-key core required"
    from hyprial.daemon.impl.user_key.policy import Caller, RateLimiter, authorize
    return core, Caller, RateLimiter, authorize


PURPOSES = json.loads((Path(__file__).resolve().parents[3] / "contract/fixtures/user-key/purposes.json").read_text())["reserved"]


@pytest.mark.parametrize("purpose", PURPOSES)
@pytest.mark.parametrize("kind", ["operator", "daemon", "session", "squire", "adapter", "channel"])
def test_reserved_purpose_caller_matrix(purpose, kind):
    UserKey, Caller, _, authorize = policy()
    caller = Caller(kind, "user:alice", frozenset())
    allowed = kind == "operator" or (kind == "daemon" and purpose == "orgfs.commit-as-user")
    if allowed:
        authorize(caller, purpose, owner="alice")
    else:
        with pytest.raises(UserKey.Error) as caught:
            authorize(caller, purpose, owner="alice")
        assert caught.value.code == "CALLER_NOT_AUTHORIZED"


def test_internal_derived_and_ledger_rate():
    UserKey, Caller, RateLimiter, authorize = policy()
    caller = Caller("daemon", "daemon", frozenset(), operator_initiated=True)
    authorize(caller, "revocation-ledger.append", owner="alice")
    limiter = RateLimiter()
    for _ in range(60):
        limiter.consume(caller, "revocation-ledger.append", 1000)
    with pytest.raises(UserKey.Error) as caught:
        limiter.consume(caller, "revocation-ledger.append", 1000)
    assert caught.value.code == "RATE_LIMITED"
    limiter.consume(caller, "revocation-ledger.append", 1060)
    for _ in range(100):
        limiter.consume(caller, "orgfs.commit-as-user", 1000)
    assert limiter.internal_commits == 100


@pytest.mark.parametrize("command", [["status"], ["rotate"], ["renew"], ["revoke", "--device", "test-device"]])
def test_worker_cli_refuses_without_request(command, monkeypatch):
    from hyprial.shell.impl.cli.commands.common.services import get_services
    monkeypatch.setenv("HYPRIAL_WORKER_ACTOR", "synthetic-worker")
    def no_request(*args, **kwargs):
        pytest.fail("worker command must not contact daemon")
    monkeypatch.setattr(type(get_services().runtime), "request", no_request)
    result = CliRunner().invoke(cli, ["user", "key", *command, "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout)["code"] == "CALLER_NOT_AUTHORIZED"


def test_ipc_reserved_purpose_and_lifecycle_refuse(tmp_path):
    app = object.__new__(DaemonApplication)
    app._restore_done = threading.Event()
    app._restore_done.set()
    app._inbox = app._harnesses = app._presence = object()
    app.owner = "alice"
    app.state_dir = tmp_path / "state"
    app.hyprial_home = tmp_path
    for method, params in [
        ("userKey.sign", {"purpose": "org.join", "payload": {}}),
        ("userKey.rotate", {}), ("userKey.revoke", {"deviceUid": "synthetic"}),
        ("userKey.renew", {}),
    ]:
        with pytest.raises(DaemonRequestError) as caught:
            app.handle(method, params, _trusted_message_origin="ipc")
        assert caught.value.code == "USER_KEY_UNBOUND"
    assert app.handle("userKey.status", {}, _trusted_message_origin="ipc")["state"] == "unavailable"
    assert not list(tmp_path.rglob("key.json"))


def _app(tmp_path):
    app = object.__new__(DaemonApplication)
    app._restore_done = threading.Event()
    app._restore_done.set()
    app._inbox = app._harnesses = app._presence = object()
    app.owner = "alice"
    app.state_dir = tmp_path / "state"
    app.hyprial_home = tmp_path
    return app


@pytest.mark.parametrize("method", [
    "userKey.sign", "userKey.status", "userKey.rotate", "userKey.revoke", "userKey.renew",
])
@pytest.mark.parametrize("origin", ["worker", "ipc"])
def test_registry_preauthorizes_before_schema_validation(tmp_path, method, origin):
    from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY

    # Direct registry invocation cannot rely on a legacy router prefix guard.
    # Claimed actor identity is not operator authorization, even with ipc origin.
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(
            _app(tmp_path), method, {"actor": "synthetic-worker", "caller": "operator"},
            trusted_message_origin=origin,
        )
    assert caught.value.code == "CALLER_NOT_AUTHORIZED"


def test_renew_is_registry_only_and_fails_closed_when_unbound(tmp_path, monkeypatch):
    from hyprial.daemon.impl.ipc import router
    from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY, MethodRegistry

    app = _app(tmp_path)
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(app, "userKey.renew", {}, trusted_message_origin="ipc")
    assert caught.value.code == "USER_KEY_UNBOUND"

    # Removing the binding cannot expose a legacy handler or prefix guard.
    registry = MethodRegistry()
    registry.freeze()
    monkeypatch.setattr(router, "METHOD_REGISTRY", registry)
    def no_signer(*args, **kwargs):
        pytest.fail("unbound method must not reach user-key service through legacy routing")
    monkeypatch.setattr("hyprial.daemon.impl.user_key.ipc.signer_for", no_signer)
    for origin in ("ipc", "worker"):
        with pytest.raises(DaemonRequestError) as missing:
            app.handle("userKey.renew", {}, _trusted_message_origin=origin)
        assert missing.value.code == "METHOD_NOT_FOUND"
    assert not list(tmp_path.rglob("key.json"))
