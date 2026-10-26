"""Durable, incarnation-fenced agent configuration rebind saga."""

from __future__ import annotations

import errno
import json
import os
import stat
import time
import uuid
from pathlib import Path
from typing import Protocol

from hyprial.identity.impl.agents.home.config import AgentConfig
from hyprial.identity.impl.agents.home.provisioner import HomeReceipt
from hyprial.identity.impl.agents.registry._base import Agent
from hyprial.identity.impl.agents.registry.config_binding import agent_config_digest
from hyprial.kernel import ipc_errors

from .projection_diff import projection_changes
from .tree import (
    ConfigRebindError,
    copy_tree_exact,
    remove_tree,
    same_tree,
    tree_snapshot,
    validate_private_tree,
)


_TERMINAL_PHASES = {"complete", "rolled-back"}


class ConfigRebindOwner(Protocol):
    def require(self, actor: str) -> Agent: ...

    def home_receipt(
        self, actor: str, *, require_ready: bool = True, validate_mirror: bool = True
    ) -> HomeReceipt: ...

    def bind_config_quiescent(
        self, actor: str, *, config: AgentConfig, expected_current_config_digest: str
    ) -> tuple[Agent, dict[str, object]]: ...

    def quiescence(self, actor: str) -> dict[str, object]: ...


def run_config_rebind(
    owner: ConfigRebindOwner,
    actor: str,
    *,
    source: str | None,
    rollback_receipt_id: str | None,
    dry_run: bool,
) -> dict[str, object]:
    agent = owner.require(actor)
    home_receipt = owner.home_receipt(agent.actor)
    home = Path(home_receipt.path)
    receipt_root = home / "state" / "config-rebind"
    if rollback_receipt_id is not None:
        if source is not None:
            raise ConfigRebindError(
                ipc_errors.INVALID_ARGUMENT,
                "--source and --rollback are mutually exclusive",
            )
        if dry_run:
            raise ConfigRebindError(
                ipc_errors.INVALID_ARGUMENT,
                "--dry-run cannot be combined with --rollback",
            )
        return _rollback(
            owner,
            agent,
            home_receipt,
            receipt_root,
            rollback_receipt_id,
            dry_run=dry_run,
        )
    if source is None:
        raise ConfigRebindError(
            ipc_errors.INVALID_ARGUMENT,
            "configuration rebind requires --source or --rollback",
        )
    return _forward(
        owner, agent, home_receipt, receipt_root, Path(source), dry_run=dry_run
    )


