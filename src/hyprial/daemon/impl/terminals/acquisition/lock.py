"""Pinned hc release metadata and target validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from importlib import resources
import re
from pathlib import PureWindowsPath
from typing import Any, Mapping

from hyprial.kernel import host_hc_release_target


class HcAcquisitionError(RuntimeError):
    """Raised when a pinned hc release cannot be used safely."""


@dataclass(frozen=True)
class HcTargetSpec:
    """The immutable download and verification facts for one platform."""

    url: str
    sha256: str
    size: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "HcTargetSpec":
        url = value.get("url")
        digest = value.get("sha256")
        size = value.get("size")
        if not isinstance(url, str) or not url:
            raise HcAcquisitionError("hc target url must be a non-empty string")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise HcAcquisitionError("hc target sha256 must be 64 lowercase hex characters")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise HcAcquisitionError("hc target size must be positive")
        return cls(url=url, sha256=digest, size=size)

    def to_mapping(self) -> dict[str, Any]:
        return {"url": self.url, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class HcLock:
    """The pinned hc version and its platform-specific release metadata."""

    version: str
    generation: int
    targets: Mapping[str, HcTargetSpec]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "HcLock":
        version = value.get("version")
        generation = value.get("generation")
        raw_targets = value.get("targets")
        if not isinstance(version, str) or not _safe_version(version):
            raise HcAcquisitionError("hc lock version must be one safe path segment")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise HcAcquisitionError("hc lock generation must be positive")
        if not isinstance(raw_targets, Mapping) or not raw_targets:
            raise HcAcquisitionError("hc lock targets must be a non-empty mapping")
        targets: dict[str, HcTargetSpec] = {}
        for name, raw_target in raw_targets.items():
            if not isinstance(name, str) or not isinstance(raw_target, Mapping):
                raise HcAcquisitionError("hc lock target entries are malformed")
            if not name or "/" in name or "\\" in name:
                raise HcAcquisitionError("hc lock target name is unsafe")
            targets[name] = HcTargetSpec.from_mapping(raw_target)
        return cls(version=version, generation=generation, targets=targets)

    @classmethod
    def from_json(cls, body: str) -> "HcLock":
        try:
            value = json.loads(body)
        except json.JSONDecodeError as error:
            raise HcAcquisitionError("hc lock is not valid JSON") from error
        if not isinstance(value, Mapping):
            raise HcAcquisitionError("hc lock must be a JSON object")
        return cls.from_mapping(value)

    def target_for(self, system: str | None = None, machine: str | None = None) -> HcTargetSpec:
        target_name = host_hc_release_target(system, machine)
        if target_name is None or target_name not in self.targets:
            raise HcAcquisitionError("no pinned hc target exists for this host")
        return self.targets[target_name]

    def target(self, name: str) -> HcTargetSpec:
        try:
            return self.targets[name]
        except KeyError as error:
            raise HcAcquisitionError(f"unknown hc target: {name}") from error

    def to_mapping(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generation": self.generation,
            "targets": {name: target.to_mapping() for name, target in self.targets.items()},
        }


def _safe_version(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and ":" not in value
        and not PureWindowsPath(value).drive
    )


def load_bundled_hc_lock() -> HcLock:
    """Load and validate the lock file shipped with the daemon package."""
    try:
        body = (
            resources.files("hyprial.daemon.impl.terminals")
            .joinpath("hc-lock.json")
            .read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError) as error:
        raise HcAcquisitionError("bundled hc lock is unavailable") from error
    lock = HcLock.from_json(body)
    for target in lock.targets.values():
        if not target.url.startswith("https://"):
            raise HcAcquisitionError("production hc lock urls must use https")
    return lock


__all__ = ["HcAcquisitionError", "HcLock", "HcTargetSpec", "load_bundled_hc_lock"]
