"""User and route-target delivery: watchdog delivers, live user proxy, forwarding and blocking failures."""

from __future__ import annotations

from __future__ import annotations
import json
import os
from typing import Any, TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid4, uuid5
from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError
from hyprial.daemon.impl.adapters.lark.contracts.reply_bridge import (
    lark_reply_adapter,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.kernel import (
    SESSION_CARRIER_SOURCES,
)
from hyprial.daemon.impl.inbox import (
    InboxMessage,
)
from hyprial.daemon.impl.squire import (
    display_sender,
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
    TARGET_KIND_CHANNEL_ROUTE,
    TARGET_KIND_HOST,
    TARGET_KIND_UNKNOWN,
    TARGET_KIND_USER,
    canonical_agent_uri,
    canonical_user_uri,
)
from hyprial.daemon.impl.network.route_delivery  import (
    RouteDeliveryError,
    RouteResource,
    RouteTarget,
    find_gateway,
    is_route_target,
    map_lark_send_error,
    resolve_gateway_routes,
)
from hyprial.daemon.impl.runtime  import ForwardOutcome
if TYPE_CHECKING:
    from hyprial.daemon.impl.operations.watchdog_actor import AlertDeliveryOutcome

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _required_string,
)


FORWARD_TARGET_UNKNOWN = "FORWARD_TARGET_UNKNOWN"

_FORWARD_UNKNOWN_TARGET_CODES = frozenset(
    {
        ipc_errors.TARGET_IS_NODE,
        ipc_errors.UNSUPPORTED_TARGET,
        ipc_errors.AMBIGUOUS_TARGET,
        ipc_errors.INVALID_ARGUMENT,
        ipc_errors.ROUTE_ADAPTER_UNCONFIGURED,
        "ROUTE_NOT_CONFIGURED",
        "ROUTE_FANOUT_MEMBER_INVALID",
    }
)

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
            "deliverable forms are agent:<owner>:<machine>:<agent>, "
            "user:<owner>, and route:<adapter>:<route>",
            {"target": target},
        )

def _undeliverable_outbox_recipient(recipient: str) -> bool:
    """Mirror the CURRENT send-time admission gate for queued outbox rows.

    C1 (2026-08-22 dead-letter audit): the previous predicate deliberately
    kept bare node addresses deliverable "for the pending migration card" —
    that migration is DONE, and the send gate now rejects node-addressed
    sends outright (TARGET_IS_NODE: delivered-and-receipted, never consumed).
    Rows that can never pass today's gate are dead by construction:
    ``host`` and ``unknown`` are undeliverable; ``agent``/``user``/
    ``channel_route`` keep their queue semantics.  Existence questions
    (offline vs nonexistent) are NOT answered here — see the daemon's
    unresolvable predicate, which must never prune a merely-offline target.
    """

    if lark_reply_adapter(recipient) is not None:
        return False
    kind = classify_target_identity(recipient)
    return kind not in (
        TARGET_KIND_AGENT,
        TARGET_KIND_USER,
        TARGET_KIND_CHANNEL_ROUTE,
    )


