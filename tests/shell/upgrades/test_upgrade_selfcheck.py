"""Every upgrade self-checks the installed build and reports it in the result.

R2 (owner ruling 2026-11-19): nothing is sent to any IM. The self-check, the
state chooser and the restart verdict stay; they are observed through the
upgrade result instead of a message.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.shell.impl.cli.commands.common import pending_restart
from hyprial.shell.impl.cli.commands.upgrade import commands as upgrade_commands

import hyprial.daemon.impl.updates as updates
from hyprial.daemon.impl.autoupdate import alert
from hyprial.daemon.impl.autoupdate.alert import run_self_check
from hyprial.kernel import DaemonLaunchResult
from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import subprocess as _cli_subprocess
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _locked_constraints(monkeypatch: pytest.MonkeyPatch) -> None:
    def export_constraints(
        url: str, commit: str, destination: Path, *, runner: object = None
    ) -> Path:
        destination.write_text("anyio==4.15.1\n", encoding="utf-8")
        return destination

    monkeypatch.setattr(updates, "export_locked_constraints", export_constraints)


def _home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir(parents=True)
    state.mkdir(parents=True)
    return home, state


def test_a_restart_that_never_serves_is_a_restart_failure() -> None:
    """The serving-boundary timeout IS the failure, asserted on the source.

    This test replaces its own opposite: the still-starting branch it used to
    pin existed because restore sat inside the readiness budget, so a healthy
    but slow fleet measured as "never became ready" (161s against 90s,
    2026-08-31).  With restore on the daemon's own thread, the wait covers
    only bind + accept + ping -- exhausting it means the new daemon never
    came up, and the restart-failed path (marker + failure result) is the
    honest report.  The behavioural half lives in test_cli_init.py, which drives a
    timeout through `_launch_daemon_process`; what is pinned here is that the
    third verdict and its fleet-scaled budget do not come back.
    """

    source = (
        Path(pending_restart.__file__)
    ).read_text("utf-8")
    assert "stillStarting" not in source, (
        "the still-starting verdict is back; a launch timeout is a startup "
        "failure now, because restore no longer sits inside the readiness wait"
    )
    assert "ready_timeout=90.0" not in source, (
        "the restart's wait is a fleet-scaled budget again; it must be the "
        "small serving-boundary budget"
    )
    assert "ready_timeout=DAEMON_START_HARD_CAP_SECONDS" in source, (
        "the upgrade restart no longer observes a live child through the "
        "named absolute startup bound"
    )


def test_the_reporter_prefers_unconfirmed_over_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real path, not source order: a still-restoring daemon reaches
    UPGRADE_UNCONFIRMED.

    The previous body asserted the *order of return statements* in
    ``_action_for`` (``UPGRADE_UNCONFIRMED`` before ``UPGRADE_INSTALLED``)
    and stayed green while the branch was unreachable on intg -- the write
    point for ``confirmed: False`` had been deleted with the 90s wall clock
    (F2/U7), so nothing could ever produce the state the chooser prefers.
    Order pinned on dead code proves nothing about reachability.

    This drives the actual upgrade flow with only the daemon boundary
    stubbed: launch answers the typed result and ping answers the daemon's
    real restoring shape. Restore still in progress (``phase=restoring``)
    must be recorded as unconfirmed and closed by the same-process follow-up,
    never as the all-clear nobody verified. R2: nothing is sent to any IM, so
    the result carries the self-check and no owner-alert field.
    """

    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "state"))
    # Guard 3's positive shape: this process sits inside the tool directory
    # uv would write, so the upgrade under test reaches its (faked) install.
    monkeypatch.setenv("UV_TOOL_DIR", str(Path(sys.prefix).resolve().parent))
    monkeypatch.delenv("HYPRIAL_SOCKET_PATH", raising=False)
    monkeypatch.delenv("HARNESS_CONFIG", raising=False)
    _home(tmp_path)

    before = updates.Installation(
        version="0.4.0.dev1",
        commit="a" * 40,
        requested_revision="v0.4.0.dev1",
        url="ssh://git@example.test/hyprial.git",
    )
    after = updates.Installation(
        version="0.4.0.dev2",
        commit="d" * 40,
        requested_revision="v0.4.0.dev2",
        url="ssh://git@example.test/hyprial.git",
    )
    installations = [before, after]
    monkeypatch.setattr(
        updates,
        "read_installation",
        lambda: installations.pop(0) if installations else after,
    )
    monkeypatch.setattr(
        updates,
        "resolve_remote",
        lambda *_args, **_kwargs: updates.RemoteResolution(
            tag="v0.4.0.dev2", commit="d" * 40, version="0.4.0.dev2"
        ),
    )
    monkeypatch.setattr(_cli_get_services(), "_prime_post_install_restart_imports", lambda: None)
    monkeypatch.setattr(
        _cli_subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, "installed\n", ""),
    )

    # The daemon's real ping shape mid-restore (application.py's ping
    # handler): flat, `phase` carries the restore thread's state. Not a
    # hand-invented form -- this is the contract the daemon publishes.
    restoring_ping = {
        "running": True,
        "pid": 4202,
        "epoch": "new",
        "version": "0.4.0.dev2",
        "phase": "restoring",
        "restorePending": True,
    }

    lifecycle = {"stopped": False}


    def daemon_request(
        method: str,
        params: dict[str, object] | None = None,
        *,
        timeout: float = 15.0,
        restore_wait: float | None = None,
    ) -> dict[str, object]:
        del params, restore_wait
        if method == "ps":
            return {"daemon": {"running": True, "pid": 4101, "epoch": "old"}}
        if method == "shutdown":
            lifecycle["stopped"] = True
            return {"stopping": True, "pid": 4101}
        if method == "ping":
            # Card b9872e94: the pre-restart snapshot asks ping, so answer by
            # phase -- the old generation until shutdown, the fresh one after.
            if not lifecycle["stopped"]:
                return {
                    "running": True,
                    "pid": 4101,
                    "epoch": "old",
                    "version": "0.4.0.dev1",
                    "phase": "reconciled",
                }
            return dict(restoring_ping)
        raise AssertionError(f"unexpected daemon request: {method}")

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)
    # The fake old daemon exits once asked to; never probe the real pid 4101
    # (a live unrelated process there costs the full stop budget -- #1149).
    monkeypatch.setattr(
        _cli_get_services(), "_stopped_process", lambda _pid, _identity: lifecycle["stopped"]
    )
    def restart_daemon(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        lifecycle["stopped"] = True
        return DaemonLaunchResult.launched(restoring_ping)

    monkeypatch.setattr(pending_restart, "restart_daemon", restart_daemon)

    result = _cli_get_services()._perform_upgrade_and_report(force=False)

    restart = result["restart"]
    assert restart.get("confirmed") is False, (
        "a daemon still mid-restore must be recorded as unconfirmed -- an "
        "absent key is exactly the dead-reader regression"
    )
    assert "selfCheck" in result
    assert "ownerAlert" not in result
    follow_up = restart.get("followUp")
    assert isinstance(follow_up, dict) and "alert" not in follow_up
    assert not hasattr(alert, "notify_upgrade_outcome")


def test_the_call_site_reads_what_happened_instead_of_asserting_it() -> None:
    """⚠️ Asserted on the source, because the defect was a literal.

    The old call site passed `upgraded=True` on the whole success path. A
    constant reintroduced there would decide the follow-up poll on a state
    nobody read off the result.
    """

    source = (
        Path(upgrade_commands.__file__)
    ).read_text("utf-8")
    tree = ast.parse(source)
    chooser = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_action_for"
    )
    read_keys = {
        node.args[0].value
        for node in ast.walk(chooser)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    assert {"upgraded", "declinedDowngrade"} <= read_keys, (
        "the state is no longer read off the upgrade result; a constant here "
        "is how the no-op path came to announce a completed upgrade"
    )


# ---------------------------------------------------------------------------
# The self-check itself.
# ---------------------------------------------------------------------------


def test_the_self_check_runs_the_installed_binary_not_this_process(
    tmp_path: Path,
) -> None:
    """⚠️ Checking `import hyprial` from in here would check the wrong artefact.

    `autoupdate` deliberately preloads the pre-upgrade modules so the restart
    survives the distribution being replaced. So this process holds the OLD
    code; asking it whether hyprial imports would pass however broken the new build
    is. The check has to run the binary on disk.
    """

    seen: list[list[str]] = []

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"ok": True, "packageVersion": "0.4.3"}), ""
        )

    result = run_self_check(executable=tmp_path / "hyprial", runner=runner)
    assert result.ok is True
    assert seen == [[str(tmp_path / "hyprial"), "version", "--json"]]


