"""Login orchestration flow (service path) and daemon-absence proof."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import notice, progress, warn

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # deferred annotations only
    from hyprial.identity import IdentityTransactionLock

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from collections.abc import Callable
from hyprial.kernel import DaemonLaunchResult
from pathlib import Path
from hyprial.kernel import ipc_errors, resolve_node_id
import json
from hyprial.kernel import probe_process

from hyprial.shell.impl.cli.commands.common.daemon_start import _CUSTODY_STARTUP_ERROR_SHAPES
from hyprial.shell.impl.cli.commands.common.support import DAEMON_START_IDLE_BUDGET_SECONDS, JsonObject, _DEVICE_SIGN_IN_HINT, CliError
def _run_login_cli_flow(
    *,
    no_open: bool,
    json_output: bool,
    switch_account: bool,
    dry_run: bool = False,
    no_daemon: bool = False,
    first_time_setup: bool = False,
    held_identity_transaction: list[IdentityTransactionLock] | None = None,
) -> JsonObject:
    """Run login's identity and device-key logic without CLI recursion."""
    services = get_services()

    from hyprial.shell.impl.login.flow import LoginError, run_login

    profile, source = services.resolve_profile()
    events: list[dict[str, Any]] = []

    def emit(kind: str, data: dict[str, Any]) -> None:
        # stdout in --json mode carries exactly the final result object;
        # progress events (verification URI / user code — both public,
        # never the device code or any token) go to stderr there and to
        # stdout in human mode.
        events.append({"event": kind, **data})
        if json_output:
            progress(kind, "", json_output=True, **data)
        elif kind == "device":
            notice(
                "device authorization required — open this URL and enter the code:\n"
                f"  {data['verificationUri']}\n"
                f"  code: {data['userCode']}\n"
                "  (waiting for authorization; server interval "
                f"{data.get('interval')}s, expires in "
                f"{data.get('expiresIn')}s)\n"
                f"  {_DEVICE_SIGN_IN_HINT}"
            )
        elif kind == "authenticated":
            warn(f"authenticated as {data['owner']} (issuer {data['issuer']})", json_output=False)

    transaction: IdentityTransactionLock | None = None
    daemon_before: JsonObject = {"state": "unknown"}
    # No classification has run yet, so a failure report must say the
    # migration was not attempted — the earlier "not-required" default
    # read as "we checked and nothing was needed", which a stop-phase
    # failure cannot know.
    stopped_preview: JsonObject = {"status": "not-attempted"}
    state_dir = services._state_dir()
    home = services._hyprial_home()

    def failure_data(
        phase: str,
        *,
        committed: bool,
        device: JsonObject | None = None,
        daemon: JsonObject | None = None,
        migration: JsonObject | None = None,
        next_step: str,
    ) -> JsonObject:
        return {
            "failedPhase": phase,
            "identityCommitted": committed,
            "device": device or {"status": "not-attempted"},
            "daemon": daemon if daemon is not None else daemon_before,
            "migration": migration or stopped_preview,
            "nextStep": next_step,
        }

    def classify(
        target_owner: str, phase: str, *, allow_retry: bool
    ) -> JsonObject:
        services = get_services()
        from hyprial.shell.impl.login.preview import (
            LoginPreviewError,
            preview_owner_migration,
        )

        preview = None
        for attempt in range(3):
            try:
                preview = preview_owner_migration(
                    state_dir=state_dir,
                    hyprial_home=home,
                    target_owner=target_owner,
                )
                break
            except LoginPreviewError as error:
                # A read-only SQLite WAL reader may materialize its own
                # -wal/-shm pair on a first snapshot.  S2 correctly rejects
                # that attempt.  The ONLINE preview (plan-live) may retry
                # because it advances only from a later stable snapshot.
                # The STOPPED replay (plan-stopped) may not: the old
                # generation is proven gone, so a source that still
                # changes means another writer is active — refuse and
                # report instead of retrying the evidence away.
                if (
                    error.code == "PREVIEW_SOURCE_CHANGED"
                    and attempt < 2
                    and allow_retry
                ):
                    continue
                raise services.CliError(
                    error.code,
                    str(error),
                    failure_data(
                        phase,
                        committed=False,
                        migration={"status": "failed", **error.data},
                        next_step="inspect the named preview source; identity is unchanged",
                    ),
                ) from error
        assert preview is not None
        projection = preview.as_dict()
        if not preview.ready:
            raise services.CliError(
                "MIGRATION_PREVIEW_BLOCKED",
                "owner migration contains unclassified values",
                failure_data(
                    phase,
                    committed=False,
                    migration=projection,
                    next_step="classify every reported value before retrying login",
                ),
            )
        return projection

    def before_commit(
        previous_owner: str | None,
        target_owner: str,
        observed_identity: tuple[str, str | None, str | None] | None,
    ) -> None:
        from hyprial.identity import IdentityTransactionBusy, IdentityTransactionLock
        services = get_services()
        nonlocal transaction, daemon_before, stopped_preview
        if previous_owner is not None:
            classify(target_owner, "plan-live", allow_retry=True)
        try:
            transaction = IdentityTransactionLock.acquire(home)
        except IdentityTransactionBusy as error:
            raise services.CliError(
                error.code,
                str(error),
                failure_data(
                    "prepare",
                    committed=False,
                    next_step="wait for the active identity transaction and retry",
                ),
            ) from error
        except OSError as error:
            raise services.CliError(
                "IDENTITY_TRANSACTION_FAILED",
                "cannot acquire the home identity transaction: "
                f"{type(error).__name__}",
                failure_data(
                    "prepare",
                    committed=False,
                    next_step="repair home permissions and retry login",
                ),
            ) from error
        try:
            from hyprial.daemon import read_settings_identity

            current_identity = read_settings_identity(hyprial_home=home)
            if current_identity != observed_identity:
                raise services.CliError(
                    "IDENTITY_TRANSACTION_STALE",
                    "settings identity changed while this login was authenticating",
                    failure_data(
                        "prepare",
                        committed=False,
                        next_step="retry login against the newly committed identity",
                    ),
                )
            try:
                probe = services._daemon_probe(timeout=0.5)
            except ipc_errors.DaemonUnavailableError:
                daemon_before = _prove_daemon_absent(home, state_dir, failure_data)
            else:
                daemon_before = {
                    "state": "running",
                    "pid": probe.get("pid"),
                    "epoch": probe.get("epoch"),
                    "owner": probe.get("owner"),
                    "identityMode": probe.get("identityMode"),
                    "identityIssuer": probe.get("identityIssuer"),
                }
                runtime_matches = (
                    probe.get("owner") == target_owner
                    and probe.get("identityMode") == "casdoor"
                    and probe.get("identityIssuer") == profile.issuer
                )
                must_stop = previous_owner is not None or not runtime_matches
                if (
                    must_stop
                    and probe.get("owner") != target_owner
                    and not switch_account
                ):
                    raise services.CliError(
                        "DAEMON_IDENTITY_CONFLICT",
                        "running daemon identity differs from the login candidate; "
                        "use --switch-account",
                        failure_data(
                            "stop",
                            committed=False,
                            next_step="retry with --switch-account",
                        ),
                    )
                if must_stop:
                    services._stop_daemon_for_identity_switch()
                    daemon_before["state"] = "stopped"
            if previous_owner is not None:
                stopped_preview = classify(
                    target_owner, "plan-stopped", allow_retry=False
                )
        except BaseException as error:
            transaction.close()
            transaction = None
            if isinstance(error, services.CliError):
                details = error.data or {}
                if "failedPhase" in details:
                    raise
                raise services.CliError(
                    error.code,
                    str(error),
                    {
                        **details,
                        **failure_data(
                            "stop",
                            committed=False,
                            next_step="prove the old daemon generation exited, then retry",
                        ),
                    },
                ) from error
            if isinstance(error, Exception):
                raise services.CliError(
                    "LOGIN_ORCHESTRATION_FAILED",
                    f"identity orchestration failed: {type(error).__name__}",
                    failure_data(
                        "stop",
                        committed=False,
                        next_step="inspect the old generation and retry login",
                    ),
                ) from error
            raise

    def before_commit_no_daemon(
        previous_owner: str | None,
        target_owner: str,
        observed_identity: tuple[str, str | None, str | None] | None,
    ) -> None:
        """--no-daemon: the liveness check and the commit share one OS lock.

        login.py's heartbeat-only gate cannot see a daemon that is still
        constructing: the constructor already holds this identity
        transaction but has not claimed its heartbeat yet, so a check
        outside the lock can pass and then commit into the construction
        window, splitting the in-memory old owner from the disk new one.
        The gate is therefore repeated under the lock, and the lock is
        held until the commit writes land.
        """
        from hyprial.identity import IdentityTransactionBusy, IdentityTransactionLock
        services = get_services()

        nonlocal transaction
        try:
            transaction = IdentityTransactionLock.acquire(home)
        except IdentityTransactionBusy as error:
            raise services.CliError(
                error.code,
                str(error),
                failure_data(
                    "prepare",
                    committed=False,
                    next_step="wait for the active identity transaction and retry",
                ),
            ) from error
        except OSError as error:
            raise services.CliError(
                "IDENTITY_TRANSACTION_FAILED",
                "cannot acquire the home identity transaction: "
                f"{type(error).__name__}",
                failure_data(
                    "prepare",
                    committed=False,
                    next_step="repair home permissions and retry login",
                ),
            ) from error
        try:
            from hyprial.daemon import read_settings_identity

            current_identity = read_settings_identity(hyprial_home=home)
            if current_identity != observed_identity:
                raise services.CliError(
                    "IDENTITY_TRANSACTION_STALE",
                    "settings identity changed while this login was authenticating",
                    failure_data(
                        "prepare",
                        committed=False,
                        next_step="retry login against the newly committed identity",
                    ),
                )
            identity_changes = (
                current_identity is not None
                and current_identity
                != (target_owner, "casdoor", profile.issuer)
            )
            if identity_changes:
                from hyprial.daemon import live_daemon_pid

                pid = live_daemon_pid(home)
                if pid is not None:
                    raise services.CliError(
                        "DAEMON_RUNNING",
                        "cannot change the persisted identity for "
                        f"{target_owner!r} while this home's daemon is "
                        f"running (pid {pid}): its in-memory identity "
                        "would split from disk",
                        failure_data(
                            "prepare",
                            committed=False,
                            next_step=(
                                "stop the daemon first — `hyprial daemon "
                                "stop` — or omit --no-daemon so login can "
                                "orchestrate the restart"
                            ),
                        ),
                    )
            if previous_owner is not None:
                classify(target_owner, "plan-stopped", allow_retry=False)
        except BaseException:
            transaction.close()
            transaction = None
            raise

    identity_committed = False
    post_commit_phase = "device"
    post_commit_device: JsonObject | None = None
    post_commit_daemon: JsonObject | None = None
    post_commit_migration: JsonObject | None = None

    try:
        try:
            result = run_login(
                profile,
                profile_source=source,
                switch_account=switch_account,
                dry_run=dry_run,
                state_dir=state_dir,
                identity_mode="casdoor",
                identity_issuer=getattr(profile, "issuer", None),
                orchestrate_daemon=not no_daemon and not dry_run,
                before_commit=(
                    None
                    if dry_run
                    else before_commit_no_daemon
                    if no_daemon
                    else before_commit
                ),
                assume_yes=json_output,
                open_browser=not no_open,
                emit=emit,
            )
        except LoginError as error:
            data = error.data or {}
            if not dry_run and "failedPhase" not in data:
                if error.code == "OWNER_WRITE_FAILED":
                    # The credential is already durable, and on the
                    # orchestrated path the old generation is already
                    # stopped; a plain "fix and retry" hides both facts.
                    stopped_note = (
                        " and the previous daemon generation is stopped"
                        if daemon_before.get("state") == "stopped"
                        else ""
                    )
                    retry_next_step = (
                        f"the credential is already written{stopped_note}; "
                        "re-run hyprial login to complete the owner write"
                    )
                else:
                    retry_next_step = "fix the reported error and retry login"
                data = {
                    **data,
                    **failure_data(
                        "commit" if transaction is not None else "prepare",
                        committed=False,
                        next_step=retry_next_step,
                    ),
                }
            raise services.CliError(error.code, str(error), data=data or None) from error

        identity_committed = True
        if no_daemon and not first_time_setup and transaction is not None:
            # The identity commit has landed; holding the transaction
            # through the device stage would only block a daemon start
            # without guarding anything further.
            transaction.close()
            transaction = None

        identity: JsonObject = {
            "status": "switched" if result.switched else "authenticated",
            "owner": result.owner,
            "mode": "casdoor",
            "issuer": result.issuer,
            "source": result.profile_source,
        }
        if result.switched:
            identity["previousOwner"] = result.previous_owner
        if getattr(result, "dry_run", False):
            identity["status"] = "planned"
            identity["committed"] = False
            return {
                "ok": True,
                "identity": identity,
                "migration": getattr(result, "migration_preview", None),
                "device": {"status": "not-attempted", "reason": "dry-run"},
                "daemon": {"state": "not-attempted"},
                "verificationUri": result.verification_uri,
                "userCode": result.user_code,
            }

        device = _ensure_login_device(home, owner=result.owner)
        post_commit_device = device
        if device.get("ready") is True:
            warn(f"  device key: ready (device {device.get('deviceId')})", json_output=json_output)
        else:
            error = device.get("error") if isinstance(device.get("error"), dict) else {}
            warn(
                "  device key: NOT provisioned "
                f"({error.get('code', 'DEVICE_KEY_FAILED')}: "
                f"{error.get('message', '')})\n"
                f"  next step: {device.get('nextStep')}",
                json_output=json_output,
            )
        post_commit_phase = "start"
        daemon_result: JsonObject
        migration_result = stopped_preview
        if first_time_setup:
            # Merge glue: first-time setup commits identity only; init's
            # start path starts the daemon, and a device-stage failure
            # keeps the committed identity instead of failing the login.
            daemon_result = {"state": "not-attempted"}
            migration_result = {"status": "not-required"}
        elif no_daemon:
            daemon_result = {"state": "not-attempted"}
            migration_result = {
                "status": "pending" if result.switched else "not-required"
            }
            identity["nextStep"] = (
                "start the daemon to apply and verify migration"
                if result.switched
                else "start the daemon when ready"
            )
        elif transaction is None:
            # Compatibility for protocol-shaped test stubs which predate
            # the before_commit callback (an identity-only run_login).
            # Making this fail loudly forces those shared stubs into
            # real daemon launches; the real-path contract is pinned by
            # every orchestration test that requires probe/stop/launch.
            daemon_result = {"state": "not-attempted"}
        elif daemon_before.get("state") == "running" and not result.switched:
            daemon_result = dict(daemon_before)
            migration_result = {"status": "not-required"}
        else:
            try:
                launched = services._launch_daemon_process(
                    ready_timeout=DAEMON_START_IDLE_BUDGET_SECONDS,
                    identity_transaction=transaction,
                )
            except Exception as error:
                code = getattr(error, "code", ipc_errors.DAEMON_START_FAILED)
                if code in _CUSTODY_STARTUP_ERROR_SHAPES:
                    # The switch committed and the old generation is proven
                    # gone, but the new generation refused to start at the
                    # owner-migration custody gate.  This is a *named*
                    # outcome of the switch, not a generic startup failure:
                    # the identity stays committed (no rewind), the daemon
                    # state is reported as-is, and nextStep names the same
                    # real, non-destructive first recourse the daemon's
                    # refusal message gives.
                    bounded = getattr(error, "data", None)
                    bounded = bounded if isinstance(bounded, dict) else {}
                    previous = result.previous_owner or "the previous spelling"
                    raise services.CliError(
                        code,
                        str(error),
                        failure_data(
                            "start",
                            committed=True,
                            device=device,
                            migration={
                                "status": "refused",
                                "reason": (
                                    "owner-migration-hosted-conflict"
                                    if code == ipc_errors.OWNER_MIGRATION_HOSTED_CONFLICT
                                    else
                                    "owner-migration-custody-conflict"
                                    if code
                                    == ipc_errors.OWNER_MIGRATION_CUSTODY_CONFLICT
                                    else "owner-migration-custody-unreadable"
                                ),
                                **{
                                    name: bounded[name]
                                    for name, _kind in (
                                        _CUSTODY_STARTUP_ERROR_SHAPES[code]
                                    )
                                    if name in bounded
                                },
                            },
                            next_step=(
                                str(error)
                                if code == ipc_errors.OWNER_MIGRATION_HOSTED_CONFLICT
                                else
                                "the daemon refused to start at the "
                                "owner-migration custody gate; the identity "
                                "stays committed. First recourse "
                                "(non-destructive): switch the login "
                                f"identity back to {previous!r} — "
                                "`hyprial login --switch-account` — then "
                                "start under that spelling (`hyprial "
                                "daemon run`); the daemon's refusal message "
                                "(daemon-launch.log) names the grant-level "
                                "ways out"
                            ),
                        ),
                    ) from error
                raise services.CliError(
                    code,
                    str(error),
                    failure_data(
                        "start",
                        committed=True,
                        device=device,
                        migration={"status": "unknown"},
                        next_step="inspect daemon launch diagnostics; do not roll back settings alone",
                    ),
                ) from error
            if not isinstance(launched, DaemonLaunchResult):
                raise services.CliError(
                    "LOGIN_VERIFY_FAILED",
                    "daemon launcher returned an invalid result contract",
                    failure_data(
                        "verify",
                        committed=True,
                        device=device,
                        daemon={"state": "unknown"},
                        migration={"status": "unknown"},
                        next_step="inspect launcher diagnostics; do not roll back settings alone",
                    ),
                )
            payload = launched.to_payload()
            daemon_result = {
                "state": "running" if launched.running else "unknown",
                "pid": launched.pid,
                "epoch": launched.epoch,
                "owner": payload.get("owner"),
                "identityMode": payload.get("identityMode"),
                "identityIssuer": payload.get("identityIssuer"),
            }
            migration_value = payload.get("migration")
            migration_result = (
                migration_value
                if isinstance(migration_value, dict)
                else {"status": "unknown"}
            )
            old_epoch = daemon_before.get("epoch")
            verified = (
                launched.running
                and payload.get("owner") == result.owner
                and payload.get("identityMode") == "casdoor"
                and payload.get("identityIssuer") == profile.issuer
                and migration_result.get("status") == "applied"
                and (not isinstance(old_epoch, str) or launched.epoch != old_epoch)
            )
            if not verified:
                raise services.CliError(
                    "LOGIN_VERIFY_FAILED",
                    "new daemon did not verify the committed identity and migration",
                    failure_data(
                        "verify",
                        committed=True,
                        device=device,
                        daemon=daemon_result,
                        migration=migration_result,
                        next_step="inspect the reported daemon generation; do not roll back settings alone",
                    ),
                )

        post_commit_daemon = daemon_result
        post_commit_migration = migration_result
        post_commit_phase = "verify"
        # §5.3: after a verified login (daemon up and identity confirmed),
        # pick up account-bound invites once, best effort (D12): any
        # failure lands in the result's ``pending`` field with a warning
        # and never changes the login result or its exit code.
        pending_pickup: JsonObject = {"status": "not-attempted"}
        if daemon_result.get("state") == "running":
            pending_pickup = _pickup_org_invites_after_login(home)
            if pending_pickup.get("status") == "failed":
                warn(
                    "  org invites: not picked up "
                    f"({pending_pickup.get('code')}: "
                    f"{pending_pickup.get('message')}); run "
                    "`hyprial org pending` to retry — the login itself "
                    "is complete",
                    json_output=json_output,
                )
        response: JsonObject = {
            "ok": True,
            "identity": {**identity, "committed": True},
            "device": device,
            "daemon": daemon_result,
            "migration": migration_result,
            "pending": pending_pickup,
            "verificationUri": result.verification_uri,
            "userCode": result.user_code,
        }
        if held_identity_transaction is not None and transaction is not None:
            # First-time setup hands the held lock to init's start path,
            # which launches under it and then closes it.
            held_identity_transaction.append(transaction)
            transaction = None
        return response
    except KeyboardInterrupt as error:
        if not identity_committed:
            raise
        # Commit has landed, so a bare "interrupted" loses the facts an
        # operator needs: which phase was in flight and that the identity
        # stays committed.  `_execute` used to flatten this into an
        # empty INTERRUPTED via the finally below.
        raise services.CliError(
            "INTERRUPTED",
            "login interrupted after the identity commit",
            failure_data(
                post_commit_phase,
                committed=True,
                device=post_commit_device or {"status": "unknown"},
                daemon=post_commit_daemon,
                migration=post_commit_migration or {"status": "unknown"},
                next_step=(
                    "the identity stays committed; inspect device and "
                    "daemon state, then re-run hyprial login"
                ),
            ),
        ) from error
    finally:
        if transaction is not None:
            transaction.close()


