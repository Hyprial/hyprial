"""Synthetic model configuration receipts; no model service is contacted."""

from hyprial.error import codes
from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet

CONFIG = {"authKind": "connection", "harness": "pi", "mode": "headless",
          "model": "hy4-flash", "connectionRef": "example-connection", "configRevision": 1}
CONFIGURE = {"name": "worker", "operationId": "configure-example",
             "expectedBindingRevision": 0, "expectedCellRevision": 1, "configuration": CONFIG}
CANCEL = {"name": "worker", "operationId": "configure-example", "expectedBindingRevision": 0}
EMPTY = {"ok": True, "agentId": "a.example", "member": "member-example", "binding": None,
         "operation": None, "cell": {"cellId": "c.example", "revision": 1, "intentMatches": None},
         "cleanupPending": False, "runtime": "not-verified"}
RECEIPT = {"operationId": "configure-example", "phase": "finalized", "requestDigest": "a" * 64,
           "expectedBindingRevision": 0, "resultBindingRevision": 1, "coordinationPhase": "applied",
           "expectedCellRevision": 1, "observedCellRevision": 2}
SAVED = {**EMPTY, "binding": {"bindingRef": "mb.example", "bindingRevision": 1,
         "configuration": {**CONFIG, "intentDigest": "b" * 64}}, "operation": RECEIPT,
         "cell": {"cellId": "c.example", "revision": 2, "intentMatches": True}}
CANCELLED = {**EMPTY, "operation": {**RECEIPT, "phase": "cancelled",
             "resultBindingRevision": 0, "coordinationPhase": "compensated", "observedCellRevision": 3}}


def examples(method, params, result, *, empty=False):
    return MethodExampleSet(
        method=method, empty_not_applicable="" if empty else "An operation returns its durable receipt.",
        cases=(
            MethodExample(name="read-back", kind="empty" if empty else "success",
                          description="Synthetic local receipt; process activation is unverified.",
                          params=params, result=result,
                          reason="An agent may have no saved model configuration." if empty else ""),
            MethodExample(name="missing-name", kind="invalid-params", description="Target is required.",
                          params={}, error=MethodExampleError(codes.INVALID_ARGUMENT, "Invalid target.")),
            MethodExample(name="foreign-caller", kind="domain-error", description="Worker denied before reads.",
                          params=params, error=MethodExampleError(codes.CALLER_NOT_AUTHORIZED, "Local operator required.")),
            MethodExample(name="unreadable-binding", kind="domain-error", description="State is not guessed.",
                          params=params, error=MethodExampleError(codes.MODEL_CONFIGURATION_INCOMPLETE,
                              "Binding state is unreadable.", {"phase": "bind", "reason": "state-unreadable"})),
        ) + ((MethodExample(name="bound", kind="success", description="Previously saved configuration.",
                            params=params, result=SAVED),) if empty else ()),
    )


EXAMPLES = (
    examples("agent.model.configure", CONFIGURE, SAVED),
    examples("agent.model.status", {"name": "worker"}, EMPTY, empty=True),
    examples("agent.model.cancel", CANCEL, CANCELLED),
)
