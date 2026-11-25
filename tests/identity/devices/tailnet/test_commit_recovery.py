from .support import request_count
from dataclasses import replace
from uuid import uuid4

import pytest

from hyprial.identity import TailnetCredentialError, read_transport_credential
from .support import LoopbackAuthority, connect_context, inventory, stage


@pytest.mark.parametrize(
    "case",
    [
        "lease",
        "tailnet_id",
        "device_id",
        "owner",
        "device_uid",
        "binding_id",
        "credential_generation",
        "spki_sha256",
        "owner_user_id",
        "policy_issuer",
        "trust_bundle_id",
        "state",
        "authority_pin",
        "absent",
        "unavailable",
    ],
)
def test_commit_fresh_gate_prevents_actual_install(tmp_path, case):
    with LoopbackAuthority([]) as server:
        c = connect_context(tmp_path, server)
        server.responders = [c.issue]
        w = c.workflow()
        h = stage(w)
        w.enroll(h)
        w.validate(h)
        if case == "lease":
            c.snapshot = replace(c.snapshot, lease_remaining_seconds=0)
        elif case == "absent":
            c.current = None
        elif case == "unavailable":
            c.lookup_error = True
        else:
            value = 9 if case == "credential_generation" else "other"
            if case == "authority_pin":
                value = replace(c.current.authority_pin, master_public="other")
            c.current = replace(c.current, **{case: value})
        before = inventory(tmp_path)
        caught = None
        try:
            w.commit(h)
        except TailnetCredentialError as error:
            caught = error
        assert read_transport_credential(tmp_path) is None, (
            "unsafe gate installed real credential"
        )
        assert inventory(tmp_path) == before
        assert caught is not None
        assert w.operation_status(h)["remoteOutcome"] == "SUCCESS"


@pytest.mark.parametrize("case", ["lease", "association", "issuer", "bundle"])
def test_success_is_cached_before_validation_failure(tmp_path, case):
    with LoopbackAuthority([]) as server:
        c = connect_context(tmp_path, server)

        def response(request):
            result = c.issue(request)
            if case == "lease":
                c.snapshot = replace(c.snapshot, lease_remaining_seconds=0)
            if case == "association":
                c.lookup_error = True
            if case == "issuer":
                c.current = replace(c.current, policy_issuer="other")
            if case == "bundle":
                c.current = replace(c.current, trust_bundle_id="other")
            return result

        server.responders = [response]
        w = c.workflow()
        h = stage(w)
        w.enroll(h)
        caught = None
        try:
            w.validate(h)
            w.commit(h)
        except TailnetCredentialError as error:
            caught = error
        assert read_transport_credential(tmp_path) is None, (
            "post-success failure installed credential"
        )
        assert caught is not None
        status = w.operation_status(h)
        assert status["remoteOutcome"] == "SUCCESS"
        assert status["hasResponse"] is True
        assert bool(w.enroll(h)) is True
        assert request_count(server) == 1


def test_successful_commit_and_offline_observation(tmp_path):
    with LoopbackAuthority([]) as server:
        c = connect_context(tmp_path, server)
        server.responders = [c.issue]
        w = c.workflow()
        h = stage(w)
        returned = w.enroll(h)
        returned.clear()
        w.validate(h)
        w.commit(h)
    c.lookup_error = True
    c.snapshot = None
    assert w.read() is not None
    assert w.status()
    assert w.operation_status(h)["phase"] == "COMMITTED"


def test_pending_home_slot_rejects_new_retire_or_duplicate_stage(tmp_path):
    c = connect_context(
        tmp_path, type("Server", (), {"origin": "https://127.0.0.1:1", "ca": None})()
    )
    w = c.workflow()
    opid = str(uuid4())
    h = w.stage(opid, 0)
    with pytest.raises(TailnetCredentialError):
        w.stage(opid, 0)
    with pytest.raises(TailnetCredentialError):
        w.prepare_retire(str(uuid4()))
    assert w.operation_status(h)["phase"] == "PREPARED"


def test_foreign_and_fabricated_handles_and_kind_misuse(tmp_path):
    from hyprial.identity import TailnetCredentialOperation

    c = connect_context(
        tmp_path, type("Server", (), {"origin": "https://127.0.0.1:1", "ca": None})()
    )
    w = c.workflow()
    h = stage(w)
    for handle in [TailnetCredentialOperation(), object(), [], h]:
        with pytest.raises(TailnetCredentialError):
            (w.renew(handle) if handle is h else w.enroll(handle))
    with pytest.raises(TailnetCredentialError):
        c.workflow().enroll(h)
    with pytest.raises(AttributeError):
        h.operation_id = "mutated"


def test_offline_status_with_no_credential(tmp_path):
    c = connect_context(
        tmp_path, type("Server", (), {"origin": "https://127.0.0.1:1", "ca": None})()
    )
    w = c.workflow()
    c.snapshot = None
    assert w.read() is None
    assert w.status() is None


