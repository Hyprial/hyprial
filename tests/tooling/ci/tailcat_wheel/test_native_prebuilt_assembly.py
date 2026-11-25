"""Contract tests for the per-target native module receipt/assembly tooling."""

from __future__ import annotations

import hashlib
import json
import struct
import zipfile
from pathlib import Path

import pytest

from hyprial.shell.impl.packaging import native_engine
from hyprial.shell.impl.packaging.tailcat_wheel import platform_tag
from scripts.ci import assemble_native_prebuilt as assembly

REPO = Path(__file__).resolve().parents[4]
VERSION = "0.5.5.4+gabcdef0"
SOURCE_SHA = "0" * 39 + "1"


def module_bytes(target: str) -> bytes:
    os_name, arch = target.split("/")
    if os_name == "linux":
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", header, 16, 3)
        struct.pack_into("<H", header, 18, 62 if arch == "amd64" else 183)
        return bytes(header)
    if os_name == "darwin":
        header = bytearray(32)
        header[:4] = b"\xcf\xfa\xed\xfe"
        struct.pack_into("<I", header, 4, 0x01000007 if arch == "amd64" else 0x0100000C)
        return bytes(header)
    data = bytearray(0x80)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x40)
    data[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", data, 0x44, 0x8664 if arch == "amd64" else 0xAA64)
    return bytes(data)


def write_wheel(
    dist: Path,
    target: str,
    *,
    payload: bytes | None = None,
    wheel_platform: str | None = None,
    extra_members: tuple[str, ...] = (),
    duplicate: bool = False,
) -> Path:
    dist.mkdir(parents=True, exist_ok=True)
    tag = wheel_platform or platform_tag(target)
    path = dist / f"hyprial-{VERSION}-cp312-abi3-{tag}.whl"
    member = f"hyprial/_core/{native_engine.wheel_module_name(target)}"
    data = payload if payload is not None else module_bytes(target)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, data)
        if duplicate:
            archive.writestr(member, data)
        for extra in extra_members:
            archive.writestr(extra, b"")
        archive.writestr(f"hyprial-{VERSION}.dist-info/WHEEL", b"Wheel-Version: 1.0\n")
    return path


def make_receipt(
    tmp_path: Path,
    target: str,
    *,
    smoke_abi: str = "1",
    installed_bytes: bytes | None = None,
    expect: int = 0,
    **wheel_options,
) -> Path:
    wheel = write_wheel(tmp_path / "dist", target, **wheel_options)
    member = f"hyprial/_core/{native_engine.wheel_module_name(target)}"
    with zipfile.ZipFile(wheel) as archive:
        body = archive.read(member)
    installed = tmp_path / "installed" / native_engine.wheel_module_name(target)
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_bytes(body if installed_bytes is None else installed_bytes)
    python = tmp_path / "fake-python"
    python.write_text(f"#!/bin/sh\nprintf '{smoke_abi}\\n'\n", encoding="utf-8")
    python.chmod(0o755)
    output = tmp_path / "prebuilt" / target.replace("/", "-")
    result = assembly.main(
        [
            "receipt",
            "--wheel",
            str(wheel),
            "--target",
            target,
            "--source-sha",
            SOURCE_SHA,
            "--output",
            str(output),
            "--installed-module",
            str(installed),
            "--python",
            str(python),
            "--smoke",
            "pass",
        ]
    )
    assert result == expect
    return output


def read_receipt(folder: Path) -> dict:
    return json.loads((folder / "receipt.json").read_text(encoding="utf-8"))


def write_receipt(folder: Path, document: dict) -> None:
    (folder / "receipt.json").write_text(json.dumps(document), encoding="utf-8")


