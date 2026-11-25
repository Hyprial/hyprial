"""M2 onboarding contract package; declarations and pure state only.

Decision: gui-m2-onboarding-contracts-20261105.  There is no production
handler binding here.  The common line registers these declarations after the
structural packages merge; until then the package is consumed by isolated
contract tests and by the interface notes for the later runtime nodes.

Decision: 2026-11-19 examples split.  ``ONBOARDING_EXAMPLES`` is assembled here,
from ``examples`` (the account sets) and ``first_run_examples`` (the first-run
confirmation sets), because ``first_run_examples`` imports helpers from
``examples`` and so ``examples`` cannot import it back; the order is unchanged.
"""

from __future__ import annotations

from .examples import (
    AUTH_CANCEL_EXAMPLES,
    AUTH_GET_EXAMPLES,
    DEVICE_EXAMPLES,
    INITIALIZE_EXAMPLES,
    LOGIN_BEGIN_EXAMPLES,
    LOGOUT_EXAMPLES,
    RELOGIN_EXAMPLES,
    STATUS_EXAMPLES,
)
from .first_run_examples import (
    FIRST_RUN_BEGIN_EXAMPLES,
    FIRST_RUN_BIND_EXAMPLES,
    FIRST_RUN_CANCEL_EXAMPLES,
    FIRST_RUN_CONFIRM_EXAMPLES,
    FIRST_RUN_SKIP_EXAMPLES,
)
from .specs import (
    AUTH_LOGOUT_SPEC,
    AUTH_OPERATION_CANCEL_SPEC,
    AUTH_OPERATION_GET_SPEC,
    AUTH_RELOGIN_SPEC,
    DEVICE_ENSURE_SPEC,
    INITIALIZE_SPEC,
    LOGIN_BEGIN_SPEC,
    ONBOARDING_SPECS,
    STATUS_SPEC,
    FIRST_RUN_SKIP_SPEC,
)
from .state import (
    AUTH_OPERATION_CONFLICT as AUTH_OPERATION_CONFLICT,
    AUTH_OPERATION_NOT_FOUND as AUTH_OPERATION_NOT_FOUND,
    LOGOUT_STATES,
    CallContext,
    CancelOutcome,
    FirstRunSnapshot,
    OnboardingStateError,
    OperationError,
    OperationRecord,
    OperationStore,
    PromotionClaim,
    StoreSnapshot,
    Surface,
    authorize_call,
    onboarding_status,
    status_snapshot,
)

ONBOARDING_EXAMPLES = (
    STATUS_EXAMPLES,
    LOGIN_BEGIN_EXAMPLES,
    AUTH_GET_EXAMPLES,
    AUTH_CANCEL_EXAMPLES,
    DEVICE_EXAMPLES,
    INITIALIZE_EXAMPLES,
    LOGOUT_EXAMPLES,
    RELOGIN_EXAMPLES,
    FIRST_RUN_BEGIN_EXAMPLES,
    FIRST_RUN_BIND_EXAMPLES,
    FIRST_RUN_CONFIRM_EXAMPLES,
    FIRST_RUN_CANCEL_EXAMPLES,
    FIRST_RUN_SKIP_EXAMPLES,
)

__all__ = [
    "AUTH_LOGOUT_SPEC",
    "AUTH_OPERATION_CANCEL_SPEC",
    "AUTH_OPERATION_GET_SPEC",
    "AUTH_RELOGIN_SPEC",
    "LOGOUT_STATES",
    "CallContext",
    "CancelOutcome",
    "DEVICE_ENSURE_SPEC",
    "FirstRunSnapshot",
    "INITIALIZE_SPEC",
    "LOGIN_BEGIN_SPEC",
    "ONBOARDING_EXAMPLES",
    "ONBOARDING_SPECS",
    "OnboardingStateError",
    "OperationError",
    "OperationRecord",
    "OperationStore",
    "PromotionClaim",
    "STATUS_SPEC",
    "FIRST_RUN_SKIP_SPEC",
    "StoreSnapshot",
    "Surface",
    "authorize_call",
    "onboarding_status",
    "status_snapshot",
]
