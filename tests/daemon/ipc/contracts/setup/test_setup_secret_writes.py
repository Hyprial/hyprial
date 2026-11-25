"""Secret writes use the existing authority without credential-bearing receipts."""

from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.request_catalog.registry import MethodRegistry
from hyprial.daemon.impl.request_catalog.setup import SETUP_BINDINGS

from hyprial.kernel import DaemonRequestError
from tests.daemon.ipc.contracts.agent.test_read_specs import app as app

METHOD_REGISTRY = MethodRegistry.from_bindings(SETUP_BINDINGS)
WRITE = "agent.secret.provider-write"
GRANT = "agent.secret.grant"
KEY = "synthetic-model-key-not-a-real-credential"


@pytest.mark.parametrize("method", [WRITE, GRANT])
def test_secret_write_declarations_use_h03(method):
    binding = METHOD_REGISTRY.get(method)
    assert binding is not None
    spec = binding.spec
    assert spec.mutates and spec.retry == "never"
    assert spec.http_exposed and spec.mode == "unary"
    assert spec.audience == ("local-operator",)
    assert spec.confirmation == "none"
    assert spec.http_binding.identity.mode == "none"
    assert spec.http_binding.identity.param is None
    assert spec.ipc_param_validation == "handler"


def test_provider_write_persists_in_the_real_secret_resolver(app):
    params = {"entryId": "http-test", "fieldName": "key", "value": KEY, "legacyUnused": 1}
    result = METHOD_REGISTRY.invoke(app, WRITE, params)
    assert result == {"ok": True, "entryId": "http-test", "fieldNames": ["key"]}
    assert KEY not in json.dumps(result)
    stored = app.hyprial_home / "secrets" / "providers" / "http-test.json"
    assert json.loads(stored.read_text()) == {"key": KEY}


def test_grant_uses_the_real_agent_authority_and_list_receipt(app):
    app._agent_registry.create("worker")
    params = {"actor": "worker", "grantId": "http-test", "source": "user-provider",
              "entryId": "http-test", "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"],
              "revision": 1, "ownerActor": None, "legacyUnused": True}
    result = METHOD_REGISTRY.invoke(app, GRANT, params)
    assert result["ok"] is True
    assert result["grant"]["revision"] == 1
    assert result["grant"]["ownerActor"] is None
    assert result["grant"] in app.handle("agent.secret.list", {"actor": "worker"})["grants"]


@pytest.mark.parametrize("params", [None, [], {}, {"entryId": "x", "fieldName": "key", "value": KEY + "\n"},
                                    {"entryId": "../escape", "fieldName": "key", "value": KEY}])
def test_invalid_provider_write_never_returns_a_secret(app, params):
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(app, WRITE, params)
    assert caught.value.code == "INVALID_ARGUMENT"
    assert KEY not in str(caught.value)


@pytest.mark.parametrize("revision", [None, 0, -1, True, "1"])
def test_invalid_grant_revision_is_refused(app, revision):
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(app, GRANT, {
            "actor": "worker", "grantId": "x", "source": "user-provider", "entryId": "x",
            "fieldName": "key", "environmentNames": ["DEEPSEEK_API_KEY"], "revision": revision,
        })
    assert caught.value.code == "INVALID_ARGUMENT"


def test_secret_path_refusal_keeps_the_declared_error(app):
    directory = app.hyprial_home / "secrets"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(DaemonRequestError) as caught:
        METHOD_REGISTRY.invoke(app, WRITE, {"entryId": "x", "fieldName": "key", "value": KEY})
    assert caught.value.code == "AGENT_SECRET_RESOLUTION_FAILED"
    assert KEY not in str(caught.value)
    assert not (directory / "providers" / "x.json").exists()



def test_unsupported_expected_agent_is_not_an_advertised_http_fence():
    spec = METHOD_REGISTRY.get(GRANT).spec
    declaration = spec.params_schema['properties']['expectedAgent']
    assert declaration['x-hyprial-http-forbidden'] is True

def test_setup_examples_validate_against_the_same_bindings():
    from hyprial.kernel import validate_method_example_sets
    from hyprial.daemon.impl.request_catalog.setup import SETUP_EXAMPLES
    validate_method_example_sets(METHOD_REGISTRY.specs, SETUP_EXAMPLES)
