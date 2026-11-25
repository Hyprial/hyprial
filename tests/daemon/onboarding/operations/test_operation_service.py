"""Durable onboarding operation receipts across real files.

Decision: gui-m2-operation-receipts-20261105.  These tests use only temporary
homes and self-owned child processes; they never touch the production daemon,
credentials, user configuration or another agent's identity.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import hyprial.daemon.impl.onboarding.operations.journal as journal_module
from hyprial.daemon.impl.onboarding.operations import (
    OPERATION_STORE_COMMIT_UNCONFIRMED,
    OPERATION_STORE_CORRUPT,
    OPERATION_STORE_INSECURE,
    OPERATION_STORE_WRITE_FAILED,
    OperationReceiptError,
    OperationService,
    RECOVERY_PROOF_REQUIRED,
    RECOVERY_SCOPE_MISMATCH,
    RecoveryAuthority,
)
from hyprial.daemon.impl.request_catalog.onboarding import (
    AUTH_OPERATION_CONFLICT,
    AUTH_OPERATION_NOT_FOUND,
    CallContext,
    OnboardingStateError,
    Surface,
)
from hyprial.kernel import (
    apply_private_umask,
    ipc_errors,
    posix_metadata_enforced,
    process_birth_identity,
)

OWNER = "user:alice@example.test"
OTHER_OWNER = "user:bob@example.test"
KEY = "opkey-0123456789abcdef"
KEY2 = "opkey-fedcba9876543210"
OP = "op-0123456789abcdef"
OP2 = "op-1122334455667788"
RCPT = "rcpt-0123456789abcdef"
DEVICE_KEY = "opkey-device-0001"
DEVICE_OP = "op-device-0001"
DEVICE_RCPT = "rcpt-device-0001"
INITIALIZE_KEY = "opkey-initialize-0001"
INITIALIZE_OP = "op-initialize-0001"
INITIALIZE_RCPT = "rcpt-initialize-0001"
RECORD_FIELDS = {
    "operationId",
    "operationKey",
    "kind",
    "state",
    "surface",
    "receiptId",
    "accountOwner",
    "cancelable",
    "writerPid",
    "writerBirth",
    "controllerPid",
    "controllerBirth",
}
_FIFO_CHILD = """
import sys
from pathlib import Path
import hyprial.daemon.impl.onboarding.operations.journal as journal

root = Path(sys.argv[1])
journal._precheck_journal = lambda path: None
try:
    journal.read_journal(root)
except journal.OperationReceiptError as error:
    print(error.code, flush=True)
    sys.exit(0 if error.code == journal.OPERATION_STORE_INSECURE else 2)
