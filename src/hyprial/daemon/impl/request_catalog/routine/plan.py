"""Read-only routine plan request and its existing domain projection."""

from hyprial.daemon.impl.request_catalog.agent.relations import _LAUNCH
from hyprial.kernel import DaemonRequestError, IdempotencySpec, MethodError, MethodSpec, ipc_errors


def obj(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_STRING = {"type": "string"}
_ROUTE = obj({"tag": _STRING, "kind": {"enum": ["target", "self", "escalate"]}, "value": _STRING}, ["tag", "kind", "value"])
_BREAKER = obj({"windowRuns": {"type": "integer"}, "escalateRatio": {"type": "number"},
                "action": {"const": "pause+alarm"}}, ["windowRuns", "escalateRatio", "action"])
_LIMITS = obj({"maxInFlight": {"type": "integer"}, "circuitBreaker": _BREAKER},
              ["maxInFlight", "circuitBreaker"])
_SOURCE = {"oneOf": [
    obj({"kind": {"const": "taskwarrior"}, "filter": _STRING}, ["kind", "filter"]),
    obj({"kind": {"const": "pac-journal"}, "idleThresholdSeconds": {"type": "number"}},
        ["kind", "idleThresholdSeconds"]),
    obj({"kind": {"const": "scheduled"}}, ["kind"]),
]}

_PLAN = obj({
    "name": _STRING, "mode": {"enum": ["source", "scheduled"]},
    "role": {"enum": ["execute", "plan", "review", "dispatch"]}, "actor": {"type": ["string", "null"]},
    "actorOwnership": {"enum": ["borrowed", "routine"]}, "launch": _LAUNCH,
    "overlap": {"enum": ["skip", None]}, "missedPeriods": {"enum": ["skip", None]},
    "intervalSeconds": {"type": "number"}, "source": _SOURCE,
    "routes": {"type": "array", "items": _ROUTE}, "defaultRoute": {"enum": ["escalate", "self"]},
    "limits": _LIMITS, "escalateTo": _STRING, "produces": _STRING,
}, ["name", "mode", "role", "actor", "actorOwnership", "launch", "overlap", "missedPeriods",
    "intervalSeconds", "source", "routes", "defaultRoute", "limits", "escalateTo"])

SPEC = MethodSpec(
    name="routine.plan", owner="daemon.routine",
    params_schema=obj({"yaml": _STRING, "label": _STRING, "actor": _STRING,
                       "sessionRef": _STRING}, ["yaml", "label"]),
    result_schema=obj({"plan": _PLAN}, ["plan"]),
    errors=tuple(MethodError(code) for code in (ipc_errors.INVALID_ARGUMENT, ipc_errors.ROUTINE_SCHEMA_ERROR,
                                                ipc_errors.DAEMON_RESTORING, ipc_errors.DAEMON_ERROR)),
    mode="unary", mutates=False, retry="safe", audience=("local-operator", "bound-agent"),
    http_exposed=False, confirmation="none",
    idempotency=IdempotencySpec(mode="safe", scope="routine-plan"),
)


def plan(app, params, *, trusted_message_origin=None):
    from hyprial.daemon.impl.application.ports import RoutineSchemaPortError

    try:
        spec = app._routine_text_loader(params["yaml"], label=params["label"])
    except RoutineSchemaPortError as error:
        raise DaemonRequestError(ipc_errors.ROUTINE_SCHEMA_ERROR, f"routine schema error: {error}") from error
    return {"plan": {
        "name": spec.name,
        "mode": spec.mode,
        "role": spec.role,
        "actor": spec.actor or spec.produces,
        "actorOwnership": "borrowed" if spec.actor else "routine",
        "launch": spec.launch,
        "overlap": "skip" if spec.mode == "scheduled" else None,
        "missedPeriods": "skip" if spec.mode == "scheduled" else None,
        "intervalSeconds": spec.interval_seconds,
        "source": {
            "kind": spec.source_kind,
            **({"filter": spec.source_filter} if spec.source_kind == "taskwarrior" else {}),
            **({"idleThresholdSeconds": spec.source_idle_threshold_seconds}
               if spec.source_kind == "pac-journal" else {}),
        },
        **({"produces": spec.produces} if spec.produces is not None else {}),
        "routes": [{"tag": r.tag, "kind": r.kind, "value": r.value} for r in spec.routes],
        "defaultRoute": spec.default_route,
        "limits": {
            "maxInFlight": spec.limits.max_in_flight,
            "circuitBreaker": {
                "windowRuns": spec.limits.circuit_breaker.window_runs,
                "escalateRatio": spec.limits.circuit_breaker.escalate_ratio,
                "action": spec.limits.circuit_breaker.action,
            },
        },
        "escalateTo": spec.escalate_to,
    }}


BINDINGS = ((SPEC, plan),)
