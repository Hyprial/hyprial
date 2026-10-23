"""Provision and safely reissue the persistent Tailcat device key.

The private key never enters Python.  The sidecar writes the 0600 file and
prints only the public halves.  A machine binding detects copied homes before
the daemon advertises a duplicate identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from hyprial.identity import (
    DeviceRecord,
    MachineBinding,
    address_path,
    device_key_path,
    machine_fingerprint,
    read_device_record,
    read_machine_binding,
    write_device_record,
    write_machine_binding,
)
from hyprial.kernel import is_identity_id_segment

from .binary import (
    TailcatSidecarError,
    locate_tailcat_sidecar,
    no_console_window_kwargs,
)

_GENKEY_TIMEOUT_SECONDS = 15.0
_DEVICE_NAME_ENV = "HYPRIAL_DEVICE_NAME"


def ensure_device_key(
    home: Path,
    *,
    owner: str,
    device_id: str,
    name: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> DeviceRecord:
    """Create/adopt a key, rotating only when a copied home changes machines."""

    home = Path(home)
    existing = read_device_record(home)
    binding = _read_binding(home)
    fingerprint = machine_fingerprint()
    stable_existing = (
        existing is not None
        and device_key_path(home).is_file()
        and existing.owner == owner
        and existing.device_id == device_id
        and (name is None or existing.name == name.strip())
    )
    if binding is not None and binding.machine_fingerprint != fingerprint:
        return _provision(
            home,
            owner=owner,
            device_id=device_id,
            name=name,
            environ=environ,
            force=True,
            existing=existing,
            binding=binding,
            fingerprint=fingerprint,
            reason="copied-home",
        )
    if stable_existing:
        if binding is None:
            write_machine_binding(
                home,
                MachineBinding(
                    machine_fingerprint=fingerprint,
                    last_reason="adopted",
                ),
            )
        return existing
    record = _provision(
        home,
        owner=owner,
        device_id=device_id,
        name=name,
        environ=environ,
        force=False,
        existing=existing,
        binding=binding,
        fingerprint=fingerprint,
        reason="adopted" if existing is not None else "created",
    )
    if binding is None:
        write_machine_binding(
            home,
            MachineBinding(
                machine_fingerprint=fingerprint,
                last_reason="adopted" if existing is not None else "created",
            ),
        )
    return record


def reissue_device_key(
    home: Path,
    *,
    reason: str,
    conflict_token: str,
    environ: Mapping[str, str] | None = None,
) -> DeviceRecord | None:
    """Reissue once for a deterministic, redacted directory conflict token."""

    if reason != "directory-conflict":
        raise ValueError("unsupported Tailcat identity reissue reason")
    if len(conflict_token) != 64 or any(
        ch not in "0123456789abcdef" for ch in conflict_token
    ):
        raise ValueError("conflict token must be 64 lowercase hex digits")
    home = Path(home)
    existing = read_device_record(home)
    if existing is None:
        raise TailcatSidecarError("KEYGEN_FAILED", "cannot reissue a missing identity")
    binding = _read_binding(home)
    if binding is not None and binding.last_conflict_token == conflict_token:
        return None
    return _provision(
        home,
        owner=existing.owner,
        device_id=existing.device_id,
        name=existing.name,
        environ=environ,
        force=True,
        existing=existing,
        binding=binding,
        fingerprint=machine_fingerprint(),
        reason=reason,
        conflict_token=conflict_token,
    )


def _provision(
    home: Path,
    *,
    owner: str,
    device_id: str,
    name: str | None,
    environ: Mapping[str, str] | None,
    force: bool,
    existing: DeviceRecord | None,
    binding: MachineBinding | None,
    fingerprint: str,
    reason: str,
    conflict_token: str | None = None,
) -> DeviceRecord:
    public = _run_genkey(home, environ=environ, force=force)
    generation = (
        existing.key_generation
        if existing is not None
        and existing.server_public == public["serverPublic"]
        and existing.client_public == public["clientPublic"]
        else (existing.key_generation + 1 if existing is not None else 1)
    )
    device_name = _device_name(existing, name, environ)
    record = DeviceRecord(
        device_id=device_id,
        owner=owner,
        server_public=public["serverPublic"],
        client_public=public["clientPublic"],
        key_generation=generation,
        device_uid=(
            existing.device_uid
            if existing is not None and existing.device_uid is not None
            else uuid4().hex
        ),
        name=device_name,
    )
    write_device_record(home, record)
    if force:
        try:
            address_path(home).unlink()
        except FileNotFoundError:
            pass
        write_machine_binding(
            home,
            MachineBinding(
                machine_fingerprint=fingerprint,
                first_published_at=None,
                last_conflict_token=conflict_token,
                last_reason=reason,
                pending_event={
                    "deviceId": device_id,
                    "oldIdentityHash": _identity_hash(existing),
                    "newIdentityHash": _identity_hash(record),
                    "reason": reason,
                },
            ),
        )
    return record


def _run_genkey(
    home: Path, *, environ: Mapping[str, str] | None, force: bool
) -> dict[str, object]:
    binary = locate_tailcat_sidecar(home, environ)
    key_path = device_key_path(home)
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [str(binary), "genkey", "--out", str(key_path)]
    if force:
        command.append("--force")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=_GENKEY_TIMEOUT_SECONDS,
            check=False,
            **no_console_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TailcatSidecarError(
            "KEYGEN_FAILED", f"cannot run {binary} genkey: {error}"
        ) from error
    if completed.returncode not in (0, 3):
        raise TailcatSidecarError(
            "KEYGEN_FAILED",
            f"{binary} genkey exited with status {completed.returncode}: "
            f"{completed.stderr.decode('utf-8', 'replace').strip()[:200]}",
        )
    try:
        public = json.loads(completed.stdout.decode("utf-8", "replace").strip())
    except ValueError as error:
        raise TailcatSidecarError(
            "KEYGEN_FAILED", f"{binary} genkey did not print JSON: {error}"
        ) from error
    if (
        not isinstance(public, dict)
        or set(public) != {"v", "serverPublic", "clientPublic"}
        or public.get("v") != 4
        or not isinstance(public.get("serverPublic"), str)
        or not public["serverPublic"]
        or not isinstance(public.get("clientPublic"), str)
        or not public["clientPublic"]
    ):
        raise TailcatSidecarError(
            "KEYGEN_FAILED", f"{binary} genkey printed an invalid public key record"
        )
    return public


def _device_name(
    existing: DeviceRecord | None,
    name: str | None,
    environ: Mapping[str, str] | None,
) -> str:
    environment = os.environ if environ is None else environ
    if existing is not None and existing.name is not None:
        device_name = existing.name
    elif name is not None:
        device_name = name.strip()
    else:
        device_name = (
            environment.get(_DEVICE_NAME_ENV, "").strip()
            or socket.gethostname().strip()
        )
    if is_identity_id_segment(device_name.lower()):
        raise ValueError(
            "device display name must not have an exact identity id shape; "
            f"set {_DEVICE_NAME_ENV} to a different machine display name and re-run `hyprial login`"
        )
    return device_name


def _read_binding(home: Path) -> MachineBinding | None:
    try:
        return read_machine_binding(home)
    except ValueError as error:
        raise TailcatSidecarError("KEYGEN_FAILED", str(error)) from error


def _identity_hash(record: DeviceRecord | None) -> str:
    if record is None:
        return hashlib.sha256(b"missing").hexdigest()[:12]
    value = f"{record.server_public}\0{record.client_public}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:12]
