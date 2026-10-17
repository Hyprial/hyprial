"""Source-side session quiesce and rollback for CLI-hosted TUI workers.

The TUI variant of the P2 source quiesce.  The unit is an
``InteractiveSession``, and its **session shape** is anchored on
``operation_id + session_ref [+ process_identity]`` -- never on a generation:
the session document has none (``desired_state/documents.py``).  The durable
carrier is the same ``quiesce`` step the headless effect uses
(``p2/orchestration/chain.py``; ``SourceIntent``/``SourceFinish`` on the
admission binding), so no second "hold" noun is introduced.

The kill is a **best-effort hard kill** (``tmux.kill_session``, ``check=False``).
It can interrupt an in-flight turn in the pane, and the receipt says so:
``turn=not-preserved``.  The verdict never trusts the kill return code; a
``quiesced`` outcome requires all three checks:

1. the tmux session is really gone (existence re-read, never the kill return),
2. the recorded ``process_pid``/``process_identity`` is not alive (a different
   birth marker for a reused PID is still "the recorded process is gone"),
3. the recorded session removal is confirmed by the session store.

The heartbeat TTL is only a fallback upper bound.  Within it the truth is a
**记账滞后窗口** (the process may already be dead while the record still shows
it); it is never permission to round "unknown" down to "quiesced".
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import time
from pathlib import Path

from hyprial.kernel import agent_uri_actor, process_birth_identity

from hyprial.daemon.impl.harnesses.runtime import tmux as tmux_module

from .effect import SOURCE_QUIESCE_DEADLINE_SECONDS
from ..model import identifier
from ..source_ports import SourceIntent, SourceFinish

#: The verdict when the hard kill interrupted whatever turn the pane was in.
#: Recorded on every receipt: the transfer never pretends a partial turn was
#: preserved.
IN_FLIGHT_TURN_NOT_PRESERVED = "not-preserved"

#: The design's wording for the interval inside the heartbeat TTL: the process
#: may already be dead while desired state still records it.
LAGGED_RECORDING_WINDOW_NOTE = "记账滞后窗口"

#: TTL is a fallback upper bound only; the real verdict is the three checks.
SESSION_QUIESCE_DEADLINE_SECONDS = SOURCE_QUIESCE_DEADLINE_SECONDS

#: TUI harnesses OPEN for cross-machine transfer.  codex now has a detached
#: ``--tmux`` form, but the transfer fence admits only a proven
#: ``(harness, mode, entrypoint)``, so codex is deliberately not listed yet.
RESUMABLE_TUI_HARNESSES = frozenset({"claude", "pi"})

_ANCHOR_SEPARATOR = "\x1f"


@dataclass(frozen=True, slots=True)
class SessionQuiesceRequest:
    """One session-shaped quiesce intent.

    ``actor``/``entity_token`` are the incarnation fence the journal already
    requires; the session anchor itself is ``operation_id + session_ref
    [+ process_identity]`` and is folded into the intent fingerprint.
    """

    operation_id: str
    actor: str
    entity_token: str
    session_ref: str
    tmux_session: str
    process_pid: int | None = None
    process_identity: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.operation_id,
            self.actor,
            self.entity_token,
            self.session_ref,
            self.tmux_session,
        ):
            identifier(value)
        if (self.process_pid is None) != (self.process_identity is None):
            raise ValueError("process_pid and process_identity must be provided together")
        if self.process_pid is not None and self.process_pid < 1:
            raise ValueError("process_pid must be positive")

    @property
    def fingerprint(self) -> str:
        anchor = _ANCHOR_SEPARATOR.join(
            (self.operation_id, self.session_ref, self.process_identity or "")
        )
        return hashlib.sha256(anchor.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionQuiesceReceipt:
    """The kill fact: when it happened, and what the pane's turn lost."""

    kill_moment_ms: int
    in_flight_turn: str = IN_FLIGHT_TURN_NOT_PRESERVED

    def token(self) -> str:
        """The compact identifier stored as the journal step's receipt."""

        return f"kill={self.kill_moment_ms};turn={self.in_flight_turn}"