def _forward(
    owner: ConfigRebindOwner,
    agent: Agent,
    home_receipt: HomeReceipt,
    receipt_root: Path,
    source: Path,
    *,
    dry_run: bool,
) -> dict[str, object]:
    source = source.expanduser()
    if not source.is_absolute():
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_INVALID_SOURCE,
            "staging source must be an absolute path",
        )
    _validate_receipt_root(receipt_root, create=not dry_run)
    if not dry_run:
        _cleanup_uncommitted(receipt_root)
    pending = _pending_receipt(receipt_root, agent.actor)
    if pending is not None:
        if pending.get("phase") in {"rolling-back", "binding-restored"}:
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_IN_PROGRESS,
                "configuration rollback is incomplete; retry rollback with its receipt",
                data={
                    "receiptId": str(pending["receiptId"]),
                    "phase": str(pending["phase"]),
                },
            )
        if pending.get("source") != str(source):
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_IN_PROGRESS,
                "a different configuration rebind is incomplete; resume its source or roll back its receipt",
                data={
                    "receiptId": str(pending["receiptId"]),
                    "phase": str(pending["phase"]),
                },
            )
        if dry_run:
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_IN_PROGRESS,
                "--dry-run cannot resume an incomplete rebind",
            )
        return _resume_forward(owner, agent, home_receipt, receipt_root, pending)

    completed = (
        None
        if dry_run
        else _completed_receipt(receipt_root, agent, home_receipt, source)
    )
    if completed is not None:
        return _result(completed, dry_run=False)

    proof = _require_quiescent(owner, agent.actor)
    validated = validate_private_tree(source)
    old = agent.config
    if old is None:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_NO_BINDING,
            "agent has no existing configuration binding to replace",
        )
    old_source = Path(old.source).expanduser().absolute()
    if not old_source.is_dir():
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_SOURCE_DRIFT,
            f"current configuration source is missing: {old_source}",
        )
    before_tree = tree_snapshot(old_source)
    after_tree = validated["tree"]
    assert isinstance(after_tree, dict)
    receipt_id = uuid.uuid4().hex
    target = receipt_root.parent.parent / "config-revisions" / receipt_id
    after_config = AgentConfig(
        source=str(target),
        required=old.required,
        discovery=old.discovery,
        shared_credentials=old.shared_credentials,
    )
    receipt: dict[str, object] = {
        "schemaVersion": 1,
        "receiptId": receipt_id,
        "actor": agent.actor,
        "entityToken": agent.entity_token,
        "homeResourceToken": home_receipt.resource_token,
        "phase": "validated" if dry_run else "prepared",
        "source": str(source),
        "target": str(target),
        "beforeSource": str(old_source),
        "beforeConfig": old.to_json(),
        "afterConfig": after_config.to_json(),
        "beforeConfigDigest": agent_config_digest(old),
        "afterConfigDigest": agent_config_digest(after_config),
        "beforeTree": before_tree,
        "afterTree": after_tree,
        "projectionChanges": projection_changes(agent, Path(home_receipt.path), source),
        "quiescence": proof,
        "createdAtMs": time.time_ns() // 1_000_000,
    }
    if dry_run:
        return _result(receipt, dry_run=True, validation=validated)

    transaction = receipt_root / receipt_id
    transaction.mkdir(mode=0o700, parents=False)
    copy_tree_exact(old_source, transaction / "before")
    copy_tree_exact(source, transaction / "new")
    if not same_tree(transaction / "before", before_tree) or not same_tree(
        transaction / "new", after_tree
    ):
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_SOURCE_DRIFT,
            "configuration tree changed while the durable receipt was prepared",
        )
    _write_receipt(transaction / "receipt.json", receipt)
    _crash_point("receipt-prepared")
    return _resume_forward(owner, agent, home_receipt, receipt_root, receipt)


def _resume_forward(
    owner: ConfigRebindOwner,
    agent: Agent,
    home_receipt: HomeReceipt,
    receipt_root: Path,
    receipt: dict[str, object],
) -> dict[str, object]:
    _validate_fence(agent, home_receipt, receipt)
    proof = _require_quiescent(owner, agent.actor)
    receipt_id = _required_receipt_text(receipt, "receiptId")
    transaction = receipt_root / receipt_id
    receipt_path = transaction / "receipt.json"
    target = Path(_required_receipt_text(receipt, "target"))
    phase = _required_receipt_text(receipt, "phase")
    owns_revision_parent = _claim_revision_parent(
        target.parent, receipt, receipt_path
    )
    _validate_revision_parent(target.parent, create=owns_revision_parent)
    after_tree = receipt.get("afterTree")
    before_tree = receipt.get("beforeTree")
    before_source = Path(_required_receipt_text(receipt, "beforeSource"))
    if not same_tree(transaction / "before", before_tree):
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            "durable before-image does not match its receipt",
        )
    if not same_tree(before_source, before_tree):
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_SOURCE_DRIFT,
            "original configuration changed after rebind began",
        )
    if not target.exists():
        stage = transaction / "new"
        if not same_tree(stage, after_tree):
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
                "durable staged tree does not match its receipt",
            )
        os.replace(stage, target)
        _fsync_directory(transaction)
        _fsync_directory(target.parent)
        _crash_point("tree-published")
    if not same_tree(target, after_tree):
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_TARGET_DRIFT,
            "published configuration does not match its receipt",
        )
    proof = _require_quiescent(owner, agent.actor)
    if phase == "prepared":
        receipt["phase"] = "published"
        receipt["quiescence"] = proof
        _write_receipt(receipt_path, receipt)
        _crash_point("publish-recorded")

    proof = _require_quiescent(owner, agent.actor)
    receipt["quiescence"] = proof
    current = owner.require(agent.actor)
    before_digest = _required_receipt_text(receipt, "beforeConfigDigest")
    after_digest = _required_receipt_text(receipt, "afterConfigDigest")
    current_digest = agent_config_digest(current.config)
    if current_digest == before_digest:
        after_config = AgentConfig.from_json(
            receipt.get("afterConfig"), "config-rebind.afterConfig"
        )
        _bound, proof = owner.bind_config_quiescent(
            agent.actor,
            config=after_config,
            expected_current_config_digest=before_digest,
        )
        receipt["quiescence"] = proof
        _crash_point("binding-updated")
    elif current_digest != after_digest:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_CAS_MISMATCH,
            "agent config changed while the rebind receipt was incomplete",
        )
    receipt["phase"] = "complete"
    receipt["completedAtMs"] = time.time_ns() // 1_000_000
    _write_receipt(receipt_path, receipt)
    _crash_point("complete-recorded")
    return _result(receipt, dry_run=False)


