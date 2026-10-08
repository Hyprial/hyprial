"""Bounded inline output carried by an exact PAC workflow request."""

from __future__ import annotations

from typing import Any


MAX_WORKFLOW_OUTPUT_TEXT_BYTES = 64 * 1024


def validate_workflow_output_text(value: Any) -> str | None:
    """Return optional output verbatim, refusing non-text or oversized input."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("outputText must be text when provided")
    size = len(value.encode("utf-8"))
    if size > MAX_WORKFLOW_OUTPUT_TEXT_BYTES:
        raise ValueError(
            "outputText exceeds the maximum of "
            f"{MAX_WORKFLOW_OUTPUT_TEXT_BYTES} UTF-8 bytes (received {size})"
        )
    return value
