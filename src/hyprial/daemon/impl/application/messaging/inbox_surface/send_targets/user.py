"""message.send to one ``user:<owner>`` target, and the owner push.

Decision notes:

- Split out of ``_ipc_message_send`` (conversation plan slice P2) as a pure
  move.  It is a function over the application rather than a mixin method so
  the composed class gains no attribute and callers that bind
  ``_ipc_message_send`` onto a partial application keep working.
- The caller owns the ``deliveries`` list; this function appends to it at the
  same points the inline branch did.
- UA2: ``send_to_user_target`` is the path only for a ``user:`` target that
  is not queued here (another owner, or no local user agent).  A message to
  the own owner with the user agent on this node takes the ordinary queue
  block of ``message.send``; ``push_recorded_user_message`` then pushes the
  owner's copy under the same ``target_key`` and message id this function
  uses.
- R2 (design ``user-im-binding-removal-tier2`` 8.1 to 8.3, owner rulings 7.2
  and 7.5), I3 and I7a (contract i7-0 v2 sections 3.2 and 4, owner items
  A-1, A-6, A-7 and A-8): the copy for this node's owner is decided on this
  node from the sender's own cell, never from any IM account of the person.
  The sender's cell is bound when its structure has an adapter-role member,
  with or without an ``adapter`` datum:

  ===================================  ======================================
  sender                               copy
  ===================================  ======================================
  a local agent whose cell is bound    pushed by the daemon to that cell's
  (the subject is the agent address,   targets (``user_push_targets`` of the
  so the entry member, or a member     emitting member), ``via: cell``.  The
  address ``<name>@<member>``)         cell's data says who pushes, so no
                                       target (another member's ``user``
                                       connection, no entry member) is
                                       ``TARGET_UNRESOLVED`` and never a
                                       fall-back to the user agent cell
  a local agent whose cell cannot be   none: ``USER_DELIVERY_UNAVAILABLE``;
  read (no cell runtime, cell not      an unknown binding must not choose a
  loaded)                              chat
  unbound (an agent without adapter    pushed by the daemon to the targets of
  members, the operator, an operator   this node's user agent cell
  ``onBehalfOf`` such an agent)        (``cell_push_targets``), ``via:
                                       user-agent``; an empty user agent cell
                                       is ``TARGET_UNRESOLVED`` with no wait,
                                       an unreadable one
                                       ``USER_DELIVERY_UNAVAILABLE``
  an adapter address (any ``adapter:``  none: a message that came in through
  URI, with or without a chat          an adapter is never pushed back out
  segment, the prefix the caller       (``pushSkipped: sender-is-adapter``)
  check admits as an adapter caller)
  ===================================  ======================================

  A non-entry member launch speaks under its own address
  ``<name>@<member>`` (``agents_registry`` mints it from the launch's
  member), so the plain agent address always means the entry member.
- I3 and I7a, A-7: the push of a queued message is best-effort.  It is one
  inline attempt per target and never raises; the entry folds the targets:
  any sent is ``pushed: true`` (with the first other target's code as
  ``pushCode`` when one was not sent), none sent with an unknown outcome is
  ``SUBMIT_OUTCOME_UNKNOWN``, otherwise the first target's code.  A caller's
  own same-key replay pushes again under the same per-target keys.  The
  push-only path (nothing queued here) has the push as its only copy, so an
  unknown outcome raises ``SUBMIT_OUTCOME_UNKNOWN`` and no sent target is a
  refusal with the first code; no user agent on this node is a refusal
  with ``TARGET_UNRESOLVED``.  Both log ``send.user_push`` with one
  ``targets[]`` row per target and ``pushSkipped`` when there was none.
- UA2b item 2, unchanged: the message stays queued and recorded whatever
  the push does.
- The push-only path keeps another owner's ``user:`` target on the Zenoh
  hand-off to the machine that owns that person (owner ruling: unchanged
  until the cross-owner work); its entry still carries ``receiverAdapter``.
  On that machine ``push_owner_copy`` posts to its own user agent cell's
  targets (contract section 4): any sent is accepted, all not sent is a
  refusal that is not recorded, and none sent with an unknown outcome is
  ``USER_DELIVERY_FAILED`` that the receiver records, so a retry never
  posts again; an unexpected error there is the same ``USER_DELIVERY_FAILED``,
  because a post may already have gone out.
- I7b-2 removed the user-proxy worker and its forward mark: every queued
  ``user:<owner>`` send is pushed.
- I4b (2026-11-24): attachments ride the owner's copy.  Each pushed chat gets
  the text, then every resource under ``<push key>:resource:<i>``, and the
  entry lists the posted resources as ``resourceDeliveries``.  Another
  owner's ``user:`` target never carries attachments (``message.send``
  refuses them before this module runs).
- S3c (coordinator ruling, 2026-11-24): with the users profile gone, the
  machine that accepts another owner's hand-off is decided by
  ``accepts_owner_copy``: the request's owner is this node's owner (strict
  key equality) and this node's user agent cell names at least one push
  target.  Every other machine stays silent, so the sender's receipt wait
  ends with ``USER_DELIVERY_TIMEOUT`` as it did for a home without a
  profile.  One acceptor is the owner's configuration: an adapter member in
  the user agent cell of one machine only.  An unreadable cell is silent
  rather than answering, so it never races the machine that does accept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from hyprial.kernel import (
    ADAPTER_URI_PREFIX,
    DaemonRequestError,
    canonical_agent_uri,
    canonical_user_uri,
    ipc_errors,
    is_own_user_uri,
    member_actor_parts,
    parse_agent_uri,
)
from hyprial.daemon.impl.network.route_delivery import RouteResource
from hyprial.daemon.impl.network.route_push import (
    CopyTarget,
    PushOutcome,
    fold_outcomes,
    push_user_copy,
)
from hyprial.daemon.impl.cells.structure.lookup import cell_push_targets, user_push_targets
from hyprial.daemon.impl.cells.structure.parse import (
    ADAPTER_ROLE,
    CellStructure,
    parse_structure,
)
from hyprial.daemon.impl.composition.cells import cell_runtime
from hyprial.daemon.impl.application.messaging.delivery.wake import observe_user_request
from hyprial.daemon.impl.adapters.user_delivery import (
    UserDeliveryRequest,
    UserDeliveryResult,
    UserDeliveryTarget,
)
from hyprial.daemon.impl.ipc.params import JsonObject
from hyprial.daemon.impl.user_agent.ensure import local_user_agent
from hyprial.daemon.impl.user_agent.local_target import note_user_pushed

_NO_COPY_MESSAGE = "no copy of this message was pushed to the owner's chats"


def send_to_user_target(
    app: Any,
    deliveries: list[JsonObject],
    *,
    caller: Any,
    target: str,
    index: int,
    operation_id: str,
    source: str,
    text: str,
    reply_to: str | None,
    conversation: str,
    resources: tuple[RouteResource, ...] = (),
) -> None:
    """Deliver one message.send to a ``user:`` target and record its delivery."""

    target_key = f"{operation_id}:{target}"
    message_id = str(
        uuid5(NAMESPACE_URL, f"hyprial:send:{target_key}:{index}")
    )
    try:
        user_target = UserDeliveryTarget.parse(target)
    except ValueError as error:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, str(error)
        ) from error
    if is_own_user_uri(target, getattr(app, "owner", None)):
        _hand_off_unqueued(
            app, deliveries, caller=caller, target=target, owner=user_target.owner,
            target_key=target_key, message_id=message_id, source=source, text=text,
            reply_to=reply_to, conversation=conversation, resources=resources,
        )
        return
    if app._user_delivery is None:
        raise DaemonRequestError(
            ipc_errors.USER_DELIVERY_UNAVAILABLE,
            "user delivery transport is unavailable",
            {"target": target, "messageId": message_id},
        )
    user_request = UserDeliveryRequest(
        message_id=message_id,
        idempotency_key=target_key,
        owner=user_target.owner,
        sender=source,
        message=text,
        conversation_id=conversation,
    )
    observe_user_request(app, "send", user_request)
    outcome = app._user_delivery.deliver(user_request)
    if not outcome.accepted:
        raise DaemonRequestError(
            outcome.code or ipc_errors.USER_DELIVERY_FAILED,
            outcome.message or "user delivery failed",
            {"target": target, "messageId": outcome.message_id},
        )
    deliveries.append(
        {
            "target": target,
            "messageId": outcome.message_id,
            "accepted": True,
            "queued": False,
            "receiverAdapter": True,
            **(
                {"nativeMessageId": outcome.native_message_id}
                if outcome.native_message_id is not None
                else {}
            ),
            **({"duplicate": True} if outcome.duplicate else {}),
        }
    )
    app._log(
        "info",
        "daemon",
        "send.received",
        messageId=outcome.message_id,
        correlationId=outcome.message_id,
        node="daemon-send",
        conversationId=conversation,
        sender=source,
        target=target,
        **(
            {"nativeMessageId": outcome.native_message_id}
            if outcome.native_message_id is not None
            else {}
        ),
    )


def _hand_off_unqueued(
    app: Any,
    deliveries: list[JsonObject],
    *,
    caller: Any,
    target: str,
    owner: str,
    target_key: str,
    message_id: str,
    source: str,
    text: str,
    reply_to: str | None,
    conversation: str,
    resources: tuple[RouteResource, ...] = (),
) -> None:
    """This owner's push-only send: the push is the only copy."""

    del reply_to
    copy_source = _copy_source(app, caller.subject, source)
    outcomes, skipped, pushed, code = _cell_copy(
        app, copy_source, owner=owner, target_key=target_key,
        message_id=message_id, source=source, text=text, conversation=conversation,
        resources=resources,
    )
    _log_user_push(
        app, message_id=message_id, conversation=conversation, source=source,
        target=target, pushed=pushed, code=code, skipped=skipped, outcomes=outcomes,
    )
    if not pushed:
        unknown = code == ipc_errors.SUBMIT_OUTCOME_UNKNOWN
        raise DaemonRequestError(
            str(code),
            "the owner push may have arrived; retry with the same key" if unknown
            else _NO_COPY_MESSAGE,
            {"target": target, "messageId": message_id},
        )
    deliveries.append({
        "target": target, "messageId": message_id, "accepted": True,
        "queued": False, "via": _VIA[copy_source.kind], **({"pushCode": code} if code else {}),
        **_resource_fields(outcomes),
    })
    app._log(
        "info", "daemon", "send.received", messageId=message_id, correlationId=message_id,
        node="daemon-send", conversationId=conversation, sender=source, target=target,
    )


