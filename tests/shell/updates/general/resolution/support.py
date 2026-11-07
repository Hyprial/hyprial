"""Tests for tag-only remote resolution and the public upgrade contract."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


import hyprial.daemon.impl.updates as updates
from urllib.parse import urlsplit
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

_FORGEJO_HOST = urlsplit(updates.FORGEJO_GIT_URL).hostname
_OLD_VERSION = "0.4.0.dev0"
_NEW_VERSION = "0.5.0rc1"
_RENAMED_GIT_URL = (
    f"ssh://git@{_FORGEJO_HOST}/HyprialOS/harness-bridge.git"
)
_OFFICIAL_GIT_URL = "https://github.com/Hyprial/hyprial.git"
_OLD_GIT_URL = (
    f"ssh://git@{_FORGEJO_HOST}/HyprialOS/harness-bridge-py.git"
)


def _git(*args: str | Path, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *(str(arg) for arg in args)],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "hyprial tests",
            "GIT_AUTHOR_EMAIL": "hyprial-tests@example.invalid",
            "GIT_COMMITTER_NAME": "hyprial tests",
            "GIT_COMMITTER_EMAIL": "hyprial-tests@example.invalid",
        },
    )
    return completed.stdout


def _write_version(seed: Path, version: str) -> None:
    (seed / "pyproject.toml").write_text(
        f'[project]\nname = "hyprial"\nversion = "{version}"\n', encoding="utf-8"
    )


class RemoteFixture:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True)
        self.bare = root / "remote.git"
        self.seed = root / "seed"
        _git("init", "--bare", "--initial-branch=dev", self.bare, cwd=root)
        _git("init", "--initial-branch=dev", self.seed, cwd=root)
        _git("remote", "add", "origin", self.bare.as_uri(), cwd=self.seed)

        _write_version(self.seed, _OLD_VERSION)
        _git("add", "pyproject.toml", cwd=self.seed)
        _git("commit", "-m", "old", cwd=self.seed)
        self.old_commit = _git("rev-parse", "HEAD", cwd=self.seed).strip()
        _git("tag", "v0.3.0", self.old_commit, cwd=self.seed)
        _git("tag", "emergency", self.old_commit, cwd=self.seed)

        _write_version(self.seed, _NEW_VERSION)
        _git("commit", "-am", "tagged release candidate", cwd=self.seed)
        self.tagged_commit = _git("rev-parse", "HEAD", cwd=self.seed).strip()
        _git("tag", "-a", "-m", "release", "v0.4.0", self.tagged_commit, cwd=self.seed)
        _git("tag", "v0.5.0-rc.1", self.tagged_commit, cwd=self.seed)

        # A moving branch is deliberately newer than every tag.  Tag-only
        # resolution must never see this commit as an upgrade target.
        _write_version(self.seed, "99.0.0.dev1")
        _git("commit", "-am", "untagged dev head", cwd=self.seed)
        self.dev_commit = _git("rev-parse", "HEAD", cwd=self.seed).strip()
        # The three movable track tags: none is a vX.Y.Z version tag, so the
        # legacy latest-tag probe must ignore all of them, while an explicit
        # updateTrack resolves exactly its own tag's commit.
        _git("tag", "stable", self.old_commit, cwd=self.seed)
        _git("tag", "nightly", self.tagged_commit, cwd=self.seed)
        _git("tag", "internal", self.dev_commit, cwd=self.seed)
        # The dev branch and the dev track tag share a name; spell the branch
        # ref out or the push refspec is ambiguous.
        _git("push", "origin", "refs/heads/dev", "--tags", cwd=self.seed)
        self.url = self.bare.as_uri()


@pytest.fixture
def remote(tmp_path: Path) -> RemoteFixture:
    return RemoteFixture(tmp_path / "remote-root")


def _installation(
    *,
    commit: str | None = None,
    version: str | None = _OLD_VERSION,
    requested: str | None = "dev",
    url: str | None = None,
) -> updates.Installation:
    return updates.Installation(
        version=version, commit=commit, requested_revision=requested, url=url
    )


def _isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "home").mkdir(parents=True, exist_ok=True, mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))


class _ProbeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


# --- hyprial daemon upgrade ------------------------------------------------------------ #


class _UpgradeHarness:
    """Upgrade-path rig with production-shaped isolation guards.

    The fixture's bare repo is a stand-in REMOTE, so the *recorded install
    source* must be remote-shaped too (a production install never carries a
    ``file://`` PEP 610 url -- that spelling now means "working tree" and
    guard 2 refuses it).  Resolution stays hermetic by delegating
    ``resolve_remote`` to the fixture repo, and guard 3's positive shape
    (this process inside the tool dir uv would write) comes from
    ``UV_TOOL_DIR``.
    """

    #: Remote-shaped install metadata url (stands for the git source).
    source_url = _OFFICIAL_GIT_URL

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remote: RemoteFixture
    ) -> None:
        _isolate_home(monkeypatch, tmp_path)
        self.calls: list[list[str]] = []
        self.resolve_timeouts: list[float] = []
        self._real_run = subprocess.run
        self.installation = _installation(
            commit=remote.old_commit, requested="dev", url=self.source_url
        )
        self.resolve_url = remote.url
        real_resolve_remote = updates.resolve_remote
        #: Like real uv, a successful install rewrites the PEP 610 receipt to
        #: the commit it fetched; a test can pin a different one to model uv
        #: fetching something other than what was resolved.
        self.last_resolution: updates.RemoteResolution | None = None
        self.fetched_commit: str | None = None
        self.exported_constraints: list[tuple[str, str]] = []

        def resolve(
            url: str,
            tag: str | None = None,
            timeout: float = updates.LS_REMOTE_TIMEOUT,
        ) -> updates.RemoteResolution:
            self.resolve_timeouts.append(timeout)
            self.last_resolution = real_resolve_remote(
                self.resolve_url, tag=tag, timeout=timeout
            )
            return self.last_resolution

        monkeypatch.setattr(updates, "resolve_remote", resolve)
        def export_constraints(
            url: str,
            commit: str,
            destination: Path,
            *,
            runner: object = None,
        ) -> Path:
            self.exported_constraints.append((url, commit))
            destination.write_text("anyio==4.15.1\n", encoding="utf-8")
            return destination

        monkeypatch.setattr(updates, "export_locked_constraints", export_constraints)
        monkeypatch.setenv("UV_TOOL_DIR", str(Path(sys.prefix).resolve().parent))
        monkeypatch.setattr(updates, "read_installation", lambda: self.installation)
        monkeypatch.setattr(_cli_subprocess, "run", self._run)

    def _run(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[0] != "uv":
            return self._real_run(argv, **kwargs)  # type: ignore[arg-type]
        recorded = list(argv)
        if "--constraints" in recorded:
            index = recorded.index("--constraints")
            del recorded[index : index + 2]
        self.calls.append(recorded)
        fetched = self.fetched_commit or (
            self.last_resolution.commit if self.last_resolution is not None else None
        )
        if fetched is not None:
            requested = argv[-1].rsplit("@", 1)[-1]
            self.installation = dataclasses.replace(
                self.installation, commit=fetched, requested_revision=requested
            )
        return subprocess.CompletedProcess(argv, 0, stdout="installed hyprial\n", stderr="")


def _write_track(home: Path, track: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"version": 1, "updateTrack": track}) + "\n", encoding="utf-8"
    )


# devusr-dev, 2026-09-26: `hyprial daemon upgrade --tag internal` moved b6cd834 ->
# e9dec894, reported upgraded=true "resolved tag unchanged; restart not
# needed", and left the daemon on the old code.  The restart decision compared
# the tag NAME (PEP 610 requested_revision) while uv reinstalled a new commit.


def _record_restart_decision(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record that the restart path was entered (it probes the running daemon)."""

    entered: list[str] = []

    def probe(version: str) -> None:
        entered.append(version)
        return None  # no daemon in the rig => "daemon was not running"

    monkeypatch.setattr(_cli_get_services(), "_running_daemon_before_upgrade", probe)
    return entered
