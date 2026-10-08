"""``hyprial org`` organization document commands."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import confirm as ask, render_generic

from collections.abc import Mapping
from hyprial.shell.impl.cli.output import CliResult

from hyprial.shell.impl.cli.commands.common.services import get_services

from typing import Any
from pathlib import Path
import difflib
import hashlib
from hyprial.kernel import ipc_errors
from hyprial.identity import org_space_name
import json
import sys
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject
org_app = typer.Typer(
    help=(
        "Inspect and locally adopt organization context, and manage org "
        "membership over the Tailcat directory. Adoption is a local owner "
        "decision: the mesh can only stage candidates, and only "
        "`org import <file>` fills the single accepted slot."
    )
)


# -- org network lifecycle (tailnet cutover §4.4; IPC org.*, W-D §4.3) -------


def _validated_org(value: str) -> str:
    services = get_services()
    try:
        org_space_name(value)
    except ValueError as error:
        raise services.CliError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    return value


@org_app.command("create")
def org_create(
    org: str = typer.Argument(..., help="Casdoor group name (group-NAME)."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Create the org directory and ACL spaces, then publish this device."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        result = services._daemon_request("org.create", {"org": _validated_org(org)})
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.create must return an object")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@org_app.command("invite")
def org_invite(
    org: str = typer.Argument(..., help="Organization name."),
    user: str = typer.Option(
        ..., "--user", help="Username to invite (directory rw, ACL ro)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Invite one user and print the invite link (§5.1).

    The link is a secret (its payload embeds the inviter's Tailcat address
    and a bearer token): the human output is exactly two lines — the
    clickable https form and the copy-paste command — and is never written
    to disk or logs.  ``--json`` passes the daemon's fields through
    verbatim: ``link`` (https), ``deepLink``, ``command``, ``invite``
    (bare token) and ``expiresAt``.
    """
    services = get_services()

    def render(result: Mapping[str, Any]) -> str:
        command = result.get("command")
        lines = [result["link"]]
        if isinstance(command, str) and command:
            lines.append(f"或在终端执行：{command}")
        return "\n".join(lines)

    def operation() -> CliResult:
        services = get_services()
        result = services._daemon_request(
            "org.invite", {"org": _validated_org(org), "user": user}
        )
        if not isinstance(result, dict) or not isinstance(result.get("link"), str):
            raise services.CliError(
                "INVALID_RESPONSE", "org.invite must return the https invite link"
            )
        return CliResult({"ok": True, **result}, render=render)

    services._execute(operation, json_output=json_output)


def _read_link_argument(link: str) -> str:
    """The invite link from the argument, or one line from stdin for '-'."""
    services = get_services()

    text = link
    if text == "-":
        text = sys.stdin.read().strip()
    if not text:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT, "empty invite link"
        )
    return text


def _org_join_via_ipc(method: str, link: str, *, json_output: bool) -> None:
    """Shared body of ``org join``/``org execute``: every link form — https
    URL, ``hyprial://`` deep link, bare token, bare ``v1.`` — is passed
    through untouched; decoding is the daemon's job (§5.1)."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        # The join workflow maps the inviter, redials, and then retries
        # ``fs.join`` until the holder is visible (bounded at 30s
        # daemon-side), so the default 15s IPC roundtrip would cut a slow
        # but healthy join in half.
        result = services._daemon_request(
            method, {"link": _read_link_argument(link)}, timeout=75.0
        )
        if not isinstance(result, dict):
            raise services.CliError(
                "INVALID_RESPONSE", f"{method} must return an object"
            )
        # ``org`` rides in the daemon result; Desktop's deep-link patch
        # depends on it, so keep the passthrough verbatim.
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@org_app.command("join")
def org_join(
    link: str = typer.Argument(
        ...,
        help=(
            "The invite link — https URL, hyprial:// deep link, bare token "
            "or 'v1.<payload>' — or '-' to read it from stdin (keeps the "
            "secret out of the shell history)."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Join an org from any invite-link form (§5.1/§5.2 path 1)."""
    _org_join_via_ipc("org.join", link, json_output=json_output)