_BOUND = "bound"
_UNBOUND = "unbound"
_UNAVAILABLE = "unavailable"
_USER_AGENT = "user-agent"
_NO_USER_AGENT = "no-user-agent"
_ADAPTER_SENDER = "adapter-sender"
_VIA = {_BOUND: "cell", _USER_AGENT: "user-agent"}


@dataclass(frozen=True, slots=True)
class _SenderCell:
    """The cell whose data decides the copy, and which member emits."""

    kind: str
    cell_id: str | None = None
    structure: CellStructure | None = None
    member: str | None = None


def _is_adapter_address(address: object) -> bool:
    return isinstance(address, str) and address.startswith(ADAPTER_URI_PREFIX)


def _copy_source(app: Any, subject: str, source: str) -> _SenderCell:
    """The sender's own cell when it is bound or unreadable, else the user agent cell."""

    if _is_adapter_address(subject) or _is_adapter_address(source):
        return _SenderCell(_ADAPTER_SENDER)
    sender_cell = _sender_cell(app, subject)
    if sender_cell.kind != _UNBOUND:
        return sender_cell
    return _user_agent_cell(app)


def _sender_cell(app: Any, subject: str) -> _SenderCell:
    """The sender's cell for the owner copy (contract 3.2 rows 1 to 6)."""

    parsed = parse_agent_uri(subject)
    if parsed is None:
        return _SenderCell(_UNBOUND)
    owner, machine, short = parsed
    parts = member_actor_parts(short)
    address = subject if parts is None else canonical_agent_uri(owner, machine, parts[0])
    agent = app.agents.get(address)
    if agent is None or agent.owner != app.owner or agent.machine != app.node_id:
        return _SenderCell(_UNBOUND)
    cell = _lookup_cell(app, agent.cell_id)
    if cell is None:
        return _SenderCell(_UNAVAILABLE)
    if cell.kind != "agent" or cell.retired:
        return _SenderCell(_UNBOUND)
    try:
        structure = parse_structure(cell)
    except Exception:  # noqa: BLE001 - an unparsable cell is an unknown binding
        return _SenderCell(_UNAVAILABLE)
    if not structure.adapter_members:
        return _SenderCell(_UNBOUND)
    member = structure.entry if parts is None else parts[1]
    return _SenderCell(_BOUND, cell.cell_id, structure, member)


