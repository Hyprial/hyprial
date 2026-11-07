"""agent_home_dirs: destruction scenarios."""

from __future__ import annotations
import shutil
import time
from pathlib import Path
import pytest
from hyprial.identity.impl.agents import (
    AgentHomeError,
    SecretResolver,
    SecretResolutionError,
    SecretSource,
)
from hyprial.identity.impl.agents.home.provisioner import HomeReceipt
from hyprial.identity.impl.agents.actor._core import AgentActor
from hyprial.identity.impl.agents.actor.ports import (
    CleanupRevokedAgentHomeCommand,
    CreateAgentCommand,
)
from hyprial.kernel.impl.contracts.ports import PortAdmission, PortCommandRejected
from hyprial.daemon.impl.application import DaemonRequestError

from .support import (
    _AgentEvents,
    _application,
    _close_application,
    _isolated_environment as _isolated_environment,
    _registry,
)


def test_t13_destroy_and_rebuild_cannot_inherit_home_or_grants(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    resolver = SecretResolver(tmp_path / "home", registry)
    old = registry.create("worker")
    receipt = registry.home_receipt("worker")
    legacy = Path(receipt.path) / "secrets" / "provider-demo.json"
    legacy.parent.mkdir(mode=0o700, exist_ok=True)
    legacy.write_text('{"key":"sentinel-old"}', encoding="utf-8")
    legacy.chmod(0o600)
    registry.grant_secret(
        "worker",
        grant_id="provider-demo",
        source=SecretSource.AGENT_PRIVATE,
        entry_id="provider-demo.json",
        field_name="key",
        environment_names=("OPENAI_API_KEY",),
        revision=1,
    )
    assert resolver.resolve("worker", "provider-demo").value == "sentinel-old"

    assert registry.destroy("worker")
    assert registry.secret_inventory() == ()
    assert not Path(receipt.path).exists()
    with pytest.raises(AgentHomeError) as fenced:
        registry.cleanup_home("worker", expected_token="wrong-token")
    assert fenced.value.category == "cleanup-fenced"

    rebuilt = registry.create("worker")
    assert rebuilt.entity_token != old.entity_token
    with pytest.raises(SecretResolutionError) as missing:
        resolver.resolve("worker", "provider-demo")
    assert missing.value.category == "grant-missing"


def test_t13_create_resumes_cleanup_after_destroy_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    original = registry.create("worker")
    receipt = registry.home_receipt("worker")
    sentinel = Path(receipt.path) / "secrets" / "old-auth.json"
    sentinel.parent.mkdir(mode=0o700, exist_ok=True)
    sentinel.write_text("NON_PRODUCTION_SENTINEL\n", encoding="utf-8")
    cleanup = registry.cleanup_home

    def crash_after_revoke(_actor: str, *, expected_token: str) -> HomeReceipt:
        assert expected_token == receipt.resource_token
        raise RuntimeError("simulated crash after durable revoke")

    monkeypatch.setattr(registry, "cleanup_home", crash_after_revoke)
    with pytest.raises(RuntimeError, match="simulated crash"):
        registry.destroy("worker")
    assert registry.get("worker") is None
    assert sentinel.exists()

    monkeypatch.setattr(registry, "cleanup_home", cleanup)
    rebuilt = registry.create("worker")
    assert rebuilt.entity_token != original.entity_token
    assert not sentinel.exists()


def test_unexpected_create_effect_error_settles_and_keeps_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unhandled effect failure must answer, not wait for the domain deadline.

    The durable create claim stays replayable: the diagnostic rejection is not
    the expected-error path that aborts a fresh claim.
    """

    registry = _registry(tmp_path)
    events = _AgentEvents()
    actor = AgentActor(registry, event_sink=events)
    provisioner = registry._home
    assert provisioner is not None
    provision = provisioner.provision_claimed
    calls = 0

    def explode(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise RuntimeError("synthetic unexpected home failure")

    monkeypatch.setattr(provisioner, "provision_claimed", explode)
    try:
        started = time.monotonic()
        assert actor.submit(
            CreateAgentCommand("create-unexpected", "worker")
        ) is PortAdmission.ACCEPTED
        rejected = events.wait("create-unexpected", PortCommandRejected)
        elapsed = time.monotonic() - started

        assert calls == 1
        assert elapsed < 2.0, "the caller waited for the domain deadline"
        assert rejected.code == "RuntimeError"
        assert "synthetic unexpected home failure" in rejected.detail
        assert registry.get("worker") is None

        monkeypatch.setattr(provisioner, "provision_claimed", provision)
        resumed = registry.create("worker")
        assert resumed.actor == "worker"
        assert registry.home_receipt("worker") is not None
    finally:
        assert actor.drain(3.0).complete


def test_t13_partial_cleanup_keeps_receipt_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path)
    application.handle("agent.create", {"name": "worker"})
    registry = application._agent_registry
    receipt = registry.home_receipt("worker")
    root = Path(receipt.path)
    for relative in ("config/old.json", "secrets/old-auth.json"):
        path = root / relative
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_text("NON_PRODUCTION_SENTINEL\n", encoding="utf-8")

    provisioner = registry._home
    assert provisioner is not None
    remove = provisioner._remove_tree_entry
    calls = 0

    def interrupt_cleanup(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption")
        remove(path)

    monkeypatch.setattr(provisioner, "_remove_tree_entry", interrupt_cleanup)
    with pytest.raises(DaemonRequestError, match="cleanup-not-empty"):
        application.handle("agent.destroy", {"name": "worker"})
    assert (root / "state" / "home-receipt.json").exists()

    monkeypatch.setattr(provisioner, "_remove_tree_entry", remove)
    _close_application(application)
    restarted = _application(tmp_path)
    cleaned = restarted.handle("agent.destroy", {"name": "worker"})
    assert cleaned["cleanupResumed"] is True
    assert cleaned["destroyed"] is False
    assert not root.exists()


def test_t13_receipt_missing_cleanup_rejects_symlinked_agents_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path)
    application.handle("agent.create", {"name": "worker"})
    registry = application._agent_registry
    receipt = registry.home_receipt("worker")
    provisioner = registry._home
    assert provisioner is not None
    cleanup = provisioner.cleanup

    def crash_after_revoke(
        _receipt: HomeReceipt, *, expected_token: str
    ) -> HomeReceipt:
        assert expected_token == receipt.resource_token
        raise RuntimeError("simulated crash after durable revoke")

    monkeypatch.setattr(provisioner, "cleanup", crash_after_revoke)
    with pytest.raises(DaemonRequestError, match="simulated crash") as rejected:
        application.handle("agent.destroy", {"name": "worker"})
    assert rejected.value.code == "RuntimeError"
    monkeypatch.setattr(provisioner, "cleanup", cleanup)

    root = Path(receipt.path)
    agents_root = root.parent
    (root / "state" / "home-receipt.json").unlink()
    shutil.rmtree(root / "workspace")
    for relative in ("config", "state"):
        (root / relative).rmdir()
    root.rmdir()
    agents_root.rmdir()

    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    outside_victim = outside / "worker"
    outside_victim.mkdir(mode=0o700)
    agents_root.symlink_to(outside, target_is_directory=True)

    _close_application(application)
    restarted = _application(tmp_path)
    with pytest.raises(DaemonRequestError, match="unsafe-path"):
        restarted.handle("agent.destroy", {"name": "worker"})
    assert outside_victim.is_dir()


@pytest.mark.parametrize(
    "crash_boundary",
    ("receipt", "workspace", "secrets", "config", "state", "root"),
)
def test_t13_receipt_missing_cleanup_converges_at_every_boundary(
    crash_boundary: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    application.handle("agent.create", {"name": "worker"})
    registry = application._agent_registry
    receipt = registry.home_receipt("worker")
    root = Path(receipt.path)
    receipt_path = root / "state" / "home-receipt.json"
    (root / "secrets").mkdir(mode=0o700)  # optional legacy/native scaffold
    workspace = root / "workspace"
    assert (workspace / ".git").is_dir()
    rmdir_boundaries = {
        "workspace": workspace,
        "secrets": root / "secrets",
        "config": root / "config",
        "state": root / "state",
        "root": root,
    }
    unlink = Path.unlink
    rmdir = Path.rmdir

    def interrupt_unlink(path: Path, *args: object, **kwargs: object) -> None:
        unlink(path, *args, **kwargs)
        if crash_boundary == "receipt" and path == receipt_path:
            raise RuntimeError("simulated crash after receipt unlink")

    def interrupt_rmdir(path: Path) -> None:
        rmdir(path)
        if rmdir_boundaries.get(crash_boundary) == path:
            raise RuntimeError(f"simulated crash after {crash_boundary} rmdir")

    monkeypatch.setattr(Path, "unlink", interrupt_unlink)
    monkeypatch.setattr(Path, "rmdir", interrupt_rmdir)
    with pytest.raises(DaemonRequestError, match="simulated crash") as rejected:
        application.handle("agent.destroy", {"name": "worker"})
    assert rejected.value.code == "RuntimeError"
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(Path, "rmdir", rmdir)

    _close_application(application)
    restarted = _application(tmp_path)
    cleaned = restarted.handle("agent.destroy", {"name": "worker"})
    assert cleaned["cleanupResumed"] is True
    assert cleaned["destroyed"] is False
    assert not root.exists()
    recreated = restarted.handle("agent.create", {"name": "worker"})
    assert recreated["created"] is True


def test_t13_bad_receipt_and_foreign_uri_never_authorize_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path)
    application.handle("agent.create", {"name": "worker"})
    registry = application._agent_registry
    receipt = registry.home_receipt("worker")
    sentinel = Path(receipt.path) / "secrets" / "old-auth.json"
    sentinel.parent.mkdir(mode=0o700, exist_ok=True)
    sentinel.write_text("NON_PRODUCTION_SENTINEL\n", encoding="utf-8")
    provisioner = registry._home
    assert provisioner is not None
    cleanup = provisioner.cleanup

    monkeypatch.setattr(
        provisioner,
        "cleanup",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated crash after durable revoke")
        ),
    )
    with pytest.raises(DaemonRequestError, match="simulated crash") as rejected:
        application.handle("agent.destroy", {"name": "worker"})
    assert rejected.value.code == "RuntimeError"
    monkeypatch.setattr(provisioner, "cleanup", cleanup)

    _close_application(application)
    restarted = _application(tmp_path)
    with pytest.raises(DaemonRequestError) as foreign:
        restarted.handle(
            "agent.destroy", {"name": "agent:other:node:worker"}
        )
    assert foreign.value.code == "AGENT_NOT_FOUND"
    assert sentinel.exists()
    restarted_registry = restarted._agent_registry
    with restarted_registry._lock, restarted_registry._db:
        restarted_registry._db.execute(
            "UPDATE lifecycle_resources SET payload = ? WHERE resource_key = ?",
            ("{}", "agent-home:worker"),
        )
    with pytest.raises(DaemonRequestError, match="invalid-registry-receipt"):
        restarted.handle("agent.destroy", {"name": "worker"})
    events = _AgentEvents()
    actor = AgentActor(restarted_registry, event_sink=events)
    try:
        command = CleanupRevokedAgentHomeCommand(
            "typed-invalid-registry-receipt", "worker"
        )
        assert actor.submit(command) is PortAdmission.ACCEPTED
        rejected = events.wait(
            "typed-invalid-registry-receipt", PortCommandRejected
        )
        assert rejected.detail == (
            "agent home invalid-registry-receipt: "
            "actor='worker' phase='cleanup-registry'"
        )
        assert sentinel.exists()
    finally:
        assert actor.drain(3.0).complete
    assert sentinel.exists()


def test_t13_real_authority_rotation_revokes_but_alias_updates_do_not(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    original = registry.create("worker")
    registry.grant_secret(
        "worker",
        grant_id="shared",
        source=SecretSource.USER_PROVIDER,
        entry_id="shared",
        field_name="key",
        environment_names=("DEEPSEEK_API_KEY",),
        revision=1,
    )
    alias = registry.update("worker", owner="display-alias")
    assert alias.entity_token == original.entity_token
    assert registry.secret_grant("worker", "shared") is not None

    rotated = registry.rotate_secret_authority("worker")
    assert rotated.entity_token != original.entity_token
    assert registry.secret_grant("worker", "shared") is None
    with pytest.raises(AgentHomeError) as revoked:
        registry.home_receipt("worker")
    assert revoked.value.category == "receipt-mismatch"
