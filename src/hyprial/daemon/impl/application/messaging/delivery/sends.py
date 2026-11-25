"""User-target delivery: owner notices, the owner copy and blocking failures.

Decision notes (I7a, 2026-11-24; I7b-2):

- I7a (contract i7-0 v2 section 4): the receiver's callable for another
  owner's ``user:`` message is ``inbox_surface.send``'s ``_push_owner_copy``.
- I7b-2: no worker turn is sent on the worker's behalf; the daemon has no
  forward lane and no in-process relay mark.

Decision notes (R2, design ``user-im-binding-removal-tier2`` 8.1 and 8.2):

- Owner rulings 2026-11-19: the daemon itself sends nothing to the user.
  Its monitoring notices (quota and inbox watchdogs, the operator alarm,
  the OrgFS integrity notice, owner alerts) are logged at warn level with
  their full text and idempotency key, each under its own literal event
  name at its own call site (``_notice_fields`` gives the shared fields,
  ``OperatorAlarmLogPort`` the operator alarm), and reported delivered, so
  their callers do not retry.
- A routine, workflow or PAC report to this node's owner is sent by its
  owning agent: an in-process ``message.send`` from that agent to
  ``user:<owner>`` with no conversation id, so it lands in that agent's
  default conversation with the owner and follows every rule of an agent
  message (queue, record, case-3 push, the pinned-sender gap).  The owning
  agent is the report's sender when that is one of this node's agents;
  otherwise (the owner's own action, a remote origin, no agent) the report
  is logged as ``workflow.user_report.unsent`` and reported delivered.  A
  report for another owner keeps the user-delivery transport.
- The autoupdate restart notice and its IPC method ``autoupdate.notify`` are
  deleted; the upgrade CLI no longer waits for a notice before restarting.

Decision notes (I3, contract i7-0 v2 section 3.1, 2026-11-23):

- The body of the old ``route:`` delivery is
  ``network.route_push.push_to_adapter_route``, keyed by adapter and route
  name, with the attribution rule.

Decision notes (I4b, owner items Q0-a and Q0-d, 2026-11-23):

- The ``route:<adapter>:<route>`` front door is deleted: ``route:`` is no
  longer an address, so a send to it is ``UNSUPPORTED_TARGET`` at the
  admission gate and a queued outbox row with that recipient is
  undeliverable.  Text and attachments to a person go to ``user:<owner>``
  and the sender cell's push; the Lark reauth notice uses
  ``adapter.routes.send``.
"""

from __future__ import annotations

