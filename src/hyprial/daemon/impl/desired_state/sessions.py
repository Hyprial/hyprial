"""DesiredStateStore session/adapter cluster: interactive claims, agent effects, channel pins and adapter registrations."""

from __future__ import annotations

from hyprial.daemon.impl.desired_state.documents import (
    DesiredState,
    InteractiveSession,
    PendingSessionAgentEffect,
    ZenohEndpoints,
    _apply_create_resource,
    _apply_delete_resource,
    _new_stored_receipt,
    _reconcile_resource,
    _resource_map,
    _stored_receipt,
    _with_external_resource,
    _merge_session_effects,
)


from collections.abc import Mapping
from dataclasses import replace
from hyprial.kernel  import (
    MutationProvenance,
)
from hyprial.kernel import LifecycleMutationRequest
from hyprial.kernel import DesiredStateError, HarnessLaunchSpec  # canonical defs (MP-1)

class _DesiredStateSessionsMixin:
    """DesiredStateStore cluster; the composing class owns the state."""

    def apply_session_lifecycle(
        self, request: LifecycleMutationRequest
    ) -> tuple[MutationProvenance, tuple[str, ...]]:
        """Commit a Session mutation and its token receipt in one file replace."""

        from hyprial.daemon.impl.operations.session_ports  import RegisterSessionCommand, UnregisterSessionCommand

        payload = request.payload
        if not isinstance(payload, (RegisterSessionCommand, UnregisterSessionCommand)):
            raise TypeError(f"unsupported Session lifecycle payload: {type(payload).__name__}")
        key = f"session:{payload.actor}"
        with self._lock:
            state = self.load()
            replay = _stored_receipt(state, "session", request, key)
            if replay is not None:
                return replay.provenance, ()
            sessions = {item.actor: item for item in state.interactive_sessions}
            current = sessions.get(payload.actor)
            resources = _resource_map(state)
            resource = _reconcile_resource(
                resources.get(("session", key)),
                "session",
                key,
                current is not None,
                {} if current is None else current.to_json(),
            )
            superseded: tuple[str, ...] = ()
            if isinstance(payload, RegisterSessionCommand):
                desired = InteractiveSession(
                    actor=payload.actor,
                    cwd=payload.cwd,
                    command=payload.command,
                    source=payload.source,
                    session_ref=payload.session_ref,
                    runtime=payload.runtime,
                    channel_confirmed=payload.channel_confirmed,
                    channel_build_version=payload.channel_build_version,
                    channel_protocol_version=payload.channel_protocol_version,
                    owner_fence=payload.owner_fence,
                    tmux_session=payload.tmux_session,
                    process_pid=payload.process_pid,
                    process_identity=payload.process_identity,
                )
                changed, created, resource = _apply_create_resource(
                    resource,
                    request.expected_resource_token,
                    desired.to_json(),
                )
                if changed:
                    superseded = tuple(
                        sorted(
                            item.actor
                            for item in sessions.values()
                            if item.actor != desired.actor
                            and desired.session_ref is not None
                            and item.session_ref == desired.session_ref
                            and item.source == desired.source
                        )
                    )
                    for actor in superseded:
                        sessions.pop(actor, None)
                    sessions[desired.actor] = desired
            else:
                if current is not None and current.session_ref != payload.session_ref:
                    changed = False
                    created = False
                else:
                    changed, created, resource = _apply_delete_resource(
                        resource,
                        request.expected_resource_token,
                        {} if current is None else current.to_json(),
                    )
                    if changed:
                        sessions.pop(payload.actor, None)
            resources[("session", key)] = resource
            provenance = MutationProvenance(created, changed, resource.resource_token)
            receipt = _new_stored_receipt("session", request, key, provenance)
            updated = replace(
                state,
                interactive_sessions=tuple(sessions[name] for name in sorted(sessions)),
                lifecycle_resources=tuple(resources[item] for item in sorted(resources)),
                lifecycle_receipts=(*state.lifecycle_receipts, receipt),
            )
            self.save(updated)
            return provenance, superseded

    def adapter_registration(
        self, name: str
    ) -> tuple[HarnessLaunchSpec | None, str | None]:
        """Read the desired Lark harness and staged legacy pin atomically."""

        with self._lock:
            state = self.load()
            spec = next(
                (
                    item
                    for item in state.harnesses
                    if (item.harness, item.name) == ("lark", name)
                ),
                None,
            )
            return spec, dict(state.channel_pins).get(name)

    def remove_adapter_registration(
        self,
        name: str,
        *,
        expected_spec: HarnessLaunchSpec | None,
        expected_legacy_pin: str | None,
    ) -> DesiredState:
        """Remove one adapter's desired rows behind an exact compare fence."""

        with self._lock:
            state = self.load()
            current_spec = next(
                (
                    item
                    for item in state.harnesses
                    if (item.harness, item.name) == ("lark", name)
                ),
                None,
            )
            pins = dict(state.channel_pins)
            current_pin = pins.get(name)
            if current_spec != expected_spec or current_pin != expected_legacy_pin:
                raise DesiredStateError(
                    f"adapter {name!r} registry changed during removal"
                )
            pins.pop(name, None)
            harnesses = tuple(
                item
                for item in state.harnesses
                if (item.harness, item.name) != ("lark", name)
            )
            if harnesses == state.harnesses and current_pin is None:
                return state
            updated = replace(
                state,
                harnesses=harnesses,
                channel_pins=tuple(sorted(pins.items())),
            )
            self.save(updated)
            return updated

    def restore_adapter_registration(
        self,
        name: str,
        *,
        spec: HarnessLaunchSpec | None,
        legacy_pin: str | None,
    ) -> DesiredState:
        """Rollback one removal without overwriting a concurrent replacement."""

        with self._lock:
            state = self.load()
            current_spec = next(
                (
                    item
                    for item in state.harnesses
                    if (item.harness, item.name) == ("lark", name)
                ),
                None,
            )
            pins = dict(state.channel_pins)
            current_pin = pins.get(name)
            if current_spec == spec and current_pin == legacy_pin:
                return state
            if current_spec is not None or current_pin is not None:
                raise DesiredStateError(
                    f"adapter {name!r} registry replacement blocks rollback"
                )
            harnesses = list(state.harnesses)
            if spec is not None:
                harnesses.append(spec)
            if legacy_pin is not None:
                pins[name] = legacy_pin
            updated = replace(
                state,
                harnesses=tuple(
                    sorted(harnesses, key=lambda item: (item.harness, item.name))
                ),
                channel_pins=tuple(sorted(pins.items())),
            )
            self.save(updated)
            return updated

    def sync_harness_session_refs(
        self, refs: Mapping[tuple[str, str], str]
    ) -> DesiredState:
        """Persist runtime-learned session refs, touching nothing else.

        Managed harnesses learn their native session/thread id only once the
        connector is up (a Codex thread id after ``thread/start``, a pi
        session id minted at launch, a Claude session id after connect).
        Writing the ref back is what lets a daemon restart resume the
        conversation instead of cold-starting every worker.

        This is deliberately NOT ``upsert_harness``: it rewrites only the
        ``sessionRef`` key of harnesses still present in desired state, so a
        concurrent operator edit (args/cwd via ``lifecycle.start``, which
        replaces the whole spec and thereby clears the ref) can never be
        clobbered by a stale runtime observation.  Harnesses absent from
        desired state are ignored -- a removed worker keeps no residue.
        """

        with self._lock:
            state = self.load()
            changed = False
            harnesses: list[HarnessLaunchSpec] = []
            for spec in state.harnesses:
                ref = refs.get((spec.harness, spec.name))
                if ref is not None and ref != spec.session_ref:
                    spec = replace(spec, session_ref=ref)
                    changed = True
                harnesses.append(spec)
            if not changed:
                return state
            updated = replace(state, harnesses=tuple(harnesses))
            self.save(updated)
            return updated

    def set_mailbox_role(self, enabled: bool) -> DesiredState:
        with self._lock:
            state = self.load()
            updated = replace(state, as_mailbox=enabled)
            self.save(updated)
            return updated

    def update_zenoh_endpoints(
        self,
        *,
        listen: tuple[str, ...] | None = None,
        connect: tuple[str, ...] | None = None,
    ) -> DesiredState:
        """Persist explicit Zenoh endpoints; None leaves that side unchanged.

        Pass an empty tuple to clear one side, for example after tearing down
        a two-machine mesh or to fall back to per-launch environment
        overrides only.
        """

        with self._lock:
            state = self.load()
            updated = replace(
                state,
                zenoh=ZenohEndpoints(
                    listen=state.zenoh.listen if listen is None else listen,
                    connect=state.zenoh.connect if connect is None else connect,
                ),
            )
            self.save(updated)
            return updated

    def register_interactive(self, session: InteractiveSession) -> DesiredState:
        state, _superseded = self.claim_interactive(session)
        return state

    def claim_interactive(
        self, session: InteractiveSession
    ) -> tuple[DesiredState, tuple[str, ...]]:
        return self.claim_interactive_with_effects(session, ())

    def claim_interactive_with_effects(
        self,
        session: InteractiveSession,
        effects: tuple[PendingSessionAgentEffect, ...],
    ) -> tuple[DesiredState, tuple[str, ...]]:
        """Atomically make ``session`` the owner of its actor and carrier id.

        The original ``register_interactive`` API remains the compatibility
        seam used by the pre-cutover daemon.  Actor-owned session mutation needs
        the displaced aliases as part of the *same* serialized state change so
        it can fence them without a load/register race.  A carrier session may
        move to another actor, and an actor may be taken over by a newer session;
        both remain the established last-writer-wins contract.
        """

        with self._lock:
            state = self.load()
            superseded = tuple(
                sorted(
                    existing.actor
                    for existing in state.interactive_sessions
                    if existing.actor != session.actor
                    and session.session_ref is not None
                    and existing.session_ref == session.session_ref
                    and existing.source == session.source
                )
            )
            sessions = {
                existing.actor: existing
                for existing in state.interactive_sessions
                if not (
                    session.session_ref is not None
                    and existing.session_ref == session.session_ref
                    and existing.source == session.source
                    and existing.actor != session.actor
                )
            }
            sessions[session.actor] = session
            updated = replace(
                state,
                interactive_sessions=tuple(
                    sessions[actor] for actor in sorted(sessions)
                ),
                pending_session_agent_effects=_merge_session_effects(
                    state.pending_session_agent_effects, effects
                ),
            )
            updated = _with_external_resource(
                updated,
                "session",
                f"session:{session.actor}",
                True,
                session.to_json(),
            )
            for actor in superseded:
                previous = next(
                    item for item in state.interactive_sessions if item.actor == actor
                )
                updated = _with_external_resource(
                    updated,
                    "session",
                    f"session:{actor}",
                    False,
                    previous.to_json(),
                )
            self.save(updated)
            return updated, superseded

    def claim_interactive_with_agent_effects(
        self,
        session: InteractiveSession,
        bind_effect: PendingSessionAgentEffect,
    ) -> tuple[
        DesiredState,
        tuple[str, ...],
        tuple[PendingSessionAgentEffect, ...],
    ]:
        """Atomically claim a session and durably stage every Agent effect."""

        if bind_effect.operation != "bind" or bind_effect.actor != session.actor:
            raise DesiredStateError(
                "interactive claim requires a bind effect for the claimed actor"
            )
        with self._lock:
            state = self.load()
            superseded = tuple(
                sorted(
                    existing.actor
                    for existing in state.interactive_sessions
                    if existing.actor != session.actor
                    and session.session_ref is not None
                    and existing.session_ref == session.session_ref
                    and existing.source == session.source
                )
            )
            effects = tuple(
                PendingSessionAgentEffect(
                    effect_id=f"{bind_effect.effect_id}:release:{index}",
                    correlation_id=bind_effect.correlation_id,
                    operation="release",
                    actor=actor,
                )
                for index, actor in enumerate(superseded)
            ) + (bind_effect,)
            sessions = {
                existing.actor: existing
                for existing in state.interactive_sessions
                if not (
                    session.session_ref is not None
                    and existing.session_ref == session.session_ref
                    and existing.source == session.source
                    and existing.actor != session.actor
                )
            }
            sessions[session.actor] = session
            updated = replace(
                state,
                interactive_sessions=tuple(
                    sessions[actor] for actor in sorted(sessions)
                ),
                pending_session_agent_effects=_merge_session_effects(
                    state.pending_session_agent_effects, effects
                ),
            )
            updated = _with_external_resource(
                updated,
                "session",
                f"session:{session.actor}",
                True,
                session.to_json(),
            )
            for actor in superseded:
                previous = next(
                    item for item in state.interactive_sessions if item.actor == actor
                )
                updated = _with_external_resource(
                    updated,
                    "session",
                    f"session:{actor}",
                    False,
                    previous.to_json(),
                )
            self.save(updated)
            return updated, superseded, effects

    def unregister_interactive(self, actor: str) -> DesiredState:
        with self._lock:
            state = self.load()
            current = next(
                (
                    session
                    for session in state.interactive_sessions
                    if session.actor == actor
                ),
                None,
            )
            updated = replace(
                state,
                interactive_sessions=tuple(
                    session
                    for session in state.interactive_sessions
                    if session.actor != actor
                ),
            )
            if current is not None:
                updated = _with_external_resource(
                    updated,
                    "session",
                    f"session:{actor}",
                    False,
                    current.to_json(),
                )
            self.save(updated)
            return updated

    def unregister_interactive_if_current(
        self,
        actor: str,
        session_ref: str,
        effects: tuple[PendingSessionAgentEffect, ...] = (),
    ) -> tuple[DesiredState, bool]:
        """Remove only the session generation that still owns ``actor``.

        A delayed ``finally`` from a superseded carrier must never unregister
        its successor.  Keeping the comparison and write under this store's
        lock preserves the old no-op response while closing that race for the
        actor-owned mutation path.
        """

        with self._lock:
            state = self.load()
            current = next(
                (
                    session
                    for session in state.interactive_sessions
                    if session.actor == actor
                ),
                None,
            )
            if current is None or current.session_ref != session_ref:
                return state, False
            updated = replace(
                state,
                interactive_sessions=tuple(
                    session
                    for session in state.interactive_sessions
                    if session.actor != actor
                ),
                pending_session_agent_effects=_merge_session_effects(
                    state.pending_session_agent_effects, effects
                ),
            )
            updated = _with_external_resource(
                updated,
                "session",
                f"session:{actor}",
                False,
                current.to_json(),
            )
            self.save(updated)
            return updated, True

    def record_session_agent_effects(
        self, effects: tuple[PendingSessionAgentEffect, ...]
    ) -> DesiredState:
        """Durably take custody of effects without changing session ownership."""

        if not effects:
            return self.load()
        with self._lock:
            state = self.load()
            merged = _merge_session_effects(state.pending_session_agent_effects, effects)
            updated = replace(state, pending_session_agent_effects=merged)
            if not self._sqlite_shadow.has_document():
                self.save(updated)  # first write of a fresh home creates the root
                return updated
            known = {effect.effect_id for effect in state.pending_session_agent_effects}
            # Label a refusal by the effect's place in the document it joins,
            # exactly as the whole-document write would have.
            position = {effect.effect_id: index for index, effect in enumerate(merged)}
            rows = []
            for effect in effects:
                if effect.effect_id in known:
                    continue
                document = effect.to_json()
                # The whole-document write validated every effect through
                # DesiredState.from_json; a row write must refuse the same
                # shapes, or one bad row makes every later load() raise.
                PendingSessionAgentEffect.from_json(
                    document,
                    f"pendingSessionAgentEffects[{position[effect.effect_id]}]",
                )
                rows.append(document)
            if not self._sqlite_shadow.insert_session_agent_effects(rows):
                self.save(updated)  # the root vanished: rewrite the document
            return updated

    def complete_session_agent_effect(self, effect_id: str) -> bool:
        """Retire an effect only after Agent reports its correlated result."""

        with self._lock:
            state = self.load()
            remaining = tuple(
                effect
                for effect in state.pending_session_agent_effects
                if effect.effect_id != effect_id
            )
            if len(remaining) == len(state.pending_session_agent_effects):
                return False
            self._sqlite_shadow.delete_session_agent_effect(effect_id)
            return True

    def set_channel_pin(
        self, channel: str, agent: str
    ) -> tuple[DesiredState, str | None]:
        with self._lock:
            state = self.load()
            pins = dict(state.channel_pins)
            previous = pins.get(channel)
            pins[channel] = agent
            updated = replace(state, channel_pins=tuple(sorted(pins.items())))
            self.save(updated)
            return updated, previous

    def remove_channel_pin(self, channel: str) -> tuple[DesiredState, str | None]:
        with self._lock:
            state = self.load()
            pins = dict(state.channel_pins)
            previous = pins.pop(channel, None)
            updated = replace(state, channel_pins=tuple(sorted(pins.items())))
            self.save(updated)
            return updated, previous

    def remove_matching_channel_pins(
        self, matches: tuple[tuple[str, str], ...]
    ) -> DesiredState:
        """Retire migrated pins without replacing another writer's document."""

        with self._lock:
            state = self.load()
            pins = dict(state.channel_pins)
            for channel, expected in matches:
                if pins.get(channel) == expected:
                    pins.pop(channel)
            updated = tuple(sorted(pins.items()))
            if updated == state.channel_pins:
                return state
            result = replace(state, channel_pins=updated)
            self.save(result)
            return result

    def normalize_interactive_sessions(
        self,
        *,
        rewrites: tuple[tuple[str, str | None, str], ...],
    ) -> DesiredState:
        """Apply fenced startup identity rewrites to the current document.

        Each source row is identified by actor and session ref, so a newer
        carrier cannot be moved by a stale normalization attempt.  A row
        already stored under the canonical actor wins a collision.
        """

        with self._lock:
            state = self.load()
            targets = {(actor, session_ref): canonical for actor, session_ref, canonical in rewrites}
            normalized: dict[str, InteractiveSession] = {}
            for session in state.interactive_sessions:
                canonical = targets.get((session.actor, session.session_ref), session.actor)
                candidate = (
                    session if canonical == session.actor
                    else replace(session, actor=canonical)
                )
                existing = normalized.get(canonical)
                if existing is None or session.actor == canonical:
                    normalized[canonical] = candidate
            sessions = tuple(normalized[actor] for actor in sorted(normalized))
            if sessions == state.interactive_sessions:
                return state
            result = replace(state, interactive_sessions=sessions)
            self.save(result)
            return result
