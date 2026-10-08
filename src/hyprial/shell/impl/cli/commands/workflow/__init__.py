"""Workflow command family split by operation."""

from . import control, inspect, misc, overview, run  # noqa: F401

_ORDER = {
    name: index
    for index, name in enumerate(
        (
            "plan", "run", "status", "list", "inspect", "cancel", "events",
            "context", "reset", "complete", "fail", "gc",
        )
    )
}
run.workflow_app.registered_commands.sort(
    key=lambda command: _ORDER.get(command.name or "", len(_ORDER))
)
