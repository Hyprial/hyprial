"""Message-plane queries: pending/progress/status/ack, outbox views and pruning."""

from __future__ import annotations

from __future__ import annotations
import json
import threading
import time
from typing import Any, TYPE_CHECKING
from hyprial.kernel import AdmissionResult
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.kernel import (
    is_session_fetch,
)
from hyprial.daemon.impl.inbox.tracking.progress import decode_progress_event
from hyprial.kernel import (
    canonical_agent_uri,
    parse_agent_uri,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.application.messaging.delivery.sends import (
    _undeliverable_outbox_recipient,
)
from hyprial.daemon.impl.application.messaging.delivery.status_queries import (
    _outbox_entry_json,
    _query_entry,
)
from hyprial.daemon.impl.application.messaging.visibility import (
    _visible_message_origin,
)
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _actor,
    _optional_string_param,
    _required_string,
)


#: Longest a ``message.pending.wait`` holds one IPC slot.  The Channel loop
#: also services recovery pulses and due re-notifies once per pass, so this
#: bounds their delay too; it is not the message latency (a change returns
#: at once).
MESSAGE_WAIT_HOLD_SECONDS = 5.0
#: Concurrent waits allowed; one per live Channel session, well under the
#: 64 IPC client slots so held waits cannot starve CLI and adapter calls.
MESSAGE_WAIT_CAPACITY = 24
#: Longest single block on the inbox doorbell, so shutdown (which allows
#: in-flight IPC 5 s to settle) is observed promptly.
MESSAGE_WAIT_SLICE_SECONDS = 0.5
#: Minimum gap between a waiter's rescans after an inbox change that was not
#: for it.  Every mutation moves the shared version, so on a busy daemon this
#: caps one waiter at ~4 rescans/s -- about the cost of the 0.5 s polling it
#: replaces -- while the first change after a quiet spell still wakes at once.
MESSAGE_WAIT_RESCAN_SECONDS = 0.25


def new_message_wait_slots() -> threading.BoundedSemaphore:
    return threading.BoundedSemaphore(MESSAGE_WAIT_CAPACITY)


