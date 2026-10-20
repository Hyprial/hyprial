"""Validation for additive Tailcat map-peer connection telemetry."""

from __future__ import annotations

from typing import Any

from .policy import ForwardingSidecarError

_BASE_ERRORS = {"eof", "reset", "timeout", "canceled"}
_CLOSE_SIDES = {"local", "peer", "both"}
_MAX_SLOTS = 256
_MAX_OTHER_LENGTH = 40


def _integer(value: object, *, maximum: int | None = None) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
        and (maximum is None or value <= maximum)
    )


def _error_class(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value in _BASE_ERRORS:
        return True
    return (
        value.startswith("other:")
        and 6 < len(value) <= 6 + _MAX_OTHER_LENGTH
        and all(character.isalnum() or character in "._-" for character in value[6:])
    )


def _invalid() -> ForwardingSidecarError:
    return ForwardingSidecarError("forwarding status has invalid connection telemetry")


def parse_connection_telemetry(value: object) -> dict[str, Any] | None:
    """Return a bounded, detached projection or reject a malformed status value."""

    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"ringCapacity", "slots"}:
        raise _invalid()
    capacity, slots = value["ringCapacity"], value["slots"]
    if not _integer(capacity, maximum=256) or capacity == 0:
        raise _invalid()
    if not isinstance(slots, list) or len(slots) > _MAX_SLOTS:
        raise _invalid()
    normalized_slots: list[dict[str, Any]] = []
    for slot in slots:
        if not isinstance(slot, dict) or set(slot) != {
            "peer", "mapPort", "closedCount", "errorCounts", "recent"
        }:
            raise _invalid()
        peer, port = slot["peer"], slot["mapPort"]
        counters, recent = slot["errorCounts"], slot["recent"]
        if (
            not isinstance(peer, str)
            or not peer
            or len(peer) > 512
            or not _integer(port, maximum=65535)
            or port == 0
            or not _integer(slot["closedCount"])
            or not isinstance(counters, dict)
            or not all(_error_class(key) and _integer(count) for key, count in counters.items())
            or not isinstance(recent, list)
            or len(recent) > capacity
        ):
            raise _invalid()
        normalized_recent = [
            _parse_close(item, peer=peer, port=port) for item in recent
        ]
        normalized_slots.append(
            {
                "peer": peer,
                "mapPort": port,
                "closedCount": slot["closedCount"],
                "errorCounts": dict(counters),
                "recent": normalized_recent,
            }
        )
    return {"ringCapacity": capacity, "slots": normalized_slots}


def _parse_close(value: object, *, peer: str, port: int) -> dict[str, Any]:
    keys = {
        "peer", "mapPort", "bytesIn", "bytesOut", "durationMs", "closeSide", "errors"
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise _invalid()
    errors = value["errors"]
    if (
        value["peer"] != peer
        or value["mapPort"] != port
        or not _integer(value["bytesIn"])
        or not _integer(value["bytesOut"])
        or not _integer(value["durationMs"])
        or not isinstance(value["closeSide"], str)
        or value["closeSide"] not in _CLOSE_SIDES
        or not isinstance(errors, dict)
        or set(errors) != {"in", "out"}
        or not _error_class(errors["in"])
        or not _error_class(errors["out"])
    ):
        raise _invalid()
    return {
        "peer": peer,
        "mapPort": port,
        "bytesIn": value["bytesIn"],
        "bytesOut": value["bytesOut"],
        "durationMs": value["durationMs"],
        "closeSide": value["closeSide"],
        "errors": {"in": errors["in"], "out": errors["out"]},
    }