def test_receipt_then_assemble_round_trip(tmp_path, monkeypatch):
    targets = ["darwin/arm64", "linux/amd64", "windows/amd64"]
    for target in targets:
        make_receipt(tmp_path, target)
    directory = tmp_path / "prebuilt"
    assert (
        assembly.main(
            [
                "assemble",
                "--directory",
                str(directory),
                "--source-sha",
                SOURCE_SHA,
                *[arg for target in targets for arg in ("--target", target)],
            ]
        )
        == 0
    )
    manifest = json.loads(
        (directory / native_engine.PREBUILT_MANIFEST).read_text(encoding="utf-8")
    )
    assert manifest["schema"] == native_engine.PREBUILT_SCHEMA
    assert manifest["source_sha256"] == native_engine.source_digest(REPO)
    assert sorted(manifest["targets"]) == sorted(targets)
    for target in targets:
        body = module_bytes(target)
        entry = manifest["targets"][target]
        assert entry["module"] == native_engine.wheel_module_name(target)
        assert entry["sha256"] == hashlib.sha256(body).hexdigest()
        assert entry["size"] == len(body)
        assert entry["engine_abi_version"] == native_engine.ENGINE_ABI_VERSION
        assert entry["platform_tag"] == platform_tag(target)
        monkeypatch.setenv(native_engine.PREBUILT_ENV, str(directory))
        verified = native_engine.verify_prebuilt(REPO, target)
        assert verified["sha256"] == entry["sha256"]
        assert verified["platform_tag"] == platform_tag(target)


def test_receipt_rejects_wrong_wheel_platform(tmp_path):
    wheel = write_wheel(
        tmp_path / "dist", "linux/amd64", wheel_platform=platform_tag("darwin/arm64")
    )
    assert (
        assembly.main(
            [
                "receipt",
                "--wheel",
                str(wheel),
                "--target",
                "linux/amd64",
                "--source-sha",
                SOURCE_SHA,
                "--output",
                str(tmp_path / "out"),
                "--smoke",
                "pass",
            ]
        )
        == 1
    )
    assert not (tmp_path / "out" / "receipt.json").exists()


