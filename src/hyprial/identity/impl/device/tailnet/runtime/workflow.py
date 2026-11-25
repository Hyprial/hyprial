"""Durable enroll preparation only; no mutation or successful issue is implied."""

import base64
import time
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from ...store import read_device_record
from ..ports import TailnetCredentialError, TailnetMembership
from .journal import Journal, VERSION, observe
from .ports import FrozenTransport, MemberProjection, integer
from .schema import (
    TRANSPORT_VERSION, decode, device_record, encode, exact, fail,
    operation_id as check_operation_id, parse_descriptor, public_gate, receipt, sha, transport_record,
)


def _checkpoint(_name):
    """Crash boundary for deterministic local-storage tests."""


class Runtime:
    def __init__(self):
        raise TypeError("use TailnetCredentials.runtime")

    @classmethod
    def _create(cls, facade, slot):
        value = object.__new__(cls)
        value._facade = facade
        value._slot = slot
        value._journal = Journal(slot)
        return value

    def _local_record(self):
        try:
            record = read_device_record(
                self._slot.home,
                file_reader=lambda path: self._slot.read(path.relative_to(self._slot.home).parts),
            )
        except (ValueError, OSError):
            fail("TAILNET_LOCAL_DEVICE_INVALID")
        if record is None or record.device_uid is None:
            fail("TAILNET_LOCAL_DEVICE_INVALID")
        return record

    def _fresh(self):
        record = self._local_record()
        source = self._facade._membership
        started = time.monotonic()
        observation = source.membership(self._slot.tailnet_id)
        rows = source.members(self._slot.tailnet_id)
        if (type(observation) is not TailnetMembership or type(rows) is not tuple
                or any(type(row) is not MemberProjection for row in rows)):
            fail("TAILNET_MEMBERSHIP_INVALID")
        p = observation.authority_pin
        if (observation.tailnet_id != self._slot.tailnet_id or p.tailnet_id != self._slot.tailnet_id
                or observation.member_ready is not True or observation.code is not None
                or observation.authority_device_id != p.device_id):
            fail("TAILNET_MEMBERSHIP_UNAVAILABLE")
        locals_ = [row for row in rows if row.device_id == record.device_id]
        authorities = [row for row in rows if row.device_id == p.device_id]
        if len(locals_) != 1 or len(authorities) != 1:
            fail("TAILNET_MEMBER_MISMATCH")
        local, authority = locals_[0], authorities[0]
        if (local.state != "active" or authority.state != "active"
                or (local.owner, local.master_public, local.server_public)
                != (record.owner, record.client_public, record.server_public)
                or (authority.owner, authority.master_public, authority.server_public, authority.address)
                != (p.owner, p.master_public, p.server_public, p.address)
                or observation.local_member is None
                or (observation.local_member.owner, observation.local_member.device_id, observation.local_member.state)
                != (local.owner, local.device_id, "active")):
            fail("TAILNET_MEMBER_MISMATCH")
        if observation.role == "authority":
            if local != authority or observation.lease_remaining_seconds is not None:
                fail("TAILNET_AUTHORITY_MISMATCH")
        elif observation.role == "member":
            remaining = observation.lease_remaining_seconds
            if (type(remaining) is not int or not 0 < remaining <= 300
                    or remaining - (time.monotonic() - started) <= 0):
                fail("TAILNET_MEMBERSHIP_UNAVAILABLE")
        else:
            fail("TAILNET_MEMBERSHIP_INVALID")
        integer(observation.revision)
        return record, observation

    def _active(self):
        raw = self._slot.read((*self._slot.state, "active.json"))
        if raw is None:
            return None
        try:
            value = exact(decode(raw), ("version", "tailnetId", "generation", "state"))
            if (type(value["version"]) is not str or value["version"] != "hyprial.tailnet.credential-slot.v1"
                    or value["tailnetId"] != self._slot.tailnet_id
                    or value["state"] not in ("active", "retired")):
                raise ValueError("invalid active pointer")
            integer(value["generation"])
            return value
        except (ValueError, TypeError, UnicodeError, RecursionError):
            fail("TAILNET_SLOT_INVALID", "read")

    @public_gate
    def read(self):
        if self._active() is None:
            return None
        # No generation has been produced by this preparation-only increment.
        # Preserve any existing state rather than guessing its interpretation.
        fail("TAILNET_RUNTIME_NOT_READY", "read")

    @public_gate
    def status(self, operation_id=None):
        with self._slot.locked():
            if operation_id is not None:
                value = self._journal.load(operation_id)
                if value is None:
                    fail("TAILNET_OPERATION_UNKNOWN", "status")
                self._verify(value)
                return observe(value)
            pending = self._journal.pending()
            for value in pending:
                self._verify(value)
            return {"tailnetId": self._slot.tailnet_id, "localReady": False,
                    "hasActive": self._active() is not None,
                    "pending": [observe(value) for value in pending]}

    def _descriptor(self, observation):
        raw = self._slot.read((*self._slot.state, "descriptor.json"))
        if raw is None:
            fail("TAILNET_CA_UNPROVISIONED")
        try:
            descriptor = parse_descriptor(decode(raw))
        except (ValueError, TypeError, UnicodeError):
            fail("TAILNET_AUTHORITY_DESCRIPTOR_CONFLICT")
        if descriptor.tailnet_id != self._slot.tailnet_id or descriptor.target.authority != observation.authority_pin:
            fail("TAILNET_AUTHORITY_DESCRIPTOR_CONFLICT")
        return descriptor

    def _verify(self, value):
        if value["device"] != device_record(self._local_record()):
            fail("TAILNET_LOCAL_DEVICE_MISMATCH", "recover")
        try:
            transport = value["transport"]
            if set(transport) != {"descriptor", "mtls"} or transport["mtls"] is not None:
                raise ValueError("invalid enroll transport")
            descriptor = parse_descriptor(transport["descriptor"])
            if (descriptor.tailnet_id != self._slot.tailnet_id
                    or type(value["expectedGeneration"]) is not int or value["expectedGeneration"] != 0
                    or value["route"] != "/v1/tailnet-credentials/enroll"):
                raise ValueError("invalid frozen preparation")
            if value["phase"] == "PREPARING" and any(
                value[field] is not None for field in ("request", "requestSha256", "staging")
            ):
                raise ValueError("invalid preparing state")
            if value["phase"] != "PREPARING":
                raw = base64.b64decode(value["request"], validate=True)
                if sha(raw) != value["requestSha256"]:
                    raise ValueError("invalid frozen request")
                request = decode(raw)
                if (set(request) != {"version", "tailnetId", "operationId", "deviceUid",
                                     "expectedCredentialGeneration", "csrPem"}
                        or request["version"] != TRANSPORT_VERSION
                        or request["tailnetId"] != self._slot.tailnet_id
                        or request["operationId"] != value["operationId"]
                        or request["deviceUid"] != value["device"]["deviceUid"]
                        or type(request["expectedCredentialGeneration"]) is not int
                        or request["expectedCredentialGeneration"] != 0):
                    raise ValueError("invalid request binding")
                path = (*self._slot.secrets, "staged", value["operationId"], "key.pem")
                material = self._slot.read_with_marker(path)
                if material is None or material[1] != value["staging"]:
                    raise ValueError("staging mismatch")
                key = material[0]
                private = serialization.load_pem_private_key(key, password=None)
                csr = x509.load_pem_x509_csr(request["csrPem"].encode("ascii"))
                public = private.public_key().public_bytes(serialization.Encoding.DER,
                                                          serialization.PublicFormat.SubjectPublicKeyInfo)
                if (not isinstance(private, rsa.RSAPrivateKey) or private.key_size != 2048
                        or len(csr.subject) or len(csr.extensions) or not csr.is_signature_valid
                        or csr.public_key().public_bytes(serialization.Encoding.DER,
                                                        serialization.PublicFormat.SubjectPublicKeyInfo) != public):
                    raise ValueError("CSR mismatch")
            if value["phase"] == "RECEIVED":
                body = base64.b64decode(value["responseBody"], validate=True)
                if (sha(body) != value["responseSha256"]
                        or receipt(value, value["responseStatus"], body) != value["remoteOutcome"]):
                    raise ValueError("invalid cached receipt")
        except (ValueError, TypeError, KeyError, UnicodeError):
            fail("TAILNET_JOURNAL_INVALID", "recover")

    @public_gate
    def prepare(self, *, kind, operation_id, expected_generation):
        identifier = check_operation_id(operation_id)
        integer(expected_generation, minimum=0)
        if kind != "enroll" or expected_generation != 0:
            fail("TAILNET_RUNTIME_NOT_READY")
        with self._slot.locked():
            record, observation = self._fresh()
            descriptor = self._descriptor(observation)
            existing = self._journal.load(identifier)
            if existing is not None:
                self._verify(existing)
                if existing["transport"] != transport_record(FrozenTransport(descriptor, None)):
                    fail("TAILNET_AUTHORITY_DESCRIPTOR_CONFLICT", "recover")
                if existing["phase"] in ("PREPARED", "SENDING", "RECEIVED"):
                    return observe(existing)
            elif self._journal.pending():
                fail("TAILNET_OPERATION_PENDING")
            if self._slot.read((*self._slot.state, "active.json")) is not None:
                fail("TAILNET_ASSOCIATION_CONFLICT")
            value = existing or {
                "version": VERSION, "home": self._slot.identity, "tailnetId": self._slot.tailnet_id,
                "operationId": identifier, "kind": kind, "phase": "PREPARING", "remoteOutcome": "NOT_SENT",
                "device": device_record(record), "transport": transport_record(FrozenTransport(descriptor, None)),
                "expectedGeneration": expected_generation, "route": "/v1/tailnet-credentials/enroll",
                "request": None, "requestSha256": None, "staging": None,
            }
            self._journal.save(value)
            _checkpoint("PREPARING")
            path = (*self._slot.secrets, "staged", identifier, "key.pem")
            if self._slot.read(path) is not None:
                fail("TAILNET_STAGING_UNPROVEN")
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            created = self._slot.write(path, pem, exclusive=True)
            _checkpoint("KEY_DURABLE")
            csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name([])).sign(key, hashes.SHA256())
            request = encode({"version": TRANSPORT_VERSION, "tailnetId": self._slot.tailnet_id,
                              "operationId": identifier, "deviceUid": record.device_uid,
                              "expectedCredentialGeneration": 0,
                              "csrPem": csr.public_bytes(serialization.Encoding.PEM).decode("ascii")})
            _checkpoint("BEFORE_PREPARED")
            current = self._slot.read_with_marker(path)
            if current is None or current[1] != created:
                fail("TAILNET_STAGING_UNPROVEN")
            value.update(phase="PREPARED", request=base64.b64encode(request).decode("ascii"),
                         requestSha256=sha(request), staging=created)
            self._journal.save(value)
            _checkpoint("PREPARED")
            return observe(value)

    @public_gate
    def execute(self, operation_id):
        identifier = check_operation_id(operation_id)
        with self._slot.locked():
            value = self._journal.load(identifier)
            if value is None:
                fail("TAILNET_OPERATION_UNKNOWN", "send")
            try:
                return self._execute_locked(value)
            except TailnetCredentialError as error:
                failure = error.code, error.phase, error.remote_outcome
            except OSError:
                failure = "TAILNET_AUTHORITY_UNAVAILABLE", "send", "UNKNOWN"
            outcome = failure[2]
            if outcome != "UNKNOWN" and value["remoteOutcome"] != "NOT_SENT":
                outcome = value["remoteOutcome"]
            raise TailnetCredentialError(failure[0], phase=failure[1], remote_outcome=outcome)

    def _execute_locked(self, value):
        from ..authority.service import local
        self._verify(value)
        if value["phase"] == "RECEIVED":
            return observe(value)
        if value["phase"] == "PREPARING":
            fail("TAILNET_STAGING_UNPROVEN", "send")
        _, observation = self._fresh()
        if observation.role != "authority":
            fail("TAILNET_RUNTIME_NOT_READY", "send")
        descriptor = self._descriptor(observation)
        if value["transport"] != transport_record(FrozenTransport(descriptor, None)):
            fail("TAILNET_AUTHORITY_DESCRIPTOR_CONFLICT", "send")
        previous = value["remoteOutcome"]
        value.update(phase="SENDING", remoteOutcome="UNKNOWN", previousRemoteOutcome=previous,
                     attemptId=str(uuid4()), attemptOutcome=None, responseStatus=None,
                     responseBody=None, responseSha256=None)
        self._save_sending(value)
        _checkpoint("SENDING")
        status, body = local(self, value["route"], base64.b64decode(value["request"], validate=True), descriptor)
        try:
            outcome = receipt(value, status, body)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise TailnetCredentialError("TAILNET_RESPONSE_INVALID", phase="send", remote_outcome="UNKNOWN") from None
        if outcome == "UNKNOWN":
            raise TailnetCredentialError("TAILNET_ATTEMPT_FAILED", phase="send", remote_outcome="UNKNOWN")
        value.update(phase="RECEIVED", remoteOutcome=outcome, attemptOutcome=outcome,
                     responseStatus=status, responseBody=base64.b64encode(body).decode(), responseSha256=sha(body))
        _checkpoint("BEFORE_RECEIVED")
        self._save_sending(value)
        _checkpoint("RECEIVED")
        return observe(value)

    def _save_sending(self, value):
        try:
            self._journal.save(value)
        except (OSError, TailnetCredentialError):
            raise TailnetCredentialError("TAILNET_JOURNAL_UNAVAILABLE", phase="send", remote_outcome="UNKNOWN") from None

    @public_gate
    def recover(self, operation_id):
        return self.execute(operation_id)

    @public_gate
    def discard_staged(self, operation_id, *, confirmed=False):
        check_operation_id(operation_id)
        fail("TAILNET_RUNTIME_NOT_READY", "discard")
