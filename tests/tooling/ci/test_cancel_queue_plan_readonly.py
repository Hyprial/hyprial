"""`scripts/ci/cancel-queue-plan.py` 必须是**只读**的：它只生成"待取消清单"。

规则来源（emergent 2026-10-30）:
    批量取消一律走 API；DB 层 UPDATE/DELETE 需 Allen + emergent 批准。
    ⇒ 本脚本的存在理由就是"给有权限的人一份清单"，它自己不许改任何状态。

这些断言是**静态**的（读源码文本），因此可在 CI 里跑，不需要数据库连接。
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/ci/cancel-queue-plan.py"
TEXT = SCRIPT.read_text(encoding="utf-8")

DML = ("insert into", "update ", "delete from", "alter table", "drop table", "truncate", "grant ", "revoke ")


def test_script_exists_and_is_python() -> None:
    assert SCRIPT.is_file(), f"缺少 {SCRIPT}"
    assert TEXT.lstrip().startswith("#!/usr/bin/env python3")


def test_contains_no_dml() -> None:
    """整份文件（含引号内的 SQL 字符串）不得出现写语句。"""
    low = TEXT.lower()
    hits = [kw for kw in DML if kw in low]
    assert not hits, f"只读脚本里出现了写语句关键字: {hits}"


def test_sql_is_select_only() -> None:
    """取出 SQL 常量，确认它以 select 开头且不含写语句。"""
    m = re.search(r'SQL = """(.*?)"""', TEXT, re.S)
    assert m, "找不到 SQL 常量"
    sql = m.group(1).strip().lower()
    assert sql.startswith("select"), f"SQL 必须以 select 开头，实际: {sql[:40]!r}"
    assert not any(kw in sql for kw in DML), "SQL 里出现了写语句"


def test_excludes_release_chain_by_default() -> None:
    m = re.search(r'DEFAULT_EXCLUDE = \((.*?)\)', TEXT, re.S)
    assert m, "找不到 DEFAULT_EXCLUDE"
    body = m.group(1)
    for wf in ("wheel-release.yml", "github-publish.yml", "release-tag.yml", "main-release.yml",
               "release-main-dispatch.yml"):
        assert wf in body, f"DEFAULT_EXCLUDE 缺少发版链 workflow: {wf}"


def test_skips_runs_with_running_jobs() -> None:
    """有 running job 的 run 必须被排除（否则会把正在跑的活算进待取消）。"""
    assert re.search(r"having\s+count\(\*\)\s+filter\s*\(\s*where\s+j\.status\s*=\s*6\s*\)\s*=\s*0", TEXT), (
        "SQL 缺少 `having count(*) filter (where j.status = 6) = 0`"
    )


def test_dml_detector_is_not_vacuous() -> None:
    """变异自证：往文本里注入一条 UPDATE ⇒ 同一个检测必须能命中。"""
    mutated = TEXT + "\n# update action_run set status=3\n"
    low = mutated.lower()
    assert [kw for kw in DML if kw in low], "注入 UPDATE 后检测仍未命中 ⇒ 检测已失效"


def test_uses_no_third_party_imports() -> None:
    r"""只允许标准库，避免在 CI 里为"只读清单"装依赖。

    ⛔ 用 AST 读 import 节点，不用正则扫文本 —— SQL 里的 "from action_run r join …"
       会被"^from\s+(\w+)"吃成 import（本测试第一版就这么误报过）。
    """
    import ast

    tree = ast.parse(TEXT)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.add(node.module.split(".")[0])
    allowed = {"argparse", "ast", "os", "pathlib", "subprocess", "sys"}
    bad = mods - allowed
    assert not bad, f"出现非标准库依赖: {sorted(bad)}"
