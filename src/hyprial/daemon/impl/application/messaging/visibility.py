"""Caller identity, sender authentication and send/channel/org enforcement."""

from __future__ import annotations

from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import (
    lark_reply_adapter,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.identity import IdentityResolverError
from hyprial.daemon.impl.configuration.identity import (
    classify_target_identity,
    normalize_agent_recipient,
)
from hyprial.identity import (
    SEE_ACTORS,
    SEND_TO,
    CallerIdentity,
    GrantRecord,
    VisibilityError,
    may_send,
)
from hyprial.identity import (
    EnforcementError,
    GRANTABLE_CAPABILITIES,
    CapabilityRecord,
    Principal,
    check_channel,
    check_org_context,
)
from hyprial.kernel import (
    ADAPTER_URI_PREFIX,
    AGENT_URI_PREFIX,
    CHANNEL_URI_PREFIX,
    TARGET_KIND_AGENT,
    TARGET_KIND_CHANNEL_ROUTE,
    agent_uri_actor,
    canonical_agent_uri,
    canonical_user_uri,
    parse_agent_uri,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _actor,
)


_SENDER_AUTH_MODE_ENV = "HYPRIAL_SENDER_AUTH_MODE"

_SENDER_AUTH_ENFORCE = "enforce"

_SENDER_AUTH_OBSERVE = "observe"

_AUTH_LOG_IDENTITY_MAX_CHARS = 256

_LOCAL_ADAPTER_IDENTITIES = frozenset({"lark-adapter"})

_SENDER_TEXT_FIELDS = (
    "kind",
    "platformId",
    "unionId",
    "displayName",
    "owner",
    "standing",
    "source",
    "userKey",
    "userKind",
    "nickname",
    "realName",
)

_SENDER_STANDINGS = frozenset({"verified", "observed", "ambiguous", "unresolved"})

_SENDER_FIELD_MAX_CHARS = 256

def _visible_message_origin(body: object, sender: str) -> JsonObject:
    """Return stored provenance, with a conservative legacy-path fallback."""

    origin = body.get("origin") if isinstance(body, dict) else None
    if isinstance(origin, dict) and origin:
        return origin
    if sender.startswith((CHANNEL_URI_PREFIX, ADAPTER_URI_PREFIX)):
        via = "adapter"
    elif sender.startswith("user:"):
        via = "operator"
    else:
        via = "daemon"
    return {"via": via}

def _message_origin(metadata: object) -> JsonObject | None:
    """Carry a message's reported origin through to its reader.

    ``None`` means the adapter said nothing, which a reader must keep distinct
    from any particular answer: "unknown" and "a direct message" are different
    facts, and only one of them is safe to act on.  The block stays
    adapter-scoped rather than flattened into a generic key, because a bare
    ``chatType`` would claim a meaning that the other adapters never agreed to.
    """

    if not isinstance(metadata, dict):
        return None
    origin: JsonObject = {}
    chat_type = metadata.get("chatType")
    if isinstance(chat_type, str) and chat_type:
        origin["chatType"] = chat_type
    sender = _reported_sender(metadata.get("sender"))
    if sender is not None:
        origin["sender"] = sender
    if not origin:
        return None
    reported_by = metadata.get("provider")
    if isinstance(reported_by, str) and reported_by:
        origin["provider"] = reported_by
    return origin

def _reported_sender(value: object) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    standing = value.get("standing")
    if standing not in _SENDER_STANDINGS:
        # An answer without a known confidence cannot be acted on safely;
        # dropping it keeps "no sender block" meaning "the adapter said
        # nothing we can trust", never a silently upgraded standing.
        return None
    sender: JsonObject = {}
    for field in _SENDER_TEXT_FIELDS:
        text = value.get(field)
        sender[field] = (
            text[:_SENDER_FIELD_MAX_CHARS] if isinstance(text, str) and text else None
        )
    if standing != "verified":
        # Only a person's confirmation names an owner.
        sender["owner"] = None
    for candidates_field in ("candidateOwners", "candidateUsers"):
        candidates = value.get(candidates_field)
        if standing == "ambiguous" and isinstance(candidates, list):
            sender[candidates_field] = [
                candidate[:_SENDER_FIELD_MAX_CHARS]
                for candidate in candidates[:8]
                if isinstance(candidate, str) and candidate
            ]
    if value.get("lookupFailed") is True:
        sender["lookupFailed"] = True
    return sender

@dataclass(frozen=True, slots=True)
class _MessageCaller:
    sender: str
    subject: str
    via: str
    on_behalf_of: str | None = None


class _VisibilityMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _resolve_send_sender(self, actor: str) -> str:
        """Resolve an unfenced (CLI) sender to a registered identity — or refuse.

        Allen (b): resolve-or-reject.  Every identity on the wire carries a
        registry row; the daemon never mints one from nothing.  Runs the one
        targeting pipeline (``normalize_agent_recipient`` +
        ``_resolve_agent_alias``, local-only: a sender can only be an
        identity of THIS node).  That local-only property used to need an
        opt-out here (``include_presence=False``) because resolution scanned
        network presence; it is now structural — the resolver reads two local
        tables and nothing else, so no caller can readmit a remote candidate
        by forgetting a keyword.  Scheme-carrying senders — canonical agent
        URIs, ``channel:lark:<adapter>`` inbound forwards, ``user:`` — pass
        through untouched; ambiguity propagates as AMBIGUOUS_TARGET.  The
        error states facts only, no remediation instructions.
        """

        normalized = normalize_agent_recipient(actor)
        if ":" not in normalized:
            resolved = self._resolve_agent_alias(normalized)
            if resolved == normalized:
                raise DaemonRequestError(
                    ipc_errors.SENDER_UNRESOLVED,
                    (f"sender {actor!r} is not a registered agent on this "
                    "node; an unregistered identity cannot receive replies"),
                    {"sender": actor},
                )
            return resolved
        if normalized.startswith(AGENT_URI_PREFIX):
            if classify_target_identity(normalized) != TARGET_KIND_AGENT:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"sender {actor!r} is not a canonical "
                    "agent:<owner>:<machine>:<actor> URI",
                    {"sender": actor},
                )
            # A canonical-shaped sender is not automatically backed: the URI
            # must name THIS node and resolve to a registered identity here.
            # Anything else is a foreign or unbacked identity — sending with
            # it would put an impersonating sender on the wire.
            parsed_sender = parse_agent_uri(normalized)
            assert parsed_sender is not None  # classify proved four segments
            _owner, _machine, short = parsed_sender
            if (
                parsed_sender[0] == self.owner
                and parsed_sender[1] == self.node_id
                and self._resolve_agent_alias(short)
                == normalized
            ):
                return normalized
            raise DaemonRequestError(
                ipc_errors.SENDER_UNRESOLVED,
                (f"sender {actor!r} is not a registered agent on this "
                    "node; an unregistered identity cannot receive replies"),
                {"sender": actor},
            )
        return normalized

    def _authenticated_message_caller(
        self,
        params: JsonObject,
        *,
        method: str,
        trusted_origin: str | None = None,
    ) -> _MessageCaller:
        """Bind a message principal to its daemon-owned session.

        The private local socket is an operator boundary only for the exact
        ``user:<owner>`` identity.  Agent names are claims until the existing
        session fence proves that the same actor owns the presented opaque
        reference.  Adapter/channel identities retain their existing local
        gateway path.  The socket dispatcher supplies the Python-only ``ipc``
        marker; direct in-process calls are daemon-owned and no IPC parameter
        can manufacture either trusted origin.
        """

        raw = _actor(params)
        on_behalf_raw = params.get("onBehalfOf")
        if on_behalf_raw is not None and (
            not isinstance(on_behalf_raw, str) or not on_behalf_raw
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "onBehalfOf must be a non-empty actor identity",
            )

        if "sessionRef" in params:
            actor = self._canonical_interactive_actor(raw)
            try:
                actor = self._fence_interactive_session(actor, params)
            except DaemonRequestError:
                # The fence's own code is the answer: SESSION_SUPERSEDED and
                # STALE_SESSION tell a Channel child whether to go quiet or
                # re-register, and the request is refused either way.
                self._record_unauthenticated_sender(method, actor)
                raise
            if on_behalf_raw is not None:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "onBehalfOf is available only to the local operator identity",
                )
            return _MessageCaller(actor, actor, "session")

        normalized = normalize_agent_recipient(raw)
        if normalized == canonical_user_uri(self.owner):
            on_behalf = (
                self._resolve_on_behalf_actor(on_behalf_raw)
                if isinstance(on_behalf_raw, str)
                else None
            )
            return _MessageCaller(
                normalized,
                on_behalf or normalized,
                "operator",
                on_behalf,
            )
        if normalized.startswith(
            (CHANNEL_URI_PREFIX, ADAPTER_URI_PREFIX)
        ) or normalized in _LOCAL_ADAPTER_IDENTITIES:
            if on_behalf_raw is not None:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "onBehalfOf is available only to the local operator identity",
                )
            return _MessageCaller(normalized, normalized, "adapter")

        if trusted_origin not in (None, "daemon", "ipc"):
            raise ValueError("unknown trusted message origin")
        if trusted_origin in (None, "daemon"):
            if method in {
                "message.ack",
                "message.pending.list",
                "message.pending.wait",
                "message.status",
                "progress.list",
            }:
                sender = self._message_consumer_actor(params)
            else:
                sender = (
                    raw
                    if raw == self._dispatch_service_actor
                    else self._resolve_send_sender(raw)
                )
            on_behalf = (
                self._resolve_on_behalf_actor(on_behalf_raw)
                if isinstance(on_behalf_raw, str)
                else None
            )
            return _MessageCaller(
                sender=sender,
                subject=on_behalf or sender,
                via="daemon",
                on_behalf_of=on_behalf,
            )

        claimed = (
            self._message_consumer_actor(params)
            if method
            in {
                "message.ack",
                "message.pending.list",
                "message.pending.wait",
                "message.status",
                "progress.list",
            }
            else self._resolve_send_sender(raw)
        )
        self._record_unauthenticated_sender(method, claimed)
        if self._sender_auth_enforced:
            raise self._sender_not_authenticated(claimed)
        return _MessageCaller(claimed, claimed, "daemon")

    def _resolve_on_behalf_actor(self, actor: str) -> str:
        normalized = normalize_agent_recipient(actor)
        if normalized.startswith(AGENT_URI_PREFIX):
            if classify_target_identity(normalized) != TARGET_KIND_AGENT:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "onBehalfOf must be an agent identity",
                )
            return normalized
        return self._resolve_send_sender(actor)

    @staticmethod
    def _sender_not_authenticated(actor: str) -> DaemonRequestError:
        return DaemonRequestError(
            ipc_errors.SENDER_NOT_AUTHENTICATED,
            f"identity {actor!r} was claimed, but no matching session was "
            "presented; the operator form is --from user:<owner> "
            "--on-behalf-of <actor>",
            {"claimedIdentity": actor},
        )

    def _record_unauthenticated_sender(self, method: str, actor: str) -> None:
        peer_pid = getattr(self._ipc_peer, "pid", None)
        self._log(
            "warn",
            "daemon",
            "send.sender_unauthenticated",
            method=method[:64],
            claimedActor=actor[:_AUTH_LOG_IDENTITY_MAX_CHARS],
            count=1,
            **({"callerPid": peer_pid} if isinstance(peer_pid, int) else {}),
        )

    def _resolve_consumer_identity(self, actor: str) -> str:
        """Read-side identity: resolve a bare name when registered, else verbatim.

        Reads never reject: a registered alias reads its canonical key, an
        unregistered bare name reads the verbatim key its durable rows
        carry.  ``_message_consumer_keys`` adds the union spellings.
        """

        normalized = normalize_agent_recipient(actor)
        if ":" in normalized:
            return normalized
        return self._resolve_agent_alias(normalized)

    def _reply_path_unavailable(self, sender: str) -> bool:
        """Best-effort verdict: is a reply to this sender certain to fail?

        Only the two provably-broken shapes return True (#60: 宁可漏报,
        不误报): a ``user:<owner>`` sender whose squire profile or binding
        is incomplete (delivery fails TARGET_SQUIRE_UNCONFIGURED), and a
        ``channel:lark:<adapter>`` sender whose adapter is not configured
        here (replies fail MESSAGE_REPLY_UNAVAILABLE).  Everything else
        returns False — unsure means silent.
        """

        if sender.startswith("user:"):
            owner = sender.removeprefix("user:")
            # `resolve`, not `get_by_owner`: `user:<x>` carries either an owner
            # key (what `resolve_node_owner()` mints) or an owner.
            profile = self.user_profiles.resolve(owner)
            if profile is None or profile.squire_adapter is None:
                return True
            try:
                return (
                    self._identity_resolver.profile_open_id(
                        profile, profile.squire_adapter
                    )
                    is None
                )
            except IdentityResolverError as error:
                # Evaluated after delivery: an identity fault must not turn a
                # delivered send into an IPC error (a retry would duplicate).
                # Unsure means silent (#60), and the operator hears about it.
                self._log(
                    "warn",
                    "daemon",
                    "send.reply_path_identity_unresolved",
                    code=error.code,
                )
                return False
        adapter = lark_reply_adapter(sender)
        if adapter is not None:
            return (
                self._adapters is None
                or adapter not in self._lark_gateway_names()
            )
        return False

    def _canonical_interactive_actor(self, actor: str) -> str:
        """Normalize one local MCP/Channel actor at the daemon boundary.

        CLI normalization is convenience only: older channel children and raw
        MCP clients can still call ``session.register`` directly.  The daemon
        is the authority that prevents those callers from creating a second,
        bare network registration beside the managed-harness four-segment
        form.  An already-canonical URI remains byte-for-byte unchanged for
        compatibility with callers that supply the complete identity.
        """

        if ":" not in actor:
            try:
                return canonical_agent_uri(self.owner, self.node_id, actor)
            except ValueError as error:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        short = agent_uri_actor(actor)
        if short is None:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"actor {actor!r} is neither a bare actor name nor a canonical "
                "agent:<owner>:<machine>:<actor> URI",
            )
        return actor

    def _mcp_actor(self, params: JsonObject) -> str:
        """Canonicalize only actors carried by fenced MCP/session requests.

        The same message methods also back unfenced CLI operations such as
        ``hyprial ack --from <node>``.  Those actors address durable inbox rows
        verbatim and must not be rewritten as interactive-agent URIs.
        """

        actor = _actor(params)
        return (
            self._canonical_interactive_actor(actor)
            if "sessionRef" in params
            else actor
        )

    def _visibility_identity(self, actor: str) -> CallerIdentity:
        """Wrap one identity this daemon has already verified.

        ``hosted`` comes from the registry row, never from the request: the
        payload may not promote a hosted visitor to a host-local caller.
        """

        agent = self.agents.get(actor)
        hosted = agent is not None and agent.hosted_by is not None
        return CallerIdentity(uri=actor, hosted=hosted, verified=True)

    def _visibility_caller(self, params: JsonObject) -> CallerIdentity | None:
        """The caller a see-actors/send-to decision may trust, or ``None``.

        Card 358 puts identity on a controlled channel: this daemon mints the
        session bindings, so ``actor`` + ``sessionRef`` is the only shape that
        counts.  No binding is the local operator path (CLI, this daemon's own
        workers, another node's inbound forward) and keeps today's behaviour.
        A binding that does not verify -- a presented identity with a made-up
        or superseded ``sessionRef`` -- is refused by the fence below rather
        than downgraded to "unverified": a claimed identity is not a
        credential.

        A verified NATIVE caller keeps today's view on purpose.  Card 358
        names hosted visitors ("no grant: see only itself"); an agent running
        natively on this host runs with the operator's own credentials, so
        tightening it is a ruling, not a side effect of wiring (AT09, the
        reviewer's (iv) note).
        """

        if "sessionRef" not in params:
            return None
        claimed = self._mcp_actor(params)
        if not self._visibility_identity(claimed).hosted:
            # Only a HOSTED visitor is fenced and restricted here.  MCP puts
            # actor+sessionRef on every invoke, native agents included, so
            # fencing first would newly refuse a native caller's ``targets``
            # with STALE_SESSION / SESSION_SUPERSEDED after a daemon restart or
            # a carrier re-register -- a change to today's behaviour that card
            # 358 does not ask for (reviewer's (b) finding, 2026-09-30), and one
            # that ``message.send`` -- which fences ``source`` itself -- would
            # not share.  Recorded limitation: a caller that presents another
            # actor's name is not separable from that actor on this path, so a
            # hosted visitor that claims a native name sees the host-local view.
            # Closing that needs an identity this build does not have (L2 is a
            # ruling, card 358's own scope note), not a fence on natives.
            return None
        actor = self._fence_interactive_session(claimed, params)
        return self._visibility_identity(actor)

    def _visibility_grants(self, actor: str) -> list[GrantRecord]:
        """The caller's live see-actors/send-to grants, read on every call.

        Nothing here caches a verdict, which is what makes a withdrawal take
        effect on the next request.  A ledger row the decision cannot parse is
        logged and dropped: dropping can only narrow the answer, and the
        warning keeps it from being silent.

        The ledger keys rows by the local actor NAME and ``read_capability_grants``
        already joins them to this actor's live incarnation, so every row it
        returns belongs to ``actor``: the record is stamped with the caller URI
        the decision compares against, not with the bare name.
        """

        records: list[GrantRecord] = []
        for grant in self._agent_session_domains.agent.read_capability_grants(actor):
            if grant.capability not in (SEE_ACTORS, SEND_TO):
                continue
            try:
                records.append(
                    GrantRecord(
                        actor=actor,
                        capability=grant.capability,
                        scope=grant.scope,
                        revision=grant.revision,
                    )
                )
            except VisibilityError:
                self._log(
                    "warn",
                    "visibility",
                    "visibility.grant.unusable",
                    actor=grant.actor,
                    capability=grant.capability,
                )
        return records

    def _enforce_send_to(
        self, caller: CallerIdentity, targets: tuple[str, ...]
    ) -> None:
        """Refuse a delivery the caller's send-to ledger does not name.

        Only the host-local path is exempt: see ``_visibility_caller``.  This
        is deliberately separate from the ``targets`` filter -- filtering a
        list never stopped anyone from naming an address directly.

        ``route:<adapter>:<route>`` targets are delivery exits, not
        principals: a send-to scope is a principal list, so the ledger can
        never name a route.  Those targets belong to the channel capability
        and are decided by ``_enforce_channel`` instead (AT10, card 359).
        """

        if not caller.hosted:
            return
        grants = self._visibility_grants(caller.uri)
        for target in targets:
            if classify_target_identity(target) == TARGET_KIND_CHANNEL_ROUTE:
                continue
            decision = may_send(target, caller=caller, grants=grants)
            if decision.allowed:
                continue
            raise DaemonRequestError(
                ipc_errors.TARGET_NOT_AUTHORIZED,
                f"{caller.uri} may not send to {target}: {decision.reason}",
                {
                    "caller": caller.uri,
                    "target": target,
                    "capability": decision.capability,
                    "reason": decision.reason,
                },
            )

    def _capability_caller(self, params: JsonObject) -> Principal | None:
        """The verified caller a capability entry point may trust, or ``None``.

        Every presented binding must verify before a content read. A hosted
        visitor is then returned as a ``Principal``; a host-local operator or
        verified native agent remains exempt from the capability grant. The
        disclosed native-name exemption for target listing does not authorize
        reading contents with a forged or stale session reference.
        """

        if "sessionRef" not in params:
            return None
        claimed = self._mcp_actor(params)
        actor = self._fence_interactive_session(claimed, params)
        identity = self._visibility_identity(actor)
        if not identity.hosted:
            return None
        return Principal(uri=identity.uri, hosted=True, verified=True)

    def _capability_records(self, actor: str) -> list[CapabilityRecord]:
        """The caller's live capability grants, read on every call.

        Mirrors ``_visibility_grants``: nothing caches a verdict, so a
        withdrawal takes effect on the next request.  Rows for capabilities
        this layer does not decide (see-actors / send-to, which AT09 owns) or
        that fail to parse are logged and dropped -- dropping can only narrow
        the answer.
        """

        records: list[CapabilityRecord] = []
        for grant in self._agent_session_domains.agent.read_capability_grants(actor):
            if grant.capability not in GRANTABLE_CAPABILITIES:
                continue
            try:
                records.append(
                    CapabilityRecord(
                        actor=actor,
                        capability=grant.capability,
                        scope=grant.scope,
                        revision=grant.revision,
                    )
                )
            except EnforcementError:
                self._log(
                    "warn",
                    "capability",
                    "capability.grant.unusable",
                    actor=grant.actor,
                    capability=grant.capability,
                )
        return records

    def _enforce_channel(
        self, caller: CallerIdentity, targets: tuple[str, ...]
    ) -> None:
        """Refuse an outbound adapter route the caller's channel ledger omits.

        Only ``route:<adapter>:<route>`` targets are the channel entry point;
        agent and user addresses are decided by the send-to ledger (AT09),
        which cannot name a route at all.
        Only a hosted caller is fenced -- the same rule as ``_enforce_send_to``
        -- and the grant names the exact route, which is the minimum grant for
        a channel rather than an adapter-wide allow.
        """

        if not caller.hosted:
            return
        principal = Principal(uri=caller.uri, hosted=True, verified=True)
        records: list[CapabilityRecord] | None = None
        for target in targets:
            if classify_target_identity(target) != TARGET_KIND_CHANNEL_ROUTE:
                continue
            if records is None:
                records = self._capability_records(caller.uri)
            decision = check_channel(target, caller=principal, records=records)
            if decision.allowed:
                continue
            raise DaemonRequestError(
                ipc_errors.CHANNEL_NOT_AUTHORIZED,
                f"{caller.uri} may not use channel {target}: {decision.reason}",
                {
                    "caller": caller.uri,
                    "channel": target,
                    "capability": decision.capability,
                    "entryPoint": decision.entry_point,
                    "reason": decision.reason,
                },
            )

    def _enforce_org_context(self, caller: Principal | None) -> None:
        """Refuse an org-shared-folder read the caller's org-context grant omits.

        org-context is granted only for the accepted document (fixed scope
        ``accepted``), never per space (AT10 / card 359).  ``None`` is the
        host-local path and is not restricted here.
        """

        if caller is None:
            return
        decision = check_org_context(
            caller=caller, records=self._capability_records(caller.uri)
        )
        if decision.allowed:
            return
        raise DaemonRequestError(
            ipc_errors.ORG_CONTEXT_NOT_AUTHORIZED,
            f"{caller.uri} may not read the org shared folder: {decision.reason}",
            {
                "caller": caller.uri,
                "capability": decision.capability,
                "entryPoint": decision.entry_point,
                "resource": decision.resource,
                "reason": decision.reason,
            },
        )
