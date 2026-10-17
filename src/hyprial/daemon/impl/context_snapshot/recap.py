"""Conservative coverage analysis for the existing state/recaps.jsonl."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hyprial.kernel import redact, redact_turn_excerpt

from hyprial.daemon.impl.context_snapshot.contract import (
    ContextSnapshotRequest,
    SnapshotGap,
    SnapshotSourceEvidence,
    SourceAnalysis,
)
from hyprial.daemon.impl.context_snapshot.io import (
    read_bounded_lines,
    relative_ref,
)

_SAFE_RECAP_FIELDS = (
    "deliveryId",
    "status",
    "startedAtMs",
    "endedAtMs",
    "toolNames",
    "outputExcerpt",
)


def _safe_excerpt(record: dict[str, Any]) -> str:
    selected = {key: record[key] for key in _SAFE_RECAP_FIELDS if key in record}
    safe = redact(selected)
    return redact_turn_excerpt(
        json.dumps(safe, ensure_ascii=False, sort_keys=True),
        enforce_output_bound=False,
    )


def analyze_recaps(home: Path, request: ContextSnapshotRequest) -> SourceAnalysis:
    if not request.recap.enabled:
        return SourceAnalysis(
            coverage="unavailable",
            gaps=(SnapshotGap("recap-disabled", "recap capture was not enabled"),),
        )

    path = home / "state" / "recaps.jsonl"
    try:
        bounded = read_bounded_lines(home, path, request.session_read_limit_bytes)
    except FileNotFoundError:
        return SourceAnalysis(
            coverage="unavailable",
            gaps=(SnapshotGap("recap-empty", "no recap records exist"),),
        )

    ref = relative_ref(home, path)
    source = SnapshotSourceEvidence(
        kind="recap",
        ref=ref,
        digest=bounded.digest,
        captured_bytes=bounded.captured_bytes,
        source_bytes=bounded.source_bytes,
    )
    gaps: list[SnapshotGap] = []
    if bounded.byte_truncated:
        gaps.append(
            SnapshotGap(
                "recap-read-limit",
                f"recap prefix before byte {bounded.start_offset} was not read",
                ref,
            )
        )
    if bounded.torn_final_line or bounded.invalid_utf8:
        gaps.append(
            SnapshotGap(
                "recap-torn-record",
                "an incomplete or invalid UTF-8 recap record was excluded",
                ref,
            )
        )

    records: list[dict[str, Any]] = []
    for line in bounded.lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            gaps.append(
                SnapshotGap(
                    "recap-torn-record",
                    "an invalid JSON recap record was excluded",
                    ref,
                )
            )
            continue
        if not isinstance(value, dict):
            gaps.append(
                SnapshotGap("recap-invalid-record", "a non-object recap was excluded", ref)
            )
            continue
        records.append(value)

    if not records:
        gaps.append(SnapshotGap("recap-empty", "no valid recap records exist", ref))
        return SourceAnalysis(
            coverage="unavailable",
            sources=(source,),
            gaps=tuple(gaps),
        )
    if request.recap.dropped_records:
        gaps.append(
            SnapshotGap(
                "recap-write-dropped",
                f"the bounded writer dropped {request.recap.dropped_records} record(s)",
                ref,
            )
        )

    ownership_missing = False
    ownership_mismatch = False
    for record in records:
        entity = record.get("entityToken")
        if not isinstance(entity, str) or not entity:
            ownership_missing = True
        elif entity != request.expected_entity_token:
            ownership_mismatch = True
    if ownership_missing:
        gaps.append(
            SnapshotGap(
                "recap-ownership-unverifiable",
                "recap schema has no entity token, so current-entity ownership is unprovable",
                ref,
            )
        )
    if ownership_mismatch:
        gaps.append(
            SnapshotGap(
                "recap-entity-mismatch",
                "one or more records belong to another agent incarnation",
                ref,
            )
        )

    if records[-1].get("deliveryId") != request.last_settled_delivery_id:
        gaps.append(
            SnapshotGap(
                "recap-stale-cutoff",
                "the newest recap does not cover the last settled delivery",
                ref,
            )
        )
    coverage = "complete-within-cutoff" if not gaps else "partial"
    return SourceAnalysis(
        coverage=coverage,
        sources=(source,),
        gaps=tuple(gaps),
        excerpts=tuple(_safe_excerpt(record) for record in records[-20:]),
    )


__all__ = ["analyze_recaps"]
