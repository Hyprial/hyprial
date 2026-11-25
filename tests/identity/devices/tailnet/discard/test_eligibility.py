from uuid import uuid4
from dataclasses import replace

import pytest

from hyprial.identity import TailnetCredentialError, TransportClientError
from ..support import LoopbackAuthority, connect_context, enroll_ready, request_count

from . import Context, inventory, reject, stage, targets


def test_never_sent_discard_is_offline_and_releases_slot(tmp_path):
    c = Context(tmp_path)
    w = c.workflow()
    h = stage(w)
    key, enrollment = targets(tmp_path, w, h)
    sibling = key.parent.parent / str(uuid4())
    sibling.mkdir(mode=0o700)
    (sibling / "keep").write_text("sibling")
    (key.parent / "keep").write_text("operation sibling")
    (tmp_path / "tailcat").write_text("tailcat")
    before = inventory(tmp_path)
    calls = len(c.calls)
    snapshot = c.snapshot
    c.snapshot = None
    c.lookup_error = True
    receipt = w.discard_staged(h, confirmed=True)
    remaining = inventory(tmp_path)
    removed = set(before) - set(remaining)
    assert removed == {
        str(key.relative_to(tmp_path)),
        str(enrollment.relative_to(tmp_path)),
    }
    import stat

    assert all(
        remaining[p] == before[p] for p in remaining if not stat.S_ISDIR(before[p][0])
    )
    assert len(c.calls) == calls
    assert receipt == {
        "operationId": w.operation_status(h)["operationId"],
        "phase": "DISCARDED",
    }
    c.snapshot = snapshot
    c.lookup_error = False
    stage(w)


@pytest.mark.parametrize("confirmed", [False, None, 1, "yes"])
def test_confirmation_is_strict_intent_before_any_deletion(tmp_path, confirmed):
    w = Context(tmp_path).workflow()
    h = stage(w)
    before = inventory(tmp_path)
    error = None
    try:
        w.discard_staged(h, confirmed=confirmed)
    except TailnetCredentialError as caught:
        error = caught
    assert inventory(tmp_path) == before, "unconfirmed discard deleted staging"
    assert error is not None


@pytest.mark.parametrize("rejected", [False, True])
def test_adopted_staging_never_inherits_discard_proof(tmp_path, rejected):
    with LoopbackAuthority([reject], accept_timeout=0.5) as server:
        c = connect_context(tmp_path, server)
        old = c.workflow()
        h = stage(old)
        opid = old.operation_status(h)["operationId"]
        new = c.workflow()
        adopted = new.stage(opid, 0)
        if rejected:
            with pytest.raises(TransportClientError):
                new.enroll(adopted)
        before = inventory(tmp_path)
        error = None
        try:
            new.discard_staged(adopted, confirmed=True)
        except TailnetCredentialError as caught:
            error = caught
        assert inventory(tmp_path) == before, (
            "adopted operation deleted original material"
        )
        assert error is not None
        assert request_count(server) == int(rejected)


@pytest.mark.parametrize(
    "state", ["unknown", "unknown-not-sent", "success", "key-installed", "committed"]
)
def test_remote_pending_or_installed_is_never_discardable(tmp_path, state, monkeypatch):
    from hyprial.identity.impl.device import transport_credential as storage

    with LoopbackAuthority([], accept_timeout=0.5) as server:
        c = connect_context(tmp_path, server)
        server.responders = (
            [lambda _: None] if state.startswith("unknown") else [c.issue]
        )
        w = c.workflow()
        h = stage(w)
        if state.startswith("unknown"):
            with pytest.raises(TransportClientError):
                w.enroll(h)
        else:
            w.enroll(h)
            if state in ("key-installed", "committed"):
                w.validate(h)
                if state == "key-installed":

                    def stop(checkpoint):
                        if checkpoint == "key-installed":
                            raise OSError("synthetic interruption")

                    monkeypatch.setattr(storage, "_commit_checkpoint", stop)
                    with pytest.raises(OSError):
                        w.commit(h)
                else:
                    w.commit(h)
    if state == "unknown-not-sent":
        with pytest.raises(TailnetCredentialError):
            w.enroll(h)
    before = inventory(tmp_path)
    error = None
    try:
        w.discard_staged(h, confirmed=True)
    except (TailnetCredentialError, FileNotFoundError) as caught:
        error = caught
    assert inventory(tmp_path) == before, (
        "remote pending/installed operation lost material"
    )
    assert isinstance(error, TailnetCredentialError)
    assert w.operation_status(h)["remoteOutcome"] == (
        "UNKNOWN" if state.startswith("unknown") else "SUCCESS"
    )


