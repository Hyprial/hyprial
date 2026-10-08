"""``hyprial upgrade`` - self-upgrade with restore follow-up."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from collections.abc import Callable
from hyprial.kernel import ipc_errors
import math
import re
import socket
import typer

from hyprial.shell.impl.cli.commands.common.pending_restart import (
    _install_locked_requirement,
    _restart_daemon_onto_install,
    _write_pending_restart,
)
from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject, _RESTORE_FOLLOWUP_POLL_INTERVAL_SECONDS, _utc_now
def _perform_upgrade_and_report(*args: object, **kwargs: object) -> JsonObject:
    """Upgrade, then check what got installed, then tell the owner -- every time.

    ⭐ Sending only on failure is why this channel had never delivered anything:
    the one path that exercises it is the path where the machine is already in
    trouble. On 2026-08-31 it was needed twice and arrived zero times -- once
    the process died before reaching it, once the owner lookup was wrong and had
    never been run. Both invisible for the same reason: nothing used this code
    while things were fine.

    ⚠️ The report can never change the upgrade's outcome. It is written outside
    `_perform_upgrade` precisely so it cannot: whatever that raised is re-raised
    untouched, and nothing here raises anything of its own. A self-check that
    could fail an upgrade would turn "we could not verify" into "it did not
    work", and someone would go re-install something that had installed fine.
    """
    from hyprial.daemon import UPGRADE_FAILED, UPGRADE_UNCONFIRMED
    services = get_services()

    host = socket.gethostname()

    def report(*, action: str, detail: str, key: str) -> JsonObject:
        from hyprial.daemon import notify_upgrade_outcome, run_self_check
        services = get_services()
        check = run_self_check()
        alert = notify_upgrade_outcome(
            hyprial_home=services._hyprial_home(),
            state_dir=services._state_dir(),
            host=host,
            action=action,
            upgrade_detail=detail,
            check=check,
            idempotency_key=key,
        )
        return {"selfCheck": check.to_json(), "ownerAlert": alert.to_json()}

    def _action_for(result: JsonObject) -> str:
        """What the run actually did -- read off the result, never assumed.

        ⚠️ This used to be a hardcoded `True` passed as `upgraded`. Reaching
        the success path and installing something are different facts, and
        `hyprial upgrade` runs on a timer where the second is usually false.
        """
        from hyprial.daemon import UPGRADE_ALREADY_CURRENT, UPGRADE_AWAITING_RESTART, UPGRADE_DECLINED_DOWNGRADE, UPGRADE_INSTALLED, UPGRADE_UNCONFIRMED

        restart = result.get("restart")
        if isinstance(restart, dict) and restart.get("awaitingConfirmation") is True:
            # Installed on purpose without a restart; the owner confirms.
            return UPGRADE_AWAITING_RESTART
        if isinstance(restart, dict) and restart.get("confirmed") is False:
            # ⭐ Installed, daemon not yet confirmed ready. Checked before
            # `upgraded`, because this run *did* install -- so asking "did
            # anything install?" first would answer `✅ 升级完成` and state
            # something nobody verified. Trading a false alarm for a false
            # all-clear is not an improvement.
            return UPGRADE_UNCONFIRMED
        if result.get("upgraded"):
            return UPGRADE_INSTALLED
        if result.get("declinedDowngrade"):
            return UPGRADE_DECLINED_DOWNGRADE
        return UPGRADE_ALREADY_CURRENT

    try:
        result = services._perform_upgrade(*args, **kwargs)  # type: ignore[arg-type]
    except (services.CliError, ipc_errors.TransientDaemonError) as error:
        try:
            observed = report(
                action=UPGRADE_FAILED,
                detail=f"{error.code}: {error}",
                key=f"upgrade-outcome:failed:{error.code}:{_utc_now()}",
            )
            if isinstance(error.data, dict):
                error.data.update(observed)
        except Exception:  # noqa: BLE001 -- the upgrade error is the message that matters
            pass
        raise
    try:
        action = _action_for(result)
        result.update(
            report(
                action=action,
                detail=str(result.get("resolvedTag") or "upgrade completed"),
                key=f"upgrade-outcome:ok:{result.get('resolvedCommit')}:{action}",
            )
        )
        if action == UPGRADE_UNCONFIRMED:
            # 3c116ad2 (P2): the notice above promised a follow-up; deliver
            # it from this same process -- poll ping's phase to reconciliation
            # or the fleet-derived budget, then one closing message.  Inside
            # the guard on purpose: reporting must never undo a good upgrade.
            _follow_up_restore_confirmation(result)
    except Exception:  # noqa: BLE001 -- reporting must not undo a good upgrade
        pass
    return result


def _resolve_upgrade_target(
    *, source: str, ref: str | None, tag: str | None
) -> tuple[str, str | None, Any, JsonObject]:
    """Resolve one persistent-official or explicit one-shot upgrade target."""
    services = get_services()

    from hyprial.daemon import updates

    if source not in updates.UPGRADE_SOURCES:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"--source must be one of {', '.join(sorted(updates.UPGRADE_SOURCES))}",
        )
    if source == "forgejo" and ref is None:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "--source forgejo requires --ref with a full commit or tag",
        )
    if source == "official" and ref is not None:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "--ref is reserved for --source forgejo; use --tag for an official tag",
        )
    if tag is not None and (source != "official" or ref is not None):
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "--tag cannot be combined with --source forgejo or --ref",
        )
    url = updates.UPGRADE_SOURCES[source]
    try:
        track = (
            updates.read_update_track(services._hyprial_home())
            if tag is None and ref is None
            else None
        )
        if ref is not None and re.fullmatch(r"[0-9a-fA-F]{40}", ref):
            resolution = updates.RemoteResolution(tag=ref, commit=ref.lower())
        else:
            resolution = updates.resolve_remote(
                url,
                tag=ref or tag or track,
                timeout=updates.read_ls_remote_timeout(services._hyprial_home()),
            )
    except updates.UpdateProbeError as error:
        raise services.CliError("UPGRADE_CHECK_FAILED", str(error)) from error
    resolved: JsonObject = {
        "resolvedTag": resolution.tag,
        "resolvedCommit": resolution.commit,
        "source": source,
        "persistentUpdateSource": updates.OFFICIAL_GIT_URL,
    }
    if source != "official":
        resolved["oneShotSource"] = True
    if track is not None:
        resolved["track"] = track
    warning = updates.retired_track_warning(services._hyprial_home())
    if warning is not None:
        resolved["warning"] = warning
    return url, track, resolution, resolved


def _perform_upgrade(
    force: bool,
    tag: str | None = None,
    *,
    source: str = "official",
    ref: str | None = None,
    restart: bool = True,
    before_restart: Callable[[JsonObject, str, str, str], JsonObject] | None = None,
    awaiting_confirmation: bool = False,
) -> JsonObject:
    """Install one exact tag and restart an existing daemon when it changes.

    ``awaiting_confirmation`` (the timer's mode): install, never restart, and
    record a pending restart that ``hyprial autoupdate restart`` applies.
    """
    services = get_services()

    from hyprial.daemon import updates

    services.require_initialized_hyprial_home()

    installation = updates.read_installation()
    if updates.installation_is_local(installation):
        # Guard 2 (spec autoupdate-isolated-home-2026-09-15): this process
        # was installed from a local path (file://, bare path, git+file,
        # editable), so it runs from a working tree, and "upgrading" it
        # would overwrite the USER'S GLOBAL uv tool directory with that
        # tree's resolution.  Refuse before any remote probe.  Display-
        # through code per proto.md's registry border ruling: it lands in
        # the --json error surface and the autoupdate last-run record, and
        # no process branches on the string.
        raise services.CliError(
            "UPGRADE_LOCAL_SOURCE",
            "refusing to upgrade: this hyprial was installed from a local "
            f"source ({installation.url}); a uv tool install would overwrite "
            "the user's global tool directory",
            {
                "guard": "local-install-source",
                "installationUrl": installation.url,
            },
        )
    url, track, resolution, resolved = _resolve_upgrade_target(
        source=source, ref=ref, tag=tag
    )
    if not force and not updates.upgrade_available(
        installation,
        resolution,
        operator_chose_the_tag=tag is not None or ref is not None,
    ):
        # ⚠️ Two different reasons land here and they must not share a sentence.
        # "already at the resolved tag's commit" is simply false when the track
        # moved backwards -- we are NOT at that commit, we declined to go to
        # it. And a guard that leaves no trace is unobservable: "the protection
        # fired" and "no rollback ever happened" would read identically, and we
        # want to know whether it has ever actually caught anything.
        declined_downgrade = (
            tag is None
            and ref is None
            and updates.resolution_is_a_downgrade(installation, resolution)
        )
        return {
            "ok": True,
            "upgraded": False,
            "reason": (
                f"resolved tag names an older version than the installed "
                f"{installation.version}; refusing to move backwards"
                if declined_downgrade
                else "already at the resolved tag's commit"
            ),
            **({"declinedDowngrade": True} if declined_downgrade else {}),
            **resolved,
            "restartRequired": False,
            "restart": {
                "attempted": False,
                "restarted": False,
                "reason": "upgrade was a no-op; restart not needed",
            },
        }
    # Track installs pin the resolved commit, never the movable tag name: the
    # tag can advance between this resolution and uv's fetch, and uv records
    # the requirement's ref as PEP 610 requested_revision — pinning the tag
    # name on a track would make every later upgrade see a changed ref and
    # restart the daemon on every timer tick.  The legacy and explicit-tag
    # paths keep their exact @tag requirement.
    requested = (
        resolution.commit
        if track is not None or ref is not None
        else resolution.tag
    )
    # A moved tag keeps its name: compare the commit too (see _require_installed_commit).
    ref_changed = installation.requested_revision != requested or (
        installation.commit is not None
        and resolution.commit is not None
        and installation.commit != resolution.commit
    )
    before = (
        services._running_daemon_before_upgrade(installation.version or "unknown")
        if ref_changed
        else None
    )
    if before is not None and restart:
        services._prime_post_install_restart_imports()
    requirement = f"git+{url}@{requested}"
    resolved_suffix = f"resolvedTag={resolution.tag} resolvedCommit={resolution.commit}"
    tool_guard = updates.uv_tool_dir_guard()
    if not tool_guard["allowed"]:
        # Guard 3 (spec autoupdate-isolated-home-2026-09-15), the backstop:
        # ``uv tool install`` may run only when this process itself lives in
        # the tool directory uv is about to write.  Placement is the single
        # install call site, so the manual ``hyprial upgrade``, the
        # autoupdate child, and the legacy launchd/systemd timer (which
        # shells out to the same child) all share it.  Fail-closed on an
        # unresolvable tool directory: we cannot prove where uv would
        # write, so we cannot prove the write is ours.
        raise services.CliError(
            "UPGRADE_TOOL_DIR_MISMATCH",
            "refusing to run uv tool install: uv would write "
            f"{tool_guard['toolDir']} but this process runs from "
            f"{tool_guard['sysPrefix']}; only the installation this process "
            "is part of may be upgraded in place",
            {**resolved, **tool_guard},
        )
    completed, installed = _install_locked_requirement(
        url=url,
        requirement=requirement,
        resolution=resolution,
        resolved=resolved,
        resolved_suffix=resolved_suffix,
    )
    installed_version = installed.version or resolution.version or resolution.tag
    result: JsonObject = {
        "ok": True,
        "upgraded": True,
        **resolved,
        "output": completed.stdout.strip(),
    }
    if not ref_changed:
        result.update(
            {
                "restartRequired": False,
                "restart": {
                    "attempted": False,
                    "restarted": False,
                    # The legacy string is byte-preserved; only track mode
                    # (which pins a commit, not a tag) uses the new wording.
                    "reason": (
                        "resolved ref unchanged; restart not needed"
                        if track is not None
                        else "resolved tag unchanged; restart not needed"
                    ),
                },
            }
        )
        return result
    if before is None:
        result.update(
            {
                "restartRequired": False,
                "restart": {
                    "attempted": False,
                    "restarted": False,
                    "reason": "daemon was not running; restart skipped",
                },
            }
        )
        return result
    if awaiting_confirmation:
        # Allen 2026-09-23: the timer no longer restarts on its own.  The old
        # gate (restart only after a 3s squire receipt) failed on a slow
        # receipt while the notice itself arrived, and a blocked restart was
        # then forgotten: the next run saw "already current" and never
        # restarted.  Now the install is recorded as a pending restart that
        # survives until someone runs `hyprial autoupdate restart`.
        pending = {
            "tag": resolution.tag,
            "commit": resolution.commit,
            "version": installed_version,
            "before": before,
            "resolved": resolved,
            "installedAt": _utc_now(),
        }
        _write_pending_restart(pending)
        result.update(
            {
                "restartRequired": True,
                "restart": {
                    "attempted": False,
                    "restarted": False,
                    "awaitingConfirmation": True,
                    "reason": (
                        "installed; restart waits for confirmation: "
                        "hyprial autoupdate restart"
                    ),
                    "before": before,
                },
                "pendingRestart": pending,
            }
        )
        return result
    if not restart:
        result.update(
            {
                "restartRequired": True,
                "restart": {
                    "attempted": False,
                    "restarted": False,
                    "reason": "restart disabled by --no-restart",
                    "before": before,
                },
            }
        )
        return result

    notification: JsonObject | None = None
    if before_restart is not None:
        try:
            notification = before_restart(
                before,
                installed_version,
                resolution.tag,
                resolution.commit,
            )
            if (
                notification.get("delivered") is not True
                or notification.get("deliveryConfirmed") is not True
            ):
                raise services.CliError(
                    "AUTOUPDATE_NOTIFICATION_UNDELIVERED",
                    "restart notification did not receive delivery confirmation",
                )
        except Exception as error:  # noqa: BLE001 - restart must fail closed
            failure: JsonObject = {
                "upgradeCompleted": True,
                "upgraded": True,
                **resolved,
                "restartRequired": True,
                "restart": {
                    "attempted": False,
                    "restarted": False,
                    "reason": (
                        "restart blocked because the pre-restart notification "
                        "was not confirmed"
                    ),
                    "before": before,
                },
            }
            if notification is not None:
                failure["notification"] = notification
            raise services.CliError(
                "UPGRADE_NOTIFICATION_FAILED",
                f"upgrade completed at {resolution.tag} ({installed_version}), "
                f"but restart notification was not confirmed: {error}",
                failure,
            ) from error
        result["notification"] = notification

    return _restart_daemon_onto_install(
        result,
        before=before,
        installed_version=installed_version,
        resolution=resolution,
        resolved=resolved,
    )


def _read_restore_phase() -> str | None:
    """One light ping; the answer's ``phase`` is the only readiness truth.

    The three phases are the daemon's contract (``restoring`` ->
    ``serving`` -> ``reconciled``); this function reads and returns one,
    interpreting nothing.  No phase in the answer is returned as ``None`` --
    only an explicit ``reconciled`` may ever count as confirmed.
    """
    services = get_services()

    answer = services._daemon_request("ping", timeout=2.0)
    if isinstance(answer, dict):
        phase = answer.get("phase")
        if isinstance(phase, str):
            return phase
    return None


def _poll_restore_phase() -> str | None:
    """`_read_restore_phase` for the poll loop: no answer is not reconciled.

    A daemon that cannot answer ping while its budget elapses is simply not
    reconciled -- that is the whole of the interpretation, and the poll
    keeps its promise of deciding on ping's phase rather than on an error.
    Both error families count as "no answer": CliError (malformed reply)
    and the transient transport classes (#332 F4② -- a restoring or
    reconnecting daemon is mid-flight, not gone).
    """
    services = get_services()

    try:
        return _read_restore_phase()
    except (services.CliError, ipc_errors.TransientDaemonError):
        return None


def _pending_connector_count() -> int | None:
    """Connectors not back yet, counted off ps's existing ``connectors`` list.

    ``None`` when ps cannot answer (mid-restore it is gated behind the
    restore wall) or carries no connector list -- then the message omits
    the count rather than guess at one.  Best-effort by design:
    ``restore_wait=0.0`` never sits out the restoring refusal.
    """
    services = get_services()

    try:
        answer = services._daemon_request("ps", timeout=2.0, restore_wait=0.0)
    except (services.CliError, ipc_errors.TransientDaemonError):
        # Mid-restore ps is refused as a transient class (#332 F4②); either
        # way there is no count to read, and the message omits N.
        return None
    if not isinstance(answer, dict):
        return None
    connectors = answer.get("connectors")
    if not isinstance(connectors, list):
        return None
    return sum(
        1
        for row in connectors
        if not (isinstance(row, dict) and row.get("running") is True)
    )


def _desired_connector_rows() -> int:
    """Connector rows in this machine's desired state -- the restore's fleet.

    The row count is what the poll budget scales with, exactly as the
    daemon-side settlement bound scales with its target count.  A fresh
    home has no rows and derives an empty budget; that is the honest bound
    for a machine with nothing to restore.
    """
    services = get_services()

    from hyprial.daemon import DesiredStateStore

    document = services._state_dir() / "desired-state.json"
    return len(DesiredStateStore(document).load().harnesses)


def _expected_interruption_seconds() -> int:
    """The notice budget, derived from this machine's fleet -- not a literal.

    Same arithmetic as the follow-up poll's backstop (F1's admission-round
    shape with margin), rounded up to whole seconds.  The daemon's notice
    text no longer speaks a duration (3c116ad2 P3), but the field stays a
    positive-int contract; a machine with an empty desired state derives
    zero seconds and is floored to 1 -- the smallest value the contract
    accepts, not a new promise.
    """

    from hyprial.daemon import restore_followup_budget_seconds

    seconds = restore_followup_budget_seconds(_desired_connector_rows())
    return max(1, math.ceil(seconds))


def _follow_up_restore_confirmation(
    result: JsonObject,
    *,
    poll_interval_seconds: float = _RESTORE_FOLLOWUP_POLL_INTERVAL_SECONDS,
    admission_width: int | None = None,
    start_timeout_seconds: float | None = None,
) -> None:
    """Close the unconfirmed state: poll ping's phase, then follow up once.

    The first alert said "unconfirmed"; card 3c116ad2 P2 requires the same
    CLI process to settle it.  Poll until ping reports ``reconciled`` or the
    fleet-derived budget runs out -- never longer -- then send exactly one
    follow-up over the owner channel: 「已恢复」 flips ``confirmed`` to true,
    「仍在启动:N」 leaves it false.  The budget is the F1 shape
    (``ceil(rows/width)`` rounds of one start timeout, with margin) over
    this machine's own desired-state rows, never a wall-clock guess; the
    width/timeout parameters are the production defaults and exist so
    tests can shrink them instead of sleeping minutes.
    """
    from hyprial.daemon import notify_restore_followup
    services = get_services()

    from hyprial.daemon import (
        START_ADMISSION_WIDTH_DEFAULT,
        START_TIMEOUT_SECONDS_DEFAULT,
        restore_followup_budget_seconds,
    )

    restart = result.get("restart")
    if not isinstance(restart, dict):
        return
    budget = restore_followup_budget_seconds(
        _desired_connector_rows(),
        admission_width=(
            START_ADMISSION_WIDTH_DEFAULT
            if admission_width is None
            else admission_width
        ),
        start_timeout_seconds=(
            START_TIMEOUT_SECONDS_DEFAULT
            if start_timeout_seconds is None
            else start_timeout_seconds
        ),
    )
    deadline = services.time.monotonic() + budget
    phase = _poll_restore_phase()
    while phase != "reconciled" and services.time.monotonic() < deadline:
        services.time.sleep(poll_interval_seconds)
        phase = _poll_restore_phase()
    host = socket.gethostname()
    upgrade_detail = str(result.get("resolvedTag") or "upgrade completed")
    commit = str(result.get("resolvedCommit") or "unknown")
    follow_up: JsonObject = {"phase": phase}
    if phase == "reconciled":
        restart["confirmed"] = True
        restart["reason"] = "restore settled after poll (phase reconciled)"
        follow_up["outcome"] = "recovered"
        alert = notify_restore_followup(
            hyprial_home=services._hyprial_home(),
            state_dir=services._state_dir(),
            host=host,
            recovered=True,
            pending_connectors=None,
            upgrade_detail=upgrade_detail,
            idempotency_key=f"upgrade-restore-followup:recovered:{commit}",
        )
    else:
        pending = _pending_connector_count()
        follow_up["outcome"] = "still-starting"
        if pending is not None:
            follow_up["pendingConnectors"] = pending
        alert = notify_restore_followup(
            hyprial_home=services._hyprial_home(),
            state_dir=services._state_dir(),
            host=host,
            recovered=False,
            pending_connectors=pending,
            upgrade_detail=upgrade_detail,
            idempotency_key=f"upgrade-restore-followup:still-starting:{commit}",
        )
    follow_up["alert"] = alert.to_json()
    restart["followUp"] = follow_up


@app.command()
def upgrade(
    tag: str | None = typer.Option(
        None,
        "--tag",
        help="Install this exact remote tag instead of the latest version tag.",
    ),
    source: str = typer.Option(
        "official",
        "--source",
        help="Use the official release, or a one-shot Forgejo test source.",
    ),
    ref: str | None = typer.Option(
        None,
        "--ref",
        help="One-shot Forgejo full commit or tag; requires --source forgejo.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Reinstall the resolved tag even when its commit is current.",
    ),
    no_restart: bool = typer.Option(
        False,
        "--no-restart",
        help="Install the tag but leave an existing daemon on its current code.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Upgrade the uv-managed hyprial tool from the official release."""
    services = get_services()

    services._execute(
        lambda: services._perform_upgrade_and_report(
            force,
            tag,
            source=source,
            ref=ref,
            restart=not no_restart,
        ),
        json_output=json_output,
        allow_missing_home=True,
    )
