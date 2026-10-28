"""Resolve the host CLI login files that seed an agent's native credential.

The command itself is intentionally thin: it resolves the documented source
files, refuses anything that is not a plain non-empty file, and hands the bytes
to the daemon.  Reading happens in this process, so the daemon never gains the
ability to read arbitrary operator paths.

Sources are the harnesses' own documented locations, not guesses:

- claude  ``~/.claude/.credentials.json``
- codex   ``~/.codex/auth.json``
- pi      ``~/.pi/agent/auth.json`` (and its ``models-store.json``; the
          2026-10-13 autodl failure was an empty pair).  When
          ``PI_CODING_AGENT_DIR`` names pi's own root, the two files are read
          from there instead.

A provider may own more than one file; ``--source`` is therefore only accepted
for single-file providers, where naming one path is unambiguous.
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path
from typing import Any

import typer

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.output import warn
from hyprial.shell.impl.cli.commands.common.services import get_services

#: provider -> the files the harness reads, relative to the operator's HOME.
PROVIDER_SOURCE_FILES: dict[str, tuple[str, ...]] = {
    "claude": (".claude/.credentials.json",),
    "codex": (".codex/auth.json",),
    "pi": (".pi/agent/auth.json", ".pi/agent/models-store.json"),
}


class ProviderSourceError(Exception):
    """A refused source; ``code`` is the CLI-facing code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_provider_sources(
    provider: str,
    *,
    home: Path,
    source: Path | None = None,
    pi_agent_dir: Path | None = None,
) -> tuple[Path, ...]:
    """Return the source files to seed for ``provider``, or refuse.

    Refusals are explicit: an unknown provider, a ``--source`` on a multi-file
    provider, a missing path, a symlink, a non-regular file, or an empty file.
    Nothing is written here.

    ``pi_agent_dir`` is the directory ``PI_CODING_AGENT_DIR`` names, where pi
    keeps ``auth.json`` and ``models-store.json`` directly; it replaces the
    ``~/.pi/agent`` base for pi only.  The caller reads the environment so this
    function stays a pure path resolver.
    """

    relative = PROVIDER_SOURCE_FILES.get(provider)
    if relative is None:
        known = ", ".join(sorted(PROVIDER_SOURCE_FILES))
        raise ProviderSourceError(
            ipc_errors.PROVIDER_UNSUPPORTED,
            f"provider {provider!r} is not supported; supported: {known}",
        )

    if source is not None:
        if len(relative) > 1:
            raise ProviderSourceError(
                ipc_errors.PROVIDER_SOURCE_AMBIGUOUS,
                f"provider {provider!r} reads {len(relative)} files "
                f"({', '.join(relative)}); --source names one file",
            )
        candidates: tuple[Path, ...] = (Path(source),)
    elif provider == "pi" and pi_agent_dir is not None:
        candidates = tuple(
            Path(pi_agent_dir) / Path(item).name for item in relative
        )
    else:
        candidates = tuple(Path(home) / item for item in relative)

    resolved: list[Path] = []
    for candidate in candidates:
        _require_readable_regular_file(candidate, provider)
        resolved.append(candidate)
    return tuple(resolved)


def _require_readable_regular_file(path: Path, provider: str) -> None:
    if path.is_symlink():
        raise ProviderSourceError(
            ipc_errors.PROVIDER_UNSAFE_PATH,
            f"{path} is a symlink; name the real file",
        )
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ProviderSourceError(
            ipc_errors.PROVIDER_SOURCE_MISSING,
            f"{provider} login not found at {path}: {error.strerror or error}",
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ProviderSourceError(
            ipc_errors.PROVIDER_UNSAFE_PATH,
            f"{path} is not a regular file",
        )
    if metadata.st_size == 0:
        raise ProviderSourceError(
            ipc_errors.PROVIDER_SOURCE_MISSING,
            f"{path} is empty; log in with the {provider} CLI first",
        )


#: pi relocates its own agent root when this is set (design source table).
PI_AGENT_DIR_ENV = "PI_CODING_AGENT_DIR"

#: The harness exports its session's canonical actor URI under this name.
_WORKER_ACTOR_ENV = "HYPRIAL_WORKER_ACTOR"


provider_app = typer.Typer(
    help="Seed an agent's native credential from this host's own CLI login."
)


def _pi_agent_dir(provider: str) -> Path | None:
    """Read pi's documented root override; empty means the default base."""

    if provider != "pi":
        return None
    configured = os.environ.get(PI_AGENT_DIR_ENV, "").strip()
    return Path(configured) if configured else None


def _refuse_in_worker_shell(services: Any) -> None:
    if _addressed_actor_or_none() is not None:
        raise services.CliError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "an agent shell may not change the machine's provider default; "
            "run this from the operator's own shell",
        )


