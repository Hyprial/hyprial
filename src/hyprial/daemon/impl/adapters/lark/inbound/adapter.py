"""Lark event conversion, routing, acknowledgement, and receipt correlation."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hyprial.daemon import AlarmEmitter, node_owner_or_none
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import lark_reply_adapter
from hyprial.kernel import Logger, ipc_errors
from hyprial.kernel import configured_hyprial_home
from hyprial.identity import LazyUserStore, UserStore

from hyprial.daemon.impl.adapters.lark.contracts.messages import (
    canonical_lark_message_type,
    normalize_lark_message_content,
)
from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    HarnessPort,
    InboundOutcome,
    LarkApiPort,
    LarkInboundMessage,
    RouteDirectory,
)
from hyprial.daemon.impl.adapters.lark.credentials.identities import resolve_sender_identity
from hyprial.daemon.impl.identity import IdentityResolver, IdentityResolverError
from hyprial.daemon.impl.adapters.lark.inbound.inbound_runtime import LarkInboundRuntime
from hyprial.daemon.impl.adapters.lark.outbound.reactions import (
    ReactionEffectsRuntime,
)
from hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore
from hyprial.daemon.impl.adapters.lark.inbound.commands import _CommandResponseMixin
from hyprial.daemon.impl.adapters.lark.inbound.delivery import _DeliveryAckMixin
from hyprial.daemon.impl.adapters.lark.inbound.pipeline import (
    DEFAULT_TRANSIENT_RETRY_BACKOFF_INITIAL_SECONDS,
    DEFAULT_TRANSIENT_RETRY_BACKOFF_MAX_SECONDS,
    DEFAULT_TRANSIENT_RETRY_BUDGET_SECONDS,
    _InboundPipelineMixin,
)
from hyprial.daemon.impl.adapters.lark.inbound.reconcile import (
    DEFAULT_DEAD_LETTER_ALERT_THRESHOLD,
    _ReconcileDeadLetterMixin,
)
OperatorNotifier = Callable[[str, str], bool]
def _channel_actor_adapter_name(channel_actor_id: str) -> str:
    """The operator-facing adapter name from either channel URI shape.

    Three-segment ``channel:lark:<adapter>`` via the reply-bridge parser,
    four-segment ``channel:<owner>:<machine>:<adapter>`` via the one
    channel-URI deconstructor; anything else stays verbatim.
    """

    bridged = lark_reply_adapter(channel_actor_id)
    if bridged is not None:
        return bridged
    from hyprial.kernel import parse_channel_uri

    parsed = parse_channel_uri(channel_actor_id)
    return parsed[2] if parsed is not None else channel_actor_id

def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Lark event {name} must be a non-empty string")
    return value

def normalize_sdk_event(
    value: Any, *, logger: Logger | None = None
) -> LarkInboundMessage:
    """Normalize the official SDK's ``P2ImMessageReceiveV1`` model."""

    header = value.header
    event = value.event
    message = event.message
    sender = event.sender
    sender_id = sender.sender_id
    message_id = _required(message.message_id, "message.message_id")
    message_type = _required(message.message_type, "message.message_type")
    normalized = normalize_lark_message_content(
        message_type,
        _required(message.content, "message.content"),
        # The carrying message's own id, so image/file stand-ins print the
        # ``ref:<message_id>/<key>`` handle for ``hyprial adapter media get``.
        message_id=message_id,
    )
    if logger is not None:
        for shape in normalized.unparsed_post_shapes:
            logger.log(
                "warn",
                "lark.post.attachment_unparsed",
                nativeMessageId=message_id,
                shape={"tags": list(shape.tags), "keys": list(shape.keys)},
            )
    text = normalized.text
    mention_names: list[str] = []
    mention_open_ids: list[str] = []
    for mention in message.mentions or ():
        name = getattr(mention, "name", None)
        key = getattr(mention, "key", None)
        if isinstance(name, str) and name:
            mention_names.append(name)
        if isinstance(key, str) and key:
            text = text.replace(key, "")
        # ``MentionEvent.id`` is a UserId object; its open_id is the identity
        # key bot-mention routing compares against the app's own bot open_id.
        identity = getattr(mention, "id", None)
        open_id = getattr(identity, "open_id", None)
        if isinstance(open_id, str) and open_id:
            mention_open_ids.append(open_id)
    sender_union_id = getattr(sender_id, "union_id", None)
    return LarkInboundMessage(
        event_id=_required(header.event_id, "header.event_id"),
        message_id=message_id,
        chat_id=_required(message.chat_id, "message.chat_id"),
        chat_type=_required(message.chat_type, "message.chat_type"),
        text=text.strip(),
        sender_id=_required(sender_id.open_id, "sender.sender_id.open_id"),
        sender_type=_required(sender.sender_type, "sender.sender_type"),
        root_id=message.root_id,
        thread_id=getattr(message, "thread_id", None),
        reply_to=message.parent_id,
        create_time=message.create_time,
        message_type=normalized.message_type,
        content_status=normalized.status,
        mentions=tuple(mention_names),
        mention_open_ids=tuple(mention_open_ids),
        sender_union_id=(
            sender_union_id
            if isinstance(sender_union_id, str) and sender_union_id
            else None
        ),
    )


