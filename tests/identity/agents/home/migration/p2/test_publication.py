"""agent_home_p2_migration: publication scenarios."""

from __future__ import annotations
import json
import os
import shutil
from pathlib import Path
import pytest
import hyprial.identity.impl.agents.migration.coordinator as migration_module
from hyprial.identity.impl.agents import (
    AgentConfig,
)
from hyprial.identity.impl.agents.migration._base import (
    AgentHomeMigrationError,
    AgentMigrationAuthorization,
    MigrationEntry,
    MigrationPhase,
)
from hyprial.identity.impl.agents.migration.coordinator import (
    AgentMigrationCoordinator,
    )

from .support import (
    _Bindings,
    _authorization,
    _coordinator,
    _isolated_environment as _isolated_environment,
    _plan,
    _registry,
    _source,
    _support,
)


def test_t15_publish_retires_source_is_idempotent_and_rolls_back_per_agent(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        bindings = _Bindings(registry)
        coordinator, plan, source = _plan(tmp_path, registry, bindings)

        completed = coordinator.execute(plan, bindings=bindings)
        repeated = coordinator.execute(plan, bindings=bindings)
        target = Path(registry.home_receipt("worker").path) / "config"

        assert completed.phase is MigrationPhase.COMPLETE
        assert repeated == completed
        assert not source.exists()
        assert json.loads(
            (target / "native" / "codex" / "settings.json").read_text()
        )["sentinel"] == (
            "NON_PRODUCTION_SENTINEL"
        )
        assert registry.require("worker").config == AgentConfig(str(target))
        assert bindings.events == ["preflight", "activate", "source-check"]

        rolled_back = coordinator.rollback(
            plan,
            authorization=_authorization(registry, "worker"),
            bindings=bindings,
        )
        assert rolled_back.phase is MigrationPhase.ROLLED_BACK
        assert source.exists()
        assert target.is_dir() and not tuple(target.iterdir())
        assert registry.require("worker").config is None
    finally:
        registry.close()


def test_t17_multi_entry_rollback_resumes_after_process_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        bindings = _Bindings(registry)
        config_source = _source(tmp_path / "config-source")
        tools_source = tmp_path / "legacy-tools"
        tools_source.mkdir(mode=0o700)
        (tools_source / "login.json").write_text(
            "NON_PRODUCTION_SENTINEL\n", encoding="utf-8"
        )
        (tools_source / "login.json").chmod(0o600)
        coordinator = _coordinator(registry)
        plan = coordinator.preflight(
            _authorization(registry, "worker"),
            (
                MigrationEntry(str(config_source), "config", False),
                MigrationEntry(str(tools_source), "secrets/tools", True),
            ),
            bindings=bindings,
        )
        coordinator.execute(plan, bindings=bindings)
        root = Path(registry.home_receipt("worker").path)
        config_target = root / "config"
        record_path = root / "state" / "migrations" / plan.migration_id / "record.json"
        original_rename = migration_module.os.rename

        def crash_before_second_entry(source: Path, destination: Path) -> None:
            if Path(source) == config_target:
                raise KeyboardInterrupt("simulated process death")
            original_rename(source, destination)

        monkeypatch.setattr(migration_module.os, "rename", crash_before_second_entry)
        with pytest.raises(KeyboardInterrupt, match="simulated process death"):
            coordinator.rollback(
                plan,
                authorization=_authorization(registry, "worker"),
                bindings=bindings,
            )

        interrupted = json.loads(record_path.read_text(encoding="utf-8"))
        assert interrupted["phase"] == "rolling-back"
        assert tools_source.is_dir()
        assert not config_source.exists()

        monkeypatch.setattr(migration_module.os, "rename", original_rename)
        resumed = coordinator.rollback(
            plan,
            authorization=_authorization(registry, "worker"),
            bindings=bindings,
        )

        assert resumed.phase is MigrationPhase.ROLLED_BACK
        assert config_source.is_dir()
        assert tools_source.is_dir()
        assert registry.require("worker").config is None
        assert bindings.events.count("rollback") == 2
    finally:
        registry.close()


def test_t15_rollback_requires_a_live_fresh_authorization_window(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        clock = [1_000]
        coordinator = AgentMigrationCoordinator(
            registry,
            _support(),
            liveness_probe=lambda _uri, _token: True,
            clock_ms=lambda: clock[0],
        )
        bindings = _Bindings(registry)
        source = _source(tmp_path)
        original = _authorization(registry, "worker")
        plan = coordinator.preflight(
            original,
            (MigrationEntry(str(source), "config", False),),
            bindings=bindings,
        )
        coordinator.execute(plan, bindings=bindings)

        clock[0] = 3_000
        with pytest.raises(AgentHomeMigrationError) as expired:
            coordinator.rollback(plan, authorization=original, bindings=bindings)
        assert expired.value.category == "authorization-expired"

        fresh = AgentMigrationAuthorization(
            original.actor,
            original.entity_token,
            original.home_resource_token,
            "authorized-window-2",
            "operator@example.test",
            4_000,
        )
        rolled_back = coordinator.rollback(
            plan, authorization=fresh, bindings=bindings
        )
        assert rolled_back.phase is MigrationPhase.ROLLED_BACK
        assert source.exists()
    finally:
        registry.close()


def test_t15_crash_mid_publish_resumes_from_durable_phase(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        bindings = _Bindings(registry)
        coordinator, plan, source = _plan(tmp_path, registry, bindings)
        root = Path(registry.home_receipt("worker").path)
        migrations = root / "state" / "migrations"
        migrations.mkdir(mode=0o700)
        transaction = migrations / plan.migration_id
        staging = transaction / "staging"
        backups = transaction / "target-backups"
        transaction.mkdir(mode=0o700)
        staging.mkdir(mode=0o700)
        backups.mkdir(mode=0o700)
        shutil.copytree(source, staging / "0")
        (transaction / "plan.json").write_text(
            json.dumps(plan.to_json(), sort_keys=True) + "\n", encoding="utf-8"
        )
        (transaction / "plan.json").chmod(0o600)
        record = {
            "schemaVersion": 1,
            "migrationId": plan.migration_id,
            "planDigest": plan.digest,
            "actor": "worker",
            "phase": "staged",
            "startedAtMs": 1_000,
            "updatedAtMs": 1_000,
            "deadlineMs": 2_000,
            "nextResponsible": "operator@example.test",
            "failurePhase": None,
            "failureCategory": None,
        }
        (transaction / "record.json").write_text(
            json.dumps(record, sort_keys=True) + "\n", encoding="utf-8"
        )
        (transaction / "record.json").chmod(0o600)

        target = root / "config"
        os.rename(target, backups / "0")
        os.rename(staging / "0", target)
        # Simulated process death here: target 0 is published, while the
        # durable phase still says STAGED and no binding/source-exit ran.
        completed = coordinator.execute(plan, bindings=bindings)

        assert completed.phase is MigrationPhase.COMPLETE
        assert not source.exists()
        assert registry.require("worker").config == AgentConfig(str(target))
        assert bindings.events == ["preflight", "activate", "source-check"]
    finally:
        registry.close()


def test_t15_copy_failure_leaves_source_and_empty_incumbent_and_is_durable(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        bindings = _Bindings(registry)
        source = _source(tmp_path)

        def fail_copy(_source: Path, _destination: Path) -> None:
            raise OSError("injected copy failure")

        coordinator = _coordinator(registry, copy_file=fail_copy)
        plan = coordinator.preflight(
            _authorization(registry, "worker"),
            (MigrationEntry(str(source), "config", False),),
            bindings=bindings,
        )
        with pytest.raises(AgentHomeMigrationError) as failed:
            coordinator.execute(plan, bindings=bindings)

        target = Path(registry.home_receipt("worker").path) / "config"
        record = json.loads(
            (target.parent / "state" / "migrations" / plan.migration_id / "record.json").read_text()
        )
        assert failed.value.category == "OSError"
        assert source.exists()
        assert target.is_dir() and not tuple(target.iterdir())
        assert record["phase"] == "failed"
        assert record["failurePhase"] == "stage"
        assert record["failureCategory"] == "OSError"
        assert record["deadlineMs"] == 2_000
        assert record["nextResponsible"] == "operator@example.test"
        assert bindings.events == ["preflight"]
    finally:
        registry.close()


def test_t15_source_still_in_use_compensates_binding_and_publication(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        bindings = _Bindings(registry, legacy_in_use=True)
        coordinator, plan, source = _plan(tmp_path, registry, bindings)

        with pytest.raises(AgentHomeMigrationError) as failed:
            coordinator.execute(plan, bindings=bindings)

        target = Path(registry.home_receipt("worker").path) / "config"
        assert failed.value.category == "legacy-source-in-use"
        assert source.exists()
        assert target.is_dir() and not tuple(target.iterdir())
        assert registry.require("worker").config is None
        assert bindings.events == ["preflight", "activate", "source-check", "rollback"]
    finally:
        registry.close()


def test_t17_binding_compensation_failure_does_not_skip_filesystem_compensation(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")

        class BrokenRollback(_Bindings):
            def rollback(self, plan) -> None:  # noqa: ANN001
                self.events.append("rollback")
                raise RuntimeError("injected binding rollback failure")

            def legacy_sources_in_use(self, plan) -> tuple[str, ...]:  # noqa: ANN001
                self.events.append("source-check")
                raise RuntimeError("injected source-use observation failure")

        bindings = BrokenRollback(registry)
        coordinator, plan, source = _plan(tmp_path, registry, bindings)
        with pytest.raises(AgentHomeMigrationError):
            coordinator.execute(plan, bindings=bindings)

        target = Path(registry.home_receipt("worker").path) / "config"
        record = json.loads(
            (target.parent / "state" / "migrations" / plan.migration_id / "record.json").read_text()
        )
        assert source.exists()
        assert target.is_dir() and not tuple(target.iterdir())
        assert record["phase"] == "rollback-failed"
        assert record["failureCategory"].endswith("+compensation-error")
    finally:
        registry.close()