@dataclass(frozen=True, slots=True)
class SessionQuiesceOutcome:
    operation_id: str
    state: str
    receipt: str | None = None
    reason: str = ""
    kill_moment_ms: int | None = None
    in_flight_turn: str = IN_FLIGHT_TURN_NOT_PRESERVED


class TmuxTarget:
    """Real tmux port: resolved binary plus the best-effort hard kill.

    ``kill_session`` is the module's ``check=False`` teardown; the effect always
    re-reads ``has_session`` afterwards instead of trusting its return.
    """

    def __init__(self, tmux_bin: str, *, tmux: object | None = None) -> None:
        self._tmux_bin = tmux_bin
        self._tmux = tmux if tmux is not None else tmux_module

    def has_session(self, name: str) -> bool:
        return self._tmux.has_session(self._tmux_bin, name)

    def kill_session(self, name: str) -> None:
        self._tmux.kill_session(self._tmux_bin, name)


class DesiredStateSessionRemoval:
    """Removal port over a ``DesiredStateStore``.

    Confirms the actor still owns this exact ``session_ref`` before removing,
    so a delayed verdict can never unregister a superseded successor.  An
    already-absent session counts as removal confirmed.
    """

    def __init__(self, store: object) -> None:
        self._store = store

    @staticmethod
    def _local_name(actor: str) -> str:
        return agent_uri_actor(actor) or actor

    def remove(self, actor: str, session_ref: str) -> bool:
        state = self._store.load()
        current = next(
            (
                session
                for session in state.interactive_sessions
                if self._local_name(session.actor) == actor
            ),
            None,
        )
        if current is None:
            return True
        if current.session_ref != session_ref:
            return False
        _state, removed = self._store.unregister_interactive_if_current(
            current.actor, session_ref
        )
        if not removed:
            return False
        remaining = self._store.load().interactive_sessions
        return not any(
            self._local_name(session.actor) == actor
            and session.session_ref == session_ref
            for session in remaining
        )


def _process_still_alive(pid: int | None, identity: str | None) -> bool:
    """True only when the recorded incarnation is positively still alive.

    ``None`` from the birth probe means the process is gone; a *different*
    marker means the PID was reused, so the recorded process is gone too.
    """

    if pid is None or identity is None:
        return False
    observed = process_birth_identity(pid)
    return observed is not None and observed == identity


class SessionQuiesceEffect:
    def __init__(
        self,
        journal,
        tmux,
        sessions,
        *,
        deadline_seconds: float = SESSION_QUIESCE_DEADLINE_SECONDS,
        clock_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
        process_alive: Callable[[int | None, str | None], bool] | None = None,
    ) -> None:
        if deadline_seconds <= 0:
            raise ValueError("deadline must be positive")
        self._journal = journal
        self._tmux = tmux
        self._sessions = sessions
        self._deadline_seconds = deadline_seconds
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._monotonic = monotonic or time.monotonic
        self._process_alive = process_alive or _process_still_alive

    def __call__(self, request: SessionQuiesceRequest) -> SessionQuiesceOutcome:
        deadline = self._monotonic() + self._deadline_seconds

        def remaining() -> float:
            budget = deadline - self._monotonic()
            if budget <= 0:
                raise TimeoutError("session quiesce outcome remains unknown")
            return budget

        try:
            start = self._journal.submit(
                SourceIntent(
                    request.operation_id,
                    request.actor,
                    request.entity_token,
                    request.actor,
                    request.fingerprint,
                )
            ).result(remaining())
            if not start.execute:
                # A replayed intent may conceal an accepted effect; only a
                # durable completed row is proof, never the missing waiter.
                completed = (
                    start.state.phase == "quiesced" and not start.state.needs_attention
                )
                return SessionQuiesceOutcome(
                    request.operation_id,
                    "quiesced" if completed else "unknown",
                    start.state.receipt if completed else None,
                    "journal-replay",
                )
            state = start.state
            # The kill moment is recorded before the best-effort hard kill so a
            # crash between the two still names when the pane was interrupted.
            kill_moment_ms = self._clock_ms()
            self._tmux.kill_session(request.tmux_session)
            # Never trust the kill return: re-read existence.
            if self._tmux.has_session(request.tmux_session):
                return self._unknown(request, "tmux-still-present", kill_moment_ms)
            if self._process_alive(request.process_pid, request.process_identity):
                return self._unknown(request, "process-still-alive", kill_moment_ms)
            if not self._sessions.remove(request.actor, request.session_ref):
                return self._unknown(request, "removal-not-confirmed", kill_moment_ms)
            receipt = SessionQuiesceReceipt(kill_moment_ms)
            finished = self._journal.submit(
                SourceFinish(state, receipt.token())
            ).result(remaining())
            return SessionQuiesceOutcome(
                request.operation_id,
                "quiesced",
                finished.receipt,
                "",
                kill_moment_ms,
                IN_FLIGHT_TURN_NOT_PRESERVED,
            )
        except Exception as error:
            # An unproven stop is reported as-is; it is never rounded down to a
            # successful quiesce, and nothing is rolled back here.
            return self._unknown(request, type(error).__name__, None)

    @staticmethod
    def _unknown(
        request: SessionQuiesceRequest, reason: str, kill_moment_ms: int | None
    ) -> SessionQuiesceOutcome:
        return SessionQuiesceOutcome(
            request.operation_id,
            "unknown",
            None,
            reason,
            kill_moment_ms,
            IN_FLIGHT_TURN_NOT_PRESERVED,
        )


