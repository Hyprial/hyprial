"""Shared duration parsing independent of workflow or routine schemas."""

from __future__ import annotations

import re

_DURATION = re.compile(r"([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)\Z")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


class DurationParseError(ValueError):
    """A duration value could not be interpreted without guessing."""


def parse_duration(value: object, label: str) -> float:
    """Parse seconds or a ``<number><ms|s|m|h>`` string into seconds.

    Booleans and non-positive values are rejected.  Keeping this parser in a
    dependency-free leaf lets workflow, routine, and PAC-facing configuration
    use one unit contract without assigning ownership to an execution engine.
    """

    if isinstance(value, bool):
        raise DurationParseError(f"{label} must be a duration, not a boolean")
    if isinstance(value, (int, float)):
        seconds = float(value)
    elif isinstance(value, str):
        match = _DURATION.match(value.strip())
        if match is None:
            raise DurationParseError(
                f"{label} must be seconds or a '<number><ms|s|m|h>' string, got {value!r}"
            )
        seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2)]
    else:
        raise DurationParseError(
            f"{label} must be a duration, got {type(value).__name__}"
        )
    if seconds <= 0:
        raise DurationParseError(f"{label} must be positive, got {seconds}s")
    return seconds


__all__ = ["DurationParseError", "parse_duration"]
