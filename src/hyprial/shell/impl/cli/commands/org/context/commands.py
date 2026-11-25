"""``hyprial org context`` commands: validate, show, status, fetch, import.

The group object and the registration live in :mod:`.commands`; these are the
command callables it registers.  Backend helpers arrive through that module's
``_org_context_documents`` port because the CLI->backend import ledger is
exact per file and those imports must stay where it records them.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import typer

from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.commands.org.context_views import org_human_view, org_meta
from hyprial.shell.impl.cli.output import CliResult, confirm as ask, render_generic, warn


def _org_context_documents() -> Any:
    """The lazily resolved org-context document helpers (see ``commands``)."""

    from hyprial.shell.impl.cli.commands.org import commands as org_commands

    return org_commands._org_context_documents()

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
        documents = _org_context_documents()

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
        result = documents.validate_document(source)
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
            slot_issue = documents.store(
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
                "meta": org_meta(document),
            },
            render=_context_validate_render,
        )

    services.runtime.execute(operation, json_output=json_output, allow_missing_home=True)

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
        documents = _org_context_documents()

        store = documents.store(services._hyprial_home())
        document = store.load_accepted()
        if document is None:
            return CliResult(
                {
                    "ok": True,
                    "status": "absent",
                    "message": "org-context absent",
                },
                render=org_human_view,
            )
        try:
            record = store.acceptance_record()
        except documents.store_error as error:
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
            "meta": org_meta(document),
            "source": {
                "publisher": document.meta.publisher,
            },
            "summary": documents.document_summary(document),
            **(_org_full_document(store, document) if full else {}),
        }
        if record is not None:
            result["adoptedAt"] = record.adopted_at.isoformat().replace("+00:00", "Z")
        if warning is not None:
            result["warnings"] = [warning]
        return CliResult(result, render=org_human_view)

    services.runtime.execute(operation, json_output=json_output)

def _org_full_document(store: Any, document: Any) -> JsonObject:
    """The adopted document as JSON, with the digest of the bytes it came from."""

    accepted = store.accepted_path.read_bytes()
    body = {key: value for key, value in document.data.items() if key != "meta"}
    return {
        "documentSha256": hashlib.sha256(accepted).hexdigest(),
        # YAML dates and other scalars become plain JSON values.
        "document": json.loads(json.dumps(body, ensure_ascii=False, default=str)),
    }

def org_status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show accepted-slot and pending-candidate counts."""
    services = get_services()

    def operation() -> CliResult:
        services = get_services()
        documents = _org_context_documents()

        status = documents.store(services._hyprial_home()).status()
        for warning in status.get("warnings", []):
            warn(f"hyprial: {warning['message']}", json_output=json_output)
        return CliResult({"ok": True, **status}, render=org_human_view)

    services.runtime.execute(operation, json_output=json_output)

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
        documents = _org_context_documents()
        if timeout <= 0 or timeout > 30:
            raise services.CliError(
                documents.ipc_errors.INVALID_ARGUMENT,
                "--timeout must be greater than 0 and at most 30",
            )
        params: JsonObject = {"timeoutSeconds": timeout}
        if source_target is not None:
            params["from"] = source_target
        result = services.runtime.request("org.fetch", params, timeout=timeout + 1.0)
        if not isinstance(result, dict) or not isinstance(
            result.get("candidates"), list
        ):
            raise services.CliError(
                "INVALID_RESPONSE", "daemon org.fetch result must contain candidates"
            )
        return {"ok": True, **result}

    services.runtime.execute(operation, json_output=json_output)

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
        documents = _org_context_documents()

        try:
            document = documents.parse_document(source.read_text(encoding="utf-8"))
        except OSError as error:
            raise services.CliError(
                "ORG_CONTEXT_READ_FAILED", f"cannot read org-context {source}: {error}"
            ) from error
        store = documents.store(services._hyprial_home())
        accepted = store.load_accepted()
        previous = "" if accepted is None else documents.serialize_document(accepted)
        incoming = documents.serialize_document(document)
        diff = "".join(
            difflib.unified_diff(
                previous.splitlines(keepends=True),
                incoming.splitlines(keepends=True),
                fromfile="accepted/org-context.md",
                tofile=f"incoming/{source.name}",
            )
        )
        preview: JsonObject = {
            "meta": org_meta(document),
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
        except (documents.store_error, OSError, ValueError):
            # Adoption is the owner-controlled commit point.  A publication
            # marker failure must not turn that successful local decision into
            # a false import failure.
            pass
        published = False
        orgfs_published = False
        try:
            publish_result = services.runtime.request(
                "org.publish", timeout=2.0, restore_wait=0.0
            )
        except (services.CliError, documents.ipc_errors.TransientDaemonError):
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

    services.runtime.execute(operation, json_output=json_output, ensure_daemon=False)
