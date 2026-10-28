"""Create or restore the automatic sparse GUI development worktree.

This module is the only part of the GUI launcher that writes to Git and to a
GUI checkout.  It is used exclusively by ``hyprial gui dev`` when the caller
did not provide ``--gui-home`` or ``HYPRIAL_GUI_HOME``.  It creates a linked
worktree with a stable branch and a cone sparse checkout containing only
``gui``; later invocations restore that same worktree without resetting,
cleaning, force-checking-out, rebasing, stashing, committing, or pushing it.

The source repository is discovered with read-only Git commands.  Existing
worktrees at the target path are validated strictly and otherwise reported as
conflicts; nothing is deleted, moved, or reused on a best-effort guess.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Any

from ..errors import GuiError

AUTO_WORKTREE_RELATIVE = Path(".worktrees") / "gui-dev-auto"
AUTO_BRANCH = "work/gui-dev-auto"
SPARSE_PATHS = ("gui",)

SOURCE_CODE = "GUI_AUTO_WORKTREE_SOURCE"
CONFLICT_CODE = "GUI_AUTO_WORKTREE_CONFLICT"
FAILED_CODE = "GUI_AUTO_WORKTREE_FAILED"
INSTALL_FAILED_CODE = "GUI_AUTO_WORKTREE_INSTALL_FAILED"

_GIT_TIMEOUT_SECONDS = 60.0
_INSTALL_TIMEOUT_SECONDS = 900.0
_MAX_DIAGNOSTIC_CHARS = 1200


@dataclass(frozen=True)
class DependencyInstall:
    """The one dependency install attempted (or skipped) for a worktree."""

    command: tuple[str, ...]
    cwd: Path
    ran: bool
    returncode: int | None = None
    output: str = ""
    reason: str | None = None


@dataclass(frozen=True)
class AutoGuiWorktree:
    """The independent sparse worktree used by the no-option dev mode."""

    source: Path
    path: Path
    branch: str
    base: str
    home: Path
    created: bool
    install: DependencyInstall


def auto_worktree_payload(auto: AutoGuiWorktree | None) -> dict[str, Any] | None:
    """Startup metadata for the automatic worktree, or ``None`` when unused."""
    if auto is None:
        return None
    return {
        "source": str(auto.source),
        "path": str(auto.path),
        "branch": auto.branch,
        "base": auto.base,
        "created": auto.created,
        "install": {
            "command": list(auto.install.command),
            "cwd": str(auto.install.cwd),
            "ran": auto.install.ran,
            "returncode": auto.install.returncode,
            "reason": auto.install.reason,
        },
    }


def ensure_dev_worktree(
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> AutoGuiWorktree:
    """Discover a source checkout, then create or restore the sparse worktree."""
    environment = dict(os.environ if environ is None else environ)
    source = discover_source_repo(
        cwd=cwd,
        environ=environment,
        which=which,
        run=run,
    )
    git = _require_tool(
        "git",
        which,
        "GUI_GIT_MISSING",
        "git is required to create the independent GUI development worktree",
    )
    base = _git_required(
        git,
        source,
        ["rev-parse", "HEAD"],
        environ=environment,
        run=run,
        code=SOURCE_CODE,
        message=f"cannot read HEAD of source GUI repository {source}",
    )
    if not _git_succeeds(
        git,
        source,
        ["cat-file", "-e", f"{base}:gui/package.json"],
        environ=environment,
        run=run,
    ):
        raise GuiError(
            SOURCE_CODE,
            f"source repository {source} does not contain gui/package.json at HEAD",
            {"source": str(source), "base": base},
        )

    if _is_auto_worktree(git, source, environment, run):
        path = source
        created = False
        _validate_existing(git, source, path, environment, run)
    else:
        # Keep the path unresolved so a symlinked ``.worktrees`` or target is
        # rejected below instead of silently redirecting writes outside it.
        path = source / AUTO_WORKTREE_RELATIVE
        if _path_present(path):
            created = False
            _validate_existing(git, source, path, environment, run)
        else:
            _guard_new_worktree(git, source, path, environment, run)
            _create_worktree(git, source, path, base, environment, run)
            created = True
            _validate_existing(git, source, path, environment, run)

    actual_base = _git_required(
        git,
        path,
        ["rev-parse", "HEAD"],
        environ=environment,
        run=run,
        code=FAILED_CODE,
        message=f"cannot read HEAD of GUI worktree {path}",
    )
    home = path / SPARSE_PATHS[0]
    install = _ensure_dependencies(
        home,
        source=source,
        worktree=path,
        environ=environment,
        which=which,
        run=run,
    )
    return AutoGuiWorktree(
        source=source,
        path=path,
        branch=AUTO_BRANCH,
        base=actual_base,
        home=home,
        created=created,
        install=install,
    )


def discover_source_repo(
    *,
    cwd: Path | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> Path:
    """Find the nearest Git checkout whose HEAD contains ``gui/package.json``.

    The current directory is searched first.  The launcher module's checkout is
    the fallback so an installed CLI can still start from an unrelated cwd, but
    every candidate must be a real Git worktree with the GUI at HEAD.
    """
    git = _require_tool(
        "git",
        which,
        "GUI_GIT_MISSING",
        "git is required to discover the source GUI repository",
    )
    environment = dict(os.environ if environ is None else environ)
    current = (cwd or Path.cwd()).expanduser().resolve()
    module = Path(__file__).resolve()
    candidates = [current, *current.parents, *module.parents]
    seen: set[Path] = set()
    rejected: list[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        completed = _run_git(
            git,
            candidate,
            ["rev-parse", "--show-toplevel"],
            environ=environment,
            run=run,
        )
        if completed.returncode != 0:
            continue
        text = (completed.stdout or "").strip()
        if not text:
            continue
        root = Path(text).resolve()
        if not _git_succeeds(
            git,
            root,
            ["cat-file", "-e", "HEAD:gui/package.json"],
            environ=environment,
            run=run,
        ):
            rejected.append(str(root))
            continue
        return root
    raise GuiError(
        SOURCE_CODE,
        "cannot find a source Git repository whose HEAD contains gui/package.json; "
        "pass --gui-home or HYPRIAL_GUI_HOME to select an existing GUI checkout",
        {
            "cwd": str(current),
            "rejectedRepositories": rejected[:20],
            "hint": "run from the repository root or set HYPRIAL_GUI_HOME",
        },
    )


def _create_worktree(
    git: str,
    source: Path,
    path: Path,
    base: str,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> None:
    """Run the exact create -> sparse -> checkout sequence, preserving failures."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise GuiError(
            CONFLICT_CODE,
            f"cannot create the auto GUI worktree parent {path.parent}: {error}",
            {"source": str(source), "path": str(path)},
        ) from error
    commands = (
        [
            git,
            "-C",
            str(source),
            "worktree",
            "add",
            "-b",
            AUTO_BRANCH,
            "--no-checkout",
            str(path),
            base,
        ],
        [
            git,
            "-C",
            str(path),
            "sparse-checkout",
            "set",
            "--cone",
            *SPARSE_PATHS,
        ],
        [git, "-C", str(path), "checkout"],
    )
    for command in commands:
        completed = _run_command(
            command,
            environ=environ,
            run=run,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
        if completed.returncode == 0:
            continue
        detail = _diagnostic(completed)
        lowered = detail.lower()
        code = (
            CONFLICT_CODE
            if "already exists" in lowered or "already checked out" in lowered
            else FAILED_CODE
        )
        raise GuiError(
            code,
            f"cannot create the automatic sparse GUI worktree at {path} "
            f"(exit {completed.returncode}); the partial scene was preserved",
            {
                "source": str(source),
                "path": str(path),
                "branch": AUTO_BRANCH,
                "base": base,
                "command": command,
                "returncode": completed.returncode,
                "output": detail,
            },
        )


def _guard_new_worktree(
    git: str,
    source: Path,
    path: Path,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> None:
    """Reject a target path or branch that is not ours before writing anything."""
    parent = path.parent
    if parent.is_symlink():
        raise GuiError(
            CONFLICT_CODE,
            f"automatic GUI worktree parent {parent} is a symlink; refusing to "
            "write through it",
            {"source": str(source), "path": str(path)},
        )
    if _path_present(parent) and not parent.is_dir():
        raise GuiError(
            CONFLICT_CODE,
            f"automatic GUI worktree parent {parent} is not a directory",
            {"source": str(source), "path": str(path)},
        )
    if _branch_exists(git, source, AUTO_BRANCH, environ, run):
        raise GuiError(
            CONFLICT_CODE,
            f"branch {AUTO_BRANCH} already exists but the automatic worktree is "
            f"missing at {path}; refusing to reuse or move it",
            {
                "source": str(source),
                "path": str(path),
                "branch": AUTO_BRANCH,
                "reason": "branch-already-exists",
            },
        )


def _validate_existing(
    git: str,
    source: Path,
    path: Path,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> None:
    """Validate a path without modifying it; every mismatch is a clear conflict."""
    data = {"source": str(source), "path": str(path), "branch": AUTO_BRANCH}
    if path.parent.is_symlink():
        raise GuiError(
            CONFLICT_CODE,
            f"automatic GUI worktree parent {path.parent} is a symlink; "
            "refusing to use it",
            data,
        )
    if path.is_symlink() or not path.is_dir():
        raise GuiError(
            CONFLICT_CODE,
            f"{path} is not a Git worktree; refusing to delete, copy, or reuse it",
            data,
        )
    toplevel = _git_text(
        git,
        path,
        ["rev-parse", "--show-toplevel"],
        environ=environ,
        run=run,
        allow_failure=True,
    )
    if toplevel is None or Path(toplevel).resolve() != path:
        raise GuiError(
            CONFLICT_CODE,
            f"{path} is not a Git worktree root; refusing to delete or reuse it",
            data,
        )
    source_common = _git_common_dir(git, source, environ, run)
    path_common = _git_common_dir(git, path, environ, run)
    if source_common is None or path_common is None or source_common != path_common:
        raise GuiError(
            CONFLICT_CODE,
            f"{path} belongs to a different Git repository than {source}; "
            "refusing to reuse it",
            {**data, "sourceCommonDir": str(source_common), "pathCommonDir": str(path_common)},
        )
    branch = _git_text(
        git,
        path,
        ["branch", "--show-current"],
        environ=environ,
        run=run,
        allow_failure=True,
    )
    if branch != AUTO_BRANCH:
        raise GuiError(
            CONFLICT_CODE,
            f"{path} is on branch {branch or '(detached)'}, not {AUTO_BRANCH}; "
            "refusing to switch or reuse it",
            {**data, "actualBranch": branch},
        )
    sparse = _sparse_entries(git, path, environ, run)
    if sparse != list(SPARSE_PATHS):
        raise GuiError(
            CONFLICT_CODE,
            f"{path} must use a sparse checkout containing only {list(SPARSE_PATHS)}; "
            f"found {sparse!r}",
            {**data, "sparseCheckout": sparse},
        )
    gui = path / SPARSE_PATHS[0]
    if gui.is_symlink() or not gui.is_dir():
        raise GuiError(
            CONFLICT_CODE,
            f"{gui} is not the materialized sparse GUI directory; refusing to "
            "follow or reuse it",
            data,
        )
    if not (gui / "package.json").is_file():
        raise GuiError(
            CONFLICT_CODE,
            f"{path} has no gui/package.json; refusing to guess or reuse it",
            data,
        )


def _is_auto_worktree(
    git: str,
    root: Path,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> bool:
    """Whether ``root`` is already a linked worktree for this auto branch.

    Reusing the linked worktree makes the command stable when run from inside
    it.  The main checkout is deliberately not reused even if someone checked
    out the same branch there; a source checkout must remain independent.
    """
    branch = _git_text(
        git,
        root,
        ["branch", "--show-current"],
        environ=environ,
        run=run,
        allow_failure=True,
    )
    if branch != AUTO_BRANCH:
        return False
    common = _git_common_dir(git, root, environ, run)
    git_dir = _git_dir(git, root, environ, run)
    if common is None or git_dir is None or common == git_dir:
        return False
    return _sparse_entries(git, root, environ, run) == list(SPARSE_PATHS)


def _ensure_dependencies(
    home: Path,
    *,
    source: Path,
    worktree: Path,
    environ: Mapping[str, str],
    which: Callable[[str], str | None],
    run: Callable[..., Any],
) -> DependencyInstall:
    """Install dependencies only when ``gui/node_modules`` is absent."""
    node_modules = home / "node_modules"
    if _path_present(node_modules):
        return DependencyInstall(
            command=(),
            cwd=home,
            ran=False,
            reason="node_modules already exists",
        )
    npm = _require_tool(
        "npm",
        which,
        "GUI_NPM_MISSING",
        "npm is required to install the GUI development dependencies",
    )
    command = (*_npm_prefix(npm, environ), "ci", "--ignore-scripts")
    try:
        completed = run(
            list(command),
            cwd=str(home),
            env=dict(environ),
            check=False,
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GuiError(
            INSTALL_FAILED_CODE,
            f"cannot install GUI dependencies with npm ci in {home}: {error}; "
            "the worktree was preserved",
            {
                "command": list(command),
                "cwd": str(home),
                "gui_home": str(home),
                "path": str(worktree),
                "source": str(source),
            },
        ) from error
    returncode = int(getattr(completed, "returncode", completed))
    output = _diagnostic(completed)
    if returncode != 0:
        raise GuiError(
            INSTALL_FAILED_CODE,
            f"npm ci failed with exit {returncode} in {home}; "
            "the worktree was preserved for inspection",
            {
                "command": list(command),
                "cwd": str(home),
                "gui_home": str(home),
                "path": str(worktree),
                "source": str(source),
                "returncode": returncode,
                "output": output,
            },
        )
    return DependencyInstall(
        command=command,
        cwd=home,
        ran=True,
        returncode=returncode,
        output=output,
    )


def _branch_exists(
    git: str,
    root: Path,
    branch: str,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> bool:
    completed = _run_git(
        git,
        root,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        environ=environ,
        run=run,
    )
    return completed.returncode == 0


def _sparse_entries(
    git: str,
    root: Path,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> list[str] | None:
    completed = _run_git(
        git,
        root,
        ["sparse-checkout", "list"],
        environ=environ,
        run=run,
    )
    if completed.returncode != 0:
        return None
    return [
        line.strip()
        for line in (completed.stdout or "").splitlines()
        if line.strip()
    ]


def _git_common_dir(
    git: str,
    root: Path,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> Path | None:
    return _git_path(git, root, ["rev-parse", "--git-common-dir"], environ, run)


def _git_dir(
    git: str,
    root: Path,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> Path | None:
    return _git_path(git, root, ["rev-parse", "--git-dir"], environ, run)


def _git_path(
    git: str,
    root: Path,
    arguments: list[str],
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> Path | None:
    raw = _git_text(
        git,
        root,
        arguments,
        environ=environ,
        run=run,
        allow_failure=True,
    )
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _git_succeeds(
    git: str,
    root: Path,
    arguments: list[str],
    *,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> bool:
    return _run_git(git, root, arguments, environ=environ, run=run).returncode == 0


def _git_text(
    git: str,
    root: Path,
    arguments: list[str],
    *,
    environ: Mapping[str, str],
    run: Callable[..., Any],
    allow_failure: bool,
) -> str | None:
    completed = _run_git(git, root, arguments, environ=environ, run=run)
    if completed.returncode != 0:
        if allow_failure:
            return None
        raise GuiError(
            FAILED_CODE,
            f"git {' '.join(arguments)} failed in {root} "
            f"(exit {completed.returncode})",
            {
                "root": str(root),
                "command": ["git", *arguments],
                "returncode": completed.returncode,
                "output": _diagnostic(completed),
            },
        )
    return (completed.stdout or "").strip()


def _git_required(
    git: str,
    root: Path,
    arguments: list[str],
    *,
    environ: Mapping[str, str],
    run: Callable[..., Any],
    code: str,
    message: str,
) -> str:
    completed = _run_git(git, root, arguments, environ=environ, run=run)
    if completed.returncode != 0:
        raise GuiError(
            code,
            message,
            {
                "root": str(root),
                "command": ["git", *arguments],
                "returncode": completed.returncode,
                "output": _diagnostic(completed),
            },
        )
    return (completed.stdout or "").strip()


def _run_git(
    git: str,
    root: Path,
    arguments: list[str],
    *,
    environ: Mapping[str, str],
    run: Callable[..., Any],
):
    return _run_command(
        [git, "-C", str(root), *arguments],
        environ=environ,
        run=run,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _run_command(
    command: list[str],
    *,
    environ: Mapping[str, str],
    run: Callable[..., Any],
    timeout: float,
):
    try:
        return run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=dict(environ),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GuiError(
            FAILED_CODE,
            f"cannot run {command[0]}: {error}",
            {"command": command, "output": str(error)},
        ) from error


def _npm_prefix(npm: str, environ: Mapping[str, str]) -> list[str]:
    """Make Windows ``npm.cmd`` executable without a shell on POSIX."""
    if os.name == "nt" and Path(npm).suffix.lower() in {".cmd", ".bat"}:
        command = environ.get("COMSPEC") or "cmd.exe"
        return [command, "/d", "/s", "/c", npm]
    return [npm]


def _require_tool(
    name: str,
    which: Callable[[str], str | None],
    code: str,
    message: str,
) -> str:
    executable = which(name)
    if not executable:
        raise GuiError(code, message, {"tool": name})
    return executable


def _path_present(path: Path) -> bool:
    """Include broken symlinks, which must be conflicts rather than overwritten."""
    return path.is_symlink() or path.exists()


def _diagnostic(completed: Any) -> str:
    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    text = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
    return text[-_MAX_DIAGNOSTIC_CHARS:]
