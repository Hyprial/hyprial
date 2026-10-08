"""Operator settings commands."""

from __future__ import annotations

import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject

config_app = typer.Typer(
    help="Read and write operator switches in settings.json."
)

@config_app.command("set")
def config_set(
    key: str = typer.Argument(
        ...,
        help=(
            "Config key (supported: autoUpgrade, lsRemoteTimeoutSeconds, "
            "forwarding.mode, org.fetchSource, workerProxy.url, "
            "workerProxy.vendors, workerProxy.noProxy, dispatch.reminder)."
        ),
    ),
    value: str = typer.Argument(
        ...,
        help=(
            "New value (autoUpgrade: true|false; lsRemoteTimeoutSeconds: "
            "positive seconds; forwarding.mode: off|auto|on; org.fetchSource: "
            "mesh|orgfs; "
            "workerProxy.url: http(s) URL, empty clears; workerProxy.vendors: "
            "comma list; workerProxy.noProxy: NO_PROXY list, empty = ambient; "
            "dispatch.reminder: text, empty disables)."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Set an operator switch in settings.json.

    ``autoUpgrade`` (spec autoupdate-isolated-home r1) gates every automatic
    self-upgrade: the daemon scheduler and the ``autoupdate run`` timer entry
    point skip until this is explicitly ``true``.  The manual
    ``hyprial upgrade`` is an operator's explicit act and stays ungated.

    ``forwarding.mode`` is the durable forwarding switch: ``auto`` (the
    default) forwards through the sidecar on a sidecar-joined home, ``off``
    disables it -- including any explicit or generated forwarding variables
    -- and ``on`` refuses to start the daemon without it.
    ``HYPRIAL_FORWARDING`` in the daemon's environment still wins.  It takes
    effect on the next daemon start.

    ``workerProxy.*`` is hyprial's own proxy for model-vendor workers:
    ``url`` (http/https; empty clears the whole setting and restores the
    ambient behaviour), ``vendors`` (the model-vendor families that use it,
    default ``openai,anthropic``; every other vendor's worker gets NO proxy
    variables) and ``noProxy`` (the workers' ``NO_PROXY``; empty keeps the
    daemon's own).  It is read at each worker launch, so it applies to the
    next launch without a daemon restart.
    """
    services = get_services()

    def operation() -> JsonObject:
        from hyprial.daemon import ForwardingConfigurationError
        services = get_services()
        from hyprial.daemon import updates
        from hyprial.identity import (
            WORKER_PROXY_FIELDS,
            WORKER_PROXY_SETTINGS_KEY,
            WorkerProxyError,
            write_worker_proxy_field,
        )
        from hyprial.daemon import (
            FORWARDING_SETTINGS_KEY,
            write_forwarding_mode,
        )

        forwarding_key = f"{FORWARDING_SETTINGS_KEY}.mode"
        org_fetch_source_key = "org.fetchSource"
        worker_proxy_keys = {
            f"{WORKER_PROXY_SETTINGS_KEY}.{field}": field
            for field in WORKER_PROXY_FIELDS
        }
        if key == "dispatch.reminder":
            from hyprial.daemon import write_workflow_reminder

            try:
                path = write_workflow_reminder(value, services._hyprial_home())
            except ValueError as error:
                raise services.CliError("INVALID_CONFIGURATION", str(error)) from error
            return {
                "ok": True,
                "key": key,
                "value": value,
                "path": str(path),
            }
        if key in updates.LS_REMOTE_TIMEOUT_KEY_ALIASES:
            try:
                timeout = float(value)
                path = updates.write_ls_remote_timeout(timeout, services._hyprial_home())
            except (ValueError, OverflowError) as error:
                raise services.CliError("INVALID_CONFIGURATION", str(error)) from error
            return {
                "ok": True,
                "key": updates.LS_REMOTE_TIMEOUT_SETTINGS_KEY,
                "value": timeout,
                "path": str(path),
            }
        if key in worker_proxy_keys:
            try:
                path, setting = write_worker_proxy_field(
                    worker_proxy_keys[key], value, services._hyprial_home()
                )
            except WorkerProxyError as error:
                raise services.CliError(
                    "INVALID_CONFIGURATION", str(error), data={"reason": error.code}
                ) from error
            return {
                "ok": True,
                "key": key,
                "path": str(path),
                # The effective setting after this write; None means no url,
                # i.e. workers keep the ambient behaviour.
                WORKER_PROXY_SETTINGS_KEY: (
                    None if setting is None else setting.to_json()
                ),
                "appliesOn": "next worker launch",
            }
        if key == forwarding_key:
            try:
                path = write_forwarding_mode(value, services._hyprial_home())
            except (ValueError, ForwardingConfigurationError) as error:
                raise services.CliError("INVALID_CONFIGURATION", str(error)) from error
            return {
                "ok": True,
                "key": forwarding_key,
                "value": value.strip().lower(),
                "path": str(path),
                "appliesOn": "next daemon start",
            }
        if key == org_fetch_source_key:
            from hyprial.daemon import write_org_fetch_source

            try:
                path = write_org_fetch_source(value, services._hyprial_home())
            except ValueError as error:
                raise services.CliError("INVALID_CONFIGURATION", str(error)) from error
            return {
                "ok": True,
                "key": org_fetch_source_key,
                "value": value.strip().lower(),
                "path": str(path),
                "appliesOn": "next org fetch",
            }
        if key not in updates.AUTOUPGRADE_KEY_ALIASES:
            raise services.CliError(
                "INVALID_CONFIGURATION",
                f"unknown config key {key!r}; supported: "
                + ", ".join(
                    (
                        *updates.AUTOUPGRADE_KEY_ALIASES,
                        *updates.LS_REMOTE_TIMEOUT_KEY_ALIASES,
                        forwarding_key,
                        org_fetch_source_key,
                        "dispatch.reminder",
                        *worker_proxy_keys,
                    )
                ),
            )
        normalized = value.strip().lower()
        if normalized not in ("true", "false"):
            raise services.CliError(
                "INVALID_CONFIGURATION",
                f"{updates.AUTOUPGRADE_SETTINGS_KEY} accepts true|false; "
                f"got {value!r}",
            )
        enabled = normalized == "true"
        try:
            path = updates.write_auto_upgrade(enabled, services._hyprial_home())
        except ValueError as error:
            raise services.CliError("INVALID_CONFIGURATION", str(error)) from error
        return {
            "ok": True,
            "key": updates.AUTOUPGRADE_SETTINGS_KEY,
            "value": enabled,
            "path": str(path),
        }

    services._execute(operation, json_output=json_output)
