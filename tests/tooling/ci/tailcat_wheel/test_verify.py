from __future__ import annotations

import json
import struct
import warnings
import zipfile
from pathlib import Path

import pytest

from scripts.ci.verify_tailcat_wheels import (
    TARGETS,
    WheelError,
    verify_macho,
    verify_set,
    verify_wheel,
)

PUBLIC_REMOTE = "https://github.com/Hyprial/hyprial.git"
VERSION = "0.4.6+g1234567"
COMMIT = "1234567" + "0" * 33


def native_header(kind: str, machine: int) -> bytes:
    if kind == "elf":
        header = bytearray(20)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", header, 18, machine)
        return bytes(header)
    if kind == "pe":
        header = bytearray(70)
        header[:2] = b"MZ"
        struct.pack_into("<I", header, 0x3C, 64)
        header[64:68] = b"PE\0\0"
        struct.pack_into("<H", header, 68, machine)
        return bytes(header)
    header = bytearray(56)
    header[:4] = b"\xcf\xfa\xed\xfe"
    struct.pack_into("<IiiIIII", header, 4, machine, 0, 2, 1, 24, 0, 0)
    struct.pack_into("<IIIIII", header, 32, 0x32, 24, 1, (13 << 16), (13 << 16), 0)
    return bytes(header)


def write_wheel(
    dist: Path,
    target: str,
    *,
    payload: bytes | None = None,
    duplicate: bool = False,
    duplicate_tag: bool = False,
    traversal: bool = False,
    include_gui_script: bool = True,
    abi: str = "py3-none",
    include_actor: bool = True,
    source_remote: str | None = PUBLIC_REMOTE,
) -> Path:
    platform_tag, binary_path, kind, machine = TARGETS[target]
    wheel_name = f"hyprial-{VERSION}-{abi}-{platform_tag}.whl"
    path = dist / wheel_name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = payload if payload is not None else native_header(kind, machine)
    metadata_path = f"hyprial-{VERSION}.dist-info/WHEEL"
    package_metadata_path = f"hyprial-{VERSION}.dist-info/METADATA"
    expanded_platforms = platform_tag.split(".")
    metadata = "Wheel-Version: 1.0\nRoot-Is-Purelib: false\n" + "".join(
        f"Tag: {abi}-{platform}\n" for platform in expanded_platforms
    )
    if duplicate_tag:
        metadata += f"Tag: {abi}-{expanded_platforms[0]}\n"
    with zipfile.ZipFile(path, "w") as archive:
        files = [
            (binary_path, data, 0o100755),
            (metadata_path, metadata.encode(), 0o100644),
            (package_metadata_path, f"Metadata-Version: 2.4\nName: hyprial\nVersion: {VERSION}\n".encode(), 0o100644),
        ]
        if source_remote is not None:
            manifest = {"schemaVersion": 1, "sourceRemote": source_remote}
            files.append(("hyprial/_gui/release.json", json.dumps(manifest).encode(), 0o100644))
        if include_gui_script:
            files.append(("hyprial/_gui/scripts/start-gui.sh", b"#!/bin/sh\nexit 0\n", 0o100755))
        if abi == "cp312-abi3" and include_actor:
            actor_name = "actors.pyd" if kind == "pe" else "actors.abi3.so"
            files.append((f"hyprial/_core/{actor_name}", native_header(kind, machine), 0o100755))
        for name, content, mode in files:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, content)
        if duplicate:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(binary_path, data)
        if traversal:
            archive.writestr("hyprial/../../escape", b"x")
    return path


def complete_set(dist: Path) -> None:
    for target in TARGETS:
        write_wheel(dist, target)


def test_complete_set_checks_exact_five_targets_and_source_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete_set(tmp_path)
    monkeypatch.delenv("HYPRIAL_TAILCAT_ALLOW_MISSING", raising=False)
    assert len(verify_set(tmp_path, VERSION, COMMIT)) == 5


def test_native_set_preserves_cp312_abi3_tags_and_actor_payloads(tmp_path: Path) -> None:
    for target in TARGETS:
        write_wheel(tmp_path, target, abi="cp312-abi3")
    assert len(verify_set(tmp_path, VERSION, COMMIT)) == 5


def test_mixed_legacy_native_set_is_rejected(tmp_path: Path) -> None:
    for index, target in enumerate(TARGETS):
        write_wheel(tmp_path, target, abi="py3-none" if index else "cp312-abi3")
    with pytest.raises(WheelError, match="exactly the five target wheels"):
        verify_set(tmp_path, VERSION, COMMIT)


def test_native_tag_without_actor_is_rejected(tmp_path: Path) -> None:
    path = write_wheel(tmp_path, "windows/amd64", abi="cp312-abi3", include_actor=False)
    with pytest.raises(WheelError, match="actor"):
        verify_wheel(path, VERSION, "windows/amd64")