class LarkAdapter(
    _InboundPipelineMixin,
    _ReconcileDeadLetterMixin,
    _DeliveryAckMixin,
    _CommandResponseMixin,
):
    def __init__(
        self,
        *,
        state: LarkStateStore,
        lark: LarkApiPort,
        reaction_lark: LarkApiPort | None = None,
        reaction_effects: ReactionEffectsRuntime | None = None,
        harness: HarnessPort,
        routes: RouteDirectory,
        channel_actor_id: str,
        logger: Logger | None = None,
        notify_operator: OperatorNotifier | None = None,
        dead_letter_alert_threshold: int = DEFAULT_DEAD_LETTER_ALERT_THRESHOLD,
        transient_retry_budget: float = DEFAULT_TRANSIENT_RETRY_BUDGET_SECONDS,
        transient_retry_backoff_initial: float = (
            DEFAULT_TRANSIENT_RETRY_BACKOFF_INITIAL_SECONDS
        ),
        transient_retry_backoff_max: float = (
            DEFAULT_TRANSIENT_RETRY_BACKOFF_MAX_SECONDS
        ),
        utcnow: Callable[[], datetime] | None = None,
        org_context_path: Path | None = None,
        users: UserStore | LazyUserStore | None = None,
        identity_resolver: IdentityResolver | None = None,
    ) -> None:
        if dead_letter_alert_threshold < 1:
            raise ValueError("dead-letter alert threshold must be positive")
        if transient_retry_budget < 0.0:
            raise ValueError("transient retry budget must not be negative")
        if transient_retry_backoff_initial <= 0.0:
            raise ValueError("transient retry backoff start must be positive")
        self._state = state
        self._closing = threading.Event()
        # The per-machine user store, consulted first when naming a sender.
        # ``None`` (no users.sqlite3 on this machine) keeps #818's
        # identities-only resolution exactly.
        self._users = users
        resolver_home = (
            org_context_path.parent
            if org_context_path is not None
            else state.path.parent
        )
        resolver_owner = node_owner_or_none(hyprial_home=resolver_home)
        self._identity_resolver = identity_resolver or IdentityResolver(
            state_dir=state.path.parent,
            hyprial_home=resolver_home,
            owner=resolver_owner or "unknown",
            # The resolver opens users.sqlite3 itself, exactly as the daemon's
            # does: the worker's LazyUserStore cannot enumerate users, which
            # made every inbound lookup on the detached worker unavailable.
            legacy_path=state.path,
        )
        self._lark = lark
        self._reaction_lark = reaction_lark or lark
        self._reaction_lock = threading.Lock()
        self._reaction_effects = reaction_effects
        self._harness = harness
        self._routes = routes
        self._channel_actor_id = channel_actor_id
        # The operator-facing adapter name, for guidance that names the exact
        # CLI command instead of a dead-end "pin an agent" instruction.
        self._adapter_name = _channel_actor_adapter_name(channel_actor_id)
        self._logger = logger.bind(component="adapter") if logger else None
        self._notify_operator = notify_operator
        self._dead_letter_alert_threshold = dead_letter_alert_threshold
        self._transient_retry_budget = transient_retry_budget
        self._transient_retry_backoff_initial = transient_retry_backoff_initial
        self._transient_retry_backoff_max = max(
            transient_retry_backoff_max, transient_retry_backoff_initial
        )
        self._utcnow = utcnow or (lambda: datetime.now(UTC))
        self._org_context_path = org_context_path or (
            configured_hyprial_home()[0] / "org-context.md"
        )
        self._alarm = (
            AlarmEmitter(
                self._logger,
                deliver_human=self._deliver_native_alarm,
                claim=self._state.claim_alarm,
                clock_ms=lambda: int(self._utcnow().timestamp() * 1000),
            )
            if self._logger is not None
            else None
        )
        self._inbound = LarkInboundRuntime(
            self._adapter_name,
            processor=self._process_inbound,
        )

    def _process_inbound(
        self,
        message: LarkInboundMessage,
        suppress_guidance: bool,
        frozen_recovery: bool = False,
        recovery_deadline: float | None = None,
    ) -> InboundOutcome:
        return self._handle_inbound(
            message,
            suppress_guidance=suppress_guidance,
            frozen_recovery=frozen_recovery,
            recovery_deadline=recovery_deadline,
        )

    def close(self, timeout: float = 5.0) -> None:
        """Boundedly drain actor admission and its ordered effect lane."""

        self._closing.set()
        self._inbound.close(timeout)
        if self._reaction_effects is not None:
            self._reaction_effects.close(timeout)
        self._state.close()
        if self._users is not None:
            self._users.close()

    def __del__(self) -> None:
        # Tests and embedders historically treated LarkAdapter as a plain
        # value.  Keep that seam leak-free while explicit worker shutdown uses
        # close() for a real bounded drain.
        try:
            self.close(0.0)
        except Exception:
            pass

    def handle_sdk_event(self, event: Any) -> InboundOutcome:
        try:
            message = normalize_sdk_event(event, logger=self._logger)
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - normalization boundary
            # A message we cannot parse (non-text content, missing fields)
            # would otherwise vanish without a trace; keep what we can.
            self._dead_letter_unparsed(event, error)
            return InboundOutcome(status="normalize-error", ack_error=str(error))
        return self.handle_inbound(message)


    def send_owner_dm(
        self, open_id: str, text: str, *, idempotency_key: str
    ) -> str:
        """Send through this receiving user's own app-scoped adapter."""

        return self._lark.send_owner_dm(
            open_id, text, idempotency_key=idempotency_key
        )

    def handle_inbound(
        self, message: LarkInboundMessage, *, suppress_guidance: bool = False
    ) -> InboundOutcome:
        canonical_type = canonical_lark_message_type(message.message_type)
        if canonical_type != message.message_type:
            # Callers of this public seam may bypass the SDK normalizer. Do
            # not permit an attacker-controlled type/body to reach outbound
            # metadata or the durable dead-letter store through that path.
            message = replace(
                message,
                message_type="unknown",
                content_status="unsupported",
                text="[Unsupported Lark message type]",
            )
        # Official SDK callbacks may run concurrently.  The inbound actor owns
        # admission and in-flight event/message deduplication; blocking REST,
        # SQLite and daemon IPC execute on its bounded effect pool.
        return self._inbound.process(
            message,
            suppress_guidance=suppress_guidance,
        )


    #: message sent by an app arrives with the app's *bot* open_id, so it is
    #: recorded as the bot presence, not the app itself.
    _SENDER_IDENTITY_KINDS = {"user": "user", "app": "bot"}

    def _observe_sender_identity(self, message: LarkInboundMessage) -> None:
        """Passively record who was seen sending, without names.

        Identity collection is bookkeeping on the side of the inbound path:
        it must never fail a message, so every error is swallowed.  Events
        carry no sender display name; the name arrives later via
        ``identities sync`` or a member-change event and merges into the
        same row.
        """

        kind = self._SENDER_IDENTITY_KINDS.get(message.sender_type)
        if kind is None or not message.sender_id or message.sender_id == "unknown":
            return
        try:
            self._state.observe_identity(
                kind,
                message.sender_id,
                union_id=message.sender_union_id,
                source=f"event-sender:{message.chat_id}",
            )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - bookkeeping must never break inbound
            pass

    def _resolved_sender(self, message: LarkInboundMessage) -> dict[str, Any]:
        """Who sent this, from recorded identities -- for the agent to read.

        ``from`` on the harness message is this adapter (the routable actor a
        reply goes back through), so without this block an agent cannot tell
        one human from another, nor whether a message is its owner's.  The
        lookup never fails the message: an unanswerable one is reported as
        ``unresolved`` with ``lookupFailed``, never as a guessed name.
        """

        kind = self._SENDER_IDENTITY_KINDS.get(message.sender_type)
        unresolved: dict[str, Any] = {
            "kind": kind or message.sender_type or "unknown",
            "platformId": message.sender_id or None,
            "unionId": message.sender_union_id or None,
            "displayName": None,
            "owner": None,
            "standing": "unresolved",
            "source": None,
        }
        if kind is None or not message.sender_id or message.sender_id == "unknown":
            return unresolved
        if kind == "user" and not message.sender_union_id:
            return unresolved
        try:
            if self._users is None and not self._identity_resolver.is_owner_union(
                message.sender_union_id
            ):
                return resolve_sender_identity(
                    self._state,
                    kind=kind,
                    platform_id=message.sender_id,
                    union_id=message.sender_union_id,
                    users=None,
                )
            if kind == "user":
                return self._identity_resolver.resolve_sender(
                    message.sender_union_id,
                    open_id=message.sender_id,
                    adapter=self._adapter_name,
                )
            return resolve_sender_identity(
                self._state,
                kind=kind,
                platform_id=message.sender_id,
                union_id=message.sender_union_id,
                users=self._users,
            )
        except IdentityResolverError as error:
            if error.code in {
                ipc_errors.IDENTITY_UNBOUND,
                ipc_errors.IDENTITY_CONFLICT,
            }:
                return {
                    **unresolved,
                    **(
                        {"standing": "ambiguous"}
                        if error.code == ipc_errors.IDENTITY_CONFLICT
                        else {}
                    ),
                }
            return {**unresolved, "lookupFailed": True}
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - identity lookup must never break inbound
            return {**unresolved, "lookupFailed": True}

    def handle_member_change_event(self, event: Any) -> None:
        """Record identities from a chat member added/deleted event.

        These events carry name + open_id pairs bound together by the
        platform, so they are the one passive source that can attach display
        names safely.  A deletion still refreshes the identity (the person
        exists; only the membership changed).  Fires only when the app's
        platform-side event subscription includes the im.chat.member.user.*
        events; registration alone does not subscribe.  Never raises.
        """

        try:
            body = getattr(event, "event", None)
            chat_id = getattr(body, "chat_id", None)
            users = getattr(body, "users", None) or ()
            source = (
                f"member-event:{chat_id}"
                if isinstance(chat_id, str) and chat_id
                else "member-event:unknown"
            )
            for user in users:
                identity = getattr(user, "user_id", None)
                open_id = getattr(identity, "open_id", None)
                if not isinstance(open_id, str) or not open_id:
                    continue
                name = getattr(user, "name", None)
                union_id = getattr(identity, "union_id", None)
                self._state.observe_identity(
                    "user",
                    open_id,
                    display_name=(
                        name if isinstance(name, str) and name else None
                    ),
                    union_id=(
                        union_id
                        if isinstance(union_id, str) and union_id
                        else None
                    ),
                    source=source,
                )
        except (NameError, ImportError):
            raise
        except Exception:  # noqa: BLE001 - bookkeeping must never break the stream
            pass
