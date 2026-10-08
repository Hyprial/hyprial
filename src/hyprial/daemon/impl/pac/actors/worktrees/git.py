"""Bounded, option-safe git effects for PAC-managed worker worktrees.

This module is deliberately independent of the PAC store and actor writer.  A
caller supplies an immutable plan and proves worker absence before cleanup;
the helpers only perform local git operations and return a small projection.
"""

from __future__ import annotations

import math
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from hyprial.identity import (
    PAC_WORKTREE_CLEANUP_FAILED,
    PAC_WORKTREE_IDENTITY_MISMATCH,
    PAC_WORKTREE_PREPARE_FAILED,
    PacError,
)


_PLAN_FIELDS = (
    "graphId",
    "nodeId",
    "actorNode",
    "repo",
    "baseRef",
    "baseOid",
    "branch",
    "path",
    "effectiveCwd",
    "operationId",
)
_OID_RE = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")
_UNSAFE_REF = re.compile(r"[\x00-\x20~^:?*\\\[]")


def _bounded(value: object, limit: int = 256) -> str:
    text = str(value)
    return text[:limit]


def _error(
    code: str,
    message: str,
    *,
    stage: str,
    plan: Mapping[str, object] | None = None,
) -> PacError:
    data: dict[str, str] = {"stage": stage}
    if plan is not None:
        for key in ("graphId", "nodeId", "path"):
            value = plan.get(key)
            if isinstance(value, str):
                data[key] = _bounded(value)
    return PacError(code, message, data)


def _deadline(
    timeout_s: float,
    *,
    stage: str,
    code: str,
    plan: Mapping[str, object] | None = None,
) -> float:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise _error(
            code,
            "git timeout must be a positive finite number",
            stage=stage,
            plan=plan,
        )
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise _error(
            code,
            "git timeout must be a positive finite number",
            stage=stage,
            plan=plan,
        )
    return time.monotonic() + float(timeout_s)


def _remaining(deadline: float, *, code: str, stage: str, plan: Mapping[str, object] | None) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _error(code, "git operation timed out", stage=stage, plan=plan)
    return remaining


def _run(
    repo: Path,
    arguments: Sequence[str],
    *,
    deadline: float,
    code: str,
    stage: str,
    plan: Mapping[str, object] | None = None,
) -> subprocess.CompletedProcess[str]:
    timeout = _remaining(deadline, code=code, stage=stage, plan=plan)
    argv = ["git", "--no-optional-locks", "-C", str(repo), *arguments]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            shell=False,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise _error(code, "git operation timed out", stage=stage, plan=plan) from error
    except OSError as error:
        raise _error(code, "git executable or repository unavailable", stage=stage, plan=plan) from error
    return completed


def _check(
    repo: Path,
    arguments: Sequence[str],
    *,
    deadline: float,
    code: str,
    stage: str,
    plan: Mapping[str, object] | None = None,
) -> str:
    completed = _run(
        repo,
        arguments,
        deadline=deadline,
        code=code,
        stage=stage,
        plan=plan,
    )
    if completed.returncode != 0:
        raise _error(code, "git command failed", stage=stage, plan=plan)
    return completed.stdout


def _reject_symlink_components(path: Path, *, stage: str, plan: Mapping[str, object] | None) -> Path:
    absolute = Path.cwd() / path if not path.is_absolute() else path
    absolute = Path(*absolute.parts)
    current = absolute
    while current != current.parent:
        try:
            if current.is_symlink():
                raise _error(
                    PAC_WORKTREE_IDENTITY_MISMATCH,
                    "worktree path contains a symlink",
                    stage=stage,
                    plan=plan,
                )
        except OSError as error:
            raise _error(
                PAC_WORKTREE_IDENTITY_MISMATCH,
                "worktree path cannot be inspected",
                stage=stage,
                plan=plan,
            ) from error
        current = current.parent
    return absolute.resolve(strict=False)


