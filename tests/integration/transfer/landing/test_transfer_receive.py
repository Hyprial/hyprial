"""AT07: land a validated bundle on a target agent home (red-first mutants)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from hyprial.kernel import resolve_node_id

from hyprial.daemon.impl.transfer.archive.bundle import BundleTamperError, export_bundle
from hyprial.daemon.impl.transfer.landing.receive import (
    ReceiveActorMismatch,
    ReceiveDestinationNotEmpty,
    ReceiveFailed,
    receive_bundle)
from tests.support.transfer.rehearsal import UNSIGNED_REHEARSAL
from tests.support.transfer.signed import enroll_machine_key

CREATED_AT = "2026-09-30T22:05:00+08:00"


def _source_home(tmp_path: Path) -> Path:
    home = tmp_path / "agent-home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "agent.json").write_text('{"actor": "demo"}\n')
    (home / "notes.md").write_text("hello bundle\n")
    (home / "secrets").mkdir()
    secret = home / "secrets" / "private.json"
    secret.write_text('{"token": "synthetic"}\n')
    os.chmod(secret, 0o600)
    return home


def _bundle(tmp_path: Path, **overrides) -> Path:
    source = overrides.pop("source", None) or _source_home(tmp_path)
    bundle_dir = overrides.pop("bundle_dir", None) or (tmp_path / "bundle")
    kwargs = dict(
        owner="owner@example.com",
        actor="codex-sw",
        source_machine="devusr-dev",
        created_at=CREATED_AT,
        session_refs=["session-1"],
        grants=["tool-surface"],
    )
    kwargs.update(overrides)
    export_bundle(source, bundle_dir, **kwargs)
    return bundle_dir


def test_receive_roundtrip_into_empty_destination(tmp_path: Path) -> None:
    bundle_dir = _bundle(tmp_path)
    destination = tmp_path / "target-home"

    result = receive_bundle(
        bundle_dir,
        destination,
        expect_actor="codex-sw",
        signature=UNSIGNED_REHEARSAL,
    )

    assert result.dry_run is False
    assert result.actor == "codex-sw"
    assert result.owner == "owner@example.com"
    assert result.source_machine == "devusr-dev"
    assert result.files == 3
    assert result.bytes == sum(
        entry.size for entry in result.manifest.entries
    )

    assert (destination / "notes.md").read_text() == "hello bundle\n"
    assert (destination / "config" / "agent.json").exists()
    landed_secret = destination / "secrets" / "private.json"
    assert landed_secret.exists()
    assert stat.S_IMODE(landed_secret.stat().st_mode) == 0o600


def test_receive_into_existing_empty_directory_keeps_the_directory(
    tmp_path: Path,
) -> None:
    bundle_dir = _bundle(tmp_path)
    destination = tmp_path / "prepared-home"
    destination.mkdir()

    receive_bundle(
        bundle_dir,
        destination,
        signature=UNSIGNED_REHEARSAL,
    )

    assert destination.is_dir()
    assert (destination / "notes.md").exists()


def test_receive_dry_run_writes_nothing(tmp_path: Path) -> None:
    bundle_dir = _bundle(tmp_path)
    destination = tmp_path / "target-home"

    result = receive_bundle(
        bundle_dir,
        destination,
        dry_run=True,
        signature=UNSIGNED_REHEARSAL,
    )

    assert result.dry_run is True
    assert result.files == 3
    assert not destination.exists()


def test_receive_refuses_non_empty_destination(tmp_path: Path) -> None:
    bundle_dir = _bundle(tmp_path)
    destination = tmp_path / "target-home"
    destination.mkdir()
    (destination / "existing.md").write_text("already here\n")

    with pytest.raises(ReceiveDestinationNotEmpty) as caught:
        receive_bundle(
            bundle_dir,
            destination,
            signature=UNSIGNED_REHEARSAL,
        )

    assert caught.value.code == "receive_destination_not_empty"
    assert (destination / "existing.md").read_text() == "already here\n"
    assert sorted(p.name for p in destination.iterdir()) == ["existing.md"]


def test_receive_refuses_destination_that_is_a_file(tmp_path: Path) -> None:
    bundle_dir = _bundle(tmp_path)
    destination = tmp_path / "target-home"
    destination.write_text("not a directory\n")

    with pytest.raises(ReceiveDestinationNotEmpty):
        receive_bundle(
            bundle_dir,
            destination,
            signature=UNSIGNED_REHEARSAL,
        )

    assert destination.read_text() == "not a directory\n"


def test_receive_refuses_actor_mismatch(tmp_path: Path) -> None:
    bundle_dir = _bundle(tmp_path)
    destination = tmp_path / "target-home"

    with pytest.raises(ReceiveActorMismatch) as caught:
        receive_bundle(
            bundle_dir,
            destination,
            expect_actor="codex-other",
            signature=UNSIGNED_REHEARSAL,
        )

    assert caught.value.code == "receive_actor_mismatch"
    assert not destination.exists()


def test_receive_refuses_tampered_bundle_before_writing(tmp_path: Path) -> None:
    bundle_dir = _bundle(tmp_path)
    (bundle_dir / "payload" / "notes.md").write_text("tampered\n")
    destination = tmp_path / "target-home"

    with pytest.raises(BundleTamperError):
        receive_bundle(
            bundle_dir,
            destination,
            signature=UNSIGNED_REHEARSAL,
        )

    assert not destination.exists()


def test_receive_failure_undoes_the_created_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_dir = _bundle(tmp_path)
    destination = tmp_path / "target-home"

    import hyprial.daemon.impl.transfer.landing.receive as receive_module

    real_copy = receive_module.shutil.copyfile
    calls = {"count": 0}

    def flaky_copy(source, target, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("simulated disk failure")
        return real_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(receive_module.shutil, "copyfile", flaky_copy)

    with pytest.raises(ReceiveFailed) as caught:
        receive_bundle(
            bundle_dir,
            destination,
            signature=UNSIGNED_REHEARSAL,
        )

    assert caught.value.code == "receive_failed"
    assert not destination.exists()


def test_receive_failure_keeps_a_prepared_empty_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_dir = _bundle(tmp_path)
    destination = tmp_path / "prepared-home"
    destination.mkdir()

    import hyprial.daemon.impl.transfer.landing.receive as receive_module

    def always_fail(source, target, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated disk failure")

    monkeypatch.setattr(receive_module.shutil, "copyfile", always_fail)

    with pytest.raises(ReceiveFailed):
        receive_bundle(
            bundle_dir,
            destination,
            signature=UNSIGNED_REHEARSAL,
        )

    assert destination.is_dir()
    assert list(destination.iterdir()) == []


# -- production entry points -------------------------------------------------


def test_cli_receive_roundtrip(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    source = _source_home(tmp_path)
    bundle_dir = tmp_path / "cli-bundle"
    destination = tmp_path / "cli-target"
    runner = CliRunner()

    exported = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "bundle-export",
            "--from",
            str(source),
            "--to",
            str(bundle_dir),
            "--owner",
            "owner@example.com",
            "--actor",
            "codex-sw",
            "--machine",
            "devusr-dev",
        ],
    )
    assert exported.exit_code == 0, exported.output
    # The CLI export stamps the bundle with this machine's own key; the same
    # home receives, so it has to know that key -- exactly the enrollment an
    # operator does on the target before a real transfer.
    enroll_machine_key(actor="codex-sw", source_machine="devusr-dev")

    received = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "bundle-receive",
            str(bundle_dir),
            "--to",
            str(destination),
            "--expect-actor",
            "codex-sw",
            "--json",
        ],
    )
    assert received.exit_code == 0, received.output
    payload = json.loads(received.stdout)
    assert payload["ok"] is True
    assert payload["files"] == 3
    assert payload["dry_run"] is False
    assert (destination / "notes.md").exists()


def test_cli_receive_dry_run_and_refusal(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    source = _source_home(tmp_path)
    bundle_dir = tmp_path / "cli-bundle"
    destination = tmp_path / "cli-target"
    runner = CliRunner()
    assert (
        runner.invoke(
            cli.app,
            [
                "agent", "transfer", "bundle-export",
                "--from",
                str(source),
                "--to",
                str(bundle_dir),
                "--owner",
                "owner@example.com",
                "--actor",
                "codex-sw",
            ],
        ).exit_code
        == 0
    )
    enroll_machine_key(actor="codex-sw", source_machine=resolve_node_id())

    dry = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "bundle-receive",
            str(bundle_dir),
            "--to",
            str(destination),
            "--dry-run",
            "--json",
        ],
    )
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.stdout)["dry_run"] is True
    assert not destination.exists()

    mismatch = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "bundle-receive",
            str(bundle_dir),
            "--to",
            str(destination),
            "--expect-actor",
            "someone-else",
        ],
    )
    assert mismatch.exit_code != 0
    assert "someone-else" in mismatch.output
    assert not destination.exists()
