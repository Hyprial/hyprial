"""Split-out serving lifecycle internals.

``account_lifecycle`` remains the public module for these names; this package
only exists so no single module exceeds the 1000-line structural budget.
"""

from __future__ import annotations

from .logout import LogoutArtifactPort
from .logout import _IdentityLogoutArtifacts as _IdentityLogoutArtifacts
from .logout import _logout_operation as _logout_operation
from .logout import _logout_result as _logout_result
from .recovery import ServingRecovery
from .relogin import (
    SERVING_PERMIT_SECONDS,
    ServingRelogin,
    _auth_operation as _auth_operation,
    _begin_record_result as _begin_record_result,
    _begin_result as _begin_result,
    _cancel_result as _cancel_result,
)

__all__ = [
    "LogoutArtifactPort",
    "SERVING_PERMIT_SECONDS",
    "ServingRecovery",
    "ServingRelogin",
]
