from __future__ import annotations

import json
import multiprocessing
import os
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from hyprial.identity.impl.device import transport_credential as credential_module
from hyprial.identity.interface import (
    DeviceRecord,
    TransportCredentialError,
    binding_claims,
    binding_uri,
    commit_transport_credential,
    read_transport_credential,
    retire_transport_credential,
    stage_transport_credential,
    transport_credential_status,
    validate_enrollment_response,
    write_device_record,
)

OWNER = "alice@example.test"
OWNER_USER_ID = "casdoor-user-alice"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _device_record(device_uid: str, *, owner: str = OWNER) -> DeviceRecord:
    return DeviceRecord(
        device_id="node-test",
        owner=owner,
        server_public="nodekey:server",
        client_public="nodekey:client",
        device_uid=device_uid,
        name="test-device",
    )


def _stage(
    home: Path,
    *,
    owner: str = OWNER,
    operation_id: str | None = None,
    device_uid: str | None = None,
):
    uid = device_uid or uuid4().hex
    if not (home / "state/tailcat/device.json").exists():
        write_device_record(home, _device_record(uid, owner=owner))
    return stage_transport_credential(
        home,
        operation_id=operation_id or str(uuid4()),
        owner=owner,
        device_uid=uid,
        expected_credential_generation=0,
    )


def _ca(
    common_name: str = "test transport root", *, path_length: int | None = None
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    now = datetime.now(timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=path_length), critical=True
        )
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _intermediate_ca(
    issuer_key: rsa.RSAPrivateKey,
    issuer: x509.Certificate,
    *,
    key_cert_sign: bool = True,
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    now = datetime.now(timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test intermediate")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=7))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=key_cert_sign,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(issuer_key, hashes.SHA256())
    )
    return key, cert


def _response(
    staged,
    *,
    not_after_delta: timedelta = timedelta(hours=1),
    leaf_ca: bool = False,
    include_key_usage: bool = True,
    include_eku: bool = True,
    include_server_auth: bool = True,
    root_path_length: int | None = None,
    include_intermediate: bool = False,
    intermediate_key_cert_sign: bool = True,
) -> tuple[dict[str, object], bytes]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    ca_key, ca_cert = _ca(path_length=root_path_length)
    issuer_key, issuer_cert = ca_key, ca_cert
    chain = ca_cert.public_bytes(serialization.Encoding.PEM)
    if include_intermediate:
        issuer_key, issuer_cert = _intermediate_ca(
            ca_key, ca_cert, key_cert_sign=intermediate_key_cert_sign
        )
        chain = issuer_cert.public_bytes(serialization.Encoding.PEM) + chain
    csr = x509.load_pem_x509_csr(staged.csr_pem.encode("ascii"))
    binding_id = str(uuid4())
    generation = 1
    cn = f"hdev-{binding_id}-g{generation}"
    not_before = now - timedelta(minutes=1)
    not_after = now + not_after_delta
    claims = binding_claims(
        policy_issuer="https://authority.example.test",
        binding_id=binding_id,
        owner_user_id=OWNER_USER_ID,
        owner=staged.owner,
        device_uid=staged.device_uid,
        credential_generation=generation,
        spki_sha256=staged.spki_sha256,
        renewable_until=int((not_after + timedelta(days=7)).timestamp()),
        trust_bundle_id="test-root-v1",
    )
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(issuer_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.BasicConstraints(ca=leaf_ca, path_length=None), critical=True
        )
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(binding_uri(claims))]
            ),
            critical=False,
        )
    )
    if include_key_usage:
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
    if include_eku:
        usages = [ExtendedKeyUsageOID.CLIENT_AUTH]
        if include_server_auth:
            usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
        builder = builder.add_extension(x509.ExtendedKeyUsage(usages), critical=False)
    cert = builder.sign(issuer_key, hashes.SHA256())
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM)
    response: dict[str, object] = {
        "ownerUserId": OWNER_USER_ID,
        "owner": staged.owner,
        "bindingId": binding_id,
        "deviceUid": staged.device_uid,
        "credentialGeneration": generation,
        "certificatePem": cert_pem,
        "chainPem": chain.decode("ascii"),
        "cn": cn,
        "spkiSha256": staged.spki_sha256,
        "serial": format(cert.serial_number, "x"),
        "notBefore": int(not_before.timestamp()),
        "notAfter": int(not_after.timestamp()),
        "renewableUntil": int((not_after + timedelta(days=7)).timestamp()),
        "trustBundleId": "test-root-v1",
        "policyIssuer": "https://authority.example.test",
    }
    return response, ca_pem


