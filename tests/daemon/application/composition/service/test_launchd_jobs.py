"""Which launchd jobs count as THIS install's daemon (identification only).

Split out of test_service_supervision.py, which the 1000-line gate caps.

The identity is the parsed command line, matched exactly -- never a substring:
another install's binary, a different subcommand, ``/bin/echo`` with the same
words, a plist that only mentions the path in metadata, or a wrapper that also
exits must not be claimed. Restarting the wrong job is worse than spawning one.
"""

from __future__ import annotations

import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.configuration.launchd_jobs import argv_runs_daemon

from .test_service_supervision import LaunchdSupervisor, completed, manager


def _foreign_plist(directory: Path, name: str, *, label: str, argv: list[str]) -> Path:
    """Write a launchd plist the way an operator wrapper would look."""

    path = directory / f"{name}.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": argv,
                "KeepAlive": True,
                "RunAtLoad": True,
            }
        )
    )
    return path


def test_launchd_jobs_are_matched_by_resolved_argv_not_substrings(
    tmp_path: Path,
) -> None:
    """Near-misses must not be claimed as this install's supervisor.

    Review probe on c9c51544d: substring matching claimed another install's
    binary (``<exe>-other-install``), ``daemon status run``, ``/bin/echo`` with
    the same words, and a plist that only mentions the path in metadata.
    Restarting any of those is worse than spawning: it stops someone else's
    daemon.
    """

    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    service.unit_path.unlink()
    directory = service.unit_path.parent
    exe = str(service.config.executable.resolve())

    _foreign_plist(
        directory, "other-install", label="other", argv=[f"{exe}-other-install", "daemon", "run"]
    )
    _foreign_plist(directory, "wrong-subcommand", label="wrong", argv=[exe, "daemon", "status", "run"])
    _foreign_plist(directory, "not-hyprial", label="echo", argv=["/bin/echo", exe, "daemon", "run"])
    metadata_only = directory / "metadata.plist"
    metadata_only.write_bytes(
        plistlib.dumps(
            {
                "Label": "metadata",
                "ProgramArguments": ["/usr/bin/true"],
                "Comment": f"points at {exe} daemon run",
            }
        )
    )

    assert service._launchd_jobs() == [], service._launchd_jobs()


def test_launchd_jobs_use_the_plist_label_not_the_file_name(tmp_path: Path) -> None:
    """The label is what launchctl prints; a renamed file must not shadow it."""

    runner = LaunchdSupervisor(job_pid=5150)
    service = manager(tmp_path, runner)
    service.unit_path.unlink()
    exe = str(service.config.executable.resolve())
    _foreign_plist(
        service.unit_path.parent,
        "renamed-file",
        label="com.hyprial.daemon",
        argv=["/bin/bash", "-lc", f"ulimit -n 65536; exec {exe} daemon run --json"],
    )

    jobs = service._launchd_jobs()

    assert [label for label, _path in jobs] == ["com.hyprial.daemon"], jobs


def test_an_unloaded_foreign_job_is_not_a_manager_failure(tmp_path: Path) -> None:
    """A discovered job that is not loaded answers 113 for ITS label."""

    class NotLoadedForItsOwnLabel(LaunchdSupervisor):
        """launchctl names the label it could not find -- so must the fake."""

        def __call__(self, command, *, timeout=None):  # type: ignore[no-untyped-def]
            command = tuple(command)
            if command[:2] == ("launchctl", "print"):
                self.commands.append(command)
                label = command[2].rsplit("/", 1)[-1]
                return completed(
                    command,
                    113,
                    stderr=(
                        f'Could not find service "{label}" in domain for user '
                        f"gui: 501"
                    ),
                )
            return super().__call__(command, timeout=timeout)

    runner = NotLoadedForItsOwnLabel()
    service = manager(tmp_path, runner)
    service.unit_path.unlink()
    exe = str(service.config.executable.resolve())
    _foreign_plist(
        service.unit_path.parent,
        "com.hyprial.daemon",
        label="com.hyprial.daemon",
        argv=["/bin/bash", "-lc", f"exec {exe} daemon run --json"],
    )

    status = service.status()

    assert (status.installed, status.loaded) == (True, False), status


