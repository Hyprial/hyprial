from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


def test_daemon_import_surface_never_imports_tryx_native_package() -> None:
    root = Path(__file__).resolve().parents[4] / "src/hyprial/daemon/impl/adapters/whatsapp"
    assert root.is_dir()
    violations = []
    for path in root.rglob("*.py"):
        if path.name == "tryx_driver.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name == "tryx" or alias.name.startswith("tryx.") for alias in node.names):
                violations.append(path)
            if isinstance(node, ast.ImportFrom) and node.module and (node.module == "tryx" or node.module.startswith("tryx.")):
                violations.append(path)
    assert violations == []


def test_default_worker_command_is_child_only_and_contains_no_token_or_state_secret(tmp_path: Path) -> None:
    from hyprial.daemon.impl.adapters.whatsapp import whatsapp_worker_command

    command = whatsapp_worker_command()
    assert command[:2] == (sys.executable, "-m")
    assert command[-1] == "hyprial.daemon.impl.adapters.whatsapp.worker.tryx_driver"
    joined = " ".join(command)
    assert "token" not in joined.lower()
    assert str(tmp_path) not in joined


def test_package_import_keeps_tryx_out_of_daemon_modules() -> None:
    before = {name for name in sys.modules if name == "tryx" or name.startswith("tryx.")}
    import hyprial.daemon.impl.adapters.whatsapp as whatsapp

    assert whatsapp.ArtifactStatus.unavailable("x").available is False
    after = {name for name in sys.modules if name == "tryx" or name.startswith("tryx.")}
    assert after == before


def test_private_store_keeps_cursor_and_unknown_submission_across_reopen(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.adapters.whatsapp.worker.store import NativeStore

    event = {
        "messageId": "wamid-1",
        "chatJid": "15550002@s.whatsapp.net",
    }
    first = NativeStore(tmp_path)
    cursor = first.append_event(event)
    assert first.commit_checkpoint(cursor, "journal-1", "d" * 64)
    assert first.begin_submission("node-1", "request-digest") is None
    first.close()

    reopened = NativeStore(tmp_path)
    try:
        assert reopened.history(None, 10) == [{"cursor": cursor, "event": event}]
        replay = reopened.begin_submission("node-1", "request-digest")
        assert replay["state"] == "unknown"
        assert replay["definitelyNotSent"] is False
        with pytest.raises(ValueError, match="changed request"):
            reopened.begin_submission("node-1", "different-digest")
    finally:
        reopened.close()


def test_actual_w1_generation_restart_reuses_instance_durable_store_and_fence(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.adapters.whatsapp.worker.store import NativeStore
    from hyprial.daemon.impl.adapters.worker import NativeSessionWriterLock

    first_lock = NativeSessionWriterLock(tmp_path, "whatsapp", "wa-main", 7)
    first_lock.acquire()
    try:
        first = NativeStore.for_worker_session(first_lock.directory)
        assert first.begin_submission("node-unknown", "request-digest") is None
        first.set_metadata("account_jid", "15550001@s.whatsapp.net")
        first.fence_effects(
            instance_id="wa-main",
            generation=7,
            account_jid="15550001@s.whatsapp.net",
            reason_code="WHATSAPP_NATIVE_CRASH",
        )
        durable_path = first.path
        first.close()
    finally:
        first_lock.release()

    second_lock = NativeSessionWriterLock(tmp_path, "whatsapp", "wa-main", 8)
    second_lock.acquire()
    try:
        restarted = NativeStore.for_worker_session(second_lock.directory)
        try:
            assert restarted.path == durable_path
            assert restarted.path.parent == tmp_path / "adapters" / "whatsapp" / "wa-main" / "durable"
            assert restarted.metadata("account_jid") == "15550001@s.whatsapp.net"
            replay = restarted.begin_submission("node-unknown", "request-digest")
            assert replay["state"] == "unknown"
            assert replay["definitelyNotSent"] is False
            assert restarted.effect_fence(
                instance_id="wa-main",
                generation=8,
                account_jid="15550001@s.whatsapp.net",
            ) == "WHATSAPP_NATIVE_CRASH"
        finally:
            restarted.close()
    finally:
        second_lock.release()


def test_legacy_generation_store_migrates_once_without_resending_unknown(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.adapters.whatsapp.worker.store import NativeStore

    instance_dir = tmp_path / "adapters" / "whatsapp" / "wa-main"
    legacy_dir = instance_dir / "7" / "native-session"
    legacy = NativeStore(legacy_dir)
    assert legacy.begin_submission("node-legacy", "legacy-digest") is None
    legacy.set_metadata("account_jid", "15550001@s.whatsapp.net")
    legacy.close()

    generation_eight = instance_dir / "8" / "native-session"
    migrated = NativeStore.for_worker_session(generation_eight)
    try:
        assert migrated.metadata("account_jid") == "15550001@s.whatsapp.net"
        assert migrated.begin_submission("node-legacy", "legacy-digest")["state"] == "unknown"
    finally:
        migrated.close()


def test_durable_store_has_one_instance_writer_across_w1_generations(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.adapters.whatsapp.worker.store import NativeStore

    instance_dir = tmp_path / "adapters" / "whatsapp" / "wa-main"
    first = NativeStore.for_worker_session(instance_dir / "7" / "native-session")
    try:
        with pytest.raises(BlockingIOError):
            NativeStore.for_worker_session(instance_dir / "8" / "native-session")
    finally:
        first.close()

    replacement = NativeStore.for_worker_session(
        instance_dir / "8" / "native-session"
    )
    replacement.close()


def test_unknown_reconcile_advances_only_from_correlated_native_receipt_or_echo(
    tmp_path: Path,
) -> None:
    from hyprial.daemon.impl.adapters.whatsapp.worker.store import NativeStore

    store = NativeStore(tmp_path)
    try:
        assert store.begin_submission("node-1", "digest-1") is None
        store.note_submission_candidate("node-1", "wamid-native-1")
        assert store.settle_native_evidence("wamid-other", "receipt") is False
        assert store.submission("node-1")["state"] == "unknown"
        assert store.settle_native_evidence("wamid-native-1", "echo") is True
        settled = store.submission("node-1")
        assert settled["state"] == "accepted"
        assert settled["providerMessageId"] == "wamid-native-1"
        assert settled["reasonCode"] is None
    finally:
        store.close()