def _validate(staged, response: dict[str, object], bundle: bytes):
    return validate_enrollment_response(
        response,
        staged=staged,
        pinned_trust_bundle_pem=bundle,
        effective_owner=OWNER,
        now=datetime.now(timezone.utc),
    )


def _commit_then_abort(home, staged, credential, bundle: bytes, checkpoint: str) -> None:
    def abort_at(observed: str) -> None:
        if observed == checkpoint:
            os._exit(86)

    credential_module._commit_checkpoint = abort_at
    commit_transport_credential(
        home,
        staged=staged,
        credential=credential,
        pinned_trust_bundle_pem=bundle,
    )
    os._exit(0)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (lambda response, _staged: response.__setitem__("owner", "mallory"), "TRANSPORT_OWNER_MISMATCH"),
        (lambda response, _staged: response.__setitem__("spkiSha256", "0" * 64), "TRANSPORT_SPKI_MISMATCH"),
        (lambda response, _staged: response.__setitem__("credentialGeneration", True), "TRANSPORT_RESPONSE_INVALID"),
        (lambda response, _staged: response.__setitem__("unexpected", "field"), "TRANSPORT_RESPONSE_INVALID"),
        (lambda response, _staged: response.__setitem__("deviceUid", uuid4().hex), "TRANSPORT_RESPONSE_INVALID"),
        (lambda response, _staged: response.__setitem__("renewableUntil", response["notAfter"] - 1), "TRANSPORT_RESPONSE_INVALID"),
    ],
    ids=["owner", "spki", "boolean-generation", "unknown-field", "device-uid", "renewal-horizon"],
)
def test_invalid_responses_are_rejected_without_committing(
    tmp_path: Path, mutation, expected_code: str
) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(staged)
    mutation(response, staged)
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    with pytest.raises(TransportCredentialError) as caught:
        _validate(staged, response, bundle)

    assert caught.value.code == expected_code
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before
    assert not (tmp_path / "state/transport/credential.json").exists()
    assert "BEGIN " not in str(caught.value)


