"""Slice 4a: decoders ignore unknown extra header fields (forward compatibility)."""

from __future__ import annotations

import pytest

from ..support import teleport
from .samples import KINDS, base_headers, codec


@pytest.mark.parametrize("kind", KINDS)
def test_unknown_header_field_is_ignored(kind: str) -> None:
    header = base_headers()[kind]
    expected = codec(kind)[1](teleport().encode_frame(header))
    header["futureField"] = {"anything": [1, 2, 3]}
    assert codec(kind)[1](teleport().encode_frame(header)) == expected


def test_unknown_field_inside_routine_entries_is_ignored() -> None:
    header = base_headers()["receive_request"]
    header["routines"][0]["extra"] = 1
    request = codec("receive_request")[1](teleport().encode_frame(header))
    assert request.routines[0].name == "nightly"
