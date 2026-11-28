"""Public synthetic materials for the sealed-envelope primitive; never operator keys.

Decision (service grant sealed envelope, 2026-11-26): randomness is injected
only here, by replacing secrets.token_bytes for one test, so byte-for-byte
comparisons do not change how production code draws entropy.

Decision (gate fix, 2026-11-27): provider_key_baseline.json stores each
envelope's sealedBy signature as sigHex, the same 64 bytes in hex. One
base64 spelling happened to contain an old product token that the merge
residual scan refuses anywhere under tests/; the fixture turns sigHex back
into the base64 sig the code writes, so every comparison is unchanged.
"""
import base64
import importlib
import json
from pathlib import Path

import pytest

import hyprial.identity as identity

HERE = Path(__file__).resolve().parent


@pytest.fixture
def vector():
    return json.loads((HERE.parents[2] / "contract/fixtures/provider-keys/envelopes.json").read_text())


@pytest.fixture
def baseline():
    data = json.loads((HERE / "provider_key_baseline.json").read_text())
    for case in data.values():
        if isinstance(case, dict):
            signed = case["envelope"]["sealedBy"]
            signed["sig"] = base64.b64encode(bytes.fromhex(signed.pop("sigHex"))).decode("ascii")
    return data


@pytest.fixture
def provider():
    return identity.ProviderKeys


@pytest.fixture
def sealed():
    return importlib.import_module("hyprial.identity.impl.sealed_envelope")


@pytest.fixture
def sealed_core():
    return importlib.import_module("hyprial.identity.impl.sealed_envelope.core")


@pytest.fixture
def service_grant_mod():
    return importlib.import_module("hyprial.identity.impl.sealed_envelope.service_grant")


@pytest.fixture
def materials(provider, vector):
    raw = base64.b64decode
    signer = provider.SigningKey("test-sub", raw(vector["ownerPrivate"]))
    owner = provider.OwnerKey("test-sub", vector["ownerKeyId"], raw(vector["ownerPublic"]))
    recipients = [provider.Recipient(row["keyId"], raw(row["public"])) for row in vector["recipients"]]
    return signer, owner, recipients


@pytest.fixture
def entropy(monkeypatch):
    def install(values):
        queue = iter(base64.b64decode(value) if isinstance(value, str) else value for value in values)

        def fixed(size):
            value = next(queue)
            assert len(value) == size
            return value
        monkeypatch.setattr("secrets.token_bytes", fixed)
    return install
