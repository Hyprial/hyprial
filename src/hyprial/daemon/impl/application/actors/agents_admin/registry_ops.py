"""Agent administration: registry verbs, migration, destroy/stop and runtime-launch custody."""

from __future__ import annotations

from __future__ import annotations
import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TYPE_CHECKING
from uuid import uuid4
from hyprial.identity import (
    RUNTIME_HEADLESS,
    RUNTIME_INTERACTIVE,
    AgentConfig,
    AgentError,
    HandoverNotice,
    normalize_capabilities,
    normalize_harness_args,
)
from hyprial.identity import AgentKeepListError
from hyprial.identity import (
    RestorePolicyError,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.kernel import DurationParseError, parse_duration
from hyprial.daemon.impl.composition  import (
    DomainCommandError,
)
from hyprial.daemon.impl.configuration.identity import (
    normalize_agent_recipient,
)
from hyprial.kernel import (
    canonical_agent_uri,
    canonical_user_uri,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _optional_positive_integer,
    _optional_string_param,
    _required_string,
)


_AGENT_OPERATION_NAMESPACE = "agent"

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

    def _handle_agent(self, method: str, params: JsonObject) -> Any:
        if method.startswith("agent.migrate."):
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
            return {"ok": True, "entryId": entry_id, "fieldNames": [field_name]}
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
                home_token = (
                    self._agent_session_domains.agent.prevalidate_home(actor).entity_token
                    if source is SecretSource.AGENT_PRIVATE else None
                )
                generation = self._agent_session_domains.agent.generation
                event = self._agent_session_domains.call_agent(
                    GrantAgentSecretCommand(
                        f"{_AGENT_OPERATION_NAMESPACE}:secret-grant:{uuid4().hex}", actor, grant_id,
                        source, entry_id, field_name, tuple(raw_names), revision,
                        home_token,
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
                },
            }
        if method == "agent.secret.list":
            actor = params.get("actor")
            if actor is not None and (not isinstance(actor, str) or not actor):
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "actor must be a non-empty string")
            grants = self._agent_session_domains.agent.read_secret_inventory(actor)
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
        if method == "agent.host-invite":
            # Host-controlled creation is separate from ordinary create/start:
            # neither a URI nor a caller-supplied flag confers hosting authority.
            agent = self.agents.create_host_invited(
                _required_string(params.get("name"), "name"),
                pinned_owner=_required_string(params.get("owner"), "owner"),
                cwd=_optional_string_param(params.get("cwd"), "cwd"),
                preferred_harness=_optional_string_param(
                    params.get("preferredHarness"), "preferredHarness"
                ),
            )
            routine_warning = self._finish_resident_agent_creation(agent)
            self._declare_persona_route(agent.uri)
            return {
                "ok": True,
                "created": True,
                "agent": self._agent_status_json(agent),
                **(
                    {"routineWarning": routine_warning}
                    if routine_warning is not None
                    else {}
                ),
            }
        if method == "agent.create":
            # Decision A5: the one creation path. `hyprial agent create` calls it
            # directly; `hyprial start` calls it first and only then launches a
            # harness, so a connector can never exist without an identity.
            name = self.agents.native_actor(
                _required_string(params.get("name"), "name")
            )
            reuse = params.get("existing") == "reuse"
            existing = self.agents.get(name)
            if existing is not None and not reuse:
                raise DaemonRequestError(
                    ipc_errors.AGENT_EXISTS,
                    f"the name {name!r} is already taken on this node "
                    f"({self.owner}@{self.node_id}) — {existing.uri} "
                    f"exists. One name is one agent, whether or not anything "
                    f"is currently running under it. To reuse the name, "
                    f"destroy that agent first ('hyprial agent destroy {name}', "
                    f"which is irreversible); to run this agent on a different "
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
                # A legacy pin naming this agent could not migrate while the
                # record did not exist; it can now.
                self._migrate_legacy_channel_pins()
                self._declare_persona_route(agent.uri)
            else:
                agent = existing
                routine_warning = None
            # When the caller says which harness it is about to launch, refuse
            # here if the agent is already being served -- before a TUI has
            # been spawned -- and otherwise hand back the A9 handover notice so
            # it can be put in front of the agent's first turn.
            handover: HandoverNotice | None = None
            if harness_name is not None:
                self._refuse_if_running(
                    agent.uri,
                    harness=harness_name,
                    runtime=str(params.get("runtime") or RUNTIME_HEADLESS),
                )
                agent = self._ensure_agent(
                    agent.uri, harness=harness_name,
                    interactive=params.get("runtime") == RUNTIME_INTERACTIVE,
                    cwd=effective_cwd,
                    provider=_optional_string_param(params.get("provider"), "provider"),
                    model=_optional_string_param(params.get("model"), "model"),
                )
                assert agent is not None
                if effective_cwd == self._agent_session_domains.agent.resolve_workspace_path(
                    agent.actor
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
            cutoff_ms: int | None = None
            if inactive_since is not None:
                try:
                    seconds = parse_duration(inactive_since, "inactiveSince")
                except DurationParseError as error:
                    raise DaemonRequestError(
                        ipc_errors.INVALID_ARGUMENT, str(error)
                    ) from error
                cutoff_ms = self._agent_registry.now_ms() - int(seconds * 1_000)
            try:
                kept = frozenset(self._agent_keep.list())
            except AgentKeepListError as error:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT, str(error)
                ) from error
            with self._worker_status_snapshot():
                agents = []
                visible_actors: list[str] = []
                for agent in self.agents.list():
                    if params.get("excludeWf") is True and agent.actor.startswith("wf-"):
                        continue
                    hints = self._agent_session_domains.agent.read_activity_hints(
                        agent.actor
                    ).to_payload()
                    if cutoff_ms is not None and not self._agent_is_inactive(
                        agent, hints, cutoff_ms=cutoff_ms, kept=kept
                    ):
                        continue
                    agents.append(self._agent_status_json(agent, activity_hints=hints))
                    visible_actors.append(agent.uri)
                bound_actors = {
                    binding
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
                        actor for actor in visible_actors if actor not in bound_actors
                    ],
                }
        if method == "agent.home-census":
            from collections import Counter

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
            for agent in self.agents.list():
                receipt = self._agent_registry.home_receipt(
                    agent.actor, require_ready=False
                )
                home = Path(receipt.path)
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
                        "explicitConfig": agent.config is not None,
                        "configInsideHome": (
                            False
                            if agent.config is None
                            else inside_home(agent.config.source, home)
                        ),
                        "cwdInsideHome": inside_home(agent.cwd, home),
                        # The durable registry intentionally has no resident
                        # column while G-R is unresolved. DSH is the only
                        # honest registry-derived member of this guard class.
                        "residentOrDsh": harness == "dsh",
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
                },
            }
        if method == "agent.keep.list":
            return {"ok": True, "agents": list(self._agent_keep.list())}
        if method in {"agent.keep.add", "agent.keep.remove"}:
            name = self.agents.normalize_actor(
                _required_string(params.get("name"), "name")
            )
            changed = (
                self._agent_keep.add(name)
                if method == "agent.keep.add"
                else self._agent_keep.remove(name)
            )
            if method == "agent.keep.add":
                self._submit_restore_wake(name, reason="keep-list")
            return {
                "ok": True,
                "agent": name,
                "changed": changed,
                "agents": list(self._agent_keep.list()),
            }
        if method == "agent.restore-policy":
            name = self.agents.normalize_actor(
                _required_string(params.get("name"), "name")
            )
            policy_name = _required_string(params.get("policy"), "policy")
            try:
                policy = self._restore_policy.set_agent(name, policy_name)
            except RestorePolicyError as error:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT, str(error)
                ) from error
            if policy_name == "always":
                self._submit_restore_wake(name, reason="per-agent-always")
            return {
                "ok": True,
                "actor": name,
                "policy": policy.policy_for(name),
                "restoreThresholdMs": policy.threshold_ms,
            }
        if method == "agent.restore-threshold":
            threshold_ms = params.get("thresholdMs")
            if (
                not isinstance(threshold_ms, int)
                or isinstance(threshold_ms, bool)
                or threshold_ms < 0
            ):
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "thresholdMs must be a non-negative integer",
                )
            policy = self._restore_policy.set_threshold(threshold_ms)
            return {
                "ok": True,
                "restoreThresholdMs": policy.threshold_ms,
            }
        if method == "agent.unblock":
            name = self.agents.normalize_actor(
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
            name = self.agents.normalize_actor(
                _required_string(params.get("name"), "name")
            )
            harness = _required_string(params.get("harness"), "harness")
            cwd = _optional_string_param(params.get("cwd"), "cwd")
            from hyprial.identity import (
                DEFAULT_AGENT_TOOL_PROFILE,
                AgentRuntimeError,
            )
            from hyprial.identity import AgentConfigError
            from hyprial.identity import AgentHomeError

            try:
                context = self._agent_session_domains.agent.prepare_runtime_context(
                    name,
                    harness,
                    cwd,
                    DEFAULT_AGENT_TOOL_PROFILE,
                    containerized=False,
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
                ) from error
            if context is None:
                return {"ok": True, "mode": "legacy", "environment": {}}
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
            name = self.agents.normalize_actor(
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
        if method == "agent.destroy.preview":
            requested = _required_string(params.get("name"), "name")
            agent = self.agents.require(requested)
            return {
                "ok": True,
                "actor": agent.uri,
                "agent": agent.actor,
                "workspace": self._agent_session_domains.agent.read_workspace_summary(
                    agent.actor
                ).to_json(),
            }
        if method == "pac.gc.preview":
            if self._pac_gc is None:
                raise DaemonRequestError(
                    "PAC_GC_UNAVAILABLE", "PAC GC service is unavailable"
                )
            return self._pac_gc.preview()
        if method == "agent.destroy":
            # Resolve the exact identity before dropping URI ownership. Hosted
            # URIs are valid; unrelated same-name foreign URIs are not.
            requested = _required_string(params.get("name"), "name")
            agent = self.agents.get(requested)
            if agent is None:
                from hyprial.identity import (
                    AgentMutationCompleted,
                    CleanupRevokedAgentHomeCommand,
                )

                generation = self._agent_session_domains.agent.generation
                cleaned = self._agent_session_domains.call_agent(
                    CleanupRevokedAgentHomeCommand(
                        f"{_AGENT_OPERATION_NAMESPACE}:cleanup-home:{uuid4().hex}", requested
                    ),
                    AgentMutationCompleted,
                )
                if (
                    cleaned.generation != generation
                    or cleaned.operation != "cleanup-home"
                ):
                    raise DomainCommandError(
                        "STALE_AGENT_HOME_CLEANUP",
                        "agent home cleanup completion fence mismatch",
                    )
                if not cleaned.changed:
                    self.agents.require(requested)
                    raise AssertionError("require() returned for a missing agent")
                cleaned_actor = self.agents.normalize_actor(requested)
                actor = canonical_agent_uri(
                    self.owner, self.node_id, cleaned_actor
                )
                self._log(
                    "warn",
                    "agents",
                    "agent.destroyed",
                    actor=actor,
                    stopped=[],
                    destroyedMessages=0,
                    unpinnedAdapters=[],
                    cleanupResumed=True,
                )
                return {
                    "ok": True,
                    "destroyed": False,
                    "cleanupResumed": True,
                    "actor": actor,
                    "agent": cleaned_actor,
                    "stopped": [],
                    "destroyedMessages": 0,
                    "unpinnedAdapters": [],
                    "irreversible": True,
                }
            return self._destroy_agent(agent.actor)
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
        from hyprial.identity import SecretResolutionError, resolve_agent_secrets

        try:
            resolved = resolve_agent_secrets(
                registry=self._agent_registry,
                hyprial_home=self.hyprial_home,
                agent_name=event.actor,
                expected_grants=event.secret_grants,
            )
            environment: dict[str, str] = {}
            for secret in resolved:
                for name, value in secret.environment().items():
                    if name in environment:
                        raise SecretResolutionError(
                            "environment-conflict",
                            secret.grant.entry_id,
                            "compose",
                        )
                    environment[name] = value
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
            # Only the resolver's own messages are sanitized; anything else is
            # reported by type so no detail of a secret can leak into it.
            detail = (
                str(error)
                if isinstance(error, SecretResolutionError)
                else f"agent secret resolution failed: {type(error).__name__}"
            )
            raise DomainCommandError(
                ipc_errors.AGENT_SECRET_RESOLUTION_FAILED, detail
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
                "AGENT_RUNTIME_CONTEXT_STALE",
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

    def _host_invited_owner(self, name: str) -> str | None:
        """Read the admitted visitor owner; transfer-receive grants no start authority."""
        agent = self.agents.get(self.agents.uri_for(name))
        return agent.owner if agent is not None and agent.hosted_by == "host-invite" else None
