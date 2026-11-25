"""wheel-release 的并发组必须是**跨 track 单一组**（整桶级串行）。

背景（2026-10-30 现场）:
    scripts/ci/r2_upload.py:200 `guard_changes()` 比较**整桶** before/after 快照，
    任何 changed key 不在本 run 允许前缀内就 raise。
    而 stable / nightly / internal 三个 track 写的是**同一个 R2 桶**的不同前缀。
    ⇒ 两条 track 并发运行时，每个 run 都会看见对方的写入 ⇒ 双方都判"越界" ⇒ 两个 job 都红，
      尽管 wheels 已上传成功（run 40721 / 40722 的 task 98687 / 98688 就是这个形态:
      98687 `Uploaded wheel/stable/` ×6 同时报 `Bucket changed outside allowed scope: wheel/nightly/…`，
      98688 反之）。

因此:
    · concurrency.group 不得包含 `inputs.track` / `github.ref_name` / `github.ref`（否则重新变成按 track 分组）
    · cancel-in-progress 必须为 false（发布不允许被新事件顶掉）
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[4] / ".forgejo/workflows/wheel-release.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")


def _triggers(doc: dict) -> dict:
    return doc.get("on") or doc.get(True) or {}


def test_concurrency_group_is_single_repo_wide() -> None:
    doc = yaml.safe_load(TEXT) or {}
    conc = doc.get("concurrency") or {}
    group = str(conc.get("group", ""))
    assert group, "wheel-release.yml 缺少 concurrency.group"
    banned = ("inputs.track", "github.ref_name", "github.ref", "matrix.")
    hits = [b for b in banned if b in group]
    assert not hits, (
        f"concurrency.group 里出现了按 track/ref 分组的片段 {hits} ⇒ 会退化成跨 track 并发；当前 group={group!r}"
    )


def test_cancel_in_progress_is_false() -> None:
    doc = yaml.safe_load(TEXT) or {}
    conc = doc.get("concurrency") or {}
    value = conc.get("cancel-in-progress")
    assert value in (False, "false"), f"发布不允许被顶掉，cancel-in-progress 必须 false，当前={value!r}"


def test_group_name_is_stable_literal() -> None:
    """组名应当是字面量（不依赖上下文），否则两条 track 仍可能算出不同组名。"""
    doc = yaml.safe_load(TEXT) or {}
    group = str((doc.get("concurrency") or {}).get("group", ""))
    assert "${{" not in group, f"组名不应包含表达式（会按上下文分裂成多个组）: {group!r}"


def test_comment_records_the_reason() -> None:
    """放宽/改写规则的改动必须把理由写在旁边（本仓库纪律）。"""
    assert "guard_changes" in TEXT, "concurrency 附近必须写明它对应的守卫（r2_upload.py guard_changes）"
    assert re.search(r"r2_upload\.py", TEXT), "必须点名守卫所在文件"


def test_detector_is_not_vacuous() -> None:
    """变异自证: 把 group 改回按 track ⇒ 同一个检测必须命中。"""
    mutated = TEXT.replace("  group: wheel-publish", "  group: wheel-${{ inputs.track || github.ref_name }}", 1)
    assert mutated != TEXT, "变异未生效"
    bad = yaml.safe_load(mutated) or {}
    group = str((bad.get("concurrency") or {}).get("group", ""))
    assert any(b in group for b in ("inputs.track", "github.ref_name")), "变异后检测仍未命中 ⇒ 检测已失效"
