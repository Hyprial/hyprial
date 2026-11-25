"""Records stay valid across an owner signing-key rotation (contract §8.4) [security].

The anchor verifies two bound U1 keys of the same sub: the envelope stays sealed by
the first while grants are signed by the second, and each object is verified with the
key its own sealedBy names.
"""
from __future__ import annotations

import json
import time

import pytest

from hyprial.daemon.impl.provider_keys import storage
from hyprial.daemon.impl.provider_keys.api import GRANT_TTL_MS
from hyprial.daemon.impl.user_key.verifier import Lookup
from hyprial.kernel import DaemonRequestError, ipc_errors

from ..conftest import DEVICE, NOW, SECRET, SUB, bound_signer

HALF = NOW + GRANT_TTL_MS // 2 + 1


class TwoKeys:
    """Fake U2 verifier that verifies every listed signer of the one sub."""

    def __init__(self, *signers) -> None:
        self.keys = {signer.store.status()["keyId"]: signer.store.status()["publicKey"] for signer in signers}

    def lookup(self, owner: str, key_id: str) -> Lookup:
        if key_id not in self.keys:
            return Lookup()
        return Lookup("verified", self.keys[key_id], {"revocationSeq": 1}, time.time() + 3600, DEVICE, SUB)


@pytest.fixture
def rotated(world, tmp_path):
    second_state = tmp_path / "state-b"
    second_state.mkdir(mode=0o700)
    second = bound_signer(second_state)
    world.app._provider_key_owner_key_port = TwoKeys(world.signer, second)
    return second


def _sealed_by(world, path: str) -> str:
    return json.loads(world.store.read(path)[0])["sealedBy"]["keyId"]


def test_a_renewal_under_the_new_signing_key_keeps_the_slot_launchable(world, rotated):
    key = world.login()
    ref = world.ref()
    world.service().allow(agent=ref, provider="anthropic")
    world.app._user_key_signer = rotated
    _, secrets = world.service().launch_secrets(ref, now_ms=HALF)
    assert [secret.value for secret in secrets] == [SECRET]
    grant = world.service().grants_for(ref)[0]["grant"]
    envelope = storage.key_envelope_path(key["keyId"], 1)
    assert grant["sealedBy"]["keyId"] != _sealed_by(world, envelope)
    _, again = world.service().launch_secrets(ref, now_ms=HALF + 1)
    assert [secret.value for secret in again] == [SECRET]
    rows = world.service().status(agent=ref)["grants"]
    assert [(row["state"], row["code"]) for row in rows] == [("ok", None)]


def test_a_fresh_allow_under_the_new_signing_key_launches(world, rotated):
    world.login()
    ref = world.ref()
    world.app._user_key_signer = rotated
    result = world.service().allow(agent=ref, provider="anthropic")
    assert result["changed"] is True
    _, secrets = world.service().launch_secrets(ref, now_ms=NOW + 1)
    assert [secret.value for secret in secrets] == [SECRET]


def test_a_grant_signed_by_an_unanchored_key_still_refuses(world, rotated):
    world.login()
    ref = world.ref()
    world.app._user_key_signer = rotated
    world.service().allow(agent=ref, provider="anthropic")
    world.app._provider_key_owner_key_port = TwoKeys(world.signer)
    with pytest.raises(DaemonRequestError) as caught:
        world.service().launch_secrets(ref, now_ms=NOW + 1)
    assert caught.value.code in (ipc_errors.PROVIDER_STORE_UNAVAILABLE, ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID)
    assert SECRET not in str(caught.value)
