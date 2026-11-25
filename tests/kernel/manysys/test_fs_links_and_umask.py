# Decision record (test_fs_links_and_umask, W1-MANYSYS 2026-11-21):
# - Covers apply_private_umask on POSIX and simulated Windows, plus real POSIX
#   directory symlinks and simulated Windows _winapi.CreateJunction / os.rmdir
#   reparse handling without deleting target contents or non-link directories.
# - Module-scoped SimpleNamespace proxies isolate simulated os/sys patches so
#   global os/shutil teardown in autouse fixtures is never affected.
"""Tests for ``apply_private_umask`` and directory link/junction primitives."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import sys
import types

import pytest

from hyprial.kernel import (
    apply_private_umask,
    is_directory_link,
    link_directory,
    unlink_directory_link,
)
from hyprial.kernel.impl.manysys.fs import account, links


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask round-trip")
def test_apply_private_umask_updates_and_returns_previous_mask_on_posix() -> None:
    original = os.umask(0o022)
    try:
        previous = apply_private_umask(0o077)
        assert previous == 0o022
        observed = os.umask(original)
        assert observed == 0o077
    finally:
        os.umask(original)


def test_apply_private_umask_is_noop_zero_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[int] = []
    monkeypatch.setattr(
        account,
        "os",
        types.SimpleNamespace(name="nt", umask=lambda mask: called.append(mask) or 0o022),
    )
    monkeypatch.setattr(account, "sys", types.SimpleNamespace(platform="win32"))

    assert apply_private_umask() == 0
    assert apply_private_umask(0o027) == 0
    assert called == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory symlink behavior")
def test_posix_directory_link_lifecycle_preserves_target_contents(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target-dir"
    target.mkdir()
    nested = target / "payload.txt"
    nested.write_text("keep-me", encoding="utf-8")
    link = tmp_path / "linked-dir"

    assert is_directory_link(link) is False
    assert is_directory_link(target) is False

    link_directory(target, link)
    assert is_directory_link(link) is True
    assert (link / "payload.txt").read_text(encoding="utf-8") == "keep-me"

    unlink_directory_link(link)
    assert not link.exists()
    assert is_directory_link(link) is False
    assert nested.read_text(encoding="utf-8") == "keep-me"


@pytest.mark.skipif(os.name == "nt", reason="POSIX dangling directory symlink")
def test_posix_dangling_directory_link_is_detected_and_unlinked(
    tmp_path: Path,
) -> None:
    target = tmp_path / "vanished"
    target.mkdir()
    link = tmp_path / "dangling"
    link_directory(target, link)
    target.rmdir()

    assert is_directory_link(link) is True
    unlink_directory_link(link)
    assert is_directory_link(link) is False


def test_unlink_directory_link_refuses_real_directory_and_regular_file(
    tmp_path: Path,
) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    regular = tmp_path / "file.txt"
    regular.write_text("x", encoding="utf-8")

    with pytest.raises(OSError) as dir_err:
        unlink_directory_link(real_dir)
    assert dir_err.value.errno == errno.EINVAL
    assert real_dir.is_dir()

    with pytest.raises(OSError) as file_err:
        unlink_directory_link(regular)
    assert file_err.value.errno == errno.EINVAL
    assert regular.is_file()


def test_windows_junction_lifecycle_uses_winapi_and_rmdir_without_touching_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    payload = target / "data.txt"
    payload.write_text("intact", encoding="utf-8")
    junction = tmp_path / "junction"
    created_junctions: list[tuple[str, str]] = []
    rmdir_calls: list[str] = []
    unlink_calls: list[str] = []
    active_junctions: set[str] = set()

    fake_winapi = types.ModuleType("_winapi")

    def create_junction(src: str, dst: str) -> None:
        created_junctions.append((src, dst))
        Path(dst).mkdir()
        active_junctions.add(os.fspath(dst))

    fake_winapi.CreateJunction = create_junction  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_winapi", fake_winapi)

    def fake_lstat(path: Path | str):
        raw = os.fspath(path)
        if raw in active_junctions:
            return types.SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o777,
                st_file_attributes=0x400,
            )
        return os.lstat(path)

    def fake_rmdir(path: Path | str) -> None:
        raw = os.fspath(path)
        rmdir_calls.append(raw)
        active_junctions.discard(raw)
        os.rmdir(path)

    fake_os = types.SimpleNamespace(
        name="nt",
        fspath=os.fspath,
        lstat=fake_lstat,
        rmdir=fake_rmdir,
        unlink=lambda path: unlink_calls.append(os.fspath(path)),
    )
    monkeypatch.setattr(links, "os", fake_os)
    monkeypatch.setattr(links, "sys", types.SimpleNamespace(platform="win32"))

    link_directory(target, junction)
    assert created_junctions == [(str(target), str(junction))]
    assert is_directory_link(junction) is True
    assert is_directory_link(target) is False

    with pytest.raises(OSError) as refused:
        unlink_directory_link(target)
    assert refused.value.errno == errno.EINVAL
    assert rmdir_calls == []

    unlink_directory_link(junction)
    assert rmdir_calls == [str(junction)]
    assert unlink_calls == []
    assert not junction.exists()
    assert payload.read_text(encoding="utf-8") == "intact"
