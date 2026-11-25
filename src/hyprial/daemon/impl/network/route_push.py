"""The push primitive keyed by (adapter, route), and the owner's copy per cell target.

Decision notes (I3, contract i7-0 v2 sections 3.1, 3.3 and 3.5; owner items
A-6 and A-7, approved 2026-11-23):

- ``push_to_adapter_route`` is the body of the old ``route:`` delivery
  without the URI parse: it loads the adapter configuration, applies the
  gateway's ``defaultRoute`` when no route is named, expands fanout, and
  posts each chat in order.  With ``stop_at_first_failure`` (the default,
  the ``route:`` front door) it stops at the first failing chat exactly as
  that door always did.  The cell push passes False (contract 3.3 row 10,
  critic P1-1): every chat of a fanout is attempted, so one unreachable
  chat does not hide the chats after it.  A failure is raised as
  ``PushFailure`` with a stable state and reason token, the chats that were
  posted, and the original exception chained, so the front door can answer
  its historic ``ROUTE_*`` codes byte for byte.  Without stopping, the
  failure raised after the loop is the first failing chat when some chat
  was posted; when none was, it is the first unknown outcome if there is
  one (row 10: any unknown makes the target unknown), else the first
  failure.
- A bot can only post as itself, so the receiver must be able to tell who
  sent the message (owner ruling): with ``attribute`` the text starts with
  ``转述自 <sender>：``.  Every caller passes it, the cell push included.
- ``push_user_copy`` posts the owner's copy to each target of a cell and
  never raises for one target.  The caller resolves the targets from the
  cell structure (``CopyTarget``), so this module needs no cells import.  It
  lives in ``network`` beside ``route_delivery`` rather than under
  ``application.messaging.delivery`` (contract 3.1 named that place): a new
  module there reaches the cells world through the application package and
  would need a new exception of the ``messaging-delivery-isolation``
  boundary, while ``network`` is held to the same isolation without one.
  Each
  target ends in a ``PushOutcome``.
  The failure table is fixed: a member that is not an adapter or has no
  ``adapter`` datum, an adapter that is not a configured gateway, no route
  and no default, or an unknown route is ``TARGET_UNRESOLVED``; anything
  that fails before a request leaves (configuration, secret, facade) is
  ``USER_DELIVERY_UNAVAILABLE``; a platform answer with an error code
  (``LarkApiError``) is ``USER_DELIVERY_FAILED``; any other exception out
  of the post is an unknown outcome, ``SUBMIT_OUTCOME_UNKNOWN``.  A fanout
  with at least one posted chat and a failing one counts as sent, with the
  first failing chat's code and the reason ``fanout-partial``, whatever
  the order of the chats.
- Resources follow the text of each chat, in order (I4b).  A resource that
  fails after its chat's text was posted does not undo the text: the chat
  counts as posted with the resources posted before the failure, the
  target is sent with the failing resource's code and the reason
  ``resource-partial``, and the remaining resources of that chat are not
  attempted.  A retry of ``message.send`` under the same idempotency key
  posts every item under its old key again, and Lark deduplicates on it.
- The key of a target is the caller's key followed by
  ``:push:<cell>:<member>:<route or empty>``; a fanout chat appends its
  member route.  The document revision is not part of it, so an edit
  between a send and its replay does not double-post a chat in both.  Lark
  deduplicates on that key; there is no local ledger and nothing retries
  a push (A-7).
- The actionable data of a platform refusal (``larkCode``, missing scopes,
  the authorization link) stays in the outcome for the daemon log only.
- ``push_to_route_name`` (I4b, 2026-11-24) serves ``adapter.routes.send``, the
  local operator's post to a route named by the adapter's own route name
  (the Lark reauth notice, owner item Q0-d).  The name must belong to exactly
  one configured adapter; none is ``route-unconfigured`` and several is
  ``route-ambiguous``.
- The outbound gateway is Lark only; a generic adapter kind is simply not a
  configured gateway here (F18 is out of scope).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkApiError
from hyprial.daemon.impl.adapters.user_delivery import display_sender
from hyprial.daemon.impl.ipc.params import JsonObject
from hyprial.daemon.impl.network.route_delivery import (
    RouteDeliveryError,
    RouteResource,
    find_gateway,
    map_lark_send_error,
    resolve_gateway_routes,
)
from hyprial.kernel import ipc_errors

SENT = "sent"
NOT_SENT = "not-sent"
UNKNOWN = "unknown"

_UNRESOLVED_REASONS = frozenset({
    "member-not-adapter", "member-adapter-missing", "adapter-unconfigured",
    "no-default-route", "route-unconfigured", "route-fanout-invalid",
})
_REASON_CODES = {
    "adapter-unavailable": ipc_errors.USER_DELIVERY_UNAVAILABLE,
    "provider-refused": ipc_errors.USER_DELIVERY_FAILED,
    "outcome-unknown": ipc_errors.SUBMIT_OUTCOME_UNKNOWN,
}


@dataclass(frozen=True, slots=True)
class RoutePost:
    """One chat posted by the primitive, with the key it was posted under."""

    route: str
    chat_id: str
    key: str
    native_message_id: str
    resource_deliveries: tuple[JsonObject, ...] = ()


@dataclass(frozen=True, slots=True)
class RoutePush:
    """Every chat of one (adapter, route) push, and the route actually used."""

    adapter: str
    route: str
    fanout: bool
    posts: tuple[RoutePost, ...]


class PushFailure(Exception):
    """One (adapter, route) push that did not complete; the cause is chained."""

    def __init__(
        self,
        state: str,
        reason: str,
        *,
        adapter: str,
        route: str | None,
        chat_id: str | None = None,
        posts: tuple[RoutePost, ...] = (),
    ) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason
        self.adapter = adapter
        self.route = route
        self.chat_id = chat_id
        self.posts = posts


@dataclass(frozen=True, slots=True)
class CopyTarget:
    """One cell push target as the caller resolved it from the cell structure."""

    member: str
    route: str | None
    adapter: str | None
    is_adapter_member: bool


@dataclass(frozen=True, slots=True)
class PushOutcome:
    """The result of one cell target: member, adapter, route and state."""

    member: str
    adapter: str | None
    route: str | None
    state: str
    code: str | None
    reason: str | None
    data: JsonObject | None = None
    lark_code: int | None = None
    native_message_ids: tuple[str, ...] = ()
    resource_deliveries: tuple[JsonObject, ...] = ()

    def log_fields(self) -> JsonObject:
        """The ``send.user_push`` ``targets[]`` row of this outcome."""

        return {
            "member": self.member, "adapter": self.adapter, "route": self.route,
            "state": self.state, "code": self.code, "reason": self.reason,
            **({} if self.lark_code is None else {"larkCode": self.lark_code}),
            **({} if self.data is None else {"data": self.data}),
        }


def push_to_adapter_route(
    app: Any,
    *,
    adapter: str,
    route: str | None,
    text: str,
    sender: str,
    idempotency_key: str,
    resources: tuple[RouteResource, ...] = (),
    attribute: bool = True,
    stop_at_first_failure: bool = True,
) -> RoutePush:
    """Post *text* to one adapter route (its default when *route* is None)."""

    try:
        configuration = app.load_persistent_configuration()
    except (NameError, ImportError):
        raise
    except Exception as error:  # noqa: BLE001 - pre-request failure boundary
        raise PushFailure(NOT_SENT, "adapter-unavailable", adapter=adapter, route=route) from error
    try:
        gateway_config = find_gateway(configuration.channels, adapter)
    except RouteDeliveryError as error:
        raise PushFailure(NOT_SENT, "adapter-unconfigured", adapter=adapter, route=route) from error
    route_name = route if route is not None else gateway_config.default_route
    if route_name is None:
        raise PushFailure(NOT_SENT, "no-default-route", adapter=adapter, route=None)
    try:
        resolved = resolve_gateway_routes(gateway_config, route_name)
    except RouteDeliveryError as error:
        reason = (
            "route-fanout-invalid" if error.code == "ROUTE_FANOUT_MEMBER_INVALID"
            else "route-unconfigured"
        )
        raise PushFailure(NOT_SENT, reason, adapter=adapter, route=route_name) from error
    try:
        gateway = app._route_lark_gateway(gateway_config)
    except (NameError, ImportError):
        raise
    except Exception as error:  # noqa: BLE001 - pre-request failure boundary
        raise PushFailure(NOT_SENT, "adapter-unavailable", adapter=adapter, route=route_name) from error
    attributed = f"转述自 {display_sender(sender)}：\n\n{text}" if attribute else text
    fanout = len(resolved) > 1
    posts: list[RoutePost] = []
    failures: list[tuple[str, str, Any, BaseException]] = []
    for item in resolved:
        key = f"{idempotency_key}:{item.route}" if fanout else idempotency_key
        try:
            native_message_id = gateway.send_chat(item.chat_id, attributed, idempotency_key=key)
        except (NameError, ImportError):
            raise
        except LarkApiError as error:
            failures.append((NOT_SENT, "provider-refused", item, error))
        except Exception as error:  # noqa: BLE001 - post outcome boundary
            failures.append((UNKNOWN, "outcome-unknown", item, error))
        else:
            delivered, resource_failure = _send_resources(gateway, item, resources, key)
            posts.append(RoutePost(item.route, item.chat_id, key, native_message_id, delivered))
            if resource_failure is None:
                continue
            failures.append(resource_failure)
        if stop_at_first_failure:
            break
    if failures:
        state, reason, item, error = _reported_failure(failures, posted=bool(posts))
        raise PushFailure(
            state, reason, adapter=gateway_config.name,
            route=item.route, chat_id=item.chat_id, posts=tuple(posts),
        ) from error
    return RoutePush(gateway_config.name, route_name, fanout, tuple(posts))


def push_to_route_name(
    app: Any,
    *,
    route: str,
    text: str,
    sender: str,
    idempotency_key: str,
) -> RoutePush:
    """Post *text* to the one configured adapter that has a route named *route*."""

    try:
        configuration = app.load_persistent_configuration()
    except (NameError, ImportError):
        raise
    except Exception as error:  # noqa: BLE001 - pre-request failure boundary
        raise PushFailure(NOT_SENT, "adapter-unavailable", adapter="", route=route) from error
    owners = sorted(
        gateway.name for gateway in configuration.channels.gateways
        if any(item.name == route for item in gateway.routes)
    )
    if len(owners) != 1:
        reason = "route-ambiguous" if owners else "route-unconfigured"
        raise PushFailure(NOT_SENT, reason, adapter=",".join(owners), route=route)
    return push_to_adapter_route(
        app, adapter=owners[0], route=route, text=text, sender=sender,
        idempotency_key=idempotency_key,
    )


def _reported_failure(
    failures: list[tuple[str, str, Any, BaseException]], *, posted: bool,
) -> tuple[str, str, Any, BaseException]:
    if posted:
        return failures[0]
    return next((failure for failure in failures if failure[0] == UNKNOWN), failures[0])


def _send_resources(
    gateway: Any, item: Any, resources: tuple[RouteResource, ...], key: str,
) -> tuple[tuple[JsonObject, ...], tuple[str, str, Any, BaseException] | None]:
    """Post each resource after the text; stop at the first that fails."""

    delivered: list[JsonObject] = []
    for index, resource in enumerate(resources):
        try:
            delivered.append(
                _send_resource(gateway, item.chat_id, resource, f"{key}:resource:{index}")
            )
        except (NameError, ImportError):
            raise
        except LarkApiError as error:
            return tuple(delivered), (NOT_SENT, "provider-refused", item, error)
        except Exception as error:  # noqa: BLE001 - post outcome boundary
            return tuple(delivered), (UNKNOWN, "outcome-unknown", item, error)
    return tuple(delivered), None


def _send_resource(gateway: Any, chat_id: str, resource: RouteResource, key: str) -> JsonObject:
    send = gateway.send_chat_file if resource.kind == "file" else gateway.send_chat_image
    return send(
        chat_id, resource.name, resource.data,
        media_type=resource.media_type, idempotency_key=key,
    )


def push_user_copy(
    app: Any,
    *,
    cell_id: str,
    targets: tuple[CopyTarget, ...],
    text: str,
    sender: str,
    target_key: str,
    attribute: bool = True,
    resources: tuple[RouteResource, ...] = (),
) -> tuple[PushOutcome, ...]:
    """Post the owner's copy to every target of one cell; one outcome per target."""

    return tuple(
        _push_one(
            app, cell_id=cell_id, target=target, text=text,
            sender=sender, target_key=target_key, attribute=attribute,
            resources=resources,
        )
        for target in targets
    )


