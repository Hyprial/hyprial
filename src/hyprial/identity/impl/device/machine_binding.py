"""Bind the local Tailcat identity to one physical machine.

The fingerprint is a SHA-256 digest of an OS machine identifier, never key
material.  Sources are Linux machine-id, macOS IOPlatformUUID, and Windows
MachineGuid.  A digest of hostname plus the hardware node id is the documented
last-resort fallback when the platform identifier is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import uuid

from hyprial.kernel import atomic_json_write

MACHINE_BINDING_RELPATH = "state/tailcat/machine-binding.json"
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class MachineBinding:
    machine_fingerprint: str
    first_published_at: str | None = None
    last_conflict_token: str | None = None
    last_reason: str | None = None
    pending_event: dict[str, str] | None = None

    def as_record(self) -> dict[str, object]:
        value: dict[str, object] = {
            "version": 1,
            "machineFingerprint": self.machine_fingerprint,
            "firstPublishedAt": self.first_published_at,
        }
        if self.last_conflict_token is not None:
            value["lastConflictToken"] = self.last_conflict_token
        if self.last_reason is not None:
            value["lastReason"] = self.last_reason
        if self.pending_event is not None:
            value["pendingEvent"] = dict(self.pending_event)
        return value

    @classmethod
    def from_record(cls, value: object) -> "MachineBinding":
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("machine binding must be a version-1 object")
        fingerprint = value.get("machineFingerprint")
        if (
            not isinstance(fingerprint, str)
            or _FINGERPRINT.fullmatch(fingerprint) is None
        ):
            raise ValueError(
                "machine binding fingerprint must be 64 lowercase hex digits"
            )
        first = value.get("firstPublishedAt")
        token = value.get("lastConflictToken")
        reason = value.get("lastReason")
        pending = value.get("pendingEvent")
        if first is not None and not isinstance(first, str):
            raise ValueError(
                "machine binding firstPublishedAt must be a string or null"
            )
        if token is not None and (
            not isinstance(token, str) or _FINGERPRINT.fullmatch(token) is None
        ):
            raise ValueError(
                "machine binding conflict token must be 64 lowercase hex digits"
            )
        if reason is not None and reason not in {
            "created",
            "adopted",
            "copied-home",
            "directory-conflict",
        }:
            raise ValueError("machine binding reason is invalid")
        if pending is not None and (
            not isinstance(pending, dict)
            or set(pending)
            != {"deviceId", "oldIdentityHash", "newIdentityHash", "reason"}
            or not all(isinstance(item, str) for item in pending.values())
        ):
            raise ValueError("machine binding pending event is invalid")
        return cls(fingerprint, first, token, reason, pending)


def machine_binding_path(home: Path) -> Path:
    return Path(home) / MACHINE_BINDING_RELPATH


def read_machine_binding(home: Path) -> MachineBinding | None:
    path = machine_binding_path(home)
    try:
        value = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ValueError(f"machine binding at {path} is unreadable: {error}") from error
    return MachineBinding.from_record(value)


def write_machine_binding(home: Path, binding: MachineBinding) -> None:
    atomic_json_write(machine_binding_path(home), binding.as_record())


def mark_first_published(home: Path, published_at: str) -> MachineBinding:
    binding = read_machine_binding(home)
    if binding is None:
        binding = MachineBinding(machine_fingerprint(), last_reason="adopted")
    if binding.first_published_at is None:
        binding = replace(binding, first_published_at=published_at)
        write_machine_binding(home, binding)
    return binding


def consume_rotation_event(home: Path) -> dict[str, str] | None:
    binding = read_machine_binding(home)
    if binding is None or binding.pending_event is None:
        return None
    event = dict(binding.pending_event)
    write_machine_binding(home, replace(binding, pending_event=None))
    return event


def machine_fingerprint() -> str:
    """Return a stable, non-secret digest from the best platform source."""

    source, value = _machine_identifier()
    return hashlib.sha256(f"{source}\0{value}".encode("utf-8")).hexdigest()


def _machine_identifier() -> tuple[str, str]:
    system = platform.system().lower()
    if system == "linux":
        for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
            try:
                value = path.read_text("utf-8").strip()
            except OSError:
                continue
            if value:
                return "linux-machine-id", value
    elif system == "darwin":
        try:
            completed = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                check=False,
                timeout=3,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            completed = None
        if completed is not None and completed.returncode == 0:
            match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', completed.stdout)
            if match is not None:
                return "darwin-platform-uuid", match.group(1)
    elif system == "windows":
        try:
            import winreg  # type: ignore[import-not-found]

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            ) as key:
                value, _kind = winreg.QueryValueEx(key, "MachineGuid")
            if isinstance(value, str) and value:
                return "windows-machine-guid", value
        except (ImportError, OSError):
            pass
    return (
        "fallback-host-node",
        f"{socket.gethostname()}\0{uuid.getnode():012x}\0{os.name}",
    )
