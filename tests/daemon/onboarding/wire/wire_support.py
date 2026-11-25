"""Controlled ports and isolated homes for the bootstrap dispatcher and wire host tests.

Decision: daemon-first-z1-test-support-20261115.  Every home lives under a short
temporary root (AF_UNIX paths are short on macOS), the owner override is
removed, identity commit and device preparation are controlled in-process ports
(no account, no network), and nothing touches an ambient home, a production
daemon or a service unit.
"""

from __future__ import annotations

from tests.support.runtime.environment import home_with_node_id
import json
import shutil
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

from hyprial.daemon import DaemonOwnershipBusy, DaemonStateOwnershipFence
from hyprial.identity import (
    AuthProfile,
    IdentityBegin,
    IdentityPublicState,
    IdentityTransactionBusy,
    IdentityTransactionLock,
)
from tests.support.runtime._tmp import hyprial_test_tmp_root

BUSY = (DaemonOwnershipBusy, IdentityTransactionBusy)
OWNER = "wire-test@example.test"
ISSUER = "https://issuer.example.test"


class ControlledIdentity:
    """Commits ``OWNER`` into ``settings.json`` when polled, like a finished login."""

    def begin(self, *, profile, home, permit, lock_fd=None, transport=None):
        del profile, lock_fd, transport
        self.home = home
        return IdentityBegin(
            permit.operation_id, "https://issuer.example.test/device", "WIRE-CODE", 600, 1
        )

    def poll_public(self, operation_id):
        (self.home / "settings.json").write_text(
            json.dumps(
                {"owner": OWNER, "identityMode": "casdoor", "identityIssuer": ISSUER}
            )
        )
        return IdentityPublicState(
            operation_id,
            "committed",
            OWNER,
            ISSUER,
            {"credentialCommitted": True, "settingsCommitted": True},
        )

    def close(self):
        pass


def gateway_options():
    return {
        "profile_resolver": lambda _: AuthProfile(ISSUER, "test-client"),
        "provisioner": SimpleNamespace(
            ensure=lambda **_: SimpleNamespace(device_id="controlled-device")
        ),
    }


class OwnerlessHome:
    """A fresh ownerless home and state directory under a short temporary root."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="hw", dir=hyprial_test_tmp_root()))
        self.home = self.root / "home"
        self.state = self.home / "state"
        home_with_node_id(self.home)
        (self.home / "settings.json").write_text(
            json.dumps({"version": 1}), encoding="utf-8"
        )

    @property
    def socket(self) -> Path:
        return self.state / "daemon.sock"

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class ServedWire:
    """A wire host served on a thread; ``stop`` returns its outcome."""

    def __init__(self, host) -> None:
        self.host = host
        self.outcome: str | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        self.outcome = self.host.serve()

    def wait(self, timeout: float = 10.0) -> str | None:
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "the wire host did not stop"
        return self.outcome

    def stop(self) -> str | None:
        self.host.request_stop()
        return self.wait()


def assert_home_released(home: "OwnerlessHome") -> None:
    """Both continuous locks are acquirable again and no bootstrap record is left."""

    fence = DaemonStateOwnershipFence.acquire(home.state, timeout=0)
    try:
        identity = IdentityTransactionLock.acquire(home.home, timeout=0)
        identity.close()
    finally:
        fence.close()
    assert not (home.state / "bootstrap.json").exists()
