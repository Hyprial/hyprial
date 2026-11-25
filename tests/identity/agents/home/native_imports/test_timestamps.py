"""Invalid import or response clocks never establish native Auth provenance."""

import pytest
from jsonschema import Draft202012Validator

from hyprial.daemon.impl.request_catalog.agent.provider_auth import SPEC
from tests.daemon.harnesses.providers.test_daemon_provider_login import (
    ACTOR, provider_app as provider_app,
)
from tests.daemon.harnesses.providers.test_native_auth_imports import _login, _record
from tests.identity.agents.home.native_imports.test_launch_evidence import capture, select
from tests.support.runtime._owned_applications import owned_applications as owned_applications


@pytest.mark.parametrize("at_ms", [0, -1, True, False, 1.5, "1"])
@pytest.mark.parametrize("operation", ["write", "confirm", "restore"])
def test_invalid_timestamp_cannot_mutate_import_provenance(provider_app, at_ms, operation):
    app = provider_app
    _login(app, "codex", ["auth.json"])
    record = app._agent_registry.native_credential_import_record(ACTOR)
    database = app._agent_registry._db
    before = tuple(database.execute("SELECT * FROM native_auth_imports").fetchone())
    import_id, manifest = database.execute("SELECT import_id,manifest FROM native_auth_imports").fetchone()
    with pytest.raises(ValueError):
        if operation == "write":
            record.write("codex", manifest, at_ms)
        elif operation == "confirm":
            record.confirm("codex", (import_id, manifest), at_ms)
        else:
            record.restore("codex", (import_id, manifest), 1000, at_ms)
    assert tuple(database.execute("SELECT * FROM native_auth_imports").fetchone()) == before
    assert _record(app, "codex")["startupConfirmedAt"] is None


@pytest.mark.parametrize("at_ms", [0, -1, True, False])
def test_invalid_carrier_clock_leaves_confirmation_unknown(provider_app, at_ms):
    _login(provider_app, "codex", ["auth.json"])
    proof = capture(provider_app)
    select(proof, provider_app)
    proof.confirm(at_ms)
    assert _record(provider_app, "codex")["startupConfirmedAt"] is None


@pytest.mark.parametrize("field", ["imported_at_ms", "confirmed_at_ms"])
@pytest.mark.parametrize("at_ms", [0, -1, 1.5, "invalid"])
def test_inventory_does_not_publish_invalid_stored_proof(provider_app, field, at_ms):
    app = provider_app
    _login(app, "codex", ["auth.json"])
    proof = capture(app)
    select(proof, app)
    proof.confirm(2000)
    database = app._agent_registry._db
    with database:
        database.execute(f"UPDATE native_auth_imports SET {field}=?", (at_ms,))
    row = _record(app, "codex")
    assert row["state"] == "stored"
    assert row["startupConfirmedAt"] is None
    if field == "imported_at_ms":
        assert row["importedAt"] is None
        assert capture(app) is None


@pytest.mark.parametrize("field", ["importedAt", "startupConfirmedAt"])
@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "1", None, 1, 2000])
def test_auth_inventory_contract_requires_positive_integer_or_null(field, value):
    schema = SPEC.result_schema["properties"]["records"]["items"]["properties"][field]
    valid = value is None or (type(value) is int and value > 0)
    assert Draft202012Validator(schema).is_valid(value) is valid
