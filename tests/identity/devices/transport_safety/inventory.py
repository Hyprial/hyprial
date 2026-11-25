"""Metadata/content fingerprints without opening special files or symlinks."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


def inventory(root: Path) -> dict[str, tuple[object, ...]]:
    result = {}
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        value = (
            stat.S_IFMT(metadata.st_mode),
            stat.S_IMODE(metadata.st_mode),
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_nlink,
            metadata.st_ino,
        )
        if stat.S_ISREG(metadata.st_mode):
            value += (hashlib.sha256(path.read_bytes()).hexdigest(),)
        elif stat.S_ISLNK(metadata.st_mode):
            value += (os.readlink(path),)
        result[str(path.relative_to(root))] = value
    return result
