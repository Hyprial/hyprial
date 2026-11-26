"""Closed model-start diagnostics survive the existing lifecycle text bridge."""

from hyprial.error import codes

MODEL_BINDING_FAILURES = {
    codes.MODEL_CONFIGURATION_INCOMPLETE: (
        "bind",
        ("destroy-cleanup-pending", "operation-in-progress", "state-unreadable"),
    ),
    codes.MODEL_CONFIGURATION_CONFLICT: (
        "bind",
        ("revision-mismatch", "cell-intent-mismatch"),
    ),
    codes.MODEL_CONFIGURATION_INVALID: ("validate", ("invalid-connection",)),
}