def _required_plan(plan: Mapping[str, object], *, stage: str) -> dict[str, str]:
    if not isinstance(plan, Mapping):
        raise _error(
            PAC_WORKTREE_IDENTITY_MISMATCH,
            "worktree plan is not an object",
            stage=stage,
        )
    values: dict[str, str] = {}
    for key in _PLAN_FIELDS:
        value = plan.get(key)
        if not isinstance(value, str) or not value or "\x00" in value:
            raise _error(
                PAC_WORKTREE_IDENTITY_MISMATCH,
                "worktree plan is incomplete",
                stage=stage,
                plan=plan,
            )
        values[key] = value
    expected_operation = f"pac-worktree:{values['graphId']}:{values['nodeId']}"
    expected_branch = f"pac/{values['graphId']}/{values['nodeId']}"
    if values["operationId"] != expected_operation or values["branch"] != expected_branch:
        raise _error(
            PAC_WORKTREE_IDENTITY_MISMATCH,
            "worktree operation identity does not match its plan",
            stage=stage,
            plan=plan,
        )
    if not _OID_RE.fullmatch(values["baseOid"]):
        raise _error(
            PAC_WORKTREE_IDENTITY_MISMATCH,
            "worktree base is not a commit oid",
            stage=stage,
            plan=plan,
        )
    if _UNSAFE_REF.search(values["branch"]) or values["branch"].startswith("-"):
        raise _error(
            PAC_WORKTREE_IDENTITY_MISMATCH,
            "worktree branch is not safe",
            stage=stage,
            plan=plan,
        )
    return values


def _repo_path(values: Mapping[str, str], *, stage: str, plan: Mapping[str, object]) -> Path:
    repo = Path(values["repo"])
    if not repo.is_absolute() or not repo.is_dir():
        raise _error(
            PAC_WORKTREE_IDENTITY_MISMATCH,
            "worktree repository is not an absolute directory",
            stage=stage,
            plan=plan,
        )
    return _reject_symlink_components(repo, stage=stage, plan=plan)


def _plan_path(values: Mapping[str, str], *, stage: str, plan: Mapping[str, object]) -> tuple[Path, Path]:
    path = Path(values["path"])
    cwd = Path(values["effectiveCwd"])
    if not path.is_absolute() or not cwd.is_absolute():
        raise _error(
            PAC_WORKTREE_IDENTITY_MISMATCH,
            "worktree paths must be absolute",
            stage=stage,
            plan=plan,
        )
    canonical = _reject_symlink_components(path, stage=stage, plan=plan)
    effective = _reject_symlink_components(cwd, stage=stage, plan=plan)
    if canonical.name != values["nodeId"] or canonical.parent.name != values["graphId"]:
        raise _error(
            PAC_WORKTREE_IDENTITY_MISMATCH,
            "managed path does not identify the planned graph node",
            stage=stage,
            plan=plan,
        )
    return canonical, effective


def _repo_ready(
    repo: Path,
    *,
    deadline: float,
    stage: str,
    plan: Mapping[str, object] | None,
    code: str,
) -> None:
    top = _check(
        repo,
        ["rev-parse", "--show-toplevel"],
        deadline=deadline,
        code=code,
        stage=stage,
        plan=plan,
    ).strip()
    if not top or Path(top).resolve() != repo:
        raise _error(
            PAC_WORKTREE_IDENTITY_MISMATCH,
            "worktree repository root does not match the plan",
            stage=stage,
            plan=plan,
        )
    common = _check(
        repo,
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        deadline=deadline,
        code=code,
        stage=stage,
        plan=plan,
    ).strip()
    if not common:
        raise _error(
            PAC_WORKTREE_IDENTITY_MISMATCH,
            "worktree common directory is unavailable",
            stage=stage,
            plan=plan,
        )


