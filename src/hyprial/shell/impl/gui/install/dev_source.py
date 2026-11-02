"""Prepare the exact-source development worktree for an installed wheel.

The wheel manifest pins one full Git object id.  This module maintains a bare,
trusted source cache under the configured Hyprial home and a detached worktree
at exactly that object.  It only fetches the pinned object from the manifest's
recorded remote; it never substitutes the latest branch, resets, cleans,
stashes, rebases, force-checks-out, or overwrites an existing worktree.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from hyprial.kernel import configured_hyprial_home

from ..errors import GuiError
from ..worktree import (
    AutoGuiWorktree,
    _ensure_dependencies,
    _path_present,
    _require_tool,
    _run_command,
)
from .resources import GuiAssets, redact_remote, remote_has_credentials

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_INVALID_CODE = "GUI_DEV_SOURCE_INVALID"
_MISSING_CODE = "GUI_DEV_SOURCE_MISSING"
_CONFLICT_CODE = "GUI_DEV_SOURCE_CONFLICT"
_FAILED_CODE = "GUI_DEV_SOURCE_FAILED"


def managed_root(environ: Mapping[str, str] | None = None) -> Path:
    """The fixed managed location below the selected Hyprial home."""
    home, _source = configured_hyprial_home(environ)
    return home / "state" / "gui-dev"


def ensure_installed_dev_worktree(
    assets: GuiAssets,
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., Any] = subprocess.run,
) -> AutoGuiWorktree:
    """Fetch and materialize exactly ``assets.source_commit`` without updating."""
    environment = dict(os.environ if environ is None else environ)
    commit = assets.source_commit
    remote = assets.source_remote.strip()
    if not _COMMIT_RE.fullmatch(commit):
        raise GuiError(
            _INVALID_CODE,
            "the installed GUI manifest does not contain a full source commit",
            {"sourceCommit": commit},
        )
    if not remote or _has_credentials(remote):
        raise GuiError(
            _INVALID_CODE,
            "the installed GUI manifest's source remote is empty or contains "
            "credentials or query data; refusing to fetch",
            {"sourceRemote": redact_remote(remote)},
        )
    git = _require_tool(
        "git",
        which,
        "GUI_GIT_MISSING",
        "git is required for `hyprial gui dev`; install Git or pass an "
        "existing --gui-home worktree",
    )
    root = managed_root(environment)
    cache = root / "source.git"
    tree = root / "worktrees" / commit
    created = False
    _prepare_cache(git, cache, remote, environment, run)
    _ensure_object(git, cache, commit, remote, environment, run)

    if _path_present(tree):
        _validate_worktree(git, cache, tree, commit, environment, run)
    else:
        _guard_worktree_parent(tree, root)
        created = True
        _create_worktree(git, cache, tree, commit, environment, run)
        _validate_worktree(git, cache, tree, commit, environment, run)

    home = tree / "gui"
    install = _ensure_dependencies(
        home,
        source=cache,
        worktree=tree,
        environ=environment,
        which=which,
        run=run,
    )
    return AutoGuiWorktree(
        source=cache,
        path=tree,
        branch="(detached)",
        base=commit,
        home=home,
        created=created,
        install=install,
        managed=True,
        dirty=_dirty(git, tree, environment, run),
        remote=remote,
    )


def _prepare_cache(
    git: str,
    cache: Path,
    remote: str,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> None:
    if _path_present(cache):
        if cache.is_symlink() or not cache.is_dir():
            raise GuiError(
                _CONFLICT_CODE,
                f"the managed GUI source cache {cache} is not a directory; "
                "refusing to delete or replace it",
                {"cache": str(cache)},
            )
        bare = _git(
            git, None, ["--git-dir", str(cache), "rev-parse", "--is-bare-repository"],
            environ, run, allow_failure=True,
        )
        if bare != "true":
            raise GuiError(
                _CONFLICT_CODE,
                f"the managed GUI source cache {cache} is not a bare Git "
                "repository; refusing to reuse it",
                {"cache": str(cache)},
            )
        configured = _git(
            git, None,
            ["--git-dir", str(cache), "config", "--get", "remote.origin.url"],
            environ, run, allow_failure=True,
        )
        if configured != remote:
            expected = redact_remote(remote)
            actual = redact_remote(configured or "")
            raise GuiError(
                _CONFLICT_CODE,
                f"the managed GUI source cache {cache} uses a different remote "
                f"({actual!r}) than the installed version ({expected!r}); "
                "refusing to fetch from an arbitrary remote",
                {"cache": str(cache), "expectedRemote": expected,
                 "actualRemote": actual},
            )
        return
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise GuiError(
            _FAILED_CODE,
            f"cannot create the managed GUI source directory {cache.parent}: {error}",
            {"cache": str(cache)},
        ) from error
    _checked(
        [git, "init", "--bare", str(cache)],
        environ, run,
        message=f"cannot initialize the managed GUI source cache {cache}",
        code=_FAILED_CODE,
    )
    _checked(
        [git, "--git-dir", str(cache), "remote", "add", "origin", remote],
        environ, run,
        message=f"cannot record the trusted GUI source remote in {cache}",
        code=_FAILED_CODE,
        redact=(remote,),
    )


def _ensure_object(
    git: str,
    cache: Path,
    commit: str,
    remote: str,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> None:
    present = _git(
        git, None,
        ["--git-dir", str(cache), "cat-file", "-e", f"{commit}^{{commit}}"],
        environ, run, allow_failure=True,
    )
    if present is not None:
        return
    fetched = _run_command(
        [git, "--git-dir", str(cache), "fetch", "--no-tags", "origin", commit],
        environ=environ,
        run=run,
        timeout=120.0,
    )
    if fetched.returncode != 0:
        display = redact_remote(remote)
        detail = _diagnostic(fetched, remote)
        raise GuiError(
            _MISSING_CODE,
            f"the installed GUI needs source commit {commit}, but it is not in "
            f"the managed cache and fetching it from the recorded remote failed "
            f"(exit {fetched.returncode}). Check the network and access to "
            f"{display}; this command never substitutes a latest branch.",
            {"commit": commit, "remote": display, "cache": str(cache),
             "command": ["git", "--git-dir", str(cache), "fetch", "--no-tags",
                         "origin", commit],
             "returncode": fetched.returncode, "output": detail},
        )
    actual = _git(
        git, None,
        ["--git-dir", str(cache), "rev-parse", f"{commit}^{{commit}}"],
        environ, run, allow_failure=True,
    )
    if actual != commit:
        raise GuiError(
            _MISSING_CODE,
            f"fetching from {redact_remote(remote)} did not produce the exact "
            f"installed commit {commit}",
            {"commit": commit, "remote": redact_remote(remote), "actual": actual},
        )


def _create_worktree(
    git: str,
    cache: Path,
    tree: Path,
    commit: str,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> None:
    commands = (
        ["--git-dir", str(cache), "worktree", "add", "--detach", "--no-checkout",
         str(tree), commit],
        ["-C", str(tree), "sparse-checkout", "set", "--cone", "gui"],
        ["-C", str(tree), "checkout"],
    )
    for args in commands:
        completed = _run_command(
            [git, *args], environ=environ, run=run, timeout=120.0
        )
        if completed.returncode == 0:
            continue
        raise GuiError(
            _CONFLICT_CODE,
            f"cannot create the exact-SHA GUI worktree {tree} "
            f"(exit {completed.returncode}); the partial scene was preserved",
            {"path": str(tree), "commit": commit, "command": [git, *args],
             "returncode": completed.returncode, "output": _diagnostic(completed)},
        )


def _validate_worktree(
    git: str,
    cache: Path,
    tree: Path,
    commit: str,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> None:
    data = {"path": str(tree), "commit": commit, "cache": str(cache)}
    if tree.is_symlink() or not tree.is_dir():
        raise GuiError(
            _CONFLICT_CODE,
            f"{tree} is not a Git worktree; refusing to delete or reuse it",
            data,
        )
    toplevel = _git(
        git, tree, ["rev-parse", "--show-toplevel"], environ, run,
        allow_failure=True,
    )
    if toplevel is None or Path(toplevel).resolve() != tree.resolve():
        raise GuiError(
            _CONFLICT_CODE,
            f"{tree} is not a Git worktree root; refusing to delete or reuse it",
            data,
        )
    actual = _git(
        git, tree, ["rev-parse", "HEAD"], environ, run, allow_failure=True
    )
    if actual != commit:
        raise GuiError(
            _CONFLICT_CODE,
            f"{tree} is at {actual or '(unknown)'}, not the installed source "
            f"commit {commit}; refusing to switch, reset, or overwrite it",
            {**data, "actualCommit": actual},
        )
    sparse = _git(
        git, tree, ["sparse-checkout", "list"], environ, run, allow_failure=True
    )
    if sparse != "gui":
        raise GuiError(
            _CONFLICT_CODE,
            f"{tree} does not have the expected sparse checkout (gui); refusing "
            "to modify or reuse it",
            {**data, "sparseCheckout": sparse},
        )
    if not (tree / "gui" / "package.json").is_file():
        raise GuiError(
            _CONFLICT_CODE,
            f"{tree} has no gui/package.json at the installed commit; refusing "
            "to guess or reuse it",
            data,
        )


def _guard_worktree_parent(tree: Path, root: Path) -> None:
    parent = tree.parent
    if parent.is_symlink():
        raise GuiError(
            _CONFLICT_CODE,
            f"the managed GUI worktree parent {parent} is a symlink; refusing "
            "to write through it",
            {"path": str(tree)},
        )
    if _path_present(parent) and not parent.is_dir():
        raise GuiError(
            _CONFLICT_CODE,
            f"the managed GUI worktree parent {parent} is not a directory",
            {"path": str(tree), "root": str(root)},
        )
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise GuiError(
            _FAILED_CODE,
            f"cannot create the managed GUI worktree directory {parent}: {error}",
            {"path": str(tree)},
        ) from error


def _dirty(
    git: str,
    tree: Path,
    environ: Mapping[str, str],
    run: Callable[..., Any],
) -> bool:
    status = _git(
        git, tree, ["status", "--porcelain"], environ, run, allow_failure=True
    )
    return bool(status)


def _git(
    git: str,
    root: Path | None,
    args: list[str],
    environ: Mapping[str, str],
    run: Callable[..., Any],
    *,
    allow_failure: bool,
) -> str | None:
    command = [git]
    if root is not None:
        command += ["-C", str(root)]
    command += args
    completed = _run_command(command, environ=environ, run=run, timeout=60.0)
    if completed.returncode != 0:
        if allow_failure:
            return None
        raise GuiError(
            _FAILED_CODE,
            f"git {' '.join(args)} failed (exit {completed.returncode})",
            {"command": command, "returncode": completed.returncode,
             "output": _diagnostic(completed)},
        )
    return (completed.stdout or "").strip()


def _checked(
    command: list[str],
    environ: Mapping[str, str],
    run: Callable[..., Any],
    *,
    message: str,
    code: str,
    redact: tuple[str, ...] = (),
) -> None:
    completed = _run_command(command, environ=environ, run=run, timeout=120.0)
    if completed.returncode != 0:
        display = [
            redact_remote(part) if part in redact else part for part in command
        ]
        raise GuiError(
            code,
            f"{message} (exit {completed.returncode})",
            {"command": display, "returncode": completed.returncode,
             "output": _diagnostic(completed, *redact)},
        )


def _has_credentials(remote: str) -> bool:
    """Reject embedded secrets, while allowing a normal SSH user name.

    ``ssh://git@host/...`` is the repository's normal SSH transport; the ``git``
    account is an identity, not a credential.  HTTP(S) userinfo, explicit
    passwords, and query/fragment tokens are different: they must not be copied
    into logs or fetched implicitly.
    """
    return remote_has_credentials(remote)


def _diagnostic(completed: Any, *redact: str) -> str:
    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    text = "\n".join(part.strip() for part in (stdout, stderr) if part.strip())
    for secret in redact:
        if secret:
            text = text.replace(secret, redact_remote(secret))
    return text[-1200:]
