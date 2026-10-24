"""Target-side TUI start (candidate B) and pre-commit failure cleanup.

S6 lands and activates a TUI without launching it; the daemon never starts the
TUI.  S7 runs the *target machine's own* CLI over the move's SSH channel:

    hyprial agent start <harness> --name <actor> --resume <sessionRef> --tmux

The CLI's registration wait is the readiness gate; this module re-verifies the
same predicate against the target's own daemon-status snapshot so a build that returns
early cannot pass it -- ``channelConfirmed`` for claude, ``session.register``
with ``source=pi-extension`` for pi.  Attach hints come from
``tmux.attach_hints``; a WSL target gets only the plain terminal line.

The commit point is S7.  Before it a failure is undoable: the source rolls back
through the S2 session restart, and this module clears the target side, always
naming the three residue classes (tmux / process / landed record) and which of
them could not be cleared.  After it nothing rolls back; the caller reports.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from hyprial.kernel import agent_uri_actor, ipc_errors

from hyprial.daemon.impl.harnesses.runtime import tmux as tmux_module

from ..source.session import (
    DualActiveRiskWindow,
    RESUMABLE_TUI_HARNESSES,
    restart_session,
)
from .lift import ENTRYPOINT_HEADLESS, ENTRYPOINT_TUI

#: Targets that only ever get the plain ``tmux attach`` line.
_WSL_PLATFORMS = frozenset({"wsl", "wsl2", "windows", "win32", "win"})


class TuiStartError(RuntimeError):
    """The target TUI did not start or register; ``code`` names the reason."""

    def __init__(self, code: str, detail: str, data: Mapping[str, Any] | None = None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.data = dict(data or {})


@dataclass(frozen=True, slots=True)
class TuiStartRequest:
    """One target-side start.  ``platform`` decides the attach-hint filter."""

    harness: str
    actor: str
    session_ref: str
    platform: str = ""
    cwd: str | None = None


@dataclass(frozen=True, slots=True)
class TuiStartResult:
    actor: str
    session_ref: str
    tmux_session: str
    attach: dict[str, str]
    registration: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "sessionRef": self.session_ref,
            "tmuxSession": self.tmux_session,
            "attach": dict(self.attach),
            "registration": dict(self.registration),
        }


def start_argv(request: TuiStartRequest) -> list[str]:
    """The target CLI command: the launcher rebuilds the full argv, not S2's.

    The session document stores the carrier command (pi: ``["pi"]``), which is
    NOT a launch argv; driving ``hyprial agent start`` re-adds the extension
    and launch flags the fresh TUI needs.
    """

    if request.harness not in RESUMABLE_TUI_HARNESSES:
        raise TuiStartError(
            ipc_errors.TRANSFER_UNSUPPORTED_HARNESS,
            f"{request.harness} has no resumable --tmux TUI form",
            {"harness": request.harness},
        )
    argv = [
        "agent",
        "start",
        request.harness,
        "--name",
        request.actor,
        "--resume",
        request.session_ref,
        "--tmux",
    ]
    if request.cwd:
        argv += ["--cwd", request.cwd]
    return argv


def registration_confirmed(
    harness: str, actor: str, session_ref: str, status: Mapping[str, Any]
) -> bool:
    """The CLI's own predicates, re-applied to the target's daemon-status payload.

    The actor is compared by its LOCAL name (``agent_uri_actor``): the daemon
    registers the canonical ``agent:<owner>:<machine>:<name>`` URI while the
    request may carry either spelling, so a raw string compare would never
    match a canonical registration against a bare ``--name`` repeatedly.
    """

    sessions = status.get("interactiveSessions", [])
    if not isinstance(sessions, list):
        return False
    wanted = agent_uri_actor(actor) or actor
    for item in sessions:
        if not isinstance(item, Mapping):
            continue
        observed = item.get("actor")
        if not isinstance(observed, str) or (agent_uri_actor(observed) or observed) != wanted:
            continue
        if item.get("sessionRef") != session_ref:
            continue
        if harness == "claude":
            if item.get("channelConfirmed") is True:
                return True
        elif harness == "pi":
            if item.get("source") == "pi-extension":
                return True
    return False


def attach_hints_for(platform: str, tmux_session: str) -> dict[str, str]:
    """Both hints normally; a WSL target gets only the plain attach line."""

    hints = tmux_module.attach_hints(tmux_session)
    if platform.strip().lower() in _WSL_PLATFORMS:
        return {"wsl-linux-terminal": hints["wsl-linux-terminal"]}
    return hints


def drive_target_start(request: TuiStartRequest, *, remote: Any) -> TuiStartResult:
    """Run the target CLI, then re-verify registration from the target's ps.

    ``remote`` is the move's ``RemoteRunner`` seam: ``run_json`` runs the target
    machine's own hyprial CLI.  The CLI waits for registration itself; the
    second read here is the fence, not a second wait.
    """

    response = remote.run_json(start_argv(request))
    tmux_session = response.get("tmuxSession")
    if not isinstance(tmux_session, str) or not tmux_session:
        raise TuiStartError(
            "TRANSFER_PROTOCOL",
            "the target start returned no tmuxSession to attach to",
            {"response": response},
        )
    status = remote.run_json(["daemon", "status"])
    if not registration_confirmed(request.harness, request.actor, request.session_ref, status):
        raise TuiStartError(
            "CHANNEL_REGISTRATION_FAILED",
            f"{request.harness}:{request.actor} did not register session "
            f"{request.session_ref!r} on the target",
            {"tmuxSession": tmux_session, "actor": request.actor},
        )
    return TuiStartResult(
        actor=request.actor,
        session_ref=request.session_ref,
        tmux_session=tmux_session,
        attach=attach_hints_for(request.platform, tmux_session),
        registration={"source": "target-ps"},
    )


# ---------------------------------------------------------------------------
# pre-commit cleanup: the target's three residue classes, always named
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TargetResidue:
    """What a pre-S7 failure may have left on the target."""

    tmux_session: str | None = None
    process_identity: str | None = None
    landed_actor: str | None = None
    entity_token: str | None = None


@dataclass(frozen=True, slots=True)
class TargetCleanupPorts:
    """The target-side teardown verbs; a fake target supplies test doubles."""

    kill_tmux: Callable[[str], None]
    kill_process: Callable[[str], None]
    undo_land: Callable[[str, str], None]


#: Per-class dispositions.  ``residual`` and ``left-in-place`` both mean
#: "not cleared"; only the first says a teardown was attempted and failed.
CLEARED = "cleared"
RESIDUAL = "residual"
ABSENT = "absent"
LEFT_IN_PLACE = "left-in-place"
_NOT_CLEARED = (RESIDUAL, LEFT_IN_PLACE)


@dataclass(frozen=True, slots=True)
class TargetCleanupReport:
    """Always lists tmux / process / landed, each with its disposition."""

    tmux: str
    process: str
    landed: str
    detail: tuple[str, ...] = ()

    @property
    def residual(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in ("tmux", "process", "landed")
            if getattr(self, name) in _NOT_CLEARED
        )

    @classmethod
    def left_in_place(cls, residue: "TargetResidue") -> "TargetCleanupReport":
        """Post-S7: report only.  Nothing is torn down, not even attempted.

        The target's landed TUI is a committed result, not a residue; it is
        named here so an operator sees it is intentionally left running.
        """

        return cls(
            tmux=LEFT_IN_PLACE if residue.tmux_session is not None else ABSENT,
            process=(
                LEFT_IN_PLACE if residue.process_identity is not None else ABSENT
            ),
            landed=(
                LEFT_IN_PLACE
                if residue.landed_actor is not None or residue.entity_token is not None
                else ABSENT
            ),
            detail=(
                "post-S7: the target may already own the session; report only, "
                "no target teardown was attempted",
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "tmux": self.tmux,
            "process": self.process,
            "landed": self.landed,
            "residual": list(self.residual),
            "detail": list(self.detail),
        }


def cleanup_target(residue: TargetResidue, *, ports: TargetCleanupPorts) -> TargetCleanupReport:
    """Clear each present residue; a failed clear is reported, never masked."""

    states = {"tmux": ABSENT, "process": ABSENT, "landed": ABSENT}
    detail: list[str] = []
    attempts = (
        ("tmux", residue.tmux_session, lambda: ports.kill_tmux(residue.tmux_session)),
        (
            "process",
            residue.process_identity,
            lambda: ports.kill_process(residue.process_identity),
        ),
    )
    for name, value, action in attempts:
        if value is None:
            continue
        try:
            action()
            states[name] = CLEARED
        except Exception as error:  # noqa: BLE001 - reported as residual
            states[name] = RESIDUAL
            detail.append(f"{name}: {error}")
    if residue.landed_actor is None and residue.entity_token is None:
        states["landed"] = ABSENT
    elif residue.landed_actor is None or residue.entity_token is None:
        # Half a landed record cannot be undone safely; surface it, never hide
        # it as "absent".
        states["landed"] = RESIDUAL
        detail.append("landed: incomplete residue (actor or entity_token missing)")
    else:
        try:
            ports.undo_land(residue.landed_actor, residue.entity_token)
            states["landed"] = CLEARED
        except Exception as error:  # noqa: BLE001 - reported as residual
            states["landed"] = RESIDUAL
            detail.append(f"landed: {error}")
    return TargetCleanupReport(
        states["tmux"], states["process"], states["landed"], tuple(detail)
    )


@dataclass(frozen=True, slots=True)
class PreCommitRollbackReport:
    source_restarted: bool
    source_error: str | None
    target: TargetCleanupReport

    def to_json(self) -> dict[str, Any]:
        return {
            "sourceRestarted": self.source_restarted,
            "sourceError": self.source_error,
            "target": self.target.to_json(),
        }


def rollback_pre_commit(
    *,
    source_request: Any,
    source_tmux: Any,
    source_tmux_bin: str,
    target_residue: TargetResidue,
    target_ports: TargetCleanupPorts,
    launch: Callable[..., None] | None = None,
    target_started: bool | None = None,
) -> PreCommitRollbackReport:
    """Undo a failure *before* S7: source restart (S2) + target residue cleanup.

    After the S7 commit point the design says **report only, never roll back**:
    restarting the source is the true dual-active window, and the target's
    landed TUI is a committed result, not a residue.  In that window neither
    the source restart nor any target teardown port is called; the report names
    the three classes as ``left-in-place``.  ``target_started`` defaults to the
    request's own S7 flag so the window cannot be selected the wrong way.
    """

    if target_started is None:
        target_started = bool(getattr(source_request, "target_started", False))
    if target_started:
        return PreCommitRollbackReport(
            source_restarted=False,
            source_error=(
                f"{DualActiveRiskWindow.__name__}: the target may already own "
                "this session (after the S7 commit point); the design says "
                "report only, never roll back"
            ),
            target=TargetCleanupReport.left_in_place(target_residue),
        )
    source_restarted, source_error = True, None
    try:
        restart_session(
            source_request,
            tmux=source_tmux,
            tmux_bin=source_tmux_bin,
            launch=launch,
        )
    except Exception as error:  # noqa: BLE001 - reported, never masked
        source_restarted, source_error = False, f"{type(error).__name__}: {error}"
    return PreCommitRollbackReport(
        source_restarted=source_restarted,
        source_error=source_error,
        target=cleanup_target(target_residue, ports=target_ports),
    )


__all__ = [
    "ABSENT",
    "CLEARED",
    "ENTRYPOINT_HEADLESS",
    "ENTRYPOINT_TUI",
    "LEFT_IN_PLACE",
    "PreCommitRollbackReport",
    "RESIDUAL",
    "RESUMABLE_TUI_HARNESSES",
    "TargetCleanupPorts",
    "TargetCleanupReport",
    "TargetResidue",
    "TuiStartError",
    "TuiStartRequest",
    "TuiStartResult",
    "attach_hints_for",
    "cleanup_target",
    "drive_target_start",
    "registration_confirmed",
    "rollback_pre_commit",
    "start_argv",
]
