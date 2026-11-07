from __future__ import annotations


def scan_composition(source: str) -> list[str]:
    findings: list[str] = []
    for forbidden in (
        "resource_exists",
        "lifecycle-domain-receipts.sqlite3",
        "execute_agent",
        "execute_session",
        "execute_harness",
        "self.agents.get(",
        ".read_binding(",
        ".read_session(",
        ".read_harnesses(",
    ):
        if forbidden in source:
            findings.append(f"composition pre-reads or executes domain state: {forbidden}")
    return findings


def scan_agent_store(source: str) -> list[str]:
    findings: list[str] = []
    compare_delete = "elif actual_active and (expected is None or token == expected):"
    fresh_token = "if not actual_active:\n                        token = uuid.uuid4().hex"
    if compare_delete not in source:
        findings.append("Agent delete lost expected-resource-token compare fence")
    if fresh_token not in source:
        findings.append("Agent create no longer rotates the prior tombstone token")
    return findings


def scan_harness_custody(source: str) -> list[str]:
    findings: list[str] = []
    required = {
        "if key in self._lifecycle_effect_resources:": (
            "Harness reconcile/restore lost lifecycle custody fence"
        ),
        "if key in self._lifecycle_effect_resources and not allow_lifecycle:": (
            "Harness start lost central lifecycle custody fence"
        ),
        "self._terminalize_incomplete_lifecycle(": (
            "Harness close no longer terminalizes incomplete lifecycle effects"
        ),
        "AdmissionResult.CLOSED\n                if self._closing": (
            "Harness completion admission lost its closing fence"
        ),
        "with self._generation_lock:\n            closing = self._closing": (
            "Harness public admission lost close linearization"
        ),
        "if len(self._settled_lifecycle_order) >= _LIFECYCLE_SETTLED_CAPACITY:": (
            "Harness settled lifecycle dedupe lost its capacity bound"
        ),
        "expired = self._settled_lifecycle_order.popleft()": (
            "Harness settled lifecycle dedupe lost ordered eviction"
        ),
        "if durable_receipt is not None and durable_receipt.completed:": (
            "Harness terminal receipt replay was hidden by memory dedupe"
        ),
        "and durable_receipt is None": (
            "Harness settled cache no longer requires confirmed receipt absence"
        ),
        "self._replay_lifecycle_completion(command, durable_receipt)": (
            "Harness terminal receipt no longer replays its completion"
        ),
        "if len(self._lifecycle_replay_claims) >= self._lifecycle_replay_capacity:": (
            "Harness active replay claims lost capacity backpressure"
        ),
        'return "full"': "Harness active replay capacity no longer rejects admission",
    }
    for fragment, detail in required.items():
        if fragment not in source:
            findings.append(detail)
    return findings