def _rollback(
    owner: ConfigRebindOwner,
    agent: Agent,
    home_receipt: HomeReceipt,
    receipt_root: Path,
    receipt_id: str,
    *,
    dry_run: bool,
) -> dict[str, object]:
    receipt_id = receipt_id.strip()
    if len(receipt_id) != 32 or any(
        character not in "0123456789abcdef" for character in receipt_id
    ):
        raise ConfigRebindError(
            ipc_errors.INVALID_ARGUMENT, "invalid rollback receipt id"
        )
    _validate_receipt_root(receipt_root, create=False)
    receipt_path = receipt_root / receipt_id / "receipt.json"
    receipt = _read_receipt(receipt_path)
    _validate_fence(agent, home_receipt, receipt)
    if receipt.get("receiptId") != receipt_id:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            "rollback receipt id does not match its durable directory",
        )
    proof = _require_quiescent(owner, agent.actor)
    phase = _required_receipt_text(receipt, "phase")
    transaction = receipt_root / receipt_id
    target = Path(_required_receipt_text(receipt, "target"))
    _validate_revision_parent(target.parent, create=False)
    staged_target = transaction / "new"
    retired_target = transaction / "retired-target"
    after_tree = receipt.get("afterTree")
    created_revision_parent = receipt.get("revisionParentCreated") is True
    if phase == "rolled-back":
        _cleanup_rolled_back_target(
            target,
            staged_target,
            retired_target,
            after_tree,
            created_revision_parent=created_revision_parent,
        )
        return _result(receipt, dry_run=dry_run)
    rollback_phases = {"rolling-back", "binding-restored"}
    forward_phases = {"prepared", "published", "complete"}
    if phase not in forward_phases | rollback_phases:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            f"receipt cannot be rolled back from phase {phase!r}",
        )
    rollback_from = receipt.get("rollbackFromPhase", phase)
    if rollback_from not in forward_phases:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            "rollback receipt does not identify its forward phase",
        )
    if phase in rollback_phases and "rollbackFromPhase" not in receipt:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            "rollback receipt is missing its forward phase",
        )
    before_tree = receipt.get("beforeTree")
    before_source = Path(_required_receipt_text(receipt, "beforeSource"))
    if not same_tree(transaction / "before", before_tree) or not same_tree(
        before_source, before_tree
    ):
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_SOURCE_DRIFT,
            "original configuration no longer matches the durable before-image",
        )
    after_images = tuple(
        path for path in (staged_target, target, retired_target) if path.exists()
    )
    cleanup_pending = phase == "binding-restored"
    if cleanup_pending:
        if staged_target in after_images or target in after_images:
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_TARGET_DRIFT,
                "binding-restored rollback still has an unretired after-image",
            )
    else:
        if len(after_images) != 1:
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_TARGET_DRIFT,
                "rollback requires exactly one staged, published, or retired after-image",
            )
        after_image = after_images[0]
        if not same_tree(after_image, after_tree):
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_TARGET_DRIFT,
                "configuration after-image no longer matches its receipt",
            )
        if rollback_from in {"published", "complete"} and after_image == staged_target:
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_TARGET_DRIFT,
                "published configuration unexpectedly remains in staging",
            )
    current = owner.require(agent.actor)
    before_digest = _required_receipt_text(receipt, "beforeConfigDigest")
    after_digest = _required_receipt_text(receipt, "afterConfigDigest")
    current_digest = agent_config_digest(current.config)
    expected_digests = (
        {before_digest} if cleanup_pending else {before_digest, after_digest}
    )
    if current_digest not in expected_digests:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_CAS_MISMATCH,
            "agent config changed after the recorded rebind",
        )
    receipt["quiescence"] = proof
    if dry_run:
        candidate = dict(receipt)
        candidate["phase"] = "validated-rollback"
        return _result(candidate, dry_run=True)
    if not cleanup_pending:
        if phase not in rollback_phases:
            receipt["rollbackFromPhase"] = phase
            receipt["phase"] = "rolling-back"
            _write_receipt(receipt_path, receipt)
            _crash_point("rollback-recorded")
        proof = _require_quiescent(owner, agent.actor)
        receipt["quiescence"] = proof
        if current_digest == after_digest:
            before_config = AgentConfig.from_json(
                receipt.get("beforeConfig"), "config-rebind.beforeConfig"
            )
            _restored, proof = owner.bind_config_quiescent(
                agent.actor,
                config=before_config,
                expected_current_config_digest=after_digest,
            )
            receipt["quiescence"] = proof
            _crash_point("binding-restored")
        proof = _require_quiescent(owner, agent.actor)
        receipt["quiescence"] = proof
        _retire_rollback_after_image(
            target=target,
            staged_target=staged_target,
            retired_target=retired_target,
            transaction=transaction,
        )
        proof = _require_quiescent(owner, agent.actor)
        receipt["quiescence"] = proof
        receipt["phase"] = "binding-restored"
        _write_receipt(receipt_path, receipt)
    _cleanup_rolled_back_target(
        target,
        staged_target,
        retired_target,
        after_tree,
        created_revision_parent=created_revision_parent,
    )
    receipt["phase"] = "rolled-back"
    receipt["rolledBackAtMs"] = time.time_ns() // 1_000_000
    _write_receipt(receipt_path, receipt)
    _crash_point("rollback-complete")
    return _result(receipt, dry_run=False)


