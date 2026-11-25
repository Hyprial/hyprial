"""userKey IPC declarations and operator-only U1 composition."""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path

from hyprial.identity import UserKey, machine_fingerprint, read_device_record
from hyprial.kernel import IdempotencySpec, MethodError, MethodSpec, canonical_user_uri, ipc_errors
from .policy import Caller
from .service import Signer

METHODS = frozenset({"userKey.sign", "userKey.status", "userKey.rotate", "userKey.revoke", "userKey.renew"})


def compose(app) -> Signer:
    sub = uid = ""
    try:
        settings = json.loads((Path(app.hyprial_home) / "settings.json").read_text())
        device = read_device_record(app.hyprial_home)
        if (settings.get("owner") == app.owner and isinstance(settings.get("userId"), str)
                and device is not None and device.owner == app.owner and device.device_uid):
            sub, uid = settings["userId"], device.device_uid
    except (OSError, ValueError, TypeError, AttributeError):
        pass  # No identity proof: unavailable, never a fabricated deviceUid.
    store = UserKey.Store(state_dir=app.state_dir, owner=app.owner, casdoor_sub=sub,
                          device_uid=uid, fingerprint=machine_fingerprint())
    # Local verification does not activate production. No settings/IPC field
    # can inject an issuer, trust source or signing activation.
    app._user_key_signer = Signer(store)
    return app._user_key_signer


def signer_for(app) -> Signer:
    return getattr(app, "_user_key_signer", None) or compose(app)


def preauthorize(method, app, params, *, trusted_message_origin=None):
    """Refuse known agents before schema validation; JSON cannot set origin."""
    peer_pid = getattr(getattr(app, "_ipc_peer", None), "pid", None)
    agent = trusted_message_origin == "worker" or any(k in params for k in ("actor", "sessionRef"))
    if not agent and peer_pid is not None:
        agent = app._interactive_identity_binding_peer(peer_pid)
    if agent or trusted_message_origin not in (None, "ipc", "daemon"):
        # Do not retain caller-supplied actor/session/purpose in a refusal log.
        caller = Caller("worker" if trusted_message_origin == "worker" else "session", "unverified", frozenset(), pid=peer_pid)
        signer_for(app).refuse(method, caller, ipc_errors.CALLER_NOT_AUTHORIZED)


def handle(method, app, params, *, trusted_message_origin=None):
    preauthorize(method, app, params, trusted_message_origin=trusted_message_origin)
    signer = signer_for(app)
    operator = trusted_message_origin == "ipc"
    caller = Caller("operator" if operator else "daemon",
        canonical_user_uri(app.owner) if operator else "daemon", frozenset(),
                    pid=getattr(getattr(app, "_ipc_peer", None), "pid", None))
    if method == "userKey.status":
        if not signer.store.casdoor_sub or not signer.store.device_uid:
            return {"keyId": None, "publicKey": None, "state": "unavailable",
                    "bindingExp": None, "revocationSeq": None}
        return signer.status()
    if method in {"userKey.rotate", "userKey.revoke", "userKey.renew"}:
        if not operator:
            signer.refuse(method, caller, ipc_errors.CALLER_NOT_AUTHORIZED)
        # These require U2 issuance, publication and revocation. No destructive
        # local pretend-success or rotation before a trusted replacement exists.
        signer.refuse(method, caller, ipc_errors.USER_KEY_UNBOUND)
    return {"envelope": signer.sign(params["purpose"], params["payload"], caller)}


_ERRORS = tuple(MethodError(code=code, description=description) for code, description in (
    (ipc_errors.CALLER_NOT_AUTHORIZED, "Known agent or non-operator caller refused."),
    (ipc_errors.USER_KEY_ABSENT, "No local user key."),
    (ipc_errors.USER_KEY_UNBOUND, "Trusted signing or lifecycle prerequisite unavailable."),
    (ipc_errors.USER_KEY_BINDING_EXPIRED, "Verified binding freshness expired."),
    (ipc_errors.USER_KEY_QUARANTINED, "Copied identity or unsafe local storage."),
    (ipc_errors.PURPOSE_UNKNOWN, "Purpose not in the closed registry."),
    (ipc_errors.PAYLOAD_INVALID, "Payload does not match the purpose schema."),
    (ipc_errors.RATE_LIMITED, "Caller-purpose rate limit reached."),
    (ipc_errors.INVALID_ARGUMENT, "IPC parameters do not match the declared schema."),
))

_STATUS = {"type": "object", "properties": {
    "keyId": {"type": ["string", "null"]}, "publicKey": {"type": ["string", "null"]},
    "state": {"enum": ["absent", "unavailable", "unbound", "bound", "quarantined", "retired"]},
    "bindingExp": {"type": ["number", "null"]}, "revocationSeq": {"type": ["integer", "null"]},
}, "required": ["keyId", "publicKey", "state", "bindingExp", "revocationSeq"], "additionalProperties": False}


def _spec(method):
    properties = {}
    required = []
    if method == "userKey.sign":
        properties = {"purpose": {"type": "string", "maxLength": 128}, "payload": {"type": "object"},
                      "actor": {"type": "string"}, "sessionRef": {"type": "string"}}
        required = ["purpose", "payload"]
    elif method == "userKey.revoke":
        properties = {"deviceUid": {"type": "string", "minLength": 1, "maxLength": 128}}
        required = ["deviceUid"]
    return MethodSpec(
        name=method, owner="daemon.user-key",
        params_schema={"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
        result_schema=_STATUS if method == "userKey.status" else {"type": "object", "properties": {
            "envelope": {"type": "object"}}, "required": ["envelope"], "additionalProperties": False},
        errors=_ERRORS, mode="unary", mutates=method != "userKey.status",
        retry="safe" if method == "userKey.status" else "never",
        audience=("local-operator",), http_exposed=False, confirmation="none",
        idempotency=IdempotencySpec(mode="safe" if method == "userKey.status" else "never",
                                   scope="local-user-key"))


USER_KEY_BINDINGS = tuple((_spec(method), partial(handle, method)) for method in sorted(METHODS))
