"""Composition helpers for the authenticated remote facade.

The N-A2 request key comes from the tailcat sidecar's ``derive`` op (role
``server``, peer = the approved device public key).  That op is not part of
the baseline sidecar this branch is cut from, so the host takes one explicit
helper command instead of reaching into the daemon or inventing a key:

``stdin``  one JSON line ``{"v":1,"op":"derive","role":"server","peer":"..."}``
``stdout`` one JSON line ``{"key":"<base64 of the 32-byte request key>"}``

An error line ``{"event":"error","code":"SIDECAR_DERIVE_UNSUPPORTED"}`` maps
to the frozen unsigned-downgrade refusal; every other fault fails closed as
``derive-unavailable``.  The helper never receives a pairing code, a nonce or
a business payload, and this module never logs its stdout or environment.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import shlex
import subprocess
from pathlib import Path

from hyprial.shell.impl.gui.adapter.envelope import AuthGate, DeriveUnsupported
from hyprial.shell.impl.gui.adapter.mobile.store import MobileAccessStore

DERIVE_TIMEOUT_SECONDS = 5.0
DERIVE_KEY_BYTES = 32
_UNSUPPORTED_CODES = frozenset({"SIDECAR_DERIVE_UNSUPPORTED", "derive-unsupported"})


class DeriveCommandError(RuntimeError):
    """The configured helper answered something unusable; never downgrade."""


class CommandDerive:
    """One request-key derivation through the operator-configured helper."""

    def __init__(self, command: str | Path) -> None:
        parts = shlex.split(os.fspath(command))
        if not parts:
            raise ValueError("derive command must not be empty")
        executable = Path(parts[0])
        if not executable.is_file():
            raise ValueError(f"derive command does not exist: {executable}")
        if not os.access(executable, os.X_OK):
            raise ValueError(f"derive command is not executable: {executable}")
        self._parts = parts
        self.timeout = DERIVE_TIMEOUT_SECONDS

    def __call__(self, role: str, peer: str) -> bytes:
        if role not in {"client", "server"}:
            raise DeriveCommandError("derive role must be client or server")
        request = json.dumps(
            {"v": 1, "op": "derive", "role": role, "peer": peer},
            separators=(",", ":"),
        )
        try:
            completed = subprocess.run(
                self._parts,
                input=request + "\n",
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DeriveCommandError("derive helper is unavailable") from error
        if completed.returncode != 0:
            raise DeriveCommandError(
                f"derive helper exited {completed.returncode}"
            )
        lines = completed.stdout.splitlines()
        if len(lines) != 1:
            raise DeriveCommandError("derive helper must answer exactly one line")
        try:
            answer = json.loads(lines[0])
        except json.JSONDecodeError as error:
            raise DeriveCommandError("derive helper answer is not JSON") from error
        if not isinstance(answer, dict):
            raise DeriveCommandError("derive helper answer must be an object")
        code = answer.get("code")
        if code is not None:
            if code in _UNSUPPORTED_CODES:
                raise DeriveUnsupported(str(code))
            raise DeriveCommandError(f"derive helper refused: {code}")
        key = answer.get("key")
        if not isinstance(key, str) or not key:
            raise DeriveCommandError("derive helper returned no key")
        try:
            raw = base64.b64decode(key, validate=True)
        except (binascii.Error, ValueError) as error:
            raise DeriveCommandError("derive key is not base64") from error
        if len(raw) != DERIVE_KEY_BYTES:
            raise DeriveCommandError("derive key is not 32 bytes")
        return raw


def build_auth_gate(
    db_path: Path | str, node_id: str, derive_command: str | Path
) -> AuthGate:
    """Open the durable authority and compose the one production gate.

    The store lives as long as the returned gate (the gate holds it as its
    admission authority).  A missing or unusable helper raises here, before
    any listener binds, so a remote launch can never fall back to an
    unauthenticated facade.
    """

    store = MobileAccessStore(Path(db_path), node_id=node_id)
    return AuthGate(
        derive=CommandDerive(derive_command),
        device_row=store.device_row,
        target_node_id=store.node_id,
        access=store,
        clock=store.clock,
    )