def _validate_receipt_root(root: Path, *, create: bool) -> None:
    try:
        info = root.lstat()
    except FileNotFoundError:
        if not create:
            return
        root.mkdir(mode=0o700, parents=False)
        os.chmod(root, 0o700)
        info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            "configuration rebind receipt root must be a real directory",
        )
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            "configuration rebind receipt root is not owned by the daemon user",
        )
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            "configuration rebind receipt root must have mode 700",
        )


def _revision_parent_present(parent: Path) -> bool:
    try:
        parent.lstat()
    except FileNotFoundError:
        return False
    return True


def _claim_revision_parent(
    parent: Path,
    receipt: dict[str, object],
    receipt_path: Path,
) -> bool:
    """Decide, and durably record, who owns the revision parent.

    The intent is written **before** any creation: an interruption between the
    record and the ``mkdir`` therefore cannot lose the attribution, and a
    resumed run finishes the same job with the same owner (the review's
    interruption sits in exactly that window). Returns True when this
    transaction owns (or must create) the parent, False when it pre-existed
    and must be preserved.
    """

    recorded = receipt.get("revisionParentCreated")
    if recorded is True:
        return True
    if recorded is False:
        if not _revision_parent_present(parent):
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_TARGET_DRIFT,
                "configuration revision parent disappeared after it was "
                "recorded as pre-existing",
            )
        return False
    if recorded is not None:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            "revisionParentCreated must be a boolean",
        )
    owns = not _revision_parent_present(parent)
    receipt["revisionParentCreated"] = owns
    _write_receipt(receipt_path, receipt)
    _crash_point("revision-parent-claimed")
    return owns


def _validate_revision_parent(parent: Path, *, create: bool) -> bool:
    """Validate the revision parent and report whether this call created it."""

    created = False
    try:
        info = parent.lstat()
    except FileNotFoundError:
        if not create:
            return
        parent.mkdir(mode=0o700, parents=False)
        os.chmod(parent, 0o700)
        info = parent.lstat()
        created = True
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_TARGET_DRIFT,
            "configuration revision parent must be a real directory",
        )
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_TARGET_DRIFT,
            "configuration revision parent is not owned by the daemon user",
        )
    if stat.S_IMODE(info.st_mode) != 0o700:
        if created:
            os.chmod(parent, 0o700)
        else:
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_TARGET_DRIFT,
                "configuration revision parent must have mode 700",
            )
    return created


