"""Signer.signing_key applies the U1 signing gates and audits without key material [security]."""
from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyprial.identity import ProviderKeys, UserKey
from hyprial.kernel import ipc_errors

from .conftest import SUB, bound_signer

OPERATION = "provider-key/login"


def _audit(signer) -> list[dict]:
    path = signer.store.root / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _refused(signer, code: str) -> None:
    with pytest.raises(UserKey.Error) as caught:
        signer.signing_key(OPERATION)
    assert caught.value.code == code
    rows = _audit(signer)
    assert rows[-1]["decision"] == "refused" and rows[-1]["code"] == code
    assert rows[-1]["purpose"] == OPERATION


def test_bound_trusted_signer_returns_owner_key_and_audits(tmp_path):
    signer = bound_signer(tmp_path / "state")
    key = signer.signing_key(OPERATION)
    assert isinstance(key, ProviderKeys.SigningKey)
    assert key.owner == SUB
    public = Ed25519PrivateKey.from_private_bytes(key.private_key).public_key().public_bytes_raw()
    assert base64.b64encode(public).decode() == signer.store.status()["publicKey"]
    rows = _audit(signer)
    assert len(rows) == 1 and rows[0]["decision"] == "signed" and rows[0]["purpose"] == OPERATION
    assert rows[0]["keyId"] == signer.store.status()["keyId"]
    assert rows[0]["payloadSha256"] is None
    raw = (signer.store.root / "audit.jsonl").read_bytes()
    assert base64.b64encode(key.private_key) not in raw
    assert key.private_key.hex().encode() not in raw
    assert base64.b64encode(key.private_key).decode() not in repr(key)


def test_default_off_signer_refuses_unbound(tmp_path):
    _refused(bound_signer(tmp_path / "state", enabled=False), ipc_errors.USER_KEY_UNBOUND)


def test_unbound_record_refuses(tmp_path):
    _refused(bound_signer(tmp_path / "state", bound=False), ipc_errors.USER_KEY_UNBOUND)


def test_untrusted_record_refuses(tmp_path):
    _refused(bound_signer(tmp_path / "state", trusted=False), ipc_errors.USER_KEY_UNBOUND)


def test_trust_for_another_public_key_refuses(tmp_path):
    signer = bound_signer(tmp_path / "state")
    other = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode()
    signer.trust = lambda record: UserKey.Trust(other, time.time() + 3600, 1)
    _refused(signer, ipc_errors.USER_KEY_UNBOUND)


def test_expired_binding_refuses(tmp_path):
    _refused(bound_signer(tmp_path / "state", expires_at=time.time() - 1), ipc_errors.USER_KEY_BINDING_EXPIRED)


def test_absent_key_refuses_absent(tmp_path):
    signer = bound_signer(tmp_path / "state")
    (signer.store.root / "key.json").unlink()
    _refused(signer, ipc_errors.USER_KEY_ABSENT)


def test_production_default_signer_refuses(tmp_path):
    signer = bound_signer(tmp_path / "state")
    default = type(signer)(signer.store)
    _refused(default, ipc_errors.USER_KEY_UNBOUND)


@pytest.mark.parametrize("operation", ["", "Provider/Login", "x" * 80, None, "a b"])
def test_operation_label_is_closed(tmp_path, operation):
    signer = bound_signer(tmp_path / "state")
    with pytest.raises(UserKey.Error) as caught:
        signer.signing_key(operation)
    assert caught.value.code == ipc_errors.PURPOSE_UNKNOWN
    assert _audit(signer)[-1]["decision"] == "refused"
