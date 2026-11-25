"""New connections have no public launch switch or ambient credential fallback."""

import json

import pytest

from hyprial.daemon.impl.harnesses.pi.bindings.launch import bind_launch, PiModelBindingError, start_failure
from .test_binding import binding


def payload(tmp_path, version="1.2.0"):
    package = tmp_path / "sdk"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({"version": version}))
    session = tmp_path / "agent" / "state" / "pi"
    session.mkdir(parents=True)
    return {"piPackageRoot": str(package), "sessionRoot": str(session)}


def test_bound_payload_never_contains_key(tmp_path):
    data, env = payload(tmp_path), {}
    bind_launch(data, env, binding=binding(), agent_home=tmp_path / "agent", mode="rpc")
    assert "synthetic-pi-test-key" not in json.dumps(data)
    assert env == {"HYPRIAL_PI_CONNECTION_KEY": "synthetic-pi-test-key"}
    assert data["modelBinding"]["bindingRef"].startswith("fixture:")


@pytest.mark.parametrize("version", ["0.84.0", "0.85.2", "1.2.0-beta", "1.3.1", "2.0.0"])
def test_unverified_version_refused(tmp_path, version):
    data = payload(tmp_path, version)
    with pytest.raises(PiModelBindingError) as error:
        bind_launch(data, {}, binding=binding(), agent_home=tmp_path / "agent", mode="rpc")
    assert error.value.data == {"phase": "admission", "reason": "sdk-version"}


def test_external_and_shared_roots_refused(tmp_path, monkeypatch):
    data = payload(tmp_path)
    foreign = tmp_path / "external"
    foreign.mkdir()
    with pytest.raises(PiModelBindingError, match="session-storage"):
        bind_launch(data, {}, binding=binding(), agent_home=foreign, mode="rpc")
    monkeypatch.setattr("hyprial.kernel.manysys.local_filesystem", lambda _: False)
    with pytest.raises(PiModelBindingError, match="session-storage"):
        bind_launch(data, {}, binding=binding(), agent_home=tmp_path / "agent", mode="rpc")


def test_marker_decodes_without_copying_stderr():
    error = start_failure(b"secret must not escape\nHYPRIAL_PI_START_FAILURE:MODEL_RESUME_MISMATCH\nresume/provenance-missing\n")
    assert error.code == "MODEL_RESUME_MISMATCH"
    assert error.data["reason"] == "provenance-missing"
    assert "secret" not in str(error)
    assert start_failure(b"untrusted MODEL_SESSION_BUSY text") is None


def test_root_alias_is_refused_before_canonicalization(tmp_path):
    data = payload(tmp_path)
    alias = tmp_path / "agent" / "alias"
    alias.symlink_to(tmp_path / "agent" / "state" / "pi", target_is_directory=True)
    data["sessionRoot"] = str(alias)
    with pytest.raises(PiModelBindingError, match="session-storage"):
        bind_launch(data, {}, binding=binding(), agent_home=tmp_path / "agent", mode="rpc")


def test_missing_key_marker_is_classified():
    error = start_failure(b"HYPRIAL_PI_START_FAILURE:MODEL_AUTH_UNAVAILABLE\nauthorize/missing\n")
    assert error.code == "MODEL_AUTH_UNAVAILABLE"
    assert error.data == {"phase": "authorize", "reason": "missing"}


def test_arbitrary_subtree_inside_agent_home_is_not_managed_sessions(tmp_path):
    data = payload(tmp_path)
    foreign = tmp_path / "agent" / "workspace" / "arbitrary"
    foreign.mkdir(parents=True)
    data["sessionRoot"] = str(foreign)
    with pytest.raises(PiModelBindingError, match="session-storage"):
        bind_launch(data, {}, binding=binding(), agent_home=tmp_path / "agent", mode="rpc")


@pytest.mark.parametrize("same", [True, False])
def test_explicit_selection_must_match_resolved_binding(tmp_path, same):
    data, env, selected = payload(tmp_path), {}, binding()
    data.update(modelProvider=selected.provider_id, model=selected.model if same else "different")
    if same:
        bind_launch(data, env, binding=selected, agent_home=tmp_path / "agent", mode="rpc")
        assert data["model"] == selected.model
    else:
        before = data.copy()
        with pytest.raises(PiModelBindingError, match="runtime-option"):
            bind_launch(data, env, binding=selected, agent_home=tmp_path / "agent", mode="rpc")
        assert data == before and env == {}
