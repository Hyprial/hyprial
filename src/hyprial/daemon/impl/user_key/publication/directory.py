"""No production path or unsigned compatibility publisher.

U2 local verification does not authorize issuance, renewal or publication.
"""
from hyprial.identity import UserKey
from hyprial.kernel import ipc_errors


def publish():
    raise UserKey.Error(ipc_errors.USER_KEY_UNBOUND)