class _MessageQueriesMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _running_actor_uris(self) -> frozenset[str]:
        """Actors this node currently supervises as running.

        Liveness lives here and ⛔ not in the inbox store: the store knows
        mail, the daemon knows processes.  The same split `prune_outbox`
        already uses for its address predicates.
        """

        # A batch path like ps/top: without the request snapshot every online
        # actor's liveness probe costs one supervisor round trip plus one
        # full desired-state load per connector (card 259 / T4).  This runs
        # on every maintenance tick, so the per-actor fallback kept the
        # production daemon at ~0.8 core idle (2026-09-25, SIGUSR1 dump:
        # _watch_inbox_collection -> _running_actor_uris -> ... ->
        # _canonical_harness_uri -> desired_state.load, 273 agents).
        try:
            with self._worker_status_snapshot():
                statuses = self._actor_status_snapshot()
        except Exception:  # noqa: BLE001 - a watchdog must never break the tick
            return frozenset()
        live: set[str] = set()
        for status in statuses:
            if status.get("running") is True or status.get("status") == "online":
                actor = status.get("actor") or status.get("uri")
                if isinstance(actor, str) and actor:
                    live.add(actor)
        return frozenset(live)

    def _agent_is_blocked(self, actor: str) -> bool:
        """Compatibility query; the Agent owner supplies the committed fact."""
        return self.agents.is_blocked(actor)

    def _on_blocking_failure(self, recipient: str, failure_code: str) -> None:
        """Preserve the synchronous legacy entry through typed Agent commands.

        Production delivery uses BlockingFailureAuthority and its captured
        incarnation; this direct entry resolves the current incarnation at call.
        """
        reason = {
            "PROVIDER_USAGE_LIMIT": "provider-quota",
            "PROVIDER_AUTHENTICATION_FAILED": "credential-invalid",
        }.get(failure_code)
        if reason is None:
            return
        projection = self.agents.projection(recipient)
        if projection is None:
            return
        result = self._commit_blocking_failure(recipient, reason, projection.entity_token)
        if result is None or not result[1]:
            return
        agent = result[0]
        try:
            self._owner_alert_notifier(
                f"Agent {agent.actor} is blocked and needs human action: {reason}.",
                idempotency_key=f"agent-blocked:{agent.entity_token}:{reason}",
            )
        except Exception as error:  # noqa: BLE001 - preserve committed block
            self._log(
                "error", "daemon", "agent.block.notice_failed",
                actor=agent.uri, reason=reason, errorType=type(error).__name__,
            )

    def _unresolvable_outbox_recipient(self, recipient: str) -> bool:
        """C1: provably NONEXISTENT agent target — never a merely-offline one.

        The rule and why each clause exists:
        - must parse as a canonical agent URI (bare/host shapes are the
          scheme predicate's domain, not this one's);
        - the URI's machine must be THIS node: only this node's registry is
          authoritative for nonexistence.  A REMOTE node's agent that is
          offline looks identical to a dead one from here — never prunable;
        - a REGISTERED agent is real by definition (even down) → keep;
        - a PRESENT agent is alive right now → keep.
        Everything left over is a local identity nothing claims and nothing
        can revive: unresolvable.
        """

        parsed = parse_agent_uri(recipient)
        if parsed is None:
            return False
        owner, machine, actor = parsed
        if machine != self.node_id or owner != self.owner:
            return False
        if self.agents.get(actor) is not None:
            return False
        if self._presence is not None and self._presence.actor_online(recipient):
            return False
        return True

    def _ipc_message_query(self, params) -> Any:
        return self._message_query(params)

    def _ipc_message_pending_list(self, method, params, _trusted_message_origin: str | None = None) -> Any:
        caller = self._authenticated_message_caller(
            params,
            method=method,
            trusted_origin=_trusted_message_origin,
        )
        actor = caller.subject
        if is_session_fetch(params) and "sessionRef" not in params:
            # The explicit-fetch marker belongs to the session-carrier
            # contract: it commits custody (fetched stamp, sender-outbox
            # retirement, fetch receipt). A caller that signs no
            # sessionRef owns no custody, so an unfenced fetch must be
            # rejected BEFORE anything is committed -- the row stays
            # pending for whichever session actually holds the actor.
            raise DaemonRequestError(
                ipc_errors.STALE_SESSION,
                "an explicit inbox fetch (fetched=true) requires the "
                "session fence (sessionRef); an unfenced caller cannot "
                "commit another session's fetch",
            )
        keys = self._message_consumer_keys(params, actor=actor)
        messages: list[JsonObject] = []
        drain_notices = getattr(self._inbox, "drain_system_notices", None)
        notices = tuple(
            notice
            for key in keys
            for notice in (drain_notices(key) if callable(drain_notices) else ())
        )
        fetch_pending = getattr(self._inbox, "fetch_pending", None)
        if is_session_fetch(params) and callable(fetch_pending):
            pending = fetch_pending(actor)
        else:
            seen: set[str] = set()
            pending_list: list[Any] = []
            for key in keys:
                for item in self._inbox.pending_messages(key):
                    if item.message_id not in seen:
                        seen.add(item.message_id)
                        pending_list.append(item)
            pending = tuple(pending_list)
        if is_session_fetch(params) and self._fetch_receipt_publisher is not None:
            for message in pending:
                admitted = self._fetch_receipt_publisher.submit(message)
                if admitted is not AdmissionResult.ACCEPTED:
                    self._log(
                        "warn", "daemon", "inbox.fetch_receipt_hint_rejected",
                        reason=admitted.value,
                    )
                    break
        for message in (*notices, *pending):
            try:
                body = json.loads(message.payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = {}
            text = body.get("message", "") if isinstance(body, dict) else ""
            row: JsonObject = {
                "messageId": message.message_id,
                # Stable across daemon restart/resume; never use queue index.
                "deliveryId": message.message_id,
                "conversationId": message.conversation_id,
                "from": message.sender,
                # Every delivered row names both ends.  ``to`` matters when
                # one reader drains several keys (aliases, a session that
                # holds more than one actor): without it the reader cannot
                # tell which of its identities was addressed.
                "to": message.recipient,
                "intent": message.intent,
                "message": text if isinstance(text, str) else str(text),
            }
            row["origin"] = _visible_message_origin(body, message.sender)
            messages.append(row)
        return {"ok": True, "messages": messages, "daemonEpoch": self.epoch}

    def _ipc_message_pending_wait(self, method, params, _trusted_message_origin: str | None = None) -> Any:
        """Hold until this caller has a message it has not seen, or a short bound.

        The doorbell for Channel children: ``message.pending.list`` stays the
        only reader (and the only thing that drains notices); this method only
        answers *when* to call it.  ``knownMessageIds`` is what the caller saw
        on its last list, so a message that lands between that list and this
        wait returns at once instead of after the hold.  Nothing is written.
        """

        caller = self._authenticated_message_caller(
            params,
            method=method,
            trusted_origin=_trusted_message_origin,
        )
        keys = self._message_consumer_keys(params, actor=caller.subject)
        known_raw = params.get("knownMessageIds", [])
        if not isinstance(known_raw, list) or not all(
            isinstance(item, str) for item in known_raw
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "knownMessageIds must be a list of message ids",
            )
        known = frozenset(known_raw)
        hold_ms = params.get("holdMs", int(MESSAGE_WAIT_HOLD_SECONDS * 1000))
        if isinstance(hold_ms, bool) or not isinstance(hold_ms, int) or hold_ms < 0:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "holdMs must be a non-negative integer"
            )
        hold = min(hold_ms / 1000, MESSAGE_WAIT_HOLD_SECONDS)

        def unseen() -> bool:
            for key in keys:
                for item in (
                    *self._inbox.pending_messages(key),
                    *self._inbox.system_notices(key),
                ):
                    if item.message_id not in known:
                        return True
            return False

        def reply(*, changed: bool, held: bool) -> JsonObject:
            return {
                "ok": True,
                "changed": changed,
                "held": held,
                "daemonEpoch": self.epoch,
            }

        version = self._inbox.version
        if unseen():
            return reply(changed=True, held=False)
        if not self._message_waits.acquire(blocking=False):
            # Every wait pins one IPC client slot for its hold; past this
            # bound the caller polls on its own interval instead.
            return reply(changed=False, held=False)
        try:
            deadline = time.monotonic() + hold
            while True:
                remaining = deadline - time.monotonic()
                with self._ipc_clients_lock:
                    closing = self._ipc_closing
                if remaining <= 0 or closing or self.stop_event.is_set():
                    return reply(changed=False, held=True)
                current = self._inbox.wait_for_version(
                    version, min(MESSAGE_WAIT_SLICE_SECONDS, remaining)
                )
                if current == version:
                    continue
                version = current
                if unseen():
                    return reply(changed=True, held=True)
                # A daemon-wide burst moves the version for other actors too;
                # bound this waiter's rescans instead of following every one.
                time.sleep(min(MESSAGE_WAIT_RESCAN_SECONDS, max(0.0, remaining)))
        finally:
            self._message_waits.release()

    def _ipc_progress_list(self, method, params, _trusted_message_origin: str | None = None) -> Any:
        caller = self._authenticated_message_caller(
            params,
            method=method,
            trusted_origin=_trusted_message_origin,
        )
        actor = caller.subject
        keys = self._message_consumer_keys(params, actor=actor)
        delivery_id = _optional_string_param(params.get("deliveryId"), "deliveryId")
        since_seq = params.get("sinceSeq")
        if since_seq is not None and (
            isinstance(since_seq, bool)
            or not isinstance(since_seq, int)
            or since_seq < 0
        ):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "sinceSeq must be a non-negative integer"
            )
        list_progress = getattr(self._inbox, "list_progress_events", None)
        if not callable(list_progress):
            return {"ok": True, "events": [], "daemonEpoch": self.epoch}
        seen: set[str] = set()
        events: list[JsonObject] = []
        for key in keys:
            for message in list_progress(key, delivery_id=delivery_id):
                if message.message_id in seen:
                    continue
                seen.add(message.message_id)
                event = decode_progress_event(message.payload)
                if event is None:
                    continue
                if since_seq is not None and event.seq <= since_seq:
                    continue
                record: JsonObject = {
                    "messageId": message.message_id,
                    "from": message.sender,
                    "recipient": message.recipient,
                    "intent": message.intent,
                    "createdAtMs": message.created_at_ms,
                    **event.to_payload_dict(),
                }
                events.append(record)
        return {"ok": True, "events": events, "daemonEpoch": self.epoch}

    def _ipc_message_status(self, params, _trusted_message_origin: str | None = None) -> Any:
        return self._delivery_status_result(
            params, trusted_origin=_trusted_message_origin
        )

    def _ipc_message_ack(self, method, params, _trusted_message_origin: str | None = None) -> Any:
        caller = self._authenticated_message_caller(
            params,
            method=method,
            trusted_origin=_trusted_message_origin,
        )
        actor = caller.subject
        self._queue_agent_activity(actor)
        message_id = _required_string(params.get("messageId"), "messageId")
        result = self._inbox.ack(actor, message_id)
        if not result.acknowledged:
            # Rows written before the canonicalizing boundary carry the
            # verbatim key; try the fallback spellings before failing.
            for key in self._message_consumer_keys(params, actor=actor)[1:]:
                result = self._inbox.ack(key, message_id)
                if result.acknowledged:
                    break
        return {
            "ok": result.acknowledged,
            "messageId": result.message_id,
            "acknowledged": result.acknowledged,
            **({"code": result.code} if result.code else {}),
        }

    def _ipc_outbox_list(self) -> Any:
        return {
            "ok": True,
            "entries": [_outbox_entry_json(item) for item in self._inbox.outbox_items()],
        }

    def _ipc_outbox_prune(self, params) -> Any:
        dry_run = params.get("dryRun") is True
        pruned = self._inbox.prune_outbox(
            undeliverable=_undeliverable_outbox_recipient,
            unresolvable=self._unresolvable_outbox_recipient,
            dry_run=dry_run,
        )
        for item in pruned:
            self._log(
                "info",
                "daemon",
                "outbox.prune.dry-run" if dry_run else "outbox.pruned",
                messageId=item.message_id,
                correlationId=item.message_id,
                **({"node": "delivery-terminal"} if not dry_run else {}),
                recipient=item.recipient,
                reason=item.reason,
                attempts=item.attempts,
            )
        return {
            "ok": True,
            "dryRun": dry_run,
            "pruned": [
                {
                    "messageId": item.message_id,
                    "recipient": item.recipient,
                    "reason": item.reason,
                    "createdAtMs": item.created_at_ms,
                    "attempts": item.attempts,
                }
                for item in pruned
            ],
            "prunedCount": len(pruned),
            "remainingCount": self._inbox.outbox_count(),
        }

    def _message_query(self, params: JsonObject) -> JsonObject:
        """Read one local actor's inbox or outbox for a person, changing nothing.

        ``message.pending.list`` is the CONSUMER surface: even without the
        explicit fetch marker it drains the actor's system notices
        (``drain_system_notices`` deletes them), so a person "just looking"
        through it would eat notices meant for the agent.  This method reads
        the same rows through the non-draining readers only -- no fence, no
        fetch stamp, no receipt, no notice drain -- so the agent's next
        ``harness_read`` sees exactly what it would have seen.
        """

        view = _required_string(params.get("view"), "view")
        if view not in {"inbox", "outbox"}:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "view must be inbox or outbox"
            )
        # A person's query never speaks for a session: resolve the name the
        # same way a CLI send/read does, never through a session fence.
        lookup = {key: value for key, value in params.items() if key != "sessionRef"}
        keys = self._message_consumer_keys(lookup)
        actor = keys[0]
        entries: list[JsonObject] = []
        if view == "inbox":
            seen: set[str] = set()
            notices_reader = getattr(self._inbox, "system_notices", None)
            for key in keys:
                for notice in notices_reader(key) if callable(notices_reader) else ():
                    if notice.message_id not in seen:
                        seen.add(notice.message_id)
                        entries.append(_query_entry(notice, kind="notice"))
            for key in keys:
                for message in self._inbox.pending_messages(key):
                    if message.message_id not in seen:
                        seen.add(message.message_id)
                        entries.append(_query_entry(message, kind="message"))
        else:
            senders = set(keys)
            for item in self._inbox.outbox_items():
                if item.message.sender in senders:
                    entry = _query_entry(item.message, kind="outbox")
                    entry["attempts"] = item.attempts
                    entry["expiresAtMs"] = item.expires_at_ms
                    entries.append(entry)
        return {"ok": True, "actor": actor, "view": view, "entries": entries}

    def _message_consumer_actor(self, params: JsonObject) -> str:
        """Resolve an inbox consumer with the same rule as ``message.send``.

        A bare CLI name resolves exactly like a send sender: a registered
        alias reads its registered four-segment identity's key, an unknown
        bare name reads the verbatim key.  Fenced session calls still use
        :meth:`_mcp_actor` and are always canonical.  Reads union the other
        spellings (see ``_message_consumer_keys``) so rows written under a
        different era's key stay visible.
        """

        if "sessionRef" in params:
            return self._mcp_actor(params)
        return self._resolve_consumer_identity(_actor(params))

    def _message_consumer_keys(
        self, params: JsonObject, *, actor: str | None = None
    ) -> tuple[str, ...]:
        """Read keys for a consumer: resolved first, then the other eras' keys.

        Three spellings can hold rows for one typed name: the registered
        canonical URI (current), the verbatim bare name (pre-boundary), and
        — for an unregistered bare name — the local four-segment spelling
        the short-lived mint era (between A and the resolve-or-reject
        decision) wrote.  The minted probe is a READ key composed through
        the one URI constructor, never a new identity — a transitional
        compatibility window, removable once that era's rows are drained.  Union all that
        apply, resolved first; losing sight of durable rows is not an
        acceptable form of breaking compatibility.
        """

        actor = actor or self._message_consumer_actor(params)
        if "sessionRef" in params or "onBehalfOf" in params:
            return (actor,)
        raw = _actor(params)
        if actor != raw:
            return (actor, raw)
        if ":" not in raw:
            return (actor, canonical_agent_uri(self.owner, self.node_id, raw))
        return (actor,)
