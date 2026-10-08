"""Versioned, non-secret execution selection; independent of harness identity."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re


@dataclass(frozen=True, slots=True)
class SmolvmRuntimeSpec:
    smolvm_path: str
    smolvm_sha256: str
    rootfs_path: str
    rootfs_digest: str
    resize2fs_path: str
    resize2fs_sha256: str
    guest_command: tuple[str, ...]
    guest_python: str
    smolvm_bundle_digest: str

    @classmethod
    def from_json(cls, value: object) -> SmolvmRuntimeSpec:
        keys = {
            "version",
            "kind",
            "smolvmPath",
            "smolvmSha256",
            "rootfsPath",
            "rootfsDigest",
            "resize2fsPath",
            "resize2fsSha256",
            "guestCommand",
            "guestPython",
            "smolvmBundleDigest",
        }
        if not isinstance(value, dict) or set(value) != keys:
            raise ValueError("executionRuntime requires exactly the smolvm v1 keys")
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or value["kind"] != "smolvm"
        ):
            raise ValueError("unsupported executionRuntime kind/version")
        for key in ("smolvmPath", "rootfsPath", "resize2fsPath", "guestPython"):
            _path(value[key], key)
        for key in (
            "smolvmSha256",
            "rootfsDigest",
            "resize2fsSha256",
            "smolvmBundleDigest",
        ):
            if not isinstance(value[key], str) or not re.fullmatch(
                r"[0-9a-f]{64}", value[key]
            ):
                raise ValueError(f"{key} must be a lowercase SHA256")
        command = value["guestCommand"]
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(x, str) or not x or "\0" in x for x in command)
        ):
            raise ValueError("guestCommand must be nonempty argv")
        _path(command[0], "guestCommand[0]")
        return cls(
            value["smolvmPath"],
            value["smolvmSha256"],
            value["rootfsPath"],
            value["rootfsDigest"],
            value["resize2fsPath"],
            value["resize2fsSha256"],
            tuple(command),
            value["guestPython"],
            value["smolvmBundleDigest"],
        )

    def to_json(self) -> dict[str, object]:
        return dict(
            version=1,
            kind="smolvm",
            smolvmPath=self.smolvm_path,
            smolvmSha256=self.smolvm_sha256,
            rootfsPath=self.rootfs_path,
            rootfsDigest=self.rootfs_digest,
            resize2fsPath=self.resize2fs_path,
            resize2fsSha256=self.resize2fs_sha256,
            guestCommand=list(self.guest_command),
            guestPython=self.guest_python,
            smolvmBundleDigest=self.smolvm_bundle_digest,
        )


def _path(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "\0" in value
        or "\n" in value
        or ".." in PurePosixPath(value).parts
        or str(PurePosixPath(value)) != value
    ):
        raise ValueError(f"{label} must be a canonical absolute path")


def parse_execution_runtime(value: object) -> SmolvmRuntimeSpec | None:
    return None if value is None else SmolvmRuntimeSpec.from_json(value)


def has_execution_runtime(value: object) -> bool:
    """Also cover persisted lifecycle resource/receipt payloads."""
    if isinstance(value, dict):
        return value.get("executionRuntime") is not None or any(
            has_execution_runtime(v) for v in value.values()
        )
    return isinstance(value, list) and any(has_execution_runtime(v) for v in value)