def _user_agent_cell(app: Any) -> _SenderCell:
    """This node's user agent cell as the source of an unbound sender's copy (I7a)."""

    agent = local_user_agent(app.agents.list(), str(getattr(app, "owner", "") or ""))
    if agent is None:
        return _SenderCell(_NO_USER_AGENT)
    cell = _lookup_cell(app, agent.cell_id)
    if cell is None or cell.kind != "agent" or cell.retired:
        return _SenderCell(_UNAVAILABLE)
    try:
        structure = parse_structure(cell)
    except Exception:  # noqa: BLE001 - an unparsable cell is an unknown binding
        return _SenderCell(_UNAVAILABLE)
    if any(member.role != ADAPTER_ROLE for member in structure.members):
        return _SenderCell(_UNAVAILABLE)
    return _SenderCell(_USER_AGENT, cell.cell_id, structure)


def _lookup_cell(app: Any, cell_id: str) -> Any:
    runtime = cell_runtime(app)
    try:
        return None if runtime is None else runtime.lookup(cell_id)
    except Exception:  # noqa: BLE001 - an unreadable cell is an unknown binding
        return None


def agent_cell_names_a_chat(app: Any) -> bool:
    """Whether some agent cell already composed here has an adapter-role member.

    The user agent cell is one of them.  Best-effort for the reply-path
    hint: a runtime that is not composed yet answers False, so the caller
    keeps its own rule.
    """

    runtime = getattr(app, "_cell_runtime", None)
    if runtime is None:
        return False
    try:
        return any(
            parse_structure(cell).adapter_members
            for cell in runtime.index().of_kind("agent")
        )
    except Exception:  # noqa: BLE001 - a hint never fails the send
        return False


