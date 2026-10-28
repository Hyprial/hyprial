"""Validation of the npm GUI project and its Git provenance.

This module is read-only: it never checks out, resets, cleans, or writes to the
selected worktree.  It exists separately from the launcher so the project and
process-lifecycle rules stay independently testable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import GuiError

_NODE_ENGINE_RE = re.compile(r"(?:>=|\^|~)?\s*(\d+)")
_NODE_VERSION_RE = re.compile(r"v?(\d+)")
_GUI_PACKAGE_NAMES = frozenset({"@hyprial/gui-app", "@hyprial/gui"})


@dataclass(frozen=True)
class GuiProject:
    """A validated npm project backed by a Git worktree."""

    home: Path
    worktree: Path
    branch: str
    node: str
    npm: str
    node_version: str
    npm_version: str
    scripts: Mapping[str, str]


def resolve_gui_home(
    explicit: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Path:
    """Resolve the npm project root from option, environment, or checkout.

    The value is the directory containing ``package.json`` (normally
    ``<worktree>/gui``), not necessarily the Git worktree root.  An explicit
    option wins over the environment; neither is modified.
    """
    source = os.environ if environ is None else environ
    if explicit is not None:
        return explicit.expanduser().resolve()
    configured = str(source.get("HYPRIAL_GUI_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    current = (cwd or Path.cwd()).expanduser().resolve()
    candidates: list[Path] = []
    for base in (current, *current.parents):
        candidate = base / "gui"
        if candidate.is_dir():
            candidates.append(candidate)
        if _looks_like_gui_project(base):
            candidates.append(base)
    module = Path(__file__).resolve()
    for base in module.parents:
        if (base / "src" / "hyprial").is_dir() and (base / "gui").is_dir():
            candidates.append(base / "gui")
            break
    for candidate in candidates:
        if (candidate / "package.json").is_file():
            return candidate.resolve()
    # Keep the most useful path in the eventual error message.
    return (current / "gui").resolve()


def inspect_project(
    home: Path,
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> GuiProject:
    """Validate the npm project and its Git provenance without writing to it."""
    package_file = home / "package.json"
    if not home.is_dir():
        raise GuiError(
            "GUI_HOME_INVALID",
            f"GUI project directory does not exist: {home}",
            {"gui_home": str(home)},
        )
    try:
        package = json.loads(package_file.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise GuiError(
            "GUI_HOME_INVALID",
            f"GUI project has no package.json: {home}",
            {"gui_home": str(home)},
        ) from error
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GuiError(
            "GUI_HOME_INVALID",
            f"cannot read {package_file}: {error}",
            {"gui_home": str(home)},
        ) from error
    if not isinstance(package, dict):
        raise GuiError(
            "GUI_HOME_INVALID",
            f"{package_file} must contain a JSON object",
            {"gui_home": str(home)},
        )
    scripts = package.get("scripts")
    if not isinstance(scripts, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in scripts.items()
    ):
        raise GuiError(
            "GUI_HOME_INVALID",
            f"{package_file} must define a string-valued scripts object",
            {"gui_home": str(home)},
        )

    git = which("git")
    if not git:
        raise GuiError(
            "GUI_GIT_MISSING",
            "git is required to verify the independent GUI worktree",
        )
    worktree = _git_output(git, home, ["rev-parse", "--show-toplevel"], run)
    if not worktree:
        raise GuiError(
            "GUI_NOT_GIT_WORKTREE",
            f"{home} is not inside a Git worktree; do not use a copied directory",
            {"gui_home": str(home)},
        )
    branch = _git_output(git, home, ["rev-parse", "--abbrev-ref", "HEAD"], run)
    if branch == "HEAD":
        branch = _git_output(git, home, ["rev-parse", "--short", "HEAD"], run)

    node = which("node")
    if not node:
        raise GuiError(
            "GUI_NODE_MISSING",
            "Node.js is required to run the GUI; install Node 24 or newer",
        )
    npm = which("npm")
    if not npm:
        raise GuiError(
            "GUI_NPM_MISSING",
            "npm is required to run the GUI; install Node 24 or newer",
        )
    node_version = _version(node, run)
    npm_version = _version(npm, run)
    _check_engine(package.get("engines"), node_version)

    return GuiProject(
        home=home,
        worktree=Path(worktree).resolve(),
        branch=branch,
        node=node,
        npm=npm,
        node_version=node_version,
        npm_version=npm_version,
        scripts=scripts,
    )


def _npm_prefix(npm: str) -> list[str]:
    """Make Windows ``npm.cmd`` executable without a shell on POSIX."""
    if os.name == "nt" and Path(npm).suffix.lower() in {".cmd", ".bat"}:
        command = os.environ.get("COMSPEC", "cmd.exe")
        return [command, "/d", "/s", "/c", npm]
    return [npm]


def _version(executable: str, run: Callable[..., Any]) -> str:
    is_npm = Path(executable).name.lower().startswith("npm")
    argv = _npm_prefix(executable) if is_npm else [executable]
    try:
        completed = run(
            [*argv, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GuiError(
            "GUI_TOOL_UNAVAILABLE",
            f"cannot query {executable} --version: {error}",
        ) from error
    output = (completed.stdout or "").strip()
    if completed.returncode != 0 or not output:
        raise GuiError(
            "GUI_TOOL_UNAVAILABLE",
            f"{executable} --version failed with exit {completed.returncode}",
            {"stderr": (completed.stderr or "").strip()[:500]},
        )
    return output.splitlines()[0].strip()


def _check_engine(engines: Any, node_version: str) -> None:
    if not isinstance(engines, dict):
        return
    required = engines.get("node")
    if not isinstance(required, str):
        return
    match = _NODE_ENGINE_RE.search(required)
    current = _NODE_VERSION_RE.search(node_version)
    if match is None or current is None:
        return
    if int(current.group(1)) < int(match.group(1)):
        raise GuiError(
            "GUI_NODE_VERSION",
            f"GUI requires Node {required}; found {node_version}",
            {"required": required, "found": node_version},
        )


def _git_output(
    git: str,
    home: Path,
    arguments: list[str],
    run: Callable[..., Any],
) -> str:
    try:
        completed = run(
            [git, "-C", str(home), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GuiError(
            "GUI_NOT_GIT_WORKTREE",
            f"cannot inspect Git worktree at {home}: {error}",
            {"gui_home": str(home)},
        ) from error
    if completed.returncode != 0:
        if arguments == ["rev-parse", "--show-toplevel"]:
            raise GuiError(
                "GUI_NOT_GIT_WORKTREE",
                f"{home} is not inside a Git worktree; do not use a copied directory",
                {"gui_home": str(home)},
            )
        return ""
    return (completed.stdout or "").strip()


def _looks_like_gui_project(path: Path) -> bool:
    package_file = path / "package.json"
    if not package_file.is_file():
        return False
    try:
        package = json.loads(package_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(package, dict) and package.get("name") in _GUI_PACKAGE_NAMES


def looks_like_gui_project(path: Path) -> bool:
    """Public predicate used by docs/tests without starting a GUI."""
    return _looks_like_gui_project(path)
