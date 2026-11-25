"""Real bootstrap frames and immutable target binding before first pin."""

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import struct

import pytest

from hyprial.identity import TailnetCredentialError

from .support import Context, encoded, frame, state, unavailable


def test_bootstrap_installs_only_socket_authenticated_descriptor(tmp_path):
    context = Context(tmp_path / "home")
    try:
        descriptor = context.provision()
        assert descriptor.target == context.targets[0]
        assert context.requests[0][:5] == b"HCA1\n"
        assert json.loads(context.requests[0][9:]) == {
            "version": "hyprial.tailnet.credential-bootstrap.v1", "tailnetId": "tailnet-A"}
        path = state(context.home) / "descriptor.json"
        original = path.read_bytes()
        assert context.provision() == descriptor
        assert path.read_bytes() == original
        assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(FrozenInstanceError):
            descriptor.target.remote_port = 1
    finally:
        context.close()


@pytest.mark.parametrize("field", ["owner", "deviceId", "masterPublic", "serverPublic", "address", "controlPort", "tailnetId", "remotePort"])
def test_bootstrap_checks_every_frozen_authority_field(tmp_path, field):
    context = Context(tmp_path / "home")

    def change(target, value):
        parent = value["authority"] if field in value["authority"] else value
        parent[field] = parent[field] + 1 if type(parent[field]) is int else "different"
        if field == "remotePort":
            value["origin"] = "https://test.credential.invalid:" + str(value[field])
        return value

    context.change = change
    try:
        with pytest.raises(TailnetCredentialError):
            context.provision()
        assert not (state(context.home) / "descriptor.json").exists()
    finally:
        context.close()


@pytest.mark.parametrize("kind", ["prefix", "oversize", "trailing", "duplicate", "unknown", "truncated", "wrong_der"])
def test_bootstrap_rejects_invalid_frames_without_pin(tmp_path, kind):
    context = Context(tmp_path / "home")
    target = context.facade().DialTarget(context.pin("tailnet-A"), 7449)
    value = context.descriptor(target)
    body = encoded(value)
    if kind == "prefix":
        context.raw = b"OTHER" + struct.pack("!I", len(body)) + body
    elif kind == "oversize":
        context.raw = b"HCA1\n" + struct.pack("!I", 65537)
    elif kind == "trailing":
        context.raw = frame(body) + b"x"
    elif kind == "duplicate":
        context.raw = frame(body[:-1] + b',"tailnetId":"tailnet-A"}')
    elif kind == "unknown":
        value["mutation"] = "enroll"
        context.raw = frame(encoded(value))
    elif kind == "truncated":
        context.raw = frame(body)[:-3]
    else:
        value["caSha256"] = "0" * 64
        context.raw = frame(encoded(value))
    try:
        with pytest.raises(TailnetCredentialError):
            context.provision()
        assert not (state(context.home) / "descriptor.json").exists()
    finally:
        context.close()


def test_bootstrap_does_not_overwrite_an_existing_pin(tmp_path):
    from tests.identity.devices.binding.support import make_ca
    context = Context(tmp_path / "home")
    try:
        context.provision()
        path = state(context.home) / "descriptor.json"
        original = path.read_bytes()
        context.ca = make_ca()
        with pytest.raises(TailnetCredentialError) as caught:
            context.provision()
        assert caught.value.code == "TAILNET_AUTHORITY_DESCRIPTOR_CONFLICT"
        assert path.read_bytes() == original
    finally:
        context.close()


def test_bootstrap_rechecks_membership_after_reading_socket(tmp_path):
    context = Context(tmp_path / "home")
    context.on_dial = lambda target: unavailable(context)
    try:
        with pytest.raises(TailnetCredentialError):
            context.provision()
        assert not (state(context.home) / "descriptor.json").exists()
    finally:
        context.close()


def test_inactive_tailnet_cannot_dial_even_with_a_known_pin(tmp_path):
    context = Context(tmp_path / "home")
    context.observations["tailnet-A"] = replace(context.membership("tailnet-A"), lease_remaining_seconds=0)
    with pytest.raises(TailnetCredentialError):
        context.provision()
    assert not context.targets


def test_bootstrap_replays_the_committed_exact_frame_golden(tmp_path):
    root = Path(__file__).resolve().parents[5]
    golden = json.loads((root / "contract/tailnet-credentials/golden/bootstrap-v1.json").read_text())
    context = Context(tmp_path / "home")
    context.raw = bytes.fromhex(golden["responseHex"])
    try:
        descriptor = context.provision()
        assert context.requests[0] == bytes.fromhex(golden["requestHex"])
        assert descriptor.ca_sha256 == golden["descriptor"]["caSha256"]
    finally:
        context.close()


def test_bootstrap_certificate_missing_basic_constraints_is_a_typed_rejection(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    context = Context(tmp_path / "home")
    certificate = context.ca.certificate
    without_constraints = (x509.CertificateBuilder()
        .subject_name(certificate.subject).issuer_name(certificate.issuer)
        .public_key(context.ca.key.public_key()).serial_number(certificate.serial_number)
        .not_valid_before(certificate.not_valid_before_utc)
        .not_valid_after(certificate.not_valid_after_utc)
        .sign(context.ca.key, hashes.SHA256()))
    context.ca = replace(context.ca, certificate=without_constraints)
    try:
        with pytest.raises(TailnetCredentialError) as caught:
            context.provision()
        assert caught.value.code == "TAILNET_BOOTSTRAP_INVALID"
        assert caught.value.__context__ is None
        assert not (state(context.home) / "descriptor.json").exists()
    finally:
        context.close()
