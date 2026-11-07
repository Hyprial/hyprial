"""Repository-rename regressions for distribution and TS cleanup seams."""

from __future__ import annotations

import json
from pathlib import Path

import hyprial.daemon.impl.updates as updates


ROOT = Path(__file__).resolve().parents[4]
OFFICIAL_GIT_URL = "https://github.com/Hyprial/hyprial.git"


def test_shared_repository_default_stays_on_internal_ssh() -> None:
    assert updates.DEFAULT_GIT_URL == updates.FORGEJO_GIT_URL
    assert updates.OFFICIAL_GIT_URL == OFFICIAL_GIT_URL
    assert updates.FORGEJO_GIT_URL.startswith("ssh://git@")
    assert updates.FORGEJO_GIT_URL.endswith("/HyprialOS/harness-bridge.git")


def test_retired_ts_install_is_removed_before_fresh_python_install() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    operator_guide = (ROOT / "docs" / "guides" / "operator-guide.md").read_text(
        encoding="utf-8"
    )
    cutover = (ROOT / "docs" / "guides" / "cutover-runbook.md").read_text(encoding="utf-8")

    for text in (readme, operator_guide, cutover):
        assert "hyprial-migrate-from-ts" not in text
    assert "npm uninstall -g @hyprial/harness-bridge" in operator_guide
    assert "uv tool install --force" in operator_guide
    assert "不迁移旧状态" in operator_guide
    assert "先删除旧版，再安装" in cutover


def test_wire_golden_sources_follow_archived_ts_repository_name() -> None:
    expectations = json.loads(
        (ROOT / "contract/wire/golden/expectations.json").read_text(encoding="utf-8")
    )
    external_sources = [
        source
        for expectation in expectations["cases"]
        for source in expectation["sources"]
        if source.startswith("../")
    ]
    assert external_sources
    assert all(source.startswith("../harness-bridge-ts/") for source in external_sources)
