#!/usr/bin/env python3
# Decisions:
# - Moved out of packaging/clients/sign_bundle.py (2026-11-19, milestone 2
#   review of PR #2980) so the ad-hoc packaging path can run the same scan:
#   scripts/build/package-macos-shell.sh runs this file on the built app in
#   its verify_bundle stage with or without --developer-id, so a dependency
#   that brings an archive the signing tool refuses turns the ordinary
#   candidate build red, long before a signed run on the Mac would. The
#   ad-hoc run only reads files; it writes nothing and prints one line.
#   sign_bundle.py re-exports every name it used to define here.
# - Mach-O is found by magic, not by name: thin MH_MAGIC/MH_MAGIC_64 in
#   either byte order, fat FAT_MAGIC/FAT_MAGIC_64 (big-endian) only when the
#   architecture count is 1..29, because a Java class file starts with the
#   same 0xCAFEBABE followed by a version >= 45.
# - Archives: every file with zip magic, a .zip/.whl/.jar/.egg name or a
#   central directory zipfile.is_zipfile finds (a .pyz, a self-extracting
#   file) is opened and each member's first bytes checked for Mach-O. A hit
#   that opens with no members is harmless (measured: the stdlib's
#   zipimport .pyc holds the end-of-directory signature as a constant); one
#   that does not open is unreadable. tar, gzip, bzip2, xz, zstd, xar and
#   static libraries (`!<arch>`) are found by magic and refused, not opened.
#   The magics are the full format headers, so a text file that merely starts
#   with `BZh` or a byte pair is not taken for an archive: gzip `1f 8b 08`
#   (deflate, the only method), bzip2 `BZh` + a block size digit 1-9 + the
#   block magic `1AY&SY` (or the end-of-stream magic of an empty stream), tar
#   `ustar\0` (POSIX) or `ustar  \0` (GNU) at offset 257.
# - Static libraries (`!<arch>`, `*.a`) stay refused, fail closed, as before
#   (owner 2026-11-19: there is no decision to prune them). Reasoning: a
#   static library is not loaded at run time and the shipped app does not
#   need it; codesign cannot sign the object files inside it; whether Apple's
#   notary service objects to unsigned objects inside an `.a` has NOT been
#   measured (the measured bundle contains none). If one ever appears, the
#   options are to measure whether notarization accepts it as it is (then
#   accept it and record it here) or to drop the file at staging; neither is
#   implemented.
"""Find archives (and Mach-O inside them) under a HyprialOS.app; exit 1 when any would stop signing."""

from __future__ import annotations

import argparse
import os
import re
import stat
import zipfile
from pathlib import Path
from typing import Any

THIN_MAGIC = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe"}
FAT_MAGIC = {b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"}
FAT_ARCH_LIMIT = 30
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06")
ZIP_SUFFIXES = {".zip", ".whl", ".jar", ".egg"}
OTHER_ARCHIVES = (
    (re.compile(rb"\A\x1f\x8b\x08"), "gzip"),
    (re.compile(rb"\ABZh[1-9](?:1AY&SY|\x17rE8P\x90)"), "bzip2"),
    (re.compile(rb"\A\xfd7zXZ\x00"), "xz"),
    (re.compile(rb"\A\x28\xb5\x2f\xfd"), "zstd"),
    (re.compile(rb"\Axar!"), "xar"),
    (re.compile(rb"\A!<arch>\n"), "ar"),
)
TAR_MAGICS = (b"ustar\x00", b"ustar  \x00")
TAR_OFFSET = 257


def is_macho_header(head: bytes) -> bool:
    """Whether the first bytes of a file are a Mach-O (thin or fat) header."""

    if head[:4] in THIN_MAGIC:
        return True
    if head[:4] in FAT_MAGIC and len(head) >= 8:
        return 0 < int.from_bytes(head[4:8], "big") < FAT_ARCH_LIMIT
    return False


def archive_kind(head: bytes) -> str | None:
    """The non-zip archive kind the first 512 bytes of a file show, if any."""

    if any(head[TAR_OFFSET:TAR_OFFSET + len(magic)] == magic for magic in TAR_MAGICS):
        return "tar"
    return next((kind for magic, kind in OTHER_ARCHIVES if magic.match(head)), None)


def _files(root: Path) -> list[Path]:
    found = []
    for directory, _names, files in os.walk(root, followlinks=False):
        found.extend(Path(directory) / name for name in files)
    return sorted(found)


def scan_archives(app: Path) -> dict[str, Any]:
    """Zip-family archives under the app and which members hold Mach-O; archives of other kinds."""

    archives: list[dict[str, Any]] = []
    unreadable: list[str] = []
    other: list[dict[str, str]] = []
    for path in _files(app):
        if not stat.S_ISREG(path.lstat().st_mode):
            continue
        relative = path.relative_to(app).as_posix()
        with path.open("rb") as stream:
            head = stream.read(512)
        kind = archive_kind(head)
        if kind is not None:
            other.append({"path": relative, "kind": kind})
            continue
        if not (head[:4] in ZIP_MAGIC or path.suffix.lower() in ZIP_SUFFIXES or zipfile.is_zipfile(path)):
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                members = [info for info in archive.infolist() if not info.is_dir()]
                macho = sorted(info.filename for info in members if is_macho_header(archive.open(info).read(8)))
        except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
            unreadable.append(relative)
            continue
        archives.append({"path": relative, "members": len(members), "macho": macho})
    return {
        "archives": archives,
        "withMacho": sorted(entry["path"] for entry in archives if entry["macho"]),
        "unreadable": unreadable,
        "otherArchives": other,
    }


def blocking(report: dict[str, Any]) -> bool:
    """Whether the report holds anything the signing tool refuses."""

    return bool(report["withMacho"] or report["unreadable"] or report["otherArchives"])


def summary(report: dict[str, Any]) -> str:
    """One line for the log."""

    return f"archives: {len(report['archives'])} zip-family, with Mach-O {report['withMacho']}, unreadable {report['unreadable']}, other kinds {report['otherArchives']}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True, help="the built HyprialOS.app (read only)")
    args = parser.parse_args(argv)
    if not (args.app / "Contents").is_dir():
        print(f"not an app bundle: {args.app}")
        return 1
    report = scan_archives(args.app)
    print(summary(report))
    if blocking(report):
        print("FAIL: Developer ID signing refuses these archives; removing or repacking them is a design decision (packaging/clients/sign_bundle.py)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
