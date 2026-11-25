"""Actual durable preparation, crash boundaries and cross-process observation."""

import base64
from dataclasses import replace
import json
import os
import shutil
import subprocess
import time
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import serialization
import pytest

from hyprial.identity import TailnetCredentialError, write_device_record

from .support import Context, child, inventory, prepare, secret, state


def test_prepared_operation_survives_a_new_facade_with_exact_key_and_csr(tmp_path):
    context = Context(tmp_path / "home")
    try:
        context.provision()
        identifier = str(uuid4())
        result = prepare(context, identifier)
        assert result == {"operationId": identifier, "kind": "enroll", "phase": "PREPARED", "remoteOutcome": "NOT_SENT"}
        path = state(context.home) / "journal" / (identifier + ".json")
        journal = json.loads(path.read_bytes())
        request = json.loads(base64.b64decode(journal["request"]))
        assert set(request) == {"version", "tailnetId", "operationId", "deviceUid", "expectedCredentialGeneration", "csrPem"}
        assert request["deviceUid"] == context.record.device_uid
        csr = x509.load_pem_x509_csr(request["csrPem"].encode())
        assert len(csr.subject) == len(csr.extensions) == 0
        assert csr.is_signature_valid
        key_path = secret(context.home) / "staged" / identifier / "key.pem"
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        assert key.key_size == 2048
        assert key.public_key().public_numbers() == csr.public_key().public_numbers()
        before = inventory(context.home)
        assert prepare(context, identifier) == result
        assert context.facade().inspect_journal(tailnet_id="tailnet-A", operation_id=identifier) == result
        assert inventory(context.home) == before
        assert len(context.targets) == 1  # Bootstrap only, no mutation dial.
        assert path.stat().st_mode & 0o777 == 0o600
        assert key_path.stat().st_mode & 0o777 == 0o600
    finally:
        context.close()


@pytest.mark.parametrize("field,value", [("route", "/different"), ("expectedGeneration", False)])
def test_reconstruction_rejects_changed_frozen_operation_fields(tmp_path, field, value):
    context = Context(tmp_path / "home")
    try:
        context.provision()
        result = prepare(context)
        path = state(context.home) / "journal" / (result["operationId"] + ".json")
        journal = json.loads(path.read_bytes())
        journal[field] = value
        path.write_text(json.dumps(journal))
        with pytest.raises(TailnetCredentialError) as caught:
            context.facade().inspect_journal(tailnet_id="tailnet-A", operation_id=result["operationId"])
        assert caught.value.code == "TAILNET_JOURNAL_INVALID"
    finally:
        context.close()


def test_identical_key_bytes_in_a_replacement_inode_are_not_original_staging(tmp_path):
    context = Context(tmp_path / "home")
    try:
        context.provision()
        result = prepare(context)
        key_path = secret(context.home) / "staged" / result["operationId"] / "key.pem"
        replacement = key_path.with_suffix(".replacement")
        replacement.write_bytes(key_path.read_bytes())
        replacement.chmod(0o600)
        os.replace(replacement, key_path)
        with pytest.raises(TailnetCredentialError) as caught:
            context.facade().inspect_journal(tailnet_id="tailnet-A", operation_id=result["operationId"])
        assert caught.value.code == "TAILNET_JOURNAL_INVALID"
    finally:
        context.close()


def test_copying_a_journal_to_another_home_cannot_recover_it(tmp_path):
    context = Context(tmp_path / "home")
    try:
        context.provision()
        result = prepare(context)
        clone = tmp_path / "clone"
        shutil.copytree(context.home, clone)
        other = context.facade().__class__(clone, membership=context, dial=context.dial)
        before = inventory(clone)
        with pytest.raises(TailnetCredentialError) as caught:
            other.inspect_journal(tailnet_id="tailnet-A", operation_id=result["operationId"])
        assert caught.value.code == "TAILNET_JOURNAL_INVALID"
        assert inventory(clone) == before
    finally:
        context.close()


@pytest.mark.parametrize("point", ["PREPARING", "KEY_DURABLE", "BEFORE_PREPARED", "PREPARED"])
@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_interrupt_preserves_durable_preparation_and_does_not_claim_mutation(tmp_path, monkeypatch, point, exception):
    from hyprial.identity.impl.device.tailnet.runtime import workflow

    context = Context(tmp_path / "home")
    try:
        context.provision()
        identifier = str(uuid4())
        interrupt = exception("test interrupt")

        def checkpoint(name):
            if name == point:
                raise interrupt

        monkeypatch.setattr(workflow, "_checkpoint", checkpoint)
        with pytest.raises(exception) as caught:
            prepare(context, identifier)
        assert caught.value is interrupt
        observed = context.facade().inspect_journal(tailnet_id="tailnet-A", operation_id=identifier)
        assert observed["remoteOutcome"] == "NOT_SENT"
        assert observed["phase"] == ("PREPARED" if point == "PREPARED" else "PREPARING")
        monkeypatch.setattr(workflow, "_checkpoint", lambda name: None)
        if point in ("KEY_DURABLE", "BEFORE_PREPARED"):
            before = inventory(context.home)
            with pytest.raises(TailnetCredentialError) as refused:
                prepare(context, identifier)
            assert refused.value.code == "TAILNET_STAGING_UNPROVEN"
            assert inventory(context.home) == before
        else:
            assert prepare(context, identifier)["phase"] == "PREPARED"
        assert len(context.targets) == 1
    finally:
        context.close()


