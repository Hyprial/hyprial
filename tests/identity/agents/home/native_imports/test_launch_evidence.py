"""Native success belongs to the selected import generation, not an arbitrary turn."""

import pytest

from tests.daemon.harnesses.providers.test_daemon_provider_login import (
    ACTOR, _agent_home, provider_app as provider_app,
)
from tests.daemon.harnesses.providers.test_native_auth_imports import _login, _record
from tests.support.runtime._owned_applications import owned_applications as owned_applications


def capture(app, harness="codex"):
    from hyprial.identity.impl.agents.home.native_imports.launch import capture_native_auth_launch

    return capture_native_auth_launch(
        _agent_home(app), harness,
        import_record=app._agent_registry.native_credential_import_record(ACTOR),
    )


def select(proof, app, harness="codex"):
    proof.select(_agent_home(app) / "secrets" / "native" / harness)


@pytest.mark.parametrize("harness,names", [
    ("claude", [".credentials.json"]), ("codex", ["auth.json"]),
    ("pi", ["auth.json", "models-store.json"]),
])
def test_success_requires_actual_native_selection_and_persists(provider_app, harness, names):
    app = provider_app
    _login(app, harness, names)
    proof = capture(app, harness)
    assert proof is not None
    proof.confirm(2000)
    assert _record(app, harness)["startupConfirmedAt"] is None
    select(proof, app, harness)
    proof.confirm(2000)
    proof.confirm(1500)
    assert _record(app, harness)["startupConfirmedAt"] == 2000
    from hyprial.identity import AgentRegistry, inspect_native_credentials

    registry = AgentRegistry(app._agent_registry.database, owner=app.owner, machine=app.node_id,
                             hyprial_home=app.hyprial_home)
    try:
        observed = inspect_native_credentials(_agent_home(app), harness,
            import_record=registry.native_credential_import_record(ACTOR))
        assert observed["startupConfirmedAt"] == 2000
        assert set(observed) == {"state", "files", "missingFiles", "importedAt", "startupConfirmedAt"}
    finally:
        registry.close()


@pytest.mark.parametrize("change", ["replace", "remove-readd", "external", "incarnation"])
def test_stale_launch_cannot_confirm_new_or_changed_import(provider_app, monkeypatch, change):
    app = provider_app
    monkeypatch.setattr(app._agent_registry, "_clock", lambda: 1000)
    _login(app, "codex", ["auth.json"])
    proof = capture(app)
    select(proof, app)
    proof.confirm(2000)
    if change == "replace":
        _login(app, "codex", ["auth.json"], b"replacement", force=True)
    elif change == "remove-readd":
        app.handle("agent.provider.logout", {"actor": ACTOR, "provider": "codex"})
        _login(app, "codex", ["auth.json"])
    elif change == "external":
        (_agent_home(app) / "secrets/native/codex/auth.json").write_bytes(b"external")
    else:
        app._agent_registry.rotate_secret_authority(ACTOR)
    proof.confirm(3000)
    assert _record(app, "codex")["startupConfirmedAt"] is None


def test_noop_import_preserves_generation_but_non_native_reconnect_cannot_confirm(provider_app):
    app = provider_app
    _login(app, "codex", ["auth.json"])
    proof = capture(app)
    select(proof, app)
    proof.confirm(1000)
    _login(app, "codex", ["auth.json"])
    proof.confirm(2000)
    assert _record(app, "codex")["startupConfirmedAt"] == 2000
    proof.select(None)
    proof.confirm(3000)
    assert _record(app, "codex")["startupConfirmedAt"] == 2000


def test_foreign_root_and_untracked_files_cannot_confirm(provider_app, tmp_path):
    app = provider_app
    _login(app, "codex", ["auth.json"])
    proof = capture(app)
    proof.select(tmp_path)
    proof.confirm(2000)
    assert _record(app, "codex")["startupConfirmedAt"] is None
    app._agent_registry.native_credential_import_record(ACTOR).clear("codex")
    assert capture(app) is None


def test_capture_before_replacement_cannot_select_replacement(provider_app):
    app = provider_app
    _login(app, "codex", ["auth.json"])
    proof = capture(app)
    _login(app, "codex", ["auth.json"], b"replacement", force=True)
    select(proof, app)
    proof.confirm(2000)
    assert _record(app, "codex")["startupConfirmedAt"] is None


def test_old_schema_migrates_without_claiming_success_or_leaving_a_transaction_open():
    import sqlite3
    from hyprial.identity.impl.agents.home.native_imports import initialize

    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE native_auth_imports (actor TEXT,entity_token TEXT,harness TEXT,"
                           "resource_token TEXT,manifest TEXT,imported_at_ms INTEGER,PRIMARY KEY(actor,harness))")
        with connection:
            connection.execute("INSERT INTO native_auth_imports VALUES ('actor','entity','codex','resource','[]',1000)")
        initialize(connection)
        first = connection.execute("SELECT imported_at_ms,import_id,confirmed_at_ms FROM native_auth_imports").fetchone()
        assert first[0] == 1000 and first[1] and first[2] is None
        assert not connection.in_transaction
        initialize(connection)
        assert connection.execute("SELECT imported_at_ms,import_id,confirmed_at_ms FROM native_auth_imports").fetchone() == first
    finally:
        connection.close()


def test_rolled_back_import_retains_old_success_and_generation(provider_app, monkeypatch):
    from pathlib import Path
    from hyprial.identity.impl.agents.home import native_credential as native
    from hyprial.kernel import DaemonRequestError

    app = provider_app
    names = ["auth.json", "models-store.json"]
    _login(app, "pi", names)
    proof = capture(app, "pi")
    select(proof, app, "pi")
    proof.confirm(1000)
    replace_file = native.os.replace
    def fail(source, destination, *args, **kwargs):
        if Path(source).name == "models-store.json.incoming":
            raise OSError("injected install failure")
        return replace_file(source, destination, *args, **kwargs)
    monkeypatch.setattr(native.os, "replace", fail)
    with pytest.raises(DaemonRequestError):
        _login(app, "pi", names, b"replacement", force=True)
    assert _record(app, "pi")["startupConfirmedAt"] == 1000
    proof.confirm(2000)
    assert _record(app, "pi")["startupConfirmedAt"] == 2000


def test_concurrent_registry_open_migrates_the_same_generation(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import sqlite3
    from hyprial.identity.impl.agents.home.native_imports import initialize

    database = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(database)
    with connection:
        connection.execute("CREATE TABLE native_auth_imports (actor TEXT,entity_token TEXT,harness TEXT,"
                           "resource_token TEXT,manifest TEXT,imported_at_ms INTEGER,PRIMARY KEY(actor,harness))")
        connection.execute("INSERT INTO native_auth_imports VALUES ('actor','entity','codex','resource','[]',1000)")
    connection.close()
    def open_registry(_):
        connection = sqlite3.connect(database)
        try:
            initialize(connection)
            assert not connection.in_transaction
            return connection.execute("SELECT import_id,confirmed_at_ms FROM native_auth_imports").fetchone()
        finally:
            connection.close()
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(open_registry, range(8)))
    assert len(set(values)) == 1 and values[0][0] and values[0][1] is None
