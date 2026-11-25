"""shell-6.md §D.4 6c-2: staging the bundled runtime from a fake PBS tarball and wheel.

Everything is local: the tarball, the hyprial wheel and one dependency wheel
are written into the test's temporary directory and installed by uv with
``--offline``; no network, no real interpreter from the tarball is run.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.tooling.release.shell.support import LOCK, load

TARGET = "aarch64-apple-darwin"
COMMIT = "c" * 40
WHEEL_VERSION = "0.0.1+gccccccc"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _member(archive: tarfile.TarFile, name: str, *, data: bytes = b"", mode: int = 0o644, kind: bytes = tarfile.REGTYPE, link: str = "") -> None:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.mtime = 0
    info.linkname = link
    if kind == tarfile.REGTYPE:
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    else:
        archive.addfile(info)


def _tarball(path: Path, extra: list[tuple[str, bytes]] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for directory in ("python", "python/bin", "python/lib", "python/lib/python3.12", "python/include", "python/share"):
            _member(archive, directory, kind=tarfile.DIRTYPE, mode=0o755)
        _member(archive, "python/bin/python3.12", data=b"#!/bin/sh\nexit 0\n", mode=0o755)
        _member(archive, "python/bin/python3", kind=tarfile.SYMTYPE, link="python3.12", mode=0o777)
        _member(archive, "python/bin/pip3", data=b"#!/bin/sh\n", mode=0o755)
        _member(archive, "python/lib/libpython3.12.dylib", data=b"\xcf\xfa\xed\xfe fake", mode=0o755)
        _member(archive, "python/lib/python3.12/os.py", data=b"VALUE = {'a', 'b', 'c'}\n")
        _member(archive, "python/lib/python3.12/__pycache__/os.cpython-312.pyc", data=b"stale")
        _member(archive, "python/lib/python3.12/lib-dynload/_tkinter.cpython-312-darwin.so", data=b"tk")
        _member(archive, "python/lib/python3.12/lib-dynload/_json.cpython-312-darwin.so", data=b"json", mode=0o755)
        _member(archive, "python/lib/python3.12/config-3.12-darwin/libpython3.12.a", data=b"static")
        _member(archive, "python/lib/python3.12/site-packages/pip/__init__.py", data=b"")
        _member(archive, "python/lib/libtcl9.0.dylib", data=b"tcl")
        _member(archive, "python/lib/pkgconfig/python3.pc", kind=tarfile.SYMTYPE, link="python-3.12.pc")
        for pruned in ("test", "idlelib", "tkinter", "ensurepip"):
            _member(archive, f"python/lib/python3.12/{pruned}/__init__.py", data=b"")
        _member(archive, "python/include/python3.12/Python.h", data=b"")
        _member(archive, "python/share/man/man1/python3.12.1", data=b"")
        for name, data in extra or []:
            _member(archive, name, data=data)
    path.write_bytes(buffer.getvalue())
    return buffer.getvalue()


def _wheel(directory: Path, name: str, version: str, files: dict[str, bytes], tag: str = "py3-none-any") -> Path:
    path = directory / f"{name}-{version}-{tag}.whl"
    info = f"{name}-{version}.dist-info"
    entries = dict(files)
    entries[f"{info}/METADATA"] = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    entries[f"{info}/WHEEL"] = f"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: {tag}\n".encode()
    record = "".join(f"{key},," + "\n" for key in sorted(entries)) + f"{info}/RECORD,,\n"
    entries[f"{info}/RECORD"] = record.encode()
    with zipfile.ZipFile(path, "w") as archive:
        for key in sorted(entries):
            archive.writestr(zipfile.ZipInfo(key, (2020, 2, 2, 0, 0, 0)), entries[key])
    return path


@pytest.fixture()
def inputs(tmp_path: Path) -> dict[str, Path]:
    source = tmp_path / "inputs"
    source.mkdir()
    archive = source / "cpython.tar.gz"
    data = _tarball(archive)
    release = json.dumps({"sourceCommit": COMMIT}).encode()
    wheel = _wheel(
        source,
        "hyprial",
        WHEEL_VERSION,
        {
            "hyprial/__init__.py": b"NAMES = {'x', 'y'}\n",
            "hyprial/_gui/release.json": release,
            f"hyprial-{WHEEL_VERSION}.dist-info/entry_points.txt": b"[console_scripts]\nhyprial = hyprial:main\n",
        },
    )
    dependency = _wheel(source, "fakedep", "1.0", {"fakedep/__init__.py": b"X = 1\n"})
    requirements = source / "requirements.txt"
    requirements.write_text(f"fakedep @ {dependency.as_uri()} --hash=sha256:{_sha(dependency.read_bytes())}\n", encoding="utf-8")
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    lock["targets"][TARGET]["sha256"] = _sha(data)
    lock["targets"][TARGET]["size"] = len(data)
    lock_path = source / "runtime-lock.json"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    return {"archive": archive, "wheel": wheel, "requirements": requirements, "lock": lock_path}


def _stage(inputs: dict[str, Path], out: Path, **overrides: object) -> dict:
    stage = load("stage_runtime")
    arguments = {
        "target": TARGET,
        "wheel": inputs["wheel"],
        "out": out,
        "lock_path": inputs["lock"],
        "archive": inputs["archive"],
        "requirements": inputs["requirements"],
        "offline": True,
    }
    arguments.update(overrides)
    return stage.stage(**arguments)


def _tree(root: Path) -> dict[str, tuple[bytes, int]]:
    result = {}
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode
        relative = path.relative_to(root).as_posix()
        if stat.S_ISREG(mode):
            result[relative] = (path.read_bytes(), stat.S_IMODE(mode))
        else:
            result[relative] = (b"<dir>" if stat.S_ISDIR(mode) else b"<other>", stat.S_IMODE(mode))
    return result


def test_wrong_sha_is_refused_before_extraction(inputs: dict[str, Path], tmp_path: Path) -> None:
    stage = load("stage_runtime")
    lock = json.loads(inputs["lock"].read_text(encoding="utf-8"))
    lock["targets"][TARGET]["sha256"] = "0" * 64
    inputs["lock"].write_text(json.dumps(lock), encoding="utf-8")
    out = tmp_path / "work" / "runtime"
    with pytest.raises(stage.StageError, match="sha256"):
        _stage(inputs, out)
    assert not (tmp_path / "work").exists() or not any((tmp_path / "work").rglob("os.py"))


def test_staged_tree_is_pruned_symlink_free_and_complete(inputs: dict[str, Path], tmp_path: Path) -> None:
    out = tmp_path / "runtime"
    manifest = _stage(inputs, out)

    interpreter = out / "bin" / "python3.12"
    assert stat.S_ISREG(interpreter.lstat().st_mode)
    assert os.access(interpreter, os.X_OK)
    assert sorted(path.name for path in (out / "bin").iterdir()) == ["python3.12"]
    assert [path for path in out.rglob("*") if path.is_symlink()] == []
    library = out / "lib" / "python3.12"
    for pruned in ("test", "idlelib", "tkinter", "ensurepip", "config-3.12-darwin"):
        assert not (library / pruned).exists(), pruned
    for pruned in ("include", "share", "lib/pkgconfig", "lib/libtcl9.0.dylib", "lib/python3.12/lib-dynload/_tkinter.cpython-312-darwin.so", "lib/python3.12/site-packages/pip"):
        assert not (out / pruned).exists(), pruned
    assert (out / "lib/python3.12/lib-dynload/_json.cpython-312-darwin.so").is_file()
    assert b"stale" not in b"".join(path.read_bytes() for path in out.rglob("*.pyc"))

    site = library / "site-packages"
    assert (site / "hyprial" / "__init__.py").is_file()
    assert (site / "hyprial" / "_gui" / "release.json").is_file()
    assert (site / "fakedep" / "__init__.py").is_file()
    assert not (site / "bin").exists()
    for name in ("direct_url.json", "INSTALLER", "REQUESTED"):
        assert list(site.rglob(name)) == [], name
    records = "".join(path.read_text() for path in site.rglob("RECORD"))
    assert "direct_url.json" not in records and "INSTALLER" not in records
    assert list(site.glob("hyprial/__pycache__/__init__.cpython-312.pyc"))

    written = json.loads((out / "runtime-manifest.json").read_text(encoding="utf-8"))
    assert written == manifest
    assert written["target"] == TARGET
    assert written["pbsTag"] == "20261110"
    assert written["python"] == "3.12.16"
    assert written["sourceCommit"] == COMMIT
    assert written["wheel"]["version"] == WHEEL_VERSION
    assert written["wheel"]["sha256"] == _sha(inputs["wheel"].read_bytes())
    files = {path.relative_to(out).as_posix(): path for path in out.rglob("*") if path.is_file()}
    files.pop("runtime-manifest.json")
    assert set(written["files"]) == set(files)
    for relative, path in files.items():
        assert written["files"][relative]["sha256"] == _sha(path.read_bytes())
        assert written["files"][relative]["mode"] == f"{stat.S_IMODE(path.stat().st_mode):04o}"


def test_no_build_path_is_baked_into_the_tree(inputs: dict[str, Path], tmp_path: Path) -> None:
    out = tmp_path / "runtime"
    _stage(inputs, out)
    needles = [str(tmp_path).encode(), str(inputs["wheel"].parent).encode()]
    for path in out.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert not any(needle in data for needle in needles), path


def test_second_run_is_byte_identical(inputs: dict[str, Path], tmp_path: Path) -> None:
    first = tmp_path / "a" / "runtime"
    second = tmp_path / "b" / "runtime"
    _stage(inputs, first)
    _stage(inputs, second)
    assert _tree(first) == _tree(second)
    _stage(inputs, first)
    assert _tree(first) == _tree(second)


@pytest.mark.parametrize("name", ["python/../evil.py", "/python/abs.py", "other/file.py"])
def test_unsafe_members_are_refused(inputs: dict[str, Path], tmp_path: Path, name: str) -> None:
    stage = load("stage_runtime")
    data = _tarball(inputs["archive"], extra=[(name, b"x")])
    lock = json.loads(inputs["lock"].read_text(encoding="utf-8"))
    lock["targets"][TARGET]["sha256"] = _sha(data)
    lock["targets"][TARGET]["size"] = len(data)
    inputs["lock"].write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(stage.StageError, match="member"):
        _stage(inputs, tmp_path / "runtime")
    assert not (tmp_path / "evil.py").exists()
    assert not (tmp_path / "runtime").exists()


def test_wheel_for_another_platform_is_refused(inputs: dict[str, Path], tmp_path: Path) -> None:
    stage = load("stage_runtime")
    linux = _wheel(inputs["wheel"].parent, "hyprial", "0.0.2", {"hyprial/_gui/release.json": b"{}"}, tag="cp312-abi3-manylinux_2_17_x86_64")
    with pytest.raises(stage.StageError, match="macosx_13_0_arm64"):
        _stage(inputs, tmp_path / "runtime", wheel=linux)


def test_existing_directory_without_manifest_is_never_replaced(inputs: dict[str, Path], tmp_path: Path) -> None:
    stage = load("stage_runtime")
    out = tmp_path / "runtime"
    out.mkdir()
    (out / "precious.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(stage.StageError, match="runtime-manifest.json"):
        _stage(inputs, out)
    assert (out / "precious.txt").read_text(encoding="utf-8") == "keep"
