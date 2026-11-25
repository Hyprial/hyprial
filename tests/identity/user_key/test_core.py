"""Closed purpose reservations and synthetic deterministic crypto vectors."""
import json
from pathlib import Path

import pytest

import hyprial.identity as identity

ROOT = Path(__file__).resolve().parents[3]


def api():
    core = getattr(identity, "UserKey", None)
    assert core is not None, "local user-key core required"
    return core


def test_reserved_registry_is_closed_and_inactive():
    core = api()
    fixture = json.loads((ROOT / "contract/fixtures/user-key/purposes.json").read_text())
    assert set(core.PURPOSES) == set(fixture["reserved"])
    assert all(p.fields is None for p in core.PURPOSES.values())
    assert {p.domain for p in core.PURPOSES.values()} == set(fixture["domains"])
    assert not set(fixture["deviceOnlyRefused"]) & set(core.PURPOSES)
    assert "test.fixture.v1" not in core.PURPOSES
    with pytest.raises(TypeError):
        core.PURPOSES["test.fixture.v1"] = None


def test_synthetic_envelope_golden():
    core = api()
    fixture = json.loads((ROOT / "contract/fixtures/user-key/envelopes.json").read_text())
    payload = json.loads(fixture["canonicalUtf8"])
    assert core.canonical_bytes(payload).decode() == fixture["canonicalUtf8"]
    purpose = core.Purpose(fixture["purpose"], fixture["domain"], fixture["kind"], frozenset(payload))
    envelope = {k: fixture[k] for k in ("domain", "kind", "signature")}
    envelope.update(payload=payload, signer={"keyKind": "owner", "owner": payload["owner"], "keyId": fixture["keyId"]})
    assert core.verify(envelope, fixture["publicKey"], purpose=purpose)
    expected = fixture["domain"].encode() + b"\0" + fixture["kind"].encode() + b"\0" + core.canonical_bytes(payload)
    assert expected.hex() == fixture["inputHex"]


@pytest.mark.parametrize("value", [1.5, float("nan"), {1: "not-a-string-key"}, "\ud800", 2**54])
def test_noncanonical_values_refused(value):
    core = api()
    with pytest.raises(core.Error) as caught:
        core.canonical_bytes(value)
    assert caught.value.code == "PAYLOAD_INVALID"


def test_canonical_bound_and_utf16_sort():
    core = api()
    assert core.canonical_bytes({"é": "字", "a": 1}) == '{"a":1,"é":"字"}'.encode()
    with pytest.raises(core.Error):
        core.canonical_bytes({"a": "x" * 65536})
