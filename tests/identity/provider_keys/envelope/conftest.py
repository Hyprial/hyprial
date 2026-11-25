"""Public, synthetic test material and deterministic entropy for fixed vectors."""
import base64
import json
from pathlib import Path

import pytest

import hyprial.identity as identity


@pytest.fixture
def vector():
    path = Path(__file__).resolve().parents[4] / "contract/fixtures/provider-keys/envelopes.json"
    return json.loads(path.read_text())


@pytest.fixture
def core():
    value = getattr(identity, "ProviderKeys", None)
    assert value is not None, "E1 must expose the pure provider-key namespace"
    return value


@pytest.fixture
def materials(core, vector):
    def raw(value):
        return base64.b64decode(value)
    signer = core.SigningKey("test-sub", raw(vector["ownerPrivate"]))
    owner = core.OwnerKey("test-sub", vector["ownerKeyId"], raw(vector["ownerPublic"]))
    recipients = [core.Recipient(row["keyId"], raw(row["public"])) for row in vector["recipients"]]
    return signer, owner, recipients


@pytest.fixture
def entropy(monkeypatch, vector):
    values = iter(base64.b64decode(value) for value in vector["randomValues"])
    def fixed(size):
        value = next(values)
        assert len(value) == size
        return value
    monkeypatch.setattr("secrets.token_bytes", fixed)
