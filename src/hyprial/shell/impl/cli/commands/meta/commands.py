"""``hyprial help`` and ``hyprial version``."""

from __future__ import annotations

from hyprial.shell.impl.cli.output import CliResult

from hyprial.shell.impl.cli.commands.common.services import get_services

import re
from hyprial.shell.impl.cli.inventory import render_top_level_help
import typer

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.shell.impl.cli.commands.common.support import JsonObject
def _semantic_version(value: str) -> str:
    """Translate the installed PEP 440 development version to SemVer."""

    translated = re.sub(r"\.dev(\d+)$", r"-dev.\1", value)
    translated = re.sub(r"(a|b|rc)(\d+)$", r"-\1.\2", translated)
    if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", translated):
        return translated
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", value)
    return ".".join(match.groups()) if match else "0.0.0-dev.0"


def _version_result() -> JsonObject:
    """Local-only identity fields; remote tag probing lives in upgrade."""
    services = get_services()

    from hyprial import __version__
    from hyprial.daemon import updates

    installation = updates.read_installation()
    installed = installation.version or __version__
    local = _semantic_version(installed)
    update_source = updates.installation_git_url(installation)
    result: JsonObject = {
        "ok": True,
        "packageVersion": installed,
        "localVersion": local,
        "registry": update_source,
        "persistentUpdateSource": update_source,
        "installOrigin": updates.installation_origin(installation),
        "dependencyLock": updates.dependency_lock_report(
            services._hyprial_home(), installation
        ),
    }
    warning = updates.retired_track_warning(services._hyprial_home())
    if warning is not None:
        result["warning"] = warning
    return result


@app.command("help")
def help_command(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the top-level command inventory registered with Typer."""
    get_services()._execute(
        lambda: CliResult(
            {"ok": True, "usage": render_top_level_help(app)}, render=lambda data: data["usage"]
        ),
        json_output=json_output,
    )


@app.command()
def version(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show the installed version and update source."""
    services = get_services()

    services._execute(
        _version_result, json_output=json_output, allow_missing_home=True
    )