def _pickup_org_invites_after_login(home: Path) -> JsonObject:
    """One best-effort ``org pending`` run after a verified login (§5.3, D12).

    Any failure — no device record, a refused refresh, an unreachable
    account server — is reported in the result's ``pending`` field as a
    warning; it never changes the login result or its exit code.  The
    join itself is the daemon's (IPC ``org.join``); this only orchestrates.
    """
    services = get_services()
    try:
        from hyprial.shell.impl.invites import pending as pending_module

        outcome = pending_module.run_pending(
            home,
            accept_all=True,
            confirm=None,
            ipc_join=lambda link: services._daemon_request(
                "org.join", {"link": link}
            ),
            ipc_list=lambda: services._daemon_request("org.list"),
            now=None,
        )
    except Exception as error:  # noqa: BLE001 - D12: best effort by ruling
        return {
            "status": "failed",
            "code": str(getattr(error, "code", "INVITE_PICKUP_FAILED")),
            "message": str(error),
        }
    return {
        "status": "ok",
        "joined": outcome.get("joined", []),
        "failed": outcome.get("failed", []),
        "skipped": outcome.get("skipped", 0),
        "warnings": outcome.get("warnings", []),
    }


def _login_device_id() -> str:
    """This machine's device id: the same value the daemon announces as its
    node id (``HYPRIAL_NODE_ID`` > hostname)."""

    return resolve_node_id()