_SKIPPED = {
    _UNAVAILABLE: ("cell-unavailable", ipc_errors.USER_DELIVERY_UNAVAILABLE),
    _NO_USER_AGENT: ("no-user-agent-target", ipc_errors.TARGET_UNRESOLVED),
    _ADAPTER_SENDER: ("sender-is-adapter", ipc_errors.TARGET_UNRESOLVED),
}


def _cell_copy(
    app: Any,
    sender_cell: _SenderCell,
    *,
    owner: str,
    target_key: str,
    message_id: str,
    source: str,
    text: str,
    conversation: str,
    resources: tuple[RouteResource, ...] = (),
) -> tuple[tuple[PushOutcome, ...], str | None, bool, str | None]:
    """Push the owner's copy to the targets of the cell that decides it.

    Returns the per-target outcomes, the ``pushSkipped`` reason when there
    was no target, and the folded ``(pushed, pushCode)``.
    """

    observe_user_request(app, "send", UserDeliveryRequest(
        message_id=message_id, idempotency_key=target_key, owner=owner,
        sender=source, message=text, conversation_id=conversation,
    ))
    if sender_cell.kind in _SKIPPED:
        skipped, code = _SKIPPED[sender_cell.kind]
        return (), skipped, False, code
    structure = sender_cell.structure
    assert structure is not None and sender_cell.cell_id is not None
    if sender_cell.kind == _USER_AGENT:
        targets = cell_push_targets(structure)
        if not targets:
            return (), "no-user-agent-target", False, ipc_errors.TARGET_UNRESOLVED
    elif sender_cell.member is None:
        return (), "sender-member-unknown", False, ipc_errors.TARGET_UNRESOLVED
    else:
        targets = user_push_targets(structure, sender_cell.member)
        if not targets:
            return (), "sender-cell-silent", False, ipc_errors.TARGET_UNRESOLVED
    outcomes = push_user_copy(
        app, cell_id=sender_cell.cell_id, targets=_copy_targets(structure, targets),
        text=text, sender=source, target_key=target_key, resources=resources,
    )
    pushed, code = fold_outcomes(outcomes)
    return outcomes, None, pushed, code


