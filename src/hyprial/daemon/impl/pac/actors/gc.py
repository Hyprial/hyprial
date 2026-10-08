"""Bounded removal of reclaimed workflow-owned worker agents."""

from __future__ import annotations

from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
import random
import threading
import time
from typing import Any, Callable, ContextManager

from hyprial.kernel import (
    PAC_GC_INTERVAL_SECONDS,
    PAC_GC_JITTER_RATIO,
    PAC_GC_MAX_REMOVALS_PER_PASS,
)
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.kernel import parse_agent_uri


@dataclass(frozen=True, slots=True)
class _Candidate:
    graph_id: str
    actor_uri: str
    actor_name: str
    entity_token: str

    def to_json(self) -> dict[str, str]:
        return {
            "graphId": self.graph_id,
            "actor": self.actor_uri,
            "agent": self.actor_name,
        }


@dataclass(frozen=True, slots=True)
class _Selection:
    candidates: tuple[_Candidate, ...]
    skipped: dict[str, int]
    errors: tuple[dict[str, str], ...]


class PacGc:
    """Own one off-queue scan thread and the exact read-only preview selector."""

    def __init__(
        self,
        *,
        state_dir: Path,
        application: Any,
        runtime: Any,
        logger: Callable[..., None],
        interval_seconds: float = PAC_GC_INTERVAL_SECONDS,
        jitter_ratio: float = PAC_GC_JITTER_RATIO,
        max_removals: int = PAC_GC_MAX_REMOVALS_PER_PASS,
        start_thread: bool = True,
        jitter: Callable[[], float] = random.random,
        clock_ms: Callable[[], int] = lambda: time.time_ns() // 1_000_000,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("PAC GC interval must be positive")
        if not 0 <= jitter_ratio <= 1:
            raise ValueError("PAC GC jitter ratio must be between zero and one")
        if max_removals <= 0:
            raise ValueError("PAC GC removal bound must be positive")
        self.database = default_database_path(state_dir)
        self.application = application
        self.runtime = runtime
        self.logger = logger
        self.interval_seconds = interval_seconds
        self.jitter_ratio = jitter_ratio
        self.max_removals = max_removals
        self.jitter = jitter
        self.clock_ms = clock_ms
        self.monotonic = monotonic
        self._stop = threading.Event()
        self._pass_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._last_pass_at_ms: int | None = None
        self._last_removed_count = 0
        self._last_backlog: int | None = None
        self.thread = threading.Thread(
            target=self._run,
            name="hyprial-pac-gc",
            daemon=True,
        )
        if start_thread:
            self.thread.start()

    def _delay(self) -> float:
        sample = min(1.0, max(0.0, float(self.jitter())))
        offset = self.interval_seconds * self.jitter_ratio * ((2 * sample) - 1)
        return max(0.001, self.interval_seconds + offset)

    def _run(self) -> None:
        while not self._stop.wait(self._delay()):
            try:
                self.run_pass()
            except Exception as error:  # noqa: BLE001 - the next pass must survive
                self.logger(
                    "error",
                    "pac",
                    "pac.gc.service_failed",
                    errorType=type(error).__name__,
                    detail=str(error)[:500],
                )

    def _observation_scope(self) -> ContextManager[Any]:
        current = getattr(self.application, "_current_worker_snapshot", None)
        if callable(current) and current() is not None:
            return nullcontext()
        snapshot = getattr(self.application, "_worker_status_snapshot", None)
        return snapshot() if callable(snapshot) else nullcontext()

    @staticmethod
    def _skip(skipped: Counter[str], reason: str) -> None:
        skipped[reason] += 1

    @staticmethod
    def _workflow_owned(
        roster_node: dict[str, Any],
        receipt: dict[str, Any] | None,
        *,
        actor_uri: str,
        actor_name: str,
    ) -> bool:
        """Require both canonical roster ownership and its immutable receipt."""

        return (
            roster_node.get("ownership") == "workflow"
            and receipt is not None
            and receipt["ownership"] == "workflow"
            and receipt["actor_uri"] == actor_uri
            and receipt["actor_name"] == actor_name
        )

    def _select(self) -> _Selection:
        if not self.database.exists():
            return _Selection((), {}, ())
        skipped: Counter[str] = Counter()
        candidates: list[_Candidate] = []
        errors: list[dict[str, str]] = []
        store = PacGraphStore(self.database, read_only=True)
        try:
            graphs = list(
                store._db.execute(
                    "SELECT g.graph_id,g.closed_at,w.roster_json "
                    "FROM graphs g JOIN workflow_graphs w USING(graph_id) "
                    "ORDER BY g.graph_id"
                )
            )
            with self._observation_scope():
                for graph in graphs:
                    graph_id = str(graph["graph_id"])
                    try:
                        roster = store.workflow_roster(graph_id)
                        if roster is None:
                            self._skip(skipped, "ownership-unknown")
                            continue
                        seen: set[str] = set()
                        for raw in roster.get("nodes", []):
                            if not isinstance(raw, dict):
                                continue
                            actor_uri = raw.get("actorUri") or raw.get("owner")
                            parsed = (
                                parse_agent_uri(actor_uri)
                                if isinstance(actor_uri, str)
                                else None
                            )
                            if parsed is None or actor_uri in seen:
                                continue
                            seen.add(actor_uri)
                            actor_name = parsed[2]
                            actor_node = raw.get("actorNode")
                            receipt = (
                                store.workflow_worker_receipt(graph_id, actor_node)
                                if isinstance(actor_node, str)
                                else None
                            )
                            owned = self._workflow_owned(
                                raw,
                                receipt,
                                actor_uri=actor_uri,
                                actor_name=actor_name,
                            )
                            if not owned:
                                self._skip(skipped, "not-workflow-owned")
                                continue
                            if graph["closed_at"] is None:
                                self._skip(skipped, "graph-open")
                                continue
                            assert isinstance(actor_node, str)
                            assert receipt is not None
                            intent = store.workflow_worker_cleanup_intent(
                                graph_id, actor_node
                            )
                            activation = store._db.execute(
                                "SELECT 1 FROM actor_activations "
                                "WHERE graph_id=? AND node_id=?",
                                (graph_id, actor_node),
                            ).fetchone()
                            never_activated = (
                                receipt["agent_entity_token"] is None
                                and activation is None
                            )
                            cleanup_state = (
                                None if intent is None else str(intent["state"])
                            )
                            if cleanup_state != "complete" and not never_activated:
                                self._skip(
                                    skipped,
                                    (
                                        f"cleanup-{cleanup_state}"
                                        if cleanup_state is not None
                                        else "cleanup-missing"
                                    ),
                                )
                                continue

                            agent = self.application.agents.get(actor_uri)
                            if agent is None:
                                self._skip(skipped, "agent-missing")
                                continue
                            expected_token = receipt["agent_entity_token"]
                            if expected_token is None:
                                # A planned roster name is not proof that the
                                # current entity was created by this workflow.
                                self._skip(skipped, "ownership-incarnation-unknown")
                                continue
                            if agent.entity_token != expected_token:
                                self._skip(skipped, "agent-incarnation-mismatch")
                                continue
                            if agent.pinned_adapters:
                                self._skip(skipped, "pinned")
                                continue
                            if self.runtime.observe(actor_name).present:
                                self._skip(skipped, "online")
                                continue
                            candidates.append(
                                _Candidate(graph_id, actor_uri, actor_name, agent.entity_token)
                            )
                    except Exception as error:  # noqa: BLE001 - isolate one graph
                        errors.append(
                            {
                                "graphId": graph_id,
                                "errorType": type(error).__name__,
                                "detail": str(error)[:500],
                            }
                        )
        finally:
            store.close()
        return _Selection(
            tuple(candidates),
            dict(sorted(skipped.items())),
            tuple(errors),
        )

    def preview(self) -> dict[str, Any]:
        """Return the next bounded pass from the production selector, without writes."""

        selection = self._select()
        selected = selection.candidates[: self.max_removals]
        skipped = Counter(selection.skipped)
        if len(selection.candidates) > len(selected):
            skipped["batch-limit"] += len(selection.candidates) - len(selected)
        return {
            "ok": True,
            "dryRun": True,
            "selected": [item.to_json() for item in selected],
            "selectedCount": len(selected),
            "removableBacklog": len(selection.candidates),
            "skipped": dict(sorted(skipped.items())),
            "errors": list(selection.errors),
        }

    def run_pass(self) -> dict[str, Any]:
        """Remove at most the registered batch bound and log one summary."""

        with self._pass_lock:
            started = self.monotonic()
            try:
                selection = self._select()
            except Exception as error:  # noqa: BLE001 - every pass still reports
                selection = _Selection(
                    (),
                    {},
                    (
                        {
                            "graphId": "(scan)",
                            "errorType": type(error).__name__,
                            "detail": str(error)[:500],
                        },
                    ),
                )
            selected = selection.candidates[: self.max_removals]
            skipped = Counter(selection.skipped)
            if len(selection.candidates) > len(selected):
                skipped["batch-limit"] += len(selection.candidates) - len(selected)
            errors = list(selection.errors)
            removed = 0
            for candidate in selected:
                try:
                    result = self.application._destroy_agent(
                        candidate.actor_uri,
                        expected_entity_token=candidate.entity_token,
                        require_unpinned=True,
                        require_offline=True,
                    )
                    if result.get("destroyed") is True:
                        removed += 1
                    else:
                        skipped["destroy-noop"] += 1
                except Exception as error:  # noqa: BLE001 - isolate one removal
                    errors.append(
                        {
                            **candidate.to_json(),
                            "errorType": type(error).__name__,
                            "detail": str(error)[:500],
                        }
                    )
            at_ms = self.clock_ms()
            duration_ms = max(0, int((self.monotonic() - started) * 1000))
            report = {
                "selected": len(selected),
                "removed": removed,
                "removableBacklog": len(selection.candidates),
                "skipped": dict(sorted(skipped.items())),
                "errors": len(errors),
                "errorDetails": errors,
                "durationMs": duration_ms,
                "atMs": at_ms,
            }
            with self._status_lock:
                self._last_pass_at_ms = at_ms
                self._last_removed_count = removed
                self._last_backlog = len(selection.candidates) - removed
            self.logger(
                "info",
                "pac",
                "pac.gc.pass",
                selected=report["selected"],
                removed=removed,
                skipped=report["skipped"],
                errors=report["errors"],
                durationMs=duration_ms,
            )
            return report

    def status(self) -> dict[str, int | None]:
        """Report the last pass only; daemon status must not run a scan.

        Status is polled by monitors, so a full graph scan plus a worker
        snapshot here would put GC cost on every poll.  The backlog is the
        one the last pass left behind; ``preview`` computes a fresh one.
        """

        with self._status_lock:
            return {
                "lastPassAtMs": self._last_pass_at_ms,
                "lastRemovedCount": self._last_removed_count,
                "removableBacklog": self._last_backlog,
            }

    def close(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        if self.thread.ident is not None:
            self.thread.join(timeout)
        return not self.thread.is_alive()
