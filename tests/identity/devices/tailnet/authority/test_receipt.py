"""Real local transactions and exact operation outcomes."""

import json
from uuid import uuid4

import pytest

from hyprial.identity import TailnetCredentialError

from .support import LocalContext, body, call, prepare, journal, state, image_rows, child


def test_success_and_business_rejection_have_exact_durable_replay(tmp_path):
    context = LocalContext(tmp_path / "home")
    first = body(context)
    status, response = call(context, first)
    assert status == 200
    assert call(context, first) == (status, response)
    rejected = body(context)
    rejected_status, rejected_response = call(context, rejected)
    assert rejected_status == 409
    assert json.loads(rejected_response)["receipt"] is not None
    assert call(context, rejected) == (rejected_status, rejected_response)


def test_valid_integer_wrong_enrollment_generation_has_a_durable_business_receipt(tmp_path):
    context = LocalContext(tmp_path / "home")
    value = json.loads(body(context))
    value["expectedCredentialGeneration"] = 4
    raw = json.dumps(value).encode()
    status, response = call(context, raw)
    result = json.loads(response)
    assert status == 409
    assert result["error"]["code"] == "CREDENTIAL_GENERATION_STALE"
    assert result["receipt"] is not None
    assert call(context, raw) == (status, response)
    value["expectedCredentialGeneration"] = 0
    conflict_status, conflict_raw = call(context, json.dumps(value).encode())
    assert conflict_status == 409
    assert json.loads(conflict_raw)["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_revocation_after_sending_latch_keeps_unknown_in_public_error(tmp_path, monkeypatch):
    from dataclasses import replace
    from hyprial.identity.impl.device.tailnet.authority import service
    context = LocalContext(tmp_path / "home")
    context.provision()
    operation = prepare(context)

    def revoke(name):
        if name == "RESULT_STAGED":
            context.observations["tailnet-A"] = replace(context.membership("tailnet-A"), member_ready=False, code="MEMBER_REVOKED")

    monkeypatch.setattr(service, "_checkpoint", revoke)
    with pytest.raises(TailnetCredentialError) as caught:
        context.runtime().execute(operation["operationId"])
    assert caught.value.remote_outcome == "UNKNOWN"
    value, _ = journal(context, operation["operationId"])
    assert value["phase"] == "SENDING" and value["remoteOutcome"] == "UNKNOWN"


def test_bad_csr_business_refusal_cannot_later_become_success(tmp_path):
    context = LocalContext(tmp_path / "home")
    identifier = str(uuid4())
    raw = body(context, identifier=identifier, csr="not a CSR")
    status, response = call(context, raw)
    assert status == 400
    assert json.loads(response)["error"]["code"] == "CSR_INVALID"
    assert json.loads(response)["receipt"] is not None
    assert call(context, raw) == (status, response)
    changed = body(context, identifier=identifier)
    conflict_status, conflict_raw = call(context, changed)
    assert conflict_status == 409
    assert json.loads(conflict_raw)["receipt"] is None


@pytest.mark.parametrize("point", ["BEFORE_COMMIT", "BEFORE_PUBLISH"])
def test_first_enrollment_transaction_interruption_can_replay_the_original_request(tmp_path, monkeypatch, point):
    from hyprial.identity.impl.device.tailnet.authority import store
    context = LocalContext(tmp_path / "home")
    context.provision()
    operation = prepare(context)

    def interrupt(name):
        if name == point:
            raise KeyboardInterrupt("first mutation interrupted")

    monkeypatch.setattr(store, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        context.runtime().execute(operation["operationId"])
    monkeypatch.setattr(store, "_checkpoint", lambda name: None)
    recovered = context.runtime().recover(operation["operationId"])
    assert recovered["phase"] == "RECEIVED"
    assert recovered["remoteOutcome"] == "SUCCESS"


def test_same_operation_with_a_different_route_is_a_conflict(tmp_path):
    from hyprial.identity.impl.device.tailnet.authority.service import local
    context = LocalContext(tmp_path / "home")
    raw = body(context)
    original = call(context, raw)
    runtime = context.runtime()
    descriptor = context.provision()
    with runtime._slot.locked():
        status, response = local(runtime, "/v1/tailnet-credentials/renew", raw, descriptor)
    assert status == 409
    assert json.loads(response)["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert json.loads(response)["receipt"] is None
    assert call(context, raw) == original


@pytest.mark.parametrize("point", ["SENDING", "BEFORE_COMMIT", "BEFORE_PUBLISH", "AFTER_COMMIT", "BEFORE_RECEIVED", "RECEIVED"])
@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_local_mutation_interrupt_recovers_exact_receipt_and_atomic_image(tmp_path, monkeypatch, point, exception):
    from hyprial.identity.impl.device.tailnet.authority import store
    from hyprial.identity.impl.device.tailnet.runtime import workflow
    context = LocalContext(tmp_path / "home")
    assert call(context, body(context, csr="seed refusal"))[0] == 400
    operation = prepare(context)
    identifier = operation["operationId"]
    path = state(context.home) / "authority/store.sqlite3"
    original = path.read_bytes()
    interrupt = exception("local test interrupt")

    def checkpoint(name):
        if name == point:
            raise interrupt

    monkeypatch.setattr(store, "_checkpoint", checkpoint)
    monkeypatch.setattr(workflow, "_checkpoint", checkpoint)
    with pytest.raises(exception) as caught:
        context.runtime().execute(identifier)
    assert caught.value is interrupt
    committed = point in ("AFTER_COMMIT", "BEFORE_RECEIVED", "RECEIVED")
    assert len(image_rows(context, "SELECT * FROM current")) == int(committed)
    receipts = image_rows(context, "SELECT * FROM operations WHERE operation_id=?", (identifier,))
    assert len(receipts) == int(committed)
    if not committed:
        assert path.read_bytes() == original
    before_replay = receipts[0]["response"] if receipts else None
    value, _ = journal(context, identifier)
    assert value["remoteOutcome"] == ("SUCCESS" if point == "RECEIVED" else "UNKNOWN")
    monkeypatch.setattr(store, "_checkpoint", lambda name: None)
    monkeypatch.setattr(workflow, "_checkpoint", lambda name: None)
    assert context.runtime().recover(identifier)["phase"] == "RECEIVED"
    assert len(image_rows(context, "SELECT * FROM current")) == 1
    after = image_rows(context, "SELECT response FROM operations WHERE operation_id=?", (identifier,))
    assert len(after) == 1
    if before_replay is not None:
        assert after[0]["response"] == before_replay


@pytest.mark.parametrize("point", ["BEFORE_COMMIT", "BEFORE_PUBLISH", "AFTER_COMMIT"])
def test_business_refusal_is_committed_with_no_current_change_and_replays_exactly(tmp_path, monkeypatch, point):
    from hyprial.identity.impl.device.tailnet.authority import store
    context = LocalContext(tmp_path / "home")
    assert call(context, body(context, csr="seed refusal"))[0] == 400
    rejected = body(context, csr="not a CSR")
    identifier = json.loads(rejected)["operationId"]

    def interrupt(name):
        if name == point:
            raise KeyboardInterrupt

    monkeypatch.setattr(store, "_checkpoint", interrupt)
    with pytest.raises(KeyboardInterrupt):
        call(context, rejected)
    rows = image_rows(context, "SELECT * FROM operations WHERE operation_id=?", (identifier,))
    assert len(rows) == int(point == "AFTER_COMMIT")
    assert image_rows(context, "SELECT * FROM current") == []
    monkeypatch.setattr(store, "_checkpoint", lambda name: None)
    first = call(context, rejected)
    assert first[0] == 400 and json.loads(first[1])["receipt"] is not None
    assert call(context, rejected) == first


def test_publish_fsync_failure_retains_unknown_then_finds_the_same_committed_receipt(tmp_path, monkeypatch):
    import os
    import stat
    context = LocalContext(tmp_path / "home")
    call(context, body(context, csr="seed refusal"))
    operation = prepare(context)
    path = state(context.home) / "authority/store.sqlite3"
    before = path.read_bytes()
    fsync = os.fsync
    failed = []

    def interrupted_sync(descriptor):
        if not failed and stat.S_ISDIR(os.fstat(descriptor).st_mode) and path.read_bytes() != before:
            failed.append(True)
            raise OSError("private-io-sentinel")
        return fsync(descriptor)

    monkeypatch.setattr(os, "fsync", interrupted_sync)
    with pytest.raises(TailnetCredentialError) as caught:
        context.runtime().execute(operation["operationId"])
    assert caught.value.remote_outcome == "UNKNOWN"
    assert "private-io-sentinel" not in str(caught.value)
    assert failed == [True]
    assert len(image_rows(context, "SELECT * FROM current")) == 1
    rows = image_rows(context, "SELECT response FROM operations WHERE operation_id=?", (operation["operationId"],))
    assert len(rows) == 1
    monkeypatch.setattr(os, "fsync", fsync)
    assert context.runtime().recover(operation["operationId"])["phase"] == "RECEIVED"
    assert image_rows(context, "SELECT response FROM operations WHERE operation_id=?", (operation["operationId"],))[0]["response"] == rows[0]["response"]


def test_retired_binding_is_preserved_without_automatic_reregistration(tmp_path):
    from hyprial.identity.impl.device.tailnet.authority.store import Store
    context = LocalContext(tmp_path / "home")
    first_status, first_raw = call(context, body(context))
    assert first_status == 200
    original = json.loads(first_raw)["data"]["credential"]["bindingId"]
    runtime = context.runtime()
    descriptor = context.provision()
    with runtime._slot.locked():
        with Store(runtime._slot, descriptor.ca_sha256).transaction() as connection:
            connection.execute("UPDATE bindings SET state='retired'")
    status, raw = call(context, body(context))
    assert status == 403
    assert json.loads(raw)["error"]["code"] == "DEVICE_REVOKED"
    assert json.loads(raw)["receipt"] is not None
    rows = image_rows(context, "SELECT binding_id,state FROM bindings")
    assert rows == [{"binding_id": original, "state": "retired"}]


def test_new_process_recovers_exact_local_receipt_after_committed_response_was_lost(tmp_path, monkeypatch):
    from hyprial.identity.impl.device.tailnet.authority import store
    context = LocalContext(tmp_path / "home")
    context.provision()
    operation = prepare(context)
    identifier = operation["operationId"]

    def lost_reply(name):
        if name == "AFTER_COMMIT":
            raise KeyboardInterrupt

    monkeypatch.setattr(store, "_checkpoint", lost_reply)
    with pytest.raises(KeyboardInterrupt):
        context.runtime().execute(identifier)
    original = image_rows(context, "SELECT response FROM operations WHERE operation_id=?", (identifier,))[0]["response"]
    result = child('''
from pathlib import Path
import json, sys
from hyprial.identity import read_device_record
from tests.identity.devices.tailnet.authority.support import LocalContext
context = LocalContext.__new__(LocalContext)
context.home = Path(sys.argv[1])
context.record = read_device_record(context.home)
context.observations = {}
context.rows_override = None
print(json.dumps(context.runtime().recover(sys.argv[2])))
''', context.home, identifier)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["remoteOutcome"] == "SUCCESS"
    assert image_rows(context, "SELECT response FROM operations WHERE operation_id=?", (identifier,))[0]["response"] == original
    assert journal(context, identifier)[0]["phase"] == "RECEIVED"


@pytest.mark.parametrize("invalid_json", [[], None, 7])
@pytest.mark.parametrize("location", ["cached_journal", "authority_response"])
def test_non_object_response_json_stays_inside_typed_runtime_boundary(tmp_path, monkeypatch, invalid_json, location):
    import base64
    from hashlib import sha256
    import sqlite3
    from hyprial.identity.impl.device.tailnet.authority import store
    context = LocalContext(tmp_path / "home")
    context.provision()
    operation = prepare(context)
    identifier = operation["operationId"]
    malformed = json.dumps(invalid_json).encode()
    if location == "cached_journal":
        context.runtime().execute(identifier)
        path = state(context.home) / "journal" / (identifier + ".json")
        value = json.loads(path.read_bytes())
        value.update(responseBody=base64.b64encode(malformed).decode(), responseSha256=sha256(malformed).hexdigest())
        path.write_text(json.dumps(value))
        expected_code = "TAILNET_JOURNAL_INVALID"
    else:
        def interrupt(name):
            if name == "AFTER_COMMIT":
                raise KeyboardInterrupt
        monkeypatch.setattr(store, "_checkpoint", interrupt)
        with pytest.raises(KeyboardInterrupt):
            context.runtime().execute(identifier)
        monkeypatch.setattr(store, "_checkpoint", lambda name: None)
        path = state(context.home) / "authority/store.sqlite3"
        database = sqlite3.connect(":memory:")
        try:
            database.deserialize(path.read_bytes())
            database.execute("UPDATE operations SET response=? WHERE operation_id=?", (malformed, identifier))
            database.commit()
            path.write_bytes(database.serialize())
        finally:
            database.close()
        expected_code = "TAILNET_RESPONSE_INVALID"
    with pytest.raises(TailnetCredentialError) as caught:
        context.runtime().recover(identifier)
    assert caught.value.code == expected_code
    assert caught.value.__context__ is None


def test_codec_never_settles_503_as_definitive_rejection_even_with_receipt():
    from hyprial.identity.impl.device.tailnet.runtime.schema import receipt
    value = {"operationId": str(uuid4()), "route": "/v1/tailnet-credentials/enroll", "requestSha256": "a" * 64}
    raw = json.dumps({"version": "hyprial.tailnet.transport.v1", "ok": False,
                      "operationId": value["operationId"],
                      "receipt": {"route": value["route"], "requestSha256": value["requestSha256"]},
                      "error": {"code": "AUTHORITY_UNAVAILABLE", "retryable": True}}).encode()
    # This is a codec control; the implemented local handler does not emit
    # this combination and no remote authentication capability is implied.
    assert receipt(value, 503, raw) == "UNKNOWN"