def test_the_lock_wait_reports_through_the_cli_reporter(tmp_path: Path) -> None:
    """The default reporter must actually work: `warn` takes json_output.

    Review B1: `_report_lock_wait` called `warn(message)` without the required
    `json_output`, so the FIRST poll raised TypeError and `daemon run` exited
    instead of waiting -- the fix did not take effect in production. The other
    tests injected `log=`, which is exactly why it slipped through.
    """

    from hyprial.shell.impl.cli.commands.daemon import commands as daemon_commands
    from hyprial.daemon import DaemonOwnershipBusy, DaemonStateOwnershipFence

    attempts = {"count": 0}
    fence = object()

    def fake_acquire(_path, *, timeout: float = 0.0):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise DaemonOwnershipBusy("held by the outgoing daemon")
        return fence

    service = SimpleNamespace(
        _daemon_probe=lambda timeout=0.5: {"daemon": {"pid": 4242}},
        CliError=type("CliError", (Exception,), {}),
    )
    original = DaemonStateOwnershipFence.acquire
    DaemonStateOwnershipFence.acquire = staticmethod(fake_acquire)  # type: ignore[assignment]
    try:
        taken = daemon_commands._acquire_daemon_fence(
            service, tmp_path, timeout=0.0, sleep=lambda _seconds: None
        )
    finally:
        DaemonStateOwnershipFence.acquire = original  # type: ignore[assignment]

    assert taken is fence, taken

def test_a_binary_plist_next_to_ours_does_not_break_status(tmp_path: Path) -> None:
    """Review B5: reading every *.plist as UTF-8 raised UnicodeDecodeError.

    LaunchAgents can hold binary plists (plistlib writes them, and other tools
    do too). Status and restart must ignore a plist they cannot parse instead of
    crashing on it.
    """

    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    directory = service.unit_path.parent
    (directory / "binary-other.plist").write_bytes(
        plistlib.dumps({"Label": "other", "ProgramArguments": ["/usr/bin/true"]}, fmt=plistlib.FMT_BINARY)
    )

    status = service.status()

    assert status.loaded is True, status


def test_installed_reports_the_file_not_an_assumption(tmp_path: Path) -> None:
    """Review B6: a loaded job with no plist file on disk is not "installed".

    The baseline reported installed=False when the canonical label was loaded
    without its file; hardcoding True in the loaded-status builder inverted that.
    """

    class LoadedWithoutFile(LaunchdSupervisor):
        def __call__(self, command, *, timeout=None):  # type: ignore[no-untyped-def]
            return super().__call__(command, timeout=timeout)

    runner = LoadedWithoutFile()
    service = manager(tmp_path, runner)
    service.unit_path.unlink()

    status = service.status()

    assert status.loaded is True, status
    assert status.installed is False, status

def test_program_key_overrides_program_arguments(tmp_path: Path) -> None:
    """Review B7: launchd runs Program, so that is what must be matched.

    A plist with ``Program=/bin/echo`` and arguments that look like ours was
    claimed and kickstarted -- restarting a job that only echoes.
    """

    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    service.unit_path.unlink()
    (service.unit_path.parent / "program-echo.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": "echo-job",
                "Program": "/bin/echo",
                "ProgramArguments": ["/bin/echo", "daemon", "run"],
            }
        )
    )

    assert service._launchd_jobs() == [], service._launchd_jobs()


def test_only_the_operator_ulimit_wrapper_shape_is_unwrapped(tmp_path: Path) -> None:
    """Review B7: ``exit 0;`` or a ``#`` comment must not be unwrapped as ours."""

    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    service.unit_path.unlink()
    exe = str(service.config.executable.resolve())
    directory = service.unit_path.parent
    _foreign_plist(
        directory, "exit-wrapper", label="exit", argv=["/bin/bash", "-lc", f"exit 0; exec {exe} daemon run"]
    )
    _foreign_plist(
        directory, "comment-wrapper", label="comment", argv=["/bin/bash", "-lc", f"# comment; exec {exe} daemon run"]
    )
    production = _foreign_plist(
        directory,
        "com.hyprial.daemon",
        label="com.hyprial.daemon",
        argv=["/bin/bash", "-lc", f"ulimit -n 65536; exec {exe} daemon run --json"],
    )

    assert service._launchd_jobs() == [("com.hyprial.daemon", production)]


def test_a_malformed_plist_does_not_abort_the_scan(tmp_path: Path) -> None:
    """Review B8: one unparseable plist must not crash status()/restart()."""

    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    directory = service.unit_path.parent
    (directory / "broken.plist").write_bytes(b"<plist><dict><key>Label</key>")

    status = service.status()

    assert status.loaded is True, status


