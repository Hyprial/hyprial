"""The ``hyprial send`` operator messaging command."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import warn

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from pathlib import Path
from hyprial.kernel import ipc_errors
import mimetypes
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject, _message_claim_params


def _validate_from_identity(
    ctx: typer.Context, param: typer.CallbackParam, value: str
) -> str:
    """Reject an unresolvable bare ``--from`` before anything is sent.

    The daemon is the single source of resolution truth: ``agent.resolve``
    runs the same pipeline ``message.send`` uses, so this check cannot drift
    from the delivery boundary.  Any uncertainty -- daemon unreachable,
    timeout, malformed answer -- fails open and lets the daemon judge at
    send time: a validator that blocks legitimate sends trains people to
    bypass it.  Shell completion runs callbacks under resilient parsing;
    doing IPC (or raising) there would break completion.
    """
    services = get_services()

    if ctx.resilient_parsing or not value or ":" in value:
        return value
    try:
        answer = services._daemon_request("agent.resolve", {"name": value}, timeout=1.0)
    except (services.CliError, ipc_errors.TransientDaemonError) as error:
        if isinstance(error, services.CliError) and error.code == ipc_errors.AMBIGUOUS_TARGET:
            raise typer.BadParameter(str(error), ctx=ctx, param=param) from error
        return value
    if not isinstance(answer, dict) or "known" not in answer:
        return value
    if answer["known"] is True:
        return value
    raise typer.BadParameter(
        f"unknown local actor {value!r}; use a registered alias "
        "(see 'hyprial targets') or the canonical form "
        "agent:<owner>:<node>:<actor>",
        ctx=ctx,
        param=param,
    )


@app.command()
def send(
    message: list[str] = typer.Argument(..., help="Message text."),
    source: str = typer.Option(
        ...,
        "--from",
        help="Local sending actor.",
        callback=_validate_from_identity,
    ),
    to: list[str] = typer.Option(..., "--to", help="Target; repeat for fan-out."),
    topic: str | None = typer.Option(None, "--topic"),
    conversation: str | None = typer.Option(None, "--conversation"),
    reply_to: str | None = typer.Option(None, "--reply-to"),
    files: list[Path] | None = typer.Option(None, "--file"),
    images: list[Path] | None = typer.Option(None, "--image"),
    on_behalf_of: str | None = typer.Option(
        None,
        "--on-behalf-of",
        help="Visible actor attribution for an operator send; never the wire sender.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Send a message through the daemon."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        text = " ".join(message).strip()
        if not text:
            raise services.CliError(ipc_errors.INVALID_ARGUMENT, "message must not be empty")
        resources = []
        for kind, paths in (("file", files or []), ("image", images or [])):
            for path in paths:
                resources.append(
                    {
                        "path": str(path.expanduser().resolve()),
                        "kind": kind,
                        "mediaType": mimetypes.guess_type(path.name)[0]
                        or "application/octet-stream",
                    }
                )
        params: JsonObject = {
            **_message_claim_params(source, on_behalf_of=on_behalf_of),
            "to": to,
            "message": text,
        }
        if topic is not None:
            params["topic"] = topic
        if conversation is not None:
            params["conversationId"] = conversation
        if reply_to is not None:
            params["replyTo"] = reply_to
        if resources:
            params["resourcePaths"] = resources
        result = services._daemon_request("message.send", params)
        if (
            not json_output
            and isinstance(result, dict)
            and result.get("replyPathUnavailable") is True
        ):
            # Facts only (#67): what is true about this sender's reply path.
            # No instructions.
            warn(
                f"hyprial: sender {source!r} has no reply path: "
                "replies to it cannot be delivered",
                json_output=json_output,
            )
        return result

    services._execute(operation, json_output=json_output)