class UnsupportedSessionHarness(ValueError):
    """This harness is not open for cross-machine TUI transfer yet."""


class DualActiveRiskWindow(RuntimeError):
    """The target may already own the session (post-S7); restart is refused."""


class SessionAlreadyRunning(RuntimeError):
    """A tmux session with this name is already up; never start a second."""


@dataclass(frozen=True, slots=True)
class SessionRollbackRequest:
    operation_id: str
    actor: str
    harness: str
    session_ref: str
    cwd: Path
    env: Mapping[str, str]
    command: tuple[str, ...]
    tmux_session: str
    #: True once the commit point (S7: target registered) has passed.  The true
    #: dual-active window is *after* S7, so a rollback that restarts the source
    #: there must be refused, not performed silently.
    target_started: bool = False

    def __post_init__(self) -> None:
        for value in (self.operation_id, self.actor, self.session_ref, self.tmux_session):
            identifier(value)


def resume_argv(
    harness: str, session_ref: str, command: tuple[str, ...]
) -> tuple[str, ...]:
    """The source restart argv: the same command, forced onto ``--resume``.

    Reuses the recorded launch command and pins the *same* session ref, so a
    rollback continues the interrupted conversation instead of cold-starting a
    new one.
    """

    if harness not in RESUMABLE_TUI_HARNESSES:
        raise UnsupportedSessionHarness(
            f"{harness} has no resumable TUI form; refusing the rollback restart"
        )
    argv = list(command)
    if "--resume" in argv:
        index = argv.index("--resume")
        if index + 1 < len(argv):
            argv[index + 1] = session_ref
        else:
            argv.append(session_ref)
    else:
        argv.extend(["--resume", session_ref])
    return tuple(argv)


def restart_session(
    request: SessionRollbackRequest,
    *,
    tmux,
    tmux_bin: str,
    launch: Callable[..., None] | None = None,
) -> str:
    """Restart the source TUI with the same session_ref, or refuse loudly.

    Pre-S7 this is the safe rollback: no other owner exists.  Post-S7 the
    target may own the session, so a restart would be a true dual-active state;
    that window is refused explicitly rather than papered over.
    """

    if request.target_started:
        raise DualActiveRiskWindow(
            "the target may already own this session (after the S7 commit "
            "point); restarting the source here is the true dual-active "
            "window -- refusing the silent rollback"
        )
    if tmux.has_session(request.tmux_session):
        raise SessionAlreadyRunning(
            f"tmux session {request.tmux_session!r} is already running; "
            "refusing to start a second source"
        )
    argv = resume_argv(request.harness, request.session_ref, request.command)
    launcher = launch if launch is not None else tmux_module.new_detached_session
    launcher(
        tmux_bin,
        request.tmux_session,
        argv,
        cwd=request.cwd,
        env=request.env,
    )
    return request.tmux_session
