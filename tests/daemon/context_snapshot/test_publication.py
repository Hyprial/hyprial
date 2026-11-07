from __future__ import annotations

import json
import stat
import threading
from pathlib import Path

import pytest

from hyprial.daemon import ContextSnapshotExporter, ContextSnapshotWorker

from .support import agent_home, request, write_recaps


def test_publication_is_private_atomic_and_runs_on_the_io_worker(tmp_path: Path) -> None:
    with agent_home(tmp_path) as (registry, agent, home):
        write_recaps(
            home,
            {
                "schemaVersion": 2,
                "deliveryId": "delivery-2",
                "entityToken": agent.entity_token,
            },
        )
        observed: list[str] = []

        def before_publish(_stage: Path, _target: Path) -> None:
            observed.append(threading.current_thread().name)

        exporter = ContextSnapshotExporter(registry, before_publish=before_publish)
        with ContextSnapshotWorker(exporter) as worker:
            result = worker.submit(request(agent)).result(timeout=3)

        assert observed and observed[0].startswith("hyprial-context-snapshot-io")
        assert result.path == home / "state" / "context" / "handoff" / "switch-1"
        assert stat.S_IMODE(result.path.stat().st_mode) == 0o700
        assert stat.S_IMODE((result.path / "manifest.json").stat().st_mode) == 0o600
        assert stat.S_IMODE((result.path / "handoff.md").stat().st_mode) == 0o600


def test_failure_before_rename_exposes_no_partial_handoff(tmp_path: Path) -> None:
    with agent_home(tmp_path) as (registry, agent, home):
        def crash(_stage: Path, _target: Path) -> None:
            raise RuntimeError("fault-before-rename")

        exporter = ContextSnapshotExporter(registry, before_publish=crash)
        with pytest.raises(RuntimeError, match="fault-before-rename"):
            exporter.export(request(agent))

        parent = home / "state" / "context" / "handoff"
        assert not (parent / "switch-1").exists()
        assert list(parent.glob(".*.tmp")) == []


def test_manifest_and_body_never_contain_environment_or_hidden_reasoning(
    tmp_path: Path,
) -> None:
    with agent_home(tmp_path) as (registry, agent, home):
        path = home / "state" / "recaps.jsonl"
        path.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "deliveryId": "delivery-2",
                    "entityToken": agent.entity_token,
                    "environment": {"OPENAI_API_KEY": "secret-value"},
                    "hiddenReasoning": "private chain",
                    "outputExcerpt": "Authorization: Bearer token-value",
                }
            )
            + "\n"
        )
        result = ContextSnapshotExporter(registry).export(request(agent))
        published = (result.path / "manifest.json").read_text() + (
            result.path / "handoff.md"
        ).read_text()
        assert "secret-value" not in published
        assert "private chain" not in published
        assert "token-value" not in published
