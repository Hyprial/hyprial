from .support import request_count, request_hash
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from hyprial.identity import TailnetCredentialError, TransportClientError
from tests.identity.devices.client.support import (
    error_envelope,
    raw_response,
    success_envelope,
)
from .support import LoopbackAuthority, connect_context, enroll_ready, inventory, stage


@pytest.mark.parametrize("case", ["lease", "association", "uid", "retired", "absent"])
def test_prepare_retire_rejects_stale_target(tmp_path, case):
    with LoopbackAuthority([]) as server:
        c = connect_context(tmp_path, server)
        server.responders = [c.issue]
        w, _ = enroll_ready(c)
        if case == "lease":
            c.snapshot = replace(c.snapshot, lease_remaining_seconds=0)
        if case == "association":
            c.lookup_error = True
        if case == "uid":
            c.current = replace(c.current, device_uid=uuid4().hex)
        if case == "retired":
            c.current = replace(c.current, state="retired")
        if case == "absent":
            c.current = None
        before = inventory(tmp_path)
        caught = None
        try:
            w.prepare_retire(str(uuid4()))
        except TailnetCredentialError as error:
            caught = error
        assert caught is not None, "unsafe remote retirement target was prepared"
        assert inventory(tmp_path) == before


def test_retire_unknown_replays_frozen_target_without_cleanup(tmp_path):
    with LoopbackAuthority([], max_sessions=3) as server:
        c = connect_context(tmp_path, server)

        def lost(request):
            c.current = replace(c.current, state="retired")
            return None

        def settled(request):
            body = json.loads(request.split(b"\r\n\r\n", 1)[1])
            return raw_response(
                success_envelope(
                    body["operationId"],
                    {
                        "operationId": body["operationId"],
                        "retired": True,
                        "remoteEnforcementState": "pending",
                    },
                )
            )

        server.responders = [c.issue, lost, settled]
        w, _ = enroll_ready(c)
        old = w.read()
        h = w.prepare_retire(str(uuid4()))
        before = inventory(tmp_path)
        with pytest.raises(TransportClientError):
            w.retire_remote(h)
        with pytest.raises(TailnetCredentialError):
            w.prepare_retire(str(uuid4()))
        assert w.operation_status(h)["remoteOutcome"] == "UNKNOWN"
        result = w.retire_remote(h)
        assert result["remoteEnforcementState"] == "pending"
        assert request_hash(server, 1) == request_hash(server, 2)
        body = json.loads(server.observation.requests[2].split(b"\r\n\r\n", 1)[1])
        assert body["bindingId"] == old.binding_id
        assert body["expectedCredentialGeneration"] == old.credential_generation
        assert inventory(tmp_path) == before
        assert w.read() == old
        assert not hasattr(w, "cleanup_local")


def test_rejected_renewal_keeps_old_ready_and_cached_rejection(tmp_path):
    with LoopbackAuthority([]) as bootstrap:
        c = connect_context(tmp_path, bootstrap)
        bootstrap.responders = [c.issue]
        w, _ = enroll_ready(c)
    with LoopbackAuthority([], ca=c.ca, max_sessions=2) as server:
        c.binding = replace(
            c.binding, config=replace(c.binding.config, origin=server.origin)
        )

        def reject(request):
            body = json.loads(request.split(b"\r\n\r\n", 1)[1])
            return raw_response(
                error_envelope(body["operationId"], "DEVICE_REVOKED", False),
                status="403 Forbidden",
            )

        server.responders = [reject, reject]
        old = w.read()
        h = stage(w, kind="renew", generation=old.credential_generation)
        before = inventory(tmp_path)
        with pytest.raises(TransportClientError):
            w.renew(h)
        with pytest.raises(TransportClientError):
            w.renew(h)
        assert request_count(bootstrap) + request_count(server) == 2, (
            "canonical rejection was sent again"
        )
        assert w.operation_status(h)["remoteOutcome"] == "REJECTED"
        assert w.read() == old
        assert inventory(tmp_path) == before
        with pytest.raises(TailnetCredentialError):
            stage(w, kind="renew", generation=old.credential_generation)


@pytest.mark.parametrize("kind", ["renew", "retire"])
@pytest.mark.parametrize("tls", ["absent", "wrong"])
def test_preparation_requires_exact_ready_mtls_before_side_effect(tmp_path, kind, tls):
    from hyprial.identity import ClientTlsMaterial

    with LoopbackAuthority([]) as server:
        c = connect_context(tmp_path, server)
        server.responders = [c.issue]
        w, _ = enroll_ready(c)
        old = w.read()
        material = (
            None
            if tls == "absent"
            else ClientTlsMaterial(b"other-cert", b"other-key", b"other-ca")
        )
        c.binding = replace(
            c.binding, config=replace(c.binding.config, client_tls=material)
        )
        before = inventory(tmp_path)
        caught = None
        try:
            if kind == "renew":
                stage(w, kind="renew", generation=old.credential_generation)
            else:
                w.prepare_retire(str(uuid4()))
        except TailnetCredentialError as error:
            caught = error
        assert inventory(tmp_path) == before, "wrong mTLS created actual staged key"
        assert caught is not None, "wrong ready mTLS accepted"
        assert request_count(server) == 1


