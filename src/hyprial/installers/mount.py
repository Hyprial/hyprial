"""Mount installed apps' declared commands onto the ``hyprial`` CLI (H3).

An app's manifest may declare ``commands`` (``hyprial.install/v2``).  At CLI
startup this module reads every installed app's receipt and manifest and
registers one generic subcommand per declaration, so ``hyprial kanban`` exists
because the kanban app *said so*, not because ``cli.py`` was edited for it.

⚠️ Two constraints shape everything here, and both are about ``hyprial --help``:

* **It must be cheap.**  Registration runs on every CLI invocation, so it
  reads the receipt and the manifest file only.  ⛔ No ``git status``, no
  catalog fetch, no checkout validation -- those already run at *launch* time
  (``prepare_application_launch``), which is where a stale or dirty checkout
  should be reported, not while printing help.

* **It must not be able to break the CLI.**  A malformed manifest, an
  unreadable receipt, a name that collides with a built-in: every one is
  skipped with a warning that *names the app and the reason*.  ⛔ Never
  silently -- "the command vanished" and "it was never installed" must not
  look the same to the operator.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.contracts import ipc_errors

from .core import InstallError, MountedCommand, _read_manifest
from .error_codes import INSTALL_STATE_INVALID

#: Actions the generic runner knows how to perform, in the order ``--help``
#: lists them.  A manifest may only ask for a subset (enforced at parse time).
ACTIONS = ("start", "status", "stop", "upgrade")

#: Top-level names hyprial has claimed but not yet shipped.  The collision check
#: reads the LIVE inventory, so a name that is not a command yet is invisible
#: to it -- and an app that mounts ``profile`` today would be silently
#: shadowed the day ``hyprial profile`` lands (or, worse, shadow it).  Reserving
#: here makes the claim hold before the command exists.  ``login`` /
#: ``profile``: the hyprial login line (U1 spec, 2026-09-04).
RESERVED_COMMAND_NAMES: frozenset[str] = frozenset({"login", "profile"})


@dataclass(frozen=True, slots=True)
class Mount:
    """One resolved ``hyprial <command>`` → app binding."""

    command: MountedCommand
    app: str


@dataclass(frozen=True, slots=True)
class MountReport:
    mounts: tuple[Mount, ...]
    skipped: tuple[tuple[str, str], ...]  # (app, reason)


def discover_mounts(
    hyprial_home: Path,
    *,
    builtin_names: Iterable[str],
) -> MountReport:
    """Read every installed app and decide which commands may be mounted.

    Conflicts are resolved in three deliberately different ways
    (design-app-manifest-commands §2):

    ① collides with a built-in or a RESERVED name ⇒ skip + warn.  Built-ins win *at mount time*
       rather than being refused at install time, so that hyprial growing a new
       built-in later does not turn an already-installed app into one that
       "cannot be installed".  Install-time refusal is a separate check.
    ② two apps declare one name ⇒ the app that sorts first wins, the other is
       skipped + warned.  Deterministic, not "whichever installed last": the
       order on disk is not something an operator can see or reason about.
    """

    # Reserved names join the live inventory so the check covers commands
    # that are claimed but not yet registered.
    builtins = frozenset(builtin_names) | RESERVED_COMMAND_NAMES
    apps_root = hyprial_home / "apps"
    mounts: list[Mount] = []
    skipped: list[tuple[str, str]] = []
    claimed: dict[str, str] = {}
    if not apps_root.is_dir():
        return MountReport(mounts=(), skipped=())
    for app_root in sorted(p for p in apps_root.iterdir() if p.is_dir()):
        app = app_root.name
        try:
            commands = _declared_commands(app_root, app)
        except InstallError as error:
            skipped.append((app, str(error)))
            continue
        for command in commands:
            if command.name in builtins:
                skipped.append(
                    (app, f"command {command.name!r} collides with a built-in hyprial command")
                )
                continue
            holder = claimed.get(command.name)
            if holder is not None:
                skipped.append(
                    (app, f"command {command.name!r} is already mounted by app {holder!r}")
                )
                continue
            claimed[command.name] = app
            mounts.append(Mount(command=command, app=app))
    return MountReport(mounts=tuple(mounts), skipped=tuple(skipped))


def _declared_commands(app_root: Path, app: str) -> tuple[MountedCommand, ...]:
    """Receipt → manifest → commands, touching only two JSON files."""

    receipt_path = app_root / "install.json"
    if receipt_path.is_dir():
        # Not a receipt.  Another installer parked a directory under this
        # name (kanban's install.sh keeps its per-file ledger at
        # apps/kanban/install.json/ -- a name collision, not a residue).
        # A directory is not an installed app: skip it silently instead
        # of warning on every hyprial invocation.
        return ()
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # A directory under apps/ with no receipt is not an installed app
        # (a half-finished install, or something an operator put there).
        return ()
    except (OSError, json.JSONDecodeError) as error:
        raise InstallError(INSTALL_STATE_INVALID, f"cannot read install receipt: {error}") from error
    if not isinstance(receipt, dict):
        raise InstallError(INSTALL_STATE_INVALID, "install receipt must be an object")
    manifest_rel = receipt.get("manifest", "hyprial-install.json")
    if not isinstance(manifest_rel, str) or not manifest_rel:
        raise InstallError(INSTALL_STATE_INVALID, "install receipt manifest must be a non-empty string")
    # Schema-agnostic on purpose: the receipt's ``manifest`` key is present in
    # both v1 and v2 receipts, so a v2 install mounts its commands exactly like
    # a v1 one -- mount never reads the install-state schema (design §10 row 17).
    return _read_manifest(app_root / "source", name=app, manifest_rel=manifest_rel).commands


def warn_skipped(report: MountReport, *, stream: Any = None) -> None:
    """One line per skipped app on stderr.  Visible on purpose.

    ⚠️ ``sys.stderr`` is resolved at *call* time, not bound as a default.  A
    default of ``sys.stderr`` freezes whatever object existed at import, so any
    later redirect -- pytest's capsys, a wrapper capturing stderr, a daemon
    re-pointing fds -- would silently receive nothing.  That is the exact
    failure this warning exists to prevent: a skipped app nobody hears about.
    """

    target = sys.stderr if stream is None else stream
    for app, reason in report.skipped:
        print(f"hyprial: not mounting commands from app {app!r}: {reason}", file=target)


def register_mounts(
    report: MountReport,
    *,
    register: Callable[[str, str, Callable[..., Any]], None],
    run: Callable[..., Any],
) -> None:
    """Hand each mount to the CLI as a generic ``<command> [action]`` runner.

    ``register(name, help, fn)`` is whatever the CLI framework offers;
    ``run(app, action)`` performs the action.  Both are injected so this
    module -- and its tests -- never import ``cli``.
    """

    for mount in report.mounts:
        register(mount.command.name, _help_for(mount), _runner(mount, run))


def _help_for(mount: Mount) -> str:
    verbs = "|".join(action for action in ACTIONS if action in mount.command.actions)
    summary = mount.command.summary or f"the {mount.app} app"
    return f"{summary} [{verbs}]"


def _runner(mount: Mount, run: Callable[..., Any]) -> Callable[..., Any]:
    allowed = mount.command.actions

    def invoke(action: str = "start", **options: Any) -> Any:
        if action not in allowed:
            raise InstallError(
                ipc_errors.INVALID_ARGUMENT,
                f"hyprial {mount.command.name} accepts {sorted(allowed)!r}; got {action!r}",
            )
        return run(mount.app, action, **options)

    invoke.__name__ = mount.command.name
    return invoke
