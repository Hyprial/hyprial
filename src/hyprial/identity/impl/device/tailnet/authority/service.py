"""Private local-authority handler. Remote connection authentication is not implemented."""

import sqlite3

from ..ports import TailnetCredentialError
from ..runtime.schema import TRANSPORT_VERSION, device_record, encode, fail, sha
from .issuer import ensure
from .profile import ROUTE, issue, request
from .store import Store


def _checkpoint(_name):
    """Internal handler boundaries, not injectable production send callbacks."""


def _response(operation_id, route, raw, *, code=None, data=None, durable=False):
    receipt = {"route": route, "requestSha256": sha(raw)} if durable else None
    value = {"version": TRANSPORT_VERSION, "ok": code is None,
             "operationId": operation_id, "receipt": receipt}
    if code is None:
        value["data"] = data
        status = 200
    else:
        value["error"] = {"code": code, "retryable": code == "AUTHORITY_UNAVAILABLE"}
        status = {"CSR_INVALID": 400, "BAD_REQUEST": 400, "DEVICE_BINDING_CONFLICT": 409,
                  "DEVICE_REVOKED": 403, "IDEMPOTENCY_CONFLICT": 409,
                  "CREDENTIAL_GENERATION_STALE": 409,
                  "AUTHORITY_UNAVAILABLE": 503, "UNSAFE_AUTHORITY_STATE": 503}[code]
    return status, encode(value)


def local(runtime, route, raw, frozen_descriptor):
    """Caller owns the slot OS lock; only Runtime.execute uses this in production.

    No caller-supplied trusted flag, peer object, handler or issuer is accepted.
    The closure below is the short-lived, non-exported local authority capability.
    """
    from ..runtime.workflow import Runtime
    if type(runtime) is not Runtime:
        fail("TAILNET_CONNECTION_UNTRUSTED", "authority")
    record, observation = runtime._fresh()
    if observation.role != "authority" or observation.authority_pin != frozen_descriptor.target.authority:
        fail("TAILNET_AUTHORITY_MISMATCH", "authority")
    original_network = device_record(record)
    # UID is self-reported credential metadata, not part of authenticated network identity.
    network = {key: original_network[key] for key in ("owner", "deviceId", "masterPublic", "serverPublic")}

    def verify_capability():
        current, fresh = runtime._fresh()
        if (fresh.role != "authority" or fresh.authority_pin != observation.authority_pin
                or device_record(current) != original_network):
            fail("TAILNET_AUTHORITY_MISMATCH", "authority")

    verify_capability()
    descriptor, ca_key, ca = ensure(runtime._slot, observation.authority_pin,
                                    frozen_descriptor.target.remote_port)
    if descriptor != frozen_descriptor:
        fail("TAILNET_AUTHORITY_DESCRIPTOR_CONFLICT", "authority")
    try:
        # Decode the supported body before checking a stored operation's route.
        value = request(ROUTE, raw, runtime._slot.tailnet_id)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return _response(None, route, raw, code="BAD_REQUEST")
    identifier = value["operationId"]
    frozen_network = encode(network)
    try:
        with Store(runtime._slot, descriptor.ca_sha256).transaction() as database:
            previous = database.execute("SELECT * FROM operations WHERE device_id=? AND operation_id=?",
                                        (record.device_id, identifier)).fetchone()
            if previous is not None:
                if (previous["route"] != route or previous["request"] != raw
                        or previous["network"] != frozen_network):
                    result = _response(identifier, route, raw, code="IDEMPOTENCY_CONFLICT")
                else:
                    result = previous["status"], previous["response"]
                verify_capability()
                return result
            if route != ROUTE:
                return _response(identifier, route, raw, code="BAD_REQUEST")
            binding = database.execute("SELECT b.* FROM current c JOIN bindings b USING(binding_id) WHERE c.device_id=?",
                                       (record.device_id,)).fetchone()
            if value["expectedCredentialGeneration"] != 0:
                result = _response(identifier, route, raw, code="CREDENTIAL_GENERATION_STALE", durable=True)
            elif binding is not None:
                result = _response(identifier, route, raw, code="DEVICE_REVOKED" if binding["state"] == "retired"
                                   else "DEVICE_BINDING_CONFLICT", durable=True)
            else:
                material = issue(value, network, descriptor, ca_key, ca)
                if material is None:
                    result = _response(identifier, route, raw, code="CSR_INVALID", durable=True)
                else:
                    binding_id = material["credential"]["bindingId"]
                    database.execute("INSERT INTO bindings VALUES(?,?,?,?,?)",
                                     (binding_id, record.device_id, frozen_network, "active", encode(material)))
                    database.execute("INSERT INTO current VALUES(?,?)", (record.device_id, binding_id))
                    result = _response(identifier, route, raw, data=material, durable=True)
            database.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?)",
                             (record.device_id, identifier, route, raw, frozen_network, result[0], result[1]))
            _checkpoint("RESULT_STAGED")
            verify_capability()
        return result
    except (sqlite3.Error, OSError):
        return _response(identifier, route, raw, code="AUTHORITY_UNAVAILABLE")
    except TailnetCredentialError as error:
        if error.code not in ("UNSAFE_AUTHORITY_STATE", "TAILNET_STORAGE_UNSAFE"):
            raise
        return _response(identifier, route, raw, code="UNSAFE_AUTHORITY_STATE")
