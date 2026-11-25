"""Changing a Key must not silently discard its owner-selected service."""

import json
from pathlib import Path

import pytest

from hyprial.identity import AgentRegistry, SecretResolver, SecretResolutionError, SecretSource


@pytest.fixture
def store(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    registry = AgentRegistry(tmp_path / "state" / "agents.sqlite3", owner="owner",
                             machine="node", hyprial_home=home)
    registry.create("worker")
    try:
        yield registry, SecretResolver(home, registry)
    finally:
        registry.close()


def configure(resolver, **kwargs):
    return resolver.configure_model_service(
        "gateway", field_name="TOKEN", endpoint="https://model.example/v1",
        protocol="openai-completions", vendor_id="deepseek", **kwargs,
    )


def grant(registry, *, revision=1, field_name="TOKEN"):
    return registry.grant_secret(
        "worker", grant_id="model", source=SecretSource.USER_PROVIDER,
        entry_id="gateway", field_name=field_name,
        environment_names=("DEEPSEEK_API_KEY",), revision=revision,
    )


@pytest.mark.parametrize("raw", ["synthetic-raw-key\n", '{"nested":{"a":1}}\n'])
def test_existing_whole_file_grant_preserves_legacy_text(store, raw):
    registry, resolver = store
    path = resolver.write_user_provider("gateway", {"TOKEN": "temporary"})
    path.write_text(raw)
    grant(registry, field_name=None)
    result = resolver.resolve("worker", "model")
    assert result.value == raw
    assert result.route is None


def test_private_read_failure_is_typed_and_does_not_expose_error_text(store, monkeypatch):
    from hyprial.identity.impl.agents.runtime import secrets
    from hyprial.kernel import PrivateAccessUnsupported

    registry, resolver = store
    resolver.write_user_provider("gateway", {"TOKEN": "synthetic"})
    grant(registry)

    def refuse(*args, **kwargs):
        raise PrivateAccessUnsupported("private-error-sentinel")

    monkeypatch.setattr(secrets, "read_private_file", refuse)
    with pytest.raises(SecretResolutionError) as caught:
        resolver.resolve("worker", "model")
    assert "private-error-sentinel" not in str(caught.value)


def test_configure_rotate_and_reopen_preserve_route_and_other_fields(store):
    registry, resolver = store
    resolver.write_user_provider("gateway", {"TOKEN": "synthetic-first", "OTHER": "synthetic"})
    path = configure(resolver)
    assert resolver.list_user_providers() == (("gateway", ("OTHER", "TOKEN")),)
    row = grant(registry)
    assert row.route_required is True
    resolver.write_user_provider("gateway", {"TOKEN": "synthetic-second"})
    reopened = SecretResolver(resolver.hyprial_home, registry)
    result = reopened.resolve("worker", "model")
    assert result.route.endpoint == "https://model.example/v1"
    assert result.route.protocol == "openai-completions"
    assert result.value == "synthetic-second"
    assert reopened.list_user_providers() == (("gateway", ("OTHER", "TOKEN")),)
    assert path.stat().st_mode & 0o777 == 0o600
    assert "synthetic-second" not in repr(result)


def test_old_grant_cannot_acquire_a_new_route_without_explicit_regrant(store):
    registry, resolver = store
    grant(registry)
    configure(resolver, value="synthetic-new")
    with pytest.raises(SecretResolutionError, match="route-mismatch"):
        resolver.resolve("worker", "model")
    assert grant(registry, revision=2).route_required is True
    assert resolver.resolve("worker", "model").route is not None


def test_old_writer_overwrite_cannot_send_key_to_deployment_default(store):
    registry, resolver = store
    path = configure(resolver, value="synthetic-new")
    grant(registry)
    path.write_text(json.dumps({"TOKEN": "synthetic-old-writer"}))
    with pytest.raises(SecretResolutionError, match="route-mismatch"):
        resolver.resolve("worker", "model")


def test_custom_record_cannot_be_granted_as_whole_json(store):
    registry, resolver = store
    configure(resolver, value="synthetic-new")
    with pytest.raises(SecretResolutionError):
        grant(registry, field_name=None)


def test_transfer_cannot_export_custom_key_without_its_service_route(store):
    from hyprial.daemon.impl.transfer.archive.env_credentials import resolve_env_credentials

    registry, resolver = store
    configure(resolver, value="synthetic-routed-key")
    grant(registry)
    rows = resolve_env_credentials(registry=registry, hyprial_home=resolver.hyprial_home,
                                   agent_name="worker", harness="pi")
    assert len(rows) == 1
    assert rows[0].requires_reauth is True
    assert rows[0].value is None
    assert "synthetic-routed-key" not in str(rows[0].as_dict())


@pytest.mark.parametrize("damage", ["schema", "extra", "value", "route", "symlink"])
def test_invalid_record_cannot_be_repaired_by_rotation(store, damage, tmp_path):
    _registry, resolver = store
    path = configure(resolver, value="synthetic-new")
    document = json.loads(path.read_bytes())
    if damage == "schema":
        document["$schema"] = "future-format"
    elif damage == "extra":
        document["unknown"] = "synthetic-new"
    elif damage == "value":
        document["values"]["TOKEN"] = {}
    elif damage == "route":
        document["routes"]["TOKEN"]["protocol"] = "unknown"
    else:
        target = tmp_path / "outside"
        path.rename(target)
        path.symlink_to(target)
    if damage != "symlink":
        path.write_text(json.dumps(document))
    before = path.read_bytes()
    with pytest.raises(SecretResolutionError):
        resolver.write_user_provider("gateway", {"TOKEN": "synthetic-rotation"})
    assert path.read_bytes() == before
    with pytest.raises(SecretResolutionError):
        resolver.list_user_providers()


def test_failed_replace_preserves_complete_old_record(store, monkeypatch):
    import os

    registry, resolver = store
    path = configure(resolver, value="synthetic-first")
    grant(registry)
    original = os.replace

    def fail(source, destination, **kwargs):
        if Path(destination) == path:
            raise OSError("synthetic-private-error")
        return original(source, destination, **kwargs)

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(SecretResolutionError) as error:
        resolver.write_user_provider("gateway", {"TOKEN": "synthetic-second"})
    assert "synthetic-private-error" not in str(error.value)
    resolved = resolver.resolve("worker", "model")
    assert resolved.value == "synthetic-first"
    assert resolved.route.endpoint == "https://model.example/v1"


def test_plain_provider_keeps_legacy_behavior(store):
    registry, resolver = store
    resolver.write_user_provider("gateway", {"TOKEN": "synthetic-first"})
    grant(registry)
    resolver.write_user_provider("gateway", {"TOKEN": "synthetic-second"})
    result = resolver.resolve("worker", "model")
    assert result.value == "synthetic-second"
    assert result.route is None


def test_unaware_launch_consumer_cannot_drop_route(store):
    registry, resolver = store
    configure(resolver, value="synthetic-new")
    grant(registry)
    with pytest.raises(SecretResolutionError, match="route-not-admitted"):
        resolver.resolve("worker", "model").environment()


def test_daemon_worker_uses_route_and_granted_key_over_deployment_default(store):
    from types import SimpleNamespace

    from hyprial.daemon.impl.application.netendpoints.endpoints import compose_daemon_worker_launch
    from hyprial.kernel import HarnessLaunchSpec

    registry, resolver = store
    configure(resolver, value="synthetic-new")
    grant(registry)
    launch = compose_daemon_worker_launch(
        registry=registry, hyprial_home=resolver.hyprial_home,
        spec=HarnessLaunchSpec(harness="pi", name="worker", model_provider="deepseek",
                               model="deepseek-flash", headless=True),
        channel=SimpleNamespace(actor="worker", identity_environment=lambda: {}),
        environ={"HYPRIAL_OPENAI_BASE_URL": "https://deployment.example/v1"},
    )
    environment = launch.environment.for_exec()
    assert environment["HYPRIAL_OPENAI_BASE_URL"] == "https://model.example/v1"
    assert environment["DEEPSEEK_API_KEY"] == "synthetic-new"
    assert launch.grants == (("model", 1),)


@pytest.mark.parametrize("grant_first", [False, True])
def test_interactive_consumer_refuses_configured_user_grant(store, grant_first):
    from hyprial.daemon.impl.application.actors.agents_admin.provider_credential import materialize_agent_secret_environment

    registry, resolver = store
    if grant_first:
        grant(registry)
    configure(resolver, value="synthetic-new")
    if not grant_first:
        grant(registry)
    with pytest.raises(SecretResolutionError, match="route-unsupported"):
        materialize_agent_secret_environment(registry=registry, actor="worker")
