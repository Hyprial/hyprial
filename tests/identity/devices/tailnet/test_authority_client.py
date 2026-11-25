from .support import request_count, request_hash
from dataclasses import replace
from uuid import uuid4

import pytest

from hyprial.identity import (
    ClientTlsMaterial,
    TailnetCredentialError,
    TransportClientError,
)
from .support import LoopbackAuthority, connect_context, stage


@pytest.mark.parametrize(
    "case",
    [
        "lease",
        "owner",
        "uid",
        "pin-owner",
        "pin-master",
        "pin-server",
        "pin-address",
        "pin-port",
        "origin",
        "ca",
        "tls",
        "issuer",
        "bundle",
    ],
)
def test_fresh_send_gate_prevents_real_http(tmp_path, case):
    with LoopbackAuthority([], accept_timeout=0.2) as server:
        c = connect_context(tmp_path, server)
        w = c.workflow()
        handle = stage(w)
        s = c.snapshot
        if case == "lease":
            c.snapshot = replace(s, lease_remaining_seconds=0)
        if case == "owner":
            c.snapshot = replace(s, local_member=replace(s.local_member, owner="other"))
        if case == "uid":
            from hyprial.identity import write_device_record

            write_device_record(tmp_path, replace(c.record, device_uid=uuid4().hex))
        if case.startswith("pin-"):
            field = {
                "pin-owner": "owner",
                "pin-master": "master_public",
                "pin-server": "server_public",
                "pin-address": "address",
                "pin-port": "control_port",
            }[case]
            pin = replace(
                s.authority_pin, **{field: 7449 if case == "pin-port" else "other"}
            )
            c.snapshot = replace(s, authority_pin=pin)
            c.binding = replace(c.binding, authority_pin=pin)
        config = c.binding.config
        if case == "origin":
            c.binding = replace(
                c.binding, config=replace(config, origin="https://127.0.0.1:2")
            )
        if case == "ca":
            c.binding = replace(
                c.binding, config=replace(config, trust_bundle_pem=b"other")
            )
        if case == "tls":
            c.binding = replace(
                c.binding,
                config=replace(config, client_tls=ClientTlsMaterial(b"c", b"k", b"t")),
            )
        if case == "issuer":
            c.binding = replace(c.binding, policy_issuer="other")
        if case == "bundle":
            c.binding = replace(c.binding, trust_bundle_id="other")
        error = None
        try:
            w.enroll(handle)
        except (TailnetCredentialError, TransportClientError) as caught:
            error = caught
        assert request_count(server) == 0, "unsafe gate sent actual HTTP"
        assert isinstance(error, TailnetCredentialError)


def test_unknown_replay_gate_keeps_unknown_and_original_client(tmp_path):
    with LoopbackAuthority(
        [lambda _: None, lambda _: None], max_sessions=2, accept_timeout=0.3
    ) as server:
        c = connect_context(tmp_path, server)
        w = c.workflow()
        h = stage(w)
        with pytest.raises(TransportClientError):
            w.enroll(h)
        assert w.operation_status(h)["remoteOutcome"] == "UNKNOWN"
        c.snapshot = replace(c.snapshot, lease_remaining_seconds=0)
        with pytest.raises(TailnetCredentialError):
            w.enroll(h)
        assert w.operation_status(h)["remoteOutcome"] == "UNKNOWN"
        assert request_count(server) == 1


def test_unknown_replay_does_not_recheck_initial_absence(tmp_path):
    with LoopbackAuthority([], max_sessions=2) as server:
        c = connect_context(tmp_path, server)

        def lost(request):
            c.issue(request)
            return None

        server.responders = [lost, c.issue]
        w = c.workflow()
        h = stage(w)
        with pytest.raises(TransportClientError):
            w.enroll(h)
        response = w.enroll(h)
        assert w.operation_status(h)["remoteOutcome"] == "SUCCESS"
        assert request_hash(server, 0) == request_hash(server, 1)
        response.clear()
        assert w.operation_status(h)["hasResponse"] is True
        assert bool(w.enroll(h)) is True


def test_not_sent_after_unknown_does_not_erase_unknown(tmp_path):
    with LoopbackAuthority([lambda _: None], accept_timeout=0.3) as server:
        c = connect_context(tmp_path, server)
        w = c.workflow()
        h = stage(w)
        with pytest.raises(TransportClientError):
            w.enroll(h)
    with pytest.raises(TailnetCredentialError) as caught:
        w.enroll(h)
    assert caught.value.attempt_outcome == "NOT_SENT"
    assert caught.value.remote_outcome == "UNKNOWN"
    assert w.operation_status(h)["remoteOutcome"] == "UNKNOWN"


def test_first_send_rechecks_authoritative_absence(tmp_path):
    with LoopbackAuthority([], accept_timeout=0.3) as server:
        c = connect_context(tmp_path, server)
        w = c.workflow()
        h = stage(w)
        c.current = object()
        caught = None
        try:
            w.enroll(h)
        except (TailnetCredentialError, TransportClientError) as error:
            caught = error
        assert request_count(server) == 0, "first send ignored changed association"
        assert isinstance(caught, TailnetCredentialError)


def test_retryable_authority_error_stays_unknown_and_replays(tmp_path):
    import json
    from tests.identity.devices.client.support import error_envelope, raw_response

    with LoopbackAuthority([], max_sessions=2) as server:
        c = connect_context(tmp_path, server)

        def retryable(request):
            body = json.loads(request.split(b"\r\n\r\n", 1)[1])
            return raw_response(
                error_envelope(body["operationId"], "AUTHORITY_UNAVAILABLE", True),
                status="503 Service Unavailable",
            )

        server.responders = [retryable, c.issue]
        w = c.workflow()
        h = stage(w)
        with pytest.raises(TransportClientError):
            w.enroll(h)
        assert w.operation_status(h)["remoteOutcome"] == "UNKNOWN"
        w.enroll(h)
        assert request_hash(server, 0) == request_hash(server, 1)
        assert w.operation_status(h)["remoteOutcome"] == "SUCCESS"


