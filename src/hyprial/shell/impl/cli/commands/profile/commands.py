"""``hyprial profile`` network profile commands."""

from __future__ import annotations

from collections.abc import Mapping

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # deferred annotations only
    from hyprial.daemon import NetworkProfile

from hyprial.shell.impl.cli.commands.common.services import get_services

from pathlib import Path
import typer

from hyprial.shell.impl.cli.commands.common.support import JsonObject
from hyprial.shell.impl.cli.output import CliResult
profile_app = typer.Typer(
    help=(
        "Inspect, create and select network profiles. One profile is one "
        "HYPRIAL_HOME; the only selection mechanism is HYPRIAL_HOME itself."
    )
)


def _require_profile_name(name: str) -> None:
    """Reject names that cannot be an ``org`` or a path segment under
    ``~/.hyprial/profiles/`` (same rule as the record's ``org`` field, plus the
    two dot-names that would escape the directory)."""
    services = get_services()

    if not name or ":" in name or "/" in name or name in {".", ".."}:
        raise services.CliError(
            "PROFILE_NAME_INVALID",
            f"profile name must be a non-empty string without ':' or '/' "
            f"(and not '.' or '..'); got {name!r}",
        )


def _profile_rows() -> list[JsonObject]:
    """The default home's row plus one row per ``~/.hyprial/profiles/*/`` home."""
    from hyprial.daemon import DEFAULT_PROFILE, PROFILE_FILENAME, read_profile
    services = get_services()

    base = services.default_hyprial_home()[0]
    current, _source = services.configured_hyprial_home()

    def row(name: str, profile: NetworkProfile, home: Path) -> JsonObject:
        return {
            "name": name,
            "issuer": profile.issuer,
            "clientId": profile.client_id,
            "home": str(home),
            "current": home.resolve() == current,
        }

    default_record = base / PROFILE_FILENAME
    if default_record.exists():
        default_profile = read_profile(default_record)
        rows = [row(default_profile.org, default_profile, base)]
    else:
        rows = [row(DEFAULT_PROFILE.org, DEFAULT_PROFILE, base)]
    profiles_root = base / "profiles"
    if profiles_root.is_dir():
        for entry in sorted(profiles_root.iterdir()):
            record = entry / PROFILE_FILENAME
            if not record.is_file():
                continue
            rows.append(row(entry.name, read_profile(record), entry))
    return rows


@profile_app.command("list")
def profile_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """List this machine's network profiles (read-only; no daemon, no state)."""

    def render(data: Mapping[str, Any]) -> str:
        rows = data["profiles"]
        name_width = max([len("name")] + [len(str(item["name"])) for item in rows])
        home_width = max([len("home")] + [len(str(item["home"])) for item in rows])
        lines = [f"  {'name':<{name_width}}  issuer  {'home':<{home_width}}"]
        for item in rows:
            marker = "*" if item["current"] else " "
            lines.append(
                f"{marker} {str(item['name']):<{name_width}}  "
                f"{item['issuer']}  {str(item['home']):<{home_width}}"
            )
        lines.append("(* = current: the home configured_hyprial_home() resolves to)")
        return "\n".join(lines)

    get_services()._execute(
        lambda: CliResult({"ok": True, "profiles": _profile_rows()}, render=render),
        json_output=json_output,
        allow_missing_home=True,
    )


