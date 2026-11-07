from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from hyprial.daemon import (
    ContextSnapshotBody,
    ContextSnapshotRequest,
    ContextSnapshotSource,
    ContextSnapshotTarget,
    RecapCaptureState,
)
from hyprial.identity import AgentRegistry


@contextmanager
def agent_home(tmp_path: Path) -> Iterator[tuple[AgentRegistry, object, Path]]:
    hyprial_home = tmp_path / "hyprial-home"
    hyprial_home.mkdir(mode=0o700)
    registry = AgentRegistry(
        tmp_path / "state" / "agents.sqlite3",
        owner="owner@example.com",
        machine="node",
        hyprial_home=hyprial_home,
    )
    try:
        agent = registry.create("worker", cwd=str(tmp_path / "work"))
        home = Path(registry.home_receipt(agent.actor).path)
        yield registry, agent, home
    finally:
        registry.close()


def request(
    agent: object,
    *,
    harness: str = "claude",
    session_ref: str = "session-1",
    cutoff: str = "delivery-2",
    recap_enabled: bool = True,
    recap_dropped: int = 0,
    read_limit: int = 4096,
    token_limit: int = 512,
) -> ContextSnapshotRequest:
    return ContextSnapshotRequest(
        switch_id="switch-1",
        actor=str(getattr(agent, "uri")),
        expected_entity_token=str(getattr(agent, "entity_token")),
        source=ContextSnapshotSource(
            harness=harness,
            generation="generation-7",
            session_ref=session_ref,
            cwd=str(getattr(agent, "cwd")),
        ),
        target=ContextSnapshotTarget(
            harness="pi", provider="openai-codex", model="gpt-test"
        ),
        last_settled_delivery_id=cutoff,
        cutoff_at_ms=1_791_000_000_000,
        recap=RecapCaptureState(
            enabled=recap_enabled, dropped_records=recap_dropped
        ),
        body=ContextSnapshotBody(
            objective="continue the bounded switch",
            verified_facts=("fact-one",),
            model_inferences=("inference-one",),
            incomplete=("pending-one",),
            next_steps=("next-one",),
            files_and_cwd=("src/example.py", "cwd=<path>"),
            pending_handles=("delivery:pending-1", "pac:graph-1"),
            unknown_or_irreversible=("unknown-one",),
        ),
        session_read_limit_bytes=read_limit,
        injection_token_limit=token_limit,
    )


def write_recaps(home: Path, *records: object, torn: bytes = b"") -> Path:
    path = home / "state" / "recaps.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(
        json.dumps(record, separators=(",", ":")).encode() + b"\n"
        for record in records
    )
    path.write_bytes(payload + torn)
    return path


def native_session_path(home: Path, harness: str, cwd: str, session_ref: str) -> Path:
    if harness == "claude":
        from hyprial.daemon.impl.transfer.archive.session_files import (
            claude_project_dir_name,
        )

        return (
            home
            / "state"
            / "sessions"
            / "claude"
            / claude_project_dir_name(cwd)
            / f"{session_ref}.jsonl"
        )
    if harness == "pi":
        return home / "state" / "pi" / f"2026-10-09T00-00-00Z_{session_ref}.jsonl"
    if harness == "codex":
        return (
            home
            / "state"
            / "sessions"
            / "codex"
            / "2026"
            / "10"
            / "09"
            / f"rollout-2026-10-09T00-00-00-{session_ref}.jsonl"
        )
    raise AssertionError(harness)
