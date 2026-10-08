"""``hyprial user`` commands backed by the daemon identity resolver."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import typer

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.common.services import get_services
from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.output import CliResult, scalar_text
from hyprial.shell.impl.cli.output.tables import render_table


# Design §5 @ 5cbffb0e: exactly five row-level sources, two of them legacy so
# the audit can tell the stores apart.  Account-only rows are local-override
# with outboundOnly:true; "override" is only an accounts[].source value.
_LEGACY_SOURCES = ("legacy-user-bind", "legacy-identities")
_SOURCES = frozenset(
    {"casdoor-login", "org-directory", "local-override", *_LEGACY_SOURCES}
)
_BINDING_COLUMNS = (
    "user",
    "kind",
    "platform",
    "unionId",
    "source",
    "confirmedBy",
    "updatedAt",
    "accounts",
)
_AUDIT_LABELS = {
    "confirmed-elsewhere": "confirmed elsewhere",
    "conflicting": "conflicting",
    "only-legacy": "only-legacy",
    "open-id-only-grandfathered": "open-id-only (grandfathered inbound)",
}
_AUDIT_ORDER = tuple(_AUDIT_LABELS)
_OPEN_ID_HINT = "unknown until this person messages the adapter; see user whois"

user_app = typer.Typer(
    help=(
        "Resolve platform accounts to members or guests through the daemon's "
        "merged identity view. Guests never grant authorization."
    )
)
bindings_app = typer.Typer(help="Inspect binding migration state.")
user_app.add_typer(bindings_app, name="bindings")


def _confirmer(confirmed_by: str) -> str:
    who = confirmed_by.strip()
    if not who:
        raise get_services().CliError(
            ipc_errors.INVALID_ARGUMENT,
            "--confirmed-by must name the person or coordinator confirming this change",
        )
    return who


def _user_adapter(name: str) -> str:
    adapter = name.strip()
    if not adapter or ":" in adapter:
        raise get_services().CliError(
            ipc_errors.INVALID_ARGUMENT,
            "--adapter takes a configured Lark adapter name, not a namespaced value",
        )
    return adapter


def _identity_target(
    *, union_id: str | None, adapter: str | None, open_id: str | None
) -> JsonObject:
    union = union_id.strip() if union_id is not None else None
    adapter_value = adapter.strip() if adapter is not None else None
    open_value = open_id.strip() if open_id is not None else None
    has_union = bool(union)
    has_account = bool(adapter_value) or bool(open_value)
    if has_union == has_account or (has_account and not (adapter_value and open_value)):
        raise get_services().CliError(
            ipc_errors.INVALID_ARGUMENT,
            "choose exactly one identity target: --union-id, or both --adapter and --open-id",
        )
    if has_union:
        return {"platform": "lark", "unionId": union}
    assert adapter_value is not None and open_value is not None
    return {"adapter": _user_adapter(adapter_value), "openId": open_value}


def _mapping(value: object, *, method: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise get_services().CliError(
            "INVALID_RESPONSE", f"{method} result must be an object"
        )
    return dict(value)


def _rows(value: object, *, method: str, keys: Sequence[str]) -> list[JsonObject]:
    candidate = (
        next((value[key] for key in keys if key in value), None)
        if isinstance(value, Mapping)
        else None
    )
    if not isinstance(candidate, list) or not all(
        isinstance(row, Mapping) for row in candidate
    ):
        raise get_services().CliError(
            "INVALID_RESPONSE", f"{method} result must contain object rows"
        )
    return [dict(row) for row in candidate]


def _binding_rows(source: str | None = None) -> list[JsonObject]:
    params: JsonObject = {}
    if source is not None:
        if source not in _SOURCES:
            raise get_services().CliError(
                ipc_errors.INVALID_ARGUMENT,
                "--source must be one of: " + ", ".join(sorted(_SOURCES)),
            )
        params["source"] = source
    result = get_services()._daemon_request("identity.bindings.list", params)
    return _rows(
        result,
        method="identity.bindings.list",
        keys=("bindings",),
    )


def _account_only(row: Mapping[str, Any]) -> bool:
    # Design §5 @ 7db7e5c6 marks account-only rows explicitly; resolve results
    # may carry no unionId key at all.
    if row.get("outboundOnly") is True:
        return True
    accounts = row.get("accounts")
    return (
        "unionId" in row
        and row.get("unionId") in (None, "")
        and isinstance(accounts, list)
        and bool(accounts)
    )


def _render_bindings(data: Mapping[str, Any]) -> str:
    rows = data["rows"]
    cells = []
    for row in rows:
        account_only = _account_only(row)
        cells.append(
            [
                (
                    ""
                    if account_only and column == "unionId"
                    else (
                        f"{scalar_text(row.get(column))}; account-only (outbound only)"
                        if account_only and column == "accounts"
                        else scalar_text(row.get(column))
                    )
                )
                for column in _BINDING_COLUMNS
            ]
        )
    return render_table(None, list(_BINDING_COLUMNS), cells)


def _render_whois(data: Mapping[str, Any]) -> str:
    account_only = _account_only(data)
    lines = [
        "user: " + scalar_text(data.get("user")),
        f"kind: {scalar_text(data.get('kind'))}",
        f"source: {scalar_text(data.get('source'))}",
        f"verified: {'false' if account_only else scalar_text(data.get('verified'))}",
    ]
    if account_only:
        lines.extend(("unionId: ", "identity: account-only (outbound only)"))
    if data.get("confirmedBy") is not None:
        lines.append(f"confirmedBy: {scalar_text(data.get('confirmedBy'))}")
    if data.get("kind") == "guest" or (
        "owner" in data and data.get("owner") is None
    ):
        lines.extend(("owner: null", "authorization: a guest never authorizes"))
    return "\n".join(lines)


def _render_mutation(data: Mapping[str, Any]) -> str:
    if "cleared" in data:
        return "cleared" if data["cleared"] else "no binding cleared"
    rendered = (
        f"bound user {scalar_text(data.get('user'))} "
        f"from {scalar_text(data.get('source'))}"
    )
    if _account_only(data):
        rendered += "; account-only (outbound only)"
    return rendered


@user_app.command("list")
def user_list(
    source: str | None = typer.Option(
        None, "--source", help="Filter by one frozen binding source."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List the merged binding view; JSON output uses {"rows": [...]}."""

    get_services()._execute(
        lambda: CliResult({"rows": _binding_rows(source)}, render=_render_bindings),
        json_output=json_output,
    )


