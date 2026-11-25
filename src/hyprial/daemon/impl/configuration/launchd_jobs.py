"""Launchd job identification: does this job run OUR daemon?

Production (2026-10-29) ran an operator-written job named
``com.hyprial.daemon`` (``bash -lc "ulimit -n 65536; exec .../hyprial daemon
run --json"``) while our own label was never loaded, so keying the supervisor
identity to the label made every restart spawn an unsupervised daemon.

The identity is the parsed command line, matched exactly -- never a substring:
another install's binary, a different subcommand, ``/bin/echo`` with the same
words, or a plist that only mentions the path in metadata must not be claimed.

Z-8b (owner-confirmed 2026-11-15, PR #2830): the scanner also recognizes the
module form ``[interpreter, -m, hyprial.cli, daemon, run]`` for installs whose
unit runs ``sys.executable -m hyprial.cli``, and additionally matches the job's
``EnvironmentVariables.HYPRIAL_HOME`` by resolved path, so a derived-home
manager never adopts the default home's job and vice versa. A job without a
HYPRIAL_HOME entry predates per-home units and serves the default home, so
only the default home's manager claims it.

Z-8 launchd acceptance fix D1 (2026-11-16): the module form renders the
venv's unresolved ``bin/python`` (a symlink to the base CPython). The module
form is therefore matched on the absolute spelling only: resolving both sides
would make every venv built on one CPython the same file and claim another
installation's job. The console-script form keeps symlink-tolerant matching
(``~/.local/bin/hyprial`` -> tool venv script), as before.

Z-8 launchd acceptance fix N1 (2026-11-16): one installation has several
interpreter spellings in its ``bin`` directory (uv: ``python3`` and
``python3.12`` are symlinks to ``python``; console scripts carry the
``python3`` shebang), so a unit rendered with ``python`` was foreign to a
process that computed ``python3``. ``same_installation_program`` is the ONE
comparison every attribution/ownership/job-scan site uses. Rule: both paths
are made absolute and only their DIRECTORY is resolved (``/var`` vs
``/private/var`` and symlinked checkout directories stay the same
installation); the entry name then follows symlinks only while the link
target stays a sibling in that same directory, and stops at the first link
that leaves it (``bin/python`` -> base CPython keeps its own name). Two paths
are the same installation exactly when their (directory, final entry) match.
Chosen over resolving the last component (which makes every venv on one
CPython, and the base interpreter itself, the same file: it re-opens D1 and
the cross-installation takeover) and stricter than "same target outside the
directory": two different sibling names that each leave the directory are
different, because nothing proves they are one entry. Non-existent paths and
unreadable links never raise; they compare by their absolute spelling.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

CONSOLE_DAEMON_ARGS = ("daemon", "run")


def read_launchd_plist(
    path: Path,
) -> tuple[str, list[str], str | None, dict[str, str] | None] | None:
    """Return ``(Label, ProgramArguments, Program, EnvironmentVariables)``.

    Any parse failure -- invalid XML, a binary blob that is not a plist, an
    unreadable file -- means "not our supervisor", never a crash: LaunchAgents
    routinely holds files written by other tools. ``EnvironmentVariables`` is
    a string-keyed dict when present and ``None`` otherwise.
    """

    import plistlib
    import xml.parsers.expat

    try:
        with path.open("rb") as handle:
            payload = plistlib.load(handle)
    except (
        OSError,
        ValueError,
        UnicodeDecodeError,
        plistlib.InvalidFileException,
        xml.parsers.expat.ExpatError,
    ):
        return None
    if not isinstance(payload, dict):
        return None
    raw_argv = payload.get("ProgramArguments")
    argv = [str(item) for item in raw_argv] if isinstance(raw_argv, list) else []
    label = payload.get("Label")
    program = payload.get("Program")
    raw_environment = payload.get("EnvironmentVariables")
    environment = (
        {str(key): str(value) for key, value in raw_environment.items()}
        if isinstance(raw_environment, dict)
        else None
    )
    return (
        label if isinstance(label, str) else "",
        argv,
        program if isinstance(program, str) else None,
        environment,
    )


def text_program_argv_runs_daemon(
    text: str, executable: Path, *, daemon_args: Sequence[str] = CONSOLE_DAEMON_ARGS
) -> bool:
    """Attribution check over a plist's raw text (used by install/replace)."""

    import plistlib
    import xml.parsers.expat

    try:
        payload = plistlib.loads(text.encode("utf-8"))
    except (
        ValueError,
        UnicodeDecodeError,
        plistlib.InvalidFileException,
        xml.parsers.expat.ExpatError,
    ):
        return False
    if not isinstance(payload, dict):
        return False
    raw_argv = payload.get("ProgramArguments")
    argv = [str(item) for item in raw_argv] if isinstance(raw_argv, list) else []
    program = payload.get("Program")
    return argv_runs_daemon(
        argv,
        executable,
        program if isinstance(program, str) else None,
        daemon_args=daemon_args,
    )


