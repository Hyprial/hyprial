# Decision record (W2B-2, 2026-11-21):
# - probe_delivery_paths writes delivery-paths-probe.json inside HYPRIAL_HOME/
#   state via a unique same-directory temporary path + os.replace so concurrent
#   invocations cannot overwrite each other's temporary file. CLI entrypoints
#   apply a private umask, so the temporary file needs no explicit chmod.
"""The ``hyprial message send`` operator messaging command.

Decision notes (slice GC5b, owner 2026-11-19): a group is sent to with the
same command.  ``--conversation <g.>`` without ``--to`` sends to the group
(one entry in its document; the daemon refuses a ``--to`` beside it), and
``--mention`` names members by name or member id, mapped to member ids by
one ``conversations.get``.  Without a group id, ``--to`` stays required
with Typer's own usage error, and the direct params are unchanged.
"""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult, warn

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from collections.abc import Mapping
from pathlib import Path
from hyprial.error import codes
from hyprial.kernel import ipc_errors
import json
import mimetypes
import os
import time
import uuid
from typer._click.exceptions import MissingParameter
import typer

from hyprial.shell.impl.cli.commands.message import message_app
from hyprial.shell.impl.cli.commands.message.group.names import (
    is_group,
    member_id,
    read_members,
)
from hyprial.shell.impl.cli.commands.common.support import (
    CliError,
    JsonObject,
    _local_operator_identity,
    _message_claim_params,
)

DASH = "\u2014"
RESEND_GUARD = (
    "the daemon re-evaluates this queued message; do not resend "
    "(the wait is bounded by the message TTL)"
)


def _validate_from_identity(
    ctx: typer.Context, param: typer.CallbackParam, value: str | None
) -> str | None:
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
        answer = services.runtime.request("agent.resolve", {"name": value}, timeout=1.0)
    except (services.CliError, ipc_errors.TransientDaemonError) as error:
        if isinstance(error, services.CliError) and error.code == codes.AMBIGUOUS_TARGET:
            raise typer.BadParameter(str(error), ctx=ctx, param=param) from error
        return value
    if not isinstance(answer, dict) or "known" not in answer:
        return value
    if answer["known"] is True:
        return value
    raise typer.BadParameter(
        f"unknown local actor {value!r}; use a registered alias "
        "(see 'hyprial message targets') or the canonical form "
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
            codes.SUMMARY_REQUIRED,
            f"--summary is required (at most {SUMMARY_MAX_BYTES} UTF-8 bytes)",
        )
    try:
        measured = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise CliError(
            codes.SUMMARY_REQUIRED, "--summary is not valid UTF-8 text"
        ) from None
    if measured > SUMMARY_MAX_BYTES:
        raise CliError(
            codes.SUMMARY_REQUIRED,
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
    reason = row.get("reason")
    return reason if isinstance(reason, str) and reason else "queued"


def _lift_hold(result: dict[str, Any]) -> dict[str, Any]:
    """Project a single queued delivery's reason onto the top level.

    A pure projection: nothing the daemon already put at the top level is
    overwritten, and a fan-out result never generalizes one target's reason
    to every target -- the per-delivery reason stays where it was.
    """

    rows = _delivery_rows(result)
    if not rows:
        return result
    if "queued" not in result:
        result["queued"] = any(row.get("queued") is True for row in rows)
    if len(rows) == 1 and rows[0].get("queued") is True:
        if "reason" in rows[0] and "reason" not in result:
            result["reason"] = rows[0]["reason"]
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
            "'hyprial message reply <messageId>'",
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
        "if this is a reply, use 'hyprial message reply <messageId>'",
        json_output=json_output,
    )


def _render_send(data: Mapping[str, Any]) -> str:
    """One line per delivery: sent, held (with its reason) or failed."""

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


def _require_targets(
    ctx: typer.Context, param: typer.CallbackParam, value: list[str] | None
) -> list[str] | None:
    """``--to`` is required unless ``--conversation`` names a group.

    A missing option is processed after every given one, so a given
    ``--conversation`` is already parsed here; the refusal is Typer's own
    missing-option usage error.
    """

    if ctx.resilient_parsing or value or is_group(ctx.params.get("conversation")):
        return value
    raise MissingParameter(ctx=ctx, param=param)


