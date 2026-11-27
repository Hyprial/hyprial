# Decisions:
# - Design §3.3 / plan §10: state vectors are compared only after decoding to
#   {client: clock}; the raw get_state() byte order is not stable.
# - The decoder is the Yjs v1 state-vector encoding (lib0 varuint count, then
#   varuint client / varuint clock pairs); pycrdt 0.14.9 exposes no decoder.
"""State-vector decoding and polling helpers for OrgFS contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import time
from typing import TypeVar

from hyprial.daemon.impl.orgfs import OrgFsError

T = TypeVar("T")


def _read_varuint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated state vector")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7


def decode_state_vector(raw: bytes) -> dict[int, int]:
    """Decode a Yjs v1 encoded state vector to ``{client: clock}``."""

    count, offset = _read_varuint(raw, 0)
    vector: dict[int, int] = {}
    for _ in range(count):
        client, offset = _read_varuint(raw, offset)
        clock, offset = _read_varuint(raw, offset)
        vector[client] = clock
    if offset != len(raw):
        raise ValueError("trailing bytes after state vector")
    return vector


def strictly_advanced(before: Mapping[int, int], after: Mapping[int, int]) -> bool:
    """True when ``after`` covers ``before`` and is larger for some client."""

    covers = all(after.get(client, 0) >= clock for client, clock in before.items())
    grew = any(clock > before.get(client, 0) for client, clock in after.items())
    return covers and grew


def eventually(action: Callable[[], T], *, timeout: float = 5.0) -> T:
    """Retry ``action`` until it stops raising an assertion or OrgFS error."""

    deadline = time.monotonic() + timeout
    while True:
        try:
            return action()
        except (AssertionError, OrgFsError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def holds(action: Callable[[], object], *, duration: float = 1.5) -> None:
    """Assert ``action`` keeps passing for ``duration`` seconds."""

    deadline = time.monotonic() + duration
    while True:
        action()
        if time.monotonic() >= deadline:
            return
        time.sleep(0.02)


__all__ = ["decode_state_vector", "eventually", "holds", "strictly_advanced"]
