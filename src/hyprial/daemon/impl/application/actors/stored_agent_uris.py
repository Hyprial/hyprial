# Decision record (UUID addressing P4, external stores; spec §2.4 of
# docs/design/uuid-addressing-implementation-2026-11-03.md):
# - One mapping for every store: a stored agent:<owner>:<node>:<name> whose
#   node is this machine and whose name is the actor of a live registered
#   agent under that owner (local, or hosted for that owner) becomes that
#   agent's official address, read through _agent_address (the daemon's one
#   name-to-address entry).  Only an answer in a. id form is a rewrite;
#   every other value (ids, other nodes, service actors, unregistered or
#   destroyed names, users, channels) is kept verbatim.
# - Runs once per store, at daemon start, only while id_form_minting() is on.
#   The identity registry is opened (running the identity half of P4, which
#   rewrites agents.uri) while DaemonApplication is constructed; this runs
#   from _startup_adopt_domain_services right after the legacy workflow
#   cutover, before the link service is composed (its first tick reads PAC
#   owners) and before the PAC authority and the routine runtime open their
#   stores.  The PAC store is migrated only when that cutover succeeded:
#   opening it may run the PAC schema upgrade, which reads the sealed
#   workflow database, so the upgrade and the id-form rewrite land in the
#   same start, before the link service, authority, reactor, IPC or GC read
#   an owner.  When
#   the cutover failed the PAC authority does not open either, and the PAC
#   store stays unmarked for the next start.  Each store migrates in its own
#   transaction and writes its own durable marker, so a later start is a
#   no-op.  A store that fails is logged and retried at the next start; it
#   never blocks startup.  The routine store follows the same failure policy
#   in its shell adapter.
# - Covered here: Lark reply routes (adapters.sqlite3), PAC authorization
#   owners (pac-graph.sqlite3), org ACL entry grants.  The routine store is a
#   biz store: the mapping travels to it through RoutineRuntimeDeps.
#   Desired-state interactive sessions belong to
#   _normalize_persisted_interactive_sessions (integration ruling D6).
#   Message rows (inbox, custody, delivery status, dead letters, PAC
#   notifications) are never rewritten (ruling X).
"""Stored URI migration and receipt-proven routine publication addresses.

Routine publication leaves stored YAML intact. Only its declared local owned
target can use a current entity, registration marker, and process observation
fence to publish an official owner. Borrowed targets and other principals keep
the strict resolver; this is not a general name-address compatibility path.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from hyprial.kernel import DaemonRequestError, is_agent_id_segment, parse_agent_uri

MIGRATED_EVENT = "agent_uri.stores_migrated"
FAILED_EVENT = "agent_uri.store_migration_failed"


def resolve_routine_target(app: Any, name: str, target: str) -> str:
    """Publish the current official identity of this routine's owned worker."""

    parsed = parse_agent_uri(target)
    if (parsed is None or parsed[:2] != (app.owner, app.node_id)
            or app._routine_service is None):
        return app._resolve_routine_principal(target)
    routine = app._routine_service.status(name=name)
    if routine.get("produces") != target or routine.get("actorOwnership") != "routine":
        return app._resolve_routine_principal(target)
    agent = app.agents.current(target)
    binding = None if agent is None else app._agent_liveness.binding(agent.uri)
    spec = None if agent is None else app.desired_state.harness_spec(agent.actor)
    marker = app._routine_coordinator_marker(name, routine.get("registrationId"))
    runtime = getattr(app._harnesses, "actor", None)
    observation = None
    if (agent is not None and binding is not None and spec is not None
            and (target == agent.uri if is_agent_id_segment(parsed[2]) else agent.actor == parsed[2])
            and (agent.owner, agent.machine) == parsed[:2]
            and agent.hosted_by is None and spec.headless and spec.nickname == marker
            and binding.runtime == "headless" and binding.harness == spec.harness
            and runtime is not None):
        observation = runtime.projection.read_observation_source(
            f"{spec.harness}:{agent.actor}", agent.entity_token, binding.revision,
        )
    current = None if agent is None else app.agents.current(target)
    if (observation is None or current is None or current.entity_token != agent.entity_token
            or current.uri != agent.uri or app._agent_liveness.binding(agent.uri) != binding
            or app.desired_state.harness_spec(agent.actor) != spec
            or runtime.projection.read_observation_source(
                f"{spec.harness}:{agent.actor}", agent.entity_token, binding.revision,
            ) != observation):
        raise DaemonRequestError("ROUTINE_COORDINATOR_IDENTITY_MISMATCH",
                                 "routine coordinator has no current owned process identity")
    return agent.uri


def stored_agent_uri_mapping(app: Any) -> Callable[[str], str | None]:
    """The official URI for a stored name-form local agent URI, else None."""

    def official(value: str) -> str | None:
        parsed = parse_agent_uri(value)
        if parsed is None or parsed[1] != app.node_id or is_agent_id_segment(parsed[2]):
            return None
        address = app._agent_address(parsed[2], owner=parsed[0])
        target = parse_agent_uri(address)
        if address == value or target is None or not is_agent_id_segment(target[2]):
            return None
        return address

    return official


def migrate_stored_agent_uris(app: Any, *, pac: bool = True) -> dict[str, int] | None:
    """Migrate the daemon-owned stores once; None while the switch is off.

    ``pac`` is False when the legacy workflow cutover failed; the PAC store
    is then left unopened and unmarked.
    """

    from hyprial.identity import id_form_minting

    if not id_form_minting():
        return None
    from hyprial.daemon.impl.adapters.lark.state.agent_uris import (
        migrate_reply_route_agent_uris,
    )
    from hyprial.daemon.impl.org.network.state import migrate_org_acl_grant_agents
    from hyprial.daemon.impl.pac.storage.agent_uris import migrate_pac_agent_uris
    from hyprial.daemon.impl.pac.storage.store import default_database_path

    mapping = stored_agent_uri_mapping(app)
    at_ms = time.time_ns() // 1_000_000
    runs: tuple[tuple[str, Callable[[], int | None]], ...] = (
        ("adapters", lambda: migrate_reply_route_agent_uris(
            app.state_dir / "adapters.sqlite3", mapping)),
        ("pac", lambda: migrate_pac_agent_uris(
            default_database_path(app.state_dir), mapping, at_ms=at_ms)),
        ("orgAcl", lambda: migrate_org_acl_grant_agents(
            app.hyprial_home, mapping, at_ms=at_ms)),
    )
    counts: dict[str, int] = {}
    for store, run in runs:
        if store == "pac" and not pac:
            continue
        try:
            rewritten = run()
        except Exception as error:  # noqa: BLE001 - retried at the next start
            app._log(
                "error", "agents", FAILED_EVENT, store=store,
                error=f"{type(error).__name__}: {error}"[:500],
            )
            continue
        if rewritten is not None:
            counts[store] = rewritten
    if counts:
        app._log("info", "agents", MIGRATED_EVENT, stores=counts)
    return counts


__all__ = [
    "FAILED_EVENT",
    "MIGRATED_EVENT",
    "migrate_stored_agent_uris",
    "stored_agent_uri_mapping",
]
