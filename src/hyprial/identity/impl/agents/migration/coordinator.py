from __future__ import annotations
from ._bindings import AgentMigrationBindings, AgentMigrationLivenessProbe

from hyprial.identity.impl.agents.home.config import AgentConfig
from hyprial.identity.impl.agents.home.config import AgentConfigError
from hyprial.identity.impl.agents.registry._core import AgentRegistry
from collections.abc import Callable
from collections.abc import Iterable
from pathlib import Path
from hyprial.identity.impl.agents.home.config import build_native_projection
import os
import shutil
import time
import uuid

from ._base import (
    AgentHomeMigrationError,
    AgentMigrationAuthorization,
    AgentMigrationPlan,
    MigrationEntry,
    MigrationPhase,
    MigrationRecord,
    SupportKey,
    SupportMatrix,
    _EntrySnapshot,
    _agent_config_content_digest,
    _config_content_digest,
    _config_revision,
    _copy_regular_file,
    _copy_tree,
    _ensure_private_directory,
    _fsync_directory,
    _is_within,
    _matches_shape,
    _read_plan,
    _read_record,
    _require_bindings,
    _safe_target_parents,
    _snapshot_tree,
    _tree_shape,
    _validate_target_slot,
    _write_json,
    _write_record,
)

class AgentMigrationCoordinator():
    """Preflight, publish, bind, retire, and roll back one agent at a time."""
    def __init__(
        self,
        registry: AgentRegistry,
        support: SupportMatrix,
        *,
        liveness_probe: AgentMigrationLivenessProbe,
        clock_ms: Callable[[], int] | None = None,
        copy_file: Callable[[Path, Path], None] | None = None,
    ) -> None:
        self.registry = registry
        self.support = support
        self._liveness_probe = liveness_probe
        self._clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._copy_file = copy_file or _copy_regular_file
    def preflight(
        self,
        authorization: AgentMigrationAuthorization,
        entries: Iterable[MigrationEntry],
        *,
        required_support: Iterable[SupportKey] = (),
        bindings: AgentMigrationBindings,
    ) -> AgentMigrationPlan:
        agent, receipt = self._authorize(authorization, "preflight")
        requested = tuple(entries)
        if not requested:
            raise AgentHomeMigrationError("empty-plan", "preflight", actor=agent.actor)
        destinations = [entry.destination for entry in requested]
        if len(destinations) != len(set(destinations)):
            raise AgentHomeMigrationError("duplicate-target", "preflight", actor=agent.actor)
        sources = [Path(entry.source) for entry in requested]
        if any(
            left == right or _is_within(left, right) or _is_within(right, left)
            for index, left in enumerate(sources)
            for right in sources[index + 1 :]
        ):
            raise AgentHomeMigrationError(
                "overlapping-source", "preflight-source", actor=agent.actor
            )
        config_entries = [entry for entry in requested if entry.destination == "config"]
        if (
            config_entries
            and agent.config is not None
            and Path(config_entries[0].source) != Path(agent.config.source)
        ):
            raise AgentHomeMigrationError(
                "config-source-mismatch", "preflight-config", actor=agent.actor
            )
        keys = tuple(required_support)
        declared_harnesses = {key.harness for key in keys}
        native_harnesses = {
            entry.destination.removeprefix("secrets/native/")
            for entry in requested
            if entry.destination.startswith("secrets/native/")
        }
        missing_support = native_harnesses - declared_harnesses
        if missing_support:
            raise AgentHomeMigrationError(
                "support-not-declared", "preflight-support", actor=agent.actor
            )
        self.support.require_pass(keys, actor=agent.actor)
        root = Path(receipt.path)
        snapshots: list[_EntrySnapshot] = []
        for entry in requested:
            source = Path(entry.source)
            if source.absolute() != source.resolve(strict=False):
                raise AgentHomeMigrationError(
                    "source-ancestor-link", "preflight-source", actor=agent.actor
                )
            if _is_within(source, root) or _is_within(root, source):
                raise AgentHomeMigrationError("overlapping-root", "preflight", actor=agent.actor)
            items = _snapshot_tree(source, agent.actor, private=entry.sensitive)
            target = root / entry.destination
            _validate_target_slot(target, root, agent.actor)
            snapshots.append(_EntrySnapshot(entry, items))
        prior_config_revision = _config_revision(agent)
        target_config_content_digest = _agent_config_content_digest(agent)
        if config_entries:
            try:
                candidate_config = AgentConfig(config_entries[0].source)
                candidate_manifest = candidate_config.freeze_manifest()
                for harness in ("claude", "codex", "pi"):
                    build_native_projection(candidate_manifest, harness)
                target_config_content_digest = _config_content_digest(
                    candidate_manifest
                )
            except AgentConfigError as error:
                raise AgentHomeMigrationError(
                    "invalid-config-source", "preflight-config", actor=agent.actor
                ) from error
        plan = AgentMigrationPlan(
            uuid.uuid4().hex,
            authorization,
            agent.uri,
            agent.config,
            prior_config_revision,
            target_config_content_digest,
            tuple(snapshots),
            keys,
            self._clock(),
        )
        _require_bindings(bindings, plan)
        bindings.preflight(plan)
        return plan
    def execute(
        self, plan: AgentMigrationPlan, *, bindings: AgentMigrationBindings
    ) -> MigrationRecord:
        agent, receipt = self._authorize(plan.authorization, "execute")
        if agent.uri != plan.agent_uri:
            raise AgentHomeMigrationError("identity-drift", "execute", actor=agent.actor)
        self.support.require_pass(plan.support, actor=agent.actor)
        _require_bindings(bindings, plan)
        root = Path(receipt.path)
        migrations_root = root / "state" / "migrations"
        _ensure_private_directory(migrations_root, agent.actor, "migration-root")
        transaction = migrations_root / plan.migration_id
        record_path = transaction / "record.json"
        plan_path = transaction / "plan.json"
        existing = _read_record(record_path, agent.actor)
        if (
            existing is None
            or existing.phase in {MigrationPhase.PLANNED, MigrationPhase.STAGED}
        ) and _config_revision(agent) != plan.prior_config_revision:
            raise AgentHomeMigrationError(
                "config-revision-drift", "execute", actor=agent.actor
            )
        if existing is not None:
            if existing.plan_digest != plan.digest:
                raise AgentHomeMigrationError("plan-mismatch", "resume", actor=agent.actor)
            if existing.phase is MigrationPhase.COMPLETE:
                return existing
            if existing.phase in {
                MigrationPhase.ROLLING_BACK,
                MigrationPhase.FAILED,
                MigrationPhase.ROLLBACK_FAILED,
            }:
                raise AgentHomeMigrationError("failed-transaction", "resume", actor=agent.actor)
        _ensure_private_directory(transaction, agent.actor, "transaction")
        persisted_plan = _read_plan(plan_path, agent.actor)
        if persisted_plan is None:
            _write_json(plan_path, plan.to_json())
        elif persisted_plan != plan:
            raise AgentHomeMigrationError("plan-mismatch", "resume", actor=agent.actor)
        started = self._clock()
        record = existing or MigrationRecord(
            plan.migration_id,
            plan.digest,
            agent.actor,
            MigrationPhase.PLANNED,
            started,
            started,
            plan.authorization.expires_at_ms,
            plan.authorization.responsible_owner,
        )
        _write_record(record_path, record)
        activated = record.phase in {MigrationPhase.BOUND}
        operation_phase = record.phase.value
        try:
            stage_root = transaction / "staging"
            if record.phase is MigrationPhase.PLANNED:
                operation_phase = "stage"
                if stage_root.exists():
                    shutil.rmtree(stage_root)
                _ensure_private_directory(stage_root, agent.actor, "stage-root")
                for index, snapshot in enumerate(plan.entries):
                    source = Path(snapshot.entry.source)
                    if _snapshot_tree(
                        source, agent.actor, private=snapshot.entry.sensitive
                    ) != snapshot.items:
                        raise AgentHomeMigrationError(
                            "source-drift", "stage", actor=agent.actor
                        )
                    _copy_tree(
                        source,
                        stage_root / str(index),
                        agent.actor,
                        self._copy_file,
                    )
                record = self._advance(record_path, record, MigrationPhase.STAGED)

            if record.phase is MigrationPhase.STAGED:
                operation_phase = "publish"
                backup_root = transaction / "target-backups"
                _ensure_private_directory(backup_root, agent.actor, "backup-root")
                for index, snapshot in enumerate(plan.entries):
                    target = root / snapshot.entry.destination
                    staged = stage_root / str(index)
                    backup = backup_root / str(index)
                    if staged.exists():
                        _validate_target_slot(target, root, agent.actor)
                        if target.exists():
                            if backup.exists():
                                raise AgentHomeMigrationError(
                                    "backup-occupied", "publish", actor=agent.actor
                                )
                            os.rename(target, backup)
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        _safe_target_parents(target.parent, root, agent.actor)
                        os.rename(staged, target)
                        _fsync_directory(target.parent)
                        _fsync_directory(backup_root)
                    elif not target.exists() or not _matches_shape(
                        target, snapshot, agent.actor
                    ):
                        raise AgentHomeMigrationError(
                            "publish-resume-drift", "publish", actor=agent.actor
                        )
                _fsync_directory(root)
                record = self._advance(record_path, record, MigrationPhase.PUBLISHED)

            if record.phase is MigrationPhase.PUBLISHED:
                operation_phase = "activate-bindings"
                bindings.activate(plan)
                activated = True
                activated_agent = self.registry.require(agent.actor)
                has_config_migration = any(
                    entry.entry.destination == "config" for entry in plan.entries
                )
                config_matches = (
                    _agent_config_content_digest(activated_agent)
                    == plan.target_config_content_digest
                    if has_config_migration
                    else _config_revision(activated_agent)
                    == plan.prior_config_revision
                )
                if not config_matches:
                    raise AgentHomeMigrationError(
                        "binding-config-mismatch",
                        "activate-bindings",
                        actor=agent.actor,
                    )
                record = self._advance(record_path, record, MigrationPhase.BOUND)

            if record.phase is MigrationPhase.BOUND:
                operation_phase = "source-exit"
                in_use = bindings.legacy_sources_in_use(plan)
                if in_use:
                    raise AgentHomeMigrationError(
                        "legacy-source-in-use", "source-exit", actor=agent.actor
                    )
                for index, snapshot in enumerate(plan.entries):
                    source = Path(snapshot.entry.source)
                    retired_path = source.parent / (
                        f".{source.name}.hyprial-retired-{plan.migration_id}-{index}"
                    )
                    if source.exists() and not os.path.lexists(retired_path):
                        os.rename(source, retired_path)
                        _fsync_directory(source.parent)
                    elif source.exists() or not os.path.lexists(retired_path):
                        raise AgentHomeMigrationError(
                            "source-exit-drift", "source-exit", actor=agent.actor
                        )
                record = self._advance(record_path, record, MigrationPhase.COMPLETE)
            return record
        except BaseException as error:
            category = error.category if isinstance(error, AgentHomeMigrationError) else type(error).__name__
            phase = error.phase if isinstance(error, AgentHomeMigrationError) else operation_phase
            compensation_errors: list[str] = []
            for index, snapshot in reversed(tuple(enumerate(plan.entries))):
                source = Path(snapshot.entry.source)
                retired_path = source.parent / (
                    f".{source.name}.hyprial-retired-{plan.migration_id}-{index}"
                )
                try:
                    if retired_path.exists() and not source.exists():
                        os.rename(retired_path, source)
                        _fsync_directory(source.parent)
                    elif retired_path.exists() and source.exists():
                        raise OSError("source and retired source both exist")
                except BaseException as compensation_error:
                    compensation_errors.append(type(compensation_error).__name__)
            if activated or record.phase in {
                MigrationPhase.PUBLISHED,
                MigrationPhase.BOUND,
            }:
                try:
                    bindings.rollback(plan)
                except BaseException as compensation_error:
                    compensation_errors.append(type(compensation_error).__name__)
            backup_root = transaction / "target-backups"
            for index, snapshot in reversed(tuple(enumerate(plan.entries))):
                target = root / snapshot.entry.destination
                staged = stage_root / str(index)
                backup = backup_root / str(index)
                try:
                    if not staged.exists() and target.exists() and _matches_shape(
                        target, snapshot, agent.actor
                    ):
                        shutil.rmtree(target)
                    if backup.exists() and not target.exists():
                        os.rename(backup, target)
                    _fsync_directory(target.parent)
                except BaseException as compensation_error:
                    compensation_errors.append(type(compensation_error).__name__)
            try:
                stage_root = transaction / "staging"
                if stage_root.exists():
                    shutil.rmtree(stage_root)
            except BaseException as compensation_error:
                compensation_errors.append(type(compensation_error).__name__)
            failed = MigrationRecord(
                record.migration_id,
                record.plan_digest,
                record.actor,
                MigrationPhase.ROLLBACK_FAILED if compensation_errors else MigrationPhase.FAILED,
                record.started_at_ms,
                self._clock(),
                record.deadline_ms,
                record.next_responsible,
                phase,
                category if not compensation_errors else f"{category}+compensation-error",
            )
            _write_record(record_path, failed)
            if isinstance(error, AgentHomeMigrationError):
                raise
            raise AgentHomeMigrationError(category, phase, actor=agent.actor) from error
    def rollback(
        self,
        plan: AgentMigrationPlan,
        *,
        authorization: AgentMigrationAuthorization,
        bindings: AgentMigrationBindings,
    ) -> MigrationRecord:
        agent, receipt = self._authorize(authorization, "rollback")
        if (
            authorization.actor != plan.authorization.actor
            or authorization.entity_token != plan.authorization.entity_token
            or authorization.home_resource_token
            != plan.authorization.home_resource_token
        ):
            raise AgentHomeMigrationError(
                "rollback-authorization-mismatch", "rollback", actor=agent.actor
            )
        _require_bindings(bindings, plan)
        root = Path(receipt.path)
        transaction = root / "state" / "migrations" / plan.migration_id
        record_path = transaction / "record.json"
        record = _read_record(record_path, agent.actor)
        if record is None or record.plan_digest != plan.digest:
            raise AgentHomeMigrationError("unknown-transaction", "rollback", actor=agent.actor)
        if record.phase is MigrationPhase.ROLLED_BACK:
            return record
        if record.phase not in {
            MigrationPhase.COMPLETE,
            MigrationPhase.ROLLING_BACK,
        }:
            raise AgentHomeMigrationError("not-complete", "rollback", actor=agent.actor)
        quarantine = transaction / "rolled-back-targets"
        _ensure_private_directory(quarantine, agent.actor, "rollback-quarantine")
        try:
            if record.phase is MigrationPhase.COMPLETE:
                for index, snapshot in enumerate(plan.entries):
                    source = Path(snapshot.entry.source)
                    retired_path = source.parent / (
                        f".{source.name}.hyprial-retired-{plan.migration_id}-{index}"
                    )
                    target = root / snapshot.entry.destination
                    if (
                        source.exists()
                        or not retired_path.exists()
                        or not target.exists()
                        or (quarantine / str(index)).exists()
                    ):
                        raise AgentHomeMigrationError(
                            "rollback-drift", "rollback", actor=agent.actor
                        )
                    if _tree_shape(
                        _snapshot_tree(target, agent.actor, private=True)
                    ) != _tree_shape(snapshot.items):
                        raise AgentHomeMigrationError(
                            "rollback-drift", "rollback", actor=agent.actor
                        )
                record = self._advance(
                    record_path, record, MigrationPhase.ROLLING_BACK
                )
            bindings.rollback(plan)
            for index, snapshot in reversed(tuple(enumerate(plan.entries))):
                source = Path(snapshot.entry.source)
                retired_path = source.parent / (
                    f".{source.name}.hyprial-retired-{plan.migration_id}-{index}"
                )
                target = root / snapshot.entry.destination
                rolled_back_target = quarantine / str(index)
                if not rolled_back_target.exists():
                    if source.exists() or not retired_path.exists() or not target.exists():
                        raise AgentHomeMigrationError(
                            "rollback-drift", "rollback", actor=agent.actor
                        )
                    if _tree_shape(
                        _snapshot_tree(target, agent.actor, private=True)
                    ) != _tree_shape(snapshot.items):
                        raise AgentHomeMigrationError(
                            "rollback-drift", "rollback", actor=agent.actor
                        )
                    os.rename(target, rolled_back_target)
                    _fsync_directory(quarantine)
                elif _tree_shape(
                    _snapshot_tree(rolled_back_target, agent.actor, private=True)
                ) != _tree_shape(snapshot.items):
                    raise AgentHomeMigrationError(
                        "rollback-drift", "rollback", actor=agent.actor
                    )
                if not source.exists():
                    if not retired_path.exists():
                        raise AgentHomeMigrationError(
                            "rollback-drift", "rollback", actor=agent.actor
                        )
                    os.rename(retired_path, source)
                elif retired_path.exists():
                    raise AgentHomeMigrationError(
                        "rollback-drift", "rollback", actor=agent.actor
                    )
                _fsync_directory(source.parent)
                backup = transaction / "target-backups" / str(index)
                if backup.exists():
                    if target.exists():
                        raise AgentHomeMigrationError(
                            "rollback-drift", "rollback", actor=agent.actor
                        )
                    os.rename(backup, target)
                _fsync_directory(target.parent)
            if _config_revision(self.registry.require(agent.actor)) != (
                plan.prior_config_revision
            ):
                raise AgentHomeMigrationError(
                    "rollback-config-mismatch", "rollback", actor=agent.actor
                )
            return self._advance(record_path, record, MigrationPhase.ROLLED_BACK)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:
            category = (
                error.category
                if isinstance(error, AgentHomeMigrationError)
                else type(error).__name__
            )
            phase = (
                error.phase
                if isinstance(error, AgentHomeMigrationError)
                else "rollback"
            )
            failed = MigrationRecord(
                record.migration_id,
                record.plan_digest,
                record.actor,
                MigrationPhase.ROLLBACK_FAILED,
                record.started_at_ms,
                self._clock(),
                record.deadline_ms,
                record.next_responsible,
                phase,
                category,
            )
            _write_record(record_path, failed)
            if isinstance(error, AgentHomeMigrationError):
                raise
            raise AgentHomeMigrationError(category, phase, actor=agent.actor) from error
    def _authorize(self, authorization: AgentMigrationAuthorization, phase: str):
        agent = self.registry.require(authorization.actor)
        if self._clock() > authorization.expires_at_ms:
            raise AgentHomeMigrationError("authorization-expired", phase, actor=agent.actor)
        if agent.entity_token != authorization.entity_token:
            raise AgentHomeMigrationError("incarnation-mismatch", phase, actor=agent.actor)
        receipt = self.registry.home_receipt(agent.actor)
        if receipt.resource_token != authorization.home_resource_token:
            raise AgentHomeMigrationError("home-receipt-mismatch", phase, actor=agent.actor)
        try:
            stopped = self._liveness_probe(agent.uri, agent.entity_token)
        except Exception as error:
            raise AgentHomeMigrationError(
                "liveness-unknown", phase, actor=agent.actor
            ) from error
        if stopped is not True:
            category = "liveness-unknown" if stopped is None else "agent-running"
            raise AgentHomeMigrationError(category, phase, actor=agent.actor)
        return agent, receipt
    def _advance(
        self, path: Path, record: MigrationRecord, phase: MigrationPhase
    ) -> MigrationRecord:
        updated = MigrationRecord(
            record.migration_id,
            record.plan_digest,
            record.actor,
            phase,
            record.started_at_ms,
            self._clock(),
            record.deadline_ms,
            record.next_responsible,
        )
        _write_record(path, updated)
        return updated
