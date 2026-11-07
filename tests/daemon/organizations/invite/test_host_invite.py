"""Host invitations through the real daemon / Agent command pipeline.

No worker is started. Only network presence and the inactive harness status
surface are doubles; identity, home provisioning and SQLite are real.
"""

from __future__ import annotations

import sqlite3
import shutil
from pathlib import Path

import pytest

from hyprial.identity.impl.agents.runtime.secrets import SecretSource
from hyprial.daemon.impl.application import DaemonApplication, DaemonRequestError
from hyprial.daemon.impl.configuration.identity import write_settings_owner
from hyprial.daemon.impl.owner_migration import MIGRATION_TEXT_FILES, OwnerMigrationHostedConflict
from hyprial.daemon.impl.inbox import InboxService, MemoryDeliveryTransport
from hyprial.shell.impl.login.preview import LoginPreviewError, preview_owner_migration
from tests.identity.agents.registry.entity.support import StubPresence
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

TABLES = (
    "agents",
    "pins",
    "agent_secret_grants",
    "lifecycle_resources",
    "lifecycle_receipts",
)


def database_rows(database: Path) -> dict[str, list[tuple]]:
    connection = sqlite3.connect(database)
    try:
        return {
            table: sorted(connection.execute(f"SELECT * FROM {table}").fetchall())
            for table in TABLES
        }
    finally:
        connection.close()


def home_files(home: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(home)): path.read_bytes()
        for path in home.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def app_factory(tmp_path, own_daemon_application):
    class InactiveHarnesses:
        def status(self):
            return ()

    def create(root: Path | None = None, *, owner="host-owner"):
        root = root or tmp_path
        home = root / "home"
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        app = own_daemon_application(
            DaemonApplication(
                state_dir=root / "state",
                socket_path=root / "daemon.sock",
                owner=owner,
                node_id="host-node",
                hyprial_home=home,
            **_deep_module_daemon_dependencies())
        )
        app._inbox = InboxService(root / "inbox.sqlite3", MemoryDeliveryTransport())
        app._presence = StubPresence()
        app._harnesses = InactiveHarnesses()
        return app

    return create


@pytest.mark.parametrize("incumbent", ["native", "hosted"])
def test_creation_duplicate_preserves_existing(app_factory, incumbent):
    app = app_factory()
    if incumbent == "native":
        app.handle("agent.create", {"name": "taken"})
    else:
        app.handle("agent.host-invite", {"name": "taken", "owner": "first-owner"})
    receipt = app._agent_registry.home_receipt("taken")
    (Path(receipt.path) / "config/keep.txt").write_text("synthetic user content")
    app._agent_registry.grant_secret(
        "taken",
        grant_id="existing",
        source=SecretSource.USER_PROVIDER,
        entry_id="synthetic-only",
        field_name="key",
        environment_names=("DEEPSEEK_API_KEY",),
        revision=1,
    )
    before = database_rows(app._agent_registry.database)
    files = home_files(app.hyprial_home)
    with pytest.raises(DaemonRequestError):
        app.handle("agent.host-invite", {"name": "taken", "owner": "second-owner"})
    assert database_rows(app._agent_registry.database) == before
    assert home_files(app.hyprial_home) == files
    assert app._agent_registry.home_receipt("taken") == receipt


def test_duplicate_host_invite_keeps_hosted_scenario_error_code(app_factory):
    app = app_factory()
    app.handle("agent.host-invite", {"name": "guest", "owner": "first-owner"})
    before = database_rows(app._agent_registry.database)
    files = home_files(app.hyprial_home)
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("agent.host-invite", {"name": "guest", "owner": "second-owner"})
    assert failure.value.code == "AGENT_ERROR"
    assert database_rows(app._agent_registry.database) == before
    assert home_files(app.hyprial_home) == files


@pytest.mark.parametrize(
    "owner", ["", "a:b", "host-owner"]
)
def test_boundary_owner(app_factory, owner):
    app = app_factory()
    before = database_rows(app._agent_registry.database)
    files = home_files(app.hyprial_home)
    with pytest.raises(DaemonRequestError) as failure:
        app.handle("agent.host-invite", {"name": "visitor", "owner": owner})
    assert failure.value.code == "INVALID_ARGUMENT"
    assert database_rows(app._agent_registry.database) == before
    assert home_files(app.hyprial_home) == files
    assert not (app.hyprial_home / "agents/visitor").exists()


def test_start_cannot_reown_invited_agent(app_factory):
    app = app_factory()
    app.handle("agent.host-invite", {"name": "visitor", "owner": "visitor-owner"})
    before = database_rows(app._agent_registry.database)
    with pytest.raises(DaemonRequestError, match="start cannot re-own") as failure:
        app.handle("lifecycle.start", {
            "name": "visitor", "provider": "pi", "headless": True,
            "pinnedOwner": "host-owner",
        })
    assert failure.value.code == "INVALID_ARGUMENT"
    assert database_rows(app._agent_registry.database) == before
    assert app.desired_state.load().harnesses == ()


