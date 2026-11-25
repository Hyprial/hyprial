"""Shared helpers for the slice 4a stream (bundle tar) tests.

Decisions pinned by these tests (docs/design/agent-teleport-protocol.md §1):

- The public surface is ``hyprial.daemon.impl.teleport.stream`` exporting
  ``PackedStream``, ``pack_bundle`` and ``unpack_stream``, imported lazily via
  ``stream()``.
- ``pack_bundle(bundle_dir, dest)`` writes the tar to the file path ``dest``
  (its parent exists) and returns ``PackedStream(path, total, sha256)`` where
  ``path == Path(dest)``, ``total`` is the file size and ``sha256`` the
  lowercase hex digest of the file bytes.
- Members are only regular files (no directory entries), named by their
  POSIX path relative to ``bundle_dir`` without a ``./`` prefix, ordered by the
  UTF-8 bytes of that name.  Payload files take the mode recorded in the
  bundle manifest; ``manifest.json`` itself is not a manifest entry and is
  always written with mode ``0o600``.
- ``unpack_stream(path, dest, *, total)`` creates ``dest`` (which must not
  exist; its parent does) and every directory as ``0o700``, restores each
  file's mode from its tar header regardless of the process umask, and
  rejects a violation with ``TELEPORT_DIGEST_MISMATCH`` leaving no ``dest``.
  ``total`` bounds the sum of extracted file sizes; a stream whose members
  add up to exactly ``total`` is accepted.
- Bundles are real ``export_bundle`` output from a tiny temporary agent home,
  so ``validate_bundle`` can check the unpacked result.
"""

from __future__ import annotations

import importlib
import io
import os
import tarfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from hyprial.daemon.impl.transfer.archive.bundle import export_bundle

STREAM = "hyprial.daemon.impl.teleport.stream"
CREATED_AT = "2026-11-12T00:00:00Z"

HOME_FILES: tuple[tuple[str, bytes, int], ...] = (
    ("state/agent.json", b'{"name": "worker"}\n', 0o600),
    ("config/settings.toml", b"model = 'm-1'\n", 0o640),
    ("workspace/run.sh", b"#!/bin/sh\necho hi\n", 0o755),
    ("workspace/a/x.txt", b"inside a\n", 0o644),
    ("workspace/a-b/x.txt", b"inside a-b\n", 0o644),
    ("workspace/B.txt", b"upper\n", 0o644),
    ("workspace/é.txt", "non-ascii é\n".encode(), 0o644),
    ("workspace/empty.txt", b"", 0o600),
    ("workspace/sub/deep/big.bin", bytes(range(256)) * 23, 0o644),
)


def stream() -> ModuleType:
    """Import the slice 4a stream package on demand."""

    return importlib.import_module(STREAM)


def make_home(root: Path, files=HOME_FILES) -> Path:
    for relative, data, mode in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, mode)
    return root


def make_bundle(tmp_path: Path, name: str = "bundle") -> Path:
    """Export a real bundle of a tiny agent home into ``tmp_path / name``."""

    home = make_home(tmp_path / f"{name}-home")
    bundle = tmp_path / name
    export_bundle(
        home,
        bundle,
        owner="owner-1",
        actor="worker",
        source_machine="node-a",
        created_at=CREATED_AT,
    )
    return bundle


def expected_names() -> list[str]:
    names = ["manifest.json"] + [f"payload/{rel}" for rel, _, _ in HOME_FILES]
    return sorted(names, key=lambda name: name.encode("utf-8"))


@dataclass(frozen=True)
class Member:
    name: str
    kind: bytes = tarfile.REGTYPE
    data: bytes = b""
    linkname: str = ""
    mode: int = 0o644


def write_tar(path: Path, members: list[Member]) -> Path:
    """Write a hand-built PAX tar; names are stored exactly as given."""

    with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
        for member in members:
            info = tarfile.TarInfo(member.name)
            info.type = member.kind
            info.mode = member.mode
            info.linkname = member.linkname
            if member.kind == tarfile.REGTYPE:
                info.size = len(member.data)
                archive.addfile(info, io.BytesIO(member.data))
            else:
                archive.addfile(info)
    return path


def stored_names(path: Path) -> list[str]:
    with tarfile.open(path, "r:") as archive:
        return archive.getnames()
