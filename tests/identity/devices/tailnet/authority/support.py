"""Local authority network facts; actual files/crypto, never self-dial."""

from hyprial.identity import TailnetAuthorityPin, TailnetCredentials, TailnetMember, TailnetMembership
from tests.identity.devices.tailnet.runtime.support import Context, state, secret, prepare, inventory, child
import base64
import json
import sqlite3
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

__all__ = ["LocalContext", "state", "secret", "prepare", "inventory", "child", "body", "call", "journal", "image_rows"]


class LocalContext(Context):
    def pin(self, tailnet_id):
        r = self.record
        return TailnetAuthorityPin(tailnet_id, r.owner, r.device_id, r.client_public,
                                  r.server_public, "own-authority-address", 7448)

    def membership(self, tailnet_id):
        r = self.record
        return self.observations.get(tailnet_id) or TailnetMembership(
            tailnet_id, "authority", 1, r.device_id, True, None, None,
            TailnetMember(r.owner, r.device_id, "active"), self.pin(tailnet_id))

    def members(self, tailnet_id):
        if self.rows_override is not None:
            return self.rows_override
        p = self.pin(tailnet_id)
        return (TailnetCredentials.MemberProjection(p.owner, p.device_id, p.master_public,
                p.server_public, (), p.address, "active", 1),)

    def dial(self, target):
        raise AssertionError("authority-local must not self-dial")


def body(context, *, identifier=None, csr=None):
    if csr is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([]))
               .sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode())
    return json.dumps({"version": "hyprial.tailnet.transport.v1", "tailnetId": "tailnet-A",
                       "operationId": identifier or str(uuid4()), "deviceUid": context.record.device_uid,
                       "expectedCredentialGeneration": 0, "csrPem": csr}, separators=(",", ":")).encode()


def call(context, raw):
    """Private handler unit boundary, using real fresh local capability creation.

    Not a remote adapter or caller-supplied trusted identity.
    """
    from hyprial.identity.impl.device.tailnet.authority.service import local
    descriptor = context.provision()
    runtime = context.runtime()
    with runtime._slot.locked():
        return local(runtime, "/v1/tailnet-credentials/enroll", raw, descriptor)


def journal(context, identifier):
    value = json.loads((state(context.home) / "journal" / (identifier + ".json")).read_bytes())
    response = json.loads(base64.b64decode(value["responseBody"])) if value.get("responseBody") else None
    return value, response


def image_rows(context, sql, parameters=()):
    connection = sqlite3.connect(":memory:")
    try:
        connection.row_factory = sqlite3.Row
        connection.deserialize((state(context.home) / "authority/store.sqlite3").read_bytes())
        return [dict(row) for row in connection.execute(sql, parameters)]
    finally:
        connection.close()