def _push_one(
    app: Any,
    *,
    cell_id: str,
    target: CopyTarget,
    text: str,
    sender: str,
    target_key: str,
    attribute: bool,
    resources: tuple[RouteResource, ...] = (),
) -> PushOutcome:
    if not target.is_adapter_member:
        return _not_sent(target, None, "member-not-adapter")
    if not target.adapter:
        return _not_sent(target, None, "member-adapter-missing")
    key = f"{target_key}:push:{cell_id}:{target.member}:{target.route or ''}"
    try:
        pushed = push_to_adapter_route(
            app, adapter=target.adapter, route=target.route, text=text,
            sender=sender, idempotency_key=key, attribute=attribute,
            stop_at_first_failure=False, resources=resources,
        )
    except PushFailure as failure:
        return _failed(target, target.adapter, failure)
    return PushOutcome(
        member=target.member, adapter=target.adapter, route=pushed.route, state=SENT,
        code=None, reason=None,
        native_message_ids=tuple(post.native_message_id for post in pushed.posts),
        resource_deliveries=_posted_resources(pushed.posts),
    )


def _posted_resources(posts: tuple[RoutePost, ...]) -> tuple[JsonObject, ...]:
    return tuple(row for post in posts for row in post.resource_deliveries)


def _not_sent(target: CopyTarget, adapter: str | None, reason: str) -> PushOutcome:
    return PushOutcome(
        member=target.member, adapter=adapter, route=target.route, state=NOT_SENT,
        code=ipc_errors.TARGET_UNRESOLVED, reason=reason,
    )


