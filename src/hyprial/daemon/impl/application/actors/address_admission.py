# Decision record (UUID addressing §2.3, docs/design/uuid-addressing-implementation-2026-11-03.md):
# - Ruling U1 (no compatibility window): once id-form minting is on, a local
#   agent URI whose actor segment is a name of a live registered agent (its
#   actor or display name) is refused with AGENT_ADDRESS_RETIRED carrying
#   that agent's id address.  A name no live agent holds -- a destroyed
#   agent's name or a previous display name included -- is an ordinary
#   unregistered name (owner decision 2026-11-19: no name tombstone), so it
#   follows the unclaimed-name rule below; once a new agent takes the name,
#   the name URI is refused with the new agent's id address and never
#   delivered to it.
# - "Local" means this daemon's node with this daemon's owner, or an owner
#   with a live agent hosted on this node.  Once a hosted owner's last agent
#   is destroyed, nothing here records that owner, so its addresses --
#   the dead a.<id> included -- are foreign again and go to the forwarding
#   path, where they fail to resolve elsewhere (accepted 2026-11-19).
#   Foreign nodes and owners nothing here records stay outside.
# - An a.<id> segment with no live agent is TARGET_UNRESOLVED (fatal).
# - HQ question Q2 is still open: refusing a name that nothing on this node
#   claims changes the shipped message.send behaviour (it used to queue with
#   a TARGET_UNRESOLVED code).  That refusal lives alone in
#   _refuse_unclaimed_local_name so it can be dropped if HQ rules otherwise.
# - Daemon service actors (RESERVED_AGENT_ACTOR_NAMES, pac-expansion) have
#   no registry record and keep their name addresses; unregistered
#   streaming harnesses and interactive sessions stay claimed through the
#   same predicate the queue telemetry already used.  squire is not a
#   service actor (Allen 2026-11-06): it is an ordinary registered agent,
#   so its name address is retired like any other agent's.
# - The delivery side shares the claim: _local_recipient_claimed is injected
#   into the inbox and the local-first transport by wiring, so an unclaimed
#   self-node recipient ends in a terminal failure instead of ACK_RECEIVED
#   and a row nobody drains.  Every check is a no-op with the switch off.
# - A registry record claims only its stored (id) URI on the delivery side:
#   no agent reads rows at a name URI (the pre-flip read alias D4 is gone),
#   so a row in flight to a name URI -- a destroyed agent's included --
#   is unclaimed and fails terminally instead of reaching a new agent that
#   took the name.  Unregistered claimants keep their claim.
# - Locality is owner-aware on both sides (fix-pass review 1): the delivery
#   claim uses the same authority as _local_address, so a hosted agent's
#   owner on this node is judged too, and a registry row claims only under
#   its own owner.
# - The unregistered-claim check matches an interactive session by the URI
#   or the bare actor segment for this daemon's owner, as
#   _is_unresolved_local_identity does.  A hosted owner matches by the full
#   URI only: a stored bare session actor is canonicalized under this
#   daemon's own owner, so it never drains a hosted owner's address.
# - message.reply checks the stored sender the same way.  With the switch
#   off a reply to a self-node name nothing claims still queues: the
#   local-first transport commits it into the durable inbox and the read
#   side canonicalizes the same way.
# - Sessions (flip round 2): a plain first session.register for a local
#   name with no record creates the record before the per-actor route
#   coordination key is chosen, under a registration boundary keyed by the
#   name URI, so every registration of one agent serializes and fences
#   under its official (id) address and none commits at the name URI.
# - A session call (register, refresh, heartbeat, any session-fenced call)
#   at a local a.<id> with no live agent is refused with SESSION_SUPERSEDED,
#   the code a destroyed agent's retired session already gets: a carrier
#   goes quiet on it and never re-registers.  TARGET_UNRESOLVED stays the
#   send-boundary code; carriers treat it as transient and would retry.
"""Send-boundary and delivery-side admission of local agent addresses."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from hyprial.kernel import (
    RESERVED_AGENT_ACTOR_NAMES,
    DaemonRequestError,
    ipc_errors,
    is_agent_id_segment,
    parse_agent_uri,
)

SERVICE_ACTOR_NAMES = RESERVED_AGENT_ACTOR_NAMES | frozenset({"pac-expansion"})


def _switch_on() -> bool:
    from hyprial.identity import id_form_minting

    return id_form_minting()


class _AgentAddressAdmissionMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _local_address(self, target: str) -> tuple[str, str] | None:
        """``(owner, segment)`` when this node is the authority for ``target``."""

        parsed = parse_agent_uri(target)
        if parsed is None or parsed[1] != self.node_id:
            return None
        owner, _node, segment = parsed
        if owner != self.owner and not any(
            agent.owner == owner for agent in self.agents.list()
        ):
            return None
        return owner, segment

    def _live_agent_named(self, owner: str, segment: str, target: str) -> Any:
        """The live agent whose actor or display name is ``segment``."""

        if owner == self.owner:
            agent = self.agents.get(target)
            return agent if agent is not None and agent.owner == owner else None
        return next(
            (
                agent
                for agent in self.agents.list()
                if agent.owner == owner
                and agent.machine == self.node_id
                and segment in (agent.actor, agent.display_name)
            ),
            None,
        )

    def _registered_at(self, target: str) -> bool:
        """With the switch on, a live record stored at exactly ``target``."""

        if not _switch_on():
            return False
        try:
            agent = self.agents.get(target)
        except Exception:  # noqa: BLE001
            return False
        return agent is not None and agent.uri == target

    def _refuse_retired_local_address(self, target: str) -> None:
        """Refuse a local name address of a live registered agent."""

        if not _switch_on():
            return
        local = self._local_address(target)
        if local is None or is_agent_id_segment(local[1]):
            return
        owner, segment = local
        agent = self._live_agent_named(owner, segment, target)
        if agent is not None:
            if agent.uri == target:
                return
            raise DaemonRequestError(
                ipc_errors.AGENT_ADDRESS_RETIRED,
                f"agent address {target!r} is retired: agents are addressed "
                f"by id now; use {agent.uri!r}",
                {"target": target, "address": agent.uri, "agentId": agent.agent_id},
            )

    def _dead_local_agent_id(self, target: str) -> bool:
        """With the switch on, a local ``a.`` id address no live agent has."""

        if not _switch_on():
            return False
        local = self._local_address(target)
        return (
            local is not None
            and is_agent_id_segment(local[1])
            and self.agents.get(target) is None
        )

    def _refuse_unresolved_local_target(self, target: str) -> None:
        """Refuse a local id with no live agent, then an unclaimed name."""

        if not _switch_on():
            return
        local = self._local_address(target)
        if local is None:
            return
        if is_agent_id_segment(local[1]):
            if self._dead_local_agent_id(target):
                raise DaemonRequestError(
                    ipc_errors.TARGET_UNRESOLVED,
                    f"no live agent has the id address {target!r} on this node",
                    {"target": target},
                )
            return
        self._refuse_unclaimed_local_name(target)

    def _refuse_dead_session_address(self, actor: str) -> None:
        """Refuse a session at a local ``a.`` id no live agent has.

        The answer is SESSION_SUPERSEDED, as for a destroyed agent's retired
        session, so no session is ever kept at a dead incarnation's id.  It
        carries no data: every spec declares SESSION_SUPERSEDED without a
        data schema (the release contract), and the actor is in the message.
        """

        if self._dead_local_agent_id(actor):
            raise DaemonRequestError(
                ipc_errors.SESSION_SUPERSEDED,
                f"no live agent has the id address {actor!r} on this node "
                "(destroyed or unknown); this session is permanently retired",
            )

    def _official_registration_command(self, command: Any, *, harness: str) -> Any:
        """The registration ``command`` keyed at its agent's official address.

        A plain first ``session.register`` for a local name with no record
        creates the record here, before the per-actor route coordination key
        is chosen, inside an exclusive registration boundary keyed by the
        name URI; a registration that waited on that boundary finds the
        record.  Both continue under the record's address, so every
        registration of one agent serializes and fences under one key.  A
        guarded registration, an ``a.`` id, another node, or the switch off
        leaves the command unchanged.
        """

        actor = command.actor
        parsed = parse_agent_uri(actor)
        if (
            not _switch_on()
            or command.refuse_live_incumbent
            or parsed is None
            or parsed[1] != self.node_id
            or is_agent_id_segment(parsed[2])
        ):
            return command
        with self._interactive_registration_effect(actor, exclusive=True):
            if self.agents.get(actor) is None:
                self._prepare_session_registration(
                    command, actor=actor, harness=harness
                )
            official = self._canonical_interactive_actor(actor)
        return replace(command, actor=official)

    def _refuse_unclaimed_local_name(self, target: str) -> None:
        """HQ Q2: refuse a local name address nothing on this node claims."""

        if not self._local_recipient_claimed(target):
            raise DaemonRequestError(
                ipc_errors.TARGET_UNRESOLVED,
                f"nothing on this node claims {target!r}; the message would "
                "never be delivered",
                {"target": target},
            )

    def _refuse_undeliverable_local_target(self, target: str) -> None:
        """Every send-boundary refusal of a local agent address, in order."""

        self._refuse_retired_local_address(target)
        self._refuse_unresolved_local_target(target)

    def _claimed_without_record(
        self, target: str, session_actors: tuple[str, ...]
    ) -> bool:
        """Whether a claimant without a registry record drains ``target``.

        An online actor, a streaming harness whose canonical URI is
        ``target``, an interactive session whose actor is one of
        ``session_actors``, or a managed worker session.
        """

        if self._presence is not None and self._presence.actor_online(target):
            return True
        from hyprial.daemon.impl.harnesses import is_streaming_spec

        for spec in self.desired_state.load().harnesses:
            if (
                is_streaming_spec(spec)
                and self._canonical_harness_uri(spec.name, spec) == target
            ):
                return True
        for session in self._agent_session_domains.session.read_sessions():
            if session.actor in session_actors:
                return True
        return self._worker_session_ref(target) is not None

    def _local_recipient_claimed(self, recipient: str) -> bool:
        """Whether this node can ever drain ``recipient`` (delivery claim).

        True for anything this node is not the authority for (see
        ``_local_address``), for daemon service actors, and while the
        id-form switch is off; an unreadable claim source also answers True
        so delivery never breaks on a lookup failure.  A registered agent
        claims only its stored URI, under its own owner.
        """

        if not _switch_on():
            return True
        try:
            local = self._local_address(recipient)
            if local is None:
                return True
            owner, segment = local
            if segment in SERVICE_ACTOR_NAMES:
                return True
            if any(
                agent.owner == owner
                and agent.machine == self.node_id
                and agent.uri == recipient
                for agent in self.agents.list()
            ):
                return True
            if owner == self.owner:
                return not self._is_unresolved_local_identity(recipient)
            return self._claimed_without_record(recipient, (recipient,))
        except Exception:  # noqa: BLE001
            return True


__all__ = ["SERVICE_ACTOR_NAMES", "_AgentAddressAdmissionMixin"]
