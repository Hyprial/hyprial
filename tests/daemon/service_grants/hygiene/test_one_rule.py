"""Each identifier rule a grant meets is defined once.

Decision (service grant integration fix, 2026-11-27): the grant document's
path segments (owner, grant document name) and the device ids inside it are
checked by the rule the sealed envelope applies to its header owners,
exported once from identity (``SealedEnvelope.service_grant_path_segment``);
the domain models and the directory store both call it. The grant id pattern
is identity's, the canonical nodekey pattern and the zero key are the
forwarding policy's, and the grants directory is the directory store's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.org.network.grant_docs import ServiceGrantDocuments
from hyprial.daemon.impl.service_grants import models
from hyprial.identity import SealedEnvelope

SRC = Path(__file__).resolve().parents[4] / "src" / "hyprial" / "daemon" / "impl"
LINE = (SRC / "service_grants", SRC / "org" / "network" / "grant_docs")
SAMPLES = (
    "alice", "device-a", "@alice", "a" * 256, "a" * 257, ".", "..", "a/b", "a\\b", "a:b",
    "a\x01b", "a\x7fb", "a\ud800b", " alice", "alice ", "", "ü-device",
)


def _accepts(check, value: str) -> bool:
    try:
        check(value)
    except ValueError:
        return False
    return True


@pytest.mark.parametrize("value", SAMPLES)
def test_the_models_apply_identitys_rule(value: str) -> None:
    expected = SealedEnvelope.service_grant_path_segment(value) and value.strip() == value

    assert _accepts(models.require_bare_owner, value) is expected


@pytest.mark.parametrize("value", ["@sg", "a\x01b", "a" * 257, "a\ud800b"])
def test_the_directory_store_refuses_a_grant_path_identity_refuses(value: str) -> None:
    assert not SealedEnvelope.service_grant_path_segment(value)
    with pytest.raises(ValueError):
        ServiceGrantDocuments._service_grant_path("alice", value)


def test_the_grant_id_pattern_is_identitys() -> None:
    assert models.GRANT_ID_PATTERN is SealedEnvelope.GRANT_ID_PATTERN


def _sources() -> list[tuple[Path, str]]:
    return [(path, path.read_text()) for root in LINE for path in sorted(root.rglob("*.py"))]


@pytest.mark.parametrize(
    "fragment",
    ["[a-z2-7]{26}", "nodekey:[0-9a-f]", '"0" * 64', "0xD800", "IDENTIFIER_MAX_CHARS"],
)
def test_no_copy_of_a_rule_is_left_in_the_line(fragment: str) -> None:
    assert [str(path) for path, text in _sources() if fragment in text] == []


def test_the_grants_directory_is_named_once() -> None:
    named = [
        str(path)
        for path in sorted(SRC.rglob("*.py"))
        if '"directory/service-grants"' in path.read_text()
    ]

    assert named == [str(SRC / "org" / "network" / "grant_docs" / "__init__.py")]