def test_a_build_that_cannot_start_fails_the_self_check() -> None:
    """The 15:17 shape: installed, and dead on import."""

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 1, "", "ImportError: cannot import name 'updates' from 'hyprial'"
        )

    result = run_self_check(executable=Path("/nonexistent/hyprial"), runner=runner)
    assert result.ok is False
    assert "cannot import name" in result.detail


def test_a_self_check_that_cannot_run_is_not_a_pass() -> None:
    """"Could not check" must not read as "checked and fine"."""

    def runner(command: list[str]) -> subprocess.CompletedProcess[str]:
        raise OSError("no such file")

    result = run_self_check(executable=Path("/nonexistent/hyprial"), runner=runner)
    assert result.ok is False
    assert "could not run" in result.detail


# ---------------------------------------------------------------------------
# ⭐ Control group ④: the report must never change the upgrade's outcome.
# ---------------------------------------------------------------------------


def test_the_report_cannot_turn_a_completed_upgrade_into_a_failure() -> None:
    """"We could not verify" and "it did not work" are different facts.

    Conflating them sends someone to re-install something that installed fine.
    Checked structurally: the wrapper re-raises what `_perform_upgrade` raised
    and swallows anything the reporting itself throws -- so no failure can
    originate here.
    """

    source = (
        Path(upgrade_commands.__file__)
    ).read_text("utf-8")
    tree = ast.parse(source)
    wrapper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_perform_upgrade_and_report"
    )
    raises = [n for n in ast.walk(wrapper) if isinstance(n, ast.Raise)]
    assert raises, "the wrapper must re-raise the upgrade's own error"
    assert all(n.exc is None for n in raises), (
        "the wrapper raises an error of its own; reporting must not be able to "
        "fail an upgrade that completed"
    )
    handlers = [n for n in ast.walk(wrapper) if isinstance(n, ast.ExceptHandler)]
    assert len(handlers) >= 2, (
        "both the failure and the success report must be individually guarded"
    )


