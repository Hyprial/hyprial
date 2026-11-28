"""sign_bundle.py's pure parts: Mach-O detection, signing order, archives, manifest.

Runs on every platform without any tool: Mach-O files are found by their
magic number (thin, fat and fat64, a Java class file is not fat Mach-O),
ordered deepest first with the bundle's main executable left to the bundle
signature; archives (zip family, found by magic, name or a readable central
directory, so a ``.pyz`` or self-extracting file counts) are opened and every
member holding Mach-O is reported, and tar, gzip, bzip2, xz, zstd, xar and
static ``!<arch>`` libraries are reported as archives the tool will not sign
around; the regenerated runtime manifest may differ from the old one only in
the SHA-256 of the files that were signed, and at least one must have been;
the home directory is written ``~`` in anything the tool prints or records.
"""

from __future__ import annotations

import bz2
import gzip
import io
import json
import lzma
import plistlib
import struct
import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.tooling.release.shell.signing.bundle.bundle_fakes import FAT, FAT64, JAVA_CLASS, MACHO, RUNTIME_MACHOS, make_app, write, write_zip
from tests.tooling.release.shell.support import load

sign_bundle = load("sign_bundle")


def _runtime(app: Path) -> Path:
    return app / "Contents" / "Resources" / "runtime"


def test_a_fat_header_is_told_apart_from_a_java_class(tmp_path: Path) -> None:
    cases = {
        "thin64": MACHO,
        "thin32": struct.pack("<I", 0xFEEDFACE) + b"xxxx",
        "be64": struct.pack(">I", 0xFEEDFACF) + b"xxxx",
        "fat": FAT + b"x",
        "fat64": FAT64 + b"x",
        "java": JAVA_CLASS + b"x",
        "text": b"#!/bin/sh\n",
        "short": b"\xca\xfe",
        "empty": b"",
        "zip": b"PK\x03\x04rest",
    }
    for name, data in cases.items():
        (tmp_path / name).write_bytes(data)
    assert {name for name in cases if sign_bundle.is_macho(tmp_path / name)} == {"thin64", "thin32", "be64", "fat", "fat64"}


