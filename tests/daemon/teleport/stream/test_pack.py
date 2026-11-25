"""Slice 4a: ``pack_bundle`` produces one deterministic, uncompressed PAX tar.

Decisions are recorded in ``samples.py``.  Determinism is checked against a
second copy of the bundle written in reverse order with different mtimes,
different on-disk modes for ``manifest.json`` and, when the process may
``chown``, a different owner.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
from pathlib import Path

from hyprial.daemon.impl.transfer.archive.bundle import load_manifest

from .samples import HOME_FILES, expected_names, make_bundle, stream


def test_pack_returns_packed_stream(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    dest = tmp_path / "out.tar"
    packed = stream().pack_bundle(bundle, dest)
    assert isinstance(packed, stream().PackedStream)
    data = dest.read_bytes()
    assert packed.path == dest
    assert packed.total == len(data)
    assert packed.sha256 == hashlib.sha256(data).hexdigest()


def test_pack_is_an_uncompressed_pax_tar(tmp_path: Path) -> None:
    dest = tmp_path / "out.tar"
    stream().pack_bundle(make_bundle(tmp_path), dest)
    data = dest.read_bytes()
    assert not data.startswith(b"\x1f\x8b")
    assert data[257:265] == b"ustar\x0000"
    with tarfile.open(dest, "r:") as archive:
        assert archive.getmembers()


def test_members_are_regular_files_in_byte_order(tmp_path: Path) -> None:
    dest = tmp_path / "out.tar"
    stream().pack_bundle(make_bundle(tmp_path), dest)
    with tarfile.open(dest, "r:") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == expected_names()
    assert all(member.isreg() for member in members)


def test_member_headers_are_normalised(tmp_path: Path) -> None:
    dest = tmp_path / "out.tar"
    stream().pack_bundle(make_bundle(tmp_path), dest)
    with tarfile.open(dest, "r:") as archive:
        for member in archive.getmembers():
            assert member.uid == 0, member.name
            assert member.gid == 0, member.name
            assert member.uname == "", member.name
            assert member.gname == "", member.name
            assert member.mtime == 0, member.name


def test_member_content_matches_bundle_files(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    dest = tmp_path / "out.tar"
    stream().pack_bundle(bundle, dest)
    with tarfile.open(dest, "r:") as archive:
        for member in archive.getmembers():
            extracted = archive.extractfile(member)
            assert extracted is not None
            assert extracted.read() == (bundle / member.name).read_bytes()
            assert member.size == (bundle / member.name).stat().st_size


def test_modes_come_from_the_manifest(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    manifest = load_manifest(bundle)
    recorded = {f"payload/{entry.path}": entry.mode for entry in manifest.entries}
    assert recorded["payload/workspace/run.sh"] == 0o755
    os.chmod(bundle / "payload/workspace/run.sh", 0o600)
    os.chmod(bundle / "payload/state/agent.json", 0o644)
    os.chmod(bundle / "manifest.json", 0o664)
    dest = tmp_path / "out.tar"
    stream().pack_bundle(bundle, dest)
    with tarfile.open(dest, "r:") as archive:
        modes = {member.name: member.mode for member in archive.getmembers()}
    assert modes.pop("manifest.json") == 0o600
    assert modes == recorded
    expected = {f"payload/{rel}": mode for rel, _, mode in HOME_FILES}
    assert modes == expected


def test_two_packs_of_the_same_directory_are_identical(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    first = stream().pack_bundle(bundle, tmp_path / "first.tar")
    second = stream().pack_bundle(bundle, tmp_path / "second.tar")
    assert first.path.read_bytes() == second.path.read_bytes()
    assert (first.total, first.sha256) == (second.total, second.sha256)


def _copy_reversed(source: Path, dest: Path) -> None:
    files = sorted(
        (path for path in source.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(source).as_posix(),
        reverse=True,
    )
    dest.mkdir()
    for index, path in enumerate(files):
        target = dest / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        os.chmod(target, 0o640 if path.name == "manifest.json" else 0o600)
        os.utime(target, (1_000_000 + index, 2_000_000 + index * 7))
        if os.geteuid() == 0:
            os.chown(target, 1234, 4321)


def test_pack_ignores_iteration_order_mtimes_and_ownership(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    copy = tmp_path / "copy"
    _copy_reversed(bundle, copy)
    original = stream().pack_bundle(bundle, tmp_path / "original.tar")
    shuffled = stream().pack_bundle(copy, tmp_path / "shuffled.tar")
    assert original.path.read_bytes() == shuffled.path.read_bytes()
    assert original.sha256 == shuffled.sha256
