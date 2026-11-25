#!/usr/bin/env python3
"""Verify the complete native tailcat wheel set before a release publish."""

from __future__ import annotations

import os
import re
import runpy
import stat
import struct
import sys
import zipfile
from pathlib import Path

_SOURCE_REMOTE = runpy.run_path(str(Path(__file__).with_name("wheel_source_remote.py")))
SourceRemoteError = _SOURCE_REMOTE["SourceRemoteError"]
TARGETS = {
    "darwin/arm64": ("macosx_13_0_arm64", "hyprial/_bin/hyprial-tailcat", "macho", 0x0100000C),
    "darwin/amd64": ("macosx_13_0_x86_64", "hyprial/_bin/hyprial-tailcat", "macho", 0x01000007),
    "linux/amd64": (
        "manylinux_2_17_x86_64.manylinux2014_x86_64",
        "hyprial/_bin/hyprial-tailcat",
        "elf",
        62,
    ),
    "linux/arm64": (
        "manylinux_2_17_aarch64.manylinux2014_aarch64",
        "hyprial/_bin/hyprial-tailcat",
        "elf",
        183,
    ),
    "windows/amd64": ("win_amd64", "hyprial/_bin/hyprial-tailcat.exe", "pe", 0x8664),
}
ALLOW_MISSING_ENV = "HYPRIAL_TAILCAT_ALLOW_MISSING"
SUPPORTED_ABIS = ("py3-none", "cp312-abi3")


class WheelError(ValueError):
    """A wheel set or a native payload violates the release contract."""


def fail(message: str) -> None:
    raise WheelError(message)


def verify_path(name: str) -> None:
    if "\\" in name or "\x00" in name:
        fail(f"unsafe wheel member path: {name!r}")
    checked_name = name[:-1] if name.endswith("/") else name
    if checked_name.startswith("/") or any(
        part in {"", ".", ".."} for part in checked_name.split("/")
    ):
        fail(f"unsafe wheel member path: {name!r}")
    if re.match(r"^[A-Za-z]:", name):
        fail(f"drive-qualified wheel member path: {name!r}")


def verify_elf(data: bytes, machine: int) -> None:
    if len(data) < 20 or data[:4] != b"\x7fELF":
        fail("tailcat payload is not an ELF binary")
    if data[5] == 1:
        actual = struct.unpack_from("<H", data, 18)[0]
    elif data[5] == 2:
        actual = struct.unpack_from(">H", data, 18)[0]
    else:
        fail("ELF binary has invalid byte order")
    if data[4] != 2 or actual != machine:
        fail(f"ELF architecture mismatch: class={data[4]} machine={actual}")


def verify_pe(data: bytes, machine: int) -> None:
    if len(data) < 64 or data[:2] != b"MZ":
        fail("tailcat payload is not a PE binary")
    offset = struct.unpack_from("<I", data, 0x3C)[0]
    if offset + 6 > len(data) or data[offset : offset + 4] != b"PE\0\0":
        fail("tailcat payload has an invalid PE header")
    actual = struct.unpack_from("<H", data, offset + 4)[0]
    if actual != machine:
        fail(f"PE architecture mismatch: machine={actual:#x}")


