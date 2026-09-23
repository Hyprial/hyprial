"""Fail-loud notices for a request whose provider never produced a result.

Policy (Allen, 2026-09-21): *provider/model availability is the user's own
guarantee*, so dispatch assumes every candidate is usable and never probes or
routes around one that is down (see ``hyprial.dispatch.matrix``).  What must
not happen is silence: when a request cannot produce a receipt -- quota
exhausted, API connection lost, a worker stuck reconnecting, or an approval
prompt that parks the turn with no event -- the **sender that is waiting for
that receipt** must be told, with evidence identifying *this* attempt.

Three facts shape this module:

* the notice is addressed to the original sender, derived from the request's
  authoritative routing fields -- never guessed as ``owner``, and never sent
  to the stuck worker so it can relay;
* the notice carries a per-attempt identity (delivery id + attempt generation)
  and a nonce minted here, so a receiver can check that the text came from
  this attempt rather than from a stale aggregate;
* the notice is a failure/no-progress report.  It is never dressed up as the
  successful business reply, and it never acknowledges or settles the original
  delivery -- settlement semantics stay exactly where they were.

The module is deliberately free of I/O: it builds :class:`InboxMessage`
values, and the daemon runtime owns submission, retry and logging.

No cross-event frequency cap is introduced here.  The plan makes that the
simplest permitted implementation and requires a visible "N suppressed"
summary only if such a cap exists; adding one would put a new silence in the
path it is supposed to make loud, so the only dedupe is the per-attempt once
key (one notice per attempt, replay-safe).  The retained owner-side
aggregation (quota watchdog hold/dedupe) is untouched and is not this path's
gate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid4, uuid5

from hyprial.inbox.api import DeliveryLifecycle, InboxMessage

#: Externally observed P95 *completed* turn durations, rounded up to the next
#: whole minute, used as the no-progress reporting budget per harness.  The
#: derivation (6309 paired completed turns, fixed window 2026-09-14..
#: 2026-09-21) is recorded in the accepted plan; the numbers are data, not a
#: preference: pi 470.115s -> 480, claude 1619.617s -> 1620, codex 2479.320s
#: -> 2520.  These bound when a *silent* attempt is reported, never who is
#: selected: changing availability cannot move a selection (matrix policy).
NO_PROGRESS_BUDGET_SECONDS: dict[str, int] = {
    "pi": 480,
    "claude": 1620,
    "codex": 2520,
}

#: A harness with no observed distribution (or none known before the worker
#: is even connected) uses the largest observed P95, so the fallback can only
#: be later than a measured budget: no measured harness is reported earlier
#: than its own evidence would justify.
DEFAULT_NO_PROGRESS_BUDGET_SECONDS = 2520

_NOTICE_NAMESPACE = uuid5(NAMESPACE_URL, "hyprial://provider-availability-loud")

NOTICE_KIND_UNAVAILABLE = "provider-unavailable"
NOTICE_KIND_NO_PROGRESS = "provider-no-progress"


def no_progress_budget_seconds(harness: str | None) -> int:
    """Reporting deadline for one harness; unknown harnesses get the maximum."""

    if harness is None:
        return DEFAULT_NO_PROGRESS_BUDGET_SECONDS
    return NO_PROGRESS_BUDGET_SECONDS.get(harness, DEFAULT_NO_PROGRESS_BUDGET_SECONDS)


@dataclass(frozen=True, slots=True)
class AttemptIdentity:
    """Who is waiting, which worker was asked, and which attempt this is.

    ``sender`` is the recipient of the notice: the actor whose request is
    outstanding.  ``worker`` is the harness actor that accepted (or was asked
    to accept) the delivery.  ``generation`` is the real execution attempt --
    a delivery id alone is not enough, because one delivery id can host more
    than one started/completed pairing.
    """

    delivery_id: str
    conversation_id: str
    sender: str
    worker: str
    harness: str | None
    generation: int
    observed_at_ms: int

    @property
    def attempt_id(self) -> str:
        return f"{self.delivery_id}#{self.generation}"


def _message(
    identity: AttemptIdentity,
    *,
    kind: str,
    key: str,
    text: str,
    fields: dict[str, object],
) -> InboxMessage:
    nonce = uuid4().hex
    payload = {
        "message": text,
        "notification": kind,
        # Correlatable, per-attempt evidence.  The nonce is minted here so a
        # receiver can tell "this attempt's message" from a replayed or
        # aggregated one; the ids let it join the notice back to its request.
        "deliveryId": identity.delivery_id,
        "attemptId": identity.attempt_id,
        "attempt": identity.generation,
        # The party being told about the failure (the notice's recipient),
        # named for what it is in this notice rather than reused as "sender",
        # which on the envelope is the worker that produced the failure.
        "requester": identity.sender,
        "worker": identity.worker,
        "harness": identity.harness,
        "observedAtMs": identity.observed_at_ms,
        "nonce": nonce,
        **fields,
    }
    return InboxMessage(
        message_id=str(uuid5(_NOTICE_NAMESPACE, key)),
        conversation_id=identity.conversation_id,
        sender=identity.worker,
        recipient=identity.sender,
        payload=json.dumps(payload, separators=(",", ":")).encode(),
        intent="reply",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=identity.observed_at_ms,
        idempotency_key=f"availability-loud:{key}",
    )


def unavailable_notice(
    identity: AttemptIdentity,
    *,
    failure_code: str,
    terminal: bool,
    attempts: int,
    max_attempts: int,
    detail: str | None = None,
) -> InboxMessage:
    """The provider failed this attempt; the sender is told with the evidence.

    ``failure_code`` is the closed harness-failure classification (for
    example ``PROVIDER_USAGE_LIMIT`` for an exhausted quota) and is carried
    verbatim: the notice reports what was observed, it does not re-diagnose.
    """

    text = "\n".join(
        line
        for line in (
            "⚠️ 你等待的请求没有拿到回复:provider 侧的这次尝试失败了。",
            f"- worker: {identity.worker}",
            f"- harness: {identity.harness or 'unknown'}",
            f"- 失败原因: {failure_code}",
            f"- 尝试: 第 {attempts}/{max_attempts} 次" + ("(已终止)" if terminal else "(将重试)"),
            f"- delivery: {identity.delivery_id}",
            f"- attempt: {identity.attempt_id}",
            f"- 观察时刻: {identity.observed_at_ms}",
            "这不是成功回复;该请求本身仍未完成。",
        )
        if line
    )
    return _message(
        identity,
        kind=NOTICE_KIND_UNAVAILABLE,
        key=f"unavailable:{identity.attempt_id}:{failure_code}",
        text=text,
        fields={
            "failureCode": failure_code,
            "terminal": terminal,
            "attempts": attempts,
            "maxAttempts": max_attempts,
            "detail": detail,
        },
    )


def no_progress_notice(
    identity: AttemptIdentity,
    *,
    waited_ms: int,
    budget_ms: int,
) -> InboxMessage:
    """No correlated progress within the budget; say so without diagnosing.

    This is the only path for the "stuck" class, which by definition has no
    failure event.  It must not claim a permission/approval cause: the honest
    statement is that nothing observable happened and the reason is not yet
    known.  The turn is left running -- this reports, it does not kill.
    """

    waited_s = max(0, waited_ms) // 1000
    text = "\n".join(
        (
            f"⚠️ 你等待的请求已 {waited_s}s 没有可核进展:原因待确认(还没有判定为故障)。",
            f"- worker: {identity.worker}",
            f"- harness: {identity.harness or 'unknown'}",
            f"- 静默预算: {budget_ms // 1000}s",
            f"- delivery: {identity.delivery_id}",
            f"- attempt: {identity.attempt_id}",
            f"- 观察时刻: {identity.observed_at_ms}",
            "本轮仍在继续,没有终止也没有自动放宽审批;若持续无进展请让 owner 介入。",
        )
    )
    return _message(
        identity,
        kind=NOTICE_KIND_NO_PROGRESS,
        key=f"no-progress:{identity.attempt_id}:{budget_ms}",
        text=text,
        fields={
            "waitedMs": waited_ms,
            "budgetMs": budget_ms,
        },
    )
