"""Select an installed GUI target, an explicit project, or a dev worktree.

This is the policy boundary for the two environment variables:

* normal mode reads ``HYPRIAL_GUI_HOME`` or the verified wheel ``_gui``;
* dev mode reads ``HYPRIAL_DEV_GUI_HOME`` or ``--gui-home``, then a source
  checkout, then the wheel's exact source commit.

The two variables are never fallbacks for one another.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any

from ..errors import GuiError
from ..project import GuiProject, resolve_gui_home
from ..worktree import SOURCE_CODE, AutoGuiWorktree, ensure_dev_worktree
from .dev_source import ensure_installed_dev_worktree
from .resources import (
    MANIFEST_NAME,
    GuiAssetError,
    GuiAssets,
    installed_asset_root,
    load_assets,
    redact_remote,
    resolve_explicit_assets,
)

NORMAL_HOME_ENV = "HYPRIAL_GUI_HOME"
DEV_HOME_ENV = "HYPRIAL_DEV_GUI_HOME"


@dataclass(frozen=True)
class LaunchTarget:
    project: GuiProject
    assets: GuiAssets | None = None
    auto: AutoGuiWorktree | None = None
    static_root: Path | None = None
    dev_prompt: str = ""


def select_normal_target(
    gui_home: Path | None,
    *,
    environ: Mapping[str, str],
    cwd: Path | None,
    inspect: Callable[..., GuiProject],
    which: Callable[[str], str | None],
    run: Callable[..., Any],
) -> LaunchTarget:
    explicit = gui_home is not None or _configured(environ, NORMAL_HOME_ENV)
    if explicit:
        home = resolve_gui_home(gui_home, environ=environ, cwd=cwd)
        assets = _explicit_assets(home)
        return LaunchTarget(
            project=_static_project(assets.root, assets),
            assets=assets,
            static_root=assets.root,
        )

    assets = _builtin_assets()
    if assets is not None:
        return LaunchTarget(
            project=_static_project(assets.root, assets),
            assets=assets,
            static_root=assets.root,
        )
    if _is_installed_package():
        raise GuiError(
            "GUI_ASSETS_MISSING",
            "the installed hyprial package has no built GUI resources under "
            f"{installed_asset_root()}; reinstall the wheel or pass "
            "HYPRIAL_GUI_HOME with a built GUI",
            {"asset_root": str(installed_asset_root())},
        )
    home = resolve_gui_home(gui_home, environ=environ, cwd=cwd)
    return LaunchTarget(project=inspect(home, which=which, run=run))


def select_dev_target(
    gui_home: Path | None,
    *,
    environ: Mapping[str, str],
    cwd: Path | None,
    inspect: Callable[..., GuiProject],
    which: Callable[[str], str | None],
    run: Callable[..., Any],
    ensure: Callable[..., AutoGuiWorktree] = ensure_dev_worktree,
) -> LaunchTarget:
    explicit = gui_home is not None or _configured(environ, DEV_HOME_ENV)
    auto: AutoGuiWorktree | None = None
    if explicit:
        home = (
            gui_home.expanduser().resolve()
            if gui_home is not None
            else Path(str(environ[DEV_HOME_ENV])).expanduser().resolve()
        )
        _reject_non_dev_home(home, environ)
        project = inspect(home, which=which, run=run)
    else:
        try:
            auto = ensure(
                cwd=cwd, environ=environ, which=which, run=run
            )
        except GuiError as error:
            if error.code != SOURCE_CODE:
                raise
            assets = _builtin_assets()
            if assets is None:
                raise GuiError(
                    error.code,
                    "cannot find a source Git repository and the installed GUI "
                    "has no source manifest; pass --gui-home or "
                    "HYPRIAL_DEV_GUI_HOME to select an existing GUI checkout",
                    error.data,
                ) from error
            auto = ensure_installed_dev_worktree(
                assets, environ=environ, which=which, run=run
            )
        project = inspect(auto.home, which=which, run=run)
    return LaunchTarget(
        project=project,
        auto=auto,
        dev_prompt=_dev_prompt(project, auto, environ=environ, run=run),
    )


def _static_project(root: Path, assets: GuiAssets | None) -> GuiProject:
    commit = assets.source_commit if assets is not None else ""
    remote = assets.source_remote if assets is not None else ""
    verified = assets is not None and bool(assets.manifest)
    label = f"installed {commit[:12]}" if verified else "explicit unverified build"
    return GuiProject(
        home=root,
        worktree=root,
        branch=label,
        node="",
        npm="",
        node_version="",
        npm_version="",
        scripts={},
        installed=verified,
        source_commit=commit,
        source_remote=remote,
    )


def _builtin_assets() -> GuiAssets | None:
    root = installed_asset_root()
    if not (root / MANIFEST_NAME).is_file():
        if root.is_dir():
            raise GuiError(
                "GUI_ASSETS_MISSING",
                f"the installed GUI directory exists at {root} but its "
                f"{MANIFEST_NAME} manifest is missing; reinstall the wheel",
                {"asset_root": str(root)},
            )
        return None
    try:
        return load_assets(root, verify=True)
    except GuiAssetError as error:
        raise _asset_error(error) from error


def _explicit_assets(home: Path) -> GuiAssets:
    try:
        root = resolve_explicit_assets(home)
    except GuiAssetError as error:
        raise _asset_error(error) from error
    if (root / MANIFEST_NAME).is_file():
        try:
            return load_assets(root, verify=True)
        except GuiAssetError as error:
            raise _asset_error(error) from error
    return GuiAssets(root=root, manifest={})


def _asset_error(error: GuiAssetError) -> GuiError:
    return GuiError(error.code, str(error), dict(error.data))


def _is_installed_package() -> bool:
    parts = installed_asset_root().parent.parts
    return "site-packages" in parts or "dist-packages" in parts


def _reject_non_dev_home(home: Path, environ: Mapping[str, str]) -> None:
    normal = str(environ.get(NORMAL_HOME_ENV) or "").strip()
    if normal:
        normal_path = Path(normal).expanduser().resolve()
        if home == normal_path:
            raise GuiError(
                "GUI_DEV_HOME_INVALID",
                "HYPRIAL_DEV_GUI_HOME/--gui-home points at the normal GUI home "
                "from HYPRIAL_GUI_HOME; the two variables are independent and "
                "must not be borrowed from one another",
                {"gui_home": str(home), "normal_home": str(normal_path)},
            )
    asset_root = installed_asset_root().resolve()
    if home == asset_root or home == asset_root.parent or (
        (home / MANIFEST_NAME).is_file()
    ):
        raise GuiError(
            "GUI_DEV_HOME_INVALID",
            f"{home} is an installed GUI asset directory, not a development "
            "source worktree; pass a Git worktree containing gui/package.json",
            {"gui_home": str(home), "asset_root": str(asset_root)},
        )


def _dev_prompt(
    project: GuiProject,
    auto: AutoGuiWorktree | None,
    *,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> str:
    worktree = str(auto.path if auto is not None else project.worktree)
    head = auto.base if auto is not None else _git(
        project.worktree, ["rev-parse", "HEAD"], environ, run
    )
    raw_remote = (
        auto.remote
        if auto is not None
        else _git(project.worktree, ["config", "--get", "remote.origin.url"],
                  environ, run)
    )
    dirty = (
        auto.dirty
        if auto is not None
        else bool(_git(project.worktree, ["status", "--porcelain"], environ, run))
    )
    source = redact_remote(raw_remote or "") or "origin"
    if raw_remote and source != raw_remote:
        source = f"{source} (credentials redacted)"
    branch = project.branch
    baseline = (
        f"installed sourceCommit {head}"
        if auto is not None and auto.managed
        else f"current HEAD {head}"
    )
    quoted = shlex.quote(worktree)
    return "\n".join(
        [
            "Development source (this CLI does not auto-update it):",
            f"  path    : {worktree}",
            f"  HEAD    : {head or '(unknown)'}",
            f"  branch  : {branch}",
            f"  remote  : {source}",
            f"  baseline: {baseline}",
            f"  dirty   : {'yes (preserved)' if dirty else 'no'}",
            "  update explicitly, after checking the result yourself:",
            f"    git -C {quoted} fetch origin",
            f"    git -C {quoted} status",
            f"    git -C {quoted} rebase origin/dev",
        ]
    )


def _git(
    root: Path,
    args: list[str],
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> str:
    try:
        completed = run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            env=dict(environ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if getattr(completed, "returncode", 1) != 0:
        return ""
    return (getattr(completed, "stdout", "") or "").strip()


def _configured(environ: Mapping[str, str], name: str) -> bool:
    return bool(str(environ.get(name) or "").strip())


def source_environment() -> Mapping[str, str]:
    """The real process environment, isolated here for tests to monkeypatch."""
    return os.environ
