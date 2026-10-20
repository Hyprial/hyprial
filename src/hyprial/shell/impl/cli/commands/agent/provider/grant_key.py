"""`hyprial agent provider grant-key`: grant one user-provider entry to many agents.

Design: docs/design/provider-login-2026-10-14.md, "Batch API-key grant". This is
a CLI loop over the existing ``agent.secret.grant`` and adds no daemon
authority. The three principles hold by construction:

- owned by the daemon's logged-in user: the source is fixed to that user's
  ``user-provider`` store; ``agent-private`` is per agent and not offered;
- revocable per agent: every actor receives its own grant record, and the
  result lists ``hyprial agent secret revoke <actor> <grant-id>`` for each;
- no echo: only the entry id, field name and environment names are sent or
  printed; the value is never read here.

Actors are named one by one (no ``--all``).  Before granting, each actor's
existing grants are read (``agent.secret.list``): a grant under the same id
that already points at the same entry, field and names is reported
``alreadyGranted`` and skipped, so re-running the same command after a partial
failure continues where it stopped.  A grant under the same id that points
elsewhere is refused with ``PROVIDER_CREDENTIAL_EXISTS`` unless ``--replace``
is given (the daemon upserts by grant id, so replacing silently would lose the
old grant); with ``--replace`` the daemon still requires a higher
``--revision`` and the actor is reported under ``replaced``, with the replaced
grant's entry, field and revision under ``replacedGrants`` for audit.  The loop stops at
the first refusal; grants already made stay made, because each is a complete,
individually revocable grant, and the result reports granted, replaced,
alreadyGranted, failed and notAttempted actors with ``ok: false``.
"""

from __future__ import annotations

from typing import Any

import typer

from hyprial.kernel import ipc_errors
from hyprial.shell.impl.cli.commands.agent.provider.commands import provider_app
from hyprial.shell.impl.cli.commands.common.services import get_services

_USER_SOURCE = "user-" + "provider"


@provider_app.command("grant-key")
def agent_provider_grant_key(
    actors: list[str] = typer.Option(
        ..., "--actor", help="Agent to grant to; repeat for each agent."
    ),
    entry_id: str = typer.Option(
        ..., "--entry-id", help="Entry written with 'hyprial agent secret provider-write'."
    ),
    field_name: str = typer.Option(..., "--field-name", help="Field of that entry."),
    environment_name: list[str] = typer.Option(
        ..., "--environment-name", help="Approved environment variable name; repeatable."
    ),
    grant_id: str = typer.Option(..., "--grant-id", help="Grant id used for every agent."),
    revision: int = typer.Option(..., "--revision", help="Positive grant revision."),
    replace: bool = typer.Option(
        False,
        "--replace",
        help=(
            "Replace a different grant already held under --grant-id "
            "(needs a higher --revision than the one it replaces)."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON only."),
) -> None:
    """Grant one shared API-key entry to several agents, one grant each.

    The entry belongs to the user this daemon is logged in as. Each agent gets
    its own grant; revoke one with 'hyprial agent secret revoke <actor>
    <grant-id>' (the entry itself stays). The key value is never read or shown.

    Re-running the same command is safe: actors that already hold this exact
    grant are skipped (alreadyGranted). An actor holding a different grant
    under the same id is refused unless --replace. Stops at the first refusal
    and reports granted, replaced, alreadyGranted, failed and notAttempted.
    """
    services = get_services()

    def operation() -> Any:
        services = get_services()
        if len(set(actors)) != len(actors):
            raise services.CliError(
                ipc_errors.INVALID_ARGUMENT, "each --actor may be named once"
            )
        wanted = {
            "source": _USER_SOURCE,
            "entryId": entry_id,
            "fieldName": field_name,
            "environmentNames": list(environment_name),
        }
        outcome: dict[str, list[str]] = {"granted": [], "replaced": [], "alreadyGranted": []}
        replaced_grants: list[dict[str, Any]] = []
        for index, actor in enumerate(actors):
            try:
                held = _held_grant(services, actor, grant_id)
                if held is not None and _same_grant(held, wanted):
                    outcome["alreadyGranted"].append(actor)
                    continue
                if held is not None and not replace:
                    raise services.CliError(
                        ipc_errors.PROVIDER_CREDENTIAL_EXISTS,
                        f"{actor} already holds grant {grant_id} for "
                        f"{held.get('entryId')}/{held.get('fieldName')}; pass "
                        "--replace with a higher --revision to replace it",
                    )
                services._daemon_request(
                    "agent.secret.grant",
                    {"actor": actor, "grantId": grant_id, **wanted, "revision": revision},
                )
            except services.CliError as error:
                return {
                    "ok": False,
                    **outcome,
                    "replacedGrants": replaced_grants,
                    "failed": {"actor": actor, "code": error.code, "message": str(error)},
                    "notAttempted": actors[index + 1 :],
                    "revoke": _revoke_lines(_holding(outcome, actors), grant_id),
                }
            outcome["replaced" if held is not None else "granted"].append(actor)
            if held is not None:
                replaced_grants.append(_replaced_summary(actor, held))
        return {
            "ok": True,
            **outcome,
            "replacedGrants": replaced_grants,
            "revoke": _revoke_lines(_holding(outcome, actors), grant_id),
        }

    services._execute(operation, json_output=json_output)


def _held_grant(services: Any, actor: str, grant_id: str) -> dict[str, Any] | None:
    listed = services._daemon_request("agent.secret.list", {"actor": actor})
    grants = listed.get("grants", []) if isinstance(listed, dict) else []
    for grant in grants:
        if isinstance(grant, dict) and grant.get("grantId") == grant_id:
            return grant
    return None


def _replaced_summary(actor: str, held: dict[str, Any]) -> dict[str, Any]:
    return {
        "actor": actor,
        "entryId": held.get("entryId"),
        "fieldName": held.get("fieldName"),
        "revision": held.get("revision"),
    }


def _same_grant(held: dict[str, Any], wanted: dict[str, Any]) -> bool:
    return all(held.get(key) == value for key, value in wanted.items())


def _holding(outcome: dict[str, list[str]], actors: list[str]) -> list[str]:
    holders = set(outcome["granted"]) | set(outcome["replaced"]) | set(outcome["alreadyGranted"])
    return [actor for actor in actors if actor in holders]


def _revoke_lines(actors: list[str], grant_id: str) -> list[str]:
    return [f"hyprial agent secret revoke {actor} {grant_id}" for actor in actors]
