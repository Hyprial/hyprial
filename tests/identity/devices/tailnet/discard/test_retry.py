import json
import os
from pathlib import Path

import pytest

from hyprial.identity import (
    TailnetCredentialError,
    TransportClientError,
    TransportCredentialError,
)
from ..support import LoopbackAuthority, connect_context, request_count
from . import Context, body_hash, inventory, operation_authority, stage, targets


@pytest.mark.parametrize(
    "point", ["key-unlink", "key-fsync", "enrollment-unlink", "state-fsync"]
)
@pytest.mark.parametrize("timing", ["before", "after"])
def test_io_fault_keeps_slot_blocks_delegates_and_resumes(
    tmp_path, monkeypatch, point, timing
):
    with LoopbackAuthority([lambda _: None], accept_timeout=0.5) as server:
        c = connect_context(tmp_path, server)
        w = c.workflow()
        h = stage(w)
        key, enrollment = targets(tmp_path, w, h)
        inodes = {
            key.parent.stat().st_ino: "key-fsync",
            enrollment.parent.stat().st_ino: "state-fsync",
        }
        unlink, fsync = Path.unlink, os.fsync
        fired = []

        def execute(label, action):
            if label == point and not fired:
                fired.append(label)
                if timing == "after":
                    action()
                raise OSError("synthetic discard IO fault")
            return action()

        def wrapped_unlink(path, *args, **kwargs):
            label = (
                "key-unlink"
                if path == key
                else "enrollment-unlink"
                if path == enrollment
                else "other"
            )
            return execute(label, lambda: unlink(path, *args, **kwargs))

        def wrapped_fsync(fd):
            label = inodes.get(os.fstat(fd).st_ino, "other")
            return execute(label, lambda: fsync(fd))

        monkeypatch.setattr(Path, "unlink", wrapped_unlink)
        monkeypatch.setattr(os, "fsync", wrapped_fsync)
        error = None
        try:
            w.discard_staged(h, confirmed=True)
        except (OSError, TransportCredentialError) as caught:
            error = caught
        assert error is not None, "required actual IO boundary was not executed"
        assert fired == [point]
        calls = len(c.calls)
        errors = []
        for mutate in (w.enroll, w.renew, w.validate, w.commit, w.retire_remote):
            try:
                mutate(h)
            except (
                TailnetCredentialError,
                TransportClientError,
                TransportCredentialError,
            ) as caught:
                errors.append(caught)
        assert request_count(server) == 0, "discard-started operation sent cached CSR"
        assert len(c.calls) == calls, "discard-started mutation reached trusted ports"
        assert len(errors) == 5
        assert all(isinstance(e, TailnetCredentialError) for e in errors)
        status = w.operation_status(h)
        assert status["phase"] == "DISCARD_PENDING"
        assert status["remoteOutcome"] == "NOT_SENT"
        with pytest.raises(TailnetCredentialError):
            stage(w)
        monkeypatch.undo()
        receipt = w.discard_staged(h, confirmed=True)
        assert receipt["phase"] == "DISCARDED"
        assert key.exists() is False
        assert enrollment.exists() is False
        stage(w)


def test_success_executes_key_unlink_sync_then_enrollment_unlink_sync(
    tmp_path, monkeypatch
):
    w = Context(tmp_path).workflow()
    h = stage(w)
    key, enrollment = targets(tmp_path, w, h)
    inodes = {
        key.parent.stat().st_ino: "key-fsync",
        enrollment.parent.stat().st_ino: "state-fsync",
    }
    unlink, fsync = Path.unlink, os.fsync
    events = []

    def tracked_unlink(path, *args, **kwargs):
        events.append("key-unlink" if path == key else "enrollment-unlink")
        return unlink(path, *args, **kwargs)

    def tracked_fsync(fd):
        events.append(inodes.get(os.fstat(fd).st_ino, "other-fsync"))
        return fsync(fd)

    monkeypatch.setattr(Path, "unlink", tracked_unlink)
    monkeypatch.setattr(os, "fsync", tracked_fsync)
    w.discard_staged(h, confirmed=True)
    assert events == ["key-unlink", "key-fsync", "enrollment-unlink", "state-fsync"]


@pytest.mark.parametrize("target", ["key", "enrollment"])
def test_partial_discard_rejects_replacement_even_with_identical_content(
    tmp_path, monkeypatch, target
):
    w = Context(tmp_path).workflow()
    h = stage(w)
    key, enrollment = targets(tmp_path, w, h)
    chosen = key if target == "key" else enrollment
    unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        if path == key:
            raise OSError("synthetic pre-unlink fault")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", interrupted)
    with pytest.raises((OSError, TransportCredentialError)):
        w.discard_staged(h, confirmed=True)
    monkeypatch.undo()
    replacement = chosen.with_name("replacement")
    replacement.write_bytes(chosen.read_bytes())
    replacement.chmod(0o600)
    replacement.replace(chosen)
    before = inventory(tmp_path)
    error = None
    try:
        w.discard_staged(h, confirmed=True)
    except (TailnetCredentialError, TransportCredentialError) as caught:
        error = caught
    assert inventory(tmp_path) == before, "replacement matching bytes was deleted"
    assert error is not None
    assert w.operation_status(h)["phase"] == "DISCARD_PENDING"


