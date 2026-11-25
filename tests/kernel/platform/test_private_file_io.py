"""Cross-platform private file I/O contracts shared by credential stores."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hyprial.kernel import (
    PrivateAccessUnsupported,
    append_private_file,
    open_private_file,
    read_private_file,
    secure_private_directory,
    write_private_file_atomic,
)


def test_nested_private_write_and_read(tmp_path: Path) -> None:
    root = secure_private_directory(tmp_path / "state" / "nested", create=True)
    path = root / "record.json"
    created = write_private_file_atomic(path, b'{"ok":true}\n')
    assert stat.S_IMODE(created.st_mode) == 0o600
    data, metadata = read_private_file(path)
    assert data == b'{"ok":true}\n'
    assert metadata.st_nlink == 1


def test_exclusive_write_never_replaces_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    write_private_file_atomic(path, b"original")
    with pytest.raises(FileExistsError):
        write_private_file_atomic(path, b"replacement", exclusive=True)
    assert read_private_file(path)[0] == b"original"


def test_append_failure_rolls_back_only_the_current_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "receipts.jsonl"
    append_private_file(path, b"first\n")
    original = read_private_file(path)[0]
    monkeypatch.setattr(os, "fsync", lambda _descriptor: (_ for _ in ()).throw(OSError("flush")))
    with pytest.raises(OSError, match="flush"):
        append_private_file(path, b"second\n")
    assert read_private_file(path)[0] == original
    monkeypatch.undo()
    append_private_file(path, b"second\n")
    assert read_private_file(path)[0] == original + b"second\n"


def test_symlink_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    write_private_file_atomic(target, b"secret")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(PrivateAccessUnsupported):
        read_private_file(link)


def test_open_private_file_checks_identity_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.json"
    write_private_file_atomic(path, b"private")
    monkeypatch.setattr(
        os, "read", lambda *_args: (_ for _ in ()).throw(AssertionError("read"))
    )
    with open_private_file(path) as (descriptor, metadata):
        assert descriptor >= 0
        assert metadata.st_nlink == 1
        assert os.fstat(descriptor).st_ino == metadata.st_ino


class _ObservingOs:
    """Forward to ``os`` and record which temp descriptors are open at publish.

    Windows raises a sharing violation (``WinError 32``) when a file is linked
    or replaced while a descriptor on it is still open, so the publish call must
    observe a closed descriptor.
    """

    def __init__(self, real) -> None:
        self._real = real
        self.opened: dict[int, str] = {}
        self.closed: set[int] = set()
        self.publish_live_descriptors: list[set[int]] = []

    def open(self, *args, **kwargs):
        descriptor = self._real.open(*args, **kwargs)
        self.opened[descriptor] = os.fspath(args[0])
        return descriptor

    def close(self, descriptor):
        self.closed.add(descriptor)
        return self._real.close(descriptor)

    def _record(self, source) -> None:
        source = os.fspath(source)
        self.publish_live_descriptors.append(
            {
                descriptor
                for descriptor, opened in self.opened.items()
                if descriptor not in self.closed and opened == source
            }
        )

    def link(self, source, target, **kwargs):
        self._record(source)
        return self._real.link(source, target, **kwargs)

    def replace(self, source, target):
        self._record(source)
        return self._real.replace(source, target)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_publish_runs_after_the_temporary_descriptor_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.kernel.impl.manysys.fs import private_access

    observer = _ObservingOs(os)
    monkeypatch.setattr(private_access, "os", observer)
    path = tmp_path / "state" / "record.json"

    write_private_file_atomic(path, b"exclusive")
    assert observer.publish_live_descriptors == [set()]  # os.link path

    write_private_file_atomic(path, b"replaced")
    assert observer.publish_live_descriptors[-1] == set()  # os.replace path

    assert read_private_file(path)[0] == b"replaced"
    assert observer.closed  # the descriptor was closed exactly once


def test_failed_publish_still_closes_the_descriptor_and_removes_the_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hyprial.kernel.impl.manysys.fs import private_access

    observer = _ObservingOs(os)

    def failing_replace(source, target):
        observer._record(source)
        raise OSError(32, "sharing violation")

    observer.replace = failing_replace  # type: ignore[method-assign]
    monkeypatch.setattr(private_access, "os", observer)
    path = tmp_path / "state" / "record.json"

    with pytest.raises(OSError, match="sharing violation"):
        write_private_file_atomic(path, b"new")

    assert observer.publish_live_descriptors == [set()]
    assert not list((path.parent).glob(f".{path.name}.*.tmp"))


class _WindowsLikeOs:
    """Windows-like ``os``: ``O_BINARY`` exists and ``os.pread`` does not."""

    O_BINARY = 0x8000

    def __init__(self, real) -> None:
        self._real = real
        self.open_flags: list[int] = []

    def open(self, path, flags, mode=0o777, **kwargs):
        self.open_flags.append(flags)
        return self._real.open(path, flags & ~self.O_BINARY, mode, **kwargs)

    def __getattr__(self, name):
        if name == "pread":
            raise AttributeError(name)
        return getattr(self._real, name)


def test_windows_private_io_requests_binary_mode_on_every_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LF must survive: without O_BINARY, Windows text mode turns it into CRLF."""

    from hyprial.kernel.impl.manysys.fs import private_access

    fakos = _WindowsLikeOs(os)
    monkeypatch.setattr(private_access, "os", fakos)
    path = tmp_path / "state" / "record.txt"

    write_private_file_atomic(path, b"line one\nline two\n")
    assert read_private_file(path)[0] == b"line one\nline two\n"
    append_private_file(path, b"line three\n")
    assert read_private_file(path)[0] == b"line one\nline two\nline three\n"

    assert fakos.open_flags  # at least create + append + read
    assert all(flags & fakos.O_BINARY for flags in fakos.open_flags)


def test_windows_append_works_without_os_pread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.pread` does not exist on Windows; the row-boundary check must still work."""

    from hyprial.kernel.impl.manysys.fs import private_access

    fakos = _WindowsLikeOs(os)
    monkeypatch.setattr(private_access, "os", fakos)
    path = tmp_path / "state" / "receipts.jsonl"

    append_private_file(path, b'{"row":1}')  # no trailing newline
    append_private_file(path, b'{"row":2}\n')

    assert read_private_file(path)[0] == b'{"row":1}\n{"row":2}\n'
