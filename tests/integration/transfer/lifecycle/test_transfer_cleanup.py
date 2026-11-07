"""AT11: transfer-out completion — fence, cleanup, receipt (red-first mutants)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.transfer.archive.bundle import export_bundle
from hyprial.daemon.impl.transfer.cleanup import (
    CleanupFenceError,
    CleanupNotConfirmed,
    CleanupUncoveredFiles,
    execute_cleanup,
    plan_cleanup)

CREATED_AT = "2026-09-30T23:10:00+08:00"


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "agents" / "codex-sw"
    (home / "sessions").mkdir(parents=True)
    (home / "sessions" / "session-1.json").write_text('{"title": "demo"}\n')
    (home / "notes.md").write_text("payload\n")
    return home


def _bundle(tmp_path: Path, home: Path) -> Path:
    return _bundle_named(tmp_path, home, name="bundle", actor="codex-sw")


def _bundle_named(tmp_path: Path, home: Path, *, name: str, actor: str) -> Path:
    bundle_dir = tmp_path / name
    export_bundle(
        home,
        bundle_dir,
        owner="owner@example.com",
        actor=actor,
        source_machine="devusr-dev",
        created_at=CREATED_AT,
    )
    return bundle_dir


def test_plan_lists_the_home_and_declared_roots(tmp_path: Path) -> None:
    home = _home(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "partial.bin").write_bytes(b"x" * 16)
    grants = tmp_path / "private-grants.json"
    grants.write_text("{}\n")

    plan = plan_cleanup(
        home,
        actor="codex-sw",
        owner="owner@example.com",
        staging=[staging],
        private_grants=[grants],
        # These are arbitrary tmp dirs, not roots the daemon creates for the
        # actor, so the fence requires the explicit release (see the
        # not-this-actor's tests below).
        allow_external=True,
    )

    kinds = {item.kind for item in plan.items}
    assert kinds == {"agent-home", "staging", "private-grant"}
    assert plan.byte_total >= 16
    assert plan.as_dict()["erasure"].startswith("removing these paths does not erase")


def test_plan_refuses_the_operators_own_home(tmp_path: Path) -> None:
    with pytest.raises(CleanupFenceError) as caught:
        plan_cleanup(Path.home(), actor="codex-sw", owner="owner@example.com")
    assert caught.value.code == "cleanup_fence_refused"


@pytest.mark.parametrize("name", ["inbox", "INBOX"])
def test_plan_refuses_inbox_paths(tmp_path: Path, name: str) -> None:
    home = _home(tmp_path)
    inbox = tmp_path / name
    inbox.mkdir()

    with pytest.raises(CleanupFenceError) as caught:
        plan_cleanup(
            home,
            actor="codex-sw",
            owner="owner@example.com",
            staging=[inbox],
            # Release the external-root rule so this test isolates the P0
            # inbox rule; on APFS ``INBOX`` names the same directory.
            allow_external=True,
        )
    assert "P0 inbox" in str(caught.value)


def test_plan_refuses_a_symlinked_root(tmp_path: Path) -> None:
    home = _home(tmp_path)
    real = tmp_path / "real-staging"
    real.mkdir()
    link = tmp_path / "staging-link"
    link.symlink_to(real)

    with pytest.raises(CleanupFenceError):
        plan_cleanup(
            home,
            actor="codex-sw",
            owner="owner@example.com",
            staging=[link],
        )


def test_execute_requires_a_target_receipt(tmp_path: Path) -> None:
    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    plan = plan_cleanup(home, actor="codex-sw", owner="owner@example.com")

    with pytest.raises(CleanupNotConfirmed) as caught:
        execute_cleanup(plan, target_receipt="", bundle_dir=bundle_dir)

    assert caught.value.code == "cleanup_not_confirmed"
    assert home.exists()


def test_execute_requires_a_valid_fallback_bundle(tmp_path: Path) -> None:
    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    plan = plan_cleanup(home, actor="codex-sw", owner="owner@example.com")

    with pytest.raises(CleanupNotConfirmed):
        execute_cleanup(plan, target_receipt="target-ok-1", bundle_dir=tmp_path / "missing")

    (bundle_dir / "payload" / "notes.md").write_text("tampered\n")
    with pytest.raises(CleanupNotConfirmed):
        execute_cleanup(plan, target_receipt="target-ok-1", bundle_dir=bundle_dir)

    assert home.exists()


def test_dry_run_removes_nothing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    staging = tmp_path / "staging"
    staging.mkdir()
    plan = plan_cleanup(
        home,
        actor="codex-sw",
        owner="owner@example.com",
        staging=[staging],
        allow_external=True,
    )

    receipt = execute_cleanup(
        plan, target_receipt="target-ok-1", bundle_dir=bundle_dir, dry_run=True
    )

    assert receipt.dry_run is True
    assert receipt.complete is False
    assert sorted(receipt.leftover) == sorted([str(home), str(staging)])
    assert receipt.removed == ()
    assert home.exists() and staging.exists()


def test_cli_complete_dry_run_still_refuses_without_a_fallback_bundle(
    tmp_path: Path,
) -> None:
    """The other half of kanban 61364374: the preview is not a fake green.

    Exit 0 is for a preview of a run that could actually proceed; a preview
    whose fallback bundle cannot be validated still refuses, so ``--dry-run``
    is never the way past the confirmation the real run enforces.
    """

    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    home = _home(tmp_path)
    runner = CliRunner()

    dry = runner.invoke(
        cli.app,
        [
            "agent",
            "transfer",
            "bundle-complete",
            str(home),
            "--bundle",
            str(tmp_path / "not-a-bundle"),
            "--receipt",
            "target-ok-1",
            "--actor",
            "codex-sw",
            "--owner",
            "owner@example.com",
            "--dry-run",
            "--json",
        ],
    )

    assert dry.exit_code != 0
    assert json.loads(dry.stdout)["code"] == "cleanup_not_confirmed"
    assert home.exists()


def test_execute_removes_the_source_and_reports(tmp_path: Path) -> None:
    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "partial.bin").write_bytes(b"x" * 8)
    plan = plan_cleanup(
        home,
        actor="codex-sw",
        owner="owner@example.com",
        staging=[staging],
        allow_external=True,
    )

    receipt = execute_cleanup(plan, target_receipt="target-ok-1", bundle_dir=bundle_dir)

    assert receipt.complete is True
    assert set(receipt.removed) == {str(home), str(staging)}
    assert not home.exists() and not staging.exists()
    payload = receipt.as_dict()
    assert payload["receipt"] == "target-ok-1"
    assert "does not erase" in payload["erasure"]


def test_a_failed_removal_is_reported_and_resumable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hyprial.daemon.impl.transfer.cleanup as cleanup_module

    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    plan = plan_cleanup(home, actor="codex-sw", owner="owner@example.com")

    real_rmtree = cleanup_module.shutil.rmtree

    def refuse(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if Path(path) == home:
            raise OSError("device busy")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(cleanup_module.shutil, "rmtree", refuse)

    first = execute_cleanup(plan, target_receipt="target-ok-1", bundle_dir=bundle_dir)
    assert first.complete is False
    assert first.leftover == (str(home),)
    assert first.failed and first.failed[0][1] == "device busy"
    assert home.exists()

    monkeypatch.setattr(cleanup_module.shutil, "rmtree", real_rmtree)
    second = execute_cleanup(plan, target_receipt="target-ok-1", bundle_dir=bundle_dir)
    assert second.complete is True
    assert not home.exists()


# -- production entry points -------------------------------------------------


def test_cli_complete_dry_run_and_success(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    runner = CliRunner()

    dry = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "bundle-complete",
            str(home),
            "--bundle",
            str(bundle_dir),
            "--receipt",
            "target-ok-1",
            "--actor",
            "codex-sw",
            "--owner",
            "owner@example.com",
            "--dry-run",
            "--json",
        ],
    )
    # Kanban 61364374: a preview answers with the PLAN -- exit 0 plus the
    # paths that would be removed -- instead of reusing the real run's
    # failure code (ok:false/cleanup_incomplete was the fake-red shape).
    assert dry.exit_code == 0, dry.output
    body = json.loads(dry.stdout)
    assert body["dry_run"] is True
    assert body["complete"] is False  # a preview completes nothing
    assert body["removed"] == []
    assert body["leftover"] == [str(home)]
    assert body["registration"] == {"removed": False, "reason": "dry-run"}
    assert home.exists()

    done = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "bundle-complete",
            str(home),
            "--bundle",
            str(bundle_dir),
            "--receipt",
            "target-ok-1",
            "--actor",
            "codex-sw",
            "--owner",
            "owner@example.com",
            "--json",
        ],
    )
    assert done.exit_code == 0, done.output
    assert json.loads(done.stdout)["complete"] is True
    assert not home.exists()


def test_cli_complete_refuses_without_a_receipt(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from hyprial.shell.impl.cli.runtime import entry as cli

    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    runner = CliRunner()

    refused = runner.invoke(
        cli.app,
        [
            "agent", "transfer", "bundle-complete",
            str(home),
            "--bundle",
            str(bundle_dir),
            "--receipt",
            "",
            "--actor",
            "codex-sw",
            "--owner",
            "owner@example.com",
        ],
    )
    assert refused.exit_code != 0
    assert home.exists()


def test_plan_refuses_an_external_root_that_contains_the_operator_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    operator_home = tmp_path / "operator"
    (operator_home / "work").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(operator_home))
    home = tmp_path / "agents" / "codex-sw"
    home.mkdir(parents=True)
    (home / "notes.md").write_text("payload\n")

    # The reviewer's repro: --staging pointed at the operator's own home used to
    # be accepted just because it was declared.
    with pytest.raises(CleanupFenceError) as caught:
        plan_cleanup(
            home,
            actor="codex-sw",
            owner="owner@example.com",
            staging=[operator_home],
        )
    assert caught.value.code == "cleanup_fence_refused"

    with pytest.raises(CleanupFenceError):
        plan_cleanup(
            home,
            actor="codex-sw",
            owner="owner@example.com",
            credential_volumes=[tmp_path],
        )


def test_plan_refuses_an_external_root_above_the_agent_home(tmp_path: Path) -> None:
    home = tmp_path / "agents" / "codex-sw"
    home.mkdir(parents=True)
    (home / "notes.md").write_text("payload\n")
    other = tmp_path / "agents" / "other-agent"
    other.mkdir()

    with pytest.raises(CleanupFenceError):
        plan_cleanup(
            home,
            actor="codex-sw",
            owner="owner@example.com",
            staging=[tmp_path / "agents"],
        )

    assert other.exists()


def test_execute_refuses_a_bundle_from_another_actor(tmp_path: Path) -> None:
    alice_home = tmp_path / "agents" / "alice"
    (alice_home / "sessions").mkdir(parents=True)
    (alice_home / "sessions" / "s1.json").write_text("{}\n")
    (alice_home / "notes.md").write_text("alice\n")
    alice_bundle = _bundle_named(tmp_path, alice_home, name="alice-bundle", actor="alice")

    bob_home = tmp_path / "agents" / "bob"
    (bob_home / "sessions").mkdir(parents=True)
    (bob_home / "sessions" / "s1.json").write_text("{}\n")
    (bob_home / "notes.md").write_text("bob\n")

    plan = plan_cleanup(bob_home, actor="bob", owner="owner@example.com")

    with pytest.raises(CleanupNotConfirmed) as caught:
        execute_cleanup(plan, target_receipt="target-ok-1", bundle_dir=alice_bundle)

    assert "belongs to" in str(caught.value)
    assert bob_home.exists()


def test_execute_refuses_a_bundle_taken_from_a_different_home(tmp_path: Path) -> None:
    first_home = tmp_path / "agents" / "codex-sw"
    (first_home / "sessions").mkdir(parents=True)
    (first_home / "sessions" / "s1.json").write_text("{}\n")
    (first_home / "notes.md").write_text("first\n")
    bundle_dir = _bundle(tmp_path, first_home)

    # Same actor and owner, but this home never produced that bundle: the
    # files the manifest records are simply not here.
    second_home = tmp_path / "agents" / "codex-sw-copy"
    (second_home / "sessions").mkdir(parents=True)
    (second_home / "sessions" / "s1.json").write_text("{}\n")

    plan = plan_cleanup(second_home, actor="codex-sw", owner="owner@example.com")

    with pytest.raises(CleanupNotConfirmed) as caught:
        execute_cleanup(plan, target_receipt="target-ok-1", bundle_dir=bundle_dir)

    assert "was not taken from here" in str(caught.value)
    assert second_home.exists()

    # A file that exists but no longer matches the manifest is refused too.
    (second_home / "notes.md").write_text("second\n")
    with pytest.raises(CleanupNotConfirmed) as caught_again:
        execute_cleanup(plan, target_receipt="target-ok-1", bundle_dir=bundle_dir)

    assert "no longer matches the bundle manifest" in str(caught_again.value)
    assert second_home.exists()


def test_uncovered_files_stop_the_removal(tmp_path: Path) -> None:
    # The reviewer's repro: a file written after the export is not in the
    # bundle, so deleting the home would delete its only copy.
    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    (home / "written-after-export.md").write_text("the only copy\n")
    plan = plan_cleanup(home, actor="codex-sw", owner="owner@example.com")

    with pytest.raises(CleanupUncoveredFiles) as caught:
        execute_cleanup(plan, target_receipt="target-ok-1", bundle_dir=bundle_dir)

    assert caught.value.code == "cleanup_uncovered_files"
    assert "written-after-export.md" in str(caught.value)
    assert home.exists()
    assert (home / "written-after-export.md").read_text() == "the only copy\n"


def test_dry_run_lists_uncovered_files_without_removing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    (home / "late-note.md").write_text("written after the bundle\n")
    plan = plan_cleanup(home, actor="codex-sw", owner="owner@example.com")

    receipt = execute_cleanup(
        plan, target_receipt="target-ok-1", bundle_dir=bundle_dir, dry_run=True
    )

    assert receipt.dry_run is True
    assert receipt.uncovered == ("late-note.md",)
    assert receipt.as_dict()["uncovered"] == ["late-note.md"]
    assert home.exists()


def test_allow_uncovered_deletes_files_the_bundle_does_not_hold(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    (home / "late-note.md").write_text("written after the bundle\n")
    plan = plan_cleanup(home, actor="codex-sw", owner="owner@example.com")

    receipt = execute_cleanup(
        plan,
        target_receipt="target-ok-1",
        bundle_dir=bundle_dir,
        allow_uncovered=True,
    )

    assert receipt.complete is True
    assert receipt.uncovered == ("late-note.md",)
    assert not home.exists()


def test_an_empty_bundle_cannot_stand_in_for_a_copy(tmp_path: Path) -> None:
    # An empty export binds to any home of the same actor; it is refused even
    # with the explicit override.
    empty_home = tmp_path / "agents" / "empty"
    empty_home.mkdir(parents=True)
    bundle_dir = _bundle_named(
        tmp_path, empty_home, name="empty-bundle", actor="bob"
    )

    bob_home = tmp_path / "agents" / "bob"
    (bob_home / "sessions").mkdir(parents=True)
    (bob_home / "real.md").write_text("the only copy\n")
    plan = plan_cleanup(bob_home, actor="bob", owner="owner@example.com")

    with pytest.raises(CleanupUncoveredFiles) as caught:
        execute_cleanup(
            plan,
            target_receipt="target-ok-1",
            bundle_dir=bundle_dir,
            allow_uncovered=True,
        )

    assert caught.value.code == "cleanup_uncovered_files"
    assert bob_home.exists()
    assert (bob_home / "real.md").read_text() == "the only copy\n"


def _case_insensitive_filesystem(directory: Path) -> bool:
    probe = directory / "case-probe"
    probe.mkdir()
    try:
        return probe.with_name("CASE-PROBE").exists()
    finally:
        probe.rmdir()


def test_a_case_variant_of_the_agent_home_is_refused(tmp_path: Path) -> None:
    """AVFS/NTFS name one directory two ways; the fence must not be fooled.

    cc-sw-review's repro (2026-09-30): ``--staging <tmp>/FH`` with the real home
    ``<tmp>/fh`` deleted the home's own files.
    """

    if not _case_insensitive_filesystem(tmp_path):
        pytest.skip("this filesystem is case-sensitive; the case variant is a different path")

    home = tmp_path / "fh"
    home.mkdir()
    keep = home / "keep.txt"
    keep.write_text("must survive\n")
    variant = tmp_path / "FH"
    assert variant.exists(), "the case-insensitive probe disagrees with itself"

    with pytest.raises(CleanupFenceError) as caught:
        plan_cleanup(
            home,
            actor="codex-sw",
            owner="owner@example.com",
            staging=[variant],
            allow_external=True,  # even a released root may not be the home
        )
    assert caught.value.code == "cleanup_fence_refused"
    assert keep.read_text() == "must survive\n"


def test_an_external_root_the_actor_does_not_own_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    home = _home(tmp_path)
    documents = tmp_path / "Documents"
    documents.mkdir()
    (documents / "taxes.pdf").write_bytes(b"not the daemon's\n")

    with pytest.raises(CleanupFenceError) as caught:
        plan_cleanup(
            home,
            actor="codex-sw",
            owner="owner@example.com",
            staging=[documents],
        )
    assert "not this actor's" in str(caught.value)
    assert (documents / "taxes.pdf").exists()

    # The operator can still release it deliberately, and the receipt says so.
    plan = plan_cleanup(
        home,
        actor="codex-sw",
        owner="owner@example.com",
        staging=[documents],
        allow_external=True,
    )
    notes = {item.path: item.note for item in plan.items}
    assert "allow-external" in notes[documents]


def test_a_staging_root_the_daemon_creates_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
    home = _home(tmp_path)
    from hyprial.daemon.impl.transfer.execution.container import staging_dir

    owned = staging_dir(state, "codex-sw")
    owned.mkdir(parents=True)
    (owned / "bundle.tar").write_bytes(b"x" * 4)

    plan = plan_cleanup(
        home, actor="codex-sw", owner="owner@example.com", staging=[owned]
    )

    assert any(item.path == owned and item.kind == "staging" for item in plan.items)

# -- AT11 leave-half: a completed transfer-out also leaves the registration ---


class _FakeCliError(Exception):
    """The CLI's error shape, reduced to what these tests read."""

    def __init__(self, code: str, message: str, payload: object = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = payload


class _RecordingDependencies:
    """Stands in for ``TransferCliDependencies`` and records the IPC it sees."""

    def __init__(self, *, destroy_error: Exception | None = None) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.results: list[dict[str, object]] = []
        self.failures: list[_FakeCliError] = []
        self._destroy_error = destroy_error
        self.error_type = _FakeCliError

    def daemon_request(self, method: str, params=None, **kwargs) -> object:
        self.requests.append((method, dict(params or {})))
        if self._destroy_error is not None:
            raise self._destroy_error
        return {
            "ok": True,
            "destroyed": True,
            "actor": f"agent:owner@example.com:machine:{(params or {}).get('name')}",
            "stopped": [],
            "destroyedMessages": 0,
        }

    def execute(self, operation, *, json_output, json_indent=None, allow_missing_home=False):
        try:
            self.results.append(operation())
        except _FakeCliError as error:
            self.failures.append(error)


def _complete_app(dependencies: _RecordingDependencies):
    import typer

    from hyprial.shell.impl.cli.commands.transfer import bundle as bundle_commands

    app = typer.Typer()
    bundle_commands.register_bundle_complete_command(app, lambda: dependencies)
    return app


def _complete_args(home: Path, bundle_dir: Path) -> list[str]:
    # One command on the app: Typer collapses it to single-command mode, so the
    # command name is not part of argv (the real CLI registers it in a group).
    return [
        str(home),
        "--bundle",
        str(bundle_dir),
        "--receipt",
        "target-ok-1",
        "--actor",
        "codex-sw",
        "--owner",
        "owner@example.com",
        "--json",
    ]


def test_cli_complete_deregisters_the_source(tmp_path: Path) -> None:
    """Cleaning the files must also clear the row: that is "it moved away"."""

    from typer.testing import CliRunner

    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    dependencies = _RecordingDependencies()

    result = CliRunner().invoke(
        _complete_app(dependencies), _complete_args(home, bundle_dir)
    )

    assert result.exit_code == 0, result.output
    assert dependencies.failures == []
    assert dependencies.requests == [("agent.destroy", {"name": "codex-sw"})]
    payload = dependencies.results[-1]
    assert payload["complete"] is True
    assert payload["registration"]["removed"] is True
    assert not home.exists()


def test_cli_complete_keeps_the_registration_on_request(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    dependencies = _RecordingDependencies()

    result = CliRunner().invoke(
        _complete_app(dependencies),
        [*_complete_args(home, bundle_dir), "--keep-registration"],
    )

    assert result.exit_code == 0, result.output
    assert dependencies.requests == []
    assert dependencies.results[-1]["registration"] == {
        "removed": False,
        "reason": "kept",
    }


def test_cli_complete_tolerates_a_daemon_it_cannot_reach(tmp_path: Path) -> None:
    """No daemon to ask is not a failure: the cleanup stands and says so."""

    from typer.testing import CliRunner

    from hyprial.kernel import ipc_errors

    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    dependencies = _RecordingDependencies(
        destroy_error=_FakeCliError(ipc_errors.DAEMON_UNAVAILABLE, "no socket")
    )

    result = CliRunner().invoke(
        _complete_app(dependencies), _complete_args(home, bundle_dir)
    )

    assert result.exit_code == 0, result.output
    assert dependencies.failures == []
    registration = dependencies.results[-1]["registration"]
    assert registration["removed"] is False
    assert registration["reason"] == "unverified"
    assert registration["code"] == ipc_errors.DAEMON_UNAVAILABLE
    assert "agent destroy codex-sw" in registration["hint"]


def test_cli_complete_reports_a_registration_that_will_not_come_off(
    tmp_path: Path,
) -> None:
    """Any other refusal is the ghost this step exists to prevent: stay loud."""

    from typer.testing import CliRunner

    from hyprial.kernel import ipc_errors

    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    dependencies = _RecordingDependencies(
        destroy_error=_FakeCliError(ipc_errors.DAEMON_ERROR, "refused")
    )

    result = CliRunner().invoke(
        _complete_app(dependencies), _complete_args(home, bundle_dir)
    )

    assert result.exit_code == 0, result.output  # the fake execute absorbs it
    assert [error.code for error in dependencies.failures] == [
        "registration_not_removed"
    ]
    payload = dependencies.failures[0].data
    assert payload["registration"]["reason"] == "failed"
    assert payload["registration"]["error"] == "refused"


def test_cli_complete_treats_an_unknown_agent_as_already_deregistered(
    tmp_path: Path,
) -> None:
    from typer.testing import CliRunner

    from hyprial.kernel import ipc_errors

    home = _home(tmp_path)
    bundle_dir = _bundle(tmp_path, home)
    dependencies = _RecordingDependencies(
        destroy_error=_FakeCliError(ipc_errors.AGENT_NOT_FOUND, "unknown")
    )

    result = CliRunner().invoke(
        _complete_app(dependencies), _complete_args(home, bundle_dir)
    )

    assert result.exit_code == 0, result.output
    assert dependencies.failures == []
    assert dependencies.results[-1]["registration"] == {
        "removed": False,
        "reason": "not-registered",
    }