def _failed(target: CopyTarget, adapter: str, failure: PushFailure) -> PushOutcome:
    if failure.reason in _UNRESOLVED_REASONS:
        return _not_sent(target, adapter, failure.reason)
    code = _REASON_CODES[failure.reason]
    cause = failure.__cause__
    lark_code: int | None = None
    data: JsonObject | None = None
    if isinstance(cause, LarkApiError) and failure.route and failure.chat_id:
        lark_code = cause.code
        data = dict(map_lark_send_error(
            cause, adapter=failure.adapter, route=failure.route, chat_id=failure.chat_id,
        ).data or {})
    return PushOutcome(
        member=target.member, adapter=adapter, route=failure.route,
        state=SENT if failure.posts else failure.state, code=code,
        reason=_partial_reason(failure),
        data=data, lark_code=lark_code,
        native_message_ids=tuple(post.native_message_id for post in failure.posts),
        resource_deliveries=_posted_resources(failure.posts),
    )


def _partial_reason(failure: PushFailure) -> str:
    if not failure.posts:
        return failure.reason
    if any(post.chat_id == failure.chat_id for post in failure.posts):
        return "resource-partial"
    return "fanout-partial"


def fold_outcomes(outcomes: tuple[PushOutcome, ...]) -> tuple[bool, str | None]:
    """``(pushed, pushCode)`` for one delivery entry (contract 3.4)."""

    if not outcomes:
        return False, ipc_errors.TARGET_UNRESOLVED
    first_code = next((item.code for item in outcomes if item.code), None)
    if any(item.state == SENT for item in outcomes):
        return True, first_code
    if any(item.state == UNKNOWN for item in outcomes):
        return False, ipc_errors.SUBMIT_OUTCOME_UNKNOWN
    return False, first_code or ipc_errors.TARGET_UNRESOLVED


__all__ = [
    "CopyTarget",
    "NOT_SENT",
    "PushFailure",
    "PushOutcome",
    "RoutePost",
    "RoutePush",
    "SENT",
    "UNKNOWN",
    "fold_outcomes",
    "push_to_adapter_route",
    "push_to_route_name",
    "push_user_copy",
]