def test_mach_o_files_are_found_by_magic_deepest_first_without_the_main_executable(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    found = sign_bundle.signing_order(app)
    runtime = _runtime(app)
    assert {path.relative_to(runtime).as_posix() for path in found} == RUNTIME_MACHOS
    assert app / "Contents" / "MacOS" / "hyprial-desktop" not in found
    depths = [len(path.parts) for path in found]
    assert depths == sorted(depths, reverse=True)
    assert all(not path.is_symlink() for path in found)


def test_other_mach_o_next_to_the_main_executable_is_signed_on_its_own(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    helper = app / "Contents" / "MacOS" / "helper"
    write(helper, MACHO + b"helper", 0o755)
    assert helper in sign_bundle.signing_order(app)


def test_a_nested_bundle_is_signed_after_its_contents_and_its_executable_by_the_bundle(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    framework = app / "Contents" / "Frameworks" / "Thing.framework"
    write(framework / "Versions" / "A" / "Thing", MACHO + b"thing", 0o755)
    write(framework / "Versions" / "A" / "Resources" / "Info.plist", b"plist")
    nested = app / "Contents" / "Library" / "Helper.app"
    write(nested / "Contents" / "Info.plist", plistlib.dumps({"CFBundleExecutable": "helper"}))
    write(nested / "Contents" / "MacOS" / "helper", MACHO + b"nested main", 0o755)
    write(nested / "Contents" / "Resources" / "lib.dylib", MACHO + b"nested lib")
    order = sign_bundle.signing_order(app)
    assert nested in order and framework in order
    assert nested / "Contents" / "MacOS" / "helper" not in order
    assert order.index(nested / "Contents" / "Resources" / "lib.dylib") < order.index(nested)
    assert order.index(framework / "Versions" / "A" / "Thing") < order.index(framework)


def test_archives_are_found_by_magic_and_mach_o_members_reported(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    site = _runtime(app) / "lib" / "python3.12" / "site-packages"
    write_zip(site / "vendored" / "tool.whl", {"tool/_speed.so": MACHO + b"in wheel", "tool/__init__.py": b""})
    write_zip(site / "vendored" / "no_suffix_archive", {"bin/run": FAT + b"fat in zip"})
    write_zip(site / "vendored" / "clean.jar", {"A.class": JAVA_CLASS + b"java"})
    report = sign_bundle.scan_archives(app)
    archives = {entry["path"]: entry for entry in report["archives"]}
    base = "Contents/Resources/runtime/lib/python3.12/site-packages"
    assert set(archives) == {f"{base}/pkg/data.zip", f"{base}/vendored/tool.whl", f"{base}/vendored/no_suffix_archive", f"{base}/vendored/clean.jar"}
    assert archives[f"{base}/vendored/tool.whl"]["macho"] == ["tool/_speed.so"]
    assert archives[f"{base}/vendored/no_suffix_archive"]["macho"] == ["bin/run"]
    assert archives[f"{base}/vendored/clean.jar"]["macho"] == []
    assert archives[f"{base}/pkg/data.zip"]["macho"] == []
    assert report["withMacho"] == sorted([f"{base}/vendored/tool.whl", f"{base}/vendored/no_suffix_archive"])


def test_an_unreadable_archive_is_reported_not_ignored(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    write(_runtime(app) / "broken.zip", b"PK\x03\x04 not really a zip")
    report = sign_bundle.scan_archives(app)
    assert report["unreadable"] == ["Contents/Resources/runtime/broken.zip"]


def test_a_zip_behind_a_prefix_is_an_archive_and_an_empty_central_directory_is_harmless(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    site = _runtime(app) / "lib" / "python3.12" / "site-packages"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("tool/_speed.so", MACHO + b"in pyz")
    write(site / "tool.pyz", b"#!/usr/bin/env python3\n" + buffer.getvalue(), 0o755)
    write(site / "__pycache__" / "zipimport.cpython-312.pyc", b"\xcb\r\r\n" + b"code mentioning " + b"PK\x05\x06" + bytes(18))
    report = sign_bundle.scan_archives(app)
    archives = {entry["path"].rsplit("/", 1)[-1]: entry for entry in report["archives"]}
    assert archives["tool.pyz"]["macho"] == ["tool/_speed.so"]
    assert archives["zipimport.cpython-312.pyc"] == {"path": archives["zipimport.cpython-312.pyc"]["path"], "members": 0, "macho": []}
    assert report["unreadable"] == [] and report["withMacho"] == [archives["tool.pyz"]["path"]]


def test_archives_the_tool_cannot_look_into_are_reported_by_kind(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    other = _runtime(app) / "other"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("x.so")
        member.size = len(MACHO)
        archive.addfile(member, io.BytesIO(MACHO))
    write(other / "a.tar", buffer.getvalue())
    write(other / "b.gz", gzip.compress(MACHO))
    write(other / "c.bz2", bz2.compress(MACHO))
    write(other / "d.xz", lzma.compress(MACHO))
    write(other / "e.zst", b"\x28\xb5\x2f\xfd" + b"frame")
    write(other / "f.pkg", b"xar!" + bytes(24))
    write(other / "libg.a", b"!<arch>\n" + b"member")
    report = sign_bundle.scan_archives(app)
    kinds = {entry["path"].rsplit("/", 1)[-1]: entry["kind"] for entry in report["otherArchives"]}
    assert kinds == {"a.tar": "tar", "b.gz": "gzip", "c.bz2": "bzip2", "d.xz": "xz", "e.zst": "zstd", "f.pkg": "xar", "libg.a": "ar"}


def test_home_paths_are_written_with_a_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    home = "/nowhere/someone"
    monkeypatch.setenv("HOME", home)
    text = f"{home}/out/x.dmg and {home}.other and {home}"
    assert sign_bundle.scrub(text) == f"~/out/x.dmg and {home}.other and ~"
    assert sign_bundle.scrub({"argv": [f"{home}/a"], "n": 1}) == {"argv": ["~/a"], "n": 1}


def test_a_runtime_where_nothing_was_signed_is_refused(tmp_path: Path) -> None:
    runtime = _runtime(make_app(tmp_path))
    with pytest.raises(sign_bundle.SignError, match="nothing under the runtime was signed"):
        sign_bundle.regenerate_manifest(runtime, set())


def _sign_like_codesign(runtime: Path, names: set[str]) -> None:
    for name in names:
        with (runtime / name).open("ab") as stream:
            stream.write(b"\x00signature")


def test_the_regenerated_manifest_changes_only_the_hashes_of_signed_files(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    runtime = _runtime(app)
    before = json.loads((runtime / "runtime-manifest.json").read_text(encoding="utf-8"))
    _sign_like_codesign(runtime, RUNTIME_MACHOS)
    result = sign_bundle.regenerate_manifest(runtime, RUNTIME_MACHOS)
    after = json.loads((runtime / "runtime-manifest.json").read_text(encoding="utf-8"))
    assert {key: value for key, value in before.items() if key != "files"} == {key: value for key, value in after.items() if key != "files"}
    assert set(before["files"]) == set(after["files"])
    assert {name for name in before["files"] if before["files"][name] != after["files"][name]} == RUNTIME_MACHOS
    assert all(before["files"][name]["mode"] == after["files"][name]["mode"] for name in before["files"])
    assert result["changedFiles"] == len(RUNTIME_MACHOS)
    assert (runtime / "runtime-manifest.json").read_text(encoding="utf-8") == json.dumps(after, indent=2, sort_keys=True) + "\n"
    assert (runtime / "runtime-manifest.json").stat().st_mode & 0o777 == 0o644
    assert sign_bundle.verify_runtime.check_manifest(runtime)[0] == []


@pytest.mark.parametrize("damage", ["unsigned-file-changed", "mode-changed", "file-added", "file-removed", "signed-file-unchanged"])
def test_any_other_difference_refuses_the_regenerated_manifest(tmp_path: Path, damage: str) -> None:
    app = make_app(tmp_path)
    runtime = _runtime(app)
    site = runtime / "lib" / "python3.12" / "site-packages" / "pkg"
    signed = set(RUNTIME_MACHOS)
    _sign_like_codesign(runtime, signed)
    if damage == "unsigned-file-changed":
        (site / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    elif damage == "mode-changed":
        (site / "__init__.py").chmod(0o755)
    elif damage == "file-added":
        write(site / "new.py", b"")
    elif damage == "file-removed":
        (site / "fake.so").unlink()
    else:
        signed.add("lib/python3.12/site-packages/pkg/__init__.py")
    original = (runtime / "runtime-manifest.json").read_bytes()
    with pytest.raises(sign_bundle.SignError, match="manifest"):
        sign_bundle.regenerate_manifest(runtime, signed)
    assert (runtime / "runtime-manifest.json").read_bytes() == original


def test_the_archive_magics_are_the_full_headers_so_text_is_not_taken_for_an_archive(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    other = _runtime(app) / "other"
    posix = bytearray(512)
    posix[257:265] = b"ustar\x0000"
    gnu = bytearray(512)
    gnu[257:265] = b"ustar  \x00"
    loose = bytearray(512)
    loose[257:265] = b"ustarxyz"
    write(other / "posix.tar", bytes(posix))
    write(other / "gnu.tar", bytes(gnu))
    write(other / "empty.bz2", bz2.compress(b""))
    write(other / "notes.txt", b"BZh is how this text file starts\n")
    write(other / "bz.txt", b"BZh9 and then words\n")
    write(other / "gz.bin", b"\x1f\x8b\x00not deflate")
    write(other / "loose.bin", bytes(loose))
    report = sign_bundle.scan_archives(app)
    kinds = {entry["path"].rsplit("/", 1)[-1]: entry["kind"] for entry in report["otherArchives"]}
    assert kinds == {"posix.tar": "tar", "gnu.tar": "tar", "empty.bz2": "bzip2"}


def test_the_archive_scan_runs_on_its_own_for_the_ad_hoc_build(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archives = sign_bundle.archives_module
    assert archives.__file__.endswith("signing/archives.py")
    assert sign_bundle.scan_archives is archives.scan_archives and sign_bundle.archive_kind is archives.archive_kind
    app = make_app(tmp_path)
    assert archives.main(["--app", str(app)]) == 0
    assert capsys.readouterr().out.startswith("archives: 1 zip-family, with Mach-O [], unreadable [], other kinds []")
    write(_runtime(app) / "libx.a", b"!<arch>\n" + b"member")
    assert archives.main(["--app", str(app)]) == 1
    printed = capsys.readouterr().out
    assert "'kind': 'ar'" in printed and "FAIL" in printed
    assert archives.main(["--app", str(tmp_path / "nothing.app")]) == 1
