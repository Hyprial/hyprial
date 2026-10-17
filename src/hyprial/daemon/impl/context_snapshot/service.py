"""Receipt-fenced context snapshot service and its dedicated I/O worker."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self

from hyprial.kernel import redact, redact_turn_excerpt

from hyprial.daemon.impl.context_snapshot.contract import (
    HANDOFF_SCHEMA_VERSION,
    ContextSnapshotRequest,
    ContextSnapshotResult,
    Coverage,
    SnapshotGap,
)
from hyprial.daemon.impl.context_snapshot.io import publish_handoff
from hyprial.daemon.impl.context_snapshot.native import analyze_native_session
from hyprial.daemon.impl.context_snapshot.recap import analyze_recaps


class SnapshotRegistry(Protocol):
    def home_receipt(
        self,
        actor: str,
        *,
        require_ready: bool = True,
        validate_mirror: bool = True,
        expected_entity_token: str | None = None,
    ) -> object: ...

    def confirm_home_authority(self, receipt: object) -> None: ...


def _safe(value: str) -> str:
    structured = redact(value)
    return redact_turn_excerpt(str(structured), enforce_output_bound=False)


def _section(title: str, values: tuple[str, ...]) -> list[str]:
    rendered = [f"## {title}", ""]
    rendered.extend(f"- {_safe(value)}" for value in values)
    if not values:
        rendered.append("- （无已知项）")
    rendered.append("")
    return rendered


def _render_body(
    request: ContextSnapshotRequest,
    recap_excerpts: tuple[str, ...],
    native_excerpts: tuple[str, ...],
) -> str:
    body = request.body
    lines = ["# Switch handoff", "", "## 任务目标", "", _safe(body.objective), ""]
    lines.extend(_section("已核事实与证据", body.verified_facts))
    lines.extend(["### 模型推断（非事实）", ""])
    lines.extend(f"- {_safe(value)}" for value in body.model_inferences)
    if not body.model_inferences:
        lines.append("- （无模型推断）")
    lines.append("")
    lines.extend(_section("未完成与阻塞", body.incomplete))
    lines.extend(_section("下一步", body.next_steps))
    lines.extend(_section("文件与 cwd", body.files_and_cwd))
    lines.extend(_section("pending delivery/PAC/job 句柄", body.pending_handles))
    lines.extend(_section("未知与不可重做副作用", body.unknown_or_irreversible))
    lines.extend(["## 有界源摘录", ""])
    if recap_excerpts:
        lines.extend(["### recap（已核记录字段）", ""])
        lines.extend(f"- {value}" for value in recap_excerpts)
        lines.append("")
    if native_excerpts:
        lines.extend(["### native session（只读、有界、已脱敏）", "", "```text"])
        lines.extend(native_excerpts)
        lines.extend(["```", ""])
    if not recap_excerpts and not native_excerpts:
        lines.extend(["- 无可用源摘录；见 manifest gaps。", ""])
    return "\n".join(lines)


class ContextSnapshotExporter:
    """Read sources and atomically publish one immutable handoff directory.

    The low-cost phase-1 path is intentional: H2 remains at
    ``state/recaps.jsonl`` and handoffs live below
    ``state/context/handoff/<switch-id>``. Moving them to top-level
    ``context/`` is deferred to the complete home-directory rearrangement PR;
    this keeps the existing transfer/cleanup top-level closure unchanged.
    """

    def __init__(
        self,
        registry: SnapshotRegistry,
        *,
        before_publish: Callable[[Path, Path], None] | None = None,
    ) -> None:
        self._registry = registry
        self._before_publish = before_publish

    def export(self, request: ContextSnapshotRequest) -> ContextSnapshotResult:
        receipt = self._registry.home_receipt(
            request.actor,
            expected_entity_token=request.expected_entity_token,
            require_ready=True,
            validate_mirror=True,
        )
        home = Path(str(getattr(receipt, "path")))
        recap = analyze_recaps(home, request)
        native = (
            analyze_native_session(home, request)
            if recap.coverage != "complete-within-cutoff"
            else None
        )
        sources = list(recap.sources)
        gaps = list(recap.gaps)
        native_excerpts: tuple[str, ...] = ()
        if native is not None:
            sources.extend(native.sources)
            gaps.extend(native.gaps)
            native_excerpts = native.excerpts
        coverage: Coverage
        if recap.coverage == "complete-within-cutoff":
            coverage = "complete-within-cutoff"
        elif recap.coverage == "partial" or (native and native.coverage == "partial"):
            coverage = "partial"
        else:
            coverage = "unavailable"

        manifest: dict[str, Any] = {
            "schemaVersion": HANDOFF_SCHEMA_VERSION,
            "switchId": request.switch_id,
            "actor": request.actor,
            "entityToken": request.expected_entity_token,
            "source": {
                "harness": request.source.harness,
                "sessionRef": request.source.session_ref,
                "generation": request.source.generation,
            },
            "target": {
                "harness": request.target.harness,
                "provider": request.target.provider,
                "model": request.target.model,
            },
            "cutoff": {
                "lastSettledDeliveryId": request.last_settled_delivery_id,
                "atMs": request.cutoff_at_ms,
            },
            "coverage": coverage,
            "coverageNote": (
                "Evidence coverage only; not a promise of lossless model memory."
            ),
            "limits": {
                "sessionReadBytes": request.session_read_limit_bytes,
                "injectionTokens": request.injection_token_limit,
            },
            "sources": [source.as_json() for source in sources],
            "gaps": [gap.as_json() for gap in _deduplicate_gaps(gaps)],
            "body": "handoff.md",
        }
        body = _render_body(request, recap.excerpts, native_excerpts)
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        body_bytes = body.encode("utf-8")
        # Recheck the registry-backed resource token immediately before the
        # atomic publish so a same-name replacement cannot inherit old bytes.
        self._registry.confirm_home_authority(receipt)
        path = publish_handoff(
            home,
            request.switch_id,
            manifest=manifest_bytes,
            body=body_bytes,
            before_publish=self._before_publish,
        )
        return ContextSnapshotResult(
            path=path,
            coverage=coverage,
            manifest_digest=hashlib.sha256(manifest_bytes).hexdigest(),
        )


def _deduplicate_gaps(gaps: list[SnapshotGap]) -> tuple[SnapshotGap, ...]:
    seen: set[tuple[str, str, str | None]] = set()
    values: list[SnapshotGap] = []
    for gap in gaps:
        key = (gap.code, gap.detail, gap.ref)
        if key not in seen:
            seen.add(key)
            values.append(gap)
    return tuple(values)


class ContextSnapshotWorker:
    """Single-lane I/O effect worker; actor mailboxes only submit requests."""

    def __init__(self, exporter: ContextSnapshotExporter) -> None:
        self._exporter = exporter
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hyprial-context-snapshot-io"
        )

    def submit(self, request: ContextSnapshotRequest) -> Future[ContextSnapshotResult]:
        return self._executor.submit(self._exporter.export, request)

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=False)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


__all__ = ["ContextSnapshotExporter", "ContextSnapshotWorker", "SnapshotRegistry"]
