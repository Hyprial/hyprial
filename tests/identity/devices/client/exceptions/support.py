"""Private-marker injection with boolean-only public error observations."""

import secrets
import ssl
from types import SimpleNamespace

from hyprial.identity.impl.device.client import http
from hyprial.identity.interface import (
    ClientTlsMaterial,
    DeviceAuthorityConfig,
    TransportClientError,
)
from tests.identity.devices.client import support as wire

OPERATION = "11111111-1111-4111-8111-111111111111"
NEXT_OPERATION = "22222222-2222-4222-8222-222222222222"
BINDING = "33333333-3333-4333-8333-333333333333"
DEVICE = "44444444444444448444444444444444"


def marker(tmp_path):
    value = secrets.token_hex(24)
    path = tmp_path / "private-marker"
    path.write_text(value)
    path.chmod(0o600)
    return value


def capture(call, private):
    """Never return an exception object or private string to pytest assertions."""
    try:
        call()
    except TransportClientError as error:
        chain = [error.__context__, error.__cause__]
        seen = set()
        reachable = private in str(error) or private in repr(error.__dict__)
        while chain:
            item = chain.pop()
            if item is None or id(item) in seen:
                continue
            seen.add(id(item))
            reachable = reachable or private in str(item) or private in repr(item)
            chain.extend((item.__context__, item.__cause__))
        return {
            "code": error.code,
            "outcome": error.outcome,
            "retryable": error.retryable,
            "http_status": error.http_status,
            "operation_id": error.operation_id,
            "reason_code": error.reason_code,
            "exception_class": error.exception_class,
            "context_absent": error.__context__ is None,
            "cause_absent": error.__cause__ is None,
            "private_reachable": reachable,
            "repr_clean": private not in repr(error),
        }
    except BaseException as error:
        return {"unexpected_exception_class": type(error).__name__}
    return {"unexpected_success": True}


def expected(code, outcome="NOT_SENT", retryable=False, operation_id=None,
             *, reason_code=None, exception_class=None):
    return {
        "code": code, "outcome": outcome, "retryable": retryable,
        "http_status": None, "operation_id": operation_id,
        "reason_code": reason_code, "exception_class": exception_class,
        "context_absent": True, "cause_absent": True,
        "private_reachable": False, "repr_clean": True,
    }


def config(**overrides):
    values = dict(
        origin="https://authority.test", server_name="authority.test",
        trust_bundle_pem=b"synthetic", connect_timeout=1, handshake_timeout=1,
        read_timeout=1, write_timeout=1,
    )
    values.update(overrides)
    return DeviceAuthorityConfig(**values)


def retire(client, operation_id=OPERATION):
    return client.retire(operation_id=operation_id, binding_id=BINDING,
                         expected_credential_generation=1)


def rejection():
    return wire.raw_response(wire.error_envelope(OPERATION, "CSR_INVALID", False),
                             status="400 Bad Request")


class Stream:
    def __init__(self, faults, private, kind):
        self.faults, self.private, self.kind = faults, private, kind
        self.sent = 0
        self.closed = False
        self.chunks = [rejection(), b""]

    def settimeout(self, _timeout):
        site = "raw-timeout" if self.kind == "raw" else (
            "read-timeout" if self.sent else "write-timeout"
        )
        self.faults.raise_at(site)

    def sendall(self, _request):
        self.faults.raise_at("send")
        self.sent += 1

    def recv(self, _size):
        self.faults.raise_at("recv")
        return self.chunks.pop(0)

    def close(self):
        self.closed = True
        self.faults.raise_at("close")


class Faults:
    def __init__(self, site, private, failure=OSError):
        self.site, self.private, self.failure = site, private, failure
        self.raw = Stream(self, private, "raw")
        self.tls = Stream(self, private, "tls")

    def raise_at(self, site):
        if site == self.site:
            raise self.failure(self.private)

    def connect(self, *_args, **_kwargs):
        self.raise_at("connect")
        return self.raw

    def context(self, *_args, **_kwargs):
        return SimpleNamespace(
            options=0,
            set_alpn_protocols=lambda _protocols: None,
            load_verify_locations=self.trust,
            load_cert_chain=self.chain,
            wrap_socket=self.handshake,
        )

    def trust(self, **_kwargs):
        self.raise_at("trust")

    def chain(self, **_kwargs):
        self.raise_at("chain")

    def handshake(self, *_args, **_kwargs):
        self.raise_at("handshake")
        return self.tls


class FaultModule:
    def __init__(self, wrapped, name, faults):
        self.wrapped, self.name, self.faults = wrapped, name, faults

    def __getattr__(self, name):
        if name == self.name:
            return lambda *_args, **_kwargs: self.faults.raise_at("material")
        return getattr(self.wrapped, name)


def inject(monkeypatch, site, private, failure=OSError):
    faults = Faults(site, private, failure)
    monkeypatch.setattr(http.ssl, "SSLContext", faults.context)
    monkeypatch.setattr(http.socket, "create_connection", faults.connect)
    return faults


def material_config():
    return config(client_tls=ClientTlsMaterial(b"chain", b"key", b"bundle"))


HTTP_FAILURES = [
    ("connect", OSError, "AUTHORITY_UNAVAILABLE", "NOT_SENT", True, None),
    ("raw-timeout", OSError, "AUTHORITY_UNAVAILABLE", "NOT_SENT", True, None),
    ("handshake", ssl.SSLCertVerificationError, "TRANSPORT_CHAIN_UNTRUSTED", "NOT_SENT", False, OPERATION),
    ("handshake", OSError, "AUTHORITY_UNAVAILABLE", "NOT_SENT", True, None),
    ("handshake", ValueError, "AUTHORITY_UNAVAILABLE", "NOT_SENT", True, None),
    ("write-timeout", OSError, "TRANSPORT_RESPONSE_UNKNOWN", "UNKNOWN", True, OPERATION),
    ("send", OSError, "TRANSPORT_RESPONSE_UNKNOWN", "UNKNOWN", True, OPERATION),
    ("recv", OSError, "TRANSPORT_RESPONSE_UNKNOWN", "UNKNOWN", True, OPERATION),
    ("trust", ssl.SSLError, "TRANSPORT_CLIENT_CONFIG_INVALID", "NOT_SENT", False, None),
    ("trust", ValueError, "TRANSPORT_CLIENT_CONFIG_INVALID", "NOT_SENT", False, None),
    ("chain", ssl.SSLError, "TRANSPORT_CLIENT_CONFIG_INVALID", "NOT_SENT", False, None),
    ("chain", TypeError, "TRANSPORT_CLIENT_CONFIG_INVALID", "NOT_SENT", False, None),
    ("chain", UnicodeError, "TRANSPORT_CLIENT_CONFIG_INVALID", "NOT_SENT", False, None),
    ("chain", ValueError, "TRANSPORT_CLIENT_CONFIG_INVALID", "NOT_SENT", False, None),
]
