"""Per-tailnet local CA material. Never replace an uncertain existing CA."""

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import base64
import hashlib

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ..runtime.schema import decode, descriptor_record, encode, exact, fail, parse_descriptor, sha
from ..runtime.ports import Descriptor, DialTarget

CA_DAYS = 365
SERVER_DAYS = 30
LEAF_DAYS = 1
RENEWABLE_DAYS = 30


def _checkpoint(_name):
    """Durable CA setup boundaries for deterministic interruption tests."""


def _private(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _public(key):
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _present(slot, path):
    try:
        with slot.directory(path[:-1]) as directory:
            return slot._existing_target(directory, path[-1])
    except FileNotFoundError:
        return False


def ensure(slot, pin, port):
    """Called only under the owning slot lock after full local authority verification."""
    target = DialTarget(pin, port)
    state = (*slot.state, "authority")
    secrets = (*slot.secrets, "authority")
    paths = {"caKey": (*secrets, "ca-key.pem"), "serverKey": (*secrets, "server-key.pem"),
             "ca": (*state, "ca.pem"), "server": (*state, "server.pem")}
    manifest_path = (*state, "material.json")
    raw = slot.read(manifest_path)
    if raw is None:
        # A partial prior initialization is evidence, never permission to create a new CA.
        evidence = (*paths.values(), (*slot.state, "descriptor.json"), (*slot.state, "active.json"),
                    (*state, "store.json"), (*state, "store.sqlite3"),
                    (*state, "store.sqlite3-journal"), (*state, "store.sqlite3-wal"), (*state, "store.sqlite3-shm"))
        if any(_present(slot, path) for path in evidence):
            fail("UNSAFE_AUTHORITY_STATE", "authority")
        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hyprial tailnet credential CA")])
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
              .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=CA_DAYS))
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
              .sign(ca_key, hashes.SHA256()))
        label = base64.b32encode(hashlib.sha256(slot.tailnet_id.encode()).digest()).decode().lower().rstrip("=")
        server_name = "t-" + label + ".credential.invalid"
        server = (x509.CertificateBuilder()
                  .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hyprial tailnet credential server")]))
                  .issuer_name(ca.subject).public_key(server_key.public_key())
                  .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
                  .not_valid_after(now + timedelta(days=SERVER_DAYS))
                  .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                  .add_extension(x509.SubjectAlternativeName([x509.DNSName(server_name)]), critical=False)
                  .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                  .sign(ca_key, hashes.SHA256()))
        material = {"caKey": _private(ca_key), "serverKey": _private(server_key),
                    "ca": ca.public_bytes(serialization.Encoding.PEM),
                    "server": server.public_bytes(serialization.Encoding.PEM)}
        descriptor = Descriptor("hyprial.tailnet.credential-authority.v1", slot.tailnet_id, target,
                                f"https://{server_name}:{port}", server_name, material["ca"],
                                sha(ca.public_bytes(serialization.Encoding.DER)))
        for key, path in paths.items():
            slot.write(path, material[key], exclusive=True)
            _checkpoint(key + "_DURABLE")
        manifest = {"version": "hyprial.tailnet.credential-authority-material.v1", "home": slot.identity,
                    "tailnetId": slot.tailnet_id, "pin": asdict(pin),
                    "descriptor": descriptor_record(descriptor),
                    "hashes": {key: sha(value) for key, value in material.items()}}
        slot.write(manifest_path, encode(manifest), exclusive=True)
        _checkpoint("MATERIAL_DURABLE")
        raw = encode(manifest)
    try:
        manifest = exact(decode(raw), ("version", "home", "tailnetId", "pin", "descriptor", "hashes"))
        if (manifest["version"] != "hyprial.tailnet.credential-authority-material.v1"
                or manifest["home"] != slot.identity or manifest["tailnetId"] != slot.tailnet_id
                or manifest["pin"] != asdict(pin) or set(manifest["hashes"]) != set(paths)):
            raise ValueError("authority manifest mismatch")
        material = {key: slot.read(path) for key, path in paths.items()}
        if any(value is None or sha(value) != manifest["hashes"][key] for key, value in material.items()):
            raise ValueError("authority material mismatch")
        descriptor = parse_descriptor(manifest["descriptor"])
        if descriptor.target != target or descriptor.ca_certificate_pem != material["ca"]:
            raise ValueError("authority descriptor mismatch")
        ca_key = serialization.load_pem_private_key(material["caKey"], password=None)
        ca = x509.load_pem_x509_certificate(material["ca"])
        server_key = serialization.load_pem_private_key(material["serverKey"], password=None)
        server = x509.load_pem_x509_certificate(material["server"])
        if (not isinstance(ca_key, rsa.RSAPrivateKey) or ca_key.key_size != 2048
                or not isinstance(server_key, rsa.RSAPrivateKey) or server_key.key_size != 2048
                or _public(ca_key.public_key()) != _public(ca.public_key())
                or _public(server_key.public_key()) != _public(server.public_key())
                or not ca.not_valid_before_utc <= datetime.now(timezone.utc) < server.not_valid_after_utc
                or not ca.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign
                or server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName) != [descriptor.server_name]):
            raise ValueError("authority key profile mismatch")
        ca.public_key().verify(ca.signature, ca.tbs_certificate_bytes, padding.PKCS1v15(), hashes.SHA256())
        ca.public_key().verify(server.signature, server.tbs_certificate_bytes, padding.PKCS1v15(), hashes.SHA256())
        return descriptor, ca_key, ca
    except (ValueError, TypeError, KeyError, UnicodeError, InvalidSignature, UnsupportedAlgorithm,
            x509.ExtensionNotFound, x509.DuplicateExtension):
        fail("UNSAFE_AUTHORITY_STATE", "authority")
