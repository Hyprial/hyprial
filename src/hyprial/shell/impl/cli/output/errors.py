"""Errors as the CLI reports them: one JSON shape, one human line."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from hyprial.identity import PacError
from hyprial.error import codes
from hyprial.kernel import HYPRIALHomeNotInitialized, RequestPortError, ipc_errors

JsonObject = dict[str, Any]

# Canonical protocol codes used by `hyprial agent args`.  They live here
# because this module already owns the CLI's kernel-error import (the import
# ratchet forbids a new CLI->kernel edge) and no other CLI module may spell a
# registered code as a string literal.
#: One registered code the authority commands raise for a malformed target.
#: Named here for the same reason as the harness-args constants below: this
#: module already owns the CLI's kernel import, and the ratchet forbids a new
#: CLI->kernel edge for a single string.
AUTHORITY_INVALID_ARGUMENT = codes.INVALID_ARGUMENT
TELEPORT_INVALID_ARGUMENT = codes.INVALID_ARGUMENT
HARNESS_ARGS_METHOD_NOT_FOUND = codes.METHOD_NOT_FOUND
HARNESS_ARGS_CONFLICT = codes.HARNESS_ARGS_CONFLICT
HARNESS_ARGS_INVALID_ARGUMENT = codes.INVALID_ARGUMENT
HARNESS_ARGS_INVALID = codes.HARNESS_ARGS_INVALID
HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED = (
    codes.HARNESS_ARGS_DANGEROUS_CONFIRMATION_REQUIRED
)


def json_failure(error: Exception) -> JsonObject:
    """``{"ok": false, "code"?, "error", "data"?}`` for any CLI failure."""
    # PR #332 F4②: a surfaced transient renders exactly like the CliError it
    # used to be -- ok:false with its code (the runner classifies on it).
    if isinstance(
        error,
        (RequestPortError, PacError, HYPRIALHomeNotInitialized, ipc_errors.TransientDaemonError),
    ) or (
        isinstance(getattr(error, "code", None), str)
        and isinstance(getattr(error, "data", None), dict)
    ):
        result: JsonObject = {
            "ok": False,
            "code": str(getattr(error, "code")),
            "error": str(error),
        }
        data = getattr(error, "data", None)
        if data is not None:
            result["data"] = data
        return result
    return {"ok": False, "error": str(error)}


def _orgfs_content_pending(data: dict[str, Any]) -> str:
    expected = data.get("expectedSize")
    digest = str(data.get("expectedSha256") or "")
    holders = data.get("suggestedHolders")
    waited = data.get("waitedSeconds", 0)
    expected_text = (
        f"{int(expected) / (1024 * 1024):.1f} MB" if type(expected) is int else "unknown size"
    )
    holder_text = ", ".join(map(str, holders)) if holders else "none online"
    return (
        f"content not yet on this node: expected {expected_text} "
        f"sha256 {digest[:8] or 'unknown'}, holders online: {holder_text}; "
        f"waited {float(waited):g}s — retry with --wait 60 or from a holder node"
    )


_HUMAN_HINTS: dict[str, Callable[[dict[str, Any]], str]] = {
    codes.ORGFS_CONTENT_PENDING: _orgfs_content_pending,
}
"""Error codes whose human message is derived from the error's data."""

_HUMAN_MESSAGES = {
    codes.IDENTITY_OVERRIDE_FORBIDDEN: (
        "permission denied: identity overrides and user changes require operator authorization"
    ),
}


def human_message(error: Exception) -> str:
    """The one-line human message for ``error``."""
    code = getattr(error, "code", None)
    if isinstance(code, str) and code in _HUMAN_MESSAGES:
        return _HUMAN_MESSAGES[code]
    data = getattr(error, "data", None)
    hint = _HUMAN_HINTS.get(code) if isinstance(code, str) else None
    if hint is not None and isinstance(error, RequestPortError) and isinstance(data, dict):
        return hint(data)
    return str(error)
