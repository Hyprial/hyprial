# Decision record (file header; no inline comments by repository rule):
# - These are the host operations the teleport source adapter runs on the
#   daemon application: admission of a name against the desired state, the
#   stop (the former transfer quiesce without any custody chain), the
#   non-strict restart, and the launch identity stamped into the bundle.
#   They moved here, behaviour unchanged, when the legacy transfer surface
#   was removed; the helpers keep their refusal texts because the adapter
#   forwards them inside ``TELEPORT_RUNTIME_UNSUPPORTED``.
# - A name is resolved against both desired-state tables.  Exactly one table
#   may speak for it: a managed headless worker, or a CLI-hosted interactive
#   session (refused by the preflight, never by this module).  A name in both
#   is an ambiguity, never a silent first-table pick.
# - The restart is deliberately not strict: it restores the operator's
#   pre-teleport state on the machine where the session files never left, so
#   a dead session ref degrades to the ordinary cold-start fallback.
# - Heavy modules are imported inside the functions so the request catalog can
#   import the wiring package without loading the composition.
"""Source-side host operations of an agent teleport."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hyprial.kernel import HarnessLaunchSpec, agent_uri_actor

ENTRYPOINT_HEADLESS = "headless"
ENTRYPOINT_TUI = "tui"

_SESSION_RUNTIME_HARNESSES = frozenset({"claude", "pi", "codex"})
_SESSION_SOURCE_HARNESSES = {
    "claude-channel": "claude",
    "pi-extension": "pi",
    "codex-app-server": "codex",
}


class AdmissionRefusal(ValueError):
    """A name that cannot be admitted for teleport; the text says why."""


@dataclass(frozen=True, slots=True)
class SourceTarget:
    """The one desired-state table that spoke for a name."""

    name: str
    harness: str
    entrypoint: str
    spec: HarnessLaunchSpec | None = None


def _session_harness(session: Any) -> str | None:
    runtime = (session.runtime or "").split("_", 1)[0]
    if runtime in _SESSION_RUNTIME_HARNESSES:
        return runtime
    return _SESSION_SOURCE_HARNESSES.get(session.source)


def _session_local_name(
    session: Any, local_name: Callable[[str], str | None] | None
) -> str:
    resolved = None if local_name is None else local_name(session.actor)
    return resolved or agent_uri_actor(session.actor) or session.actor


def resolve_source_target(
    state: Any,
    *,
    name: str,
    harness: str | None = None,
    local_name: Callable[[str], str | None] | None = None,
) -> SourceTarget:
    """Resolve ``name`` across both tables; fail loudly on an ambiguity."""

    from hyprial.daemon.impl.harnesses.capabilities import DECLARED_HARNESSES

    harness_matches = [
        spec
        for spec in state.harnesses
        if spec.name == name
        and spec.harness in DECLARED_HARNESSES
        and (harness is None or spec.harness == harness)
    ]
    session_matches = [
        session
        for session in state.interactive_sessions
        if _session_local_name(session, local_name) == name
        and (harness is None or _session_harness(session) == harness)
    ]
    if harness_matches and session_matches:
        raise AdmissionRefusal(
            f"{name!r} is registered in both managed harnesses and interactive "
            "sessions on this node; refusing instead of choosing one -- stop "
            "or rename one side and retry"
        )
    if len(harness_matches) > 1:
        raise AdmissionRefusal(
            f"more than one managed harness named {name!r}; "
            "pass --harness to disambiguate"
        )
    if session_matches:
        resolved = _session_harness(session_matches[0])
        if resolved is None:
            raise AdmissionRefusal(
                f"interactive session {name!r} does not name a transferable harness"
            )
        return SourceTarget(name, resolved, ENTRYPOINT_TUI)
    if harness_matches:
        spec = harness_matches[0]
        return SourceTarget(name, spec.harness, ENTRYPOINT_HEADLESS, spec)
    raise AdmissionRefusal(
        f"no managed harness or interactive session named {name!r} in desired state"
    )


def desired_spec(app: Any, name: str, harness: str | None) -> HarnessLaunchSpec:
    """The streaming headless launch spec that speaks for ``name``."""

    from hyprial.daemon.impl.harnesses import is_streaming_spec
    from hyprial.daemon.impl.transfer.archive.session_files import (
        TRANSFERABLE_HARNESSES,
    )
    from hyprial.kernel import DaemonRequestError, ipc_errors

    try:
        target = resolve_source_target(
            app.desired_state.load(),
            name=name,
            harness=harness,
            local_name=app.agents.local_actor,
        )
    except AdmissionRefusal as error:
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    spec = target.spec
    if target.entrypoint != ENTRYPOINT_HEADLESS or spec is None:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"{target.harness}:{target.name} is an interactive session; "
            "only managed headless workers can be teleported",
        )
    if spec.harness not in TRANSFERABLE_HARNESSES:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"{spec.harness} workers cannot be teleported: only pi, codex, "
            "and claude headless workers can",
        )
    if not is_streaming_spec(spec):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"{spec.harness}:{spec.name} is not a streaming headless "
            "worker; only streaming workers carry a resumable sessionRef",
        )
    return spec


def quiesce(app: Any, name: str, harness: str | None, operation_id: str) -> tuple[Any, bool]:
    """Stop the worker after syncing its session ref; return its spec and the proof."""

    from hyprial.daemon.impl.lifecycle.primitives import quiesce_agent
    from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation

    def stop(spec: HarnessLaunchSpec) -> None:
        app._run_lifecycle_operation(
            LifecycleOperation.deactivate(operation_id, app._lifecycle_spec(spec))
        )

    quiescence = quiesce_agent(
        resolve_spec=lambda: desired_spec(app, name, harness),
        sync_session_refs=app._harnesses.reconcile_session_refs,
        deactivate=stop,
        prove_quiescence=lambda spec: {"quiescent": True},
    )
    return quiescence.spec.to_json(), quiescence.quiescent


def restart(app: Any, spec_json: Any, operation_id: str) -> None:
    """Put the stopped worker back exactly as it was."""

    from hyprial.daemon.impl.lifecycle_manager import LifecycleOperation

    spec = HarnessLaunchSpec.from_json(spec_json, "spec")
    app._run_lifecycle_operation(
        LifecycleOperation.create(operation_id, app._lifecycle_spec(spec))
    )


def launch_wiring_identity(agent: object, harness: str) -> dict[str, object]:
    """The source registry row's launch wiring, as the bundle's identity bag.

    The model vendor, model and preferred harness live only in the source
    daemon's SQLite, and a landed row without them cannot be resolved into a
    launch.  Absent values are omitted.  ``harness`` is the teleport's own
    harness, never the row's ``preferredHarness``: an agent may legally have
    swapped harnesses, and the landing-side gate must not refuse it.
    """

    if not isinstance(agent, dict):
        return {}
    provider = agent.get("provider")
    model = agent.get("model")
    launch: dict[str, object] = {}
    if isinstance(provider, str) and provider:
        launch["provider"] = provider
    if isinstance(model, str) and model:
        launch["model"] = model
    if launch and isinstance(harness, str) and harness:
        launch["harness"] = harness
    return {"launch": launch} if launch else {}