def _resource_fields(outcomes: tuple[PushOutcome, ...] | None) -> JsonObject:
    posted = [row for item in outcomes or () for row in item.resource_deliveries]
    return {"resourceDeliveries": posted} if posted else {}


def _copy_targets(structure: CellStructure, targets: tuple[Any, ...]) -> tuple[CopyTarget, ...]:
    return tuple(_copy_target(structure, target.member, target.route) for target in targets)


def _copy_target(structure: CellStructure, name: str, route: str | None) -> CopyTarget:
    member = structure.member(name)
    return CopyTarget(
        member=name, route=route,
        adapter=None if member is None else member.adapter,
        is_adapter_member=member is not None and member.role == ADAPTER_ROLE,
    )


def push_owner_copy(app: Any, request: UserDeliveryRequest) -> UserDeliveryResult | None:
    """Post another owner's message to this node's user agent cell (contract section 4).

    None when the user agent cell names no target (the receiver answers
    ``TARGET_UNRESOLVED``).  Never raises for one target: any sent is
    accepted, none sent with an unknown outcome is ``USER_DELIVERY_FAILED``
    that may have arrived (the receiver records it, so it is not attempted
    again), and the rest is a refusal that was definitely not sent.  An
    unexpected error is ``USER_DELIVERY_FAILED`` that may have arrived, so it
    is recorded and never posted again.
    """

    try:
        return _push_owner_copy(app, request)
    except Exception as error:
        _log_user_push(
            app, message_id=request.message_id, conversation=request.conversation_id,
            source=request.sender, target=canonical_user_uri(request.owner), pushed=False,
            code=ipc_errors.USER_DELIVERY_FAILED, skipped=None, outcomes=None,
            error_type=type(error).__name__,
        )
        return UserDeliveryResult(
            message_id=request.message_id, accepted=False,
            code=ipc_errors.USER_DELIVERY_FAILED,
            message="the push on the receiving node failed unexpectedly; it is not attempted again",
            definitely_not_sent=False,
        )


def accepts_owner_copy(app: Any, owner: str) -> bool:
    """Whether this node accepts another node's hand-off for *owner* (S3c).

    The wire answer stays silent when it does not; an own-owner refusal
    because the user agent cell cannot be read is logged as
    ``user-delivery.not-accepted`` so the sender's timeout has a cause on
    this node.
    """

    if not owner or owner != getattr(app, "owner", None):
        return False
    try:
        source = _user_agent_cell(app)
    except Exception as error:  # noqa: BLE001 - an unreadable cell never accepts
        _log_not_accepted(app, owner, type(error).__name__)
        return False
    if source.kind == _UNAVAILABLE:
        _log_not_accepted(app, owner, None)
    if source.kind != _USER_AGENT or source.structure is None:
        return False
    return bool(cell_push_targets(source.structure))


def _log_not_accepted(app: Any, owner: str, error_type: str | None) -> None:
    app._log(
        "warn", "user-delivery", "user-delivery.not-accepted",
        owner=owner, reason="cell-unavailable",
        **({} if error_type is None else {"errorType": error_type}),
    )