def test_real_renewal_and_retire_use_ready_mtls(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    with LoopbackAuthority([]) as first:
        c = connect_context(tmp_path, first)
        first.responders = [c.issue]
        w, _ = enroll_ready(c)
    old = w.read()
    with LoopbackAuthority([], ca=c.ca, require_client_cert=True) as mtls:
        c.binding = replace(
            c.binding, config=replace(c.binding.config, origin=mtls.origin)
        )
        mtls.responders = [c.issue]
        h = stage(w, kind="renew", generation=old.credential_generation)
        w.renew(h)
        w.validate(h)
        w.commit(h)
        expected = x509.load_pem_x509_certificate(
            old.certificate_pem.encode()
        ).public_bytes(serialization.Encoding.DER)
        assert mtls.observation.client_certificate_der == expected
        assert request_count(mtls) == 1


def test_renew_unknown_replays_after_registered_generation_changes(tmp_path):
    with LoopbackAuthority([]) as bootstrap:
        c = connect_context(tmp_path, bootstrap)
        bootstrap.responders = [c.issue]
        w, _ = enroll_ready(c)
    with LoopbackAuthority([], ca=c.ca, max_sessions=2) as server:
        c.binding = replace(
            c.binding, config=replace(c.binding.config, origin=server.origin)
        )

        def lost(request):
            c.issue(request)
            return None

        server.responders = [lost, c.issue]
        old = w.read()
        h = stage(w, kind="renew", generation=old.credential_generation)
        with pytest.raises(TransportClientError):
            w.renew(h)
        assert c.current.credential_generation == old.credential_generation + 1
        assert w.read() == old
        w.renew(h)
        w.validate(h)
        w.commit(h)
        assert w.read().credential_generation == old.credential_generation + 1
        assert request_hash(server, 0) == request_hash(server, 1)


def test_retire_replay_uses_original_mtls_and_target_after_local_ready_changes(
    tmp_path,
):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from hyprial.identity import (
        commit_transport_credential,
        validate_enrollment_response,
    )
    from tests.identity.devices.test_transport_credential import _response, _stage

    with LoopbackAuthority([]) as bootstrap:
        c = connect_context(tmp_path, bootstrap)
        bootstrap.responders = [c.issue]
        w, _ = enroll_ready(c)
    old = w.read()
    with LoopbackAuthority(
        [], ca=c.ca, require_client_cert=True, max_sessions=2
    ) as server:
        c.binding = replace(
            c.binding, config=replace(c.binding.config, origin=server.origin)
        )

        def settled(request):
            body = json.loads(request.split(b"\r\n\r\n", 1)[1])
            return raw_response(
                success_envelope(
                    body["operationId"],
                    {
                        "operationId": body["operationId"],
                        "retired": True,
                        "remoteEnforcementState": "pending",
                    },
                )
            )

        server.responders = [lambda _: None, settled]
        h = w.prepare_retire(str(uuid4()))
        with pytest.raises(TransportClientError):
            w.retire_remote(h)
        # Adversarial external writer: the required production home lock prevents
        # this, but recovery must still never select a different target or TLS.
        replacement = _stage(
            tmp_path, device_uid=c.record.device_uid, owner=c.record.owner
        )
        response, bundle = _response(replacement)
        replacement_credential = validate_enrollment_response(
            response,
            staged=replacement,
            pinned_trust_bundle_pem=bundle,
            effective_owner=c.record.owner,
        )
        commit_transport_credential(
            tmp_path,
            staged=replacement,
            credential=replacement_credential,
            pinned_trust_bundle_pem=bundle,
        )
        assert w.read().spki_sha256 != old.spki_sha256
        w.retire_remote(h)
        assert request_hash(server, 0) == request_hash(server, 1)
        expected = x509.load_pem_x509_certificate(
            old.certificate_pem.encode()
        ).public_bytes(serialization.Encoding.DER)
        assert server.observation.client_certificate_der == expected
        assert w.read() == replacement_credential


@pytest.mark.parametrize(
    "invalid",
    [
        "not-a-uuid",
        "00000000-0000-1000-8000-000000000000",
        "550E8400-E29B-41D4-A716-446655440000",
        None,
        True,
    ],
)
def test_invalid_retire_id_never_owns_home_slot(tmp_path, invalid):
    with LoopbackAuthority([]) as server:
        c = connect_context(tmp_path, server)
        server.responders = [c.issue]
        w, _ = enroll_ready(c)
        before = inventory(tmp_path)
        caught = None
        try:
            w.prepare_retire(invalid)
        except TailnetCredentialError as error:
            caught = error
        assert caught is not None, "invalid retire ID created pending handle"
        assert inventory(tmp_path) == before
        valid = w.prepare_retire(str(uuid4()))
        assert w.operation_status(valid)["phase"] == "PREPARED"
