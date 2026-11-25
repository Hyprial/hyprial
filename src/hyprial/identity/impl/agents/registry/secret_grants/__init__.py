"""Secret grant mutation through the existing registry transaction authority."""
from __future__ import annotations

import json
from dataclasses import replace
from collections.abc import Iterable
from typing import TYPE_CHECKING

from hyprial.identity.impl.agents.home.provisioner import AgentHomeError, HomeReceipt
from .._base import AgentError
from ..naming.id_form import stamp_agent_id

if TYPE_CHECKING:
    from hyprial.identity.impl.agents.runtime.secrets import SecretGrant, SecretSource


class _RegistrySecretGrantMixin:
    def grant_secret(
        self,
        actor: str,
        *,
        grant_id: str,
        source: "SecretSource",
        entry_id: str,
        field_name: str | None,
        environment_names: Iterable[str],
        revision: int,
        prevalidated_home_token: str | None = None,
        owner_actor: str | None = None,
        target_entity_token: str | None = None,
        prevalidated_route_required: bool | None = None,
    ) -> "SecretGrant":
        """Bind one exact entry to the current agent incarnation."""

        from hyprial.identity.impl.agents.runtime.secrets  import SecretGrant, SecretSource

        agent = self.require(actor)
        owner = self.require(owner_actor) if owner_actor is not None else None
        grant = SecretGrant(
            actor=agent.actor,
            entity_token=agent.entity_token,
            grant_id=grant_id,
            source=SecretSource(source),
            entry_id=entry_id,
            field_name=field_name,
            environment_names=tuple(environment_names),
            revision=revision,
            owner_actor=None if owner is None else owner.actor,
            owner_entity_token=None if owner is None else owner.entity_token,
        )
        if prevalidated_route_required is not None:
            grant = replace(grant, route_required=prevalidated_route_required)
        elif grant.source is SecretSource.USER_PROVIDER and self._home is not None:
            from hyprial.identity.impl.agents.runtime.secrets import SecretResolver

            resolver = SecretResolver(self._home.hyprial_home, self)
            grant = replace(grant, route_required=resolver.model_route_required(entry_id, field_name))
        if owner is not None:
            if self.home_receipt(owner.actor).entity_token != owner.entity_token:
                raise AgentError("secret owner incarnation changed")
        validated_home_token = None
        if grant.source in {SecretSource.AGENT_PRIVATE, SecretSource.AGENT_SHARED}:
            validated_home_token = (
                self.home_receipt(agent.actor).entity_token
                if prevalidated_home_token is None
                else prevalidated_home_token
            )
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if owner is not None and self.require(owner.actor).entity_token != owner.entity_token:
                raise AgentError("secret owner incarnation changed")
            incumbent = self._db.execute(
                "SELECT entity_token FROM agents WHERE actor=?", (agent.actor,)
            ).fetchone()
            if target_entity_token is not None and (
                incumbent is None or str(incumbent["entity_token"]) != target_entity_token
            ):
                from ..config_binding import AgentIncarnationMismatch

                raise AgentIncarnationMismatch("target agent incarnation changed before secret grant")
            if incumbent is None or str(incumbent["entity_token"]) != grant.entity_token:
                raise AgentError("agent incarnation changed during secret grant")
            if validated_home_token is not None:
                row = self._db.execute(
                    "SELECT active,payload FROM lifecycle_resources WHERE resource_key=?",
                    (f"agent-home:{agent.actor}",),
                ).fetchone()
                if row is None or not bool(row["active"]):
                    raise AgentHomeError("revoked", agent.actor, "grant-secret")
                receipt = HomeReceipt.from_json(json.loads(str(row["payload"])))
                if (
                    receipt.status != "ready"
                    or receipt.entity_token != validated_home_token
                    or receipt.entity_token != grant.entity_token
                ):
                    raise AgentHomeError("receipt-mismatch", agent.actor, "grant-secret")
            prior = self._db.execute(
                "SELECT revision FROM agent_secret_grants "
                "WHERE agent = ? AND grant_id = ?",
                (agent.actor, grant.grant_id),
            ).fetchone()
            if prior is not None and revision <= int(prior["revision"]):
                raise AgentError("secret grant revision must increase")
            self._db.execute(
                "INSERT INTO agent_secret_grants "
                "(agent, entity_token, grant_id, source, entry_id, field_name, "
                "environment_names, revision, owner_actor, owner_entity_token, route_required) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent, grant_id) DO UPDATE SET "
                "entity_token=excluded.entity_token, source=excluded.source, "
                "entry_id=excluded.entry_id, field_name=excluded.field_name, "
                "environment_names=excluded.environment_names, "
                "revision=excluded.revision, owner_actor=excluded.owner_actor, "
                "owner_entity_token=excluded.owner_entity_token, route_required=excluded.route_required",
                (
                    grant.actor,
                    grant.entity_token,
                    grant.grant_id,
                    grant.source.value,
                    grant.entry_id,
                    grant.field_name,
                    json.dumps(list(grant.environment_names)),
                    grant.revision,
                    grant.owner_actor,
                    grant.owner_entity_token,
                    int(grant.route_required),
                ),
            )
            stamp_agent_id(self._db, "agent_secret_grants", grant.entity_token)
        return grant
