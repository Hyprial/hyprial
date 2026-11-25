"""Platform confirmation uses real Agent custody and the Lark actor port."""

from dataclasses import replace
import json

import pytest

from hyprial.daemon.impl.adapters.lark.runtime.runtime import AdapterRuntime
from hyprial.daemon.impl.composition.events import CorrelatedDomainEvents
from hyprial.daemon.impl.composition.ports import LarkPortClient
from hyprial.daemon.impl.request_catalog import METHOD_REGISTRY
from hyprial.kernel import ChannelConfiguration, DaemonRequestError
from tests.daemon.adapters.lark.lifecycle.test_lark_lifecycle_guards import _Launcher, _gateway
from tests.daemon.management.test_management_actor import Harnesses
from tests.support.identity.cell_runtime import registry_cell_runtime
from tests.identity.agents.home.roots.lifecycle.support import (
    _application, _isolated_environment as _isolated_environment,
)


@pytest.fixture
def app(tmp_path):
    app = _application(tmp_path)
    app.handle("agent.create", {"name": "worker"})
    config = ChannelConfiguration(gateways=(replace(_gateway("app"), credential_ref="lark-app"),))
    (app.hyprial_home / "channels.json").write_text(json.dumps(config.to_json()))
    secrets = app.hyprial_home / "secrets"
    secrets.mkdir(exist_ok=True, mode=0o700)
    (secrets / "lark-app.json").write_text('{"appSecret":"synthetic-only"}')
    events = CorrelatedDomainEvents()
    runtime = AdapterRuntime(_Launcher(), config, event_sink=events,
        configuration_source=lambda: ChannelConfiguration.from_json(json.loads((app.hyprial_home / "channels.json").read_text())))
    app._adapters = runtime
    app._lark_client = LarkPortClient(runtime, events)
    app._harnesses = Harnesses()
    app._harnesses.status = lambda: ()
    registry_cell_runtime(app)
    return app


def call(app, method, params):
    return app.handle(method, params, _trusted_message_origin="ipc")


def preview(app):
    return call(app, "management.adapter.remove.preview", {"name": "app"})


def execute(app, receipt):
    return call(app, "management.adapter.remove", {"operationId": receipt["operationId"]})


def test_methods_are_declared_for_http_with_confirmed_execution():
    for name, confirmation in (("management.adapter.remove.preview", "preview"),
                               ("management.adapter.remove", "execute")):
        spec = METHOD_REGISTRY.get(name).spec
        assert spec.http_exposed and spec.confirmation == confirmation
    assert set(METHOD_REGISTRY.get("management.adapter.remove").spec.params_schema["properties"]) == {"operationId"}


def test_cancelled_preview_preserves_every_effect(app):
    before = (app.hyprial_home / "channels.json").read_bytes()
    result = preview(app)
    assert result["boundAgent"] is None
    assert result["processRunning"] is False
    assert (app.hyprial_home / "channels.json").read_bytes() == before
    assert app._harnesses.events == []


def test_confirmed_delete_keeps_agent_and_replays_once(app):
    receipt = preview(app)
    result = execute(app, receipt)
    assert result["removed"] is True and result["credentialRemoved"] is True
    assert result["unboundAgent"] is None
    assert app.agents.get("worker") is not None
    assert app._lark_client.read_adapter("app") is None
    assert not (app.hyprial_home / "secrets/lark-app.json").exists()
    assert execute(app, receipt) == result
    assert [kind for kind, _ in app._harnesses.events] == ["remove"]


@pytest.mark.parametrize("change", ["configuration", "credential", "runtime"])
def test_changed_preview_is_refused_before_any_deletion(app, change):
    receipt = preview(app)
    if change == "configuration":
        path = app.hyprial_home / "channels.json"
        path.write_text(path.read_text() + "\n")
    elif change == "credential":
        (app.hyprial_home / "secrets/lark-app.json").write_text('{"appSecret":"replacement"}')
    else:
        app._lark_client.start("app")
    with pytest.raises(DaemonRequestError) as refused:
        execute(app, receipt)
    assert refused.value.code == "PREVIEW_STALE"
    assert app._harnesses.events == []
    assert (app.hyprial_home / "secrets/lark-app.json").exists()


def test_running_adapter_must_be_stopped_and_previewed_again(app):
    app._lark_client.start("app")
    receipt = preview(app)
    assert receipt["processRunning"] is True
    with pytest.raises(DaemonRequestError) as refused:
        execute(app, receipt)
    assert refused.value.code == "OPERATION_IN_PROGRESS"
    assert app._lark_client.read_adapter("app").process_running
    assert app._harnesses.events == []