class _UserDeliveryMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _workflow_deliver_user(
        self,
        recipient: str,
        text: str,
        message_id: str,
        sender: str | None = None,
    ) -> bool:
        """Escalation delivery to a ``user:<owner>`` target via the Squire
        user-delivery path.

        True when accepted.  A transient timeout (receipt never arrived) is
        raised as a non-permanent ``InboxIoError`` so the report path can
        retry instead of permanently failing; unconfigured or rejected
        delivery raises a permanent error with the receiver's code."""

        if self._user_delivery is None:
            # The runtime (and PAC with it) starts before the user transport
            # is assigned, and shutdown tears it down: a drain in either
            # window must retry.  Only a wired daemon without user delivery
            # configured is a permanent refusal.
            raise InboxIoError(
                "user delivery transport is unavailable",
                permanent=self._user_delivery_settled,
                code=ipc_errors.USER_DELIVERY_UNAVAILABLE,
            )
        try:
            user_target = UserDeliveryTarget.parse(recipient)
        except ValueError as error:
            raise InboxIoError(
                str(error), permanent=True, code=ipc_errors.INVALID_ARGUMENT
            ) from error
        outcome = self._user_delivery.deliver(
            UserDeliveryRequest(
                message_id=message_id,
                idempotency_key=f"workflow-escalate:{message_id}",
                owner=user_target.owner,
                sender=sender or canonical_user_uri(self.owner),
                message=text,
                conversation_id="workflow",
            )
        )
        if outcome.accepted:
            return True
        if outcome.code == ipc_errors.USER_DELIVERY_TIMEOUT:
            raise InboxIoError(
                f"owner-DM delivery for {recipient} timed out waiting for "
                "the receiver's squire receipt",
                permanent=False,
                code=outcome.code,
            )
        raise InboxIoError(
            outcome.message or f"owner-DM delivery for {recipient} was rejected",
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
        reports, PAC notifications, legacy workflow effects — shares one
        squire DM path instead of writing inbox messages no transport
        consumes (2026-09-14 defect class B). A missing or rejected owner-DM
        route raises a coded permanent error without queueing a doomed retry
        cycle.
        """

        return self._workflow_deliver_user(recipient, text, message_id, sender)

    def _quota_watchdog_deliver(
        self, idempotency_key: str, text: str
    ) -> "AlertDeliveryOutcome":
        """Watchdog alerts go to this daemon's owner through Squire."""

        from hyprial.daemon.impl.operations.watchdog_actor import AlertDeliveryOutcome

        if self._user_delivery is None:
            return AlertDeliveryOutcome(False, True)
        outcome = self._user_delivery.deliver(
            UserDeliveryRequest(
                message_id=f"quota-watchdog-{uuid4().hex[:12]}",
                idempotency_key=idempotency_key,
                owner=self.owner,
                sender="quota-watchdog",
                message=text,
                conversation_id="quota-watchdog",
            )
        )
        return AlertDeliveryOutcome(
            outcome.accepted, outcome.definitely_not_sent
        )

    def _inbox_watchdog_deliver(
        self, idempotency_key: str, text: str
    ) -> "AlertDeliveryOutcome":
        """Mail-collection alerts go to this daemon's owner through Squire."""

        from hyprial.daemon.impl.operations.watchdog_actor import AlertDeliveryOutcome

        if self._user_delivery is None:
            return AlertDeliveryOutcome(False, True)
        outcome = self._user_delivery.deliver(
            UserDeliveryRequest(
                message_id=f"inbox-watchdog-{uuid4().hex[:12]}",
                idempotency_key=idempotency_key,
                owner=self.owner,
                sender="inbox-watchdog",
                message=text,
                conversation_id="inbox-watchdog",
            )
        )
        return AlertDeliveryOutcome(
            outcome.accepted, outcome.definitely_not_sent
        )

    def _deliver_to_live_user_proxy(
        self, agent: str, request: UserDeliveryRequest
    ) -> UserDeliveryResult | None:
        """Submit receiver-owned user traffic to one live local proxy.

        ``None`` means the configured agent is not currently a running
        user-proxy and tells :class:`ReceiverUserDelivery` to use the existing
        Squire fallback.  A selected proxy returns a real submission result,
        including a refusal, so one request is never attempted on both paths.
        """

        if self._inbox is None:
            return None
        desired = self.desired_state.load()
        spec = next(
            (
                candidate
                for candidate in desired.harnesses
                if candidate.name == agent and candidate.harness == "user-proxy"
            ),
            None,
        )
        if spec is None:
            return None
        recipient = self._canonical_harness_uri(agent, spec, desired)
        if self._managed_worker_running(recipient, desired) is not True:
            return None
        # Through the daemon's own send boundary, not a raw inbox write: the
        # inbox authority gate keeps raw submits to a shrinking manifest.  A
        # daemon service is the authenticated sender.  onBehalfOf is decided
        # by the same check message.send applies to it, so every identity it
        # accepts (user:, local or remote agent URIs, channel:/route:) keeps
        # its attribution.  A sender it refuses is an attribution problem,
        # never a reason to drop the message: it goes out without onBehalfOf,
        # attributed in the text.  The frame is unauthenticated, so that label
        # claims nothing about the sender and the name is sanitised.
        params: JsonObject = {
            "from": self._dispatch_service_actor,
            "to": [recipient],
            "message": request.message,
            "conversationId": request.conversation_id,
            "idempotencyKey": request.idempotency_key,
        }
        try:
            params["onBehalfOf"] = self._resolve_on_behalf_actor(request.sender)
        except DaemonRequestError:
            params["message"] = (
                f"转述自 {display_sender(request.sender)}（发送方身份未能确认）："
                f"\n\n{request.message}"
            )
        try:
            sent = self.handle(
                "message.send",
                params,
                _trusted_message_origin="daemon",
            )
        except DaemonRequestError as error:
            return UserDeliveryResult(
                message_id=request.message_id,
                accepted=False,
                code=error.code,
                message=str(error),
                definitely_not_sent=True,
            )
        deliveries = sent.get("deliveries") if isinstance(sent, dict) else None
        delivery = deliveries[0] if isinstance(deliveries, list) and deliveries else {}
        accepted = bool(isinstance(delivery, dict) and delivery.get("accepted"))
        native = delivery.get("messageId") if isinstance(delivery, dict) else None
        return UserDeliveryResult(
            message_id=request.message_id,
            accepted=accepted,
            native_message_id=str(native) if accepted and native else None,
            code=None if accepted else str(delivery.get("code") or "PROXY_SEND_REFUSED"),
            message=None if accepted else "the live user-proxy refused the delivery",
        )

    def _on_usage_limit_failure(self, recipient: str) -> None:
        watchdog = self._quota_watchdog
        if watchdog is None:
            return
        alert = watchdog.observe_usage_limit_failure(recipient)
        if alert is not None:
            self._log("info", "daemon", "quota_watchdog.alerted", kind=alert.kind, key=alert.key)

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
                if item.name == blocked.actor and item.harness != "lark"
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

    def _forward_as_actor(
        self, original: InboxMessage, to: str, text: str
    ) -> ForwardOutcome:
        """Send one relayed turn on behalf of the worker receiving ``original``.

        The worker (user-proxy) only names the recipient; this is the same
        send boundary ``message.send`` uses, so aliases, ``user:`` and
        ``route:`` targets resolve exactly as they do for any agent.  The
        operation id is derived from the original row, so a redelivered
        turn resends idempotently instead of posting twice.
        """

        operation_id = f"forward:{original.message_id}"
        try:
            if is_route_target(to):
                # A route post can only speak as the bot, so it is attributed
                # in the text.  Attribute it to whoever the proxy is relaying
                # (the original sender), not to the proxy itself.
                self._deliver_route_target(
                    to,
                    text=text,
                    sender=self._relayed_sender(original),
                    conversation=original.conversation_id,
                    operation_id=operation_id,
                    index=0,
                    resources=(),
                )
                return ForwardOutcome(True)
            reply = self.handle(
                "message.send",
                {
                    "from": self._dispatch_service_actor,
                    "onBehalfOf": original.recipient,
                    "to": [to],
                    "message": text,
                    "conversationId": original.conversation_id,
                    "idempotencyKey": operation_id,
                },
                _trusted_message_origin="daemon",
            )
        except DaemonRequestError as error:
            code = (
                FORWARD_TARGET_UNKNOWN
                if error.code in _FORWARD_UNKNOWN_TARGET_CODES
                else error.code
            )
            return ForwardOutcome(False, code, f"{error.code}: {error}"[:500])
        deliveries = reply.get("deliveries") if isinstance(reply, dict) else None
        first = deliveries[0] if isinstance(deliveries, list) and deliveries else {}
        if isinstance(first, dict) and first.get("accepted") is True:
            return ForwardOutcome(True)
        code = first.get("code") if isinstance(first, dict) else None
        return ForwardOutcome(
            False,
            "HARNESS_TRANSIENT_FAILURE",
            f"forward to {to} was not accepted ({code or 'no code'})",
        )

    def _relayed_sender(self, original: InboxMessage) -> str:
        """Who a relayed turn speaks for: the original sender, not the relay.

        User deliveries reach a live proxy from this daemon's dispatch
        service, with the real sender in ``origin.onBehalfOf``.  That field
        is written only by message.send after its own onBehalfOf check
        (adapter metadata cannot set it), so it is trusted only when the row
        really came from this daemon's dispatch service.  Anything else, or
        a relay with no onBehalfOf, keeps the transport sender.
        """

        if original.sender != self._dispatch_service_actor:
            return original.sender
        try:
            payload = json.loads(original.payload)
        except (TypeError, ValueError):
            return original.sender
        origin = payload.get("origin") if isinstance(payload, dict) else None
        on_behalf_of = origin.get("onBehalfOf") if isinstance(origin, dict) else None
        if isinstance(on_behalf_of, str) and on_behalf_of:
            return on_behalf_of
        return original.sender

    def _deliver_route_target(
        self,
        target: str,
        *,
        text: str,
        sender: str,
        conversation: str,
        operation_id: str,
        index: int,
        resources: tuple[RouteResource, ...],
    ) -> list[JsonObject]:
        """Post to one ``route:<adapter>:<route>`` target, expanding fanout."""

        try:
            route_target = RouteTarget.parse(target)
        except ValueError as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
        try:
            configuration = self.load_persistent_configuration()
            gateway_config = find_gateway(
                configuration.channels, route_target.adapter
            )
            resolved = resolve_gateway_routes(gateway_config, route_target.route)
        except RouteDeliveryError as error:
            raise DaemonRequestError(error.code, str(error), error.data) from error
        gateway = self._route_lark_gateway(gateway_config)
        target_key = f"{operation_id}:{target}"
        # Allen: the receiver must be able to tell WHO sent it.  A Lark app
        # can only post as the bot, so the sender is attributed in the text
        # — the same 转述自 <full-actor>： marker the user:<owner> DM path
        # renders.  Text-only injection: the message body structure and any
        # file/image posts stay untouched.
        attributed_text = f"转述自 {display_sender(sender)}：\n\n{text}"
        deliveries: list[JsonObject] = []
        for item in resolved:
            member_key = (
                f"{target_key}:{item.route}" if len(resolved) > 1 else target_key
            )
            message_id = str(
                uuid5(NAMESPACE_URL, f"hyprial:route-send:{member_key}:{index}")
            )
            try:
                native_message_id = gateway.send_chat(
                    item.chat_id, attributed_text, idempotency_key=member_key
                )
                resource_deliveries: list[JsonObject] = []
                for resource_index, resource in enumerate(resources):
                    resource_key = f"{member_key}:resource:{resource_index}"
                    if resource.kind == "file":
                        evidence = gateway.send_chat_file(
                            item.chat_id,
                            resource.name,
                            resource.data,
                            media_type=resource.media_type,
                            idempotency_key=resource_key,
                        )
                    else:
                        evidence = gateway.send_chat_image(
                            item.chat_id,
                            resource.name,
                            resource.data,
                            media_type=resource.media_type,
                            idempotency_key=resource_key,
                        )
                    resource_deliveries.append(evidence)
            except LarkApiError as error:
                mapped = map_lark_send_error(
                    error,
                    adapter=gateway_config.name,
                    route=item.route,
                    chat_id=item.chat_id,
                )
                raise DaemonRequestError(
                    mapped.code, str(mapped), mapped.data
                ) from error
            except DaemonRequestError:
                raise
            except (NameError, ImportError):
                raise
            except Exception as error:  # noqa: BLE001 - harness failure boundary
                raise DaemonRequestError(
                    ipc_errors.ROUTE_SEND_FAILED,
                    f"route {item.route!r} (chat_id={item.chat_id}) send via "
                    f"adapter {gateway_config.name!r} failed: {error}",
                    {
                        "adapter": gateway_config.name,
                        "route": item.route,
                        "chatId": item.chat_id,
                    },
                ) from error
            deliveries.append(
                {
                    "target": (
                        target
                        if len(resolved) == 1
                        else f"route:{gateway_config.name}:{item.route}"
                    ),
                    "messageId": message_id,
                    "accepted": True,
                    "queued": False,
                    "routeDelivery": True,
                    "chatId": item.chat_id,
                    "nativeMessageId": native_message_id,
                    **(
                        {"resourceDeliveries": resource_deliveries}
                        if resource_deliveries
                        else {}
                    ),
                    **(
                        {"fanoutOf": target}
                        if len(resolved) > 1
                        else {}
                    ),
                }
            )
            self._log(
                "info",
                "daemon",
                "send.received",
                messageId=message_id,
                correlationId=message_id,
                node="daemon-send",
                conversationId=conversation,
                sender=sender,
                target=target,
                chatId=item.chat_id,
                nativeMessageId=native_message_id,
            )
        return deliveries

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

    def _deliver_autoupdate_restart_notification(
        self, params: JsonObject
    ) -> JsonObject:
        """Deliver and receipt-confirm the restart notice over the user bus."""

        if self._user_delivery is None:
            raise DaemonRequestError(
                ipc_errors.AUTOUPDATE_NOTIFICATION_UNAVAILABLE,
                "user delivery transport is unavailable",
            )
        old_version = _required_string(params.get("oldVersion"), "oldVersion")
        new_version = _required_string(params.get("newVersion"), "newVersion")
        resolved_tag = _required_string(params.get("resolvedTag"), "resolvedTag")
        resolved_commit = _required_string(
            params.get("resolvedCommit"), "resolvedCommit"
        )
        expected_seconds = params.get("expectedInterruptionSeconds", 90)
        if not isinstance(expected_seconds, int) or expected_seconds <= 0:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "expectedInterruptionSeconds must be positive"
            )
        # 3c116ad2 (P3): the field stays a validated positive int (the CLI
        # now derives it from this machine's desired state; 90 remains the
        # fallback for an older CLI), but the text no longer speaks a
        # duration -- measured fleet restores ran 2-10 minutes against the
        # old fixed-second promise, and the follow-up notice, not a number,
        # is what closes the interruption now.
        text = (
            f"Harness Bridge 已完成升级：{old_version} → {new_version} "
            f"({resolved_tag}@{resolved_commit[:12]})。即将重启 daemon，"
            "重启中，恢复完成会再通知。若长时间仍未恢复，请运行 "
            "hyprial daemon status --json 检查状态，并查看 "
            "$HYPRIAL_HOME/state/logs/daemon.jsonl 中本次重启之后最近一条 "
            "daemon.start.failed 的 phase/errorType/error；这些操作不会改配置。"
            "升级已安装，不要重复执行升级。"
        )
        sender = canonical_agent_uri(self.owner, self.node_id, "squire")
        message_id = str(
            uuid5(
                NAMESPACE_URL,
                "hyprial:autoupdate-restart:"
                f"{self.owner}:{old_version}:{new_version}:"
                f"{resolved_commit}:{os.getpid()}",
            )
        )
        outcome = self._user_delivery.deliver(
            UserDeliveryRequest(
                message_id=message_id,
                idempotency_key=(
                    "autoupdate-restart:"
                    f"{old_version}:{new_version}:{resolved_tag}:"
                    f"{resolved_commit}:{os.getpid()}"
                ),
                owner=self.owner,
                sender=sender,
                message=text,
                conversation_id="autoupdate-restart",
            )
        )
        if not outcome.accepted:
            raise DaemonRequestError(
                outcome.code or "AUTOUPDATE_NOTIFICATION_UNDELIVERED",
                outcome.message or "restart notification was not delivered",
                {"messageId": outcome.message_id},
            )
        self._log(
            "info",
            "autoupdate",
            "autoupdate.restart_notification.delivered",
            messageId=outcome.message_id,
            oldVersion=old_version,
            newVersion=new_version,
            resolvedCommit=resolved_commit,
        )
        return {
            "ok": True,
            "delivered": True,
            "deliveryConfirmed": True,
            "messageId": outcome.message_id,
            **(
                {"nativeMessageId": outcome.native_message_id}
                if outcome.native_message_id is not None
                else {}
            ),
        }