@user_app.command("bind")
def user_bind(
    user: str = typer.Argument(..., help="Local user key."),
    union_id: str | None = typer.Option(
        None, "--union-id", help="Feishu cross-App union_id."
    ),
    adapter: str | None = typer.Option(
        None, "--adapter", help="Configured Lark adapter name."
    ),
    open_id: str | None = typer.Option(
        None, "--open-id", help="App-scoped open_id observed by that adapter."
    ),
    confirmed_by: str = typer.Option(
        ..., "--confirmed-by", help="Person or coordinator confirming the override."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Set a local override by union_id or one observed adapter account."""

    def operation() -> CliResult:
        params = {
            **_identity_target(
                union_id=union_id, adapter=adapter, open_id=open_id
            ),
            "user": user,
            "confirmedBy": _confirmer(confirmed_by),
        }
        result = get_services()._daemon_request("identity.override.set", params)
        return CliResult(
            _mapping(result, method="identity.override.set"), render=_render_mutation
        )

    get_services()._execute(operation, json_output=json_output)


@user_app.command("unbind")
def user_unbind(
    union_id: str | None = typer.Option(
        None, "--union-id", help="Feishu cross-App union_id."
    ),
    adapter: str | None = typer.Option(
        None, "--adapter", help="Configured Lark adapter name."
    ),
    open_id: str | None = typer.Option(
        None, "--open-id", help="App-scoped open_id observed by that adapter."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Clear a local override; this never changes directory or legacy rows."""

    def operation() -> CliResult:
        params = _identity_target(
            union_id=union_id, adapter=adapter, open_id=open_id
        )
        result = get_services()._daemon_request("identity.override.clear", params)
        return CliResult(
            _mapping(result, method="identity.override.clear"), render=_render_mutation
        )

    get_services()._execute(operation, json_output=json_output)


@user_app.command("whois")
def user_whois(
    adapter: str | None = typer.Argument(
        None, help="Configured Lark adapter name; omit with --union-id."
    ),
    open_id: str | None = typer.Argument(
        None, help="App-scoped open_id; omit with --union-id."
    ),
    union_id: str | None = typer.Option(
        None, "--union-id", help="Resolve one Feishu cross-App union_id."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Resolve one platform identity without guessing through conflicts."""

    def operation() -> CliResult:
        params = _identity_target(
            union_id=union_id, adapter=adapter, open_id=open_id
        )
        # Preserve the resolver's conflict detail. A conflict remains an error,
        # so neither JSON nor human output can accidentally select a user.
        result = get_services()._daemon_request("identity.resolve", params)
        return CliResult(
            _mapping(result, method="identity.resolve"), render=_render_whois
        )

    get_services()._execute(operation, json_output=json_output)


@user_app.command("add")
def user_add(
    kind: str = typer.Option(
        ..., "--kind", help="member (has a hyprial owner) or guest."
    ),
    confirmed_by: str = typer.Option(
        ..., "--confirmed-by", help="Person or coordinator confirming the user."
    ),
    owner: str | None = typer.Option(
        None, "--owner", help="The member's hyprial owner; forbidden for guests."
    ),
    nickname: str | None = typer.Option(None, "--nickname", help="Nickname."),
    real_name: str | None = typer.Option(None, "--real-name", help="Real name."),
    display_name: str | None = typer.Option(
        None, "--display-name", help="Name shown to agents."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Add one local member or guest; the daemon generates the user key."""

    def operation() -> CliResult:
        # Design §5 (round 4): members are keyed by slug(owner), guests by a
        # random key, so the caller names the kind's anchor, never the key.
        if kind == "member" and not owner:
            raise get_services().CliError(
                ipc_errors.INVALID_ARGUMENT, "a member needs --owner"
            )
        if kind == "guest" and not display_name:
            raise get_services().CliError(
                ipc_errors.INVALID_ARGUMENT, "a guest needs --display-name"
            )
        params: JsonObject = {
            "kind": kind,
            "confirmedBy": _confirmer(confirmed_by),
        }
        for name, value in (
            ("owner", owner),
            ("nickname", nickname),
            ("realName", real_name),
            ("displayName", display_name),
        ):
            if value is not None:
                params[name] = value
        result = _mapping(
            get_services()._daemon_request("identity.users.add", params),
            method="identity.users.add",
        )
        record = result.get("user")
        if not isinstance(record, Mapping):
            raise get_services().CliError(
                "INVALID_RESPONSE", "identity.users.add returned no user record"
            )
        return CliResult(
            {"user": dict(record)},
            render=lambda data: (
                f"added {scalar_text(data['user'].get('kind'))} "
                f"{scalar_text(data['user'].get('userKey') or data['user'].get('user'))}"
            ),
        )

    get_services()._execute(operation, json_output=json_output)


@user_app.command("show")
def user_show(
    user: str = typer.Argument(..., help="Local user key."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show one local member or guest through the daemon authority."""

    def operation() -> JsonObject:
        result = get_services()._daemon_request(
            "identity.users.list", {"user": user}
        )
        rows = _rows(
            result, method="identity.users.list", keys=("users",)
        )
        matches = [
            row
            for row in rows
            if row.get("userKey", row.get("user")) == user
        ]
        if not matches:
            raise get_services().CliError("USER_NOT_FOUND", "user not found")
        return matches[0]

    get_services()._execute(operation, json_output=json_output)


def _account_keys(row: Mapping[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    accounts = row.get("accounts")
    if isinstance(accounts, list):
        for account in accounts:
            if not isinstance(account, Mapping):
                continue
            adapter = account.get("adapter")
            open_id = account.get("openId")
            if isinstance(adapter, str) and isinstance(open_id, str):
                keys.add((adapter, open_id))
    return keys


def _binding_keys(row: Mapping[str, Any]) -> set[tuple[str, str]]:
    keys = _account_keys(row)
    union_id = row.get("unionId")
    if isinstance(union_id, str) and union_id:
        keys.add(("unionId", union_id))
    return keys


def _has_union_id(row: Mapping[str, Any]) -> bool:
    union_id = row.get("unionId")
    return isinstance(union_id, str) and bool(union_id)


def _is_open_id_only(row: Mapping[str, Any]) -> bool:
    return not _has_union_id(row) and bool(_account_keys(row))


def _audit_category(
    legacy: Mapping[str, Any], merged: Sequence[Mapping[str, Any]]
) -> str:
    keys = _binding_keys(legacy)
    matches = [row for row in merged if keys & _binding_keys(row)]
    if any(row.get("user") != legacy.get("user") for row in matches):
        return "conflicting"
    if matches:
        return "confirmed-elsewhere"
    if _is_open_id_only(legacy):
        return "open-id-only-grandfathered"
    return "only-legacy"


def _observed_union_ids(
    legacy: Mapping[str, Any], merged: Sequence[Mapping[str, Any]]
) -> set[str]:
    account_keys = _account_keys(legacy)
    observed: set[str] = set()
    for row in merged:
        if not account_keys & _account_keys(row):
            continue
        union_id = row.get("unionId")
        if isinstance(union_id, str) and union_id:
            observed.add(union_id)
    return observed


def _suggested_command(
    legacy: Mapping[str, Any],
    category: str,
    merged: Sequence[Mapping[str, Any]],
) -> tuple[str | None, str | None]:
    if category not in {"only-legacy", "open-id-only-grandfathered"}:
        return None, None
    union_id = legacy.get("unionId") if category == "only-legacy" else None
    source: str | None = None
    if category == "open-id-only-grandfathered":
        observed = _observed_union_ids(legacy, merged)
        if len(observed) == 1:
            union_id = next(iter(observed))
            source = "observed"
    rendered_union = union_id if isinstance(union_id, str) and union_id else "<UNION_ID>"
    confirmed_by = legacy.get("confirmedBy")
    rendered_confirmer = (
        confirmed_by.strip()
        if isinstance(confirmed_by, str) and confirmed_by.strip()
        else "<WHO>"
    )
    command = (
        f"hyprial user bind {legacy.get('user')} --union-id {rendered_union} "
        f"--confirmed-by {rendered_confirmer}"
    )
    return command, source


def _render_audit(data: Mapping[str, Any]) -> str:
    rows = data["rows"]
    counts = {category: 0 for category in _AUDIT_ORDER}
    for row in rows:
        category = row.get("category")
        if category in counts:
            counts[category] += 1
    summary = "Audit summary: " + "; ".join(
        f"{_AUDIT_LABELS[category]}: {counts[category]}"
        for category in _AUDIT_ORDER
    )
    headers = [*_BINDING_COLUMNS, "audit", "suggestedCommand"]
    cells = []
    suggestions = []
    for row in rows:
        suggestion = row.get("suggestedCommand")
        if suggestion and row.get("category") == "open-id-only-grandfathered":
            if row.get("suggestedUnionIdSource") == "observed":
                suggestion = f"{suggestion} (observed, confirm before binding)"
            else:
                suggestion = f"{suggestion} ({_OPEN_ID_HINT})"
        if suggestion:
            suggestions.append(f"{row.get('user')}: {suggestion}")
        cells.append(
            [
                *[scalar_text(row.get(column)) for column in _BINDING_COLUMNS],
                scalar_text(row.get("audit")),
                scalar_text(suggestion),
            ]
        )
    rendered = summary + "\n" + render_table(None, headers, cells, truncate_column=7)
    if suggestions:
        rendered += "\n" + "\n".join(suggestions)
    return rendered


@bindings_app.command("audit")
def user_bindings_audit(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Classify legacy rows against the merged view without writing anything."""

    def operation() -> CliResult:
        legacy = [row for source in _LEGACY_SOURCES for row in _binding_rows(source)]
        merged = [
            row for row in _binding_rows() if row.get("source") not in _LEGACY_SOURCES
        ]
        rows = []
        for row in legacy:
            category = _audit_category(row, merged)
            command, union_source = _suggested_command(row, category, merged)
            audit_row = {
                **row,
                "audit": _AUDIT_LABELS[category],
                "category": category,
                "suggestedCommand": command,
            }
            if union_source is not None:
                audit_row["suggestedUnionIdSource"] = union_source
            rows.append(audit_row)
        return CliResult({"rows": rows}, render=_render_audit)

    get_services()._execute(operation, json_output=json_output)