from __future__ import annotations
from typing import Any, TYPE_CHECKING
from uuid import uuid4
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import (
    lark_reply_adapter,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.kernel import (
    SESSION_CARRIER_SOURCES,
)
from hyprial.daemon.impl.inbox.contracts.summary import summary_from_text
from hyprial.daemon.impl.application.messaging.delivery.wake import observe_user_wake
from hyprial.daemon.impl.adapters.user_delivery import (
    UserDeliveryRequest,
    UserDeliveryResult,
    UserDeliveryTarget,
)
from hyprial.daemon.impl.inbox.links.io import (
    InboxIoError,
)
from hyprial.daemon.impl.composition  import (
    DomainCommandError,
)
from hyprial.daemon.impl.operations.session_ports  import (
    SessionMutationCompleted,
)
from hyprial.daemon.impl.configuration.identity import (
    classify_target_identity,
)
from hyprial.kernel import (
    TARGET_KIND_AGENT,
    TARGET_KIND_HOST,
    TARGET_KIND_UNKNOWN,
    TARGET_KIND_USER,
    canonical_user_uri,
)
if TYPE_CHECKING:
    from hyprial.daemon.impl.operations.watchdog_actor import AlertDeliveryOutcome



def _require_deliverable_send_target(target: str) -> None:
    """Send-time admission gate: only deliverable address forms pass.

    Post-alias-resolution a bare name IS a node address — the bare
    namespace is the node namespace.  Delivering to one lands in the node
    inbox, receipt-signed, with no consumer: the delivered-and-receipted
    silent hole.  Allen: reject outright, no backwards-compat shim; the
    error states facts only, no remediation instructions.
    """

    target_kind = classify_target_identity(target)
    if target_kind == TARGET_KIND_HOST:
        raise DaemonRequestError(
            ipc_errors.TARGET_IS_NODE,
            f"target {target!r} is a node address, not an agent: nodes are "
            "announced on the network but do not receive actor messages",
            {"target": target},
        )
    if target_kind == TARGET_KIND_UNKNOWN:
        raise DaemonRequestError(
            ipc_errors.UNSUPPORTED_TARGET,
            f"target {target!r} matches no deliverable address form; "
            "deliverable forms are agent:<owner>:<machine>:<agent> and "
            "user:<owner>",
            {"target": target},
        )

def _undeliverable_outbox_recipient(recipient: str) -> bool:
    """Mirror the CURRENT send-time admission gate for queued outbox rows.

    C1 (2026-08-22 dead-letter audit): the previous predicate deliberately
    kept bare node addresses deliverable "for the pending migration card" —
    that migration is DONE, and the send gate now rejects node-addressed
    sends outright (TARGET_IS_NODE: delivered-and-receipted, never consumed).
    Rows that can never pass today's gate are dead by construction:
    ``host`` and ``unknown`` are undeliverable; ``agent`` and ``user`` keep
    their queue semantics.  Existence questions
    (offline vs nonexistent) are NOT answered here — see the daemon's
    unresolvable predicate, which must never prune a merely-offline target.
    """

    if lark_reply_adapter(recipient) is not None:
        return False
    kind = classify_target_identity(recipient)
    return kind not in (TARGET_KIND_AGENT, TARGET_KIND_USER)



_REPORT_RETRYABLE = frozenset({
    ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
    ipc_errors.USER_DELIVERY_UNAVAILABLE,
    ipc_errors.USER_DELIVERY_TIMEOUT,
})


def _notice_fields(request: UserDeliveryRequest) -> dict[str, Any]:
    """The fields every logged daemon notice carries: its full text and key."""

    return {
        "messageId": request.message_id,
        "idempotencyKey": request.idempotency_key,
        "conversationId": request.conversation_id,
        "source": request.sender,
        "text": request.message,
    }


def _notice_logged(request: UserDeliveryRequest) -> UserDeliveryResult:
    return UserDeliveryResult(message_id=request.message_id, accepted=True)


class OperatorAlarmLogPort:
    """The user-delivery port shape that logs an operator alarm; nothing is sent."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        return self._app._log_operator_alarm(request)


class _UserDeliveryMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _workflow_deliver_user(
        self,
        recipient: str,
        text: str,
        message_id: str,
        sender: str | None = None,
    ) -> bool:
        """Escalation delivery to a ``user:<owner>`` target.

        This node's owner gets it from the owning agent
        (``_send_report_as_owning_agent``); another owner through the
        user-delivery transport.  True when accepted.  An outcome that may still change (a receipt that never
        arrived, a transport or inbox not wired yet, an unknown submit) is
        raised as a non-permanent ``InboxIoError`` so the report path can
        retry; a definite refusal raises a permanent error with its code."""

        try:
            user_target = UserDeliveryTarget.parse(recipient)
        except ValueError as error:
            raise InboxIoError(
                str(error), permanent=True, code=ipc_errors.INVALID_ARGUMENT
            ) from error
        request = UserDeliveryRequest(
            message_id=message_id,
            idempotency_key=f"workflow-escalate:{message_id}",
            owner=user_target.owner,
            sender=sender or canonical_user_uri(self.owner),
            message=text,
            conversation_id="workflow",
        )
        observe_user_wake(
            self, "workflow", message_id=request.message_id, sender=request.sender,
            recipient=recipient, text=text, conversation_id=request.conversation_id,
            idempotency_key=request.idempotency_key,
        )
        if user_target.owner == self.owner:
            return self._send_report_as_owning_agent(request, sender)
        if self._user_delivery is None:
            raise InboxIoError(
                "user delivery transport is unavailable",
                permanent=self._user_delivery_settled,
                code=ipc_errors.USER_DELIVERY_UNAVAILABLE,
            )
        outcome = self._user_delivery.deliver(request)
        if outcome.accepted:
            return True
        if outcome.code == ipc_errors.USER_DELIVERY_TIMEOUT:
            raise InboxIoError(
                f"delivery to {recipient} timed out waiting for the receiver's receipt",
                permanent=False,
                code=outcome.code,
            )
        raise InboxIoError(
            outcome.message or f"delivery to {recipient} was rejected",
            permanent=True,
            code=outcome.code or ipc_errors.USER_DELIVERY_FAILED,
        )

    def _deliver_report_to_user(
        self,
        recipient: str,
        text: str,
        message_id: str,
        sender: str,
    ) -> bool:
        """Delivery-layer ``user:<owner>`` split for run/PAC reports.

        Wired into ``InboxDeliveryIoAdapter`` so every report consumer — run
        reports, PAC notifications, legacy workflow effects — shares the one
        ``_workflow_deliver_user`` path.
        """

        return self._workflow_deliver_user(recipient, text, message_id, sender)

    def _quota_watchdog_deliver(
        self, idempotency_key: str, text: str
    ) -> "AlertDeliveryOutcome":
        """A quota alert is logged as ``quota_watchdog.notice``; nothing is sent."""

        from hyprial.daemon.impl.operations.watchdog_actor import AlertDeliveryOutcome

        request = UserDeliveryRequest(
            message_id=f"quota-watchdog-{uuid4().hex[:12]}",
            idempotency_key=idempotency_key,
            owner=self.owner,
            sender="quota-watchdog",
            message=text,
            conversation_id="quota-watchdog",
        )
        self._log("warn", "daemon", "quota_watchdog.notice", **_notice_fields(request))
        outcome = _notice_logged(request)
        return AlertDeliveryOutcome(
            outcome.accepted, outcome.definitely_not_sent
        )

    def _inbox_watchdog_deliver(
        self, idempotency_key: str, text: str
    ) -> "AlertDeliveryOutcome":
        """A mail-collection alert is logged as ``inbox_watchdog.notice``; nothing is sent."""

        from hyprial.daemon.impl.operations.watchdog_actor import AlertDeliveryOutcome

        request = UserDeliveryRequest(
            message_id=f"inbox-watchdog-{uuid4().hex[:12]}",
            idempotency_key=idempotency_key,
            owner=self.owner,
            sender="inbox-watchdog",
            message=text,
            conversation_id="inbox-watchdog",
        )
        self._log("warn", "daemon", "inbox_watchdog.notice", **_notice_fields(request))
        outcome = _notice_logged(request)
        return AlertDeliveryOutcome(
            outcome.accepted, outcome.definitely_not_sent
        )

    def _log_operator_alarm(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        """Log one rendered operator alarm as ``daemon.operator_alarm.notice``."""

        self._log("warn", "daemon", "daemon.operator_alarm.notice", **_notice_fields(request))
        return _notice_logged(request)

    def _send_report_as_owning_agent(
        self, request: UserDeliveryRequest, sender: str | None
    ) -> bool:
        """Send one report to this node's owner as its owning agent, else log it."""

        try:
            agent = self.agents.get(sender) if sender else None
        except Exception:  # noqa: BLE001 - an unknown sender owns no report
            agent = None
        if (
            agent is None
            or agent.owner != self.owner
            or agent.machine != self.node_id
        ):
            self._log(
                "warn", "daemon", "workflow.user_report.unsent", **_notice_fields(request)
            )
            return True
        if self._inbox is None:
            raise InboxIoError(
                "the daemon inbox is not composed yet",
                permanent=False,
                code=ipc_errors.USER_DELIVERY_UNAVAILABLE,
            )
        try:
            sent = self.handle(
                "message.send",
                {
                    "from": agent.uri,
                    "to": [canonical_user_uri(self.owner)],
                    "message": request.message,
                    "summary": summary_from_text(request.message),
                    "idempotencyKey": request.idempotency_key,
                },
                _trusted_message_origin="daemon",
            )
        except DaemonRequestError as error:
            raise InboxIoError(
                str(error),
                permanent=error.code not in _REPORT_RETRYABLE,
                code=error.code,
            ) from error
        deliveries = sent.get("deliveries") if isinstance(sent, dict) else None
        delivery = deliveries[0] if isinstance(deliveries, list) and deliveries else {}
        if isinstance(delivery, dict) and delivery.get("accepted"):
            return True
        code = str(delivery.get("code") or ipc_errors.USER_DELIVERY_FAILED)
        raise InboxIoError(
            f"the report to {request.owner} was not queued ({code})",
            permanent=code not in _REPORT_RETRYABLE,
            code=code,
        )

    def _blocking_failure_entity_token(self, recipient: str) -> str | None:
        projection = self.agents.projection(recipient)
        return None if projection is None else projection.entity_token

    def _commit_blocking_failure(
        self, recipient: str, reason: str, expected_entity_token: str
    ):
        projection = self.agents.projection(recipient)
        if (
            projection is None
            or projection.entity_token != expected_entity_token
        ):
            return None
        try:
            blocked, changed = self.agents.block_agent(
                projection.actor,
                reason=reason,
                expected_entity_token=expected_entity_token,
            )
        except DomainCommandError as error:
            if error.code == ipc_errors.AGENT_NOT_FOUND:
                return None
            raise
        if blocked.entity_token != expected_entity_token:
            return None
        projection = self.agents.projection(blocked.actor)
        block = None if projection is None else projection.block
        spec = next(
            (
                item
                for item in self.desired_state.load().harnesses
                if item.name == blocked.actor
            ),
            None,
        )
        if spec is not None:
            self._publish_restore_eligibility(
                spec=spec,
                entity_token=expected_entity_token,
                suppressed=False,
            )
        if changed:
            self._log(
                "warn",
                "daemon",
                "agent.blocked",
                actor=blocked.uri,
                reason=reason,
                **(
                    {"blockedAtMs": block.blocked_at_ms}
                    if block is not None
                    else {}
                ),
            )
        return blocked, changed, reason if block is None else block.reason

    def _carrier_session(
        self, actor: str, session_ref: str
    ) -> Any | None:
        """The persisted session one carrier-source caller owns.

        The closed carrier source set (claude-channel, pi-extension,
        codex-app-server -- hyprial.contracts.session.SESSION_CARRIER_SOURCES)
        shares the refresh/heartbeat protocol; only claude-channel sessions
        additionally carry an operational liveness lease.
        """

        return next(
            (
                session
                for session in self._agent_session_domains.session.read_sessions()
                if session.actor == actor
                and session.session_ref == session_ref
                and session.source in SESSION_CARRIER_SOURCES
            ),
            None,
        )

    def _call_session(self, command: object) -> SessionMutationCompleted:
        try:
            return self._agent_session_domains.call_session(
                command, SessionMutationCompleted
            )
        except DomainCommandError as error:
            raise DaemonRequestError(error.code, error.detail) from error
