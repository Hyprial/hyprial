"""``hyprial org`` organization document commands."""

from __future__ import annotations

from datetime import UTC, datetime
import time
import webbrowser

from hyprial.shell.impl.cli.output import (
    confirm as ask,
    notice,
    progress,
    render_generic,
    warn,
)

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

from hyprial.identity import ORG_SPACE_PREFIX
from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.commands.org.acl import acl_app
from .maintenance import org_repin

org_app = typer.Typer(
    help=(
        "Inspect and locally adopt organization context, and manage org "
        "membership over the Tailcat directory. Adoption is a local owner "
        "decision: the mesh can only stage candidates, and only "
        "`org context import <file>` fills the single accepted slot."
        " `org context validate` checks the accepted bytes against their "
        "adoption record and orgfs publish marker before or after a hand edit."
    )
)
org_app.add_typer(acl_app, name="acl")
org_app.command('repin')(org_repin)

context_app = typer.Typer(
    help=(
        "Inspect, validate, and locally adopt the organization context document. "
        "`fetch` stages candidates; `import` is the single owner-adopted slot; "
        "`show`/`status` read what this node has accepted; `validate` checks "
        "the fenced YAML and the accepted slot's adoption state."
    )
)
org_app.add_typer(context_app, name="context")


def _context_validate_render(data: Mapping[str, Any]) -> str:
    if data.get("ok") and data.get("valid"):
        meta = data.get("meta") or {}
        return (
            f"org-context OK: version {meta.get('version')} "
            f"published by {meta.get('publisher')}\n{data.get('path')}"
        )
    if data.get("valid"):
        return (
            "org-context ADOPTION STATE INVALID\n"
            f"  {data.get('path')}\n"
            f"  {data.get('reason')}"
        )
    return (
        "org-context INVALID\n"
        f"  {data.get('path')}: line {data.get('line')}, column {data.get('column')}\n"
        f"  {data.get('reason')}"
    )


