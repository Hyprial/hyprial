"""Operator messaging commands: send/ack/reply, outbox, delivery."""

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
outbox_app = typer.Typer(help="Inspect and prune the durable outbox.")


delivery_app = typer.Typer(help="Ask what actually happened to messages you sent.")


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


@app.command()
def ack(
    message_id: str = typer.Argument(..., help="Message ID to acknowledge."),
    source: str = typer.Option(..., "--from", help="Local acknowledging actor."),
    on_behalf_of: str | None = typer.Option(
        None,
        "--on-behalf-of",
        help="Visible actor attribution for an operator ack; never the wire sender.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Acknowledge a message without replying."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request(
            "message.ack",
            {
                **_message_claim_params(source, on_behalf_of=on_behalf_of),
                "messageId": message_id,
            },
        ),
        json_output=json_output,
    )


@app.command()
def reply(
    message_id: str = typer.Argument(..., help="Pending message ID to reply to."),
    message: list[str] = typer.Argument(..., help="Reply text."),
    source: str = typer.Option(..., "--from", help="Local replying identity."),
    on_behalf_of: str | None = typer.Option(
        None,
        "--on-behalf-of",
        help="Visible actor attribution for an operator reply; never the wire sender.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Reply to one pending message and acknowledge it after durable acceptance."""
    services = get_services()

    text = " ".join(message).strip()
    if not text:
        raise services.CliError(ipc_errors.INVALID_ARGUMENT, "message must not be empty")
    services._execute(
        lambda: services._daemon_request(
            "message.reply",
            {
                **_message_claim_params(source, on_behalf_of=on_behalf_of),
                "messageId": message_id,
                "message": text,
            },
        ),
        json_output=json_output,
    )


@outbox_app.command("list")
def outbox_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List every queued outbox entry, flagging undeliverable schemes."""
    services = get_services()

    services._execute(lambda: services._daemon_request("outbox.list"), json_output=json_output)


@outbox_app.command("prune")
def outbox_prune(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be pruned without deleting."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Move dead outbox entries (undeliverable scheme or expired TTL) to the DLQ."""
    services = get_services()

    services._execute(
        lambda: services._daemon_request("outbox.prune", {"dryRun": dry_run}),
        json_output=json_output,
    )


@delivery_app.command("status")
def delivery_status(
    source: str = typer.Option(..., "--from", help="Local sending actor."),
    message_id: str | None = typer.Option(
        None, "--message-id", help="One message; omit for every recorded outcome."
    ),
    timeout: float | None = typer.Option(
        None, "--timeout", help="Seconds to wait on other holders (default 2)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the persisted terminal state of messages you sent.

    Every persisted outcome is ``fetched`` (the recipient took it) or
    ``expired`` (the holder TTL elapsed).  Holder response health remains in
    ``diagnostics`` and never changes that terminal outcome.  ``pending`` means
    a holder still has it and no verdict exists yet; ``unknown`` means no
    reachable holder has a record.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        params = _message_claim_params(source)
        if message_id is not None:
            params["messageId"] = message_id
        if timeout is not None:
            params["timeoutSeconds"] = timeout
        return services._daemon_request("message.status", params)

    services._execute(operation, json_output=json_output)
