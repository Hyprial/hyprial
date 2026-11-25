"""Pack a bundle directory into one deterministic uncompressed PAX tar.

Decisions:

- Only regular files become members, named by their POSIX path relative to
  the bundle directory and ordered by UTF-8 bytes; uid, gid, names and mtime
  are zeroed so equal bundles give equal bytes (and equal ``sha256``).
- Payload modes come from the manifest; ``manifest.json`` is always 0600.  A
  file the manifest does not register, a symlink, a device or any other
  non-regular entry makes packing fail rather than be silently dropped.
- ``dest`` is created with ``O_EXCL``: an existing file is never overwritten.
  A failed pack removes the partial ``dest`` it created.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
from dataclasses import dataclass
from pathlib import Path

from hyprial.daemon.impl.transfer.archive.bundle import (
    MANIFEST_NAME,
    PAYLOAD_DIRNAME,
    BundleManifestError,
    load_manifest,
)

_MANIFEST_MODE = 0o600
_COPY = 1 << 20
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True, slots=True)
class PackedStream:
    path: Path
    total: int
    sha256: str


def _walk(root: Path) -> list[str]:
    names: list[str] = []
    stack = [(root, "")]
    while stack:
        directory, prefix = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                relative = prefix + entry.name
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    stack.append((Path(entry.path), relative + "/"))
                elif stat.S_ISREG(info.st_mode):
                    names.append(relative)
                else:
                    raise BundleManifestError(f"non-regular bundle entry: {relative!r}")
    return sorted(names, key=lambda name: name.encode("utf-8"))


def _modes(bundle: Path) -> dict[str, int]:
    manifest = load_manifest(bundle)
    modes = {f"{PAYLOAD_DIRNAME}/{entry.path}": entry.mode for entry in manifest.entries}
    modes[MANIFEST_NAME] = _MANIFEST_MODE
    return modes


def _open_regular(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise BundleManifestError(f"non-regular bundle entry: {path.name!r}")
    return fd


def _write_tar(bundle: Path, names: list[str], modes: dict[str, int], out: io.BufferedWriter) -> None:
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in names:
            fd = _open_regular(bundle / name)
            with os.fdopen(fd, "rb") as source:
                info = tarfile.TarInfo(name)
                info.size = os.fstat(source.fileno()).st_size
                info.mode = modes[name]
                archive.addfile(info, source)


def _digest(path: Path) -> tuple[int, str]:
    sha = hashlib.sha256()
    total = 0
    with open(path, "rb") as handle:
        while block := handle.read(_COPY):
            sha.update(block)
            total += len(block)
    return total, sha.hexdigest()


def pack_bundle(bundle_dir: os.PathLike[str] | str, dest: os.PathLike[str] | str) -> PackedStream:
    bundle = Path(bundle_dir)
    target = Path(dest)
    names = _walk(bundle)
    modes = _modes(bundle)
    unregistered = [name for name in names if name not in modes]
    if unregistered:
        raise BundleManifestError(f"bundle has unregistered files: {unregistered[:5]}")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            _write_tar(bundle, names, modes, out)
            out.flush()
            os.fsync(out.fileno())
        total, sha256 = _digest(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return PackedStream(path=target, total=total, sha256=sha256)
