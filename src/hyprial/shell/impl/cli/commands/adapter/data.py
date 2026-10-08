"""``hyprial adapter identities`` and ``hyprial adapter media``."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from hyprial.kernel import ipc_errors
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.output import CliResult
identities_app = typer.Typer(
    help=(
        "Record and query platform-identity mappings (who is who): "
        "platform id ↔ display name ↔ hyprial owner, with provenance and "
        "confidence. Identity lookups run on recorded data, never on "
        "live-chat deduction."
    )
)


def _identity_json(identity: Any) -> JsonObject:
    return {
        "kind": identity.kind,
        "platformId": identity.platform_id,
        "displayName": identity.display_name,
        "unionId": identity.union_id,
        "hyprialOwner": identity.hyprial_owner,
        "standing": identity.standing,
        "source": identity.source,
        "firstSeenMs": identity.first_seen_ms,
        "lastSeenMs": identity.last_seen_ms,
    }


def _identities_result(name: str, rows: Any) -> JsonObject:
    return {
        "adapter": name,
        "count": len(rows),
        "identities": [_identity_json(row) for row in rows],
    }


def _checked_identity_kind(kind: str | None) -> str | None:
    services = get_services()
    from hyprial.daemon import IDENTITY_KINDS

    if kind is not None and kind not in IDENTITY_KINDS:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"--kind must be one of {', '.join(IDENTITY_KINDS)}",
        )
    return kind


@identities_app.command("sync")
def adapter_identities_sync(
    name: str = typer.Argument(..., help="Configured Lark adapter name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Enumerate this adapter's identities from the platform into the store.

    Records the App itself, its bot open_id, and every member of every group
    the App belongs to — draining every pagination cursor (a truncated member
    view once produced a confidently wrong who-is-who). Uses the adapter's
    own App credential: open_ids are namespaced per App, so ids enumerated
    through any other App would never match this adapter's events. Read-only
    platform calls; nothing is sent to any chat.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import configured_identity_gateway, open_identity_store, sync_identities
        from hyprial.kernel import PersistentConfigError

        try:
            app_id, gateway = configured_identity_gateway(
                services._hyprial_home(), services._state_dir(), name
            )
        except PersistentConfigError as error:
            raise services.CliError(ipc_errors.ADAPTER_NOT_FOUND, str(error)) from error
        store = open_identity_store(services._state_dir(), name)
        try:
            report = sync_identities(state=store, gateway=gateway, app_id=app_id)
        finally:
            store.close()
        return {"adapter": name, **report}

    services._execute(operation, json_output=json_output)


@identities_app.command("list")
def adapter_identities_list(
    name: str = typer.Argument(..., help="Lark adapter name."),
    kind: str | None = typer.Option(
        None, "--kind", help="Filter by identity kind: user, bot or app."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List recorded identities for one adapter's namespace."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import open_identity_store

        _checked_identity_kind(kind)
        store = open_identity_store(services._state_dir(), name)
        try:
            rows = store.identities(kind=kind)
        finally:
            store.close()
        return _identities_result(name, rows)

    services._execute(operation, json_output=json_output)


@identities_app.command("find")
def adapter_identities_find(
    name: str = typer.Argument(..., help="Lark adapter name."),
    display_name: str | None = typer.Option(
        None, "--name", help="Display-name substring to search for."
    ),
    platform_id: str | None = typer.Option(
        None, "--id", help="Exact platform id (open_id or app_id)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Find recorded identities by display name and/or platform id."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import open_identity_store

        if display_name is None and platform_id is None:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "identities find requires --name and/or --id",
            )
        store = open_identity_store(services._state_dir(), name)
        try:
            rows = store.find_identities(name=display_name, platform_id=platform_id)
        finally:
            store.close()
        return _identities_result(name, rows)

    services._execute(operation, json_output=json_output)


media_app = typer.Typer(
    help=(
        "Retrieve platform media referenced by inbound stand-ins. Image and "
        "file messages reach agents as labels carrying a "
        "ref:<message_id>/<key> handle; 'media get' turns that handle into "
        "a local file on demand (directed pull — nothing is auto-inlined)."
    )
)


@media_app.command("get")
def adapter_media_get(
    name: str = typer.Argument(..., help="Configured Lark adapter name."),
    ref: str = typer.Argument(
        ...,
        help=(
            "Media reference as printed in the stand-in label: "
            "<message_id>/<key>, optionally prefixed with 'ref:'."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Download one referenced image/file via the adapter's own credential.

    Works for refs harvested anywhere a stand-in appears — direct messages,
    rich-text posts, and merge-forward children at any nesting depth (the
    ref names its carrying message directly). The payload is stored under
    <hyprial-home>/media/<adapter>/ (0700) and the local path is printed.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        from hyprial.daemon import configured_identity_gateway
        from hyprial.daemon import MediaFetchError, fetch_media, parse_media_ref
        from hyprial.kernel import PersistentConfigError

        try:
            parsed = parse_media_ref(ref)
        except ValueError as error:
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        try:
            # The same credential-loading path as 'identities sync': the
            # adapter's own App secret, resolved locally, never printed.
            _app_id, gateway = configured_identity_gateway(
                services._hyprial_home(), services._state_dir(), name
            )
        except PersistentConfigError as error:
            raise services.CliError(ipc_errors.ADAPTER_NOT_FOUND, str(error)) from error
        try:
            result = fetch_media(
                gateway,
                adapter=name,
                ref=parsed,
                media_root=services._hyprial_home() / "media",
            )
        except MediaFetchError as error:
            # Already sanitized: summaries only, never SDK error text.
            raise services.CliError("MEDIA_FETCH_FAILED", str(error)) from error
        return CliResult({
            "adapter": result.adapter,
            "messageId": result.message_id,
            "key": result.key,
            "resourceType": result.resource_type,
            "path": str(result.path),
            "sizeBytes": result.size_bytes,
            "contentType": result.content_type,
            "fileName": result.file_name,
        }, render=lambda data: str(data["path"]))

    services._execute(operation, json_output=json_output)