def test_the_coexistence_directory_claims_only_the_production_job(
    tmp_path: Path,
) -> None:
    """Bad XML + a Program=/bin/echo job + the production job, side by side."""

    runner = LaunchdSupervisor(job_pid=72472, runs=2900)
    service = manager(tmp_path, runner)
    service.unit_path.unlink()
    exe = str(service.config.executable.resolve())
    directory = service.unit_path.parent
    (directory / "broken.plist").write_bytes(b"<plist><dict>")
    (directory / "program-echo.plist").write_bytes(
        plistlib.dumps(
            {"Label": "echo-job", "Program": "/bin/echo", "ProgramArguments": ["/bin/echo", "daemon", "run"]}
        )
    )
    production = _foreign_plist(
        directory,
        "com.hyprial.daemon",
        label="com.hyprial.daemon",
        argv=["/bin/bash", "-lc", f"ulimit -n 65536; exec {exe} daemon run --json"],
    )

    jobs = service._launchd_jobs()

    assert jobs == [("com.hyprial.daemon", production)], jobs


def test_json_mode_reports_the_wait_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Review B9: production runs `daemon run --json`; the wait was silent.

    stdout is reserved for the single result object, so the per-poll event goes
    to stderr as one JSON line.
    """

    import json

    from hyprial.shell.impl.cli.commands.daemon import commands as daemon_commands
    from hyprial.daemon import DaemonOwnershipBusy, DaemonStateOwnershipFence

    attempts = {"count": 0}
    fence = object()

    def fake_acquire(_path, *, timeout: float = 0.0):
        attempts["count"] += 1
        # One bounded attempt plus two wait-loop polls, then the lock frees.
        if attempts["count"] <= 3:
            raise DaemonOwnershipBusy("held")
        return fence

    service = SimpleNamespace(
        _daemon_probe=lambda timeout=0.5: {"daemon": {"pid": 72472}},
        CliError=type("CliError", (Exception,), {}),
    )
    original = DaemonStateOwnershipFence.acquire
    DaemonStateOwnershipFence.acquire = staticmethod(fake_acquire)  # type: ignore[assignment]
    try:
        daemon_commands._acquire_daemon_fence(
            service,
            tmp_path,
            timeout=0.0,
            sleep=lambda _seconds: None,
            json_output=True,
        )
    finally:
        DaemonStateOwnershipFence.acquire = original  # type: ignore[assignment]

    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.err.splitlines() if line.strip()]
    assert [line["event"] for line in lines] == [
        "daemon.lock.waiting",
        "daemon.lock.waiting",
    ], lines
    assert {line["holderPid"] for line in lines} == {72472}, lines
    assert captured.out == "", "stdout must stay reserved for the result object"

def test_loose_wrapper_variants_are_refused(tmp_path: Path) -> None:
    """Review B10: `ulimit … && exit 0; exec …` and newline exits are not ours.

    The previous check only looked at the startswith('ulimit') segment, so a
    wrapper that also exits, or breaks the line before ``exit``, was claimed.
    The whole script is now matched against one whitelist pattern, which has no
    slot for those tokens.
    """

    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    service.unit_path.unlink()
    exe = str(service.config.executable.resolve())
    directory = service.unit_path.parent
    for name, script in {
        "and-exit": f"ulimit -n 65536 && exit 0; exec {exe} daemon run",
        "newline-exit": f"ulimit -n 65536; exit 0\nexec {exe} daemon run",
        "trailing-command": f"ulimit -n 65536; exec {exe} daemon run; true",
        "piped": f"ulimit -n 65536; exec {exe} daemon run | cat",
    }.items():
        _foreign_plist(directory, name, label=name, argv=["/bin/bash", "-lc", script])
    production = _foreign_plist(
        directory,
        "com.hyprial.daemon",
        label="com.hyprial.daemon",
        argv=["/bin/bash", "-lc", f"ulimit -n 65536; exec {exe} daemon run --json"],
    )

    assert service._launchd_jobs() == [("com.hyprial.daemon", production)]


def test_program_is_the_executable_and_argv0_is_not(tmp_path: Path) -> None:
    """Review B11: with Program set, argv[0] is an argument, not the program.

    ``Program=<exe>`` + ``["daemon","run"]`` was refused (argv[0] compared as a
    path), and ``Program=/bin/bash`` was claimed through the wrapper branch.
    """

    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    service.unit_path.unlink()
    exe = str(service.config.executable.resolve())
    directory = service.unit_path.parent
    (directory / "program-with-argv0.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": "good",
                "Program": exe,
                # argv[0] is the name slot launchd passes to Program; the
                # subcommand starts at argv[1].
                "ProgramArguments": ["hyprial", "daemon", "run", "--json"],
            }
        )
    )
    (directory / "program-subcommand-in-argv0.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": "no-subcommand",
                "Program": exe,
                # Runs `<exe> run --json`: argv[0] was eaten as the name slot.
                "ProgramArguments": ["daemon", "run", "--json"],
            }
        )
    )
    (directory / "program-bash-no-script.plist").write_bytes(
        plistlib.dumps(
            {"Label": "bash", "Program": "/bin/bash", "ProgramArguments": ["daemon", "run"]}
        )
    )

    jobs = service._launchd_jobs()

    assert [label for label, _path in jobs] == ["good"], jobs

def test_program_wins_even_when_argv0_is_our_executable(tmp_path: Path) -> None:
    """Review B12: the mutation "ignore Program" must make a test fail.

    Every other Program case passes argv0=/bin/echo, so a matcher that dropped
    the Program parameter still refused them. The hole production could hit is
    argv[0] = our executable with Program pointing somewhere else: launchd runs
    Program, so claiming this job restarts the wrong command.
    """

    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    service.unit_path.unlink()
    exe = str(service.config.executable.resolve())
    (service.unit_path.parent / "program-wins.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": "program-wins",
                "Program": "/bin/echo",
                "ProgramArguments": [exe, "daemon", "run"],
            }
        )
    )

    assert service._launchd_jobs() == [], service._launchd_jobs()

def test_wrapper_whitespace_and_option_slots_are_closed(tmp_path: Path) -> None:
    r"""Review B14/B15: no newlines in the wrapper, and only --json as an option.

    `\s` matched newlines, so `ulimit -n\n65536; exec …` was accepted while the
    shell would run a program literally named 65536. And the option slot took any
    flag, so `--help`, `--version`, `-x` or `--config=abc` were claimed -- those
    run our binary with a different meaning than `daemon run`.
    """

    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    service.unit_path.unlink()
    exe = str(service.config.executable.resolve())
    directory = service.unit_path.parent
    for name, script in {
        "ulimit-newline": f"ulimit -n\n65536; exec {exe} daemon run",
        "help": f"ulimit -n 65536; exec {exe} daemon run --help",
        "version": f"exec {exe} daemon run --version",
        "short-flag": f"exec {exe} daemon run -x",
        "config": f"exec {exe} daemon run --config=abc",
    }.items():
        _foreign_plist(directory, name, label=name, argv=["/bin/bash", "-lc", script])
    production = _foreign_plist(
        directory,
        "com.hyprial.daemon",
        label="com.hyprial.daemon",
        argv=["/bin/bash", "-lc", f"ulimit -n 65536; exec {exe} daemon run --json"],
    )

    assert service._launchd_jobs() == [("com.hyprial.daemon", production)]

def test_program_shell_skips_the_name_slot(tmp_path: Path) -> None:
    """Review: `Program=/bin/bash` also passes argv[0] as the name slot.

    launchd runs Program and passes ProgramArguments verbatim, so the flags start
    at argv[1]: `Program=bash` + ["hyprial", "-lc", script] runs the wrapper, while
    `Program=bash` + ["-lc", script] runs bash with -lc eaten as its name.
    """

    runner = LaunchdSupervisor()
    service = manager(tmp_path, runner)
    service.unit_path.unlink()
    exe = str(service.config.executable.resolve())
    directory = service.unit_path.parent
    script = f"ulimit -n 65536; exec {exe} daemon run --json"
    (directory / "bash-with-name.plist").write_bytes(
        plistlib.dumps(
            {"Label": "bash-good", "Program": "/bin/bash", "ProgramArguments": ["hyprial", "-lc", script]}
        )
    )
    (directory / "bash-eaten-name.plist").write_bytes(
        plistlib.dumps(
            {"Label": "bash-bad", "Program": "/bin/bash", "ProgramArguments": ["-lc", script]}
        )
    )

    jobs = service._launchd_jobs()

    assert [label for label, _path in jobs] == ["bash-good"], jobs


def test_the_matcher_ignores_the_name_slot_only_when_program_is_set() -> None:
    """Mutation guard for the same rule, without a plist or a store.

    Dropping the "skip argv[0]" step makes the eaten-name case pass; keeping it
    makes only the correct shape pass.
    """

    executable = Path("/opt/hyprial/bin/hyprial")
    script = f"ulimit -n 65536; exec {executable} daemon run --json"

    assert argv_runs_daemon(["hyprial", "-lc", script], executable, "/bin/bash") is True
    assert argv_runs_daemon(["-lc", script], executable, "/bin/bash") is False
    # No Program: argv[0] IS the program, so the flags start there.
    assert argv_runs_daemon(["/bin/bash", "-lc", script], executable) is True
