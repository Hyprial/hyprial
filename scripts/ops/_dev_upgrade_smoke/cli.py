"""Command-line parser and process entrypoint."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .common import BLOCKED, EXIT_BLOCKED, EXIT_FAILED, EXIT_USAGE, FAIL
from .smoke import Smoke

DESCRIPTION = """Smoke-test a hyprial dev wheel on a test host.

The five-step flow this instrument encodes is deliberately smaller than a
release drill:

1. Record the installed production daemon, its wheel, and the state of the
   home's per-home OS service (``hyprial daemon service status --json``).
2. Install the candidate wheel into a throwaway uv tool environment and run
   its CLI surface there.  A cold daemon start is optional because an empty
   home has no identity and stops at device authorization.
3. Stop the daemon with ``hyprial daemon stop`` and prove it is gone.
4. Install the candidate into the live uv tool environment and start the
   daemon with ``hyprial daemon start`` against the existing production home,
   then wait up to ``--ready-timeout`` for it to be ready.  The start command
   gives up after its own 30 s without startup progress
   (``DAEMON_START_TIMEOUT``); when the unit is running that is not a failed
   start, and the script keeps waiting until ``--ready-timeout``.
5. If any step fails, reinstall the recorded old wheel and start it again.

Both service managers (systemd user units, launchd agents) are driven only
through hyprial; the home must already have an installed unit attributable to
this installation, and the script never installs one.  A baseline wheel whose
service status predates ``supervision``/``attributed`` keeps the former
``systemctl --user`` path on Linux and is refused elsewhere.

``preflight`` is read-only apart from throwaway directories.  ``apply`` stops
the daemon and therefore requires ``--expect-host`` plus ``--yes`` in
non-interactive runs.  The old wheel must exist on disk before the daemon is
stopped; without it there is no rollback path.

This script is intentionally host-neutral: every path and wheel is an
argument and the unit is derived from the home by hyprial (``--service`` is
deprecated and only compared).  It never reads the GUI handoff file (the
one-time grant), never writes to the production home, and never installs or
edits a service unit.  Every hyprial command it runs gets the target home and
state dir, ``--bin-dir`` first on PATH, and none of HYPRIAL_DAEMON_AUTOSTART,
HYPRIAL_SERVICE_AUTOINSTALL, HARNESS_SOCKET_PATH, HYPRIAL_OWNER,
HYPRIAL_NODE_ID (the installed unit sets none of them either); the summary's
``droppedEnvironment`` names those that were set.

``apply --gui`` runs ``hyprial gui --no-open --json --stdin-stop`` as a child of
this script against the same home (no ``systemd-run``, no unit; the GUI never
outlives the script) and checks its startup record, listener, handoff file mode
and the daemon's identity.  ``--gui-hold-minutes`` keeps it open for a person.
Before the stop, ``--gui`` probes the home's GUI runtime with the installed
CLI's ``hyprial gui --port 0 --stdin-stop`` (not read-only: a GUI host runs for
a moment and writes a one-time grant file, never read).  GUI_RUNTIME_BUSY
refuses the run with exit 2 before anything is stopped; a probe without an
answer is NOT_RUN and the upgrade proceeds.  A GUI that appears after the probe
fails the GUI check and the upgrade is rolled back.

