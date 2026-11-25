"""Startup JSON and human rendering for a GUI session.

The record's machine contract is ``contract/cli/gui-startup-record.v1.json``;
``record_version`` is bumped only when a required field changes meaning.
Decision host-serves-daemon-in-process-20261119 (2822, B3): for the daemon driver
the host serves the request-http dispatch itself, so ``backend_pid`` is null and
``backend_endpoint`` is empty; no key is added or removed and ``record_version``
stays 1 (owner-approved).  The contract-mock driver keeps its integer pid.
``dev_tools`` and ``api_docs`` (PR 4b-C) are optional additions: the first is the
plan's ``--with-dev-tools`` decision, the second what the host confirmed on its
ready line.  ``live_source`` (PR 5) is optional too and equals ``hot_reload``;
the Vite port is not part of the record (owner decision Q-6).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..worktree import auto_worktree_payload
from .resources import redact_remote


RECORD_VERSION = 1


def startup_payload(session: Any) -> dict[str, Any]:
    """The one machine-readable startup record emitted before the wait."""
    plan = session.plan
    auto = auto_worktree_payload(plan.auto_worktree)
    hosted = plan.project.installed
    handoff = getattr(session, "handoff_path", None)
    admission = plan.admission
    if hosted:
        business = (
            "daemon through the request-http v1 dispatch inside the same-origin Python session host"
        )
        note = (
            "Foreground installed GUI: the Python session host serves the verified "
            "built-in assets and relays the daemon through the request-http v1 dispatch "
            "in its own process; hyprial gui ensures the daemon. The handoff opens this launch only and is not E2E "
            "acceptance."
        )
    else:
        business = (
            "Source GUI: daemon through the request-http v1 dispatch inside the "
            "same-origin Python session host"
            if plan.driver == "daemon"
            else f"Source GUI: {plan.driver} through request-http v1 and the "
            "same-origin Python session host"
        )
        note = (
            "Foreground source GUI: the Python session host serves the built GUI "
            "(or relays the Vite dev server with --live-source) and relays the "
            "backend driver (the daemon through the request-http v1 dispatch in its own "
            "process, or the Contract Mock over loopback); with the daemon driver, "
            "hyprial gui ensures the daemon. The handoff opens this launch only and is not E2E "
            "acceptance."
        )
    return {
        "ok": True,
        "record_version": RECORD_VERSION,
        "mode": plan.mode,
        "gui_home": str(plan.project.home),
        "git_worktree": str(plan.project.worktree),
        "git_branch": plan.project.branch,
        "url": plan.url,
        "host": plan.host,
        "port": plan.port,
        "hot_reload": plan.hot_reload,
        "live_source": plan.live_source,
        "dev_tools": plan.dev_tools,
        "api_docs": plan.api_docs,
        "backend_endpoint": plan.backend_endpoint or "",
        "backend_owned": plan.backend_owned,
        "backend_driver": plan.driver,
        "backend_principal": admission.principal if admission is not None else None,
        "frontend_pid": session.frontend.process.pid,
        "backend_pid": (
            session.backend.process.pid if session.backend is not None else None
        ),
        "assets_pid": (
            assets.process.pid
            if (assets := getattr(session, "assets", None)) is not None
            else None
        ),
        "node_version": plan.project.node_version,
        "npm_version": plan.project.npm_version,
        "installed": plan.project.installed,
        "assets": "installed" if plan.project.installed else "source",
        "source_commit": plan.project.source_commit,
        "source_remote": redact_remote(plan.project.source_remote),
        "auto_worktree": auto,
        "dev_prompt": plan.dev_prompt,
        "business": business,
        "note": note,
        "handoff_path": str(handoff) if handoff is not None else None,
        "handoff_note": (
            "Open the absolute URL in this private 0600 file. The host removes it after "
            "the launch grant is consumed or expires; Ctrl-C also removes it."
            if handoff is not None
            else None
        ),
    }


def _dev_tools_line(payload: Mapping[str, Any]) -> str:
    if payload.get("dev_tools") is not True:
        return "not used"
    return "enabled (API docs available)" if payload.get("api_docs") is True else "enabled (no API docs)"


def render_startup(payload: Mapping[str, Any]) -> str:
    """Human output for a running session; the URL is stable and copyable."""
    lines = [
        f"Hyprial GUI ({payload['mode']})",
        f"  GUI_HOME : {payload['gui_home']}",
        f"  Git      : {payload['git_branch']} at {payload['git_worktree']}",
        f"  URL      : {payload['url']}",
        (
            "  backend  : in the host process (request-http v1 dispatch; no separate backend process)"
            if payload.get("backend_pid") is None and not payload["backend_endpoint"]
            else f"  backend  : {payload['backend_endpoint']} "
            f"({'owned by CLI' if payload['backend_owned'] else 'external, loopback only'})"
        ),
        f"  driver   : {payload['backend_driver']}",
        f"  hot reload: {'enabled' if payload['hot_reload'] else 'not used'}",
        f"  dev tools: {_dev_tools_line(payload)}",
        (
            f"  live source: {payload['gui_home']} (edits reload in the browser)"
            if payload.get("live_source") is True
            else "  live source: not used"
        ),
        f"  business : {payload['business']}",
        (
            "  assets   : verified built GUI (served by the Python host)"
            if payload.get("installed")
            else "  assets   : source GUI project"
        ),
        "  stop     : Ctrl-C (only this invocation's processes are reclaimed)",
    ]
    if payload.get("handoff_path"):
        lines.extend(
            [
                f"  launch   : {payload['handoff_path']} (private one-time URL)",
                f"  open     : {payload['handoff_note']}",
            ]
        )
    if payload.get("dev_prompt"):
        lines.extend(["", str(payload["dev_prompt"])])
    return "\n".join(lines)
