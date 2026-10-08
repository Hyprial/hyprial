"""``hyprial fs`` organization folder commands."""

from __future__ import annotations

from hyprial.shell.impl.cli.commands.common.services import get_services

from collections.abc import Mapping
from typing import Any
from pathlib import Path
from hyprial.kernel import ipc_errors
import json
import os
from hyprial.kernel import parse_orgfs_uri
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject, _local_operator_identity, _message_claim_params
from hyprial.shell.impl.cli.output import CliResult, render_generic
fs_app = typer.Typer(help="Share and synchronize organization folders.")


def _fs_run(method: str, params: JsonObject, *, json_output: bool) -> None:
    services = get_services()

    def render(data: Mapping[str, Any]) -> str:
        # Human output is the one surface that derives the per-tailnet web
        # link (design docs/notes/orgfs-uri/design.md §7a): the wire form carries
        # the host-free URI alone.
        from hyprial.daemon import with_human_web_urls

        home = get_services().require_initialized_hyprial_home()
        return render_generic(with_human_web_urls(dict(data), home))

    def operation() -> CliResult:
        return CliResult(get_services()._daemon_request(method, params), render=render)

    services._execute(operation, json_output=json_output)


@fs_app.command("spaces")
def fs_spaces(json_output: bool = typer.Option(False, "--json", help="Emit JSON only.")) -> None:
    """List spaces joined on this node."""

    _fs_run("orgfs.spaces", {}, json_output=json_output)