def _records(
    repo: Path,
    *,
    deadline: float,
    stage: str,
    plan: Mapping[str, object] | None,
    code: str,
) -> list[dict[str, str | bool]]:
    output = _check(
        repo,
        ["worktree", "list", "--porcelain"],
        deadline=deadline,
        code=code,
        stage=stage,
        plan=plan,
    )
    result: list[dict[str, str | bool]] = []
    current: dict[str, str | bool] = {}
    for line in (*output.splitlines(), ""):
        if not line:
            if current:
                result.append(current)
                current = {}
            continue
        key, separator, value = line.partition(" ")
        if key == "locked":
            current["locked"] = True
            if separator:
                current["lockReason"] = value
        elif key == "branch":
            current[key] = value.removeprefix("refs/heads/")
        elif key in {"worktree", "HEAD"}:
            current[key] = value
    return result


def _record_for(records: Sequence[Mapping[str, str | bool]], path: Path) -> Mapping[str, str | bool] | None:
    for record in records:
        raw = record.get("worktree")
        if isinstance(raw, str) and Path(raw).resolve(strict=False) == path:
            return record
    return None


def _result(values: Mapping[str, str], *, state: str, reason: str | None = None) -> dict[str, str | None]:
    return {
        "state": state,
        "path": values["path"],
        "branch": values["branch"],
        "baseOid": values["baseOid"],
        "reason": reason,
    }


def _raise_identity(message: str, *, stage: str, plan: Mapping[str, object]) -> None:
    raise _error(
        PAC_WORKTREE_IDENTITY_MISMATCH,
        message,
        stage=stage,
        plan=plan,
    )


def resolve_base(repo: Path, base_ref: str, *, timeout_s: float) -> str:
    """Resolve a local commit without fetching or mutating the repository."""

    deadline = _deadline(
        timeout_s, stage="base-resolution", code=PAC_WORKTREE_PREPARE_FAILED
    )
    if not isinstance(repo, Path):
        repo = Path(repo)
    if not isinstance(base_ref, str) or not base_ref or "\x00" in base_ref or base_ref.startswith("-"):
        raise _error(
            PAC_WORKTREE_PREPARE_FAILED,
            "base reference is not safe",
            stage="base-resolution",
        )
    repo = _reject_symlink_components(repo, stage="base-resolution", plan=None)
    if not repo.is_dir():
        raise _error(
            PAC_WORKTREE_PREPARE_FAILED,
            "repository is unavailable",
            stage="base-resolution",
        )
    output = _check(
        repo,
        ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{base_ref}^{{commit}}"],
        deadline=deadline,
        code=PAC_WORKTREE_PREPARE_FAILED,
        stage="base-resolution",
    ).strip()
    if not _OID_RE.fullmatch(output):
        raise _error(
            PAC_WORKTREE_PREPARE_FAILED,
            "base reference is not a commit",
            stage="base-resolution",
        )
    return output