def test_unfinished_other_operation_blocks_a_second_mutation_without_new_staging(tmp_path):
    context = Context(tmp_path / "home")
    try:
        context.provision()
        prepare(context)
        before = inventory(context.home)
        with pytest.raises(TailnetCredentialError) as caught:
            prepare(context)
        assert caught.value.code == "TAILNET_OPERATION_PENDING"
        assert inventory(context.home) == before
    finally:
        context.close()


def test_changed_local_device_tuple_is_not_rebound_by_matching_uid(tmp_path):
    context = Context(tmp_path / "home")
    try:
        context.provision()
        result = prepare(context)
        write_device_record(context.home, replace(context.record, device_id="renamed-device"))
        with pytest.raises(TailnetCredentialError) as caught:
            context.facade().inspect_journal(tailnet_id="tailnet-A", operation_id=result["operationId"])
        assert caught.value.code == "TAILNET_LOCAL_DEVICE_MISMATCH"
    finally:
        context.close()


def test_a_second_process_reads_the_same_durable_public_observation(tmp_path):
    context = Context(tmp_path / "home")
    try:
        context.provision()
        result = prepare(context)
        completed = child('''
import json, sys
from hyprial.identity import TailnetCredentials
class Offline:
    def membership(self, tailnet_id): raise AssertionError("must not read stale network state")
    def members(self, tailnet_id): raise AssertionError("must not read stale network state")
def no_dial(target): raise AssertionError("must not dial")
facade = TailnetCredentials(sys.argv[1], membership=Offline(), dial=no_dial)
print(json.dumps(facade.inspect_journal(tailnet_id="tailnet-A", operation_id=sys.argv[2])))
''', context.home, result["operationId"])
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout) == result
    finally:
        context.close()


def test_two_tailnets_prepare_without_using_the_legacy_global_slot(tmp_path, monkeypatch):
    from hyprial.identity.impl.device import transport_credential as legacy

    context = Context(tmp_path / "home")
    global_path = context.home / "state/transport/credential.json"
    global_path.parent.mkdir(mode=0o700)
    global_path.write_bytes(b"old-global-ready-sentinel")
    global_path.chmod(0o600)

    def forbidden(*args, **kwargs):
        raise AssertionError("v2 called the v1 global slot")

    for name in ("read_transport_credential", "stage_transport_credential", "retire_transport_credential"):
        monkeypatch.setattr(legacy, name, forbidden)
    try:
        context.provision("tailnet-A")
        context.provision("tailnet-B")
        a = prepare(context, tailnet_id="tailnet-A")
        before_a = inventory(state(context.home, "tailnet-A"))
        b = prepare(context, tailnet_id="tailnet-B")
        assert a["operationId"] != b["operationId"]
        assert inventory(state(context.home, "tailnet-A")) == before_a
        assert context.runtime("tailnet-A").read() is None
        assert global_path.read_bytes() == b"old-global-ready-sentinel"
        assert state(context.home, "tailnet-A") != state(context.home, "tailnet-B")
    finally:
        context.close()


def test_preparing_record_cannot_pretend_it_already_has_frozen_request_material(tmp_path, monkeypatch):
    from hyprial.identity.impl.device.tailnet.runtime import workflow

    context = Context(tmp_path / "home")
    try:
        context.provision()
        identifier = str(uuid4())

        def stop(name):
            if name == "PREPARING":
                raise KeyboardInterrupt

        monkeypatch.setattr(workflow, "_checkpoint", stop)
        with pytest.raises(KeyboardInterrupt):
            prepare(context, identifier)
        path = state(context.home) / "journal" / (identifier + ".json")
        journal = json.loads(path.read_bytes())
        journal["request"] = "not-an-unsent-preparation"
        path.write_text(json.dumps(journal))
        with pytest.raises(TailnetCredentialError) as caught:
            context.facade().inspect_journal(tailnet_id="tailnet-A", operation_id=identifier)
        assert caught.value.code == "TAILNET_JOURNAL_INVALID"
    finally:
        context.close()


