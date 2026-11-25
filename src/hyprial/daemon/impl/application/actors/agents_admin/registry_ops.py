# Decision record (UA1b, 2026-11-18; UA1c; UA1f): ``agent.list`` hides this
# machine's user agent by default, so a user who created no agents still gets
# an empty list; ``includeUserAgent: true`` (read with ``is True`` like
# ``excludeWf``) adds its row, marked ``userAgent: true``.  It is never in
# ``agentsWithoutRoutine`` (it has no model, so it is never offered a routine)
# nor in the ``inactiveSince`` cleanup listing (it is never active and can
# never be destroyed), opt-in or not.  ``agent.get`` by name answers it, and
# the whole-node ``agent.home-census`` leaves it out unless its ``actor``
# selector names it.
"""Agent administration: registry verbs, migration, destroy/stop and runtime-launch custody."""
from __future__ import annotations
import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4
from hyprial.identity import (
    Agent,
    RUNTIME_HEADLESS,
    RUNTIME_INTERACTIVE,
    AgentConfig,
    AgentError,
    AgentHomeError,
    HandoverNotice,
    normalize_capabilities,
    name_taken_remedy,
    normalize_harness_args,
)
from hyprial.kernel import DaemonRequestError, DurationParseError, ipc_errors, parse_duration
from hyprial.daemon.impl.composition  import (
    DomainCommandError,
)
from hyprial.daemon.impl.configuration.identity import (
    normalize_agent_recipient,
)
from hyprial.kernel import (
    canonical_user_uri,
)
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _optional_positive_integer,
    _optional_string_param,
    _required_string,
)
from hyprial.daemon.impl.provider_keys.legacy import LEGACY_SECRET_METHODS, NOT_HANDLED, dispatch as dispatch_legacy_secret

from .policies import handle_restore_policy
from .secret_fences import expected_secret_target

_AGENT_OPERATION_NAMESPACE = "agent"
_RESUME_COMMIT_GUARD_VERSION = 1

def _canonical_holder_name(name: str) -> str:
    """Case-insensitive identity for holder names.

    A recipient URI's machine segment and a mesh holder (node id) name the
    same kind of thing, but nothing forces them to agree on casing, and a
    raw comparison would silently read a casing-only mismatch as "holder
    absent".  Every holder comparison must go through this normalizer.
    """
    return name.casefold()