def prepare_worktree(plan: dict, *, timeout_s: float) -> dict:
    """Create or replay one owned worktree, pinned to the prepared base OID."""

    deadline = _deadline(
        timeout_s,
        stage="prepare",
        code=PAC_WORKTREE_PREPARE_FAILED,
        plan=plan if isinstance(plan, Mapping) else None,
    )
    values = _required_plan(plan, stage="prepare")
    repo = _repo_path(values, stage="prepare", plan=plan)
    path, _ = _plan_path(values, stage="prepare", plan=plan)
    _repo_ready(
        repo,
        deadline=deadline,
        stage="prepare",
        plan=plan,
        code=PAC_WORKTREE_PREPARE_FAILED,
    )
    pinned = _check(
        repo,
        ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{values['baseOid']}^{{commit}}"],
        deadline=deadline,
        code=PAC_WORKTREE_PREPARE_FAILED,
        stage="prepare-base",
        plan=plan,
    ).strip()
    if pinned.lower() != values["baseOid"].lower():
        _raise_identity("prepared base OID is not the pinned commit", stage="prepare-base", plan=plan)

    records = _records(
        repo,
        deadline=deadline,
        stage="prepare-registration",
        plan=plan,
        code=PAC_WORKTREE_PREPARE_FAILED,
    )
    existing = _record_for(records, path)
    if existing is not None:
        if existing.get("locked") or existing.get("branch") != values["branch"]:
            _raise_identity("registered worktree does not match the plan", stage="prepare-registration", plan=plan)
        return _result(values, state="prepared")
    if path.exists() or path.is_symlink():
        _raise_identity("unregistered path cannot be adopted", stage="prepare-path", plan=plan)

    branch_ref = f"refs/heads/{values['branch']}"
    branch_check = _run(
        repo,
        ["show-ref", "--verify", "--quiet", branch_ref],
        deadline=deadline,
        code=PAC_WORKTREE_PREPARE_FAILED,
        stage="prepare-branch",
        plan=plan,
    )
    if branch_check.returncode == 0:
        _raise_identity("existing branch cannot be adopted", stage="prepare-branch", plan=plan)
    if branch_check.returncode not in {1, 128}:
        raise _error(
            PAC_WORKTREE_PREPARE_FAILED,
            "could not inspect worktree branch",
            stage="prepare-branch",
            plan=plan,
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise _error(
            PAC_WORKTREE_PREPARE_FAILED,
            "managed worktree parent cannot be created",
            stage="prepare-path",
            plan=plan,
        ) from error
    _run(
        repo,
        ["worktree", "add", "-b", values["branch"], "--", str(path), values["baseOid"]],
        deadline=deadline,
        code=PAC_WORKTREE_PREPARE_FAILED,
        stage="prepare-add",
        plan=plan,
    )
    records = _records(
        repo,
        deadline=deadline,
        stage="prepare-verify",
        plan=plan,
        code=PAC_WORKTREE_PREPARE_FAILED,
    )
    registered = _record_for(records, path)
    if registered is None or registered.get("branch") != values["branch"]:
        _raise_identity("git did not register the planned worktree", stage="prepare-verify", plan=plan)
    return _result(values, state="prepared")


def cleanup_worktree(plan: dict, *, timeout_s: float) -> dict:
    """Remove one clean, proven-owned worktree without force or branch deletion."""

    deadline = _deadline(
        timeout_s,
        stage="cleanup",
        code=PAC_WORKTREE_CLEANUP_FAILED,
        plan=plan if isinstance(plan, Mapping) else None,
    )
    values = _required_plan(plan, stage="cleanup")
    repo = _repo_path(values, stage="cleanup", plan=plan)
    path, effective_cwd = _plan_path(values, stage="cleanup", plan=plan)
    if effective_cwd != path:
        return _result(values, state="retained", reason="effective-cwd-mismatch")
    if path.is_symlink():
        return _result(values, state="attention", reason="replaced-path")
    if not path.exists():
        return _result(values, state="removed")
    _repo_ready(
        repo,
        deadline=deadline,
        stage="cleanup",
        plan=plan,
        code=PAC_WORKTREE_CLEANUP_FAILED,
    )
    records = _records(
        repo,
        deadline=deadline,
        stage="cleanup-registration",
        plan=plan,
        code=PAC_WORKTREE_CLEANUP_FAILED,
    )
    record = _record_for(records, path)
    if record is None or record.get("branch") != values["branch"]:
        return _result(values, state="attention", reason="identity-mismatch")
    if record.get("locked"):
        return _result(values, state="retained", reason="locked")
    status = _check(
        path,
        [
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
            "-z",
            "--",
        ],
        deadline=deadline,
        code=PAC_WORKTREE_CLEANUP_FAILED,
        stage="cleanup-dirty-check",
        plan=plan,
    )
    if status:
        return _result(values, state="retained", reason="dirty-or-untracked")
    _run(
        repo,
        ["worktree", "remove", "--", str(path)],
        deadline=deadline,
        code=PAC_WORKTREE_CLEANUP_FAILED,
        stage="cleanup-remove",
        plan=plan,
    )
    if path.exists() or path.is_symlink():
        return _result(values, state="attention", reason="remove-incomplete")
    return _result(values, state="removed")


__all__ = ["cleanup_worktree", "prepare_worktree", "resolve_base"]
