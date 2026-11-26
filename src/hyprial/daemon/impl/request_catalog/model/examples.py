"""Synthetic model configuration receipts; no model service is contacted."""

from hashlib import sha256

from hyprial.error import codes
from hyprial.kernel import MethodExample, MethodExampleError, MethodExampleSet

_DIGEST = sha256(b"example-not-a-credential").hexdigest()
CONFIG = {"authKind": "connection", "harness": "pi", "mode": "headless",
          "model": "hy4-flash", "connectionRef": "example-connection", "configRevision": 1}
CONFIGURE = {"name": "worker", "operationId": "configure-example",
             "expectedBindingRevision": 0, "expectedCellRevision": 1, "configuration": CONFIG}
CANCEL = {"name": "worker", "operationId": "configure-example", "expectedBindingRevision": 0}
EMPTY = {"ok": True, "agentId": "a.example", "member": "member-example", "binding": None,
         "operation": None, "cell": {"cellId": "c.example", "revision": 1, "intentMatches": None},
         "cleanupPending": False, "runtime": "not-verified"}
RECEIPT = {"operationId": "configure-example", "phase": "finalized", "requestDigest": _DIGEST,
           "expectedBindingRevision": 0, "resultBindingRevision": 1, "coordinationPhase": "applied",
           "expectedCellRevision": 1, "observedCellRevision": 2}
SAVED = {**EMPTY, "binding": {"bindingRef": "mb.example", "bindingRevision": 1,
         "configuration": {**CONFIG, "intentDigest": _DIGEST}}, "operation": RECEIPT,
         "cell": {"cellId": "c.example", "revision": 2, "intentMatches": True}}
CANCELLED = {**EMPTY, "operation": {**RECEIPT, "phase": "cancelled",
             "resultBindingRevision": 0, "coordinationPhase": "compensated", "observedCellRevision": 3}}
GUI_OPERATION = "00000000-0000-4000-8000-000000000001"
GUI_CONFIG = {**CONFIG, "connectionRef": "0123456789abcdef0123456789abcdef"}
GUI_SAVED = {**SAVED, "member": "agent:acme:node-1:worker", "binding": {**SAVED["binding"], "configuration": {
    **GUI_CONFIG, "intentDigest": "b" * 64}}, "operation": {**RECEIPT, "operationId": GUI_OPERATION}}
GUI_PENDING = {**EMPTY, "member": "agent:acme:node-1:worker", "operation": {**RECEIPT, "operationId": GUI_OPERATION,
    "phase": "prepared", "coordinationPhase": "planned", "resultBindingRevision": None,
    "observedCellRevision": None}}
GUI_CANCELLED = {**CANCELLED, "member": "agent:acme:node-1:worker", "operation": {**CANCELLED["operation"], "operationId": GUI_OPERATION}}


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
        ) + ((MethodExample(name="manual-connection", kind="success",
                            description="Manual model and a versioned saved connection, without activation.",
                            params=params, result=GUI_SAVED),
              MethodExample(name="pending-connection", kind="success", description="Durable configuration awaits reconciliation.",
                            params=params, result=GUI_PENDING),
              MethodExample(name="bound", kind="success", description="Previously saved configuration.",
                            params=params, result=SAVED),) if empty else ()),
    )


EXAMPLES = (
    examples("agent.model.configure", CONFIGURE, SAVED),
    examples("agent.model.status", {"name": "worker"}, EMPTY, empty=True),
    MethodExampleSet(method="agent.model.cancel", empty_not_applicable="Cancellation returns its durable receipt.",
        cases=examples("agent.model.cancel", CANCEL, CANCELLED).cases + (
            MethodExample(name="cancel-manual-connection", kind="success", description="Confirmed compensation of a prepared operation.",
                          params={**CANCEL, "operationId": GUI_OPERATION}, result=GUI_CANCELLED),)),
)