def verify_macho(data: bytes, machine: int, platform_tag: str) -> None:
    formats = {
        b"\xce\xfa\xed\xfe": ("<", False),
        b"\xfe\xed\xfa\xce": (">", False),
        b"\xcf\xfa\xed\xfe": ("<", True),
        b"\xfe\xed\xfa\xcf": (">", True),
    }
    if len(data) < 28 or data[:4] not in formats:
        fail("tailcat payload is not a thin Mach-O binary")
    endian, is_64 = formats[data[:4]]
    actual = struct.unpack_from(endian + "I", data, 4)[0]
    header_size = 32 if is_64 else 28
    ncmds, sizeofcmds = struct.unpack_from(endian + "II", data, 16)
    if actual != machine or header_size + sizeofcmds > len(data):
        fail(f"Mach-O architecture or load-command bounds mismatch: cputype={actual:#x}")
    floor_match = re.fullmatch(r"macosx_(\d+)_(\d+)_.*", platform_tag)
    if not floor_match:
        fail(f"invalid macOS wheel tag: {platform_tag}")
    floor = (int(floor_match.group(1)), int(floor_match.group(2)))
    if floor < (13, 0):
        fail(f"macOS wheel tag {platform_tag} is below Go 1.27's macOS 13.0 floor")

    minimum: tuple[int, int] | None = None
    cursor = header_size
    for _ in range(ncmds):
        if cursor + 8 > header_size + sizeofcmds:
            fail("truncated Mach-O load command")
        command, size = struct.unpack_from(endian + "II", data, cursor)
        if size < 8 or cursor + size > header_size + sizeofcmds:
            fail("invalid Mach-O load-command size")
        if command == 0x32 and size >= 24:  # LC_BUILD_VERSION
            build_platform = struct.unpack_from(endian + "I", data, cursor + 8)[0]
            if build_platform != 1:  # PLATFORM_MACOS
                fail(f"Mach-O declares Apple platform {build_platform}, expected macOS")
            packed = struct.unpack_from(endian + "I", data, cursor + 12)[0]
            minimum = (packed >> 16, (packed >> 8) & 0xFF)
        elif command == 0x24 and size >= 16:  # LC_VERSION_MIN_MACOSX
            packed = struct.unpack_from(endian + "I", data, cursor + 8)[0]
            minimum = (packed >> 16, (packed >> 8) & 0xFF)
        cursor += size
    if minimum is None:
        fail("Mach-O binary does not declare its minimum macOS version")
    if minimum > floor:
        fail(f"Mach-O minimum macOS {minimum[0]}.{minimum[1]} exceeds wheel tag {floor[0]}.{floor[1]}")


def verify_binary(data: bytes, kind: str, machine: int, platform_tag: str) -> None:
    if kind == "elf":
        verify_elf(data, machine)
    elif kind == "pe":
        verify_pe(data, machine)
    else:
        verify_macho(data, machine, platform_tag)


def expected_tags(platform_tag: str, abi: str = "py3-none") -> list[str]:
    """Expand the wheel filename's PEP 425 compressed platform set."""

    return [f"{abi}-{part}" for part in platform_tag.split(".")]


