"""T3 D5: Codex boot products are admitted without a quarantine step.

Codex writes a refreshable model catalog, app/tools caches, a remote-plugin
receipt and (on Linux) a fourth arg0 dispatch alias after every boot.  The
native-root inventory treated them as unprojected personality items, so the
next start was refused until an operator quarantined them.  They are now
allowlisted while the personality negative arms stay refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.codex import (
    CodexAgentHomeError,
    prepare_codex_runtime_roots,
)


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    projection = tmp_path / "projection"
    projection.mkdir(mode=0o700)
    config = projection / "config.toml"
    config.write_text('model = "target-model"\n', encoding="utf-8")
    config.chmod(0o600)
    native = tmp_path / "native"
    native.mkdir(mode=0o700)
    sessions = tmp_path / "sessions"
    sessions.mkdir(mode=0o700)
    return projection, native, sessions


def _codex_bin(tmp_path: Path) -> Path:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary.resolve()


def _prepare(
    projection: Path, native: Path, sessions: Path, codex: Path
) -> None:
    prepare_codex_runtime_roots(
        projection_root=projection,
        native_root=native,
        session_root=sessions,
        codex_executable=codex,
    )


def test_boot_products_pass_on_the_second_prepare(tmp_path: Path) -> None:
    projection, native, sessions = _roots(tmp_path)
    codex = _codex_bin(tmp_path)
    _prepare(projection, native, sessions, codex)  # first start
    # Codex boot products (shapes from the 2026-10-16 target reading).
    (native / "models_cache.json").write_text('{"models": []}', encoding="utf-8")
    (native / ".sqlite-maintenance.lock").touch()
    control = native / "app-server-control"
    control.mkdir()
    (control / "app-server-startup.lock").touch()
    macos_arg0 = native / "tmp" / "arg0-macos" / "codex-arg0AbC123"
    macos_arg0.mkdir(parents=True)
    (macos_arg0 / ".lock").touch()
    tools = native / "cache" / "codex_apps_tools"
    tools.mkdir(parents=True)
    (tools / "tools.json").write_text("{}", encoding="utf-8")
    plugins = native / "plugins" / "cache" / "openai-curated-remote"
    plugins.mkdir(parents=True)
    (plugins / ".codex-remote-plugin-install.json").write_text(
        "{}", encoding="utf-8"
    )
    arg0 = native / "tmp" / "arg0" / "codex-arg0AbC123"
    arg0.mkdir(parents=True)
    (arg0 / ".lock").write_text("", encoding="utf-8")
    for alias in (
        "apply_patch",
        "applypatch",
        "codex-execve-wrapper",
        "codex-linux-sandbox",
    ):
        (arg0 / alias).symlink_to(codex)
    # Judgement: boot-after-start passes with no quarantine.
    _prepare(projection, native, sessions, codex)


def test_personality_drift_is_still_refused(tmp_path: Path) -> None:
    projection, native, sessions = _roots(tmp_path)
    codex = _codex_bin(tmp_path)
    _prepare(projection, native, sessions, codex)
    (native / "HOST_PERSONALITY_BAIT.md").write_text(
        "must not load\n", encoding="utf-8"
    )
    with pytest.raises(CodexAgentHomeError, match="unprojected personality"):
        _prepare(projection, native, sessions, codex)


def test_personality_bait_inside_arg0_is_still_refused(tmp_path: Path) -> None:
    projection, native, sessions = _roots(tmp_path)
    codex = _codex_bin(tmp_path)
    arg0 = native / "tmp" / "arg0" / "codex-arg0AbC123"
    arg0.mkdir(parents=True)
    (arg0 / ".lock").write_text("", encoding="utf-8")
    # The arg0 directory is not a blanket allowlist: it admits only the four
    # dispatch aliases and the .lock file, so a regular item there is still an
    # unprojected personality item.
    (arg0 / "HOST_PERSONALITY_BAIT.md").write_text(
        "must not load\n", encoding="utf-8"
    )
    with pytest.raises(CodexAgentHomeError, match="unprojected personality"):
        _prepare(projection, native, sessions, codex)


def test_foreign_arg0_alias_is_still_refused(tmp_path: Path) -> None:
    projection, native, sessions = _roots(tmp_path)
    codex = _codex_bin(tmp_path)
    arg0 = native / "tmp" / "arg0" / "codex-arg0AbC123"
    arg0.mkdir(parents=True)
    (arg0 / ".lock").write_text("", encoding="utf-8")
    foreign = tmp_path / "evil"
    foreign.write_text("#!/bin/sh\n", encoding="utf-8")
    foreign.chmod(0o755)
    (arg0 / "applypatch").symlink_to(foreign)
    with pytest.raises(CodexAgentHomeError, match="unauthorized"):
        _prepare(projection, native, sessions, codex)


@pytest.mark.parametrize(
    "relative",
    [
        "app-server-control/unapproved.lock",
        "tmp/arg0-macos/unapproved.lock",
        "tmp/arg0-macos/nested/unapproved.lock",
        "tmp/arg0-macos/codex-arg0AbC12/.lock",
    ],
)
def test_runtime_lock_allowlist_is_exact(tmp_path: Path, relative: str) -> None:
    projection, native, sessions = _roots(tmp_path)
    codex = _codex_bin(tmp_path)
    candidate = native / relative
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.touch()
    with pytest.raises(CodexAgentHomeError, match="unprojected personality"):
        _prepare(projection, native, sessions, codex)


def test_maintenance_lock_symlink_is_refused(tmp_path: Path) -> None:
    projection, native, sessions = _roots(tmp_path)
    codex = _codex_bin(tmp_path)
    foreign = tmp_path / "foreign-lock"
    foreign.touch()
    (native / ".sqlite-maintenance.lock").symlink_to(foreign)
    with pytest.raises(CodexAgentHomeError, match="unauthorized symbolic link"):
        _prepare(projection, native, sessions, codex)


def test_reparse_sessions_link_is_admitted_and_rogue_junction_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stat
    from types import SimpleNamespace
    from hyprial.daemon.impl.harnesses.codex import native_env

    projection, native, sessions = _roots(tmp_path)
    codex = _codex_bin(tmp_path)
    _prepare(projection, native, sessions, codex)
    (sessions / "2026" / "11" / "rollout.jsonl").parent.mkdir(parents=True)
    (sessions / "2026" / "11" / "rollout.jsonl").write_text("{}\n", encoding="utf-8")

    sessions_entry = native / "sessions"
    rogue = native / "rogue-junction"
    rogue.mkdir()
    real_scandir = native_env.os.scandir

    class _EntryProxy:
        def __init__(self, inner) -> None:
            self._inner = inner
            self.path = inner.path
            self.name = inner.name

        def stat(self, *, follow_symlinks: bool = True):
            meta = self._inner.stat(follow_symlinks=follow_symlinks)
            if not follow_symlinks and Path(self.path) in {sessions_entry, rogue}:
                return SimpleNamespace(
                    st_mode=stat.S_IFDIR | 0o700,
                    st_file_attributes=0x400,
                )
            return meta

    class _ScandirProxy:
        def __init__(self, inner) -> None:
            self._inner = inner

        def __enter__(self):
            return (_EntryProxy(item) for item in self._inner.__enter__())

        def __exit__(self, exc_type, exc, tb):
            return self._inner.__exit__(exc_type, exc, tb)

        def __iter__(self):
            return (_EntryProxy(item) for item in self._inner)

        def close(self) -> None:
            self._inner.close()

    def fake_scandir(directory):
        if isinstance(directory, (str, native_env.os.PathLike)) and Path(directory) == native:
            return _ScandirProxy(real_scandir(directory))
        return real_scandir(directory)

    monkeypatch.setattr(native_env.os, "scandir", fake_scandir)
    with pytest.raises(CodexAgentHomeError, match="unauthorized symbolic link"):
        _prepare(projection, native, sessions, codex)
    rogue.rmdir()
    _prepare(projection, native, sessions, codex)
    monkeypatch.undo()
