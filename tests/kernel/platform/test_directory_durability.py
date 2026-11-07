"""``kernel.fsync_directory``: directory durability decided by capability, not platform."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from hyprial.kernel import fsync_directory
from hyprial.kernel.impl.platform.filesystem import durability


def test_flushes_a_directory_that_can_be_opened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = os.stat(tmp_path)
    flushed: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        if os.path.samestat(os.fstat(descriptor), target):
            flushed.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(durability.os, "fsync", recording_fsync)
    assert fsync_directory(tmp_path) is True
    assert len(flushed) == 1


def test_a_directory_the_os_cannot_open_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Windows CRT refuses ``os.open(dir)`` with EACCES (doctor --fix, #1902)."""

    real_open = os.open
    real_fsync = os.fsync
    flushed: list[int] = []

    def refuse(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if Path(os.fspath(path)) == tmp_path:
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)

    def recording_fsync(descriptor: int) -> None:
        flushed.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(durability.os, "open", refuse)
    monkeypatch.setattr(durability.os, "fsync", recording_fsync)
    assert fsync_directory(tmp_path) is False
    assert flushed == []


def test_a_failing_flush_propagates_and_closes_the_descriptor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    closed: list[int] = []
    real_close = os.close

    target = os.stat(tmp_path)
    real_fsync = os.fsync

    def failing_fsync(descriptor: int) -> None:
        if os.path.samestat(os.fstat(descriptor), target):
            raise OSError(errno.EIO, "I/O error")
        real_fsync(descriptor)

    def recording_close(descriptor: int) -> None:
        if os.path.samestat(os.fstat(descriptor), target):
            closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(durability.os, "fsync", failing_fsync)
    monkeypatch.setattr(durability.os, "close", recording_close)
    with pytest.raises(OSError) as raised:
        fsync_directory(tmp_path)
    assert raised.value.errno == errno.EIO
    assert len(closed) == 1


def test_a_missing_directory_is_an_error_not_a_skip(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        fsync_directory(tmp_path / "absent")
