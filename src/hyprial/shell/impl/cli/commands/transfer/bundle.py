"""Transfer bundle export, validation, receive, landing and completion."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer

from hyprial.kernel import ipc_errors
from hyprial.kernel import resolve_node_id



def register_bundle_commands(app, get_dependencies):

    @app.command("bundle-export")
    def transfer_bundle_export(
        source_dir: Path = typer.Option(
            ...,
            "--from",
            help="Agent home directory to pack (the transfer unit is a copy, not a move).",
        ),
        bundle_dir: Path = typer.Option(
            ...,
            "--to",
            help=(
                "Destination bundle directory: must not exist, or be an empty directory; "
                "it must not be the source home, contain it, or sit inside it."
            ),
        ),
        owner: str = typer.Option(..., "--owner", help="Owner the bundle belongs to."),
        actor: str = typer.Option(..., "--actor", help="Agent actor name being transported."),
        machine: str | None = typer.Option(
            None,
            "--machine",
            help="Source machine id (default: HYPRIAL_NODE_ID, else the host name).",
        ),
        created_at: str | None = typer.Option(
            None,
            "--created-at",
            help="ISO timestamp to record (default: now, local offset).",
        ),
        session_ref: list[str] | None = typer.Option(
            None,
            "--session-ref",
            help="Session reference the bundle carries (repeatable).",
        ),
        grant: list[str] | None = typer.Option(
            None,
            "--grant",
            help="Capability grant the bundle declares (repeatable).",
        ),
        credential_envelope: str | None = typer.Option(
            None,
            "--credential-envelope",
            help=(
                "Bundle-relative path of the sealed credential envelope this bundle "
                "carries (e.g. .transfer/envelope.json, so it lands in payload/). "
                "A declaration the bundle cannot honour refuses the export."
            ),
        ),
        depends: list[str] | None = typer.Option(
            None,
            "--depends",
            help=(
                "Declared dependency (repeatable): 'KIND=PATH' for a file the bundle "
                "carries, or 'KIND=!note' for one the target must provide "
                "(e.g. binary=!ripgrep). A present=true path the bundle lacks is refused."
            ),
        ),
        source_epoch: str | None = typer.Option(
            None,
            "--source-epoch",
            help=(
                "Source agent incarnation identifier (entity token / resource id). "
                "Only its digest is written into the manifest; the value itself "
                "never lands in the bundle. Omit for an offline export."
            ),
        ),
        allow_live: bool = typer.Option(
            False,
            "--allow-live",
            help="Pack even though the source still has a live daemon (best effort, not a snapshot).",
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Pack an agent home into a transfer bundle (AT06 transport unit)."""

        dependencies = get_dependencies()
        _execute = dependencies.execute
        CliError = dependencies.error_type
        _parse_depends = dependencies.parse_depends

        from hyprial.daemon import (
            BundleError,
            SignatureError,
            export_bundle,
            sign_exported_bundle,
        )

        def operation() -> dict[str, object]:
            try:
                manifest = export_bundle(
                    source_dir,
                    bundle_dir,
                    owner=owner,
                    actor=actor,
                    source_machine=machine or resolve_node_id(),
                    created_at=created_at or datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
                    session_refs=session_ref or (),
                    grants=grant or (),
                    credential_envelope=credential_envelope or "",
                    dependencies=_parse_depends(depends or ()),
                    source_epoch=source_epoch or "",
                    allow_live=allow_live,
                )
                # Attribution, the second half of the export: the machine's own
                # key signs the sealed manifest digest, so a target that has
                # enrolled this key can tell where the box came from.  Fail
                # loud rather than hand on a bundle nobody can attribute.
                signature = sign_exported_bundle(
                    bundle_dir,
                    home=Path(dependencies.configured_home()[0]),
                    credential_envelope=manifest.credential_envelope or None,
                )
            except BundleError as error:
                raise CliError(error.code, str(error)) from error
            except SignatureError as error:
                raise CliError(error.code, str(error)) from error
            return {
                "bundle": str(bundle_dir),
                "entries": len(manifest.entries),
                "schema_version": manifest.schema_version,
                "protocol_version": manifest.protocol_version,
                "owner": manifest.owner,
                "actor": manifest.actor,
                "source_machine": manifest.source_machine,
                "created_at": manifest.created_at,
                "session_refs": list(manifest.session_refs),
                "grants": list(manifest.grants),
                "source_epoch": manifest.source_epoch or None,
                "credential_envelope": manifest.credential_envelope or None,
                "dependencies": [dict(item) for item in manifest.dependencies],
                "target_must_provide": [
                    dict(item) for item in manifest.target_must_provide()
                ],
                "allow_live": allow_live,
                "signature": {
                    "keyId": signature.key_id,
                    "digest": signature.digest,
                    "headerDigest": signature.header_digest,
                },
            }

        _execute(operation, json_output=json_output)


    @app.command("bundle-validate")
    def transfer_bundle_validate(
        bundle_dir: Path = typer.Argument(..., help="Bundle directory to verify."),
        expect_owner: str | None = typer.Option(
            None,
            "--expect-owner",
            help="Refuse when the bundle carries a different owner than this one.",
        ),
        expect_actor: str | None = typer.Option(
            None,
            "--expect-actor",
            help="Refuse when the bundle carries a different actor than this one.",
        ),
        expect_epoch: str | None = typer.Option(
            None,
            "--expect-epoch",
            help=(
                "Source incarnation identifier to require; only its digest is "
                "compared. A bundle with no epoch never matches."
            ),
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Verify a transfer bundle before receiving it (AT06 pre-receive gate)."""

        dependencies = get_dependencies()
        _execute = dependencies.execute
        CliError = dependencies.error_type

        from hyprial.daemon import (
            BundleError,
            assert_bundle_identity,
            validate_bundle,
        )

        def operation() -> dict[str, object]:
            try:
                manifest = validate_bundle(bundle_dir)
                assert_bundle_identity(
                    manifest,
                    expect_owner=expect_owner,
                    expect_actor=expect_actor,
                    expect_epoch=expect_epoch,
                )
            except BundleError as error:
                raise CliError(error.code, str(error)) from error
            return {
                "bundle": str(bundle_dir),
                "ok": True,
                "entries": len(manifest.entries),
                "owner": manifest.owner,
                "actor": manifest.actor,
                "source_machine": manifest.source_machine,
                "created_at": manifest.created_at,
                "schema_version": manifest.schema_version,
                "protocol_version": manifest.protocol_version,
                "source_epoch": manifest.source_epoch or None,
            }

        _execute(operation, json_output=json_output)


    @app.command("bundle-receive")
    def transfer_bundle_receive(
        bundle_dir: Path = typer.Argument(..., help="Bundle directory to land."),
        destination: Path = typer.Option(
            ...,
            "--to",
            help="Target agent home; refused unless it is absent or an empty directory.",
        ),
        expect_actor: str | None = typer.Option(
            None,
            "--expect-actor",
            help="Refuse when the bundle carries a different actor than this one.",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Validate and report what would land, without writing anything.",
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Land a validated transfer bundle at its target home (AT07 receive gate)."""

        dependencies = get_dependencies()
        _execute = dependencies.execute
        CliError = dependencies.error_type

        from hyprial.daemon import BundleError
        from hyprial.daemon import ReceiveError, receive_bundle
        from hyprial.daemon import SignatureError, TrustStoreError, trust_policy_for

        configured_home = dependencies.configured_home

        def operation() -> dict[str, object]:
            try:
                result = receive_bundle(
                    bundle_dir,
                    destination,
                    expect_actor=expect_actor,
                    dry_run=dry_run,
                    # AT06 slice 2b: attribution is the target's own offline
                    # anchor.  The CLI never offers a way to skip it.
                    signature=trust_policy_for(Path(configured_home()[0])),
                )
            except (
                BundleError,
                ReceiveError,
                SignatureError,
                TrustStoreError,
            ) as error:
                raise CliError(
                    getattr(error, "code", "receive_error"), str(error)
                ) from error
            payload: dict[str, object] = result.as_dict()
            payload["ok"] = True
            return payload

        _execute(operation, json_output=json_output)


    @app.command("bundle-land")
    def transfer_bundle_land(
        bundle_dir: Path = typer.Argument(..., help="Bundle directory this node has received."),
        actor: str | None = typer.Option(
            None,
            "--expect-actor",
            help="Refuse a bundle whose actor is not this one.",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="Report what a landing would do; change nothing.",
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Land a received bundle as one of this node's own agents (AT07).

        The identity half of a receive, and the only half the CLI cannot do by
        itself: the daemon mints the agent row and the home that incarnation owns,
        moves the payload into it, and materialises the manifest's grants.  A name
        already in use is refused before a byte moves.  Use
        ``transfer-bundle-receive`` only for a destination the registry does not
        manage.
        """

        dependencies = get_dependencies()
        _execute = dependencies.execute
        _daemon_request = dependencies.daemon_request

        # Resolve here: the daemon resolves a relative bundleDir against ITS working
        # directory, so `./bundle` would land on a different directory (or a
        # missing one) than the operator is looking at.  Same convention as the
        # other file arguments crossing IPC.
        params: dict[str, object] = {
            "bundleDir": str(bundle_dir.expanduser().resolve())
        }
        if actor is not None:
            params["actor"] = actor
        if dry_run:
            params["dryRun"] = True
        _execute(lambda: _daemon_request("transfer.land", params), json_output=json_output)

    return {
        "transfer_bundle_export": transfer_bundle_export,
        "transfer_bundle_validate": transfer_bundle_validate,
        "transfer_bundle_receive": transfer_bundle_receive,
        "transfer_bundle_land": transfer_bundle_land,
    }


def register_bundle_complete_command(app, get_dependencies):

    @app.command("bundle-complete")
    def transfer_bundle_complete(
        home: Path = typer.Argument(
            ...,
            help="Source agent home to clean up once the target holds the agent.",
        ),
        bundle_dir: Path = typer.Option(
            ...,
            "--bundle",
            help="Bundle that must still validate before anything on the source is removed.",
        ),
        receipt: str = typer.Option(
            ...,
            "--receipt",
            help="Target confirmation id: the target says it holds the agent.",
        ),
        actor: str = typer.Option(..., "--actor", help="Agent actor being handed over."),
        owner: str = typer.Option(..., "--owner", help="Owner the agent belongs to."),
        staging: list[Path] | None = typer.Option(
            None,
            "--staging",
            help="Staging directory to clean (repeatable).",
        ),
        credential_volume: list[Path] | None = typer.Option(
            None,
            "--credential-volume",
            help="Container credential volume to clean (repeatable).",
        ),
        private_grant: list[Path] | None = typer.Option(
            None,
            "--private-grant",
            help="Private grant file or directory to clean (repeatable).",
        ),
        dry_run: bool = typer.Option(
            False,
            "--dry-run",
            help="List what would be removed, without removing anything.",
        ),
        allow_uncovered: bool = typer.Option(
            False,
            "--allow-uncovered",
            help=(
                "Delete files the bundle does not hold. Without this the removal "
                "is refused when the home has moved on since the export."
            ),
        ),
        allow_external: bool = typer.Option(
            False,
            "--allow-external",
            help=(
                "Delete --staging / --credential-volume / --private-grant paths "
                "that are not under a root this actor owns. Without it those "
                "paths are refused."
            ),
        ),
        keep_registration: bool = typer.Option(
            False,
            "--keep-registration",
            help=(
                "Leave this machine's registration (agent row, routes, "
                "undelivered messages) in place after the files are cleaned. "
                "Without it a completed transfer-out also deregisters the "
                "source, because 'transferred away' means the source no longer "
                "holds the identity."
            ),
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
    ) -> None:
        """Complete a transfer-out: clean the source by identity fence (AT11).

        Cleaning the files is only half of "the agent left this machine": the
        registration row, routes and undelivered messages live in the daemon,
        so a completed cleanup also asks the daemon to destroy the source agent
        (``--keep-registration`` opts out).  Without that step the source keeps
        advertising a delivery target whose home is gone: senders get an
        acknowledgement and nobody answers.
        """

        dependencies = get_dependencies()
        _execute = dependencies.execute
        _daemon_request = dependencies.daemon_request
        CliError = dependencies.error_type

        from hyprial.daemon import (
            CleanupError,
            execute_cleanup,
            plan_cleanup,
        )

        #: A daemon that cannot be reached cannot tell us whether this machine
        #: still holds the agent, so the cleanup stands and the payload says so.
        #: Every other refusal is a registration that exists and did not come
        #: off -- that one is fatal, because it is exactly the ghost this step
        #: exists to prevent.
        #:
        #: This set is a DOMAIN judgment, not the daemon's transient-code list:
        #: it holds only "the daemon could not answer at all" (unreachable, not
        #: ready, transport deadline).  A reachable daemon that refuses has
        #: answered.  ``DAEMON_RESTORING`` deliberately stays out: the request
        #: layer already waits out the restore gate for its bounded budget
        #: (``_restore_wait_budget``), so a refusal arriving here means the
        #: daemon kept refusing past that window -- a hard failure
        #: (``registration_not_removed``), not an unverifiable state.
        #: (Design answer, kanban e75fa678 [0]: the hard failure is intended.)
        unverifiable = frozenset(
            {
                ipc_errors.DAEMON_UNAVAILABLE,
                ipc_errors.DAEMON_DISCONNECTED,
                ipc_errors.DAEMON_NOT_READY,
                ipc_errors.DAEMON_START_FAILED,
                ipc_errors.DAEMON_START_TIMEOUT,
                ipc_errors.IPC_TIMEOUT,
            }
        )

        def deregister() -> dict[str, object]:
            if keep_registration:
                return {"removed": False, "reason": "kept"}
            try:
                result = _daemon_request("agent.destroy", {"name": actor})
            except Exception as error:  # noqa: BLE001 - classified by code below
                code = getattr(error, "code", None)
                if code == ipc_errors.AGENT_NOT_FOUND:
                    return {"removed": False, "reason": "not-registered"}
                if code in unverifiable:
                    return {
                        "removed": False,
                        "reason": "unverified",
                        "code": code,
                        "error": str(error),
                        "hint": f"hyprial agent destroy {actor} --yes",
                    }
                return {
                    "removed": False,
                    "reason": "failed",
                    "code": code,
                    "error": str(error),
                    "hint": f"hyprial agent destroy {actor} --yes",
                }
            return {
                "removed": True,
                "actor": result.get("actor"),
                "stopped": result.get("stopped"),
                "destroyedMessages": result.get("destroyedMessages"),
            }

        def operation() -> dict[str, object]:
            try:
                plan = plan_cleanup(
                    home,
                    actor=actor,
                    owner=owner,
                    staging=staging or (),
                    credential_volumes=credential_volume or (),
                    private_grants=private_grant or (),
                    allow_external=allow_external,
                )
                result = execute_cleanup(
                    plan,
                    target_receipt=receipt,
                    bundle_dir=bundle_dir,
                    dry_run=dry_run,
                    allow_uncovered=allow_uncovered,
                )
            except CleanupError as error:
                raise CliError(error.code, str(error)) from error
            payload = result.as_dict()
            if not result.complete:
                if dry_run:
                    # A preview is not an incomplete cleanup.  Nothing is
                    # removed by construction, so every planned path lands in
                    # ``leftover`` and the receipt answers complete=False --
                    # that IS the plan.  Answering with the real-run failure
                    # code made every successful preview exit as
                    # ``cleanup_incomplete`` (kanban 61364374).
                    payload["registration"] = {
                        "removed": False,
                        "reason": "dry-run",
                    }
                    return payload
                raise CliError(
                    "cleanup_incomplete",
                    "transfer-out is not complete: "
                    + ("; ".join(f"{path}" for path, _reason in result.failed) or "dry run"),
                    payload,
                )
            if dry_run:
                # A dry run cleans nothing, so it deregisters nothing either.
                # (The receipt already refuses to call a dry run complete.)
                # Reachable when the plan had nothing left to remove -- and
                # the fail-safe if a future receipt ever calls a preview
                # complete with paths still present (kanban e75fa678 [1]:
                # keep this branch, it is the only guard that stops a preview
                # from deregistering).
                payload["registration"] = {"removed": False, "reason": "dry-run"}
                return payload
            payload["registration"] = deregister()
            if payload["registration"].get("reason") == "failed":
                raise CliError(
                    "registration_not_removed",
                    "cleaned up the files, but this machine still holds the "
                    f"agent registration: {payload['registration'].get('error')}",
                    payload,
                )
            return payload

        _execute(operation, json_output=json_output)


    # -- transfer receive side (hidden; the orchestrator drives these over SSH) --

    return {
        "transfer_bundle_complete": transfer_bundle_complete,
    }
