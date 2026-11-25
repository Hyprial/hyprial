"""Reconcile one E2E-024 send without repeating a write or resetting its budget.

Only SUBMIT_OUTCOME_UNKNOWN permits read-only lookup of the frozen command.
Durable acceptance must match the original command, conversation and receipt;
delivery and acknowledgement remain separate assertions in the runner.
"""
from __future__ import annotations

import time
from uuid import uuid4

from hyprial.kernel import RequestPortError


SUBMIT_BUDGET_SECONDS = 4.0


class ScenarioRequestError(AssertionError):
    def __init__(self, method: str, error: RequestPortError):
        super().__init__(f"{method} failed with {error.code}: {error}")
        self.code = error.code
        self.data = error.data


class SubmissionFailure(AssertionError):
    def __init__(self, message: str, evidence: dict):
        super().__init__(message)
        self.evidence = dict(evidence)


def submit_once(daemon, *, request, sender: str, target: str, text: str, clock=time) -> tuple[dict, dict]:
    started = clock.monotonic()
    deadline = started + SUBMIT_BUDGET_SECONDS
    key = uuid4().hex
    conversation = f"e2e-024-{key}"
    evidence = {
        "idempotencyKey": key, "conversationId": conversation, "sender": sender, "target": target,
        "submitBudgetSeconds": SUBMIT_BUDGET_SECONDS, "lookupAttempts": 0,
        "phase": "send", "recoveredByLookup": False,
    }

    def fail(message: str):
        evidence["failedAt"] = evidence["phase"]
        evidence.update(phase="failed", elapsedSeconds=round(clock.monotonic() - started, 6))
        raise SubmissionFailure(message, evidence)

    def remaining() -> float:
        value = deadline - clock.monotonic()
        if value <= 0:
            fail("original 4s submission deadline expired without a confirmed outcome")
        return value

    params = {"from": sender, "to": [target], "message": text, "summary": text,
              "conversationId": conversation, "idempotencyKey": key}
    try:
        result = request(daemon, "message.send", params, timeout=remaining())
    except ScenarioRequestError as error:
        evidence["firstRequestSeconds"] = round(clock.monotonic() - started, 6)
        evidence["requestError"] = {"code": error.code, "message": str(error), "data": error.data}
        if error.code != "SUBMIT_OUTCOME_UNKNOWN":
            fail(str(error))
        receipt = error.data if isinstance(error.data, dict) else {}
        message_id = receipt.get("messageId")
        if not isinstance(message_id, str) or not message_id:
            fail("ambiguous submission has no original message receipt identity")
        if receipt.get("target", target) != target:
            fail("ambiguous submission names a foreign target")
        recorded_key = receipt.get("idempotencyKey", key)
        if recorded_key != key and recorded_key != f"{key}:{target}:0":
            fail("ambiguous submission names a foreign command")
        if receipt.get("commandAdmission", "accepted") != "accepted" or receipt.get("outcomeKnown", False) is not False:
            fail("ambiguous submission has contradictory admission/outcome evidence")
        evidence.update(phase="lookup", messageId=message_id)
        lookup = {"from": sender, "target": target, "text": text,
                  "conversationId": conversation, "idempotencyKey": key}
        while True:
            patience = remaining()
            evidence["lookupAttempts"] += 1
            try:
                found = request(daemon, "message.send.lookup", lookup, timeout=patience)
            except ScenarioRequestError as lookup_error:
                evidence["lookupError"] = {"code": lookup_error.code, "data": lookup_error.data}
                fail(f"read-only lookup failed: {lookup_error}")
            except (AssertionError, OSError) as lookup_error:
                fail(f"read-only lookup failed: {lookup_error}")
            evidence["lastLookup"] = found
            remaining()
            if found.get("status") == "accepted":
                if (found.get("commandId") != key or found.get("conversationId") != conversation
                        or found.get("messageId") != message_id):
                    fail("read-only lookup did not confirm the original command/conversation/message identity")
                evidence["recoveredByLookup"] = True
                result = found
                break
            if (found.get("status") != "unknown" or not isinstance(found.get("reason"), str)
                    or found["reason"] not in {"not-found", "unavailable"}):
                fail("read-only lookup returned a terminal or invalid result")
            clock.sleep(min(0.05, remaining()))
    except (AssertionError, OSError) as error:
        fail(str(error))
    else:
        evidence["firstRequestSeconds"] = round(clock.monotonic() - started, 6)
        remaining()
        if (result.get("ok") is not True or result.get("operationId") != key
                or result.get("conversationId") != conversation):
            fail("send did not confirm the original command/conversation identity")
    message_id = result.get("messageId")
    if not isinstance(message_id, str) or not message_id:
        fail("confirmed submission has no original message identity")
    remaining()
    evidence.update(phase="confirmed", messageId=message_id,
                    elapsedSeconds=round(clock.monotonic() - started, 6))
    return result, evidence
