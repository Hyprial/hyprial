"""Isolated staging discard behavior; shared real storage and TLS fixtures."""

import json
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

from tests.identity.devices.client.support import (
    error_envelope,
    raw_response,
    success_envelope,
)
from ..support import LoopbackAuthority, connect_context, enroll_ready
from ..support import Context as Context, inventory as inventory, stage as stage


def reject(request):
    payload = json.loads(request.split(b"\r\n\r\n", 1)[1])
    return raw_response(
        error_envelope(payload["operationId"], "DEVICE_REVOKED", False),
        status="403 Forbidden",
    )


def targets(home, workflow, handle):
    opid = workflow.operation_status(handle)["operationId"]
    return (
        home / "secrets/transport/staged" / opid / "key.pem",
        home / "state/transport/enrollment.json",
    )


def body_hash(server, index):
    return sha256(
        server.observation.requests[index].split(b"\r\n\r\n", 1)[1]
    ).hexdigest()


@contextmanager
def operation_authority(home, route):
    """Real TLS/mTLS plus one semantic mutation per original operation ID."""
    c = w = None
    if route != "enroll":
        with LoopbackAuthority([], accept_timeout=0.5) as bootstrap:
            c = connect_context(home, bootstrap)
            bootstrap.responders = [c.issue]
            w, _ = enroll_ready(c)
    with LoopbackAuthority(
        [],
        ca=c.ca if c else None,
        require_client_cert=route != "enroll",
        accept_timeout=0.5,
        max_sessions=2,
    ) as server:
        if c is None:
            c = connect_context(home, server)
            w = c.workflow()
        else:
            c.binding = replace(
                c.binding, config=replace(c.binding.config, origin=server.origin)
            )
        responses = {}
        mutations = []

        def respond(request):
            body = json.loads(request.split(b"\r\n\r\n", 1)[1])
            opid = body["operationId"]
            if opid not in responses:
                mutations.append(opid)
                if route == "retire":
                    c.current = replace(c.current, state="retired")
                    responses[opid] = raw_response(
                        success_envelope(
                            opid,
                            {
                                "operationId": opid,
                                "retired": True,
                                "remoteEnforcementState": "pending",
                            },
                        )
                    )
                else:
                    responses[opid] = c.issue(request)
            return responses[opid]

        server.responders = [respond, respond]
        handle = (
            w.prepare_retire(str(uuid4()))
            if route == "retire"
            else stage(w, kind=route, generation=0 if route == "enroll" else 1)
        )
        send = {"enroll": w.enroll, "renew": w.renew, "retire": w.retire_remote}[route]
        yield c, w, handle, send, server, mutations