def test_legacy_tag_cannot_hide_native_actor_payload(tmp_path: Path) -> None:
    path = write_wheel(tmp_path, "windows/amd64")
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("hyprial/_core/actors.pyd", native_header("pe", 0x8664))
    with pytest.raises(WheelError, match="native actor payload as py3-none"):
        verify_wheel(path, VERSION, "windows/amd64")


def test_native_actor_wrong_architecture_is_rejected(tmp_path: Path) -> None:
    path = write_wheel(tmp_path, "windows/amd64", abi="cp312-abi3", include_actor=False)
    with zipfile.ZipFile(path, "a") as archive:
        info = zipfile.ZipInfo("hyprial/_core/actors.pyd")
        info.external_attr = 0o100755 << 16
        archive.writestr(info, native_header("pe", 0xAA64))
    with pytest.raises(WheelError, match="architecture mismatch"):
        verify_wheel(path, VERSION, "windows/amd64")


def test_native_wheel_metadata_cannot_claim_legacy_tags(tmp_path: Path) -> None:
    old_path = write_wheel(tmp_path, "windows/amd64")
    path = old_path.with_name(old_path.name.replace("py3-none", "cp312-abi3"))
    old_path.rename(path)
    with pytest.raises(WheelError, match="tag set"):
        verify_wheel(path, VERSION, "windows/amd64")


def test_linux_wheel_accepts_pep425_expanded_compressed_platform_tags(tmp_path: Path) -> None:
    path = write_wheel(tmp_path, "linux/amd64", include_gui_script=False)
    verify_wheel(path, VERSION, "linux/amd64")


def test_linux_wheel_rejects_duplicate_expanded_tags(tmp_path: Path) -> None:
    path = write_wheel(
        tmp_path, "linux/amd64", include_gui_script=False, duplicate_tag=True
    )
    with pytest.raises(WheelError, match="without duplicates"):
        verify_wheel(path, VERSION, "linux/amd64")


def test_wheel_accepts_gui_start_script_executable_mode(tmp_path: Path) -> None:
    path = write_wheel(tmp_path, "darwin/arm64")
    verify_wheel(path, VERSION, "darwin/arm64")


@pytest.mark.parametrize(
    "remote", ["ssh://git@git.internal.invalid/org/repo.git", "", "https://github.com/Hyprial/hyprial", None]
)
def test_wheel_with_a_non_public_source_remote_is_refused(tmp_path: Path, remote: str | None) -> None:
    path = write_wheel(tmp_path, "linux/amd64", source_remote=remote)
    with pytest.raises(WheelError, match="release.json"):
        verify_wheel(path, VERSION, "linux/amd64")


def test_wheel_with_the_public_source_remote_is_accepted(tmp_path: Path) -> None:
    verify_wheel(write_wheel(tmp_path, "linux/amd64"), VERSION, "linux/amd64")


def test_missing_target_fails_before_publish(tmp_path: Path) -> None:
    for target in list(TARGETS)[:-1]:
        write_wheel(tmp_path, target)
    with pytest.raises(WheelError, match="exactly the five target wheels"):
        verify_set(tmp_path, VERSION, COMMIT)


@pytest.mark.parametrize("bad_archive", ["duplicate", "traversal"])
def test_bad_zip_members_fail(tmp_path: Path, bad_archive: str) -> None:
    target = "linux/amd64"
    complete_set(tmp_path)
    path = tmp_path / next(
        name for name in (item.name for item in tmp_path.glob("*.whl"))
        if TARGETS[target][0] in name
    )
    path.unlink()
    write_wheel(tmp_path, target, **{bad_archive: True})
    with pytest.raises(WheelError, match="duplicate ZIP member|unsafe wheel member path"):
        verify_set(tmp_path, VERSION, COMMIT)


def test_wrong_native_magic_fails(tmp_path: Path) -> None:
    complete_set(tmp_path)
    target = "windows/amd64"
    path = tmp_path / next(name for name in (item.name for item in tmp_path.glob("*.whl")) if "win_amd64" in name)
    path.unlink()
    write_wheel(tmp_path, target, payload=b"this is not a PE executable")
    with pytest.raises(WheelError, match="not a PE binary"):
        verify_set(tmp_path, VERSION, COMMIT)


def test_mac_tag_below_go_127_floor_fails() -> None:
    with pytest.raises(WheelError, match="below Go 1.27's macOS 13.0 floor"):
        verify_macho(native_header("macho", 0x0100000C), 0x0100000C, "macosx_11_0_arm64")


def test_allow_missing_development_escape_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete_set(tmp_path)
    monkeypatch.setenv("HYPRIAL_TAILCAT_ALLOW_MISSING", "1")
    with pytest.raises(WheelError, match="forbids HYPRIAL_TAILCAT_ALLOW_MISSING=1"):
        verify_set(tmp_path, VERSION, COMMIT)


def test_release_guard_and_product_share_the_public_remote_definition() -> None:
    from scripts.ci.wheel_source_remote import PUBLIC_SOURCE_REMOTE

    assert PUBLIC_SOURCE_REMOTE == PUBLIC_REMOTE
