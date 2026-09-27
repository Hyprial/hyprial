"""Durable restart policy and PAC work facts for managed agents."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from hyprial.daemon.desired_state import HarnessLaunchSpec
from hyprial.persistent_config import atomic_json_write


RESTORE_THRESHOLD_MS = 12 * 60 * 60 * 1_000
RESTORE_POLICIES = frozenset({"active", "always", "never"})


class RestorePolicyError(ValueError):
    """The restore policy could not be read without guessing."""


@dataclass(frozen=True, slots=True)
class RestorePolicy:
    threshold_ms: int = RESTORE_THRESHOLD_MS
    agents: tuple[tuple[str, str], ...] = ()

    def policy_for(self, actor: str) -> str:
        return dict(self.agents).get(actor, "active")

    def to_json(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "restoreThresholdMs": self.threshold_ms,
            "agents": dict(self.agents),
        }


class RestorePolicyStore:
    """Atomic policy document; missing is the accepted 12-hour default."""

    def __init__(self, path: Path, *, normalize: Callable[[str], str]) -> None:
        self.path = Path(path)
        self._normalize = normalize
        self._lock = threading.RLock()

    def load(self) -> RestorePolicy:
        with self._lock:
            if not self.path.exists():
                return RestorePolicy()
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RestorePolicyError(
                    f"cannot read agent restore policy {self.path}: {error}"
                ) from error
            if not isinstance(raw, dict) or raw.get("schemaVersion") != 1:
                raise RestorePolicyError("agent restore policy must use schemaVersion 1")
            threshold = raw.get("restoreThresholdMs")
            if (
                not isinstance(threshold, int)
                or isinstance(threshold, bool)
                or threshold < 0
            ):
                raise RestorePolicyError(
                    "agent restore policy restoreThresholdMs must be a non-negative integer"
                )
            agents = raw.get("agents", {})
            if not isinstance(agents, dict):
                raise RestorePolicyError("agent restore policy agents must be an object")
            normalized: dict[str, str] = {}
            for actor, policy in agents.items():
                if not isinstance(actor, str) or policy not in RESTORE_POLICIES:
                    raise RestorePolicyError(
                        "agent restore policy entries must map names to active, always, or never"
                    )
                try:
                    name = self._normalize(actor)
                except (ValueError, RuntimeError) as error:
                    raise RestorePolicyError(
                        f"invalid agent restore policy entry {actor!r}: {error}"
                    ) from error
                normalized[name] = str(policy)
            return RestorePolicy(threshold, tuple(sorted(normalized.items())))

    def set_agent(self, actor: str, policy: str) -> RestorePolicy:
        if policy not in RESTORE_POLICIES:
            raise RestorePolicyError("policy must be active, always, or never")
        name = self._normalize(actor)
        with self._lock:
            current = self.load()
            agents = dict(current.agents)
            if policy == "active":
                agents.pop(name, None)
            else:
                agents[name] = policy
            updated = RestorePolicy(current.threshold_ms, tuple(sorted(agents.items())))
            atomic_json_write(self.path, updated.to_json())
            return updated

    def set_threshold(self, threshold_ms: int) -> RestorePolicy:
        if threshold_ms < 0:
            raise RestorePolicyError("restore threshold must not be negative")
        with self._lock:
            current = self.load()
            updated = RestorePolicy(threshold_ms, current.agents)
            atomic_json_write(self.path, updated.to_json())
            return updated


def desired_generation(spec: HarnessLaunchSpec) -> str:
    """Stable desired-intent generation used by the suppression fence."""

    encoded = json.dumps(
        spec.to_json(), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PacRestoreFacts:
    terminal: bool = False
    requested: bool = False
    remote_return: bool = False

    @property
    def pending_work(self) -> bool:
        return self.requested or self.remote_return


def pac_restore_facts(database: Path, actor: str) -> PacRestoreFacts:
    """Read PAC-owned restore facts without mutating workflow state."""

    path = Path(database)
    if not path.exists():
        return PacRestoreFacts()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.1)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT n.graph_id,n.node_id,w.state "
            "FROM nodes n JOIN workflow_graphs w ON w.graph_id=n.graph_id "
            "WHERE n.kind='actor' AND n.actor_name=?",
            (actor,),
        ).fetchall()
        if not rows:
            return PacRestoreFacts()
        terminal = all(
            str(row["state"]) in {"completed", "failed", "cancelled"}
            for row in rows
        )
        requested = any(
            connection.execute(
                "SELECT 1 FROM workflow_nodes WHERE graph_id=? AND actor_node=? "
                "AND state='requested' LIMIT 1",
                (row["graph_id"], row["node_id"]),
            ).fetchone()
            is not None
            for row in rows
        )
        remote_return = any(
            connection.execute(
                "SELECT 1 FROM remote_workflow_outbox o "
                "JOIN remote_workflow_requests r ON r.request_id=o.request_id "
                "JOIN workflow_nodes wn ON wn.graph_id=r.graph_id AND wn.node_id=r.node_id "
                "WHERE wn.graph_id=? AND wn.actor_node=? LIMIT 1",
                (row["graph_id"], row["node_id"]),
            ).fetchone()
            is not None
            for row in rows
        )
        return PacRestoreFacts(
            terminal=terminal, requested=requested, remote_return=remote_return
        )
    finally:
        connection.close()


__all__ = [
    "PacRestoreFacts",
    "RESTORE_POLICIES",
    "RESTORE_THRESHOLD_MS",
    "RestorePolicy",
    "RestorePolicyError",
    "RestorePolicyStore",
    "desired_generation",
    "pac_restore_facts",
]
