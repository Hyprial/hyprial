"""Synthetic trusted ports and real loopback TLS, never a production adapter."""

from dataclasses import replace
import json
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from hyprial.identity import (
    DeviceAuthorityConfig,
    TailnetAuthorityBinding,
    TailnetAuthorityPin,
    TailnetCredentialAssociation,
    TailnetCredentialWorkflow,
    TailnetMember,
    TailnetMembership,
    write_device_record,
)
from tests.identity.devices.binding import support as b
from tests.identity.devices.client.support import (
    LoopbackAuthority as LoopbackAuthority,
    raw_response,
    success_envelope,
)
from tests.identity.devices.transport_safety.inventory import inventory as inventory


class Context:
    def __init__(self, home, *, origin="https://127.0.0.1:1", ca=None):
        self.home = home
        self.ca = ca or b.make_ca()
        self.record = b.device_record(uuid4().hex)
        write_device_record(home, self.record)
        pin = TailnetAuthorityPin(
            "tailnet-A",
            "authority-owner",
            "authority-node",
            "master",
            "server",
            "192.0.2.254",
            7448,
        )
        self.snapshot = TailnetMembership(
            "tailnet-A",
            "member",
            1,
            "authority-node",
            True,
            250,
            None,
            TailnetMember(self.record.owner, self.record.device_id, "active"),
            pin,
        )
        config = DeviceAuthorityConfig(origin, "127.0.0.1", self.ca.pem, 1, 1, 1, 1)
        self.binding = TailnetAuthorityBinding(
            pin, config, b.POLICY_ISSUER, b.TRUST_BUNDLE_ID
        )
        self.current = None
        self.lookup_error = False
        self.calls = []
        self.issued = {}

    def membership(self, tailnet_id):
        self.calls.append("membership")
        return self.snapshot

    def authority(self, tailnet_id, pin):
        self.calls.append("authority")
        return self.binding

    def association(self, tailnet_id, device_id, pin):
        self.calls.append("association")
        if self.lookup_error:
            raise OSError("synthetic lookup unavailable")
        return self.current

    def workflow(self):
        return TailnetCredentialWorkflow(
            self.home, tailnet_id="tailnet-A", context=self
        )

    def accept(self, data):
        self.current = TailnetCredentialAssociation(
            self.binding.authority_pin,
            "tailnet-A",
            self.record.device_id,
            data["owner"],
            data["deviceUid"],
            data["bindingId"],
            data["credentialGeneration"],
            data["spkiSha256"],
            data["ownerUserId"],
            data["policyIssuer"],
            data["trustBundleId"],
            "active",
        )

    def issue(self, request):
        payload = json.loads(request.split(b"\r\n\r\n", 1)[1])
        if payload["operationId"] in self.issued:
            return self.issued[payload["operationId"]]
        csr = x509.load_pem_x509_csr(payload["csrPem"].encode())
        from hashlib import sha256

        spki = sha256(
            csr.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).hexdigest()
        claims = b.claims_for(
            binding_id=str(uuid4()),
            device_uid=payload["deviceUid"],
            credential_generation=payload["expectedCredentialGeneration"] + 1,
            spki_sha256=spki,
            renewable_until=b.horizon(),
            owner=self.record.owner,
        )
        cn = f"hdev-{claims['bindingId']}-g{claims['credentialGeneration']}"
        cert = b.issue_leaf(
            self.ca,
            csr.public_key(),
            common_name=cn,
            uri_sans=(b.reference_uri(claims),),
        )
        data = {k: claims[k] for k in claims if k != "profile"}
        data.update(
            certificatePem=cert.public_bytes(serialization.Encoding.PEM).decode(),
            chainPem=self.ca.pem.decode(),
            cn=cn,
            serial=format(cert.serial_number, "x"),
            notBefore=int(cert.not_valid_before_utc.timestamp()),
            notAfter=int(cert.not_valid_after_utc.timestamp()),
        )
        self.accept(data)
        response = raw_response(success_envelope(payload["operationId"], data))
        self.issued[payload["operationId"]] = response
        return response


def stage(w, *, kind="enroll", generation=0):
    return w.stage(str(uuid4()), generation, kind=kind)


def connect_context(home, server):
    return Context(home, origin=server.origin, ca=server.ca)


def enroll_ready(context):
    w = context.workflow()
    handle = stage(w)
    w.enroll(handle)
    w.validate(handle)
    w.commit(handle)
    from hyprial.identity import load_client_tls_material

    context.binding = replace(
        context.binding,
        config=replace(
            context.binding.config, client_tls=load_client_tls_material(context.home)
        ),
    )
    return w, handle


def request_count(server):
    """Keep pytest assertion introspection from rendering HTTP/CSR bytes."""
    return len(server.observation.requests)


def request_hash(server, index):
    from hashlib import sha256

    return sha256(server.observation.requests[index]).hexdigest()