def _addressed_actor_or_none() -> str | None:
    return os.environ.get(_WORKER_ACTOR_ENV, "").strip() or None


def _addressed_actor(services: Any) -> str:
    """Default the target to the actor this shell is already running as."""

    actor = os.environ.get(_WORKER_ACTOR_ENV, "").strip()
    if not actor:
        raise services.CliError(
            ipc_errors.INVALID_ARGUMENT,
            "no agent is addressed by this shell; pass --actor <NAME>",
        )
    return actor


@provider_app.command("login")
def agent_provider_login(
    provider: str = typer.Argument(
        ..., help="Harness: claude, codex, or pi (the native login files are per harness)."
    ),
    actor: str | None = typer.Option(
        None,
        "--actor",
        help=(
            "Agent to seed. Defaults to the agent this shell is already "
            "running as; an operator shell must pass --actor."
        ),
    ),
    source: Path | None = typer.Option(
        None,
        "--source",
        help=(
            "Read a single-file provider's login from this path; refused for "
            "pi, which reads two files."
        ),
    ),
    force: bool = typer.Option(
        False, "--force", help="Replace an existing credential."
    ),
    make_default: bool = typer.Option(
        False,
        "--default",
        help=(
            "Also record this provider as the machine default that future "
            "'hyprial agent create' runs seed from. Existing agents are not "
            "touched. Without --actor only the default is recorded."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Seed an agent's native credential from this host's own provider login.

    The credential belongs to the user this daemon is logged in as: whatever
    that user's own vendor CLI login holds is copied into the agent's isolated
    home, and the vendor bills and audits it as that user. API keys stay the
    default (hyprial agent secret grant); this OAuth seed is the explicit
    alternative.

    One-shot, not a refresh loop: when the seeded login expires or breaks, log
    in again with the vendor CLI and re-run this command with --force.

    Revoke: hyprial agent provider logout <provider> --actor <NAME> removes the
    seeded files from the agent home (the agent keeps what its harness already
    loaded until it restarts). To invalidate the copied token itself, log out
    with the vendor's own CLI as well.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        if make_default:
            _refuse_in_worker_shell(services)
        target = actor or _addressed_actor_or_none()
        if target is None and not make_default:
            target = _addressed_actor(services)
        documents = read_provider_documents(services, provider, source=source)
        result: dict[str, Any] = {"ok": True, "provider": provider}
        if target is not None:
            result = seed_agent(services, provider, target, documents, force=force)
        if make_default:
            recorded = services._daemon_request(
                "agent.provider.default", {"provider": provider}
            )
            result["default"] = {
                "provider": recorded.get("provider"),
                "previous": recorded.get("previous"),
            }
        return result

    services._execute(operation, json_output=json_output)


def read_provider_documents(
    services: Any, provider: str, *, source: Path | None = None
) -> list[tuple[str, bytes]]:
    try:
        sources = resolve_provider_sources(
            provider,
            home=Path.home(),
            source=source,
            pi_agent_dir=_pi_agent_dir(provider),
        )
    except ProviderSourceError as error:
        raise services.CliError(error.code, error.message) from error
    documents: list[tuple[str, bytes]] = []
    for path in sources:
        try:
            documents.append((path.name, path.read_bytes()))
        except OSError as error:
            raise services.CliError(
                ipc_errors.PROVIDER_SOURCE_MISSING,
                f"{provider} login at {path} could not be read: {error.strerror}",
            ) from error
    return documents


def seed_agent(
    services: Any,
    provider: str,
    actor: str,
    documents: list[tuple[str, bytes]],
    *,
    force: bool = False,
) -> dict[str, Any]:
    seeded = services._daemon_request(
        "agent.provider.login",
        {
            "actor": actor,
            "provider": provider,
            "files": [
                {
                    "filename": filename,
                    "contentBase64": base64.b64encode(content).decode("ascii"),
                }
                for filename, content in documents
            ],
            "force": force,
        },
    )
    return _with_revoke_hint(seeded, provider, actor)


@provider_app.command("default")
def agent_provider_default(
    clear: bool = typer.Option(
        False, "--clear", help="Forget the machine default; existing agents keep theirs."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Show (or clear) the provider future 'hyprial agent create' runs seed from.

    Set it with 'hyprial agent provider login <provider> --default'. The seed
    copies the login of the user this daemon is logged in as.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        if clear:
            _refuse_in_worker_shell(services)
        return services._daemon_request(
            "agent.provider.default", {"clear": True} if clear else {}
        )

    services._execute(operation, json_output=json_output)


@provider_app.command("logout")
def agent_provider_logout(
    provider: str = typer.Argument(..., help="Harness: claude, codex, or pi (the native login files are per harness)."),
    actor: str | None = typer.Option(
        None,
        "--actor",
        help="Agent to revoke. Defaults to the agent this shell is running as.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Revoke a seeded provider login: remove its files from the agent home.

    Only the files that provider's harness reads are removed; the operator's
    own host login is untouched. A running agent keeps what its harness already
    loaded until it restarts. To invalidate the copied token itself, also log
    out with the vendor's own CLI.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        target = actor or _addressed_actor(services)
        return services._daemon_request(
            "agent.provider.logout", {"actor": target, "provider": provider}
        )

    services._execute(operation, json_output=json_output)


def revoke_command(provider: str, actor: str) -> str:
    return f"hyprial agent provider logout {provider} --actor {actor}"


def _with_revoke_hint(seeded: Any, provider: str, actor: str) -> dict[str, Any]:
    return {
        **dict(seeded),
        "revoke": revoke_command(provider, actor),
        "note": (
            "seeded, not validated; when it expires, log in again with the "
            "vendor CLI and re-run with --force"
        ),
    }


def seed_new_agent(
    services: Any,
    actor: str,
    default: object,
    *,
    json_output: bool,
    preferred_harness: str | None = None,
    model_provider: str | None = None,
) -> dict[str, Any]:
    """Seed a just-created agent from the machine default, or say how to.

    A default for one harness is not copied into an agent whose preferred
    harness is another. A renewable Codex/OpenAI login is also not copied into
    a custom-provider agent. The result is ``skipped`` with the safe next step.
    """

    if default == "codex" and model_provider not in {None, "openai"}:
        seed: dict[str, Any] = {
            "state": "skipped",
            "provider": default,
            "modelProvider": model_provider,
            "hint": [
                f"the agent selects custom Codex provider {model_provider}; "
                "the machine's renewable OpenAI auth.json is not projected",
                f"grant its API key: hyprial agent secret grant {actor} "
                "--source user-provider ...",
            ],
        }
    elif (
        isinstance(default, str)
        and default
        and preferred_harness is not None
        and preferred_harness != default
    ):
        seed = {
            "state": "skipped",
            "provider": default,
            "preferredHarness": preferred_harness,
            "hint": [
                f"the machine default seeds {default}; this agent prefers "
                f"{preferred_harness}: hyprial agent provider login "
                f"{preferred_harness} --actor {actor}",
                f"or grant an API key: hyprial agent secret grant {actor} "
                f"--source user-provider ...",
            ],
        }
    elif not isinstance(default, str) or not default:
        seed = {
            "state": "not-configured",
            "hint": [
                f"OAuth seed (copies this host's vendor CLI login, owned by the "
                f"daemon's logged-in user): hyprial agent provider login "
                f"<provider> --actor {actor}",
                f"API key (the default): hyprial agent secret grant {actor} "
                f"--source user-provider --entry-id <ENTRY> --field-name <FIELD> "
                f"--environment-name <NAME> --grant-id <ID> --revision 1",
            ],
        }
    else:
        try:
            documents = read_provider_documents(services, default)
            seeded = seed_agent(services, default, actor, documents)
        except services.CliError as error:
            seed = {
                "state": "refused",
                "provider": default,
                "code": error.code,
                "message": str(error),
                "hint": [
                    f"fix the cause, then: hyprial agent provider login {default} "
                    f"--actor {actor}",
                    f"or grant an API key: hyprial agent secret grant {actor} "
                    f"--source user-provider ...",
                ],
            }
        else:
            seed = {
                "state": "seeded",
                "provider": default,
                "files": seeded.get("files", []),
                "revoke": seeded["revoke"],
            }
    if seed["state"] != "seeded":
        warn(
            f"agent {actor} has no provider credential yet ({seed['state']}):\n  "
            + "\n  ".join(seed["hint"]),
            json_output=json_output,
        )
    return seed
