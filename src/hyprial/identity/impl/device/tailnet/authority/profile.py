"""Tailnet-only association and actual RSA2048 credential issuance."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ...binding.jcs import canonical_bytes
from ..runtime.schema import TRANSPORT_VERSION, decode, exact, operation_id, sha
from .issuer import LEAF_DAYS, RENEWABLE_DAYS

ROUTE = "/v1/tailnet-credentials/enroll"
NAMESPACE = "https://auth.hyprial.com/transport/tailnet-association/v1/sha256/"


def request(route, raw, tailnet_id):
    if route != ROUTE or len(raw) > 65536:
        raise ValueError("invalid enrollment route/size")
    value = exact(decode(raw), ("version", "tailnetId", "operationId", "deviceUid",
                                "expectedCredentialGeneration", "csrPem"))
    if (value["version"] != TRANSPORT_VERSION or value["tailnetId"] != tailnet_id
            or type(value["expectedCredentialGeneration"]) is not int
            or not 0 <= value["expectedCredentialGeneration"] <= 2**31 - 1 or type(value["csrPem"]) is not str
            or type(value["deviceUid"]) is not str):
        raise ValueError("invalid enrollment request")
    operation_id(value["operationId"])
    uid = UUID(hex=value["deviceUid"])
    if uid.version != 4 or uid.hex != value["deviceUid"]:
        raise ValueError("invalid device UID")
    return value


def csr_key(pem):
    csr = x509.load_pem_x509_csr(pem.encode("ascii"))
    key = csr.public_key()
    if (not isinstance(key, rsa.RSAPublicKey) or key.key_size != 2048
            or key.public_numbers().e != 65537 or len(csr.subject) or len(csr.extensions)
            or not isinstance(csr.signature_hash_algorithm, hashes.SHA256) or not csr.is_signature_valid):
        raise ValueError("invalid CSR profile")
    return key


def issue(request, network, descriptor, ca_key, ca):
    try:
        public = csr_key(request["csrPem"])
    except (ValueError, TypeError, UnicodeError, UnsupportedAlgorithm, x509.DuplicateExtension):
        return None
    now = datetime.now(timezone.utc)
    not_after = min(now + timedelta(days=LEAF_DAYS), ca.not_valid_after_utc)
    renewable = min(now + timedelta(days=RENEWABLE_DAYS), ca.not_valid_after_utc)
    if not_after <= now + timedelta(minutes=5):
        raise ValueError("authority CA validity is insufficient")
    binding_id = str(uuid4())
    spki = sha(public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
    association = {"version": "hyprial.tailnet.credential-association.v1",
                   "tailnetId": descriptor.tailnet_id, "authorityDeviceId": descriptor.target.authority.device_id,
                   "caSha256": descriptor.ca_sha256, "deviceId": network["deviceId"], "owner": network["owner"],
                   "memberMasterPublic": network["masterPublic"], "memberServerPublic": network["serverPublic"],
                   "deviceUid": request["deviceUid"], "deviceUidProvenance": "member-self-reported",
                   "bindingId": binding_id, "credentialGeneration": 1, "spkiSha256": spki,
                   "renewableUntil": int(renewable.timestamp())}
    certificate = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, request["deviceUid"])]))
                   .issuer_name(ca.subject).public_key(public).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=5)).not_valid_after(not_after)
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
                   .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(
                       NAMESPACE + sha(canonical_bytes(association)))]), critical=False)
                   .sign(ca_key, hashes.SHA256()))
    credential = {"version": "hyprial.tailnet.credential.v1", "bindingId": binding_id,
                  "credentialGeneration": 1, "certificatePem": certificate.public_bytes(serialization.Encoding.PEM).decode(),
                  "chainPem": descriptor.ca_certificate_pem.decode(), "spkiSha256": spki,
                  "serial": format(certificate.serial_number, "x"),
                  "notBefore": int(certificate.not_valid_before_utc.timestamp()),
                  "notAfter": int(certificate.not_valid_after_utc.timestamp()),
                  "renewableUntil": association["renewableUntil"]}
    return {"credential": credential, "association": association}