@pytest.mark.parametrize("with_grant", [False, True])
@pytest.mark.parametrize("count", [1, 2])
def test_creation_restart_preserves_hosted_identity(app_factory, count, with_grant):
    app = app_factory()
    for index in range(count):
        app.handle(
            "agent.host-invite", {"name": f"guest-{index}", "owner": f"visitor-{index}"}
        )
        if with_grant:
            app._agent_registry.grant_secret(
                f"guest-{index}",
                grant_id="synthetic",
                source=SecretSource.USER_PROVIDER,
                entry_id="synthetic-only",
                field_name="key",
                environment_names=("DEEPSEEK_API_KEY",),
                revision=1,
            )
    database, home = app._agent_registry.database, app.hyprial_home
    before, files = database_rows(database), home_files(home)
    app._close()
    reopened = app_factory()
    assert reopened._owner_migration_rewrites == 0
    assert database_rows(database) == before, "restart changed hosted identity"
    assert home_files(home) == files
    for index in range(count):
        actor = f"guest-{index}"
        assert reopened.agents.require(actor).owner == f"visitor-{index}"
        assert (
            reopened._agent_registry.home_receipt(actor).entity_token
            == reopened.agents.require(actor).entity_token
        )


def test_native_alias_still_migrates_with_hosted_visitor(app_factory):
    app = app_factory(owner="old-native-owner")
    app.handle("agent.create", {"name": "native"})
    app.handle("agent.host-invite", {"name": "visitor", "owner": "visitor-owner"})
    visitor = app.agents.require("visitor")
    receipt = app._agent_registry.home_receipt("visitor")
    files = home_files(app.hyprial_home)
    app._close()
    reopened = app_factory(owner="new-native-owner")
    assert reopened._owner_migration_rewrites > 0
    assert reopened.agents.require("native").owner == "new-native-owner"
    assert reopened.agents.require("visitor") == visitor
    assert reopened._agent_registry.home_receipt("visitor") == receipt
    assert home_files(reopened.hyprial_home) == files


def _alias_state(root: Path) -> dict:
    """Logical database contents plus home bytes/modes, excluding SQLite journals."""
    result = {}
    for path in sorted(root.rglob("*.sqlite3")):
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            result[str(path.relative_to(root))] = {
                table: sorted(db.execute(
                    'SELECT * FROM "' + table.replace('"', '""') + '"'
                ).fetchall(), key=repr) for (table,) in tables
            }
        finally:
            db.close()
    result["home"] = {
        str(path.relative_to(root)): (path.stat().st_mode, path.read_bytes())
        for path in (root / "home").rglob("*") if path.is_file()
    }
    result["state_text"] = {
        name: (path.stat().st_mode, path.read_bytes())
        for name in MIGRATION_TEXT_FILES
        if (path := root / "state" / name).is_file()
    }
    return result


@pytest.mark.parametrize("hosted_owner", ["old-native-owner", "new-native-owner"])
def test_hosted_owner_collision_refuses_before_any_rewrite(
    app_factory, tmp_path, hosted_owner,
):
    old, new = "old-native-owner", "new-native-owner"
    first = app_factory(owner=old)
    write_settings_owner(old, environ={"HYPRIAL_HOME": str(first.hyprial_home)})
    first.handle("agent.create", {"name": "native"})
    if hosted_owner == old:
        first.agents.create_transfer_hosted("visitor", pinned_owner=old)
    else:
        first.handle("agent.host-invite", {"name": "visitor", "owner": new})
    first.agents.pin("lark:synthetic-alias", "visitor")
    first._close()
    before = _alias_state(tmp_path)

    # H1/H2's mutation must fail here as DID NOT RAISE, not in fixture setup.
    with pytest.raises(OwnerMigrationHostedConflict) as caught:
        app_factory(owner=new)
    assert caught.value.code == "OWNER_MIGRATION_HOSTED_CONFLICT"
    assert caught.value.data == {
        "old": old, "new": new, "actor": "visitor", "hostedOwner": hosted_owner,
    }
    assert "restore the previous login configuration" in str(caught.value)
    assert (
        "Move or migrate" if hosted_owner == old else "Choose a new owner"
    ) in str(caught.value)
    assert _alias_state(tmp_path) == before

    removed = []

    def cleanup(path):
        removed.append(path)
        shutil.rmtree(path)

    with pytest.raises(LoginPreviewError) as preview:
        preview_owner_migration(
            state_dir=tmp_path / "state", hyprial_home=tmp_path / "home",
            target_owner=new, cleanup=cleanup,
        )
    assert preview.value.code == caught.value.code
    assert removed and all(not path.exists() for path in removed)
    assert _alias_state(tmp_path) == before

    restored = app_factory(owner=old)
    assert restored._owner_migration_rewrites == 0
    restored._close()
    assert _alias_state(tmp_path) == before
