"""Strict codecs for the new protocol; no v1 field reinterpretation."""

from dataclasses import asdict
from functools import wraps
import hashlib
import json
from uuid import UUID

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization

from ..ports import TailnetAuthorityPin, TailnetCredentialError
from .ports import Descriptor, DialTarget, text

DESCRIPTOR_VERSION = "hyprial.tailnet.credential-authority.v1"
BOOTSTRAP_VERSION = "hyprial.tailnet.credential-bootstrap.v1"
TRANSPORT_VERSION = "hyprial.tailnet.transport.v1"


def fail(code, phase="prepare"):
    raise TailnetCredentialError(code, phase=phase)


def public_gate(function):
    """Detach private exception chains at the public runtime boundary."""
    @wraps(function)
    def call(*args, **kwargs):
        failure = None
        try:
            return function(*args, **kwargs)
        except TailnetCredentialError as error:
            failure = (error.code, error.phase, error.remote_outcome)
        except OSError:
            failure = ("TAILNET_STORAGE_UNSAFE", "storage", "NOT_SENT")
        code, phase, outcome = failure
        raise TailnetCredentialError(code, phase=phase, remote_outcome=outcome)
    return call


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate credential field")
        result[key] = value
    return result


def decode(raw):
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                      parse_constant=lambda _: _invalid())


def _invalid():
    raise ValueError("invalid credential JSON")


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError("invalid credential fields")
    return value


def operation_id(value):
    text(value)
    parsed = UUID(value)
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("invalid operation ID")
    return value


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def descriptor_record(descriptor):
    pin = descriptor.target.authority
    return {"version": descriptor.version, "tailnetId": descriptor.tailnet_id,
            "authority": {"owner": pin.owner, "deviceId": pin.device_id,
                          "masterPublic": pin.master_public, "serverPublic": pin.server_public,
                          "address": pin.address, "controlPort": pin.control_port},
            "origin": descriptor.origin, "serverName": descriptor.server_name,
            "remotePort": descriptor.target.remote_port,
            "caCertificatePem": descriptor.ca_certificate_pem.decode("ascii"),
            "caSha256": descriptor.ca_sha256}


def parse_descriptor(value):
    try:
        return _parse_descriptor(value)
    except (x509.ExtensionNotFound, x509.DuplicateExtension, UnsupportedAlgorithm):
        raise ValueError("invalid descriptor certificate") from None


def _parse_descriptor(value):
    exact(value, ("version", "tailnetId", "authority", "origin", "serverName",
                  "remotePort", "caCertificatePem", "caSha256"))
    a = exact(value["authority"], ("owner", "deviceId", "masterPublic", "serverPublic",
                                    "address", "controlPort"))
    pin = TailnetAuthorityPin(value["tailnetId"], a["owner"], a["deviceId"],
                             a["masterPublic"], a["serverPublic"], a["address"], a["controlPort"])
    if type(value["caCertificatePem"]) is not str:
        raise ValueError("invalid CA PEM type")
    pem = value["caCertificatePem"].encode("ascii")
    # Accept precisely one PEM certificate, no alternate certificate or suffix.
    certificates = x509.load_pem_x509_certificates(pem)
    if len(certificates) != 1 or certificates[0].public_bytes(serialization.Encoding.PEM).strip() != pem.strip():
        raise ValueError("invalid CA certificate count")
    cert = certificates[0]
    if not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("descriptor certificate is not a CA")
    if sha(cert.public_bytes(serialization.Encoding.DER)) != value["caSha256"]:
        raise ValueError("descriptor CA digest mismatch")
    return Descriptor(value["version"], value["tailnetId"], DialTarget(pin, value["remotePort"]),
                      value["origin"], value["serverName"], pem, value["caSha256"])


def device_record(record):
    return {"owner": record.owner, "deviceId": record.device_id,
            "masterPublic": record.client_public, "serverPublic": record.server_public,
            "deviceUid": record.device_uid}


def transport_record(frozen):
    return {"descriptor": descriptor_record(frozen.descriptor),
            "mtls": asdict(frozen.mtls) if frozen.mtls else None}


def receipt(value, status, body):
    result = decode(body)
    if type(result) is not dict or type(status) is not int or type(result.get("ok")) is not bool:
        raise ValueError("invalid authority response")
    exact(result, ("version", "ok", "operationId", "receipt", "data" if result["ok"] else "error"))
    if result["version"] != TRANSPORT_VERSION or result["operationId"] != value["operationId"]:
        raise ValueError("invalid response identity")
    if result["ok"]:
        if status != 200:
            raise ValueError("invalid success status")
        exact(result["data"], ("credential", "association"))
    else:
        error = exact(result["error"], ("code", "retryable"))
        text(error["code"])
        if type(error["retryable"]) is not bool or not 400 <= status <= 599:
            raise ValueError("invalid rejection")
    if result["receipt"] is None or status == 503:
        return "UNKNOWN"
    proof = exact(result["receipt"], ("route", "requestSha256"))
    if proof != {"route": value["route"], "requestSha256": value["requestSha256"]}:
        raise ValueError("authority receipt mismatch")
    return "SUCCESS" if result["ok"] else "REJECTED"
