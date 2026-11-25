"""Organization membership declarations beside the shared public org surface."""

from .context import CONTEXT_BINDINGS
from .invites import ORG_INVITE_BINDINGS

MEMBERSHIP_BINDINGS = (
    *ORG_INVITE_BINDINGS,
    *CONTEXT_BINDINGS,
)

__all__ = ["MEMBERSHIP_BINDINGS"]