print("opened", flush=True)
sys.exit(3)
"""


def context(
    generation: str = "gen-1",
    owner: str | None = OWNER,
    *,
    home: str = "home-1",
    host: str = "host-1",
    view: str = "view-1",
    phase: str = "bootstrap",
    caller: str = "local-bootstrap",
) -> CallContext:
    return CallContext(
        surface=Surface(
            home_binding=home,
            host_control_session_id=host,
            view_session_id=view,
            transport_generation=generation,
        ),
        phase=phase,
        caller=caller,
        owner=owner,
    )


def journal_file(root) -> str:
    return root / "operations.json"


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


def test_accepted_and_claimed_initialize_are_durable_before_promotion(tmp_path, monkeypatch) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    observed: list[tuple[str, str]] = []
    transitions: list[str] = []
    write = journal_module.write_journal

    def observe_write(*args, **kwargs):
        result = write(*args, **kwargs)
        transitions.append(json.loads(journal_file(root).read_text())["records"][0]["state"])
        return result

    monkeypatch.setattr(journal_module, "write_journal", observe_write)

    def promotion(record) -> None:
        persisted = json.loads(journal_file(root).read_text())
        observed.append((record.state, persisted["records"][0]["state"]))

    record = service.accept_initialize(
        context(),
        KEY,
        OP,
        RCPT,
        promotion=promotion,
    )

    assert record.state == "promoting"
    assert record.cancelable is False
    assert observed == [("promoting", "promoting")]
    assert transitions == ["accepted", "promoting"]


def test_claim_write_failure_does_not_run_promotion_and_can_cancel(tmp_path, monkeypatch) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    write = journal_module.write_journal
    writes = 0
    called: list[object] = []

    def fail_claim(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OperationReceiptError(OPERATION_STORE_WRITE_FAILED, "claim not written")
        return write(*args, **kwargs)

    monkeypatch.setattr(journal_module, "write_journal", fail_claim)
    with pytest.raises(OperationReceiptError):
        service.accept_initialize(context(), KEY, OP, RCPT, promotion=called.append)
    assert called == []
    assert service.get(context(), OP).state == "accepted"
    assert service.cancel(context(), OP).cancelled is True
    reopened = OperationService.open(root)
    assert reopened.get(context(), OP).state == "cancelled"
    reopened.accept_initialize(context(), KEY, OP, RCPT, promotion=called.append)
    assert called == []


@pytest.mark.parametrize("owner", [None, OWNER])
def test_pending_owner_binding_survives_journal_reload(tmp_path, owner) -> None:
    service = OperationService.open(tmp_path / "operations")
    service.begin_auth(context(owner=owner), "login", KEY, OP)
    reopened = OperationService.open(tmp_path / "operations")
    assert reopened.get(context(owner=owner), OP).state == "pending"
    wrong = context(owner=OWNER if owner is None else None)
    for operation in (reopened.get, reopened.cancel):
        with pytest.raises(OnboardingStateError) as error:
            operation(wrong, OP)
        assert error.value.code == ipc_errors.IDENTITY_CONFLICT
    assert reopened.status(wrong)["operations"] == []


def test_initialize_write_failure_has_no_side_effects(tmp_path, monkeypatch) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    called: list[object] = []

    def fail(*args, **kwargs):
        raise OperationReceiptError(
            OPERATION_STORE_WRITE_FAILED, "injected write failure"
        )

    monkeypatch.setattr(journal_module, "write_journal", fail)
    with pytest.raises(OperationReceiptError) as error:
        service.accept_initialize(
            context(), KEY, OP, RCPT, promotion=called.append
        )

    assert error.value.code == OPERATION_STORE_WRITE_FAILED
    assert called == []
    assert not journal_file(root).exists()
    with pytest.raises(OnboardingStateError) as missing:
        service.get(context(), OP)
    assert missing.value.code == AUTH_OPERATION_NOT_FOUND
    assert service.visible(context()) == ()


def test_initialize_write_failure_preserves_existing_journal(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    auth = service.begin_auth(context(), "login", KEY, OP)
    service.mark_authenticated(context(), auth.operation_id, OWNER)
    before = journal_file(root).read_bytes()

    def fail(*args, **kwargs):
        raise OperationReceiptError(
            OPERATION_STORE_WRITE_FAILED, "injected write failure"
        )

    monkeypatch.setattr(journal_module, "write_journal", fail)
    called: list[object] = []
    with pytest.raises(OperationReceiptError):
        service.accept_initialize(
            context(), INITIALIZE_KEY, INITIALIZE_OP, INITIALIZE_RCPT,
            promotion=called.append,
        )

    assert called == []
    assert journal_file(root).read_bytes() == before
    assert service.get(context(), OP).state == "authenticated"
    with pytest.raises(OnboardingStateError):
        service.get(context(), INITIALIZE_OP)


def test_rename_failure_is_zero_write_and_keeps_service_usable(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    auth = service.begin_auth(context(), "login", KEY, OP)
    service.mark_authenticated(context(), auth.operation_id, OWNER)
    before = journal_file(root).read_bytes()

    def fail_replace(source, destination):
        raise OSError("injected rename failure")

    monkeypatch.setattr(journal_module.os, "replace", fail_replace)
    called: list[object] = []
    with pytest.raises(OperationReceiptError) as error:
        service.accept_initialize(
            context(), INITIALIZE_KEY, INITIALIZE_OP, INITIALIZE_RCPT,
            promotion=called.append,
        )

    assert error.value.code == OPERATION_STORE_WRITE_FAILED
    assert called == []
    assert journal_file(root).read_bytes() == before
    assert service.get(context(), auth.operation_id).state == "authenticated"
    assert not list(root.glob(".operations-*"))


def test_directory_fsync_failure_is_unconfirmed_and_freezes_service(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    called: list[object] = []
    original = journal_module._fsync_directory

    def fail_directory_fsync(directory):
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(
        journal_module, "_fsync_directory", fail_directory_fsync
    )
    with pytest.raises(OperationReceiptError) as error:
        service.accept_initialize(
            context(), KEY, OP, RCPT, promotion=called.append
        )

    assert error.value.code == OPERATION_STORE_COMMIT_UNCONFIRMED
    assert called == []
    persisted = json.loads(journal_file(root).read_text())
    assert [record["state"] for record in persisted["records"]] == ["accepted"]
    with pytest.raises(OperationReceiptError) as frozen_get:
        service.get(context(), OP)
    assert frozen_get.value.code == OPERATION_STORE_COMMIT_UNCONFIRMED
    retried: list[object] = []
    with pytest.raises(OperationReceiptError) as frozen_retry:
        service.accept_initialize(
            context(), KEY, OP, RCPT, promotion=retried.append
        )
    assert frozen_retry.value.code == OPERATION_STORE_COMMIT_UNCONFIRMED
    assert retried == []
    with pytest.raises(OperationReceiptError) as frozen_other:
        service.begin_auth(context(), "login", KEY2, OP2)
    assert frozen_other.value.code == OPERATION_STORE_COMMIT_UNCONFIRMED

    monkeypatch.setattr(journal_module, "_fsync_directory", original)
    reopened = OperationService.open(root)
    assert reopened.get(context(), OP).state == "accepted"
    replay: list[object] = []
    again = reopened.accept_initialize(
        context(), KEY, OP, RCPT, promotion=replay.append
    )
    assert again.state == "accepted"
    assert replay == []
    reopened.begin_auth(context(), "login", KEY2, OP2)
    document = json.loads(journal_file(root).read_text())
    states = {
        record["operationId"]: record["state"]
        for record in document["records"]
    }
    assert states == {OP: "accepted", OP2: "pending"}


def test_auth_device_and_initialize_receipts_survive_reopen(tmp_path) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    auth = service.begin_auth(context(), "login", KEY, OP)
    auth = service.mark_authenticated(context(), auth.operation_id, OWNER)
    device = service.begin_device(
        context(), DEVICE_KEY, DEVICE_OP, DEVICE_RCPT
    )
    device = service.mark_device(context(), device.operation_id, "ready")
    initialize = service.accept_initialize(
        context(),
        INITIALIZE_KEY,
        INITIALIZE_OP,
        INITIALIZE_RCPT,
        promotion=lambda record: None,
    )
    initialize = service.mark_initialize(
        context(), initialize.operation_id, "completed"
    )
    before = journal_file(root).read_bytes()

    reopened = OperationService.open(root)
    assert reopened.get(context(), auth.operation_id).state == "authenticated"
    assert reopened.get(context(), device.operation_id).state == "ready"
    assert reopened.get(context(), initialize.operation_id).state == "completed"
    assert journal_file(root).read_bytes() == before
    status = reopened.status(context())
    assert status["authenticated"] is True
    assert status["deviceReady"] is True
    assert status["initialized"] is True
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(journal_file(root).stat().st_mode) == 0o600


def test_same_key_and_operation_id_collisions_keep_old_record(
    tmp_path,
) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    auth = service.begin_auth(context(), "login", KEY, OP)
    service.mark_authenticated(context(), auth.operation_id, OWNER)
    before = journal_file(root).read_bytes()

    with pytest.raises(OnboardingStateError) as same_key:
        service.begin_auth(context(), "register", KEY, OP2)
    assert same_key.value.code == AUTH_OPERATION_CONFLICT
    with pytest.raises(OnboardingStateError) as same_id:
        service.begin_auth(context(), "login", KEY2, OP)
    assert same_id.value.code == AUTH_OPERATION_CONFLICT
    with pytest.raises(OnboardingStateError) as initialize_id:
        service.accept_initialize(
            context(), INITIALIZE_KEY, OP, INITIALIZE_RCPT,
            promotion=lambda record: None,
        )
    assert initialize_id.value.code == AUTH_OPERATION_CONFLICT
    assert journal_file(root).read_bytes() == before
    reopened = OperationService.open(root)
    visible = reopened.visible(context())
    assert len(visible) == 1
    assert visible[0].operation_id == OP
    assert visible[0].state == "authenticated"


def test_cross_context_and_generation_are_rejected(tmp_path) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    auth = service.begin_auth(context(), "login", KEY, OP)

    with pytest.raises(OnboardingStateError) as wrong_home:
        service.get(context(home="home-2"), OP)
    assert wrong_home.value.code == AUTH_OPERATION_NOT_FOUND
    with pytest.raises(OnboardingStateError) as wrong_host:
        service.cancel(context(host="host-2"), OP)
    assert wrong_host.value.code == AUTH_OPERATION_NOT_FOUND
    with pytest.raises(OnboardingStateError) as wrong_view:
        service.get(context(view="view-2"), OP)
    assert wrong_view.value.code == AUTH_OPERATION_NOT_FOUND
    with pytest.raises(OnboardingStateError) as wrong_view_cancel:
        service.cancel(context(view="view-2"), OP)
    assert wrong_view_cancel.value.code == AUTH_OPERATION_NOT_FOUND
    with pytest.raises(OnboardingStateError) as stale_get:
        service.get(context(generation="gen-2"), OP)
    assert stale_get.value.code == ipc_errors.STALE_DAEMON_GENERATION
    with pytest.raises(OnboardingStateError) as stale_mark:
        service.mark_authenticated(context(generation="gen-2"), OP, OWNER)
    assert stale_mark.value.code == ipc_errors.STALE_DAEMON_GENERATION
    with pytest.raises(OnboardingStateError) as wrong_owner:
        service.mark_authenticated(context(), OP, OTHER_OWNER)
    assert wrong_owner.value.code == ipc_errors.IDENTITY_CONFLICT
    assert service.get(context(), auth.operation_id).state == "pending"

    service.mark_authenticated(context(), OP, OWNER)
    with pytest.raises(OnboardingStateError) as owner_get:
        service.get(context(owner=OTHER_OWNER), OP)
    assert owner_get.value.code == ipc_errors.IDENTITY_CONFLICT
    with pytest.raises(OnboardingStateError) as owner_cancel:
        service.cancel(context(owner=OTHER_OWNER), OP)
    assert owner_cancel.value.code == ipc_errors.IDENTITY_CONFLICT
    assert service.status(context(generation="gen-2"))["operations"] == []


def test_cancel_pending_receipt_is_persisted(tmp_path) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    service.begin_auth(context(), "login", KEY, OP)
    outcome = service.cancel(context(), OP)
    assert outcome.cancelled is True
    assert outcome.state == "cancelled"
    reopened = OperationService.open(root)
    assert reopened.get(context(), OP).state == "cancelled"


def test_cancel_does_not_roll_back_committed_identity(tmp_path) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    auth = service.begin_auth(context(), "login", KEY, OP)
    service.mark_authenticated(context(), auth.operation_id, OWNER)
    outcome = service.cancel(context(), OP)
    assert outcome.cancelled is False
    assert outcome.state == "authenticated"
    assert outcome.reason == "identity-committed"
    reopened = OperationService.open(root)
    assert reopened.get(context(), OP).state == "authenticated"


def test_missing_and_mismatched_recovery_authority_is_rejected(tmp_path) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    service.begin_auth(context(owner=None), "login", KEY, OP)
    birth = process_birth_identity(os.getpid())
    assert birth is not None

    with pytest.raises(OperationReceiptError) as missing:
        service.migrate_generation(context(generation="gen-2", owner=None), OP, None)
    assert missing.value.code == RECOVERY_PROOF_REQUIRED
    with pytest.raises(OperationReceiptError) as raw:
        service.migrate_generation(
            context(generation="gen-2", owner=None), OP, "not-an-authority"
        )
    assert raw.value.code == RECOVERY_PROOF_REQUIRED
    with pytest.raises(OperationReceiptError) as incomplete:
        RecoveryAuthority(
            "home-1", "host-1", "view-1", None, os.getpid(), " "
        )
    assert incomplete.value.code == RECOVERY_PROOF_REQUIRED
    wrong_scope = RecoveryAuthority(
        "home-2", "host-1", "view-1", None, os.getpid(), birth
    )
    with pytest.raises(OperationReceiptError) as mismatch:
        service.migrate_generation(
            context(generation="gen-2", owner=None), OP, wrong_scope
        )
    assert mismatch.value.code == RECOVERY_SCOPE_MISMATCH
    wrong_owner = RecoveryAuthority(
        "home-1", "host-1", "view-1", OWNER, os.getpid(), birth
    )
    with pytest.raises(OperationReceiptError) as owner_mismatch:
        service.migrate_generation(
            context(generation="gen-2", owner=None), OP, wrong_owner
        )
    assert owner_mismatch.value.code == RECOVERY_SCOPE_MISMATCH
    wrong_pid = RecoveryAuthority(
        "home-1", "host-1", "view-1", None, os.getpid() + 1, birth
    )
    with pytest.raises(OperationReceiptError) as controller_mismatch:
        service.migrate_generation(
            context(generation="gen-2", owner=None), OP, wrong_pid
        )
    assert controller_mismatch.value.code == RECOVERY_SCOPE_MISMATCH
    wrong_birth = RecoveryAuthority(
        "home-1", "host-1", "view-1", None, os.getpid(), "not-the-birth"
    )
    with pytest.raises(OperationReceiptError) as birth_mismatch:
        service.migrate_generation(
            context(generation="gen-2", owner=None), OP, wrong_birth
        )
    assert birth_mismatch.value.code == RECOVERY_SCOPE_MISMATCH

    authority = RecoveryAuthority(
        "home-1", "host-1", "view-1", None, os.getpid(), birth
    )
    migrated = service.migrate_generation(
        context(generation="gen-2", owner=None), OP, authority
    )
    assert migrated.state == "pending"
    assert service.get(context(generation="gen-2", owner=None), OP).state == "pending"
    with pytest.raises(OnboardingStateError):
        service.get(context(generation="gen-1", owner=None), OP)
    reopened = OperationService.open(root)
    assert (
        reopened.get(context(generation="gen-2", owner=None), OP).state
        == "pending"
    )


def test_promotion_failure_keeps_non_cancelable_claim_persisted(tmp_path) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)

    def fail_promotion(record) -> None:
        raise RuntimeError("promotion failed")

    with pytest.raises(RuntimeError):
        service.accept_initialize(
            context(), KEY, OP, RCPT, promotion=fail_promotion
        )
    assert service.get(context(), OP).state == "promoting"
    assert service.cancel(context(), OP).cancelled is False
    reopened = OperationService.open(root)
    assert reopened.get(context(), OP).state == "promoting"
    replay: list[object] = []
    again = reopened.accept_initialize(
        context(), KEY, OP, RCPT, promotion=replay.append
    )
    assert again.state == "promoting"
    assert replay == []


def test_invalid_operation_identity_is_not_written(tmp_path) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)

    with pytest.raises(OperationReceiptError) as error:
        service.begin_auth(context(), "login", "short", OP)
    assert error.value.code == OPERATION_STORE_WRITE_FAILED
    assert not journal_file(root).exists()
    with pytest.raises(OnboardingStateError):
        service.get(context(), OP)


def test_reopen_does_not_replay_claimed_initialize(tmp_path) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    first: list[object] = []
    service.accept_initialize(
        context(), KEY, OP, RCPT, promotion=first.append
    )
    assert len(first) == 1
    before = journal_file(root).read_bytes()

    reopened = OperationService.open(root)
    second: list[object] = []
    record = reopened.accept_initialize(
        context(), KEY, OP, RCPT, promotion=second.append
    )
    assert record.state == "promoting"
    assert second == []
    assert journal_file(root).read_bytes() == before


def test_auth_pending_survives_same_process_reopen(tmp_path) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    service.begin_auth(context(), "login", KEY, OP)
    before = journal_file(root).read_bytes()
    reopened = OperationService.open(root)
    assert reopened.get(context(), OP).state == "pending"
    assert journal_file(root).read_bytes() == before


def test_secret_material_is_absent_from_journal(tmp_path) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    auth = service.begin_auth(context(), "login", KEY, OP)
    service.mark_authenticated(context(), auth.operation_id, OWNER)
    device = service.begin_device(
        context(), DEVICE_KEY, DEVICE_OP, DEVICE_RCPT
    )
    service.mark_device(context(), device.operation_id, "ready")
    service.accept_initialize(
        context(),
        INITIALIZE_KEY,
        INITIALIZE_OP,
        INITIALIZE_RCPT,
        promotion=lambda record: None,
    )
    text = journal_file(root).read_text()
    document = json.loads(text)
    assert set(document) == {"version", "records", "keys"}
    assert len(document["records"]) == 3
    assert all(set(record) == RECORD_FIELDS for record in document["records"])
    lowered = text.lower()
    for forbidden in (
        "accesstoken",
        "refreshtoken",
        "devicecode",
        "usercode",
        "bindingbearer",
        "bearer",
        "verification",
        "password",
        "authorizationcode",
    ):
        assert forbidden not in lowered


@pytest.mark.parametrize(
    "mutate",
    [
        lambda document: {**document, "extra": 1},
        lambda document: {**document, "version": True},
        lambda document: _set_record(document, "extra", 1),
        lambda document: _set_record(document, "kind", ["login"]),
        lambda document: _set_record(document, "operationId", "short"),
        lambda document: _set_record(document, "writerPid", 2**63),
        lambda document: _set_record(document, "writerPid", True),
        lambda document: _set_surface(document, "extra", 1),
        lambda document: _set_key(document, "extra", 1),
        lambda document: _set_key(document, "operationId", "op-missing-0001"),
        lambda document: _set_key(
            document, "operationKey", "opkey-ffffffffffffffff"
        ),
        lambda document: _drop_keys(document),
        lambda document: _duplicate_key(document),
        lambda document: _duplicate_record(document),
    ],
)
def test_corrupt_journal_is_preserved_and_fails_closed(
    tmp_path, mutate
) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    service.begin_auth(context(), "login", KEY, OP)
    original = json.loads(journal_file(root).read_text())
    changed = mutate(copy.deepcopy(original))
    raw = (json.dumps(changed) + "\n").encode()
    journal_file(root).write_bytes(raw)
    if os.name == "posix":
        journal_file(root).chmod(0o600)

    with pytest.raises(OperationReceiptError) as error:
        OperationService.open(root)
    assert error.value.code == OPERATION_STORE_CORRUPT
    assert journal_file(root).read_bytes() == raw


def test_truncated_duplicate_and_malformed_journal_fails_closed(tmp_path) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    service.begin_auth(context(), "login", KEY, OP)
    original = journal_file(root).read_bytes()
    cases = (
        b"",
        b"{",
        original[: len(original) // 2],
        b'{"version":1,"version":1,"records":[],"keys":[]}',
        b'{"version":1,"canary":"SECRETCANARY"}',
        b"[]",
        b"[" * 6000 + b"]" * 6000,
        b"x" * (journal_module.MAX_JOURNAL_BYTES + 1),
    )
    for raw in cases:
        journal_file(root).write_bytes(raw)
        if os.name == "posix":
            journal_file(root).chmod(0o600)
        with pytest.raises(OperationReceiptError) as error:
            OperationService.open(root)
        assert error.value.code == OPERATION_STORE_CORRUPT
        assert "SECRETCANARY" not in str(error.value)
        assert journal_file(root).read_bytes() == raw


def test_insecure_root_and_journal_links_fail_closed(tmp_path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("needs symlink support")
    root = tmp_path / "operations"
    root.mkdir(mode=0o700)
    target = tmp_path / "target.json"
    target.write_text("{}\n")
    journal_file(root).symlink_to(target)
    with pytest.raises(OperationReceiptError) as linked_journal:
        OperationService.open(root)
    assert linked_journal.value.code == OPERATION_STORE_INSECURE
    assert journal_file(root).is_symlink()
    assert target.read_text() == "{}\n"

    journal_file(root).unlink()
    linked_root = tmp_path / "linked-operations"
    linked_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(OperationReceiptError) as linked_directory:
        OperationService.open(linked_root)
    assert linked_directory.value.code == OPERATION_STORE_INSECURE

    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("not a directory")
    with pytest.raises(OperationReceiptError) as not_directory:
        OperationService.open(regular_file)
    assert not_directory.value.code == OPERATION_STORE_INSECURE


def test_fifo_journal_is_rejected_before_open(tmp_path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("needs mkfifo")
    root = tmp_path / "operations"
    root.mkdir(mode=0o700)
    os.mkfifo(journal_file(root), 0o600)
    with pytest.raises(OperationReceiptError) as error:
        OperationService.open(root)
    assert error.value.code == OPERATION_STORE_INSECURE
    assert stat.S_ISFIFO(journal_file(root).lstat().st_mode)


def test_fifo_journal_open_is_bounded_and_post_open_rejected(tmp_path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("needs mkfifo")
    root = tmp_path / "operations"
    root.mkdir(mode=0o700)
    os.mkfifo(journal_file(root), 0o600)
    completed = subprocess.run(
        [sys.executable, "-c", _FIFO_CHILD, str(root)],
        cwd=repository_root(),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == OPERATION_STORE_INSECURE
    assert stat.S_ISFIFO(journal_file(root).lstat().st_mode)


def test_reparse_root_and_journal_fail_closed_and_umask_creates_private_files(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "operations"
    previous_umask = apply_private_umask()
    try:
        service = OperationService.open(root)
        service.begin_auth(context(), "login", KEY, OP)
    finally:
        if posix_metadata_enforced():
            os.umask(previous_umask)
    if posix_metadata_enforced():
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(journal_file(root).stat().st_mode) == 0o600

    raw = journal_file(root).read_bytes()
    monkeypatch.setattr(journal_module, "path_is_reparse", lambda _meta: True)
    with pytest.raises(OperationReceiptError) as reparse_journal:
        OperationService.open(root)
    assert reparse_journal.value.code == OPERATION_STORE_INSECURE
    with pytest.raises(OperationReceiptError) as reparse_root:
        journal_module.prepare_root(root)
    assert reparse_root.value.code == OPERATION_STORE_INSECURE
    assert journal_file(root).read_bytes() == raw


def _set_record(document, field: str, value) -> dict:
    document["records"][0][field] = value
    return document


def _set_surface(document, field: str, value) -> dict:
    document["records"][0]["surface"][field] = value
    return document


def _set_key(document, field: str, value) -> dict:
    document["keys"][0][field] = value
    return document


def _duplicate_key(document) -> dict:
    document["keys"].append(dict(document["keys"][0]))
    return document


def _drop_keys(document) -> dict:
    document["keys"] = []
    return document


def _duplicate_record(document) -> dict:
    document["records"].append(copy.deepcopy(document["records"][0]))
    return document