def argv_runs_daemon(
    argv: Sequence[str],
    executable: Path,
    program: str | None = None,
    *,
    daemon_args: Sequence[str] = CONSOLE_DAEMON_ARGS,
) -> bool:
    """Whitelist match: exactly one of the shapes this install starts.

    1. ``<exe> <daemon_args...> [opts]``               (no Program key)
    2. ``Program=<exe>`` + ``[name-slot, <daemon_args...>, opts]``
       (launchd runs Program, so argv[0] is NOT a path and must not be
       matched as one)
    3. ``<shell> -lc|-c "<ulimit -n N;> exec <exe> daemon run [opts]"``
       (the operator wrapper, matched as a whole script -- not parsed;
       console-script argument shape only: the wrapper is a console-script
       shape, a module-form unit has no wrapper)

    ``daemon_args`` is ``("daemon", "run")`` for a console-script unit and
    ``("-m", "hyprial.cli", "daemon", "run")`` for a module-form unit.

    Anything else is not ours. A parser over shell text invites variants like
    ``ulimit -n 65536 && exit 0; exec ...`` or a newline-separated ``exit``; a
    whitelist regex over the entire script refuses them by construction.
    """

    expected = [str(item) for item in daemon_args]
    strict = expected[:1] == ["-m"]
    if program is not None:
        if _same_file(program, executable, strict=strict):
            # launchd runs Program and passes ProgramArguments verbatim, so
            # argv[0] is the program NAME slot, not an argument: `Program=<exe>`
            # with ["daemon", "run"] runs `<exe> run`, not `<exe> daemon run`.
            # The subcommand therefore starts at argv[1].
            return [str(item) for item in argv][1 : 1 + len(expected)] == expected
        if _is_shell(program) and expected == list(CONSOLE_DAEMON_ARGS):
            # Program is the shell, so argv[0] is again the name slot: the flags
            # start at argv[1]. `Program=/bin/bash` + ["-lc", script] would run
            # bash with -lc eaten as its name -- not our daemon.
            shell_argv = [str(item) for item in argv][1:]
            if len(shell_argv) >= 2 and shell_argv[0] in ("-lc", "-c"):
                return _matches_wrapper_script(shell_argv[1], executable)
        return False
    if not argv:
        return False
    if _same_file(str(argv[0]), executable, strict=strict):
        return [str(item) for item in argv[1 : 1 + len(expected)]] == expected
    if (
        _is_shell(str(argv[0]))
        and expected == list(CONSOLE_DAEMON_ARGS)
        and len(argv) >= 3
        and argv[1] in ("-lc", "-c")
    ):
        return _matches_wrapper_script(str(argv[2]), executable)
    return False


def environment_matches_home(
    environment: Mapping[str, str] | None,
    hyprial_home: Path,
    *,
    default_home: Path,
) -> bool:
    """Whether a job's plist environment serves this config's home.

    A job that names a HYPRIAL_HOME claims only that home (resolved-path
    equality). A job without one predates per-home units and serves the
    default home, so only the default home's manager claims it.
    """

    import os

    expected = os.path.realpath(hyprial_home)
    raw = None if environment is None else environment.get("HYPRIAL_HOME")
    if not isinstance(raw, str) or raw == "":
        return os.path.realpath(default_home) == expected
    try:
        return os.path.realpath(Path(raw).expanduser()) == expected
    except (OSError, RuntimeError):
        return False


_MAX_ENTRY_HOPS = 40


def _installation_entry(path: str | Path) -> tuple[str, str]:
    import os

    absolute = os.path.abspath(os.path.expanduser(path))
    name = os.path.basename(absolute)
    try:
        directory = os.path.realpath(os.path.dirname(absolute))
        for _hop in range(_MAX_ENTRY_HOPS):
            candidate = os.path.join(directory, name)
            if not os.path.islink(candidate):
                break
            target = os.path.normpath(os.path.join(directory, os.readlink(candidate)))
            target_directory, target_name = os.path.split(target)
            if target_name in ("", ".", ".."):
                break
            if os.path.realpath(target_directory) != directory:
                break
            name = target_name
    except (OSError, ValueError, RuntimeError):
        return os.path.dirname(absolute), name
    return directory, name


def same_installation_program(left: str | Path, right: str | Path) -> bool:
    """Whether two interpreter/program spellings are one installation's entry."""

    return _installation_entry(left) == _installation_entry(right)


def _is_shell(path: str) -> bool:
    return path.endswith(("/bash", "/sh"))


def _same_file(candidate: str, executable: Path, *, strict: bool = False) -> bool:
    try:
        expanded = Path(candidate).expanduser()
        if same_installation_program(expanded, executable):
            return True
        if strict:
            return False
        return str(expanded.resolve()) == str(executable.resolve())
    except (OSError, RuntimeError):
        return False


def _matches_wrapper_script(script: str, executable: Path) -> bool:
    """Whole-script whitelist: ``ulimit -n N; exec <exe> daemon run [opts]``.

    ``re.fullmatch`` (no DOTALL) is the point: a newline, ``&&``, an ``exit``, a
    comment or a second command cannot appear, because the pattern has no slot
    for it.
    """

    import re

    # [ \t] everywhere, never \s: a newline inside `ulimit -n` would make the
    # shell run a program named after the number, which we must not accept.
    # The only option this install's wrapper passes is --json.
    pattern = (
        r"[ \t]*(?:ulimit[ \t]+-n[ \t]+\d+[ \t]*;[ \t]*)?"
        r"exec[ \t]+" + re.escape(str(executable)) + r"[ \t]+daemon[ \t]+run"
        r"(?:[ \t]+--json)?[ \t]*"
    )
    return re.fullmatch(pattern, script) is not None


def printed_plist_path(stdout: str) -> Path | None:
    """The plist ``launchctl print`` reports for a loaded job, if it reports one."""

    import re

    match = re.search(r"^\s*path\s*=\s*(\S.*?)\s*$", stdout, re.MULTILINE)
    return Path(match.group(1)) if match else None


def owned_unit_file(reported: Path | str | None, unit_file: Path) -> bool:
    """Whether a loaded job's own file is the unit file this home manages.

    The label is machine-global while the unit file is per home, so a job loaded
    from elsewhere belongs to another home or install.  ``None`` keeps the
    caller's answer when the manager does not report the path.
    """

    import os

    if reported is None or str(reported).strip() == "":
        return True
    return os.path.realpath(reported) == os.path.realpath(unit_file)
