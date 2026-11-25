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
from typing import Any, Callable

from hyprial.daemon.impl.transfer.archive.envelope import EnvCredential

__all__ = ["env_credentials_for_quiesce", "resolve_env_credentials"]


def resolve_env_credentials(
    *,
    registry: object,
    hyprial_home: str | Path,
    agent_name: str,
    harness: str,
    withheld: Callable[[Any], bool] | None = None,
) -> tuple[EnvCredential, ...]:
    """Resolve one agent's authorized env credentials through the daemon gate.

    The registry's ``secret_inventory`` names every grant; the shared
    ``SecretResolver`` resolves one exact entry under the grant's incarnation
    fence.  A grant that exists but cannot produce a value degrades to a
    ``requires_reauth`` record -- the native-file precedent -- so the export
    never silently loses a credential.  This never scans a process environment
    and never opens a secret file itself: both are the resolver's job.

    ``withheld`` names grants whose value must never leave this node (user-sourced
    grants and owner-key slots under that gate); each becomes a ``requires_reauth`` record
    and is not resolved at all.
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
        if withheld is not None and withheld(grant):
            resolved.append(
                EnvCredential(
                    harness=harness,
                    entry_id=grant.entry_id,
                    environment=grant.environment_names,
                    field_name=grant.field_name,
                    requires_reauth=True,
                    exclude=exclude,
                    notes=(
                        "held by the owner-key authority and never exported; "
                        "the target must re-authenticate"
                    ),
                )
            )
            continue
        try:
            secret = resolver.resolve(agent_name, grant.grant_id)
            if secret.route is not None:
                raise SecretResolutionError("route-unsupported", grant.entry_id, "transfer-export")
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
    app: Any = None,
) -> list[dict[str, object]]:
    """The wire form the quiesce answer publishes for the export's seal plan.

    ``app`` is the exporting daemon; with it, the owner-key authority (behind
    its gate) decides what is withheld and announced (see :func:`_owner_policy`).
    """

    withheld, announced = (
        (None, ()) if app is None
        else _owner_policy(app, registry, spec.name, spec.harness)
    )
    resolved = resolve_env_credentials(
        registry=registry,
        hyprial_home=hyprial_home,
        agent_name=spec.name,
        harness=spec.harness,
        withheld=withheld,
    )
    return [item.as_dict() for item in (*resolved, *announced)]


def _owner_policy(
    app: Any, registry: Any, agent_name: str, harness: str,
) -> tuple[Callable[[Any], bool] | None, tuple[EnvCredential, ...]]:
    """What the export withholds and announces under the owner-key gate.

    The legacy gate (and a guest agent the authority can never hold) changes
    nothing.  Under "owner-keys" no user-sourced plaintext is exported at all,
    nor any grant whose id is a slot the authority holds a record for, verified
    or not: a slot whose record fails verification is invisible to the grant
    listing, so withholding only the listed slots would fall back to the legacy
    value, an agent-private one included (§7.3, §8.2, §8.4).  When the
    authority cannot answer, the same user-sourced rule applies.  A live slot
    (active or awaiting a key) with no legacy row is announced as a
    ``requires_reauth`` record so the target learns the credential exists; a
    revoked slot holds no credential and is not announced.
    """

    from hyprial.identity import SecretResolutionError, SecretSource
    from hyprial.daemon.impl.provider_keys.launch import export_slots

    def user_sourced(grant: Any) -> bool:
        return grant.source is SecretSource.USER_PROVIDER

    try:
        exported = export_slots(app, agent_name, registry=registry)
    except SecretResolutionError:
        return user_sourced, ()
    if exported is None:
        return None, ()
    slots, held = exported
    present = frozenset(grant.grant_id for grant in registry.secret_inventory(agent_name))
    announced = tuple(
        EnvCredential(
            harness=harness,
            entry_id=slot.entry_id,
            environment=slot.environment_names,
            field_name=None,
            requires_reauth=True,
            notes=(
                "held by the owner-key authority and never exported; "
                "the target must re-authenticate"
            ),
        )
        for slot in slots
        if slot.state != "revoked" and slot.grant_id not in present
    )
    return (lambda grant: user_sourced(grant) or grant.grant_id in held), announced
