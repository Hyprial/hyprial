"""Deterministic PAC workflow executor core (design-pac-workflow.md §4).

The executor is daemon-hosted deterministic code: it dispatches the declared
task to every target, watches for replies/acks, applies the timeout policy,
and emits the final report.  **No model turn is ever involved** — the failure
domain is exactly "a target worker did not answer", enumerable and observable.

All side effects go through narrow ports so the core is unit-testable with
in-memory fakes; the daemon wiring (a periodic fiber calling :meth:`tick`,
ports backed by the inbox service) lives outside this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Literal, Protocol
from uuid import uuid4

from .schema import OnTimeoutSpec, WorkflowSpec, expand_template


class TargetState(StrEnum):
    PENDING = "pending"          # not yet dispatched (initial / awaiting retry slot)
    DISPATCHED = "dispatched"    # message accepted by the inbox; deadline running
    BACKOFF = "backoff"          # retry scheduled, waiting out the backoff
    DONE = "done"                # reply matched / ack observed
    TIMED_OUT = "timed_out"      # terminal: counted in the report, no alarm
    ESCALATED = "escalated"      # terminal: alarm emitted to escalate_to


TERMINAL_TARGET_STATES = frozenset(
    {TargetState.DONE, TargetState.TIMED_OUT, TargetState.ESCALATED}
)

MAX_ACTIVITY_EXTENSIONS = 20
"""Cap on non-terminal-reply deadline extensions per target (PR #282 修正1).

Activity keeps a target alive on purpose ("don't guess dead, but must
report") — but indefinite liveness is not the same as unbounded liveness.
Without a cap, an eternally-chatty worker that never emits the terminal
marker parks its run in DISPATCHED forever with no escalation path, the
same failure shape the "activity extends life" fix was meant to close for
genuinely silent targets, just inverted. 20 is deliberately generous — a
legitimately slow/verbose worker gets 20 rounds of "still going" — while
still bounding the worst case; once hit, the target falls through the
declared on_timeout policy exactly like a genuinely silent one would.
"""

REPORT_RETRY_BACKOFF_MS = 30_000
"""Minimum delay between final-report delivery attempts.

Unlike target dispatch, report delivery has no YAML retry policy.  A user
target can synchronously wait for a remote receipt, so retrying on every daemon
tick turns one unavailable report sink into continuous multi-second work.
"""

REPORT_MAX_ATTEMPTS = 10
"""Cap on report delivery attempts before the run finishes without its report.

Ten attempts at the backoff above is roughly five minutes.  The bound is sized
against what actually raises a *non-permanent* dispatch error: the typed inbox
refusing a submission (``inbox_io.py``) or not acknowledging it
(``service.py``) -- congestion and contention, which clear in seconds.  A sink
still refusing after five minutes is an incident, and this now escalates
instead of retrying into it forever.

The counter is per-executor and is not persisted, exactly like
``_report_next_attempt_ms``.  A daemon restart therefore reloads the open run
and grants a fresh budget -- deliberately: each process bounds itself, which is
what stops the tick from carrying a dead run forever, while a genuinely
transient outage that outlives a restart still gets more chances.
"""


class RunState(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class WorkflowDispatchError(RuntimeError):
    """The dispatch port refused or failed a send.

    ``permanent`` marks failures that retrying can never fix (a static
    schema violation like ``route:`` targets, or an unwired user-delivery
    transport): the executor treats those as terminal immediately instead of
    burning the retry budget (Allen 2026-08: 400-class errors never retry).
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


def _monotonic_ms() -> int:
    import time

    return int(time.monotonic() * 1000)


def _is_permanent_failure(runtime: "TargetRuntime") -> bool:
    error = getattr(runtime, "last_error", None)
    return bool(getattr(error, "permanent", False))


_EXCERPT_LIMIT = 200
_EXCERPT_MARKER = "…[truncated]"


def _excerpt(text: str) -> str:
    """First 200 chars of a reply, marked when cut so a reader can tell a
    short excerpt from a truncated one (O4, PR #282 review — the #155
    truncation had no such marker and read as a full message)."""
    if len(text) <= _EXCERPT_LIMIT:
        return text
    return text[:_EXCERPT_LIMIT] + _EXCERPT_MARKER


@dataclass(frozen=True, slots=True)
class ReplyView:
    """One inbound message inside a workflow conversation."""

    message_id: str
    text: str


class DispatchPort(Protocol):
    """Send one message onto the mesh; returns the accepted message id."""

    def send(self, *, sender: str, target: str, conversation_id: str, text: str) -> str: ...


class ObservePort(Protocol):
    """Read-side view of the daemon inbox (the truth source)."""

    def list_replies(self, *, conversation_id: str, exclude_sender: str) -> tuple[ReplyView, ...]:
        """Pending messages in the conversation not from the runner, oldest first.

        A list, not the single oldest: a reply that fails the content match
        must not shadow a later matching one (a worker answering off-format
        would otherwise wedge the target until timeout — found by E2E-015).
        """
        ...

    def is_acknowledged(self, message_id: str) -> bool:
        """True once the target consumed (acked) the dispatched message."""
        ...

    def ack(self, message_id: str) -> None:
        """Consume a matched reply so it does not linger in the runner inbox."""
        ...


class AlarmPort(Protocol):
    """Escalation channel (backed by hyprial.alarm.AlarmEmitter in the daemon)."""

    def escalate(self, *, to: str, text: str) -> None: ...


@dataclass(frozen=True, slots=True)
class TimeoutOverride:
    """A hook's verdict replacing the declared on_timeout policy for one target."""

    action: Literal["retry", "escalate", "report"]
    escalate_to: str | None = None


class WorkflowHooks(Protocol):
    """Per-target transition hooks (design-pac-workflow §4.7, Allen 2026-08-20).

    v1 ships :class:`NoopHooks`; the points exist so future layers — a DAG
    gate ("dispatch only once dependencies are satisfied") or a custom
    on_timeout strategy — attach WITHOUT changing the core.  Hooks obey the
    same discipline as the executor: deterministic, and never a model turn.
    """

    def gate_dispatch(self, run: WorkflowRun, target: TargetRuntime) -> bool:
        """Before each (re-)dispatch; False holds the target in PENDING this pass."""
        ...

    def on_dispatched(self, run: WorkflowRun, target: TargetRuntime) -> None:
        """After a dispatch was accepted by the inbox."""
        ...

    def on_reply(self, run: WorkflowRun, target: TargetRuntime, view: ReplyView) -> None:
        """A matched reply, before the core acks it."""
        ...

    def on_activity(self, run: WorkflowRun, target: TargetRuntime, view: ReplyView) -> None:
        """A non-terminal reply extended the deadline (liveness, not completion).

        Fires every extension so a monitor can see "deadline keeps moving
        out" without polling status — ``target.extend_count`` is already
        updated when this fires (PR #282 修正1).
        """
        ...

    def on_timeout(self, run: WorkflowRun, target: TargetRuntime) -> TimeoutOverride | None:
        """Override the declared timeout policy; None = follow the YAML."""
        ...

    def on_terminal(self, run: WorkflowRun, target: TargetRuntime) -> None:
        """The target entered done / timed_out / escalated."""
        ...


class NoopHooks:
    """The v1 hooks: every gate open, every notification ignored, no override."""

    def gate_dispatch(self, run: WorkflowRun, target: TargetRuntime) -> bool:
        return True

    def on_dispatched(self, run: WorkflowRun, target: TargetRuntime) -> None:
        pass

    def on_reply(self, run: WorkflowRun, target: TargetRuntime, view: ReplyView) -> None:
        pass

    def on_activity(self, run: WorkflowRun, target: TargetRuntime, view: ReplyView) -> None:
        pass

    def on_timeout(self, run: WorkflowRun, target: TargetRuntime) -> TimeoutOverride | None:
        return None

    def on_terminal(self, run: WorkflowRun, target: TargetRuntime) -> None:
        pass


@dataclass(slots=True)
class TargetRuntime:
    name: str
    conversation_id: str
    state: TargetState = TargetState.PENDING
    attempts: int = 0
    last_error: WorkflowDispatchError | None = None
    deadline_ms: int | None = None
    next_attempt_ms: int | None = None
    last_message_id: str | None = None
    reply_excerpt: str | None = None
    delivered: bool = False
    extend_count: int = 0


@dataclass(slots=True)
class WorkflowRun:
    run_id: str
    spec: WorkflowSpec
    sender: str
    nonce: str
    state: RunState = RunState.RUNNING
    targets: list[TargetRuntime] = field(default_factory=list)
    report_text: str | None = None
    dispatch_warnings: tuple[str, ...] = ()

    def status(self) -> dict[str, object]:
        return {
            "runId": self.run_id,
            "name": self.spec.name,
            "state": str(self.state),
            "sender": self.sender,
            "nonce": self.nonce,
            "targets": [
                {
                    "target": t.name,
                    "conversationId": t.conversation_id,
                    "state": str(t.state),
                    "attempts": t.attempts,
                    **(
                        {
                            "lastEventAtMs": t.deadline_ms
                            - int(self.spec.await_.timeout_seconds * 1000)
                        }
                        if t.state is TargetState.DISPATCHED
                        and t.deadline_ms is not None
                        else {}
                    ),
                    **({"extendCount": t.extend_count} if t.extend_count else {}),
                    **({"replyExcerpt": t.reply_excerpt} if t.reply_excerpt else {}),
                }
                for t in self.targets
            ],
            **({"report": self.report_text} if self.report_text is not None else {}),
            **({"dispatchWarnings": list(self.dispatch_warnings)} if self.dispatch_warnings else {}),
        }


class WorkflowExecutor:
    """Drive one workflow run to completion through periodic :meth:`tick` calls.

    The host (daemon fiber, or a test) owns the cadence; every state
    transition happens inside ``start``/``tick``/``cancel``, so a run's whole
    evolution is reconstructable from the calls — no hidden timers.
    """

    def __init__(
        self,
        *,
        spec: WorkflowSpec,
        sender: str,
        dispatch: DispatchPort,
        observe: ObservePort,
        alarm: AlarmPort,
        clock_ms: Callable[[], int] | None = None,
        run_id: str | None = None,
        nonce: str | None = None,
        hooks: WorkflowHooks | None = None,
        restored: WorkflowRun | None = None,
        conversation_ids: Mapping[str, str] | None = None,
        explicit_final: bool = False,
        emit_report: bool = True,
    ) -> None:
        if restored is not None:
            # Recovery path (store.load_open_runs): the run and its targets
            # arrive fully stateful — deadlines and backoffs keep their
            # persisted wall-clock values, so downtime honestly burns budget.
            self.run = restored
        else:
            self.run = WorkflowRun(
                run_id=run_id or f"run-{uuid4().hex[:12]}",
                spec=spec,
                sender=sender,
                nonce=nonce or uuid4().hex[:12],
            )
            for target in spec.targets:
                self.run.targets.append(
                    TargetRuntime(
                        name=target.name,
                        conversation_id=(
                            conversation_ids[target.name]
                            if conversation_ids is not None
                            and target.name in conversation_ids
                            else f"wf-{self.run.run_id}-{target.name}"
                        ),
                    )
                )
        self._dispatch = dispatch
        self._observe = observe
        self._alarm = alarm
        self._clock_ms = clock_ms or _monotonic_ms
        self._hooks: WorkflowHooks = hooks or NoopHooks()
        self._explicit_final = explicit_final
        self._emit_report = emit_report
        # Ephemeral by design: after daemon recovery one immediate report retry
        # is safe, while repeated attempts in the same process are rate-limited.
        self._report_next_attempt_ms: int | None = None
        self._report_attempts = 0

    # ── public surface ───────────────────────────────────────────────────

    def start(self) -> None:
        """Dispatch every target once. Idempotent: a started run does not restart."""
        if self.run.state is not RunState.RUNNING:
            return
        for runtime in self.run.targets:
            if runtime.state is TargetState.PENDING and self._hooks.gate_dispatch(
                self.run, runtime
            ):
                self._dispatch_target(runtime)
        self._maybe_finish()

    def tick(self) -> None:
        """One scheduling pass: fire due retries, collect replies, apply timeouts."""
        if self.run.state is not RunState.RUNNING:
            return
        now = self._clock_ms()
        for runtime in self.run.targets:
            if runtime.state is TargetState.PENDING:
                # A target the gate held (at start, or after a gated retry)
                # is re-evaluated every pass until the gate opens.
                if self._hooks.gate_dispatch(self.run, runtime):
                    self._dispatch_target(runtime)
                continue
            if runtime.state is TargetState.BACKOFF:
                if (
                    runtime.next_attempt_ms is not None
                    and now >= runtime.next_attempt_ms
                    and self._hooks.gate_dispatch(self.run, runtime)
                ):
                    self._dispatch_target(runtime)
                continue
            if runtime.state is not TargetState.DISPATCHED:
                continue
            if self._try_complete(runtime):
                continue
            if runtime.deadline_ms is not None and now > runtime.deadline_ms:
                self._on_timeout(runtime)
        self._maybe_finish()

    def cancel(self) -> None:
        """Freeze the run; in-flight targets keep their last state for forensics."""
        if self.run.state is RunState.RUNNING:
            self.run.state = RunState.CANCELLED

    def record_activity(self, target: str, text: str) -> None:
        """Apply typed activity as liveness without inferring completion."""

        runtime = next((item for item in self.run.targets if item.name == target), None)
        if runtime is None or runtime.state is not TargetState.DISPATCHED:
            return
        runtime.reply_excerpt = _excerpt(text)
        if runtime.extend_count < MAX_ACTIVITY_EXTENSIONS:
            runtime.extend_count += 1
            runtime.deadline_ms = self._clock_ms() + int(
                self.run.spec.await_.timeout_seconds * 1000
            )

    def complete_target(self, target: str, *, message_id: str, excerpt: str) -> None:
        """Complete only after an explicit final was durably accepted."""

        runtime = next((item for item in self.run.targets if item.name == target), None)
        if runtime is None or runtime.state in TERMINAL_TARGET_STATES:
            return
        runtime.last_message_id = message_id
        runtime.reply_excerpt = _excerpt(excerpt)
        runtime.state = TargetState.DONE
        self._hooks.on_terminal(self.run, runtime)
        self._maybe_finish()

    # ── internals ────────────────────────────────────────────────────────

    def _dispatch_target(self, runtime: TargetRuntime) -> None:
        # Only the first successful delivery carries the original task text.
        # Every dispatch after that is a retry of a target that already has
        # it (or a target we can't confirm ever received it, which is the
        # send-failure path below and stays on the full-text branch) — CAPFIX
        # /XFERC: resending the task verbatim made an already-done worker
        # reject the duplicate dispatch outright.
        text = (
            self._retry_reminder_text(runtime)
            if runtime.delivered
            else expand_template(
                self.run.spec.task_for(runtime.name), nonce=self.run.nonce, target=runtime.name
            )
        )
        try:
            message_id = self._dispatch.send(
                sender=self.run.sender,
                target=runtime.name,
                conversation_id=runtime.conversation_id,
                text=text,
            )
        except WorkflowDispatchError as error:
            # A refused/failed send takes the same path as a timeout: the
            # declared on_timeout policy decides retry/escalate/report.
            # A failed dispatch consumes one attempt (never an infinite
            # retry loop), and a permanent failure skips retrying entirely.
            runtime.attempts += 1
            runtime.last_error = error
            self._on_timeout(runtime)
            return
        runtime.last_message_id = message_id
        runtime.attempts += 1
        runtime.delivered = True
        runtime.deadline_ms = self._clock_ms() + int(
            self.run.spec.await_.timeout_seconds * 1000
        )
        runtime.next_attempt_ms = None
        runtime.state = TargetState.DISPATCHED
        self._hooks.on_dispatched(self.run, runtime)

    def _retry_reminder_text(self, runtime: TargetRuntime) -> str:
        """Short confirmation nudge for a retry — never the task body itself.

        The worker already has the task from the first delivery; repeating it
        verbatim reads as a brand-new duplicate dispatch (CAPFIX/XFERC), so a
        retry only asks for a terminal-marker reply and points back at the
        original conversation instead of re-stating the task.
        """
        spec = self.run.spec
        attempt_no = runtime.attempts + 1
        lines = [
            f"[PAC confirmation request #{attempt_no}] workflow '{spec.name}' "
            f"(run {self.run.run_id}) for target '{runtime.name}'.",
            "This is a reminder, not a re-dispatch of the task — see this "
            "conversation's earlier message for the original task.",
        ]
        if spec.await_.match is not None:
            needle = expand_template(
                spec.await_.match, nonce=self.run.nonce, target=runtime.name
            )
            lines.append(
                f"If it is already done, reply with the terminal marker "
                f"(nonce {self.run.nonce!r}): {needle!r}"
            )
        else:
            lines.append(
                f"If it is already done, reply to confirm (nonce {self.run.nonce!r})."
            )
        return "\n".join(lines)

    def _try_complete(self, runtime: TargetRuntime) -> bool:
        """One observation pass over a dispatched target; True once DONE."""
        spec = self.run.spec
        if spec.await_.kind == "ack":
            if runtime.last_message_id is not None and self._observe.is_acknowledged(
                runtime.last_message_id
            ):
                runtime.state = TargetState.DONE
                self._hooks.on_terminal(self.run, runtime)
                return True
            return False
        replies = self._observe.list_replies(
            conversation_id=runtime.conversation_id, exclude_sender=self.run.sender
        )
        if self._explicit_final:
            if replies:
                if runtime.extend_count >= MAX_ACTIVITY_EXTENSIONS:
                    runtime.reply_excerpt = _excerpt(replies[-1].text)
                    for candidate in replies:
                        self._observe.ack(candidate.message_id)
                    self._on_timeout(runtime)
                else:
                    self._extend_on_activity(runtime, replies)
            return False
        view: ReplyView | None = None
        for candidate in replies:
            if spec.await_.match is None:
                view = candidate
                break
            needle = expand_template(
                spec.await_.match, nonce=self.run.nonce, target=runtime.name
            )
            if needle in candidate.text:
                view = candidate
                break
        if view is not None:
            self._hooks.on_reply(self.run, runtime, view)
            self._observe.ack(view.message_id)
            runtime.reply_excerpt = _excerpt(view.text)
            runtime.state = TargetState.DONE
            self._hooks.on_terminal(self.run, runtime)
            return True
        if spec.await_.match is not None and replies:
            # Any reply from the target is evidence it's alive even when it
            # missed the terminal marker — extend the deadline instead of
            # silently ticking toward a false timeout (R267B: a worker that
            # answered repeatedly, just never with the nonce prefix, still
            # got escalated off a clock nothing had reset). Consumed replies
            # are acked here (the permanent forensic record is the worker
            # JSONL, not this queue) — leaving them unacked would make the
            # same stale reply re-extend the deadline forever on every tick,
            # so "genuinely silent" could never be reached again.
            if runtime.extend_count >= MAX_ACTIVITY_EXTENSIONS:
                # The extension budget is spent: an eternally-chatty,
                # never-terminal target must eventually surface through the
                # declared on_timeout policy, same as genuine silence would
                # (PR #282 修正1) — not extend forever. Still record and ack
                # the reply that tripped the cap so whatever the policy does
                # next (escalate/report) carries it, not a stale excerpt.
                runtime.reply_excerpt = _excerpt(replies[-1].text)
                for candidate in replies:
                    self._observe.ack(candidate.message_id)
                self._on_timeout(runtime)
                return True
            self._extend_on_activity(runtime, replies)
        return False

    def _extend_on_activity(self, runtime: TargetRuntime, replies: tuple[ReplyView, ...]) -> None:
        latest = replies[-1]
        runtime.extend_count += 1
        self._hooks.on_activity(self.run, runtime, latest)
        runtime.reply_excerpt = _excerpt(latest.text)
        for candidate in replies:
            self._observe.ack(candidate.message_id)
        runtime.deadline_ms = self._clock_ms() + int(
            self.run.spec.await_.timeout_seconds * 1000
        )

    def _on_timeout(self, runtime: TargetRuntime) -> None:
        policy = self.run.spec.on_timeout
        override = self._hooks.on_timeout(self.run, runtime)
        action = override.action if override is not None else policy.action
        escalate_to = (
            override.escalate_to
            if override is not None and override.escalate_to
            else policy.escalate_to
        )
        if (
            action == "retry"
            and not _is_permanent_failure(runtime)
            and runtime.attempts < policy.max_attempts
        ):
            # Always park in BACKOFF, even for a zero delay: the next
            # dispatch happens on a later tick() pass (its BACKOFF branch
            # already re-checks gate_dispatch), never synchronously from
            # here. A same-call retry would recurse through
            # _dispatch_target -> _on_timeout up to max_attempts deep, and
            # every entry point that can reach _on_timeout (start(), tick()'s
            # PENDING/BACKOFF branches, a failed _dispatch_target) shares
            # this one exit, so none of them recurse either.
            delay = self._backoff_for(policy, attempt=runtime.attempts)
            runtime.state = TargetState.BACKOFF
            runtime.next_attempt_ms = self._clock_ms() + int(delay * 1000)
            return
        if action == "escalate" or (action == "retry" and escalate_to is not None):
            # Retry budget exhausted with an escalate address, or an explicit
            # escalate action: the alarm is never silent (design §3).
            self._alarm.escalate(
                to=escalate_to or self.run.sender,
                text=self._timeout_text(runtime),
            )
            runtime.state = TargetState.ESCALATED
            self._hooks.on_terminal(self.run, runtime)
            return
        runtime.state = TargetState.TIMED_OUT
        self._hooks.on_terminal(self.run, runtime)

    @staticmethod
    def _backoff_for(policy: OnTimeoutSpec, *, attempt: int) -> float:
        """Delay before the NEXT attempt, indexed by attempts already made."""
        backoff = policy.backoff_seconds
        if not backoff:
            return 0.0
        index = min(attempt - 1, len(backoff) - 1) if attempt >= 1 else 0
        return backoff[max(index, 0)]

    def _timeout_text(self, runtime: TargetRuntime) -> str:
        spec = self.run.spec
        text = (
            f"PAC workflow '{spec.name}' (run {self.run.run_id}): target "
            f"'{runtime.name}' did not answer within "
            f"{spec.await_.timeout_seconds:.0f}s after {runtime.attempts} "
            f"attempt(s). Conversation: {runtime.conversation_id}"
        )
        if runtime.reply_excerpt:
            # Escalating with a bare "it went silent" starves the coordinator
            # of context it already has in hand — give it the target's last
            # word, terminal-matched or not (Allen 2026-08-24 PACAW).
            text += f"\nLast reply from target: {runtime.reply_excerpt!r}"
        return text

    def _maybe_finish(self) -> None:
        if self.run.state is not RunState.RUNNING:
            return
        if not all(t.state in TERMINAL_TARGET_STATES for t in self.run.targets):
            return
        if not self.run.targets:
            return
        now = self._clock_ms()
        if (
            self._report_next_attempt_ms is not None
            and now < self._report_next_attempt_ms
        ):
            return
        self.run.report_text = self._build_report()
        if not self._emit_report:
            self.run.state = RunState.COMPLETED
            return
        try:
            self._dispatch.send(
                sender=self.run.sender,
                target=self.run.spec.report_to or self.run.sender,
                conversation_id=f"wf-{self.run.run_id}-report",
                text=self.run.report_text,
            )
        except WorkflowDispatchError as error:
            # Report send refused: stay RUNNING so the next tick retries the
            # report.  The run is otherwise finished; only the delivery is
            # owed. A remote user receipt may consume seconds, so never retry
            # on every reconcile pass.
            #
            # But the retry must end.  This clause used to catch every
            # WorkflowDispatchError and back off forever, while the *target*
            # dispatch path already honoured ``permanent`` (see
            # ``_is_permanent_failure`` at the on_timeout branch).  So the one
            # failure that retrying can never fix -- an unwired delivery port,
            # named in WorkflowDispatchError's own docstring -- kept a finished
            # run RUNNING for the life of the process, and every reconcile pass
            # kept paying for it.  A run that cannot reach a terminal state is
            # also the ground the assign reclamation invariant stands on
            # (docs/design-assign.md §C.1): "is this run over?" has to have an
            # answer.
            self._report_attempts += 1
            if error.permanent or self._report_attempts >= REPORT_MAX_ATTEMPTS:
                self._abandon_report(error)
                return
            self._report_next_attempt_ms = now + REPORT_RETRY_BACKOFF_MS
            return
        self._report_next_attempt_ms = None
        self.run.state = RunState.COMPLETED

    def _abandon_report(self, error: WorkflowDispatchError) -> None:
        """Give up on delivering the report, loudly, and let the run finish.

        The run's work is done -- every target reached a terminal state -- so
        the state is COMPLETED rather than CANCELLED: nothing was cancelled,
        and a report is not a target.  What must not happen is for the give-up
        to be silent.  The previous behaviour was wrong but *loud*: a run stuck
        RUNNING wedged the tick, which is how anyone found out at all.  Trading
        that for a quiet COMPLETED would remove the only signal, so the alarm
        carries it instead -- the same channel, and the same "never silent"
        rule, the on_timeout escalate branch already uses.

        The report text stays on the run and is persisted; it is abandoned as
        a *delivery*, not lost as a record.
        """

        intended = self.run.spec.report_to or self.run.sender
        reason = (
            "delivery is permanently refused"
            if error.permanent
            else f"delivery failed {self._report_attempts} times"
        )
        self._alarm.escalate(
            to=self.run.sender,
            text=(
                f"PAC workflow {self.run.spec.name} (run {self.run.run_id}) "
                f"finished, but its report could not be delivered to "
                f"{intended}: {reason} ({error}). The run is completed; the "
                f"report text is on the run record and was not sent."
            ),
        )
        self._report_next_attempt_ms = None
        self.run.state = RunState.COMPLETED

    def _build_report(self) -> str:
        lines = [
            f"PAC workflow report: {self.run.spec.name} (run {self.run.run_id})",
        ]
        for runtime in self.run.targets:
            line = (
                f"- {runtime.name}: {runtime.state} "
                f"(attempts={runtime.attempts})"
            )
            if runtime.reply_excerpt:
                line += f" — {runtime.reply_excerpt}"
            lines.append(line)
        done = sum(1 for t in self.run.targets if t.state is TargetState.DONE)
        lines.append(f"done {done}/{len(self.run.targets)}")
        lines.extend(f"warning: {warning}" for warning in self.run.dispatch_warnings)
        return "\n".join(lines)
