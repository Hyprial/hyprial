# Decision record (Z-5, owner decision 2026-11-18: the daemon owns the first login):
# - The user-signing key a login creates needs nothing from the user's
#   interactive context: no network, no keychain, no terminal.  It is built from
#   the committed owner, the settings ``userId``, the registered device's
#   ``deviceUid`` and the machine fingerprint, which is exactly what the
#   daemon's own signer already composes (``ipc.compose``).  Once the first
#   login runs inside the daemon, this is therefore the daemon's step, reached
#   through the public identity face only (no shell import, no settings parsing
#   of its own: the subject is read through ``read_onboarding_subject``).
# - The answer is a secret-free ``{state, code}`` pair.  The key id and the
#   public key stay in the store; they never travel in a login projection.
# - ``bound`` on disk is reported ``unbound`` with ``USER_KEY_UNBOUND``: trusted
#   binding issuance is not available yet, so a bound bit alone is not evidence.
#   This is the same rule the shell's login stage applies; the shell copy stays
#   until the owned re-login route moves.
# - The caller fences a concurrent login.  In bootstrap the daemon already holds
#   the identity transaction lock continuously; serving recovery takes it with a
#   bounded wait and skips on busy.  This module takes no lock of its own beyond
#   the store's.
"""The login user-key step, run by the daemon over the public identity face."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hyprial.identity import (
    IdentityRecordError,
    UserKey,
    machine_fingerprint,
    read_onboarding_subject,
)
from hyprial.kernel import ipc_errors

__all__ = ["ensure_login_user_key"]


def _unavailable(code: str) -> dict[str, str]:
    return {"state": "unavailable", "code": code}


def ensure_login_user_key(
    home: Path, state_dir: Path, owner: str, device: Any
) -> dict[str, str]:
    """Create or confirm the local user key for ``owner`` and ``device``.

    Never raises for a missing prerequisite or a store refusal: those are
    answers (``unavailable`` plus the store's own code).  Missing prerequisites
    never mint substitutes.
    """

    if not UserKey.Store.supported():
        return _unavailable(ipc_errors.USER_KEY_UNBOUND)
    device_uid = getattr(device, "device_uid", None)
    if (
        device is None
        or getattr(device, "owner", None) != owner
        or not isinstance(device_uid, str)
        or not device_uid
    ):
        return _unavailable(ipc_errors.USER_KEY_ABSENT)
    try:
        subject = read_onboarding_subject(Path(home))
        if subject is None:
            return _unavailable(ipc_errors.USER_KEY_ABSENT)
        store = UserKey.Store(
            state_dir=Path(state_dir),
            owner=owner,
            casdoor_sub=subject.user_id,
            device_uid=device_uid,
            fingerprint=machine_fingerprint(),
        )
        result = store.ensure()
    except UserKey.Error as error:
        return _unavailable(error.code)
    except (IdentityRecordError, OSError, ValueError, TypeError, AttributeError):
        return _unavailable(ipc_errors.USER_KEY_ABSENT)
    state = "unbound" if result.get("state") == "bound" else str(result.get("state"))
    return {"state": state, "code": ipc_errors.USER_KEY_UNBOUND}
