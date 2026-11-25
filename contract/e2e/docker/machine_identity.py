"""Give one test container a persistent OS identity before device provisioning.

The image removes its shared template. Its single entrypoint initializes the
container's writable layer once; normal stop/start keeps the same file.
Product machine binding and copied-home detection remain unchanged.
"""

from pathlib import Path
import os
import re
import stat
from uuid import uuid4


def _read(path: Path) -> bytes | None:
    try:
        if path.is_symlink():
            raise ValueError("fixture machine-id must not be a symlink")
        mode = path.stat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise ValueError("fixture machine-id must be a regular file")
    with path.open("rb") as stream:
        return stream.read(129)


def ensure_machine_id(path: Path) -> str:
    """Create a nonempty OS machine-id, preserving every valid existing byte."""

    current = _read(path)
    value = current.strip() if current is not None else b""
    if value:
        if len(current) > 128 or re.fullmatch(rb"[0-9a-f]{32}", value) is None or value == b"0" * 32:
            raise ValueError("fixture machine-id must be a nonzero 32-character lowercase hex id")
        return value.decode("ascii")
    if current is not None and len(current) > 128:
        raise ValueError("fixture machine-id is oversized")
    generated = uuid4().hex
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o644)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write((generated + "\n").encode("ascii"))
        stream.flush()
        os.fsync(stream.fileno())
    return generated
