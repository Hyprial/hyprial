"""One owned subprocess per bounded authorization or inventory effect."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Any

from hyprial.kernel import create_owned_process_group, process_birth_identity, windows_process_identity

_WORKER = "hyprial.daemon.impl.adapters.lark.credentials.authorization.worker"
_ENVIRONMENT = ("PATH", "SYSTEMROOT", "WINDIR", "LANG", "LC_ALL",
                "HYPRIAL_LARK_BASE_URL", "HYPRIAL_LARK_ACCOUNTS_DOMAIN")


def owner_identity(pid: int) -> str | None:
    return windows_process_identity(pid) if os.name == "nt" else process_birth_identity(pid)


class AuthorizationEffect:
    """snapshot() never waits for SDK I/O; close() reaps the exact child."""

    def __init__(self, job: dict[str, Any]) -> None:
        self._lock = threading.Lock()
        self._directory = tempfile.TemporaryDirectory(prefix="hyprial-lark-auth-")
        self._root = Path(self._directory.name)
        self._closed = False
        self._owner = create_owned_process_group(label="Lark App authorization")
        path = self._root / "job.json"
        marker = owner_identity(os.getpid())
        if marker is None:
            self._directory.cleanup()
            raise RuntimeError("authorization host identity is unavailable")
        path.write_text(json.dumps({**job, "ownerPid": os.getpid(), "ownerIdentity": marker}), encoding="utf-8")
        environment = {key: os.environ[key] for key in _ENVIRONMENT if key in os.environ}
        environment.update(HOME=str(self._root), USERPROFILE=str(self._root), PYTHONNOUSERSITE="1")
        # Run the exact installed/source package imported by this host, even
        # when a trusted composition selected it via PYTHONPATH.
        package = next(parent for parent in Path(__file__).resolve().parents if parent.name == "hyprial")
        environment["PYTHONPATH"] = str(package.parent)
        self._process = None
        try:
            self._process = subprocess.Popen(
                [sys.executable, "-m", _WORKER, str(path)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=environment, close_fds=True, start_new_session=os.name != "nt",
                **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}),
            )
            self._owner.register(self._process.pid)
        except BaseException:
            self.close()
            raise

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            if self._closed:
                return {"status": "cancelled"}
            try:
                with (self._root / "result.json").open("rb") as stream:
                    raw = stream.read(65537)
                if len(raw) > 65536:
                    raise ValueError("oversized result")
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("invalid result")
            except FileNotFoundError:
                value = None
            except (OSError, ValueError):
                return {"status": "unknown", "errorCode": "INVALID_AUTHORIZATION_RESULT"}
            if self._process.poll() is not None and (
                value is None or value.get("status") in {"starting", "awaiting_user", "checking"}
            ):
                return {"status": "unknown", "errorCode": "AUTHORIZATION_WORKER_EXITED"}
            return value

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            process = self._process
            if process is not None:
                self._owner.force_close()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=0.5)
                self._owner.release_if_gone(process.pid)
                if not self._owner.stopped():
                    raise RuntimeError("authorization process cleanup is unconfirmed")
            self._closed = True
            self._directory.cleanup()
