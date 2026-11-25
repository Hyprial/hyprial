"""Identity-owned transport credential staging and durable projection storage.

This module deliberately has no authority or network client.  It owns the local
RSA key, CSR, exact enrollment projection, and crash-safe commit boundary used
by the shell identity transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from hyprial.identity.impl.device.binding import verify_credential_binding
from hyprial.identity.impl.device.binding.crypto import (
    certificate_public_key,
    certificate_signature_hash,
    load_private_key_bytes,
    verify_certificate_signature as _verify_signature,
)
from hyprial.identity.impl.device.binding.extensions import read_certificate_extensions
from hyprial.identity.impl.device.store import read_device_record
from hyprial.identity.impl.device.transport_storage import (
    TransportCredentialError,
    _complete_paths_present,
    _fsync_directory,
    _path_present,
    _preflight_directories,
    _preflight_file,
    _read_json_if_present,
    _read_safe_file,
    _read_safe_optional_file_beneath,
    _remove_recoverable_temp,
    _require_safe_directory,
    _secure_directory,
    _unsafe,
    _write_bundle_atomic,
    _write_json_exclusive,
)
from hyprial.kernel import atomic_json_write, current_uid, harden_private_path, write_private_file_atomic

TRANSPORT_SECRETS_RELPATH = "secrets/transport"
TRANSPORT_STAGED_RELPATH = "secrets/transport/staged"
TRANSPORT_KEY_RELPATH = "secrets/transport/device-key.pem"
TRANSPORT_STATE_RELPATH = "state/transport"
TRANSPORT_ENROLLMENT_RELPATH = "state/transport/enrollment.json"
TRANSPORT_CREDENTIAL_RELPATH = "state/transport/credential.json"
TRANSPORT_TRUST_BUNDLE_RELPATH = "state/transport/trust-bundle.pem"

_ENROLLMENT_KEYS = frozenset(
    {
        "operationId",
        "spkiSha256",
        "owner",
        "deviceUid",
        "expectedCredentialGeneration",
    }
)
_CREDENTIAL_KEYS = frozenset(
    {
        "ownerUserId",
        "owner",
        "bindingId",
        "deviceUid",
        "credentialGeneration",
        "certificatePem",
        "chainPem",
        "cn",
        "spkiSha256",
        "serial",
        "notBefore",
        "notAfter",
        "renewableUntil",
        "trustBundleId",
        "policyIssuer",
    }
)
_STORED_CREDENTIAL_KEYS = _CREDENTIAL_KEYS | {
    "trustBundleSha256",
    "trustBundleBindingSha256",
}
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_DEVICE_UID = re.compile(r"[0-9a-f]{32}\Z")
_CN = re.compile(
    r"hdev-([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})-g([1-9][0-9]*)\Z"
)


@dataclass(frozen=True, slots=True)
class StagedTransportCredential:
    """A resumable enrollment operation and its public request material."""

    operation_id: str
    owner: str
    device_uid: str
    expected_credential_generation: int
    spki_sha256: str
    csr_pem: str = field(repr=False)
    _key_path: Path = field(repr=False, compare=False)
    _home: Path = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class TransportCredential:
    """The frozen local projection of an Interface A response."""

    owner_user_id: str
    owner: str
    binding_id: str
    device_uid: str
    credential_generation: int
    certificate_pem: str = field(repr=False)
    chain_pem: str = field(repr=False)
    cn: str
    spki_sha256: str
    serial: str
    not_before: int
    not_after: int
    renewable_until: int
    trust_bundle_id: str
    policy_issuer: str

    def as_record(self) -> dict[str, object]:
        return {
            "ownerUserId": self.owner_user_id,
            "owner": self.owner,
            "bindingId": self.binding_id,
            "deviceUid": self.device_uid,
            "credentialGeneration": self.credential_generation,
            "certificatePem": self.certificate_pem,
            "chainPem": self.chain_pem,
            "cn": self.cn,
            "spkiSha256": self.spki_sha256,
            "serial": self.serial,
            "notBefore": self.not_before,
            "notAfter": self.not_after,
            "renewableUntil": self.renewable_until,
            "trustBundleId": self.trust_bundle_id,
            "policyIssuer": self.policy_issuer,
        }


def stage_transport_credential(
    home: Path,
    *,
    operation_id: str,
    owner: str,
    device_uid: str,
    expected_credential_generation: int,
) -> StagedTransportCredential:
    """Create an RSA-2048 key and empty-subject CSR, or resume it exactly."""

    home = Path(home)
    operation_id = _uuid4(operation_id, "operationId")
    owner = _text(owner, "owner")
    device_uid = _device_uid(device_uid)
    expected = _generation(
        expected_credential_generation,
        "expectedCredentialGeneration",
        minimum=0,
    )
    secrets_root = home / TRANSPORT_SECRETS_RELPATH
    staged_root = home / TRANSPORT_STAGED_RELPATH
    operation_root = staged_root / operation_id
    state_root = home / TRANSPORT_STATE_RELPATH
    enrollment_path = home / TRANSPORT_ENROLLMENT_RELPATH
    directories = (secrets_root, staged_root, state_root)
    _preflight_directories(
        (*directories, operation_root), root=home, final_mode=0o700
    )
    _require_local_device(home, device_uid, owner)
    key_path = operation_root / "key.pem"
    key_present = _path_present(key_path)
    enrollment = _read_json_if_present(enrollment_path)
    if enrollment is None and key_present:
        raise TransportCredentialError(
            "TRANSPORT_STAGING_CONFLICT",
            "unrecorded transport staging material exists",
        )
    for directory in directories:
        _secure_directory(directory, root=home)

    if enrollment is not None:
        parsed = _parse_enrollment(enrollment)
        key_path = staged_root / str(parsed["operationId"]) / "key.pem"
        if parsed["owner"] != owner:
            raise TransportCredentialError(
                "TRANSPORT_OWNER_MISMATCH",
                "staged transport credential belongs to another owner",
            )
        if parsed["operationId"] != operation_id:
            raise TransportCredentialError(
                "TRANSPORT_STAGING_CONFLICT",
                "another transport enrollment operation is unfinished",
            )
        if parsed["deviceUid"] != device_uid or parsed[
            "expectedCredentialGeneration"
        ] != expected:
            raise TransportCredentialError(
                "TRANSPORT_RESPONSE_INVALID",
                "staged transport credential does not match the requested device",
            )
        _require_safe_directory(key_path.parent)
        if not _path_present(key_path):
            committed_path = home / TRANSPORT_KEY_RELPATH
            if not _path_present(committed_path):
                raise TransportCredentialError(
                    "TRANSPORT_STAGING_CONFLICT",
                    "transport enrollment staging key is absent",
                )
            key_path = committed_path
        key = _load_private_key(key_path)
        spki = _public_spki_sha256(key.public_key())
        if spki != parsed["spkiSha256"]:
            raise TransportCredentialError(
                "TRANSPORT_SPKI_MISMATCH",
                "staged transport credential key does not match its record",
            )
        return _staged(parsed, key, key_path, home)

    _secure_directory(operation_root, root=home)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    try:
        write_private_file_atomic(key_path, key_bytes, exclusive=True)
        _fsync_directory(operation_root)
        spki = _public_spki_sha256(key.public_key())
        record: dict[str, object] = {
            "operationId": operation_id,
            "spkiSha256": spki,
            "owner": owner,
            "deviceUid": device_uid,
            "expectedCredentialGeneration": expected,
        }
        atomic_json_write(enrollment_path, record)
        harden_private_path(enrollment_path, mode=0o600)
        _fsync_directory(state_root)
    except Exception:
        key_path.unlink(missing_ok=True)
        enrollment_path.unlink(missing_ok=True)
        raise
    return _staged(record, key, key_path, home)


def validate_enrollment_response(
    response: Mapping[str, object],
    *,
    staged: StagedTransportCredential,
    pinned_trust_bundle_pem: bytes | str,
    effective_owner: str,
    now: datetime | None = None,
) -> TransportCredential:
    """Purely validate an exact authority response against local public state."""

    credential = _projection_from_mapping(response)
    effective_owner = _text(effective_owner, "effective owner")
    if credential.owner != effective_owner or credential.owner != staged.owner:
        raise TransportCredentialError(
            "TRANSPORT_OWNER_MISMATCH",
            "transport credential owner does not match the effective owner",
        )
    if credential.device_uid != staged.device_uid:
        raise TransportCredentialError(
            "TRANSPORT_RESPONSE_INVALID",
            "transport credential device does not match the local device",
        )
    _preflight_directories(
        (staged._home / TRANSPORT_STATE_RELPATH,),
        root=staged._home,
        final_mode=0o700,
    )
    _require_local_device(staged._home, staged.device_uid, staged.owner)
    if credential.spki_sha256 != staged.spki_sha256:
        raise TransportCredentialError(
            "TRANSPORT_SPKI_MISMATCH",
            "transport credential public key does not match the staged key",
        )

    current = _utc(now or datetime.now(timezone.utc))
    certificate = _validate_certificate_projection(
        credential, expected_spki=staged.spki_sha256, now=current
    )
    chain = _certificates(credential.chain_pem, "chainPem")
    roots = _certificates(pinned_trust_bundle_pem, "pinned trust bundle")
    _verify_chain(certificate, chain, roots, current)
    return credential


def commit_transport_credential(
    home: Path,
    *,
    staged: StagedTransportCredential,
    credential: TransportCredential,
    pinned_trust_bundle_pem: bytes | str,
) -> None:
    """Commit in the specified temp/key/projection/bundle/enrollment order."""

    home = Path(home)
    operation_id = _uuid4(staged.operation_id, "operationId")
    expected_key = home / TRANSPORT_STAGED_RELPATH / operation_id / "key.pem"
    committed_key = home / TRANSPORT_KEY_RELPATH
    if staged._home != home or staged._key_path not in (expected_key, committed_key):
        raise TransportCredentialError(
            "TRANSPORT_STAGING_CONFLICT", "transport staging location is invalid"
        )
    enrollment_path = home / TRANSPORT_ENROLLMENT_RELPATH
    secrets_root = home / TRANSPORT_SECRETS_RELPATH
    state_root = home / TRANSPORT_STATE_RELPATH
    commit_roots = (secrets_root, state_root)
    if staged._key_path == expected_key:
        commit_roots += (expected_key.parent,)
    _preflight_directories(commit_roots, root=home, final_mode=0o700)
    enrollment = _read_json_if_present(enrollment_path)
    if enrollment is None:
        raise TransportCredentialError(
            "TRANSPORT_STAGING_CONFLICT", "transport enrollment staging record is absent"
        )
    parsed = _parse_enrollment(enrollment)
    if (
        parsed["operationId"] != staged.operation_id
        or parsed["owner"] != staged.owner
        or parsed["deviceUid"] != staged.device_uid
        or parsed["expectedCredentialGeneration"]
        != staged.expected_credential_generation
        or parsed["spkiSha256"] != staged.spki_sha256
    ):
        raise TransportCredentialError(
            "TRANSPORT_STAGING_CONFLICT",
            "transport enrollment staging record changed before commit",
        )
    key = _load_private_key(staged._key_path)
    if _public_spki_sha256(key.public_key()) != staged.spki_sha256:
        raise TransportCredentialError(
            "TRANSPORT_SPKI_MISMATCH", "transport staging key changed before commit"
        )
    validate_enrollment_response(
        credential.as_record(),
        staged=staged,
        pinned_trust_bundle_pem=pinned_trust_bundle_pem,
        effective_owner=staged.owner,
    )
    credential_path = home / TRANSPORT_CREDENTIAL_RELPATH
    credential_temp = state_root / f".credential.{operation_id}.tmp"
    key_path = home / TRANSPORT_KEY_RELPATH
    trust_path = home / TRANSPORT_TRUST_BUNDLE_RELPATH
    for path, mode in (
        (key_path, 0o600),
        (credential_path, 0o600),
        (trust_path, 0o644),
        (credential_temp, 0o600),
        (state_root / f".trust-bundle.{operation_id}.tmp", 0o644),
    ):
        _preflight_file(path, mode)
    _secure_directory(secrets_root, root=home)
    _secure_directory(state_root, root=home)
    bundle = (
        pinned_trust_bundle_pem.encode("ascii")
        if isinstance(pinned_trust_bundle_pem, str)
        else pinned_trust_bundle_pem
    )
    bundle_sha256 = hashlib.sha256(bundle).hexdigest()
    stored_record = credential.as_record() | {
        "trustBundleSha256": bundle_sha256,
        "trustBundleBindingSha256": _bundle_binding_sha256(
            credential.trust_bundle_id, bundle_sha256
        ),
    }
    _remove_recoverable_temp(credential_temp, 0o600)
    try:
        _write_json_exclusive(credential_temp, stored_record, 0o600)
        _commit_checkpoint("credential-temp-written")
        if staged._key_path != key_path:
            os.replace(staged._key_path, key_path)
            _fsync_directory(staged._key_path.parent)
            _fsync_directory(secrets_root)
        _commit_checkpoint("key-installed")
        _write_bundle_atomic(trust_path, pinned_trust_bundle_pem, operation_id)
        _commit_checkpoint("bundle-installed")
        os.replace(credential_temp, credential_path)
        _fsync_directory(state_root)
        _commit_checkpoint("credential-installed")
        enrollment_path.unlink(missing_ok=True)
        _fsync_directory(state_root)
    finally:
        credential_temp.unlink(missing_ok=True)
    try:
        expected_key.parent.rmdir()
    except OSError:
        pass


def read_transport_credential(home: Path) -> TransportCredential | None:
    """Return only a complete key/projection/bundle set with the same SPKI."""

    home = Path(home)
    secrets_root = home / TRANSPORT_SECRETS_RELPATH
    state_root = home / TRANSPORT_STATE_RELPATH
    key_path = home / TRANSPORT_KEY_RELPATH
    credential_path = home / TRANSPORT_CREDENTIAL_RELPATH
    trust_path = home / TRANSPORT_TRUST_BUNDLE_RELPATH
    enrollment_path = home / TRANSPORT_ENROLLMENT_RELPATH
    _preflight_directories(
        (secrets_root, state_root), root=home, final_mode=0o700
    )
    if _path_present(state_root):
        _require_safe_directory(state_root)
        enrollment = _read_json_if_present(enrollment_path)
        if enrollment is not None:
            _parse_enrollment(enrollment)
    if not _complete_paths_present(
        (secrets_root, state_root, key_path, credential_path, trust_path)
    ):
        return None
    _require_safe_directory(secrets_root)
    _require_safe_directory(state_root)
    try:
        key = _load_private_key(key_path)
        raw = json.loads(_read_safe_file(credential_path, 0o600).decode("utf-8"))
        bundle = _read_safe_file(trust_path, 0o644)
        if not isinstance(raw, Mapping) or set(raw) != _STORED_CREDENTIAL_KEYS:
            raise _invalid("stored transport credential schema is invalid")
        bundle_sha256 = _hex64(raw["trustBundleSha256"], "trustBundleSha256")
        binding_sha256 = _hex64(
            raw["trustBundleBindingSha256"], "trustBundleBindingSha256"
        )
        credential = _projection_from_mapping(
            {key: raw[key] for key in _CREDENTIAL_KEYS}
        )
        if (
            hashlib.sha256(bundle).hexdigest() != bundle_sha256
            or _bundle_binding_sha256(credential.trust_bundle_id, bundle_sha256)
            != binding_sha256
        ):
            return None
        current = datetime.now(timezone.utc)
        cert = _validate_certificate_projection(
            credential, expected_spki=credential.spki_sha256, now=current
        )
        key_spki = _public_spki_sha256(key.public_key())
        chain = _certificates(credential.chain_pem, "chainPem")
        roots = _certificates(bundle, "pinned trust bundle")
        _verify_chain(cert, chain, roots, current)
    except TransportCredentialError as error:
        if error.code == "TRANSPORT_STORAGE_UNSAFE":
            raise
        return None
    except (OSError, ValueError, TypeError, UnicodeError):
        return None
    if key_spki != credential.spki_sha256:
        return None
    return credential


def retire_transport_credential(home: Path) -> None:
    """Remove only local transport material, never the Tailcat device key."""

    home = Path(home)
    secrets_root = home / TRANSPORT_SECRETS_RELPATH
    state_root = home / TRANSPORT_STATE_RELPATH
    _inspect_retirement_directory(home, exact_mode=False)
    _preflight_directories((secrets_root.parent, state_root.parent), root=home)
    secrets_kind = _inspect_retirement_directory(secrets_root)
    state_kind = _inspect_retirement_directory(state_root)
    if secrets_kind == "directory":
        _inspect_retirement_tree(secrets_root)
    if state_kind == "directory":
        for name, mode in (
            ("credential.json", 0o600),
            ("enrollment.json", 0o600),
            ("trust-bundle.pem", 0o644),
        ):
            _inspect_retirement_file(state_root / name, mode)
    if secrets_kind == "symlink":
        secrets_root.unlink()
        _fsync_directory(secrets_root.parent)
    elif secrets_kind == "directory":
        shutil.rmtree(secrets_root)
    if state_kind == "symlink":
        state_root.unlink()
        _fsync_directory(state_root.parent)
    elif state_kind == "directory":
        for name in ("credential.json", "enrollment.json", "trust-bundle.pem"):
            (state_root / name).unlink(missing_ok=True)
        _fsync_directory(state_root)


def _inspect_retirement_directory(path: Path, *, exact_mode: bool = True) -> str:
    try:
        parent = os.lstat(path.parent)
        metadata = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError as error:
        raise _unsafe("transport retirement path is unsafe") from error
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise _unsafe("transport retirement path is unsafe")
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink"
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != current_uid()
        or (exact_mode and stat.S_IMODE(metadata.st_mode) != 0o700)
    ):
        raise _unsafe("transport retirement path is unsafe")
    return "directory"


def _inspect_retirement_tree(root: Path) -> None:
    try:
        children = tuple(root.iterdir())
        for child in children:
            metadata = os.lstat(child)
            if stat.S_ISDIR(metadata.st_mode):
                if (
                    metadata.st_uid != current_uid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700
                ):
                    raise _unsafe("transport retirement path is unsafe")
                _inspect_retirement_tree(child)
            elif (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != current_uid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise _unsafe("transport retirement path is unsafe")
    except TransportCredentialError:
        raise
    except OSError as error:
        raise _unsafe("transport retirement path is unsafe") from error


def _inspect_retirement_file(path: Path, mode: int) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as error:
        raise _unsafe("transport retirement path is unsafe") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != current_uid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise _unsafe("transport retirement path is unsafe")


def transport_credential_status(credential: TransportCredential) -> dict[str, object]:
    """Produce the deliberately non-secret status projection."""

    return {
        "cn": credential.cn,
        "bindingId": credential.binding_id,
        "credentialGeneration": credential.credential_generation,
        "spkiSha256": credential.spki_sha256[:12],
        "notAfter": credential.not_after,
        "renewableUntil": credential.renewable_until,
    }


def _staged(
    record: Mapping[str, object], key: rsa.RSAPrivateKey, key_path: Path, home: Path
) -> StagedTransportCredential:
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(
        key, hashes.SHA256()
    )
    if list(csr.extensions):
        raise TransportCredentialError(
            "TRANSPORT_RESPONSE_INVALID", "transport CSR unexpectedly has extensions"
        )
    return StagedTransportCredential(
        operation_id=str(record["operationId"]),
        owner=str(record["owner"]),
        device_uid=str(record["deviceUid"]),
        expected_credential_generation=int(record["expectedCredentialGeneration"]),
        spki_sha256=str(record["spkiSha256"]),
        csr_pem=csr.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        _key_path=key_path,
        _home=home,
    )


def _projection_from_mapping(value: Mapping[str, object]) -> TransportCredential:
    if not isinstance(value, Mapping):
        raise _invalid("transport enrollment response must be an object")
    if set(value) != _CREDENTIAL_KEYS:
        raise _invalid("transport enrollment response schema is invalid")
    try:
        generation = _generation(value["credentialGeneration"], "credentialGeneration")
        binding_id = _uuid4(value["bindingId"], "bindingId")
        device_uid = _device_uid(value["deviceUid"])
        spki = _hex64(value["spkiSha256"], "spkiSha256")
        serial = _lower_hex(value["serial"], "serial")
        not_before = _timestamp(value["notBefore"], "notBefore")
        not_after = _timestamp(value["notAfter"], "notAfter")
        renewable_until = _timestamp(value["renewableUntil"], "renewableUntil")
        return TransportCredential(
            owner_user_id=_text(value["ownerUserId"], "ownerUserId"),
            owner=_text(value["owner"], "owner"),
            binding_id=binding_id,
            device_uid=device_uid,
            credential_generation=generation,
            certificate_pem=_pem_text(value["certificatePem"], "certificatePem"),
            chain_pem=_pem_text(value["chainPem"], "chainPem"),
            cn=_text(value["cn"], "cn"),
            spki_sha256=spki,
            serial=serial,
            not_before=not_before,
            not_after=not_after,
            renewable_until=renewable_until,
            trust_bundle_id=_text(value["trustBundleId"], "trustBundleId"),
            policy_issuer=_text(value["policyIssuer"], "policyIssuer"),
        )
    except TransportCredentialError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise _invalid("transport enrollment response schema is invalid") from error


def _parse_enrollment(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != _ENROLLMENT_KEYS:
        raise TransportCredentialError(
            "TRANSPORT_STAGING_CONFLICT", "transport enrollment staging record is invalid"
        )
    try:
        return {
            "operationId": _uuid4(value["operationId"], "operationId"),
            "spkiSha256": _hex64(value["spkiSha256"], "spkiSha256"),
            "owner": _text(value["owner"], "owner"),
            "deviceUid": _device_uid(value["deviceUid"]),
            "expectedCredentialGeneration": _generation(
                value["expectedCredentialGeneration"],
                "expectedCredentialGeneration",
                minimum=0,
            ),
        }
    except TransportCredentialError as error:
        raise TransportCredentialError(
            "TRANSPORT_STAGING_CONFLICT", "transport enrollment staging record is invalid"
        ) from error


def _validate_certificate_projection(
    credential: TransportCredential, *, expected_spki: str, now: datetime
) -> x509.Certificate:
    certificate = _one_certificate(credential.certificate_pem, "certificatePem")
    public_key = certificate_public_key(certificate)
    if _public_spki_sha256(public_key) != expected_spki:
        raise TransportCredentialError(
            "TRANSPORT_SPKI_MISMATCH",
            "transport certificate public key does not match the staged key",
        )
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size != 2048:
        raise _invalid("transport certificate does not use the required key profile")
    signature_hash = certificate_signature_hash(certificate)
    if (
        signature_hash is None
        or signature_hash.name != "sha256"
    ):
        raise _invalid("transport certificate signature profile is invalid")
    extensions = read_certificate_extensions(certificate)
    try:
        basic = extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value
        usage = extensions.get_extension_for_class(x509.KeyUsage).value
        extended = extensions.get_extension_for_class(
            x509.ExtendedKeyUsage
        ).value
    except x509.ExtensionNotFound as error:
        raise _invalid("transport certificate profile is incomplete") from error
    if basic.ca or not usage.digital_signature or not usage.key_encipherment:
        raise _invalid("transport certificate privileges are invalid")
    if not {
        ExtendedKeyUsageOID.CLIENT_AUTH,
        ExtendedKeyUsageOID.SERVER_AUTH,
    }.issubset(set(extended)):
        raise _invalid("transport certificate extended key usage is invalid")
    names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    cn_match = _CN.fullmatch(credential.cn)
    if len(names) != 1 or names[0].value != credential.cn:
        raise _invalid("transport certificate common name does not match the response")
    if (
        cn_match is None
        or cn_match.group(1) != credential.binding_id
        or int(cn_match.group(2)) != credential.credential_generation
    ):
        raise _invalid("transport certificate common name has an invalid binding shape")
    verify_credential_binding(certificate, credential)
    if (
        credential.serial != format(certificate.serial_number, "x")
        or credential.not_before != int(certificate.not_valid_before_utc.timestamp())
        or credential.not_after != int(certificate.not_valid_after_utc.timestamp())
        or credential.renewable_until < credential.not_after
    ):
        raise _invalid("transport certificate metadata does not match the response")
    if certificate.not_valid_before_utc > now:
        raise _invalid("transport certificate is not valid yet")
    if certificate.not_valid_after_utc <= now + _five_minutes():
        raise TransportCredentialError(
            "TRANSPORT_CERT_EXPIRING", "transport certificate expires too soon"
        )
    return certificate


def _verify_chain(
    leaf: x509.Certificate,
    chain: list[x509.Certificate],
    roots: list[x509.Certificate],
    now: datetime,
) -> None:
    root_fingerprints = {
        cert.fingerprint(hashes.SHA256()) for cert in roots if _is_ca(cert)
    }
    candidates = chain + roots
    current = leaf
    seen: set[bytes] = set()
    subordinate_ca_count = 0
    for _depth in range(len(candidates) + 1):
        fingerprint = current.fingerprint(hashes.SHA256())
        if fingerprint in seen:
            break
        seen.add(fingerprint)
        if fingerprint in root_fingerprints:
            try:
                _verify_signature(current, current)
            except (InvalidSignature, TypeError, ValueError):
                break
            return
        issuer = next(
            (
                candidate
                for candidate in candidates
                if candidate.subject == current.issuer
                and candidate.fingerprint(hashes.SHA256()) not in seen
            ),
            None,
        )
        if issuer is None:
            break
        issuer_extensions = read_certificate_extensions(issuer)
        try:
            constraints = issuer_extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value
        except x509.ExtensionNotFound:
            break
        try:
            issuer_usage = issuer_extensions.get_extension_for_class(
                x509.KeyUsage
            ).value
        except x509.ExtensionNotFound:
            issuer_usage = None
        if _is_ca(current) and current.subject != current.issuer:
            subordinate_ca_count += 1
        if (
            not constraints.ca
            or (issuer_usage is not None and not issuer_usage.key_cert_sign)
            or (
                constraints.path_length is not None
                and subordinate_ca_count > constraints.path_length
            )
            or issuer.not_valid_before_utc > now
            or issuer.not_valid_after_utc <= now
        ):
            break
        try:
            _verify_signature(current, issuer)
        except (InvalidSignature, TypeError, ValueError):
            break
        current = issuer
    raise TransportCredentialError(
        "TRANSPORT_CHAIN_UNTRUSTED",
        "transport certificate does not chain to the pinned trust bundle",
    )


def _is_ca(certificate: x509.Certificate) -> bool:
    extensions = read_certificate_extensions(certificate)
    try:
        return extensions.get_extension_for_class(
            x509.BasicConstraints
        ).value.ca
    except x509.ExtensionNotFound:
        return False


def _certificates(value: bytes | str, label: str) -> list[x509.Certificate]:
    try:
        encoded = value.encode("ascii") if isinstance(value, str) else value
        if not isinstance(encoded, bytes):
            raise TypeError
        certificates = x509.load_pem_x509_certificates(encoded)
    except (TypeError, ValueError, UnicodeError) as error:
        raise _invalid(f"{label} is not a valid certificate sequence") from error
    if not certificates:
        raise _invalid(f"{label} is not a valid certificate sequence")
    return certificates


def _one_certificate(value: str, label: str) -> x509.Certificate:
    certificates = _certificates(value, label)
    if len(certificates) != 1:
        raise _invalid(f"{label} must contain exactly one certificate")
    return certificates[0]


def _load_private_key(path: Path) -> rsa.RSAPrivateKey:
    try:
        key = load_private_key_bytes(_read_safe_file(path, 0o600))
    except (OSError, TypeError, ValueError) as error:
        raise TransportCredentialError(
            "TRANSPORT_STAGING_CONFLICT", "transport staging key is unreadable"
        ) from error
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 2048:
        raise TransportCredentialError(
            "TRANSPORT_STAGING_CONFLICT", "transport staging key profile is invalid"
        )
    return key


def _public_spki_sha256(public_key: Any) -> str:
    encoded = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(encoded).hexdigest()


def _bundle_binding_sha256(bundle_id: str, content_sha256: str) -> str:
    return hashlib.sha256(f"{bundle_id}\0{content_sha256}".encode()).hexdigest()


def _commit_checkpoint(_step: str) -> None:
    pass


def _require_local_device(home: Path, device_uid: str, owner: str) -> None:
    try:
        record = read_device_record(
            home,
            file_reader=lambda path: _read_local_device_file(home, path),
        )
    except (OSError, ValueError) as error:
        raise TransportCredentialError(
            "TRANSPORT_DEVICE_MISMATCH", "local device identity is invalid"
        ) from error
    if record is None or record.device_uid != device_uid:
        raise TransportCredentialError(
            "TRANSPORT_DEVICE_MISMATCH",
            "transport credential device does not match the local device",
        )
    if record.owner != owner:
        raise TransportCredentialError(
            "TRANSPORT_OWNER_MISMATCH",
            "transport credential owner does not match the local device",
        )


def _read_local_device_file(home: Path, path: Path) -> bytes | None:
    return _read_safe_optional_file_beneath(path, root=home, mode=0o600)


def _uuid4(value: object, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = UUID(text)
    except ValueError as error:
        raise _invalid(f"{label} must be a canonical UUID4") from error
    if parsed.version != 4 or str(parsed) != text:
        raise _invalid(f"{label} must be a canonical UUID4")
    return text


def _device_uid(value: object) -> str:
    text = _text(value, "deviceUid")
    if _DEVICE_UID.fullmatch(text) is None:
        raise _invalid("deviceUid must be 32 lowercase hexadecimal characters")
    try:
        parsed = UUID(hex=text)
    except ValueError as error:
        raise _invalid("deviceUid must be a UUID4 hexadecimal value") from error
    if parsed.version != 4 or parsed.hex != text:
        raise _invalid("deviceUid must be a UUID4 hexadecimal value")
    return text


def _generation(value: object, label: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _invalid(f"{label} must be an integer >= {minimum}")
    return value


def _timestamp(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(f"{label} must be a non-negative integer")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _invalid(f"{label} must be a non-empty string without surrounding whitespace")
    return value


def _pem_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid(f"{label} must be a non-empty string")
    try:
        value.encode("ascii")
    except UnicodeError as error:
        raise _invalid(f"{label} must be ASCII PEM") from error
    return value


def _hex64(value: object, label: str) -> str:
    text = _text(value, label)
    if _HEX_64.fullmatch(text) is None:
        raise _invalid(f"{label} must be 64 lowercase hexadecimal characters")
    return text


def _lower_hex(value: object, label: str) -> str:
    text = _text(value, label)
    if re.fullmatch(r"[0-9a-f]+", text) is None:
        raise _invalid(f"{label} must be lowercase hexadecimal")
    return text


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise _invalid("validation time must include a timezone")
    return value.astimezone(timezone.utc)


def _five_minutes():
    from datetime import timedelta

    return timedelta(minutes=5)


def _invalid(message: str) -> TransportCredentialError:
    return TransportCredentialError("TRANSPORT_RESPONSE_INVALID", message)


__all__ = [
    "StagedTransportCredential",
    "TransportCredential",
    "TransportCredentialError",
    "commit_transport_credential",
    "read_transport_credential",
    "retire_transport_credential",
    "stage_transport_credential",
    "transport_credential_status",
    "validate_enrollment_response",
]