def test_direct_name_and_worker_identity_cannot_execute(app):
    with pytest.raises(DaemonRequestError) as refused:
        call(app, "management.adapter.remove", {"name": "app"})
    assert refused.value.code == "INVALID_ARGUMENT"
    with pytest.raises(DaemonRequestError) as refused:
        app.handle("management.adapter.remove.preview", {"name": "app"}, _trusted_message_origin="worker")
    assert refused.value.code == "CALLER_NOT_AUTHORIZED"
    assert app._harnesses.events == []


def test_expiry_and_another_epoch_cannot_consume_confirmation(app, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(app._adapter_remove_previews, "_now_ms", lambda: clock[0])
    receipt = preview(app)
    clock[0] = receipt["expiresAt"]
    with pytest.raises(DaemonRequestError) as refused:
        execute(app, receipt)
    assert refused.value.code == "PREVIEW_EXPIRED"
    assert app._harnesses.events == []


@pytest.mark.parametrize("committed", [False, True])
def test_recovery_settles_durable_intent_without_forward_replay(app, committed):
    from hyprial.daemon.impl.operations.adapter_removal.service import AdapterRemovalService, recover_removals
    from hyprial.daemon.impl.operations.adapter_removal.journal import RemovalIntent, RemovalPhase

    service = AdapterRemovalService(app)
    snapshot = service.observe("app")
    intent = RemovalIntent("interrupted", "app", "user:test", app.epoch, snapshot.digest,
        snapshot.before, snapshot.after, snapshot.registry, snapshot.entity_token,
        snapshot.credential_ref, snapshot.credential_digest, snapshot.remove_credential)
    service.journal.prepare(intent)
    (app.hyprial_home / "channels.json").write_bytes(snapshot.after)
    with app._registry_management_handler().adapter_removal("app") as lease:
        lease.commit()
    if committed:
        service.journal.advance(intent.operation_id, RemovalPhase.PREPARED, RemovalPhase.COMMITTED)
    effects_before = list(app._harnesses.events)
    recover_removals(app)
    recover_removals(app)
    assert service.journal.pending() == ()
    assert (app.hyprial_home / "secrets/lark-app.json").exists() is not committed
    assert (app._lark_client.read_adapter("app") is None) is committed
    assert app.agents.get("worker") is not None
    assert all(kind != "remove" for kind, _ in app._harnesses.events[len(effects_before):])


@pytest.mark.parametrize("failure", ["admission", "completion"])
def test_failed_runtime_begin_preserves_files_and_releases_reservation(app, monkeypatch, failure):
    from hyprial.daemon.impl.adapters.lark.runtime.commands import _IoResult

    runtime = app._adapters
    before = (app.hyprial_home / "channels.json").read_bytes()
    receipt = preview(app)
    original_submit = runtime._effects.submit

    def fail_stop(**request):
        if request["operation"] != "deactivate-stop":
            return original_submit(**request)
        if failure == "admission":
            raise RuntimeError("synthetic effect admission failure")
        runtime._effects._completion(_IoResult(
            request["correlation_id"], request["generation"], request["version"],
            request["name"], request["operation"], False, detail="synthetic stop failure",
        ))
        return True

    with monkeypatch.context() as patch:
        patch.setattr(runtime._effects, "submit", fail_stop)
        with pytest.raises(DaemonRequestError):
            execute(app, receipt)
    assert (app.hyprial_home / "channels.json").read_bytes() == before
    assert (app.hyprial_home / "secrets/lark-app.json").exists()
    assert runtime._authority.removals == {}
    assert not runtime._authority.lifecycle_effects
    app._lark_client.reload()
    assert app._lark_client.start("app")


def test_failed_runtime_finish_keeps_durable_recovery_pending(app, monkeypatch):
    from hyprial.daemon.impl.operations.adapter_removal.journal import AdapterRemovalJournal, RemovalPhase

    receipt = preview(app)

    def failed(*args, **kwargs):
        raise RuntimeError("synthetic runtime settlement unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(app._lark_client, "finish_removal", failed)
        with pytest.raises(DaemonRequestError):
            execute(app, receipt)
    journal = AdapterRemovalJournal(app.state_db)
    assert journal.get(receipt["operationId"]).phase is RemovalPhase.COMMITTED
    assert len(journal.pending()) == 1
    assert not (app.hyprial_home / "secrets/lark-app.json").exists()
    with pytest.raises(DaemonRequestError) as blocked:
        preview(app)
    assert blocked.value.code == "OPERATION_IN_PROGRESS"
    with pytest.raises(DaemonRequestError):
        execute(app, receipt)