@org_app.command("execute")
def org_execute(
    link: str = typer.Argument(
        ...,
        help=(
            "The invite link, or '-' to read it from stdin (keeps the secret "
            "out of the shell history)."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Execute an invite link's declarative join workflow on this node.

    Equivalent to ``org join`` (§5.2); kept under its original name."""
    _org_join_via_ipc("org.execute", link, json_output=json_output)


@org_app.command("leave")
def org_leave(
    org: str = typer.Argument(..., help="Organization name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Leave an org: remove this device and file a leave request."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        result = services._daemon_request("org.leave", {"org": _validated_org(org)})
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.leave must return an object")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@org_app.command("remove")
def org_remove(
    org: str = typer.Argument(..., help="Organization name."),
    user: str = typer.Argument(..., help="Username to remove from the org."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove a user from an org and delete their device rows (owner/admin)."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        result = services._daemon_request(
            "org.remove", {"org": _validated_org(org), "user": user}
        )
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.remove must return an object")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@org_app.command("delete")
def org_delete(
    org: str = typer.Argument(..., help="Organization name."),
    yes: bool = typer.Option(
        False, "--yes", help="Confirm the deletion without an interactive prompt."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Mark an org deleted and drop every non-owner member (owner only).

    Destructive: without ``--yes`` a TTY is asked to confirm; a non-TTY
    invocation without ``--yes`` is refused with USER_ACTION_REQUIRED.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        validated_org = _validated_org(org)
        if not yes:
            if json_output or not services._stdin_isatty():
                raise services.CliError(
                    "USER_ACTION_REQUIRED",
                    f"deleting org {org!r} removes every non-owner member; "
                    "re-run with --yes to confirm",
                )
            if not ask(f"Delete org {org!r} and remove every non-owner member?"):
                raise services.CliError(
                    "DELETE_DECLINED", f"org {org!r} was not deleted"
                )
        result = services._daemon_request("org.delete", {"org": validated_org})
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.delete must return an object")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@org_app.command("list")
def org_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List the orgs this node belongs to (role, devices, state)."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        result = services._daemon_request("org.list")
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.list must return an object")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@org_app.command("pending")
def org_pending(
    accept_all: bool = typer.Option(
        False,
        "--accept-all",
        help="Join every pending invite without asking.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Pick up invites bound to this account and join them (§5.3).

    A TTY without ``--accept-all`` asks per invite; a non-TTY (or
    ``--json``) invocation without ``--accept-all`` only lists what is
    pending and joins nothing.
    """
    services = get_services()

    def operation() -> JsonObject | str:
        services = get_services()
        from hyprial.shell.impl.invites.pending import (
            InvitePickupError,
            run_pending,
        )

        confirm = None
        if not accept_all and not json_output and services._stdin_isatty():

            def confirm(org_name: str, inviter: str) -> bool:  # noqa: F811
                return ask(f"join org {org_name!r} (invited by {inviter})?")

        try:
            result = run_pending(
                services._hyprial_home(),
                accept_all=accept_all,
                confirm=confirm,
                ipc_join=lambda text: services._daemon_request(
                    "org.join", {"link": text}
                ),
                ipc_list=lambda: services._daemon_request("org.list"),
                now=None,
            )
        except InvitePickupError as error:
            raise services.CliError(error.code, str(error)) from error
        return CliResult(result, render=_render_pending)

    services._execute(operation, json_output=json_output)


def _render_pending(result: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for item in result.get("joined", []):
        lines.append(f"joined {item['org']} (invited by {item['inviter']})")
    for item in result.get("failed", []):
        lines.append(f"FAILED {item['org']}: {item['code']}")
    for item in result.get("pending", []):
        lines.append(
            f"pending: {item['org']} (invited by {item['inviter']}, "
            f"accepted {item.get('acceptedAt') or 'unknown'})"
        )
    for warning in result.get("warnings", []):
        lines.append(f"warning: {warning}")
    if not lines:
        lines.append("no pending invites")
    return "\n".join(lines)


@org_app.command("network")
def org_network(
    org: str | None = typer.Argument(
        None, help="Organization name; omit for every org this node belongs to."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show directory members/devices, mapped peers and sidecar status.

    Three panes, each labelled with its source: the OrgFS directory, the
    forwarding sidecar's mapped peers, and the sidecar status.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        params: JsonObject = {}
        if org is not None:
            params["org"] = _validated_org(org)
        result = services._daemon_request("org.network", params)
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.network must return an object")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


# -- org-context document commands (unchanged semantics) ---------------------


def _org_meta(document: Any) -> JsonObject:
    return {
        "version": document.meta.version,
        "issuedAt": document.meta.issued_at.isoformat(),
        "publisher": document.meta.publisher,
    }


@org_app.command("show")
def org_show(
    full: bool = typer.Option(
        False,
        "--full",
        help=(
            "Also print the whole adopted document (members, lines, routing, "
            "norms, residents) and the sha256 of its bytes."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the organization view this node's owner has adopted.

    Org context is public within the organization: ``--full`` lets any user or
    agent read it without opening the accepted file under HYPRIAL_HOME.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import document_summary
        from hyprial.daemon import OrgContextStore

        store = OrgContextStore(services._hyprial_home())
        document = store.load_accepted()
        if document is None:
            return {
                "ok": True,
                "status": "absent",
                "message": "org-context absent",
            }
        record = store.acceptance_record()
        if record is None:  # load_accepted above proved the slot exists
            raise services.CliError("ORG_CONTEXT_CORRUPT", "acceptance metadata is absent")
        return {
            "ok": True,
            "status": "accepted",
            "meta": _org_meta(document),
            "source": {
                "publisher": document.meta.publisher,
            },
            "adoptedAt": record.adopted_at.isoformat().replace("+00:00", "Z"),
            "summary": document_summary(document),
            **(_org_full_document(store, document) if full else {}),
        }

    services._execute(operation, json_output=json_output)


def _org_full_document(store: Any, document: Any) -> JsonObject:
    """The adopted document as JSON, with the digest of the bytes it came from."""

    accepted = store.accepted_path.read_bytes()
    body = {key: value for key, value in document.data.items() if key != "meta"}
    return {
        "documentSha256": hashlib.sha256(accepted).hexdigest(),
        # YAML dates and other scalars become plain JSON values.
        "document": json.loads(json.dumps(body, ensure_ascii=False, default=str)),
    }


@org_app.command("status")
def org_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show accepted-slot and pending-candidate counts."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import OrgContextStore

        return {"ok": True, **OrgContextStore(services._hyprial_home()).status()}

    services._execute(operation, json_output=json_output)


@org_app.command("fetch")
def org_fetch(
    source_target: str | None = typer.Option(
        None,
        "--from",
        help="Node target, or user:<name> while org.fetchSource=orgfs.",
    ),
    timeout: float = typer.Option(2.0, "--timeout", help="Refresh/fetch timeout."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Fetch org-context candidates from the configured source."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        if timeout <= 0 or timeout > 30:
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--timeout must be greater than 0 and at most 30",
            )
        params: JsonObject = {"timeoutSeconds": timeout}
        if source_target is not None:
            params["from"] = source_target
        result = services._daemon_request("org.fetch", params, timeout=timeout + 1.0)
        if not isinstance(result, dict) or not isinstance(
            result.get("candidates"), list
        ):
            raise services.CliError(
                "INVALID_RESPONSE", "daemon org.fetch result must contain candidates"
            )
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@org_app.command("import")
def org_import(
    source: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    force: bool = typer.Option(
        False, "--force", help="Skip the owner confirmation (validation still applies)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Preview and locally adopt one organization view."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from hyprial.daemon import parse_document, serialize_document
        from hyprial.daemon import OrgContextStore, OrgStoreError

        try:
            document = parse_document(source.read_text(encoding="utf-8"))
        except OSError as error:
            raise services.CliError(
                "ORG_CONTEXT_READ_FAILED", f"cannot read org-context {source}: {error}"
            ) from error
        store = OrgContextStore(services._hyprial_home())
        accepted = store.load_accepted()
        previous = "" if accepted is None else serialize_document(accepted)
        incoming = serialize_document(document)
        diff = "".join(
            difflib.unified_diff(
                previous.splitlines(keepends=True),
                incoming.splitlines(keepends=True),
                fromfile="accepted/org-context.md",
                tofile=f"incoming/{source.name}",
            )
        )
        preview: JsonObject = {
            "meta": _org_meta(document),
            "source": {
                "publisher": document.meta.publisher,
            },
            "diff": diff,
        }
        if not force:
            if json_output:
                raise services.CliError(
                    "CONFIRMATION_REQUIRED",
                    "org-context adoption requires owner confirmation; rerun with --force",
                    preview,
                )
            if not ask("Adopt this org-context on this node?", preview=render_generic(preview)):
                raise services.CliError("ADOPTION_DECLINED", "org-context was not adopted")
        record = store.adopt(document)
        try:
            store.queue_orgfs_publish(record.document_sha256)
        except (OrgStoreError, OSError, ValueError):
            # Adoption is the owner-controlled commit point.  A publication
            # marker failure must not turn that successful local decision into
            # a false import failure.
            pass
        published = False
        orgfs_published = False
        try:
            publish_result = services._daemon_request(
                "org.publish", timeout=2.0, restore_wait=0.0
            )
        except (services.CliError, ipc_errors.TransientDaemonError):
            pass
        else:
            published = (
                isinstance(publish_result, dict)
                and publish_result.get("published") is True
            )
            orgfs_published = (
                isinstance(publish_result, dict)
                and publish_result.get("orgfsPublished") is True
            )
        return {
            "ok": True,
            "adopted": True,
            "published": published,
            "orgfsPublished": orgfs_published,
            **preview,
            "adoptedAt": record.adopted_at.isoformat().replace("+00:00", "Z"),
        }

    services._execute(operation, json_output=json_output)