def _push_owner_copy(app: Any, request: UserDeliveryRequest) -> UserDeliveryResult | None:
    source = _user_agent_cell(app)
    if source.kind == _UNAVAILABLE:
        return UserDeliveryResult(
            message_id=request.message_id, accepted=False,
            code=ipc_errors.USER_DELIVERY_UNAVAILABLE,
            message="the user agent cell of the receiving node cannot be read",
            definitely_not_sent=True,
        )
    if source.kind != _USER_AGENT or source.structure is None or source.cell_id is None:
        return None
    targets = cell_push_targets(source.structure)
    if not targets:
        return None
    outcomes = push_user_copy(
        app, cell_id=source.cell_id, targets=_copy_targets(source.structure, targets),
        text=request.message, sender=request.sender, target_key=request.idempotency_key,
    )
    pushed, code = fold_outcomes(outcomes)
    _log_user_push(
        app, message_id=request.message_id, conversation=request.conversation_id,
        source=request.sender, target=canonical_user_uri(request.owner), pushed=pushed,
        code=code, skipped=None, outcomes=outcomes,
    )
    if pushed:
        native = next((nid for item in outcomes for nid in item.native_message_ids), None)
        return UserDeliveryResult(
            message_id=request.message_id, accepted=True, native_message_id=native,
        )
    if code == ipc_errors.SUBMIT_OUTCOME_UNKNOWN:
        return UserDeliveryResult(
            message_id=request.message_id, accepted=False,
            code=ipc_errors.USER_DELIVERY_FAILED,
            message="the push outcome on the receiving node is unknown; it is not attempted again",
            definitely_not_sent=False,
        )
    return UserDeliveryResult(
        message_id=request.message_id, accepted=False, code=code,
        message="no copy was pushed to the user agent cell's chats",
        definitely_not_sent=True,
    )


def _log_user_push(
    app: Any,
    *,
    message_id: str,
    conversation: str,
    source: str,
    target: str,
    pushed: bool,
    code: str | None,
    skipped: str | None,
    outcomes: tuple[PushOutcome, ...] | None,
    error_type: str | None = None,
) -> None:
    app._log(
        "info" if pushed and code is None else "warn",
        "daemon",
        "send.user_push",
        messageId=message_id,
        conversationId=conversation,
        sender=source,
        target=target,
        pushed=pushed,
        **({} if code is None else {"code": code}),
        **({} if skipped is None else {"pushSkipped": skipped}),
        **({} if outcomes is None else {"targets": [item.log_fields() for item in outcomes]}),
        **({} if error_type is None else {"errorType": error_type}),
    )


def push_recorded_user_message(
    app: Any,
    *,
    caller: Any,
    target: str,
    operation_id: str,
    message_id: str,
    source: str,
    text: str,
    conversation: str,
    resources: tuple[RouteResource, ...] = (),
) -> JsonObject:
    """Push the owner's copy of a queued ``user:`` message (UA2, R2, I3, I7a).

    Returns the fields for the delivery entry: ``pushed`` with ``via``
    (``cell`` or ``user-agent``) and, when some copy was not posted,
    ``pushCode``.  It never raises (A-7): an exception while reading a cell
    or pushing its copy answers ``pushed: false`` with
    ``USER_DELIVERY_UNAVAILABLE`` and ``pushSkipped: cell-unavailable``, and
    its type is logged as ``errorType``.
    """

    owner = UserDeliveryTarget.parse(target).owner
    target_key = f"{operation_id}:{target}"
    error_type: str | None = None
    try:
        copy_source = _copy_source(app, caller.subject, source)
    except Exception as error:  # noqa: BLE001 - A-7: the queued send never fails on its copy
        copy_source, error_type = _SenderCell(_UNAVAILABLE), type(error).__name__
    try:
        outcomes, skipped, pushed, push_code = _cell_copy(
            app, copy_source, owner=owner, target_key=target_key,
            message_id=message_id, source=source, text=text, conversation=conversation,
            resources=resources,
        )
    except Exception as error:  # noqa: BLE001 - A-7: the queued send never fails on its copy
        outcomes, skipped, pushed, push_code = (
            (), "cell-unavailable", False, ipc_errors.USER_DELIVERY_UNAVAILABLE,
        )
        error_type = type(error).__name__
    _log_user_push(
        app, message_id=message_id, conversation=conversation, source=source,
        target=target, pushed=pushed, code=push_code, skipped=skipped, outcomes=outcomes,
        error_type=error_type,
    )
    if not pushed:
        return {"pushed": False, "pushCode": push_code}
    note_user_pushed(app, message_id)
    return {
        "pushed": True, "via": _VIA[copy_source.kind],
        **({"pushCode": push_code} if push_code else {}),
        **_resource_fields(outcomes),
    }
