# Decision record (C3, 2026-11-25):
# - pycrdt 0.14.9 does not emit state vector entries in a fixed order
#   (plan §10), so vectors are only ever compared after decoding into
#   {client: clock}.  The codec is a local copy of the journal store's
#   varuint reader because storage/store is deleted in phase E.
# - pycrdt.get_state(update) parses an update without a Doc and raises
#   ValueError on undecodable bytes (measured, 0.14.9).  It is used only as a
#   decodability check; for a diff whose predecessor is missing it returns the
#   empty vector, so it cannot replace the vector carried in each frame.
"""Yjs v1 state vector codec and decoded-clock comparison."""

from __future__ import annotations

from collections.abc import Mapping

import pycrdt

from hyprial.daemon.impl.orgfs.sync.interface import EMPTY_VECTOR


class FrameError(ValueError):
    """A wire value that fails validation; the whole frame is refused."""

    code = "invalid-argument"


def _read_var_uint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            break
    raise FrameError("invalid Yjs state vector")


def _write_var_uint(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def decode_vector(value: bytes) -> dict[int, int]:
    """Decode a Yjs v1 state vector into ``{client: clock}``."""

    if not isinstance(value, (bytes, bytearray)) or not value:
        raise FrameError("state vector must be non-empty bytes")
    data = bytes(value)
    count, offset = _read_var_uint(data, 0)
    result: dict[int, int] = {}
    for _ in range(count):
        client, offset = _read_var_uint(data, offset)
        clock, offset = _read_var_uint(data, offset)
        if client in result:
            raise FrameError("state vector repeats a client")
        result[client] = clock
    if offset != len(data):
        raise FrameError("state vector has trailing bytes")
    return result


def encode_vector(value: Mapping[int, int]) -> bytes:
    """Canonical encoding: clients sorted ascending, zero clocks dropped."""

    items = sorted((int(client), int(clock)) for client, clock in value.items() if clock > 0)
    if not items:
        return EMPTY_VECTOR
    parts = [_write_var_uint(len(items))]
    for client, clock in items:
        parts.append(_write_var_uint(client))
        parts.append(_write_var_uint(clock))
    return b"".join(parts)


def covers(actual: bytes, required: bytes) -> bool:
    """True when ``actual`` has every clock of ``required``."""

    have = decode_vector(actual)
    return all(have.get(client, 0) >= clock for client, clock in decode_vector(required).items())


def check_update(update: bytes) -> None:
    """Refuse bytes that are not a decodable Yjs v1 update."""

    if not isinstance(update, (bytes, bytearray)) or not update:
        raise FrameError("update must be non-empty bytes")
    try:
        pycrdt.get_state(bytes(update))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        raise FrameError("update is not a decodable Yjs v1 update") from exc


__all__ = [
    "FrameError",
    "check_update",
    "covers",
    "decode_vector",
    "encode_vector",
]
