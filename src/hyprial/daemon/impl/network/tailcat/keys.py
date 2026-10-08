"""Device key provisioning through the sidecar's ``genkey`` subcommand.

The private key never enters Python: the sidecar writes the 0600 key file
itself and prints only the *public* halves on stdout.  This module turns that
stdout into the published :class:`DeviceRecord` (identity domain, §4.1).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from hyprial.identity import (
    DeviceRecord,
    device_key_path,
    read_device_record,
    write_device_record,
)
from hyprial.kernel import is_identity_id_segment

from .binary import TailcatSidecarError, locate_tailcat_sidecar

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
    """Ensure the device key exists and (re)publish its public record.

    ``genkey --out`` exits 0 when it minted a fresh key and 3 when the key
    already existed; both print the public key JSON on stdout, so both are
    success here.  Any other exit status is ``KEYGEN_FAILED``.  The
    :class:`DeviceRecord` is rewritten on every call so a changed owner or
    device id lands without a separate migration.
    """

    home = Path(home)
    binary = locate_tailcat_sidecar(home, environ)
    key_path = device_key_path(home)
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        completed = subprocess.run(
            [str(binary), "genkey", "--out", str(key_path)],
            capture_output=True,
            timeout=_GENKEY_TIMEOUT_SECONDS,
            check=False,
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
        or public.get("v") != 3
        or not isinstance(public.get("serverPublic"), str)
        or not public["serverPublic"]
        or not isinstance(public.get("clientPublic"), str)
        or not public["clientPublic"]
    ):
        raise TailcatSidecarError(
            "KEYGEN_FAILED", f"{binary} genkey printed an invalid public key record"
        )
    existing = read_device_record(home)
    generation = (
        existing.key_generation
        if existing is not None
        and existing.server_public == public["serverPublic"]
        and existing.client_public == public["clientPublic"]
        else (existing.key_generation + 1 if existing is not None else 1)
    )
    environment = os.environ if environ is None else environ
    if existing is not None and existing.name is not None:
        device_name = existing.name
    elif name is not None:
        device_name = name.strip()
    else:
        device_name = (
            environment.get(_DEVICE_NAME_ENV, "").strip() or socket.gethostname().strip()
        )
    if is_identity_id_segment(device_name.lower()):
        raise ValueError(
            "device display name must not have an exact identity id shape; "
            f"set {_DEVICE_NAME_ENV} to a different machine "
            "display name and re-run `hyprial login`"
        )
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
    return record
