"""P1 first-run receipt storage: strict, atomic, identity-partitioned."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from hyprial.daemon.impl.onboarding import first_run as first_run_module
from hyprial.daemon.impl.onboarding.first_run import (
    FirstRunStore,
    FirstRunStoreError,
    FIRST_RUN_REVISION_CONFLICT,
    FIRST_RUN_STORE_COMMIT_UNCONFIRMED,
    FIRST_RUN_STORE_WRITE_FAILED,
    first_run_status,
)
from hyprial.daemon.impl.request_catalog.onboarding import FirstRunSnapshot
from hyprial.identity import OnboardingSubject
from hyprial.kernel import PrivateAccessUnsupported

ISSUER = "https://issuer.example.test"
USER_ID = "user-123"
DEVICE_UID = "12345678123442348234123456789abc"
KEY = "opkey-0123456789abcdef"
KEY_2 = "opkey-fedcba9876543210"


def _subject(
    *,
    user_id: str = USER_ID,
    device_uid: str = DEVICE_UID,
) -> OnboardingSubject:
    return OnboardingSubject(
        issuer=ISSUER,
        user_id=user_id,
        device_uid=device_uid,
    )


def _store(tmp_path: Path) -> FirstRunStore:
    return FirstRunStore(tmp_path / "onboarding")


def test_absent_record_is_not_an_empty_completion(tmp_path):
    snapshot = _store(tmp_path).read(_subject())
    assert snapshot.state == "none"
    assert snapshot.revision == 0
    assert snapshot.confirmed == ()
    assert snapshot.updated_at is None
    assert not (tmp_path / "onboarding" / "first-run.json").exists()


def test_confirmation_is_strict_and_atomic(tmp_path):
    store = _store(tmp_path)
    snapshot = store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    assert snapshot.state == "recorded"
    assert snapshot.revision == 1
    assert snapshot.confirmed == ("configuration",)
    path = tmp_path / "onboarding" / "first-run.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert document["records"] == [
        {
            "issuer": ISSUER,
            "userId": USER_ID,
            "deviceUid": DEVICE_UID,
            "revision": 1,
            "confirmed": ["configuration"],
            "lastOperationKey": KEY,
            "updatedAt": "2026-11-18T00:00:00Z",
        }
    ]


def test_skip_records_a_durable_shortcut_without_confirming_steps(tmp_path):
    store = _store(tmp_path)
    snapshot = store.skip(
        _subject(),
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    assert snapshot.state == "recorded"
    assert snapshot.confirmed == ()
    assert snapshot.skipped is True
    assert store.read(_subject()) == snapshot
    assert json.loads(store.path.read_text(encoding="utf-8"))["records"][0]["skipped"] is True


def test_same_operation_key_and_same_step_is_idempotent(tmp_path):
    store = _store(tmp_path)
    first = store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    path = tmp_path / "onboarding" / "first-run.json"
    before = path.read_bytes()
    again = store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:01:00Z",
    )
    assert again == first
    assert path.read_bytes() == before


@pytest.mark.parametrize("supported", [True, False])
def test_receipt_publication_requires_private_file_access(
    tmp_path, monkeypatch, supported
):
    store = _store(tmp_path)
    first = store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
    )
    before = store.path.read_bytes()
    harden = first_run_module.harden_private_path
    checked = []

    def control_private_access(path, *, mode):
        if path.is_file():
            checked.append(path)
            assert path.parent == store.path.parent
            assert path != store.path
            assert mode == 0o600
            assert path.read_bytes() == b""
            if not supported:
                raise PrivateAccessUnsupported("injected unsupported private ACL")
        harden(path, mode=mode)

    monkeypatch.setattr(first_run_module, "harden_private_path", control_private_access)
    if supported:
        result = store.confirm(
            _subject(), step="reply", expected_revision=first.revision,
            operation_key=KEY_2,
        )
        assert result.revision == first.revision + 1
        assert result.confirmed == ("configuration", "reply")
    else:
        with pytest.raises(FirstRunStoreError) as failed:
            store.confirm(
                _subject(), step="reply", expected_revision=first.revision,
                operation_key=KEY_2,
            )
        assert failed.value.code == FIRST_RUN_STORE_WRITE_FAILED
        assert store.path.read_bytes() == before
        assert store.read(_subject()) == first
    assert len(checked) == 1
    assert list(store.path.parent.iterdir()) == [store.path]


def test_same_step_with_another_key_does_not_consume_a_revision(tmp_path):
    store = _store(tmp_path)
    first = store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    again = store.confirm(
        _subject(),
        step="configuration",
        expected_revision=1,
        operation_key=KEY_2,
        now="2026-11-18T00:01:00Z",
    )
    assert again == first


def test_stale_revision_is_a_conflict_and_does_not_write(tmp_path):
    store = _store(tmp_path)
    store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    path = tmp_path / "onboarding" / "first-run.json"
    before = path.read_bytes()
    with pytest.raises(FirstRunStoreError) as refused:
        store.confirm(
            _subject(),
            step="reply",
            expected_revision=0,
            operation_key=KEY_2,
            now="2026-11-18T00:01:00Z",
        )
    assert refused.value.code == FIRST_RUN_REVISION_CONFLICT
    assert path.read_bytes() == before


def test_subject_partition_is_issuer_user_and_device(tmp_path):
    store = _store(tmp_path)
    store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    assert store.read(_subject(user_id="other-user")).state == "none"
    assert store.read(_subject(device_uid="f" * 32)).state == "none"
    assert store.read(None).state == "none"


def test_corrupt_file_is_preserved_and_never_treated_as_empty(tmp_path):
    root = tmp_path / "onboarding"
    root.mkdir()
    root.chmod(0o700)
    path = root / "first-run.json"
    original = b'{"version": 1, "records": ['
    path.write_bytes(original)
    path.chmod(0o600)
    store = FirstRunStore(root)
    snapshot = store.read(_subject())
    assert snapshot.state == "corrupt"
    assert snapshot.revision == 0
    assert path.read_bytes() == original
    with pytest.raises(FirstRunStoreError):
        store.confirm(
            _subject(),
            step="configuration",
            expected_revision=0,
            operation_key=KEY,
            now="2026-11-18T00:00:00Z",
        )
    assert path.read_bytes() == original


def test_unknown_version_is_reported_and_preserved(tmp_path):
    root = tmp_path / "onboarding"
    root.mkdir()
    root.chmod(0o700)
    path = root / "first-run.json"
    original = b'{"version": 2, "records": []}\n'
    path.write_bytes(original)
    path.chmod(0o600)
    snapshot = FirstRunStore(root).read(_subject())
    assert snapshot.state == "unsupported-version"
    assert path.read_bytes() == original


def test_symlinked_record_is_not_followed(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"version": 1, "records": []}\n', encoding="utf-8")
    target.chmod(0o600)
    root = tmp_path / "onboarding"
    root.mkdir()
    root.chmod(0o700)
    (root / "first-run.json").symlink_to(target)
    snapshot = FirstRunStore(root).read(_subject())
    assert snapshot.state == "corrupt"
    assert target.read_text(encoding="utf-8") == '{"version": 1, "records": []}\n'


def test_failed_replace_keeps_the_previous_record(monkeypatch, tmp_path):
    store = _store(tmp_path)
    first = store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    path = tmp_path / "onboarding" / "first-run.json"
    before = path.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(FirstRunStoreError) as failed:
        store.confirm(
            _subject(),
            step="reply",
            expected_revision=1,
            operation_key=KEY_2,
            now="2026-11-18T00:01:00Z",
        )
    assert failed.value.code == FIRST_RUN_STORE_WRITE_FAILED
    assert path.read_bytes() == before
    assert store.read(_subject()).revision == first.revision


def test_status_keeps_progress_when_logout_removes_the_subject(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "onboarding"
    store = FirstRunStore(root)
    store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    path = root / "first-run.json"
    before = path.read_bytes()
    signed_out = first_run_status(home, root, subject_reader=lambda _home: None)
    assert signed_out.state == "none"
    assert path.read_bytes() == before
    signed_in = first_run_status(home, root, subject_reader=lambda _home: _subject())
    assert signed_in.state == "recorded"
    assert signed_in.revision == 1


def test_status_does_not_use_a_shared_fallback_or_another_device(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    root = tmp_path / "onboarding"
    FirstRunStore(root).confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    copied_home = first_run_status(
        home,
        root,
        subject_reader=lambda _home: _subject(device_uid="f" * 32),
    )
    assert copied_home.state == "none"
    unavailable = first_run_status(
        home,
        root,
        subject_reader=lambda _home: (_ for _ in ()).throw(ValueError("corrupt")),
    )
    assert unavailable.state == "unavailable"


def test_snapshot_invariants_reject_a_fabricated_completion(tmp_path):
    from hyprial.daemon.impl.request_catalog.onboarding import FirstRunSnapshot

    with pytest.raises(Exception):
        FirstRunSnapshot(state="recorded", revision=0, updated_at=None)
    with pytest.raises(Exception):
        FirstRunSnapshot(
            state="corrupt",
            revision=1,
            confirmed=("configuration",),
            updated_at="2026-11-18T00:00:00Z",
        )


def test_status_does_not_read_or_expose_history_before_authentication(tmp_path):
    root = tmp_path / "onboarding"
    FirstRunStore(root).confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    reads: list[Path] = []

    def subject_reader(home: Path):
        reads.append(home)
        return _subject()

    snapshot = first_run_status(
        tmp_path / "home",
        root,
        authenticated=False,
        subject_reader=subject_reader,
    )
    assert snapshot.state == "none"
    assert reads == []


def test_directory_mode_is_rejected_and_preserved(tmp_path):
    store = _store(tmp_path)
    store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    root = tmp_path / "onboarding"
    root.chmod(0o755)

    assert store.read(_subject()).state == "corrupt"
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_file_mode_is_rejected_and_preserved(tmp_path):
    store = _store(tmp_path)
    store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    store.path.chmod(0o644)

    assert store.read(_subject()).state == "corrupt"
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o644


def test_symlinked_root_is_not_followed(tmp_path):
    outside = tmp_path / "outside"
    FirstRunStore(outside).confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    link = tmp_path / "onboarding"
    link.symlink_to(outside, target_is_directory=True)

    assert FirstRunStore(link).read(_subject()).state == "corrupt"
    assert FirstRunStore(outside).read(_subject()).state == "recorded"


def test_confirmation_flushes_the_directory(tmp_path, monkeypatch):
    observations: list[bool] = []
    original = os.fsync

    def observe(descriptor):
        observations.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        return original(descriptor)

    monkeypatch.setattr(os, "fsync", observe)
    _store(tmp_path).confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    assert True in observations


def test_post_replace_flush_failure_is_unconfirmed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )

    def fail_directory(_path):
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(
        "hyprial.daemon.impl.onboarding.first_run.fsync_directory",
        fail_directory,
    )
    with pytest.raises(FirstRunStoreError) as failed:
        store.confirm(
            _subject(),
            step="reply",
            expected_revision=1,
            operation_key=KEY_2,
            now="2026-11-18T00:01:00Z",
        )
    assert failed.value.code == FIRST_RUN_STORE_COMMIT_UNCONFIRMED
    assert store.read(_subject()).revision == 2


def test_idempotent_retry_requires_a_successful_directory_flush(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    original = first_run_module.fsync_directory

    def fail_directory(_path):
        raise OSError("injected directory fsync failure")

    monkeypatch.setattr(first_run_module, "fsync_directory", fail_directory)
    with pytest.raises(FirstRunStoreError) as initial:
        store.confirm(
            _subject(),
            step="reply",
            expected_revision=1,
            operation_key=KEY_2,
            now="2026-11-18T00:01:00Z",
        )
    assert initial.value.code == FIRST_RUN_STORE_COMMIT_UNCONFIRMED
    after = store.path.read_bytes()

    with pytest.raises(FirstRunStoreError) as retry:
        store.confirm(
            _subject(),
            step="reply",
            expected_revision=1,
            operation_key=KEY_2,
            now="2026-11-18T00:01:00Z",
        )
    assert retry.value.code == FIRST_RUN_STORE_COMMIT_UNCONFIRMED

    monkeypatch.setattr(first_run_module, "fsync_directory", original)
    result = store.confirm(
        _subject(),
        step="reply",
        expected_revision=1,
        operation_key=KEY_2,
        now="2026-11-18T00:01:00Z",
    )
    assert result.revision == 2
    assert store.path.read_bytes() == after


def test_raced_public_root_is_refused_without_chmod(tmp_path, monkeypatch):
    root = tmp_path / "onboarding"
    original = Path.mkdir
    raced: list[bool] = []

    def create_public(self, *args, **kwargs):
        if self == root and not raced:
            original(self, mode=0o755)
            self.chmod(0o755)
            raced.append(True)
            raise FileExistsError(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", create_public)
    with pytest.raises(FirstRunStoreError):
        _store(tmp_path).confirm(
            _subject(),
            step="configuration",
            expected_revision=0,
            operation_key=KEY,
            now="2026-11-18T00:00:00Z",
        )
    assert raced == [True]
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert not (root / "first-run.json").exists()


def test_raced_private_root_is_accepted(tmp_path, monkeypatch):
    root = tmp_path / "onboarding"
    original = Path.mkdir
    raced: list[bool] = []

    def create_private(self, *args, **kwargs):
        if self == root and not raced:
            original(self, mode=0o700)
            self.chmod(0o700)
            raced.append(True)
            raise FileExistsError(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", create_private)
    result = _store(tmp_path).confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    assert raced == [True]
    assert result.state == "recorded"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_raced_missing_parent_symlink_is_rejected_before_child_creation(
    tmp_path, monkeypatch
):
    parent = tmp_path / "new-parent"
    root = parent / "onboarding"
    outside = tmp_path / "outside-parent"
    outside.mkdir(mode=0o700)
    original = Path.mkdir
    raced: list[bool] = []

    def create_symlink(self, *args, **kwargs):
        if self == parent and not raced:
            self.symlink_to(outside, target_is_directory=True)
            raced.append(True)
            raise FileExistsError(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", create_symlink)
    with pytest.raises(FirstRunStoreError):
        FirstRunStore(root).confirm(
            _subject(),
            step="configuration",
            expected_revision=0,
            operation_key=KEY,
            now="2026-11-18T00:00:00Z",
        )
    assert raced == [True]
    assert parent.is_symlink()
    assert list(outside.iterdir()) == []


def test_raced_private_parent_is_accepted(tmp_path, monkeypatch):
    parent = tmp_path / "new-parent"
    root = parent / "onboarding"
    original = Path.mkdir
    raced: list[bool] = []

    def create_private(self, *args, **kwargs):
        if self == parent and not raced:
            original(self, mode=0o700)
            self.chmod(0o700)
            raced.append(True)
            raise FileExistsError(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", create_private)
    result = FirstRunStore(root).confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    assert raced == [True]
    assert result.state == "recorded"
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert (root / "first-run.json").is_file()


def test_invalid_calendar_timestamp_is_corrupt_and_preserved(tmp_path):
    store = _store(tmp_path)
    store.confirm(
        _subject(),
        step="configuration",
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["records"][0]["updatedAt"] = "2026-99-99T99:99:99Z"
    store.path.write_text(json.dumps(raw), encoding="utf-8")
    before = store.path.read_bytes()

    assert store.read(_subject()).state == "corrupt"
    assert store.path.read_bytes() == before


def test_snapshot_rejects_invalid_calendar_timestamp():
    with pytest.raises(ValueError):
        FirstRunSnapshot(
            state="recorded",
            revision=1,
            confirmed=("reply",),
            updated_at="2026-99-99T99:99:99Z",
        )


def test_final_confirmation_records_both_steps_atomically_and_idempotently(tmp_path):
    store = _store(tmp_path)
    first = store.confirm_many(
        _subject(),
        steps=("configuration", "reply"),
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )
    assert first.state == "recorded"
    assert first.revision == 1
    assert first.confirmed == ("configuration", "reply")

    repeated = store.confirm_many(
        _subject(),
        steps=("configuration", "reply"),
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T01:00:00Z",
    )
    assert repeated == first


def test_confirmation_rejects_a_stale_revision_before_adding_a_step(tmp_path):
    store = _store(tmp_path)
    store.confirm_many(
        _subject(),
        steps=("configuration",),
        expected_revision=0,
        operation_key=KEY,
        now="2026-11-18T00:00:00Z",
    )

    with pytest.raises(FirstRunStoreError) as raised:
        store.confirm_many(
            _subject(),
            steps=("reply",),
            expected_revision=0,
            operation_key=KEY_2,
            now="2026-11-18T01:00:00Z",
        )
    assert raised.value.code == FIRST_RUN_REVISION_CONFLICT

    completed = store.confirm_many(
        _subject(),
        steps=("reply",),
        expected_revision=1,
        operation_key=KEY_2,
        now="2026-11-18T02:00:00Z",
    )
    assert completed.revision == 2
    assert completed.confirmed == ("configuration", "reply")
