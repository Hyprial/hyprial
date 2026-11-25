"""Controlled ``hyprial daemon run --json`` entry for serving relogin integration tests."""

from __future__ import annotations

import os
import runpy
import sys

ROOT = os.environ.get("Z2_REPO_ROOT")
if ROOT:
    sys.path.insert(0, ROOT)

from hyprial.daemon.impl.application.onboarding import account_lifecycle  # noqa: E402
from hyprial.daemon.impl.application.onboarding.provisioning import (  # noqa: E402
    DeviceProvisioningError,
)
from hyprial.identity import (  # noqa: E402
    AuthProfile,
    DeviceRecord,
    IdentityBegin,
    IdentityPublicState,
    write_device_record,
)

OWNER = "z2-owner@example.test"
ISSUER = "https://issuer.example.test"
UID = "uid-z2-owner-001"


class _ServingIdentity:
    def begin(self, *, profile, home, permit, lock_fd=None, transport=None):
        del profile, lock_fd, transport
        self.home = home
        return IdentityBegin(permit.operation_id, f"{ISSUER}/device", "RL-Z2", 600, 1)

    def poll_public(self, operation_id: str) -> IdentityPublicState:
        if os.environ.get("L2_HOLD") == "1":
            return IdentityPublicState(operation_id, "pending")
        uid = os.environ.get("L2_COMMIT_UID", UID)
        return IdentityPublicState(
            operation_id,
            "committed",
            OWNER,
            ISSUER,
            {"credentialCommitted": True, "settingsCommitted": True},
            user_id=uid,
        )

    def cancel(self, operation_id: str) -> IdentityPublicState:
        return IdentityPublicState(operation_id, "cancelled")

    def close(self) -> None:
        pass


class _ControlledDevice:
    def ensure(self, *, home, owner):
        device = DeviceRecord(
            "z2-relogin-device",
            owner,
            "nodekey:test-server",
            "nodekey:test-client",
            device_uid="12345678123442348234123456789abc",
            name="z2-relogin-device",
        )
        write_device_record(home, device)
        return device


class _FailingDevice:
    def ensure(self, *, home, owner):
        del home, owner
        raise DeviceProvisioningError("controlled provisioner failure")


_original_init = account_lifecycle.ServingOnboardingGateway.__init__


def _patched_init(self, **kwargs):
    kwargs.setdefault("profile_resolver", lambda _home: AuthProfile(ISSUER, "test-client"))
    kwargs.setdefault("identity_factory", _ServingIdentity)
    kwargs.setdefault(
        "provisioner",
        _FailingDevice() if os.environ.get("L2_DEVICE") == "fail" else _ControlledDevice(),
    )
    _original_init(self, **kwargs)


if __name__ == "__main__":
    account_lifecycle.ServingOnboardingGateway.__init__ = _patched_init
    sys.argv = ["hyprial", "daemon", "run", "--json"]
    runpy.run_module("hyprial.cli", run_name="__main__")
