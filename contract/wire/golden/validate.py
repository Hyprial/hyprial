#!/usr/bin/env python3
"""Structural validator for the language-neutral wire golden corpus."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parent
REQUIRED_ENVELOPE_FIELDS = {
    "version", "messageId", "conversationId", "intent", "from", "to",
    "type", "content", "resources", "timestamp", "trace",
}
#: Fixtures that exist to prove the summary rule *fails*: they are exempt from
#: the "agent-origin request/reply/event carries a summary" check below.
NEGATIVE_SUMMARY_FIXTURES = frozenset({"request-no-summary", "request-summary-513"})
SUMMARY_MAX_BYTES = 512
REQUIRED_COVERAGE = {
    "send", "reply", "ack", "dedup", "offline", "R10",
    "history-quote-21", "fallback-route-32", "error",
}


def fail(message: str) -> None:
    raise AssertionError(message)


def load(name: str) -> dict[str, Any]:
    value = json.loads((ROOT / name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        fail(f"{name}: top-level value must be an object")
    return value


def main() -> int:
    envelope_document = load("envelopes.json")
    expectation_document = load("expectations.json")
    envelopes = envelope_document.get("envelopes")
    cases = expectation_document.get("cases")
    if not isinstance(envelopes, list) or len(envelopes) < 20:
        fail("envelopes.json must contain at least 20 envelope fixtures")
    if not isinstance(cases, list) or len(cases) < 20:
        fail("expectations.json must contain at least 20 cases")

    by_id: dict[str, dict[str, Any]] = {}
    for envelope in envelopes:
        if not isinstance(envelope, dict):
            fail("every envelope fixture must be an object")
        fixture_id = envelope.get("fixture_id")
        if not isinstance(fixture_id, str) or not fixture_id:
            fail("every envelope fixture needs a non-empty fixture_id")
        if fixture_id in by_id:
            fail(f"duplicate envelope fixture_id: {fixture_id}")
        missing = REQUIRED_ENVELOPE_FIELDS - envelope.keys()
        if missing:
            fail(f"{fixture_id}: missing envelope fields {sorted(missing)}")
        if envelope["version"] != 1:
            fail(f"{fixture_id}: version must be 1")
        if envelope["intent"] not in {"request", "reply", "event"}:
            fail(f"{fixture_id}: invalid intent")
        if not isinstance(envelope["to"], list) or not envelope["to"]:
            fail(f"{fixture_id}: to must be a non-empty array")
        trace = envelope["trace"]
        if not isinstance(trace, dict) or not isinstance(trace.get("hopCount"), int):
            fail(f"{fixture_id}: trace.hopCount must be an integer")
        sender = envelope["from"].get("actorId") if isinstance(
            envelope["from"], dict) else None
        if (
            isinstance(sender, str)
            and sender.startswith("agent:")
            and envelope["type"] != "control"
            and fixture_id not in NEGATIVE_SUMMARY_FIXTURES
        ):
            summary = envelope.get("summary")
            if not isinstance(summary, str) or not summary:
                fail(f"{fixture_id}: an agent-sent message needs a summary")
            if len(summary.encode("utf-8")) > SUMMARY_MAX_BYTES:
                fail(f"{fixture_id}: summary over {SUMMARY_MAX_BYTES} UTF-8 bytes")
        by_id[fixture_id] = envelope

    case_ids: set[str] = set()
    coverage: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            fail("every expectation case must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id:
            fail("every expectation case needs an id")
        if case_id in case_ids:
            fail(f"duplicate expectation id: {case_id}")
        case_ids.add(case_id)
        if not isinstance(case.get("title"), str) or not case["title"]:
            fail(f"{case_id}: title is required")
        sources = case.get("sources")
        if not isinstance(sources, list) or not sources:
            fail(f"{case_id}: at least one source is required")
        for source in sources:
            if not isinstance(source, str) or not source.strip():
                fail(f"{case_id}: source entries must be non-empty strings")
            if not (source.startswith("../harness-bridge-ts/") or source.startswith("docs/")):
                fail(f"{case_id}: source must point to the TS reference or current design")
        envelope_ids = case.get("envelope_ids")
        if not isinstance(envelope_ids, list) or not envelope_ids:
            fail(f"{case_id}: envelope_ids must be a non-empty array")
        unknown = [fixture_id for fixture_id in envelope_ids if fixture_id not in by_id]
        if unknown:
            fail(f"{case_id}: unknown envelope fixtures {unknown}")
        if not isinstance(case.get("expect"), dict) or not case["expect"]:
            fail(f"{case_id}: declarative expect object is required")
        tags = case.get("coverage")
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            fail(f"{case_id}: coverage must be a string array")
        coverage.update(tags)

    missing_coverage = REQUIRED_COVERAGE - coverage
    if missing_coverage:
        fail(f"missing required coverage tags: {sorted(missing_coverage)}")

    print(
        f"RESULT PASS {len(envelopes)} envelope fixtures, "
        f"{len(cases)} expectation cases, {len(coverage)} coverage tags"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (AssertionError, json.JSONDecodeError) as error:
        print(f"RESULT FAIL {error}")
        sys.exit(1)
