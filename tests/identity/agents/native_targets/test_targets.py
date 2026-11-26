"""Native selections identify a target slot, never credential bytes or defaults."""

import json

import pytest

import hyprial.identity as identity


def inventory(home, harness, payload):
    home.mkdir(exist_ok=True)
    documents = [(".credentials.json" if harness == "claude" else "auth.json", json.dumps(payload).encode())]
    if harness == "pi":
        documents.append(("models-store.json", b"{}"))
    identity.install_native_credential_set(home, harness, documents, force=True)
    api = getattr(identity, "native_auth_targets", None)
    assert callable(api), "the identity authority must expose explicit native targets"
    return api(home, harness)


def test_multiple_pi_targets_are_explicit_and_refresh_preserves_refs(tmp_path):
    home = tmp_path / "agent"
    payload = {"openai": {"type": "api_key", "key": "synthetic-key"},
               "anthropic": {"type": "oauth", "access": "synthetic-access", "refresh": "synthetic-refresh", "expires": 1}}
    before = inventory(home, "pi", payload)
    assert [target.target for target in before] == ["anthropic", "openai"]
    assert len({target.reference for target in before}) == 2
    assert "synthetic" not in repr(before)
    payload["anthropic"].update(access="refreshed-access", refresh="refreshed-refresh", expires=9999999999999)
    assert inventory(home, "pi", payload) == before
    payload.pop("openai")
    assert [target.target for target in inventory(home, "pi", payload)] == ["anthropic"]
    identity.remove_native_credentials(home, "pi")
    with pytest.raises(identity.NativeCredentialError):
        identity.native_auth_targets(home, "pi")


@pytest.mark.parametrize("harness,payload,target,kind", [
    ("claude", {"claudeAiOauth": {"accessToken": "synthetic-access"}}, "anthropic", "oauth"),
    ("codex", {"tokens": {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh"}}, "openai", "oauth"),
    ("codex", {"OPENAI_API_KEY": "synthetic-key"}, "openai", "api-key"),
])
def test_native_target_formats(tmp_path, harness, payload, target, kind):
    rows = inventory(tmp_path / "agent", harness, payload)
    assert len(rows) == 1
    assert (rows[0].harness, rows[0].target, rows[0].kind) == (harness, target, kind)
    assert identity.NativeAuthTarget.from_reference(rows[0].reference) == rows[0]


@pytest.mark.parametrize("harness,payload", [
    ("pi", {"openai": {"type": "api_key", "key": "!printenv OPENAI_API_KEY"}}),
    ("pi", {"openai": {"type": "api_key", "key": ""}}),
    ("pi", {"openai": {"type": "oauth", "access": "token", "expires": 3}}),
    ("codex", {"tokens": {"access_token": "token"}, "OPENAI_API_KEY": "key"}),
    ("claude", {"unknownLogin": {"accessToken": "secret"}}),
])
def test_ambiguous_or_unsupported_material_is_not_a_usable_target(tmp_path, harness, payload):
    with pytest.raises(identity.NativeCredentialError):
        inventory(tmp_path / "agent", harness, payload)


def test_reference_rejects_partial_and_foreign_formats():
    api = getattr(identity, "NativeAuthTarget", None)
    assert api is not None
    for value in ("openai", "native:v1:pi:openai", "native:v2:pi:oauth:openai", "native:v1:pi:oauth:../openai"):
        with pytest.raises(ValueError):
            api.from_reference(value)