def test_receipt_rejects_non_abi3_wheel_name(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel = dist / f"hyprial-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("hyprial/_core/actors.pyd", module_bytes("windows/amd64"))
    assert (
        assembly.main(
            [
                "receipt",
                "--wheel",
                str(wheel),
                "--target",
                "windows/amd64",
                "--source-sha",
                SOURCE_SHA,
                "--output",
                str(tmp_path / "out"),
                "--smoke",
                "pass",
            ]
        )
        == 1
    )


def test_receipt_rejects_missing_module(tmp_path):
    wheel = write_wheel(tmp_path / "dist", "linux/amd64")
    with zipfile.ZipFile(wheel, "a") as archive:
        pass
    dist = tmp_path / "dist"
    empty = dist / f"hyprial-{VERSION}-cp312-abi3-{platform_tag('linux/amd64')}.whl"
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("hyprial/__init__.py", b"")
    assert (
        assembly.main(
            [
                "receipt",
                "--wheel",
                str(empty),
                "--target",
                "linux/amd64",
                "--source-sha",
                SOURCE_SHA,
                "--output",
                str(tmp_path / "out"),
                "--smoke",
                "pass",
            ]
        )
        == 1
    )


def test_receipt_rejects_duplicate_module(tmp_path):
    wheel = write_wheel(tmp_path / "dist", "linux/amd64", duplicate=True)
    assert (
        assembly.main(
            [
                "receipt",
                "--wheel",
                str(wheel),
                "--target",
                "linux/amd64",
                "--source-sha",
                SOURCE_SHA,
                "--output",
                str(tmp_path / "out"),
                "--smoke",
                "pass",
            ]
        )
        == 1
    )


def test_receipt_rejects_second_actor_payload(tmp_path):
    wheel = write_wheel(
        tmp_path / "dist",
        "windows/amd64",
        extra_members=("hyprial/_core/actors.abi3.so",),
    )
    assert (
        assembly.main(
            [
                "receipt",
                "--wheel",
                str(wheel),
                "--target",
                "windows/amd64",
                "--source-sha",
                SOURCE_SHA,
                "--output",
                str(tmp_path / "out"),
                "--smoke",
                "pass",
            ]
        )
        == 1
    )


def test_receipt_rejects_import_check_failure(tmp_path):
    output = make_receipt(tmp_path, "windows/amd64", smoke_abi="2", expect=1)
    assert not (output / "receipt.json").exists()


def test_receipt_rejects_installed_module_mismatch(tmp_path):
    output = make_receipt(
        tmp_path, "windows/amd64", installed_bytes=b"not the wheel module", expect=1
    )
    assert not (output / "receipt.json").exists()


def test_receipt_requires_isolated_install(tmp_path):
    wheel = write_wheel(tmp_path / "dist", "windows/amd64")
    assert (
        assembly.main(
            [
                "receipt",
                "--wheel",
                str(wheel),
                "--target",
                "windows/amd64",
                "--source-sha",
                SOURCE_SHA,
                "--output",
                str(tmp_path / "out"),
                "--python",
                "3.12",
                "--smoke",
                "pass",
            ]
        )
        == 1
    )
    assert not (tmp_path / "out" / "receipt.json").exists()


def test_receipt_requires_passing_smoke(tmp_path):
    wheel = write_wheel(tmp_path / "dist", "windows/amd64")
    output = tmp_path / "out"
    assert (
        assembly.main(
            [
                "receipt",
                "--wheel",
                str(wheel),
                "--target",
                "windows/amd64",
                "--source-sha",
                SOURCE_SHA,
                "--output",
                str(output),
                "--smoke",
                "fail",
            ]
        )
        == 1
    )
    assert not (output / "receipt.json").exists()


def _assemble(directory: Path, targets: list[str]) -> int:
    return assembly.main(
        [
            "assemble",
            "--directory",
            str(directory),
            "--source-sha",
            SOURCE_SHA,
            *[arg for target in targets for arg in ("--target", target)],
        ]
    )


def test_assemble_rejects_mixed_source_identity(tmp_path):
    first = make_receipt(tmp_path, "linux/amd64")
    second = make_receipt(tmp_path, "windows/amd64")
    document = read_receipt(second)
    document["source_sha"] = "f" * 40
    write_receipt(second, document)
    assert _assemble(tmp_path / "prebuilt", ["linux/amd64", "windows/amd64"]) == 1
    assert first.is_dir()


def test_assemble_rejects_tampered_module(tmp_path):
    make_receipt(tmp_path, "linux/amd64")
    make_receipt(tmp_path, "windows/amd64")
    module = (
        tmp_path
        / "prebuilt"
        / "windows-amd64"
        / native_engine.wheel_module_name("windows/amd64")
    )
    module.write_bytes(module_bytes("windows/amd64") + b"\0")
    assert _assemble(tmp_path / "prebuilt", ["linux/amd64", "windows/amd64"]) == 1


def test_assemble_rejects_missing_target(tmp_path):
    make_receipt(tmp_path, "linux/amd64")
    assert _assemble(tmp_path / "prebuilt", ["linux/amd64", "darwin/arm64"]) == 1


def test_assemble_rejects_wrong_abi_version(tmp_path):
    folder = make_receipt(tmp_path, "linux/amd64")
    document = read_receipt(folder)
    document["engine_abi_version"] = 2
    write_receipt(folder, document)
    assert _assemble(tmp_path / "prebuilt", ["linux/amd64"]) == 1


def test_assemble_rejects_wrong_architecture(tmp_path):
    folder = make_receipt(tmp_path, "darwin/arm64")
    document = read_receipt(folder)
    module = folder / native_engine.wheel_module_name("darwin/arm64")
    body = module_bytes("darwin/amd64")
    module.write_bytes(body)
    document["sha256"] = hashlib.sha256(body).hexdigest()
    document["size"] = len(body)
    write_receipt(folder, document)
    assert _assemble(tmp_path / "prebuilt", ["darwin/arm64"]) == 1


@pytest.mark.parametrize(
    "target",
    ["darwin/amd64", "darwin/arm64", "linux/amd64", "linux/arm64", "windows/amd64"],
)
def test_receipt_accepts_every_release_target(tmp_path, target):
    make_receipt(tmp_path, target)
    assert _assemble(tmp_path / "prebuilt", [target]) == 0