@context_app.command("validate")
def org_context_validate(
    path: Path | None = typer.Option(
        None,
        "--path",
        help=(
            "Validate this file instead of the accepted org-context.md slot. "
            "This is a format check only: the accepted slot's adoption record "
            "and any orgfs publish marker are not checked for this file."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Validate org-context.md before or after a hand edit.

    Parses only the fenced YAML block.  On failure it names the file line,
    column and reason and exits 1; an invalid document is never reported as
    valid.  This is the manual pre-flight for the accepted slot, which is
    written by adoption from a file the owner may have edited by hand.
    """
    services = get_services()

    def operation() -> CliResult:
        services = get_services()
        from hyprial.daemon import OrgContextStore, validate_document

        target = (
            path if path is not None else services._hyprial_home() / "org-context.md"
        )
        try:
            payload = target.read_bytes()
        except FileNotFoundError:
            raise services.CliError(
                "ORG_CONTEXT_ABSENT", f"org-context absent: {target}"
            ) from None
        except OSError as error:
            raise services.CliError(
                "ORG_CONTEXT_READ_FAILED",
                f"cannot read org-context {target}: {error}",
            ) from error
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            return CliResult(
                {
                    "ok": False,
                    "valid": False,
                    "path": str(target),
                    "line": None,
                    "column": None,
                    "reason": f"org-context is not valid UTF-8: {error}",
                },
                render=_context_validate_render,
            )
        # Two separate facts: the audit digest is over the raw bytes above
        # (accepted_slot_issue receives ``payload``), while the format check
        # runs on the newline-normalized text ``read_text`` used to produce.
        # A legal file whose newlines are all CRLF is Markdown, not a fence
        # violation, so normalizing here keeps the pre-flight non-regressive.
        source = text.replace("\r\n", "\n").replace("\r", "\n")
        result = validate_document(source)
        if not result.ok:
            issue = result.issue
            return CliResult(
                {
                    "ok": False,
                    "valid": False,
                    "path": str(target),
                    "line": None if issue is None else issue.line,
                    "column": None if issue is None else issue.column,
                    "reason": (
                        "org-context is invalid" if issue is None else issue.reason
                    ),
                },
                render=_context_validate_render,
            )
        document = result.document
        assert document is not None
        if path is None:
            # The default slot claims to be the adopted view: the format being
            # valid is not enough, the audit record must describe these exact
            # raw bytes.  --path stays a format pre-flight.
            slot_issue = OrgContextStore(
                services._hyprial_home()
            ).accepted_slot_issue(document, payload)
            if slot_issue is not None:
                return CliResult(
                    {
                        "ok": False,
                        "valid": True,
                        "slot": "accepted",
                        "path": str(target),
                        "line": None,
                        "column": None,
                        "reason": slot_issue,
                    },
                    render=_context_validate_render,
                )
        return CliResult(
            {
                "ok": True,
                "valid": True,
                "slot": "accepted" if path is None else "file",
                "path": str(target),
                "meta": _org_meta(document),
            },
            render=_context_validate_render,
        )

    services._execute(operation, json_output=json_output, allow_missing_home=True)
# `org group invite` is the spelling callers have used; keep it as an alias of
# `org invite` rather than a second implementation.  Registering the SAME
# callable means the two entry points cannot drift apart.
group_app = typer.Typer(help="Alias group for organization subcommands.")


# -- org network lifecycle (tailnet cutover §4.4; IPC org.*, W-D §4.3) -------


_ORG_NAME_HELP = (
    "letters, digits and hyphens only (lowercase, at most "
    f"{63 - len(ORG_SPACE_PREFIX)} characters), and the name must not end "
    "with '-acl'"
)


def _validated_org(value: str) -> str:
    """Return the directory space id for a user-supplied organization name.

    The user names the organization; the ``group-`` prefix is an internal
    space-id detail.  ``foo`` and ``group-foo`` therefore name the same
    organization, and the prefix is added here rather than asked for.  The
    grammar itself is still ``org_space_name``'s: there is one rule set.
    """
    services = get_services()
    # Strip a leading prefix ONCE, then re-add it.  This makes the mapping
    # idempotent (group-foo -> group-foo, foo -> group-foo) and defines the two
    # edges instead of leaving them to chance:
    #   "group-"        -> empty remainder -> rejected with a message about the NAME,
    #                      not about a missing prefix the user never typed;
    #   "group-group-x" -> remainder "group-x" -> "group-group-x" (stripped once only,
    #                      so a real name that starts with "group-" keeps working).
    # The prefix comparison is case sensitive: "GROUP-foo" is not the prefix, so it
    # becomes "group-GROUP-foo" and the grammar rejects it.  We never silently
    # lower-case what the user typed.
    remainder = value
    if remainder.startswith(ORG_SPACE_PREFIX):
        remainder = remainder[len(ORG_SPACE_PREFIX) :]
    candidate = ORG_SPACE_PREFIX + remainder
    try:
        org_space_name(candidate)
    except ValueError as error:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            f"invalid organization name {value!r}: {_ORG_NAME_HELP}",
        ) from error
    return candidate


@org_app.command("create")
def org_create(
    org: str = typer.Argument(
        ...,
        help=(
            "Organization name. `foo` and `group-foo` name the same "
            "organization."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Create the org directory and ACL spaces, then publish this device."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        result = services._daemon_request("org.create", {"org": _validated_org(org)})
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.create must return an object")
        published = result.get("published", result.get("devicePublished"))
        return {"ok": published is not False, **result}

    services._execute(operation, json_output=json_output)


@org_app.command("repair")
def org_repair(
    org: str | None = typer.Argument(
        None,
        help="Optional organization name; omitted scans all incomplete creations.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Resume repairable creator-owned creations. Default is dry-run.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Preview incomplete org creations, or resume them with --apply."""
    services = get_services()

    def operation() -> JsonObject:
        params: JsonObject = {"apply": apply}
        if org is not None:
            params["org"] = _validated_org(org)
        result = services._daemon_request("org.repair", params)
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.repair must return an object")
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


# The login modules import hyprial.daemon; importing them at module level would
# load the daemon package on every `hyprial` invocation (see
# test_importing_the_cli_does_not_drag_in_the_daemon_package). These module-level
# names stay patchable and import on first use.
def obtain_binding_proof(*args: Any, **kwargs: Any) -> str:
    from hyprial.shell.impl.login.binding import obtain_binding_proof as obtain

    return obtain(*args, **kwargs)


def validate_binding_confirmation(*args: Any, **kwargs: Any) -> None:
    from hyprial.shell.impl.login.binding import (
        validate_binding_confirmation as validate,
    )

    validate(*args, **kwargs)


def _retry_binding_proof_before_join(*, json_output: bool) -> None:
    """Best-effort proof retry; the org join itself is never conditional."""

    from hyprial.shell.impl.login.flow import LoginError

    services = get_services()
    home = Path(services._hyprial_home())
    try:
        settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(settings, dict) or settings.get("identityMode") != "casdoor":
        return
    owner = settings.get("owner")
    claims = settings.get("identityClaims")
    properties = claims.get("properties") if isinstance(claims, dict) else None
    union_id = (
        properties.get("oauth_Lark_unionId")
        if isinstance(properties, dict)
        else None
    )
    if not isinstance(owner, str) or not isinstance(union_id, str):
        return
    existing = settings.get("identityBinding")
    if (
        isinstance(existing, dict)
        and existing.get("user") == owner
        and existing.get("larkUnionId") == union_id
    ):
        return

    def emit(kind: str, data: dict[str, Any]) -> None:
        if kind != "binding-device":
            return
        restart = " (fresh code after expiry)" if data.get("restarted") else ""
        message = (
            f"binding proof confirmation required{restart} — open this URL "
            f"and enter code {data['userCode']}: {data['verificationUri']}"
        )
        if json_output:
            progress(kind, message, json_output=True, **data)
        else:
            notice(message)

    def open_url(url: str) -> bool:
        try:
            return webbrowser.open(url)
        except Exception:  # noqa: BLE001 - URI remains visible in the notice
            return False

    try:
        profile, _source = services.resolve_profile()
        proof = obtain_binding_proof(
            profile,
            emit=emit,
            open_browser=True,
            open_url=open_url,
            sleep=time.sleep,
            clock=time.monotonic,
        )
        validate_binding_confirmation(
            proof, expected_user=owner, expected_union_id=union_id
        )
        from hyprial.daemon import write_settings_identity

        write_settings_identity(
            owner,
            mode="casdoor",
            issuer=settings.get("identityIssuer"),
            user_id=(
                settings.get("userId")
                if isinstance(settings.get("userId"), str)
                else None
            ),
            username=(
                settings.get("username")
                if isinstance(settings.get("username"), str)
                else None
            ),
            lark_union_id=union_id,
            identity_binding={
                "user": owner,
                "larkUnionId": union_id,
                "proof": proof,
                "publishedAt": datetime.now(UTC).isoformat(),
            },
            hyprial_home=home,
        )
    except LoginError as error:
        message = f"{error}; will retry on the next login or org join"
        progress(
            "binding-proof-failed",
            message,
            json_output=json_output,
            code=error.code,
            detail=message,
        )
    except Exception as error:  # noqa: BLE001 - proof is non-fatal to joining
        message = (
            f"binding proof retry failed ({type(error).__name__}); will retry "
            "on the next login or org join"
        )
        progress(
            "binding-proof-failed",
            message,
            json_output=json_output,
            code=type(error).__name__,
            detail=message,
        )


def _org_join_via_ipc(method: str, link: str, *, json_output: bool) -> None:
    """Body of ``org join``: every link form — https URL, ``hyprial://``
    deep link, bare token, bare ``v1.`` — is passed through untouched;
    decoding is the daemon's job (§5.1)."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        _retry_binding_proof_before_join(json_output=json_output)
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


group_app.command("invite")(org_invite)
org_app.add_typer(group_app, name="group")


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


@org_app.command("leave")
def org_leave(
    org: str = typer.Argument(..., help="Organization name."),
    yes: bool = typer.Option(False, '--yes', help='Confirm device cutoff.'),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Leave an org: remove this device and file a leave request."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from .removal import confirmed_removal
        result = confirmed_removal(services, 'org.leave', {'org': _validated_org(org)}, yes=yes)
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.leave must return an object")
        return {"ok": True, **result}

    services._execute(operation, json_output=json_output)


@org_app.command("remove")
def org_remove(
    org: str = typer.Argument(..., help="Organization name."),
    user: str = typer.Argument(..., help="Username to remove from the org."),
    yes: bool = typer.Option(False, '--yes', help='Confirm cutoff of all user devices.'),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove OrgFS membership, then revoke fixed sidecar devices (owner/admin).

    Removing someone from the org also removes them from the org's Casdoor
    group, and with it from anything else that relies on that group.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from .removal import confirmed_removal
        result = confirmed_removal(services, 'org.remove',
                                   {'org': _validated_org(org), 'user': user}, yes=yes)
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.remove must return an object")
        group = result.get("casdoorGroup")
        if isinstance(group, str) and group.startswith("removed from "):
            result = {
                **result,
                "note": (
                    f"{user} was also {group}; other applications that rely "
                    "on that Casdoor group lose them too"
                ),
            }
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
    """The displayable fields the parsed body backs (never an adoption time).

    ``OrgMeta.to_json`` is the single recovery function; ``org status`` reads
    the same view from the parsed body, so show and status never disagree.
    """

    return dict(document.meta.to_json())


def _org_human_view(data: Mapping[str, Any]) -> str:
    """The human view of an org result without the machine-only warnings.

    ``warnings`` is the ``--json`` home of the warning the ``warn`` channel
    already wrote to stderr; rendering it again on stdout would report the
    same fact twice in the human mode.
    """

    return render_generic(
        {key: value for key, value in data.items() if key != "warnings"}
    )


@context_app.command("show")
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

    def operation() -> CliResult:
        services = get_services()
        from hyprial.daemon import document_summary
        from hyprial.daemon import OrgContextStore, OrgStoreError

        store = OrgContextStore(services._hyprial_home())
        document = store.load_accepted()
        if document is None:
            return CliResult(
                {
                    "ok": True,
                    "status": "absent",
                    "message": "org-context absent",
                },
                render=_org_human_view,
            )
        try:
            record = store.acceptance_record()
        except OrgStoreError as error:
            record = None
            warning: JsonObject | None = {
                "code": "ORG_ACCEPTANCE_METADATA_UNAVAILABLE",
                "message": str(error),
            }
        else:
            warning = (
                None
                if record is not None
                else {
                    "code": "ORG_ACCEPTANCE_METADATA_UNAVAILABLE",
                    "message": (
                        "accepted org-context has no readable adoption record; "
                        "showing the parsed body only"
                    ),
                }
            )
        if warning is not None:
            # A person reads this on stderr; --json carries it in the result.
            warn(f"hyprial: {warning['message']}", json_output=json_output)
        result: JsonObject = {
            "ok": True,
            "status": "accepted",
            "meta": _org_meta(document),
            "source": {
                "publisher": document.meta.publisher,
            },
            "summary": document_summary(document),
            **(_org_full_document(store, document) if full else {}),
        }
        if record is not None:
            result["adoptedAt"] = record.adopted_at.isoformat().replace("+00:00", "Z")
        if warning is not None:
            result["warnings"] = [warning]
        return CliResult(result, render=_org_human_view)

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


@context_app.command("status")
def org_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show accepted-slot and pending-candidate counts."""
    services = get_services()

    def operation() -> CliResult:
        services = get_services()
        from hyprial.daemon import OrgContextStore

        status = OrgContextStore(services._hyprial_home()).status()
        for warning in status.get("warnings", []):
            warn(f"hyprial: {warning['message']}", json_output=json_output)
        return CliResult({"ok": True, **status}, render=_org_human_view)

    services._execute(operation, json_output=json_output)


@context_app.command("fetch")
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


@context_app.command("import")
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
