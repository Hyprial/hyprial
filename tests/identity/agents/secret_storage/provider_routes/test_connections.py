"""Connection revisions fence endpoint edits while key rotation preserves identity."""

import json
from types import SimpleNamespace

import pytest

from hyprial.identity import SecretResolver, SecretResolutionError

CONNECTION = {"version": 1, "endpoint": "https://model.example/v1",
              "protocol": "openai-completions", "profile": "openai-chat-completions/v1"}


@pytest.fixture
def resolver(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return SecretResolver(home, SimpleNamespace())


def save(resolver, *, revision=0, connection=None, value="synthetic-key"):
    return resolver.configure_model_connection("custom", field_name="key", value=value,
        connection=CONNECTION if connection is None else connection, expected_revision=revision)


def test_rotation_and_reopen_preserve_connection_and_schema(resolver):
    path = save(resolver)
    route = resolver.model_service_metadata("custom")["connections"]["key"]
    assert route["configRevision"] == 1
    assert route["connectionRef"]
    assert "vendorId" not in route
    resolver.write_user_provider("custom", {"key": "synthetic-rotated"})
    reopened = SecretResolver(resolver.hyprial_home, SimpleNamespace())
    assert reopened.model_service_metadata("custom")["connections"]["key"] == route
    assert json.loads(path.read_bytes())["$schema"] == "hyprial-model-secret/v2"
    assert "synthetic" not in json.dumps(route)


def test_endpoint_edit_bumps_revision_and_stale_edit_cannot_replace_key(resolver):
    path = save(resolver)
    first = resolver.model_service_metadata("custom")["connections"]["key"]
    changed = dict(CONNECTION, endpoint="https://second.example/v1")
    save(resolver, revision=1, connection=changed)
    second = resolver.model_service_metadata("custom")["connections"]["key"]
    assert second["configRevision"] == 2
    assert second["connectionRef"] == first["connectionRef"]
    before = path.read_bytes()
    with pytest.raises(SecretResolutionError) as error:
        save(resolver, revision=1, value="synthetic-stale")
    assert error.value.category == "revision-mismatch"
    assert path.read_bytes() == before


def test_same_configuration_rotation_does_not_change_revision(resolver):
    save(resolver)
    save(resolver, revision=1, value="synthetic-rotated")
    assert resolver.model_service_metadata("custom")["connections"]["key"]["configRevision"] == 1


def test_legacy_route_only_migrates_explicitly_and_cannot_downgrade(resolver):
    path = resolver.configure_model_service("custom", field_name="key", value="synthetic-old",
        endpoint=CONNECTION["endpoint"], protocol="openai-completions", vendor_id="deepseek")
    assert json.loads(path.read_bytes())["$schema"] == "hyprial-model-secret/v1"
    save(resolver, value=None)
    before = path.read_bytes()
    with pytest.raises(SecretResolutionError):
        resolver.configure_model_service("custom", field_name="key", value="synthetic-new",
            endpoint=CONNECTION["endpoint"], protocol="openai-completions", vendor_id="deepseek")
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", [{"version": 2}, {"profile": "unknown"},
    {"protocol": "anthropic-messages"}, {"endpoint": "https://user:secret@example.com"},
    {"model": "hy4-flash"}])
def test_unsupported_or_secret_bearing_configuration_does_not_create_file(resolver, change):
    with pytest.raises(ValueError):
        save(resolver, connection=dict(CONNECTION, **change))
    assert not (resolver.hyprial_home / "secrets/providers/custom.json").exists()


def test_concurrent_edits_have_one_revision_winner(resolver):
    from concurrent.futures import ThreadPoolExecutor

    path = save(resolver)

    def edit(index):
        try:
            save(resolver, revision=1, connection=dict(CONNECTION,
                 endpoint=f"https://model-{index}.example/v1"))
            return True
        except SecretResolutionError as error:
            assert error.category == "revision-mismatch"
            return False

    with ThreadPoolExecutor(max_workers=4) as workers:
        assert sum(workers.map(edit, range(4))) == 1
    assert json.loads(path.read_bytes())["routes"]["key"]["configRevision"] == 2


def test_failed_atomic_replacement_preserves_previous_connection(resolver, monkeypatch):
    from hyprial.identity.impl.agents.runtime import model_records

    path = save(resolver)
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("synthetic-write-failure")

    monkeypatch.setattr(model_records, "write_private_file_atomic", fail)
    with pytest.raises(SecretResolutionError):
        save(resolver, revision=1, connection=dict(CONNECTION, endpoint="https://second.example"))
    assert path.read_bytes() == before


def test_v2_connection_cannot_use_legacy_deepseek_launch(resolver):
    from hyprial.daemon.impl.harnesses.model_routes import route_environment
    from hyprial.kernel import HarnessLaunchSpec
    from hyprial.identity.impl.agents.runtime import model_records

    path = save(resolver)
    route = model_records.read(path).routes["key"]
    selected = SimpleNamespace(route=route, grant=SimpleNamespace(
        environment_names=("DEEPSEEK_API_KEY",), entry_id="custom"))
    with pytest.raises(SecretResolutionError):
        route_environment(HarnessLaunchSpec(harness="pi", name="worker", headless=True, model_provider="deepseek", model="deepseek-flash"),
                          (selected,))


@pytest.mark.parametrize("schema", [[], {}, None, 2, "hyprial-model-secret/v3"])
def test_invalid_schema_is_refused_without_replacing_record(resolver, schema):
    path = save(resolver)
    document = json.loads(path.read_bytes())
    document["$schema"] = schema
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    with pytest.raises(SecretResolutionError):
        resolver.model_service_metadata("custom")
    with pytest.raises(SecretResolutionError):
        save(resolver, revision=1)
    assert path.read_bytes() == before