Exit codes:
    0  every required check passed (optional BLOCKED checks may remain)
    1  a required check failed (including the optional --gui check); if the
       daemon changed, rollback succeeded.  A stop that did not complete
       restarts the old daemon once the stop has ended and also exits 1
    2  usage or safety guard rejected the run before mutation (also: no
       installed attributable unit, a daemon outside its unit's custody,
       --service different from the derived unit, HYPRIAL_DAEMON_AUTOSTART=0,
       a legacy baseline wheel on macOS, and with --gui a GUI already holding
       the home's runtime or a probe GUI that could not be stopped)
    3  rollback failed, the stop outcome stayed unknown, or the GUI child was
       still alive after SIGKILL (the summary names its process group and the
       kill command); manual intervention is required
    4  blocked before use (for example SERVICE_PLATFORM_UNSUPPORTED), with the
       reason recorded
"""


MAX_GUI_HOLD_MINUTES = 60.0


def hold_minutes(text: str) -> float:
    try:
        value = float(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from error
    if not 0.0 <= value <= MAX_GUI_HOLD_MINUTES:
        raise argparse.ArgumentTypeError(
            f"must be between 0 and {MAX_GUI_HOLD_MINUTES:g} minutes, got {text}"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--new-wheel", type=Path, required=True)
        subparser.add_argument(
            "--wheelhouse",
            type=Path,
            action="append",
            required=True,
            help="offline find-links directory; repeatable",
        )
        subparser.add_argument("--old-wheel", type=Path)
        subparser.add_argument("--new-wheel-sha256")
        subparser.add_argument("--old-wheel-sha256")
        subparser.add_argument("--expect-commit")
        subparser.add_argument(
            "--python",
            type=Path,
            help="interpreter for uv tool install; default: the receipt's python "
            "when it is an existing absolute path, else <tool-dir>/bin/python",
        )
        subparser.add_argument("--uv", default="uv")
        subparser.add_argument(
            "--service",
            default=None,
            help="deprecated: hyprial derives the unit from the home; when given "
            "it must equal that unit or the run is refused",
        )
        subparser.add_argument("--tool-dir", type=Path)
        subparser.add_argument("--bin-dir", type=Path)
        subparser.add_argument("--home", type=Path)
        subparser.add_argument("--state-dir", type=Path)
        subparser.add_argument("--artifacts-dir", type=Path)
        subparser.add_argument("--expect-host")
        subparser.add_argument(
            "--ready-timeout",
            type=float,
            default=180.0,
            help="seconds from the start command until the daemon must be ready "
            "(serving the expected version under its unit). The start command's "
            "own 30 s window is not this limit: a start answered "
            "DAEMON_START_TIMEOUT while the unit runs is still waited for",
        )
        subparser.add_argument("--stop-timeout", type=float, default=180.0)
        subparser.add_argument(
            "--cold-start-timeout",
            type=float,
            default=0.0,
            help="seconds for an optional empty-home daemon start; 0 skips it",
        )
        subparser.add_argument("--expect-routine", action="append", default=[])
        subparser.add_argument("--require-native-actor", action="store_true")
        subparser.add_argument("--require-pac-schema", action="store_true")
        subparser.add_argument("--keep-workdir", action="store_true")
        subparser.add_argument("--json", action="store_true")

    preflight = subparsers.add_parser(
        "preflight", help="read-only checks plus a throwaway isolated install"
    )
    add_common(preflight)
    apply = subparsers.add_parser(
        "apply", help="stop, install, start, verify, and roll back on failure"
    )
    add_common(apply)
    apply.add_argument("--yes", action="store_true")
    apply.add_argument(
        "--gui",
        action="store_true",
        help="probe the home's GUI runtime before the stop (refuses when another "
        "GUI holds it), then after the upgrade run hyprial gui --stdin-stop as a "
        "child of this script and check it; a failed GUI check rolls back",
    )
    apply.add_argument(
        "--gui-port",
        type=int,
        default=4173,
        help="port for hyprial gui --port; 0 lets the GUI pick one",
    )
    apply.add_argument(
        "--gui-hold-minutes",
        type=hold_minutes,
        default=0.0,
        help="keep the GUI running this long (0..60) after its checks pass, "
        "so a human can open it; it is stopped when the hold ends and its login "
        "grant lasts the hold, at most 10 minutes",
    )
    apply.add_argument("--ssh-host")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        smoke = Smoke(args)
    except OSError as error:
        print(f"cannot create artifact directory: {error}", file=sys.stderr)
        return EXIT_USAGE
    try:
        return smoke.run()
    except KeyboardInterrupt:
        smoke.check("interrupt", BLOCKED, "interrupted by operator")
        smoke.result = "ABORTED"
        return smoke.finish(EXIT_BLOCKED)
    except Exception as error:  # noqa: BLE001 - the harness must report, not traceback
        smoke.check("exception", FAIL, f"{type(error).__name__}: {error}")
        smoke.result = "FAILED"
        return smoke.finish(EXIT_FAILED)
