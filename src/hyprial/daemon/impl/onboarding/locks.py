# Decision record (W2A-LOGIN-INPROC, 2026-11-21):
# - BootstrapLocks acquires IdentityTransactionLock.acquire() (ProcessFileLock)
#   instead of POSIX-only acquire_transferable(), and drops
#   duplicate_identity_descriptor() now that onboarding commits identity
#   in-process under the continuous home lock.
"""Keep existing home/state OS locks continuous through bootstrap promotion.

This helper is not authorization or bootstrap admission. The caller must first
validate the selected home, owner state, process identity and environment, then
recheck identity under these locks. No credential or business store is opened.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.configuration.ownership import DaemonStateOwnershipFence
from hyprial.identity import IdentityTransactionLock


def require_unoverridden_owner() -> None:
    """Reject every nonempty override, including whitespace, without mutation.

    This check applies only to explicit bootstrap and its promotion; normal
    CLI identity resolution retains its managed override behavior.
    """
    if os.environ.get("HYPRIAL_OWNER", ""):
        raise ValueError("bootstrap requires HYPRIAL_OWNER to be unset or empty")


class BootstrapLocks:
    """Own both locks until closure or explicit same-process runtime transfer."""

    def __init__(
        self, identity: IdentityTransactionLock, ownership: DaemonStateOwnershipFence
    ) -> None:
        self._identity = identity
        self._ownership = ownership
        self._active = True

    @classmethod
    def acquire(cls, home: Path, state_dir: Path) -> BootstrapLocks:
        require_unoverridden_owner()
        identity = IdentityTransactionLock.acquire(home, timeout=0)
        try:
            ownership = DaemonStateOwnershipFence.acquire(state_dir, timeout=0)
        except BaseException:
            identity.close()
            raise
        return cls(identity, ownership)

    def _require_active(self) -> None:
        if not self._active:
            raise RuntimeError("bootstrap locks have already been closed or transferred")

    def require_active(self) -> None:
        """Reject lifecycle work after the continuous locks were transferred or closed."""
        self._require_active()

    def detach(self) -> tuple[Any, Any]:
        """Transfer ownership_stream and identity_transaction_stream to run().

        The receiver must close both on construction failure. Normal runtime
        releases identity at serving and retains state ownership until exit.
        No unlock/reacquire window is introduced by this transfer.
        """
        self._require_active()
        require_unoverridden_owner()
        ownership = self._ownership.detach()
        try:
            identity = self._identity.detach()
        except BaseException:
            ownership.close()
            self.close()
            raise
        self._active = False
        return ownership, identity

    def close(self) -> None:
        if not self._active:
            return
        self._active = False
        try:
            self._ownership.close()
        finally:
            self._identity.close()

    def __enter__(self) -> BootstrapLocks:
        self._require_active()
        return self

    def __exit__(self, *_error: object) -> None:
        self.close()