def test_terminal_receipt_precedes_all_file_access_and_protects_new_operation(
    tmp_path, monkeypatch
):
    w = Context(tmp_path).workflow()
    h = stage(w)
    receipt = w.discard_staged(h, confirmed=True)
    expected = dict(receipt)
    receipt.clear()
    new = stage(w)
    before = inventory(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("terminal replay accessed files")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", forbidden)
        patch.setattr(os, "lstat", forbidden)
        patch.setattr(os, "open", forbidden)
        assert w.discard_staged(h, confirmed=True) == expected
        repeated = w.discard_staged(h)
        assert repeated == expected
        for mutate in (w.enroll, w.renew, w.validate, w.commit, w.retire_remote):
            with pytest.raises(TailnetCredentialError):
                mutate(h)
    assert inventory(tmp_path) == before
    assert w.operation_status(new)["phase"] == "PREPARED"
    assert "-----BEGIN" not in json.dumps(w.operation_status(h))


@pytest.mark.parametrize("route", ["enroll", "renew", "retire"])
@pytest.mark.parametrize("interrupt_type", [KeyboardInterrupt, SystemExit])
def test_post_send_interrupt_preserves_unknown_and_material_then_exactly_recovers(
    tmp_path, monkeypatch, route, interrupt_type
):
    from hyprial.identity.impl.device.client import http

    with operation_authority(tmp_path, route) as (c, w, h, send, server, mutations):
        original_client = w._operations[h].client
        original_binding = w._operations[h].binding
        marker = interrupt_type()
        original_decode = http._decode_response

        def interrupted_decode(*args, **kwargs):
            original_decode(*args, **kwargs)
            raise marker

        propagated = None
        with monkeypatch.context() as patch:
            patch.setattr(http, "_decode_response", interrupted_decode)
            try:
                send(h)
            except interrupt_type as error:
                propagated = error
        same_interrupt = propagated is marker
        assert same_interrupt is True
        assert request_count(server) == 1
        assert len(mutations) == 1
        assert bool(server.observation.client_certificate_der) is (route != "enroll")
        before = inventory(tmp_path)
        denied = None
        try:
            w.discard_staged(h, confirmed=True)
        except TailnetCredentialError as error:
            denied = error
        assert inventory(tmp_path) == before, (
            "post-send interrupt permitted actual material deletion"
        )
        assert denied is not None
        assert w.operation_status(h)["remoteOutcome"] == "UNKNOWN"
        with pytest.raises(TailnetCredentialError):
            stage(w)
        result = send(h)
        assert request_count(server) == 2
        assert body_hash(server, 0) == body_hash(server, 1)
        assert len(mutations) == 1
        same_client = w._operations[h].client is original_client
        same_binding = w._operations[h].binding is original_binding
        assert same_client is True
        assert same_binding is True
        assert w.operation_status(h)["remoteOutcome"] == "SUCCESS"
        if route == "retire":
            assert result["remoteEnforcementState"] == "pending"
            assert inventory(tmp_path) == before
        else:
            w.validate(h)
            w.commit(h)
            assert w.operation_status(h)["phase"] == "COMMITTED"
            assert w.read().spki_sha256 == c.current.spki_sha256


def test_first_definitive_connect_not_sent_still_allows_discard(tmp_path):
    import socket

    with socket.socket() as unlistening:
        unlistening.bind(("127.0.0.1", 0))
        c = Context(
            tmp_path, origin=f"https://127.0.0.1:{unlistening.getsockname()[1]}"
        )
        w = c.workflow()
        h = stage(w)
        with pytest.raises(TransportClientError) as caught:
            w.enroll(h)
        assert caught.value.outcome == "NOT_SENT"
        assert w.operation_status(h)["remoteOutcome"] == "NOT_SENT"
        assert w.discard_staged(h, confirmed=True)["phase"] == "DISCARDED"


def test_first_definitive_handshake_not_sent_still_allows_discard(tmp_path):
    from tests.identity.devices.binding.support import make_ca

    with LoopbackAuthority(
        [], server_ca=make_ca("other test CA"), accept_timeout=0.5
    ) as server:
        c = connect_context(tmp_path, server)
        w = c.workflow()
        h = stage(w)
        with pytest.raises(TransportClientError) as caught:
            w.enroll(h)
        assert caught.value.code == "TRANSPORT_CHAIN_UNTRUSTED"
        assert caught.value.outcome == "NOT_SENT"
        assert request_count(server) == 0
        assert w.operation_status(h)["remoteOutcome"] == "NOT_SENT"
        assert w.discard_staged(h, confirmed=True)["phase"] == "DISCARDED"