def test_unpinned_chain_is_rejected_without_committing(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    response, _bundle = _response(staged)
    _other_key, other_ca = _ca("other root")
    other_bundle = other_ca.public_bytes(serialization.Encoding.PEM)

    with pytest.raises(TransportCredentialError) as caught:
        _validate(staged, response, other_bundle)

    assert caught.value.code == "TRANSPORT_CHAIN_UNTRUSTED"
    assert not (tmp_path / "state/transport/credential.json").exists()


def test_expiring_certificate_is_rejected_without_committing(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(staged, not_after_delta=timedelta(minutes=5))

    with pytest.raises(TransportCredentialError) as caught:
        _validate(staged, response, bundle)

    assert caught.value.code == "TRANSPORT_CERT_EXPIRING"
    assert not (tmp_path / "state/transport/credential.json").exists()


def test_chain_cannot_exceed_root_path_length(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(
        staged, root_path_length=0, include_intermediate=True
    )

    with pytest.raises(TransportCredentialError) as caught:
        _validate(staged, response, bundle)

    assert caught.value.code == "TRANSPORT_CHAIN_UNTRUSTED"


def test_chain_rejects_issuer_without_key_cert_sign(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(
        staged, include_intermediate=True, intermediate_key_cert_sign=False
    )

    with pytest.raises(TransportCredentialError) as caught:
        _validate(staged, response, bundle)

    assert caught.value.code == "TRANSPORT_CHAIN_UNTRUSTED"


def test_staging_and_response_must_match_local_device_record(tmp_path: Path) -> None:
    local_uid = uuid4().hex
    staged = _stage(tmp_path, device_uid=local_uid)

    with pytest.raises(TransportCredentialError) as caught:
        stage_transport_credential(
            tmp_path,
            operation_id=str(uuid4()),
            owner=OWNER,
            device_uid=uuid4().hex,
            expected_credential_generation=0,
        )
    assert caught.value.code == "TRANSPORT_DEVICE_MISMATCH"

    response, bundle = _response(staged)
    write_device_record(tmp_path, _device_record(uuid4().hex))
    with pytest.raises(TransportCredentialError) as caught:
        _validate(staged, response, bundle)
    assert caught.value.code == "TRANSPORT_DEVICE_MISMATCH"


def test_validation_rejects_symlinked_local_state_parent(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(staged)
    state_parent = tmp_path / "state"
    external = tmp_path / "external-state"
    state_parent.rename(external)
    state_parent.symlink_to(external, target_is_directory=True)

    with pytest.raises(TransportCredentialError) as caught:
        _validate(staged, response, bundle)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"


@pytest.mark.parametrize(
    "response_options",
    [
        {"leaf_ca": True},
        {"include_key_usage": False},
        {"include_eku": False},
        {"include_server_auth": False},
    ],
    ids=["ca-leaf", "missing-key-usage", "missing-eku", "missing-server-auth"],
)
def test_invalid_certificate_profiles_are_rejected(
    tmp_path: Path, response_options: dict[str, bool]
) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(staged, **response_options)

    with pytest.raises(TransportCredentialError) as caught:
        _validate(staged, response, bundle)

    assert caught.value.code == "TRANSPORT_RESPONSE_INVALID"


def test_receipt_loss_resumes_same_operation_and_private_key(tmp_path: Path) -> None:
    operation_id = str(uuid4())
    staged = _stage(tmp_path, operation_id=operation_id)
    key_path = tmp_path / f"secrets/transport/staged/{operation_id}/key.pem"
    original_key = key_path.read_bytes()

    resumed = stage_transport_credential(
        tmp_path,
        operation_id=operation_id,
        owner=OWNER,
        device_uid=staged.device_uid,
        expected_credential_generation=0,
    )

    assert resumed == staged
    assert key_path.read_bytes() == original_key
    assert resumed.csr_pem == staged.csr_pem


def test_staging_record_for_another_owner_is_never_adopted(tmp_path: Path) -> None:
    operation_id = str(uuid4())
    staged = _stage(tmp_path, operation_id=operation_id)
    key_path = tmp_path / f"secrets/transport/staged/{operation_id}/key.pem"
    original_key = key_path.read_bytes()

    with pytest.raises(TransportCredentialError) as caught:
        stage_transport_credential(
            tmp_path,
            operation_id=operation_id,
            owner="mallory@example.test",
            device_uid=staged.device_uid,
            expected_credential_generation=0,
        )

    assert caught.value.code == "TRANSPORT_OWNER_MISMATCH"
    assert key_path.read_bytes() == original_key


def test_commit_revalidates_mutated_projection(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(staged)
    credential = _validate(staged, response, bundle)

    with pytest.raises(TransportCredentialError) as caught:
        commit_transport_credential(
            tmp_path,
            staged=staged,
            credential=replace(credential, cn=f"{credential.cn}-changed"),
            pinned_trust_bundle_pem=bundle,
        )

    assert caught.value.code == "TRANSPORT_RESPONSE_INVALID"
    assert read_transport_credential(tmp_path) is None


def test_commit_rejects_symlinked_staged_key_parent_without_external_write(
    tmp_path: Path,
) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(staged)
    credential = _validate(staged, response, bundle)
    operation_root = staged._key_path.parent
    original_root = operation_root.with_name(f"{operation_root.name}.original")
    operation_root.rename(original_root)
    external = tmp_path / "external-operation"
    external.mkdir(mode=0o700)
    external_key = external / "key.pem"
    key_bytes = (original_root / "key.pem").read_bytes()
    external_key.write_bytes(key_bytes)
    os.chmod(external_key, 0o600)
    operation_root.symlink_to(external, target_is_directory=True)

    with pytest.raises(TransportCredentialError) as caught:
        commit_transport_credential(
            tmp_path,
            staged=staged,
            credential=credential,
            pinned_trust_bundle_pem=bundle,
        )

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert external_key.read_bytes() == key_bytes
    assert not (tmp_path / "secrets/transport/device-key.pem").exists()
    assert not (tmp_path / "state/transport/credential.json").exists()


def test_key_rename_without_credential_reads_as_absent(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    source = tmp_path / f"secrets/transport/staged/{staged.operation_id}/key.pem"
    destination = tmp_path / "secrets/transport/device-key.pem"
    os.replace(source, destination)  # crash after the first commit rename

    assert read_transport_credential(tmp_path) is None


@pytest.mark.parametrize(
    "checkpoint",
    [
        "credential-temp-written",
        "key-installed",
        "bundle-installed",
        "credential-installed",
    ],
)
def test_real_process_abort_is_resumable_or_absent(
    tmp_path: Path, checkpoint: str
) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(staged)
    credential = _validate(staged, response, bundle)
    process = multiprocessing.get_context("spawn").Process(
        target=_commit_then_abort,
        args=(tmp_path, staged, credential, bundle, checkpoint),
    )

    process.start()
    process.join(timeout=30)
    exitcode = process.exitcode
    process.close()

    assert exitcode == 86
    loaded = read_transport_credential(tmp_path)
    if loaded is not None:
        assert checkpoint == "credential-installed"
        assert (tmp_path / "state/transport/trust-bundle.pem").is_file()
        return

    resumed = stage_transport_credential(
        tmp_path,
        operation_id=staged.operation_id,
        owner=staged.owner,
        device_uid=staged.device_uid,
        expected_credential_generation=staged.expected_credential_generation,
    )
    commit_transport_credential(
        tmp_path,
        staged=resumed,
        credential=credential,
        pinned_trust_bundle_pem=bundle,
    )
    assert read_transport_credential(tmp_path) == credential


def test_read_rejects_symlinked_parent_even_when_credential_is_incomplete(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external-secrets"
    (external / "transport").mkdir(parents=True, mode=0o700)
    (tmp_path / "secrets").symlink_to(external, target_is_directory=True)

    with pytest.raises(TransportCredentialError) as caught:
        read_transport_credential(tmp_path)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"


@pytest.mark.parametrize(
    "relative",
    [
        "secrets/transport/device-key.pem",
        "state/transport/credential.json",
        "state/transport/trust-bundle.pem",
    ],
)
def test_read_rejects_symlinked_material(tmp_path: Path, relative: str) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(staged)
    credential = _validate(staged, response, bundle)
    commit_transport_credential(
        tmp_path,
        staged=staged,
        credential=credential,
        pinned_trust_bundle_pem=bundle,
    )
    path = tmp_path / relative
    target = tmp_path / f"target-{path.name}"
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)

    with pytest.raises(TransportCredentialError) as caught:
        read_transport_credential(tmp_path)
    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"


@pytest.mark.parametrize(
    ("relative", "mode"),
    [
        ("secrets/transport", 0o755),
        ("state/transport", 0o755),
        ("secrets/transport/device-key.pem", 0o644),
        ("state/transport/credential.json", 0o644),
        ("state/transport/trust-bundle.pem", 0o600),
    ],
)
def test_read_rejects_unsafe_modes(tmp_path: Path, relative: str, mode: int) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(staged)
    credential = _validate(staged, response, bundle)
    commit_transport_credential(
        tmp_path,
        staged=staged,
        credential=credential,
        pinned_trust_bundle_pem=bundle,
    )
    os.chmod(tmp_path / relative, mode)

    with pytest.raises(TransportCredentialError) as caught:
        read_transport_credential(tmp_path)
    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"


def test_stage_rejects_symlinked_device_identity_without_creating_material(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    device_uid = uuid4().hex
    write_device_record(home, _device_record(device_uid))
    identity_path = home / "state/tailcat/device-identity.json"
    external_identity = tmp_path / "external-device-identity.json"
    external_identity.write_bytes(identity_path.read_bytes())
    os.chmod(external_identity, 0o600)
    identity_path.unlink()
    identity_path.symlink_to(external_identity)

    with pytest.raises(TransportCredentialError) as caught:
        _stage(home, device_uid=device_uid)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert not (home / "secrets/transport").exists()
    assert not (home / "state/transport").exists()
    assert identity_path.is_symlink()
    assert external_identity.is_file()


def test_stage_does_not_create_through_symlinked_parent(tmp_path: Path) -> None:
    device_uid = uuid4().hex
    write_device_record(tmp_path, _device_record(device_uid))
    external = tmp_path / "external-secrets"
    external.mkdir(mode=0o700)
    (tmp_path / "secrets").symlink_to(external, target_is_directory=True)

    with pytest.raises(TransportCredentialError) as caught:
        _stage(tmp_path, device_uid=device_uid)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert not (external / "transport").exists()


def test_stage_preflights_all_parents_before_modifying(tmp_path: Path) -> None:
    device_uid = uuid4().hex
    external_home = tmp_path / "external-home"
    write_device_record(external_home, _device_record(device_uid))
    (tmp_path / "state").symlink_to(
        external_home / "state", target_is_directory=True
    )

    with pytest.raises(TransportCredentialError) as caught:
        _stage(tmp_path, device_uid=device_uid)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert not os.path.lexists(tmp_path / "secrets")
    assert not (external_home / "state/transport").exists()


def test_stage_preflights_operation_root_before_other_writes(tmp_path: Path) -> None:
    device_uid = uuid4().hex
    operation_id = str(uuid4())
    write_device_record(tmp_path, _device_record(device_uid))
    staged_root = tmp_path / "secrets/transport/staged"
    staged_root.mkdir(parents=True, mode=0o700)
    os.chmod(tmp_path / "secrets/transport", 0o700)
    os.chmod(staged_root, 0o700)
    external = tmp_path / "external-operation"
    external.mkdir(mode=0o700)
    (staged_root / operation_id).symlink_to(external, target_is_directory=True)
    state_root = tmp_path / "state/transport"
    assert not state_root.exists()

    with pytest.raises(TransportCredentialError) as caught:
        _stage(tmp_path, device_uid=device_uid, operation_id=operation_id)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert not state_root.exists()
    assert not list(external.iterdir())


def test_stage_rejects_unsafe_enrollment_before_creating_secret_roots(
    tmp_path: Path,
) -> None:
    device_uid = uuid4().hex
    write_device_record(tmp_path, _device_record(device_uid))
    state_root = tmp_path / "state/transport"
    state_root.mkdir(mode=0o700)
    target = tmp_path / "external-enrollment"
    target.write_text("{}", encoding="utf-8")
    (state_root / "enrollment.json").symlink_to(target)
    secrets_root = tmp_path / "secrets/transport"
    assert not secrets_root.exists()

    with pytest.raises(TransportCredentialError) as caught:
        _stage(tmp_path, device_uid=device_uid)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert not secrets_root.exists()
    assert target.read_text("utf-8") == "{}"


def test_stage_rejects_symlinked_key_before_other_writes(tmp_path: Path) -> None:
    device_uid = uuid4().hex
    operation_id = str(uuid4())
    write_device_record(tmp_path, _device_record(device_uid))
    operation_root = tmp_path / f"secrets/transport/staged/{operation_id}"
    operation_root.mkdir(parents=True, mode=0o700)
    for directory in (
        tmp_path / "secrets/transport",
        tmp_path / "secrets/transport/staged",
        operation_root,
    ):
        os.chmod(directory, 0o700)
    external_key = tmp_path / "external-key.pem"
    external_key.write_text("external", encoding="utf-8")
    os.chmod(external_key, 0o600)
    (operation_root / "key.pem").symlink_to(external_key)
    state_root = tmp_path / "state/transport"
    assert not state_root.exists()

    with pytest.raises(TransportCredentialError) as caught:
        _stage(tmp_path, device_uid=device_uid, operation_id=operation_id)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert not state_root.exists()
    assert external_key.read_text("utf-8") == "external"


def test_resume_rejects_permissive_operation_directory(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    operation_root = tmp_path / f"secrets/transport/staged/{staged.operation_id}"
    os.chmod(operation_root, 0o755)

    with pytest.raises(TransportCredentialError) as caught:
        _stage(tmp_path, operation_id=staged.operation_id, device_uid=staged.device_uid)
    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"


def test_resume_rejects_symlinked_or_permissive_enrollment(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    enrollment = tmp_path / "state/transport/enrollment.json"
    target = tmp_path / "enrollment-target.json"
    target.write_bytes(enrollment.read_bytes())
    enrollment.unlink()
    enrollment.symlink_to(target)

    with pytest.raises(TransportCredentialError) as caught:
        _stage(tmp_path, operation_id=staged.operation_id, device_uid=staged.device_uid)
    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"

    enrollment.unlink()
    enrollment.write_bytes(target.read_bytes())
    os.chmod(enrollment, 0o644)
    with pytest.raises(TransportCredentialError) as caught:
        _stage(tmp_path, operation_id=staged.operation_id, device_uid=staged.device_uid)
    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"


def test_read_rejects_unsafe_staged_enrollment_before_absent_result(
    tmp_path: Path,
) -> None:
    _stage(tmp_path)
    enrollment = tmp_path / "state/transport/enrollment.json"
    os.chmod(enrollment, 0o644)

    with pytest.raises(TransportCredentialError) as caught:
        read_transport_credential(tmp_path)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"


def test_read_requires_the_committed_trust_bundle(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(staged)
    credential = _validate(staged, response, bundle)
    commit_transport_credential(
        tmp_path,
        staged=staged,
        credential=credential,
        pinned_trust_bundle_pem=bundle,
    )
    _key, other_ca = _ca("replacement root")
    trust_path = tmp_path / "state/transport/trust-bundle.pem"
    trust_path.write_bytes(other_ca.public_bytes(serialization.Encoding.PEM))
    os.chmod(trust_path, 0o644)

    assert read_transport_credential(tmp_path) is None


@pytest.mark.parametrize("field", ["trustBundleId", "trustBundleSha256"])
def test_read_rejects_tampered_bundle_identity_or_hash(
    tmp_path: Path, field: str
) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(staged)
    credential = _validate(staged, response, bundle)
    commit_transport_credential(
        tmp_path,
        staged=staged,
        credential=credential,
        pinned_trust_bundle_pem=bundle,
    )
    credential_path = tmp_path / "state/transport/credential.json"
    record = json.loads(credential_path.read_text("utf-8"))
    assert "trustBundleSha256" in record
    record[field] = "wrong-bundle-id" if field == "trustBundleId" else "0" * 64
    credential_path.write_text(json.dumps(record), encoding="utf-8")
    os.chmod(credential_path, 0o600)

    assert read_transport_credential(tmp_path) is None


def test_commit_read_status_and_file_modes_do_not_expose_pem(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(staged)
    credential = _validate(staged, response, bundle)

    commit_transport_credential(
        tmp_path,
        staged=staged,
        credential=credential,
        pinned_trust_bundle_pem=bundle,
    )

    loaded = read_transport_credential(tmp_path)
    assert loaded == credential
    status = transport_credential_status(loaded)
    assert status == {
        "cn": credential.cn,
        "bindingId": credential.binding_id,
        "credentialGeneration": 1,
        "spkiSha256": credential.spki_sha256[:12],
        "notAfter": credential.not_after,
        "renewableUntil": credential.renewable_until,
    }
    assert _mode(tmp_path / "secrets/transport") == 0o700
    assert _mode(tmp_path / "state/transport") == 0o700
    assert _mode(tmp_path / "secrets/transport/device-key.pem") == 0o600
    assert _mode(tmp_path / "state/transport/credential.json") == 0o600
    assert _mode(tmp_path / "state/transport/trust-bundle.pem") == 0o644
    enrollment = tmp_path / "state/transport/enrollment.json"
    assert not enrollment.exists()
    observed = json.dumps(status, sort_keys=True) + "\n" + "\n".join(
        record.getMessage() for record in caplog.records
    )
    assert "BEGIN " not in observed


def test_retire_preflights_all_roots_before_mutation(tmp_path: Path) -> None:
    secrets_root = tmp_path / "secrets/transport"
    secrets_root.mkdir(parents=True, mode=0o700)
    sentinel = secrets_root / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    state_root = tmp_path / "state/transport"
    state_root.parent.mkdir(parents=True)
    state_root.write_text("unsafe", encoding="utf-8")

    with pytest.raises(TransportCredentialError) as caught:
        retire_transport_credential(tmp_path)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert sentinel.read_text("utf-8") == "keep"
    assert state_root.read_text("utf-8") == "unsafe"


def test_retire_preflight_preserves_symlink_when_other_root_is_unsafe(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external-secrets"
    external.mkdir(mode=0o700)
    sentinel = external / "sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    secrets_root = tmp_path / "secrets/transport"
    secrets_root.parent.mkdir(parents=True)
    secrets_root.symlink_to(external, target_is_directory=True)
    state_root = tmp_path / "state/transport"
    state_root.parent.mkdir(parents=True)
    state_root.write_text("unsafe", encoding="utf-8")

    with pytest.raises(TransportCredentialError) as caught:
        retire_transport_credential(tmp_path)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert secrets_root.is_symlink()
    assert sentinel.read_text("utf-8") == "keep"


def test_retire_preflight_preserves_state_when_secret_root_is_unsafe(
    tmp_path: Path,
) -> None:
    secrets_root = tmp_path / "secrets/transport"
    secrets_root.parent.mkdir(parents=True)
    secrets_root.write_text("unsafe", encoding="utf-8")
    state_root = tmp_path / "state/transport"
    state_root.mkdir(parents=True, mode=0o700)
    sentinel = state_root / "credential.json"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(TransportCredentialError) as caught:
        retire_transport_credential(tmp_path)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert secrets_root.read_text("utf-8") == "unsafe"
    assert sentinel.read_text("utf-8") == "keep"


def test_retire_preflights_directory_credential_before_any_deletion(
    tmp_path: Path,
) -> None:
    staged = _stage(tmp_path)
    secrets_root = tmp_path / "secrets/transport"
    staged_key = staged._key_path
    staged_key_bytes = staged_key.read_bytes()
    enrollment = tmp_path / "state/transport/enrollment.json"
    enrollment_bytes = enrollment.read_bytes()
    credential = tmp_path / "state/transport/credential.json"
    credential.mkdir(mode=0o700)
    trust_bundle = tmp_path / "state/transport/trust-bundle.pem"
    trust_bundle.write_bytes(b"trust-bundle-sentinel")
    os.chmod(trust_bundle, 0o644)

    with pytest.raises(TransportCredentialError) as caught:
        retire_transport_credential(tmp_path)

    assert caught.value.code == "TRANSPORT_STORAGE_UNSAFE"
    assert secrets_root.is_dir()
    assert staged_key.read_bytes() == staged_key_bytes
    assert enrollment.read_bytes() == enrollment_bytes
    assert credential.is_dir()
    assert trust_bundle.read_bytes() == b"trust-bundle-sentinel"


def test_retire_does_not_follow_symlinked_state_root(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    state_root = tmp_path / "state/transport"
    external = tmp_path / "external-transport-state"
    state_root.rename(external)
    marker = external / "credential.json"
    marker.write_text("external-marker", encoding="utf-8")
    state_root.symlink_to(external, target_is_directory=True)

    retire_transport_credential(tmp_path)

    assert marker.read_text("utf-8") == "external-marker"
    assert (external / "enrollment.json").is_file()
    assert not os.path.lexists(state_root)
    assert not (tmp_path / "secrets/transport").exists()
    assert staged.operation_id


def test_retire_removes_transport_material_but_keeps_tailcat_key(tmp_path: Path) -> None:
    staged = _stage(tmp_path)
    response, bundle = _response(staged)
    credential = _validate(staged, response, bundle)
    commit_transport_credential(
        tmp_path,
        staged=staged,
        credential=credential,
        pinned_trust_bundle_pem=bundle,
    )
    extra_operation = str(uuid4())
    stage_transport_credential(
        tmp_path,
        operation_id=extra_operation,
        owner=OWNER,
        device_uid=staged.device_uid,
        expected_credential_generation=1,
    )
    tailcat_key = tmp_path / "secrets/tailcat/device-key.json"
    tailcat_key.parent.mkdir(parents=True, mode=0o700)
    tailcat_key.write_text("tailcat-private-material", encoding="utf-8")

    retire_transport_credential(tmp_path)

    assert not (tmp_path / "secrets/transport").exists()
    assert not (tmp_path / "state/transport/credential.json").exists()
    assert not (tmp_path / "state/transport/enrollment.json").exists()
    assert not (tmp_path / "state/transport/trust-bundle.pem").exists()
    assert tailcat_key.read_text("utf-8") == "tailcat-private-material"