class _AgentRegistryOpsMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _handle_agent(self, method: str, params: JsonObject, *, expected_secret_agent=None) -> Any:
        target = expected_secret_target(self, method, params, expected_secret_agent)
        if method in {"agent.create", "agent.start.preflight"}:
            from hyprial.daemon.impl.bootstrap.names import require_agent_name_available
            from hyprial.daemon.impl.cells.runtime.agents import refuse_custody_launch

            name = self.agents.native_actor(_required_string(params.get("name"), "name"))
            require_agent_name_available(self, name)
            refuse_custody_launch(
                self.agents.get(name), method, params, owner=self.owner
            )
        if method == "agent.resume.restore":
            name = self.agents.native_actor(
                _required_string(params.get("name"), "name")
            )
            expected = Agent.from_json(params.get("expected"), "expected")
            requested_replacement = Agent.from_json(
                params.get("replacement"), "replacement"
            )
            if (
                expected.actor != name
                or requested_replacement.actor != name
                or expected.uri != requested_replacement.uri
                or expected.entity_token != requested_replacement.entity_token
            ):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "guarded resume restore snapshots must name the same Agent incarnation",
                )
            with self._interactive_route_effect(expected.uri):
                guard = self._resume_agent_restore_guards.get(expected.uri)
                if guard is None or guard[1] != expected:
                    raise DaemonRequestError(
                        ipc_errors.RESUME_AGENT_RESTORE_FAILED,
                        "resume start was refused, but the daemon no longer has the "
                        "exact agent.create rollback record for this attempt",
                    )
                replacement = guard[0]
                del self._resume_agent_restore_guards[expected.uri]
                if replacement is None:
                    raise DaemonRequestError(
                        ipc_errors.RESUME_AGENT_RESTORE_FAILED,
                        "resume start was refused after agent.create created a new Agent; "
                        "automatic removal is not safe, so inspect and retire that Agent "
                        "explicitly",
                    )
                restored = self.agents.compare_and_swap(expected, replacement)
            if not restored:
                raise DaemonRequestError(
                    ipc_errors.RESUME_AGENT_RESTORE_FAILED,
                    "resume start was refused, but Agent restore was refused because "
                    "the Agent no longer matched this start attempt; inspect the later "
                    "Agent write before retrying",
                )
            return {"ok": True, "restored": True, "agent": replacement.to_json()}
        if method.startswith("agent.migrate.") or method == "agent.config.rebind":
            return self._handle_agent_migration(method, params)
        if method in ("agent.grant", "agent.revoke", "agent.grants"):
            # L0: the local host operator records these facts. This ledger is
            # not a caller-authentication or runtime enforcement boundary.
            try:
                if method == "agent.grant":
                    from hyprial.identity import (
                        AgentCapabilityGrantCompleted,
                        GrantAgentCapabilityCommand,
                    )

                    revision = _optional_positive_integer(params.get("revision"), "revision")
                    if revision is None:
                        raise ValueError("revision is required")
                    generation = self._agent_session_domains.agent.generation
                    event = self._agent_session_domains.call_agent(
                        GrantAgentCapabilityCommand(
                            f"{_AGENT_OPERATION_NAMESPACE}:grant:{uuid4().hex}",
                        _required_string(params.get("actor"), "actor"),
                            _required_string(params.get("grantId"), "grantId"),
                            _required_string(params.get("capability"), "capability"),
                            _required_string(params.get("scope"), "scope"),
                            canonical_user_uri(self.owner), revision,
                        ),
                        AgentCapabilityGrantCompleted,
                    )
                    if event.generation != generation or event.operation != "grant":
                        raise DomainCommandError(
                            "STALE_AGENT_GRANT", "agent grant completion fence mismatch"
                        )
                    assert event.grant is not None
                    return {"ok": True, "grant": event.grant.to_json()}
                if method == "agent.revoke":
                    from hyprial.identity import (
                        AgentCapabilityGrantCompleted,
                        RevokeAgentCapabilityCommand,
                    )

                    generation = self._agent_session_domains.agent.generation
                    event = self._agent_session_domains.call_agent(
                        RevokeAgentCapabilityCommand(
                            f"{_AGENT_OPERATION_NAMESPACE}:revoke:{uuid4().hex}",
                            _required_string(params.get("actor"), "actor"),
                            _required_string(params.get("grantId"), "grantId"),
                            canonical_user_uri(self.owner),
                        ),
                        AgentCapabilityGrantCompleted,
                    )
                    if event.generation != generation or event.operation != "revoke":
                        raise DomainCommandError(
                            "STALE_AGENT_GRANT", "agent revoke completion fence mismatch"
                        )
                    return {"ok": True, "revoked": event.changed}
                actor = _optional_string_param(params.get("actor"), "actor")
                if params.get("audit") is True:
                    if actor is None:
                        raise ValueError("actor is required for audit")
                    entries = self._agent_session_domains.agent.read_grant_journal(actor)
                    return {"ok": True, "journal": [entry.to_json() for entry in entries]}
                grants = self._agent_session_domains.agent.read_capability_grants(actor)
                return {"ok": True, "grants": [grant.to_json() for grant in grants]}
            except (ValueError, TypeError, AgentError) as error:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        if method in LEGACY_SECRET_METHODS:
            secret_params = (dict(params, expectedAgent=expected_secret_agent)
                             if expected_secret_agent is not None else params)
            handled = dispatch_legacy_secret(self, method, secret_params)
            if handled is not NOT_HANDLED:
                return handled
        if method == "agent.secret." + "provider-write":
            from hyprial.identity import SecretResolver

            entry_id = _required_string(params.get("entryId"), "entryId")
            field_name = _required_string(params.get("fieldName"), "fieldName")
            value = params.get("value")
            if not isinstance(value, str) or not value:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "secret model-vendor value must be non-empty",
                )
            if "\n" in value or "\r" in value:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "secret model-vendor value must contain one line",
                )
            try:
                resolver = SecretResolver(self.hyprial_home, self._agent_registry)
                getattr(resolver, "write_user_" + "provider")(
                    entry_id, {field_name: value}
                )
            except (ValueError, TypeError, OSError) as error:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
            saved = resolver.model_service_metadata(entry_id)
            return {"ok": True, "entryId": entry_id, "fieldNames": saved["fieldNames"]}
        if method == "agent.secret.grant":
            from hyprial.identity import SecretSource
            from hyprial.identity import (
                AgentSecretGrantCompleted, GrantAgentSecretCommand,
            )

            actor = _required_string(params.get("actor"), "actor")
            grant_id = _required_string(params.get("grantId"), "grantId")
            source_raw = _required_string(params.get("source"), "source")
            entry_id = _required_string(params.get("entryId"), "entryId")
            field_name = _required_string(params.get("fieldName"), "fieldName")
            raw_names = params.get("environmentNames")
            if not isinstance(raw_names, list) or not raw_names or any(
                not isinstance(item, str) or not item for item in raw_names
            ):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "environmentNames must be a non-empty string array",
                )
            revision = _optional_positive_integer(params.get("revision"), "revision")
            if revision is None:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "revision is required")
            try:
                source = SecretSource(source_raw)
                from hyprial.identity import SecretResolver

                route_required = source is SecretSource.USER_PROVIDER and SecretResolver(
                    self.hyprial_home, self._agent_registry,
                ).model_route_required(entry_id, field_name)
                home_token = (
                    self._agent_session_domains.agent.prevalidate_home(actor).entity_token
                    if source in {SecretSource.AGENT_PRIVATE, SecretSource.AGENT_SHARED} else None
                )
                generation = self._agent_session_domains.agent.generation
                event = self._agent_session_domains.call_agent(
                    GrantAgentSecretCommand(
                        f"{_AGENT_OPERATION_NAMESPACE}:secret-grant:{uuid4().hex}", actor, grant_id,
                        source, entry_id, field_name, tuple(raw_names), revision,
                        home_token, _optional_string_param(params.get("ownerActor"), "ownerActor"),
                        target_entity_token=None if target is None else target.entity_token,
                        route_required=route_required,
                    ),
                    AgentSecretGrantCompleted,
                )
                if event.generation != generation or event.operation != "grant":
                    raise DomainCommandError(
                        "STALE_AGENT_SECRET_GRANT",
                        "agent secret grant completion fence mismatch",
                    )
                assert event.grant is not None
                grant = event.grant
            except (ValueError, TypeError, AgentError) as error:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
            return {
                "ok": True,
                "grant": {
                    "actor": grant.actor,
                    "grantId": grant.grant_id,
                    "source": grant.source.value,
                    "entryId": grant.entry_id,
                    "fieldName": grant.field_name,
                    "environmentNames": list(grant.environment_names),
                    "revision": grant.revision,
                    "ownerActor": grant.owner_actor,
                    "ownerEntityToken": grant.owner_entity_token,
                },
            }
        if method == "agent.secret.list":
            actor = params.get("actor")
            if actor is not None and (not isinstance(actor, str) or not actor):
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "actor must be a non-empty string")
            constraints = {} if target is None else {
                "expected_uri": str(target.uri), "expected_created_at_ms": target.created_at_ms,
            }
            try:
                grants = self._agent_session_domains.agent.read_secret_inventory(actor, **constraints)
            except AgentError as error:
                if target is None:
                    raise
                raise DaemonRequestError(getattr(error, "code", "AGENT_ERROR"), str(error)) from error
            return {
                "ok": True,
                "grants": [
                    {
                        "actor": grant.actor,
                        "grantId": grant.grant_id,
                        "source": grant.source.value,
                        "entryId": grant.entry_id,
                        "fieldName": grant.field_name,
                        "environmentNames": list(grant.environment_names),
                        "revision": grant.revision,
                        "ownerActor": grant.owner_actor,
                        "ownerEntityToken": grant.owner_entity_token,
                    }
                    for grant in grants
                ],
            }
        if method == "agent.secret.revoke":
            from hyprial.identity import (
                AgentSecretGrantCompleted, RevokeAgentSecretCommand,
            )

            actor = _required_string(params.get("actor"), "actor")
            grant_id = _required_string(params.get("grantId"), "grantId")
            try:
                generation = self._agent_session_domains.agent.generation
                event = self._agent_session_domains.call_agent(
                    RevokeAgentSecretCommand(
                        f"{_AGENT_OPERATION_NAMESPACE}:secret-revoke:{uuid4().hex}", actor, grant_id,
                    ),
                    AgentSecretGrantCompleted,
                )
                if event.generation != generation or event.operation != "revoke":
                    raise DomainCommandError(
                        "STALE_AGENT_SECRET_GRANT",
                        "agent secret revoke completion fence mismatch",
                    )
                revoked = event.changed
            except AgentError as error:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
            return {"ok": True, "revoked": revoked}
        if method == "agent.start.preflight":
            agent = self.agents.get(name)
            if agent is None:
                raise DaemonRequestError(
                    ipc_errors.AGENT_NOT_FOUND,
                    f"no agent named {name!r} on this machine",
                )
            harness = _required_string(params.get("harness"), "harness")
            self._effective_harness_args(
                agent.actor, harness, (),
                ignore_stored=params.get("ignoreStoredHarnessArgs") is True,
            )
            runtime = str(params.get("runtime") or RUNTIME_HEADLESS)
            with self._interactive_route_effect(agent.uri):
                self._refuse_if_running(
                    agent.uri, harness=harness, runtime=runtime
                )
            return {
                "ok": True,
                "agent": self._agent_status_json(agent),
                "resumeCommitGuardVersion": _RESUME_COMMIT_GUARD_VERSION,
            }
        if method == "agent.create":
            # Decision A5: the one creation path. `hyprial agent create` calls it
            # directly; `hyprial agent start` calls it first and only then launches a
            # harness, so a connector can never exist without an identity.
            reuse = params.get("existing") == "reuse"
            if "org" in params:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "agent.create does not accept an organization",
                )
            existing = self.agents.current(name)
            if existing is not None and not reuse:
                raise DaemonRequestError(
                    ipc_errors.AGENT_EXISTS,
                    f"the name {name!r} is already taken on this node "
                    f"({self.owner}@{self.node_id}) — {existing.uri} "
                    f"exists. One name is one agent, whether or not anything "
                    f"is currently running under it. {name_taken_remedy(name)}; "
                    f"to run this agent on a different "
                    f"harness, just start it there — that is a rebinding of "
                    f"the same agent, not a new one.",
                    {"actor": existing.uri, "agent": name},
                )
            harness = params.get("harness")
            harness_name = harness if isinstance(harness, str) and harness else None
            requested_cwd = _optional_string_param(params.get("cwd"), "cwd")
            effective_cwd = requested_cwd
            if effective_cwd is None and (existing is None or harness_name is not None):
                effective_cwd = self._agent_session_domains.agent.resolve_workspace_path(name)
            if existing is None:
                config = params.get("config")
                if config is None:
                    workspace = self._agent_session_domains.agent.resolve_workspace_path(name)
                    config = AgentConfig(str(Path(workspace).parent / "config")).to_json()
                agent = self.agents.create(
                    name,
                    cwd=effective_cwd,
                    config=config,
                    # Model vendor, the same word squire uses.
                    provider=_optional_string_param(
                        params.get("provider"), "provider"
                    ),
                    model=_optional_string_param(params.get("model"), "model"),
                    capabilities=normalize_capabilities(params.get("capabilities")),
                    harness_args=normalize_harness_args(params.get("harnessArgs")),
                    preferred_harness=_optional_string_param(
                        params.get("preferredHarness"), "preferredHarness"
                    )
                    or harness_name,
                )
                routine_warning = self._finish_resident_agent_creation(agent)
                self._declare_persona_route(agent.uri)
            else:
                agent = existing
                routine_warning = None
            if existing is None and requested_cwd is None:
                self._agent_session_domains.agent.prepare_workspace(agent.actor)
            # A harness-bound create refuses a served agent before spawning a
            # TUI and otherwise returns A9 handover for the first turn.
            handover: HandoverNotice | None = None
            resume_before: Agent | None = None
            if harness_name is not None:
                with self._interactive_route_effect(agent.uri):
                    self._refuse_if_running(
                        agent.uri,
                        harness=harness_name,
                        runtime=str(params.get("runtime") or RUNTIME_HEADLESS),
                    )
                    if (
                        params.get("includeResumeSnapshot") is True
                        and existing is not None
                    ):
                        resume_before = self.agents.current(agent.uri)
                    agent = self._ensure_agent(
                        agent.uri, harness=harness_name,
                        interactive=params.get("runtime") == RUNTIME_INTERACTIVE,
                        cwd=effective_cwd,
                        provider=_optional_string_param(
                            params.get("provider"), "provider"
                        ),
                        model=_optional_string_param(params.get("model"), "model"),
                    )
                    if params.get("includeResumeSnapshot") is True:
                        assert agent is not None
                        self._resume_agent_restore_guards[agent.uri] = (
                            resume_before,
                            agent,
                        )
                assert agent is not None
                if harness_name == "pi" and effective_cwd == (
                    self._agent_session_domains.agent.resolve_workspace_path(agent.actor)
                ):
                    self._agent_session_domains.agent.prepare_workspace(agent.actor)
            if (
                harness_name is not None
                and agent.last_harness is not None
                and agent.last_harness != harness_name
            ):
                handover = HandoverNotice(
                    actor=agent.uri,
                    previous_harness=agent.last_harness,
                    previous_session_id=agent.last_session_id,
                    next_harness=harness_name,
                )
            return {
                "ok": True,
                "created": existing is None,
                "agent": self._agent_status_json(agent),
                **(
                    {
                        "resumePreviousSnapshot": (
                            None if resume_before is None else resume_before.to_json()
                        ),
                        "resumeSnapshot": agent.to_json(),
                    }
                    if params.get("includeResumeSnapshot") is True
                    else {}
                ),
                **(
                    {"harnessHandover": handover.to_json()}
                    if handover is not None
                    else {}
                ),
                **(
                    {"routineWarning": routine_warning}
                    if routine_warning is not None
                    else {}
                ),
            }
        if method == "agent.list":
            # Same per-request snapshot as ps: agent.list is the same loop
            # over agents[], so without it the storm only moves house.
            inactive_since = params.get("inactiveSince")
            inactivity_filter: tuple[int, int] | None = None
            if inactive_since is not None:
                try:
                    seconds = parse_duration(inactive_since, "inactiveSince")
                except DurationParseError as error:
                    raise DaemonRequestError(
                        ipc_errors.INVALID_ARGUMENT, str(error)
                    ) from error
                inactivity_filter = (
                    self._agent_registry.now_ms(),
                    int(seconds * 1_000),
                )
            with self._worker_status_snapshot():
                from hyprial.daemon.impl.user_agent.naming import is_user_agent

                agents = []
                visible_actors: list[str] = []
                user_agents: set[str] = set()
                for agent in self.agents.list():
                    if is_user_agent(agent):
                        if params.get("includeUserAgent") is not True:
                            continue
                        user_agents.add(agent.uri)
                    if params.get("excludeWf") is True and agent.actor.startswith("wf-"):
                        continue
                    hints = self._agent_session_domains.agent.read_activity_hints(
                        agent.actor
                    ).to_payload()
                    if inactivity_filter is not None and (
                        agent.uri in user_agents
                        or not self._agent_is_inactive(
                            agent,
                            now_ms=inactivity_filter[0],
                            threshold_ms=inactivity_filter[1],
                        )
                    ):
                        continue
                    agents.append(self._agent_status_json(agent, activity_hints=hints))
                    visible_actors.append(agent.uri)
                bound_actors = {
                    self._routine_actor_key(binding)
                    for routine in (
                        self._routine_service.list()["routines"]
                        if self._routine_service is not None
                        else []
                    )
                    if isinstance(routine, dict)
                    and (binding := self._routine_binding(routine)) is not None
                }
                return {
                    "ok": True,
                    "agents": agents,
                    "agentsWithoutRoutine": [
                        actor
                        for actor in visible_actors
                        if self._routine_actor_key(actor) not in bound_actors
                        and actor not in user_agents
                    ],
                }
        if method == "agent.ensure-home":
            # The registry's two-phase provision is the one official path
            # for giving an existing agent a P2 home: claim receipt, inactive
            # lifecycle row, ``provision_claimed`` filesystem effect (0700
            # tree, 0600 receipt mirror), then activation -- with compensating
            # rollback on failure.  Idempotent: an active ready row is
            # validated and returned unchanged.  Legacy agents otherwise have
            # no official rebuild path once a restart takes the P2 branch
            # (`not-provisioned` on every read).
            name = self.agents.actor_key(
                _required_string(params.get("name"), "name")
            )
            receipt = self._agent_registry.ensure_home(name)
            agent = self.agents.require(name)
            return {
                "ok": True,
                "actor": name,
                "agent": agent.uri,
                "home": receipt.to_json(),
            }
        if method == "agent.home-census":
            from collections import Counter

            from hyprial.daemon.impl.user_agent.runtime import census_agents

            from hyprial.identity import agent_home_mode

            def inside_home(path: str | None, home: Path) -> bool:
                if path is None:
                    return False
                try:
                    return Path(path).expanduser().resolve(strict=False).is_relative_to(
                        home.resolve(strict=False)
                    )
                except OSError:
                    return False

            rows: list[JsonObject] = []
            counts: Counter[tuple[str, str]] = Counter()
            legacy_home_root = (
                self._agent_registry.home_provisioner.agents_root
                if self._agent_registry.home_enabled
                else self.hyprial_home / "agents"
            )
            unprovisioned = 0
            for agent in census_agents(self.agents.list(), params.get("actor")):
                # The census measures the legacy population too: a missing
                # home row is a finding (``homeProvisioned: false``), not a
                # reason to fail the whole read (the pre-fix command died on
                # the first unprovisioned agent with ``not-provisioned``).
                receipt = None
                try:
                    receipt = self._agent_registry.home_receipt(
                        agent.actor, require_ready=False
                    )
                except AgentHomeError as error:
                    if error.category != "not-provisioned":
                        raise
                    unprovisioned += 1
                home = (
                    Path(receipt.path) if receipt is not None
                    else legacy_home_root / agent.actor
                )
                harness = agent.preferred_harness or agent.last_harness
                mode, reason = agent_home_mode(agent, harness)
                display_harness = harness or "unknown"
                counts[(mode, display_harness)] += 1
                rows.append(
                    {
                        "actor": agent.actor,
                        "uri": agent.uri,
                        "preferredHarness": agent.preferred_harness,
                        "lastHarness": agent.last_harness,
                        "harness": harness,
                        "homeProvisioned": receipt is not None,
                        "explicitConfig": agent.config is not None,
                        "configInsideHome": (
                            False
                            if agent.config is None
                            else inside_home(agent.config.source, home)
                        ),
                        "cwdInsideHome": inside_home(agent.cwd, home),
                        "residentOrDsh": False,
                        "transferReceived": agent.hosted_by == "transfer-receive",
                        "mode": mode,
                        "legacyReason": reason,
                    }
                )
            return {
                "ok": True,
                "readAtMs": self._agent_registry.now_ms(),
                "registry": {
                    "path": str(self._agent_registry.database),
                    "version": self._agent_session_domains.agent.version,
                },
                "agents": rows,
                "totals": {
                    "agents": len(rows),
                    "byModeHarness": [
                        {"mode": mode, "harness": harness, "count": count}
                        for (mode, harness), count in sorted(counts.items())
                    ],
                    "homeUnprovisioned": unprovisioned,
                },
            }
        if method == "agent.readiness":
            return self._agent_readiness(params)
        if method in {"agent.restore-policy", "agent.restore-threshold"}:
            return handle_restore_policy(
                method,
                params,
                authority=self._restore_policy,
                normalize_actor=self.agents.normalize_actor,
                submit_wake=self._submit_restore_wake,
            )
        if method == "agent.unblock":
            name = self.agents.actor_key(
                _required_string(params.get("name"), "name")
            )
            self.agents.require(name)
            changed = self.agents.unblock_agent(name)
            if changed:
                self.agents.record_activity(name)
            return {"actor": name, "changed": changed}
        if method == "agent.runtime-context":
            # Non-secret interactive-launch handoff.  The daemon remains the
            # sole root/profile resolver; this projection deliberately omits
            # entity tokens, grant values, and credential material (T16).
            name = self.agents.actor_key(
                _required_string(params.get("name"), "name")
            )
            harness = _required_string(params.get("harness"), "harness")
            cwd = _optional_string_param(params.get("cwd"), "cwd")
            model_vendor = _optional_string_param(
                params.get("modelProvider"), "modelProvider"
            )
            credential_preflight = params.get("credentialPreflight", False)
            if not isinstance(credential_preflight, bool):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "credentialPreflight must be a boolean",
                )
            from hyprial.identity import AgentRuntimeError
            from hyprial.identity import AgentConfigError

            try:
                context = self._agent_session_domains.agent.prepare_runtime_context(
                    name,
                    harness,
                    cwd,
                    None,
                    containerized=False,
                    credential_preflight=(
                        credential_preflight
                        and harness == "codex"
                        and model_vendor in {None, "openai"}
                    ),
                    legacy_reporter=self._report_legacy_agent_home,
                )
            except (
                AgentError,
                AgentRuntimeError,
                AgentConfigError,
                AgentHomeError,
            ) as error:
                raise DaemonRequestError(
                    getattr(error, "code", None) or ipc_errors.INVALID_ARGUMENT,
                    str(error),
                    getattr(error, "data", None),
                ) from error
            if context is None:
                return {"ok": True, "mode": "legacy", "environment": {}}
            self._report_agent_runtime_notices(context)
            return {"ok": True, **context.public_projection()}
        if method == "agent.runtime-launch.acquire":
            launch_token = _required_string(
                params.get("launchToken"), "launchToken"
            )
            operation_id = _optional_string_param(
                params.get("operationId"), "operationId"
            ) or f"{_AGENT_OPERATION_NAMESPACE}:runtime-launch:acquire:{uuid4().hex}"
            lease, secret_environment = self._acquire_agent_runtime_launch(
                launch_token, operation_id=operation_id
            )
            return {
                "ok": True,
                "operationId": operation_id,
                "actor": lease.actor,
                "leaseToken": lease.lease_token,
                "expiresAtMs": lease.expires_at_ms,
                "secretEnvironment": secret_environment,
                "secretGrants": [list(item) for item in lease.secret_grants],
            }
        if method == "agent.runtime-launch.custody":
            # Recovery view for an IPC caller that disconnected after the
            # Agent accepted acquire but before its response was delivered.
            # Custody is never expired here: the exact lease token remains
            # visible for explicit compensation.
            operation_id = _optional_string_param(
                params.get("operationId"), "operationId"
            )
            entries = self._agent_session_domains.agent.read_runtime_launch_custody()
            return {
                "ok": True,
                "custody": [
                    {
                        "operationId": item.operation_id,
                        "actor": item.actor,
                        "leaseToken": item.lease_token,
                    }
                    for item in entries
                    if operation_id is None or item.operation_id == operation_id
                ],
            }
        if method == "agent.runtime-launch.release":
            lease_token = _required_string(
                params.get("leaseToken"), "leaseToken"
            )
            operation_id = _required_string(
                params.get("operationId"), "operationId"
            )
            released = self._release_agent_runtime_launch(
                lease_token, operation_id=operation_id
            )
            return {
                "ok": True,
                "actor": released.actor,
                "leaseToken": released.lease_token,
                "released": not released.acquired,
            }
        if method == "agent.get":
            name = self.agents.actor_key(
                _required_string(params.get("name"), "name")
            )
            with self._worker_status_snapshot():
                return {
                    "ok": True,
                    "agent": self._agent_status_json(self.agents.require(name)),
                }
        if method == "agent.resolve":
            # Read-only view of the message.send targeting pipeline, so the
            # CLI can validate a bare --from at command time without
            # reimplementing (and drifting from) the delivery boundary's
            # rules.  Ambiguity propagates as AMBIGUOUS_TARGET; an unknown
            # bare name is not an error here, it is reported as known=False
            # so the caller decides (durable-queue semantics stay legal).
            name = _required_string(params.get("name"), "name")
            normalized = normalize_agent_recipient(name)
            resolved = self._resolve_agent_alias(normalized)
            reason = "unknown"
            if resolved != normalized:
                reason = "alias"
            elif normalized == self.node_id:
                reason = "node"
            elif ":" in normalized:
                reason = "scheme"
            return {
                "ok": True,
                "input": name,
                "resolved": resolved,
                "known": reason != "unknown",
                "reason": reason,
            }
        if method == "pac.gc.preview":
            if self._pac_gc is None:
                raise DaemonRequestError(
                    "PAC_GC_UNAVAILABLE", "PAC GC service is unavailable"
                )
            return self._pac_gc.preview()
        raise DaemonRequestError(ipc_errors.METHOD_NOT_FOUND, f"unknown daemon method {method}")

    def _acquire_agent_runtime_launch(
        self,
        launch_token: str,
        *,
        operation_id: str | None = None,
        resolve_secrets: bool = True,
    ) -> Any:
        from hyprial.identity import (
            AcquireAgentRuntimeLaunchCommand,
            AgentRuntimeLaunchLeaseCompleted,
        )

        event = self._agent_session_domains.call_agent_settled(
            AcquireAgentRuntimeLaunchCommand(
                operation_id or f"{_AGENT_OPERATION_NAMESPACE}:runtime-launch:acquire:{uuid4().hex}",
                launch_token,
            ),
            AgentRuntimeLaunchLeaseCompleted,
        )
        if not event.acquired or not event.lease_token:
            raise DomainCommandError(
                "AGENT_RUNTIME_LAUNCH_NOT_ACQUIRED",
                "Agent runtime launch lease was not acquired",
            )
        if not resolve_secrets:
            return event, {}
        from .provider_credential import materialize_agent_secret_environment

        try:
            environment = materialize_agent_secret_environment(
                registry=self._agent_registry,
                actor=event.actor,
            )
        except BaseException as error:
            # Any failure after acquire releases the lease first: a lease left
            # held has no expiry, blocks destroy and fills the launch capacity.
            try:
                self._release_agent_runtime_launch(
                    event.lease_token,
                    operation_id=event.correlation_id,
                )
            except Exception as release_error:  # noqa: BLE001 - retain coded refusal
                self._log(
                    "error",
                    "agents",
                    "agent.runtime_launch.secret_resolution_compensation_failed",
                    actor=event.actor,
                    errorType=type(release_error).__name__,
                )
            if not isinstance(error, Exception):
                raise
            # Error details could contain plaintext or a private path. Report
            # only the type at this process boundary.
            raise DomainCommandError(
                ipc_errors.AGENT_SECRET_RESOLUTION_FAILED,
                f"agent secret resolution failed: {type(error).__name__}",
            ) from error
        if environment:
            self._log(
                "info",
                "agents",
                "agent.runtime_launch.secrets_delivered",
                actor=event.actor,
                names=sorted(environment),
                grantCount=len(event.secret_grants),
            )
        return event, environment

    def _release_agent_runtime_launch(
        self, lease_token: str, *, operation_id: str
    ) -> Any:
        from hyprial.identity import (
            AgentRuntimeLaunchLeaseCompleted,
            ReleaseAgentRuntimeLaunchCommand,
        )

        return self._agent_session_domains.call_agent_settled(
            ReleaseAgentRuntimeLaunchCommand(
                f"{_AGENT_OPERATION_NAMESPACE}:runtime-launch:release:{uuid4().hex}",
                lease_token,
                operation_id,
            ),
            AgentRuntimeLaunchLeaseCompleted,
        )

    @contextlib.contextmanager
    def _agent_runtime_launch_custody(self, context: Any) -> Iterator[None]:
        if not getattr(context, "authority_prepared", False):
            yield
            return
        launch_token = getattr(context, "launch_token", None)
        if not isinstance(launch_token, str) or not launch_token:
            raise DomainCommandError(
                ipc_errors.AGENT_RUNTIME_CONTEXT_STALE,
                "authority-prepared runtime context has no launch token",
            )
        lease, _secret_environment = self._acquire_agent_runtime_launch(
            launch_token, resolve_secrets=False
        )
        try:
            yield
        finally:
            try:
                self._release_agent_runtime_launch(
                    lease.lease_token,
                    operation_id=lease.correlation_id,
                )
            except Exception as error:  # noqa: BLE001 - custody remains visible
                self._log(
                    "error",
                    "agents",
                    "agent.runtime_launch.release_failed",
                    actor=lease.actor,
                    errorType=type(error).__name__,
                    detail=str(error),
                )
