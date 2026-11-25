"""Actual local CA and signed tailnet association."""

import pytest

from hyprial.identity import TailnetCredentialError

from .support import LocalContext, state, secret, prepare, journal, inventory


def test_authority_local_bootstrap_does_not_require_a_self_derived_key(tmp_path):
    context = LocalContext(tmp_path / "home")
    descriptor = context.provision()
    assert descriptor.target.authority == context.pin("tailnet-A")
    assert (state(context.home) / "descriptor.json").is_file()
    assert context.members("tailnet-A")[0].client_public_by_server == ()


def test_local_execute_really_signs_csr_and_saves_received_without_active_commit(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from hyprial.identity.impl.device.binding.jcs import canonical_bytes
    from hashlib import sha256
    context = LocalContext(tmp_path / "home")
    descriptor = context.provision()
    operation = prepare(context)
    result = context.runtime().execute(operation["operationId"])
    assert result["phase"] == "RECEIVED"
    assert result["remoteOutcome"] == "SUCCESS"
    value, response = journal(context, operation["operationId"])
    assert response["receipt"]["requestSha256"] == value["requestSha256"]
    data = response["data"]
    certificate = x509.load_pem_x509_certificate(data["credential"]["certificatePem"].encode())
    ca = x509.load_pem_x509_certificate(descriptor.ca_certificate_pem)
    ca.public_key().verify(certificate.signature, certificate.tbs_certificate_bytes, padding.PKCS1v15(), hashes.SHA256())
    assert certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.UniformResourceIdentifier) == [
        "https://auth.hyprial.com/transport/tailnet-association/v1/sha256/" + sha256(canonical_bytes(data["association"])).hexdigest()]
    assert data["association"]["deviceUidProvenance"] == "member-self-reported"
    assert context.runtime().read() is None
    assert not (state(context.home) / "active.json").exists()


@pytest.mark.parametrize("point", ["caKey_DURABLE", "serverKey_DURABLE", "ca_DURABLE", "server_DURABLE", "MATERIAL_DURABLE"])
def test_ca_initialization_interrupt_never_generates_a_replacement_ca(tmp_path, monkeypatch, point):
    from hyprial.identity.impl.device.tailnet.authority import issuer
    context = LocalContext(tmp_path / "home")

    def interrupt(name):
        if name == point:
            raise KeyboardInterrupt

    monkeypatch.setattr(issuer, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        context.provision()
    old_key = (secret(context.home) / "authority/ca-key.pem").read_bytes()
    before = inventory(context.home)
    monkeypatch.setattr(issuer, "_checkpoint", lambda name: None)
    if point == "MATERIAL_DURABLE":
        context.provision()
    else:
        with pytest.raises(TailnetCredentialError) as caught:
            context.provision()
        assert caught.value.code == "UNSAFE_AUTHORITY_STATE"
        assert inventory(context.home) == before
    assert (secret(context.home) / "authority/ca-key.pem").read_bytes() == old_key


@pytest.mark.parametrize("mutation", ["symlink", "hardlink", "mode", "directory"])
def test_authority_ca_rejects_unsafe_key_paths_without_touching_outside(tmp_path, mutation):
    import os
    context = LocalContext(tmp_path / "home")
    context.provision()
    path = secret(context.home) / "authority/ca-key.pem"
    original = path.read_bytes()
    outside = tmp_path / "external-key-sentinel"
    outside.write_bytes(original)
    outside.chmod(0o600)
    if mutation in ("symlink", "hardlink"):
        path.unlink()
        path.symlink_to(outside) if mutation == "symlink" else os.link(outside, path)
    elif mutation == "mode":
        path.chmod(0o644)
    else:
        path.parent.chmod(0o755)
    with pytest.raises(TailnetCredentialError):
        context.provision()
    assert outside.read_bytes() == original


def test_authority_ca_is_independent_per_tailnet_and_survives_a_new_facade(tmp_path):
    context = LocalContext(tmp_path / "home")
    a = context.provision("tailnet-A")
    b = context.provision("tailnet-B")
    assert a.ca_sha256 != b.ca_sha256
    assert context.provision("tailnet-A") == a
    assert context.provision("tailnet-B") == b


@pytest.mark.parametrize("with_store", [False, True])
def test_lost_material_cannot_reset_a_previously_trusted_authority_ca(tmp_path, monkeypatch, with_store):
    from hyprial.identity.impl.device.tailnet.authority import issuer
    from .support import body, call
    context = LocalContext(tmp_path / "home")
    context.provision()
    if with_store:
        assert call(context, body(context, csr="seed receipt"))[0] == 400
    backup = tmp_path / "missing-material"
    backup.mkdir(mode=0o700)
    for path in (secret(context.home) / "authority/ca-key.pem", secret(context.home) / "authority/server-key.pem",
                 state(context.home) / "authority/ca.pem", state(context.home) / "authority/server.pem",
                 state(context.home) / "authority/material.json"):
        path.rename(backup / path.name)
    before = inventory(context.home)
    generated = []
    actual_generate = issuer.rsa.generate_private_key

    def observed_generation(*args, **kwargs):
        generated.append(True)
        return actual_generate(*args, **kwargs)

    monkeypatch.setattr(issuer.rsa, "generate_private_key", observed_generation)
    with pytest.raises(TailnetCredentialError) as caught:
        context.provision()
    assert generated == [], "prior descriptor/store trust must stop before key generation"
    assert caught.value.code == "UNSAFE_AUTHORITY_STATE"
    assert inventory(context.home) == before