@message_app.command("send")
def send(
    message: list[str] = typer.Argument(..., help="Message text."),
    source: str | None = typer.Option(
        None,
        "--from",
        help="Local sending actor; defaults to the verified interactive agent.",
        callback=_validate_from_identity,
    ),
    to: list[str] | None = typer.Option(
        None,
        "--to",
        help="Target; repeat for fan-out; omit when --conversation is a group (g.…).",
        callback=_require_targets,
    ),
    summary: str | None = typer.Option(
        None,
        "--summary",
        help="Short summary the peer reads first; required, at most 512 UTF-8 bytes.",
    ),
    topic: str | None = typer.Option(None, "--topic"),
    conversation: str | None = typer.Option(
        None,
        "--conversation",
        help=(
            "Conversation id to send in; omitted, each target's message joins "
            "the default conversation of the sender and that target."
        ),
    ),
    reply_to: str | None = typer.Option(None, "--reply-to"),
    mentions: list[str] | None = typer.Option(
        None,
        "--mention",
        help="Group member to mention (wakes only mentioned agent members), by agent name or member id; repeatable.",
    ),
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
        binding = services.runtime.worker_session_binding()
        if source is None:
            if binding is None:
                raise services.CliError(
                    codes.INVALID_ARGUMENT,
                    "--from is required outside a verified agent session",
                )
            source_value = binding.actor
        else:
            source_value = source
        summary_value = _require_summary(summary)
        text = " ".join(message).strip()
        if not text:
            raise services.CliError(codes.INVALID_ARGUMENT, "message must not be empty")
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
        claims = _message_claim_params(
            source_value, on_behalf_of=on_behalf_of, binding=binding
        )
        params: JsonObject = {
            **claims,
            **({"to": to} if to else {}),
            "message": text,
            "summary": summary_value,
        }
        if topic is not None:
            params["topic"] = topic
        if conversation is not None:
            params["conversationId"] = conversation
        if reply_to is not None:
            params["replyTo"] = reply_to
        if mentions:
            members = (
                read_members(services, claims, conversation)
                if conversation is not None and is_group(conversation)
                else []
            )
            params["mentions"] = [member_id(members, value) for value in mentions]
        if resources:
            params["resourcePaths"] = resources
        result = services.runtime.request("message.send", params)
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
                f"hyprial: sender {source_value!r} has no reply path: "
                "replies to it cannot be delivered",
                json_output=json_output,
            )
        if isinstance(result, Mapping):
            _warn_open_requests(result, json_output=json_output)
            return CliResult(result, render=_render_send)
        return result

    services.runtime.execute(operation, json_output=json_output)


PROBE_TAG = "hyprial-probe"
_PROBE_FAMILIES = ("agent", "user")


def _probe_send(services: Any, source: str, target: str) -> dict[str, Any]:
    text = f"{PROBE_TAG} delivery-path {target}"
    try:
        result = services.runtime.request(
            "message.send",
            {
                **_message_claim_params(source),
                "to": [target],
                "message": text,
                "summary": f"{PROBE_TAG} delivery-path probe",
            },
            timeout=15.0,
        )
    except Exception as error:  # noqa: BLE001 - the probe records every failure
        return {
            "target": target,
            "accepted": False,
            "error": f"{type(error).__name__}: {error}"[:300],
        }
    rows = result.get("deliveries") if isinstance(result, Mapping) else None
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], Mapping) else {}
    return {
        "target": target,
        "accepted": row.get("accepted") is True,
        "messageId": row.get("messageId"),
        **({"queued": row.get("queued")} if row.get("queued") is not None else {}),
        **({"code": row.get("code")} if isinstance(row.get("code"), str) else {}),
    }


@message_app.command("probe-delivery-paths")
def probe_delivery_paths(
    source: str | None = typer.Option(
        None,
        "--from",
        callback=_validate_from_identity,
        help="Local sending actor; defaults to the verified worker binding.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Send one explicitly tagged probe through each delivery family (agent, user)."""

    services = get_services()

    def operation() -> Any:
        services = get_services()
        binding = services.runtime.worker_session_binding()
        if source is None:
            if binding is None:
                raise services.CliError(
                    codes.INVALID_ARGUMENT,
                    "--from is required outside a verified agent session",
                )
            source_value = binding.actor
        else:
            source_value = source
        resolved = source_value
        if ":" not in resolved:
            answer = services.runtime.request(
                "agent.resolve", {"name": source_value}, timeout=1.0
            )
            if isinstance(answer, Mapping) and answer.get("known") is True:
                resolved = str(answer.get("resolved") or resolved)
        # The CLI may not grow backend imports (A-03 ratchet) nor hand-build
        # user URIs: the agent family is the daemon-resolved canonical URI,
        # and the user family is this node's operator (the owner of every
        # local --from actor), built by the existing support helper.  The
        # agent target is not re-parsed here: the daemon's message.send
        # judges it and a rejection is recorded as accepted=false.
        try:
            user_target: str | None = _local_operator_identity()
        except Exception:  # noqa: BLE001 - recorded below as a failed family
            user_target = None
        targets = {"agent": resolved, "user": user_target}
        per_family: dict[str, Any] = {}
        for family in _PROBE_FAMILIES:
            target = targets[family]
            if target is None:
                per_family[family] = {
                    "target": None,
                    "accepted": False,
                    "error": f"cannot derive {family} target from {resolved!r}",
                }
                continue
            per_family[family] = _probe_send(services, source_value, target)
        payload = {
            "ok": all(item.get("accepted") is True for item in per_family.values()),
            "tag": PROBE_TAG,
            "epoch": int(time.time()),
            "per_family": per_family,
        }
        state_dir = Path(services._hyprial_home()) / "state"
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = state_dir / "delivery-paths-probe.json"
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
        return CliResult(payload, render=_render_probe)

    services.runtime.execute(operation, json_output=json_output)


def _render_probe(data: Mapping[str, Any]) -> str:
    lines = [f"delivery-path probe {data.get('tag', PROBE_TAG)}"]
    per_family = data.get("per_family")
    if isinstance(per_family, Mapping):
        for family, item in per_family.items():
            if isinstance(item, Mapping):
                state = "accepted" if item.get("accepted") is True else "FAILED"
                lines.append(f"{family}: {state} {item.get('target') or '-'}")
    return "\n".join(lines)