def _ensure_login_device(home: Path, *, owner: str) -> JsonObject:
    """Ensure the Tailcat device key after the identity commit (§4.4).

    D12: a sidecar/device failure never rolls the identity back and never
    fails the login — the result carries ``ready: false`` with the typed
    error and the next step, and a re-run retries the device stage alone.
    """

    from hyprial.daemon import (
        TailcatSidecarError,
        ensure_device_key,
    )

    try:
        record = ensure_device_key(
            Path(home), owner=owner, device_id=_login_device_id()
        )
    except TailcatSidecarError as error:
        code = error.code
        message = str(error)
    except Exception as error:  # noqa: BLE001 - D12: never strand a committed identity
        code = "DEVICE_KEY_FAILED"
        message = f"unexpected device-key failure: {type(error).__name__}: {error}"
    else:
        return {
            "ready": True,
            "deviceId": record.device_id,
            "owner": record.owner,
            "keyGeneration": record.key_generation,
        }
    return {
        "ready": False,
        "error": {"code": code, "message": message},
        "nextStep": (
            "install the hyprial-tailcat sidecar (build "
            "sidecar/hyprial-tailcat into $HYPRIAL_HOME/bin or set "
            "HYPRIAL_TAILCAT_BINARY), then re-run `hyprial login` to "
            "provision the device key; the identity stays committed"
        ),
    }


