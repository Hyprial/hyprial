"""Thin typed device-preparation port over the existing Tailcat capability.

The onboarding service never reads or writes the private device key itself and
never reimplements the sidecar key/sidecar rules.  It calls the existing
``daemon.impl.network.tailcat.ensure_device_key`` capability through this narrow
port; the private key stays in the sidecar's 0600 file, and the public record is
written by the existing capability.

The port exists so the gateway can be tested with a fake and so the failure
classification (definite vs outcome-unknown) is one explicit decision rather
than a broad ``except Exception`` in the handler.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from hyprial.identity import DeviceRecord, read_device_record
from hyprial.kernel import resolve_node_id

from hyprial.daemon.impl.network.tailcat import (
    TailcatSidecarError,
    ensure_device_key,
)


class DeviceProvisioningError(Exception):
    """A device-preparation failure with an explicit outcome classification.

    ``outcome_unknown`` means the sidecar dispatch may have reached the child
    and the key may already exist; callers must not retry blindly.
    """

    def __init__(
        self, message: str, *, outcome_unknown: bool = False
    ) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


class DeviceProvisioner(Protocol):
    """Prepare this home's public device record through the Tailcat capability."""

    def ensure(self, *, home: Path, owner: str) -> DeviceRecord: ...


class TailcatDeviceProvisioner:
    """Default port implementation backed by ``ensure_device_key``."""

    def __init__(
        self,
        *,
        device_id_factory: Callable[[], str] = resolve_node_id,
    ) -> None:
        self._device_id_factory = device_id_factory

    def ensure(self, *, home: Path, owner: str) -> DeviceRecord:
        home = Path(home)
        existing = read_device_record(home)
        device_id = (
            existing.device_id
            if existing is not None
            else self._device_id_factory()
        )
        try:
            return ensure_device_key(home, owner=owner, device_id=device_id)
        except TailcatSidecarError as error:
            # genkey failures are definite at the sidecar boundary: the
            # capability reports a stable KEYGEN_FAILED code and the public
            # record was not written.
            raise DeviceProvisioningError(
                f"device key provisioning failed: {error}"
            ) from error
        except OSError as error:
            # A local OS failure after dispatch is not provably before the
            # child ran; treat it as outcome-unknown and never retry blindly.
            raise DeviceProvisioningError(
                f"device key provisioning outcome is unknown: {error}",
                outcome_unknown=True,
            ) from error


__all__ = [
    "DeviceProvisioner",
    "DeviceProvisioningError",
    "TailcatDeviceProvisioner",
]