def _retire_rollback_after_image(
    *,
    target: Path,
    staged_target: Path,
    retired_target: Path,
    transaction: Path,
) -> None:
    if retired_target.exists():
        return
    source = target if target.exists() else staged_target
    if not source.exists():
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_TARGET_DRIFT,
            "configuration after-image is missing before rollback retirement",
        )
    os.replace(source, retired_target)
    _fsync_directory(source.parent)
    if source.parent != transaction:
        _fsync_directory(transaction)
    _crash_point("target-retired")


def _cleanup_rolled_back_target(
    target: Path,
    staged_target: Path,
    retired_target: Path,
    after_tree: object,
    *,
    created_revision_parent: bool,
) -> None:
    if target.exists():
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_TARGET_DRIFT,
            "rolled-back configuration target unexpectedly exists",
        )
    if staged_target.exists():
        if retired_target.exists() or not same_tree(staged_target, after_tree):
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_TARGET_DRIFT,
                "rolled-back staged after-image does not match its receipt",
            )
        os.replace(staged_target, retired_target)
        _fsync_directory(retired_target.parent)
    if retired_target.exists():
        remove_tree(retired_target)
        _fsync_directory(retired_target.parent)
    if not created_revision_parent:
        # A pre-existing revision parent belongs to the operator (or to an
        # earlier transaction): preserve it and its mode exactly.
        return
    revisions = target.parent
    try:
        revisions.rmdir()
    except FileNotFoundError:
        return
    except OSError as error:
        if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
            # Another revision still lives here; an empty-parent cleanup is
            # simply not applicable, so the directory stays as it is.
            return
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_CLEANUP_FAILED,
            f"cannot remove the configuration revision parent {revisions}: {error}",
        ) from error
    _fsync_directory(revisions.parent)


def _require_quiescent(owner: ConfigRebindOwner, actor: str) -> dict[str, object]:
    proof = owner.quiescence(actor)
    if proof.get("quiescent") is not True:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_NOT_QUIESCENT,
            "agent configuration can be rebound only when durable desired state, supervisor, and active runs are quiescent",
            data=proof,
        )
    return proof


def _cleanup_uncommitted(root: Path) -> None:
    """Remove preparation trees that never reached the first receipt commit."""

    if not root.is_dir():
        return
    changed = False
    for path in root.iterdir():
        if (
            path.is_dir()
            and len(path.name) == 32
            and all(character in "0123456789abcdef" for character in path.name)
            and not (path / "receipt.json").exists()
        ):
            remove_tree(path)
            changed = True
    if changed:
        _fsync_directory(root)


def _pending_receipt(root: Path, actor: str) -> dict[str, object] | None:
    if not root.is_dir():
        return None
    pending: list[dict[str, object]] = []
    for path in sorted(root.glob("*/receipt.json")):
        receipt = _read_receipt(path)
        if (
            receipt.get("actor") == actor
            and receipt.get("phase") not in _TERMINAL_PHASES
        ):
            pending.append(receipt)
    if len(pending) > 1:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            "multiple incomplete rebind receipts exist for one agent",
        )
    return pending[0] if pending else None


def _completed_receipt(
    root: Path,
    agent: Agent,
    home: HomeReceipt,
    source: Path,
) -> dict[str, object] | None:
    if not root.is_dir():
        return None
    for path in sorted(root.glob("*/receipt.json"), reverse=True):
        receipt = _read_receipt(path)
        if (
            receipt.get("actor") != agent.actor
            or receipt.get("phase") != "complete"
            or receipt.get("source") != str(source)
        ):
            continue
        _validate_fence(agent, home, receipt)
        if agent_config_digest(agent.config) != receipt.get("afterConfigDigest"):
            continue
        if source.exists() and not same_tree(source, receipt.get("afterTree")):
            continue
        target = Path(_required_receipt_text(receipt, "target"))
        if not same_tree(target, receipt.get("afterTree")):
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_TARGET_DRIFT,
                "completed rebind target no longer matches its receipt",
            )
        return receipt
    return None