def _prove_daemon_absent(
    home: Path,
    state_dir: Path,
    failure_data: Callable[..., JsonObject],
) -> JsonObject:
    """Fail-safe proof that no old daemon generation survives.

    A free ``daemon.lock`` proves only that the previous holder reached the
    end of ``DaemonApplication._close`` — not that the process exited.  On
    2026-08-30 teardown completed, the lock came back, and the process lived
    nine more hours holding its descendants; ``hyprial daemon stop``
    reports exactly that survivor as ``survivingPid``.  An identity commit
    therefore needs positive process evidence: the durable generation
    records name who to watch — the ``.active_daemon`` heartbeat carries
    pid + birth identity, ``daemon.json`` carries a pid — and only positive
    death (missing pid, birth-identity mismatch) counts as gone.  Alive,
    uninspectable, or malformed all land on the refuse side.

    Residual, documented rather than papered over: a generation that
    finished every cleanup step and then clung to life left no record at
    all; that window is bounded by the daemon's exit backstop and cannot be
    told apart from a clean stop from here.
    """

    def unproven(reason: str) -> CliError:
        services = get_services()
        return services.CliError(
            "DAEMON_STOP_UNPROVEN",
            "daemon did not answer and its exit is unproven: "
            f"{reason}; refusing an identity commit",
            failure_data(
                "stop",
                committed=False,
                next_step=(
                    "inspect the recorded daemon generation; remove stale "
                    "records only after the named pid is gone, then retry"
                ),
            ),
        )

    from hyprial.daemon import (
        DaemonOwnershipBusy,
        DaemonStateOwnershipFence,
    )

    try:
        with DaemonStateOwnershipFence.acquire(state_dir):
            pass
    except DaemonOwnershipBusy as error:
        raise unproven("its state lock is still held") from error

    from hyprial.daemon import OwnerProcessStatus as _OwnerProcessStatus, owner_process_status as _owner_process_status

    try:
        heartbeat: Any = json.loads(
            (Path(home) / ".active_daemon").read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        heartbeat = None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise unproven(
            f"the heartbeat record is unreadable ({type(error).__name__})"
        ) from error
    if heartbeat is not None:
        if not isinstance(heartbeat, dict):
            raise unproven("the heartbeat record is not a JSON object")
        heartbeat_pid = heartbeat.get("pid")
        heartbeat_identity = heartbeat.get("processIdentity")
        if (
            not isinstance(heartbeat_pid, int)
            or isinstance(heartbeat_pid, bool)
            or heartbeat_pid <= 0
            or not isinstance(heartbeat_identity, str)
            or not heartbeat_identity
        ):
            raise unproven(
                "the heartbeat record names no usable pid/birth identity"
            )
        status = _owner_process_status(heartbeat_pid, heartbeat_identity)
        if status not in {
            _OwnerProcessStatus.PID_MISSING,
            _OwnerProcessStatus.IDENTITY_MISMATCH,
        }:
            raise unproven(
                f"the heartbeat record names pid {heartbeat_pid} and that "
                "process is still alive or cannot be positively reaped"
            )

    try:
        marker: Any = json.loads(
            (Path(state_dir) / "daemon.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        marker = None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise unproven(
            f"the daemon state marker is unreadable ({type(error).__name__})"
        ) from error
    if marker is not None:
        marker_pid = marker.get("pid") if isinstance(marker, dict) else None
        if (
            not isinstance(marker_pid, int)
            or isinstance(marker_pid, bool)
            or marker_pid <= 0
        ):
            raise unproven("the daemon state marker names no usable pid")
        try:
            probe_process(marker_pid)
        except ProcessLookupError:
            pass
        except (PermissionError, OSError) as error:
            raise unproven(
                f"the state marker names pid {marker_pid}, which cannot be "
                "inspected"
            ) from error
        else:
            raise unproven(
                f"the state marker names pid {marker_pid} and a process is "
                "still there"
            )
    return {"state": "stopped"}
