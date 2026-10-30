"""Launchd job identification: does this job run OUR daemon?

Production (2026-10-29) ran an operator-written job named
``com.hyprial.daemon`` (``bash -lc "ulimit -n 65536; exec .../hyprial daemon
run --json"``) while our own label was never loaded, so keying the supervisor
identity to the label made every restart spawn an unsupervised daemon.

The identity is the parsed command line, matched exactly -- never a substring:
another install's binary, a different subcommand, ``/bin/echo`` with the same
words, or a plist that only mentions the path in metadata must not be claimed.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def read_launchd_plist(path: Path) -> tuple[str, list[str], str | None] | None:
    """Return ``(Label, ProgramArguments, Program)`` from a launchd plist.

    Any parse failure -- invalid XML, a binary blob that is not a plist, an
    unreadable file -- means "not our supervisor", never a crash: LaunchAgents
    routinely holds files written by other tools.
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
    return (
        label if isinstance(label, str) else "",
        argv,
        program if isinstance(program, str) else None,
    )


def text_program_argv_runs_daemon(text: str, executable: Path) -> bool:
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
        argv, executable, program if isinstance(program, str) else None
    )


def argv_runs_daemon(
    argv: Sequence[str], executable: Path, program: str | None = None
) -> bool:
    """Whitelist match: exactly one of the three shapes this install starts.

    1. ``<exe> daemon run [opts]``                     (no Program key)
    2. ``Program=<exe>`` + ``["daemon", "run", opts]`` (launchd runs Program, so
       argv[0] is NOT a path and must not be matched as one)
    3. ``<shell> -lc|-c "<ulimit -n N;> exec <exe> daemon run [opts]"``
       (the operator wrapper, matched as a whole script -- not parsed)

    Anything else is not ours. A parser over shell text invites variants like
    ``ulimit -n 65536 && exit 0; exec ...`` or a newline-separated ``exit``; a
    whitelist regex over the entire script refuses them by construction.
    """

    if program is not None:
        if _same_file(program, executable):
            # launchd runs Program and passes ProgramArguments verbatim, so
            # argv[0] is the program NAME slot, not an argument: `Program=<exe>`
            # with ["daemon", "run"] runs `<exe> run`, not `<exe> daemon run`.
            # The subcommand therefore starts at argv[1].
            return [str(item) for item in argv][1:3] == ["daemon", "run"]
        if _is_shell(program):
            # Program is the shell, so argv[0] is again the name slot: the flags
            # start at argv[1]. `Program=/bin/bash` + ["-lc", script] would run
            # bash with -lc eaten as its name -- not our daemon.
            shell_argv = [str(item) for item in argv][1:]
            if len(shell_argv) >= 2 and shell_argv[0] in ("-lc", "-c"):
                return _matches_wrapper_script(shell_argv[1], executable)
        return False
    if not argv:
        return False
    if _same_file(str(argv[0]), executable):
        return [str(item) for item in argv[1:3]] == ["daemon", "run"]
    if _is_shell(str(argv[0])) and len(argv) >= 3 and argv[1] in ("-lc", "-c"):
        return _matches_wrapper_script(str(argv[2]), executable)
    return False


def _is_shell(path: str) -> bool:
    return path.endswith(("/bash", "/sh"))


def _same_file(candidate: str, executable: Path) -> bool:
    try:
        return str(Path(candidate).expanduser().resolve()) == str(executable)
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