@fs_app.command("create")
def fs_create(
    name: str = typer.Argument(..., help="Space display name."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Create a space owned by the current user."""

    _fs_run("orgfs.create", {"name": name}, json_output=json_output)


@fs_app.command("ls")
def fs_ls(
    space_id: str = typer.Argument(..., help="Space UUID."),
    path: str = typer.Argument("", help="Directory path, id:<nodeId>, or orgfs: URI."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List one directory."""

    _fs_run("orgfs.ls", {"spaceId": space_id, "path": path}, json_output=json_output)


@fs_app.command("resolve")
def fs_resolve(
    target: str = typer.Argument(..., help="Space UUID, or a full orgfs:<owner>:<spaceId>:<nodeId> URI."),
    path: str | None = typer.Argument(None, help="Path, id:<nodeId>, or orgfs: URI to resolve; omit when target is an orgfs: URI."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Resolve a path without rejecting same-name ambiguity."""

    parsed = parse_orgfs_uri(target)
    if parsed is not None:
        if path is not None:
            raise typer.BadParameter(
                "path must be omitted when target is an orgfs: URI"
            )
        # The daemon re-checks the owner/spaceId binding in _resolve_ids, so
        # client and server can never disagree; the CLI uses the one reader
        # (parse_orgfs_uri), never manual splitting.
        _fs_run(
            "orgfs.resolve",
            {"spaceId": parsed[1], "path": target},
            json_output=json_output,
        )
        return
    if path is None:
        raise typer.BadParameter("path is required when target is a space UUID")
    _fs_run("orgfs.resolve", {"spaceId": target, "path": path}, json_output=json_output)


@fs_app.command("read")
def fs_read(
    space_id: str = typer.Argument(..., help="Space UUID."),
    path: str = typer.Argument(..., help="Path, id:<nodeId>, or orgfs: URI."),
    wait: float = typer.Option(10.0, "--wait", min=0.0, max=120.0, help="Seconds to wait for verified content."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Read a text or small binary node."""

    _fs_run(
        "orgfs.read",
        {"spaceId": space_id, "path": path, "waitSeconds": wait},
        json_output=json_output,
    )


@fs_app.command("write")
def fs_write(
    space_id: str = typer.Argument(..., help="Space UUID."),
    path: str = typer.Argument(..., help="Path, id:<nodeId>, or orgfs: URI."),
    text: str = typer.Argument(..., help="Complete desired text."),
    base_version: str | None = typer.Option(None, "--base-version", help="Version used as the merge baseline."),
    expect_version: str | None = typer.Option(None, "--expect-version", help="Reject unless this is the current version."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Write text, optionally against a merge or strict baseline."""

    params: JsonObject = {"spaceId": space_id, "path": path, "text": text}
    if base_version is not None:
        params["baseVersion"] = base_version
    if expect_version is not None:
        params["expectVersion"] = expect_version
    _fs_run("orgfs.write", params, json_output=json_output)


@fs_app.command("mkdir")
def fs_mkdir(
    space_id: str = typer.Argument(..., help="Space UUID."),
    path: str = typer.Argument(..., help="Directory path to create."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Create a directory."""
    _fs_run("orgfs.mkdir", {"spaceId": space_id, "path": path}, json_output=json_output)


@fs_app.command("mv")
def fs_mv(
    space_id: str = typer.Argument(..., help="Space UUID."),
    source: str = typer.Argument(..., help="Source path, id:<nodeId>, or orgfs: URI."),
    destination: str = typer.Argument(..., help="Destination path, directory id:<nodeId>, or orgfs: URI."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Move or rename a node."""
    _fs_run(
        "orgfs.move",
        {"spaceId": space_id, "sourcePath": source, "destinationPath": destination},
        json_output=json_output,
    )


@fs_app.command("rm")
def fs_rm(
    space_id: str = typer.Argument(..., help="Space UUID."),
    path: str = typer.Argument(..., help="Path, id:<nodeId>, or orgfs: URI."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Soft-delete a node."""
    _fs_run("orgfs.remove", {"spaceId": space_id, "path": path}, json_output=json_output)


@fs_app.command("history")
def fs_history(
    space_id: str = typer.Argument(..., help="Space UUID."),
    node: str = typer.Argument(..., help="Path, id:<nodeId>, or orgfs: URI."),
    limit: int = typer.Option(50, "--limit", help="Maximum history entries."),
    before: str | None = typer.Option(None, "--before", help="Return entries before this version."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show attributed node history."""
    params: JsonObject = {"spaceId": space_id, "node": node, "limit": limit}
    if before is not None:
        params["before"] = before
    _fs_run("orgfs.history", params, json_output=json_output)


@fs_app.command("watch")
def fs_watch(
    space_id: str = typer.Argument(..., help="Space UUID."),
    glob: str = typer.Option("*", "--glob", help="Path glob to match."),
    since_version: str | None = typer.Option(None, "--since-version", help="Poll changes after this version."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Poll a batch of matching changes."""
    params: JsonObject = {"spaceId": space_id, "glob": glob}
    if since_version is not None:
        params["sinceVersion"] = since_version
    _fs_run("orgfs.watch", params, json_output=json_output)


def _local_file_caller() -> JsonObject:
    """Who asks the daemon to touch a local file (#1169 path rule).

    Inside a worker the worker's own claim, so its workspace and cwd apply;
    at the terminal the local operator.
    """

    worker = os.environ.get("HYPRIAL_WORKER_ACTOR")
    return _message_claim_params(worker) if worker else {"from": _local_operator_identity()}


@fs_app.command("import")
def fs_import(
    space_id: str = typer.Argument(..., help="Space UUID."),
    path: str = typer.Argument(..., help="Destination path, id:<nodeId>, or orgfs: URI."),
    source: Path = typer.Argument(..., exists=True, dir_okay=False, help="Local file to import."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Import a local file as immutable blob content."""
    _fs_run(
        "orgfs.import",
        {
            **_local_file_caller(),
            "spaceId": space_id,
            "path": path,
            "source": str(source.resolve()),
        },
        json_output=json_output,
    )


@fs_app.command("export")
def fs_export(
    space_id: str = typer.Argument(..., help="Space UUID."),
    node: str = typer.Argument(..., help="Path, id:<nodeId>, or orgfs: URI."),
    destination: Path = typer.Argument(..., help="Local destination file."),
    wait: float = typer.Option(10.0, "--wait", min=0.0, max=120.0, help="Seconds to wait for verified content."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing destination file."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Export a node to a local file inside your workspace or working directory."""
    _fs_run(
        "orgfs.export",
        {
            **_local_file_caller(),
            "spaceId": space_id,
            "node": node,
            "destination": str(destination.resolve()),
            "waitSeconds": wait,
            "overwrite": overwrite,
        },
        json_output=json_output,
    )


@fs_app.command("invite")
def fs_invite(
    space_id: str = typer.Argument(..., help="Space UUID."),
    user: str = typer.Argument(..., help="Invited user URI."),
    mode: str = typer.Option("rw", "--mode", help="Member mode: ro or rw."),
    no_notify: bool = typer.Option(False, "--no-notify", help="Update membership without sending an inbox invitation."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Add a member and, by default, send the frozen inbox invitation."""
    services = get_services()

    def operation() -> Any:
        services = get_services()
        member = services._daemon_request(
            "orgfs.invite", {"spaceId": space_id, "user": user, "mode": mode}
        )
        notified = False
        if not no_notify:
            spaces = services._daemon_request("orgfs.spaces", {})
            name = next(
                (
                    item.get("name")
                    for item in spaces.get("spaces", [])
                    if isinstance(item, dict) and item.get("space_id", item.get("spaceId")) == space_id
                ),
                space_id,
            )
            owner = member.get("added_by", member.get("addedBy", "")) if isinstance(member, dict) else ""
            payload = json.dumps(
                {
                    "schemaVersion": 1,
                    "type": "orgfs-invite",
                    "spaceId": space_id,
                    "name": name,
                    "owner": owner,
                    "mode": mode,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            services._daemon_request(
                "message.send",
                {
                    **(
                        _message_claim_params(os.environ["HYPRIAL_WORKER_ACTOR"])
                        if os.environ.get("HYPRIAL_WORKER_ACTOR")
                        else {"from": _local_operator_identity()}
                    ),
                    "to": [user],
                    "message": payload,
                },
            )
            notified = True
        return {"member": member, "notified": notified}

    services._execute(operation, json_output=json_output)


@fs_app.command("remove-member")
def fs_remove_member(
    space_id: str = typer.Argument(..., help="Space UUID."),
    user: str = typer.Argument(..., help="Member user URI."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Remove a member from future writes."""
    _fs_run("orgfs.remove_member", {"spaceId": space_id, "user": user}, json_output=json_output)


@fs_app.command("members")
def fs_members(
    space_id: str = typer.Argument(..., help="Space UUID."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List current space members."""
    _fs_run("orgfs.members", {"spaceId": space_id}, json_output=json_output)


@fs_app.command("join")
def fs_join(
    space_id: str = typer.Argument(..., help="Invited space UUID."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Clone and join a space from an online holder."""
    _fs_run("orgfs.join", {"spaceId": space_id}, json_output=json_output)


@fs_app.command("web")
def fs_web(
    listen: str = typer.Option(
        ...,
        "--listen",
        help=(
            "host:port (loopback or an explicit non-wildcard address; "
            "wildcards are refused) or unix:/absolute/path."
        ),
    ),
    host: str = typer.Option(
        ..., "--host", help="The host name this service answers as (never guessed)."
    ),
    cert: Path = typer.Option(..., "--cert", help="TLS certificate chain (PEM)."),
    key: Path = typer.Option(..., "--key", help="TLS private key (PEM)."),
    client_address: str = typer.Option(
        "proxy-v2",
        "--client-address",
        help=(
            "socket: the TCP peer is the client (tailscale-container front). "
            "proxy-v2: require a PROXY v2 header (forwarding sidecar front; "
            "pair with a unix listener — loopback TCP is spoofable by "
            "same-OS-user processes)."
        ),
    ),
    daemon_socket: Path | None = typer.Option(
        None,
        "--daemon-socket",
        help=(
            "proxy-v2 mode only: the daemon socket whose org directory "
            "resolves the TLV 0xE0 nodekey to an owner. Defaults to this "
            "environment's daemon socket."
        ),
    ),
    retry_after: int = typer.Option(
        5, "--retry-after", help="Retry-After seconds on 503 content-pending."
    ),
    check: bool = typer.Option(
        False, "--check", help="Verify listen address, cert/key readability and expiry, then exit."
    ),
    json_output: bool = typer.Option(False, "--json", help="With --check: emit JSON only."),
) -> None:
    """Run the read-only orgfs HTTPS web service (docs/notes/orgfs-web/brief.md)."""

    from hyprial.daemon import (
        WebServiceConfig,
        check_config,
        serve,
    )

    config = WebServiceConfig(
        host=host,
        listen=listen,
        cert=cert,
        key=key,
        client_address=client_address,
        retry_after_seconds=retry_after,
        daemon_socket=daemon_socket,
    )
    if check:
        def render(data: Mapping[str, Any]) -> str:
            if not data["problems"]:
                return "ok: listen address, certificate and key verified"
            return "\n".join(f"not ok: {problem}" for problem in data["problems"])

        def operation() -> CliResult:
            problems = [str(problem) for problem in check_config(config)]
            return CliResult({"ok": not problems, "problems": problems}, render=render)

        get_services()._execute(operation, json_output=json_output, allow_missing_home=True)
        return
    # The service runs in the foreground until stopped; its exit status is the
    # server's, and its log is not a command result (output discipline allowlist).
    # proxy-v2 resolves the TLV 0xE0 nodekey through the daemon's org
    # directory (serve wires the IPC-backed resolver); socket mode has no
    # resolver here and fails closed, as before.
    raise typer.Exit(serve(config, resolver=None))


@fs_app.command("trash")
def fs_trash(
    space_id: str = typer.Argument(..., help="Space UUID."),
    limit: int = typer.Option(100, "--limit", help="Maximum deleted nodes."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List deleted nodes that can still be restored."""
    _fs_run("orgfs.trash", {"spaceId": space_id, "limit": limit}, json_output=json_output)


@fs_app.command("restore")
def fs_restore(
    space_id: str = typer.Argument(..., help="Space UUID."),
    node: str = typer.Argument(..., help="Path, id:<nodeId>, or orgfs: URI."),
    version: str = typer.Argument(..., help="Version to restore."),
    recursive: bool = typer.Option(True, "--recursive/--no-recursive", help="Restore directory descendants too."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Restore a node as a new operation."""
    _fs_run(
        "orgfs.restore",
        {"spaceId": space_id, "node": node, "version": version, "recursive": recursive},
        json_output=json_output,
    )


@fs_app.command("status")
def fs_status(
    space_id: str = typer.Argument(..., help="Space UUID."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show online holders and unconfirmed local commits."""
    _fs_run("orgfs.status", {"spaceId": space_id}, json_output=json_output)


@fs_app.command("serve")
def fs_serve(
    space_id: str = typer.Argument(..., help="Space UUID."),
    backend: str = typer.Option("fs", "--backend", help="Replica backend: fs or memory."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Serve a space as a resident replica."""
    _fs_run(
        "orgfs.serve",
        {"spaceId": space_id, "backend": backend},
        json_output=json_output,
    )


@fs_app.command("checkout")
def fs_checkout(
    space_id: str = typer.Argument(..., help="Space UUID."),
    enabled: bool = typer.Option(
        True,
        "--enable/--disable",
        help="Enable or remove the read-only materialized checkout.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Manage the read-only checkout projection."""
    _fs_run(
        "orgfs.checkout",
        {"spaceId": space_id, "enabled": enabled},
        json_output=json_output,
    )


@fs_app.command("purge-plan")
def fs_purge_plan(
    space_id: str = typer.Argument(..., help="Space UUID."),
    targets_json: str = typer.Argument(
        ..., help="JSON array of exact blob/doc/tree-range targets."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Build an owner-reviewed purge plan bound to exact targets."""
    services = get_services()
    try:
        targets = json.loads(targets_json)
    except json.JSONDecodeError as exc:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT, "targets must be valid JSON"
        ) from exc
    if not isinstance(targets, list) or not all(
        isinstance(target, dict) for target in targets
    ):
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT, "targets must be a JSON array of objects"
        )
    _fs_run(
        "orgfs.purge_plan",
        {"spaceId": space_id, "targets": targets},
        json_output=json_output,
    )


@fs_app.command("purge")
def fs_purge(
    space_id: str = typer.Argument(..., help="Space UUID."),
    plan_id: str = typer.Argument(..., help="Exact reviewed purge plan ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Execute an unchanged owner-reviewed purge plan."""
    _fs_run(
        "orgfs.purge",
        {"spaceId": space_id, "planId": plan_id},
        json_output=json_output,
    )


@fs_app.command("purge-status")
def fs_purge_status(
    space_id: str = typer.Argument(..., help="Space UUID."),
    plan_id: str = typer.Argument(..., help="Purge plan ID."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show acknowledged and pending purge participants."""
    _fs_run(
        "orgfs.purge_status",
        {"spaceId": space_id, "planId": plan_id},
        json_output=json_output,
    )


@fs_app.command("unban")
def fs_unban(
    space_id: str = typer.Argument(..., help="Space UUID."),
    sha: str = typer.Argument(..., help="Lowercase SHA-256 digest."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Explicitly allow the same content hash to be written again."""
    _fs_run(
        "orgfs.unban",
        {"spaceId": space_id, "sha": sha},
        json_output=json_output,
    )
