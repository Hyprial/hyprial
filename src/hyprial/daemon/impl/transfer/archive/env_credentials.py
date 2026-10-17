"""Resolve an agent's env credentials through the daemon's own gate.

The export side (the CLI) has no registry and must not open a secret file
itself: the only correct source of an env-key credential is the same
grant + incarnation resolver the launch uses.  These helpers run where that
registry lives -- the daemon -- and turn the resolved secrets into wire records
the quiesce answer publishes and ``plan_credential_seal`` consumes.

Split out of ``envelope.py`` when the producer pushed that module over the
structural line limit.  This module imports ``EnvCredential`` from
``envelope``; ``envelope`` imports nothing from here.
"""

from __future__ import annotations

from pathlib import Path

from hyprial.daemon.impl.transfer.archive.envelope import EnvCredential

__all__ = ["env_credentials_for_quiesce", "resolve_env_credentials"]


def resolve_env_credentials(
    *,
    registry: object,
    hyprial_home: str | Path,
    agent_name: str,
    harness: str,
) -> tuple[EnvCredential, ...]:
    """Resolve one agent's authorized env credentials through the daemon gate.

    The registry's ``secret_inventory`` names every grant; the shared
    ``SecretResolver`` resolves one exact entry under the grant's incarnation
    fence.  A grant that exists but cannot produce a value degrades to a
    ``requires_reauth`` record -- the native-file precedent -- so the export
    never silently loses a credential.  This never scans a process environment
    and never opens a secret file itself: both are the resolver's job.
    """

    from hyprial.identity import (
        SecretResolutionError,
        SecretResolver,
        SecretSource,
    )

    inventory = registry.secret_inventory(agent_name)
    resolver = SecretResolver(Path(hyprial_home), registry)
    resolved: list[EnvCredential] = []
    for grant in inventory:
        exclude = (
            f"secrets/{grant.entry_id}"
            if grant.source is SecretSource.AGENT_PRIVATE
            else None
        )
        try:
            secret = resolver.resolve(agent_name, grant.grant_id)
        except SecretResolutionError as error:
            resolved.append(
                EnvCredential(
                    harness=harness,
                    entry_id=grant.entry_id,
                    environment=grant.environment_names,
                    field_name=grant.field_name,
                    requires_reauth=True,
                    exclude=exclude,
                    notes=(
                        "not resolvable through this daemon's gate "
                        f"({error.category}); the target must re-authenticate"
                    ),
                )
            )
            continue
        resolved.append(
            EnvCredential(
                harness=harness,
                entry_id=grant.entry_id,
                environment=grant.environment_names,
                field_name=grant.field_name,
                value=secret.value,
                exclude=exclude,
                notes=f"resolved from the agent's authorized grant {grant.grant_id}",
            )
        )
    return tuple(resolved)


def env_credentials_for_quiesce(
    *,
    registry: object,
    hyprial_home: str | Path,
    spec: object,
) -> list[dict[str, object]]:
    """The wire form the quiesce answer publishes for the export's seal plan."""

    return [
        item.as_dict()
        for item in resolve_env_credentials(
            registry=registry,
            hyprial_home=hyprial_home,
            agent_name=spec.name,
            harness=spec.harness,
        )
    ]
