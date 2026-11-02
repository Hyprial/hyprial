"""Startup JSON and human rendering for a GUI session."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..worktree import auto_worktree_payload
from .resources import redact_remote
from .server import NOT_IMPLEMENTED_TEXT


def startup_payload(session: Any) -> dict[str, Any]:
    """The one machine-readable startup record emitted before the wait."""
    plan = session.plan
    auto = auto_worktree_payload(plan.auto_worktree)
    return {
        "ok": True,
        "mode": plan.mode,
        "gui_home": str(plan.project.home),
        "git_worktree": str(plan.project.worktree),
        "git_branch": plan.project.branch,
        "url": plan.url,
        "host": plan.host,
        "port": plan.port,
        "hot_reload": plan.hot_reload,
        "backend_endpoint": plan.backend_endpoint or "",
        "backend_owned": plan.backend_owned,
        "frontend_pid": session.frontend.process.pid,
        "backend_pid": (
            session.backend.process.pid if session.backend is not None else None
        ),
        "node_version": plan.project.node_version,
        "npm_version": plan.project.npm_version,
        "installed": plan.project.installed,
        "assets": (
            "installed" if plan.project.installed else
            "explicit-unverified" if plan.installed_assets is not None else "source"
        ),
        "source_commit": plan.project.source_commit,
        "source_remote": redact_remote(plan.project.source_remote),
        "auto_worktree": auto,
        "dev_prompt": plan.dev_prompt,
        "business": NOT_IMPLEMENTED_TEXT,
        "note": (
            "The GUI uses the independent placeholder boundary; business "
            "calls are not wired to a daemon."
        ),
    }


def render_startup(payload: Mapping[str, Any]) -> str:
    """Human output for a running session; the URL is stable and copyable."""
    lines = [
        f"Hyprial GUI ({payload['mode']})",
        f"  GUI_HOME : {payload['gui_home']}",
        f"  Git      : {payload['git_branch']} at {payload['git_worktree']}",
        f"  URL      : {payload['url']}",
        f"  backend  : {payload['backend_endpoint']} "
        f"({'owned by CLI' if payload['backend_owned'] else 'external, loopback only'})",
        f"  hot reload: {'enabled' if payload['hot_reload'] else 'not used'}",
        f"  business : {payload['business']}",
        (
            "  assets   : verified built GUI (static)"
            if payload.get("installed")
            else "  assets   : explicit built GUI (unverified; no release manifest)"
            if payload.get("assets") == "explicit-unverified"
            else "  assets   : source GUI project"
        ),
        "  stop     : Ctrl-C (only this invocation's processes are reclaimed)",
    ]
    if payload.get("dev_prompt"):
        lines.extend(["", str(payload["dev_prompt"])])
    return "\n".join(lines)
