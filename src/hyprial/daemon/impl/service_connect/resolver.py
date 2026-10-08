"""Descriptor-checked loading for daemon-private service access records."""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Mapping

from hyprial.kernel import ipc_errors

from .models import (
    AccessSnapshot,
    CatalogTrust,
    ProtectedDevice,
    ServiceConnectError,
    invalid_registry,
)


_MAX_RECORD_BYTES = 1024 * 1024


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate protected record key")
        result[key] = value
    return result


class ProtectedResolver:
    """Read one atomic protected record without retaining a last-good secret."""

    def __init__(self, path: str | Path, *, expected_uid: int | None = None) -> None:
        self.path = Path(path)
        self._expected_uid = os.getuid() if expected_uid is None else expected_uid
        self._lock = threading.Lock()
        self._seen: dict[str, tuple[int, tuple[object, ...]]] = {}

    def load(self) -> AccessSnapshot:
        raw = self._read_protected()
        try:
            value = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(value, Mapping) or set(value) != {
                "schemaVersion",
                "catalogTrust",
                "devices",
            }:
                raise invalid_registry("protected service record is invalid")
            if value["schemaVersion"] != 1:
                raise invalid_registry("protected service record is invalid")
            trust = self._parse_trust(value["catalogTrust"])
            devices_value = value["devices"]
            if not isinstance(devices_value, list):
                raise invalid_registry("protected service record is invalid")
            devices = tuple(ProtectedDevice.from_json(item) for item in devices_value)
            if len({device.device_id for device in devices}) != len(devices):
                raise invalid_registry("protected service record is invalid")
            self._check_revisions(devices)
            return AccessSnapshot(trust, devices)
        except ServiceConnectError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise invalid_registry("protected service record is invalid") from error

    def resolve_device(self, device_id: str, remote_port: int) -> ProtectedDevice:
        return self.load().resolve_device(device_id, remote_port)

    def _read_protected(self) -> str:
        directory_fd = file_fd = None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        try:
            if not self.path.is_absolute():
                raise OSError("protected service path must be absolute")
            directory_fd = os.open(
                "/", flags | getattr(os, "O_DIRECTORY", 0) | nofollow
            )
            for part in self.path.parent.parts[1:]:
                next_fd = os.open(
                    part,
                    flags | getattr(os, "O_DIRECTORY", 0) | nofollow,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = next_fd
            directory = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(directory.st_mode)
                or stat.S_IMODE(directory.st_mode) != 0o700
                or directory.st_uid != self._expected_uid
            ):
                raise OSError("unsafe protected service directory")
            file_fd = os.open(
                self.path.name,
                flags | nofollow,
                dir_fd=directory_fd,
            )
            info = os.fstat(file_fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != self._expected_uid
                or info.st_size > _MAX_RECORD_BYTES
            ):
                raise OSError("unsafe protected service file")
            chunks: list[bytes] = []
            remaining = _MAX_RECORD_BYTES + 1
            while remaining:
                chunk = os.read(file_fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > _MAX_RECORD_BYTES:
                raise OSError("protected service file is too large")
            return payload.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise ServiceConnectError(
                ipc_errors.SERVICE_DEVICE_UNAVAILABLE,
                "protected service access unavailable",
            ) from error
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    def _parse_trust(self, value: object) -> CatalogTrust | None:
        if value is None:
            return None
        if (
            not isinstance(value, Mapping)
            or set(value) != {"spaceId", "owner"}
            or not isinstance(value["spaceId"], str)
            or not value["spaceId"]
            or not isinstance(value["owner"], str)
            or not value["owner"]
        ):
            raise invalid_registry("protected catalog trust is invalid")
        return CatalogTrust(value["spaceId"], value["owner"])

    def _check_revisions(self, devices: tuple[ProtectedDevice, ...]) -> None:
        with self._lock:
            for device in devices:
                previous = self._seen.get(device.device_id)
                if (
                    previous is not None
                    and previous[0] == device.generation
                    and previous[1] != device.fingerprint()
                ):
                    raise invalid_registry(
                        "protected service record revision is inconsistent"
                    )
            self._seen.update(
                {
                    device.device_id: (device.generation, device.fingerprint())
                    for device in devices
                }
            )
