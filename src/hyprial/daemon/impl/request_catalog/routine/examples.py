"""Synthetic examples for routine audit and activation requests."""

from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet, ipc_errors


USER = "user:owner"


def case(name, kind, params, result=None, error=None, reason=""):
    return MethodExample(name=name, kind=kind, description=name.replace("-", " "),
                         params=params, result=result, error=error, reason=reason)


def example_set(method):
    audit = method == "routine.audit"
    name = method.removeprefix("routine.")
    params = {"actor": USER} if audit else {"actor": USER, "name": "daily"}
    result = ({"quarantined": [], "addressMigrations": [{"routine": "daily", "field": "actor",
                "before": "old", "after": "new", "migratedAtMs": 1}]} if audit else
              {"name": "daily", "enabled": name == "resume"})
    invalid = [] if audit else {"actor": USER}
    invalid_message = "params must be an object" if audit else "name must be a non-empty string"
    cases = [case("success", "success", params, result=result),
             case("invalid-params", "invalid-params", invalid,
                  error=MethodExampleError(ipc_errors.INVALID_ARGUMENT, invalid_message)),
             case("unavailable", "domain-error", params,
                  error=MethodExampleError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running"))]
    if audit:
        cases.append(case("empty", "empty", params,
                          result={"quarantined": [], "addressMigrations": []},
                          reason="No routines are quarantined and no addresses were migrated."))
    return MethodExampleSet(method=method, cases=tuple(cases),
                            empty_not_applicable="The operation returns the named routine." if not audit else "")


ROUTINE_EXAMPLES = tuple(example_set(name) for name in ("routine.audit", "routine.pause", "routine.resume"))
