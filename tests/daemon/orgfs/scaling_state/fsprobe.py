"""Count what a block of code does to the files under one directory.

Decisions (C6, 2026-11-25):

* The probe sits at the Python level (``io.open``/``builtins.open``, ``os.open``,
  ``os.read``/``os.write``, ``os.fsync``, ``os.replace``/``os.rename``,
  ``os.unlink``/``os.remove``, ``os.truncate``/``os.ftruncate``, ``os.scandir``/
  ``os.listdir``) so that any ``StateStore`` implementation is measured the
  same way, whether it uses ``open()``, ``Path.read_bytes`` or raw fds.
* Unlinks are recorded only when they succeed: a failed ``unlink`` of a missing
  file changes nothing on disk.
* Only paths under ``root`` are counted.  Counts are keyed by the path relative
  to ``root`` so a test can say "only these files were touched".
* Assertions elsewhere compare counts and bytes across space sizes; the probe
  never measures time.
* Memory-mapped I/O is not seen; an implementation that mmaps state files has
  to extend this probe.
"""

from __future__ import annotations

import builtins
from collections import Counter
from contextlib import contextmanager
import io
import os
from pathlib import Path
from typing import Any, Iterator


class _Counted:
    def __init__(self, probe: "FsProbe", raw: Any, rel: str) -> None:
        self._probe = probe
        self._raw = raw
        self._rel = rel

    def read(self, *args: Any) -> Any:
        data = self._raw.read(*args)
        self._probe.read_bytes[self._rel] += len(data)
        return data

    def readline(self, *args: Any) -> Any:
        data = self._raw.readline(*args)
        self._probe.read_bytes[self._rel] += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        count = self._raw.readinto(buffer)
        self._probe.read_bytes[self._rel] += count or 0
        return count

    def write(self, data: Any) -> int:
        count = self._raw.write(data)
        self._probe.write_bytes[self._rel] += len(data)
        return count

    def truncate(self, *args: Any) -> int:
        self._probe.truncates[self._rel] += 1
        return self._raw.truncate(*args)

    def __iter__(self) -> Iterator[Any]:
        for line in self._raw:
            self._probe.read_bytes[self._rel] += len(line)
            yield line

    def __enter__(self) -> "_Counted":
        self._raw.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._raw.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class FsProbe:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.reset()

    def reset(self) -> None:
        self.opens: Counter[str] = Counter()
        self.opens_for_read: set[str] = set()
        self.opens_for_write: set[str] = set()
        self.read_bytes: Counter[str] = Counter()
        self.write_bytes: Counter[str] = Counter()
        self.file_fsyncs: Counter[str] = Counter()
        self.dir_fsyncs: Counter[str] = Counter()
        self.renames: list[tuple[str, str]] = []
        self.unlinks: list[str] = []
        self.truncates: Counter[str] = Counter()
        self.listings: list[str] = []
        self._fd_paths: dict[int, str] = {}

    @property
    def fsyncs(self) -> int:
        return sum(self.file_fsyncs.values()) + sum(self.dir_fsyncs.values())

    @property
    def bytes_read(self) -> int:
        return sum(self.read_bytes.values())

    @property
    def bytes_written(self) -> int:
        return sum(self.write_bytes.values())

    @property
    def files_opened(self) -> set[str]:
        return set(self.opens)

    def summary(self) -> dict[str, Any]:
        return {
            "files_opened": len(self.opens),
            "bytes_read": self.bytes_read,
            "bytes_written": self.bytes_written,
            "fsyncs": self.fsyncs,
            "renames": len(self.renames),
            "unlinks": len(self.unlinks),
            "listings": len(self.listings),
        }

    def _rel(self, path: Any) -> str | None:
        try:
            resolved = Path(os.fsdecode(path))
        except TypeError:
            return None
        if not resolved.is_absolute():
            resolved = Path(os.getcwd()) / resolved
        try:
            return str(resolved.relative_to(self.root)) or "."
        except ValueError:
            return None

    @contextmanager
    def active(self) -> Iterator["FsProbe"]:
        probe = self
        real_open = io.open
        real_os_open = os.open
        real_read = os.read
        real_write = os.write
        real_fsync = os.fsync
        real_replace = os.replace
        real_rename = os.rename
        real_unlink = os.unlink
        real_remove = os.remove
        real_truncate = os.truncate
        real_ftruncate = os.ftruncate
        real_scandir = os.scandir
        real_listdir = os.listdir
        real_close = os.close

        def note_open(rel: str, mode: str) -> None:
            probe.opens[rel] += 1
            if "r" in mode or "+" in mode:
                probe.opens_for_read.add(rel)
            if any(flag in mode for flag in "wa+x"):
                probe.opens_for_write.add(rel)

        def counted_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            raw = real_open(file, mode, *args, **kwargs)
            if isinstance(file, int):
                return raw
            rel = probe._rel(file)
            if rel is None:
                return raw
            note_open(rel, mode)
            try:
                probe._fd_paths[raw.fileno()] = rel
            except (OSError, ValueError):
                pass
            return _Counted(probe, raw, rel)

        def counted_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            fd = real_os_open(path, flags, *args, **kwargs)
            rel = probe._rel(path)
            if rel is not None:
                accmode = flags & os.O_ACCMODE
                mode = "r" if accmode == os.O_RDONLY else "w"
                note_open(rel, mode)
                probe._fd_paths[fd] = rel
            return fd

        def counted_read(fd: int, length: int) -> bytes:
            data = real_read(fd, length)
            rel = probe._fd_paths.get(fd)
            if rel is not None:
                probe.read_bytes[rel] += len(data)
            return data

        def counted_write(fd: int, data: Any) -> int:
            count = real_write(fd, data)
            rel = probe._fd_paths.get(fd)
            if rel is not None:
                probe.write_bytes[rel] += count
            return count

        def counted_fsync(fd: Any) -> None:
            number = fd if isinstance(fd, int) else fd.fileno()
            rel = probe._fd_paths.get(number)
            if rel is not None:
                is_dir = (probe.root / rel).is_dir()
                (probe.dir_fsyncs if is_dir else probe.file_fsyncs)[rel] += 1
            real_fsync(fd)

        def counted_close(fd: int) -> None:
            probe._fd_paths.pop(fd, None)
            real_close(fd)

        def counted_replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
            rs, rd = probe._rel(src), probe._rel(dst)
            if rs is not None or rd is not None:
                probe.renames.append((rs or str(src), rd or str(dst)))
            real_replace(src, dst, *args, **kwargs)

        def counted_rename(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
            rs, rd = probe._rel(src), probe._rel(dst)
            if rs is not None or rd is not None:
                probe.renames.append((rs or str(src), rd or str(dst)))
            real_rename(src, dst, *args, **kwargs)

        def counted_unlink(path: Any, *args: Any, **kwargs: Any) -> None:
            real_unlink(path, *args, **kwargs)
            rel = probe._rel(path)
            if rel is not None:
                probe.unlinks.append(rel)

        def counted_remove(path: Any, *args: Any, **kwargs: Any) -> None:
            real_remove(path, *args, **kwargs)
            rel = probe._rel(path)
            if rel is not None:
                probe.unlinks.append(rel)

        def counted_truncate(path: Any, length: int) -> None:
            rel = probe._rel(path) if not isinstance(path, int) else probe._fd_paths.get(path)
            if rel is not None:
                probe.truncates[rel] += 1
            real_truncate(path, length)

        def counted_ftruncate(fd: int, length: int) -> None:
            rel = probe._fd_paths.get(fd)
            if rel is not None:
                probe.truncates[rel] += 1
            real_ftruncate(fd, length)

        def counted_scandir(path: Any = ".") -> Any:
            rel = probe._rel(path)
            if rel is not None:
                probe.listings.append(rel)
            return real_scandir(path)

        def counted_listdir(path: Any = ".") -> Any:
            rel = probe._rel(path)
            if rel is not None:
                probe.listings.append(rel)
            return real_listdir(path)

        patches = [
            (builtins, "open", counted_open),
            (io, "open", counted_open),
            (os, "open", counted_os_open),
            (os, "read", counted_read),
            (os, "write", counted_write),
            (os, "fsync", counted_fsync),
            (os, "close", counted_close),
            (os, "replace", counted_replace),
            (os, "rename", counted_rename),
            (os, "unlink", counted_unlink),
            (os, "remove", counted_remove),
            (os, "truncate", counted_truncate),
            (os, "ftruncate", counted_ftruncate),
            (os, "scandir", counted_scandir),
            (os, "listdir", counted_listdir),
        ]
        originals = [(target, name, getattr(target, name)) for target, name, _ in patches]
        for target, name, replacement in patches:
            setattr(target, name, replacement)
        try:
            yield self
        finally:
            for target, name, original in originals:
                setattr(target, name, original)


@contextmanager
def fsync_disabled() -> Iterator[None]:
    """Skip fsync while populating a fixture space; never inside a measurement."""

    real = os.fsync
    os.fsync = lambda fd: None
    try:
        yield
    finally:
        os.fsync = real