def test_original_rejected_renewal_preserves_ready_files_and_allows_new_operation(
    tmp_path,
):
    with LoopbackAuthority([], accept_timeout=0.5) as bootstrap:
        c = connect_context(tmp_path, bootstrap)
        bootstrap.responders = [c.issue]
        w, _ = enroll_ready(c)
    with LoopbackAuthority(
        [reject], ca=c.ca, require_client_cert=True, accept_timeout=0.5
    ) as server:
        c.binding = replace(
            c.binding, config=replace(c.binding.config, origin=server.origin)
        )
        h = stage(w, kind="renew", generation=1)
        with pytest.raises(TransportClientError):
            w.renew(h)
        before = inventory(tmp_path)
        calls = len(c.calls)
        c.snapshot, snapshot = None, c.snapshot
        c.lookup_error = True
        w.discard_staged(h, confirmed=True)
        after = inventory(tmp_path)
        protected = [
            p
            for p in before
            if p.endswith(("device-key.pem", "credential.json", "trust-bundle.pem"))
        ]
        assert len(protected) == 3
        assert all(before[p] == after[p] for p in protected)
        assert len(c.calls) == calls
        assert request_count(server) == 1
        c.snapshot = snapshot
        c.lookup_error = False
        stage(w, kind="renew", generation=1)


def test_foreign_fabricated_and_retirement_handles_have_no_discard_authority(tmp_path):
    from hyprial.identity import TailnetCredentialOperation

    with LoopbackAuthority([], accept_timeout=0.5) as bootstrap:
        c = connect_context(tmp_path, bootstrap)
        bootstrap.responders = [c.issue]
        w, committed = enroll_ready(c)
    retired = w.prepare_retire(str(uuid4()))
    before = inventory(tmp_path)
    for handle in (TailnetCredentialOperation(), object(), [], retired):
        error = None
        try:
            w.discard_staged(handle, confirmed=True)
        except (TailnetCredentialError, AttributeError) as caught:
            error = caught
        assert inventory(tmp_path) == before
        assert isinstance(error, TailnetCredentialError)
    with pytest.raises(TailnetCredentialError):
        c.workflow().discard_staged(committed, confirmed=True)


def test_adopted_rejection_under_new_https_configuration_does_not_settle_old_operation(
    tmp_path,
):
    c = Context(tmp_path)
    original = c.workflow()
    h = stage(original)
    opid = original.operation_status(h)["operationId"]
    with LoopbackAuthority([reject], ca=c.ca, accept_timeout=0.5) as server:
        c.binding = replace(
            c.binding, config=replace(c.binding.config, origin=server.origin)
        )
        adopted = c.workflow()
        new_handle = adopted.stage(opid, 0)
        with pytest.raises(TransportClientError):
            adopted.enroll(new_handle)
        before = inventory(tmp_path)
        with pytest.raises(TailnetCredentialError):
            adopted.discard_staged(new_handle, confirmed=True)
        assert inventory(tmp_path) == before
        assert original.operation_status(h)["remoteOutcome"] == "NOT_SENT"
        assert request_count(server) == 1


@pytest.mark.parametrize("state", ["unknown", "success", "key-installed"])
def test_discard_refusal_keeps_original_operation_recoverable(
    tmp_path, monkeypatch, state
):
    from hyprial.identity.impl.device import transport_credential as storage

    with LoopbackAuthority([], max_sessions=2, accept_timeout=0.5) as server:
        c = connect_context(tmp_path, server)

        def lost(request):
            c.issue(request)
            return None

        server.responders = [lost, c.issue] if state == "unknown" else [c.issue]
        w = c.workflow()
        h = stage(w)
        if state == "unknown":
            with pytest.raises(TransportClientError):
                w.enroll(h)
        else:
            w.enroll(h)
            w.validate(h)
            if state == "key-installed":

                def stop(checkpoint):
                    if checkpoint == "key-installed":
                        raise OSError("synthetic interruption")

                monkeypatch.setattr(storage, "_commit_checkpoint", stop)
                with pytest.raises(OSError):
                    w.commit(h)
                monkeypatch.setattr(storage, "_commit_checkpoint", lambda _: None)
        before = inventory(tmp_path)
        with pytest.raises(TailnetCredentialError):
            w.discard_staged(h, confirmed=True)
        assert inventory(tmp_path) == before
        if state == "unknown":
            w.enroll(h)
            w.validate(h)
        w.commit(h)
        assert w.operation_status(h)["phase"] == "COMMITTED"
        assert w.read().spki_sha256 == c.current.spki_sha256
        assert request_count(server) == (2 if state == "unknown" else 1)
