# Decision notes (service grant S5, 2026-11-26):
# - This package is the recipient side of service grants. The grantor side
#   (S3) stays in the parent package; this package never writes a grant.
# - Four modules, one job each: derive (pure: grants + device rows become
#   service-connect entries and devices), keys (the granted Key as a
#   user-provider entry, with the ledger that proves ownership),
#   maintenance (GrantConsumer: the derived source service-connect drives),
#   startup (the cheap question the daemon asks before starting
#   service-connect for a grant-only recipient).
# - It is a subpackage because the parent package's __init__ is not this
#   slice's to change, and because S4-py owns enforcement/ beside it.
"""The recipient side of service grants: what a grant gives the device it is addressed to."""

from .keys import LEDGER_RELPATH as LEDGER_RELPATH
from .keys import entry_id_for_grant as entry_id_for_grant
from .maintenance import GrantConsumer as GrantConsumer
from .startup import device_id_of as device_id_of
from .startup import has_effective_grant as has_effective_grant

__all__ = [
    "LEDGER_RELPATH",
    "GrantConsumer",
    "device_id_of",
    "entry_id_for_grant",
    "has_effective_grant",
]
