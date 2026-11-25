"""Synthetic owner-record materials; never an operator key, home or daemon."""
import pytest

import hyprial.identity as identity
from record_vectors import OWNER, OWNER_PRIVATE, kid, owner_public


@pytest.fixture
def core():
    return identity.ProviderKeys


@pytest.fixture
def signer(core):
    return core.SigningKey(OWNER, OWNER_PRIVATE)


@pytest.fixture
def owner_key(core):
    return core.OwnerKey(OWNER, kid("ed25519", owner_public()), owner_public())
