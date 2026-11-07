from __future__ import annotations

import json
from pathlib import Path

import pytest

from hyprial.daemon import ContextSnapshotError, ContextSnapshotExporter
from hyprial.identity import AgentHomeError

from .support import agent_home, request, write_recaps


def _manifest(path: Path) -> dict[str, object]:
    return json.loads((path / "manifest.json").read_text())


def test_empty_old_and_current_home_are_explicit_and_complete_is_bounded(
    tmp_path: Path,
) -> None:
    with agent_home(tmp_path) as (registry, agent, home):
        exporter = ContextSnapshotExporter(registry)
        empty = exporter.export(request(agent))
        assert _manifest(empty.path)["coverage"] == "unavailable"
        assert {gap["code"] for gap in _manifest(empty.path)["gaps"]} >= {
            "recap-empty",
            "native-session-unavailable",
        }

        old_request = request(agent)
        old_request = old_request.with_switch_id("switch-old")
        write_recaps(
            home,
            {"schemaVersion": 1, "event": "turn", "deliveryId": "delivery-2"},
        )
        old = exporter.export(old_request)
        assert _manifest(old.path)["coverage"] == "partial"
        assert "recap-ownership-unverifiable" in {
            gap["code"] for gap in _manifest(old.path)["gaps"]
        }

        current_request = request(agent).with_switch_id("switch-current")
        write_recaps(
            home,
            {
                "schemaVersion": 2,
                "event": "turn",
                "deliveryId": "delivery-2",
                "entityToken": agent.entity_token,
                "outputExcerpt": "done",
            },
        )
        current = exporter.export(current_request)
        manifest = _manifest(current.path)
        assert manifest["coverage"] == "complete-within-cutoff"
        assert manifest["coverageNote"].startswith(
            "Evidence coverage only; not a promise of lossless model memory"
        )
        assert manifest["body"] == "handoff.md"
        assert manifest["limits"] == {
            "sessionReadBytes": 4096,
            "injectionTokens": 512,
        }
        assert manifest["source"] == {
            "harness": "claude",
            "sessionRef": "session-1",
            "generation": "generation-7",
        }
        assert manifest["target"] == {
            "harness": "pi",
            "provider": "openai-codex",
            "model": "gpt-test",
        }
        body = (current.path / "handoff.md").read_text()
        for heading in (
            "## 任务目标",
            "## 已核事实与证据",
            "### 模型推断（非事实）",
            "## 未完成与阻塞",
            "## 下一步",
            "## 文件与 cwd",
            "## pending delivery/PAC/job 句柄",
            "## 未知与不可重做副作用",
        ):
            assert heading in body


@pytest.mark.parametrize(
    ("case", "records", "torn", "enabled", "dropped", "expected"),
    [
        ("off", (), b"", False, 0, "recap-disabled"),
        ("drop", ({"schemaVersion": 2, "deliveryId": "delivery-2", "entityToken": "TOKEN"},), b"", True, 1, "recap-write-dropped"),
        ("torn", ({"schemaVersion": 2, "deliveryId": "delivery-2", "entityToken": "TOKEN"},), b'{"bad":', True, 0, "recap-torn-record"),
        ("stale", ({"schemaVersion": 2, "deliveryId": "delivery-1", "entityToken": "TOKEN"},), b"", True, 0, "recap-stale-cutoff"),
        ("foreign", ({"schemaVersion": 2, "deliveryId": "delivery-2", "entityToken": "other"},), b"", True, 0, "recap-entity-mismatch"),
    ],
)
def test_recap_failure_states_never_claim_complete(
    tmp_path: Path,
    case: str,
    records: tuple[dict[str, object], ...],
    torn: bytes,
    enabled: bool,
    dropped: int,
    expected: str,
) -> None:
    with agent_home(tmp_path) as (registry, agent, home):
        materialized = tuple(
            {
                **record,
                "entityToken": (
                    agent.entity_token
                    if record.get("entityToken") == "TOKEN"
                    else record.get("entityToken")
                ),
            }
            for record in records
        )
        if materialized or torn:
            write_recaps(home, *materialized, torn=torn)
        result = ContextSnapshotExporter(registry).export(
            request(
                agent,
                recap_enabled=enabled,
                recap_dropped=dropped,
            ).with_switch_id(f"switch-{case}")
        )
        manifest = _manifest(result.path)
        assert manifest["coverage"] in {"partial", "unavailable"}
        assert expected in {gap["code"] for gap in manifest["gaps"]}


def test_receipt_symlink_and_reused_name_are_refused_as_sources(tmp_path: Path) -> None:
    with agent_home(tmp_path) as (registry, agent, home):
        exporter = ContextSnapshotExporter(registry)
        receipt = home / "state" / "home-receipt.json"
        original = receipt.read_text()
        receipt.write_text("{}")
        with pytest.raises(AgentHomeError):
            exporter.export(request(agent).with_switch_id("bad-receipt"))
        receipt.write_text(original)

        recaps = home / "state" / "recaps.jsonl"
        target = tmp_path / "foreign-recaps.jsonl"
        target.write_text('{}\n')
        recaps.symlink_to(target)
        with pytest.raises(ContextSnapshotError, match="symlink"):
            exporter.export(request(agent).with_switch_id("symlink-source"))
        recaps.unlink()

        stale = request(agent).with_switch_id("stale-entity")
        assert registry.destroy(agent.actor) is True
        replacement = registry.create("worker", cwd=str(tmp_path / "work"))
        assert replacement.entity_token != agent.entity_token
        with pytest.raises(Exception, match="changed before home access"):
            exporter.export(stale)