@profile_app.command("show")
def profile_show(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the current home's network profile (read-only; no daemon).

    The whole non-secret record, including ``inviteBaseUrl`` — the https
    host invite links are rendered against (§5.1).
    """
    services = get_services()

    def render(data: Mapping[str, Any]) -> str:
        lines = [f"{key}: {value}" for key, value in data["profile"].items()]
        lines.append(f"source: {data['source']}")
        return "\n".join(lines)

    def operation() -> CliResult:
        services = get_services()
        profile, source = services.resolve_profile()
        return CliResult({"ok": True, "source": source, "profile": profile.as_record()}, render=render)

    services._execute(operation, json_output=json_output, allow_missing_home=True)


@profile_app.command("use")
def profile_use(
    name: str = typer.Argument(..., help="Profile name under ~/.hyprial/profiles/."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Print the export that selects this profile's home (writes nothing).

    D-U1-1: use prints ``export HYPRIAL_HOME=<home>`` for
    ``eval "$(hyprial profile use <name>)"`` and deliberately persists no
    machine-level pointer — the resident daemon's home is pinned by its
    service unit, and a second selector would let the CLI and the daemon
    disagree about which home is active.
    """
    services = get_services()

    def operation() -> JsonObject:
        from hyprial.daemon import PROFILE_FILENAME, read_profile
        services = get_services()
        _require_profile_name(name)
        home = (services.default_hyprial_home()[0] / "profiles" / name).resolve()
        record = home / PROFILE_FILENAME
        if not record.exists():
            raise services.CliError(
                "PROFILE_NOT_FOUND",
                f"no profile named {name!r}: {record} does not exist. Create "
                "it with: hyprial profile create --issuer <url> "
                "--client-id <id>",
                data={"name": name, "path": str(record)},
            )
        return {
            "ok": True,
            "home": str(home),
            "profile": read_profile(record).as_record(),
        }

    # Text mode emits exactly one eval-safe line.
    services._execute(
        lambda: CliResult(operation(), render=lambda data: f"export HYPRIAL_HOME={data['home']}"),
        json_output=json_output,
        allow_missing_home=True,
    )


@profile_app.command("create")
def profile_create(
    name: str = typer.Argument(
        ..., help="Profile name — becomes the record's org and ~/.hyprial/profiles/<name>/."
    ),
    issuer: str = typer.Option(
        ...,
        "--issuer",
        help="Identity issuer URL (https://, no trailing slash; stored verbatim).",
    ),
    client_id: str = typer.Option(
        ...,
        "--client-id",
        help=(
            "Public OIDC client id for this issuer (D-U2-1: public value, not "
            "a secret; hyprial login presents it to the issuer)."
        ),
    ),
    invite_base_url: str | None = typer.Option(
        None,
        "--invite-base-url",
        help=(
            "https base URL of the invite landing page (§5.1; stored as "
            "inviteBaseUrl, validated by NetworkProfile). Omit for the "
            "built-in placeholder."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Create ~/.hyprial/profiles/<name>/ and write its profile.json.

    Idempotent for an identical existing record; refuses a differing one
    (changing a profile in place is U5's switch semantics — there is no
    --force here). Never calls hyprial init, never starts a daemon, never
    writes settings.json.  Since the tailnet cutover the record carries no
    control-plane or join fields: reachability is the Tailcat sidecar's
    business, keyed by the device key login creates.
    """
    services = get_services()

    def operation() -> JsonObject:
        from hyprial.daemon import NetworkProfile, PROFILE_FILENAME, read_profile, validate_profile, write_profile
        services = get_services()
        _require_profile_name(name)
        fields: dict[str, Any] = {
            "org": name,
            "issuer": issuer,
            "client_id": client_id,
        }
        if invite_base_url is not None:
            fields["invite_base_url"] = invite_base_url
        profile = NetworkProfile(**fields)
        home = services.default_hyprial_home()[0] / "profiles" / name
        record = home / PROFILE_FILENAME
        if record.exists():
            existing = read_profile(record)  # loud when the file is broken
            if existing != profile:
                raise services.CliError(
                    "PROFILE_EXISTS",
                    f"profile {name!r} already exists at {record} with "
                    "different values; refusing to overwrite. Changing a "
                    "profile's values is a switch (U5) and there is no "
                    "--force here.",
                    data={"name": name, "path": str(record)},
                )
            return {
                "ok": True,
                "name": name,
                "home": str(home),
                "created": False,
                "profile": profile.as_record(),
            }
        validate_profile(profile, source=record)  # loud before any mkdir
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_profile(profile, hyprial_home=home)
        return {
            "ok": True,
            "name": name,
            "home": str(home),
            "created": True,
            "profile": profile.as_record(),
        }

    services._execute(operation, json_output=json_output, allow_missing_home=True)
