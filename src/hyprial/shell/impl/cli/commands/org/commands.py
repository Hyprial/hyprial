"""``hyprial org`` organization document commands.

The command bodies for the ``context`` and ``member`` subfamilies live in
their own subpackages; this module owns the two groups, the org-name grammar,
the helpers the subpackages reach back for, and registers every subcommand
callable once -- in the historical order, so the generated command inventory
changes only the attributed module.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import typer

from hyprial.identity import ORG_SPACE_PREFIX, org_space_name
from hyprial.error import codes
from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.groups import group
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.commands.org.acl import acl_app
from hyprial.shell.impl.cli.commands.org.authority import (
    org_migration_status,
    org_pin_authority,
)
from hyprial.shell.impl.cli.commands.org.context import commands as context_commands
from hyprial.shell.impl.cli.commands.org.maintenance import org_repin
from hyprial.shell.impl.cli.commands.org.member import commands as member_commands
from hyprial.shell.impl.cli.output import CliResult, render_generic


org_app = group(
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
org_app.command('pin-authority')(org_pin_authority)
org_app.command('migration-status')(org_migration_status)

context_app = group(
    help=(
        "Inspect, validate, and locally adopt the organization context document. "
        "`fetch` stages candidates; `import` is the single owner-adopted slot; "
        "`show`/`status` read what this node has accepted; `validate` checks "
        "the fenced YAML and the accepted slot's adoption state."
    )
)
org_app.add_typer(context_app, name="context")

context_app.command("validate")(context_commands.org_context_validate)

# `org group invite` is the spelling callers have used; keep it as an alias of
# `org invite` rather than a second implementation.  Registering the SAME
# callable means the two entry points cannot drift apart.
group_app = group(help="Alias group for organization subcommands.")


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
            codes.INVALID_ARGUMENT,
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
    parent: str | None = typer.Option(
        None,
        "--parent",
        help="Optional parent organization name for creating a sub-org.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Create local org spaces, then initialize Tailcat and publish when available."""
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        payload: JsonObject = {"org": _validated_org(org)}
        if parent is not None:
            payload["parent"] = _validated_org(parent)
        result = services.runtime.request("org.create", payload)
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.create must return an object")
        published = result.get("published", result.get("devicePublished"))
        local_create = result.get("tailnetInitialized") is False
        return {"ok": local_create or published is not False, **result}

    services.runtime.execute(operation, json_output=json_output)


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
        result = services.runtime.request("org.repair", params)
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.repair must return an object")
        return {"ok": True, **result}

    services.runtime.execute(operation, json_output=json_output)


# -- member subcommands (bodies in ``org/member``) ---------------------------

org_app.command("invite")(member_commands.org_invite)
org_app.command("join")(member_commands.org_join)
org_app.command("leave")(member_commands.org_leave)
org_app.command("remove")(member_commands.org_remove)
group_app.command("invite")(member_commands.org_invite)
org_app.add_typer(group_app, name="group")


def _read_link_argument(link: str) -> str:
    """The invite link from the argument, or one line from stdin for '-'."""
    services = get_services()

    text = link
    if text == "-":
        text = sys.stdin.read().strip()
    if not text:
        raise services.CliError(
            codes.INVALID_ARGUMENT, "empty invite link"
        )
    return text


@org_app.command("delete")
def org_delete(
    org: str = typer.Argument(..., help="Organization name."),
    yes: bool = typer.Option(
        False, "--yes", help="Confirm the deletion without an interactive prompt."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Delete an org: tombstone it, shut its tailnet down, clean up (owner only).

    Destructive and irreversible: the CLI first previews the deletion, then
    confirms the preview's exact operation. Without ``--yes`` a TTY is asked
    to confirm; a non-TTY invocation without ``--yes`` is refused with
    USER_ACTION_REQUIRED.
    """
    services = get_services()

    def operation() -> JsonObject:
        services = get_services()
        from .removal import ConfirmationPrompt, confirmed_removal

        validated_org = _validated_org(org)

        def describe(preview: JsonObject) -> str:
            if preview.get("tailnetPhase") == "inherited":
                return (
                    f"Delete sub-org {validated_org!r}: tombstone it and revoke its "
                    "members; the inherited parent tailnet stays open."
                )
            devices = preview.get("deviceIds")
            count = len(devices) if isinstance(devices, list) else 0
            return (
                f"Delete org {validated_org!r}: tombstone it and shut down its "
                f"tailnet ({count} non-authority device(s))."
            )

        prompt = ConfirmationPrompt(
            question=f"Delete org {validated_org!r}?",
            unconfirmed=(
                "USER_ACTION_REQUIRED",
                f"deleting org {validated_org!r} tombstones it and shuts "
                "its tailnet down; re-run with --yes to confirm",
            ),
            declined=("DELETE_DECLINED", f"org {validated_org!r} was not deleted"),
            describe=describe,
            bind_devices=False,
            interactive=not json_output,
        )
        result = confirmed_removal(
            services, "org.delete", {"org": validated_org}, yes=yes, prompt=prompt
        )
        return {"ok": True, **result}

    services.runtime.execute(operation, json_output=json_output)


@org_app.command("use")
def org_use(
    org: str | None = typer.Argument(None, help="Active organization to use by default."),
    clear: bool = typer.Option(False, "--clear", help="Clear this home's current organization."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Set or clear the daemon-owned default organization for this home."""
    services = get_services()

    def operation() -> CliResult:
        params = {**({"org": org} if org is not None else {}), **({"clear": True} if clear else {})}
        result = services.runtime.request("org.use", params)
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.use must return an object")
        return CliResult(result, render=render_generic)

    services.runtime.execute(operation, json_output=json_output)


@org_app.command("list")
def org_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List organizations with kind, role, current selection, and state."""
    services = get_services()

    def operation() -> CliResult:
        services = get_services()
        result = services.runtime.request("org.list")
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.list must return an object")
        return CliResult({"ok": True, **result}, render=render_generic)

    services.runtime.execute(operation, json_output=json_output)

org_app.command("pending")(member_commands.org_pending)

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
        result = services.runtime.request("org.network", params)
        if not isinstance(result, dict):
            raise services.CliError("INVALID_RESPONSE", "org.network must return an object")
        return {"ok": True, **result}

    services.runtime.execute(operation, json_output=json_output)


# -- org-context document commands (unchanged semantics) ---------------------

context_app.command("show")(context_commands.org_show)
context_app.command("status")(context_commands.org_status)
context_app.command("fetch")(context_commands.org_fetch)
context_app.command("import")(context_commands.org_import)


def _org_context_documents() -> Any:
    """Resolve the local org-context document helpers on first use.

    These are the public daemon helpers ``org context`` reads and writes with.
    The CLI->backend import ledger is exact per file, and this module is where
    it records them, so the extracted command bodies in
    ``org/context/commands.py`` reach them through this port instead of moving
    the imports to a file the ledger does not know.
    """

    from hyprial.daemon import (
        OrgContextStore,
        OrgStoreError,
        document_summary,
        parse_document,
        serialize_document,
        validate_document,
    )

    return SimpleNamespace(
        store=OrgContextStore,
        store_error=OrgStoreError,
        document_summary=document_summary,
        parse_document=parse_document,
        serialize_document=serialize_document,
        validate_document=validate_document,
        ipc_errors=ipc_errors,
    )
