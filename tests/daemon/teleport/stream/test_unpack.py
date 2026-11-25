"""Slice 4a: ``unpack_stream`` restores a bundle and enforces §1 strictly.

Decisions are recorded in ``samples.py``.  Every rejection case writes a
legitimate file first, so an extractor that does not validate up front (or
does not clean up) would leave ``dest`` behind; each case asserts ``dest``
does not exist afterwards and nothing escaped next to it.
"""

from __future__ import annotations

import os
import stat
import tarfile
from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.archive.bundle import load_manifest, validate_bundle

from ..support import teleport
from .samples import HOME_FILES, Member, make_bundle, stored_names, stream, write_tar


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(params=(0o022, 0o077, 0o000), ids=("umask022", "umask077", "umask000"))
def umask(request):
    previous = os.umask(request.param)
    try:
        yield request.param
    finally:
        os.umask(previous)


def test_round_trip_restores_content_mode_and_validates(tmp_path: Path, umask) -> None:
    bundle = make_bundle(tmp_path)
    packed = stream().pack_bundle(bundle, tmp_path / "out.tar")
    dest = tmp_path / "unpacked"
    stream().unpack_stream(packed.path, dest, total=packed.total)

    for relative, data, mode in HOME_FILES:
        restored = dest / "payload" / relative
        assert restored.read_bytes() == data, relative
        assert _mode(restored) == mode, relative
    assert (dest / "manifest.json").read_bytes() == (bundle / "manifest.json").read_bytes()
    assert _mode(dest / "manifest.json") == 0o600

    assert _mode(dest) == 0o700
    for directory in [path for path in dest.rglob("*") if path.is_dir()]:
        assert _mode(directory) == 0o700, directory

    manifest = validate_bundle(dest)
    original = load_manifest(bundle)
    assert manifest.as_dict()["manifest_digest"] == original.as_dict()["manifest_digest"]


def test_unpacked_tree_holds_exactly_the_packed_files(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    packed = stream().pack_bundle(bundle, tmp_path / "out.tar")
    dest = tmp_path / "unpacked"
    stream().unpack_stream(packed.path, dest, total=packed.total)
    files = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file())
    expected = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file())
    assert files == expected


def test_hand_built_tar_restores_modes_and_private_dirs(tmp_path: Path, umask) -> None:
    source = write_tar(
        tmp_path / "in.tar",
        [
            Member("a/b/c.txt", data=b"abc", mode=0o751),
            Member("top.txt", data=b"top", mode=0o604),
        ],
    )
    dest = tmp_path / "unpacked"
    stream().unpack_stream(source, dest, total=source.stat().st_size)
    assert (dest / "a/b/c.txt").read_bytes() == b"abc"
    assert _mode(dest / "a/b/c.txt") == 0o751
    assert _mode(dest / "top.txt") == 0o604
    for directory in (dest, dest / "a", dest / "a/b"):
        assert _mode(directory) == 0o700


def _assert_rejected(source: Path, dest: Path, *, total: int) -> None:
    module = teleport()
    with pytest.raises(module.TeleportError) as caught:
        stream().unpack_stream(source, dest, total=total)
    assert caught.value.code == "TELEPORT_DIGEST_MISMATCH"
    assert not dest.exists()
    assert not os.path.lexists(dest)


GOOD = Member("payload/ok.txt", data=b"fine\n")

REJECTED = {
    "absolute-path": [GOOD, Member("/tmp/teleport-4a-absolute.txt", data=b"x")],
    "dotdot-escape": [GOOD, Member("../escape.txt", data=b"x")],
    "dotdot-nested-escape": [GOOD, Member("payload/../../escape.txt", data=b"x")],
    "dotdot-inside": [GOOD, Member("payload/../inside.txt", data=b"x")],
    "dotdot-component": [GOOD, Member("payload/..", data=b"x")],
    "symlink": [GOOD, Member("payload/link", kind=tarfile.SYMTYPE, linkname="ok.txt")],
    "symlink-escape": [
        GOOD,
        Member("payload/link", kind=tarfile.SYMTYPE, linkname="/etc/passwd"),
    ],
    "hardlink": [GOOD, Member("payload/hard", kind=tarfile.LNKTYPE, linkname="payload/ok.txt")],
    "char-device": [GOOD, Member("payload/chr", kind=tarfile.CHRTYPE)],
    "block-device": [GOOD, Member("payload/blk", kind=tarfile.BLKTYPE)],
    "fifo": [GOOD, Member("payload/fifo", kind=tarfile.FIFOTYPE)],
    "directory-entry": [GOOD, Member("payload/dir", kind=tarfile.DIRTYPE, mode=0o755)],
    "duplicate-entry": [GOOD, Member("payload/ok.txt", data=b"second copy\n")],
}


@pytest.mark.parametrize("case", sorted(REJECTED))
def test_unsafe_member_is_rejected_and_leaves_no_dest(tmp_path: Path, case: str) -> None:
    work = tmp_path / "work"
    work.mkdir()
    source = write_tar(tmp_path / "in.tar", REJECTED[case])
    assert stored_names(source)[-1] == REJECTED[case][-1].name
    dest = work / "unpacked"
    _assert_rejected(source, dest, total=source.stat().st_size)
    assert sorted(os.listdir(work)) == []
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "inside.txt").exists()
    assert not Path("/tmp/teleport-4a-absolute.txt").exists()


def test_total_below_extracted_bytes_is_rejected(tmp_path: Path) -> None:
    source = write_tar(
        tmp_path / "in.tar",
        [Member("a.txt", data=b"0123456789"), Member("b.txt", data=b"abcdefghij")],
    )
    _assert_rejected(source, tmp_path / "unpacked", total=19)


def test_total_below_extracted_bytes_of_real_bundle_is_rejected(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    packed = stream().pack_bundle(bundle, tmp_path / "out.tar")
    payload = sum(len(data) for _, data, _ in HOME_FILES)
    manifest = (bundle / "manifest.json").stat().st_size
    _assert_rejected(packed.path, tmp_path / "unpacked", total=payload + manifest - 1)


def test_total_equal_to_extracted_bytes_is_accepted(tmp_path: Path) -> None:
    source = write_tar(
        tmp_path / "in.tar",
        [Member("a.txt", data=b"0123456789"), Member("b.txt", data=b"abcdefghij")],
    )
    dest = tmp_path / "unpacked"
    stream().unpack_stream(source, dest, total=20)
    assert (dest / "a.txt").read_bytes() == b"0123456789"
    assert (dest / "b.txt").read_bytes() == b"abcdefghij"


def test_garbage_stream_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "in.tar"
    source.write_bytes(b"this is not a tar archive" * 50)
    _assert_rejected(source, tmp_path / "unpacked", total=source.stat().st_size)


def test_truncated_stream_is_rejected(tmp_path: Path) -> None:
    full = write_tar(
        tmp_path / "full.tar",
        [GOOD, Member("payload/big.bin", data=bytes(range(256)) * 40)],
    )
    source = tmp_path / "in.tar"
    source.write_bytes(full.read_bytes()[:3000])
    _assert_rejected(source, tmp_path / "unpacked", total=full.stat().st_size)