def test_cosmetic_local_rename_does_not_strand_unknown_replay(tmp_path):
    from hyprial.identity import write_device_record

    with LoopbackAuthority([], max_sessions=2) as server:
        c = connect_context(tmp_path, server)

        def lost(request):
            c.issue(request)
            return None

        server.responders = [lost, c.issue]
        w = c.workflow()
        h = stage(w)
        with pytest.raises(TransportClientError):
            w.enroll(h)
        write_device_record(tmp_path, replace(c.record, name="renamed display"))
        w.enroll(h)
        assert w.operation_status(h)["remoteOutcome"] == "SUCCESS"
        assert request_hash(server, 0) == request_hash(server, 1)


def test_unknown_connection_failure_error_preserves_operation_outcome(tmp_path):
    with LoopbackAuthority([lambda _: None]) as server:
        c = connect_context(tmp_path, server)
        w = c.workflow()
        h = stage(w)
        with pytest.raises(TransportClientError):
            w.enroll(h)
    with pytest.raises(TailnetCredentialError) as caught:
        w.enroll(h)
    assert caught.value.attempt_outcome == "NOT_SENT"
    assert caught.value.remote_outcome == "UNKNOWN"
    assert caught.value.transport_code
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("case", ["origin", "ca", "tls", "pin", "issuer"])
def test_unknown_security_config_drift_never_replaces_original_client(tmp_path, case):
    with LoopbackAuthority([], max_sessions=2) as server:
        c = connect_context(tmp_path, server)

        def lost(request):
            c.issue(request)
            return None

        server.responders = [lost, c.issue]
        w = c.workflow()
        h = stage(w)
        with pytest.raises(TransportClientError):
            w.enroll(h)
        original_binding, original_snapshot = c.binding, c.snapshot
        if case == "origin":
            c.binding = replace(
                c.binding,
                config=replace(c.binding.config, origin="https://127.0.0.1:2"),
            )
        if case == "ca":
            c.binding = replace(
                c.binding, config=replace(c.binding.config, trust_bundle_pem=b"other")
            )
        if case == "tls":
            c.binding = replace(
                c.binding,
                config=replace(
                    c.binding.config, client_tls=ClientTlsMaterial(b"c", b"k", b"t")
                ),
            )
        if case == "pin":
            pin = replace(c.snapshot.authority_pin, server_public="other")
            c.snapshot = replace(c.snapshot, authority_pin=pin)
            c.binding = replace(c.binding, authority_pin=pin)
        if case == "issuer":
            c.binding = replace(c.binding, policy_issuer="other")
        with pytest.raises(TailnetCredentialError) as caught:
            w.enroll(h)
        assert caught.value.remote_outcome == "UNKNOWN"
        assert w.operation_status(h)["remoteOutcome"] == "UNKNOWN"
        assert request_count(server) == 1
        c.binding, c.snapshot = original_binding, original_snapshot
        w.enroll(h)
        assert request_hash(server, 0) == request_hash(server, 1)


@pytest.mark.parametrize("kind", ["enroll", "renew", "retire"])
def test_cached_rejection_detaches_active_caller_context_without_http(tmp_path, kind):
    import json
    from tests.identity.devices.client.support import error_envelope, raw_response
    from .support import enroll_ready

    if kind == "enroll":
        c = None
        w = None
    else:
        with LoopbackAuthority([]) as bootstrap:
            c = connect_context(tmp_path, bootstrap)
            bootstrap.responders = [c.issue]
            w, _ = enroll_ready(c)
    with LoopbackAuthority(
        [], ca=c.ca if c else None, require_client_cert=kind != "enroll"
    ) as server:
        if c is None:
            c = connect_context(tmp_path, server)
            w = c.workflow()
        else:
            c.binding = replace(
                c.binding, config=replace(c.binding.config, origin=server.origin)
            )

        def reject(request):
            body = json.loads(request.split(b"\r\n\r\n", 1)[1])
            return raw_response(
                error_envelope(body["operationId"], "DEVICE_REVOKED", False),
                status="403 Forbidden",
            )

        server.responders = [reject]
        h = (
            w.prepare_retire(str(uuid4()))
            if kind == "retire"
            else stage(
                w,
                kind=kind,
                generation=0 if kind == "enroll" else w.read().credential_generation,
            )
        )
        send = {"enroll": w.enroll, "renew": w.renew, "retire": w.retire_remote}[kind]
        with pytest.raises(TransportClientError) as initial:
            send(h)
        first = initial.value
        fields = (
            first.code,
            first.retryable,
            first.outcome,
            first.http_status,
            first.operation_id,
            first.reason_code,
            first.exception_class,
        )
        assert fields[:4] == ("DEVICE_REVOKED", False, "REJECTED", 403)
        assert request_count(server) == 1
        caught = None
        try:
            raise RuntimeError("synthetic caller sentinel")
        except RuntimeError:
            try:
                send(h)
            except TransportClientError as error:
                caught = error
                has_context = error.__context__ is not None
                has_cause = error.__cause__ is not None
        assert caught is first
        repeated_fields = (
            caught.code,
            caught.retryable,
            caught.outcome,
            caught.http_status,
            caught.operation_id,
            caught.reason_code,
            caught.exception_class,
        )
        assert repeated_fields == fields
        assert request_count(server) == 1
        assert has_context is False
        assert has_cause is False