def _validate_fence(
    agent: Agent, home: HomeReceipt, receipt: dict[str, object]
) -> None:
    if (
        receipt.get("actor") != agent.actor
        or receipt.get("entityToken") != agent.entity_token
    ):
        raise ConfigRebindError(
            ipc_errors.AGENT_INCARNATION_MISMATCH,
            "rebind receipt belongs to a different agent incarnation",
        )
    if receipt.get("homeResourceToken") != home.resource_token:
        raise ConfigRebindError(
            ipc_errors.AGENT_INCARNATION_MISMATCH,
            "rebind receipt belongs to a different home resource",
        )
    try:
        receipt_id = _required_receipt_text(receipt, "receiptId")
        if len(receipt_id) != 32 or any(
            character not in "0123456789abcdef" for character in receipt_id
        ):
            raise ValueError("receiptId must be lowercase UUID hex")
        source = Path(_required_receipt_text(receipt, "source"))
        before_source = Path(_required_receipt_text(receipt, "beforeSource"))
        target = Path(_required_receipt_text(receipt, "target"))
        if not source.is_absolute() or not before_source.is_absolute():
            raise ValueError("receipt source paths must be absolute")
        expected_target = Path(home.path) / "config-revisions" / receipt_id
        if target != expected_target:
            raise ValueError("published target is outside its fenced agent home")
        before_config = AgentConfig.from_json(
            receipt.get("beforeConfig"), "config-rebind.beforeConfig"
        )
        after_config = AgentConfig.from_json(
            receipt.get("afterConfig"), "config-rebind.afterConfig"
        )
        if Path(before_config.source).expanduser().absolute() != before_source:
            raise ValueError("before config does not match beforeSource")
        if Path(after_config.source) != target:
            raise ValueError("after config does not match target")
        if agent_config_digest(before_config) != receipt.get("beforeConfigDigest"):
            raise ValueError("before config digest does not match its endpoint")
        if agent_config_digest(after_config) != receipt.get("afterConfigDigest"):
            raise ValueError("after config digest does not match its endpoint")
    except ConfigRebindError:
        raise
    except (TypeError, ValueError) as error:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            f"invalid rebind receipt: {error}",
        ) from error


def _result(
    receipt: dict[str, object],
    *,
    dry_run: bool,
    validation: dict[str, object] | None = None,
) -> dict[str, object]:
    before_tree = receipt.get("beforeTree")
    after_tree = receipt.get("afterTree")
    public_receipt = dict(receipt)
    if dry_run:
        public_receipt["receiptId"] = None
    result: dict[str, object] = {
        "ok": True,
        "name": str(receipt["actor"]),
        "dryRun": dry_run,
        "state": str(receipt["phase"]),
        "receiptId": None if dry_run else str(receipt["receiptId"]),
        "beforeHash": before_tree.get("digest")
        if isinstance(before_tree, dict)
        else None,
        "afterHash": after_tree.get("digest") if isinstance(after_tree, dict) else None,
        "beforeConfigDigest": receipt.get("beforeConfigDigest"),
        "afterConfigDigest": receipt.get("afterConfigDigest"),
        "source": receipt.get("source"),
        "target": receipt.get("target"),
        "projectionChanges": receipt.get("projectionChanges", []),
        "receipt": public_receipt,
    }
    if validation is not None:
        result["validation"] = validation
    return result


def _read_receipt(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            f"cannot read rebind receipt {path}: {error}",
        ) from error
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            f"unsupported rebind receipt: {path}",
        )
    return value


def _write_receipt(path: Path, receipt: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(receipt, sort_keys=True, indent=2, allow_nan=False) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    _fsync_directory(path.parent)


def _required_receipt_text(receipt: dict[str, object], key: str) -> str:
    value = receipt.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_RECEIPT_INVALID,
            f"receipt field {key!r} is missing",
        )
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _crash_point(_label: str) -> None:
    """Test seam for proving restart behavior between durable phases."""