def test_importing_the_cli_does_not_drag_in_the_daemon_package() -> None:
    """⭐ A one-line import cost every `hyprial` invocation 1.2 seconds.

    The post-install primer imports the self-check before the install boundary
    (pinned by the test below),
    and URI parsing now comes from the dependency-free `hyprial.uri` leaf rather
    than executing the eager `hyprial.daemon` package. The old import path took
    `import hyprial.cli` from 63ms to 1266ms -- paid by every CLI call, including
    `daemon stop`, whose IPC budget is 2 seconds. Nothing raised; the cost is
    the whole of the symptom.

    ⚠️ A full run at that commit also failed `test_e2e009_controlled` on a
    `daemon stop` IPC timeout. Whether this import caused it is **open**, and
    the check that looked like it settled the question did not:

        the failure appeared in a 24-minute full run -- a saturated machine
        the mutation re-ran that file alone -- an idle one

    The IPC budget is a fixed 2s. Idle, both 56ms and 1266ms clear it by a mile,
    so "green with and without the import" could not have come out any other
    way. ⛔ Zero discriminating power: it did not exonerate the import, it only
    showed that an idle machine has room to spare. Under load that 1.2s is
    taken out of an already-squeezed budget -- and even idle, the with-import
    run was 13% slower (45.8s vs 40.5s).

    ⇒ Established: "no causal link **under isolation**". Not established: "no
    causal link". The honest resolution is to run both directions under load.

    ⇒ The measurement below stands on its own: a 20x startup regression on
    every invocation is worth refusing whether or not it broke that test.
    Asserted as a structural fact rather than a timing budget -- a budget would
    be flaky, and the thing that went wrong is not "slow", it is "the daemon
    package is now on the startup path".
    """

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import hyprial.cli, sys; print('hyprial.daemon' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        cwd=ROOT,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "False", (
        "importing hyprial.cli now executes the hyprial.daemon package, which adds "
        "~1.2s to every hyprial invocation; import it lazily and prime it in "
        "_prime_post_install_restart_imports instead"
    )


def test_the_self_check_is_imported_before_the_upgrade_replaces_the_files() -> None:
    """⚠️ Where the module lives does not matter; when it is imported does.

    The upgrade replaces the installed distribution underneath this process, so
    anything imported *after* that point may not be there. The self-check
    survives because the post-install primer imports it first. Moving that
    import later would keep every test above green and make the self-check
    vanish exactly when it is needed -- so the import site is pinned here.
    """

    tree = ast.parse(
        (
            ROOT / "src/hyprial/shell/impl/upgrade_bootstrap.py"
        ).read_text("utf-8")
    )
    primer = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "prime_post_install_imports"
    )
    primed = {
        alias.name
        for node in ast.walk(primer)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert "run_self_check" in primed, (
        "the self-check is no longer primed before the install boundary; after "
        "`uv tool install` swaps the distribution a lazy import can fail, and "
        "it would fail precisely on the upgrade that needed to report "
        "something"
    )