def test_commit_exception_recovers_spki_without_second_server_mutation(
    tmp_path, monkeypatch
):
    from hyprial.identity.impl.device import transport_credential as storage

    with LoopbackAuthority([]) as server:
        c = connect_context(tmp_path, server)
        server.responders = [c.issue]
        w = c.workflow()
        h = stage(w)
        w.enroll(h)
        w.validate(h)

        def fail_after_key(checkpoint):
            if checkpoint == "key-installed":
                raise OSError("synthetic interruption")

        monkeypatch.setattr(storage, "_commit_checkpoint", fail_after_key)
        with pytest.raises(OSError):
            w.commit(h)
        assert (tmp_path / "secrets/transport/device-key.pem").exists()
        assert w.operation_status(h)["remoteOutcome"] == "SUCCESS"
        with pytest.raises(TailnetCredentialError):
            stage(w)
        monkeypatch.setattr(storage, "_commit_checkpoint", lambda _: None)
        w.commit(h)
        assert w.read().spki_sha256 == c.current.spki_sha256
        assert request_count(server) == 1


def test_repeated_commit_is_observation_without_replacing_staging(tmp_path):
    from .support import enroll_ready

    with LoopbackAuthority([]) as server:
        c = connect_context(tmp_path, server)
        server.responders = [c.issue]
        w, h = enroll_ready(c)
        before = inventory(tmp_path)
        w.validate(h)
        w.commit(h)
        assert inventory(tmp_path) == before
        assert w.operation_status(h)["phase"] == "COMMITTED"


def test_boolean_association_generation_is_not_integer_generation(tmp_path):
    with LoopbackAuthority([]) as server:
        c = connect_context(tmp_path, server)
        server.responders = [c.issue]
        w = c.workflow()
        h = stage(w)
        w.enroll(h)
        w.validate(h)
        c.current = replace(c.current, credential_generation=True)
        caught = None
        try:
            w.commit(h)
        except TailnetCredentialError as error:
            caught = error
        assert read_transport_credential(tmp_path) is None, (
            "boolean generation installed credential"
        )
        assert caught is not None


def test_success_pending_install_blocks_replacement_before_ports(tmp_path):
    with LoopbackAuthority([]) as server:
        c = connect_context(tmp_path, server)
        server.responders = [c.issue]
        w = c.workflow()
        h = stage(w)
        w.enroll(h)
        before = list(c.calls)
        with pytest.raises(TailnetCredentialError) as caught:
            w.prepare_retire(str(uuid4()))
        assert caught.value.code == "TAILNET_OPERATION_PENDING"
        assert caught.value.remote_outcome == "SUCCESS"
        assert c.calls == before


def test_operation_status_never_exposes_cached_certificate_pem(tmp_path):
    import json

    with LoopbackAuthority([]) as server:
        c = connect_context(tmp_path, server)
        server.responders = [c.issue]
        w = c.workflow()
        h = stage(w)
        w.enroll(h)
        status = w.operation_status(h)
        contains_pem = "-----BEGIN" in json.dumps(status)
        assert contains_pem is False
        assert status["hasResponse"] is True
        cached = w.enroll(h)
        assert cached["spkiSha256"] == c.current.spki_sha256
        assert request_count(server) == 1


def test_received_non_ascii_certificate_failure_detaches_raw_chain(tmp_path):
    import json
    from hyprial.identity import TransportCredentialError
    from tests.identity.devices.client.support import raw_response, success_envelope

    with LoopbackAuthority([]) as server:
        c = connect_context(tmp_path, server)

        def invalid_certificate(request):
            response = c.issue(request)
            envelope = json.loads(response.split(b"\r\n\r\n", 1)[1])
            envelope["data"]["certificatePem"] = "\u00e9"
            return raw_response(
                success_envelope(envelope["operationId"], envelope["data"])
            )

        server.responders = [invalid_certificate]
        w = c.workflow()
        h = stage(w)
        w.enroll(h)
        assert w.operation_status(h)["remoteOutcome"] == "SUCCESS"
        with pytest.raises(TransportCredentialError) as caught:
            w.validate(h)
        error = caught.value
        has_context = error.__context__ is not None
        has_cause = error.__cause__ is not None
        assert error.code == "TRANSPORT_RESPONSE_INVALID"
        assert request_count(server) == 1
        assert has_context is False
        assert has_cause is False
        assert w.operation_status(h)["remoteOutcome"] == "SUCCESS"


@pytest.mark.parametrize("method", ["read", "status"])
def test_offline_corrupt_enrollment_detaches_raw_json_chain(tmp_path, method):
    from .support import Context
    from hyprial.identity import TransportCredentialError

    c = Context(tmp_path)
    w = c.workflow()
    stage(w)
    (tmp_path / "state/transport/enrollment.json").write_bytes(b"{")
    calls_before = list(c.calls)
    c.snapshot = None
    c.lookup_error = True
    with pytest.raises(TransportCredentialError) as caught:
        getattr(w, method)()
    error = caught.value
    has_context = error.__context__ is not None
    has_cause = error.__cause__ is not None
    assert error.code == "TRANSPORT_STAGING_CONFLICT"
    assert c.calls == calls_before
    assert has_context is False
    assert has_cause is False
