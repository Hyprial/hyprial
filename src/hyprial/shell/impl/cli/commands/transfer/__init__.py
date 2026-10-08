"""Transfer command registration with explicitly supplied CLI services.

Domain imports remain inside callbacks. Importing this package does not
import hyprial.cli, materialize a home, or initialize daemon authorities.

The family is split by responsibility: runtime.py (runtime-intent export and
VM probe), move.py (the visible worker transfer), internal.py (hidden
receive-side protocol commands), bundle.py (bundle
export/validate/receive/land/complete),
credentials.py (credential seal/open).  register_transfer_commands wires them
at the caller's original composition position and in the original order.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import typer

from hyprial.shell.impl.cli.commands.transfer import (
    bundle,
    credentials,
    internal,
    move,
    runtime,
)


class Execute(Protocol):
    def __call__(
        self, operation: Callable[[], Any], *, json_output: bool,
        json_indent: int | None = None, allow_missing_home: bool = False,
    ) -> None: ...


class DaemonRequest(Protocol):
    def __call__(
        self, method: str, params: dict[str, Any] | None = None, *,
        timeout: float = ..., restore_wait: float | None = None,
    ) -> Any: ...


@dataclass(frozen=True)
class TransferCliDependencies:
    """Current callables, resolved once per command invocation by the root CLI."""

    execute: Execute
    daemon_request: DaemonRequest
    state_dir: Callable[[], Path]
    error_type: type[Exception]
    configured_home: Callable[[], tuple[Path, str]]
    default_home: Callable[[], tuple[Path, str]]
    print: Callable[..., None]
    parse_depends: Callable[[list[str] | tuple[str, ...]], list[dict[str, object]]]


@dataclass(frozen=True)
class TransferCommands:
    """Explicit aliases for the pre-existing hyprial.cli callback API."""

    transfer_runtime_downgrade_state: Callable[..., None]
    transfer_runtime_probe: Callable[..., None]
    transfer: Callable[..., None]
    transfer_bundle_export: Callable[..., None]
    transfer_bundle_validate: Callable[..., None]
    transfer_bundle_receive: Callable[..., None]
    transfer_bundle_land: Callable[..., None]
    transfer_credential_seal: Callable[..., None]
    transfer_credential_open: Callable[..., None]
    transfer_bundle_complete: Callable[..., None]
    transfer_precheck: Callable[..., None]
    transfer_receive: Callable[..., None]
    transfer_cred_stage: Callable[..., None]
    transfer_container_home: Callable[..., None]
    transfer_session_path: Callable[..., None]
    parse_depends: Callable[[list[str] | tuple[str, ...]], list[dict[str, object]]]


def register_transfer_commands(
    app: typer.Typer,
    transfer_runtime_app: typer.Typer,
    get_dependencies: Callable[[], TransferCliDependencies],
) -> TransferCommands:
    """Register the family at the caller's original composition position."""
    callbacks: dict[str, Any] = {}
    # Original statement order: transfer_runtime commands, transfer +
    # parse_depends, bundle export/validate/receive/land, credential
    # seal/open, bundle complete, then the hidden receive-side commands.
    callbacks.update(runtime.register_runtime_commands(transfer_runtime_app, get_dependencies))
    callbacks.update(move.register_session_commands(app, get_dependencies))
    callbacks.update(bundle.register_bundle_commands(app, get_dependencies))
    callbacks.update(credentials.register_credential_commands(app, get_dependencies))
    callbacks.update(bundle.register_bundle_complete_command(app, get_dependencies))
    callbacks.update(internal.register_hidden_session_commands(app, get_dependencies))
    return TransferCommands(
        transfer_runtime_downgrade_state=callbacks["transfer_runtime_downgrade_state"],
        transfer_runtime_probe=callbacks["transfer_runtime_probe"],
        transfer=callbacks["transfer"],
        transfer_bundle_export=callbacks["transfer_bundle_export"],
        transfer_bundle_validate=callbacks["transfer_bundle_validate"],
        transfer_bundle_receive=callbacks["transfer_bundle_receive"],
        transfer_bundle_land=callbacks["transfer_bundle_land"],
        transfer_credential_seal=callbacks["transfer_credential_seal"],
        transfer_credential_open=callbacks["transfer_credential_open"],
        transfer_bundle_complete=callbacks["transfer_bundle_complete"],
        transfer_precheck=callbacks["transfer_precheck"],
        transfer_receive=callbacks["transfer_receive"],
        transfer_cred_stage=callbacks["transfer_cred_stage"],
        transfer_container_home=callbacks["transfer_container_home"],
        transfer_session_path=callbacks["transfer_session_path"],
        parse_depends=callbacks["_parse_depends"],
    )