def test_staging_write_file_not_found_is_typed_and_path_free_and_keeps_preparing(tmp_path, monkeypatch):
    context = Context(tmp_path / "home")
    try:
        context.provision()
        identifier = str(uuid4())
        publish_file = os.link

        def failed(source, destination, **kwargs):
            if os.path.basename(destination) == "key.pem":
                raise FileNotFoundError("private-path-sentinel")
            return publish_file(source, destination, **kwargs)

        monkeypatch.setattr(os, "link", failed)
        with pytest.raises(TailnetCredentialError) as caught:
            prepare(context, identifier)
        assert "private-path-sentinel" not in str(caught.value)
        assert caught.value.__context__ is None
        observed = context.facade().inspect_journal(tailnet_id="tailnet-A", operation_id=identifier)
        assert observed["phase"] == "PREPARING"
        assert observed["remoteOutcome"] == "NOT_SENT"
    finally:
        context.close()


def test_same_slot_waits_for_os_lock_while_another_tailnet_progresses(tmp_path):
    from hyprial.kernel import lock_exclusive, unlock

    context = Context(tmp_path / "home")
    context.runtime().status()
    lock_path = state(context.home) / "operation.lock"
    inode = lock_path.stat().st_ino
    descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
    marker = tmp_path / "worker-started"
    code = '''
from pathlib import Path
import json, sys
from hyprial.identity import TailnetCredentials
class Offline:
    def membership(self, tailnet_id): raise AssertionError("unexpected network read")
    def members(self, tailnet_id): raise AssertionError("unexpected network read")
def no_dial(target): raise AssertionError("unexpected network dial")
facade = TailnetCredentials(sys.argv[1], membership=Offline(), dial=no_dial)
Path(sys.argv[3]).write_text("started")
print(json.dumps(facade.runtime(tailnet_id=sys.argv[2]).status()), flush=True)
'''
    process = None
    try:
        lock_exclusive(descriptor)
        process = subprocess.Popen(["uv", "run", "--no-sync", "python", "-c", code,
                                    str(context.home), "tailnet-A", str(marker)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.01)
        assert marker.exists(), "child failed to reach the slot boundary"
        time.sleep(0.05)
        assert process.poll() is None, "same-slot operation bypassed the OS lock"
        other = child(code, context.home, "tailnet-B", tmp_path / "other-started")
        assert other.returncode == 0, other.stderr
        assert json.loads(other.stdout)["tailnetId"] == "tailnet-B"
        assert process.poll() is None
    finally:
        unlock(descriptor)
        os.close(descriptor)
        if process is not None:
            try:
                stdout, stderr = process.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
    assert process.returncode == 0, stderr
    assert json.loads(stdout)["tailnetId"] == "tailnet-A"
    assert lock_path.stat().st_ino == inode


@pytest.mark.parametrize("point", ["KEY_DURABLE", "BEFORE_PREPARED"])
def test_prepare_does_not_claim_a_key_replaced_after_its_creation(tmp_path, monkeypatch, point):
    from hyprial.identity.impl.device.tailnet.runtime import workflow
    context = Context(tmp_path / "home")
    try:
        context.provision()
        identifier = str(uuid4())
        path = secret(context.home) / "staged" / identifier / "key.pem"

        def replace_at_checkpoint(name):
            if name == point:
                alternate = path.with_suffix(".alternate")
                alternate.write_bytes(path.read_bytes())
                alternate.chmod(0o600)
                os.replace(alternate, path)

        monkeypatch.setattr(workflow, "_checkpoint", replace_at_checkpoint)
        with pytest.raises(TailnetCredentialError) as caught:
            prepare(context, identifier)
        assert caught.value.code == "TAILNET_STAGING_UNPROVEN"
        journal = json.loads((state(context.home) / "journal" / (identifier + ".json")).read_bytes())
        assert journal["phase"] == "PREPARING"
        assert journal["remoteOutcome"] == "NOT_SENT"
        assert journal["staging"] is None
    finally:
        context.close()


def test_exclusive_staging_publication_does_not_clobber_a_racing_file(tmp_path, monkeypatch):
    context = Context(tmp_path / "home")
    try:
        context.provision()
        identifier = str(uuid4())
        key_path = secret(context.home) / "staged" / identifier / "key.pem"
        real_open = os.open
        raced = []

        def open_with_competitor(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if (os.path.basename(path).startswith(".key.pem.") and os.fspath(path).endswith(".tmp")
                    and flags & os.O_EXCL and key_path.parent.exists()
                    and os.path.dirname(path) == str(key_path.parent)):
                competitor = real_open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(competitor, b"competing-staging-sentinel")
                    os.fsync(competitor)
                finally:
                    os.close(competitor)
                raced.append(True)
            return descriptor

        monkeypatch.setattr(os, "open", open_with_competitor)
        with pytest.raises(TailnetCredentialError) as caught:
            prepare(context, identifier)
        assert caught.value.code == "TAILNET_STAGING_CONFLICT"
        assert raced == [True]
        assert key_path.read_bytes() == b"competing-staging-sentinel"
        assert json.loads((state(context.home) / "journal" / (identifier + ".json")).read_bytes())["phase"] == "PREPARING"
    finally:
        context.close()
