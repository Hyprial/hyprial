"""Independent reference helpers for the signed binding profile witnesses.

Nothing here imports the product canonicalizer: the expected digest is produced
by the standard library JSON encoder, and the frozen vectors pin that reference
byte for byte.  ``test_jcs_vectors`` asserts both encoders agree.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from hyprial.identity.interface import DeviceRecord, stage_transport_credential, write_device_record

PROFILE = "hyprial.transport.binding.v1"
BINDING_URI_NAMESPACE = "https://auth.hyprial.com/transport/binding/"
BINDING_URI_PREFIX = BINDING_URI_NAMESPACE + "v1/sha256/"
POLICY_ISSUER = "https://authority.example.test"
TRUST_BUNDLE_ID = "sha256:" + "b" * 64
OWNER = "alice@example.invalid"
OWNER_USER_ID = "u.alice"
MAX_SAFE_INTEGER = 9007199254740991
CLAIM_FIELDS = (
    "profile",
    "policyIssuer",
    "bindingId",
    "ownerUserId",
    "owner",
    "deviceUid",
    "credentialGeneration",
    "spkiSha256",
    "renewableUntil",
    "trustBundleId",
)
FROZEN_VECTORS_SHA256 = (
    "7ea026cb44399d24ef5685a3d195a43e8c8cf62faa4b806387b372a279da1240"
)
FIXTURES = Path(__file__).parent / "fixtures"


def frozen_vectors() -> dict[str, object]:
    return json.loads((FIXTURES / "issuer-binding-profile-v3-vectors.json").read_text())


def reference_canonical_bytes(claims: dict[str, object]) -> bytes:
    """Stdlib reference encoder for the restricted string/integer profile."""

    return json.dumps(
        claims,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def reference_digest(claims: dict[str, object]) -> str:
    return hashlib.sha256(reference_canonical_bytes(claims)).hexdigest()


def reference_uri(claims: dict[str, object]) -> str:
    return BINDING_URI_PREFIX + reference_digest(claims)


def horizon(days: int = 8) -> int:
    return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())


def claims_for(
    *,
    binding_id: str,
    device_uid: str,
    credential_generation: int,
    spki_sha256: str,
    renewable_until: int,
    owner: str = OWNER,
    owner_user_id: str = OWNER_USER_ID,
    policy_issuer: str = POLICY_ISSUER,
    trust_bundle_id: str = TRUST_BUNDLE_ID,
) -> dict[str, object]:
    return {
        "profile": PROFILE,
        "policyIssuer": policy_issuer,
        "bindingId": binding_id,
        "ownerUserId": owner_user_id,
        "owner": owner,
        "deviceUid": device_uid,
        "credentialGeneration": credential_generation,
        "spkiSha256": spki_sha256,
        "renewableUntil": renewable_until,
        "trustBundleId": trust_bundle_id,
    }


def device_record(device_uid: str, *, owner: str = OWNER) -> DeviceRecord:
    return DeviceRecord(
        device_id="node-test",
        owner=owner,
        server_public="nodekey:server",
        client_public="nodekey:client",
        device_uid=device_uid,
        name="test-device",
    )


def stage(home: Path, *, owner: str = OWNER, device_uid: str | None = None):
    uid = device_uid or uuid4().hex
    if not (home / "state/tailcat/device.json").exists():
        write_device_record(home, device_record(uid, owner=owner))
    return stage_transport_credential(
        home,
        operation_id=str(uuid4()),
        owner=owner,
        device_uid=uid,
        expected_credential_generation=0,
    )


@dataclass(frozen=True, slots=True)
class SyntheticCa:
    key: rsa.RSAPrivateKey
    certificate: x509.Certificate

    @property
    def pem(self) -> bytes:
        return self.certificate.public_bytes(serialization.Encoding.PEM)


def make_ca(common_name: str = "test transport root") -> SyntheticCa:
    now = datetime.now(timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return SyntheticCa(key=key, certificate=certificate)


def issue_leaf(
    ca: SyntheticCa,
    public_key: rsa.RSAPublicKey,
    *,
    common_name: str,
    uri_sans: tuple[str, ...] = (),
    dns_sans: tuple[str, ...] = (),
    ip_addresses: tuple[str, ...] = (),
    not_before: datetime | None = None,
    not_after: datetime | None = None,
) -> x509.Certificate:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = not_before or (now - timedelta(minutes=1))
    end = not_after or (now + timedelta(hours=1))
    general_names: list[x509.GeneralName] = [
        x509.UniformResourceIdentifier(value) for value in uri_sans
    ]
    general_names += [x509.DNSName(value) for value in dns_sans]
    general_names += [x509.IPAddress(ipaddress.ip_address(value)) for value in ip_addresses]
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca.certificate.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
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
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]
            ),
            critical=False,
        )
    )
    if general_names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(general_names), critical=False
        )
    return builder.sign(ca.key, hashes.SHA256())


def binding_response(
    staged,
    *,
    claims: dict[str, object] | None = None,
    uri_sans: tuple[str, ...] | None = None,
    common_name: str | None = None,
    ca: SyntheticCa | None = None,
) -> tuple[dict[str, object], bytes]:
    """Build one exact Interface A response whose leaf carries the binding URI."""

    authority_ca = ca or make_ca()
    csr = x509.load_pem_x509_csr(staged.csr_pem.encode("ascii"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    not_after = now + timedelta(hours=1)
    effective_claims = claims or claims_for(
        binding_id=str(uuid4()),
        device_uid=staged.device_uid,
        credential_generation=1,
        spki_sha256=staged.spki_sha256,
        renewable_until=int((not_after + timedelta(days=7)).timestamp()),
    )
    cn = common_name or (
        f"hdev-{effective_claims['bindingId']}-g{effective_claims['credentialGeneration']}"
    )
    certificate = issue_leaf(
        authority_ca,
        csr.public_key(),
        common_name=cn,
        uri_sans=uri_sans
        if uri_sans is not None
        else (reference_uri(effective_claims),),
        ip_addresses=("127.0.0.1",),
        not_after=not_after,
    )
    response: dict[str, object] = {
        "ownerUserId": effective_claims["ownerUserId"],
        "owner": effective_claims["owner"],
        "bindingId": effective_claims["bindingId"],
        "deviceUid": effective_claims["deviceUid"],
        "credentialGeneration": effective_claims["credentialGeneration"],
        "certificatePem": certificate.public_bytes(serialization.Encoding.PEM).decode(
            "ascii"
        ),
        "chainPem": authority_ca.certificate.public_bytes(
            serialization.Encoding.PEM
        ).decode("ascii"),
        "cn": cn,
        "spkiSha256": effective_claims["spkiSha256"],
        "serial": format(certificate.serial_number, "x"),
        "notBefore": int(certificate.not_valid_before_utc.timestamp()),
        "notAfter": int(certificate.not_valid_after_utc.timestamp()),
        "renewableUntil": effective_claims["renewableUntil"],
        "trustBundleId": effective_claims["trustBundleId"],
        "policyIssuer": effective_claims["policyIssuer"],
    }
    return response, authority_ca.pem


def inventory(home: Path) -> tuple[tuple[object, ...], ...]:
    """Symlink-safe recursive client inventory without following or printing data."""

    records: list[tuple[object, ...]] = []
    for relative in (
        "secrets/transport",
        "state/transport",
    ):
        root = home / relative
        if not os.path.lexists(root):
            records.append((relative, "absent"))
            continue
        for directory, directories, files in os.walk(root, followlinks=False):
            for name in sorted(directories) + sorted(files):
                path = Path(directory) / name
                label = str(path.relative_to(home))
                metadata = os.lstat(path)
                if stat.S_ISLNK(metadata.st_mode):
                    target = os.readlink(path)
                    records.append(
                        (label, "symlink", hashlib.sha256(target.encode()).hexdigest())
                    )
                elif stat.S_ISREG(metadata.st_mode):
                    records.append(
                        (
                            label,
                            "file",
                            stat.S_IMODE(metadata.st_mode),
                            metadata.st_size,
                            hashlib.sha256(path.read_bytes() if metadata.st_size else b"").hexdigest()
                            if metadata.st_size
                            else hashlib.sha256(b"").hexdigest(),
                        )
                    )
                elif stat.S_ISDIR(metadata.st_mode):
                    records.append((label, "directory", stat.S_IMODE(metadata.st_mode)))
                else:
                    records.append((label, "other", stat.S_IFMT(metadata.st_mode)))
    return tuple(sorted(records))
