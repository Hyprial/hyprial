# Decision record (file header; no inline comments by repository rule):
# - Agent teleport slice 3: strict resume and the resume pre-check as units
#   with plain arguments, runnable without a ``transfer.receive`` params dict
#   or a daemon application.
# - ``strict_resume`` reads only ``wait_ready`` and ``session_refs`` and never
#   stops or removes anything: compensation stays with each caller.  Its
#   refusal is ``STRICT_RESUME_FAILED`` carrying the data shape that
#   ``_verify_started_resume`` publishes.  ``StrictResumeError`` adds ``ready``
#   so callers that keep their own historical wording can tell "never ready"
#   from "ready on another ref" without parsing text.
# - ``require_resumable_session`` reads no process environment and no real
#   home: the daemon passes ``Path.home()`` and ``PI_CODING_AGENT_DIR``.
# - Session-file helpers are imported inside the function: importing the
#   ``transfer`` package at module load pulls the whole transfer stack, which
#   the activation module (same package) must stay free of.
"""Strict resume verification and the resumable-transcript pre-check."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

from hyprial.kernel import DaemonRequestError, ipc_errors

if TYPE_CHECKING:
    from hyprial.identity import AgentRuntimeContext

RESUMABLE_HARNESSES = ("claude", "codex", "pi")


class ResumeHarnesses(Protocol):
    """The harness-runtime reads strict resume needs.

    Strict resume also asks the same object whether the worker became ready
    (``wait_ready(harness, name, timeout) -> bool``).  That read is not
    declared here on purpose: the readiness wait is owned by the harness
    runtime, and this module must not define a second one.
    """

    def session_refs(self) -> Mapping[tuple[str, str, str], str]: ...


def session_ref_for(
    refs: Mapping[tuple[str, str, str], str] | Mapping[tuple[str, str], str],
    *,
    harness: str,
    name: str,
    member: str | None,
) -> str | None:
    """Read a member-scoped ref, explicitly adapting legacy two-key callers."""

    resolved_member = member or name
    scoped_key = (harness, name, resolved_member)
    if scoped_key in refs:
        return refs[scoped_key]  # type: ignore[index]
    if resolved_member == name:
        legacy_key = (harness, name)
        return refs.get(legacy_key)  # type: ignore[arg-type]
    return None


class StrictResumeError(DaemonRequestError):
    """``STRICT_RESUME_FAILED`` that also says whether the worker became ready."""

    def __init__(self, message: str, data: dict[str, object], *, ready: bool) -> None:
        super().__init__(ipc_errors.STRICT_RESUME_FAILED, message, data)
        self.ready = ready


def strict_resume(
    harnesses: ResumeHarnesses,
    *,
    harness: str,
    name: str,
    member: str | None = None,
    session_ref: str,
    timeout: float,
) -> str:
    """Return the established ref once the worker is ready ON ``session_ref``."""

    ready = harnesses.wait_ready(harness, name, timeout)
    resumed = (
        session_ref_for(
            harnesses.session_refs(), harness=harness, name=name, member=member
        )
        if ready
        else None
    )
    if ready and resumed == session_ref:
        return resumed
    detail = (
        f"did not become ready within {timeout}s"
        if not ready
        else f"established session {resumed!r} instead"
    )
    raise StrictResumeError(
        f"resume of {harness} session {session_ref!r} for {name} did not hold: "
        f"the worker {detail}",
        {
            "harness": harness,
            "name": name,
            "sessionRef": session_ref,
            "established": resumed,
        },
        ready=bool(ready),
    )


def require_resumable_harness(harness: str, headless: bool) -> None:
    """Refuse a harness or entrypoint that cannot resume a session."""

    if harness not in RESUMABLE_HARNESSES or not headless:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"resuming a session is supported for headless "
            f"{', '.join(sorted(RESUMABLE_HARNESSES))} only, not "
            f"{harness}{'' if headless else ' (interactive)'}",
        )


def require_resumable_session(
    *,
    harness: str,
    name: str,
    headless: bool,
    cwd: str,
    session_ref: str,
    home: Path,
    pi_agent_dir: str | Path | None = None,
    runtime_context: AgentRuntimeContext | None = None,
) -> Path:
    """Return the transcript of ``session_ref`` or refuse before anything starts.

    pi given an unknown ``--session-id`` warns and begins a fresh session
    under that very id, so a post-start id comparison passes on a cold start;
    for pi this file check is the gate that holds.
    """

    from hyprial.daemon.impl.transfer.archive.session_files import (
        SessionFileError,
        SessionFileNotFound,
        locate_session_file,
        pi_session_file,
    )

    require_resumable_harness(harness, headless)
    try:
        if runtime_context is None and harness == "pi" and pi_agent_dir:
            located = pi_session_file(Path(pi_agent_dir).expanduser(), cwd, session_ref)
        else:
            located = locate_session_file(
                harness,
                cwd,
                session_ref,
                home=home,
                runtime_context=runtime_context,
            )
        if not located.is_file():
            raise SessionFileNotFound(f"not a regular session file: {located}")
    except SessionFileError as error:
        raise DaemonRequestError(
            ipc_errors.RESUME_SESSION_NOT_FOUND,
            f"cannot resume {harness} session {session_ref!r} for "
            f"{name}: {error}; nothing was started",
            {
                "harness": harness,
                "name": name,
                "sessionRef": session_ref,
                "cwd": cwd,
                "reason": (
                    "not-found" if isinstance(error, SessionFileNotFound) else "unusable"
                ),
            },
        ) from error
    return located