def verify_wheel(path: Path, version: str, target: str) -> None:
    platform_tag, binary_path, kind, machine = TARGETS[target]
    matching_abis = [
        abi for abi in SUPPORTED_ABIS
        if path.name == f"hyprial-{version}-{abi}-{platform_tag}.whl"
    ]
    if len(matching_abis) != 1:
        fail(f"unexpected wheel filename for {target}: {path.name!r}")
    abi = matching_abis[0]
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            for name in names:
                verify_path(name)
            if len(names) != len(set(names)):
                fail(f"duplicate ZIP member in {path.name}")
            members = [info for info in infos if not info.is_dir()]
            binaries = [info for info in members if info.filename.startswith("hyprial/_bin/")]
            if [info.filename for info in binaries] != [binary_path]:
                fail(f"{path.name} must contain only {binary_path} under hyprial/_bin/")
            binary_mode = (binaries[0].external_attr >> 16) & 0xFFFF
            if not stat.S_ISREG(binary_mode):
                fail(f"{path.name} {binary_path} must be a regular file")
            if not binary_mode & 0o111:
                fail(f"{path.name} {binary_path} must have executable mode")
            wheel_metadata = [info for info in members if info.filename.endswith(".dist-info/WHEEL")]
            if len(wheel_metadata) != 1:
                fail(f"{path.name} must have exactly one .dist-info/WHEEL")
            metadata = archive.read(wheel_metadata[0]).decode("utf-8")
            purelib = [
                line.removeprefix("Root-Is-Purelib: ")
                for line in metadata.splitlines()
                if line.startswith("Root-Is-Purelib: ")
            ]
            if purelib != ["false"]:
                fail(f"{path.name} must declare Root-Is-Purelib: false")
            tags = [line[5:] for line in metadata.splitlines() if line.startswith("Tag: ")]
            expected = expected_tags(platform_tag, abi)
            if len(tags) != len(expected) or set(tags) != set(expected):
                fail(f"{path.name} must declare exactly the tag set {expected!r} without duplicates, got {tags!r}")
            package_metadata = [info for info in members if info.filename.endswith(".dist-info/METADATA")]
            if len(package_metadata) != 1:
                fail(f"{path.name} must have exactly one .dist-info/METADATA")
            fields = {}
            for line in archive.read(package_metadata[0]).decode("utf-8").splitlines():
                if ": " in line:
                    key, value = line.split(": ", 1)
                    fields.setdefault(key, []).append(value)
            if fields.get("Name") != ["hyprial"] or fields.get("Version") != [version]:
                fail(f"{path.name} METADATA Name/Version do not match hyprial {version}")
            verify_binary(archive.read(binary_path), kind, machine, platform_tag)
            try:
                _SOURCE_REMOTE["check_archive"](archive, path.name)
            except SourceRemoteError as error:
                fail(str(error))
            actors = [info for info in members if info.filename.startswith("hyprial/_core/")]
            if abi == "py3-none" and actors:
                fail(f"{path.name} cannot label a native actor payload as py3-none")
            if abi == "cp312-abi3":
                actor_path = "hyprial/_core/" + ("actors.pyd" if kind == "pe" else "actors.abi3.so")
                if [info.filename for info in actors] != [actor_path]:
                    fail(f"{path.name} must contain exactly the native actor module {actor_path}")
                actor_mode = (actors[0].external_attr >> 16) & 0xFFFF
                if not stat.S_ISREG(actor_mode):
                    fail(f"{path.name} native actor module must be a regular file")
                verify_binary(archive.read(actor_path), kind, machine, platform_tag)
    except zipfile.BadZipFile as error:
        fail(f"invalid wheel ZIP {path.name}: {error}")


def verify_set(dist: Path, version: str, commit: str) -> list[Path]:
    if os.environ.get(ALLOW_MISSING_ENV) == "1":
        fail(f"release verification forbids {ALLOW_MISSING_ENV}=1")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        fail("HYPRIAL_TAILCAT_SOURCE_COMMIT must be a full 40-character Git SHA")
    stamped = re.search(r"\+g([0-9a-fA-F]{7})$", version)
    if not stamped or stamped.group(1).lower() != commit[:7].lower():
        fail("wheel version must carry +g<short7> matching HYPRIAL_TAILCAT_SOURCE_COMMIT")
    wheels = sorted(dist.glob("*.whl"))
    actual = {path.name for path in wheels}
    matching_abis = [
        abi for abi in SUPPORTED_ABIS
        if actual == {f"hyprial-{version}-{abi}-{platform_tag}.whl" for platform_tag, *_ in TARGETS.values()}
    ]
    if len(wheels) != len(TARGETS) or len(matching_abis) != 1:
        fail(f"dist must contain exactly the five target wheels with one supported ABI; got={sorted(actual)!r}")
    abi = matching_abis[0]
    by_name = {path.name: path for path in wheels}
    verified = []
    for target, (platform_tag, *_rest) in TARGETS.items():
        path = by_name[f"hyprial-{version}-{abi}-{platform_tag}.whl"]
        verify_wheel(path, version, target)
        verified.append(path)
    return verified


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: verify_tailcat_wheels.py DIST_DIR", file=sys.stderr)
        return 2
    version = os.environ.get("HYPRIAL_TAILCAT_WHEEL_VERSION", "")
    commit = os.environ.get("HYPRIAL_TAILCAT_SOURCE_COMMIT", "")
    try:
        wheels = verify_set(Path(sys.argv[1]), version, commit)
    except (OSError, UnicodeError, WheelError, struct.error) as error:
        print(f"tailcat wheel verification failed: {error}", file=sys.stderr)
        return 1
    print(f"verified {len(wheels)} tailcat wheels for version {version} from {commit}")
    for wheel in wheels:
        print(f"  {wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
