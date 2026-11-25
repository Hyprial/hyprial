"""Synthetic IPC examples for the routine plan request."""

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors


_SOURCE_YAML = '''version: 1
name: daily
actor: agent:owner:node:worker
schedule: {interval: 60}
source: SOURCE
policy:
  routes: [{tag: "route:self", target: self}]
  default: self
  task_template: "{{task.description}}"
on_task_timeout: {action: escalate, escalate_to: "user:owner"}
'''
_TASKWARRIOR = _SOURCE_YAML.replace("SOURCE", '{kind: taskwarrior, filter: "+ready"}')
_JOURNAL = _SOURCE_YAML.replace("SOURCE", "{kind: pac-journal, idle_threshold: 120}")
_OWNED = _TASKWARRIOR.replace("actor: agent:owner:node:worker", "launch: {harness: codex}")
_SCHEDULED = '''version: 2
mode: scheduled
name: daily
actor: agent:owner:node:worker
schedule: {interval: 60}
task: Check the inbox
on_task_timeout: {action: escalate, escalate_to: "user:owner"}
'''


def _plan(source, *, scheduled=False, owned=False):
    return {"plan": {
        "name": "daily", "mode": "scheduled" if scheduled else "source", "role": "dispatch",
        "actor": None if owned else "agent:owner:node:worker",
        "actorOwnership": "routine" if owned else "borrowed",
        "launch": {"harness": "codex", "args": []} if owned else None,
        "overlap": "skip" if scheduled else None, "missedPeriods": "skip" if scheduled else None,
        "intervalSeconds": 60.0, "source": source,
        "routes": [{"tag": "route:self", "kind": "self", "value": "self"}],
        "defaultRoute": "self", "limits": {"maxInFlight": 1 if scheduled else 3,
            "circuitBreaker": {"windowRuns": 5, "escalateRatio": 1.0, "action": "pause+alarm"}},
        "escalateTo": "user:owner",
    }}


EXAMPLES = (MethodExampleSet(method="routine.plan", cases=(
    MethodExample(name="taskwarrior", kind="success", description="Plan a taskwarrior routine.",
        params={"yaml": _TASKWARRIOR, "label": "routine.yaml"},
        result=_plan({"kind": "taskwarrior", "filter": "+ready"})),
    MethodExample(name="routine-owned", kind="success", description="Plan a routine-owned explicit launch.",
        params={"yaml": _OWNED, "label": "routine.yaml"},
        result=_plan({"kind": "taskwarrior", "filter": "+ready"}, owned=True)),
    MethodExample(name="pac-journal", kind="success", description="Plan a PAC journal routine.",
        params={"yaml": _JOURNAL, "label": "routine.yaml"},
        result=_plan({"kind": "pac-journal", "idleThresholdSeconds": 120.0})),
    MethodExample(name="scheduled", kind="success", description="Plan a scheduled borrowed actor.",
        params={"yaml": _SCHEDULED, "label": "routine.yaml"},
        result=_plan({"kind": "scheduled"}, scheduled=True)),
    MethodExample(name="non-object", kind="invalid-params", description="IPC params must be an object.",
        params=[], error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, "params must be an object")),
    MethodExample(name="missing-version", kind="domain-error", description="The routine schema requires a version.",
        params={"yaml": "name: daily", "label": "routine.yaml"},
        error=MethodExampleError(ipc_errors.ROUTINE_SCHEMA_ERROR, "routine schema error: routine.yaml.version must be 1 (stored source format) or 2")),
), empty_not_applicable="An empty routine document does not define a plan."),)
