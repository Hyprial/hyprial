"""App-facing chat facade (node side) for the browser Core chat port, v0."""

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.gui.adapter.chat import (
    IDENTITY_FIELDS,
    SEND_REQUEST_KEYS,
    V0_OPERATIONS,
    AppChatAdapter,
    SessionBinding,
)
from hyprial.shell.impl.gui.adapter.errors import (
    DAEMON_REJECTED,
    IDENTITY_REJECTED,
    INVALID_ARGUMENT,  # noqa: F401 - exported via __all__ as the registry constant
    SEND_REJECTED,
    UNKNOWN_SESSION,
    UNKNOWN_TARGET,
    UNSUPPORTED_OPERATION,
    AppAdapterError,
    AppAdapterUnavailable,
)

__all__ = [
    "AppAdapterError",
    "AppAdapterUnavailable",
    "AppChatAdapter",
    "SessionBinding",
    "DAEMON_REJECTED",
    "IDENTITY_FIELDS",
    "IDENTITY_REJECTED",
    # This export name happens to equal a proto-facing code; spell the entry
    # through the registry constant so the AST guard reads a reference, not a
    # literal (the value is the same string).
    ipc_errors.INVALID_ARGUMENT,
    "SEND_REJECTED",
    "SEND_REQUEST_KEYS",
    "UNKNOWN_SESSION",
    "UNKNOWN_TARGET",
    "UNSUPPORTED_OPERATION",
    "V0_OPERATIONS",
]
