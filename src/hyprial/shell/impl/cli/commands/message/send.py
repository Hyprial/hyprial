"""The ``hyprial send`` operator messaging command."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult, warn

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from collections.abc import Mapping
from pathlib import Path
from hyprial.kernel import ipc_errors
import mimetypes
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import (
    CliError,
    JsonObject,
    _message_claim_params,
)

DASH = "\u2014"


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


def _require_summary(value: str | None) -> str:
    """Return the summary unchanged, or refuse locally before any IPC.

    Landing the check here keeps it uniform for ``send`` and ``reply`` and
    independent of the daemon: a missing, blank or non-UTF-8 summary is a
    SUMMARY_REQUIRED error (exit 1), never a click usage error.  The byte
    budget is measured on the value as given -- stripping first would pass an
    over-budget summary under a smaller post-strip size -- and the same raw
    value reaches the daemon.
    """
    from hyprial.daemon import SUMMARY_MAX_BYTES

    if value is None or not value.strip():
        raise CliError(
            ipc_errors.SUMMARY_REQUIRED,
            f"--summary is required (at most {SUMMARY_MAX_BYTES} UTF-8 bytes)",
        )
    try:
        measured = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise CliError(
            ipc_errors.SUMMARY_REQUIRED, "--summary is not valid UTF-8 text"
        ) from None
    if measured > SUMMARY_MAX_BYTES:
        raise CliError(
            ipc_errors.SUMMARY_REQUIRED,
            f"--summary is {measured} bytes; at most {SUMMARY_MAX_BYTES} UTF-8 bytes",
            {"summaryBytes": measured, "maxBytes": SUMMARY_MAX_BYTES},
        )
    return value


def _delivery_rows(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    deliveries = result.get("deliveries")
    if not isinstance(deliveries, list):
        return []
    return [row for row in deliveries if isinstance(row, dict)]


def _target(row: Mapping[str, Any]) -> str:
    target = row.get("target")
    return target if isinstance(target, str) and target else DASH


def _hold_text(row: Mapping[str, Any]) -> str:
    from hyprial.daemon import HOLD_REASON_KEY

    reason = row.get(HOLD_REASON_KEY)
    if not isinstance(reason, str) or not reason:
        # Legacy free-text reason (e.g. "target-not-visible") as a fallback.
        fallback = row.get("reason")
        reason = fallback if isinstance(fallback, str) and fallback else "queued"
    detail = row.get("detail")
    return f"{reason}: {detail}" if isinstance(detail, str) and detail else reason


def _lift_hold(result: dict[str, Any]) -> dict[str, Any]:
    """Project a single queued delivery's hold onto the top level.

    A pure projection: nothing the daemon already put at the top level is
    overwritten, and a fan-out result never generalizes one target's reason
    to every target -- the per-delivery reason stays where it was.
    """
    from hyprial.daemon import HOLD_REASON_KEY

    rows = _delivery_rows(result)
    if not rows:
        return result
    if "queued" not in result:
        result["queued"] = any(row.get("queued") is True for row in rows)
    if len(rows) == 1 and rows[0].get("queued") is True:
        for key in (HOLD_REASON_KEY, "reason"):
            if key in rows[0] and key not in result:
                result[key] = rows[0][key]
    return result


def _open_request_ids(row: Mapping[str, Any]) -> list[str]:
    raw = row.get("openRequestsFrom")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item]


def _warn_open_requests(result: Mapping[str, Any], *, json_output: bool) -> None:
    """Name the request(s) this target still owes a reply to (WP7 field)."""

    rows = _delivery_rows(result)
    warned = False
    for row in rows:
        ids = _open_request_ids(row)
        if not ids:
            continue
        warned = True
        warn(
            f"hyprial: you have pending request(s) from {_target(row)}: "
            f"{', '.join(ids)}; if this is a reply, use "
            "'hyprial reply <messageId>'",
            json_output=json_output,
        )
    if warned:
        return
    ids = _open_request_ids(result)
    if not ids:
        return
    targets = [row["target"] for row in rows if isinstance(row.get("target"), str)]
    who = targets[0] if len(targets) == 1 else "the target"
    warn(
        f"hyprial: you have pending request(s) from {who}: {', '.join(ids)}; "
        "if this is a reply, use 'hyprial reply <messageId>'",
        json_output=json_output,
    )


def _render_send(data: Mapping[str, Any]) -> str:
    """One line per delivery: sent, held (with its reason) or failed."""

    from hyprial.shell.impl.cli.commands.link.render import RESEND_GUARD

    rows = _delivery_rows(data)
    width = max((len(_target(row)) for row in rows), default=0)
    lines: list[str] = []
    queued = False
    for row in rows:
        target = _target(row)
        message_id = row.get("messageId")
        short = (
            f"{message_id[:4]}\u2026"
            if isinstance(message_id, str) and message_id
            else DASH
        )
        if row.get("accepted") is not True:
            code = row.get("code")
            named = code if isinstance(code, str) and code else DASH
            lines.append(f"failed  {target.ljust(width)}  {short}   {named}")
            continue
        if row.get("queued") is True and row.get("expired") is not True:
            queued = True
            lines.append(
                f"held    {target.ljust(width)}  {short}   {_hold_text(row)}"
            )
        elif row.get("queued") is True:
            lines.append(f"expired {short} -> {target}")
        else:
            lines.append(f"sent    {target.ljust(width)}  {short}")
    if queued:
        lines.append(RESEND_GUARD)
    return "\n".join(lines)


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
    summary: str | None = typer.Option(
        None,
        "--summary",
        help="Short summary the peer reads first; required, at most 512 UTF-8 bytes.",
    ),
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
        summary_value = _require_summary(summary)
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
            "summary": summary_value,
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
        if isinstance(result, dict):
            result = _lift_hold(result)
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
        if isinstance(result, Mapping):
            _warn_open_requests(result, json_output=json_output)
            return CliResult(result, render=_render_send)
        return result

    services._execute(operation, json_output=json_output)
