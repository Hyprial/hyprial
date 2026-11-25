"""Caller identity, sender authentication and send/org enforcement.

Decision notes (I4b, owner items Q0-a and Q0-d, 2026-11-23): the
``route:<adapter>:<route>`` target is gone, so ``message.send`` has no
channel entry point left and ``_enforce_channel`` is deleted; the send-to
ledger no longer skips route targets because there are none.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import (
    lark_reply_adapter,
)
from hyprial.daemon.impl.api import (
    ORIGIN_CHAT_KINDS,
    ORIGIN_FIELD_MAX_CHARS,
    origin_sender,
    visible_message_origin,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.configuration.identity import (
    classify_target_identity,
    normalize_agent_recipient,
)
from hyprial.identity import (
    SEE_ACTORS,
    SEND_TO,
    CallerIdentity,
    GrantRecord,
    InvalidAgentNameError,
    VisibilityError,
    may_send,
)
from hyprial.identity import (
    EnforcementError,
    GRANTABLE_CAPABILITIES,
    CapabilityRecord,
    Principal,
    check_org_context,
)
from hyprial.kernel import (
    ADAPTER_URI_PREFIX,
    AGENT_URI_PREFIX,
    RESERVED_AGENT_ACTOR_NAMES,
    TARGET_KIND_AGENT,
    agent_uri_actor,
    is_agent_id_segment,
    member_actor_parts,
    canonical_user_uri,
    is_own_user_uri,
    parse_agent_uri,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _actor,
)
from hyprial.kernel.interface import (
    IPC_MESSAGE_READ_BODY,
    IPC_MESSAGE_SETTLE_HINT,
)

#: Consumer-plane methods: resolved as a read key (never as a send sender), so
#: a bare name reads its own durable rows instead of failing sender
#: authentication.  ``message.read_body`` and ``message.settle_hint`` join
#: ``message.ack`` here (WP3): same identity rule, same read keys.
_CONSUMER_METHODS = frozenset(
    {
        "conversations.history",
        "conversations.list",
        "message.ack",
        "message.history",
        "message.pending.batch",
        "message.pending.list",
        "message.pending.wait",
        "message.status",
        "progress.list",
        IPC_MESSAGE_READ_BODY,
        IPC_MESSAGE_SETTLE_HINT,
    }
)


_SENDER_AUTH_MODE_ENV = "HYPRIAL_SENDER_AUTH_MODE"

_SENDER_AUTH_ENFORCE = "enforce"

_SENDER_AUTH_OBSERVE = "observe"

_AUTH_LOG_IDENTITY_MAX_CHARS = 256

_LOCAL_ADAPTER_IDENTITIES = frozenset({"lark-adapter"})



def _visible_message_origin(body: object, sender: str) -> JsonObject:
    return visible_message_origin(body, sender)


def _message_origin(
    metadata: object, *, on_unparsable: Callable[[object], None] | None = None
) -> JsonObject | None:
    """Carry a message's reported origin through to its reader.

    ``None`` means the adapter said nothing, which a reader must keep distinct
    from any particular answer: "unknown" and "a direct message" are different
    facts.  Only the generic facts every adapter kind can state are carried:
    the chat kind, the sender's platform account, the platform message id and
    time.  Adapter-private metadata stays with the adapter.  The sender block
    names the platform account and its observed display name, nothing more:
    an IM sender is whoever owns that account, and no field judges who that is.
    """

    if not isinstance(metadata, dict):
        return None
    origin: JsonObject = {}
    chat_kind = metadata.get("chatKind")
    if isinstance(chat_kind, str) and chat_kind in ORIGIN_CHAT_KINDS:
        origin["chatKind"] = chat_kind
    sender = origin_sender(metadata.get("sender"))
    if sender is not None:
        origin["sender"] = sender
    if not origin:
        return None
    reported_by = metadata.get("provider")
    if isinstance(reported_by, str) and reported_by:
        origin["provider"] = reported_by
    platform_message_id = metadata.get("messageId")
    if isinstance(platform_message_id, str) and platform_message_id:
        origin["platformMessageId"] = platform_message_id[:ORIGIN_FIELD_MAX_CHARS]
    created_at, state = _platform_created_at(metadata.get("createTime"))
    if created_at is not None:
        origin["platformCreatedAt"] = created_at
    elif state == "unparsable" and on_unparsable is not None:
        # "The platform said something we could not read" is an anomaly in the
        # reporting adapter, not a property of this message: it stays out of the
        # envelope (a reader must not see a fabricated timestamp) but it is
        # reported once, so operators can see the third state at all.
        on_unparsable(metadata.get("createTime"))
    return origin


# An unreadable platform time is an anomaly in the reporting adapter, not a
# property of one message: during a format change every message carries it. The
# counter is monotonic (so the third state can be *quantified*, not merely seen in
# a log), and the log line is throttled (so the anomaly cannot drown the log it
# is reported in). Both are process-local and are not persisted on purpose: a
# restart is the moment someone is looking anyway.
UNPARSABLE_REPORT_WINDOW_MS = 300_000

# The counter is per process, not per install: it resets on restart on purpose.
# The name says so, and the log line carries the epoch it started counting from,
# so a reader never has to guess which total they are looking at.
# Not "process start" in the OS sense: this is the moment the module was imported.
# The daemon imports it once per process, so in practice the two coincide; under a
# reload (or a second import in one process) this value moves while the process
# does not, which is why the log field is paired with the total rather than trusted
# alone. A reader compares (since_ms, total) pairs, never totals across processes.
_PROCESS_STARTED_MS = int(time.time() * 1000)

_unparsable_totals: dict[str, int] = {"total": 0}
_unparsable_last_report_ms: dict[str, int | None] = {"at": None}


def platform_created_at_unparsable_total_per_process() -> int:
    """How many platform times **this process** could not read.

    Per process, not per install: it restarts at zero. "Monotonic" would be the
    wrong word — a restart is a legitimate discontinuity, and a reader comparing
    two numbers from different processes would be comparing different spans.
    """

    return _unparsable_totals["total"]


def _reset_unparsable_report_state() -> None:
    """Test hook: the counter and the throttle are process state."""

    _unparsable_totals["total"] = 0
    _unparsable_last_report_ms["at"] = None


def _unparsable_preview(raw: object) -> str:
    """A bounded diagnostic preview of an unreadable value that never raises.

    Python 3.12 raises ``ValueError`` once an ``int``→``str`` conversion
    exceeds the digit limit (default 4300), so a hostile epoch such as
    ``10**5000`` must never reach ``str()``: ints are described by bit
    length instead, and any other ``repr`` failure falls back to the bare
    type name.  The anomaly log *is* the containment path, so it must not
    be the thing that raises.
    """

    if isinstance(raw, int) and not isinstance(raw, bool):
        return f"<int {raw.bit_length()} bits>"
    try:
        return repr(raw)[:32]
    except Exception:
        return type(raw).__name__


def report_unparsable_platform_created_at(
    raw: object, *, log: Callable[..., None], now_ms: int
) -> bool:
    """Count one unreadable platform time; log at most once per window.

    Returns whether this call emitted a line. The first observation in a window
    always emits (so a single anomaly is never silent); later ones inside the
    window are counted and summarised by the next emission's ``total``.
    """

    _unparsable_totals["total"] += 1
    total = _unparsable_totals["total"]
    last = _unparsable_last_report_ms["at"]
    if last is not None and now_ms - last < UNPARSABLE_REPORT_WINDOW_MS:
        return False
    _unparsable_last_report_ms["at"] = now_ms
    log(
        "warn",
        "daemon",
        "origin.platform_created_at.unparsable",
        raw=_unparsable_preview(raw),
        total=total,
        since_ms=_PROCESS_STARTED_MS,
    )
    return True


def _platform_created_at(value: object) -> tuple[str | None, str]:
    """The platform's own creation time, as ISO-8601 in UTC.

    Adapters hand over whatever the platform reported; Lark reports a millisecond
    epoch as a decimal string.  Decisions pinned here:

    * the value is **normalised**, never passed through raw: a bare ``"1793…"``
      reads as a local millisecond count to a consumer that has not been told
      otherwise, while ``…Z`` cannot be misread;
    * a decimal string must be pure ASCII digits (``[0-9]{1,16}``), not
      ``str.isdigit``: that accepts characters such as ``"²"`` that ``int()``
      cannot read, and unbounded digits reach the int↔str conversion limit —
      the 16-digit bound keeps ``int()`` exact and the division in float range;
    * an absent or unparseable value yields ``None`` so the key is **omitted**
      and the caller never sees an exception.  "The platform said nothing" and
      "the platform said 1970" are different facts, and only one of them is
      safe to act on (the same rule the sender block follows above).
    """

    if value is None:
        return None, "absent"
    if isinstance(value, bool):
        return None, "unparsable"
    if isinstance(value, int):
        milliseconds = value
    elif isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"[0-9]{1,16}", text):
            return None, "unparsable"
        milliseconds = int(text)
    else:
        return None, "unparsable"
    if milliseconds <= 0:
        return None, "unparsable"
    try:
        moment = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None, "unparsable"
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z"), "ok"

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
        URIs, ``adapter:lark:<adapter>`` inbound forwards, ``user:`` — pass
        through untouched; ambiguity propagates as AMBIGUOUS_TARGET.  The
        error states facts only, no remediation instructions.

        Integrator decision D8 (ruling U1): with id-form minting on, a sender
        URI of this node and owner that names a live registered agent by name
        is refused as the send boundary refuses such a target:
        ``AGENT_ADDRESS_RETIRED`` with the id address, never mapped to it.
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
            if parsed_sender[0] == self.owner and parsed_sender[1] == self.node_id:
                if self._resolve_agent_alias(short) == normalized:
                    return normalized
                if member_actor_parts(short) is not None:
                    agent = self.agents.get(normalized)
                    if agent is not None:
                        return agent.uri
                self._refuse_retired_local_address(normalized)
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
            actor = self._mcp_actor(params)
            self._refuse_reserved_interactive_actor(actor)
            try:
                actor = self._fence_live_principal_session(actor, params)
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
            ADAPTER_URI_PREFIX
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
            if method in _CONSUMER_METHODS:
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
            if method in _CONSUMER_METHODS
            else self._resolve_send_sender(raw)
        )
        self._record_unauthenticated_sender(method, claimed)
        if self._sender_auth_enforced:
            raise self._sender_not_authenticated(claimed)
        return _MessageCaller(claimed, claimed, "daemon")

    def _resolve_on_behalf_actor(self, actor: str) -> str:
        """The agent an operator acts for, at its official address.

        A canonical agent URI keeps its spelling.  With id-form minting on, a
        local name URI of a live registered agent is refused with
        ``AGENT_ADDRESS_RETIRED`` and the id address (integrator decision D8,
        ruling U1), as the send boundary refuses it; it is never rewritten
        to the id address.  A bare name resolves like an unfenced sender.
        """

        normalized = normalize_agent_recipient(actor)
        if normalized.startswith(AGENT_URI_PREFIX):
            if classify_target_identity(normalized) != TARGET_KIND_AGENT:
                raise DaemonRequestError(
                    ipc_errors.INVALID_ARGUMENT,
                    "onBehalfOf must be an agent identity",
                )
            self._refuse_retired_local_address(normalized)
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

    def _consumer_read_keys(self, address: str) -> tuple[str, ...]:
        """Every inbox key a consumer at ``address`` reads: ``address`` alone.

        The one helper behind every consumer read of an actor's rows: the
        session fetch, the headless runtime pull and the dormant wake.  No
        name-form alias is read (owner decision 2026-11-19): rows at a name
        URI are never an id-minted agent's, so a new agent that reuses a name
        never drains mail addressed to an earlier one.
        """

        return (address,)

    def _harness_consumer_read_keys(self, name: str) -> tuple[str, ...]:
        """The consumer read keys of a supervised worker, by its local name."""

        return self._consumer_read_keys(self._canonical_harness_uri(name))

    def _reply_path_unavailable(self, sender: str) -> bool:
        """Best-effort verdict: is a reply to this sender certain to fail?

        Only the two provably-broken shapes return True (#60: 宁可漏报,
        不误报): this node's ``user:<owner>`` when no composed agent cell
        names a chat (R2, I3, I7a: nothing pushes a reply to the owner's IM;
        it stays in GUI and CLI; which agent will reply is not known here, so
        any bound agent cell counts, the user agent cell included), and an
        ``adapter:lark:<adapter>`` sender whose adapter is not
        configured here (replies fail MESSAGE_REPLY_UNAVAILABLE).  Everything
        else, another owner included, returns False — unsure means silent.
        """

        if is_own_user_uri(sender, self.owner):
            from hyprial.daemon.impl.application.messaging.inbox_surface.send_targets.user import (
                agent_cell_names_a_chat,
            )

            return not agent_cell_names_a_chat(self)
        adapter = lark_reply_adapter(sender)
        if adapter is not None:
            return (
                self._adapters is None
                or adapter not in self._lark_gateway_names()
            )
        return False

    def _refuse_reserved_interactive_actor(self, actor: str) -> None:
        """Refuse a daemon mechanism name as a session identity (ruling 63).

        Reserved actor names belong to daemon-owned delivery identities, so a
        session claiming one must be rejected even when it has no registry
        row.  This boundary and the session-register entry point both call
        this check.  The comparison is exact equality on the actor segment of
        the canonical URI, never a prefix, so ``turn-hooks`` and
        ``dispatch-coordinator2`` stay ordinary agents.
        """

        parsed = parse_agent_uri(actor)
        name = actor if parsed is None else parsed[2]
        if name in RESERVED_AGENT_ACTOR_NAMES:
            raise DaemonRequestError(
                InvalidAgentNameError.code,
                f"actor {actor!r} is reserved for a daemon internal actor and "
                "cannot own an interactive session",
            )

    def _canonical_interactive_actor(self, actor: str) -> str:
        """Normalize one local MCP/Channel actor at the daemon boundary.

        CLI normalization is convenience only: older channel children and raw
        MCP clients can still call ``session.register`` directly.  The daemon
        is the authority that prevents those callers from creating a second,
        bare network registration beside the managed-harness four-segment
        form.  An already-canonical URI remains byte-for-byte unchanged for
        compatibility with callers that supply the complete identity, except
        that a name-form URI of a registered local agent whose record is
        minted under its ``a.`` id resolves to that official id address, so
        the session never registers a route the agent is not reached at.
        That holds for every agent this node hosts: the URI's owner segment
        picks the record (this daemon's owner, or a hosted visitor's owner),
        so a hosted carrier's name-form URI reaches its id address too.
        """

        if ":" not in actor:
            try:
                return self._agent_address(actor)
            except ValueError as error:
                raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        short = agent_uri_actor(actor)
        if short is None:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"actor {actor!r} is neither a bare actor name nor a canonical "
                "agent:<owner>:<machine>:<actor> URI",
            )
        parsed = parse_agent_uri(actor)
        if (
            parsed is not None
            and parsed[1] == self.node_id
            and not is_agent_id_segment(short)
        ):
            official = self._agent_address(short, parsed[0])
            if is_agent_id_segment(agent_uri_actor(official) or ""):
                return official
        return actor

    def _relocated_session_actor(self, actor: str, session_ref: object) -> str:
        """The id address an owner migration moved this name-form session to.

        An owner migration rewrites a session's stored address to the new
        owner, and a carrier launched before it keeps announcing the old
        owner's name-form URI.  Before id-form minting the session layer
        accepted that as the same session because the two URIs differed only
        in the owner segment.  Once the stored address is an ``a.`` id URI
        the name segment differs too, so the same session is recognised
        here, at the boundary: ``session_ref`` holds a session on this node
        stored at a live record's id URI, under another owner, whose record
        name is the name ``actor`` carries.  Anything else is returned
        unchanged and stays a supersede or a stale session.  With minting off
        no stored address is an id URI and this is the identity.
        """

        parsed = parse_agent_uri(actor)
        if (
            not isinstance(session_ref, str)
            or parsed is None
            or parsed[0] == self.owner
            or parsed[1] != self.node_id
            or is_agent_id_segment(parsed[2])
        ):
            return actor
        try:
            sessions = self._agent_session_domains.session.read_sessions()
        except Exception:  # noqa: BLE001 - the session fence answers this caller
            return actor
        stored = next(
            (item.actor for item in sessions if item.session_ref == session_ref),
            None,
        )
        located = None if stored is None else parse_agent_uri(stored)
        if (
            located is None
            or located[1] != parsed[1]
            or located[0] == parsed[0]
            or not is_agent_id_segment(located[2])
        ):
            return actor
        try:
            agent = self.agents.get(stored)
        except Exception:  # noqa: BLE001 - an unreadable record is no proof
            return actor
        if agent is None or agent.uri != stored or agent.actor != parsed[2]:
            return actor
        return stored

    def _mcp_actor(self, params: JsonObject) -> str:
        """Canonicalize only actors carried by fenced MCP/session requests.

        The same message methods also back unfenced CLI operations such as
        ``hyprial message ack --from <node>``.  Those actors address durable inbox rows
        verbatim and must not be rewritten as interactive-agent URIs.  This
        is the one canonicalization every session-fenced call goes through:
        the boundary name-to-address mapping, then an owner migration's
        relocation of the same session (``_relocated_session_actor``).
        """

        actor = _actor(params)
        if "sessionRef" not in params:
            return actor
        return self._relocated_session_actor(
            self._canonical_interactive_actor(actor), params.get("sessionRef")
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
        warning keeps it from being silent.  A row whose capability is not in
        the vocabulary never reaches this read: the registry skips it, and only
        the actor's grant journal (``agent.grants`` with ``audit``) shows it.

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
        """

        if not caller.hosted:
            return
        grants = self._visibility_grants(caller.uri)
        for target in targets:
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
        the answer.  Rows of a capability outside the vocabulary are already
        skipped by the registry and are not logged here.
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
