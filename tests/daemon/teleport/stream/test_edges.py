"""Slice 4a: choices the main stream tests leave open, pinned conservatively.

- ``pack_bundle`` / ``unpack_stream`` refuse an existing ``dest`` and leave it
  untouched.
- ``pack_bundle`` refuses symlinks, fifos and files the manifest does not
  register, and leaves no partial tar.
- ``unpack_stream`` restores permission bits only, never setuid/sticky bits.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.archive.bundle import BundleError

from .samples import Member, make_bundle, stream, write_tar


def test_pack_refuses_existing_dest(tmp_path: Path) -> None:
    dest = tmp_path / "out.tar"
    dest.write_bytes(b"keep me")
    with pytest.raises(FileExistsError):
        stream().pack_bundle(make_bundle(tmp_path), dest)
    assert dest.read_bytes() == b"keep me"


def _extra_symlink(bundle: Path) -> None:
    os.symlink("ok.txt", bundle / "payload" / "link")


def _extra_symlink_to_dir(bundle: Path) -> None:
    os.symlink(bundle / "payload", bundle / "payload" / "dirlink")


def _extra_fifo(bundle: Path) -> None:
    os.mkfifo(bundle / "payload" / "pipe")


def _extra_regular(bundle: Path) -> None:
    (bundle / "payload" / "stray.txt").write_bytes(b"not in the manifest")


@pytest.mark.parametrize(
    "tamper",
    (_extra_symlink, _extra_symlink_to_dir, _extra_fifo, _extra_regular),
    ids=lambda fn: fn.__name__,
)
def test_pack_refuses_non_regular_or_unregistered_entries(tmp_path: Path, tamper) -> None:
    bundle = make_bundle(tmp_path)
    tamper(bundle)
    dest = tmp_path / "out.tar"
    with pytest.raises(BundleError):
        stream().pack_bundle(bundle, dest)
    assert not dest.exists()


def test_unpack_refuses_existing_dest(tmp_path: Path) -> None:
    source = write_tar(tmp_path / "in.tar", [Member("a.txt", data=b"x")])
    dest = tmp_path / "unpacked"
    dest.mkdir()
    (dest / "keep.txt").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        stream().unpack_stream(source, dest, total=1)
    assert [p.name for p in dest.iterdir()] == ["keep.txt"]


def test_unpack_drops_special_mode_bits(tmp_path: Path) -> None:
    source = write_tar(tmp_path / "in.tar", [Member("a.sh", data=b"x", mode=0o6755)])
    dest = tmp_path / "unpacked"
    stream().unpack_stream(source, dest, total=1)
    assert stat.S_IMODE((dest / "a.sh").stat().st_mode) == 0o755


def test_unpack_rejects_member_path_through_a_file(tmp_path: Path) -> None:
    source = write_tar(
        tmp_path / "in.tar",
        [Member("a", data=b"file"), Member("a/b.txt", data=b"x")],
    )
    dest = tmp_path / "unpacked"
    with pytest.raises(Exception) as caught:
        stream().unpack_stream(source, dest, total=100)
    assert caught.value.code == "TELEPORT_DIGEST_MISMATCH"
    assert not dest.exists()
