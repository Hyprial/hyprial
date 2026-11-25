"""Shared helpers for GUI development sources.

Decision (2026-11-14, GUI launch consolidation PR 1a): the CLI no longer
creates ``.worktrees/gui-dev-auto`` for a source checkout; ``hyprial gui dev``
uses the checkout's own ``gui/`` directly.  What remains here is the read-only
source-checkout discovery and the worktree record, dependency install and Git
runners reused by the installed-wheel exact-``sourceCommit`` worktree in
``install/dev_source.py``.  Nothing in this module writes to a source checkout.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
from typing import Any

from hyprial.kernel import manysys

from ..errors import GuiError
from ..install.resources import redact_remote

SOURCE_CODE = "GUI_AUTO_WORKTREE_SOURCE"
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
    """The managed exact-sourceCommit worktree prepared for an installed wheel."""

    source: Path
    path: Path
    branch: str
    base: str
    home: Path
    created: bool
    install: DependencyInstall
    managed: bool = False
    dirty: bool = False
    remote: str | None = None


def auto_worktree_payload(auto: AutoGuiWorktree | None) -> dict[str, Any] | None:
    """Startup metadata for the managed worktree, or ``None`` when unused.

    The remote is display metadata and is always redacted here, not by the
    caller, so every consumer of the startup payload gets the same treatment as
    the manifest and explicit-dev paths.
    """
    if auto is None:
        return None
    return {
        "source": str(auto.source),
        "path": str(auto.path),
        "branch": auto.branch,
        "base": auto.base,
        "created": auto.created,
        "managed": auto.managed,
        "dirty": auto.dirty,
        "remote": redact_remote(auto.remote or ""),
        "install": {
            "command": list(auto.install.command),
            "cwd": str(auto.install.cwd),
            "ran": auto.install.ran,
            "returncode": auto.install.returncode,
            "reason": auto.install.reason,
        },
    }


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
        "pass --gui-home or HYPRIAL_DEV_GUI_HOME to select an existing GUI checkout",
        {
            "cwd": str(current),
            "rejectedRepositories": rejected[:20],
            "hint": "run from the repository root or set HYPRIAL_DEV_GUI_HOME",
        },
    )


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


def _git_succeeds(
    git: str,
    root: Path,
    arguments: list[str],
    *,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> bool:
    return _run_git(git, root, arguments, environ=environ, run=run).returncode == 0


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
    if manysys.managed_launcher_names("npm")[0].endswith(".cmd") and Path(
        npm
    ).suffix.lower() in {".cmd", ".bat"}:
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
